"""
NYSE trading calendar — holidays, early closes, and session-derived times.

A 0DTE bot MUST know the real session close for the day it is trading:
  - On full holidays there is no session (and no 0DTE expiry) — refuse to run.
  - On early-close days (13:00 ET) a fixed 15:25 time stop never fires and a
    0DTE position is held into expiry: worthless, or ITM auto-exercise into an
    overnight share position. The time stop must be derived from the actual
    close, not hardcoded.

Static tables are used instead of a dependency (pandas_market_calendars) to
keep the framework stdlib-only. Tables cover 2025–2027; startup fails loudly
past the table horizon rather than guessing.
"""

import datetime
from typing import Optional

ET_REGULAR_CLOSE = datetime.time(16, 0)
ET_EARLY_CLOSE   = datetime.time(13, 0)
ET_OPEN          = datetime.time(9, 30)

# NYSE full-close holidays.
_HOLIDAYS = {
    # 2025
    datetime.date(2025, 1, 1),    # New Year's Day
    datetime.date(2025, 1, 20),   # MLK Day
    datetime.date(2025, 2, 17),   # Washington's Birthday
    datetime.date(2025, 4, 18),   # Good Friday
    datetime.date(2025, 5, 26),   # Memorial Day
    datetime.date(2025, 6, 19),   # Juneteenth
    datetime.date(2025, 7, 4),    # Independence Day
    datetime.date(2025, 9, 1),    # Labor Day
    datetime.date(2025, 11, 27),  # Thanksgiving
    datetime.date(2025, 12, 25),  # Christmas
    # 2026
    datetime.date(2026, 1, 1),
    datetime.date(2026, 1, 19),
    datetime.date(2026, 2, 16),
    datetime.date(2026, 4, 3),
    datetime.date(2026, 5, 25),
    datetime.date(2026, 6, 19),
    datetime.date(2026, 7, 3),    # July 4 falls on Saturday — observed Friday
    datetime.date(2026, 9, 7),
    datetime.date(2026, 11, 26),
    datetime.date(2026, 12, 25),
    # 2027
    datetime.date(2027, 1, 1),
    datetime.date(2027, 1, 18),
    datetime.date(2027, 2, 15),
    datetime.date(2027, 3, 26),
    datetime.date(2027, 5, 31),
    datetime.date(2027, 6, 18),   # Juneteenth falls Saturday — observed Friday
    datetime.date(2027, 7, 5),    # July 4 falls Sunday — observed Monday
    datetime.date(2027, 9, 6),
    datetime.date(2027, 11, 25),
    datetime.date(2027, 12, 24),  # Christmas falls Saturday — observed Friday
}

# 13:00 ET early closes.
_EARLY_CLOSES = {
    datetime.date(2025, 7, 3),
    datetime.date(2025, 11, 28),
    datetime.date(2025, 12, 24),
    datetime.date(2026, 11, 27),
    datetime.date(2026, 12, 24),
    datetime.date(2027, 11, 26),
}

_TABLE_YEARS = {2025, 2026, 2027}


def covers(d: datetime.date) -> bool:
    """True if the static tables cover this date's year."""
    return d.year in _TABLE_YEARS


def near_horizon(d: datetime.date, days: int = 30) -> bool:
    """True when within `days` of the table edge — startup warns the operator
    to extend the tables before the bot hard-stops past the horizon."""
    horizon = datetime.date(max(_TABLE_YEARS), 12, 31)
    return (horizon - d).days <= days


def is_trading_day(d: datetime.date) -> bool:
    if not covers(d):
        raise ValueError(
            f"market_calendar tables do not cover {d.year} — "
            f"extend _HOLIDAYS/_EARLY_CLOSES before trading."
        )
    if d.weekday() >= 5:          # Saturday/Sunday
        return False
    return d not in _HOLIDAYS


def session_close(d: datetime.date) -> Optional[datetime.time]:
    """ET close time for the session, or None if not a trading day."""
    if not is_trading_day(d):
        return None
    return ET_EARLY_CLOSE if d in _EARLY_CLOSES else ET_REGULAR_CLOSE


def is_early_close(d: datetime.date) -> bool:
    return d in _EARLY_CLOSES


def shift_for_close(hhmm: str, d: datetime.date) -> str:
    """
    Re-anchor an "HH:MM" config time (defined relative to a 16:00 close) to
    this session's actual close, preserving the offset-from-close.
    E.g. TIME_STOP "15:25" (35 min before close) becomes "12:25" on a 13:00
    early-close day. Regular days return the input unchanged.
    """
    close = session_close(d)
    if close is None or close == ET_REGULAR_CLOSE:
        return hhmm
    h, m = map(int, hhmm.split(":"))
    regular = datetime.datetime.combine(d, ET_REGULAR_CLOSE)
    actual  = datetime.datetime.combine(d, close)
    shifted = datetime.datetime.combine(d, datetime.time(h, m)) - (regular - actual)
    return shifted.strftime("%H:%M")
