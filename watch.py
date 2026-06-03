#!/usr/bin/env python3
"""
watch.py — send a push notification via ntfy.sh when a file is created or
modified inside a folder.

Usage:
    python watch.py <folder_path> <ntfy_topic>

Example:
    python watch.py "C:\\Users\\me\\Downloads" my-downloads-abc123
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
COOLDOWN_SECONDS = 30   # minimum seconds between alerts for the same file (prevents spam on rapid writes)


class FolderHandler(FileSystemEventHandler):
    def __init__(self, folder, ntfy_topic):
        self.folder = os.path.abspath(folder)
        self.ntfy_topic = ntfy_topic
        self._last_notified = {}   # per-file path -> last notified timestamp
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
        if now - self._last_notified.get(path, 0.0) < COOLDOWN_SECONDS:
            return
        self._last_notified[path] = now
        self._send_notification(path, action)

    def _send_notification(self, path, action):
        timestamp = time.strftime("%H:%M:%S")
        filename = os.path.basename(path)
        tags = {"New file": "sparkles",
                "File modified": "pencil2",
                "File deleted": "wastebasket"}.get(action, "bell")
        body = f"{action} at {timestamp}\n\n{path}"

        try:
            req = urllib.request.Request(
                f"{NTFY_SERVER}/{self.ntfy_topic}",
                data=body.encode(),
                headers={
                    "Title": f"{action}: {filename}",
                    "Priority": "high",
                    "Tags": tags,
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
            print(f"[{timestamp}] {action}: {filename} — notification sent.")
        except urllib.error.URLError as e:
            print(f"[{timestamp}] Failed to send notification: {e}")


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    folder = sys.argv[1]
    ntfy_topic = sys.argv[2]

    if not os.path.isdir(folder):
        print(f"Folder not found: {folder}")
        sys.exit(1)

    handler = FolderHandler(folder, ntfy_topic)
    observer = Observer()
    observer.schedule(handler, path=folder, recursive=False)
    observer.start()

    print(f"Watching : {folder}")
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
