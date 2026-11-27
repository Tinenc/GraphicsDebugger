param(
    [string]$RepoRoot = "",
    [string]$WeGameExe = "",
    [string]$GameDir = "",
    [string]$CaptureDir = ""
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$toolsDir = if (![string]::IsNullOrWhiteSpace($PSScriptRoot)) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $toolsDir ".."))
}

$BreadDir = Join-Path $RepoRoot "x64\BreadDevelopment"
$ToolGui = Join-Path $BreadDir "qTinecmaTool.exe"
$ToolCmd = Join-Path $BreadDir "TinecmaToolcmd.exe"
$ToolDll = Join-Path $BreadDir "TinecmaTool.dll"
$ToolX86Cmd = Join-Path $BreadDir "x86\TinecmaToolcmd.exe"
$UiScript = Join-Path $toolsDir "launch_abi_ui.py"
$TempRdcDir = Join-Path $env:TEMP "RenderDoc"
$LogDir = Join-Path $env:TEMP "TinecmaTool"

if ([string]::IsNullOrWhiteSpace($CaptureDir)) {
    $CaptureDir = Join-Path $RepoRoot "captures"
}

# ---------------------------------------------------------------------------
# Auto-detect WeGame & Arena Breakout Infinite paths
# ---------------------------------------------------------------------------
if ([string]::IsNullOrWhiteSpace($WeGameExe)) {
    $wgCandidates = @(
        "F:\WeGame\wegame.exe",
        "D:\WeGame\wegame.exe",
        "E:\WeGame\wegame.exe",
        "C:\Program Files (x86)\WeGame\wegame.exe",
        "C:\WeGame\wegame.exe"
    )
    foreach ($c in $wgCandidates) {
        if (Test-Path -LiteralPath $c -PathType Leaf) {
            $WeGameExe = $c
            break
        }
    }
    if ([string]::IsNullOrWhiteSpace($WeGameExe)) {
        try {
            $cmd = (Get-ItemProperty "Registry::HKEY_CLASSES_ROOT\wegame\shell\open\command" -ErrorAction SilentlyContinue).'(default)'
            if ($cmd -match '"([^"]+wegame\.exe)"') {
                if (Test-Path -LiteralPath $matches[1] -PathType Leaf) { $WeGameExe = $matches[1] }
            }
        } catch {}
    }
}

if ([string]::IsNullOrWhiteSpace($GameDir)) {
    $gameCandidates = @(
        "D:\WeGameApps\rail_apps\暗区突围无限(2001688)",
        "E:\WeGameApps\rail_apps\暗区突围无限(2001688)",
        "F:\WeGameApps\rail_apps\暗区突围无限(2001688)",
        "C:\WeGameApps\rail_apps\暗区突围无限(2001688)"
    )
    foreach ($g in $gameCandidates) {
        if (Test-Path -LiteralPath (Join-Path $g "UAGame\Binaries\Win64\UAGame.exe") -PathType Leaf) {
            $GameDir = $g
            break
        }
    }
}

# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------
function Stop-WeGameProcesses {
    $stopped = 0
    foreach ($name in @('wegame', 'rail', 'wegame_env', 'tcls_core')) {
        $procs = Get-Process -Name $name -ErrorAction SilentlyContinue
        if ($procs) {
            $stopped += $procs.Count
            $procs | Stop-Process -Force -ErrorAction SilentlyContinue
        }
    }
    $wgDir = if (![string]::IsNullOrWhiteSpace($txtWeGame.Text)) { Split-Path -Parent $txtWeGame.Text } else { "F:\WeGame" }
    $browsers = Get-Process -Name 'browser' -ErrorAction SilentlyContinue | Where-Object {
        try { $_.Path -and $_.Path.StartsWith($wgDir, [System.StringComparison]::OrdinalIgnoreCase) } catch { $false }
    }
    if ($browsers) {
        $stopped += $browsers.Count
        $browsers | Stop-Process -Force -ErrorAction SilentlyContinue
    }
    return $stopped
}

function Disable-DebugOpenConsole {
    $conf = Join-Path $env:APPDATA "renderdoc\renderdoc.conf"
    if (Test-Path -LiteralPath $conf -PathType Leaf) {
        $raw = Get-Content -LiteralPath $conf -Raw -Encoding UTF8
        if ($raw -match '<OpenConsole type="Boolean">true</OpenConsole>') {
            $raw = $raw -replace '<OpenConsole type="Boolean">true</OpenConsole>', '<OpenConsole type="Boolean">false</OpenConsole>'
            Set-Content -LiteralPath $conf -Value $raw -NoNewline -Encoding UTF8
        }
    }
}

