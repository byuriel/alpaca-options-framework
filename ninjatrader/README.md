# NinjaTrader 8 port — ES/MES sibling strategy (fully automated)

This directory is the C# side of the port described in `ES_PORT_PLAN.md`.
The Python repo remains the **specification and research brain**; the C#
engine may not be trusted until `check_conformance.ps1` prints
**CONFORMANT** against Python-generated golden vectors.

Conformance status: verified bit-for-bit against the Python oracle on a
7-session synthetic tape (2,730 RTH bars, both zone variants) under mono.
Re-run it yourself after ANY edit to either side.

## ⚠ Prop-firm compliance is YOUR pre-flight check

Several futures prop firms — **Apex included** — prohibit unattended
automation on funded accounts and enforce it with account closure and
forfeiture of all funds. `AofEsStrategy` is fully automated: run it only
on an account whose **written rules permit automated trading**, and get
that permission in writing if the rules are ambiguous. Every prop-firm
number in the strategy (trailing drawdown, caps, contract scaling) is a
parameter — set them to your firm's actual rules before enabling. The
defaults are Apex-50K-shaped ONLY because that was the reference model.

For firms that allow only manual entry, `AofEsMomentum` (the co-pilot
indicator: same engine, signals + alerts + printed order ticket, zero
order-placing code) remains in this directory as the compliant fallback.

## Architecture — one brain, thin mirror

`AofEsStrategy` does not reimplement the strategy. The conformance-checked
`SessionRunner` (the exact object the golden vectors gate) is the state of
record; the NT shell only mirrors its shadow position with real orders:

- shadow enters → `EnterLong/EnterShort` with the shadow qty + broker-side
  bracket at the shadow stop/target (stops rest at the exchange, never
  soft-only)
- shadow soft-exits (trail / stagnation / 15:25 time stop / FOMC flatten)
  → market flatten
- shadow hard-exits (stop/target) → the bracket normally filled at the
  same level already; a reconciling flatten covers gaps/lag
- any disagreement between real and shadow → **flat wins**: real flat +
  shadow holding kills the shadow (never auto-re-enters); shadow flat +
  real holding gets flattened
- shadow account breach → hard flatten and permanent stand-down

Honest limitation (deliberate, documented in the file header): the shadow
account books SIM fills, so its balance drifts from the broker's ledger
over time — the in-strategy risk gates are a conservative approximation,
the firm's own risk engine is the hard enforcement. Live-fill
reconciliation into the brain is Stage-2 work.

## Files

| file | what |
|---|---|
| `AofCore.cs` | The engine, no NT dependencies. Mirrors the Python line-for-line — momentum, gates, exits, sim fills, prop-account math, session runner. **Never "improve" it here; change Python first, regenerate goldens, then match.** |
| `AofEsStrategy.cs` | Fully automated NT8 strategy — the thin execution mirror described above. Prop-firm rules and risk budget as parameters. Optional CSV logging (`LogDirectory`) in the repo's state/trade schemas. |
| `AofEsMomentum.cs` | Co-pilot indicator (manual-entry firms): signals + alerts + suggested size. Places no orders — there is no code path that could. |
| `AofGoldenRunner.cs` | Console conformance runner (bars CSV → state vectors). |
| `check_conformance.ps1` | Compile + run + diff against Python goldens. |

## Conformance workflow (run before first use and after any change)

```powershell
# 1. from the repo root, generate Python goldens over any bars CSV
python golden_vectors.py generate path\to\bars.csv out\goldens

# 2. compile the C# engine and diff it against them
powershell -ExecutionPolicy Bypass -File ninjatrader\check_conformance.ps1 `
    -Bars path\to\bars.csv -Golden out\goldens\states_es.csv
```

Any output other than `CONFORMANT — 0 mismatches` means the port is
broken. There are no acceptable differences.

Bars CSV: `timestamp,open,high,low,close,volume` 1-minute rows, timestamps
= bar OPEN time (ISO-8601; naive = ET). NT8 can export this via Tools →
Export → Historical Data (export in ET; NT stamps bar CLOSE — the Python
loader `es_backtest.load_bars_csv` auto-shifts NT8-format files).

## NT8 installation (automated strategy)

1. Copy `AofCore.cs` into `Documents\NinjaTrader 8\bin\Custom\AddOns\`
   and `AofEsStrategy.cs` into `...\bin\Custom\Strategies\`
   (`AofEsMomentum.cs` into `...\Indicators\` if you want the co-pilot too).
2. NinjaScript Editor → compile (F5). Zero errors expected.
3. Chart: **MES front-month, 1-minute bars, chart time zone US Eastern**
   (Data Series → Time zone). The engine ignores non-RTH bars for
   everything except EMA warmth, same as the Python bot.
4. Add strategy `AofEsStrategy`; set every prop-firm parameter to YOUR
   firm's rules (StartBalance, TrailingDrawdown, MaxMinis, caps,
   CommissionPerSide from your fee schedule). Set `LogDirectory` to get
   per-session state/trade CSVs the Python drift tooling can read.
5. **Validate the full gauntlet before real money**, in order:
   Strategy Analyzer backtest → Market Replay → Sim101 live-paper for
   ≥10 sessions → firm evaluation → funded. The pre-registered criteria
   in `ES_PORT_PLAN.md` §9 gate each promotion.
6. Strategy start behavior: leave `WaitUntilFlat`; the strategy also
   refuses to seed a live order from a position its shadow opened during
   historical warmup.

## Trailing-drawdown account model (parameterized)

The account model (`ApexAccount` in `AofCore.cs`) encodes the geometry
most futures prop firms share — verify each number against YOUR firm:

- **Trailing threshold** trails on equity peaks (the reference model
  trails in REAL TIME on unrealized peaks — the most punitive variant;
  if your firm trails end-of-day only, the model is conservative for you)
  and locks after banking `TrailingDrawdown + ThresholdLockBuffer`.
- Tradeable capital = **headroom above the threshold**, never the nominal
  balance. Sizing: min(RiskPerTrade, 5% of current headroom) / (stop
  ticks × tick value), floored — a stop too wide for the budget skips.
- Daily / weekly loss gates are prospective: if losing the NEXT trade in
  full would cross the limit, the trade doesn't happen.
- An open winner that spikes and retraces permanently consumes headroom
  under real-time trailing — the engine meters this
  (`UnrealizedConsumption`), which is why the exit stack leans toward
  banking at targets.
- Time stop 15:25 ET keeps you far inside any firm's flat-by-close rule;
  the FOMC blackout/flatten avoids the one scheduled event inside the
  entry window.
