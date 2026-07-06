"""
Global mutable state for the bot session.

Keeps track of:
  - Current open position (at most one at a time in Phase 0)
  - Latest SPY price (updated from stock stream)
  - Latest option quotes (updated from option stream, keyed by symbol)
  - Proxy delta trackers per symbol
  - Append-only CSV trade log (net of fees, with order IDs and
    decision-vs-fill slippage so the record is auditable)
  - Position metadata persisted to logs/position_state.json so a restart
    recovers the REAL entry time/price/SPY/atr5 instead of approximations
    (a REST-only recovery loses all of that: the SPY stop gets disabled and
    TP/stop/trail run off Alpaca's day-average basis)
"""

import csv
import datetime
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, Optional

import config
from signals import ProxyDeltaTracker, Quote

logger = logging.getLogger(__name__)

POSITION_STATE_FILE = os.path.join(config.LOG_DIR, "position_state.json")

CSV_COLUMNS = [
    "date", "symbol", "side", "strike",
    "entry_price", "exit_price", "qty", "reason",
    "realized_pnl", "entry_time", "exit_time",
    # audit trail — reconciliation against broker statements needs order IDs;
    # slippage needs the decision-time quote next to the fill
    "fees", "entry_order_id", "exit_order_id",
    "entry_bid", "entry_ask", "exit_bid", "exit_ask",
    "entry_slippage", "exit_slippage", "entry_spy",
]


@dataclass
class Position:
    symbol:        str
    side:          str            # "call" or "put"
    strike:        float
    qty:           int
    entry_price:   float          # option fill price
    entry_time:    datetime.datetime
    order_id:      str
    qty_remaining:   int   = 0
    peak_mid:        float = 0.0   # highest option mid seen — for peak trailing stop
    min_unreal_pnl:  float = 0.0   # most negative unrealized P&L seen since entry
    max_unreal_pnl:  float = 0.0   # most positive unrealized P&L seen since entry
    entry_spy_price: float = 0.0   # SPY bar-close price at entry — for SPY-level stop
    entry_atr5:      float = 0.0   # atr5 at entry — scales the SPY-level stop buffer
    entry_bid:       float = 0.0   # decision-time quote — for slippage measurement
    entry_ask:       float = 0.0
    booked_pnl:      float = 0.0   # net P&L booked so far across partial exit legs —
                                   # record_trade() fires ONCE per position with this
                                   # total when the last leg closes, so a position
                                   # closed in pieces is still one trade

    def __post_init__(self):
        if self.qty_remaining == 0:
            self.qty_remaining = self.qty
        if self.peak_mid == 0.0:
            self.peak_mid = self.entry_price


