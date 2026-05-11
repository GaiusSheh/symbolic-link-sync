"""Core logic: read symlinks.json, detect junction status, create/repair junctions."""

import json
import os
import re
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Optional

from core.paths import get_symlinks_json as _get_json_path

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
    target_empty: bool = False       # True when status is OK but target directory is empty
    machine_specific: bool = False   # True when link or target uses an absolute path (no template)


@dataclass
class SyncResult:
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed:  list[str] = field(default_factory=list)
    broken:  list[str] = field(default_factory=list)


# ── JSON I/O ──────────────────────────────────────────────────────────────────

def _load_raw() -> dict:
    try:
        with open(_get_json_path(), encoding="utf-8-sig") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_raw(cfg: dict) -> None:
    _get_json_path().parent.mkdir(parents=True, exist_ok=True)
    with open(_get_json_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)


# ── Machine / path helpers ────────────────────────────────────────────────────

def _machine_name() -> str:
    return socket.gethostname().upper()


def _local_data(cfg: dict, machine: str) -> dict:
    """Return (creating if needed) the local_data sub-dict for a machine."""
    return (cfg.setdefault("local_data", {})
               .setdefault(machine, {"symlinks": [], "scanned": []}))


def _is_global(link_json: str, target_json: str) -> bool:
    """True when both paths use only template tokens (no absolute paths)."""
    return link_json.startswith("{") and target_json.startswith("{")


def get_other_machines_local_entries() -> dict[str, list[dict]]:
    """Return {entry_id: [raw_with_machine_name, ...]} for all other machines."""
    cfg     = _load_raw()
    machine = _machine_name()
    result: dict[str, list[dict]] = {}
    for mname, mdata in cfg.get("local_data", {}).items():
        if mname.upper() == machine:
            continue
        for raw in mdata.get("symlinks", []):
            result.setdefault(raw["id"], []).append({**raw, "_machine": mname})
    return result


def normalize_entries() -> tuple[int, int]:
    """Normalize global ↔ local_data placement for this machine in one pass.

    Demote:         global entries where link or target is not a template → local_data.
    Promote:        local managed entries where both paths are templates and all machines
                    have the required bases → global symlinks.
    Promote scanned: unignored scanned entries whose paths resolve to valid global templates
                    → global symlinks (auto-managed, removed from scanned list).

    Returns (n_demoted, n_promoted).  n_promoted includes scanned promotions.
    """
    cfg     = _load_raw()
    machine = _machine_name()
    bases   = get_machine_config() or {}

    # ── Demote ────────────────────────────────────────────────────────────────
    global_list = cfg.get("symlinks", [])
    to_keep:   list[dict] = []
    to_demote: list[dict] = []
    for raw in global_list:
        if _is_global(raw.get("link", ""), raw.get("target", "")):
            to_keep.append(raw)
        else:
            to_demote.append(raw)

    if to_demote:
        cfg["symlinks"] = to_keep
        local    = _local_data(cfg, machine)
        existing = {r["id"] for r in local.get("symlinks", [])}
        for raw in to_demote:
            if raw["id"] not in existing:
                local.setdefault("symlinks", []).append(raw)

    # ── Promote managed ───────────────────────────────────────────────────────
    local      = _local_data(cfg, machine)
    local_list = local.get("symlinks", [])
    promoted: list[dict] = []
    remaining: list[dict] = []
    for raw in local_list:
        link_j   = _to_json_path(_resolve(raw["link"],   bases), bases)
        target_j = _to_json_path(_resolve(raw["target"], bases), bases)
        if not _is_global(link_j, target_j):
            remaining.append(raw)
            continue
        keys = set(re.findall(r"\{(\w+)\}", link_j + target_j))
        if _all_machines_have_keys(cfg, keys):
            raw["link"], raw["target"] = link_j, target_j
            cfg.setdefault("symlinks", []).append(raw)
            promoted.append(raw)
        else:
            remaining.append(raw)
    if promoted:
        local["symlinks"] = remaining

    # ── Promote scanned ───────────────────────────────────────────────────────
    local        = _local_data(cfg, machine)
    scanned_list = local.get("scanned", [])
    kept_scanned: list[dict] = []
    n_scanned_promoted = 0
    existing_ids = {r["id"] for r in cfg.get("symlinks", [])} | \
                   {r["id"] for r in local.get("symlinks", [])}
    for scan_raw in scanned_list:
        if scan_raw.get("ignored", False):
            kept_scanned.append(scan_raw)
            continue
        link_j   = _to_json_path(_resolve(scan_raw["link"],   bases), bases)
        target_j = _to_json_path(_resolve(scan_raw["target"], bases), bases)
        if not _is_global(link_j, target_j):
            kept_scanned.append(scan_raw)
            continue
        keys = set(re.findall(r"\{(\w+)\}", link_j + target_j))
        if not _all_machines_have_keys(cfg, keys):
            kept_scanned.append(scan_raw)
            continue
        # Build a unique ID from the link filename
        base_id  = Path(link_j).name
        entry_id = base_id
        counter  = 1
        while entry_id in existing_ids:
            entry_id = f"{base_id}-{counter}"
            counter += 1
        new_entry = {"id": entry_id, "description": "", "link": link_j, "target": target_j}
        cfg.setdefault("symlinks", []).append(new_entry)
        existing_ids.add(entry_id)
        n_scanned_promoted += 1

    # Always write back the filtered list (e.g. ignored entries may have changed)
    local["scanned"] = kept_scanned
    changed = bool(to_demote or promoted or n_scanned_promoted
                   or kept_scanned != scanned_list)
    if changed:
        _save_raw(cfg)
    return len(to_demote), len(promoted) + n_scanned_promoted


