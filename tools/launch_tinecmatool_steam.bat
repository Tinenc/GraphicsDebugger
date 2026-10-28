@echo off
setlocal EnableExtensions EnableDelayedExpansion
title TinecmaTool - Steam and COD D3D12 Capture Launcher

rem ===========================================================================
rem  TinecmaTool (Stealth RenderDoc) - Portable One-Click Steam and COD Launcher
rem  Compatible with any PC running Windows 10/11 and Steam
rem ===========================================================================

rem Ensure Administrator privileges for driver management and proxy deployment
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Requesting Administrator privileges...
    powershell -NoProfile -Command "Start-Process cmd -ArgumentList '/c \"\"%~f0\" %*\"' -Verb RunAs"
    exit /b
)

set "HERE=%~dp0"
rem Check if run from tools\ or repo root
if exist "%HERE%x64\Development\TinecmaTool.dll" (
    set "ROOT=%HERE%"
) else if exist "%HERE%..\x64\Development\TinecmaTool.dll" (
    for %%i in ("%HERE%..") do set "ROOT=%%~fi\"
) else (
    set "ROOT=%HERE%"
)

set "TOOL_DLL=%ROOT%x64\Development\TinecmaTool.dll"
set "TOOL_CMD=%ROOT%x64\Development\TinecmaToolcmd.exe"
set "TOOL_GUI=%ROOT%x64\Development\qTinecmaTool.exe"
set "PROXY_DLL=%ROOT%tools\proxy\dxgi.dll"
set "DESKTOP=%USERPROFILE%\Desktop"

echo ========================================================================
echo   TinecmaTool (Stealth RenderDoc) - Steam and COD D3D12 Launcher
echo   Repo Root: %ROOT%
echo ========================================================================
echo.

rem 1. Check TinecmaTool.dll
if not exist "%TOOL_DLL%" (
    echo [ERROR] TinecmaTool.dll not found at:
    echo         %TOOL_DLL%
    echo.
    echo Please build the solution first by running build_renderdoc.bat.
    pause
    exit /b 1
)
echo [OK] Located TinecmaTool core: %TOOL_DLL%

rem 2. Check Proxy DLL
if not exist "%PROXY_DLL%" (
    echo [WARN] Pre-compiled proxy not found at %PROXY_DLL%.
    echo        Attempting to compile proxy via tools\proxy\build.bat...
    if exist "%ROOT%tools\proxy\build.bat" (
        pushd "%ROOT%tools\proxy"
        call build.bat
        popd
    )
    if not exist "%PROXY_DLL%" (
        echo [ERROR] Failed to locate or build %PROXY_DLL%!
        pause
        exit /b 1
    )
)
echo [OK] Located DXGI proxy DLL: %PROXY_DLL%

rem 3. Detect Steam installation
echo.
echo [1/5] Detecting Steam installation...
set "STEAM_DIR="

for /f "tokens=2*" %%A in ('reg query "HKCU\Software\Valve\Steam" /v SteamPath 2^>nul') do set "STEAM_DIR=%%B"
if not defined STEAM_DIR (
    for /f "tokens=2*" %%A in ('reg query "HKLM\SOFTWARE\WOW6432Node\Valve\Steam" /v InstallPath 2^>nul') do set "STEAM_DIR=%%B"
)
if not defined STEAM_DIR (
    for /f "tokens=2*" %%A in ('reg query "HKLM\SOFTWARE\Valve\Steam" /v InstallPath 2^>nul') do set "STEAM_DIR=%%B"
)

rem Normalize slashes if found
if defined STEAM_DIR (
    set "STEAM_DIR=!STEAM_DIR:/=\!"
)

rem Fallback drives check
if not defined STEAM_DIR (
    for %%D in (C D E F G H) do (
        if not defined STEAM_DIR (
            if exist "%%D:\Program Files (x86)\Steam\steam.exe" set "STEAM_DIR=%%D:\Program Files (x86)\Steam"
            if exist "%%D:\Steam\steam.exe" set "STEAM_DIR=%%D:\Steam"
        )
    )
)

if not defined STEAM_DIR (
    echo [ERROR] Steam directory could not be automatically detected.
    set /p "STEAM_DIR=Please enter your Steam install directory: "
)

if not exist "!STEAM_DIR!\steam.exe" (
    echo [ERROR] Invalid Steam directory: !STEAM_DIR!
    pause
    exit /b 1
)
echo [OK] Steam located at: !STEAM_DIR!

rem 4. Detect Call of Duty HQ install path
echo.
echo [2/5] Detecting Call of Duty HQ directory...
set "GAMEDIR="

