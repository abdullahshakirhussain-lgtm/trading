"""Periodic jobs: poll data, detect conditions, push Telegram alerts.

Every job takes (market, send) — `send` is an async callable(text) wired to
Telegram by the bot layer, or print() in selftest mode. All alerts dedup
through db.dedup_ok so a persistent condition doesn't spam. Futures-only:
symbols are USD-M perps, rendered with fmt.sym for display.
"""
import json
import logging
import math
import statistics
import time

from .. import config, db, fmt
from ..data import announcements, feargreed, news
from . import narrative, paper, read, scanner

log = logging.getLogger(__name__)


async def _alert(send, kind: str, key: str, text: str, cooldown: int) -> None:
    if db.dedup_ok(kind, key, cooldown):
        db.log_alert(kind, key, text)
        await send(text)


# --- prices + user price alerts ---

async def poll_prices(market, send) -> None:
    prices = await market.watchlist_prices()
    if not prices:
        return
    for row in db.fetchall("SELECT * FROM price_alerts WHERE fired_ts IS NULL"):
        px = prices.get(row["symbol"])
        if px is None:
            continue
        hit = (row["direction"] == "above" and px >= row["level"]) or \
              (row["direction"] == "below" and px <= row["level"])
        if hit:
            db.execute("UPDATE price_alerts SET fired_ts=? WHERE id=?",
                       (int(time.time()), row["id"]))
            await send(f"🎯 <b>{fmt.esc(fmt.sym(row['symbol']))}</b> crossed {row['direction']} "
                       f"{fmt.price(row['level'])} — now {fmt.price(px)}")


# --- funding ---

async def poll_funding(market, send) -> None:
    rates = await market.funding_rates()
    if not rates:
        return
    now = int(time.time())
    watch = db.watchlist()
    rows = [(now, s, r) for s, r in rates.items()
            if any(s.startswith(w.split("/")[0] + "/") for w in watch)]
    if rows:
        db.executemany("INSERT INTO funding(ts, symbol, rate) VALUES (?,?,?)", rows)
    extremes = sorted(((s, r) for s, r in rates.items() if abs(r) >= config.FUNDING_EXTREME),
                      key=lambda x: abs(x[1]), reverse=True)[:5]
    for sym, rate in extremes:
        side = "longs paying (crowded long)" if rate > 0 else "shorts paying (crowded short)"
        await _alert(send, "funding", sym,
                     f"⚡ <b>Funding extreme</b> {fmt.esc(fmt.sym(sym))}: "
                     f"{fmt.funding_pct(rate)}/8h — {side}", config.COOLDOWN_FUNDING)


# --- movers ---

async def poll_movers(market, send) -> None:
    rows = await market.movers(config.MOVER_MIN_QVOL)
    if not rows:
        return
    watch_bases = {w.split("/")[0] for w in db.watchlist()}
    for r in rows:
        base = r["symbol"].split("/")[0]
        p = r["pct24h"]
        if base in watch_bases and abs(p) >= config.WATCHLIST_MOVE_PCT:
            direction = "up" if p > 0 else "down"
            await _alert(send, "watch_move", f"{r['symbol']}:{direction}",
                         f"📈 <b>Watchlist move</b> {fmt.esc(fmt.sym(r['symbol']))} {fmt.pct(p)} "
                         f"in 24h (vol {fmt.compact_usd(r['qvol'])})", config.COOLDOWN_MOVER)
        elif abs(p) >= config.MARKET_MOVER_PCT:
            await _alert(send, "mkt_move", f"{r['symbol']}:{'up' if p > 0 else 'down'}",
                         f"🚀 <b>Big mover</b> {fmt.esc(fmt.sym(r['symbol']))} {fmt.pct(p)} "
                         f"in 24h (vol {fmt.compact_usd(r['qvol'])})", config.COOLDOWN_MOVER * 2)


# --- fear & greed ---

