@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

:: ─── Pack Sync Live Debug Launcher ──────────────────────────────────────────
:: Runs injector.py which:
::   1. Launches Pack Sync (normal window)
::   2. Launches graphics_editor.py in mirror mode (side-by-side)
::   3. Mirrors the running UI to the editor canvas in real time
::   4. Sends color edits from the editor back to the live Pack Sync window
::
:: This console window stays open and shows injector debug output.
:: Close it or press Ctrl+C to shut everything down.

set INJECTOR=..\Pythonimageditor\injector.py

echo.
echo  =====================================================
echo    Pack Sync  ^|  Live Mirror Debug Mode
echo  =====================================================
echo.
echo  injector.py will launch both Pack Sync and
echo  Graphics Editor side by side.
echo.
echo  How to use:
echo    1. Navigate Pack Sync — the editor mirrors the UI
echo    2. Click a shape in the editor ^(select tool^)
echo    3. Change Fill/Outline color ^> Apply Changes
echo    4. The live Pack Sync window updates instantly
echo.

:: Try Windows Python Launcher first (pyw keeps console for injector output)
where py >nul 2>&1
if !errorlevel! equ 0 (
    py -3 "%INJECTOR%"
    goto :done
)

:: Try python from PATH
where python >nul 2>&1
if !errorlevel! equ 0 (
    python "%INJECTOR%"
    goto :done
)

:: Try common install locations
for %%D in (
    "%LOCALAPPDATA%\Programs\Python\Python313"
    "%LOCALAPPDATA%\Programs\Python\Python312"
    "%LOCALAPPDATA%\Programs\Python\Python311"
    "C:\Python313" "C:\Python312" "C:\Python311"
) do (
    if exist "%%~D\python.exe" (
        "%%~D\python.exe" "%INJECTOR%"
        goto :done
    )
)

echo  ERROR: Python not found. Install from https://www.python.org/downloads/
pause
exit /b 1

:done
echo.
echo  [injector] exited.
pause
