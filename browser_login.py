"""Headless-browser login for X using Playwright.

X retired password-based onboarding for plain HTTP clients, so the only way
to mint fresh auth cookies server-side is a real browser that can run the
login JavaScript. This module drives https://x.com/login with the account
credentials from env vars and returns the resulting cookie jar as a dict.

Runs on demand only (Render free tier has ~512MB RAM — the browser is
launched, used, and closed inside a single call).
"""

import asyncio
import re
import shutil
import subprocess
import sys

LOGIN_URL = "https://x.com/login"
LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--single-process",
    "--no-zygote",
    "--disable-extensions",
]


async def _ensure_browser() -> None:
    """Install the Chromium build on first use (Render has no persistent disk)."""
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as p:
            b = await p.chromium.launch(headless=True, args=LAUNCH_ARGS)
            await b.close()
            return
    except Exception as e:
        if "Executable doesn't exist" not in str(e) and "executable" not in str(
            e
        ).lower():
            raise
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "playwright",
        "install",
        "chromium",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"playwright install failed: {out.decode()[-2000:]}")


async def _click_button(page, names: list[str], timeout: int = 15000) -> bool:
    for name in names:
        try:
            await page.get_by_role("button", name=name).first.click(timeout=timeout)
            return True
        except Exception:
            continue
    return False


async def _page_text(page) -> str:
    try:
        return (await page.content())[0:4000]
    except Exception:
        return ""


async def debug_login_submit(user_agent: str, identifier: str) -> dict:
    """Fill the login identifier and dump the live submit controls + forms
    WITHOUT submitting (finds the right button)."""
    from playwright.async_api import async_playwright

    await _ensure_browser()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=LAUNCH_ARGS)
        try:
            ctx = await browser.new_context(
                user_agent=user_agent,
                locale="en-US",
                viewport={"width": 1280, "height": 800},
            )
            page = await ctx.new_page()
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=90000)
            out: dict = {"start_url": page.url}
            for _ in range(20):
                await page.wait_for_timeout(6000)
                try:
                    loc = page.locator("#jf-input-username_or_email").first
                    await loc.wait_for(state="visible", timeout=3000)
                    out["form_found"] = True
                    break
                except Exception:
                    continue
            else:
                out["form_found"] = False
                return out
            await page.locator("#jf-input-username_or_email").first.fill(identifier)
            await page.wait_for_timeout(3000)
            forms = await page.locator("form:visible").evaluate_all(
                "els => els.map(e => (e.action || '(no-action)') + '|' + (e.innerHTML.slice(0, 120)))"
            )
            out["forms"] = forms[:4]
            btns = await page.locator(
                "button:visible, div[role=button]:visible, input[type=submit]:visible"
            ).evaluate_all(
                "els => els.map(e => ((e.innerText || e.value || e.getAttribute('aria-label') || e.type || '') + '').trim()).filter(t => t)"
            )
            out["buttons"] = btns[:15]
            out["url"] = page.url
            return out
        finally:
            await browser.close()


