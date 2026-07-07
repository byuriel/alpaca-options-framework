# ES Futures Port — Decomposition & Implementation Plan

*Status: SIGN-OFF RECEIVED (sibling-strategy basis, Q8 = yes). Phase 1 is
built and conformance-verified — see §11 for the resolved decisions, the
Apex compliance pivot, and what exists now. §§0–10 below are the original
pre-build analysis, kept verbatim as the record; where later evidence
corrected it, §11 says so explicitly.*

---

## 0. Executive verdict — read this before anything else

**A high-fidelity port of the SIGNAL layer is feasible. A "duplication of
the strategy" is not, and I want that on the record before a line of code
exists.**

Three structural facts, from the code itself:

1. **The strategy's own thesis is convexity harvesting.** It buys cheap
   near-OTM 0DTE gamma ahead of momentum continuation ("gamma explosion").
   The options version can be profitable with a sub-50% directional hit
   rate because winners are convex multiples of a small fixed premium.
   A linear ES position keeps the *timing* and discards the *asymmetry*:
   the identical signals on ES must clear >50% directional accuracy net of
   costs. If the live edge (n is still tiny — see §9) turns out to be
   substantially convexity-derived, **ES cannot reproduce it**, full stop.

2. **The exit stack is priced in premium space and has no ES analog.**
   TP = 1.5× premium, stop = 0.5× premium, trail on option mid, cat-stop at
   0.2× premium. On a 0DTE option those thresholds are nonlinear functions
   of spot, time, and IV. Most important: **theta acts as a stop** — a flat
   tape bleeds premium to −50% and exits the options trade with no price
   move at all. ES flat = P&L flat: the naive port would HOLD positions the
   options system exits, silently changing the trade distribution. Exits
   must be **re-derived in underlying-price space**, not translated by
   formula. (One component already lives in underlying space and ports
   cleanly: the SPY-level stop, `0.75 × atr5` on bar close.)

3. **The "activation zone" trigger is partly a strike-grid artifact.** See
   §2.3 — the interaction of the ATR-scaled OTM offset, the ±$3 subscription
   window, and the $0.50 strike grid functions as a de facto *volatility
   regime filter* (high 5-day ATR pushes the tradeable window away → few
   entries; low ATR admits them). That behavior is load-bearing for WHICH
   momentum signals get taken, and it is an instrument artifact. Porting it
   requires a conscious decision (§2.3 options), and the parity harness must
   A/B it — this is the most likely source of silent strategy change.

**Bottom line:** what ports is a *momentum-timing day strategy on ES that
shares the SPY system's directional trigger*. It is a sibling strategy with
a common signal engine — not a clone. Its economics (breakeven hit rate,
loss profile, cost structure) are different by construction, and the plan
below treats it as a new strategy requiring its own validation, sized from
scratch. If you want me to proceed on that honest basis, say so explicitly.

---

## 1. Layer decomposition — component inventory

