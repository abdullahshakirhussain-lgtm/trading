"""Crypto news ingest from free RSS feeds into SQLite."""
import asyncio
import calendar
import hashlib
import logging
import time

import feedparser
import httpx

from .. import config, db
from ..engine import narrative

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) crypto-copilot/1.0"}


def _entry_id(url: str, title: str) -> str:
    return hashlib.sha1(f"{url}|{title}".encode()).hexdigest()[:16]


async def poll() -> list[dict]:
    """Fetch all feeds, insert new items, return the NEW items only."""
    new_items: list[dict] = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for source, feed_url in config.NEWS_FEEDS.items():
            try:
                r = await client.get(feed_url, headers=HEADERS)
                r.raise_for_status()
                # feedparser is blocking — parse off the event loop
                parsed = await asyncio.to_thread(feedparser.parse, r.content)
            except Exception as e:
                log.warning("news feed %s failed: %s", source, e)
                continue
            for e in parsed.entries[:30]:
                title = getattr(e, "title", "").strip()
                url = getattr(e, "link", "")
                if not title or not url:
                    continue
                nid = _entry_id(url, title)
                if db.fetchone("SELECT 1 FROM news WHERE id = ?", (nid,)):
                    continue
                published = getattr(e, "published_parsed", None)
                ts = calendar.timegm(published) if published else int(time.time())
                narratives, severity = narrative.tag(title)
                db.execute(
                    "INSERT OR IGNORE INTO news(id, ts, source, title, url, narratives, severity) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (nid, ts, source, title, url, ",".join(narratives), severity))
                new_items.append({
                    "id": nid, "ts": ts, "source": source, "title": title,
                    "url": url, "narratives": narratives, "severity": severity,
                })
    db.kv_set("last_poll_news", str(int(time.time())))
    return new_items
