"""ES futures port — contract math, Apex account geometry, price-space
exits, the shared signal engine on ES bars, conservative sim fills, and the
end-to-end backtest/golden-vector runner (determinism included)."""

import csv
import datetime
import gzip
import os

import pytest

import config
import futures_contracts as fc
from apex_risk import ApexAccount
from es_engine import ES_GATE_NAMES, EsSignalEngine
from futures_exits import (ExitParams, FuturesPosition, initial_stop_price,
                           soft_exit_reason, stop_distance, target_price)
from futures_sim import FuturesSimBroker
from momentum import Bar


# ── Contract math ─────────────────────────────────────────────────────────────

class TestContracts:
    def test_third_friday_2026(self):
        assert fc.third_friday(2026, 3)  == datetime.date(2026, 3, 20)
        assert fc.third_friday(2026, 6)  == datetime.date(2026, 6, 19)
        assert fc.third_friday(2026, 9)  == datetime.date(2026, 9, 18)
        assert fc.third_friday(2026, 12) == datetime.date(2026, 12, 18)

    def test_front_month_roll_boundary(self):
        # ESH6 expiry 2026-03-20 → roll 2026-03-12: front flips that day
        assert fc.front_month(datetime.date(2026, 3, 11)) == (2026, 3)
        assert fc.front_month(datetime.date(2026, 3, 12)) == (2026, 6)
        assert fc.front_contract_code("ES", datetime.date(2026, 7, 7)) == "ESU6"

    def test_codes_and_names(self):
        assert fc.contract_code("MES", 2026, 6) == "MESM6"
        assert fc.nt_instrument_name("ES", 2026, 3) == "ES 03-26"

    def test_tick_math(self):
        assert fc.round_to_tick(6270.13) == 6270.25
        assert fc.ticks_between(6001.25, 5999.50) == 7
        # the sizing identity from the plan: 8 ticks × $12.50 = $100
        assert fc.dollar_risk(8, fc.ES, 1) == 100.0
        assert fc.dollar_risk(8, fc.MES, 15) == 150.0

    def test_roll_schedule_contiguous(self):
        rows = fc.roll_schedule(datetime.date(2026, 1, 5),
                                datetime.date(2026, 12, 31))
        # ESZ6 rolls to ESH7 on Dec 10 (expiry Dec 18 − 8), inside the window
        assert [r["code"] for r in rows] == ["ESH6", "ESM6", "ESU6", "ESZ6",
                                             "ESH7"]
        for a, b in zip(rows, rows[1:]):
            assert b["front_from"] == a["front_to"] + datetime.timedelta(days=1)


# ── Apex account geometry ─────────────────────────────────────────────────────

class TestApex:
    def test_fresh_account(self):
        a = ApexAccount()
        assert a.s.threshold == 47_500.0
        assert a.headroom() == 2_500.0

    def test_realtime_trailing_on_unrealized_and_consumption(self):
        a = ApexAccount()
        # open trade spikes +$1,000 unrealized, retraces to scratch
        a.mark_equity(51_000.0)
        assert a.s.threshold == 48_500.0
        assert a.s.unrealized_consumption == 1_000.0   # headroom gone, unbanked
        a.mark_equity(50_000.0)                        # retrace: no un-ratchet
        assert a.s.threshold == 48_500.0
        assert a.headroom() == 1_500.0

    def test_threshold_locks_at_start_plus_100(self):
        a = ApexAccount()
        a.mark_equity(60_000.0)
        assert a.s.threshold == 50_100.0               # locked, not 57,500

    def test_breach(self):
        a = ApexAccount()
        assert a.mark_equity(47_500.0) is True
        assert a.s.breached

    def test_contract_scaling_unlocks_at_eod_52600(self):
        a = ApexAccount()
        assert a.contracts_cap(fc.MES) == 50            # half of 100 micros
        a.s.balance = 52_599.0
        a.end_of_day()
        assert a.contracts_cap(fc.MES) == 50
        a.s.balance = 52_600.0
        a.end_of_day()
        assert a.contracts_cap(fc.MES) == 100
        assert a.contracts_cap(fc.ES) == 10

    def test_sizing_from_headroom_not_nominal_balance(self):
        a = ApexAccount()
        # 8-tick stop: MES $10/contract → floor(min(125, 125)/10) = 12
        assert a.size_trade(8, fc.MES) == 12
        # ES $100/contract → 1
        assert a.size_trade(8, fc.ES) == 1
        # stop too wide for the budget → SKIP, never round up
        assert a.size_trade(120, fc.ES) == 0
        # shrunken headroom shrinks the budget: 5% × 1000 = $50 → 5 MES
        a.s.threshold = a.s.balance - 1_000.0
        assert a.size_trade(8, fc.MES) == 5

    def test_prospective_daily_gate(self):
        a = ApexAccount()
        assert a.daily_loss_limit() == 250.0            # min(300, 10%×2500)
        assert a.can_open(100.0, realized_today=-200.0, realized_week=0.0) is False
        assert a.can_open(40.0,  realized_today=-200.0, realized_week=0.0) is True

    def test_consistency_rule(self):
        assert ApexAccount.consistency_ok([300.0, 100.0]) is False   # 300 > 50%×400
        # 50% rule ⇔ best day ≤ sum of all the others
        assert ApexAccount.consistency_ok([300.0, 300.0]) is True
        # at 50%: today may not exceed everything banked before it
        assert ApexAccount.soft_daily_profit_cap(500.0) == pytest.approx(500.0)


