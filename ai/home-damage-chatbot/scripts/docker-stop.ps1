# Stop Zeo Damage Chatbot Docker container on Windows PowerShell
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent $ScriptDir

Set-Location $RootDir

Write-Host "=== Stopping Zeo Energy Service Chatbot Container ===" -ForegroundColor Cyan
docker compose down

Write-Host "✓ Chatbot container stopped." -ForegroundColor Green
