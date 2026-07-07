#!/usr/bin/env python3
"""
Drift report — WHERE the strategy changed, WHY it most probably changed, and
WHAT to do about it.

    python drift_report.py logs/ --recent-days 10
    python drift_report.py logs/ --baseline-days 40 --recent-days 10 --json out.json

A log without a comparator is not a diagnostic. This reads the decision log
(gate verdicts per candidate per bar), the attempts log, and the trades CSV,
splits history into BASELINE and RECENT windows, and localizes deviation to a
layer of the funnel:

  L1 market   — are candidates/conditions still arriving? (context PSI)
  L2 signal   — are gates passing at the same rate? which gate moved?
                (per-gate fail rates + SOLE-BLOCKER shares — the single most
                actionable stat: the gate that alone blocks near-misses)
  L3 execute  — are fired signals filling at expected prices? (fill rate)
  L4 realize  — do positions still reach profitable levels (MFE), and how
                much do they give back (capture)? This is the fork between
                "edge decay — stop and redesign" and "exit tuning — replay
                sweeps fix it".
  L5 pnl      — the number everyone watches, diagnosed last because it is
                the SUM of the layers above.

Statistics match trade_stats.py's discipline: distribution shifts scored
with PSI (deciles fit on baseline; <0.10 stable, 0.10–0.25 moderate, >0.25
major), rate changes tested with two-sample by-day bootstrap (days are the
independent unit), deterministic seed, and an insufficient-data gate that
refuses to diagnose from noise.
"""

import argparse
import bisect
import csv
import glob
import json
import math
import os
import random
from collections import defaultdict
from typing import List, Optional

import config
import decision_logger as dl
from signals import GATE_NAMES
from trade_stats import _mean, _percentile

MIN_DAYS_PER_WINDOW = 5
PSI_MODERATE, PSI_MAJOR = 0.10, 0.25
DEFAULT_BOOT, DEFAULT_SEED = 3000, 42


# ── Statistics helpers ─────────────────────────────────────────────────────────

def psi(baseline: List[float], recent: List[float], bins: int = 10) -> Optional[float]:
    """Population Stability Index with decile bins fit on the baseline."""
    base = sorted(v for v in baseline if v is not None)
    rec  = [v for v in recent if v is not None]
    if len(base) < 30 or len(rec) < 30:
        return None
    edges = [_percentile(base, i / bins) for i in range(1, bins)]

    def dist(vals):
        counts = [0] * bins
        for v in vals:
            counts[bisect.bisect_right(edges, v)] += 1
        n = len(vals)
        return [max(c / n, 1e-4) for c in counts]

    p, q = dist(base), dist(rec)
    return sum((qi - pi) * math.log(qi / pi) for pi, qi in zip(p, q))


def psi_categorical(baseline: List[str], recent: List[str]) -> Optional[float]:
    if len(baseline) < 10 or len(recent) < 10:
        return None
    cats = sorted(set(baseline) | set(recent))

    def dist(vals):
        n = len(vals)
        return [max(vals.count(c) / n, 1e-4) for c in cats]

    p, q = dist(baseline), dist(recent)
    return sum((qi - pi) * math.log(qi / pi) for pi, qi in zip(p, q))


def two_sample_day_boot(base_daily: List[float], recent_daily: List[float],
                        n_boot: int, rng: random.Random):
    """(diff, ci95, p) for recent−baseline mean of a per-day statistic.
    Days are the resampling unit — within-day rows are dependent."""
    if len(base_daily) < 2 or len(recent_daily) < 2:
        return None
    diffs = []
    for _ in range(n_boot):
        b = [base_daily[rng.randrange(len(base_daily))] for _ in base_daily]
        r = [recent_daily[rng.randrange(len(recent_daily))] for _ in recent_daily]
        diffs.append(_mean(r) - _mean(b))
    diffs.sort()
    n  = len(diffs)
    le = sum(1 for v in diffs if v <= 0) / n
    ge = sum(1 for v in diffs if v >= 0) / n
    return {
        "diff": round(_mean(recent_daily) - _mean(base_daily), 5),
        "ci":   (round(_percentile(diffs, 0.025), 5),
                 round(_percentile(diffs, 0.975), 5)),
        "p":    max(min(1.0, 2.0 * min(le, ge)), 1.0 / n),
    }