async def browser_search(query: str, user_agent: str, max_tweets: int = 10) -> list[dict]:
    """Scrape logged-out search results page via headless Chromium.

    X blocks SearchTimeline for guest API tokens, but the logged-out
    /search page renders tweet articles in-DOM. Returns
    [{id, text, user, created_at}] (best effort, may be empty).
    """
    import re as _re
    from urllib.parse import quote as _quote
    from playwright.async_api import async_playwright

    await _ensure_browser()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=LAUNCH_ARGS)
        try:
            ctx = await browser.new_context(
                user_agent=user_agent,
                locale="en-US",
                viewport={"width": 1280, "height": 2000},
            )
            page = await ctx.new_page()
            url = (
                "https://x.com/search?q=" + _quote(query)
                + "&src=typed_query&f=live"
            )
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=90000)
            except Exception as e:
                raise RuntimeError(f"search page load failed: {e!r}"[:300])
            # Wait for tweet articles to hydrate (challenge may hold first).
            n = 0
            for _ in range(20):
                await page.wait_for_timeout(6000)
                try:
                    n = await page.locator('article[data-testid="tweet"]').count()
                except Exception:
                    n = 0
                if n:
                    break
            if not n:
                try:
                    body_txt = await page.locator("body").evaluate(
                        "b => b.innerText.slice(0, 400)"
                    )
                except Exception:
                    body_txt = ""
                raise RuntimeError(
                    f"no tweets rendered. url={page.url} body={body_txt!r}"[:500]
                )
            try:
                cards = await page.locator('article[data-testid="tweet"]').evaluate_all(
                    """els => els.slice(0, 30).map(a => {
                        const t = a.querySelector('div[data-testid="tweetText"]');
                        const time = a.querySelector('time');
                        const user = a.querySelector('div[data-testid="User-Name"]');
                        const link = a.querySelector('a[href*="/status/"]');
                        return {
                            text: t ? t.innerText.slice(0, 500) : '',
                            created_at: time ? (time.getAttribute('datetime') || '') : '',
                            user: user ? user.innerText.split('\\n')[0].slice(0, 80) : '',
                            status_href: link ? (link.getAttribute('href') || '') : ''
                        };
                    })"""
                )
            except Exception as e:
                raise RuntimeError(f"scrape failed: {e!r}"[:300])
            out: list[dict] = []
            for c in cards:
                m = _re.search(r"/status/(\d+)", c.get("status_href") or "")
                if not c.get("text") and not m:
                    continue
                out.append(
                    {
                        "id": m.group(1) if m else "",
                        "text": c.get("text", ""),
                        "user": {"name": c.get("user", ""), "screen_name": ""},
                        "created_at": c.get("created_at", ""),
                    }
                )
                if len(out) >= max_tweets:
                    break
            return out
        finally:
            await browser.close()


async def debug_login_page(user_agent: str) -> dict:
    """Load x.com/login headlessly and report what renders (for selector debugging)."""
    from playwright.async_api import async_playwright

    await _ensure_browser()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=LAUNCH_ARGS)
        try:
            ctx = await browser.new_context(
                user_agent=user_agent,
                locale="en-US",
                viewport={"width": 1280, "height": 800},
            )
            page = await ctx.new_page()
            try:
                await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=90000)
            except Exception as e:
                return {"goto": f"FAIL: {type(e).__name__}: {str(e)[:200]}"}
            await page.wait_for_timeout(8000)
            html = await page.content()
            inputs = await page.locator("input").evaluate_all(
                "els => els.map(e => e.outerHTML.slice(0, 160))"
            )
            buttons = await page.locator("button, div[role=button]").evaluate_all(
                "els => els.map(e => (e.innerText || '').slice(0, 40))"
            )
            low = html.lower()
            csp = await page.locator(
                "script[src*='challenge-platform'], iframe[src*='challenge'], iframe[src*='arkose'], iframe[src*='captcha']"
            ).evaluate_all("els => els.map(e => e.outerHTML.slice(0, 200))")
            # Wait to see if a managed challenge auto-clears.
            waited_inputs: list[str] = []
            authed_hint = ""
            for _ in range(10):
                await page.wait_for_timeout(6000)
                try:
                    waited_inputs = await page.locator("input").evaluate_all(
                        "els => els.map(e => e.outerHTML.slice(0, 160))"
                    )
                except Exception:
                    waited_inputs = []
                if waited_inputs:
                    break
                try:
                    if "auth_token" in [c["name"] for c in await ctx.cookies()]:
                        authed_hint = "auth_token appeared"
                        break
                except Exception:
                    pass
            return {
                "url": page.url,
                "title": await page.title(),
                "html_len": len(html),
                "inputs": inputs[:8],
                "buttons": [b for b in buttons if b.strip()][:12],
                "has_challenge": any(
                    s in low
                    for s in ["arkose", "funcaptcha", "challenge-platform", "attention required"]
                ),
                "challenge_nodes": csp[:4],
                "body_snippet": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))[:600],
                "waited_inputs": waited_inputs[:8],
                "authed_hint": authed_hint,
            }
        finally:
            await browser.close()


