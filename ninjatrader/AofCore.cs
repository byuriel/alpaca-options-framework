// AofCore.cs — the ES sibling strategy's engine, ported from the Python
// oracle (momentum.py, es_engine.py, futures_exits.py, futures_sim.py,
// apex_risk.py, es_backtest.py).
//
// RULE OF THIS FILE: it may not "improve" anything. Every formula, every
// comparison, every rounding mode, every ordering decision mirrors the
// Python reference line for line — the golden-vector conformance check
// (check_conformance.ps1) is the only judge. If you want to change strategy
// behavior, change the Python first, regenerate goldens, then match here.
//
// No NinjaTrader dependencies — plain .NET Framework 4.8 / netstandard2.0.
// AofEsMomentum.cs (the NT8 indicator) and AofGoldenRunner.cs (the
// conformance console app) are thin shells over this file.

using System;
using System.Collections.Generic;
using System.Globalization;

namespace Aof
{
    // ── Configuration (mirror of config.py — keep in sync BY CONFORMANCE) ────
    public static class Cfg
    {
        // momentum (shared signal layer)
        public const double RocThreshold  = 0.0003;
        public const int    MinConsecBars = 3;

        // ES port
        public const double EsAtr5MinFrac = 0.00032;
        public const string EntryStart = "09:45";
        public const string EntryEnd   = "14:30";
        public const string TimeStop   = "15:25";
        public const int    MaxTradesPerDay   = 999;
        public const int    TradeCooldownBars = 3;

        // zone translation ("off" | "grid") — plan §2.3
        public const double AtrMult        = 0.60;
        public const double EsGridStep     = 5.00;
        public const double EsGridMaxOffset = 45.0;
        public const int    EsGridAlts     = 6;
        public const double ActivationPct  = 0.003;

        // exits (price space)
        public const double EsStopAtrMult      = 0.75;
        public const double EsStopFloorPts     = 1.00;
        public const double EsTargetAtrMult    = 1.50;
        public const double EsTrailArmAtrMult  = 1.00;
        public const double EsTrailGiveback    = 0.40;
        public const int    EsStagnationBars   = 20;
        public const double EsStagnationAtrFrac = 0.25;

        // execution model
        public const int    EsEntrySlipTicks = 1;
        public const int    EsStopSlipTicks  = 1;

        // Apex 50K
        public const double ApexStartBalance = 50000.0;
        public const double ApexTrailingDd   = 2500.0;
        public const double ApexLockBuffer   = 100.0;
        public const int    ApexMaxMinis     = 10;
        public const bool   ApexHalfUntilNet = true;
        public const double ApexRiskPerTrade     = 125.0;
        public const double ApexRiskHeadroomFrac = 0.05;
        public const double ApexDailyLossCap     = 300.0;
        public const double ApexDailyLossFrac    = 0.10;
        public const double ApexWeeklyLossCap    = 900.0;
        public const double ApexWeeklyLossFrac   = 0.30;

        // events
        public const bool   EventBlackoutEnabled   = true;
        public const string FomcEntryBlackoutStart = "13:30";
        public const bool   FomcFlattenPositions   = true;
        public const string FomcFlattenTime        = "13:45";
    }

    public struct Contract
    {
        public string Root; public double PointValue, TickValue, TickSize;
        public static readonly Contract ES  = new Contract { Root = "ES",  PointValue = 50.0, TickValue = 12.50, TickSize = 0.25 };
        public static readonly Contract MES = new Contract { Root = "MES", PointValue = 5.0,  TickValue = 1.25,  TickSize = 0.25 };
        public static Contract Get(string root) { return root == "ES" ? ES : MES; }
    }

    public struct AofBar
    {
        public DateTime TimeEt;   // bar OPEN time, US Eastern
        public double Open, High, Low, Close, Volume;
    }

