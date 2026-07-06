"""
Strike selection and OCC symbol construction.

Modes:
  - startup_atr()            : fetches 5-day daily ATR once at startup (fallback baseline)
  - prime_chain_cache()      : fetches the real option chain once at startup so
                               dynamically built symbols can be validated —
                               subscribing to strikes that don't exist silently
                               shrinks the tradeable window
  - compute_dynamic_strikes(): called on every 1-min bar with live SPY price +
                               ATR baseline; returns call/put OCC symbols
                               filtered against the cached chain
"""

import datetime
import logging
from typing import Optional

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.requests import OptionChainRequest
from alpaca.data.enums import DataFeed

import config
from occ import build_occ_symbol

logger = logging.getLogger(__name__)

# Chain symbols that actually exist, keyed by expiry — primed once at startup.
# Empty set for an expiry means "validation unavailable, pass everything through"
# (never fail closed on a data-API hiccup; Alpaca just won't stream fakes).
_chain_cache: dict = {}


def _atr_5day(stock_client: StockHistoricalDataClient) -> float:
    end   = datetime.datetime.now(tz=config.ET)
    start = end - datetime.timedelta(days=15)  # generous window → 5 complete days
    req   = StockBarsRequest(
        symbol_or_symbols=config.UNDERLYING,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        feed=DataFeed.IEX,
    )
    bars = stock_client.get_stock_bars(req)[config.UNDERLYING]
    # Exclude today's PARTIAL bar by date, not by blind slicing — the old
    # [-6:-1] silently dropped the newest complete day whenever the query
    # didn't include a today-bar (e.g. premarket starts).
    today = config.today_et()
    bars  = [b for b in bars
             if b.close is not None and b.timestamp.astimezone(config.ET).date() < today]
    bars  = bars[-5:]
    if not bars:
        return 3.0  # fallback ATR if data unavailable
    # True range: max(H-L, |H-prevC|, |L-prevC|) — plain H-L understates the
    # range on gap days, which shrinks strike offsets exactly when moves are big.
    trs = []
    prev_close = None
    for b in bars:
        if prev_close is None:
            trs.append(b.high - b.low)
        else:
            trs.append(max(b.high - b.low,
                           abs(b.high - prev_close),
                           abs(b.low - prev_close)))
        prev_close = b.close
    return sum(trs) / len(trs)


def _current_spy_price(stock_client: StockHistoricalDataClient) -> float:
    req    = StockLatestQuoteRequest(symbol_or_symbols=config.UNDERLYING, feed=DataFeed.IEX)
    quotes = stock_client.get_stock_latest_quote(req)
    q      = quotes[config.UNDERLYING]
    if q.bid_price and q.ask_price and q.bid_price > 0 and q.ask_price > 0:
        return (q.ask_price + q.bid_price) / 2.0
    return q.ask_price or q.bid_price or 0.0


def _round_to_step(price: float, step: float) -> float:
    return round(round(price / step) * step, 2)


def _strike_window(target: float) -> list:
    return [target + i * config.STRIKE_STEP
            for i in range(-config.STRIKE_ALTS, config.STRIKE_ALTS + 1)]


# ── Chain validation ───────────────────────────────────────────────────────────

def prime_chain_cache(option_client: OptionHistoricalDataClient,
                      expiry: Optional[datetime.date] = None) -> int:
    """
    Fetch the real chain for this expiry once and cache the existing OCC
    symbols. Called at startup from main(). Returns the number of symbols
    cached (0 = validation unavailable; symbol filtering becomes a no-op).
    """
    if expiry is None:
        expiry = config.today_et()
    existing = set()
    for side in ("call", "put"):
        req = OptionChainRequest(
            underlying_symbol=config.UNDERLYING,
            expiration_date=expiry,
            type=side,
        )
        try:
            chain = option_client.get_option_chain(req)
            existing.update(chain.keys())
        except Exception as e:
            logger.warning("Chain fetch failed for %s %s: %s", expiry, side, e)
    _chain_cache[expiry] = existing
    if existing:
        logger.info("Chain cache primed: %d contracts for %s", len(existing), expiry)
    else:
        logger.warning(
            "Chain cache EMPTY for %s — strike validation disabled, "
            "dynamically built symbols pass through unfiltered.", expiry,
        )
    return len(existing)


def _filter_existing(symbols: list, expiry: datetime.date) -> list:
    existing = _chain_cache.get(expiry)
    if not existing:
        return symbols
    valid = [s for s in symbols if s in existing]
    if len(valid) < len(symbols):
        logger.debug("Chain filter: %d/%d symbols exist for %s",
                     len(valid), len(symbols), expiry)
    return valid


# ── Public API ─────────────────────────────────────────────────────────────────

def startup_atr(stock_client: StockHistoricalDataClient) -> float:
    """
    Fetch 5-day daily ATR once at startup as a baseline.
    Used to seed the dynamic strike logic before enough 1-min bars accumulate.
    """
    return _atr_5day(stock_client)


def compute_dynamic_strikes(
    spy_price: float,
    atr:       float,
    expiry:    Optional[datetime.date] = None,
) -> dict:
    """
    Called on every 1-min bar. Computes the current ideal call/put strikes
    from live SPY price and the daily ATR baseline, builds OCC symbols, and
    filters them against the chain cache (when primed) so the subscription
    window only contains contracts that exist.

    Returns:
      {
        "call_strike":  563.50,
        "put_strike":   556.50,
        "call_symbols": ["SPY260513C00563500", ...],   # target ± STRIKE_ALTS
        "put_symbols":  ["SPY260513P00556500", ...],
      }
    """
    if expiry is None:
        expiry = config.today_et()

    # atr is always the daily ATR baseline passed from main.py — no scaling needed.
    # The 1-min live ATR from the momentum engine is intentionally NOT used here
    # because premarket and early-session 1-min ranges are too small to be meaningful
    # for daily strike offset calculation.
    # MAX_STRIKE_OFFSET caps the offset so elevated 5-day ATR doesn't push strikes
    # beyond the approach zone on low-realized-range days.
    offset      = min(config.ATR_MULT * atr, config.MAX_STRIKE_OFFSET)
    call_target = _round_to_step(spy_price + offset, config.STRIKE_STEP)
    put_target  = _round_to_step(spy_price - offset, config.STRIKE_STEP)

    call_symbols = [build_occ_symbol(config.UNDERLYING, expiry, "CALL", s)
                    for s in _strike_window(call_target)]
    put_symbols  = [build_occ_symbol(config.UNDERLYING, expiry, "PUT", s)
                    for s in _strike_window(put_target)]

    return {
        "call_strike":  call_target,
        "put_strike":   put_target,
        "call_symbols": _filter_existing(call_symbols, expiry),
        "put_symbols":  _filter_existing(put_symbols, expiry),
    }
