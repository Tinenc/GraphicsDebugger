param(
    [string]$GameDir = "",
    [string]$SteamDir = "",
    [string]$ToolDll = "",
    [string]$ProxyDll = "",
    [string]$DesktopDir = "",
    [string]$OutputFile = ""
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

if ([string]::IsNullOrWhiteSpace($DesktopDir)) {
    $DesktopDir = [System.IO.Path]::Combine($env:USERPROFILE, "Desktop")
}

$toolsDir = if (![string]::IsNullOrWhiteSpace($PSScriptRoot) -and (Test-Path (Join-Path $PSScriptRoot "proxy"))) { $PSScriptRoot } else { "C:\Program Files\GraphicsDebugger-Steam-Cod\tools" }
$repoRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::Combine($toolsDir, ".."))
if ([string]::IsNullOrWhiteSpace($ToolDll)) {
    $ToolDll = [System.IO.Path]::Combine($repoRoot, "x64", "Development", "TinecmaTool.dll")
}
$ToolGui = [System.IO.Path]::Combine($repoRoot, "x64", "Development", "qTinecmaTool.exe")
$ToolCmd = [System.IO.Path]::Combine($repoRoot, "x64", "Development", "TinecmaToolcmd.exe")
if (![System.IO.File]::Exists($ToolCmd) -and [System.IO.File]::Exists($ToolGui)) {
    $ToolCmd = $ToolGui
}
$ProxyDxgi = Join-Path $toolsDir "proxy\dxgi.dll"
$ProxyD3D12 = Join-Path $toolsDir "proxy\d3d12.dll"

# ---------------------------------------------------------------------------
# Auto-detect game paths
# ---------------------------------------------------------------------------
$detectedCodHQ = ""
$codCandidates = @(
    $GameDir,
    "F:\SteamLibrary\steamapps\common\Call of Duty HQ",
    "D:\SteamLibrary\steamapps\common\Call of Duty HQ",
    "C:\Program Files (x86)\Steam\steamapps\common\Call of Duty HQ",
    "C:\Program Files\Steam\steamapps\common\Call of Duty HQ"
)
foreach ($c in $codCandidates) {
    if (![string]::IsNullOrWhiteSpace($c) -and (Test-Path (Join-Path $c "cod.exe"))) {
        $detectedCodHQ = $c
        break
    }
}
if ([string]::IsNullOrWhiteSpace($detectedCodHQ)) {
    try {
        $reg = Get-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Steam App 1938090" -ErrorAction SilentlyContinue
        if ($reg -and $reg.InstallLocation -and (Test-Path (Join-Path $reg.InstallLocation "cod.exe"))) {
            $detectedCodHQ = $reg.InstallLocation
        }
    } catch {}
}

$detectedMW2019 = ""
$mwCandidates = @(
    $GameDir,
    "D:\Call of Duty Modern Warfare",
    "C:\Program Files (x86)\Call of Duty Modern Warfare",
    "E:\Call of Duty Modern Warfare",
    "F:\Call of Duty Modern Warfare"
)
foreach ($m in $mwCandidates) {
    if (![string]::IsNullOrWhiteSpace($m) -and (Test-Path (Join-Path $m "ModernWarfare.exe"))) {
        $detectedMW2019 = $m
        break
    }
}

if ([string]::IsNullOrWhiteSpace($SteamDir)) {
    try {
        $sReg = Get-ItemProperty -Path "HKCU:\Software\Valve\Steam" -ErrorAction SilentlyContinue
        if ($sReg -and $sReg.SteamPath) {
            $SteamDir = $sReg.SteamPath.Replace("/", "\")
        }
    } catch {}
}

$BattleNetLauncher = ""
$bnetCandidates = @(
    "C:\Program Files (x86)\Battle.net\Battle.net Launcher.exe",
    "C:\Program Files\Battle.net\Battle.net Launcher.exe",
    "C:\Program Files (x86)\Battle.net\Battle.net.exe",
    "C:\Program Files\Battle.net\Battle.net.exe"
)
foreach ($b in $bnetCandidates) {
    if (Test-Path $b) {
        $BattleNetLauncher = $b
        break
    }
}
if ([string]::IsNullOrWhiteSpace($BattleNetLauncher)) {
    try {
        $bCmd = (Get-ItemProperty "Registry::HKEY_CLASSES_ROOT\battlenet\shell\open\command" -ErrorAction SilentlyContinue).'(default)'
        if ($bCmd -match '"([^"]+)"') {
            $p = $matches[1]
            if (Test-Path $p) { $BattleNetLauncher = $p }
        }
    } catch {}
}

# ---------------------------------------------------------------------------
# Form Setup
# ---------------------------------------------------------------------------
$form = New-Object System.Windows.Forms.Form
$form.Text = "TinecmaTool (Stealth RenderDoc) - 图形抓帧控制中心"
$form.Size = New-Object System.Drawing.Size(740, 710)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.MinimizeBox = $true
$form.TopMost = $false
$form.BackColor = [System.Drawing.Color]::FromArgb(32, 33, 36)
$form.ForeColor = [System.Drawing.Color]::White
$form.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5)

# 1. Title Banner
$lblTitle = New-Object System.Windows.Forms.Label
$lblTitle.Location = New-Object System.Drawing.Point(24, 14)
$lblTitle.Size = New-Object System.Drawing.Size(680, 28)
$lblTitle.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 13, [System.Drawing.FontStyle]::Bold)
$lblTitle.ForeColor = [System.Drawing.Color]::FromArgb(0, 215, 160)
$lblTitle.Text = "TinecmaTool (Stealth RenderDoc) - 图形抓帧控制中心"
$form.Controls.Add($lblTitle)

