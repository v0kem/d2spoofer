@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

if not exist "%~dp0config.json" (
  echo ERROR: config.json was not found.
  set "EXIT_CODE=2"
  goto :finished
)

if exist "%~dp0SteamCleanupTool.exe" (
  "%~dp0SteamCleanupTool.exe" --config "%~dp0config.json" --check-config
  set "EXIT_CODE=!ERRORLEVEL!"
  goto :finished
)

where py.exe >nul 2>&1
if not errorlevel 1 (
  py.exe -3 "%~dp0cleanup_tool.py" --config "%~dp0config.json" --check-config
  set "EXIT_CODE=!ERRORLEVEL!"
  goto :finished
)

where python.exe >nul 2>&1
if not errorlevel 1 (
  python.exe "%~dp0cleanup_tool.py" --config "%~dp0config.json" --check-config
  set "EXIT_CODE=!ERRORLEVEL!"
  goto :finished
)

echo ERROR: Neither SteamCleanupTool.exe nor Python 3 was found.
set "EXIT_CODE=2"

:finished
echo.
pause
exit /b %EXIT_CODE%
