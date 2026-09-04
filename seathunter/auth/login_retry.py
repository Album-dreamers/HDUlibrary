"""Rate-limit-conscious retry policy for account login validation."""

from __future__ import annotations

import logging
import random
from time import sleep
from typing import Callable, Optional, Tuple


logger = logging.getLogger("seathunter.auth")

LOGIN_ERR_SESSION = "session"
LOGIN_ERR_CREDENTIALS = "credentials"


def login_with_retry(
    session_manager,
    max_attempts: int = 3,
    initial_delay: float = 30.0,
    max_delay: float = 90.0,
    jitter_ratio: float = 0.15,
    sleep_fn: Optional[Callable[[float], None]] = None,
) -> Tuple[bool, Optional[str]]:
    """Validate login with bounded exponential backoff.

    A successful redirect without an account UID is treated as an incomplete
    session. Explicit credential failures are terminal; transient browser,
    network, and session failures are retried slowly to avoid CAS throttling.
    """
    attempts = max(1, int(max_attempts))
    first_delay = max(0.0, float(initial_delay))
    delay_cap = max(0.0, float(max_delay))
    jitter = max(0.0, float(jitter_ratio))
    sleeper = sleep_fn or sleep
    last_error: Optional[str] = LOGIN_ERR_SESSION

    for attempt in range(1, attempts + 1):
        # Never let a UID from an older session make a failed refresh look valid.
        session_manager.uid = ""
        session_manager.name = ""
        success, error_type = session_manager.login()
        if success and session_manager.uid:
            return True, None

        last_error = error_type or LOGIN_ERR_SESSION
        if last_error == LOGIN_ERR_CREDENTIALS or attempt >= attempts:
            break

        base_delay = min(delay_cap, first_delay * (2 ** (attempt - 1)))
        wait_seconds = min(
            delay_cap,
            base_delay + random.uniform(0.0, base_delay * jitter),
        )
        logger.warning(
            "Login validation attempt %d/%d failed (%s); retrying in %.1fs",
            attempt,
            attempts,
            last_error,
            wait_seconds,
        )
        sleeper(wait_seconds)

    return False, last_error
