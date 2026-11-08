"""Twitter API as a production MCP server (Streamable HTTP).

Exposes every twitter-api capability as MCP tools so any AI agent
(Claude, Cursor, custom agents) can use X data with zero glue code.

Transports:
  - stdio (local dev):            python twitter_mcp.py
  - Streamable HTTP (production): mounted at /mcp in app.py (uvicorn)

Auth is server-side (service API_KEY + X cookies live in the service
env). Tools take no credentials — agents just call them.
"""

import asyncio
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

import app as api

mcp = MCPServer("twitter-api")

# Retry policy for flaky X calls: (attempts, base_delay_s).
_RETRY = (3, 2.0)


async def _with_retry(label: str, fn, *args, **kwargs):
    """Run fn with exponential backoff on transient X errors."""
    last: Exception | None = None
    for attempt in range(_RETRY[0]):
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            # Deliberate, already-explained failure — never retry.
            raise
        except Exception as e:
            last = e
            msg = f"{type(e).__name__}: {e}"
            transient = any(
                s in msg
                for s in (
                    "429", "500", "502", "503", "504", "Timeout", "Connect",
                    "TooManyRequests", "RequestTimeout", "Internal error",
                    "Network", "Connection reset", "ConnectionReset",
                    "ReadError", "RemoteProtocolError", "SSL",
                )
            )
            if not transient or attempt == _RETRY[0] - 1:
                raise
            await asyncio.sleep(_RETRY[1] * (2**attempt))
    raise last  # pragma: no cover


async def _search_tweets(q: str, product: str, count: int) -> list[dict]:
    """Authed search with session refresh; @handle fallback without auth."""
    if q.strip().startswith("@"):
        try:
            await api.ensure_guest()
            u = await _with_retry(
                "handle-lookup",
                api.guest.get_user_by_screen_name,
                q.strip().lstrip("@"),
            )
            tweets = await _with_retry("handle-tweets", u.get_tweets, "Tweets", count=count)
            out = [api.tweet_to_dict(t) for t in tweets]
            if out:
                return out
        except Exception:
            pass
    # Authed path (refresh session once on auth failure, then retry).
    for login_try in (False, True):
        try:
            if login_try or not api._logged_in:
                api._logged_in = False
                await api.ensure_login()
            tweets = await _with_retry(
                "search", api.client.search_tweet, q, product, count
            )
            out = [api.tweet_to_dict(t) for t in tweets]
            if out:
                return out
            return []  # valid empty result, don't fall through
        except Exception as e:
            if login_try:
                # Last resort: X sometimes rejects the authed search path while
                # the guest endpoint still answers. Try it before failing.
                try:
                    await api.ensure_guest()
                    tweets = await _with_retry(
                        "guest-search", api.guest.search_tweet, q, product, count
                    )
                    return [api.tweet_to_dict(t) for t in tweets]
                except Exception:
                    pass
                raise RuntimeError(f"search failed: {type(e).__name__}: {str(e)[:150]}") from e
    raise RuntimeError("search failed")  # pragma: no cover


async def _search_users(q: str, count: int) -> list[dict]:
    def _fmt(users):
        return [
            {"id": u.id, "name": u.name, "screen_name": u.screen_name,
             "followers": u.followers_count} for u in users
        ]

    for login_try in (False, True):
        try:
            if login_try or not api._logged_in:
                api._logged_in = False
                await api.ensure_login()
            users = await _with_retry("user-search", api.client.search_user, q, count)
            out = _fmt(users)
            if out:
                return out
            return []
        except Exception as e:
            if login_try:
                break
    await api.ensure_guest()
    u = await _with_retry(
        "handle-resolve", api.guest.get_user_by_screen_name, q.lstrip("@")
    )
    return _fmt([u])


async def _authed(coro_fn):
    """Ensure login (refresh once) and run. Server-side auth only."""
    try:
        await api.ensure_login()
        return await coro_fn()
    except ToolError:
        # A deliberate failure (bad input, X rejected the write, etc.) —
        # re-login won't help and retrying can duplicate side effects.
        raise
    except Exception:
        api._logged_in = False
        await api.ensure_login()
        return await coro_fn()


def _raise_write_error(action: str, e: Exception) -> None:
    """Convert a write failure into an actionable ToolError."""
    msg = f"{type(e).__name__}: {e}"
    low = msg.lower()
    if "344" in msg or "daily limit" in low:
        raise ToolError(
            f"{action}: X daily tweet/message limit reached (code 344). "
            "Try again once the limit resets."
        ) from e
    raise ToolError(f"{action} failed: {msg[:400]}") from e


