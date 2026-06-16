@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist "%~dp0web_control_panel.py" (
    echo [release] web_control_panel.py not found. Run this script from the project root.
    pause
    exit /b 1
)

set "APP_NAME=OKX AI Assistant"
set "APP_VERSION=0.0.0"
for /f "usebackq tokens=1,* delims==" %%A in (`findstr /B /C:"APP_NAME = " "%~dp0web_control_panel.py"`) do set "APP_NAME=%%B"
for /f "usebackq tokens=1,* delims==" %%A in (`findstr /B /C:"APP_VERSION = " "%~dp0web_control_panel.py"`) do set "APP_VERSION=%%B"
set "APP_NAME=!APP_NAME:"=!"
set "APP_VERSION=!APP_VERSION:"=!"
if "!APP_NAME:~0,1!"==" " set "APP_NAME=!APP_NAME:~1!"
if "!APP_VERSION:~0,1!"==" " set "APP_VERSION=!APP_VERSION:~1!"
set "APP_SLUG=!APP_NAME: =-!"

for /f "delims=" %%D in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Date -Format yyyyMMdd"') do set "BUILD_DATE=%%D"
if not defined BUILD_DATE set "BUILD_DATE=00000000"
if not defined APP_SLUG set "APP_SLUG=OKX-AI-Assistant"

set "RELEASE_NAME=!APP_SLUG!_!APP_VERSION!_!BUILD_DATE!"
set "STAGING_ROOT=%~dp0release_staging"
set "STAGING_DIR=!STAGING_ROOT!\!RELEASE_NAME!"
set "OUTPUT_DIR=%~dp0output"
set "ZIP_PATH=!OUTPUT_DIR!\!RELEASE_NAME!.zip"

echo [release] product : !APP_NAME!
echo [release] version : !APP_VERSION!
echo [release] date    : !BUILD_DATE!
echo [release] bundle  : !RELEASE_NAME!.zip
echo.

if exist "!STAGING_ROOT!" rmdir /s /q "!STAGING_ROOT!"
mkdir "!STAGING_DIR!" 2>nul
mkdir "!OUTPUT_DIR!" 2>nul

call :copy_required "web_control_panel.py"
call :copy_required "okx_signal_monitor.py"
call :copy_required "tray_launcher.py"
call :copy_required "launch_web_control_panel.vbs"
call :copy_required "setup_windows_runtime.bat"
call :copy_required "start_web_control_panel_windows.bat"
call :copy_required "launch_web_control_panel.vbs"
call :copy_required "restart_web_control_panel_windows.bat"

if exist "%~dp0config" (
    mkdir "!STAGING_DIR!\config" 2>nul
    if exist "%~dp0config\api_secrets.env.example" copy /y "%~dp0config\api_secrets.env.example" "!STAGING_DIR!\config\" >nul
    if exist "%~dp0config\trading_assistant_config.json" copy /y "%~dp0config\trading_assistant_config.json" "!STAGING_DIR!\config\" >nul
    if exist "%~dp0config\web_console_auth.default.json" copy /y "%~dp0config\web_console_auth.default.json" "!STAGING_DIR!\config\" >nul
)

if exist "%~dp0web_assets" (
    xcopy /e /i /y /q "%~dp0web_assets" "!STAGING_DIR!\web_assets\" >nul
)

call :write_readme "!STAGING_DIR!\README.txt"

if exist "!ZIP_PATH!" del /f /q "!ZIP_PATH!"

powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Compress-Archive -Path '!STAGING_DIR!' -DestinationPath '!ZIP_PATH!' -Force; exit 0 } catch { Write-Host $_; exit 1 }"
if !ERRORLEVEL! NEQ 0 (
    echo [release] Failed to create zip: !ZIP_PATH!
    pause
    exit /b 1
)

for %%F in ("!ZIP_PATH!") do set "ZIP_SIZE=%%~zF"
echo.
echo [release] done
echo [release] output: !ZIP_PATH!
echo [release] size  : !ZIP_SIZE! bytes
echo.
echo After extract:
echo   1. setup_windows_runtime.bat
echo   2. launch_web_control_panel.vbs (or start_web_control_panel_windows.bat)
echo.

if exist "!STAGING_ROOT!" rmdir /s /q "!STAGING_ROOT!"
pause
exit /b 0

:copy_required
if not exist "%~dp0%~1" (
    echo [release] missing required file: %~1
    pause
    exit /b 1
)
copy /y "%~dp0%~1" "!STAGING_DIR!\" >nul
exit /b 0

:write_readme
> "%~1" echo !APP_NAME! v!APP_VERSION!
>> "%~1" echo Release date: !BUILD_DATE!
>> "%~1" echo.
>> "%~1" echo Quick start (Windows):
>> "%~1" echo   1. setup_windows_runtime.bat
>> "%~1" echo   2. launch_web_control_panel.vbs (or start_web_control_panel_windows.bat)
>> "%~1" echo.
>> "%~1" echo Web console: http://127.0.0.1:8765
>> "%~1" echo Default login: admin / admin123
exit /b 0
