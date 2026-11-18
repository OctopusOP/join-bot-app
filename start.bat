@echo off
REM ===========================================================================
REM start.bat - sets up and starts the Join Bot app (Windows)
REM Double-click this file, or run:  start.bat
REM
REM   1. Finds Python 3.8+  (install from python.org, tick "Add to PATH")
REM   2. Creates a private Python environment in ".venv" (first run only)
REM   3. Installs the packages listed in requirements.txt
REM   4. Starts the app
REM ===========================================================================

cd /d "%~dp0"

REM ---- 1. find Python ----
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo Python 3.8+ not found. Install it from https://www.python.org and tick "Add python.exe to PATH".
  pause
  exit /b 1
)

REM ---- 2. private environment ----
if not exist ".venv\Scripts\python.exe" (
  echo Creating Python environment in .venv ^(first run only^)...
  %PY% -m venv .venv || (echo Could not create .venv & pause & exit /b 1)
)

REM ---- 3. packages (pip skips ones that are already installed) ----
echo Checking packages from requirements.txt...
".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet --disable-pip-version-check
if errorlevel 1 (
  echo Package install failed. Check your internet connection.
  pause
  exit /b 1
)

REM ---- 4. start (pythonw = no extra black console window) ----
echo Starting Join Bot...
start "" ".venv\Scripts\pythonw.exe" app.py
