# File Change Notifier

Get an instant push notification on your phone whenever a file is created, modified, or deleted in a folder you choose — useful when you're waiting on a download, an export, or files dropped by another process and you step away.

Built with Python + [watchdog](https://github.com/gorakhargosh/watchdog). Notifications delivered via [ntfy](https://ntfy.sh), a free open-source push notification service.

---

## How it works

1. The app watches a folder you choose
2. When a file is created, modified, or deleted in that folder, it sends a push notification to your phone via ntfy
3. The notification tells you the file name and whether it was created, modified, or deleted

---

## Setup

### 1. Install the ntfy app on your phone

| Platform | Link |
|----------|------|
| iOS | [Download on the App Store](https://apps.apple.com/us/app/ntfy/id1625396347) |
| Android | [Get it on Google Play](https://play.google.com/store/apps/details?id=io.heckel.ntfy) |

Open the app, tap **Subscribe to topic**, and enter a topic name of your choice (e.g. `my-import-log`). Keep this name handy.

### 2. Run the app

**Option A — GUI (recommended for sharing)**

Download `FileChangeNotifier.exe` from [Releases](../../releases), double-click it, and fill in:
- **Folder to watch** — the folder to monitor (use the Browse button)
- **ntfy subscription** — the topic name you subscribed to

Hit **Start Watching** and leave the window open.

**Option B — Command line**

```bash
pip install watchdog
python watch.py "C:\path\to\your\folder" your-ntfy-topic
```

---

## Building the exe yourself

```bash
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --windowed --name FileChangeNotifier app.py
# Output: dist/FileChangeNotifier.exe
```

---

## SQL Row Notifier (companion app)

A second GUI app, `sql_app.py`, watches a **table in a local SQL Server database** and sends a push notification whenever **new rows are inserted** — handy when another system writes records into a table and you want to know the moment they land.

### How it works

1. It connects to your SQL Server using **Windows authentication** and polls the table on an interval (default 30s).
2. It auto-detects the table's **IDENTITY** column and tracks its highest value, so it can report exactly how many new rows arrived (and the latest key). If the table has no identity column, it falls back to watching the row count.
3. The first read just establishes a baseline — you won't get an alert until *new* rows actually appear.

### Prerequisites

- **SQL Server reachable** from this machine, with a login that has `SELECT` on the table (Windows auth).
- **ODBC Driver 18 for SQL Server** installed — this is a system component and is **not** bundled into the exe. ([Microsoft download](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server))

### Run it

**GUI** — download `SqlRowNotifier.exe` from [Releases](../../releases) (or run `python sql_app.py`) and fill in:
- **SQL Server** — e.g. `localhost`, `.\SQLEXPRESS`, or `MYHOST\INSTANCE`
- **Database** and **Table** (`schema.table`, e.g. `dbo.Orders`)
- **Key column** — optional; leave blank to auto-detect the identity column
- **ntfy subscription** — the topic name you subscribed to
- **Poll interval** and **Trust server certificate** (leave checked for a local/self-signed instance — Driver 18 encrypts by default)

Hit **Start Monitoring** and leave the window open.

### Build the SQL exe yourself

```bash
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --windowed --name SqlRowNotifier sql_app.py
# Output: dist/SqlRowNotifier.exe
```

> **Note on `TrustServerCertificate`:** keeping it checked is fine for a local instance with a self-signed certificate (traffic is still encrypted). For production, install a trusted certificate and uncheck it.

### Quick test

```sql
CREATE TABLE dbo.NotifyTest (Id INT IDENTITY PRIMARY KEY, Note NVARCHAR(50));
-- Start the app against dbo.NotifyTest, then:
INSERT INTO dbo.NotifyTest (Note) VALUES ('one'), ('two');
-- Within one poll interval you should get a "2 new row(s) … Latest Id = 2" push.
```

---

## Configuration

| Setting | Location | Default |
|---------|----------|---------|
| Cooldown between alerts (per file) | Top of `watch.py` / `app.py` | 30 seconds |
| Cooldown between alerts (SQL) | Top of `sql_app.py` | 30 seconds |
| SQL poll interval | `sql_app.py` field / `DEFAULT_INTERVAL` | 30 seconds |

---

## Requirements

- Windows (exe) or Python 3.8+ with `watchdog` (file notifier, cross-platform)
- For the SQL Row Notifier: `pyodbc` + **ODBC Driver 18 for SQL Server**, and a reachable SQL Server (Windows auth)
- Internet connection for ntfy notifications
- ntfy app on your phone

---

*Author: Gunnthor — Built with [Claude Code](https://claude.ai/code)*
