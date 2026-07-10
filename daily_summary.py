"""
Daily results summary - end-of-session P&L delivered to a webhook, so
"how did today go" never requires opening a terminal.

Reuses the ALERT_WEBHOOK_URL channel by default (Slack-compatible JSON
POST - {"text": ...}; for Discord append /slack to the webhook URL, same
convention as alerts.py) so one webhook configured once covers both
anomaly alerts and the daily report. Set DAILY_SUMMARY_WEBHOOK_URL to
route the report somewhere else (e.g. a #results channel, with incident
alerts going to a separate #alerts channel).

Design note - this inverts alerts.py's "never fail silently" rule rather
than dropping it: a trading day with ZERO trades still sends a message.
Silence from this tool would otherwise be indistinguishable from the
supervisor never having run at all - and after the tzdata/alpaca-py
incidents (two silent no-op mornings in a row, no error visible anywhere
but the log), that distinction is exactly the one worth paying for.

Reads logs/trades_YYYY-MM-DD.csv (state.py's CSV_COLUMNS schema) - no
other dependency, so this runs standalone against any day's file, live or
replayed.

Run manually:
    python daily_summary.py                  # today, ET
    python daily_summary.py --date 2026-07-08
Wired automatically into run_session.py's post-session audit - runs once,
right after reconcile.py and feed_monitor.py, only on days the bot
actually attempted to launch (never on skipped weekends/holidays).
"""

import argparse
import csv
import json
import logging
import os
import urllib.request
from typing import List, Optional

import config

logger = logging.getLogger(__name__)

SEND_TIMEOUT = 10.0


def _trades_path(date_str: str) -> str:
    return os.path.join(config.LOG_DIR, f"trades_{date_str}.csv")


def load_trades(date_str: str) -> List[dict]:
    path = _trades_path(date_str)
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def _num(row: dict, col: str) -> float:
    v = row.get(col, "")
    try:
        return float(v) if v not in (None, "") else 0.0
    except ValueError:
        return 0.0


def summarize(rows: List[dict]) -> dict:
    n = len(rows)
    pnls = [_num(r, "realized_pnl") for r in rows]
    fees = sum(_num(r, "fees") for r in rows)
    gross = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    scratches = n - wins - losses
    reasons: dict = {}
    for r in rows:
        k = r.get("reason") or "?"
        reasons[k] = reasons.get(k, 0) + 1
    return {
        "trades": n, "wins": wins, "losses": losses, "scratches": scratches,
        "win_rate": (wins / n) if n else None,
        "gross_pnl": gross, "fees": fees, "net_pnl": gross - fees,
        "best": max(pnls) if pnls else 0.0,
        "worst": min(pnls) if pnls else 0.0,
        "reasons": reasons,
    }


def format_message(date_str: str, s: dict) -> str:
    tag = f"[{config.UNDERLYING} bot | {'PAPER' if config.PAPER else 'LIVE'}]"
    if s["trades"] == 0:
        return f"{tag} Results for {date_str}: 0 trades today."
    wr = f"{s['win_rate'] * 100:.0f}%" if s["win_rate"] is not None else "n/a"
    reasons = ", ".join(f"{k}={v}" for k, v in sorted(s["reasons"].items()))
    net_sign   = "+" if s["net_pnl"]   >= 0 else ""
    gross_sign = "+" if s["gross_pnl"] >= 0 else ""
    return (
        f"{tag} Results for {date_str}\n"
        f"Trades: {s['trades']}  (W {s['wins']} / L {s['losses']} / "
        f"S {s['scratches']})   Win rate: {wr}\n"
        f"Net P&L: {net_sign}${s['net_pnl']:.2f}  "
        f"(gross {gross_sign}${s['gross_pnl']:.2f}, fees ${s['fees']:.2f})\n"
        f"Best: +${s['best']:.2f}   Worst: ${s['worst']:.2f}\n"
        f"Exits: {reasons}"
    )


def send_webhook(url: str, text: str) -> bool:
    try:
        req = urllib.request.Request(
            url, data=json.dumps({"text": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=SEND_TIMEOUT).read()
        return True
    except Exception as e:
        logger.error("Daily summary webhook failed: %s", e)
        return False


def webhook_url() -> str:
    """DAILY_SUMMARY_WEBHOOK_URL takes priority; falls back to the same
    ALERT_WEBHOOK_URL alerts.py uses, so one webhook covers both by
    default. Read live (not cached at import) so a .env change is picked
    up without a restart of this standalone tool."""
    return (os.environ.get("DAILY_SUMMARY_WEBHOOK_URL", "").strip()
            or os.environ.get("ALERT_WEBHOOK_URL", "").strip())


def run(date_str: Optional[str] = None) -> dict:
    date_str = date_str or config.today_et().isoformat()
    rows = load_trades(date_str)
    s = summarize(rows)
    text = format_message(date_str, s)
    url = webhook_url()
    if url:
        s["webhook_sent"] = send_webhook(url, text)
    else:
        logger.info("No DAILY_SUMMARY_WEBHOOK_URL/ALERT_WEBHOOK_URL configured "
                    "- printing only.")
        s["webhook_sent"] = False
    print(text)
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--date", default=None, help="YYYY-MM-DD, default today ET")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    run(args.date)


if __name__ == "__main__":
    main()
