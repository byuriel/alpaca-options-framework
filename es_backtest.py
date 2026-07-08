"""
ES/MES bar-driven session runner - backtest, Apex survival simulation,
decision logging, and golden-vector emission in ONE deterministic loop.

    python es_backtest.py bars.csv --out out/es_run [--zone grid] [--spec MES]
                                   [--states]   # emit per-bar golden vectors

One loop on purpose: the golden vectors that gate the C# NinjaScript port
(ninjatrader/) are per-bar snapshots of THIS runner's engine + shadow-trade
state. If goldens came from a different code path than the backtest, C#
conformance would prove nothing about the numbers we actually trust.

Point-in-time discipline:
  - daily ATR (grid zone variant) uses PRIOR sessions only; warmup days
    fail the zone gate closed rather than peeking at same-day data
  - the Apex threshold is marked intrabar pessimistically: the bar's best
    favorable mark ratchets the threshold FIRST, then the worst adverse
    mark is tested for breach against the raised threshold

Fill and cost conservatism lives in futures_sim.FuturesSimBroker.

Bar-time convention: bar.t = bar OPEN time (Alpaca/Databento convention).
NT8 exports stamp bar CLOSE - the loader shifts them back one minute.

Outputs in --out:
  decisions_es.csv.gz  one row per RTH bar: every gate verdict, sole
                       blocker, momentum context (same crash-safe complete-
                       gzip-member format as decision_logger.py)
  trades_es.csv        one row per round turn incl. MFE/MAE, Apex state
  states_es.csv        (--states) per-bar golden vectors for C# conformance
  summary printed as JSON
"""

import argparse
import csv
import datetime
import gzip
import io
import json
import logging
import math
import os
from collections import deque
from typing import List, Optional

import config
import event_calendar
from apex_risk import ApexAccount
from es_engine import ES_GATE_NAMES, EsSignalEngine
from futures_contracts import SPECS, ticks_between
from futures_exits import (ExitParams, FuturesPosition, initial_stop_price,
                           soft_exit_reason, target_price)
from futures_sim import FuturesSimBroker
from momentum import Bar

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

DECISION_COLUMNS = [
    "schema", "date", "bar_time_et", "close",
    "direction", "ema5", "ema20", "vwap", "roc5", "atr5", "consec",
    "atr5d", "zone_dist_pct",
    *[f"g_{n}" for n in ES_GATE_NAMES],
    "all_pass", "sole_blocker",
    "in_position", "risk_ok", "blackout", "event_day",
]

TRADE_COLUMNS = [
    "schema", "date", "contract", "side", "qty",
    "entry_time_et", "entry_px", "exit_time_et", "exit_px", "reason",
    "pnl_points", "pnl_usd", "commissions",
    "mfe_points", "mae_points", "bars_held",
    "stop_px", "target_px", "atr5_entry",
    "balance_after", "threshold_after", "headroom_after",
]

STATE_COLUMNS = [
    "date", "time_et", "open", "high", "low", "close", "volume",
    "ema5", "ema20", "vwap", "roc5", "atr5",
    "consec_green", "consec_red", "direction",
    *[f"g_{n}" for n in ES_GATE_NAMES],
    "all_pass", "entry_side", "in_pos", "pos_side", "qty",
    "stop_px", "target_px", "exit_reason",
]


def _f(v, nd=6):
    return "" if v is None else f"{v:.{nd}f}"


class _GzMemberWriter:
    """Crash-safe by construction - one complete gzip member per batch,
    mtime=0 for byte determinism. Same policy as decision_logger.py."""

    def __init__(self, path: str, header: List[str]):
        self._raw = open(path, "wb")
        hdr = io.StringIO()
        csv.writer(hdr).writerow(header)
        self.write_member(hdr.getvalue())

    def write_member(self, text: str):
        buf = io.BytesIO()
        with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as gz:
            gz.write(text.encode())
        self._raw.write(buf.getvalue())
        self._raw.flush()

    def write_rows(self, rows: List[list]):
        buf = io.StringIO()
        w = csv.writer(buf)
        for r in rows:
            w.writerow(r)
        if rows:
            self.write_member(buf.getvalue())

    def close(self):
        self._raw.close()


# -- Bar loading (Databento / NT8 export / generic OHLCV) ---------------------

def load_bars_csv(path: str, symbol: Optional[str] = None) -> List[Bar]:
    with open(path, newline="") as f:
        first = f.readline()
    if ";" in first and "," not in first:
        return _load_nt8(path)
    if "ts_event" in first:
        return _load_databento(path, symbol)
    return _load_generic(path)


