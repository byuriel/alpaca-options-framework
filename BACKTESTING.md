# Backtesting — Assessment, Design, and Fidelity Contract (v2)

*v2 is the output of an adversarial review of the original proposal. The
material changes are listed in §2 — including one fatal data-availability
error in v1 that would have stopped implementation on day one. A design
document that hides its own revision history is not one to trust.*

## 1. What exists today (honest assessment)

| Capability | Status |
|---|---|
| Re-run a *recorded* session through the live code | ✅ `replay.py` — deterministic, conservative fills |
| Parameter sweeps on recorded sessions | ✅ `--set KEY=VALUE` + paired stats (`trade_stats.py --compare`) |
| Feed-gap / staleness behavior reproduced in replay | ✅ (added in this review — see §2) |
| Test against *historical* periods before recording began | ❌ does not exist |
| Test against specific past regimes (a vol spike, a grind, an FOMC cycle) | ❌ does not exist |

Replay is forward-collecting: the library grows one gold-standard session per
live trading day, at zero marginal cost. Backfill — reconstructing sessions
the bot never ran — is the gap this document designs.

## 2. What the v1 review found (and what was done about it)

1. **FATAL — v1 assumed an Alpaca endpoint that does not exist.** The design
   called for "historical option quotes (tick-level NBBO)" from Alpaca.
   Alpaca's options data API provides historical **trades** and **bars**,
   plus **latest** quotes/snapshots — there is **no historical option NBBO
   quote time series** (confirmed against the SDK surface and Alpaca's open
   feature-request tracker). → v2 makes the data source **pluggable** (§4);
   option NBBO history comes from a dedicated provider.
2. **Engine gap — replay ignored the staleness kill switch.** A recorded
   session containing a real feed gap would have flattened live at
   gap+45s, but replay rode through it — divergence on exactly the sessions
   with data incidents. → **Fixed in code** (replay simulates the staleness
   flatten; pinned by a test where a 4-minute quote gap exits at the last
   executable bid, reason `stale_data`).
3. **Irreversible data loss — NBBO sizes were being discarded.** The live
   stream carries `bid_size`/`ask_size`; the recorder dropped them, and size
   history cannot be retro-captured. → **Fixed in code** (recording format
   now carries sizes; readers stay backward-compatible). This enables a
   size-aware fill model later (§6.5).
4. **Underspecified — subscription-state reconstruction.** v1 hand-waved
   which quotes the bot "would have seen". v2 proves it is exactly
   derivable (§5).
5. **Unstated fidelity assumptions** — feed consistency, bar adjustment
   mode, latency model, data revisions. v2 pins each one (§6).
6. **Missing entirely — a research protocol.** An engine without sweep
   governance is an overfitting machine (§9).
7. **Volume estimate low by ~10×** — corrected (§10). **Tier C demoted**
   from "fallback" to rejected-for-production (§8).

## 3. Design principle: one engine, never two

The classic failure is a *second* simulator — vectorized, bar-based, with
its own fill model and a reimplementation of the strategy. Two
implementations always diverge, and every divergence silently invalidates
the backtest. This repo already paid down that risk: `replay.py` feeds
events through the identical live handlers.

**Backfill is therefore not a backtester. It is a data source.**

```
   live capture ──────────┐
   (recorder.py — gold,   │      ┌──────────────────────────────┐
    zero marginal cost)   ├────► │   the ONE replay engine      │ ─► trades CSV
                          │      │   (replay.py — live code,    │ ─► trade_stats
   backfill.py (NEW)      │      │    SimClock, SimBroker)      │    (CIs, paired
   ┌────────────────────┐ │      └──────────────────────────────┘     sweeps)
   │ provider adapters: ├─┘
   │  stock bars: Alpaca (SIP, RAW adjustment)
   │  option NBBO: Polygon / ThetaData / Databento  ← NOT Alpaca (see §4)
   └────────────────────┘
```

`backfill.py` synthesizes recording files — the same `.jsonl.gz` format
`recorder.py` writes — so the engine, fill model, statistics, and sweep
workflow work unchanged, and every future replay improvement automatically
improves backtesting.

## 4. Data reality (verified, not assumed)

