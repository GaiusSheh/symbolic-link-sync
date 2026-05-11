"""File watcher (watchdog) + periodic check with configurable interval."""

import logging
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import win32con
import win32file
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

_FILE_NOTIFY_CHANGE_DIR_NAME  = 0x00000002
_FILE_ACTION_ADDED            = 1
_FILE_ACTION_RENAMED_NEW_NAME = 5

logger = logging.getLogger(__name__)

from core.paths import SYMLINKS_JSON as _JSON_PATH


class _JsonHandler(FileSystemEventHandler):
    def __init__(self, event_queue: queue.Queue):
        super().__init__()
        self._q = event_queue
        self._debounce: threading.Timer | None = None
        self._lock = threading.Lock()

    def on_modified(self, event):
        if Path(event.src_path).resolve() != _JSON_PATH.resolve():
            return
        with self._lock:
            if self._debounce:
                self._debounce.cancel()
            self._debounce = threading.Timer(2.0, self._fire)
            self._debounce.daemon = True
            self._debounce.start()

    def _fire(self):
        logger.info("symlinks.json changed, queuing smart_sync")
        self._q.put(("smart_sync",))


class _AncestorDirHandler(FileSystemEventHandler):
    """Watches all ancestor dirs of link/target paths.

    on_moved (directory):  → ("repath", src, dest)  — no debounce, need both paths
    on_created (directory): record in recent_creates deque + debounce refresh
    on_deleted:             debounce refresh
    """

    def __init__(self, event_queue: queue.Queue):
        super().__init__()
        self._q = event_queue
        self._debounce: threading.Timer | None = None
        self._lock = threading.Lock()

    def on_any_event(self, event):
        logger.debug("[watchdog] %s  is_dir=%s  src=%s  dest=%s",
                     event.event_type, event.is_directory,
                     event.src_path,
                     getattr(event, "dest_path", ""))

    def on_moved(self, event):
        if not event.is_directory:
            return  # junction renames handled by find_renamed_junction in refresh
        logger.info("Dir moved: %s → %s", event.src_path, event.dest_path)
        self._q.put(("repath", event.src_path, event.dest_path))

    def on_created(self, event):
        # recent_creates is handled exclusively by _DriveRootThread (full-drive coverage)
        self._debounce_refresh()

    def on_deleted(self, event):
        self._debounce_refresh()

    def _debounce_refresh(self):
        with self._lock:
            if self._debounce:
                self._debounce.cancel()
            self._debounce = threading.Timer(1.0, self._fire_refresh)
            self._debounce.daemon = True
            self._debounce.start()

    def _fire_refresh(self):
        logger.info("Ancestor dir changed, queuing refresh")
        self._q.put(("refresh",))


class _DriveRootThread(threading.Thread):
    """Watches a drive root recursively for directory creates/renames only.

    Uses ReadDirectoryChangesW with FILE_NOTIFY_CHANGE_DIR_NAME exclusively,
    so file-level events never reach Python — overhead is negligible even for C:\\.
    Records qualifying events into the shared recent_creates deque.
    """

    def __init__(self, drive: Path, recent_creates: deque, event_queue: queue.Queue):
        super().__init__(daemon=True, name=f"DriveRoot-{drive.drive}")
        self._drive            = drive
        self._recent_creates   = recent_creates
        self._q                = event_queue
        self._stop             = threading.Event()
        self._fhandle          = None          # Win32 HANDLE (not threading.Thread._handle)
        self._fhandle_lock     = threading.Lock()
        self._refresh_debounce: threading.Timer | None = None
        self._debounce_lock    = threading.Lock()

    def stop(self) -> None:
        self._stop.set()
        with self._debounce_lock:
            if self._refresh_debounce:
                self._refresh_debounce.cancel()
                self._refresh_debounce = None
        with self._fhandle_lock:
            h, self._fhandle = self._fhandle, None
        if h is not None:
            try:
                win32file.CloseHandle(h)     # unblocks ReadDirectoryChangesW
            except Exception:
                pass

    def _debounce_refresh(self) -> None:
        with self._debounce_lock:
            if self._refresh_debounce:
                self._refresh_debounce.cancel()
            t = threading.Timer(1.0, lambda: self._stop.is_set() or self._q.put(("refresh",)))
            t.daemon = True
            t.start()
            self._refresh_debounce = t

    def run(self) -> None:
        try:
            h = win32file.CreateFile(
                str(self._drive),
                win32con.GENERIC_READ,
                win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
                None, win32con.OPEN_EXISTING,
                win32con.FILE_FLAG_BACKUP_SEMANTICS, None,
            )
        except Exception as exc:
            logger.warning("DriveRootThread: cannot open %s: %s", self._drive, exc)
            return
        with self._fhandle_lock:
            if self._stop.is_set():          # stop() called before CreateFile returned
                win32file.CloseHandle(h)
                return
            self._fhandle = h
        try:
            while not self._stop.is_set():
                try:
                    results = win32file.ReadDirectoryChangesW(
                        self._fhandle,
                        1024 * 1024,                    # 1 MB buffer
                        True,                           # watch_subtree (recursive)
                        _FILE_NOTIFY_CHANGE_DIR_NAME,   # directories only
                        None, None,
                    )
                    for action, rel_path in results:
                        if action in (_FILE_ACTION_ADDED, _FILE_ACTION_RENAMED_NEW_NAME):
                            full = self._drive / rel_path
                            self._recent_creates.append((time.time(), full))
                            logger.debug("DriveRootThread create: %s", full)
                            self._debounce_refresh()
                except Exception as exc:
                    if not self._stop.is_set():
                        logger.warning("DriveRootThread %s: read error: %s",
                                       self._drive, exc)
                        time.sleep(1)
        finally:
            with self._fhandle_lock:
                h, self._fhandle = self._fhandle, None
            if h is not None:
                try:
                    win32file.CloseHandle(h)
                except Exception:
                    pass


