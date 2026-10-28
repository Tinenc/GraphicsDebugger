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

$form = New-Object System.Windows.Forms.Form
$form.Text = "TinecmaTool - 准备就绪 (按回车继续启动游戏)"
$form.Size = New-Object System.Drawing.Size(720, 595)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedDialog"
$form.MaximizeBox = $false
$form.MinimizeBox = $true
$form.TopMost = $true
$form.BackColor = [System.Drawing.Color]::FromArgb(32, 33, 36)
$form.ForeColor = [System.Drawing.Color]::White
$form.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5)

# 1. Title Banner
$lblTitle = New-Object System.Windows.Forms.Label
$lblTitle.Location = New-Object System.Drawing.Point(24, 18)
$lblTitle.Size = New-Object System.Drawing.Size(660, 30)
$lblTitle.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 13, [System.Drawing.FontStyle]::Bold)
$lblTitle.ForeColor = [System.Drawing.Color]::FromArgb(0, 215, 160)
$lblTitle.Text = "TinecmaTool (Stealth RenderDoc) - 启动配置向导"
$form.Controls.Add($lblTitle)

$lblSub = New-Object System.Windows.Forms.Label
$lblSub.Location = New-Object System.Drawing.Point(26, 48)
$lblSub.Size = New-Object System.Drawing.Size(660, 22)
$lblSub.ForeColor = [System.Drawing.Color]::FromArgb(180, 180, 180)
$lblSub.Text = "DirectX 12 商业游戏静默抓帧代理层已部署完毕，请核对启动配置："
$form.Controls.Add($lblSub)

# 2. GroupBox: 游戏路径设置
$grpGame = New-Object System.Windows.Forms.GroupBox
$grpGame.Location = New-Object System.Drawing.Point(24, 76)
$grpGame.Size = New-Object System.Drawing.Size(655, 96)
$grpGame.Text = " 选项 1：游戏路径设置 (Game Directory) "
$grpGame.ForeColor = [System.Drawing.Color]::FromArgb(210, 210, 210)
$grpGame.BackColor = [System.Drawing.Color]::FromArgb(40, 42, 48)

$lblGame = New-Object System.Windows.Forms.Label
$lblGame.Location = New-Object System.Drawing.Point(16, 26)
$lblGame.Size = New-Object System.Drawing.Size(85, 22)
$lblGame.Text = "游戏根目录:"
$grpGame.Controls.Add($lblGame)

$txtGame = New-Object System.Windows.Forms.TextBox
$txtGame.Location = New-Object System.Drawing.Point(105, 24)
$txtGame.Size = New-Object System.Drawing.Size(445, 24)
$txtGame.BackColor = [System.Drawing.Color]::FromArgb(24, 25, 28)
$txtGame.ForeColor = [System.Drawing.Color]::White
$txtGame.Text = $GameDir
$grpGame.Controls.Add($txtGame)

$btnBrowse = New-Object System.Windows.Forms.Button
$btnBrowse.Location = New-Object System.Drawing.Point(560, 22)
$btnBrowse.Size = New-Object System.Drawing.Size(80, 28)
$btnBrowse.Text = "浏览..."
$btnBrowse.BackColor = [System.Drawing.Color]::FromArgb(55, 58, 66)
$btnBrowse.ForeColor = [System.Drawing.Color]::White
$btnBrowse.FlatStyle = "Flat"
$btnBrowse.FlatAppearance.BorderSize = 0
$btnBrowse.Cursor = [System.Windows.Forms.Cursors]::Hand
$btnBrowse.Add_Click({
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    $dlg.Description = "选择 Call of Duty HQ 游戏安装目录 (包含 cod.exe)"
    if (Test-Path $txtGame.Text) { $dlg.SelectedPath = $txtGame.Text }
    if ($dlg.ShowDialog() -eq "OK") {
        $txtGame.Text = $dlg.SelectedPath
    }
})
$grpGame.Controls.Add($btnBrowse)

$lblGameTip = New-Object System.Windows.Forms.Label
$lblGameTip.Location = New-Object System.Drawing.Point(16, 58)
$lblGameTip.Size = New-Object System.Drawing.Size(625, 30)
$lblGameTip.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 8.8)
$lblGameTip.ForeColor = [System.Drawing.Color]::FromArgb(255, 205, 90)
$lblGameTip.Text = "提示：请确认目录包含 cod.exe。代理 DLL 将自动同步部署至该目录及单人战役子目录 sp26\"
$grpGame.Controls.Add($lblGameTip)

$form.Controls.Add($grpGame)

