#!/usr/bin/env python3
"""
Feed coverage & quality monitor — turn feed unknowns into daily numbers.

    python feed_monitor.py recordings/session_2026-07-06.jsonl.gz
    python feed_monitor.py recordings/session_*.jsonl.gz --json

Why this exists: Alpaca's Basic plan documents a 30-symbol WebSocket limit
while this bot subscribes 43+ option symbols, and the indicative feed's
sampling behavior is not publicly specified. Neither should be an article of
faith. This tool measures, from the session recording alone:

  COVERAGE — subscribed symbols vs symbols that actually delivered quotes.
    A subscribed-but-silent symbol is a dark spot in the strike window: a
    feed symbol cap, a dead subscription, or a nonexistent contract. Dark
    spots bias WHICH strikes can ever fire, invisibly to the P&L.

  QUALITY — quote inter-arrival distribution (median/p95/max gap during the
    subscribed window), aggregate quote rate, spread distribution, and the
    one-sided-quote share. Run it on indicative sessions now and OPRA
    sessions later: the before/after diff IS the measured cost of the free
    feed.

  SAFETY — for each trade in the session's CSV (if present), the maximum
    quote gap on the HELD symbol while the position was open — the number
    the staleness kill switch lives on.

Output: human report (+ exit code 1 below the coverage threshold, so cron
can alert) and a sidecar JSON next to the recording
(<recording>.feedreport.json) — recordings stay immutable.
"""

import argparse
import csv
import datetime
import glob
import json
import os
import sys
from collections import defaultdict
from typing import Optional

import config
import recorder

COVERAGE_FAIL_BELOW = 0.90   # exit non-zero below this — cron turns it into an alert


def _percentile(sorted_vals, q: float) -> float:
    if not sorted_vals:
        return 0.0
    pos  = q * (len(sorted_vals) - 1)
    lo   = int(pos)
    hi   = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def analyze(recording_path: str, trades_dir: Optional[str] = None) -> dict:
    meta, events = recorder.load_session(recording_path)

    subscribed_at = {}                    # symbol -> first subscription recv_wall
    quote_times   = defaultdict(list)    # symbol -> [recv_wall, ...]
    spreads       = []
    one_sided     = 0
    n_quotes      = 0
    first_w = last_w = None

    for ev in events:
        w = float(ev[1])
        first_w = w if first_w is None else first_w
        last_w  = w
        if ev[0] == "s":
            for sym in ev[2]:
                subscribed_at.setdefault(sym, w)
        elif ev[0] == "q":
            sym, bid, ask = ev[2], float(ev[3]), float(ev[4])
            n_quotes += 1
            quote_times[sym].append(w)
            if bid > 0 and ask > 0 and ask >= bid:
                spreads.append((ask - bid) / ((ask + bid) / 2.0))
            else:
                one_sided += 1

    session_secs = (last_w - first_w) if (first_w and last_w) else 0.0

    # ── Coverage ──────────────────────────────────────────────────────────────
    subscribed = set(subscribed_at)
    delivering = set(quote_times)
    dark       = sorted(subscribed - delivering)
    unexpected = sorted(delivering - subscribed)   # quotes without a recorded sub
    coverage   = (len(subscribed & delivering) / len(subscribed)) if subscribed else 1.0

    # ── Inter-arrival quality (per symbol, within its subscribed window) ─────
    gaps_all = []
    per_symbol_max_gap = {}
    for sym, times in quote_times.items():
        if len(times) < 2:
            continue
        gaps = [b - a for a, b in zip(times, times[1:])]
        gaps_all.extend(gaps)
        per_symbol_max_gap[sym] = max(gaps)
    gaps_all.sort()
    spreads.sort()

    result = {
        "recording":     recording_path,
        "session_date":  meta.get("session_date", ""),
        "stock_feed":    meta.get("stock_feed", "?"),
        "option_feed":   meta.get("option_feed", "?"),
        "n_subscribed":  len(subscribed),
        "n_delivering":  len(delivering),
        "coverage":      round(coverage, 4),
        "dark_symbols":  dark,
        "unexpected_symbols": unexpected,
        "n_quotes":      n_quotes,
        "quotes_per_sec": round(n_quotes / session_secs, 2) if session_secs else 0.0,
        "gap_median_s":  round(_percentile(gaps_all, 0.50), 3),
        "gap_p95_s":     round(_percentile(gaps_all, 0.95), 3),
        "gap_max_s":     round(gaps_all[-1], 1) if gaps_all else 0.0,
        "spread_median_pct": round(_percentile(spreads, 0.50) * 100, 2),
        "spread_p95_pct":    round(_percentile(spreads, 0.95) * 100, 2),
        "one_sided_pct":     round(one_sided / n_quotes * 100, 2) if n_quotes else 0.0,
        "held_symbol_gaps":  [],
    }

    # ── Held-symbol gaps (the staleness-relevant number) ─────────────────────
    date_str = result["session_date"]
    csv_path = os.path.join(trades_dir or config.LOG_DIR, f"trades_{date_str}.csv")
    if date_str and os.path.exists(csv_path):
        with open(csv_path, newline="") as f:
            for r in csv.DictReader(f):
                try:
                    t0 = datetime.datetime.fromisoformat(r["entry_time"]).timestamp()
                    t1 = datetime.datetime.fromisoformat(r["exit_time"]).timestamp()
                except (KeyError, ValueError):
                    continue
                times = [t for t in quote_times.get(r.get("symbol", ""), [])
                         if t0 <= t <= t1]
                max_gap = max((b - a for a, b in zip(times, times[1:])), default=0.0)
                result["held_symbol_gaps"].append({
                    "symbol":    r.get("symbol", ""),
                    "hold_secs": round(t1 - t0, 1),
                    "n_quotes":  len(times),
                    "max_gap_s": round(max_gap, 1),
                })

    return result


