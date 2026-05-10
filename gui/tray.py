"""System tray icon, menu, and dynamic icon drawing."""

import queue
import threading
from datetime import datetime
from typing import Callable

import pystray
from PIL import Image

from icons import tray_icon
from symlink_manager import Status, get_scanned, get_other_machines_local_entries


def _status_color(entries, confirmed_empty: set[str]) -> str:
    if any(e.status in (Status.BROKEN, Status.MISSING) for e in entries):
        return "red"
    if any(e.status == Status.PENDING for e in entries):
        return "yellow"
    if any(e.target_empty and e.id not in confirmed_empty for e in entries):
        return "yellow"
    my_ids = {e.id for e in entries}
    # Unmanaged scan items → yellow
    managed_links = {str(e.link).lower() for e in entries}
    if any(not s.get("ignored") and s.get("link", "").lower() not in managed_links
           for s in get_scanned()):
        return "yellow"
    # Offline entries not yet configured on this machine → yellow
    if any(eid not in my_ids for eid in get_other_machines_local_entries()):
        return "yellow"
    return "green"


def _status_tooltip(entries) -> str:
    ok      = sum(1 for e in entries if e.status == Status.OK)
    broken  = sum(1 for e in entries if e.status == Status.BROKEN)
    pending = sum(1 for e in entries if e.status == Status.PENDING)
    missing = sum(1 for e in entries if e.status == Status.MISSING)
    parts = [f"{ok} OK"]
    if broken:  parts.append(f"{broken} broken")
    if pending: parts.append(f"{pending} pending")
    if missing: parts.append(f"{missing} missing")
    return "Sym-Link: " + ", ".join(parts)


class TrayIcon:
    def __init__(self, event_queue: queue.Queue,
                 on_sync: Callable, on_open_window: Callable,
                 on_open_settings: Callable, on_quit: Callable):
        self._q = event_queue
        self._on_sync = on_sync
        self._on_open_window = on_open_window
        self._on_open_settings = on_open_settings
        self._on_quit = on_quit

        self._last_sync: datetime | None = None
        self._next_check: datetime | None = None
        self._entries = []

        self._icon = pystray.Icon(
            "sym-link",
            icon=tray_icon("green"),
            title="Sym-Link",
            menu=self._build_menu(),
        )

    def _build_menu(self) -> pystray.Menu:
        last = self._last_sync.strftime("%H:%M:%S") if self._last_sync else "—"
        nxt  = self._next_check.strftime("%H:%M:%S") if self._next_check else "—"
        return pystray.Menu(
            pystray.MenuItem("打开状态窗口", self._open_window, default=True),
            pystray.MenuItem("立即同步", self._sync_now),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(f"上次同步：{last}", None, enabled=False),
            pystray.MenuItem(f"下次检测：{nxt}",  None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("设置...", self._open_settings),
            pystray.MenuItem("退出", self._quit),
        )

    def _open_window(self, icon=None, item=None):
        self._on_open_window()

    def _sync_now(self, icon=None, item=None):
        self._on_sync()

    def _open_settings(self, icon=None, item=None):
        self._on_open_settings()

    def _quit(self, icon=None, item=None):
        self._icon.stop()
        self._on_quit()

    def update(self, entries, last_sync: datetime | None = None,
               next_check: datetime | None = None,
               confirmed_empty: set[str] | None = None):
        self._entries = entries
        if last_sync:
            self._last_sync = last_sync
        if next_check:
            self._next_check = next_check

        color_key = _status_color(entries, confirmed_empty or set())
        self._icon.icon = tray_icon(color_key)
        self._icon.title = _status_tooltip(entries)
        self._icon.menu = self._build_menu()

    def run(self):
        self._icon.run()

    def stop(self):
        self._icon.stop()
