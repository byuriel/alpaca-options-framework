"""
Entry point — orchestrates the full bot lifecycle.

Flow:
  1. Startup: validate credentials + trading calendar, fetch ATR baseline,
     prime the option-chain cache, pre-seed EMAs, recover open positions
     (full metadata from logs/position_state.json, existence from REST)
  2. On each 1-min SPY bar:
       a. Update momentum engine (EMA5/20, VWAP, ROC5, atr5, direction)
       b. Tick risk cooldown
       c. Recompute dynamic strikes, update routing table
       d. Log bar summary
  3. On each option quote tick (quote-driven exits):
       - Update quote cache and proxy delta tracker
       - IF holding this symbol: asyncio.create_task(_evaluate_exit())
       - ELSE: asyncio.create_task(_evaluate_entry()) — entries are ALSO
         task-spawned so the buy path (submit + fill wait) never blocks the
         quote handler chain; a blocked handler would leave the just-opened
         position's exits blind during its riskiest first seconds
  4. _exit_monitor: 30-second safety net in case quotes stop arriving
  5. _staleness_watcher: kill switch — flattens if quotes for the held symbol
     go silent, locks new entries if the bar stream dies
  6. _time_stop_watcher: force-close all positions at the session-derived
     time stop (re-anchored on early-close days)

Exit priority (evaluated in _evaluate_exit on every quote):
  1. TP       — mid >= entry × TP_MULT (1.50×)
  2. Stop     — mid <= entry × STOP_MULT (0.50×)
  3. Trail    — peak_mid >= entry × 1.20 AND mid <= peak_mid × 0.88
  4. TimeStop — session close − 35 min (separate watcher)

Execution integrity invariants (do not weaken these):
  - A trade is booked into the CSV ONLY on a broker-confirmed fill. A failed
    close keeps the position tracked and retries — it never books a guessed
    price and never erases local tracking while the broker still holds the
    position (that combination double-counts P&L via the ghost sweeper).
  - entry_pending is True from the entry decision until the position is fully
    tracked locally; the ghost sweeper stands down while it is set, so a fill
    that exists on Alpaca a beat before local tracking cannot be reaped.
  - All broker REST calls in-session run off the event loop
    (asyncio.to_thread) so a slow HTTP round trip cannot stall the streams
    or trip the watchdog.
"""

import asyncio
import csv
import datetime
import logging
import os
import select
import signal
import sys
import threading

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import Adjustment
from alpaca.trading.enums import AssetClass

import alerts
import clock
import config
import event_calendar
import market_calendar
import recorder as recorder_mod
import reconcile as reconcile_mod
import strikes
from feeds import FeedManager, option_feed, stock_feed
from momentum import MomentumEngine, Bar
from occ import parse_occ_expiry, parse_occ_symbol
from orders import OrderManager
from risk import RiskManager
from orb_filter import ORBFilter
from signals import check_entry
from state import BotState, Position

os.makedirs(config.LOG_DIR, exist_ok=True)
_today_str = config.today_et().isoformat()
logging.basicConfig(
    level    = logging.INFO,
    format   = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"{config.LOG_DIR}/bot_{_today_str}.log"),
    ],
)
logger = logging.getLogger("main")


# ── Shared singletons ─────────────────────────────────────────────────────────

momentum_engine = MomentumEngine()
bot_state       = BotState()
risk_manager    = RiskManager()
order_manager   = OrderManager()
orb_filter      = ORBFilter()

# Created inside the running event loop (main() / replay setup) — a Lock
# constructed at import time binds to the wrong loop on Python 3.9 and
# raises "attached to a different loop" at first acquire.
_entry_lock:       asyncio.Lock = None
_ghost_sweep_lock: asyncio.Lock = None
_foreign_positions_seen: set = set()   # out-of-scope positions logged once
_recorder = None   # MarketDataRecorder — created in main() when enabled;
                   # stays None in replay (replay reads recordings, never writes)
_blackout_announced = False   # event-blackout WARNING logged once, not per tick

# ── Watchdog heartbeat ────────────────────────────────────────────────────────
# Updated by _status_loop every iteration. Watchdog thread checks every 10s;
# if stale for > WATCHDOG_TIMEOUT seconds during market hours, auto-restarts.
import time as _time
_last_heartbeat:     float = 0.0
_WATCHDOG_TIMEOUT    = 20    # seconds — restart if event loop silent this long
_WATCHDOG_START      = "09:00"   # ET — only watch after this time
_WATCHDOG_END        = "15:35"   # ET — stop watching after this time (re-anchored
                                 # to the session close on early-close days)

# Routing table: {occ_symbol: (side, strike)} — updated every bar
_current_subscriptions: dict[str, tuple] = {}
_feed:               FeedManager    = None   # set in main()
_baseline_atr:       float          = 3.0    # set in main()
_market_open_event:  asyncio.Event  = None   # set in main(); fired on first RTH bar
_open_bar_strikes:   dict           = {}     # strike data from the opening bar

# Re-subscription tracking
_RESUB_THRESHOLD     = 3.0   # SPY points of movement before re-subscribing option window
_last_sub_spy_price: float = 0.0

# Exit executor
_CLOSE_MAX_ATTEMPTS  = 3     # close retries before locking the gate and alerting


def _build_routing_table(strikes_dict: dict) -> dict:
    """
    Build {occ_symbol: (side, actual_strike)} routing table.
    Each symbol gets its OWN parsed strike — NOT the shared target strike.
    This ensures zone and directionality checks in check_entry use the real
    strike of each symbol, not a shared target that may be several strikes away.
    """
    meta = {}
    for sym in strikes_dict["call_symbols"]:
        _, actual_strike = parse_occ_symbol(sym)
        if actual_strike is not None:
            meta[sym] = ("call", actual_strike)
    for sym in strikes_dict["put_symbols"]:
        _, actual_strike = parse_occ_symbol(sym)
        if actual_strike is not None:
            meta[sym] = ("put", actual_strike)
    return meta


# ── Bar handler ───────────────────────────────────────────────────────────────

async def on_spy_bar(bar):
    global _current_subscriptions, _open_bar_strikes

    b = Bar(
        t      = bar.timestamp,
        open   = float(bar.open),
        high   = float(bar.high),
        low    = float(bar.low),
        close  = float(bar.close),
        volume = float(bar.volume),
    )
    if _recorder is not None:
        _recorder.record_bar(b.t.isoformat(), b.open, b.high, b.low, b.close, b.volume)
    bot_state.spy_price = b.close
    bot_state.last_bar_monotonic = clock.monotonic()   # staleness watcher input
    m_state = momentum_engine.on_bar(b)

    # Tick cooldown counter
    risk_manager.tick_bar()

    # ORB filter — update on every bar (records only the first bar of each hour)
    bar_time = b.t.astimezone(config.ET).time()
    orb_filter.on_bar(bar_time, b.high, b.low)

    # Fire market-open event on first RTH bar (≥ 09:30 ET)
    if not _market_open_event.is_set() and bar_time >= market_calendar.ET_OPEN:
        _open_bar_strikes = strikes.compute_dynamic_strikes(b.close, _baseline_atr)
        _market_open_event.set()
        logger.info(
            "Market open bar: SPY=%.2f — option subscription triggered",
            b.close,
        )

    if _market_open_event.is_set():
        # SPY-level stop — check on every bar close while a position is open
        asyncio.create_task(_evaluate_spy_stop(b.close))
        # Ghost sweeper — close any Alpaca option positions not tracked locally
        asyncio.create_task(_check_ghost_positions())

    # Update routing table every bar (no WebSocket changes — subscriptions fixed at open)
    # new_strikes initialised to zero so the BAR log below is always safe pre-market
    new_strikes = {"call_strike": 0.0, "put_strike": 0.0}
    if _market_open_event.is_set():
        new_strikes = strikes.compute_dynamic_strikes(b.close, _baseline_atr)
        new_meta    = _build_routing_table(new_strikes)

        if set(new_meta) != set(_current_subscriptions):
            logger.info(
                "Strikes updated: call=%.2f put=%.2f (SPY=%.2f ATR=%.3f)",
                new_strikes["call_strike"], new_strikes["put_strike"], b.close, _baseline_atr,
            )
            _current_subscriptions = new_meta

    # Per-bar console summary
    pos = bot_state.position
    logger.info(
        "BAR | SPY=%.2f | %s | EMA5=%.2f EMA20=%.2f | VWAP=%.2f | ROC=%.4f | "
        "consec=%s | atr5=%.3f | call=%.2f put=%.2f | orb=%s | pos=%s | pnl=$%.2f",
        b.close,
        m_state.direction.upper(),
        m_state.ema5, m_state.ema20,
        m_state.vwap,
        m_state.roc5,
        f"+{m_state.consec_green}g" if m_state.consec_green else f"-{m_state.consec_red}r",
        m_state.atr5,
        new_strikes["call_strike"],
        new_strikes["put_strike"],
        orb_filter.bias or "NONE",
        f"{pos.symbol} @{pos.entry_price:.2f}" if pos else "NONE",
        risk_manager.daily_pnl,
    )


