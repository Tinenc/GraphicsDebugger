#Requires -Version 5.1
<#
.SYNOPSIS
    以 StartGame.exe 为注入目标抓取《永劫无间》，并在结束后自动判读钩子链日志。

.DESCRIPTION
    背景（2026-10-24 实测）：
      LauncherGame.exe 是引导壳，启动 tool\LootHoarder.exe 后立刻自行退出，所以
      「Launch Application -> LauncherGame.exe」必然在 1 秒内断开，注入链断在第一步。
      StartGame.exe 才是真正派生 NarakaBladepoint.exe 的那一环，且是 x64，
      直接以它为 --opt-hook-children 的起点，游戏会在创建时就被注入（设备创建之前）。

    本脚本只做三件事：预检 -> 启动捕获 -> 判读日志。不改动游戏文件、不停 ACE 服务、
    不伪造 tempdx12.txt。

.PARAMETER WorkDir
    游戏 program 目录，必须是 F:\Naraka\program（StartGame 用相对路径 ../../ 找资源）。

.PARAMETER CaptureDir
    .rdc 输出目录，默认 %LOCALAPPDATA%\Temp\Naraka。

.PARAMETER Target
    StartGame（默认）或 Game。后者直接注入 NarakaBladepoint.exe，仅用于对照实验，
    预期会因缺票据报「启动不合法」。

.EXAMPLE
    .\Invoke-NarakaCapture.ps1
    .\Invoke-NarakaCapture.ps1 -Target Game
#>
[CmdletBinding()]
param(
    [string]$RepoRoot   = "C:\Program Files\GraphicsDebugger-Naraka",
    [string]$WorkDir    = "F:\Naraka\program",
    [string]$CaptureDir = (Join-Path $env:LOCALAPPDATA "Temp\Naraka"),
    [ValidateSet("StartGame", "Game")]
    [string]$Target     = "StartGame",
    [switch]$SkipPreflight,
    [switch]$PreflightOnly
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host ""; Write-Host "== $msg ==" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "   [OK]   $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "   [WARN] $msg" -ForegroundColor Yellow }
function Write-Bad($msg)  { Write-Host "   [FAIL] $msg" -ForegroundColor Red }

# ---------------------------------------------------------------- 0. 提权检查
# TinecmaToolcmd 需要管理员才能打开目标进程并注入。非管理员时注入会失败，
# 而 RenderDoc 在注入失败后仍会 resume 目标进程 —— 结果是游戏裸跑一趟、
# 没有任何工具日志，看起来像"什么都没发生"。这里提前拦住。
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host ""
    Write-Host "本脚本必须以管理员身份运行。" -ForegroundColor Red
    Write-Host "  注入失败时目标进程仍会被拉起，但没有任何钩子，日志也不会生成 —— 只会浪费一次 ACE 启动。" -ForegroundColor Yellow
    Write-Host "  右键 PowerShell / Windows Terminal -> 以管理员身份运行，再执行本脚本。" -ForegroundColor Yellow
    exit 2
}

# ---------------------------------------------------------------- 1. 工具校验
Write-Step "工具与产物"

$cmd = Join-Path $RepoRoot "x64\Development\TinecmaToolcmd.exe"
$shim64 = Join-Path $RepoRoot "x64\Development\TinecmaToolshim64.dll"
$shim32 = Join-Path $RepoRoot "Win32\Development\TinecmaToolshim32.dll"

foreach ($f in @($cmd, $shim64, $shim32)) {
    if (-not (Test-Path $f)) { Write-Bad "缺失: $f"; throw "构建产物不完整，先编译 x64 与 x86 两个平台。" }
}
Write-Ok "TinecmaToolcmd.exe / shim64 / shim32 齐备"
Write-Ok ("构建时间: " + (Get-Item (Join-Path $RepoRoot "x64\Development\TinecmaTool.dll")).LastWriteTime)

# 新黑名单只在本次之后重编的二进制里生效，提示一下就够
$hookSrc = Join-Path $RepoRoot "renderdoc\os\win32\sys_win32_hooks.cpp"
if ((Test-Path $hookSrc) -and ((Get-Item $hookSrc).LastWriteTime -gt (Get-Item (Join-Path $RepoRoot "x64\Development\TinecmaTool.dll")).LastWriteTime)) {
    Write-Warn2 "sys_win32_hooks.cpp 比已编译的 DLL 新 —— 黑名单改动尚未生效，建议重编。"
}

