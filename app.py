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

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    if AUTO_LOGIN_RETRY and all([USERNAME, EMAIL, PASSWORD]):
        asyncio.create_task(_auto_login_loop())
    yield


app = FastAPI(title="twitter-api", version="1.0.0", lifespan=lifespan)


async def _auto_login_loop() -> None:
    """Retry browser login every 6h until cookies are minted (rate limits
    clear with time). Stops permanently once logged in."""
    import logging

    log = logging.getLogger("auto-login")
    while True:
        await asyncio.sleep(6 * 3600)
        global _logged_in
        if _logged_in:
            return
        try:
            from browser_login import browser_bootstrap

            cookies = await asyncio.wait_for(
                browser_bootstrap(USERNAME, EMAIL, PASSWORD, client._user_agent),
                timeout=900,
            )
        except Exception as e:
            log.warning("auto-login retry failed: %s", str(e)[:200])
            continue
        async with _login_lock:
            client.set_cookies(
                {k: v for k, v in cookies.items() if v}, clear_cookies=True
            )
            try:
                await client.get_user_by_screen_name(USERNAME.lstrip("@"))
            except Exception as e:
                client.http.cookies.clear()
                log.warning("auto-login cookies rejected: %s", str(e)[:200])
                continue
            _logged_in = True
            try:
                client.save_cookies(COOKIES_FILE)
            except Exception:
                pass
            log.warning("auto-login succeeded")
            return


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
# AUTO_LOGIN_RETRY=1: background task retries browser login every 6h until it
# succeeds (for X-side rate limits that clear with time). Off by default.
AUTO_LOGIN_RETRY = os.getenv("AUTO_LOGIN_RETRY", "") in ("1", "true", "yes")
# COOKIES_JSON: paste the whole cookies.json content as an env var (easiest
# path on Render: dashboard -> Environment -> add secret). Takes precedence
# over COOKIES_FILE when set and non-empty.
COOKIES_JSON = os.getenv("COOKIES_JSON", "")
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
    """Log in once per process lifetime; reuse session cookies after that.

    NOTE: X retired password-based onboarding (LoginFlow) — it answers 131/
    366 to every HTTP client. The working path is cookies: place a
    browser-exported cookies.json at COOKIES_FILE (Render Secret File) and
    login is bypassed via load_cookies.
    """
    import json as _json
    import os as _os

    global _logged_in
    async with _login_lock:
        if _logged_in:
            return
        if COOKIES_JSON.strip():
            try:
                data = _json.loads(COOKIES_JSON)
            except ValueError as e:
                raise HTTPException(
                    status_code=500, detail=f"COOKIES_JSON is not valid JSON: {e}"
                )
            cookies = data if isinstance(data, dict) else {
                c.get("name"): c.get("value")
                for c in data if isinstance(c, dict) and c.get("name")
            }
            client.set_cookies({k: v for k, v in cookies.items() if v})
            _logged_in = True
            return
        if COOKIES_FILE and _os.path.exists(COOKIES_FILE):
            client.load_cookies(COOKIES_FILE)
            _logged_in = True
            return
        if not all([USERNAME, EMAIL, PASSWORD]):
            raise HTTPException(
                status_code=500,
                detail="Missing env vars: set TWITTER_USERNAME, TWITTER_EMAIL, TWITTER_PASSWORD",
            )
        await prime_cookies()
        try:
            await client.login(
                auth_info_1=USERNAME,
                auth_info_2=EMAIL,
                password=PASSWORD,
                totp_secret=TOTP_SECRET,
                cookies_file=COOKIES_FILE,
            )
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=(
                    "Password login failed (X retired LoginFlow for HTTP clients). "
                    "Export cookies.json from a logged-in browser and mount it as "
                    f"a Render Secret File at {COOKIES_FILE}. Cause: {type(e).__name__}: {e}"
                ),
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


