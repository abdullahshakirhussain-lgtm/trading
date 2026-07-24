"""Futures paper-trading engine (USD-M perps).

Isolated-margin model with long + short + leverage. Honest friction: taker fee +
slippage on entry and exit, funding accrued over the hold, and forced liquidation
when the mark crosses the (approximate) liq price. Simplifications, stated plainly:
one position per symbol (one-way mode), a flat maintenance-margin rate instead of
Binance's tiered MMR, and funding prorated from the 8h rate. It's a track record,
not an exchange.

Two buckets — 'core' and 'degen' (high-leverage) — tracked separately so the stats
answer the only question that matters: which style actually earns? Trades flagged
is_real=1 are a journal of actual money and never touch the paper cash balance.
"""
import time

from .. import config, db


class TradeError(Exception):
    pass


def _sign(side: str) -> int:
    return 1 if side == "long" else -1


def _slip(bucket: str) -> float:
    return config.SLIPPAGE_DEGEN if bucket == "degen" else config.SLIPPAGE_CORE


def _liq_price(entry: float, side: str, lev: float) -> float:
    """Approx isolated-margin liquidation price (flat MMR, no tiers)."""
    ss = _sign(side)
    return entry * (1 - ss * (1 / lev - config.FUT_MMR))


# --- lifecycle ---

def ensure_init(btc_price: float | None) -> None:
    if db.kv_get("fut_cash") is None:
        db.kv_set("fut_cash", str(config.PAPER_START_CASH))
        db.kv_set("fut_start_ts", str(int(time.time())))
        if btc_price:
            db.kv_set("fut_btc_start", str(btc_price))


def cash() -> float:
    return float(db.kv_get("fut_cash", str(config.PAPER_START_CASH)))


def _set_cash(v: float) -> None:
    db.kv_set("fut_cash", f"{v:.8f}")


# --- positions ---

def positions(is_real: int = 0) -> list[dict]:
    rows = db.fetchall(
        "SELECT symbol, side, bucket, qty, entry, leverage, margin, liq_price, "
        "funding_accrued, opened_ts, last_funding_ts FROM fut_positions "
        "WHERE is_real = ? AND qty > 1e-12 ORDER BY bucket, symbol", (is_real,))
    return [dict(r) for r in rows]


def _get_pos(symbol: str, is_real: int) -> dict | None:
    row = db.fetchone(
        "SELECT symbol, side, bucket, qty, entry, leverage, margin, liq_price, "
        "funding_accrued, opened_ts, last_funding_ts FROM fut_positions "
        "WHERE symbol=? AND is_real=?", (symbol, is_real))
    return dict(row) if row else None


def _put_pos(p: dict, is_real: int) -> None:
    db.execute(
        "INSERT INTO fut_positions(symbol, is_real, side, bucket, qty, entry, leverage, "
        "margin, liq_price, funding_accrued, opened_ts, last_funding_ts) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(symbol, is_real) DO UPDATE SET side=excluded.side, bucket=excluded.bucket, "
        "qty=excluded.qty, entry=excluded.entry, leverage=excluded.leverage, "
        "margin=excluded.margin, liq_price=excluded.liq_price, "
        "funding_accrued=excluded.funding_accrued, last_funding_ts=excluded.last_funding_ts",
        (p["symbol"], is_real, p["side"], p["bucket"], p["qty"], p["entry"], p["leverage"],
         p["margin"], p["liq_price"], p["funding_accrued"], p["opened_ts"], p["last_funding_ts"]))


def _del_pos(symbol: str, is_real: int) -> None:
    db.execute("DELETE FROM fut_positions WHERE symbol=? AND is_real=?", (symbol, is_real))


def degen_margin(is_real: int = 0) -> float:
    row = db.fetchone(
        "SELECT COALESCE(SUM(margin), 0) AS m FROM fut_positions "
        "WHERE bucket='degen' AND is_real=? AND qty > 1e-12", (is_real,))
    return row["m"]


# --- open / close ---

