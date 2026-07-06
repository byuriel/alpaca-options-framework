"""
Signal engine — combines momentum state + option quote data to decide
entry / exit actions.

Key concepts:
  - Greeks from Alpaca are null for 0DTE, so we compute proxy delta from
    the rolling ratio of option price change to SPY price change.
  - Zone is determined by how close SPY is to the strike.
  - Entry fires only when: valid time window, correct momentum direction,
    option in approach/activation zone, proxy delta trending up, affordable price.
"""

import datetime
import logging
from dataclasses import dataclass, field
from typing import Optional

import config
from momentum import MomentumState

logger = logging.getLogger(__name__)


@dataclass
class Quote:
    symbol:    str
    bid:       float
    ask:       float
    timestamp: datetime.datetime          # exchange timestamp
    recv_monotonic: float = 0.0           # local receive time (time.monotonic) —
                                          # drives the data-staleness kill switch;
                                          # exchange clocks can't be trusted for age

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.ask or self.bid

    @property
    def spread_pct(self) -> float:
        """Bid/ask spread as a fraction of mid; inf when one-sided/crossed."""
        if self.bid > 0 and self.ask > 0 and self.ask >= self.bid:
            return (self.ask - self.bid) / ((self.ask + self.bid) / 2.0)
        return float("inf")


@dataclass
class ProxyDeltaTracker:
    """
    Estimates delta = Δoption_price / ΔSPY_price over the last N seconds.
    Updated each time a new option quote arrives paired with the latest SPY price.
    """
    _prev_option_mid: Optional[float] = field(default=None, repr=False)
    _prev_spy_price:  Optional[float] = field(default=None, repr=False)
    _prev_ts:         Optional[datetime.datetime] = field(default=None, repr=False)

    proxy_delta:  float = 0.0
    delta_rising: bool  = False   # True if proxy_delta increased vs last reading

    def update(self, option_mid: float, spy_price: float, ts: datetime.datetime) -> float:
        if (
            self._prev_option_mid is not None
            and self._prev_spy_price is not None
            and spy_price != self._prev_spy_price
        ):
            d_opt = option_mid - self._prev_option_mid
            d_spy = spy_price  - self._prev_spy_price
            new_delta = d_opt / d_spy if d_spy != 0 else self.proxy_delta

            # Clamp to plausible range [0, 1] for calls, [-1, 0] for puts
            new_delta = max(-1.0, min(1.0, new_delta))

            self.delta_rising = new_delta > self.proxy_delta
            self.proxy_delta  = new_delta

        self._prev_option_mid = option_mid
        self._prev_spy_price  = spy_price
        self._prev_ts         = ts
        return self.proxy_delta


def _zone(spy_price: float, strike: float) -> str:
    """Return proximity zone of SPY price relative to the strike."""
    dist_pct = abs(strike - spy_price) / spy_price
    if dist_pct <= config.ACTIVATION_PCT:
        return "activation"
    if dist_pct <= config.APPROACH_PCT:
        return "approach"
    return "dead"


def _in_entry_window() -> bool:
    now_et = datetime.datetime.now(tz=config.ET).strftime("%H:%M")
    return config.ENTRY_START <= now_et <= config.ENTRY_END


# ── Entry signal ──────────────────────────────────────────────────────────────

def check_entry(
    *,
    side:          str,             # "call" or "put"
    strike:        float,
    option_quote:  Quote,
    momentum:      MomentumState,
    proxy_tracker: ProxyDeltaTracker,
    spy_price:     float,
    trades_today:  int,
    has_open_pos:  bool,
    atr5:          float = 0.0,     # 5-bar ATR at entry bar — used for ATR gate
) -> bool:
    """
    Returns True when all entry conditions are satisfied.
    """
    sym = option_quote.symbol

    if has_open_pos:
        return False
    if trades_today >= config.MAX_TRADES_PER_DAY:
        return False
    if not _in_entry_window():
        return False

    # ATR gate — requires minimum intrabar velocity to support a gamma move
    if atr5 < config.ATR5_MIN_ENTRY:
        logger.debug("SKIP %s | atr5=%.3f below min=%.3f", sym, atr5, config.ATR5_MIN_ENTRY)
        return False

    # Momentum must match the direction of the trade
    required_direction = "bull" if side == "call" else "bear"
    if momentum.direction != required_direction:
        logger.debug("SKIP %s | momentum=%s need=%s", sym, momentum.direction, required_direction)
        return False

    # Option price within affordable range
    price = option_quote.mid
    if price < config.OPTION_MIN_PRICE or price > config.OPTION_MAX_PRICE:
        logger.debug("SKIP %s | price=%.2f outside [%.2f, %.2f]",
                     sym, price, config.OPTION_MIN_PRICE, config.OPTION_MAX_PRICE)
        return False

    # Directionality: option must be OTM and SPY approaching from the correct side.
    # A call entered when SPY >= strike is already ITM — the gamma explosion has passed.
    # A put entered when SPY <= strike is already ITM — same problem.
    if side == "call" and spy_price >= strike:
        logger.debug("SKIP %s | call ITM: spy=%.2f >= strike=%.2f", sym, spy_price, strike)
        return False
    if side == "put" and spy_price <= strike:
        logger.debug("SKIP %s | put ITM: spy=%.2f <= strike=%.2f", sym, spy_price, strike)
        return False

    # Must be in activation zone only — approach zone entries reverse too often
    zone = _zone(spy_price, strike)
    if zone != "activation":
        logger.debug("SKIP %s | zone=%s spy=%.2f strike=%.2f", sym, zone, spy_price, strike)
        return False

    # Proxy delta must be above minimum
    if proxy_tracker.proxy_delta < config.PROXY_DELTA_MIN:
        logger.debug("SKIP %s | proxy_delta=%.3f < min=%.3f",
                     sym, proxy_tracker.proxy_delta, config.PROXY_DELTA_MIN)
        return False

    # Optionally require delta is rising (relaxed in data-collection mode)
    if config.REQUIRE_DELTA_RISING and not proxy_tracker.delta_rising:
        logger.debug("SKIP %s | delta not rising (%.3f)", sym, proxy_tracker.proxy_delta)
        return False

    logger.info(
        "ENTRY signal: side=%s strike=%.2f spy=%.2f zone=%s "
        "proxy_delta=%.3f option_mid=%.2f momentum=%s",
        side, strike, spy_price, zone,
        proxy_tracker.proxy_delta, price, momentum.direction,
    )
    return True


# NOTE: the live exit logic (TP / hard stop / peak trail / SPY-level stop /
# time stop) is implemented in main.py's _evaluate_exit / _evaluate_spy_stop
# and executed through the centralized _execute_exit path. A previous
# check_exit() prototype here referenced config keys that were never added
# (TARGET_1_MULT etc.) and would have crashed if called — removed rather than
# shipped as dead code.
