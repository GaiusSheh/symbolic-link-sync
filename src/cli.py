"""Console CLI for SymLiSync — headless symlink management for scripts/agents.

Operates directly on the core (no tray, no IPC): every command returns a
process exit code (0 = success, non-zero = failure) and supports --json so an
automation agent can parse the result. It shares data/settings.json with the
tray app (same install dir), so run the GUI once to pick the symlinks.json
location, or pass --db to point at one explicitly.
"""

import argparse
import json
import sys
from pathlib import Path

from core import explorer_menu
from core import paths
from core import settings_manager as sm
from core import symlink_manager as mgr


def _activate_db(db: str | None) -> None:
    """Point the core at the active symlinks.json (CLI flag > saved setting)."""
    if db:
        paths.set_symlinks_json(Path(db))
        return
    s = sm.load()
    if s.symlinks_path:
        paths.set_symlinks_json(Path(s.symlinks_path))


def _emit(args, payload: dict, human: str) -> None:
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(human)


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_list(args) -> int:
    entries = mgr.check_all()
    if args.json:
        print(json.dumps(
            [{"id": e.id, "status": e.status.name,
              "link": str(e.link), "target": str(e.target)}
             for e in entries], ensure_ascii=False, indent=2))
    else:
        if not entries:
            print("(no managed entries)")
        for e in entries:
            print(f"{e.status.name:8} {e.id}\t{e.link} -> {e.target}")
    return 0


def cmd_status(args) -> int:
    entries = mgr.check_all()
    counts: dict[str, int] = {}
    for e in entries:
        counts[e.status.name] = counts.get(e.status.name, 0) + 1
    payload = {"total": len(entries), "by_status": counts}
    human = f"total={len(entries)} " + " ".join(
        f"{k}={v}" for k, v in sorted(counts.items()))
    _emit(args, payload, human or "total=0")
    return 0


def cmd_add(args) -> int:
    if not mgr.is_registered():
        _emit(args, {"ok": False, "error": "machine not registered"},
              "FAILED: 本机尚未注册（请先运行 GUI 完成配置）")
        return 2
    link   = Path(args.link)
    target = Path(args.target)
    eid    = args.name or link.name
    ok, err = mgr.create_entry(eid, args.description or "", link, target,
                               force_overwrite=args.force)
    mgr.normalize_entries()
    _emit(args, {"ok": ok, "id": eid, "error": ("" if ok else err)},
          f"OK: {eid}  {link} -> {target}" if ok else f"FAILED: {err}")
    return 0 if ok else 1


def cmd_remove(args) -> int:
    ok, err = mgr.delete_entry(args.id, remove_junction=args.with_link)
    mgr.normalize_entries()
    _emit(args, {"ok": ok, "id": args.id, "error": ("" if ok else err)},
          f"OK: removed {args.id}" if ok else f"FAILED: {err}")
    return 0 if ok else 1


def cmd_sync(args) -> int:
    res = mgr.sync_all()
    payload = {"created": res.created, "skipped": res.skipped,
               "failed": res.failed, "broken": res.broken}
    human = (f"created={len(res.created)} skipped={len(res.skipped)} "
             f"failed={len(res.failed)} broken={len(res.broken)}")
    if res.created and not args.json:
        human += "\n  created: " + ", ".join(res.created)
    _emit(args, payload, human)
    return 1 if (res.failed or res.broken) else 0


def cmd_cleanup(args) -> int:
    """Remove machine-level integration (Explorer menu + autostart). Invoked by
    the uninstaller; safe to run anytime."""
    actions: list[str] = []
    try:
        explorer_menu.unregister()
        actions.append("explorer menu removed")
    except Exception as exc:
        actions.append(f"explorer menu: {exc}")
    try:
        sm.set_autostart(False)
        actions.append("autostart removed")
    except Exception as exc:
        actions.append(f"autostart: {exc}")
    _emit(args, {"ok": True, "actions": actions}, "; ".join(actions))
    return 0


# ── Parser ──────────────────────────────────────────────────────────────────--

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable JSON output")
    common.add_argument("--db", help="path to symlinks.json (overrides saved setting)")

    p = argparse.ArgumentParser(
        prog="symlisync", description="SymLiSync command-line interface")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", parents=[common], help="list managed entries + status").set_defaults(func=cmd_list)
    sub.add_parser("status", parents=[common], help="summary counts by status").set_defaults(func=cmd_status)

    a = sub.add_parser("add", parents=[common], help="create + manage a symlink (junction)")
    a.add_argument("link", help="符号链接位置 (where the link is created)")
    a.add_argument("target", help="符号链接指向 (the directory it points at)")
    a.add_argument("--name", help="entry id (default: link folder name)")
    a.add_argument("--description", help="optional description")
    a.add_argument("--force", action="store_true", help="overwrite a non-empty dir at the link path")
    a.set_defaults(func=cmd_add)

    r = sub.add_parser("remove", parents=[common], help="remove a managed entry")
    r.add_argument("id", help="entry id to remove")
    r.add_argument("--with-link", action="store_true", help="also delete the symlink itself (not its target data)")
    r.set_defaults(func=cmd_remove)

    sub.add_parser("sync", parents=[common], help="rebuild broken / pending links").set_defaults(func=cmd_sync)
    sub.add_parser("cleanup", parents=[common], help="remove Explorer menu + autostart (used by uninstaller)").set_defaults(func=cmd_cleanup)
    return p


def main(argv=None) -> int:
    # Force UTF-8 output so non-GBK paths don't crash on a CP936 console.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    _activate_db(getattr(args, "db", None))
    try:
        return args.func(args)
    except Exception as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
