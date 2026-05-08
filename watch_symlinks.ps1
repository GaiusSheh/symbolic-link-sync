# watch_symlinks.ps1
# - Watches symlinks.json for changes and re-runs sync immediately
# - Checks for broken junctions every N minutes

$ScriptDir       = Split-Path -Parent $MyInvocation.MyCommand.Path
$JsonPath        = Join-Path $ScriptDir "symlinks.json"
$SyncScript      = Join-Path $ScriptDir "sync_symlinks.ps1"
$LogPath         = Join-Path $ScriptDir "watcher.log"
$CheckIntervalMin = 10   # broken junction check interval in minutes

function Write-Log {
    param([string]$msg)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Add-Content -Path $LogPath -Value $line
    Write-Host $line
}

function Run-Sync {
    param([string]$reason)
    Write-Log "[$reason] Running sync..."
    & powershell -NonInteractive -ExecutionPolicy Bypass -File $SyncScript 2>&1 |
        ForEach-Object { Write-Log $_ }
}

# Initial sync on startup
Run-Sync "startup"

# FileSystemWatcher for symlinks.json changes
$watcher = New-Object System.IO.FileSystemWatcher
$watcher.Path             = $ScriptDir
$watcher.Filter           = "symlinks.json"
$watcher.NotifyFilter     = [System.IO.NotifyFilters]::LastWrite

$debounceTimer = $null
$onChange = {
    if ($debounceTimer) { $debounceTimer.Stop(); $debounceTimer.Dispose() }
    $script:debounceTimer = [System.Timers.Timer]::new(2000)
    $script:debounceTimer.AutoReset = $false
    $script:debounceTimer.add_Elapsed({
        Run-Sync "json-changed"
    })
    $script:debounceTimer.Start()
}

Register-ObjectEvent $watcher "Changed" -Action $onChange | Out-Null
$watcher.EnableRaisingEvents = $true
Write-Log "Watching: $JsonPath"
Write-Log "Broken junction check every $CheckIntervalMin min."

# Main loop: periodic broken-link check
$lastCheck = [DateTime]::Now
try {
    while ($true) {
        Start-Sleep -Seconds 30

        $elapsed = ([DateTime]::Now - $lastCheck).TotalMinutes
        if ($elapsed -ge $CheckIntervalMin) {
            Run-Sync "periodic-check"
            $lastCheck = [DateTime]::Now
        }
    }
} finally {
    $watcher.EnableRaisingEvents = $false
    $watcher.Dispose()
    Write-Log "Watcher stopped."
}