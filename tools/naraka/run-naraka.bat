@echo off
setlocal EnableExtensions EnableDelayedExpansion
title TinecmaTool - Naraka: Bladepoint

rem ===========================================================================
rem  TinecmaTool launcher for Naraka: Bladepoint.
rem
rem    run-naraka.bat             start the game and watch it come up
rem    run-naraka.bat inject      start, then inject the renderer at first sight
rem    run-naraka.bat capture     write the capture trigger file
rem    run-naraka.bat tool        open TinecmaTool (the analyzer) only
rem    run-naraka.bat status      environment report, starts nothing
rem    run-naraka.bat gui         open the graphical control center
rem    run-naraka.bat help        this text
rem
rem  Every mode takes an optional target as its second argument:
rem    pc      Naraka: Bladepoint        F:\Naraka\program        [default]
rem    mobile  Naraka: Bladepoint Mobile F:\NarakaMobile\game
rem  e.g.  run-naraka.bat inject mobile
rem
rem  Naraka-Launcher.bat opens the GUI, which is the normal way in. This file is
rem  the same thing without a window, and is what the GUI calls.
rem
rem  DO NOT ELEVATE. Measured 2026-11-02: started from an elevated parent,
rem  StartGame.exe brings up NeacClient.exe but NarakaBladepoint.exe never
rem  appears and Player.log is never touched. Through explorer.exe - medium
rem  integrity - the game starts normally. So this script never asks for admin;
rem  when it finds itself elevated it hands the launch to explorer instead.
rem
rem  The start target is StartGame.exe, not NarakaBladepoint.exe: the renderer
rem  needs the launch ticket StartGame hands it. It is also not LauncherGame.exe,
rem  an x86 shell that exits within a second.
rem ===========================================================================

set "HERE=%~dp0"
rem repo root: this file lives in tools\naraka
for %%i in ("%HERE%..\..") do set "ROOT=%%~fi\"
set "TOOL=%ROOT%x64\Development\TinecmaToolcmd.exe"
set "CORE=%ROOT%x64\Development\TinecmaTool.dll"
set "VIEWER=%ROOT%x64\Development\qTinecmaTool.exe"

set "GAMEDIR=F:\Naraka\program"
set "STARTER=StartGame.exe"
set "TARGET=NarakaBladepoint.exe"
set "TGT=pc"

rem Historical name - the capture core resolves this path itself, so it is not
rem ours to rename. See the trigger watcher in renderdoc/os/win32/win32_libentry.cpp
set "WORK=%LOCALAPPDATA%\Temp\Naraka\proxy"
set "CAPTURES=%WORK%\captures"
set "TRIGGER=%CAPTURES%\trigger_capture_narakabladepoint"
set "RUNLOG=%WORK%\launcher.log"

set "MODE=%~1"
if "%MODE%"=="" set "MODE=launch"
if /i "%MODE%"=="-h" set "MODE=help"
if /i "%MODE%"=="--help" set "MODE=help"
if /i "%MODE%"=="/?" set "MODE=help"

rem Second argument picks the game. Same core and same route for both - only the
rem three names differ. Mobile is Unity/x64 as well, and its UnityPlayer also
rem resolves the graphics entry points at runtime, so nothing else changes.
set "TGT=%~2"

rem Also allow the target to be the first argument:  run-naraka.bat mobile
if /i "%MODE%"=="pc"     goto :tgt_as_mode_pc
if /i "%MODE%"=="mobile" goto :tgt_as_mode_mobile
goto :tgt_apply

:tgt_as_mode_pc
set "TGT=pc"
set "MODE=inject"
goto :tgt_apply

:tgt_as_mode_mobile
set "TGT=mobile"
set "MODE=inject"

:tgt_apply
if "%TGT%"=="" set "TGT=pc"
if /i "%TGT%"=="mobile" (
  set "LABEL=Naraka: Bladepoint Mobile"
  set "GAMEDIR=F:\NarakaMobile\game"
  set "STARTER=StartGame_l22.exe"
  set "TARGET=NarakaBladepointMobile.exe"
) else (
  set "TGT=pc"
  set "LABEL=Naraka: Bladepoint"
  set "GAMEDIR=F:\Naraka\program"
  set "STARTER=StartGame.exe"
  set "TARGET=NarakaBladepoint.exe"
)

