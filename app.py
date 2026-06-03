#!/usr/bin/env python3
import os
import sys
import time
import urllib.request
import urllib.error
import tkinter as tk
from tkinter import filedialog, scrolledtext

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    root = tk.Tk()
    root.withdraw()
    from tkinter import messagebox
    messagebox.showerror("Missing dependency", "Run:\n\n  pip install watchdog")
    sys.exit(1)


VERSION = "v1.2.2"
AUTHOR = "Gunnthor"
NTFY_SERVER = "https://ntfy.sh"
COOLDOWN_SECONDS = 30   # minimum seconds between alerts for the same file

BG          = "#f1f5f9"
HEADER_BG   = "#1e40af"
HEADER_FG   = "#ffffff"
GUIDE_BG    = "#eff6ff"
GUIDE_BORDER= "#93c5fd"
BTN_BG      = "#2563eb"
BTN_FG      = "#ffffff"
BTN_STOP_BG = "#dc2626"
LOG_BG      = "#ffffff"
LOG_FG      = "#1e293b"
LABEL_FG    = "#1e293b"
MUTED_FG    = "#6b7280"


class FolderHandler(FileSystemEventHandler):
    def __init__(self, folder, topic, log_cb):
        self.folder = os.path.abspath(folder)
        self.topic = topic
        self.log_cb = log_cb
        self._last = {}   # per-file path -> last notified timestamp
        self._dirs = self._scan_dirs()   # immediate subdirs, to spot dir deletes

    def _scan_dirs(self):
        # On Windows a deleted entry reports is_directory=False (it's already
        # gone), so we remember which immediate children are folders.
        dirs = set()
        try:
            for name in os.listdir(self.folder):
                full = os.path.join(self.folder, name)
                if os.path.isdir(full):
                    dirs.add(os.path.abspath(full))
        except OSError:
            pass
        return dirs

    def on_created(self, event):
        if event.is_directory:
            self._dirs.add(os.path.abspath(event.src_path))
            return
        self._handle(event.src_path, "New file")

    def on_modified(self, event):
        if event.is_directory:
            return
        self._handle(event.src_path, "File modified")

    def on_deleted(self, event):
        path = os.path.abspath(event.src_path)
        if event.is_directory or path in self._dirs:
            self._dirs.discard(path)
            return
        self._handle(event.src_path, "File deleted")

    def _handle(self, src_path, action):
        path = os.path.abspath(src_path)
        now = time.time()
        if now - self._last.get(path, 0.0) < COOLDOWN_SECONDS:
            return
        self._last[path] = now
        self._notify(path, action)

    def _notify(self, path, action):
        ts = time.strftime("%H:%M:%S")
        fname = os.path.basename(path)
        tags = {"New file": "sparkles",
                "File modified": "pencil2",
                "File deleted": "wastebasket"}.get(action, "bell")
        body = f"{action} at {ts}\n\n{path}"
        try:
            req = urllib.request.Request(
                f"{NTFY_SERVER}/{self.topic}",
                data=body.encode(),
                headers={
                    "Title": f"{action}: {fname}",
                    "Priority": "high",
                    "Tags": tags,
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
            self.log_cb(f"[{ts}] {action}: {fname} — notification sent.")
        except urllib.error.URLError as e:
            self.log_cb(f"[{ts}] Failed to send: {e}")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("File Change Notifier")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.observer = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self):
        # ── Header ──────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=HEADER_BG)
        hdr.pack(fill="x")
        tk.Label(hdr, text="File Change Notifier", font=("Segoe UI", 15, "bold"),
                 bg=HEADER_BG, fg=HEADER_FG, pady=14).pack(side="left", padx=18)
        tk.Label(hdr, text=VERSION, font=("Segoe UI", 9),
                 bg=HEADER_BG, fg="#93c5fd").pack(side="right", padx=18, pady=14)

        body = tk.Frame(self, bg=BG, padx=22, pady=18)
        body.pack(fill="both")

        # ── Folder picker ───────────────────────────────────────────────
        tk.Label(body, text="Folder to watch", font=("Segoe UI", 9, "bold"),
                 bg=BG, fg=LABEL_FG, anchor="w").grid(row=0, column=0, columnspan=3,
                                                       sticky="w", pady=(0, 4))

        self.file_var = tk.StringVar()
        self.file_entry = tk.Entry(body, textvariable=self.file_var, width=46,
                              font=("Segoe UI", 9), relief="solid", bd=1)
        self.file_entry.grid(row=1, column=0, columnspan=2, sticky="ew", ipady=5, padx=(0, 6))

        tk.Button(body, text="Browse…", command=self._browse,
                  font=("Segoe UI", 9), relief="solid", bd=1,
                  cursor="hand2").grid(row=1, column=2, sticky="ew", ipady=4)

        # Just the folder name shown here (bold) so it's clear at a glance
        self.path_label = tk.Label(body, text="", font=("Segoe UI", 9, "bold"),
                                   bg=BG, fg=LABEL_FG, justify="left", anchor="w",
                                   wraplength=360)
        self.path_label.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self.file_var.trace_add("write", self._on_path_change)

        # ── Subscription input ──────────────────────────────────────────
        tk.Label(body, text="ntfy subscription", font=("Segoe UI", 9, "bold"),
                 bg=BG, fg=LABEL_FG, anchor="w").grid(row=3, column=0, columnspan=3,
                                                       sticky="w", pady=(16, 4))

        self.topic_var = tk.StringVar()
        tk.Entry(body, textvariable=self.topic_var, width=46,
                 font=("Segoe UI", 9), relief="solid", bd=1).grid(
                     row=4, column=0, columnspan=3, sticky="ew", ipady=5)

        # ── Guide box ───────────────────────────────────────────────────
        guide_frame = tk.Frame(body, bg=GUIDE_BG, highlightbackground=GUIDE_BORDER,
                               highlightthickness=1)
        guide_frame.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(18, 0))

        guide_text = (
            "How to receive notifications on your phone\n\n"
            "1.  Download the ntfy app\n"
            "      Android: Google Play  ·  iOS: App Store\n"
            "      Or visit:  ntfy.sh/app\n\n"
            "2.  Open the app and tap  Subscribe to topic\n\n"
            "3.  Enter the subscription name from the field above\n\n"
            "That's it — you'll get a push notification\n"
            "whenever a file is created, changed, or deleted in the folder."
        )
        tk.Label(guide_frame, text=guide_text, font=("Segoe UI", 9),
                 bg=GUIDE_BG, fg="#1e3a8a", justify="left",
                 padx=14, pady=12, anchor="w").pack(fill="both")

        # ── Start / Stop button ─────────────────────────────────────────
        self.btn = tk.Button(body, text="Start Watching", command=self._toggle,
                             font=("Segoe UI", 10, "bold"), bg=BTN_BG, fg=BTN_FG,
                             activebackground="#1d4ed8", activeforeground=BTN_FG,
                             relief="flat", cursor="hand2", pady=9)
        self.btn.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(18, 0))

        # ── Status log ──────────────────────────────────────────────────
        tk.Label(body, text="Status", font=("Segoe UI", 9, "bold"),
                 bg=BG, fg=LABEL_FG, anchor="w").grid(row=7, column=0, columnspan=3,
                                                       sticky="w", pady=(16, 4))

        self.log = scrolledtext.ScrolledText(body, height=7, width=55,
                                             font=("Consolas", 8),
                                             bg=LOG_BG, fg=LOG_FG,
                                             relief="solid", bd=1, state="disabled")
        self.log.grid(row=8, column=0, columnspan=3, sticky="ew")

        # ── Footer ──────────────────────────────────────────────────────
        tk.Label(body,
                 text=f"Author: {AUTHOR}  ·  Built with Claude Code  ·  {VERSION}",
                 font=("Segoe UI", 8), bg=BG, fg=MUTED_FG).grid(
                     row=9, column=0, columnspan=3, pady=(14, 0))

        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

    def _browse(self):
        path = filedialog.askdirectory(title="Select folder to watch")
        if path:
            self.file_var.set(path)

    def _on_path_change(self, *_):
        # Show just the folder name (bold) below the entry, and keep the entry
        # scrolled to the end so the folder name stays visible for long paths.
        path = self.file_var.get().strip()
        name = os.path.basename(path.rstrip("/\\")) or path
        self.path_label.config(text=name)
        self.file_entry.xview_moveto(1.0)

    def _toggle(self):
        if self.observer and self.observer.is_alive():
            self._stop()
        else:
            self._start()

    def _start(self):
        folder = self.file_var.get().strip()
        topic = self.topic_var.get().strip()

        if not folder:
            self._append("Select a folder first.")
            return
        if not os.path.isdir(folder):
            self._append("Folder not found — check the path.")
            return
        if not topic:
            self._append("Enter an ntfy subscription first.")
            return

        handler = FolderHandler(folder, topic, self._log_from_thread)
        self.observer = Observer()
        self.observer.schedule(handler, path=folder, recursive=False)
        self.observer.start()

        self._append(f"Watching:  {folder}")
        self._append(f"Notifying: {NTFY_SERVER}/{topic}")
        self.btn.config(text="Stop Watching", bg=BTN_STOP_BG,
                        activebackground="#b91c1c")

    def _stop(self):
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None
        self._append("Stopped.")
        self.btn.config(text="Start Watching", bg=BTN_BG,
                        activebackground="#1d4ed8")

    def _log_from_thread(self, msg):
        self.after(0, self._append, msg)

    def _append(self, msg):
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _on_close(self):
        self._stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
