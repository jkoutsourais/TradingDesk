#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Registers the desk Python services with NSSM.

.DESCRIPTION
    Each service runs the project virtualenv's python.exe directly, so `uv sync` must have
    been run first. Services run as LocalSystem, which needs no account password. That
    account has full rights on the machine, which is the accepted trade-off for this
    single-user workstation.

    desk-tunnel keeps an SSH reverse tunnel open from the Beelink's loopback port
    $TunnelRemotePort to the API on 127.0.0.1:$ApiPort, so the dashboard reaches the API
    without opening any inbound port here. Windows OpenSSH only accepts a private key owned
    by the account using it, so the installer copies the installing user's key and
    known_hosts to C:\ProgramData\desk\ssh, owned by SYSTEM and readable only by SYSTEM
    and Administrators.

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
    [switch]$NoStart,
    # SSH target of the Beelink that serves the dashboard (key-based login must work).
    [string]$TunnelTarget = 'jkoutsourais@omalleys-alley',
    [int]$TunnelRemotePort = 18010,
    [int]$ApiPort = 8000
)

$ErrorActionPreference = 'Stop'

# Service name -> python module. Later phases add the desk workers here.
$Services = [ordered]@{
    'desk-api' = 'desk.api'
    'desk-collectors' = 'desk.collectors'
    'desk-scheduler' = 'desk.watch.scheduler'
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

# The account running this script (elevated, but the same user) gets start, stop, query
# and status rights on the desk services only, so code deploys can restart them from a
# normal shell. No other admin rights are granted.
$ControllerSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value

function Grant-ServiceControl {
    param([string]$Name, [string]$Sid)
    # RP start, WP stop, DT pause/continue, LO query status, CR user control, RC read.
    $Ace = "(A;;RPWPDTLOCRRC;;;$Sid)"
    $Current = (& sc.exe sdshow $Name | Where-Object { $_ -match '^D:' } | Select-Object -First 1).Trim()
    if (-not $Current) { throw "could not read the security descriptor of $Name" }
    if ($Current.Contains($Ace)) { return }
    $Updated = $Current -replace '^D:', "D:$Ace"
    & sc.exe sdset $Name $Updated | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "sc sdset $Name failed with exit code $LASTEXITCODE" }
    Write-Host "Granted service control on $Name to the installing account"
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
    Grant-ServiceControl -Name $Name -Sid $ControllerSid

    if (-not $NoStart) {
        Invoke-Nssm start $Name
        Write-Host "Started $Name (log: $LogFile)"
    }
}

# --- SSH reverse tunnel to the Beelink -----------------------------------------------------

$Ssh = (Get-Command ssh -ErrorAction SilentlyContinue).Source
if (-not $Ssh) { throw 'ssh not found on PATH. Install the Windows OpenSSH client.' }
$SshDir = Join-Path $env:USERPROFILE '.ssh'
$UserKey = Join-Path $SshDir 'id_ed25519'
if (-not (Test-Path $UserKey)) { throw "No SSH key at $UserKey; set up key login to $TunnelTarget first." }
$ServiceSshDir = Join-Path $env:ProgramData 'desk\ssh'
New-Item -ItemType Directory -Force -Path $ServiceSshDir | Out-Null
$Key = Join-Path $ServiceSshDir 'id_ed25519'
$KnownHosts = Join-Path $ServiceSshDir 'known_hosts'
Copy-Item $UserKey $Key -Force
Copy-Item (Join-Path $SshDir 'known_hosts') $KnownHosts -Force
foreach ($Path in @($ServiceSshDir, $Key, $KnownHosts)) {
    & icacls $Path /inheritance:r /grant:r 'SYSTEM:(F)' 'Administrators:(F)' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls on $Path failed" }
    & icacls $Path /setowner 'SYSTEM' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "setting the owner of $Path failed" }
}
$TunnelArgs = @(
    '-N',
    '-i', "`"$Key`"",
    '-o', "UserKnownHostsFile=`"$KnownHosts`"",
    '-o', 'BatchMode=yes',
    '-o', 'ServerAliveInterval=30',
    '-o', 'ServerAliveCountMax=3',
    '-o', 'ExitOnForwardFailure=yes',
    '-R', "127.0.0.1:${TunnelRemotePort}:127.0.0.1:${ApiPort}",
    $TunnelTarget
) -join ' '

$Name = 'desk-tunnel'
$Existing = Get-Service -Name $Name -ErrorAction SilentlyContinue
if ($Existing) {
    Write-Host "Updating $Name"
    if ($Existing.Status -ne 'Stopped') { Invoke-Nssm stop $Name }
    Invoke-Nssm set $Name Application $Ssh
} else {
    Write-Host "Installing $Name"
    Invoke-Nssm install $Name $Ssh
}
$LogFile = Join-Path $LogDir "$Name.log"
Invoke-Nssm set $Name AppParameters $TunnelArgs
Invoke-Nssm set $Name DisplayName "Desk: $Name"
Invoke-Nssm set $Name Start SERVICE_AUTO_START
Invoke-Nssm set $Name ObjectName LocalSystem
Invoke-Nssm set $Name AppExit Default Restart
# A dropped connection or a busy remote port retries after 10 s.
Invoke-Nssm set $Name AppRestartDelay 10000
Invoke-Nssm set $Name AppStdout $LogFile
Invoke-Nssm set $Name AppStderr $LogFile
Invoke-Nssm set $Name AppRotateFiles 1
Invoke-Nssm set $Name AppRotateOnline 0
Invoke-Nssm set $Name AppRotateBytes 1048576
Grant-ServiceControl -Name $Name -Sid $ControllerSid

# Logon tasks that held the tunnel before this service (the development dashboard and the
# interim user-level tunnel) would hold the same remote port.
foreach ($TaskName in @('desk-dev-dashboard', 'desk-tunnel-user')) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed the $TaskName logon task"
    }
}
Get-CimInstance Win32_Process -Filter "Name = 'ssh.exe'" |
    Where-Object { $_.CommandLine -like "*${TunnelRemotePort}:127.0.0.1*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

if (-not $NoStart) {
    Invoke-Nssm start $Name
    Write-Host "Started $Name (log: $LogFile)"
}