$lblSub = New-Object System.Windows.Forms.Label
$lblSub.Location = New-Object System.Drawing.Point(26, 42)
$lblSub.Size = New-Object System.Drawing.Size(680, 20)
$lblSub.ForeColor = [System.Drawing.Color]::FromArgb(180, 180, 180)
$lblSub.Text = "支持《使命召唤：现代战争 (2019)》与《Call of Duty HQ》(BO6/MW3/MW2) 静默双代理抓帧"
$form.Controls.Add($lblSub)

# 2. GroupBox: 目标游戏选择与路径 (Target Game & Directory)
$grpGame = New-Object System.Windows.Forms.GroupBox
$grpGame.Location = New-Object System.Drawing.Point(24, 68)
$grpGame.Size = New-Object System.Drawing.Size(675, 144)
$grpGame.Text = " 选项 1：目标游戏选择与路径 (Target Game & Directory) "
$grpGame.ForeColor = [System.Drawing.Color]::FromArgb(210, 210, 210)
$grpGame.BackColor = [System.Drawing.Color]::FromArgb(40, 42, 48)

$rbGameMW = New-Object System.Windows.Forms.RadioButton
$rbGameMW.Location = New-Object System.Drawing.Point(16, 22)
$rbGameMW.Size = New-Object System.Drawing.Size(265, 24)
$rbGameMW.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5, [System.Drawing.FontStyle]::Bold)
$rbGameMW.ForeColor = [System.Drawing.Color]::FromArgb(100, 215, 255)
$rbGameMW.Text = "现代战争 2019 (战网: MW2019)"
$grpGame.Controls.Add($rbGameMW)

$rbGameCod = New-Object System.Windows.Forms.RadioButton
$rbGameCod.Location = New-Object System.Drawing.Point(286, 22)
$rbGameCod.Size = New-Object System.Drawing.Size(265, 24)
$rbGameCod.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5, [System.Drawing.FontStyle]::Bold)
$rbGameCod.ForeColor = [System.Drawing.Color]::FromArgb(255, 205, 90)
$rbGameCod.Text = "Call of Duty HQ (Steam: cod.exe)"
$grpGame.Controls.Add($rbGameCod)

$rbGameCustom = New-Object System.Windows.Forms.RadioButton
$rbGameCustom.Location = New-Object System.Drawing.Point(560, 22)
$rbGameCustom.Size = New-Object System.Drawing.Size(100, 24)
$rbGameCustom.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5)
$rbGameCustom.ForeColor = [System.Drawing.Color]::FromArgb(190, 190, 190)
$rbGameCustom.Text = "自定义..."
$grpGame.Controls.Add($rbGameCustom)

$lblGame = New-Object System.Windows.Forms.Label
$lblGame.Location = New-Object System.Drawing.Point(16, 54)
$lblGame.Size = New-Object System.Drawing.Size(85, 22)
$lblGame.Text = "游戏根目录:"
$grpGame.Controls.Add($lblGame)

$txtGame = New-Object System.Windows.Forms.TextBox
$txtGame.Location = New-Object System.Drawing.Point(105, 52)
$txtGame.Size = New-Object System.Drawing.Size(465, 24)
$txtGame.BackColor = [System.Drawing.Color]::FromArgb(24, 25, 28)
$txtGame.ForeColor = [System.Drawing.Color]::White
$grpGame.Controls.Add($txtGame)

$btnBrowse = New-Object System.Windows.Forms.Button
$btnBrowse.Location = New-Object System.Drawing.Point(580, 50)
$btnBrowse.Size = New-Object System.Drawing.Size(80, 28)
$btnBrowse.Text = "浏览..."
$btnBrowse.BackColor = [System.Drawing.Color]::FromArgb(55, 58, 66)
$btnBrowse.ForeColor = [System.Drawing.Color]::White
$btnBrowse.FlatStyle = "Flat"
$btnBrowse.FlatAppearance.BorderSize = 0
$btnBrowse.Cursor = [System.Windows.Forms.Cursors]::Hand
$btnBrowse.Add_Click({
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    $dlg.Description = "选择游戏安装根目录"
    if (![string]::IsNullOrWhiteSpace($txtGame.Text) -and (Test-Path $txtGame.Text)) { $dlg.SelectedPath = $txtGame.Text }
    if ($dlg.ShowDialog() -eq "OK") {
        $txtGame.Text = $dlg.SelectedPath
    }
})
$grpGame.Controls.Add($btnBrowse)

$lblGameTip = New-Object System.Windows.Forms.Label
$lblGameTip.Location = New-Object System.Drawing.Point(16, 84)
$lblGameTip.Size = New-Object System.Drawing.Size(645, 52)
$lblGameTip.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 8.8)
$lblGameTip.ForeColor = [System.Drawing.Color]::FromArgb(255, 205, 90)
$grpGame.Controls.Add($lblGameTip)

$form.Controls.Add($grpGame)

