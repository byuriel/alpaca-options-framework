# Running Hands-Off on Windows

Goal: the bot launches itself each weekday morning, trades the session, closes
out and reconciles itself after the bell, and you never touch a terminal. You
watch a browser page if and when you feel like it.

The mental model that makes this correct: **one process = one session.** The
bot hard-exits at the 15:25 ET time stop by design. A supervisor
(`run_session.py`) launched by Windows Task Scheduler runs that one session
each trading day and then runs the daily audit tools. Holidays and weekends
are skipped automatically — the bot's own NYSE calendar decides, so the
schedule is a blind "every weekday" trigger.

---

## One-time setup (~15 minutes)

### 1. Install Python
Download Python 3.9+ from [python.org](https://www.python.org/downloads/windows/).
**Check "Add python.exe to PATH"** in the installer.

### 2. Get the code and dependencies
Open **PowerShell** in the repo folder:
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```
Using the `.venv` is recommended — the scheduler installer auto-detects it.

### 3. Credentials and settings
```powershell
Copy-Item .env.example .env
notepad .env
```
Fill in your Alpaca **paper** key + secret; leave `ALPACA_PAPER=true`. Enable
options trading on your Alpaca paper account first (Account → Configure →
Options, Level 2 — buying calls/puts). If you have a Slack/Discord webhook,
paste it into `ALERT_WEBHOOK_URL` so the bot can reach you when a kill switch
fires.

### 4. Test once, by hand
Confirm it boots and reaches "waiting for 09:30 ET" (run this during or before
market hours on a weekday):
```powershell
python run_session.py
```
On a weekend/holiday it will log "not a trading day" and exit in a second —
that is correct. Watch a live session at **http://127.0.0.1:8080**.

### 5. Install the scheduler
Open PowerShell **as Administrator** (needed for "run whether logged on or
not"), then:
```powershell
.\windows\install_scheduler.ps1
```
That's it. It registers a task that fires each weekday at the **local-clock
equivalent of 09:00 ET** (computed for you — correct in any time zone), runs
the supervisor, and self-skips non-trading days.

Fire it once immediately to prove the whole chain end-to-end:
```powershell
Start-ScheduledTask -TaskName "AlpacaOptionsBot"
```

---

## What happens every trading day, untouched

1. ~09:00 ET the task starts `run_session.py`.
2. The supervisor confirms it's a trading day and launches the bot with no
   console (keyboard controls off, web monitor on).
3. The bot subscribes at the open, trades 09:45–14:30, force-closes at 15:25,
   verifies it's flat, and hard-exits.
4. The supervisor then runs **`reconcile.py`** (local record vs broker) and
   **`feed_monitor.py`** (coverage/quality), writing verdicts to
   `logs\supervisor_YYYY-MM-DD.log`.
5. If reconciliation finds a discrepancy, the bot **locks its entry gate at
   the next start** until you review and run `python reconcile.py --clear`.

Everything lands in `logs\` (bot, supervisor) and `recordings\`.

---

## Watching and controlling

- **Live view:** http://127.0.0.1:8080 while a session runs (read-only).
- **Alerts:** configure `ALERT_WEBHOOK_URL` / SMTP in `.env` — kill switches,
  exit failures, reconciliation mismatches, and restarts reach your phone.
- **Track record:** `python trade_stats.py logs\` any time (it refuses to make
  a claim until the sample is large enough — that's intended).
- **Stop it running each day:** `.\windows\uninstall_scheduler.ps1`
  (or disable the task in Task Scheduler).
- **Force-flatten a live session now:** Task Scheduler → the task → **End**,
  or let the 15:25 time stop handle it. Positions left open are recovered on
  the next start.

---

## Notes and gotchas

- **Machine must be on (or set to wake).** The task uses *WakeToRun* and
  *StartWhenAvailable*, so a sleeping PC wakes for it and a missed trigger runs
  late — but a powered-off PC obviously can't trade. A cheap always-on mini-PC
  or a Windows VPS is the reliable home for this. If fired too late in the day,
  the supervisor skips the session rather than opening a pointless afternoon.
- **Free data plan.** `STRIKE_ALTS=6` keeps you under Alpaca's 30-symbol
  WebSocket cap. Run `feed_monitor.py` after day one and confirm coverage is
  ~100% with no dark symbols. Widen back to 10 on the paid OPRA feed.
- **Paper first, always.** `ALPACA_PAPER=true` until you have weeks of clean,
  reconciled, statistically-gated results. Live requires explicitly setting
  `ALPACA_PAPER=false`.
- **Watchdog restarts** under the supervisor are clean exit-and-relaunch (not
  `os.execl`), which is the correct behavior on Windows; three restarts within
  an hour trips the storm brake, which flattens and halts until you clear it.
