"""
Clock-hour ORB directional filter (shadow mode).

Logic:
  - Track each clock-hour window's first-bar high and low (ORH/ORL).
  - When a new hour begins, compare to the prior hour's ORH/ORL:
      both higher → call bias  (bullish regime)
      both lower  → put  bias  (bearish regime)
      mixed       → None       (no restriction)
  - First window of the day has no prior hour → None.

Shadow mode: check_shadow() logs whether a trade would be blocked without
preventing it. Set LIVE_MODE = True in config to activate real blocking.
"""

import datetime
import logging

logger = logging.getLogger(__name__)


class ORBFilter:
    def __init__(self):
        self._hour_bars: dict[int, tuple[float, float]] = {}  # hour → (orh, orl)
        self._bias: str | None = None   # "call", "put", or None

    def reset(self):
        self._hour_bars.clear()
        self._bias = None

    def on_bar(self, bar_time: datetime.time, bar_high: float, bar_low: float):
        """
        Called on every 1-min SPY bar. Records the first bar of each clock
        hour and recomputes directional bias when a new hour window opens.
        """
        hour = bar_time.hour
        if hour in self._hour_bars:
            return   # already recorded the first bar for this hour

        self._hour_bars[hour] = (bar_high, bar_low)

        prev_hour = hour - 1
        if prev_hour not in self._hour_bars:
            new_bias = None   # no prior window — no restriction
        else:
            prev_h, prev_l = self._hour_bars[prev_hour]
            curr_h, curr_l = self._hour_bars[hour]
            if curr_h > prev_h and curr_l > prev_l:
                new_bias = "call"
            elif curr_h < prev_h and curr_l < prev_l:
                new_bias = "put"
            else:
                new_bias = None   # mixed signal — no restriction

        prev_info = (
            f"prev={self._hour_bars[prev_hour][0]:.2f}/{self._hour_bars[prev_hour][1]:.2f}"
            if prev_hour in self._hour_bars else "prev=N/A"
        )
        logger.info(
            "ORB_FILTER: hour=%d → bias=%s | curr=%.2f/%.2f %s",
            hour, new_bias or "NONE", bar_high, bar_low, prev_info,
        )
        self._bias = new_bias

    @property
    def bias(self) -> str | None:
        """Current directional bias: 'call', 'put', or None (no restriction)."""
        return self._bias

    def check_shadow(self, side: str, symbol: str) -> bool:
        """
        Shadow-mode check. Logs BLOCK when the trade conflicts with current bias.
        Always returns True — the trade is never actually prevented in shadow mode.
        Call this after all other entry filters pass so blocked candidates are
        real trades the strategy would have taken.
        """
        allowed = self._bias is None or side == self._bias
        if not allowed:
            logger.info(
                "ORB_SHADOW | BLOCK: %s side=%s bias=%s — trade allowed (shadow mode)",
                symbol, side, self._bias,
            )
        else:
            logger.info(
                "ORB_SHADOW | ALLOW: %s side=%s bias=%s",
                symbol, side, self._bias or "NONE",
            )
        return True   # shadow mode — never blocks