@mcp.tool()
async def search_tweets(
    q: str, product: Literal["Top", "Latest", "Media"] = "Latest", count: int = 10
) -> list[dict]:
    """Search X tweets. Keyword search + @handle timelines.
    Returns [{id, text, user{name,screen_name}, created_at, favorite_count, retweet_count, reply_count}]. Retries transient X errors automatically."""
    try:
        return await _search_tweets(q, product, min(max(count, 1), 20))
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(
            f"search_tweets failed: {type(e).__name__}: {str(e)[:300]}"
        ) from e


@mcp.tool()
async def search_users(q: str, count: int = 10) -> list[dict]:
    """Search X users by keyword, or resolve an exact screen_name.
    Returns [{id, name, screen_name, followers}]."""
    try:
        return await _search_users(q, min(max(count, 1), 20))
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(
            f"search_users failed: {type(e).__name__}: {str(e)[:300]}"
        ) from e


@mcp.tool()
async def get_user(screen_name: str) -> dict:
    """Get an X user profile. Returns id, name, screen_name, followers, following, tweets_count, description."""
    try:
        await api.ensure_guest()
        u = await _with_retry(
            "get-user", api.guest.get_user_by_screen_name, screen_name
        )
    except Exception as guest_error:
        # Guest sessions are occasionally rejected mid-flight; the authed
        # client is steadier. Keep the original error if both fail.
        try:
            u = await _authed(
                lambda: api.client.get_user_by_screen_name(screen_name)
            )
        except Exception:
            raise guest_error
    return {
        "id": u.id, "name": u.name, "screen_name": u.screen_name,
        "followers": u.followers_count, "following": u.following_count,
        "tweets_count": u.statuses_count, "description": u.description,
    }


@mcp.tool()
async def get_user_tweets(
    screen_name: str,
    count: int = 10,
    order: Literal["ranked", "chronological"] = "ranked",
    pages: int = 3,
) -> list[dict]:
    """Get tweets from an X user. order=ranked (X default) or chronological
    (oldest first, merges `pages` timeline pages). Returns tweet dicts."""
    count = min(max(count, 1), 40)
    if order != "chronological":
        try:
            await api.ensure_guest()
            u = await _with_retry(
                "get-user", api.guest.get_user_by_screen_name, screen_name
            )
            tweets, _ = await _with_retry(
                "get-tweets", u.get_tweets, "Tweets", count=count
            )
        except Exception:
            # Guest timeline occasionally flakes; fall back to the authed
            # endpoint before giving up.
            async def _authed_tweets():
                u = await api.client.get_user_by_screen_name(screen_name)
                res = await u.get_tweets("Tweets", count=count)
                return list(res)

            tweets = await _authed(_authed_tweets)
        # X's timeline ignores `count` and returns a full page; trim so
        # callers pay for only what they asked for.
        return [api.tweet_to_dict(t) for t in list(tweets)[:count]]
    tweets = await api._guest_timeline(
        screen_name, count, "chronological", max_pages=min(max(pages, 1), 10)
    )
    return list(tweets)[:count]


@mcp.tool()
async def get_tweet(tweet_id: str) -> dict:
    """Get a single tweet by ID. Returns the tweet dict (see search_tweets)."""
    await api.ensure_guest()
    try:
        return api.tweet_to_dict(
            await _with_retry("get-tweet", api.guest.get_tweet_by_id, tweet_id)
        )
    except Exception:
        import httpx

        async with httpx.AsyncClient(follow_redirects=True) as s:
            r = await s.get(
                f"https://api.fxtwitter.com/i/status/{tweet_id}",
                headers={"User-Agent": api.guest._user_agent}, timeout=60,
            )
            r.raise_for_status()
            j = r.json().get("tweet", {})
            u = j.get("author", {})
            return {
                "id": str(j.get("id", tweet_id)), "text": j.get("text", ""),
                "user": {"id": str(u.get("id", "")), "name": u.get("name", ""),
                         "screen_name": u.get("screen_name", "")},
                "created_at": j.get("created_at", ""),
                "favorite_count": j.get("likes"),
                "retweet_count": j.get("retweets"),
                "reply_count": j.get("replies"),
            }


