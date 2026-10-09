<#
.SYNOPSIS  Stop the Service page mock (site server, chatbot API and account service).
#>
param([switch]$Quiet)
$Root    = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $Root "mock\.pids.json"

if (-not (Test-Path $PidFile)) {
    if (-not $Quiet) { Write-Host "Mock is not running (no mock\.pids.json)." }
    exit 0
}
$pids = Get-Content $PidFile -Raw | ConvertFrom-Json
foreach ($name in @("site", "api", "account")) {
    $id = $pids.$name
    if ($id) {
        $p = Get-Process -Id $id -ErrorAction SilentlyContinue
        if ($p) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue; if (-not $Quiet) { Write-Host "Stopped $name (pid $id)" } }
    }
}
Remove-Item $PidFile -ErrorAction SilentlyContinue
if (-not $Quiet) { Write-Host "Mock stopped." -ForegroundColor Green }