# 3. GroupBox: 抓帧后端配置 (Capture Backend)
$grpBackend = New-Object System.Windows.Forms.GroupBox
$grpBackend.Location = New-Object System.Drawing.Point(24, 218)
$grpBackend.Size = New-Object System.Drawing.Size(675, 108)
$grpBackend.Text = " 选项 2：抓帧后端选择 (Capture Backend) "
$grpBackend.ForeColor = [System.Drawing.Color]::FromArgb(210, 210, 210)
$grpBackend.BackColor = [System.Drawing.Color]::FromArgb(40, 42, 48)

$rbRdoc = New-Object System.Windows.Forms.RadioButton
$rbRdoc.Location = New-Object System.Drawing.Point(16, 22)
$rbRdoc.Size = New-Object System.Drawing.Size(320, 24)
$rbRdoc.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5, [System.Drawing.FontStyle]::Bold)
$rbRdoc.ForeColor = [System.Drawing.Color]::FromArgb(0, 230, 170)
$rbRdoc.Text = "TinecmaTool (RenderDoc v1.44) - 推荐"
$rbRdoc.Checked = $true
$grpBackend.Controls.Add($rbRdoc)

$lblRdocTip = New-Object System.Windows.Forms.Label
$lblRdocTip.Location = New-Object System.Drawing.Point(36, 46)
$lblRdocTip.Size = New-Object System.Drawing.Size(625, 20)
$lblRdocTip.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 8.8)
$lblRdocTip.ForeColor = [System.Drawing.Color]::FromArgb(190, 190, 190)
$lblRdocTip.Text = "提示：深度逆向调试首选。基于 1.33s 同步直写与 SEH 容错，完整抓取所有 DrawCall、PSO 与着色器字节码。"
$grpBackend.Controls.Add($lblRdocTip)

$rbPix = New-Object System.Windows.Forms.RadioButton
$rbPix.Location = New-Object System.Drawing.Point(16, 70)
$rbPix.Size = New-Object System.Drawing.Size(320, 24)
$rbPix.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5, [System.Drawing.FontStyle]::Bold)
$rbPix.ForeColor = [System.Drawing.Color]::FromArgb(120, 180, 255)
$rbPix.Text = "Microsoft PIX (WinPixGpuCapturer)"
$grpBackend.Controls.Add($rbPix)

$lblPixTip = New-Object System.Windows.Forms.Label
$lblPixTip.Location = New-Object System.Drawing.Point(36, 92)
$lblPixTip.Size = New-Object System.Drawing.Size(625, 14)
$lblPixTip.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 8.8)
$lblPixTip.ForeColor = [System.Drawing.Color]::FromArgb(190, 190, 190)
$lblPixTip.Text = "提示：微软官方性能剖析首选。支持 GPU 时间线分析，生成 .wpix 帧文件。"
$lblPixTip.Visible = $false
$grpBackend.Controls.Add($lblPixTip)

$form.Controls.Add($grpBackend)

# 4. GroupBox: 抓帧与调试工具箱 (Capture & Inspection Tools) - 合并进面板的核心工具区
$grpTools = New-Object System.Windows.Forms.GroupBox
$grpTools.Location = New-Object System.Drawing.Point(24, 332)
$grpTools.Size = New-Object System.Drawing.Size(675, 150)
$grpTools.Text = " 抓帧与调试工具箱 (Capture & Inspection Tools) "
$grpTools.ForeColor = [System.Drawing.Color]::FromArgb(210, 210, 210)
$grpTools.BackColor = [System.Drawing.Color]::FromArgb(40, 42, 48)

# Row 1 of Tool Buttons
$btnTrigger = New-Object System.Windows.Forms.Button
$btnTrigger.Location = New-Object System.Drawing.Point(16, 26)
$btnTrigger.Size = New-Object System.Drawing.Size(205, 36)
$btnTrigger.Text = "[F12] 立即触发抓帧"
$btnTrigger.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5, [System.Drawing.FontStyle]::Bold)
$btnTrigger.BackColor = [System.Drawing.Color]::FromArgb(0, 130, 85)
$btnTrigger.ForeColor = [System.Drawing.Color]::White
$btnTrigger.FlatStyle = "Flat"
$btnTrigger.FlatAppearance.BorderSize = 0
$btnTrigger.Cursor = [System.Windows.Forms.Cursors]::Hand
$grpTools.Controls.Add($btnTrigger)

$btnThumb = New-Object System.Windows.Forms.Button
$btnThumb.Location = New-Object System.Drawing.Point(235, 26)
$btnThumb.Size = New-Object System.Drawing.Size(205, 36)
$btnThumb.Text = "[预览] 最新截帧缩略图"
$btnThumb.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5)
$btnThumb.BackColor = [System.Drawing.Color]::FromArgb(45, 95, 145)
$btnThumb.ForeColor = [System.Drawing.Color]::White
$btnThumb.FlatStyle = "Flat"
$btnThumb.FlatAppearance.BorderSize = 0
$btnThumb.Cursor = [System.Windows.Forms.Cursors]::Hand
$grpTools.Controls.Add($btnThumb)

$btnOpenRdoc = New-Object System.Windows.Forms.Button
$btnOpenRdoc.Location = New-Object System.Drawing.Point(454, 26)
$btnOpenRdoc.Size = New-Object System.Drawing.Size(205, 36)
$btnOpenRdoc.Text = "[查看] 在 qTinecmaTool 中打开"
$btnOpenRdoc.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5)
$btnOpenRdoc.BackColor = [System.Drawing.Color]::FromArgb(105, 65, 145)
$btnOpenRdoc.ForeColor = [System.Drawing.Color]::White
$btnOpenRdoc.FlatStyle = "Flat"
$btnOpenRdoc.FlatAppearance.BorderSize = 0
$btnOpenRdoc.Cursor = [System.Windows.Forms.Cursors]::Hand
$grpTools.Controls.Add($btnOpenRdoc)

