@echo off
:: CS2 Farm — VM Agent launcher
:: Deploy this alongside vm_agent.py on each VM and run as Administrator

setlocal

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
    echo [ERROR] Python not found. Install Python 3.11+
    pause
    exit /b 1
)

echo [VM Agent] Using Python: %PYTHON%
echo [VM Agent] Listening on port 9999...
cd /d "%~dp0"
"%PYTHON%" vm_agent.py
if errorlevel 1 (
    echo.
    echo [ERROR] Agent crashed. Check vm_agent.log for details.
    pause
)
