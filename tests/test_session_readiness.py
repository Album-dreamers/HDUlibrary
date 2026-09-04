"""Checks that an incomplete CAS redirect is never treated as a ready session."""

import unittest
from unittest.mock import patch

from seathunter.auth.session_manager import SessionManager


class _Config:
    def get_user_info(self):
        return {"login_name": "student", "password": "secret"}

    def get_api_base_url(self):
        return "https://example.invalid"


class _CookieStore:
    def __init__(self):
        self.saved = []
        self.cleared = False

    def save(self, *args):
        self.saved.append(args)

    def clear(self):
        self.cleared = True


class _RequestsSession:
    def __init__(self):
        self.cookies = {}


class _Response:
    status_code = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, data=None, error=None):
        self.data = data
        self.error = error

    def json(self):
        if self.error:
            raise self.error
        return self.data


class _SequencedSession(_RequestsSession):
    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.get_calls = 0

    def get(self, **kwargs):
        self.get_calls += 1
        return self.responses.pop(0)


class SessionReadinessTests(unittest.TestCase):
    def test_playwright_redirect_without_uid_is_not_login_success(self):
        manager = SessionManager.__new__(SessionManager)
        manager.config = _Config()
        manager.session = _RequestsSession()
        manager.cookie_store = _CookieStore()
        manager.uid = ""
        manager.name = ""

        with patch(
            "seathunter.auth.session_manager.playwright_login",
            return_value=(True, None, [{"name": "sid", "value": "x"}], "", ""),
        ), patch.object(manager, "_fetch_user_info_from_api", return_value=False):
            success, error_type = manager._login_with_playwright()

        self.assertFalse(success)
        self.assertEqual(error_type, "session")
        self.assertEqual(manager.cookie_store.saved, [])
        self.assertTrue(manager.cookie_store.cleared)

    def test_user_info_retries_same_session_without_repeating_cas(self):
        manager = SessionManager.__new__(SessionManager)
        manager.config = _Config()
        manager.session = _SequencedSession([
            _Response(error=ValueError("empty response")),
            _Response({"data": {"uid": "420001", "uname": "reader"}}),
        ])
        manager.uid = ""
        manager.name = ""

        with patch("seathunter.auth.session_manager.sleep") as sleep:
            ready = manager._fetch_user_info_from_api()

        self.assertTrue(ready)
        self.assertEqual(manager.uid, "420001")
        self.assertEqual(manager.session.get_calls, 2)
        sleep.assert_called_once_with(0.5)

    def test_user_info_retry_uses_bounded_exponential_delays(self):
        manager = SessionManager.__new__(SessionManager)
        manager.config = _Config()
        manager.session = _SequencedSession([
            _Response({"data": {}}),
            _Response({"data": {}}),
            _Response({"data": {}}),
            _Response({"data": {"uid": "420001", "uname": "reader"}}),
        ])
        manager.uid = ""
        manager.name = ""

        with patch("seathunter.auth.session_manager.sleep") as sleep:
            ready = manager._fetch_user_info_from_api(attempts=4)

        self.assertTrue(ready)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.5, 1.0, 2.0])


if __name__ == "__main__":
    unittest.main()
