#!/usr/bin/env bash
# =============================================================================
# start.sh — sets up and starts the Join Bot app (macOS + Linux)
#
#     ./start.sh            open the window (or server mode if there's no screen)
#     ./start.sh --server   no window: run only the join-request bot (for a VPS)
#     ./start.sh --check    test the connection to Telegram + the bot's admin rights
#
# If you get "Permission denied", run it as:   bash start.sh
#
# Step by step:
#   1. Finds Python 3.8 or newer.
#   2. Makes a private Python environment in ".venv". If .venv was copied from
#      another computer (e.g. Mac -> Linux) it is broken, so it's rebuilt.
#   3. Installs requirements.txt when it changed since the last run.
#   4. Window mode: checks Tkinter + a screen. No screen -> server mode.
#   5. Closes an already-running copy, then starts.
# =============================================================================

set -u

# Always work inside the folder this script lives in
cd "$(dirname "$0")" || exit 1
APP_DIR="$(pwd -P)"   # -P = real path (no symlinks), so we can recognise our own running copies
VENV="$APP_DIR/.venv"
STAMP="$VENV/.requirements.sum"   # remembers which requirements were installed
OS="$(uname -s)"                  # "Linux" or "Darwin" (macOS)

say()  { printf "\033[1;34m▸\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m! %s\033[0m\n" "$*"; }
fail() { printf "\033[1;31m✖ %s\033[0m\n" "$*"; exit 1; }

# Prints the right "install this" command for the user's Linux distribution.
# $1 = what is missing: python | venv | tk  (package names differ per distro)
linux_install_hint() {
  if command -v apt >/dev/null 2>&1; then          # Ubuntu, Debian, Mint, Raspberry Pi OS
    case "$1" in python) echo "sudo apt install -y python3" ;; venv) echo "sudo apt install -y python3-venv" ;; tk) echo "sudo apt install -y python3-tk" ;; esac
  elif command -v dnf >/dev/null 2>&1; then        # Fedora, RHEL, Rocky, Alma
    case "$1" in python|venv) echo "sudo dnf install -y python3" ;; tk) echo "sudo dnf install -y python3-tkinter" ;; esac
  elif command -v pacman >/dev/null 2>&1; then     # Arch, Manjaro
    case "$1" in python|venv) echo "sudo pacman -S python" ;; tk) echo "sudo pacman -S tk" ;; esac
  elif command -v zypper >/dev/null 2>&1; then     # openSUSE
    case "$1" in python|venv) echo "sudo zypper install python3" ;; tk) echo "sudo zypper install python3-tk" ;; esac
  else
    echo "your package manager (package: python3 / python3-venv / python3-tk)"
  fi
}

# ---------- 1. find Python 3.8+ ----------
PYTHON=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
    PYTHON="$candidate"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  [ "$OS" = "Linux" ] && fail "Python 3.8+ not found. Install it: $(linux_install_hint python)"
  fail "Python 3.8+ not found. Install it: brew install python"
fi

# ---------- 2. private environment (.venv) ----------
# A .venv only works on the computer that made it. Test it; rebuild if broken.
if [ -d "$VENV" ] && ! "$VENV/bin/python" -c "import sys" >/dev/null 2>&1; then
  warn ".venv was made on another computer (or is damaged). Rebuilding it…"
  rm -rf "$VENV"
fi

VPY=""
if [ -x "$VENV/bin/python" ]; then
  VPY="$VENV/bin/python"
else
  say "Creating Python environment in .venv (first run only)…"
  if "$PYTHON" -m venv "$VENV" >/dev/null 2>&1 && "$VENV/bin/python" -m pip --version >/dev/null 2>&1; then
    VPY="$VENV/bin/python"
  else
    rm -rf "$VENV"
    # Ubuntu/Debian ship Python without the venv module. The app still runs
    # without it (requirements are optional), so continue with plain Python.
    if [ "$OS" = "Linux" ]; then
      warn "Couldn't create .venv (python3-venv is missing). Fix later with: $(linux_install_hint venv)"
    else
      warn "Couldn't create .venv."
    fi
    warn "Continuing with the system Python, without extra packages."
    VPY="$PYTHON"
  fi
fi

# ---------- 3. install requirements when they changed ----------
if [ "$VPY" != "$PYTHON" ]; then
  NEW_SUM="$(cksum < requirements.txt)"          # cksum exists on every Linux & macOS
  OLD_SUM="$(cat "$STAMP" 2>/dev/null || true)"
  if [ "$NEW_SUM" != "$OLD_SUM" ]; then
    say "Installing packages from requirements.txt…"
    if "$VPY" -m pip install -r requirements.txt --quiet --disable-pip-version-check; then
      echo "$NEW_SUM" > "$STAMP"
      say "Packages ready."
    else
      warn "Package install failed (no internet?). Starting anyway; it will retry next time."
    fi
  fi
fi

# ---------- 4. window or server mode? ----------
# --check only tests the connection + bot rights; it must NOT close the running bot
if [ "${1:-}" = "--check" ]; then
  exec "$VPY" server.py --check
fi

MODE="window"
[ "${1:-}" = "--server" ] && MODE="server"

if [ "$MODE" = "window" ] && [ "$OS" = "Linux" ] && [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
  warn "No screen found (SSH / server). Starting SERVER MODE: join-request bot only, no window."
  MODE="server"
fi

if [ "$MODE" = "window" ] && ! "$VPY" -c "import tkinter" >/dev/null 2>&1; then
  if [ "$OS" = "Linux" ]; then
    fail "Tkinter (the window library) is missing. Install it: $(linux_install_hint tk)   — then run ./start.sh again. (Or run without a window: ./start.sh --server)"
  fi
  PYVER="$("$VPY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
  fail "Tkinter is missing. Install it: brew install python-tk@$PYVER"
fi

# ---------- 5. close an old copy, then start ----------
# Two copies would both read the same bot's updates and Telegram rejects that.
proc_dir() {  # working folder of a process
  if [ -e "/proc/$1/cwd" ]; then readlink "/proc/$1/cwd"            # Linux
  else lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p'; fi  # macOS
}
# Only Python running app.py / server.py (not e.g. `nano server.py`), and
# never a `--check` run.
for pid in $(pgrep -f "[Pp]ython[0-9.]*[^ ]* .*(app|server)\.py" 2>/dev/null); do
  [ "$pid" = "$$" ] && continue
  case "$(ps -o args= -p "$pid" 2>/dev/null)" in *--check*) continue ;; esac
  if [ "$(proc_dir "$pid")" = "$APP_DIR" ]; then
    say "Closing the copy that is already running (PID $pid)…"
    kill "$pid" 2>/dev/null
    # Wait until it has really exited (it finishes its current request and
    # tells Telegram where it stopped: up to ~10 s). Starting earlier gives
    # "409 Conflict" and could handle the same request twice.
    waited=0
    while kill -0 "$pid" 2>/dev/null; do
      if [ "$waited" -ge 40 ]; then
        warn "Old copy (PID $pid) didn't stop in 40 s; forcing it."
        kill -9 "$pid" 2>/dev/null
        sleep 1
        break
      fi
      sleep 1
      waited=$((waited + 1))
    done
  fi
done

if [ "$MODE" = "server" ]; then
  say "Starting Join Bot in server mode (Ctrl+C to stop)…"
  exec "$VPY" server.py
fi
say "Starting Join Bot…"
exec "$VPY" app.py
