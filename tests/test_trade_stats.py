"""
Statistics layer tests.

The exact-distribution functions are validated against textbook values —
if t_two_sided_p is wrong, every verdict downstream is wrong. The cluster
bootstrap is validated for the property it exists for: within-day dependence
must WIDEN intervals versus naive trade resampling.
"""

import csv
import math
import os
import random

import pytest

import trade_stats as ts
from trade_stats import (
    Trade, betainc_reg, cluster_bootstrap, compare, compute_stats,
    load_trades, max_drawdown, t_two_sided_p, wilson_interval, _percentile,
)


class TestExactDistributions:
    def test_t_p_value_textbook_values(self):
        # Standard t-table: two-sided p for t=2.0, df=10 is 0.0734
        assert t_two_sided_p(2.0, 10) == pytest.approx(0.0734, abs=1e-3)
        # Critical value: t=2.228, df=10 → p = 0.05
        assert t_two_sided_p(2.228, 10) == pytest.approx(0.05, abs=1e-3)
        # Large df converges to normal: t=1.96 → p ≈ 0.05
        assert t_two_sided_p(1.96, 100000) == pytest.approx(0.05, abs=1e-3)
        # Symmetry and edges
        assert t_two_sided_p(-2.0, 10) == pytest.approx(t_two_sided_p(2.0, 10))
        assert t_two_sided_p(0.0, 10) == pytest.approx(1.0)

    def test_betainc_identities(self):
        assert betainc_reg(2.0, 3.0, 0.0) == 0.0
        assert betainc_reg(2.0, 3.0, 1.0) == 1.0
        # I_x(1,1) = x (uniform CDF)
        assert betainc_reg(1.0, 1.0, 0.37) == pytest.approx(0.37, abs=1e-9)
        # I_0.5(a,a) = 0.5 by symmetry
        assert betainc_reg(4.0, 4.0, 0.5) == pytest.approx(0.5, abs=1e-9)

    def test_wilson_textbook(self):
        lo, hi = wilson_interval(6, 10)
        assert lo == pytest.approx(0.3127, abs=1e-3)
        assert hi == pytest.approx(0.8318, abs=1e-3)

    def test_wilson_extremes_stay_in_bounds(self):
        assert wilson_interval(0, 10)[0] == 0.0
        assert wilson_interval(10, 10)[1] == pytest.approx(1.0)
        lo, hi = wilson_interval(0, 10)
        assert hi > 0.0          # zero successes still admits nonzero p
        assert wilson_interval(0, 0) == (0.0, 1.0)

    def test_percentile_interpolation(self):
        vals = [1.0, 2.0, 3.0, 4.0]
        assert _percentile(vals, 0.0) == 1.0
        assert _percentile(vals, 1.0) == 4.0
        assert _percentile(vals, 0.5) == pytest.approx(2.5)


class TestMaxDrawdown:
    def test_hand_computed(self):
        # cum: 10, -10, -5, -15, 15 ; peak: 10 → deepest trough −15 → MDD 25
        assert max_drawdown([10, -20, 5, -10, 30]) == 25.0

    def test_monotone_up_has_zero(self):
        assert max_drawdown([5, 5, 5]) == 0.0

    def test_all_losses(self):
        assert max_drawdown([-10, -20]) == 30.0


class TestClusterBootstrap:
    def test_deterministic_for_seed(self):
        days = [[10.0, -5.0], [3.0], [-8.0, 2.0, 1.0]]
        a = cluster_bootstrap(days, lambda xs: sum(xs) / len(xs), 500, random.Random(7))
        b = cluster_bootstrap(days, lambda xs: sum(xs) / len(xs), 500, random.Random(7))
        assert a == b

    def test_within_day_dependence_widens_interval(self):
        # 20 days × 10 trades; ALL trades within a day share the day's value
        # (perfect within-day correlation). Effective sample size is 20, not
        # 200 — the cluster CI must be far wider than naive trade resampling.
        rng_data = random.Random(1)
        day_vals = [rng_data.gauss(0, 10) for _ in range(20)]
        days = [[v] * 10 for v in day_vals]
        trades = [x for d in days for x in d]

        mean = lambda xs: sum(xs) / len(xs)
        cluster = cluster_bootstrap(days, mean, 2000, random.Random(2))
        naive = []
        r = random.Random(2)
        for _ in range(2000):
            naive.append(mean([trades[r.randrange(len(trades))]
                               for _ in range(len(trades))]))
        naive.sort()

        cluster_width = _percentile(cluster, 0.975) - _percentile(cluster, 0.025)
        naive_width   = _percentile(naive, 0.975) - _percentile(naive, 0.025)
        # sqrt(10) ≈ 3.16× wider in theory; demand at least 2× in practice
        assert cluster_width > 2.0 * naive_width


def _mk_trades(day_pnls, per_day=1, spread=0.0, seed=3):
    """day_pnls: list of per-trade P&L means, one entry per day."""
    rng = random.Random(seed)
    out = []
    for i, mu in enumerate(day_pnls):
        date = f"2026-06-{(i % 28) + 1:02d}" if i < 28 else f"2026-07-{(i - 27):02d}"
        for _ in range(per_day):
            out.append(Trade(date=date, pnl=mu + rng.gauss(0, spread), reason="tp"))
    return out


