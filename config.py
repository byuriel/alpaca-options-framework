import datetime
import os
from zoneinfo import ZoneInfo

ET      = ZoneInfo("America/New_York")
LOG_DIR = "logs"


def today_et() -> datetime.date:
    """Today in exchange time. Always use this — never date.today(), which is
    the machine-local date and wrong around midnight on non-ET hosts."""
    return datetime.datetime.now(tz=ET).date()


# ── API credentials ────────────────────────────────────────────────────────────
# Loaded from the environment (optionally via a local .env file — see
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
# Live trading requires PAPER=false explicitly — the default is always paper.
PAPER             = os.environ.get("ALPACA_PAPER", "true").strip().lower() != "false"


def validate_credentials():
    """Fail fast at startup instead of failing at 09:30 with a stream error."""
    if not ALPACA_API_KEY or not ALPACA_API_SECRET or "YOUR_" in ALPACA_API_KEY:
        raise SystemExit(
            "Alpaca credentials missing. Set ALPACA_API_KEY / ALPACA_API_SECRET "
            "environment variables, or copy .env.example to .env and fill it in."
        )


# ── Universe ───────────────────────────────────────────────────────────────────
UNDERLYING        = "SPY"

# ── Strike selection ───────────────────────────────────────────────────────────
# Target strike = round(SPY_price + min(ATR_MULT * ATR_5day, MAX_STRIKE_OFFSET)) to nearest STRIKE_STEP
ATR_MULT          = 0.60   # strike offset as multiple of 5-day daily ATR
MAX_STRIKE_OFFSET = 4.50   # hard cap — prevents elevated ATR from placing strikes
                            # so far OTM that SPY never reaches the approach zone
STRIKE_STEP       = 0.50   # SPY strikes are in $0.50 increments near the money
STRIKE_ALTS       = 10     # extra strikes above/below — wide window, never resubscribe

# ── Entry filters ──────────────────────────────────────────────────────────────
# NOTE: these times are defined relative to a regular 16:00 ET close. On
# early-close days (13:00 ET) main.py re-anchors them to the actual session
# close at startup via market_calendar.shift_for_close(), preserving the
# offset-from-close. A fixed 15:25 time stop on a 13:00 close day would
# otherwise hold a 0DTE position into expiry.
ENTRY_START       = "09:45"  # ET — ignore signals before this
ENTRY_END         = "14:30"  # ET — no new entries after this
TIME_STOP         = "15:25"  # ET — force-close all positions

OPTION_MIN_PRICE  = 0.20   # min price — filters deeply OTM lottery tickets
OPTION_MAX_PRICE  = 10.00  # max price

# ── Momentum thresholds ────────────────────────────────────────────────────────
MIN_CONSEC_BARS   = 3      # consecutive green (or red) 1-min bars required
ROC_THRESHOLD     = 0.0003 # 5-bar rate-of-change minimum
ATR5_MIN_ENTRY    = 0.20   # min 5-bar intrabar ATR at entry — blocks low-vel theta-bleed setups

# ── Strike proximity zones ─────────────────────────────────────────────────────
ACTIVATION_PCT    = 0.003  # within 0.3% of strike → "activation" zone
APPROACH_PCT      = 0.007  # within 0.7% → "approach" zone (outer band)

# ── Proxy delta (disabled) ─────────────────────────────────────────────────────
# Delta only updates on 1-min bar ticks so stays 0 between bars and blocks
# all entries if used as a filter. Momentum + zone filters are sufficient.
PROXY_DELTA_MIN      = 0.0
REQUIRE_DELTA_RISING = False

# ── Risk & sizing ──────────────────────────────────────────────────────────────
MAX_RISK_PER_TRADE  = 150.00  # dollars at risk per trade (stop-loss basis).
                              # HARD limit: if even 1 contract exceeds this,
                              # the trade is skipped — never rounded up to 1.
