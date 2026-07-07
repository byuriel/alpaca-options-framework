// AofEsMomentum.cs — NinjaTrader 8 INDICATOR shell over AofCore.cs.
//
// DELIBERATELY AN INDICATOR, NOT A STRATEGY. Apex Trader Funding prohibits
// fully automated trading on PA/funded accounts (account closure + payout
// forfeiture). The compliant envelope is: the machine computes, the human
// enters, semi-automated tools manage the open position. So this shell:
//
//   - runs the exact conformance-checked engine (AofCore.SessionRunner)
//     on 1-minute bars,
//   - on an entry signal: draws an arrow, fires an alert, and prints the
//     suggested contract count, stop and target — the trader enters
//     MANUALLY with the pre-built ATM template (see README.md; the ATM
//     bracket also satisfies Apex's mandatory attached-stop rule),
//   - on a soft exit signal (trail / stagnation / time stop / FOMC
//     flatten): fires an exit alert for the trader to act on. Stop and
//     target fills happen broker-side via the ATM bracket.
//
// It never places, modifies, or cancels an order. There is no code path
// that could.
//
// Chart requirements (README.md): 1-minute bars, chart time zone set to
// US Eastern, CME US Index Futures RTH+ETH template (the engine ignores
// non-RTH bars for everything except EMA warmth, same as the Python bot).

#region Using declarations
using System;
using System.Windows.Media;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.DrawingTools;
using Aof;
#endregion

namespace NinjaTrader.NinjaScript.Indicators
{
    public class AofEsMomentum : Indicator
    {
        private SessionRunner runner;

        [NinjaScriptProperty]
        public string ZoneVariant { get; set; }

        [NinjaScriptProperty]
        public string SpecRoot { get; set; }

        [NinjaScriptProperty]
        public double CommissionPerSide { get; set; }

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = "AOF ES sibling — momentum co-pilot (signals only; "
                            + "manual entry per Apex PA rules)";
                Name = "AofEsMomentum";
                Calculate = Calculate.OnBarClose;      // decisions on completed bars
                IsOverlay = true;
                DisplayInDataBox = true;
                ZoneVariant = "off";
                SpecRoot = "MES";
                CommissionPerSide = 1.30;
            }
            else if (State == State.DataLoaded)
            {
                if (BarsPeriod.BarsPeriodType != BarsPeriodType.Minute
                    || BarsPeriod.Value != 1)
                    Draw.TextFixed(this, "aof_warn",
                        "AofEsMomentum requires a 1-MINUTE chart", TextPosition.TopLeft);
                runner = new SessionRunner(ZoneVariant, SpecRoot, CommissionPerSide);
            }
        }

        protected override void OnBarUpdate()
        {
            if (runner == null || CurrentBar < 1)
                return;

            // NT stamps minute bars at bar CLOSE; the engine uses bar OPEN
            var bar = new AofBar
            {
                TimeEt = Time[0].AddMinutes(-1),
                Open = Open[0], High = High[0], Low = Low[0], Close = Close[0],
                Volume = Volume[0],
            };
            runner.OnBar(bar);

            bool live = State == State.Realtime;

            if (runner.EnteredThisBar && runner.Pos != null)
            {
                var p = runner.Pos;
                bool isLong = p.Side == "long";
                string tag = "aof_entry_" + CurrentBar;
                if (isLong)
                    Draw.ArrowUp(this, tag, false, 0, Low[0] - 2 * TickSize, Brushes.Lime);
                else
                    Draw.ArrowDown(this, tag, false, 0, High[0] + 2 * TickSize, Brushes.Red);

                Draw.Line(this, "aof_stop_" + CurrentBar, false, 0, p.StopPrice,
                          -15, p.StopPrice, Brushes.Red, DashStyleHelper.Dash, 2);
                Draw.Line(this, "aof_tgt_" + CurrentBar, false, 0, p.TargetPrice,
                          -15, p.TargetPrice, Brushes.LimeGreen, DashStyleHelper.Dash, 2);

                string msg = string.Format(
                    "AOF ENTRY {0} — {1} x{2} @ mkt | stop {3} | target {4} "
                    + "(enter manually via ATM template)",
                    p.Side.ToUpper(), SpecRoot, p.Qty, p.StopPrice, p.TargetPrice);
                Print(Time[0] + "  " + msg);
                if (live)
                    Alert("aof_entry", Priority.High, msg,
                          NinjaTrader.Core.Globals.InstallDir + @"\sounds\Alert1.wav",
                          10, Brushes.Black, isLong ? Brushes.Lime : Brushes.Red);
            }

            string exit = runner.LastExitReason;
            if (!string.IsNullOrEmpty(exit)
                && exit != "stop" && exit != "target")   // those fill broker-side
            {
                string msg = "AOF EXIT NOW — " + exit + " (flatten manually)";
                Print(Time[0] + "  " + msg);
                if (live)
                    Alert("aof_exit", Priority.High, msg,
                          NinjaTrader.Core.Globals.InstallDir + @"\sounds\Alert2.wav",
                          10, Brushes.Black, Brushes.Orange);
            }
        }
    }
}
