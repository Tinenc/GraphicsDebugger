# ===========================================================================
#  TinecmaTool - Naraka: Bladepoint 控制中心
# ===========================================================================
#
#  照着 Steam-Cod 那份启动向导写的，但两条实测结论决定了它和 COD 不一样：
#
#  1) 绝不提权。从高完整性父进程拉起 StartGame.exe，NeacClient 会起来但
#     NarakaBladepoint.exe 永不出现。本脚本发现自己在提权环境时，会把启动交给
#     explorer.exe 退回中等完整性。
#  2) 默认走注入，不走代理。注入不往游戏目录放任何文件；代理路线技术可用但会被
#     反作弊识别（2026-11-02 实测：进游戏弹提示后游戏拆除图形设备退出）。
#
#  COD 那份 GUI 的教训也照搬了：不使用 emoji（微软雅黑会退化成空心方块，被误当成
#  复选框），信息区用 GroupBox + ● 明确标注"非可交互控件"，可选项一律用原生
#  RadioButton。
# ===========================================================================

[CmdletBinding()]
param(
    [string]$GameDir = 'F:\Naraka\program',
    # 构建完界面后立即退出，不进入消息循环。用来无人值守地确认脚本真的能跑起来，
    # 而不只是能通过语法解析。
    [switch]$SmokeTest
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- paths ----
$Root      = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$CoreDll   = Join-Path $Root 'x64\Development\TinecmaTool.dll'
$ToolCmd   = Join-Path $Root 'x64\Development\TinecmaToolcmd.exe'
$GuiViewer = Join-Path $Root 'x64\Development\qTinecmaTool.exe'
$InjectPs1 = Join-Path $PSScriptRoot 'wait-and-inject.ps1'
$DeployPs1 = Join-Path $PSScriptRoot 'Deploy-NarakaProxy.ps1'

$Starter = 'StartGame.exe'
$Target  = 'NarakaBladepoint.exe'

$Work     = Join-Path $env:LOCALAPPDATA 'Temp\Naraka\proxy'
$Captures = Join-Path $Work 'captures'
$Trigger  = Join-Path $Captures 'trigger_capture_narakabladepoint'

foreach ($d in @($Work, $Captures)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
}

# --------------------------------------------------------------- state ----
$script:Phase        = 'idle'
$script:Proc         = $null
$script:Injector     = $null
$script:CaptureStart = $null
$script:WaitTicks    = 0
$script:Deadline     = $null

# --------------------------------------------------------------- theme ----
$ColBack    = [Drawing.Color]::FromArgb(30, 30, 30)
$ColPanel   = [Drawing.Color]::FromArgb(37, 37, 38)
$ColText    = [Drawing.Color]::FromArgb(230, 230, 230)
$ColDim     = [Drawing.Color]::FromArgb(178, 178, 178)
$ColAccent  = [Drawing.Color]::FromArgb(86, 156, 214)
$ColWarn    = [Drawing.Color]::FromArgb(214, 157, 86)
$ColOk      = [Drawing.Color]::FromArgb(106, 190, 106)
$ColBad     = [Drawing.Color]::FromArgb(214, 106, 106)
$FontUi     = New-Object Drawing.Font('Microsoft YaHei UI', 9)
$FontMono   = New-Object Drawing.Font('Consolas', 8.5)

# ------------------------------------------------------------- helpers ----
function Test-Elevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $pr = New-Object Security.Principal.WindowsPrincipal($id)
    return $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-ProcByName([string]$name) {
    return @(Get-Process -Name ([IO.Path]::GetFileNameWithoutExtension($name)) -ErrorAction SilentlyContinue)
}

function Get-NewestCoreLog {
    $dir = Join-Path $env:LOCALAPPDATA 'Temp\TinecmaTool'
    if (-not (Test-Path $dir)) { return $null }
    $f = Get-ChildItem -Path $dir -Filter 'RenderDoc_app_*.log' -ErrorAction SilentlyContinue |
         Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($f) { return $f.FullName }
    return $null
}

function Write-Status([string]$text, [Drawing.Color]$color) {
    $stamp = (Get-Date).ToString('HH:mm:ss')
    $box = $script:StatusBox
    $box.SelectionStart = $box.TextLength
    $box.SelectionLength = 0
    $box.SelectionColor = $color
    $box.AppendText("[$stamp] $text`r`n")
    $box.SelectionStart = $box.TextLength
    $box.ScrollToCaret()
}

function Start-Game([string]$dir) {
    # 提权时经 explorer 退回中等完整性；否则直接起。
    if (Test-Elevated) {
        $cmd = Join-Path $Work 'naraka_launch.cmd'
        $lines = @(
            '@echo off',
            ('cd /d "' + $dir + '"'),
            ('start "" "' + (Join-Path $dir $Starter) + '"')
        )
        Set-Content -Path $cmd -Value $lines -Encoding ASCII
        Start-Process -FilePath (Join-Path $env:SystemRoot 'explorer.exe') -ArgumentList $cmd
        Write-Status '已通过 explorer 以中等完整性启动（本会话是提权的）' $ColDim
    } else {
        Start-Process -FilePath (Join-Path $dir $Starter) -WorkingDirectory $dir
        Write-Status '已启动 StartGame.exe（中等完整性）' $ColDim
    }
}

# Flat buttons on a dark panel: the stock disabled rendering greys the glyph out
# against the panel colour and it turns unreadable. So these stay enabled for their
# whole life and the click handlers do the gating, saying why nothing happened -
# a readable hint beats a button that looks broken.
function New-SecondaryButton([string]$text, [int]$x, [int]$y, [int]$w) {
    $b = New-Object Windows.Forms.Button
    $b.Text = $text
    $b.SetBounds($x, $y, $w, 36)
    $b.FlatStyle = 'Flat'
    $b.BackColor = [Drawing.Color]::FromArgb(52, 52, 56)
    $b.ForeColor = [Drawing.Color]::FromArgb(248, 248, 248)
    $b.FlatAppearance.BorderColor = [Drawing.Color]::FromArgb(96, 96, 100)
    $b.FlatAppearance.BorderSize = 1
    $b.FlatAppearance.MouseOverBackColor = [Drawing.Color]::FromArgb(68, 68, 74)
    $b.FlatAppearance.MouseDownBackColor = [Drawing.Color]::FromArgb(40, 40, 44)
    $b.UseVisualStyleBackColor = $false
    return $b
}

function Set-Running($running) {
    $script:BtnLaunch.Enabled = -not $running
    $script:GrpRoute.Enabled = -not $running
    $script:TxtPath.Enabled = -not $running
    $script:BtnBrowse.Enabled = -not $running
}

function Begin-Inject {
    Set-Running $true
    $script:Phase = 'wait-renderer'
    $script:WaitTicks = 0
    $script:Deadline = (Get-Date).AddMinutes(5)
    Write-Status '等待 NarakaBladepoint.exe 出现（约 12 秒），出现即注入' $ColAccent

    $args = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', ('"' + $InjectPs1 + '"'),
        '-ProcessName', $Target,
        '-ToolCmd', ('"' + $ToolCmd + '"'),
        '-CaptureDir', ('"' + $Captures + '"')
    )
    $script:Injector = Start-Process -FilePath 'powershell.exe' -ArgumentList $args -PassThru -WindowStyle Hidden
    $script:Phase = 'injecting'
}