    public static class Px
    {
        // Python: round(round(price / tick) * tick, 2) — round() is
        // banker's rounding; MidpointRounding.ToEven matches it exactly.
        public static double RoundToTick(double price, double tick = 0.25)
        {
            return Math.Round(Math.Round(price / tick, MidpointRounding.ToEven) * tick,
                              2, MidpointRounding.ToEven);
        }

        public static int TicksBetween(double a, double b, double tick = 0.25)
        {
            return (int)Math.Round(Math.Abs(a - b) / tick, MidpointRounding.ToEven);
        }
    }

    // ── Momentum engine (mirror of momentum.py — EMAs continuous across
    //    sessions; VWAP/streaks/ROC/atr5 are RTH-session statistics) ─────────
    public class MomentumState
    {
        public string Direction = "neutral";
        public double Ema5, Ema20, Vwap, Roc5, LastClose, Atr5;
        public int ConsecGreen, ConsecRed;
    }

    public class MomentumEngine
    {
        private readonly List<AofBar> _bars = new List<AofBar>();  // maxlen 50
        private double? _ema5, _ema20;
        private double _cumTpVol, _cumVol;
        private DateTime? _sessionDate;
        public MomentumState State = new MomentumState();

        private static double Ema(double? prev, double price, int period)
        {
            double k = 2.0 / (period + 1);
            if (!prev.HasValue) return price;
            return price * k + prev.Value * (1 - k);
        }

        private void ResetSession(DateTime date)
        {
            _cumTpVol = 0.0; _cumVol = 0.0; _sessionDate = date;
            _bars.Clear();
            State = new MomentumState { Ema5 = State.Ema5, Ema20 = State.Ema20 };
        }

        public MomentumState OnBar(AofBar bar)
        {
            var t = bar.TimeEt.TimeOfDay;
            bool rth = t >= new TimeSpan(9, 30, 0) && t < new TimeSpan(16, 0, 0);
            if (!rth)
            {
                _ema5  = Ema(_ema5,  bar.Close, 5);
                _ema20 = Ema(_ema20, bar.Close, 20);
                State = new MomentumState
                {
                    Direction = "neutral", Ema5 = _ema5.Value, Ema20 = _ema20.Value,
                    Vwap = State.Vwap, Roc5 = State.Roc5,
                    ConsecGreen = State.ConsecGreen, ConsecRed = State.ConsecRed,
                    LastClose = bar.Close, Atr5 = State.Atr5,
                };
                return State;
            }

            if (_sessionDate != bar.TimeEt.Date) ResetSession(bar.TimeEt.Date);

            double tp = (bar.High + bar.Low + bar.Close) / 3.0;
            _cumTpVol += tp * bar.Volume;
            _cumVol   += bar.Volume;
            double vwap = _cumVol > 0 ? _cumTpVol / _cumVol : bar.Close;

            _ema5  = Ema(_ema5,  bar.Close, 5);
            _ema20 = Ema(_ema20, bar.Close, 20);

            _bars.Add(bar);
            if (_bars.Count > 50) _bars.RemoveAt(0);

            double roc5 = 0.0;
            if (_bars.Count >= 6)
            {
                double prev5 = _bars[_bars.Count - 6].Close;
                roc5 = prev5 != 0 ? (bar.Close - prev5) / prev5 : 0.0;
            }

            int cg, cr;
            if (bar.Close > bar.Open)      { cg = State.ConsecGreen + 1; cr = 0; }
            else if (bar.Close < bar.Open) { cg = 0; cr = State.ConsecRed + 1; }
            else                           { cg = State.ConsecGreen; cr = State.ConsecRed; }

            bool bull = bar.Close > vwap && _ema5.Value > _ema20.Value
                        && roc5 >= Cfg.RocThreshold && cg >= Cfg.MinConsecBars;
            bool bear = bar.Close < vwap && _ema5.Value < _ema20.Value
                        && roc5 <= -Cfg.RocThreshold && cr >= Cfg.MinConsecBars;
            string direction = bull ? "bull" : (bear ? "bear" : "neutral");

            // rolling atr5: mean of last ≤5 (high − low), summed oldest→newest
            int n = Math.Min(5, _bars.Count);
            double s = 0.0;
            for (int i = _bars.Count - n; i < _bars.Count; i++)
                s += _bars[i].High - _bars[i].Low;
            double atr5 = n > 0 ? s / n : bar.High - bar.Low;

            State = new MomentumState
            {
                Direction = direction, Ema5 = _ema5.Value, Ema20 = _ema20.Value,
                Vwap = vwap, Roc5 = roc5, ConsecGreen = cg, ConsecRed = cr,
                LastClose = bar.Close, Atr5 = atr5,
            };
            return State;
        }
    }

