# Desk Review — Alpaca 0DTE SPY Options Framework

*A hedge-fund-quant-style review of the full codebase: correctness, execution
microstructure, risk, strategy design, data quality, and a phased roadmap to
prop-firm readiness. Every finding cites file:line against the current tree.*

---

## 0. Executive summary

**What this is:** a single-position, long-premium 0DTE SPY momentum bot on
Alpaca — WebSocket bars/quotes/fills, quote-driven exits, REST order
management, restart recovery, ghost-position sweeping, a shadow-mode regime
filter, and an HTML KPI dashboard.

**What's genuinely good** (and rare in retail frameworks):

- The engineering is **failure-mode driven**. The cancel-race fill recovery
  (`orders.py:74-97`), the ghost sweeper (`main.py:596`), the
  verify-flat-then-`os._exit` teardown (`main.py:904`), and the watchdog
  (`main.py:1102`) all encode real live-fire lessons. Most frameworks at this
  level have none of this.
- The `exit_pending`-before-first-await reasoning (`main.py:695-745`) is
  correct asyncio thinking.
- **Shadow mode** (`orb_filter.py`) — validating a filter in the live code
  path before letting it block trades — is genuinely professional
  methodology. Keep it; extend it.
- The Alpaca-quirks documentation in the README is real, hard-won IP.

**What fails a desk review today:** the framework's *accounting can lie*, its
*risk cap can be silently violated*, its *safety systems can race each other
into closing a real position*, and the *paper track record is marked
optimistically* (mid-marks, no fees, known double-counting path). Any
quantitative buyer or prop-firm risk officer will find these within a day.
None are fatal — all are fixable — but they gate everything else.

Severity legend: **SEV-1** = can lose money or corrupt the track record.
**SEV-2** = correctness/quality defect. **SEV-3** = hygiene.

---

## 1. Step-by-step architecture walkthrough

1. **Startup** (`main.py:970`): reset/restore daily risk counters from CSV →
   fetch 5-day ATR baseline → pre-seed EMAs with 30 historical RTH bars →
   cancel leftover option orders → recover any open position via REST.
2. **Feeds** (`feeds.py`): three concurrent streams — SPY 1-min bars (IEX),
   option quotes (indicative feed), trade updates. Option subscription is
   deferred until the first ≥09:30 ET bar.
3. **Per bar** (`main.py:119`): momentum engine update → cooldown tick → ORB
   filter update → dynamic strike recompute + routing-table refresh → spawn
   SPY-stop check and ghost sweep.
4. **Per option quote** (`main.py:202`): update quote cache/proxy delta; if
   holding this symbol → spawn `_evaluate_exit`; else → run `_evaluate_entry`
   under a lock.
5. **Entry** (`main.py:245`): `check_entry` gates (time window, ATR velocity,
   momentum direction, price band, OTM-only, activation zone) → size →
   limit buy at mid×1.02 → poll for fill → track position.
6. **Exits**: quote-driven TP/stop/peak-trail (`main.py:695`), bar-driven
   SPY-level stop (`main.py:646`), 30-second cached-quote safety net
   (`main.py:764`), 15:25 ET time stop with flat verification (`main.py:893`).
7. **Reporting**: append-only CSV per trade; `kpi_dashboard.py` merges CSV +
   log-parsed ghost closes + shadow-filter decisions into an HTML report.

The shape is right. The defects are in the seams.

---

## 2. SEV-1 findings — money and track-record integrity

### 2.1 Failed exits book fictitious fills and erase real positions

`main.py:749-752` (`_evaluate_exit`), `main.py:679-682`
(`_evaluate_spy_stop`), `main.py:962-964` (`_force_close_all`):

```python
order = await order_manager.close_position(pos.symbol, pos.qty_remaining)
fill  = order_manager.get_fill_price(order) if order else mid
pnl   = bot_state.close_position(fill, reason)
```

If `close_position` returns `None` (submit failed, fill-wait timed out —
`orders.py:119-141`), the code books an exit **at the cached mid** (or at
`entry × 0.5` in `_force_close_all`) and deletes local tracking — while the
real position is still open on Alpaca. Consequences:

