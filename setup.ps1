# setup.ps1
# Registers watch_symlinks.ps1 as a login-triggered scheduled task.
# Must be run once as Administrator.

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$WatchScript = Join-Path $ScriptDir "watch_symlinks.ps1"
$TaskName    = "SymLinkWatcher"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Please run this script as Administrator."
    exit 1
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-WindowStyle Hidden -NonInteractive -ExecutionPolicy Bypass -File `"$WatchScript`""

$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval ([TimeSpan]::FromMinutes(1)) `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -RunLevel Highest `
    -Description "Watches symlinks.json and rebuilds symlinks on change" | Out-Null

Write-Host "Scheduled task '$TaskName' registered." -ForegroundColor Green

Write-Host "Start Watcher now without rebooting? [Y/N] " -NoNewline
if ((Read-Host) -match "^[Yy]") {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Watcher started." -ForegroundColor Green
}