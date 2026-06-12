@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PY="

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else if exist "build\python_runtime\python.exe" (
    set "PY=build\python_runtime\python.exe"
)

if not defined PY (
    where py >nul 2>nul
    if !ERRORLEVEL! EQU 0 (
        py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,8) else 1)" >nul 2>nul
        if !ERRORLEVEL! EQU 0 set "PY=py -3"
    )
)

if not defined PY (
    where python >nul 2>nul
    if !ERRORLEVEL! EQU 0 (
        python -c "import sys; raise SystemExit(0 if sys.version_info >= (3,8) else 1)" >nul 2>nul
        if !ERRORLEVEL! EQU 0 set "PY=python"
    )
)

if not defined PY (
    echo [config] Python 3.8+ not found.
    echo Please run setup_windows_runtime.bat first.
    pause
    exit /b 1
)

if not exist "%~dp0web_control_panel.py" (
    echo [config] web_control_panel.py not found.
    pause
    exit /b 1
)

if not exist "%~dp0build\local_state\trading_assistant_config.json" (
    echo [config] build\local_state\trading_assistant_config.json not found.
    pause
    exit /b 1
)

echo [config] starting browser config UI...
echo [config] using Python: %PY%
%PY% --version
echo [config] open http://127.0.0.1:8765 if the browser does not open automatically.
echo.

%PY% "%~dp0web_control_panel.py"
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [config] config web exited with error: %ERRORLEVEL%
    echo Please check the error message above.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo [config] config web stopped.
pause
endlocal