# ---------------------------------------------------------------- 2. 预检
if (-not $SkipPreflight) {
    Write-Step "预检"

    $stale = Get-Process -Name "qTinecmaTool", "TinecmaToolcmd" -ErrorAction SilentlyContinue
    if ($stale) {
        foreach ($p in $stale) { Write-Warn2 ("残留工具进程: {0} (pid {1}) {2}" -f $p.ProcessName, $p.Id, $p.Path) }
        Write-Warn2 "请先关闭这些进程再抓帧（尤其是其它树里的实例）。"
    } else {
        Write-Ok "无残留工具进程"
    }

    # 全局钩子残留（AppInit_DLLs）。读不到就当干净，不阻断。
    try {
        $k = Get-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Windows" -ErrorAction Stop
        $ai = [string]$k.AppInit_DLLs
        $ld = $k.LoadAppInit_DLLs
        if ($ai.Trim() -ne "" -or $ld -ne 0) { Write-Warn2 "AppInit_DLLs 非空: '$ai' LoadAppInit_DLLs=$ld" }
        else { Write-Ok "AppInit_DLLs 干净（全局钩子未启用）" }
    } catch { Write-Warn2 "无法读取 AppInit_DLLs（忽略）" }

    $cc = Get-Process -Name "CCGameRecord", "CCGameRecordHWEncoder", "CCMini" -ErrorAction SilentlyContinue
    if ($cc) { Write-Warn2 "CC 录制在运行，会与我们的 DXGI hook 抢交换链，建议先关掉游戏内录制" }
    else { Write-Ok "CC 录制未运行" }

    # 渲染后端判断。注意 tempdx12.txt 实际位于 NarakaBladepoint_Data 下，
    # 因为 StartGame 用相对路径 ../../NarakaBladepoint_Data/tempdx12.txt。
    $tdx = Join-Path $WorkDir "NarakaBladepoint_Data\tempdx12.txt"
    $ev  = Join-Path $WorkDir "ExtraVars.unityargs"
    if (Test-Path $tdx) {
        $flagTxt = (Get-Content $tdx -Raw).Trim()
        if ($flagTxt -match "dx12") {
            Write-Ok ("tempdx12.txt -> D3D12 标记有效 ('{0}', {1})" -f $flagTxt, (Get-Item $tdx).LastWriteTime)
        } else {
            Write-Warn2 ("tempdx12.txt 存在但内容不含 dx12: '{0}'" -f $flagTxt)
        }
    } else {
        Write-Warn2 "tempdx12.txt 不存在 -> 预期 D3D11"
    }

    if (Test-Path $ev) {
        $args0 = (Get-Content $ev -Raw).Trim()
        Write-Host ("   ExtraVars.unityargs = '{0}' (mtime {1})" -f $args0, (Get-Item $ev).LastWriteTime)
        if ($args0 -match "d3d12") { Write-Ok "-force-d3d12 已配置，下次经 StartGame 启动应切到 D3D12" }
    } else {
        Write-Warn2 "ExtraVars.unityargs 不存在"
    }

    $gi = Join-Path $WorkDir "NarakaBladepoint_Data\StreamingAssets\GameInit"
    $tickets = @()
    if (Test-Path $gi) { $tickets = Get-ChildItem $gi -Force -ErrorAction SilentlyContinue }
    if ($tickets.Count -eq 0) {
        Write-Warn2 "GameInit 票据目录为空 —— StartGame 可能判「启动不合法」。"
        Write-Warn2 "建议先正常走一次启动器登录，票据生成后立刻跑本脚本。"
    } else {
        $t = $tickets | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        Write-Ok ("票据存在: {0} ({1} B, {2})" -f $t.Name, $t.Length, $t.LastWriteTime)
    }
}

# ---------------------------------------------------------------- 3. 启动捕获
if ($PreflightOnly) {
    Write-Step "仅预检模式"
    Write-Host "   未启动游戏。去掉 -PreflightOnly 才会真正抓帧。"
    return
}

Write-Step "启动捕获（目标: $Target）"

if (-not (Test-Path $CaptureDir)) { New-Item -ItemType Directory -Path $CaptureDir -Force | Out-Null }

switch ($Target) {
    "StartGame" { $exe = Join-Path $WorkDir "StartGame.exe" }
    "Game"      { $exe = Join-Path $WorkDir "NarakaBladepoint.exe" }
}
if (-not (Test-Path $exe)) { throw "目标不存在: $exe" }

$logDir = Join-Path $env:LOCALAPPDATA "Temp\TinecmaTool"
$before = @()
if (Test-Path $logDir) { $before = Get-ChildItem $logDir -Filter "RenderDoc_*.log" | Select-Object -ExpandProperty FullName }

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$capTemplate = Join-Path $CaptureDir ("naraka_$stamp")
$toolArgs = @(
    "capture",
    "-w",
    "--opt-hook-children",
    "--opt-disallow-fullscreen",
    "-c", $capTemplate,
    "-d", $WorkDir,
    $exe
)

Write-Host ("   {0} {1}" -f $cmd, ($toolArgs -join " ")) -ForegroundColor DarkGray
Write-Host "   现在去做："
Write-Host "     1) 进入游戏，窗口化（不要全屏独占）"
Write-Host "     2) 到主界面/局内后按 F12"
Write-Host "     3) 抓完退出游戏，让命令返回" -ForegroundColor Yellow

$toolOut = Join-Path $CaptureDir ("naraka_$stamp" + "_tool.log")
Write-Host "   工具输出会同时写入: $toolOut" -ForegroundColor DarkGray

