@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

if not exist "%~dp0web_control_panel.py" (
    echo [installer] web_control_panel.py not found. Run this script from the project root.
    pause
    exit /b 1
)

if not exist "%~dp0installer\okx_ai_assistant.iss" (
    echo [installer] installer\okx_ai_assistant.iss not found.
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

set "STAGING_DIR=%~dp0release_staging\installer_payload"
set "OUTPUT_DIR=%~dp0output"
set "SETUP_NAME=!APP_SLUG!-Setup-v!APP_VERSION!"
set "SETUP_EXE=!OUTPUT_DIR!\!SETUP_NAME!.exe"

echo [installer] product : !APP_NAME!
echo [installer] version : !APP_VERSION!
echo [installer] date    : !BUILD_DATE!
echo [installer] bundle  : !SETUP_NAME!.exe
echo.

set "ISCC="
if defined ISCC_EXE if exist "%ISCC_EXE%" set "ISCC=%ISCC_EXE%"
if not defined ISCC if exist "D:\Programs\Inno Setup 6\ISCC.exe" set "ISCC=D:\Programs\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "%~dp0tools\Inno Setup 6\ISCC.exe" set "ISCC=%~dp0tools\Inno Setup 6\ISCC.exe"
if not defined ISCC (
    echo [installer] Inno Setup 6 not found.
    echo Checked: D:\Programs\Inno Setup 6
    echo          tools\Inno Setup 6 under project root
    echo          default Program Files locations
    echo Install from: https://jrsoftware.org/isdownload.php
    echo Or set ISCC_EXE to your ISCC.exe path, then rerun.
    pause
    exit /b 1
)
echo [installer] ISCC    : !ISCC!

call :ensure_app_icon
if !ERRORLEVEL! NEQ 0 (
    pause
    exit /b 1
)

if exist "%~dp0release_staging" rmdir /s /q "%~dp0release_staging"
mkdir "!STAGING_DIR!" 2>nul
mkdir "!OUTPUT_DIR!" 2>nul

call :copy_required "web_control_panel.py" || goto :fail
call :copy_required "okx_signal_monitor.py" || goto :fail
call :copy_required "monitor_config_summary.py" || goto :fail
call :copy_required "monitor_design_docs.py" || goto :fail
call :copy_required "runtime_identity.py" || goto :fail
call :copy_required "tray_launcher.py" || goto :fail
call :copy_required "launch_web_control_panel.vbs" || goto :fail
call :copy_required "setup_windows_runtime.bat" || goto :fail
call :copy_required "start_web_control_panel_windows.bat" || goto :fail
call :copy_required "restart_web_control_panel_windows.bat" || goto :fail

if exist "%~dp0config" (
    mkdir "!STAGING_DIR!\config" 2>nul
    if exist "%~dp0config\api_secrets.env.example" copy /y "%~dp0config\api_secrets.env.example" "!STAGING_DIR!\config\" >nul
    if exist "%~dp0config\trading_assistant_config.json" copy /y "%~dp0config\trading_assistant_config.json" "!STAGING_DIR!\config\" >nul
    if exist "%~dp0config\web_console_auth.default.json" copy /y "%~dp0config\web_console_auth.default.json" "!STAGING_DIR!\config\" >nul
)

if exist "%~dp0web_assets" (
    xcopy /e /i /y /q "%~dp0web_assets" "!STAGING_DIR!\web_assets\" >nul
) else (
    mkdir "!STAGING_DIR!\web_assets" 2>nul
)
if not exist "!STAGING_DIR!\web_assets\app.ico" (
    if exist "%~dp0web_assets\app.ico" copy /y "%~dp0web_assets\app.ico" "!STAGING_DIR!\web_assets\app.ico" >nul
)

call :write_readme "!STAGING_DIR!\README.txt"

echo [installer] compiling with Inno Setup...
"%ISCC%" /DMyAppVersion="!APP_VERSION!" /DMyAppName="!APP_NAME!" /DMyAppSlug="!APP_SLUG!" /DStagingDir="!STAGING_DIR!" "%~dp0installer\okx_ai_assistant.iss"
if !ERRORLEVEL! NEQ 0 (
    echo [installer] ISCC compile failed.
    pause
    exit /b 1
)

if not exist "!SETUP_EXE!" (
    echo [installer] expected output not found: !SETUP_EXE!
    pause
    exit /b 1
)

for %%F in ("!SETUP_EXE!") do set "SETUP_SIZE=%%~zF"
echo.
echo [installer] done
echo [installer] output: !SETUP_EXE!
echo [installer] size  : !SETUP_SIZE! bytes
echo.
echo End user flow:
echo   1. Run Setup.exe
echo   2. Follow wizard — runtime setup runs on "正在安装运行环境" page
echo   3. Launch from desktop shortcut or start menu
echo.

if exist "%~dp0release_staging" rmdir /s /q "%~dp0release_staging"
pause
exit /b 0

:fail
echo [installer] staging failed.
pause
exit /b 1

:copy_required
if not exist "%~dp0%~1" (
    echo [installer] missing required file: %~1
    exit /b 1
)
copy /y "%~dp0%~1" "!STAGING_DIR!\" >nul
exit /b 0

:write_readme
> "%~1" echo !APP_NAME! v!APP_VERSION!
>> "%~1" echo Release date: !BUILD_DATE!
>> "%~1" echo.
>> "%~1" echo Installed via Setup.exe — runtime is configured automatically on first install.
>> "%~1" echo Launch: desktop shortcut or launch_web_control_panel.vbs
>> "%~1" echo Web console: http://127.0.0.1:8765
>> "%~1" echo Default login: admin / admin123
exit /b 0

:ensure_app_icon
if exist "%~dp0web_assets\app_icon.png" (
    echo [installer] building app.ico from web_assets\app_icon.png...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\png_to_ico.ps1" -PngPath "%~dp0web_assets\app_icon.png" -IcoPath "%~dp0web_assets\app.ico"
    if !ERRORLEVEL! NEQ 0 (
        echo [installer] failed to convert app_icon.png to app.ico
        exit /b 1
    )
    if exist "%~dp0web_assets\app.ico" exit /b 0
)
if exist "%~dp0web_assets\app.ico" exit /b 0
echo [installer] web_assets\app.ico not found, generating default icon...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\generate_app_icon.ps1" -OutputPath "%~dp0web_assets\app.ico"
if !ERRORLEVEL! NEQ 0 (
    echo [installer] failed to generate app.ico
    exit /b 1
)
if not exist "%~dp0web_assets\app.ico" (
    echo [installer] failed to generate app.ico
    exit /b 1
)
exit /b 0
