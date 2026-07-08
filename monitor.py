"""
Live web monitor - a read-only status page served from inside the bot.

Open http://127.0.0.1:8080 in any browser while the bot runs: current
position with ticking P&L, momentum state, risk gates, kill-switch ages,
and today's trades, refreshing every 2 seconds.

Design constraints, in order:
  1. READ-ONLY BY CONSTRUCTION. Two GET endpoints (the page and a JSON
     snapshot); every other method/path is rejected. There is no code path
     from this module into trading state - it holds a snapshot *function*
     and renders whatever that returns.
  2. NEVER touches the trading path. The server runs on its own daemon
     threads (ThreadingHTTPServer); snapshot errors return an error payload
     instead of raising; a busy port disables the monitor with a warning
     instead of stopping the bot.
  3. LOCALHOST BY DEFAULT. Binds 127.0.0.1 unless MONITOR_HOST says
     otherwise - a monitoring page on a trading process is not something to
     expose to a network casually, read-only or not. MONITOR_PORT=0 disables.
  4. Stdlib only, self-contained HTML (no CDN), same rule as everything else.
"""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class MonitorServer:
    def __init__(self, snapshot_fn: Callable[[], dict],
                 host: str = "127.0.0.1", port: int = 8080):
        self._snapshot_fn = snapshot_fn
        self.host = host
        self.port = port
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        """Start serving. Returns False (and logs) instead of raising -
        a busy port must never stop the bot."""
        if self.port == 0:
            return False
        snapshot_fn = self._snapshot_fn

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/api/state":
                    try:
                        payload = snapshot_fn()
                    except Exception as e:   # snapshot must never 500 the page
                        payload = {"error": f"snapshot failed: {e}"}
                    body = json.dumps(payload, default=str).encode()
                    self._respond(200, "application/json", body)
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
                # covers non-GET methods (501 by default) - normalize to 405
                if code == 501:
                    code, message = 405, "read-only monitor"
                super().send_error(code, message, explain)

            def log_message(self, fmt, *args):
                pass   # no per-request noise in the bot's log

        try:
            self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
            self._httpd.daemon_threads = True
        except OSError as e:
            logger.warning("Monitor disabled - could not bind %s:%d (%s)",
                           self.host, self.port, e)
            return False
        # port 0 -> ephemeral; report the real one
        self.port = self._httpd.server_port
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True, name="monitor")
        self._thread.start()
        logger.info("Monitor live: http://%s:%d  (read-only)", self.host, self.port)
        return True

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


# -- The page -------------------------------------------------------------------
# Chart-free ops monitor: stat tiles + tables, committed dark theme (this is a
# trading-desk screen, not a document). Status colors are the validated
# dark-surface set and never carry meaning alone - every state is also text.

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>0DTE Bot Monitor</title>
<style>
  :root {
    --surface: #1a1a19; --panel: #232322; --line: #3a3a38;
    --text: #ffffff; --text-2: #c3c2b7; --text-3: #8a897f;
    --good: #0ca30c; --warn: #fab219; --crit: #d03b3b; --accent: #3987e5;
  }
  * { box-sizing: border-box; margin: 0; }
  body { background: var(--surface); color: var(--text);
         font: 14px/1.45 -apple-system, "Segoe UI", Roboto, sans-serif;
         padding: 16px; max-width: 1080px; margin: 0 auto; }
  .num { font-variant-numeric: tabular-nums; }
  header { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap;
           margin-bottom: 14px; }
  h1 { font-size: 16px; font-weight: 650; letter-spacing: .2px; }
  .chip { font-size: 11px; padding: 2px 8px; border-radius: 999px;
          border: 1px solid var(--line); color: var(--text-2); }
  .chip.live { border-color: var(--crit); color: var(--crit); font-weight: 700; }
  #conn { margin-left: auto; font-size: 12px; color: var(--text-3); }
  #conn.down { color: var(--crit); font-weight: 700; }

  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
           gap: 8px; margin-bottom: 14px; }
  .tile { background: var(--panel); border: 1px solid var(--line);
          border-radius: 10px; padding: 10px 12px; }
  .tile .k { font-size: 11px; text-transform: uppercase; letter-spacing: .8px;
             color: var(--text-3); margin-bottom: 4px; }
  .tile .v { font-size: 20px; font-weight: 700; }
  .tile .s { font-size: 11.5px; color: var(--text-2); margin-top: 2px; }

  .panel { background: var(--panel); border: 1px solid var(--line);
           border-radius: 10px; padding: 12px 14px; margin-bottom: 14px; }
  .panel h2 { font-size: 11px; text-transform: uppercase; letter-spacing: 1px;
              color: var(--text-3); margin-bottom: 8px; }
  .kv { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
        gap: 6px 18px; }
  .kv div { font-size: 13px; color: var(--text-2); }
  .kv b { color: var(--text); font-weight: 600; }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: var(--text-3); font-weight: 600; font-size: 11px;
       text-transform: uppercase; letter-spacing: .6px; padding: 4px 8px;
       border-bottom: 1px solid var(--line); }
  td { padding: 5px 8px; border-bottom: 1px solid var(--line); color: var(--text-2); }
  td.num, th.num { text-align: right; }
  .pos { color: var(--good); } .neg { color: var(--crit); } .warnc { color: var(--warn); }
  footer { color: var(--text-3); font-size: 11px; margin-top: 10px; }
