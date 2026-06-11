@echo off
cd /d "%~dp0"
if not "%TELEGRAM_PROXY%"=="" (
  set HTTP_PROXY=%TELEGRAM_PROXY%
  set HTTPS_PROXY=%TELEGRAM_PROXY%
)

if exist "dist\TelegramLogin.exe" (
  "dist\TelegramLogin.exe"
  pause
  exit /b
)

if not exist ".venv\Scripts\python.exe" (
  where py >nul 2>nul
  if %errorlevel%==0 (
    py -3 -m venv ".venv"
  ) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
      python -m venv ".venv"
    ) else (
      echo Python is not installed and dist\TelegramLogin.exe was not found.
      echo Use the packaged zip that includes dist\TelegramLogin.exe.
      pause
      exit /b 1
    )
  )
)
if "%PIP_PROXY%"=="" (
  ".venv\Scripts\python.exe" -m pip install --timeout 60 --retries 8 -r requirements.txt
) else (
  ".venv\Scripts\python.exe" -m pip install --proxy "%PIP_PROXY%" --timeout 60 --retries 8 -r requirements.txt
)
".venv\Scripts\python.exe" telegram_login.py
pause