# Row 2 of Tool Buttons
$btnDeploy = New-Object System.Windows.Forms.Button
$btnDeploy.Location = New-Object System.Drawing.Point(16, 70)
$btnDeploy.Size = New-Object System.Drawing.Size(205, 34)
$btnDeploy.Text = "[同步] 重新部署双代理 DLL"
$btnDeploy.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
$btnDeploy.BackColor = [System.Drawing.Color]::FromArgb(60, 64, 75)
$btnDeploy.ForeColor = [System.Drawing.Color]::White
$btnDeploy.FlatStyle = "Flat"
$btnDeploy.FlatAppearance.BorderSize = 0
$btnDeploy.Cursor = [System.Windows.Forms.Cursors]::Hand
$grpTools.Controls.Add($btnDeploy)

$btnViewLog = New-Object System.Windows.Forms.Button
$btnViewLog.Location = New-Object System.Drawing.Point(235, 70)
$btnViewLog.Size = New-Object System.Drawing.Size(205, 34)
$btnViewLog.Text = "[日志] 查看代理运行日志"
$btnViewLog.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
$btnViewLog.BackColor = [System.Drawing.Color]::FromArgb(60, 64, 75)
$btnViewLog.ForeColor = [System.Drawing.Color]::White
$btnViewLog.FlatStyle = "Flat"
$btnViewLog.FlatAppearance.BorderSize = 0
$btnViewLog.Cursor = [System.Windows.Forms.Cursors]::Hand
$grpTools.Controls.Add($btnViewLog)

$btnClean = New-Object System.Windows.Forms.Button
$btnClean.Location = New-Object System.Drawing.Point(454, 70)
$btnClean.Size = New-Object System.Drawing.Size(205, 34)
$btnClean.Text = "[清理] 一键清理反作弊/环境"
$btnClean.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
$btnClean.BackColor = [System.Drawing.Color]::FromArgb(60, 64, 75)
$btnClean.ForeColor = [System.Drawing.Color]::White
$btnClean.FlatStyle = "Flat"
$btnClean.FlatAppearance.BorderSize = 0
$btnClean.Cursor = [System.Windows.Forms.Cursors]::Hand
$grpTools.Controls.Add($btnClean)

# Live status label inside Tools Box
$lblToolStatus = New-Object System.Windows.Forms.Label
$lblToolStatus.Location = New-Object System.Drawing.Point(16, 114)
$lblToolStatus.Size = New-Object System.Drawing.Size(645, 26)
$lblToolStatus.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 8.8, [System.Drawing.FontStyle]::Bold)
$lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(100, 220, 160)
$lblToolStatus.Text = "[就绪] 面板已就绪。点击下方【部署代理并启动游戏】拉起，游戏内可随时按 F12 或点击上方按钮抓帧。"
$grpTools.Controls.Add($lblToolStatus)

$form.Controls.Add($grpTools)

# 5. GroupBox: 说明与防封保障 (Instructions & Safety)
$grpTips = New-Object System.Windows.Forms.GroupBox
$grpTips.Location = New-Object System.Drawing.Point(24, 488)
$grpTips.Size = New-Object System.Drawing.Size(675, 78)
$grpTips.Text = " 使用说明与防封提示 (抓帧说明，无需勾选) "
$grpTips.ForeColor = [System.Drawing.Color]::FromArgb(210, 210, 210)
$grpTips.BackColor = [System.Drawing.Color]::FromArgb(40, 42, 48)

$lblTip1 = New-Object System.Windows.Forms.Label
$lblTip1.Location = New-Object System.Drawing.Point(16, 22)
$lblTip1.Size = New-Object System.Drawing.Size(645, 20)
$lblTip1.ForeColor = [System.Drawing.Color]::FromArgb(230, 230, 230)
$lblTip1.Text = "[抓帧方式] 进入游戏后左上角呈现 HUD 浮层，随时按下 [F12] 键或点击上方【立即触发抓帧】。"
$grpTips.Controls.Add($lblTip1)

$lblTip2 = New-Object System.Windows.Forms.Label
$lblTip2.Location = New-Object System.Drawing.Point(16, 46)
$lblTip2.Size = New-Object System.Drawing.Size(645, 24)
$lblTip2.ForeColor = [System.Drawing.Color]::FromArgb(100, 215, 255)
$lblTip2.Text = "[安全免杀保障] PEB 双向链表已物理隐匿，已屏蔽端口并隔离看门狗 0x00001337，反作弊零报警。"
$grpTips.Controls.Add($lblTip2)

$form.Controls.Add($grpTips)

# 6. Action Banner & Buttons
$lblEnterNotice = New-Object System.Windows.Forms.Label
$lblEnterNotice.Location = New-Object System.Drawing.Point(24, 574)
$lblEnterNotice.Size = New-Object System.Drawing.Size(380, 22)
$lblEnterNotice.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5, [System.Drawing.FontStyle]::Bold)
$lblEnterNotice.ForeColor = [System.Drawing.Color]::FromArgb(255, 215, 80)
$lblEnterNotice.Text = "确认配置后，按 Enter 键一键部署并启动"
$form.Controls.Add($lblEnterNotice)

