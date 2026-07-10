# Alpaca Options Bot Framework

A production-ready Python framework for building **live options trading bots on Alpaca Markets**.

This repo solves the hard infrastructure problems — real-time streaming, position management, order execution, and all the Alpaca API quirks — so you can focus on your strategy.

> **Note:** This repo includes the full working 0DTE SPY gamma explosion strategy — all parameters, the live entry/exit logic in `signals.py`, and the complete 22-page strategy manual ([`SPY_0DTE_Strategy.pdf`](./SPY_0DTE_Strategy.pdf)) covering the options physics, every design decision, live session post-mortems (including the losing days), and the bug write-ups. Live results: [milgar7969.github.io/spy-0dte-dashboard](https://milgar7969.github.io/spy-0dte-dashboard/)

---

## What the Documentation Doesn't Tell You

If you've tried to build an options bot on Alpaca, you've probably hit these:

**1. Bracket orders are rejected**
```
error 42210000: complex orders not supported
```
Bracket legs, sell limits — all rejected for options on Alpaca paper trading. The only working exit is `close_position()`. This framework builds its entire exit system around that constraint.

**2. Sell limit orders are rejected**
```
error 40310000: cannot submit sell order — no existing position
```
Alpaca treats a sell limit on an option as an attempt to open a short position. Even when you own the position, it's unreliable. Use `close_position()`.

**3. Greeks return null for 0DTE contracts**
Alpaca computes Greeks via Black-Scholes. At T=0 (expiry day) the model is undefined. Every 0DTE contract returns null for delta, gamma, theta, vega. This framework includes a proxy delta implementation as a workaround.

**4. SPY price updates only once per 1-minute bar**
`StockDataStream` delivers bars at the close of each minute. Option quotes arrive many times per second. Between bar closes the underlying price is stale — proxy delta stays at zero. The framework handles this gracefully with a disable flag.

**5. Option subscriptions go stale intraday**
If you subscribe at market open and SPY moves $8 by noon, your subscribed strikes are irrelevant. The framework includes a background re-subscription watcher triggered by price movement (default: ±$3).

**6. Premarket price ≠ open price**
Subscribing options before 9:30 ET uses the premarket SPY price. SPY can gap significantly at open. The framework defers all option subscriptions until the first 9:30 ET RTH bar.

**7. Restart abandons open positions**
If your process crashes with an open position, that position still exists on Alpaca. On startup the framework polls REST, finds any existing option positions, and reconstructs local state automatically.

**8. A cancelled buy can still fill**
If an `asyncio.CancelledError` tears through your buy task mid-fill-wait (WebSocket teardown, task cancellation), cancelling the order on Alpaca is not guaranteed to win the race — the order can fill anyway, leaving a position your bot doesn't know it owns. The framework defends in depth: `buy_limit()` re-checks the order status after a cancelled wait and returns the fill if it landed (so the position is tracked normally), and a bar-driven **ghost sweeper** polls REST every minute and force-closes any option position that isn't tracked locally — worst-case exposure is ~60 seconds.

**9. Feed teardown can hang the event loop at end-of-day**
Stopping the WebSocket streams (`feed.stop()`) can block the event loop for 20+ seconds. If you run a watchdog that restarts on a silent loop, this creates a restart cascade: each fresh boot is already past the time stop, immediately re-fires it, hangs again, restarts again. The framework sidesteps the teardown entirely — after the time stop closes all positions, it **verifies the account is flat via REST (with retries), then hard-exits with `os._exit(0)`**. Observed live: flat-check plus exit completes in under 100ms.

---

## Architecture

```
asyncio.gather(
    feed.start(),               # StockDataStream + OptionDataStream + TradingStream
    _option_subscriber(),       # waits for 9:30 ET bar → subscribes strikes
    _resubscribe_watcher(),     # re-subscribes if SPY moves ±$3
    _exit_monitor(),            # 30-second safety net (WebSocket reconnect gaps)
    _time_stop_watcher(),       # 3:25 PM ET: close all → verify flat → clean exit
    _status_loop(),             # live terminal display
)

# Quote-driven exits (fires per quote tick, not a gathered task):
# on_option_quote() → asyncio.create_task(_evaluate_exit(quote))
# Sub-50ms from quote arrival to order submission.
```

Three concurrent WebSocket streams. Entry logic runs in the quote handler. Exit logic is **quote-driven** — `_evaluate_exit()` fires on every tick for the held symbol via `asyncio.create_task()`, so `close_position()` is never called from inside a stream callback.

---

## File Structure

```
alpaca-options-framework/
│
├── config.py       All parameters in one place — env credentials, thresholds,
│                     timing, fees, kill-switch thresholds
├── main.py         asyncio orchestrator, centralized exit executor, exit monitor,
│                     staleness kill switch, terminal UI, keyboard cmds
│
├── feeds.py        WebSocket stream manager (subscribe / add_option_symbols)
├── orders.py       Alpaca TradingClient wrapper — event-driven fills, partial-fill
│                     handling, all REST off the event loop
├── state.py        Session state: position, quote cache, CSV log (net of fees,
│                     order IDs, slippage), position persistence for restarts
├── risk.py         Position sizing (hard caps), prospective daily loss gate,
│                     cooldown, session lock, P&L restore
├── strikes.py      ATR calculation, dynamic strike selection, chain validation
├── occ.py          OCC symbol build/parse (stdlib-pure, unit-tested)
├── market_calendar.py  NYSE holidays + early closes — session-derived time stop
├── event_calendar.py   FOMC/CPI/NFP blackout — entry blocks + pre-statement flatten
├── momentum.py     EMA5/EMA20, VWAP, ROC5, atr5, consecutive bar engine
│                     (session stats strictly RTH — premarket feeds EMAs only)
│
├── clock.py        Single time source — live wall clock or replay's SimClock
├── recorder.py     Off-loop market-data capture → recordings/*.jsonl.gz
├── sim_broker.py   Deterministic conservative execution model for replay
├── replay.py       Replay CLI — recorded sessions through the live handlers
│
├── trade_stats.py  Statistics layer — cluster bootstrap CIs, exact t/Wilson
│                     inference, verdict tiers, paired sweep comparison
├── decision_logger.py  Per-candidate gate verdicts + attempt records — the
│                     counterfactual funnel log (replay-regenerable)
├── drift_report.py Baseline-vs-recent funnel diagnosis — PSI + bootstrap,
│                     ranked findings with cause and prescribed action
├── reconcile.py    Nightly broker reconciliation — order-ID matching, gate
│                     lock on unexplained discrepancies
├── feed_monitor.py Feed coverage & quality — dark symbols, quote gaps,
│                     spread stats, held-symbol staleness analysis
├── alerts.py       Critical-event alerting — webhook/email on kill switches,
│                     exit failures, reconciliation mismatches
├── run_session.py  Daily supervisor — ET gate, runs the bot + auto reconcile/monitor
├── windows/        Task Scheduler install/uninstall (hands-off Windows) — see WINDOWS.md
├── monitor.py      Live web monitor — read-only browser status page (localhost)
├── orb_filter.py   Clock-hour ORB directional regime filter (shadow mode)
├── kpi_dashboard.py  Self-contained HTML KPI report generator (see below)
├── tests/          Unit + async integration + replay-determinism tests
│
└── signals.py      Entry signal logic — the LIVE strategy, as traded
                      daily on paper: momentum match, ATR velocity gate, strike
                      proximity zones, ITM guard, proxy delta.
```

**The full strategy ships in `signals.py`** — this is the exact entry logic the public dashboard results come from, not a demo. Run it as-is, or swap in your own logic behind the same interface.

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt          # runtime
pip install -r requirements-dev.txt     # + pytest, to run the test suite
```

Requires Python 3.9+. All other runtime dependencies are stdlib. Run the
tests any time with `python -m pytest tests/`.

### 2. Add your Alpaca credentials

Credentials come from the environment — never from a tracked file:

```bash
cp .env.example .env
# edit .env:
#   ALPACA_API_KEY=...
#   ALPACA_API_SECRET=...
#   ALPACA_PAPER=true      # live requires an explicit "false"
```

`.env` is gitignored; plain environment variables work too and always win
over the file. The bot refuses to start with missing/placeholder keys.

Get your keys: [alpaca.markets](https://alpaca.markets) → Paper Trading → API Keys → Generate

### 3. (Optional) Adjust the strategy in `signals.py`

The live entry logic ships ready to run — no implementation required. `check_entry()` receives:
- `side` — "call" or "put"
- `strike` — the option's strike price
- `option_quote` — current bid/ask/mid
- `momentum` — live momentum state (EMA5, EMA20, VWAP, ROC5, atr5, direction)
- `spy_price` — latest SPY price
- `atr5` — 5-bar rolling ATR (intrabar velocity)

Return `True` to trigger a limit buy. The framework handles sizing, order submission, fill confirmation, and position tracking automatically — so replacing the shipped strategy with your own is a single-function change.

### 4. Run

```bash
python main.py
```

The bot waits for 9:30 ET, subscribes options at the actual open price, and becomes active at 9:45 ET. Force-closes all positions at 3:25 PM ET.

**Terminal controls:**
- `q + Enter` — quit cleanly (closes all positions)
- `r + Enter` — restart (leaves positions open, recovers on next start)
- `t + Enter` — print today's trade table

**Live web monitor:** while the bot runs, open **http://127.0.0.1:8080** in
any browser — position with ticking unrealized P&L, momentum state, risk
gates, kill-switch ages, event blackouts, and today's trades, refreshing
every 2 seconds. **Read-only by construction** (two GET endpoints, every
write method rejected — it observes the bot and cannot act on it), served
from a daemon thread that never touches the trading path, bound to
localhost only by default (`MONITOR_HOST`/`MONITOR_PORT` in `.env`;
`MONITOR_PORT=0` disables). Stdlib and self-contained — no dependencies,
no CDN.

---

## How Exits Work

The exit infrastructure is independent of the entry strategy. Once a position is open, exits are **quote-driven** — `_evaluate_exit()` fires on every option quote tick for the held symbol via `asyncio.create_task()`. Sub-50ms from quote arrival to order submission.

```
On every option quote tick for the held symbol:
  1. Update peak_mid (highest option mid seen since entry)
  2. IF mid >= entry × TP_MULT      → close_position() → "tp"
  3. IF mid <= entry × STOP_MULT    → close_position() → "stop"
  4. IF peak_mid >= entry × PEAK_TRAIL_ACTIVATE:
       trail_stop = peak_mid × PEAK_TRAIL_PCT
       IF mid <= trail_stop         → close_position() → "peak_trail"

Every 30 seconds (safety net — covers WebSocket reconnect gaps):
  → re-evaluates the same conditions using the cached quote

At 3:25 PM ET (time stop watcher):
  → close_position() → "time_stop"
```

**Race-condition safety:** `exit_pending` is set to `True` before the first `await` in the exit path. Because asyncio is cooperative (no preemption between synchronous lines), no duplicate exit orders can be submitted even when multiple quote ticks arrive simultaneously.

**Execution integrity (the invariants everything else depends on):**

- **Confirmed fills only.** A trade row is written to the CSV only on a
  broker-confirmed fill. A failed close keeps the position tracked and retries
  with backoff — it never books a guessed price and never erases local
  tracking while the broker still holds the position. After repeated failures
  it locks the entry gate and logs CRITICAL instead of pretending.
- **Partial fills are first-class.** A partially filled entry adopts the
  filled portion (real quantity, real average price); a partial close books
  the closed leg and immediately retries the remainder.
- **The ghost sweeper can't eat a real position.** `entry_pending` covers the
  entire window from order submission until local tracking exists, and the
  sweeper stands down while it's set (re-checked after every await).
- **Wide spreads don't blind the stop.** When the spread blows out, TP/trail
  skip the unreliable mid — but the hard stop still evaluates on the
  executable bid. Dislocations are exactly when the stop must fire.
- **Data staleness kill switch.** No fresh quote for the held symbol in 45s →
  flatten (`close_position()` needs no quotes). No SPY bar in 180s → lock new
  entries. A silently dead WebSocket can no longer leave a position flying blind.
- **Session-aware clock.** NYSE holidays and 13:00 early closes shift the
  time stop and entry cutoff automatically — a 0DTE position is never held
  into expiry because the market closed at 1pm. The calendar tables in
  `market_calendar.py` cover **2025–2027**; past that horizon the bot
  *refuses to start* (loud `SystemExit`) rather than guess at holidays, and
  it warns at startup for 30 days before the edge. Extending the tables is a
  two-minute edit — do it before January of the first uncovered year.
- **Macro-event blackout** (`event_calendar.py`). The FOMC statement drops
  at 14:00 ET — inside the entry window. On statement days, new entries are
  blocked from 13:30 and any open position is flattened at 13:45 (both
  configurable; replay honors them too, so backtested FOMC days behave like
  live ones). Premarket releases (CPI/NFP) are announced at startup and
  observe-only by default — shadow first, gate later. FOMC dates ship for
  2025–2026 (2027 tentative); the CPI table must be maintained from the BLS
  schedule and is deliberately harmless until you flip gating on.

All exits are logged to `logs/trades_YYYY-MM-DD.csv` with entry/exit price,
quantity, reason, **P&L net of regulatory fees**, order IDs (reconcilable
against broker statements), decision-time bid/ask, and per-side slippage.
Position metadata is persisted to `logs/position_state.json`, so a restart
recovers the real entry time/price/SPY level — not an approximation.

---

## Record & Replay — Same Code, Recorded Markets

Backtests lie in the seams — bar timing, data availability, fill assumptions.
This framework takes a different route: the live bot **records every bar and
option quote its decision code receives** (`recordings/session_YYYY-MM-DD.jsonl.gz`,
written off the event loop, a few tens of MB per session, on by default via
`RECORD_MARKET_DATA`), and `replay.py` feeds those events back through the
**identical live handlers** — `on_spy_bar` / `on_option_quote`, the same
entry gates, the same exit executor — under a simulated clock and a
simulated broker:

```bash
python replay.py recordings/session_2026-07-06.jsonl.gz
python replay.py recordings/session_*.jsonl.gz --set TP_MULT=1.8 --set STOP_MULT=0.45
```

- **Deterministic**: same recording + same config → byte-identical trade CSV,
  every run (pinned by the test suite).
- **Conservative fills by construction**: buys cross the spread at the ask
  (a resting limit fills only when the recorded ask actually crosses); sells
  hit the bid. Entries whose limit never crossed are counted as
  `unfilled entries` in the summary — missed fills are a result, not noise.
- **Session-faithful**: the simulated clock drives the entry window, quote
  freshness, the 30-second exit-monitor cadence, and the time stop; the
  session's config snapshot, ATR baseline, and momentum preseed are stored
  in the recording's metadata line.
- **Parameter sweeps in seconds**: `--set KEY=VALUE` overrides any config
  scalar for the run and restores it afterwards — one recorded session
  answers "what would a wider stop have done?" without waiting a live day.

Scope, stated plainly: the simulated broker always resolves, so
broker-failure paths (close retries, reconciliation, ghost sweeps) are
covered by unit tests, not replay; live-only watchers (watchdog, staleness
flatten) don't run. Replay measures the *strategy*, not the plumbing.

---

## Operational Trust — Reconcile, Monitor, Alert

Three tools turn "a bot that trades" into "a system whose numbers you can
defend":

**Nightly reconciliation** (`reconcile.py`) — run from cron after each close:

```bash
python reconcile.py            # exit 0 = clean, 1 = discrepancies
```

Every local CSV row is matched against the broker's closed orders **by order
ID**: existence, quantity (partial exit legs aggregated per entry order),
average fill price, side. Broker option activity the CSV doesn't know about
(ghost sweeps, manual trades) is surfaced item by item. On failure it writes
a flag file and the bot **locks its entry gate at the next startup** until
you investigate and run `python reconcile.py --clear` — trading does not
resume on top of unexplained numbers. An unreconciled track record is a
self-published claim; a reconciled one is evidence.

**Feed coverage & quality** (`feed_monitor.py`) — run on any session
recording:

```bash
python feed_monitor.py recordings/session_2026-07-06.jsonl.gz --json
```

Measures what the feed actually delivered: subscribed-vs-delivering symbols
(a **dark** symbol — subscribed, zero quotes — is a feed symbol cap or dead
contract, and it silently biases which strikes can ever fire; relevant on
the free plan's documented 30-symbol WebSocket limit vs this bot's 43+
subscriptions), quote-gap distribution, spread stats, and the maximum quote
gap on each held position (the number the staleness kill switch lives on).
Exits non-zero below 90% coverage so cron can alert. Run it on indicative
sessions now and OPRA sessions later — the diff is the measured cost of the
free feed.

**Restart-storm brake** (`restart_guard.py`) — one watchdog restart is
recovery; several within an hour is a failure loop re-entering the same
defect with a position possibly open. At 3 watchdog restarts/hour the bot
flattens its positions via REST, writes a halt flag, alerts, and refuses to
run until `python restart_guard.py --clear`. The counter is file-based —
it survives the very restarts it counts.

**Catastrophic backstop + weekly limit** — two last-line risk rules:
`CAT_STOP_MULT` (bid ≤ 20% of entry → flatten) runs on the 5-second safety
watcher, an *independent* code path from the quote-driven exits, so no
defect or starvation in the exit machinery can leave a collapsing position
unbounded; and `WEEKLY_MAX_LOSS` (enforced prospectively, restored from the
week's CSV history at startup) stops five max-loss days from compounding
past the weekly line. Both are honored in replay.

**Critical-event alerting** (`alerts.py`) — configure `ALERT_WEBHOOK_URL`
(Slack-compatible; Discord via `/slack` suffix) and/or SMTP email in `.env`.
Every CRITICAL log line — staleness flatten, exit failure + gate lock,
broker desync, reconciliation failure, watchdog restart — reaches your
phone. Rate-limited (5-min per-message cooldown, 20/day cap with suppression
counts), delivered off the event loop, flushed before every hard-exit path
so the last alert escapes. A machine that flattens at 11:00 and says
nothing until you read the terminal is not an unattended system.

**Daily results webhook** (`daily_summary.py`) — a separate, guaranteed
end-of-day message: trades, win/loss/scratch split, win rate, gross/net
P&L, best/worst trade, exit-reason breakdown. Reuses `ALERT_WEBHOOK_URL`
by default (one webhook covers both channels), or set
`DAILY_SUMMARY_WEBHOOK_URL` to route it elsewhere (e.g. a separate
`#results` channel). Runs automatically as the last step of
`run_session.py`'s post-session audit — no setup beyond the env var. On
purpose, it still sends on a **0-trade day**: silence here would be
indistinguishable from the supervisor never having run at all. Run it by
hand for any past day: `python daily_summary.py --date 2026-07-08`.

---

## Decision Log & Drift Report — When the Strategy Deviates, Know WHY

P&L only says *that* a strategy changed. Four different deaths print the
same red number and need four different responses: signals stopped firing
(regime moved), gates started blocking (a filter went stale), fills stopped
happening (execution), or winners became losers (the edge repriced). The
decision-logging stack localizes deviation to a layer of the funnel:

```
market conditions → signal gates → attempts/fills → realization → P&L
```

- **`decisions_YYYY-MM-DD.csv.gz`** — one row per (bar, candidate): EVERY
  gate's verdict (never short-circuited — order-dependent gate stats are
  lies), the **sole blocker** (the gate that alone stopped a near-miss —
  the single most actionable statistic), and the full market/momentum
  context. Written from the *same* gate function the live entry path calls,
  so the record and the trading can never disagree.
