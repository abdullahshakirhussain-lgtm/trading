"""Explosive-mover scanner: find small/new-cap coins igniting *now*.

The majors watchlist and the DEX radar miss the middle of the market — the
small, often newly-listed Binance spot/perp coins that make violent intraday
moves. This finds them with a two-stage sweep:

  stage 1  one ticker call per market (spot + perps) -> a cheap market-wide
           screen. Short-window % move comes from diffing the live price against
           a stored snapshot ~5 min old; 24h % and fresh-listing status are the
           other ways onto the candidate list.
  stage 2  for the (capped) candidate set, pull 5m candles and compute real
           momentum (5m/15m/1h) plus RVOL — current 5m volume vs its own trailing
           median, which is the actual tell that a move has fuel behind it.

Every candidate is tiered by 24h volume (micro/small/mid), scored, and run
through a quality gate. Like the DEX radar, this is descriptive: it surfaces
volatility, it does not tell anyone to trade. Most of these reverse hard.
"""
import logging
import statistics
import time

from .. import config, db, fmt

log = logging.getLogger(__name__)

DISCLAIMER = ("<i>Data, not a recommendation — this finds volatility, not free "
              "money. Most of these reverse hard.</i>")


# --- helpers -------------------------------------------------------------

def _base(symbol: str) -> str:
    """'ZAMA/USDT:USDT' -> 'ZAMA'; 'ZAMA/USDT' -> 'ZAMA'."""
    return symbol.split("/")[0].upper()


def _tier(qvol: float) -> str | None:
    """micro / small / mid by 24h quote volume, or None if outside the universe."""
    if qvol < config.SCAN_MIN_QVOL or qvol > config.SCAN_MID_MAX:
        return None
    if qvol < config.SCAN_SMALL_MIN:
        return "micro"
    if qvol < config.SCAN_MID_MIN:
        return "small"
    return "mid"


def _is_junk(base: str) -> bool:
    """Majors/stables (belong on the watchlist) and leveraged tokens (fake moves)."""
    if base in config.SCAN_EXCLUDE_BASES:
        return True
    if base.endswith(("BULL", "BEAR")):
        return True
    return any(x in base for x in ("3L", "3S", "5L", "5S"))


def _url(base: str, market: str) -> str:
    if market == "perp":
        return f"https://www.binance.com/en/futures/{base}USDT"
    return f"https://www.binance.com/en/trade/{base}_USDT"


def _recent_new_bases() -> set[str]:
    """Bases genuinely listed within the fresh-listing window (from the exchangeInfo diff).

    poll_new_symbols seeds the *entire* market with first_seen=now on its very first
    run, so a plain 'listed in the last 72h' query would tag every coin as fresh for
    three days. The seed all lands at ~one instant (the min first_seen); a real new
    listing shows up in a later poll, well after it. Ignore anything at the seed instant.
    """
    now = int(time.time())
    win_cutoff = now - int(config.SCAN_NEW_LISTING_BOOST_H * 3600)
    seed = db.fetchone("SELECT MIN(first_seen) AS m FROM known_symbols")
    if not seed or seed["m"] is None:
        return set()
    seed_floor = seed["m"] + 120  # 2-min grace past the initial bulk seed
    rows = db.fetchall(
        "SELECT symbol FROM known_symbols WHERE first_seen > ? AND first_seen > ?",
        (win_cutoff, seed_floor))
    return {_base(r["symbol"]) for r in rows}


def _prior_price(symbol: str, target_ago_s: float) -> float | None:
    """Snapshot price closest to `target_ago_s` ago, within a tolerance window."""
    now = int(time.time())
    lo = now - min(int(target_ago_s * 4), 900)   # not older than ~4x window / 15m
    hi = now - int(target_ago_s * 0.5)           # at least half a window old
    if hi <= lo:
        return None
    row = db.fetchone(
        "SELECT price FROM scan_snapshots WHERE symbol=? AND ts BETWEEN ? AND ? "
        "ORDER BY ABS(ts - ?) LIMIT 1", (symbol, lo, hi, now - int(target_ago_s)))
    return row["price"] if row and row["price"] else None


def _write_snapshots(rows: list[dict]) -> None:
    now = int(time.time())
    db.executemany(
        "INSERT INTO scan_snapshots(ts, market, symbol, price, qvol) VALUES (?,?,?,?,?)",
        [(now, r["market"], r["symbol"], r["price"], r["qvol"]) for r in rows])
    db.execute("DELETE FROM scan_snapshots WHERE ts < ?", (now - 30 * 60,))