$chkKeepOpen = New-Object System.Windows.Forms.CheckBox
$chkKeepOpen.Location = New-Object System.Drawing.Point(420, 572)
$chkKeepOpen.Size = New-Object System.Drawing.Size(275, 24)
$chkKeepOpen.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
$chkKeepOpen.ForeColor = [System.Drawing.Color]::FromArgb(200, 220, 255)
$chkKeepOpen.Text = "启动后保持控制面板开启 (推荐)"
$chkKeepOpen.Checked = $true
$form.Controls.Add($chkKeepOpen)

$btnLaunch = New-Object System.Windows.Forms.Button
$btnLaunch.Location = New-Object System.Drawing.Point(24, 604)
$btnLaunch.Size = New-Object System.Drawing.Size(380, 46)
$btnLaunch.Text = "▶ 部署代理并启动游戏 (Enter)"
$btnLaunch.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 11, [System.Drawing.FontStyle]::Bold)
$btnLaunch.BackColor = [System.Drawing.Color]::FromArgb(0, 150, 100)
$btnLaunch.ForeColor = [System.Drawing.Color]::White
$btnLaunch.FlatStyle = "Flat"
$btnLaunch.FlatAppearance.BorderSize = 0
$btnLaunch.Cursor = [System.Windows.Forms.Cursors]::Hand

$btnExit = New-Object System.Windows.Forms.Button
$btnExit.Location = New-Object System.Drawing.Point(420, 604)
$btnExit.Size = New-Object System.Drawing.Size(279, 46)
$btnExit.Text = "✕ 退出面板 (Esc)"
$btnExit.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 10.5)
$btnExit.BackColor = [System.Drawing.Color]::FromArgb(70, 72, 80)
$btnExit.ForeColor = [System.Drawing.Color]::White
$btnExit.FlatStyle = "Flat"
$btnExit.FlatAppearance.BorderSize = 0
$btnExit.Cursor = [System.Windows.Forms.Cursors]::Hand

$form.Controls.Add($btnLaunch)
$form.Controls.Add($btnExit)

# ---------------------------------------------------------------------------
# Functions for Panel Tools
# ---------------------------------------------------------------------------

function Deploy-ProxiesToGame {
    param([string]$targetFolder)
    if ([string]::IsNullOrWhiteSpace($targetFolder) -or !(Test-Path $targetFolder)) {
        throw "目标游戏目录不存在: $targetFolder"
    }

    # Backup Win7 d3d12.dll if MW2019
    $d12Path = Join-Path $targetFolder "d3d12.dll"
    if (Test-Path $d12Path) {
        $info = (Get-Item $d12Path).VersionInfo
        if ($info.FileDescription -match "Windows 7") {
            Copy-Item $d12Path (Join-Path $targetFolder "d3d12_win7.dll.bak") -Force
        }
    }

    # Copy dxgi.dll and d3d12.dll
    Copy-Item $ProxyDxgi (Join-Path $targetFolder "dxgi.dll") -Force
    Copy-Item $ProxyD3D12 (Join-Path $targetFolder "d3d12.dll") -Force

    # If COD HQ with sp26
    $sp26Path = Join-Path $targetFolder "sp26"
    if (Test-Path $sp26Path) {
        Copy-Item $ProxyDxgi (Join-Path $sp26Path "dxgi.dll") -Force
        Copy-Item $ProxyD3D12 (Join-Path $sp26Path "d3d12.dll") -Force
    }

    # Write Desktop and game dir dxgi_proxy.ini
    $mode = if ($rbPix.Checked) { "pix" } else { "renderdoc" }
    $iniContent = @"
[proxy]
mode=$mode
rdoc_dll=$ToolDll

[Capture]
mode=$mode
rdoc_dll=$ToolDll
"@
    $desktopIni = Join-Path $DesktopDir "dxgi_proxy.ini"
    [System.IO.File]::WriteAllText($desktopIni, $iniContent, [System.Text.Encoding]::UTF8)
    [System.IO.File]::WriteAllText((Join-Path $targetFolder "dxgi_proxy.ini"), $iniContent, [System.Text.Encoding]::UTF8)
}

function Find-NewestRdc {
    $files = Get-ChildItem -Path $DesktopDir -Filter "COD_RDC_*.rdc" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending
    if ($files -and $files.Count -gt 0) {
        return $files[0].FullName
    }
    return $null
}

# ---------------------------------------------------------------------------
# Tool Button Handlers
# ---------------------------------------------------------------------------

