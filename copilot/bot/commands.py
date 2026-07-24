"""Telegram command handlers. The bot IS the UI — no web frontend."""
import functools
import logging
import time

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from .. import config, db, fmt
from ..data import feargreed
from ..data.binance import norm_symbol
from ..engine import narrative, paper, read, scanner

log = logging.getLogger(__name__)

HELP = """<b>crypto co-pilot</b> — alerts &amp; analysis only, never execution.

<b>Market</b>
/price SYM — price + 24h stats
/funding [SYM] — perp funding (watchlist by default)
/movers — top movers (volume-floored)
/fng — Fear &amp; Greed index

<b>Watchlist &amp; alerts</b>
/watch SYM · /unwatch SYM · /watchlist
/alert SYM above|below PRICE · /alerts · /delalert ID

<b>Read the market</b>
/read — what funding, sentiment &amp; vol currently imply

<b>Radar &amp; news</b>
/hot [micro|small|mid] — small/new-cap coins moving right now
/radar — memecoin radar with rug-filter verdicts
/news [N] — latest headlines
/heat — narrative heat (what's accelerating)

<b>Paper trading</b> (fake $10k — the track record that matters)
/buy SYM USD [degen] [real]
/sell SYM [USD] [real] · /close SYM [real]
/positions · /stats [real]

<b>LLM co-pilot</b>
/brief — market brief now
/check LONG SOL because ... — devil's advocate on your thesis
/review — trading-pattern review

/status — system health"""


def handler(fn):
    """Wrap a command: log, catch errors, reply politely."""
    @functools.wraps(fn)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            await fn(update, context)
        except paper.TradeError as e:
            await update.message.reply_text(f"⚠️ {e}")
        except Exception as e:
            log.exception("command %s failed", fn.__name__)
            await update.message.reply_text(f"❌ Error: {e}")
    return wrapped


def _market(context):
    return context.application.bot_data["market"]


async def _reply(update: Update, text: str) -> None:
    await update.message.reply_text(text, parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True)


async def _prices_for_positions(market) -> dict[str, float]:
    """Live prices for watchlist + any symbol we hold a position in + BTC."""
    symbols = set(db.watchlist()) | {"BTC/USDT"}
    for p in paper.positions(0) + paper.positions(1):
        symbols.add(p["symbol"])
    out = {}
    try:
        tickers = await market.spot.fetch_tickers(list(symbols))
        out = {s: t.get("last") for s, t in tickers.items() if t.get("last")}
    except Exception as e:
        log.warning("prices_for_positions: %s", e)
    return out


# --- basic ---

@handler
async def start(update: Update, context) -> None:
    db.kv_set("alert_chat_id", str(update.effective_chat.id))
    await _reply(update, "✅ Connected. Alerts will arrive in this chat.\n\n" + HELP)


@handler
async def help_cmd(update: Update, context) -> None:
    await _reply(update, HELP)


@handler
async def price(update: Update, context) -> None:
    if not context.args:
        await _reply(update, "Usage: /price BTC")
        return
    sym = norm_symbol(context.args[0])
    t = await _market(context).ticker(sym)
    if not t:
        await _reply(update, f"Couldn't fetch {fmt.esc(sym)} — is the symbol right?")
        return
    await _reply(update,
                 f"<b>{fmt.esc(sym)}</b>  {fmt.price(t['price'])}\n"
                 f"24h: {fmt.pct(t['pct24h'])} · H {fmt.price(t['high'])} · "
                 f"L {fmt.price(t['low'])} · vol {fmt.compact_usd(t['qvol'])}")


@handler
async def funding(update: Update, context) -> None:
    rates = await _market(context).funding_rates()
    if not rates:
        await _reply(update, "Funding data unavailable right now.")
        return
    if context.args:
        base = norm_symbol(context.args[0]).split("/")[0]
        lines = [f"{fmt.esc(s)}: {fmt.funding_pct(r)}/8h"
                 for s, r in rates.items() if s.startswith(base + "/")]
        await _reply(update, "\n".join(lines) or f"No perp found for {fmt.esc(base)}.")
        return
    watch_bases = [w.split("/")[0] for w in db.watchlist()]
    lines = ["<b>Funding (watchlist)</b>"]
    for s, r in sorted(rates.items()):
        if any(s.startswith(b + "/") for b in watch_bases):
            note = " ⚠️ crowded" if abs(r) >= config.FUNDING_EXTREME else ""
            lines.append(f"{fmt.esc(s)}: {fmt.funding_pct(r)}/8h{note}")
    await _reply(update, "\n".join(lines))