rem elevation matters in the opposite direction here - see the header
net session >nul 2>&1
if errorlevel 1 (set "ELEVATED=0") else (set "ELEVATED=1")

if /i "%MODE%"=="help"    goto :usage
if /i "%MODE%"=="status"  goto :status
if /i "%MODE%"=="capture" goto :capture
if /i "%MODE%"=="tool"    goto :tool
if /i "%MODE%"=="inject"  goto :inject
if /i "%MODE%"=="gui"     goto :gui
if /i "%MODE%"=="launch"  goto :preflight
echo [error] unknown mode: %MODE%
goto :usage

rem ---------------------------------------------------------------------------
:preflight
if not exist "%GAMEDIR%\%STARTER%" (
  echo [error] starter not found:
  echo         %GAMEDIR%\%STARTER%
  echo [hint]  pass a different install by editing GAMEDIR at the top of this file.
  goto :fail
)

call :makedirs
>>"%RUNLOG%" echo ===== %DATE% %TIME% mode=%MODE% elevated=%ELEVATED% =====

call :hygiene

rem a leftover trigger file from an earlier session would fire a capture on frame
rem 0, while the engine is still compiling pipelines. Drop it before we start.
if exist "%TRIGGER%" del /q "%TRIGGER%" >nul 2>&1

rem the in-game recorder hooks the same swapchain we do
tasklist /FI "IMAGENAME eq CCGameRecord.exe" /FO CSV /NH 2>nul | findstr /i "CCGameRecord" >nul
if not errorlevel 1 (
  echo [warn] the in-game recorder is running. It hooks the same DXGI entry
  echo [warn] points; close it before capturing or the capture comes out empty.
)

call :launchgame
goto :watch

rem ---------------------------------------------------------------------------
:makedirs
if not exist "%WORK%"     mkdir "%WORK%"     >nul 2>&1
if not exist "%CAPTURES%" mkdir "%CAPTURES%" >nul 2>&1
goto :eof

rem ---------------------------------------------------------------------------
:hygiene
rem The capture path is injection now. A dxgi/d3d11/d3d12 left next to the game
rem would be loaded instead of the system ones, the core would skip its own hooks
rem for that process, and injection would silently achieve nothing.
set "STRAY="
for %%F in (dxgi.dll d3d11.dll d3d12.dll) do (
  if exist "%GAMEDIR%\%%F" set "STRAY=1"
)
if defined STRAY (
  echo [warn] capture DLLs found in the game directory. They get loaded instead
  echo [warn] of the system ones and injection will do nothing.
  echo [warn] remove dxgi.dll / d3d11.dll / d3d12.dll from %GAMEDIR%
)
goto :eof

rem ---------------------------------------------------------------------------
:launchgame
echo [launch] %GAMEDIR%\%STARTER%
call :log "[launch] %STARTER% from %GAMEDIR% elevated=%ELEVATED%"
call :startviewer
if "%ELEVATED%"=="1" goto :launch_via_explorer

pushd "%GAMEDIR%"
start "" "%GAMEDIR%\%STARTER%"
popd
goto :eof

:launch_via_explorer
rem explorer.exe already runs at medium integrity, so a process started through
rem it inherits that instead of our high-integrity token.
echo [note] this shell is elevated. Launching through explorer so the game runs
echo [note] at medium integrity - an elevated launch leaves NeacClient up but
echo [note] NarakaBladepoint.exe never appears.
set "LAUNCHCMD=%WORK%\naraka_launch.cmd"
>"%LAUNCHCMD%" echo @echo off
>>"%LAUNCHCMD%" echo cd /d "%GAMEDIR%"
>>"%LAUNCHCMD%" echo start "" "%GAMEDIR%\%STARTER%"
explorer.exe "%LAUNCHCMD%"
goto :eof

