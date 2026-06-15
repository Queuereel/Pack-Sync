@echo off
:: Build Pack Sync for Windows (native, no Python required in output)
:: Double-click this file to build. Output: Pack Sync\dist\windows-x64\PackSync.exe
::
:: Requirements: Python 3.11+ must be installed (python.exe in PATH)
:: Internet access is needed on first run to install PyInstaller + Pillow + pystray.

title Build Pack Sync
cd /d "%~dp0"

echo.
echo  ==========================================
echo    Pack Sync Builder ^| Windows x64/ARM64
echo  ==========================================
echo.

:: Find a real Python. Bare "python" is avoided on purpose: on this machine it
:: resolves to the Microsoft Store stub (...\WindowsApps\python.exe), which only
:: prints "Python was not found" and exits. The py launcher reaches the real
:: installs. Native ARM64 is preferred, then x64, then whatever py defaults to.
set "PY="
for %%P in ("py -V:3.14-arm64" "py -3-64" "py -3" "py") do (
    if not defined PY (
        %%~P -c "pass" >nul 2>&1 && set "PY=%%~P"
    )
)

if not defined PY (
    echo  ERROR: No real Python found.
    echo  The py launcher could not locate a Python install, and bare "python"
    echo  only finds the Microsoft Store stub.
    echo  Install Python 3.11+ from https://www.python.org/downloads/windows/
    echo  ^(or run:  winget install Python.Python.3.14^)
    pause
    exit /b 1
)

echo  Python found:
%PY% --version
echo.

:: PyInstaller can't overwrite dist\...\PackSync.exe while it is running.
:: Stop any running instance (and its tray helper) before building.
echo  Stopping any running Pack Sync ...
taskkill /IM PackSync.exe /F  >nul 2>&1
taskkill /IM TrayHelper.exe /F >nul 2>&1
echo.

echo  Running build.py ...
echo.
%PY% build.py

if %errorlevel% equ 0 (
    echo.
    echo  ============================================================
    echo    Build complete!
    echo    Output: dist\windows-arm64\PackSync.exe
    echo            (or windows-x64 on a 64-bit Intel/AMD machine)
    echo  ============================================================
    echo.
    echo  Press any key to open the output folder ...
    pause >nul
    for /d %%D in ("dist\windows-*") do (
        if exist "%%D\PackSync.exe" (
            explorer "%%D"
            goto :done
        )
    )
) else (
    echo.
    echo  BUILD FAILED. Check the errors above.
    echo.
)

:done
pause
