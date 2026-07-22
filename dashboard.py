"""
Always-on dashboard - the status page that works whether or not the bot is
trading.

The problem this solves: monitor.py serves its page from INSIDE the bot
process, so http://127.0.0.1:8080 only exists while the bot is live (roughly
09:00-15:25 ET on trading days). Every other moment - nights, weekends,
after the session ends - the page is unreachable, which reads as "broken."

This server runs on its OWN, independently of the bot, and reads everything
from the files on disk (logs/trades_*.csv, the position state file). So it
is always up and always answers, and it shows a CUMULATIVE summary since the
bot's first recorded trade - not just the current session.

  python dashboard.py                 # serve on DASHBOARD_HOST/PORT (8080)
  python dashboard.py --once           # print a text summary and exit

Read-only by construction (two GET routes; every other method/path 405s),
localhost-bound by default, stdlib only, self-contained HTML. Same rules as
monitor.py.

"Is the bot running right now?" is inferred without any coupling to the bot:
the bot appends to logs/bot_<date>.log continuously while alive, so a log
touched within LIVENESS_WINDOW_SEC means it is live. No shared process, no
port handshake, no new write path in the trading code.
"""

import argparse
import csv
import datetime
import glob
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional

import config

logger = logging.getLogger(__name__)

LIVENESS_WINDOW_SEC = 150     # bot log touched within this -> "running"


# -- Data assembly (all from disk) ---------------------------------------------

def _num(row: dict, col: str) -> float:
    v = row.get(col, "")
    try:
        return float(v) if v not in (None, "") else 0.0
    except ValueError:
        return 0.0


