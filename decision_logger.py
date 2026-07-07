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
    """Per-session writer — crash-safe BY CONSTRUCTION.

    Every batch (one bar's candidates) is written as a COMPLETE, self-
    terminated gzip member appended to the raw file. There is no long-lived
    compressed stream to finalize, so the bot's deliberate hard exits
    (os._exit at the time stop, watchdog execl) can never leave an
    unterminated member that corrupts everything appended after a restart —
    Python's gzip reader walks concatenated members natively. The header is
    its own member, written synchronously AT CREATION, so no restart window
    can produce a header-less file. Cost: ~20 bytes of member overhead per
    bar. Worth it.

    Failure policy: if a batch fails to write (disk full), it is DROPPED and
    counted — a lost bar of rows is honest; silently re-emitting it next bar
    would double-count and skew every rate drift_report computes."""

    def __init__(self, log_dir: str, date_str: str):
        os.makedirs(log_dir, exist_ok=True)
        self.decisions_path = os.path.join(log_dir, f"decisions_{date_str}.csv.gz")
        self.attempts_path  = os.path.join(log_dir, f"attempts_{date_str}.csv")
        self._date = date_str
        self.dropped_batches = 0

        is_new    = (not os.path.exists(self.decisions_path)
                     or os.path.getsize(self.decisions_path) == 0)
        self._raw = open(self.decisions_path, "ab")
        if is_new:
            hdr = io.StringIO()
            csv.writer(hdr).writerow(DECISION_COLUMNS)
            self._write_member(hdr.getvalue())   # on disk before anything else

        new_attempts = (not os.path.exists(self.attempts_path)
                        or os.path.getsize(self.attempts_path) == 0)
        self._att_f   = open(self.attempts_path, "a", newline="")
        self._att_csv = csv.writer(self._att_f)
        if new_attempts:
            self._att_csv.writerow(ATTEMPT_COLUMNS)
            self._att_f.flush()

    def _write_member(self, text: str):
        """Compress `text` as one complete gzip member (mtime=0 → byte-
        deterministic) and append it with a single write+flush. On failure
        the batch is dropped and counted — never retried (see class doc)."""
        buf = io.BytesIO()
        with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz:
            gz.write(text.encode())
        try:
            self._raw.write(buf.getvalue())
            self._raw.flush()
        except (OSError, ValueError) as e:   # ValueError: closed/broken handle
            self.dropped_batches += 1
            logger.error("Decision batch dropped (write failed: %s) — "
                         "%d dropped so far", e, self.dropped_batches)

    # ── Decisions (one batch per bar) ─────────────────────────────────────────

    def log_candidates(self, bar_time_et: str, rows: List[dict]):
        """One complete gzip member per bar batch — a hard exit at ANY moment
        leaves a fully readable file."""
        buf = io.StringIO()
        w   = csv.writer(buf)
        for r in rows:
            w.writerow([
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
        if rows:
            self._write_member(buf.getvalue())

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
            self._raw.close()
            self._att_f.close()
        except Exception:
            pass


# ── Readers (tolerant of hard-exit truncation, like recorder.py) ─────────────

def read_decisions(path: str) -> Iterator[dict]:
    # zlib.error is in the net: it does NOT subclass OSError, and a corrupt
    # byte inside a member surfaces as zlib.error, not BadGzipFile.
    import zlib
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            yield from csv.DictReader(f)
    except (EOFError, OSError, gzip.BadGzipFile, zlib.error) as e:
        logger.warning("Decision log %s truncated tail (%s) — using intact prefix",
                       path, e)


def read_attempts(path: str) -> Iterator[dict]:
    try:
        with open(path, newline="") as f:
            yield from csv.DictReader(f)
    except OSError:
        return
