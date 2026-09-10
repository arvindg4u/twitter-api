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
            await page.wait_for_timeout(4000)

            # Step 1: username / phone / email.
            user_input = page.locator(
                'input[autocomplete="username"], input[name="text"]'
            ).first
            await user_input.wait_for(timeout=30000)
            await user_input.fill(username)
            if not await _click_button(page, ["Next"]):
                raise RuntimeError("username-step: Next button not found")
            await page.wait_for_timeout(4000)

            # Possible identifier-verification step ("enter email/phone").
            for _ in range(2):
                body = await _page_text(page)
                low = body.lower()
                if "auth_token" in [c["name"] for c in await ctx.cookies()]:
                    break
                if any(
                    s in low
                    for s in [
                        "verify your identity",
                        "enter your phone number or",
                        "unusual login activity",
                    ]
                ):
                    ident = page.locator('input[name="text"]').first
                    try:
                        await ident.wait_for(timeout=10000)
                        await ident.fill(email)
                        await _click_button(page, ["Next"])
                        await page.wait_for_timeout(4000)
                        continue
                    except Exception:
                        pass
                break

            # Step 2: password (if we are not already in).
            cookies = {c["name"]: c["value"] for c in await ctx.cookies()}
            if "auth_token" not in cookies:
                pwd = page.locator('input[name="password"], input[type="password"]').first
                try:
                    await pwd.wait_for(timeout=20000)
                except Exception:
                    body = await _page_text(page)
                    raise RuntimeError(
                        "password field never appeared. page snapshot: "
                        + body[body.lower().find("error") - 100 : body.lower().find("error") + 300]
                        if "error" in body.lower()
                        else body[:500]
                    )
                await pwd.fill(password)
                if not await _click_button(page, ["Log in"]):
                    raise RuntimeError("password-step: Log in button not found")

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
                if "captcha" in low or "arkose" in low or "funcaptcha" in low:
                    raise RuntimeError("blocked by CAPTCHA challenge")
                if "check your inbox" in low or "enter the code" in low or "verification code" in low:
                    raise RuntimeError("blocked: X emailed a verification code (inbox access needed)")
                if "suspended" in low or "locked" in low:
                    raise RuntimeError("account suspended or locked")
                if "wrong" in low and "password" in low:
                    raise RuntimeError("wrong password rejected by X")
                raise RuntimeError("login did not complete; no auth_token cookie. url=" + page.url)

            return authed
        finally:
            await browser.close()
