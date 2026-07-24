"""Binance public market data via async ccxt. No API key needed for any of this."""
import logging
import time

import ccxt.async_support as ccxt

from .. import db

log = logging.getLogger(__name__)


def norm_symbol(user_input: str) -> str:
    """'btc' -> 'BTC/USDT'; 'BTC/USDT' passes through."""
    s = user_input.strip().upper()
    if "/" not in s:
        s = f"{s}/USDT"
    return s


class Market:
    """Holds the spot + USD-M futures clients for the process lifetime."""

    def __init__(self) -> None:
        self.spot = ccxt.binance({"enableRateLimit": True})
        self.usdm = ccxt.binanceusdm({"enableRateLimit": True})

    async def close(self) -> None:
        for c in (self.spot, self.usdm):
            try:
                await c.close()
            except Exception:
                pass

    # --- prices ---

    async def ticker(self, symbol: str) -> dict | None:
        try:
            t = await self.spot.fetch_ticker(symbol)
            return {
                "symbol": symbol,
                "price": t.get("last"),
                "pct24h": t.get("percentage"),
                "high": t.get("high"),
                "low": t.get("low"),
                "qvol": t.get("quoteVolume"),
            }
        except Exception as e:
            log.warning("ticker %s failed: %s", symbol, e)
            return None

    async def watchlist_prices(self) -> dict[str, float]:
        """Fetch + persist current prices for the watchlist. Returns {symbol: price}."""
        symbols = db.watchlist()
        if not symbols:
            return {}
        out: dict[str, float] = {}
        try:
            tickers = await self.spot.fetch_tickers(symbols)
        except Exception as e:
            log.warning("watchlist_prices failed: %s", e)
            return {}
        now = int(time.time())
        rows = []
        for sym, t in tickers.items():
            px = t.get("last")
            if px:
                out[sym] = px
                rows.append((now, sym, px))
        if rows:
            db.executemany("INSERT INTO prices(ts, symbol, price) VALUES (?, ?, ?)", rows)
        db.kv_set("last_poll_prices", str(now))
        return out

    async def last_price(self, symbol: str) -> float | None:
        t = await self.ticker(symbol)
        return t["price"] if t else None

    # --- funding ---

    async def funding_rates(self) -> dict[str, float]:
        """Current funding rate for every USD-M perp, keyed by base symbol pair."""
        try:
            rates = await self.usdm.fetch_funding_rates()
        except Exception as e:
            log.warning("funding_rates failed: %s", e)
            return {}
        out = {}
        for sym, r in rates.items():
            rate = r.get("fundingRate")
            if rate is not None:
                out[sym] = rate
        db.kv_set("last_poll_funding", str(int(time.time())))
        return out

    # --- movers ---

    async def movers(self, min_qvol: float) -> list[dict]:
        """All spot /USDT pairs sorted by 24h % change, volume-floored."""
        try:
            tickers = await self.spot.fetch_tickers()
        except Exception as e:
            log.warning("movers failed: %s", e)
            return []
        rows = []
        for sym, t in tickers.items():
            if not sym.endswith("/USDT") or ":" in sym:
                continue
            pct = t.get("percentage")
            qvol = t.get("quoteVolume")
            if pct is None or qvol is None or qvol < min_qvol:
                continue
            rows.append({"symbol": sym, "pct24h": pct, "qvol": qvol, "price": t.get("last")})
        rows.sort(key=lambda r: r["pct24h"], reverse=True)
        db.kv_set("last_poll_movers", str(int(time.time())))
        return rows

    # --- explosive-mover scanner ---

    async def scan_universe(self) -> list[dict]:
        """Every /USDT spot pair and USD-M perp with a 24h ticker, in one row each.

        Two batch calls (spot + perps) cover the whole market — cheap enough to run
        every couple of minutes. Rows: {symbol, market, price, chg24, qvol, high, low}.
        """
        out: list[dict] = []
        for client, market, suffix in (
            (self.spot, "spot", "/USDT"),
            (self.usdm, "perp", "/USDT:USDT"),
        ):
            try:
                tickers = await client.fetch_tickers()
            except Exception as e:
                log.warning("scan_universe(%s) failed: %s", market, e)
                continue
            for sym, t in tickers.items():
                if not sym.endswith(suffix):
                    continue
                price, qvol = t.get("last"), t.get("quoteVolume")
                if price is None or qvol is None:
                    continue
                out.append({
                    "symbol": sym, "market": market, "price": price,
                    "chg24": t.get("percentage"), "qvol": qvol,
                    "high": t.get("high"), "low": t.get("low"),
                })
        return out

    async def ohlcv(self, symbol: str, timeframe: str = "5m", limit: int = 24,
                    market: str = "spot") -> list[list]:
        """Recent candles [[ts, o, h, l, c, v], …] from the matching client."""
        client = self.spot if market == "spot" else self.usdm
        try:
            return await client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        except Exception as e:
            log.warning("ohlcv %s (%s) failed: %s", symbol, market, e)
            return []

    # --- listings (new-symbol detection via exchangeInfo diff) ---

    async def list_symbols(self, market: str) -> list[str]:
        client = self.spot if market == "spot" else self.usdm
        try:
            markets = await client.load_markets(reload=True)
        except Exception as e:
            log.warning("list_symbols(%s) failed: %s", market, e)
            return []
        return [s for s, m in markets.items() if m.get("active")]
