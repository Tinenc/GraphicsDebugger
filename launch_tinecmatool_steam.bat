@echo off
setlocal EnableExtensions EnableDelayedExpansion
title TinecmaTool - Call of Duty / D3D12 Capture Launcher

rem Change working directory to script location immediately
cd /d "%~dp0"

rem ===========================================================================
rem  TinecmaTool (Stealth RenderDoc) - Portable One-Click COD & D3D12 Launcher
rem  Compatible with Call of Duty HQ (Steam), Modern Warfare 2019 (Battle.net)
rem ===========================================================================

rem Ensure Administrator privileges for driver management and proxy deployment
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Requesting Administrator privileges...
    powershell -NoProfile -Command "Start-Process cmd -ArgumentList '/c \"\"%~f0\" %*\"' -WorkingDirectory '%~dp0' -Verb RunAs"
    exit /b
)

rem Re-verify working directory after elevation
cd /d "%~dp0"

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
set "D3D12_PROXY_DLL=%ROOT%tools\proxy\d3d12.dll"
set "DESKTOP=%USERPROFILE%\Desktop"

echo ========================================================================
echo   TinecmaTool (Stealth RenderDoc) - COD and D3D12 Launcher
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

rem 2. Check Proxy DLLs (DXGI and D3D12)
set "NEED_BUILD_PROXY="
if not exist "%PROXY_DLL%" set "NEED_BUILD_PROXY=1"
if not exist "%D3D12_PROXY_DLL%" set "NEED_BUILD_PROXY=1"

if defined NEED_BUILD_PROXY (
    echo [WARN] Pre-compiled proxies not found at %PROXY_DLL% or %D3D12_PROXY_DLL%.
    echo        Attempting to compile proxies via tools\proxy\build.bat...
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
    if not exist "%D3D12_PROXY_DLL%" (
        echo [ERROR] Failed to locate or build %D3D12_PROXY_DLL%!
        pause
        exit /b 1
    )
)
echo [OK] Located DXGI proxy DLL: %PROXY_DLL%
echo [OK] Located D3D12 proxy DLL: %D3D12_PROXY_DLL%

rem 3. Detect Steam installation (optional for MW2019)
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

if defined STEAM_DIR (
    set "STEAM_DIR=!STEAM_DIR:/=\!"
)

if not defined STEAM_DIR (
    for %%D in (C D E F G H) do (
        if not defined STEAM_DIR (
            if exist "%%D:\Program Files (x86)\Steam\steam.exe" set "STEAM_DIR=%%D:\Program Files (x86)\Steam"
            if exist "%%D:\Steam\steam.exe" set "STEAM_DIR=%%D:\Steam"
        )
    )
)

if defined STEAM_DIR (
    if exist "!STEAM_DIR!\steam.exe" (
        echo [OK] Steam located at: !STEAM_DIR!
    ) else (
        echo [INFO] Steam client not found at !STEAM_DIR!.
    )
) else (
    echo [INFO] Steam client not detected in default paths.
)

rem 4. Detect Call of Duty installations
echo.
echo [2/5] Detecting Call of Duty installations...
set "GAMEDIR="
set "COD_HQ_DIR="
set "MW2019_DIR="

rem Check MW2019
if exist "D:\Call of Duty Modern Warfare\ModernWarfare.exe" set "MW2019_DIR=D:\Call of Duty Modern Warfare"
if not defined MW2019_DIR (
    for %%D in (C D E F G H X) do (
        if not defined MW2019_DIR (
            if exist "%%D:\Call of Duty Modern Warfare\ModernWarfare.exe" set "MW2019_DIR=%%D:\Call of Duty Modern Warfare"
        )
    )
)

rem Check COD HQ (AppID 1938090)
for /f "tokens=2*" %%A in ('reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Steam App 1938090" /v InstallLocation 2^>nul') do (
    if exist "%%B\cod.exe" set "COD_HQ_DIR=%%B"
)
if not defined COD_HQ_DIR (
    for %%D in (F D C E G H X) do (
        if not defined COD_HQ_DIR (
            if exist "%%D:\SteamLibrary\steamapps\common\Call of Duty HQ\cod.exe" set "COD_HQ_DIR=%%D:\SteamLibrary\steamapps\common\Call of Duty HQ"
            if exist "%%D:\Steam\steamapps\common\Call of Duty HQ\cod.exe" set "COD_HQ_DIR=%%D:\Steam\steamapps\common\Call of Duty HQ"
            if exist "%%D:\Program Files (x86)\Steam\steamapps\common\Call of Duty HQ\cod.exe" set "COD_HQ_DIR=%%D:\Program Files (x86)\Steam\steamapps\common\Call of Duty HQ"
            if exist "%%D:\Program Files\Steam\steamapps\common\Call of Duty HQ\cod.exe" set "COD_HQ_DIR=%%D:\Program Files\Steam\steamapps\common\Call of Duty HQ"
        )
    )
)

