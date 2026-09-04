"""Tests for the non-booking login diagnostic mode."""

import unittest
from unittest.mock import Mock, patch

import main


class _Config:
    def __init__(self, path):
        self.path = path

    def load(self):
        return None

    def get_user_info(self):
        return {"login_name": "student", "password": "secret"}

    def get_settings(self):
        return {
            "login_max_attempts": 3,
            "login_retry_initial_delay_seconds": 0,
            "login_retry_max_delay_seconds": 0,
            "login_retry_jitter_ratio": 0,
        }


class _Session:
    def __init__(self, config, success=True, uid="410676", error=None):
        self.config = config
        self.success = success
        self.uid = uid
        self._uid_outcome = uid
        self.name = "reader"
        self.error = error
        self.login_calls = 0

    def init_session(self):
        return None

    def login(self):
        self.login_calls += 1
        self.uid = self._uid_outcome
        return self.success, self.error


class LoginCheckTests(unittest.TestCase):
    def _run(self, session):
        summary = Mock()
        with patch("seathunter.config.manager.ConfigManager", _Config), \
             patch("seathunter.auth.session_manager.SessionManager", return_value=session), \
             patch("seathunter.logging_.logger.setup_logging", return_value=Mock()), \
             patch("seathunter.scheduler.one_shot.append_github_summary", summary):
            code = main.run_login_check("config/ci.yaml")
        return code, summary

    def test_valid_uid_passes_without_booking(self):
        session = _Session(None)

        code, summary = self._run(session)

        self.assertEqual(code, 0)
        self.assertEqual(session.login_calls, 1)
        self.assertIn("Login check passed", summary.call_args.args[0][0])

    def test_redirect_without_uid_fails_validation(self):
        session = _Session(None, success=True, uid="")

        code, summary = self._run(session)

        self.assertEqual(code, 1)
        self.assertIn("Login check failed", summary.call_args.args[0][0])

    def test_login_error_fails_validation(self):
        session = _Session(None, success=False, uid="", error="auth")

        code, summary = self._run(session)

        self.assertEqual(code, 1)
        self.assertIn("auth", " ".join(summary.call_args.args[0]))


if __name__ == "__main__":
    unittest.main()