def migrate_to_local_data() -> None:
    """One-time migration from old format (top-level scanned / machine field)."""
    cfg     = _load_raw()
    machine = _machine_name()
    changed = False

    # Move 'machine'-tagged symlinks into local_data
    remaining = []
    for raw in cfg.get("symlinks", []):
        m = raw.pop("machine", "")
        if m:
            _local_data(cfg, m.upper())["symlinks"].append(raw)
            changed = True
        else:
            remaining.append(raw)
    if changed:
        cfg["symlinks"] = remaining

    # Move top-level scanned into local_data
    if "scanned" in cfg:
        _local_data(cfg, machine)["scanned"] = cfg.pop("scanned")
        changed = True

    if changed:
        _save_raw(cfg)


def _all_machines_have_keys(cfg: dict, keys: set[str]) -> bool:
    """True if every registered machine has handled all required template keys.

    A key is "handled" when it's present in the machine config (confirmed or ignored/null).
    """
    if not cfg.get("machines"):
        return False
    return all(
        all(k in (mc or {}) for k in keys)
        for mc in cfg.get("machines", {}).values()
    )




def get_machine_config_full() -> Optional[dict[str, Optional[str]]]:
    """Return all base entries for this machine including ignored (null) ones.

    Returns None if the machine is not registered at all.
    Ignored bases appear as None values; confirmed bases are path strings.
    Keys starting with '__' (internal metadata like __drives__) are excluded.
    """
    name     = _machine_name()
    cfg      = _load_raw()
    machines = {k.upper(): v for k, v in cfg.get("machines", {}).items()}
    raw      = machines.get(name)
    if raw is None:
        return None
    return {k: v for k, v in raw.items() if not k.startswith("__")}


def get_machine_config() -> Optional[dict[str, str]]:
    """Return confirmed (non-null) base paths for this machine, or None if not registered."""
    full = get_machine_config_full()
    if full is None:
        return None
    return {k: v for k, v in full.items() if v is not None}


def is_registered() -> bool:
    return get_machine_config_full() is not None


def get_base_status() -> dict[str, dict]:
    """Return per-key status for this machine.

    Each value is {"state": "confirmed"|"ignored"|"pending", "path": str|None}.
    Includes all keys referenced in global symlinks plus all keys in this machine's config.
    """
    full = get_machine_config_full()
    if full is None:
        return {}
    all_keys = get_required_bases() | set(full.keys())
    result = {}
    for key in all_keys:
        if key not in full:
            result[key] = {"state": "pending", "path": None}
        elif full[key] is None:
            result[key] = {"state": "ignored", "path": None}
        else:
            result[key] = {"state": "confirmed", "path": full[key]}
    return result


def _get_local_drives() -> list[str]:
    """Enumerate fixed (non-removable, non-network) drives. Returns ['C:/', 'D:/'] etc."""
    import ctypes
    import string
    DRIVE_FIXED = 3
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    result = []
    for i, letter in enumerate(string.ascii_uppercase):
        if bitmask & (1 << i):
            root = f"{letter}:/"
            if ctypes.windll.kernel32.GetDriveTypeW(root) == DRIVE_FIXED:
                result.append(root)
    return result