async def poll_fng(market, send) -> None:
    fng = await feargreed.fetch()
    if not fng:
        return
    db.kv_set("fng_value", str(fng["value"]))
    db.kv_set("fng_label", fng["label"])
    v = fng["value"]
    if v <= config.FNG_LOW:
        await _alert(send, "fng", "low",
                     f"😱 <b>Fear &amp; Greed: {v}</b> ({fmt.esc(fng['label'])}) — extreme fear zone",
                     config.COOLDOWN_FNG)
    elif v >= config.FNG_HIGH:
        await _alert(send, "fng", "high",
                     f"🤑 <b>Fear &amp; Greed: {v}</b> ({fmt.esc(fng['label'])}) — extreme greed zone",
                     config.COOLDOWN_FNG)


# --- volatility regime ---

def _returns(prices: list[float]) -> list[float]:
    return [math.log(b / a) for a, b in zip(prices, prices[1:]) if a > 0 and b > 0]


async def poll_volatility(market, send) -> None:
    now = int(time.time())
    day_ago = now - 86400
    for sym in db.watchlist():
        rows = db.fetchall(
            "SELECT ts, price FROM prices WHERE symbol=? AND ts > ? ORDER BY ts",
            (sym, now - 14 * 86400))
        recent = [r["price"] for r in rows if r["ts"] > day_ago]
        baseline = [r["price"] for r in rows if r["ts"] <= day_ago]
        if len(recent) < 30 or len(baseline) < 300:
            continue  # not enough history yet — needs a few days of uptime
        rv_recent = statistics.pstdev(_returns(recent))
        rv_base = statistics.pstdev(_returns(baseline))
        if rv_base > 0 and rv_recent / rv_base >= config.VOL_SPIKE_RATIO:
            await _alert(send, "vol", sym,
                         f"🌊 <b>Volatility spike</b> {fmt.esc(fmt.sym(sym))}: 24h realized vol "
                         f"is {rv_recent / rv_base:.1f}x the 14-day baseline — regime change, "
                         f"size accordingly", config.COOLDOWN_VOL)


# --- news + narrative heat ---

async def poll_news(market, send) -> None:
    new_items = await news.poll()
    for item in new_items:
        if item["severity"] == "high" and item["ts"] > time.time() - 6 * 3600:
            tags = ",".join(item["narratives"]) or "news"
            await _alert(send, "news", item["id"],
                         f"📰 <b>[{fmt.esc(tags)}]</b> {fmt.esc(item['title'])}\n"
                         f"{fmt.esc(item['source'])} — {item['url']}", 6 * 3600)
    for h in narrative.accelerating():
        await _alert(send, "heat", h["narrative"],
                     f"🔥 <b>Narrative heating: {fmt.esc(h['narrative'])}</b> — "
                     f"{h['count24h']} mentions in 24h vs {h['prior_daily_avg']}/day baseline "
                     f"({h['accel']}x)", config.COOLDOWN_HEAT)


# --- Binance listing announcements (minutes-sensitive) ---

async def poll_announcements(market, send) -> None:
    items = await announcements.fetch_latest()
    if not items:
        return
    first_run = db.fetchone("SELECT 1 FROM announcements LIMIT 1") is None
    for a in items:
        if db.fetchone("SELECT 1 FROM announcements WHERE id=?", (a["id"],)):
            continue
        db.execute("INSERT OR IGNORE INTO announcements(id, ts, title, url) VALUES (?,?,?,?)",
                   (a["id"], a["ts"] or int(time.time()), a["title"], a["url"]))
        if not first_run:  # seed silently on first run
            await send(f"🚨 <b>BINANCE LISTING ANNOUNCEMENT</b>\n{fmt.esc(a['title'])}\n{a['url']}")
    db.kv_set("last_poll_announcements", str(int(time.time())))


# --- new tradable perps via exchangeInfo diff ---

