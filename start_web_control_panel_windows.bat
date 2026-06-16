@echo off

setlocal enabledelayedexpansion

cd /d "%~dp0"

if defined OKX_LAUNCH_SILENT set "OKX_LAUNCH_NO_PAUSE=1"



set "PY="



if exist ".venv\Scripts\python.exe" (

    set "PY=.venv\Scripts\python.exe"

) else if exist ".python\python.exe" (

    set "PY=.python\python.exe"

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

    call :launch_maybe_pause

    exit /b 1

)



if not exist "%~dp0web_control_panel.py" (

    echo [config] web_control_panel.py not found.

    call :launch_maybe_pause

    exit /b 1

)



if defined OKX_LAUNCH_SILENT (
    if exist "%~dp0.venv\Scripts\pythonw.exe" (
        set "PY=%~dp0.venv\Scripts\pythonw.exe"
    ) else if /I "!PY:~-10!"=="python.exe" (
        set "PYW=!PY:python.exe=pythonw.exe!"
        if exist "!PYW!" set "PY=!PYW!"
    )
)



if not defined OKX_LAUNCH_SILENT (

    echo [config] starting browser config UI...

    echo [config] using Python: %PY%

    %PY% --version

    echo [config] open http://127.0.0.1:8765 if the browser does not open automatically.

    echo [config] press Ctrl+C in this window to stop.

    echo.

)



%PY% "%~dp0web_control_panel.py"

set "EXIT_CODE=%ERRORLEVEL%"

if "%EXIT_CODE%"=="130" (

    echo.

    echo [config] stopped by Ctrl+C.

    call :launch_maybe_pause

)

if not "%EXIT_CODE%"=="0" if not "%EXIT_CODE%"=="130" (

    echo.

    echo [config] config web exited with error: %EXIT_CODE%

    echo Please check the error message above.

    call :launch_maybe_pause

)

endlocal & set "EXIT_CODE=%EXIT_CODE%"

if "%EXIT_CODE%"=="0" exit 0

if "%EXIT_CODE%"=="130" exit /b 0

exit /b %EXIT_CODE%

:launch_maybe_pause
if defined OKX_LAUNCH_NO_PAUSE exit /b 0
pause
exit /b 0


