"""Exercise actual waits and requests with a deterministic clock, no network."""

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from seathunter.scheduler.booking_runner import BookingRunner


class Clock:
    def __init__(self):
        self.seconds = 0.0
        self.on_sleep = None

    def monotonic(self):
        return self.seconds

    def now(self):
        return datetime(2026, 9, 5, 20) + timedelta(seconds=self.seconds)

    def sleep(self, seconds):
        self.seconds += seconds
        if self.on_sleep:
            self.on_sleep()


class BookingTimingTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.starts = []
        self.duration = 0.0
        self.responses = []
        self.plan = SimpleNamespace(
            id="primary", room_name="room", begin_time="10:00:00",
            duration_hours=11,
            seats=[SimpleNamespace(seat_id="62561", seat_num="421")],
        )
        self.runner = BookingRunner(
            SimpleNamespace(book_seat=self.book), SimpleNamespace(uid="410676"),
            interval=0.5, rate_limit_probe_interval=0.5,
            retry_jitter_ratio=0, max_try_times=4,
        )
        clock_patch = patch("seathunter.scheduler.booking_runner.time", self.clock)
        date_patch = patch("seathunter.scheduler.booking_runner.datetime", self.clock)
        clock_patch.start()
        date_patch.start()
        self.addCleanup(clock_patch.stop)
        self.addCleanup(date_patch.stop)

    def book(self, *args):
        self.starts.append(self.clock.seconds)
        self.clock.seconds += self.duration
        if self.responses:
            return self.responses.pop(0)
        return {"CODE": "1", "MESSAGE": "rate limited"}

    def run_booking(self, **kwargs):
        return self.runner.run_booking(
            kwargs.pop("plans", [self.plan]), datetime(2026, 9, 7), **kwargs
        )

    def assert_starts(self, expected):
        self.assertEqual(len(self.starts), len(expected))
        for actual, target in zip(self.starts, expected):
            self.assertAlmostEqual(actual, target, places=7)

    def test_short_responses_do_not_add_to_half_second_interval(self):
        self.duration = 0.2
        self.run_booking()
        self.assert_starts([0, 0.5, 1, 1.5])

    def test_slow_responses_are_serial_without_catchup_burst(self):
        self.duration = 1.2
        self.run_booking()
        self.assert_starts([0, 1.2, 2.4, 3.6])

    def test_multiple_plans_share_the_same_half_second_spacing(self):
        self.runner.max_try_times = 2
        self.run_booking(plans=[self.plan, self.plan])
        self.assert_starts([0, 0.5, 1, 1.5])

    def test_cancelling_while_waiting_between_plans_sends_no_extra_request(self):
        self.clock.on_sleep = self.runner.cancel
        self.run_booking(plans=[self.plan, self.plan])
        self.assert_starts([0])

    def test_deadline_crossed_by_attempt_callback_sends_no_request(self):
        def slow_callback(*args):
            self.clock.seconds = 1.0

        self.run_booking(
            deadline=self.clock.now() + timedelta(seconds=0.5),
            on_attempt=slow_callback,
        )
        self.assert_starts([])

    def test_retry_wait_stops_at_booking_deadline(self):
        self.run_booking(deadline=self.clock.now() + timedelta(seconds=0.25))
        self.assert_starts([0])
        self.assertLessEqual(self.clock.seconds, 0.25 + 1e-7)

    def test_success_does_not_send_later_requests(self):
        self.responses = [{"CODE": "ok", "MESSAGE": "created"}]
        self.assertTrue(self.run_booking()[0].created_by_this_run)
        self.assert_starts([0])

    def test_existing_reservation_stops_without_claiming_creation(self):
        self.responses = [{"CODE": "ParamError", "MESSAGE": "已有预约"}]
        result = self.run_booking()[0]
        self.assertTrue(result.already_reserved)
        self.assertFalse(result.created_by_this_run)
        self.assert_starts([0])

    def test_half_second_policy_reaches_deadline_before_attempt_cap(self):
        self.runner.max_try_times = 2000
        with patch("seathunter.scheduler.booking_runner.logger"):
            self.run_booking(deadline=self.clock.now() + timedelta(minutes=15))
        self.assertEqual(len(self.starts), 1800)
        self.assertAlmostEqual(self.starts[-1], 899.5)


if __name__ == "__main__":
    unittest.main()