- **`attempts_YYYY-MM-DD.csv`** — every order attempt including failures;
  fill-rate decay is an execution-regime change with its own fix.
- **Trades CSV excursion columns** (`mfe_pnl`/`mae_pnl`/`peak_mid`) — the
  fork in the diagnostic tree: MFE collapsed = edge decay (cut size,
  redesign); MFE intact but capture down = give-back (exit sweeps fix it).
- **Regenerable history**: replay drives the same code path, so decision
  logs can be rebuilt for every session ever recorded — the drift baseline
  exists on day one, byte-deterministically.

```bash
python drift_report.py logs/ --recent-days 10
```

compares a recent window against baseline with the same statistical
discipline as `trade_stats` (PSI distribution-shift scores, by-day
bootstrap, deterministic seed, an insufficient-data refusal) and emits
**ranked findings with the most probable cause and the prescribed action**
— e.g. "EDGE DECAY: median MFE ratio 0.30→0.03 — cut size per kill
criteria; exit retuning will NOT fix this" vs "'atr' became the binding
constraint — replay-sweep its threshold and quote the adjusted p."

---

## Statistics Layer — Is There Actually an Edge?

Point estimates lie at small samples. `trade_stats.py` answers the only
question that matters — *is the edge statistically distinguishable from
zero, and with what uncertainty?* — with methods chosen for small,
dependent samples (all stdlib, deterministic for a given seed):

