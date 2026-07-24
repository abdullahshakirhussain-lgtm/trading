"""LLM co-pilot: daily brief, thesis devil's-advocate, weekly journal review.

Uses Claude Haiku (user's cost constraint: ~cents/day). Cleanly disabled when
no ANTHROPIC_API_KEY is set — everything else in the app works without it.

Guardrail baked into every prompt: the model describes data and argues risk,
it never issues buy/sell instructions. Decisions are the user's.
"""
import logging
import time

from .. import config, db, fmt
from ..engine import narrative, paper

log = logging.getLogger(__name__)

_client = None

SYSTEM = (
    "You are the analysis module of a personal crypto market dashboard. "
    "You are talking to its single user, an occasional retail trader with tiny capital. "
    "Rules you must follow:\n"
    "1. NEVER tell the user to buy, sell, or hold anything. No trade instructions, "
    "no price predictions stated as fact. Describe data, regimes, and risks.\n"
    "2. Be blunt and specific. No hedging filler, no disclaimers longer than one line.\n"
    "3. Keep responses under 250 words — this is a Telegram message.\n"
    "4. Plain text only: no markdown, no headers, no bullet symbols other than '-'."
)


def enabled() -> bool:
    return bool(config.ANTHROPIC_API_KEY)


def _get_client():
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic
        _client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


async def _ask(prompt: str, max_tokens: int = 700) -> str:
    import anthropic
    try:
        response = await _get_client().messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        return next((b.text for b in response.content if b.type == "text"), "").strip()
    except anthropic.RateLimitError:
        return "LLM rate-limited right now — try again in a minute."
    except anthropic.APIStatusError as e:
        log.warning("LLM call failed: %s %s", e.status_code, e.message)
        return f"LLM error ({e.status_code}) — check logs."
    except anthropic.APIConnectionError:
        return "LLM unreachable — network issue."


# --- context assembly (all from local data, cheap) ---

async def _market_context(market) -> str:
    lines = ["(USD-M perpetual futures)"]
    prices = await market.watchlist_prices()
    for sym, px in sorted(prices.items()):
        t = await market.ticker(sym)
        pct = f" ({t['pct24h']:+.1f}% 24h)" if t and t.get("pct24h") is not None else ""
        ls = db.kv_get(f"ls:{sym}")
        ls_s = f" [L/S {float(ls):.2f}]" if ls else ""
        lines.append(f"{fmt.sym(sym)}: {px}{pct}{ls_s}")
    rates = await market.funding_rates()
    watch_bases = [w.split("/")[0] for w in db.watchlist()]
    for s, r in sorted(rates.items()):
        if any(s.startswith(b + "/") for b in watch_bases):
            lines.append(f"funding {fmt.sym(s)}: {r * 100:+.4f}%/8h")
    fng_v, fng_l = db.kv_get("fng_value"), db.kv_get("fng_label")
    if fng_v:
        lines.append(f"Fear&Greed: {fng_v} ({fng_l})")
    heat = [h for h in narrative.heat() if h["count24h"] > 0][:6]
    if heat:
        lines.append("narrative heat (24h count, accel vs 7d avg): " +
                     ", ".join(f"{h['narrative']}={h['count24h']} ({h['accel']}x)" for h in heat))
    headlines = db.fetchall(
        "SELECT title, severity FROM news WHERE ts > ? ORDER BY severity DESC, ts DESC LIMIT 10",
        (int(time.time()) - 24 * 3600,))
    for h in headlines:
        lines.append(f"headline[{h['severity']}]: {h['title']}")
    return "\n".join(lines) or "no market data collected yet"


def _journal_context(days: int = 14) -> str:
    cutoff = int(time.time()) - days * 86400
    rows = db.fetchall(
        "SELECT ts, symbol, side, action, bucket, margin, leverage, fee, funding, "
        "is_real, realized_pnl FROM fut_trades WHERE ts > ? ORDER BY ts", (cutoff,))
    if not rows:
        return "no trades in the journal yet"
    lines = []
    for r in rows:
        kind = "REAL" if r["is_real"] else "paper"
        pnl = f" pnl={r['realized_pnl']:+.2f}" if r["realized_pnl"] is not None else ""
        fund = f" funding={r['funding']:+.2f}" if r["funding"] else ""
        day = time.strftime("%a %d %b %H:%M", time.localtime(r["ts"]))
        lines.append(f"{day} {kind} {r['action']} {r['side']} {fmt.sym(r['symbol'])} "
                     f"margin ${r['margin']:.0f} {r['leverage']:.0f}x [{r['bucket']}] "
                     f"fee={r['fee']:.2f}{fund}{pnl}")
    return "\n".join(lines)


# --- public features ---

async def daily_brief(market) -> str:
    ctx = await _market_context(market)
    text = await _ask(
        "Write today's market brief from this data snapshot. Cover: overall regime "
        "(risk-on/off and why), anything unusual in funding or volatility, which "
        "narratives are accelerating and what that historically precedes, and one "
        "thing worth watching today. Data:\n\n" + ctx,
        max_tokens=800)
    return f"☀️ <b>Daily brief</b>\n{fmt.esc(text)}"


async def check_thesis(market, thesis: str) -> str:
    ctx = await _market_context(market)
    text = await _ask(
        "The user is considering a trade and wants you to play devil's advocate. "
        "Argue AGAINST their thesis using the data below: what contradicts it, what "
        "risk they're ignoring, what the crowd positioning (funding) implies, and "
        "what would have to be true for the thesis to work. End with the single "
        "strongest counterargument. Do not tell them whether to do the trade.\n\n"
        f"Their thesis: {thesis}\n\nCurrent data:\n{ctx}",
        max_tokens=700)
    return f"😈 <b>Devil's advocate</b>\n{fmt.esc(text)}"


async def weekly_review() -> str:
    journal = _journal_context()
    prices_note = ""
    stats_paper = None
    try:
        stats_paper = paper.stats({}, 0)
    except Exception:
        pass
    if stats_paper:
        b = stats_paper["buckets"]
        prices_note = (f"\ncore: {b['core']['trades']} trades, realized {b['core']['realized']:.2f}, "
                       f"fees {b['core']['fees']:.2f}. degen: {b['degen']['trades']} trades, "
                       f"realized {b['degen']['realized']:.2f}, fees {b['degen']['fees']:.2f}.")
    text = await _ask(
        "Review this trading journal for behavioral patterns. Look for: overtrading, "
        "revenge trading (buys shortly after losses), cutting winners early vs letting "
        "losers run, fee bleed relative to position sizes, degen bucket discipline, and "
        "day/time patterns. Be specific — cite the trades. If the journal is too thin "
        "to conclude anything, say exactly that and what sample size would help.\n\n"
        f"Journal (last 14 days):\n{journal}{prices_note}",
        max_tokens=800)
    return f"📋 <b>Weekly review</b>\n{fmt.esc(text)}"
