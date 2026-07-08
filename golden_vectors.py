"""
Golden vectors - the machine-checked contract between the Python oracle and
the C# NinjaScript port (ninjatrader/).

"Do not fork the strategy" is enforced here, not by code review: the C# port
may trade only while it reproduces these vectors. The vectors are per-bar
snapshots of the FULL session state - indicators, every gate verdict, entry
signals, shadow-position lifecycle, stop/target levels, sizing - emitted by
the exact runner the backtests use (es_backtest.run_backtest emit_states).

    python golden_vectors.py generate bars.csv out/goldens [--zone grid]
    python golden_vectors.py compare  out/goldens/states_es.csv csharp_states.csv

compare() checks numeric columns to a tolerance (default 1e-6 - double
arithmetic in identical order agrees far tighter; the tolerance only absorbs
decimal-formatting differences) and everything else byte-exact. ANY mismatch
is a port bug by definition - there are no acceptable differences.
"""

import argparse
import csv
import json
import sys
from typing import List, Optional

FLOAT_TOL = 1e-6

# columns compared numerically; all others must match as exact strings
NUMERIC_COLS = {
    "open", "high", "low", "close", "volume",
    "ema5", "ema20", "vwap", "roc5", "atr5",
    "stop_px", "target_px",
}


def generate(bars_csv: str, out_dir: str,
             zone_variant: Optional[str] = None,
             spec_root: Optional[str] = None,
             symbol: Optional[str] = None) -> dict:
    from es_backtest import load_bars_csv, run_backtest
    bars = load_bars_csv(bars_csv, symbol=symbol)
    return run_backtest(bars, spec_root=spec_root, zone_variant=zone_variant,
                        out_dir=out_dir, emit_states=True)


def compare(path_a: str, path_b: str,
            tol: float = FLOAT_TOL, max_report: int = 50) -> List[dict]:
    """Row-by-row, column-by-column. Returns mismatches (empty = conformant)."""
    with open(path_a, newline="") as fa, open(path_b, newline="") as fb:
        ra, rb = csv.DictReader(fa), csv.DictReader(fb)
        if ra.fieldnames != rb.fieldnames:
            return [{"row": 0, "col": "<header>",
                     "a": str(ra.fieldnames), "b": str(rb.fieldnames)}]
        mismatches = []
        n = 0
        for i, (a, b) in enumerate(zip(ra, rb), start=1):
            n = i
            for col in ra.fieldnames:
                va, vb = a[col], b[col]
                if va == vb:
                    continue
                if col in NUMERIC_COLS and va and vb:
                    try:
                        if abs(float(va) - float(vb)) <= tol:
                            continue
                    except ValueError:
                        pass
                mismatches.append({"row": i, "col": col, "a": va, "b": vb})
                if len(mismatches) >= max_report:
                    return mismatches
        # unequal lengths are a mismatch, not a truncation to forgive
        extra_a = sum(1 for _ in ra)
        extra_b = sum(1 for _ in rb)
        if extra_a or extra_b:
            mismatches.append({"row": n + 1, "col": "<length>",
                               "a": f"+{extra_a} rows", "b": f"+{extra_b} rows"})
    return mismatches


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("generate")
    g.add_argument("bars_csv")
    g.add_argument("out_dir")
    g.add_argument("--zone", default=None, choices=["off", "grid"])
    g.add_argument("--spec", default=None, choices=["ES", "MES"])
    g.add_argument("--symbol", default=None)

    c = sub.add_parser("compare")
    c.add_argument("golden_csv")
    c.add_argument("candidate_csv")
    c.add_argument("--tol", type=float, default=FLOAT_TOL)

    args = ap.parse_args()
    if args.cmd == "generate":
        summary = generate(args.bars_csv, args.out_dir,
                           zone_variant=args.zone, spec_root=args.spec,
                           symbol=args.symbol)
        print(json.dumps(summary, indent=2))
    else:
        mm = compare(args.golden_csv, args.candidate_csv, tol=args.tol)
        if not mm:
            print("CONFORMANT - 0 mismatches")
            sys.exit(0)
        for m in mm:
            print(f"row {m['row']:>6}  {m['col']:<14} "
                  f"python={m['a']!r}  candidate={m['b']!r}")
        print(f"NOT CONFORMANT - {len(mm)} mismatch(es) shown "
              f"(port may not trade until this prints CONFORMANT)")
        sys.exit(1)


if __name__ == "__main__":
    main()
