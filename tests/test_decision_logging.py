"""Decision-logging stack: the gate report must never short-circuit, the
logger must be deterministic and truncation-tolerant, and replay must
regenerate identical decision history from a recording."""

import datetime
import glob
import gzip
import os

import pytest

import config
import decision_logger as dl
import signals
from momentum import MomentumState
from signals import GATE_NAMES, ProxyDeltaTracker, Quote, evaluate_entry_gates


def _quote(bid=0.48, ask=0.52):
    return Quote(symbol="SPY260706C00627000", bid=bid, ask=ask,
                 timestamp=datetime.datetime.now(tz=config.ET))


def _kwargs(**over):
    kw = dict(side="call", strike=627.0, option_quote=_quote(),
              momentum=MomentumState(direction="bull"),
              proxy_tracker=ProxyDeltaTracker(),
              spy_price=627.0 * (1 - 0.002), trades_today=0,
              has_open_pos=False, atr5=0.30, quote_age_s=0.5)
    kw.update(over)
    return kw


class TestGateReport:
    @pytest.fixture(autouse=True)
    def _open_window(self, monkeypatch):
        monkeypatch.setattr(signals, "_in_entry_window", lambda: True)

    def test_all_gates_always_evaluated_no_short_circuit(self):
        # capacity fails (first gate) — the zone verdict must STILL be
        # computed correctly, or gate statistics become order-dependent lies
        r = evaluate_entry_gates(**_kwargs(has_open_pos=True))
        assert r.gates["capacity"] is False
        assert r.gates["zone"] is True            # still evaluated
        assert r.gates["momentum"] is True        # still evaluated
        assert len(r.gates) == len(GATE_NAMES)

    def test_sole_blocker_exactly_one(self):
        r = evaluate_entry_gates(**_kwargs(atr5=0.05))       # only atr fails
        assert r.sole_blocker == "atr"
        r2 = evaluate_entry_gates(**_kwargs(atr5=0.05, has_open_pos=True))
        assert r2.sole_blocker == ""              # two failures → no sole blocker
        r3 = evaluate_entry_gates(**_kwargs())
        assert r3.sole_blocker == "" and r3.all_pass

    def test_execution_gates_in_report_but_not_strategy_pass(self):
        # wide spread: strategy_pass (historical check_entry semantics)
        # unaffected; all_pass (the live entry path) blocked
        r = evaluate_entry_gates(**_kwargs(option_quote=_quote(0.40, 0.60)))
        assert r.gates["spread"] is False
        assert r.strategy_pass is True
        assert r.all_pass is False

    def test_freshness_gate(self):
        stale = evaluate_entry_gates(**_kwargs(
            quote_age_s=config.ENTRY_QUOTE_MAX_AGE_SEC + 1))
        assert stale.gates["fresh"] is False
        unknown = evaluate_entry_gates(**_kwargs(quote_age_s=None))
        assert unknown.gates["fresh"] is True     # unknown age passes (parity)

    def test_check_entry_wrapper_equivalence(self):
        assert signals.check_entry(**{k: v for k, v in _kwargs().items()
                                      if k != "quote_age_s"}) is True
        assert signals.check_entry(**{k: v for k, v in
                                      _kwargs(atr5=0.0).items()
                                      if k != "quote_age_s"}) is False


