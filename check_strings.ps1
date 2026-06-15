# TinecmaTools 字符串检查脚本
# 用于检查编译后的文件中是否还包含 "renderdoc" 相关字符串

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "TinecmaTools 字符串检查工具" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# 定义要检查的目录
$checkDirs = @(
    "x64\Development",
    "x64\Release",
    "Win32\Development",
    "Win32\Release"
)

# 定义要检查的文件类型
$fileTypes = @("*.exe", "*.dll", "*.json")

# 定义敏感字符串
$sensitiveStrings = @(
    "renderdoc",
    "RenderDoc",
    "RENDERDOC",
    "VK_LAYER_RENDERDOC",
    "ENABLE_VULKAN_RENDERDOC",
    "DISABLE_VULKAN_RENDERDOC"
)

$foundIssues = @()

foreach ($dir in $checkDirs) {
    if (Test-Path $dir) {
        Write-Host "检查目录: $dir" -ForegroundColor Yellow
        
        foreach ($fileType in $fileTypes) {
            $files = Get-ChildItem -Path $dir -Filter $fileType -Recurse -ErrorAction SilentlyContinue
            
            foreach ($file in $files) {
                Write-Host "  检查文件: $($file.Name)" -ForegroundColor Gray
                
                try {
                    $content = Get-Content -Path $file.FullName -Raw -Encoding Byte -ErrorAction Stop
                    $contentStr = [System.Text.Encoding]::ASCII.GetString($content)
                    
                    foreach ($str in $sensitiveStrings) {
                        if ($contentStr -match $str) {
                            $issue = @{
                                File = $file.FullName
                                String = $str
                            }
                            $foundIssues += $issue
                            Write-Host "    [警告] 发现: $str" -ForegroundColor Red
                        }
                    }
                } catch {
                    Write-Host "    [跳过] 无法读取文件" -ForegroundColor DarkGray
                }
            }
        }
        Write-Host ""
    }
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "检查完成" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

if ($foundIssues.Count -gt 0) {
    Write-Host ""
    Write-Host "发现 $($foundIssues.Count) 个潜在问题:" -ForegroundColor Red
    Write-Host ""
    
    $foundIssues | ForEach-Object {
        Write-Host "文件: $($_.File)" -ForegroundColor Yellow
        Write-Host "字符串: $($_.String)" -ForegroundColor Red
        Write-Host ""
    }
    
    Write-Host "建议: 需要进一步修改源代码以消除这些字符串" -ForegroundColor Cyan
} else {
    Write-Host ""
    Write-Host "✓ 未发现敏感字符串！" -ForegroundColor Green
    Write-Host ""
}

Write-Host "按任意键退出..."
$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
