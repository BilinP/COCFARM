@echo off
rem Double-click to open farmbot. The first run sets up Python packages
rem in a .venv folder next to this file; later runs just update them.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Setting up farmbot for the first time. This takes a minute...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if not exist ".venv\Scripts\python.exe" (
        echo.
        echo Python 3.11 or newer is needed. Get it from https://www.python.org/downloads/
        echo and tick "Add python.exe to PATH" during the install.
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -e .
if errorlevel 1 (
    echo.
    echo Installing farmbot's packages failed; see the messages above.
    pause
    exit /b 1
)

start "" ".venv\Scripts\pythonw.exe" -m farmbot gui
