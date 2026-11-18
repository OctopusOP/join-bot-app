@echo off
REM ===========================================================================
REM start.bat - sets up and starts the Join Bot app (Windows)
REM Double-click this file, or run:  start.bat
REM
REM   1. Finds Python 3.8+  (install from python.org, tick "Add to PATH")
REM   2. Creates a private Python environment in ".venv" (first run only)
REM   3. Installs the packages listed in requirements.txt
REM   4. Closes a copy that is already running, then starts the app
REM ===========================================================================

cd /d "%~dp0"

REM ---- 1. find Python 3.8+ ----
set "PY="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo Python 3.8 or newer not found. Install it from https://www.python.org and tick "Add python.exe to PATH".
  pause
  exit /b 1
)

REM ---- 2. private environment ----
if not exist ".venv\Scripts\python.exe" (
  echo Creating Python environment in .venv ^(first run only^)...
  %PY% -m venv .venv || (echo Could not create .venv & pause & exit /b 1)
)

REM ---- 3. packages (pip skips ones that are already installed) ----
REM The packages are optional (better HTTPS certificates), so a failed install
REM (no internet, proxy) is only a warning - same as start.sh.
echo Checking packages from requirements.txt...
".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet --disable-pip-version-check
if errorlevel 1 echo WARNING: package install failed ^(no internet?^). Starting anyway; it will retry next time.

REM ---- 4. close a running copy (two copies = Telegram "409 Conflict"), then start ----
REM Matches only Python processes started from THIS folder's .venv.
powershell -NoProfile -Command "$me = (Resolve-Path '.venv').Path; Get-CimInstance Win32_Process -Filter \"Name like 'python%%'\" | Where-Object { $_.ExecutablePath -like ($me + '*') -and $_.CommandLine -match '(app|server)\.py' -and $_.CommandLine -notmatch '--check' } | ForEach-Object { Write-Host ('Closing the copy that is already running (PID ' + $_.ProcessId + ')...'); Stop-Process -Id $_.ProcessId -Force }" 2>nul

REM (pythonw = no extra black console window)
echo Starting Join Bot...
start "" ".venv\Scripts\pythonw.exe" app.py