def open_position(symbol: str, margin: float, price: float, side: str = "long",
                  leverage: float | None = None, bucket: str | None = None,
                  is_real: int = 0, equity_now: float | None = None) -> dict:
    if margin <= 0:
        raise TradeError("Margin must be positive.")
    lev = float(leverage or config.FUT_DEFAULT_LEV)
    if lev < 1:
        raise TradeError("Leverage must be at least 1x.")
    lev = min(lev, config.FUT_MAX_LEV)
    if bucket is None:
        bucket = "degen" if lev >= config.FUT_DEGEN_MIN_LEV else "core"
    ss = _sign(side)
    entry = price * (1 + ss * _slip(bucket))     # long fills up, short fills down
    notional = margin * lev
    fee = notional * config.FEE_RATE
    qty = notional / entry

    if not is_real:
        if margin + fee > cash():
            raise TradeError(f"Not enough paper cash (${cash():,.2f} available) for "
                             f"${margin:,.2f} margin + ${fee:,.2f} fee.")
        if bucket == "degen" and equity_now:
            cap = config.DEGEN_CAP * equity_now
            if degen_margin() + margin > cap:
                raise TradeError(
                    f"Degen cap: open degen margin would exceed {config.DEGEN_CAP:.0%} of "
                    f"equity (${cap:,.0f}). That cap is the whole discipline — respect it.")
        _set_cash(cash() - margin - fee)

    now = int(time.time())
    existing = _get_pos(symbol, is_real)
    if existing and existing["qty"] > 1e-12:
        if existing["side"] != side:
            raise TradeError(
                f"You already hold a {existing['side']} on this symbol. Close it "
                f"before opening a {side}.")
        # Add to the position: weighted entry, summed qty/margin, recomputed lev + liq.
        new_qty = existing["qty"] + qty
        new_entry = (existing["qty"] * existing["entry"] + qty * entry) / new_qty
        new_margin = existing["margin"] + margin
        new_notional = existing["qty"] * existing["entry"] + notional
        new_lev = new_notional / new_margin
        pos = {**existing, "qty": new_qty, "entry": new_entry, "margin": new_margin,
               "leverage": new_lev, "liq_price": _liq_price(new_entry, side, new_lev),
               "last_funding_ts": existing["last_funding_ts"] or now}
    else:
        pos = {"symbol": symbol, "side": side, "bucket": bucket, "qty": qty, "entry": entry,
               "leverage": lev, "margin": margin, "liq_price": _liq_price(entry, side, lev),
               "funding_accrued": 0.0, "opened_ts": now, "last_funding_ts": now}
    _put_pos(pos, is_real)

    db.execute(
        "INSERT INTO fut_trades(ts, symbol, side, action, bucket, margin, notional, qty, "
        "entry, exit, leverage, fee, funding, realized_pnl, is_real) "
        "VALUES (?,?,?,?,?,?,?,?,?,NULL,?,?,0,NULL,?)",
        (now, symbol, side, "open", pos["bucket"], margin, notional, qty, entry,
         pos["leverage"], fee, is_real))
    return {"symbol": symbol, "side": side, "qty": qty, "entry": entry, "fee": fee,
            "leverage": pos["leverage"], "margin": margin, "liq_price": pos["liq_price"],
            "bucket": pos["bucket"]}


def close_position(symbol: str, price: float, is_real: int = 0,
                   action: str = "close") -> dict:
    pos = _get_pos(symbol, is_real)
    if not pos or pos["qty"] <= 1e-12:
        raise TradeError(f"No open {'real' if is_real else 'paper'} position in {symbol}.")
    ss = _sign(pos["side"])
    qty, entry, margin = pos["qty"], pos["entry"], pos["margin"]
    funding = pos["funding_accrued"]

    if action == "liquidation":
        exit_px = pos["liq_price"]
        close_fee = 0.0
        realized = -margin                      # the whole margin is gone
        cash_back = 0.0
    else:
        exit_px = price * (1 - ss * _slip(pos["bucket"]))   # long sells down, short buys up
        gross = (exit_px - entry) * qty * ss
        close_fee = qty * exit_px * config.FEE_RATE
        # openFee was already paid from cash at open; realized is the full trade P&L.
        open_fee = (qty * entry) * config.FEE_RATE
        realized = gross - open_fee - close_fee - funding
        cash_back = margin + gross - close_fee - funding

    if not is_real:
        _set_cash(cash() + cash_back)
    _del_pos(symbol, is_real)
    db.execute(
        "INSERT INTO fut_trades(ts, symbol, side, action, bucket, margin, notional, qty, "
        "entry, exit, leverage, fee, funding, realized_pnl, is_real) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (int(time.time()), symbol, pos["side"], action, pos["bucket"], margin,
         qty * entry, qty, entry, exit_px, pos["leverage"], close_fee, funding,
         realized, is_real))
    return {"symbol": symbol, "side": pos["side"], "qty": qty, "exit": exit_px,
            "realized": realized, "funding": funding, "fee": close_fee,
            "bucket": pos["bucket"], "action": action}


