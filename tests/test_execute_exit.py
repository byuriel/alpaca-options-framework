"""_execute_exit invariants — the fix for the double-counted-P&L bug class.

A failed close must NEVER book a row or erase local tracking; a confirmed
fill must book exactly once; partial fills must book per-leg but count as ONE
trade; a close order that fills after its fill-wait gave up must be
reconciled from order history and booked with the REAL price.
"""

import asyncio
import csv
import datetime
import glob
import os
from types import SimpleNamespace

import pytest

import config
import state as state_mod
from orders import OrderManager
from risk import RiskManager
from state import BotState, Position

import main


def _pos(qty=2):
    return Position(
        symbol="SPY260706C00625000", side="call", strike=625.0,
        qty=qty, entry_price=0.50,
        entry_time=datetime.datetime(2026, 7, 6, 10, 15, tzinfo=config.ET),
        order_id="entry-1", entry_spy_price=622.8, entry_atr5=0.25,
    )


class FakeOrderManager:
    """Scripted close_position responses; real static fill helpers."""
    def __init__(self, script, broker_positions=None, lost_fill=None):
        self.script = list(script)            # per-attempt: Order-like or None
        self.calls  = []
        self.broker_positions = broker_positions if broker_positions is not None else []
        self.lost_fill = lost_fill            # returned by find_recent_close_fill

    async def close_position(self, symbol, qty):
        self.calls.append((symbol, qty))
        return self.script.pop(0) if self.script else None

    async def get_open_positions_async(self):
        return self.broker_positions

    async def find_recent_close_fill(self, symbol):
        return self.lost_fill

    get_fill_price = staticmethod(OrderManager.get_fill_price)
    get_filled_qty = staticmethod(OrderManager.get_filled_qty)


def _order(fill_px, fill_qty, oid="exit-1"):
    return SimpleNamespace(id=oid, filled_avg_price=str(fill_px),
                           filled_qty=str(fill_qty))


def _broker_pos(symbol="SPY260706C00625000"):
    return SimpleNamespace(symbol=symbol)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(state_mod, "POSITION_STATE_FILE",
                        str(tmp_path / "position_state.json"))
    bot  = BotState()
    risk = RiskManager()
    monkeypatch.setattr(main, "bot_state", bot)
    monkeypatch.setattr(main, "risk_manager", risk)
    return SimpleNamespace(bot=bot, risk=risk, tmp=tmp_path, mp=monkeypatch)


def _csv_rows(tmp_path):
    rows = []
    for path in glob.glob(os.path.join(str(tmp_path), "trades_*.csv")):
        with open(path, newline="") as f:
            rows.extend(csv.DictReader(f))
    return rows


class TestExecuteExit:
    def test_confirmed_fill_books_once(self, env):
        env.bot.open_position(_pos())
        env.mp.setattr(main, "order_manager", FakeOrderManager([_order(0.75, 2)]))

        ok = asyncio.run(main._execute_exit("tp"))

        assert ok is True
        assert env.bot.position is None
        assert env.bot.exit_pending is False
        rows = _csv_rows(env.tmp)
        assert len(rows) == 1
        assert rows[0]["exit_order_id"] == "exit-1"
        assert env.risk.trades_today == 1

    def test_failed_close_keeps_position_and_books_nothing(self, env):
        env.bot.open_position(_pos())
        env.mp.setattr(main, "_CLOSE_MAX_ATTEMPTS", 1)
        # Broker still shows the position (close truly went nowhere)
        env.mp.setattr(main, "order_manager",
                       FakeOrderManager([None], broker_positions=[_broker_pos()]))

        ok = asyncio.run(main._execute_exit("stop"))

        assert ok is False
        assert env.bot.position is not None          # still tracked — the broker
        assert env.bot.position.qty_remaining == 2   # still owns it
        assert env.bot.exit_pending is False         # retries can run
        assert _csv_rows(env.tmp) == []              # NO fabricated row
        assert env.risk.locked is True               # loud failure
        assert env.risk.trades_today == 0

    def test_lost_close_fill_is_reconciled_from_order_history(self, env):
        # close_position times out (None) but the market close actually
        # filled: broker shows flat, order history has the real fill.
        env.bot.open_position(_pos())
        env.mp.setattr(main, "order_manager", FakeOrderManager(
            [None],
            broker_positions=[],                       # broker already flat
            lost_fill=_order(0.71, 2, oid="lost-1"),   # the real fill
        ))

        ok = asyncio.run(main._execute_exit("stop"))

        assert ok is True
        assert env.bot.position is None
        rows = _csv_rows(env.tmp)
        assert len(rows) == 1
        assert rows[0]["exit_order_id"] == "lost-1"
        assert float(rows[0]["exit_price"]) == 0.71   # REAL price, not a guess
        assert env.risk.trades_today == 1
        assert env.risk.locked is False

    def test_partial_fill_books_both_legs_one_trade(self, env):
        env.bot.open_position(_pos())
        env.mp.setattr(main, "order_manager", FakeOrderManager([
            _order(0.75, 1, oid="exit-a"),   # partial: 1 of 2
            _order(0.73, 1, oid="exit-b"),   # remainder
        ]))

        ok = asyncio.run(main._execute_exit("tp"))

        assert ok is True
        assert env.bot.position is None
        rows = _csv_rows(env.tmp)
        assert len(rows) == 2
        assert {r["exit_order_id"] for r in rows} == {"exit-a", "exit-b"}
        assert env.risk.trades_today == 1            # one trade, not two

    def test_partial_then_fail_then_retry_records_one_trade(self, env):
        # Leg 1 books, remaining attempts fail → NO record yet; a later
        # _execute_exit closes the remainder → ONE record_trade with the
        # position's total. This is the double-count regression test.
        env.bot.open_position(_pos())
        env.mp.setattr(main, "_CLOSE_MAX_ATTEMPTS", 2)
        env.mp.setattr(main, "order_manager", FakeOrderManager(
            [_order(0.75, 1, oid="exit-a"), None],
            broker_positions=[_broker_pos()],          # remainder still on broker
        ))

        ok1 = asyncio.run(main._execute_exit("tp"))
        assert ok1 is False
        assert env.risk.trades_today == 0              # not recorded yet
        assert env.bot.position.qty_remaining == 1

        # Exit monitor retriggers later; remainder fills
        env.mp.setattr(main, "order_manager",
                       FakeOrderManager([_order(0.70, 1, oid="exit-b")]))
        ok2 = asyncio.run(main._execute_exit("tp"))
        assert ok2 is True
        assert env.bot.position is None
        assert env.risk.trades_today == 1              # ONE trade total
        leg1 = (0.75 - 0.50) * 100 - config.FEES_PER_CONTRACT_RT
        leg2 = (0.70 - 0.50) * 100 - config.FEES_PER_CONTRACT_RT
        assert env.risk.daily_pnl == pytest.approx(leg1 + leg2)

    def test_reentrancy_guard(self, env):
        env.bot.open_position(_pos())
        env.bot.exit_pending = True
        env.mp.setattr(main, "order_manager", FakeOrderManager([_order(0.75, 2)]))
        ok = asyncio.run(main._execute_exit("tp"))
        assert ok is False
        assert env.bot.position is not None


