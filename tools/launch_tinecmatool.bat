@echo off
setlocal EnableExtensions EnableDelayedExpansion
title TinecmaTool - Call of Duty / D3D12 Capture Control Center

rem Change working directory to script location immediately
cd /d "%~dp0"

rem ===========================================================================
rem  TinecmaTool (Stealth RenderDoc) - Portable One-Click GUI Launcher
rem  Location: tools\launch_tinecmatool.bat
rem ===========================================================================

rem Ensure Administrator privileges for proxy deployment and anti-cheat control
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Requesting Administrator privileges...
    powershell -NoProfile -Command "Start-Process cmd -ArgumentList '/c \"\"%~f0\" %*\"' -WorkingDirectory '%~dp0' -Verb RunAs"
    exit /b
)

rem Re-verify working directory after elevation
cd /d "%~dp0"

set "TOOLS_DIR=%~dp0"
for %%i in ("%TOOLS_DIR%..") do set "ROOT=%%~fi\"

set "TOOL_DLL=%ROOT%x64\Development\TinecmaTool.dll"
set "TOOL_GUI=%ROOT%x64\Development\qTinecmaTool.exe"
set "PROXY_DLL=%TOOLS_DIR%proxy\dxgi.dll"
set "D3D12_PROXY_DLL=%TOOLS_DIR%proxy\d3d12.dll"
set "DESKTOP=%USERPROFILE%\Desktop"

rem Auto-compile proxies if missing
if not exist "%PROXY_DLL%" (
    echo [INFO] Building DXGI and D3D12 proxies...
    pushd "%TOOLS_DIR%proxy"
    call build.bat
    popd
)

rem Launch Unified GUI Control Center
powershell -NoProfile -ExecutionPolicy Bypass -File "%TOOLS_DIR%gui_countdown.ps1" -ToolDll "%TOOL_DLL%" -ProxyDll "%PROXY_DLL%" -DesktopDir "%DESKTOP%"

exit /b 0
