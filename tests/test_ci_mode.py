import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from seathunter.config.manager import ConfigManager
from seathunter.models.booking_result import BookingResult
from seathunter.models.schedule import DateMapping, Schedule
from seathunter.scheduler.booking_runner import BookingRunner
from seathunter.scheduler.one_shot import (
    booking_open_at,
    collect_plan_ids,
    session_refresh_at,
    target_date_for_run,
    wait_until,
)


class OneShotSelectionTests(unittest.TestCase):
    def test_collects_weekday_and_date_plans_without_duplicates(self):
        target = datetime(2026, 9, 2)  # Wednesday
        schedules = [
            Schedule(
                mode="weekdays",
                target_weekdays=[3],
                plan_ids=["primary", "backup"],
            ),
            Schedule(
                mode="dates",
                mappings=[DateMapping("2026-09-02", ["backup", "special"])],
            ),
            Schedule(
                mode="weekdays",
                enabled=False,
                target_weekdays=[3],
                plan_ids=["disabled"],
            ),
        ]

        self.assertEqual(
            collect_plan_ids(schedules, target),
            ["primary", "backup", "special"],
        )

    def test_target_date_is_two_days_after_run_date(self):
        now = datetime(2026, 8, 31, 19, 45, 30)
        self.assertEqual(target_date_for_run(now), datetime(2026, 9, 2))

    def test_booking_open_time_uses_run_date(self):
        now = datetime(2026, 8, 31, 19, 45, 30)
        self.assertEqual(
            booking_open_at(now, "20:00:00"),
            datetime(2026, 8, 31, 20, 0, 0),
        )

    def test_invalid_booking_open_time_is_rejected(self):
        with self.assertRaises(ValueError):
            booking_open_at(datetime(2026, 8, 31), "8pm")

    def test_session_refresh_can_be_staggered_before_opening(self):
        open_at = datetime(2026, 8, 31, 20, 0, 0)

        self.assertEqual(
            session_refresh_at(open_at, 420),
            datetime(2026, 8, 31, 19, 53, 0),
        )


