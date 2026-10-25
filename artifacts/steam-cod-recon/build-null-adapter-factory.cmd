@echo off
rem Build the null-adapter factory fixture.
rem Needs an x64 MSVC environment. vcvarsall is the normal route; on machines where it fails
rem (reg.exe blocked by policy) set INCLUDE/LIB by hand and run the cl line directly.
setlocal
cd /d "%~dp0"

if "%VSCMD_ARG_TGT_ARCH%"=="x64" goto :build
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat" x64 >nul
if errorlevel 1 (
  echo [error] vcvarsall x64 failed - set INCLUDE/LIB manually and re-run cl
  exit /b 1
)

:build
rem Ordinal import of D3D12CreateDevice @101, same shape cod.exe uses.
lib /nologo /def:"..\iat-diagnostics-test\d3d12-ordinal.def" /machine:x64 /out:d3d12-ordinal.lib
if errorlevel 1 exit /b 1

cl /nologo /EHsc /W4 /Zi d3d12_null_adapter_factory.cpp /Fe:d3d12_null_adapter_factory.exe ^
   /link d3d12-ordinal.lib delayimp.lib dxgi.lib user32.lib /DELAYLOAD:d3d12.dll
if errorlevel 1 exit /b 1

echo [ok] d3d12_null_adapter_factory.exe
endlocal
