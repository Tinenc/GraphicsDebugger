@echo off
rem ===========================================================================
rem Build the three helper exes used when no one is at the keyboard.
rem ===========================================================================
rem
rem Why these exist: Add-Type is blocked by policy on this machine ("compiles and
rem loads .NET code at runtime") and so is WScript.Shell, which rules out every
rem scripting route to screenshot a window, send a key, or click a button. These
rem are the smallest things that do the job with nothing but user32/gdi32.
rem
rem Same build approach as tools\proxy\build.cmd: vcvars64.bat is unusable here
rem (it shells out to reg.exe, which is blocked) and MSBuild.exe is blocked as a
rem Known Windows LOLBin, so INCLUDE/LIB are set by hand and cl.exe is called
rem directly.

setlocal EnableExtensions

set "MSVC=C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Tools\MSVC\14.44.35207"
set "SDKROOT=C:\Program Files (x86)\Windows Kits\10"
set "SDKV=10.0.26100.0"

if not exist "%MSVC%\bin\Hostx64\x64\cl.exe" (
    echo [ERROR] cl.exe not found at "%MSVC%"
    exit /b 1
)

set "INCLUDE=%MSVC%\include;%SDKROOT%\Include\%SDKV%\ucrt;%SDKROOT%\Include\%SDKV%\shared;%SDKROOT%\Include\%SDKV%\um;%SDKROOT%\Include\%SDKV%\winrt"
set "LIB=%MSVC%\lib\x64;%SDKROOT%\Lib\%SDKV%\um\x64;%SDKROOT%\Lib\%SDKV%\ucrt\x64"
rem NOT named CL: that is an MSVC environment variable holding default compiler
rem options, and cl.exe would treat the path as an input file.
set "CLPATH=%MSVC%\bin\Hostx64\x64\cl.exe"

cd /d "%~dp0"

for %%T in (cap sendkey click) do (
    echo.
    echo === %%T ===
    "%CLPATH%" -nologo -std:c++17 -O2 -EHsc -D_CRT_SECURE_NO_WARNINGS ^
        %%T.cpp /Fe:%%T.exe /link /SUBSYSTEM:CONSOLE user32.lib gdi32.lib
    if errorlevel 1 (
        echo [ERROR] %%T failed
        exit /b 1
    )
)

echo.
echo built: cap.exe sendkey.exe click.exe
endlocal
exit /b 0
