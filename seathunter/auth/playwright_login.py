"""Playwright CAS SSO login automation.

Extracted from killer.py:128-312.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import logging
from typing import Optional, Tuple, List, Dict
from urllib.parse import urlsplit

logger = logging.getLogger("seathunter.auth")

LOGIN_ERR_NETWORK = "network"
LOGIN_ERR_AUTH = "auth"


def _safe_url(value: str) -> str:
    """Drop query strings because CAS URLs can contain one-time tickets."""
    parsed = urlsplit(value)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def _payload_summary(body: str) -> str:
    """Describe an API response without dumping account data into CI logs."""
    try:
        data = json.loads(body)
    except Exception:
        preview = " ".join(str(body).split())[:240]
        return f"non-json length={len(body)} preview={preview!r}"

    if not isinstance(data, dict):
        return f"json-type={type(data).__name__}"

    details = [f"keys={sorted(str(key) for key in data.keys())}"]
    payload = data.get("data")
    if isinstance(payload, dict):
        details.append(f"data_keys={sorted(str(key) for key in payload.keys())}")
    else:
        details.append(f"data_type={type(payload).__name__}")
    for key in ("CODE", "code", "MESSAGE", "message", "msg"):
        if key in data:
            details.append(f"{key}={str(data[key])[:160]!r}")
    return " ".join(details)


def playwright_login(username: str, password: str, library_url: str,
                     base_url: str) -> Tuple[bool, Optional[str],
                                              Optional[List[Dict]], Optional[str], Optional[str]]:
    """Perform Playwright browser-based HDU CAS SSO login.

    Args:
        username: Student ID.
        password: Login password.
        library_url: The library system URL (for landing page).
        base_url: Base API URL for user info extraction.

    Returns:
        (success, error_type, cookies, uid, name) tuple.
        - success: Whether login succeeded.
        - error_type: "network", "auth", or None.
        - cookies: List of cookie dicts from Playwright.
        - uid: User ID string.
        - name: User display name.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("Playwright not installed. Run: pip install playwright && python -m playwright install chromium")
        return (False, LOGIN_ERR_AUTH, None, "", "")

    async def _login():
        async with async_playwright() as p:
            logger.info("Starting browser for CAS login...")
            launch_opts = {"headless": True}
            if getattr(sys, "frozen", False):
                base = os.path.dirname(sys.executable)
                if sys.platform == "win32":
                    chromium_path = os.path.join(base, "chromium", "chrome-win64", "chrome.exe")
                elif sys.platform == "darwin":
                    chromium_path = os.path.join(base, "chromium", "chrome-mac", "Chromium")
                else:
                    chromium_path = os.path.join(base, "chromium", "chrome-linux", "chrome")
                if os.path.exists(chromium_path):
                    launch_opts["executable_path"] = chromium_path
                else:
                    logger.warning("Bundled Chromium not found at: %s", chromium_path)

            browser = await p.chromium.launch(**launch_opts)
            context = await browser.new_context()
            page = await context.new_page()

            logger.info("Navigating to login page...")
            try:
                await page.goto(library_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                await browser.close()
                return False, None, "", "", str(e)

            # Wait for username input
            logger.info("Waiting for login form...")
            username_selectors = [
                'input[name="username"]',
                'input[formcontrolname="username"]',
                'input[placeholder*="学工号"]',
                'input[type="text"]',
            ]
            username_input = None
            for selector in username_selectors:
                try:
                    username_input = await page.wait_for_selector(selector, timeout=10000)
                    if username_input:
                        break
                except Exception:
                    continue

            if not username_input:
                try:
                    username_input = await page.wait_for_selector(
                        ",".join(username_selectors), timeout=20000
                    )
                except Exception:
                    pass

            if not username_input:
                try:
                    page_text = " ".join(
                        (await page.locator("body").inner_text(timeout=3000)).split()
                    )[:400]
                except Exception:
                    page_text = "(unavailable)"
                logger.error(
                    "[DEBUG-logincheck-form] Could not find username input; "
                    "url=%s title=%r body=%r",
                    _safe_url(page.url),
                    await page.title(),
                    page_text,
                )
                await browser.close()
                return False, None, "", "", "auth"

            logger.info("Filling in credentials...")
            await username_input.fill(str(username))

            # Wait for password input
            password_selectors = [
                'input[type="password"]',
                'input[name="password"]',
                'input[formcontrolname="password"]',
            ]
            password_input = None
            for selector in password_selectors:
                try:
                    password_input = await page.wait_for_selector(selector, timeout=3000)
                    if password_input:
                        break
                except Exception:
                    continue

            if not password_input:
                logger.error("Could not find password input field")
                await browser.close()
                return False, None, "", "", "auth"

            await password_input.fill(str(password))

            # Submit login
            logger.info("Submitting login...")
            login_btn = None
            for selector in ['button[type="submit"]', 'button:has-text("登录")']:
                try:
                    login_btn = await page.wait_for_selector(selector, timeout=3000)
                    if login_btn:
                        break
                except Exception:
                    continue

            if not login_btn:
                logger.error("Could not find login button")
                await browser.close()
                return False, None, "", "", "auth"

            await login_btn.click()

            logger.info("Waiting for login completion...")
            try:
                await page.wait_for_url("**/huitu.zhishulib.com/**", timeout=30000)
            except Exception:
                await asyncio.sleep(5)

            current_url = page.url
            logger.info(
                "[DEBUG-logincheck-redirect] Final login page: url=%s title=%r",
                _safe_url(current_url),
                await page.title(),
            )
            if "huitu.zhishulib.com" not in current_url:
                logger.error("Login may have failed, current URL: %s", _safe_url(current_url))
                await browser.close()
                return False, None, "", "", "auth"

            # Extract cookies
            all_cookies = await context.cookies()
            lib_cookies = [c for c in all_cookies if "huitu.zhishulib.com" in c.get("domain", "")]

            # Get user info
            logger.info("Fetching user info...")
            uid = ""
            name = ""
            resp_info = {}
            fetch_error = ""
            try:
                resp_info = await page.evaluate("""async () => {
                    try {
                        const resp = await fetch("/Seat/Index/searchSeats?space_category[category_id]=591&space_category[content_id]=3&LAB_JSON=1");
                        return {
                            status: resp.status,
                            url: resp.url,
                            redirected: resp.redirected,
                            contentType: resp.headers.get("content-type") || "",
                            body: await resp.text(),
                        };
                    } catch (error) {
                        return {error: String(error), body: ""};
                    }
                }""")
                resp_text = resp_info.get("body", "")
                data = json.loads(resp_text)
                if isinstance(data, dict) and data.get("data"):
                    uid = str(data["data"].get("uid", ""))
                    name = data["data"].get("uname", "")
            except Exception as exc:
                fetch_error = str(exc)
                logger.warning("Failed to get user info from browser: %s", exc)

            if not uid:
                logger.warning(
                    "[DEBUG-logincheck-userinfo] Browser user-info response: "
                    "status=%s redirected=%s url=%s content-type=%r %s",
                    resp_info.get("status", "error"),
                    resp_info.get("redirected", "unknown"),
                    _safe_url(resp_info.get("url", "")),
                    resp_info.get("contentType", ""),
                    _payload_summary(resp_info.get("body", "")) if resp_info else fetch_error,
                )

            if not uid:
                for c in lib_cookies:
                    if c["name"] == "uid":
                        uid = c["value"]
                        break

            logger.info("Got user info: uid=%s, name=%s", uid, name)
            await browser.close()
            return True, lib_cookies, uid, name, ""

    try:
        success, cookies, uid, name, err = asyncio.run(_login())
    except Exception as e:
        logger.error("Login exception: %s", e)
        return (False, LOGIN_ERR_NETWORK, None, "", "")

    if not success or not cookies:
        if err and any(k in str(err) for k in ["CONNECTION_RESET", "CONNECTION_REFUSED", "ERR_NAME", "timeout"]):
            return (False, LOGIN_ERR_NETWORK, None, "", "")
        return (False, LOGIN_ERR_AUTH, None, "", "")

    return (True, None, cookies, uid, name)
