param(
    [double]$Hours = 15,
    [int]$IntervalSec = 60,
    [double]$AlarmMiB = 100,
    [string]$OutDir = (Join-Path $PSScriptRoot 'soak')
)
# Long-running soak monitor for SymLiSync-Tray (issue #1).
# Samples the worker process (PyInstaller onefile child) every IntervalSec and
# appends to soak.csv. Never exits early: a restarted tray is followed to its
# new PID, and an alarm (private growth > AlarmMiB over that PID's first sample)
# is recorded in status.txt while sampling continues.
#
# Run detached so it survives the editor/agent session:
#   Start-Process pwsh -WindowStyle Hidden -ArgumentList '-NoProfile','-File','<this>.ps1'
$ErrorActionPreference = 'Continue'
New-Item -ItemType Directory -Force $OutDir | Out-Null
$csv    = Join-Path $OutDir 'soak.csv'
$status = Join-Path $OutDir 'status.txt'
$log    = "$env:LOCALAPPDATA\Programs\SymLiSync\SymLiSync.log"

function Get-Worker {
    $procs = @(Get-CimInstance Win32_Process -Filter "Name='SymLiSync-Tray.exe'")
    $ids = $procs.ProcessId
    $procs | Where-Object { $ids -contains $_.ParentProcessId } | Select-Object -First 1
}

function Write-Status([string]$msg) {
    "$(Get-Date -Format s)  $msg" | Add-Content $status -Encoding UTF8
}

if (-not (Test-Path $csv)) {
    "time,pid,priv_mib,ws_mib,threads,handles,drive_failed_lines" | Set-Content $csv -Encoding UTF8
}
Write-Status "START soak pid=$PID hours=$Hours interval=${IntervalSec}s alarm=${AlarmMiB}MiB"

$end = (Get-Date).AddHours($Hours)
$wpid = $null; $base = $null; $alarmed = $false; $n = 0
while ((Get-Date) -lt $end) {
    $p = if ($wpid) { Get-Process -Id $wpid -ErrorAction SilentlyContinue } else { $null }
    if (-not $p) {
        $w = Get-Worker
        if ($w) {
            if ($wpid) { Write-Status "worker PID changed $wpid -> $($w.ProcessId) (tray restarted)" }
            $wpid = $w.ProcessId; $base = $null; $alarmed = $false
            $p = Get-Process -Id $wpid -ErrorAction SilentlyContinue
        } elseif ($wpid -ne -1) {
            Write-Status "worker not running"; $wpid = -1
        }
    }
    if ($p) {
        $priv = [math]::Round($p.PrivateMemorySize64 / 1MB, 1)
        $ws   = [math]::Round($p.WorkingSet64 / 1MB, 1)
        if ($null -eq $base) { $base = $priv }
        $failed = 0
        try { $failed = @(Select-String -Path $log -Pattern 'DriveRootThread .*failed' -Encoding UTF8).Count } catch {}
        "$(Get-Date -Format s),$wpid,$priv,$ws,$($p.Threads.Count),$($p.HandleCount),$failed" |
            Add-Content $csv -Encoding UTF8
        if (-not $alarmed -and ($priv - $base) -gt $AlarmMiB) {
            Write-Status "ALARM pid=$wpid private grew $([math]::Round($priv - $base, 1)) MiB (base $base)"
            $alarmed = $true
        }
        $n++
    }
    Start-Sleep -Seconds $IntervalSec
}
Write-Status "DONE $n samples"
