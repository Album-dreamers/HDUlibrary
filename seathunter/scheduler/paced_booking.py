"""Bounded workers with a single admission gate at the HTTP call boundary."""

import logging
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

logger = logging.getLogger("seathunter.scheduler")


class BookingPool:
    def __init__(self, runner):
        self.runner = runner
        self.condition = threading.Condition()
        self.stopped = False
        self.last_start = None
        self.cooldown_until = 0.0
        self.limited = False
        self.lanes = []
        try:
            for index in range(runner.max_inflight):
                client = runner.api if index == 0 else runner.api.fork()
                executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix=f"booking-{index + 1}",
                )
                self.lanes.append((client, executor))
                # Start the thread before the opening boundary. No network I/O.
                executor.submit(lambda: None).result()
        except BaseException:
            self.close()
            raise

    def stop(self):
        with self.condition:
            self.stopped = True
            self.condition.notify_all()

    def admit(self, deadline):
        """Reserve a global send slot before signing the payload, never catch up."""
        with self.condition:
            while not self.stopped and self.runner._can_request(deadline):
                interval = (
                    self.runner.rate_limit_probe_interval if self.limited
                    else max(float(self.runner.current_interval()), 0.01)
                )
                earliest = self.cooldown_until
                if self.last_start is not None:
                    earliest = max(earliest, self.last_start + interval)
                remaining = earliest - time.monotonic()
                if remaining <= 0:
                    self.last_start = time.monotonic()
                    return True
                self.condition.wait(min(remaining, 0.05))
            return False

    def observe(self, result, admitted_at):
        # Update the gate in the worker, before callbacks or disk logging can
        # delay the coordinator. Already-admitted HTTP requests cannot be undone.
        with self.condition:
            if result.success:
                self.stopped = True
            elif self.runner._is_rate_limited(result) or result.retry_after_seconds is not None:
                pause = max(
                    self.runner.rate_limit_probe_interval,
                    result.retry_after_seconds or 0.0,
                )
                self.cooldown_until = max(self.cooldown_until, time.monotonic() + pause)
                self.limited = True
                logger.warning("Booking rate limited; global send pause at least %.3fs", pause)
            elif admitted_at >= self.cooldown_until:
                # A slow old response is not evidence that a newer throttle
                # has cleared. Only a post-cooldown probe can restore cadence.
                self.limited = False
            self.condition.notify_all()

    def run(self, plan, target_date, on_result, on_attempt, deadline):
        results = []
        pending = {}
        launched = 0
        hour, minute, second = (int(x) for x in plan.begin_time.split(":"))
        begin = target_date.replace(hour=hour, minute=minute, second=second, microsecond=0)
        seats = [seat.seat_id for seat in plan.seats]
        uids = [self.runner.session_mgr.uid] * len(seats)

        def book(client):
            admitted_at = [0.0]

            def admit():
                allowed = self.admit(deadline)
                admitted_at[0] = time.monotonic()
                return allowed

            result = self.runner._book_single_plan(
                plan, begin, seats, uids, target_date,
                api=client, before_send=admit,
                can_send=lambda: not self.stopped and self.runner._can_request(deadline),
            )
            if result.code != "not_sent":
                self.observe(result, admitted_at[0])
                return result
            return None

        try:
            while True:
                # Collect all completed requests before filling a free lane.
                for future in [item for item in pending if item.done()]:
                    pending.pop(future)
                    result = future.result()
                    if result is not None:
                        results.append(result)
                        if on_result:
                            on_result(result)

                can_send = not self.stopped and self.runner._can_request(deadline)
                if not can_send:
                    self.stop()
                if can_send and launched < self.runner.max_try_times:
                    occupied = set(pending.values())
                    for lane, (client, executor) in enumerate(self.lanes):
                        if lane in occupied:
                            continue
                        if launched >= self.runner.max_try_times or self.stopped:
                            break
                        if on_attempt:
                            on_attempt(launched + 1, -1)
                        if self.stopped or not self.runner._can_request(deadline):
                            break
                        launched += 1
                        pending[executor.submit(book, client)] = lane

                if not pending:
                    break
                wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
        finally:
            self.stop()
        return results

    def close(self):
        self.stop()
        # Collect/drain in-flight responses before closing their connections.
        # A client timeout is not proof that the server did not create a booking.
        for client, executor in self.lanes:
            executor.shutdown(wait=True)
            if client is not self.runner.api:
                client.close()
        self.lanes.clear()