class TestDecisionLoggerFiles:
    @pytest.fixture(autouse=True)
    def _open_window(self, monkeypatch):
        # the "window" gate reads the real wall clock; without pinning it,
        # all_pass is only true when the suite happens to run inside
        # 09:45-14:30 ET. Force it open so these file-format assertions are
        # deterministic at any hour.
        monkeypatch.setattr(signals, "_in_entry_window", lambda: True)

    def _row(self, sym="SPY260706C00627000", **over):
        r = evaluate_entry_gates(**_kwargs())
        d = {"symbol": sym, "side": "call", "strike": 627.0, "spy": 625.7,
             "zone_dist_pct": 0.002, "bid": 0.48, "ask": 0.52, "mid": 0.50,
             "spread_pct": 0.08, "quote_age_s": 0.5, "direction": "bull",
             "ema5": 625.1, "ema20": 624.8, "vwap": 624.9, "roc5": 0.001,
             "atr5": 0.30, "consec": 4, "gates": r.gates,
             "strategy_pass": r.strategy_pass, "all_pass": r.all_pass,
             "sole_blocker": r.sole_blocker, "in_position": False,
             "entry_pending": False, "risk_ok": True, "blackout": None,
             "event_day": False}
        d.update(over)
        return d

    def test_roundtrip(self, tmp_path):
        lg = dl.DecisionLogger(str(tmp_path), "2026-07-06")
        lg.log_candidates("09:47:00", [self._row()])
        lg.log_attempt(time_et="09:47:05", symbol="X", side="call",
                       qty_requested=6, decision_bid=0.48, decision_ask=0.52,
                       decision_mid=0.50, limit_px=0.51, outcome="filled",
                       fill_px=0.51, filled_qty=6, wait_s=2.0, order_id="o1")
        lg.close()

        rows = list(dl.read_decisions(str(tmp_path / "decisions_2026-07-06.csv.gz")))
        assert len(rows) == 1
        assert rows[0]["g_zone"] == "1" and rows[0]["all_pass"] == "1"
        assert rows[0]["bar_time_et"] == "09:47:00"
        atts = list(dl.read_attempts(str(tmp_path / "attempts_2026-07-06.csv")))
        assert atts[0]["outcome"] == "filled" and atts[0]["order_id"] == "o1"

    def test_byte_deterministic(self, tmp_path):
        for sub in ("a", "b"):
            os.makedirs(tmp_path / sub)
            lg = dl.DecisionLogger(str(tmp_path / sub), "2026-07-06")
            lg.log_candidates("09:47:00", [self._row(), self._row(atr5=0.25)])
            lg.close()
        ba = (tmp_path / "a" / "decisions_2026-07-06.csv.gz").read_bytes()
        bb = (tmp_path / "b" / "decisions_2026-07-06.csv.gz").read_bytes()
        assert ba == bb            # mtime=0 → identical inputs, identical bytes

    def test_truncated_tail_tolerated(self, tmp_path):
        lg = dl.DecisionLogger(str(tmp_path), "2026-07-06")
        for i in range(5):
            lg.log_candidates(f"09:{31 + i}:00", [self._row()])
        lg.close()
        p = tmp_path / "decisions_2026-07-06.csv.gz"
        blob = p.read_bytes()
        p.write_bytes(blob[:-9])   # simulate os._exit mid-write
        rows = list(dl.read_decisions(str(p)))
        assert len(rows) >= 3      # intact prefix survives

    def test_hard_exit_then_restart_append_fully_readable(self, tmp_path):
        # THE adversarial-review finding: the bot never calls close() on its
        # deliberate hard exits (os._exit at time stop, watchdog execl). A
        # long-lived gzip stream left unterminated + restart-append made
        # everything after the restart unreadable (uncaught zlib.error).
        # Complete-member-per-batch writing makes this impossible: NO close,
        # then append, then read EVERYTHING.
        lg1 = dl.DecisionLogger(str(tmp_path), "2026-07-06")
        lg1.log_candidates("09:31:00", [self._row()])
        lg1.log_candidates("09:32:00", [self._row()])
        del lg1                                   # hard exit: no close()

        lg2 = dl.DecisionLogger(str(tmp_path), "2026-07-06")   # watchdog restart
        lg2.log_candidates("11:00:00", [self._row()])
        del lg2                                   # dies hard again

        rows = list(dl.read_decisions(str(tmp_path / "decisions_2026-07-06.csv.gz")))
        assert [r["bar_time_et"] for r in rows] == ["09:31:00", "09:32:00", "11:00:00"]
        # exactly one header, present even though NOTHING was ever closed
        assert all(r["date"] == "2026-07-06" for r in rows)

    def test_header_survives_restart_before_first_batch(self, tmp_path):
        # Second finding: file created but header unflushed → restart saw a
        # 0-byte file and never wrote a header. Header is now its own member,
        # written synchronously at creation.
        lg1 = dl.DecisionLogger(str(tmp_path), "2026-07-06")
        del lg1                                   # dies before ANY batch
        lg2 = dl.DecisionLogger(str(tmp_path), "2026-07-06")
        lg2.log_candidates("09:31:00", [self._row()])
        del lg2
        rows = list(dl.read_decisions(str(tmp_path / "decisions_2026-07-06.csv.gz")))
        assert len(rows) == 1 and rows[0]["bar_time_et"] == "09:31:00"

    def test_failed_write_drops_batch_never_duplicates(self, tmp_path):
        # Third finding: a failed flush used to retain the batch and re-emit
        # it next bar — duplicated rows silently skew every drift rate.
        # Policy: drop and COUNT. A lost bar is honest; a doubled one lies.
        lg = dl.DecisionLogger(str(tmp_path), "2026-07-06")
        lg._raw.close()                            # force writes to fail
        lg.log_candidates("09:31:00", [self._row()])
        assert lg.dropped_batches == 1
        lg._raw = open(lg.decisions_path, "ab")    # disk "recovers"
        lg.log_candidates("09:32:00", [self._row()])
        lg.close()
        rows = list(dl.read_decisions(str(tmp_path / "decisions_2026-07-06.csv.gz")))
        assert [r["bar_time_et"] for r in rows] == ["09:32:00"]   # no resurrection


