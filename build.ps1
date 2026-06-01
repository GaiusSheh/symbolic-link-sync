# Build SymLiSync.exe (tray GUI) + symlisync.exe (console CLI)
# Output goes to dist/ and build/ at repo root (sibling of src/)

$venv   = "C:\venvs\sym-link-gui"
$srcDir = "$PSScriptRoot\src"
$pyi    = "$venv\Scripts\pyinstaller.exe"

# ── Tray GUI (windowed, no console) ──────────────────────────────────────────
# NOTE: name must NOT be a case-only variant of the CLI (symlisync.exe), or the
# two collide on Windows' case-insensitive filesystem. Hence "SymLiSync-Tray".
& $pyi `
    --onefile `
    --noconsole `
    --name "SymLiSync-Tray" `
    --icon "$srcDir\ui\assets\icon.ico" `
    --add-data "$srcDir\ui\assets;ui/assets" `
    --distpath "$PSScriptRoot\dist" `
    --workpath "$PSScriptRoot\build" `
    --specpath "$PSScriptRoot" `
    "$srcDir\main.py"

# ── Console CLI (blocks the shell, clean stdout / exit codes for agents) ──────
& $pyi `
    --onefile `
    --console `
    --name "symlisync" `
    --icon "$srcDir\ui\assets\icon.ico" `
    --distpath "$PSScriptRoot\dist" `
    --workpath "$PSScriptRoot\build" `
    --specpath "$PSScriptRoot" `
    "$srcDir\cli.py"

# ── Installer (compile after the exes so the setup always bundles fresh ones) ──
# Skipped if Inno Setup (ISCC) isn't installed.
$iscc = "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) { $iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" }
if (Test-Path $iscc) {
    & $iscc "$PSScriptRoot\installer\SymLiSync.iss"
} else {
    Write-Host "ISCC not found - skipping installer build (exes are in dist/)."
}