# 1. Trigger Capture
$btnTrigger.Add_Click({
    try {
        $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 205, 90)
        $lblToolStatus.Text = "[抓帧中] 正在向游戏发送捕获信号 (trigger_capture)..."
        $form.Refresh()

        $doneFile = Join-Path $DesktopDir "capture_completed.txt"
        if (Test-Path $doneFile) { Remove-Item $doneFile -Force -ErrorAction SilentlyContinue }

        # Broadcast triggers
        [System.IO.File]::WriteAllText((Join-Path $DesktopDir "trigger_capture"), "TRIGGER`n", [System.Text.Encoding]::UTF8)
        [System.IO.File]::WriteAllText((Join-Path $DesktopDir "trigger_capture_cod"), "TRIGGER`n", [System.Text.Encoding]::UTF8)
        [System.IO.File]::WriteAllText((Join-Path $DesktopDir "trigger_capture_modernwarfare_exe"), "TRIGGER`n", [System.Text.Encoding]::UTF8)

        # Wait up to 10 seconds for completion
        $captured = $false
        for ($i = 0; $i -lt 10; $i++) {
            Start-Sleep -Seconds 1
            if (Test-Path $doneFile) {
                $capPath = (Get-Content $doneFile -Raw).Trim()
                $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(100, 220, 160)
                $lblToolStatus.Text = "[抓帧完成] 成功截获帧文件: " + [System.IO.Path]::GetFileName($capPath)
                $captured = $true
                break
            }
        }
        if (!$captured) {
            $latest = Find-NewestRdc
            if ($latest) {
                $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(100, 220, 160)
                $lblToolStatus.Text = "[抓帧信号已发送] 发现截帧文件: " + [System.IO.Path]::GetFileName($latest)
            } else {
                $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 140, 100)
                $lblToolStatus.Text = "[提示] 信号已发送。请确认游戏已进入 3D 渲染画面，或直接在游戏内按 F12 抓帧。"
            }
        }
    } catch {
        $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 100, 100)
        $lblToolStatus.Text = "[错误] 触发抓帧失败: " + $_.Exception.Message
    }
})

# 2. Preview Thumbnail
$btnThumb.Add_Click({
    try {
        $latest = Find-NewestRdc
        if (!$latest) {
            [System.Windows.Forms.MessageBox]::Show("桌面暂未发现 COD_RDC_*.rdc 截帧文件，请先进入游戏按 F12 抓帧！", "提示", [System.Windows.Forms.MessageBoxButtons]::OK, [System.Windows.Forms.MessageBoxIcon]::Information)
            return
        }
        $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 205, 90)
        $lblToolStatus.Text = "[提取中] 正在从截帧文件提取缩略图: " + [System.IO.Path]::GetFileName($latest)
        $form.Refresh()

        $thumbJpg = Join-Path $DesktopDir "COD_latest_thumb.jpg"
        if (Test-Path $thumbJpg) { Remove-Item $thumbJpg -Force -ErrorAction SilentlyContinue }

        # Run extraction
        $proc = Start-Process -FilePath $ToolCmd -ArgumentList "thumb", "--out=`"$thumbJpg`"", "--format=jpg", "`"$latest`"" -Wait -PassThru -NoNewWindow
        if (Test-Path $thumbJpg) {
            $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(100, 220, 160)
            $lblToolStatus.Text = "[缩略图就绪] 已打开最新截帧预览: " + [System.IO.Path]::GetFileName($latest)
            Start-Process $thumbJpg
        } else {
            $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 140, 100)
            $lblToolStatus.Text = "[提示] 缩略图提取失败，请直接点击【在 qTinecmaTool 中打开】查看完整帧。"
        }
    } catch {
        $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 100, 100)
        $lblToolStatus.Text = "[错误] 提取缩略图异常: " + $_.Exception.Message
    }
})

# 3. Open in qTinecmaTool
$btnOpenRdoc.Add_Click({
    try {
        $latest = Find-NewestRdc
        if ($latest) {
            $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(100, 220, 160)
            $lblToolStatus.Text = "[已调起] 正在 qTinecmaTool 中加载: " + [System.IO.Path]::GetFileName($latest)
            Start-Process -FilePath $ToolGui -ArgumentList "`"$latest`""
        } else {
            $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(100, 220, 160)
            $lblToolStatus.Text = "[已调起] 正在启动 qTinecmaTool 查看器..."
            Start-Process -FilePath $ToolGui
        }
    } catch {
        $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 100, 100)
        $lblToolStatus.Text = "[错误] 调起 qTinecmaTool 失败: " + $_.Exception.Message
    }
})

# 4. Deploy Proxies
$btnDeploy.Add_Click({
    try {
        Deploy-ProxiesToGame -targetFolder $txtGame.Text
        $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(100, 220, 160)
        $lblToolStatus.Text = "[部署成功] 双代理 DLL (dxgi.dll / d3d12.dll) 及配置文件已同步至: " + $txtGame.Text
    } catch {
        $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 100, 100)
        $lblToolStatus.Text = "[部署失败] " + $_.Exception.Message
    }
})

# 5. View Log
$btnViewLog.Add_Click({
    $logPath = Join-Path $DesktopDir "dxgi_proxy.log"
    if (Test-Path $logPath) {
        Start-Process notepad.exe -ArgumentList "`"$logPath`""
        $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(100, 220, 160)
        $lblToolStatus.Text = "[日志就绪] 已在记事本中打开 dxgi_proxy.log"
    } else {
        $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 140, 100)
        $lblToolStatus.Text = "[提示] 桌面暂未生成 dxgi_proxy.log，请先启动游戏并完成一次加载。"
    }
})

# 6. Clean Environment
$btnClean.Add_Click({
    try {
        & sc.exe stop atvi-randgrid_sr 2>$null | Out-Null
        & net.exe stop "COD.Broker.Service" 2>$null | Out-Null
        Remove-Item (Join-Path $DesktopDir "trigger_capture*") -Force -ErrorAction SilentlyContinue
        Remove-Item (Join-Path $DesktopDir "capture_completed.txt") -Force -ErrorAction SilentlyContinue
        $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(100, 220, 160)
        $lblToolStatus.Text = "[环境清理完成] 已停止反作弊服务并清除残留的抓帧触发信号。"
    } catch {
        $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 100, 100)
        $lblToolStatus.Text = "[清理异常] " + $_.Exception.Message
    }
})