function Get-LatestRdc {
    param([string]$OutDir)
    $list = @()
    foreach ($d in @($OutDir, $TempRdcDir)) {
        if (![string]::IsNullOrWhiteSpace($d) -and (Test-Path -LiteralPath $d)) {
            $list += Get-ChildItem -LiteralPath $d -Filter "*.rdc" -File -ErrorAction SilentlyContinue
        }
    }
    return ($list | Sort-Object LastWriteTime -Descending | Select-Object -First 1)
}

function Get-LatestLog {
    if (Test-Path -LiteralPath $LogDir) {
        return (Get-ChildItem -LiteralPath $LogDir -Filter "RenderDoc_*.log" -File -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending | Select-Object -First 1)
    }
    return $null
}

# ---------------------------------------------------------------------------
# Form Setup (Dark Theme matching Steam-Cod Control Center)
# ---------------------------------------------------------------------------
$form = New-Object System.Windows.Forms.Form
$form.Text = "TinecmaTool (Bread v1.46) - 暗区突围：无限 图形抓帧控制中心"
$form.Size = New-Object System.Drawing.Size(740, 730)
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
$lblTitle.Text = "TinecmaTool (Bread v1.46) - 暗区突围：无限 抓帧控制中心"
$form.Controls.Add($lblTitle)

$lblSub = New-Object System.Windows.Forms.Label
$lblSub.Location = New-Object System.Drawing.Point(26, 42)
$lblSub.Size = New-Object System.Drawing.Size(680, 20)
$lblSub.ForeColor = [System.Drawing.Color]::FromArgb(180, 180, 180)
$lblSub.Text = "WeGame 子进程链 + GameLoader Inline Hook + UAGame D3D12 (M3/M4) & BGRA D3D11 防崩放行"
$form.Controls.Add($lblSub)

# 2. GroupBox: 路径配置 (WeGame & Game Paths)
$grpPaths = New-Object System.Windows.Forms.GroupBox
$grpPaths.Location = New-Object System.Drawing.Point(24, 68)
$grpPaths.Size = New-Object System.Drawing.Size(675, 146)
$grpPaths.Text = " 选项 1：WeGame 与暗区突围：无限路径配置 "
$grpPaths.ForeColor = [System.Drawing.Color]::FromArgb(210, 210, 210)
$grpPaths.BackColor = [System.Drawing.Color]::FromArgb(40, 42, 48)

$lblWG = New-Object System.Windows.Forms.Label
$lblWG.Location = New-Object System.Drawing.Point(16, 26)
$lblWG.Size = New-Object System.Drawing.Size(92, 22)
$lblWG.Text = "WeGame 路径:"
$grpPaths.Controls.Add($lblWG)

$txtWeGame = New-Object System.Windows.Forms.TextBox
$txtWeGame.Location = New-Object System.Drawing.Point(112, 24)
$txtWeGame.Size = New-Object System.Drawing.Size(458, 24)
$txtWeGame.BackColor = [System.Drawing.Color]::FromArgb(24, 25, 28)
$txtWeGame.ForeColor = [System.Drawing.Color]::White
$txtWeGame.Text = $WeGameExe
$grpPaths.Controls.Add($txtWeGame)

$btnBrowseWG = New-Object System.Windows.Forms.Button
$btnBrowseWG.Location = New-Object System.Drawing.Point(580, 22)
$btnBrowseWG.Size = New-Object System.Drawing.Size(80, 28)
$btnBrowseWG.Text = "浏览..."
$btnBrowseWG.BackColor = [System.Drawing.Color]::FromArgb(55, 58, 66)
$btnBrowseWG.ForeColor = [System.Drawing.Color]::White
$btnBrowseWG.FlatStyle = "Flat"
$btnBrowseWG.FlatAppearance.BorderSize = 0
$btnBrowseWG.Cursor = [System.Windows.Forms.Cursors]::Hand
$btnBrowseWG.Add_Click({
    $dlg = New-Object System.Windows.Forms.OpenFileDialog
    $dlg.Title = "选择 wegame.exe"
    $dlg.Filter = "WeGame (wegame.exe)|wegame.exe|可执行文件 (*.exe)|*.exe"
    if (![string]::IsNullOrWhiteSpace($txtWeGame.Text) -and (Test-Path -LiteralPath (Split-Path -Parent $txtWeGame.Text))) {
        $dlg.InitialDirectory = Split-Path -Parent $txtWeGame.Text
    }
    if ($dlg.ShowDialog() -eq "OK") { $txtWeGame.Text = $dlg.FileName }
})
$grpPaths.Controls.Add($btnBrowseWG)

