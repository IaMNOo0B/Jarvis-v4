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

rem Voice pack (optional, never blocks setup): microphone/speaker
rem for the live voice, plus the local 'Jarvis' wake word.
echo Installing the voice pack (sounddevice + openwakeword)...
python -m pip install --user sounddevice openwakeword >nul 2>nul
if errorlevel 1 echo Voice pack could not be installed now - JARVIS still works in text; run setup.bat again later for voice.
echo.

python -m jarvis.preflight --explain
echo.
echo Setup check finished. You can now double-click JARVIS.bat to start.
echo.
pause
