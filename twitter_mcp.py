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
        except Exception as e:
            last = e
            msg = f"{type(e).__name__}: {e}"
            transient = any(
                s in msg
                for s in (
                    "429", "500", "502", "503", "Timeout", "Connect",
                    "TooManyRequests", "RequestTimeout", "Internal error",
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
                raise RuntimeError(f"search failed: {type(e).__name__}: {str(e)[:150]}")
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
    except Exception:
        api._logged_in = False
        await api.ensure_login()
        return await coro_fn()


@mcp.tool()
async def search_tweets(
    q: str, product: Literal["Top", "Latest", "Media"] = "Latest", count: int = 10
) -> list[dict]:
    """Search X tweets. Keyword search + @handle timelines.
    Returns [{id, text, user{name,screen_name}, created_at, favorite_count, retweet_count, reply_count}]. Retries transient X errors automatically."""
    return await _search_tweets(q, product, min(max(count, 1), 20))


@mcp.tool()
async def search_users(q: str, count: int = 10) -> list[dict]:
    """Search X users by keyword, or resolve an exact screen_name.
    Returns [{id, name, screen_name, followers}]."""
    return await _search_users(q, min(max(count, 1), 20))


@mcp.tool()
async def get_user(screen_name: str) -> dict:
    """Get an X user profile. Returns id, name, screen_name, followers, following, tweets_count, description."""
    await api.ensure_guest()
    u = await _with_retry(
        "get-user", api.guest.get_user_by_screen_name, screen_name
    )
    return {
        "id": u.id, "name": u.name, "screen_name": u.screen_name,
        "followers": u.followers_count, "following": u.following_count,
        "tweets_count": u.statuses_count, "description": u.description,
    }


@mcp.tool()
async def get_user_tweets(screen_name: str, count: int = 10) -> list[dict]:
    """Get recent tweets from an X user. Returns tweet dicts (see search_tweets)."""
    await api.ensure_guest()
    u = await _with_retry(
        "get-user", api.guest.get_user_by_screen_name, screen_name
    )
    tweets = await _with_retry(
        "get-tweets", u.get_tweets, "Tweets", count=min(max(count, 1), 40)
    )
    return [api.tweet_to_dict(t) for t in tweets]


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
    """Get X trending topics (worldwide by default, woeid=1). Returns [{name, tweet_count}]."""
    await api.ensure_guest()
    items = await _with_retry("trends", api.guest.get_trends, woeid)
    return [
        {"name": t.get("name"), "tweet_count": t.get("tweet_volume")}
        for t in items[: min(max(count, 1), 50)]
    ]


@mcp.tool()
async def post_tweet(text: str, reply_to: str = "") -> dict:
    """Post a tweet from the connected X account. Returns {posted, id}."""
    async def _run():
        t = await api.client.create_tweet(text=text, reply_to=reply_to or None)
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
        await api.client.send_dm(user_id, text)
        return {"sent": True, "to": user_id}

    return await _authed(_run)


def _user_dict(u) -> dict:
    return {
        "id": u.id, "name": u.name, "screen_name": u.screen_name,
        "followers": u.followers_count,
    }


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
        res = await _with_retry(
            "followers", u.get_followers, min(max(count, 1), 40)
        )
        return [_user_dict(x) for x in res]

    return await _authed(_run)


@mcp.tool()
async def get_following(screen_name: str, count: int = 10) -> list[dict]:
    """Get accounts an X user follows. Returns [{id, name, screen_name, followers}]."""
    async def _run():
        u = await api.client.get_user_by_screen_name(screen_name)
        res = await _with_retry(
            "following", u.get_following, min(max(count, 1), 40)
        )
        return [_user_dict(x) for x in res]

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
        return [api.tweet_to_dict(t) for t in tweets]

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
        return [api.tweet_to_dict(t) for t in tweets]

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
        return [api.tweet_to_dict(t) for t in res]

    return await _authed(_run)


@mcp.tool()
async def reply_to_tweet(tweet_id: str, text: str) -> dict:
    """Reply to a tweet. Returns {posted, id} of the reply."""
    async def _run():
        t = await api.client.create_tweet(text=text, reply_to=tweet_id)
        return {"posted": True, "id": t.id}

    return await _authed(_run)


@mcp.tool()
async def quote_tweet(tweet_id: str, text: str = "") -> dict:
    """Quote-post a tweet (comment + embed). Returns {posted, id}."""
    async def _run():
        src = await api.client.get_tweet_by_id(tweet_id)
        url = f"https://x.com/{src.user.screen_name}/status/{src.id}"
        t = await api.client.create_tweet(text=f"{text} {url}".strip())
        return {"posted": True, "id": t.id}

    return await _authed(_run)


@mcp.tool()
async def upload_media(media_url: str) -> dict:
    """Upload an image from a URL for use in a tweet. Returns {media_id}."""
    async def _run():
        import httpx
        import tempfile
        import os

        async with httpx.AsyncClient(follow_redirects=True) as s:
            r = await s.get(media_url, timeout=120)
            r.raise_for_status()
            suffix = ".jpg"
            ct = r.headers.get("content-type", "")
            if "png" in ct:
                suffix = ".png"
            elif "gif" in ct:
                suffix = ".gif"
            elif "webp" in ct:
                suffix = ".webp"
            with tempfile.NamedTemporaryFile(
                suffix=suffix, delete=False
            ) as f:
                f.write(r.content)
                path = f.name
        try:
            mid = await api.client.upload_media(path, 0)
        finally:
            try:
                os.unlink(path)
            except Exception:
                pass
        return {"media_id": mid}

    return await _authed(_run)


@mcp.tool()
async def post_tweet_with_media(text: str, media_urls: list[str]) -> dict:
    """Post a tweet with 1-4 images (URLs). Returns {posted, id}."""
    async def _run():
        import httpx
        import tempfile
        import os

        mids = []
        for url in media_urls[:4]:
            async with httpx.AsyncClient(follow_redirects=True) as s:
                r = await s.get(url, timeout=120)
                r.raise_for_status()
                with tempfile.NamedTemporaryFile(
                    suffix=".jpg", delete=False
                ) as f:
                    f.write(r.content)
                    path = f.name
            try:
                mids.append(await api.client.upload_media(path, len(mids)))
            finally:
                try:
                    os.unlink(path)
                except Exception:
                    pass
        t = await api.client.create_tweet(text=text, media_ids=mids)
        return {"posted": True, "id": t.id}

    return await _authed(_run)


@mcp.tool()
async def get_mentions(count: int = 10) -> list[dict]:
    """Get mentions of the connected account. Returns [{id, text, user, created_at}]."""
    async def _run():
        res = await _with_retry(
            "mentions", api.client.get_notifications, "Mentions",
            min(max(count, 1), 40),
        )
        out = []
        for n in res:
            try:
                out.append(
                    {
                        "id": getattr(n, "id", ""),
                        "text": getattr(n, "text", "") or "",
                        "user": {
                            "screen_name": getattr(getattr(n, "user", None), "screen_name", "")
                        },
                        "created_at": str(getattr(n, "created_at", "")),
                    }
                )
            except Exception:
                continue
        return out

    return await _authed(_run)


@mcp.tool()
async def get_notifications(count: int = 10) -> list[dict]:
    """Get notifications for the connected account. Returns [{type, text, user}]."""
    async def _run():
        res = await _with_retry(
            "notifications", api.client.get_notifications, "All",
            min(max(count, 1), 40),
        )
        out = []
        for n in res:
            try:
                out.append(
                    {
                        "type": type(n).__name__,
                        "text": getattr(n, "text", "") or "",
                        "user": {
                            "screen_name": getattr(getattr(n, "user", None), "screen_name", "")
                        },
                    }
                )
            except Exception:
                continue
        return out

    return await _authed(_run)


@mcp.tool()
async def create_poll_tweet(
    text: str, options: list[str], duration_minutes: int = 1440
) -> dict:
    """Post a tweet with a poll (2-4 options). Returns {posted, id}."""
    async def _run():
        opts = [o for o in options if o.strip()][:4]
        if len(opts) < 2:
            raise RuntimeError("poll needs 2-4 non-empty options")
        uri = await api.client.create_poll(
            opts, duration_minutes=min(max(duration_minutes, 5), 10080)
        )
        t = await api.client.create_tweet(text=text, poll_uri=uri)
        return {"posted": True, "id": t.id}

    return await _authed(_run)


if __name__ == "__main__":
    import sys

    # stdio for local dev; production goes through app.py's /mcp mount.
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    mcp.run(transport=transport)
