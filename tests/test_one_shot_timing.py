"""Exercise the CI entry point and real booking loop with an offline clock."""

import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main
from seathunter.scheduler.booking_runner import BookingRunner
from seathunter.scheduler.one_shot import wait_until


class _Clock:
    def __init__(self, start):
        self.current = start
        self.elapsed = 0.0

    def now(self):
        return self.current

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.current += timedelta(seconds=seconds)
        self.elapsed += seconds


class OneShotTimingTests(unittest.TestCase):
    release = datetime(2026, 9, 5, 20, 0, 0)

    def _run(self, start, login_results=None, booking_results=None,
             history_error=False, history_init_error=False, runner_results=None):
        clock = _Clock(start)
        setup_events = []
        posts = []
        logins = []
        outcomes = list(login_results or [(True, None, "12345")] * 2)
        responses = list(booking_results or [{"CODE": "ok", "MESSAGE": "booked"}])

        class ClockDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.now()

        settings = {
            "interval": 0.5,
            "max_try_times": 3,
            "rate_limit_probe_interval": 0.5,
            "retry_jitter_ratio": 0,
            "session_refresh_lead_seconds": 720,
            "login_max_attempts": 3,
            "login_retry_initial_delay_seconds": 0,
            "login_retry_max_delay_seconds": 0,
            "login_retry_jitter_ratio": 0,
            "booking_open_time": "20:00:00",
            "booking_deadline": "20:15:00",
        }
        plan = SimpleNamespace(
            id="daily", begin_time="10:00:00", duration_hours=11,
            room_name="room", validate=lambda: [],
            seats=[SimpleNamespace(seat_id="62561", seat_num="421")],
        )
        config = Mock()
        config.get_settings.return_value = settings
        config.get_schedules.return_value = [
            SimpleNamespace(plan_ids_for_date=lambda day: ["daily"])
        ]
        config.get_plans.return_value = [plan]
        config.get_user_info.return_value = {"login_name": "student", "password": "test"}
        session = SimpleNamespace(uid="", name="", init_session=lambda: None)

        def login():
            logins.append(clock.now())
            success, error, session.uid = outcomes.pop(0)
            return success, error

        session.login = login

        def book_seat(*args):
            posts.append(clock.now())
            return responses.pop(0)

        def make_api(*args):
            setup_events.append(("api", clock.now()))
            clock.sleep(0.1)
            return SimpleNamespace(book_seat=book_seat)

        def make_runner(**kwargs):
            setup_events.append(("runner", clock.now()))
            clock.sleep(0.2)
            runner = BookingRunner(**kwargs)
            if runner_results is not None:
                runner.run_booking = Mock(return_value=runner_results)
            return runner

        history = Mock()
        if history_error:
            history.log.side_effect = OSError("history disk is full")

        def make_history():
            setup_events.append(("history", clock.now()))
            clock.sleep(0.3)
            if history_init_error:
                raise OSError("history directory is unavailable")
            return history

        logger = Mock()
        summary = Mock()
        with ExitStack() as stack:
            for target in (
                "datetime.datetime", "seathunter.scheduler.one_shot.datetime",
                "seathunter.scheduler.booking_runner.datetime",
            ):
                stack.enter_context(patch(target, ClockDateTime))
            stack.enter_context(patch("seathunter.scheduler.booking_runner.time", clock))
            stack.enter_context(patch("seathunter.config.manager.ConfigManager", return_value=config))
            stack.enter_context(patch("seathunter.auth.session_manager.SessionManager", return_value=session))
            stack.enter_context(patch("seathunter.auth.login_retry.sleep", clock.sleep))
            stack.enter_context(patch("seathunter.api.client.ApiClient", side_effect=make_api))
            stack.enter_context(patch("seathunter.scheduler.booking_runner.BookingRunner", side_effect=make_runner))
            stack.enter_context(patch("seathunter.logging_.history.HistoryLogger", side_effect=make_history))
            stack.enter_context(patch("seathunter.logging_.logger.setup_logging", return_value=logger))
            stack.enter_context(patch("seathunter.scheduler.one_shot.append_github_summary", summary))
            stack.enter_context(patch(
                "seathunter.scheduler.one_shot.wait_until",
                side_effect=lambda target: wait_until(target, now=clock.now, sleep=clock.sleep),
            ))
            code = main.run_once("unused-offline-config.yaml")
        return SimpleNamespace(
            code=code, posts=posts, setup=setup_events, logins=logins,
            summary=summary, logger=logger,
        )

    def test_setup_finishes_before_release_and_first_post_waits_for_boundary(self):
        result = self._run(self.release - timedelta(seconds=2))
        self.assertEqual(result.code, 0)
        self.assertEqual([name for name, _ in result.setup], ["api", "runner", "history"])
        self.assertTrue(all(when < self.release for _, when in result.setup))
        self.assertEqual(result.posts, [self.release])

    def test_preopening_refresh_happens_once_then_booking_waits_until_release(self):
        result = self._run(self.release - timedelta(minutes=13))
        self.assertEqual(result.logins, [
            self.release - timedelta(minutes=13),
            self.release - timedelta(minutes=12),
        ])
        self.assertEqual(result.posts, [self.release])

    def test_late_initial_login_is_not_immediately_repeated(self):
        result = self._run(self.release - timedelta(minutes=10))
        self.assertEqual(len(result.logins), 1)
        self.assertEqual(result.posts, [self.release])

    def test_exhausted_initial_login_sends_no_booking_requests(self):
        result = self._run(
            self.release - timedelta(minutes=13),
            login_results=[(False, "network", "")] * 3,
        )
        self.assertEqual(result.code, 1)
        self.assertEqual(len(result.logins), 3)
        self.assertEqual(result.posts, [])

    def test_exhausted_refresh_sends_no_booking_requests(self):
        result = self._run(
            self.release - timedelta(minutes=13),
            login_results=[(True, None, "12345")] + [(False, "network", "")] * 3,
        )
        self.assertEqual(result.code, 1)
        self.assertEqual(len(result.logins), 4)
        self.assertEqual(result.posts, [])

    def test_history_write_failure_does_not_stop_retry_or_hide_success(self):
        result = self._run(
            self.release - timedelta(seconds=2), history_error=True,
            booking_results=[
                {"CODE": "ParamError", "MESSAGE": "seat unavailable"},
                {"CODE": "ok", "MESSAGE": "booked"},
            ],
        )
        self.assertEqual(result.code, 0)
        self.assertEqual(result.posts, [self.release, self.release + timedelta(seconds=0.5)])
        self.assertIn("booking created", result.summary.call_args.args[0][0])

    def test_history_setup_failure_does_not_prevent_booking(self):
        result = self._run(
            self.release - timedelta(seconds=2), history_init_error=True,
        )
        self.assertEqual(result.code, 0)
        self.assertEqual(result.posts, [self.release])

    def test_summary_prefers_creation_receipt_over_earlier_existing_response(self):
        from seathunter.models.booking_result import BookingResult
        result = self._run(self.release - timedelta(seconds=2), runner_results=[
            BookingResult(True, "ParamError", "已有预约"),
            BookingResult(True, "ok", "created", plan_id="daily", seat_num="421", room_name="room"),
        ])
        self.assertEqual(result.code, 0)
        summary = result.summary.call_args.args[0]
        self.assertEqual(summary[0], "## SeatHunter booking created")
        self.assertIn("- Seat: room-421", summary)


if __name__ == "__main__":
    unittest.main()