$lblGame = New-Object System.Windows.Forms.Label
$lblGame.Location = New-Object System.Drawing.Point(16, 60)
$lblGame.Size = New-Object System.Drawing.Size(92, 22)
$lblGame.Text = "暗区根目录:"
$grpPaths.Controls.Add($lblGame)

$txtGame = New-Object System.Windows.Forms.TextBox
$txtGame.Location = New-Object System.Drawing.Point(112, 58)
$txtGame.Size = New-Object System.Drawing.Size(458, 24)
$txtGame.BackColor = [System.Drawing.Color]::FromArgb(24, 25, 28)
$txtGame.ForeColor = [System.Drawing.Color]::White
$txtGame.Text = $GameDir
$grpPaths.Controls.Add($txtGame)

$btnBrowseGame = New-Object System.Windows.Forms.Button
$btnBrowseGame.Location = New-Object System.Drawing.Point(580, 56)
$btnBrowseGame.Size = New-Object System.Drawing.Size(80, 28)
$btnBrowseGame.Text = "浏览..."
$btnBrowseGame.BackColor = [System.Drawing.Color]::FromArgb(55, 58, 66)
$btnBrowseGame.ForeColor = [System.Drawing.Color]::White
$btnBrowseGame.FlatStyle = "Flat"
$btnBrowseGame.FlatAppearance.BorderSize = 0
$btnBrowseGame.Cursor = [System.Windows.Forms.Cursors]::Hand
$btnBrowseGame.Add_Click({
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    $dlg.Description = "选择暗区突围无限(2001688)安装根目录"
    if (![string]::IsNullOrWhiteSpace($txtGame.Text) -and (Test-Path -LiteralPath $txtGame.Text)) {
        $dlg.SelectedPath = $txtGame.Text
    }
    if ($dlg.ShowDialog() -eq "OK") { $txtGame.Text = $dlg.SelectedPath }
})
$grpPaths.Controls.Add($btnBrowseGame)

$lblPathStatus = New-Object System.Windows.Forms.Label
$lblPathStatus.Location = New-Object System.Drawing.Point(16, 92)
$lblPathStatus.Size = New-Object System.Drawing.Size(645, 44)
$lblPathStatus.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 8.8)
$grpPaths.Controls.Add($lblPathStatus)

$form.Controls.Add($grpPaths)

# 3. GroupBox: 输出目录与启动选项
$grpOpts = New-Object System.Windows.Forms.GroupBox
$grpOpts.Location = New-Object System.Drawing.Point(24, 220)
$grpOpts.Size = New-Object System.Drawing.Size(675, 110)
$grpOpts.Text = " 选项 2：抓帧归档目录与启动环境 "
$grpOpts.ForeColor = [System.Drawing.Color]::FromArgb(210, 210, 210)
$grpOpts.BackColor = [System.Drawing.Color]::FromArgb(40, 42, 48)

$lblCapDir = New-Object System.Windows.Forms.Label
$lblCapDir.Location = New-Object System.Drawing.Point(16, 26)
$lblCapDir.Size = New-Object System.Drawing.Size(92, 22)
$lblCapDir.Text = "RDC 归档目录:"
$grpOpts.Controls.Add($lblCapDir)

$txtCapDir = New-Object System.Windows.Forms.TextBox
$txtCapDir.Location = New-Object System.Drawing.Point(112, 24)
$txtCapDir.Size = New-Object System.Drawing.Size(372, 24)
$txtCapDir.BackColor = [System.Drawing.Color]::FromArgb(24, 25, 28)
$txtCapDir.ForeColor = [System.Drawing.Color]::White
$txtCapDir.Text = $CaptureDir
$grpOpts.Controls.Add($txtCapDir)

