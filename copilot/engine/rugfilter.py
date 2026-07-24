"""Rug-risk filter for memecoin radar candidates.

Works only with what free data can prove: liquidity depth, pair age,
volume/liquidity sanity, and one-sided flow. Candidates that fail are still
shown (flagged SKIP with reasons) — hiding them would hide the lesson.
This is a filter for OBVIOUS traps, not a safety guarantee.
"""
from .. import config


def evaluate(p: dict) -> tuple[str, list[str]]:
    """p is a pair_stats dict from data/dexscreener.py. Returns (verdict, reasons).

    verdict: 'PASS' | 'SKIP'
    """
    reasons: list[str] = []

    liq = p.get("liq_usd") or 0
    if liq < config.RUG_MIN_LIQ_USD:
        reasons.append(f"liquidity ${liq:,.0f} < ${config.RUG_MIN_LIQ_USD:,.0f}")

    age_h = p.get("age_h")
    if age_h is None:
        reasons.append("pair age unknown")
    elif age_h < config.RUG_MIN_AGE_H:
        reasons.append(f"pair only {age_h:.0f}h old (< {config.RUG_MIN_AGE_H:.0f}h)")

    vol24 = p.get("vol24") or 0
    if liq > 0:
        ratio = vol24 / liq
        if ratio < config.RUG_MIN_VOL_LIQ:
            reasons.append(f"vol/liq {ratio:.2f} — dead pool")
        elif ratio > config.RUG_MAX_VOL_LIQ:
            reasons.append(f"vol/liq {ratio:.0f} — wash-trading pattern")

    buys, sells = p.get("buys24") or 0, p.get("sells24") or 0
    total = buys + sells
    if total >= 50 and sells > 0 and buys / max(sells, 1) > 8:
        reasons.append("extreme one-sided buying — possible honeypot (can't sell)")
    if total < 20:
        reasons.append(f"only {total} trades in 24h")

    return ("PASS" if not reasons else "SKIP"), reasons