# ── Trade update handler (order event stream) ─────────────────────────────────

async def on_trade_update(update):
    """Real-time order events → OrderManager fill fast-path. A fill confirms
    in milliseconds via the stream instead of waiting for the next REST poll."""
    try:
        order_manager.handle_trade_update(update)
        logger.debug("Trade update: event=%s order=%s", update.event, update.order.id)
    except Exception:
        pass


# ── Option quote handler ───────────────────────────────────────────────────────

async def on_option_quote(quote):
    sym = quote.symbol
    bid = float(quote.bid_price or 0)
    ask = float(quote.ask_price or 0)
    ts  = quote.timestamp

    if _recorder is not None:
        _recorder.record_quote(
            sym, bid, ask, ts.isoformat() if ts else "",
            int(quote.bid_size or 0), int(quote.ask_size or 0),
        )

    # Update proxy delta tracker
    bot_state.update_option_quote(sym, bid, ask, ts)

    # Quote-driven exit — fires on every tick for the held symbol.
    # asyncio.create_task() schedules the coroutine on the event loop and
    # returns immediately, so close_position() is never called from inside
    # the stream callback.
    pos = bot_state.position
    if pos and pos.symbol == sym and not bot_state.exit_pending:
        asyncio.create_task(_evaluate_exit(bot_state.get_quote(sym)))
        return

    # Entry evaluation — ALSO task-spawned: the buy path (submit + fill wait)
    # must never run inside the quote handler chain, or every other symbol's
    # quotes (including the just-opened position's) queue behind it.
    if bot_state.position is not None:
        return
    if bot_state.entry_pending:          # buy already in-flight on another quote tick
        return
    if sym not in _current_subscriptions:
        return
    if _entry_lock is not None and not _entry_lock.locked():
        asyncio.create_task(_evaluate_entry(sym))


# ── Entry evaluation ──────────────────────────────────────────────────────────

async def _evaluate_entry(symbol: str):
    async with _entry_lock:
        if bot_state.position is not None or bot_state.entry_pending:
            return
        if not risk_manager.can_trade():
            return

        # Scheduled-event blackout (FOMC statement etc.) — risk gating, not
        # an alpha filter; sits with the other risk gates, not in signals.
        blackout = event_calendar.entry_blackout_reason(clock.now_et())
        if blackout is not None:
            global _blackout_announced
            if not _blackout_announced:
                _blackout_announced = True
                logger.warning("EVENT BLACKOUT active: %s — no new entries", blackout)
            return

        side, strike = _current_subscriptions.get(symbol, (None, None))
        if side is None:
            return

        quote   = bot_state.get_quote(symbol)
        tracker = bot_state.get_tracker(symbol)
        if quote is None:
            return

        # Freshness gate: a queued entry task (e.g. behind another entry's
        # 30s fill wait) must not size a trade off a quote from before the
        # wait — the market has moved and the signal never saw this price.
        if (quote.recv_monotonic > 0
                and clock.monotonic() - quote.recv_monotonic > config.ENTRY_QUOTE_MAX_AGE_SEC):
            logger.debug("SKIP %s | quote stale (%.1fs old)", symbol,
                         clock.monotonic() - quote.recv_monotonic)
            return

        # Entry-side quote quality gate: a mid computed inside a wide (or
        # one-sided) spread is not a price — don't size a trade off it.
        if quote.spread_pct > config.ENTRY_MAX_SPREAD_PCT:
            logger.debug(
                "SKIP %s | entry spread %.1f%% > %.1f%%",
                symbol, quote.spread_pct * 100, config.ENTRY_MAX_SPREAD_PCT * 100,
            )
            return

        should_enter = check_entry(
            side          = side,
            strike        = strike,
            option_quote  = quote,
            momentum      = momentum_engine.state,
            proxy_tracker = tracker,
            spy_price     = bot_state.spy_price,
            trades_today  = risk_manager.trades_today,
            has_open_pos  = False,
            atr5          = momentum_engine.state.atr5,
        )
        if not should_enter:
            return

        # ORB shadow filter — logs BLOCK/ALLOW without preventing the trade
        orb_filter.check_shadow(side, symbol)

        entry_mid   = quote.mid
        qty         = risk_manager.size_trade(entry_mid)
        if qty <= 0:
            return   # cannot size within risk limits — sizing already logged why
        limit_price = round(entry_mid * 1.02, 2)

        logger.info("Placing entry: %s qty=%d limit=%.2f", symbol, qty, limit_price)

        # entry_pending covers the ENTIRE window from order submission until
        # the position is tracked locally — the ghost sweeper stands down
        # while it is set, so a fill that lands on Alpaca moments before
        # open_position() cannot be mistaken for a ghost and force-closed.
        bot_state.entry_pending = True
        try:
            order = await order_manager.buy_limit(symbol, qty, limit_price)

            if order is None:
                logger.warning("Entry failed/timed out for %s — no fill adopted", symbol)
                return

            fill_price = order_manager.get_fill_price(order)
            filled_qty = order_manager.get_filled_qty(order)
            if fill_price is None or filled_qty <= 0:
                # A fill without a usable price cannot be tracked coherently.
                # Loud log; if contracts actually exist the ghost sweeper
                # reaps them on the next bar once entry_pending clears.
                logger.error(
                    "Fill price/qty unavailable for %s (order %s) — NOT tracking; "
                    "ghost sweeper will reconcile against the broker",
                    symbol, order.id,
                )
                return
            if filled_qty < qty:
                logger.warning(
                    "PARTIAL entry fill: %d/%d contracts — adopting filled portion",
                    filled_qty, qty,
                )

            pos = Position(
                symbol           = symbol,
                side             = side,
                strike           = strike,
                qty              = filled_qty,
                entry_price      = fill_price,
                entry_time       = clock.now_et(),
                order_id         = str(order.id),
                entry_spy_price  = bot_state.spy_price,
                entry_atr5       = momentum_engine.state.atr5,
                entry_bid        = quote.bid,
                entry_ask        = quote.ask,
            )
            bot_state.open_position(pos)
            logger.info(
                "ENTERED: %s at %.2f × %d | TP=%.2f | Stop=%.2f | slippage=%+.3f vs decision mid",
                symbol, fill_price, filled_qty,
                round(fill_price * config.TP_MULT,   2),
                round(fill_price * config.STOP_MULT, 2),
                fill_price - entry_mid,
            )
        finally:
            # Cleared only after open_position (or a definitive no-fill) —
            # see the ghost-sweeper invariant above.
            bot_state.entry_pending = False


# ── Trade list display ────────────────────────────────────────────────────────

