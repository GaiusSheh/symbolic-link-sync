"""Settings dialog."""

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

import settings_manager as sm
import symlink_manager as mgr


def _center(win: tk.Toplevel):
    win.update()
    w, h = win.winfo_width(), win.winfo_height()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")


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
        win.title("Sym-Link 设置")
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

        _center(win)
        win.lift()
        win.focus_force()

    def _add_base_row(self, name: str, path: str, ignored: bool = False):
        row_frame = ttk.Frame(self._bases_frame)
        row_frame.pack(fill="x", pady=2)

        name_var    = tk.StringVar(value=name)
        path_var    = tk.StringVar(value=path)
        ignored_var = tk.BooleanVar(value=ignored)

        ttk.Label(row_frame, text="名称：", width=6, anchor="e").pack(side="left")
        ttk.Entry(row_frame, textvariable=name_var, width=12).pack(side="left", padx=(0, 6))
        ttk.Label(row_frame, text="路径：", width=5, anchor="e").pack(side="left")
        path_entry = ttk.Entry(row_frame, textvariable=path_var, width=30)
        path_entry.pack(side="left", padx=(0, 4))

        def browse(pv=path_var):
            cur  = pv.get()
            init = cur if cur and Path(cur).is_dir() else str(Path.home())
            p = filedialog.askdirectory(parent=self._win, title="选择同步目录根路径",
                                        initialdir=init)
            if p:
                pv.set(p)

        browse_btn = ttk.Button(row_frame, text="浏览", command=browse, width=5)
        browse_btn.pack(side="left", padx=(0, 4))

        def _toggle_ignored(*_):
            state = "disabled" if ignored_var.get() else "normal"
            path_entry.config(state=state)
            browse_btn.config(state=state)

        ignored_var.trace_add("write", _toggle_ignored)
        _toggle_ignored()

        ttk.Checkbutton(row_frame, text="不使用", variable=ignored_var).pack(
            side="left", padx=(0, 4))

        row = {"name_var": name_var, "path_var": path_var,
               "ignored_var": ignored_var, "frame": row_frame, "orig_name": name}
        self._base_rows.append(row)

        def remove(r=row):
            r["frame"].destroy()
            self._base_rows.remove(r)

        ttk.Button(row_frame, text="✕", command=remove, width=3).pack(side="left")

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
                cfg = mgr._load_raw()
                for old_key, new_key in valid_renames:
                    old_tmpl = "{" + old_key + "}"
                    new_tmpl = "{" + new_key + "}"
                    for raw in cfg.get("symlinks", []):
                        raw["link"]   = raw.get("link", "").replace(old_tmpl, new_tmpl)
                        raw["target"] = raw.get("target", "").replace(old_tmpl, new_tmpl)
                        for k, v in raw.get("target_override", {}).items():
                            raw["target_override"][k] = v.replace(old_tmpl, new_tmpl)
                    for mc_data in cfg.get("local_data", {}).values():
                        for raw in mc_data.get("symlinks", []):
                            raw["link"]   = raw.get("link", "").replace(old_tmpl, new_tmpl)
                            raw["target"] = raw.get("target", "").replace(old_tmpl, new_tmpl)
                            for k, v in raw.get("target_override", {}).items():
                                raw["target_override"][k] = v.replace(old_tmpl, new_tmpl)
                        for raw in mc_data.get("scanned", []):
                            raw["link"]   = raw.get("link", "").replace(old_tmpl, new_tmpl)
                            raw["target"] = raw.get("target", "").replace(old_tmpl, new_tmpl)
                # Update ALL machines' entries so no machine is left with the old key name
                for mc in cfg.get("machines", {}).values():
                    for old_key, new_key in valid_renames:
                        if old_key in mc:
                            mc[new_key] = mc.pop(old_key)
                mgr._save_raw(cfg)

        required = mgr.get_required_bases()
        missing  = required - set(bases.keys())
        if missing:
            from dialogs import ask_missing_bases
            choice = ask_missing_bases(self._win, missing)
            if choice == "cancel":
                return
            elif choice == "local":
                mgr.demote_base_entries_local(missing)
                bases.update({k: None for k in missing})   # mark all as 不使用
            else:  # global
                mgr.demote_base_entries(missing)
                required = mgr.get_required_bases()
                still_missing = required - set(bases.keys())
                if still_missing:
                    messagebox.showwarning(
                        "Base 未完整配置",
                        f"以下 base 仍未配置：\n\n"
                        + "\n".join(f"  {{{k}}}" for k in sorted(still_missing))
                        + "\n\n请补充配置或勾选「不使用」后再保存。",
                        parent=self._win,
                    )
                    return

        mgr.register_machine(bases)
        _, n_promoted = mgr.normalize_entries()
        if n_promoted:
            from tkinter import messagebox as _mb
            _mb.showinfo(
                "条目已提升为全局",
                f"{n_promoted} 个条目现已对所有计算机可见。",
                parent=self._win,
            )

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