class BotState:
    def __init__(self):
        self.spy_price:    float = 0.0
        self.position:     Optional[Position] = None
        self.exit_pending: bool = False   # True while a close_position order is in-flight
        self.entry_pending: bool = False  # True from entry decision until the position is
                                          # fully tracked locally (NOT just until the order
                                          # returns) — blocks concurrent entries AND tells
                                          # the ghost sweeper a fill may exist on Alpaca
                                          # that local state doesn't know about yet

        # Latest quotes per option symbol  {symbol: Quote}
        self.option_quotes: Dict[str, Quote] = {}
        self.last_bar_monotonic: float = 0.0   # loop-clock time of last SPY bar (staleness)

        # Proxy delta tracker per symbol
        self.delta_trackers: Dict[str, ProxyDeltaTracker] = {}

        today = config.today_et().isoformat()
        self._log_path = os.path.join(config.LOG_DIR, f"trades_{today}.csv")
        self._ensure_log()

    # ── Quote helpers ─────────────────────────────────────────────────────────

    def update_option_quote(self, symbol: str, bid: float, ask: float, ts: datetime.datetime):
        quote = Quote(
            symbol=symbol, bid=bid, ask=ask, timestamp=ts,
            recv_monotonic=time.monotonic(),
        )
        self.option_quotes[symbol] = quote

        if self.spy_price > 0:
            tracker = self.delta_trackers.setdefault(symbol, ProxyDeltaTracker())
            tracker.update(quote.mid, self.spy_price, ts)

    def get_quote(self, symbol: str) -> Optional[Quote]:
        return self.option_quotes.get(symbol)

    def get_tracker(self, symbol: str) -> ProxyDeltaTracker:
        return self.delta_trackers.setdefault(symbol, ProxyDeltaTracker())

    # ── Position helpers ──────────────────────────────────────────────────────

    def open_position(self, pos: Position):
        if self.position is not None:
            logger.error("Attempted to open a second position while one is already open.")
            return
        self.position = pos
        self._persist_position()
        logger.info("Position opened: %s", pos)

    def book_exit_fill(
        self,
        exit_price:    float,
        reason:        str,
        qty:           Optional[int] = None,
        exit_order_id: str   = "",
        exit_bid:      float = 0.0,
        exit_ask:      float = 0.0,
    ) -> float:
        """
        Book a CONFIRMED exit fill (full or partial) and update tracking.
        Only call this with a real fill price — a failed close must keep the
        position tracked and retry, never book a guess into the permanent
        record. A partial fill decrements qty_remaining and keeps the
        position open; the last fill clears it. Returns P&L net of fees.
        """
        if self.position is None:
            return 0.0
        pos = self.position
        qty = pos.qty_remaining if qty is None else min(qty, pos.qty_remaining)
        if qty <= 0:
            return 0.0
        fees         = config.FEES_PER_CONTRACT_RT * qty
        gross_pnl    = (exit_price - pos.entry_price) * qty * 100
        realized_pnl = gross_pnl - fees
        self._log_trade(pos, exit_price, reason, realized_pnl, fees,
                        exit_order_id, exit_bid, exit_ask, qty)
        pos.booked_pnl    += realized_pnl
        pos.qty_remaining -= qty
        if pos.qty_remaining <= 0:
            self.position     = None
            self.exit_pending = False
            self._clear_persisted_position()
        else:
            self._persist_position()
            logger.warning(
                "PARTIAL close booked: %s %d contracts remain tracked",
                pos.symbol, pos.qty_remaining,
            )
        logger.info(
            "Exit booked: symbol=%s reason=%s qty=%d exit=%.2f gross=$%.2f fees=$%.2f net=$%.2f",
            pos.symbol, reason, qty, exit_price, gross_pnl, fees, realized_pnl,
        )
        return realized_pnl

    # ── Position persistence (restart recovery) ───────────────────────────────

    def _persist_position(self):
        """Write position metadata to disk so a restart recovers the real
        entry context (time, price, SPY level, atr5) instead of approximating
        it from Alpaca's day-average cost basis."""
        pos = self.position
        if pos is None:
            return
        try:
            payload = {
                "symbol":          pos.symbol,
                "side":            pos.side,
                "strike":          pos.strike,
                "qty":             pos.qty,
                "qty_remaining":   pos.qty_remaining,
                "entry_price":     pos.entry_price,
                "entry_time":      pos.entry_time.isoformat(),
                "order_id":        pos.order_id,
                "entry_spy_price": pos.entry_spy_price,
                "entry_atr5":      pos.entry_atr5,
                "entry_bid":       pos.entry_bid,
                "entry_ask":       pos.entry_ask,
                "peak_mid":        pos.peak_mid,
            }
            tmp = POSITION_STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f)
            os.replace(tmp, POSITION_STATE_FILE)   # atomic — no torn state file
        except OSError as e:
            logger.warning("Could not persist position state: %s", e)

    def _clear_persisted_position(self):
        try:
            os.remove(POSITION_STATE_FILE)
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("Could not remove position state file: %s", e)

    @staticmethod
    def load_persisted_position() -> Optional[dict]:
        """Read persisted position metadata, if any. The caller must still
        cross-check against Alpaca REST — the broker is authoritative for
        WHETHER a position exists; the state file is authoritative for its
        entry context."""
        try:
            with open(POSITION_STATE_FILE) as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Position state file unreadable (%s) — ignoring.", e)
            return None

    # ── Logging ───────────────────────────────────────────────────────────────

    def _ensure_log(self):
        os.makedirs(config.LOG_DIR, exist_ok=True)
        if not os.path.exists(self._log_path):
            with open(self._log_path, "w", newline="") as f:
                csv.writer(f).writerow(CSV_COLUMNS)

    def _log_trade(
        self,
        pos:           Position,
        exit_price:    float,
        reason:        str,
        realized_pnl:  float,
        fees:          float,
        exit_order_id: str,
        exit_bid:      float,
        exit_ask:      float,
        qty:           int,
    ):
        entry_mid = (pos.entry_bid + pos.entry_ask) / 2 if pos.entry_bid and pos.entry_ask else 0.0
        exit_mid  = (exit_bid + exit_ask) / 2 if exit_bid and exit_ask else 0.0
        with open(self._log_path, "a", newline="") as f:
            csv.writer(f).writerow([
                config.today_et().isoformat(),
                pos.symbol,
                pos.side,
                pos.strike,
                pos.entry_price,
                exit_price,
                qty,
                reason,
                f"{realized_pnl:.2f}",
                pos.entry_time.isoformat(),
                datetime.datetime.now(tz=config.ET).isoformat(),
                f"{fees:.2f}",
                pos.order_id,
                exit_order_id,
                pos.entry_bid, pos.entry_ask,
                exit_bid, exit_ask,
                # slippage: paid-vs-decision-mid on entry, received-vs-decision-mid on exit
                f"{(pos.entry_price - entry_mid):.4f}" if entry_mid else "",
                f"{(exit_price - exit_mid):.4f}" if exit_mid else "",
                pos.entry_spy_price,
            ])
