@echo off
rem ===========================================================================
rem Syntax-check the core files that carry Naraka-specific changes
rem ===========================================================================
rem
rem MSBuild.exe cannot run on this machine (blocked as a Known Windows LOLBin)
rem and vcvars64.bat fails because it shells out to reg.exe. That leaves no way
rem to compile the core locally - but cl.exe with /Zs (parse only) still catches
rem typos, bad signatures and include mistakes in the files we touch, without
rem needing link or the full project.
rem
rem This is not a substitute for a real build. It is the cheapest way to avoid
rem handing the user a source tree that fails 30 seconds into an MSBuild run.

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
set "CL=%MSVC%\bin\Hostx64\x64\cl.exe"

set "REPO=%~dp0..\.."
for %%i in ("%REPO%") do set "REPO=%%~fi"
cd /d "%REPO%"

set "FAILED=0"

for %%f in (
    renderdoc\driver\d3d11\d3d11_hooks.cpp
    renderdoc\driver\d3d12\d3d12_hooks.cpp
    renderdoc\driver\dxgi\dxgi_hooks.cpp
    renderdoc\os\win32\win32_libentry.cpp
    renderdoc\os\win32\win32_hook.cpp
    renderdoc\hooks\hooks.cpp
) do (
    echo --- %%f
    "%CL%" /nologo /std:c++17 /Zs /D_CRT_SECURE_NO_WARNINGS ^
      /DRENDERDOC_PLATFORM_WIN32 /DRENDERDOC_EXPORTS ^
      /I renderdoc /I renderdoc\api\replay /I renderdoc\3rdparty ^
      %%f >nul 2>&1
    if errorlevel 1 (
        echo     [FAIL]
        "%CL%" /nologo /std:c++17 /Zs /D_CRT_SECURE_NO_WARNINGS ^
          /DRENDERDOC_PLATFORM_WIN32 /DRENDERDOC_EXPORTS ^
          /I renderdoc /I renderdoc\api\replay /I renderdoc\3rdparty ^
          %%f 2>&1 | findstr /i "error"
        set "FAILED=1"
    ) else (
        echo     [ok]
    )
)

echo.
if "%FAILED%"=="1" (
    echo RESULT: syntax errors present
    endlocal
    exit /b 1
)

echo RESULT: all checked files parse cleanly
endlocal
exit /b 0
