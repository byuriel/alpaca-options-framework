"""Momentum engine — session hygiene. Premarket bars must never contaminate
VWAP/streaks (README: 'resets at 9:30 ET'), and EMAs must stay continuous."""

import datetime

import config
from momentum import Bar, MomentumEngine


def _bar(hh, mm, o, h, l, c, vol=1000.0, day=6):
    t = datetime.datetime(2026, 7, day, hh, mm, tzinfo=config.ET)
    return Bar(t=t, open=o, high=h, low=l, close=c, volume=vol)


class TestPremarketHygiene:
    def test_premarket_bars_update_emas_only(self):
        eng = MomentumEngine()
        eng.on_bar(_bar(8, 0, 100, 101, 99, 100.5, vol=50))   # premarket
        assert eng.state.ema5 > 0                             # EMA warmed
        assert eng.state.vwap == 0.0                          # VWAP untouched
        assert eng.state.consec_green == 0                    # streaks untouched
        assert eng.state.direction == "neutral"

    def test_premarket_volume_not_in_vwap(self):
        eng = MomentumEngine()
        # Huge low-priced premarket print that would poison VWAP if counted
        eng.on_bar(_bar(7, 0, 90, 90, 90, 90, vol=1e9))
        eng.on_bar(_bar(9, 30, 100, 101, 99, 100, vol=1000))
        # VWAP must reflect only the RTH bar (typical price = 100)
        assert abs(eng.state.vwap - 100.0) < 0.5


class TestDirection:
    def _run_bull_sequence(self, eng):
        # Rising closes above VWAP, enough consecutive green bars, positive ROC
        px = 100.0
        for i in range(10):
            px += 0.20
            eng.on_bar(_bar(9, 31 + i, px - 0.15, px + 0.05, px - 0.20, px))
        return eng.state

    def test_bull_direction_after_consecutive_green(self):
        eng = MomentumEngine()
        st  = self._run_bull_sequence(eng)
        assert st.consec_green >= config.MIN_CONSEC_BARS
        assert st.direction == "bull"

    def test_session_reset_on_new_day(self):
        eng = MomentumEngine()
        self._run_bull_sequence(eng)
        ema5_before = eng.state.ema5
        # First RTH bar of the NEXT day: VWAP/streaks reset, EMAs carry
        eng.on_bar(_bar(9, 30, 102, 102.5, 101.5, 102, day=7))
        assert eng.state.consec_green <= 1        # streak reset
        assert abs(eng.state.ema5 - ema5_before) < 1.0   # EMA continuous


class TestATR:
    def test_atr5_is_rolling_mean_of_ranges(self):
        eng = MomentumEngine()
        for i in range(5):
            eng.on_bar(_bar(9, 31 + i, 100, 100.4, 100.0, 100.2))
        assert abs(eng.state.atr5 - 0.4) < 1e-9