def get_machine_drives() -> list[Path]:
    """Return stored drive roots for this machine; falls back to live detection."""
    name = _machine_name()
    cfg  = _load_raw()
    raw  = {k.upper(): v for k, v in cfg.get("machines", {}).items()}.get(name) or {}
    drives = raw.get("__drives__", [])
    if not drives:
        drives = _get_local_drives()
    return [Path(d) for d in drives]


def refresh_machine_drives() -> None:
    """Re-detect local drives and update stored list if changed (called at startup)."""
    cfg     = _load_raw()
    machine = _machine_name()
    entry   = cfg.get("machines", {}).get(machine)
    if entry is None:
        return
    current = _get_local_drives()
    if entry.get("__drives__") != current:
        entry["__drives__"] = current
        _save_raw(cfg)


def register_machine(bases: dict[str, Optional[str]]) -> None:
    """Write or update the current machine's base paths.

    Values may be path strings (confirmed) or None (ignored/not available on this machine).
    Also auto-detects and stores local fixed drives under '__drives__'.
    """
    cfg           = _load_raw()
    machine_entry = dict(bases)
    machine_entry["__drives__"] = _get_local_drives()
    cfg.setdefault("machines", {})[_machine_name()] = machine_entry
    _save_raw(cfg)


def detect_sync_services() -> dict[str, str]:
    """Auto-detect installed sync services. Returns {suggested_name: path}."""
    import base64
    import winreg
    found: dict[str, str] = {}

    # OneDrive
    for reg_path in (
        r"SOFTWARE\Microsoft\OneDrive",
        r"SOFTWARE\Microsoft\SkyDrive",
    ):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path)
            val, _ = winreg.QueryValueEx(key, "UserSkyDriveRootFolder")
            winreg.CloseKey(key)
            if Path(val).is_dir():
                found["onedrive"] = val.replace("\\", "/")
                break
        except OSError:
            pass
    if "onedrive" not in found:
        default = Path.home() / "OneDrive"
        if default.is_dir():
            found["onedrive"] = str(default).replace("\\", "/")

    # Google Drive (DriveFS)
    gdrive_local = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "DriveFS"
    if gdrive_local.is_dir():
        for child in gdrive_local.iterdir():
            if child.is_dir() and (child / "root").is_dir():
                found["gdrive"] = str(child / "root").replace("\\", "/")
                break

    # Dropbox
    host_db = Path(os.environ.get("APPDATA", "")) / "Dropbox" / "host.db"
    if host_db.exists():
        try:
            lines = host_db.read_bytes().splitlines()
            if len(lines) >= 2:
                p = base64.b64decode(lines[1]).decode("utf-8")
                if Path(p).is_dir():
                    found["dropbox"] = p.replace("\\", "/")
        except Exception:
            pass

    # iCloud
    icloud = Path.home() / "iCloudDrive"
    if icloud.is_dir():
        found["icloud"] = str(icloud).replace("\\", "/")

    return found


def rename_base_key(renames: list[tuple[str, str]]) -> None:
    """Rename one or more base keys in all template paths (pure string rename).

    Each element of renames is (old_key, new_key). All renames are applied in a
    single load/save to avoid repeated I/O.
    """
    cfg = _load_raw()
    pairs = [("{" + old + "}", "{" + new + "}") for old, new in renames]

    def _rw(s: str) -> str:
        for old_tmpl, new_tmpl in pairs:
            s = s.replace(old_tmpl, new_tmpl)
        return s

    for raw in cfg.get("symlinks", []):
        raw["link"]   = _rw(raw.get("link", ""))
        raw["target"] = _rw(raw.get("target", ""))
        for k, v in raw.get("target_override", {}).items():
            raw["target_override"][k] = _rw(v)
    for mc_data in cfg.get("local_data", {}).values():
        for raw in mc_data.get("symlinks", []) + mc_data.get("scanned", []):
            raw["link"]   = _rw(raw.get("link", ""))
            raw["target"] = _rw(raw.get("target", ""))
    for mc in cfg.get("machines", {}).values():
        for old_key, new_key in renames:
            if old_key in mc:
                mc[new_key] = mc.pop(old_key)
    _save_raw(cfg)


