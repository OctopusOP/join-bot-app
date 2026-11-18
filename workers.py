"""
workers.py
==========
The jobs that talk to Telegram. Each runs in a background thread so the
window never freezes. They report back through a `report(kind, text)`
function the GUI gives them (the GUI shows it in its log).

  send_content()   send a greeting/broadcast (text + files) to ONE user
  JoinWorker       watches join requests -> greet -> approve -> save user
  BroadcastWorker  sends a message to every saved subscriber
  ForwardWorker    copies/forwards many message links into your channel
"""

import html
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import storage
from links import group_into_batches, parse_link
from telegram_api import TelegramBot, TelegramError, guess_kind

Report = Callable[[str, str], None]  # report("ok" | "error" | "info", "message")


def personalize(text: str, user: Dict[str, Any], channel_title: str) -> str:
    """
    Fills {first_name} {last_name} {username} {user_id} {channel} in a text.
    Values are HTML-escaped, because every text is sent with parse_mode=HTML:
    a name like "Tom & <Jerry>" would otherwise make Telegram reject the message.
    """
    if not text:
        return text
    values = {
        "{first_name}": user.get("first_name") or "there",
        "{last_name}": user.get("last_name") or "",
        "{username}": ("@" + user["username"]) if user.get("username") else "",
        "{user_id}": str(user.get("id") or user.get("user_id") or ""),
        "{channel}": channel_title or "",
    }
    # One pass, so a value is never itself treated as a placeholder
    # (a user named "{channel}" must stay "{channel}").
    return _PLACEHOLDER.sub(
        lambda m: html.escape(str(values[m.group(0)]), quote=False) if m.group(0) in values else m.group(0),
        text)


_PLACEHOLDER = re.compile(r"\{\w+\}")


def _with_retry(call: Callable[[], Any], attempts: int = 4, safe_to_repeat: bool = False) -> Any:
    """
    Runs ONE Telegram call and retries it when that is safe:
      - 429 "too fast": always (Telegram did nothing, it says how long to wait)
      - network error while connecting: always (the request never arrived)
      - network timeout / Telegram 5xx: only if `safe_to_repeat`, because the
        request may have worked and a message would arrive twice. Approving a
        join request twice is harmless, so approvals pass safe_to_repeat=True.
    Retrying single calls (not a whole greeting) means nobody gets duplicates.
    """
    delay = 2.0
    for attempt in range(attempts):
        try:
            return call()
        except TelegramError as e:
            if attempt == attempts - 1:
                raise
            if e.code == 429:
                time.sleep(e.retry_after + 1)
            elif e.network and (safe_to_repeat or not e.maybe_sent):
                time.sleep(delay)
                delay = min(delay * 2, 15)
            elif (e.code or 0) >= 500 and safe_to_repeat:
                time.sleep(delay)
                delay = min(delay * 2, 15)
            else:
                raise


# Errors from approveChatJoinRequest that mean "nothing left to do"
ALREADY_HANDLED = ("USER_ALREADY_PARTICIPANT", "HIDE_REQUESTER_MISSING")
PENDING_GIVE_UP = 24 * 3600  # stop retrying a failed approval after a day


def bot_rights_problem(bot: TelegramBot, channel_id: Any, bot_user_id: int) -> str:
    """
    Approving join requests needs the bot to be an ADMIN with the
    "Invite users via link" right. Returns a message saying what's wrong,
    or "" when everything is fine (or it couldn't be checked).
    """
    try:
        member = bot.get_chat_member(channel_id, bot_user_id)
    except TelegramError:
        return ""  # can't tell (network); approvals will show any real error
    if member.get("status") == "creator":
        return ""
    if member.get("status") != "administrator":
        return ("The bot is NOT an admin of this channel, so it can't approve join requests. "
                "Channel → Administrators → add the bot with “Invite users via link”.")
    if not member.get("can_invite_users"):
        return ("The bot is an admin but lacks the “Invite users via link” (Add members) right, "
                "so join requests can't be approved. Turn it on in Channel → Administrators → the bot.")
    return ""


