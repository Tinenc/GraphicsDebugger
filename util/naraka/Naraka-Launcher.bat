@echo off
rem ===========================================================================
rem  TinecmaTool - Naraka: Bladepoint launcher (GUI entry point)
rem ===========================================================================
rem
rem  Same shape as the Steam-Cod launcher: this file does nothing but hand over
rem  to the GUI, which owns the whole run.
rem
rem  Deliberately does NOT request elevation. Started from an elevated parent,
rem  StartGame.exe brings up NeacClient.exe but NarakaBladepoint.exe never
rem  appears. The GUI drops to medium integrity through explorer when it finds
rem  itself elevated; asking for admin here would defeat that.
rem
rem  -STA is required for WinForms.

setlocal
cd /d "%~dp0"

if not exist "%~dp0Naraka-Gui.ps1" (
    echo [error] Naraka-Gui.ps1 is missing next to this file.
    pause
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File "%~dp0Naraka-Gui.ps1" %*

endlocal
exit /b 0
