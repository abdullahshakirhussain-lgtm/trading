"""Paper trading engine.

Virtual portfolio with honest friction: taker fee + slippage on every fill
(memes get a fatter slippage assumption). Two buckets — 'core' and 'degen' —
tracked separately so the stats eventually answer the only question that
matters: which style actually earns?

Trades flagged is_real=1 are a journal of actual money and never touch the
paper cash balance.
"""
import time

from .. import config, db


class TradeError(Exception):
    pass


# --- lifecycle ---

def ensure_init(btc_price: float | None) -> None:
    if db.kv_get("paper_cash") is None:
        db.kv_set("paper_cash", str(config.PAPER_START_CASH))
        db.kv_set("paper_start_ts", str(int(time.time())))
        if btc_price:
            db.kv_set("paper_btc_start", str(btc_price))


def cash() -> float:
    return float(db.kv_get("paper_cash", str(config.PAPER_START_CASH)))


def _set_cash(v: float) -> None:
    db.kv_set("paper_cash", f"{v:.8f}")


# --- positions ---

def positions(is_real: int = 0) -> list[dict]:
    rows = db.fetchall(
        "SELECT symbol, bucket, qty, avg_cost FROM positions "
        "WHERE is_real = ? AND qty > 1e-12 ORDER BY bucket, symbol", (is_real,))
    return [dict(r) for r in rows]


def _get_pos(symbol: str, bucket: str, is_real: int) -> tuple[float, float]:
    row = db.fetchone(
        "SELECT qty, avg_cost FROM positions WHERE symbol=? AND bucket=? AND is_real=?",
        (symbol, bucket, is_real))
    return (row["qty"], row["avg_cost"]) if row else (0.0, 0.0)


def _set_pos(symbol: str, bucket: str, is_real: int, qty: float, avg_cost: float) -> None:
    db.execute(
        "INSERT INTO positions(symbol, bucket, is_real, qty, avg_cost) VALUES (?,?,?,?,?) "
        "ON CONFLICT(symbol, bucket, is_real) DO UPDATE SET qty=excluded.qty, "
        "avg_cost=excluded.avg_cost",
        (symbol, bucket, is_real, qty, avg_cost))


def degen_cost_basis(is_real: int = 0) -> float:
    row = db.fetchone(
        "SELECT COALESCE(SUM(qty * avg_cost), 0) AS cb FROM positions "
        "WHERE bucket='degen' AND is_real=?", (is_real,))
    return row["cb"]


# --- trades ---

def _slip(bucket: str) -> float:
    return config.SLIPPAGE_DEGEN if bucket == "degen" else config.SLIPPAGE_CORE


def buy(symbol: str, usd: float, price: float, bucket: str = "core",
        is_real: int = 0, equity_now: float | None = None) -> dict:
    if usd <= 0:
        raise TradeError("Amount must be positive.")
    fill = price * (1 + _slip(bucket))
    fee = usd * config.FEE_RATE
    if not is_real:
        total = usd + fee
        if total > cash():
            raise TradeError(f"Not enough paper cash (${cash():,.2f} available).")
        if bucket == "degen" and equity_now:
            cap = config.DEGEN_CAP * equity_now
            if degen_cost_basis() + usd > cap:
                raise TradeError(
                    f"Degen cap: open degen cost basis would exceed "
                    f"{config.DEGEN_CAP:.0%} of equity (${cap:,.0f}). "
                    f"That cap is the whole discipline — respect it.")
        _set_cash(cash() - total)
    qty = usd / fill
    old_qty, old_avg = _get_pos(symbol, bucket, is_real)
    new_qty = old_qty + qty
    new_avg = (old_qty * old_avg + qty * fill) / new_qty
    _set_pos(symbol, bucket, is_real, new_qty, new_avg)
    db.execute(
        "INSERT INTO paper_trades(ts, symbol, side, usd, qty, price, fee, bucket, is_real, realized_pnl) "
        "VALUES (?,?,?,?,?,?,?,?,?,NULL)",
        (int(time.time()), symbol, "buy", usd, qty, fill, fee, bucket, is_real))
    return {"symbol": symbol, "qty": qty, "fill": fill, "fee": fee, "bucket": bucket}


