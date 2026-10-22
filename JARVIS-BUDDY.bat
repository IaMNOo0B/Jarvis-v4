@echo off
rem ============================================================
rem  JARVIS v4 - buddy overlay
rem  A small always-on-top window showing what JARVIS is
rem  doing. The main UI (JARVIS-UI.bat) must be running;
rem  double-click the buddy to open it. No console stays open.
rem ============================================================
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
  start "" pythonw -m jarvis.overlay
) else (
  start "" python -m jarvis.overlay
)
