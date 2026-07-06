# Backtesting — Current Capability and Proposal

## 1. What exists today (honest assessment)

The framework has **deterministic session replay**, not backtesting:

| Capability | Status |
|---|---|
| Re-run a *recorded* session through the live code | ✅ `replay.py` — deterministic, conservative fills |
| Parameter sweeps on recorded sessions | ✅ `--set KEY=VALUE` + paired stats (`trade_stats.py --compare`) |
| Test against *historical* periods before recording began | ❌ does not exist |
| Test against specific past regimes (a vol spike, a grind, an FOMC cycle) | ❌ does not exist |

Replay is forward-collecting: the library grows one session per live trading
day. After three months you have ~60 sessions of gold-standard data — but
you cannot ask "how would this have behaved in the March 2025 vol event?"
because nothing was recorded then. That question is what backtesting answers,
and it is the gap.

## 2. Design philosophy: one engine, never two

The classic mistake is building a *second* simulator — a vectorized,
bar-based backtester with its own fill model and its own reimplementation of
the strategy. Two implementations of the same strategy **always** diverge
(entry-gate subtleties, exit ordering, partial-fill handling, session-time
edge cases), and every divergence silently invalidates the backtest. This
repo already paid down that risk: `replay.py` feeds events through the
identical live handlers.

**The proposal is therefore not a backtester. It is a data source.**

```
                          ┌──────────────────────────────┐
   live capture ────────► │                              │
   (recorder.py, gold)    │   the ONE replay engine      │ ──► trades CSV
                          │   (replay.py — live code,    │ ──► trade_stats
   historical backfill ─► │    SimClock, SimBroker)      │      (CIs, sweeps)
   (backfill.py, NEW)     │                              │
                          └──────────────────────────────┘
```

`backfill.py` synthesizes recording files — the *same* `.jsonl.gz` format
`recorder.py` writes — from Alpaca's **historical** REST data. The replay
engine, the fill model, the statistics layer, and the sweep workflow all
work unchanged. Zero new simulation code; every improvement to replay
automatically improves backtesting.

## 3. How backfill works

Per historical session date:

1. **Bars**: fetch SPY 1-min bars (historical REST, `feed=sip` — the
   consolidated tape is available historically even on accounts that stream
   IEX live).
2. **Strike determination is deterministic**: `compute_dynamic_strikes()` is
   a pure function of (bar closes, 5-day ATR baseline). Walk the day's bars
   *offline* to derive exactly which option symbols the bot would have
   subscribed — the target window plus re-subscription shifts. Typically
   ~42–80 symbols/day.
3. **Option quotes**: fetch historical option quotes (`OptionQuotesRequest`,
   tick-level NBBO) for **only those symbols** — this is what makes the
   fetch tractable (dozens of contracts, not the whole chain).
4. **Interleave** bars and quotes into receive order, synthesizing
   `recv_wall = exchange_ts + LATENCY_MS` (configurable, default ~50ms), and
   write the recording with metadata marked `"source": "backfill"` — every
   downstream report can (and must) disclose reconstructed provenance.
5. Compute the session's ATR baseline and momentum preseed from historical
   data the same way `main()` does at startup; store in metadata.

## 4. Fidelity tiers — label everything

| Tier | Source | Fidelity | Use for |
|---|---|---|---|
| **A** | Live capture (`recorder.py`) | Gold — true receive order, true gaps, the tape the bot actually saw | Final validation, track record |
| **B** | Backfill from historical tick quotes | Silver — real NBBO ticks, synthetic receive times, no feed outages | Parameter studies, regime testing |
| **C** | Backfill from 1-min option bars (if tick history unavailable) | Bronze — intra-minute path unknown; peak-trail and fast stops unreliable | Rough viability screens ONLY, pessimistic settlement rules required |

Tier B is the target. Tier C should be implemented only as a fallback and
its reports stamped accordingly.

**The acceptance test that makes Tier B trustworthy** (this is the
non-negotiable part): take N sessions that were BOTH live-captured and
backfilled, replay each pair, and diff the outcomes (trades, fills,
P&L). The measured divergence — the *reconstruction error* — is reported
with every backfill study. If entry fills differ by more than ~1 tick or
trade sets diverge on >5% of sessions, the latency model gets tuned before
any conclusions are drawn. Backtests earn trust by being checked against
ground truth, not by being plausible.

## 5. Data requirements and constraints

- **History depth**: Alpaca's historical options data begins **Feb 2024** —
  the backtest horizon is bounded there. That still covers multiple distinct
  regimes (2024 grind, Aug-2024 vol event, 2025 cycles).
- **Subscription**: historical option *quotes* at tick level require the
  paid Alpaca options data subscription (the same OPRA entitlement now
  supported live via `ALPACA_OPTION_FEED=opra`). Without it, only Tier C is
  possible.
- **Volume**: ~50 symbols × one session of NBBO ticks ≈ a few million rows;
  fetched once per session, cached as the recording file forever. Rate
  limits make first-time backfill of a year of sessions an overnight batch
  job, not an interactive one — design it resumable (skip already-built
  recordings).
- **Survivorship/lookahead**: none — strikes are derived from bars only
  (information available at the time), and the chain existence check uses
  the historical chain for that date.

## 6. Implementation plan

| Step | Deliverable | Size |
|---|---|---|
| 1 | `backfill.py`: bars → offline strike walk → quote fetch → interleave → recording file; resumable batch mode (`--from 2025-01-01 --to 2025-06-30`) | ~300 lines |
| 2 | Provenance: `"source": "backfill"` in metadata; `replay.py` prints it; `trade_stats.py` reports refuse to silently mix Tier A and Tier B samples (separate sections or explicit `--allow-mixed`) | ~50 lines |
| 3 | Reconstruction-error harness: `backfill.py --verify` on dates with live captures; report per-session diff table | ~100 lines |
| 4 | Docs + tests (offline strike-walk determinism, interleave ordering, verify-mode diff on synthetic fixtures) | — |

No changes to replay, stats, or the live bot are required — that is the
point of the architecture.

## 7. What was deliberately rejected

- **A vectorized bar-based backtester** (pandas/vectorbt style): fast, and
  wrong for this strategy — quote-driven exits, resting-limit entries, and
  the peak trail all live *inside* the minute. Rejected.
- **Third-party frameworks** (backtrader, zipline, etc.): require
  reimplementing the strategy in their dialect — reintroducing the
  two-implementations divergence this repo's whole design avoids. Rejected.
- **Synthetic option pricing** (Black-Scholes quotes from SPY bars): 0DTE
  microstructure (spread dynamics, pin behavior, event vol) is exactly what
  BS misses, and it's exactly what this strategy trades. Real recorded/
  historical NBBO or nothing. Rejected.
