"""Session manager: login orchestration and cookie auto-refresh.

Combines cookie-based login and Playwright login with auto-relogin support.
"""

from __future__ import annotations

import logging
from time import sleep
from typing import Optional, Tuple, Dict

import requests

from seathunter.auth.cookie_store import CookieStore
from seathunter.auth.playwright_login import playwright_login, LOGIN_ERR_NETWORK
from seathunter.config.manager import ConfigManager
from seathunter.platform_.paths import get_config_path

logger = logging.getLogger("seathunter.auth")

LOGIN_ERR_SESSION = "session"


class SessionManager:
    """Manages HTTP sessions with automatic login and cookie refresh."""

    def __init__(self, config_manager: ConfigManager):
        self.config = config_manager
        self.session: Optional[requests.Session] = None
        self.cookie_store = CookieStore(get_config_path("session.json"))
        self.uid: str = ""
        self.name: str = ""
        self._cookie_login_network_err = False

    @property
    def base_url(self) -> str:
        return self.config.get_api_base_url()

    @property
    def user_info(self) -> Dict[str, str]:
        return self.config.get_user_info()

    def init_session(self):
        """Initialize the requests session with configured headers."""
        import urllib3
        urllib3.disable_warnings()

        session_cfg = self.config.get_session_config()
        self.session = requests.Session()
        self.session.headers.clear()
        self.session.headers = session_cfg.get("headers", {})
        self.session.trust_env = session_cfg.get("trust_env", False)
        self.session.verify = session_cfg.get("verify", False)
        self.session.params = session_cfg.get("params", {})
        self.session.cookies.update({"org_id": self.config.get_user_info().get("org_id", "104")})

    def login(self) -> Tuple[bool, Optional[str]]:
        """Login: try cached cookies first, fall back to Playwright.

        Returns:
            (success, error_type) where error_type is "network", "auth", or None.
        """
        self._cookie_login_network_err = False

        if self._login_with_cookies():
            return (True, None)

        if self._cookie_login_network_err:
            return (False, LOGIN_ERR_NETWORK)

        return self._login_with_playwright()

    def relogin(self) -> Tuple[bool, Optional[str]]:
        """Force re-login using Playwright (skip cookie cache)."""
        self.cookie_store.clear()
        return self._login_with_playwright()

    def _login_with_cookies(self) -> bool:
        """Try to login using cached cookies."""
        cached = self.cookie_store.load()
        if not cached:
            return False

        logger.info("Found cached cookies, attempting to use...")
        cookie_dict = {c["name"]: c["value"] for c in cached["cookies"]}
        self.session.cookies.update(cookie_dict)

        # Validate cookies by making a test request
        try:
            params = {
                "space_category[category_id]": "591",
                "space_category[content_id]": "3",
            }
            url = self.base_url + "/Seat/Index/searchSeats"
            resp = self.session.get(url=url, params=params, timeout=15)
            data = resp.json()
            if isinstance(data, dict) and data.get("data") and data["data"].get("uid"):
                self.uid = str(data["data"]["uid"])
                self.name = data["data"].get("uname", "")
                self.session.cookies.update({"org_id": "104"})
                logger.info("Cookie login successful: uid=%s, name=%s", self.uid, self.name)
                return True
            else:
                logger.info("Cookie validation failed, server returned: %s",
                           json_dumps_truncate(data, 200))
        except (ConnectionResetError, ConnectionError, requests.exceptions.ConnectionError) as e:
            logger.warning("Cookie validation network error: %s", e)
            self._cookie_login_network_err = True
            return False
        except Exception as e:
            logger.warning("Cookie validation error: %s", e)

        logger.info("Cached cookies are invalid")
        return False

    def _login_with_playwright(self) -> Tuple[bool, Optional[str]]:
        """Login using Playwright browser automation."""
        user = self.user_info
        success, err_type, cookies, uid, name = playwright_login(
            username=user.get("login_name", ""),
            password=user.get("password", ""),
            library_url=self.base_url + "/",
            base_url=self.base_url,
        )

        if not success:
            return (False, err_type)

        # Apply cookies to session
        cookie_dict = {c["name"]: c["value"] for c in cookies}
        self.session.cookies.update(cookie_dict)
        self.uid = uid or ""
        self.name = name or ""

        # If uid not obtained from browser, try API
        if not self.uid:
            self._fetch_user_info_from_api()

        # Reaching the library domain is not enough.  A usable booking session
        # must be able to identify the account through the library API.
        if not self.uid:
            logger.error(
                "CAS redirect completed, but the library session is not ready: "
                "user info returned no uid"
            )
            self.cookie_store.clear()
            return (False, LOGIN_ERR_SESSION)

        # Save cookies
        self.cookie_store.save(cookies, self.uid, self.name)
        logger.info("Login successful (cookies saved)")
        return (True, None)

    def _fetch_user_info_from_api(self, attempts: int = 5,
                                  retry_delay: float = 0.5,
                                  max_retry_delay: float = 4.0) -> bool:
        """Obtain and validate the account uid without repeating CAS login."""
        params = {
            "space_category[category_id]": "591",
            "space_category[content_id]": "3",
        }
        url = self.base_url + "/Seat/Index/searchSeats"

        for attempt in range(1, attempts + 1):
            try:
                resp = self.session.get(url=url, params=params, timeout=15)
                data = resp.json()
                if isinstance(data, dict) and data.get("data"):
                    uid = str(data["data"].get("uid", ""))
                    if uid:
                        self.uid = uid
                        self.name = data["data"].get("uname", "")
                        return True
                logger.warning(
                    "User-info validation attempt %d/%d returned no uid "
                    "(status=%s, content-type=%s)",
                    attempt,
                    attempts,
                    getattr(resp, "status_code", "unknown"),
                    getattr(resp, "headers", {}).get("Content-Type", "unknown"),
                )
            except Exception as exc:
                logger.warning(
                    "User-info validation attempt %d/%d failed: %s",
                    attempt,
                    attempts,
                    exc,
                )

            if attempt < attempts:
                delay = min(max_retry_delay, retry_delay * (2 ** (attempt - 1)))
                sleep(delay)

        return False


def json_dumps_truncate(data, max_len=200):
    """Truncated JSON dumps for logging."""
    import json
    s = json.dumps(data, ensure_ascii=False)
    return s[:max_len] + "..." if len(s) > max_len else s