- The CSV now contains a **fabricated fill price**.
- The ghost sweeper finds the still-open position within ~60s and closes it,
  logging its own P&L — which the dashboard **adds** to booked P&L
  (`kpi_dashboard.py:151-152`). The same position's P&L is counted twice:
  once fictitious, once real.
- If the ghost close happens at a very different price, "actual P&L" is
  arbitrarily wrong.

**Fix:** never book without a confirmed fill. On failed close: keep the
position tracked, set a retry/backoff state, alert loudly. Reconcile
`bot_state` against `get_all_positions()` before writing the CSV row.

### 2.2 The ghost sweeper can force-close a real, wanted position

`main.py:596-641`: the sweeper early-returns on `exit_pending` but **not on
`entry_pending`**. The fill path polls REST every 2s (`orders.py:32,154`), so
there is a multi-second window where Alpaca shows the position but
`bot_state.position` is still `None`. A bar-close sweep landing in that
window sees an "unknown" position and market-closes it. Then `buy_limit`
returns the fill, the bot opens local tracking for a position that no longer
exists, and every subsequent exit books phantom P&L against nothing
(`close_position` fails → finding 2.1 fires → fictitious CSV row).

**Fix:** `if bot_state.exit_pending or bot_state.entry_pending: return`, and
give the sweeper a grace period (skip positions whose Alpaca `avg_entry`
timestamp is < 10s old). Strategically: consume the **TradingStream** fill
events you already subscribe to (`main.py:192` is logging-only today) as the
authoritative position source instead of REST polling — it eliminates this
whole race class and the 2s fill latency.

### 2.3 Partial fills are unhandled in every path

`orders.py:154-171` (`_wait_for_fill`) recognizes only `FILLED` /
`CANCELLED` / `EXPIRED` / `REJECTED`. A limit buy that partially fills and
then times out is cancelled; the filled portion is **owned** but `buy_limit`
returns `None`, main treats the entry as failed, and the contracts sit until
the ghost sweeper market-closes them — an unintended round trip with no
signal, booked only via log-parsing. Same class of problem on the close
side. `Position.qty_remaining` and `target1_hit` exist (`state.py:34-35`)
but nothing maintains them.

**Fix:** treat `PARTIALLY_FILLED` explicitly: adopt the filled quantity as
the position (with the real `filled_avg_price`), cancel the remainder, and
size exits off `filled_qty`.

### 2.4 `max(1, contracts)` silently violates the risk cap

`risk.py:74`. Measured against the shipped config:

| Entry premium | Risk/contract (stop basis) | Contracts | Actual stop risk | Max loss (full premium) |
|---|---|---|---|---|
| $0.60 | $30 | 5 | $150 ✔ | $300 |
| $3.05 | $152.50 | 1 | **$152.50** | $305 |
| $6.00 | $300 | 1 | **$300 — 2× the cap** | $600 |
| $10.00 | $500 | 1 | **$500 — 3.3× the cap** | **$1,000** |

`OPTION_MAX_PRICE = 10.00` (`config.py:26`) explicitly allows the worst row.
A risk officer reads this as: *the stated per-trade limit is not enforced.*

**Fix:** `if contracts < 1: return 0` and skip the trade; add a hard premium
cap (`qty × premium × 100 ≤ X% of equity`).

### 2.5 The risk basis itself understates 0DTE risk