async def poll_new_symbols(market, send) -> None:
    symbols = await market.list_symbols("perp")
    if not symbols:
        return
    first_run = db.fetchone("SELECT 1 FROM known_symbols WHERE market='perp' LIMIT 1") is None
    now = int(time.time())
    for sym in symbols:
        if db.fetchone("SELECT 1 FROM known_symbols WHERE market='perp' AND symbol=?", (sym,)):
            continue
        db.execute("INSERT OR IGNORE INTO known_symbols(market, symbol, first_seen) "
                   "VALUES ('perp',?,?)", (sym, now))
        if not first_run and sym.endswith("/USDT:USDT"):
            await send(f"🆕 <b>New Binance perp live:</b> {fmt.esc(fmt.sym(sym))} — "
                       f"tradable right now")
    db.kv_set("last_poll_new_symbols", str(now))


# --- explosive-mover scanner (small/new-cap day-trading radar) ---

async def poll_scan(market, send) -> None:
    hits = await scanner.scan(market)
    now = int(time.time())
    # Replace the live board each cycle so the dashboard reflects the current scan,
    # not just the sparse cooldown-gated alert feed.
    db.kv_set("scan_live", json.dumps(scanner.live_payload(hits)))
    for r in hits:
        if not (r["verdict"] == "PASS" and r["push"]):
            continue
        key = f"{r['base']}:{r['direction']}"
        fresh = db.dedup_ok("scan", key, config.COOLDOWN_SCAN)
        await _alert(send, "scan", key,
                     scanner.format_hit(r) + "\n" + scanner.DISCLAIMER,
                     config.COOLDOWN_SCAN)
        if fresh:  # record only the fired signal, so scan_hits stays a clean feed
            db.execute(
                "INSERT INTO scan_hits(ts, market, symbol, tier, mom_5m, mom_15m, mom_1h, "
                "rvol, qvol, chg24, funding, score, verdict, is_new, url) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, r["market"], r["symbol"], r["tier"], r.get("mom_5m"), r.get("mom_15m"),
                 r.get("mom_1h"), r.get("rvol"), r.get("qvol"), r.get("chg24"),
                 r.get("funding"), r.get("score"), r["verdict"],
                 1 if r.get("is_new") else 0, r.get("url")))
    db.kv_set("last_poll_scan", str(now))


# --- open interest (positioning building) ---

async def poll_open_interest(market, send) -> None:
    now = int(time.time())
    for sym in db.watchlist():
        series = await market.open_interest_hist(sym, limit=12)
        if len(series) < 4:
            continue
        latest = series[-1]
        db.kv_set(f"oi:{sym}", f"{latest['oi_usd']:.0f}")
        base = statistics.median([s["oi"] for s in series[:-1]])
        if base > 0:
            surge = (latest["oi"] / base - 1) * 100
            if surge >= config.OI_SURGE_PCT:
                await _alert(send, "oi", sym,
                             f"📊 <b>Open interest surging</b> {fmt.esc(fmt.sym(sym))}: "
                             f"+{surge:.0f}% vs recent baseline (now "
                             f"{fmt.compact_usd(latest['oi_usd'])}). Positioning building fast — "
                             f"moves often follow.", config.COOLDOWN_OI)
    db.kv_set("last_poll_oi", str(now))


# --- long/short account ratio (crowd positioning) ---

async def poll_long_short(market, send) -> None:
    for sym in db.watchlist():
        ratio = await market.long_short_ratio(sym)
        if ratio is None:
            continue
        db.kv_set(f"ls:{sym}", f"{ratio:.3f}")
        if ratio >= config.LS_EXTREME:
            await _alert(send, "ls", f"{sym}:long",
                         f"⚖️ <b>Crowd heavily long</b> {fmt.esc(fmt.sym(sym))}: long/short "
                         f"account ratio {ratio:.2f}. One-sided — flushes start from here.",
                         config.COOLDOWN_LS)
        elif ratio <= config.LS_EXTREME_LOW:
            await _alert(send, "ls", f"{sym}:short",
                         f"⚖️ <b>Crowd heavily short</b> {fmt.esc(fmt.sym(sym))}: long/short "
                         f"account ratio {ratio:.2f}. One-sided the other way — squeeze fuel.",
                         config.COOLDOWN_LS)
    db.kv_set("last_poll_ls", str(int(time.time())))


