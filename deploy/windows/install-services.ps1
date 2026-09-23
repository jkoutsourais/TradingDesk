#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Registers the desk Python services with NSSM.

.DESCRIPTION
    Each service runs the project virtualenv's python.exe directly, so `uv sync` must have
    been run first. Services run as LocalSystem, which needs no account password. That
    account has full rights on the machine, which is the accepted trade-off for this
    single-user workstation.

    Services start at boot, restart 5 seconds after a crash, and write rotated logs to
    logs\<service>.log in the repo (git-ignored). Docker Desktop and Ollama only start
    after a user signs in, so until then the services fail to reach Postgres and keep
    restarting; auto-login removes that window. Re-running the script updates existing
    services in place.

.EXAMPLE
    .\deploy\windows\install-services.ps1
#>
[CmdletBinding()]
param(
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'

# Service name -> python module. Later phases add the scheduler and desk workers here.
$Services = [ordered]@{
    'desk-api' = 'desk.api'
    'desk-collectors' = 'desk.collectors'
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$LogDir = Join-Path $RepoRoot 'logs'

$Nssm = (Get-Command nssm -ErrorAction SilentlyContinue).Source
if (-not $Nssm) { throw 'nssm not found on PATH. Install with: winget install NSSM.NSSM' }
if (-not (Test-Path $Python)) { throw "Virtualenv python not found at $Python. Run 'uv sync' first." }
if (-not (Test-Path (Join-Path $RepoRoot '.env'))) { throw "No .env in $RepoRoot. Copy .env.example and fill it in." }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Invoke-Nssm {
    param([Parameter(ValueFromRemainingArguments)][string[]]$Arguments)
    & $Nssm @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "nssm $($Arguments[0..1] -join ' ') failed with exit code $LASTEXITCODE" }
}

foreach ($Name in $Services.Keys) {
    $Module = $Services[$Name]
    $Existing = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if ($Existing) {
        Write-Host "Updating $Name"
        if ($Existing.Status -ne 'Stopped') { Invoke-Nssm stop $Name }
        Invoke-Nssm set $Name Application $Python
    } else {
        Write-Host "Installing $Name"
        Invoke-Nssm install $Name $Python
    }

    $LogFile = Join-Path $LogDir "$Name.log"
    Invoke-Nssm set $Name AppParameters "-m $Module"
    Invoke-Nssm set $Name AppDirectory $RepoRoot
    Invoke-Nssm set $Name DisplayName "Desk: $Name"
    Invoke-Nssm set $Name Start SERVICE_AUTO_START
    Invoke-Nssm set $Name ObjectName LocalSystem
    Invoke-Nssm set $Name AppExit Default Restart
    Invoke-Nssm set $Name AppRestartDelay 5000
    # Console Ctrl+C first so uvicorn and workers shut down cleanly; hard kill after 15 s.
    Invoke-Nssm set $Name AppStopMethodConsole 15000
    Invoke-Nssm set $Name AppStdout $LogFile
    Invoke-Nssm set $Name AppStderr $LogFile
    Invoke-Nssm set $Name AppRotateFiles 1
    # Online rotation keeps a log-pipe thread in NSSM that can leave the service stuck in
    # STOP_PENDING after the app exits; rotation at service start is enough here.
    Invoke-Nssm set $Name AppRotateOnline 0
    Invoke-Nssm set $Name AppRotateBytes 10485760
    Invoke-Nssm set $Name AppEnvironmentExtra 'PYTHONUNBUFFERED=1'

    if (-not $NoStart) {
        Invoke-Nssm start $Name
        Write-Host "Started $Name (log: $LogFile)"
    }
}