@handler
async def movers(update: Update, context) -> None:
    rows = await _market(context).movers(config.MOVER_MIN_QVOL)
    if not rows:
        await _reply(update, "No mover data right now.")
        return
    lines = ["<b>Top gainers (24h, vol ≥ $5M)</b>"]
    lines += [f"{fmt.esc(r['symbol'])}  {fmt.pct(r['pct24h'])}  ({fmt.compact_usd(r['qvol'])})"
              for r in rows[:8]]
    lines.append("\n<b>Top losers</b>")
    lines += [f"{fmt.esc(r['symbol'])}  {fmt.pct(r['pct24h'])}  ({fmt.compact_usd(r['qvol'])})"
              for r in rows[-8:][::-1]]
    await _reply(update, "\n".join(lines))


@handler
async def fng(update: Update, context) -> None:
    data = await feargreed.fetch()
    if not data:
        await _reply(update, "Fear & Greed unavailable right now.")
        return
    await _reply(update, f"<b>Fear &amp; Greed: {data['value']}</b> — {fmt.esc(data['label'])}")


# --- watchlist & price alerts ---

@handler
async def watch(update: Update, context) -> None:
    if not context.args:
        await _reply(update, "Usage: /watch PEPE")
        return
    sym = norm_symbol(context.args[0])
    t = await _market(context).ticker(sym)
    if not t:
        await _reply(update, f"{fmt.esc(sym)} not found on Binance spot.")
        return
    db.execute("INSERT OR IGNORE INTO watchlist(symbol) VALUES (?)", (sym,))
    await _reply(update, f"👁 Watching {fmt.esc(sym)} (now {fmt.price(t['price'])})")


@handler
async def unwatch(update: Update, context) -> None:
    if not context.args:
        await _reply(update, "Usage: /unwatch PEPE")
        return
    sym = norm_symbol(context.args[0])
    db.execute("DELETE FROM watchlist WHERE symbol = ?", (sym,))
    await _reply(update, f"Stopped watching {fmt.esc(sym)}.")


@handler
async def watchlist_cmd(update: Update, context) -> None:
    syms = db.watchlist()
    prices = await _market(context).watchlist_prices()
    lines = [f"{fmt.esc(s)}  {fmt.price(prices.get(s))}" for s in syms]
    await _reply(update, "<b>Watchlist</b>\n" + ("\n".join(lines) or "empty"))


@handler
async def alert(update: Update, context) -> None:
    if len(context.args) != 3 or context.args[1].lower() not in ("above", "below"):
        await _reply(update, "Usage: /alert BTC above 65000")
        return
    sym = norm_symbol(context.args[0])
    direction = context.args[1].lower()
    level = float(context.args[2].replace(",", ""))
    db.execute("INSERT INTO price_alerts(symbol, direction, level, created_ts) VALUES (?,?,?,?)",
               (sym, direction, level, int(time.time())))
    await _reply(update, f"🎯 Alert set: {fmt.esc(sym)} {direction} {fmt.price(level)}")


@handler
async def alerts_cmd(update: Update, context) -> None:
    rows = db.fetchall("SELECT * FROM price_alerts WHERE fired_ts IS NULL ORDER BY id")
    lines = [f"#{r['id']}  {fmt.esc(r['symbol'])} {r['direction']} {fmt.price(r['level'])}"
             for r in rows]
    await _reply(update, "<b>Active price alerts</b>\n" + ("\n".join(lines) or "none"))


@handler
async def delalert(update: Update, context) -> None:
    if not context.args:
        await _reply(update, "Usage: /delalert 3")
        return
    db.execute("DELETE FROM price_alerts WHERE id = ?", (int(context.args[0]),))
    await _reply(update, "Deleted.")


# --- radar / news / heat ---