| Need | Source | Notes |
|---|---|---|
| SPY 1-min bars, historical | **Alpaca** (`feed=sip`, `adjustment=RAW`) | SIP history available regardless of live-stream tier. RAW is mandatory — adjusted prices shift off the option strike grid. |
| Option chain per historical date | **Alpaca** | for existence validation, as live |
| Option NBBO tick history | **Polygon.io** (`/v3/quotes/{contract}`), **ThetaData**, or **Databento** (OPRA) | Alpaca does not offer this. Adapter interface keeps the choice open; Polygon is the pragmatic default (per-contract REST, years of OPRA history). |
| Option trade/bar history | Alpaca has it | insufficient for this strategy — see Tier C rejection (§8) |

The adapter interface is small — `stock_bars(date)`,
`option_quotes(symbol, date)`, `chain(date)` — and the recording file is the
boundary: the engine never learns which provider produced the ticks.

## 5. Subscription reconstruction is exactly derivable (proof sketch)

Which quotes did the live bot *see*? Only subscribed symbols'. The live
subscription set is **add-only within a session** (`add_option_symbols`
never unsubscribes), and every addition is triggered by bar-close prices
alone: the 9:30 open bar fixes the initial window; the re-subscribe watcher
adds a new window whenever SPY's bar-close price moves ±$3 from the last
anchor; the held-symbol pin is a no-op for the quote stream because a held
symbol was necessarily already subscribed when entered. Therefore the
per-symbol subscription start times are a pure function of (bars, ATR
baseline) — the offline walk computes them exactly, and backfill emits each
symbol's quotes only from its subscription time onward. No superset
approximation, no lookahead: strikes derive from bars available at the time,
and chain validation uses that date's chain.

## 6. Fidelity contract (each item is a divergence source if unpinned)

1. **Feed consistency.** Backfill bars are SIP. If the live sessions used
   for verification ran on IEX, verify-mode diffs conflate feed deltas with
   reconstruction error. Rule: verification compares like-for-like feeds —
   which is one more reason to run live on SIP/OPRA (now supported) before
   building the verification library.
2. **Adjustment mode pinned to RAW** on every historical bar request.
3. **Latency is calibrated, not assumed.** Live recordings carry both
   `recv_wall` and exchange timestamps per event — the Tier A library yields
   the empirical latency distribution for quotes *and* bar-arrival delays.
   Backfill synthesizes `recv_wall = exch_ts + median_latency` (seeded
   sampling from the measured distribution as an option). No magic 50ms.
4. **Historical data is revised; recordings are immutable.** A re-fetch
   months later can differ. Every backfilled recording stamps
   `source: backfill`, provider, fetch timestamp, and a content hash in its
   metadata; studies cite recording hashes. Reproducible research, not
   "whatever the API returned that day".
5. **Sizes.** The recording format now carries NBBO sizes (live capture
   banking them from today). Backfill providers supply historical sizes.
   This enables an optional size-capped fill model in SimBroker later; at
   this strategy's 1–12 contract clips it rarely binds, but the data must
   exist before the model can.
6. **Provenance segregation downstream.** `replay.py` prints the recording's
   source; `trade_stats.py` must refuse to silently pool Tier A and Tier B
   samples (separate sections, or an explicit `--allow-mixed`).

## 7. The verification harness (what makes any of this trustworthy)

For every session that was BOTH live-captured and backfilled, replay each
and diff, in decomposition order:

1. **Subscription-set match** — if symbol sets differ, everything downstream
   is tape divergence, not model divergence; report and stop there.
2. **Trade-set overlap** (Jaccard on entry decisions ± a tolerance window).
3. **Per-matched-fill price differences** (in ticks, signed — detects
   systematic optimism, not just noise).
4. **Session P&L difference distribution.**

The aggregate is the **reconstruction error**, and it is reported alongside
every backfill study, permanently. Acceptance gate to trust Tier B at all:
trade sets match on ≥95% of verification sessions and matched fills agree
within one tick at the median. Fail → tune the latency model, re-verify —
never proceed on vibes. Backtests earn trust by being checked against
ground truth, not by being plausible.

## 8. Fidelity tiers

