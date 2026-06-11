@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
set "USE_PORTABLE=0"

call :find_python
if not defined PY (
    call :install_python
)

if not defined PY (
    echo.
    echo Python was installed, but this terminal cannot find it yet.
    echo Please close this window and run install_windows.bat again.
    pause
    exit /b 1
)

echo [install] using Python: %PY%
%PY% --version

if "%USE_PORTABLE%"=="1" (
    echo [install] portable Python mode, skip venv.
    set "INSTALL_PY=%PY%"
) else (
    echo [install] create venv
    %PY% -m venv .venv
    if %ERRORLEVEL% NEQ 0 (
        echo [install] Failed to create venv with current Python.
        echo [install] Try to install/repair Python 3.12 with winget...
        call :install_python
        if not defined PY (
            echo Failed to create venv. Please install Python 3.12 manually:
            echo https://www.python.org/downloads/windows/
            pause
            exit /b 1
        )
        if "%USE_PORTABLE%"=="1" (
            echo [install] portable Python mode, skip venv.
            set "INSTALL_PY=%PY%"
        ) else (
            echo [install] retry create venv with: %PY%
            %PY% -m venv .venv
            if %ERRORLEVEL% NEQ 0 (
                echo Failed to create venv again.
                echo Please disable Windows Store Python alias or install Python from python.org.
                echo Settings - Apps - Advanced app settings - App execution aliases - disable python.exe/python3.exe
                pause
                exit /b 1
            )
            set "INSTALL_PY=.venv\Scripts\python.exe"
        )
    ) else (
        set "INSTALL_PY=.venv\Scripts\python.exe"
    )
)

echo [install] upgrade pip
"%INSTALL_PY%" -m pip install --upgrade pip
if %ERRORLEVEL% NEQ 0 (
    echo Failed to upgrade pip.
    pause
    exit /b 1
)

echo [install] install full runtime dependencies
echo [install] install python-okx and openai
"%INSTALL_PY%" -m pip install python-okx openai
if %ERRORLEVEL% NEQ 0 (
    echo Failed to install full dependencies.
    pause
    exit /b 1
)

echo.
echo [install] done
echo Next:
echo   config_windows.bat
echo.
echo Web login default:
echo   user: admin
echo   password: admin123
echo You can change it in the web UI later.
pause
endlocal
exit /b 0

:find_python
set "PY="
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    py -3 -c "import sys, venv; raise SystemExit(0 if sys.version_info >= (3,8) else 1)" >nul 2>nul
    if %ERRORLEVEL% EQU 0 (
        set "PY=py -3"
        exit /b 0
    )
)
where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    python -c "import sys, venv; raise SystemExit(0 if sys.version_info >= (3,8) else 1)" >nul 2>nul
    if %ERRORLEVEL% EQU 0 (
        set "PY=python"
        exit /b 0
    )
)
if exist "%LocalAppData%\Programs\Python\Python312\python.exe" (
    "%LocalAppData%\Programs\Python\Python312\python.exe" -c "import sys, venv; raise SystemExit(0 if sys.version_info >= (3,8) else 1)" >nul 2>nul
    if %ERRORLEVEL% EQU 0 (
        set "PY=%LocalAppData%\Programs\Python\Python312\python.exe"
        exit /b 0
    )
)
if exist "%CD%\.python\python.exe" (
    "%CD%\.python\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,8) else 1)" >nul 2>nul
    if %ERRORLEVEL% EQU 0 (
        set "PY=%CD%\.python\python.exe"
        set "USE_PORTABLE=1"
        exit /b 0
    )
)
exit /b 0

:install_python
echo [install] Python 3.8+ with venv not found.
echo [install] Try to install Python 3.12 automatically with winget...
where winget >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Cannot find winget. Please install Python manually:
    echo https://www.python.org/downloads/windows/
    echo Remember to check "Add python.exe to PATH".
    set "PY="
    exit /b 0
)

