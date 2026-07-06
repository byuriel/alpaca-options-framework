#!/usr/bin/env python3
"""
Deterministic session replay — the same live code, fed recorded market data.

    python replay.py recordings/session_2026-07-06.jsonl.gz
    python replay.py recordings/session_*.jsonl.gz --set TP_MULT=1.8 --set STOP_MULT=0.45
    python replay.py recordings/session_2026-07-06.jsonl.gz --out replay_out --quiet

Why this exists: backtests lie in the seams — bar timing, data availability,
restart behavior, fill assumptions. This harness does NOT reimplement the
strategy in a backtest dialect. It replays the recorded event stream through
main.on_spy_bar / main.on_option_quote — the identical functions the live
bot runs — with a simulated clock (clock.SimClock) and a simulated broker
(sim_broker.SimBroker) whose fills are conservative by construction: buys
cross the spread at the ask, sells hit the bid, resting limits fill only
when the recorded ask actually crosses.

Guarantees:
  - Deterministic: same recording + same config → identical fills and an
    identical trades CSV, every run (pinned by tests/test_replay.py).
  - Honest about misses: entries whose limit never crossed are counted in
    the summary (`unfilled_entries`), not silently skipped.
  - Parameter sweeps: --set KEY=VALUE overrides any config scalar for the
    run and restores it afterwards, so one recorded session answers "what
    would STOP_MULT=0.45 have done?" in seconds instead of a live day.

Scope (documented, not hidden): the simulated broker always resolves —
broker-failure machinery (close retries, order-history reconciliation,
ghost adoption) is exercised by unit tests, not by replay. Live-only
watchers (watchdog thread, status display, staleness flatten) don't run;
the 30-second exit-monitor cadence and the session time stop ARE simulated
because they change trade outcomes.
"""

import argparse
import asyncio
import csv
import datetime
import glob
import logging
import os
from typing import Optional

import clock
import config
import market_calendar
import recorder
import state as state_mod
import strikes
from momentum import Bar, MomentumEngine
from orb_filter import ORBFilter
from risk import RiskManager
from sim_broker import SimBroker
from state import BotState

logger = logging.getLogger("replay")

EXIT_MONITOR_INTERVAL = 30.0   # mirrors main._exit_monitor's live cadence


class _SimBar:
    __slots__ = ("timestamp", "open", "high", "low", "close", "volume")

    def __init__(self, ts, o, h, l, c, v):
        self.timestamp = ts
        self.open, self.high, self.low, self.close, self.volume = o, h, l, c, v


class _SimQuote:
    __slots__ = ("symbol", "bid_price", "ask_price", "timestamp")

    def __init__(self, symbol, bid, ask, ts):
        self.symbol    = symbol
        self.bid_price = bid
        self.ask_price = ask
        self.timestamp = ts


async def _drain(n: int = 8):
    """Let every task spawned by the injected event run to its next await.
    The loop is single-threaded and FIFO, so this is deterministic."""
    for _ in range(n):
        await asyncio.sleep(0)


def _parse_override(kv: str):
    key, _, raw = kv.partition("=")
    if not key or not raw:
        raise SystemExit(f"--set expects KEY=VALUE, got: {kv!r}")
    low = raw.lower()
    if low in ("true", "false"):
        val = (low == "true")
    else:
        try:
            val = int(raw)
        except ValueError:
            try:
                val = float(raw)
            except ValueError:
                val = raw
    return key.strip(), val


