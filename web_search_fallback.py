"""Keyword tweet search without X auth: Brave (keyless) -> tweet IDs.

X blocks every guest search path (GraphQL, adaptive, typeahead, v1.1) and
login-walls the logged-out /search page. Brave's HTML endpoint still
indexes X posts without a key. We extract status IDs and hydrate them
through the guest TweetDetail API (which works).
"""

import re
from urllib.parse import quote

import httpx

_STATUS_RE = re.compile(r"(?:x|twitter)\.com/[A-Za-z0-9_]+/status/(\d+)")

# Tiny in-memory cache: {query: (timestamp, [ids])}.
_CACHE: dict[str, tuple[float, list[str]]] = {}
_TTL = 600.0


async def brave_tweet_ids(
    query: str, user_agent: str, limit: int = 20, transport=None
) -> list[str]:
    """Return up to `limit` tweet IDs for a keyword query via Brave."""
    import time

    key = query.strip().lower()
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1][:limit]

    url = (
        "https://search.brave.com/search?q="
        + quote(f"site:twitter.com {query.strip()}")
        + "&source=web"
    )
    headers = {"User-Agent": user_agent}
    if transport is not None:
        async with httpx.AsyncClient(transport=transport) as s:
            r = await s.get(url, headers=headers, timeout=60)
            html = r.text
    else:
        # Plain httpx is fine here: Brave is far less strict than X, and
        # callers may pass the curl_cffi transport when needed.
        async with httpx.AsyncClient(follow_redirects=True) as s:
            r = await s.get(url, headers=headers, timeout=60)
            html = r.text

    seen: set[str] = set()
    ids: list[str] = []
    for tid in _STATUS_RE.findall(html):
        if tid not in seen:
            seen.add(tid)
            ids.append(tid)
        if len(ids) >= limit:
            break
    _CACHE[key] = (now, ids)
    # Bound cache size.
    if len(_CACHE) > 200:
        oldest = min(_CACHE, key=lambda k: _CACHE[k][0])
        del _CACHE[oldest]
    return ids
