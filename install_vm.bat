@echo off
:: CS2 Farm — VM Setup Script
:: Run as Administrator on each VM
:: Installs Python 3.11 + Node.js + project dependencies

setlocal EnableDelayedExpansion
title CS2 Farm VM Setup

echo =========================================
echo  CS2 Farm — VM Setup
echo =========================================
echo.

:: --- Check if already installed ---
where python >nul 2>&1
if %errorlevel%==0 (
    echo [OK] Python already installed:
    python --version
    goto :install_node
)

:: --- Download Python 3.11 installer ---
echo [1/4] Downloading Python 3.11...
set PYTHON_URL=https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
set PYTHON_INSTALLER=%TEMP%\python-3.11.9-amd64.exe

powershell -Command "Invoke-WebRequest -Uri '%PYTHON_URL%' -OutFile '%PYTHON_INSTALLER%' -UseBasicParsing"
if not exist "%PYTHON_INSTALLER%" (
    echo [ERROR] Failed to download Python. Check internet connection.
    pause & exit /b 1
)

:: Install silently, add to PATH
echo [2/4] Installing Python 3.11 (silent)...
"%PYTHON_INSTALLER%" /quiet InstallAllUsers=1 PrependPath=1 Include_pip=1 Include_test=0
if errorlevel 1 (
    echo [ERROR] Python installation failed.
    pause & exit /b 1
)

:: Refresh PATH in current session
set "PATH=C:\Program Files\Python311;C:\Program Files\Python311\Scripts;%PATH%"
echo [OK] Python installed.

:install_node
:: --- Check Node.js ---
where node >nul 2>&1
if %errorlevel%==0 (
    echo [OK] Node.js already installed:
    node --version
    goto :install_deps
)

echo [3/4] Downloading Node.js 20 LTS...
set NODE_URL=https://nodejs.org/dist/v20.18.3/node-v20.18.3-x64.msi
set NODE_INSTALLER=%TEMP%\node-v20.18.3-x64.msi

powershell -Command "Invoke-WebRequest -Uri '%NODE_URL%' -OutFile '%NODE_INSTALLER%' -UseBasicParsing"
if not exist "%NODE_INSTALLER%" (
    echo [ERROR] Failed to download Node.js.
    pause & exit /b 1
)

echo Installing Node.js (silent)...
msiexec /i "%NODE_INSTALLER%" /quiet /norestart
set "PATH=C:\Program Files\nodejs;%PATH%"
echo [OK] Node.js installed.

:install_deps
:: --- Install Python packages ---
echo [4/4] Installing Python packages...
python -m pip install --upgrade pip --quiet
python -m pip install requests --quiet
echo [OK] requests installed.

:: pyautoit — опционально, если рядом есть AutoIt DLL
if exist "C:\CS2_FARM\autoit\lib\AutoItX3.dll" (
    python -m pip install pyautoit --quiet
    echo [OK] pyautoit installed.
) else if exist "C:\CS2_FARM\autoit\lib\AutoItX3_x64.dll" (
    python -m pip install pyautoit --quiet
    echo [OK] pyautoit installed.
) else (
    echo [SKIP] pyautoit skipped (autoit\lib\AutoItX3*.dll не найден — vm_agent грузит DLL через ctypes)
)

:: --- npm install for Node.js scripts ---
echo.
echo Installing Node.js packages...
if exist "C:\CS2_FARM\package.json" (
    cd /d "C:\CS2_FARM"
    call npm install --silent
    echo [OK] npm packages installed.
) else (
    echo [WARN] Скопируйте содержимое папки _client из репозитория в C:\CS2_FARM (с package.json).
)

echo.
echo =========================================
echo  Setup complete!
echo.
echo  Next: start the VM agent:
echo    python C:\CS2_FARM\vm_agent.py
echo    (файлы из репозитория: папка _client\)
echo    (or double-click run_agent.bat)
echo =========================================
pause
