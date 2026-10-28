param(
    [string]$GameDir = "",
    [string]$SteamDir = "",
    [string]$ToolDll = "",
    [string]$ProxyDll = "",
    [int]$TimeoutSeconds = 15
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

$form = New-Object System.Windows.Forms.Form
$form.Text = "TinecmaTool - 准备就绪 (15s 自动启动)"
$form.Size = New-Object System.Drawing.Size(640, 420)
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
$lblTitle.Location = New-Object System.Drawing.Point(24, 20)
$lblTitle.Size = New-Object System.Drawing.Size(580, 32)
$lblTitle.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 13, [System.Drawing.FontStyle]::Bold)
$lblTitle.ForeColor = [System.Drawing.Color]::FromArgb(0, 210, 160)
$lblTitle.Text = "🎮 TinecmaTool (Stealth RenderDoc) - 准备就绪"
$form.Controls.Add($lblTitle)

$lblSub = New-Object System.Windows.Forms.Label
$lblSub.Location = New-Object System.Drawing.Point(26, 52)
$lblSub.Size = New-Object System.Drawing.Size(580, 24)
$lblSub.ForeColor = [System.Drawing.Color]::FromArgb(180, 180, 180)
$lblSub.Text = "DirectX 12 商业游戏静默抓帧代理层已部署完毕。"
$form.Controls.Add($lblSub)

# 2. Info Group Box
$grpInfo = New-Object System.Windows.Forms.GroupBox
$grpInfo.Location = New-Object System.Drawing.Point(24, 82)
$grpInfo.Size = New-Object System.Drawing.Size(580, 150)
$grpInfo.Text = " 部署信息 "
$grpInfo.ForeColor = [System.Drawing.Color]::FromArgb(200, 200, 200)
$grpInfo.BackColor = [System.Drawing.Color]::FromArgb(40, 42, 48)

# Game Path Label & Text
$lblGame = New-Object System.Windows.Forms.Label
$lblGame.Location = New-Object System.Drawing.Point(16, 28)
$lblGame.Size = New-Object System.Drawing.Size(90, 22)
$lblGame.Text = "游戏路径:"
$grpInfo.Controls.Add($lblGame)

$txtGame = New-Object System.Windows.Forms.TextBox
$txtGame.Location = New-Object System.Drawing.Point(105, 26)
$txtGame.Size = New-Object System.Drawing.Size(370, 24)
$txtGame.BackColor = [System.Drawing.Color]::FromArgb(24, 25, 28)
$txtGame.ForeColor = [System.Drawing.Color]::White
$txtGame.Text = $GameDir
$grpInfo.Controls.Add($txtGame)

$btnBrowse = New-Object System.Windows.Forms.Button
$btnBrowse.Location = New-Object System.Drawing.Point(485, 24)
$btnBrowse.Size = New-Object System.Drawing.Size(80, 28)
$btnBrowse.Text = "浏览..."
$btnBrowse.BackColor = [System.Drawing.Color]::FromArgb(55, 58, 66)
$btnBrowse.ForeColor = [System.Drawing.Color]::White
$btnBrowse.FlatStyle = "Flat"
$btnBrowse.FlatAppearance.BorderSize = 0
$btnBrowse.Add_Click({
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    $dlg.Description = "选择 Call of Duty HQ 游戏安装目录"
    if (Test-Path $txtGame.Text) { $dlg.SelectedPath = $txtGame.Text }
    if ($dlg.ShowDialog() -eq "OK") {
        $txtGame.Text = $dlg.SelectedPath
    }
})
$grpInfo.Controls.Add($btnBrowse)

# Mode
$lblMode = New-Object System.Windows.Forms.Label
$lblMode.Location = New-Object System.Drawing.Point(16, 68)
$lblMode.Size = New-Object System.Drawing.Size(540, 22)
$lblMode.ForeColor = [System.Drawing.Color]::FromArgb(220, 220, 220)
$lblMode.Text = "捕获核心: TinecmaTool.dll (RenderDoc v1.44 同步直写 + SEH 容错)"
$grpInfo.Controls.Add($lblMode)

