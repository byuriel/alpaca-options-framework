"""
Order manager.

Alpaca options reality:
  - BUY:    simple limit order works fine
  - SELL:   submit_order fails ("insufficient buying power") — treated as new short
  - CLOSE:  close_position() works — Alpaca recognises it as closing a long
  - BRACKET: not supported for options ("complex orders not supported")

So: buy_limit() for entry, close_position() for all exits.

Execution architecture:
  - Every REST call runs via asyncio.to_thread() — the alpaca-py TradingClient
    is synchronous HTTP, and a blocking call on the event loop freezes all
    three WebSocket streams (bars, quotes, fills) for its duration. Off-loop,
    a slow Alpaca response can no longer stall quote processing or trip the
    watchdog into a mid-position restart.
  - Fill confirmation is event-driven: main.py routes TradingStream order
    events into handle_trade_update(), which wakes _wait_for_fill()
    immediately (millisecond fill latency instead of a 2-second poll). REST
    polling remains as the fallback for stream gaps.
  - Partial fills are first-class: a partially filled buy that times out has
    its remainder cancelled and the FILLED PORTION IS ADOPTED as the position
    (previously the contracts were silently owned and untracked until the
    ghost sweeper round-tripped them at market).
  - Orders this bot submits carry a CLIENT_ORDER_PREFIX client_order_id, and
    cleanup (cancel_all_options) only touches this bot's underlying — it no
    longer cancels every option order on the account.
"""

import asyncio
import logging
import uuid
from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    OrderSide, TimeInForce, OrderStatus, AssetClass, PositionIntent,
    QueryOrderStatus,
)
from alpaca.trading.requests import (
    LimitOrderRequest, ClosePositionRequest, GetOrdersRequest,
)
from alpaca.trading.models import Order

import config

logger = logging.getLogger(__name__)

FILL_POLL_INTERVAL = 2    # seconds between fallback fill-status polls
FILL_TIMEOUT       = 30   # seconds before giving up on a fill

# Order states with no further fills coming
_TERMINAL = (OrderStatus.FILLED, OrderStatus.CANCELED,
             OrderStatus.EXPIRED, OrderStatus.REJECTED)


def _make_client() -> TradingClient:
    return TradingClient(
        api_key    = config.ALPACA_API_KEY,
        secret_key = config.ALPACA_API_SECRET,
        paper      = config.PAPER,
    )


