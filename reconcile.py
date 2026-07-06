#!/usr/bin/env python3
"""
Nightly broker reconciliation — the local trade record vs Alpaca's.

    python reconcile.py                      # today's session
    python reconcile.py --date 2026-07-06
    python reconcile.py --clear              # clear a failure flag after review

An unreconciled track record is a self-published claim; a reconciled one is
evidence. After each session this compares every row in the local trades CSV
against the broker's closed orders, matched BY ORDER ID:

  - every local entry/exit order exists at the broker
  - filled quantity matches exactly (partial exit legs are aggregated per
    entry order before comparison)
  - average fill price matches within PRICE_TOL
  - sides are correct (entries buy, exits sell)
  - broker option orders for the bot's underlying that the CSV does NOT
    know about are surfaced (ghost sweeps, manual interventions — each one
    listed for the operator to classify)

Outcome contract:
  PASS  → exit 0, one summary line.
  FAIL  → exit 1, every discrepancy listed, logs/reconcile_FAIL_<date>.flag
          written, and a CRITICAL log (→ alert, if channels configured).
          At the next startup, main() sees the flag and LOCKS the entry gate
          until the operator investigates and runs --clear. Trading does not
          resume on top of unexplained numbers.

Run it from cron/systemd shortly after the session close (the bot has
hard-exited by then; this is a separate, read-only process).
"""

import argparse
import csv
import datetime
import glob
import logging
import os
import sys
from collections import defaultdict
from typing import List, Optional

import config

logger = logging.getLogger("reconcile")

PRICE_TOL = 0.005   # dollars — avg fill prices are exact in practice; this
                    # tolerates float/decimal representation only
FLAG_GLOB = "reconcile_FAIL_*.flag"


def _flag_path(date) -> str:
    return os.path.join(config.LOG_DIR, f"reconcile_FAIL_{date}.flag")


# ── Local side ─────────────────────────────────────────────────────────────────

def load_local_orders(date_str: str, log_dir: Optional[str] = None):
    """
    Read the session CSV into order-level expectations:
      entries: {order_id: {"symbol", "qty", "price"}}   (legs aggregated)
      exits:   {order_id: {"symbol", "qty", "price"}}
    Rows with sentinel IDs ("recovered") are returned separately — they are
    known-degraded restarts, reported but not failed.
    """
    path = os.path.join(log_dir or config.LOG_DIR, f"trades_{date_str}.csv")
    entries, exits, degraded = {}, {}, []
    if not os.path.exists(path):
        return entries, exits, degraded

    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("date") != date_str or not r.get("realized_pnl"):
                continue
            qty = int(float(r.get("qty", 0) or 0))
            eid, xid = r.get("entry_order_id", ""), r.get("exit_order_id", "")

            if not eid or eid == "recovered":
                degraded.append(r)
            else:
                e = entries.setdefault(eid, {
                    "symbol": r["symbol"], "qty": 0,
                    "price": float(r["entry_price"]),
                })
                e["qty"] += qty          # partial exit legs share the entry order

            if xid:
                x = exits.setdefault(xid, {
                    "symbol": r["symbol"], "qty": 0,
                    "price": float(r["exit_price"]),
                })
                x["qty"] += qty
    return entries, exits, degraded


# ── Broker side ────────────────────────────────────────────────────────────────

def fetch_broker_orders(date_str: str) -> List[dict]:
    """Closed, filled option orders for the bot's underlying on the session
    date, normalized to plain dicts (network boundary kept thin so the
    matching core stays pure and testable)."""
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus, AssetClass

    day   = datetime.date.fromisoformat(date_str)
    start = datetime.datetime.combine(day, datetime.time(0, 0), tzinfo=config.ET)
    end   = start + datetime.timedelta(days=1)

    client = TradingClient(api_key=config.ALPACA_API_KEY,
                           secret_key=config.ALPACA_API_SECRET,
                           paper=config.PAPER)
    orders = client.get_orders(GetOrdersRequest(
        status=QueryOrderStatus.CLOSED, after=start, until=end, limit=500))

    out = []
    for o in orders:
        if o.asset_class != AssetClass.US_OPTION:
            continue
        if not str(o.symbol).startswith(config.UNDERLYING):
            continue
        fq = int(float(o.filled_qty or 0))
        if fq <= 0:
            continue
        out.append({
            "id":     str(o.id),
            "symbol": str(o.symbol),
            "side":   str(o.side.value if hasattr(o.side, "value") else o.side),
            "qty":    fq,
            "price":  float(o.filled_avg_price or 0),
        })
    return out


# ── Matching core (pure — this is what the tests pin) ─────────────────────────

