param(
    [string]$ModelUrl = "https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF/resolve/main/qwen2.5-coder-7b-instruct-q4_k_m.gguf?download=true",
    [string]$ModelPath = "models/qwen2.5-coder-7b-instruct-q4_k_m.gguf",
    [string]$ExpectedSha256 = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $scriptDir
$targetPath = Join-Path $projectRoot $ModelPath
$targetDir = Split-Path -Parent $targetPath
$tempPath = "$targetPath.download"

New-Item -ItemType Directory -Path $targetDir -Force | Out-Null

if ((Test-Path -LiteralPath $targetPath) -and -not $Force) {
    Write-Host "Model already exists: $targetPath"
    Write-Host "Use -Force to download it again."
    exit 0
}

if (Test-Path -LiteralPath $tempPath) {
    Remove-Item -LiteralPath $tempPath -Force
}

Write-Host "Downloading model to: $targetPath"
Write-Host "Source: $ModelUrl"

curl.exe -L --fail --progress-bar -o $tempPath $ModelUrl

$hash = (Get-FileHash -LiteralPath $tempPath -Algorithm SHA256).Hash.ToUpperInvariant()
if ($ExpectedSha256 -and $hash -ne $ExpectedSha256.ToUpperInvariant()) {
    Remove-Item -LiteralPath $tempPath -Force
    throw "SHA256 mismatch. Expected $ExpectedSha256, got $hash"
}

Move-Item -LiteralPath $tempPath -Destination $targetPath -Force

Write-Host "Model downloaded successfully."
Write-Host "Path: $targetPath"
Write-Host "SHA256: $hash"
