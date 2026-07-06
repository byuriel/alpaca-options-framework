"""Event calendar — a wrong answer here either blocks a normal session or
holds long 0DTE gamma into an FOMC statement."""

import datetime

import pytest

import config
import event_calendar as ec

ET = config.ET


def _dt(y, m, d, hh, mm):
    return datetime.datetime(y, m, d, hh, mm, tzinfo=ET)


class TestFomc:
    def test_2026_statement_days(self):
        assert ec.is_fomc_day(datetime.date(2026, 1, 28)) is True
        assert ec.is_fomc_day(datetime.date(2026, 6, 17)) is True
        # first day of the meeting is NOT a statement day
        assert ec.is_fomc_day(datetime.date(2026, 1, 27)) is False
        assert ec.is_fomc_day(datetime.date(2026, 7, 6)) is False

    def test_entry_blackout_window(self):
        fomc = _dt(2026, 1, 28, 13, 30)
        assert ec.entry_blackout_reason(fomc) is not None            # 13:30 blocked
        assert ec.entry_blackout_reason(_dt(2026, 1, 28, 13, 29)) is None
        assert ec.entry_blackout_reason(_dt(2026, 1, 28, 14, 15)) is not None
        # normal day: never blocked
        assert ec.entry_blackout_reason(_dt(2026, 7, 6, 14, 0)) is None

    def test_flatten_window(self):
        assert ec.should_flatten_for_event(_dt(2026, 1, 28, 13, 44)) is None
        assert ec.should_flatten_for_event(_dt(2026, 1, 28, 13, 45)) is not None
        assert ec.should_flatten_for_event(_dt(2026, 7, 6, 13, 45)) is None

    def test_master_switch_disables_everything(self, monkeypatch):
        monkeypatch.setattr(config, "EVENT_BLACKOUT_ENABLED", False)
        assert ec.entry_blackout_reason(_dt(2026, 1, 28, 14, 0)) is None
        assert ec.should_flatten_for_event(_dt(2026, 1, 28, 14, 0)) is None

    def test_flatten_switch_independent_of_blackout(self, monkeypatch):
        monkeypatch.setattr(config, "FOMC_FLATTEN_POSITIONS", False)
        assert ec.should_flatten_for_event(_dt(2026, 1, 28, 14, 0)) is None
        # entry blackout still active
        assert ec.entry_blackout_reason(_dt(2026, 1, 28, 14, 0)) is not None


class TestPremarket:
    def test_nfp_first_friday_rule(self):
        assert ec.is_nfp_day(datetime.date(2026, 6, 5)) is True     # 1st Friday
        assert ec.is_nfp_day(datetime.date(2026, 6, 12)) is False   # 2nd Friday
        assert ec.is_nfp_day(datetime.date(2026, 6, 4)) is False    # Thursday

    def test_cpi_table(self):
        assert "CPI" in ec.premarket_events(datetime.date(2026, 6, 10))
        assert "CPI" not in ec.premarket_events(datetime.date(2026, 6, 11))

    def test_todays_events_aggregates(self):
        ev = ec.todays_events(datetime.date(2026, 1, 28))
        assert any("FOMC" in e for e in ev)
        assert ec.todays_events(datetime.date(2026, 7, 6)) == []

    def test_premarket_events_never_block_entries(self):
        # CPI day, mid-session: premarket events are observe-only
        assert ec.entry_blackout_reason(_dt(2026, 6, 10, 10, 0)) is None