def _print_trades():
    """Print today's closed trades from trades_YYYY-MM-DD.csv to the terminal."""
    today    = config.today_et().isoformat()
    log_path = os.path.join(config.LOG_DIR, f"trades_{today}.csv")

    if not os.path.exists(log_path):
        print("  No trades log found.")
        return

    rows = []
    try:
        with open(log_path, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("date") == today:
                    rows.append(row)
    except Exception as e:
        print(f"  Error reading trades log: {e}")
        return

    def _fmt_time(iso: str) -> str:
        """ISO timestamp → HH:MM:SS ET."""
        try:
            dt = datetime.datetime.fromisoformat(iso).astimezone(config.ET)
            return dt.strftime("%H:%M:%S")
        except Exception:
            return "  --:--  "

    def _fmt_duration(entry_iso: str, exit_iso: str) -> str:
        """Return elapsed time as Xm Ys."""
        try:
            t0 = datetime.datetime.fromisoformat(entry_iso)
            t1 = datetime.datetime.fromisoformat(exit_iso)
            secs = int((t1 - t0).total_seconds())
            return f"{secs // 60}m {secs % 60:02d}s"
        except Exception:
            return "  --   "

    print("\n" + "─" * 86)
    print(f"  TODAY'S TRADES  ({today})  —  {len(rows)} closed")
    print("─" * 86)
    if not rows:
        print("  No closed trades yet today.")
    else:
        print(f"  {'#':<3}  {'Symbol':<22}  {'Side':<5}  "
              f"{'Entry $':>7}  {'Exit $':>6}  {'Qty':>3}  "
              f"{'In':>8}  {'Out':>8}  {'TiT':>7}  "
              f"{'Reason':<8}  {'P&L':>8}")
        print("  " + "-" * 82)
        total = 0.0
        for i, row in enumerate(rows, 1):
            pnl       = float(row.get("realized_pnl", 0))
            total    += pnl
            icon      = "✅" if pnl >= 0 else "❌"
            entry_t   = _fmt_time(row.get("entry_time", ""))
            exit_t    = _fmt_time(row.get("exit_time",  ""))
            duration  = _fmt_duration(row.get("entry_time", ""), row.get("exit_time", ""))
            print(
                f"  {i:<3}  {row.get('symbol',''):<22}  {row.get('side',''):<5}  "
                f"${float(row.get('entry_price', 0)):>6.2f}  "
                f"${float(row.get('exit_price',  0)):>5.2f}  "
                f"{int(float(row.get('qty', 0))):>3}  "
                f"{entry_t:>8}  {exit_t:>8}  {duration:>7}  "
                f"{row.get('reason',''):<8}  "
                f"{icon} ${pnl:>+7.2f}"
            )
        print("  " + "-" * 82)
        print(f"  {'TOTAL':>65}  ${total:>+7.2f}")
    print("─" * 86 + "\n")


# ── Daily P&L recovery from CSV ──────────────────────────────────────────────

def _restore_daily_pnl():
    """
    Read today's closed trades from trades_YYYY-MM-DD.csv and restore risk manager counters.
    Called after reset_day() so a restart doesn't wipe the session P&L.
    """
    today    = config.today_et().isoformat()
    log_path = os.path.join(config.LOG_DIR, f"trades_{today}.csv")
    if not os.path.exists(log_path):
        return

    daily_pnl = 0.0
    entries   = set()   # distinct positions — partial exit legs share an
    rows      = 0       # entry_order_id and must count as ONE trade
    try:
        with open(log_path, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("date") == today:
                    daily_pnl += float(row.get("realized_pnl", 0))
                    rows      += 1
                    if row.get("entry_order_id"):
                        entries.add(row["entry_order_id"])
    except Exception as e:
        logger.warning("Could not restore daily P&L from CSV: %s", e)
        return
    trades_today = len(entries) if entries else rows

    if trades_today > 0:
        risk_manager.restore_day(daily_pnl, trades_today)
    else:
        logger.info("No trades found in CSV for today — starting fresh.")


# ── Position recovery (called at startup after a restart) ─────────────────────

def _recover_open_position():
    """
    On startup, poll Alpaca REST for any existing option position.

    Two-source recovery: the broker is authoritative for WHETHER a position
    exists and its quantity; logs/position_state.json is authoritative for
    the entry CONTEXT (real entry time/price, SPY level at entry, atr5 at
    entry, decision quote). A REST-only recovery loses all of that — the
    SPY-level stop gets disabled and TP/stop/trail run off Alpaca's
    day-average basis instead of the actual fill.
    """
    persisted = BotState.load_persisted_position()
    try:
        positions = order_manager.get_open_positions()
    except Exception as e:
        logger.warning("Position recovery check failed: %s", e)
        return

    found = False
    for p in positions:
        if p.asset_class != AssetClass.US_OPTION:
            continue
        symbol = p.symbol
        side, strike = parse_occ_symbol(symbol)
        if side is None:
            logger.warning("Could not parse recovered position symbol: %s", symbol)
            continue
        broker_qty = int(float(p.qty))

        # One constructor for both recovery modes: broker-derived identity
        # plus a metadata overlay from the state file when it matches. Two
        # hand-maintained constructors is how a new Position field silently
        # misses the rarer degraded branch.
        meta = persisted if (persisted and persisted.get("symbol") == symbol) else {}
        pos = Position(
            symbol          = symbol,
            side            = side,
            strike          = strike,
            qty             = broker_qty,          # broker qty is authoritative
            entry_price     = float(meta.get("entry_price", 0.0) or p.avg_entry_price),
            entry_time      = (datetime.datetime.fromisoformat(meta["entry_time"])
                               if meta.get("entry_time")
                               else datetime.datetime.now(tz=config.ET)),   # approx
            order_id        = meta.get("order_id", "recovered"),
            entry_spy_price = float(meta.get("entry_spy_price", 0.0)),
            entry_atr5      = float(meta.get("entry_atr5", 0.0)),
            entry_bid       = float(meta.get("entry_bid", 0.0)),
            entry_ask       = float(meta.get("entry_ask", 0.0)),
            peak_mid        = float(meta.get("peak_mid", 0.0)),
        )
        if meta:
            logger.info(
                "RECOVERED position (full metadata): %s entry=%.2f qty=%d "
                "entry_spy=%.2f atr5=%.3f | TP=%.2f Stop=%.2f",
                symbol, pos.entry_price, broker_qty,
                pos.entry_spy_price, pos.entry_atr5,
                round(pos.entry_price * config.TP_MULT,   2),
                round(pos.entry_price * config.STOP_MULT, 2),
            )
        else:
            logger.warning(
                "RECOVERED position (DEGRADED — no state file): %s entry=%.2f qty=%d | "
                "TP=%.2f Stop=%.2f | SPY-level stop DISABLED (entry SPY unknown)",
                symbol, pos.entry_price, broker_qty,
                round(pos.entry_price * config.TP_MULT,   2),
                round(pos.entry_price * config.STOP_MULT, 2),
            )
        bot_state.open_position(pos)
        found = True
        break   # only one position at a time

    if not found and persisted:
        logger.warning(
            "Stale position state file for %s (no matching broker position) — clearing.",
            persisted.get("symbol"),
        )
        bot_state._clear_persisted_position()


# ── Dynamic re-subscription watcher ──────────────────────────────────────────

async def _resubscribe_watcher():
    """
    After market open, watches SPY price and re-subscribes the option window
    whenever SPY moves ±_RESUB_THRESHOLD points from the last subscription price.

    Rules:
      - Never resubscribes from inside a stream callback (safe — standalone task).
      - Always keeps the currently held symbol subscribed, regardless of where
        SPY has moved, so the exit monitor's quote feed is never interrupted.
      - Alpaca deduplicates internally — passing already-subscribed symbols is harmless.
    """
    global _last_sub_spy_price, _current_subscriptions

    logger.info("Re-subscription watcher waiting for market open...")
    await _market_open_event.wait()
    _last_sub_spy_price = bot_state.spy_price
    logger.info("Re-subscription watcher active. Anchor SPY=%.2f threshold=±%.1f pts",
                _last_sub_spy_price, _RESUB_THRESHOLD)

    while True:
        await asyncio.sleep(10)

        spy = bot_state.spy_price
        if spy <= 0 or _last_sub_spy_price <= 0:
            continue

        move = abs(spy - _last_sub_spy_price)
        if move < _RESUB_THRESHOLD:
            continue

        logger.info(
            "Re-subscribe triggered: SPY moved %.2f pts (anchor=%.2f → now=%.2f)",
            move, _last_sub_spy_price, spy,
        )

        new_strikes = strikes.compute_dynamic_strikes(spy, _baseline_atr)
        new_symbols = new_strikes["call_symbols"] + new_strikes["put_symbols"]

        # Always keep the held symbol — exit monitor depends on its quotes
        pos      = bot_state.position
        held_sym = pos.symbol if pos else None
        if held_sym and held_sym not in new_symbols:
            new_symbols.append(held_sym)
            logger.info("Held symbol pinned in subscription: %s", held_sym)

        # Update routing table first (in-memory, always safe)
        _current_subscriptions = _build_routing_table(new_strikes)
        _last_sub_spy_price    = spy

        # Attempt WebSocket expansion in a thread executor with a 5-second timeout.
        # subscribe_quotes() is a synchronous call that can block the event loop
        # indefinitely if the WebSocket is in a bad state — confirmed on Jun 1 & 2.
        # Running in an executor isolates the block to a thread; wait_for cancels
        # it after 5 seconds so the event loop (and bar stream) is never frozen.
        # Routing table is already updated above, so entry logic stays correct
        # even if the WebSocket expansion fails or times out.
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_feed.add_option_symbols, new_symbols),
                timeout=5.0,
            )
            if _recorder is not None:
                _recorder.record_subscription(new_symbols)
            logger.info(
                "Re-subscribed: call=%.2f put=%.2f | %d symbols total",
                new_strikes["call_strike"], new_strikes["put_strike"], len(new_symbols),
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Re-subscribe timed out after 5s — routing table updated, WebSocket unchanged"
            )
        except Exception as e:
            logger.warning("Re-subscribe WebSocket call failed (routing table updated): %s", e)


# ── Market-open option subscriber ────────────────────────────────────────────

