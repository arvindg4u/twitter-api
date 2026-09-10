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

from fastapi import FastAPI, HTTPException, Query, Header
from pydantic import BaseModel
from twikit import Client

app = FastAPI(title="twitter-api", version="1.0.0")

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
