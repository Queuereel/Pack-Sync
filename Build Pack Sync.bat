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

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo  ERROR: Python not found in PATH.
    echo  Install from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo  Python found:
python --version
echo.

:: PyInstaller can't overwrite dist\...\PackSync.exe while it is running.
:: Stop any running instance (and its tray helper) before building.
echo  Stopping any running Pack Sync ...
taskkill /IM PackSync.exe /F  >nul 2>&1
taskkill /IM TrayHelper.exe /F >nul 2>&1
echo.

echo  Running build.py ...
echo.
python build.py

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