```bash
python trade_stats.py logs/                          # live track record
python trade_stats.py replay_out/session_*/          # replay outputs
python trade_stats.py --compare replay_out_base replay_out_variant \
    --variants-tested 12                             # sweep comparison
```

- **Cluster bootstrap by session day.** Trades within a day share regime;
  resampling individual trades understates variance. Days are resampled
  with replacement, each carrying all its trades — intervals come out
  wider, and honest.
- **Exact small-sample inference.** Student-t p-values via the regularized
  incomplete beta, Wilson score intervals for win rate — correct at n=20,
  not just n=500.
- **A sample-size gate.** Below 30 trades / 10 sessions the verdict is
  "INSUFFICIENT SAMPLE — no statistical claim possible", not a noise
  Sharpe with two decimal places.
- **Verdict tiers**: SIGNIFICANT (p<0.01) / SUGGESTIVE (p<0.05) /
  NO DETECTABLE EDGE — with the bootstrap p-value printed next to each.
- **Sweep honesty.** `--compare` pairs variants on identical replayed
  sessions (paired daily differences — far more power than unpaired) and
  Bonferroni-adjusts for `--variants-tested K`: quoting the raw p-value of
  the best of 12 sweeps is data mining, and the report prints the adjusted
  number with an arrow telling you which one you may quote.