$btnBrowseCap = New-Object System.Windows.Forms.Button
$btnBrowseCap.Location = New-Object System.Drawing.Point(492, 22)
$btnBrowseCap.Size = New-Object System.Drawing.Size(78, 28)
$btnBrowseCap.Text = "浏览..."
$btnBrowseCap.BackColor = [System.Drawing.Color]::FromArgb(55, 58, 66)
$btnBrowseCap.ForeColor = [System.Drawing.Color]::White
$btnBrowseCap.FlatStyle = "Flat"
$btnBrowseCap.FlatAppearance.BorderSize = 0
$btnBrowseCap.Cursor = [System.Windows.Forms.Cursors]::Hand
$btnBrowseCap.Add_Click({
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    $dlg.Description = "选择 RDC 归档保存目录"
    if (![string]::IsNullOrWhiteSpace($txtCapDir.Text) -and (Test-Path -LiteralPath $txtCapDir.Text)) {
        $dlg.SelectedPath = $txtCapDir.Text
    }
    if ($dlg.ShowDialog() -eq "OK") { $txtCapDir.Text = $dlg.SelectedPath }
})
$grpOpts.Controls.Add($btnBrowseCap)

$btnOpenCapDir = New-Object System.Windows.Forms.Button
$btnOpenCapDir.Location = New-Object System.Drawing.Point(578, 22)
$btnOpenCapDir.Size = New-Object System.Drawing.Size(82, 28)
$btnOpenCapDir.Text = "打开目录"
$btnOpenCapDir.BackColor = [System.Drawing.Color]::FromArgb(55, 58, 66)
$btnOpenCapDir.ForeColor = [System.Drawing.Color]::White
$btnOpenCapDir.FlatStyle = "Flat"
$btnOpenCapDir.FlatAppearance.BorderSize = 0
$btnOpenCapDir.Cursor = [System.Windows.Forms.Cursors]::Hand
$btnOpenCapDir.Add_Click({
    if (![string]::IsNullOrWhiteSpace($txtCapDir.Text)) {
        New-Item -ItemType Directory -Force -Path $txtCapDir.Text | Out-Null
        Start-Process explorer.exe $txtCapDir.Text
    }
})
$grpOpts.Controls.Add($btnOpenCapDir)

$chkKillOldWG = New-Object System.Windows.Forms.CheckBox
$chkKillOldWG.Location = New-Object System.Drawing.Point(16, 58)
$chkKillOldWG.Size = New-Object System.Drawing.Size(340, 22)
$chkKillOldWG.ForeColor = [System.Drawing.Color]::FromArgb(100, 220, 160)
$chkKillOldWG.Text = "启动前自动关闭旧 WeGame 及残留子进程 (推荐)"
$chkKillOldWG.Checked = $true
$grpOpts.Controls.Add($chkKillOldWG)

$chkNoConsole = New-Object System.Windows.Forms.CheckBox
$chkNoConsole.Location = New-Object System.Drawing.Point(365, 58)
$chkNoConsole.Size = New-Object System.Drawing.Size(300, 22)
$chkNoConsole.ForeColor = [System.Drawing.Color]::FromArgb(160, 210, 255)
$chkNoConsole.Text = "关闭 Debug Console 黑框弹窗 (推荐)"
$chkNoConsole.Checked = $true
$grpOpts.Controls.Add($chkNoConsole)

$lblOptNote = New-Object System.Windows.Forms.Label
$lblOptNote.Location = New-Object System.Drawing.Point(16, 84)
$lblOptNote.Size = New-Object System.Drawing.Size(645, 20)
$lblOptNote.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 8.5)
$lblOptNote.ForeColor = [System.Drawing.Color]::FromArgb(180, 180, 180)
$lblOptNote.Text = "后端：x64\BreadDevelopment (v1.46) + x86\TinecmaToolcmd.exe 跨位宽提升；captures\*.rdc 已被 .gitignore 忽略。"
$grpOpts.Controls.Add($lblOptNote)

$form.Controls.Add($grpOpts)

# 4. GroupBox: 抓帧与调试工具箱
$grpTools = New-Object System.Windows.Forms.GroupBox
$grpTools.Location = New-Object System.Drawing.Point(24, 338)
$grpTools.Size = New-Object System.Drawing.Size(675, 158)
$grpTools.Text = " 抓帧与调试工具箱 (Capture & Inspection Tools) "
$grpTools.ForeColor = [System.Drawing.Color]::FromArgb(210, 210, 210)
$grpTools.BackColor = [System.Drawing.Color]::FromArgb(40, 42, 48)

