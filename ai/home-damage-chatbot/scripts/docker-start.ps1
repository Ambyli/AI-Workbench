# Start Zeo Damage Chatbot Docker container on Windows PowerShell
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent $ScriptDir

Set-Location $RootDir

Write-Host "=== Starting Zeo Energy Service Chatbot Container ===" -ForegroundColor Cyan
if (-not (Test-Path .env)) {
    Write-Host "No .env found; copying .env.example" -ForegroundColor Yellow
    Copy-Item .env.example .env
}

docker compose up -d --build chatbot

Write-Host "`nWaiting for healthcheck on http://localhost:8000..." -ForegroundColor Cyan
$healthy = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $resp = Invoke-RestMethod -Uri "http://localhost:8000/api/health" -Method Get -TimeoutSec 2 -ErrorAction SilentlyContinue
        if ($resp.ok) {
            $healthy = $true
            break
        }
    } catch {}
}

if ($healthy) {
    Write-Host "✓ Chatbot is healthy and listening on http://localhost:8000" -ForegroundColor Green
} else {
    Write-Host "Chatbot container started. Check logs with: docker compose logs -f chatbot" -ForegroundColor Yellow
}
