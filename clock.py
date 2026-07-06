"""
Single time source for every DECISION path in the bot.

Why this exists: deterministic replay. The strategy's behavior depends on
wall-clock reads (entry window, quote freshness, staleness ages, timestamps
booked into the CSV). If decision code calls datetime.now()/time.monotonic()
directly, a replayed session can never reproduce the live one — the replay
harness must be able to install a simulated clock driven by the RECORDED
receive timestamps, so the same events produce the same decisions, fills,
and CSV rows, byte for byte.

Rules:
  - Decision/safety code (signals, state, entry/exit paths, staleness ages)
    uses clock.now_et() / clock.monotonic() / clock.today_et().
  - Live-only plumbing (watchdog thread, status display, log formatting)
    may keep using the real clock — it is not replayed.

In live mode this delegates to the real clock with zero behavioral change:
monotonic() is time.monotonic() and now_et() is datetime.now(ET).
"""

import datetime
import time

import config

ET = config.ET   # one timezone definition — config owns it, clock reuses it


class LiveClock:
    """Real time — the default."""

    def now_et(self) -> datetime.datetime:
        return datetime.datetime.now(tz=ET)

    def monotonic(self) -> float:
        return time.monotonic()


class SimClock:
    """
    Deterministic clock for replay, advanced by the harness to each recorded
    event's receive time (epoch seconds). monotonic() shares the same axis —
    ages and deadlines measured against it are faithful to what the live
    process experienced.
    """

    def __init__(self, start_wall: float):
        self._wall = float(start_wall)

    def advance_to(self, wall: float):
        """Monotonic advance only — recorded receive times can never move
        the clock backwards (out-of-order lines are clamped)."""
        if wall > self._wall:
            self._wall = float(wall)

    def advance_by(self, seconds: float):
        self._wall += float(seconds)

    def now_et(self) -> datetime.datetime:
        return datetime.datetime.fromtimestamp(self._wall, tz=ET)

    def monotonic(self) -> float:
        return self._wall


_active = LiveClock()


def install(clk) -> None:
    """Install a clock (replay). Callers MUST restore with install_live()."""
    global _active
    _active = clk


def install_live() -> None:
    global _active
    _active = LiveClock()


def now_et() -> datetime.datetime:
    return _active.now_et()


def monotonic() -> float:
    return _active.monotonic()


def today_et() -> datetime.date:
    return _active.now_et().date()
