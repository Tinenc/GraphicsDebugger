@echo off
setlocal EnableExtensions EnableDelayedExpansion
title TinecmaTool - Modern Warfare (2019) D3D12 Capture Launcher

rem Change working directory to script location immediately
cd /d "%~dp0"

rem ===========================================================================
rem  TinecmaTool (Stealth RenderDoc) - Modern Warfare (2019) Launcher
rem ===========================================================================

rem Ensure Administrator privileges
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Requesting Administrator privileges...
    powershell -NoProfile -Command "Start-Process cmd -ArgumentList '/c \"\"%~f0\" %*\"' -WorkingDirectory '%~dp0' -Verb RunAs"
    exit /b
)

rem Re-verify working directory after elevation
cd /d "%~dp0"

set "HERE=%~dp0"
set "ROOT=%HERE%"
set "GAMEDIR=D:\Call of Duty Modern Warfare"
set "TOOL_DLL=%ROOT%x64\Development\TinecmaTool.dll"
set "TOOL_CMD=%ROOT%x64\Development\qTinecmaTool.exe"
set "PROXY_DLL=%ROOT%tools\proxy\dxgi.dll"
set "D3D12_PROXY_DLL=%ROOT%tools\proxy\d3d12.dll"
set "DESKTOP=C:\Users\Administrator\Desktop"

echo ========================================================================
echo   TinecmaTool - Call of Duty: Modern Warfare (2019) D3D12 Launcher
echo   Game Directory: %GAMEDIR%
echo ========================================================================
echo.

rem 1. Pre-flight cleanup
echo [1/4] Performing pre-flight cleanup...
del /f /q "%DESKTOP%\trigger_capture*" >nul 2>&1
del /f /q "%DESKTOP%\capture_completed.txt" >nul 2>&1
sc.exe stop atvi-randgrid_sr >nul 2>&1
echo [OK] Pre-flight cleanup completed.

rem 2. Deploy Proxies and Configuration
echo.
echo [2/4] Verifying and deploying DXGI and D3D12 proxies...

rem Backup original Win7 d3d12.dll if still present
if exist "%GAMEDIR%\d3d12.dll" (
    powershell -NoProfile -Command "$info = (Get-Item '%GAMEDIR%\d3d12.dll').VersionInfo; if ($info.FileDescription -match 'Windows 7') { Copy-Item '%GAMEDIR%\d3d12.dll' '%GAMEDIR%\d3d12_win7.dll.bak' -Force; Write-Host '[OK] Backed up original Win7 d3d12.dll' }"
)

copy /y "%PROXY_DLL%" "%GAMEDIR%\dxgi.dll" >nul
echo [OK] Deployed %GAMEDIR%\dxgi.dll (DXGI Proxy)

copy /y "%D3D12_PROXY_DLL%" "%GAMEDIR%\d3d12.dll" >nul
echo [OK] Deployed %GAMEDIR%\d3d12.dll (D3D12 Proxy)

(
    echo [proxy]
    echo mode=renderdoc
    echo rdoc_dll=%TOOL_DLL%
    echo.
    echo [Capture]
    echo mode=renderdoc
    echo rdoc_dll=%TOOL_DLL%
) > "%DESKTOP%\dxgi_proxy.ini"
copy /y "%DESKTOP%\dxgi_proxy.ini" "%GAMEDIR%\dxgi_proxy.ini" >nul
echo [OK] Configured dxgi_proxy.ini (mode=renderdoc)

rem 3. Launch Guidance
echo.
echo [3/4] Ready to launch!
echo ========================================================================
echo   HOW TO START THE GAME:
echo   Option A: Click 'Play / 进入游戏' in Battle.net (战网客户端) [RECOMMENDED]
echo   Option B: Press [G] below to launch ModernWarfare.exe directly
echo ========================================================================
echo.
echo [4/4] In-Game Capture Guide:
echo   1. Wait until game finishes shader compilation and enters Main Menu / 3D Scene.
echo   2. Look for the RenderDoc HUD in the top-left corner:
echo      'Capturing D3D12. Frame: ... (XX FPS)'
echo   3. Press F12 in-game OR press [C] in this console to capture a frame.
echo ========================================================================
echo.

