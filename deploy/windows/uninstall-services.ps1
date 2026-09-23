#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Stops and removes the desk services registered by install-services.ps1.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$ServiceNames = @('desk-api', 'desk-collectors', 'desk-scheduler')

$Nssm = (Get-Command nssm -ErrorAction SilentlyContinue).Source
if (-not $Nssm) { throw 'nssm not found on PATH. Install with: winget install NSSM.NSSM' }

foreach ($Name in $ServiceNames) {
    $Existing = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if (-not $Existing) {
        Write-Host "$Name is not installed"
        continue
    }
    if ($Existing.Status -ne 'Stopped') { & $Nssm stop $Name | Out-Null }
    & $Nssm remove $Name confirm | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "nssm remove $Name failed with exit code $LASTEXITCODE" }
    Write-Host "Removed $Name"
}