$btnCollect = New-Object System.Windows.Forms.Button
$btnCollect.Location = New-Object System.Drawing.Point(16, 26)
$btnCollect.Size = New-Object System.Drawing.Size(205, 36)
$btnCollect.Text = "[收集] 归档最新截帧到项目"
$btnCollect.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5, [System.Drawing.FontStyle]::Bold)
$btnCollect.BackColor = [System.Drawing.Color]::FromArgb(0, 130, 85)
$btnCollect.ForeColor = [System.Drawing.Color]::White
$btnCollect.FlatStyle = "Flat"
$btnCollect.FlatAppearance.BorderSize = 0
$btnCollect.Cursor = [System.Windows.Forms.Cursors]::Hand
$grpTools.Controls.Add($btnCollect)

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

$btnCheckLink = New-Object System.Windows.Forms.Button
$btnCheckLink.Location = New-Object System.Drawing.Point(16, 70)
$btnCheckLink.Size = New-Object System.Drawing.Size(205, 34)
$btnCheckLink.Text = "[状态] 检测注入与 Present 链路"
$btnCheckLink.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
$btnCheckLink.BackColor = [System.Drawing.Color]::FromArgb(60, 64, 75)
$btnCheckLink.ForeColor = [System.Drawing.Color]::White
$btnCheckLink.FlatStyle = "Flat"
$btnCheckLink.FlatAppearance.BorderSize = 0
$btnCheckLink.Cursor = [System.Windows.Forms.Cursors]::Hand
$grpTools.Controls.Add($btnCheckLink)

$btnViewLog = New-Object System.Windows.Forms.Button
$btnViewLog.Location = New-Object System.Drawing.Point(235, 70)
$btnViewLog.Size = New-Object System.Drawing.Size(205, 34)
$btnViewLog.Text = "[日志] 查看最新 TinecmaTool 日志"
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
$btnClean.Text = "[清理] 退出 WeGame 与残留进程"
$btnClean.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
$btnClean.BackColor = [System.Drawing.Color]::FromArgb(60, 64, 75)
$btnClean.ForeColor = [System.Drawing.Color]::White
$btnClean.FlatStyle = "Flat"
$btnClean.FlatAppearance.BorderSize = 0
$btnClean.Cursor = [System.Windows.Forms.Cursors]::Hand
$grpTools.Controls.Add($btnClean)

$lblToolStatus = New-Object System.Windows.Forms.Label
$lblToolStatus.Location = New-Object System.Drawing.Point(16, 112)
$lblToolStatus.Size = New-Object System.Drawing.Size(645, 38)
$lblToolStatus.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 8.8, [System.Drawing.FontStyle]::Bold)
$lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(100, 220, 160)
$lblToolStatus.Text = "[就绪] 点击下方【一键启动】自动拉起 qTinecmaTool 与 WeGame，在 WeGame 中点击启动暗区即可自动挂钩。"
$grpTools.Controls.Add($lblToolStatus)

$form.Controls.Add($grpTools)

# 5. GroupBox: 链路说明
$grpTips = New-Object System.Windows.Forms.GroupBox
$grpTips.Location = New-Object System.Drawing.Point(24, 504)
$grpTips.Size = New-Object System.Drawing.Size(675, 80)
$grpTips.Text = " 暗区突围：无限 抓帧链路说明 "
$grpTips.ForeColor = [System.Drawing.Color]::FromArgb(210, 210, 210)
$grpTips.BackColor = [System.Drawing.Color]::FromArgb(40, 42, 48)

$lblTip1 = New-Object System.Windows.Forms.Label
$lblTip1.Location = New-Object System.Drawing.Point(16, 22)
$lblTip1.Size = New-Object System.Drawing.Size(645, 22)
$lblTip1.ForeColor = [System.Drawing.Color]::FromArgb(230, 230, 230)
$lblTip1.Text = "[抓帧步骤] 1. 点下方启动 -> 2. 在 WeGame 启动暗区 -> 3. 进大厅后在 qTinecmaTool 的 UAGame 页点立即捕获帧（或 F12）。"
$grpTips.Controls.Add($lblTip1)

