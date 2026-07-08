<#
.SYNOPSIS
  Remove the 0DTE bot scheduled task.
.PARAMETER TaskName
  Task to remove. Default "AlpacaOptionsBot".
#>
[CmdletBinding()]
param([string]$TaskName = "AlpacaOptionsBot")

$ErrorActionPreference = "Stop"
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
  Write-Host "No task named '$TaskName' is registered - nothing to remove."
  return
}
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Green
Write-Host "(A session already running is not stopped - use Task Scheduler's"
Write-Host " 'End' or let it exit at the 15:25 ET time stop.)"