    // ── Event calendar (mirror of event_calendar.py; FOMC statement days) ───
    public static class Events
    {
        private static readonly HashSet<DateTime> Fomc = new HashSet<DateTime>
        {
            // 2025 confirmed
            new DateTime(2025,1,29), new DateTime(2025,3,19),
            new DateTime(2025,5,7),  new DateTime(2025,6,18),
            new DateTime(2025,7,30), new DateTime(2025,9,17),
            new DateTime(2025,10,29), new DateTime(2025,12,10),
            // 2026 announced
            new DateTime(2026,1,28), new DateTime(2026,3,18),
            new DateTime(2026,4,29), new DateTime(2026,6,17),
            new DateTime(2026,7,29), new DateTime(2026,9,16),
            new DateTime(2026,10,28), new DateTime(2026,12,9),
            // 2027 TENTATIVE
            new DateTime(2027,1,27), new DateTime(2027,3,17),
            new DateTime(2027,4,28), new DateTime(2027,6,16),
            new DateTime(2027,7,28), new DateTime(2027,9,15),
            new DateTime(2027,10,27), new DateTime(2027,12,8),
        };

        public static bool IsFomcDay(DateTime d) { return Fomc.Contains(d.Date); }

        public static bool EntryBlackout(DateTime nowEt)
        {
            if (!Cfg.EventBlackoutEnabled) return false;
            return IsFomcDay(nowEt)
                && string.CompareOrdinal(nowEt.ToString("HH:mm", CultureInfo.InvariantCulture),
                                         Cfg.FomcEntryBlackoutStart) >= 0;
        }

        public static bool ShouldFlatten(DateTime nowEt)
        {
            if (!(Cfg.EventBlackoutEnabled && Cfg.FomcFlattenPositions)) return false;
            return IsFomcDay(nowEt)
                && string.CompareOrdinal(nowEt.ToString("HH:mm", CultureInfo.InvariantCulture),
                                         Cfg.FomcFlattenTime) >= 0;
        }
    }

    // ── Signal engine (mirror of es_engine.py) ──────────────────────────────
    public class EsDecision
    {
        public DateTime BarTimeEt;
        public double Close;
        public MomentumState Momentum;
        public Dictionary<string, bool> Gates;
        public double? ZoneDistPct;
        public string EntrySide;   // "long"/"short"/null

        public bool AllPass
        {
            get { foreach (var v in Gates.Values) if (!v) return false; return true; }
        }
    }

    public class EsSignalEngine
    {
        public static readonly string[] GateNames =
            { "capacity", "max_trades", "window", "atr", "momentum", "cooldown", "zone" };

        public readonly MomentumEngine Momentum = new MomentumEngine();
        public readonly string ZoneVariant;
        public double? Atr5d;
        public int TradesToday, BarIndex = -1;
        private int? _lastExitBar;
        private DateTime? _session;

        public EsSignalEngine(string zoneVariant = "off")
        {
            if (zoneVariant != "off" && zoneVariant != "grid")
                throw new ArgumentException("unknown zone variant: " + zoneVariant);
            ZoneVariant = zoneVariant;
        }

        public void SetDailyAtr(double atr5d) { Atr5d = atr5d; }
        public void NoteEntry() { TradesToday += 1; }
        public void NoteExit()  { _lastExitBar = BarIndex; }

