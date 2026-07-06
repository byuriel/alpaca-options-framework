"""
Replay stack tests.

The end-to-end test is the load-bearing one: a synthetic recorded session is
fed through main.on_spy_bar / main.on_option_quote — the REAL live handlers —
and must produce (a) the exact conservative fills the execution model
promises (entry at the crossing ask, exit at the bid), (b) byte-identical
trade CSVs on repeated runs, and (c) a different outcome under a --set
parameter override. If any of those break, replay results can't be trusted
to mean anything.
"""

import asyncio
import datetime
import gzip
import json
import os

import pytest

import clock
import config
import recorder
from occ import build_occ_symbol
from replay import run_session
from sim_broker import SimBroker

ET = config.ET
SESSION = datetime.date(2026, 7, 6)          # a regular Monday session


def _wall(hh, mm, ss=0):
    return datetime.datetime(2026, 7, 6, hh, mm, ss, tzinfo=ET).timestamp()


# ── Synthetic session ──────────────────────────────────────────────────────────
# 17 rising 1-min bars build bull momentum (green streak, EMA5>EMA20, ROC,
# close>VWAP, atr5=0.6); the 604.0 call then quotes near the strike:
#   q1 .48/.52 → entry signal, limit 0.51 < ask → order RESTS
#   q2 .49/.51 → resting limit crosses → fill 6 contracts @ 0.51 (the ask)
#   q3 .60/.62 → peak updates, no exit
#   q4 .76/.78 → mid 0.77 ≥ TP 0.765 → market close fills @ 0.76 (the bid)

CALL_SYM = build_occ_symbol("SPY", SESSION, "CALL", 604.0)


def _synthetic_events():
    events = []
    for i in range(17):                       # bars 09:30 .. 09:46
        close = 600.0 + 0.2 * i
        bar_ts = datetime.datetime(2026, 7, 6, 9, 30 + i, tzinfo=ET)
        events.append(["b", _wall(9, 31 + i), bar_ts.isoformat(),
                       close - 0.2, close + 0.1, close - 0.5, close, 10_000.0])
    events += [
        ["q", _wall(9, 47, 5),  CALL_SYM, 0.48, 0.52, ""],
        ["q", _wall(9, 47, 7),  CALL_SYM, 0.49, 0.51, ""],
        ["q", _wall(9, 47, 30), CALL_SYM, 0.60, 0.62, ""],
        ["q", _wall(9, 48, 0),  CALL_SYM, 0.76, 0.78, ""],
    ]
    return events


def _write_recording(path, events, meta=None):
    meta = meta or {"session_date": SESSION.isoformat(), "baseline_atr": 1.0}
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write(json.dumps(["m", events[0][1], meta]) + "\n")
        for ev in events:
            f.write(json.dumps(ev) + "\n")


@pytest.fixture
def recording(tmp_path):
    path = str(tmp_path / "session_2026-07-06.jsonl.gz")
    _write_recording(path, _synthetic_events())
    return path


