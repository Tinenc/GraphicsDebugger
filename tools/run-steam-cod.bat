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
rem repo root, one level up from tools\
for %%i in ("%HERE%..") do set "ROOT=%%~fi\"
set "TOOL=%ROOT%x64\Development\TinecmaToolcmd.exe"
set "WAREHOUSE=F:\SteamLibrary\steamapps\common\Call of Duty HQ"
set "GAMEDIR=%WAREHOUSE%\sp26"
set "TARGET=sp26-cod.exe"
set "CAPTURES=%ROOT%artifacts\steam-cod-captures"

set "MODE=%~1"
if "%MODE%"=="" set "MODE=steam"
rem everything after the mode, passed through to the tool (e.g. -w)
set "EXTRA="
if not "%~2"=="" for /f "tokens=1,*" %%a in ("%*") do set "EXTRA=%%b"

rem elevation matters: an elevated shell often fails to hand steam:// to a
rem non-elevated Steam client, so the URI is routed through explorer instead.
net session >nul 2>&1
if errorlevel 1 (set "ELEVATED=0") else (set "ELEVATED=1")

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
set "RUNLOG=%CAPTURES%\run.log"
>>"%RUNLOG%" echo ===== %DATE% %TIME% mode=%MODE% =====

if /i "%MODE%"=="launch" goto :launch
if /i "%MODE%"=="hq"     goto :hq
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
:hq
rem Let the tool own the HQ launcher and hook whatever child it spawns.
rem Unlike steam.exe this is safe to do: cod.exe is not a Chromium app, its
rem child is sp26-cod.exe. It may still want Steam's launch ticket - if the HQ
rem asks to be started from Steam, fall back to:  %~nx0 steam
if not exist "%WAREHOUSE%\cod.exe" (
  call :log "[error] cod.exe not found: %WAREHOUSE%\cod.exe"
  goto :fail
)
call :log "[hq] launching the HQ shell under the tool (children get hooked)"
pushd "%WAREHOUSE%"
"%TOOL%" capture -w --opt-hook-children %EXTRA% -c "%CAPTURES%\steam-cod" "%WAREHOUSE%\cod.exe"
set "RC=%errorlevel%"
popd
call :log "[hq] rc=%RC%"
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
if "%ELEVATED%"=="1" (
  call :log "[steam] elevated shell - routing the URI through explorer"
  explorer.exe "steam://rungameid/1938090"
) else (
  start "" "steam://rungameid/1938090"
)
call :log "[wait] Watching for %TARGET% (HQ shell cod.exe shows up first)"
goto :waitloop

rem ---------------------------------------------------------------------------
:wait
set "RUNNING="
for /f "tokens=2 delims=," %%P in ('tasklist /FI "IMAGENAME eq %TARGET%" /FO CSV /NH 2^>nul ^| findstr /i "%TARGET%"') do (
  if not defined RUNNING set "RUNNING=%%~P"
)
if defined RUNNING (
  call :log "[warn] %TARGET% is already running, pid=!RUNNING!"
  call :log "[warn] d3d12.dll loads about 40s after start, so a late inject may"
  call :log "[warn] miss device creation. If capture looks empty, restart the game."
  choice /c YN /n /t 8 /d Y /m "  inject into the running process? [Y/n] "
  if errorlevel 2 goto :fresh
  set "PID=!RUNNING!"
  goto :inject
)

:fresh
call :log "[wait] Start Call of Duty from Steam now."
call :log "[wait] Watching for %TARGET% ..."

:waitloop
if not defined WAITS set "WAITS=0"
set /a WAITS+=1
if not defined HEART set "HEART=0"
if !WAITS! geq 600 (
  call :log "[warn] gave up after about 10 minutes, %TARGET% never started."
  call :log "[hint] check that Steam actually launched the game, or start it"
  call :log "[hint] by hand and run:  %~nx0 wait"
  goto :fail
)
set "PID="
for /f "tokens=2 delims=," %%P in ('tasklist /FI "IMAGENAME eq %TARGET%" /FO CSV /NH 2^>nul ^| findstr /i "%TARGET%"') do (
  if not defined PID set "PID=%%~P"
)
if defined PID goto :inject
set /a HEART+=1
if !HEART! geq 3 (
  set "HEART=0"
  call :status
)
:tick
ping -n 2 127.0.0.1 >nul
goto :waitloop

rem heartbeat: is the HQ shell up yet? tells the user something is happening
:status
set "SHELL="
for /f "tokens=2 delims=," %%P in ('tasklist /FI "IMAGENAME eq cod.exe" /FO CSV /NH 2^>nul ^| findstr /i "cod.exe"') do (
  if not defined SHELL set "SHELL=%%~P"
)
if defined SHELL (
  echo [wait] +%WAITS%s  cod.exe pid=%SHELL% is up, waiting for %TARGET% ...
  >>"%RUNLOG%" echo [%TIME%] +%WAITS%s cod.exe pid=%SHELL%
) else (
  echo [wait] +%WAITS%s  nothing running yet - has the game been launched?
  >>"%RUNLOG%" echo [%TIME%] +%WAITS%s nothing running
)
goto :eof

:log
echo %~1
>>"%RUNLOG%" echo [%TIME%] %~1
goto :eof

:inject
call :log "[found] %TARGET% pid=%PID%"
call :log "[inject] %TOOL% inject --PID=%PID%"
"%TOOL%" inject --PID=%PID% %EXTRA% -c "%CAPTURES%\steam-cod"
set "RC=%errorlevel%"
call :log "[inject] rc=%RC%"
if not "%RC%"=="0" goto :injectfail

echo.
call :log "[ok] hooked into pid %PID%"
call :log "[ok] captures go to %CAPTURES%"
call :log "[ok] press the capture hotkey in game (F12 / PrintScreen) to grab a frame"
echo [ok] full transcript: %RUNLOG%
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
echo    %~nx0                let Steam launch the game, wait, inject   [default]
echo    %~nx0 hq             launch the HQ shell under the tool, hook children
echo    %~nx0 wait           only wait, you start the game yourself
echo    %~nx0 wait -w        inject, then block until the game exits
echo    %~nx0 launch         start the renderer exe directly under the tool
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
