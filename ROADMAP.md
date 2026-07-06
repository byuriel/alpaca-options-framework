# Roadmap — Remaining Work to Institutional Grade

*Prioritized gap analysis after the execution-integrity overhaul, record/
replay, statistics layer, event calendar, and SIP/OPRA support. Ordered by
(risk reduction ÷ effort), not by how interesting the work is. Items are
deliberately small and verifiable.*

## Feed facts — verified, corrected, and one open question

- **Confirmed:** feed access is gated by subscription and an unentitled
  request fails at authentication — which is what the startup probe is for.
  The options stream is msgpack-only (alpaca-py handles it).
- **Correction to earlier analysis:** the exact sampling cadence of the
  indicative options feed is **not publicly specified** in Alpaca's
  retrievable docs. "~1 update/second" is community lore, not a spec. The
  institutional response to a vague vendor spec is to *measure the feed* —
  the recorder already banks receive timestamps per quote, so a
  feed-quality report is a pure post-processing job (Tier 1, item 2).
- **New finding:** Alpaca documents a **30-symbol WebSocket limit on the
  Basic (free) plan**. This bot subscribes **43+ option symbols** (target ±
  10 strikes × 2 sides, add-only growth intraday, plus SPY). Quotes have
  empirically flowed in paper sessions, but *coverage has never been
  measured* — if the limit applies to the options stream, part of the
  strike window may be silently dark, which would bias which strikes ever
  fire. Item 2 turns this unknown into a daily number.
- Algo Trader Plus (~$99/month — verify current pricing) lifts the symbol
  limit and provides SIP + OPRA.

---

## Tier 1 — Trust and safety gates before live money

**1. Nightly broker reconciliation** *(the most important unbuilt item —
promised in QUANT_REVIEW Phase 1 and still missing)*
Compare the local trades CSV against Alpaca's account activities API after
each session: every fill matched by order ID, quantities and prices equal,
fees accounted. Any mismatch → CRITICAL alert + risk gate locked at next
start. An unreconciled track record is a self-published claim; a reconciled
one is evidence. ~150 lines + tests.

**2. Feed coverage & quality monitor**
At EOD (and stamped into recording metadata): subscribed symbols vs symbols
that actually delivered ≥1 quote; per-symbol quote inter-arrival
distribution; session spread statistics. Resolves the 30-symbol question
empirically, detects dead subscriptions and nonexistent strikes, and — once
OPRA is enabled — quantifies exactly what the indicative feed was hiding by
before/after comparison. ~100 lines, pure post-processing of existing data.

**3. Critical-event alerting**
The bot currently tells no one when it matters: staleness flatten, EXIT
FAILED + gate lock, watchdog restart, event flatten, reconciliation
mismatch. Add a webhook/SMTP hook (env-configured, stdlib) fired on CRITICAL
log events. A machine that flattens at 11:00 and stays silent until you
check the terminal is not an unattended system. ~80 lines.

**4. Restart-storm brake**
The watchdog restarts without limit. File-based counter: >N restarts/hour →
flatten via REST, write a halt marker, refuse to trade until manually
cleared. Converts an infinite failure loop into one loud stop. ~60 lines.

**5. Catastrophic-loss backstop + weekly limit**
A gap through the −50% stop currently has no second line until the position
hits zero. Add: hard exit at −80% of premium regardless of stop logic, and a
weekly loss limit backed by the CSV history (daily limits alone let five
max-loss days compound). ~60 lines.

## Tier 2 — Measurement depth (the allocator's questions)

**6. Execution-quality section in `trade_stats`**
The CSV already logs decision bid/ask and per-side slippage — nothing
aggregates it. Report: effective spread paid per round trip, slippage by
exit reason / hour / spread decile, and the gap between mid-marked and
executable P&L. This is the number that decides whether exit levels need
redesign, and the first table a trading-cost reviewer asks for.

**7. Internal latency telemetry**
Timestamp decision → submit → fill per order into the CSV. Replaces the
README's "sub-50ms" folklore with a measured distribution; feeds the replay
latency calibration required by BACKTESTING.md §6.3.

**8. Deflated Sharpe / PSR in `trade_stats`**
Once ≥ ~60 daily observations exist: probabilistic Sharpe ratio and
deflated Sharpe using the experiment ledger's cumulative trial count K
(Bailey & López de Prado). Stdlib-implementable; the natural extension of
the existing sweep governance.

**9. Regime tagging**
Tag each session (realized-vol tercile from SPY bars, overnight gap size,
day-of-week, event flags already recorded) and report stats stratified —
an edge that exists only in one regime should be known as a regime bet.

**10. Experiment ledger CLI**
Tiny append-only tool: hypothesis, sessions used, `--variants-tested`,
adjusted p, decision. The ledger's running K is the honest input to item 8.

## Tier 3 — Scale and portability

**11. Broker abstraction** — `Broker`/`DataFeed` interfaces so Alpaca is one
adapter; prerequisite for any prop-firm deployment (they won't be on
Alpaca). Largest single item; do it when a real second venue exists.

**12. `backfill.py`** per BACKTESTING.md v2 — trigger-based: build when a
specific regime question justifies the option-NBBO provider subscription.

**13. Size-aware SimBroker fills** — the NBBO sizes are being banked now;
add a fill model capped at displayed size when the data supports it.

**14. Ops hardening** — structured JSON logs + rotation, systemd unit,
recordings retention policy (~30–60 MB/day growth), disk-space guard.

**15. Provenance stamp** — git commit hash into recordings and log headers
so every artifact names the code that produced it.

## Explicit non-goals (for now)

Multi-symbol universes, concurrent positions, spread structures, ML signal
layers, latency-competitive execution. All premature before a single
strategy has a statistically established, reconciled, regime-qualified edge
— complexity added before that point only manufactures more ways to fool
yourself.
