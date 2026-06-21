@echo off
REM start.bat — Launch Yuan Knowledge Base workspace console (Windows)
REM Starts a local static file server and opens paper-ui in the default browser.

setlocal

REM EVOLVEKB_PORT / EVOLVEKB_AGENT: legacy compatibility env vars, kept as-is
set PORT=8741
if defined EVOLVEKB_PORT set PORT=%EVOLVEKB_PORT%

where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Please install Python 3.8+.
    pause
    exit /b 1
)

set CONSOLE_URL=http://localhost:%PORT%/paper-ui/index.html
cd /d "%~dp0"

echo Starting Yuan Knowledge Base server on http://localhost:%PORT% ...
echo.
echo Yuan Knowledge Base is starting...
echo   Console:  %CONSOLE_URL%
echo   Server:   http://localhost:%PORT%
echo   Stop:     Ctrl+C or close this window
echo.

REM Open browser after a short delay (in background)
start /b cmd /c "timeout /t 2 /nobreak >nul & start "" "%CONSOLE_URL%""

if not defined EVOLVEKB_AGENT set EVOLVEKB_AGENT=codex

REM Run server in FOREGROUND so the window stays alive
python -u server.py --port %PORT% --bind 127.0.0.1 --agent %EVOLVEKB_AGENT%