        private void ResetSession(DateTime d)
        {
            _session = d; TradesToday = 0; BarIndex = -1; _lastExitBar = null;
        }

        // fail CLOSED on warmup days — no entries beats peeking (es_engine.py)
        private Tuple<bool, double?> GridZone(double spot, string direction)
        {
            if (!Atr5d.HasValue) return Tuple.Create(false, (double?)null);
            double step = Cfg.EsGridStep;
            double offset = Math.Min(Cfg.AtrMult * Atr5d.Value, Cfg.EsGridMaxOffset);

            Func<string, double?> nearestOtm = side =>
            {
                double raw = side == "long" ? spot + offset : spot - offset;
                double target = Math.Round(
                    Math.Round(raw / step, MidpointRounding.ToEven) * step,
                    2, MidpointRounding.ToEven);
                double? best = null;
                for (int i = -Cfg.EsGridAlts; i <= Cfg.EsGridAlts; i++)
                {
                    double level = target + i * step;
                    bool otm = side == "long" ? level > spot : level < spot;
                    if (!otm) continue;
                    double d = Math.Abs(level - spot) / spot;
                    best = best.HasValue ? Math.Min(best.Value, d) : d;
                }
                return best;
            };

            var sides = direction == "bull" ? new[] { "long" }
                      : direction == "bear" ? new[] { "short" }
                      : new[] { "long", "short" };
            double? dist = null;
            foreach (var s in sides)
            {
                var d = nearestOtm(s);
                if (d.HasValue) dist = dist.HasValue ? Math.Min(dist.Value, d.Value) : d;
            }
            if (!dist.HasValue) return Tuple.Create(false, (double?)null);
            return Tuple.Create(dist.Value <= Cfg.ActivationPct, dist);
        }

        public EsDecision OnBar(AofBar bar, bool hasOpenPos)
        {
            if (_session != bar.TimeEt.Date) ResetSession(bar.TimeEt.Date);
            BarIndex += 1;

            var m = Momentum.OnBar(bar);

            string hhmm = bar.TimeEt.ToString("HH:mm", CultureInfo.InvariantCulture);
            bool inWindow = string.CompareOrdinal(Cfg.EntryStart, hhmm) <= 0
                         && string.CompareOrdinal(hhmm, Cfg.EntryEnd) <= 0;

            bool cooldownOk = !_lastExitBar.HasValue
                           || BarIndex - _lastExitBar.Value > Cfg.TradeCooldownBars;

            bool zoneOk; double? zoneDist;
            if (ZoneVariant == "grid")
            {
                var z = GridZone(bar.Close, m.Direction);
                zoneOk = z.Item1; zoneDist = z.Item2;
            }
            else { zoneOk = true; zoneDist = null; }

            var gates = new Dictionary<string, bool>
            {
                { "capacity",   !hasOpenPos },
                { "max_trades", TradesToday < Cfg.MaxTradesPerDay },
                { "window",     inWindow },
                { "atr",        m.Atr5 >= Cfg.EsAtr5MinFrac * bar.Close },
                { "momentum",   m.Direction == "bull" || m.Direction == "bear" },
                { "cooldown",   cooldownOk },
                { "zone",       zoneOk },
            };

            string side = null;
            bool all = true;
            foreach (var v in gates.Values) if (!v) { all = false; break; }
            if (all) side = m.Direction == "bull" ? "long" : "short";

            return new EsDecision
            {
                BarTimeEt = bar.TimeEt, Close = bar.Close, Momentum = m,
                Gates = gates, ZoneDistPct = zoneDist, EntrySide = side,
            };
        }
    }

    // ── Position + exits (mirror of futures_exits.py) ───────────────────────
    public class FuturesPosition
    {
        public string Side; public double EntryPrice; public int Qty;
        public double Atr5Entry; public DateTime EntryTime;
        public double StopPrice, TargetPrice;
        public double PeakFavorable, MaePoints;
        public int BarsHeld;

        public double Favorable(double price)
        {
            double d = price - EntryPrice;
            return Side == "long" ? d : -d;
        }