# ── Loading ────────────────────────────────────────────────────────────────────

def _dates_from(paths_glob: str, prefix: str) -> List[str]:
    out = []
    for p in glob.glob(paths_glob):
        name = os.path.basename(p)
        out.append(name[len(prefix):len(prefix) + 10])
    return sorted(set(out))


def load_window(log_dir: str, dates: List[str]) -> dict:
    """All three funnel logs for a set of session dates."""
    decisions, attempts, trades = [], [], []
    dset = set(dates)
    for d in dates:
        decisions.extend(r for r in dl.read_decisions(
            os.path.join(log_dir, f"decisions_{d}.csv.gz")))
        attempts.extend(r for r in dl.read_attempts(
            os.path.join(log_dir, f"attempts_{d}.csv")))
        path = os.path.join(log_dir, f"trades_{d}.csv")
        if os.path.exists(path):
            with open(path, newline="") as f:
                trades.extend(r for r in csv.DictReader(f)
                              if r.get("date") in dset and r.get("realized_pnl"))
    return {"dates": dates, "decisions": decisions,
            "attempts": attempts, "trades": trades}


# ── Per-window funnel metrics ──────────────────────────────────────────────────

def _fl(row, key):
    try:
        return float(row[key]) if row.get(key, "") != "" else None
    except (ValueError, KeyError):
        return None


def summarize(win: dict) -> dict:
    dec, att, tr = win["decisions"], win["attempts"], win["trades"]
    days = win["dates"]

    by_day = defaultdict(list)
    for r in dec:
        by_day[r["date"]].append(r)

    def daily_rate(pred):
        return [(sum(1 for r in rows if pred(r)) / len(rows))
                for rows in by_day.values() if rows]

    s = {
        "days": len(days),
        "candidates_per_day": round(len(dec) / max(1, len(by_day)), 1),
        "signal_rate": _mean([int(r["strategy_pass"]) for r in dec]) if dec else 0.0,
        "signal_rate_daily": daily_rate(lambda r: r["strategy_pass"] == "1"),
        "gate_fail": {g: _mean([1 - int(r[f"g_{g}"]) for r in dec]) if dec else 0.0
                      for g in GATE_NAMES},
        "sole_share": {g: (sum(1 for r in dec if r["sole_blocker"] == g)
                           / len(dec)) if dec else 0.0
                       for g in GATE_NAMES},
        "context": {
            "spread_pct":    [_fl(r, "spread_pct") for r in dec],
            "atr5":          [_fl(r, "atr5") for r in dec],
            "abs_roc5":      [abs(v) if (v := _fl(r, "roc5")) is not None else None
                              for r in dec],
            "zone_dist_pct": [_fl(r, "zone_dist_pct") for r in dec],
            "quote_age_s":   [_fl(r, "quote_age_s") for r in dec],
        },
        "attempts_per_day": round(len(att) / max(1, len(days)), 2),
        "fill_rate": (_mean([1.0 if a["outcome"] in ("filled", "partial") else 0.0
                             for a in att]) if att else None),
    }

    # Realization layer (needs the mfe/mae columns — regenerable via replay
    # for sessions logged before they existed)
    pnls, mfe_ratios, captures, reasons = [], [], [], []
    for t in tr:
        pnl = _fl(t, "realized_pnl")
        if pnl is None:
            continue
        pnls.append(pnl)
        reasons.append(t.get("reason", ""))
        mfe   = _fl(t, "mfe_pnl")
        entry = _fl(t, "entry_price") or 0.0
        qty   = _fl(t, "qty") or 0.0
        notional = entry * qty * 100
        if mfe is not None and notional > 0:
            mfe_ratios.append(mfe / notional)
            if mfe > 0:
                captures.append(pnl / mfe)
    daily_pnl = defaultdict(float)
    for t in tr:
        daily_pnl[t["date"]] += _fl(t, "realized_pnl") or 0.0
    s.update({
        "trades": len(pnls),
        "trades_per_day": round(len(pnls) / max(1, len(days)), 2),
        "ev": round(_mean(pnls), 2) if pnls else None,
        "daily_pnl": [round(v, 2) for v in daily_pnl.values()],
        "win_rate": (sum(1 for p in pnls if p > 0) / len(pnls)) if pnls else None,
        "mfe_ratio_median": (round(_percentile(sorted(mfe_ratios), 0.5), 3)
                             if mfe_ratios else None),
        "capture_median": (round(_percentile(sorted(captures), 0.5), 3)
                           if captures else None),
        "exit_reasons": reasons,
    })
    return s


