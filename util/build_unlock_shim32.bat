@echo off
setlocal EnableExtensions
set "SHIM=C:\Program Files\GraphicsDebugger\Win32\Development\TinecmaToolshim32.dll"

echo Unlocking TinecmaToolshim32.dll for rebuild (requires Administrator)...
echo.

net session >nul 2>&1
if errorlevel 1 (
  echo Re-launching as Administrator...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

call "%~dp0build_disable_global_hook.bat" >nul 2>&1

echo Stopping tools that often keep the shim loaded...
taskkill /F /IM qTinecmaTool.exe >nul 2>&1
taskkill /F /IM TinecmaToolcmd.exe >nul 2>&1
taskkill /F /IM devenv.exe >nul 2>&1
taskkill /F /IM MSBuild.exe >nul 2>&1
timeout /t 2 /nobreak >nul

echo.
echo Processes still using TinecmaToolshim32.dll:
tasklist /m TinecmaToolshim32.dll
echo.

if not exist "%SHIM%" (
  echo Shim not found - nothing to unlock.
  goto :done
)

del /f /q "%SHIM%" >nul 2>&1
if not exist "%SHIM%" (
  echo Deleted locked shim successfully. Rebuild Win32^|Development now.
  goto :done
)

echo Could not delete shim - renaming aside...
ren "%SHIM%" TinecmaToolshim32.dll.old >nul 2>&1
if not exist "%SHIM%" (
  echo Renamed old shim aside. Rebuild Win32^|Development now.
  goto :done
)

echo.
echo Shim is still locked by a running 32-bit program.
echo Do ONE of the following:
echo   1^) Sign out and sign back in, or reboot once, then rebuild
echo   2^) Close 32-bit apps ^(browsers, games, launchers^), then run this script again
echo   3^) Skip Win32 shim: in VS choose x64 ^| Development and build only qTinecmaTool
echo.

:done
pause
