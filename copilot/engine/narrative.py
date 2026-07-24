"""Narrative tagging + heat tracking.

Keyword tagger works with zero API cost; the LLM module upgrades tagging
quality when an Anthropic key is configured (see llm/).
Heat = mentions of a narrative in the last 24h vs its prior-7-day daily
average. Acceleration is what front-runs rotations, not absolute counts.
"""
import time

from .. import config, db

NARRATIVES: dict[str, list[str]] = {
    "memes": ["meme", "memecoin", "doge", "shiba", "pepe", "wif", "bonk", "floki"],
    "ai": ["ai ", " ai", "artificial intelligence", "ai agent", "gpt", "llm", "machine learning"],
    "defi": ["defi", "dex ", "lending", "yield", "liquidity pool", "amm", "staking"],
    "l2": ["layer 2", "l2", "rollup", "arbitrum", "optimism", "zksync", "base chain"],
    "rwa": ["rwa", "real world asset", "tokenized", "tokenization", "treasury"],
    "regulation": ["sec ", "regulat", "lawsuit", "ban ", "compliance", "cftc", "mica", "court"],
    "macro": ["fed ", "inflation", "rate cut", "rate hike", "recession", "tariff", "etf flow"],
    "security": ["hack", "exploit", "drained", "stolen", "breach", "rug pull", "scam"],
    "listing": ["list", "listing", "launchpool", "launchpad", "airdrop"],
    "etf": ["etf"],
    "stablecoin": ["stablecoin", "usdt", "usdc", "tether", "circle"],
    "bitcoin": ["bitcoin", "btc"],
    "ethereum": ["ethereum", " eth", "eth "],
    "solana": ["solana", " sol", "sol "],
}

HIGH_SEVERITY = {"security", "regulation", "etf", "listing"}


def tag(title: str) -> tuple[list[str], str]:
    """Returns (narratives, severity) for a headline. severity: 'high' | 'normal'."""
    t = f" {title.lower()} "
    hits = [name for name, kws in NARRATIVES.items() if any(kw in t for kw in kws)]
    severity = "high" if any(n in HIGH_SEVERITY for n in hits) else "normal"
    return hits, severity


def heat() -> list[dict]:
    """Per-narrative: count last 24h, prior 7d daily avg, acceleration ratio."""
    now = int(time.time())
    day_ago = now - 86400
    week_start = now - 8 * 86400
    rows = db.fetchall(
        "SELECT ts, narratives FROM news WHERE ts > ? AND narratives != ''", (week_start,))
    recent: dict[str, int] = {}
    prior: dict[str, int] = {}
    for r in rows:
        for n in r["narratives"].split(","):
            if r["ts"] > day_ago:
                recent[n] = recent.get(n, 0) + 1
            else:
                prior[n] = prior.get(n, 0) + 1
    out = []
    for name in NARRATIVES:
        c24 = recent.get(name, 0)
        prior_avg = prior.get(name, 0) / 7.0
        ratio = (c24 / prior_avg) if prior_avg > 0 else (float(c24) if c24 else 0.0)
        out.append({"narrative": name, "count24h": c24,
                    "prior_daily_avg": round(prior_avg, 1), "accel": round(ratio, 1)})
    out.sort(key=lambda x: (x["accel"], x["count24h"]), reverse=True)
    return out


def accelerating() -> list[dict]:
    """Narratives hot enough to alert on."""
    return [h for h in heat()
            if h["count24h"] >= config.HEAT_MIN_COUNT and h["accel"] >= config.HEAT_ACCEL_RATIO]
