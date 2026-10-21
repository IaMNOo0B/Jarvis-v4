@echo off
rem ============================================================
rem  JARVIS v4 - app UI launcher
rem  Checks everything is in place, then opens the JARVIS app
rem  (ring + chat) in your browser. Same brain as JARVIS.bat.
rem ============================================================
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on this PC.
    echo Next: Install Python 3.11 or newer from python.org
    echo       - tick "Add Python to PATH" during setup -
    echo       then double-click JARVIS-UI.bat again.
    echo.
    pause
    exit /b 1
)

python -m jarvis.preflight
if errorlevel 1 (
    echo.
    echo JARVIS cannot start yet. Please follow the "Next:" step above,
    echo then double-click JARVIS-UI.bat again.
    echo.
    pause
    exit /b 1
)

python -m jarvis.ui
echo.
pause
