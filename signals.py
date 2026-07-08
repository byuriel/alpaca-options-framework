"""
Signal engine - combines momentum state + option quote data to decide
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

import clock
import config
from momentum import MomentumState

logger = logging.getLogger(__name__)


@dataclass
class Quote:
    symbol:    str
    bid:       float
    ask:       float
    timestamp: datetime.datetime          # exchange timestamp
    recv_monotonic: float = 0.0           # local receive time (time.monotonic) -
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
    Estimates delta = deltaoption_price / deltaSPY_price over the last N seconds.
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
    # clock, not datetime.now(): the entry window must follow replay's
    # simulated session time, not the machine running the replay
    now_et = clock.now_et().strftime("%H:%M")
    return config.ENTRY_START <= now_et <= config.ENTRY_END


# -- Entry signal --------------------------------------------------------------

# Fixed gate order - the decision log's schema and every diagnostic report
# key off these names. Strategy gates first, execution-quality gates last.
GATE_NAMES = (
    "capacity",    # not already holding a position
    "max_trades",  # daily trade-count cap
    "window",      # inside the entry time window
    "atr",         # minimum intrabar velocity (atr5)
    "momentum",    # direction matches side
    "price",       # premium inside [OPTION_MIN_PRICE, OPTION_MAX_PRICE]
    "otm",         # SPY on the correct (OTM) side of the strike
    "zone",        # inside the activation zone
    "delta_min",   # proxy delta above floor
    "delta_rising",  # proxy delta rising (when required)
    "fresh",       # quote age within ENTRY_QUOTE_MAX_AGE_SEC (execution)
    "spread",      # bid/ask spread within ENTRY_MAX_SPREAD_PCT (execution)
)
_EXECUTION_GATES = ("fresh", "spread")


@dataclass
class GateReport:
    """
    Verdict of EVERY entry gate for one candidate - no short-circuiting.

    Why all gates always evaluate: diagnosis. If evaluation stopped at the
    first failure, gate-failure statistics would be order-dependent lies -
    "momentum failures rose" could really mean "everything degraded but
    momentum is checked first". The trade decision is still the AND of all
    gates (identical semantics); only the *information* is richer.
    """
    gates: dict                      # name -> bool (True = pass)
    zone:  str = ""
    mid:   float = 0.0

    @property
    def strategy_pass(self) -> bool:
        """All strategy gates pass (execution-quality gates excluded) -
        this is the historical check_entry() semantics."""
        return all(v for k, v in self.gates.items() if k not in _EXECUTION_GATES)

    @property
    def all_pass(self) -> bool:
        return all(self.gates.values())

    @property
    def sole_blocker(self) -> str:
        """The single most actionable diagnostic: if EXACTLY one gate failed,
        it alone stood between this candidate and a trade. A gate whose
        sole-blocker rate shifts is the binding constraint that changed."""
        failed = [k for k, v in self.gates.items() if not v]
        return failed[0] if len(failed) == 1 else ""


def evaluate_entry_gates(
    *,
    side:          str,             # "call" or "put"
    strike:        float,
    option_quote:  Quote,
    momentum:      MomentumState,
    proxy_tracker: ProxyDeltaTracker,
    spy_price:     float,
    trades_today:  int,
    has_open_pos:  bool,
    atr5:          float = 0.0,     # 5-bar ATR at entry bar - used for ATR gate
    quote_age_s:   Optional[float] = None,   # None -> freshness unknown, passes
) -> GateReport:
    """
    Evaluate ALL entry gates (strategy + execution-quality) for one
    candidate. Pure - no logging, no side effects; the same function serves
    the live entry path, the per-bar decision logger, and replay.
    """
    price = option_quote.mid
    zone  = _zone(spy_price, strike)
    required_direction = "bull" if side == "call" else "bear"

    gates = {
        "capacity":   not has_open_pos,
        "max_trades": trades_today < config.MAX_TRADES_PER_DAY,
        "window":     _in_entry_window(),
        "atr":        atr5 >= config.ATR5_MIN_ENTRY,
        "momentum":   momentum.direction == required_direction,
        "price":      config.OPTION_MIN_PRICE <= price <= config.OPTION_MAX_PRICE,
        "otm":        (spy_price < strike) if side == "call" else (spy_price > strike),
        "zone":       zone == "activation",
        "delta_min":  proxy_tracker.proxy_delta >= config.PROXY_DELTA_MIN,
        "delta_rising": (proxy_tracker.delta_rising
                         if config.REQUIRE_DELTA_RISING else True),
        # Execution-quality gates (previously inline in main._evaluate_entry -
        # one source of truth now):
        "fresh":      not (quote_age_s is not None
                           and quote_age_s > config.ENTRY_QUOTE_MAX_AGE_SEC),
        "spread":     option_quote.spread_pct <= config.ENTRY_MAX_SPREAD_PCT,
    }
    return GateReport(gates=gates, zone=zone, mid=price)


def check_entry(**kwargs) -> bool:
    """
    Historical boolean interface - all STRATEGY conditions satisfied.
    (Execution-quality gates are applied by the entry path via the full
    GateReport.) Kept as the stable strategy-swap interface.
    """
    return evaluate_entry_gates(**kwargs).strategy_pass


# NOTE: the live exit logic (TP / hard stop / peak trail / SPY-level stop /
# time stop) is implemented in main.py's _evaluate_exit / _evaluate_spy_stop
# and executed through the centralized _execute_exit path. A previous
# check_exit() prototype here referenced config keys that were never added
# (TARGET_1_MULT etc.) and would have crashed if called - removed rather than
# shipped as dead code.
