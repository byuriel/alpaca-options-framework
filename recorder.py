"""
Market-data recorder + reader.

Records every event the DECISION code receives — SPY 1-min bars and option
quotes, in receive order, with receive timestamps — plus one metadata line
(config snapshot, ATR baseline, momentum preseed bars, chain symbols) so a
session can be replayed through the exact same code paths by replay.py.

Design constraints, in order:
  1. NEVER block the event loop. record_*() formats a small tuple and does a
     non-blocking queue put; a daemon thread does all JSON/gzip/disk work.
  2. NEVER drop silently. A full queue increments a counter that is logged
     and stamped into the file; a recording that lost events says so.
  3. Survive the bot's own exit style. The bot hard-exits via os._exit(0) by
     design (documented feed-teardown hang), which skips file close — so the
     writer flushes with gzip sync points every few seconds, and the reader
     tolerates a truncated tail instead of refusing the whole file. Worst
     case a few seconds around the 15:25 exit are lost, after which no
     decisions happen anyway.

Line formats (JSONL, compact arrays — ~40% smaller than dicts at this volume):
  ["m", recv_wall, {metadata...}]
  ["b", recv_wall, bar_ts_iso, open, high, low, close, volume]
  ["q", recv_wall, symbol, bid, ask, exch_ts_iso, bid_size, ask_size]
  ["s", recv_wall, [symbols...]]        # subscription event (open + expansions)

recv_wall is the local receive time (epoch seconds) — the axis replay's
simulated clock runs on. Exchange timestamps are preserved for analysis but
ordering is by receipt, because that is what the live process experienced.

Quote SIZES are captured even though the decision code doesn't use them yet:
displayed size enables a size-aware fill model later (fill only up to the
NBBO size), and size history cannot be retro-captured — every session
recorded without it is permanently lost to that analysis. Readers tolerate
the older 6-field quote rows (sizes default to 0).
"""

import gzip
import json
import logging
import os
import queue
import threading
import time
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

_QUEUE_MAX      = 200_000   # ~40s of extreme quote flow before drops
_FLUSH_INTERVAL = 2.0       # seconds between gzip sync flushes


class MarketDataRecorder:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path      = path
        self._q        = queue.Queue(maxsize=_QUEUE_MAX)
        self._dropped  = 0
        self._stop     = threading.Event()
        # Append mode: a same-day restart continues the same file, and the
        # extra "m" metadata line it writes doubles as a restart marker.
        self._file     = gzip.open(path, "at", encoding="utf-8")
        self._thread   = threading.Thread(
            target=self._writer_loop, daemon=True, name="mdata-recorder")
        self._thread.start()
        logger.info("Market data recorder active: %s", path)

    # ── Producers (event-loop side — must stay allocation-light) ─────────────

    def record_meta(self, meta: dict):
        self._put(["m", time.time(), meta])

    def record_bar(self, ts_iso: str, o: float, h: float, l: float,
                   c: float, v: float):
        self._put(["b", time.time(), ts_iso, o, h, l, c, v])

    def record_quote(self, symbol: str, bid: float, ask: float, exch_ts_iso: str,
                     bid_size: int = 0, ask_size: int = 0):
        self._put(["q", time.time(), symbol, bid, ask, exch_ts_iso,
                   bid_size, ask_size])

    def record_subscription(self, symbols):
        """Subscription events make coverage measurable: subscribed-but-
        never-delivering symbols are how a feed's symbol cap or a dead
        contract shows up (feed_monitor.py)."""
        self._put(["s", time.time(), sorted(symbols)])

    def _put(self, item):
        try:
            self._q.put_nowait(item)
        except queue.Full:
            # Counted, logged (rate-limited by the writer), stamped on close.
            self._dropped += 1

    @property
    def dropped(self) -> int:
        return self._dropped

    # ── Writer thread ─────────────────────────────────────────────────────────

    def _writer_loop(self):
        last_flush   = time.monotonic()
        last_drop_no = 0
        while not (self._stop.is_set() and self._q.empty()):
            try:
                item = self._q.get(timeout=0.5)
            except queue.Empty:
                item = None
            try:
                if item is not None:
                    self._file.write(json.dumps(item, separators=(",", ":")))
                    self._file.write("\n")
                now = time.monotonic()
                if now - last_flush >= _FLUSH_INTERVAL:
                    # gzip sync point — everything before this is readable
                    # even if the process os._exit()s later
                    self._file.flush()
                    last_flush = now
                    if self._dropped > last_drop_no:
                        logger.warning(
                            "Recorder dropped %d events so far (queue full)",
                            self._dropped,
                        )
                        last_drop_no = self._dropped
            except Exception as e:
                logger.error("Recorder write failed: %s", e)
                time.sleep(1)

    def close(self):
        """Best-effort clean close (not guaranteed to run — see module doc)."""
        self._stop.set()
        self._thread.join(timeout=5)
        try:
            if self._dropped:
                self._file.write(json.dumps(
                    ["m", time.time(), {"dropped_events": self._dropped}],
                    separators=(",", ":")) + "\n")
            self._file.close()
        except Exception:
            pass


# ── Reader ─────────────────────────────────────────────────────────────────────

def read_events(path: str) -> Iterator[list]:
    """
    Yield recorded events in file (= receive) order. Tolerates:
      - a truncated gzip tail (os._exit before close) — yields what's intact
      - individual corrupt lines — skipped with a warning count
    """
    corrupt = 0
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    corrupt += 1
    except (EOFError, OSError, gzip.BadGzipFile) as e:
        # Truncated tail from a hard exit — everything already yielded is good
        logger.warning("Recording %s has a truncated tail (%s) — using intact prefix",
                       path, e)
    if corrupt:
        logger.warning("Recording %s: skipped %d corrupt lines", path, corrupt)


def load_session(path: str):
    """
    Read a recording into (meta, events):
      meta   — the FIRST metadata dict (session provenance); later "m" lines
               (restart markers) are folded in for missing keys only.
      events — list of ["b"|"q", ...] rows in receive order.
    """
    meta: Optional[dict] = None
    events = []
    for ev in read_events(path):
        if not ev:
            continue
        if ev[0] == "m":
            if meta is None:
                meta = dict(ev[2])
            else:
                for k, v in ev[2].items():
                    meta.setdefault(k, v)
        elif ev[0] in ("b", "q", "s"):
            events.append(ev)
    return meta or {}, events


def default_recording_path(recordings_dir: str, session_date) -> str:
    return os.path.join(recordings_dir, f"session_{session_date.isoformat()}.jsonl.gz")