def _parse_ts(raw: str) -> datetime.datetime:
    """ISO8601 (Z ok) or integer nanoseconds -> aware datetime (naive -> ET)."""
    raw = raw.strip()
    if raw.isdigit():
        return datetime.datetime.fromtimestamp(int(raw) / 1e9,
                                               tz=datetime.timezone.utc)
    ts = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=config.ET)


def _load_databento(path: str, symbol: Optional[str]) -> List[Bar]:
    bars, symbols_seen = [], set()
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            sym = row.get("symbol", "")
            if sym:
                symbols_seen.add(sym)
                if symbol and sym != symbol:
                    continue
            bars.append(Bar(t=_parse_ts(row["ts_event"]),
                            open=float(row["open"]), high=float(row["high"]),
                            low=float(row["low"]), close=float(row["close"]),
                            volume=float(row.get("volume", 0) or 0)))
    if not symbol and len(symbols_seen) > 1:
        raise ValueError(f"multiple symbols in {path}: {sorted(symbols_seen)} "
                         "- pass symbol= to pick the front contract; mixing "
                         "contracts splices phantom roll gaps into the series")
    return bars


def _load_nt8(path: str) -> List[Bar]:
    """NT8 minute export: 'yyyyMMdd HHmmss;O;H;L;C;V', chart-timezone stamps
    at bar CLOSE. We require the export be done with the chart on US Eastern
    and shift stamps to bar-open convention."""
    bars = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            ts_s, o, h, l, c, v = line.split(";")
            t = (datetime.datetime.strptime(ts_s, "%Y%m%d %H%M%S")
                 .replace(tzinfo=config.ET) - datetime.timedelta(minutes=1))
            bars.append(Bar(t=t, open=float(o), high=float(h),
                            low=float(l), close=float(c), volume=float(v)))
    return bars


def _load_generic(path: str) -> List[Bar]:
    bars = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        cols = {c.lower(): c for c in reader.fieldnames}
        tcol = next(cols[k] for k in ("timestamp", "datetime", "time", "date")
                    if k in cols)
        for row in reader:
            bars.append(Bar(t=_parse_ts(row[tcol]),
                            open=float(row[cols["open"]]),
                            high=float(row[cols["high"]]),
                            low=float(row[cols["low"]]),
                            close=float(row[cols["close"]]),
                            volume=float(row.get(cols.get("volume", ""), 0) or 0)))
    return bars


# -- The runner ----------------------------------------------------------------

RTH_OPEN  = datetime.time(9, 30)
RTH_CLOSE = datetime.time(16, 0)