:loop
echo [Console Commands]
echo   [G] Launch ModernWarfare.exe directly now
echo   [C] Capture Frame Now (F12 equivalent)
echo   [T] Extract Thumbnail from Newest Capture
echo   [R] Open qTinecmaTool GUI to inspect captures
echo   [L] View proxy log tail
echo   [Q] Quit Launcher Console
echo.
set /p "CHOICE=Enter command [G/C/T/R/L/Q]: "

if /i "%CHOICE%"=="G" goto :do_launch
if /i "%CHOICE%"=="C" goto :do_capture
if /i "%CHOICE%"=="T" goto :do_thumb
if /i "%CHOICE%"=="R" goto :do_gui
if /i "%CHOICE%"=="L" goto :do_log
if /i "%CHOICE%"=="Q" goto :do_quit
goto :loop

:do_launch
echo [LAUNCH] Starting ModernWarfare.exe...
start "" /d "%GAMEDIR%" "%GAMEDIR%\ModernWarfare.exe"
timeout /t 3 /nobreak >nul
goto :loop

:do_capture
echo [CAPTURE] Sending trigger signal to ModernWarfare.exe...
echo TRIGGER > "%DESKTOP%\trigger_capture"
echo TRIGGER > "%DESKTOP%\trigger_capture_modernwarfare_exe"
echo [CAPTURE] Signal sent. Waiting for capture to finish...
set "CAP_DONE="
for /l %%i in (1,1,20) do (
    if not defined CAP_DONE (
        if exist "%DESKTOP%\capture_completed.txt" (
            set "CAP_DONE=1"
        ) else (
            timeout /t 1 /nobreak >nul
        )
    )
)
if defined CAP_DONE (
    echo [OK] Capture completed successfully!
    type "%DESKTOP%\capture_completed.txt"
) else (
    echo [INFO] Signal dispatched. Check Desktop for COD_RDC_*.rdc file.
)
echo.
goto :loop

:do_thumb
echo [THUMB] Searching for newest .rdc on Desktop...
set "NEWRDC="
for /f "tokens=*" %%F in ('dir /b /o-d "%DESKTOP%\COD_RDC_*.rdc" 2^>nul') do (
    if not defined NEWRDC set "NEWRDC=%DESKTOP%\%%F"
)
if not defined NEWRDC (
    echo [WARN] No COD_RDC_*.rdc file found on Desktop!
) else (
    echo [THUMB] Extracting thumbnail from: !NEWRDC!
    "%TOOL_CMD%" thumb --out="%DESKTOP%\MW2019_latest_thumb.jpg" --format=jpg "!NEWRDC!"
    if exist "%DESKTOP%\MW2019_latest_thumb.jpg" (
        echo [OK] Saved thumbnail: %DESKTOP%\MW2019_latest_thumb.jpg
        start "" "%DESKTOP%\MW2019_latest_thumb.jpg"
    )
)
echo.
goto :loop

:do_gui
echo [GUI] Launching qTinecmaTool...
set "NEWRDC="
for /f "tokens=*" %%F in ('dir /b /o-d "%DESKTOP%\COD_RDC_*.rdc" 2^>nul') do (
    if not defined NEWRDC set "NEWRDC=%DESKTOP%\%%F"
)
if defined NEWRDC (
    echo [GUI] Opening newest capture: !NEWRDC!
    start "" "%TOOL_CMD%" "!NEWRDC!"
) else (
    start "" "%TOOL_CMD%"
)
echo.
goto :loop

:do_log
echo [LOG] Last 20 lines of dxgi_proxy.log:
if exist "%DESKTOP%\dxgi_proxy.log" (
    powershell -NoProfile -Command "Get-Content '%DESKTOP%\dxgi_proxy.log' -Tail 20"
) else (
    echo [INFO] dxgi_proxy.log does not exist yet.
)
echo.
goto :loop

:do_quit
echo Exiting launcher console. TinecmaTool remains active.
exit /b 0