        public void UpdateOnBar(double high, double low, double close)
        {
            double hiFav = Favorable(high), loFav = Favorable(low);
            PeakFavorable = Math.Max(PeakFavorable, Math.Max(Math.Max(hiFav, loFav), 0.0));
            MaePoints     = Math.Max(MaePoints, -Math.Min(Math.Min(hiFav, loFav), 0.0));
            BarsHeld     += 1;
        }
    }

    public static class Exits
    {
        public static double StopDistance(double atr5)
        {
            return Math.Max(Cfg.EsStopAtrMult * atr5, Cfg.EsStopFloorPts);
        }

        public static double InitialStopPrice(string side, double entry, double atr5)
        {
            double d = StopDistance(atr5);
            return Px.RoundToTick(side == "long" ? entry - d : entry + d);
        }

        public static double TargetPrice(string side, double entry, double atr5)
        {
            double d = Cfg.EsTargetAtrMult * atr5;
            return Px.RoundToTick(side == "long" ? entry + d : entry - d);
        }

        // priority: time stop > trail > stagnation (futures_exits.py)
        public static string SoftExitReason(FuturesPosition pos, double close,
                                            TimeSpan barTimeEt)
        {
            var parts = Cfg.TimeStop.Split(':');
            var ts = new TimeSpan(int.Parse(parts[0]), int.Parse(parts[1]), 0);
            if (barTimeEt >= ts) return "time_stop";

            double arm = Cfg.EsTrailArmAtrMult * pos.Atr5Entry;
            if (pos.PeakFavorable >= arm && arm > 0)
            {
                double retrace = pos.PeakFavorable - pos.Favorable(close);
                if (retrace >= Cfg.EsTrailGiveback * pos.PeakFavorable) return "trail";
            }

            if (pos.BarsHeld >= Cfg.EsStagnationBars
                && pos.PeakFavorable < Cfg.EsStagnationAtrFrac * pos.Atr5Entry)
                return "stagnation";

            return null;
        }
    }

    // ── Sim fills (mirror of futures_sim.py — conservative by construction) ─
    public class FuturesSim
    {
        public readonly Contract Spec;
        public readonly double CommissionPerSide;

        public FuturesSim(string specRoot, double commissionPerSide)
        {
            Spec = Contract.Get(specRoot);
            CommissionPerSide = commissionPerSide;
        }

        public double EntryFill(string side, double barClose)
        {
            double slip = Cfg.EsEntrySlipTicks * Spec.TickSize;
            return Px.RoundToTick(side == "long" ? barClose + slip : barClose - slip);
        }

        public double CloseFill(string side, double barClose)
        {
            double slip = Cfg.EsEntrySlipTicks * Spec.TickSize;
            return Px.RoundToTick(side == "long" ? barClose - slip : barClose + slip);
        }

        // stop first when both are inside one bar; target needs trade-THROUGH
        public bool CheckHardExits(FuturesPosition pos, double high, double low,
                                   out double price, out string reason)
        {
            double t = Spec.TickSize;
            if (pos.Side == "long")
            {
                if (low <= pos.StopPrice)
                { price = Px.RoundToTick(pos.StopPrice - Cfg.EsStopSlipTicks * t); reason = "stop"; return true; }
                if (high >= pos.TargetPrice + t)
                { price = pos.TargetPrice; reason = "target"; return true; }
            }
            else
            {
                if (high >= pos.StopPrice)
                { price = Px.RoundToTick(pos.StopPrice + Cfg.EsStopSlipTicks * t); reason = "stop"; return true; }
                if (low <= pos.TargetPrice - t)
                { price = pos.TargetPrice; reason = "target"; return true; }
            }
            price = 0; reason = null; return false;
        }

        public double RoundTurnCommission(int qty) { return 2.0 * CommissionPerSide * qty; }

        public double PnlUsd(FuturesPosition pos, double exitPrice)
        {
            return pos.Favorable(exitPrice) * Spec.PointValue * pos.Qty
                 - RoundTurnCommission(pos.Qty);
        }

