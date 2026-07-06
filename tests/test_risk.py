"""Risk manager — the limits must be HARD. Every test here encodes a rule a
prop-firm risk officer will check: caps cannot be exceeded by rounding, and
the daily loss gate blocks prospectively."""

import config
from risk import RiskManager


def _rm() -> RiskManager:
    return RiskManager()


class TestSizing:
    def test_normal_sizing_respects_risk_cap(self):
        # $0.60 entry: risk/contract = 0.60*0.5*100 = $30 → 5 contracts at $150 cap
        qty = _rm().size_trade(0.60)
        assert qty == 5
        assert qty * 0.60 * (1 - config.STOP_MULT) * 100 <= config.MAX_RISK_PER_TRADE

    def test_expensive_option_is_skipped_not_rounded_to_one(self):
        # $6.00 entry: risk/contract = $300 > $150 cap. The old max(1, ...)
        # would trade 1 contract at 2× the configured risk. Must be 0.
        assert _rm().size_trade(6.00) == 0

    def test_max_config_price_is_skipped(self):
        # OPTION_MAX_PRICE ($10) → $500 stop risk, $1000 premium. Must be 0.
        assert _rm().size_trade(config.OPTION_MAX_PRICE) == 0

    def test_premium_cap_binds_on_cheap_options(self):
        # $0.25 entry: risk cap allows 12 contracts ($150/$12.50) but premium
        # cap allows MAX_PREMIUM_PER_TRADE/$25 — the tighter bound must win.
        qty = _rm().size_trade(0.25)
        assert qty * 0.25 * 100 <= config.MAX_PREMIUM_PER_TRADE
        assert qty * 0.25 * (1 - config.STOP_MULT) * 100 <= config.MAX_RISK_PER_TRADE

    def test_zero_and_negative_prices(self):
        assert _rm().size_trade(0.0) == 0
        assert _rm().size_trade(-1.0) == 0


class TestDailyLossGate:
    def test_gate_open_at_start(self):
        assert _rm().can_trade() is True

    def test_prospective_block_before_limit_is_booked(self):
        # daily_pnl such that one more full stop-out breaches the limit →
        # blocked BEFORE the loss exists, not after.
        rm = _rm()
        rm.restore_day(-(config.MAX_DAILY_LOSS - config.MAX_RISK_PER_TRADE), 1)
        rm.tick_bar(); rm.tick_bar(); rm.tick_bar()   # clear any cooldown
        assert rm.can_trade() is False

    def test_headroom_allows_trading(self):
        rm = _rm()
        rm.restore_day(-(config.MAX_DAILY_LOSS - config.MAX_RISK_PER_TRADE - 1.0), 0)
        assert rm.can_trade() is True

    def test_lock_on_limit_hit(self):
        rm = _rm()
        rm.record_trade(-config.MAX_DAILY_LOSS)
        assert rm.locked is True
        assert rm.can_trade() is False

    def test_manual_lock(self):
        rm = _rm()
        rm.lock("kill switch test")
        assert rm.locked is True
        assert "kill switch" in rm.lock_reason
        assert rm.can_trade() is False

    def test_restore_relocks(self):
        rm = _rm()
        rm.restore_day(-config.MAX_DAILY_LOSS - 10, 3)
        assert rm.locked is True


class TestWeeklyLimit:
    def test_week_baseline_plus_daily_locks_at_limit(self):
        rm = _rm()
        rm.set_week_baseline(-(config.WEEKLY_MAX_LOSS - 50.0))
        assert rm.locked is False
        rm.record_trade(-50.0)                 # week total hits the line
        assert rm.locked is True
        assert "WEEKLY" in rm.lock_reason

    def test_already_breached_week_locks_at_startup(self):
        rm = _rm()
        rm.set_week_baseline(-config.WEEKLY_MAX_LOSS - 1.0)
        assert rm.locked is True

    def test_prospective_weekly_gate_blocks_entry(self):
        # Enough weekly headroom for the day gate but not the week gate
        rm = _rm()
        rm.set_week_baseline(
            -(config.WEEKLY_MAX_LOSS - config.MAX_RISK_PER_TRADE))
        assert rm.can_trade() is False

    def test_weekly_headroom_allows_trading(self):
        rm = _rm()
        rm.set_week_baseline(
            -(config.WEEKLY_MAX_LOSS - config.MAX_RISK_PER_TRADE - 1.0))
        assert rm.can_trade() is True

    def test_profitable_week_no_effect(self):
        rm = _rm()
        rm.set_week_baseline(500.0)
        assert rm.can_trade() is True
        assert rm.week_pnl == 500.0


class TestCooldown:
    def test_cooldown_blocks_then_expires(self):
        rm = _rm()
        rm.record_trade(10.0)
        assert rm.can_trade() is False
        for _ in range(config.TRADE_COOLDOWN_BARS):
            rm.tick_bar()
        assert rm.can_trade() is True
