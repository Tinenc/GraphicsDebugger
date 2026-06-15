# TinecmaTools 使用示例脚本
# 本脚本展示了如何使用 TinecmaTools 的各种功能

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  TinecmaTools 使用示例" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 配置参数
$RenderDocPath = "C:\Program Files\RenderDoc"
$GamePath = "C:\Games\YourGame\game.exe"
$OutputDir = "C:\Temp\TinecmaTools"

Write-Host "[配置]" -ForegroundColor Yellow
Write-Host "  RenderDoc 路径: $RenderDocPath"
Write-Host "  游戏路径: $GamePath"
Write-Host "  输出目录: $OutputDir"
Write-Host ""

# 显示菜单
Write-Host "[请选择操作模式]" -ForegroundColor Green
Write-Host "1. 自动化模式 (推荐) - 一键完成所有步骤"
Write-Host "2. 手动模式 - 分步执行每个步骤"
Write-Host "3. 仅准备工具 - 只重命名和修改文件"
Write-Host "4. 查看帮助文档"
Write-Host "5. 退出"
Write-Host ""

$choice = Read-Host "请输入选项 (1-5)"

switch ($choice) {
    "1" {
        Write-Host "`n[自动化模式]" -ForegroundColor Cyan
        Write-Host "正在执行自动绕过流程..." -ForegroundColor Yellow
        
        # 检查 Python 是否安装
        try {
            $pythonVersion = python --version 2>&1
            Write-Host "[+] 找到 Python: $pythonVersion" -ForegroundColor Green
        } catch {
            Write-Host "[错误] 未找到 Python,请先安装 Python 3.7+" -ForegroundColor Red
            exit 1
        }
        
        # 执行自动化脚本
        Write-Host "[*] 启动自动化工具..." -ForegroundColor Yellow
        python tools\auto_bypass.py `
            --renderdoc "$RenderDocPath" `
            --game "$GamePath" `
            --output "$OutputDir" `
            --delay 10
    }
    
    "2" {
        Write-Host "`n[手动模式]" -ForegroundColor Cyan
        
        # 步骤 1: 重命名文件
        Write-Host "`n--- 步骤 1/4: 重命名文件 ---" -ForegroundColor Yellow
        $confirm = Read-Host "是否执行重命名? (Y/N)"
        if ($confirm -eq "Y" -or $confirm -eq "y") {
            .\tools\rename_tool.ps1 -RenderDocPath "$RenderDocPath" -NewName "TinecmaTools"
        }
        
        # 步骤 2: 修改字符串
        Write-Host "`n--- 步骤 2/4: 修改特征字符串 ---" -ForegroundColor Yellow
        $confirm = Read-Host "是否修改 DLL 字符串? (Y/N)"
        if ($confirm -eq "Y" -or $confirm -eq "y") {
            $dllPath = Join-Path $RenderDocPath "TinecmaTools.dll"
            if (Test-Path $dllPath) {
                python tools\string_replacer.py "$dllPath"
            } else {
                Write-Host "[警告] DLL 文件不存在,跳过此步骤" -ForegroundColor Yellow
            }
        }
        
        # 步骤 3: 启动游戏
        Write-Host "`n--- 步骤 3/4: 启动游戏 ---" -ForegroundColor Yellow
        $confirm = Read-Host "是否启动游戏? (Y/N)"
        if ($confirm -eq "Y" -or $confirm -eq "y") {
            if (Test-Path $GamePath) {
                Start-Process $GamePath
                Write-Host "[+] 游戏已启动" -ForegroundColor Green
            } else {
                Write-Host "[错误] 游戏路径不存在: $GamePath" -ForegroundColor Red
            }
        }
        
        # 步骤 4: 注入 DLL
        Write-Host "`n--- 步骤 4/4: 注入 DLL ---" -ForegroundColor Yellow
        $confirm = Read-Host "是否注入 DLL? (Y/N)"
        if ($confirm -eq "Y" -or $confirm -eq "y") {
            $processName = Read-Host "请输入游戏进程名(不含.exe)"
            $dllPath = Join-Path $RenderDocPath "TinecmaTools.dll"
            
            if (Test-Path $dllPath) {
                .\tools\inject_dll.ps1 -ProcessName $processName -DllPath $dllPath -Delay 5
            } else {
                Write-Host "[错误] DLL 文件不存在: $dllPath" -ForegroundColor Red
            }
        }
        
        Write-Host "`n[完成] 手动流程执行完毕" -ForegroundColor Green
    }
    
    "3" {
        Write-Host "`n[仅准备工具]" -ForegroundColor Cyan
        
        # 重命名文件
        Write-Host "[*] 正在重命名文件..." -ForegroundColor Yellow
        .\tools\rename_tool.ps1 -RenderDocPath "$RenderDocPath" -NewName "TinecmaTools"
        
        # 修改字符串
        Write-Host "`n[*] 正在修改特征字符串..." -ForegroundColor Yellow
        $files = @(
            "TinecmaTools.dll",
            "TinecmaTools.exe"
        )
        
        foreach ($file in $files) {
            $filePath = Join-Path $RenderDocPath $file
            if (Test-Path $filePath) {
                python tools\string_replacer.py "$filePath"
            }
        }
        
        Write-Host "`n[完成] 工具准备完毕,可以手动使用" -ForegroundColor Green
        Write-Host "[提示] 使用 TinecmaToolsUI.exe 启动界面" -ForegroundColor Cyan
    }
    
    "4" {
        Write-Host "`n[帮助文档]" -ForegroundColor Cyan
        Write-Host ""
        Write-Host "可用文档:" -ForegroundColor Yellow
        Write-Host "1. README.md             - 主要说明文档"
        Write-Host "2. QUICK_START.md        - 快速开始指南"
        Write-Host "3. bypass_anticheat_guide.md - 详细技术文档"
        Write-Host "4. CHANGELOG.md          - 更新日志"
        Write-Host "5. PROJECT_STRUCTURE.md  - 项目结构说明"
        Write-Host ""
        
        $docChoice = Read-Host "请选择要查看的文档 (1-5,或按回车跳过)"
        
        $docs = @{
            "1" = "README.md"
            "2" = "QUICK_START.md"
            "3" = "bypass_anticheat_guide.md"
            "4" = "CHANGELOG.md"
            "5" = "PROJECT_STRUCTURE.md"
        }
        
        if ($docs.ContainsKey($docChoice)) {
            $docPath = $docs[$docChoice]
            if (Test-Path $docPath) {
                notepad $docPath
            } else {
                Write-Host "[错误] 文档不存在: $docPath" -ForegroundColor Red
            }
        }
    }
    
    "5" {
        Write-Host "`n再见!" -ForegroundColor Cyan
        exit 0
    }
    
    default {
        Write-Host "`n[错误] 无效的选项" -ForegroundColor Red
        exit 1
    }
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  感谢使用 TinecmaTools!" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
