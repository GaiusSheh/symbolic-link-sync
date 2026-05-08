"""Core logic: read symlinks.json, detect junction status, create/repair junctions."""

import json
import os
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Optional

_JSON_PATH = Path(__file__).parent.parent / "symlinks.json"

# Sentinel returned by create_entry when the link path is a non-empty directory,
# so the caller (UI) can ask the user for confirmation before retrying with force=True.
ERR_LINK_NONEMPTY = "LINK_NONEMPTY"


class Status(Enum):
    OK      = auto()   # junction exists and target reachable
    BROKEN  = auto()   # junction (or symlink) exists but target gone
    PENDING = auto()   # no junction yet, but target exists
    MISSING = auto()   # no junction, target also absent (OneDrive not synced)


@dataclass
class LinkEntry:
    id: str
    description: str
    link: Path
    target: Path
    status: Status
    target_empty: bool = False   # True when status is OK but target directory is empty


@dataclass
class SyncResult:
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed:  list[str] = field(default_factory=list)
    broken:  list[str] = field(default_factory=list)


# ── JSON I/O ──────────────────────────────────────────────────────────────────

def _load_raw() -> dict:
    with open(_JSON_PATH, encoding="utf-8-sig") as f:
        return json.load(f)


def _save_raw(cfg: dict) -> None:
    with open(_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)


# ── Machine / path helpers ────────────────────────────────────────────────────

def _machine_name() -> str:
    return socket.gethostname().upper()


def get_onedrive() -> Optional[str]:
    name = _machine_name()
    cfg  = _load_raw()
    machines = {k.upper(): v for k, v in cfg.get("machines", {}).items()}
    return machines[name]["onedrive"] if name in machines else None


def _resolve(template: str, onedrive: str) -> Path:
    return Path(template.replace("{onedrive}", onedrive))


def _to_json_path(p: Path, onedrive: str) -> str:
    s  = str(p).replace("\\", "/")
    od = onedrive.replace("\\", "/")
    if s.startswith(od):
        s = "{onedrive}" + s[len(od):]
    return s


# ── cmd helpers ───────────────────────────────────────────────────────────────

def _decode(b: bytes) -> str:
    for enc in ("gbk", "utf-8", "latin-1"):
        try:
            return b.decode(enc).strip()
        except Exception:
            pass
    return b.decode("latin-1").strip()


