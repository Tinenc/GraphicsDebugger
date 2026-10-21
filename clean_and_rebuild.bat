@echo off
echo ========================================
echo TinecmaTool 完全清理和重新编译脚本
echo ========================================
echo.

REM 清理所有编译产物
echo [1/4] 清理编译产物和历史品牌残留...
if exist "x64\Development\obj" rmdir /s /q "x64\Development\obj"
if exist "x64\Release\obj" rmdir /s /q "x64\Release\obj"
if exist "Win32\Development\obj" rmdir /s /q "Win32\Development\obj"
if exist "Win32\Release\obj" rmdir /s /q "Win32\Release\obj"

if exist "x64\Development\*.json" del /q "x64\Development\*.json"
if exist "x64\Release\*.json" del /q "x64\Release\*.json"
if exist "Win32\Development\*.json" del /q "Win32\Development\*.json"
if exist "Win32\Release\*.json" del /q "Win32\Release\*.json"

REM 旧版复数名和上游默认名不会被当前解决方案的 Clean 目标删除。
REM 若它们残留在输出目录，Vulkan Loader 或人工复制步骤可能把两代核心 DLL
REM 同时载入一个进程。只清理已知构建输出目录，不触碰源码或其他路径。
for %%D in ("x64\Development" "x64\Release" "Win32\Development" "Win32\Release") do (
  if exist %%D (
    del /q "%%~D\TinecmaTools.*" "%%~D\TinecmaToolscmd.*" "%%~D\TinecmaToolsshim32.*" "%%~D\TinecmaToolsshim64.*" "%%~D\TinecmaToolsui.*" 2>nul
    del /q "%%~D\qTinecmaTools.*" 2>nul
    del /q "%%~D\renderdoc.dll" "%%~D\renderdoccmd.exe" "%%~D\renderdocshim32.dll" "%%~D\renderdocshim64.dll" "%%~D\renderdoc.json" 2>nul
  )
)

if exist ".vs" rmdir /s /q ".vs"

echo 清理完成！
echo.

echo [2/4] 等待 5 秒...
timeout /t 5 /nobreak

echo.
echo [3/4] 开始重新编译...
echo 请手动在 Visual Studio 中执行以下操作：
echo 1. 打开 renderdoc.sln
echo 2. 选择 "生成" -> "清理解决方案"
echo 3. 选择 "生成" -> "重新生成解决方案"
echo.
echo [4/4] 编译完成后，检查生成的文件
echo.

pause
