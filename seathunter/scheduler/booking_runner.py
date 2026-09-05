"""Booking runner: execute bookings with retry logic.

Extracted from main.py:157-172 (startNow) and killer.py:416-422 (run).
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from typing import List, Callable, Optional

from seathunter.api.client import ApiClient
from seathunter.auth.session_manager import SessionManager
from seathunter.models.plan import Plan
from seathunter.models.booking_result import BookingResult

logger = logging.getLogger("seathunter.scheduler")

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _parse_time(value: Optional[str]):
    """Parse an HH:MM:SS setting into a time, ignoring blank values."""
    if not value:
        return None
    return datetime.strptime(value, "%H:%M:%S").time()


class BookingRunner:
    """Executes booking attempts with retry logic."""

    def __init__(self, api_client: ApiClient, session_manager: SessionManager,
                 interval: float = 5, max_try_times: int = 10,
                 burst_interval: Optional[float] = None,
                 burst_from: Optional[str] = None,
                 burst_to: Optional[str] = None,
                 rate_limit_probe_interval: float = 1.0,
                 retry_jitter_ratio: float = 0.15,
                 max_inflight: int = 1):
        if type(max_inflight) is not int or not 1 <= max_inflight <= 4:
            raise ValueError("max_inflight must be an integer from 1 to 4")
        self.api = api_client
        self.session_mgr = session_manager
        self.interval = interval
        self.max_try_times = max_try_times
        self.burst_interval = burst_interval
        self.burst_from = _parse_time(burst_from)
        self.burst_to = _parse_time(burst_to)
        self.rate_limit_probe_interval = max(
            float(rate_limit_probe_interval), 0.01
        )
        self.retry_jitter_ratio = max(float(retry_jitter_ratio), 0.0)
        self._cancelled = False
        self.max_inflight = max_inflight
        self._pool = None

    def prepare(self):
        """Prepare isolated workers before the opening time without logging in."""
        if self.max_inflight > 1 and self._pool is None:
            from seathunter.scheduler.paced_booking import BookingPool
            self._pool = BookingPool(self)

    def close(self):
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def current_interval(self) -> float:
        """Seconds to wait before the next attempt.

        Retries tighten inside the burst window so attempts land densely
        around the moment seats are released.
        """
        if not (self.burst_interval and self.burst_from and self.burst_to):
            return self.interval
        now = datetime.now().time()
        if self.burst_from <= now < self.burst_to:
            return self.burst_interval
        return self.interval

    @staticmethod
    def _is_rate_limited(result: BookingResult) -> bool:
        return (
            str(result.code) in {"1", "429"}
            or "请求太频繁" in str(result.message)
            or "Too Many Requests" in str(result.message)
        )

    def retry_delay(self, results: List[BookingResult]) -> float:
        """Choose the configured cadence for ordinary or rate-limited results.

        These are client policy settings, not a guarantee about the server's
        quota or penalty rules. The serial path adds the configured jitter;
        the bounded worker pool uses a shared admission gate.
        """
        if any(self._is_rate_limited(result) for result in results):
            base = self.rate_limit_probe_interval
        else:
            base = max(float(self.current_interval()), 0.01)

        jitter = random.uniform(0.0, base * self.retry_jitter_ratio)
        return base + jitter

    def _interruptible_sleep_until(self, deadline: float) -> None:
        """Wait for a monotonic deadline without accumulating response time.

        The configured interval is a minimum distance between request starts,
        not an extra delay added after the server finishes responding.
        """
        while not self._cancelled:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.1))

    def cancel(self):
        """Cancel the current booking run."""
        self._cancelled = True
        if self._pool is not None:
            self._pool.stop()

    def _can_request(self, deadline: Optional[datetime]) -> bool:
        if self._cancelled:
            return False
        if deadline is not None and datetime.now() >= deadline:
            logger.info("Booking deadline %s reached, stopping",
                        deadline.strftime("%H:%M:%S"))
            return False
        return True

    def _wait_for_slot(self, slot: Optional[float],
                       deadline: Optional[datetime]) -> bool:
        if not self._can_request(deadline):
            return False
        if slot is not None:
            if deadline is not None:
                remaining = max(0.0, (deadline - datetime.now()).total_seconds())
                slot = min(slot, time.monotonic() + remaining)
            self._interruptible_sleep_until(slot)
        return self._can_request(deadline)

    def run_booking(self, plans: List[Plan], target_date: datetime,
                    on_result: Optional[Callable[[BookingResult], None]] = None,
                    on_attempt: Optional[Callable[[int, int], None]] = None,
                    deadline: Optional[datetime] = None) -> List[BookingResult]:
        """Execute booking for all plans targeting a specific date.

        Args:
            plans: List of Plan objects to book.
            target_date: The date the seats are for.
            on_result: Callback for each booking result.
            on_attempt: Callback for each attempt (attempt_num, plan_index).

        Returns:
            List of BookingResult for all attempts.
        """
        self._cancelled = False
        results = []
        next_request_not_before = None

        # Without a uid the request cannot name a booker, so no number of
        # retries can succeed. This is a certainty, not a probability.
        if not self.session_mgr.uid:
            logger.error(
                "Session has no uid; a booking cannot identify the account. "
                "Sending no requests."
            )
            self.close()
            return results

        if self.max_inflight > 1 and len(plans) == 1:
            self.prepare()
            try:
                return self._pool.run(plans[0], target_date, on_result, on_attempt, deadline)
            finally:
                self.close()
        # Never race different seat plans: two successes could reserve two seats.
        # Multiple plans retain the original ordered, serial fallback behavior.
        self.close()

        for retry in range(self.max_try_times):
            if not self._wait_for_slot(next_request_not_before, deadline):
                break

            if on_attempt:
                on_attempt(retry + 1, -1)
            logger.info("Booking attempt %d/%d for %s",
                       retry + 1, self.max_try_times,
                       target_date.strftime("%Y-%m-%d"))
            for i, plan in enumerate(plans):
                # Multiple plans share the same booking endpoint and limiter.
                # Pace every POST, including plans within the same retry round.
                if i > 0 and not self._wait_for_slot(next_request_not_before, deadline):
                    return results

                # Build the actual datetime for the plan
                hour, minute, second = (int(x) for x in plan.begin_time.split(":"))
                begin_time = target_date.replace(hour=hour, minute=minute, second=second, microsecond=0)

                # Get seat IDs and booker UIDs
                seat_ids = [s.seat_id for s in plan.seats]
                booker_uids = [self.session_mgr.uid] * len(plan.seats)

                # Waiting, callbacks and preparation may cross the cutoff or
                # receive a cancellation. Check again at the call boundary.
                if not self._can_request(deadline):
                    return results
                request_started_at = time.monotonic()
                result = self._book_single_plan(plan, begin_time, seat_ids, booker_uids, target_date)
                results.append(result)

                if on_result:
                    on_result(result)

                if result.success:
                    if result.already_reserved:
                        logger.info(
                            "Reservation already existed; this run did not create it. "
                            "Stopping retries for %s - %s",
                            plan.id,
                            plan.room_name,
                        )
                    else:
                        logger.info(
                            "Booking created by this run: %s - %s",
                            plan.id,
                            plan.room_name,
                        )
                    return results

                logger.warning("Plan %s failed: %s", plan.id, result.message)

                # Response time counts toward the cadence. A slow response
                # never creates concurrent requests or a catch-up burst.
                retry_interval = self.retry_delay([result])
                next_request_not_before = request_started_at + retry_interval
                if result.retry_after_seconds is not None:
                    next_request_not_before = max(
                        next_request_not_before,
                        time.monotonic() + result.retry_after_seconds,
                    )
                logger.info(
                    "Next booking request no earlier than %.3fs after this "
                    "request started",
                    retry_interval,
                )

                if self._cancelled:
                    break

        return results

    def _book_single_plan(self, plan: Plan, begin_time: datetime,
                          seat_ids: List[str], booker_uids: List[str],
                          target_date: datetime, api=None, before_send=None,
                          can_send=None) -> BookingResult:
        """Execute a single booking attempt for one plan."""
        try:
            client = api if api is not None else self.api
            kwargs = {"before_send": before_send} if before_send is not None else {}
            if can_send is not None:
                kwargs["can_send"] = can_send
            resp = client.book_seat(begin_time, plan.duration_hours, seat_ids, booker_uids, **kwargs)
            return BookingResult.from_api_response(
                resp,
                plan_id=plan.id,
                seat_num=",".join(s.seat_num for s in plan.seats),
                room_name=plan.room_name,
                target_date=target_date.strftime("%Y-%m-%d"),
            )
        except Exception as e:
            logger.error("Booking exception for plan %s: %s", plan.id, e)
            return BookingResult(
                success=False,
                code="error",
                message=str(e),
                plan_id=plan.id,
                target_date=target_date.strftime("%Y-%m-%d"),
            )