@handler
async def radar(update: Update, context) -> None:
    rows = db.fetchall(
        "SELECT * FROM radar WHERE last_seen > ? "
        "ORDER BY (verdict = 'PASS') DESC, liq_usd DESC LIMIT 12",
        (int(time.time()) - 24 * 3600,))
    if not rows:
        await _reply(update, "Radar is empty — no trending candidates in the last 24h "
                             "(or the radar job hasn't run yet).")
        return
    lines = ["<b>Memecoin radar (24h)</b> — data, not recommendations"]
    for r in rows:
        mark = "✅" if r["verdict"] == "PASS" else "⚠️"
        age = f"{r['age_h']:.0f}h" if r["age_h"] is not None else "?"
        lines.append(
            f"\n{mark} <b>{fmt.esc(r['symbol'])}</b> ({fmt.esc(r['chain'])}) · "
            f"liq {fmt.compact_usd(r['liq_usd'])} · vol {fmt.compact_usd(r['vol24'])} · age {age}")
        if r["verdict"] == "SKIP":
            lines.append(f"   ↳ {fmt.esc(r['reasons'])}")
        else:
            lines.append(f"   {r['url']}")
    await _reply(update, "\n".join(lines))


@handler
async def news_cmd(update: Update, context) -> None:
    n = int(context.args[0]) if context.args else 8
    rows = db.fetchall("SELECT * FROM news ORDER BY ts DESC LIMIT ?", (min(n, 20),))
    if not rows:
        await _reply(update, "No news ingested yet — give the news job a few minutes.")
        return
    lines = []
    for r in rows:
        tag = f" <i>[{fmt.esc(r['narratives'])}]</i>" if r["narratives"] else ""
        lines.append(f"• {fmt.esc(r['title'])}{tag}\n  {fmt.esc(r['source'])} — {r['url']}")
    await _reply(update, "<b>Latest headlines</b>\n" + "\n".join(lines))


@handler
async def read_cmd(update: Update, context) -> None:
    """Plain-English reading of the current regime + which conditions are true."""
    lines = ["<b>Market read</b>", fmt.esc(read.summary()), ""]
    for r in read.readings():
        lines.append(f"<b>{fmt.esc(r['metric'])}</b>: {fmt.esc(r['value'])}\n"
                     f"{fmt.esc(r['text'])}")
    data = read.conditions()
    active = [c for c in data["conditions"] if c["active"]]
    if active:
        lines.append("\n<b>Conditions currently true</b>")
        for c in active:
            fires = c["fires"]
            lines.append(f"🔔 {fmt.esc(c['name'])} — fired {fires}x in "
                         f"{data['tracking_days']:.0f} days of tracking")
    lines.append("\n<i>Descriptive only. None of these are backtested, and none "
                 "of them tell you to buy or sell.</i>")
    await _reply(update, "\n".join(lines))


@handler
async def hot(update: Update, context) -> None:
    """Live scan for small/new-cap coins igniting right now (spot + perps)."""
    tier_filter = None
    if context.args and context.args[0].lower() in ("micro", "small", "mid"):
        tier_filter = context.args[0].lower()
    await update.message.reply_text("🔎 Scanning spot + perps for movers…")
    hits = await scanner.scan(_market(context))
    passed = [h for h in hits if h["verdict"] == "PASS"]
    if tier_filter:
        passed = [h for h in passed if h["tier"] == tier_filter]
    if not passed:
        where = f" in the {tier_filter} tier" if tier_filter else ""
        await _reply(update, f"Nothing igniting right now{where}. Either the market's "
                     "quiet or nothing clears the thresholds — try again shortly. "
                     "(A fresh start needs a few minutes of snapshots before "
                     "short-window momentum kicks in.)")
        return
    header = "<b>🔥 Hot movers</b>" + (f" · {fmt.esc(tier_filter)} tier" if tier_filter else "")
    blocks = [scanner.format_hit(h) for h in passed[:10]]
    await _reply(update, header + "\n\n" + "\n\n".join(blocks) + "\n\n" + scanner.DISCLAIMER)