| # | Component (file) | Layer | Portability verdict |
|---|---|---|---|
| 1 | Momentum direction: close>VWAP ∧ EMA5>EMA20 ∧ ROC5≥3bp ∧ ≥3 consec bars (`momentum.py`) | **A** | **PORTABLE.** Pure price-series logic. Caveats: computed today on SPY IEX 1-min bars — ES bars differ (basis, dividends, tape); ROC threshold is *relative* (portable as-is); VWAP must stay RTH-anchored (§4). |
| 2 | atr5 velocity gate ≥ 0.20 SPY pts (`config.ATR5_MIN_ENTRY`) | **A** (units are B) | **PORTABLE after re-basing.** Absolute SPY points. ES trades ~10× SPY's index level; translate as a ratio-to-price or re-fit on ES bars (recommend: re-specify as fraction of price, e.g. 0.20/spot ≈ 3.2bp, then re-validate). |
| 3 | Entry window 09:45–14:30 ET; time stop 15:25 ET (`config`) | **A** | **PORTABLE** (RTH policy decision in §4). |
| 4 | Event blackout: FOMC entry block 13:30, flatten 13:45 (`event_calendar.py`) | **A** | **PORTABLE** unchanged — arguably more important on ES (no premium cap on event gap risk). |
| 5 | Cooldown 3 bars, daily/weekly loss gates, prospective gating (`risk.py`) | **A** | **PORTABLE** as policy; dollar values re-derived (§5). Prop-firm rules (trailing DD) must be encoded if applicable — open question Q1. |
| 6 | ORB shadow regime filter (`orb_filter.py`) | **A** | **PORTABLE** (still shadow-only). |
| 7 | Strike selection: target = spot + min(0.6×ATR5d, $4.50) on $0.50 grid (`strikes.py`) | **B** | **NOT PORTABLE AS-IS** — but load-bearing for entry timing via #8. See §2.3. |
| 8 | Activation zone: fire only when spot within 0.3% of a subscribed OTM strike; OTM-side gate (`signals.py`) | **B masquerading as A** | **THE decision point.** Economically = "price has approached a predefined level derived from recent vol." Options in §2.3; must be A/B'd in the harness. |
| 9 | Premium band $0.20–$10 (`OPTION_MIN/MAX_PRICE`) | **B** | **NOT PORTABLE.** Implicit moneyness/vol filter. Dropped on ES; its regime-filtering side effect (if any) must be measured in the divergence report. |
| 10 | Proxy delta gates (`PROXY_DELTA_MIN=0`, `REQUIRE_DELTA_RISING=False`) | **B** | Dead in config (disabled). **Dropped.** No behavior change. |
| 11 | Entry execution: limit at option-mid×1.02, 30s fill wait, partial adoption (`orders.py`) | **B** | Replaced by futures adapter: marketable limit on ES book (§6). Slippage model changes entirely. |
| 12 | TP 1.5× / stop 0.5× premium (`_evaluate_exit`) | **B** | **NOT PORTABLE.** Re-derive in price space (§3). |
| 13 | Peak trail: arm 1.2×, trail 0.88× on option mid | **B** | **NOT PORTABLE** (mid-of-option space). Price-space trail proposed in §3. |
| 14 | SPY-level stop: 0.75×atr5 beyond entry spot, bar close (`_evaluate_spy_stop`) | **A** | **PORTABLE DIRECTLY** — already an underlying-space stop. Becomes the *primary* stop on ES (it was the backup on options). |
| 15 | Theta as implicit flat-market exit | **B (implicit!)** | **NO ANALOG.** Requires an explicit max-hold-time / stagnation exit on ES or the port holds dead trades the original exits (§3.4). |
| 16 | Cat-stop 0.2× premium; staleness flatten; wide-spread executable-price logic | **B / infra** | Cat-stop → replaced by a **resting protective stop order** (futures allow real stops — an *upgrade* over Alpaca's close-only options API). Staleness/kill-switch policies port as infra. |
| 17 | Premium-based sizing: `floor($150 / (premium×0.5×100))` (`risk.py`) | **B** | **CATEGORY ERROR to port.** Re-derived in §5. |
| 18 | Measurement stack: recorder, replay, decision log, drift report, trade stats, reconcile | infra (A) | **PORTABLE as the brain** — NT writes our CSV/recording schemas via an adapter; the analytics run unchanged (§7). |

**Share-of-edge assessment (honest):** with the live sample still tiny, no
one can decompose the edge empirically yet. Structurally: timing (#1–4) is
portable; the convexity payoff (#12–13,15) and the vol-regime side effects
of the strike machinery (#7–9) are not. Expect the ES sibling to need a
**meaningfully higher hit rate** than the options original for the same
signals. The parity harness quantifies exactly this instead of guessing.

---

## 2. The three hard translation problems

### 2.1 Exits (premium space → price space)
Proposed ES exit stack, all in ES points, all explicit (§3 details):
stop = k_s×atr5 (the existing SPY-level stop promoted to primary, as a
RESTING stop order, not a soft trigger); target = k_t×atr5; trail arms at
a_t×atr5 favorable and trails p_t×peak-excursion; max-hold-time exit
(theta's replacement); time stop 15:25 unchanged. Initial k’s set to match
the *underlying-move equivalents* observed in the options trade history
(measured from recorded sessions — we have the data), then swept on replay.

### 2.2 The theta problem
Options version: flat tape for ~25–40 min ≈ −50% premium ≈ stop-out.
ES version must add an explicit **stagnation exit** (e.g., exit if
excursion < x×atr5 after N minutes) or accept a structurally different
holding distribution. I recommend implementing it and A/B-ing it in the
harness — defaulting to "no stagnation exit" silently un-ports the exit
behavior.

### 2.3 The activation zone
Three candidate translations, to be run head-to-head in the harness:
  (a) **Geometric replication**: synthetic "strike grid" on ES at 5-pt
      spacing (≈ $0.50 on SPY ×10), same offset/zone arithmetic. Maximum
      fidelity to *behavior*, zero economic rationale on a strike-less
      instrument.
  (b) **Re-derived trigger**: drop the grid; fire when momentum aligns AND
      price has traveled ≥ f(ATR5d) from a session anchor (captures
      "approaching the level" without grid artifacts).
  (c) **Momentum-only**: drop #7–9 entirely; measure what the zone was
      actually contributing.
Parity target: (a) must reproduce options-side entry timestamps near-
exactly; (b)/(c) are controlled deviations whose deltas get quantified.

---

## 3. Explicit risk replacement (nothing implicit survives)

Options implicit protections → ES explicit rules:

| Implicit (options) | Explicit (ES) |
|---|---|
| Max loss = premium | **Resting stop order at entry ± k_s×atr5, placed at fill time, CME-side.** Never soft-only. |
| Expiry ends the trade | Time stop 15:25 ET + supervisor flat-check (ports) |
| Theta bleeds dead trades out | Stagnation exit (§2.2) |
| Premium cap bounds event gaps | FOMC flatten (ports) + **no overnight, ever** + weekly loss gate |
| Cat-stop at −80% premium | Disaster stop: hard bracket at 2×k_s (server-side), independent of the soft logic — same two-independent-paths philosophy as the current bot |

Gap risk: intraday-only + resting stops bounds but does not eliminate it
(stops can gap through on halts/events). Modeled in the risk report via
worst-case slip-through scenarios (limit-down tick tables).

## 4. Contract, session, data policy (proposed — confirm)

- **Instrument: start on MES** ($5/pt, $1.25/tick). ES's $12.50/tick makes
  the smallest stop ≈ the whole current per-trade budget (§5); MES gives
  10× sizing granularity for validation. Graduate to ES on evidence.
- **RTH-only (09:30–16:00 ET / 08:30–15:00 CT)**, matching the SPY system's
  definition exactly: VWAP anchored at RTH open, momentum/ATR from RTH bars
  only, no positions outside the window. ETH is a different regime the
  original never traded; mixing it in would be a silent strategy change.
- **Timezones:** all internal decision times remain ET via the existing
  `clock` module (the strategy's windows are NYSE-anchored); CME contract
  times converted explicitly. No naive datetimes anywhere (already the
  repo's rule).
- **Continuous series (backtest only):** roll on volume crossover (typically
  ~8 days before expiry, quarterly H/M/U/Z); **back-adjusted series used
  ONLY for return/indicator computation; all stops, targets, zone levels,
  and fills computed on unadjusted front-month prices.** Live trading always
  front-month by volume; the strategy is intraday-flat so roll cost is a
  backtest-continuity concern, not a live P&L line.
- **Fills:** never at mid. Entry modeled as marketable limit crossing the
  spread (1 tick half-spread) + 1 tick adverse slippage baseline (sweep 0–2
  in sensitivity); stops filled at stop price + 1 tick slip baseline
  (sweep to 4 for event bars). Costs per side per contract: commission +
  exchange + clearing + NFA (venue-dependent — Q4); ~$1.00–1.60/side/MES
  round numbers to be pinned to the actual broker schedule.

## 5. Sizing translation (the mapping math)

Options risk unit: premium at risk. `floor(150 / (premium × 0.5 × 100))`.
ES risk unit: `dollar_risk = stop_ticks × tick_value × contracts`.

With stop = 0.75 × atr5_ES. Example at atr5_ES ≈ 2.5 pts (≈ today's SPY
0.25 × 10): stop distance = 1.875 pts = **7.5 ticks** (rounded to 8):

- **ES:** 8 ticks × $12.50 = $100/contract → $150 budget → **1 contract**,
  and any atr5 > ~2.9 pts forces 0 contracts (skip) — the same
  no-rounding-up rule as `risk.size_trade`, which is why MES matters.
- **MES:** 8 ticks × $1.25 = $10/contract → **floor(150/10) = 15 MES**, with
  granularity to scale risk smoothly with vol.

Daily $300 / weekly $900 gates port unchanged (prospective, as now).
If this runs on a funded prop account, **trailing drawdown replaces these
as the binding constraint and sizing must be re-derived against it** — Q1.

## 6. Architecture: shared engine, swappable adapters (no fork)

The repo already separates signal (`momentum.py`, `signals.py` gates) from
execution (`orders.py`/SimBroker). The port adds:

```
signal engine (shared, existing)        adapters
 momentum, gates, calendar, risk  ──►  options adapter (exists: Alpaca)
 decision logger, drift, stats    ──►  futures adapter (new): sizing in
                                        ticks, bracket/stop orders,
                                        stagnation exit, MES/ES contract
                                        math, FuturesSimBroker for replay
```

**The NinjaTrader question (Q5, most consequential):** NT8 strategies are
C# NinjaScript on Windows; this engine is Python. Two honest options:

- **(i) C# NinjaScript implementation + golden-vector conformance.** The
  Python engine remains the specification and research brain. We generate
  golden vectors (bar series in → expected indicator states, gate verdicts,
  entries/exits out) from the Python engine, and the C# port must reproduce
  them bit-for-bit in NT's Market Replay/unit harness before it may trade.
  "Do not fork" is enforced by machine-checked conformance rather than
  shared runtime — the institutional-standard way to run one strategy on
  two runtimes. NT writes our `decisions_/attempts_/trades_` CSV schemas so
  drift_report/trade_stats/reconcile work unchanged.
- **(ii) Python brain + NT as dumb executor** via a local bridge. Rejected
  by default: fragile plumbing in the live path, and most funded-account
  rules restrict external automation — confirm Q5.

Recommendation: **(i)**.

## 7. Parity harness — three isolation stages

1. **Code parity (same data, two engines):** feed IDENTICAL SPY 1-min bars
   to the existing engine and the ported signal engine (Python↔Python
   first, then Python↔C# golden vectors). Required result: identical
   indicator states, gate verdicts, entry/exit timestamps, direction —
   bar-exact, zero tolerance. Any diff = bug, not "difference."
2. **Data parity (same engine, SPY vs ES bars):** run the shared engine on
   time-aligned SPY and ES RTH bars over the same window. Divergences here
   are *market structure* (basis, tape), not code — quantified per gate via
   the decision-log format (which both runs emit natively).
3. **Economic divergence (options exits vs ES exits, same entries):** for
   every matched entry, compare realized P&L paths — options premium P&L
   vs ES linear P&L under §3 exits. This measures the convexity/theta gap
   directly and answers "how much of the edge was payoff geometry."

Deliverable: trade-by-trade divergence table + per-gate attribution, from
the same drift/stats tooling that already exists.

## 8. Backtest integrity

Point-in-time ES/MES RTH bars (source: Q4), session-aware construction,
volume-roll continuous series with the §4 adjusted-vs-unadjusted rule,
costs and slippage per §4 with sensitivity sweeps (slippage 0–4 ticks, roll
method A/B), walk-forward split with the OOS window locked before any
sweep, every experiment through the existing ledger/`--variants-tested`
discipline. The existing replay engine gains a `FuturesSimBroker` (linear
fills, bracket semantics) so recorded/backfilled sessions replay through
the same shared code.

## 9. Pre-registered success criteria (decide them now, not after)

- Stage-1 parity: 100% signal-state match, or the port is broken.
- ES sibling goes live-paper only after: ≥10 sessions replayed with
  positive EV point estimate net of §4 costs AND the divergence report
  shows entries are signal-driven (not zone-artifact-driven).
- The options system's own edge is still unproven (tiny n) — porting an
  unvalidated strategy doubles unvalidated surface. The harness is
  therefore also the cheapest way to learn whether the SIGNAL has value
  independent of payoff geometry: if Stage-3 shows ES-linear P&L ≈ 0 while
  options P&L > 0, the edge is convexity and the port should be shelved.

---

## 10. Questions requiring answers before build (blocking)

1. **Venue/account:** personal futures account or funded prop account
   (Topstep/Apex/etc.)? If prop: exact daily loss, trailing drawdown,
   consistency rules, and whether fully automated NinjaScript is permitted.
   Trailing DD changes the sizing chapter entirely.
2. **Risk budget:** keep $150/trade, $300/day, $900/week? Account size for
   context?
3. **MES first?** (Strong recommendation yes.)
4. **ES historical data source** for the harness/backtest: NT8 export
   (which feed — Kinetick/Rithmic/CQG?), or licensed history (Databento/
   Polygon futures)? Depth needed: ≥1 year of 1-min RTH.
5. **NT integration mode:** confirm option (i) C# + golden-vector
   conformance, or argue for the bridge.
6. **Zone translation:** run all three variants (§2.3) in the harness and
   pick on evidence — or do you have a prior?
7. **RTH-only confirmed?**
8. **Sign-off on the §0 verdict:** you are commissioning a sibling
   strategy with a shared signal engine and re-derived risk/exits — not a
   payoff-identical clone. Proceed on that basis?

---

## 11. POST-SIGN-OFF ADDENDUM (July 2026) — resolved decisions, corrections, build record

### 11.1 The Apex constraint that reshapes the deliverable

Q1 answer: **Apex Trader Funding, 50K account.** Rules verified against
Apex's published material (July 2026):

| rule | value | consequence here |
|---|---|---|
| Trailing threshold | $2,500, trails **in real time on unrealized equity peaks**, locks at start+$100 ($50,100) | Tradeable capital = headroom, not balance. Sizing re-derived (§11.3). An open winner that spikes and retraces **permanently consumes headroom** — `apex_risk.py` meters this (`unrealized_consumption`) so the exit sweep can price trail looseness. |
| Contract scaling | Half size until EOD balance ≥ $52,600 (50K: 50 micros → 100) | Encoded in `ApexAccount.contracts_cap`. |
| Consistency | Best day ≤ **50%** of total profit at payout (relaxed from 30%, Mar 2026). Soft — delays payout, no breach. | `consistency_ok` + soft daily cap helper. |
| Mandatory stop | Since Mar 2026 every order must carry an attached stop (broker-side reject) | ATM bracket satisfies it; sizing REQUIRES a stop distance by construction. |
| **Automation** | **Fully automated trading PROHIBITED on PA/Live** (bots, algos, AI, set-and-forget → closure + forfeiture). Semi-automated management of an existing position after manual entry is permitted. | **The deliverable pivots from auto-trading strategy to CO-PILOT INDICATOR** (§11.4). |
| Flat by 16:59 ET | — | 15:25 time stop is well inside. |

The automation rule is the big one: the original "port the bot to NT and
let it trade" is not executable on an Apex PA without risking forfeiture.
What ships instead is compliant by construction: the machine computes
(identical conformance-checked engine), the human enters, the ATM bracket
manages, the indicator alerts the soft exits. It never places an order.

### 11.2 Correction to §0.3 / §2.3 — what the zone actually does

Reading the live candidate loop closed the question: strikes recompute
EVERY bar from spot (`compute_dynamic_strikes`), the bot subscribes a
±6-strike window around the target, and entry fires on whichever
subscribed candidate ticks first with all gates green. Working the
geometry: **some subscribed strike passes zone+otm on virtually every
bar** (offset is capped at $4.50, the window reaches $3.00 back toward
spot, and the activation band is ~$1.88 wide). So the zone machinery
mostly selects WHICH contract, not WHETHER to trade — the portfolio-level
entry filters are really momentum + velocity + premium band + execution
quality. §0.3's "hidden vol-regime filter" claim was overstated; the real
residual effects are (a) subscription-recenter staleness occasionally
blinding the bot during the fastest moves (an accidental don't-chase
brake ES won't have — Stage-2 divergence to watch) and (b) the premium
band clipping late-day/low-vol entries.

Consequence: **zone variant "off" (momentum-only) is the default** on ES;
"grid" (faithful ×10 geometric replication) is kept as the parity control.
`tests/test_futures_port.py::test_grid_zone_near_vacuous_at_es_scale`
encodes the finding as an executable assertion. The travel-trigger variant
(b) was dropped — it translated a mechanism the SPY system doesn't
actually have.

### 11.3 Resolved decisions (Q1–Q8)

1. **Venue:** Apex 50K PA — rules above; automation pivot in §11.4.
2. **Risk:** re-derived from the $2,500 buffer: $125/trade cap AND ≤5% of
   current headroom; daily loss min($300, 10% headroom) → $250 fresh;
   weekly min($900, 30% headroom). Prospective gating, as on options.
3. **MES first: yes.** ES's $12.50/tick makes an 8-tick stop ≈ the whole
   per-trade budget (1 contract, no granularity); MES gives ~12 contracts.
4. **Data (world-class default): Databento GLBX.MDP3** 1-min OHLCV for
   ES+MES history (point-in-time, per-contract series so we do our own
   volume roll — `futures_contracts.roll_schedule`), NT8/Rithmic export
   accepted as a secondary format (`load_bars_csv` reads both; NT8 close-
   stamps are auto-shifted to open convention). ≥1 year before any sweep
   is trusted.
5. **NT integration: option (i) confirmed** — C# NinjaScript engine with
   golden-vector conformance; Python remains the oracle. The bridge is
   dead (fragile AND non-compliant on Apex).
6. **Zone:** default "off", "grid" as control (§11.2), both A/B'd in the
   divergence report.
7. **RTH-only: confirmed.** VWAP anchored at RTH open, non-RTH bars warm
   EMAs only (exactly the SPY engine's behavior).
8. **Sibling-strategy basis: signed off.**

### 11.4 What exists now (built + verified this session)

Python (the oracle — 35 new tests, 251 total green):
- `futures_contracts.py` — ES/MES specs, third-Friday expiries, volume-roll
  front-month calendar, tick math, the sizing identity.
- `apex_risk.py` — the account geometry above, real-time threshold
  trailing on unrealized peaks, breach detection, headroom sizing,
  prospective gates, consistency helpers, unrealized-consumption meter.
- `futures_exits.py` — price-space exit stack: resting stop
  max(0.75×atr5, 1pt) primary, 1.5×atr5 target, trail (arm 1.0×atr5,
  40% give-back), stagnation exit (theta's replacement: 20 bars with
  <0.25×atr5 excursion), 15:25 time stop. All k's provisional pending the
  replay sweep.
- `es_engine.py` — shared signal engine: REUSES the live `MomentumEngine`
  object; velocity gate re-based as fraction of price (3.2bp); futures
  gate set; no-short-circuit gate reports.
- `futures_sim.py` — conservative fills: stop-first intrabar ordering,
  target requires trade-through, adverse slip both ways, commissions.
- `es_backtest.py` — session runner: point-in-time daily ATR, FOMC
  blackout/flatten, Apex marked intrabar pessimistically (peak ratchets
  threshold BEFORE trough tests breach), decisions/trades/states CSVs
  (crash-safe gzip members, byte-deterministic), Apex survival stats in
  the summary. CLI.
- `golden_vectors.py` — golden generation + conformance compare (numeric
  tol only absorbs decimal formatting; everything else exact). CLI.

C# (`ninjatrader/`):
- `AofCore.cs` — the engine mirrored line-for-line (momentum, gates,
  exits, sim, Apex, session runner). Banker's rounding matched to
  Python's; no NT dependencies.
- `AofGoldenRunner.cs` + `check_conformance.ps1` — compile with the
  stock .NET Framework csc, run, diff.
- `AofEsMomentum.cs` — NT8 **indicator** (co-pilot): arrows, alerts,
  printed order ticket (side/qty/stop/target from the engine's sizing),
  soft-exit alerts. Places no orders — no code path exists.
- `README.md` — install, ATM template workflow, compliance box, Apex
  cheat sheet.

**Conformance verified in-session** (mono): 7-session synthetic tape,
2,730 RTH bars × 30 state columns, both zone variants —
`CONFORMANT — 0 mismatches`. Stage-1 code parity (§7) is a passing
harness, not a plan.

### 11.5 Still ahead (in order)

1. Buy ≥1yr of MES/ES 1-min from Databento; splice per
   `roll_schedule`; run `es_backtest.py` per contract segment.
2. Re-derive exit k's from the recorded options sessions' underlying-move
   equivalents; sweep on the ES history (slippage 0–4 ticks, roll A/B);
   pre-registered criteria in §9 decide.
3. Stage-2 data parity (SPY vs ES bars through the shared engine) and
   Stage-3 economic divergence (options premium P&L vs ES linear P&L on
   matched entries) — the convexity question answers itself here.
4. Apex survival report: breach probability / payout-time distribution
   under the swept parameter sets, using the intrabar-pessimistic
   threshold model.
5. Paper co-pilot dry runs in NT (Sim101/Market Replay) before any PA
   order. The §9 gate stands: no live-paper until ≥10 sessions positive
   EV net of costs AND entries proven signal-driven.

### 11.6 Decision update — fully automated after all (firm TBD)

User decision after reading §11.1: **the port will be fully automated on
NinjaTrader; Apex is dropped in favor of a prop firm whose rules permit
automation** (firm not yet chosen).

Consequences, implemented:
- `ninjatrader/AofEsStrategy.cs` — fully automated NT8 strategy. One
  brain, thin mirror: the conformance-checked `SessionRunner` remains the
  state of record; the shell mirrors its shadow position with real orders
  (entry + broker-side bracket at shadow levels, market flatten on soft
  exits, gap/lag backstop on hard exits) and reconciles disagreements
  with a flat-wins rule. Shadow breach → hard flatten + permanent
  stand-down.
- The account model is fully parameterized (`ApexAccount` fields are now
  instance parameters; Apex-50K defaults retained as the reference).
  When the firm is chosen, its trailing-DD mechanics (real-time vs EOD,
  scaling, caps, consistency) get encoded as the strategy's parameter set
  — and if its geometry differs structurally (e.g., static drawdown),
  the model gets a variant + tests BEFORE go-live.
- The co-pilot indicator stays in the tree as the fallback for
  manual-entry firms.
- Known approximation, carried forward deliberately: the shadow account
  books sim fills, so in-strategy risk gates approximate the firm's
  ledger. Stage-2 adds live-fill reconciliation into the brain.
- Conformance re-verified after the refactor: both zone variants,
  0 mismatches.

Firm-selection due diligence (user's task, quant's checklist): written
confirmation that automation is permitted; trailing-DD basis (real-time
unrealized vs EOD); max contracts + scaling; consistency rules; payout
cadence/caps; data/platform fees; whether NT8 + your data feed is
supported natively.
