#!/usr/bin/env python3
import sys
import ssl
import time
import threading
import urllib.request
import urllib.error
import tkinter as tk
from tkinter import scrolledtext

try:
    import pyodbc
except ImportError:
    root = tk.Tk()
    root.withdraw()
    from tkinter import messagebox
    messagebox.showerror("Missing dependency", "Run:\n\n  pip install pyodbc")
    sys.exit(1)

# Verify TLS against the OS (Windows) certificate store so connections work
# behind a corporate TLS-inspecting proxy whose root CA is trusted by Windows
# but not by Python's bundled CA bundle. Optional — degrades gracefully.
try:
    import truststore
    truststore.inject_into_ssl()
    _OS_TRUST = True
except Exception:
    _OS_TRUST = False


VERSION = "v1.2.2"
AUTHOR = "Gunnthor"
NTFY_SERVER = "https://ntfy.sh"
COOLDOWN_SECONDS = 30          # minimum seconds between notifications
DEFAULT_INTERVAL = 30          # seconds between polls

# Preferred SQL Server ODBC drivers, best first. We use whichever is installed
# so the app works on machines without the newest driver (e.g. an AX/D365 VM
# that only has Driver 17 or the SQL Server Native Client).
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


# ── Helpers ──────────────────────────────────────────────────────────────
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


class MonitorError(Exception):
    """A permanent problem that should stop the monitor (e.g. bad table)."""


# ── Polling monitor (runs on a worker thread) ────────────────────────────
class SqlMonitor:
    def __init__(self, server, database, schema, table, topic, interval,
                 trust_cert, key_column, log_cb, done_cb, insecure_tls=False):
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

    # -- polling --
    def _baseline(self):
        cur = self._conn_cursor()
        resolved = resolve_table(cur, self.schema_in, self.table_in)
        if not resolved:
            raise MonitorError(
                f"[{_ts()}] Table not found: {self.schema_in}.{self.table_in}"
            )
        self.schema, self.table = resolved
        self.key_col = self.key_in or detect_identity_column(
            cur, self.schema, self.table)

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
                cur, f"SELECT COUNT(*) FROM {self._qtable}")
            self.log_cb(f"[{_ts()}] No identity column — using row-count mode "
                        f"(may miss inserts if rows are also deleted).")
            self.log_cb(f"[{_ts()}] Baseline: {self.last_count} rows in "
                        f"{self.schema}.{self.table} — watching for new rows…")
        self.baselined = True

    def _check(self):
        cur = self._conn_cursor()
        if self.mode == "hwm":
            self._check_hwm(cur)
        else:
            self._check_count(cur)

    def _check_hwm(self, cur):
        key = quote_ident(self.key_col)
        if self.last_max is None:
            n = self._scalar(cur, f"SELECT COUNT(*) FROM {self._qtable}")
        else:
            n = self._scalar(
                cur, f"SELECT COUNT(*) FROM {self._qtable} WHERE {key} > ?",
                self.last_max)
        if n <= 0:
            return
        if not self._cooldown_ok():
            return                      # leave last_max; report cumulatively next time
        new_max = self._scalar(cur, f"SELECT MAX({key}) FROM {self._qtable}")
        self._notify(
            n, f"Latest {self.key_col} = {new_max}")
        self.last_max = new_max

    def _check_count(self, cur):
        current = self._scalar(cur, f"SELECT COUNT(*) FROM {self._qtable}")
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

    def _notify(self, n, detail):
        ts = _ts()
        title = f"New rows in {self.schema}.{self.table}"
        body = f"{n} new row(s) in {self.schema}.{self.table} at {ts}\n\n{detail}"
        try:
            send_ntfy(self.topic, title, body, "inbox_tray", self.insecure_tls)
            self.log_cb(f"[{ts}] {n} new row(s) — notification sent.")
        except urllib.error.URLError as e:
            self.log_cb(f"[{ts}] Failed to send: {e}")


def _ts():
    return time.strftime("%H:%M:%S")


def _clean_err(e):
    msg = str(e)
    return msg if len(msg) <= 200 else msg[:200] + "…"