class CookiesIn(BaseModel):
    """Browser-exported cookies: either a name->value dict or a list of
    {name, value} objects (extension export format)."""

    cookies: dict | list


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/cookies")
async def import_cookies(body: CookiesIn, x_api_key: str | None = Header(default=None)) -> dict:
    """Import browser cookies at runtime (no redeploy needed) and verify them
    with a live authed call. Protected when API_KEY is set."""
    check_api_key(x_api_key)
    global _logged_in
    data = body.cookies
    cookies = data if isinstance(data, dict) else {
        c.get("name"): c.get("value")
        for c in data if isinstance(c, dict) and c.get("name")
    }
    cookies = {k: v for k, v in cookies.items() if v}
    if "auth_token" not in cookies or "ct0" not in cookies:
        raise HTTPException(
            status_code=400,
            detail="cookies must include at least auth_token and ct0",
        )
    async with _login_lock:
        _logged_in = False
        client.set_cookies(cookies, clear_cookies=True)
        try:
            me = await client.get_user_by_screen_name((USERNAME or "").lstrip("@") or "x")
        except Exception as e:
            client.http.cookies.clear()
            raise HTTPException(
                status_code=401,
                detail=f"cookies rejected by X: {type(e).__name__}: {str(e)[:200]}",
            )
        _logged_in = True
        try:
            client.save_cookies(COOKIES_FILE)
            saved = COOKIES_FILE
        except Exception:
            saved = None
        return {"logged_in": True, "id": me.id, "name": me.name, "saved_to": saved}


@app.get("/debug-login-submit")
async def debug_login_submit_ep(identifier: str = "rvndkaswan@gmail.com") -> dict:
    """Fill identifier and dump live submit controls without submitting."""
    try:
        from browser_login import debug_login_submit

        return await asyncio.wait_for(
            debug_login_submit(client._user_agent, identifier), timeout=400
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="debug timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)[:300]}")


@app.get("/debug-login-page")
async def debug_login_page_ep() -> dict:
    """Report what headless Chromium sees on x.com/login (selector debugging)."""
    try:
        from browser_login import debug_login_page

        return await asyncio.wait_for(
            debug_login_page(client._user_agent), timeout=300
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="debug timed out")
    except ImportError:
        raise HTTPException(status_code=500, detail="playwright not installed")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {str(e)[:300]}")


