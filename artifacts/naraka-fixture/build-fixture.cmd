@echo off
rem Build the Naraka D3D11 reproduction fixture.
rem
rem MSBuild.exe is blocked by local security policy on this machine, and
rem vcvars64.bat fails because it shells out to reg.exe (also blocked), so the
rem include/lib paths are set by hand and cl.exe is invoked directly.

setlocal

set "MSVC=C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Tools\MSVC\14.44.35207"
set "SDKROOT=C:\Program Files (x86)\Windows Kits\10"
set "SDKV=10.0.26100.0"

set "INCLUDE=%MSVC%\include;%SDKROOT%\Include\%SDKV%\ucrt;%SDKROOT%\Include\%SDKV%\shared;%SDKROOT%\Include\%SDKV%\um;%SDKROOT%\Include\%SDKV%\winrt"
set "LIB=%MSVC%\lib\x64;%SDKROOT%\Lib\%SDKV%\um\x64;%SDKROOT%\Lib\%SDKV%\ucrt\x64"

cd /d "%~dp0"

set "REPO=%~dp0..\.."
for %%i in ("%REPO%") do set "REPO=%%~fi"

if not exist "%MSVC%\bin\Hostx64\x64\cl.exe" (
  echo ERROR: cl.exe not found at %MSVC%
  exit /b 1
)

"%MSVC%\bin\Hostx64\x64\cl.exe" /nologo /std:c++17 /O2 /EHsc /MD /W3 ^
  /D_CRT_SECURE_NO_WARNINGS ^
  /I"%REPO%\renderdoc\api\app" ^
  naraka_fixture.cpp ^
  /Fe:naraka_fixture.exe ^
  /link /SUBSYSTEM:CONSOLE

echo EXITCODE_D3D11=%ERRORLEVEL%

"%MSVC%\bin\Hostx64\x64\cl.exe" /nologo /std:c++17 /O2 /EHsc /MD /W3 ^
  /D_CRT_SECURE_NO_WARNINGS ^
  /I"%REPO%\renderdoc\api\app" ^
  naraka_fixture12.cpp ^
  /Fe:naraka_fixture12.exe ^
  /link /SUBSYSTEM:CONSOLE

echo EXITCODE_D3D12=%ERRORLEVEL%
