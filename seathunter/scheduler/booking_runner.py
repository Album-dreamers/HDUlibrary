"""Booking runner: execute bookings with retry logic.

Extracted from main.py:157-172 (startNow) and killer.py:416-422 (run).
"""

from __future__ import annotations

import logging
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
                 interval: int = 5, max_try_times: int = 10,
                 burst_interval: Optional[int] = None,
                 burst_from: Optional[str] = None,
                 burst_to: Optional[str] = None):
        self.api = api_client
        self.session_mgr = session_manager
        self.interval = interval
        self.max_try_times = max_try_times
        self.burst_interval = burst_interval
        self.burst_from = _parse_time(burst_from)
        self.burst_to = _parse_time(burst_to)
        self._cancelled = False

    def current_interval(self) -> int:
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

                if on_result:
                    on_result(result)

                if result.success:
                    logger.info("Booking successful: %s - %s", plan.id, plan.room_name)
                    return results

                logger.warning("Plan %s failed: %s", plan.id, result.message)

                if self._cancelled:
                    break

            if not self._cancelled and retry < self.max_try_times - 1:
                wait_seconds = self.current_interval()
                logger.debug("Waiting %ds before retry...", wait_seconds)
                # Interruptible sleep
                for _ in range(wait_seconds):
                    if self._cancelled:
                        break
                    sleep(1)

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
