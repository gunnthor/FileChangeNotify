#!/usr/bin/env python3
"""Notifier — push an ntfy notification when a folder changes or when new rows
are inserted into a SQL Server table. Pick the mode in the window."""
import os
import sys
import ssl
import json
import time
import threading
import urllib.request
import urllib.error
import tkinter as tk
from tkinter import filedialog, scrolledtext, messagebox

# Folder mode needs watchdog; SQL mode needs pyodbc. Import each independently so
# the app still runs (with that mode disabled) if one is missing. In the bundled
# exe both are present.
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    _HAS_WATCHDOG = True
except ImportError:
    _HAS_WATCHDOG = False
    FileSystemEventHandler = object   # so FolderHandler can still be defined

try:
    import pyodbc
    _HAS_PYODBC = True
except ImportError:
    _HAS_PYODBC = False

if not _HAS_WATCHDOG and not _HAS_PYODBC:
    _root = tk.Tk()
    _root.withdraw()
    messagebox.showerror(
        "Missing dependencies",
        "Install at least one monitor backend:\n\n  pip install watchdog pyodbc")
    sys.exit(1)

# Verify TLS against the OS (Windows) certificate store so sending works behind a
# corporate TLS-inspecting proxy whose root CA Windows trusts. Degrades gracefully.
try:
    import truststore
    truststore.inject_into_ssl()
    _OS_TRUST = True
except Exception:
    _OS_TRUST = False

# System-tray support (minimize to tray). Optional — without it the window just
# minimizes to the taskbar as usual.
try:
    import pystray
    from PIL import Image, ImageDraw
    _HAS_TRAY = True
except Exception:
    _HAS_TRAY = False


VERSION = "v2.3.0"
AUTHOR = "Gunnthor"
NTFY_SERVER = "https://ntfy.sh"
COOLDOWN_SECONDS = 30          # minimum seconds between notifications
DEFAULT_INTERVAL = 30          # seconds between SQL polls
MAX_INCLUDE_VALUES = 10        # max field values to list in one notification

# Preferred SQL Server ODBC drivers, best first. We use whichever is installed so
# the app works on machines without the newest driver (e.g. an AX/D365 VM that only
# has Driver 17 or the SQL Server Native Client).
DRIVER_PREFERENCE = [
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 13.1 for SQL Server",
    "ODBC Driver 13 for SQL Server",
    "ODBC Driver 11 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server Native Client 10.0",
    "SQL Server",
]

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

GUIDE_TEXT = (
    "Receive notifications on your phone\n"
    "   1.  Install the ntfy app  (Android: Google Play · iOS: App Store · ntfy.sh/app)\n"
    "   2.  Open it and tap  Subscribe to topic\n"
    "   3.  Enter a subscription name, then type that same name in the\n"
    "         “ntfy subscription” field here.\n"
    "\n"
    "What you can monitor\n"
    "   •  Folder — a push when a file is created, changed, or deleted in\n"
    "         the folder you pick.\n"
    "   •  SQL Server — a push when new rows are inserted into a table.\n"
    "\n"
    "SQL Server notes\n"
    "   •  Connects with Windows authentication; your account needs SELECT\n"
    "         on the table.\n"
    "   •  Uses whichever SQL ODBC driver is installed (auto-detected).\n"
    "   •  “Trust SQL Server certificate” — leave on for a local/self-signed\n"
    "         instance.\n"
    "\n"
    "Behind a corporate proxy?\n"
    "   If notifications fail with a certificate error, tick\n"
    "   “Ignore certificate errors when sending”."
)


# ── Helpers ──────────────────────────────────────────────────────────────
def _ts():
    return time.strftime("%H:%M:%S")


def _clean_err(e):
    msg = str(e)
    return msg if len(msg) <= 200 else msg[:200] + "…"


def _make_tray_image():
    """A simple blue bell icon for the system tray (drawn, no asset file)."""
    img = Image.new("RGB", (64, 64), HEADER_BG)
    d = ImageDraw.Draw(img)
    d.pieslice((18, 12, 46, 38), 180, 360, fill="white")   # bell dome
    d.rectangle((18, 25, 46, 40), fill="white")            # bell body
    d.rectangle((15, 40, 49, 45), fill="white")            # rim
    d.ellipse((28, 45, 36, 53), fill="white")              # clapper
    return img


def pick_driver():
    """Return the best installed SQL Server ODBC driver, or None."""
    available = pyodbc.drivers()
    for d in DRIVER_PREFERENCE:
        if d in available:
            return d
    for d in available:                 # any other SQL Server driver
        if "SQL Server" in d:
            return d
    return None


def quote_ident(name):
    """Bracket-quote a SQL identifier (replicates T-SQL QUOTENAME)."""
    return "[" + name.replace("]", "]]") + "]"