rem Method A: Registry query (Standard for Steam installations on any PC)
for /f "tokens=2*" %%A in ('reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Steam App 1938090" /v InstallLocation 2^>nul') do (
    if exist "%%B\cod.exe" set "GAMEDIR=%%B"
)

rem Method B: Search drives directly
if not defined GAMEDIR (
    for %%D in (C D E F G H X) do (
        if not defined GAMEDIR (
            if exist "%%D:\SteamLibrary\steamapps\common\Call of Duty HQ\cod.exe" set "GAMEDIR=%%D:\SteamLibrary\steamapps\common\Call of Duty HQ"
            if exist "%%D:\Steam\steamapps\common\Call of Duty HQ\cod.exe" set "GAMEDIR=%%D:\Steam\steamapps\common\Call of Duty HQ"
            if exist "%%D:\Program Files (x86)\Steam\steamapps\common\Call of Duty HQ\cod.exe" set "GAMEDIR=%%D:\Program Files (x86)\Steam\steamapps\common\Call of Duty HQ"
            if exist "%%D:\Program Files\Steam\steamapps\common\Call of Duty HQ\cod.exe" set "GAMEDIR=%%D:\Program Files\Steam\steamapps\common\Call of Duty HQ"
        )
    )
)

if not defined GAMEDIR (
    echo [WARN] Call of Duty HQ could not be found automatically.
    set /p "GAMEDIR=Please enter the path to 'Call of Duty HQ' directory: "
)

rem Strip surrounding quotes
set "GAMEDIR=%GAMEDIR:"=%"

if not exist "%GAMEDIR%\cod.exe" (
    echo [ERROR] cod.exe was not found in: %GAMEDIR%
    pause
    exit /b 1
)
echo [OK] Call of Duty HQ found at: %GAMEDIR%

rem 5. Pre-flight cleanup and conflict resolution
echo.
echo [3/5] Performing pre-flight checks and cleanup...

rem Check legacy Win7 d3d12.dll
if exist "%GAMEDIR%\d3d12.dll" (
    echo [INFO] Detected legacy d3d12.dll in game root, renaming to d3d12.dll.bak...
    ren "%GAMEDIR%\d3d12.dll" d3d12.dll.bak >nul 2>&1
)

rem Clean up any stale desktop trigger files to prevent early watchdog trips (0x00001337)
del /f /q "%DESKTOP%\trigger_capture*" >nul 2>&1
del /f /q "%DESKTOP%\capture_completed.txt" >nul 2>&1

rem Stop interfering background services if active
sc.exe stop atvi-randgrid_sr >nul 2>&1
net stop "COD.Broker.Service" >nul 2>&1

echo [OK] Pre-flight cleanup completed.

rem 6. Deploy Proxy and Configuration
echo.
echo [4/5] Deploying DXGI proxy and capture configuration...

copy /y "%PROXY_DLL%" "%GAMEDIR%\dxgi.dll" >nul
if %errorlevel% neq 0 (
    echo [ERROR] Failed to copy dxgi.dll to %GAMEDIR%!
    pause
    exit /b 1
)
echo [OK] Deployed %GAMEDIR%\dxgi.dll

if exist "%GAMEDIR%\sp26" (
    copy /y "%PROXY_DLL%" "%GAMEDIR%\sp26\dxgi.dll" >nul
    echo [OK] Deployed %GAMEDIR%\sp26\dxgi.dll [Campaign Subdirectory]
)

rem Write Desktop\dxgi_proxy.ini
(
    echo [Capture]
    echo mode=renderdoc
    echo rdoc_dll=%TOOL_DLL%
) > "%DESKTOP%\dxgi_proxy.ini"
echo [OK] Configured %DESKTOP%\dxgi_proxy.ini (mode=renderdoc)

rem 6.5 Interactive Confirmation and 15s Countdown (GUI Window + Console Fallback)
echo.
echo [INFO] Opening configuration and 15s countdown window...
echo        (Press Enter in window to launch immediately, or wait 15s)
set "ACTION=LAUNCH"
if exist "%ROOT%tools\gui_countdown.ps1" (
    for /f "tokens=1* delims==" %%A in ('powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%tools\gui_countdown.ps1" -GameDir "%GAMEDIR%" -SteamDir "%STEAM_DIR%" -ToolDll "%TOOL_DLL%" -ProxyDll "%PROXY_DLL%" -TimeoutSeconds 15') do (
        if "%%A"=="ACTION" set "ACTION=%%B"
        if "%%A"=="GAMEDIR" (
            if exist "%%B\cod.exe" set "GAMEDIR=%%B"
        )
    )
) else (
    echo.
    echo ========================================================================
    echo   Auto-launching in 15 seconds. Press [Enter] to launch immediately,
    echo   or press [C] then [Enter] to Cancel.
    echo ========================================================================
    choice /t 15 /d Y /n /m "Launching game in 15s... [Y=Launch / C=Cancel]: "
    if errorlevel 2 set "ACTION=CANCEL"
)

