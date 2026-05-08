"""Settings dialog."""

import tkinter as tk
from tkinter import ttk
from typing import Callable

import settings_manager as sm


def _center(win: tk.Toplevel):
    win.update()
    w, h = win.winfo_width(), win.winfo_height()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")


class SettingsWindow:
    def __init__(self, root: tk.Tk, on_apply: Callable[[sm.Settings], None]):
        self._root = root
        self._on_apply = on_apply
        self._win: tk.Toplevel | None = None

    def show(self):
        if self._win and self._win.winfo_exists():
            self._win.deiconify()
            self._win.lift()
            self._win.focus_force()
            return
        self._build()

    def _build(self):
        s = sm.load()
        s.autostart = sm.get_autostart()  # registry is ground truth

        win = tk.Toplevel(self._root)
        self._win = win
        win.title("Sym-Link 设置")
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", win.withdraw)

        outer = ttk.Frame(win, padding=16)
        outer.pack(fill="both", expand=True)

        # ── Check interval ──────────────────────────────────────────────────
        interval_lf = ttk.LabelFrame(outer, text="检测间隔", padding=10)
        interval_lf.pack(fill="x", pady=(0, 10))

        row = ttk.Frame(interval_lf)
        row.pack(fill="x")
        ttk.Label(row, text="每隔").pack(side="left")

        self._interval_var = tk.IntVar(value=s.check_interval_minutes)
        ttk.Spinbox(row, from_=1, to=1440, textvariable=self._interval_var,
                    width=6).pack(side="left", padx=6)
        ttk.Label(row, text="分钟检测一次断链").pack(side="left")

        # ── Behavior ────────────────────────────────────────────────────────
        behavior_lf = ttk.LabelFrame(outer, text="行为", padding=10)
        behavior_lf.pack(fill="x", pady=(0, 16))

        self._autostart_var = tk.BooleanVar(value=s.autostart)
        ttk.Checkbutton(behavior_lf, text="开机自动启动",
                        variable=self._autostart_var).pack(anchor="w", pady=(0, 6))

        self._close_to_tray_var = tk.BooleanVar(value=s.close_to_tray)
        ttk.Checkbutton(behavior_lf,
                        text="关闭窗口时最小化到托盘（取消则直接退出）",
                        variable=self._close_to_tray_var).pack(anchor="w")

        # ── Buttons ─────────────────────────────────────────────────────────
        btn_row = ttk.Frame(outer)
        btn_row.pack(fill="x")
        ttk.Button(btn_row, text="取消", command=win.withdraw,
                   width=10).pack(side="right", padx=(6, 0))
        ttk.Button(btn_row, text="确定", command=self._apply,
                   width=10).pack(side="right")

        _center(win)
        win.lift()
        win.focus_force()

    def _apply(self):
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
