@echo off
rem ============================================================
rem  JARVIS v4 - one-time setup (Step 00)
rem  Creates the folders JARVIS needs, then explains the checks.
rem ============================================================
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on this PC.
    echo Next: Install Python 3.11 or newer from python.org
    echo       - tick "Add Python to PATH" during setup -
    echo       then double-click setup.bat again.
    echo.
    pause
    exit /b 1
)

if not exist "data" mkdir "data"
if not exist "models" mkdir "models"

python -m jarvis.preflight --explain
echo.
echo Setup check finished. You can now double-click JARVIS.bat to start.
echo.
pause
