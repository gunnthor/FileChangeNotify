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

## Configuration

| Setting | Location | Default |
|---------|----------|---------|
| Cooldown between alerts (per file) | Top of `watch.py` / `app.py` | 30 seconds |

---

## Requirements

- Windows (exe) or Python 3.8+ with `watchdog` (cross-platform)
- Internet connection for ntfy notifications
- ntfy app on your phone

---

*Author: Gunnthor — Built with [Claude Code](https://claude.ai/code)*
