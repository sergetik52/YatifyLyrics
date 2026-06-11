$ErrorActionPreference = "Stop"

$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$parentDir = Split-Path -Parent $appDir
$packageName = "Yatify-DiscordActivity-source"
$stagingDir = Join-Path $env:TEMP ($packageName + "-package")
$zipPath = Join-Path $parentDir ($packageName + ".zip")

Remove-Item -LiteralPath $stagingDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $stagingDir | Out-Null

$excludeDirs = @(
    ".venv",
    ".buildvenv",
    "build",
    "dist",
    "logs",
    "__pycache__",
    ".git"
)

$excludeFiles = @(
    "config.json",
    "YaMusicLyricsDiscordActivity.spec",
    "TelegramLogin.spec",
    "YatifySetup.spec"
)

Get-ChildItem -LiteralPath $appDir -Force |
    Where-Object {
        ($_.PSIsContainer -and $_.Name -notin $excludeDirs) -or
        (-not $_.PSIsContainer -and $_.Name -notin $excludeFiles -and $_.Name -notlike "*.zip" -and $_.Name -notlike "*.log")
    } |
    ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $stagingDir -Recurse -Force
    }

Compress-Archive -LiteralPath $stagingDir -DestinationPath $zipPath -Force
Remove-Item -LiteralPath $stagingDir -Recurse -Force

Write-Host "Source package created:"
Write-Host $zipPath