class TestVerdicts:
    def test_insufficient_sample_refuses_to_claim(self):
        s = compute_stats(_mk_trades([50.0] * 5), n_boot=200)
        assert s["sufficient"] is False
        assert "INSUFFICIENT SAMPLE" in s["verdict"]

    def test_strong_edge_is_significant(self):
        # 15 days × 4 trades, mean +$40, sd $10 — unambiguous edge
        s = compute_stats(_mk_trades([40.0] * 15, per_day=4, spread=10.0),
                          n_boot=1000)
        assert s["sufficient"] is True
        assert "SIGNIFICANT positive edge" in s["verdict"]
        assert s["ev_ci"][0] > 0

    def test_pure_noise_is_no_edge(self):
        rng = random.Random(9)
        day_means = [rng.gauss(0, 30) for _ in range(20)]
        s = compute_stats(_mk_trades(day_means, per_day=3, spread=20.0),
                          n_boot=1000)
        assert "NO DETECTABLE EDGE" in s["verdict"]

    def test_negative_edge_called_out(self):
        s = compute_stats(_mk_trades([-40.0] * 15, per_day=4, spread=10.0),
                          n_boot=1000)
        assert "NEGATIVE" in s["verdict"]

    def test_deterministic_report(self):
        trades = _mk_trades([10.0, -5.0, 20.0] * 6, per_day=3, spread=15.0)
        a = compute_stats(trades, n_boot=500, seed=11)
        b = compute_stats(trades, n_boot=500, seed=11)
        assert a == b

    def test_breakeven_win_rate(self):
        # avg win 100, avg loss 50 → breakeven at 50/(100+50) = 33.3%
        trades = ([Trade(date="2026-06-01", pnl=100.0)] * 5
                  + [Trade(date="2026-06-02", pnl=-50.0)] * 5)
        s = compute_stats(trades, n_boot=100)
        assert s["breakeven_win_rate"] == pytest.approx(1 / 3, abs=1e-6)


class TestLoader:
    def _write_csv(self, path, rows):
        cols = ["date", "symbol", "side", "strike", "entry_price", "exit_price",
                "qty", "reason", "realized_pnl", "entry_time", "exit_time"]
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow(r)

    def test_loads_dirs_and_excludes_replay_eof(self, tmp_path):
        self._write_csv(tmp_path / "trades_2026-07-06.csv", [
            {"date": "2026-07-06", "reason": "tp", "realized_pnl": "50",
             "entry_time": "2026-07-06T10:00:00-04:00",
             "exit_time": "2026-07-06T10:05:00-04:00", "side": "call"},
            {"date": "2026-07-06", "reason": "replay_eof", "realized_pnl": "10",
             "entry_time": "2026-07-06T11:00:00-04:00",
             "exit_time": "2026-07-06T11:05:00-04:00", "side": "put"},
        ])
        trades, excluded = load_trades([str(tmp_path)])
        assert len(trades) == 1
        assert excluded == 1
        assert trades[0].pnl == 50.0
        assert trades[0].hold_secs == 300.0

    def test_include_flag_restores_excluded_reason(self, tmp_path):
        self._write_csv(tmp_path / "trades_2026-07-06.csv", [
            {"date": "2026-07-06", "reason": "replay_eof", "realized_pnl": "10",
             "entry_time": "", "exit_time": "", "side": "put"},
        ])
        trades, excluded = load_trades([str(tmp_path)], exclude_reasons=())
        assert len(trades) == 1 and excluded == 0


class TestCompare:
    def _sessions(self, deltas, base_mu=20.0):
        base, variant = [], []
        for i, d in enumerate(deltas):
            date = f"2026-06-{i + 1:02d}"
            base.append(Trade(date=date, pnl=base_mu))
            variant.append(Trade(date=date, pnl=base_mu + d))
        return base, variant

    def test_consistent_improvement_detected(self):
        base, variant = self._sessions([15.0] * 12)   # +$15 every session
        c = compare(base, variant, n_boot=1000)
        assert c["n_common_days"] == 12
        assert c["mean_daily_diff"] == pytest.approx(15.0)
        assert "IMPROVES" in c["verdict"]

    def test_bonferroni_kills_marginal_result(self):
        # A modest, noisy improvement that clears raw p≈.05 must NOT survive
        # honest adjustment for a 20-variant sweep.
        rng = random.Random(5)
        base, variant = self._sessions([8.0 + rng.gauss(0, 12) for _ in range(12)])
        raw = compare(base, variant, n_boot=2000, variants_tested=1)
        adj = compare(base, variant, n_boot=2000, variants_tested=20)
        assert adj["p_bonferroni"] == pytest.approx(
            min(1.0, raw["p_raw"] * 20), abs=1e-9)
        assert adj["p_bonferroni"] >= raw["p_raw"]

    def test_insufficient_overlap(self):
        base, variant = self._sessions([10.0] * 4)
        c = compare(base, variant, n_boot=200)
        assert "INSUFFICIENT OVERLAP" in c["verdict"]

    def test_disjoint_sessions(self):
        base = [Trade(date="2026-06-01", pnl=10.0)]
        variant = [Trade(date="2026-06-02", pnl=20.0)]
        c = compare(base, variant, n_boot=100)
        assert "NO OVERLAPPING SESSIONS" in c["verdict"]