def _momentum(candles: list[list]) -> dict:
    """From 5m OHLCV [[ts,o,h,l,c,v], …] -> mom_5m/15m/1h %, rvol, accel flag."""
    closes = [c[4] for c in candles if c and c[4]]
    vols = [c[5] for c in candles if c and c[5] is not None]

    def chg(n: int) -> float | None:
        if len(closes) > n and closes[-1 - n] > 0:
            return (closes[-1] / closes[-1 - n] - 1) * 100
        return None

    mom_5m, mom_15m, mom_1h = chg(1), chg(3), chg(12)
    rvol = None
    if len(vols) >= 4:
        base = statistics.median(vols[-13:-1]) if len(vols) >= 5 else statistics.median(vols[:-1])
        if base > 0:
            rvol = vols[-1] / base
    accel = None
    prev = None
    if len(closes) > 2 and closes[-3] > 0:
        prev = (closes[-2] / closes[-3] - 1) * 100
    if mom_5m is not None and prev is not None:
        accel = abs(mom_5m) > abs(prev)
    return {"mom_5m": mom_5m, "mom_15m": mom_15m, "mom_1h": mom_1h,
            "rvol": rvol, "accel": accel}


def _score(row: dict) -> float:
    m5 = abs(row.get("mom_5m") or 0)
    m15 = abs(row.get("mom_15m") or 0)
    rvol = row.get("rvol") or 1
    score = m5 + m15 * 0.5 + max(rvol - 1, 0) * 2
    if row.get("is_new"):
        score += 8
    if row.get("accel"):
        score += 3
    return round(score, 1)


def _flags(row: dict) -> list[str]:
    """Soft, descriptive caveats — not gate failures, just things to see."""
    flags = []
    rvol = row.get("rvol")
    if rvol is not None and rvol < 1:
        flags.append("volume fading — move not backed by fresh flow")
    chg24 = row.get("chg24")
    if chg24 is not None and abs(chg24) >= 60:
        flags.append(f"already {chg24:+.0f}% on the day — extended, chasing risk")
    if row.get("is_new"):
        flags.append("fresh listing — thin history, wild swings normal")
    return flags


def evaluate(row: dict) -> tuple[str, list[str]]:
    """Quality gate. PASS = worth showing; SKIP = data too thin to mean anything.

    Like the DEX radar's old gate: reasons explain a SKIP rather than hiding it.
    """
    reasons: list[str] = []
    if (row.get("qvol") or 0) < config.SCAN_MIN_QVOL:
        reasons.append(f"24h vol ${row.get('qvol') or 0:,.0f} below tradable floor")
    if row.get("price") is None:
        reasons.append("no price")
    if all(row.get(k) is None for k in ("mom_5m", "mom_15m", "mom_1h")):
        reasons.append("no candle data — can't confirm the move")
    return ("PASS" if not reasons else "SKIP"), reasons


def _tier_thr(table: dict, tier: str, is_new: bool) -> float:
    thr = table[tier]
    return thr * config.SCAN_NEW_LISTING_FACTOR if is_new else thr


def _should_push(row: dict) -> tuple[bool, str]:
    """Aggressive/early trigger: a momentum jump *with* a volume surge behind it."""
    tier, is_new = row["tier"], row["is_new"]
    m5, m15, rvol = row.get("mom_5m"), row.get("mom_15m"), row.get("rvol")
    trig5 = m5 is not None and abs(m5) >= _tier_thr(config.SCAN_MOM_5M, tier, is_new)
    trig15 = m15 is not None and abs(m15) >= _tier_thr(config.SCAN_MOM_15M, tier, is_new)
    rvol_ok = rvol is not None and rvol >= config.SCAN_RVOL
    # Fresh listings often have no usable volume baseline yet — let momentum alone carry them.
    push = (trig5 or trig15) and (rvol_ok or (is_new and rvol is None))
    lead = m5 if m5 is not None else (m15 or 0)
    return push, ("up" if lead >= 0 else "down")


# --- presentation --------------------------------------------------------

def _mom(x: float | None) -> str:
    return fmt.pct(x) if x is not None else "—"


def format_hit(row: dict) -> str:
    """One HTML block for a scan hit — shared by the push alert and /hot."""
    arrow = "🚀" if row.get("direction") == "up" else "🔻"
    new = " 🆕" if row.get("is_new") else ""
    lines = [
        f"{arrow} <b>{fmt.esc(row['base'])}</b> "
        f"[{fmt.esc(row['market'])} · {fmt.esc(row['tier'])}]{new}",
        f"{_mom(row.get('mom_5m'))} 5m · {_mom(row.get('mom_15m'))} 15m · "
        f"{_mom(row.get('mom_1h'))} 1h",
    ]
    meta = []
    if row.get("rvol") is not None:
        meta.append(f"RVOL {row['rvol']:.1f}x")
    if row.get("chg24") is not None:
        meta.append(f"24h {fmt.pct(row['chg24'])}")
    meta.append(f"vol {fmt.compact_usd(row.get('qvol'))}")
    if row.get("funding") is not None:
        meta.append(f"funding {fmt.funding_pct(row['funding'])}/8h")
    lines.append(" · ".join(meta))
    for fl in row.get("flags", []):
        lines.append(f"⚠️ {fmt.esc(fl)}")
    if row.get("reasons"):
        lines.append(f"↳ SKIP: {fmt.esc('; '.join(row['reasons']))}")
    if row.get("url"):
        lines.append(row["url"])
    return "\n".join(lines)


