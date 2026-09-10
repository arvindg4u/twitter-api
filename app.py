"""FastAPI wrapper around twikit — makes this library deployable as a Render Web Service.

Auth comes from env vars: TWITTER_USERNAME, TWITTER_EMAIL, TWITTER_PASSWORD.
Optional: TWITTER_TOTP_SECRET (for 2FA accounts), API_KEY (protects write endpoints).

Run locally:
    pip install -r requirements.txt
    export TWITTER_USERNAME=... TWITTER_EMAIL=... TWITTER_PASSWORD=...
    uvicorn app:app --reload
"""

import asyncio
import os
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from twikit import Client

app = FastAPI(title="twitter-api", version="1.0.0")


@app.exception_handler(Exception)
async def all_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Convert unhandled errors (e.g. twikit login failures) to JSON instead of
    Starlette's plain-text 'Internal Server Error' so the real cause is visible."""
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return JSONResponse(
        status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"}
    )

USERNAME = os.getenv("TWITTER_USERNAME")
EMAIL = os.getenv("TWITTER_EMAIL")
PASSWORD = os.getenv("TWITTER_PASSWORD")
TOTP_SECRET = os.getenv("TWITTER_TOTP_SECRET")
API_KEY = os.getenv("API_KEY")  # if set, write endpoints require x-api-key header
COOKIES_FILE = os.getenv("COOKIES_FILE", "cookies.json")

client = Client("en-US")
_logged_in = False
_login_lock = asyncio.Lock()


async def ensure_login() -> None:
    """Log in once per process lifetime; reuse session cookies after that."""
    global _logged_in
    async with _login_lock:
        if _logged_in:
            return
        if not all([USERNAME, EMAIL, PASSWORD]):
            raise HTTPException(
                status_code=500,
                detail="Missing env vars: set TWITTER_USERNAME, TWITTER_EMAIL, TWITTER_PASSWORD",
            )
        await client.login(
            auth_info_1=USERNAME,
            auth_info_2=EMAIL,
            password=PASSWORD,
            totp_secret=TOTP_SECRET,
            cookies_file=COOKIES_FILE,
        )
        _logged_in = True


def check_api_key(x_api_key: str | None) -> None:
    """Gate write endpoints when API_KEY is configured."""
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing x-api-key")


def tweet_to_dict(t) -> dict:
    return {
        "id": t.id,
        "text": t.text,
        "user": {"id": t.user.id, "name": t.user.name, "screen_name": t.user.screen_name},
        "created_at": str(t.created_at),
        "favorite_count": t.favorite_count,
        "retweet_count": t.retweet_count,
        "reply_count": t.reply_count,
    }


class TweetIn(BaseModel):
    text: str
    reply_to: str | None = None


class DMIn(BaseModel):
    user_id: str
    text: str


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/debug")
def debug() -> dict:
    """Show which env vars are present (values never exposed) — helps diagnose 500s."""
    return {
        "env_present": {
            "TWITTER_USERNAME": bool(USERNAME),
            "TWITTER_EMAIL": bool(EMAIL),
            "TWITTER_PASSWORD": bool(PASSWORD),
            "TWITTER_TOTP_SECRET": bool(TOTP_SECRET),
            "API_KEY": bool(API_KEY),
        },
        "logged_in": _logged_in,
    }


@app.post("/login")
async def do_login(x_api_key: str | None = Header(default=None)) -> dict:
    """Trigger the login flow explicitly (protected when API_KEY is set).
    Lets you verify credentials work without calling a data endpoint."""
    check_api_key(x_api_key)
    global _logged_in
    _logged_in = False  # force a fresh login attempt
    await ensure_login()
    me = await client.get_user_by_screen_name(USERNAME.lstrip("@"))
    return {"logged_in": True, "id": me.id, "name": me.name}


