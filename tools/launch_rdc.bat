@echo off
setlocal enableextensions

REM ============================================================================
REM  Launch qTinecmaTool (RenderDoc) with the child-injection environment that
REM  Wuthering Waves needs.
REM
REM    TCM_CHILD_ARGS         - appended to the command line of every injected
REM                             child process. The game client aborts with
REM                             "kuro: Use launcher to start game!" unless it
REM                             receives -krqlv=hd/sd/uhd.
REM    TCM_FORCE_CHILD_INJECT - keep injecting children even before the capture
REM                             options arrive (APC injection lets the target run
REM                             immediately, so the options can be late).
REM
REM  Usage:  launch_rdc.bat [hd/sd/uhd]
REM ============================================================================

set "QUALITY=%~1"
if "%QUALITY%"=="" set "QUALITY=hd"

set "ROOT=%~dp0.."
set "EXE=%ROOT%\x64\Development\qTinecmaTool.exe"

if not exist "%EXE%" (
    echo [ERROR] not found: "%EXE%"
    echo         Build renderdoc.sln with Configuration=Development, Platform=x64 first.
    pause
    exit /b 1
)

REM A running instance keeps its old environment, so start from a clean one.
taskkill /f /im qTinecmaTool.exe >nul 2>&1
if not errorlevel 1 (
    echo [INFO] closed the previous qTinecmaTool so the new environment applies.
    ping -n 2 127.0.0.1 >nul
)

set "TCM_CHILD_ARGS=-krqlv=%QUALITY%"
set "TCM_FORCE_CHILD_INJECT=1"

echo.
echo   qTinecmaTool          : %EXE%
echo   TCM_CHILD_ARGS        : %TCM_CHILD_ARGS%
echo   TCM_FORCE_CHILD_INJECT: %TCM_FORCE_CHILD_INJECT%
echo.
echo   In the tool: Launch Application
echo     Executable              : D:\Wuthering Waves\Wuthering Waves Game\Wuthering Waves.exe
echo     Capture Child Processes : checked
echo     Global hook             : unchecked
echo   Then press F12 in game to capture a frame.
echo.

start "" "%EXE%"

REM keep the console readable for a moment when started by double-click
ping -n 4 127.0.0.1 >nul

endlocal
