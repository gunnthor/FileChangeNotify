#!/usr/bin/env python3
"""
watch.py — send a push notification via ntfy.sh when a file is written to.

Usage:
    python watch.py <file_path> <ntfy_topic>

Example:
    python watch.py "C:\\logs\\import.log" my-import-log-abc123
"""

import os
import sys
import time
import urllib.request
import urllib.error

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    print("Missing dependency. Run:  pip install watchdog")
    sys.exit(1)


NTFY_SERVER = "https://ntfy.sh"
COOLDOWN_SECONDS = 30   # minimum seconds between notifications (prevents spam on rapid writes)
TAIL_LINES = 5          # how many trailing log lines to include in the notification


def tail(filepath, n):
    """Return the last n lines of a file without loading it all into memory."""
    try:
        with open(filepath, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            buf = b""
            chunk = 4096
            lines_found = 0
            pos = size
            while pos > 0 and lines_found <= n:
                read = min(chunk, pos)
                pos -= read
                f.seek(pos)
                buf = f.read(read) + buf
                lines_found = buf.count(b"\n")
            lines = buf.decode(errors="replace").splitlines()
            return "\n".join(lines[-n:]) if lines else ""
    except OSError:
        return ""


class LogFileHandler(FileSystemEventHandler):
    def __init__(self, filepath, ntfy_topic):
        self.filepath = os.path.abspath(filepath)
        self.ntfy_topic = ntfy_topic
        self._last_notified = 0.0

    def on_modified(self, event):
        if event.is_directory:
            return
        if os.path.abspath(event.src_path) != self.filepath:
            return

        now = time.time()
        if now - self._last_notified < COOLDOWN_SECONDS:
            return
        self._last_notified = now

        self._send_notification()

    def _send_notification(self):
        timestamp = time.strftime("%H:%M:%S")
        last_lines = tail(self.filepath, TAIL_LINES)
        filename = os.path.basename(self.filepath)

        body = f"Written at {timestamp}\n\n{last_lines}" if last_lines else f"Written at {timestamp}"

        try:
            req = urllib.request.Request(
                f"{NTFY_SERVER}/{self.ntfy_topic}",
                data=body.encode(),
                headers={
                    "Title": f"{filename} was written to",
                    "Priority": "high",
                    "Tags": "warning,page_facing_up",
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
            print(f"[{timestamp}] Notification sent.")
        except urllib.error.URLError as e:
            print(f"[{timestamp}] Failed to send notification: {e}")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    filepath = sys.argv[1]
    ntfy_topic = sys.argv[2]

    if not os.path.isfile(filepath):
        print(f"File not found: {filepath}")
        sys.exit(1)

    watch_dir = os.path.dirname(os.path.abspath(filepath)) or "."
    handler = LogFileHandler(filepath, ntfy_topic)
    observer = Observer()
    observer.schedule(handler, path=watch_dir, recursive=False)
    observer.start()

    print(f"Watching : {filepath}")
    print(f"Notify   : {NTFY_SERVER}/{ntfy_topic}")
    print(f"Cooldown : {COOLDOWN_SECONDS}s between alerts")
    print("Press Ctrl+C to stop.\n")

    try:
        while observer.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()


if __name__ == "__main__":
    main()