:startviewer
rem Bring the analyzer up with the game. One instance only - a second copy just
rem gives you two replay contexts and no way to tell which is which.
rem
rem When this shell is elevated the viewer has to go through explorer too: a Qt
rem main window started from a high-integrity process comes up and then disappears
rem a few seconds later, right after driver enumeration, with nothing in the event
rem log. Same exe, medium integrity: stays up.
if not exist "%VIEWER%" (
  echo [note] analyzer not built, skipping: %VIEWER%
  goto :eof
)
tasklist /FI "IMAGENAME eq qTinecmaTool.exe" /FO CSV /NH 2>nul | findstr /i "qTinecmaTool" >nul
if not errorlevel 1 (
  echo [note] TinecmaTool already running
  goto :eof
)
if "%ELEVATED%"=="1" (
  set "VCMD=%WORK%\naraka_viewer.cmd"
  >"!VCMD!" echo @echo off
  >>"!VCMD!" echo start "" "%VIEWER%"
  explorer.exe "!VCMD!"
) else (
  start "" "%VIEWER%"
)
echo [ok] opened TinecmaTool
goto :eof

rem ---------------------------------------------------------------------------
:watch
call :log "[wait] watching for %TARGET%"
echo [wait] waiting for %TARGET% ...

set "WAITS=0"
:waitloop
set "PID="
for /f "tokens=2 delims=," %%P in ('tasklist /FI "IMAGENAME eq %TARGET%" /FO CSV /NH 2^>nul ^| findstr /i "%TARGET%"') do (
  if not defined PID set "PID=%%~P"
)
if defined PID goto :up

set /a WAITS+=1
if !WAITS! geq 240 (
  echo [warn] gave up after about 4 minutes, %TARGET% never appeared.
  echo [hint] check %GAMEDIR%\log\Neac.log and the launcher log:
  echo [hint]   %RUNLOG%
  goto :fail
)
if !WAITS! geq 15 (
  set "WAITS=0"
  echo [wait] still waiting ...
)
ping -n 2 127.0.0.1 >nul
goto :waitloop

:up
echo [ok] %TARGET% pid !PID! - the game is up.
echo.
echo [state] no capture layer in this run - starting is all this mode does.
echo [state] to capture, use:  %~nx0 inject
goto :done

rem ---------------------------------------------------------------------------
:tool
rem Just the analyzer, no game. Launched with the game from :launchgame as well.
call :makedirs
call :startviewer
goto :done

rem ---------------------------------------------------------------------------
:capture
call :makedirs
set "PID="
for /f "tokens=2 delims=," %%P in ('tasklist /FI "IMAGENAME eq %TARGET%" /FO CSV /NH 2^>nul ^| findstr /i "%TARGET%"') do (
  if not defined PID set "PID=%%~P"
)
if not defined PID (
  echo [error] %TARGET% is not running - nothing to capture.
  goto :fail
)
echo [capture] pid=%PID%
>"%TRIGGER%" echo CAPTURE
echo [capture] trigger written: %TRIGGER%
echo [capture] waiting for the .rdc ...
echo [tip] if nothing happens, click the game window and press F12 - that is
echo [tip] RenderDoc's default capture key and needs no rebuild. It only fires
echo [tip] while the game window is foreground.
echo [tip] This file trigger needs a core built after 5c2c7436c instead.

rem The trigger only does anything while a capture layer is inside the game, and
rem only a core built after 5c2c7436c watches for it. If nothing arrives those two
rem are the first things to check - see:  %~nx0 status
for /l %%N in (1,1,40) do (
  if exist "%CAPTURES%\capture_done.txt" goto :capture_done
  ping -n 2 127.0.0.1 >nul
)
echo [warn] no completion signal after about 40s.
echo [hint] press F12 in the game - the default capture key, works without a rebuild.
echo [hint] is the layer injected in this session? see:  %~nx0 status
goto :fail

