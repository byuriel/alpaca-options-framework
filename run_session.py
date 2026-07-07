#!/usr/bin/env python3
"""
Daily session supervisor — the hands-off launcher.

Windows Task Scheduler (or cron/systemd) fires this ONCE each weekday
morning. It owns the full daily lifecycle so nothing else has to:

  1. Pre-flight gate (ET): is today a trading day, and are we still within
     the session window? On a holiday/weekend, or if fired too late, it logs
     and exits WITHOUT launching the bot — so a blind "every weekday" trigger
     is safe; the calendar decides.
  2. Launch main.py as a child with AOF_SUPERVISED=1 and no stdin (so the
     bot's keyboard thread stays off and its watchdog restarts via a clean
     exit code instead of os.execl — correct under a waiting parent on
     Windows, where execl changes the PID and orphans the replacement).
  3. Relaunch on the supervised-restart exit code, bounded, and only while
     the restart-storm halt flag is clear.
  4. After the session ends (the bot hard-exits at the time stop), run
     reconcile.py and feed_monitor.py and record their verdicts to the
     supervisor log — the daily audit runs itself.

Pure-decision logic (should_launch) is isolated and unit-tested; everything
else is orchestration.
"""

import datetime
import logging
import os
import subprocess
import sys

import config
import market_calendar

ROOT = os.path.dirname(os.path.abspath(__file__))
logger = logging.getLogger("supervisor")


# ── Pure decision (unit-tested) ───────────────────────────────────────────────

def should_launch(now_et: datetime.datetime):
    """Return (launch: bool, reason: str). ET-aware; early-close-aware."""
    d = now_et.date()
    if not market_calendar.covers(d):
        return False, (f"calendar tables do not cover {d.year} — extend "
                       f"market_calendar.py")
    if not market_calendar.is_trading_day(d):
        return False, f"{d:%A %Y-%m-%d} is not a trading day"
    entry_end = config.ENTRY_END
    if market_calendar.is_early_close(d):
        entry_end = market_calendar.shift_for_close(entry_end, d)
    if now_et.strftime("%H:%M") >= entry_end:
        return False, (f"fired at {now_et:%H:%M} ET — past the {entry_end} ET "
                       f"entry cutoff; no session worth starting")
    return True, f"{d:%A %Y-%m-%d} trading day, {now_et:%H:%M} ET within window"


# ── Orchestration ─────────────────────────────────────────────────────────────

def _setup_logging():
    os.makedirs(os.path.join(ROOT, config.LOG_DIR), exist_ok=True)
    day = datetime.datetime.now(tz=config.ET).date().isoformat()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] supervisor: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(os.path.join(ROOT, config.LOG_DIR,
                                             f"supervisor_{day}.log")),
        ],
    )


def _run_bot_with_relaunch() -> int:
    """Launch the bot; relaunch on the supervised-restart code, bounded and
    only while no halt flag is set. Returns the final exit code."""
    import restart_guard
    env = os.environ.copy()
    env["AOF_SUPERVISED"] = "1"
    max_launches = config.RESTART_STORM_MAX + 1
    launches = 0
    while True:
        launches += 1
        logger.info("Launching bot (attempt %d/%d)...", launches, max_launches)
        code = subprocess.call(
            [sys.executable, "main.py"],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
        )
        logger.info("Bot exited with code %d.", code)
        if code != config.SUPERVISED_RESTART_CODE:
            return code
        if launches >= max_launches:
            logger.error("Supervised-restart cap reached (%d) — not relaunching.",
                         max_launches)
            return code
        if restart_guard.halt_active(os.path.join(ROOT, config.LOG_DIR)):
            logger.error("Restart-storm halt flag set — not relaunching.")
            return code
        logger.warning("Supervised restart requested — relaunching in 2s.")
        import time
        time.sleep(2)


def _post_session():
    """Run the daily audit tools and log their verdicts. Failures here are
    logged, never fatal — the trading session already happened."""
    def _tool(name, args):
        try:
            r = subprocess.run([sys.executable, *args], cwd=ROOT,
                               capture_output=True, text=True, timeout=300)
            tail = (r.stdout or r.stderr or "").strip().splitlines()
            logger.info("%s exit=%d\n    %s", name, r.returncode,
                        "\n    ".join(tail[-12:]))
            return r.returncode
        except Exception as e:
            logger.error("%s failed to run: %s", name, e)
            return -1

    logger.info("── Post-session audit ─────────────────────────────")
    _tool("reconcile.py", ["reconcile.py"])

    import recorder
    day = datetime.datetime.now(tz=config.ET).date()
    rec = os.path.join(ROOT, recorder.default_recording_path(
        config.RECORDINGS_DIR, day))
    if os.path.exists(rec):
        _tool("feed_monitor.py", ["feed_monitor.py", rec, "--json"])
    else:
        logger.info("No recording at %s — skipping feed monitor "
                    "(session may not have opened).", rec)


def main() -> int:
    _setup_logging()
    now = datetime.datetime.now(tz=config.ET)
    launch, reason = should_launch(now)
    logger.info("Pre-flight: %s", reason)
    if not launch:
        return 0

    import restart_guard
    halt = restart_guard.halt_active(os.path.join(ROOT, config.LOG_DIR))
    if halt:
        logger.critical("HALTED before launch: %s — clear with "
                        "`python restart_guard.py --clear`.", halt)
        return 0

    _run_bot_with_relaunch()
    _post_session()
    logger.info("Session supervisor done for %s.", now.date())
    return 0


if __name__ == "__main__":
    sys.exit(main())