if defined MW2019_DIR echo [OK] Found Modern Warfare (2019): !MW2019_DIR!
if defined COD_HQ_DIR echo [OK] Found Call of Duty HQ: !COD_HQ_DIR!

rem Default to MW2019 or COD HQ
if defined MW2019_DIR (
    set "GAMEDIR=!MW2019_DIR!"
) else if defined COD_HQ_DIR (
    set "GAMEDIR=!COD_HQ_DIR!"
)

rem 5. Pre-flight cleanup
echo.
echo [3/5] Performing pre-flight checks and cleanup...
del /f /q "%DESKTOP%\trigger_capture*" >nul 2>&1
del /f /q "%DESKTOP%\capture_completed.txt" >nul 2>&1
sc.exe stop atvi-randgrid_sr >nul 2>&1
net stop "COD.Broker.Service" >nul 2>&1
echo [OK] Pre-flight cleanup completed.

rem 6. Interactive Confirmation and Options Guide (GUI Window + Console Fallback)
echo.
echo [4/5] Opening configuration guide window...
echo        (Press Enter in window to launch immediately, or close to cancel)
set "ACTION=LAUNCH"
set "GAME_TYPE=MW2019"
set "TARGET_EXE=ModernWarfare.exe"
set "CAPTURE_MODE=renderdoc"

if not exist "%ROOT%tools\gui_countdown.ps1" goto :fallback_console

set "GUI_RES_FILE=%TEMP%\tinecma_gui_res_%RANDOM%.txt"
if exist "!GUI_RES_FILE!" del /f /q "!GUI_RES_FILE!" >nul 2>&1

powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%tools\gui_countdown.ps1" -GameDir "%GAMEDIR%" -SteamDir "%STEAM_DIR%" -ToolDll "%TOOL_DLL%" -ProxyDll "%PROXY_DLL%" -DesktopDir "%DESKTOP%" -OutputFile "!GUI_RES_FILE!"
set "PS_EXIT=!ERRORLEVEL!"

if exist "!GUI_RES_FILE!" (
    for /f "usebackq tokens=1* delims==" %%A in ("!GUI_RES_FILE!") do (
        if "%%A"=="ACTION" set "ACTION=%%B"
        if "%%A"=="GAMEDIR" set "GAMEDIR=%%B"
        if "%%A"=="GAME_TYPE" set "GAME_TYPE=%%B"
        if "%%A"=="TARGET_EXE" set "TARGET_EXE=%%B"
        if "%%A"=="MODE" set "CAPTURE_MODE=%%B"
    )
    del /f /q "!GUI_RES_FILE!" >nul 2>&1
    goto :guide_done
)

if !PS_EXIT! neq 0 (
    set "ACTION=CANCEL"
    goto :guide_done
)

:fallback_console
echo.
echo ========================================================================
echo   [Options and Configuration Guide]
echo   - Selected Directory: %GAMEDIR%
echo   - Capture Backend: %TOOL_DLL%
echo     Tip: TinecmaTool [RenderDoc v1.44] 1.33s sync write + SEH protection
echo   - Usage: Press [F12] in-game or [C] in console to capture frame
echo   - Stealth: PEB unlinked, port-free, watchdog 0x00001337 isolated
echo ========================================================================
echo   [No Time Limit] Press [Enter] to continue launching the game...
echo   (Or enter Q and press Enter to cancel and exit)
echo ========================================================================
set "CONFIRM="
set /p "CONFIRM=Press [Enter] to launch or [Q] to quit: "
if /i "!CONFIRM!"=="Q" set "ACTION=CANCEL"

:guide_done
if /i "!ACTION!"=="CANCEL" (
    echo.
    echo [CANCEL] Launch cancelled by user. Exiting.
    pause
    exit /b 0
)

rem 7. Deploy Proxies and Configuration to selected game
echo.
echo [5/5] Deploying DXGI and D3D12 proxies to %GAMEDIR%...

if not exist "%GAMEDIR%" (
    echo [ERROR] Game directory does not exist: %GAMEDIR%
    pause
    exit /b 1
)