def split_table(text):
    """Parse 'schema.table' / '[schema].[table]' / 'table' -> (schema, table)."""
    text = text.strip()
    if "." in text:
        schema, _, table = text.rpartition(".")
    else:
        schema, table = "dbo", text
    schema = schema.strip().strip("[]") or "dbo"
    table = table.strip().strip("[]")
    return schema, table


def build_conn_str(server, database, trust_cert, driver):
    """Windows-auth connection string for the chosen ODBC driver."""
    parts = [
        f"DRIVER={{{driver}}}",
        f"SERVER={server}",
        f"DATABASE={database}",
        "Trusted_Connection=yes",
    ]
    # Encrypt/TrustServerCertificate are only understood by the modern
    # "ODBC Driver NN for SQL Server" family; older drivers reject them.
    if driver.startswith("ODBC Driver"):
        parts.append("Encrypt=yes")
        parts.append(f"TrustServerCertificate={'yes' if trust_cert else 'no'}")
    return ";".join(parts) + ";"


def resolve_table(cur, schema, table):
    """Return the server's canonical (schema, table) if it exists, else None.

    The user's text is passed only as parameters to OBJECT_ID(QUOTENAME(?)...),
    so it can never be injected into the query.
    """
    cur.execute(
        "SELECT s.name, t.name FROM sys.tables t "
        "JOIN sys.schemas s ON s.schema_id = t.schema_id "
        "WHERE t.object_id = OBJECT_ID(QUOTENAME(?) + N'.' + QUOTENAME(?))",
        schema, table,
    )
    row = cur.fetchone()
    return (row[0], row[1]) if row else None


def detect_identity_column(cur, schema, table):
    """Return the table's IDENTITY column name, or None."""
    cur.execute(
        "SELECT c.name FROM sys.identity_columns c "
        "WHERE c.object_id = OBJECT_ID(QUOTENAME(?) + N'.' + QUOTENAME(?))",
        schema, table,
    )
    row = cur.fetchone()
    return row[0] if row else None


def send_ntfy(topic, title, body, tags, insecure=False):
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{topic}",
        data=body.encode(),
        headers={"Title": title, "Priority": "high", "Tags": tags},
        method="POST",
    )
    ctx = None
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    urllib.request.urlopen(req, timeout=10, context=ctx)


def settings_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Notifier", "settings.json")


def load_settings():
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data):
    try:
        path = settings_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


class MonitorError(Exception):
    """A permanent problem that should stop the monitor (e.g. bad table)."""


# ── Folder monitor (watchdog) ─────────────────────────────────────────────
class FolderHandler(FileSystemEventHandler):
    def __init__(self, folder, topic, insecure, log_cb):
        self.folder = os.path.abspath(folder)
        self.topic = topic
        self.insecure = insecure
        self.log_cb = log_cb
        self._last = {}                  # per-file path -> last notified timestamp
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
        ts = _ts()
        fname = os.path.basename(path)
        tags = {"New file": "sparkles",
                "File modified": "pencil2",
                "File deleted": "wastebasket"}.get(action, "bell")
        body = f"{action} at {ts}\n\n{path}"
        try:
            send_ntfy(self.topic, f"{action}: {fname}", body, tags, self.insecure)
            self.log_cb(f"[{ts}] {action}: {fname} — notification sent.")
        except urllib.error.URLError as e:
            self.log_cb(f"[{ts}] Failed to send: {e}")


