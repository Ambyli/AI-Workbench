<#
.SYNOPSIS  Show what staff would receive for recent mock submissions (which account matched).
#>
param([int]$Count = 10)
$Root = Split-Path -Parent $PSScriptRoot
& python (Join-Path $Root "mock\inbox.py") $Count