@app.get("/diag-transaction")
async def diag_transaction() -> dict:
    """Step-by-step diagnosis of ClientTransaction.init — shows exactly which step fails."""
    from twikit.x_client_transaction.transaction import ClientTransaction
    import httpx

    steps: dict = {}
    ct = ClientTransaction()
    headers = {
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Referer": "https://x.com",
        "User-Agent": client._user_agent,
    }
    try:
        async with httpx.AsyncClient() as session:
            from twikit.x_client_transaction.utils import handle_x_migration

            home = await handle_x_migration(session, headers)
            steps["fetch_home"] = f"ok, len={len(str(home))}"
            try:
                row, idx = await ct.get_indices(home, session, headers)
                steps["get_indices"] = f"ok, row={row}, n_indices={len(idx)}"
            except Exception as e:
                steps["get_indices"] = f"FAIL: {type(e).__name__}: {e}"
                return steps
            try:
                key = ct.get_key(response=home)
                steps["get_key"] = f"ok, len={len(key)}"
            except Exception as e:
                steps["get_key"] = f"FAIL: {type(e).__name__}: {e}"
                return steps
            try:
                kb = ct.get_key_bytes(key=key)
                steps["get_key_bytes"] = f"ok, n={len(kb)}"
            except Exception as e:
                steps["get_key_bytes"] = f"FAIL: {type(e).__name__}: {e}"
                return steps
            try:
                ak = ct.get_animation_key(key_bytes=kb, response=home)
                steps["get_animation_key"] = f"ok, len={len(ak)}"
            except Exception as e:
                steps["get_animation_key"] = f"FAIL: {type(e).__name__}: {e}"
                return steps
    except Exception as e:
        steps["fetch_home"] = f"FAIL: {type(e).__name__}: {e}"
    return steps


@app.get("/search")
async def search(
    q: str = Query(..., description="Search query"),
    product: Literal["Top", "Latest", "Media"] = "Latest",
    count: int = Query(20, ge=1, le=20),
):
    await ensure_login()
    tweets = await client.search_tweet(q, product, count=count)
    return [tweet_to_dict(t) for t in tweets]


@app.get("/search-users")
async def search_users(q: str = Query(...), count: int = Query(20, ge=1, le=20)):
    await ensure_login()
    users = await client.search_user(q, count=count)
    return [
        {
            "id": u.id,
            "name": u.name,
            "screen_name": u.screen_name,
            "followers": u.followers_count,
        }
        for u in users
    ]


@app.get("/user/{screen_name}")
async def get_user(screen_name: str):
    await ensure_login()
    u = await client.get_user_by_screen_name(screen_name)
    return {
        "id": u.id,
        "name": u.name,
        "screen_name": u.screen_name,
        "followers": u.followers_count,
        "following": u.following_count,
        "tweets_count": u.statuses_count,
        "description": u.description,
    }


@app.get("/user/{screen_name}/tweets")
async def get_user_tweets(
    screen_name: str,
    tweet_type: Literal["Tweets", "Replies", "Media", "Likes"] = "Tweets",
    count: int = Query(20, ge=1, le=40),
):
    await ensure_login()
    u = await client.get_user_by_screen_name(screen_name)
    tweets = await u.get_tweets(tweet_type, count=count)
    return [tweet_to_dict(t) for t in tweets]


@app.get("/tweet/{tweet_id}")
async def get_tweet(tweet_id: str):
    await ensure_login()
    t = await client.get_tweet_by_id(tweet_id)
    return tweet_to_dict(t)


@app.get("/trends")
async def trends(
    category: Literal["trending", "for-you", "news", "sports", "entertainment"] = "trending",
    count: int = Query(20, ge=1, le=50),
):
    await ensure_login()
    items = await client.get_trends(category, count=count)
    return [{"name": tr.name, "tweet_count": tr.tweet_count} for tr in items]


@app.post("/tweet")
async def post_tweet(body: TweetIn, x_api_key: str | None = Header(default=None)):
    check_api_key(x_api_key)
    await ensure_login()
    t = await client.create_tweet(text=body.text, reply_to=body.reply_to)
    return {"posted": True, "id": t.id}


@app.post("/tweet/{tweet_id}/like")
async def like_tweet(tweet_id: str, x_api_key: str | None = Header(default=None)):
    check_api_key(x_api_key)
    await ensure_login()
    t = await client.get_tweet_by_id(tweet_id)
    await t.favorite()
    return {"liked": True, "id": tweet_id}


@app.post("/tweet/{tweet_id}/retweet")
async def retweet(tweet_id: str, x_api_key: str | None = Header(default=None)):
    check_api_key(x_api_key)
    await ensure_login()
    t = await client.get_tweet_by_id(tweet_id)
    await t.retweet()
    return {"retweeted": True, "id": tweet_id}


@app.post("/dm")
async def send_dm(body: DMIn, x_api_key: str | None = Header(default=None)):
    check_api_key(x_api_key)
    await ensure_login()
    await client.send_dm(body.user_id, body.text)
    return {"sent": True, "to": body.user_id}
