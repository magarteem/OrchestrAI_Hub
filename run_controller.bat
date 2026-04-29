@echo off
:: CS2 Farm 2v2 — Controller launcher
:: Automatically finds Python and starts the GUI

setlocal

:: Try common Python locations
set PYTHON=
if exist "C:\Users\%USERNAME%\AppData\Local\Python\bin\python.exe" (
    set PYTHON=C:\Users\%USERNAME%\AppData\Local\Python\bin\python.exe
) else if exist "C:\Python311\python.exe" (
    set PYTHON=C:\Python311\python.exe
) else if exist "C:\Python312\python.exe" (
    set PYTHON=C:\Python312\python.exe
) else (
    where python >nul 2>&1 && set PYTHON=python
)

if "%PYTHON%"=="" (
    echo [ERROR] Python not found. Install Python 3.11+ from https://python.org
    pause
    exit /b 1
)

echo [CS2 Farm] Using Python: %PYTHON%
echo [CS2 Farm] Starting controller...
cd /d "%~dp0"
"%PYTHON%" cs2_farm_controller.py
if errorlevel 1 (
    echo.
    echo [ERROR] Controller crashed. Check controller.log for details.
    pause
)
