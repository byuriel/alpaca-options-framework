"""
ES signal engine — the SHARED alpha layer bound to futures bars.

This is deliberately thin: it re-uses MomentumEngine (the exact object the
live SPY bot runs — same EMA recursion, same VWAP anchoring, same streak
logic) and adds only what differs on ES:

  - the velocity gate re-based as a FRACTION of price (config.ES_ATR5_MIN_FRAC)
    instead of absolute SPY points,
  - the zone translation variants from plan §2.3 (default "off": the
    subscription-window analysis showed the strike/zone machinery mostly
    selects WHICH contract, not WHETHER to trade — "grid" replicates the SPY
    geometry ×10 as the parity control),
  - futures gate names (no premium band, no proxy delta, no option spread).

Gate reports never short-circuit — identical policy to signals.GateReport,
for the same reason: per-gate statistics must not be order-dependent lies.

The engine holds NO position or account state beyond what gating needs
(trade count, cooldown). Positions belong to the runner (es_backtest live
shadow / futures_sim); accounts belong to apex_risk. This file is part of
the C# conformance spec (ninjatrader/AofCore.cs).
"""

import datetime
from dataclasses import dataclass, field
from typing import Optional

import config
from momentum import Bar, MomentumEngine, MomentumState

ES_GATE_NAMES = ("capacity", "max_trades", "window", "atr",
                 "momentum", "cooldown", "zone")


@dataclass
class EsDecision:
    bar_time_et: datetime.datetime
    close:       float
    momentum:    MomentumState
    gates:       dict
    zone_dist_pct: Optional[float] = None   # distance to nearest OTM grid level
    entry_side:  Optional[str] = None       # "long"/"short" iff all gates pass

    @property
    def all_pass(self) -> bool:
        return all(self.gates.values())

    @property
    def sole_blocker(self) -> str:
        failed = [n for n, ok in self.gates.items() if not ok]
        return failed[0] if len(failed) == 1 else ""


class EsSignalEngine:
    def __init__(self, zone_variant: Optional[str] = None):
        self.momentum = MomentumEngine()
        self.zone_variant = (zone_variant if zone_variant is not None
                             else config.ES_ZONE_VARIANT)
        if self.zone_variant not in ("off", "grid"):
            raise ValueError(f"unknown zone variant: {self.zone_variant}")
        self.atr5d: Optional[float] = None   # 5-day daily ATR, ES points
        self.trades_today  = 0
        self.bar_index     = -1
        self._last_exit_bar: Optional[int] = None
        self._session: Optional[datetime.date] = None

    # ── Runner hooks ───────────────────────────────────────────────────────────

    def set_daily_atr(self, atr5d: float):
        self.atr5d = atr5d

    def note_entry(self):
        self.trades_today += 1

    def note_exit(self, bar_index: Optional[int] = None):
        self._last_exit_bar = self.bar_index if bar_index is None else bar_index

    def _reset_session(self, d: datetime.date):
        self._session       = d
        self.trades_today   = 0
        self.bar_index      = -1
        self._last_exit_bar = None

    # ── Zone translation (plan §2.3) ───────────────────────────────────────────

    def _grid_zone(self, spot: float, direction: str):
        """Faithful ×10 replication of the SPY machinery's observed
        semantics: 'some subscribed strike on the OTM side sits within
        ACTIVATION_PCT of spot'. Targets recompute every bar from spot and
        the daily-ATR offset, exactly like strikes.compute_dynamic_strikes;
        the ±ES_GRID_ALTS window is the subscription window.

        atr5d is point-in-time (prior sessions only). Until the runner has
        one full prior session the gate FAILS CLOSED — no entries on a
        warmup day beats entries derived from data we couldn't have had."""
        if self.atr5d is None:
            return False, None
        step   = config.ES_GRID_STEP
        offset = min(config.ATR_MULT * self.atr5d, config.ES_GRID_MAX_OFFSET)

        def nearest_otm_dist(side: str) -> Optional[float]:
            target = round(round((spot + offset if side == "long"
                                  else spot - offset) / step) * step, 2)
            best = None
            for i in range(-config.ES_GRID_ALTS, config.ES_GRID_ALTS + 1):
                level = target + i * step
                otm = level > spot if side == "long" else level < spot
                if not otm:
                    continue
                d = abs(level - spot) / spot
                best = d if best is None else min(best, d)
            return best

        sides = (["long"] if direction == "bull"
                 else ["short"] if direction == "bear"
                 else ["long", "short"])
        dists = [d for d in (nearest_otm_dist(s) for s in sides) if d is not None]
        if not dists:
            return False, None
        dist = min(dists)
        return dist <= config.ACTIVATION_PCT, dist

    # ── Per-bar evaluation ─────────────────────────────────────────────────────

    def on_bar(self, bar: Bar, has_open_pos: bool) -> EsDecision:
        bar_et = bar.t.astimezone(config.ET)
        if bar_et.date() != self._session:
            self._reset_session(bar_et.date())
        self.bar_index += 1

        m = self.momentum.on_bar(bar)

        hhmm = bar_et.strftime("%H:%M")
        in_window = config.ENTRY_START <= hhmm <= config.ENTRY_END

        cooldown_ok = (self._last_exit_bar is None
                       or self.bar_index - self._last_exit_bar
                          > config.TRADE_COOLDOWN_BARS)

        if self.zone_variant == "grid":
            zone_ok, zone_dist = self._grid_zone(bar.close, m.direction)
        else:
            zone_ok, zone_dist = True, None

        gates = {
            "capacity":   not has_open_pos,
            "max_trades": self.trades_today < config.MAX_TRADES_PER_DAY,
            "window":     in_window,
            "atr":        m.atr5 >= config.ES_ATR5_MIN_FRAC * bar.close,
            "momentum":   m.direction in ("bull", "bear"),
            "cooldown":   cooldown_ok,
            "zone":       zone_ok,
        }

        side = None
        if all(gates.values()):
            side = "long" if m.direction == "bull" else "short"

        return EsDecision(bar_time_et=bar_et, close=bar.close, momentum=m,
                          gates=gates, zone_dist_pct=zone_dist,
                          entry_side=side)
