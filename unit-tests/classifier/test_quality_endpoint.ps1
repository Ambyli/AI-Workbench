param(
    [string]$ImagePath = "$PSScriptRoot\Neighborhood.jpeg",
    [string]$Criteria = '[{"name":"document legibility","type":"quality"},{"name":"image sharpness","type":"quality"},{"name":"proper exposure","type":"quality"},{"name":"absence of artifacts","type":"quality"}]',
    [string]$BaseUrl = "http://192.168.5.233:4001",
    [string]$ApiKey = "",
    [int]$MaxWaitSeconds = 300,
    [int]$PollIntervalSeconds = 3
)

# Load API key from .env if not provided
if (-not $ApiKey) {
    $envFile = Resolve-Path "$PSScriptRoot\..\..\\.env"
    $ApiKey = (Get-Content $envFile | Where-Object { $_ -match "^DEFAULT_LITELLM_MASTER_KEY=" }) -replace "^DEFAULT_LITELLM_MASTER_KEY=", ""
    if (-not $ApiKey) { Write-Error "DEFAULT_LITELLM_MASTER_KEY not found in .env"; exit 1 }
}

# Submit job
$sizeKB = [Math]::Round((Get-Item $ImagePath).Length / 1KB, 1)
Write-Host "Submitting ${sizeKB} KB image to classifier..."

$submitRaw = curl.exe -s -X POST "$BaseUrl/v1/classifier/assess" `
    -H "Authorization: Bearer $ApiKey" `
    -F "image=@$ImagePath" `
    -F "criteria=$Criteria"

$submit = $submitRaw | ConvertFrom-Json
$jobId = $submit.job_id

if (-not $jobId) {
    Write-Error "Failed to get job_id from response: $submitRaw"
    exit 1
}

Write-Host "Job submitted: $jobId" -ForegroundColor Cyan

# Poll until complete or failed
$elapsed = 0
$finalStatus = $null

while ($elapsed -lt $MaxWaitSeconds) {
    Start-Sleep -Seconds $PollIntervalSeconds
    $elapsed += $PollIntervalSeconds

    $statusRaw = curl.exe -s "$BaseUrl/v1/classifier/jobs/$jobId" `
        -H "Authorization: Bearer $ApiKey"

    $statusObj = $statusRaw | ConvertFrom-Json
    $status = $statusObj.status

    Write-Host "  [${elapsed}s] $status"

    if ($status -eq "completed" -or $status -eq "failed") {
        $finalStatus = $status
        break
    }
}

if (-not $finalStatus) {
    Write-Warning "Job did not complete within ${MaxWaitSeconds}s (last status: $status)"
    exit 1
}

# Show result
Write-Host ""
if ($finalStatus -eq "completed") {
    Write-Host "Result:" -ForegroundColor Green
    $statusObj.result | ConvertTo-Json -Depth 15
} else {
    Write-Host "Job failed:" -ForegroundColor Red
    Write-Host $statusObj.error
    exit 1
}
