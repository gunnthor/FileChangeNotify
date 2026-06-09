# Notifier

Get an instant push notification on your phone when something changes locally — pick what to watch:

- **Folder** — a file is created, modified, or deleted in a folder you choose.
- **SQL Server** — new rows are inserted into a database table.

Useful when you're waiting on a download, an export, an import job, or records dropped by another system, and you've stepped away.

Built with Python + [watchdog](https://github.com/gorakhargosh/watchdog) (folder mode) and [pyodbc](https://github.com/mkleehammer/pyodbc) (SQL mode). Notifications delivered via [ntfy](https://ntfy.sh), a free open-source push notification service.

---

## How it works

1. Open the app and choose a mode — **Folder** or **SQL Server** — with the selector at the top.
2. Fill in the fields for that mode and your **ntfy subscription**, then hit **Start Monitoring**.
3. When something changes, you get a push on your phone. Click **Get started** in the app any time for setup help (it also pops up the first time you run it).

- **Folder mode** tells you the file name and whether it was created, modified, or deleted.
- **SQL mode** polls the table (default every 30s), auto-detects the **IDENTITY** column to report exactly how many new rows arrived and the latest key, and falls back to watching the row count if there's no identity column. The first read just sets a baseline — no alert until *new* rows appear.

---

## Setup

### 1. Install the ntfy app on your phone

| Platform | Link |
|----------|------|
| iOS | [Download on the App Store](https://apps.apple.com/us/app/ntfy/id1625396347) |
| Android | [Get it on Google Play](https://play.google.com/store/apps/details?id=io.heckel.ntfy) |

Open the app, tap **Subscribe to topic**, and enter a topic name of your choice (e.g. `my-alerts`). Keep this name handy — it's your **ntfy subscription**.

### 2. Run the app

Download `Notifier.exe` from [Releases](../../releases) and double-click it (or run `python notifier.py`).

**Folder mode**
- **Folder to watch** — the folder to monitor (use Browse)
- **ntfy subscription** — the topic name you subscribed to

**SQL Server mode**
- **SQL Server** — e.g. `localhost`, `.\SQLEXPRESS`, or `MYHOST\INSTANCE`
- **Database** and **Table** (`schema.table`, e.g. `dbo.Orders`)
- **Key column** — optional; leave blank to auto-detect the identity column
- **Poll interval** and **Trust SQL Server certificate** (leave checked for a local/self-signed instance)
- **ntfy subscription** — the topic name you subscribed to

Hit **Start Monitoring** and leave it running. **Minimizing sends Notifier to the system tray** (it disappears from the taskbar) and keeps monitoring in the background — right-click the tray icon for **Show Notifier** or **Quit**.

---

## SQL Server prerequisites

- **SQL Server reachable** from the machine running Notifier, with a Windows-auth login that has `SELECT` on the table.
- A **SQL Server ODBC driver** installed (a system component, not bundled into the exe). The app auto-detects whichever is present — preferring "ODBC Driver 18 for SQL Server", then 17/13/11, then the SQL Server Native Client, then the built-in "SQL Server" driver. Driver 18 is recommended ([Microsoft download](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server)); on a Dynamics AX/D365 server an older driver is usually already installed and works fine.

> **Trust SQL Server certificate:** keeping it checked is fine for a local instance with a self-signed certificate (traffic is still encrypted). For production, install a trusted certificate and uncheck it.

> **Behind a corporate proxy?** Notifications go to `https://ntfy.sh`. If your network does TLS inspection, the app verifies against the **Windows certificate store** (via `truststore`), so a corporate root CA already trusted by Windows just works. If sending still fails with a certificate error, tick **"Ignore certificate errors when sending."**

### Advanced: filter which rows count

Expand **Advanced** in SQL mode to enter a SQL `WHERE` condition. Only rows matching it are treated as detectable changes, so you get a push for just the inserts you care about. Examples:

- `Status = 'Error'`
- `LogType IN ('Error','Warning') AND IsHandled = 0`

Use **Test filter** to check it against the table — it reports how many rows match right now, or the exact SQL error. The condition is your own SQL, run as you against your database; it's validated when monitoring starts, and the monitor stops with a clear message if it's invalid. Detection stays insert-based (new rows whose key is higher than the last one seen): the filter narrows *which* new rows notify — it won't re-trigger on updates to older rows.

**Include a column's value in the message.** Also under Advanced, enter a column name (e.g. `ErrorMessage`) to have that field's value from each new row added to the notification — so the push tells you *what* arrived, not just that something did. Up to 10 values are listed per notification (with "…and N more" if there are more). This needs a key/identity column so the app knows which rows are new; the column name is validated at startup.

### Quick SQL test

```sql
CREATE TABLE dbo.NotifyTest (Id INT IDENTITY PRIMARY KEY, Note NVARCHAR(50));
-- Start Notifier (SQL mode) against dbo.NotifyTest, then:
INSERT INTO dbo.NotifyTest (Note) VALUES ('one'), ('two');
-- Within one poll interval you should get a "2 new row(s) … Latest Id = 2" push.
```

---

## Command line (folder only)

A lightweight CLI folder watcher is also included:

```bash
pip install watchdog
python watch.py "C:\path\to\your\folder" your-ntfy-topic
```

---

## Building the exe yourself

```bash
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --windowed --name Notifier --hidden-import pystray._win32 notifier.py
# Output: dist/Notifier.exe
```

(`--hidden-import pystray._win32` ensures the system-tray backend is bundled, since pystray loads it dynamically.)

---

## Configuration

| Setting | Location | Default |
|---------|----------|---------|
| Cooldown between notifications | `COOLDOWN_SECONDS` in `notifier.py` | 30 seconds |
| SQL poll interval | app field / `DEFAULT_INTERVAL` in `notifier.py` | 30 seconds |

---

## Requirements

- Windows (exe) or Python 3.8+ with `watchdog`, `pyodbc`, `truststore` (`pip install -r requirements.txt`)
- For SQL mode: a reachable SQL Server (Windows auth) + a SQL Server ODBC driver
- Internet connection for ntfy notifications
- ntfy app on your phone

---

*Author: Gunnthor — Built with [Claude Code](https://claude.ai/code)*
