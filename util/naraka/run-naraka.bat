@echo off
setlocal EnableExtensions EnableDelayedExpansion
title TinecmaTool - Naraka: Bladepoint

rem ===========================================================================
rem  One-click TinecmaTool launcher for Naraka: Bladepoint.
rem
rem    run-naraka.bat              preflight, start the game, watch it come up
rem    run-naraka.bat status       environment report only, starts nothing
rem    run-naraka.bat capture      write the capture trigger file
rem    run-naraka.bat proxy        deploy proxies then start [BLOCKED - see below]
rem    run-naraka.bat undeploy     remove the proxies, restore the game dir
rem    run-naraka.bat help         this text
rem
rem  Two things here are the opposite of the Steam-Cod launcher, both measured:
rem
rem  1. DO NOT ELEVATE. Started from an elevated parent, StartGame.exe brings up
rem     NeacClient.exe but NarakaBladepoint.exe never appears and Player.log is
rem     never touched. Through explorer.exe - medium integrity - the game starts
rem     normally. So this script never asks for admin; when it detects that it is
rem     already elevated it hands the launch to explorer instead.
rem
rem  2. The proxy route is refused unless asked for twice. Putting dxgi/d3d11/
rem     d3d12 into the game directory works technically - the game reaches
rem     "Graphics first present" with wrapped devices - but the protection sees
rem     the files in the install, warns on entering the game and shuts the
rem     graphics devices down. See docs/NARAKA_代理层实测_2026-11-02.md
rem
rem  The start target is StartGame.exe, not NarakaBladepoint.exe: the renderer
rem  expects the launch ticket that StartGame hands it. It also is not
rem  LauncherGame.exe, which is an x86 shell that exits within a second.
rem ===========================================================================

set "HERE=%~dp0"
rem repo root: this file lives in util\naraka
for %%i in ("%HERE%..\..") do set "ROOT=%%~fi\"
set "TOOL=%ROOT%x64\Development\TinecmaToolcmd.exe"
set "CORE=%ROOT%x64\Development\TinecmaTool.dll"
set "PROXYDIR=%ROOT%tools\proxy"
set "DEPLOY=%HERE%Deploy-NarakaProxy.ps1"

set "GAMEDIR=F:\Naraka\program"
set "STARTER=StartGame.exe"
set "TARGET=NarakaBladepoint.exe"

set "WORK=%LOCALAPPDATA%\Temp\Naraka\proxy"
set "CAPTURES=%WORK%\captures"
set "TRIGGER=%CAPTURES%\trigger_capture_narakabladepoint"
set "RUNLOG=%WORK%\launcher.log"

set "MODE=%~1"
if "%MODE%"=="" set "MODE=launch"
if /i "%MODE%"=="-h" set "MODE=help"
if /i "%MODE%"=="--help" set "MODE=help"
if /i "%MODE%"=="/?" set "MODE=help"

rem elevation matters in the opposite direction here - see the header
net session >nul 2>&1
if errorlevel 1 (set "ELEVATED=0") else (set "ELEVATED=1")

if /i "%MODE%"=="help"     goto :usage
if /i "%MODE%"=="status"   goto :status
if /i "%MODE%"=="capture"  goto :capture
if /i "%MODE%"=="proxy"    goto :proxy
if /i "%MODE%"=="undeploy" goto :undeploy
if /i "%MODE%"=="inject"   goto :inject
if /i "%MODE%"=="launch"   goto :preflight
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

if not exist "%WORK%"  mkdir "%WORK%"      >nul 2>&1
if not exist "%CAPTURES%" mkdir "%CAPTURES%" >nul 2>&1
>>"%RUNLOG%" echo ===== %DATE% %TIME% mode=%MODE% elevated=%ELEVATED% =====

call :checkdeployed

rem a leftover trigger file from an earlier session would fire a capture on
rem frame 0, while the engine is still compiling pipelines - the watchdog-stall
rem pattern. Remove it before the game starts.
if exist "%TRIGGER%" del /q "%TRIGGER%" >nul 2>&1

rem game-over-recording hooks the same swapchain we do
tasklist /FI "IMAGENAME eq CCGameRecord.exe" /FO CSV /NH 2>nul | findstr /i "CCGameRecord" >nul
if not errorlevel 1 (
  echo [warn] the in-game recorder is running. It hooks the same DXGI entry
  echo [warn] points; close it before capturing or the capture comes out empty.
)