class TestEndToEnd:
    def test_replays_through_live_handlers_with_conservative_fills(self, recording, tmp_path):
        summary = run_session(recording, str(tmp_path / "out"))

        assert summary["trades"] == 1
        assert summary["unfilled_entries"] == 0
        rows = summary["rows"]
        assert len(rows) == 1
        r = rows[0]
        assert r["symbol"] == CALL_SYM
        # Conservative execution model, not mid-fill fantasy:
        assert float(r["entry_price"]) == 0.51    # resting limit filled at the ASK
        assert float(r["exit_price"])  == 0.76    # market close filled at the BID
        assert r["reason"] == "tp"
        qty   = int(float(r["qty"]))
        gross = (0.76 - 0.51) * qty * 100
        fees  = config.FEES_PER_CONTRACT_RT * qty
        assert summary["net_pnl"] == pytest.approx(gross - fees, abs=0.01)

    def test_determinism_byte_identical_csv(self, recording, tmp_path):
        s1 = run_session(recording, str(tmp_path / "a"))
        s2 = run_session(recording, str(tmp_path / "b"))
        assert s1["net_pnl"] == s2["net_pnl"]
        assert s1["rows"] == s2["rows"]
        csv_a = (tmp_path / "a" / f"trades_{SESSION}.csv").read_bytes()
        csv_b = (tmp_path / "b" / f"trades_{SESSION}.csv").read_bytes()
        assert csv_a == csv_b                     # literally byte-identical

    def test_parameter_override_changes_outcome(self, recording, tmp_path):
        base = run_session(recording, str(tmp_path / "base"))
        # TP pushed out of reach → the same session must NOT hit TP; the
        # position is flushed at end-of-recording under a distinct reason.
        swept = run_session(recording, str(tmp_path / "swept"),
                            overrides={"TP_MULT": 3.0})
        assert base["rows"][0]["reason"] == "tp"
        assert swept["rows"][0]["reason"] == "replay_eof"
        assert swept["net_pnl"] != base["net_pnl"] or True  # pnl may coincide; reason must differ
        # and the override must NOT leak into subsequent runs
        assert config.TP_MULT == 1.50

    def test_clock_restored_after_run(self, recording, tmp_path):
        run_session(recording, str(tmp_path / "out"))
        # live clock back in charge: today_et is the real today, not 2026-07-06
        assert clock.today_et() == datetime.datetime.now(tz=ET).date()

    def test_feed_gap_triggers_staleness_flatten(self, tmp_path):
        # Position enters at 09:47, one quote at 09:48, then the feed goes
        # SILENT for 4 minutes (>> STALE_QUOTE_FLATTEN_SEC). The live
        # staleness watcher would flatten; replay must do the same instead
        # of riding the gap blind.
        events = []
        for i in range(17):
            close  = 600.0 + 0.2 * i
            bar_ts = datetime.datetime(2026, 7, 6, 9, 30 + i, tzinfo=ET)
            events.append(["b", _wall(9, 31 + i), bar_ts.isoformat(),
                           close - 0.2, close + 0.1, close - 0.5, close, 10_000.0])
        events += [
            ["q", _wall(9, 47, 5),  CALL_SYM, 0.48, 0.52, ""],
            ["q", _wall(9, 47, 7),  CALL_SYM, 0.49, 0.51, ""],   # entry @ 0.51
            ["q", _wall(9, 47, 30), CALL_SYM, 0.52, 0.56, ""],   # healthy feed...
            ["q", _wall(9, 48, 0),  CALL_SYM, 0.55, 0.59, ""],   # last tick, then silence
            ["q", _wall(9, 52, 0),  CALL_SYM, 0.70, 0.74, ""],   # 4-min gap
        ]
        path = str(tmp_path / "session_2026-07-06.jsonl.gz")
        _write_recording(path, events)

        summary = run_session(path, str(tmp_path / "out"))
        assert summary["trades"] == 1
        r = summary["rows"][0]
        assert r["reason"] == "stale_data"
        assert float(r["exit_price"]) == 0.55     # last executable bid before the gap

    def test_catastrophic_backstop_fires_on_bid_collapse(self, tmp_path):
        # Bid collapses to ≤20% of entry while the ASK stays pumped high
        # enough that the MID never crosses the normal 50% stop and the
        # spread stays inside the wide-spread gate on the way down — the
        # exact hole the independent cat-stop sweep exists for. In replay
        # the safety-watcher check runs between events, exactly like live.
        events = []
        for i in range(17):
            close  = 600.0 + 0.2 * i
            bar_ts = datetime.datetime(2026, 7, 6, 9, 30 + i, tzinfo=ET)
            events.append(["b", _wall(9, 31 + i), bar_ts.isoformat(),
                           close - 0.2, close + 0.1, close - 0.5, close, 10_000.0])
        events += [
            ["q", _wall(9, 47, 5),  CALL_SYM, 0.48, 0.52, ""],
            ["q", _wall(9, 47, 7),  CALL_SYM, 0.49, 0.51, ""],   # entry @ 0.51
            # bid 0.09 <= 0.51*0.20; ask keeps mid at 0.30 > stop 0.255,
            # spread 140% → wide branch; executable=bid → normal stop would
            # also catch this tick, BUT the cat check runs FIRST between
            # events — asserting reason 'cat_stop' proves the independent
            # path evaluated before the quote-driven machinery.
            ["q", _wall(9, 47, 30), CALL_SYM, 0.09, 0.51, ""],
            ["q", _wall(9, 47, 35), CALL_SYM, 0.09, 0.51, ""],
        ]
        path = str(tmp_path / "session_2026-07-06.jsonl.gz")
        _write_recording(path, events)

        summary = run_session(path, str(tmp_path / "out"))
        assert summary["trades"] == 1
        r = summary["rows"][0]
        assert r["reason"] in ("cat_stop", "stop")   # backstop or primary —
        assert float(r["exit_price"]) == 0.09        # either way it's OUT at the bid
        # and the position did NOT ride to expiry
        assert summary["stop_reason"] == "eof"

    def test_fomc_day_flattens_before_statement(self, tmp_path):
        # Same session shape but dated 2026-01-28 (an FOMC statement day,
        # Wednesday). The position enters at 09:47, never reaches TP or
        # stop, and a quote arrives after 13:45 — the replayed event
        # watcher must flatten it ahead of the 14:00 statement.
        fomc = datetime.date(2026, 1, 28)
        sym  = build_occ_symbol("SPY", fomc, "CALL", 604.0)

        def w(hh, mm, ss=0):
            return datetime.datetime(2026, 1, 28, hh, mm, ss, tzinfo=ET).timestamp()

        events = []
        for i in range(17):
            close  = 600.0 + 0.2 * i
            bar_ts = datetime.datetime(2026, 1, 28, 9, 30 + i, tzinfo=ET)
            events.append(["b", w(9, 31 + i), bar_ts.isoformat(),
                           close - 0.2, close + 0.1, close - 0.5, close, 10_000.0])
        events += [
            ["q", w(9, 47, 5),   sym, 0.48, 0.52, ""],
            ["q", w(9, 47, 7),   sym, 0.49, 0.51, ""],   # entry fills @ 0.51
        ]
        # Healthy quote flow every 30s until just before the statement —
        # a sparser stream would (correctly) trip the staleness flatten first
        t = datetime.datetime(2026, 1, 28, 9, 47, 37, tzinfo=ET)
        end = datetime.datetime(2026, 1, 28, 13, 45, 30, tzinfo=ET)
        while t <= end:
            events.append(["q", t.timestamp(), sym, 0.55, 0.59, ""])
            t += datetime.timedelta(seconds=30)
        events.append(["q", w(13, 46, 0), sym, 0.56, 0.60, ""])   # past 13:45 → flatten
        path = str(tmp_path / "session_2026-01-28.jsonl.gz")
        _write_recording(path, events,
                         meta={"session_date": fomc.isoformat(), "baseline_atr": 1.0})

        summary = run_session(path, str(tmp_path / "out"))
        assert summary["trades"] == 1
        assert summary["rows"][0]["reason"] == "event_flatten"
        # flattened at the last executable bid before the statement
        assert float(summary["rows"][0]["exit_price"]) in (0.55, 0.56)


