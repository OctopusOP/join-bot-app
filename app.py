"""
app.py — Join Bot (simple desktop app, one bot, one channel)
=============================================================
Run it with:   python3 app.py

Tabs (left to right, like the sketch):
  Join Req     greeting message + rows of links/files -> auto-accept, greet, save users
  Broadcast    message + rows of message/link/file   -> sent privately to all saved users
  Subscribers  see / export / remove saved users
  Forward      rows of links                         -> sent into your channel
  ⚙            settings: bot token + channel
A log at the bottom shows everything that happens.

The "rows" idea:
  Under the message box there is one input row per thing to send. Paste a
  message link (https://t.me/c/123/55) or press "+ File" to pick a file.
  "Add more +" adds another row. Rows are sent top to bottom. Messages from
  links are copied, so they arrive WITHOUT the "Forwarded from" tag.

How the window stays responsive:
  Telegram work runs in background threads (workers.py). Tkinter may only be
  touched from the main thread, so workers put messages into a queue and
  `_drain_queue()` (called every 100 ms) shows them.
"""

import csv
import os
import queue
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import storage
from links import group_into_batches, parse_link
from telegram_api import TelegramBot, TelegramError, guess_kind
from workers import BroadcastWorker, ForwardWorker, JoinWorker, send_content

PAD = 8  # standard spacing between widgets


# =============================================================================
# A frame that scrolls (so you can add as many rows as you want)
# =============================================================================
class ScrollFrame(ttk.Frame):
    """Put widgets inside `.inner`; a scrollbar appears when they don't fit."""

    def __init__(self, parent, height=200):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, height=height, highlightthickness=0)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        # Keep the scroll area as big as the content, and as wide as the canvas
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(window, width=e.width))
        # Mouse wheel / trackpad scrolling while the pointer is over the list.
        # macOS + Windows send <MouseWheel>; Linux (X11) sends Button-4 (up) / Button-5 (down).
        self.canvas.bind("<Enter>", self._wheel_on)
        self.canvas.bind("<Leave>", self._wheel_off)
        self.inner.bind("<Leave>", self._wheel_off)  # leaving via a row's edge

    def _wheel_on(self, _event=None):
        self.canvas.bind_all("<MouseWheel>", self._wheel)
        self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))

    def _wheel_off(self, _event=None):
        # Tk also sends <Leave> when the pointer moves onto a row INSIDE the
        # list; keep scrolling working unless the pointer really left the list.
        try:
            x, y = self.winfo_pointerxy()
            under = self.winfo_containing(x, y)
        except (tk.TclError, KeyError):
            under = None
        if under is not None and str(under).startswith(str(self.canvas)):
            return
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.canvas.unbind_all(seq)

    def _wheel(self, event):
        # macOS gives small deltas (±1..), Windows gives multiples of 120
        step = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(step, "units")

    def scroll_to_bottom(self):
        self.update_idletasks()
        self.canvas.yview_moveto(1.0)