- Report includes: EV/trade with CI, breakeven win rate at the realized
  payoff asymmetry, profit factor CI, annualized Sharpe/Sortino with CI,
  observed and day-resampled drawdown distribution (median / p95), SQN,
  per-reason breakdown, and a "what this cannot tell you" footer.

The KPI dashboard consumes the same module: its headline cards now carry a
Wilson CI on win rate and a **Statistical Edge** card
(SIGNIFICANT / SUGGESTIVE / NOT DETECTED / SAMPLE TOO SMALL) so the public
track record makes no claim the sample can't support.

---

## Shadow Filters — Validate Before You Gate

`orb_filter.py` demonstrates a pattern worth stealing even if you don't use the filter itself: **run a new filter in observe-only mode before letting it block live trades.**

The filter tracks each clock hour's opening-range high/low (first 1-min bar of the hour) and compares it to the prior hour's: both higher → call-only bias, both lower → put-only bias, mixed → no restriction. Wired into the entry path, `check_shadow()` logs what it *would* have done on every real entry signal:

```
ORB_SHADOW | BLOCK: SPY260702C00753000 side=call bias=put — trade allowed (shadow mode)
ORB_SHADOW | ALLOW: SPY260702P00746000 side=put bias=put
```

It always returns `True` — zero effect on trading. After enough sessions you can score every BLOCK against the trade's actual P&L and decide with data, not backtest hope, whether to flip it live (a two-line change). Backtests lie in subtle ways — pre-market data availability, restart behavior, bar timing. Shadow mode tests the filter in the exact code path that would run it.