# ── Price-space exits ─────────────────────────────────────────────────────────

def _pos(side="long", entry=6000.0, atr5=2.0, qty=10):
    return FuturesPosition(side=side, entry_price=entry, qty=qty,
                           atr5_entry=atr5,
                           entry_time=datetime.datetime(2026, 7, 6, 10, 0,
                                                        tzinfo=config.ET))


class TestExits:
    def test_stop_floor_binds_in_dead_tape(self):
        p = ExitParams()
        assert stop_distance(0.5, p) == config.ES_STOP_FLOOR_PTS   # 0.375 → 1.0

    def test_hard_levels(self):
        p = ExitParams()
        assert initial_stop_price("long", 6001.25, 2.0, p) == 5999.75
        assert target_price("long", 6001.25, 2.0, p) == 6004.25
        assert initial_stop_price("short", 6001.25, 2.0, p) == 6002.75

    def test_trail_arms_then_exits_on_giveback(self):
        p, pos = ExitParams(), _pos()
        pos.update_on_bar(6003.0, 6000.5, 6003.0)   # peak +3.0 ≥ arm 1.0×2.0
        t = datetime.time(10, 30)
        assert soft_exit_reason(pos, 6002.0, t, p) is None      # retrace 1.0 < 1.2
        assert soft_exit_reason(pos, 6001.7, t, p) == "trail"   # retrace 1.3 ≥ 1.2

    def test_stagnation_is_the_theta_replacement(self):
        p, pos = ExitParams(), _pos()
        for _ in range(config.ES_STAGNATION_BARS):
            pos.update_on_bar(6000.3, 5999.8, 6000.1)   # peak 0.3 < 0.25×2.0
        assert soft_exit_reason(pos, 6000.1, datetime.time(11, 0), p) == "stagnation"

    def test_winner_never_stagnates(self):
        p, pos = ExitParams(), _pos()
        pos.update_on_bar(6001.0, 6000.0, 6000.9)       # peak 1.0 ≥ 0.5
        for _ in range(config.ES_STAGNATION_BARS):
            pos.update_on_bar(6000.9, 6000.7, 6000.8)
        assert soft_exit_reason(pos, 6000.8, datetime.time(11, 0), p) is None

    def test_time_stop_outranks_everything(self):
        p, pos = ExitParams(), _pos()
        pos.update_on_bar(6003.0, 6000.5, 6001.0)
        assert soft_exit_reason(pos, 6001.0, datetime.time(15, 25), p) == "time_stop"

    def test_mfe_mae_bookkeeping(self):
        pos = _pos()
        pos.update_on_bar(6002.0, 5998.5, 6001.0)
        assert pos.peak_favorable == 2.0
        assert pos.mae_points == 1.5


# ── ES signal engine ──────────────────────────────────────────────────────────

def _bar(hh, mm, close, *, rng=2.5, day=6):
    t = datetime.datetime(2026, 7, day, hh, mm, tzinfo=config.ET)
    o = close - 1.0   # green bar
    return Bar(t=t, open=o, high=max(o, close) + rng / 2,
               low=min(o, close) - rng / 2, close=close, volume=10_000.0)


def _ramp_engine(engine=None):
    """Feed a bull ramp: rising closes, green bars, fat ranges."""
    e = engine or EsSignalEngine(zone_variant="off")
    d = None
    px = 6000.0
    for i in range(20):                       # 09:31 .. 09:50
        px += 1.2
        d = e.on_bar(_bar(9, 31 + i, px), has_open_pos=False)
    return e, d


