@echo off
title Central Telegram Broker Cleanup
echo =============================================================
echo          CLEANING UP DUPLICATE CENTRAL BROKER INSTANCES
echo =============================================================
echo.
echo Terminating any background central broker python scripts...
powershell -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'telemetry_broker.exe' -or ($_.Name -eq 'python.exe' -and $_.CommandLine -like '*telemetry_broker*') } | Remove-CimInstance"
echo.
echo Cleaning up telemetry session files...
del /f /q "%~dp0telemetry\*_*.json" 2>nul
echo.
echo All central broker processes and telemetry files cleaned successfully!
echo.
pause