call :launchgame
goto :watch

rem ---------------------------------------------------------------------------
:launchgame
echo [launch] %GAMEDIR%\%STARTER%
call :log "[launch] %STARTER% from %GAMEDIR% elevated=%ELEVATED%"
if "%ELEVATED%"=="1" goto :launch_via_explorer

pushd "%GAMEDIR%"
start "" "%GAMEDIR%\%STARTER%"
popd
goto :eof

:launch_via_explorer
rem explorer.exe is already running at medium integrity, so a process started
rem through it inherits that instead of our high-integrity token.
echo [note] this shell is elevated. Launching through explorer so the game
echo [note] runs at medium integrity - an elevated launch leaves NeacClient
echo [note] up but NarakaBladepoint.exe never appears.
set "LAUNCHCMD=%WORK%\naraka_launch.cmd"
>"%LAUNCHCMD%" echo @echo off
>>"%LAUNCHCMD%" echo cd /d "%GAMEDIR%"
>>"%LAUNCHCMD%" echo start "" "%GAMEDIR%\%STARTER%"
explorer.exe "%LAUNCHCMD%"
goto :eof

rem ---------------------------------------------------------------------------
:watch
call :log "[wait] watching for %TARGET%"
echo [wait] waiting for %TARGET% ...

set "WAITS=0"
set "HEART=0"
:waitloop
if !WAITS! geq 240 (
  echo [warn] gave up after about 4 minutes, %TARGET% never appeared.
  echo [hint] check the starter is still running, and see:
  echo [hint]   %WORK%\naraka_proxy.log
  call :log "[warn] gave up, target never appeared"
  goto :fail
)
set /a WAITS+=1

set "PID="
for /f "tokens=2 delims=," %%P in ('tasklist /FI "IMAGENAME eq %TARGET%" /FO CSV /NH 2^>nul ^| findstr /i "%TARGET%"') do (
  if not defined PID set "PID=%%~P"
)
if defined PID goto :up

set /a HEART+=1
if !HEART! geq 4 (
  set "HEART=0"
  call :heartbeat
)
ping -n 2 127.0.0.1 >nul
goto :waitloop

:heartbeat
set "SHELL="
for /f "tokens=2 delims=," %%P in ('tasklist /FI "IMAGENAME eq %STARTER%" /FO CSV /NH 2^>nul ^| findstr /i "%STARTER%"') do (
  if not defined SHELL set "SHELL=%%~P"
)
if defined SHELL (
  echo [wait] +%WAITS%s  %STARTER% pid=%SHELL% is up, waiting for %TARGET% ...
) else (
  echo [wait] +%WAITS%s  nothing running yet - did the game window come up?
)
goto :eof

rem ---------------------------------------------------------------------------
:up
call :log "[found] %TARGET% pid=%PID%"
echo.
echo [ok] %TARGET% is running, pid=%PID%
call :checkdeployed
echo.
if exist "%WORK%\renderdoc_core.log" (
  echo [log] capture core log:
  powershell -NoProfile -Command "$p='%WORK%\renderdoc_core.log'; $i=Get-Item $p; $age=((Get-Date)-$i.LastWriteTime).TotalMinutes; if ($age -lt 10) { 'this session, last lines:'; Get-Content $p -Tail 6 } else { 'stale - last written ' + $i.LastWriteTime + ', no capture layer loaded this run' }" 2>nul
  echo.
)
echo [next] get into the game until it is actually rendering, then either:
echo [next]   press F12
echo [next]   or run:  %~nx0 capture
echo [next] captures land in: %CAPTURES%
echo [next] proxy log:        %WORK%\naraka_proxy.log
goto :done

rem ---------------------------------------------------------------------------
:capture
if not exist "%CAPTURES%" mkdir "%CAPTURES%" >nul 2>&1
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
for /l %%N in (1,1,40) do (
  if exist "%CAPTURES%\capture_done.txt" goto :capture_done
  ping -n 2 127.0.0.1 >nul
)
echo [warn] no completion signal after about 40s.
echo [hint] the trigger only works while our layer is loaded in the game. If the
echo [hint] proxy is not deployed there is nothing to trigger - see:  %~nx0 status
goto :fail