# ── GUI ──────────────────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SQL Row Notifier")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.monitor = None
        self.thread = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self):
        # ── Header ──────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=HEADER_BG)
        hdr.pack(fill="x")
        tk.Label(hdr, text="SQL Row Notifier", font=("Segoe UI", 15, "bold"),
                 bg=HEADER_BG, fg=HEADER_FG, pady=14).pack(side="left", padx=18)
        tk.Label(hdr, text=VERSION, font=("Segoe UI", 9),
                 bg=HEADER_BG, fg="#93c5fd").pack(side="right", padx=18, pady=14)

        body = tk.Frame(self, bg=BG, padx=22, pady=18)
        body.pack(fill="both")

        def field(row, label, var, show=None):
            tk.Label(body, text=label, font=("Segoe UI", 9, "bold"),
                     bg=BG, fg=LABEL_FG, anchor="w").grid(
                         row=row, column=0, columnspan=3, sticky="w",
                         pady=(12 if row else 0, 4))
            e = tk.Entry(body, textvariable=var, width=46, font=("Segoe UI", 9),
                         relief="solid", bd=1, show=show)
            e.grid(row=row + 1, column=0, columnspan=3, sticky="ew", ipady=5)
            return e

        self.server_var = tk.StringVar()
        self.db_var = tk.StringVar()
        self.table_var = tk.StringVar()
        self.key_var = tk.StringVar()
        self.topic_var = tk.StringVar()
        self.interval_var = tk.StringVar(value=str(DEFAULT_INTERVAL))
        self.trust_var = tk.BooleanVar(value=True)
        self.insecure_var = tk.BooleanVar(value=False)

        field(0, "SQL Server  (e.g. localhost or .\\SQLEXPRESS)", self.server_var)
        field(2, "Database", self.db_var)
        field(4, "Table  (schema.table)", self.table_var)
        field(6, "Key column  (optional — auto-detects identity)", self.key_var)
        field(8, "ntfy subscription", self.topic_var)

        # ── Options: poll interval + TLS choices ────────────────────────
        opts = tk.Frame(body, bg=BG)
        opts.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(14, 0))

        line1 = tk.Frame(opts, bg=BG)
        line1.pack(fill="x", anchor="w")
        tk.Label(line1, text="Poll every", font=("Segoe UI", 9), bg=BG,
                 fg=LABEL_FG).pack(side="left")
        tk.Entry(line1, textvariable=self.interval_var, width=5,
                 font=("Segoe UI", 9), relief="solid", bd=1, justify="center").pack(
                     side="left", padx=(6, 4))
        tk.Label(line1, text="seconds", font=("Segoe UI", 9), bg=BG,
                 fg=LABEL_FG).pack(side="left")

        tk.Checkbutton(opts, text="Trust SQL Server certificate (local/self-signed)",
                       variable=self.trust_var, bg=BG, fg=LABEL_FG,
                       activebackground=BG, font=("Segoe UI", 9),
                       anchor="w").pack(fill="x", anchor="w", pady=(6, 0))
        tk.Checkbutton(opts, text="Ignore certificate errors when sending (corporate TLS proxy)",
                       variable=self.insecure_var, bg=BG, fg=LABEL_FG,
                       activebackground=BG, font=("Segoe UI", 9),
                       anchor="w").pack(fill="x", anchor="w")

        # ── Guide box ───────────────────────────────────────────────────
        guide_frame = tk.Frame(body, bg=GUIDE_BG, highlightbackground=GUIDE_BORDER,
                               highlightthickness=1)
        guide_frame.grid(row=11, column=0, columnspan=3, sticky="ew", pady=(16, 0))
        guide_text = (
            "How to receive notifications on your phone\n\n"
            "1.  Download the ntfy app\n"
            "      Android: Google Play  ·  iOS: App Store\n"
            "      Or visit:  ntfy.sh/app\n\n"
            "2.  Open the app and tap  Subscribe to topic\n\n"
            "3.  Enter the subscription name from the field above\n\n"
            "That's it — you'll get a push notification\n"
            "whenever new rows are inserted into the table."
        )
        tk.Label(guide_frame, text=guide_text, font=("Segoe UI", 9),
                 bg=GUIDE_BG, fg="#1e3a8a", justify="left",
                 padx=14, pady=12, anchor="w").pack(fill="both")

        # ── Start / Stop button ─────────────────────────────────────────
        self.btn = tk.Button(body, text="Start Monitoring", command=self._toggle,
                             font=("Segoe UI", 10, "bold"), bg=BTN_BG, fg=BTN_FG,
                             activebackground="#1d4ed8", activeforeground=BTN_FG,
                             relief="flat", cursor="hand2", pady=9)
        self.btn.grid(row=12, column=0, columnspan=3, sticky="ew", pady=(16, 0))

        # ── Status log ──────────────────────────────────────────────────
        tk.Label(body, text="Status", font=("Segoe UI", 9, "bold"),
                 bg=BG, fg=LABEL_FG, anchor="w").grid(row=13, column=0, columnspan=3,
                                                       sticky="w", pady=(16, 4))
        self.log = scrolledtext.ScrolledText(body, height=7, width=55,
                                             font=("Consolas", 8),
                                             bg=LOG_BG, fg=LOG_FG,
                                             relief="solid", bd=1, state="disabled")
        self.log.grid(row=14, column=0, columnspan=3, sticky="ew")

        # ── Footer ──────────────────────────────────────────────────────
        tk.Label(body,
                 text=f"Author: {AUTHOR}  ·  Built with Claude Code  ·  {VERSION}",
                 font=("Segoe UI", 8), bg=BG, fg=MUTED_FG).grid(
                     row=15, column=0, columnspan=3, pady=(14, 0))

        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)

    # ── Actions ─────────────────────────────────────────────────────────
    def _toggle(self):
        if self.thread and self.thread.is_alive():
            self._stop()
        else:
            self._start()

    def _start(self):
        server = self.server_var.get().strip()
        database = self.db_var.get().strip()
        table = self.table_var.get().strip()
        topic = self.topic_var.get().strip()
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
        if not topic:
            self._append("Enter an ntfy subscription first.")
            return
        try:
            interval = int(self.interval_var.get().strip())
            if interval <= 0:
                raise ValueError
        except ValueError:
            self._append("Poll interval must be a positive whole number of seconds.")
            return

        schema, tbl = split_table(table)
        insecure = self.insecure_var.get()
        self.monitor = SqlMonitor(
            server, database, schema, tbl, topic, interval,
            self.trust_var.get(), key, self._log_from_thread, self._on_monitor_exit,
            insecure_tls=insecure)
        self.thread = threading.Thread(target=self.monitor.run, daemon=True)
        self.thread.start()

        self._append(f"Monitoring: {schema}.{tbl} on {server}/{database}")
        self._append(f"Notifying:  {NTFY_SERVER}/{topic}  (every {interval}s)")
        if insecure:
            self._append("TLS: certificate verification OFF for sending.")
        else:
            self._append(f"TLS: verifying via {'OS trust store' if _OS_TRUST else 'bundled CA store'}.")
        self.btn.config(text="Stop Monitoring", bg=BTN_STOP_BG,
                        activebackground="#b91c1c")

    def _stop(self):
        if self.monitor:
            self.monitor.stop()
        if self.thread:
            self.thread.join(timeout=2)
        self.monitor = None
        self.thread = None
        self._append("Stopped.")
        self.btn.config(text="Start Monitoring", bg=BTN_BG,
                        activebackground="#1d4ed8")

    def _on_monitor_exit(self):
        # Called from the worker thread when run() returns.
        self.after(0, self._handle_monitor_exit)

    def _handle_monitor_exit(self):
        # Only reset if the monitor stopped itself (e.g. a permanent error),
        # not when the user clicked Stop (which already reset the button).
        if self.monitor and not self.monitor.stopped_by_user:
            self.monitor = None
            self.thread = None
            self.btn.config(text="Start Monitoring", bg=BTN_BG,
                            activebackground="#1d4ed8")

    def _log_from_thread(self, msg):
        self.after(0, self._append, msg)

    def _append(self, msg):
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _on_close(self):
        if self.monitor:
            self.monitor.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