function Begin-Capture {
    if (-not (Get-ProcByName $Target)) {
        Write-Status '渲染进程不在运行，无法抓帧' $ColBad
        return
    }
    if ($script:Phase -ne 'ready' -and $script:Phase -ne 'capturing') {
        Write-Status ('设备还没包装（当前阶段 ' + $script:Phase + '），等状态显示「就绪」再按抓帧') $ColWarn
        return
    }
    $script:CaptureStart = Get-Date
    New-Item -ItemType File -Force -Path $Trigger | Out-Null
    $script:Phase = 'capturing'
    Write-Status '已下发抓帧请求，等待 .rdc 落盘（大场景可能需要几十秒）' $ColAccent
}

function Complete-Success {
    $script:Phase = 'ready'
    $script:LblState.Text = '就绪 — 设备已包装，可以抓帧'
    $script:LblState.ForeColor = $ColOk
    Write-Status '设备已包装（created wrapped device），可以按 [抓帧] 或 F12 之外的文件触发' $ColOk
}

# ------------------------------------------------------------------ UI ----
$form = New-Object Windows.Forms.Form
$form.Text = 'TinecmaTool  —  Naraka: Bladepoint 抓帧控制中心'
$form.Size = New-Object Drawing.Size(700, 720)
$form.StartPosition = 'CenterScreen'
$form.FormBorderStyle = 'FixedSingle'
$form.MaximizeBox = $false
$form.BackColor = $ColBack
$form.ForeColor = $ColText
$form.Font = $FontUi
$form.TopMost = $true

