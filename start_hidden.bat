@echo off
setlocal
cd /d "%~dp0"
if exist "dist\YaMusicLyricsDiscordActivity.exe" (
    start "" "%~dp0dist\YaMusicLyricsDiscordActivity.exe"
    exit /b 0
)
if not exist ".venv\Scripts\pythonw.exe" (
    echo Run install_autostart.bat first.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0main.py"
