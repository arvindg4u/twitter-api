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
from twikit.guest import GuestClient

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
# Browser TLS impersonation via curl_cffi (default: chrome). Set to empty to
# disable and use stock httpx. Needed because Cloudflare challenges
# datacenter IPs with non-browser TLS fingerprints.
TLS_IMPERSONATE = os.getenv("TLS_IMPERSONATE", "chrome")
# Fresh curl session per request (no connection/TLS-session reuse). Slower,
# but defeats Cloudflare flagging reused datacenter connections.
FRESH_SESSION = os.getenv("FRESH_SESSION", "true").lower() in ("1", "true", "yes")

def _make_transport() -> object | None:
    if not TLS_IMPERSONATE:
        return None
    from curl_transport import CurlCffiTransport

    return CurlCffiTransport(
        TLS_IMPERSONATE, fresh_session_per_request=FRESH_SESSION
    )


_transport = _make_transport()
client = Client("en-US", **({"transport": _transport} if _transport else {}))
guest = GuestClient("en-US", **({"transport": _make_transport()} if _transport else {}))
_logged_in = False
_login_lock = asyncio.Lock()
_guest_ready = False
_guest_lock = asyncio.Lock()


async def raw_guest_activate() -> str:
    """Guest token via bare request (no tid/cookies) — twikit's tid-carrying
    request gets a bogus 404 from X while the bare one returns 200."""
    import httpx
    from curl_transport import CurlCffiTransport

    async with httpx.AsyncClient(
        transport=CurlCffiTransport(
            TLS_IMPERSONATE or "chrome", fresh_session_per_request=True
        )
    ) as s:
        r = await s.post(
            "https://api.x.com/1.1/guest/activate.json",
            headers={
                "User-Agent": guest._user_agent,
                "authorization": f"Bearer {guest._token}",
                "content-type": "application/json",
                "Origin": "https://x.com",
                "Referer": "https://x.com/",
            },
            content=b"{}",
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["guest_token"]


async def ensure_guest() -> None:
    """Activate guest session (no credentials) for read-only endpoints."""
    global _guest_ready
    async with _guest_lock:
        if _guest_ready:
            return
        guest._guest_token = await raw_guest_activate()
        _guest_ready = True


async def prime_cookies() -> dict:
    """Fetch the homepage first so ct0/guest cookies + csrf token exist before
    the login flow hits api.x.com (datacenter IPs get Cloudflare-challenged
    on a bare first request)."""
    client.http.cookies.clear()  # avoid duplicate gt cookies on repeat calls
    info: dict = {}
    try:
        await client.http.get(
            "https://x.com/",
            headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
                "Referer": "https://x.com/",
                "User-Agent": client._user_agent,
            },
        )
    except Exception as e:
        info["home_fetch"] = f"FAIL: {type(e).__name__}: {e}"
    else:
        info["home_fetch"] = "ok"
    cookies = {c.name: c.value for c in client.http.cookies.jar}
    info["cookies"] = sorted(cookies.keys())
    info["csrf"] = bool(client._get_csrf_token())
    return info


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
        await prime_cookies()
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