:capture_done
set "RDC="
for %%F in ("%CAPTURES%\*.rdc") do set "RDC=%%~fF"
echo.
echo [ok] capture complete: !RDC!
goto :done

rem ---------------------------------------------------------------------------
:gui
if not exist "%HERE%Naraka-Launcher.bat" (
  echo [error] Naraka-Launcher.bat not found next to this file.
  goto :fail
)
call "%HERE%Naraka-Launcher.bat"
goto :done

rem ---------------------------------------------------------------------------
:inject
rem Inject the capture core into the renderer as soon as it appears.
rem
rem This is the only route that does not put files in the game directory. It also
rem has to win a race: the renderer appears 8-12s after the starter, and creates
rem its D3D12 device some 16s after that (measured 19:08 on 2026-11-02: process
rem 19:08:38, wrapped device 19:08:54). Miss the window and the device exists
rem before the layer does - unrecoverable for that session, because RenderDoc can
rem only wrap a device it saw being created.
if not exist "%TOOL%" (
  echo [error] toolcmd not found: %TOOL%
  goto :fail
)
call :makedirs
>>"%RUNLOG%" echo ===== %DATE% %TIME% mode=inject elevated=%ELEVATED% =====
call :hygiene
if exist "%TRIGGER%" del /q "%TRIGGER%" >nul 2>&1

call :launchgame
echo.
echo [inject] waiting for %TARGET%, then injecting the moment it appears ...
call :log "[inject] waiting for target"

rem The wait lives in wait-and-inject.ps1. A batch loop built from tasklist plus
rem ping took about ten seconds just to notice the process; by then the device
rem already existed and the injection was wasted. The script polls Get-Process
rem every 100ms in a single process and reports its own timings.
powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%wait-and-inject.ps1" -ProcessName "%TARGET%" -ToolCmd "%TOOL%" -CaptureDir "%CAPTURES%"
if errorlevel 1 goto :injectfail
call :log "[inject] injected"

echo.
echo [ok] injected - the layer is inside the renderer now
echo.
echo [wait] watching the core log for device wrapping, up to 90s ...
set "NEWLOG="
for /f "delims=" %%F in ('dir /b /o-d "%LOCALAPPDATA%\Temp\TinecmaTool\RenderDoc_app_*.log" 2^>nul') do (
  if not defined NEWLOG set "NEWLOG=%LOCALAPPDATA%\Temp\TinecmaTool\%%F"
)
if not defined NEWLOG (
  echo [warn] no core log yet - the layer may not have initialised.
  goto :injectdone
)
echo [log] !NEWLOG!
call :log "[inject] log=!NEWLOG!"

set "TICKS=0"
:watchloop
findstr /c:"created wrapped device" "!NEWLOG!" >nul 2>&1
if not errorlevel 1 (
  echo.
  echo [OK] device wrapped - the layer is in the pipeline.
  goto :watchpresent
)
set /a TICKS+=3
if !TICKS! geq 90 (
  echo.
  echo [warn] 90s and no wrapped device.
  echo [hint] check "Export redirect" in the log: that redirect is what makes a
  echo [hint] wrapped device possible. No redirect lines means the export table
  echo [hint] was not patched and nothing intercepts device creation.
  echo [hint] last lines:
  powershell -NoProfile -Command "Get-Content '!NEWLOG!' -Tail 8 -ErrorAction SilentlyContinue" 2>nul
  goto :injectdone
)
ping -n 4 127.0.0.1 >nul
goto :watchloop

:watchpresent
set "TICKS=0"
:presentloop
findstr /c:"Graphics first present" "!NEWLOG!" >nul 2>&1
if not errorlevel 1 (
  echo [OK] first present reached - the swapchain is hooked.
  goto :injectdone
)
set /a TICKS+=3
if !TICKS! geq 60 goto :injectdone
ping -n 4 127.0.0.1 >nul
goto :presentloop

:injectdone
echo.
echo [next] get into the game until it is actually rendering, then run:
echo [next]   %~nx0 capture
echo [next] captures land in: %CAPTURES%
goto :done