async def _run(rec_path: str, out_dir: str) -> dict:
    meta, events = recorder.load_session(rec_path)
    if not events:
        raise SystemExit(f"No events in recording: {rec_path}")

    start_wall = float(events[0][1])
    sim        = clock.SimClock(start_wall)
    clock.install(sim)

    session_date = (datetime.date.fromisoformat(meta["session_date"])
                    if meta.get("session_date")
                    else sim.now_et().date())

    saved = (config.LOG_DIR, config.ENTRY_END, config.TIME_STOP,
             state_mod.POSITION_STATE_FILE)
    config.LOG_DIR = out_dir
    # Replay must never touch the LIVE bot's persisted position state
    state_mod.POSITION_STATE_FILE = os.path.join(out_dir, "position_state.json")
    if market_calendar.covers(session_date) and market_calendar.is_early_close(session_date):
        config.ENTRY_END = market_calendar.shift_for_close(config.ENTRY_END, session_date)
        config.TIME_STOP = market_calendar.shift_for_close(config.TIME_STOP, session_date)
        logger.info("Early-close session %s: entry_end=%s time_stop=%s",
                    session_date, config.ENTRY_END, config.TIME_STOP)

    import main   # after clock install — module already handles late import fine
    try:
        # Fresh decision stack — identical construction to a live boot
        broker                       = SimBroker(sim)
        main.momentum_engine         = MomentumEngine()
        main.bot_state               = BotState()
        main.risk_manager            = RiskManager()
        main.orb_filter              = ORBFilter()
        main.order_manager           = broker
        main._recorder               = None
        main._market_open_event      = asyncio.Event()
        main._entry_lock             = asyncio.Lock()   # fresh, bound to THIS loop
        main._ghost_sweep_lock       = asyncio.Lock()
        main._current_subscriptions  = {}
        main._foreign_positions_seen = set()
        main._open_bar_strikes       = {}
        main._baseline_atr           = float(meta.get("baseline_atr", 3.0))

        strikes._chain_cache.clear()
        if meta.get("chain_symbols"):
            strikes._chain_cache[session_date] = set(meta["chain_symbols"])

        seed = [Bar(t=datetime.datetime.fromisoformat(r[0]),
                    open=r[1], high=r[2], low=r[3], close=r[4], volume=r[5])
                for r in meta.get("preseed_bars", [])]
        if seed:
            main.momentum_engine.preseed(seed)
            main.bot_state.spy_price = seed[-1].close

        next_monitor: Optional[float] = None
        stop_reason = "eof"
        n_bars = n_quotes = 0

        for ev in events:
            sim.advance_to(float(ev[1]))

            # Session time stop — same trigger the live watcher fires on
            if sim.now_et().strftime("%H:%M") >= config.TIME_STOP:
                if main.bot_state.entry_pending:
                    # let a resting entry hit its timeout deterministically
                    sim.advance_by(broker.fill_timeout + 1)
                    broker.notify_tick()
                    await _drain()
                if main.bot_state.position is not None:
                    await main._execute_exit("time_stop")
                    await _drain()
                stop_reason = "time_stop"
                break

            if ev[0] == "b":
                _, _, ts_iso, o, h, l, c, v = ev
                bar = _SimBar(datetime.datetime.fromisoformat(ts_iso), o, h, l, c, v)
                await main.on_spy_bar(bar)
                n_bars += 1
            else:  # "q"
                _, _, sym, bid, ask, exch_iso = ev
                # Broker sees the tick BEFORE the decision code, exactly like
                # live: the exchange had the quote before the bot acted on it.
                broker.on_quote(sym, bid, ask)
                ts = (datetime.datetime.fromisoformat(exch_iso)
                      if exch_iso else sim.now_et())
                await main.on_option_quote(_SimQuote(sym, bid, ask, ts))
                n_quotes += 1

            broker.notify_tick()   # wake resting orders on the new tick
            await _drain()

            # 30-second exit-monitor cadence (live safety net, simulated —
            # it re-evaluates cached quotes during quote gaps and can fire
            # exits, so omitting it would change outcomes)
            pos = main.bot_state.position
            if pos is not None:
                if next_monitor is None:
                    next_monitor = sim.monotonic() + EXIT_MONITOR_INTERVAL
                while (main.bot_state.position is not None
                       and sim.monotonic() >= next_monitor):
                    q = main.bot_state.get_quote(main.bot_state.position.symbol)
                    if q is not None:
                        await main._evaluate_exit(q)
                        await _drain()
                    next_monitor += EXIT_MONITOR_INTERVAL
            else:
                next_monitor = None

        if stop_reason == "eof":
            # Resolve resting entries (timeout) and flush any open position at
            # the last recorded quote — booked under a DISTINCT reason so
            # truncated-recording flushes never pollute strategy exit stats.
            sim.advance_by(broker.fill_timeout + 1)
            broker.notify_tick()
            await _drain()
            if main.bot_state.position is not None:
                await main._execute_exit("replay_eof")
                await _drain()

        trades = _load_trades(out_dir, session_date)
        return {
            "recording":        rec_path,
            "session_date":     session_date.isoformat(),
            "stop_reason":      stop_reason,
            "bars":             n_bars,
            "quotes":           n_quotes,
            "trades":           main.risk_manager.trades_today,
            "net_pnl":          round(main.risk_manager.daily_pnl, 2),
            "unfilled_entries": broker.unfilled_entries,
            "fills":            len(broker.fills),
            "rows":             trades,
        }
    finally:
        clock.install_live()
        (config.LOG_DIR, config.ENTRY_END, config.TIME_STOP,
         state_mod.POSITION_STATE_FILE) = saved


