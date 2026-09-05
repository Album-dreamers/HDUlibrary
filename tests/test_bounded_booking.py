"""Use blocked responses to exercise real concurrent booking orchestration."""

import queue
import threading
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from seathunter.scheduler.booking_runner import BookingRunner


class BlockingApi:
    def __init__(self):
        self.calls = queue.Queue()
        self.release = threading.Event()
        self.started = []
        self.lock = threading.Lock()

    def fork(self):
        return self

    def close(self):
        pass

    def book_seat(self, *args, before_send=None, can_send=None):
        if before_send is not None and not before_send():
            return {"CODE": "not_sent", "MESSAGE": "Stopped before POST"}
        if can_send is not None and not can_send():
            return {"CODE": "not_sent", "MESSAGE": "Stopped during preparation"}
        release = threading.Event()
        with self.lock:
            self.started.append(time.monotonic())
        response = {"CODE": "ParamError", "MESSAGE": "seat unavailable"}
        self.calls.put((release, response))
        while not release.wait(0.01):
            if self.release.is_set():
                break
        return response


class BoundedBookingTests(unittest.TestCase):
    def setUp(self):
        self.api = BlockingApi()
        self.plan = SimpleNamespace(
            id="primary", room_name="room", begin_time="10:00:00",
            duration_hours=11,
            seats=[SimpleNamespace(seat_id="62561", seat_num="421")],
        )
        self.runner = BookingRunner(
            self.api, SimpleNamespace(uid="123"), interval=0.1,
            max_try_times=4, max_inflight=2,
            rate_limit_probe_interval=0.1, retry_jitter_ratio=0,
        )
        self.results = []
        self.errors = []
        self.thread = None
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.runner.cancel()
        self.api.release.set()
        if self.thread:
            self.thread.join(3)
            self.assertFalse(self.thread.is_alive(), "booking workers did not stop")
        self.runner.close()

    def start(self, **kwargs):
        self.runner.prepare()

        def run():
            try:
                self.results.extend(self.runner.run_booking(
                    [self.plan], datetime(2026, 9, 7), **kwargs,
                ))
            except Exception as exc:
                self.errors.append(exc)

        self.thread = threading.Thread(target=run)
        self.thread.start()

    def finish(self):
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual(self.errors, [])

    def test_second_post_can_start_while_first_response_is_blocked(self):
        self.runner.interval = 0.5
        self.start()
        first, _ = self.api.calls.get(timeout=2)
        second, response = self.api.calls.get(timeout=2)
        self.assertFalse(first.is_set())
        self.assertGreaterEqual(self.api.started[1] - self.api.started[0], 0.5)
        self.assertLess(self.api.started[1] - self.api.started[0], 1.5)
        with self.assertRaises(queue.Empty):
            self.api.calls.get(timeout=0.15)
        response.update(CODE="ok", MESSAGE="created")
        second.set()
        # Success stops admission but the first POST is still in flight.
        with self.assertRaises(queue.Empty):
            self.api.calls.get(timeout=0.15)
        first.set()
        self.finish()
        self.assertEqual(len(self.api.started), 2)
        self.assertTrue(any(result.created_by_this_run for result in self.results))

    def test_fast_success_prevents_the_waiting_second_post(self):
        self.runner.interval = 1
        self.start()
        release, response = self.api.calls.get(timeout=2)
        response.update(CODE="ok", MESSAGE="created")
        release.set()
        self.finish()
        self.assertEqual(len(self.api.started), 1)
        self.assertEqual(len(self.results), 1)

    def test_late_success_is_kept_when_an_existing_reservation_returns_first(self):
        self.start()
        first, first_response = self.api.calls.get(timeout=2)
        second, second_response = self.api.calls.get(timeout=2)
        second_response.update(CODE="ParamError", MESSAGE="已有预约")
        second.set()
        first_response.update(CODE="ok", MESSAGE="created")
        first.set()
        self.finish()
        self.assertTrue(any(result.created_by_this_run for result in self.results))
        self.assertEqual(len(self.api.started), 2)

    def test_cancellation_sends_no_waiting_request(self):
        self.runner.interval = 1
        self.start()
        first, _ = self.api.calls.get(timeout=2)
        self.runner.cancel()
        first.set()
        self.finish()
        self.assertEqual(len(self.api.started), 1)

    def test_deadline_is_checked_at_send_boundary(self):
        self.runner.interval = 1
        cutoff = datetime.now() + timedelta(seconds=0.3)
        self.start(deadline=cutoff)
        self.api.calls.get(timeout=2)
        time.sleep(0.35)
        self.api.release.set()
        self.finish()
        self.assertEqual(len(self.api.started), 1)

    def test_attempt_limit_counts_sent_requests_not_completion_rounds(self):
        self.runner.max_try_times = 3
        self.api.release.set()
        self.start()
        self.finish()
        self.assertEqual(len(self.api.started), 3)
        self.assertEqual(len(self.results), 3)
        for a, b in zip(self.api.started, self.api.started[1:]):
            self.assertGreaterEqual(b - a, 0.099)

    def test_invalid_concurrency_is_rejected(self):
        for value in (0, 5, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                BookingRunner(self.api, SimpleNamespace(uid="123"), max_inflight=value)

    def test_four_slow_requests_fill_capacity_without_a_fifth_queued_post(self):
        self.runner.max_inflight = 4
        self.runner.max_try_times = 20
        self.start()
        for _ in range(4):
            self.api.calls.get(timeout=2)
        with self.assertRaises(queue.Empty):
            self.api.calls.get(timeout=0.2)
        self.assertEqual(len(self.api.started), 4)
        for a, b in zip(self.api.started, self.api.started[1:]):
            self.assertGreaterEqual(b - a, 0.099)
        self.runner.cancel()
        self.api.release.set()
        self.finish()

    def test_rate_limit_and_retry_after_pause_all_waiting_workers(self):
        self.runner.rate_limit_probe_interval = 0.3
        self.start()
        release, response = self.api.calls.get(timeout=2)
        response.update(CODE="429", MESSAGE="limited", _retry_after_seconds=0.4)
        released_at = time.monotonic()
        release.set()
        with self.assertRaises(queue.Empty):
            self.api.calls.get(timeout=0.25)
        next_release, next_response = self.api.calls.get(timeout=2)
        self.assertGreaterEqual(self.api.started[1] - released_at, 0.399)
        next_response.update(CODE="ok", MESSAGE="created")
        next_release.set()
        self.finish()
        self.assertEqual(len(self.api.started), 2)

    def test_callback_crossing_deadline_cannot_send_a_request(self):
        cutoff = datetime.now() + timedelta(seconds=0.1)
        self.start(deadline=cutoff, on_attempt=lambda *args: time.sleep(0.15))
        self.finish()
        self.assertEqual(self.api.started, [])

    def test_callback_error_stops_waiting_workers(self):
        self.runner.interval = 1

        def fail(result):
            raise RuntimeError("callback failed")

        self.start(on_result=fail)
        release, _ = self.api.calls.get(timeout=2)
        release.set()
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
        self.assertEqual([str(error) for error in self.errors], ["callback failed"])
        self.assertEqual(len(self.api.started), 1)

    def test_old_response_cannot_clear_a_newer_cooldown(self):
        from seathunter.models.booking_result import BookingResult
        self.runner.prepare()
        pool = self.runner._pool
        with patch("seathunter.scheduler.paced_booking.time.monotonic", return_value=10):
            pool.observe(BookingResult(False, "429", "limited", retry_after_seconds=1), 9)
        with patch("seathunter.scheduler.paced_booking.time.monotonic", return_value=12):
            pool.observe(BookingResult(False, "ParamError", "unavailable"), 9)
            self.assertTrue(pool.limited)
            pool.observe(BookingResult(False, "ParamError", "unavailable"), 11.5)
            self.assertFalse(pool.limited)


if __name__ == "__main__":
    unittest.main()