@mcp.tool()
async def get_trends(woeid: int = 1, count: int = 10) -> list[dict]:
    """Get X trending topics (worldwide by default, woeid=1). Returns
    [{rank, name, tweet_count}] in X's own order. tweet_count is null: X no
    longer reports per-trend volume; use rank for ordering."""
    items = await _with_retry("trends", api.fetch_trends, woeid)
    # tweet_volume is null upstream (X no longer populates it); `rank` is X's
    # own ordering, 1 = top, so trends can still be ordered.
    return [
        {"rank": i + 1, "name": t.get("name"), "tweet_count": t.get("tweet_volume")}
        for i, t in enumerate(items[: min(max(count, 1), 50)])
    ]


@mcp.tool()
async def post_tweet(text: str, reply_to: str = "") -> dict:
    """Post a tweet from the connected X account. Returns {posted, id}."""
    async def _run():
        try:
            t = await api.client.create_tweet(text=text, reply_to=reply_to or None)
        except ToolError:
            raise
        except Exception as e:
            _raise_write_error("post_tweet", e)
        return {"posted": True, "id": t.id}

    return await _authed(_run)


@mcp.tool()
async def like_tweet(tweet_id: str) -> dict:
    """Like a tweet from the connected X account. Returns {liked, id}."""
    async def _run():
        t = await api.client.get_tweet_by_id(tweet_id)
        await t.favorite()
        return {"liked": True, "id": tweet_id}

    return await _authed(_run)


@mcp.tool()
async def unlike_tweet(tweet_id: str) -> dict:
    """Unlike a tweet. Returns {unliked, id}."""
    async def _run():
        t = await api.client.get_tweet_by_id(tweet_id)
        await t.unfavorite()
        return {"unliked": True, "id": tweet_id}

    return await _authed(_run)


@mcp.tool()
async def retweet(tweet_id: str) -> dict:
    """Retweet from the connected X account. Returns {retweeted, id}."""
    async def _run():
        t = await api.client.get_tweet_by_id(tweet_id)
        await t.retweet()
        return {"retweeted": True, "id": tweet_id}

    return await _authed(_run)


@mcp.tool()
async def unretweet(tweet_id: str) -> dict:
    """Remove a retweet. Returns {unretweeted, id}."""
    async def _run():
        t = await api.client.get_tweet_by_id(tweet_id)
        await t.delete_retweet()
        return {"unretweeted": True, "id": tweet_id}

    return await _authed(_run)


@mcp.tool()
async def delete_tweet(tweet_id: str) -> dict:
    """Delete your own tweet. Returns {deleted, id}."""
    async def _run():
        await api.client.delete_tweet(tweet_id)
        return {"deleted": True, "id": tweet_id}

    return await _authed(_run)


@mcp.tool()
async def send_dm(user_id: str, text: str) -> dict:
    """Send a DM to an X user ID. Returns {sent, to}."""
    async def _run():
        try:
            await api.client.send_dm(user_id, text)
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            low = msg.lower()
            if "344" in msg or "daily limit" in low:
                raise ToolError(
                    "X rejected the DM: daily message limit reached (code 344)."
                ) from e
            if "403" in msg or "forbidden" in low or "not allowed" in low:
                raise ToolError(
                    "X rejected the DM (403). The recipient may not accept DMs "
                    "from you, or this account lacks DM permission. "
                    f"Raw: {msg[:300]}"
                ) from e
            raise ToolError(f"send_dm failed: {msg[:300]}") from e
        return {"sent": True, "to": user_id}

    return await _authed(_run)


def _user_dict(u) -> dict:
    return {
        "id": u.id, "name": u.name, "screen_name": u.screen_name,
        "followers": u.followers_count,
    }


def _rest_user(u: dict) -> dict:
    """Map a v1.1 followers/friends list entry to the MCP user shape."""
    return {
        "id": str(u.get("id_str") or u.get("id") or ""),
        "name": u.get("name", "") or "",
        "screen_name": u.get("screen_name", "") or "",
        "followers": u.get("followers_count", 0) or 0,
    }


async def _rest_follow(user_id: str, count: int, kind: str) -> list[dict]:
    """Fetch followers/following from the v1.1 REST list endpoint.

    X sometimes 404s the GraphQL Followers query ID. The legacy payload is
    not GraphQL-shaped, so it is parsed here instead of via twikit's User
    model (which expects nested entities).
    """
    cap = min(max(count, 1), 40)
    endpoint = (
        api.client.v11.followers_list
        if kind == "followers"
        else api.client.v11.friends_list
    )
    resp, _ = await _with_retry(
        f"{kind}-rest",
        endpoint,
        user_id=user_id,
        screen_name=None,
        count=cap,
        cursor=None,
    )
    users = (resp or {}).get("users") or []
    return [_rest_user(x) for x in users[:cap]]


