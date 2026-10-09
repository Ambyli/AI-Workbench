<#
.SYNOPSIS
  Start the Service page mock without Docker, with the account check gapped.

.DESCRIPTION
  Three local processes, like the production layout:
  * account service on 127.0.0.1:8100: the ONLY process with CRM access and the
    handoff delivery. Loads .env.account if present (no model key).
  * chatbot API on 127.0.0.1:8000: the real model from .env, REQUIRE_ACCOUNT_GAP=true.
    It refuses to start if it could reach CRM or delivery secrets, and only learns
    "found / not found" from the account service.
  * site server on http://localhost:8080: production website build; forwards only
    the 4 public chatbot routes.
  Mock-safe overrides everywhere: no email or chat sends, staff API off, mock
  accounts, 3 active chats. A fresh random service key is generated on every start.

.PARAMETER Rebuild  Rebuild the website even if a build already exists (after code changes).
.PARAMETER StandIn  Use the offline stand-in model instead of the real Qwen gateway.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\mock-start.ps1
#>
param([switch]$Rebuild, [switch]$StandIn, [int]$Port = 8080, [int]$ApiPort = 8000, [int]$AccountPort = 8100)
$ErrorActionPreference = "Stop"

$Root    = Split-Path -Parent $PSScriptRoot
$Mock    = Join-Path $Root "mock"
$Site    = Join-Path $Root "New-Zeo-Website"
$Build   = Join-Path $Mock ".site-build"
$Logs    = Join-Path $Mock "logs"
$BotData = Join-Path $Mock ".data\chatbot"
$AcctData = Join-Path $Mock ".data\account"
$PidFile = Join-Path $Mock ".pids.json"
New-Item -ItemType Directory -Force $Logs, $BotData, $AcctData, (Join-Path $BotData "uploads") | Out-Null

Write-Host "=== Zeo Service page mock (no Docker, account check gapped) ===" -ForegroundColor Cyan
if (Test-Path $PidFile) { & (Join-Path $PSScriptRoot "mock-stop.ps1") -Quiet }
if (-not (Test-Path (Join-Path $Root ".env"))) { throw ".env not found. Copy .env.example to .env and set VLLM_API_KEY." }
if (-not (Test-Path $Site)) { throw "Website folder not found: $Site" }

# ---- 1. Website production build ------------------------------------------------
if ($Rebuild -or -not (Test-Path (Join-Path $Build "index.html"))) {
    Write-Host "Building the Service page (production build, about a minute)..."
    if (-not (Test-Path (Join-Path $Site "node_modules"))) {
        Push-Location $Site
        try { npx -y pnpm@9 install --no-frozen-lockfile; git checkout -- pnpm-lock.yaml 2>$null } finally { Pop-Location }
    }
    $env:VITE_SERVICE_MOCK = "1"          # service-only router
    $env:VITE_FORM_API_URL = "/mock-form" # classic form -> local sink, never the real Cloud Function
    Push-Location $Site
    try {
        npx vite build --outDir $Build --emptyOutDir --logLevel warn
        if ($LASTEXITCODE -ne 0) { throw "Website build failed (exit $LASTEXITCODE)." }
    } finally {
        Pop-Location
        Remove-Item Env:VITE_SERVICE_MOCK, Env:VITE_FORM_API_URL -ErrorAction SilentlyContinue
    }
    $leak = Select-String -Path (Join-Path $Build "assets\*.js") -Pattern "zeo-send-email" -List -ErrorAction SilentlyContinue
    if ($leak) { throw "Safety check failed: the build still points at the production form endpoint." }
    Write-Host "Build OK (classic form goes to the local sink)." -ForegroundColor Green
} else {
    Write-Host "Using the existing website build (pass -Rebuild after website changes)."
}

