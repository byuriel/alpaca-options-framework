# NinjaTrader 8 port — ES/MES sibling strategy (Apex-compliant co-pilot)

This directory is the C# side of the port described in `ES_PORT_PLAN.md`.
The Python repo remains the **specification and research brain**; the C#
engine may not be trusted until `check_conformance.ps1` prints
**CONFORMANT** against Python-generated golden vectors.

Conformance status: verified bit-for-bit against the Python oracle on a
7-session synthetic tape (2,730 RTH bars, both zone variants) under mono.
Re-run it yourself after ANY edit to either side.

## Why this is an indicator, not a strategy

**Apex Trader Funding prohibits fully automated trading on PA/funded
accounts** — bots, algorithms, AI, set-and-forget systems. Violations are
account closure and forfeiture. What IS allowed: manual entries, and
semi-automated tools that manage an existing position after you enter it.

So the port is a **co-pilot**: `AofEsMomentum` computes everything the
Python bot would (same conformance-checked engine — momentum, gates,
sizing, stop/target), then draws the arrow, fires the alert, and prints
the order ticket. **You click the entry.** The ATM bracket manages the
open position (that's the permitted semi-automation), and the indicator
alerts you when a soft exit (trail / stagnation / time stop / FOMC
flatten) says get out.

This also satisfies Apex's **mandatory attached-stop rule** (since March
2026 Rithmic/Tradovate reject orders without a stop): every ATM entry
carries its bracket.

## Files

| file | what |
|---|---|
| `AofCore.cs` | The engine, no NT dependencies. Mirrors the Python line-for-line — momentum, gates, exits, sim fills, Apex account math, session runner. **Never "improve" it here; change Python first, regenerate goldens, then match.** |
| `AofEsMomentum.cs` | NT8 indicator shell. Signals + alerts + suggested size. Places no orders — there is no code path that could. |
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

## NT8 installation

1. Copy `AofCore.cs` and `AofEsMomentum.cs` into
   `Documents\NinjaTrader 8\bin\Custom\Indicators\`.
2. NinjaScript Editor → compile (F5). Zero errors expected.
3. Chart: **MES front-month, 1-minute bars, chart time zone US Eastern**
   (Data Series → Time zone). The engine ignores non-RTH bars for
   everything except EMA warmth, same as the Python bot.
4. Add indicator `AofEsMomentum` (parameters: ZoneVariant `off`,
   SpecRoot `MES`, CommissionPerSide per your Apex fee schedule).

## ATM template (one-time setup)

Create an ATM strategy template named `AOF`:
- Bracket: 1 stop + 1 target, quantity left at 1 (you'll set actual qty
  from the alert), stop ~8 ticks, target ~24 ticks as placeholders.
- On an entry alert: set quantity to the printed size, enter with the ATM
  active, then drag the stop and target to the exact printed prices.
  Adjusting a live bracket is position management — permitted.
- On an exit alert (`trail`/`stagnation`/`time_stop`/`event_flatten`):
  flatten manually. Stop/target exits fill broker-side on their own.

## Apex 50K cheat sheet (encoded in the engine — July 2026 rules)

- Trailing threshold: **$2,500**, trails in real time on unrealized peaks,
  locks at **$50,100**. Tradeable capital = headroom, not balance.
- Contract scaling: **half size (50 micros) until EOD balance ≥ $52,600**.
- Consistency: best day ≤ **50%** of total profit at payout (soft rule).
- Flat by 16:59 ET — the engine's 15:25 time stop is well inside.
- Every order needs an attached stop — the ATM bracket does this.
- **No unattended automation. You are the executor.** The engine sizes
  every trade off current headroom (max $125 or 5% of headroom) and
  prospectively gates daily (−$250 fresh) and weekly losses.