if /i "!ACTION!"=="CANCEL" (
    echo.
    echo [CANCEL] Launch cancelled by user. Exiting.
    pause
    exit /b 0
)

rem Re-deploy in case game directory was changed in GUI
copy /y "%PROXY_DLL%" "%GAMEDIR%\dxgi.dll" >nul 2>&1
if exist "%GAMEDIR%\sp26" copy /y "%PROXY_DLL%" "%GAMEDIR%\sp26\dxgi.dll" >nul 2>&1

rem 7. Launch Steam and Game
echo.
echo [5/5] Launching Steam and Call of Duty HQ...

rem Start Steam if not running
tasklist /FI "IMAGENAME eq steam.exe" 2>nul | findstr /i "steam.exe" >nul
if %errorlevel% neq 0 (
    echo [INFO] Starting Steam client...
    start "" "!STEAM_DIR!\steam.exe"
    timeout /t 5 /nobreak >nul
) else (
    echo [OK] Steam is already running.
)

rem Launch Game via Steam URL (AppID 1938090)
echo [INFO] Handing launch to Steam (AppID 1938090)...
start "" "steam://rungameid/1938090"

echo.
echo ========================================================================
echo   GAME LAUNCH INITIATED
echo ========================================================================
echo   1. The game will start with TinecmaTool attached via DXGI Proxy.
echo   2. Look for the RenderDoc HUD in the top-left corner:
echo      'Capturing D3D12. Frame: ... (120 FPS)'
echo   3. To Capture a Frame:
echo      - Method A: Press F12 in-game
echo      - Method B: Press [C] in this console below
echo ========================================================================
echo.

:loop
echo [Commands]
echo   [C] Capture Frame Now
echo   [T] Extract Thumbnail from Newest .rdc
echo   [R] Open qTinecmaTool GUI to inspect captures
echo   [L] View dxgi_proxy.log tail
echo   [Q] Quit Launcher Console
echo.
set /p "CHOICE=Enter command [C/T/R/L/Q]: "

if /i "%CHOICE%"=="C" goto :do_capture
if /i "%CHOICE%"=="T" goto :do_thumb
if /i "%CHOICE%"=="R" goto :do_gui
if /i "%CHOICE%"=="L" goto :do_log
if /i "%CHOICE%"=="Q" goto :do_quit
goto :loop

:do_capture
echo [CAPTURE] Sending trigger signal to game...
echo TRIGGER > "%DESKTOP%\trigger_capture_cod"
echo [CAPTURE] Trigger file created at %DESKTOP%\trigger_capture_cod.
echo           Proxy will consume it and serialize frame directly to Desktop.
timeout /t 3 /nobreak >nul
goto :loop

:do_thumb
echo [THUMB] Searching for newest .rdc on Desktop...
set "NEWRDC="
for /f "tokens=*" %%F in ('dir /b /o-d "%DESKTOP%\COD_RDC_*.rdc" 2^>nul') do (
    if not defined NEWRDC set "NEWRDC=%DESKTOP%\%%F"
)

if not defined NEWRDC (
    echo [WARN] No COD_RDC_*.rdc file found on Desktop yet!
) else (
    echo [THUMB] Extracting thumbnail from: !NEWRDC!
    "%TOOL_CMD%" thumb --out="%DESKTOP%\COD_latest_thumb.jpg" --format=jpg "!NEWRDC!"
    if exist "%DESKTOP%\COD_latest_thumb.jpg" (
        echo [OK] Saved thumbnail: %DESKTOP%\COD_latest_thumb.jpg
        start "" "%DESKTOP%\COD_latest_thumb.jpg"
    )
)
goto :loop

:do_gui
echo [GUI] Launching qTinecmaTool...
set "NEWRDC="
for /f "tokens=*" %%F in ('dir /b /o-d "%DESKTOP%\COD_RDC_*.rdc" 2^>nul') do (
    if not defined NEWRDC set "NEWRDC=%DESKTOP%\%%F"
)
if defined NEWRDC (
    start "" "%TOOL_GUI%" "!NEWRDC!"
) else (
    start "" "%TOOL_GUI%"
)
goto :loop

:do_log
echo.
echo === Last 20 lines of dxgi_proxy.log ===
if exist "%DESKTOP%\dxgi_proxy.log" (
    powershell -NoProfile -Command "Get-Content '%DESKTOP%\dxgi_proxy.log' -Tail 20"
) else (
    echo Log file not created yet.
)
echo =======================================
echo.
goto :loop

:do_quit
echo Exiting launcher console. TinecmaTool remains active in-game.
exit /b 0
