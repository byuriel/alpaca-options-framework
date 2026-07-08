"""
ES exit engine - the premium-space exit stack re-derived in PRICE space.

Nothing implicit survives the port (plan Sec.3). The options position carried
four implicit protections; each gets an explicit price-space replacement:

  premium = max loss        ->  resting stop at entry -/+ max(0.75xatr5, floor)
  theta bleeds dead trades  ->  stagnation exit: no excursion after N bars
  expiry ends the trade     ->  time stop 15:25 ET (unchanged)
  TP/trail on option mid    ->  ATR-multiple target + excursion trail

Two evaluation surfaces, matching how live vs sim actually execute:
  - stop/target are HARD levels: live they are resting broker-side orders
    (Apex requires an attached stop on every order anyway); in the sim they
    fill intrabar (futures_sim owns the conservative ordering rule).
  - trail / stagnation / time are SOFT rules evaluated on bar close - live
    they are alerts the trader acts on; sim fills them at the close.

All functions are pure; FuturesPosition carries the little state there is.
This file is part of the C# conformance spec (ninjatrader/AofCore.cs must
reproduce it bit-for-bit on the golden vectors).
"""

import datetime
from dataclasses import dataclass, field
from typing import Optional

import config
from futures_contracts import round_to_tick


@dataclass
class ExitParams:
    stop_atr_mult:       float = None
    stop_floor_pts:      float = None
    target_atr_mult:     float = None
    trail_arm_atr_mult:  float = None
    trail_giveback:      float = None
    stagnation_bars:     int   = None
    stagnation_atr_frac: float = None
    time_stop:           str   = None     # "HH:MM" ET

    def __post_init__(self):
        # config-backed defaults, overridable per sweep
        if self.stop_atr_mult       is None: self.stop_atr_mult       = config.ES_STOP_ATR_MULT
        if self.stop_floor_pts      is None: self.stop_floor_pts      = config.ES_STOP_FLOOR_PTS
        if self.target_atr_mult     is None: self.target_atr_mult     = config.ES_TARGET_ATR_MULT
        if self.trail_arm_atr_mult  is None: self.trail_arm_atr_mult  = config.ES_TRAIL_ARM_ATR_MULT
        if self.trail_giveback      is None: self.trail_giveback      = config.ES_TRAIL_GIVEBACK
        if self.stagnation_bars     is None: self.stagnation_bars     = config.ES_STAGNATION_BARS
        if self.stagnation_atr_frac is None: self.stagnation_atr_frac = config.ES_STAGNATION_ATR_FRAC
        if self.time_stop           is None: self.time_stop           = config.TIME_STOP


@dataclass
class FuturesPosition:
    side:        str            # "long" | "short"
    entry_price: float
    qty:         int
    atr5_entry:  float          # 1-min atr5 at entry - freezes the exit geometry
    entry_time:  datetime.datetime
    stop_price:   float = 0.0   # set at open from stop_distance()
    target_price: float = 0.0
    # runtime
    peak_favorable: float = 0.0     # best favorable excursion, points, >= 0
    mae_points:     float = 0.0     # worst adverse excursion, points, >= 0
    bars_held:      int   = 0

    def favorable(self, price: float) -> float:
        d = price - self.entry_price
        return d if self.side == "long" else -d

    def update_on_bar(self, high: float, low: float, close: float):
        """Excursion bookkeeping from the bar's extremes - MFE/MAE feed the
        drift report's L4 fork exactly as they do on the options side."""
        hi_fav = self.favorable(high)
        lo_fav = self.favorable(low)
        self.peak_favorable = max(self.peak_favorable, hi_fav, lo_fav, 0.0)
        self.mae_points     = max(self.mae_points, -min(hi_fav, lo_fav, 0.0))
        self.bars_held     += 1


# -- Hard levels (resting orders live; intrabar fills in sim) ------------------

def stop_distance(atr5: float, p: ExitParams) -> float:
    return max(p.stop_atr_mult * atr5, p.stop_floor_pts)


def initial_stop_price(side: str, entry: float, atr5: float,
                       p: ExitParams) -> float:
    d = stop_distance(atr5, p)
    return round_to_tick(entry - d if side == "long" else entry + d)


def target_price(side: str, entry: float, atr5: float, p: ExitParams) -> float:
    d = p.target_atr_mult * atr5
    return round_to_tick(entry + d if side == "long" else entry - d)


# -- Soft rules (bar-close evaluation; alerts live, close-fills in sim) --------

def soft_exit_reason(pos: FuturesPosition, close: float,
                     bar_time_et: datetime.time,
                     p: ExitParams) -> Optional[str]:
    """Priority order mirrors the options stack: the time stop outranks
    everything; trail before stagnation (an armed trail means the trade
    worked - stagnation is for trades that never went anywhere)."""
    hh, mm = map(int, p.time_stop.split(":"))
    if bar_time_et >= datetime.time(hh, mm):
        return "time_stop"

    arm = p.trail_arm_atr_mult * pos.atr5_entry
    if pos.peak_favorable >= arm and arm > 0:
        retrace = pos.peak_favorable - pos.favorable(close)
        if retrace >= p.trail_giveback * pos.peak_favorable:
            return "trail"

    if (pos.bars_held >= p.stagnation_bars
            and pos.peak_favorable < p.stagnation_atr_frac * pos.atr5_entry):
        return "stagnation"

    return None
