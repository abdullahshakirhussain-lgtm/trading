"""Turn raw metrics into plain-English readings and named condition flags.

Deliberate boundary: this module describes what the data *implies about crowd
positioning and risk*. It never says buy, sell, or hold. Nothing here has been
backtested — every condition ships with an UNVALIDATED label and its own fire
count so the user can see exactly how little evidence stands behind it.

The fire counts are the point. After a few months they turn "this setup feels
meaningful" into a number you can check against the paper record.
"""
import math
import statistics
import time

from .. import config, db
from . import narrative

# --- individual readings -------------------------------------------------

def _avg_watchlist_funding() -> float | None:
    rates = []
    for sym in db.watchlist():
        base = sym.split("/")[0]
        row = db.fetchone(
            "SELECT rate FROM funding WHERE symbol LIKE ? AND ts > ? "
            "ORDER BY ts DESC LIMIT 1", (base + "/%", int(time.time()) - 3 * 3600))
        if row:
            rates.append(row["rate"])
    return sum(rates) / len(rates) if rates else None


def _btc_vol_ratio() -> float | None:
    """24h realized vol vs the 14-day baseline. Needs a few days of uptime."""
    now = int(time.time())
    rows = db.fetchall(
        "SELECT ts, price FROM prices WHERE symbol='BTC/USDT' AND ts > ? ORDER BY ts",
        (now - 14 * 86400,))
    recent = [r["price"] for r in rows if r["ts"] > now - 86400]
    base = [r["price"] for r in rows if r["ts"] <= now - 86400]
    if len(recent) < 30 or len(base) < 300:
        return None
    rets = lambda p: [math.log(b / a) for a, b in zip(p, p[1:]) if a > 0 and b > 0]
    rv_r, rv_b = statistics.pstdev(rets(recent)), statistics.pstdev(rets(base))
    return (rv_r / rv_b) if rv_b > 0 else None


def readings() -> list[dict]:
    """[{metric, value, text}] — what each number means, in words."""
    out = []

    f = _avg_watchlist_funding()
    if f is None:
        out.append({"metric": "Funding", "value": "—",
                    "text": "No funding data yet."})
    else:
        pct = f * 100
        if f > config.FUNDING_EXTREME:
            t = ("Longs are paying shorts heavily — the crowd is leveraged long. "
                 "Crowded positioning is what makes sharp downside moves violent, "
                 "because longs get liquidated into each other.")
        elif f < -config.FUNDING_EXTREME:
            t = ("Shorts are paying longs heavily — the crowd is leveraged short. "
                 "Crowded shorts are the fuel for squeezes.")
        elif f > 0:
            t = ("Longs pay shorts, mildly. Normal in an uptrend; positioning is "
                 "not stretched.")
        else:
            t = ("Shorts pay longs, mildly. Slight bearish lean, not stretched.")
        out.append({"metric": "Funding", "value": f"{pct:+.4f}%/8h", "text": t})

    v = db.kv_get("fng_value")
    if not v:
        out.append({"metric": "Fear & Greed", "value": "—",
                    "text": "Index unavailable."})
    else:
        n = int(v)
        if n <= config.FNG_LOW:
            t = ("Extreme fear. Sentiment is washed out — historically where "
                 "sellers are most exhausted, but fear can persist for weeks.")
        elif n >= config.FNG_HIGH:
            t = ("Extreme greed. Sentiment is stretched — the point at which "
                 "new buyers are paying up for what earlier buyers already own.")
        elif n < 45:
            t = "Cautious. Sentiment leans fearful without being extreme."
        elif n > 55:
            t = "Optimistic. Sentiment leans greedy without being extreme."
        else:
            t = "Neutral. Sentiment is giving no strong signal either way."
        out.append({"metric": "Fear & Greed",
                    "value": f"{n} ({db.kv_get('fng_label','')})", "text": t})

    r = _btc_vol_ratio()
    if r is None:
        out.append({"metric": "BTC volatility", "value": "—",
                    "text": "Needs ~3 days of continuous uptime to compute."})
    else:
        if r >= config.VOL_SPIKE_RATIO:
            t = (f"24h volatility is {r:.1f}x its 14-day baseline — regime change. "
                 "Position sizes that were sane last week are oversized now.")
        elif r < 0.6:
            t = (f"Volatility is {r:.1f}x baseline — unusually quiet. Compression "
                 "often precedes expansion, but gives no direction.")
        else:
            t = f"Volatility is {r:.1f}x baseline — normal range."
        out.append({"metric": "BTC volatility", "value": f"{r:.1f}x", "text": t})

    hot = [h for h in narrative.heat()
           if h["count24h"] >= config.HEAT_MIN_COUNT
           and h["accel"] >= config.HEAT_ACCEL_RATIO]
    if hot:
        names = ", ".join(h["narrative"] for h in hot[:3])
        out.append({"metric": "Narrative", "value": names,
                    "text": ("Coverage of these is accelerating against their own "
                             "baseline. Attention tends to rotate before capital "
                             "does — this is where to look, not what to conclude.")})
    else:
        out.append({"metric": "Narrative", "value": "no rotation",
                    "text": "No narrative is accelerating meaningfully right now."})
    return out


