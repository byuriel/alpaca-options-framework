"""
Scheduled macro-event calendar — blackouts for a 0DTE options bot.

Why this exists: 0DTE around a scheduled macro release is a different asset
class. The FOMC statement drops at 14:00 ET — INSIDE this strategy's entry
window (09:45–14:30) — and holding long 0DTE gamma through it is a coin flip
on a headline, not the strategy. Premarket releases (CPI, NFP at 08:30 ET)
shape the whole session's regime but don't interrupt it mid-day.

Two event classes, two treatments:

  INTRADAY (FOMC statement days, 14:00 ET):
    - new entries blocked from FOMC_ENTRY_BLACKOUT_START (default 13:30)
    - any open position flattened at FOMC_FLATTEN_TIME (default 13:45)
    This is risk management, not an alpha filter — it defaults ON.

  PREMARKET (CPI / NFP, 08:30 ET):
    - logged at startup and available to filters; by default OBSERVE-ONLY
      (the repo's shadow-first philosophy: measure before you gate).
    - optional: PREMARKET_EVENT_OPEN_DELAY_MIN pushes ENTRY_START later on
      these days once you have evidence it helps.

Date tables follow the same static-table philosophy as market_calendar.py:
deterministic, reviewable, no network dependency — and honest about
provenance. FOMC dates come from the Fed's published meeting calendar
(2025–2026 announced; 2027 TENTATIVE — the Fed publishes ~2 years ahead,
verify when the final calendar posts). NFP is derived by the first-Friday
rule (BLS occasionally shifts around holidays — verify month by month if
you gate on it). CPI dates must be maintained from the BLS release schedule
(https://www.bls.gov/schedule/news_release/cpi.htm) — the shipped 2026 table
is TENTATIVE and premarket events are observe-only by default precisely so
an unverified date cannot affect trading.
"""

import datetime
from typing import List, Optional

import config

# ── FOMC statement days (second day of each meeting; statement 14:00 ET) ──────
FOMC_STATEMENT_DAYS = {
    # 2025 — historical/confirmed
    datetime.date(2025, 1, 29), datetime.date(2025, 3, 19),
    datetime.date(2025, 5, 7),  datetime.date(2025, 6, 18),
    datetime.date(2025, 7, 30), datetime.date(2025, 9, 17),
    datetime.date(2025, 10, 29), datetime.date(2025, 12, 10),
    # 2026 — from the Fed's announced calendar
    datetime.date(2026, 1, 28), datetime.date(2026, 3, 18),
    datetime.date(2026, 4, 29), datetime.date(2026, 6, 17),
    datetime.date(2026, 7, 29), datetime.date(2026, 9, 16),
    datetime.date(2026, 10, 28), datetime.date(2026, 12, 9),
    # 2027 — TENTATIVE (verify against the Fed's final calendar)
    datetime.date(2027, 1, 27), datetime.date(2027, 3, 17),
    datetime.date(2027, 4, 28), datetime.date(2027, 6, 16),
    datetime.date(2027, 7, 28), datetime.date(2027, 9, 15),
    datetime.date(2027, 10, 27), datetime.date(2027, 12, 8),
}

# ── CPI release days (08:30 ET, premarket) ────────────────────────────────────
# TENTATIVE — maintain from the BLS schedule. Observe-only by default, so an
# unverified date cannot affect trading.
CPI_DAYS = {
    datetime.date(2026, 1, 13), datetime.date(2026, 2, 11),
    datetime.date(2026, 3, 11), datetime.date(2026, 4, 10),
    datetime.date(2026, 5, 12), datetime.date(2026, 6, 10),
    datetime.date(2026, 7, 14), datetime.date(2026, 8, 12),
    datetime.date(2026, 9, 11), datetime.date(2026, 10, 13),
    datetime.date(2026, 11, 10), datetime.date(2026, 12, 10),
}


def is_nfp_day(d: datetime.date) -> bool:
    """Nonfarm payrolls: first Friday of the month (08:30 ET). BLS shifts
    occasionally around holidays — verify before gating on this."""
    return d.weekday() == 4 and d.day <= 7


def is_fomc_day(d: datetime.date) -> bool:
    return d in FOMC_STATEMENT_DAYS


def premarket_events(d: datetime.date) -> List[str]:
    """Premarket (08:30 ET) releases scheduled for this session."""
    out = []
    if d in CPI_DAYS:
        out.append("CPI")
    if is_nfp_day(d):
        out.append("NFP")
    return out


def todays_events(d: datetime.date) -> List[str]:
    """All scheduled events for the session — for startup announcement and
    recording metadata."""
    out = premarket_events(d)
    if is_fomc_day(d):
        out.append("FOMC_STATEMENT_14:00ET")
    return out


def entry_blackout_reason(now_et: datetime.datetime) -> Optional[str]:
    """
    Non-None when NEW ENTRIES are blocked right now by a scheduled event.
    Currently: FOMC statement days from FOMC_ENTRY_BLACKOUT_START onward —
    a fresh long-gamma position minutes before the statement is a bet on a
    headline, not on the strategy.
    """
    if not config.EVENT_BLACKOUT_ENABLED:
        return None
    if is_fomc_day(now_et.date()):
        if now_et.strftime("%H:%M") >= config.FOMC_ENTRY_BLACKOUT_START:
            return "FOMC statement blackout"
    return None


def should_flatten_for_event(now_et: datetime.datetime) -> Optional[str]:
    """
    Non-None when an open position should be flattened NOW ahead of a
    scheduled intraday event. The event watcher (live) and the replay pump
    both consult this, so replayed FOMC days behave like live ones.
    """
    if not (config.EVENT_BLACKOUT_ENABLED and config.FOMC_FLATTEN_POSITIONS):
        return None
    if is_fomc_day(now_et.date()):
        if now_et.strftime("%H:%M") >= config.FOMC_FLATTEN_TIME:
            return "FOMC statement"
    return None
