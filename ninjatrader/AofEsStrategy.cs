// AofEsStrategy.cs — FULLY AUTOMATED NinjaTrader 8 strategy over AofCore.cs.
//
// ⚠ COMPLIANCE IS YOUR JOB: several futures prop firms (Apex among them)
// prohibit unattended automation on funded accounts and enforce it with
// account closure and payout forfeiture. Run this only on accounts whose
// written rules permit fully automated trading. Every prop-firm number
// below (trailing drawdown, caps, scaling) is a strategy PARAMETER — set
// them to your firm's actual rules before enabling.
//
// ARCHITECTURE — one brain, thin mirror:
//   The conformance-checked SessionRunner (AofCore.cs — the same object
//   the golden vectors gate against the Python oracle) is the state of
//   record: it decides entries, sizing, stop/target levels, and soft exits
//   (trail / stagnation / time stop / FOMC flatten). This shell only
//   mirrors the shadow position with real orders:
//     shadow enters      → EnterLong/EnterShort with the shadow qty and a
//                          broker-side bracket at the shadow stop/target
//     shadow soft-exits  → market flatten
//     shadow hard-exits  → the real bracket should already have filled at
//                          the same level; if the broker order gapped or
//                          lagged, the reconciler market-flattens
//     reconciliation     → any bar where real and shadow disagree about
//                          being in a position, REALITY IS FORCED TO MATCH
//                          THE BRAIN, flat side wins (never auto-re-enter)
//
// HONEST LIMITATION (documented, deliberate): the shadow account books SIM
// fills (close ± 1 tick slip, modeled commissions), so its balance drifts
// from the broker's over time. Sizing and daily/weekly gates therefore run
// on a conservative approximation, not the firm's ledger. The firm's own
// risk engine remains the hard enforcement; treat the in-strategy gates as
// the first line, not the last. Live-fill reconciliation into the brain is
// Stage-2 work (ES_PORT_PLAN.md §11.5).
//
// Chart requirements: 1-minute bars, chart time zone US Eastern, MES or ES
// front month. Calculate = OnBarClose (decisions on completed bars, same
// as the Python bot).

#region Using declarations
using System;
using System.IO;
using System.Globalization;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using Aof;
#endregion

namespace NinjaTrader.NinjaScript.Strategies
{
    public class AofEsStrategy : Strategy
    {
        private SessionRunner runner;
        private ApexAccount account;
        private bool wasInShadowPos;
        private StreamWriter stateLog, tradeLog;
        private double lastCumProfit;

        // ── Prop-firm rule parameters (SET THESE TO YOUR FIRM'S RULES) ──────
        [NinjaScriptProperty] public double StartBalance { get; set; }
        [NinjaScriptProperty] public double TrailingDrawdown { get; set; }
        [NinjaScriptProperty] public double ThresholdLockBuffer { get; set; }
        [NinjaScriptProperty] public int    MaxMinis { get; set; }
        [NinjaScriptProperty] public bool   HalfSizeUntilBufferBanked { get; set; }

        // ── Risk parameters ──────────────────────────────────────────────────
        [NinjaScriptProperty] public double RiskPerTrade { get; set; }
        [NinjaScriptProperty] public double RiskHeadroomFraction { get; set; }
        [NinjaScriptProperty] public double DailyLossCap { get; set; }
        [NinjaScriptProperty] public double WeeklyLossCap { get; set; }

        // ── Engine parameters ────────────────────────────────────────────────
        [NinjaScriptProperty] public string ZoneVariant { get; set; }
        [NinjaScriptProperty] public double CommissionPerSide { get; set; }
        [NinjaScriptProperty] public string LogDirectory { get; set; }

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "AOF ES sibling — fully automated. Verify your "
                            + "prop firm PERMITS automation before enabling.";
                Name = "AofEsStrategy";
                Calculate = Calculate.OnBarClose;
                EntriesPerDirection = 1;
                EntryHandling = EntryHandling.AllEntries;
                IsExitOnSessionCloseStrategy = true;   // backstop; the engine's
                ExitOnSessionCloseSeconds = 130;        // 15:25 stop fires first
                BarsRequiredToTrade = 25;
                IncludeCommission = true;