@app.get("/diag-guest")
async def diag_guest(screen_name: str = "elonmusk") -> dict:
    """Raw guest GraphQL user lookup — shows exact X response for debugging."""
    import httpx
    from curl_transport import CurlCffiTransport

    steps: dict = {}
    # Raw guest_activate exactly like a bare urllib call (no tid, no cookies,
    # no extra headers) — isolates whether the endpoint itself 404s from Render.
    try:
        import httpx
        from curl_transport import CurlCffiTransport

        async with httpx.AsyncClient(
            transport=CurlCffiTransport("chrome", fresh_session_per_request=True)
        ) as raws:
            rr = await raws.post(
                "https://api.x.com/1.1/guest/activate.json",
                headers={
                    "User-Agent": guest._user_agent,
                    "authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
                    "content-type": "application/json",
                    "Origin": "https://x.com",
                    "Referer": "https://x.com/",
                },
                content=b"{}",
                timeout=60,
            )
            steps["raw_activate"] = f"status={rr.status_code} {rr.text[:120]}"
    except Exception as e:
        steps["raw_activate"] = f"FAIL: {type(e).__name__}: {str(e)[:200]}"
    try:
        if not guest._guest_token:
            await ensure_guest()
        gt = guest._guest_token
        steps["guest_token"] = f"ok len={len(gt)} prefix={gt[:4]}"
    except Exception as e:
        steps["guest_token"] = f"FAIL: {type(e).__name__}: {str(e)[:200]}"
        return steps
    try:
        if not guest.client_transaction.home_page_response:
            import httpx as _hx
            from curl_transport import CurlCffiTransport as _CT

            async with _hx.AsyncClient(
                transport=_CT("chrome", fresh_session_per_request=True)
            ) as tmps:
                await guest.client_transaction.init(
                    tmps,
                    {
                        "Accept-Language": "en-US,en;q=0.9",
                        "Cache-Control": "no-cache",
                        "Referer": "https://x.com",
                        "User-Agent": guest._user_agent,
                    },
                )
        tid = guest.client_transaction.generate_transaction_id(
            "GET", "/i/api/graphql/KybxDj9RrADIITXlGG8kpw/UserByScreenName"
        )
        steps["tid"] = f"ok len={len(tid)}"
    except Exception as e:
        steps["tid"] = f"FAIL: {type(e).__name__}: {str(e)[:200]}"
        tid = None
    try:
        async with httpx.AsyncClient(
            transport=CurlCffiTransport("chrome", fresh_session_per_request=True)
        ) as s:
            url = (
                "https://x.com/i/api/graphql/KybxDj9RrADIITXlGG8kpw/UserByScreenName"
                f"?variables={{\"screen_name\":\"{screen_name}\",\"withSafetyModeUserFields\":true}}"
                f"&features={{\"hidden_profile_subscriptions_enabled\":true}}"
            )
            base_gql_h = {
                "User-Agent": guest._user_agent,
                "Accept": "*/*",
                "authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
                "x-guest-token": gt,
                "x-twitter-active-user": "yes",
                "x-twitter-client-language": "en",
                "Origin": "https://x.com",
                "Referer": "https://x.com/",
            }
            r = await s.get(
                url, headers={**base_gql_h, "X-Client-Transaction-Id": tid},
                timeout=60,
            )
            steps["graphql_tid"] = f"status={r.status_code} {r.text[:200]}"
            r2 = await s.get(url, headers=base_gql_h, timeout=60)
            steps["graphql_notid"] = f"status={r2.status_code} {r2.text[:200]}"
            # I) same URL but with twikit's _base_headers (has OAuth2Session auth-type)
            r3 = await s.get(url, headers=dict(guest._base_headers, **{"x-guest-token": gt}), timeout=60)
            steps["graphql_baseh"] = f"status={r3.status_code} {r3.text[:200]}"
    except Exception as e:
        steps["graphql"] = f"FAIL: {type(e).__name__}: {str(e)[:200]}"
    return steps


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
    """Run the real ClientTransaction.init against a temp session and report."""
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
            try:
                await ct.init(session, headers)
                steps["init"] = (
                    f"ok, row={ct.DEFAULT_ROW_INDEX}, "
                    f"indices={ct.DEFAULT_KEY_BYTES_INDICES}, "
                    f"key_len={len(ct.key)}, anim_len={len(ct.animation_key)}"
                )
            except Exception as e:
                steps["init"] = f"FAIL: {type(e).__name__}: {e}"
            try:
                tid = ct.generate_transaction_id("GET", "/i/api/graphql/test")
                steps["tid"] = f"ok, len={len(tid)}"
            except Exception as e:
                steps["tid"] = f"FAIL: {type(e).__name__}: {e}"
    except Exception as e:
        steps["session"] = f"FAIL: {type(e).__name__}: {e}"
    return steps