# --- liquidation-cascade proxy (no public liq REST — inferred from price + OI) ---

async def poll_liquidations(market, send) -> None:
    now = int(time.time())
    for sym in db.watchlist():
        series = await market.open_interest_hist(sym, limit=4)
        if len(series) < 2 or not series[0]["oi"]:
            continue
        oi_drop = (series[-1]["oi"] / series[0]["oi"] - 1) * 100
        rows = db.fetchall(
            "SELECT price FROM prices WHERE symbol=? AND ts > ? ORDER BY ts",
            (sym, now - 20 * 60))
        if len(rows) < 2 or not rows[0]["price"]:
            continue
        move = (rows[-1]["price"] / rows[0]["price"] - 1) * 100
        if abs(move) >= config.LIQ_MOVE_PCT and oi_drop <= -config.LIQ_OI_DROP_PCT:
            who = "longs" if move < 0 else "shorts"
            await _alert(send, "liq", sym,
                         f"💥 <b>Likely liquidation cascade</b> {fmt.esc(fmt.sym(sym))}: "
                         f"{fmt.pct(move)} with open interest {fmt.pct(oi_drop)} — {who} getting "
                         f"flushed.\n<i>Inferred from price + OI, not a raw liquidation feed.</i>",
                         config.COOLDOWN_LIQ)
    db.kv_set("last_poll_liq", str(now))


# --- named conditions: record transitions, alert on the notable ones ---

async def poll_conditions(market, send) -> None:
    for name in read.record_fires():
        c = next((x for x in read._evaluate() if x["name"] == name), None)
        if not c:
            continue
        await _alert(send, "condition", name,
                     f"🔔 <b>Condition now true: {fmt.esc(name)}</b>\n"
                     f"{fmt.esc(c['meaning'])}\n"
                     f"<i>Unvalidated pattern — no backtest stands behind this. "
                     f"It describes positioning, not what to do.</i>",
                     12 * 3600)


# --- open positions: mark-to-market, funding accrual, forced liquidation ---

async def poll_positions(market, send) -> None:
    held = {p["symbol"] for p in paper.positions(0)} | {p["symbol"] for p in paper.positions(1)}
    if not held:
        return
    marks = await market.marks(held)
    if not marks:
        return
    rates = await market.funding_rates()
    for is_real in (0, 1):
        for liq in paper.mark_manage(marks, rates, is_real):
            tag = "REAL" if is_real else "Paper"
            await send(f"💥 <b>{tag} LIQUIDATED</b>: {fmt.esc(fmt.sym(liq['symbol']))} "
                       f"{liq['side']} @ {fmt.price(liq['exit'])}\n"
                       f"margin ${abs(liq['realized']):,.2f} wiped — position gone.")
    db.kv_set("last_poll_positions", str(int(time.time())))


# --- equity snapshots (hourly, so the dashboard curve has shape) ---

async def snapshot_equity(market, send) -> None:
    marks = await market.watchlist_prices()
    btc = marks.get("BTC/USDT:USDT") or await market.last_price("BTC/USDT:USDT")
    paper.ensure_init(btc)
    held = {p["symbol"] for p in paper.positions(0)} - set(marks)
    if held:
        marks.update(await market.marks(held))
    paper.snapshot_equity(marks, btc)


# --- daily housekeeping ---

async def daily_housekeeping(market, send) -> None:
    await snapshot_equity(market, send)
    db.prune()
