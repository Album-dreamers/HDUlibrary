"""
HDU Library SeatHunter - Main Entry Point

Usage:
    python main.py                  # GUI mode (default)
    python main.py --cli            # CLI mode (terminal interface)
    python main.py --daemon         # Daemon mode (no menu, reads config and runs)
    python main.py --once           # One booking window, then exit (for CI)
    python main.py --check-login     # Validate login and UID without booking
    python main.py -c path/to/config.yaml  # Custom config path
"""

import os
import sys
import signal
import logging
import time
import warnings

from argparse import ArgumentParser

# Python version check
_MIN_PYTHON = (3, 8)
if sys.version_info < _MIN_PYTHON:
    sys.exit(
        f"SeatHunter requires Python >= {_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}, "
        f"current: {sys.version_info.major}.{sys.version_info.minor}"
    )

# Suppress typing module DeprecationWarning on Python 3.12+
if sys.version_info >= (3, 12):
    warnings.filterwarnings("ignore", category=DeprecationWarning, module="typing")


def get_app_dir():
    """Get application root directory (PyInstaller-compatible)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def setup_path():
    """Add project root to Python path for imports."""
    app_dir = get_app_dir()
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)


def parse_args():
    """Parse command-line arguments."""
    parser = ArgumentParser(description="HDU Library SeatHunter")
    parser.add_argument(
        "-c", "--config",
        type=str,
        default=os.path.join(get_app_dir(), "config", "config.yaml"),
        help="Config file path (default: config/config.yaml)",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run in daemon mode (no interactive menu, reads config and starts scheduler)",
    )
    parser.add_argument(
        "--cli",
        action="store_true",
        help="Use CLI mode instead of GUI",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Book the plans for two days from today, then exit (GitHub Actions mode)",
    )
    parser.add_argument(
        "--check-login",
        action="store_true",
        help="Validate credentials and account UID, then exit without booking",
    )
    return parser.parse_args()


def run_interactive(config_path: str):
    """Run in interactive mode with GUI (default) or CLI."""
    try:
        import tkinter as tk
    except ImportError:
        run_cli(config_path)
        return

    from seathunter.logging_.logger import setup_logging
    from seathunter.config.manager import ConfigManager
    from seathunter.auth.session_manager import SessionManager
    from seathunter.api.client import ApiClient
    from seathunter.api.room_cache import RoomCache
    from seathunter.ui.gui import GuiApp

    logger = setup_logging()
    logger.info("SeatHunter starting (GUI mode)")

    # Initialize components
    config = ConfigManager(config_path)
    config.load()

    session_mgr = SessionManager(config)
    session_mgr.init_session()

    api_client = ApiClient(session_mgr)
    room_cache = RoomCache(api_client)

    # Create and run GUI
    try:
        root = tk.Tk()
    except tk.TclError:
        print("无法初始化图形界面，切换到命令行模式")
        run_cli(config_path)
        return

    # Hide console after GUI window is ready
    from seathunter.platform_.window import hide_console
    hide_console()

    app = GuiApp(root, config, session_mgr, api_client, room_cache)
    root.mainloop()


def run_cli(config_path: str):
    """Run in CLI mode with full terminal menu."""
    from seathunter.logging_.logger import setup_logging
    from seathunter.config.manager import ConfigManager
    from seathunter.auth.session_manager import SessionManager
    from seathunter.api.client import ApiClient
    from seathunter.api.room_cache import RoomCache
    from seathunter.ui.cli import CliUI
    from seathunter.platform_.window import maximize_window

    # Setup
    maximize_window()
    logger = setup_logging()
    logger.info("SeatHunter starting (CLI mode)")

    # Initialize components
    config = ConfigManager(config_path)
    config.load()

    session_mgr = SessionManager(config)
    session_mgr.init_session()

    api_client = ApiClient(session_mgr)
    room_cache = RoomCache(api_client)

    # Create and run UI
    ui = CliUI(config, session_mgr, api_client, room_cache)
    ui.login()
    ui.run()


def run_daemon(config_path: str):
    """Run in daemon mode: read config, start scheduler, no menu."""
    from seathunter.logging_.logger import setup_logging
    from seathunter.config.manager import ConfigManager
    from seathunter.auth.session_manager import SessionManager
    from seathunter.api.client import ApiClient
    from seathunter.api.room_cache import RoomCache
    from seathunter.scheduler.engine import SchedulerEngine
    from seathunter.scheduler.booking_runner import BookingRunner
    from seathunter.logging_.history import HistoryLogger

    logger = setup_logging()
    logger.info("SeatHunter starting (daemon mode)")

    # Initialize components
    config = ConfigManager(config_path)
    config.load()

    schedules = config.get_schedules()
    active_schedules = [s for s in schedules if s.enabled]
    plans = config.get_plans()

    if not active_schedules:
        logger.error("No active schedules found in config. Exiting.")
        sys.exit(1)
    if not plans:
        logger.error("No plans found in config. Exiting.")
        sys.exit(1)

    logger.info("Found %d active schedule(s) and %d plan(s)",
               len(active_schedules), len(plans))

    # Login
    session_mgr = SessionManager(config)
    session_mgr.init_session()

    success, err = session_mgr.login()
    if not success:
        logger.error("Login failed: %s", err)
        sys.exit(1)
    logger.info("Login successful: uid=%s", session_mgr.uid)

    # Setup API and room cache
    api_client = ApiClient(session_mgr)
    room_cache = RoomCache(api_client)

    # Background room data refresh
    room_cache.start_background_refresh()

    # Settings
    settings = config.get_settings()
    runner = BookingRunner(
        api_client=api_client,
        session_manager=session_mgr,
        interval=settings["interval"],
        max_try_times=settings["max_try_times"],
    )

    engine = SchedulerEngine(
        config_manager=config,
        session_manager=session_mgr,
        booking_runner=runner,
    )

    history = HistoryLogger()

    # Engine callbacks (log-only in daemon mode)
    def on_countdown(remaining, trigger_time, plan_desc):
        from seathunter.ui.display import format_countdown
        logger.info(
            "Countdown: %s -> %s | Remaining: %s",
            trigger_time.strftime("%Y-%m-%d %H:%M"),
            plan_desc,
            format_countdown(remaining),
        )

    def on_result(result):
        history.log(result)
        if result.success:
            logger.info("Booking result: %s", result)
        else:
            logger.warning("Booking result: %s", result)

    def on_start(target_date, plan_ids):
        logger.info("Booking starting for %s, plans: %s",
                    target_date.strftime("%Y-%m-%d"), ", ".join(plan_ids))

    def on_error(error):
        logger.error("Engine error: %s", error)

    engine.on_countdown_tick = on_countdown
    engine.on_booking_result = on_result
    engine.on_booking_start = on_start
    engine.on_error = on_error

    # Handle SIGTERM/SIGINT for clean shutdown
    def signal_handler(signum, frame):
        logger.info("Received signal %s, shutting down...", signum)
        engine.stop()
        room_cache.stop_background_refresh()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    # Start engine
    engine.start()
    logger.info("Scheduler engine started in daemon mode")

    # Block main thread while engine runs
    try:
        while engine.is_running:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt, shutting down...")
        engine.stop()
        room_cache.stop_background_refresh()


# Seconds before the booking window to re-validate the session. A CI run may
# wait hours between login and booking; the cached cookie can expire meanwhile.
SESSION_REFRESH_LEAD = 300


def run_login_check(config_path: str) -> int:
    """Validate the library login and account identity without booking."""
    from seathunter.logging_.logger import setup_logging
    from seathunter.config.manager import ConfigManager
    from seathunter.auth.session_manager import SessionManager
    from seathunter.auth.login_retry import login_with_retry
    from seathunter.scheduler.one_shot import append_github_summary

    logger = setup_logging()
    logger.info("SeatHunter starting (login-check mode; no booking requests)")

    config = ConfigManager(config_path)
    config.load()
    user = config.get_user_info()
    if not user.get("login_name") or not user.get("password"):
        message = "Credentials are missing (SCHOOL_ID or PASSWORD)."
        logger.error(message)
        append_github_summary(["## Login check failed", "", message])
        return 1

    session_mgr = SessionManager(config)
    session_mgr.init_session()
    settings = config.get_settings()
    success, error_type = login_with_retry(
        session_mgr,
        max_attempts=int(settings.get("login_max_attempts", 3)),
        initial_delay=float(settings.get("login_retry_initial_delay_seconds", 30.0)),
        max_delay=float(settings.get("login_retry_max_delay_seconds", 90.0)),
        jitter_ratio=float(settings.get("login_retry_jitter_ratio", 0.15)),
    )
    if not success or not session_mgr.uid:
        reason = error_type or "session returned no uid"
        logger.error("Login check failed: %s", reason)
        append_github_summary([
            "## Login check failed",
            "",
            f"- Reason: {reason}",
            "- No booking request was sent.",
        ])
        return 1

    logger.info(
        "Login check passed: uid=%s, name=%s",
        session_mgr.uid,
        session_mgr.name or "(not returned)",
    )
    append_github_summary([
        "## Login check passed",
        "",
        f"- UID: {session_mgr.uid}",
        f"- Name: {session_mgr.name or '(not returned)'}",
        "- No booking request was sent.",
    ])
    return 0


def run_once(config_path: str) -> int:
    """Run one booking window and exit with a CI-friendly status code."""
    from datetime import datetime, timedelta

    from seathunter.logging_.logger import setup_logging
    from seathunter.logging_.history import HistoryLogger
    from seathunter.config.manager import ConfigManager
    from seathunter.auth.session_manager import SessionManager
    from seathunter.auth.login_retry import login_with_retry
    from seathunter.api.client import ApiClient
    from seathunter.scheduler.booking_runner import BookingRunner
    from seathunter.scheduler.one_shot import (
        append_github_summary,
        booking_open_at,
        collect_plan_ids,
        session_refresh_at,
        target_date_for_run,
        wait_until,
    )

    logger = setup_logging()
    logger.info("SeatHunter starting (one-shot mode)")

    config = ConfigManager(config_path)
    config.load()

    now = datetime.now()
    target_date = target_date_for_run(now)
    schedules = config.get_schedules()
    plan_ids = collect_plan_ids(schedules, target_date)

    if not plan_ids:
        message = f"No enabled plans for {target_date:%Y-%m-%d}; nothing to do."
        logger.info(message)
        append_github_summary(["## SeatHunter", "", message])
        return 0

    plans_map = {plan.id: plan for plan in config.get_plans()}
    missing_plan_ids = [plan_id for plan_id in plan_ids if plan_id not in plans_map]
    if missing_plan_ids:
        logger.error("Schedules reference missing plans: %s", ", ".join(missing_plan_ids))
        return 1
    plans = [plans_map[plan_id] for plan_id in plan_ids]

    invalid_messages = []
    for plan in plans:
        invalid_messages.extend(plan.validate())
    if invalid_messages:
        for message in invalid_messages:
            logger.error(message)
        return 1

    user = config.get_user_info()
    if not user.get("login_name") or not user.get("password"):
        logger.error(
            "Credentials are missing. Set SEATHUNTER_LOGIN_NAME and "
            "SEATHUNTER_PASSWORD (GitHub secrets SCHOOL_ID and PASSWORD)."
        )
        return 1

    settings = config.get_settings()
    try:
        open_at = booking_open_at(datetime.now(), settings["booking_open_time"])
        deadline = None
        if settings.get("booking_deadline"):
            deadline = booking_open_at(
                datetime.now(), settings["booking_deadline"], "booking_deadline"
            )
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    # A delayed Actions dispatch can land after the window has closed. Bail out
    # before logging in rather than firing requests that cannot win a seat.
    if deadline is not None and datetime.now() >= deadline:
        message = (
            f"Run started at {datetime.now():%H:%M:%S}, after the "
            f"{deadline:%H:%M:%S} cutoff; skipping without booking."
        )
        logger.error(message)
        append_github_summary([
            "## SeatHunter booking skipped",
            "",
            f"- Target date: {target_date:%Y-%m-%d}",
            f"- {message}",
        ])
        return 1

    logger.info(
        "Booking policy: opens=%s; deadline=%s; interval=%.3fs; "
        "burst=%s; rate-limit probe=%.3fs; max rounds=%s; max in-flight=%s",
        open_at.strftime("%H:%M:%S"),
        deadline.strftime("%H:%M:%S") if deadline else "none",
        float(settings["interval"]),
        settings.get("burst_interval"),
        float(settings.get("rate_limit_probe_interval", 1.0)),
        settings["max_try_times"],
        settings.get("max_inflight", 1),
    )
    session_mgr = SessionManager(config)
    session_mgr.init_session()
    success, error_type = login_with_retry(
        session_mgr,
        max_attempts=int(settings.get("login_max_attempts", 3)),
        initial_delay=float(settings.get("login_retry_initial_delay_seconds", 30.0)),
        max_delay=float(settings.get("login_retry_max_delay_seconds", 90.0)),
        jitter_ratio=float(settings.get("login_retry_jitter_ratio", 0.15)),
    )
    if not success:
        logger.error("Login failed: %s", error_type)
        append_github_summary(["## SeatHunter", "", f"Login failed: {error_type}"])
        return 1
    if not session_mgr.uid:
        logger.error("Login did not produce a usable uid; refusing to wait")
        append_github_summary([
            "## SeatHunter",
            "",
            "Login validation failed: the library session returned no uid.",
        ])
        return 1
    logger.info("Login verified: uid=%s", session_mgr.uid)

    if datetime.now() < open_at:
        logger.info(
            "Target date: %s; booking opens at %s",
            target_date.strftime("%Y-%m-%d"),
            open_at.strftime("%Y-%m-%d %H:%M:%S"),
        )
        # The runner starts hours early to absorb the Actions dispatch delay,
        # so refresh the session just before firing rather than booking with
        # a cookie obtained hours ago.
        refresh_at = session_refresh_at(
            open_at,
            float(settings.get("session_refresh_lead_seconds", SESSION_REFRESH_LEAD)),
        )
        if datetime.now() < refresh_at:
            wait_until(refresh_at)
            success, error_type = login_with_retry(
                session_mgr,
                max_attempts=int(settings.get("login_max_attempts", 3)),
                initial_delay=float(
                    settings.get("login_retry_initial_delay_seconds", 30.0)
                ),
                max_delay=float(settings.get("login_retry_max_delay_seconds", 90.0)),
                jitter_ratio=float(settings.get("login_retry_jitter_ratio", 0.15)),
            )
            if not success:
                logger.error("Session refresh failed: %s", error_type)
                append_github_summary(
                    ["## SeatHunter", "", f"Session refresh failed: {error_type}"]
                )
                return 1
            if not session_mgr.uid:
                logger.error("Session refresh returned no uid; refusing to book")
                append_github_summary([
                    "## SeatHunter",
                    "",
                    "Session refresh failed validation: no uid.",
                ])
                return 1
            logger.info("Session refreshed and verified: uid=%s", session_mgr.uid)
    else:
        logger.warning("Booking window has already opened; starting immediately")

    if not session_mgr.uid:
        message = (
            "No uid available; a booking request cannot identify the account, "
            "so no attempt can succeed. Sending none."
        )
        logger.error(message)
        append_github_summary([
            "## SeatHunter booking skipped",
            "",
            f"- Target date: {target_date:%Y-%m-%d}",
            f"- {message}",
        ])
        return 1

    # Finish local setup before the opening boundary, including filesystem work.
    runner = BookingRunner(
        api_client=ApiClient(session_mgr),
        session_manager=session_mgr,
        interval=float(settings["interval"]),
        max_try_times=int(settings["max_try_times"]),
        burst_interval=settings.get("burst_interval"),
        burst_from=settings.get("burst_from"),
        burst_to=settings.get("burst_to"),
        rate_limit_probe_interval=float(
            settings.get("rate_limit_probe_interval", 1.0)
        ),
        retry_jitter_ratio=float(settings.get("retry_jitter_ratio", 0.15)),
        max_inflight=settings.get("max_inflight", 1),
    )
    runner.prepare()
    try:
        history = HistoryLogger()
    except OSError as exc:
        logger.warning("History unavailable; booking will continue: %s", exc)
        history = None

    def on_result(result):
        nonlocal history
        if history is not None:
            try:
                history.log(result)
            except OSError as exc:
                logger.warning("History write failed; booking will continue: %s", exc)
                history = None
        if result.success:
            logger.info("Booking result: %s", result)
        else:
            logger.warning("Booking result: %s", result)

    if datetime.now() < open_at:
        wait_until(open_at)
    loop_ready_at = datetime.now()
    logger.info(
        "Booking loop started at %s; local opening offset %.1fms "
        "(not server arrival time)",
        loop_ready_at.isoformat(timespec="milliseconds"),
        (loop_ready_at - open_at).total_seconds() * 1000,
    )
    results = runner.run_booking(
        plans=plans,
        target_date=target_date,
        on_result=on_result,
        deadline=deadline,
    )
    # A concurrent request may see "already reserved" before the actual creator
    # finishes returning. Prefer the explicit creation receipt after draining.
    successful = next((result for result in results if result.created_by_this_run), None)
    if successful is None:
        successful = next((result for result in results if result.success), None)
    if successful:
        if successful.already_reserved:
            append_github_summary([
                "## SeatHunter found an existing reservation",
                "",
                "- This run did not create the reservation.",
                f"- Target date: {target_date:%Y-%m-%d}",
                f"- API result: {successful.message}",
            ])
        else:
            append_github_summary([
                "## SeatHunter booking created",
                "",
                f"- Target date: {target_date:%Y-%m-%d}",
                f"- Plan: {successful.plan_id}",
                f"- Seat: {successful.room_name}-{successful.seat_num}",
            ])
        return 0

    last_message = results[-1].message if results else "No booking request was made"
    append_github_summary([
        "## SeatHunter booking failed",
        "",
        f"- Target date: {target_date:%Y-%m-%d}",
        f"- Result: {last_message}",
    ])
    return 1


def main():
    """Main entry point."""
    setup_path()
    args = parse_args()

    if args.check_login:
        sys.exit(run_login_check(args.config))
    elif args.once:
        sys.exit(run_once(args.config))
    elif args.daemon:
        run_daemon(args.config)
    elif args.cli:
        run_cli(args.config)
    else:
        run_interactive(args.config)


if __name__ == "__main__":
    main()