def print_report(r: dict):
    W = 74
    print("═" * W)
    print(f"  FEED REPORT — {r['session_date']}   "
          f"stock={r['stock_feed']}  options={r['option_feed']}")
    print("─" * W)
    cov_icon = "✅" if r["coverage"] >= COVERAGE_FAIL_BELOW else "❌"
    print(f"  Coverage:  {cov_icon} {r['n_delivering']}/{r['n_subscribed']} subscribed "
          f"symbols delivered quotes ({r['coverage']*100:.1f}%)")
    if r["dark_symbols"]:
        print(f"  DARK symbols (subscribed, zero quotes) — feed cap, dead sub,")
        print(f"  or nonexistent contract; these strikes can never fire:")
        for s in r["dark_symbols"]:
            print(f"    {s}")
    if r["unexpected_symbols"]:
        print(f"  Unexpected (quotes without recorded subscription): "
              f"{len(r['unexpected_symbols'])}")
    print("─" * W)
    print(f"  Quotes:    {r['n_quotes']:,} total   {r['quotes_per_sec']}/sec aggregate")
    print(f"  Gaps:      median {r['gap_median_s']}s   p95 {r['gap_p95_s']}s   "
          f"max {r['gap_max_s']}s")
    print(f"  Spread:    median {r['spread_median_pct']}%   "
          f"p95 {r['spread_p95_pct']}%   one-sided {r['one_sided_pct']}%")
    if r["held_symbol_gaps"]:
        print("─" * W)
        print("  Held-symbol quote gaps (staleness kill switch operates on these):")
        for h in r["held_symbol_gaps"]:
            flag = "  ⚠" if h["max_gap_s"] > config.STALE_QUOTE_FLATTEN_SEC / 2 else ""
            print(f"    {h['symbol']:<22} held {h['hold_secs']:>7.0f}s  "
                  f"{h['n_quotes']:>6} quotes  max gap {h['max_gap_s']:>6.1f}s{flag}")
    print("═" * W)


def main_cli():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("recordings", nargs="+", help="recording file(s); globs ok")
    ap.add_argument("--trades-dir", default=None,
                    help="dir with trades_*.csv for held-symbol analysis "
                         "(default: logs/)")
    ap.add_argument("--json", action="store_true",
                    help="also write <recording>.feedreport.json sidecars")
    args = ap.parse_args()

    paths = []
    for pattern in args.recordings:
        paths.extend(sorted(glob.glob(pattern)) or [pattern])

    worst = 1.0
    for path in paths:
        r = analyze(path, trades_dir=args.trades_dir)
        print_report(r)
        worst = min(worst, r["coverage"])
        if args.json:
            side = path + ".feedreport.json"
            with open(side, "w") as f:
                json.dump(r, f, indent=2)
            print(f"  sidecar written: {side}")

    sys.exit(0 if worst >= COVERAGE_FAIL_BELOW else 1)


if __name__ == "__main__":
    main_cli()