</style>
</head>
<body>
<header>
  <h1>0DTE Bot</h1>
  <span class="chip" id="mode">...</span>
  <span class="chip" id="feeds">...</span>
  <span class="chip" id="clock">...</span>
  <span id="conn">connecting...</span>
</header>

<div class="tiles" id="tiles"></div>

<div class="panel"><h2>Position</h2><div id="position">-</div></div>
<div class="panel"><h2>Safety</h2><div class="kv" id="safety"></div></div>
<div class="panel"><h2>Today's Trades</h2><div id="trades">-</div></div>

<footer>Read-only monitor - it observes the bot and cannot act on it.
Refreshes every 2s.</footer>

<script>
const $ = id => document.getElementById(id);
const fmt$ = v => (v >= 0 ? "+$" : "-$") + Math.abs(v).toFixed(2);
const cls$ = v => v > 0 ? "pos" : (v < 0 ? "neg" : "");
const esc = s => String(s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));

function tile(k, v, s, cls) {
  return `<div class="tile"><div class="k">${k}</div>` +
         `<div class="v num ${cls||""}">${v}</div>` +
         `<div class="s num">${s||""}</div></div>`;
}

function render(d) {
  $("mode").textContent = d.paper ? "PAPER" : "LIVE";
  $("mode").className = "chip" + (d.paper ? "" : " live");
  $("feeds").textContent = d.feeds.stock + " / " + d.feeds.option;
  $("clock").textContent = d.ts_et + " ET";

  const m = d.spy, r = d.risk;
  const dirTxt = {bull: "^ BULL", bear: "v BEAR", neutral: "- NEUTRAL"}[m.direction] || m.direction;
  const gate = r.locked ? `LOCKED` : `open`;
  $("tiles").innerHTML =
    tile("SPY", m.price ? "$" + m.price.toFixed(2) : "-", dirTxt) +
    tile("Daily P&L", fmt$(r.daily_pnl), r.trades_today + " trades", cls$(r.daily_pnl)) +
    tile("Week P&L", fmt$(r.week_pnl), "limit -$" + d.limits.weekly.toFixed(0), cls$(r.week_pnl)) +
    tile("Entry gate", gate, r.locked ? esc(r.lock_reason) :
         (r.cooldown_bars ? "cooldown " + r.cooldown_bars + " bars" : "ready"),
         r.locked ? "neg" : "pos") +
    tile("Momentum", "EMA " + m.ema5.toFixed(2) + "/" + m.ema20.toFixed(2),
         "VWAP " + m.vwap.toFixed(2) + " - ROC " + m.roc5.toFixed(4) +
         " - atr5 " + m.atr5.toFixed(3)) +
    tile("Session", d.session.market_open ? "OPEN" : "pre-open",
         "entries " + d.session.entry_start + "-" + d.session.entry_end +
         " - stop " + d.session.time_stop);

  const p = d.position;
  if (p) {
    $("position").innerHTML = `<div class="kv">
      <div>Symbol <b>${esc(p.symbol)}</b></div>
      <div>Side <b>${esc(p.side.toUpperCase())} $${p.strike}</b></div>
      <div>Qty <b class="num">${p.qty}</b></div>
      <div>Entry <b class="num">$${p.entry.toFixed(2)}</b></div>
      <div>Mid <b class="num">$${p.mid.toFixed(2)} (${p.pct >= 0 ? "+" : ""}${p.pct.toFixed(0)}%)</b></div>
      <div>Unrealized <b class="num ${cls$(p.unreal_pnl)}">${fmt$(p.unreal_pnl)}</b></div>
      <div>TP / Stop <b class="num">$${p.tp.toFixed(2)} / $${p.stop.toFixed(2)}</b></div>
      <div>Trail <b class="num">${p.trail_armed ? "$" + p.trail_stop.toFixed(2) + " (peak $" + p.peak.toFixed(2) + ")" : "arms @ $" + p.trail_arms_at.toFixed(2)}</b></div>
      <div>SPY stop <b class="num">${p.spy_stop ? "$" + p.spy_stop.toFixed(2) : "OFF"}</b></div>
      <div>In trade <b class="num">${Math.floor(p.age_s/60)}m ${String(Math.floor(p.age_s%60)).padStart(2,"0")}s</b></div>
      <div>Min/Max uP&L <b class="num">${fmt$(p.min_unreal)} / ${fmt$(p.max_unreal)}</b></div>
    </div>`;
  } else {
    $("position").innerHTML = `<span style="color:var(--text-2)">FLAT - no open position</span>`;
  }

  const s = d.safety;
  const age = (v, warnAt) => v == null ? "-"
      : `<b class="num ${v > warnAt ? "warnc" : ""}">${v.toFixed(0)}s${v > warnAt ? " [WARN]" : ""}</b>`;
  $("safety").innerHTML =
    `<div>Quote age ${age(s.quote_age_s, d.limits.stale_quote / 2)}</div>` +
    `<div>Bar age ${age(s.bar_age_s, 120)}</div>` +
    `<div>Entry pending <b>${s.entry_pending ? "YES" : "no"}</b></div>` +
    `<div>Exit pending <b>${s.exit_pending ? "YES" : "no"}</b></div>` +
    `<div>Event blackout <b class="${s.blackout ? "warnc" : ""}">${s.blackout ? "[WARN] " + esc(s.blackout) : "none"}</b></div>` +
    `<div>Events today <b>${d.session.events.length ? esc(d.session.events.join(", ")) : "none"}</b></div>` +
    `<div>Subscribed <b class="num">${d.subs} symbols</b></div>` +
    `<div>Recorder <b>${d.recorder.active ? (d.recorder.dropped ? "[WARN] dropped " + d.recorder.dropped : "on") : "off"}</b></div>`;

  if (d.trades && d.trades.length) {
    $("trades").innerHTML = `<table><thead><tr>
      <th>Out</th><th>Symbol</th><th>Side</th><th class="num">Qty</th>
      <th class="num">Entry</th><th class="num">Exit</th><th>Reason</th>
      <th class="num">P&L</th></tr></thead><tbody>` +
      d.trades.map(t => `<tr>
        <td class="num">${esc(t.time)}</td><td>${esc(t.symbol)}</td>
        <td>${esc(t.side)}</td><td class="num">${t.qty}</td>
        <td class="num">$${t.entry.toFixed(2)}</td>
        <td class="num">$${t.exit.toFixed(2)}</td><td>${esc(t.reason)}</td>
        <td class="num ${cls$(t.pnl)}">${fmt$(t.pnl)}</td></tr>`).join("") +
      `</tbody></table>`;
  } else {
    $("trades").innerHTML = `<span style="color:var(--text-2)">No closed trades yet today.</span>`;
  }
}

async function tick() {
  try {
    const res = await fetch("/api/state");
    const d = await res.json();
    if (d.error) { $("conn").textContent = "snapshot error: " + d.error; $("conn").className = "down"; }
    else { $("conn").textContent = "live"; $("conn").className = ""; render(d); }
  } catch (e) {
    $("conn").textContent = "disconnected - is the bot running?";
    $("conn").className = "down";
  }
}
tick();
setInterval(tick, 2000);
</script>
</body>
</html>
"""
