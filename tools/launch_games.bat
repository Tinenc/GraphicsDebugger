@echo off
setlocal

REM ===========================================================================
REM  launch_games.bat  --  one click, elevated: register the layer, start the
REM                        tool, and launch Games.exe under capture
REM
REM  Why this exists
REM  ---------------
REM  launch_endfield.bat prepares the environment and opens qTinecmaTool, but you
REM  still have to fill in the Executable field, tick "Capture Child Processes"
REM  and press Launch. This script does all of that through the command line:
REM
REM      TinecmaToolcmd.exe capture --opt-hook-children <Games.exe>
REM
REM  --opt-hook-children is the command line name for what the GUI calls
REM  "Capture Child Processes" (renderdoccmd.cpp: opts.hookIntoChildren = true).
REM  Without it the injection stops at Games.exe and never reaches Endfield.exe.
REM
REM  Elevation
REM  ---------
REM  Injection into the launcher needs admin rights, and so does fixing the HKLM
REM  layer registration if it drifted. Rather than stop half way with a UAC
REM  prompt, the script re-launches itself elevated once at the start.
REM  Pass -NoAdmin to skip that (useful when comparing behaviour).
REM
REM  Deliberately NOT set: TCM_FORCE_CHILD_INJECT
REM  -------------------------------------------
REM  That variable is read by ShouldInject() and short-circuits the
REM  hookIntoChildren check. TinecmaToolcmd.exe itself also loads TinecmaTool.dll
REM  and registers the CreateProcess hook, so with the variable set the command
REM  would inject into Games.exe twice - once from its own hook, once from
REM  ExecuteAndInject. --opt-hook-children is enough on this path: the options are
REM  pushed through INTERNAL_SetCaptureOptions before the target thread resumes,
REM  so there is no race to compensate for.
REM
REM  Only uses the build in this tree.
REM ===========================================================================

if /i "%~1"=="-elevated" goto :Main
if /i "%~1"=="-NoAdmin" goto :Main

REM --- are we already elevated? ----------------------------------------------
REM A marker argument keeps this from looping even if the check itself fails.
net session >nul 2>&1
if not errorlevel 1 goto :Main

echo ==========================================================
echo   Administrator rights required - asking for elevation
echo ==========================================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -ArgumentList '-elevated' -Verb RunAs"
exit /b 0

:Main
set "TOOLS=%~dp0"
REM Normalise: "%~dp0.." keeps the "..", and every path we compare against the
REM registry or against Process.Path is fully resolved. Without this the layer
REM registration check always reports NEED and rewrites HKLM on every run, and
REM the "is this tree already running" path comparison never matches.
for %%i in ("%~dp0..") do set "ROOT=%%~fi"
set "DEV=%ROOT%\x64\Development"
set "QD=%DEV%\qTinecmaTool.exe"
set "QCMD=%DEV%\TinecmaToolcmd.exe"
set "JSON=%DEV%\TinecmaTool.json"
set "BAK=%JSON%.bak"
set "GAMES=H:\Hypergryph Launcher\1.6.0\Games.exe"
set "GAMESDIR=H:\Hypergryph Launcher\1.6.0"
set "CAP=%TEMP%\TinecmaTool\endfield"
set "REGQ=%TEMP%\_tcm_reg.txt"
set "PS=powershell -NoProfile -ExecutionPolicy Bypass -Command"

echo ==========================================================
echo   Endfield capture - one click launch
echo ==========================================================
echo.

for %%f in ("%QD%" "%QCMD%" "%JSON%") do (
    if not exist "%%~f" (
        echo   ERROR: missing build output:
        echo          %%~f
        echo          Build renderdoc.sln in this tree first.
        echo.
        pause
        exit /b 2
    )
)
if not exist "%GAMES%" (
    echo   ERROR: launcher UI not found:
    echo          %GAMES%
    echo.
    pause
    exit /b 3
)

net session >nul 2>&1
if errorlevel 1 (
    echo   note: running WITHOUT admin - injection may fail
) else (
    echo   elevated: yes
)
echo   tool   : %QCMD%
echo   target : %GAMES%
echo   captures: %CAP%_frame*.rdc
echo.

REM ---- 1. the json must be LOCKED -------------------------------------------
REM Injection delivers the enable variable inside the game process, so the layer
REM must not load unconditionally. Undo any unlock left over from older setups.
echo [1/5] checking the Vulkan layer json ...
findstr /c:"enable_environment" "%JSON%" >nul 2>&1
if not errorlevel 1 (
    echo         locked, as it should be
) else (
    if exist "%BAK%" (
        copy /y "%BAK%" "%JSON%" >nul 2>&1
        findstr /c:"enable_environment" "%JSON%" >nul 2>&1
        if errorlevel 1 (
            echo         WARNING: could not re-lock the json
        ) else (
            echo         re-locked from backup
        )
    ) else (
        echo         WARNING: json is unlocked and no backup exists at
        echo                  %BAK%
        echo                  Every Vulkan program on this machine will load the layer.
    )
)
echo.

