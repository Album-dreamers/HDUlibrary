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

logger = logging.getLogger("seathunter.auth")

LOGIN_ERR_NETWORK = "network"
LOGIN_ERR_AUTH = "auth"


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
                logger.error("Could not find username input field")
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
            if "huitu.zhishulib.com" not in current_url:
                logger.error("Login may have failed, current URL: %s", current_url)
                await browser.close()
                return False, None, "", "", "auth"

            # Extract cookies
            all_cookies = await context.cookies()
            lib_cookies = [c for c in all_cookies if "huitu.zhishulib.com" in c.get("domain", "")]

            # Get user info
            logger.info("Fetching user info...")
            uid = ""
            name = ""
            try:
                resp_text = await page.evaluate("""async () => {
                    const resp = await fetch("/Seat/Index/searchSeats?space_category[category_id]=591&space_category[content_id]=3&LAB_JSON=1");
                    return await resp.text();
                }""")
                data = json.loads(resp_text)
                if isinstance(data, dict) and data.get("data"):
                    uid = str(data["data"].get("uid", ""))
                    name = data["data"].get("uname", "")
            except Exception as e:
                logger.warning("Failed to get user info from browser: %s", e)

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
