@echo off
setlocal
echo Disabling TinecmaTool Global Hook (requires Administrator)...
echo.

net session >nul 2>&1
if errorlevel 1 (
  echo Re-launching as Administrator...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Windows" /v LoadAppInit_DLLs /t REG_DWORD /d 0 /f >nul
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Windows" /v AppInit_DLLs /t REG_SZ /d "" /f >nul
reg add "HKLM\SOFTWARE\Wow6432Node\Microsoft\Windows NT\CurrentVersion\Windows" /v LoadAppInit_DLLs /t REG_DWORD /d 0 /f >nul
reg add "HKLM\SOFTWARE\Wow6432Node\Microsoft\Windows NT\CurrentVersion\Windows" /v AppInit_DLLs /t REG_SZ /d "" /f >nul

echo Global Hook disabled in registry.
echo.
echo IMPORTANT: Already-running programs may still lock TinecmaToolshim32.dll.
echo If MSBuild still reports access denied, either:
echo   1) Sign out and sign back in, or reboot once
echo   2) Close qTinecmaTool / games / browsers, then rebuild
echo   3) Build only x64^|Development (skip Win32 shim) if you do not need 32-bit hook
echo.
pause