# 3. GroupBox: 抓帧后端选择
$grpBackend = New-Object System.Windows.Forms.GroupBox
$grpBackend.Location = New-Object System.Drawing.Point(24, 180)
$grpBackend.Size = New-Object System.Drawing.Size(655, 142)
$grpBackend.Text = " 选项 2：抓帧后端选择 (Capture Backend) "
$grpBackend.ForeColor = [System.Drawing.Color]::FromArgb(210, 210, 210)
$grpBackend.BackColor = [System.Drawing.Color]::FromArgb(40, 42, 48)

$rbRdoc = New-Object System.Windows.Forms.RadioButton
$rbRdoc.Location = New-Object System.Drawing.Point(16, 24)
$rbRdoc.Size = New-Object System.Drawing.Size(320, 24)
$rbRdoc.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5, [System.Drawing.FontStyle]::Bold)
$rbRdoc.ForeColor = [System.Drawing.Color]::FromArgb(0, 230, 170)
$rbRdoc.Text = "TinecmaTool (RenderDoc v1.44) - 推荐"
$rbRdoc.Checked = $true
$grpBackend.Controls.Add($rbRdoc)

$lblRdocTip = New-Object System.Windows.Forms.Label
$lblRdocTip.Location = New-Object System.Drawing.Point(36, 48)
$lblRdocTip.Size = New-Object System.Drawing.Size(605, 30)
$lblRdocTip.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 8.8)
$lblRdocTip.ForeColor = [System.Drawing.Color]::FromArgb(190, 190, 190)
$lblRdocTip.Text = "提示：深度逆向调试首选。基于 1.33s 同步直写与 SEH 容错，完整抓取所有 DrawCall、PSO 与着色器字节码。"
$grpBackend.Controls.Add($lblRdocTip)

$rbPix = New-Object System.Windows.Forms.RadioButton
$rbPix.Location = New-Object System.Drawing.Point(16, 80)
$rbPix.Size = New-Object System.Drawing.Size(320, 24)
$rbPix.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5, [System.Drawing.FontStyle]::Bold)
$rbPix.ForeColor = [System.Drawing.Color]::FromArgb(120, 180, 255)
$rbPix.Text = "Microsoft PIX (WinPixGpuCapturer)"
$grpBackend.Controls.Add($rbPix)

$lblPixTip = New-Object System.Windows.Forms.Label
$lblPixTip.Location = New-Object System.Drawing.Point(36, 104)
$lblPixTip.Size = New-Object System.Drawing.Size(605, 30)
$lblPixTip.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 8.8)
$lblPixTip.ForeColor = [System.Drawing.Color]::FromArgb(190, 190, 190)
$lblPixTip.Text = "提示：微软官方性能剖析首选。支持 GPU 时间线、GPU 时钟周期、绘制阶段耗时分析，生成 .wpix 帧文件。"
$grpBackend.Controls.Add($lblPixTip)

$form.Controls.Add($grpBackend)

# 4. GroupBox: 使用说明与防封提示 (说明信息区，非勾选选项)
$grpTips = New-Object System.Windows.Forms.GroupBox
$grpTips.Location = New-Object System.Drawing.Point(24, 330)
$grpTips.Size = New-Object System.Drawing.Size(655, 106)
$grpTips.Text = " 使用说明与防封提示 (Instructions && Safety) "
$grpTips.ForeColor = [System.Drawing.Color]::FromArgb(210, 210, 210)
$grpTips.BackColor = [System.Drawing.Color]::FromArgb(40, 42, 48)

$lblTip1 = New-Object System.Windows.Forms.Label
$lblTip1.Location = New-Object System.Drawing.Point(16, 24)
$lblTip1.Size = New-Object System.Drawing.Size(625, 20)
$lblTip1.ForeColor = [System.Drawing.Color]::FromArgb(230, 230, 230)
$lblTip1.Text = "● 触发方式 1：进入游戏后左上角呈现 HUD 浮层，随时按下 [F12] 键即可极速无感抓帧。"
$grpTips.Controls.Add($lblTip1)

$lblTip2 = New-Object System.Windows.Forms.Label
$lblTip2.Location = New-Object System.Drawing.Point(16, 48)
$lblTip2.Size = New-Object System.Drawing.Size(625, 20)
$lblTip2.ForeColor = [System.Drawing.Color]::FromArgb(230, 230, 230)
$lblTip2.Text = "● 触发方式 2：在控制台输入 [C] 键，或向桌面写入 trigger_capture_cod 文件即可抓帧。"
$grpTips.Controls.Add($lblTip2)

