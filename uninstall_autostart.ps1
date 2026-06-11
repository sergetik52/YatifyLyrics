$ErrorActionPreference = "SilentlyContinue"

$appDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$shortcutPath = Join-Path ([Environment]::GetFolderPath("Startup")) "YaMusicLyrics Discord Activity.lnk"
$venvPython = Join-Path $appDir ".venv\Scripts\python.exe"

if (Test-Path $venvPython) {
    @'
from pypresence import Presence
try:
    rpc = Presence("896771305108553788")
    rpc.connect()
    rpc.clear()
except Exception:
    pass
'@ | & $venvPython -u -
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

Remove-Item -LiteralPath $shortcutPath -Force
Write-Host "Stopped and removed from Windows startup."