$lblTip2 = New-Object System.Windows.Forms.Label
$lblTip2.Location = New-Object System.Drawing.Point(16, 48)
$lblTip2.Size = New-Object System.Drawing.Size(645, 24)
$lblTip2.ForeColor = [System.Drawing.Color]::FromArgb(100, 215, 255)
$lblTip2.Text = "[已集成修复] GameLoader.exe Detours Inline CreateProcess + UAGame D3D12 延迟导入接管 + 辅助 BGRA D3D11 透传防 dcomp 崩。"
$grpTips.Controls.Add($lblTip2)

$form.Controls.Add($grpTips)

# 6. Action Banner & Buttons
$lblEnterNotice = New-Object System.Windows.Forms.Label
$lblEnterNotice.Location = New-Object System.Drawing.Point(24, 594)
$lblEnterNotice.Size = New-Object System.Drawing.Size(390, 22)
$lblEnterNotice.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5, [System.Drawing.FontStyle]::Bold)
$lblEnterNotice.ForeColor = [System.Drawing.Color]::FromArgb(255, 215, 80)
$lblEnterNotice.Text = "确认路径无误后，按 Enter 键一键启动抓帧环境"
$form.Controls.Add($lblEnterNotice)

$chkKeepOpen = New-Object System.Windows.Forms.CheckBox
$chkKeepOpen.Location = New-Object System.Drawing.Point(420, 592)
$chkKeepOpen.Size = New-Object System.Drawing.Size(275, 24)
$chkKeepOpen.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9)
$chkKeepOpen.ForeColor = [System.Drawing.Color]::FromArgb(200, 220, 255)
$chkKeepOpen.Text = "启动后保持控制面板开启 (推荐)"
$chkKeepOpen.Checked = $true
$form.Controls.Add($chkKeepOpen)

$btnLaunch = New-Object System.Windows.Forms.Button
$btnLaunch.Location = New-Object System.Drawing.Point(24, 624)
$btnLaunch.Size = New-Object System.Drawing.Size(380, 46)
$btnLaunch.Text = "▶ 一键启动 qTinecmaTool 并拉起 WeGame (Enter)"
$btnLaunch.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 10.5, [System.Drawing.FontStyle]::Bold)
$btnLaunch.BackColor = [System.Drawing.Color]::FromArgb(0, 150, 100)
$btnLaunch.ForeColor = [System.Drawing.Color]::White
$btnLaunch.FlatStyle = "Flat"
$btnLaunch.FlatAppearance.BorderSize = 0
$btnLaunch.Cursor = [System.Windows.Forms.Cursors]::Hand

$btnExit = New-Object System.Windows.Forms.Button
$btnExit.Location = New-Object System.Drawing.Point(420, 624)
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
# Path Validation Updater
# ---------------------------------------------------------------------------
$updatePathStatus = {
    $wgOk = (![string]::IsNullOrWhiteSpace($txtWeGame.Text)) -and (Test-Path -LiteralPath $txtWeGame.Text -PathType Leaf)
    $uaExe = if (![string]::IsNullOrWhiteSpace($txtGame.Text)) { Join-Path $txtGame.Text "UAGame\Binaries\Win64\UAGame.exe" } else { "" }
    $gameOk = (![string]::IsNullOrWhiteSpace($uaExe)) -and (Test-Path -LiteralPath $uaExe -PathType Leaf)
    $guiOk = Test-Path -LiteralPath $ToolGui -PathType Leaf
    $x86Ok = Test-Path -LiteralPath $ToolX86Cmd -PathType Leaf

    if ($wgOk -and $gameOk -and $guiOk -and $x86Ok) {
        $lblPathStatus.ForeColor = [System.Drawing.Color]::FromArgb(100, 220, 160)
        $lblPathStatus.Text = "状态：全部就绪！已检测到 wegame.exe、UAGame.exe、x64\BreadDevelopment 及 x86 提升辅助程序。`n可直接点击下方【一键启动】开始抓帧。"
    } else {
        $missing = @()
        if (!$wgOk) { $missing += "wegame.exe" }
        if (!$gameOk) { $missing += "UAGame.exe" }
        if (!$guiOk) { $missing += "qTinecmaTool.exe" }
        if (!$x86Ok) { $missing += "x86\TinecmaToolcmd.exe" }
        $lblPathStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 140, 100)
        $lblPathStatus.Text = "警告：缺少以下组件或路径未找到：" + ($missing -join ", ") + "`n请核对路径后再启动。"
    }
}