def _load_day(path: str) -> List[dict]:
    try:
        with open(path, newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


def _day_summary(date_str: str, rows: List[dict]) -> dict:
    pnls = [_num(r, "realized_pnl") for r in rows]
    fees = sum(_num(r, "fees") for r in rows)
    gross = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    return {
        "date": date_str, "trades": len(rows),
        "wins": wins, "losses": losses, "scratches": len(rows) - wins - losses,
        "gross_pnl": gross, "fees": fees, "net_pnl": gross - fees,
        "best": max(pnls) if pnls else 0.0,
        "worst": min(pnls) if pnls else 0.0,
    }


def _trade_date(path: str) -> str:
    base = os.path.basename(path)
    return base[len("trades_"):-len(".csv")] if base.startswith("trades_") else base


def bot_running() -> Optional[float]:
    """Seconds since the bot's log was last written, or None if never/too
    old to be considered live. The liveness heuristic - no bot coupling."""
    logs = glob.glob(os.path.join(config.LOG_DIR, "bot_*.log"))
    if not logs:
        return None
    newest = max(logs, key=os.path.getmtime)
    age = _now_ts() - os.path.getmtime(newest)
    return age if age <= LIVENESS_WINDOW_SEC else None


def _now_ts() -> float:
    import time
    return time.time()


def open_position() -> Optional[dict]:
    from state import BotState
    return BotState.load_persisted_position()


def build_state() -> dict:
    """The whole payload, assembled fresh from disk on each request."""
    files = sorted(glob.glob(os.path.join(config.LOG_DIR, "trades_*.csv")))
    days = []
    for p in files:
        rows = _load_day(p)
        days.append(_day_summary(_trade_date(p), rows))
    days_with_trades = [d for d in days if d["trades"] > 0]

    total_trades = sum(d["trades"] for d in days)
    total_wins = sum(d["wins"] for d in days)
    total_net = sum(d["net_pnl"] for d in days)
    total_gross = sum(d["gross_pnl"] for d in days)
    total_fees = sum(d["fees"] for d in days)
    green_days = sum(1 for d in days_with_trades if d["net_pnl"] > 0)

    today = config.today_et().isoformat()
    today_rows = _load_day(os.path.join(config.LOG_DIR, f"trades_{today}.csv"))

    live_age = bot_running()
    return {
        "ts_et": datetime.datetime.now(tz=config.ET).strftime("%Y-%m-%d %H:%M:%S"),
        "paper": config.PAPER,
        "underlying": config.UNDERLYING,
        "bot": {
            "running": live_age is not None,
            "log_age_s": round(live_age, 0) if live_age is not None else None,
        },
        "since_inception": {
            "first_day": days_with_trades[0]["date"] if days_with_trades else None,
            "trading_days": len(days_with_trades),
            "trades": total_trades,
            "wins": total_wins,
            "win_rate": (total_wins / total_trades) if total_trades else None,
            "net_pnl": round(total_net, 2),
            "gross_pnl": round(total_gross, 2),
            "fees": round(total_fees, 2),
            "green_days": green_days,
            "red_days": len(days_with_trades) - green_days,
            "best_day": max((d["net_pnl"] for d in days_with_trades), default=0.0),
            "worst_day": min((d["net_pnl"] for d in days_with_trades), default=0.0),
        },
        "days": list(reversed(days))[:30],          # most-recent 30, newest first
        "today": {
            "date": today,
            **_day_summary(today, today_rows),
            "trades_list": [{
                "time": r.get("exit_time", ""), "symbol": r.get("symbol", ""),
                "side": r.get("side", ""), "qty": r.get("qty", ""),
                "entry": _num(r, "entry_price"), "exit": _num(r, "exit_price"),
                "reason": r.get("reason", ""), "pnl": _num(r, "realized_pnl"),
            } for r in today_rows],
        },
        "position": open_position(),
    }


# -- Text mode (quick terminal check) ------------------------------------------

def print_summary():
    d = build_state()
    si = d["since_inception"]
    b = d["bot"]
    print("=" * 60)
    print(f"  {d['underlying']} BOT DASHBOARD  ({'PAPER' if d['paper'] else 'LIVE'})"
          f"   {d['ts_et']} ET")
    print("=" * 60)
    status = (f"RUNNING (log {b['log_age_s']:.0f}s ago)" if b["running"]
              else "NOT RUNNING")
    print(f"  Bot status      : {status}")
    if si["first_day"]:
        print(f"  Since {si['first_day']}  ({si['trading_days']} trading days)")
        wr = f"{si['win_rate']*100:.0f}%" if si["win_rate"] is not None else "n/a"
        print(f"  Total net P&L   : {'+' if si['net_pnl']>=0 else ''}${si['net_pnl']:.2f}"
              f"   ({si['trades']} trades, win rate {wr})")
        print(f"  Green/red days  : {si['green_days']} / {si['red_days']}")
        print(f"  Best / worst day: +${si['best_day']:.2f} / ${si['worst_day']:.2f}")
    else:
        print("  No trades recorded yet.")
    t = d["today"]
    print(f"  Today ({t['date']}): {t['trades']} trades, "
          f"net {'+' if t['net_pnl']>=0 else ''}${t['net_pnl']:.2f}")
    if d["position"]:
        p = d["position"]
        print(f"  OPEN POSITION   : {p.get('symbol')} x{p.get('qty')} "
              f"entry ${p.get('entry_price')}")
    print("=" * 60)


# -- Server --------------------------------------------------------------------

class Dashboard:
    def __init__(self, host: str = "127.0.0.1", port: int = 8080):
        self.host, self.port = host, port
        self._httpd: Optional[ThreadingHTTPServer] = None

    def serve_forever(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/api/state":
                    try:
                        payload = build_state()
                    except Exception as e:
                        payload = {"error": f"assembly failed: {e}"}
                    self._respond(200, "application/json",
                                  json.dumps(payload, default=str).encode())
                elif self.path in ("/", "/index.html"):
                    self._respond(200, "text/html; charset=utf-8",
                                  HTML_PAGE.encode())
                else:
                    self._respond(404, "text/plain", b"not found")

            def _respond(self, code, ctype, body):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def send_error(self, code, message=None, explain=None):
                if code == 501:
                    code, message = 405, "read-only dashboard"
                super().send_error(code, message, explain)

            def log_message(self, fmt, *args):
                pass

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._httpd.daemon_threads = True
        logger.info("Dashboard live: http://%s:%d  (read-only, always-on)",
                    self.host, self.port)
        self._httpd.serve_forever()


HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>0DTE Bot Dashboard</title>
<style>
  :root {
    --surface:#1a1a19; --panel:#232322; --line:#3a3a38;
    --text:#fff; --text-2:#c3c2b7; --text-3:#8a897f;
    --good:#0ca30c; --warn:#fab219; --crit:#d03b3b; --accent:#3987e5;
  }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--surface); color:var(--text);
         font:14px/1.45 -apple-system,"Segoe UI",Roboto,sans-serif;
         padding:16px; max-width:1080px; margin:0 auto; }
  .num { font-variant-numeric:tabular-nums; }
  header { display:flex; align-items:baseline; gap:12px; flex-wrap:wrap; margin-bottom:14px; }
  h1 { font-size:16px; font-weight:650; }
  .chip { font-size:11px; padding:2px 8px; border-radius:999px;
          border:1px solid var(--line); color:var(--text-2); }
  .chip.run { border-color:var(--good); color:var(--good); font-weight:700; }
  .chip.off { border-color:var(--text-3); color:var(--text-3); }
  .chip.live { border-color:var(--crit); color:var(--crit); font-weight:700; }
  #conn { margin-left:auto; font-size:12px; color:var(--text-3); }
  #conn.down { color:var(--crit); font-weight:700; }
  .tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
           gap:8px; margin-bottom:14px; }
  .tile { background:var(--panel); border:1px solid var(--line);
          border-radius:10px; padding:10px 12px; }
  .tile .k { font-size:11px; text-transform:uppercase; letter-spacing:.8px;
             color:var(--text-3); margin-bottom:4px; }
  .tile .v { font-size:20px; font-weight:700; }
  .tile .s { font-size:11.5px; color:var(--text-2); margin-top:2px; }
  .panel { background:var(--panel); border:1px solid var(--line);
           border-radius:10px; padding:12px 14px; margin-bottom:14px; }
  .panel h2 { font-size:11px; text-transform:uppercase; letter-spacing:1px;
              color:var(--text-3); margin-bottom:8px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th { text-align:left; color:var(--text-3); font-weight:600; font-size:11px;
       text-transform:uppercase; letter-spacing:.6px; padding:4px 8px;
       border-bottom:1px solid var(--line); }
  td { padding:5px 8px; border-bottom:1px solid var(--line); color:var(--text-2); }
  td.num, th.num { text-align:right; }
  .pos { color:var(--good); } .neg { color:var(--crit); }
  footer { color:var(--text-3); font-size:11px; margin-top:10px; }
</style>
</head>
<body>
<header>
  <h1>0DTE Bot</h1>
  <span class="chip" id="mode">...</span>
  <span class="chip" id="status">...</span>
  <span class="chip" id="clock">...</span>
  <span id="conn">connecting...</span>
</header>

<div class="tiles" id="tiles"></div>
<div class="panel"><h2>Open Position</h2><div id="position">-</div></div>
<div class="panel"><h2>Today's Trades</h2><div id="today">-</div></div>
<div class="panel"><h2>Daily History (newest first)</h2><div id="days">-</div></div>

<footer>Always-on dashboard - reads saved results from disk, works whether or
not the bot is currently trading. Read-only. Refreshes every 5s.</footer>

<script>
const $ = id => document.getElementById(id);
const fmt$ = v => (v>=0?"+$":"-$")+Math.abs(v).toFixed(2);
const cls$ = v => v>0?"pos":(v<0?"neg":"");
const esc = s => String(s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const tile = (k,v,s,c)=>`<div class="tile"><div class="k">${k}</div>`+
  `<div class="v num ${c||""}">${v}</div><div class="s num">${s||""}</div></div>`;

function render(d){
  $("mode").textContent = d.paper?"PAPER":"LIVE";
  $("mode").className = "chip"+(d.paper?"":" live");
  const b = d.bot;
  $("status").textContent = b.running?("RUNNING - live "+b.log_age_s+"s ago"):"NOT RUNNING";
  $("status").className = "chip "+(b.running?"run":"off");
  $("clock").textContent = d.ts_et+" ET";

  const si = d.since_inception, t = d.today;
  const wr = si.win_rate!=null?(si.win_rate*100).toFixed(0)+"%":"n/a";
  $("tiles").innerHTML =
    tile("Net P&L (all time)", fmt$(si.net_pnl),
         si.trades+" trades - "+wr+" win", cls$(si.net_pnl)) +
    tile("Trading days", si.trading_days||0,
         (si.green_days||0)+" green / "+(si.red_days||0)+" red") +
    tile("Best / worst day", fmt$(si.best_day||0),
         "worst "+fmt$(si.worst_day||0)) +
    tile("Today", fmt$(t.net_pnl), t.trades+" trades", cls$(t.net_pnl)) +
    tile("Fees (all time)", "$"+(si.fees||0).toFixed(2),
         "gross "+fmt$(si.gross_pnl||0)) +
    tile("Since", si.first_day||"-", "first recorded trade");

  const p = d.position;
  $("position").innerHTML = p
    ? `<div class="num">${esc(p.symbol||"")} &nbsp; ${esc((p.side||"").toUpperCase())} $${p.strike} `+
      `&nbsp; qty ${p.qty} &nbsp; entry $${Number(p.entry_price).toFixed(2)} `+
      `&nbsp; opened ${esc((p.entry_time||"").replace("T"," ").slice(0,19))}</div>`
    : `<span style="color:var(--text-2)">FLAT - no open position on record.</span>`;

  $("today").innerHTML = (t.trades_list&&t.trades_list.length)
    ? `<table><thead><tr><th>Out</th><th>Symbol</th><th>Side</th>`+
      `<th class="num">Entry</th><th class="num">Exit</th><th>Reason</th>`+
      `<th class="num">P&L</th></tr></thead><tbody>`+
      t.trades_list.map(x=>`<tr><td class="num">${esc(x.time)}</td>`+
        `<td>${esc(x.symbol)}</td><td>${esc(x.side)}</td>`+
        `<td class="num">$${x.entry.toFixed(2)}</td><td class="num">$${x.exit.toFixed(2)}</td>`+
        `<td>${esc(x.reason)}</td><td class="num ${cls$(x.pnl)}">${fmt$(x.pnl)}</td></tr>`).join("")+
      `</tbody></table>`
    : `<span style="color:var(--text-2)">No closed trades today.</span>`;

  $("days").innerHTML = (d.days&&d.days.filter(x=>x.trades>0).length)
    ? `<table><thead><tr><th>Date</th><th class="num">Trades</th>`+
      `<th class="num">W/L</th><th class="num">Net P&L</th></tr></thead><tbody>`+
      d.days.filter(x=>x.trades>0).map(x=>`<tr><td>${esc(x.date)}</td>`+
        `<td class="num">${x.trades}</td><td class="num">${x.wins}/${x.losses}</td>`+
        `<td class="num ${cls$(x.net_pnl)}">${fmt$(x.net_pnl)}</td></tr>`).join("")+
      `</tbody></table>`
    : `<span style="color:var(--text-2)">No trading days recorded yet.</span>`;
}

async function tick(){
  try {
    const r = await fetch("/api/state"); const d = await r.json();
    if (d.error){ $("conn").textContent="error: "+d.error; $("conn").className="down"; }
    else { $("conn").textContent="connected"; $("conn").className=""; render(d); }
  } catch(e){ $("conn").textContent="disconnected"; $("conn").className="down"; }
}
tick(); setInterval(tick, 5000);
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--once", action="store_true",
                    help="print a text summary and exit (no server)")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.once:
        print_summary()
        return

    host = args.host or config.DASHBOARD_HOST
    port = args.port if args.port is not None else config.DASHBOARD_PORT
    try:
        Dashboard(host, port).serve_forever()
    except OSError as e:
        logger.error("Dashboard could not bind %s:%d (%s). Is it already "
                     "running, or is the bot's own monitor on that port?",
                     host, port, e)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