winget install -e --id Python.Python.3.12 --source winget --scope user --accept-package-agreements --accept-source-agreements
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Python winget install failed. Try direct python.org installer...
    call :install_python_direct
    exit /b 0
)

set "PATH=%LocalAppData%\Programs\Python\Python312;%LocalAppData%\Programs\Python\Python312\Scripts;%LocalAppData%\Programs\Python\Launcher;%PATH%"
call :find_python
if not defined PY (
    echo [install] winget finished, but Python is still unavailable. Try direct python.org installer...
    call :install_python_direct
)
exit /b 0

:install_python_direct
set "PY="
set "PYTHON_URL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
set "PYTHON_INSTALLER=%TEMP%\python-3.12.10-amd64.exe"
set "PYTHON_TARGET=%LocalAppData%\Programs\Python\Python312"

echo [install] download Python installer:
echo %PYTHON_URL%
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%PYTHON_INSTALLER%' -UseBasicParsing } catch { Write-Host $_; exit 1 }"
if %ERRORLEVEL% NEQ 0 (
    echo Python direct download failed.
    echo Please install Python manually:
    echo https://www.python.org/downloads/windows/
    exit /b 0
)

echo [install] run Python installer for current user...
start /wait "" "%PYTHON_INSTALLER%" /quiet InstallAllUsers=0 TargetDir="%PYTHON_TARGET%" PrependPath=1 Include_pip=1 Include_launcher=1 Include_test=0
if %ERRORLEVEL% NEQ 0 (
    echo Python direct installer failed with exit code %ERRORLEVEL%.
    echo [install] This computer may block installers. Try portable Python zip...
    call :install_python_portable
    exit /b 0
)

set "PATH=%PYTHON_TARGET%;%PYTHON_TARGET%\Scripts;%LocalAppData%\Programs\Python\Launcher;%PATH%"
call :find_python
exit /b 0

:install_python_portable
set "PY="
set "PORTABLE_DIR=%CD%\.python"
set "PORTABLE_ZIP=%TEMP%\python-3.12.10-embed-amd64.zip"
set "PORTABLE_URL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
set "GETPIP=%TEMP%\get-pip.py"

echo [install] download portable Python:
echo %PORTABLE_URL%
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%PORTABLE_URL%' -OutFile '%PORTABLE_ZIP%' -UseBasicParsing } catch { Write-Host $_; exit 1 }"
if %ERRORLEVEL% NEQ 0 (
    echo Portable Python download failed.
    set "PY="
    exit /b 0
)

if exist "%PORTABLE_DIR%" rmdir /s /q "%PORTABLE_DIR%"
mkdir "%PORTABLE_DIR%"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Path '%PORTABLE_ZIP%' -DestinationPath '%PORTABLE_DIR%' -Force"
if %ERRORLEVEL% NEQ 0 (
    echo Portable Python unzip failed.
    set "PY="
    exit /b 0
)

echo [install] enable site-packages in portable Python
powershell -NoProfile -ExecutionPolicy Bypass -Command "$pth = Get-ChildItem '%PORTABLE_DIR%' -Filter 'python*._pth' | Select-Object -First 1; if ($pth) { (Get-Content $pth.FullName) -replace '#import site','import site' | Set-Content $pth.FullName -Encoding ASCII }"

echo [install] download get-pip.py
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%GETPIP%' -UseBasicParsing } catch { Write-Host $_; exit 1 }"
if %ERRORLEVEL% NEQ 0 (
    echo get-pip.py download failed.
    set "PY="
    exit /b 0
)

echo [install] install pip into portable Python
"%PORTABLE_DIR%\python.exe" "%GETPIP%"
if %ERRORLEVEL% NEQ 0 (
    echo get-pip failed.
    set "PY="
    exit /b 0
)

set "PY=%PORTABLE_DIR%\python.exe"
set "USE_PORTABLE=1"
exit /b 0