# ── SQL monitor (pyodbc polling, runs on a worker thread) ─────────────────
class SqlMonitor:
    def __init__(self, server, database, schema, table, topic, interval,
                 trust_cert, key_column, log_cb, done_cb, filter_expr=None,
                 include_col=None, insecure_tls=False):
        self.server = server
        self.database = database
        self.schema_in = schema
        self.table_in = table
        self.topic = topic
        self.interval = interval
        self.trust_cert = trust_cert
        self.key_in = key_column
        self.log_cb = log_cb
        self.done_cb = done_cb
        self.filter_expr = filter_expr or None
        self.include_col = include_col or None
        self.insecure_tls = insecure_tls

        self._stop = threading.Event()
        self.stopped_by_user = False
        self._conn = None

        self.schema = None
        self.table = None
        self.key_col = None
        self.mode = None          # "hwm" or "count"
        self.last_max = None
        self.last_count = None
        self.baselined = False
        self._last_notified = 0.0

    # -- lifecycle --
    def stop(self):
        self.stopped_by_user = True
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                if not self.baselined:
                    self._baseline()
                else:
                    self._check()
            except MonitorError as e:
                self.log_cb(str(e))
                break
            except pyodbc.Error as e:
                self.log_cb(f"[{_ts()}] DB error: {_clean_err(e)} "
                            f"— retrying in {self.interval}s")
                self._close()
            self._stop.wait(self.interval)
        self._close()
        self.done_cb()

    def _close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except pyodbc.Error:
                pass
            self._conn = None

    def _conn_cursor(self):
        if self._conn is None:
            driver = pick_driver()
            if driver is None:
                raise MonitorError(
                    f"[{_ts()}] No SQL Server ODBC driver found — install "
                    f"'ODBC Driver 18 for SQL Server'."
                )
            try:
                conn = pyodbc.connect(
                    build_conn_str(self.server, self.database,
                                   self.trust_cert, driver),
                    timeout=10,
                )
            except pyodbc.Error as e:
                if "IM002" in str(e):
                    raise MonitorError(
                        f"[{_ts()}] ODBC driver '{driver}' could not be loaded "
                        f"— try installing 'ODBC Driver 18 for SQL Server'."
                    )
                raise
            self._conn = conn
            self.log_cb(f"[{_ts()}] Connected using ODBC driver: {driver}")
        return self._conn.cursor()

    def _scalar(self, cur, sql, *params):
        cur.execute(sql, *params)
        return cur.fetchone()[0]

    @property
    def _qtable(self):
        return f"{quote_ident(self.schema)}.{quote_ident(self.table)}"

    def _filter_where(self):
        """' WHERE (<filter>)' when a filter is set, else ''."""
        return f" WHERE ({self.filter_expr})" if self.filter_expr else ""

    def _filter_and(self):
        """' AND (<filter>)' when a filter is set, else ''."""
        return f" AND ({self.filter_expr})" if self.filter_expr else ""

    @staticmethod
    def _fmt_val(v):
        if v is None:
            return "NULL"
        s = str(v).strip()
        return s if len(s) <= 120 else s[:120] + "…"

    # -- polling --
    def _baseline(self):
        cur = self._conn_cursor()
        resolved = resolve_table(cur, self.schema_in, self.table_in)
        if not resolved:
            raise MonitorError(
                f"[{_ts()}] Table not found: {self.schema_in}.{self.table_in}"
            )
        self.schema, self.table = resolved

        if self.filter_expr:
            try:
                self._scalar(cur,
                             f"SELECT COUNT(*) FROM {self._qtable}{self._filter_where()}")
            except pyodbc.Error as e:
                raise MonitorError(f"[{_ts()}] Invalid filter: {_clean_err(e)}")
            self.log_cb(f"[{_ts()}] Filter active: {self.filter_expr}")

        self.key_col = self.key_in or detect_identity_column(
            cur, self.schema, self.table)

        if self.include_col:
            if not self.key_col:
                self.log_cb(f"[{_ts()}] Note: including a column's value needs a key "
                            f"column — skipping it in row-count mode.")
                self.include_col = None
            else:
                try:
                    cur.execute(f"SELECT {quote_ident(self.include_col)} "
                                f"FROM {self._qtable} WHERE 1 = 0")
                except pyodbc.Error as e:
                    raise MonitorError(f"[{_ts()}] Column not found: "
                                       f"{self.include_col} ({_clean_err(e)})")
                self.log_cb(f"[{_ts()}] Including column in messages: {self.include_col}")

        if self.key_col:
            self.mode = "hwm"
            self.last_max = self._scalar(
                cur, f"SELECT MAX({quote_ident(self.key_col)}) FROM {self._qtable}")
            shown = self.last_max if self.last_max is not None else "(empty)"
            self.log_cb(f"[{_ts()}] Baseline: MAX({self.key_col})={shown} "
                        f"in {self.schema}.{self.table} — watching for new rows…")
        else:
            self.mode = "count"
            self.last_count = self._scalar(
                cur, f"SELECT COUNT(*) FROM {self._qtable}{self._filter_where()}")
            self.log_cb(f"[{_ts()}] No identity column — using row-count mode "
                        f"(may miss inserts if rows are also deleted).")
            self.log_cb(f"[{_ts()}] Baseline: {self.last_count} rows in "
                        f"{self.schema}.{self.table} — watching for new rows…")
        self.baselined = True

    def _fetch_values(self, cur):
        """The chosen column's values for the new rows (newest first, capped)."""
        col = quote_ident(self.include_col)
        key = quote_ident(self.key_col)
        if self.last_max is None:
            cur.execute(f"SELECT TOP ({MAX_INCLUDE_VALUES}) {col} FROM {self._qtable}"
                        f"{self._filter_where()} ORDER BY {key} DESC")
        else:
            cur.execute(f"SELECT TOP ({MAX_INCLUDE_VALUES}) {col} FROM {self._qtable} "
                        f"WHERE {key} > ?{self._filter_and()} ORDER BY {key} DESC",
                        self.last_max)
        return [self._fmt_val(r[0]) for r in cur.fetchall()]

    def _check(self):
        cur = self._conn_cursor()
        if self.mode == "hwm":
            self._check_hwm(cur)
        else:
            self._check_count(cur)

    def _check_hwm(self, cur):
        key = quote_ident(self.key_col)
        if self.last_max is None:
            n = self._scalar(
                cur, f"SELECT COUNT(*) FROM {self._qtable}{self._filter_where()}")
        else:
            n = self._scalar(
                cur, f"SELECT COUNT(*) FROM {self._qtable} WHERE {key} > ?"
                     f"{self._filter_and()}",
                self.last_max)
        if n <= 0:
            return
        if not self._cooldown_ok():
            return                      # leave last_max; report cumulatively next time
        # Fetch the chosen column's values BEFORE advancing the watermark.
        values = self._fetch_values(cur) if self.include_col else None
        # Advance over ALL rows (keys are monotonic) so we only inspect newer ones.
        new_max = self._scalar(cur, f"SELECT MAX({key}) FROM {self._qtable}")
        self._notify(n, f"Latest {self.key_col} = {new_max}", values)
        self.last_max = new_max

    def _check_count(self, cur):
        current = self._scalar(
            cur, f"SELECT COUNT(*) FROM {self._qtable}{self._filter_where()}")
        new = current - self.last_count
        if new > 0:
            if not self._cooldown_ok():
                return                  # leave last_count; report cumulatively next time
            self._notify(new, f"Row count: {self.last_count} → {current}")
            self.last_count = current
        else:
            self.last_count = current   # track deletions / no change

    def _cooldown_ok(self):
        now = time.time()
        if now - self._last_notified < COOLDOWN_SECONDS:
            return False
        self._last_notified = now
        return True

    def _notify(self, n, detail, values=None):
        ts = _ts()
        title = f"New rows in {self.schema}.{self.table}"
        body = f"{n} new row(s) in {self.schema}.{self.table} at {ts}\n\n{detail}"
        if values:
            lines = "\n".join(f"  • {v}" for v in values)
            body += f"\n\n{self.include_col}:\n{lines}"
            if n > len(values):
                body += f"\n  …and {n - len(values)} more"
        if self.filter_expr:
            body += f"\nFilter: {self.filter_expr}"
        try:
            send_ntfy(self.topic, title, body, "inbox_tray", self.insecure_tls)
            self.log_cb(f"[{ts}] {n} new row(s) — notification sent.")
        except urllib.error.URLError as e:
            self.log_cb(f"[{ts}] Failed to send: {e}")


