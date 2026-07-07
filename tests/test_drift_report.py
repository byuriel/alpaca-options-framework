"""Drift report — the findings engine must localize an ENGINEERED shift to
the right funnel layer and prescribe the right action class. Each test
builds a synthetic history where exactly one thing changed."""

import csv
import datetime
import gzip
import os
import random

import pytest

import decision_logger as dl
from drift_report import build_report, psi, two_sample_day_boot
from signals import GATE_NAMES
from trade_stats import _mean


# ── Synthetic history builder ─────────────────────────────────────────────────

def _dates(n, start=datetime.date(2026, 6, 1)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def _write_day(log_dir, date, *, n_bars=60, rng, signal_p=0.06,
               spread_mu=0.08, atr_mu=0.30, sole_gate=None, sole_p=0.0,
               trades=None):
    """One session of decisions (+optional trades). Gates are drawn so that
    `signal_p` of candidates pass all strategy gates; `sole_gate` fails
    alone with prob sole_p (near-misses)."""
    lg = dl.DecisionLogger(log_dir, date)
    for b in range(n_bars):
        rows = []
        for k in range(4):                     # 4 candidates per bar
            gates = {g: True for g in GATE_NAMES}
            r = rng.random()
            if r < sole_p and sole_gate:
                gates[sole_gate] = False       # near-miss: one gate blocks
            elif r > signal_p + sole_p:
                gates["momentum"] = False      # ordinary multi-fail candidate
                gates["zone"] = rng.random() < 0.5
            spread = max(0.01, rng.gauss(spread_mu, 0.02))
            gates["spread"] = spread <= 0.15
            sole = ([g for g, v in gates.items() if not v][0]
                    if sum(1 for v in gates.values() if not v) == 1 else "")
            rows.append({
                "symbol": f"SPY{k}", "side": "call", "strike": 600 + k,
                "spy": 600.0, "zone_dist_pct": 0.002,
                "bid": 0.48, "ask": 0.48 + spread, "mid": 0.5,
                "spread_pct": spread, "quote_age_s": abs(rng.gauss(0.5, 0.2)),
                "direction": "bull", "ema5": 600.1, "ema20": 600.0,
                "vwap": 600.0, "roc5": rng.gauss(0.0005, 0.0002),
                "atr5": max(0.05, rng.gauss(atr_mu, 0.05)),
                "consec": 3, "gates": gates,
                "strategy_pass": all(v for g, v in gates.items()
                                     if g not in ("fresh", "spread")),
                "all_pass": all(gates.values()), "sole_blocker": sole,
                "in_position": False, "entry_pending": False,
                "risk_ok": True, "blackout": None, "event_day": False,
            })
        lg.log_candidates(f"{10 + b // 60:02d}:{b % 60:02d}:00", rows)
    lg.close()

    if trades:
        cols = ["date", "symbol", "side", "strike", "entry_price", "exit_price",
                "qty", "reason", "realized_pnl", "entry_time", "exit_time",
                "fees", "entry_order_id", "exit_order_id", "mfe_pnl",
                "mae_pnl", "peak_mid"]
        with open(os.path.join(log_dir, f"trades_{date}.csv"), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for i, (pnl, mfe, reason) in enumerate(trades):
                w.writerow({"date": date, "symbol": "SPY...", "side": "call",
                            "strike": 600, "entry_price": 0.5,
                            "exit_price": 0.5 + pnl / 300, "qty": 3,
                            "reason": reason, "realized_pnl": pnl,
                            "entry_time": f"{date}T10:0{i % 10}:00-04:00",
                            "exit_time": f"{date}T10:3{i % 10}:00-04:00",
                            "fees": "0.45", "entry_order_id": f"e{date}{i}",
                            "exit_order_id": f"x{date}{i}", "mfe_pnl": mfe,
                            "mae_pnl": -20.0, "peak_mid": 0.6})


def _history(log_dir, *, base_days=12, rec_days=8, base_kw=None, rec_kw=None,
             seed=7):
    rng = random.Random(seed)
    days = _dates(base_days + rec_days)
    for d in days[:base_days]:
        _write_day(log_dir, d, rng=rng, **(base_kw or {}))
    for d in days[base_days:]:
        _write_day(log_dir, d, rng=rng, **(rec_kw or {}))


# ── Helper-level checks ────────────────────────────────────────────────────────

class TestStats:
    def test_psi_identical_distributions_below_moderate(self):
        # PSI carries positive bias ≈ 2·(bins−1)/n for identical dists
        # (~0.036 at n=500) — the correct invariant is that same-distribution
        # noise never crosses the 0.10 "moderate" flag, not that it's ~0.
        rng = random.Random(1)
        a = [rng.gauss(0, 1) for _ in range(500)]
        b = [rng.gauss(0, 1) for _ in range(500)]
        assert psi(a, b) < 0.10

    def test_psi_shifted_distribution_flags(self):
        rng = random.Random(2)
        a = [rng.gauss(0, 1) for _ in range(500)]
        b = [rng.gauss(1.2, 1) for _ in range(500)]
        assert psi(a, b) > 0.25

    def test_two_sample_boot_detects_level_shift(self):
        rng = random.Random(3)
        base = [rng.gauss(10, 2) for _ in range(15)]
        rec  = [rng.gauss(4, 2) for _ in range(10)]
        res = two_sample_day_boot(base, rec, 2000, random.Random(4))
        assert res["p"] < 0.01 and res["diff"] < 0


# ── End-to-end findings ────────────────────────────────────────────────────────

class TestFindings:
    def test_insufficient_data_refuses(self, tmp_path):
        _history(str(tmp_path), base_days=3, rec_days=2)
        rep = build_report(str(tmp_path), None, 2)
        assert "INSUFFICIENT DATA" in rep["verdict"]

    def test_stable_history_no_material_drift(self, tmp_path):
        _history(str(tmp_path))
        rep = build_report(str(tmp_path), None, 8, n_boot=800)
        assert "NO MATERIAL DRIFT" in rep["verdict"]

    def test_spread_widening_localized_to_execution(self, tmp_path):
        # Engineered change: spreads double in the recent window
        _history(str(tmp_path),
                 base_kw={"spread_mu": 0.06}, rec_kw={"spread_mu": 0.13})
        rep = build_report(str(tmp_path), None, 8, n_boot=800)
        layers = [f["layer"] for f in rep["findings"]]
        assert any(l == "L3-execution" for l in layers), rep["findings"]

    def test_sole_blocker_surge_named(self, tmp_path):
        # Engineered change: 'atr' becomes the sole blocker on 10% of
        # candidates in the recent window (near-misses pile up behind it)
        _history(str(tmp_path),
                 base_kw={"sole_gate": "atr", "sole_p": 0.01},
                 rec_kw={"sole_gate": "atr", "sole_p": 0.10})
        rep = build_report(str(tmp_path), None, 8, n_boot=800)
        titles = " | ".join(f["title"] for f in rep["findings"])
        assert "'atr' became the binding constraint" in titles

    def test_edge_decay_vs_giveback_fork(self, tmp_path):
        # Baseline: winners reach good MFE and keep most of it.
        base_tr = [(40.0, 55.0, "tp"), (-25.0, 10.0, "stop"), (35.0, 50.0, "tp")]
        # EDGE DECAY: recent trades never reach favorable territory (MFE
        # collapsed) and lose — must be called edge decay, NOT exit tuning.
        decay_tr = [(-20.0, 4.0, "stop"), (-18.0, 3.0, "stop"),
                    (-22.0, 5.0, "spy_stop")]
        _history(str(tmp_path),
                 base_kw={"trades": base_tr}, rec_kw={"trades": decay_tr})
        rep = build_report(str(tmp_path), None, 8, n_boot=800)
        assert any("EDGE DECAY" in f["title"] for f in rep["findings"]), \
            rep["findings"]
        decay = next(f for f in rep["findings"] if "EDGE DECAY" in f["title"])
        assert "cut size" in decay["action"]
        assert "will NOT fix" in decay["action"]     # exit retuning warned off

    def test_giveback_prescribes_exit_sweeps(self, tmp_path):
        base_tr = [(40.0, 55.0, "tp"), (-25.0, 10.0, "stop"), (35.0, 50.0, "tp")]
        # GIVE-BACK: MFE intact (trades still reach +$50) but exits surrender
        # it — capture collapses. Must prescribe exit sweeps, not size cuts.
        give_tr = [(4.0, 52.0, "peak_trail"), (-15.0, 48.0, "peak_trail"),
                   (2.0, 55.0, "peak_trail")]
        _history(str(tmp_path),
                 base_kw={"trades": base_tr}, rec_kw={"trades": give_tr})
        rep = build_report(str(tmp_path), None, 8, n_boot=800)
        gb = [f for f in rep["findings"] if "GIVE-BACK" in f["title"]]
        assert gb, rep["findings"]
        assert "replay-sweep" in gb[0]["action"].lower()

    def test_deterministic(self, tmp_path):
        _history(str(tmp_path), base_kw={"spread_mu": 0.06},
                 rec_kw={"spread_mu": 0.13})
        a = build_report(str(tmp_path), None, 8, n_boot=500, seed=9)
        b = build_report(str(tmp_path), None, 8, n_boot=500, seed=9)
        assert a == b
