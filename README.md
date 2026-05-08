# Sym-Link

OneDrive does not sync symbolic links or junctions. This tool maintains a JSON database of all known symlinks and a background watcher that rebuilds them automatically on every machine.

## Files

| File | Purpose |
|---|---|
| `symlinks.json` | Source of truth — all symlink definitions and machine OneDrive paths |
| `sync_symlinks.ps1` | Reads the JSON and rebuilds all junctions on the current machine |
| `watch_symlinks.ps1` | Background process: watches for JSON changes and runs periodic broken-link checks |
| `setup.ps1` | One-time install: registers the watcher as a login-triggered scheduled task |
| `scan_symlinks.ps1` | Utility: scans OneDrive for existing junctions and .lnk shortcuts |
| `watcher.log` | Auto-generated log of all sync activity |

## First-time setup on a new machine

1. Add the machine to `symlinks.json` under `machines`:
```json
"machines": {
    "MY-PC": {
        "onedrive": "C:/Users/Username/OneDrive"
    }
}
```

2. Run `setup.ps1` as Administrator (once only):
```powershell
powershell -ExecutionPolicy Bypass -File "...\Sym-Link\setup.ps1"
```

This registers a scheduled task (`SymLinkWatcher`) that starts the watcher silently at every login.

## How it works

- **On login**: watcher starts and runs an initial sync
- **When `symlinks.json` changes**: sync runs immediately (debounced 2s for OneDrive write delay)
- **Every 10 minutes**: sync runs to detect broken junctions and fire a Windows toast notification if any are found

## Adding a new symlink

Edit `symlinks.json` and add an entry:

```json
{
    "id": "my-link",
    "description": "Short description of what this link is for",
    "link": "{onedrive}/path/to/the/link",
    "target": "{onedrive}/path/to/the/real/folder"
}
```

`{onedrive}` is resolved per-machine using the `machines` table. Save the file — the watcher picks up the change and builds the junction automatically.

If the target path differs between machines, use `target_override`:

```json
{
    "id": "my-link",
    "description": "...",
    "link": "{onedrive}/path/to/link",
    "target": "D:/default/path",
    "target_override": {
        "MY-OTHER-PC": "E:/different/path"
    }
}
```

## Running sync manually

```powershell
# Normal run
powershell -ExecutionPolicy Bypass -File "...\Sym-Link\sync_symlinks.ps1"

# Dry run (shows what would happen, no changes)
powershell -ExecutionPolicy Bypass -File "...\Sym-Link\sync_symlinks.ps1" -DryRun

# Verbose output
powershell -ExecutionPolicy Bypass -File "...\Sym-Link\sync_symlinks.ps1" -Verbose
```

## Important: renaming or moving target directories

Junctions store a hardcoded path string. If a target directory is renamed or moved, the junction becomes broken. The watcher will detect this within 10 minutes and show a toast notification.

**Correct workflow when reorganising folders:**
1. Update `symlinks.json` first
2. Then rename/move the directory
3. The watcher rebuilds the junction automatically

## Junction vs target: what OneDrive syncs

OneDrive does not follow junctions. It syncs the target directory directly via its own path. This means:

- If the target is **inside OneDrive**: file changes sync across machines normally. The junction is just a convenient entry point.
- If the target is **outside OneDrive** (e.g. a large local data folder): the junction is rebuilt on each machine from the JSON, but the contents are local only.

## Scanning for existing shortcuts

To find all existing junctions and `.lnk` shortcuts under OneDrive:

```powershell
powershell -ExecutionPolicy Bypass -File "...\Sym-Link\scan_symlinks.ps1"
```

Results are printed in real time and saved to `scan_result.txt`.