# Anti-cheat note
$lblAnti = New-Object System.Windows.Forms.Label
$lblAnti.Location = New-Object System.Drawing.Point(16, 104)
$lblAnti.Size = New-Object System.Drawing.Size(540, 26)
$lblAnti.ForeColor = [System.Drawing.Color]::FromArgb(100, 200, 255)
$lblAnti.Text = "防护状态: PEB 双向链表已隐匿，已隔离内核看门狗 0x00001337"
$grpInfo.Controls.Add($lblAnti)

$form.Controls.Add($grpInfo)

# 3. Countdown Display
$lblCountdown = New-Object System.Windows.Forms.Label
$lblCountdown.Location = New-Object System.Drawing.Point(24, 246)
$lblCountdown.Size = New-Object System.Drawing.Size(580, 28)
$lblCountdown.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 10.5, [System.Drawing.FontStyle]::Bold)
$lblCountdown.ForeColor = [System.Drawing.Color]::FromArgb(255, 200, 60)
$lblCountdown.Text = "⏳ 将在 $TimeoutSeconds 秒后自动启动游戏... (按回车直接启动)"
$form.Controls.Add($lblCountdown)

$progressBar = New-Object System.Windows.Forms.ProgressBar
$progressBar.Location = New-Object System.Drawing.Point(24, 278)
$progressBar.Size = New-Object System.Drawing.Size(580, 14)
$progressBar.Maximum = $TimeoutSeconds
$progressBar.Value = $TimeoutSeconds
$progressBar.Style = "Continuous"
$form.Controls.Add($progressBar)

# 4. Action Buttons
$btnLaunch = New-Object System.Windows.Forms.Button
$btnLaunch.Location = New-Object System.Drawing.Point(24, 312)
$btnLaunch.Size = New-Object System.Drawing.Size(260, 44)
$btnLaunch.Text = "🚀 立即启动游戏 (Enter)"
$btnLaunch.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 11, [System.Drawing.FontStyle]::Bold)
$btnLaunch.BackColor = [System.Drawing.Color]::FromArgb(0, 150, 100)
$btnLaunch.ForeColor = [System.Drawing.Color]::White
$btnLaunch.FlatStyle = "Flat"
$btnLaunch.FlatAppearance.BorderSize = 0
$btnLaunch.Cursor = [System.Windows.Forms.Cursors]::Hand

$btnCancel = New-Object System.Windows.Forms.Button
$btnCancel.Location = New-Object System.Drawing.Point(344, 312)
$btnCancel.Size = New-Object System.Drawing.Size(260, 44)
$btnCancel.Text = "❌ 取消启动 (Esc)"
$btnCancel.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 10.5)
$btnCancel.BackColor = [System.Drawing.Color]::FromArgb(70, 72, 80)
$btnCancel.ForeColor = [System.Drawing.Color]::White
$btnCancel.FlatStyle = "Flat"
$btnCancel.FlatAppearance.BorderSize = 0
$btnCancel.Cursor = [System.Windows.Forms.Cursors]::Hand

$form.Controls.Add($btnLaunch)
$form.Controls.Add($btnCancel)

# Set Enter and Esc shortcuts
$form.AcceptButton = $btnLaunch
$form.CancelButton = $btnCancel

# Script state
$script:Action = "LAUNCH"
$script:Remaining = $TimeoutSeconds

$btnLaunch.Add_Click({
    $timer.Stop()
    $script:Action = "LAUNCH"
    $form.DialogResult = [System.Windows.Forms.DialogResult]::OK
    $form.Close()
})

$btnCancel.Add_Click({
    $timer.Stop()
    $script:Action = "CANCEL"
    $form.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $form.Close()
})

# Timer logic
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 1000
$timer.Add_Tick({
    $script:Remaining--
    if ($script:Remaining -gt 0) {
        $lblCountdown.Text = "⏳ 将在 $($script:Remaining) 秒后自动启动游戏... (按回车直接启动)"
        $progressBar.Value = $script:Remaining
    } else {
        $timer.Stop()
        $script:Action = "LAUNCH"
        $form.DialogResult = [System.Windows.Forms.DialogResult]::OK
        $form.Close()
    }
})
$timer.Start()

# Display Dialog
$res = $form.ShowDialog()

# Output result for batch to capture
Write-Output "ACTION=$($script:Action)"
Write-Output "GAMEDIR=$($txtGame.Text)"

if ($script:Action -eq "LAUNCH") {
    exit 0
} else {
    exit 1
}