$txtWeGame.Add_TextChanged({ & $updatePathStatus })
$txtGame.Add_TextChanged({ & $updatePathStatus })
& $updatePathStatus

# ---------------------------------------------------------------------------
# Tool Button Handlers
# ---------------------------------------------------------------------------
$btnCollect.Add_Click({
    try {
        $outDir = $txtCapDir.Text
        if ([string]::IsNullOrWhiteSpace($outDir)) { $outDir = $CaptureDir }
        New-Item -ItemType Directory -Force -Path $outDir | Out-Null

        $srcFiles = @()
        if (Test-Path -LiteralPath $TempRdcDir) {
            $srcFiles = Get-ChildItem -LiteralPath $TempRdcDir -Filter "*.rdc" -File -ErrorAction SilentlyContinue |
                        Where-Object { $_.Name -match 'frame\d+\.rdc$' } |
                        Sort-Object LastWriteTime -Descending
        }
        if ($srcFiles.Count -eq 0) {
            $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 205, 90)
            $lblToolStatus.Text = "[未找到] 临时目录 $TempRdcDir 中暂无新的截帧 RDC 文件。"
            return
        }

        $copied = 0
        $skipped = 0
        foreach ($f in $srcFiles) {
            $dstName = if ($f.Name -match '(frame\d+)\.rdc$') { "ABI_$($matches[1]).rdc" } else { $f.Name }
            $dstPath = Join-Path $outDir $dstName
            if ((Test-Path -LiteralPath $dstPath) -and ((Get-Item -LiteralPath $dstPath).Length -eq $f.Length)) {
                $skipped++
                continue
            }
            Copy-Item -LiteralPath $f.FullName -Destination $dstPath -Force
            $copied++
        }
        $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(100, 220, 160)
        $lblToolStatus.Text = "[归档完成] 新复制 $copied 帧（跳过已存在 $skipped 帧）至：$outDir"
    } catch {
        $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 100, 100)
        $lblToolStatus.Text = "[归档失败] " + $_.Exception.Message
    }
})

$btnThumb.Add_Click({
    try {
        $latest = Get-LatestRdc -OutDir $txtCapDir.Text
        if (!$latest) {
            $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 205, 90)
            $lblToolStatus.Text = "[无截帧] 未在归档目录或临时目录找到 .rdc 文件。"
            return
        }
        $png = Join-Path $env:TEMP ($latest.BaseName + "_thumb.png")
        & $ToolCmd thumb --out $png $latest.FullName | Out-Null
        if (Test-Path -LiteralPath $png) {
            Start-Process $png
            $mb = [math]::Round($latest.Length / 1MB, 1)
            $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(100, 220, 160)
            $lblToolStatus.Text = "[缩略图已打开] $($latest.Name) ($mb MB)"
        } else {
            $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 120, 120)
            $lblToolStatus.Text = "[提取失败] 无法从 $($latest.Name) 提取缩略图。"
        }
    } catch {
        $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 100, 100)
        $lblToolStatus.Text = "[预览失败] " + $_.Exception.Message
    }
})

$btnOpenRdoc.Add_Click({
    try {
        $latest = Get-LatestRdc -OutDir $txtCapDir.Text
        if ($latest) {
            Start-Process -FilePath $ToolGui -WorkingDirectory $BreadDir -ArgumentList ('"' + $latest.FullName + '"')
            $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(100, 220, 160)
            $lblToolStatus.Text = "[已打开] 正在 qTinecmaTool 中加载 $($latest.Name)..."
        } else {
            Start-Process -FilePath $ToolGui -WorkingDirectory $BreadDir
            $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 205, 90)
            $lblToolStatus.Text = "[已打开] 未找到最新 RDC，已直接启动 qTinecmaTool。"
        }
    } catch {
        $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 100, 100)
        $lblToolStatus.Text = "[打开失败] " + $_.Exception.Message
    }
})

