"""
Deterministic simulated broker for replay.

Implements the exact OrderManager surface the live decision code calls
(buy_limit, close_position, get_open_positions_async, find_recent_close_fill,
handle_trade_update, cancel_all_options[_async], get_fill_price,
get_filled_qty), so main.py's entry/exit/sweeper code runs UNMODIFIED against
recorded market data.

Execution model - conservative by construction, because a replay that fills
better than reality is worse than no replay:

  BUY (limit): fills at the ASK (you cross the spread; the limit only caps
    the price). If the ask is above the limit, the order RESTS and re-checks
    on every subsequent recorded quote, exactly like the live 30s fill wait;
    if the simulated clock passes FILL_TIMEOUT first, the entry fails -
    a missed fill is a real outcome and is counted, not papered over.

  SELL (market close): fills at the BID - what a market sell receives. If
    the bid is momentarily zero, the last known nonzero bid is used, floored
    at $0.01 (a market order always clears somewhere; pretending otherwise
    would route replay into the broker-failure machinery, which is unit-
    tested separately and not what replay measures).

Determinism: no wall clock, no threads, no network. Time is the injected
SimClock; order resolution is driven by notify_tick() from the replay pump.
Same recording + same config -> identical fills, identical CSV, every run.
"""

import asyncio
import logging
from typing import Optional

from alpaca.trading.enums import AssetClass

from orders import OrderManager, FILL_TIMEOUT

logger = logging.getLogger(__name__)


class SimOrder:
    def __init__(self, oid: str, fill_px: float, fill_qty: int):
        self.id               = oid
        self.filled_avg_price = fill_px
        self.filled_qty       = fill_qty


class SimPosition:
    def __init__(self, symbol: str, qty: int, avg_entry_price: float):
        self.symbol          = symbol
        self.qty             = qty
        self.avg_entry_price = avg_entry_price
        self.asset_class     = AssetClass.US_OPTION


class SimBroker:
    def __init__(self, clk, fill_timeout: float = FILL_TIMEOUT):
        self._clk          = clk
        self.fill_timeout  = fill_timeout
        self._quotes       = {}      # symbol -> (bid, ask)
        self._last_bid     = {}      # symbol -> last nonzero bid
        self._positions    = {}      # symbol -> SimPosition
        self._next_id      = 0
        self._tick_fut: Optional[asyncio.Future] = None
        # Audit trail for the replay summary
        self.fills             = []  # (kind, symbol, px, qty, sim_time)
        self.unfilled_entries  = 0

    # -- Replay-pump integration -----------------------------------------------

    def on_quote(self, symbol: str, bid: float, ask: float):
        self._quotes[symbol] = (bid, ask)
        if bid > 0:
            self._last_bid[symbol] = bid

    def notify_tick(self):
        """Wake resting orders. Swap-then-resolve: waiters that loop re-await
        the FRESH future, so a set event can never spin them hot."""
        fut, self._tick_fut = self._tick_fut, None
        if fut is not None and not fut.done():
            fut.set_result(None)

    async def _wait_tick(self):
        if self._tick_fut is None or self._tick_fut.done():
            self._tick_fut = asyncio.get_running_loop().create_future()
        await self._tick_fut

    # -- OrderManager surface --------------------------------------------------

    async def buy_limit(self, symbol: str, qty: int, limit_px: float) -> Optional[SimOrder]:
        deadline = self._clk.monotonic() + self.fill_timeout
        while True:
            bid, ask = self._quotes.get(symbol, (0.0, 0.0))
            if 0 < ask <= limit_px:
                fill_px = ask          # cross the spread - the honest cost
                self._next_id += 1
                oid = f"sim-buy-{self._next_id}"
                pos = self._positions.get(symbol)
                if pos is None:
                    self._positions[symbol] = SimPosition(symbol, qty, fill_px)
                else:  # average in (single-position strategy rarely hits this)
                    total = pos.qty + qty
                    pos.avg_entry_price = (
                        pos.avg_entry_price * pos.qty + fill_px * qty) / total
                    pos.qty = total
                self.fills.append(("buy", symbol, fill_px, qty, self._clk.monotonic()))
                logger.debug("SIM BUY fill: %s %d @ %.2f (limit %.2f)",
                             symbol, qty, fill_px, limit_px)
                return SimOrder(oid, fill_px, qty)
            if self._clk.monotonic() >= deadline:
                self.unfilled_entries += 1
                logger.info("SIM BUY unfilled: %s limit=%.2f ask=%.2f - timed out",
                            symbol, limit_px, ask)
                return None
            await self._wait_tick()   # rest until the next recorded quote

    async def close_position(self, symbol: str, qty: int) -> Optional[SimOrder]:
        pos = self._positions.get(symbol)
        if pos is None:
            return None
        qty     = min(qty, pos.qty)
        bid, _  = self._quotes.get(symbol, (0.0, 0.0))
        fill_px = bid if bid > 0 else max(self._last_bid.get(symbol, 0.0), 0.01)
        pos.qty -= qty
        if pos.qty <= 0:
            del self._positions[symbol]
        self._next_id += 1
        self.fills.append(("sell", symbol, fill_px, qty, self._clk.monotonic()))
        logger.debug("SIM SELL fill: %s %d @ %.2f", symbol, qty, fill_px)
        return SimOrder(f"sim-sell-{self._next_id}", fill_px, qty)

    def get_open_positions(self) -> list:
        return list(self._positions.values())

    async def get_open_positions_async(self) -> list:
        return list(self._positions.values())

    async def find_recent_close_fill(self, symbol: str):
        return None   # sim closes resolve synchronously - nothing to reconcile

    def cancel_all_options(self):
        pass

    async def cancel_all_options_async(self):
        pass

    def handle_trade_update(self, update):
        pass

    # Same fill interpretation as live - shared, never reimplemented
    get_fill_price = staticmethod(OrderManager.get_fill_price)
    get_filled_qty = staticmethod(OrderManager.get_filled_qty)
