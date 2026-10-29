# TinecmaTools 注入诊断脚本
Write-Host "========== TinecmaTools 注入诊断 ==========" -ForegroundColor Cyan
Write-Host ""

# 1. 检查文件完整性
Write-Host "[1] 检查核心文件..." -ForegroundColor Yellow
$files = @(
    "F:\renderdoc-1.42\x64\Development\TinecmaTools.dll",
    "F:\renderdoc-1.42\x64\Development\TinecmaToolscmd.exe",
    "F:\renderdoc-1.42\x64\Development\qTinecmaTools.exe",
    "F:\renderdoc-1.42\Win32\Development\TinecmaToolscmd.exe",
    "F:\renderdoc-1.42\Win32\Development\TinecmaTools.dll"
)

foreach($file in $files) {
    if(Test-Path $file) {
        $size = (Get-Item $file).Length / 1MB
        Write-Host "  ✓ $file ($([math]::Round($size, 2)) MB)" -ForegroundColor Green
    } else {
        Write-Host "  ✗ $file 缺失!" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "[2] 检查 DLL 导出函数..." -ForegroundColor Yellow
try {
    $dllPath = "F:\renderdoc-1.42\x64\Development\TinecmaTools.dll"
    $assembly = [System.Reflection.Assembly]::LoadFile($dllPath)
    Write-Host "  DLL 可以加载" -ForegroundColor Green
} catch {
    Write-Host "  警告: 无法通过 .NET 加载 DLL (这是正常的)" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "[3] 检查依赖项..." -ForegroundColor Yellow
$dllPath = "F:\renderdoc-1.42\x64\Development\TinecmaTools.dll"
Write-Host "  正在检查 $dllPath 的依赖..." -ForegroundColor Gray

Write-Host ""
Write-Host "[4] 可能的失败原因分析:" -ForegroundColor Yellow
Write-Host "  1. 反作弊检测 CreateRemoteThread 注入方式" -ForegroundColor White
Write-Host "     → 建议: 尝试使用 APC 注入或手动映射注入" -ForegroundColor Gray
Write-Host ""
Write-Host "  2. 游戏运行在受保护模式或沙盒中" -ForegroundColor White
Write-Host "     → 建议: 以管理员身份运行 qTinecmaTools.exe" -ForegroundColor Gray
Write-Host ""
Write-Host "  3. DLL 被反作弊检测并立即卸载" -ForegroundColor White
Write-Host "     → 建议: 检查游戏进程是否立即崩溃" -ForegroundColor Gray
Write-Host ""
Write-Host "  4. 注入时机过晚，游戏已设置保护" -ForegroundColor White
Write-Host "     → 建议: 使用全局钩子 (Global Hook) 在游戏启动前注入" -ForegroundColor Gray
Write-Host ""
Write-Host "  5. 缺少运行时依赖 (VCRUNTIME, MSVCP 等)" -ForegroundColor White
Write-Host "     → 建议: 安装 Visual C++ 2019-2022 Redistributable" -ForegroundColor Gray

Write-Host ""
Write-Host "[5] 建议的测试步骤:" -ForegroundColor Yellow
Write-Host "  步骤 1: 用记事本等简单程序测试注入" -ForegroundColor White
Write-Host "    Start-Process notepad" -ForegroundColor Gray
Write-Host "    然后用 qTinecmaTools 的 File → Inject into Process 注入" -ForegroundColor Gray
Write-Host ""
Write-Host "  步骤 2: 检查游戏是否有反调试/反注入保护" -ForegroundColor White
Write-Host "    用 Process Explorer 查看游戏进程的保护状态" -ForegroundColor Gray
Write-Host ""
Write-Host "  步骤 3: 尝试以管理员身份运行" -ForegroundColor White
Write-Host "    右键 qTinecmaTools.exe → 以管理员身份运行" -ForegroundColor Gray
Write-Host ""
Write-Host "  步骤 4: 启用调试日志查看详细错误" -ForegroundColor White
Write-Host "    在 qTinecmaTools 中设置日志文件路径" -ForegroundColor Gray

Write-Host ""
Write-Host "=========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "按任意键继续..." -ForegroundColor Gray
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