$title = New-Object Windows.Forms.Label
$title.Text = 'TinecmaTool  /  Naraka: Bladepoint'
$title.Font = New-Object Drawing.Font('Microsoft YaHei UI', 14, [Drawing.FontStyle]::Bold)
$title.ForeColor = $ColText
$title.SetBounds(20, 14, 640, 30)
$form.Controls.Add($title)

$sub = New-Object Windows.Forms.Label
$sub.Text = '注入渲染进程抓帧 · 不修改游戏安装目录'
$sub.ForeColor = $ColDim
$sub.SetBounds(22, 44, 640, 20)
$form.Controls.Add($sub)

# --- 1. 游戏路径 -----------------------------------------------------------
$grpPath = New-Object Windows.Forms.GroupBox
$grpPath.Text = ' 1. 游戏路径 '
$grpPath.ForeColor = $ColAccent
$grpPath.SetBounds(18, 74, 646, 74)
$form.Controls.Add($grpPath)

$script:TxtPath = New-Object Windows.Forms.TextBox
$script:TxtPath.Text = $GameDir
$script:TxtPath.SetBounds(14, 30, 520, 24)
$script:TxtPath.BackColor = $ColPanel
$script:TxtPath.ForeColor = $ColText
$script:TxtPath.BorderStyle = 'FixedSingle'
$grpPath.Controls.Add($script:TxtPath)

$script:BtnBrowse = New-Object Windows.Forms.Button
$script:BtnBrowse.Text = '浏览...'
$script:BtnBrowse.SetBounds(544, 28, 86, 27)
$script:BtnBrowse.FlatStyle = 'Flat'
$script:BtnBrowse.BackColor = $ColPanel
$script:BtnBrowse.ForeColor = $ColText
$script:BtnBrowse.Add_Click({
    $dlg = New-Object Windows.Forms.FolderBrowserDialog
    $dlg.Description = '选择 Naraka 的 program 目录（里面有 StartGame.exe）'
    if ($dlg.ShowDialog() -eq 'OK') {
        $script:TxtPath.Text = $dlg.SelectedPath
        $script:GameDir = $dlg.SelectedPath
    }
})
$grpPath.Controls.Add($script:BtnBrowse)

# --- 2. 抓帧方式 -----------------------------------------------------------
$script:GrpRoute = New-Object Windows.Forms.GroupBox
$script:GrpRoute.Text = ' 2. 抓帧方式 '
$script:GrpRoute.ForeColor = $ColAccent
$script:GrpRoute.SetBounds(18, 158, 646, 96)
$form.Controls.Add($script:GrpRoute)

$script:RadInject = New-Object Windows.Forms.RadioButton
$script:RadInject.Text = '注入渲染进程（推荐）—  不往游戏目录写任何文件'
$script:RadInject.SetBounds(16, 26, 610, 22)
$script:RadInject.ForeColor = $ColText
$script:RadInject.Checked = $true
$script:GrpRoute.Controls.Add($script:RadInject)

