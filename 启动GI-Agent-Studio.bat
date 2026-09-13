@echo off
rem =====================================================================
rem  GI Agent Studio launcher (native window)
rem
rem  Prefers GI-Agent-Studio.exe == a real WinForms + WebView2 window.
rem  Falls back to the browser app-window mode if the exe is missing.
rem  ASCII-only on purpose: cmd.exe parses .bat with the OEM codepage.
rem =====================================================================
setlocal
cd /d "%~dp0"

if not exist "%~dp0.env" (
    echo [!] .env missing - creating one from .env.example ...
    copy /y "%~dp0.env.example" "%~dp0.env" >nul
    echo     Open the console, go to the "Config" tab, and fill in your
    echo     LLM api key and Genshin UID before starting the Agent.
)

if exist "%~dp0GI-Agent-Studio.exe" (
    start "" "%~dp0GI-Agent-Studio.exe"
    exit /b 0
)

set "PYW=%~dp0venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=%~dp0venv\Scripts\python.exe"

if not exist "%PYW%" (
    echo [x] Neither GI-Agent-Studio.exe nor venv was found.
    echo     Build the exe:  powershell -ExecutionPolicy Bypass -File scripts\build_studio_exe.ps1
    echo     Or create venv:  python -m venv venv
    echo                      venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

start "" "%PYW%" "%~dp0app_web.py"
exit /b 0