# --- named conditions ----------------------------------------------------

def _evaluate() -> list[dict]:
    f = _avg_watchlist_funding()
    fng = db.kv_get("fng_value")
    fng = int(fng) if fng else None
    vol = _btc_vol_ratio()
    hot = [h for h in narrative.heat()
           if h["count24h"] >= config.HEAT_MIN_COUNT
           and h["accel"] >= config.HEAT_ACCEL_RATIO]

    def cond(name, active, meaning):
        return {"name": name, "active": bool(active), "meaning": meaning}

    return [
        cond("crowded long", f is not None and f > config.FUNDING_EXTREME,
             "Longs paying shorts heavily. Leverage is one-sided; downside moves "
             "tend to be faster than the news justifies."),
        cond("crowded short", f is not None and f < -config.FUNDING_EXTREME,
             "Shorts paying longs heavily. One-sided the other way; squeezes "
             "start from here."),
        cond("extreme fear", fng is not None and fng <= config.FNG_LOW,
             "Sentiment washed out. Sellers may be exhausted — or early."),
        cond("extreme greed", fng is not None and fng >= config.FNG_HIGH,
             "Sentiment stretched. Late buyers are paying up."),
        cond("capitulation setup",
             fng is not None and fng <= 25 and f is not None and f < 0,
             "Extreme fear AND shorts paying longs — maximum pessimism with "
             "leverage positioned for more downside."),
        cond("euphoria setup",
             fng is not None and fng >= 75 and f is not None
             and f > config.FUNDING_EXTREME,
             "Extreme greed AND crowded longs — the combination that precedes "
             "the sharpest unwinds."),
        cond("volatility expansion",
             vol is not None and vol >= config.VOL_SPIKE_RATIO,
             "Realized volatility broke out of its baseline. Size down; stops "
             "placed for the old regime are now too tight."),
        cond("narrative rotation", bool(hot),
             "One or more narratives are accelerating hard. Attention rotates "
             "before capital does."),
    ]


def conditions() -> dict:
    """Current conditions plus how often each has fired since tracking began."""
    since = db.kv_get("conditions_since")
    if not since:
        since = str(int(time.time()))
        db.kv_set("conditions_since", since)
    days = max((time.time() - float(since)) / 86400, 0)
    out = []
    for c in _evaluate():
        row = db.fetchone(
            "SELECT COUNT(*) n, MAX(ts) last FROM condition_fires WHERE name=?",
            (c["name"],))
        out.append({**c, "fires": row["n"], "last_fired": row["last"]})
    return {"conditions": out, "tracking_days": days,
            "validated": False}


def record_fires() -> list[str]:
    """Log inactive -> active transitions. Returns names that just fired."""
    now = int(time.time())
    fired = []
    for c in _evaluate():
        was = db.kv_get(f"cond_active:{c['name']}") == "1"
        if c["active"] and not was:
            db.execute("INSERT INTO condition_fires(name, ts) VALUES (?,?)",
                       (c["name"], now))
            fired.append(c["name"])
        db.kv_set(f"cond_active:{c['name']}", "1" if c["active"] else "0")
    return fired


def summary() -> str:
    """One line describing the regime. Descriptive only."""
    active = [c["name"] for c in _evaluate() if c["active"]]
    if not active:
        return ("Nothing notable is firing. Funding, sentiment and volatility "
                "are all inside normal ranges.")
    return "Currently true: " + ", ".join(active) + "."