---

## KPI Dashboard

`kpi_dashboard.py` generates a self-contained dark-theme HTML report (Chart.js via CDN, no build step) from the framework's own logs:

- Equity curve and daily P&L over a rolling calendar window (`--days 30`)
- Win rate, profit factor, EV/trade, breakdowns by exit reason / side / entry hour
- **True account P&L**: parses ghost-sweeper closes out of the bot logs and adds them to the booked CSV totals — duplicate fills don't silently vanish from your stats
- Shadow-filter scoreboard: actual vs. would-have-been-filtered P&L, every block listed

```bash
python kpi_dashboard.py --days 30 --out dashboard/index.html
```

Push the output to any static host (GitHub Pages works fine) for a public, auto-updating track record.

---

## Momentum Engine

`momentum.py` computes five indicators per 1-minute bar:

| Indicator | Formula | Notes |
|---|---|---|
| EMA5 | Exponential MA, period=5 | Fast trend |
| EMA20 | Exponential MA, period=20 | Slow trend |
| VWAP | Sum(typical_price × vol) / Sum(vol) | Resets at 9:30 ET |
| ROC5 | (close - close[5]) / close[5] | 5-bar rate of change |
| atr5 | Avg(high - low) over last 5 bars | Intrabar velocity |

Pre-seeded with the last 30 historical RTH 1-min bars at startup so all indicators are meaningful from the first live bar.