class CredentialOverrideTests(unittest.TestCase):
    def test_environment_credentials_override_file_without_saving(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = os.path.join(tmp_dir, "config.yaml")
            with open(config_path, "w", encoding="utf-8") as config_file:
                config_file.write(
                    "user:\n"
                    "  login_name: file-user\n"
                    "  password: file-password\n"
                )

            manager = ConfigManager(config_path)
            manager.load()
            with patch.dict(
                os.environ,
                {
                    "SEATHUNTER_LOGIN_NAME": "secret-user",
                    "SEATHUNTER_PASSWORD": "secret-password",
                },
                clear=False,
            ):
                self.assertEqual(
                    manager.get_user_info(),
                    {"login_name": "secret-user", "password": "secret-password"},
                )

            with open(config_path, "r", encoding="utf-8") as config_file:
                self.assertNotIn("secret-password", config_file.read())


class BookingResultTests(unittest.TestCase):
    def test_existing_reservation_is_an_idempotent_success(self):
        result = BookingResult.from_api_response(
            {"CODE": "ParamError", "MESSAGE": "已有预约，请勿重复预约！"}
        )

        self.assertTrue(result.success)
        self.assertTrue(result.already_reserved)
        self.assertFalse(result.created_by_this_run)

    def test_api_ok_means_this_run_created_the_reservation(self):
        result = BookingResult.from_api_response(
            {"CODE": "ok", "MESSAGE": "预约成功"}
        )

        self.assertTrue(result.success)
        self.assertFalse(result.already_reserved)
        self.assertTrue(result.created_by_this_run)


if __name__ == "__main__":
    unittest.main()


class _StubSession:
    """Minimal stand-in for SessionManager in runner tests."""

    def __init__(self, uid="410676"):
        self.uid = uid


class BurstIntervalTests(unittest.TestCase):
    def _runner(self, **kwargs):
        return BookingRunner(api_client=None, session_manager=None, **kwargs)

    def test_plain_interval_without_burst_settings(self):
        self.assertEqual(self._runner(interval=5).current_interval(), 5)

    def test_burst_window_tightens_the_interval(self):
        runner = self._runner(
            interval=5, burst_interval=2,
            burst_from="19:58:00", burst_to="20:05:00",
        )
        with patch("seathunter.scheduler.booking_runner.datetime") as fake:
            fake.now.return_value = datetime(2026, 9, 2, 20, 0, 30)
            self.assertEqual(runner.current_interval(), 2)
            fake.now.return_value = datetime(2026, 9, 2, 19, 57, 59)
            self.assertEqual(runner.current_interval(), 5)
            fake.now.return_value = datetime(2026, 9, 2, 20, 5, 0)
            self.assertEqual(runner.current_interval(), 5)

    def test_rate_limit_uses_smooth_exponential_backoff(self):
        runner = self._runner(
            interval=1.0,
            rate_limit_max_interval=8.0,
            retry_jitter_ratio=0.0,
        )
        limited = BookingResult(success=False, code="1", message="请求太频繁了，请稍后再试")

        self.assertEqual(runner.retry_delay([limited]), 2.0)
        self.assertEqual(runner.retry_delay([limited]), 4.0)
        self.assertEqual(runner.retry_delay([limited]), 8.0)

    def test_normal_response_gradually_releases_rate_limit_penalty(self):
        runner = self._runner(
            interval=1.0,
            rate_limit_max_interval=8.0,
            retry_jitter_ratio=0.0,
        )
        limited = BookingResult(success=False, code="1", message="请求太频繁了，请稍后再试")
        ordinary = BookingResult(success=False, code="ParamError", message="座位暂不可用")
        runner.retry_delay([limited])
        runner.retry_delay([limited])

        self.assertEqual(runner.retry_delay([ordinary]), 3.0)

    def test_jitter_never_exceeds_the_configured_rate_limit_cap(self):
        runner = self._runner(
            interval=1.0,
            rate_limit_max_interval=8.0,
            retry_jitter_ratio=0.5,
        )
        limited = BookingResult(success=False, code="429", message="Too Many Requests")

        with patch("seathunter.scheduler.booking_runner.random.uniform", side_effect=lambda low, high: high):
            runner.retry_delay([limited])
            runner.retry_delay([limited])
            delay = runner.retry_delay([limited])

        self.assertLessEqual(delay, 8.0)


class PreciseWaitTests(unittest.TestCase):
    def test_wait_until_never_returns_before_target_and_uses_fine_final_ticks(self):
        clock = [datetime(2026, 9, 4, 19, 59, 58)]
        target = datetime(2026, 9, 4, 20, 0, 0)
        sleeps = []

        def now():
            return clock[0]

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += timedelta(seconds=seconds)

        wait_until(target, precision=0.01, now=now, sleep=sleep)

        self.assertGreaterEqual(clock[0], target)
        self.assertTrue(any(seconds <= 0.01 for seconds in sleeps))


class BookingDeadlineTests(unittest.TestCase):
    def test_named_setting_appears_in_the_error(self):
        with self.assertRaises(ValueError) as ctx:
            booking_open_at(datetime(2026, 9, 3), "8pm", "booking_deadline")
        self.assertIn("settings.booking_deadline", str(ctx.exception))

    def test_run_booking_stops_once_the_deadline_passes(self):
        runner = BookingRunner(
            api_client=None, session_manager=_StubSession(), interval=0,
            max_try_times=50,
        )
        past = datetime.now() - timedelta(seconds=1)
        results = runner.run_booking(
            plans=[object()], target_date=datetime(2026, 9, 5), deadline=past,
        )
        self.assertEqual(results, [])


class TerminalStateTests(unittest.TestCase):
    """Retries must stop only where another attempt provably cannot help."""

    def _result(self, code, message):
        return BookingResult.from_api_response({"CODE": code, "MESSAGE": message})

    def test_booked_seat_is_success(self):
        self.assertTrue(self._result("ok", "预约成功").success)

    def test_existing_reservation_is_success(self):
        self.assertTrue(self._result("ParamError", "已有预约，请勿重复预约！").success)

    def test_seat_taken_keeps_retrying(self):
        # Someone may release the seat, so this is not terminal.
        taken = self._result("ParamError", "选择的座位无法预约，可能座位不可用或已经被其他人锁定或占用")
        self.assertFalse(taken.success)

    def test_network_error_keeps_retrying(self):
        self.assertFalse(self._result("error", "Connection reset").success)

    def test_failure_string_carries_the_code(self):
        self.assertIn("CODE=ParamError", str(self._result("ParamError", "boom")))

    def test_missing_uid_sends_no_requests(self):
        runner = BookingRunner(api_client=None, session_manager=_StubSession(""))
        results = runner.run_booking(plans=[object()], target_date=datetime(2026, 9, 6))
        self.assertEqual(results, [])
