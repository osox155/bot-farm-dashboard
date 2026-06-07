@echo off
setlocal ENABLEEXTENSIONS
set SCRIPT_DIR=%~dp0

REM Run the PowerShell launcher without creating any log files
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%start-bots.ps1"
set ERR=%ERRORLEVEL%

if not "%ERR%"=="0" (
  echo Launcher ended with error code %ERR%.
  pause
)

endlocal & exit /b %ERR%
