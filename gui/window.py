"""Status window: Treeview table of all symlink entries + per-row edit / new-entry dialogs."""

import logging
import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

logger = logging.getLogger(__name__)

from symlink_manager import ERR_LINK_NONEMPTY, LinkEntry, Status, create_entry, delete_entry, edit_entry  # noqa: F401

_STATUS_LABEL = {
    Status.OK:      "✅ 正常",
    Status.BROKEN:  "❌ 断链",
    Status.PENDING: "➕ 待建",
    Status.MISSING: "⚠️ 缺失",
}

_JSON_PATH = Path(__file__).parent.parent / "symlinks.json"


def _center(win: tk.Toplevel | tk.Tk):
    """Move window to screen center without changing its current size."""
    win.update()
    w, h = win.winfo_width(), win.winfo_height()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"+{max(0,(sw-w)//2)}+{max(0,(sh-h)//2)}")


def _center_on(dlg: tk.Toplevel, parent: tk.Toplevel):
    dlg.update()
    pw, ph = parent.winfo_width(), parent.winfo_height()
    px, py = parent.winfo_rootx(), parent.winfo_rooty()
    dw, dh = dlg.winfo_width(), dlg.winfo_height()
    dlg.geometry(f"+{px+(pw-dw)//2}+{py+(ph-dh)//2}")


class StatusWindow:
    def __init__(self, root: tk.Tk, on_sync: Callable,
                 on_refresh_needed: Callable, on_open_settings: Callable,
                 on_entry_saved: Callable[[str], None] | None = None,
                 on_relink: Callable[[str], None] | None = None):
        self._root = root
        self._on_sync = on_sync
        self._on_refresh_needed = on_refresh_needed
        self._on_open_settings = on_open_settings
        self._on_entry_saved = on_entry_saved
        self._on_relink = on_relink
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
            from icons import app_icon
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
        tree = ttk.Treeview(win, columns=cols, show="headings", height=16)
        self._tree = tree

        for col in cols:
            anchor = "center" if col == "状态" else "w"
            tree.heading(col, text=col, anchor=anchor)

        tree.column("状态",   width=100, anchor="center", stretch=False)
        tree.column("名称",   width=170, anchor="w",      stretch=False)
        tree.column("描述",   width=200, anchor="w")
        tree.column("链接路径", width=320, anchor="w")
        tree.column("目标路径", width=320, anchor="w")

        sb = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)

        tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        sb.grid(row=0,  column=1, sticky="ns",   pady=8, padx=(0, 4))

        tree.tag_configure("broken",       background="#FFEBEE")
        tree.tag_configure("pending",      background="#E8F5E9")
        tree.tag_configure("missing",      background="#FFFDE7")
        tree.tag_configure("ok_empty",     background="#FFF3E0")

        ttk.Label(win, text="双击任意行可编辑", foreground="gray").grid(
            row=1, column=0, columnspan=2, sticky="w", padx=10)

        btn_frame = ttk.Frame(win)
        btn_frame.grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=(4, 8))

        ttk.Button(btn_frame, text="新建",            command=self._open_new_dialog).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="立即同步",         command=self._on_sync).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="打开配置文件",      command=self._open_json).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="设置...",          command=self._on_open_settings).pack(side="left", padx=4)

        win.columnconfigure(0, weight=1)
        win.rowconfigure(0, weight=1)

        tree.bind("<Double-1>", self._on_double_click)
        tree.bind("<Button-3>", self._on_right_click)

        self._populate(entries)
        _center(win)

    def _populate(self, entries: list[LinkEntry]):
        tree = self._tree
        for row in tree.get_children():
            tree.delete(row)
        od = _onedrive_root()
        for e in entries:
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

    # ── Actions ──────────────────────────────────────────────────────────────

    def _open_json(self):
        os.startfile(str(_JSON_PATH))

    def _on_double_click(self, event):
        item = self._tree.identify_row(event.y)
        if not item:
            return
        entry = next((e for e in self._entries if e.id == item), None)
        if entry:
            self._open_edit_dialog(entry)

    def _on_right_click(self, event):
        item = self._tree.identify_row(event.y)
        if not item:
            return
        self._tree.selection_set(item)
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

    def _relink_entry(self, entry):
        if self._on_relink:
            self._on_relink(entry.id)

    def _open_in_explorer(self, path):
        import subprocess
        target = path if path.exists() else path.parent
        subprocess.run(f'explorer /select,"{path}"', shell=True)

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

    # ── Shared dialog builder ─────────────────────────────────────────────────

    def _build_entry_form(self, dlg: tk.Toplevel,
                          id_val="", desc_val="", target_val="", link_val="",
                          id_readonly=False):
        """Build the shared form for edit and new-entry dialogs.
        Returns (id_var, desc_text, target_var, link_var)."""
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
        _center_on(dlg, self._win)

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

        def confirm():
            do_create()

        ttk.Button(btn_row, text="取消", command=dlg.destroy, width=8).pack(side="right", padx=(6, 0))
        ttk.Button(btn_row, text="创建", command=confirm,     width=8).pack(side="right")
        _center_on(dlg, self._win)


# ── Helpers ───────────────────────────────────────────────────────────────────

def link_var_path(var: tk.StringVar) -> Path:
    return Path(var.get().strip())


def _onedrive_root() -> str:
    try:
        from symlink_manager import get_onedrive
        od = get_onedrive()
        return od.replace("/", "\\") if od else ""
    except Exception:
        return ""


def _shorten(path: str, onedrive: str) -> str:
    if onedrive and path.startswith(onedrive):
        return "…" + path[len(onedrive):]
    return path