$btnCheckLink.Add_Click({
    try {
        $hasGui = [bool](Get-Process -Name 'qTinecmaTool' -ErrorAction SilentlyContinue)
        $hasWG = [bool](Get-Process -Name 'wegame' -ErrorAction SilentlyContinue)
        $hasGL = [bool](Get-Process -Name 'GameLoader' -ErrorAction SilentlyContinue)
        $hasUA = [bool](Get-Process -Name 'UAGame' -ErrorAction SilentlyContinue)

        $log = Get-LatestLog
        $hasPresent = $false
        $hasBypass = $false
        if ($log) {
            $hasPresent = [bool](Select-String -Path $log.FullName -Pattern "Graphics first present" -Quiet -ErrorAction SilentlyContinue)
            $hasBypass = [bool](Select-String -Path $log.FullName -Pattern "Bypassing auxiliary BGRA D3D11" -Quiet -ErrorAction SilentlyContinue)
        }
        $lblToolStatus.ForeColor = if ($hasUA -and $hasPresent) { [System.Drawing.Color]::FromArgb(100, 220, 160) } else { [System.Drawing.Color]::FromArgb(255, 205, 90) }
        $lblToolStatus.Text = "进程：GUI=$hasGui | WeGame=$hasWG | GameLoader=$hasGL | UAGame=$hasUA`n日志：FirstPresent=$hasPresent | BGRA放行=$hasBypass"
    } catch {
        $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 100, 100)
        $lblToolStatus.Text = "[检测异常] " + $_.Exception.Message
    }
})

$btnViewLog.Add_Click({
    $log = Get-LatestLog
    if ($log) {
        Start-Process notepad.exe $log.FullName
        $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(100, 220, 160)
        $lblToolStatus.Text = "[日志已打开] $($log.Name)"
    } else {
        $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 205, 90)
        $lblToolStatus.Text = "[无日志] 未在 $LogDir 找到 RenderDoc_*.log。"
    }
})

$btnClean.Add_Click({
    $count = Stop-WeGameProcesses
    $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(100, 220, 160)
    $lblToolStatus.Text = "[清理完成] 已退出 $count 个 WeGame / 托盘 / 内嵌浏览器残留进程。"
})

# ---------------------------------------------------------------------------
# Launch Handler
# ---------------------------------------------------------------------------
$btnLaunch.Add_Click({
    try {
        $wgExe = $txtWeGame.Text.Trim()
        if ([string]::IsNullOrWhiteSpace($wgExe) -or !(Test-Path -LiteralPath $wgExe -PathType Leaf)) {
            throw "未找到 WeGame 可执行文件: $wgExe"
        }
        if (!(Test-Path -LiteralPath $ToolGui -PathType Leaf)) {
            throw "未找到 qTinecmaTool.exe: $ToolGui"
        }
        if (!(Test-Path -LiteralPath $ToolX86Cmd -PathType Leaf)) {
            throw "未找到 32 位提升辅助程序: $ToolX86Cmd"
        }

        if ($chkNoConsole.Checked) {
            Disable-DebugOpenConsole
        }

        if ($chkKillOldWG.Checked) {
            $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 205, 90)
            $lblToolStatus.Text = "[准备中] 正在清理旧 WeGame 进程..."
            $form.Refresh()
            $null = Stop-WeGameProcesses
            Start-Sleep -Seconds 1
        } elseif (Get-Process -Name 'wegame' -ErrorAction SilentlyContinue) {
            throw "WeGame 已在运行，请勾选【启动前自动关闭旧 WeGame】或手动从托盘完全退出后再启动。"
        }

        $env:DISABLE_VULKAN_TINECMATOOL_CAPTURE_1_44 = '1'
        $env:DISABLE_VULKAN_TINECMATOOL_CAPTURE_1_46 = '1'
        $env:ABI_WEGAME_EXE = $wgExe

        Start-Process -FilePath $ToolGui -WorkingDirectory $BreadDir -ArgumentList @('--ui-script', ('"' + $UiScript + '"')) | Out-Null

        $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(100, 220, 160)
        $lblToolStatus.Text = "[已拉起] qTinecmaTool 正在启动并将在 1.5 秒后自动拉起 WeGame。`n随后在 WeGame 中点击【启动 暗区突围：无限】即可，抓完后可点上方【收集】归档到 captures\。"

        if (!$chkKeepOpen.Checked) {
            $form.Close()
        }
    } catch {
        $lblToolStatus.ForeColor = [System.Drawing.Color]::FromArgb(255, 100, 100)
        $lblToolStatus.Text = "[启动失败] " + $_.Exception.Message
    }
})

$btnExit.Add_Click({ $form.Close() })

$form.AcceptButton = $btnLaunch
$form.CancelButton = $btnExit

$form.Add_Shown({
    $form.Activate()
    $form.BringToFront()
    $btnLaunch.Focus()
})

[void]$form.ShowDialog()
exit 0
