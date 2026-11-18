"""
server.py — Join Bot WITHOUT a window (for a Linux server / VPS / SSH)
=====================================================================
Runs only the join-request bot: accept requests for your channel, send the
greeting, save the users. Logs are printed to the terminal.

    ./start.sh --server        (recommended: sets everything up first)
    python3 server.py          (direct)

It uses the same settings.json as the window app. Easiest setup:
  1. Set the token, channel and greeting in the window app on your PC.
  2. Copy settings.json (and the files used in the greeting, at the same
     paths) into this folder on the server.
Or create settings.json yourself; see the example printed if it's missing.

Broadcast and Forward need the window app; run that on a PC with a screen.
Don't run the bot on the PC and the server at the same time with the same token.
"""

import signal
import sys
import time
from datetime import datetime

import storage
from workers import JoinWorker

EXAMPLE = """{
  "token": "123456789:AA...your bot token...",
  "channel": "-1001234567890",
  "auto_approve": true,
  "send_greeting": true,
  "greeting": {
    "text": "Hi {first_name}! Welcome to <b>{channel}</b>",
    "items": [ {"type": "link", "link": "https://t.me/c/2583777886/414"} ]
  }
}"""

# Colours for the terminal output
COLORS = {"ok": "\033[32m", "error": "\033[31m", "info": "\033[90m"}


def report(kind: str, text: str) -> None:
    """Prints one log line, e.g. '14:02:11  ✓ Approved Rahul (...)'."""
    mark = {"ok": "✓", "error": "✖", "info": "·"}.get(kind, "·")
    try:
        print(f"{COLORS.get(kind, '')}{datetime.now():%Y-%m-%d %H:%M:%S}  {mark} {text}\033[0m", flush=True)
    except (BrokenPipeError, OSError):
        pass  # terminal closed (e.g. SSH disconnected): keep the bot running anyway


def main() -> None:
    settings = storage.load_settings()
    if not settings.get("token") or not settings.get("channel"):
        print(f"settings.json has no bot token / channel yet ({storage.SETTINGS_FILE}).\n"
              "Set them up in the window app and copy settings.json here, or create it like this:\n")
        print(EXAMPLE)
        sys.exit(1)

    worker = JoinWorker(report, on_stats=lambda stats: None)

    # Ctrl+C or `kill` -> stop cleanly (lets Telegram know where we stopped)
    stopped_by_user = False

    def stop(*_):
        nonlocal stopped_by_user
        stopped_by_user = True
        report("info", "Stopping… (up to 20 seconds)")
        worker.stop()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    report("info", f"Server mode · channel {settings['channel']} · "
                   f"auto-approve {'ON' if settings.get('auto_approve') else 'OFF'} · "
                   f"greeting {'ON' if settings.get('send_greeting') else 'OFF'}")
    worker.start()

    # Keep the program alive while the worker thread runs; print stats hourly.
    # (If the thread ever ends, for any reason, the program exits too, so a
    # service manager like systemd can restart it.)
    last_stats = time.time()
    while worker.thread and worker.thread.is_alive():
        time.sleep(1)
        if time.time() - last_stats > 3600:
            s = worker.stats
            report("info", f"Stats: {s['requests']} requests, {s['approved']} approved, "
                           f"{s['greeted']} greeted, {s['errors']} errors")
            last_stats = time.time()

    # Exit code 1 when the bot stopped by itself (bad token/channel, crash),
    # so systemd's Restart=on-failure restarts it. Network outages at start
    # are retried inside the worker and don't end up here.
    if worker.failed or not stopped_by_user:
        sys.exit(1)


if __name__ == "__main__":
    main()
