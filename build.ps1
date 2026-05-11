# Build SymLiSync.exe
# Output goes to dist/ and build/ at repo root (sibling of src/)

$venv   = "C:\venvs\sym-link-gui"
$srcDir = "$PSScriptRoot\src"

& "$venv\Scripts\pyinstaller.exe" `
    --onefile `
    --noconsole `
    --name "SymLiSync" `
    --icon "$srcDir\ui\assets\icon.ico" `
    --add-data "$srcDir\ui\assets;ui/assets" `
    --distpath "$PSScriptRoot\dist" `
    --workpath "$PSScriptRoot\build" `
    --specpath "$srcDir" `
    "$srcDir\main.py"
