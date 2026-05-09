"""Entry point: wires together tray, window, watcher, manager, and settings."""

import ctypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI aware
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import logging
import logging.handlers
import os
import queue
import shutil
import sys
import threading
import tkinter as tk
from datetime import datetime, timedelta
from pathlib import Path

import json
import settings_manager as sm
import symlink_manager as mgr
from icons import app_icon

_STATE_PATH = Path(__file__).parent / "state.json"


def _load_confirmed_empty() -> set[str]:
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        return set(data.get("confirmed_empty", []))
    except Exception:
        return set()


def _save_confirmed_empty(ids: set[str]) -> None:
    try:
        _STATE_PATH.write_text(
            json.dumps({"confirmed_empty": sorted(ids)}, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
from notifier import send_toast, show_banner
from symlink_manager import LinkEntry, Status
from settings_window import SettingsWindow
from tray import TrayIcon
from watcher import BackgroundWatcher
from window import StatusWindow

_LOG_PATH = Path(__file__).parent / "symlink-gui.log"
_POLL_MS  = 100


def _setup_logging():
    _LOG_PATH.unlink(missing_ok=True)          # fresh log each run
    Path(str(_LOG_PATH) + ".1").unlink(missing_ok=True)
    handler = logging.FileHandler(_LOG_PATH, encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        handlers=[handler, logging.StreamHandler(sys.stdout)],
    )


class App:
    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        self._settings = sm.load()

        self._root = tk.Tk()
        self._root.withdraw()
        self._root.title("Sym-Link")
        try:
            # Give the process its own taskbar identity (separates from python.exe)
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "YuanFeng.SymLink.GUI"
            )
            # iconphoto with high-res PNG — must be LAST icon call (iconbitmap overwrites it)
            from PIL import ImageTk
            _photo = ImageTk.PhotoImage(app_icon(256), master=self._root)
            self._root.iconphoto(True, _photo)   # True = propagate to all child Toplevels
            self._app_icon_ref = _photo   # prevent GC
        except Exception:
            pass

        self._entries: list[LinkEntry] = []
        self._last_sync: datetime | None = None
        self._repair_shown: set[str] = set()
        self._confirmed_empty: set[str] = _load_confirmed_empty()

        self._tray = TrayIcon(
            event_queue=self._q,
            on_sync=self._request_sync,
            on_open_window=self._request_open_window,
            on_open_settings=self._request_open_settings,
            on_quit=self._quit,
        )
        self._window = StatusWindow(
            root=self._root,
            on_sync=self._request_sync,
            on_refresh_needed=self._request_refresh,
            on_open_settings=self._request_open_settings,
            on_entry_saved=self._on_entry_saved,
            on_relink=self._on_relink_entry,
        )
        self._settings_win = SettingsWindow(
            root=self._root,
            on_apply=self._on_settings_applied,
        )
        self._watcher = BackgroundWatcher(
            self._q,
            check_interval_seconds=self._settings.check_interval_minutes * 60,
        )

        self._apply_close_to_tray(self._settings.close_to_tray)

    def _apply_close_to_tray(self, enabled: bool):
        if enabled:
            self._window.set_close_handler(lambda: self._window.hide())
        else:
            self._window.set_close_handler(self._request_quit_from_window)

    def _request_quit_from_window(self):
        self._q.put(("quit",))

    def run(self):
        tray_thread = threading.Thread(target=self._tray.run, daemon=True, name="tray")
        tray_thread.start()

        self._watcher.start()
        self._q.put(("sync", "startup"))

        self._root.after(_POLL_MS, self._poll)
        self._root.mainloop()

    # ── Queue polling ────────────────────────────────────────────────────────

    def _poll(self):
        try:
            while True:
                msg = self._q.get_nowait()
                self._handle(msg)
        except queue.Empty:
            pass
        self._root.after(_POLL_MS, self._poll)

    def _handle(self, msg):
        kind = msg[0]
        if kind == "sync":
            self._do_sync(reason=msg[1] if len(msg) > 1 else "")
        elif kind == "smart_sync":
            self._do_smart_sync()
        elif kind == "repath":
            self._do_repath(msg[1], msg[2])
        elif kind == "open_window":
            self._do_open_window()
        elif kind == "open_settings":
            self._settings_win.show()
        elif kind == "refresh":
            self._do_refresh()
        elif kind == "quit":
            self._do_quit()

    # ── Actions (any thread → queue) ─────────────────────────────────────────

    def _request_sync(self):
        self._q.put(("sync", "manual"))

    def _request_open_window(self):
        self._q.put(("open_window",))

    def _request_open_settings(self):
        self._q.put(("open_settings",))

    def _request_refresh(self):
        self._q.put(("refresh",))

    def _quit(self):
        self._q.put(("quit",))

    # ── Actions (main thread) ─────────────────────────────────────────────────

    def _do_smart_sync(self):
        known_ids = {e.id for e in self._entries}
        result = mgr.smart_sync(known_ids)
        self._last_sync = datetime.now()
        self._entries = mgr.check_all()
        next_check = self._last_sync + timedelta(
            seconds=self._settings.check_interval_minutes * 60
        )
        self._tray.update(self._entries, self._last_sync, next_check, confirmed_empty=self._confirmed_empty)
        self._window.refresh(self._entries)
        self._watcher.update_watch_dirs(mgr.collect_watch_dirs(self._entries))
        if result.created:
            send_toast("Sym-Link: 已创建链接", ", ".join(result.created))
        if result.failed:
            send_toast("Sym-Link: 创建失败", ", ".join(result.failed))
        if result.broken:
            ids = ", ".join(result.broken)
            send_toast(f"Sym-Link: {len(result.broken)} 个断链", ids)
        logging.info("Smart sync done: %d created, %d failed, %d broken",
                     len(result.created), len(result.failed), len(result.broken))

    def _do_sync(self, reason: str = ""):
        self._repair_shown.clear()
        logging.info("[%s] Running sync...", reason)
        result = mgr.sync_all()
        self._last_sync = datetime.now()
        next_check = self._last_sync + timedelta(
            seconds=self._settings.check_interval_minutes * 60
        )

        self._entries = mgr.check_all()
        self._tray.update(self._entries, self._last_sync, next_check, confirmed_empty=self._confirmed_empty)
        self._window.set_confirmed_empty(self._confirmed_empty)
        self._window.refresh(self._entries)
        self._watcher.update_watch_dirs(mgr.collect_watch_dirs(self._entries))

        logging.info("Sync done: %d created, %d skipped, %d failed, %d broken",
                     len(result.created), len(result.skipped),
                     len(result.failed), len(result.broken))

        if result.broken:
            ids = ", ".join(result.broken)
            send_toast(title=f"Sym-Link: {len(result.broken)} 个断链", body=ids)

    def _do_repath(self, old_path: str, new_path: str):
        logging.info("Dir moved: %s → %s", old_path, new_path)
        updated, failed = mgr.repath_entries(old_path, new_path)
        if not updated and not failed:
            return  # unrelated directory move, ignore
        self._entries = mgr.check_all()
        self._tray.update(self._entries, self._last_sync, confirmed_empty=self._confirmed_empty)
        self._window.refresh(self._entries)
        self._watcher.update_watch_dirs(mgr.collect_watch_dirs(self._entries))
        old_name = Path(old_path).name
        new_name = Path(new_path).name
        msg = f"已更新 {len(updated)} 项路径：{old_name} → {new_name}"
        if failed:
            msg += f"，{len(failed)} 项 junction 重建失败"
        send_toast("Sym-Link: 已自动更新路径", msg)
        show_banner(self._root, "Sym-Link: 已自动更新路径", msg)
        logging.info("Repath done: %d updated, %d failed", len(updated), len(failed))

    def _do_open_window(self):
        if not self._entries:
            self._entries = mgr.check_all()
        self._window.show(self._entries)

    def _find_moved_ancestor(self, entry: LinkEntry) -> tuple[Path, Path] | None:
        """Walk link/target ancestors; return (old_ancestor, new_loc) if a dir was recently moved."""
        seen: set[Path] = set()
        for path in (entry.link, entry.target):
            for ancestor in path.parents:
                if ancestor in seen:
                    continue
                seen.add(ancestor)
                if ancestor.parent == ancestor:   # drive root
                    break
                new_loc = self._watcher.find_recent_create(ancestor.name, max_age_s=8.0)
                if new_loc and new_loc != ancestor:
                    return ancestor, new_loc
        return None

    def _do_refresh(self):
        prev = {e.id: e for e in self._entries}
        self._entries = mgr.check_all()
        _repaths_done: set[str] = set()   # avoid duplicate repath calls per ancestor

        for entry in self._entries:
            prev_entry = prev.get(entry.id)

            # ── Both link and target gone (OK → MISSING) — directory moved ───
            if entry.status == Status.MISSING and prev_entry and prev_entry.status == Status.OK:
                moved = self._find_moved_ancestor(entry)
                if moved:
                    old_base, new_base = moved
                    key = str(old_base)
                    if key not in _repaths_done:
                        _repaths_done.add(key)
                        logging.info("Dir move detected: %s → %s", old_base, new_base)
                        updated, failed = mgr.repath_entries(str(old_base), str(new_base))
                        self._entries = mgr.check_all()
                        self._tray.update(self._entries, self._last_sync, confirmed_empty=self._confirmed_empty)
                        self._window.refresh(self._entries)
                        self._watcher.update_watch_dirs(mgr.collect_watch_dirs(self._entries))
                        msg = f"已更新 {len(updated)} 项路径：{old_base.name} → {new_base.name}"
                        if failed:
                            msg += f"，{len(failed)} 项 junction 重建失败"
                        send_toast("Sym-Link: 已自动更新路径", msg)
                        show_banner(self._root, "Sym-Link: 已自动更新路径", msg)
                else:
                    logging.warning("Dir move: no destination found for %s", entry.id)
                    self._show_repair_dialog(entry, "目录已移动，但无法找到新位置，请手动更新路径。")
                continue

            # ── Explorer cut+paste junction: junction still exists but target emptied ─
            # Windows sometimes keeps the original junction and just empties the target
            if entry.status == Status.OK and prev_entry and prev_entry.status == Status.OK:
                try:
                    target_empty = entry.target.exists() and not any(entry.target.iterdir())
                except OSError:
                    target_empty = False
                if not target_empty:
                    self._repair_shown.discard(entry.id)
                    if entry.id in self._confirmed_empty:
                        self._confirmed_empty.discard(entry.id)
                        _save_confirmed_empty(self._confirmed_empty)
                if target_empty:
                    new_dir = self._watcher.find_recent_create(entry.link.name, max_age_s=10.0)
                    if new_dir and new_dir.parent != entry.link.parent:
                        logging.info("Explorer cut+paste detected (junction kept): %s → %s", entry.link, new_dir)
                        self._repair_shown.discard(entry.id)
                        self._confirmed_empty.discard(entry.id)
                        self._root.after(3000, lambda e=entry, nd=new_dir: self._do_explorer_recovery_link(e, nd))
                        continue
                    elif (entry.id not in self._repair_shown
                          and entry.id not in self._confirmed_empty):
                        self._repair_shown.add(entry.id)
                        logging.info("Empty target detected for %s", entry.id)
                        self._show_empty_target_dialog(entry)
                        continue

            # ── Junction disappeared (OK → PENDING) ──────────────────────────
            if entry.status == Status.PENDING and prev_entry and prev_entry.status == Status.OK:

                # Case 1: renamed junction in the same parent dir
                new_path = mgr.find_renamed_junction(entry)
                if new_path:
                    mgr.rename_link_in_json(entry.id, new_path)
                    msg = f"{entry.id}: {entry.link.name} → {new_path.name}"
                    send_toast("Sym-Link: 已自动更新", msg)
                    show_banner(self._root, "Sym-Link: 已自动更新", msg)
                    self._entries = mgr.check_all()
                    logging.info("Rename detected: %s → %s", entry.link, new_path)
                    continue

                # Case 2: Explorer cut+paste of the junction (link)
                # Signature: junction gone + target exists but empty + new dir with same name appeared
                try:
                    target_empty = entry.target.exists() and not any(entry.target.iterdir())
                except OSError:
                    target_empty = False

                if target_empty:
                    new_dir = self._watcher.find_recent_create(entry.link.name, max_age_s=5.0)
                    if new_dir:
                        logging.info("Explorer cut+paste detected (link): %s → %s", entry.link, new_dir)
                        self._root.after(3000, lambda e=entry, nd=new_dir: self._do_explorer_recovery_link(e, nd))
                        continue

                # Fallback
                logging.warning("Junction gone, no rename/recovery found: %s", entry.id)
                self._show_repair_dialog(entry, "链接已失效且无法自动定位，请手动更新链接路径或目标路径。")

            # ── Target disappeared (OK → BROKEN) — possible cross-drive target move ──
            elif entry.status == Status.BROKEN and prev_entry and prev_entry.status == Status.OK:
                if os.path.lexists(entry.link) and not entry.target.exists():
                    new_dir = self._watcher.find_recent_create(entry.target.name, max_age_s=10.0)
                    if new_dir:
                        logging.info("Explorer cut+paste detected (target): %s → %s", entry.target, new_dir)
                        self._do_explorer_recovery_target(entry, new_dir)
                        continue

                self._show_repair_dialog(entry, "Target 目录已消失且无法自动定位，请手动更新目标路径。")

            # ── Other new BROKEN entry (PENDING/MISSING → BROKEN, or brand-new) ──
            elif entry.status == Status.BROKEN and (not prev_entry or prev_entry.status != Status.BROKEN):
                send_toast("Sym-Link: 断链", f"{entry.id}: target 不可达")

        self._tray.update(self._entries, self._last_sync, confirmed_empty=self._confirmed_empty)
        self._window.set_confirmed_empty(self._confirmed_empty)
        self._window.refresh(self._entries)
        self._watcher.update_watch_dirs(mgr.collect_watch_dirs(self._entries))

    def _do_explorer_recovery_link(self, entry: LinkEntry, new_dir: Path):
        """Called ~3 s after detecting Explorer cut+paste of a junction (link)."""
        try:
            if not new_dir.exists() or not new_dir.is_dir():
                raise FileNotFoundError(f"新目录已消失: {new_dir}")

            # Move contents from new_dir back to target
            for item in list(new_dir.iterdir()):
                shutil.move(str(item), str(entry.target / item.name))

            # Remove old junction if it still exists (Windows may not have deleted it)
            if os.path.lexists(entry.link):
                mgr._remove_link(entry.link)

            # Remove new_dir (now empty) and create junction there
            new_dir.rmdir()
            ok, err = mgr._create_junction(new_dir, entry.target)
            if not ok:
                raise RuntimeError(f"创建 junction 失败: {err}")

            mgr.rename_link_in_json(entry.id, new_dir)
            msg = f"{entry.id}: {entry.link.name} → {new_dir.name}"
            send_toast("Sym-Link: 已自动修复 Explorer 移动", msg)
            show_banner(self._root, "Sym-Link: 已自动修复 Explorer 移动", msg)
            logging.info("Explorer link recovery done: %s → %s", entry.link, new_dir)

        except Exception as exc:
            msg = f"{entry.id} 自动修复失败：{exc}\n请在状态窗口中手动重建"
            show_banner(self._root, "Sym-Link: 恢复失败", msg)
            logging.error("Explorer link recovery failed for %s: %s", entry.id, exc)

        self._entries = mgr.check_all()
        self._tray.update(self._entries, self._last_sync, confirmed_empty=self._confirmed_empty)
        self._window.refresh(self._entries)
        self._watcher.update_watch_dirs(mgr.collect_watch_dirs(self._entries))

    def _show_repair_dialog(self, entry: LinkEntry, reason: str):
        """Modal dialog asking the user to repair a broken entry manually."""
        dlg = tk.Toplevel(self._root)
        dlg.title("Sym-Link: 需要手动修复")
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)

        import tkinter.ttk as ttk
        pad = 16
        ttk.Label(dlg, text=f"「{entry.id}」需要手动修复", font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=pad, pady=(pad, 4))
        ttk.Label(dlg, text=reason, wraplength=360, justify="left").pack(
            anchor="w", padx=pad, pady=(0, 12))

        btn_row = ttk.Frame(dlg)
        btn_row.pack(fill="x", padx=pad, pady=(0, pad))

        def edit_now():
            dlg.destroy()
            self._do_open_window()

        ttk.Button(btn_row, text="立即修改", command=edit_now, width=12).pack(side="left")
        ttk.Button(btn_row, text="稍后修改", command=dlg.destroy, width=12).pack(side="left", padx=(8, 0))

        dlg.update_idletasks()
        sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
        w, h = dlg.winfo_width(), dlg.winfo_height()
        dlg.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

    def _show_empty_target_dialog(self, entry: LinkEntry):
        """Dialog when target is empty: let user confirm intentional or open status list."""
        import tkinter.ttk as ttk
        dlg = tk.Toplevel(self._root)
        dlg.title("Sym-Link: 目标目录为空")
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)

        pad = 16
        ttk.Label(dlg, text=f"「{entry.id}」的目标目录为空",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=pad, pady=(pad, 4))
        ttk.Label(dlg, text=f"{entry.target}\n\n可能是链接被移动到了未监听位置，也可能是目标目录本身已清空。",
                  wraplength=380, justify="left").pack(anchor="w", padx=pad, pady=(0, 12))

        btn_row = ttk.Frame(dlg)
        btn_row.pack(fill="x", padx=pad, pady=(0, pad))

        def confirm_empty():
            self._confirmed_empty.add(entry.id)
            self._repair_shown.discard(entry.id)
            _save_confirmed_empty(self._confirmed_empty)
            self._window.set_confirmed_empty(self._confirmed_empty)
            self._tray.update(self._entries, self._last_sync,
                              confirmed_empty=self._confirmed_empty)
            dlg.destroy()

        def edit_now():
            dlg.destroy()
            self._do_open_window()

        ttk.Button(btn_row, text="确认（目标为空）", command=confirm_empty, width=16).pack(side="left")
        ttk.Button(btn_row, text="立即修改",        command=edit_now,       width=10).pack(side="left", padx=(8, 0))
        ttk.Button(btn_row, text="稍后处理",        command=dlg.destroy,   width=10).pack(side="left", padx=(8, 0))

        dlg.update_idletasks()
        sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
        w, h = dlg.winfo_width(), dlg.winfo_height()
        dlg.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

    def _do_explorer_recovery_target(self, entry: LinkEntry, new_dir: Path):
        """Target was moved cross-drive; just update JSON and rebuild junction."""
        ok = mgr.edit_entry(entry.id, new_target=new_dir)
        if ok:
            msg = f"{entry.id}: target 路径已更新 → {new_dir.name}"
            show_banner(self._root, "Sym-Link: 已自动更新 target 路径", msg)
            logging.info("Explorer target recovery done: %s → %s", entry.target, new_dir)
        else:
            msg = f"{entry.id} target 路径更新失败，请手动重建"
            show_banner(self._root, "Sym-Link: 更新失败", msg)
        self._entries = mgr.check_all()
        self._tray.update(self._entries, self._last_sync, confirmed_empty=self._confirmed_empty)
        self._window.refresh(self._entries)
        self._watcher.update_watch_dirs(mgr.collect_watch_dirs(self._entries))

    def _do_quit(self):
        self._watcher.stop()
        self._root.quit()

    def _on_relink_entry(self, entry_id: str):
        """Rebuild junction + confirm empty target for the given entry."""
        ok = mgr.edit_entry(entry_id)
        self._confirmed_empty.add(entry_id)
        self._repair_shown.discard(entry_id)
        _save_confirmed_empty(self._confirmed_empty)
        self._entries = mgr.check_all()
        self._tray.update(self._entries, self._last_sync, confirmed_empty=self._confirmed_empty)
        self._window.set_confirmed_empty(self._confirmed_empty)
        self._window.refresh(self._entries)
        if not ok:
            from tkinter import messagebox
            messagebox.showerror("重连失败", f"「{entry_id}」junction 重建失败，请检查路径。")

    def _on_entry_saved(self, entry_id: str):
        """Called when user saves an entry via the edit dialog — counts as confirming empty target."""
        self._confirmed_empty.add(entry_id)
        self._repair_shown.discard(entry_id)
        _save_confirmed_empty(self._confirmed_empty)
        self._window.set_confirmed_empty(self._confirmed_empty)
        self._tray.update(self._entries, self._last_sync,
                          confirmed_empty=self._confirmed_empty)

    def _on_settings_applied(self, new_settings: sm.Settings):
        self._settings = new_settings
        self._watcher.set_interval(new_settings.check_interval_minutes * 60)
        self._apply_close_to_tray(new_settings.close_to_tray)
        logging.info("Settings applied: interval=%dmin autostart=%s close_to_tray=%s",
                     new_settings.check_interval_minutes,
                     new_settings.autostart,
                     new_settings.close_to_tray)


def main():
    _setup_logging()

    if mgr.get_onedrive() is None:
        import socket
        import tkinter.messagebox as mb
        tk.Tk().withdraw()
        mb.showerror(
            "Sym-Link",
            f"机器 '{socket.gethostname()}' 未在 symlinks.json 中注册。\n"
            "请先在 machines 表中添加此机器。",
        )
        sys.exit(1)

    App().run()


if __name__ == "__main__":
    main()