BULL / BEAR / NEUTRAL direction is derived by combining all five — see `momentum.py` for the exact conditions.

---

## Data Feeds — IEX/Indicative vs SIP/OPRA

The free Alpaca tiers (`iex` stocks, `indicative` options) are what the bot
uses by default — fine for paper trading and development. But IEX carries
~2–3% of consolidated stock volume and the indicative options feed is
sampled: signals, stops, and `peak_mid` are only as good as the tape they
watch. **For live capital, use the paid feeds:**

```bash
# .env (requires the Alpaca market-data subscription)
ALPACA_STOCK_FEED=sip      # full consolidated tape
ALPACA_OPTION_FEED=opra    # full options NBBO
```

One switch changes everything consistently — live streams, historical
requests (ATR baseline, momentum preseed), and the chain cache all route
through the same mapping, so signals and seed data can never come from
different tapes. Startup probes the entitlement with one cheap REST request
and fails fast with an actionable message if the account lacks the
subscription, instead of dying at 09:30 with an opaque stream error. The
feeds in use are stamped into every session recording's metadata.

---

## Backtesting

Recorded-session replay (above) covers every day from the moment you start
running the bot. For testing against *historical* periods before that, see
[`BACKTESTING.md`](./BACKTESTING.md) — the assessment of what exists and the
proposed design: a `backfill.py` that reconstructs recording files from
Alpaca's historical data so the **same replay engine** becomes a full
backtester, with fidelity tiers and a reconstruction-error acceptance test
against live-captured ground truth.

