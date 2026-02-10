# RenderDoc 文件重命名工具
# 用于伪装 RenderDoc 文件名以绕过基础的进程名检测

param(
    [Parameter(Mandatory=$false)]
    [string]$RenderDocPath = "C:\Program Files\RenderDoc",
    
    [Parameter(Mandatory=$false)]
    [string]$NewName = "TinecmaTools"
)

Write-Host "======================================" -ForegroundColor Cyan
Write-Host "RenderDoc 文件重命名工具" -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan
Write-Host ""

# 检查 RenderDoc 是否存在
if (-not (Test-Path $RenderDocPath)) {
    Write-Host "[错误] RenderDoc 路径不存在: $RenderDocPath" -ForegroundColor Red
    exit 1
}

# 创建备份目录
$backupPath = Join-Path $RenderDocPath "backup_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
New-Item -ItemType Directory -Path $backupPath -Force | Out-Null
Write-Host "[+] 创建备份目录: $backupPath" -ForegroundColor Green

# 需要重命名的文件列表
$filesToRename = @(
    @{Old="renderdoc.dll"; New="$NewName.dll"},
    @{Old="renderdoc.exe"; New="$NewName.exe"},
    @{Old="qrenderdoc.exe"; New="${NewName}UI.exe"},
    @{Old="renderdoccmd.exe"; New="${NewName}Cmd.exe"}
)

# 备份并重命名文件
foreach ($file in $filesToRename) {
    $oldPath = Join-Path $RenderDocPath $file.Old
    $newPath = Join-Path $RenderDocPath $file.New
    $backupFilePath = Join-Path $backupPath $file.Old
    
    if (Test-Path $oldPath) {
        # 备份原文件
        Copy-Item $oldPath $backupFilePath -Force
        Write-Host "[+] 备份: $($file.Old) -> backup/" -ForegroundColor Yellow
        
        # 重命名
        Rename-Item $oldPath $file.New -Force
        Write-Host "[+] 重命名: $($file.Old) -> $($file.New)" -ForegroundColor Green
    } else {
        Write-Host "[-] 未找到文件: $($file.Old)" -ForegroundColor DarkGray
    }
}

Write-Host ""
Write-Host "[完成] 所有文件已重命名" -ForegroundColor Green
Write-Host "[提示] 原文件已备份到: $backupPath" -ForegroundColor Cyan
Write-Host ""
Write-Host "现在可以使用 ${NewName}UI.exe 启动界面" -ForegroundColor Yellow
