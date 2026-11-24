@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0replaylab\packaging\Build-RenderDocBread.ps1" %*
set "bread_package_exit=%ERRORLEVEL%"
echo.
if not "%bread_package_exit%"=="0" echo RenderDocBread packaging failed. See the error above.
pause
exit /b %bread_package_exit%
