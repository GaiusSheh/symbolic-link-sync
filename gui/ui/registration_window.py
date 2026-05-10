"""Machine registration dialog: configure sync-service base paths for this machine."""

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from core import symlink_manager as mgr
from ui.utils import apply_base_registration, build_base_row


class RegistrationWindow:
    """Modal dialog to register (or re-configure) base paths for the current machine."""

    def __init__(self, root: tk.Tk):
        self._root = root
        self._win: tk.Toplevel | None = None
        self._rows: list[dict] = []   # [{name_var, path_var, frame}, ...]

    # ── Public ────────────────────────────────────────────────────────────────

    def show_modal(self) -> bool:
        """Show the dialog and block until dismissed. Returns True if registered."""
        self._registered = False
        self._build()
        self._win.grab_set()
        self._root.wait_window(self._win)
        return self._registered

    def show(self):
        """Show non-modal (for Settings → re-configure)."""
        if self._win and self._win.winfo_exists():
            self._win.lift(); self._win.focus_force(); return
        self._build()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self):
        win = tk.Toplevel(self._root)
        self._win = win
        win.title("注册当前计算机")
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self._on_skip)

        outer = ttk.Frame(win, padding=16)
        outer.pack(fill="both", expand=True)

        # Header
        ttk.Label(outer, text=f"当前计算机：{mgr._machine_name()}",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 4))
        ttk.Label(outer, text="请配置此机器上的同步目录（至少一条）：",
                  foreground="gray").pack(anchor="w", pady=(0, 8))

        # Rows container
        self._rows_frame = ttk.Frame(outer)
        self._rows_frame.pack(fill="x", pady=(0, 8))

        # Load existing config or auto-detect
        existing_full = mgr.get_machine_config_full() or {}
        detected = mgr.detect_sync_services() if not existing_full else {}
        init_bases = existing_full if existing_full else detected

        self._rows = []
        if init_bases:
            for name, path in init_bases.items():
                ignored = (path is None)
                self._add_row(name, path or "", ignored=ignored)
        else:
            self._add_row("", "")

        # Add row button
        ttk.Button(outer, text="＋ 添加同步目录",
                   command=lambda: self._add_row("", "")).pack(anchor="w", pady=(0, 12))

        # Buttons
        btn_row = ttk.Frame(outer)
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="完成注册", command=self._confirm, width=12).pack(side="left")
        ttk.Button(btn_row, text="稍后",     command=self._on_skip,  width=8).pack(
            side="left", padx=(8, 0))

        # Centre
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        w, h   = win.winfo_width(), win.winfo_height()
        win.geometry(f"+{(sw-w)//2}+{(sh-h)//2}")

    def _add_row(self, name: str, path: str, ignored: bool = False):
        build_base_row(
            self._win, self._rows, self._rows_frame,
            name=name, path=path, ignored=ignored,
            ignored_label="此机器不使用", name_width=14, path_width=36, browse_text="浏览…",
        )

    # ── Actions ───────────────────────────────────────────────────────────────

    def _confirm(self):
        bases: dict[str, str | None] = {}
        for row in self._rows:
            name    = row["name_var"].get().strip()
            path    = row["path_var"].get().strip()
            ignored = row["ignored_var"].get()
            if not name and not path and not ignored:
                continue
            if not name:
                messagebox.showwarning("名称不能为空", "每条同步目录必须填写名称。",
                                       parent=self._win)
                return
            if name in bases:
                messagebox.showwarning("名称重复", f"名称「{name}」重复，请修改。",
                                       parent=self._win)
                return
            if ignored:
                bases[name] = None
                continue
            if not path or not Path(path).is_dir():
                messagebox.showwarning("路径无效", f"「{name}」的路径不存在或不是目录。",
                                       parent=self._win)
                return
            bases[name] = path.replace("\\", "/")

        if not bases:
            messagebox.showwarning("至少一条", "请至少填写一条同步目录配置。", parent=self._win)
            return

        if not apply_base_registration(self._win, bases, "此机器不使用"):
            return
        self._registered = True
        self._win.destroy()

    def _on_skip(self):
        self._registered = False
        self._win.destroy()