$script:RadProxy = New-Object Windows.Forms.RadioButton
$script:RadProxy.Text = '代理 DLL（实验）—  会被反作弊识别，仅用于对比'
$script:RadProxy.SetBounds(16, 52, 610, 22)
$script:RadProxy.ForeColor = $ColWarn
$script:GrpRoute.Controls.Add($script:RadProxy)

# --- 3. 说明（非交互） -----------------------------------------------------
$grpInfo = New-Object Windows.Forms.GroupBox
$grpInfo.Text = ' 3. 使用说明（说明信息区，非可交互控件） '
$grpInfo.ForeColor = $ColDim
$grpInfo.SetBounds(18, 264, 646, 158)
$form.Controls.Add($grpInfo)

$infoText = @'
● 点 [启动并注入] 后脚本会启动 StartGame.exe，等 NarakaBladepoint.exe 出现就注入。
● 后台放行后进入游戏，等到真正开始渲染（不是加载画面）再抓帧。
● 抓帧方式：点 [抓帧] ，或手动建一个文件
    %LOCALAPPDATA%\Temp\Naraka\proxy\captures\trigger_capture_narakabladepoint
● 产物目录：%LOCALAPPDATA%\Temp\Naraka\proxy\captures
● 日志：%LOCALAPPDATA%\Temp\TinecmaTool\RenderDoc_app_*.log
● 注入不改安装目录；但反作弊对注入本身也是可见的，若出现提示立即退出游戏。
● 提权会话下游戏起不来（NeacClient 起、NarakaBladepoint 不出现），本工具会自动降权。
'@

$lblInfo = New-Object Windows.Forms.Label
$lblInfo.Text = $infoText
$lblInfo.ForeColor = $ColText
$lblInfo.SetBounds(14, 24, 618, 124)
$lblInfo.AutoSize = $false
$grpInfo.Controls.Add($lblInfo)

# --- 状态区 ----------------------------------------------------------------
$lblStateCaption = New-Object Windows.Forms.Label
$lblStateCaption.Text = '状态'
$lblStateCaption.ForeColor = $ColDim
$lblStateCaption.SetBounds(22, 432, 40, 20)
$form.Controls.Add($lblStateCaption)

$script:LblState = New-Object Windows.Forms.Label
$script:LblState.Text = '待机 — 还没启动'
$script:LblState.ForeColor = $ColDim
$script:LblState.SetBounds(66, 432, 600, 20)
$form.Controls.Add($script:LblState)

$script:StatusBox = New-Object Windows.Forms.RichTextBox
$script:StatusBox.SetBounds(18, 456, 646, 150)
$script:StatusBox.BackColor = [Drawing.Color]::FromArgb(22, 22, 22)
$script:StatusBox.ForeColor = $ColText
$script:StatusBox.Font = $FontMono
$script:StatusBox.BorderStyle = 'FixedSingle'
$script:StatusBox.ReadOnly = $true
$script:StatusBox.WordWrap = $false
$script:StatusBox.ScrollBars = 'Vertical'
$form.Controls.Add($script:StatusBox)

# --- 按钮 ------------------------------------------------------------------
$script:BtnLaunch = New-Object Windows.Forms.Button
$script:BtnLaunch.Text = '启动并注入  (Enter)'
$script:BtnLaunch.SetBounds(18, 620, 176, 36)
$script:BtnLaunch.FlatStyle = 'Flat'
$script:BtnLaunch.BackColor = [Drawing.Color]::FromArgb(45, 90, 140)
$script:BtnLaunch.ForeColor = [Drawing.Color]::White
$script:BtnLaunch.FlatAppearance.BorderColor = [Drawing.Color]::FromArgb(74, 133, 194)
$script:BtnLaunch.FlatAppearance.BorderSize = 1
$script:BtnLaunch.FlatAppearance.MouseOverBackColor = [Drawing.Color]::FromArgb(56, 106, 162)
$script:BtnLaunch.UseVisualStyleBackColor = $false
$form.Controls.Add($script:BtnLaunch)

