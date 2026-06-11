$ErrorActionPreference = "Stop"

$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$parentDir = Split-Path -Parent $appDir
$packageName = "YaMusicLyrics-DiscordStatus"
$stagingDir = Join-Path $env:TEMP ($packageName + "-package")
$zipPath = Join-Path $parentDir ($packageName + ".zip")

Remove-Item -LiteralPath $stagingDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $stagingDir | Out-Null
New-Item -ItemType Directory -Path (Join-Path $stagingDir "dist") | Out-Null

$rootFiles = @(
    "README.md",
    "requirements.txt",
    "config.example.json",
    "install_autostart.bat",
    "install_autostart.ps1",
    "uninstall_autostart.bat",
    "uninstall_autostart.ps1",
    "start_hidden.bat",
    "telegram_login.bat"
)

foreach ($name in $rootFiles) {
    $path = Join-Path $appDir $name
    if (Test-Path $path) {
        Copy-Item -LiteralPath $path -Destination $stagingDir -Force
    }
}

$distFiles = @(
    "YaMusicLyricsDiscordActivity.exe",
    "TelegramLogin.exe",
    "YatifySetup.exe"
)

foreach ($name in $distFiles) {
    $path = Join-Path (Join-Path $appDir "dist") $name
    if (Test-Path $path) {
        Copy-Item -LiteralPath $path -Destination (Join-Path $stagingDir "dist") -Force
    }
}

Compress-Archive -LiteralPath $stagingDir -DestinationPath $zipPath -Force
Remove-Item -LiteralPath $stagingDir -Recurse -Force

Write-Host "Package created:"
Write-Host $zipPath