async def _option_subscriber():
    """
    Waits for the first 9:30 ET bar, then subscribes to option quotes.
    Runs as a separate task — NOT inside the stream callback chain,
    so calling subscribe_options() here is safe (no deadlock).
    """
    logger.info("Option subscriber waiting for market open (09:30 ET)...")
    await _market_open_event.wait()

    strikes_dict = _open_bar_strikes
    all_syms = strikes_dict["call_symbols"] + strikes_dict["put_symbols"]

    # If we recovered a position on restart, pin its symbol even if outside window
    pos      = bot_state.position
    held_sym = pos.symbol if pos else None
    if held_sym and held_sym not in all_syms:
        all_syms.append(held_sym)
        logger.info("Recovered position symbol pinned at open subscription: %s", held_sym)

    # Populate routing table — each symbol keyed to its own actual strike
    _current_subscriptions.update(_build_routing_table(strikes_dict))

    _feed.subscribe_options(all_syms)
    if _recorder is not None:
        _recorder.record_subscription(all_syms)   # feed_monitor's coverage input
    logger.info(
        "Subscribed at open: call=%.2f put=%.2f | %d symbols",
        strikes_dict["call_strike"], strikes_dict["put_strike"], len(all_syms),
    )
    # One-shot task — sleep until cancelled so _guarded doesn't restart it
    await asyncio.sleep(float("inf"))


# ── Ghost position sweeper (bar-driven) ──────────────────────────────────────

async def _check_ghost_positions():
    """
    Called on every 1-min bar close. Fetches all open option positions from
    Alpaca REST and closes any that are NOT tracked in bot_state.

    Ghost positions arise when a buy fills on Alpaca after a CancelledError
    disrupts local tracking. Without this sweeper they sit open and
    unmonitored until the next restart (potentially hours).

    SCOPE: only THIS BOT's universe — the configured underlying with today's
    expiry. The account may hold other real positions (manual trades, other
    strategies, longer-dated options); sweeping account-wide would liquidate
    them. Foreign positions are logged once per symbol, never touched.

    Stand-down conditions (both re-checked AFTER the REST await):
      - exit_pending:  a tracked close is in flight — don't interfere
      - entry_pending: a buy may have filled on Alpaca an instant before
        local tracking exists; sweeping now would close a REAL position
    Single-flight: overlapping sweeps (slow REST + 1-min cadence) are skipped.
    """
    if bot_state.exit_pending or bot_state.entry_pending:
        return
    if _ghost_sweep_lock is None or _ghost_sweep_lock.locked():
        return
    async with _ghost_sweep_lock:
        alpaca_positions = await order_manager.get_open_positions_async()

        # State may have moved while we were polling — re-check before acting.
        if bot_state.exit_pending or bot_state.entry_pending:
            return

        for p in alpaca_positions:
            try:
                if p.asset_class != AssetClass.US_OPTION:
                    continue
                sym     = p.symbol
                qty     = int(float(p.qty))
                avg_px  = float(p.avg_entry_price or 0)
                tracked = bot_state.position

                # Out-of-scope positions are NOT ours to touch
                if (not sym.startswith(config.UNDERLYING)
                        or parse_occ_expiry(sym) != config.today_et()):
                    if sym not in _foreign_positions_seen:
                        _foreign_positions_seen.add(sym)
                        logger.info(
                            "Ghost sweep: ignoring out-of-scope position %s "
                            "(not %s 0DTE — not this bot's)", sym, config.UNDERLYING,
                        )
                    continue

                if tracked is not None and tracked.symbol == sym:
                    # Our known position — but reconcile QUANTITY: a
                    # partial-fill adoption race can leave the broker holding
                    # more contracts than we track; the excess is unmanaged.
                    excess = qty - tracked.qty_remaining
                    if excess > 0:
                        logger.warning(
                            "QTY MISMATCH: broker holds %d of %s, tracked %d — "
                            "closing %d excess contract(s)",
                            qty, sym, tracked.qty_remaining, excess,
                        )
                        order = await order_manager.close_position(sym, excess)
                        fill  = order_manager.get_fill_price(order)
                        if fill is not None:
                            pnl = (fill - avg_px) * excess * 100
                            logger.warning("GHOST CLOSED: %s fill=%.2f pnl=$%.2f",
                                           sym, fill, pnl)
                    continue

                logger.warning(
                    "GHOST POSITION DETECTED: %s qty=%d avg=%.2f — closing immediately",
                    sym, qty, avg_px,
                )
                order = await order_manager.close_position(sym, qty)
                fill  = order_manager.get_fill_price(order)
                if fill is not None:
                    pnl = (fill - avg_px) * qty * 100
                    logger.warning(
                        "GHOST CLOSED: %s fill=%.2f pnl=$%.2f",
                        sym, fill, pnl,
                    )
                else:
                    logger.error(
                        "GHOST CLOSE UNCONFIRMED for %s — will retry next bar", sym,
                    )
            except Exception as e:
                logger.error("Ghost close failed for %s: %s",
                             p.symbol if hasattr(p, 'symbol') else '?', e)


# ── Centralized exit executor ─────────────────────────────────────────────────

async def _execute_exit(reason: str) -> bool:
    """
    THE single path from "exit decided" to "exit booked". All exit triggers
    (TP/stop/trail, SPY stop, staleness, time stop, shutdown) route here.

    Invariants:
      - Books into the CSV ONLY on a broker-confirmed fill (full or partial).
        No fill → no row. Never a guessed price.
      - A failed close keeps the position TRACKED and retries with backoff.
        It never erases local state while the broker still holds the position
        (that combination made the ghost sweeper re-close it and the P&L got
        counted twice — once fictitious, once real).
      - After _CLOSE_MAX_ATTEMPTS failures: leave the position tracked, lock
        the risk gate, log CRITICAL. The exit monitor keeps re-triggering.
    """
    pos = bot_state.position
    if pos is None or bot_state.exit_pending:
        return False
    # Set BEFORE the first await — asyncio is cooperative, so no other task
    # can run between the check above and this assignment.
    bot_state.exit_pending = True

    def _book(fill: float, fqty: int, order_id: str, exit_bid: float, exit_ask: float):
        """Book one confirmed leg; when the position fully closes, record the
        trade ONCE with the position's cumulative net P&L (partial legs are
        still one trade — double record_trade corrupts trades_today/cooldown)."""
        bot_state.book_exit_fill(
            fill, reason, qty=fqty, exit_order_id=order_id,
            exit_bid=exit_bid, exit_ask=exit_ask,
        )
        if bot_state.position is None:
            risk_manager.record_trade(pos.booked_pnl)
            icon = "✅" if pos.booked_pnl >= 0 else "❌"
            logger.info(
                "%s %s: %s fill=%.2f pnl=$%.2f | daily=$%.2f trades=%d",
                icon, reason.upper(), pos.symbol, fill,
                pos.booked_pnl, risk_manager.daily_pnl, risk_manager.trades_today,
            )
            return True
        return False

    try:
        quote    = bot_state.get_quote(pos.symbol)
        exit_bid = quote.bid if quote else 0.0
        exit_ask = quote.ask if quote else 0.0

        for attempt in range(1, _CLOSE_MAX_ATTEMPTS + 1):
            order = await order_manager.close_position(pos.symbol, pos.qty_remaining)
            fill  = order_manager.get_fill_price(order)
            fqty  = order_manager.get_filled_qty(order)

            if fill is not None and fqty > 0:
                if _book(fill, fqty, str(order.id), exit_bid, exit_ask):
                    return True
                # Partial close confirmed — retry the remainder immediately
                logger.warning(
                    "EXIT partially filled (%d left) — retrying remainder "
                    "(attempt %d/%d)", bot_state.position.qty_remaining,
                    attempt, _CLOSE_MAX_ATTEMPTS,
                )
                continue

            # No confirmed fill. A timed-out market close is left LIVE (never
            # blindly cancelled), so it may have filled after we gave up —
            # reconcile against the broker before retrying: if the position
            # is gone, find the real fill in order history and book THAT.
            broker = await order_manager.get_open_positions_async()
            if not any(p.symbol == pos.symbol for p in broker):
                lost = await order_manager.find_recent_close_fill(pos.symbol)
                if lost is not None:
                    logger.warning(
                        "EXIT reconciled from order history: %s filled %d @ %.2f "
                        "after fill-wait gave up", pos.symbol,
                        order_manager.get_filled_qty(lost),
                        order_manager.get_fill_price(lost),
                    )
                    if _book(order_manager.get_fill_price(lost),
                             order_manager.get_filled_qty(lost),
                             str(lost.id), exit_bid, exit_ask):
                        return True
                    continue
                logger.critical(
                    "EXIT DESYNC: broker shows no %s position but no filled close "
                    "order found — keeping tracked. *** RECONCILE MANUALLY. ***",
                    pos.symbol,
                )

            logger.warning(
                "EXIT attempt %d/%d got no confirmed fill for %s (reason=%s) — retrying",
                attempt, _CLOSE_MAX_ATTEMPTS, pos.symbol, reason,
            )
            await asyncio.sleep(min(2 * attempt, 6))

        # All attempts exhausted. Position (or its remainder) stays TRACKED
        # and NOTHING extra is recorded — record_trade fires only when the
        # last leg eventually closes (the exit monitor keeps retriggering).
        risk_manager.lock("close orders failing — manual attention required")
        logger.critical(
            "EXIT FAILED after %d attempts: %s qty=%d (reason=%s). Position remains "
            "tracked; exit monitor will keep retrying. *** CHECK ALPACA. ***",
            _CLOSE_MAX_ATTEMPTS, pos.symbol,
            bot_state.position.qty_remaining if bot_state.position else 0, reason,
        )
        return False
    finally:
        # book_exit_fill clears exit_pending on full close; make sure a
        # failure path leaves the flag down so retries can run.
        if bot_state.position is not None:
            bot_state.exit_pending = False


