"""
Structured decision log — everything the bot SAW AND CONSIDERED, not just
what it did.

The diagnostic problem this solves: when performance deviates, P&L only says
THAT the strategy changed; the decision log says WHERE in the funnel:

    market conditions → signal gates → attempts/fills → realization → P&L

Four different deaths look identical in the P&L and need four different
responses — signals stopped firing (regime moved), gates started blocking
(a filter went stale), fills stopped happening (execution/competition), or
winners became losers (edge repriced). Only per-candidate gate verdicts,
attempt records, and excursion columns distinguish them. drift_report.py is
the comparator that reads all of this.

Two files per session, in config.LOG_DIR:

  decisions_YYYY-MM-DD.csv.gz — one row per (bar, candidate symbol): every
    gate's verdict (never short-circuited — see signals.GateReport), the
    sole blocker, and the full market/momentum context. ~26 symbols × 390
    bars ≈ 10k rows/day, a few hundred KB gzipped.

  attempts_YYYY-MM-DD.csv — one row per order attempt: decision price,
    limit, outcome (filled/partial/unfilled/error), fill price, wait time.
    Failed attempts are data, not log noise — fill-rate decay is an
    execution-regime change with its own fix.

Because these are written from the SHARED code path (the bar handler and
entry path that replay drives), replay emits identical decision logs into
its output directory — so decision history is REGENERABLE for every session
ever recorded. The baseline for drift analysis exists on day one.

Determinism: gzip members are written with mtime=0, so the same session
(live recording replayed, or replay re-run) produces byte-identical logs.
Truncation: the bot hard-exits by design; a sync-flush per bar plus a
tolerant reader (same policy as recorder.py) bounds loss to seconds.
"""

import csv
import gzip
import io
import logging
import os
from typing import Iterator, List, Optional

import config
from signals import GATE_NAMES

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

DECISION_COLUMNS = [
    "schema", "date", "bar_time_et", "symbol", "side", "strike",
    "spy", "zone_dist_pct", "bid", "ask", "mid", "spread_pct", "quote_age_s",
    "direction", "ema5", "ema20", "vwap", "roc5", "atr5", "consec",
    *[f"g_{name}" for name in GATE_NAMES],
    "strategy_pass", "all_pass", "sole_blocker",
    "in_position", "entry_pending", "risk_ok", "blackout", "event_day",
]

ATTEMPT_COLUMNS = [
    "schema", "date", "time_et", "symbol", "side", "qty_requested",
    "decision_bid", "decision_ask", "decision_mid", "limit_px",
    "outcome",            # filled | partial | unfilled | error
    "fill_px", "filled_qty", "wait_s", "order_id",
]


def _f(v, nd=4):
    """Fixed-precision float formatting — determinism requires that the same
    value always serializes to the same string."""
    return "" if v is None else f"{v:.{nd}f}"


class DecisionLogger:
    """Per-session writer. Buffered in memory, flushed once per bar batch —
    ~1 write/min, negligible even on the event loop."""

    def __init__(self, log_dir: str, date_str: str):
        os.makedirs(log_dir, exist_ok=True)
        self.decisions_path = os.path.join(log_dir, f"decisions_{date_str}.csv.gz")
        self.attempts_path  = os.path.join(log_dir, f"attempts_{date_str}.csv")
        self._date = date_str

        new_decisions = not os.path.exists(self.decisions_path)
        raw = open(self.decisions_path, "ab")
        # mtime=0 → byte-deterministic output for identical inputs
        self._gz  = gzip.GzipFile(filename="", mode="ab", fileobj=raw, mtime=0)
        self._buf = io.StringIO()
        self._csv = csv.writer(self._buf)
        if new_decisions:
            self._csv.writerow(DECISION_COLUMNS)

        new_attempts = not os.path.exists(self.attempts_path)
        self._att_f   = open(self.attempts_path, "a", newline="")
        self._att_csv = csv.writer(self._att_f)
        if new_attempts:
            self._att_csv.writerow(ATTEMPT_COLUMNS)
            self._att_f.flush()

    # ── Decisions (one batch per bar) ─────────────────────────────────────────

    def log_candidates(self, bar_time_et: str, rows: List[dict]):
        """rows: dicts from build_candidate_row(). Batched write + sync flush
        so a hard exit loses at most the current bar."""
        for r in rows:
            self._csv.writerow([
                SCHEMA_VERSION, self._date, bar_time_et,
                r["symbol"], r["side"], _f(r["strike"], 2),
                _f(r["spy"], 2), _f(r["zone_dist_pct"], 5),
                _f(r["bid"], 2), _f(r["ask"], 2), _f(r["mid"], 4),
                _f(r["spread_pct"], 4), _f(r["quote_age_s"], 1),
                r["direction"], _f(r["ema5"], 3), _f(r["ema20"], 3),
                _f(r["vwap"], 3), _f(r["roc5"], 5), _f(r["atr5"], 3),
                r["consec"],
                *[int(r["gates"][name]) for name in GATE_NAMES],
                int(r["strategy_pass"]), int(r["all_pass"]), r["sole_blocker"],
                int(r["in_position"]), int(r["entry_pending"]),
                int(r["risk_ok"]), int(bool(r["blackout"])), int(r["event_day"]),
            ])
        self._flush_decisions()

    def _flush_decisions(self):
        data = self._buf.getvalue()
        if data:
            self._gz.write(data.encode())
            self._gz.flush()          # gzip sync point — readable up to here
            self._buf.seek(0)
            self._buf.truncate()

    # ── Attempts ──────────────────────────────────────────────────────────────

    def log_attempt(self, *, time_et: str, symbol: str, side: str,
                    qty_requested: int, decision_bid: float,
                    decision_ask: float, decision_mid: float, limit_px: float,
                    outcome: str, fill_px: Optional[float],
                    filled_qty: int, wait_s: float, order_id: str):
        self._att_csv.writerow([
            SCHEMA_VERSION, self._date, time_et, symbol, side, qty_requested,
            _f(decision_bid, 2), _f(decision_ask, 2), _f(decision_mid, 4),
            _f(limit_px, 2), outcome, _f(fill_px, 4), filled_qty,
            _f(wait_s, 2), order_id,
        ])
        self._att_f.flush()

    def close(self):
        try:
            self._flush_decisions()
            self._gz.close()
            self._att_f.close()
        except Exception:
            pass


# ── Readers (tolerant of hard-exit truncation, like recorder.py) ─────────────

def read_decisions(path: str) -> Iterator[dict]:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            yield from csv.DictReader(f)
    except (EOFError, OSError, gzip.BadGzipFile) as e:
        logger.warning("Decision log %s truncated tail (%s) — using intact prefix",
                       path, e)


def read_attempts(path: str) -> Iterator[dict]:
    try:
        with open(path, newline="") as f:
            yield from csv.DictReader(f)
    except OSError:
        return
