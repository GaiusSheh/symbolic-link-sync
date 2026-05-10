"""Scan window: find existing junctions under a directory and import them."""

import os
import queue
import threading
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable
import tkinter as tk

from core import symlink_manager as mgr
from core.symlink_manager import (
    _to_json_path, get_scanned,
    ignore_scanned_entry, import_scanned_entry, merge_scanned,
    get_machine_config, get_machine_config_full, register_machine, rebase,
)
from ui.utils import iid_escape, iid_unescape, shorten_path


def _truncate_path(path: str, display_depth: int) -> str:
    """Keep only the last `display_depth` components of a path (0 = no limit)."""
    if display_depth <= 0:
        return path
    parts = Path(path).parts
    if len(parts) <= display_depth:
        return path
    return "…/" + "/".join(parts[-display_depth:])


def _fmt_mtime(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _classify_dir(scan_path: Path, bases: dict[str, str]) -> tuple[str, list[str]]:
    """Return ("child", [key]) | ("parent", [key, ...]) | ("unrelated", []).

    child:    scan_path is inside an existing base (first match only)
    parent:   scan_path is a parent of one or more existing bases (all matches)
    unrelated: no hierarchical relationship with any base
    """
    scan_resolved = scan_path.resolve()
    parent_keys: list[str] = []
    for key, base_str in bases.items():
        base = Path(base_str).resolve()
        try:
            scan_resolved.relative_to(base)
            return "child", [key]        # scan_path is under this base
        except ValueError:
            pass
        try:
            base.relative_to(scan_resolved)
            parent_keys.append(key)      # scan_path is above this base
        except ValueError:
            pass
    if parent_keys:
        return "parent", parent_keys
    return "unrelated", []


class ScanWindow:
    def __init__(self, root: tk.Tk, on_done: Callable):
        self._root   = root
        self._on_done = on_done   # called after import/ignore to trigger refresh
        self._win: tk.Toplevel | None = None
        self._sq: queue.Queue = queue.Queue()
        self._stop_event = threading.Event()
        self._scanning   = False

    # ── Public API ────────────────────────────────────────────────────────────

    def show(self, managed_entries):
        """Open (or raise) the scan window. managed_entries: list[LinkEntry]."""
        self._managed_entries = managed_entries
        if self._win and self._win.winfo_exists():
            self._win.deiconify()
            self._win.lift()
            self._win.focus_force()
            self._refresh_treeview()
            return
        self._build()

    # ── Build ─────────────────────────────────────────────────────────────────

    def _build(self):
        win = tk.Toplevel(self._root)
        self._win = win
        win.title("扫描链接")
        win.resizable(True, True)
        win.minsize(900, 600)
        win.geometry("1200x700")
        win.protocol("WM_DELETE_WINDOW", self._on_close)

        style = ttk.Style(win)
        style.configure("Treeview", rowheight=36)

        # ── Top grid: 3 rows share the same right button column ──────────────
        BTN_W = 10   # uniform button width
        hdr = ttk.Frame(win, padding=(8, 8, 8, 2))
        hdr.pack(fill="x")
        hdr.columnconfigure(1, weight=1)

        # Row 0: directory
        ttk.Label(hdr, text="扫描目录：").grid(row=0, column=0, sticky="w", pady=2)
        self._dir_var = tk.StringVar()
        dir_entry = ttk.Entry(hdr, textvariable=self._dir_var)
        dir_entry.grid(row=0, column=1, sticky="ew", padx=(4, 8), pady=2)
        dir_entry.bind("<FocusOut>", self._on_dir_changed)
        dir_entry.bind("<Return>",   self._on_dir_changed)
        ttk.Button(hdr, text="浏览…", command=self._browse_dir,
                   width=BTN_W).grid(row=0, column=2, sticky="e", pady=2)

        # Row 1: spinboxes
        def spinbox(parent, label, var, lo, hi, default):
            ttk.Label(parent, text=label).pack(side="left", padx=(0, 2))
            sb = ttk.Spinbox(parent, from_=lo, to=hi, width=5, textvariable=var)
            sb.set(default); sb.pack(side="left", padx=(0, 12))

        self._depth_var   = tk.IntVar(value=0)
        self._disp_var    = tk.IntVar(value=3)
        self._maxpair_var = tk.IntVar(value=10)
        spin_row = ttk.Frame(hdr)
        spin_row.grid(row=1, column=0, columnspan=2, sticky="w", pady=2)
        spinbox(spin_row, "扫描深度（0=无限）：", self._depth_var,   0, 50,  0)
        spinbox(spin_row, "显示深度（0=无限）：", self._disp_var,    0, 20,  3)
        spinbox(spin_row, "最大显示对数：",       self._maxpair_var, 1, 100, 10)
        self._start_btn = ttk.Button(hdr, text="开始扫描",
                                     command=self._toggle_scan, width=BTN_W)
        self._start_btn.grid(row=1, column=2, sticky="e", pady=2)

        # Row 2: counters + 完成
        self._cnt_unmanaged = tk.StringVar(value="未管理: 0")
        self._cnt_managed   = tk.StringVar(value="已管理: 0")
        self._cnt_ignored   = tk.StringVar(value="不管理: 0")
        cnt_row = ttk.Frame(hdr)
        cnt_row.grid(row=2, column=0, columnspan=2, sticky="w", pady=2)
        for var, fg in [
            (self._cnt_unmanaged, "#1565C0"),
            (self._cnt_managed,   "#2E7D32"),
            (self._cnt_ignored,   "#757575"),
        ]:
            ttk.Label(cnt_row, textvariable=var, foreground=fg,
                      font=("Segoe UI", 10, "bold")).pack(side="left", padx=(0, 24))
        self._done_btn = ttk.Button(hdr, text="完成", command=self._on_close,
                                    width=BTN_W, state="disabled")
        self._done_btn.grid(row=2, column=2, sticky="e", pady=2)

        # ── Relation hint + progress (below grid) ────────────────────────────
        self._rel_var  = tk.StringVar(value="")
        self._prog_var = tk.StringVar(value="—")
        self._rel_label = tk.Label(win, textvariable=self._rel_var,
                                   fg="#555", anchor="w", bg="SystemButtonFace")
        self._rel_label.pack(fill="x", padx=8)
        ttk.Label(win, textvariable=self._prog_var, foreground="gray",
                  anchor="w").pack(fill="x", padx=8, pady=(0, 2))

        # ── Results treeview ─────────────────────────────────────────────────
        cols = ("时间", "链接路径", "目标路径")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=12,
                            selectmode="browse")
        self._tree = tree

        tree.heading("时间",   text="修改时间", anchor="w")
        tree.heading("链接路径", text="链接路径", anchor="w")
        tree.heading("目标路径", text="目标路径", anchor="w")

        tree.column("时间",     width=140, stretch=False)
        tree.column("链接路径", width=420)
        tree.column("目标路径", width=420)

        sb = ttk.Scrollbar(win, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=(4, 0))
        sb.pack(side="left", fill="y", pady=(4, 0), padx=(0, 4))

        tree.bind("<<TreeviewSelect>>", self._on_select)

        # ── Import panel ─────────────────────────────────────────────────────
        imp = ttk.LabelFrame(win, text="导入选中条目", padding=10)
        imp.pack(fill="x", padx=8, pady=(6, 4))
        self._import_frame = imp

        ttk.Label(imp, text="名称（必填）：", width=12, anchor="e").grid(
            row=0, column=0, sticky="e", padx=(0, 6))
        self._id_var = tk.StringVar()
        ttk.Entry(imp, textvariable=self._id_var, width=40).grid(
            row=0, column=1, sticky="ew", pady=2)

        ttk.Label(imp, text="描述（可选）：", width=12, anchor="e").grid(
            row=1, column=0, sticky="e", padx=(0, 6))
        self._desc_var = tk.StringVar()
        ttk.Entry(imp, textvariable=self._desc_var, width=40).grid(
            row=1, column=1, sticky="ew", pady=2)

        btn_row = ttk.Frame(imp)
        btn_row.grid(row=2, column=1, sticky="w", pady=(6, 0))
        ttk.Button(btn_row, text="导入管理", command=self._do_import,  width=10).pack(side="left")
        ttk.Button(btn_row, text="不管理",   command=self._do_ignore,  width=8).pack(side="left", padx=(8, 0))

        imp.columnconfigure(1, weight=1)
        self._import_frame.pack_forget()   # hidden until selection

        # ── Summary label ─────────────────────────────────────────────────────────
        self._summary_var = tk.StringVar()
        ttk.Label(win, textvariable=self._summary_var, foreground="gray",
                  anchor="w").pack(fill="x", padx=8, pady=(0, 6))

        self._refresh_treeview()

    # ── Scanning ──────────────────────────────────────────────────────────────

    def _toggle_scan(self):
        if self._scanning:
            self._stop_event.set()
            self._start_btn.configure(text="开始扫描")
            self._scanning = False
        else:
            self._start_scan()

    def _start_scan(self):
        root_str = self._dir_var.get().strip()
        if not root_str or not Path(root_str).is_dir():
            messagebox.showwarning("无效目录", "请先选择一个有效的目录。", parent=self._win)
            return

        self._stop_event.clear()
        self._scanning = True
        self._start_btn.configure(text="停止扫描")
        self._prog_var.set("准备扫描…")
        self._summary_var.set("")
        self._counters = [0, 0, 0]  # unmanaged, managed, ignored
        self._update_counters()

        # Collect known link/target paths for classification
        bases      = get_machine_config() or {}
        managed    = {str(e.link).lower() for e in self._managed_entries
                      if hasattr(e, "link")}
        scanned_db = get_scanned()
        ignored    = {(e["link"], e["target"]) for e in scanned_db if e.get("ignored")}

        max_depth   = self._depth_var.get()
        disp_depth  = self._disp_var.get()
        max_pairs   = self._maxpair_var.get()

        t = threading.Thread(
            target=self._scan_worker,
            args=(root_str, max_depth, disp_depth, max_pairs, managed, ignored, bases),
            daemon=True,
        )
        t.start()
        self._win.after(100, self._poll_sq)

    def _scan_worker(self, root_str, max_depth, disp_depth, max_pairs,
                     managed_set, ignored_set, bases):
        root      = Path(root_str)
        root_depth = len(root.parts)
        results   = []

        try:
            for dirpath, dirnames, _ in os.walk(root, topdown=True):
                if self._stop_event.is_set():
                    break

                cur_depth = len(Path(dirpath).parts) - root_depth

                # Progress: current path (truncated)
                disp_path = _truncate_path(dirpath, disp_depth)
                self._sq.put(("path", disp_path))

                for name in list(dirnames):
                    if self._stop_event.is_set():
                        break
                    full = Path(dirpath) / name
                    if not full.is_junction():
                        continue
                    try:
                        target = Path(os.readlink(str(full)))
                    except OSError:
                        continue

                    link_str   = _to_json_path(full, bases) if bases else str(full).replace("\\", "/")
                    target_str = _to_json_path(target, bases) if bases else str(target).replace("\\", "/")
                    mtime      = full.stat().st_mtime if full.exists() else 0

                    link_lower = str(full).lower()
                    ign_key    = (link_str, target_str)

                    if link_lower in managed_set:
                        self._sq.put(("count", "managed"))
                    elif ign_key in ignored_set:
                        self._sq.put(("count", "ignored"))
                    else:
                        self._sq.put(("count", "unmanaged"))
                        results.append({
                            "link":       link_str,
                            "target":     target_str,
                            "scanned_at": datetime.now().isoformat(timespec="seconds"),
                            "mtime":      datetime.fromtimestamp(mtime).isoformat(timespec="seconds") if mtime else "",
                            "mtime_ts":   mtime,
                            "ignored":    False,
                        })

                # Prune after junction check so junctions at max_depth are not missed
                if max_depth > 0 and cur_depth >= max_depth:
                    dirnames[:] = []

            results.sort(key=lambda e: e.get("mtime_ts", 0), reverse=True)
            self._sq.put(("done", results[:max_pairs], results))
        except Exception as exc:
            self._sq.put(("error", str(exc)))

    def _poll_sq(self):
        try:
            while True:
                msg = self._sq.get_nowait()
                kind = msg[0]
                if kind == "path":
                    self._prog_var.set(f"正在扫描：{msg[1]}")
                elif kind == "count":
                    idx = {"unmanaged": 0, "managed": 1, "ignored": 2}[msg[1]]
                    self._counters[idx] += 1
                    self._update_counters()
                elif kind == "done":
                    self._on_scan_done(msg[1], msg[2])
                    return
                elif kind == "error":
                    self._on_scan_error(msg[1])
                    return
        except queue.Empty:
            pass
        if self._scanning:
            self._win.after(100, self._poll_sq)

    def _on_scan_done(self, display_results, all_results):
        self._scanning = False
        self._start_btn.configure(text="开始扫描")
        self._done_btn.configure(state="normal")
        total = sum(self._counters)
        merge_scanned([{k: v for k, v in e.items() if k != "mtime_ts"}
                       for e in all_results])
        extra = len(all_results) - len(display_results)
        summary = (f"共找到 {self._counters[0]} 个未管理 · "
                   f"{self._counters[1]} 个已管理 · "
                   f"{self._counters[2]} 个不管理（共 {total} 对）")
        if extra > 0:
            summary += f" — 仅显示最近 {len(display_results)} 条，其余请查阅 JSON"
        self._prog_var.set(f"扫描完成　{summary}")
        self._summary_var.set("")
        self._populate_results(display_results)

        # Handle base registration / re-base after scan
        self._win.after(200, self._post_scan_base_dialog)

    def _post_scan_base_dialog(self):
        """After scan: offer re-base (case 2) or register new base (case 3)."""
        scan_str = self._dir_var.get().strip()
        if not scan_str:
            return
        bases = get_machine_config() or {}
        kind, keys = _classify_dir(Path(scan_str), bases)

        if kind == "parent":
            auto_name   = Path(scan_str).name.lower().replace(" ", "_")
            new_key_var = tk.StringVar(value=auto_name)
            dlg = tk.Toplevel(self._win)
            dlg.title("Re-base 路径")
            dlg.resizable(False, False)
            dlg.transient(self._win)
            dlg.grab_set()
            from tkinter import ttk as _ttk
            f = _ttk.Frame(dlg, padding=16); f.pack(fill="both", expand=True)
            keys_str = "、".join(f"{{{k}}}" for k in keys)
            _ttk.Label(f, text=f"扫描目录是 {keys_str} 的上级，路径将自动统一。",
                       font=("Segoe UI", 10, "bold")).pack(anchor="w")
            for k in keys:
                old_dir = Path(bases[k]).name
                _ttk.Label(f, text=f"  {{{k}}}/X  →  {{new_key}}/{old_dir}/X",
                           foreground="gray").pack(anchor="w", pady=(1, 0))
            nr = _ttk.Frame(f); nr.pack(fill="x", pady=(8, 12))
            _ttk.Label(nr, text="新 base 名称：").pack(side="left")
            _ttk.Entry(nr, textvariable=new_key_var, width=20).pack(side="left", padx=(4, 0))
            br = _ttk.Frame(f); br.pack(anchor="e")
            def do_rebase():
                nk = new_key_var.get().strip()
                if not nk:
                    messagebox.showwarning("名称不能为空", "", parent=dlg)
                    return
                for k in keys:
                    rebase(k, nk, scan_str)
                mgr.normalize_entries()
                dlg.destroy()
                self._rel_label.configure(fg="#2E7D32")
                self._rel_var.set(f"✓ 已 re-base 为 {{{nk}}}")
                self._on_done()
            _ttk.Button(br, text="确认", command=do_rebase, width=10).pack(side="left")
            _ttk.Button(br, text="取消", command=dlg.destroy, width=8).pack(side="left", padx=(8, 0))

        elif kind == "unrelated":
            nk = Path(scan_str).name.lower().replace(" ", "_")
            if not messagebox.askyesno(
                "注册新 base",
                f"扫描目录与现有 base 无关联，是否将其注册为新 base {{{nk}}}？",
                parent=self._win,
            ):
                return
            existing = dict(get_machine_config_full() or {})
            existing[nk] = scan_str
            register_machine(existing)
            self._rel_label.configure(fg="#2E7D32")
            self._rel_var.set(f"✓ 已自动注册为 {{{nk}}}")
            self._on_done()

    def _on_scan_error(self, msg):
        self._scanning = False
        self._start_btn.configure(text="开始扫描")
        self._prog_var.set(f"扫描出错：{msg}")

    # ── Results ───────────────────────────────────────────────────────────────

    def _populate_results(self, results):
        tree = self._tree
        for row in tree.get_children():
            tree.delete(row)
        bases = get_machine_config() or {}
        for e in results:
            link_disp   = _shorten_multi(e["link"], bases)
            target_disp = _shorten_multi(e["target"], bases)
            mtime_disp  = e.get("mtime", "")[:16].replace("T", " ")
            tree.insert("", "end", iid=iid_escape(e["link"]) + "||" + iid_escape(e["target"]),
                        values=(mtime_disp, link_disp, target_disp))
        self._import_frame.pack_forget()

    def _refresh_treeview(self):
        """Repopulate from stored scanned list (for persistent unmanaged items)."""
        bases   = get_machine_config() or {}
        managed = {str(e.link).lower() for e in self._managed_entries if hasattr(e, "link")}
        scanned = get_scanned()
        unmanaged = [e for e in scanned
                     if not e.get("ignored")
                     and _resolve_with_bases(e["link"], bases).lower() not in managed]
        unmanaged.sort(key=lambda e: e.get("mtime", ""), reverse=True)
        self._populate_results(unmanaged)

    # ── Selection / Import / Ignore ───────────────────────────────────────────

    def _on_select(self, _event):
        sel = self._tree.selection()
        if not sel:
            self._import_frame.pack_forget()
            return
        iid = sel[0]
        link_esc, _, _ = iid.partition("||")
        link_str  = iid_unescape(link_esc)
        suggested = Path(link_str).name
        self._id_var.set(suggested)
        self._desc_var.set("")
        self._import_frame.pack(fill="x", padx=8, pady=(0, 4))

    def _selected_keys(self):
        """Returns (iid, link_str, target_str) with link/target unescaped from the iid."""
        sel = self._tree.selection()
        if not sel:
            return None, None, None
        iid = sel[0]
        link_esc, _, target_esc = iid.partition("||")
        link_str   = link_esc.replace("__LB__", "{").replace("__RB__", "}")
        target_str = iid_unescape(target_esc)
        return iid, link_str, target_str

    def _do_import(self):
        iid, link_str, target_str = self._selected_keys()
        if not link_str:
            return
        entry_id = self._id_var.get().strip()
        if not entry_id:
            messagebox.showwarning("名称不能为空", "请填写名称后再导入。", parent=self._win)
            return
        if entry_id in mgr.get_all_entry_ids():
            messagebox.showwarning("名称重复",
                                   f"名称「{entry_id}」已存在，请换一个。",
                                   parent=self._win)
            return
        desc = self._desc_var.get().strip()
        ok, err = import_scanned_entry(link_str, target_str, entry_id, desc)
        if ok:
            self._tree.delete(iid)
            self._import_frame.pack_forget()
            self._id_var.set("")
            self._desc_var.set("")
            self._on_done()
        else:
            messagebox.showerror("导入失败", err, parent=self._win)

    def _do_ignore(self):
        iid, link_str, target_str = self._selected_keys()
        if not link_str:
            return
        ignore_scanned_entry(link_str, target_str)
        self._tree.delete(iid)
        self._import_frame.pack_forget()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _browse_dir(self):
        cur = self._dir_var.get()
        init = cur if cur and Path(cur).is_dir() else "/"
        p = filedialog.askdirectory(parent=self._win, title="选择扫描目录", initialdir=init)
        if p:
            self._dir_var.set(p)
            self._update_rel_label(p)
            self._reset_results()

    def _reset_results(self):
        """Clear previous scan results and disable 完成."""
        for row in self._tree.get_children():
            self._tree.delete(row)
        self._counters = [0, 0, 0]
        self._update_counters()
        self._prog_var.set("—")
        self._summary_var.set("")
        self._done_btn.configure(state="disabled")
        self._import_frame.pack_forget()

    def _on_dir_changed(self, _event=None):
        p = self._dir_var.get().strip()
        if p:
            self._update_rel_label(p)
            self._reset_results()

    def _update_rel_label(self, path_str: str):
        bases = get_machine_config() or {}
        if not bases:
            self._rel_var.set("⚠️ 当前机器未注册同步目录")
            self._rel_label.configure(fg="#E65100")
            return
        kind, keys = _classify_dir(Path(path_str), bases)
        if kind == "child":
            self._rel_var.set(f"✓ 在 {{{keys[0]}}} 下")
            self._rel_label.configure(fg="#2E7D32")
        elif kind == "parent":
            keys_str = "、".join(f"{{{k}}}" for k in keys)
            self._rel_var.set(f"⚠️ 是 {keys_str} 的上级（扫完后可 re-base）")
            self._rel_label.configure(fg="#F9A825")
        else:
            self._rel_var.set("⚠️ 未配置目录（扫完后可注册为新 base）")
            self._rel_label.configure(fg="#F9A825")

    def _update_counters(self):
        u, m, i = self._counters
        self._cnt_unmanaged.set(f"未管理: {u}")
        self._cnt_managed.set(f"已管理: {m}")
        self._cnt_ignored.set(f"不管理: {i}")

    def _on_close(self):
        if self._scanning:
            self._stop_event.set()
        if self._win and self._win.winfo_exists():
            self._win.destroy()
        self._win = None
        self._on_done()


# ── Module-level helpers ──────────────────────────────────────────────────────

def _resolve_with_bases(path_str: str, bases: dict[str, str]) -> str:
    """Resolve template path to absolute string (normalised to backslash)."""
    s = path_str
    for key, val in bases.items():
        s = s.replace("{" + key + "}", val)
    return s.replace("/", "\\")


def _shorten_multi(path_str: str, bases: dict[str, str]) -> str:
    return shorten_path(path_str, bases)
