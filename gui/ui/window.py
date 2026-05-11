"""Status window: Treeview table of all symlink entries + per-row edit / new-entry dialogs."""

import logging
import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

logger = logging.getLogger(__name__)

from core.symlink_manager import (ERR_LINK_NONEMPTY, LinkEntry, Status,  # noqa: F401
                                   create_entry, delete_entry, edit_entry,
                                   get_scanned, get_machine_config,
                                   get_other_machines_local_entries)
from ui.utils import center_window, center_on_parent, iid_escape, iid_unescape, shorten_path

_STATUS_LABEL = {
    Status.OK:      "✅ 正常",
    Status.BROKEN:  "❌ 断链",
    Status.PENDING: "➕ 待建",
    Status.MISSING: "⚠️ 缺失",
}

_JSON_PATH = Path(__file__).parent.parent.parent / "symlinks.json"



class StatusWindow:
    def __init__(self, root: tk.Tk, on_sync: Callable,
                 on_refresh_needed: Callable, on_open_settings: Callable,
                 on_entry_saved: Callable[[str], None] | None = None,
                 on_relink: Callable[[str], None] | None = None,
                 on_open_scan: Callable | None = None,
                 on_manage_bases: Callable | None = None):
        self._root = root
        self._on_sync = on_sync
        self._on_refresh_needed = on_refresh_needed
        self._on_open_settings = on_open_settings
        self._on_entry_saved = on_entry_saved
        self._on_relink = on_relink
        self._on_open_scan = on_open_scan
        self._on_manage_bases = on_manage_bases
        self._confirmed_empty: set[str] = set()
        self._win: tk.Toplevel | None = None
        self._tree: ttk.Treeview | None = None
        self._entries: list[LinkEntry] = []
        self._close_handler: Callable = self.hide

    # ── Public API ───────────────────────────────────────────────────────────

    def set_confirmed_empty(self, ids: set[str]):
        self._confirmed_empty = ids
        if self._win and self._win.winfo_exists():
            self._populate(self._entries)

    def set_close_handler(self, handler: Callable):
        self._close_handler = handler
        if self._win and self._win.winfo_exists():
            self._win.protocol("WM_DELETE_WINDOW", self._close_handler)

    def show(self, entries: list[LinkEntry]):
        self._entries = entries
        if self._win and self._win.winfo_exists():
            self._populate(entries)
            self._win.deiconify()
            self._win.lift()
            self._win.focus_force()
            return
        self._build(entries)

    def refresh(self, entries: list[LinkEntry]):
        self._entries = entries
        if self._win and self._win.winfo_exists():
            self._populate(entries)

    def hide(self):
        if self._win:
            self._win.withdraw()

    # ── Build ────────────────────────────────────────────────────────────────

    def _build(self, entries: list[LinkEntry]):
        win = tk.Toplevel(self._root)
        self._win = win
        win.title("Sym-Link 状态")
        win.resizable(True, True)
        try:
            from ui.icons import app_icon
            from PIL import ImageTk
            _ico = ImageTk.PhotoImage(app_icon(256), master=win)
            win.iconphoto(False, _ico)
            win._icon_ref = _ico
        except Exception:
            pass
        win.protocol("WM_DELETE_WINDOW", self._close_handler)
        win.minsize(900, 600)
        win.geometry("1800x900")

        style = ttk.Style(win)
        style.configure("Treeview", rowheight=40)
        style.configure("Treeview.Heading", padding=(4, 6))

        cols = ("状态", "名称", "描述", "链接路径", "目标路径")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=16,
                            selectmode="extended")
        self._tree = tree

        for col in cols:
            anchor = "center" if col == "状态" else "w"
            tree.heading(col, text=col, anchor=anchor)

        tree.column("状态",   width=160, anchor="center", stretch=True)
        tree.column("名称",   width=170, anchor="w",      stretch=False)
        tree.column("描述",   width=200, anchor="w")
        tree.column("链接路径", width=320, anchor="w")
        tree.column("目标路径", width=320, anchor="w")

        sb = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)

        tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        sb.grid(row=0,  column=1, sticky="ns",   pady=8, padx=(0, 4))

        tree.tag_configure("broken",         background="#FFCDD2")  # red
        tree.tag_configure("missing",        background="#FFEBEE")  # red (lighter)
        tree.tag_configure("pending",        background="#FFF9C4")  # yellow
        tree.tag_configure("ok_empty",       background="#FFF3E0")  # amber
        tree.tag_configure("unmanaged_scan",  background="#FFF9C4")
        tree.tag_configure("separator",       background="#ECEFF1", foreground="#90A4AE")
        tree.tag_configure("offline_pending", background="#F3E5F5")
        tree.tag_configure("offline_done",    background="#E8EAF6")

        ttk.Label(win, text="双击任意行可编辑", foreground="gray").grid(
            row=1, column=0, columnspan=2, sticky="w", padx=10)

        btn_frame = ttk.Frame(win)
        btn_frame.grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=(4, 8))

        ttk.Button(btn_frame, text="新建",            command=self._open_new_dialog).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="立即同步",         command=self._on_sync).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="扫描链接",         command=self._open_scan).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="管理同步目录",      command=self._open_manage_bases).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="打开配置文件",      command=self._open_json).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="设置...",          command=self._on_open_settings).pack(side="left", padx=4)

        win.columnconfigure(0, weight=1)
        win.rowconfigure(0, weight=1)

        tree.bind("<Double-1>",  self._on_double_click)
        tree.bind("<Button-3>",  self._on_right_click)
        tree.bind("<Delete>",    self._on_delete_key)

        self._populate(entries)
        center_window(win)

    def _populate(self, entries: list[LinkEntry]):
        tree = self._tree
        for row in tree.get_children():
            tree.delete(row)
        od = _get_bases()

        # ── Managed entries ───────────────────────────────────────────────────
        global_entries = [e for e in entries if not e.machine_specific]
        local_entries  = [e for e in entries if e.machine_specific]

        def _insert_entry(e: LinkEntry):
            if e.status == Status.OK and e.target_empty and e.id not in self._confirmed_empty:
                tag   = "ok_empty"
                label = "⚠️ 空目标"
            else:
                tag   = e.status.name.lower()
                label = _STATUS_LABEL[e.status]
            tree.insert("", "end", iid=e.id,
                        values=(label, e.id, e.description,
                                _shorten(str(e.link),   od),
                                _shorten(str(e.target), od)),
                        tags=(tag,))

        for e in global_entries:
            _insert_entry(e)

        if local_entries:
            tree.insert("", "end", iid="__local_sep__",
                        values=("── 本机管理 ──", "", "", "", ""),
                        tags=("separator",))
            for e in local_entries:
                _insert_entry(e)

        # ── Unmanaged scanned entries ─────────────────────────────────────────
        managed_links = {str(e.link).lower() for e in entries}
        scanned = [s for s in get_scanned()
                   if not s.get("ignored")
                   and _resolve_link(s["link"], od).lower() not in managed_links]

        if scanned:
            tree.insert("", "end", iid="__scan_sep__",
                        values=("── 未管理的扫描结果 ──", "", "", "", ""),
                        tags=("separator",))
            for s in scanned:
                # Escape Tcl metacharacters { } which cause TclError in some tk versions
                iid = "scan::" + iid_escape(s["link"]) + "||" + iid_escape(s["target"])
                tree.insert("", "end", iid=iid,
                            values=("🔍 未管理", s["link"].split("/")[-1],
                                    "",
                                    _shorten(s["link"], od),
                                    _shorten(s["target"], od)),
                            tags=("unmanaged_scan",))

        # ── Offline-configured entries (other machines' local symlinks) ──────────
        my_ids  = {e.id for e in entries}
        other   = get_other_machines_local_entries()
        pending = {eid: ml for eid, ml in other.items() if eid not in my_ids}
        if pending:
            tree.insert("", "end", iid="__offline_sep__",
                        values=("── 离线配置 ──", "", "", "", ""),
                        tags=("separator",))
            for eid, machine_entries in sorted(pending.items()):
                machines_str = "、".join(e["_machine"] for e in machine_entries)
                tree.insert("", "end", iid=f"offline::{eid}",
                            values=("□ 待配置", eid, machines_str, "", ""),
                            tags=("offline_pending",))

    # ── Actions ──────────────────────────────────────────────────────────────

    def _open_scan(self):
        if self._on_open_scan:
            self._on_open_scan()

    def _open_manage_bases(self):
        if self._on_manage_bases:
            self._on_manage_bases()

    def _open_json(self):
        os.startfile(str(_JSON_PATH))

    def _on_double_click(self, event):
        item = self._tree.identify_row(event.y)
        if not item or item in ("__scan_sep__", "__offline_sep__", "__local_sep__"):
            return
        if item.startswith("scan::"):
            self._open_import_dialog(item)
            return
        if item.startswith("offline::"):
            self._open_offline_entry_dialog(item.removeprefix("offline::"))
            return
        entry = next((e for e in self._entries if e.id == item), None)
        if entry:
            self._open_edit_dialog(entry)

    def _on_right_click(self, event):
        item = self._tree.identify_row(event.y)
        if not item or item in ("__scan_sep__", "__offline_sep__", "__local_sep__"):
            return

        # If right-clicked outside current selection, move selection to this item
        if item not in self._tree.selection():
            self._tree.selection_set(item)

        # Collect all selected managed entries (skip separators, scan::, offline::)
        _sep = {"__scan_sep__", "__offline_sep__", "__local_sep__"}
        selected_ids = [
            iid for iid in self._tree.selection()
            if iid not in _sep and not iid.startswith("scan::") and not iid.startswith("offline::")
        ]
        selected_entries = [e for e in self._entries if e.id in set(selected_ids)]

        # Multi-select: show bulk-delete menu (only when ≥2 managed entries selected)
        if len(selected_entries) >= 2:
            menu = tk.Menu(self._win, tearoff=0)
            n = len(selected_entries)
            menu.add_command(
                label=f"删除 {n} 项的管理记录",
                command=lambda: self._delete_entries(selected_entries, remove_junction=False))
            menu.add_command(
                label=f"删除 {n} 项的管理记录和目录",
                command=lambda: self._delete_entries(selected_entries, remove_junction=True))
            menu.tk_popup(event.x_root, event.y_root)
            return

        # Single-item menus
        if item.startswith("scan::"):
            self._scan_context_menu(event, item)
            return
        if item.startswith("offline::"):
            entry_id = item.removeprefix("offline::")
            menu = tk.Menu(self._win, tearoff=0)
            menu.add_command(label="配置到本机...",
                             command=lambda: self._open_offline_entry_dialog(entry_id))
            menu.tk_popup(event.x_root, event.y_root)
            return
        entry = next((e for e in self._entries if e.id == item), None)
        if not entry:
            return
        link_ok   = os.path.lexists(entry.link)
        target_ok = entry.target.exists()

        menu = tk.Menu(self._win, tearoff=0)
        menu.add_command(label="查看链接",
                         state="normal" if link_ok else "disabled",
                         command=lambda: self._open_in_explorer(entry.link))
        menu.add_command(label="查看目标",
                         state="normal" if target_ok else "disabled",
                         command=lambda: self._open_in_explorer(entry.target))
        menu.add_separator()
        menu.add_command(label="（重新）链接 / 确认",
                         command=lambda: self._relink_entry(entry))
        menu.add_separator()
        menu.add_command(label="删除管理",
                         command=lambda: self._delete_entry(entry, remove_junction=False))
        menu.add_command(label="删除管理和目录",
                         command=lambda: self._delete_entry(entry, remove_junction=True))
        menu.tk_popup(event.x_root, event.y_root)

    def _scan_context_menu(self, event, iid):
        _, _, rest = iid.partition("::")
        link_esc, _, target_esc = rest.partition("||")
        link_str   = iid_unescape(link_esc)
        target_str = iid_unescape(target_esc)
        menu = tk.Menu(self._win, tearoff=0)
        menu.add_command(label="导入管理",
                         command=lambda: self._open_import_dialog(iid))
        menu.add_command(label="不管理",
                         command=lambda: self._do_ignore_scan(link_str, target_str, iid))
        menu.tk_popup(event.x_root, event.y_root)

    def _open_offline_entry_dialog(self, entry_id: str):
        """Show per-machine configs for an offline entry and allow configuring locally."""
        from core.symlink_manager import _resolve, get_other_machines_local_entries
        import tkinter.ttk as _ttk

        other = get_other_machines_local_entries()
        machine_entries = other.get(entry_id, [])
        if not machine_entries:
            return

        dlg = tk.Toplevel(self._win)
        dlg.title(f"{entry_id} — 各计算机配置参考")
        dlg.resizable(True, False)
        dlg.transient(self._win)
        dlg.grab_set()

        od = _get_bases()

        # ── Reference table ───────────────────────────────────────────────────
        ref = _ttk.LabelFrame(dlg, text="其他计算机配置", padding=8)
        ref.pack(fill="x", padx=12, pady=(12, 4))

        ref_tree = _ttk.Treeview(ref, columns=("机器", "链接", "目标"),
                                  show="headings", height=len(machine_entries))
        ref_tree.heading("机器", text="计算机", anchor="w")
        ref_tree.heading("链接", text="链接路径", anchor="w")
        ref_tree.heading("目标", text="目标路径", anchor="w")
        ref_tree.column("机器", width=120, stretch=False)
        ref_tree.column("链接", width=280)
        ref_tree.column("目标", width=280)

        for me in machine_entries:
            ref_tree.insert("", "end", values=(
                me["_machine"],
                _shorten(me.get("link", ""), od),
                _shorten(me.get("target", ""), od),
            ))
        ref_tree.pack(fill="x", pady=(4, 0))

        # ── Pre-fill logic ─────────────────────────────────────────────────────
        bases = get_machine_config() or {}
        src   = machine_entries[0]

        def _try(template: str) -> str:
            resolved = str(_resolve(template, bases))
            return "" if "{" in resolved else resolved

        pre_link   = _try(src.get("link",   ""))
        pre_target = _try(src.get("target", ""))
        pre_desc   = src.get("description", "")

        # ── Config form ───────────────────────────────────────────────────────
        sep = _ttk.Separator(dlg, orient="horizontal")
        sep.pack(fill="x", padx=12, pady=6)

        _, id_var, desc_text, target_var, link_var = self._build_entry_form(
            dlg,
            id_val=entry_id,
            desc_val=pre_desc,
            target_val=pre_target,
            link_val=pre_link,
        )

        btn_row = _ttk.Frame(dlg, padding=(12, 0, 12, 12))
        btn_row.pack(fill="x")

        def confirm(force: bool = False):
            eid        = id_var.get().strip()
            desc       = desc_text.get("1.0", "end-1c").strip()
            target_path = Path(target_var.get().strip())
            link_path   = Path(link_var.get().strip())

            if not eid or not str(target_path).strip() or not str(link_path).strip():
                messagebox.showwarning("输入不完整", "名称、链接路径和目标路径均为必填。", parent=dlg)
                return

            if not force and link_path.exists() and not link_path.is_junction():
                try:
                    non_empty = any(link_path.iterdir())
                except OSError:
                    non_empty = False
                if non_empty:
                    if not messagebox.askyesno("路径已存在",
                                               f"链接路径已存在非空目录：\n{link_path}\n\n删除并建立链接？",
                                               icon="warning", parent=dlg):
                        return
                    confirm(force=True); return

            from core.symlink_manager import create_entry, ERR_LINK_NONEMPTY
            ok, err = create_entry(eid, desc, link_path, target_path, force_overwrite=force)
            if ok:
                dlg.destroy()
                self._on_refresh_needed()
            elif err == ERR_LINK_NONEMPTY:
                if messagebox.askyesno("目录非空", f"{link_path}\n已存在且不为空，确认清空并建立链接？",
                                       icon="warning", parent=dlg):
                    confirm(force=True)
            else:
                messagebox.showerror("配置失败", err, parent=dlg)

        _ttk.Button(btn_row, text="取消",     command=dlg.destroy, width=8).pack(side="right", padx=(6, 0))
        _ttk.Button(btn_row, text="配置到本机", command=confirm,    width=12).pack(side="right")

        dlg.update_idletasks()
        sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
        w, h   = dlg.winfo_width(), dlg.winfo_height()
        dlg.geometry(f"+{(sw-w)//2}+{(sh-h)//2}")

    def _open_import_dialog(self, iid):
        from core.symlink_manager import import_scanned_entry
        _, _, rest = iid.partition("::")
        link_str, _, target_str = rest.partition("||")
        link_str   = iid_unescape(link_str)
        target_str = iid_unescape(target_str)

        dlg = tk.Toplevel(self._win)
        dlg.title("导入为管理条目")
        dlg.resizable(False, False)
        dlg.transient(self._win)
        dlg.grab_set()

        import tkinter.ttk as ttk_dlg
        outer = ttk_dlg.Frame(dlg, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)

        bases = _get_bases()
        ttk_dlg.Label(outer, text="链接路径：", anchor="e").grid(row=0, column=0, sticky="e", padx=(0,8))
        ttk_dlg.Label(outer, text=_shorten(link_str, bases), anchor="w").grid(row=0, column=1, sticky="w")
        ttk_dlg.Label(outer, text="目标路径：", anchor="e").grid(row=1, column=0, sticky="e", padx=(0,8))
        ttk_dlg.Label(outer, text=_shorten(target_str, bases), anchor="w").grid(row=1, column=1, sticky="w")

        ttk_dlg.Label(outer, text="名称（必填）：", anchor="e").grid(row=2, column=0, sticky="e", padx=(0,8), pady=(12,4))
        id_var = tk.StringVar()
        ttk_dlg.Entry(outer, textvariable=id_var, width=36).grid(row=2, column=1, sticky="ew", pady=(12,4))

        ttk_dlg.Label(outer, text="描述（可选）：", anchor="e").grid(row=3, column=0, sticky="e", padx=(0,8), pady=4)
        desc_var = tk.StringVar()
        ttk_dlg.Entry(outer, textvariable=desc_var, width=36).grid(row=3, column=1, sticky="ew", pady=4)

        btn_row = ttk_dlg.Frame(outer)
        btn_row.grid(row=4, column=0, columnspan=2, sticky="e", pady=(12,0))

        def confirm():
            eid = id_var.get().strip()
            if not eid:
                messagebox.showwarning("名称不能为空", "请填写名称。", parent=dlg)
                return
            ok, err = import_scanned_entry(link_str, target_str, eid, desc_var.get().strip())
            if ok:
                dlg.destroy()
                self._on_refresh_needed()
            else:
                messagebox.showerror("导入失败", err, parent=dlg)

        ttk_dlg.Button(btn_row, text="取消", command=dlg.destroy, width=8).pack(side="right", padx=(6,0))
        ttk_dlg.Button(btn_row, text="导入", command=confirm, width=8).pack(side="right")

    def _do_ignore_scan(self, link_str, target_str, iid):
        from core.symlink_manager import ignore_scanned_entry
        ignore_scanned_entry(link_str, target_str)
        self._on_refresh_needed()

    def _relink_entry(self, entry):
        if self._on_relink:
            self._on_relink(entry.id)

    def _open_in_explorer(self, path):
        import subprocess
        target = path if path.exists() else path.parent
        subprocess.run(f'explorer /select,"{target}"', shell=True)

    def _delete_entry(self, entry, remove_junction: bool = True):
        if remove_junction:
            msg = f"确认删除「{entry.id}」的管理记录，并同时删除对应的 Junction 目录？"
        else:
            msg = f"确认删除「{entry.id}」的管理记录？\n\nJunction 目录本身不会被删除。"
        if not messagebox.askyesno("确认删除", msg, icon="warning", parent=self._win):
            return
        ok, err = delete_entry(entry.id, remove_junction=remove_junction)
        if ok:
            self._on_refresh_needed()
        else:
            messagebox.showerror("删除失败", err, parent=self._win)

    def _delete_entries(self, entries: list, remove_junction: bool = True):
        names = "\n".join(f"  • {e.id}" for e in entries)
        if remove_junction:
            msg = f"确认删除以下 {len(entries)} 项的管理记录，并同时删除对应的 Junction 目录？\n\n{names}"
        else:
            msg = f"确认删除以下 {len(entries)} 项的管理记录？（Junction 目录不会被删除）\n\n{names}"
        if not messagebox.askyesno("确认批量删除", msg, icon="warning", parent=self._win):
            return
        errors = []
        for e in entries:
            ok, err = delete_entry(e.id, remove_junction=remove_junction)
            if not ok:
                errors.append(f"{e.id}: {err}")
        if errors:
            messagebox.showerror("部分删除失败", "\n".join(errors), parent=self._win)
        self._on_refresh_needed()

    def _on_delete_key(self, _event):
        _sep = {"__scan_sep__", "__offline_sep__", "__local_sep__"}
        selected_entries = [
            e for e in self._entries
            if e.id in self._tree.selection() and e.id not in _sep
        ]
        if not selected_entries:
            return
        if len(selected_entries) == 1:
            self._delete_entry(selected_entries[0], remove_junction=False)
        else:
            self._delete_entries(selected_entries, remove_junction=False)

    # ── Shared dialog builder ─────────────────────────────────────────────────

    def _build_entry_form(self, dlg: tk.Toplevel,
                          id_val="", desc_val="", target_val="", link_val="",
                          id_readonly=False):
        """Build the shared form for edit and new-entry dialogs.
        Returns (outer, id_var, desc_text, target_var, link_var)."""
        outer = ttk.Frame(dlg, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)

        def lbl(text, row, top=False):
            ttk.Label(outer, text=text, anchor="e", width=8).grid(
                row=row, column=0, sticky="ne" if top else "e",
                padx=(0, 8), pady=(8, 0) if top else 4)

        # 编号
        lbl("编号", 0)
        id_var = tk.StringVar(value=id_val)
        id_entry = ttk.Entry(outer, textvariable=id_var, width=40,
                             state="disabled" if id_readonly else "normal")
        id_entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=4)

        # 描述（多行）
        lbl("描述", 1, top=True)
        desc_text = tk.Text(outer, width=40, height=3, wrap="word",
                            relief="solid", borderwidth=1)
        desc_text.insert("1.0", desc_val)
        desc_text.grid(row=1, column=1, columnspan=2, sticky="ew", pady=4)

        # 目标路径
        lbl("目标路径", 2)
        target_var = tk.StringVar(value=target_val)
        ttk.Entry(outer, textvariable=target_var, width=34).grid(
            row=2, column=1, sticky="ew", pady=4)

        def browse_target():
            init = str(Path(target_var.get()).parent) if target_var.get() else "/"
            p = filedialog.askdirectory(parent=dlg, title="选择目标目录", initialdir=init)
            if p:
                target_var.set(p)

        ttk.Button(outer, text="浏览...", command=browse_target).grid(
            row=2, column=2, padx=(6, 0), pady=4)

        # 链接路径
        lbl("链接路径", 3)
        link_var = tk.StringVar(value=link_val)
        ttk.Entry(outer, textvariable=link_var, width=34).grid(
            row=3, column=1, sticky="ew", pady=4)

        def browse_link():
            cur = link_var.get()
            init = str(Path(cur).parent) if cur and Path(cur).parent.exists() else "/"
            p = filedialog.askdirectory(parent=dlg, title="选择链接路径（选中目录即为链接位置）", initialdir=init)
            if p:
                link_var.set(p)

        ttk.Button(outer, text="浏览...", command=browse_link).grid(
            row=3, column=2, padx=(6, 0), pady=4)

        return outer, id_var, desc_text, target_var, link_var

    # ── Edit dialog ───────────────────────────────────────────────────────────

    def _open_edit_dialog(self, entry: LinkEntry):
        dlg = tk.Toplevel(self._win)
        dlg.title(f"编辑：{entry.id}")
        dlg.resizable(True, False)
        dlg.transient(self._win)
        dlg.grab_set()

        outer, id_var, desc_text, target_var, link_var = self._build_entry_form(
            dlg,
            id_val=entry.id,
            desc_val=entry.description,
            target_val=str(entry.target),
            link_val=str(entry.link),
        )

        btn_row = ttk.Frame(outer)
        btn_row.grid(row=4, column=0, columnspan=3, sticky="e", pady=(12, 0))

        def confirm():
            new_id     = id_var.get().strip()
            new_desc   = desc_text.get("1.0", "end-1c").strip()
            new_target = Path(target_var.get().strip())
            new_link   = Path(link_var.get().strip())

            logger.info("edit confirm: id=%s link=%s→%s target=%s→%s",
                        entry.id, entry.link, new_link, entry.target, new_target)

            if not new_id:
                messagebox.showwarning("无效输入", "编号不能为空", parent=dlg)
                return

            # Check if link path already exists as a non-empty real directory
            force = False
            is_junc = new_link.is_junction()
            lexists = new_link.exists() or os.path.lexists(new_link)
            logger.info("new link path lexists=%s is_junction=%s", lexists, is_junc)
            if lexists and not is_junc:
                try:
                    non_empty = any(new_link.iterdir())
                except OSError:
                    non_empty = False
                if non_empty:
                    if not messagebox.askyesno(
                        "路径已存在",
                        f"链接路径已存在非空目录：\n{new_link}\n\n删除其中所有内容并建立链接？",
                        icon="warning", parent=dlg,
                    ):
                        return
                    force = True

            # Warn if entry would move from global to machine-specific
            from core.symlink_manager import _to_json_path, _is_global
            bases = _get_bases()
            new_link_json   = _to_json_path(new_link,   bases) if bases else str(new_link)
            new_target_json = _to_json_path(new_target, bases) if bases else str(new_target)
            from core.symlink_manager import _load_raw
            was_global = entry.id in {
                r["id"] for r in _load_raw().get("symlinks", [])
            }
            will_be_local = not _is_global(new_link_json, new_target_json)
            if was_global and will_be_local:
                if not messagebox.askyesno(
                    "变为本机独有配置",
                    "修改后该条目将包含本机特定路径，\n"
                    "变为本机独有配置，其他计算机将不再看到它。\n\n确认继续？",
                    icon="warning", parent=dlg,
                ):
                    return

            logger.info("calling edit_entry force=%s", force)
            ok = edit_entry(
                entry.id,
                new_id=new_id             if new_id     != entry.id          else None,
                new_description=new_desc  if new_desc   != entry.description else None,
                new_target=new_target     if new_target != entry.target      else None,
                new_link=new_link         if new_link   != entry.link        else None,
                force_overwrite=force,
            )
            logger.info("edit_entry returned ok=%s", ok)
            if ok:
                dlg.destroy()
                if self._on_entry_saved:
                    self._on_entry_saved(new_id)
                self._on_refresh_needed()
            else:
                messagebox.showerror("保存失败", "保存失败，请检查路径", parent=dlg)

        ttk.Button(btn_row, text="取消", command=dlg.destroy, width=8).pack(side="right", padx=(6, 0))
        ttk.Button(btn_row, text="确定", command=confirm,     width=8).pack(side="right")
        center_on_parent(dlg, self._win)

    # ── New entry dialog ──────────────────────────────────────────────────────

    def _open_new_dialog(self):
        dlg = tk.Toplevel(self._win)
        dlg.title("新建链接")
        dlg.resizable(True, False)
        dlg.transient(self._win)
        dlg.grab_set()

        outer, id_var, desc_text, target_var, link_var = self._build_entry_form(dlg)

        btn_row = ttk.Frame(outer)
        btn_row.grid(row=4, column=0, columnspan=3, sticky="e", pady=(12, 0))

        def do_create(force: bool = False):
            entry_id = id_var.get().strip()
            desc     = desc_text.get("1.0", "end-1c").strip()
            target   = link_var_path(target_var)
            link     = link_var_path(link_var)

            if not entry_id:
                messagebox.showwarning("无效输入", "编号不能为空", parent=dlg)
                return
            if not str(target).strip() or not str(link).strip():
                messagebox.showwarning("无效输入", "目标路径和链接路径不能为空", parent=dlg)
                return

            ok, err = create_entry(entry_id, desc, link, target, force_overwrite=force)
            if ok:
                dlg.destroy()
                self._on_refresh_needed()
            elif err == ERR_LINK_NONEMPTY:
                if messagebox.askyesno(
                    "目录非空",
                    f"链接路径\n{link_var.get()}\n已存在且不为空。\n\n继续将删除该目录中的所有内容，确认？",
                    icon="warning",
                    parent=dlg,
                ):
                    do_create(force=True)
            else:
                messagebox.showerror("创建失败", err, parent=dlg)

        ttk.Button(btn_row, text="取消", command=dlg.destroy, width=8).pack(side="right", padx=(6, 0))
        ttk.Button(btn_row, text="创建", command=do_create,   width=8).pack(side="right")
        center_on_parent(dlg, self._win)


# ── Helpers ───────────────────────────────────────────────────────────────────

def link_var_path(var: tk.StringVar) -> Path:
    return Path(var.get().strip())


def _get_bases() -> dict[str, str]:
    try:
        return get_machine_config() or {}
    except Exception:
        return {}


def _shorten(path: str, bases: dict[str, str]) -> str:
    return shorten_path(path, bases or {})


def _resolve_link(path_str: str, bases: dict[str, str]) -> str:
    """Resolve template path for comparison (normalised to backslash)."""
    s = path_str
    for key, val in (bases or {}).items():
        s = s.replace("{" + key + "}", val)
    return s.replace("/", "\\")
