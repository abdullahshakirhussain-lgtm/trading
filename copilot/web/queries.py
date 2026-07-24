"""Read-only queries backing the dashboard. Never writes — the bot owns writes."""
import json
import time

from .. import config, db
from ..engine import narrative, paper, read


def latest_prices() -> dict[str, float]:
    """Most recent stored price per symbol (written by the bot's price poller)."""
    rows = db.fetchall(
        "SELECT symbol, price FROM prices WHERE ts = (SELECT MAX(ts) FROM prices p2 "
        "WHERE p2.symbol = prices.symbol) GROUP BY symbol")
    return {r["symbol"]: r["price"] for r in rows}


def overview() -> dict:
    prices = latest_prices()
    s = paper.stats(prices, 0)
    real = paper.stats(prices, 1)
    now = int(time.time())

    def ago(key):
        v = db.kv_get(key)
        return round((now - int(v)) / 60) if v else None

    return {
        "equity": s.get("equity"),
        "cash": s.get("cash"),
        "return_pct": s.get("return_pct"),
        "btc_bench_pct": s.get("btc_bench_pct"),
        "max_drawdown_pct": s.get("max_drawdown_pct"),
        "days": s.get("days"),
        "start_cash": config.PAPER_START_CASH,
        "buckets": s["buckets"],
        "real_buckets": real["buckets"],
        "fng": {"value": db.kv_get("fng_value"), "label": db.kv_get("fng_label")},
        "freshness_min": {
            "prices": ago("last_poll_prices"),
            "funding": ago("last_poll_funding"),
            "news": ago("last_poll_news"),
            "radar": ago("last_poll_radar"),
            "scan": ago("last_poll_scan"),
            "listings": ago("last_poll_announcements"),
        },
        "degen_cap_pct": config.DEGEN_CAP * 100,
        "degen_used": paper.degen_cost_basis(0),
    }


def equity_curve() -> list[dict]:
    rows = db.fetchall(
        "SELECT ts, equity, btc_bench FROM equity_history ORDER BY ts")
    return [{"ts": r["ts"], "equity": r["equity"], "btc": r["btc_bench"]} for r in rows]


def positions() -> list[dict]:
    prices = latest_prices()
    out = []
    for is_real, label in ((0, "paper"), (1, "real")):
        for p in paper.positions(is_real):
            px = prices.get(p["symbol"])
            value = p["qty"] * px if px else None
            cost = p["qty"] * p["avg_cost"]
            out.append({
                "kind": label, "symbol": p["symbol"], "bucket": p["bucket"],
                "qty": p["qty"], "avg_cost": p["avg_cost"], "price": px,
                "value": value, "cost": cost,
                "pnl": (value - cost) if value is not None else None,
                "pnl_pct": ((value / cost - 1) * 100) if value and cost else None,
            })
    out.sort(key=lambda r: -(r["value"] or 0))
    return out


def trades(limit: int = 60) -> list[dict]:
    rows = db.fetchall(
        "SELECT ts, symbol, side, usd, qty, price, fee, bucket, is_real, realized_pnl "
        "FROM paper_trades ORDER BY ts DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def radar(hours: int = 48) -> list[dict]:
    rows = db.fetchall(
        "SELECT * FROM radar WHERE last_seen > ? "
        "ORDER BY (verdict = 'PASS') DESC, liq_usd DESC LIMIT 60",
        (int(time.time()) - hours * 3600,))
    return [dict(r) for r in rows]


def hot_movers() -> list[dict]:
    """Live board: the current scan's top movers, refreshed every scan cycle by
    poll_scan (kv 'scan_live'). Not the sparse fired-alert feed — this stays current."""
    raw = db.kv_get("scan_live")
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return []


def heat() -> list[dict]:
    return [h for h in narrative.heat() if h["count24h"] > 0]


def news(limit: int = 30) -> list[dict]:
    rows = db.fetchall(
        "SELECT ts, source, title, url, narratives, severity FROM news "
        "ORDER BY ts DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def alerts(limit: int = 40) -> list[dict]:
    rows = db.fetchall(
        "SELECT ts, kind, message FROM alert_log ORDER BY ts DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def watchlist_table() -> list[dict]:
    prices = latest_prices()
    out = []
    for sym in db.watchlist():
        base = sym.split("/")[0]
        f = db.fetchone(
            "SELECT rate FROM funding WHERE symbol LIKE ? ORDER BY ts DESC LIMIT 1",
            (base + "/%",))
        out.append({"symbol": sym, "price": prices.get(sym),
                    "funding": f["rate"] if f else None})
    return out


def snapshot() -> dict:
    conds = read.conditions()
    return {
        "readings": read.readings(),
        "conditions": conds["conditions"],
        "conditions_meta": {"tracking_days": conds["tracking_days"]},
        "market_summary": read.summary(),
        "overview": overview(),
        "equity_curve": equity_curve(),
        "positions": positions(),
        "trades": trades(),
        "hot": hot_movers(),
        "radar": radar(),
        "heat": heat(),
        "news": news(),
        "alerts": alerts(),
        "watchlist": watchlist_table(),
        "server_time": int(time.time()),
    }
