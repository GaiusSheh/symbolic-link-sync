"""File watcher (watchdog) + periodic check with configurable interval."""

import logging
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import pywintypes
import win32con
import win32event
import win32file
import winerror
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

_FILE_NOTIFY_CHANGE_DIR_NAME  = 0x00000002
_FILE_ACTION_ADDED            = 1
_FILE_ACTION_RENAMED_NEW_NAME = 5

# DriveRootThread tuning (module-level so tests can shrink them)
_RDCW_BUF_SIZE   = 64 * 1024   # dir-name events only; 64 KB is also the network-share cap
_BACKOFF_MIN_S   = 1.0         # retry delay after open/read failure, doubled each time …
_BACKOFF_MAX_S   = 60.0        # … up to this cap
_HEALTHY_AFTER_S = 1.0         # a pending read that survives this long counts as a working watch

logger = logging.getLogger(__name__)

from core.paths import get_symlinks_json as _get_json_path


class _JsonHandler(FileSystemEventHandler):
    def __init__(self, event_queue: queue.Queue):
        super().__init__()
        self._q = event_queue
        self._debounce: threading.Timer | None = None
        self._lock = threading.Lock()

    def on_modified(self, event):
        if Path(event.src_path).resolve() != _get_json_path().resolve():
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

    Reads are overlapped into a single preallocated buffer. Never pass an int
    buffer size to win32file.ReadDirectoryChangesW: pywin32 mallocs that buffer
    and leaks it whenever the call fails (issue #1 — a dead handle retried every
    second leaked 1 MiB/s). On open/read failure the handle is closed and
    reopened with exponential backoff, so a drive that disappears (e.g. Google
    Drive restarting) or mounts late is picked up again.
    """

    def __init__(self, drive: Path, recent_creates: deque, event_queue: queue.Queue):
        super().__init__(daemon=True, name=f"DriveRoot-{drive.drive}")
        self._drive            = drive
        self._recent_creates   = recent_creates
        self._q                = event_queue
        self._stop             = threading.Event()
        # Win32 twin of _stop so blocking waits wake up immediately on stop()
        self._stop_evt         = win32event.CreateEvent(None, True, False, None)
        self._io_evt           = win32event.CreateEvent(None, True, False, None)
        self._buf              = win32file.AllocateReadBuffer(_RDCW_BUF_SIZE)
        self._backoff          = _BACKOFF_MIN_S
        self._failures         = 0             # consecutive open/read failures
        self._refresh_debounce: threading.Timer | None = None
        self._debounce_lock    = threading.Lock()

    def stop(self) -> None:
        # The directory handle is owned (opened/closed) by the worker thread only;
        # it cancels its pending read itself once _stop_evt is signalled.
        self._stop.set()
        win32event.SetEvent(self._stop_evt)
        with self._debounce_lock:
            if self._refresh_debounce:
                self._refresh_debounce.cancel()
                self._refresh_debounce = None

    def _debounce_refresh(self) -> None:
        with self._debounce_lock:
            if self._refresh_debounce:
                self._refresh_debounce.cancel()
            t = threading.Timer(1.0, lambda: self._stop.is_set() or self._q.put(("refresh",)))
            t.daemon = True
            t.start()
            self._refresh_debounce = t

    def _open(self):
        """Open the drive root for overlapped change notification (raises on failure)."""
        return win32file.CreateFile(
            str(self._drive),
            win32con.GENERIC_READ,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
            None, win32con.OPEN_EXISTING,
            win32con.FILE_FLAG_BACKUP_SEMANTICS | win32con.FILE_FLAG_OVERLAPPED, None,
        )

    def _wait_stop(self, seconds: float) -> bool:
        """Sleep up to `seconds`; return True if stop() was called."""
        rc = win32event.WaitForSingleObject(self._stop_evt, int(seconds * 1000))
        return rc == win32event.WAIT_OBJECT_0

    def _on_failure(self, what: str, exc: Exception) -> None:
        self._failures += 1
        # Log the 1st, 2nd, 4th, 8th … consecutive failure only (no per-retry spam)
        if self._failures & (self._failures - 1) == 0:
            logger.warning("DriveRootThread %s: %s failed (%d in a row, retry in %gs): %s",
                           self._drive, what, self._failures, self._backoff, exc)

    def _mark_healthy(self) -> None:
        if self._failures:
            logger.info("DriveRootThread %s: watch restored after %d failure(s)",
                        self._drive, self._failures)
            self._debounce_refresh()     # catch up on changes missed while down
        self._failures = 0
        self._backoff  = _BACKOFF_MIN_S

    def _read_loop(self, h) -> None:
        """Issue overlapped reads until stop() (returns) or an error (raises)."""
        while not self._stop.is_set():
            ov = pywintypes.OVERLAPPED()
            ov.hEvent = self._io_evt
            win32event.ResetEvent(self._io_evt)
            win32file.ReadDirectoryChangesW(
                h, self._buf,
                True,                           # watch_subtree (recursive)
                _FILE_NOTIFY_CHANGE_DIR_NAME,   # directories only
                ov,
            )
            handles = [self._io_evt, self._stop_evt]
            rc = win32event.WaitForMultipleObjects(handles, False, int(_HEALTHY_AFTER_S * 1000))
            if rc == win32event.WAIT_TIMEOUT:
                self._mark_healthy()            # read is pending normally → watch works
                rc = win32event.WaitForMultipleObjects(handles, False, win32event.INFINITE)
            if rc != win32event.WAIT_OBJECT_0:  # stop requested
                win32file.CancelIo(h)           # same thread issued the read
                try:
                    # Wait for the kernel to finish with self._buf before it is reused/freed
                    win32file.GetOverlappedResult(h, ov, True)
                except pywintypes.error as exc:
                    if exc.winerror != winerror.ERROR_OPERATION_ABORTED:
                        logger.debug("DriveRootThread %s: cancel: %s", self._drive, exc)
                return
            nbytes = win32file.GetOverlappedResult(h, ov, False)   # raises on read error
            self._mark_healthy()
            if nbytes == 0:                     # buffer overflow: events were dropped
                self._debounce_refresh()
                continue
            for action, rel_path in win32file.FILE_NOTIFY_INFORMATION(self._buf, nbytes):
                if action in (_FILE_ACTION_ADDED, _FILE_ACTION_RENAMED_NEW_NAME):
                    full = self._drive / rel_path
                    self._recent_creates.append((time.time(), full))
                    logger.debug("DriveRootThread create: %s", full)
                    self._debounce_refresh()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                h = self._open()
            except Exception as exc:
                self._on_failure("open", exc)
            else:
                try:
                    self._read_loop(h)
                except Exception as exc:
                    if not self._stop.is_set():
                        self._on_failure("read", exc)
                finally:
                    try:
                        win32file.CloseHandle(h)
                    except Exception:
                        pass
            if self._stop.is_set() or self._wait_stop(self._backoff):
                break
            self._backoff = min(self._backoff * 2, _BACKOFF_MAX_S)


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
        self._observer.schedule(handler, str(_get_json_path().parent), recursive=False)
        self._observer.daemon = True
        self._observer.start()
        logger.info("Watching %s", _get_json_path())

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