@mcp.tool()
async def follow_user(screen_name: str) -> dict:
    """Follow an X user. Returns {followed, screen_name}."""
    async def _run():
        u = await api.client.get_user_by_screen_name(screen_name)
        await u.follow()
        return {"followed": True, "screen_name": u.screen_name}

    return await _authed(_run)


@mcp.tool()
async def unfollow_user(screen_name: str) -> dict:
    """Unfollow an X user. Returns {unfollowed, screen_name}."""
    async def _run():
        u = await api.client.get_user_by_screen_name(screen_name)
        await u.unfollow()
        return {"unfollowed": True, "screen_name": u.screen_name}

    return await _authed(_run)


@mcp.tool()
async def get_followers(screen_name: str, count: int = 10) -> list[dict]:
    """Get followers of an X user. Returns [{id, name, screen_name, followers}]."""
    async def _run():
        u = await api.client.get_user_by_screen_name(screen_name)
        cap = min(max(count, 1), 40)
        try:
            res = await _with_retry("followers", u.get_followers, cap)
        except Exception:
            # X periodically rotates (or 404s) the GraphQL Followers query ID
            # while the v1.1 REST list keeps working.
            return await _rest_follow(u.id, cap, "followers")
        # X ignores `count` on this endpoint and returns a full page.
        return [_user_dict(x) for x in list(res)[:cap]]

    return await _authed(_run)


@mcp.tool()
async def get_following(screen_name: str, count: int = 10) -> list[dict]:
    """Get accounts an X user follows. Returns [{id, name, screen_name, followers}]."""
    async def _run():
        u = await api.client.get_user_by_screen_name(screen_name)
        cap = min(max(count, 1), 40)
        try:
            res = await _with_retry("following", u.get_following, cap)
        except Exception:
            # Same stale-GraphQL-query risk as followers; use v1.1 REST.
            return await _rest_follow(u.id, cap, "following")
        return [_user_dict(x) for x in list(res)[:cap]]

    return await _authed(_run)


@mcp.tool()
async def get_tweet_replies(tweet_id: str, count: int = 10) -> list[dict]:
    """Get replies to a tweet (conversation/thread context). Returns tweet dicts."""
    async def _run():
        t = await _with_retry("tweet", api.client.get_tweet_by_id, tweet_id)

        async def _replies():
            reps = t.replies or []
            return [api.tweet_to_dict(r) for r in list(reps)[: min(max(count, 1), 40)]]

        try:
            return await _with_retry("replies", _replies)
        except Exception:
            return []

    return await _authed(_run)


@mcp.tool()
async def get_user_likes(screen_name: str, count: int = 10) -> list[dict]:
    """Get tweets liked by an X user. Returns tweet dicts."""
    async def _run():
        u = await api.client.get_user_by_screen_name(screen_name)
        tweets = await _with_retry(
            "likes", u.get_tweets, "Likes", count=min(max(count, 1), 40)
        )
        return [api.tweet_to_dict(t) for t in list(tweets)[: min(max(count, 1), 40)]]

    return await _authed(_run)


@mcp.tool()
async def get_home_timeline(count: int = 10) -> list[dict]:
    """Get the connected account's home timeline. Returns tweet dicts."""
    async def _run():
        tweets = await _with_retry(
            "home-timeline",
            api.client.get_latest_timeline,
            count=min(max(count, 1), 40),
        )
        return [api.tweet_to_dict(t) for t in list(tweets)[: min(max(count, 1), 40)]]

    return await _authed(_run)


@mcp.tool()
async def bookmark_tweet(tweet_id: str) -> dict:
    """Bookmark a tweet. Returns {bookmarked, id}."""
    async def _run():
        await api.client.bookmark_tweet(tweet_id)
        return {"bookmarked": True, "id": tweet_id}

    return await _authed(_run)


@mcp.tool()
async def get_bookmarks(count: int = 10) -> list[dict]:
    """Get your bookmarked tweets. Returns tweet dicts."""
    async def _run():
        res = await _with_retry(
            "bookmarks", api.client.get_bookmarks, min(max(count, 1), 40)
        )
        return [api.tweet_to_dict(t) for t in list(res)[: min(max(count, 1), 40)]]

    return await _authed(_run)