:capture_done
set "RDC="
for %%F in ("%CAPTURES%\*.rdc") do set "RDC=%%~fF"
echo.
echo [ok] capture complete: !RDC!
echo [ok] open it with:  "%ROOT%x64\Development\qTinecmaTool.exe" "!RDC!"
goto :done

rem ---------------------------------------------------------------------------
:proxy
echo.
echo  ==========================================================================
echo   PROXY DEPLOYMENT IS BLOCKED BY THE ANTI-CHEAT
echo  ==========================================================================
echo   Measured 2026-11-02: with dxgi/d3d11/d3d12 in the game directory the
echo   game did start and did render with wrapped devices - "Graphics first
echo   present" was logged. Then the protection warned, tore the graphics
echo   devices down and the process ended. No capture was produced.
echo.
echo   The three DLLs sitting in the install directory is what gets seen.
echo   Everything this route needs already works; it is the visibility that
echo   does not.
echo.
echo   Details: docs\NARAKA_代理层实测_2026-11-02.md
echo  ==========================================================================
echo.
choice /c YN /n /m "  deploy anyway and accept the warning plus the ban risk? [Y/N] "
if errorlevel 2 goto :done
echo.
echo  Type DEPLOY to confirm:
set "ANSWER="
set /p "ANSWER=> "
if /i not "%ANSWER%"=="DEPLOY" (
  echo [abort] not confirmed.
  goto :done
)
call :log "[proxy] deploying with -ForceProtected"
powershell -NoProfile -ExecutionPolicy Bypass -File "%DEPLOY%" -ForceProtected -Apply
if errorlevel 1 (
  echo [error] deployment failed - see the output above.
  goto :fail
)
goto :preflight

rem ---------------------------------------------------------------------------
:undeploy
echo [undeploy] removing proxies from %GAMEDIR%
call :log "[undeploy]"
powershell -NoProfile -ExecutionPolicy Bypass -File "%DEPLOY%" -Undeploy -Apply
if errorlevel 1 (
  echo [error] undeploy failed - see the output above.
  goto :fail
)
echo.
echo [ok] game directory restored.
goto :done

rem ---------------------------------------------------------------------------
:inject
rem Inject the capture core into the renderer as soon as it appears.
rem
rem This is the one route that does not put files in the game directory. It also
rem has to win a race: the renderer appears about 12s after the starter is
rem launched and creates its D3D12 device about 18s after that. Miss the window
rem and the device exists before the layer does, which is unrecoverable for that
rem session - RenderDoc can only wrap a device it saw being created.
rem
rem Two known risks, both recorded rather than hidden:
rem   - VMProtect restores its own import table during unpacking, which is what
rem     made the D3D11 IAT path report zero resolved functions in the 10-24
rem     session. Whether the D3D12 path survives that is exactly what this run
rem     is meant to find out.
rem   - remote-thread injection into a protected process may be detected. If the
rem     game warns or exits, stop - do not retry.
if not exist "%TOOL%" (
  echo [error] toolcmd not found: %TOOL%
  goto :fail
)
if not exist "%WORK%"     mkdir "%WORK%"     >nul 2>&1
if not exist "%CAPTURES%" mkdir "%CAPTURES%" >nul 2>&1
>>"%RUNLOG%" echo ===== %DATE% %TIME% mode=inject elevated=%ELEVATED% =====

set "ANY="
for %%F in (dxgi.dll d3d11.dll d3d12.dll) do (
  if exist "%GAMEDIR%\%%F" set "ANY=1"
)
if defined ANY (
  echo [error] proxies are still deployed in %GAMEDIR%
  echo [hint]  with them present the game loads the proxy and the core skips its
  echo [hint]  IAT hooks, so injecting would do nothing. Run:  %~nx0 undeploy
  goto :fail
)

if exist "%TRIGGER%" del /q "%TRIGGER%" >nul 2>&1

call :launchgame
echo.
echo [inject] waiting for %TARGET% to appear, then injecting immediately ...
call :log "[inject] waiting for target"