# ── SPY-level stop (bar-driven) ───────────────────────────────────────────────

async def _evaluate_spy_stop(spy_close: float):
    """
    Fires on every 1-minute bar close. Exits the position if SPY has closed
    (SPY_STOP_ATR_MULT × atr5_at_entry) dollars past the entry SPY price:
      - Call: SPY close < entry_spy_price - buf
      - Put:  SPY close > entry_spy_price + buf

    Buffer scales with intrabar volatility at entry so the stop is tighter on
    calm entries and wider on choppy ones — reducing whipsaw false-stops.
    SPY_STOP_FLOOR prevents a near-zero early-session atr5 from collapsing buf.
    """
    pos = bot_state.position
    if pos is None or bot_state.exit_pending:
        return
    if pos.entry_spy_price <= 0:
        return

    buf = max(config.SPY_STOP_FLOOR, config.SPY_STOP_ATR_MULT * pos.entry_atr5)
    if pos.side == "call" and spy_close >= pos.entry_spy_price - buf:
        return
    if pos.side == "put"  and spy_close <= pos.entry_spy_price + buf:
        return

    logger.info(
        "SPY STOP: side=%s entry_spy=%.2f current_spy=%.2f buf=%.2f (atr5=%.3f × %.2f)",
        pos.side, pos.entry_spy_price, spy_close, buf,
        pos.entry_atr5, config.SPY_STOP_ATR_MULT,
    )
    await _execute_exit("spy_stop")


# ── Exit evaluation (quote-driven) ────────────────────────────────────────────

async def _evaluate_exit(quote):
    """
    Evaluates TP / stop / peak trail on every option quote tick for the
    held symbol. Called via asyncio.create_task() from on_option_quote,
    so close_position() never executes inside the stream callback.
    Order submission and booking are delegated to _execute_exit().
    """
    pos = bot_state.position
    if pos is None or bot_state.exit_pending:
        return
    if quote is None:
        return

    mid        = quote.mid
    tp_price   = pos.entry_price * config.TP_MULT
    stop_price = pos.entry_price * config.STOP_MULT

    # Wide-spread / one-sided-quote handling: a mid computed inside a
    # blown-out spread is not a price, so trail decisions and peak updates
    # skip the tick. But the position does NOT go blind — dislocations are
    # exactly when exits must keep working — decisions fall back to the
    # EXECUTABLE side:
    #   - stop on the bid (what a market sell receives). When the bid is
    #     pulled entirely (bid=0), Quote.mid falls back to the ask — if even
    #     the ask is at/below the stop, the position is gone; exit.
    #   - TP on the bid: if the bid ALONE clears the target, that gain is
    #     executable regardless of how wide the ask is.
    if quote.spread_pct > config.EXIT_WIDE_SPREAD_PCT:
        executable = quote.bid if quote.bid > 0 else mid
        if executable > 0 and executable <= stop_price:
            logger.warning(
                "STOP on wide/one-sided spread: executable=%.2f <= stop=%.2f "
                "(bid=%.2f ask=%.2f)",
                executable, stop_price, quote.bid, quote.ask,
            )
            await _execute_exit("stop")
        elif quote.bid >= tp_price:
            logger.info(
                "TP on wide spread: bid=%.2f >= tp=%.2f — gain is executable",
                quote.bid, tp_price,
            )
            await _execute_exit("tp")
        return

    # Update peak mid — monotonically increasing, safe under concurrency
    if mid > pos.peak_mid:
        pos.peak_mid = mid

    # Track min/max unrealized P&L for terminal display
    unreal_pnl = (mid - pos.entry_price) * pos.qty_remaining * 100
    if unreal_pnl < pos.min_unreal_pnl:
        pos.min_unreal_pnl = unreal_pnl
    if unreal_pnl > pos.max_unreal_pnl:
        pos.max_unreal_pnl = unreal_pnl

    if mid >= tp_price:
        reason = "tp"
    elif mid <= stop_price:
        reason = "stop"
    elif pos.peak_mid >= pos.entry_price * config.PEAK_TRAIL_ACTIVATE:
        trail_stop = pos.peak_mid * config.PEAK_TRAIL_PCT
        if mid <= trail_stop:
            logger.info(
                "PEAK TRAIL: mid=%.2f peak=%.2f trail_stop=%.2f entry=%.2f",
                mid, pos.peak_mid, trail_stop, pos.entry_price,
            )
            reason = "peak_trail"
        else:
            return
    else:
        return

    logger.info("EXIT signal: reason=%s mid=%.2f tp=%.2f stop=%.2f",
                reason, mid, tp_price, stop_price)
    await _execute_exit(reason)


# ── Exit monitor (30-second safety net) ───────────────────────────────────────

async def _exit_monitor():
    """
    Fallback safety net — fires every 30 seconds in case option quotes
    stop arriving (WebSocket hiccup, reconnect gap). Normal exits are
    handled quote-driven via _evaluate_exit() called from on_option_quote.
    """
    while True:
        await asyncio.sleep(30)

        pos = bot_state.position
        if pos is None or bot_state.exit_pending:
            continue

        quote = bot_state.get_quote(pos.symbol)
        if quote is None:
            continue

        asyncio.create_task(_evaluate_exit(quote))


# ── Data staleness kill switch ────────────────────────────────────────────────

async def _staleness_watcher():
    """
    The exit monitor re-evaluates CACHED quotes — if the option stream dies
    silently (no exception, just no messages), it chews the same stale quote
    forever while the position flies blind. This watcher measures actual
    receive-time age and acts:

      - Holding + no fresh quote for the held symbol in
        STALE_QUOTE_FLATTEN_SEC → flatten via _execute_exit ("stale_data").
        close_position() needs no quotes, so this works even with a dead feed.
      - No SPY bar in STALE_BAR_WARN_SEC during the session → lock new
        entries (bars drive the SPY stop and the ghost sweeper) and log
        CRITICAL. Existing quote-driven exits keep working.
    """
    await _market_open_event.wait()
    no_quote_since: float = 0.0   # first time we saw a held position with NO cached quote
    while True:
        await asyncio.sleep(5)
        now_m = clock.monotonic()   # same axis as Quote.recv_monotonic

        now_hhmm = datetime.datetime.now(tz=config.ET).strftime("%H:%M")
        if now_hhmm >= config.TIME_STOP:
            continue   # time-stop path owns the endgame

        pos = bot_state.position
        if pos is not None and not bot_state.exit_pending:
            q = bot_state.get_quote(pos.symbol)
            if q is not None and q.recv_monotonic > 0:
                no_quote_since = 0.0
                age = now_m - q.recv_monotonic
                if age > config.STALE_QUOTE_FLATTEN_SEC:
                    logger.critical(
                        "DATA STALENESS: no quote for held %s in %.0fs — flattening",
                        pos.symbol, age,
                    )
                    await _execute_exit("stale_data")
            else:
                # NO quote has EVER arrived for the held symbol (recovered
                # position + dead/failed subscription). The age check can't
                # run, so time it ourselves — this is the fully-blind case
                # the kill switch exists for.
                if no_quote_since == 0.0:
                    no_quote_since = now_m
                elif now_m - no_quote_since > config.STALE_QUOTE_FLATTEN_SEC:
                    logger.critical(
                        "DATA STALENESS: held %s has received NO quotes for %.0fs "
                        "— flattening blind (market close needs no quotes)",
                        pos.symbol, now_m - no_quote_since,
                    )
                    await _execute_exit("stale_data")
        else:
            no_quote_since = 0.0

        if bot_state.last_bar_monotonic > 0:
            bar_age = now_m - bot_state.last_bar_monotonic
            if bar_age > config.STALE_BAR_WARN_SEC and not risk_manager.locked:
                logger.critical(
                    "DATA STALENESS: no SPY bar in %.0fs — locking new entries "
                    "(SPY stop and ghost sweeper are bar-driven)", bar_age,
                )
                risk_manager.lock("SPY bar stream stale")


# ── Scheduled-event watcher ───────────────────────────────────────────────────

