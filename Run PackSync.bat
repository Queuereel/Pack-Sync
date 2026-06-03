@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

:: ─── Pack Sync launcher (no compilation required) ────────────────────────────
:: Finds Python on the system and runs pack_sync.py via pythonw (no console).
:: If packages are missing, pack_sync.py installs them automatically on first run.

set SCRIPT=pack_sync.py

:: 1. Windows Python Launcher "pyw" — installed by default with Python 3.3+
::    pyw = like "py" but suppresses the console window
where pyw >nul 2>&1
if !errorlevel! equ 0 (
    start "" pyw -3 "%~dp0%SCRIPT%"
    exit /b 0
)

:: 2. pythonw.exe in PATH
where pythonw >nul 2>&1
if !errorlevel! equ 0 (
    start "" pythonw "%~dp0%SCRIPT%"
    exit /b 0
)

:: 3. Check common per-user and system Python install locations
set SEARCH_PATHS=^
    "%LOCALAPPDATA%\Programs\Python" ^
    "%PROGRAMFILES%\Python3*" ^
    "%PROGRAMFILES%\Python*" ^
    "C:\Python3*" ^
    "C:\Python*"

for %%R in (%SEARCH_PATHS%) do (
    for /d %%D in (%%~R) do (
        if exist "%%D\pythonw.exe" (
            start "" "%%D\pythonw.exe" "%~dp0%SCRIPT%"
            exit /b 0
        )
    )
)

:: 4. Fall back to python.exe (console will appear briefly then close)
where python >nul 2>&1
if !errorlevel! equ 0 (
    start "" /b python "%~dp0%SCRIPT%"
    exit /b 0
)

:: ─── Python not found ─────────────────────────────────────────────────────────
echo.
echo   Pack Sync could not find Python on this machine.
echo.
echo   Please install Python 3.11 or newer:
echo   https://www.python.org/downloads/
echo.
echo   Tick "Add Python to PATH" during the installer.
echo.
pause
exit /b 1