# =============================================================================
# One input row:  [ paste link / type text / file path ] [+ File] [↑] [↓] [✕]
# =============================================================================
class ItemRow(ttk.Frame):
    def __init__(self, editor: "RowsEditor", item=None):
        super().__init__(editor.rows.inner)
        self.editor = editor
        self.caption = ""  # kept for files saved by older versions (not edited here)

        self.var = tk.StringVar()
        self.entry = ttk.Entry(self, textvariable=self.var)
        self.entry.pack(side="left", fill="x", expand=True)
        if editor.allow_files:  # the Forward tab only takes message links
            ttk.Button(self, text="+ File", width=6, command=self.pick_file).pack(side="left", padx=(4, 0))
        ttk.Button(self, text="↑", width=2, command=lambda: editor.move(self, -1)).pack(side="left")
        ttk.Button(self, text="↓", width=2, command=lambda: editor.move(self, 1)).pack(side="left")
        ttk.Button(self, text="✕", width=2, command=lambda: editor.remove(self)).pack(side="left")

        # Small label under the entry that says what the row was understood as
        self.status = ttk.Label(editor.rows.inner, foreground="gray")
        self.var.trace_add("write", lambda *_: self.update_status())

        if item:
            self.set_item(item)
        self.update_status()

    # ---- reading what the row contains ----
    def get_item(self):
        """
        Turns the row text into an item, or returns:
          None      -> row is empty (ignored)
          "invalid" -> can't be sent (shown in red)
        """
        value = self.var.get().strip()
        if not value:
            return None
        try:
            parse_link(value)
            return {"type": "link", "link": value}
        except ValueError:
            pass
        if self.editor.allow_files and os.path.isfile(value):
            return {"type": "file", "path": value, "kind": guess_kind(value), "caption": self.caption}
        if self.editor.allow_text:
            return {"type": "text", "text": value}
        return "invalid"

    def set_item(self, item):
        if item["type"] == "link":
            self.var.set(item["link"])
        elif item["type"] == "file":
            self.var.set(item["path"])
            self.caption = item.get("caption", "")
        elif item["type"] == "text":
            self.var.set(item.get("text", ""))

    def update_status(self):
        """Shows e.g. '🔗 3 message(s) from -100123' under the row."""
        item = self.get_item()
        if item is None:
            text, color = self.editor.empty_hint, "gray"
        elif item == "invalid":
            text, color = self.editor.invalid_hint, "#d33"
        elif item["type"] == "link":
            pairs = parse_link(item["link"])
            text, color = f"🔗 {len(pairs)} message(s) from {pairs[0][0]} — copied without “Forwarded from”", "#1a8f4e"
        elif item["type"] == "file":
            text, color = f"📄 {os.path.basename(item['path'])} ({item['kind']})", "#1a8f4e"
        else:
            text, color = "✎ text message", "#1a8f4e"
        self.status.configure(text=text, foreground=color)

    def pick_file(self):
        path = filedialog.askopenfilename(title="Choose a file to send")
        if path:
            self.var.set(path)


