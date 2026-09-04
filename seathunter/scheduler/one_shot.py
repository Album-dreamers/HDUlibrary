"""One-shot booking helpers for short-lived CI runners."""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Callable, Iterable, List, Optional

from seathunter.models.schedule import BOOKING_ADVANCE_DAYS, Schedule

logger = logging.getLogger("seathunter.scheduler")


def collect_plan_ids(schedules: Iterable[Schedule], target_date: datetime) -> List[str]:
    """Collect enabled plan IDs for a date, preserving order and removing duplicates."""
    plan_ids = []
    seen = set()
    for schedule in schedules:
        for plan_id in schedule.plan_ids_for_date(target_date):
            if plan_id not in seen:
                seen.add(plan_id)
                plan_ids.append(plan_id)
    return plan_ids


def target_date_for_run(now: datetime) -> datetime:
    """Return the seat date released on the current run date."""
    return (now + timedelta(days=BOOKING_ADVANCE_DAYS)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def booking_open_at(now: datetime, open_time: str,
                    setting_name: str = "booking_open_time") -> datetime:
    """Build today's datetime for an HH:MM:SS setting."""
    try:
        parsed = datetime.strptime(open_time, "%H:%M:%S")
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid settings.{setting_name}: {open_time!r}; expected HH:MM:SS"
        ) from exc
    return now.replace(
        hour=parsed.hour,
        minute=parsed.minute,
        second=parsed.second,
        microsecond=0,
    )


def wait_until(target: datetime, log_interval: int = 60,
               precision: float = 0.01,
               now: Optional[Callable[[], datetime]] = None,
               sleep: Optional[Callable[[float], None]] = None) -> None:
    """Wait until ``target`` without returning before the wall-clock boundary.

    Long waits use coarse sleeps.  The final quarter-second is checked in
    short ticks so the first booking call can begin close to the release
    boundary without sending speculative requests before it.
    """
    if precision <= 0:
        raise ValueError("precision must be positive")

    now_fn = now or datetime.now
    sleep_fn = sleep or time.sleep
    last_reported_minute = None
    while True:
        remaining = (target - now_fn()).total_seconds()
        if remaining <= 0:
            return

        remaining_minute = int(remaining // log_interval)
        if remaining_minute != last_reported_minute:
            logger.info("Waiting for booking window: %.0f seconds remaining", remaining)
            last_reported_minute = remaining_minute
        if remaining > 0.25:
            delay = min(5.0, max(precision, remaining - 0.25))
        else:
            delay = min(precision, remaining)
        sleep_fn(delay)


def append_github_summary(lines: Iterable[str]) -> None:
    """Append a small result summary when running inside GitHub Actions."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    try:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write("\n".join(lines) + "\n")
    except OSError as exc:
        logger.warning("Unable to write GitHub Actions summary: %s", exc)