# ── The findings engine — ranked causes and actions ───────────────────────────

def diagnose(base: dict, rec: dict, n_boot: int, seed: int) -> List[dict]:
    rng = random.Random(seed)
    F = []

    def add(sev, layer, title, evidence, action):
        F.append({"severity": sev, "layer": layer, "title": title,
                  "evidence": evidence, "action": action})

    # L2 — signal flow
    sig = two_sample_day_boot(base["signal_rate_daily"],
                              rec["signal_rate_daily"], n_boot, rng)
    if sig and sig["p"] < 0.05 and sig["diff"] < 0:
        moved = sorted(GATE_NAMES,
                       key=lambda g: rec["gate_fail"][g] - base["gate_fail"][g],
                       reverse=True)[:2]
        driver = ", ".join(f"{g} fail {base['gate_fail'][g]:.1%}→{rec['gate_fail'][g]:.1%}"
                           for g in moved)
        if moved and moved[0] in ("momentum", "atr"):
            add(1, "L2-signal", "Signal rate fell — market regime moved away from the setup",
                f"signal rate {_mean(base['signal_rate_daily']):.2%}→"
                f"{_mean(rec['signal_rate_daily']):.2%} (p={sig['p']:.3f}); driver: {driver}",
                "Do NOT retune exits for this. Validate on replay across vol "
                "regimes; consider a bench strategy for the current regime and "
                "regime-conditional sizing.")
        else:
            add(1, "L2-signal", f"Signal rate fell — gate '{moved[0]}' is the driver",
                f"p={sig['p']:.3f}; {driver}",
                f"Replay-sweep the '{moved[0]}' gate's threshold on recent "
                f"recordings to quantify what relaxing it costs and buys.")

    # L2 — sole-blocker surges (binding constraint changed)
    for g in GATE_NAMES:
        b, r = base["sole_share"][g], rec["sole_share"][g]
        if r > max(2 * b, b + 0.02) and r > 0.02:
            add(2, "L2-blocker", f"'{g}' became the binding constraint",
                f"sole-blocker share {b:.1%}→{r:.1%} — it alone now blocks "
                f"near-miss candidates",
                f"Replay-sweep '{g}' on the recent recording library; if the "
                f"sweep says relaxing it is EV-positive, change it via the "
                f"experiment ledger (--variants-tested).")

    # L1/L3 — context & execution shifts
    ctx_labels = {"spread_pct": ("L3-execution", "Spread regime shifted",
                                 "Execution cost changed — compare feed_monitor "
                                 "reports; consider OPRA and revisit the limit-"
                                 "pricing rule (mid×1.02)."),
                  "quote_age_s": ("L1-data", "Quote latency/age distribution shifted",
                                  "Data-quality issue — run feed_monitor on recent "
                                  "recordings before touching the strategy."),
                  "atr5": ("L1-market", "Volatility regime shifted",
                           "Regime, not defect. Stratify trade_stats by vol "
                           "tercile; check strike-offset behavior (ATR-driven)."),
                  "abs_roc5": ("L1-market", "Momentum magnitude distribution shifted",
                               "Regime. Same action as volatility shift."),
                  "zone_dist_pct": ("L1-market", "Strike-distance geometry shifted",
                                    "Check ATR baseline vs realized moves — the "
                                    "strike offset may be mis-sized for the regime.")}
    for key, (layer, title, action) in ctx_labels.items():
        v = psi(base["context"][key], rec["context"][key])
        if v is not None and v > PSI_MODERATE:
            add(2 if v > PSI_MAJOR else 3, layer,
                f"{title} (PSI {v:.2f})",
                f"PSI {v:.2f} ({'major' if v > PSI_MAJOR else 'moderate'} shift "
                f"vs baseline deciles)", action)

    # L3 — fill rate
    if base["fill_rate"] is not None and rec["fill_rate"] is not None:
        if rec["fill_rate"] < base["fill_rate"] - 0.15:
            add(1, "L3-execution", "Fill rate dropped",
                f"fill rate {base['fill_rate']:.0%}→{rec['fill_rate']:.0%}",
                "Execution regime: quotes moving away faster than the resting "
                "limit. Review attempts log wait times; consider marketable "
                "pricing; check spread PSI above.")

    # L4/L5 — realization
    if base["ev"] is not None and rec["ev"] is not None and rec["trades"] >= 10:
        ev = two_sample_day_boot(base["daily_pnl"], rec["daily_pnl"], n_boot, rng)
        if ev and ev["p"] < 0.10 and ev["diff"] < 0:
            b_mfe, r_mfe = base["mfe_ratio_median"], rec["mfe_ratio_median"]
            b_cap, r_cap = base["capture_median"], rec["capture_median"]
            if b_mfe and r_mfe is not None and r_mfe < 0.7 * b_mfe:
                add(1, "L4-edge", "EDGE DECAY — trades no longer reach profitable levels",
                    f"daily P&L diff ${ev['diff']:+.0f} (p={ev['p']:.3f}); "
                    f"median MFE ratio {b_mfe:.2f}→{r_mfe:.2f}",
                    "The pre-committed response: cut size per the kill criteria, "
                    "keep recording, re-validate on replay. Exit retuning will "
                    "NOT fix this — the favorable excursion itself vanished. "
                    "Promote the best bench candidate.")
            elif b_cap and r_cap is not None and r_cap < 0.7 * b_cap:
                add(1, "L4-exits", "GIVE-BACK — trades reach levels but exits surrender them",
                    f"daily P&L diff ${ev['diff']:+.0f} (p={ev['p']:.3f}); "
                    f"MFE intact ({b_mfe}→{r_mfe}) but capture "
                    f"{b_cap:.2f}→{r_cap:.2f}",
                    "This IS an exit-tuning problem: replay-sweep trail/TP "
                    "levels (--set PEAK_TRAIL_*, TP_MULT) on recent recordings; "
                    "quote the Bonferroni-adjusted result.")
            else:
                add(2, "L5-pnl", "P&L deteriorated without a clean single-layer signature",
                    f"daily P&L diff ${ev['diff']:+.0f} (p={ev['p']:.3f}); "
                    f"MFE {b_mfe}→{r_mfe}, capture {b_cap}→{r_cap}",
                    "Mixed signature — check the L1–L3 findings above first; "
                    "if none, this may be cost creep: run the slippage columns "
                    "through trade_stats and compare fees/spread paid.")

    exit_psi = psi_categorical(base["exit_reasons"], rec["exit_reasons"])
    if exit_psi is not None and exit_psi > PSI_MAJOR:
        add(3, "L4-exits", f"Exit-reason mix shifted (PSI {exit_psi:.2f})",
            "stop/trail/tp composition changed materially",
            "Often the earliest visible symptom — read alongside MFE/capture.")

    F.sort(key=lambda f: f["severity"])
    return F