def live_payload(hits: list[dict], limit: int = 30) -> list[dict]:
    """Serializable snapshot of the current top PASS movers for the dashboard board.

    Unlike scan_hits (a sparse feed of *fired* alerts, gated by the 2h cooldown),
    this is the live picture every scan cycle — so the dashboard stays current.
    `hits` is already ranked PASS-first, score-desc by scan().
    """
    out = []
    for r in hits:
        if r["verdict"] != "PASS":
            continue
        out.append({
            "ts": int(time.time()), "market": r["market"], "symbol": r["symbol"],
            "base": r["base"], "tier": r["tier"], "is_new": 1 if r.get("is_new") else 0,
            "url": r.get("url"), "score": r.get("score"),
            "mom_5m": r.get("mom_5m"), "mom_15m": r.get("mom_15m"),
            "mom_1h": r.get("mom_1h"), "rvol": r.get("rvol"),
            "chg24": r.get("chg24"), "qvol": r.get("qvol"), "funding": r.get("funding"),
        })
        if len(out) >= limit:
            break
    return out


# --- orchestration -------------------------------------------------------

async def scan(market) -> list[dict]:
    """Run both stages. Returns evaluated rows (PASS and SKIP) ranked by score.

    Callers: poll_scan (alerts + history) and the /hot command (live view).
    """
    universe = await market.scan_universe()
    if not universe:
        return []  # geo-blocked or Binance unreachable

    new_bases = _recent_new_bases()

    # Stage 1: filter to the tradable universe, dedup a base to its deepest market,
    # and pick candidates worth a candle lookup.
    best_by_base: dict[str, dict] = {}
    for r in universe:
        base = _base(r["symbol"])
        if _is_junk(base):
            continue
        tier = _tier(r["qvol"])
        if tier is None:
            continue
        r = {**r, "base": base, "tier": tier, "is_new": base in new_bases}
        if base not in best_by_base or r["qvol"] > best_by_base[base]["qvol"]:
            best_by_base[base] = r

    rows = list(best_by_base.values())
    # Record this cycle's prices for *future* short-window deltas. These carry ts=now,
    # which falls outside the [older] window _prior_price reads, so they don't
    # contaminate this cycle's own comparison.
    _write_snapshots(rows)

    # Leaderboard, not a gate: rank the WHOLE tradable universe by how much it's
    # moving (recent short-window move weighted over the 24h move, fresh listings
    # boosted) and always take the top CAP. That keeps the board/'/hot' populated
    # with the current top movers even in a calm market — the alert thresholds only
    # decide what gets *pushed* (see _should_push), never what's shown.
    for r in rows:
        prior = _prior_price(r["symbol"], config.SCAN_SHORT_WINDOW_S)
        r["short_pct"] = ((r["price"] / prior - 1) * 100) if prior else None
        r["_rank"] = (abs(r["short_pct"] or 0) * 1.5
                      + abs(r.get("chg24") or 0) / 3
                      + (50 if r["is_new"] else 0))

    candidates = sorted(rows, key=lambda r: r["_rank"], reverse=True)[:config.SCAN_CANDIDATE_CAP]

    # Funding for perp context — one call covers every perp.
    try:
        funding = await market.funding_rates()
    except Exception:
        funding = {}

    # Stage 2: confirm each candidate with real candles.
    out = []
    for r in candidates:
        candles = await market.ohlcv(r["symbol"], "5m", 24, r["market"])
        r.update(_momentum(candles))
        r["funding"] = (funding.get(r["symbol"]) if r["market"] == "perp"
                        else funding.get(f"{r['base']}/USDT:USDT"))
        r["url"] = _url(r["base"], r["market"])
        r["score"] = _score(r)
        r["verdict"], r["reasons"] = evaluate(r)
        r["flags"] = _flags(r)
        r["push"], r["direction"] = _should_push(r)
        out.append(r)

    out.sort(key=lambda r: (r["verdict"] == "PASS", r["score"]), reverse=True)
    return out