class TestReplayRegeneratesDecisions:
    def test_replay_emits_decisions_attempts_and_excursions(self, tmp_path):
        # The synthetic session from the replay tests: bars build momentum,
        # entry fills at 0.51, TP exits at 0.76. Replay must emit the full
        # funnel record into its out dir.
        from tests.test_replay import _write_recording, _synthetic_events, _wall
        from replay import run_session
        rec = str(tmp_path / "session_2026-07-06.jsonl.gz")
        # The shared fixture's quotes all arrive after its last bar; append
        # one more bar AFTER the quotes so the per-bar decision pass sees
        # cached quotes to evaluate — as every real interleaved session does.
        events = _synthetic_events()
        bar_ts = datetime.datetime(2026, 7, 6, 9, 48, tzinfo=config.ET)
        events.append(["b", _wall(9, 49), bar_ts.isoformat(),
                       603.2, 603.5, 602.9, 603.4, 10_000.0])
        _write_recording(rec, events)

        out = str(tmp_path / "out")
        summary = run_session(rec, out)
        assert summary["trades"] == 1

        dec = list(dl.read_decisions(
            os.path.join(out, "decisions_2026-07-06.csv.gz")))
        assert len(dec) > 0
        # candidate rows carry full gate verdicts
        assert all(f"g_{g}" in dec[0] for g in GATE_NAMES)
        atts = list(dl.read_attempts(os.path.join(out, "attempts_2026-07-06.csv")))
        assert len(atts) == 1 and atts[0]["outcome"] == "filled"
        assert float(atts[0]["fill_px"]) == 0.51
        # trades CSV carries the excursion columns (L4 diagnosis fork)
        import csv as _csv
        with open(os.path.join(out, "trades_2026-07-06.csv"), newline="") as f:
            trow = list(_csv.DictReader(f))[0]
        assert "mfe_pnl" in trow and "mae_pnl" in trow
        assert float(trow["peak_mid"]) > 0

    def test_decision_logs_deterministic_across_replays(self, tmp_path):
        from tests.test_replay import _write_recording, _synthetic_events
        from replay import run_session
        rec = str(tmp_path / "session_2026-07-06.jsonl.gz")
        _write_recording(rec, _synthetic_events())
        run_session(rec, str(tmp_path / "r1"))
        run_session(rec, str(tmp_path / "r2"))
        b1 = (tmp_path / "r1" / "decisions_2026-07-06.csv.gz").read_bytes()
        b2 = (tmp_path / "r2" / "decisions_2026-07-06.csv.gz").read_bytes()
        assert b1 == b2