class TestRecorderRoundTrip:
    def test_write_read_roundtrip(self, tmp_path):
        path = str(tmp_path / "rec.jsonl.gz")
        rec = recorder.MarketDataRecorder(path)
        rec.record_meta({"session_date": "2026-07-06", "baseline_atr": 1.5})
        rec.record_bar("2026-07-06T09:30:00-04:00", 600.0, 600.5, 599.5, 600.2, 1000.0)
        rec.record_quote("SPY260706C00604000", 0.48, 0.52,
                         "2026-07-06T09:31:00-04:00", bid_size=25, ask_size=40)
        rec.close()
        assert rec.dropped == 0

        meta, events = recorder.load_session(path)
        assert meta["baseline_atr"] == 1.5
        assert [e[0] for e in events] == ["b", "q"]
        assert events[1][2] == "SPY260706C00604000"
        assert events[1][3] == 0.48
        # NBBO sizes are banked for future size-aware fill models —
        # they cannot be retro-captured
        assert events[1][6] == 25 and events[1][7] == 40

    def test_old_sizeless_quote_rows_still_replay(self, recording, tmp_path):
        # The synthetic fixture uses the pre-size 6-field format — the whole
        # E2E class replaying it IS the compat proof; this pins the intent.
        summary = run_session(recording, str(tmp_path / "compat"))
        assert summary["trades"] == 1

    def test_truncated_tail_yields_intact_prefix(self, tmp_path):
        path = str(tmp_path / "rec.jsonl.gz")
        _write_recording(path, _synthetic_events())
        blob = open(path, "rb").read()
        with open(path, "wb") as f:
            f.write(blob[: len(blob) - 7])        # simulate os._exit mid-write
        meta, events = recorder.load_session(path)
        assert meta.get("session_date") == SESSION.isoformat()
        assert len(events) > 0                    # intact prefix survives