REM ---- 2. this tree's json must be the registered implicit layer ------------
REM The layer name is global. A leftover entry pointing at another tree would make
REM the loader pick the wrong DLL. We are already elevated, so fix it in place
REM instead of spawning another UAC prompt.
echo [2/5] checking the Vulkan layer registration ...
set "REGQ_CMD=$j='%JSON%'; $k=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SOFTWARE\Khronos\Vulkan\ImplicitLayers'); if(-not $k){'NEED'}else{$n=@($k.GetValueNames() | Where-Object { $_ -match 'Tinecma|renderdoc' }); if($n.Count -eq 1 -and $n[0] -eq $j){'OK'}else{'NEED'}}"
%PS% "%REGQ_CMD%" > "%REGQ%" 2>&1
findstr /x /c:"OK" "%REGQ%" >nul 2>&1
if not errorlevel 1 (
    echo         registered, exactly one entry, points here
    goto :RegDone
)
echo         currently registered:
%PS% "$k=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey('SOFTWARE\Khronos\Vulkan\ImplicitLayers'); if($k){ $a=@($k.GetValueNames() | Where-Object { $_ -match 'Tinecma|renderdoc' }); if($a.Count -eq 0){Write-Host '          (none)'}else{ $a | ForEach-Object { $t=$_; $m=''; if(-not(Test-Path $t)){$m='  [missing file]'}; Write-Host ('          ' + $t + $m) } } }"
net session >nul 2>&1
if errorlevel 1 (
    echo         WARNING: not elevated, cannot rewrite HKLM. The layer will not
    echo                  reach the game. Re-run without -NoAdmin.
    goto :RegDone
)
%PS% "$j='%JSON%'; $p='SOFTWARE\Khronos\Vulkan\ImplicitLayers'; $k=[Microsoft.Win32.Registry]::LocalMachine.OpenSubKey($p,$true); if(-not $k){$k=[Microsoft.Win32.Registry]::LocalMachine.CreateSubKey($p)}; @($k.GetValueNames()) | Where-Object { $_ -match 'Tinecma|renderdoc' } | ForEach-Object { $k.DeleteValue($_,$false); Write-Host ('          removed ' + $_) }; $k.SetValue($j,0,[Microsoft.Win32.RegistryValueKind]::DWord); $k.Close(); Write-Host '          added this tree'"
:RegDone
del "%REGQ%" >nul 2>&1
echo.

REM ---- 3. start the UI ------------------------------------------------------
REM The UI is what you press F12 in and what you attach from. It is NOT used to
REM launch anything here - the command line below does that. Leaving it out is
REM fine too, you just cannot trigger captures interactively.
echo [3/5] starting qTinecmaTool UI ...
%PS% "$qd='%QD%'; $all=@(Get-Process -Name qTinecmaTool -ErrorAction SilentlyContinue); $mine=@($all | Where-Object { $_.Path -eq $qd }); if($mine.Count -eq 0){Write-Host '        starting a fresh instance'}else{ Write-Host ('        already running, PID ' + (($mine | ForEach-Object { $_.Id }) -join ',')) }"
start "" "%QD%"
%PS% "Start-Sleep -Milliseconds 2500"
echo.

REM ---- 4. clear stale processes ---------------------------------------------
REM A launcher that is already up was started without our DLL - the CreateProcess
REM hook lives in the process, so an old instance can never inject into the game
REM it spawns. Kill only what belongs to this game, never a shared taskkill.
echo [4/5] clearing old launcher / game processes ...
%PS% "$names=@('Endfield','Games','Launcher','PlatformProcess'); $hit=@(); foreach($n in $names){ $hit += @(Get-Process -Name $n -ErrorAction SilentlyContinue) }; if($hit.Count -eq 0){Write-Host '        nothing was running'}else{ $hit | ForEach-Object { try{ $p=$_; Stop-Process -Id $p.Id -Force; Write-Host ('        stopped ' + $p.ProcessName + ' PID ' + $p.Id) }catch{} } }"
echo.

REM ---- 5. launch it under capture -------------------------------------------
echo [5/5] launching under capture ...
echo.
echo ----------------------------------------------------------
echo   TinecmaToolcmd capture --opt-hook-children Games.exe
echo ----------------------------------------------------------
"%QCMD%" capture --opt-hook-children -c "%CAP%" -d "%GAMESDIR%" "%GAMES%"
set "RC=%ERRORLEVEL%"
echo.

if not "%RC%"=="0" (
    echo   WARNING: the command returned %RC% - see the output above.
    echo            Common causes:
    echo              - already-elevated UI holding port 38920
    echo              - json unlocked and another tree's layer winning
    echo              - launcher refusing to run elevated
    echo.
)

echo ==========================================================
echo   Next
echo ==========================================================
echo   1. the Hypergryph launcher window should appear
echo   2. click "start game" and get into the world
echo   3. in qTinecmaTool: Attach to Running Instance -^> Endfield.exe
echo      (port 3892x, Endfield.exe is the one that owns its own port)
echo   4. press F12 in game
echo.
echo   Verify
echo     netstat -ano ^| findstr "3892"
echo       Games.exe and Endfield.exe should each own one
echo     %TEMP%\TinecmaTool\RenderDoc*.log
echo       look for "Loading into ...\Endfield.exe" and "RegisterSurface()"
echo       Games.exe should say "Relay process (not endfield.exe)"
echo.
echo   The launcher window going black means the relay/graphics split is missing
echo   from this build - rebuild so win32_libentry.cpp picks RegisterProcessHooks.
echo.
echo   This window closes itself in 30 seconds.
echo.
timeout /t 30 /nobreak >nul
exit /b 0
