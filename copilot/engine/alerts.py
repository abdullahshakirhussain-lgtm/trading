"""Periodic jobs: poll data, detect conditions, push Telegram alerts.

Every job takes (market, send) — `send` is an async callable(text) wired to
Telegram by the bot layer, or print() in selftest mode. All alerts dedup
through db.dedup_ok so a persistent condition doesn't spam.
"""
import logging
import math
import statistics
import time

from .. import config, db, fmt
from ..data import announcements, dexscreener, feargreed, news
from . import narrative, paper, rugfilter

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
            await send(f"🎯 <b>{fmt.esc(row['symbol'])}</b> crossed {row['direction']} "
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
                     f"⚡ <b>Funding extreme</b> {fmt.esc(sym)}: {fmt.funding_pct(rate)}/8h — {side}",
                     config.COOLDOWN_FUNDING)


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
                         f"📈 <b>Watchlist move</b> {fmt.esc(r['symbol'])} {fmt.pct(p)} in 24h "
                         f"(vol {fmt.compact_usd(r['qvol'])})", config.COOLDOWN_MOVER)
        elif abs(p) >= config.MARKET_MOVER_PCT:
            await _alert(send, "mkt_move", f"{r['symbol']}:{'up' if p > 0 else 'down'}",
                         f"🚀 <b>Big mover</b> {fmt.esc(r['symbol'])} {fmt.pct(p)} in 24h "
                         f"(vol {fmt.compact_usd(r['qvol'])})", config.COOLDOWN_MOVER * 2)


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
                         f"🌊 <b>Volatility spike</b> {fmt.esc(sym)}: 24h realized vol is "
                         f"{rv_recent / rv_base:.1f}x the 14-day baseline — regime change, "
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


# --- new tradable symbols via exchangeInfo diff (reliable fallback) ---

async def poll_new_symbols(market, send) -> None:
    for mkt in ("spot", "perp"):
        symbols = await market.list_symbols(mkt)
        if not symbols:
            continue
        first_run = db.fetchone("SELECT 1 FROM known_symbols WHERE market=? LIMIT 1",
                                (mkt,)) is None
        now = int(time.time())
        for sym in symbols:
            if db.fetchone("SELECT 1 FROM known_symbols WHERE market=? AND symbol=?", (mkt, sym)):
                continue
            db.execute("INSERT OR IGNORE INTO known_symbols(market, symbol, first_seen) "
                       "VALUES (?,?,?)", (mkt, sym, now))
            if not first_run and sym.endswith(("/USDT", "/USDT:USDT")):
                label = "spot pair" if mkt == "spot" else "PERP"
                await send(f"🆕 <b>New Binance {label} live:</b> {fmt.esc(sym)} — "
                           f"tradable right now")
    db.kv_set("last_poll_new_symbols", str(int(time.time())))


# --- memecoin radar ---

async def poll_radar(market, send) -> None:
    candidates = await dexscreener.trending_candidates(config.RADAR_CHAINS)
    by_chain: dict[str, list[str]] = {}
    for c in candidates:
        by_chain.setdefault(c["chain"], []).append(c["addr"])
    now = int(time.time())
    for chain, addrs in by_chain.items():
        for p in await dexscreener.pair_stats(chain, addrs):
            verdict, reasons = rugfilter.evaluate(p)
            existing = db.fetchone("SELECT verdict FROM radar WHERE chain=? AND token_addr=?",
                                   (chain, p["addr"]))
            db.execute(
                "INSERT INTO radar(chain, token_addr, first_seen, last_seen, symbol, name, "
                "verdict, reasons, liq_usd, vol24, age_h, price_usd, url) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(chain, token_addr) DO UPDATE SET last_seen=excluded.last_seen, "
                "verdict=excluded.verdict, reasons=excluded.reasons, liq_usd=excluded.liq_usd, "
                "vol24=excluded.vol24, age_h=excluded.age_h, price_usd=excluded.price_usd",
                (chain, p["addr"], now, now, p["symbol"], p["name"], verdict,
                 "; ".join(reasons), p["liq_usd"], p["vol24"], p["age_h"], p["price_usd"],
                 p["url"]))
            if verdict == "PASS" and (existing is None or existing["verdict"] != "PASS"):
                await _alert(send, "radar", f"{chain}:{p['addr']}",
                             f"👀 <b>Radar: {fmt.esc(p['symbol'])}</b> ({fmt.esc(chain)}) "
                             f"passed rug filters\n"
                             f"liq {fmt.compact_usd(p['liq_usd'])} · vol24 "
                             f"{fmt.compact_usd(p['vol24'])} · age {p['age_h']:.0f}h\n"
                             f"{p['url']}\n"
                             f"<i>Data, not a recommendation — most memes still go to zero.</i>",
                             24 * 3600)
    db.kv_set("last_poll_radar", str(int(time.time())))


# --- equity snapshots (hourly, so the dashboard curve has shape) ---

async def snapshot_equity(market, send) -> None:
    prices = await market.watchlist_prices()
    btc = prices.get("BTC/USDT") or await market.last_price("BTC/USDT")
    paper.ensure_init(btc)
    # include held symbols outside the watchlist so equity is valued correctly
    held = {p["symbol"] for p in paper.positions(0)} - set(prices)
    for sym in held:
        px = await market.last_price(sym)
        if px:
            prices[sym] = px
    paper.snapshot_equity(prices, btc)


# --- daily housekeeping ---

async def daily_housekeeping(market, send) -> None:
    await snapshot_equity(market, send)
    db.prune()
