"""
Risk manager - position sizing, daily loss gate, and trade cooldown.

Rules (all HARD limits - none can be exceeded by rounding):
  - Size each trade so a full stop-loss hit <= MAX_RISK_PER_TRADE. If even a
    single contract exceeds that, size is 0 and the trade is skipped - the cap
    is never rounded up to "at least 1 contract".
  - Cap total premium outlay at MAX_PREMIUM_PER_TRADE. A long 0DTE option can
    gap through its stop; the true worst case is 100% of premium, so the tail
    loss must be bounded independently of the stop.
  - Block new entries *prospectively* when a full stop-out would breach
    MAX_DAILY_LOSS - not only after the loss is already booked.
  - Enforce a cooldown of TRADE_COOLDOWN_BARS between trades (prevents chasing).
"""

import logging
import math

import config

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self):
        self._daily_pnl:     float = 0.0
        self._trades_today:  int   = 0
        self._locked:        bool  = False   # True = no new entries this session
        self._lock_reason:   str   = ""
        self._cooldown_bars: int   = 0       # bars remaining before next entry allowed
        self._week_pnl_prior: float = 0.0    # realized P&L of EARLIER sessions this
                                             # week (Mon..yesterday) - set at startup
                                             # from the CSV history; week total =
                                             # this + _daily_pnl

    # -- Queries ---------------------------------------------------------------

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def trades_today(self) -> int:
        return self._trades_today

    @property
    def locked(self) -> bool:
        return self._locked

    @property
    def lock_reason(self) -> str:
        return self._lock_reason

    @property
    def week_pnl(self) -> float:
        return self._week_pnl_prior + self._daily_pnl

    def set_week_baseline(self, prior_pnl: float):
        """Realized P&L from this week's earlier sessions (startup restore).
        Locks immediately if the week is already through the limit."""
        self._week_pnl_prior = prior_pnl
        if self.week_pnl <= -config.WEEKLY_MAX_LOSS:
            self.lock(f"weekly loss limit already hit (${self.week_pnl:.2f})")
        elif prior_pnl != 0.0:
            logger.info("Week-to-date baseline: $%.2f (limit -$%.2f)",
                        prior_pnl, config.WEEKLY_MAX_LOSS)

    def can_trade(self) -> bool:
        if self._locked:
            logger.debug("Risk gate LOCKED (%s).", self._lock_reason or "daily loss limit")
            return False
        if self._cooldown_bars > 0:
            logger.debug("In cooldown - %d bars remaining.", self._cooldown_bars)
            return False
        if self._trades_today >= config.MAX_TRADES_PER_DAY:
            logger.debug("Max trades per day reached (%d).", self._trades_today)
            return False
        # Prospective daily-loss check: if this trade stops out at full
        # configured risk, would the day breach the limit? Then don't enter.
        if self._daily_pnl - config.MAX_RISK_PER_TRADE <= -config.MAX_DAILY_LOSS:
            logger.debug(
                "Projected daily loss gate: pnl=%.2f - risk=%.2f would breach -%.2f",
                self._daily_pnl, config.MAX_RISK_PER_TRADE, config.MAX_DAILY_LOSS,
            )
            return False
        # Same prospective logic at the week level - five bad days must not
        # compound past the weekly line either.
        if self.week_pnl - config.MAX_RISK_PER_TRADE <= -config.WEEKLY_MAX_LOSS:
            logger.debug(
                "Projected weekly loss gate: week=%.2f - risk=%.2f would breach -%.2f",
                self.week_pnl, config.MAX_RISK_PER_TRADE, config.WEEKLY_MAX_LOSS,
            )
            return False
        return True

    def lock(self, reason: str):
        """Hard-stop new entries for the rest of the session (kill switches,
        repeated order failures, daily loss limit)."""
        if not self._locked:
            self._locked      = True
            self._lock_reason = reason
            logger.warning("RISK GATE LOCKED: %s - no new entries this session.", reason)

    # -- Cooldown --------------------------------------------------------------

    def tick_bar(self):
        """Call on every 1-min bar to decrement cooldown counter."""
        if self._cooldown_bars > 0:
            self._cooldown_bars -= 1

    def start_cooldown(self):
        self._cooldown_bars = config.TRADE_COOLDOWN_BARS
        logger.info("Cooldown started - %d bars before next entry.", config.TRADE_COOLDOWN_BARS)

    # -- Sizing ----------------------------------------------------------------

    def size_trade(self, entry_price: float) -> int:
        """
        Return the number of contracts, or 0 if the trade cannot be sized
        within the risk limits (0 means: skip the trade).

        Constraints applied, tightest wins:
          stop-basis risk:  qty * entry * (1 - STOP_MULT) * 100 <= MAX_RISK_PER_TRADE
          premium outlay:   qty * entry * 100                   <= MAX_PREMIUM_PER_TRADE
        """
        if entry_price <= 0:
            return 0

        risk_per_contract    = entry_price * (1.0 - config.STOP_MULT) * 100
        premium_per_contract = entry_price * 100

        by_risk    = math.floor(config.MAX_RISK_PER_TRADE / risk_per_contract)
        by_premium = math.floor(config.MAX_PREMIUM_PER_TRADE / premium_per_contract)
        contracts  = min(by_risk, by_premium)

        if contracts < 1:
            logger.info(
                "Sizing REJECTED: entry=%.2f risk/contract=$%.2f premium/contract=$%.2f "
                "exceed limits (risk cap $%.2f, premium cap $%.2f) - trade skipped",
                entry_price, risk_per_contract, premium_per_contract,
                config.MAX_RISK_PER_TRADE, config.MAX_PREMIUM_PER_TRADE,
            )
            return 0

        logger.info(
            "Sizing: entry=%.2f risk/contract=$%.2f -> %d contract(s) "
            "(stop risk $%.2f, premium $%.2f)",
            entry_price, risk_per_contract, contracts,
            contracts * risk_per_contract, contracts * premium_per_contract,
        )
        return contracts

    # -- P&L tracking ---------------------------------------------------------

    def record_trade(self, realized_pnl: float):
        """Call after each trade closes with the net P&L (positive or negative)."""
        self._daily_pnl    += realized_pnl
        self._trades_today += 1
        self.start_cooldown()
        logger.info(
            "Trade recorded. P&L: $%.2f | Daily P&L: $%.2f | Trades today: %d",
            realized_pnl, self._daily_pnl, self._trades_today,
        )
        if self._daily_pnl <= -config.MAX_DAILY_LOSS:
            self.lock(f"daily loss limit hit (${self._daily_pnl:.2f})")
        if self.week_pnl <= -config.WEEKLY_MAX_LOSS:
            self.lock(f"WEEKLY loss limit hit (${self.week_pnl:.2f})")

    def reset_day(self):
        """Call at the start of each new session."""
        self._daily_pnl     = 0.0
        self._trades_today  = 0
        self._locked        = False
        self._lock_reason   = ""
        self._cooldown_bars = 0
        logger.info("Risk manager reset for new session.")

    def restore_day(self, daily_pnl: float, trades_today: int):
        """
        Restore daily counters from a previous run (e.g. after restart).
        Re-evaluates the daily loss lock so risk gates remain correct.
        """
        self._daily_pnl    = daily_pnl
        self._trades_today = trades_today
        if self._daily_pnl <= -config.MAX_DAILY_LOSS:
            self.lock(f"daily loss limit already hit (${self._daily_pnl:.2f})")
        logger.info(
            "Risk counters restored: daily_pnl=$%.2f trades=%d locked=%s",
            self._daily_pnl, self._trades_today, self._locked,
        )
