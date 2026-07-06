"""Trading calendar — a wrong answer here holds a 0DTE position into expiry."""

import datetime

import pytest

import market_calendar as mc


class TestTradingDays:
    def test_regular_weekday(self):
        assert mc.is_trading_day(datetime.date(2026, 7, 6)) is True    # Monday

    def test_weekend(self):
        assert mc.is_trading_day(datetime.date(2026, 7, 4)) is False   # Saturday
        assert mc.is_trading_day(datetime.date(2026, 7, 5)) is False   # Sunday

    def test_observed_holiday(self):
        # July 4 2026 falls on Saturday → observed Friday July 3
        assert mc.is_trading_day(datetime.date(2026, 7, 3)) is False

    def test_thanksgiving(self):
        assert mc.is_trading_day(datetime.date(2026, 11, 26)) is False

    def test_out_of_table_year_raises(self):
        with pytest.raises(ValueError):
            mc.is_trading_day(datetime.date(2030, 1, 2))


class TestSessionClose:
    def test_regular_close(self):
        assert mc.session_close(datetime.date(2026, 7, 6)) == datetime.time(16, 0)

    def test_early_close_day_after_thanksgiving(self):
        assert mc.session_close(datetime.date(2026, 11, 27)) == datetime.time(13, 0)
        assert mc.is_early_close(datetime.date(2026, 11, 27)) is True

    def test_christmas_eve_2026(self):
        assert mc.session_close(datetime.date(2026, 12, 24)) == datetime.time(13, 0)

    def test_holiday_has_no_session(self):
        assert mc.session_close(datetime.date(2026, 12, 25)) is None


class TestHorizon:
    def test_far_from_horizon(self):
        assert mc.near_horizon(datetime.date(2026, 7, 6)) is False

    def test_near_horizon_warns(self):
        assert mc.near_horizon(datetime.date(2027, 12, 15)) is True


class TestShiftForClose:
    def test_regular_day_unchanged(self):
        assert mc.shift_for_close("15:25", datetime.date(2026, 7, 6)) == "15:25"

    def test_time_stop_shifts_with_early_close(self):
        # 15:25 is 35 min before a 16:00 close → 12:25 before a 13:00 close
        assert mc.shift_for_close("15:25", datetime.date(2026, 11, 27)) == "12:25"

    def test_entry_end_shifts_with_early_close(self):
        # 14:30 is 90 min before close → 11:30 on an early-close day
        assert mc.shift_for_close("14:30", datetime.date(2026, 11, 27)) == "11:30"