---

## ES Futures Port — NinjaTrader Sibling Strategy

The signal engine also drives an ES/MES futures sibling for a funded prop
account (Apex 50K). See [`ES_PORT_PLAN.md`](./ES_PORT_PLAN.md) for the full
decomposition (what ports, what can't — convexity, theta-as-stop, premium
exits) and [`ninjatrader/`](./ninjatrader/) for the C# side.

Key facts: the exits are re-derived in price space (`futures_exits.py`),
sizing is re-derived from the Apex trailing-drawdown buffer, not the
nominal balance (`apex_risk.py`), the backtest/golden-vector runner is
`es_backtest.py`, and the C# NinjaScript engine is gated by machine-checked
conformance against the Python oracle (`golden_vectors.py` +
`ninjatrader/check_conformance.ps1`) — it may not trade until the diff
prints `CONFORMANT`. The NT8 deliverable is `AofEsStrategy`, a **fully
automated strategy** (one conformance-checked brain, thin order mirror,
prop-firm rules as parameters) — run it only on accounts whose written
rules permit automation; Apex's funded accounts do NOT, so a co-pilot
indicator (`AofEsMomentum`) ships alongside for manual-entry firms.

---

## WebSocket vs REST — Why Both

A common question: why use WebSocket streaming for quotes instead of REST polling?

**The bot uses both — for different purposes:**

| Operation | Method | Reason |
|---|---|---|
| Real-time SPY 1-min bars | WebSocket (StockDataStream) | Pushed at bar close, zero polling overhead |
| Real-time option quotes | WebSocket (OptionDataStream) | Every quote tick, sub-10ms delivery |
| Order fill events | WebSocket (TradingStream) | Instant fill confirmation |
| Historical bars at startup | REST | One-shot fetch, no need for streaming |
| 5-day ATR baseline | REST | One-shot fetch at startup |
| Submit entry order | REST | `submit_order()` — synchronous, want confirmation |
| Close position | REST | `close_position()` — only working exit method |
| Recover open positions on restart | REST | `get_all_positions()` — definitive server state |

**Why not REST polling for option quotes?**

The bot subscribes to 42 option symbols simultaneously (21 calls + 21 puts). Polling all 42 every second = 42 REST requests/second. Alpaca's free tier allows ~200 requests/minute — you'd hit the rate limit in under 5 seconds.

Even if rate limits weren't an issue, REST polling introduces latency on every request (50–200ms per HTTP round trip). For 0DTE entries where you're trying to catch a move in progress, a WebSocket that pushes quotes as they happen is significantly more responsive.

There's also a data completeness issue: if an option spikes and reverses within a 1-second polling window, REST never sees the peak. WebSocket delivers every tick — the spike would update `peak_mid` and potentially arm the trailing stop. With REST polling, that recovery is invisible.

**Rule of thumb:** use WebSocket for anything that changes multiple times per second. Use REST for one-shot queries and order submission.

---

## Requirements

- Python 3.9+
- `alpaca-py >= 0.43.0`
- Alpaca account (free) with options trading enabled on paper
- macOS or Linux (Windows works but terminal display may vary)

---

## Contributing

Issues and PRs welcome. If you've found additional Alpaca API quirks not documented here, please open an issue — building a comprehensive list of limitations helps everyone in this space.

---

## Disclaimer

This software is for educational purposes only. It does not constitute financial advice. Paper trading performance does not guarantee live trading results. Use at your own risk.

---

*Built with Python 3.11 · alpaca-py · Paper Trading Only*
