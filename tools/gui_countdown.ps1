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

# ---------------------------------------------------------------------------
# Form Setup
# ---------------------------------------------------------------------------
$form = New-Object System.Windows.Forms.Form
$form.Text = "TinecmaTool - 准备就绪 (按回车继续启动游戏)"
$form.Size = New-Object System.Drawing.Size(730, 650)
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
$lblTitle.Size = New-Object System.Drawing.Size(670, 30)
$lblTitle.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 13, [System.Drawing.FontStyle]::Bold)
$lblTitle.ForeColor = [System.Drawing.Color]::FromArgb(0, 215, 160)
$lblTitle.Text = "TinecmaTool (Stealth RenderDoc) - 启动配置向导"
$form.Controls.Add($lblTitle)

$lblSub = New-Object System.Windows.Forms.Label
$lblSub.Location = New-Object System.Drawing.Point(26, 48)
$lblSub.Size = New-Object System.Drawing.Size(670, 22)
$lblSub.ForeColor = [System.Drawing.Color]::FromArgb(180, 180, 180)
$lblSub.Text = "DirectX 12 商业游戏静默抓帧代理层已就绪，请核对启动配置与目标游戏："
$form.Controls.Add($lblSub)

# 2. GroupBox: 目标游戏选择与路径设置 (Target Game & Directory)
$grpGame = New-Object System.Windows.Forms.GroupBox
$grpGame.Location = New-Object System.Drawing.Point(24, 76)
$grpGame.Size = New-Object System.Drawing.Size(665, 150)
$grpGame.Text = " 选项 1：目标游戏选择与路径 (Target Game & Directory) "
$grpGame.ForeColor = [System.Drawing.Color]::FromArgb(210, 210, 210)
$grpGame.BackColor = [System.Drawing.Color]::FromArgb(40, 42, 48)

$rbGameCod = New-Object System.Windows.Forms.RadioButton
$rbGameCod.Location = New-Object System.Drawing.Point(16, 22)
$rbGameCod.Size = New-Object System.Drawing.Size(260, 24)
$rbGameCod.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5, [System.Drawing.FontStyle]::Bold)
$rbGameCod.ForeColor = [System.Drawing.Color]::FromArgb(255, 205, 90)
$rbGameCod.Text = "Call of Duty HQ (Steam: cod.exe)"
$grpGame.Controls.Add($rbGameCod)

$rbGameMW = New-Object System.Windows.Forms.RadioButton
$rbGameMW.Location = New-Object System.Drawing.Point(282, 22)
$rbGameMW.Size = New-Object System.Drawing.Size(265, 24)
$rbGameMW.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5, [System.Drawing.FontStyle]::Bold)
$rbGameMW.ForeColor = [System.Drawing.Color]::FromArgb(100, 215, 255)
$rbGameMW.Text = "现代战争 2019 (战网: MW2019)"
$grpGame.Controls.Add($rbGameMW)

$rbGameCustom = New-Object System.Windows.Forms.RadioButton
$rbGameCustom.Location = New-Object System.Drawing.Point(555, 22)
$rbGameCustom.Size = New-Object System.Drawing.Size(95, 24)
$rbGameCustom.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 9.5)
$rbGameCustom.ForeColor = [System.Drawing.Color]::FromArgb(190, 190, 190)
$rbGameCustom.Text = "自定义..."
$grpGame.Controls.Add($rbGameCustom)

$lblGame = New-Object System.Windows.Forms.Label
$lblGame.Location = New-Object System.Drawing.Point(16, 56)
$lblGame.Size = New-Object System.Drawing.Size(85, 22)
$lblGame.Text = "游戏根目录:"
$grpGame.Controls.Add($lblGame)

$txtGame = New-Object System.Windows.Forms.TextBox
$txtGame.Location = New-Object System.Drawing.Point(105, 54)
$txtGame.Size = New-Object System.Drawing.Size(455, 24)
$txtGame.BackColor = [System.Drawing.Color]::FromArgb(24, 25, 28)
$txtGame.ForeColor = [System.Drawing.Color]::White
$grpGame.Controls.Add($txtGame)

$btnBrowse = New-Object System.Windows.Forms.Button
$btnBrowse.Location = New-Object System.Drawing.Point(570, 52)
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
    if (Test-Path $txtGame.Text) { $dlg.SelectedPath = $txtGame.Text }
    if ($dlg.ShowDialog() -eq "OK") {
        $txtGame.Text = $dlg.SelectedPath
    }
})
$grpGame.Controls.Add($btnBrowse)

