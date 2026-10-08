@echo off
title TaskHub Server
cd /d "%~dp0.."
set "ROOT=%CD%"
set "TASKHUB_ADMIN_TOKEN=taskhub"
set "PORT=8787"
set "PYBASE=C:/Users/Administrator/.workbuddy/binaries/python/versions/3.13.12/python.exe"
if not exist "%PYBASE%" set "PYBASE=python"

echo.
echo   ============================================
echo       TaskHub - Task Dispatch Console
echo   ============================================
echo.

for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":%PORT%" ^| findstr LISTENING') do (
    echo   [SKIP] Port %PORT% already in use by PID %%p, opening browser.
    start "" "http://127.0.0.1:%PORT%"
    exit /b 0
)

if not exist ".venv\Scripts\python.exe" (
    echo   [1/3] Creating virtual environment...
    "%PYBASE%" -m venv .venv
    if errorlevel 1 (
        echo   [FAIL] Cannot create venv.
        pause
        exit /b 1
    )
) else (
    echo   [1/3] Virtual environment ready.
)

"%ROOT%\.venv\Scripts\python.exe" -c "import uvicorn, fastapi, multipart, requests" 2>nul
if errorlevel 1 (
    echo   [2/3] Installing dependencies, first run takes a while...
    "%ROOT%\.venv\Scripts\python.exe" -m pip install -r requirements.txt -q --disable-pip-version-check
    if errorlevel 1 (
        echo   [FAIL] Dependency install failed, check network.
        pause
        exit /b 1
    )
) else (
    echo   [2/3] Dependencies already installed.
)

echo   [3/3] Starting server...
echo.
echo       Console  : http://127.0.0.1:%PORT%
echo       Token    : %TASKHUB_ADMIN_TOKEN%
echo       Stop     : press Ctrl+C in this window
echo.

start "" "http://127.0.0.1:%PORT%"
"%ROOT%\.venv\Scripts\python.exe" -m uvicorn server.app:app --host 0.0.0.0 --port %PORT%

echo.
echo   Server stopped.
pause
