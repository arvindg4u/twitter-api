"""In-memory full-text index over tweets fetched through this service.

X blocks SearchTimeline for guest tokens (verified live + independently
confirmed by pravaha), so keyword search falls back to searching tweets
already seen via /tweet, /user/tweets and /search(@handle). The index
grows with usage. Thread-safe for asyncio (single loop).
"""

import re
import time

_TOKEN_RE = re.compile(r"[a-z0-9#@]+")

# tweet_id -> tweet dict (as returned by tweet_to_dict)
_TWEETS: dict[str, dict] = {}
# token -> {tweet_id: count}
_POSTINGS: dict[str, dict[str, int]] = {}
_SEEN_QUERIES: list[str] = []


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall((text or "").lower())


def index_tweet(tweet: dict) -> None:
    tid = str(tweet.get("id", ""))
    if not tid or tid in _TWEETS:
        return
    _TWEETS[tid] = tweet
    user = tweet.get("user") or {}
    blob = " ".join(
        [
            str(tweet.get("text", "")),
            str(user.get("name", "")),
            str(user.get("screen_name", "")),
        ]
    )
    counts: dict[str, int] = {}
    for tok in _tokens(blob):
        counts[tok] = counts.get(tok, 0) + 1
    for tok, n in counts.items():
        _POSTINGS.setdefault(tok, {})[tid] = n
    # Bound memory: keep newest ~5000 tweets.
    if len(_TWEETS) > 5000:
        oldest = next(iter(_TWEETS))
        old = _TWEETS.pop(oldest)
        for tok in set(_tokens(str(old.get("text", "")))):
            post = _POSTINGS.get(tok)
            if post is not None:
                post.pop(oldest, None)
                if not post:
                    _POSTINGS.pop(tok, None)


def index_many(tweets: list[dict]) -> None:
    for t in tweets:
        try:
            index_tweet(t)
        except Exception:
            continue


def search_index(query: str, limit: int = 20) -> list[dict]:
    toks = [t for t in _tokens(query) if t not in ("",)]
    if not toks:
        return []
    scores: dict[str, int] = {}
    for tok in toks:
        for tid, n in _POSTINGS.get(tok, {}).items():
            scores[tid] = scores.get(tid, 0) + n
    ranked = sorted(scores, key=lambda tid: (-scores[tid], tid))
    return [_TWEETS[tid] for tid in ranked[:limit] if tid in _TWEETS]


def stats() -> dict:
    return {
        "tweets_indexed": len(_TWEETS),
        "unique_tokens": len(_POSTINGS),
        "uptime_note": "in-memory, resets on redeploy",
    }


def note_query(q: str) -> None:
    q = q.strip()
    if q and (not _SEEN_QUERIES or _SEEN_QUERIES[-1] != q):
        _SEEN_QUERIES.append(q)
        del _SEEN_QUERIES[:-50]