# ---- helpers ----------------------------------------------------------------------
function Start-WithEnv([hashtable]$Vars, [string[]]$PyArgs, [string]$LogName) {
    $saved = @{}
    foreach ($k in $Vars.Keys) { $saved[$k] = [Environment]::GetEnvironmentVariable($k, "Process"); Set-Item "Env:$k" $Vars[$k] }
    try {
        return Start-Process python -PassThru -WindowStyle Hidden -WorkingDirectory $Root -ArgumentList $PyArgs `
            -RedirectStandardOutput (Join-Path $Logs "$LogName.out.log") -RedirectStandardError (Join-Path $Logs "$LogName.log")
    } finally {
        foreach ($k in $saved.Keys) {
            if ($null -eq $saved[$k]) { Remove-Item "Env:$k" -ErrorAction SilentlyContinue } else { Set-Item "Env:$k" $saved[$k] }
        }
    }
}
function Wait-Http([string]$Url, [int]$Seconds = 30) {
    for ($i = 0; $i -lt $Seconds; $i++) {
        try { Invoke-RestMethod $Url -TimeoutSec 3 | Out-Null; return "ok" }
        catch {
            if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -eq 503) { return "503" }
            Start-Sleep -Seconds 1
        }
    }
    return $null
}

$serviceKey = [Convert]::ToBase64String((1..32 | ForEach-Object { Get-Random -Maximum 256 }) -as [byte[]]).TrimEnd("=")
$accountEnvFile = Join-Path $Root ".env.account"
if (-not (Test-Path $accountEnvFile)) { $accountEnvFile = Join-Path $Mock ".no-account-env" ; Set-Content -Path $accountEnvFile -Value "" }

# ---- 2. Account service (CRM + delivery; no model key) --------------------------
$acct = Start-WithEnv -LogName "account" -PyArgs @("-m", "uvicorn", "backend.account_service:app", "--host", "127.0.0.1", "--port", "$AccountPort") -Vars @{
    ZEO_ENV_FILE        = $accountEnvFile
    ACCOUNT_SERVICE_KEY = $serviceKey
    CRM_BACKEND         = "stub"           # mock accounts only
    EMAIL_SEND_ENABLED  = "false"
    CHAT_SEND_ENABLED   = "false"
    DATA_DIR            = $AcctData
    UPLOAD_DIR          = (Join-Path $BotData "uploads")
}
if (-not (Wait-Http "http://127.0.0.1:$AccountPort/health" 20)) {
    Write-Host "Account service did not start. See: scripts\mock-logs.ps1" -ForegroundColor Red
    Stop-Process -Id $acct.Id -Force -ErrorAction SilentlyContinue
    exit 1
}

# ---- 3. Chatbot API (gapped) ------------------------------------------------------
$botVars = @{
    ACCOUNT_GATEWAY     = "service"
    ACCOUNT_SERVICE_URL = "http://127.0.0.1:$AccountPort"
    ACCOUNT_SERVICE_KEY = $serviceKey
    REQUIRE_ACCOUNT_GAP = "true"
    ENABLE_STAFF_API    = "false"
    EMAIL_SEND_ENABLED  = "false"
    CHAT_SEND_ENABLED   = "false"
    MAX_ACTIVE_USERS    = "3"
    MAX_QUEUE_SIZE      = "10"
    ACTIVE_SESSION_TIMEOUT_SECONDS = "150"  # page heartbeats every 45s; closed tabs free their slot fast
    LLM_REQUIRED        = "true"
    USE_MOCK_LLM        = $(if ($StandIn) { "1" } else { "0" })
    DATA_DIR            = $BotData
    CORS_ALLOW_ORIGINS  = "http://localhost:$Port,http://127.0.0.1:$Port"
    FORWARDED_ALLOW_IPS = "127.0.0.1"
}
$api = Start-WithEnv -LogName "api" -Vars $botVars -PyArgs @("-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "$ApiPort", "--proxy-headers")

# ---- 4. Site server ---------------------------------------------------------------
$site = Start-WithEnv -LogName "site" -Vars @{ MOCK_API_URL = "http://127.0.0.1:$ApiPort" } `
    -PyArgs @("mock\site_server.py", "--port", "$Port", "--api", "http://127.0.0.1:$ApiPort")

@{ api = $api.Id; site = $site.Id; account = $acct.Id; port = $Port; apiPort = $ApiPort; accountPort = $AccountPort; standIn = [bool]$StandIn } |
    ConvertTo-Json | Set-Content -Encoding utf8 $PidFile

# ---- 5. Checks ---------------------------------------------------------------------
$saved = @{}
foreach ($k in @("USE_MOCK_LLM")) { $saved[$k] = $env:USE_MOCK_LLM; $env:USE_MOCK_LLM = $botVars.USE_MOCK_LLM }
$modelLine = & python (Join-Path $Mock "check_model.py") 2>&1 | Select-Object -Last 1
if ($null -eq $saved.USE_MOCK_LLM) { Remove-Item Env:USE_MOCK_LLM -ErrorAction SilentlyContinue } else { $env:USE_MOCK_LLM = $saved.USE_MOCK_LLM }
$health = Wait-Http "http://127.0.0.1:$Port/api/health" 30

Write-Host ""
Write-Host $modelLine
if ($null -eq $health) {
    Write-Host "Mock did not come up (if the chatbot refused to start, the gap check failed). See: scripts\mock-logs.ps1" -ForegroundColor Red
    exit 1
} elseif ($health -eq "503") {
    Write-Host "Assistant is UNAVAILABLE (model refused or unreachable). The page will switch customers to the classic form." -ForegroundColor Yellow
    Write-Host "Fix the model access, or start with -StandIn to demo on the offline model."
} else {
    Write-Host "Assistant is available. Account check is gapped (chatbot has no CRM access)." -ForegroundColor Green
}
Write-Host ""
Write-Host "  Open:      http://localhost:$Port" -ForegroundColor Cyan
Write-Host "  Accounts:  docs\MOCK_ACCOUNTS.md (what to type, which message to expect)"
Write-Host "  Inbox:     scripts\mock-inbox.ps1   (which account matched, per submitted request)"
Write-Host "  Status:    scripts\mock-status.ps1   Logs: scripts\mock-logs.ps1   Stop: scripts\mock-stop.ps1"
