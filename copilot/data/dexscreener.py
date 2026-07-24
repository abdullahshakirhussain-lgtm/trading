"""DexScreener free API — trending/boosted tokens + pair data for the memecoin radar.

Endpoints used (all free, no key):
- token-boosts/latest/v1  : tokens currently paying for visibility (proxy for "pushing hard")
- token-profiles/latest/v1: newly submitted token profiles
- tokens/v1/{chain}/{addrs}: pair stats (liquidity, volume, age, txns) for up to 30 tokens
"""
import logging
import time

import httpx

log = logging.getLogger(__name__)

BASE = "https://api.dexscreener.com"
HEADERS = {"User-Agent": "crypto-copilot/1.0 (personal alerts tool)"}


async def _get(client: httpx.AsyncClient, path: str):
    r = await client.get(f"{BASE}{path}", headers=HEADERS)
    r.raise_for_status()
    return r.json()


async def trending_candidates(chains: list[str]) -> list[dict]:
    """Boosted + newly profiled tokens on the given chains: [{'chain':…, 'addr':…}]."""
    out: dict[tuple, dict] = {}
    async with httpx.AsyncClient(timeout=20) as client:
        for path in ("/token-boosts/latest/v1", "/token-boosts/top/v1",
                     "/token-profiles/latest/v1"):
            try:
                items = await _get(client, path)
            except Exception as e:
                log.warning("dexscreener %s failed: %s", path, e)
                continue
            for item in items if isinstance(items, list) else []:
                chain = item.get("chainId", "")
                addr = item.get("tokenAddress", "")
                if chain in chains and addr:
                    out[(chain, addr)] = {"chain": chain, "addr": addr}
    return list(out.values())


async def pair_stats(chain: str, addrs: list[str]) -> list[dict]:
    """Best pair per token: liquidity, 24h volume, age, txns, price."""
    results = []
    async with httpx.AsyncClient(timeout=20) as client:
        for i in range(0, len(addrs), 30):
            batch = addrs[i:i + 30]
            try:
                pairs = await _get(client, f"/tokens/v1/{chain}/{','.join(batch)}")
            except Exception as e:
                log.warning("dexscreener pair_stats failed: %s", e)
                continue
            best: dict[str, dict] = {}
            for p in pairs if isinstance(pairs, list) else []:
                addr = (p.get("baseToken") or {}).get("address", "")
                liq = ((p.get("liquidity") or {}).get("usd")) or 0
                if addr and (addr not in best or liq > best[addr]["liq_usd"]):
                    created_ms = p.get("pairCreatedAt") or 0
                    age_h = (time.time() * 1000 - created_ms) / 3.6e6 if created_ms else None
                    txns = (p.get("txns") or {}).get("h24") or {}
                    best[addr] = {
                        "chain": chain,
                        "addr": addr,
                        "symbol": (p.get("baseToken") or {}).get("symbol", "?"),
                        "name": (p.get("baseToken") or {}).get("name", "?"),
                        "price_usd": float(p.get("priceUsd") or 0),
                        "liq_usd": liq,
                        "vol24": ((p.get("volume") or {}).get("h24")) or 0,
                        "age_h": age_h,
                        "buys24": txns.get("buys") or 0,
                        "sells24": txns.get("sells") or 0,
                        "fdv": p.get("fdv"),
                        "url": p.get("url", ""),
                    }
            results.extend(best.values())
    return results