class TestEsEngine:
    def test_bull_ramp_fires_long(self):
        _, d = _ramp_engine()
        assert d.momentum.direction == "bull"
        assert d.all_pass and d.entry_side == "long"

    def test_atr_gate_is_fraction_of_price(self):
        e = EsSignalEngine(zone_variant="off")
        px = 6000.0
        d = None
        for i in range(20):
            px += 1.2
            d = e.on_bar(_bar(9, 31 + i, px, rng=0.5), has_open_pos=False)
        # true range 1.5 < 0.00032 × 6000 ≈ 1.92 → velocity gate blocks, and
        # it is the SOLE blocker — everything else is green
        assert d.gates["atr"] is False
        assert d.sole_blocker == "atr"

    def test_window_gate(self):
        e = EsSignalEngine(zone_variant="off")
        px = 6000.0
        d = None
        for i in range(10):                   # all before 09:45
            px += 1.2
            d = e.on_bar(_bar(9, 31 + i, px), has_open_pos=False)
        assert d.gates["window"] is False

    def test_cooldown_after_exit(self):
        e, _ = _ramp_engine()
        e.note_exit()
        d = e.on_bar(_bar(9, 51, 6025.0), has_open_pos=False)
        assert d.gates["cooldown"] is False

    def test_grid_zone_fails_closed_without_daily_atr(self):
        e = EsSignalEngine(zone_variant="grid")
        _, d = _ramp_engine(e)
        assert d.gates["zone"] is False       # point-in-time warmup: no entry

    def test_grid_zone_near_vacuous_at_es_scale(self):
        # The plan §2.3 finding, as an executable assertion: with a 5-pt grid
        # and a ±30-pt window, some OTM level is ALWAYS within 0.3% of spot —
        # even with the offset capped at 45. The zone machinery selected
        # WHICH strike, not WHETHER to trade.
        e = EsSignalEngine(zone_variant="grid")
        e.set_daily_atr(90.0)                 # offset capped at 45
        _, d = _ramp_engine(e)
        assert d.gates["zone"] is True
        assert d.zone_dist_pct < config.ACTIVATION_PCT


# ── Sim fills ─────────────────────────────────────────────────────────────────

class TestSimFills:
    def test_entry_and_soft_close_slip_are_adverse(self):
        s = FuturesSimBroker(spec_root="MES")
        assert s.entry_fill("long", 6000.0) == 6000.25
        assert s.close_fill("long", 6000.0) == 5999.75
        assert s.entry_fill("short", 6000.0) == 5999.75

    def test_stop_fills_through_target_needs_trade_through(self):
        s = FuturesSimBroker(spec_root="MES")
        pos = _pos()
        pos.stop_price, pos.target_price = 5998.0, 6004.0
        # touch the target exactly → NO fill
        assert s.check_hard_exits(pos, high=6004.0, low=6001.0) is None
        # trade through → fill at the limit
        px, why = s.check_hard_exits(pos, high=6004.25, low=6001.0)
        assert (px, why) == (6004.0, "target")
        # stop: fills THROUGH by a tick
        px, why = s.check_hard_exits(pos, high=6001.0, low=5998.0)
        assert (px, why) == (5997.75, "stop")

    def test_both_in_one_bar_stop_first(self):
        s = FuturesSimBroker(spec_root="MES")
        pos = _pos()
        pos.stop_price, pos.target_price = 5998.0, 6004.0
        px, why = s.check_hard_exits(pos, high=6010.0, low=5990.0)
        assert why == "stop"

    def test_pnl_nets_commissions(self):
        s = FuturesSimBroker(spec_root="MES")
        pos = _pos(qty=10)                    # +4 pts × $5 × 10 = $200 gross
        assert s.pnl_usd(pos, 6004.0) == pytest.approx(
            200.0 - 2 * config.ES_COMMISSION_PER_SIDE["MES"] * 10)


# ── End-to-end runner + golden vectors ────────────────────────────────────────

def _write_session_csv(path, day=6):
    """Synthetic ES session: warmup, bull ramp through 09:45, run to target,
    then drift into the close. Prices ~6000, ranges fat enough for the
    velocity gate."""
    rows = []
    px = 6000.0
    t0 = datetime.datetime(2026, 7, day, 9, 30, tzinfo=config.ET)
    for i in range(120):                      # 09:30–11:29
        t = t0 + datetime.timedelta(minutes=i)
        if i < 8:
            o, c = px, px + 0.25              # quiet open
        elif i < 30:
            o, c = px, px + 1.2               # the ramp
        else:
            o, c = px, px + 0.05              # drift
        hi, lo = max(o, c) + 1.25, min(o, c) - 1.25
        rows.append([t.isoformat(), f"{o:.2f}", f"{hi:.2f}",
                     f"{lo:.2f}", f"{c:.2f}", "10000"])
        px = c
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        w.writerows(rows)
    return path