def _file_cache_key(bot: TelegramBot, path: str) -> Optional[str]:
    """
    Key for the file_id cache. A file_id only works for the bot that uploaded
    it, and is wrong once the file changes, so the key includes the bot id and
    the file's size + modification time. None if the file can't be read.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    bot_id = bot.token.split(":", 1)[0]
    return f"{bot_id}|{path}|{st.st_size}|{st.st_mtime_ns}"


def _send_file_item(bot: TelegramBot, chat_id: Any, item: Dict[str, Any], caption: str) -> None:
    path = item["path"]
    kind = item.get("kind") or guess_kind(path)
    key = _file_cache_key(bot, path)
    cached = storage.get_file_id(key) if key else ""
    if cached:
        # Saved as "kind:file_id": Telegram may store e.g. a .wav sent as audio
        # as a document, and a file_id only works with the method of its kind.
        cached_kind, _, file_id = cached.rpartition(":")
        try:
            _with_retry(lambda: bot.send_file(chat_id, cached_kind or kind, file_id=file_id, caption=caption))
            return
        except TelegramError as e:
            if e.code != 400:  # 400 = file_id no longer valid -> upload again below
                raise
    msg = _with_retry(lambda: bot.send_file(chat_id, kind, path=path, caption=caption))
    stored_kind, new_id = TelegramBot.file_kind_and_id(msg or {})
    if new_id and key:
        storage.remember_file_id(key, f"{stored_kind}:{new_id}")


def send_content(bot: TelegramBot, chat_id: Any, content: Dict[str, Any],
                 user: Dict[str, Any], channel_title: str,
                 warn: Optional[Callable[[str], None]] = None) -> int:
    """
    Sends text first, then each attachment in order, to one chat.
    Returns how many messages were sent. Raises TelegramError on failure.
    `warn(text)` is told about messages that were skipped (deleted ones).
    """
    sent = 0
    text = personalize((content.get("text") or "").strip(), user, channel_title)
    if text:
        _with_retry(lambda: bot.send_message(chat_id, text))
        sent += 1

    for item in content.get("items") or []:
        if item.get("type") == "text":
            # An extra text message typed into one of the rows
            extra = personalize((item.get("text") or "").strip(), user, channel_title)
            if extra:
                _with_retry(lambda: bot.send_message(chat_id, extra))
                sent += 1
        elif item.get("type") == "link":
            # Copy every message the link points to (no "Forwarded from" tag).
            # copyMessages skips deleted messages instead of failing, and keeps albums together.
            pairs = parse_link(item["link"])
            copied = 0
            for from_chat, ids in group_into_batches(pairs):
                result = _with_retry(lambda: bot.copy_messages(chat_id, from_chat, ids))
                copied += len(result or [])
            sent += copied
            if copied < len(pairs) and warn:
                warn(f"{len(pairs) - copied} message(s) from {item['link']} were skipped (deleted or not copyable)")
        elif item.get("type") == "file":
            caption = personalize(item.get("caption", ""), user, channel_title)
            _send_file_item(bot, chat_id, item, caption)
            sent += 1
    return sent


# =============================================================================
# Join requests
# =============================================================================
class JoinWorker:
    """
    Accepts join requests for ONE channel only.

    Order for each request: greeting FIRST, then approve. Telegram only lets a
    bot message someone while their request is pending; once that message is
    delivered, the private chat exists and broadcasts can reach them later.
    """

    def __init__(self, report: Report, on_stats: Callable[[Dict[str, int]], None]):
        self.report = report
        self.on_stats = on_stats      # called whenever the counters change
        self.running = False
        self.failed = False           # True when the bot stopped because of an error (not Stop)
        self.thread: Optional[threading.Thread] = None
        self.offset: Optional[int] = None  # next update id to ask Telegram for
        self.generation = 0  # bumps on every start, so an old loop knows it must quit
        self.stats = {"requests": 0, "approved": 0, "greeted": 0, "ignored": 0, "errors": 0}

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.failed = False
        self.generation += 1
        previous = self.thread  # may still be finishing its last long-poll
        self.thread = threading.Thread(target=self._run, args=(self.generation, previous), daemon=True)
        self.thread.start()

    def stop(self) -> None:
        # The loop notices within ~10 seconds (the long-poll timeout)
        self.running = False

    def _active(self, gen: int) -> bool:
        """True while this loop is the current one and nobody pressed Stop."""
        return self.running and gen == self.generation

    def _sleep(self, gen: int, seconds: float) -> None:
        """Sleeps, but wakes up early when Stop is pressed."""
        end = time.time() + seconds
        while self._active(gen) and time.time() < end:
            time.sleep(0.5)

    def _bump(self, key: str) -> None:
        self.stats[key] += 1
        self.on_stats(dict(self.stats))

    def _fail(self, gen: int, text: str) -> None:
        """Stops this loop because of an error the user must fix."""
        self.report("error", text)
        if gen == self.generation:
            self.failed = True
            self.running = False
        self.on_stats(dict(self.stats))

    def _run(self, gen: int, previous: Optional[threading.Thread]) -> None:
        try:
            self._loop(gen, previous)
        except Exception as e:  # never die silently while the window says "Running"
            self._fail(gen, f"Join-request bot crashed: {e!r}")

    def _connect(self, gen: int, bot: TelegramBot, channel: str):
        """
        Checks the token and channel. Network problems are retried (e.g. the
        server booted before the network was up); a wrong token/channel stops.
        Returns (me, chat) or None.
        """
        while self._active(gen):
            try:
                me = bot.get_me()
                if bot.get_webhook_url():
                    self._fail(gen, "Could not start: this bot has a webhook set. Remove it or use another bot.")
                    return None
                chat = bot.get_chat(channel)
                return me, chat
            except TelegramError as e:
                if e.code is None or e.code == 429 or e.code >= 500:
                    self.report("error", f"Can't reach Telegram ({e}). Retrying in 15 s…")
                    self._sleep(gen, 15)
                    continue
                self._fail(gen, f"Could not start: {e}")
                return None
        return None

    def _loop(self, gen: int, previous: Optional[threading.Thread]) -> None:
        # Two loops reading the same bot = Telegram "409 Conflict", so wait
        # for the old loop (after a quick Stop -> Start) to finish first.
        if previous and previous.is_alive():
            self.report("info", "Waiting for the previous session to finish its last request…")
            # Wait until it's really gone (it may be uploading a big file); two
            # loops at once would handle the same requests twice.
            while previous.is_alive() and self._active(gen):
                previous.join(timeout=1)
            if not self._active(gen):
                return

        settings = storage.load_settings()
        bot = TelegramBot(settings["token"])

        # ---- check everything before we start ----
        connected = self._connect(gen, bot, settings["channel"])
        if not connected:
            return
        me, chat = connected
        channel_id = chat["id"]
        channel_title = chat.get("title", settings["channel"])
        self.report("ok", f"Watching join requests for “{channel_title}” as @{me['username']}")
        problem = bot_rights_problem(bot, channel_id, me["id"])
        if problem:
            self._bump("errors")
            self.report("error", problem)

        if settings.get("auto_approve") and storage.load_pending_approvals():
            self.report("info", f"{len(storage.load_pending_approvals())} approval(s) from earlier "
                                "network trouble will be retried.")

        # ---- main loop ----
        net_fails = 0          # network failures in a row
        last_net_report = 0.0  # so a bad connection doesn't flood the log
        while self._active(gen):
            if storage.load_settings().get("auto_approve"):
                try:
                    self._retry_pending(bot, channel_id)
                except Exception as e:
                    self.report("error", f"Retrying pending approvals failed: {e!r}")
            try:
                updates = bot.get_updates(self.offset, timeout=10)
                if net_fails >= 3:
                    self.report("ok", "Connection to Telegram is back.")
                net_fails = 0
            except TelegramError as e:
                if e.network or (e.code or 0) >= 500:
                    # Unstable connection: harmless for getUpdates (Telegram keeps
                    # the requests until we read them), so just reconnect quickly.
                    net_fails += 1
                    if net_fails >= 3 and time.time() - last_net_report > 300:
                        last_net_report = time.time()
                        self._bump("errors")
                        self.report("error", f"Connection to Telegram keeps failing ({e}). Still retrying. "
                                             "On a server, try: JOINBOT_IPV4=1 bash start.sh --server   "
                                             "or run: python3 server.py --check")
                    self._sleep(gen, min(2 * net_fails, 30))
                    continue
                self._bump("errors")
                if e.code in (401, 404):
                    # Token revoked/changed in @BotFather: retrying can't help
                    self._fail(gen, f"Telegram rejected the bot token ({e}). Update it in settings.json / the ⚙ tab.")
                    return
                if e.code == 409:
                    # Telegram allows only ONE program to read a bot's updates at a time
                    if "webhook" in str(e).lower():
                        self.report("error", "This bot has a webhook set (another service receives its updates). "
                                             "Remove the webhook or use a different bot.")
                    else:
                        self.report("error", "Another program is reading this bot's updates (409 Conflict). "
                                             "Is the app also running on another computer/server, or twice here? "
                                             "Linux: ps aux | grep -E 'app.py|server.py|main.py'  — retrying…")
                    self._sleep(gen, 10)  # wait a bit longer; the other copy may be closing
                elif e.code == 429:
                    self._sleep(gen, e.retry_after)
                else:
                    self.report("error", f"Telegram: {e}")
                self._sleep(gen, 5)
                continue
            except Exception as e:  # anything unexpected: log it and keep polling
                self._bump("errors")
                self.report("error", f"Unexpected error while reading updates: {e!r}")
                self._sleep(gen, 5)
                continue

            for update in updates or []:
                self.offset = update["update_id"] + 1  # never handle the same update twice
                request = update.get("chat_join_request")
                if request:
                    try:
                        self._handle(bot, request, channel_id, channel_title)
                    except Exception as e:  # one bad request must not stop the bot
                        self._bump("errors")
                        self.report("error", f"Could not handle a join request: {e!r}")

        # Confirm the last offset so a restart doesn't repeat old requests
        try:
            if self.offset is not None:
                bot.get_updates(self.offset, timeout=0)
        except TelegramError:
            pass
        if gen == self.generation and not self.failed:
            self.report("info", "Join-request bot stopped.")

    def _handle(self, bot: TelegramBot, request: Dict[str, Any], channel_id: int, channel_title: str) -> None:
        user = request["from"]
        name = f"{user.get('first_name', '')} (@{user.get('username') or '-'}, {user['id']})"

        # Telegram re-sends requests we read but didn't confirm (app closed or
        # killed mid-batch). Skip ones already handled so nobody is greeted twice.
        request_key = f"{request['chat']['id']}:{user['id']}:{request.get('date', '')}"
        if storage.was_request_handled(request_key):
            return
        self._bump("requests")

        # Only the selected channel; any other chat is left untouched
        if request["chat"]["id"] != channel_id:
            self._bump("ignored")
            self.report("info", f"Ignored request for another chat: {request['chat'].get('title')}")
            return

        # Read settings fresh, so greeting edits apply without restarting
        settings = storage.load_settings()
        greeted = False

        if settings.get("send_greeting"):
            greeting = settings.get("greeting") or {}
            if (greeting.get("text") or "").strip() or greeting.get("items"):
                try:
                    send_content(bot, request.get("user_chat_id") or user["id"], greeting, user, channel_title,
                                 warn=lambda t: self.report("info", t))
                    greeted = True
                    self._bump("greeted")
                    self.report("ok", f"Greeted {name}")
                except Exception as e:
                    # ANY greeting problem (network, bad item in a hand-edited
                    # settings.json, unreadable file…) must never skip the approval below
                    self._bump("errors")
                    self.report("error", f"Greeting to {name} failed: {e if isinstance(e, TelegramError) else repr(e)}")

        if settings.get("auto_approve"):
            self._approve(bot, channel_id, user["id"], name)

        try:
            storage.add_subscriber(user, greeted)
            storage.mark_request_handled(request_key)
        except OSError as e:  # e.g. subscribers.json owned by root after a `sudo` run
            self._bump("errors")
            self.report("error", f"Could not save {name}: {e}")

    # ---- approving, with a retry queue for network trouble ----
    def _approve(self, bot: TelegramBot, channel_id: int, user_id: int, name: str,
                 from_queue: bool = False) -> None:
        """
        Approves one request. A temporary failure (network, 429, Telegram 5xx)
        puts it in pending_approvals.json, retried from the main loop, so a
        bad moment on the network never leaves a request pending forever.
        """
        try:
            # Few, quick retries here so the bot doesn't stall on one user;
            # the queue keeps trying for longer.
            _with_retry(lambda: bot.approve_join_request(channel_id, user_id),
                        attempts=2 if from_queue else 3, safe_to_repeat=True)
        except TelegramError as e:
            if any(s in str(e) for s in ALREADY_HANDLED):
                # Also what we get when an earlier try timed out but DID work
                storage.drop_pending_approval(user_id)
                self.report("info", f"{name} was already handled")
            elif e.temporary:
                first_fail = storage.queue_pending_approval(user_id, channel_id, name)
                if first_fail:
                    self._bump("errors")
                    self.report("error", f"Approve {name} failed ({e}). Will keep retrying automatically.")
            else:
                storage.drop_pending_approval(user_id)
                self._bump("errors")
                self.report("error", f"Approve {name} failed: {e}")
            return
        storage.drop_pending_approval(user_id)
        self._bump("approved")
        self.report("ok", f"Approved {name}" + (" (after retrying)" if from_queue else ""))

    def _retry_pending(self, bot: TelegramBot, channel_id: int) -> None:
        """Retries approvals that failed because of network trouble (oldest first)."""
        now = time.time()
        for p in storage.due_pending_approvals(now):
            if p.get("channel_id") != channel_id:
                continue  # queued for a different channel (settings changed): leave it
            if now - p.get("since", now) > PENDING_GIVE_UP:
                storage.drop_pending_approval(p["user_id"])
                self.report("error", f"Gave up approving {p.get('name')} after 24 h of failures. "
                                     "Approve them by hand in Telegram.")
                continue
            self._approve(bot, channel_id, p["user_id"], p.get("name", str(p["user_id"])), from_queue=True)


# =============================================================================
# Broadcast
# =============================================================================
class BroadcastWorker:
    """Sends one content to every ACTIVE subscriber, ~20 users per second."""

    def __init__(self, report: Report, on_progress: Callable[[int, int, int, int], None]):
        self.report = report
        self.on_progress = on_progress  # (done, total, sent, failed)
        self.running = False
        self.cancel = False

    def start(self, content: Dict[str, Any]) -> None:
        if self.running:
            return
        self.running, self.cancel = True, False
        threading.Thread(target=self._run, args=(content,), daemon=True).start()

    def _run(self, content: Dict[str, Any]) -> None:
        try:
            self._broadcast(content)
        except Exception as e:
            self.report("error", f"Broadcast stopped by an unexpected error: {e!r}")
        finally:
            self.running = False  # always, so a new broadcast can be started

    def _broadcast(self, content: Dict[str, Any]) -> None:
        settings = storage.load_settings()
        bot = TelegramBot(settings["token"])
        try:
            channel_title = bot.get_chat(settings["channel"]).get("title", "") if settings["channel"] else ""
        except TelegramError:
            channel_title = ""

        users = [s for s in storage.load_subscribers() if s.get("status", "active") == "active"]
        total, sent, failed, blocked = len(users), 0, 0, 0
        self.report("info", f"Broadcast started to {total} users")
        warned = set()  # report each "skipped messages" warning once, not once per user

        def warn(text: str) -> None:
            if text not in warned:
                warned.add(text)
                self.report("info", text)

        for i, user in enumerate(users, 1):
            if self.cancel:
                self.report("info", "Broadcast cancelled")
                break
            try:
                # send_content retries each single message on 429, so a user
                # never gets the first part of the content twice
                send_content(bot, user["user_id"], content, user, channel_title, warn=warn)
                sent += 1
            except TelegramError as e:
                if e.code == 403:
                    # "bot was blocked by the user" / "user is deactivated" -> blocked.
                    # "bot can't initiate conversation" = the greeting never reached
                    # them, so the bot was never allowed to message them: unreachable.
                    status = "unreachable" if "initiate" in str(e).lower() else "blocked"
                    storage.set_subscriber_status(user["user_id"], status)
                    blocked += 1
                else:
                    failed += 1
                    self.report("error", f"{user['user_id']}: {e}")
            except ValueError as e:  # bad link in the content
                failed += 1
                self.report("error", str(e))
            self.on_progress(i, total, sent, failed + blocked)
            time.sleep(0.05)

        self.report("ok", f"Broadcast done: {sent} sent, {blocked} blocked/unreachable, {failed} failed")


# =============================================================================
# Forward many links into a channel
# =============================================================================
class ForwardWorker:
    """
    Sends the messages from many pasted links into a target channel.
    mode "copy"    -> no "Forwarded from" tag (copyMessages)
    mode "forward" -> keeps the tag (forwardMessages)
    Messages are sent in batches of up to 100 per API call, and albums stay together.
    """

    def __init__(self, report: Report, on_progress: Callable[[int, int], None]):
        self.report = report
        self.on_progress = on_progress  # (messages done, messages total)
        self.running = False
        self.cancel = False

    def start(self, pairs: List[tuple], target: str, mode: str, delay: float) -> None:
        if self.running:
            return
        self.running, self.cancel = True, False
        threading.Thread(target=self._run, args=(pairs, target, mode, delay), daemon=True).start()

    def _run(self, pairs: List[tuple], target: str, mode: str, delay: float) -> None:
        try:
            self._forward(pairs, target, mode, delay)
        except Exception as e:
            self.report("error", f"Forwarding stopped by an unexpected error: {e!r}")
        finally:
            self.running = False  # always, so a new run can be started

    def _forward(self, pairs: List[tuple], target: str, mode: str, delay: float) -> None:
        bot = TelegramBot(storage.load_settings()["token"])
        batches = group_into_batches(pairs)
        total = sum(len(ids) for _, ids in batches)
        done = ok = 0

        for from_chat, ids in batches:
            if self.cancel:
                self.report("info", "Forwarding cancelled")
                break
            while True:
                try:
                    if mode == "forward":
                        result = bot.forward_messages(target, from_chat, ids)
                    else:
                        result = bot.copy_messages(target, from_chat, ids)
                    result = result or []
                    ok += len(result)
                    skipped = len(ids) - len(result)
                    note = f" ({skipped} skipped: deleted or not copyable)" if skipped else ""
                    self.report("ok", f"{len(result)} message(s) from {from_chat} #{ids[0]}–{ids[-1]}{note}")
                    break
                except TelegramError as e:
                    if e.code == 429:
                        self.report("info", f"Telegram says slow down, waiting {e.retry_after}s…")
                        time.sleep(e.retry_after + 1)
                        continue
                    self.report("error", f"{from_chat} #{ids[0]}–{ids[-1]}: {e}")
                    break
            done += len(ids)
            self.on_progress(done, total)
            time.sleep(delay)

        self.report("ok", f"Forwarding finished: {ok} of {total} messages sent")
