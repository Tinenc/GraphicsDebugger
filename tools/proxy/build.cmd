@echo off
rem ===========================================================================
rem Build the Naraka proxy layer (dxgi.dll + d3d11.dll + d3d12.dll)
rem ===========================================================================
rem
rem Deliberately does NOT call vcvars64.bat. On this machine that script fails
rem because it shells out to reg.exe, which is blocked by local security policy,
rem and MSBuild.exe is blocked outright as a Known Windows LOLBin. Setting
rem INCLUDE/LIB by hand and calling cl.exe directly is the route that works -
rem the same trick artifacts\naraka-fixture\build-fixture.cmd uses.
rem
rem The .def files are regenerated from the real system DLLs on every build. Do
rem not hand-edit them; see gen_defs.py.

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

rem repo root = two levels up from tools\proxy
set "REPO=%~dp0..\.."
for %%i in ("%REPO%") do set "REPO=%%~fi"

rem Core location is resolved at runtime, not baked in: ini rdoc_dll first, then
rem <exe dir>\TinecmaTool.dll, then %%ProgramFiles%%\GraphicsDebugger-Naraka\x64\Development.
set "APPINC=%REPO%\renderdoc\api\app"

echo [1/4] Regenerating .def from system DLLs...
where python >nul 2>&1
if %ERRORLEVEL%==0 (
    python gen_defs.py
) else (
    echo   [warn] python not on PATH - keeping existing .def files
)

echo [2/4] Building dxgi.dll...
"%CLPATH%" /nologo /std:c++17 /O2 /MD /W3 /EHsc /D_CRT_SECURE_NO_WARNINGS ^
  /I"%APPINC%" ^
  dxgi_proxy.cpp /Fe:dxgi.dll /link /DEF:dxgi.def /DLL /MACHINE:X64 /SUBSYSTEM:WINDOWS user32.lib
if errorlevel 1 goto :failed

echo [3/4] Building d3d11.dll...
"%CLPATH%" /nologo /std:c++17 /O2 /MD /W3 /EHsc /D_CRT_SECURE_NO_WARNINGS ^
  /I"%APPINC%" ^
  d3d11_proxy.cpp /Fe:d3d11.dll /link /DEF:d3d11.def /DLL /MACHINE:X64 /SUBSYSTEM:WINDOWS user32.lib
if errorlevel 1 goto :failed

echo [4/4] Building d3d12.dll...
"%CLPATH%" /nologo /std:c++17 /O2 /MD /W3 /EHsc /D_CRT_SECURE_NO_WARNINGS ^
  /I"%APPINC%" ^
  d3d12_proxy.cpp /Fe:d3d12.dll /link /DEF:d3d12.def /DLL /MACHINE:X64 /SUBSYSTEM:WINDOWS user32.lib
if errorlevel 1 goto :failed

echo.
echo ===========================================================================
echo  BUILD OK  -  dxgi.dll / d3d11.dll / d3d12.dll
echo ===========================================================================
echo  Next: verify export tables match the system DLLs
echo    python verify_exports.py
echo.
endlocal
exit /b 0

:failed
echo.
echo [ERROR] build failed - see the compiler output above.
endlocal
exit /b 1