@mcp.tool()
async def reply_to_tweet(tweet_id: str, text: str) -> dict:
    """Reply to a tweet. Returns {posted, id} of the reply."""
    async def _run():
        try:
            t = await api.client.create_tweet(text=text, reply_to=tweet_id)
        except ToolError:
            raise
        except Exception as e:
            _raise_write_error("reply_to_tweet", e)
        return {"posted": True, "id": t.id}

    return await _authed(_run)


@mcp.tool()
async def quote_tweet(tweet_id: str, text: str = "") -> dict:
    """Quote-post a tweet (comment + embed). Returns {posted, id}."""
    async def _run():
        src = await api.client.get_tweet_by_id(tweet_id)
        url = f"https://x.com/{src.user.screen_name}/status/{src.id}"
        try:
            t = await api.client.create_tweet(text=f"{text} {url}".strip())
        except ToolError:
            raise
        except Exception as e:
            _raise_write_error("quote_tweet", e)
        return {"posted": True, "id": t.id}

    return await _authed(_run)


_MAX_IMAGE_BYTES = 5 * 1024 * 1024   # X's tweet_image limit
_MAX_GIF_BYTES = 15 * 1024 * 1024    # X's tweet_gif limit


def _wikimedia_original(url: str) -> str | None:
    """Map a Wikimedia /thumb/ URL to the original file URL.

    Wikimedia now rejects arbitrary thumbnail widths ("Use thumbnail sizes
    listed on ...") with HTTP 400, so a thumb URL can fail even though the
    file itself is fine. Fetching the original is the portable fix.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    if not parts.netloc.lower().endswith("wikimedia.org"):
        return None
    if "/thumb/" not in parts.path:
        return None
    prefix, rest = parts.path.split("/thumb/", 1)
    segs = rest.split("/")
    if len(segs) < 3:
        return None
    original = f"{prefix}/{segs[0]}/{segs[1]}/{segs[2]}"
    return urlunsplit((parts.scheme, parts.netloc, original, "", ""))


async def _download(url: str) -> tuple[bytes, str]:
    """Fetch bytes from ``url``, retrying Wikimedia originals and no-referer."""
    import httpx

    transport = None
    try:
        from curl_transport import CurlCffiTransport

        transport = CurlCffiTransport(
            api.TLS_IMPERSONATE or "chrome", fresh_session_per_request=True
        )
    except Exception:
        # TLS impersonation is optional; plain httpx works for hosts that
        # don't fingerprint the client.
        transport = None

    accept = "image/avif,image/webp,image/png,image/jpeg,image/gif,*/*;q=0.8"
    ua = api.guest._user_agent
    candidates = [url]
    original = _wikimedia_original(url)
    if original and original != url:
        candidates.append(original)

    attempts: list[str] = []
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=120, transport=transport
    ) as s:
        for candidate in candidates:
            # Some image CDNs dislike a cross-origin Referer; try it both ways.
            for headers in (
                {"User-Agent": ua, "Accept": accept, "Referer": "https://x.com/"},
                {"User-Agent": ua, "Accept": accept},
            ):
                try:
                    r = await s.get(candidate, headers=headers)
                    if r.status_code >= 400:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    if not r.content:
                        raise RuntimeError("empty response body")
                    declared = (
                        (r.headers.get("content-type") or "")
                        .split(";")[0].strip().lower()
                    )
                    return r.content, declared
                except Exception as e:
                    attempts.append(f"{candidate}: {type(e).__name__}: {str(e)[:120]}")

    raise ToolError("could not download media. " + " | ".join(attempts[:4]))


async def _fetch_bytes(url: str) -> tuple[bytes, str]:
    """Download an image URL and return (bytes, mime)."""
    blob, declared = await _download(url)

    if len(blob) > _MAX_GIF_BYTES:
        raise ToolError(
            f"media is {len(blob)} bytes; X rejects files over "
            f"{_MAX_GIF_BYTES} bytes: {url}"
        )

    media_type = declared if declared.startswith("image/") else ""
    if not media_type:
        # Header was missing or generic (application/octet-stream): trust the
        # payload over the extension.
        try:
            import filetype

            kind = filetype.guess(blob)
            if kind and kind.mime.startswith("image/"):
                media_type = kind.mime
        except Exception:
            pass
    if not media_type:
        low = url.lower()
        for ext, mime in (
            (".png", "image/png"),
            (".gif", "image/gif"),
            (".webp", "image/webp"),
            (".jpg", "image/jpeg"),
            (".jpeg", "image/jpeg"),
        ):
            if ext in low:
                media_type = mime
                break
    if not media_type:
        raise ToolError(
            f"URL did not return an image (content-type={declared or 'missing'}): {url}"
        )

    limit = _MAX_GIF_BYTES if media_type == "image/gif" else _MAX_IMAGE_BYTES
    if len(blob) > limit:
        raise ToolError(
            f"{media_type} is {len(blob)} bytes; X's limit is {limit} bytes: {url}"
        )
    return blob, media_type


async def _upload_one(url: str) -> str:
    """Download and upload a single image, returning its media ID."""
    blob, media_type = await _fetch_bytes(url)
    # GIFs must carry a category or twikit refuses to poll completion.
    category = "tweet_gif" if media_type == "image/gif" else "tweet_image"
    try:
        mid = await api.client.upload_media(
            blob,
            wait_for_completion=True,
            media_type=media_type,
            media_category=category,
        )
    except ToolError:
        raise
    except Exception as e:
        raise ToolError(
            f"X rejected the media upload ({media_type}, {len(blob)} bytes): "
            f"{type(e).__name__}: {str(e)[:300]}"
        ) from e
    if not mid:
        raise ToolError(f"X did not return a media id for {url}")
    return mid


@mcp.tool()
async def upload_media(media_url: str) -> dict:
    """Upload an image from a URL for use in a tweet. Returns {media_id}."""
    async def _run():
        return {"media_id": await _upload_one(media_url)}

    return await _authed(_run)


@mcp.tool()
async def post_tweet_with_media(text: str, media_urls: list[str]) -> dict:
    """Post a tweet with 1-4 images (URLs). Returns {posted, id}."""
    if not media_urls:
        raise ToolError("media_urls must contain at least one image URL")
    async def _run():
        mids = [await _upload_one(url) for url in media_urls[:4]]
        try:
            t = await api.client.create_tweet(text=text, media_ids=mids)
        except ToolError:
            raise
        except Exception as e:
            _raise_write_error("post_tweet_with_media", e)
        return {"posted": True, "id": t.id}

    return await _authed(_run)


@mcp.tool()
async def unbookmark_tweet(tweet_id: str) -> dict:
    """Remove a tweet from your bookmarks. Returns {unbookmarked, id}."""
    async def _run():
        await api.client.delete_bookmark(tweet_id)
        return {"unbookmarked": True, "id": tweet_id}

    return await _authed(_run)


def _notif_dict(n) -> dict:
    u = getattr(n, "from_user", None)
    t = getattr(n, "tweet", None)
    return {
        "id": getattr(n, "id", ""),
        "message": getattr(n, "message", "") or "",
        "from_user": {
            "screen_name": getattr(u, "screen_name", "") or "",
            "name": getattr(u, "name", "") or "",
        },
        "tweet_id": getattr(t, "id", "") or "",
        "timestamp_ms": getattr(n, "timestamp_ms", 0),
    }


@mcp.tool()
async def get_mentions(count: int = 10) -> list[dict]:
    """Get mentions of the connected account. Returns [{id, message, from_user, tweet_id}]."""
    async def _run():
        res = await _with_retry(
            "mentions", api.client.get_notifications, "Mentions",
            min(max(count, 1), 40),
        )
        return [_notif_dict(n) for n in list(res)[: min(max(count, 1), 40)]]

    return await _authed(_run)


@mcp.tool()
async def get_notifications(count: int = 10) -> list[dict]:
    """Get notifications for the connected account. Returns [{id, message, from_user, tweet_id}]."""
    async def _run():
        res = await _with_retry(
            "notifications", api.client.get_notifications, "All",
            min(max(count, 1), 40),
        )
        return [_notif_dict(n) for n in list(res)[: min(max(count, 1), 40)]]

    return await _authed(_run)


@mcp.tool()
async def create_poll_tweet(
    text: str, options: list[str], duration_minutes: int = 1440
) -> dict:
    """Post a tweet with a poll (2-4 options). Returns {posted, id}."""
    async def _run():
        opts = [o for o in options if o.strip()][:4]
        if len(opts) < 2:
            raise ToolError("poll needs 2-4 non-empty options")
        uri = await api.client.create_poll(
            opts, duration_minutes=min(max(duration_minutes, 5), 10080)
        )
        try:
            t = await api.client.create_tweet(text=text, poll_uri=uri)
        except ToolError:
            raise
        except Exception as e:
            _raise_write_error("create_poll_tweet", e)
        return {"posted": True, "id": t.id}

    return await _authed(_run)


if __name__ == "__main__":
    import sys

    # stdio for local dev; production goes through app.py's /mcp mount.
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    mcp.run(transport=transport)