@app.post("/bootstrap-login")
async def bootstrap_login(x_api_key: str | None = Header(default=None)) -> dict:
    """Mint fresh auth cookies via headless Chromium using the TWITTER_*
    env credentials, then verify with a live authed call. Needed because X
    retired password login for plain HTTP clients. Protected when API_KEY
    is set. Takes 1-3 minutes (browser install on first use)."""
    check_api_key(x_api_key)
    if not all([USERNAME, EMAIL, PASSWORD]):
        raise HTTPException(
            status_code=500,
            detail="Missing env vars: TWITTER_USERNAME, TWITTER_EMAIL, TWITTER_PASSWORD",
        )
    global _logged_in
    async with _login_lock:
        _logged_in = False
        try:
            from browser_login import browser_bootstrap

            cookies = await asyncio.wait_for(
                browser_bootstrap(USERNAME, EMAIL, PASSWORD, client._user_agent),
                timeout=600,
            )
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="browser login timed out")
        except RuntimeError as e:
            raise HTTPException(status_code=422, detail=str(e)[:500])
        except ImportError:
            raise HTTPException(
                status_code=500, detail="playwright not installed in this runtime"
            )
        client.set_cookies(
            {k: v for k, v in cookies.items() if v}, clear_cookies=True
        )
        try:
            me = await client.get_user_by_screen_name(USERNAME.lstrip("@"))
        except Exception as e:
            client.http.cookies.clear()
            raise HTTPException(
                status_code=401,
                detail=f"browser cookies rejected by X: {type(e).__name__}: {str(e)[:200]}",
            )
        _logged_in = True
        try:
            client.save_cookies(COOKIES_FILE)
            saved = COOKIES_FILE
        except Exception:
            saved = None
        return {"logged_in": True, "id": me.id, "name": me.name, "saved_to": saved}


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
    # Brave site: search (DDG 202s Render IPs; Brave may work).
    try:
        from web_search_fallback import brave_tweet_ids as _bt

        steps["brave_ids"] = str(
            await _bt("python", guest._user_agent, limit=5)
        )[:200]
    except Exception as e:
        steps["brave_ids"] = f"FAIL: {type(e).__name__}: {str(e)[:150]}"
    # DDG site: search for X status URLs (keyword search via search engine).
    try:
        import re as _re2
        from urllib.parse import quote as _q2

        async with httpx.AsyncClient(
            transport=CurlCffiTransport("chrome", fresh_session_per_request=True)
        ) as ddgs:
            rd = await ddgs.get(
                "https://html.duckduckgo.com/html/?q="
                + _q2("site:x.com python"),
                headers={"User-Agent": guest._user_agent},
                timeout=60,
            )
            urls = sorted(
                set(_re2.findall(r"x\.com/[A-Za-z0-9_]+/status/(\d+)", rd.text))
            )
            steps["ddg_search"] = f"status={rd.status_code} ids={urls[:8]}"
    except Exception as e:
        steps["ddg_search"] = f"FAIL: {type(e).__name__}: {str(e)[:150]}"
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
            try:
                import json as _json

                res = r2.json()["data"]["user"]["result"]

                def keys(o, depth=0):
                    if isinstance(o, dict):
                        return {
                            k: keys(v, depth + 1) if depth < 2 else "..."
                            for k, v in o.items()
                        }
                    if isinstance(o, list):
                        return [keys(o[0], depth + 1)] if o else []
                    return type(o).__name__

                steps["schema"] = _json.dumps(keys(res))[:3000]
            except Exception as e:
                steps["schema"] = f"FAIL: {type(e).__name__}: {r2.text[:200]}"
            # Tweet schema dump via twikit's own TweetResultByRestId path
            try:
                from twikit.utils import flatten_params

                tv = {
                    "tweetId": "20",
                    "withCommunity": False,
                    "includePromotedContent": False,
                    "withVoice": False,
                }
                from twikit.constants import TWEET_RESULT_BY_REST_ID_FEATURES as TWEET_FEATURES

                tparams = flatten_params(
                    {
                        "variables": tv,
                        "features": TWEET_FEATURES,
                        "fieldToggles": {
                            "withArticleRichContentState": True,
                            "withArticlePlainText": False,
                            "withGrokAnalyze": False,
                        },
                    }
                )
                import urllib.parse as _up

                turl = (
                    "https://x.com/i/api/graphql/snmujSvB_9WXyd8yjvZ24Q/TweetResultByRestId?"
                    + _up.urlencode(tparams)
                )
                rt = await s.get(url=turl, headers=base_gql_h, timeout=60)
                tdata = rt.json()["data"]["tweetResult"]["result"]
                steps["tweet_schema"] = _json.dumps(keys(tdata))[:2500]
            except Exception as e:
                steps["tweet_schema"] = f"FAIL: {type(e).__name__}: {str(e)[:200]}"
            # I) same URL but with twikit's _base_headers (has OAuth2Session auth-type)
            r3 = await s.get(url, headers=dict(guest._base_headers, **{"x-guest-token": gt}), timeout=60)
            steps["graphql_baseh"] = f"status={r3.status_code} {r3.text[:200]}"
            # J) raw SearchTimeline guest call (twikit FEATURES + fieldToggles)
            from twikit.utils import flatten_params as _fp
            from twikit.constants import FEATURES as _TF

            sparams = _fp(
                {
                    "variables": {
                        "rawQuery": "python",
                        "count": 20,
                        "querySource": "typed_query",
                        "product": "Top",
                        "withQuickPromoteEligibilityTweetFields": True,
                    },
                    "features": _TF,
                    "fieldToggles": {"withArticleRichContentState": False},
                }
            )
            # flatten_params already JSON-encodes nested values
            from twifork_constants_ref import SEARCH_TIMELINE_FEATURES as _STF

            sparams = _fp(
                {
                    "variables": {
                        "rawQuery": "python",
                        "count": 20,
                        "querySource": "typed_query",
                        "product": "Top",
                    },
                    "features": _STF,
                }
            )
            for qid in (
                "BGd0T_j7oVwlW5U79tO_0A",  # twifork Aug-2026
                "KPSo2_UWdOMpPJwjhfT1Qg",  # scraped from main bundle Sep-2026
            ):
                surl = (
                    f"https://x.com/i/api/graphql/{qid}/SearchTimeline?"
                    + _up.urlencode(sparams)
                )
                rs = await s.get(url=surl, headers=base_gql_h, timeout=60)
                steps[f"search_{qid[:6]}"] = f"status={rs.status_code} {rs.text[:200]}"
            # K) adaptive (URT/REST) search endpoint
            aurl = (
                "https://x.com/i/api/2/search/adaptive.json?"
                + _up.urlencode(
                    {
                        "q": "python",
                        "query_source": "typed_query",
                        "count": 5,
                        "tweet_search_mode": "live",
                    }
                )
            )
            ra = await s.get(url=aurl, headers=base_gql_h, timeout=60)
            steps["search_adaptive"] = f"status={ra.status_code} {ra.text[:300]}"
            # K2) typeahead endpoint (user search alternative)
            turl2 = (
                "https://x.com/i/api/2/search/typeahead.json?"
                + _up.urlencode(
                    {"q": "elon", "src": "search_box", "result_type": "users"}
                )
            )
            rt2 = await s.get(url=turl2, headers=base_gql_h, timeout=60)
            steps["search_typeahead"] = f"status={rt2.status_code} {rt2.text[:300]}"
            # K3) v1.1 REST search endpoints with guest token (trends/place
            # worked guest-side, so these might too)
            for label, v1url in (
                ("v1_tweets", "https://api.x.com/1.1/search/tweets.json?" + _up.urlencode({"q": "python", "count": 5, "tweet_mode": "extended"})),
                ("v1_users", "https://api.x.com/1.1/users/search.json?" + _up.urlencode({"q": "elon", "count": 5})),
            ):
                try:
                    rv = await s.get(url=v1url, headers=base_gql_h, timeout=60)
                    steps[f"search_{label}"] = f"status={rv.status_code} {rv.text[:250]}"
                except Exception as e:
                    steps[f"search_{label}"] = f"FAIL: {type(e).__name__}: {str(e)[:150]}"
            # L) capture EXACT twikit Flow request (transport fix now live)
            import httpx as _hx2

            cap2: dict = {}

            class CapT2(_hx2.AsyncBaseTransport):
                async def handle_async_request(self, request):
                    cap2["url"] = str(request.url)
                    cap2["headers"] = {
                        k: v for k, v in request.headers.items()
                        if "cookie" not in k.lower()
                    }
                    cap2["body"] = (await request.aread()).decode()[:800]
                    return _hx2.Response(
                        200, headers={},
                        content=b'{"flow_token":"FT","subtasks":[]}',
                        request=request,
                    )

            from twikit import Client as TwikitClient2
            from twikit.utils import Flow as Flow2

            c3 = TwikitClient2("en-US", transport=CapT2())
            f3 = Flow2(c3, gt)
            _body = {
                "input_flow_data": {
                    "flow_context": {
                        "debug_overrides": {},
                        "start_location": {"location": "splash_screen"},
                    }
                },
                "subtask_versions": {"a": 1},
            }
            try:
                await f3.execute_task(params={"flow_name": "login"}, data=_body)
            except Exception as e:
                cap2["err"] = f"{type(e).__name__}"
            import json as _j2

            steps["flow_capture"] = _j2.dumps(cap2)[:1200]
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
    # N2) keep ONLY guest_activate response cookies (gt), drop homepage ones,
    # then retry flow — X may need gt cookie, Cloudflare hates homepage ones.
    try:
        keep = {
            c.name: c.value
            for c in client.http.cookies.jar
            if c.name in ("gt", "__cf_bm", "ct0")
        }
        client.set_cookies(keep, clear_cookies=True)
        steps["cookies_kept"] = sorted(keep.keys())
        flown = Flow(client, token)
        await flown.execute_task(params={"flow_name": "login"}, data={})
        steps["flow_gtcookie"] = f"ok, task={flown.task_id}"
    except Exception as e:
        steps["flow_gtcookie"] = f"FAIL: {type(e).__name__}: {str(e)[:300]}"
    try:
        flow = Flow(client, token)
        await flow.execute_task(params={"flow_name": "login"}, data={})
        steps["flow_login"] = f"ok, task={flow.task_id}"
    except Exception as e:
        steps["flow_login"] = f"FAIL: {type(e).__name__}: {str(e)[:300]}"
    # M) minimal raw first call: no subtask_versions, no subtask_inputs
    try:
        import httpx
        from curl_transport import CurlCffiTransport

        async with httpx.AsyncClient(
            transport=CurlCffiTransport("chrome", fresh_session_per_request=True)
        ) as ms:
            mh = {
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
            }
            rm = await ms.post(
                "https://api.x.com/1.1/onboarding/task.json",
                headers=mh,
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
            steps["flow_minimal"] = f"status={rm.status_code} {rm.text[:250]}"
            # N) v1.1 trends/place with guest token (is v1.1 REST guest-OK?)
            rn = await ms.get(
                "https://api.x.com/1.1/trends/place.json?id=1",
                headers=mh,
                timeout=60,
            )
            steps["trends_guest"] = f"status={rn.status_code} {rn.text[:250]}"
    except Exception as e:
        steps["flow_minimal"] = f"FAIL: {type(e).__name__}: {str(e)[:200]}"
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
            # O) new jfapi login entrypoint (no castle_token — see what it says)
            r4 = await fresh4.post(
                "https://x.com/i/jfapi/onboarding/web/actions/begin_login",
                headers={k: v for k, v in h1.items() if k != "x-guest-token"},
                json={},
                timeout=60,
            )
            steps["exp_jfapi"] = f"status={r4.status_code} {r4.text[:300]}"
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
    # Authed API search when logged in; otherwise fall back to headless
    # logged-out page scraping (guest SearchTimeline is blocked by X).
    if _logged_in:
        tweets = await client.search_tweet(q, product, count=count)
        return [tweet_to_dict(t) for t in tweets]
    try:
        await ensure_login()
        tweets = await client.search_tweet(q, product, count=count)
        return [tweet_to_dict(t) for t in tweets]
    except HTTPException:
        pass
    # No-auth fallbacks (best effort):
    # 1. @handle query -> that user's recent timeline via guest.
    if q.strip().startswith("@"):
        try:
            await ensure_guest()
            u = await guest.get_user_by_screen_name(q.strip().lstrip("@"))
            tweets = await u.get_tweets("Tweets", count=count)
            out = [tweet_to_dict(t) for t in tweets]
            if out:
                return out
        except Exception:
            pass
    # 2. Keyword search via Brave (keyless) -> tweet IDs -> guest fetch.
    try:
        from web_search_fallback import brave_tweet_ids

        ids = await asyncio.wait_for(
            brave_tweet_ids(q, guest._user_agent), timeout=120
        )
        if ids:
            await ensure_guest()
            out = []
            for tid in ids[:count]:
                try:
                    out.append(tweet_to_dict(await guest.get_tweet_by_id(tid)))
                except Exception:
                    continue
            if out:
                return out
    except Exception:
        pass
    # 3. Headless logged-out page scrape (X login-walls it, usually []).
    try:
        from browser_login import browser_search

        out = await asyncio.wait_for(
            browser_search(q, client._user_agent, max_tweets=count), timeout=300
        )
        if out:
            return out
    except Exception:
        pass
    raise HTTPException(
        status_code=503,
        detail=(
            "keyword search needs auth (X login-walls logged-out search). "
            "Use q=@handle for user timelines, or import cookies via "
            "POST /cookies / COOKIES_JSON for full search."
        ),
    )


@app.get("/search-users")
async def search_users(q: str = Query(...), count: int = Query(20, ge=1, le=20)):
    # Authed user-search when logged in; otherwise resolve the query as an
    # exact screen_name via guest lookup (X blocks guest user-search).
    if _logged_in:
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
    try:
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
    except HTTPException:
        pass
    await ensure_guest()
    candidates = [q.lstrip("@")]
    # also try without common separators
    if "_" in q:
        candidates.append(q.replace("_", ""))
    for handle in candidates[:3]:
        try:
            u = await guest.get_user_by_screen_name(handle)
            return [
                {
                    "id": u.id,
                    "name": u.name,
                    "screen_name": u.screen_name,
                    "followers": u.followers_count,
                }
            ]
        except Exception:
            continue
    return []


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
    woeid: int = Query(1, description="Yahoo WOEID, 1 = worldwide"),
):
    await ensure_guest()
    items = await guest.get_trends(woeid)
    return [
        {"name": t.get("name"), "tweet_count": t.get("tweet_volume")}
        for t in items[:count]
    ]


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