def reconcile(entries: dict, exits: dict, broker_orders: List[dict],
              degraded: Optional[list] = None) -> dict:
    """
    Match local expectations against broker orders by ID. Returns a result
    dict with `ok`, categorized `problems`, and `notes`.
    """
    by_id    = {o["id"]: o for o in broker_orders}
    problems = []
    notes    = []
    matched_ids = set()

    def _check(local: dict, expected_side: str, kind: str):
        for oid, exp in sorted(local.items()):
            b = by_id.get(oid)
            if b is None:
                problems.append(
                    f"{kind} order {oid} ({exp['symbol']} x{exp['qty']} "
                    f"@ {exp['price']:.2f}) NOT FOUND at broker")
                continue
            matched_ids.add(oid)
            if b["symbol"] != exp["symbol"]:
                problems.append(
                    f"{kind} {oid}: symbol mismatch local={exp['symbol']} "
                    f"broker={b['symbol']}")
            if b["side"] != expected_side:
                problems.append(
                    f"{kind} {oid}: side mismatch — expected {expected_side}, "
                    f"broker says {b['side']}")
            if b["qty"] != exp["qty"]:
                problems.append(
                    f"{kind} {oid} ({exp['symbol']}): qty mismatch "
                    f"local={exp['qty']} broker={b['qty']}")
            if abs(b["price"] - exp["price"]) > PRICE_TOL:
                problems.append(
                    f"{kind} {oid} ({exp['symbol']}): price mismatch "
                    f"local={exp['price']:.4f} broker={b['price']:.4f}")

    _check(entries, "buy", "ENTRY")
    _check(exits,  "sell", "EXIT")

    # Broker activity the CSV knows nothing about — ghost sweeps, manual
    # trades, unbooked fills. Every one must be explainable by the operator.
    for o in broker_orders:
        if o["id"] not in matched_ids:
            problems.append(
                f"UNMATCHED broker order {o['id']}: {o['side']} "
                f"{o['symbol']} x{o['qty']} @ {o['price']:.2f} — "
                f"not in the local record (ghost close? manual trade?)")

    for r in degraded or []:
        notes.append(
            f"degraded-recovery row (no verifiable entry order id): "
            f"{r.get('symbol')} x{r.get('qty')} — restart artifact, review once")

    return {
        "ok":        not problems,
        "problems":  problems,
        "notes":     notes,
        "n_local":   len(entries) + len(exits),
        "n_broker":  len(broker_orders),
        "n_matched": len(matched_ids),
    }


# ── Startup gate integration ───────────────────────────────────────────────────

def pending_failure_flag(log_dir: Optional[str] = None) -> Optional[str]:
    """Path of an uncleared reconciliation failure, or None. main() locks
    the entry gate when this is set — trading does not resume on top of
    unexplained numbers."""
    flags = sorted(glob.glob(os.path.join(log_dir or config.LOG_DIR, FLAG_GLOB)))
    return flags[-1] if flags else None


# ── CLI ────────────────────────────────────────────────────────────────────────

def run(date_str: str) -> dict:
    entries, exits, degraded = load_local_orders(date_str)
    broker = fetch_broker_orders(date_str)
    result = reconcile(entries, exits, broker, degraded)

    W = 74
    print("═" * W)
    print(f"  RECONCILIATION — {date_str}   local orders: {result['n_local']}   "
          f"broker orders: {result['n_broker']}   matched: {result['n_matched']}")
    print("─" * W)
    for n in result["notes"]:
        print(f"  NOTE: {n}")
    if result["ok"]:
        print("  ✅ CLEAN — every local order confirmed at the broker, "
              "no unexplained broker activity.")
    else:
        for p in result["problems"]:
            print(f"  ❌ {p}")
        flag = _flag_path(date_str)
        os.makedirs(os.path.dirname(flag), exist_ok=True)
        with open(flag, "w") as f:
            f.write("\n".join(result["problems"]) + "\n")
        print("─" * W)
        print(f"  FAIL flag written: {flag}")
        print("  The bot will LOCK its entry gate at next startup until this is")
        print("  investigated and cleared:  python reconcile.py --clear")
        logger.critical(
            "RECONCILIATION FAILED for %s: %d discrepancies — entry gate will "
            "lock at next startup. %s", date_str, len(result["problems"]),
            "; ".join(result["problems"][:3]),
        )
    print("═" * W)
    return result


def main_cli():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--date", default=config.today_et().isoformat())
    ap.add_argument("--clear", action="store_true",
                    help="clear failure flags after manual review")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if args.clear:
        cleared = 0
        for f in glob.glob(os.path.join(config.LOG_DIR, FLAG_GLOB)):
            os.remove(f)
            cleared += 1
        print(f"Cleared {cleared} reconciliation flag(s).")
        return

    config.validate_credentials()
    result = run(args.date)
    sys.exit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main_cli()
