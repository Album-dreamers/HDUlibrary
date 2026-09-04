"""Login retry policy tests."""

import unittest

from seathunter.auth.login_retry import login_with_retry
from seathunter.auth.playwright_login import _is_credential_error


class _Session:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.uid = "stale"
        self.name = "stale"
        self.calls = 0

    def login(self):
        self.calls += 1
        success, error, uid = self.outcomes.pop(0)
        self.uid = uid
        return success, error


class LoginRetryTests(unittest.TestCase):
    def test_explicit_password_message_is_classified_as_credentials(self):
        self.assertTrue(_is_credential_error("用户名或密码错误，请重新输入"))

    def test_generic_login_page_is_not_classified_as_credentials(self):
        self.assertFalse(_is_credential_error("统一身份认证 登录"))

    def test_transient_failure_retries_and_recovers(self):
        session = _Session([
            (False, "network", ""),
            (True, None, "410676"),
        ])
        delays = []

        result = login_with_retry(
            session,
            max_attempts=3,
            initial_delay=30,
            max_delay=90,
            jitter_ratio=0,
            sleep_fn=delays.append,
        )

        self.assertEqual(result, (True, None))
        self.assertEqual(session.calls, 2)
        self.assertEqual(delays, [30])

    def test_missing_uid_is_retried_as_an_incomplete_session(self):
        session = _Session([
            (True, None, ""),
            (True, None, "410676"),
        ])

        result = login_with_retry(
            session,
            max_attempts=2,
            initial_delay=0,
            jitter_ratio=0,
            sleep_fn=lambda seconds: None,
        )

        self.assertEqual(result, (True, None))
        self.assertEqual(session.calls, 2)

    def test_explicit_credential_error_is_not_retried(self):
        session = _Session([(False, "credentials", "")])
        delays = []

        result = login_with_retry(
            session,
            max_attempts=3,
            sleep_fn=delays.append,
        )

        self.assertEqual(result, (False, "credentials"))
        self.assertEqual(session.calls, 1)
        self.assertEqual(delays, [])

    def test_retry_delay_is_exponential_and_capped(self):
        session = _Session([
            (False, "auth", ""),
            (False, "session", ""),
            (False, "network", ""),
            (True, None, "410676"),
        ])
        delays = []

        result = login_with_retry(
            session,
            max_attempts=4,
            initial_delay=30,
            max_delay=45,
            jitter_ratio=0,
            sleep_fn=delays.append,
        )

        self.assertEqual(result, (True, None))
        self.assertEqual(delays, [30, 45, 45])


if __name__ == "__main__":
    unittest.main()
