@echo off
setlocal

REM PS is defined before the -Register jump so the elevated helper can use it too
set "PS=powershell -NoProfile -ExecutionPolicy Bypass -Command"

REM ===========================================================================
REM  launch_endfield.bat   --  TinecmaTool / Endfield capture setup
REM
REM  Self contained. Uses ONLY the build in this tree:
REM    ..\x64\Development\qTinecmaTool.exe  + TinecmaTool.json + TinecmaTool.dll
REM
REM  HOW THIS WORKS (early-bird APC injection, same idea as the Wuthering Waves
REM  setup, but there is no shell layer here to inject into):
REM
REM    1. you Launch  1.6.0\Games.exe  from qTinecmaTool
REM    2. the tool injects into Games.exe with an early-bird APC - no remote
REM       thread, so the anti-cheat kernel callbacks see nothing
REM    3. Games.exe spawns Endfield.exe when you press "start game"; the hook on
REM       CreateProcess creates it suspended, queues the same APC, resumes it
REM    4. our DllMain runs before Endfield.exe executes anything, sets
REM       ENABLE_VULKAN_TINECMATOOL_CAPTURE in ITS OWN environment
REM    5. when Unity creates the Vulkan instance the loader sees that variable
REM       and loads the layer - only inside the game process
REM
REM  Why not just register the layer globally and be done: that loads the DLL
REM  into every Vulkan program on the machine, so other TinecmaTool builds and
REM  any .rdc replay window hit the "should only be called during capture"
REM  assert and die. Injection puts the variable where it belongs.
REM
REM  The json therefore stays LOCKED (enable_environment present). This script
REM  undoes any earlier unlock.
REM
REM  Admin is only needed once, to write the HKLM registration.
REM ===========================================================================

if /i "%~1"=="-Register" goto :DoRegister
if /i "%~1"=="-Off" goto :DoOff

set "TOOLS=%~dp0"
for %%i in ("%~dp0..") do set "ROOT=%%~fi"
set "QD=%ROOT%\x64\Development\qTinecmaTool.exe"
set "GAMES=H:\Hypergryph Launcher\1.6.0\Games.exe"

echo ==========================================================
echo   TinecmaTool / Endfield - capture setup
echo ==========================================================
echo.

if not exist "%QD%" (
    echo   ERROR: no built qTinecmaTool.exe in THIS tree:
    echo          %QD%
    echo          Build renderdoc.sln first, then run this again.
    echo.
    pause
    exit /b 2
)

for %%i in ("%QD%") do set "QDDIR=%%~dpi"
set "JSON=%QDDIR%TinecmaTool.json"
set "BAK=%JSON%.bak"
set "REGQ=%TEMP%\_tcm_reg.txt"

echo   tool   : %QD%
echo   layer  : %JSON%
echo   game ui: %GAMES%
echo.

if not exist "%JSON%" (
    echo   ERROR: no layer json next to the tool:
    echo          %JSON%
    echo.
    pause
    exit /b 3
)

REM ---- 1. the json must be LOCKED now ---------------------------------------
REM Injection supplies the enable variable inside the game process, so the layer
REM no longer has to load unconditionally. Put enable_environment back if an
REM earlier session unlocked it.
echo [1/4] checking the Vulkan layer json ...
findstr /c:"enable_environment" "%JSON%" >nul 2>&1
if not errorlevel 1 (
    echo         locked, as it should be
) else (
    if exist "%BAK%" (
        copy /y "%BAK%" "%JSON%" >nul 2>&1
        findstr /c:"enable_environment" "%JSON%" >nul 2>&1
        if errorlevel 1 (
            echo         WARNING: could not re-lock the json, the layer may load globally
        ) else (
            echo         re-locked from backup (undoing an earlier unlock)
        )
    ) else (
        echo         WARNING: json is unlocked and there is no backup at
        echo                  %BAK%
        echo                  The layer will load into every Vulkan program.
    )
)
echo.

REM ---- 2. this tree's json must be the registered layer ---------------------
REM The layer still has to be registered in HKLM, otherwise the loader has no
REM manifest to load inside the game process even with the env var present.
echo [2/4] checking the Vulkan layer registration ...
set "REGQ_CMD=$j='%JSON%'; $k=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SOFTWARE\Khronos\Vulkan\ImplicitLayers'); if(-not $k){'NEED'}else{$n=@($k.GetValueNames() | Where-Object { $_ -match 'Tinecma|renderdoc' }); if($n.Count -eq 1 -and $n[0] -eq $j){'OK'}else{'NEED'}}"
%PS% "%REGQ_CMD%" > "%REGQ%" 2>&1
findstr /x /c:"OK" "%REGQ%" >nul 2>&1
if not errorlevel 1 (
    echo         registered, exactly one entry, points here
    goto :RegDone
)

echo         currently registered:
%PS% "$k=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SOFTWARE\Khronos\Vulkan\ImplicitLayers'); if($k){ $a=@($k.GetValueNames() | Where-Object { $_ -match 'Tinecma|renderdoc' }); if($a.Count -eq 0){Write-Host '          (none)'}else{ $a | ForEach-Object { $t=$_; $m=''; if(-not(Test-Path $t)){$m='  [missing file]'}; Write-Host ('          ' + $t + $m) } } }"
echo         switching that entry to this tree (asks for admin once) ...
%PS% "Start-Process -FilePath '%~f0' -ArgumentList '-Register' -Verb RunAs -Wait"
%PS% "%REGQ_CMD%" > "%REGQ%" 2>&1
findstr /x /c:"OK" "%REGQ%" >nul 2>&1
if not errorlevel 1 (
    echo         registered - other builds of TinecmaTool no longer get the layer
) else (
    echo         WARNING: registration failed - the layer will not reach the game.
    echo                  Check HKLM\SOFTWARE\Khronos\Vulkan\ImplicitLayers
)
:RegDone
del "%REGQ%" >nul 2>&1
echo.