rem If Modern Warfare 2019, backup Win7 d3d12.dll safely
if exist "%GAMEDIR%\d3d12.dll" (
    powershell -NoProfile -Command "$info = (Get-Item '%GAMEDIR%\d3d12.dll').VersionInfo; if ($info.FileDescription -match 'Windows 7') { Copy-Item '%GAMEDIR%\d3d12.dll' '%GAMEDIR%\d3d12_win7.dll.bak' -Force; Write-Host '[OK] Backed up original Win7 d3d12.dll' }"
)

rem Deploy dxgi.dll and d3d12.dll
copy /y "%PROXY_DLL%" "%GAMEDIR%\dxgi.dll" >nul
if %errorlevel% neq 0 (
    echo [ERROR] Failed to copy dxgi.dll to %GAMEDIR%!
    pause
    exit /b 1
)
echo [OK] Deployed %GAMEDIR%\dxgi.dll

copy /y "%D3D12_PROXY_DLL%" "%GAMEDIR%\d3d12.dll" >nul
if %errorlevel% neq 0 (
    echo [ERROR] Failed to copy d3d12.dll to %GAMEDIR%!
    pause
    exit /b 1
)
echo [OK] Deployed %GAMEDIR%\d3d12.dll

rem If COD HQ with sp26 campaign directory, deploy there too
if exist "%GAMEDIR%\sp26" (
    copy /y "%PROXY_DLL%" "%GAMEDIR%\sp26\dxgi.dll" >nul 2>&1
    copy /y "%D3D12_PROXY_DLL%" "%GAMEDIR%\sp26\d3d12.dll" >nul 2>&1
    echo [OK] Deployed proxies to sp26\ campaign directory
)

rem Update Desktop\dxgi_proxy.ini and copy to game dir
(
    echo [proxy]
    echo mode=!CAPTURE_MODE!
    echo rdoc_dll=%TOOL_DLL%
    echo.
    echo [Capture]
    echo mode=!CAPTURE_MODE!
    echo rdoc_dll=%TOOL_DLL%
) > "%DESKTOP%\dxgi_proxy.ini"
copy /y "%DESKTOP%\dxgi_proxy.ini" "%GAMEDIR%\dxgi_proxy.ini" >nul
echo [OK] Configured dxgi_proxy.ini (mode=!CAPTURE_MODE!)

rem 8. Launch Selected Game
echo.
if /i "!GAME_TYPE!"=="MW2019" (
    echo [INFO] Launching Modern Warfare (2019)...
    start "" /d "%GAMEDIR%" "%GAMEDIR%\ModernWarfare.exe"
    echo [OK] ModernWarfare.exe launch initiated.
    echo      (Battle.net 战网客户端亦可直接点击【进入游戏】)
) else if /i "!GAME_TYPE!"=="COD_HQ" (
    echo [INFO] Launching Steam and Call of Duty HQ...
    tasklist /FI "IMAGENAME eq steam.exe" 2>nul | findstr /i "steam.exe" >nul
    if %errorlevel% neq 0 (
        if exist "!STEAM_DIR!\steam.exe" (
            echo [INFO] Starting Steam client...
            start "" "!STEAM_DIR!\steam.exe"
            timeout /t 5 /nobreak >nul
        )
    )
    echo [INFO] Handing launch to Steam (AppID 1938090)...
    start "" "steam://rungameid/1938090"
) else (
    if defined TARGET_EXE (
        if exist "%GAMEDIR%\!TARGET_EXE!" (
            echo [INFO] Launching custom game: !TARGET_EXE!...
            start "" /d "%GAMEDIR%" "%GAMEDIR%\!TARGET_EXE!"
        )
    ) else (
        echo [INFO] Proxy deployed. Please launch your game now.
    )
)

echo.
echo ========================================================================
echo   GAME LAUNCH INITIATED
echo ========================================================================
echo   1. The game will start with TinecmaTool attached via DXGI Proxy.
echo   2. Look for the RenderDoc HUD in the top-left corner:
echo      'Capturing D3D12. Frame: ... (XX FPS)'
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
echo TRIGGER > "%DESKTOP%\trigger_capture"
echo TRIGGER > "%DESKTOP%\trigger_capture_cod"
echo TRIGGER > "%DESKTOP%\trigger_capture_modernwarfare_exe"
echo [CAPTURE] Trigger signal sent. Waiting for capture to complete...
set "CAP_DONE="
for /l %%i in (1,1,15) do (
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