def sell(symbol: str, price: float, usd: float | None = None,
         bucket: str | None = None, is_real: int = 0) -> dict:
    """Sell $usd worth (or the whole position when usd is None)."""
    # find the position; if bucket not given, prefer whichever holds the symbol
    buckets = [bucket] if bucket else ["core", "degen"]
    pos_bucket, qty_held, avg_cost = None, 0.0, 0.0
    for b in buckets:
        q, a = _get_pos(symbol, b, is_real)
        if q > 1e-12:
            pos_bucket, qty_held, avg_cost = b, q, a
            break
    if not pos_bucket:
        raise TradeError(f"No open {'real' if is_real else 'paper'} position in {symbol}.")

    fill = price * (1 - _slip(pos_bucket))
    sell_qty = qty_held if usd is None else min(qty_held, usd / fill)
    if sell_qty <= 0:
        raise TradeError("Nothing to sell.")
    gross = sell_qty * fill
    fee = gross * config.FEE_RATE
    realized = (fill - avg_cost) * sell_qty - fee
    if not is_real:
        _set_cash(cash() + gross - fee)
    remaining = qty_held - sell_qty
    _set_pos(symbol, pos_bucket, is_real, remaining, avg_cost if remaining > 1e-12 else 0.0)
    db.execute(
        "INSERT INTO paper_trades(ts, symbol, side, usd, qty, price, fee, bucket, is_real, realized_pnl) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (int(time.time()), symbol, "sell", gross, sell_qty, fill, fee, pos_bucket, is_real, realized))
    return {"symbol": symbol, "qty": sell_qty, "fill": fill, "fee": fee,
            "realized": realized, "bucket": pos_bucket, "closed": remaining <= 1e-12}


# --- valuation & stats ---

def equity(prices: dict[str, float], is_real: int = 0) -> float:
    total = cash() if not is_real else 0.0
    for p in positions(is_real):
        px = prices.get(p["symbol"])
        total += p["qty"] * (px if px else p["avg_cost"])
    return total


def snapshot_equity(prices: dict[str, float], btc_price: float | None) -> None:
    eq = equity(prices)
    btc_start = db.kv_get("paper_btc_start")
    bench = None
    if btc_start and btc_price:
        bench = config.PAPER_START_CASH * btc_price / float(btc_start)
    db.execute("INSERT INTO equity_history(ts, equity, btc_bench) VALUES (?,?,?)",
               (int(time.time()), eq, bench))


def max_drawdown() -> float | None:
    rows = db.fetchall("SELECT equity FROM equity_history ORDER BY ts")
    if len(rows) < 2:
        return None
    peak, mdd = rows[0]["equity"], 0.0
    for r in rows:
        peak = max(peak, r["equity"])
        mdd = max(mdd, (peak - r["equity"]) / peak)
    return mdd * 100


def stats(prices: dict[str, float], is_real: int = 0) -> dict:
    out: dict = {"buckets": {}, "is_real": is_real}
    for b in ("core", "degen"):
        sells = db.fetchall(
            "SELECT realized_pnl FROM paper_trades "
            "WHERE side='sell' AND bucket=? AND is_real=? AND realized_pnl IS NOT NULL",
            (b, is_real))
        wins = sum(1 for r in sells if r["realized_pnl"] > 0)
        realized = sum(r["realized_pnl"] for r in sells)
        fees = db.fetchone(
            "SELECT COALESCE(SUM(fee),0) AS f FROM paper_trades WHERE bucket=? AND is_real=?",
            (b, is_real))["f"]
        n_trades = db.fetchone(
            "SELECT COUNT(*) AS n FROM paper_trades WHERE bucket=? AND is_real=?",
            (b, is_real))["n"]
        unreal = 0.0
        for p in positions(is_real):
            if p["bucket"] == b:
                px = prices.get(p["symbol"])
                if px:
                    unreal += (px - p["avg_cost"]) * p["qty"]
        out["buckets"][b] = {
            "trades": n_trades, "closed": len(sells), "wins": wins,
            "win_rate": (wins / len(sells) * 100) if sells else None,
            "realized": realized, "unrealized": unreal, "fees": fees,
        }
    if not is_real:
        eq = equity(prices)
        out["equity"] = eq
        out["cash"] = cash()
        out["return_pct"] = (eq / config.PAPER_START_CASH - 1) * 100
        btc_start = db.kv_get("paper_btc_start")
        btc_now = prices.get("BTC/USDT")
        if btc_start and btc_now:
            out["btc_bench_pct"] = (btc_now / float(btc_start) - 1) * 100
        out["max_drawdown_pct"] = max_drawdown()
        start_ts = db.kv_get("paper_start_ts")
        out["days"] = (time.time() - float(start_ts)) / 86400 if start_ts else 0
    return out