# --- funding + liquidation (called from the positions job) ---

def _accrue_funding(pos: dict, mark: float, rate: float, now: int, is_real: int) -> None:
    last = pos["last_funding_ts"] or pos["opened_ts"] or now
    dt_h = (now - last) / 3600
    if dt_h <= 0:
        return
    payment = _sign(pos["side"]) * (pos["qty"] * mark) * rate * (dt_h / 8)  # +cost / -credit
    db.execute("UPDATE fut_positions SET funding_accrued = funding_accrued + ?, "
               "last_funding_ts = ? WHERE symbol=? AND is_real=?",
               (payment, now, pos["symbol"], is_real))


def mark_manage(marks: dict[str, float], rates: dict[str, float],
                is_real: int = 0) -> list[dict]:
    """Accrue funding and force-liquidate crossed positions. Returns liquidation events."""
    now = int(time.time())
    liquidations = []
    for pos in positions(is_real):
        mark = marks.get(pos["symbol"])
        if not mark:
            continue
        rate = rates.get(pos["symbol"])
        if rate is not None:
            _accrue_funding(pos, mark, rate, now, is_real)
        ss = _sign(pos["side"])
        crossed = (ss == 1 and mark <= pos["liq_price"]) or \
                  (ss == -1 and mark >= pos["liq_price"])
        if crossed:
            res = close_position(pos["symbol"], pos["liq_price"], is_real, action="liquidation")
            liquidations.append(res)
    return liquidations


# --- valuation & stats ---

def equity(marks: dict[str, float], is_real: int = 0) -> float:
    total = cash() if not is_real else 0.0
    for pos in positions(is_real):
        mark = marks.get(pos["symbol"]) or pos["entry"]
        upnl = (mark - pos["entry"]) * pos["qty"] * _sign(pos["side"])
        total += pos["margin"] + upnl - pos["funding_accrued"]
    return total


def snapshot_equity(marks: dict[str, float], btc_price: float | None) -> None:
    eq = equity(marks)
    btc_start = db.kv_get("fut_btc_start")
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
        if peak > 0:
            mdd = max(mdd, (peak - r["equity"]) / peak)
    return mdd * 100


def stats(marks: dict[str, float], is_real: int = 0) -> dict:
    out: dict = {"buckets": {}, "is_real": is_real}
    for b in ("core", "degen"):
        closed = db.fetchall(
            "SELECT realized_pnl FROM fut_trades WHERE action IN ('close','liquidation') "
            "AND bucket=? AND is_real=? AND realized_pnl IS NOT NULL", (b, is_real))
        wins = sum(1 for r in closed if r["realized_pnl"] > 0)
        realized = sum(r["realized_pnl"] for r in closed)
        fees = db.fetchone(
            "SELECT COALESCE(SUM(fee),0) AS f FROM fut_trades WHERE bucket=? AND is_real=?",
            (b, is_real))["f"]
        n_trades = db.fetchone(
            "SELECT COUNT(*) AS n FROM fut_trades WHERE action='open' AND bucket=? AND is_real=?",
            (b, is_real))["n"]
        unreal = 0.0
        for p in positions(is_real):
            if p["bucket"] == b:
                mark = marks.get(p["symbol"])
                if mark:
                    unreal += (mark - p["entry"]) * p["qty"] * _sign(p["side"])
        out["buckets"][b] = {
            "trades": n_trades, "closed": len(closed), "wins": wins,
            "win_rate": (wins / len(closed) * 100) if closed else None,
            "realized": realized, "unrealized": unreal, "fees": fees,
        }
    if not is_real:
        eq = equity(marks)
        out["equity"] = eq
        out["cash"] = cash()
        out["return_pct"] = (eq / config.PAPER_START_CASH - 1) * 100
        btc_start = db.kv_get("fut_btc_start")
        btc_now = marks.get("BTC/USDT:USDT")
        if btc_start and btc_now:
            out["btc_bench_pct"] = (btc_now / float(btc_start) - 1) * 100
        out["max_drawdown_pct"] = max_drawdown()
        start_ts = db.kv_get("fut_start_ts")
        out["days"] = (time.time() - float(start_ts)) / 86400 if start_ts else 0
    return out