def _load_trades(out_dir: str, session_date) -> list:
    path = os.path.join(out_dir, f"trades_{session_date.isoformat()}.csv")
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def run_session(rec_path: str, out_dir: str, overrides: Optional[dict] = None) -> dict:
    """
    Replay one recorded session. `overrides` maps config attribute names to
    values applied for this run only — the sweep mechanism. Every mutated
    attribute is restored afterwards, so runs cannot contaminate each other.
    """
    os.makedirs(out_dir, exist_ok=True)
    prev = {}
    for key, val in (overrides or {}).items():
        if not hasattr(config, key):
            raise SystemExit(f"--set: config has no parameter {key!r}")
        prev[key] = getattr(config, key)
        setattr(config, key, val)
    try:
        return asyncio.run(_run(rec_path, out_dir))
    finally:
        for key, val in prev.items():
            setattr(config, key, val)


def _print_summary(s: dict):
    print("─" * 72)
    print(f"  {os.path.basename(s['recording'])}  ({s['session_date']})  "
          f"stop={s['stop_reason']}")
    print(f"  events: {s['bars']} bars, {s['quotes']} quotes  |  "
          f"fills: {s['fills']}  unfilled entries: {s['unfilled_entries']}")
    print(f"  trades: {s['trades']}  |  net P&L: ${s['net_pnl']:+.2f}")
    for r in s["rows"]:
        print(f"    {r['entry_time'][11:19]}  {r['symbol']:<21} {r['side']:<4} "
              f"{int(float(r['qty']))}x  {float(r['entry_price']):.2f}"
              f"→{float(r['exit_price']):.2f}  {r['reason']:<10} "
              f"${float(r['realized_pnl']):+8.2f}")
    print("─" * 72)


def main_cli():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("recordings", nargs="+",
                    help="recording file(s); shell globs work")
    ap.add_argument("--out", default="replay_out",
                    help="output root (per-recording subdirs)")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="config override for this run (repeatable)")
    ap.add_argument("--quiet", action="store_true",
                    help="warnings only (default shows the live INFO stream)")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    overrides = dict(_parse_override(kv) for kv in args.set)
    if overrides:
        print(f"  config overrides: {overrides}")

    paths = []
    for pattern in args.recordings:
        paths.extend(sorted(glob.glob(pattern)) or [pattern])

    total_pnl, total_trades = 0.0, 0
    for path in paths:
        sub = os.path.join(args.out, os.path.basename(path).split(".")[0])
        summary = run_session(path, sub, overrides)
        _print_summary(summary)
        total_pnl    += summary["net_pnl"]
        total_trades += summary["trades"]
    if len(paths) > 1:
        print(f"  TOTAL: {total_trades} trades, ${total_pnl:+.2f} "
              f"across {len(paths)} sessions")


if __name__ == "__main__":
    main_cli()
