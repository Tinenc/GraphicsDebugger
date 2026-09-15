@echo off
chcp 65001 >nul
echo ============================================================
echo spirv-cross.exe 设置助手
echo ============================================================
echo.
echo 当前目录: %~dp0
echo.
echo 检查 spirv-cross.exe 是否存在...
echo.

REM 检查当前目录
if exist "%~dp0spirv-cross.exe" (
    echo [✓] 找到 spirv-cross.exe 在当前目录
    echo.
    spirv-cross.exe --version 2>nul
    if %errorlevel% == 0 (
        echo [✓] spirv-cross.exe 可以正常运行
        echo.
        echo 已经配置完成！可以使用Shader导出功能。
        pause
        exit /b 0
    ) else (
        echo [✗] spirv-cross.exe 无法运行，可能损坏
        echo.
    )
)

REM 检查PATH
spirv-cross --version >nul 2>&1
if %errorlevel% == 0 (
    echo [✓] 在系统PATH中找到 spirv-cross
    echo.
    echo 已经配置完成！可以使用Shader导出功能。
    pause
    exit /b 0
)

echo [✗] 没有找到 spirv-cross.exe
echo.
echo ============================================================
echo 如何获取 spirv-cross.exe？
echo ============================================================
echo.
echo 方案A: 从Vulkan SDK获取（推荐）
echo   1. 下载Vulkan SDK: https://vulkan.lunarg.com/sdk/home
echo   2. 安装后，在以下位置查找 spirv-cross.exe：
echo      C:\VulkanSDK\[version]\Bin\spirv-cross.exe
echo   3. 复制到此目录: %~dp0
echo.
echo 方案B: 从GitHub下载预编译版本
echo   1. 访问: https://github.com/KhronosGroup/SPIRV-Cross/releases
echo   2. 下载 Windows 预编译版本
echo   3. 解压并复制 spirv-cross.exe 到此目录: %~dp0
echo.
echo 方案C: 从在线源下载（测试）
echo   我们可以尝试从Vulkan SDK在线安装器获取
echo.
echo ============================================================
set /p choice="是否尝试自动查找Vulkan SDK中的spirv-cross？(Y/N): "
if /i "%choice%" neq "Y" goto manual

echo.
echo 搜索Vulkan SDK安装...
echo.

REM 搜索常见的Vulkan SDK路径
set FOUND=0
for /d %%d in ("C:\VulkanSDK\*") do (
    if exist "%%d\Bin\spirv-cross.exe" (
        echo [✓] 找到: %%d\Bin\spirv-cross.exe
        echo.
        set /p copychoice="是否复制到当前目录？(Y/N): "
        if /i "!copychoice!" == "Y" (
            copy "%%d\Bin\spirv-cross.exe" "%~dp0spirv-cross.exe"
            if !errorlevel! == 0 (
                echo [✓] 复制成功！
                echo.
                echo 配置完成！可以使用Shader导出功能。
                set FOUND=1
                goto end
            ) else (
                echo [✗] 复制失败
            )
        )
    )
)

if %FOUND% == 0 (
    echo [✗] 未在标准位置找到Vulkan SDK
    goto manual
)
goto end

:manual
echo.
echo ============================================================
echo 手动配置步骤：
echo ============================================================
echo 1. 下载 spirv-cross.exe（见上方方案A或B）
echo 2. 将 spirv-cross.exe 复制到：
echo    %~dp0
echo 3. 重新运行此脚本验证
echo 4. 或者运行 install.bat 安装插件
echo ============================================================

:end
echo.
pause