async def _event_watcher():
    """
    On FOMC statement days, flattens any open position at FOMC_FLATTEN_TIME
    (default 13:45 ET — 15 minutes before the 14:00 statement). Long 0DTE
    gamma through the statement is a headline coin flip; the entry blackout
    (13:30) stops NEW positions, this stops EXISTING ones. One-shot per day:
    after the flatten window opens it keeps sweeping, so a position that
    somehow appears later (recovery, race) is still flattened.
    """
    if not (config.EVENT_BLACKOUT_ENABLED and config.FOMC_FLATTEN_POSITIONS):
        await asyncio.sleep(float("inf"))
    if not event_calendar.is_fomc_day(config.today_et()):
        await asyncio.sleep(float("inf"))
    logger.info("Event watcher armed: FOMC day — flatten at %s ET, "
                "entry blackout from %s ET",
                config.FOMC_FLATTEN_TIME, config.FOMC_ENTRY_BLACKOUT_START)
    while True:
        await asyncio.sleep(5)
        why = event_calendar.should_flatten_for_event(clock.now_et())
        if why is None:
            continue
        if bot_state.position is not None and not bot_state.exit_pending:
            logger.warning("EVENT FLATTEN: closing position ahead of %s", why)
            await _execute_exit("event_flatten")


# ── Task wrapper ───────────────────────────────────────────────────────────────

async def _guarded(coro_factory, name: str, restart_delay: float = 5.0):
    while True:
        try:
            await coro_factory()
        except asyncio.CancelledError:
            logger.info("%s cancelled — stopping.", name)
            break
        except Exception as e:
            logger.error("%s crashed: %s — restarting in %.0fs", name, e, restart_delay)
            await asyncio.sleep(restart_delay)


# ── Periodic status display ───────────────────────────────────────────────────

async def _status_loop():
    """
    Print status block every 5 seconds when a position is open (you want
    to watch P&L tick), or every 60 seconds when flat (bar-level is enough).
    Also updates the watchdog heartbeat on every iteration.
    """
    global _last_heartbeat
    while True:
        await asyncio.sleep(5)
        _last_heartbeat = _time.time()   # proof-of-life for the watchdog thread
        if bot_state.position is not None:
            _print_status()
        else:
            # Only print once per minute when no position
            now = datetime.datetime.now(tz=config.ET)
            if now.second < 5:   # fires in the first 5s of each minute
                _print_status()


def _print_status():
    now_et = datetime.datetime.now(tz=config.ET).strftime("%H:%M:%S")
    m      = momentum_engine.state
    pos    = bot_state.position
    spy    = bot_state.spy_price

    # Direction indicator
    dir_str = {"bull": "▲ BULL", "bear": "▼ BEAR", "neutral": "── NEUT"}.get(m.direction, m.direction)

    lines = [
        "─" * 60,
        f"  {now_et} ET  |  SPY ${spy:.2f}  |  {dir_str}  |  VWAP ${m.vwap:.2f}",
        f"  EMA5 ${m.ema5:.2f}  EMA20 ${m.ema20:.2f}  |  ROC {m.roc5:+.4f}  |  "
        f"consec {'+' if m.consec_green else '-'}{m.consec_green or m.consec_red}",
    ]

    if pos:
        quote       = bot_state.get_quote(pos.symbol)
        current_mid = quote.mid if quote else pos.entry_price
        unreal_pnl  = (current_mid - pos.entry_price) * pos.qty_remaining * 100
        pct_chg     = (current_mid / pos.entry_price - 1) * 100 if pos.entry_price else 0
        tp_price    = round(pos.entry_price * config.TP_MULT,   2)
        stop_price  = round(pos.entry_price * config.STOP_MULT, 2)
        pnl_sign    = "+" if unreal_pnl >= 0 else ""
        elapsed     = datetime.datetime.now(tz=config.ET) - pos.entry_time
        total_secs  = int(elapsed.total_seconds())
        time_in_trade = f"{total_secs // 60}m {total_secs % 60:02d}s"
        # Peak trailing stop display
        trail_armed = pos.peak_mid >= pos.entry_price * config.PEAK_TRAIL_ACTIVATE
        trail_stop  = round(pos.peak_mid * config.PEAK_TRAIL_PCT, 2) if trail_armed else None
        trail_str   = (f"Trail ${trail_stop:.2f} (peak ${pos.peak_mid:.2f})" if trail_armed
                       else f"Trail ARMED @ ${pos.entry_price * config.PEAK_TRAIL_ACTIVATE:.2f}")

        # Min/max unrealized P&L
        min_sign = "+" if pos.min_unreal_pnl >= 0 else ""
        max_sign = "+" if pos.max_unreal_pnl >= 0 else ""

        # SPY stop level (adaptive: 0.75 × atr5_at_entry, floored at SPY_STOP_FLOOR)
        if pos.entry_spy_price > 0:
            spy_buf = max(config.SPY_STOP_FLOOR, config.SPY_STOP_ATR_MULT * pos.entry_atr5)
            spy_stop_level = (
                round(pos.entry_spy_price - spy_buf, 2)
                if pos.side == "call"
                else round(pos.entry_spy_price + spy_buf, 2)
            )
            spy_arrow    = "↓" if pos.side == "call" else "↑"
            spy_stop_str = f"Entry SPY ${pos.entry_spy_price:.2f}  SPY stop {spy_arrow}${spy_stop_level:.2f}"
        else:
            spy_stop_str = "SPY stop OFF (degraded recovery)"

        lines += [
            "  " + "·" * 56,
            f"  POSITION: {pos.symbol}  ({pos.side.upper()} ${pos.strike:.0f})  |  in trade {time_in_trade}",
            f"  Entry ${pos.entry_price:.2f}  ×  {pos.qty_remaining} contracts  |  {spy_stop_str}",
            f"  Mid   ${current_mid:.2f}  ({pct_chg:+.0f}%)  |  "
            f"TP ${tp_price:.2f}  Stop ${stop_price:.2f}  |  {trail_str}",
            f"  Unrealised P&L: {pnl_sign}${unreal_pnl:.2f}  |  "
            f"min {min_sign}${pos.min_unreal_pnl:.2f}  max {max_sign}${pos.max_unreal_pnl:.2f}",
        ]
    else:
        cooldown = getattr(risk_manager, "_cooldown_bars", 0)
        status   = f"cooldown {cooldown} bars" if cooldown else "ready to trade"
        lines.append(f"  NO POSITION  |  {status}")

    lines += [
        "  " + "·" * 56,
        f"  Daily P&L ${risk_manager.daily_pnl:+.2f}  |  "
        f"Trades {risk_manager.trades_today}  |  "
        f"Gate {'🔒 LOCKED' if risk_manager.locked else '🟢 open'}",
        "─" * 60,
    ]

    print("\n".join(lines), flush=True)


# ── Time stop ─────────────────────────────────────────────────────────────────

