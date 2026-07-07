"""Supervisor decision logic — the gate that decides whether a session runs.
A wrong answer either skips a trading day or launches a pointless run."""

import datetime

import config
import market_calendar as mc
from run_session import should_launch

ET = config.ET


def _et(y, m, d, hh, mm):
    return datetime.datetime(y, m, d, hh, mm, tzinfo=ET)


class TestShouldLaunch:
    def test_regular_trading_morning_launches(self):
        ok, why = should_launch(_et(2026, 7, 6, 9, 0))   # Monday 09:00 ET
        assert ok is True
        assert "trading day" in why

    def test_weekend_skipped(self):
        ok, why = should_launch(_et(2026, 7, 4, 9, 0))   # Saturday
        assert ok is False
        assert "not a trading day" in why

    def test_holiday_skipped(self):
        # Fri 2026-07-03 — observed Independence Day
        ok, why = should_launch(_et(2026, 7, 3, 9, 0))
        assert ok is False
        assert "not a trading day" in why

    def test_past_entry_cutoff_skipped(self):
        # Fired late (StartWhenAvailable after the machine was off) — nothing
        # to run once the entry window has closed.
        ok, why = should_launch(_et(2026, 7, 6, 14, 45))
        assert ok is False
        assert "past" in why and "cutoff" in why

    def test_just_before_cutoff_still_launches(self):
        ok, _ = should_launch(_et(2026, 7, 6, 14, 29))
        assert ok is True

    def test_early_close_cutoff_shifts(self):
        # 2026-11-27 is a 13:00 ET early close → entry cutoff shifts earlier.
        assert mc.is_early_close(datetime.date(2026, 11, 27))
        # 11:29 is before the shifted cutoff → launch
        ok_before, _ = should_launch(_et(2026, 11, 27, 11, 29))
        # 12:00 is past it → skip (would be fine on a regular day)
        ok_after, why = should_launch(_et(2026, 11, 27, 12, 0))
        assert ok_before is True
        assert ok_after is False and "cutoff" in why

    def test_out_of_calendar_range_skipped_not_crashed(self):
        ok, why = should_launch(_et(2030, 3, 4, 9, 0))
        assert ok is False
        assert "do not cover" in why
