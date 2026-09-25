# Builds the dashboard on Windows and publishes it to the Beelink.
#
#   .\deploy\beelink\publish.ps1            build, copy the bundle, reload Caddy if needed
#   .\deploy\beelink\publish.ps1 -Setup     also (re)create the desk-caddy container
#
# Uses the SSH key already set up for the Beelink. The bundle is swapped in with a
# rename so a page load never sees half a copy.
param(
    [string]$HostName = "jkoutsourais@omalleys-alley",
    [switch]$Setup
)
$ErrorActionPreference = "Stop"
$repo = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$web = Join-Path $repo "web"

Push-Location $web
try {
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "npm run build failed" }
} finally {
    Pop-Location
}

$dist = Join-Path $web "dist"
ssh $HostName "mkdir -p ~/desk && rm -rf ~/desk/incoming"
if ($LASTEXITCODE -ne 0) { throw "ssh to $HostName failed" }
# Copies the folder itself (no wildcard), so it works the same from any shell.
scp -r -q "$dist" "${HostName}:~/desk/incoming"
if ($LASTEXITCODE -ne 0) { throw "copying the bundle failed" }
scp -q (Join-Path $PSScriptRoot "dashboard-up.sh") "${HostName}:~/desk/dashboard-up.sh"
if ($LASTEXITCODE -ne 0) { throw "copying dashboard-up.sh failed" }

# The site directory is bind-mounted into Caddy. New files are copied over the old ones and
# older hashed assets are kept for two weeks, so a page opened before this publish can
# still load the code it asks for.
ssh $HostName "mkdir -p ~/desk/site/assets && cp -r ~/desk/incoming/. ~/desk/site/ && find ~/desk/site/assets -type f -mtime +14 -delete && chmod +x ~/desk/dashboard-up.sh"
if ($LASTEXITCODE -ne 0) { throw "installing the bundle failed" }

if ($Setup) {
    ssh $HostName "~/desk/dashboard-up.sh"
    if ($LASTEXITCODE -ne 0) { throw "dashboard-up.sh failed" }
}
Write-Output "Published to $HostName"
