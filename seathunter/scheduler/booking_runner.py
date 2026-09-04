"""Booking runner: execute bookings with retry logic.

Extracted from main.py:157-172 (startNow) and killer.py:416-422 (run).
"""

from __future__ import annotations

import logging
import random
from datetime import datetime
from time import sleep
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
                 rate_limit_max_interval: float = 12.0,
                 retry_jitter_ratio: float = 0.15):
        self.api = api_client
        self.session_mgr = session_manager
        self.interval = interval
        self.max_try_times = max_try_times
        self.burst_interval = burst_interval
        self.burst_from = _parse_time(burst_from)
        self.burst_to = _parse_time(burst_to)
        self.rate_limit_max_interval = max(float(rate_limit_max_interval), 0.0)
        self.retry_jitter_ratio = max(float(retry_jitter_ratio), 0.0)
        self._adaptive_interval = 0.0
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
        """Return a smooth, jittered delay based on the latest responses.

        Rate-limit responses multiplicatively increase the delay.  Ordinary
        responses release the penalty gradually instead of snapping back to a
        burst, which avoids synchronized request spikes from multiple runners.
        """
        base = max(float(self.current_interval()), 0.01)
        cap = max(self.rate_limit_max_interval, base)
        if any(self._is_rate_limited(result) for result in results):
            previous = max(self._adaptive_interval, base)
            self._adaptive_interval = min(
                cap,
                max(base * 2.0, previous * 2.0),
            )
        else:
            previous = max(self._adaptive_interval, base)
            self._adaptive_interval = max(base, previous * 0.75)

        jitter = random.uniform(
            0.0, self._adaptive_interval * self.retry_jitter_ratio
        )
        return min(cap, self._adaptive_interval + jitter)

    def _interruptible_sleep(self, seconds: float) -> None:
        remaining = max(float(seconds), 0.0)
        while remaining > 0 and not self._cancelled:
            step = min(remaining, 0.1)
            sleep(step)
            remaining -= step

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
        self._adaptive_interval = 0.0
        results = []

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

            if deadline is not None and datetime.now() >= deadline:
                logger.info("Booking deadline %s reached, stopping",
                            deadline.strftime("%H:%M:%S"))
                break

            if on_attempt:
                on_attempt(retry + 1, -1)
            logger.info("Booking attempt %d/%d for %s",
                       retry + 1, self.max_try_times,
                       target_date.strftime("%Y-%m-%d"))
            round_results = []

            for i, plan in enumerate(plans):
                if self._cancelled:
                    break

                # Build the actual datetime for the plan
                hour, minute, second = (int(x) for x in plan.begin_time.split(":"))
                begin_time = target_date.replace(hour=hour, minute=minute, second=second, microsecond=0)

                # Get seat IDs and booker UIDs
                seat_ids = [s.seat_id for s in plan.seats]
                booker_uids = [self.session_mgr.uid] * len(plan.seats)

                result = self._book_single_plan(plan, begin_time, seat_ids, booker_uids, target_date)
                results.append(result)
                round_results.append(result)

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

                if self._cancelled:
                    break

            if not self._cancelled and retry < self.max_try_times - 1:
                wait_seconds = self.retry_delay(round_results)
                logger.info("Waiting %.3fs before retry", wait_seconds)
                self._interruptible_sleep(wait_seconds)

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
