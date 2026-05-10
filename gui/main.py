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
from typing import Optional
from core import settings_manager as sm
from core import symlink_manager as mgr
from ui.icons import app_icon
from ui.utils import center_window

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
from ui.notifier import send_toast, show_banner
from ui.registration_window import RegistrationWindow
from core.symlink_manager import LinkEntry, Status
from ui.scan_window import ScanWindow
from ui.settings_window import SettingsWindow
from ui.tray import TrayIcon
from core.watcher import BackgroundWatcher
from ui.window import StatusWindow

_LOG_PATH = Path(__file__).parent / "symlink-gui.log"
_POLL_MS  = 100


def _setup_logging():
    _LOG_PATH.unlink(missing_ok=True)          # fresh log each run
    Path(str(_LOG_PATH) + ".1").unlink(missing_ok=True)
    handler = logging.FileHandler(_LOG_PATH, encoding="utf-8")
    logging.basicConfig(
        level=logging.DEBUG,
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
        self._quitting = False
        self._pending_new_junction_prompt: set[str] = set()  # lower-case normalised path strings

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
            on_open_scan=self._do_open_scan,
            on_manage_bases=self._do_manage_bases,
        )
        self._settings_win = SettingsWindow(
            root=self._root,
            on_apply=self._on_settings_applied,
        )
        self._scan_window = ScanWindow(
            root=self._root,
            on_done=self._request_refresh,
        )
        self._watcher = BackgroundWatcher(
            self._q,
            check_interval_seconds=self._settings.check_interval_minutes * 60,
        )

        self._apply_close_to_tray(self._settings.close_to_tray)

        # Migrate old-format JSON (machine field / top-level scanned) if needed
        mgr.migrate_to_local_data()
        mgr.normalize_entries()

        # Prompt registration if machine not yet configured
        if not mgr.is_registered():
            RegistrationWindow(self._root).show_modal()

        # Warn about bases used globally but not yet handled on this machine
        self._root.after(200, self._check_pending_bases)

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
        if self._quitting:
            return
        try:
            while True:
                msg = self._q.get_nowait()
                self._handle(msg)
        except queue.Empty:
            pass
        if not self._quitting:
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

    def _next_check_time(self):
        if self._last_sync is None:
            return None
        return self._last_sync + timedelta(
            seconds=self._settings.check_interval_minutes * 60
        )

    def _refresh_ui(self):
        """Update tray, window, and watcher dirs from current self._entries."""
        self._tray.update(self._entries, self._last_sync, self._next_check_time(),
                          confirmed_empty=self._confirmed_empty)
        self._window.set_confirmed_empty(self._confirmed_empty)
        self._window.refresh(self._entries)
        self._watcher.update_watch_dirs(mgr.collect_watch_dirs(self._entries))

    def _do_smart_sync(self):
        known_ids = {e.id for e in self._entries}
        result = mgr.smart_sync(known_ids)
        self._last_sync = datetime.now()
        self._entries = mgr.check_all()
        self._refresh_ui()
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
        nd, np = mgr.normalize_entries()
        if nd or np:
            logging.info("normalize_entries: %d demoted, %d promoted", nd, np)
        result = mgr.sync_all()
        self._last_sync = datetime.now()
        self._entries = mgr.check_all()
        current_ids = {e.id for e in self._entries}
        stale = (self._confirmed_empty - current_ids) | {
            e.id for e in self._entries if e.id in self._confirmed_empty and not e.target_empty
        }
        if stale:
            self._confirmed_empty -= stale
            _save_confirmed_empty(self._confirmed_empty)
        self._refresh_ui()
        mgr.refresh_machine_drives()
        self._watcher.update_drive_roots(mgr.get_machine_drives())

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
        mgr.normalize_entries()
        self._entries = mgr.check_all()
        self._refresh_ui()
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

    def _find_moved_junction(self, entry: LinkEntry) -> Optional[Path]:
        """Check recent_creates for a same-named junction pointing to entry.target.

        Handles the case where a junction was moved between two watched base
        directories: watchdog fires separate Delete+Create events, so on_moved
        never fires, but the new junction appears in recent_creates.
        """
        new_loc = self._watcher.find_recent_create(entry.link.name, max_age_s=8.0)
        if not new_loc or not new_loc.is_junction():
            return None
        try:
            rp = str(Path(os.readlink(str(new_loc)))).replace("\\", "/")
            for pfx in ("//?/", "//./"):
                if rp.startswith(pfx):
                    rp = rp[len(pfx):]
                    break
            if rp.lower() == str(entry.target).replace("\\", "/").lower():
                return new_loc
        except OSError:
            pass
        return None

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
        mgr.normalize_entries()
        prev = {e.id: e for e in self._entries}
        self._entries = mgr.check_all()
        _repaths_done: set[str] = set()   # avoid duplicate repath calls per ancestor

        # Prune confirmed_empty: remove deleted entries and entries whose target is now non-empty
        current_ids = {e.id for e in self._entries}
        stale = (self._confirmed_empty - current_ids) | {
            e.id for e in self._entries if e.id in self._confirmed_empty and not e.target_empty
        }
        if stale:
            self._confirmed_empty -= stale
            _save_confirmed_empty(self._confirmed_empty)
            self._window.set_confirmed_empty(self._confirmed_empty)

        for entry in list(self._entries):   # snapshot: reassigning self._entries mid-loop is safe
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
                        self._refresh_ui()
                        msg = f"已更新 {len(updated)} 项路径：{old_base.name} → {new_base.name}"
                        if failed:
                            msg += f"，{len(failed)} 项 junction 重建失败"
                        send_toast("Sym-Link: 已自动更新路径", msg)
                        show_banner(self._root, "Sym-Link: 已自动更新路径", msg)
                else:
                    # Check if the junction itself moved between watched base dirs
                    new_link = self._find_moved_junction(entry)
                    if new_link:
                        logging.info("Junction moved: %s → %s", entry.link, new_link)
                        mgr.edit_entry(entry.id, new_link=new_link)
                        msg = f"{entry.id}: {entry.link.parent.name} → {new_link.parent.name}"
                        send_toast("Sym-Link: 已自动更新", msg)
                        show_banner(self._root, "Sym-Link: 已自动更新", msg)
                        self._entries = mgr.check_all()
                        continue  # process remaining entries; stale prev is harmless here
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
                          and entry.id not in self._confirmed_empty
                          and not prev_entry.target_empty):  # only fire when target just became empty
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

        self._check_for_new_junctions()
        self._refresh_ui()

    def _check_for_new_junctions(self):
        """Auto-register or prompt for unmanaged junctions recently created within a base dir."""
        bases = mgr.get_machine_config()
        if not bases:
            return
        recent = self._watcher.get_recent_creates(max_age_s=30.0)
        if not recent:
            return

        def _norm(p) -> str:
            return str(p).replace("/", "\\").rstrip("\\").lower()

        base_keys = [_norm(v) for v in bases.values() if v]
        managed_links = {_norm(e.link) for e in self._entries}

        to_auto:   list[tuple[Path, Path, str]] = []
        to_prompt: list[tuple[Path, Path, str]] = []

        existing_ids = mgr.get_all_entry_ids()

        for path in recent:
            # Do NOT resolve() — it follows the junction to the target directory
            rkey = _norm(path)
            if rkey in self._pending_new_junction_prompt:
                continue
            if rkey in managed_links:
                continue
            if not path.is_junction():
                continue
            if not any(rkey.startswith(bk + "\\") for bk in base_keys):
                continue
            try:
                target_str = os.readlink(str(path))
                if target_str.startswith("\\\\?\\"):
                    target_str = target_str[4:]
                target = Path(target_str)
            except OSError:
                continue

            suggested_id = path.name
            if suggested_id not in existing_ids:
                to_auto.append((path, target, suggested_id))
                existing_ids = existing_ids | {suggested_id}
            else:
                to_prompt.append((path, target, suggested_id))

        registered: list[str] = []
        for path, target, eid in to_auto:
            ok, err = mgr.create_entry(eid, "", path, target)
            if ok:
                logging.info("Auto-registered new junction '%s': %s → %s", eid, path, target)
                registered.append(eid)
                try:
                    if not any(target.iterdir()):
                        self._repair_shown.add(eid)
                except OSError:
                    pass
            else:
                logging.warning("Auto-register failed for %s: %s", path, err)

        if registered:
            mgr.normalize_entries()
            self._entries = mgr.check_all()
            names = "、".join(registered)
            show_banner(self._root, "Sym-Link: 已自动托管新链接",
                        f"检测到 {len(registered)} 个新 Junction：{names}")

        for path, target, suggested_id in to_prompt:
            self._pending_new_junction_prompt.add(_norm(path))
            self._root.after(0, lambda p=path, t=target, s=suggested_id:
                             self._show_new_junction_dialog(p, t, s))

    def _show_new_junction_dialog(self, path: Path, target: Path, suggested_id: str):
        """Prompt the user to name a new junction whose auto-ID is already taken."""
        import tkinter.ttk as ttk
        from ui.window import _shorten
        bases = mgr.get_machine_config() or {}

        dlg = tk.Toplevel(self._root)
        dlg.title("Sym-Link: 发现未管理的 Junction")
        dlg.resizable(False, False)
        dlg.attributes("-topmost", True)

        pad = 16
        ttk.Label(dlg, text="发现未管理的 Junction",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=pad, pady=(pad, 4))
        ttk.Label(dlg,
                  text=f"链接：{_shorten(str(path), bases)}\n目标：{_shorten(str(target), bases)}",
                  justify="left").pack(anchor="w", padx=pad, pady=(0, 4))
        ttk.Label(dlg, text=f"名称「{suggested_id}」已被占用，请输入新名称：",
                  justify="left").pack(anchor="w", padx=pad, pady=(0, 4))

        id_var = tk.StringVar(value=suggested_id)
        id_entry = ttk.Entry(dlg, textvariable=id_var, width=32)
        id_entry.pack(anchor="w", padx=pad, pady=(0, 8))
        id_entry.select_range(0, "end")
        id_entry.focus_set()

        btn_row = ttk.Frame(dlg)
        btn_row.pack(fill="x", padx=pad, pady=(0, pad))

        def skip():
            dlg.destroy()   # path stays in _pending; won't re-prompt this session

        def register():
            from tkinter import messagebox
            new_id = id_var.get().strip()
            if not new_id:
                messagebox.showwarning("名称不能为空", "请输入名称。", parent=dlg)
                return
            if new_id in mgr.get_all_entry_ids():
                messagebox.showerror("名称已占用", f"「{new_id}」已存在，请换一个名称。", parent=dlg)
                return
            ok, err = mgr.create_entry(new_id, "", path, target)
            if ok:
                logging.info("User-registered junction '%s': %s → %s", new_id, path, target)
                mgr.normalize_entries()
                self._pending_new_junction_prompt.discard(
                    str(path).replace("/", "\\").rstrip("\\").lower())
                dlg.destroy()
                self._do_refresh()
            else:
                messagebox.showerror("注册失败", err, parent=dlg)

        dlg.bind("<Return>", lambda _e: register())
        ttk.Button(btn_row, text="跳过", command=skip,     width=8).pack(side="right", padx=(6, 0))
        ttk.Button(btn_row, text="注册", command=register, width=8).pack(side="right")

        center_window(dlg)

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

            # Verify move completed fully before touching new_dir
            remaining = list(new_dir.iterdir())
            if remaining:
                raise RuntimeError(
                    f"移动未完全成功，{len(remaining)} 个文件仍在 {new_dir}，已中止修复"
                )

            # Remove new_dir (now empty) and create junction there
            new_dir.rmdir()
            ok, err = mgr._create_junction(new_dir, entry.target)
            if not ok:
                raise RuntimeError(f"创建 junction 失败: {err}")

            mgr.rename_link_in_json(entry.id, new_dir)
            mgr.normalize_entries()
            msg = f"{entry.id}: {entry.link.name} → {new_dir.name}"
            send_toast("Sym-Link: 已自动修复 Explorer 移动", msg)
            show_banner(self._root, "Sym-Link: 已自动修复 Explorer 移动", msg)
            logging.info("Explorer link recovery done: %s → %s", entry.link, new_dir)

        except Exception as exc:
            msg = f"{entry.id} 自动修复失败：{exc}\n请在状态窗口中手动重建"
            show_banner(self._root, "Sym-Link: 恢复失败", msg)
            logging.error("Explorer link recovery failed for %s: %s", entry.id, exc)

        self._entries = mgr.check_all()
        self._refresh_ui()

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

        center_window(dlg)

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
            self._tray.update(self._entries, self._last_sync, self._next_check_time(),
                              confirmed_empty=self._confirmed_empty)
            dlg.destroy()

        def edit_now():
            dlg.destroy()
            self._do_open_window()

        ttk.Button(btn_row, text="确认（目标为空）", command=confirm_empty, width=16).pack(side="left")
        ttk.Button(btn_row, text="立即修改",        command=edit_now,       width=10).pack(side="left", padx=(8, 0))
        ttk.Button(btn_row, text="稍后处理",        command=dlg.destroy,   width=10).pack(side="left", padx=(8, 0))

        center_window(dlg)

    def _do_explorer_recovery_target(self, entry: LinkEntry, new_dir: Path):
        """Target was moved cross-drive; just update JSON and rebuild junction."""
        ok = mgr.edit_entry(entry.id, new_target=new_dir)
        mgr.normalize_entries()
        if ok:
            msg = f"{entry.id}: target 路径已更新 → {new_dir.name}"
            show_banner(self._root, "Sym-Link: 已自动更新 target 路径", msg)
            logging.info("Explorer target recovery done: %s → %s", entry.target, new_dir)
        else:
            msg = f"{entry.id} target 路径更新失败，请手动重建"
            show_banner(self._root, "Sym-Link: 更新失败", msg)
        self._entries = mgr.check_all()
        self._refresh_ui()

    def _do_quit(self):
        self._quitting = True
        threading.Thread(target=self._watcher.stop, daemon=True, name="quit-cleanup").start()
        self._root.destroy()

    def _do_open_scan(self):
        self._scan_window.show(self._entries)

    def _do_manage_bases(self):
        RegistrationWindow(self._root).show()

    def _check_pending_bases(self):
        """Show a warning if global symlinks use bases not yet handled on this machine."""
        pending = mgr.get_pending_bases()
        if not pending:
            return
        import tkinter.ttk as ttk
        from tkinter import messagebox
        keys_str = "\n".join(f"  {{{k}}}" for k in sorted(pending))
        ans = messagebox.askquestion(
            "同步目录未配置",
            f"以下同步目录在全局配置中被使用，但本机尚未配置路径：\n\n"
            + keys_str
            + "\n\n是否立即配置？（选否则相关条目将在本次运行中被跳过）",
            icon="warning",
        )
        if ans == "yes":
            RegistrationWindow(self._root).show()

    def _on_relink_entry(self, entry_id: str):
        """Rebuild junction + confirm empty target for the given entry."""
        ok = mgr.edit_entry(entry_id)
        mgr.normalize_entries()
        if ok:
            self._confirmed_empty.add(entry_id)
            self._repair_shown.discard(entry_id)
            _save_confirmed_empty(self._confirmed_empty)
        self._entries = mgr.check_all()
        self._refresh_ui()
        if not ok:
            from tkinter import messagebox
            messagebox.showerror("重连失败", f"「{entry_id}」junction 重建失败，请检查路径。")

    def _on_entry_saved(self, entry_id: str):
        """Called when user saves an entry via the edit dialog."""
        mgr.normalize_entries()
        self._repair_shown.discard(entry_id)
        self._entries = mgr.check_all()   # refresh before checking target_empty
        entry = next((e for e in self._entries if e.id == entry_id), None)
        if entry and entry.status == Status.OK and entry.target_empty:
            self._confirmed_empty.add(entry_id)
            _save_confirmed_empty(self._confirmed_empty)
        self._window.set_confirmed_empty(self._confirmed_empty)
        self._tray.update(self._entries, self._last_sync, self._next_check_time(),
                          confirmed_empty=self._confirmed_empty)

    def _on_settings_applied(self, new_settings: sm.Settings):
        self._settings = new_settings
        self._watcher.set_interval(new_settings.check_interval_minutes * 60)
        self._apply_close_to_tray(new_settings.close_to_tray)
        mgr.refresh_machine_drives()
        self._watcher.update_drive_roots(mgr.get_machine_drives())
        logging.info("Settings applied: interval=%dmin autostart=%s close_to_tray=%s",
                     new_settings.check_interval_minutes,
                     new_settings.autostart,
                     new_settings.close_to_tray)


def main():
    _setup_logging()
    App().run()


if __name__ == "__main__":
    main()
