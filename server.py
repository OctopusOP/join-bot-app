"""
server.py — Join Bot WITHOUT a window (for a Linux server / VPS / SSH)
=====================================================================
Runs only the join-request bot: accept requests for your channel, send the
greeting, save the users. Logs are printed to the terminal.

    ./start.sh --server        (recommended: sets everything up first)
    python3 server.py          (direct)
    python3 server.py --check  (test connection + bot rights, then exit)

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
    except (OSError, ValueError):
        # Terminal closed (SSH disconnected), or it can't show some character.
        # Logging must NEVER stop the bot.
        pass


def _safe_output() -> None:
    """Non-UTF-8 terminals/log files (LANG=C, PYTHONIOENCODING=ascii): show
    '?' for characters like ✓ or emoji names instead of crashing."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # Python 3.7+
        except (AttributeError, ValueError):
            pass


def check() -> int:
    """
    `python3 server.py --check`: tests the connection to Telegram and the
    bot's rights, and says what to fix. Doesn't read or approve any requests.
    """
    import socket
    from telegram_api import TelegramBot, TelegramError
    from workers import bot_rights_problem

    host, ok = "api.telegram.org", True
    print(f"1. Reaching {host} (port 443):")
    for family, label in ((socket.AF_INET, "IPv4"), (socket.AF_INET6, "IPv6")):
        try:
            addr = socket.getaddrinfo(host, 443, family, socket.SOCK_STREAM)[0][4]
        except socket.gaierror:
            print(f"   {label}: no address (fine if the other one works)")
            continue
        start = time.time()
        try:
            with socket.create_connection(addr[:2], timeout=5):
                print(f"   {label}: OK in {(time.time() - start) * 1000:.0f} ms ({addr[0]})")
        except OSError as e:
            print(f"   {label}: FAILED ({e})")
            if label == "IPv6":
                print("         -> start the bot with  JOINBOT_IPV4=1 bash start.sh --server")

    settings = storage.load_settings()
    if not settings.get("token"):
        print("2. No bot token in settings.json.")
        return 1
    bot = TelegramBot(settings["token"])
    print("2. Bot API calls:")
    try:
        start = time.time()
        me = bot.get_me()
        print(f"   getMe: @{me['username']} in {(time.time() - start) * 1000:.0f} ms")
        if bot.get_webhook_url():
            print("   PROBLEM: a webhook is set, so this bot can't read join requests here.")
            ok = False
        chat = bot.get_chat(settings["channel"])
        print(f"   channel: “{chat.get('title')}” (ID {chat['id']})")
        problem = bot_rights_problem(bot, chat["id"], me["id"])
        print(f"   PROBLEM: {problem}" if problem else "   rights: admin with “Invite users via link” ✓")
        ok = ok and not problem
    except TelegramError as e:
        print(f"   FAILED: {e}")
        return 1
    pending = storage.load_pending_approvals()
    if pending:
        print(f"3. {len(pending)} approval(s) are waiting to be retried: "
              + ", ".join(str(p.get("name")) for p in pending[:5]))
    print("\nAll good." if ok else "\nFix the PROBLEM line(s) above.")
    return 0 if ok else 1


def main() -> None:
    _safe_output()
    if "--check" in sys.argv[1:]:
        sys.exit(check())
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
        report("info", "Stopping… (up to 10 seconds)")
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