$lblGameTip = New-Object System.Windows.Forms.Label
$lblGameTip.Location = New-Object System.Drawing.Point(16, 88)
$lblGameTip.Size = New-Object System.Drawing.Size(635, 52)
$lblGameTip.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 8.8)
$lblGameTip.ForeColor = [System.Drawing.Color]::FromArgb(255, 205, 90)
$grpGame.Controls.Add($lblGameTip)

$form.Controls.Add($grpGame)

# 3. GroupBox: 抓帧后端选择 (Capture Backend)
$grpBackend = New-Object System.Windows.Forms.GroupBox
$grpBackend.Location = New-Object System.Drawing.Point(24, 236)
$grpBackend.Size = New-Object System.Drawing.Size(665, 142)
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
$lblRdocTip.Size = New-Object System.Drawing.Size(615, 30)
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
$lblPixTip.Size = New-Object System.Drawing.Size(615, 30)
$lblPixTip.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 8.8)
$lblPixTip.ForeColor = [System.Drawing.Color]::FromArgb(190, 190, 190)
$lblPixTip.Text = "提示：微软官方性能剖析首选。支持 GPU 时间线、GPU 时钟周期、绘制阶段耗时分析，生成 .wpix 帧文件。"
$grpBackend.Controls.Add($lblPixTip)

$form.Controls.Add($grpBackend)

# 4. GroupBox: 使用说明与防封提示 (说明信息区，非勾选选项)
$grpTips = New-Object System.Windows.Forms.GroupBox
$grpTips.Location = New-Object System.Drawing.Point(24, 388)
$grpTips.Size = New-Object System.Drawing.Size(665, 106)
$grpTips.Text = " 使用说明与防封提示 (抓帧说明，无需勾选) "
$grpTips.ForeColor = [System.Drawing.Color]::FromArgb(210, 210, 210)
$grpTips.BackColor = [System.Drawing.Color]::FromArgb(40, 42, 48)

$lblTip1 = New-Object System.Windows.Forms.Label
$lblTip1.Location = New-Object System.Drawing.Point(16, 24)
$lblTip1.Size = New-Object System.Drawing.Size(635, 20)
$lblTip1.ForeColor = [System.Drawing.Color]::FromArgb(230, 230, 230)
$lblTip1.Text = "[抓帧按键] 进入游戏后左上角呈现 HUD 浮层，随时按下 [F12] 键即可极速无感抓帧。"
$grpTips.Controls.Add($lblTip1)

$lblTip2 = New-Object System.Windows.Forms.Label
$lblTip2.Location = New-Object System.Drawing.Point(16, 48)
$lblTip2.Size = New-Object System.Drawing.Size(635, 20)
$lblTip2.ForeColor = [System.Drawing.Color]::FromArgb(230, 230, 230)
$lblTip2.Text = "[控制台触发] 在控制台输入 [C] 键，或向桌面写入 trigger_capture 文件亦可快速触发。"
$grpTips.Controls.Add($lblTip2)

$lblTip3 = New-Object System.Windows.Forms.Label
$lblTip3.Location = New-Object System.Drawing.Point(16, 74)
$lblTip3.Size = New-Object System.Drawing.Size(635, 24)
$lblTip3.ForeColor = [System.Drawing.Color]::FromArgb(100, 215, 255)
$lblTip3.Text = "[安全免杀保障] PEB 双向链表已物理隐匿，已屏蔽端口并隔离看门狗 0x00001337，反作弊零报警。"
$grpTips.Controls.Add($lblTip3)

$form.Controls.Add($grpTips)

# 5. Enter Key Instruction Banner
$lblEnterNotice = New-Object System.Windows.Forms.Label
$lblEnterNotice.Location = New-Object System.Drawing.Point(24, 502)
$lblEnterNotice.Size = New-Object System.Drawing.Size(665, 26)
$lblEnterNotice.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 10.5, [System.Drawing.FontStyle]::Bold)
$lblEnterNotice.ForeColor = [System.Drawing.Color]::FromArgb(255, 215, 80)
$lblEnterNotice.Text = "已取消时间限制：确认配置无误后，直接按下 [Enter 回车键] 即可启动游戏"
$form.Controls.Add($lblEnterNotice)

