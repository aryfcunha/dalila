# Install Dalila as a Windows service via NSSM.
#
# Why a service: keeps `dalila bot` running across logout, reboot, and crashes.
# NSSM (Non-Sucking Service Manager) is the standard free tool for this on
# Windows — wraps any executable as a proper Windows service with auto-restart,
# stdout/stderr capture, and dependency management.
#
# Prerequisites:
#   1. NSSM installed:  winget install NSSM.NSSM
#                       (or download from https://nssm.cc/download)
#   2. Run THIS script from an elevated (Admin) PowerShell, since installing
#      services requires Administrator rights.
#   3. `claude login` already completed in the user account this service will
#      run under — the service inherits that user's Claude Code credentials.
#
# Usage (Admin PowerShell):
#   cd C:\Users\pc14043\Documents\Dalila\dalila
#   .\scripts\install-service.ps1
#
# To remove later:
#   nssm remove Dalila confirm
#
# To check status:
#   nssm status Dalila
#   Get-Content .\logs\dalila-stderr.log -Tail 50

$ErrorActionPreference = 'Stop'

$ServiceName = 'Dalila'
$Root = (Resolve-Path "$PSScriptRoot\..").Path
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$LogDir = Join-Path $Root 'logs'

if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
    Write-Error "NSSM not found on PATH. Install with: winget install NSSM.NSSM"
}
if (-not (Test-Path $Python)) {
    Write-Error "Python venv not found at $Python. Run setup first."
}
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory $LogDir | Out-Null }

# If service already exists, stop + remove it so this script is idempotent.
$existing = & nssm status $ServiceName 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Service '$ServiceName' exists — removing for fresh install..."
    & nssm stop $ServiceName 2>$null | Out-Null
    & nssm remove $ServiceName confirm | Out-Null
}

Write-Host "Installing service '$ServiceName'..."
& nssm install $ServiceName $Python '-m' 'dalila' 'bot'
& nssm set $ServiceName AppDirectory $Root
& nssm set $ServiceName AppStdout (Join-Path $LogDir 'dalila-stdout.log')
& nssm set $ServiceName AppStderr (Join-Path $LogDir 'dalila-stderr.log')
& nssm set $ServiceName AppRotateFiles 1
& nssm set $ServiceName AppRotateOnline 1
& nssm set $ServiceName AppRotateBytes 10485760  # rotate at 10 MB
& nssm set $ServiceName Start SERVICE_AUTO_START  # starts on boot
& nssm set $ServiceName AppExit Default Restart    # auto-restart on crash
& nssm set $ServiceName AppRestartDelay 5000       # wait 5s before restart
& nssm set $ServiceName Description "Dalila — daily UAE-lens humanitarian / development digest bot"

# Run as the current user so claude-code's stored auth is inherited.
# (Default LocalSystem would not have access to ~/.claude credentials.)
$currentUser = "$env:USERDOMAIN\$env:USERNAME"
Write-Host "Service will run as: $currentUser"
Write-Host "You'll be prompted for this account's password (one-time)."
& nssm set $ServiceName ObjectName $currentUser

Write-Host ""
Write-Host "Starting service..."
& nssm start $ServiceName

Start-Sleep -Seconds 3
& nssm status $ServiceName
Write-Host ""
Write-Host "Done. Logs:"
Write-Host "  stdout: $LogDir\dalila-stdout.log"
Write-Host "  stderr: $LogDir\dalila-stderr.log"
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  nssm status  Dalila"
Write-Host "  nssm restart Dalila"
Write-Host "  nssm stop    Dalila"
Write-Host "  nssm remove  Dalila confirm   # uninstall"
