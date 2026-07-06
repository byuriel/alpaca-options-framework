#!/usr/bin/env python3
"""
Statistics layer — uncertainty-quantified performance analysis of trade logs.

    python trade_stats.py logs/                              # live track record
    python trade_stats.py replay_out/session_*/              # replay outputs
    python trade_stats.py logs/ --json stats.json
    python trade_stats.py --compare replay_out_base replay_out_variant \
        --variants-tested 12

Why this exists: point estimates lie at small samples. A month of trading
produces a win rate and an EV that are mostly noise, and a dashboard that
prints them without confidence intervals invites (self-)deception. This
module answers the only question that matters — "is the edge statistically
distinguishable from zero, and with what uncertainty?" — using methods chosen
for SMALL, DEPENDENT samples:

  - Cluster bootstrap BY SESSION DAY. Trades within a day share regime, so
    resampling individual trades understates variance; days are resampled
    with replacement and each carries all its trades. Deterministic (seeded).
  - Exact Student-t p-values (regularized incomplete beta, stdlib math only)
    and Wilson score intervals for win rate — correct at n=20, not just n=500.
  - An explicit INSUFFICIENT-SAMPLE gate: below minimum sample the report
    says "no statistical claim possible" instead of printing a Sharpe ratio
    that is pure noise.
  - Paired comparison mode for replay sweeps: variants run on the SAME
    recorded sessions are compared on paired daily differences (far more
    power than unpaired), with a Bonferroni adjustment for the number of
    variants tested — quoting the raw p-value of the best of 12 sweeps is
    data mining, and this tool refuses to help you do it silently.

Everything is stdlib. Bootstrap replicates default to 5000; results are
reproducible for a given --seed.
"""

import argparse
import csv
import datetime
import glob
import json
import math
import os
import random
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

TRADING_DAYS_PER_YEAR = 252
DEFAULT_BOOT          = 5000
DEFAULT_SEED          = 42

# Below these, no inferential claim is printed — only raw counts.
MIN_TRADES_FOR_INFERENCE = 30
MIN_DAYS_FOR_INFERENCE   = 10

# Booked by replay when a recording ends mid-position — a truncation
# artifact, not a strategy exit. Excluded by default.
DEFAULT_EXCLUDED_REASONS = ("replay_eof",)


# ── Exact small-sample distributions (stdlib only) ─────────────────────────────

_LANCZOS = (
    676.5203681218851, -1259.1392167224028, 771.32342877765313,
    -176.61502916214059, 12.507343278686905, -0.13857109526572012,
    9.9843695780195716e-6, 1.5056327351493116e-7,
)


