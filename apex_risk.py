"""
Apex Trader Funding 50K account model - the risk geometry of the ACCOUNT,
not just the trade.

Why this module exists: on a funded prop account the binding constraint is
not the nominal balance, it is the TRAILING THRESHOLD. Apex's trails in
real time on equity peaks INCLUDING UNREALIZED open-trade profit, and stops
trailing (locks) once it reaches start + $100. Two consequences the sizing
and exit logic must respect:

  1. Tradeable capital = headroom above the threshold (starts at $2,500 on
     a 50K), never the balance. All risk fractions are fractions of that.
  2. An open winner that spikes and retraces PERMANENTLY consumes headroom:
     the threshold ratcheted up under the unrealized peak even though no
     profit was banked. Give-back is not free the way it is on a personal
     account - this model meters it (`unrealized_consumption`) so the exit
     sweep can price trail looseness correctly.

Rules encoded (verified against Apex's published rules, July 2026):
  - trailing threshold: min(peak_equity - DD, start + $100), monotonic up
  - breach: equity touches/crosses threshold -> account fails
  - contract scaling: HALF the plan max until EOD balance >= start + DD +
    $100 ($52,600 on 50K); unlocks permanently once reached
  - consistency (soft): best day <= 50% of total profit at payout request -
    violating delays payout, it does not breach the account
  - every order must carry an attached stop (enforced broker-side since
    March 2026) - sizing therefore REQUIRES a stop distance; there is no
    "size first, stop later" path, by construction.

Nothing here rounds a fractional contract up. floor() means a stop too wide
for the budget yields qty 0 - a skipped trade, same rule as risk.size_trade.
"""

import logging
import math
from dataclasses import dataclass, field
from typing import List, Optional

import config
from futures_contracts import SPECS, ContractSpec

logger = logging.getLogger(__name__)


@dataclass
class ApexState:
    balance:      float      # realized (closed-trade) balance
    peak_equity:  float      # high-water mark, INCLUDING unrealized marks
    threshold:    float      # current trailing threshold (monotonic up)
    scaling_unlocked: bool = False
    # analytics
    unrealized_consumption: float = 0.0   # threshold rise caused by marks
                                          # above realized balance
    breached: bool = False


class ApexAccount:
    """Deterministic account simulator + live sizing oracle."""

    def __init__(self,
                 start_balance: float = None,
                 trailing_dd:   float = None,
                 lock_buffer:   float = None,
                 max_minis:     int   = None):
        self.start   = start_balance if start_balance is not None else config.APEX_START_BALANCE
        self.dd      = trailing_dd   if trailing_dd   is not None else config.APEX_TRAILING_DD
        self.lock    = lock_buffer   if lock_buffer   is not None else config.APEX_LOCK_BUFFER
        self.max_minis = max_minis   if max_minis     is not None else config.APEX_MAX_MINIS
        self.s = ApexState(
            balance     = self.start,
            peak_equity = self.start,
            threshold   = self.start - self.dd,
        )

    # -- Threshold mechanics ----------------------------------------------------

    def _threshold_for_peak(self, peak: float) -> float:
        return min(peak - self.dd, self.start + self.lock)

    def mark_equity(self, equity: float) -> bool:
        """Feed EVERY equity mark through here - bar closes AND intrabar
        favorable excursions while holding. Returns True on breach.

        The unrealized-consumption meter: any threshold rise that happens
        while equity > realized balance was caused by an open trade's mark,
        i.e. headroom consumed without banking a dollar."""
        s = self.s
        if equity > s.peak_equity:
            new_thr = self._threshold_for_peak(equity)
            if new_thr > s.threshold:
                if equity > s.balance:
                    s.unrealized_consumption += new_thr - s.threshold
                s.threshold = new_thr
            s.peak_equity = equity
        if equity <= s.threshold:
            s.breached = True
            logger.error("APEX BREACH: equity %.2f <= threshold %.2f",
                         equity, s.threshold)
        return s.breached

    def book_realized(self, pnl: float) -> bool:
        """Close a trade: move realized balance and mark equity."""
        self.s.balance += pnl
        return self.mark_equity(self.s.balance)

    def end_of_day(self):
        """EOD hooks: the contract-scaling unlock is an EOD-balance test."""
        if (config.APEX_HALF_UNTIL_NET
                and self.s.balance >= self.start + self.dd + self.lock):
            if not self.s.scaling_unlocked:
                logger.info("Apex safety net reached (EOD balance %.2f) - "
                            "full contract size unlocked", self.s.balance)
            self.s.scaling_unlocked = True

    # -- Derived quantities -----------------------------------------------------

    def headroom(self, equity: Optional[float] = None) -> float:
        eq = self.s.balance if equity is None else equity
        return max(0.0, eq - self.s.threshold)

    def contracts_cap(self, spec: ContractSpec) -> int:
        cap = self.max_minis if spec.root == "ES" else self.max_minis * 10
        if config.APEX_HALF_UNTIL_NET and not self.s.scaling_unlocked:
            cap = cap // 2
        return cap

    # -- Sizing (requires a stop - no stop, no size, by construction) ----------

    def size_trade(self, stop_ticks: int, spec: ContractSpec,
                   equity: Optional[float] = None) -> int:
        """floor(min(fixed budget, frac x headroom) / $-risk-per-contract),
        capped by the scaling rule. 0 = skip (never round up)."""
        if self.s.breached or stop_ticks <= 0:
            return 0
        budget = min(config.APEX_RISK_PER_TRADE,
                     config.APEX_RISK_HEADROOM_FRAC * self.headroom(equity))
        per_contract = stop_ticks * spec.tick_value
        qty = math.floor(budget / per_contract) if per_contract > 0 else 0
        return max(0, min(qty, self.contracts_cap(spec)))

    # -- Prospective loss gates (same philosophy as risk.py: gate BEFORE) ------

    def daily_loss_limit(self) -> float:
        return min(config.APEX_DAILY_LOSS_CAP,
                   config.APEX_DAILY_LOSS_FRAC * self.headroom())

    def weekly_loss_limit(self) -> float:
        return min(config.APEX_WEEKLY_LOSS_CAP,
                   config.APEX_WEEKLY_LOSS_FRAC * self.headroom())

    def can_open(self, next_trade_risk: float,
                 realized_today: float, realized_week: float) -> bool:
        """Prospective: would losing the NEXT trade in full cross a limit?"""
        if self.s.breached:
            return False
        if realized_today - next_trade_risk < -self.daily_loss_limit():
            return False
        if realized_week - next_trade_risk < -self.weekly_loss_limit():
            return False
        return True

    # -- Consistency rule (soft: payout eligibility, not account survival) -----

    @staticmethod
    def consistency_ok(day_pnls: List[float],
                       pct: Optional[float] = None) -> bool:
        pct = config.APEX_CONSISTENCY_PCT if pct is None else pct
        total = sum(p for p in day_pnls)
        if total <= 0:
            return True          # nothing to pay out; rule moot
        best = max((p for p in day_pnls), default=0.0)
        return best <= pct * total

    @staticmethod
    def soft_daily_profit_cap(total_prior_profit: float,
                              pct: Optional[float] = None) -> float:
        """Largest profit today that keeps the consistency rule satisfied:
        d <= pct-(P + d)  ->  d <= pct-P/(1-pct). At 50% that is simply P -
        never make more in one day than everything banked before it."""
        pct = config.APEX_CONSISTENCY_PCT if pct is None else pct
        if total_prior_profit <= 0 or pct >= 1.0:
            return 0.0 if total_prior_profit <= 0 else float("inf")
        return pct * total_prior_profit / (1.0 - pct)
