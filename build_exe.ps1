$ErrorActionPreference = "Stop"

$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $appDir

$proxyUrl = $env:PIP_PROXY
if ($proxyUrl) {
    $env:HTTP_PROXY = $proxyUrl
    $env:HTTPS_PROXY = $proxyUrl
}

$buildVenvPython = Join-Path $appDir ".buildvenv\Scripts\python.exe"
$exePath = Join-Path $appDir "dist\YaMusicLyricsDiscordActivity.exe"
$loginExePath = Join-Path $appDir "dist\TelegramLogin.exe"

Write-Host "Building YaMusicLyricsDiscordActivity.exe and TelegramLogin.exe..."

if (-not (Test-Path $buildVenvPython)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv ".buildvenv"
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv ".buildvenv"
    } else {
        throw "Python is not installed. Install Python 3.12+ first."
    }
}

$pipArgs = @("--timeout", "60", "--retries", "8")
if ($proxyUrl) {
    $pipArgs += @("--proxy", $proxyUrl)
}

& $buildVenvPython -m pip install @pipArgs --upgrade pip
& $buildVenvPython -m pip install @pipArgs -r "requirements.txt"
& $buildVenvPython -m pip install @pipArgs pyinstaller

Remove-Item -LiteralPath "build" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath "dist" -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath "YaMusicLyricsDiscordActivity.spec" -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath "TelegramLogin.spec" -Force -ErrorAction SilentlyContinue

& $buildVenvPython -m PyInstaller `
    --onefile `
    --noconsole `
    --name "YaMusicLyricsDiscordActivity" `
    --collect-all aiohttp `
    --collect-all pypresence `
    --collect-all yandex_music `
    --collect-all telethon `
    --collect-all winrt `
    --collect-all winrt.windows.foundation `
    --collect-all winrt.windows.foundation.collections `
    --collect-all winrt.windows.media.control `
    "main.py"

& $buildVenvPython -m PyInstaller `
    --onefile `
    --console `
    --name "YatifySetup" `
    "setup_installer.py"

& $buildVenvPython -m PyInstaller `
    --onefile `
    --console `
    --name "TelegramLogin" `
    --collect-all telethon `
    "telegram_login.py"

if (-not (Test-Path $exePath)) {
    throw "Build failed: $exePath was not created."
}
if (-not (Test-Path $loginExePath)) {
    throw "Build failed: $loginExePath was not created."
}
$setupExePath = Join-Path $appDir "dist\YatifySetup.exe"
if (-not (Test-Path $setupExePath)) {
    throw "Build failed: $setupExePath was not created."
}

Write-Host "Done:"
Write-Host $exePath
Write-Host $loginExePath
Write-Host $setupExePath
Write-Host "You can run install_autostart.bat now. It will use the exe automatically."
