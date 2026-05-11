"""Settings dialog."""

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from core import settings_manager as sm
from core import symlink_manager as mgr
from ui.utils import apply_base_registration, build_base_row, center_window


class SettingsWindow:
    def __init__(self, root: tk.Tk, on_apply: Callable[[sm.Settings], None]):
        self._root    = root
        self._on_apply = on_apply
        self._win: tk.Toplevel | None = None
        self._base_rows: list[dict] = []

    def show(self):
        if self._win and self._win.winfo_exists():
            self._win.deiconify()
            self._win.lift()
            self._win.focus_force()
            return
        self._build()

    def _build(self):
        s = sm.load()
        s.autostart = sm.get_autostart()

        win = tk.Toplevel(self._root)
        self._win = win
        win.title("SymLiSync 设置")
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", win.withdraw)

        outer = ttk.Frame(win, padding=16)
        outer.pack(fill="both", expand=True)

        # ── Check interval ────────────────────────────────────────────────────
        interval_lf = ttk.LabelFrame(outer, text="检测间隔", padding=10)
        interval_lf.pack(fill="x", pady=(0, 10))

        row = ttk.Frame(interval_lf)
        row.pack(fill="x")
        ttk.Label(row, text="每隔").pack(side="left")
        self._interval_var = tk.IntVar(value=s.check_interval_minutes)
        ttk.Spinbox(row, from_=1, to=1440, textvariable=self._interval_var,
                    width=6).pack(side="left", padx=6)
        ttk.Label(row, text="分钟检测一次断链").pack(side="left")

        # ── Behavior ──────────────────────────────────────────────────────────
        behavior_lf = ttk.LabelFrame(outer, text="行为", padding=10)
        behavior_lf.pack(fill="x", pady=(0, 10))

        self._autostart_var = tk.BooleanVar(value=s.autostart)
        ttk.Checkbutton(behavior_lf, text="开机自动启动",
                        variable=self._autostart_var).pack(anchor="w", pady=(0, 6))

        self._close_to_tray_var = tk.BooleanVar(value=s.close_to_tray)
        ttk.Checkbutton(behavior_lf,
                        text="关闭窗口时最小化到托盘（取消则直接退出）",
                        variable=self._close_to_tray_var).pack(anchor="w")

        # ── Sync directories (base paths) ─────────────────────────────────────
        bases_lf = ttk.LabelFrame(outer, text="同步目录", padding=10)
        bases_lf.pack(fill="x", pady=(0, 16))

        ttk.Label(bases_lf,
                  text="每个同步目录对应一个路径模板（如 {onedrive}）",
                  foreground="gray").pack(anchor="w", pady=(0, 6))

        self._bases_frame = ttk.Frame(bases_lf)
        self._bases_frame.pack(fill="x")
        self._base_rows = []

        bases = mgr.get_machine_config_full() or {}
        for name, path in bases.items():
            self._add_base_row(name, path or "", ignored=(path is None))

        ttk.Button(bases_lf, text="＋ 添加", width=8,
                   command=lambda: self._add_base_row("", "")).pack(
            anchor="w", pady=(6, 0))

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_row = ttk.Frame(outer)
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="取消", command=win.withdraw,
                   width=10).pack(side="right", padx=(6, 0))
        ttk.Button(btn_row, text="确定", command=self._apply,
                   width=10).pack(side="right")

        center_window(win)
        win.lift()
        win.focus_force()

    def _add_base_row(self, name: str, path: str, ignored: bool = False):
        row = build_base_row(
            self._win, self._base_rows, self._bases_frame,
            name=name, path=path, ignored=ignored,
            ignored_label="不使用", name_width=12, path_width=30, browse_text="浏览",
        )
        row["orig_name"] = name

    def _apply(self):
        # ── Validate and collect bases ────────────────────────────────────────
        bases: dict[str, str | None] = {}
        renames: list[tuple[str, str]] = []   # (old_key, new_key)

        for row in self._base_rows:
            name    = row["name_var"].get().strip()
            path    = row["path_var"].get().strip()
            ignored = row["ignored_var"].get()
            if not name and not path and not ignored:
                continue
            if not name:
                messagebox.showwarning("名称不能为空", "请为每条同步目录填写名称。",
                                       parent=self._win)
                return
            if name in bases:
                messagebox.showwarning("名称重复", f"名称「{name}」重复，请修改。",
                                       parent=self._win)
                return
            if ignored:
                bases[name] = None
            else:
                if not path or not Path(path).is_dir():
                    messagebox.showwarning("路径无效", f"「{name}」的路径不存在。",
                                           parent=self._win)
                    return
                bases[name] = path.replace("\\", "/")
            orig = row.get("orig_name", "")
            if orig and orig != name:
                renames.append((orig, name))

        # ── Apply base renames (rebase template keys) ─────────────────────────
        if renames:
            saved_config = mgr.get_machine_config_full() or {}
            valid_renames = [(old, new) for old, new in renames if old in saved_config]
            # Conflict check: new_key must not already exist unless it is itself being renamed away
            old_keys = {old for old, _ in valid_renames}
            for old_key, new_key in valid_renames:
                if new_key in saved_config and new_key not in old_keys:
                    messagebox.showwarning(
                        "名称冲突",
                        f"将「{old_key}」改名为「{new_key}」与现有 base 冲突，请换一个名称。",
                        parent=self._win,
                    )
                    return
            if valid_renames:
                mgr.rename_base_key(valid_renames)

        if not apply_base_registration(self._win, bases, "不使用"):
            return

        # ── Apply other settings ──────────────────────────────────────────────
        interval = max(1, min(1440, self._interval_var.get()))
        new_s = sm.Settings(
            check_interval_minutes=interval,
            autostart=self._autostart_var.get(),
            close_to_tray=self._close_to_tray_var.get(),
        )
        sm.save(new_s)
        sm.set_autostart(new_s.autostart)
        self._on_apply(new_s)
        self._win.withdraw()