                // Apex-50K-shaped defaults — OVERRIDE for your firm
                StartBalance = 50000; TrailingDrawdown = 2500;
                ThresholdLockBuffer = 100; MaxMinis = 10;
                HalfSizeUntilBufferBanked = true;
                RiskPerTrade = 125; RiskHeadroomFraction = 0.05;
                DailyLossCap = 300; WeeklyLossCap = 900;
                ZoneVariant = "off"; CommissionPerSide = 1.30;
                LogDirectory = "";
            }
            else if (State == State.DataLoaded)
            {
                account = new ApexAccount
                {
                    StartBalance = StartBalance,
                    TrailingDd = TrailingDrawdown,
                    LockBuffer = ThresholdLockBuffer,
                    MaxMinis = MaxMinis,
                    HalfUntilNet = HalfSizeUntilBufferBanked,
                    RiskPerTrade = RiskPerTrade,
                    RiskHeadroomFrac = RiskHeadroomFraction,
                    DailyLossCap = DailyLossCap,
                    WeeklyLossCap = WeeklyLossCap,
                    // frac limits stay proportional to the configured buffer
                    DailyLossFrac = Cfg.ApexDailyLossFrac,
                    WeeklyLossFrac = Cfg.ApexWeeklyLossFrac,
                };
                account.Reset();

                string root = Instrument.MasterInstrument.Name == "ES" ? "ES" : "MES";
                runner = new SessionRunner(ZoneVariant, root, CommissionPerSide,
                                           account);
                if (!string.IsNullOrEmpty(LogDirectory))
                    OpenLogs();
            }
            else if (State == State.Realtime)
            {
                // a shadow position opened during historical warmup was never
                // really traded — never seed a live order from it
                runner.AbandonPosition();
                wasInShadowPos = false;
            }
            else if (State == State.Terminated)
            {
                if (stateLog != null) { stateLog.Dispose(); stateLog = null; }
                if (tradeLog != null) { tradeLog.Dispose(); tradeLog = null; }
            }
        }

        private void OpenLogs()
        {
            Directory.CreateDirectory(LogDirectory);
            string d = DateTime.Now.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);
            string sp = Path.Combine(LogDirectory, "nt_states_" + d + ".csv");
            bool newS = !File.Exists(sp) || new FileInfo(sp).Length == 0;
            stateLog = new StreamWriter(sp, true) { AutoFlush = true };
            if (newS) stateLog.WriteLine(string.Join(",", SessionRunner.StateColumns));

            string tp = Path.Combine(LogDirectory, "nt_trades_" + d + ".csv");
            bool newT = !File.Exists(tp) || new FileInfo(tp).Length == 0;
            tradeLog = new StreamWriter(tp, true) { AutoFlush = true };
            if (newT) tradeLog.WriteLine(
                "time_et,event,side,qty,shadow_px,real_avg_px,reason,"
                + "shadow_balance,threshold,headroom");
        }

        protected override void OnBarUpdate()
        {
            if (BarsInProgress != 0 || runner == null
                || CurrentBar < BarsRequiredToTrade)
                return;

            // NT stamps minute bars at bar CLOSE; the engine uses bar OPEN
            var bar = new AofBar
            {
                TimeEt = Time[0].AddMinutes(-1),
                Open = Open[0], High = High[0], Low = Low[0], Close = Close[0],
                Volume = Volume[0],
            };

            bool hadShadowPos = runner.Pos != null;
            var row = runner.OnBar(bar);
            if (stateLog != null && row != null)
                stateLog.WriteLine(string.Join(",", row));

            // ── mirror the brain with real orders ────────────────────────────

            // 1. shadow entered this bar → real entry + broker-side bracket
            if (runner.EnteredThisBar && runner.Pos != null)
            {
                var p = runner.Pos;
                string sig = p.Side == "long" ? "AofLong" : "AofShort";
                SetStopLoss(sig, CalculationMode.Price, p.StopPrice, false);
                SetProfitTarget(sig, CalculationMode.Price, p.TargetPrice);
                if (p.Side == "long") EnterLong(p.Qty, sig);
                else                  EnterShort(p.Qty, sig);
                LogTrade(bar, "entry", p.Side, p.Qty, p.EntryPrice, "");
            }

            // 2. shadow exited this bar → make sure reality is flat too.
            //    On stop/target the bracket normally filled at the same level
            //    already; the flatten below is the gap/lag backstop.
            bool shadowExited = hadShadowPos && runner.Pos == null;
            if (shadowExited && Position.MarketPosition != MarketPosition.Flat)
            {
                if (Position.MarketPosition == MarketPosition.Long) ExitLong();
                else ExitShort();
                LogTrade(bar, "exit", "", Position.Quantity, 0,
                         runner.LastExitReason);
            }

            // 3. reconciliation — real and shadow must agree; FLAT WINS.
            //    Real flat + shadow holding (bracket filled before the bar
            //    closed, manual intervention, broker reject): kill the shadow
            //    so the brain never sizes off a phantom position.
            if (State == State.Realtime && runner.Pos != null
                && !runner.EnteredThisBar
                && Position.MarketPosition == MarketPosition.Flat)
            {
                LogTrade(bar, "reconcile_abandon", runner.Pos.Side,
                         runner.Pos.Qty, runner.Pos.EntryPrice, "real_flat");
                runner.AbandonPosition();
            }

            // 4. account breach / halt → hard flatten, disable
            if (runner.Apex.Breached
                && Position.MarketPosition != MarketPosition.Flat)
            {
                if (Position.MarketPosition == MarketPosition.Long) ExitLong();
                else ExitShort();
                Print(Time[0] + "  AOF HALT — trailing threshold breached "
                      + "(shadow model). Strategy is done for good.");
            }
        }

        protected override void OnPositionUpdate(Position position,
            double averagePrice, int quantity, MarketPosition marketPosition)
        {
            // real fills, real money — log the broker's view next to the
            // shadow's so the drift between them is measurable, per bar one
            if (tradeLog != null && marketPosition == MarketPosition.Flat
                && SystemPerformance.AllTrades.Count > 0)
            {
                double cum = SystemPerformance.AllTrades.TradesPerformance
                                 .Currency.CumProfit;
                double realPnl = cum - lastCumProfit;
                lastCumProfit = cum;
                tradeLog.WriteLine(string.Format(CultureInfo.InvariantCulture,
                    "{0},real_close,,,,{1:F2},real_pnl={2:F2},{3:F2},{4:F2},{5:F2}",
                    Time[0].ToString("HH:mm:ss", CultureInfo.InvariantCulture),
                    averagePrice, realPnl, runner.Apex.Balance,
                    runner.Apex.Threshold, runner.Apex.Headroom()));
            }
        }

        private void LogTrade(AofBar bar, string ev, string side, int qty,
                              double shadowPx, string reason)
        {
            if (tradeLog == null) return;
            double realAvg = Position.MarketPosition != MarketPosition.Flat
                ? Position.AveragePrice : 0.0;
            tradeLog.WriteLine(string.Format(CultureInfo.InvariantCulture,
                "{0},{1},{2},{3},{4:F2},{5:F2},{6},{7:F2},{8:F2},{9:F2}",
                bar.TimeEt.ToString("HH:mm:ss", CultureInfo.InvariantCulture),
                ev, side, qty, shadowPx, realAvg, reason,
                runner.Apex.Balance, runner.Apex.Threshold,
                runner.Apex.Headroom()));
        }
    }
}
