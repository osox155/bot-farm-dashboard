@echo off
REM run_match.bat — double-click this in the RDP to run a full match session.
REM Provisions OBS (if needed), opens OBS, starts watcher, then runs the orchestrator.

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

REM 3. Ask for match name (so watcher can forward it to n8n / match_pipeline)
set /p MATCH_QUERY=Match name (e.g. "Arsenal vs Chelsea 2026-06-20"):

REM 4. Start the folder-watcher in the background.
REM    It will fire the n8n webhook when OBS finishes writing the recording.
if defined N8N_WEBHOOK (
  echo Starting watcher [webhook: %N8N_WEBHOOK%] ...
  start /B "" python "%~dp0watcher.py" --match-query "%MATCH_QUERY%"
) else (
  echo N8N_WEBHOOK not set — watcher will print recording path but not POST to n8n.
  start /B "" python "%~dp0watcher.py" --match-query "%MATCH_QUERY%"
)

REM 5. Run the orchestrator (go live -> ENTER at full time -> produce -> publish)
python "%~dp0run_match.py"

echo.
echo Session finished. Outputs are in %USERPROFILE%\Downloads\matches\
pause
