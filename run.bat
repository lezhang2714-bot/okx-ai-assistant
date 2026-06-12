@echo off

cd /d "%~dp0"

if not exist "logs" mkdir "logs"

if exist ".env" (
    for /f "eol=# tokens=1,2 delims==" %%a in (.env) do (
        set "%%a=%%b"
    )
)

set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"

for /f "delims=" %%i in ('"%PYTHON%" "_parse_args.py"') do set "ARGS=%%i"

"%PYTHON%" "monitor.py" %ARGS%