| Tier | Source | Status |
|---|---|---|
| **A** | Live capture | Gold. The default and the ground truth. |
| **B** | Backfill from provider NBBO ticks | Silver. Valid only under the §6 contract with §7 verification passing. |
| **C** | Backfill from option *trade* bars (Alpaca-only path) | **Rejected for production.** The strategy's economics live inside the minute (quote-driven stops, peak trail, resting-limit entries) and option trade-bars have empty minutes on quiet strikes; no settlement rule recovers information that isn't there. Permissible only as a coarse viability screen, output stamped as such. |

## 9. Research protocol (the part that prevents self-deception)

The engine makes experiments cheap; cheap experiments are how strategies
get overfit. Non-negotiable workflow:

1. **Split before looking.** Designate an out-of-sample session set (e.g.
   most recent 25% plus one full vol regime) that sweeps never touch. It is
   evaluated ONCE, when a parameter change is already accepted in-sample.
2. **Every sweep declares its family size.** `trade_stats.py --compare
   --variants-tested K` exists precisely for this; the Bonferroni-adjusted
   p is the only quotable number for the best of a sweep.
3. **Experiment ledger.** A plain CSV: date, hypothesis, sessions used,
   variants tested, adjusted p, decision. The ledger count is the true K
   accumulating across the project's life — the basis for deflated
   performance estimates (deflated Sharpe is a planned `trade_stats`
   addition once daily samples justify it).
4. **Regime stratification.** Backfill exists to buy regime coverage —
   report results split by regime (realized-vol terciles, trend/chop days,
   event days), never only pooled. An edge that lives in one regime is a
   regime bet, and should be known as one.
5. **Recording hashes in every study** (§6.4) so any result can be re-run
   bit-for-bit.

## 10. Scale and cost honesty

- **Volume:** 40–80 near-money SPY 0DTE contracts can print *tens of
  millions* of NBBO ticks on a volatile day (not "a few million" as v1
  claimed). Replay at ~30–50µs/event → 10–30 min per session,
  single-threaded.
- **Parallelism:** sessions are independent — run per-session processes;
  a year backfills overnight on a modest machine.
- **Conflation knob** (e.g. keep ≤N quotes/sec/symbol) exists as an
  explicit fidelity trade-off for coarse screens — never the default, and
  stamped into the recording metadata when used.
- **History depth:** bounded by the chosen NBBO provider (Polygon/Databento
  reach back years; SPY daily-expiry structure limits how far back "0DTE
  every day" itself existed — full daily expirations date from 2022–2023).
- **Provider cost:** an options NBBO history subscription (order of
  $100–200/month retail tiers, or metered on Databento). Weigh against the
  free alternative: the live recorder banks a gold session every day.

## 11. Implementation plan

| Step | Deliverable | Size |
|---|---|---|
| 1 | `HistoricalSource` adapter protocol + Alpaca stock-bars adapter + Polygon option-NBBO adapter | ~200 lines |
| 2 | Offline subscription walk (§5) — pure function, unit-tested against the live routing logic | ~100 lines |
| 3 | Interleave + latency synthesis (calibrated from Tier A library) + recording writer with provenance/hash metadata; resumable batch CLI (`--from --to`) | ~150 lines |
| 4 | Verification harness (§7) with per-session diff report | ~150 lines |
| 5 | Provenance guards in `replay.py` / `trade_stats.py` (no silent A/B pooling) | ~50 lines |

Engine changes required: **none** (the two found in review are already
merged). That remains the point of the architecture.

## 12. Strategic sequencing (do the free thing first)

1. **Now:** keep the recorder running every session (it is on by default) —
   the gold library grows daily at zero cost, and it doubles as the
   latency-calibration and verification corpus backfill will need.
2. **Trigger for building backfill:** a concrete regime question the live
   library cannot answer (e.g. "does the ATR gate survive a vol spike?") —
   that justifies the provider subscription and steps 1–4.
3. **Never:** skip §7 verification because the backfill "looks right".

## 13. Rejected alternatives (unchanged from v1, plus one)

- **Vectorized bar-based backtester** — the strategy's economics are
  intra-minute; rejected.
- **Third-party frameworks** (backtrader/zipline/…) — require a second
  strategy implementation; rejected.
- **Synthetic option quotes from Black-Scholes on SPY bars** — 0DTE
  microstructure is exactly what BS misses and exactly what this strategy
  trades; rejected.
- **Alpaca-only backfill** *(new in v2)* — no historical option NBBO
  exists there; the trades/bars path is Tier C, rejected for production.
