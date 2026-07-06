"""
Async data feeds — three concurrent WebSocket streams:
  1. StockDataStream   — 1-min SPY bars (IEX)
  2. OptionDataStream  — real-time option quotes (indicative)
  3. TradingStream     — real-time order/fill events

Option subscription is intentionally deferred until market open.
The bot starts with no option subscriptions, waits for the first
9:30 ET bar, then calls subscribe_options() from a separate task
(safe — not inside the stream callback chain, no deadlock risk).
"""

import asyncio
import logging
from typing import Callable, List

from alpaca.data.live import StockDataStream, OptionDataStream
from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.trading.stream import TradingStream

import config

logger = logging.getLogger(__name__)

_STOCK_FEEDS  = {"iex": DataFeed.IEX, "sip": DataFeed.SIP}
_OPTION_FEEDS = {"indicative": OptionsFeed.INDICATIVE, "opra": OptionsFeed.OPRA}


def stock_feed() -> DataFeed:
    """Configured stock feed enum — single mapping point for streams AND
    historical requests, so live signals and preseed/ATR data can never
    silently come from different tapes."""
    try:
        return _STOCK_FEEDS[config.STOCK_FEED]
    except KeyError:
        raise SystemExit(
            f"Unknown ALPACA_STOCK_FEED={config.STOCK_FEED!r} — "
            f"use one of {sorted(_STOCK_FEEDS)}")


def option_feed() -> OptionsFeed:
    try:
        return _OPTION_FEEDS[config.OPTION_FEED]
    except KeyError:
        raise SystemExit(
            f"Unknown ALPACA_OPTION_FEED={config.OPTION_FEED!r} — "
            f"use one of {sorted(_OPTION_FEEDS)}")


class FeedManager:
    def __init__(
        self,
        on_spy_bar:      Callable,
        on_option_quote: Callable,
        on_trade_update: Callable,
    ):
        self._on_spy_bar      = on_spy_bar
        self._on_option_quote = on_option_quote
        self._on_trade_update = on_trade_update

        self._stock_stream = StockDataStream(
            api_key    = config.ALPACA_API_KEY,
            secret_key = config.ALPACA_API_SECRET,
            feed       = stock_feed(),
        )
        self._option_stream = OptionDataStream(
            api_key    = config.ALPACA_API_KEY,
            secret_key = config.ALPACA_API_SECRET,
            feed       = option_feed(),
        )
        logger.info("Data feeds: stock=%s options=%s",
                    config.STOCK_FEED, config.OPTION_FEED)
        self._trading_stream = TradingStream(
            api_key    = config.ALPACA_API_KEY,
            secret_key = config.ALPACA_API_SECRET,
            paper      = config.PAPER,
        )

        self._tasks: list[asyncio.Task] = []
        self._options_subscribed = False

    # ── Handlers ──────────────────────────────────────────────────────────────

    async def _handle_spy_bar(self, bar):
        try:
            await self._on_spy_bar(bar)
        except Exception as e:
            logger.error("spy_bar handler error: %s", e)

    async def _handle_option_quote(self, quote):
        try:
            await self._on_option_quote(quote)
        except Exception as e:
            logger.error("option_quote handler error: %s", e)

    async def _handle_trade_update(self, update):
        try:
            await self._on_trade_update(update)
        except Exception as e:
            logger.error("trade_update handler error: %s", e)

    # ── Deferred option subscription ──────────────────────────────────────────

    def subscribe_options(self, symbols: List[str]):
        """
        Subscribe to option quotes. Called once at market open from a
        dedicated asyncio task — NOT from inside a stream callback.
        Calling from outside the callback chain is safe (no deadlock).
        """
        if self._options_subscribed or not symbols:
            return
        self._option_stream.subscribe_quotes(self._handle_option_quote, *symbols)
        self._options_subscribed = True
        logger.info("Option quotes subscribed: %d symbols", len(symbols))

    def add_option_symbols(self, symbols: List[str]):
        """
        Add more symbols to an existing option subscription (mid-session re-subscription).
        Safe to call from any standalone asyncio task — NOT from inside a stream callback.
        Alpaca deduplicates internally, so passing already-subscribed symbols is harmless.
        """
        if not symbols or not self._options_subscribed:
            return
        self._option_stream.subscribe_quotes(self._handle_option_quote, *symbols)
        logger.info("Option quotes expanded: +%d symbols", len(symbols))

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self):
        # Stock and trading streams start immediately.
        # Option stream connects but receives no symbols until subscribe_options() is called.
        self._stock_stream.subscribe_bars(self._handle_spy_bar, config.UNDERLYING)
        self._trading_stream.subscribe_trade_updates(self._handle_trade_update)

        # NOTE: _run_forever() is alpaca-py private API — the public .run() is
        # blocking-sync and unusable inside an existing event loop. This is why
        # requirements.txt pins alpaca-py to a bounded range: a minor release
        # can rename this without notice. Re-test before bumping the bound.
        t1 = asyncio.create_task(self._stock_stream._run_forever(),   name="stock_stream")
        t2 = asyncio.create_task(self._option_stream._run_forever(),  name="option_stream")
        t3 = asyncio.create_task(self._trading_stream._run_forever(), name="trading_stream")
        self._tasks = [t1, t2, t3]

        logger.info("Feeds started — awaiting market open for option subscription.")
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            logger.info("Feeds cancelled.")

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        logger.info("Feeds stopped.")
