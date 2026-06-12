param(
    [string]$ImagePath = "$PSScriptRoot\Neighborhood.jpeg",
    [string]$Prompt = "What is in this image?",
    [string]$BaseUrl = "http://192.168.5.233:4001",
    [string]$ApiKey = "",
    [string]$Model = "qwen2.5-vl",
    [int]$MaxTokens = 512,
    [int]$MaxWidth = 800,
    [int]$MaxHeight = 600
)

# Load API key from .env if not provided
if (-not $ApiKey) {
    $envFile = Resolve-Path "$PSScriptRoot\..\..\\.env"
    $ApiKey = (Get-Content $envFile | Where-Object { $_ -match "^DEFAULT_LITELLM_MASTER_KEY=" }) -replace "^DEFAULT_LITELLM_MASTER_KEY=", ""
    if (-not $ApiKey) { Write-Error "DEFAULT_LITELLM_MASTER_KEY not found in .env"; exit 1 }
}

# Resize image and encode as base64
Add-Type -AssemblyName System.Drawing

$img = [System.Drawing.Image]::FromFile($ImagePath)
$ratio = [Math]::Min($MaxWidth / $img.Width, $MaxHeight / $img.Height)
$newW = [int]($img.Width * $ratio)
$newH = [int]($img.Height * $ratio)

$bmp = New-Object System.Drawing.Bitmap($newW, $newH)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.DrawImage($img, 0, 0, $newW, $newH)
$g.Dispose()
$img.Dispose()

$ms = New-Object System.IO.MemoryStream
$bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Jpeg)
$bmp.Dispose()
$b64 = [Convert]::ToBase64String($ms.ToArray())
$ms.Dispose()

Write-Host "Sending $([Math]::Round((($b64.Length * 3) / 4) / 1KB, 1)) KB image (resized to ${newW}x${newH}) to $Model..."

# Build and send request
$tmpFile = "$env:TEMP\vl_test_request.json"
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
$json = "{`"model`":`"$Model`",`"messages`":[{`"role`":`"user`",`"content`":[{`"type`":`"image_url`",`"image_url`":{`"url`":`"data:image/jpeg;base64,$b64`"}},{`"type`":`"text`",`"text`":`"$Prompt`"}]}],`"max_tokens`":$MaxTokens}"
[System.IO.File]::WriteAllText($tmpFile, $json, $utf8NoBom)

$response = curl.exe -s -X POST "$BaseUrl/v1/chat/completions" `
    -H "Authorization: Bearer $ApiKey" `
    -H "Content-Type: application/json" `
    --data-binary "@$tmpFile"

$response | ConvertFrom-Json | Select-Object -ExpandProperty choices | ForEach-Object {
    Write-Host "`nResponse:" -ForegroundColor Green
    Write-Host $_.message.content
}