class TestBacktestRunner:
    def test_end_to_end_session(self, tmp_path):
        from es_backtest import load_bars_csv, run_backtest
        bars = load_bars_csv(_write_session_csv(tmp_path / "bars.csv"))
        out = str(tmp_path / "out")
        summary = run_backtest(bars, out_dir=out, emit_states=True)

        assert summary["trades"] >= 1
        assert not summary["apex"]["breached"]
        assert summary["apex"]["min_headroom"] > 0

        with open(os.path.join(out, "trades_es.csv"), newline="") as f:
            t = list(csv.DictReader(f))
        assert len(t) == summary["trades"]
        assert t[0]["side"] == "long"
        assert int(t[0]["qty"]) >= 1
        assert t[0]["reason"] in ("target", "stop", "trail", "stagnation",
                                  "time_stop")
        assert float(t[0]["stop_px"]) < float(t[0]["entry_px"])

        with gzip.open(os.path.join(out, "decisions_es.csv.gz"), "rt") as f:
            dec = list(csv.DictReader(f))
        assert len(dec) == 120                 # one row per RTH bar
        assert all(f"g_{g}" in dec[0] for g in ES_GATE_NAMES)
        fired = [r for r in dec if r["all_pass"] == "1"]
        assert fired and fired[0]["bar_time_et"] >= "09:45:00"

    def test_deterministic_byte_identical(self, tmp_path):
        from es_backtest import load_bars_csv, run_backtest
        bars = load_bars_csv(_write_session_csv(tmp_path / "bars.csv"))
        for sub in ("a", "b"):
            run_backtest(bars, out_dir=str(tmp_path / sub), emit_states=True)
        for name in ("states_es.csv", "decisions_es.csv.gz", "trades_es.csv"):
            ba = (tmp_path / "a" / name).read_bytes()
            bb = (tmp_path / "b" / name).read_bytes()
            assert ba == bb, name

    def test_golden_compare_self_and_perturbed(self, tmp_path):
        from golden_vectors import compare, generate
        _write_session_csv(tmp_path / "bars.csv")
        generate(str(tmp_path / "bars.csv"), str(tmp_path / "g"))
        golden = str(tmp_path / "g" / "states_es.csv")
        assert compare(golden, golden) == []

        # flip one gate bit in a copy → must be flagged
        with open(golden) as f:
            lines = f.readlines()
        hdr = lines[0].split(",")
        col = hdr.index("g_momentum")
        for i in range(1, len(lines)):
            parts = lines[i].split(",")
            if parts[col] == "1":
                parts[col] = "0"
                lines[i] = ",".join(parts)
                break
        bad = tmp_path / "bad.csv"
        bad.write_text("".join(lines))
        mm = compare(golden, str(bad))
        assert mm and mm[0]["col"] == "g_momentum"

    def test_nt8_loader_shifts_close_stamps_to_open(self, tmp_path):
        from es_backtest import load_bars_csv
        p = tmp_path / "nt.txt"
        p.write_text("20260706 093100;6000.00;6001.25;5999.50;6000.75;1234\n")
        bars = load_bars_csv(str(p))
        assert bars[0].t == datetime.datetime(2026, 7, 6, 9, 30,
                                              tzinfo=config.ET)

    def test_databento_multi_symbol_requires_filter(self, tmp_path):
        from es_backtest import load_bars_csv
        p = tmp_path / "db.csv"
        with open(p, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts_event", "symbol", "open", "high", "low",
                        "close", "volume"])
            w.writerow(["2026-07-06T13:30:00Z", "ESU6", "6000", "6001",
                        "5999", "6000.5", "100"])
            w.writerow(["2026-07-06T13:30:00Z", "ESZ6", "6010", "6011",
                        "6009", "6010.5", "5"])
        with pytest.raises(ValueError):
            load_bars_csv(str(p))
        bars = load_bars_csv(str(p), symbol="ESU6")
        assert len(bars) == 1
        # UTC 13:30 = 09:30 ET
        assert bars[0].t.astimezone(config.ET).time() == datetime.time(9, 30)
