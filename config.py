import datetime
import os
from zoneinfo import ZoneInfo

ET      = ZoneInfo("America/New_York")
LOG_DIR = "logs"


def today_et() -> datetime.date:
    """Today in exchange time. Always use this - never date.today(), which is
    the machine-local date and wrong around midnight on non-ET hosts.
    Delegates to the clock module so replay's simulated clock governs dates
    too (lazy import - clock imports config, so no top-level cycle)."""
    import clock
    return clock.today_et()


# -- API credentials ------------------------------------------------------------
# Loaded from the environment (optionally via a local .env file - see
# .env.example). Never hardcode keys in a tracked file.
def _load_dotenv(path: str = ".env"):
    """Minimal stdlib .env loader: KEY=VALUE lines, '#' comments. Existing
    environment variables always win over the file."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    except OSError:
        pass


_load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY", "")
ALPACA_API_SECRET = os.environ.get("ALPACA_API_SECRET", "")
# Live trading requires PAPER=false explicitly - the default is always paper.
PAPER             = os.environ.get("ALPACA_PAPER", "true").strip().lower() != "false"


def validate_credentials():
    """Fail fast at startup instead of failing at 09:30 with a stream error."""
    if not ALPACA_API_KEY or not ALPACA_API_SECRET or "YOUR_" in ALPACA_API_KEY:
        raise SystemExit(
            "Alpaca credentials missing. Set ALPACA_API_KEY / ALPACA_API_SECRET "
            "environment variables, or copy .env.example to .env and fill it in."
        )


# -- Universe -------------------------------------------------------------------
UNDERLYING        = "SPY"

# -- Data feeds -----------------------------------------------------------------
# iex/indicative are Alpaca's free tiers: IEX is ~2-3% of consolidated stock
# volume and the indicative options feed is sampled - fine for paper and
# development, NOT for live capital. With the paid Alpaca market-data
# subscription set ALPACA_STOCK_FEED=sip and ALPACA_OPTION_FEED=opra (full
# consolidated tape / full options NBBO). Startup probes the entitlement and
# fails fast with an actionable message if the account lacks the
# subscription, instead of dying at 09:30 with a stream error.
STOCK_FEED  = os.environ.get("ALPACA_STOCK_FEED", "iex").strip().lower()        # iex | sip
OPTION_FEED = os.environ.get("ALPACA_OPTION_FEED", "indicative").strip().lower()  # indicative | opra

# -- Scheduled macro events (event_calendar.py) ---------------------------------
# FOMC statement drops at 14:00 ET - inside the entry window. Holding fresh
# long 0DTE gamma into it is a headline bet, not the strategy. Risk gating,
# on by default; premarket events (CPI/NFP) are observe-only by default.
EVENT_BLACKOUT_ENABLED    = True
FOMC_ENTRY_BLACKOUT_START = "13:30"  # ET - no new entries from here on FOMC days
FOMC_FLATTEN_POSITIONS    = True     # close any open position before the statement
FOMC_FLATTEN_TIME         = "13:45"  # ET - flatten deadline on FOMC days
PREMARKET_EVENT_OPEN_DELAY_MIN = 0   # >0 pushes ENTRY_START later on CPI/NFP days
                                     # (0 = observe-only; validate in shadow first)

# -- Strike selection -----------------------------------------------------------
# Target strike = round(SPY_price + min(ATR_MULT * ATR_5day, MAX_STRIKE_OFFSET)) to nearest STRIKE_STEP
ATR_MULT          = 0.60   # strike offset as multiple of 5-day daily ATR
MAX_STRIKE_OFFSET = 4.50   # hard cap - prevents elevated ATR from placing strikes
                            # so far OTM that SPY never reaches the approach zone
STRIKE_STEP       = 0.50   # SPY strikes are in $0.50 increments near the money
STRIKE_ALTS       = 6      # extra strikes above/below the target, each side.
                            # Total subscribed = (2*ALTS+1)*2 option symbols.
                            # 6 -> 26 symbols, under Alpaca's free-plan 30-symbol
                            # WebSocket cap (run feed_monitor.py after a session
                            # to confirm zero dark symbols). On the paid feed
                            # (OPRA, no symbol cap) widen back to 10 for a bigger
                            # no-resubscribe window. +/-$3 here matches the +/-$3
                            # re-subscribe trigger, so nothing is lost at 6.

# -- Entry filters --------------------------------------------------------------
# NOTE: these times are defined relative to a regular 16:00 ET close. On
# early-close days (13:00 ET) main.py re-anchors them to the actual session
# close at startup via market_calendar.shift_for_close(), preserving the
# offset-from-close. A fixed 15:25 time stop on a 13:00 close day would
# otherwise hold a 0DTE position into expiry.
ENTRY_START       = "09:45"  # ET - ignore signals before this
ENTRY_END         = "14:30"  # ET - no new entries after this
TIME_STOP         = "15:25"  # ET - force-close all positions

OPTION_MIN_PRICE  = 0.20   # min price - filters deeply OTM lottery tickets
OPTION_MAX_PRICE  = 10.00  # max price

# -- Momentum thresholds --------------------------------------------------------
MIN_CONSEC_BARS   = 3      # consecutive green (or red) 1-min bars required
ROC_THRESHOLD     = 0.0003 # 5-bar rate-of-change minimum
ATR5_MIN_ENTRY    = 0.20   # min 5-bar intrabar ATR at entry - blocks low-vel theta-bleed setups

# -- Strike proximity zones -----------------------------------------------------
ACTIVATION_PCT    = 0.003  # within 0.3% of strike -> "activation" zone
APPROACH_PCT      = 0.007  # within 0.7% -> "approach" zone (outer band)

# -- Proxy delta (disabled) -----------------------------------------------------
# Delta only updates on 1-min bar ticks so stays 0 between bars and blocks
# all entries if used as a filter. Momentum + zone filters are sufficient.
PROXY_DELTA_MIN      = 0.0
REQUIRE_DELTA_RISING = False

# -- Risk & sizing --------------------------------------------------------------
MAX_RISK_PER_TRADE  = 150.00  # dollars at risk per trade (stop-loss basis).
                              # HARD limit: if even 1 contract exceeds this,
                              # the trade is skipped - never rounded up to 1.
MAX_PREMIUM_PER_TRADE = 600.00  # cap on total premium outlay (qty x price x 100).
                                # The true worst case on a long 0DTE option is
                                # 100% of premium (gap through the stop), so this
                                # bounds the tail loss independently of the stop.
MAX_DAILY_LOSS      = 300.00  # hard daily loss limit - bot stops new entries.
                              # Enforced *prospectively*: an entry is blocked if
                              # a full stop-out would breach this limit, not
                              # only after the loss is already booked.
WEEKLY_MAX_LOSS     = 900.00  # hard weekly loss limit (Mon-Fri, realized) -
                              # daily limits alone let five max-loss days
                              # compound; this bounds the week. Enforced
                              # prospectively like the daily limit.
MAX_TRADES_PER_DAY  = 999     # effectively unlimited during paper/data collection
TRADE_COOLDOWN_BARS = 3       # 1-min bars to wait after a close before re-entering

# -- Execution quality ----------------------------------------------------------
ENTRY_MAX_SPREAD_PCT = 0.15  # skip entries when bid/ask spread > 15% of mid -
                             # a mid computed inside a wide spread is not a price
ENTRY_QUOTE_MAX_AGE_SEC = 3.0  # skip entries on quotes older than this - a
                               # queued entry task must not size a trade off a
                               # cached quote from before a 30s fill wait
EXIT_WIDE_SPREAD_PCT = 0.25  # above this, TP/trail evaluation is skipped for the
                             # tick (unreliable mid) but the hard stop is still
                             # evaluated on the BID - dislocations are exactly
                             # when the stop must not go blind

# -- Fees -----------------------------------------------------------------------
# Alpaca charges $0 commission but regulatory/exchange fees are real:
# OCC clearing + ORF + (on sells) SEC/TAF ~ $0.10-$0.20 per contract round trip.
# Booked P&L is net of this so the track record doesn't overstate edge.
FEES_PER_CONTRACT_RT = 0.15

# -- Exit levels ----------------------------------------------------------------
STOP_MULT          = 0.50   # hard stop: exit if price falls to 50% of entry
TP_MULT            = 1.50   # take profit: exit at 50% gain (1.5x entry)
CAT_STOP_MULT      = 0.20   # catastrophic backstop: if the executable BID is at
                            # or below 20% of entry, flatten immediately. This
                            # is deliberately REDUNDANT with the quote-driven
                            # stop - it runs on an independent code path (the
                            # 5s safety watcher), so a defect or starvation in
                            # the quote-handler exit path can never leave a
                            # collapsing position unbounded. Dumbest possible
                            # rule, separately evaluated: that's the point.

# -- Restart-storm brake --------------------------------------------------------
# The watchdog restarting once is recovery; restarting every few minutes is a
# failure loop re-entering the same defect with a position possibly open.
# At the threshold: flatten via REST, halt, alert, refuse to run until
# `python restart_guard.py --clear`.
RESTART_STORM_MAX        = 3     # watchdog restarts within the window -> halt
RESTART_STORM_WINDOW_SEC = 3600

# When launched under the supervisor (run_session.py sets AOF_SUPERVISED=1),
# the watchdog restarts the session by EXITING with this code and letting the
# supervisor relaunch - rather than os.execl'ing itself in place. os.execl
# under a waiting parent process changes the PID on Windows and orphans the
# new process; the supervisor owning the relaunch is correct and cross-platform.
SUPERVISED               = os.environ.get("AOF_SUPERVISED") == "1"
SUPERVISED_RESTART_CODE  = 42

# -- Peak trailing stop ---------------------------------------------------------
# Arms once the option gains PEAK_TRAIL_ACTIVATE above entry.
# From that point, trails at PEAK_TRAIL_PCT x the highest mid seen.
# Activation threshold must clear bid/ask spread noise on cheap options -
# 1.20x requires a genuine $0.05+ move on a $0.25 option; spread noise can't reach it.
PEAK_TRAIL_ACTIVATE = 1.20  # trail arms: option must reach 20% gain first
PEAK_TRAIL_PCT      = 0.88  # trail stop: exit if mid falls to 88% of peak

# -- SPY-level stop -------------------------------------------------------------
# Fires on bar close if SPY closes more than buf dollars against the position.
# Buffer scales with intrabar volatility at entry - tighter when calm, wider
# when choppy - reducing the whipsaw false-stops a fixed dollar amount causes.
SPY_STOP_ATR_MULT  = 0.75  # buffer = 0.75 x atr5 at entry time
SPY_STOP_FLOOR     = 0.10  # minimum buffer regardless of atr5

# -- Data staleness kill switch -------------------------------------------------
# If quotes for the held symbol stop arriving (silent WebSocket death - no
# exception, just no messages), the position is flying blind: the 30s safety
# net would chew the same cached quote forever. Flatten instead.
STALE_QUOTE_FLATTEN_SEC = 45   # holding + no fresh quote for this long -> close
STALE_BAR_WARN_SEC      = 180  # no SPY bar for this long during RTH -> lock new
                               # entries and log CRITICAL (bars drive the SPY
                               # stop and the ghost sweeper)

# -- Order management -----------------------------------------------------------
CLIENT_ORDER_PREFIX = "aof"  # tags this bot's orders so cleanup only touches
                             # its own orders, not everything on the account

# -- Market data recording (replay) ---------------------------------------------
# Records every bar and option quote the decision code receives to
# recordings/session_YYYY-MM-DD.jsonl.gz (a few tens of MB per session,
# written off the event loop). These recordings feed replay.py, which runs
# the SAME live code paths deterministically - the only honest way to test
# parameter changes without waiting one live session per calendar day.
RECORD_MARKET_DATA = True
RECORDINGS_DIR     = "recordings"

# Structured decision log (decision_logger.py): one row per (bar, candidate)
# with every gate's verdict - the counterfactual record that makes strategy-
# drift diagnosis possible (drift_report.py reads it). Written from the
# shared code path, so replay regenerates it for any recorded session.
DECISION_LOG = True

# -- Web monitor + dashboard ----------------------------------------------------
# TWO read-only pages, both localhost-only:
#   dashboard.py (8080) - ALWAYS-ON. Runs as its own process, reads results
#     from disk, works whether or not the bot is trading. This is the URL to
#     bookmark: http://127.0.0.1:8080
#   monitor.py (8081) - the LIVE ticking view, served from inside the bot;
#     exists only while the bot runs. On 8081 so it never clashes with the
#     always-on dashboard when both are up. MONITOR_PORT=0 disables it.
MONITOR_HOST = os.environ.get("MONITOR_HOST", "127.0.0.1")
MONITOR_PORT = int(os.environ.get("MONITOR_PORT", "8081") or 0)

DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8080") or 0)

# -- Polling --------------------------------------------------------------------
SNAPSHOT_POLL_SEC  = 30    # how often to poll REST snapshot for proxy-delta calc

# ==============================================================================
# ES FUTURES PORT (NinjaTrader sibling strategy - see ES_PORT_PLAN.md)
# The signal engine is shared; everything below re-derives the option-specific
# machinery (sizing, exits, account rules) in futures/price space.
# ==============================================================================

# -- Instrument -----------------------------------------------------------------
ES_SPEC_ROOT = "MES"        # start on micros: 10x sizing granularity while the
                            # sibling strategy is unvalidated (plan Sec.4)

# -- Signal re-basing -----------------------------------------------------------
# ATR5_MIN_ENTRY is 0.20 absolute SPY points (~3.2bp at SPY 627). ES trades at
# ~10x SPY's level, so the velocity gate is re-specified as a FRACTION of price
# - same economic threshold, instrument-independent. Sweep on replay before
# trusting the exact value.
ES_ATR5_MIN_FRAC = 0.00032

# Zone translation variant (plan Sec.2.3, corrected):  "off" = momentum-only
# (default - the subscription-window analysis showed the zone machinery mostly
# selects WHICH contract, not WHETHER to trade);  "grid" = geometric replication
# of the SPY strike grid x10 (parity control for the divergence report).
ES_ZONE_VARIANT = "off"
ES_GRID_STEP          = 5.00    # $0.50 SPY strike step x 10
ES_GRID_MAX_OFFSET    = 45.0    # MAX_STRIKE_OFFSET 4.50 x 10
ES_GRID_ALTS          = 6       # subscription window +/- steps (STRIKE_ALTS)

# -- Exit stack (price space - plan Sec.3; provisional k's, MUST be swept) ---------
ES_STOP_ATR_MULT      = 0.75    # primary stop (ports directly from SPY_STOP_ATR_MULT)
ES_STOP_FLOOR_PTS     = 1.00    # SPY_STOP_FLOOR 0.10 x 10
ES_TARGET_ATR_MULT    = 1.50    # provisional; re-derive from recorded options MFEs
ES_TRAIL_ARM_ATR_MULT = 1.00    # trail arms after this favorable excursion
ES_TRAIL_GIVEBACK     = 0.40    # exit when retrace >= 40% of peak excursion
                                # (Apex real-time trailing DD punishes give-back -
                                # unrealized peaks consume headroom permanently)
ES_STAGNATION_BARS     = 20     # theta replacement: flat tape has no carry cost
ES_STAGNATION_ATR_FRAC = 0.25   # ... exit if peak excursion < this x atr5 by then

# -- Execution model (backtest/sim; pin to broker fills when live) --------------
ES_ENTRY_SLIP_TICKS = 1         # marketable entry: adverse ticks beyond bar close
ES_STOP_SLIP_TICKS  = 1         # stop fills through the stop price
ES_COMMISSION_PER_SIDE = {"ES": 3.20, "MES": 1.30}   # all-in $/side/contract -
                                # PROVISIONAL; pin to the actual Apex/Rithmic
                                # schedule and sweep 0-4 tick slip (plan Sec.8)

# -- Apex Trader Funding 50K account model (verified July 2026) -----------------
# Trailing threshold trails IN REAL TIME on unrealized equity peaks and locks
# once it reaches start + $100. Half contracts until EOD balance reaches
# start + drawdown + $100 ($52,600). Consistency: highest day <= 50% of total
# profit at payout (soft - delays payout, does not breach). Flat by 16:59 ET
# (our 15:25 time stop is well inside). Since March 2026 every order must
# carry an attached stop - the ATM bracket satisfies this.
APEX_START_BALANCE   = 50_000.0
APEX_TRAILING_DD     = 2_500.0
APEX_LOCK_BUFFER     = 100.0
APEX_MAX_MINIS       = 10       # 50K plan cap (micros = 10x this)
APEX_HALF_UNTIL_NET  = True     # contract scaling rule
APEX_CONSISTENCY_PCT = 0.50

# Sizing/risk derived from the DRAWDOWN BUFFER, not the nominal 50K: the
# tradeable capital is the $2,500 between balance and threshold.
APEX_RISK_PER_TRADE     = 125.0   # <=5% of a fresh buffer (was $150 on options)
APEX_RISK_HEADROOM_FRAC = 0.05    # ... and never more than 5% of CURRENT headroom
APEX_DAILY_LOSS_CAP     = 300.0   # familiar cap, AND <=10% of headroom below
APEX_DAILY_LOSS_FRAC    = 0.10
APEX_WEEKLY_LOSS_CAP    = 900.0
APEX_WEEKLY_LOSS_FRAC   = 0.30
