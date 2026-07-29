@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo Creating isolated build environment...
where py.exe >nul 2>&1
if not errorlevel 1 (
  py.exe -3 -m venv .build-venv
) else (
  where python.exe >nul 2>&1
  if errorlevel 1 (
    echo ERROR: Python 3.9 or newer is required to build the EXE.
    exit /b 2
  )
  python.exe -m venv .build-venv
)
if errorlevel 1 exit /b %ERRORLEVEL%

call ".build-venv\Scripts\activate.bat"
python.exe -m pip install --upgrade pip
if errorlevel 1 exit /b %ERRORLEVEL%
python.exe -m pip install -r requirements-build.txt
if errorlevel 1 exit /b %ERRORLEVEL%

python.exe -m PyInstaller --noconfirm --clean --onefile --console --name SteamCleanupTool cleanup_tool.py
if errorlevel 1 exit /b %ERRORLEVEL%

if exist "release" rmdir /s /q "release"
mkdir "release"
copy /y "dist\SteamCleanupTool.exe" "release\SteamCleanupTool.exe" >nul
copy /y "config.json" "release\config.json" >nul
copy /y "run_cleanup.bat" "release\run_cleanup.bat" >nul
copy /y "edit_config.bat" "release\edit_config.bat" >nul
copy /y "check_config.bat" "release\check_config.bat" >nul
copy /y "README.md" "release\README.md" >nul
copy /y "README_RU.md" "release\README_RU.md" >nul
copy /y "LICENSE" "release\LICENSE" >nul

if exist "SteamCleanupTool-Windows.zip" del /q "SteamCleanupTool-Windows.zip"
powershell.exe -NoProfile -Command "Compress-Archive -Path 'release\*' -DestinationPath 'SteamCleanupTool-Windows.zip' -Force"
if errorlevel 1 exit /b %ERRORLEVEL%

echo.
echo Build completed: %~dp0SteamCleanupTool-Windows.zip
pause
