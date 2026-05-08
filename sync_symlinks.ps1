# sync_symlinks.ps1
# Reads symlinks.json and rebuilds all symbolic links on the current machine.
# Also checks for broken junctions and fires a Windows toast notification if any found.
# Usage: .\sync_symlinks.ps1 [-DryRun] [-Verbose]

param(
    [switch]$DryRun,
    [switch]$Verbose
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$JsonPath    = Join-Path $ScriptDir "symlinks.json"
$MachineName = [System.Environment]::MachineName

function Resolve-Template {
    param([string]$Template, [hashtable]$Vars)
    $result = $Template
    foreach ($key in $Vars.Keys) { $result = $result.Replace("{$key}", $Vars[$key]) }
    return $result.Replace("/", "\")
}

function Send-Toast {
    param([string]$Title, [string]$Message)
    try {
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
        $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
            [Windows.UI.Notifications.ToastTemplateType]::ToastText02)
        $template.SelectSingleNode("//text[@id=1]").InnerText = $Title
        $template.SelectSingleNode("//text[@id=2]").InnerText = $Message
        $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("SymLinkWatcher")
        $notifier.Show([Windows.UI.Notifications.ToastNotification]::new($template))
    } catch {
        # Fallback: msg box
        [System.Windows.Forms.MessageBox]::Show($Message, $Title) | Out-Null
    }
}

if (-not (Test-Path $JsonPath)) { Write-Error "symlinks.json not found: $JsonPath"; exit 1 }

$config = Get-Content $JsonPath -Raw -Encoding UTF8 | ConvertFrom-Json

if (-not $config.machines.PSObject.Properties[$MachineName]) {
    Write-Warning "Machine '$MachineName' is not registered in symlinks.json."
    exit 1
}

$OneDrive = $config.machines.$MachineName.onedrive
$Vars     = @{ onedrive = $OneDrive }

Write-Host "Machine : $MachineName"
Write-Host "OneDrive: $OneDrive"
if ($DryRun) { Write-Host "[DryRun - no changes will be made]" -ForegroundColor Yellow }
Write-Host ""

$ok = 0; $skipped = 0; $failed = 0
$broken = [System.Collections.Generic.List[string]]::new()

foreach ($entry in $config.symlinks) {
    $id = $entry.id

    $rawTarget = $entry.target
    if ($entry.PSObject.Properties["target_override"] -and
        $entry.target_override.PSObject.Properties[$MachineName]) {
        $rawTarget = $entry.target_override.$MachineName
    }

    $linkPath   = Resolve-Template $entry.link $Vars
    $targetPath = Resolve-Template $rawTarget   $Vars

    if ($Verbose) {
        Write-Host "[$id] $($entry.description)"
        Write-Host "  link  : $linkPath"
        Write-Host "  target: $targetPath"
    }

    # ── Check for broken existing junction (target gone) ─────────────────
    $existing = Get-Item -LiteralPath $linkPath -ErrorAction SilentlyContinue
    if ($existing -and ($existing.LinkType -eq "Junction" -or $existing.LinkType -eq "SymbolicLink")) {
        if (-not (Test-Path $targetPath)) {
            Write-Host "[$id] BROKEN - target no longer exists: $targetPath" -ForegroundColor Red
            $broken.Add("[$id] $targetPath")
            $skipped++
            continue
        }
    }

    # ── Target missing (never existed on this machine) ────────────────────
    if (-not (Test-Path $targetPath)) {
        Write-Host "[$id] target not found, skipping: $targetPath" -ForegroundColor DarkGray
        $skipped++
        continue
    }

    if ($existing) {
        if ($existing.LinkType -eq "SymbolicLink" -or $existing.LinkType -eq "Junction") {
            $norm1 = ($existing.Target -replace '\\$', '')
            $norm2 = ($targetPath      -replace '\\$', '')
            if ($norm1 -eq $norm2) {
                Write-Host "[$id] already correct, skipping" -ForegroundColor DarkGray
                $skipped++; continue
            }
            Write-Host "[$id] target changed, rebuilding..." -ForegroundColor Yellow
        } else {
            Write-Host "[$id] replacing non-symlink: $($existing.Name)" -ForegroundColor Yellow
        }
        if (-not $DryRun) { Remove-Item -LiteralPath $linkPath -Force -Recurse }
    }

    $parentDir = Split-Path -Parent $linkPath
    if (-not (Test-Path $parentDir) -and -not $DryRun) {
        New-Item -ItemType Directory -Force -Path $parentDir | Out-Null
    }

    $itemType = if (Test-Path -PathType Container $targetPath) { "Junction" } else { "SymbolicLink" }
    try {
        if (-not $DryRun) { New-Item -ItemType $itemType -Path $linkPath -Target $targetPath | Out-Null }
        Write-Host "[$id] created ($itemType)" -ForegroundColor Green
        $ok++
    } catch {
        Write-Host "[$id] FAILED: $_" -ForegroundColor Red
        $failed++
    }
}

Write-Host ""
Write-Host "Done: $ok created, $skipped skipped, $failed failed"

# ── Toast notification for broken junctions ───────────────────────────────
if ($broken.Count -gt 0) {
    Write-Host ""
    Write-Host "BROKEN JUNCTIONS DETECTED:" -ForegroundColor Red
    $broken | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }

    $title = "SymLink: $($broken.Count) broken junction(s)"
    $msg   = $broken -join "`n"
    Send-Toast -Title $title -Message $msg
}