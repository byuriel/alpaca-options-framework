@echo off
REM start_dashboard.cmd - launches the always-on dashboard, hidden, at login.
REM
REM Put a SHORTCUT to this file in your Startup folder so the dashboard is
REM always available at http://127.0.0.1:8080 whether or not the bot is
REM trading. It uses pythonw.exe (no console window) and the repo's .venv.
REM
REM Setup (one time):
REM   1) Press Win+R, type:  shell:startup   and press Enter.
REM   2) Right-click this file (start_dashboard.cmd) -> Copy.
REM   3) In the Startup folder, right-click -> Paste shortcut.
REM   That's it - the dashboard starts every time you log in.
REM
REM No admin, no scheduled task. It reads results from disk, so it works
REM even overnight and on weekends.

set "REPO=%~dp0.."
set "PYW=%REPO%\.venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw"

start "" /D "%REPO%" "%PYW%" dashboard.py