def rebase(old_key: str, new_key: str, new_path: str) -> None:
    """Promote a parent directory as the new base for an existing base.

    Rewrites {old_key}/X → {new_key}/old_dir_name/X in all symlink paths,
    registers new_key in machine config, removes old_key.
    """
    cfg     = _load_raw()
    name    = _machine_name()
    machine = cfg.get("machines", {}).get(name, {})
    old_path = machine.get(old_key, "")
    old_dir  = Path(old_path).name   # e.g. "OneDrive" from "C:/WebDrives/OneDrive"

    old_tmpl = "{" + old_key + "}"
    new_pfx  = "{" + new_key + "}/" + old_dir

    def _rw(s: str) -> str:
        if s.startswith(old_tmpl):
            return new_pfx + s[len(old_tmpl):]
        return s

    def _rw_entry(raw: dict) -> None:
        raw["link"]   = _rw(raw.get("link", ""))
        raw["target"] = _rw(raw.get("target", ""))
        for k, v in raw.get("target_override", {}).items():
            raw["target_override"][k] = _rw(v)

    for raw in cfg.get("symlinks", []):
        _rw_entry(raw)

    for mc_data in cfg.get("local_data", {}).values():
        for raw in mc_data.get("symlinks", []):
            _rw_entry(raw)
        for raw in mc_data.get("scanned", []):
            raw["link"]   = _rw(raw.get("link", ""))
            raw["target"] = _rw(raw.get("target", ""))

    machine[new_key] = new_path.replace("\\", "/")
    machine.pop(old_key, None)
    cfg.setdefault("machines", {})[name] = machine
    _save_raw(cfg)


def _copy_entries_to_local(entries: list[dict], local: dict,
                           existing: set[str], mc_bases: dict) -> None:
    """Resolve template paths and append entries into local["symlinks"] (skips duplicates)."""
    for raw in entries:
        if raw["id"] in existing:
            continue
        raw_copy = dict(raw)
        raw_copy["link"]   = str(_resolve(raw["link"],   mc_bases)).replace("\\", "/")
        raw_copy["target"] = str(_resolve(raw["target"], mc_bases)).replace("\\", "/")
        if "target_override" in raw:
            raw_copy["target_override"] = {
                k: str(_resolve(v, mc_bases)).replace("\\", "/")
                for k, v in raw["target_override"].items()
            }
        local.setdefault("symlinks", []).append(raw_copy)


def demote_base_entries(keys: set[str]) -> int:
    """Globally demote entries referencing keys from global symlinks into each machine's local_data.

    For each machine that has any of keys configured (non-null), entries are written
    with all template paths fully resolved to that machine's absolute paths.
    Machines that don't have any of keys configured are skipped.
    Returns number of entries demoted.
    """
    cfg = _load_raw()
    to_keep: list[dict] = []
    to_demote: list[dict] = []
    for raw in cfg.get("symlinks", []):
        used: set[str] = set(re.findall(r"\{(\w+)\}", raw.get("link", "") + raw.get("target", "")))
        for v in raw.get("target_override", {}).values():
            used.update(re.findall(r"\{(\w+)\}", v))
        if used & keys:
            to_demote.append(raw)
        else:
            to_keep.append(raw)
    if to_demote:
        cfg["symlinks"] = to_keep
        for mc_name, mc_config in cfg.get("machines", {}).items():
            mc_bases = {k: v for k, v in mc_config.items()
                        if v is not None and not k.startswith("__")}
            if not any(k in mc_bases for k in keys):
                continue
            local    = _local_data(cfg, mc_name)
            existing = {r["id"] for r in local.get("symlinks", [])}
            _copy_entries_to_local(to_demote, local, existing, mc_bases)
        _save_raw(cfg)
    return len(to_demote)


def demote_base_entries_local(keys: set[str]) -> int:
    """Copy global entries using keys into THIS machine's local_data with resolved absolute paths.

    Global entries are kept intact for other machines to continue using.
    Only processes keys that this machine has a real (non-null) path for.
    Returns number of entries copied.
    """
    cfg       = _load_raw()
    machine   = _machine_name()
    mc_config = cfg.get("machines", {}).get(machine, {})
    # Only the keys being removed that this machine actually had a real path for
    keys_with_path = {k for k in keys
                      if mc_config.get(k) not in (None, "") and not k.startswith("__")}
    if not keys_with_path:
        return 0
    # Resolve using ALL of this machine's bases so the result is fully absolute
    mc_bases = {k: v for k, v in mc_config.items()
                if v is not None and not k.startswith("__")}
    to_copy: list[dict] = []
    for raw in cfg.get("symlinks", []):
        used: set[str] = set(re.findall(r"\{(\w+)\}", raw.get("link", "") + raw.get("target", "")))
        for v in raw.get("target_override", {}).values():
            used.update(re.findall(r"\{(\w+)\}", v))
        if used & keys_with_path:
            to_copy.append(raw)
    if to_copy:
        local    = _local_data(cfg, machine)
        existing = {r["id"] for r in local.get("symlinks", [])}
        _copy_entries_to_local(to_copy, local, existing, mc_bases)
        _save_raw(cfg)
    return len(to_copy)