$lblTip3 = New-Object System.Windows.Forms.Label
$lblTip3.Location = New-Object System.Drawing.Point(16, 74)
$lblTip3.Size = New-Object System.Drawing.Size(625, 24)
$lblTip3.ForeColor = [System.Drawing.Color]::FromArgb(100, 215, 255)
$lblTip3.Text = "● 安全防护保障：PEB 双向链表已物理隐匿，已屏蔽端口并隔离看门狗 0x00001337，反作弊零报警。"
$grpTips.Controls.Add($lblTip3)

$form.Controls.Add($grpTips)

# 5. Prominent Enter Key Instruction Banner
$lblEnterNotice = New-Object System.Windows.Forms.Label
$lblEnterNotice.Location = New-Object System.Drawing.Point(24, 448)
$lblEnterNotice.Size = New-Object System.Drawing.Size(655, 26)
$lblEnterNotice.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 10.5, [System.Drawing.FontStyle]::Bold)
$lblEnterNotice.ForeColor = [System.Drawing.Color]::FromArgb(255, 215, 80)
$lblEnterNotice.Text = "已取消时间限制：确认配置无误后，直接按下 [Enter 回车键] 即可启动游戏"
$form.Controls.Add($lblEnterNotice)

# 6. Action Buttons
$btnLaunch = New-Object System.Windows.Forms.Button
$btnLaunch.Location = New-Object System.Drawing.Point(24, 484)
$btnLaunch.Size = New-Object System.Drawing.Size(370, 46)
$btnLaunch.Text = "立即启动游戏 (按 Enter 继续)"
$btnLaunch.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 11, [System.Drawing.FontStyle]::Bold)
$btnLaunch.BackColor = [System.Drawing.Color]::FromArgb(0, 150, 100)
$btnLaunch.ForeColor = [System.Drawing.Color]::White
$btnLaunch.FlatStyle = "Flat"
$btnLaunch.FlatAppearance.BorderSize = 0
$btnLaunch.Cursor = [System.Windows.Forms.Cursors]::Hand

$btnCancel = New-Object System.Windows.Forms.Button
$btnCancel.Location = New-Object System.Drawing.Point(415, 484)
$btnCancel.Size = New-Object System.Drawing.Size(264, 46)
$btnCancel.Text = "取消退出 (Esc)"
$btnCancel.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 10.5)
$btnCancel.BackColor = [System.Drawing.Color]::FromArgb(70, 72, 80)
$btnCancel.ForeColor = [System.Drawing.Color]::White
$btnCancel.FlatStyle = "Flat"
$btnCancel.FlatAppearance.BorderSize = 0
$btnCancel.Cursor = [System.Windows.Forms.Cursors]::Hand

$form.Controls.Add($btnLaunch)
$form.Controls.Add($btnCancel)

# Set button DialogResults and keyboard shortcuts
$btnLaunch.DialogResult = [System.Windows.Forms.DialogResult]::OK
$btnCancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel

$form.AcceptButton = $btnLaunch
$form.CancelButton = $btnCancel

# Bring form to front and focus launch button on shown
$form.Add_Shown({
    $form.Activate()
    $form.BringToFront()
    $btnLaunch.Focus()
})

$script:Action = "LAUNCH"

$btnLaunch.Add_Click({
    $script:Action = "LAUNCH"
})

$btnCancel.Add_Click({
    $script:Action = "CANCEL"
})

# Display Dialog (Blocks until user hits Enter or Esc or clicks buttons)
$res = $form.ShowDialog()

if ($res -eq [System.Windows.Forms.DialogResult]::OK) {
    $script:Action = "LAUNCH"
} else {
    $script:Action = "CANCEL"
}

# Determine selected mode
$selectedMode = if ($rbPix.Checked) { "pix" } else { "renderdoc" }

# Write Desktop\dxgi_proxy.ini accordingly
$iniPath = [System.IO.Path]::Combine($DesktopDir, "dxgi_proxy.ini")
$iniContent = @"
[proxy]
mode=$selectedMode
rdoc_dll=$ToolDll

[Capture]
mode=$selectedMode
rdoc_dll=$ToolDll
"@
[System.IO.File]::WriteAllText($iniPath, $iniContent, [System.Text.Encoding]::UTF8)

# Write results to output file if provided
if (![string]::IsNullOrWhiteSpace($OutputFile)) {
    $outLines = @(
        "ACTION=$($script:Action)",
        "GAMEDIR=$($txtGame.Text)",
        "MODE=$selectedMode"
    )
    [System.IO.File]::WriteAllLines($OutputFile, $outLines, [System.Text.Encoding]::UTF8)
}

# Output results for batch to capture
Write-Output "ACTION=$($script:Action)"
Write-Output "GAMEDIR=$($txtGame.Text)"
Write-Output "MODE=$selectedMode"

if ($script:Action -eq "LAUNCH") {
    exit 0
} else {
    exit 1
}
