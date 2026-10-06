<#
.SYNOPSIS  Show whether the Service page mock is running, its health, and which model it uses.
#>
$Root    = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $Root "mock\.pids.json"
if (-not (Test-Path $PidFile)) { Write-Host "Mock is not running. Start it: scripts\mock-start.ps1"; exit 0 }

$pids = Get-Content $PidFile -Raw | ConvertFrom-Json
foreach ($name in @("account", "api", "site")) {
    $alive = [bool](Get-Process -Id $pids.$name -ErrorAction SilentlyContinue)
    Write-Host ("{0,-8} pid {1,-7} {2}" -f $name, $pids.$name, $(if ($alive) { "running" } else { "STOPPED" }))
}
try {
    $h = Invoke-RestMethod "http://127.0.0.1:$($pids.port)/api/health" -TimeoutSec 5
    Write-Host "Health: ok=$($h.ok) assistant_available=$($h.assistant_available)"
} catch {
    Write-Host "Health: unavailable ($($_.Exception.Message))" -ForegroundColor Yellow
}
Write-Host ("Model:  " + $(if ($pids.standIn) { "offline stand-in (-StandIn)" } else { "real model from .env (check: python mock\check_model.py)" }))
Write-Host "Gap:    chatbot uses the account service on 127.0.0.1:$($pids.accountPort) (no CRM access of its own)"
Write-Host "Open:   http://localhost:$($pids.port)"