Sizing assumes loss = 50% of premium (`risk.py:72`). But the stop triggers on
**mid** and fills as a **market order at the bid**, 0DTE gaps through levels
between ticks, and the spread-spike filter (`main.py:221-228`) *delays stop
evaluation precisely when markets dislocate* (that's when MMs widen). The
executable worst case is ~100% of premium. `MAX_DAILY_LOSS` is also only
checked *after* a close (`risk.py:92`) — a single open trade can carry the
day well past −$300. Prop-firm daily-loss rules are breached intraday, not at
settlement; the projection must include open-position marks.

### 2.6 No trading calendar → positions can be held into 0DTE expiry

There is no holiday/early-close awareness anywhere. On 13:00 ET early closes
(July 3, day after Thanksgiving, Christmas Eve), `TIME_STOP = 15:25` never
fires before the market closes — a 0DTE position **expires**: worthless, or
ITM auto-exercise into an overnight SPY share position (an unhedged
several-hundred-share equity position on a leveraged account). Related:
expiry and log dates use the machine-local `datetime.date.today()`
(`main.py:55`, `strikes.py:193`, `state.py:63`) rather than the ET clock —
wrong around midnight on non-ET hosts, and it will happily construct symbols
for Saturday "expiries."

**Fix:** `pandas_market_calendars` (or a static NYSE table): refuse to start
on non-trading days, derive the session close per day, set the time stop
relative to it, and derive all dates from `datetime.now(ET)`.

### 2.7 Blocking synchronous REST inside the event loop — and entries inside the quote callback

Every trading REST call — `submit_order`, `get_order_by_id`,
`close_position`, `get_all_positions` — is a synchronous HTTP round trip
executed directly on the asyncio loop (`orders.py`, `main.py:611`). Each one
freezes *all* streams for its duration; the once-per-minute ghost sweep does
this every bar; a hung call trips the 20s watchdog into a restart **while
holding a position** (your own commit history records this cascade).

Worse: `on_option_quote` **awaits** `_evaluate_entry` (`main.py:240`), so the
entire buy path — submit plus up to 30s of fill polling — runs inside the
option-quote handler chain. While an entry is in flight, quote processing
stalls, which means the *just-opened position's exits are blind* during its
riskiest first seconds. This also falsifies the "sub-50ms quote-to-order"
README claim in the case that matters.

**Fix:** wrap all REST calls in `loop.run_in_executor` (or use an async
client); `asyncio.create_task(_evaluate_entry(sym))` exactly as the exit path
already does; drive fills from TradingStream events instead of polling.

---

## 3. SEV-2 findings — correctness and measurement

### 3.1 `signals.check_exit` is dead code that crashes if called

`signals.py:210-219` references `config.TARGET_1_MULT`, `TARGET_2_MULT`,
`BREAKEVEN_MULT` — none exist in `config.py` (verified by AST scan) →
instant `AttributeError`. Nothing imports it; `main.py` reimplements exits
inline. The partial-take-profit and momentum-flip exits this module
advertises were never wired in. In a paid product this is the first thing a
buyer greps. Delete it or implement it — shipping both is the worst option.

### 3.2 Systematically optimistic marks: trigger on mid, fill at bid

Exits trigger on mid (`main.py:712`) and fill at the bid via market close;
entries are limit at mid×1.02 (≈ crossing to the ask). On a $0.30 contract
with a $0.04 spread that's ~13% round trip before fees. Concretely, with the
shipped parameters:

- Gross payoff is symmetric: TP +50% / stop −50% → breakeven ≈ 50%.
- Net of ~15% round-trip friction: wins net ≈ +35%, losses ≈ −65% →
  **breakeven win rate ≈ 65%**.
- The peak trail arms at 1.20× and stops at 0.88×peak → minimum lock-in is
  **+5.6% gross** (`1.20 × 0.88 = 1.056`) — a guaranteed **net loser** after
  friction. Every "small green" trail exit in the dashboard is likely red in
  real terms.

**Fix (measurement):** log bid, ask, and mid at decision time *and* at fill
for every trade; compute per-trade slippage; mark the track record on
executable prices. **Fix (design):** either widen the trail floor above the
friction estimate or don't arm until TP-adjacent levels.

### 3.3 No fees anywhere

P&L = `(exit − entry) × qty × 100` (`state.py:96`). Alpaca charges $0
commission but regulatory/exchange fees (OCC clearing, ORF, SEC/TAF on
sells) are real — roughly $0.10–$0.60 per contract round trip. Multi-lot
cheap contracts (12 × $0.25 is a legal size under current config) make this
several dollars per trade against single-digit expected values. Add a fee
model to both booking and the dashboard.

### 3.4 `get_fill_price` returns 0.0 on missing data → books a total loss

`orders.py:187-191` returns `0.0` when `filled_avg_price` is `None`; no call
site guards it. One malformed order object books `pnl = −entry × qty × 100`
into the permanent record. Return `None` and force callers to handle it.

### 3.5 Strike construction is never validated against the chain

The validated path (`select_strikes`/`_validate_strikes`,
`strikes.py:66-149`) is **dead code**; the live path
(`compute_dynamic_strikes`, `strikes.py:174`) assumes SPY 0DTE strikes exist
at every $0.50 step across target ± 10 strikes. Where the chain is $1-spaced,
half the subscribed symbols don't exist — silently shrinking the real
tradeable window and skewing which strikes can ever fire. Validate once at
open against `get_option_chain` (the code for it already exists) and cache.

### 3.6 Recovered positions run with degraded protection

`main.py:445-482`: recovery sets `entry_time = now` (wrong duration stats),
`entry_spy_price = 0` (which **disables the SPY-level stop**, `main.py:660`),
`entry_atr5 = 0`, and uses Alpaca's day-average price as the basis for
TP/stop/trail — all subtly different from the original trade. Your own
dashboard has to quarantine these as "artifact rows" (`kpi_dashboard.py:37`).
Fix upstream: persist position metadata (entry time/price/SPY/atr5) to a
JSON state file on entry; reload on recovery; keep REST only as
cross-check.

### 3.7 Session/indicator hygiene

- VWAP and consecutive-bar counts reset on **date change**, not at 09:30
  (`momentum.py:96-98`) — premarket IEX bars contaminate VWAP and streak
  counts; README explicitly claims "resets at 9:30 ET".
- `_atr_5day` uses high−low, not true range (gaps ignored), and the
  `[-6:-1]` slice (`strikes.py:35`) drops the newest *complete* day whenever
  the query doesn't include a partial today-bar (premarket starts).
- `win_rate` counts P&L == 0 as a win (`kpi_dashboard.py:142`).
- `risk.py:47` logs the wrong variable (`_cooldown_bars`) in the max-trades
  message.

### 3.8 Timezone fragility across every artifact

Logging timestamps: machine-local (`main.py:56`). CSV `date`: machine-local
today. `entry_time`/`exit_time`: ET-aware ISO. Dashboard hour buckets:
`astimezone()` → machine-local (`kpi_dashboard.py:187`). Shadow matching
compares naive CSV times against log-line times (`kpi_dashboard.py:111-122`).
The whole pipeline is only coherent when the host runs in US/Eastern.
Canonicalize: UTC in storage, ET at the display edge only.

### 3.9 Data-staleness kill switch is missing

If the option stream dies *silently* (no exception — just no messages), the
30s monitor re-evaluates the same cached quote forever (`main.py:777`), and
the position's only live protection is the bar-driven SPY stop; if the stock
stream is also quiet, nothing protects it. The watchdog only detects a frozen
event loop, not dead feeds. Track `last_quote_age` and `last_bar_age`;
holding + quotes stale > N seconds ⇒ flatten and halt. This is the single
most important missing safety system for live capital.

### 3.10 Fragile dependency surface

- `feeds.py:108-110` calls the **private** `_run_forever()`; `requirements`
  pins only `alpaca-py>=0.43.0` (unbounded upward). Any minor release can
  break the bot at 09:31. Pin exact versions; add a lockfile.
- `main.py:544-547` invokes `subscribe_quotes` on the stream object **from an
  executor thread** while the loop thread uses it — alpaca-py streams are not
  documented thread-safe.
- API credentials live in a tracked source file (`config.py:5-6`) and the
  README instructs users to paste keys there. Buyers *will* commit their
  keys. Use environment variables / `.env`, and fail fast if unset.

---

## 4. Strategy review (the alpha itself)

**Thesis:** buy near-OTM 0DTE SPY options when 1-min momentum aligns and SPY
approaches the strike ("gamma explosion"). Honest assessment:

1. **The signal stack is one factor wearing five hats.** Close>VWAP, EMA5>
   EMA20, ROC5 ≥ threshold, ≥3 consecutive green bars (`momentum.py:130-141`)
   are all the same short-horizon trend measurement — they co-move almost
   perfectly. Requiring 3+ consecutive bars means entering ≥3 minutes into a
   move, i.e., buying gamma *after* the tape has repriced it. The strategy
   structurally buys convexity when it has just become expensive. This is the
   classic negative-selection problem of momentum + long-premium.
2. **No premium-richness awareness.** Entries are blind to what the option
   *costs* relative to what the tape is delivering. The single highest-value
   cheap addition: compare option premium against recent realized per-minute
   volatility (you already compute atr5) — an implied-vs-realized gate. Skip
   entries when the straddle-implied move dwarfs realized velocity.
3. **No event calendar.** `ENTRY_END = 14:30` straddles FOMC statements
   (14:00 ET). 0DTE around FOMC/CPI is a different asset class. Add an event
   blackout table before anything else strategy-side.
4. **Signals run on IEX** (~2–3% of consolidated volume; `feeds.py:40`,
   `main.py:994`). Thin-tape bars can print skewed OHLC vs the SIP; your
   stops and momentum both key off them. Paper-stage acceptable; for live
   trading (and for a $20k product claim) you need SIP equities + OPRA
   options data. The "indicative" options feed is sampled — `peak_mid` and
   fast stops are only as good as the feed.
5. **Exit geometry needs net-of-cost design** — see 3.2. The trail's +5.6%
   floor is a net loser; the 1:1 TP/stop needs ≈65% win rate after realistic
   friction. Redesign levels net-of-friction, or measure friction per trade
   and let the data set the levels.
6. **ORB shadow filter:** right methodology, underpowered data collection.
   Today it only scores signals that became trades. Log the decision for
   *every candidate signal* (including ones other filters rejected) with the
   full momentum snapshot — you'll multiply your counterfactual sample and
   can score alternative gates offline from the same logs.
7. **No replay harness.** The README's "backtests lie" argument is fair for
   *validation*, but one live day per calendar day means parameter iteration
   takes months. You are already receiving every quote tick — **record the
   raw streams to disk** (a few hundred MB/day compressed) and build a
   deterministic replayer that feeds the same `on_spy_bar`/`on_option_quote`
   callbacks. Same-code-path fidelity, infinite reruns. This is the highest
   ROI engineering item in this list.
8. **Statistics.** The dashboard reports win rate/PF/EV as point estimates.
   With ~1 month of paper trades, the EV t-stat is almost certainly
   indistinguishable from zero. Add: t-stat on per-trade EV, bootstrapped CI
   on EV and max drawdown, Sharpe/Sortino on daily P&L, SQN, and a
   worst-day/worst-streak panel. Prop-firm reviewers look for exactly these,
   plus honesty about them.

---

## 5. Prop-firm readiness — the actual path

First, be precise about which "options prop firm," because the answer changes
everything:

- **Funded-eval programs** (FTMO-style): most do **not** allow options at
  all, and many prohibit fully automated trading; the few options-adjacent
  ones are futures (ES/MES) — where this strategy's logic ports, but this
  codebase doesn't. Read the rulebook *before* building.
- **Capital-allocation / seat-based prop firms**: they don't buy frameworks;
  they allocate to *track records and risk discipline*. What they ask for:
  3–6+ months of **broker-verifiable** statements (not a self-published
  dashboard), live money (small size beats any paper record), a written risk
  framework, and evidence of drawdown behavior. Capacity is the one thing
  0DTE SPY genuinely has — that's a selling point at allocation time.

### Phased roadmap

**Phase 0 — Correctness (1–2 weeks).** Fix every SEV-1: confirmed-fill-only
booking (2.1), sweeper races (2.2), partial fills (2.3), sizing floor (2.4),
projected daily-loss gate (2.5), trading calendar + ET-derived dates (2.6),
non-blocking REST + task-spawned entries (2.7). Delete or implement
`check_exit`.

**Phase 1 — Measurement integrity (1–2 weeks).** Fees in booking and
dashboard; decision-vs-fill slippage logged per trade; order IDs in the CSV;
nightly reconciliation of CSV vs Alpaca account activity (fail loudly on
mismatch); UTC canonical timestamps; state file for position metadata. Your
track record becomes *auditable* — the thing every downstream conversation
depends on.

**Phase 2 — Risk hardening (1–2 weeks).** Kill switches: data staleness
(3.9), spread blowout while holding, consecutive order errors, clock drift,
excessive restarts/hour. Pre-trade checks: premium cap vs equity, projected
(not realized) daily loss, event blackout. Equity-fraction sizing instead of
fixed dollars.

**Phase 3 — Validation (parallel, 3–6 months).** Raw quote recording +
deterministic replay harness; parameter studies net of measured friction;
shadow-log every candidate signal; upgrade to SIP+OPRA data; then a small
**live** account ($2–5k, 1-lot) — live fills are the only slippage model
that counts, and live statements are the only track record that counts.

**Phase 4 — Portability & product (as needed).** A `Broker`/`DataFeed`
interface so the Alpaca coupling is one adapter (no prop firm runs Alpaca);
pydantic-typed config from env; structured (JSON) logs; a real test suite —
`signals`, `risk`, `strikes`, `momentum` are pure functions begging for
unit tests, and the recovery paths deserve fault-injection tests — plus CI.
Today the repo has **zero tests** and **no LICENSE file**; you cannot sell
software for $20,000 with no license terms and no test suite.

---

## 6. On selling this for $20,000

What a competent buyer's due diligence finds in the first day: a dead exit
module that crashes on nonexistent config keys, an unenforced risk cap, a
double-counting P&L path, mid-marked fee-free paper results, IEX-grade data,
no tests, no license, and a "paste your API keys into a tracked file"
onboarding. That kills a $20k price regardless of how good the rest is.

What is *actually worth money* here: the documented Alpaca failure modes, the
recovery engineering, the shadow-validation methodology, and (after Phase 1)
an auditable track record with honest friction accounting. Fix the SEV-1s,
add tests + license + replay harness, publish reconciled results, and the
price becomes an argument instead of a red flag. Alternatively, reframe the
product: the *framework + war stories* at a defensible price point, with the
strategy results as marketing rather than the deliverable.

---

## 7. Complete findings index

| # | Sev | File:Line | Finding |
|---|-----|-----------|---------|
| 1 | 1 | main.py:749,679,962 | Failed close books fictitious fill, erases tracking, double-counts via ghost path |
| 2 | 1 | main.py:596 | Ghost sweeper ignores `entry_pending` → can close a real in-flight entry |
| 3 | 1 | orders.py:154 | Partial fills unrecognized in all paths |
| 4 | 1 | risk.py:74 | `max(1, contracts)` violates per-trade risk cap up to 3.3× |
| 5 | 1 | risk.py:72,92 | Risk basis = 50% premium (true worst case 100%); daily loss checked only post-close |
| 6 | 1 | config.py:23; main.py:55; strikes.py:193 | No trading calendar; early closes → held into 0DTE expiry; local-clock dates |
| 7 | 1 | orders.py:*; main.py:240,611 | Sync REST on event loop; entry path blocks quote handler up to 30s |
| 8 | 2 | signals.py:210-219 | `check_exit` dead code referencing nonexistent config keys (crashes if called) |
| 9 | 2 | main.py:712,280 | Mid-marked exits vs bid fills; trail floor +5.6% gross = net loser; ~65% net breakeven WR |
| 10 | 2 | state.py:96 | No fee model in P&L |
| 11 | 2 | orders.py:187 | `get_fill_price` → 0.0 → books total-loss exit |
| 12 | 2 | strikes.py:174 | Live strike path never validates chain; validated path is dead code |
| 13 | 2 | main.py:445 | Recovered positions: SPY stop disabled, wrong basis/time |
| 14 | 2 | momentum.py:96; strikes.py:35 | VWAP/streaks reset at date change not 09:30; ATR drops newest day, ignores gaps |
| 15 | 2 | main.py:56; kpi_dashboard.py:187,111 | Timezone inconsistency across logs/CSV/dashboard |
| 16 | 2 | main.py:764 | No data-staleness kill switch; 30s net chews stale cache forever |
| 17 | 2 | feeds.py:108; main.py:544; requirements.txt | Private `_run_forever`; cross-thread `subscribe_quotes`; unbounded dependency pin |
| 18 | 2 | config.py:5 | Credentials in tracked source file |
| 19 | 2 | main.py:221 | Spread filter suppresses stop evaluation exactly during dislocations; not applied to entries |
| 20 | 3 | signals.py:39-70 | Proxy delta tracker: 1 sample/min, sign-agnostic clamp, disabled — dead weight |
| 21 | 3 | main.py:137,141,893; risk.py:47 | Duplicate `bar_time`; unused `feed` param; wrong variable in log |
| 22 | 3 | kpi_dashboard.py:142,37 | Zero-P&L counted as win; artifact filter is post-hoc heuristic (tag rows at write time instead) |
| 23 | 3 | main.py:295 | `cancel_all_options()` nukes every option order on the account, not just this bot's |
| 24 | 3 | repo | No tests, no CI, no LICENSE, no type checking |