async def browser_bootstrap(username: str, email: str, password: str, user_agent: str) -> dict:
    """Log in via headless Chromium. Returns cookies dict on success.

    Raises RuntimeError with a human-readable state on any blocking screen
    (captcha, email-code challenge, suspension, bad credentials).
    """
    from playwright.async_api import async_playwright

    await _ensure_browser()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=LAUNCH_ARGS)
        try:
            ctx = await browser.new_context(
                user_agent=user_agent,
                locale="en-US",
                viewport={"width": 1280, "height": 800},
            )
            page = await ctx.new_page()
            await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=90000)

            async def visible_inputs() -> list:
                try:
                    return await page.locator("input:visible").evaluate_all(
                        "els => els.map(e => e.outerHTML.slice(0, 200))"
                    )
                except Exception:
                    return []

            async def authed() -> dict:
                try:
                    return {c["name"]: c["value"] for c in await ctx.cookies()}
                except Exception:
                    return {}

            # Wait for the app to hydrate (Cloudflare managed challenge can
            # hold the page for up to ~a minute on datacenter IPs).
            user_input = None
            for _ in range(20):
                await page.wait_for_timeout(6000)
                if "auth_token" in await authed():
                    return await authed()
                for sel in (
                    "#jf-input-username_or_email",
                    'input[autocomplete="username"], input[name="text"]',
                ):
                    try:
                        loc = page.locator(sel).first
                        await loc.wait_for(state="visible", timeout=3000)
                        user_input = loc
                        break
                    except Exception:
                        continue
                if user_input is not None:
                    break
            if user_input is None:
                raise RuntimeError(
                    "username field never appeared. inputs="
                    + str(await visible_inputs())[:600]
                )

            # Step 1: identifier. The jf "Use password" screen can show TWO
            # fields (username + email/phone confirmation): fill every
            # visible text input — first gets the username, any other gets
            # the email.
            ids = [username.lstrip("@")]
            if email and email not in ids:
                ids.append(email)
            try:
                fields = page.locator(
                    "#jf-input-username_or_email, input:visible:not([type=password]):not([type=hidden]):not([type=checkbox])"
                )
                n = await fields.count()
            except Exception:
                n = 0
            if n >= 2:
                for i in range(min(n, len(ids) + 1)):
                    try:
                        await fields.nth(i).fill(ids[min(i, len(ids) - 1)])
                    except Exception:
                        pass
            else:
                await user_input.fill(ids[-1])
            await page.wait_for_timeout(2500)
            # jf form's submit is a bare "Continue" button. Match it exactly:
            # "Continue with phone/Google/Apple" are SSO/signup options.
            submitted = False
            try:
                cont = page.locator("button:visible", has_text="Continue").filter(
                    has_not_text="Continue with"
                ).first
                await cont.wait_for(state="visible", timeout=10000)
                await cont.click(timeout=10000)
                submitted = True
            except Exception:
                pass
            if not submitted:
                try:
                    btns = await page.locator(
                        "button:visible, div[role=button]:visible"
                    ).evaluate_all(
                        "els => els.map(e => (e.innerText || e.getAttribute('aria-label') || '').trim()).filter(t => t)"
                    )
                except Exception:
                    btns = []
                raise RuntimeError(
                    "username-step: exact Continue button missing. buttons="
                    + str(btns)[:400]
                )
            await page.wait_for_timeout(5000)
            # Guard: if we landed on signup, the submit went to the wrong
            # form — reload the login page and retry once via Tab+Enter.
            try:
                if "/signup" in page.url:
                    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=90000)
                    await page.wait_for_timeout(8000)
                    user_input = page.locator("#jf-input-username_or_email").first
                    await user_input.wait_for(state="visible", timeout=25000)
                    await user_input.fill(first_id)
                    await page.wait_for_timeout(2500)
                    await user_input.press("Tab")
                    await page.keyboard.press("Enter")
                    await page.wait_for_timeout(5000)
                    if "/signup" in page.url:
                        raise RuntimeError(
                            "username submit keeps routing to signup; url=" + page.url
                        )
            except RuntimeError:
                raise
            except Exception:
                pass

            # Possible identifier-verification step ("enter email/phone").
            for _ in range(2):
                if "auth_token" in await authed():
                    break
                body = await _page_text(page)
                low = body.lower()
                if any(
                    s in low
                    for s in [
                        "verify your identity",
                        "enter your phone number or",
                        "unusual login activity",
                    ]
                ):
                    ident = page.locator(
                        '#jf-input-username_or_email, input[name="text"]'
                    ).first
                    try:
                        await ident.wait_for(state="visible", timeout=10000)
                        await ident.fill(email)
                        if not await _click_button(page, ["Next", "Continue"]):
                            await ident.press("Enter")
                        await page.wait_for_timeout(5000)
                        continue
                    except Exception:
                        pass
                break

            # Step 2: password (if we are not already in).
            cookies = await authed()
            if "auth_token" not in cookies:
                pwd = page.locator(
                    '#jf-input-password, input[name="password"], input[type="password"]'
                ).first
                try:
                    await pwd.wait_for(state="visible", timeout=25000)
                except Exception:
                    raise RuntimeError(
                        "password field never appeared. inputs="
                        + str(await visible_inputs())[:600]
                    )
                await pwd.fill(password)
                await page.wait_for_timeout(2000)
                try:
                    cont2 = page.locator("button:visible", has_text="Continue").filter(
                        has_not_text="Continue with"
                    ).first
                    await cont2.wait_for(state="visible", timeout=10000)
                    await cont2.click(timeout=10000)
                except Exception:
                    if not await _click_button(page, ["Log in"]):
                        try:
                            await pwd.press("Enter")
                        except Exception:
                            raise RuntimeError("password-step: submit button not found")

            # Wait for login to complete (auth_token cookie or /home).
            authed: dict = {}
            for _ in range(30):
                await page.wait_for_timeout(3000)
                authed = {c["name"]: c["value"] for c in await ctx.cookies()}
                if "auth_token" in authed:
                    break
                try:
                    if "/home" in page.url:
                        await page.wait_for_timeout(3000)
                        authed = {c["name"]: c["value"] for c in await ctx.cookies()}
                        if "auth_token" in authed:
                            break
                except Exception:
                    pass
            else:
                body = await _page_text(page)
                low = body.lower()
                try:
                    vtext = await page.locator("body").evaluate(
                        "b => b.innerText.slice(0, 800)"
                    )
                except Exception:
                    vtext = ""
                state = f"url={page.url} text={vtext[:500]!r}"
                if "captcha" in low or "arkose" in low or "funcaptcha" in low:
                    raise RuntimeError("blocked by CAPTCHA challenge. " + state)
                if "check your inbox" in low or "enter the code" in low or "verification code" in low:
                    raise RuntimeError(
                        "blocked: X emailed a verification code (inbox access needed). " + state
                    )
                if "suspended" in low or "locked" in low:
                    raise RuntimeError("account suspended or locked. " + state)
                if "wrong" in low and "password" in low:
                    raise RuntimeError("wrong password rejected by X. " + state)
                if "temporarily limited" in low or "try again later" in low:
                    raise RuntimeError(
                        "RATE_LIMITED: X temporarily limited logins from this "
                        "IP/account (too many attempts). Wait 30-60 min and retry. " + state
                    )
                if "could not log you in" in low or "try again" in low or "something went wrong" in low:
                    raise RuntimeError("X rejected the login attempt. " + state)
                raise RuntimeError("login did not complete; no auth_token cookie. " + state)

            return authed
        finally:
            await browser.close()
