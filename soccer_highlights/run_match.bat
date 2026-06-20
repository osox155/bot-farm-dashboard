@echo off
REM run_match.bat — double-click this in the RDP to run a full match session.
REM Provisions OBS (if needed), opens OBS, then runs the orchestrator.

cd /d "%~dp0"

echo === Soccer SaaS: match session ===

REM 1. Provision OBS config (idempotent) using env vars / secrets
powershell -ExecutionPolicy Bypass -File "%~dp0setup_obs.ps1"

REM 2. Launch OBS so the WebSocket server comes up (background)
set "OBS=%ProgramFiles%\obs-studio\bin\64bit\obs64.exe"
if exist "%OBS%" (
  start "" /D "%ProgramFiles%\obs-studio\bin\64bit" "%OBS%" --minimize-to-tray --disable-shutdown-check
) else (
  echo OBS not found at "%OBS%" — open OBS manually, then continue.
)

REM give OBS a few seconds to start its WebSocket server
timeout /t 8 /nobreak >nul

REM 3. Run the orchestrator (go live -> ENTER at full time -> produce -> publish)
python "%~dp0run_match.py"

echo.
echo Session finished. Outputs are in %%USERPROFILE%%\Downloads\matches\
pause
