# scan_symlinks.ps1
# Scans OneDrive for junctions, symlinks, and .lnk shortcuts.
# Prints results in real-time and saves to scan_result.txt.

$base    = "C:\Users\Shi_Y\OneDrive"
$outFile = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "scan_result.txt"
$shell   = New-Object -ComObject WScript.Shell

$scanDirs = @("Codes", "files", "Documents", "Useful", "桌面", "垃圾", "应用")

$allLines = [System.Collections.Generic.List[string]]::new()

# Enable ANSI escape codes on Windows
$null = [System.Console]::OutputEncoding
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class Console2 {
    [DllImport("kernel32.dll")] public static extern bool GetConsoleMode(IntPtr h, out uint m);
    [DllImport("kernel32.dll")] public static extern bool SetConsoleMode(IntPtr h, uint m);
    [DllImport("kernel32.dll")] public static extern IntPtr GetStdHandle(int n);
}
"@
$handle = [Console2]::GetStdHandle(-11)
$mode   = 0
[Console2]::GetConsoleMode($handle, [ref]$mode) | Out-Null
[Console2]::SetConsoleMode($handle, $mode -bor 4) | Out-Null  # ENABLE_VIRTUAL_TERMINAL_PROCESSING

$ESC     = [char]27
$UP1     = "$ESC[1A"   # cursor up 1 line
$CLRLINE = "$ESC[2K"   # clear entire line

$maxPathLen = 60
$maxDepth   = 5

function Truncate-Path {
    param([string]$p)
    if ($p.Length -le $maxPathLen) { return $p }
    return "..." + $p.Substring($p.Length - ($maxPathLen - 3))
}

function Show-Progress {
    param([string]$rel, [int]$symCount, [int]$lnkCount)
    $line1 = "  scanning: $(Truncate-Path $rel)"
    $line2 = "  [$symCount junctions, $lnkCount lnk]"
    Write-Host "`r${CLRLINE}$line1" -ForegroundColor DarkGray
    Write-Host "${CLRLINE}$line2" -NoNewline -ForegroundColor DarkGray
}

function Clear-Progress {
    # Erase both progress lines
    Write-Host "`r${CLRLINE}${UP1}${CLRLINE}" -NoNewline
}

function Print-And-Save {
    param([string]$line, [System.ConsoleColor]$color = "White")
    Clear-Progress
    Write-Host $line -ForegroundColor $color
    $allLines.Add($line)
}

function Get-Depth {
    param([string]$path, [string]$basePath)
    return ($path.Substring($basePath.Length).TrimStart("\").Split("\")).Count
}

$symCount    = 0
$lnkCount    = 0
$lastDir     = ""
$progressOn  = $false

foreach ($dir in $scanDirs) {
    $fullDir = Join-Path $base $dir
    if (-not (Test-Path $fullDir)) { continue }

    Get-ChildItem -Path $fullDir -Recurse -Force -ErrorAction SilentlyContinue | ForEach-Object {
        $depth   = Get-Depth $_.FullName $fullDir
        $scanDir = Split-Path -Parent $_.FullName

        if ($scanDir -ne $lastDir) {
            $rel = $scanDir.Substring($base.Length).TrimStart("\")
            if ($progressOn) { Clear-Progress }
            Show-Progress $rel $symCount $lnkCount
            $progressOn = $true
            $lastDir    = $scanDir
        }

        if ($depth -gt $maxDepth) { return }

        if ($_.LinkType -eq "SymbolicLink" -or $_.LinkType -eq "Junction") {
            $symCount++
            $target = $_.Target -join "; "
            Print-And-Save "[$($_.LinkType)]" Green
            Print-And-Save "  link  : $($_.FullName)"
            Print-And-Save "  target: $target"
            Print-And-Save ""
            Show-Progress $lastDir.Substring($base.Length).TrimStart("\") $symCount $lnkCount
        } elseif ($_.Extension -eq ".lnk") {
            $lnkCount++
            $t = try { $shell.CreateShortcut($_.FullName).TargetPath } catch { "(unresolvable)" }
            Print-And-Save "[lnk]" Yellow
            Print-And-Save "  file  : $($_.FullName)"
            Print-And-Save "  target: $t"
            Print-And-Save ""
            Show-Progress $lastDir.Substring($base.Length).TrimStart("\") $symCount $lnkCount
        }
    }
}

if ($progressOn) { Clear-Progress }

Write-Host "==============================" -ForegroundColor DarkGray
Write-Host "Junctions/Symlinks : $symCount" -ForegroundColor Green
Write-Host ".lnk shortcuts     : $lnkCount" -ForegroundColor Yellow
Write-Host "Saved to           : $outFile"
Write-Host "==============================" -ForegroundColor DarkGray

$allLines | Set-Content -Path $outFile -Encoding UTF8