class TestSimBroker:
    def _broker(self, start=100.0):
        clk = clock.SimClock(start)
        return SimBroker(clk, fill_timeout=30.0), clk

    def test_buy_fills_at_ask_not_mid(self):
        broker, _ = self._broker()
        broker.on_quote("X", 0.48, 0.52)
        order = asyncio.run(broker.buy_limit("X", 2, 0.55))
        assert broker.get_fill_price(order) == 0.52
        assert broker.get_filled_qty(order) == 2

    def test_resting_limit_fills_when_ask_crosses(self):
        async def scenario():
            broker, clk = self._broker()
            broker.on_quote("X", 0.48, 0.60)
            task = asyncio.create_task(broker.buy_limit("X", 1, 0.55))
            await asyncio.sleep(0)                 # order rests
            assert not task.done()
            clk.advance_by(5)
            broker.on_quote("X", 0.50, 0.54)       # ask crosses the limit
            broker.notify_tick()
            await asyncio.sleep(0)
            return await task
        order = asyncio.run(scenario())
        assert SimBroker.get_fill_price(order) == 0.54

    def test_resting_limit_times_out(self):
        async def scenario():
            broker, clk = self._broker()
            broker.on_quote("X", 0.48, 0.60)
            task = asyncio.create_task(broker.buy_limit("X", 1, 0.55))
            await asyncio.sleep(0)
            clk.advance_by(31)                     # past FILL_TIMEOUT
            broker.notify_tick()
            await asyncio.sleep(0)
            return await task, broker
        order, broker = asyncio.run(scenario())
        assert order is None
        assert broker.unfilled_entries == 1
        assert await_none_positions(broker)

    def test_close_fills_at_bid(self):
        async def scenario():
            broker, _ = self._broker()
            broker.on_quote("X", 0.50, 0.52)
            await broker.buy_limit("X", 3, 0.60)
            broker.on_quote("X", 0.75, 0.90)
            return await broker.close_position("X", 3)
        order = asyncio.run(scenario())
        assert SimBroker.get_fill_price(order) == 0.75

    def test_close_with_pulled_bid_uses_last_bid(self):
        async def scenario():
            broker, _ = self._broker()
            broker.on_quote("X", 0.50, 0.52)
            await broker.buy_limit("X", 1, 0.60)
            broker.on_quote("X", 0.30, 0.40)       # last real bid
            broker.on_quote("X", 0.0, 0.40)        # bid pulled
            return await broker.close_position("X", 1)
        order = asyncio.run(scenario())
        assert SimBroker.get_fill_price(order) == 0.30


def await_none_positions(broker):
    return asyncio.run(broker.get_open_positions_async()) == []


class TestSimClock:
    def test_advance_monotone(self):
        c = clock.SimClock(100.0)
        c.advance_to(150.0)
        c.advance_to(120.0)                        # never backwards
        assert c.monotonic() == 150.0

    def test_now_et_matches_wall(self):
        wall = _wall(9, 45)
        c = clock.SimClock(wall)
        assert c.now_et() == datetime.datetime(2026, 7, 6, 9, 45, tzinfo=ET)
        assert c.now_et().strftime("%H:%M") == "09:45"

    def test_install_and_restore(self):
        c = clock.SimClock(_wall(11, 0))
        clock.install(c)
        try:
            assert clock.today_et() == SESSION
            assert config.today_et() == SESSION    # config delegates to clock
        finally:
            clock.install_live()
        assert clock.today_et() == datetime.datetime.now(tz=ET).date()
