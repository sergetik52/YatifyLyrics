$ErrorActionPreference = "Stop"

$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $appDir

$venvPython = Join-Path $appDir ".venv\Scripts\python.exe"
$venvPythonw = Join-Path $appDir ".venv\Scripts\pythonw.exe"
$mainPy = Join-Path $appDir "main.py"
$exePath = Join-Path $appDir "dist\YaMusicLyricsDiscordActivity.exe"
$shortcutName = "YaMusicLyrics Discord Activity.lnk"
$startupDir = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupDir $shortcutName
$proxyUrl = $env:PIP_PROXY
if ($proxyUrl) {
    $env:HTTP_PROXY = $proxyUrl
    $env:HTTPS_PROXY = $proxyUrl
}

Write-Host "Installing YaMusicLyrics Discord Activity..."

if (Test-Path $exePath) {
    Write-Host "Found built exe, Python dependencies are not needed."
} else {
    if (-not (Test-Path $venvPython)) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            & py -3 -m venv ".venv"
        } elseif (Get-Command python -ErrorAction SilentlyContinue) {
            & python -m venv ".venv"
        } else {
            throw "Python is not installed. Install Python 3.12+ first."
        }
    }

    $depsOk = $false
    try {
        & $venvPython -c "import aiohttp; import pypresence; import winrt.windows.media.control" | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $depsOk = $true
        }
    } catch {
        $depsOk = $false
    }

    if ($depsOk) {
        Write-Host "Dependencies already installed, skipping pip."
    } else {
        Write-Host "Installing dependencies..."
        $pipArgs = @("--timeout", "60", "--retries", "8")
        if ($proxyUrl) {
            $pipArgs += @("--proxy", $proxyUrl)
        }
        & $venvPython -m pip install @pipArgs -r "requirements.txt"
    }
}

Get-CimInstance Win32_Process |
    Where-Object {
        (
            ($_.Name -in @("python.exe", "pythonw.exe", "python3.13.exe")) -and
            ($_.CommandLine -like ("*" + $appDir + "*main.py*"))
        ) -or (
            ($_.Name -eq "YaMusicLyricsDiscordActivity.exe") -and
            ($_.ExecutablePath -like (Join-Path $appDir "*"))
        )
    } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force
    }

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
if (Test-Path $exePath) {
    $shortcut.TargetPath = $exePath
    $shortcut.Arguments = ""
    $shortcut.IconLocation = $exePath
} else {
    $shortcut.TargetPath = $venvPythonw
    $shortcut.Arguments = "`"$mainPy`""
    $shortcut.IconLocation = $venvPythonw
}
$shortcut.WorkingDirectory = $appDir
$shortcut.Description = "Yandex Music lyrics in Discord Activity"
$shortcut.Save()

if (Test-Path $exePath) {
    Start-Process -FilePath $exePath -WorkingDirectory $appDir -WindowStyle Hidden
} else {
    Start-Process -FilePath $venvPythonw -ArgumentList "`"$mainPy`"" -WorkingDirectory $appDir -WindowStyle Hidden
}

Write-Host "Done."
Write-Host "It is running hidden now and will start automatically with Windows."
Write-Host "Logs: $appDir\logs\app.log"
