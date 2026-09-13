@echo off
rem =====================================================================
rem  GI Agent QQ bot launcher (official QQ bot channel)
rem
rem  Starts the lightweight QQ channel: python main.py qq
rem  Needs QQ_BOT_APPID / QQ_BOT_SECRET in .env (see .env.example).
rem  ASCII-only on purpose: cmd.exe parses .bat with the OEM codepage.
rem =====================================================================
setlocal
cd /d "%~dp0"

if not exist "%~dp0.env" (
    echo [!] .env missing - copy .env.example to .env and fill in
    echo     QQ_BOT_APPID / QQ_BOT_SECRET / QQ_BOT_ALLOWED_USERS first.
    pause
    exit /b 1
)

set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

rem Keep emoji log lines from crashing the bot when output is redirected.
set "PYTHONIOENCODING=utf-8"

echo Starting QQ bot ... (close this window to stop it)
echo Tip: run  "%PY%" main.py qq --check  to test the AppID/Secret only.
echo.
"%PY%" "%~dp0main.py" qq
echo.
echo [x] QQ bot stopped (exit code %ERRORLEVEL%).
pause