        public double UnrealizedUsd(FuturesPosition pos, double price)
        {
            return pos.Favorable(price) * Spec.PointValue * pos.Qty;
        }
    }

    // ── Apex 50K account (mirror of apex_risk.py) ───────────────────────────
    public class ApexAccount
    {
        public double Balance = Cfg.ApexStartBalance;
        public double PeakEquity = Cfg.ApexStartBalance;
        public double Threshold = Cfg.ApexStartBalance - Cfg.ApexTrailingDd;
        public bool ScalingUnlocked, Breached;
        public double UnrealizedConsumption;

        private static double ThresholdForPeak(double peak)
        {
            return Math.Min(peak - Cfg.ApexTrailingDd,
                            Cfg.ApexStartBalance + Cfg.ApexLockBuffer);
        }

        public bool MarkEquity(double equity)
        {
            if (equity > PeakEquity)
            {
                double newThr = ThresholdForPeak(equity);
                if (newThr > Threshold)
                {
                    if (equity > Balance) UnrealizedConsumption += newThr - Threshold;
                    Threshold = newThr;
                }
                PeakEquity = equity;
            }
            if (equity <= Threshold) Breached = true;
            return Breached;
        }

        public bool BookRealized(double pnl) { Balance += pnl; return MarkEquity(Balance); }

        public void EndOfDay()
        {
            if (Cfg.ApexHalfUntilNet
                && Balance >= Cfg.ApexStartBalance + Cfg.ApexTrailingDd + Cfg.ApexLockBuffer)
                ScalingUnlocked = true;
        }

        public double Headroom(double? equity = null)
        {
            double eq = equity ?? Balance;
            return Math.Max(0.0, eq - Threshold);
        }

        public int ContractsCap(Contract spec)
        {
            int cap = spec.Root == "ES" ? Cfg.ApexMaxMinis : Cfg.ApexMaxMinis * 10;
            if (Cfg.ApexHalfUntilNet && !ScalingUnlocked) cap = cap / 2;
            return cap;
        }

        public int SizeTrade(int stopTicks, Contract spec, double? equity = null)
        {
            if (Breached || stopTicks <= 0) return 0;
            double budget = Math.Min(Cfg.ApexRiskPerTrade,
                                     Cfg.ApexRiskHeadroomFrac * Headroom(equity));
            double perContract = stopTicks * spec.TickValue;
            int qty = perContract > 0 ? (int)Math.Floor(budget / perContract) : 0;
            return Math.Max(0, Math.Min(qty, ContractsCap(spec)));
        }

        public double DailyLossLimit()
        {
            return Math.Min(Cfg.ApexDailyLossCap, Cfg.ApexDailyLossFrac * Headroom());
        }

        public double WeeklyLossLimit()
        {
            return Math.Min(Cfg.ApexWeeklyLossCap, Cfg.ApexWeeklyLossFrac * Headroom());
        }

        public bool CanOpen(double nextTradeRisk, double realizedToday, double realizedWeek)
        {
            if (Breached) return false;
            if (realizedToday - nextTradeRisk < -DailyLossLimit()) return false;
            if (realizedWeek  - nextTradeRisk < -WeeklyLossLimit()) return false;
            return true;
        }
    }

    // ── Session runner (mirror of es_backtest.run_backtest, states only) ────
    // Drives one bar at a time so both the golden runner and the NT indicator
    // shell can share it. Emits exactly the STATE_COLUMNS row the Python
    // runner writes with emit_states=True.
    public class SessionRunner
    {
        public static readonly string[] StateColumns =
        {
            "date", "time_et", "open", "high", "low", "close", "volume",
            "ema5", "ema20", "vwap", "roc5", "atr5",
            "consec_green", "consec_red", "direction",
            "g_capacity", "g_max_trades", "g_window", "g_atr",
            "g_momentum", "g_cooldown", "g_zone",
            "all_pass", "entry_side", "in_pos", "pos_side", "qty",
            "stop_px", "target_px", "exit_reason",
        };

