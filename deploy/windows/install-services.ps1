#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Registers the desk Python services with NSSM.

.DESCRIPTION
    Each service runs the project virtualenv's python.exe directly, so `uv sync` must have
    been run first. Services run under the given Windows account rather than LocalSystem:
    the account needs read access to the repo and the uv-managed Python, and nothing more.

    Services start at boot, restart 5 seconds after a crash, and write rotated logs to
    logs\<service>.log in the repo (git-ignored). Re-running the script updates existing
    services in place.

.EXAMPLE
    .\deploy\windows\install-services.ps1
    Prompts for the Windows account password, then installs and starts every service.
#>
[CmdletBinding()]
param(
    [System.Management.Automation.PSCredential]$Credential = (Get-Credential -UserName "$env:USERDOMAIN\$env:USERNAME" -Message "Windows account the desk services run as"),
    [switch]$NoStart
)

$ErrorActionPreference = 'Stop'

# Service name -> python module. Later phases add the scheduler and collectors here.
$Services = [ordered]@{
    'desk-api' = 'desk.api'
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

$Password = $Credential.GetNetworkCredential().Password

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
    Invoke-Nssm set $Name ObjectName $Credential.UserName $Password
    Invoke-Nssm set $Name AppExit Default Restart
    Invoke-Nssm set $Name AppRestartDelay 5000
    # Console Ctrl+C first so uvicorn and workers shut down cleanly; hard kill after 15 s.
    Invoke-Nssm set $Name AppStopMethodConsole 15000
    Invoke-Nssm set $Name AppStdout $LogFile
    Invoke-Nssm set $Name AppStderr $LogFile
    Invoke-Nssm set $Name AppRotateFiles 1
    Invoke-Nssm set $Name AppRotateOnline 1
    Invoke-Nssm set $Name AppRotateBytes 10485760
    Invoke-Nssm set $Name AppEnvironmentExtra 'PYTHONUNBUFFERED=1'

    if (-not $NoStart) {
        Invoke-Nssm start $Name
        Write-Host "Started $Name (log: $LogFile)"
    }
}