# ── GUI ──────────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Notifier")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.observer = None        # active folder Observer
        self.monitor = None         # active SqlMonitor
        self.thread = None          # SqlMonitor worker thread
        self._guide_win = None
        self._tray = None           # pystray Icon (created lazily on first minimize)
        self._build()
        self._switch_mode()
        self._set_running(False)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if _HAS_TRAY:
            self.bind("<Unmap>", self._on_unmap)
        self._maybe_show_get_started()

    def _build(self):
        # ── Header ──────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=HEADER_BG)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Notifier", font=("Segoe UI", 15, "bold"),
                 bg=HEADER_BG, fg=HEADER_FG, pady=14).pack(side="left", padx=18)
        tk.Label(hdr, text=VERSION, font=("Segoe UI", 9),
                 bg=HEADER_BG, fg="#93c5fd").pack(side="right", padx=18, pady=14)
        tk.Button(hdr, text="Get started", command=self._show_get_started,
                  font=("Segoe UI", 9), bg="#1d4ed8", fg=HEADER_FG,
                  activebackground="#1e3a8a", activeforeground=HEADER_FG,
                  relief="flat", cursor="hand2", padx=10, pady=4).pack(
                      side="right", padx=(0, 6), pady=10)

        body = tk.Frame(self, bg=BG, padx=22, pady=18)
        body.pack(fill="both")

        # ── Mode selector ───────────────────────────────────────────────
        default_mode = "folder" if _HAS_WATCHDOG else "sql"
        self.mode_var = tk.StringVar(value=default_mode)
        mode_row = tk.Frame(body, bg=BG)
        mode_row.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        tk.Label(mode_row, text="Monitor:", font=("Segoe UI", 9, "bold"),
                 bg=BG, fg=LABEL_FG).pack(side="left", padx=(0, 12))
        self.folder_radio = tk.Radiobutton(
            mode_row, text="Folder", variable=self.mode_var, value="folder",
            command=self._switch_mode, bg=BG, fg=LABEL_FG, activebackground=BG,
            selectcolor=BG, font=("Segoe UI", 9), cursor="hand2")
        self.folder_radio.pack(side="left")
        self.sql_radio = tk.Radiobutton(
            mode_row, text="SQL Server", variable=self.mode_var, value="sql",
            command=self._switch_mode, bg=BG, fg=LABEL_FG, activebackground=BG,
            selectcolor=BG, font=("Segoe UI", 9), cursor="hand2")
        self.sql_radio.pack(side="left", padx=(14, 0))

        # ── Folder fields ───────────────────────────────────────────────
        self.folder_frame = tk.Frame(body, bg=BG)
        self.folder_var = tk.StringVar()
        tk.Label(self.folder_frame, text="Folder to watch",
                 font=("Segoe UI", 9, "bold"), bg=BG, fg=LABEL_FG,
                 anchor="w").grid(row=0, column=0, columnspan=3, sticky="w",
                                  pady=(0, 4))
        self.file_entry = tk.Entry(self.folder_frame, textvariable=self.folder_var,
                                   width=46, font=("Segoe UI", 9), relief="solid",
                                   bd=1)
        self.file_entry.grid(row=1, column=0, columnspan=2, sticky="ew",
                             ipady=5, padx=(0, 6))
        tk.Button(self.folder_frame, text="Browse…", command=self._browse,
                  font=("Segoe UI", 9), relief="solid", bd=1,
                  cursor="hand2").grid(row=1, column=2, sticky="ew", ipady=4)
        self.path_label = tk.Label(self.folder_frame, text="",
                                   font=("Segoe UI", 9, "bold"), bg=BG, fg=LABEL_FG,
                                   justify="left", anchor="w", wraplength=360)
        self.path_label.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self.folder_var.trace_add("write", self._on_path_change)
        self.folder_frame.columnconfigure(0, weight=1)
        self.folder_frame.columnconfigure(1, weight=1)

        # ── SQL fields ──────────────────────────────────────────────────
        self.sql_frame = tk.Frame(body, bg=BG)
        self.server_var = tk.StringVar()
        self.db_var = tk.StringVar()
        self.table_var = tk.StringVar()
        self.key_var = tk.StringVar()
        self.interval_var = tk.StringVar(value=str(DEFAULT_INTERVAL))
        self.trust_var = tk.BooleanVar(value=True)
        self.filter_var = tk.StringVar()
        self.include_var = tk.StringVar()

        def sfield(row, label, var):
            tk.Label(self.sql_frame, text=label, font=("Segoe UI", 9, "bold"),
                     bg=BG, fg=LABEL_FG, anchor="w").grid(
                         row=row, column=0, columnspan=3, sticky="w",
                         pady=(12 if row else 0, 4))
            tk.Entry(self.sql_frame, textvariable=var, width=46,
                     font=("Segoe UI", 9), relief="solid", bd=1).grid(
                         row=row + 1, column=0, columnspan=3, sticky="ew", ipady=5)

        sfield(0, "SQL Server  (e.g. localhost or .\\SQLEXPRESS)", self.server_var)
        sfield(2, "Database", self.db_var)
        sfield(4, "Table  (schema.table)", self.table_var)
        sfield(6, "Key column  (optional — auto-detects identity)", self.key_var)

        sopts = tk.Frame(self.sql_frame, bg=BG)
        sopts.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        line1 = tk.Frame(sopts, bg=BG)
        line1.pack(fill="x", anchor="w")
        tk.Label(line1, text="Poll every", font=("Segoe UI", 9), bg=BG,
                 fg=LABEL_FG).pack(side="left")
        tk.Entry(line1, textvariable=self.interval_var, width=5,
                 font=("Segoe UI", 9), relief="solid", bd=1, justify="center").pack(
                     side="left", padx=(6, 4))
        tk.Label(line1, text="seconds", font=("Segoe UI", 9), bg=BG,
                 fg=LABEL_FG).pack(side="left")
        tk.Checkbutton(sopts, text="Trust SQL Server certificate (local/self-signed)",
                       variable=self.trust_var, bg=BG, fg=LABEL_FG,
                       activebackground=BG, font=("Segoe UI", 9),
                       anchor="w").pack(fill="x", anchor="w", pady=(6, 0))

        # ── Advanced (collapsible) ──────────────────────────────────────
        self.adv_open = False
        self.adv_btn = tk.Button(self.sql_frame, text="Advanced  ▸",
                                 command=self._toggle_advanced, font=("Segoe UI", 9),
                                 bg=BG, fg=BTN_BG, activebackground=BG,
                                 activeforeground=BTN_BG, relief="flat", bd=0,
                                 cursor="hand2", anchor="w", padx=0)
        self.adv_btn.grid(row=9, column=0, columnspan=3, sticky="w", pady=(12, 0))

        self.advanced_frame = tk.Frame(self.sql_frame, bg=BG)
        self.advanced_frame.grid(row=10, column=0, columnspan=3, sticky="ew")
        tk.Label(self.advanced_frame,
                 text="Only count rows matching this condition (SQL WHERE)",
                 font=("Segoe UI", 9, "bold"), bg=BG, fg=LABEL_FG,
                 anchor="w").grid(row=0, column=0, columnspan=3, sticky="w",
                                  pady=(6, 4))
        tk.Entry(self.advanced_frame, textvariable=self.filter_var, width=46,
                 font=("Segoe UI", 9), relief="solid", bd=1).grid(
                     row=1, column=0, columnspan=3, sticky="ew", ipady=5)
        tk.Label(self.advanced_frame,
                 text="e.g.   Status = 'Error'      or      LogType IN ('Error','Warning')",
                 font=("Segoe UI", 8), bg=BG, fg=MUTED_FG, anchor="w").grid(
                     row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        tk.Button(self.advanced_frame, text="Test filter", command=self._test_filter,
                  font=("Segoe UI", 9), relief="solid", bd=1, cursor="hand2").grid(
                      row=3, column=0, sticky="w", pady=(8, 0), ipadx=6, ipady=2)
        tk.Label(self.advanced_frame,
                 text="Include this column's value in the message (optional)",
                 font=("Segoe UI", 9, "bold"), bg=BG, fg=LABEL_FG,
                 anchor="w").grid(row=4, column=0, columnspan=3, sticky="w",
                                  pady=(14, 4))
        tk.Entry(self.advanced_frame, textvariable=self.include_var, width=46,
                 font=("Segoe UI", 9), relief="solid", bd=1).grid(
                     row=5, column=0, columnspan=3, sticky="ew", ipady=5)
        tk.Label(self.advanced_frame,
                 text="e.g.   ErrorMessage   — the value from each new row is added "
                      "to the push (needs a key/identity column)",
                 font=("Segoe UI", 8), bg=BG, fg=MUTED_FG, anchor="w",
                 wraplength=360, justify="left").grid(
                     row=6, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.advanced_frame.columnconfigure(0, weight=1)
        self.advanced_frame.columnconfigure(1, weight=1)
        self.advanced_frame.grid_remove()       # collapsed by default

        self.sql_frame.columnconfigure(0, weight=1)
        self.sql_frame.columnconfigure(1, weight=1)

        # Both field frames occupy the same grid cell; _switch_mode shows one.
        self.folder_frame.grid(row=1, column=0, columnspan=3, sticky="ew")
        self.sql_frame.grid(row=1, column=0, columnspan=3, sticky="ew")

        # ── Common: subscription + send-TLS option ──────────────────────
        tk.Label(body, text="ntfy subscription", font=("Segoe UI", 9, "bold"),
                 bg=BG, fg=LABEL_FG, anchor="w").grid(row=2, column=0, columnspan=3,
                                                       sticky="w", pady=(16, 4))
        self.topic_var = tk.StringVar()
        tk.Entry(body, textvariable=self.topic_var, width=46, font=("Segoe UI", 9),
                 relief="solid", bd=1).grid(row=3, column=0, columnspan=3,
                                            sticky="ew", ipady=5)

        self.insecure_var = tk.BooleanVar(value=False)
        tk.Checkbutton(body,
                       text="Ignore certificate errors when sending (corporate TLS proxy)",
                       variable=self.insecure_var, bg=BG, fg=LABEL_FG,
                       activebackground=BG, font=("Segoe UI", 9),
                       anchor="w").grid(row=4, column=0, columnspan=3,
                                        sticky="w", pady=(8, 0))

        # ── Start / Stop button ─────────────────────────────────────────
        self.btn = tk.Button(body, text="Start Monitoring", command=self._toggle,
                             font=("Segoe UI", 10, "bold"), bg=BTN_BG, fg=BTN_FG,
                             activebackground="#1d4ed8", activeforeground=BTN_FG,
                             relief="flat", cursor="hand2", pady=9)
        self.btn.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(16, 0))

        # ── Status log ──────────────────────────────────────────────────
        tk.Label(body, text="Status", font=("Segoe UI", 9, "bold"),
                 bg=BG, fg=LABEL_FG, anchor="w").grid(row=6, column=0, columnspan=3,
                                                       sticky="w", pady=(16, 4))
        self.log = scrolledtext.ScrolledText(body, height=7, width=55,
                                             font=("Consolas", 8),
                                             bg=LOG_BG, fg=LOG_FG,
                                             relief="solid", bd=1, state="disabled")
        self.log.grid(row=7, column=0, columnspan=3, sticky="ew")

        # ── Footer ──────────────────────────────────────────────────────
        tk.Label(body,
                 text=f"Author: {AUTHOR}  ·  Built with Claude Code  ·  {VERSION}",
                 font=("Segoe UI", 8), bg=BG, fg=MUTED_FG).grid(
                     row=8, column=0, columnspan=3, pady=(14, 0))

        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

    # ── Mode switching ──────────────────────────────────────────────────
    def _switch_mode(self, *_):
        if self.mode_var.get() == "folder":
            self.sql_frame.grid_remove()
            self.folder_frame.grid()
        else:
            self.folder_frame.grid_remove()
            self.sql_frame.grid()
        self.update_idletasks()

    def _set_running(self, running):
        if running:
            self.btn.config(text="Stop Monitoring", bg=BTN_STOP_BG,
                            activebackground="#b91c1c")
            self.folder_radio.config(state="disabled")
            self.sql_radio.config(state="disabled")
        else:
            self.btn.config(text="Start Monitoring", bg=BTN_BG,
                            activebackground="#1d4ed8")
            self.folder_radio.config(state="normal" if _HAS_WATCHDOG else "disabled")
            self.sql_radio.config(state="normal" if _HAS_PYODBC else "disabled")

    # ── Get started dialog ──────────────────────────────────────────────
    def _maybe_show_get_started(self):
        settings = load_settings()
        if not settings.get("getstarted_shown"):
            self.after(400, self._show_get_started)
            settings["getstarted_shown"] = True
            save_settings(settings)

    def _show_get_started(self):
        if self._guide_win is not None and self._guide_win.winfo_exists():
            self._guide_win.lift()
            return
        win = tk.Toplevel(self)
        self._guide_win = win
        win.title("Get started")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.transient(self)
        hdr = tk.Frame(win, bg=HEADER_BG)
        hdr.pack(fill="x")
        tk.Label(hdr, text="Get started with Notifier", font=("Segoe UI", 13, "bold"),
                 bg=HEADER_BG, fg=HEADER_FG, pady=12, padx=16).pack(side="left")
        frm = tk.Frame(win, bg=BG, padx=20, pady=16)
        frm.pack(fill="both")
        tk.Label(frm, text=GUIDE_TEXT, font=("Segoe UI", 9), bg=BG, fg=LABEL_FG,
                 justify="left", anchor="w").pack(fill="both")
        tk.Button(frm, text="Got it", command=win.destroy, font=("Segoe UI", 9, "bold"),
                  bg=BTN_BG, fg=BTN_FG, activebackground="#1d4ed8",
                  activeforeground=BTN_FG, relief="flat", cursor="hand2",
                  pady=6, padx=16).pack(anchor="e", pady=(14, 0))
        try:
            win.grab_set()
        except tk.TclError:
            pass

    # ── Actions ─────────────────────────────────────────────────────────
    def _toggle_advanced(self):
        self.adv_open = not self.adv_open
        if self.adv_open:
            self.advanced_frame.grid()
            self.adv_btn.config(text="Advanced  ▾")
        else:
            self.advanced_frame.grid_remove()
            self.adv_btn.config(text="Advanced  ▸")
        self.update_idletasks()

    def _test_filter(self):
        if not _HAS_PYODBC:
            self._append("SQL support not available.")
            return
        server = self.server_var.get().strip()
        database = self.db_var.get().strip()
        table = self.table_var.get().strip()
        filt = self.filter_var.get().strip()
        if not (server and database and table):
            self._append("Fill in SQL Server, database and table first.")
            return
        if not filt:
            self._append("Enter a filter to test.")
            return
        trust = self.trust_var.get()
        schema, tbl = split_table(table)

        def work():
            conn = None
            try:
                driver = pick_driver()
                if driver is None:
                    self._log_from_thread(f"[{_ts()}] No SQL Server ODBC driver found.")
                    return
                conn = pyodbc.connect(
                    build_conn_str(server, database, trust, driver), timeout=10)
                cur = conn.cursor()
                resolved = resolve_table(cur, schema, tbl)
                if not resolved:
                    self._log_from_thread(f"[{_ts()}] Table not found: {schema}.{tbl}")
                    return
                sname, tname = resolved
                qt = f"{quote_ident(sname)}.{quote_ident(tname)}"
                cur.execute(f"SELECT COUNT(*) FROM {qt} WHERE ({filt})")
                n = cur.fetchone()[0]
                self._log_from_thread(f"[{_ts()}] Filter OK — matches {n} row(s) right now.")
            except pyodbc.Error as e:
                self._log_from_thread(f"[{_ts()}] Filter test failed: {_clean_err(e)}")
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except pyodbc.Error:
                        pass

        threading.Thread(target=work, daemon=True).start()

    def _browse(self):
        path = filedialog.askdirectory(title="Select folder to watch")
        if path:
            self.folder_var.set(path)

    def _on_path_change(self, *_):
        # Show just the folder name (bold) below the entry, and keep the entry
        # scrolled to the end so the folder name stays visible for long paths.
        path = self.folder_var.get().strip()
        name = os.path.basename(path.rstrip("/\\")) or path
        self.path_label.config(text=name)
        self.file_entry.xview_moveto(1.0)

    def _toggle(self):
        if self._is_running():
            self._stop()
        else:
            self._start()

    def _is_running(self):
        if self.observer is not None:
            return True
        return self.thread is not None and self.thread.is_alive()

    def _start(self):
        topic = self.topic_var.get().strip()
        if not topic:
            self._append("Enter an ntfy subscription first.")
            return
        insecure = self.insecure_var.get()

        if self.mode_var.get() == "folder":
            folder = self.folder_var.get().strip()
            if not folder:
                self._append("Select a folder first.")
                return
            if not os.path.isdir(folder):
                self._append("Folder not found — check the path.")
                return
            handler = FolderHandler(folder, topic, insecure, self._log_from_thread)
            self.observer = Observer()
            self.observer.schedule(handler, path=folder, recursive=False)
            self.observer.start()
            self._append(f"Watching folder: {folder}")
        else:
            server = self.server_var.get().strip()
            database = self.db_var.get().strip()
            table = self.table_var.get().strip()
            key = self.key_var.get().strip() or None
            if not server:
                self._append("Enter the SQL Server first.")
                return
            if not database:
                self._append("Enter the database first.")
                return
            if not table:
                self._append("Enter the table first.")
                return
            try:
                interval = int(self.interval_var.get().strip())
                if interval <= 0:
                    raise ValueError
            except ValueError:
                self._append("Poll interval must be a positive whole number of seconds.")
                return
            schema, tbl = split_table(table)
            filt = self.filter_var.get().strip() or None
            include = self.include_var.get().strip().strip("[]") or None
            self.monitor = SqlMonitor(
                server, database, schema, tbl, topic, interval,
                self.trust_var.get(), key, self._log_from_thread,
                self._on_monitor_exit, filter_expr=filt, include_col=include,
                insecure_tls=insecure)
            self.thread = threading.Thread(target=self.monitor.run, daemon=True)
            self.thread.start()
            self._append(f"Monitoring: {schema}.{tbl} on {server}/{database} "
                         f"(every {interval}s)")

        self._append(f"Notifying:  {NTFY_SERVER}/{topic}")
        if insecure:
            self._append("TLS: certificate verification OFF for sending.")
        else:
            self._append(f"TLS: verifying via {'OS trust store' if _OS_TRUST else 'bundled CA store'}.")
        self._set_running(True)

    def _stop(self):
        if self.observer is not None:
            try:
                self.observer.stop()
                self.observer.join()
            except Exception:
                pass
            self.observer = None
        if self.monitor is not None:
            self.monitor.stop()
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.monitor = None
        self.thread = None
        self._append("Stopped.")
        self._set_running(False)

    def _on_monitor_exit(self):
        # Called from the SQL worker thread when run() returns.
        self.after(0, self._handle_monitor_exit)

    def _handle_monitor_exit(self):
        # Only reset if the monitor stopped itself (e.g. a permanent error),
        # not when the user clicked Stop (which already reset the button).
        if self.monitor is not None and not self.monitor.stopped_by_user:
            self.monitor = None
            self.thread = None
            self._set_running(False)

    def _log_from_thread(self, msg):
        self.after(0, self._append, msg)

    def _append(self, msg):
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    # ── System tray (minimize to tray) ──────────────────────────────────
    def _on_unmap(self, event):
        # Fires when the window is minimized; send it to the tray instead.
        if event.widget is self and self.state() == "iconic":
            self._to_tray()

    def _to_tray(self):
        self.withdraw()                 # remove the taskbar button
        self._ensure_tray()
        if self._tray is not None:
            try:
                self._tray.visible = True
            except Exception:
                pass

    def _ensure_tray(self):
        if self._tray is not None or not _HAS_TRAY:
            return
        try:
            menu = pystray.Menu(
                pystray.MenuItem("Show Notifier", self._tray_show, default=True),
                pystray.MenuItem("Quit", self._tray_quit),
            )
            self._tray = pystray.Icon("Notifier", _make_tray_image(),
                                      "Notifier", menu)
            self._tray.run_detached()
        except Exception:
            self._tray = None           # fall back to normal minimize

    def _restore(self):
        self.deiconify()
        self.state("normal")
        self.lift()
        self.focus_force()
        if self._tray is not None:
            try:
                self._tray.visible = False
            except Exception:
                pass

    # pystray fires these on its own thread → marshal back to the Tk thread.
    def _tray_show(self, icon=None, item=None):
        self.after(0, self._restore)

    def _tray_quit(self, icon=None, item=None):
        self.after(0, self._quit_all)

    # ── Shutdown ─────────────────────────────────────────────────────────
    def _shutdown_monitors(self):
        if self.observer is not None:
            try:
                self.observer.stop()
            except Exception:
                pass
        if self.monitor is not None:
            self.monitor.stop()

    def _quit_all(self):
        self._shutdown_monitors()
        if self._tray is not None:
            try:
                self._tray.stop()
            except Exception:
                pass
            self._tray = None
        self.destroy()

    def _on_close(self):
        self._quit_all()


if __name__ == "__main__":
    App().mainloop()