def run_backtest(bars: List[Bar], *,
                 spec_root: Optional[str] = None,
                 zone_variant: Optional[str] = None,
                 exit_params: Optional[ExitParams] = None,
                 apex: Optional[ApexAccount] = None,
                 out_dir: Optional[str] = None,
                 emit_states: bool = False) -> dict:
    spec_root = spec_root or config.ES_SPEC_ROOT
    spec      = SPECS[spec_root]
    p         = exit_params or ExitParams()
    apex      = apex or ApexAccount()
    engine    = EsSignalEngine(zone_variant=zone_variant)
    sim       = FuturesSimBroker(spec_root=spec_root)

    dec_w = trade_w = state_w = None
    trade_f = state_f = None
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        dec_w   = _GzMemberWriter(os.path.join(out_dir, "decisions_es.csv.gz"),
                                  DECISION_COLUMNS)
        trade_f = open(os.path.join(out_dir, "trades_es.csv"), "w", newline="")
        trade_w = csv.writer(trade_f)
        trade_w.writerow(TRADE_COLUMNS)
        if emit_states:
            state_f = open(os.path.join(out_dir, "states_es.csv"), "w", newline="")
            state_w = csv.writer(state_f)
            state_w.writerow(STATE_COLUMNS)

    pos: Optional[FuturesPosition] = None
    session: Optional[datetime.date] = None
    daily_trs: deque = deque(maxlen=5)        # prior sessions' true ranges
    cur_hi = cur_lo = cur_close = prev_close = None
    realized_today = 0.0
    realized_week  = 0.0
    week_key       = None
    exit_counts: dict = {}
    trades = wins = n_sessions = 0
    total_pnl = 0.0
    min_headroom = apex.headroom()
    dec_batch: List[list] = []

    def _flush_decisions():
        if dec_w and dec_batch:
            dec_w.write_rows(dec_batch)
            dec_batch.clear()

    def _book_exit(bar_et, price, reason):
        nonlocal pos, realized_today, realized_week, trades, wins, total_pnl
        pnl = sim.pnl_usd(pos, price)
        apex.book_realized(pnl)
        realized_today += pnl
        realized_week  += pnl
        trades  += 1
        wins    += 1 if pnl > 0 else 0
        total_pnl += pnl
        exit_counts[reason] = exit_counts.get(reason, 0) + 1
        engine.note_exit()
        if trade_w:
            trade_w.writerow([
                SCHEMA_VERSION, bar_et.date().isoformat(), spec.root,
                pos.side, pos.qty,
                pos.entry_time.strftime("%H:%M:%S"), f"{pos.entry_price:.2f}",
                bar_et.strftime("%H:%M:%S"), f"{price:.2f}", reason,
                f"{pos.favorable(price):.2f}", f"{pnl:.2f}",
                f"{sim.round_turn_commission(pos.qty):.2f}",
                f"{pos.peak_favorable:.2f}", f"{pos.mae_points:.2f}",
                pos.bars_held,
                f"{pos.stop_price:.2f}", f"{pos.target_price:.2f}",
                f"{pos.atr5_entry:.4f}",
                f"{apex.s.balance:.2f}", f"{apex.s.threshold:.2f}",
                f"{apex.headroom():.2f}",
            ])
        pos = None

    for bar in bars:
        bar_et = bar.t.astimezone(config.ET)
        d      = bar_et.date()

        # -- session roll ------------------------------------------------------
        if d != session:
            if session is not None and cur_hi is not None:
                tr = (cur_hi - cur_lo if prev_close is None
                      else max(cur_hi - cur_lo, abs(cur_hi - prev_close),
                               abs(cur_lo - prev_close)))
                daily_trs.append(tr)
                prev_close = cur_close
                apex.end_of_day()
            session = d
            n_sessions += 1
            cur_hi = cur_lo = cur_close = None
            realized_today = 0.0
            wk = d.isocalendar()[:2]
            if wk != week_key:
                week_key = wk
                realized_week = 0.0
            if daily_trs:
                engine.set_daily_atr(sum(daily_trs) / len(daily_trs))
            _flush_decisions()

        is_rth = RTH_OPEN <= bar_et.time() < RTH_CLOSE
        if is_rth:
            cur_hi = bar.high if cur_hi is None else max(cur_hi, bar.high)
            cur_lo = bar.low  if cur_lo is None else min(cur_lo, bar.low)
            cur_close = bar.close

        exit_reason_this_bar = ""

        # -- holding: excursions, Apex marks, exits (before this bar's entry
        #    decision, mirroring live's continuous quote-driven exits) ---------
        if pos is not None:
            pos.update_on_bar(bar.high, bar.low, bar.close)

            # pessimistic intrabar ordering: peak ratchets threshold first,
            # then the trough is tested against the RAISED threshold
            best  = bar.high if pos.side == "long" else bar.low
            worst = bar.low  if pos.side == "long" else bar.high
            apex.mark_equity(apex.s.balance + sim.unrealized_usd(pos, best))
            eq_worst = apex.s.balance + sim.unrealized_usd(pos, worst)
            breached = apex.mark_equity(eq_worst)
            min_headroom = min(min_headroom, apex.headroom(eq_worst))
            if breached:
                _book_exit(bar_et, worst, "apex_liquidation")
                exit_reason_this_bar = "apex_liquidation"
            else:
                hard = sim.check_hard_exits(pos, bar.high, bar.low)
                if hard:
                    _book_exit(bar_et, hard[0], hard[1])
                    exit_reason_this_bar = hard[1]
                else:
                    flatten = event_calendar.should_flatten_for_event(bar_et)
                    soft = ("event_flatten" if flatten
                            else soft_exit_reason(pos, bar.close,
                                                  bar_et.time(), p))
                    if soft:
                        _book_exit(bar_et, sim.close_fill(pos.side, bar.close),
                                   soft)
                        exit_reason_this_bar = soft

        min_headroom = min(min_headroom, apex.headroom())

        # -- signal evaluation (momentum updates on EVERY bar, incl. non-RTH,
        #    exactly like the live engine) -------------------------------------
        decision = engine.on_bar(bar, has_open_pos=pos is not None)
        if apex.s.breached:
            _flush_decisions()
            break

        blackout = event_calendar.entry_blackout_reason(bar_et)
        risk_ok  = True
        qty = 0
        stop_px = target_px = None

        # -- entry -------------------------------------------------------------
        if (is_rth and decision.entry_side and pos is None
                and not blackout and exit_reason_this_bar == ""):
            atr5 = decision.momentum.atr5
            fill = sim.entry_fill(decision.entry_side, bar.close)
            stop_px   = initial_stop_price(decision.entry_side, fill, atr5, p)
            target_px = target_price(decision.entry_side, fill, atr5, p)
            stop_ticks = ticks_between(fill, stop_px)
            qty = apex.size_trade(stop_ticks, spec)
            risk_usd = (stop_ticks * spec.tick_value * qty
                        + sim.round_turn_commission(qty))
            risk_ok = qty >= 1 and apex.can_open(risk_usd, realized_today,
                                                 realized_week)
            if risk_ok:
                pos = FuturesPosition(
                    side=decision.entry_side, entry_price=fill, qty=qty,
                    atr5_entry=atr5, entry_time=bar_et,
                    stop_price=stop_px, target_price=target_px)
                engine.note_entry()
            else:
                qty = 0

        # -- decision + state rows (RTH only - non-RTH bars only warm EMAs) ---
        if is_rth:
            m = decision.momentum
            dec_batch.append([
                SCHEMA_VERSION, d.isoformat(), bar_et.strftime("%H:%M:%S"),
                f"{bar.close:.2f}",
                m.direction, _f(m.ema5, 4), _f(m.ema20, 4), _f(m.vwap, 4),
                _f(m.roc5, 6), _f(m.atr5, 4),
                m.consec_green if m.direction != "bear" else m.consec_red,
                _f(engine.atr5d, 4), _f(decision.zone_dist_pct, 5),
                *[int(decision.gates[n]) for n in ES_GATE_NAMES],
                int(decision.all_pass), decision.sole_blocker,
                int(pos is not None), int(risk_ok), blackout or "",
                int(bool(event_calendar.todays_events(d))),
            ])
            if len(dec_batch) >= 390:
                _flush_decisions()

            if state_w:
                m = decision.momentum
                state_w.writerow([
                    d.isoformat(), bar_et.strftime("%H:%M:%S"),
                    f"{bar.open:.2f}", f"{bar.high:.2f}", f"{bar.low:.2f}",
                    f"{bar.close:.2f}", f"{bar.volume:.0f}",
                    _f(m.ema5), _f(m.ema20), _f(m.vwap), _f(m.roc5),
                    _f(m.atr5), m.consec_green, m.consec_red, m.direction,
                    *[int(decision.gates[n]) for n in ES_GATE_NAMES],
                    int(decision.all_pass), decision.entry_side or "",
                    int(pos is not None), pos.side if pos else "",
                    pos.qty if pos else 0,
                    f"{pos.stop_price:.2f}" if pos else "",
                    f"{pos.target_price:.2f}" if pos else "",
                    exit_reason_this_bar,
                ])

    _flush_decisions()
    if dec_w:
        dec_w.close()
    if trade_f:
        trade_f.close()
    if state_f:
        state_f.close()

    return {
        "spec": spec.root,
        "zone_variant": engine.zone_variant,
        "sessions": n_sessions,
        "trades": trades, "wins": wins,
        "win_rate": round(wins / trades, 4) if trades else None,
        "total_pnl_usd": round(total_pnl, 2),
        "exit_counts": exit_counts,
        "apex": {
            "breached": apex.s.breached,
            "end_balance": round(apex.s.balance, 2),
            "end_threshold": round(apex.s.threshold, 2),
            "min_headroom": round(min_headroom, 2),
            "unrealized_consumption": round(apex.s.unrealized_consumption, 2),
            "scaling_unlocked": apex.s.scaling_unlocked,
        },
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("bars_csv")
    ap.add_argument("--out", required=True)
    ap.add_argument("--spec", default=None, choices=["ES", "MES"])
    ap.add_argument("--zone", default=None, choices=["off", "grid"])
    ap.add_argument("--symbol", default=None,
                    help="filter multi-symbol Databento files to one contract")
    ap.add_argument("--states", action="store_true",
                    help="emit per-bar golden vectors (states_es.csv)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    bars = load_bars_csv(args.bars_csv, symbol=args.symbol)
    summary = run_backtest(bars, spec_root=args.spec, zone_variant=args.zone,
                           out_dir=args.out, emit_states=args.states)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
