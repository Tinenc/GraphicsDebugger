@echo off
setlocal EnableExtensions EnableDelayedExpansion
title TinecmaTool - inject Steam (hook children)

rem ===========================================================================
rem  Injects TinecmaTool.dll into steam.exe with hookIntoChildProcesses.
rem
rem  steam.exe is the only link in this chain without anti-cheat, and it is
rem  fully enumerable, so the inject completes properly (rc=0 + hooks registered).
rem  Every process Steam creates afterwards is injected on creation - which is
rem  the only moment that lands before Ricochet grabs the process handle.
rem
rem    inject-steam.bat           inject now (Steam must already be running)
rem    inject-steam.bat check     only show what would be targeted
rem ===========================================================================

for %%i in ("%~dp0..") do set "ROOT=%%~fi\"
set "TOOL=%ROOT%x64\Development\TinecmaToolcmd.exe"
set "CAPTURES=%ROOT%artifacts\steam-cod-captures"

if not exist "%TOOL%" (
  echo [error] TinecmaToolcmd.exe not found:
  echo         %TOOL%
  echo         build the x64 target first.
  goto :fail
)

set "PID="
set "COUNT=0"
for /f "tokens=2 delims=," %%P in ('tasklist /FI "IMAGENAME eq steam.exe" /FO CSV /NH 2^>nul ^| findstr /i "steam.exe"') do (
  if not defined PID set "PID=%%~P"
  set /a COUNT+=1
)

if not defined PID (
  echo [error] steam.exe is not running.
  echo         Start Steam, wait until the library window shows, then run this again.
  goto :fail
)

echo [ok] steam.exe pid=!PID!  (instances=!COUNT!)
echo      tool     : %TOOL%
echo      captures : %CAPTURES%

if /i "%~1"=="check" (
  echo.
  echo [check] not injecting, as requested.
  goto :done
)

if not exist "%CAPTURES%" mkdir "%CAPTURES%" >nul 2>&1

echo.
echo [inject] injecting with --opt-hook-children ...
"%TOOL%" inject --PID=!PID! --opt-hook-children -c "%CAPTURES%\steam-cod"
set "RC=%errorlevel%"
echo [inject] rc=%RC%

if not "%RC%"=="0" goto :fail2

echo.
echo [ready] Start the game from Steam now.
echo         cod.exe and sp26-cod.exe are injected the moment they are created.
echo [note ] verify with:  %TEMP%\TinecmaTool\  (look for "Loading into ...cod.exe")
goto :done

:fail2
echo.
echo [hint] rc^!=0 means the inject did not complete. Check the newest log in:
echo        %TEMP%\TinecmaTool\
echo [hint] a log line "Loading into ...\steam.exe" plus "Registering D3D12 hooks"
echo        is what success looks like.
pause
endlocal
exit /b 1

:done
echo.
pause
endlocal
exit /b 0

:fail
pause
endlocal
exit /b 1
