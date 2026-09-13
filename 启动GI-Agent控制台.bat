@echo off
rem =====================================================================
rem  GI Agent Console - portable launcher (double click me)
rem
rem  NOTE: this file is intentionally ASCII-only. cmd.exe parses .bat
rem  files using the OEM codepage, so Chinese text here would break the
rem  parser on machines whose console codepage is not UTF-8.
rem =====================================================================
setlocal
cd /d "%~dp0"

set "PYW=%~dp0venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=%~dp0venv\Scripts\python.exe"

if not exist "%PYW%" (
    echo [x] venv not found. Create it first:
    echo     python -m venv venv
    echo     venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

if not exist "%~dp0.env" (
    echo [!] .env missing - creating one from .env.example ...
    copy /y "%~dp0.env.example" "%~dp0.env" >nul
    echo     Open the console, go to the "Config" tab, and fill in your
    echo     LLM api key and Genshin UID before starting the Agent.
)

start "" "%PYW%" "%~dp0gui.py"
exit /b 0
