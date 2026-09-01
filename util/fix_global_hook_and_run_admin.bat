@echo off
setlocal EnableExtensions

echo === TinecmaTool Global Hook - one-shot admin fix ===
echo.

net session >nul 2>&1
if errorlevel 1 (
    echo [*] Elevating to Administrator via UAC...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo [1/5] Closing existing qTinecmaTool.exe if running...
taskkill /IM qTinecmaTool.exe /F >nul 2>&1

set "REPO=C:\Program Files\GraphicsDebugger"
set "SHIM64=%REPO%\x64\Development\TinecmaToolshim64.dll"
set "SHIM32=%REPO%\Win32\Development\TinecmaToolshim32.dll"
set "QUI=%REPO%\x64\Development\qTinecmaTool.exe"

if not exist "%SHIM64%" (
    echo [ERROR] Missing "%SHIM64%"
    echo         Rebuild renderdocshim first.
    pause
    exit /b 1
)
if not exist "%SHIM32%" (
    echo [WARN]  Missing "%SHIM32%" - 32-bit games will not be hooked.
)
if not exist "%QUI%" (
    echo [ERROR] Missing "%QUI%"
    pause
    exit /b 1
)

echo [2/5] Writing HKLM AppInit_DLLs registry entries...
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Windows" /v LoadAppInit_DLLs /t REG_DWORD /d 1 /f >nul
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Windows" /v RequireSignedAppInit_DLLs /t REG_DWORD /d 0 /f >nul
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Windows" /v AppInit_DLLs /t REG_SZ /d "%SHIM64%" /f >nul

if exist "%SHIM32%" (
    reg add "HKLM\SOFTWARE\Wow6432Node\Microsoft\Windows NT\CurrentVersion\Windows" /v LoadAppInit_DLLs /t REG_DWORD /d 1 /f >nul
    reg add "HKLM\SOFTWARE\Wow6432Node\Microsoft\Windows NT\CurrentVersion\Windows" /v RequireSignedAppInit_DLLs /t REG_DWORD /d 0 /f >nul
    reg add "HKLM\SOFTWARE\Wow6432Node\Microsoft\Windows NT\CurrentVersion\Windows" /v AppInit_DLLs /t REG_SZ /d "%SHIM32%" /f >nul
)

echo [3/5] Verifying registry values:
echo   --- 64-bit view ---
reg query "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Windows" /v LoadAppInit_DLLs
reg query "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Windows" /v AppInit_DLLs
echo   --- 32-bit view (Wow6432Node) ---
reg query "HKLM\SOFTWARE\Wow6432Node\Microsoft\Windows NT\CurrentVersion\Windows" /v LoadAppInit_DLLs
reg query "HKLM\SOFTWARE\Wow6432Node\Microsoft\Windows NT\CurrentVersion\Windows" /v AppInit_DLLs

echo.
echo [4/5] Launching qTinecmaTool.exe as Administrator...
start "" "%QUI%"

echo.
echo [5/5] Done.
echo.
echo Next steps in qTinecmaTool:
echo   File menu -^> Inject into Process  OR
echo   Capture dialog -^> Global Process Hook tab -^> Start
echo   The Start button should now succeed because registry is already
echo   configured and qTinecmaTool itself runs as admin.
echo.
echo Then launch MuMu Player - the shim should auto-load into MuMuNxMain.exe
echo and any child GPU process, letting you attach and capture frames.
echo.
pause