& $cmd @toolArgs 2>&1 | Tee-Object -FilePath $toolOut
$toolExit = $LASTEXITCODE
Write-Host "`n   TinecmaToolcmd 退出码: $toolExit"
if ($toolExit -ne 0) {
    Write-Bad "工具以非 0 退出，注入大概率失败。先看上面输出与 $toolOut"
}

# ---------------------------------------------------------------- 4. 判读
Write-Step "日志判读"

$rdcs = @()
if (Test-Path $CaptureDir) {
    $rdcs = Get-ChildItem $CaptureDir -Filter "naraka_$stamp*.rdc" -ErrorAction SilentlyContinue
}
if ($rdcs.Count -gt 0) {
    foreach ($r in $rdcs) { Write-Ok ("抓到: {0} ({1:N1} MB)" -f $r.FullName, ($r.Length / 1MB)) }
} else {
    Write-Bad "没有生成 .rdc —— Present 未接通或从未按 F12。"
}

$after = @()
if (Test-Path $logDir) { $after = Get-ChildItem $logDir -Filter "RenderDoc_*.log" | Select-Object -ExpandProperty FullName }
$log = $after | Where-Object { $before -notcontains $_ } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $log) {
    Write-Bad "本次运行没有生成任何新的工具日志 —— 注入根本没有发生。"
    Write-Host "   目标进程可能已被拉起但未带钩子（RenderDoc 注入失败后仍会 resume）。" -ForegroundColor Yellow
    Write-Host "   先看工具输出：$toolOut" -ForegroundColor Yellow
    return
}

Write-Host "   新日志: $log"
$text = Get-Content $log -Raw

# 逐级判据。前一条不过就不必看后面。
$stages = @(
    @{ Name = "注入启动器";        Pattern = "Loading into .*StartGame\.exe" },
    @{ Name = "派生子进程被接管";  Pattern = "Loading into .*NarakaBladepoint\.exe" },
    @{ Name = "图形 hook 注册";    Pattern = "Registering (D3D1[12]|DXGI) hooks" },
    @{ Name = "设备进入包装";      Pattern = "created wrapped device\." },
    @{ Name = "Present 已接通";    Pattern = "Graphics first present:" }
)

$failedAt = $null
foreach ($s in $stages) {
    if ($text -match $s.Pattern) { Write-Ok $s.Name }
    else { Write-Bad ($s.Name + "  <= 未出现"); $failedAt = $s.Name; break }
}

Write-Step "实际渲染后端（Player.log）"
$plog = Join-Path $env:USERPROFILE "AppData\LocalLow\24Entertainment\Naraka\Player.log"
if (Test-Path $plog) {
    Write-Host ("   " + $plog + "  (mtime " + (Get-Item $plog).LastWriteTime + ")")
    $plines = Select-String -Path $plog -Pattern "Direct3D 12|Direct3D 11|Version:\s+Direct3D|D3D12 device|InitD3D11|InitD3D12" -ErrorAction SilentlyContinue
    if ($plines) {
        $plines | Select-Object -First 6 | ForEach-Object { Write-Host ("   " + $_.Line.Trim()) }
        if (($plines | Out-String) -match "Direct3D 12") { Write-Ok "本次会话跑在 D3D12" }
        elseif (($plines | Out-String) -match "Direct3D 11") { Write-Warn2 "本次会话仍跑在 D3D11 —— DX12 标记没生效" }
    } else {
        Write-Warn2 "Player.log 里没找到后端行，可能本次没进到设备创建阶段"
    }
} else {
    Write-Warn2 "找不到 Player.log"
}

Write-Step "关键行摘录"
$pats = "Loading into|Registering D3D1|Registering DXGI|resolved delay hook|native factory bridge|Graphics call entered|created wrapped device|first present|Injecting renderdoc|Failed to inject|Couldn't get first module|Application requested not to be hooked|启动不合法"
Select-String -Path $log -Pattern $pats | Select-Object -First 40 |
    ForEach-Object { Write-Host ("   " + $_.Line.Trim()) }

if ($null -eq $failedAt -and $rdcs.Count -gt 0) {
    Write-Step "结论"
    Write-Ok "钩子链完整，已生成抓帧。下一步用 MCP 分析：open_capture -> get_capture_status -> get_draw_calls"
} else {
    Write-Step "结论"
    if ($text -match "Couldn't get first module" -or $text -match "Failed to inject") {
        Write-Bad "注入被拒（ACE/Neac 或进程早退）。按合规边界记录 unsupported-protected，不要换姿势重试。"
    } elseif ($text -notmatch "Loading into .*NarakaBladepoint\.exe") {
        Write-Bad "子进程未被接管：确认 --opt-hook-children 生效、StartGame 确实派生了游戏进程。"
    } elseif ($text -notmatch "Graphics call entered") {
        Write-Bad "钩子装上了但从未被调用 —— VMProtect 在解包时重写了 UnityPlayer 的 IAT。这是结构性阻塞。"
    } else {
        Write-Bad ("止步于: " + $failedAt)
    }
}
