"""Entry signal gates + quote quality helpers."""

import datetime

import pytest

import config
import signals
from momentum import MomentumState
from signals import Quote, ProxyDeltaTracker, check_entry, _zone


def _quote(bid=0.48, ask=0.52, symbol="SPY260706C00625000"):
    return Quote(symbol=symbol, bid=bid, ask=ask,
                 timestamp=datetime.datetime.now(tz=config.ET))


class TestQuote:
    def test_mid(self):
        assert _quote(0.40, 0.60).mid == pytest.approx(0.50)

    def test_one_sided_mid_falls_back(self):
        assert _quote(0.0, 0.60).mid == 0.60
        assert _quote(0.40, 0.0).mid == 0.40

    def test_spread_pct(self):
        assert _quote(0.45, 0.55).spread_pct == pytest.approx(0.10 / 0.50)

    def test_one_sided_spread_is_inf(self):
        assert _quote(0.0, 0.60).spread_pct == float("inf")


class TestZone:
    def test_activation(self):
        assert _zone(spy_price=625.0, strike=625.0 * 1.002) == "activation"

    def test_approach(self):
        assert _zone(spy_price=625.0, strike=625.0 * 1.005) == "approach"

    def test_dead(self):
        assert _zone(spy_price=625.0, strike=625.0 * 1.02) == "dead"


class TestProxyDelta:
    def test_delta_from_paired_moves(self):
        tr = ProxyDeltaTracker()
        t0 = datetime.datetime.now(tz=config.ET)
        tr.update(0.50, 620.0, t0)
        tr.update(0.80, 621.0, t0 + datetime.timedelta(minutes=1))
        assert tr.proxy_delta == pytest.approx(0.30)

    def test_no_update_when_spy_static(self):
        tr = ProxyDeltaTracker()
        t0 = datetime.datetime.now(tz=config.ET)
        tr.update(0.50, 620.0, t0)
        tr.update(0.90, 620.0, t0 + datetime.timedelta(seconds=1))
        assert tr.proxy_delta == 0.0

    def test_clamped(self):
        tr = ProxyDeltaTracker()
        t0 = datetime.datetime.now(tz=config.ET)
        tr.update(0.50, 620.0, t0)
        tr.update(5.00, 620.5, t0 + datetime.timedelta(minutes=1))
        assert tr.proxy_delta <= 1.0


class TestCheckEntry:
    """check_entry gates, with the wall-clock window pinned open."""

    @pytest.fixture(autouse=True)
    def _open_window(self, monkeypatch):
        monkeypatch.setattr(signals, "_in_entry_window", lambda: True)

    def _kwargs(self, **overrides):
        kw = dict(
            side="call",
            strike=627.0,
            option_quote=_quote(),
            momentum=MomentumState(direction="bull"),
            proxy_tracker=ProxyDeltaTracker(),
            spy_price=627.0 * (1 - 0.002),   # inside activation zone, OTM
            trades_today=0,
            has_open_pos=False,
            atr5=0.30,
        )
        kw.update(overrides)
        return kw

    def test_all_gates_pass(self):
        assert check_entry(**self._kwargs()) is True

    def test_blocked_by_open_position(self):
        assert check_entry(**self._kwargs(has_open_pos=True)) is False

    def test_blocked_by_low_atr(self):
        assert check_entry(**self._kwargs(atr5=config.ATR5_MIN_ENTRY - 0.01)) is False

    def test_blocked_by_wrong_momentum(self):
        assert check_entry(**self._kwargs(
            momentum=MomentumState(direction="bear"))) is False

    def test_blocked_when_already_itm(self):
        # call with SPY at/above strike = gamma move already happened
        assert check_entry(**self._kwargs(spy_price=628.0)) is False

    def test_blocked_outside_activation_zone(self):
        assert check_entry(**self._kwargs(spy_price=627.0 * (1 - 0.02))) is False

    def test_blocked_by_price_band(self):
        assert check_entry(**self._kwargs(
            option_quote=_quote(bid=0.05, ask=0.07))) is False
        assert check_entry(**self._kwargs(
            option_quote=_quote(bid=11.0, ask=11.4))) is False

    def test_put_direction_symmetry(self):
        assert check_entry(**self._kwargs(
            side="put",
            strike=623.0,
            momentum=MomentumState(direction="bear"),
            spy_price=623.0 * (1 + 0.002),
        )) is True
