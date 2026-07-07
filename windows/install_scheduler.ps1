<#
.SYNOPSIS
  Register the Windows Task Scheduler job that runs the 0DTE bot hands-off,
  once each weekday morning, whether you are logged in or not.

.DESCRIPTION
  Creates a scheduled task that launches run_session.py (the daily supervisor)
  each weekday. The supervisor handles the trading day / holiday gate, runs the
  bot for the session, and runs reconcile + feed-monitor after the close — so
  the only thing this task does is fire once a morning.

  The trigger time is computed as the LOCAL-clock equivalent of the requested
  Eastern time, so it is correct no matter what time zone this machine is set
  to. (US zones all observe DST together, so a fixed local time tracks ET
  year-round; the supervisor's own ET gate tolerates any residual drift.)

.PARAMETER AtET
  Pre-market Eastern time to fire, "HH:mm". Default 09:00 (gives startup time
  to fetch the ATR baseline, pre-seed indicators, and prime the chain before
  the 09:30 open).

.PARAMETER Python
  Full path to python.exe. If omitted, a .venv in the repo is preferred, else
  the python on PATH.

.PARAMETER TaskName
  Scheduled task name. Default "AlpacaOptionsBot".

.EXAMPLE
  # From an elevated PowerShell (needed for "run whether logged on or not"):
  .\windows\install_scheduler.ps1
  .\windows\install_scheduler.ps1 -AtET "08:55" -Python "C:\repo\.venv\Scripts\python.exe"
#>
[CmdletBinding()]
param(
  [string]$AtET     = "09:00",
  [string]$Python   = "",
  [string]$TaskName = "AlpacaOptionsBot"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot     # repo root (this script lives in windows\)

# ── Resolve the Python interpreter ────────────────────────────────────────────
if (-not $Python) {
  $venv = Join-Path $Root ".venv\Scripts\python.exe"
  if (Test-Path $venv) { $Python = $venv }
  else {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "No Python found. Install Python 3.9+ or pass -Python <path>." }
    $Python = $cmd.Source
  }
}
if (-not (Test-Path $Python)) { throw "Python not found at: $Python" }
if (-not (Test-Path (Join-Path $Root "run_session.py"))) {
  throw "run_session.py not found in $Root — run this from the repo's windows\ folder."
}

# ── Compute the local-clock trigger from the requested ET time ────────────────
$parts = $AtET.Split(":")
if ($parts.Count -ne 2) { throw "AtET must be HH:mm, got '$AtET'." }
$etz   = [System.TimeZoneInfo]::FindSystemTimeZoneById("Eastern Standard Time")
$today = (Get-Date).Date
$etWall = [datetime]::SpecifyKind(
  $today.AddHours([int]$parts[0]).AddMinutes([int]$parts[1]), 'Unspecified')
$utc      = [System.TimeZoneInfo]::ConvertTimeToUtc($etWall, $etz)
$localFire = $utc.ToLocalTime()
$AtLocal   = $localFire.ToString("HH:mm")

# ── Build the task ────────────────────────────────────────────────────────────
$action = New-ScheduledTaskAction -Execute $Python `
  -Argument "run_session.py" -WorkingDirectory $Root

$trigger = New-ScheduledTaskTrigger -Weekly `
  -DaysOfWeek Monday,Tuesday,Wednesday,Thursday,Friday `
  -At $AtLocal

# StartWhenAvailable: run late if the machine was off at trigger time (the
#   supervisor's ET gate then decides if there's still a session worth running).
# WakeToRun: wake a sleeping machine. Battery flags: a laptop must not skip.
# IgnoreNew: never run two sessions at once.
# No restart-on-failure is configured (the default): the bot exits
#   intentionally at 15:25, and Task Scheduler must NOT relaunch it — the
#   supervisor owns any in-session relaunch itself.
$settings = New-ScheduledTaskSettingsSet `
  -StartWhenAvailable -WakeToRun `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Hours 8)

# S4U = "run whether user is logged on or not" WITHOUT storing a password.
try {
  $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U -RunLevel Limited
} catch {
  Write-Warning "S4U principal unavailable; falling back to Interactive (task runs only while you are logged in)."
  $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Settings $settings -Principal $principal -Force | Out-Null

# ── Report ────────────────────────────────────────────────────────────────────
$localTz = [System.TimeZoneInfo]::Local.Id
Write-Host ""
Write-Host "Registered scheduled task '$TaskName'." -ForegroundColor Green
Write-Host "  Python      : $Python"
Write-Host "  Working dir : $Root"
Write-Host "  Fires       : weekdays at $AtLocal local ($AtET ET)  [machine TZ: $localTz]"
Write-Host "  Runs        : run_session.py  (bot + auto reconcile + feed monitor)"
Write-Host ""
Write-Host "The task self-skips weekends and market holidays (the bot's calendar decides)."
Write-Host "Watch a live session at http://127.0.0.1:8080 . Logs are in $Root\logs ."
Write-Host "Test it now without waiting for the trigger:" -ForegroundColor Yellow
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "To remove: .\windows\uninstall_scheduler.ps1"