$script:BtnCapture = New-SecondaryButton '抓帧' 202 620 120
$form.Controls.Add($script:BtnCapture)

$script:BtnOpen = New-SecondaryButton '产物目录' 330 620 110
$script:BtnOpen.Add_Click({
    if (-not (Test-Path $Captures)) { New-Item -ItemType Directory -Force -Path $Captures | Out-Null }
    Start-Process -FilePath $Captures
})
$form.Controls.Add($script:BtnOpen)

$script:BtnViewer = New-SecondaryButton '打开分析器' 448 620 110
$script:BtnViewer.Add_Click({
    $rdc = Get-ChildItem -Path $Captures -Filter '*.rdc' -ErrorAction SilentlyContinue |
           Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $rdc) { Write-Status '还没有 .rdc 产物' $ColWarn; return }
    if (Test-Path $GuiViewer) {
        Start-Process -FilePath $GuiViewer -ArgumentList ('"' + $rdc.FullName + '"')
    } else {
        Write-Status ('分析器不存在: ' + $GuiViewer) $ColBad
    }
})
$form.Controls.Add($script:BtnViewer)

$btnClose = New-SecondaryButton '关闭 (Esc)' 566 620 98
$form.Controls.Add($btnClose)

$form.AcceptButton = $script:BtnLaunch
$form.CancelButton = $btnClose

# --- 预检显示 --------------------------------------------------------------
foreach ($pair in @(
    @('核心 TinecmaTool.dll', $CoreDll),
    @('工具 TinecmaToolcmd.exe', $ToolCmd),
    @('注入脚本 wait-and-inject.ps1', $InjectPs1)
)) {
    if (Test-Path $pair[1]) {
        Write-Status ($pair[0] + '  OK') $ColDim
    } else {
        Write-Status ($pair[0] + '  缺失: ' + $pair[1]) $ColBad
    }
}
if (Test-Elevated) {
    Write-Status '本会话是提权的，启动游戏时将经 explorer 降权' $ColWarn
} else {
    Write-Status '中等完整性，正常启动即可' $ColDim
}
if (-not (Test-Path (Join-Path $GameDir $Starter))) {
    Write-Status ('找不到 ' + (Join-Path $GameDir $Starter) + '，请检查路径') $ColBad
}

# ------------------------------------------------------------- actions ----
$script:BtnLaunch.Add_Click({
    $game = $script:TxtPath.Text.Trim()
    if (-not (Test-Path (Join-Path $game $Starter))) {
        Write-Status ('路径无效，找不到 ' + $Starter) $ColBad
        return
    }
    $script:GameDir = $game

    if ($script:RadProxy.Checked) {
        $answer = [Windows.Forms.MessageBox]::Show(
            "代理路线实测结果：`n`n" +
            "游戏能起来、设备也能被包装，但安装目录里的三个 DLL 会被反作弊看到，" +
            "进游戏会弹提示，随后游戏拆除图形设备退出，没有产出 .rdc。`n`n" +
            "确定要继续吗？（会改动游戏安装目录）",
            '代理路线已被反作弊识别',
            [Windows.Forms.MessageBoxButtons]::YesNo,
            [Windows.Forms.MessageBoxIcon]::Warning)
        if ($answer -ne [Windows.Forms.DialogResult]::Yes) {
            Write-Status '已取消（代理路线）' $ColDim
            return
        }
        Write-Status '代理路线：先部署，再启动' $ColWarn
        $p = Start-Process -FilePath 'powershell.exe' -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ('"' + $DeployPs1 + '"'),
            '-ForceProtected', '-Apply', '-GameDir', ('"' + $game + '"')
        ) -PassThru -Wait -WindowStyle Hidden
        if ($p.ExitCode -ne 0) {
            Write-Status ('部署失败，退出码 ' + $p.ExitCode + '（权限不足或被策略拦住）') $ColBad
            return
        }
        Write-Status '代理已部署' $ColWarn
        Start-Game $game
        $script:Phase = 'wait-proxy-device'
        $script:Deadline = (Get-Date).AddMinutes(4)
        $script:LblState.Text = '等待代理层加载并把设备包装起来'
        $script:LblState.ForeColor = $ColAccent
        Set-Running $true
        return
    }

    if (Get-ProcByName $Target) {
        Write-Status '游戏已经在运行了。先关掉它，否则设备已经创建、注入来不及。' $ColWarn
        return
    }
    Start-Game $game
    Begin-Inject
})

