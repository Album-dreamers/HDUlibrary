"""Malformed identities and temporary cookie timeouts must not pass login."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from seathunter.auth.login_retry import login_with_retry
from seathunter.auth.session_manager import SessionManager


class UidValidationTests(unittest.TestCase):
    def manager(self, uid):
        manager = SessionManager.__new__(SessionManager)
        manager.config = SimpleNamespace(
            get_api_base_url=lambda: "https://example.invalid",
            get_user_info=lambda: {"login_name": "test", "password": "test"},
        )
        manager.uid = ""
        manager.name = ""
        manager.session = Mock(cookies={})
        response = Mock(status_code=200, headers={})
        response.json.return_value = {"data": {"uid": uid}}
        manager.session.get.return_value = response
        manager.cookie_store = Mock()
        manager.cookie_store.load.return_value = {"cookies": [{"name": "sid", "value": "test"}]}
        return manager

    def test_malformed_uid_is_not_a_valid_api_identity(self):
        for uid in (None, [], {}, True, False, 0, "", "  ", "None", "null"):
            with self.subTest(uid=uid):
                manager = self.manager(uid)
                self.assertFalse(manager._fetch_user_info_from_api(attempts=1))
                self.assertEqual(manager.uid, "")

    def test_cookie_validation_rejects_malformed_uid(self):
        for uid in ({"nested": 1}, [123], True, "  ", "None"):
            with self.subTest(uid=uid):
                self.assertFalse(self.manager(uid)._login_with_cookies())

    def test_browser_identity_is_validated_before_saving_cookies(self):
        manager = self.manager(None)
        with patch("seathunter.auth.session_manager.playwright_login", return_value=(
            True, None, [{"name": "sid", "value": "test"}], "None", ""
        )), patch.object(manager, "_fetch_user_info_from_api", return_value=False):
            self.assertEqual(manager._login_with_playwright(), (False, "session"))
        manager.cookie_store.save.assert_not_called()

    def test_real_uid_keeps_numeric_and_string_compatibility(self):
        for uid in (410676, "410676"):
            manager = self.manager(uid)
            self.assertTrue(manager._fetch_user_info_from_api(attempts=1))
            self.assertEqual(manager.uid, "410676")

    def test_cookie_read_timeout_retries_without_new_cas_login(self):
        manager = self.manager("410676")
        response = manager.session.get.return_value
        manager.session.get.side_effect = [requests.exceptions.ReadTimeout("temporary"), response]
        delays = []
        with patch.object(manager, "_login_with_playwright") as browser:
            result = login_with_retry(manager, max_attempts=2, initial_delay=30,
                                      jitter_ratio=0, sleep_fn=delays.append)
        self.assertEqual(result, (True, None))
        browser.assert_not_called()
        self.assertEqual(delays, [30])


if __name__ == "__main__":
    unittest.main()
