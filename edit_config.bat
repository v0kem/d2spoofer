@echo off
setlocal
cd /d "%~dp0"

if not exist "%~dp0config.json" (
  echo ERROR: config.json was not found.
  pause
  exit /b 2
)

start "Steam Cleanup configuration" notepad.exe "%~dp0config.json"
