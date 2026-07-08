"""
Restart-storm brake - an infinite failure loop becomes one loud stop.

The watchdog auto-restarts the process when the event loop goes silent.
That is the right call for a one-off hang - and exactly the wrong thing to
do forever: a restart CASCADE (bad dependency update, corrupted state, an
API change at the broker) means every boot re-enters the same failure,
possibly re-entering positions between crashes, with nobody watching. This
repo's own history includes a 15x watchdog loop (Jun 30).

Mechanism (file-based - it must survive the very restarts it counts):
  - The watchdog appends a timestamp to logs/restart_log.txt before execl.
  - At startup, main() counts restarts inside RESTART_STORM_WINDOW_SEC.
    At RESTART_STORM_MAX or more: flatten this bot's positions via REST,
    write logs/restart_halt.flag, alert, and exit. Every subsequent start
    refuses to run until the operator investigates and clears:

        python restart_guard.py --clear

Scope note: this counts WATCHDOG restarts only. Keyboard 'r' restarts are
intentional operator actions, and service-manager crash loops belong to the
service manager (systemd StartLimitBurst) - both deliberately excluded.
"""

import argparse
import os
import time
from typing import Optional

import config

RESTART_LOG = "restart_log.txt"
HALT_FLAG   = "restart_halt.flag"


def _path(name: str, log_dir: Optional[str] = None) -> str:
    return os.path.join(log_dir or config.LOG_DIR, name)


def record_restart(log_dir: Optional[str] = None):
    """Called by the watchdog immediately before os.execl."""
    try:
        os.makedirs(log_dir or config.LOG_DIR, exist_ok=True)
        with open(_path(RESTART_LOG, log_dir), "a") as f:
            f.write(f"{time.time():.0f}\n")
    except OSError:
        pass   # the restart itself must not be blocked by a disk hiccup


def restarts_in_window(now: Optional[float] = None,
                       log_dir: Optional[str] = None) -> int:
    """Watchdog restarts within the storm window. Prunes old lines so the
    file cannot grow without bound."""
    now = now if now is not None else time.time()
    path = _path(RESTART_LOG, log_dir)
    try:
        with open(path) as f:
            stamps = [float(x) for x in f.read().split()]
    except (OSError, ValueError):
        return 0
    recent = [s for s in stamps if now - s <= config.RESTART_STORM_WINDOW_SEC]
    if len(recent) < len(stamps):
        try:
            with open(path, "w") as f:
                f.writelines(f"{s:.0f}\n" for s in recent)
        except OSError:
            pass
    return len(recent)


def halt_active(log_dir: Optional[str] = None) -> Optional[str]:
    """Reason string if a halt flag is set, else None."""
    path = _path(HALT_FLAG, log_dir)
    try:
        with open(path) as f:
            return f.read().strip() or "restart storm (no reason recorded)"
    except OSError:
        return None


def trigger_halt(reason: str, log_dir: Optional[str] = None):
    os.makedirs(log_dir or config.LOG_DIR, exist_ok=True)
    with open(_path(HALT_FLAG, log_dir), "w") as f:
        f.write(reason + "\n")


def clear_halt(log_dir: Optional[str] = None) -> bool:
    cleared = False
    for name in (HALT_FLAG, RESTART_LOG):
        try:
            os.remove(_path(name, log_dir))
            cleared = True
        except FileNotFoundError:
            pass
    return cleared


def main_cli():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--clear", action="store_true",
                    help="clear the halt flag and restart counter after review")
    args = ap.parse_args()
    if args.clear:
        print("Cleared." if clear_halt() else "Nothing to clear.")
    else:
        halt = halt_active()
        n = restarts_in_window()
        print(f"halt: {halt or 'none'} | restarts in window: {n}/"
              f"{config.RESTART_STORM_MAX}")


if __name__ == "__main__":
    main_cli()