class BackgroundWatcher:
    def __init__(self, event_queue: queue.Queue, check_interval_seconds: int = 600):
        self._q = event_queue
        self._interval = check_interval_seconds
        self._observer: Observer | None = None
        self._timer: threading.Timer | None = None
        self._running = False
        self._dir_watches: dict[Path, object] = {}   # Path → watchdog Watch
        self._recent_creates: deque = deque(maxlen=200)
        self._ancestor_handler = _AncestorDirHandler(self._q)
        self._drive_threads: dict[Path, _DriveRootThread] = {}

    def set_interval(self, seconds: int) -> None:
        self._interval = seconds

    def start(self):
        self._running = True

        handler = _JsonHandler(self._q)
        self._observer = Observer()
        self._observer.schedule(handler, str(_JSON_PATH.parent), recursive=False)
        self._observer.daemon = True
        self._observer.start()
        logger.info("Watching %s", _JSON_PATH)

        self._schedule_check()

    def stop(self):
        self._running = False
        if self._timer:
            self._timer.cancel()
        drive_threads = list(self._drive_threads.values())
        self._drive_threads.clear()
        for t in drive_threads:
            t.stop()
        for t in drive_threads:
            t.join(timeout=2)
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=3)

    def update_drive_roots(self, drives: list[Path]) -> None:
        """Start/stop _DriveRootThread instances to match the given drive list."""
        new     = {d.resolve() for d in drives}
        current = set(self._drive_threads.keys())
        for d in current - new:
            self._drive_threads.pop(d).stop()
        for d in new - current:
            t = _DriveRootThread(d, self._recent_creates, self._q)
            t.start()
            self._drive_threads[d] = t
            logger.info("DriveRootThread started: %s", d)

    def update_watch_dirs(self, dirs: dict[Path, bool]) -> None:
        """Update watched ancestor directories.

        dirs maps Path → recursive (True for OneDrive root, False for others).
        """
        if self._observer is None or not self._observer.is_alive():
            return

        new_set   = set(dirs.keys())
        current   = set(self._dir_watches.keys())
        to_add    = new_set - current
        to_remove = current - new_set

        for d in to_remove:
            watch = self._dir_watches.pop(d, None)
            if watch:
                try:
                    self._observer.unschedule(watch)
                except Exception:
                    pass

        for d in to_add:
            if not d.is_dir():
                logger.warning("Watch dir does not exist, skipping: %s", d)
                continue
            try:
                recursive = dirs[d]
                watch = self._observer.schedule(
                    self._ancestor_handler, str(d), recursive=recursive
                )
                self._dir_watches[d] = watch
                logger.info("Watching %s (recursive=%s)", d, recursive)
            except Exception as exc:
                logger.warning("Cannot watch %s: %s", d, exc)

    def get_recent_creates(self, max_age_s: float = 30.0) -> list[Path]:
        """Return paths of all directories created within the last max_age_s seconds."""
        now = time.time()
        return [p for ts, p in self._recent_creates if now - ts <= max_age_s]

    def find_recent_create(self, name: str, max_age_s: float = 5.0) -> Optional[Path]:
        """Return the most recent directory created with the given name within max_age_s seconds."""
        now        = time.time()
        name_lower = name.lower()
        snapshot = list(self._recent_creates)
        logger.debug("find_recent_create(%r, %.1fs): deque has %d entries", name, max_age_s, len(snapshot))
        for ts, path in reversed(snapshot):
            age = now - ts
            if age > max_age_s:
                continue
            logger.debug("  candidate age=%.2fs path=%s", age, path)
            if path.name.lower() == name_lower and path.is_dir():
                logger.debug("  → match: %s", path)
                return path
        logger.debug("  → no match found")
        return None

    def _schedule_check(self):
        if not self._running:
            return
        self._timer = threading.Timer(self._interval, self._periodic)
        self._timer.daemon = True
        self._timer.start()

    def _periodic(self):
        logger.info("Periodic check triggered")
        self._q.put(("refresh",))
        self._schedule_check()
