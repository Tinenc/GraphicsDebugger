@echo off
setlocal EnableExtensions EnableDelayedExpansion
title TinecmaTool - Steam COD (sp26-cod.exe)

rem ===========================================================================
rem  One-click TinecmaTool + Call of Duty HQ renderer.
rem
rem    run-steam-cod.bat           wait for sp26-cod.exe, inject it
rem    run-steam-cod.bat wait -w   same, then wait for the game to exit
rem    run-steam-cod.bat launch    start the renderer under the tool directly
rem    run-steam-cod.bat help      this text
rem
rem  The renderer is  sp26\sp26-cod.exe  -- NOT cod.exe.
rem  cod.exe is the HQ launcher: it loads dxgi.dll and never loads d3d12.dll.
rem  See docs/STEAM_COD_S4_TARGET_AND_LOAD_ORDER.md
rem ===========================================================================

set "HERE=%~dp0"
set "TOOL=%HERE%x64\Development\TinecmaToolcmd.exe"
set "GAMEDIR=F:\SteamLibrary\steamapps\common\Call of Duty HQ\sp26"
set "TARGET=sp26-cod.exe"
set "CAPTURES=%HERE%artifacts\steam-cod-captures"

set "MODE=%~1"
if "%MODE%"=="" set "MODE=wait"
rem everything after the mode, passed through to the tool (e.g. -w)
set "EXTRA="
if not "%~2"=="" for /f "tokens=1,*" %%a in ("%*") do set "EXTRA=%%b"

if /i "%MODE%"=="help"   goto :usage
if /i "%MODE%"=="-h"     goto :usage
if /i "%MODE%"=="--help" goto :usage

if not exist "%TOOL%" (
  echo [error] TinecmaToolcmd.exe not found:
  echo         %TOOL%
  echo         build the x64 target first.
  goto :fail
)

if not exist "%CAPTURES%" mkdir "%CAPTURES%" >nul 2>&1

if /i "%MODE%"=="launch" goto :launch
if /i "%MODE%"=="steam"  goto :steam
if /i "%MODE%"=="wait"   goto :wait
echo [error] unknown mode: %MODE%
goto :usage

rem ---------------------------------------------------------------------------
:launch
if not exist "%GAMEDIR%\%TARGET%" (
  echo [error] target not found: %GAMEDIR%\%TARGET%
  goto :fail
)
echo [launch] %GAMEDIR%\%TARGET%
echo [launch] note: the renderer normally expects to be started by the HQ
echo          launcher. If it exits immediately, use:  %~nx0 wait
pushd "%GAMEDIR%"
"%TOOL%" capture -w %EXTRA% -c "%CAPTURES%\steam-cod" "%GAMEDIR%\%TARGET%"
set "RC=%errorlevel%"
popd
echo [exit] rc=%RC%
goto :done

rem ---------------------------------------------------------------------------
:steam
rem Hand the launch to Steam itself and only wait for the renderer.
rem Do NOT use 'capture steam.exe --opt-hook-children': that hooks Steam's
rem Chromium children and sends them into a renderer restart loop.
set "RUNNING="
for /f "tokens=2 delims=," %%P in ('tasklist /FI "IMAGENAME eq %TARGET%" /FO CSV /NH 2^>nul ^| findstr /i "%TARGET%"') do (
  if not defined RUNNING set "RUNNING=%%~P"
)
if defined RUNNING (
  echo [warn] %TARGET% is already running, pid=!RUNNING!
  echo [warn] injecting that instance; d3d12.dll may already be loaded.
  set "PID=!RUNNING!"
  goto :inject
)
echo [steam] asking Steam to start Call of Duty (app 1938090) ...
start "" "steam://rungameid/1938090"
echo [wait] Watching for %TARGET% ...
goto :waitloop

rem ---------------------------------------------------------------------------
:wait
set "RUNNING="
for /f "tokens=2 delims=," %%P in ('tasklist /FI "IMAGENAME eq %TARGET%" /FO CSV /NH 2^>nul ^| findstr /i "%TARGET%"') do (
  if not defined RUNNING set "RUNNING=%%~P"
)
if defined RUNNING (
  echo [warn] %TARGET% is already running, pid=!RUNNING!
  echo [warn] d3d12.dll loads about 40s after the process starts, so injecting
  echo        now may be too late to catch the D3D12 device creation.
  choice /c YN /n /t 5 /d N /m "  inject into the running process anyway? [Y/N] "
  if errorlevel 2 goto :fresh
  set "PID=!RUNNING!"
  goto :inject
)

:fresh
echo [wait] Start Call of Duty from Steam now.
echo [wait] Watching for %TARGET% ...

:waitloop
if not defined WAITS set "WAITS=0"
set /a WAITS+=1
if !WAITS! geq 600 (
  echo [warn] gave up after about 10 minutes, %TARGET% never started.
  echo [hint] check that Steam actually launched the game, or start it by hand
  echo        and run:  %~nx0 wait
  goto :fail
)
set "PID="
for /f "tokens=2 delims=," %%P in ('tasklist /FI "IMAGENAME eq %TARGET%" /FO CSV /NH 2^>nul ^| findstr /i "%TARGET%"') do (
  if not defined PID set "PID=%%~P"
)
if defined PID goto :inject
ping -n 2 127.0.0.1 >nul
goto :waitloop

:inject
echo [found] %TARGET% pid=%PID%
echo [inject] injecting...
"%TOOL%" inject --PID=%PID% %EXTRA% -c "%CAPTURES%\steam-cod"
set "RC=%errorlevel%"
echo [inject] rc=%RC%
if not "%RC%"=="0" goto :injectfail

echo.
echo [ok] hooked into pid %PID%
echo [ok] captures go to %CAPTURES%
echo [ok] press the capture hotkey in game (F12 / PrintScreen) to grab a frame
echo [ok] this window can be closed
goto :done

:injectfail
echo.
echo [fail] injection returned %RC%
echo [hint] usual causes, in order of likelihood:
echo        1. Ricochet stripped the handle rights. OpenProcess succeeds but
echo           WriteProcessMemory / CreateRemoteThread get ACCESS_DENIED.
echo           Check with: pieces of the ETW trace, or try running this bat
echo           as Administrator.
echo        2. Injected too late. d3d12.dll loads at ~+40s after the process
echo           starts; the window is the 40s before that.
echo        3. Wrong target - cod.exe is the launcher, sp26-cod.exe renders.
goto :fail

rem ---------------------------------------------------------------------------
:usage
echo.
echo  TinecmaTool + Steam COD
echo.
echo    %~nx0                wait for sp26-cod.exe and inject
echo    %~nx0 steam          let Steam launch the game, then inject
echo    %~nx0 wait -w        inject, then block until the game exits
echo    %~nx0 launch         start the renderer under the tool
echo    %~nx0 help           this text
echo.
echo  tool     : %TOOL%
echo  target   : %GAMEDIR%\%TARGET%
echo  captures : %CAPTURES%
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
