<#
.SYNOPSIS
    Temporary remote dev dashboard (workstation side). Removed with the Phase 9 dashboard.

.DESCRIPTION
    Runs, under the signed-in user with no admin rights:
      1. A second API process on 127.0.0.1:8010 that serves /dev and /dev/status.
      2. An SSH reverse tunnel exposing it on the Beelink as 127.0.0.1:18010.
    The tunnel is outbound from the workstation, so no firewall rule is needed and the API
    is never reachable on the LAN. Each loop restarts its process if it exits.
    Logs go to logs\dev-api.log and logs\dev-tunnel.log (git-ignored).

.PARAMETER Target
    SSH destination for the Beelink.

.PARAMETER InterimCollectors
    Collectors to run here until the desk-collectors service is restarted with the code
    that includes them (restarting a LocalSystem service needs an admin shell). Set to ''
    once the service runs them, or both processes will collect the same data.
#>
[CmdletBinding()]
param(
    [string]$Target = 'jkoutsourais@omalleys-alley',
    [int]$LocalPort = 8010,
    [int]$RemotePort = 18010,
    [string]$InterimCollectors = ''
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$LogDir = Join-Path $RepoRoot 'logs'
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$apiLoop = {
    param($RepoRoot, $Python, $LogDir, $LocalPort)
    $env:API_HOST = '127.0.0.1'
    $env:API_PORT = "$LocalPort"
    Set-Location $RepoRoot
    while ($true) {
        & $Python -m desk.api 2>&1 | Out-File -Append -Encoding utf8 (Join-Path $LogDir 'dev-api.log')
        Start-Sleep -Seconds 5
    }
}

$tunnelLoop = {
    param($Target, $LocalPort, $RemotePort, $LogDir)
    while ($true) {
        "$(Get-Date -Format s) connecting tunnel" | Out-File -Append -Encoding utf8 (Join-Path $LogDir 'dev-tunnel.log')
        & ssh -N -o BatchMode=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 `
            -o ExitOnForwardFailure=yes `
            -R "127.0.0.1:${RemotePort}:127.0.0.1:${LocalPort}" $Target 2>&1 | Out-File -Append -Encoding utf8 (Join-Path $LogDir 'dev-tunnel.log')
        Start-Sleep -Seconds 10
    }
}

$collectorLoop = {
    param($RepoRoot, $Python, $LogDir, $Names)
    Set-Location $RepoRoot
    while ($true) {
        & $Python -m desk.collectors --only $Names 2>&1 | Out-File -Append -Encoding utf8 (Join-Path $LogDir 'dev-collectors.log')
        Start-Sleep -Seconds 10
    }
}

$jobs = @(
    Start-Job -Name 'desk-dev-api' -ScriptBlock $apiLoop -ArgumentList $RepoRoot, $Python, $LogDir, $LocalPort
    Start-Job -Name 'desk-dev-tunnel' -ScriptBlock $tunnelLoop -ArgumentList $Target, $LocalPort, $RemotePort, $LogDir
)
if ($InterimCollectors) {
    $jobs += Start-Job -Name 'desk-dev-collectors' -ScriptBlock $collectorLoop -ArgumentList $RepoRoot, $Python, $LogDir, $InterimCollectors
}
Write-Host "running jobs: $(($jobs | ForEach-Object Name) -join ', '); Ctrl+C to stop"
try {
    Wait-Job -Job $jobs | Out-Null
} finally {
    Stop-Job -Job $jobs
    Remove-Job -Job $jobs -Force
}