REM ---- 3. force child injection ---------------------------------------------
REM With APC injection the target starts running immediately, so the capture
REM options pushed in afterwards can arrive after Games.exe has already spawned
REM Endfield.exe. This makes the decision independent of that race.
REM It has to be in OUR environment so qTinecmaTool inherits it, and qTinecmaTool
REM passes its environment on to Games.exe.
echo [3/4] arming forced child injection ...
set "TCM_FORCE_CHILD_INJECT=1"
echo         TCM_FORCE_CHILD_INJECT=1
echo.

REM ---- 4. start the tool ----------------------------------------------------
echo [4/4] restarting qTinecmaTool ...
%PS% "$qd='%QD%'; $all=@(Get-Process -Name qTinecmaTool -ErrorAction SilentlyContinue); $mine=@($all | Where-Object { $_.Path -eq $qd }); if($mine.Count -eq 0){Write-Host '        this tree was not running'}else{ $mine | ForEach-Object { Stop-Process -Id $_.Id -Force; Write-Host ('        stopped PID ' + $_.Id) } }; $other=@($all | Where-Object { $_.Path -and $_.Path -ne $qd }); if($other.Count -gt 0){ Write-Host ('        left alone ' + $other.Count + ' instance(s) from another tree') }; $unknown=@($all | Where-Object { -not $_.Path }); if($unknown.Count -gt 0){ Write-Host ('        ' + $unknown.Count + ' instance(s) had an unreadable path, untouched') }"
start "" "%QD%"

echo.
echo ==========================================================
echo   Now in qTinecmaTool
echo ==========================================================
echo   Executable : %GAMES%
echo.
echo   [x] Capture Child Processes
echo   [ ] Global hook
echo.
echo   then press Launch. The launcher window opens - click "start game"
echo   and get into the world, then press F12.
echo.
echo   What to check
echo     ports   netstat -ano ^| findstr "3892"
echo             Games.exe and Endfield.exe should each own one
echo     log     %TEMP%\TinecmaTool\RenderDoc*.log
echo             one file, every line prefixed with the source PID
echo             Endfield.exe must show "Loading into ...\Endfield.exe",
echo             "Listening for target control on 3892x" and "RegisterSurface()"
echo.
echo   This window closes itself in 20 seconds.
echo.
timeout /t 20 /nobreak >nul
exit /b 0

REM ===========================================================================
REM  -Off : put the layer json back to locked (undo an old unlock-only setup)
REM ===========================================================================
:DoOff
for %%i in ("%~dp0..") do set "ROOT=%%~fi"
set "QD=%ROOT%\x64\Development\qTinecmaTool.exe"
for %%i in ("%QD%") do set "QDDIR=%%~dpi"
set "JSON=%QDDIR%TinecmaTool.json"
set "BAK=%JSON%.bak"

echo.
echo [off] re-locking the layer json
echo       json: %JSON%

findstr /c:"enable_environment" "%JSON%" >nul 2>&1
if not errorlevel 1 (
    echo       already locked - nothing to do
    echo.
    pause
    exit /b 0
)

if not exist "%BAK%" (
    echo       ERROR: no backup at %BAK%
    echo.
    pause
    exit /b 1
)

copy /y "%BAK%" "%JSON%" >nul 2>&1
if errorlevel 1 (
    echo       ERROR: could not write the json - no access to Program Files
    echo              right click this script and choose "Run as administrator"
    echo.
    pause
    exit /b 2
)

findstr /c:"enable_environment" "%JSON%" >nul 2>&1
if errorlevel 1 (
    echo       WARNING: the restored json still has no enable_environment
) else (
    echo       locked - the layer only loads where the enable variable exists
)
echo.
pause
exit /b 0

REM ===========================================================================
REM  elevated helper: keep exactly one TinecmaTool entry, pointing at this tree
REM ===========================================================================
:DoRegister
for %%i in ("%~dp0..") do set "ROOT=%%~fi"
set "QD=%ROOT%\x64\Development\qTinecmaTool.exe"
for %%i in ("%QD%") do set "QDDIR=%%~dpi"
set "JSON=%QDDIR%TinecmaTool.json"

echo.
echo [register] %JSON%
%PS% "$j='%JSON%'; if(-not(Test-Path $j)){Write-Host '  json not found'; exit 1}; $p='SOFTWARE\Khronos\Vulkan\ImplicitLayers'; $k=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($p,$true); if(-not $k){$k=[Microsoft.Win32.Registry]::LocalMachine.CreateSubKey($p)}; @($k.GetValueNames()) | Where-Object { $_ -match 'Tinecma|renderdoc' } | ForEach-Object { $k.DeleteValue($_,$false); Write-Host ('  removed ' + $_) }; $k.SetValue($j,0,[Microsoft.Win32.RegistryValueKind]::DWord); $k.Close(); Write-Host '  added this tree'"
echo.
exit /b 0
