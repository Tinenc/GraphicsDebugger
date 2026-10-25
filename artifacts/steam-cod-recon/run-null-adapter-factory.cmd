@echo off
rem Run the fixture under the capture tool, one mode at a time.
rem
rem   run-null-adapter-factory.cmd [null-parent|explicit-parent|null-native]
rem
rem Expected with the adapter-recovery branch in D3D12CreateDevice_hook:
rem   null-parent      captured=1   + "adapter recovered from device" + "native factory bridge installed"
rem   explicit-parent  captured=1   (baseline, worked before the branch)
rem   null-native      captured=0   (control: raw CreateDXGIFactory1 is deliberately unwrapped)
setlocal
cd /d "%~dp0"

if "%~1"=="" (set MODE=null-parent) else (set MODE=%~1)

if "%TINECMATOOL_CMD%"=="" (
  echo [error] set TINECMATOOL_CMD to the capture front-end, e.g.
  echo         set TINECMATOOL_CMD=C:\path\to\TinecmaToolcmd.exe
  exit /b 1
)

set GOP_FACTORY_MODE=%MODE%
echo [run] mode=%MODE%
"%TINECMATOOL_CMD%" capture -t "%~dp0d3d12_null_adapter_factory.exe"
echo [exit] %errorlevel%
echo [log ] %~dp0d3d12_null_adapter_factory.log
endlocal
