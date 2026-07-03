#!/usr/bin/env python3
"""
SPY 0DTE bot — KPI dashboard generator.

Reads logs/trades_*.csv (closed trades) and logs/bot_*.log (ghost closes,
ORB shadow filter decisions) and emits a self-contained dark-theme HTML
report to gh_dashboard/index.html, mirroring the Strat v60 dashboard.

Usage:
    python3 kpi_dashboard.py [--days 30] [--out gh_dashboard/index.html]

Only generated HTML ever goes into gh_dashboard/ — never config or keys.
"""

import argparse
import csv
import datetime
import glob
import html
import json
import os
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR  = os.path.join(BASE_DIR, "logs")

GHOST_RE  = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) [\d:,]+ \[WARNING\] main: GHOST CLOSED: (\S+) "
    r"fill=([\d.]+) pnl=\$(-?[\d.]+)"
)
SHADOW_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) ([\d:]{8})[\d,]* \[INFO\] orb_filter: "
    r"ORB_SHADOW \| (BLOCK|ALLOW): (\S+) side=(\w+) bias=(\S+)"
)


def is_artifact(r) -> bool:
    """
    Bug-artifact rows: 'time_stop' exits held under 60 seconds are force-closes
    of positions recovered after a restart (synthetic entry time, Alpaca
    day-average cost basis) — e.g. the Jun 3/4 watchdog-cascade rows and the
    May 27 recovered 12-lot. A genuine time-stop close holds for minutes.
    """
    hold = (r["exit_dt"] - r["entry_dt"]).total_seconds()
    return r["reason"] == "time_stop" and hold < 60


def load_trades(days: int):
    """Load closed trades from trades_*.csv, newest `days` trading days.
    Returns (clean_trades, artifact_trades)."""
    rows = []
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "trades_*.csv"))):
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                if not r.get("realized_pnl"):
                    continue
                try:
                    r["realized_pnl"] = float(r["realized_pnl"])
                    r["entry_dt"] = datetime.datetime.fromisoformat(r["entry_time"])
                    r["exit_dt"]  = datetime.datetime.fromisoformat(r["exit_time"])
                except (ValueError, KeyError):
                    continue
                rows.append(r)
    cutoff = (datetime.date.today() - datetime.timedelta(days=days)).isoformat()
    rows   = [r for r in rows if r["date"] >= cutoff]
    return ([r for r in rows if not is_artifact(r)],
            [r for r in rows if is_artifact(r)])


def load_ghosts(dates: set):
    """Parse GHOST CLOSED events from bot logs → [{date, symbol, pnl}]."""
    ghosts = []
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "bot_*.log"))):
        day = os.path.basename(path)[4:14]
        if day not in dates:
            continue
        try:
            with open(path, errors="replace") as f:
                for line in f:
                    m = GHOST_RE.match(line)
                    if m:
                        ghosts.append({"date": m.group(1), "symbol": m.group(2),
                                       "pnl": float(m.group(4))})
        except OSError:
            continue
    return ghosts


def load_shadow(dates: set):
    """Parse ORB_SHADOW decisions → [{date, time, decision, symbol, side, bias}]."""
    out = []
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "bot_*.log"))):
        day = os.path.basename(path)[4:14]
        if day not in dates:
            continue
        try:
            with open(path, errors="replace") as f:
                for line in f:
                    m = SHADOW_RE.match(line)
                    if m:
                        out.append({
                            "date": m.group(1), "time": m.group(2),
                            "decision": m.group(3), "symbol": m.group(4),
                            "side": m.group(5), "bias": m.group(6),
                        })
        except OSError:
            continue
    return out


def match_shadow(trade, shadow):
    """Latest shadow decision for this trade's symbol within 120s before entry."""
    entry = trade["entry_dt"].replace(tzinfo=None)
    best = None
    for s in shadow:
        if s["date"] != trade["date"] or s["symbol"] != trade["symbol"]:
            continue
        st = datetime.datetime.strptime(f"{s['date']} {s['time']}", "%Y-%m-%d %H:%M:%S")
        delta = (entry - st).total_seconds()
        if 0 <= delta <= 120 and (best is None or st > best[0]):
            best = (st, s)
    return best[1] if best else None