# ---------------------------------------------------------------------------
# UI Dynamic Updates
# ---------------------------------------------------------------------------
$script:isUpdatingUI = $false

$updateGameInfo = {
    if ($script:isUpdatingUI) { return }
    $script:isUpdatingUI = $true
    try {
        if ($rbGameMW.Checked) {
            if ([string]::IsNullOrWhiteSpace($txtGame.Text) -or ($txtGame.Text -eq $detectedCodHQ)) {
                if (![string]::IsNullOrWhiteSpace($detectedMW2019)) {
                    $txtGame.Text = $detectedMW2019
                }
            }
            $isFound = (![string]::IsNullOrWhiteSpace($txtGame.Text)) -and (Test-Path $txtGame.Text) -and (Test-Path (Join-Path $txtGame.Text "ModernWarfare.exe"))
            if ($isFound) {
                $lblGameTip.ForeColor = [System.Drawing.Color]::FromArgb(100, 220, 160)
                $lblGameTip.Text = "状态：已检测到 ModernWarfare.exe！`n适配：Win7 遗留库避坑完成，双代理已就绪。点击启动将拉起战网与游戏，战网客户端亦可直接进入。"
            } else {
                $lblGameTip.ForeColor = [System.Drawing.Color]::FromArgb(255, 140, 100)
                $lblGameTip.Text = "提示：未在当前目录找到 ModernWarfare.exe，请点击 [浏览...] 选择《使命召唤：现代战争》安装目录。"
            }
        } elseif ($rbGameCod.Checked) {
            if ([string]::IsNullOrWhiteSpace($txtGame.Text) -or ($txtGame.Text -eq $detectedMW2019)) {
                if (![string]::IsNullOrWhiteSpace($detectedCodHQ)) {
                    $txtGame.Text = $detectedCodHQ
                }
            }
            $isFound = (![string]::IsNullOrWhiteSpace($txtGame.Text)) -and (Test-Path $txtGame.Text) -and (Test-Path (Join-Path $txtGame.Text "cod.exe"))
            if ($isFound) {
                $lblGameTip.ForeColor = [System.Drawing.Color]::FromArgb(100, 220, 160)
                $lblGameTip.Text = "状态：已检测到 cod.exe！`n适配：双代理将自动同步至根目录及单人战役子目录 sp26\，点击启动将通过 Steam 唤醒拉起。"
            } else {
                $lblGameTip.ForeColor = [System.Drawing.Color]::FromArgb(255, 140, 100)
                $lblGameTip.Text = "提示：未在当前目录找到 cod.exe，请点击 [浏览...] 选择 Call of Duty HQ 安装目录。"
            }
        } else {
            $lblGameTip.ForeColor = [System.Drawing.Color]::FromArgb(200, 200, 200)
            $lblGameTip.Text = "提示：自定义模式。双代理将自动部署至所选目录，请确保该目录包含目标 D3D12 可执行程序。"
        }
    } finally {
        $script:isUpdatingUI = $false
    }
}

$rbGameCod.Add_CheckedChanged({ & $updateGameInfo })
$rbGameMW.Add_CheckedChanged({ & $updateGameInfo })
$rbGameCustom.Add_CheckedChanged({ & $updateGameInfo })
$txtGame.Add_TextChanged({ & $updateGameInfo })

# Set initial game selection
$hasMW = (![string]::IsNullOrWhiteSpace($GameDir)) -and ((Test-Path (Join-Path $GameDir "ModernWarfare.exe")) -or ($GameDir -match "Modern Warfare"))
$hasCod = (![string]::IsNullOrWhiteSpace($GameDir)) -and (Test-Path (Join-Path $GameDir "cod.exe"))

if ($hasMW) {
    $rbGameMW.Checked = $true
    $txtGame.Text = if (![string]::IsNullOrWhiteSpace($GameDir)) { $GameDir } else { $detectedMW2019 }
} elseif ($hasCod) {
    $rbGameCod.Checked = $true
    $txtGame.Text = $GameDir
} elseif (![string]::IsNullOrWhiteSpace($detectedMW2019) -and [string]::IsNullOrWhiteSpace($detectedCodHQ)) {
    $rbGameMW.Checked = $true
    $txtGame.Text = $detectedMW2019
} elseif (![string]::IsNullOrWhiteSpace($detectedCodHQ)) {
    $rbGameCod.Checked = $true
    $txtGame.Text = $detectedCodHQ
} else {
    $rbGameMW.Checked = $true
    $txtGame.Text = if (![string]::IsNullOrWhiteSpace($detectedMW2019)) { $detectedMW2019 } else { $GameDir }
}

& $updateGameInfo

# ---------------------------------------------------------------------------
# Launch Game Handler
# ---------------------------------------------------------------------------
$script:Action = "LAUNCH"