# =============================================================================
# Message box + rows + "Add more +"   (the layout from the sketch)
# Used by Join Req (greeting), Broadcast and Forward.
# =============================================================================
class RowsEditor(ttk.Frame):
    def __init__(self, parent, title, row_title, allow_text, with_message_box=True,
                 empty_hint="Paste a message link, or press + File", rows_height=180,
                 allow_files=True):
        super().__init__(parent)
        self.allow_text = allow_text          # may a row be a plain text message?
        self.allow_files = allow_files        # may a row be a file from this computer?
        self.empty_hint = empty_hint
        self.invalid_hint = ("⚠ Not a message link or an existing file"
                             if not allow_text else "⚠ Unreadable row")
        self.row_widgets = []

        # ---- the main message box ----
        self.text = None
        if with_message_box:
            ttk.Label(self, text=title, font=("", 13, "bold")).pack(anchor="w")
            ttk.Label(self, text="HTML: <b>bold</b> <i>italic</i> <a href='https://..'>link</a>   "
                                 "Placeholders: {first_name} {username} {user_id} {channel}",
                      foreground="gray").pack(anchor="w")
            self.text = tk.Text(self, height=5, wrap="word", undo=True)
            self.text.pack(fill="x", pady=(2, PAD))

        # ---- the rows ----
        ttk.Label(self, text=row_title, font=("", 12, "bold")).pack(anchor="w")
        ttk.Label(self, text="Example: https://t.me/c/2583777886/414   ·   a range: https://t.me/c/2583777886/410-414",
                  foreground="gray").pack(anchor="w")
        self.rows = ScrollFrame(self, height=rows_height)
        self.rows.pack(fill="both", expand=True, pady=(4, 4))

        buttons = ttk.Frame(self)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Add more +", command=self.add_row_and_focus).pack(side="left")
        ttk.Button(buttons, text="Paste many links…", command=self.paste_many).pack(side="left", padx=PAD)
        ttk.Button(buttons, text="Clear rows", command=self.clear_rows).pack(side="left")

    # ---- whole content ----
    def get(self):
        """Returns {"text": ..., "items": [...]} (empty and invalid rows are skipped)."""
        items = [r.get_item() for r in self.row_widgets]
        return {
            "text": self.text.get("1.0", "end").strip() if self.text else "",
            "items": [i for i in items if isinstance(i, dict)],
        }

    def invalid_rows(self):
        """Row numbers (1-based) that can't be sent, to warn before sending."""
        return [n for n, r in enumerate(self.row_widgets, 1) if r.get_item() == "invalid"]

    def set(self, content):
        if self.text:
            self.text.delete("1.0", "end")
            self.text.insert("1.0", content.get("text", ""))
        self.clear_rows(keep_one=False)
        for item in content.get("items") or []:
            self.add_row(item)
        if not self.row_widgets:
            self.add_row()  # always show at least one empty row

    # ---- row management ----
    def add_row(self, item=None):
        row = ItemRow(self, item)
        self.row_widgets.append(row)
        self._repack()
        return row

    def add_row_and_focus(self):
        row = self.add_row()
        row.entry.focus_set()
        self.rows.scroll_to_bottom()

    def remove(self, row):
        self.row_widgets.remove(row)
        row.status.destroy()
        row.destroy()
        if not self.row_widgets:
            self.add_row()
        self._repack()

    def move(self, row, direction):
        i = self.row_widgets.index(row)
        j = i + direction
        if 0 <= j < len(self.row_widgets):
            self.row_widgets[i], self.row_widgets[j] = self.row_widgets[j], self.row_widgets[i]
            self._repack()

    def clear_rows(self, keep_one=True):
        for r in self.row_widgets:
            r.status.destroy()
            r.destroy()
        self.row_widgets = []
        if keep_one:
            self.add_row()

    def _repack(self):
        """Lays the rows out again in list order (row, then its status line)."""
        for r in self.row_widgets:
            r.pack_forget()
            r.status.pack_forget()
        for n, r in enumerate(self.row_widgets, 1):
            r.pack(fill="x", pady=(4, 0))
            r.status.pack(anchor="w", padx=2)

    def paste_many(self):
        """Opens a box where you can paste many links at once; each becomes a row."""
        win = tk.Toplevel(self)
        win.title("Paste many links")
        win.geometry("560x320")
        ttk.Label(win, text="One link per line (or separated by spaces):").pack(anchor="w", padx=PAD, pady=(PAD, 2))
        box = ScrolledText(win, height=12, wrap="none")
        box.pack(fill="both", expand=True, padx=PAD)
        box.focus_set()

        def add():
            words = box.get("1.0", "end").split()
            # Replace a single empty starting row instead of leaving it blank
            if len(self.row_widgets) == 1 and not self.row_widgets[0].var.get().strip():
                self.clear_rows(keep_one=False)
            for word in words:
                self.add_row({"type": "link", "link": word})
            if not self.row_widgets:
                self.add_row()
            win.destroy()
            self.rows.scroll_to_bottom()

        ttk.Button(win, text="Add as rows", command=add).pack(pady=PAD)


