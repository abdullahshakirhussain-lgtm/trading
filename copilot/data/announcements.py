"""Binance new-listing announcements.

Primary: the public CMS endpoint behind binance.com/en/support/announcement
(catalogId 48 = "New Cryptocurrency Listing"). Unofficial, so treated as
best-effort — geo/CDN blocks are logged, never fatal. The exchangeInfo
new-symbol diff (engine/alerts.py) is the reliable fallback signal.
"""
import logging

import httpx

log = logging.getLogger(__name__)

URL = ("https://www.binance.com/bapi/apex/v1/public/apex/cms/article/list/query"
       "?type=1&pageNo=1&pageSize=15&catalogId=48")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "application/json",
}


async def fetch_latest() -> list[dict]:
    """Returns [{'id': str, 'title': str, 'url': str, 'ts': int}] newest first."""
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            r = await client.get(URL, headers=HEADERS)
            r.raise_for_status()
            payload = r.json()
    except Exception as e:
        log.warning("binance announcements fetch failed: %s", e)
        return []
    try:
        catalogs = (payload.get("data") or {}).get("catalogs") or []
        articles = []
        for cat in catalogs:
            articles.extend(cat.get("articles") or [])
        if not articles:  # some responses put articles at the top level
            articles = (payload.get("data") or {}).get("articles") or []
        out = []
        for a in articles:
            code = a.get("code") or str(a.get("id", ""))
            if not code:
                continue
            out.append({
                "id": code,
                "title": a.get("title", ""),
                "url": f"https://www.binance.com/en/support/announcement/{code}",
                "ts": int((a.get("releaseDate") or 0) / 1000),
            })
        return out
    except Exception as e:
        log.warning("binance announcements parse failed: %s", e)
        return []
