# Join Bot (simple desktop app)

A small Tkinter app for **one bot and one channel**.

**macOS / Linux:**
```bash
./start.sh
```
**Windows:** double-click `start.bat`.

The start script creates a private Python environment in `.venv` and installs `requirements.txt` the first
time. After that it only reinstalls when the requirements file changes, closes any copy of the app that is
already open, and then starts it. You only need Python 3.8+ installed.

## Tabs

| Tab | What it does |
| :-- | :-- |
| **Settings** | Bot token + your channel. "Test connection" checks both. |
| **Join Requests** | Start/stop the auto-accept bot. For each request to **your channel only**, it sends the greeting (text + files + copied messages), approves the request, and saves the user ID. |
| **Broadcast** | Sends a message (text + files + copied messages) privately to every saved user, with a progress bar and Cancel. Users who blocked the bot get marked and skipped. |
| **Subscribers** | List of saved users. Export to CSV, or remove users. |
| **Forward** | Paste many message links (one per line) and send them all into your channel. **Copy** mode has no "Forwarded from" tag; **Forward** mode keeps it. Ranges such as `https://t.me/c/123/10-20` work. Messages go out in the order you pasted them, up to 100 per Telegram call, and albums stay grouped. |

Placeholders in greeting and broadcast text and captions: `{first_name} {last_name} {username} {user_id} {channel}`.

## Linux

```bash
bash start.sh            # window (needs a desktop)
bash start.sh --server   # no window: join-request bot only (VPS / SSH)
```

- `bash start.sh` works even when the folder lost its "executable" permission while being copied
  (that's the `Permission denied` / `Errno 13` error). To make `./start.sh` work: `chmod +x start.sh`.
- A `.venv` folder copied from another computer doesn't work on Linux. The script detects that and rebuilds it.
- Missing system packages are named for your distro, e.g. on Ubuntu/Debian:
  `sudo apt install -y python3-venv python3-tk`.
- With no screen (SSH, server) it automatically starts **server mode** (`server.py`). Server mode runs only
  the join-request bot. It reads the same `settings.json`, so set things up in the window app on your PC
  and copy `settings.json` over. Greeting files must exist at the same paths on the server; message links
  work anywhere.
- To keep server mode running after you log out: `nohup bash start.sh --server > bot.log 2>&1 &`

When copying this folder to another computer, leave out `.venv` and `__pycache__`.

## Telegram setup (one time)

1. Add the bot to your channel as **admin** with **Add Members / Invite Users via Link**.
2. Channel → Invite Links → create a link with **Request Admin Approval** turned on, and share that link.
3. To copy or forward from another channel, the bot must be a member there (an admin, for private channels).
4. Run only one program with this bot token at a time. Telegram allows a single reader per bot.

## Files

| File | Purpose |
| :-- | :-- |
| `app.py` | The window (all tabs) |
| `server.py` | No-window mode for Linux servers (join-request bot only) |
| `workers.py` | Background jobs: join requests, broadcast, forward |
| `telegram_api.py` | Minimal Telegram Bot API client |
| `links.py` | Reads message links and ranges, groups them into batches |
| `storage.py` | Saves `settings.json`, `subscribers.json`, `file_ids.json` |
