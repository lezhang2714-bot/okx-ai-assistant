@echo off
cd /d "%~dp0"

rem Wait for the previous web control panel to release the port.
ping 127.0.0.1 -n 4 >nul

set "OKX_WEB_SKIP_BROWSER=1"

if not exist "%~dp0start_web_control_panel_windows.bat" (
    echo [config] start_web_control_panel_windows.bat not found.
    pause
    exit /b 1
)

rem Single cmd layer avoids nested "call" + Ctrl+C batch termination issues.
cmd /c ""%~dp0start_web_control_panel_windows.bat""
exit /b %ERRORLEVEL%