def get_required_bases() -> set[str]:
    """Return all {key} template names referenced in global symlink paths."""
    cfg  = _load_raw()
    keys: set[str] = set()
    for raw in cfg.get("symlinks", []):
        for fld in ("link", "target"):
            keys.update(re.findall(r"\{(\w+)\}", raw.get(fld, "")))
        for v in raw.get("target_override", {}).values():
            keys.update(re.findall(r"\{(\w+)\}", v))
    return keys


def get_pending_bases() -> set[str]:
    """Return base keys used globally but neither confirmed nor ignored on this machine."""
    required = get_required_bases()
    full     = get_machine_config_full() or {}
    return required - set(full.keys())



def _resolve(template: str, bases: dict[str, str]) -> Path:
    """Replace any {key} in template using the bases dict."""
    s = template
    for key, path in bases.items():
        s = s.replace("{" + key + "}", path)
    return Path(s)


def _to_json_path(p: Path, bases: dict[str, str]) -> str:
    """Convert absolute path to template form using the longest matching base."""
    s = str(p).replace("\\", "/")
    for prefix in ("//?/", "//./"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    best_key = None
    best_len = 0
    for key, base in bases.items():
        b = base.replace("\\", "/").rstrip("/")
        if s.lower().startswith(b.lower() + "/") or s.lower() == b.lower():
            if len(b) > best_len:
                best_key = key
                best_len = len(b)
    if best_key:
        b = bases[best_key].replace("\\", "/").rstrip("/")
        return "{" + best_key + "}" + s[best_len:]
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
    err = _decode(r.stdout) or _decode(r.stderr)   # mklink writes errors to stdout
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
        except OSError:
            # Only use /s /q for truly empty dirs with permission issues (e.g. OneDrive placeholders).
            # Never silently destroy a non-empty directory.
            try:
                if any(link.iterdir()):
                    return False, "目录非空，已中止删除"
            except OSError:
                pass
            r = subprocess.run(f'rmdir /s /q "{link}"', shell=True,
                               capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            return r.returncode == 0, _decode(r.stderr)
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
    full_config = get_machine_config_full()
    if full_config is None:
        return []
    bases        = {k: v for k, v in full_config.items() if v is not None}
    ignored_keys = {k for k, v in full_config.items() if v is None}

    cfg     = _load_raw()
    machine = _machine_name()
    # Combine global symlinks + this machine's local symlinks
    all_raws = list(cfg.get("symlinks", []))
    all_raws += cfg.get("local_data", {}).get(machine, {}).get("symlinks", [])

    entries = []
    for raw in all_raws:
        raw_target = raw["target"]
        overrides  = {k.upper(): v for k, v in raw.get("target_override", {}).items()}
        if machine in overrides:
            raw_target = overrides[machine]
        # Skip entries whose required base is explicitly ignored on this machine
        required = set(re.findall(r"\{(\w+)\}", raw["link"] + raw_target))
        if required & ignored_keys:
            continue
        link   = _resolve(raw["link"],  bases)
        target = _resolve(raw_target,   bases)
        status = _detect_status(link, target)
        try:
            t_empty = status == Status.OK and target.is_dir() and not any(target.iterdir())
        except OSError:
            t_empty = False
        m_specific = not raw["link"].startswith("{") or not raw_target.startswith("{")
        entries.append(LinkEntry(
            id=raw["id"],
            description=raw.get("description", ""),
            link=link,
            target=target,
            status=status,
            target_empty=t_empty,
            machine_specific=m_specific,
        ))
    return entries


def get_ignored_entries() -> list[dict]:
    """Return global symlink entries whose required bases are ignored on this machine."""
    full_config = get_machine_config_full()
    if full_config is None:
        return []
    ignored_keys = {k for k, v in full_config.items() if v is None}
    if not ignored_keys:
        return []
    cfg = _load_raw()
    result = []
    for raw in cfg.get("symlinks", []):
        required = set(re.findall(r"\{(\w+)\}", raw["link"] + raw.get("target", "")))
        if required & ignored_keys:
            result.append(raw)
    return result


def sync_all() -> SyncResult:
    result = SyncResult()
    for entry in check_all():
        if entry.status == Status.OK:
            result.skipped.append(entry.id)
        elif entry.status == Status.BROKEN:
            if entry.target.exists():
                # Target is reachable per config; rebuild the broken junction.
                if os.path.lexists(entry.link):
                    ok_rm, _ = _remove_link(entry.link)
                    if not ok_rm:
                        result.failed.append(entry.id)
                        continue
                ok, _ = _create_junction(entry.link, entry.target)
                if ok:
                    result.created.append(entry.id)
                else:
                    result.failed.append(entry.id)
            else:
                result.broken.append(entry.id)
        elif entry.status == Status.MISSING:
            result.skipped.append(entry.id)
        elif entry.status == Status.PENDING:
            ok, _ = _create_junction(entry.link, entry.target)
            if ok:
                result.created.append(entry.id)
            else:
                result.failed.append(entry.id)
    return result


# ── Write API ─────────────────────────────────────────────────────────────────

def get_all_entry_ids() -> set[str]:
    """Return all entry IDs from global symlinks and this machine's local_data."""
    cfg     = _load_raw()
    machine = _machine_name()
    ids = {r["id"] for r in cfg.get("symlinks", [])}
    ids |= {r["id"] for r in cfg.get("local_data", {}).get(machine, {}).get("symlinks", [])}
    return ids


def create_entry(entry_id: str, description: str, link: Path, target: Path,
                 force_overwrite: bool = False) -> tuple[bool, str]:
    """Append a new entry to symlinks.json and create the junction.

    If the link path is a non-empty directory and force_overwrite is False,
    returns (False, ERR_LINK_NONEMPTY) so the caller can confirm with the user.
    Pass force_overwrite=True after user confirms to delete it and proceed.
    """
    if not entry_id:
        return False, "编号不能为空"

    cfg   = _load_raw()
    bases = get_machine_config()
    if not bases:
        return False, "当前机器未在 symlinks.json 中注册"

    machine = _machine_name()
    all_ids = {r["id"] for r in cfg.get("symlinks", [])}
    all_ids |= {r["id"] for r in cfg.get("local_data", {}).get(machine, {}).get("symlinks", [])}
    if entry_id in all_ids:
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
    link_json   = _to_json_path(link,   bases)
    target_json = _to_json_path(target, bases)
    new_entry: dict = {
        "id":          entry_id,
        "description": description,
        "link":        link_json,
        "target":      target_json,
    }
    # Route to global symlinks or machine-local depending on path templates
    if _is_global(link_json, target_json):
        cfg.setdefault("symlinks", []).append(new_entry)
    else:
        _local_data(cfg, _machine_name())["symlinks"].append(new_entry)
    _save_raw(cfg)

    # ── Create junction ───────────────────────────────────────────────────────
    if target.exists():
        ok, err = _create_junction(link, target)
        if not ok:
            return False, f"配置已保存，但创建 Junction 失败：{err or '未知错误'}"

    return True, ""


def delete_entry(entry_id: str, remove_junction: bool = True) -> tuple[bool, str]:
    """Remove entry from symlinks.json and optionally delete the junction."""
    cfg   = _load_raw()
    bases = get_machine_config()

    machine  = _machine_name()
    global_list = cfg.get("symlinks", [])
    local_list  = cfg.get("local_data", {}).get(machine, {}).get("symlinks", [])

    deleted_raw = next((r for r in global_list if r["id"] == entry_id), None) or \
                  next((r for r in local_list  if r["id"] == entry_id), None)
    if not deleted_raw:
        return False, f"编号 '{entry_id}' 不存在"

    if remove_junction and bases:
        link_path = _resolve(deleted_raw["link"], bases)
        _remove_link(link_path)

    cfg["symlinks"] = [r for r in global_list if r["id"] != entry_id]
    if machine in cfg.get("local_data", {}):
        cfg["local_data"][machine]["symlinks"] = [
            r for r in local_list if r["id"] != entry_id
        ]
    _save_raw(cfg)
    return True, ""


def edit_entry(entry_id: str,
               new_target:      Optional[Path] = None,
               new_description: Optional[str]  = None,
               new_id:          Optional[str]  = None,
               new_link:        Optional[Path]  = None,
               force_overwrite: bool = False) -> bool:
    cfg   = _load_raw()
    bases = get_machine_config()
    if not bases:
        return False

    target_changed = False
    link_changed   = False
    old_link: Optional[Path] = None
    found = False
    machine = _machine_name()

    # Search both global symlinks and local_data for this machine
    local_list = cfg.get("local_data", {}).get(machine, {}).get("symlinks", [])
    for raw in list(cfg.get("symlinks", [])) + local_list:
        if raw["id"] != entry_id:
            continue
        found = True
        if new_description is not None:
            raw["description"] = new_description
        if new_target is not None:
            s = _to_json_path(new_target, bases)
            if raw.get("target") != s:
                raw["target"] = s
                target_changed = True
        if new_link is not None:
            s = _to_json_path(new_link, bases)
            if raw.get("link") != s:
                old_link = _resolve(raw["link"], bases)
                raw["link"] = s
                link_changed = True
        if new_id is not None and new_id != entry_id:
            all_ids = (
                {r["id"] for r in cfg.get("symlinks", [])}
                | {r["id"] for r in cfg.get("local_data", {}).get(machine, {}).get("symlinks", [])}
            )
            all_ids.discard(entry_id)
            if new_id in all_ids:
                return False   # duplicate ID — reject rename
            raw["id"] = new_id
        break

    if not found:
        return False

    _save_raw(cfg)

    effective_id = new_id if (new_id and new_id != entry_id) else entry_id

    if link_changed and old_link is not None:
        _remove_link(old_link)

    import logging as _log
    for entry in check_all():
        if entry.id == effective_id:
            if not entry.target.exists():
                # New target doesn't exist yet; remove stale junction so status
                # shows MISSING rather than a misleading OK pointing to old target.
                if target_changed and os.path.lexists(entry.link):
                    _remove_link(entry.link)
                return True
            # Remove existing path if force-overwrite requested
            if force_overwrite and entry.link.exists():
                r = subprocess.run(
                    f'rmdir /s /q "{entry.link}"',
                    shell=True, capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                _log.info("rmdir %s → rc=%d  stderr=%r",
                          entry.link, r.returncode, _decode(r.stderr))
                if r.returncode != 0:
                    return False
            # Rebuild if junction is missing, broken, or target was changed
            if target_changed or not entry.link.is_junction() or not entry.link.exists():
                if os.path.lexists(entry.link):   # lexists catches broken junctions
                    ok_rm, err_rm = _remove_link(entry.link)
                    _log.info("remove_link %s → ok=%s  err=%r", entry.link, ok_rm, err_rm)
                    if not ok_rm:
                        return False
                ok, err = _create_junction(entry.link, entry.target)
                _log.info("create_junction %s → ok=%s  err=%r", entry.link, ok, err)
                if not ok:
                    return False
            return True

    # Entry not found in check_all (e.g. base not configured on this machine).
    # If a structural change was requested the junction was never rebuilt — report failure.
    if link_changed or new_link is not None or new_target is not None:
        return False
    return True


# ── Scanned-entry helpers ─────────────────────────────────────────────────────

def get_scanned() -> list[dict]:
    machine = _machine_name()
    return _load_raw().get("local_data", {}).get(machine, {}).get("scanned", [])


def save_scanned(entries: list[dict]) -> None:
    cfg = _load_raw()
    _local_data(cfg, _machine_name())["scanned"] = entries
    _save_raw(cfg)


def merge_scanned(new_entries: list[dict]) -> None:
    """Merge new scan results into this machine's local scanned list."""
    cfg      = _load_raw()
    ld       = _local_data(cfg, _machine_name())
    existing = ld.get("scanned", [])
    existing_map = {(e["link"], e["target"]): e for e in existing}
    for entry in new_entries:
        key = (entry["link"], entry["target"])
        if key not in existing_map:
            existing_map[key] = entry
    ld["scanned"] = list(existing_map.values())
    _save_raw(cfg)


def import_scanned_entry(link_str: str, target_str: str,
                          entry_id: str, description: str) -> tuple[bool, str]:
    """Move a scanned entry into managed symlinks and create the junction."""
    cfg   = _load_raw()
    bases = get_machine_config()
    if not bases:
        return False, "当前机器未注册"

    # Remove from this machine's local scanned list
    ld = _local_data(cfg, _machine_name())
    ld["scanned"] = [e for e in ld.get("scanned", [])
                     if not (e["link"] == link_str and e["target"] == target_str)]

    # Re-apply template using current bases (stored path may be absolute)
    link   = _resolve(link_str,   bases)
    target = _resolve(target_str, bases)
    link_stored   = _to_json_path(link,   bases)
    target_stored = _to_json_path(target, bases)

    new_entry = {
        "id": entry_id,
        "description": description,
        "link": link_stored,
        "target": target_stored,
    }
    if _is_global(link_stored, target_stored):
        cfg.setdefault("symlinks", []).append(new_entry)
    else:
        ld["symlinks"].append(new_entry)
    _save_raw(cfg)

    if target.exists() and not (link.is_junction() and link.exists()):
        if os.path.lexists(link):
            _remove_link(link)
        ok, err = _create_junction(link, target)
        if not ok:
            return False, f"配置已保存，但创建 Junction 失败：{err}"
    return True, ""


def ignore_scanned_entry(link_str: str, target_str: str) -> None:
    """Mark a scanned entry as ignored (user does not want to manage it)."""
    cfg = _load_raw()
    ld  = _local_data(cfg, _machine_name())
    for e in ld.get("scanned", []):
        if e["link"] == link_str and e["target"] == target_str:
            e["ignored"] = True
            break
    _save_raw(cfg)


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

    All registered base paths get a single recursive watch (covers all moves
    within the cloud-sync directory). Non-base paths get individual non-recursive
    ancestor watches.
    """
    cfg   = get_machine_config() or {}
    bases = {Path(v).resolve(): True for v in cfg.values() if v}
    result: dict[Path, bool] = {}

    for e in entries:
        for path in (e.link, e.target):
            matched = False
            for base_root in bases:
                try:
                    path.relative_to(base_root)
                    result[base_root] = True
                    matched = True
                    break
                except ValueError:
                    pass
            if not matched:
                for ancestor in path.parents:
                    if ancestor.parent == ancestor:   # drive root
                        break
                    if ancestor not in result:
                        result[ancestor] = False

    return result


def repath_entries(old_base_str: str, new_base_str: str) -> tuple[list[str], list[str]]:
    """Batch-update all JSON paths prefixed by old_base → new_base, then rebuild junctions.
    Returns (updated_ids, failed_ids)."""
    cfg   = _load_raw()
    bases = get_machine_config()
    if not bases:
        return [], []

    old_base = Path(old_base_str)
    new_base = Path(new_base_str)
    machine  = _machine_name()

    affected: dict[str, dict] = {}

    for raw in cfg.get("symlinks", []):
        eid = raw["id"]
        ch: dict = {}

        link_path = _resolve(raw["link"], bases)
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
        target_path = _resolve(raw_target, bases)
        try:
            rel = target_path.relative_to(old_base)
            ch["target"]          = new_base / rel
            ch["target_override"] = use_override
        except ValueError:
            pass

        if ch:
            affected[eid] = ch

    for raw in cfg.get("local_data", {}).get(machine, {}).get("symlinks", []):
        eid = raw["id"]
        if eid in affected:
            continue
        ch = {}
        link_path = _resolve(raw["link"], bases)
        try:
            rel = link_path.relative_to(old_base)
            ch["link"]     = new_base / rel
            ch["old_link"] = link_path
        except ValueError:
            pass
        target_path = _resolve(raw.get("target", ""), bases)
        try:
            rel = target_path.relative_to(old_base)
            ch["target"]          = new_base / rel
            ch["target_override"] = False
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
            raw["link"] = _to_json_path(ch["link"], bases)
        if "target" in ch:
            s = _to_json_path(ch["target"], bases)
            if ch.get("target_override"):
                for k in raw.get("target_override", {}):
                    if k.upper() == machine:
                        raw["target_override"][k] = s
            else:
                raw["target"] = s

    for raw in cfg.get("local_data", {}).get(machine, {}).get("symlinks", []):
        ch = affected.get(raw["id"])
        if ch is None:
            continue
        if "link" in ch:
            raw["link"] = _to_json_path(ch["link"], bases)
        if "target" in ch:
            s = _to_json_path(ch["target"], bases)
            raw["target"] = s
            for k in raw.get("target_override", {}):
                if k.upper() == machine:
                    raw["target_override"][k] = s

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
    """Update only the link path in JSON; does not touch the filesystem.

    Searches both global symlinks and this machine's local_data.
    Global ↔ local_data normalisation is handled by normalize_entries().
    """
    cfg     = _load_raw()
    bases   = get_machine_config() or {}
    if not bases:
        return False
    machine      = _machine_name()
    new_link_str = _to_json_path(new_link, bases)

    for raw in cfg.get("symlinks", []):
        if raw["id"] == entry_id:
            raw["link"] = new_link_str
            _save_raw(cfg)
            return True

    for raw in cfg.get("local_data", {}).get(machine, {}).get("symlinks", []):
        if raw["id"] == entry_id:
            raw["link"] = new_link_str
            _save_raw(cfg)
            return True

    return False
