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
                 retry_jitter_ratio: float = 0.15):
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
        """Return an evidence-based, jittered delay for the next request.

        Captured server traces show that an admitted request consumes roughly
        four seconds of capacity, while a rejected rate-limit probe does not
        restart that timer. Probe briefly after a rejection; use the safe
        admitted-request cadence after every other response.
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
            return results

        for retry in range(self.max_try_times):
            if self._cancelled:
                logger.info("Booking run cancelled")
                break

            if next_request_not_before is not None:
                self._interruptible_sleep_until(next_request_not_before)

            if deadline is not None and datetime.now() >= deadline:
                logger.info("Booking deadline %s reached, stopping",
                            deadline.strftime("%H:%M:%S"))
                break

            if on_attempt:
                on_attempt(retry + 1, -1)
            logger.info("Booking attempt %d/%d for %s",
                       retry + 1, self.max_try_times,
                       target_date.strftime("%Y-%m-%d"))
            for i, plan in enumerate(plans):
                if self._cancelled:
                    break

                # Multiple plans share the same booking endpoint and limiter.
                # Pace every POST, including plans within the same retry round.
                if i > 0 and next_request_not_before is not None:
                    self._interruptible_sleep_until(next_request_not_before)
                    if deadline is not None and datetime.now() >= deadline:
                        logger.info("Booking deadline %s reached, stopping",
                                    deadline.strftime("%H:%M:%S"))
                        self._cancelled = True
                        break

                # Build the actual datetime for the plan
                hour, minute, second = (int(x) for x in plan.begin_time.split(":"))
                begin_time = target_date.replace(hour=hour, minute=minute, second=second, microsecond=0)

                # Get seat IDs and booker UIDs
                seat_ids = [s.seat_id for s in plan.seats]
                booker_uids = [self.session_mgr.uid] * len(plan.seats)

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

                # Anchor the next slot to this request's start. A two-second
                # response with a 4.2-second cadence therefore waits about
                # 2.2 more seconds instead of needlessly waiting 4.2 seconds.
                retry_interval = self.retry_delay([result])
                next_request_not_before = request_started_at + retry_interval
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
                          target_date: datetime) -> BookingResult:
        """Execute a single booking attempt for one plan."""
        try:
            resp = self.api.book_seat(begin_time, plan.duration_hours, seat_ids, booker_uids)
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
