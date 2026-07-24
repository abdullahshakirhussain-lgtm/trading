"""Crypto Fear & Greed index from alternative.me (free, no key)."""
import logging

import httpx

log = logging.getLogger(__name__)

URL = "https://api.alternative.me/fng/?limit=1"


async def fetch() -> dict | None:
    """Returns {'value': int, 'label': str} or None."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(URL)
            r.raise_for_status()
            item = r.json()["data"][0]
            return {"value": int(item["value"]), "label": item["value_classification"]}
    except Exception as e:
        log.warning("fear&greed fetch failed: %s", e)
        return None
