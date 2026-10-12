@echo off
REM ---------------------------------------------------------------------------
REM start.bat - double-click launcher for tickflow-stock-panel.
REM
REM Why: this machine's PowerShell execution policy is "Restricted", so running
REM start.ps1 directly fails with "running scripts is disabled". This wrapper
REM bypasses that policy for THIS ONE RUN only (no system setting is changed)
REM and passes any arguments straight through to start.ps1.
REM
REM Usage:
REM   double-click start.bat        (backend on 3018, serves frontend/dist)
REM   start.bat -Dev                (also start the Vite dev server on 3011)
REM   start.bat -BackendPort 8080
REM ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo [start.bat] start.ps1 exited with code %RC%.
  pause
)

endlocal