@app.get("/diag-login")
async def diag_login() -> dict:
    """Prime cookies (like ensure_login does) then attempt guest_activate only.
    Shows whether Cloudflare still blocks api.x.com from Render."""
    steps: dict = {}
    from twikit.utils import Flow

    steps["prime"] = await prime_cookies()
    try:
        token = await client._get_guest_token()
        steps["guest_activate"] = f"ok, token_len={len(token)}"
    except Exception as e:
        steps["guest_activate"] = f"FAIL: {type(e).__name__}: {str(e)[:300]}"
        return steps
    try:
        flow = Flow(client, token)
        await flow.execute_task(params={"flow_name": "login"}, data={})
        steps["flow_login"] = f"ok, task={flow.task_id}"
    except Exception as e:
        steps["flow_login"] = f"FAIL: {type(e).__name__}: {str(e)[:300]}"
    # F) synthetic ct0: browsers self-generate a 160-hex ct0 cookie and echo
    # it as x-csrf-token. Our homepage fetch never yields ct0 (JS challenge),
    # so mint one and retry the flow step.
    try:
        import secrets

        client.set_cookies({"ct0": secrets.token_hex(80)}, clear_cookies=False)
        flow3 = Flow(client, token)
        await flow3.execute_task(params={"flow_name": "login"}, data={})
        steps["flow_synthct0"] = f"ok, task={flow3.task_id}"
    except Exception as e:
        steps["flow_synthct0"] = f"FAIL: {type(e).__name__}: {str(e)[:300]}"
    finally:
        client.http.cookies.clear()
    # G) header combos with synthetic ct0: csrf without auth-type, and
    # auth-type without csrf (raw curl, full control)
    try:
        import httpx
        import secrets as _secrets
        from curl_transport import CurlCffiTransport

        ct0 = _secrets.token_hex(80)
        base_h = {
            "User-Agent": client._user_agent,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": "https://x.com",
            "Referer": "https://x.com/",
            "authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
            "x-guest-token": token,
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
            "Cookie": f"ct0={ct0}; guest_id=v1%3A123; gt=123",
        }
        body = {
            "flow_name": "login",
            "input_flow_data": {
                "flow_context": {
                    "debug_overrides": {},
                    "start_location": {"location": "splash_screen"},
                }
            },
        }
        async with httpx.AsyncClient(
            transport=CurlCffiTransport("chrome", fresh_session_per_request=True)
        ) as fresh4:
            h1 = dict(base_h, **{"x-csrf-token": ct0})
            r1 = await fresh4.post(
                "https://api.x.com/1.1/onboarding/task.json",
                headers=h1, json=body, timeout=60,
            )
            steps["exp_csrf_noauthtype"] = f"status={r1.status_code} {r1.text[:120]}"
            h2 = dict(base_h, **{"x-twitter-auth-type": "OAuth2Session"})
            r2 = await fresh4.post(
                "https://api.x.com/1.1/onboarding/task.json",
                headers=h2, json=body, timeout=60,
            )
            steps["exp_authtype_nocsrf"] = f"status={r2.status_code} {r2.text[:120]}"
            # H) same request but on x.com/i/api host (what the browser uses)
            r3 = await fresh4.post(
                "https://x.com/i/api/1.1/onboarding/task.json",
                headers=h1, json=body, timeout=60,
            )
            steps["exp_xhost"] = f"status={r3.status_code} {r3.text[:200]}"
    except Exception as e:
        steps["exp_g"] = f"FAIL: {type(e).__name__}: {str(e)[:200]}"
    # E) browser-shaped first call: flow_name in BODY, no query, no subtask_inputs
    try:
        import httpx
        from curl_transport import CurlCffiTransport

        async with httpx.AsyncClient(
            transport=CurlCffiTransport("chrome", fresh_session_per_request=True)
        ) as fresh3:
            tid = client.client_transaction.generate_transaction_id(
                "POST", "/1.1/onboarding/task.json"
            )
            r = await fresh3.post(
                "https://api.x.com/1.1/onboarding/task.json",
                headers={
                    "User-Agent": client._user_agent,
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Content-Type": "application/json",
                    "Origin": "https://x.com",
                    "Referer": "https://x.com/",
                    "authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
                    "x-guest-token": token,
                    "x-twitter-active-user": "yes",
                    "x-twitter-client-language": "en",
                    "X-Client-Transaction-Id": tid,
                },
                json={
                    "flow_name": "login",
                    "input_flow_data": {
                        "flow_context": {
                            "debug_overrides": {},
                            "start_location": {"location": "splash_screen"},
                        }
                    },
                },
                timeout=60,
            )
            steps["exp_browserbody"] = f"status={r.status_code} {r.text[:200]}"
    except Exception as e:
        steps["exp_browserbody"] = f"FAIL: {type(e).__name__}: {str(e)[:200]}"
    # Isolate: is it the tid header or the cookies that trigger the challenge?
    # A) twikit headers + tid but WITHOUT cookies (fresh session)
    try:
        import httpx
        from curl_transport import CurlCffiTransport

        async with httpx.AsyncClient(
            transport=CurlCffiTransport("chrome")
        ) as fresh:
            tid = client.client_transaction.generate_transaction_id(
                "POST", "/1.1/onboarding/task.json"
            )
            r = await fresh.post(
                "https://api.x.com/1.1/onboarding/task.json",
                headers={
                    "User-Agent": client._user_agent,
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Content-Type": "application/json",
                    "Origin": "https://x.com",
                    "Referer": "https://x.com/",
                    "authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
                    "x-guest-token": token,
                    "x-twitter-active-user": "yes",
                    "x-twitter-client-language": "en",
                    "X-Client-Transaction-Id": tid,
                },
                json={"flow_name": "login", "input_flow_data": {}},
                timeout=60,
            )
            steps["exp_tid_nocookie"] = f"status={r.status_code} {r.text[:120]}"
    except Exception as e:
        steps["exp_tid_nocookie"] = f"FAIL: {type(e).__name__}: {str(e)[:200]}"
    # B) twikit headers WITHOUT tid but WITH cookies (client session)
    try:
        r, _ = await client.v11.base.post(
            "https://api.x.com/1.1/onboarding/task.json",
            json={"flow_name": "login", "input_flow_data": {}},
            headers={
                "User-Agent": client._user_agent,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Content-Type": "application/json",
                "Origin": "https://x.com",
                "Referer": "https://x.com/",
                "authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
                "x-guest-token": token,
                "x-twitter-active-user": "yes",
                "x-twitter-client-language": "en",
            },
            auto_unlock=False,
        )
        # NOTE: base.post adds tid automatically; this still isolates cookies.
        steps["exp_cookies_plustid"] = f"ok task={str(r)[:120]}"
    except Exception as e:
        steps["exp_cookies_plustid"] = f"FAIL: {type(e).__name__}: {str(e)[:200]}"
    # C) client session, cookieless, WITHOUT tid (raw curl, no tid header)
    try:
        import httpx
        from curl_transport import CurlCffiTransport

        async with httpx.AsyncClient(
            transport=CurlCffiTransport("chrome")
        ) as fresh2:
            r = await fresh2.post(
                "https://api.x.com/1.1/onboarding/task.json",
                headers={
                    "User-Agent": client._user_agent,
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Content-Type": "application/json",
                    "Origin": "https://x.com",
                    "Referer": "https://x.com/",
                    "authorization": "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA",
                    "x-guest-token": token,
                    "x-twitter-active-user": "yes",
                    "x-twitter-client-language": "en",
                },
                json={"flow_name": "login", "input_flow_data": {}},
                timeout=60,
            )
            steps["exp_notid_nocookie"] = f"status={r.status_code} {r.text[:120]}"
    except Exception as e:
        steps["exp_notid_nocookie"] = f"FAIL: {type(e).__name__}: {str(e)[:200]}"
    # D) capture EXACT bytes twikit Flow sends (no network), compare with exp C
    try:
        import httpx as _httpx

        cap: dict = {}

        class CapTransport(_httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                cap["url"] = str(request.url)
                cap["headers"] = {
                    k: v
                    for k, v in request.headers.items()
                    if "cookie" not in k.lower()
                }
                cap["body"] = (await request.aread()).decode()[:500]
                return _httpx.Response(
                    200, headers={}, content=b'{"flow_token":null}', request=request
                )

        from twikit import Client as TwikitClient

        c2 = TwikitClient("en-US", transport=CapTransport())
        c2.client_transaction.home_page_response = True
        import base64 as _b64

        c2.client_transaction.key = _b64.b64encode(b"0" * 48).decode()
        c2.client_transaction.animation_key = "abc123"
        c2.client_transaction.DEFAULT_ROW_INDEX = 1
        c2.client_transaction.DEFAULT_KEY_BYTES_INDICES = [1, 42, 16]
        flow2 = Flow(c2, token)
        await flow2.execute_task(params={"flow_name": "login"}, data={})
        steps["exp_capture"] = f"url={cap.get('url')} body={cap.get('body')}"
    except Exception as e:
        steps["exp_capture"] = f"FAIL: {type(e).__name__}: {str(e)[:200]}"
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
    await ensure_guest()
    u = await guest.get_user_by_screen_name(screen_name)
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
    await ensure_guest()
    u = await guest.get_user_by_screen_name(screen_name)
    tweets = await u.get_tweets(tweet_type, count=count)
    return [tweet_to_dict(t) for t in tweets]


@app.get("/tweet/{tweet_id}")
async def get_tweet(tweet_id: str):
    await ensure_guest()
    t = await guest.get_tweet_by_id(tweet_id)
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
