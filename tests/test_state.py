"""BotState — booking integrity and restart persistence. The CSV is the
track record; these tests pin down that it only ever contains confirmed
fills, net of fees, and that a restart recovers full entry context."""

import csv
import datetime
import os

import pytest

import config
import state as state_mod
from state import BotState, Position


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(state_mod, "POSITION_STATE_FILE",
                        str(tmp_path / "position_state.json"))
    return BotState()


def _pos(**kw):
    defaults = dict(
        symbol="SPY260706C00625000", side="call", strike=625.0,
        qty=3, entry_price=0.50,
        entry_time=datetime.datetime(2026, 7, 6, 10, 15, tzinfo=config.ET),
        order_id="order-123", entry_spy_price=622.80, entry_atr5=0.25,
        entry_bid=0.48, entry_ask=0.52,
    )
    defaults.update(kw)
    return Position(**defaults)


class TestBooking:
    def test_full_close_books_net_of_fees(self, bot):
        bot.open_position(_pos())
        pnl = bot.book_exit_fill(0.75, "tp", exit_order_id="exit-1",
                                 exit_bid=0.74, exit_ask=0.76)
        gross = (0.75 - 0.50) * 3 * 100
        fees  = config.FEES_PER_CONTRACT_RT * 3
        assert pnl == pytest.approx(gross - fees)
        assert bot.position is None
        assert bot.exit_pending is False

    def test_partial_close_keeps_position_tracked(self, bot):
        bot.open_position(_pos())
        pnl = bot.book_exit_fill(0.75, "tp", qty=2)
        assert pnl == pytest.approx((0.75 - 0.50) * 2 * 100 - config.FEES_PER_CONTRACT_RT * 2)
        assert bot.position is not None
        assert bot.position.qty_remaining == 1
        # remainder closes normally
        bot.book_exit_fill(0.70, "tp", qty=1)
        assert bot.position is None

    def test_qty_clamped_to_remaining(self, bot):
        bot.open_position(_pos())
        bot.book_exit_fill(0.75, "tp", qty=99)
        assert bot.position is None

    def test_no_position_books_nothing(self, bot):
        assert bot.book_exit_fill(0.75, "tp") == 0.0

    def test_csv_row_contains_audit_fields(self, bot, tmp_path):
        bot.open_position(_pos())
        bot.book_exit_fill(0.25, "stop", exit_order_id="exit-9",
                           exit_bid=0.24, exit_ask=0.26)
        csv_path = os.path.join(str(tmp_path), f"trades_{config.today_et().isoformat()}.csv")
        with open(csv_path, newline="") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        r = rows[0]
        assert r["entry_order_id"] == "order-123"
        assert r["exit_order_id"]  == "exit-9"
        assert float(r["fees"]) == pytest.approx(config.FEES_PER_CONTRACT_RT * 3)
        # slippage columns: fill vs decision mid
        assert float(r["entry_slippage"]) == pytest.approx(0.50 - 0.50, abs=1e-9)
        assert float(r["exit_slippage"])  == pytest.approx(0.25 - 0.25, abs=1e-9)
        assert r["reason"] == "stop"


class TestPersistence:
    def test_open_persists_and_close_clears(self, bot):
        bot.open_position(_pos())
        assert BotState.load_persisted_position() is not None
        bot.book_exit_fill(0.75, "tp")
        assert BotState.load_persisted_position() is None

    def test_persisted_metadata_roundtrip(self, bot):
        pos = _pos()
        bot.open_position(pos)
        loaded = BotState.load_persisted_position()
        assert loaded["symbol"] == pos.symbol
        assert loaded["entry_price"] == pos.entry_price
        assert loaded["entry_spy_price"] == pos.entry_spy_price
        assert loaded["entry_atr5"] == pos.entry_atr5
        restored_time = datetime.datetime.fromisoformat(loaded["entry_time"])
        assert restored_time == pos.entry_time

    def test_missing_file_returns_none(self, bot):
        assert BotState.load_persisted_position() is None

    def test_corrupt_file_returns_none(self, bot):
        with open(state_mod.POSITION_STATE_FILE, "w") as f:
            f.write("{not json")
        assert BotState.load_persisted_position() is None


class TestSecondPositionGuard:
    def test_second_open_is_rejected(self, bot):
        first = _pos()
        bot.open_position(first)
        bot.open_position(_pos(symbol="SPY260706P00620000", side="put"))
        assert bot.position is first