class OrderManager:
    def __init__(self):
        # Client is constructed LAZILY on first use: this object is a
        # module-level singleton in main.py, and building the TradingClient at
        # import time with missing credentials raises an SDK ValueError before
        # config.validate_credentials() in main() can print its actionable
        # message. First real use happens well after validation.
        self._client_instance: Optional[TradingClient] = None
        # TradingStream fast-path: order_id → latest Order, and per-order
        # events so _wait_for_fill wakes the instant a fill event arrives.
        self._stream_orders: dict[str, Order] = {}
        self._stream_events: dict[str, asyncio.Event] = {}

    @property
    def _client(self) -> TradingClient:
        if self._client_instance is None:
            self._client_instance = _make_client()
        return self._client_instance

    # ── TradingStream integration ─────────────────────────────────────────────

    def handle_trade_update(self, update):
        """Called from main.on_trade_update for every order event. Caches the
        order state and wakes any _wait_for_fill waiting on it.

        Only orders with a registered waiter are cached: the TradingStream
        delivers EVERY account order event (other strategies, manual trades,
        post-timeout cancel confirmations), and unconditional inserts with
        removal only in _wait_for_fill's finally grow the dict without bound
        over a session. Events with no waiter fall back to REST polling."""
        try:
            oid = str(update.order.id)
            ev  = self._stream_events.get(oid)
            if ev is not None:
                self._stream_orders[oid] = update.order
                ev.set()
        except Exception as e:
            logger.debug("handle_trade_update parse error: %s", e)

    # ── Entry ─────────────────────────────────────────────────────────────────

    async def buy_limit(
        self,
        symbol:   str,
        qty:      int,
        limit_px: float,
    ) -> Optional[Order]:
        """Simple limit buy. Returns an Order with fills (full or partial
        after remainder-cancel) or None if nothing was filled."""
        limit_px = round(limit_px, 2)
        req = LimitOrderRequest(
            symbol          = symbol,
            qty             = qty,
            side            = OrderSide.BUY,
            time_in_force   = TimeInForce.DAY,
            limit_price     = limit_px,
            position_intent = PositionIntent.BUY_TO_OPEN,
            client_order_id = f"{config.CLIENT_ORDER_PREFIX}-{uuid.uuid4().hex[:16]}",
        )
        try:
            order = await asyncio.to_thread(self._client.submit_order, req)
            logger.info("BUY submitted: %s qty=%d limit=%.2f id=%s",
                        symbol, qty, limit_px, order.id)
        except Exception as e:
            logger.error("BUY submit failed: %s", e)
            return None

        order_id = str(order.id)
        try:
            filled = await self._wait_for_fill(order_id)
        except asyncio.CancelledError:
            # Task cancelled while waiting for fill — attempt cancel on Alpaca.
            # If the order already filled, cancel fails silently. Recover any
            # fill (full OR partial) here instead of re-raising blind, so the
            # caller tracks the real position rather than seeing "no order in
            # flight" and submitting a duplicate entry on the next quote tick.
            logger.warning("BUY fill-wait cancelled — attempting cancel of %s", order_id)
            await self._cancel(order_id)
            recovered = await self._recheck_fill(order_id, context="post-cancel")
            if recovered is not None:
                return recovered
            raise

        if filled is None:
            logger.warning("BUY timed out, cancelling: %s", order_id)
            await self._cancel(order_id)
            # Give Alpaca a moment, then check for a fill that landed anyway —
            # including a PARTIAL fill, which we adopt as the position.
            await asyncio.sleep(1)
            return await self._recheck_fill(order_id, context="post-timeout")

        return filled

    @classmethod
    def _adoptable(cls, order: Optional[Order]) -> Optional[Order]:
        """Single definition of "this order represents a real fill we should
        track": any positive filled quantity with a usable average price.
        Used by both the fill-wait terminal branch and post-cancel rechecks
        so the interpretation can never drift between them."""
        if order is None:
            return None
        if cls.get_filled_qty(order) > 0 and cls.get_fill_price(order) is not None:
            return order
        return None

    async def _recheck_fill(self, order_id: str, context: str) -> Optional[Order]:
        """After a cancel/timeout, check whether the order filled (fully or
        partially) anyway. Returns the order if any quantity was filled."""
        try:
            status = await asyncio.to_thread(self._client.get_order_by_id, order_id)
        except Exception as e:
            logger.error("%s order check failed for %s: %s", context, order_id, e)
            return None
        adopted = self._adoptable(status)
        if adopted is not None:
            logger.warning(
                "RECOVERED %s: order %s filled %d @ %.2f despite cancel — "
                "returning as a fill", context, order_id,
                self.get_filled_qty(adopted), self.get_fill_price(adopted) or 0.0,
            )
        return adopted

    async def find_recent_close_fill(self, symbol: str) -> Optional[Order]:
        """
        Reconciliation: a close order whose fill-wait timed out is left LIVE
        (market closes are not blindly cancelled), so it can fill after the
        caller already gave up. When the broker no longer shows the position,
        this looks up the most recent filled SELL order for the symbol so the
        exit can be booked with the REAL fill price instead of staying
        tracked-forever or booking a guess.
        """
        try:
            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED, symbols=[symbol], limit=20,
            )
            for o in await asyncio.to_thread(self._client.get_orders, req):
                if (o.side == OrderSide.SELL
                        and self._adoptable(o) is not None):
                    return o
        except Exception as e:
            logger.error("find_recent_close_fill failed for %s: %s", symbol, e)
        return None

    # ── Exit ──────────────────────────────────────────────────────────────────

    async def close_position(
        self,
        symbol: str,
        qty:    int,
    ) -> Optional[Order]:
        """
        Close an existing long via Alpaca's close_position endpoint.
        Works where submit_order(SELL) fails with margin errors.
        Returns the filled (or partially filled) Order, or None on failure.
        The CALLER decides what a None means — nothing here pretends a
        failed close succeeded.
        """
        try:
            order = await asyncio.to_thread(
                self._client.close_position,
                symbol, ClosePositionRequest(qty=str(qty)),
            )
            logger.info("CLOSE submitted: %s qty=%d id=%s", symbol, qty, order.id)
        except Exception as e:
            logger.error("CLOSE failed: %s", e)
            return None

        order_id = str(order.id)
        filled = await self._wait_for_fill(order_id)
        if filled is None:
            # A close is a market order — a timeout here means something is
            # genuinely wrong. Re-check once for a late/partial fill before
            # reporting failure; do NOT cancel a market close blindly.
            filled = await self._recheck_fill(order_id, context="close-timeout")
        return filled

    # ── Position polling ──────────────────────────────────────────────────────

    def get_open_positions(self) -> list:
        """Synchronous REST fetch — use only from startup/sync contexts."""
        try:
            return self._client.get_all_positions()
        except Exception as e:
            logger.error("get_open_positions failed: %s", e)
            return []

    async def get_open_positions_async(self) -> list:
        """Event-loop-safe position fetch for in-session use (ghost sweeper)."""
        try:
            return await asyncio.to_thread(self._client.get_all_positions)
        except Exception as e:
            logger.debug("get_open_positions_async failed: %s", e)
            return []

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _wait_for_fill(self, order_id: str) -> Optional[Order]:
        """
        Wait for a terminal order state. Event-driven via TradingStream with
        REST polling as fallback. Returns:
          - the Order when FILLED
          - the Order when terminal-with-partial-fill (caller adopts filled_qty)
          - None on timeout or terminal-with-zero-fill
        """
        loop     = asyncio.get_running_loop()
        deadline = loop.time() + FILL_TIMEOUT
        event    = self._stream_events.setdefault(order_id, asyncio.Event())
        try:
            while loop.time() < deadline:
                # Fast path: wake on a TradingStream event; fall back to a
                # REST poll every FILL_POLL_INTERVAL seconds regardless.
                try:
                    await asyncio.wait_for(event.wait(), timeout=FILL_POLL_INTERVAL)
                    event.clear()
                    order = self._stream_orders.get(order_id)
                except asyncio.TimeoutError:
                    order = None

                if order is None or order.status not in _TERMINAL:
                    try:
                        order = await asyncio.to_thread(
                            self._client.get_order_by_id, order_id)
                    except Exception as e:
                        logger.error("Error polling order %s: %s", order_id, e)
                        continue

                if order.status == OrderStatus.FILLED:
                    logger.info("Filled: %s avg=%.2f", order_id,
                                self.get_fill_price(order) or 0.0)
                    return order
                if order.status in _TERMINAL:
                    adopted = self._adoptable(order)
                    if adopted is not None:
                        logger.warning(
                            "Order %s terminal (%s) with PARTIAL fill %d @ %.2f — adopting",
                            order_id, order.status, self.get_filled_qty(adopted),
                            self.get_fill_price(adopted) or 0.0,
                        )
                        return adopted
                    logger.warning("Order %s terminal: %s (no fill)", order_id, order.status)
                    return None
            return None
        finally:
            self._stream_events.pop(order_id, None)
            self._stream_orders.pop(order_id, None)

    async def _cancel(self, order_id: str):
        try:
            await asyncio.to_thread(self._client.cancel_order_by_id, order_id)
        except Exception as e:
            logger.error("Cancel failed %s: %s", order_id, e)

    def emergency_close_sync(self, symbol: str, qty: int) -> Optional[str]:
        """Synchronous market close for halt paths that run BEFORE the event
        loop exists (restart-storm brake). Submits and returns the order id
        WITHOUT waiting for the fill — the process is halting; the nightly
        reconciliation and the broker's own record are the confirmation."""
        try:
            order = self._client.close_position(
                symbol, ClosePositionRequest(qty=str(qty)))
            logger.warning(
                "EMERGENCY CLOSE submitted: %s x%d id=%s — fill NOT confirmed "
                "locally; verify at the broker / next reconciliation",
                symbol, qty, order.id,
            )
            return str(order.id)
        except Exception as e:
            logger.critical("EMERGENCY CLOSE FAILED for %s x%d: %s — "
                            "*** CLOSE MANUALLY AT THE BROKER ***", symbol, qty, e)
            return None

    def cancel_all_options(self):
        """Cancel this bot's open option orders (synchronous — startup/shutdown
        paths only). Scoped to the configured underlying so a shared account's
        other option orders are never touched."""
        try:
            for o in self._client.get_orders():
                if o.asset_class != AssetClass.US_OPTION:
                    continue
                if not str(o.symbol).startswith(config.UNDERLYING):
                    continue
                try:
                    self._client.cancel_order_by_id(str(o.id))
                except Exception as e:
                    logger.error("Cancel failed %s: %s", o.id, e)
        except Exception as e:
            logger.error("cancel_all_options failed: %s", e)

    async def cancel_all_options_async(self):
        await asyncio.to_thread(self.cancel_all_options)

    @staticmethod
    def get_fill_price(order: Optional[Order]) -> Optional[float]:
        """Average fill price, or None when unknown. Callers MUST handle None —
        the old 0.0 fallback silently booked a 100% loss into the CSV."""
        if order is None:
            return None
        try:
            px = float(order.filled_avg_price)
            return px if px > 0 else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def get_filled_qty(order: Optional[Order]) -> int:
        if order is None:
            return 0
        try:
            return int(float(order.filled_qty or 0))
        except (TypeError, ValueError):
            return 0