@handler
async def heat(update: Update, context) -> None:
    rows = narrative.heat()
    active = [h for h in rows if h["count24h"] > 0]
    if not active:
        await _reply(update, "No narrative data yet — news ingest needs a little time.")
        return
    lines = ["<b>Narrative heat</b> (24h count · vs prior 7d avg)"]
    for h in active[:10]:
        flame = " 🔥" if h["accel"] >= config.HEAT_ACCEL_RATIO and \
                        h["count24h"] >= config.HEAT_MIN_COUNT else ""
        lines.append(f"{fmt.esc(h['narrative'])}: {h['count24h']} "
                     f"(avg {h['prior_daily_avg']}/day, {h['accel']}x){flame}")
    await _reply(update, "\n".join(lines))


# --- paper trading ---

def _parse_flags(args: list[str]) -> tuple[list[str], str, int]:
    """Split positional args from 'degen'/'real' flags."""
    positional, bucket, is_real = [], "core", 0
    for a in args:
        la = a.lower()
        if la == "degen":
            bucket = "degen"
        elif la == "real":
            is_real = 1
        else:
            positional.append(a)
    return positional, bucket, is_real


@handler
async def buy(update: Update, context) -> None:
    positional, bucket, is_real = _parse_flags(context.args or [])
    if len(positional) != 2:
        await _reply(update, "Usage: /buy PEPE 200 [degen] [real]")
        return
    sym = norm_symbol(positional[0])
    usd = float(positional[1])
    market = _market(context)
    px = await market.last_price(sym)
    if not px:
        await _reply(update, f"No price for {fmt.esc(sym)} — Binance spot symbol needed.")
        return
    prices = await _prices_for_positions(market)
    paper.ensure_init(prices.get("BTC/USDT"))
    equity_now = paper.equity(prices)
    res = paper.buy(sym, usd, px, bucket, is_real, equity_now)
    kind = "REAL trade logged" if is_real else "Paper buy"
    await _reply(update,
                 f"🟢 <b>{kind}</b>: {res['qty']:.6g} {fmt.esc(sym)} @ {fmt.price(res['fill'])} "
                 f"[{res['bucket']}]\nfee {fmt.money(res['fee'])} · "
                 f"paper cash {fmt.money(paper.cash())}")


@handler
async def sell(update: Update, context) -> None:
    positional, _, is_real = _parse_flags(context.args or [])
    if not positional:
        await _reply(update, "Usage: /sell PEPE [200] [real]")
        return
    sym = norm_symbol(positional[0])
    usd = float(positional[1]) if len(positional) > 1 else None
    px = await _market(context).last_price(sym)
    if not px:
        await _reply(update, f"No price for {fmt.esc(sym)}.")
        return
    res = paper.sell(sym, px, usd, is_real=is_real)
    closed = " — position closed" if res["closed"] else ""
    await _reply(update,
                 f"🔴 <b>{'REAL' if is_real else 'Paper'} sell</b>: {res['qty']:.6g} "
                 f"{fmt.esc(sym)} @ {fmt.price(res['fill'])} [{res['bucket']}]{closed}\n"
                 f"realized P&amp;L {fmt.money(res['realized'])} · fee {fmt.money(res['fee'])}")


@handler
async def close(update: Update, context) -> None:
    positional, _, is_real = _parse_flags(context.args or [])
    if not positional:
        await _reply(update, "Usage: /close PEPE [real]")
        return
    context.args = positional[:1] + (["real"] if is_real else [])
    await sell.__wrapped__(update, context)


@handler
async def positions_cmd(update: Update, context) -> None:
    prices = await _prices_for_positions(_market(context))
    lines = []
    for is_real, label in ((0, "Paper"), (1, "Real")):
        pos = paper.positions(is_real)
        if not pos:
            continue
        lines.append(f"<b>{label} positions</b>")
        for p in pos:
            px = prices.get(p["symbol"])
            upnl = (px - p["avg_cost"]) * p["qty"] if px else None
            upnl_s = f" · uP&amp;L {fmt.money(upnl)}" if upnl is not None else ""
            lines.append(f"[{p['bucket']}] {fmt.esc(p['symbol'])} {p['qty']:.6g} "
                         f"@ {fmt.price(p['avg_cost'])}{upnl_s}")
    if not lines:
        lines = ["No open positions. /buy BTC 500 to start the paper record."]
    else:
        lines.append(f"\nPaper cash: {fmt.money(paper.cash())}")
    await _reply(update, "\n".join(lines))