set "WAITS=0"
set "PID="
:injectwait
set "PID="
for /f "tokens=2 delims=," %%P in ('tasklist /FI "IMAGENAME eq %TARGET%" /FO CSV /NH 2^>nul ^| findstr /i "%TARGET%"') do (
  if not defined PID set "PID=%%~P"
)
if defined PID goto :doinject
set /a WAITS+=1
if !WAITS! geq 300 (
  echo [warn] gave up after about 5 minutes, %TARGET% never appeared.
  goto :fail
)
ping -n 2 127.0.0.1 >nul
goto :injectwait

:doinject
call :log "[inject] found pid=!PID! after ~!WAITS!s"
echo [inject] %TARGET% pid=!PID!
echo [inject] injecting now - the device window is roughly 18s from here
"%TOOL%" inject --PID=!PID! -c "%CAPTURES%\naraka"
set "RC=!errorlevel!"
call :log "[inject] rc=!RC!"
if not "!RC!"=="0" goto :injectfail

echo.
echo [ok] injected into pid !PID!
echo.
echo [wait] watching the core log for device wrapping, up to 90s ...
set "NEWLOG="
for /f "delims=" %%F in ('dir /b /o-d "%LOCALAPPDATA%\Temp\TinecmaTool\RenderDoc_*.log" 2^>nul') do (
  if not defined NEWLOG set "NEWLOG=%LOCALAPPDATA%\Temp\TinecmaTool\%%F"
)
if not defined NEWLOG (
  echo [warn] no RenderDoc log yet - the core may not have initialised.
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
  echo [hint] the IAT hook did not survive - VMProtect restores its import table
  echo [hint] during unpacking, which is the failure recorded for D3D11 on 10-24.
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
echo [next] get into the game until it is actually rendering, then:
echo [next]   press F12, or run:  %~nx0 capture
echo [next] captures land in: %CAPTURES%
goto :done

:injectfail
echo.
echo [fail] injection returned !RC!
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
echo  TinecmaTool - Naraka: Bladepoint
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
if exist "%PROXYDIR%\dxgi.dll"  (echo     proxies   OK) else (echo     proxies   not built - run tools\proxy\build.cmd)
echo.
echo   core bridge exports for the D3D12 factory path:
powershell -NoProfile -Command "if (Test-Path '%CORE%') { $t=[Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes('%CORE%')); foreach ($n in 'TINECMATOOL_CreateDXGIFactory','TINECMATOOL_D3D12CreateDevice','TINECMATOOL_D3D11CreateDevice') { '{0,-38} {1}' -f $n, $(if ($t.Contains($n)) {'yes'} else {'no'}) } }" 2>nul
echo.
echo   deployment in the game directory:
set "DEPLOYED=0"
for %%F in (dxgi.dll d3d11.dll d3d12.dll) do (
  if exist "%GAMEDIR%\%%F" (
    echo     %%F present
    set "DEPLOYED=1"
  )
)
if "!DEPLOYED!"=="0" echo     clean - no proxy files in the install
echo.
call :processes
goto :done

rem ---------------------------------------------------------------------------
:checkdeployed
set "ANY="
for %%F in (dxgi.dll d3d11.dll d3d12.dll) do (
  if exist "%GAMEDIR%\%%F" set "ANY=1"
)
if not defined ANY (
  echo [state] no proxy in the game directory. Starting the game will not load
  echo [state] any capture layer - no capture will be possible this run.
  exit /b 0
)
echo [state] proxies ARE present in the game directory. Expect the protection to
echo [state] warn and shut the graphics devices down. See: %~nx0 status
exit /b 0

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

:usage
echo.
echo  TinecmaTool + Naraka: Bladepoint
echo.
echo    %~nx0              start the game and watch it come up   [default]
echo    %~nx0 inject       start, then inject the renderer at first sight
echo    %~nx0 status       environment report, starts nothing
echo    %~nx0 capture      write the capture trigger file
echo    %~nx0 proxy        deploy proxies then start   [blocked by anti-cheat]
echo    %~nx0 undeploy     remove the proxies, restore the game dir
echo    %~nx0 help         this text
echo.
echo  start target : %GAMEDIR%\%STARTER%
echo  renderer     : %TARGET%
echo  captures     : %CAPTURES%
echo.
echo  Never elevated: an elevated launch keeps NarakaBladepoint.exe from ever
echo  appearing. This script drops to medium integrity through explorer instead.
echo.
goto :done

:fail
echo.
echo Script stopped.
pause
endlocal
exit /b 1

:done
endlocal
exit /b 0