        public readonly EsSignalEngine Engine;
        public readonly FuturesSim Sim;
        public readonly ApexAccount Apex = new ApexAccount();
        public FuturesPosition Pos;

        private DateTime? _session;
        private readonly Queue<double> _dailyTrs = new Queue<double>(); // maxlen 5
        private double? _curHi, _curLo, _curClose, _prevClose;
        private double _realizedToday, _realizedWeek;
        private DateTime? _weekKey;   // ISO week keyed by its Thursday

        // signals surfaced for the NT indicator shell
        public string LastExitReason = "";
        public EsDecision LastDecision;
        public bool EnteredThisBar;

        public SessionRunner(string zoneVariant, string specRoot,
                             double commissionPerSide)
        {
            Engine = new EsSignalEngine(zoneVariant);
            Sim = new FuturesSim(specRoot, commissionPerSide);
        }

        private static DateTime IsoWeekThursday(DateTime d)
        {
            int dow = ((int)d.DayOfWeek + 6) % 7;      // Mon=0..Sun=6
            return d.Date.AddDays(3 - dow);
        }

        private void BookExit(DateTime barEt, double price, string reason)
        {
            double pnl = Sim.PnlUsd(Pos, price);
            Apex.BookRealized(pnl);
            _realizedToday += pnl; _realizedWeek += pnl;
            Engine.NoteExit();
            LastExitReason = reason;
            Pos = null;
        }

