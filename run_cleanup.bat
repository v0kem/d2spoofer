@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

powershell.exe -NoProfile -Command "if (([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { exit 0 } else { exit 1 }" >nul 2>&1
if errorlevel 1 (
  echo Requesting administrator privileges...
  set "STEAM_CLEANUP_LAUNCHER=%~f0"
  set "STEAM_CLEANUP_DIRECTORY=%~dp0"
  powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath $env:STEAM_CLEANUP_LAUNCHER -Verb RunAs -WorkingDirectory $env:STEAM_CLEANUP_DIRECTORY"
  exit /b
)

if not exist "%~dp0config.json" (
  echo ERROR: config.json was not found next to this launcher.
  goto :failed
)

if exist "%~dp0SteamCleanupTool.exe" (
  "%~dp0SteamCleanupTool.exe" --config "%~dp0config.json"
  set "EXIT_CODE=!ERRORLEVEL!"
  goto :finished
)

if exist "%~dp0cleanup_tool.py" (
  where py.exe >nul 2>&1
  if not errorlevel 1 (
    py.exe -3 "%~dp0cleanup_tool.py" --config "%~dp0config.json"
    set "EXIT_CODE=!ERRORLEVEL!"
    goto :finished
  )

  where python.exe >nul 2>&1
  if not errorlevel 1 (
    python.exe "%~dp0cleanup_tool.py" --config "%~dp0config.json"
    set "EXIT_CODE=!ERRORLEVEL!"
    goto :finished
  )
)

echo ERROR: Neither SteamCleanupTool.exe nor Python 3 was found.
echo Download the Windows release or install Python 3.9 or newer.
goto :failed

:failed
set "EXIT_CODE=2"

:finished
echo.
if not "%EXIT_CODE%"=="0" echo Utility exited with code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
