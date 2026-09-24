# Check status of Zeo Damage Chatbot Docker container on Windows PowerShell
$ErrorActionPreference = "Continue"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent $ScriptDir

Set-Location $RootDir

Write-Host "=== Container Process Status ===" -ForegroundColor Cyan
docker compose ps

Write-Host "`n=== Application Health Endpoint ===" -ForegroundColor Cyan
try {
    $resp = Invoke-RestMethod -Uri "http://localhost:8000/api/health" -Method Get -TimeoutSec 2
    $resp | ConvertTo-Json -Depth 4
} catch {
    Write-Host "Endpoint unreachable (container may be stopped or starting)" -ForegroundColor Yellow
}
