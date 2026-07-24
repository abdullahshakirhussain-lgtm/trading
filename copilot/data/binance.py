"""Binance USD-M perpetual-futures market data via async ccxt + the public
futures/data endpoints. No API key needed for any of this.

Futures-only: every price/ticker/mover reads USD-M perps. Symbols are ccxt perp
form 'BASE/USDT:USDT' internally; render them with fmt.sym for display.
"""
import logging
import time

import ccxt.async_support as ccxt
import httpx

from .. import config, db

log = logging.getLogger(__name__)

FAPI = "https://fapi.binance.com"


def norm_symbol(user_input: str) -> str:
    """Any of 'btc' / 'BTCUSDT' / 'BTC/USDT' / 'BTC/USDT:USDT' -> 'BTC/USDT:USDT'."""
    s = user_input.strip().upper()
    if ":" in s:
        s = s.split(":")[0]
    if "/" in s:
        base = s.split("/")[0]
    elif s.endswith("USDT"):
        base = s[:-4]
    else:
        base = s
    return f"{base}/USDT:USDT"


def _fapi_sym(symbol: str) -> str:
    """ccxt perp symbol -> the plain 'BTCUSDT' the futures/data endpoints expect."""
    return symbol.split(":")[0].replace("/", "")


class Market:
    """Holds the USD-M futures client for the process lifetime."""

    def __init__(self) -> None:
        self.usdm = ccxt.binanceusdm({"enableRateLimit": True})

    async def close(self) -> None:
        try:
            await self.usdm.close()
        except Exception:
            pass

    # --- prices ---

    async def ticker(self, symbol: str) -> dict | None:
        try:
            t = await self.usdm.fetch_ticker(symbol)
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
        """Fetch + persist current perp marks for the watchlist. Returns {symbol: price}."""
        symbols = db.watchlist()
        if not symbols:
            return {}
        out: dict[str, float] = {}
        try:
            tickers = await self.usdm.fetch_tickers(symbols)
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

    async def marks(self, symbols) -> dict[str, float]:
        """Current perp mark/last for arbitrary symbols (positions, held coins)."""
        symbols = list(symbols)
        if not symbols:
            return {}
        try:
            tickers = await self.usdm.fetch_tickers(symbols)
        except Exception as e:
            log.warning("marks failed: %s", e)
            return {}
        return {s: t.get("last") for s, t in tickers.items() if t.get("last")}

    async def last_price(self, symbol: str) -> float | None:
        t = await self.ticker(symbol)
        return t["price"] if t else None

    # --- funding ---

    async def funding_rates(self) -> dict[str, float]:
        """Current funding rate for every USD-M perp, keyed by ccxt perp symbol."""
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
        """All USD-M /USDT perps sorted by 24h % change, volume-floored."""
        try:
            tickers = await self.usdm.fetch_tickers()
        except Exception as e:
            log.warning("movers failed: %s", e)
            return []
        rows = []
        for sym, t in tickers.items():
            if not sym.endswith("/USDT:USDT"):
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
        """Every USD-M /USDT perp with a 24h ticker, one row each. One batch call.

        Rows: {symbol, market, price, chg24, qvol, high, low}. market is always 'perp'.
        """
        try:
            tickers = await self.usdm.fetch_tickers()
        except Exception as e:
            log.warning("scan_universe failed: %s", e)
            return []
        out: list[dict] = []
        for sym, t in tickers.items():
            if not sym.endswith("/USDT:USDT"):
                continue
            price, qvol = t.get("last"), t.get("quoteVolume")
            if price is None or qvol is None:
                continue
            out.append({
                "symbol": sym, "market": "perp", "price": price,
                "chg24": t.get("percentage"), "qvol": qvol,
                "high": t.get("high"), "low": t.get("low"),
            })
        return out

    async def ohlcv(self, symbol: str, timeframe: str = "5m", limit: int = 24,
                    market: str = "perp") -> list[list]:
        """Recent perp candles [[ts, o, h, l, c, v], …]."""
        try:
            return await self.usdm.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        except Exception as e:
            log.warning("ohlcv %s failed: %s", symbol, e)
            return []

    # --- futures-data endpoints (open interest, long/short ratio) ---

    async def _fapi_get(self, path: str, params: dict):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(FAPI + path, params=params,
                                     headers={"User-Agent": "crypto-copilot/1.0"})
                r.raise_for_status()
                return r.json()
        except Exception as e:
            log.warning("fapi %s failed: %s", path, e)
            return None

    async def open_interest_hist(self, symbol: str, period: str | None = None,
                                 limit: int = 12) -> list[dict]:
        """Open-interest series: [{ts, oi (contracts), oi_usd}], oldest→newest."""
        data = await self._fapi_get("/futures/data/openInterestHist", {
            "symbol": _fapi_sym(symbol), "period": period or config.OI_PERIOD, "limit": limit})
        out = []
        for d in data or []:
            try:
                out.append({"ts": int(d.get("timestamp", 0)) // 1000,
                            "oi": float(d.get("sumOpenInterest") or 0),
                            "oi_usd": float(d.get("sumOpenInterestValue") or 0)})
            except (ValueError, TypeError):
                continue
        return out

    async def long_short_ratio(self, symbol: str, period: str | None = None) -> float | None:
        """Latest global long/short *account* ratio (>1 = crowd net long)."""
        data = await self._fapi_get("/futures/data/globalLongShortAccountRatio", {
            "symbol": _fapi_sym(symbol), "period": period or config.OI_PERIOD, "limit": 1})
        if data:
            try:
                return float(data[-1]["longShortRatio"])
            except (KeyError, ValueError, TypeError):
                return None
        return None

    # --- listings (new-symbol detection via exchangeInfo diff) ---

    async def list_symbols(self, market: str = "perp") -> list[str]:
        try:
            markets = await self.usdm.load_markets(reload=True)
        except Exception as e:
            log.warning("list_symbols failed: %s", e)
            return []
        return [s for s, m in markets.items() if m.get("active")]
