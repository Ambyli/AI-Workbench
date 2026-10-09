<#
.SYNOPSIS  Show recent mock logs. -Follow keeps streaming the chatbot API log.
#>
param([int]$Lines = 40, [switch]$Follow)
$Logs = Join-Path (Split-Path -Parent $PSScriptRoot) "mock\logs"
foreach ($f in @("api.log", "account.log", "site.log")) {
    $path = Join-Path $Logs $f
    Write-Host "=== $f ===" -ForegroundColor Cyan
    if (Test-Path $path) { Get-Content $path -Tail $Lines } else { Write-Host "(none yet)" }
}
if ($Follow) { Get-Content (Join-Path $Logs "api.log") -Tail 0 -Wait }