def _ln_gamma(x: float) -> float:
    if x < 0.5:
        return math.log(math.pi / math.sin(math.pi * x)) - _ln_gamma(1.0 - x)
    x -= 1.0
    a = 0.99999999999980993
    t = x + 7.5
    for i, c in enumerate(_LANCZOS):
        a += c / (x + i + 1)
    return 0.5 * math.log(2 * math.pi) + (x + 0.5) * math.log(t) - t + math.log(a)


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta (Lentz's method)."""
    MAXIT, EPS, FPMIN = 300, 3e-12, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def betainc_reg(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    ln_bt = (_ln_gamma(a + b) - _ln_gamma(a) - _ln_gamma(b)
             + a * math.log(x) + b * math.log(1.0 - x))
    bt = math.exp(ln_bt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def t_two_sided_p(t: float, df: float) -> float:
    """Exact two-sided p-value of a Student-t statistic."""
    if df <= 0:
        return 1.0
    return betainc_reg(df / 2.0, 0.5, df / (df + t * t))


def wilson_interval(wins: int, n: int, z: float = 1.959963984540054):
    """Wilson score 95% interval for a binomial proportion — correct at the
    small n where the naive ±1.96·SE interval breaks down."""
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    z2 = z * z
    denom  = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    half   = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return max(0.0, center - half), min(1.0, center + half)


def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile of a pre-sorted list, q in [0, 1]."""
    n = len(sorted_vals)
    if n == 0:
        return float("nan")
    if n == 1:
        return sorted_vals[0]
    pos = q * (n - 1)
    lo  = int(math.floor(pos))
    hi  = min(lo + 1, n - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


# ── Data model ─────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    date:   str
    pnl:    float
    reason: str = ""
    side:   str = ""
    hold_secs: float = 0.0


def load_trades(paths: Sequence[str],
                exclude_reasons: Sequence[str] = DEFAULT_EXCLUDED_REASONS):
    """
    Load trades from directories (globbing trades_*.csv inside) and/or CSV
    files. Returns (trades, excluded_count). P&L in the CSV is already net
    of fees (state.py books it that way).
    """
    files = []
    for p in paths:
        if os.path.isdir(p):
            files.extend(sorted(glob.glob(os.path.join(p, "trades_*.csv"))))
        else:
            files.extend(sorted(glob.glob(p)) or [p])

    trades, excluded = [], 0
    for path in files:
        try:
            with open(path, newline="") as f:
                for r in csv.DictReader(f):
                    if not r.get("realized_pnl"):
                        continue
                    try:
                        pnl = float(r["realized_pnl"])
                    except ValueError:
                        continue
                    reason = r.get("reason", "")
                    if reason in exclude_reasons:
                        excluded += 1
                        continue
                    hold = 0.0
                    try:
                        t0 = datetime.datetime.fromisoformat(r["entry_time"])
                        t1 = datetime.datetime.fromisoformat(r["exit_time"])
                        hold = (t1 - t0).total_seconds()
                    except (KeyError, ValueError):
                        pass
                    trades.append(Trade(
                        date=r.get("date", ""), pnl=pnl, reason=reason,
                        side=r.get("side", ""), hold_secs=hold,
                    ))
        except OSError:
            continue
    return trades, excluded


def _by_day(trades: Sequence[Trade]) -> Dict[str, List[float]]:
    days = defaultdict(list)
    for t in trades:
        days[t.date].append(t.pnl)
    return dict(sorted(days.items()))


# ── Core statistics ────────────────────────────────────────────────────────────

def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def max_drawdown(daily_pnls: Sequence[float]) -> float:
    """Peak-to-trough drawdown of the cumulative daily P&L path (positive $)."""
    cum = peak = 0.0
    mdd = 0.0
    for p in daily_pnls:
        cum += p
        peak = max(peak, cum)
        mdd  = min(mdd, cum - peak)
    return -mdd


def cluster_bootstrap(day_groups: Sequence[List[float]], stat_fn,
                      n_boot: int, rng: random.Random) -> List[float]:
    """
    Resample DAYS with replacement (each carrying all its trades) and apply
    stat_fn to the pooled trade list. Respects within-day dependence; the
    resulting intervals are wider — and more honest — than trade-level ones.
    """
    D = len(day_groups)
    out = []
    for _ in range(n_boot):
        pooled = []
        for _ in range(D):
            pooled.extend(day_groups[rng.randrange(D)])
        out.append(stat_fn(pooled))
    out.sort()
    return out


def day_bootstrap(daily: Sequence[float], stat_fn,
                  n_boot: int, rng: random.Random) -> List[float]:
    """Resample the daily P&L series (iid days) and apply stat_fn."""
    D = len(daily)
    out = []
    for _ in range(n_boot):
        sample = [daily[rng.randrange(D)] for _ in range(D)]
        out.append(stat_fn(sample))
    out.sort()
    return out


def _boot_p_two_sided(sorted_boot: Sequence[float]) -> float:
    """Percentile-bootstrap two-sided p-value for H0: statistic == 0."""
    n = len(sorted_boot)
    if n == 0:
        return 1.0
    le = sum(1 for v in sorted_boot if v <= 0.0) / n
    ge = sum(1 for v in sorted_boot if v >= 0.0) / n
    return max(min(1.0, 2.0 * min(le, ge)), 1.0 / n)   # floored at 1/n_boot


def compute_stats(trades: Sequence[Trade], n_boot: int = DEFAULT_BOOT,
                  seed: int = DEFAULT_SEED) -> dict:
    """Full uncertainty-quantified statistics. Deterministic for given seed."""
    rng  = random.Random(seed)
    days = _by_day(trades)
    day_groups = list(days.values())
    daily      = [sum(v) for v in day_groups]
    pnls       = [t.pnl for t in trades]
    n, D       = len(trades), len(days)

    s = {
        "n_trades": n,
        "n_days":   D,
        "date_range": (min(days) if days else "", max(days) if days else ""),
        "net_pnl":  round(sum(pnls), 2),
        "n_boot":   n_boot,
        "seed":     seed,
        "sufficient": n >= MIN_TRADES_FOR_INFERENCE and D >= MIN_DAYS_FOR_INFERENCE,
    }
    if n == 0:
        s["verdict"] = "NO TRADES"
        return s

    # Point estimates
    wins       = [p for p in pnls if p > 0]
    losses     = [p for p in pnls if p < 0]
    gross_win  = sum(wins)
    gross_loss = -sum(losses)
    ev         = _mean(pnls)
    sd         = _stdev(pnls)
    s.update({
        "ev_per_trade": round(ev, 2),
        "win_rate":     len(wins) / n,
        "win_rate_ci":  wilson_interval(len(wins), n),
        "avg_win":      round(_mean(wins), 2) if wins else 0.0,
        "avg_loss":     round(_mean(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else float("inf"),
        "sqn":          round(math.sqrt(n) * ev / sd, 2) if sd > 0 else float("inf"),
        "avg_hold_min": round(_mean([t.hold_secs for t in trades]) / 60, 1),
        "max_drawdown": round(max_drawdown(daily), 2),
        "best_day":     round(max(daily), 2),
        "worst_day":    round(min(daily), 2),
        "green_days":   sum(1 for d in daily if d > 0),
    })

    # Breakeven win rate implied by the realized payoff asymmetry
    aw, al = s["avg_win"], -s["avg_loss"]
    s["breakeven_win_rate"] = al / (aw + al) if (aw + al) > 0 else None

    # Per-trade t-stat (independence caveat applies — bootstrap below is the
    # primary inference; this is reported because allocators ask for it)
    if sd > 0 and n >= 2:
        t = ev / (sd / math.sqrt(n))
        s["t_stat"]   = round(t, 2)
        s["t_p_value"] = t_two_sided_p(t, n - 1)

    # Cluster bootstrap (by day) — primary inference on EV
    if D >= 2:
        boot_ev = cluster_bootstrap(day_groups, _mean, n_boot, rng)
        s["ev_ci"]     = (round(_percentile(boot_ev, 0.025), 2),
                          round(_percentile(boot_ev, 0.975), 2))
        s["ev_boot_p"] = _boot_p_two_sided(boot_ev)

        boot_pf = cluster_bootstrap(
            day_groups,
            lambda xs: (sum(x for x in xs if x > 0)
                        / max(1e-9, -sum(x for x in xs if x < 0))),
            n_boot, rng)
        s["pf_ci"] = (round(_percentile(boot_pf, 0.025), 2),
                      round(_percentile(boot_pf, 0.975), 2))

        # Daily-series statistics
        mu_d, sd_d = _mean(daily), _stdev(daily)
        if sd_d > 0:
            s["sharpe_annual"] = round(mu_d / sd_d * math.sqrt(TRADING_DAYS_PER_YEAR), 2)
            boot_sharpe = day_bootstrap(
                daily,
                lambda xs: (_mean(xs) / _stdev(xs) * math.sqrt(TRADING_DAYS_PER_YEAR)
                            if _stdev(xs) > 0 else 0.0),
                n_boot, rng)
            s["sharpe_ci"] = (round(_percentile(boot_sharpe, 0.025), 2),
                              round(_percentile(boot_sharpe, 0.975), 2))
        downside = math.sqrt(_mean([min(d, 0.0) ** 2 for d in daily]))
        if downside > 0:
            s["sortino_annual"] = round(mu_d / downside * math.sqrt(TRADING_DAYS_PER_YEAR), 2)

        # Drawdown distribution under day-resampling: "in a typical
        # alternate ordering of days like these, how deep does it go?"
        boot_mdd = day_bootstrap(daily, max_drawdown, n_boot, rng)
        s["mdd_median"] = round(_percentile(boot_mdd, 0.50), 2)
        s["mdd_p95"]    = round(_percentile(boot_mdd, 0.95), 2)

    # Breakdown by exit reason (counts always; no inference at tiny n)
    by_reason = defaultdict(list)
    for t in trades:
        by_reason[t.reason].append(t.pnl)
    s["by_reason"] = {
        k: {"n": len(v), "pnl": round(sum(v), 2), "ev": round(_mean(v), 2)}
        for k, v in sorted(by_reason.items())
    }

    # Verdict — the sample gate comes FIRST
    if not s["sufficient"]:
        s["verdict"] = (f"INSUFFICIENT SAMPLE (n={n} trades, {D} days; "
                        f"need ≥{MIN_TRADES_FOR_INFERENCE} trades and "
                        f"≥{MIN_DAYS_FOR_INFERENCE} days) — no statistical "
                        f"claim possible")
    else:
        p = s.get("ev_boot_p", 1.0)
        if p < 0.01 and ev > 0:
            s["verdict"] = f"STATISTICALLY SIGNIFICANT positive edge (bootstrap p={p:.4f})"
        elif p < 0.05 and ev > 0:
            s["verdict"] = f"SUGGESTIVE positive edge (bootstrap p={p:.3f}) — not yet conclusive"
        elif p < 0.05 and ev < 0:
            s["verdict"] = f"STATISTICALLY SIGNIFICANT NEGATIVE edge (bootstrap p={p:.3f})"
        else:
            s["verdict"] = (f"NO DETECTABLE EDGE (bootstrap p={p:.2f}) — "
                            f"results consistent with zero")
    return s


# ── Paired comparison (replay sweeps) ─────────────────────────────────────────

def compare(base_trades: Sequence[Trade], variant_trades: Sequence[Trade],
            n_boot: int = DEFAULT_BOOT, seed: int = DEFAULT_SEED,
            variants_tested: int = 1) -> dict:
    """
    Paired comparison of two variants run on the SAME sessions (replay
    sweeps). Inference is on paired DAILY P&L differences — pairing removes
    common session-level variance and is dramatically more powerful than
    comparing two independent aggregates.

    variants_tested: how many variants were tried in this sweep. The
    Bonferroni-adjusted p is what you may honestly quote for the best one.
    """
    rng    = random.Random(seed)
    base_d = {d: sum(v) for d, v in _by_day(base_trades).items()}
    var_d  = {d: sum(v) for d, v in _by_day(variant_trades).items()}
    common = sorted(set(base_d) & set(var_d))
    only_base    = sorted(set(base_d) - set(var_d))
    only_variant = sorted(set(var_d) - set(base_d))

    out = {
        "n_common_days": len(common),
        "unmatched_days": len(only_base) + len(only_variant),
        "variants_tested": variants_tested,
    }
    if not common:
        out["verdict"] = "NO OVERLAPPING SESSIONS — nothing to compare"
        return out

    diffs = [var_d[d] - base_d[d] for d in common]
    out["daily"] = [
        {"date": d, "base": round(base_d[d], 2), "variant": round(var_d[d], 2),
         "diff": round(var_d[d] - base_d[d], 2)}
        for d in common
    ]
    out["mean_daily_diff"]  = round(_mean(diffs), 2)
    out["total_diff"]       = round(sum(diffs), 2)
    out["days_improved"]    = sum(1 for x in diffs if x > 0)

    if len(diffs) >= 2:
        boot = day_bootstrap(diffs, _mean, n_boot, rng)
        out["diff_ci"] = (round(_percentile(boot, 0.025), 2),
                          round(_percentile(boot, 0.975), 2))
        p = _boot_p_two_sided(boot)
        out["p_raw"]        = p
        out["p_bonferroni"] = min(1.0, p * max(1, variants_tested))

    p_adj = out.get("p_bonferroni", 1.0)
    if len(common) < MIN_DAYS_FOR_INFERENCE:
        out["verdict"] = (f"INSUFFICIENT OVERLAP ({len(common)} common days; "
                          f"need ≥{MIN_DAYS_FOR_INFERENCE}) — direction only, "
                          f"no statistical claim")
    elif p_adj < 0.05:
        direction = "IMPROVES" if out["mean_daily_diff"] > 0 else "HURTS"
        out["verdict"] = (f"Variant {direction} performance "
                          f"(Bonferroni-adjusted p={p_adj:.3f} "
                          f"across {variants_tested} variants tested)")
    else:
        out["verdict"] = (f"NO SIGNIFICANT DIFFERENCE after adjusting for "
                          f"{variants_tested} variants tested "
                          f"(adjusted p={p_adj:.2f})")
    return out


# ── Report rendering ───────────────────────────────────────────────────────────

def _fmt_ci(ci, unit="$"):
    return f"[{unit}{ci[0]:+.2f}, {unit}{ci[1]:+.2f}]" if ci else "n/a"


def print_report(s: dict, excluded: int = 0):
    W = 74
    print("═" * W)
    print("  STATISTICAL REPORT — uncertainty-quantified, cluster bootstrap by day")
    print("═" * W)
    if s.get("n_trades", 0) == 0:
        print("  No trades found.")
        print("═" * W)
        return
    d0, d1 = s["date_range"]
    print(f"  Sample:    {s['n_trades']} trades over {s['n_days']} sessions "
          f"({d0} → {d1})")
    if excluded:
        print(f"             ({excluded} rows excluded: reasons {DEFAULT_EXCLUDED_REASONS})")
    print(f"  Net P&L:   ${s['net_pnl']:+.2f}   (all figures net of fees)")
    print("─" * W)
    print(f"  EV/trade:  ${s['ev_per_trade']:+.2f}   "
          f"95% CI {_fmt_ci(s.get('ev_ci'))}   bootstrap p={s.get('ev_boot_p', 1):.4f}")
    if "t_stat" in s:
        print(f"             t={s['t_stat']:+.2f} (p={s['t_p_value']:.4f}, "
              f"per-trade, independence assumed — bootstrap above is primary)")
    lo, hi = s["win_rate_ci"]
    print(f"  Win rate:  {s['win_rate']*100:.1f}%   Wilson 95% CI "
          f"[{lo*100:.1f}%, {hi*100:.1f}%]")
    if s.get("breakeven_win_rate") is not None:
        be = s["breakeven_win_rate"] * 100
        print(f"             breakeven at realized payoff asymmetry: {be:.1f}%  "
              f"(avg win ${s['avg_win']:+.2f} / avg loss ${s['avg_loss']:+.2f})")
    pf_ci = s.get("pf_ci")
    print(f"  P. factor: {s['profit_factor']}   95% CI {_fmt_ci(pf_ci, unit='') if pf_ci else 'n/a'}")
    print(f"  SQN:       {s['sqn']}   |   avg hold {s['avg_hold_min']} min")
    print("─" * W)
    if "sharpe_annual" in s:
        print(f"  Sharpe (ann.):  {s['sharpe_annual']:+.2f}   "
              f"95% CI {_fmt_ci(s.get('sharpe_ci'), unit='')}")
    if "sortino_annual" in s:
        print(f"  Sortino (ann.): {s['sortino_annual']:+.2f}")
    print(f"  Max drawdown:   ${s['max_drawdown']:.2f} observed   |   "
          f"day-resampled median ${s.get('mdd_median', 0):.2f}, "
          f"p95 ${s.get('mdd_p95', 0):.2f}")
    print(f"  Days:      {s['green_days']}/{s['n_days']} green   "
          f"best ${s['best_day']:+.2f}   worst ${s['worst_day']:+.2f}")
    print("─" * W)
    print("  By exit reason:")
    for k, v in s["by_reason"].items():
        note = "" if v["n"] >= 10 else "   (n<10 — no inference)"
        print(f"    {k:<12} n={v['n']:<4} pnl=${v['pnl']:+10.2f}  "
              f"ev=${v['ev']:+7.2f}{note}")
    print("─" * W)
    print(f"  VERDICT:   {s['verdict']}")
    print("─" * W)
    print("  What this report cannot tell you: fills are paper/simulated;")
    print("  one market regime; parameters chosen after seeing some of this")
    print(f"  data. Bootstrap: {s['n_boot']} replicates, seed {s['seed']} (deterministic).")
    print("═" * W)


def print_compare(c: dict):
    W = 74
    print("═" * W)
    print("  PAIRED COMPARISON — variant vs base on identical sessions")
    print("═" * W)
    if not c.get("n_common_days"):
        print(f"  {c['verdict']}")
        print("═" * W)
        return
    print(f"  Common sessions: {c['n_common_days']}"
          + (f"   (unmatched dropped: {c['unmatched_days']})" if c["unmatched_days"] else ""))
    for row in c["daily"]:
        print(f"    {row['date']}   base ${row['base']:+9.2f}   "
              f"variant ${row['variant']:+9.2f}   diff ${row['diff']:+9.2f}")
    print("─" * W)
    print(f"  Mean daily diff: ${c['mean_daily_diff']:+.2f}   "
          f"95% CI {_fmt_ci(c.get('diff_ci'))}")
    print(f"  Total diff:      ${c['total_diff']:+.2f}   "
          f"days improved: {c['days_improved']}/{c['n_common_days']}")
    if "p_raw" in c:
        print(f"  p (raw):         {c['p_raw']:.4f}")
        print(f"  p (Bonferroni ×{c['variants_tested']}): {c['p_bonferroni']:.4f}"
              f"   ← quote THIS one for the best of a sweep")
    print("─" * W)
    print(f"  VERDICT:   {c['verdict']}")
    print("═" * W)


# ── CLI ────────────────────────────────────────────────────────────────────────

def main_cli():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("paths", nargs="*",
                    help="directories or trades_*.csv files")
    ap.add_argument("--compare", nargs=2, metavar=("BASE", "VARIANT"),
                    help="paired comparison of two result dirs (replay sweeps)")
    ap.add_argument("--variants-tested", type=int, default=1,
                    help="how many variants this sweep tried (Bonferroni)")
    ap.add_argument("--boot", type=int, default=DEFAULT_BOOT)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--include-reasons", default="",
                    help="comma-separated reasons NOT to exclude "
                         f"(default excluded: {','.join(DEFAULT_EXCLUDED_REASONS)})")
    ap.add_argument("--json", metavar="PATH", help="also write machine-readable JSON")
    args = ap.parse_args()

    include = set(filter(None, args.include_reasons.split(",")))
    exclude = tuple(r for r in DEFAULT_EXCLUDED_REASONS if r not in include)

    if args.compare:
        base, _    = load_trades([args.compare[0]], exclude)
        variant, _ = load_trades([args.compare[1]], exclude)
        result = compare(base, variant, n_boot=args.boot, seed=args.seed,
                         variants_tested=args.variants_tested)
        print_compare(result)
    else:
        if not args.paths:
            raise SystemExit("Provide paths (or --compare BASE VARIANT). See --help.")
        trades, excluded = load_trades(args.paths, exclude)
        result = compute_stats(trades, n_boot=args.boot, seed=args.seed)
        print_report(result, excluded)

    if args.json:
        with open(args.json, "w") as f:
            json.dump(result, f, indent=2, default=str)
        print(f"  JSON written: {args.json}")


if __name__ == "__main__":
    main_cli()
