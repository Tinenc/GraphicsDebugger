@echo off
setlocal EnableExtensions EnableDelayedExpansion
title TinecmaTool - Arena Breakout Infinite Capture Control Center

cd /d "%~dp0"

rem ===========================================================================
rem  TinecmaTool (Bread v1.46) - Arena Breakout Infinite One-Click GUI Launcher
rem  Location: tools\launch_tinecmatool.bat
rem ===========================================================================

rem Ensure Administrator privileges for WeGame / UAGame child-process injection
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [INFO] Requesting Administrator privileges...
    powershell -NoProfile -Command "Start-Process cmd -ArgumentList '/c \"\"%~f0\" %*\"' -WorkingDirectory '%~dp0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"

set "TOOLS_DIR=%~dp0"
for %%i in ("%TOOLS_DIR%..") do set "ROOT=%%~fi"

set "TOOL_GUI=%ROOT%\x64\BreadDevelopment\qTinecmaTool.exe"
set "TOOL_X86=%ROOT%\x64\BreadDevelopment\x86\TinecmaToolcmd.exe"

if not exist "%TOOL_GUI%" (
    echo [ERROR] Missing "%TOOL_GUI%"
    echo         Build x64\BreadDevelopment first.
    pause
    exit /b 1
)
if not exist "%TOOL_X86%" (
    echo [WARN]  Missing "%TOOL_X86%" - 32-bit WeGame child-process promotion requires x86 helper.
)

powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%TOOLS_DIR%abi_capture_gui.ps1" -RepoRoot "%ROOT%"

exit /b 0