# =============================================================================
# Main window
# =============================================================================
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Join Bot")
        self.geometry("900x800")
        self.minsize(760, 640)

        self.settings = storage.load_settings()
        self.events = queue.Queue()  # messages from background threads → GUI

        # Background workers. Their callbacks only put things into the queue.
        report = lambda kind, text: self.events.put(("log", kind, text))
        self.join_worker = JoinWorker(report, on_stats=lambda s: self.events.put(("join_stats", s)))
        self.broadcaster = BroadcastWorker(
            report, on_progress=lambda d, t, s, f: self.events.put(("bc_progress", d, t, s, f)))
        self.forwarder = ForwardWorker(report, on_progress=lambda d, t: self.events.put(("fw_progress", d, t)))

        # Layout: tabs on top, log at the bottom
        paned = ttk.PanedWindow(self, orient="vertical")
        paned.pack(fill="both", expand=True, padx=PAD, pady=PAD)
        self.tabs = ttk.Notebook(paned)
        paned.add(self.tabs, weight=5)
        paned.add(self._build_log(paned), weight=1)

        # Same order as the sketch: Join Req | Broadcast | Subscribers | (Forward) | ⚙
        self._build_join_tab()
        self._build_broadcast_tab()
        self._build_subscribers_tab()
        self._build_forward_tab()
        self._build_settings_tab()

        # Open the ⚙ tab first if there's no token yet
        if not self.settings["token"]:
            self.tabs.select(self.settings_tab)

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self._drain_queue)

    # ------------------------------------------------------------------
    # Log panel
    # ------------------------------------------------------------------
    def _build_log(self, parent):
        frame = ttk.Frame(parent)
        ttk.Label(frame, text="Activity log").pack(anchor="w")
        self.log_box = ScrolledText(frame, height=7, state="disabled", wrap="word")
        self.log_box.pack(fill="both", expand=True)
        self.log_box.tag_config("ok", foreground="#1a8f4e")
        self.log_box.tag_config("error", foreground="#d33")
        self.log_box.tag_config("info", foreground="#777")
        return frame

    def log(self, kind, text):
        """Adds one coloured line to the log (main thread only)."""
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{datetime.now():%H:%M:%S}  {text}\n", kind)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _drain_queue(self):
        """Runs every 100 ms: shows everything the background threads reported."""
        try:
            self._show_events()
        except Exception as e:  # never stop the 100 ms loop, or the window freezes its log/buttons
            try:
                self.log("error", f"Display error: {e!r}")
            except Exception:
                pass
        finally:
            self.after(100, self._drain_queue)

    def _show_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "log":
                    self.log(event[1], event[2])
                elif event[0] == "join_stats":
                    s = event[1]
                    self.join_stats_var.set(
                        f"Requests {s['requests']} · Approved {s['approved']} · Greeted {s['greeted']} · "
                        f"Other chats ignored {s['ignored']} · Errors {s['errors']}")
                elif event[0] == "bc_progress":
                    _, done, total, sent, failed = event
                    self.bc_bar.configure(maximum=max(total, 1), value=done)
                    self.bc_status.set(f"{done}/{total} done · {sent} delivered · {failed} failed/blocked")
                elif event[0] == "fw_progress":
                    _, done, total = event
                    self.fw_bar.configure(maximum=max(total, 1), value=done)
                    self.fw_status.set(f"{done}/{total} messages processed")
        except queue.Empty:
            pass
        # Keep buttons in sync with what's running
        self.join_btn.configure(text="■ Stop" if self.join_worker.running else "▶ Start")
        self.join_state.set("● Running" if self.join_worker.running else "○ Stopped")
        self.bc_cancel_btn.configure(state="normal" if self.broadcaster.running else "disabled")
        self.fw_cancel_btn.configure(state="normal" if self.forwarder.running else "disabled")

    def run_in_background(self, job):
        """Runs `job()` in a thread; it reports via self.events."""
        threading.Thread(target=job, daemon=True).start()

    # ------------------------------------------------------------------
    # Tab: Join Req
    # ------------------------------------------------------------------
    def _build_join_tab(self):
        tab = ttk.Frame(self.tabs, padding=PAD * 2)
        self.tabs.add(tab, text="Join Req")

        top = ttk.Frame(tab)
        top.pack(fill="x")
        self.join_btn = ttk.Button(top, text="▶ Start", command=self.toggle_join)
        self.join_btn.pack(side="left")
        self.join_state = tk.StringVar(value="○ Stopped")
        ttk.Label(top, textvariable=self.join_state, font=("", 12, "bold")).pack(side="left", padx=PAD)
        self.join_stats_var = tk.StringVar(value="Requests 0 · Approved 0 · Greeted 0 · Other chats ignored 0 · Errors 0")
        ttk.Label(top, textvariable=self.join_stats_var, foreground="gray").pack(side="left")

        toggles = ttk.Frame(tab)
        toggles.pack(fill="x", pady=PAD)
        self.auto_approve = tk.BooleanVar(value=self.settings["auto_approve"])
        self.send_greeting = tk.BooleanVar(value=self.settings["send_greeting"])
        ttk.Checkbutton(toggles, text="Auto-approve (only the channel in ⚙)", variable=self.auto_approve).pack(side="left")
        ttk.Checkbutton(toggles, text="Send greeting", variable=self.send_greeting).pack(side="left", padx=PAD)

        self.greeting_editor = RowsEditor(
            tab, title="Greeting message", row_title="Then send these (message links / files)",
            allow_text=False,
        )
        self.greeting_editor.pack(fill="both", expand=True)
        self.greeting_editor.set(self.settings["greeting"])

        bottom = ttk.Frame(tab)
        bottom.pack(fill="x", pady=(PAD, 0))
        ttk.Button(bottom, text="Save greeting", command=self.save_settings).pack(side="left")
        self._test_send_row(bottom, self.greeting_editor)

    def toggle_join(self):
        if self.join_worker.running:
            self.join_worker.stop()
            self.log("info", "Stopping… (finishes within ~10 seconds)")
            return
        if not self.save_settings(quiet=True):
            return
        if not self.settings["token"] or not self.settings["channel"]:
            messagebox.showwarning("Missing settings", "Enter the bot token and channel in the ⚙ tab first.")
            self.tabs.select(self.settings_tab)
            return
        self.join_worker.start()

    def _test_send_row(self, parent, editor):
        """'Send test to my user ID' controls, shared by Join Req and Broadcast."""
        ttk.Label(parent, text="   Test to my user ID:").pack(side="left")
        var = tk.StringVar(value=self.settings.get("test_user_id", ""))
        ttk.Entry(parent, textvariable=var, width=14).pack(side="left", padx=4)

        def send_test():
            user_id = var.get().strip()
            if not user_id.lstrip("-").isdigit():
                messagebox.showinfo("Your user ID", "Enter your numeric Telegram ID (ask @userinfobot).\n"
                                                    "You must have pressed Start in your bot once.")
                return
            if not self._rows_ok(editor):
                return
            self.settings["test_user_id"] = user_id
            self.save_settings(quiet=True)
            content = editor.get()
            token, channel = self.settings["token"], self.settings["channel"]

            def job():
                try:
                    bot = TelegramBot(token)
                    title = bot.get_chat(channel).get("title", "") if channel else ""
                    n = send_content(bot, int(user_id), content, {"id": user_id, "first_name": "Test"}, title,
                                     warn=lambda t: self.events.put(("log", "info", t)))
                    self.events.put(("log", "ok", f"Test sent ({n} message(s)). Check Telegram."))
                except (TelegramError, ValueError) as e:
                    self.events.put(("log", "error", f"Test failed: {e}"))

            self.run_in_background(job)

        ttk.Button(parent, text="Send test", command=send_test).pack(side="left")

    def _rows_ok(self, editor):
        """Warns about rows that can't be sent. Returns True if all rows are fine."""
        bad = editor.invalid_rows()
        if bad:
            messagebox.showerror("Check the rows", f"Row(s) {', '.join(map(str, bad))} aren't a message link "
                                                   "or an existing file. Fix or remove them (✕).")
            return False
        return True

    # ------------------------------------------------------------------
    # Tab: Broadcast
    # ------------------------------------------------------------------
    def _build_broadcast_tab(self):
        tab = ttk.Frame(self.tabs, padding=PAD * 2)
        self.tabs.add(tab, text="Broadcast")

        self.broadcast_editor = RowsEditor(
            tab, title="Broadcast message", row_title="Then send these (type a message / paste a link / + File)",
            allow_text=True, empty_hint="Type a message, paste a message link, or press + File",
        )
        self.broadcast_editor.pack(fill="both", expand=True)
        self.broadcast_editor.set(self.settings["broadcast"])

        row = ttk.Frame(tab)
        row.pack(fill="x", pady=PAD)
        ttk.Button(row, text="➤ Broadcast to all", command=self.start_broadcast).pack(side="left")
        self.bc_cancel_btn = ttk.Button(row, text="Cancel", state="disabled",
                                        command=lambda: setattr(self.broadcaster, "cancel", True))
        self.bc_cancel_btn.pack(side="left", padx=PAD)
        self._test_send_row(row, self.broadcast_editor)

        self.bc_bar = ttk.Progressbar(tab, mode="determinate")
        self.bc_bar.pack(fill="x")
        self.bc_status = tk.StringVar(value="Not started")
        ttk.Label(tab, textvariable=self.bc_status).pack(anchor="w")

    def start_broadcast(self):
        if self.broadcaster.running or not self._rows_ok(self.broadcast_editor):
            return
        content = self.broadcast_editor.get()
        if not content["text"] and not content["items"]:
            messagebox.showwarning("Empty", "Write a message or fill at least one row first.")
            return
        active = [s for s in storage.load_subscribers() if s.get("status", "active") == "active"]
        if not active:
            messagebox.showinfo("No subscribers", "No saved users yet. Start the join-request bot first.")
            return
        if not messagebox.askyesno("Broadcast", f"Send this to {len(active)} users? This can't be undone."):
            return
        self.save_settings(quiet=True)
        self.broadcaster.start(content)

    # ------------------------------------------------------------------
    # Tab: Subscribers
    # ------------------------------------------------------------------
    def _build_subscribers_tab(self):
        tab = ttk.Frame(self.tabs, padding=PAD * 2)
        self.tabs.add(tab, text="Subscribers")

        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Button(row, text="Refresh", command=self.load_subscribers).pack(side="left")
        ttk.Button(row, text="Export CSV…", command=self.export_csv).pack(side="left", padx=PAD)
        ttk.Button(row, text="Remove selected", command=self.remove_selected).pack(side="left")
        self.subs_count = tk.StringVar()
        ttk.Label(row, textvariable=self.subs_count).pack(side="left", padx=PAD)

        columns = ("user_id", "name", "username", "joined_at", "greeted", "status")
        self.tree = ttk.Treeview(tab, columns=columns, show="headings", selectmode="extended")
        for col, width in zip(columns, (110, 180, 140, 150, 70, 80)):
            self.tree.heading(col, text=col.replace("_", " ").title())
            self.tree.column(col, width=width, anchor="w")
        scroll = ttk.Scrollbar(tab, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True, pady=PAD)
        scroll.pack(side="left", fill="y", pady=PAD)

        # Reload the list whenever you switch tabs
        self.tabs.bind("<<NotebookTabChanged>>", lambda e: self.load_subscribers())
        self.load_subscribers()

    def load_subscribers(self):
        subs = storage.load_subscribers()
        self.tree.delete(*self.tree.get_children())
        for s in sorted(subs, key=lambda x: x.get("joined_at", ""), reverse=True):
            name = f"{s.get('first_name', '')} {s.get('last_name', '')}".strip()
            self.tree.insert("", "end", iid=str(s["user_id"]), values=(
                s["user_id"], name, ("@" + s["username"]) if s.get("username") else "",
                s.get("joined_at", ""), "yes" if s.get("greeted") else "no", s.get("status", "active"),
            ))
        active = sum(1 for s in subs if s.get("status", "active") == "active")
        self.subs_count.set(f"{len(subs)} saved · {active} active")

    def export_csv(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="subscribers.csv")
        if not path:
            return
        subs = storage.load_subscribers()
        fields = ["user_id", "first_name", "last_name", "username", "joined_at", "greeted", "status"]
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(subs)
        self.log("ok", f"Exported {len(subs)} users to {path}")

    def remove_selected(self):
        ids = [int(i) for i in self.tree.selection()]
        if ids and messagebox.askyesno("Remove", f"Remove {len(ids)} user(s)? They won't get broadcasts."):
            storage.remove_subscribers(ids)
            self.load_subscribers()

    # ------------------------------------------------------------------
    # Tab: Forward (many links -> your channel)
    # ------------------------------------------------------------------
    def _build_forward_tab(self):
        tab = ttk.Frame(self.tabs, padding=PAD * 2)
        self.tabs.add(tab, text="Forward")

        self.forward_editor = RowsEditor(
            tab, title="", row_title="Messages to send into your channel (one link per row)",
            allow_text=False, with_message_box=False, rows_height=300,
            empty_hint="Paste a message link", allow_files=False,
        )
        self.forward_editor.invalid_hint = "⚠ Not a message link"
        self.forward_editor.pack(fill="both", expand=True)
        # Saved links (older versions stored them as one text block)
        saved = self.settings.get("forward_links", "")
        links = saved.split() if isinstance(saved, str) else saved
        self.forward_editor.set({"items": [{"type": "link", "link": l} for l in links]})

        options = ttk.Frame(tab)
        options.pack(fill="x", pady=PAD)
        ttk.Label(options, text="Send to:").pack(side="left")
        self.fw_target = tk.StringVar(value=self.settings.get("forward_target", ""))
        ttk.Entry(options, textvariable=self.fw_target, width=20).pack(side="left", padx=4)
        ttk.Label(options, text="(empty = your channel)", foreground="gray").pack(side="left")
        self.fw_mode = tk.StringVar(value=self.settings.get("forward_mode", "copy"))
        ttk.Radiobutton(options, text="Copy (no “Forwarded from”)", value="copy",
                        variable=self.fw_mode).pack(side="left", padx=(PAD * 2, 0))
        ttk.Radiobutton(options, text="Forward (keep tag)", value="forward",
                        variable=self.fw_mode).pack(side="left", padx=PAD)

        row = ttk.Frame(tab)
        row.pack(fill="x")
        ttk.Button(row, text="➤ Send to channel", command=self.start_forward).pack(side="left")
        self.fw_cancel_btn = ttk.Button(row, text="Cancel", state="disabled",
                                        command=lambda: setattr(self.forwarder, "cancel", True))
        self.fw_cancel_btn.pack(side="left", padx=PAD)
        ttk.Label(row, text="Pause between batches (s):").pack(side="left", padx=(PAD * 2, 4))
        self.fw_delay = tk.DoubleVar(value=1.0)
        ttk.Spinbox(row, from_=0, to=30, increment=0.5, textvariable=self.fw_delay, width=5).pack(side="left")

        self.fw_bar = ttk.Progressbar(tab, mode="determinate")
        self.fw_bar.pack(fill="x", pady=(PAD, 0))
        self.fw_status = tk.StringVar(value="Rows are sent top to bottom.")
        ttk.Label(tab, textvariable=self.fw_status).pack(anchor="w")

    def start_forward(self):
        if self.forwarder.running or not self._rows_ok(self.forward_editor):
            return
        pairs = []
        for item in self.forward_editor.get()["items"]:
            if item.get("type") == "link":
                pairs.extend(parse_link(item["link"]))
        if not pairs:
            messagebox.showinfo("No links", "Paste at least one message link.")
            return
        self.save_settings(quiet=True)
        target = self.settings["forward_target"] or self.settings["channel"]
        if not target or not self.settings["token"]:
            messagebox.showwarning("Missing settings", "Set the bot token and channel in the ⚙ tab first.")
            return
        batches = len(group_into_batches(pairs))
        mode_text = "copy (no tag)" if self.fw_mode.get() == "copy" else "forward (with tag)"
        if not messagebox.askyesno("Send", f"Send {len(pairs)} message(s) to {target} as {mode_text}?\n"
                                           f"({batches} Telegram call(s))"):
            return
        try:
            delay = max(0.0, float(self.fw_delay.get()))
        except (tk.TclError, ValueError):
            delay = 1.0
        self.fw_bar.configure(value=0)
        self.forwarder.start(pairs, target, self.fw_mode.get(), delay)

    # ------------------------------------------------------------------
    # Tab: ⚙ Settings
    # ------------------------------------------------------------------
    def _build_settings_tab(self):
        tab = ttk.Frame(self.tabs, padding=PAD * 2)
        self.settings_tab = tab
        self.tabs.add(tab, text=" ⚙ ")

        ttk.Label(tab, text="Settings", font=("", 13, "bold")).grid(row=0, column=0, sticky="w", pady=(0, PAD))
        ttk.Label(tab, text="Bot token (from @BotFather)").grid(row=1, column=0, sticky="w")
        self.token_var = tk.StringVar(value=self.settings["token"])
        self.token_entry = ttk.Entry(tab, textvariable=self.token_var, show="•", width=60)
        self.token_entry.grid(row=2, column=0, sticky="we")
        self.show_token = tk.BooleanVar(value=False)
        ttk.Checkbutton(tab, text="Show", variable=self.show_token,
                        command=lambda: self.token_entry.configure(show="" if self.show_token.get() else "•")
                        ).grid(row=2, column=1, padx=PAD)

        ttk.Label(tab, text="Your channel (@username or -100… ID)").grid(row=3, column=0, sticky="w", pady=(PAD * 2, 0))
        self.channel_var = tk.StringVar(value=self.settings["channel"])
        ttk.Entry(tab, textvariable=self.channel_var, width=60).grid(row=4, column=0, sticky="we")

        buttons = ttk.Frame(tab)
        buttons.grid(row=5, column=0, sticky="w", pady=PAD * 2)
        ttk.Button(buttons, text="Save", command=self.save_settings).pack(side="left")
        ttk.Button(buttons, text="Test connection", command=self.test_connection).pack(side="left", padx=PAD)

        help_text = (
            "One-time Telegram setup:\n"
            "1. Add the bot to your channel as Admin with “Add Members / Invite Users via Link”.\n"
            "2. Channel → Invite Links → create a link with “Request Admin Approval” ON, and share it.\n"
            "3. To copy/forward from other channels, the bot must be a member (admin for private channels) there.\n"
            "4. Only run ONE program with this token at a time (not the web dashboard too)."
        )
        ttk.Label(tab, text=help_text, foreground="gray", justify="left").grid(row=6, column=0, columnspan=2, sticky="w")
        tab.columnconfigure(0, weight=1)

    def save_settings(self, quiet=False):
        """Copies everything from the window into settings.json."""
        self.settings["token"] = self.token_var.get().strip()
        self.settings["channel"] = self.channel_var.get().strip()
        self.settings["auto_approve"] = self.auto_approve.get()
        self.settings["send_greeting"] = self.send_greeting.get()
        self.settings["greeting"] = self.greeting_editor.get()
        self.settings["broadcast"] = self.broadcast_editor.get()
        self.settings["forward_links"] = [i["link"] for i in self.forward_editor.get()["items"]
                                          if i.get("type") == "link"]
        self.settings["forward_target"] = self.fw_target.get().strip()
        self.settings["forward_mode"] = self.fw_mode.get()
        storage.save_settings(self.settings)
        if not quiet:
            bad = self.greeting_editor.invalid_rows()
            note = f" (greeting row(s) {', '.join(map(str, bad))} are invalid and were skipped)" if bad else ""
            self.log("ok" if not bad else "error", "Settings saved." + note)
        return True

    def test_connection(self):
        self.save_settings(quiet=True)
        token, channel = self.settings["token"], self.settings["channel"]

        def job():
            try:
                bot = TelegramBot(token)
                me = bot.get_me()
                msg = f"Bot OK: @{me['username']}"
                if channel:
                    chat = bot.get_chat(channel)
                    msg += f" · Channel OK: “{chat.get('title')}” (ID {chat['id']})"
                self.events.put(("log", "ok", msg))
            except TelegramError as e:
                self.events.put(("log", "error", f"Connection test failed: {e}"))

        self.run_in_background(job)

    # ------------------------------------------------------------------
    def on_close(self):
        """Save everything and stop the join bot before closing."""
        try:
            self.save_settings(quiet=True)
        except Exception as e:  # never let a save problem keep the window from closing
            messagebox.showerror("Couldn't save settings", str(e))
        finally:
            self.join_worker.stop()
            self.destroy()


if __name__ == "__main__":
    App().mainloop()