@handler
async def stats(update: Update, context) -> None:
    _, _, is_real = _parse_flags(context.args or [])
    prices = await _prices_for_positions(_market(context))
    paper.ensure_init(prices.get("BTC/USDT"))
    s = paper.stats(prices, is_real)
    lines = [f"<b>{'Real journal' if is_real else 'Paper portfolio'} stats</b>"]
    if not is_real:
        lines.append(f"Equity {fmt.money(s['equity'])} ({fmt.pct(s['return_pct'], 2)}) · "
                     f"cash {fmt.money(s['cash'])} · day {s['days']:.0f}")
        if "btc_bench_pct" in s:
            beat = "beating" if s["return_pct"] > s["btc_bench_pct"] else "LOSING to"
            lines.append(f"Hold-BTC benchmark: {fmt.pct(s['btc_bench_pct'], 2)} — "
                         f"you're {beat} it")
        if s.get("max_drawdown_pct") is not None:
            lines.append(f"Max drawdown: {s['max_drawdown_pct']:.1f}%")
    for b, d in s["buckets"].items():
        wr = f"{d['win_rate']:.0f}%" if d["win_rate"] is not None else "—"
        lines.append(f"\n<b>[{b}]</b> {d['trades']} trades · {d['closed']} closed · win {wr}\n"
                     f"realized {fmt.money(d['realized'])} · unrealized "
                     f"{fmt.money(d['unrealized'])} · fees {fmt.money(d['fees'])}")
    await _reply(update, "\n".join(lines))


# --- LLM co-pilot ---

@handler
async def brief(update: Update, context) -> None:
    from ..llm import copilot
    if not copilot.enabled():
        await _reply(update, "LLM module is off — add ANTHROPIC_API_KEY to .env to enable.")
        return
    await update.message.reply_text("🧠 Building brief…")
    text = await copilot.daily_brief(_market(context))
    await _reply(update, text)


@handler
async def check(update: Update, context) -> None:
    from ..llm import copilot
    if not copilot.enabled():
        await _reply(update, "LLM module is off — add ANTHROPIC_API_KEY to .env to enable.")
        return
    thesis = " ".join(context.args or [])
    if len(thesis) < 10:
        await _reply(update, "Give me the thesis: /check long SOL because ETF rumours")
        return
    await update.message.reply_text("🧠 Stress-testing your thesis…")
    text = await copilot.check_thesis(_market(context), thesis)
    await _reply(update, text)


@handler
async def review(update: Update, context) -> None:
    from ..llm import copilot
    if not copilot.enabled():
        await _reply(update, "LLM module is off — add ANTHROPIC_API_KEY to .env to enable.")
        return
    await update.message.reply_text("🧠 Reviewing your trading patterns…")
    text = await copilot.weekly_review()
    await _reply(update, text)


# --- status ---

@handler
async def status(update: Update, context) -> None:
    now = int(time.time())

    def ago(key):
        v = db.kv_get(key)
        return f"{(now - int(v)) // 60}m ago" if v else "never"

    from ..llm import copilot
    n_news = db.fetchone("SELECT COUNT(*) AS n FROM news")["n"]
    n_radar = db.fetchone("SELECT COUNT(*) AS n FROM radar")["n"]
    await _reply(update, "\n".join([
        "<b>Status</b>",
        f"prices: {ago('last_poll_prices')} · funding: {ago('last_poll_funding')}",
        f"news: {ago('last_poll_news')} ({n_news} stored) · radar: "
        f"{ago('last_poll_radar')} ({n_radar} tracked)",
        f"listings: {ago('last_poll_announcements')} · new-symbols: "
        f"{ago('last_poll_new_symbols')}",
        f"LLM: {'on (' + config.ANTHROPIC_MODEL + ')' if copilot.enabled() else 'off'}",
        f"F&amp;G: {db.kv_get('fng_value', '?')} ({fmt.esc(db.kv_get('fng_label', '?'))})",
    ]))