$btnLaunch.Add_Click({
    try {
        $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 205, 90)
        $lblToolStatus.Text = "[部署中] 正在同步代理 DLL 与配置文件..."
        $form.Refresh()

        Deploy-ProxiesToGame -targetFolder $txtGame.Text

        $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(100, 220, 160)

        if ($rbGameMW.Checked) {
            # 1. Detect Battle.net launcher
            $bnetExe = $BattleNetLauncher
            if ([string]::IsNullOrWhiteSpace($bnetExe) -or !(Test-Path $bnetExe)) {
                $bCandidates = @(
                    "C:\Program Files (x86)\Battle.net\Battle.net Launcher.exe",
                    "C:\Program Files\Battle.net\Battle.net Launcher.exe",
                    "C:\Program Files (x86)\Battle.net\Battle.net.exe",
                    "C:\Program Files\Battle.net\Battle.net.exe"
                )
                foreach ($b in $bCandidates) {
                    if (Test-Path $b) { $bnetExe = $b; break }
                }
            }

            # 2. Check if Battle.net is already running; if not, launch Battle.net
            $bnetRunning = Get-Process | Where-Object { $_.ProcessName -like "*Battle.net*" }
            if (!$bnetRunning -and (![string]::IsNullOrWhiteSpace($bnetExe)) -and (Test-Path $bnetExe)) {
                $bnetDir = [System.IO.Path]::GetDirectoryName($bnetExe)
                Start-Process -FilePath $bnetExe -WorkingDirectory $bnetDir
                Start-Sleep -Seconds 2
            }

            # 3. Launch game via Modern Warfare Launcher stub and URI protocol
            $mwLauncher = if (![string]::IsNullOrWhiteSpace($txtGame.Text)) { Join-Path $txtGame.Text "Modern Warfare Launcher.exe" } else { "" }
            $launched = $false

            if (![string]::IsNullOrWhiteSpace($mwLauncher) -and (Test-Path $mwLauncher)) {
                try {
                    Start-Process -FilePath $mwLauncher -WorkingDirectory $txtGame.Text
                    $launched = $true
                } catch {}
            }

            # Also wake up / focus MW2019 (ODIN) page in Battle.net via URI protocol
            try {
                Start-Process "battlenet://ODIN"
                $launched = $true
            } catch {}

            # If Battle.net CLI launcher exists, trigger launch argument
            if (![string]::IsNullOrWhiteSpace($bnetExe) -and (Test-Path $bnetExe)) {
                try {
                    $bnetDir = [System.IO.Path]::GetDirectoryName($bnetExe)
                    Start-Process -FilePath $bnetExe -WorkingDirectory $bnetDir -ArgumentList "--exec=`"launch ODIN`""
                    $launched = $true
                } catch {}
            }

            if ($launched) {
                $lblToolStatus.Text = "[战网已拉起] 已唤醒战网客户端并定位至《使命召唤：现代战争》！`n双代理已部署就绪，若游戏未自动进入，可在战网客户端直接点击【进入游戏】。"
            } else {
                $mwExe = if (![string]::IsNullOrWhiteSpace($txtGame.Text)) { Join-Path $txtGame.Text "ModernWarfare.exe" } else { "" }
                if (![string]::IsNullOrWhiteSpace($mwExe) -and (Test-Path $mwExe)) {
                    Start-Process -FilePath $mwExe -WorkingDirectory $txtGame.Text
                    $lblToolStatus.Text = "[游戏已拉起] ModernWarfare.exe 启动成功！(战网客户端亦可直接点击进入游戏)"
                } else {
                    $lblToolStatus.Text = "[代理已就绪] 请直接在战网 (Battle.net) 客户端点击【进入游戏】。"
                }
            }
        } elseif ($rbGameCod.Checked) {
            # Start Steam if needed
            $steamProc = Get-Process steam -ErrorAction SilentlyContinue
            if (!$steamProc -and (![string]::IsNullOrWhiteSpace($SteamDir)) -and (Test-Path (Join-Path $SteamDir "steam.exe"))) {
                Start-Process (Join-Path $SteamDir "steam.exe")
                Start-Sleep -Seconds 3
            }
            Start-Process "steam://rungameid/1938090"
            $lblToolStatus.Text = "[游戏已拉起] 已通过 Steam 唤醒拉起 Call of Duty HQ。"
        } else {
            $lblToolStatus.Text = "[代理已部署] 双代理已同步至自定义目录，请启动游戏。"
        }

        # Write output file if provided
        $targetGameType = if ($rbGameMW.Checked) { "MW2019" } elseif ($rbGameCod.Checked) { "COD_HQ" } else { "CUSTOM" }
        $targetExe = if ($rbGameMW.Checked) { "ModernWarfare.exe" } elseif ($rbGameCod.Checked) { "cod.exe" } else { "" }
        $selectedMode = if ($rbPix.Checked) { "pix" } else { "renderdoc" }
        if (![string]::IsNullOrWhiteSpace($OutputFile)) {
            $outLines = @(
                "ACTION=LAUNCH",
                "GAMEDIR=$($txtGame.Text)",
                "GAME_TYPE=$targetGameType",
                "TARGET_EXE=$targetExe",
                "MODE=$selectedMode"
            )
            [System.IO.File]::WriteAllLines($OutputFile, $outLines, [System.Text.Encoding]::UTF8)
        }

        if (!$chkKeepOpen.Checked) {
            $form.Close()
        }

    } catch {
        $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 100, 100)
        $lblToolStatus.Text = "[启动失败] " + $_.Exception.Message
    }
})

$btnExit.Add_Click({
    $script:Action = "CANCEL"
    $form.Close()
})

$form.AcceptButton = $btnLaunch
$form.CancelButton = $btnExit

$form.Add_Shown({
    $form.Activate()
    $form.BringToFront()
    $btnLaunch.Focus()
})

# Show Dialog
$res = $form.ShowDialog()

if ($script:Action -eq "LAUNCH") {
    exit 0
} else {
    exit 1
}