def fmt_usd(v, sign=True):
    s = f"{abs(v):,.2f}"
    if sign:
        return f"+${s}" if v >= 0 else f"-${s}"
    return f"${s}"


def build(days: int, out_path: str):
    trades, artifacts = load_trades(days)
    if not trades:
        raise SystemExit("No trades found.")
    dates  = sorted({r["date"] for r in trades})
    ghosts = load_ghosts(set(dates))
    shadow = load_shadow(set(dates))

    # ── Core KPIs ────────────────────────────────────────────────────────────
    pnl_total   = sum(r["realized_pnl"] for r in trades)
    wins        = [r for r in trades if r["realized_pnl"] >= 0]
    losses      = [r for r in trades if r["realized_pnl"] < 0]
    gross_win   = sum(r["realized_pnl"] for r in wins)
    gross_loss  = -sum(r["realized_pnl"] for r in losses)
    win_rate    = len(wins) / len(trades) * 100
    pf          = gross_win / gross_loss if gross_loss else float("inf")
    ev          = pnl_total / len(trades)
    avg_win     = gross_win / len(wins) if wins else 0
    avg_loss    = -gross_loss / len(losses) if losses else 0
    ghost_pnl   = sum(g["pnl"] for g in ghosts)
    actual_pnl  = pnl_total + ghost_pnl
    holds       = [(r["exit_dt"] - r["entry_dt"]).total_seconds() for r in trades]
    avg_hold    = sum(holds) / len(holds) / 60

    # ── Daily aggregates ─────────────────────────────────────────────────────
    daily = {d: {"pnl": 0.0, "n": 0, "wins": 0, "ghost": 0.0} for d in dates}
    for r in trades:
        d = daily[r["date"]]
        d["pnl"] += r["realized_pnl"]
        d["n"]   += 1
        d["wins"] += r["realized_pnl"] >= 0
    for g in ghosts:
        if g["date"] in daily:
            daily[g["date"]]["ghost"] += g["pnl"]

    cum, cum_curve = 0.0, []
    for d in dates:
        cum += daily[d]["pnl"] + daily[d]["ghost"]
        cum_curve.append(round(cum, 2))

    green_days = sum(1 for d in dates if daily[d]["pnl"] + daily[d]["ghost"] >= 0)

    # ── Exit reason / side breakdowns ────────────────────────────────────────
    def group(key_fn):
        g = {}
        for r in trades:
            k = key_fn(r)
            e = g.setdefault(k, {"n": 0, "pnl": 0.0, "wins": 0})
            e["n"] += 1
            e["pnl"] += r["realized_pnl"]
            e["wins"] += r["realized_pnl"] >= 0
        return g

    by_reason = group(lambda r: r["reason"])
    by_side   = group(lambda r: r["side"])
    by_hour   = group(lambda r: r["entry_dt"].astimezone().hour)

    # ── ORB shadow filter panel ──────────────────────────────────────────────
    shadow_days   = sorted({s["date"] for s in shadow})
    blocked       = []
    shadow_trades = [t for t in trades if t["date"] in shadow_days]
    for t in shadow_trades:
        m = match_shadow(t, shadow)
        if m and m["decision"] == "BLOCK":
            blocked.append({**t, "bias": m["bias"]})
    blocked_pnl   = sum(b["realized_pnl"] for b in blocked)
    shadow_actual = sum(t["realized_pnl"] for t in shadow_trades)
    shadow_filtered = shadow_actual - blocked_pnl
    blocked_wins  = [b for b in blocked if b["realized_pnl"] >= 0]
    blocked_loss  = [b for b in blocked if b["realized_pnl"] < 0]

    # ── Chart data ───────────────────────────────────────────────────────────
    chart = {
        "dates":     dates,
        "daily_pnl": [round(daily[d]["pnl"] + daily[d]["ghost"], 2) for d in dates],
        "cum":       cum_curve,
        "reasons":   {k: round(v["pnl"], 2) for k, v in sorted(by_reason.items())},
        "hours":     {f"{k:02d}:00": round(v["pnl"], 2) for k, v in sorted(by_hour.items())},
    }

    # ── HTML ─────────────────────────────────────────────────────────────────
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    if artifacts:
        art_pnl = sum(a["realized_pnl"] for a in artifacts)
        art_list = ", ".join(
            f"{a['date']} {fmt_usd(a['realized_pnl'])}" for a in artifacts
        )
        artifact_note = (
            f"Excluded {len(artifacts)} bug-artifact rows (restart-recovery "
            f"force-closes, hold &lt;60s), net {fmt_usd(art_pnl)}: {art_list}."
        )
    else:
        artifact_note = "No bug-artifact rows in window."

    def card(title, val, sub, cls=""):
        return f"""<div class="col"><div class="card px-2 py-2 text-center">
<div class="card-title mb-0">{title}</div>
<div class="stat-val fs-5 fw-bold {cls}">{val}</div>
<div class="stat-sub">{sub}</div></div></div>"""

    pnl_cls = "text-success" if actual_pnl >= 0 else "text-danger"
    cards = "".join([
        card("Net P&L (actual)", fmt_usd(actual_pnl), f"{fmt_usd(pnl_total)} booked, {fmt_usd(ghost_pnl)} ghosts", pnl_cls),
        card("Trades", str(len(trades)), f"{len(wins)} W / {len(losses)} L"),
        card("Win Rate", f"{win_rate:.1f}%", f"EV {fmt_usd(ev)}/trade",
             "text-success" if win_rate >= 50 else "text-warning"),
        card("Profit Factor", f"{pf:.2f}", f"avg W {fmt_usd(avg_win)} / L {fmt_usd(avg_loss)}",
             "text-success" if pf >= 1.5 else "text-warning"),
        card("Green Days", f"{green_days}/{len(dates)}", f"avg hold {avg_hold:.1f} min"),
        card("Ghost Events", str(len(ghosts)), f"net {fmt_usd(ghost_pnl)}",
             "text-warning" if ghosts else ""),
    ])

    reason_rows = "".join(
        f"<tr><td>{html.escape(k)}</td><td>{v['n']}</td>"
        f"<td>{v['wins']/v['n']*100:.0f}%</td>"
        f"<td class=\"{'text-success' if v['pnl']>=0 else 'text-danger'}\">{fmt_usd(v['pnl'])}</td></tr>"
        for k, v in sorted(by_reason.items(), key=lambda x: -x[1]["pnl"])
    )
    side_rows = "".join(
        f"<tr><td>{html.escape(k)}</td><td>{v['n']}</td>"
        f"<td>{v['wins']/v['n']*100:.0f}%</td>"
        f"<td class=\"{'text-success' if v['pnl']>=0 else 'text-danger'}\">{fmt_usd(v['pnl'])}</td></tr>"
        for k, v in sorted(by_side.items())
    )
    day_rows = "".join(
        f"<tr><td>{d}</td><td>{daily[d]['n']}</td>"
        f"<td>{daily[d]['wins']}/{daily[d]['n']}</td>"
        f"<td class=\"{'text-success' if daily[d]['pnl']>=0 else 'text-danger'}\">{fmt_usd(daily[d]['pnl'])}</td>"
        f"<td>{fmt_usd(daily[d]['ghost']) if daily[d]['ghost'] else '—'}</td>"
        f"<td class=\"{'text-success' if daily[d]['pnl']+daily[d]['ghost']>=0 else 'text-danger'}\">"
        f"{fmt_usd(daily[d]['pnl'] + daily[d]['ghost'])}</td></tr>"
        for d in reversed(dates)
    )
    block_rows = "".join(
        f"<tr><td>{b['date']} {b['entry_dt'].strftime('%H:%M')}</td>"
        f"<td>{html.escape(b['side'])} {float(b['strike']):.0f}</td>"
        f"<td>{html.escape(b['bias'])}</td><td>{html.escape(b['reason'])}</td>"
        f"<td class=\"{'text-success' if b['realized_pnl']>=0 else 'text-danger'}\">{fmt_usd(b['realized_pnl'])}</td></tr>"
        for b in blocked
    ) or "<tr><td colspan=5 class='text-muted'>No blocked trades yet</td></tr>"

    orb_improve = shadow_filtered - shadow_actual
    orb_cls     = "text-success" if orb_improve >= 0 else "text-danger"

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SPY 0DTE — KPI Dashboard</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {{ --bs-body-bg:#0f172a; --bs-body-color:#e2e8f0; --card-bg:#1e293b;
           --border-color:#334155; --accent:#22d3ee; }}
  body {{ background:var(--bs-body-bg); color:var(--bs-body-color); font-family:'Segoe UI',sans-serif; }}
  .card {{ background:var(--card-bg); border:1px solid var(--border-color); border-radius:12px; }}
  .card-title {{ color:var(--accent); font-size:0.75rem; text-transform:uppercase;
                 letter-spacing:1px; font-weight:600; }}
  .stat-val {{ font-size:1.4rem; font-weight:700; }}
  .stat-sub {{ font-size:0.75rem; color:#94a3b8; }}
  .section-title {{ color:var(--accent); font-size:0.8rem; text-transform:uppercase;
                    letter-spacing:2px; font-weight:600; border-bottom:1px solid var(--border-color);
                    padding-bottom:8px; margin-bottom:16px; }}
  .table {{ color:var(--bs-body-color); font-size:0.8rem; }}
  .table th {{ color:var(--accent); font-weight:600; font-size:0.72rem; text-transform:uppercase;
               letter-spacing:0.5px; border-color:var(--border-color); background:#0f172a; }}
  .table td {{ border-color:var(--border-color); vertical-align:middle; }}
  .table tbody tr:hover {{ background:rgba(34,211,238,0.05); }}
  ::-webkit-scrollbar {{ width:6px; height:6px; }}
  ::-webkit-scrollbar-track {{ background:#1e293b; }}
  ::-webkit-scrollbar-thumb {{ background:#334155; border-radius:3px; }}
</style>
</head>
<body>
<div class="container-fluid px-4 pt-4 pb-2">
  <div class="d-flex justify-content-between align-items-center mb-3">
    <div>
      <h4 class="mb-0 fw-bold" style="color:#22d3ee;">⚡ SPY 0DTE — KPI Dashboard
        <span style="color:#94a3b8;font-size:0.85rem;font-weight:400;">(paper, gamma-explosion strategy)</span></h4>
      <small class="text-muted">{dates[0]} → {dates[-1]} &nbsp;|&nbsp; {len(dates)} trading days
        &nbsp;|&nbsp; Generated {now}</small>
    </div>
  </div>

  <div class="row g-2 mb-4 flex-nowrap">{cards}</div>

  <div class="row g-3 mb-4">
    <div class="col-md-8">
      <div class="card p-3">
        <div class="section-title">Equity Curve (cumulative, incl. ghosts)</div>
        <canvas id="eq" height="90"></canvas>
      </div>
    </div>
    <div class="col-md-4">
      <div class="card p-3">
        <div class="section-title">Daily P&L</div>
        <canvas id="daily" height="185"></canvas>
      </div>
    </div>
  </div>

  <div class="row g-3 mb-4">
    <div class="col-md-4">
      <div class="card p-3">
        <div class="section-title">P&L by Exit Reason</div>
        <table class="table table-sm mb-0"><thead>
          <tr><th>Reason</th><th>N</th><th>Win%</th><th>P&L</th></tr></thead>
          <tbody>{reason_rows}</tbody></table>
      </div>
    </div>
    <div class="col-md-4">
      <div class="card p-3">
        <div class="section-title">P&L by Side</div>
        <table class="table table-sm mb-0"><thead>
          <tr><th>Side</th><th>N</th><th>Win%</th><th>P&L</th></tr></thead>
          <tbody>{side_rows}</tbody></table>
      </div>
    </div>
    <div class="col-md-4">
      <div class="card p-3">
        <div class="section-title">P&L by Entry Hour</div>
        <canvas id="hours" height="150"></canvas>
      </div>
    </div>
  </div>

  <div class="row g-3 mb-4">
    <div class="col-md-5">
      <div class="card p-3">
        <div class="section-title">ORB Shadow Filter (observe-only, live since 2026-06-30)</div>
        <div class="d-flex gap-4 mb-2">
          <div><div class="card-title">Actual</div><div class="stat-val fs-6">{fmt_usd(shadow_actual)}</div></div>
          <div><div class="card-title">If Filtered</div><div class="stat-val fs-6">{fmt_usd(shadow_filtered)}</div></div>
          <div><div class="card-title">Filter Effect</div><div class="stat-val fs-6 {orb_cls}">{fmt_usd(orb_improve)}</div></div>
        </div>
        <div class="stat-sub mb-2">Blocked: {len(blocked_loss)} losses ({fmt_usd(sum(b['realized_pnl'] for b in blocked_loss))}),
          {len(blocked_wins)} wins ({fmt_usd(sum(b['realized_pnl'] for b in blocked_wins))})
          over {len(shadow_days)} shadow days</div>
        <table class="table table-sm mb-0"><thead>
          <tr><th>Entry</th><th>Trade</th><th>Bias</th><th>Exit</th><th>P&L</th></tr></thead>
          <tbody>{block_rows}</tbody></table>
      </div>
    </div>
    <div class="col-md-7">
      <div class="card p-3">
        <div class="section-title">Daily Detail</div>
        <div style="max-height:320px;overflow-y:auto;">
        <table class="table table-sm mb-0"><thead>
          <tr><th>Date</th><th>Trades</th><th>W/N</th><th>Booked</th><th>Ghosts</th><th>Actual</th></tr></thead>
          <tbody>{day_rows}</tbody></table>
        </div>
      </div>
    </div>
  </div>

  <div class="text-muted pb-4" style="font-size:0.7rem;">Paper trading on Alpaca.
    Ghost events = duplicate fills recovered by the sweeper; included in actual P&L.</div>
</div>

<script>
const D = {json.dumps(chart)};
const gridCfg = {{ color:'#334155' }};
const tickCfg = {{ color:'#94a3b8', font:{{size:10}} }};
new Chart(document.getElementById('eq'), {{
  type:'line',
  data:{{ labels:D.dates, datasets:[{{ data:D.cum, borderColor:'#22d3ee',
    backgroundColor:'rgba(34,211,238,0.08)', fill:true, tension:0.25, pointRadius:3 }}] }},
  options:{{ plugins:{{legend:{{display:false}}}},
    scales:{{ x:{{grid:gridCfg,ticks:tickCfg}}, y:{{grid:gridCfg,ticks:tickCfg}} }} }}
}});
new Chart(document.getElementById('daily'), {{
  type:'bar',
  data:{{ labels:D.dates, datasets:[{{ data:D.daily_pnl,
    backgroundColor:D.daily_pnl.map(v=>v>=0?'rgba(34,197,94,0.7)':'rgba(239,68,68,0.7)') }}] }},
  options:{{ plugins:{{legend:{{display:false}}}},
    scales:{{ x:{{grid:gridCfg,ticks:tickCfg}}, y:{{grid:gridCfg,ticks:tickCfg}} }} }}
}});
new Chart(document.getElementById('hours'), {{
  type:'bar',
  data:{{ labels:Object.keys(D.hours), datasets:[{{ data:Object.values(D.hours),
    backgroundColor:Object.values(D.hours).map(v=>v>=0?'rgba(34,197,94,0.7)':'rgba(239,68,68,0.7)') }}] }},
  options:{{ plugins:{{legend:{{display:false}}}},
    scales:{{ x:{{grid:gridCfg,ticks:tickCfg}}, y:{{grid:gridCfg,ticks:tickCfg}} }} }}
}});
</script>
</body>
</html>"""

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        f.write(html_doc)
    print(f"Dashboard written: {out_path}")
    print(f"  {len(dates)} days, {len(trades)} trades, booked {fmt_usd(pnl_total)}, "
          f"ghosts {fmt_usd(ghost_pnl)}, actual {fmt_usd(actual_pnl)}")
    print(f"  ORB shadow: {len(blocked)} blocks over {len(shadow_days)} days, "
          f"effect {fmt_usd(orb_improve)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", default=os.path.join(BASE_DIR, "gh_dashboard", "index.html"))
    args = ap.parse_args()
    build(args.days, args.out)