class TestCatastrophicBreach:
    """The independent backstop rule — dumbest possible check, no other
    logic to get wrong."""

    def _quote(self, bid, ask=1.0):
        from signals import Quote
        return Quote(symbol="X", bid=bid, ask=ask,
                     timestamp=datetime.datetime.now(tz=config.ET))

    def test_fires_at_threshold(self):
        pos = _pos()                                    # entry 0.50 → line 0.10
        assert main._catastrophic_breach(pos, self._quote(bid=0.10)) is True
        assert main._catastrophic_breach(pos, self._quote(bid=0.05)) is True

    def test_holds_above_threshold(self):
        pos = _pos()
        assert main._catastrophic_breach(pos, self._quote(bid=0.11)) is False

    def test_no_bid_no_position_no_quote_are_safe(self):
        pos = _pos()
        assert main._catastrophic_breach(pos, self._quote(bid=0.0)) is False
        assert main._catastrophic_breach(None, self._quote(bid=0.01)) is False
        assert main._catastrophic_breach(pos, None) is False


class TestEvaluateExitWideSpread:
    """The no-bid/wide-spread branch must never blind the stop or an
    executable TP."""

    @pytest.fixture
    def exits(self, env):
        calls = []

        async def fake_execute(reason):
            calls.append(reason)
            return True
        env.mp.setattr(main, "_execute_exit", fake_execute)
        return calls

    def _quote(self, bid, ask):
        from signals import Quote
        return Quote(symbol="SPY260706C00625000", bid=bid, ask=ask,
                     timestamp=datetime.datetime.now(tz=config.ET),
                     recv_monotonic=1.0)

    def test_no_bid_collapse_still_stops(self, env, exits):
        # entry 0.50 → stop 0.25. Bid pulled, ask 0.05: mid falls back to
        # ask; even the ask is below the stop → must exit.
        env.bot.open_position(_pos())
        asyncio.run(main._evaluate_exit(self._quote(bid=0.0, ask=0.05)))
        assert exits == ["stop"]

    def test_wide_spread_bid_below_stop_fires(self, env, exits):
        env.bot.open_position(_pos())
        asyncio.run(main._evaluate_exit(self._quote(bid=0.20, ask=0.40)))
        assert exits == ["stop"]

    def test_wide_spread_executable_tp_fires(self, env, exits):
        # entry 0.50 → TP 0.75. bid=0.80 alone clears TP despite huge ask.
        env.bot.open_position(_pos())
        asyncio.run(main._evaluate_exit(self._quote(bid=0.80, ask=1.30)))
        assert exits == ["tp"]

    def test_wide_spread_mid_zone_skips_tick(self, env, exits):
        # Wide spread, executable price between stop and TP → no exit,
        # and peak_mid must not be polluted by the garbage mid.
        env.bot.open_position(_pos())
        peak_before = env.bot.position.peak_mid
        asyncio.run(main._evaluate_exit(self._quote(bid=0.50, ask=0.90)))
        assert exits == []
        assert env.bot.position.peak_mid == peak_before