async def _wait_for_pending_ops(timeout: float, context: str) -> None:
    """
    Bounded wait for any in-flight entry or exit to finish before an endgame
    path (time stop, shutdown) acts. Acting while an operation is in flight
    causes real damage: cancelling an in-flight close order and then no-oping
    on the exit_pending guard leaves the position open; racing a mid-retry
    exit submits duplicate closes and books twice; sweeping during an entry
    fill closes an untracked-but-real position.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while ((bot_state.exit_pending or bot_state.entry_pending)
           and asyncio.get_running_loop().time() < deadline):
        logger.info("%s: waiting for in-flight %s to finish...",
                    context, "exit" if bot_state.exit_pending else "entry")
        await asyncio.sleep(1)
    if bot_state.exit_pending or bot_state.entry_pending:
        logger.warning("%s: in-flight operation still pending after %.0fs — proceeding",
                       context, timeout)


async def _time_stop_watcher():
    while True:
        await asyncio.sleep(10)
        now_et = datetime.datetime.now(tz=config.ET).strftime("%H:%M")
        if now_et >= config.TIME_STOP:
            logger.info("TIME STOP reached (%s). Closing all positions.", config.TIME_STOP)
            # Let any in-flight entry/exit settle first: an entry may be about
            # to produce a position (which we must then close and book), and
            # racing an in-flight exit would double-close and double-book.
            await _wait_for_pending_ops(60, "TIME STOP")
            await _execute_exit("time_stop")
            await _confirm_flat_and_exit()
            return   # unreachable — _confirm_flat_and_exit calls os._exit


async def _confirm_flat_and_exit():
    """
    Called once TIME STOP has force-closed the tracked position. Confirms via
    Alpaca REST that no option position remains, then hard-exits the process.

    Why hard-exit instead of feed.stop():
      The normal feed teardown can hang the event loop for >20s, which trips
      the watchdog into a restart loop. Each restart boots past TIME_STOP and
      immediately re-fires the time stop, hanging again — observed looping 15×
      on Jun 30. os._exit(0) sidesteps the teardown entirely: the daemon
      watchdog thread dies with the process, so there is no restart loop.

    Safety: we only exit after Alpaca confirms flat. If a position somehow
    survived the force-close, we retry the close a few times — and if the
    residual is OUR tracked position, a confirmed fill is booked properly so
    the record stays truthful. We still exit afterwards (a process past TIME
    STOP can do nothing useful), but the WARNING makes any residual position
    visible for manual handling.
    """
    MAX_CLOSE_ATTEMPTS = 8
    for attempt in range(1, MAX_CLOSE_ATTEMPTS + 1):
        try:
            open_opts = [
                p for p in await order_manager.get_open_positions_async()
                if p.asset_class == AssetClass.US_OPTION
            ]
        except Exception as e:
            logger.error("TIME STOP flat-check REST call failed: %s", e)
            await asyncio.sleep(2)
            continue

        if not open_opts:
            logger.info("TIME STOP: confirmed flat on Alpaca — exiting cleanly.")
            order_manager.cancel_all_options()   # clear any dangling limit orders
            alerts.flush()   # last chance before the hard exit
            os._exit(0)

        for p in open_opts:
            logger.warning(
                "TIME STOP: position still open after force-close: %s qty=%s — "
                "retrying close (attempt %d/%d)",
                p.symbol, p.qty, attempt, MAX_CLOSE_ATTEMPTS,
            )
            try:
                pos = bot_state.position
                if pos is not None and pos.symbol == p.symbol:
                    # OUR tracked residual — route through the single booking
                    # path (confirmed fills, partial retries, one record_trade
                    # per position) rather than re-implementing it inline.
                    await _execute_exit("time_stop")
                else:
                    # Untracked residual — hand-close, log only (ghost class)
                    await order_manager.close_position(p.symbol, int(float(p.qty)))
            except Exception as e:
                logger.error("TIME STOP retry close failed for %s: %s", p.symbol, e)
        await asyncio.sleep(2)

    logger.warning(
        "TIME STOP: could NOT confirm flat after %d retries — exiting anyway. "
        "*** CHECK ALPACA for a residual open option position. ***",
        MAX_CLOSE_ATTEMPTS,
    )
    alerts.flush()   # the residual-position warning MUST escape
    os._exit(0)


# ── Feed entitlement probe ────────────────────────────────────────────────────

def _validate_feed_entitlements(stock_client, option_client, today):
    """One cheap REST request per non-default feed. A missing subscription
    surfaces here as a clear SystemExit, not a 09:30 stream failure."""
    from alpaca.data.requests import StockLatestQuoteRequest, OptionChainRequest

    if config.STOCK_FEED != "iex":
        try:
            stock_client.get_stock_latest_quote(StockLatestQuoteRequest(
                symbol_or_symbols=config.UNDERLYING, feed=stock_feed()))
            logger.info("Stock feed entitlement OK: %s", config.STOCK_FEED)
        except Exception as e:
            raise SystemExit(
                f"Stock feed {config.STOCK_FEED!r} not available on this account "
                f"({e}). Subscribe to Alpaca market data or set "
                f"ALPACA_STOCK_FEED=iex.")
    if config.OPTION_FEED != "indicative":
        try:
            option_client.get_option_chain(OptionChainRequest(
                underlying_symbol=config.UNDERLYING, expiration_date=today,
                feed=option_feed(),
                strike_price_gte=1.0, strike_price_lte=2.0))   # tiny probe window
            logger.info("Option feed entitlement OK: %s", config.OPTION_FEED)
        except Exception as e:
            raise SystemExit(
                f"Option feed {config.OPTION_FEED!r} not available on this account "
                f"({e}). Subscribe to Alpaca options data (OPRA) or set "
                f"ALPACA_OPTION_FEED=indicative.")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    global _feed, _baseline_atr, _market_open_event, _WATCHDOG_END
    global _entry_lock, _ghost_sweep_lock

    _entry_lock       = asyncio.Lock()   # created here, inside the running loop
    _ghost_sweep_lock = asyncio.Lock()

    config.validate_credentials()

    # ── Alerting ──────────────────────────────────────────────────────────────
    # Every CRITICAL log line (staleness flatten, EXIT FAILED, desync,
    # reconciliation failure) reaches the operator. Configure channels via
    # ALERT_WEBHOOK_URL / ALERT_EMAIL_TO — see alerts.py.
    if alerts.configure_from_env():
        alerts.attach_to_root_logger()
        logger.info("Alerting active — CRITICAL events will be delivered.")
    else:
        logger.warning(
            "Alerting NOT configured (no ALERT_WEBHOOK_URL / ALERT_EMAIL_TO) — "
            "kill-switch events will only appear in this log.")

    # ── Reconciliation gate ───────────────────────────────────────────────────
    # A failed nightly reconciliation means the local record and the broker
    # disagree. Trading does not resume on top of unexplained numbers.
    flag = reconcile_mod.pending_failure_flag()
    if flag:
        risk_manager.lock(f"unresolved reconciliation failure ({os.path.basename(flag)})")
        logger.critical(
            "RECONCILIATION FLAG present: %s — entry gate LOCKED. Investigate, "
            "then clear with: python reconcile.py --clear", flag,
        )

    # ── Trading calendar gate ─────────────────────────────────────────────────
    # A 0DTE bot must not run on a non-session day (its symbols won't exist),
    # and on early-close days every close-anchored time must shift with the
    # session — a 15:25 time stop after a 13:00 close means the position is
    # held into expiry.
    today = config.today_et()
    if not market_calendar.covers(today):
        raise SystemExit(
            f"market_calendar tables do not cover {today.year}. "
            f"Extend market_calendar.py before trading — refusing to guess "
            f"holidays/early closes."
        )
    if not market_calendar.is_trading_day(today):
        if os.environ.get("FORCE_RUN") == "1":
            logger.warning(
                "%s is not a trading day — running anyway (FORCE_RUN=1, dev only).",
                today,
            )
        else:
            logger.info("%s is not a trading day (weekend/holiday) — exiting.", today)
            return
    if market_calendar.near_horizon(today):
        logger.warning(
            "CALENDAR HORIZON: market_calendar tables end soon (%d) — extend "
            "market_calendar.py now or the bot will refuse to start next year.",
            today.year,
        )
    if market_calendar.is_early_close(today):
        old_stop = config.TIME_STOP
        config.ENTRY_END = market_calendar.shift_for_close(config.ENTRY_END, today)
        config.TIME_STOP = market_calendar.shift_for_close(config.TIME_STOP, today)
        _WATCHDOG_END    = market_calendar.shift_for_close(_WATCHDOG_END, today)
        logger.warning(
            "EARLY CLOSE day (13:00 ET session): entry_end=%s time_stop=%s (was %s)",
            config.ENTRY_END, config.TIME_STOP, old_stop,
        )

    # ── Scheduled events for this session ─────────────────────────────────────
    events_today = event_calendar.todays_events(today)
    if events_today:
        logger.warning("SCHEDULED EVENTS today: %s", ", ".join(events_today))
        if event_calendar.is_fomc_day(today) and config.EVENT_BLACKOUT_ENABLED:
            logger.warning(
                "FOMC day: entry blackout from %s ET%s",
                config.FOMC_ENTRY_BLACKOUT_START,
                f", flatten at {config.FOMC_FLATTEN_TIME} ET"
                if config.FOMC_FLATTEN_POSITIONS else "",
            )
        if (event_calendar.premarket_events(today)
                and config.PREMARKET_EVENT_OPEN_DELAY_MIN > 0):
            h, m = map(int, config.ENTRY_START.split(":"))
            shifted = (datetime.datetime.combine(today, datetime.time(h, m))
                       + datetime.timedelta(minutes=config.PREMARKET_EVENT_OPEN_DELAY_MIN))
            config.ENTRY_START = shifted.strftime("%H:%M")
            logger.warning("Premarket event day: ENTRY_START delayed to %s ET",
                           config.ENTRY_START)
    else:
        logger.info("No scheduled macro events today.")

    _market_open_event = asyncio.Event()
    risk_manager.reset_day()
    _restore_daily_pnl()   # replay today's closed trades after a restart

    stock_client  = StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)
    option_client = OptionHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)

    # ── Feed entitlement probe ────────────────────────────────────────────────
    # Paid feeds (SIP/OPRA) fail at stream-connect time with an opaque error
    # if the account lacks the market-data subscription. Probe cheaply NOW
    # and fail with an actionable message instead of dying at 09:30.
    _validate_feed_entitlements(stock_client, option_client, today)

    # Daily ATR baseline
    logger.info("Fetching daily ATR baseline...")
    _baseline_atr = strikes.startup_atr(stock_client)
    logger.info("Baseline ATR: %.2f", _baseline_atr)

    # Prime the option-chain cache so dynamically built strikes are validated
    # against contracts that actually exist (see strikes.prime_chain_cache).
    logger.info("Priming option chain cache for %s...", today)
    strikes.prime_chain_cache(option_client, today)

    # Pre-seed EMAs with last 30 RTH 1-min bars
    logger.info("Pre-seeding momentum engine...")
    seed_bars = []
    try:
        seed_end   = datetime.datetime.now(tz=config.ET)
        seed_start = seed_end - datetime.timedelta(days=5)  # 5 days covers Mon→Fri lookback
        seed_req   = StockBarsRequest(
            symbol_or_symbols = config.UNDERLYING,
            timeframe         = TimeFrame.Minute,
            start             = seed_start,
            end               = seed_end,
            feed              = stock_feed(),
            adjustment        = Adjustment.RAW,   # unadjusted — consistent with
                                                  # live stream + strike grid
        )
        raw = stock_client.get_stock_bars(seed_req)[config.UNDERLYING]
        raw = [b for b in raw
               if market_calendar.ET_OPEN
               <= b.timestamp.astimezone(config.ET).time()
               < market_calendar.ET_REGULAR_CLOSE]
        raw = raw[-30:]
        seed_bars = [
            Bar(t=b.timestamp, open=float(b.open), high=float(b.high),
                low=float(b.low), close=float(b.close), volume=float(b.volume))
            for b in raw
        ]
        momentum_engine.preseed(seed_bars)
        if seed_bars:
            bot_state.spy_price = seed_bars[-1].close
    except Exception as e:
        logger.warning("Pre-seed failed (%s) — EMAs will warm from live bars.", e)

    # ── Market data recorder ──────────────────────────────────────────────────
    # Captures every bar/quote the decision code receives, plus the session's
    # full provenance (config snapshot, ATR baseline, preseed bars, chain), so
    # replay.py can reproduce this session through the same code paths.
    global _recorder
    if config.RECORD_MARKET_DATA:
        try:
            _recorder = recorder_mod.MarketDataRecorder(
                recorder_mod.default_recording_path(config.RECORDINGS_DIR, today))
            _recorder.record_meta({
                "session_date":  today.isoformat(),
                "baseline_atr":  _baseline_atr,
                "paper":         config.PAPER,
                "stock_feed":    config.STOCK_FEED,
                "option_feed":   config.OPTION_FEED,
                "events":        events_today,
                "config":        {k: v for k, v in vars(config).items()
                                  if k.isupper()
                                  and isinstance(v, (int, float, str, bool))},
                "preseed_bars":  [[b.t.isoformat(), b.open, b.high, b.low,
                                   b.close, b.volume] for b in seed_bars],
                "chain_symbols": sorted(strikes._chain_cache.get(today) or []),
            })
        except Exception as e:
            logger.error("Recorder init failed (%s) — trading continues UNRECORDED.", e)
            _recorder = None

    # Cancel any pending option orders left over from a previous crash.
    # This clears ghost orders that may have been submitted but not filled
    # (or filled after a timeout) before the previous session ended.
    logger.info("Cancelling any pending option orders from previous session...")
    order_manager.cancel_all_options()

    # Check for any position left open from a previous run (e.g. after 'r' restart)
    _recover_open_position()

    logger.info("Startup complete — waiting for 09:30 ET market open to subscribe options.")

    _feed = FeedManager(
        on_spy_bar      = on_spy_bar,
        on_option_quote = on_option_quote,
        on_trade_update = on_trade_update,
    )

    loop = asyncio.get_running_loop()
    _shutdown_event = asyncio.Event()

    async def _shutdown(reason: str = "signal"):
        """Full shutdown — closes positions, then cancels orders, then exits.

        Ordering matters: an in-flight exit's close order must NOT be
        cancelled out from under it (that left the position open while the
        exit_pending guard made the follow-up close a no-op), so we first
        wait for in-flight operations, then close, and only cancel leftover
        orders after our own exits are done."""
        if _shutdown_event.is_set():
            return
        logger.info("Shutdown (%s). Settling in-flight operations...", reason)
        await _wait_for_pending_ops(20, "SHUTDOWN")
        if bot_state.position is not None:
            await _execute_exit("shutdown")
        await order_manager.cancel_all_options_async()
        _shutdown_event.set()
        for task in asyncio.all_tasks(loop):
            if task is not asyncio.current_task():
                task.cancel()

    async def _soft_shutdown(reason: str = "restart"):
        """
        Soft shutdown for restart — cancels unfilled orders but leaves open
        positions on Alpaca. They will be recovered automatically on next
        startup (full metadata via logs/position_state.json).
        """
        if _shutdown_event.is_set():
            return
        logger.info("Soft shutdown (%s). Leaving positions open for recovery.", reason)
        await order_manager.cancel_all_options_async()   # cancel any pending limit orders
        _shutdown_event.set()
        for task in asyncio.all_tasks(loop):
            if task is not asyncio.current_task():
                task.cancel()

    def _signal_shutdown():
        async def _do():
            await _shutdown("Ctrl+C / SIGTERM")
            alerts.flush(1.0)
            os._exit(0)
        asyncio.create_task(_do())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_shutdown)

    def _keyboard_watcher():
        print("  >> Bot running.  q = quit  |  r = restart (keeps positions)  |  t = trades")
        while not _shutdown_event.is_set():
            try:
                # Poll stdin with 1s timeout — never blocks indefinitely
                ready, _, _ = select.select([sys.stdin], [], [], 1.0)
                if ready:
                    line = sys.stdin.readline().strip().lower()
                    if line == "q":
                        logger.info("Keyboard quit requested.")
                        future = asyncio.run_coroutine_threadsafe(_shutdown("keyboard"), loop)
                        try:
                            # _shutdown may wait up to 20s for an in-flight
                            # exit plus close retries — give it room
                            future.result(timeout=45)
                        except Exception:
                            # Event loop may be frozen — force exit regardless
                            logger.warning("Event loop unresponsive — forcing exit.")
                        alerts.flush(1.0)
                        os._exit(0)
                    elif line == "r":
                        logger.info("Keyboard restart requested — positions left open for recovery.")
                        future = asyncio.run_coroutine_threadsafe(_soft_shutdown("restart"), loop)
                        try:
                            future.result(timeout=8)
                        except Exception:
                            # Event loop may be frozen — restart anyway, position
                            # stays on Alpaca and will be recovered on next startup.
                            logger.warning("Event loop unresponsive — forcing restart.")
                        alerts.flush(1.0)
                        os.execl(sys.executable, sys.executable, *sys.argv)
                    elif line == "t":
                        _print_trades()
            except Exception:
                break

    threading.Thread(target=_keyboard_watcher, daemon=True).start()

    # ── Watchdog thread ───────────────────────────────────────────────────────
    def _watchdog():
        """
        OS thread — runs independently of the asyncio event loop.
        If the event loop freezes (e.g. re-subscribe WebSocket hang),
        asyncio tasks stop updating _last_heartbeat. After WATCHDOG_TIMEOUT
        seconds of silence during market hours, restarts the process.
        Open positions stay on Alpaca and are recovered on next startup.
        """
        _time.sleep(30)   # give the bot time to initialise before watching
        while True:
            _time.sleep(10)
            now_et = datetime.datetime.now(tz=config.ET).strftime("%H:%M")
            if not (_WATCHDOG_START <= now_et <= _WATCHDOG_END):
                continue
            if _last_heartbeat <= 0:
                continue   # event loop hasn't started yet
            stale = _time.time() - _last_heartbeat
            if stale > _WATCHDOG_TIMEOUT:
                logger.warning(
                    "WATCHDOG: event loop silent for %.0fs — auto-restarting. "
                    "Open positions will be recovered on startup.",
                    stale,
                )
                alerts.alert(f"WATCHDOG restart: event loop silent {stale:.0f}s — "
                             "restarting; position (if any) recovers on startup")
                alerts.flush(2.0)
                _time.sleep(1)   # let the log flush
                os.execl(sys.executable, sys.executable, *sys.argv)

    threading.Thread(target=_watchdog, daemon=True, name="watchdog").start()
    logger.info(
        "Watchdog active — checks every 10s, restarts if silent >%ds "
        "between %s and %s ET (max downtime ~20s).",
        _WATCHDOG_TIMEOUT, _WATCHDOG_START, _WATCHDOG_END,
    )

    await asyncio.gather(
        _guarded(_feed.start,           "feed"),
        _guarded(_option_subscriber,    "option_subscriber"),
        _guarded(_resubscribe_watcher,  "resubscribe_watcher"),
        _guarded(_exit_monitor,         "exit_monitor"),
        _guarded(_staleness_watcher,    "staleness_watcher"),
        _guarded(_event_watcher,        "event_watcher"),
        _guarded(_status_loop,          "status_loop"),
        _guarded(_time_stop_watcher,    "time_stop_watcher"),
        return_exceptions=True,
    )

    logger.info("=" * 60)
    logger.info("SESSION COMPLETE | Trades: %d | P&L: $%.2f",
                risk_manager.trades_today, risk_manager.daily_pnl)
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
