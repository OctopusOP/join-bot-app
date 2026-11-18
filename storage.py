"""
storage.py
==========
Everything this app saves lives in two JSON files next to the app:

  settings.json     bot token, channel, greeting, broadcast draft
  subscribers.json  every user who requested to join (for broadcasts)

A "content" (greeting or broadcast) looks like:
  {
    "text": "Hi {first_name}!",
    "items": [
      {"type": "file", "path": "/Users/me/price.pdf", "kind": "document", "file_id": ""},
      {"type": "link", "link": "https://t.me/c/123/55"}
    ]
  }
  - "file": a file on your computer (after the first upload we remember
            Telegram's file_id so it isn't uploaded again)
  - "link": a message in another channel, copied without "Forwarded from"
"""

import json
import shutil
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

APP_DIR = Path(__file__).parent
SETTINGS_FILE = APP_DIR / "settings.json"
SUBSCRIBERS_FILE = APP_DIR / "subscribers.json"

_lock = threading.Lock()  # stops two threads writing the same file at once

DEFAULT_SETTINGS: Dict[str, Any] = {
    "token": "",
    "channel": "",              # "@mychannel" or "-1001234567890"
    "auto_approve": True,
    "send_greeting": True,
    "greeting": {
        "text": "👋 Hi {first_name}! Welcome to <b>{channel}</b> 🎉",
        "items": [],
    },
    "broadcast": {"text": "", "items": []},
    "forward_mode": "copy",     # "copy" = no forwarded tag, "forward" = keep tag
    "forward_links": "",
    "forward_target": "",       # empty = the main channel
}


_backed_up = set()  # (path, mtime) of broken files we already saved a copy of


def _read(path: Path, default):
    """
    Reads a JSON file. A missing file gives `default`. A file that exists but
    can't be read (bad JSON, wrong type) ALSO gives `default`, but first a copy
    is saved as e.g. "subscribers.broken-20261118-140211.json", because the
    next save would otherwise overwrite it and the data would be lost.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, type(default)):
            return data
        problem = f"expected a JSON {type(default).__name__}"
    except FileNotFoundError:
        return default
    except Exception as e:
        problem = str(e)
    _backup_broken(path, problem)
    return default


def _backup_broken(path: Path, problem: str) -> None:
    try:
        key = (str(path), path.stat().st_mtime_ns)
        if key in _backed_up:
            return
        backup = path.with_name(f"{path.stem}.broken-{datetime.now():%Y%m%d-%H%M%S}{path.suffix}")
        shutil.copy2(path, backup)
        _backed_up.add(key)
        print(f"WARNING: {path.name} could not be read ({problem}). "
              f"A copy was saved as {backup.name}; starting with empty data.", file=sys.stderr)
    except OSError:
        pass


def _write(path: Path, data) -> None:
    # Write to a temp file then rename, so a crash never leaves half a file
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


# ---------------- settings ----------------

def load_settings() -> Dict[str, Any]:
    with _lock:
        saved = _read(SETTINGS_FILE, {})  # a broken file is backed up first (see _read)
    settings = {**DEFAULT_SETTINGS, **saved}
    return settings


def save_settings(settings: Dict[str, Any]) -> None:
    with _lock:
        _write(SETTINGS_FILE, settings)


# ---------------- subscribers ----------------

def load_subscribers() -> List[Dict[str, Any]]:
    with _lock:
        data = _read(SUBSCRIBERS_FILE, [])
    return [s for s in data if isinstance(s, dict) and "user_id" in s]


def add_subscriber(user: Dict[str, Any], greeted: bool) -> None:
    """Adds (or refreshes) one user from a join request."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        subs = [s for s in _read(SUBSCRIBERS_FILE, []) if isinstance(s, dict)]
        existing = next((s for s in subs if s.get("user_id") == user.get("id")), None)
        record = existing or {"user_id": user.get("id"), "joined_at": now}
        record.update({
            "first_name": user.get("first_name", ""),
            "last_name": user.get("last_name", ""),
            "username": user.get("username", ""),
            "greeted": bool(greeted or (existing or {}).get("greeted")),
            "status": "active",
        })
        if not existing:
            subs.append(record)
        _write(SUBSCRIBERS_FILE, subs)


def set_subscriber_status(user_id: int, status: str) -> None:
    """'active' or 'blocked' (blocked = user blocked the bot)."""
    with _lock:
        subs = _read(SUBSCRIBERS_FILE, [])
        for s in subs:
            if isinstance(s, dict) and s.get("user_id") == user_id:
                s["status"] = status
        _write(SUBSCRIBERS_FILE, subs)


FILE_IDS_FILE = APP_DIR / "file_ids.json"


def get_file_id(key: str) -> str:
    """Telegram's id for a file we already uploaded once ('' if never).
    `key` = bot id + path + size + modified time (see workers._file_cache_key)."""
    with _lock:
        return _read(FILE_IDS_FILE, {}).get(key, "")


def remember_file_id(key: str, file_id: str) -> None:
    """Saves Telegram's id for an uploaded file so we never upload it again."""
    with _lock:
        ids = _read(FILE_IDS_FILE, {})
        ids[key] = file_id
        _write(FILE_IDS_FILE, ids)


def remove_subscribers(user_ids: List[int]) -> None:
    with _lock:
        subs = [s for s in _read(SUBSCRIBERS_FILE, []) if isinstance(s, dict) and s.get("user_id") not in user_ids]
        _write(SUBSCRIBERS_FILE, subs)
