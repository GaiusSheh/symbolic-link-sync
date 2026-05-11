"""Shared GUI dialog helpers."""

import tkinter as tk
from tkinter import ttk


def ask_missing_bases(parent: tk.Misc, missing: set[str]) -> str:
    """Show a three-choice dialog for unconfigured base keys.

    Returns: 'local' | 'global' | 'cancel'
      local  - copy entries to this machine's local_data (absolute paths), keep global intact;
               bases that this machine never had are simply marked as null (silently hidden)
      global - move entries out of global into every machine's local_data (absolute paths)
      cancel - do nothing
    """
    result = tk.StringVar(value="cancel")

    dlg = tk.Toplevel(parent)
    dlg.title("Base 未完整配置")
    dlg.resizable(False, False)
    dlg.grab_set()
    dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)

    outer = ttk.Frame(dlg, padding=20)
    outer.pack(fill="both", expand=True)

    keys_text = "\n".join(f"  {{{k}}}" for k in sorted(missing))
    msg = (
        f"以下 base 在现有配置中被使用，但未在本机配置路径：\n\n"
        f"{keys_text}\n\n"
        f"请选择处理方式："
    )
    ttk.Label(outer, text=msg, justify="left").pack(anchor="w", pady=(0, 14))

    sub_frame = ttk.Frame(outer)
    sub_frame.pack(anchor="w", pady=(0, 14))
    ttk.Label(sub_frame, text="在本机降级并忽略：", font=("Segoe UI", 9, "bold")).grid(
        row=0, column=0, sticky="nw", padx=(0, 8))
    ttk.Label(sub_frame,
              text="若本机曾配置此 base，相关条目展开为绝对路径写入本机管理；\n"
                   "其他机器的全局条目保持不变。\n"
                   "若本机从未配置此 base，直接标记为不使用（条目静默隐藏）。",
              justify="left", foreground="gray").grid(row=0, column=1, sticky="w")

    ttk.Label(sub_frame, text="全局降级（所有机器）：", font=("Segoe UI", 9, "bold")).grid(
        row=1, column=0, sticky="nw", padx=(0, 8), pady=(8, 0))
    ttk.Label(sub_frame,
              text="相关条目从全局移出，每台有此 base 的机器各自得到\n一份绝对路径的本地条目。",
              justify="left", foreground="gray").grid(row=1, column=1, sticky="w", pady=(8, 0))

    def choose(v: str) -> None:
        result.set(v)
        dlg.destroy()

    btn_frame = ttk.Frame(outer)
    btn_frame.pack(fill="x")
    ttk.Button(btn_frame, text="在本机降级并忽略",
               command=lambda: choose("local"),  width=18).pack(side="left", padx=(0, 6))
    ttk.Button(btn_frame, text="全局降级（所有机器）",
               command=lambda: choose("global"), width=18).pack(side="left", padx=(0, 6))
    ttk.Button(btn_frame, text="取消",
               command=lambda: choose("cancel"), width=8).pack(side="left")

    dlg.update_idletasks()
    try:
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        w  = dlg.winfo_width()
        h  = dlg.winfo_height()
        dlg.geometry(f"+{px + (pw - w) // 2}+{py + (ph - h) // 2}")
    except Exception:
        pass

    parent.wait_window(dlg)
    return result.get()