MAX_PREMIUM_PER_TRADE = 600.00  # cap on total premium outlay (qty × price × 100).
                                # The true worst case on a long 0DTE option is
                                # 100% of premium (gap through the stop), so this
                                # bounds the tail loss independently of the stop.
MAX_DAILY_LOSS      = 300.00  # hard daily loss limit — bot stops new entries.
                              # Enforced *prospectively*: an entry is blocked if
                              # a full stop-out would breach this limit, not
                              # only after the loss is already booked.
MAX_TRADES_PER_DAY  = 999     # effectively unlimited during paper/data collection
TRADE_COOLDOWN_BARS = 3       # 1-min bars to wait after a close before re-entering

# ── Execution quality ──────────────────────────────────────────────────────────
ENTRY_MAX_SPREAD_PCT = 0.15  # skip entries when bid/ask spread > 15% of mid —
                             # a mid computed inside a wide spread is not a price
ENTRY_QUOTE_MAX_AGE_SEC = 3.0  # skip entries on quotes older than this — a
                               # queued entry task must not size a trade off a
                               # cached quote from before a 30s fill wait
EXIT_WIDE_SPREAD_PCT = 0.25  # above this, TP/trail evaluation is skipped for the
                             # tick (unreliable mid) but the hard stop is still
                             # evaluated on the BID — dislocations are exactly
                             # when the stop must not go blind

# ── Fees ───────────────────────────────────────────────────────────────────────
# Alpaca charges $0 commission but regulatory/exchange fees are real:
# OCC clearing + ORF + (on sells) SEC/TAF ≈ $0.10–$0.20 per contract round trip.
# Booked P&L is net of this so the track record doesn't overstate edge.
FEES_PER_CONTRACT_RT = 0.15

# ── Exit levels ────────────────────────────────────────────────────────────────
STOP_MULT          = 0.50   # hard stop: exit if price falls to 50% of entry
TP_MULT            = 1.50   # take profit: exit at 50% gain (1.5× entry)

# ── Peak trailing stop ─────────────────────────────────────────────────────────
# Arms once the option gains PEAK_TRAIL_ACTIVATE above entry.
# From that point, trails at PEAK_TRAIL_PCT × the highest mid seen.
# Activation threshold must clear bid/ask spread noise on cheap options —
# 1.20× requires a genuine $0.05+ move on a $0.25 option; spread noise can't reach it.
PEAK_TRAIL_ACTIVATE = 1.20  # trail arms: option must reach 20% gain first
PEAK_TRAIL_PCT      = 0.88  # trail stop: exit if mid falls to 88% of peak

# ── SPY-level stop ─────────────────────────────────────────────────────────────
# Fires on bar close if SPY closes more than buf dollars against the position.
# Buffer scales with intrabar volatility at entry — tighter when calm, wider
# when choppy — reducing the whipsaw false-stops a fixed dollar amount causes.
SPY_STOP_ATR_MULT  = 0.75  # buffer = 0.75 × atr5 at entry time
SPY_STOP_FLOOR     = 0.10  # minimum buffer regardless of atr5

# ── Data staleness kill switch ─────────────────────────────────────────────────
# If quotes for the held symbol stop arriving (silent WebSocket death — no
# exception, just no messages), the position is flying blind: the 30s safety
# net would chew the same cached quote forever. Flatten instead.
STALE_QUOTE_FLATTEN_SEC = 45   # holding + no fresh quote for this long → close
STALE_BAR_WARN_SEC      = 180  # no SPY bar for this long during RTH → lock new
                               # entries and log CRITICAL (bars drive the SPY
                               # stop and the ghost sweeper)

# ── Order management ───────────────────────────────────────────────────────────
CLIENT_ORDER_PREFIX = "aof"  # tags this bot's orders so cleanup only touches
                             # its own orders, not everything on the account

# ── Polling ────────────────────────────────────────────────────────────────────
SNAPSHOT_POLL_SEC  = 30    # how often to poll REST snapshot for proxy-delta calc
