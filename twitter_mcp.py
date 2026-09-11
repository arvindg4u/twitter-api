"""Twitter API as a production MCP server (Streamable HTTP).

Exposes every twitter-api capability as MCP tools so any AI agent
(Claude, Cursor, custom agents) can use X data with zero glue code.

Transports:
  - stdio (local dev):            python mcp_server.py
  - Streamable HTTP (production): mounted at /mcp in app.py (uvicorn)

Auth: write tools take an api_key argument (maps to the service API_KEY).
Read tools are public, matching the REST API.
"""

from typing import Literal

from mcp.server.mcpserver import MCPServer

import app as api

mcp = MCPServer("twitter-api")


async def _search_tweets(q: str, product: str, count: int) -> list[dict]:
    if api._logged_in:
        tweets = await api.client.search_tweet(q, product, count)
        return [api.tweet_to_dict(t) for t in tweets]
    try:
        await api.ensure_login()
        tweets = await api.client.search_tweet(q, product, count)
        return [api.tweet_to_dict(t) for t in tweets]
    except Exception:
        pass
    if q.strip().startswith("@"):
        await api.ensure_guest()
        u = await api.guest.get_user_by_screen_name(q.strip().lstrip("@"))
        tweets = await u.get_tweets("Tweets", count=count)
        return [api.tweet_to_dict(t) for t in tweets]
    raise RuntimeError(
        "keyword search needs auth: import cookies via POST /cookies or COOKIES_JSON"
    )


async def _search_users(q: str, count: int) -> list[dict]:
    def _fmt(users):
        return [
            {"id": u.id, "name": u.name, "screen_name": u.screen_name,
             "followers": u.followers_count} for u in users
        ]

    if api._logged_in:
        return _fmt(await api.client.search_user(q, count))
    try:
        await api.ensure_login()
        return _fmt(await api.client.search_user(q, count))
    except Exception:
        pass
    await api.ensure_guest()
    u = await api.guest.get_user_by_screen_name(q.lstrip("@"))
    return _fmt([u])


async def _authed(coro_fn, api_key: str | None):
    if api.API_KEY and api_key != api.API_KEY:
        raise RuntimeError("invalid or missing api_key")
    await api.ensure_login()
    return await coro_fn()


@mcp.tool()
async def search_tweets(
    q: str, product: Literal["Top", "Latest", "Media"] = "Latest", count: int = 10
) -> list[dict]:
    """Search tweets by keyword (authed) or @handle timeline (no-auth).
    Returns [{id, text, user, created_at, favorite_count, retweet_count, reply_count}]."""
    return await _search_tweets(q, product, min(max(count, 1), 20))


@mcp.tool()
async def search_users(q: str, count: int = 10) -> list[dict]:
    """Search users (authed) or resolve exact screen_name (no-auth).
    Returns [{id, name, screen_name, followers}]."""
    return await _search_users(q, min(max(count, 1), 20))


@mcp.tool()
async def get_user(screen_name: str) -> dict:
    """Get an X user profile (no-auth). Returns id, name, followers, etc."""
    await api.ensure_guest()
    u = await api.guest.get_user_by_screen_name(screen_name)
    return {
        "id": u.id, "name": u.name, "screen_name": u.screen_name,
        "followers": u.followers_count, "following": u.following_count,
        "tweets_count": u.statuses_count, "description": u.description,
    }


@mcp.tool()
async def get_user_tweets(screen_name: str, count: int = 10) -> list[dict]:
    """Get recent tweets from a user (no-auth)."""
    await api.ensure_guest()
    u = await api.guest.get_user_by_screen_name(screen_name)
    tweets = await u.get_tweets("Tweets", count=min(max(count, 1), 40))
    return [api.tweet_to_dict(t) for t in tweets]


@mcp.tool()
async def get_tweet(tweet_id: str) -> dict:
    """Get a single tweet by ID (no-auth, FxTwitter fallback included)."""
    await api.ensure_guest()
    try:
        return api.tweet_to_dict(await api.guest.get_tweet_by_id(tweet_id))
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
    """Get trending topics, worldwide by default (no-auth). woeid=1 worldwide."""
    await api.ensure_guest()
    items = await api.guest.get_trends(woeid)
    return [
        {"name": t.get("name"), "tweet_count": t.get("tweet_volume")}
        for t in items[: min(max(count, 1), 50)]
    ]


@mcp.tool()
async def post_tweet(text: str, api_key: str = "", reply_to: str = "") -> dict:
    """Post a tweet (auth required via api_key). Returns {posted, id}."""
    async def _run():
        t = await api.client.create_tweet(text=text, reply_to=reply_to or None)
        return {"posted": True, "id": t.id}

    return await _authed(_run, api_key or None)


@mcp.tool()
async def like_tweet(tweet_id: str, api_key: str = "") -> dict:
    """Like a tweet (auth required via api_key)."""
    async def _run():
        t = await api.client.get_tweet_by_id(tweet_id)
        await t.favorite()
        return {"liked": True, "id": tweet_id}

    return await _authed(_run, api_key or None)


@mcp.tool()
async def unlike_tweet(tweet_id: str, api_key: str = "") -> dict:
    """Unlike a tweet (auth required via api_key)."""
    async def _run():
        t = await api.client.get_tweet_by_id(tweet_id)
        await t.unfavorite()
        return {"unliked": True, "id": tweet_id}

    return await _authed(_run, api_key or None)


@mcp.tool()
async def retweet(tweet_id: str, api_key: str = "") -> dict:
    """Retweet (auth required via api_key)."""
    async def _run():
        t = await api.client.get_tweet_by_id(tweet_id)
        await t.retweet()
        return {"retweeted": True, "id": tweet_id}

    return await _authed(_run, api_key or None)


@mcp.tool()
async def unretweet(tweet_id: str, api_key: str = "") -> dict:
    """Remove a retweet (auth required via api_key)."""
    async def _run():
        t = await api.client.get_tweet_by_id(tweet_id)
        await t.delete_retweet()
        return {"unretweeted": True, "id": tweet_id}

    return await _authed(_run, api_key or None)


@mcp.tool()
async def delete_tweet(tweet_id: str, api_key: str = "") -> dict:
    """Delete your own tweet (auth required via api_key)."""
    async def _run():
        await api.client.delete_tweet(tweet_id)
        return {"deleted": True, "id": tweet_id}

    return await _authed(_run, api_key or None)


@mcp.tool()
async def send_dm(user_id: str, text: str, api_key: str = "") -> dict:
    """Send a DM to a user ID (auth required via api_key)."""
    async def _run():
        await api.client.send_dm(user_id, text)
        return {"sent": True, "to": user_id}

    return await _authed(_run, api_key or None)


if __name__ == "__main__":
    import sys

    # stdio for local dev; production goes through app.py's /mcp mount.
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    mcp.run(transport=transport)
