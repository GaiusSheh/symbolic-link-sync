"""First-run setup wizard: let the user choose where symlinks.json lives."""

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ui.utils import center_window


class SetupWizard:
    """Modal dialog shown on first run to configure the symlinks.json location.

    Returns the chosen Path on confirm, or None if the user skips.
    """

    def __init__(self, root: tk.Tk):
        self._root  = root
        self._result: Path | None = None
        self._win: tk.Toplevel | None = None

    def show_modal(self) -> Path | None:
        self._build()
        self._win.grab_set()
        self._root.wait_window(self._win)
        return self._result

    def _build(self):
        win = tk.Toplevel(self._root)
        self._win = win
        win.title("配置 Sym-Link")
        win.resizable(False, False)
        win.protocol("WM_DELETE_WINDOW", self._skip)

        outer = ttk.Frame(win, padding=20)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)

        # ── Mode selection ────────────────────────────────────────────────────
        ttk.Label(outer, text="选择如何开始：",
                  font=("Segoe UI", 10, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))

        self._mode = tk.StringVar(value="new")
        ttk.Radiobutton(outer, text="创建新配置（首次使用）",
                        variable=self._mode, value="new",
                        command=self._on_mode_change).grid(
            row=1, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(outer, text="使用已有配置（其他设备已使用过）",
                        variable=self._mode, value="existing",
                        command=self._on_mode_change).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(4, 12))

        # ── Path entry ────────────────────────────────────────────────────────
        self._path_label = ttk.Label(outer, text="存放目录：")
        self._path_label.grid(row=3, column=0, sticky="e", padx=(0, 8))

        self._path_var = tk.StringVar()
        ttk.Entry(outer, textvariable=self._path_var, width=44).grid(
            row=3, column=1, sticky="ew")

        self._browse_btn = ttk.Button(outer, text="浏览…", command=self._browse, width=7)
        self._browse_btn.grid(row=3, column=2, padx=(6, 0))

        # ── Cloud-sync tip ────────────────────────────────────────────────────
        tip_frame = ttk.Frame(outer, padding=(0, 10, 0, 0))
        tip_frame.grid(row=4, column=0, columnspan=3, sticky="ew")

        ttk.Label(tip_frame, text="ℹ",
                  font=("Segoe UI", 11), foreground="#1565C0").pack(side="left", anchor="n",
                                                                     padx=(0, 6))
        ttk.Label(tip_frame,
                  text="建议将配置文件放在可跨设备同步的网盘目录中（如 OneDrive、\n"
                       "Google Drive 等），以便在多台设备间共享链接配置。",
                  justify="left", foreground="#555555").pack(side="left", fill="x")

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_row = ttk.Frame(outer, padding=(0, 16, 0, 0))
        btn_row.grid(row=5, column=0, columnspan=3, sticky="e")
        ttk.Button(btn_row, text="跳过",  command=self._skip,    width=8).pack(
            side="right", padx=(8, 0))
        ttk.Button(btn_row, text="确认 →", command=self._confirm, width=10).pack(side="right")

        center_window(win)

    def _on_mode_change(self):
        if self._mode.get() == "new":
            self._path_label.configure(text="存放目录：")
        else:
            self._path_label.configure(text="配置文件：")
        self._path_var.set("")

    def _browse(self):
        if self._mode.get() == "new":
            chosen = filedialog.askdirectory(
                parent=self._win,
                title="选择配置文件存放目录（建议选择网盘目录）",
            )
            if chosen:
                self._path_var.set(chosen)
        else:
            chosen = filedialog.askopenfilename(
                parent=self._win,
                title="选择已有的 symlinks.json",
                filetypes=[("JSON 文件", "*.json"), ("所有文件", "*.*")],
            )
            if chosen:
                self._path_var.set(chosen)

    def _confirm(self):
        raw = self._path_var.get().strip()
        if not raw:
            messagebox.showwarning("路径不能为空", "请先选择配置文件位置。", parent=self._win)
            return

        if self._mode.get() == "new":
            chosen_dir = Path(raw)
            if not chosen_dir.is_dir():
                messagebox.showwarning("目录无效",
                                       f"目录不存在：\n{chosen_dir}",
                                       parent=self._win)
                return
            self._result = chosen_dir / "symlinks.json"
        else:
            chosen_file = Path(raw)
            if not chosen_file.is_file():
                messagebox.showwarning("文件不存在",
                                       f"找不到该文件：\n{chosen_file}",
                                       parent=self._win)
                return
            self._result = chosen_file

        self._win.destroy()

    def _skip(self):
        self._result = None
        self._win.destroy()
