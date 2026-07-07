"""
FuturesSimBroker — linear fills for the ES sibling, with the same "never
flatter yourself" fill philosophy as sim_broker.py on the options side.

Fill rules (all conservative, all swept in sensitivity analysis, plan §8):
  entry:  marketable order modeled at bar close + ES_ENTRY_SLIP_TICKS adverse
  stop:   fills THROUGH the stop by ES_STOP_SLIP_TICKS when the bar's range
          touches it (low ≤ stop for longs) — stops are resting orders live,
          so they fill intrabar, not at the close
  target: resting limit; fills at the limit ONLY if the bar trades THROUGH
          it (high ≥ target + one tick for longs) — a touch is never assumed
          to fill
  both in one bar: STOP FIRST. Intrabar ordering is unknowable from bars;
          the pessimistic ordering is the only defensible default.
  soft exits (trail/stagnation/time/event): fill at bar close + slip —
          these are close-evaluated decisions executed with a market order.

Commissions: all-in per side per contract (config.ES_COMMISSION_PER_SIDE),
charged on the round turn at close.
"""

from typing import Optional, Tuple

import config
from futures_contracts import SPECS, ContractSpec, round_to_tick
from futures_exits import FuturesPosition


class FuturesSimBroker:
    def __init__(self, spec_root: Optional[str] = None,
                 entry_slip_ticks: Optional[int] = None,
                 stop_slip_ticks: Optional[int] = None,
                 commission_per_side: Optional[float] = None):
        root = spec_root or config.ES_SPEC_ROOT
        self.spec: ContractSpec = SPECS[root]
        self.entry_slip = (entry_slip_ticks if entry_slip_ticks is not None
                           else config.ES_ENTRY_SLIP_TICKS)
        self.stop_slip = (stop_slip_ticks if stop_slip_ticks is not None
                          else config.ES_STOP_SLIP_TICKS)
        self.commission = (commission_per_side if commission_per_side is not None
                           else config.ES_COMMISSION_PER_SIDE[root])

    # ── Fills ──────────────────────────────────────────────────────────────────

    def entry_fill(self, side: str, bar_close: float) -> float:
        slip = self.entry_slip * self.spec.tick_size
        return round_to_tick(bar_close + slip if side == "long"
                             else bar_close - slip)

    def close_fill(self, side: str, bar_close: float) -> float:
        """Market-out for soft exits: adverse slip on the way out too."""
        slip = self.entry_slip * self.spec.tick_size
        return round_to_tick(bar_close - slip if side == "long"
                             else bar_close + slip)

    def check_hard_exits(self, pos: FuturesPosition, high: float,
                         low: float) -> Optional[Tuple[float, str]]:
        """Intrabar stop/target against the bar's range. Stop first."""
        t = self.spec.tick_size
        if pos.side == "long":
            if low <= pos.stop_price:
                return round_to_tick(pos.stop_price - self.stop_slip * t), "stop"
            if high >= pos.target_price + t:
                return pos.target_price, "target"
        else:
            if high >= pos.stop_price:
                return round_to_tick(pos.stop_price + self.stop_slip * t), "stop"
            if low <= pos.target_price - t:
                return pos.target_price, "target"
        return None

    # ── Accounting ─────────────────────────────────────────────────────────────

    def round_turn_commission(self, qty: int) -> float:
        return 2.0 * self.commission * qty

    def pnl_usd(self, pos: FuturesPosition, exit_price: float) -> float:
        pts = pos.favorable(exit_price)
        return pts * self.spec.point_value * pos.qty - self.round_turn_commission(pos.qty)

    def unrealized_usd(self, pos: FuturesPosition, price: float) -> float:
        """Gross mark — commissions hit at close only."""
        return pos.favorable(price) * self.spec.point_value * pos.qty
