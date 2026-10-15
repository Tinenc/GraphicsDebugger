@echo off
setlocal
set "REGFILE=%TEMP%\TinecmaTool_RestoreGlobalHook.reg"

if exist "%REGFILE%" (
  echo Restoring Global Hook from %REGFILE%
  net session >nul 2>&1
  if errorlevel 1 (
    echo Re-launching as Administrator...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
  )
  reg import "%REGFILE%"
  echo Global Hook restored.
) else (
  echo %REGFILE% not found.
  echo Start Global Hook from qTinecmaTool: Capture dialog -^> Global Process Hook -^> Start.
)
pause