:injectfail
echo.
echo [fail] injection did not complete
echo [hint] usual causes, in order of likelihood:
echo [hint]  1. the anti-cheat stripped handle rights - OpenProcess works but
echo [hint]     WriteProcessMemory or CreateRemoteThread return ACCESS_DENIED.
echo [hint]  2. we were too late. The renderer appeared but had already created
echo [hint]     its device; a wrapped device can only be produced at creation.
echo [hint]  3. wrong process - the renderer is %TARGET%, not %STARTER%.
goto :fail

rem ---------------------------------------------------------------------------
:status
echo.
echo  TinecmaTool - %LABEL%
echo.
echo   repo      : %ROOT%
echo   game dir  : %GAMEDIR%
echo   starter   : %STARTER%
echo   target    : %TARGET%
echo   tool      : %TOOL%
echo   core      : %CORE%
echo   captures  : %CAPTURES%
echo   work dir  : %WORK%
echo.
if "%ELEVATED%"=="1" (
  echo   shell     : ELEVATED - the game will be launched through explorer so it
  echo               runs at medium integrity
) else (
  echo   shell     : medium integrity - good
)
echo.
echo   builds:
if exist "%CORE%" (echo     core      OK) else (echo     core      MISSING - build x64 first)
if exist "%TOOL%" (echo     toolcmd   OK) else (echo     toolcmd   MISSING - build x64 first)
echo.
echo   capture bridge exports:
powershell -NoProfile -Command "if (Test-Path '%CORE%') { $t=[Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes('%CORE%')); foreach ($n in 'TINECMATOOL_CreateDXGIFactory','TINECMATOOL_D3D12CreateDevice','TINECMATOOL_D3D11CreateDevice') { '{0,-38} {1}' -f $n, $(if ($t.Contains($n)) {'yes'} else {'no'}) } }" 2>nul
echo.
echo   install dir:
set "STRAY="
for %%F in (dxgi.dll d3d11.dll d3d12.dll) do (
  if exist "%GAMEDIR%\%%F" (echo     %%F present - breaks injection & set "STRAY=1")
)
if not defined STRAY echo     clean
echo.
call :processes
goto :done

rem ---------------------------------------------------------------------------
:processes
echo   processes:
set "SEEN=0"
for %%N in (%STARTER% %TARGET% NeacClient.exe) do (
  set "ONE="
  for /f "tokens=2 delims=," %%P in ('tasklist /FI "IMAGENAME eq %%N" /FO CSV /NH 2^>nul ^| findstr /i "%%N"') do (
    if not defined ONE set "ONE=%%~P"
  )
  if defined ONE (
    echo     %%N pid=!ONE!
    set "SEEN=1"
  )
)
if "!SEEN!"=="0" echo     none running
goto :eof

rem ---------------------------------------------------------------------------
:log
>>"%RUNLOG%" echo [%TIME%] %~1
goto :eof

rem ---------------------------------------------------------------------------
:usage
echo.
echo  TinecmaTool + Naraka: Bladepoint
echo.
echo    %~nx0 gui          open the graphical control center
echo    %~nx0              start the game and watch it come up   [default]
echo    %~nx0 inject       start, then inject the renderer at first sight
echo    %~nx0 capture      write the capture trigger file
echo    %~nx0 tool         open TinecmaTool (the analyzer) only
echo    %~nx0 status       environment report, starts nothing
echo    %~nx0 help         this text
echo.
echo  start target : %GAMEDIR%\%STARTER%
echo  renderer     : %TARGET%
echo  captures     : %CAPTURES%
echo.
echo  Second argument picks the game: pc (default) or mobile.
echo    %~nx0 inject mobile
echo.
echo  Never elevated: an elevated launch keeps NarakaBladepoint.exe from ever
echo  appearing. This script drops to medium integrity through explorer instead.
echo.
goto :done

rem ---------------------------------------------------------------------------
:fail
echo.
echo Script stopped.
pause
endlocal
exit /b 1

:done
endlocal
exit /b 0