def _create_junction(link: Path, target: Path) -> tuple[bool, str]:
    link.parent.mkdir(parents=True, exist_ok=True)
    # shell=True + quoted paths: the only reliable way to pass mklink via cmd
    cmd = f'mklink /J "{link}" "{target}"'
    r   = subprocess.run(cmd, shell=True, capture_output=True,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    err = _decode(r.stderr) or _decode(r.stdout)
    return r.returncode == 0, err


def _has_reparse_point(p: Path) -> bool:
    import ctypes
    INVALID = 0xFFFFFFFF
    attrs = ctypes.windll.kernel32.GetFileAttributesW(str(p))
    return attrs != INVALID and bool(attrs & 0x400)


def _remove_link(link: Path) -> tuple[bool, str]:
    """Remove a junction, symlink, reparse point, or empty directory at link path."""
    if not os.path.lexists(link):
        return True, ""
    # Treat any reparse point (junction, symlink, OneDrive placeholder, etc.)
    # as a link — use rmdir which handles these correctly without following them.
    if link.is_junction() or link.is_symlink() or _has_reparse_point(link):
        cmd = f'rmdir "{link}"'
        r   = subprocess.run(cmd, shell=True, capture_output=True,
                             creationflags=subprocess.CREATE_NO_WINDOW)
        return r.returncode == 0, _decode(r.stderr)
    if link.is_dir():
        try:
            link.rmdir()   # only succeeds if empty
            return True, ""
        except OSError as e:
            return False, str(e)
    try:
        link.unlink()
        return True, ""
    except OSError as e:
        return False, str(e)


# ── Status detection ──────────────────────────────────────────────────────────

def _detect_status(link: Path, target: Path) -> Status:
    if not os.path.lexists(link):
        return Status.PENDING if target.exists() else Status.MISSING

    # Something is at the link path — only treat it as OK/BROKEN if it's actually
    # a junction or symlink. A plain directory is not a valid junction.
    if link.is_junction() or link.is_symlink():
        return Status.OK if link.exists() else Status.BROKEN

    # Regular file or directory sitting where the junction should be → broken
    return Status.BROKEN


# ── Public read API ───────────────────────────────────────────────────────────

def check_all() -> list[LinkEntry]:
    onedrive = get_onedrive()
    if onedrive is None:
        return []
    cfg     = _load_raw()
    machine = _machine_name()
    entries = []
    for raw in cfg.get("symlinks", []):
        raw_target = raw["target"]
        overrides  = {k.upper(): v for k, v in raw.get("target_override", {}).items()}
        if machine in overrides:
            raw_target = overrides[machine]
        link   = _resolve(raw["link"],   onedrive)
        target = _resolve(raw_target,    onedrive)
        status = _detect_status(link, target)
        try:
            t_empty = status == Status.OK and target.is_dir() and not any(target.iterdir())
        except OSError:
            t_empty = False
        entries.append(LinkEntry(
            id=raw["id"],
            description=raw.get("description", ""),
            link=link,
            target=target,
            status=status,
            target_empty=t_empty,
        ))
    return entries


def sync_all() -> SyncResult:
    result = SyncResult()
    for entry in check_all():
        if entry.status == Status.OK:
            result.skipped.append(entry.id)
        elif entry.status in (Status.BROKEN, Status.MISSING):
            if entry.status == Status.BROKEN:
                result.broken.append(entry.id)
            result.skipped.append(entry.id)
        elif entry.status == Status.PENDING:
            ok, _ = _create_junction(entry.link, entry.target)
            if ok:
                result.created.append(entry.id)
            else:
                result.failed.append(entry.id)
    return result


# ── Write API ─────────────────────────────────────────────────────────────────

def create_entry(entry_id: str, description: str, link: Path, target: Path,
                 force_overwrite: bool = False) -> tuple[bool, str]:
    """Append a new entry to symlinks.json and create the junction.

    If the link path is a non-empty directory and force_overwrite is False,
    returns (False, ERR_LINK_NONEMPTY) so the caller can confirm with the user.
    Pass force_overwrite=True after user confirms to delete it and proceed.
    """
    if not entry_id:
        return False, "编号不能为空"

    cfg      = _load_raw()
    onedrive = get_onedrive()
    if onedrive is None:
        return False, "当前机器未在 symlinks.json 中注册"

    if any(r["id"] == entry_id for r in cfg.get("symlinks", [])):
        return False, f"编号 '{entry_id}' 已存在"

    # ── Handle existing content at link path ──────────────────────────────────
    if os.path.lexists(link):
        if link.is_junction() or link.is_symlink() or _has_reparse_point(link):
            ok, err = _remove_link(link)
            if not ok:
                return False, f"无法移除现有链接: {err}"
        elif link.is_dir():
            contents = list(link.iterdir())
            if contents and not force_overwrite:
                return False, ERR_LINK_NONEMPTY
            cmd = f'rmdir /s /q "{link}"'
            r = subprocess.run(cmd, shell=True, capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            if r.returncode != 0:
                return False, f"无法删除现有目录: {_decode(r.stderr) or _decode(r.stdout)}"
        else:
            link.unlink(missing_ok=True)

    # ── Write JSON ────────────────────────────────────────────────────────────
    cfg.setdefault("symlinks", []).append({
        "id":          entry_id,
        "description": description,
        "link":        _to_json_path(link,   onedrive),
        "target":      _to_json_path(target, onedrive),
    })
    _save_raw(cfg)

    # ── Create junction ───────────────────────────────────────────────────────
    if target.exists():
        ok, err = _create_junction(link, target)
        if not ok:
            return False, f"配置已保存，但创建 Junction 失败：{err or '未知错误'}"

    return True, ""


def delete_entry(entry_id: str, remove_junction: bool = True) -> tuple[bool, str]:
    """Remove entry from symlinks.json and optionally delete the junction."""
    cfg      = _load_raw()
    onedrive = get_onedrive()

    original = cfg.get("symlinks", [])
    remaining = [r for r in original if r["id"] != entry_id]
    if len(remaining) == len(original):
        return False, f"编号 '{entry_id}' 不存在"

    if remove_junction and onedrive:
        deleted = next(r for r in original if r["id"] == entry_id)
        machine   = _machine_name()
        raw_link  = deleted["link"]
        link_path = _resolve(raw_link, onedrive)
        _remove_link(link_path)

    cfg["symlinks"] = remaining
    _save_raw(cfg)
    return True, ""


def repair(entry_id: str, new_target: Path) -> bool:
    return edit_entry(entry_id, new_target=new_target)


def edit_entry(entry_id: str,
               new_target:      Optional[Path] = None,
               new_description: Optional[str]  = None,
               new_id:          Optional[str]  = None,
               new_link:        Optional[Path]  = None) -> bool:
    cfg      = _load_raw()
    onedrive = get_onedrive()
    if onedrive is None:
        return False

    target_changed = False
    link_changed   = False
    old_link: Optional[Path] = None
    found = False

    for raw in cfg["symlinks"]:
        if raw["id"] != entry_id:
            continue
        found = True
        if new_description is not None:
            raw["description"] = new_description
        if new_target is not None:
            s = _to_json_path(new_target, onedrive)
            if raw.get("target") != s:
                raw["target"] = s
                target_changed = True
        if new_link is not None:
            s = _to_json_path(new_link, onedrive)
            if raw.get("link") != s:
                old_link = _resolve(raw["link"], onedrive)
                raw["link"] = s
                link_changed = True
        if new_id is not None and new_id != entry_id:
            raw["id"] = new_id
        break

    if not found:
        return False

    _save_raw(cfg)

    effective_id = new_id if (new_id and new_id != entry_id) else entry_id

    if link_changed or target_changed:
        if link_changed and old_link is not None:
            _remove_link(old_link)
        for entry in check_all():
            if entry.id == effective_id:
                if not link_changed:
                    _remove_link(entry.link)
                if entry.target.exists():
                    _create_junction(entry.link, entry.target)
                return True

    return True


# ── Smart sync (JSON-change triggered) ───────────────────────────────────────

def smart_sync(known_ids: set[str]) -> SyncResult:
    """Create junctions only for entries whose ID was not previously known."""
    result = SyncResult()
    for entry in check_all():
        if entry.id not in known_ids:
            if entry.status == Status.PENDING:
                ok, _ = _create_junction(entry.link, entry.target)
                if ok:
                    result.created.append(entry.id)
                else:
                    result.failed.append(entry.id)
            elif entry.status == Status.BROKEN:
                result.broken.append(entry.id)
        else:
            if entry.status == Status.BROKEN:
                result.broken.append(entry.id)
            else:
                result.skipped.append(entry.id)
    return result


# ── Watch-dir collection ──────────────────────────────────────────────────────

def collect_watch_dirs(entries: list[LinkEntry]) -> dict[Path, bool]:
    """Maps directory → recursive.

    OneDrive paths: a single recursive watch on the OneDrive root covers all moves
    within OneDrive (including cross-sub-directory moves that don't fire on_moved
    under non-recursive watches).

    Non-OneDrive paths: individual ancestor dirs with recursive=False.
    """
    onedrive_path = get_onedrive()
    od_root = Path(onedrive_path).resolve() if onedrive_path else None
    result: dict[Path, bool] = {}

    for e in entries:
        for path in (e.link, e.target):
            if od_root:
                try:
                    path.relative_to(od_root)
                    result[od_root] = True   # one recursive watch covers entire OneDrive tree
                    continue
                except ValueError:
                    pass
            # Non-OneDrive path: individual non-recursive ancestor watches
            for ancestor in path.parents:
                if ancestor.parent == ancestor:   # drive root
                    break
                if ancestor not in result:
                    result[ancestor] = False

    return result


def repath_entries(old_base_str: str, new_base_str: str) -> tuple[list[str], list[str]]:
    """Batch-update all JSON paths prefixed by old_base → new_base, then rebuild junctions.
    Returns (updated_ids, failed_ids)."""
    cfg      = _load_raw()
    onedrive = get_onedrive()
    if onedrive is None:
        return [], []

    old_base = Path(old_base_str)
    new_base = Path(new_base_str)
    machine  = _machine_name()

    affected: dict[str, dict] = {}   # entry_id → {link?, old_link?, target?, target_override?}

    for raw in cfg.get("symlinks", []):
        eid = raw["id"]
        ch: dict = {}

        link_path = _resolve(raw["link"], onedrive)
        try:
            rel = link_path.relative_to(old_base)
            ch["link"]     = new_base / rel
            ch["old_link"] = link_path
        except ValueError:
            pass

        raw_target   = raw.get("target", "")
        overrides    = {k.upper(): v for k, v in raw.get("target_override", {}).items()}
        use_override = machine in overrides
        if use_override:
            raw_target = overrides[machine]
        target_path = _resolve(raw_target, onedrive)
        try:
            rel = target_path.relative_to(old_base)
            ch["target"]          = new_base / rel
            ch["target_override"] = use_override
        except ValueError:
            pass

        if ch:
            affected[eid] = ch

    if not affected:
        return [], []

    for raw in cfg.get("symlinks", []):
        ch = affected.get(raw["id"])
        if ch is None:
            continue
        if "link" in ch:
            raw["link"] = _to_json_path(ch["link"], onedrive)
        if "target" in ch:
            s = _to_json_path(ch["target"], onedrive)
            if ch.get("target_override"):
                for k in raw.get("target_override", {}):
                    if k.upper() == machine:
                        raw["target_override"][k] = s
            else:
                raw["target"] = s
    _save_raw(cfg)

    updated: list[str] = []
    failed:  list[str] = []
    for entry in check_all():
        ch = affected.get(entry.id)
        if ch is None:
            continue
        if "old_link" in ch:
            _remove_link(ch["old_link"])   # no-op when already gone (dir was renamed)
        _remove_link(entry.link)           # clear stale / broken junction at new path
        if entry.target.exists():
            ok, _ = _create_junction(entry.link, entry.target)
            (updated if ok else failed).append(entry.id)
        else:
            updated.append(entry.id)       # JSON updated; junction pending target availability
    return updated, failed


# ── Rename detection ──────────────────────────────────────────────────────────

def _norm(p: Path) -> str:
    return str(p).lower().replace("/", "\\").rstrip("\\")


def find_renamed_junction(entry: LinkEntry) -> Optional[Path]:
    """Scan entry.link's parent dir for a junction pointing to the same target."""
    parent = entry.link.parent
    if not parent.is_dir():
        return None
    target_norm = _norm(entry.target)
    try:
        for child in parent.iterdir():
            if child == entry.link:
                continue
            if not (child.is_junction() or _has_reparse_point(child)):
                continue
            try:
                rp = os.readlink(child)
                # Strip NT / Win32 long-path prefixes: \\?\ or \??\
                for prefix in ("\\\\?\\", "\\??\\"):
                    if rp.startswith(prefix):
                        rp = rp[len(prefix):]
                        break
                if _norm(Path(rp)) == target_norm:
                    return child
            except OSError:
                pass
    except OSError:
        pass
    return None


def rename_link_in_json(entry_id: str, new_link: Path) -> bool:
    """Update only the link path in JSON; does not touch the filesystem."""
    cfg      = _load_raw()
    onedrive = get_onedrive()
    if onedrive is None:
        return False
    for raw in cfg.get("symlinks", []):
        if raw["id"] == entry_id:
            raw["link"] = _to_json_path(new_link, onedrive)
            _save_raw(cfg)
            return True
    return False
