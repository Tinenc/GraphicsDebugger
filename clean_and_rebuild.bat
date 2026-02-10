@echo off
echo ========================================
echo TinecmaTools 完全清理和重新编译脚本
echo ========================================
echo.

REM 清理所有编译产物
echo [1/4] 清理编译产物...
if exist "x64\Development\obj" rmdir /s /q "x64\Development\obj"
if exist "x64\Release\obj" rmdir /s /q "x64\Release\obj"
if exist "Win32\Development\obj" rmdir /s /q "Win32\Development\obj"
if exist "Win32\Release\obj" rmdir /s /q "Win32\Release\obj"

if exist "x64\Development\*.json" del /q "x64\Development\*.json"
if exist "x64\Release\*.json" del /q "x64\Release\*.json"
if exist "Win32\Development\*.json" del /q "Win32\Development\*.json"
if exist "Win32\Release\*.json" del /q "Win32\Release\*.json"

if exist ".vs" rmdir /s /q ".vs"

echo 清理完成！
echo.

echo [2/4] 等待 5 秒...
timeout /t 5 /nobreak

echo.
echo [3/4] 开始重新编译...
echo 请手动在 Visual Studio 中执行以下操作：
echo 1. 打开 TinecmaTools.sln
echo 2. 选择 "生成" -> "清理解决方案"
echo 3. 选择 "生成" -> "重新生成解决方案"
echo.
echo [4/4] 编译完成后，检查生成的文件
echo.

pause
