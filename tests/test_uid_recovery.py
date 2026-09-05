"""End-to-end checks on run_once's uid handling.

These drive the real run_once against fakes so the recovery path is
exercised as written, not as described.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main


CONFIG_TEMPLATE = """
user:
  login_name: ""
  password: ""
  org_id: "104"
plans:
  - id: "p1"
    room_name: "R"
    floor_name: "F"
    begin_time: "10:00:00"
    duration_hours: 11
    seats:
      - seat_id: "62561"
        seat_num: "421"
schedules:
  - mode: weekdays
    target_weekdays: [1, 2, 3, 4, 5, 6, 7]
    plan_ids: ["p1"]
    enabled: true
settings:
  interval: 0
  max_try_times: 3
  session_refresh_lead_seconds: 60
  login_max_attempts: 3
  login_retry_initial_delay_seconds: 0
  login_retry_max_delay_seconds: 0
  login_retry_jitter_ratio: 0
  booking_open_time: "{open_time}"
  booking_deadline: "{deadline}"
api:
  base_url: "https://example.invalid"
session:
  headers: {{}}
  params: {{}}
  trust_env: false
  verify: false
"""


class FakeSession:
    """SessionManager stand-in with a scripted sequence of login outcomes."""

    def __init__(self, uid_sequence):
        self._uids = list(uid_sequence)
        self.uid = ""
        self.name = ""
        self.session = None
        self.login_calls = 0

    def init_session(self):
        pass

    def login(self):
        self.login_calls += 1
        self.uid = self._uids.pop(0) if self._uids else ""
        return (True, None)


class FakeRunner:
    """Records whether booking was attempted and with which uid."""

    instances = []

    def prepare(self):
        pass

    def __init__(self, api_client, session_manager, **kwargs):
        self.session_mgr = session_manager
        self.booked_with = None
        FakeRunner.instances.append(self)

    def run_booking(self, plans, target_date, on_result=None, deadline=None):
        self.booked_with = self.session_mgr.uid
        return []


class UidRecoveryTests(unittest.TestCase):
    def setUp(self):
        FakeRunner.instances = []
        os.environ["SEATHUNTER_LOGIN_NAME"] = "student"
        os.environ["SEATHUNTER_PASSWORD"] = "secret"
        self.addCleanup(os.environ.pop, "SEATHUNTER_LOGIN_NAME", None)
        self.addCleanup(os.environ.pop, "SEATHUNTER_PASSWORD", None)

    def _config(self, open_delta_minutes):
        now = datetime.now()
        open_at = now + timedelta(minutes=open_delta_minutes)
        deadline = open_at + timedelta(minutes=20)
        if open_at.date() != now.date() or deadline.date() != now.date():
            self.skipTest("too close to midnight for a same-day window")
        path = os.path.join(tempfile.mkdtemp(), "ci.yaml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(CONFIG_TEMPLATE.format(
                open_time=open_at.strftime("%H:%M:%S"),
                deadline=deadline.strftime("%H:%M:%S"),
            ))
        return path

    def _run(self, uid_sequence, open_delta_minutes=5):
        config_path = self._config(open_delta_minutes)
        session = FakeSession(uid_sequence)
        with patch("seathunter.auth.session_manager.SessionManager",
                   return_value=session), \
             patch("seathunter.scheduler.booking_runner.BookingRunner", FakeRunner), \
             patch("seathunter.scheduler.one_shot.wait_until"), \
             patch("seathunter.api.client.ApiClient"), \
             patch("main.SESSION_REFRESH_LEAD", 60):
            code = main.run_once(config_path)
        return code, session

    def test_uid_present_books_normally(self):
        code, session = self._run(["410676", "410676"])
        self.assertEqual(FakeRunner.instances[0].booked_with, "410676")

    def test_initial_login_without_uid_retries_then_books(self):
        code, session = self._run(["", "410676", "410676"])
        self.assertEqual(code, 1)
        self.assertEqual(session.login_calls, 3)
        self.assertEqual(FakeRunner.instances[0].booked_with, "410676")

    def test_refresh_without_uid_stops_without_booking(self):
        code, session = self._run(["410676", "", "", ""])
        self.assertEqual(code, 1)
        self.assertEqual(session.login_calls, 4)
        self.assertEqual(FakeRunner.instances, [])

    def test_uid_never_arrives_sends_no_requests(self):
        code, session = self._run(["", "", ""])
        self.assertEqual(code, 1)
        self.assertEqual(session.login_calls, 3)
        self.assertEqual(FakeRunner.instances, [])

    def test_missing_uid_uses_only_the_bounded_attempt_count(self):
        code, session = self._run(["", "", ""])
        self.assertEqual(session.login_calls, 3)


if __name__ == "__main__":
    unittest.main()