$script:BtnCapture.Add_Click({ Begin-Capture })
$btnClose.Add_Click({ $form.Close() })

$form.Add_Shown({
    $form.Activate()
    $form.BringToFront()
    $script:BtnLaunch.Focus()
})

# ------------------------------------------------------------------ tick ---
$timer = New-Object Windows.Forms.Timer
$timer.Interval = 250
$timer.Add_Tick({
    try {
        switch ($script:Phase) {
            'injecting' {
                if ($script:Injector -and $script:Injector.HasExited) {
                    $script:Phase = 'watch-device'
                    $script:Deadline = (Get-Date).AddSeconds(120)
                    Write-Status '注入进程已返回，开始观察核心日志里的设备包装' $ColDim
                    $script:LblState.Text = '已注入，等待设备创建'
                }
            }
            'watch-device' {
                $log = Get-NewestCoreLog
                if ($log) {
                    $hit = Select-String -Path $log -Pattern 'created wrapped device' -SimpleMatch -ErrorAction SilentlyContinue
                    if ($hit) {
                        Complete-Success
                    }
                }
                if ((Get-Date) -gt $script:Deadline) {
                    $script:Phase = 'failed'
                    $script:LblState.Text = '超时 — 设备没有被包装'
                    $script:LblState.ForeColor = $ColBad
                    Write-Status '90 秒内没有看到 created wrapped device' $ColBad
                    Write-Status '常见原因：注入晚于设备创建，或导出重定向没装上（看日志 Export redirect）' $ColWarn
                    Set-Running $false
                }
            }
            'wait-proxy-device' {
                $log = Get-NewestCoreLog
                if ($log) {
                    $hit = Select-String -Path $log -Pattern 'created wrapped device' -SimpleMatch -ErrorAction SilentlyContinue
                    if ($hit) { Complete-Success }
                }
                if ((Get-Date) -gt $script:Deadline) {
                    $script:Phase = 'failed'
                    $script:LblState.Text = '超时 — 代理层没有包装设备'
                    $script:LblState.ForeColor = $ColBad
                    Set-Running $false
                }
            }
            'capturing' {
                $rdc = Get-ChildItem -Path $Captures -Filter '*.rdc' -ErrorAction SilentlyContinue |
                       Where-Object { $_.LastWriteTime -ge $script:CaptureStart } |
                       Sort-Object LastWriteTime -Descending | Select-Object -First 1
                if ($rdc) {
                    $script:Phase = 'ready'
                    $script:LblState.Text = ('抓帧完成: ' + $rdc.Name)
                    $script:LblState.ForeColor = $ColOk
                    Write-Status ('已保存 ' + $rdc.Name + '  (' + [math]::Round($rdc.Length / 1MB, 2) + ' MB)') $ColOk
                } elseif ((Get-Date) -gt $script:CaptureStart.AddSeconds(90)) {
                    $script:Phase = 'ready'
                    Write-Status '90 秒没有新 .rdc，抓帧可能没被接受（看核心日志）' $ColWarn
                }
            }
        }
    } catch {
        Write-Status ('tick 异常: ' + $_.Exception.Message) $ColBad
    }
})
$timer.Start()

if ($SmokeTest) {
    Write-Host ('SMOKE OK - controls built, phase=' + $script:Phase)
    $timer.Stop()
    $form.Dispose()
    exit 0
}

[void]$form.ShowDialog()
$timer.Stop()