# ── Report ─────────────────────────────────────────────────────────────────────

def build_report(log_dir: str, baseline_days: Optional[int], recent_days: int,
                 n_boot: int = DEFAULT_BOOT, seed: int = DEFAULT_SEED) -> dict:
    dates = _dates_from(os.path.join(log_dir, "decisions_*.csv.gz"), "decisions_")
    if len(dates) < 2 * MIN_DAYS_PER_WINDOW:
        return {"verdict": (f"INSUFFICIENT DATA: {len(dates)} sessions with "
                            f"decision logs; need ≥{2 * MIN_DAYS_PER_WINDOW}. "
                            f"Tip: replay recorded sessions to regenerate "
                            f"decision logs for history."),
                "dates": dates}
    rec_dates  = dates[-recent_days:]
    base_dates = (dates[:-recent_days][-baseline_days:] if baseline_days
                  else dates[:-recent_days])
    if len(base_dates) < MIN_DAYS_PER_WINDOW or len(rec_dates) < MIN_DAYS_PER_WINDOW:
        return {"verdict": "INSUFFICIENT DATA in one of the windows", "dates": dates}

    base = summarize(load_window(log_dir, base_dates))
    rec  = summarize(load_window(log_dir, rec_dates))
    findings = diagnose(base, rec, n_boot, seed)
    return {
        "baseline": {"dates": (base_dates[0], base_dates[-1]), **_public(base)},
        "recent":   {"dates": (rec_dates[0], rec_dates[-1]), **_public(rec)},
        "findings": findings,
        "verdict": (findings[0]["title"] if findings
                    else "NO MATERIAL DRIFT — recent window consistent with baseline"),
        "n_boot": n_boot, "seed": seed,
    }