# 6. Action Buttons
$btnLaunch = New-Object System.Windows.Forms.Button
$btnLaunch.Location = New-Object System.Drawing.Point(24, 536)
$btnLaunch.Size = New-Object System.Drawing.Size(375, 46)
$btnLaunch.Text = "立即启动游戏 (按 Enter 继续)"
$btnLaunch.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 11, [System.Drawing.FontStyle]::Bold)
$btnLaunch.BackColor = [System.Drawing.Color]::FromArgb(0, 150, 100)
$btnLaunch.ForeColor = [System.Drawing.Color]::White
$btnLaunch.FlatStyle = "Flat"
$btnLaunch.FlatAppearance.BorderSize = 0
$btnLaunch.Cursor = [System.Windows.Forms.Cursors]::Hand

$btnCancel = New-Object System.Windows.Forms.Button
$btnCancel.Location = New-Object System.Drawing.Point(420, 536)
$btnCancel.Size = New-Object System.Drawing.Size(269, 46)
$btnCancel.Text = "取消退出 (Esc)"
$btnCancel.Font = New-Object System.Drawing.Font("Microsoft YaHei UI", 10.5)
$btnCancel.BackColor = [System.Drawing.Color]::FromArgb(70, 72, 80)
$btnCancel.ForeColor = [System.Drawing.Color]::White
$btnCancel.FlatStyle = "Flat"
$btnCancel.FlatAppearance.BorderSize = 0
$btnCancel.Cursor = [System.Windows.Forms.Cursors]::Hand

$form.Controls.Add($btnLaunch)
$form.Controls.Add($btnCancel)

# UI Update Events
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
            $isFound = (Test-Path $txtGame.Text) -and (Test-Path (Join-Path $txtGame.Text "ModernWarfare.exe"))
            if ($isFound) {
                $lblGameTip.ForeColor = [System.Drawing.Color]::FromArgb(100, 220, 160)
                $lblGameTip.Text = "状态：已检测到 ModernWarfare.exe！`n适配：Win7 遗留库避坑完成，双代理已部署。点击启动将直接拉起游戏（战网客户端亦可直接进入）。"
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
            $isFound = (Test-Path $txtGame.Text) -and (Test-Path (Join-Path $txtGame.Text "cod.exe"))
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
if ((Test-Path (Join-Path $GameDir "ModernWarfare.exe")) -or ($GameDir -match "Modern Warfare")) {
    $rbGameMW.Checked = $true
    $txtGame.Text = if (![string]::IsNullOrWhiteSpace($GameDir)) { $GameDir } else { $detectedMW2019 }
} elseif (Test-Path (Join-Path $GameDir "cod.exe")) {
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

# Dialog buttons
$btnLaunch.DialogResult = [System.Windows.Forms.DialogResult]::OK
$btnCancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel

$form.AcceptButton = $btnLaunch
$form.CancelButton = $btnCancel

$form.Add_Shown({
    $form.Activate()
    $form.BringToFront()
    $btnLaunch.Focus()
})

$script:Action = "LAUNCH"
$btnLaunch.Add_Click({ $script:Action = "LAUNCH" })
$btnCancel.Add_Click({ $script:Action = "CANCEL" })

$res = $form.ShowDialog()

if ($res -eq [System.Windows.Forms.DialogResult]::OK) {
    $script:Action = "LAUNCH"
} else {
    $script:Action = "CANCEL"
}

# Determine selected mode and game type
$selectedMode = if ($rbPix.Checked) { "pix" } else { "renderdoc" }
$targetGameType = if ($rbGameMW.Checked) { "MW2019" } elseif ($rbGameCod.Checked) { "COD_HQ" } else { "CUSTOM" }
$targetExe = if ($rbGameMW.Checked) {
    "ModernWarfare.exe"
} elseif ($rbGameCod.Checked) {
    "cod.exe"
} else {
    ""
}

# Write Desktop\dxgi_proxy.ini
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
        "GAME_TYPE=$targetGameType",
        "TARGET_EXE=$targetExe",
        "MODE=$selectedMode"
    )
    [System.IO.File]::WriteAllLines($OutputFile, $outLines, [System.Text.Encoding]::UTF8)
}

Write-Output "ACTION=$($script:Action)"
Write-Output "GAMEDIR=$($txtGame.Text)"
Write-Output "GAME_TYPE=$targetGameType"
Write-Output "TARGET_EXE=$targetExe"
Write-Output "MODE=$selectedMode"

if ($script:Action -eq "LAUNCH") {
    exit 0
} else {
    exit 1
}
