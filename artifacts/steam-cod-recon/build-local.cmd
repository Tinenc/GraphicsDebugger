@echo off
rem Machine-specific build helper: vcvarsall fails on this box because reg.exe is blocked by
rem policy, so INCLUDE/LIB are assembled by hand from the known VS 2022 Community + SDK paths.
setlocal
set "MSVC=C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Tools\MSVC\14.44.35207"
set "SDK=C:\Program Files (x86)\Windows Kits\10"
set "SDKVER=10.0.26100.0"
set "INCLUDE=%MSVC%\include;%SDK%\Include\%SDKVER%\ucrt;%SDK%\Include\%SDKVER%\shared;%SDK%\Include\%SDKVER%\um;%SDK%\Include\%SDKVER%\winrt"
set "LIB=%MSVC%\lib\x64;%SDK%\Lib\%SDKVER%\ucrt\x64;%SDK%\Lib\%SDKVER%\um\x64"

cd /d "%~dp0"

"%MSVC%\bin\Hostx64\x64\lib.exe" /nologo /def:"..\iat-diagnostics-test\d3d12-ordinal.def" /machine:x64 /out:d3d12-ordinal.lib
if errorlevel 1 (
  echo [error] lib failed
  exit /b 1
)

"%MSVC%\bin\Hostx64\x64\cl.exe" /nologo /EHsc /W4 /Zi d3d12_null_adapter_factory.cpp ^
  /Fe:d3d12_null_adapter_factory.exe ^
  /link d3d12-ordinal.lib delayimp.lib dxgi.lib user32.lib /DELAYLOAD:d3d12.dll
if errorlevel 1 (
  echo [error] cl failed
  exit /b 1
)

echo [ok] d3d12_null_adapter_factory.exe

rem Plain probe, no capture tool involved - answers what the OS D3D12 stack supports.
"%MSVC%\bin\Hostx64\x64\cl.exe" /nologo /EHsc /W4 probe_dxgi_device.cpp ^
  /Fe:probe_dxgi_device.exe /link d3d12.lib dxgi.lib
if errorlevel 1 (
  echo [error] probe build failed
  exit /b 1
)
echo [ok] probe_dxgi_device.exe
endlocal