def _public(s: dict) -> dict:
    return {k: v for k, v in s.items()
            if k not in ("context", "signal_rate_daily", "daily_pnl", "exit_reasons")}


def print_report(rep: dict):
    W = 76
    print("═" * W)
    print("  DRIFT REPORT — funnel-localized change detection")
    print("═" * W)
    if "baseline" not in rep:
        print(f"  {rep['verdict']}")
        print("═" * W)
        return
    b, r = rep["baseline"], rep["recent"]
    print(f"  Baseline {b['dates'][0]} → {b['dates'][1]}  ({b['days']} sessions)   "
          f"Recent {r['dates'][0]} → {r['dates'][1]}  ({r['days']} sessions)")
    print("─" * W)
    rows = [
        ("candidates/day", "candidates_per_day"), ("signal rate", "signal_rate"),
        ("attempts/day", "attempts_per_day"), ("fill rate", "fill_rate"),
        ("trades/day", "trades_per_day"), ("EV/trade $", "ev"),
        ("win rate", "win_rate"), ("MFE ratio (med)", "mfe_ratio_median"),
        ("capture (med)", "capture_median"),
    ]
    print(f"  {'metric':<18}{'baseline':>12}{'recent':>12}")
    for label, key in rows:
        bv, rv = b.get(key), r.get(key)
        fmt = (lambda v: "—" if v is None else
               (f"{v:.1%}" if "rate" in key or key == "win_rate" else f"{v}"))
        print(f"  {label:<18}{fmt(bv):>12}{fmt(rv):>12}")
    print("─" * W)
    if rep["findings"]:
        print("  FINDINGS (ranked):")
        for i, f in enumerate(rep["findings"], 1):
            sev = {1: "❗", 2: "⚠", 3: "·"}[f["severity"]]
            print(f"  {sev} {i}. [{f['layer']}] {f['title']}")
            print(f"       evidence: {f['evidence']}")
            print(f"       action:   {f['action']}")
    print("─" * W)
    print(f"  VERDICT: {rep['verdict']}")
    print(f"  (bootstrap {rep['n_boot']}, seed {rep['seed']} — deterministic)")
    print("═" * W)


def main_cli():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("log_dir", nargs="?", default=config.LOG_DIR)
    ap.add_argument("--baseline-days", type=int, default=None,
                    help="cap the baseline window (default: all pre-recent history)")
    ap.add_argument("--recent-days", type=int, default=10)
    ap.add_argument("--boot", type=int, default=DEFAULT_BOOT)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--json", metavar="PATH")
    args = ap.parse_args()

    rep = build_report(args.log_dir, args.baseline_days, args.recent_days,
                       args.boot, args.seed)
    print_report(rep)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(rep, f, indent=2, default=str)
        print(f"  JSON written: {args.json}")


if __name__ == "__main__":
    main_cli()