        /// Process one bar; returns the golden-vector state row (or null for
        /// non-RTH bars, which only warm the EMAs — exactly like Python).
        public string[] OnBar(AofBar bar)
        {
            // a breached account is DEAD — the Python runner breaks its bar
            // loop on breach and emits nothing further; mirror that exactly
            if (Apex.Breached) return null;

            var d = bar.TimeEt.Date;

            if (_session != d)
            {
                if (_session.HasValue && _curHi.HasValue)
                {
                    double tr = !_prevClose.HasValue
                        ? _curHi.Value - _curLo.Value
                        : Math.Max(_curHi.Value - _curLo.Value,
                          Math.Max(Math.Abs(_curHi.Value - _prevClose.Value),
                                   Math.Abs(_curLo.Value - _prevClose.Value)));
                    _dailyTrs.Enqueue(tr);
                    while (_dailyTrs.Count > 5) _dailyTrs.Dequeue();
                    _prevClose = _curClose;
                    Apex.EndOfDay();
                }
                _session = d;
                _curHi = _curLo = _curClose = null;
                _realizedToday = 0.0;
                var wk = IsoWeekThursday(d);
                if (_weekKey != wk) { _weekKey = wk; _realizedWeek = 0.0; }
                if (_dailyTrs.Count > 0)
                {
                    double s = 0; foreach (var tr in _dailyTrs) s += tr;
                    Engine.SetDailyAtr(s / _dailyTrs.Count);
                }
            }

            var tod = bar.TimeEt.TimeOfDay;
            bool isRth = tod >= new TimeSpan(9, 30, 0) && tod < new TimeSpan(16, 0, 0);
            if (isRth)
            {
                _curHi = _curHi.HasValue ? Math.Max(_curHi.Value, bar.High) : bar.High;
                _curLo = _curLo.HasValue ? Math.Min(_curLo.Value, bar.Low)  : bar.Low;
                _curClose = bar.Close;
            }

            string exitReasonThisBar = "";
            LastExitReason = ""; EnteredThisBar = false;

            if (Pos != null)
            {
                Pos.UpdateOnBar(bar.High, bar.Low, bar.Close);

                double best  = Pos.Side == "long" ? bar.High : bar.Low;
                double worst = Pos.Side == "long" ? bar.Low  : bar.High;
                Apex.MarkEquity(Apex.Balance + Sim.UnrealizedUsd(Pos, best));
                bool breached = Apex.MarkEquity(Apex.Balance + Sim.UnrealizedUsd(Pos, worst));

                if (breached)
                {
                    BookExit(bar.TimeEt, worst, "apex_liquidation");
                    exitReasonThisBar = "apex_liquidation";
                }
                else
                {
                    double hp; string hr;
                    if (Sim.CheckHardExits(Pos, bar.High, bar.Low, out hp, out hr))
                    {
                        BookExit(bar.TimeEt, hp, hr);
                        exitReasonThisBar = hr;
                    }
                    else
                    {
                        string soft = Events.ShouldFlatten(bar.TimeEt)
                            ? "event_flatten"
                            : Exits.SoftExitReason(Pos, bar.Close, tod);
                        if (soft != null)
                        {
                            BookExit(bar.TimeEt, Sim.CloseFill(Pos.Side, bar.Close), soft);
                            exitReasonThisBar = soft;
                        }
                    }
                }
            }

            var decision = Engine.OnBar(bar, Pos != null);
            LastDecision = decision;
            if (Apex.Breached) return null;

            bool blackout = Events.EntryBlackout(bar.TimeEt);
            int qty = 0;

            if (isRth && decision.EntrySide != null && Pos == null
                && !blackout && exitReasonThisBar == "")
            {
                double atr5 = decision.Momentum.Atr5;
                double fill = Sim.EntryFill(decision.EntrySide, bar.Close);
                double stopPx = Exits.InitialStopPrice(decision.EntrySide, fill, atr5);
                double targetPx = Exits.TargetPrice(decision.EntrySide, fill, atr5);
                int stopTicks = Px.TicksBetween(fill, stopPx);
                qty = Apex.SizeTrade(stopTicks, Sim.Spec);
                double riskUsd = stopTicks * Sim.Spec.TickValue * qty
                               + Sim.RoundTurnCommission(qty);
                bool riskOk = qty >= 1 && Apex.CanOpen(riskUsd, _realizedToday, _realizedWeek);
                if (riskOk)
                {
                    Pos = new FuturesPosition
                    {
                        Side = decision.EntrySide, EntryPrice = fill, Qty = qty,
                        Atr5Entry = atr5, EntryTime = bar.TimeEt,
                        StopPrice = stopPx, TargetPrice = targetPx,
                    };
                    Engine.NoteEntry();
                    EnteredThisBar = true;
                }
                else qty = 0;
            }

            if (!isRth) return null;

            var m = decision.Momentum;
            var inv = CultureInfo.InvariantCulture;
            Func<double, string> f6 = v => v.ToString("F6", inv);
            return new[]
            {
                d.ToString("yyyy-MM-dd", inv), bar.TimeEt.ToString("HH:mm:ss", inv),
                bar.Open.ToString("F2", inv), bar.High.ToString("F2", inv),
                bar.Low.ToString("F2", inv), bar.Close.ToString("F2", inv),
                bar.Volume.ToString("F0", inv),
                f6(m.Ema5), f6(m.Ema20), f6(m.Vwap), f6(m.Roc5), f6(m.Atr5),
                m.ConsecGreen.ToString(inv), m.ConsecRed.ToString(inv), m.Direction,
                decision.Gates["capacity"] ? "1" : "0",
                decision.Gates["max_trades"] ? "1" : "0",
                decision.Gates["window"] ? "1" : "0",
                decision.Gates["atr"] ? "1" : "0",
                decision.Gates["momentum"] ? "1" : "0",
                decision.Gates["cooldown"] ? "1" : "0",
                decision.Gates["zone"] ? "1" : "0",
                decision.AllPass ? "1" : "0",
                decision.EntrySide ?? "",
                Pos != null ? "1" : "0",
                Pos != null ? Pos.Side : "",
                (Pos != null ? Pos.Qty : 0).ToString(inv),
                Pos != null ? Pos.StopPrice.ToString("F2", inv) : "",
                Pos != null ? Pos.TargetPrice.ToString("F2", inv) : "",
                exitReasonThisBar,
            };
        }
    }
}
