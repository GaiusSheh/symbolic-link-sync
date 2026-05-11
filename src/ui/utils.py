"""Shared UI utilities."""

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


def center_window(win: tk.Toplevel | tk.Tk) -> None:
    """Center a window on the screen."""
    win.update()
    w, h = win.winfo_width(), win.winfo_height()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2)}")


def center_on_parent(dlg: tk.Toplevel, parent: tk.Toplevel) -> None:
    """Center a dialog relative to its parent window."""
    dlg.update()
    pw, ph = parent.winfo_width(), parent.winfo_height()
    px, py = parent.winfo_rootx(), parent.winfo_rooty()
    dw, dh = dlg.winfo_width(), dlg.winfo_height()
    dlg.geometry(f"+{px + (pw - dw) // 2}+{py + (ph - dh) // 2}")


def iid_escape(s: str) -> str:
    """Escape { } in a path string for use as a Treeview iid."""
    return s.replace("{", "__LB__").replace("}", "__RB__")


def iid_unescape(s: str) -> str:
    """Reverse iid_escape: restore { } from __LB__/__RB__ placeholders."""
    return s.replace("__LB__", "{").replace("__RB__", "}")


def shorten_path(path: str, bases: dict[str, str]) -> str:
    """Resolve template tokens and abbreviate using the longest matching base.

    Returns {base_key}\\relative\\path, or the resolved absolute path if no base matches.
    """
    resolved = path
    for key, val in bases.items():
        resolved = resolved.replace("{" + key + "}", val)
    resolved = resolved.replace("/", "\\")
    best_key: str | None = None
    best_len = 0
    for key, val in bases.items():
        b = val.replace("/", "\\").rstrip("\\")
        if resolved.lower().startswith(b.lower()) and len(b) > best_len:
            best_key = key
            best_len = len(b)
    if best_key:
        return "{" + best_key + "}" + resolved[best_len:]
    return resolved


def build_base_row(
    parent_win: tk.Toplevel,
    rows_list: list,
    container: ttk.Frame,
    name: str = "",
    path: str = "",
    ignored: bool = False,
    ignored_label: str = "不使用",
    name_width: int = 12,
    path_width: int = 30,
    browse_text: str = "浏览",
) -> dict:
    """Build a base-path row widget (name / path / ignored / remove) and append to rows_list.

    Returns the row dict with keys: name_var, path_var, ignored_var, frame.
    """
    row_frame = ttk.Frame(container)
    row_frame.pack(fill="x", pady=2)

    name_var    = tk.StringVar(value=name)
    path_var    = tk.StringVar(value=path)
    ignored_var = tk.BooleanVar(value=ignored)

    ttk.Label(row_frame, text="名称：", width=6, anchor="e").pack(side="left")
    ttk.Entry(row_frame, textvariable=name_var, width=name_width).pack(side="left", padx=(0, 6))
    ttk.Label(row_frame, text="路径：", width=5, anchor="e").pack(side="left")
    path_entry = ttk.Entry(row_frame, textvariable=path_var, width=path_width)
    path_entry.pack(side="left", padx=(0, 4))

    def browse(pv=path_var):
        cur  = pv.get()
        init = cur if cur and Path(cur).is_dir() else str(Path.home())
        p = filedialog.askdirectory(parent=parent_win, title="选择同步目录根路径", initialdir=init)
        if p:
            pv.set(p)

    browse_btn = ttk.Button(row_frame, text=browse_text, command=browse, width=len(browse_text) + 2)
    browse_btn.pack(side="left", padx=(0, 4))

    def _toggle_ignored(*_):
        state = "disabled" if ignored_var.get() else "normal"
        path_entry.config(state=state)
        browse_btn.config(state=state)

    ignored_var.trace_add("write", _toggle_ignored)
    _toggle_ignored()

    ttk.Checkbutton(row_frame, text=ignored_label, variable=ignored_var).pack(side="left", padx=(0, 4))

    row = {"name_var": name_var, "path_var": path_var,
           "ignored_var": ignored_var, "frame": row_frame}
    rows_list.append(row)

    def remove(r=row):
        r["frame"].destroy()
        rows_list.remove(r)

    ttk.Button(row_frame, text="✕", command=remove, width=3).pack(side="left")
    return row


def apply_base_registration(parent_win: tk.Toplevel,
                             bases: dict,
                             not_used_label: str = "不使用") -> bool:
    """Handle missing-bases dialog, register_machine, normalize_entries.

    Mutates bases in place (adds missing keys as None if user chooses local).
    Returns True on success, False on cancel/validation error.
    """
    from core import symlink_manager as mgr
    from ui.dialogs import ask_missing_bases

    required = mgr.get_required_bases()
    missing  = required - set(bases.keys())
    if missing:
        choice = ask_missing_bases(parent_win, missing)
        if choice == "cancel":
            return False
        elif choice == "local":
            mgr.demote_base_entries_local(missing)
            bases.update({k: None for k in missing})
        else:  # global
            mgr.demote_base_entries(missing)
            still_missing = mgr.get_required_bases() - set(bases.keys())
            if still_missing:
                messagebox.showwarning(
                    "Base 未完整配置",
                    "以下 base 仍未配置：\n\n"
                    + "\n".join(f"  {{{k}}}" for k in sorted(still_missing))
                    + f"\n\n请补充配置或勾选「{not_used_label}」后再确认。",
                    parent=parent_win,
                )
                return False

    mgr.register_machine(bases)
    _, n_promoted = mgr.normalize_entries()
    if n_promoted:
        messagebox.showinfo(
            "条目已提升为全局",
            f"{n_promoted} 个条目现已对所有计算机可见。",
            parent=parent_win,
        )
    return True
