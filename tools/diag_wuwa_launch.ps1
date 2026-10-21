# diag_wuwa_launch.ps1
# Launch Wuthering Waves through TinecmaTool with the mandatory -krqlv tier argument,
# then watch the render process until it dies and report WHY it died:
#   - exit code (crash vs TerminateProcess)
#   - lifetime
#   - which modules (ours / ACE / CrashSight) appeared, and how long the process
#     survived after each one loaded.
#
# This is read-only diagnostics: it does not modify the game or the tool.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File diag_wuwa_launch.ps1
#   powershell -ExecutionPolicy Bypass -File diag_wuwa_launch.ps1 -Quality sd -TimeoutSec 120
#
#   Watch mode - do not launch anything, just measure a render process that somebody
#   else starts (qTinecmaTool Launch, the real launcher, a double-click, ...):
#   powershell -ExecutionPolicy Bypass -File diag_wuwa_launch.ps1 -WatchOnly

param(
    [ValidateSet("hd", "sd", "uhd")][string]$Quality = "hd",
    [string]$Exe = "D:\Wuthering Waves\Wuthering Waves Game\Client\Binaries\Win64\Client-Win64-Shipping.exe",
    [string]$CmdExe = "C:\Program Files\GraphicsDebugger-Wuthering-Waves\x64\Development\TinecmaToolcmd.exe",
    [string]$OutDir = "$env:LOCALAPPDATA\Temp\RenderDoc",
    [int]$TimeoutSec = 90,
    [switch]$WatchOnly,
    [int]$WatchWaitSec = 120,
    # Launch the 0.6 MB forwarder shell (Wuthering Waves.exe) instead of the client and let
    # child-process capture carry the DLL down into Client-Win64-Shipping.exe.
    [switch]$ShellMode,
    [string]$ShellExe = "D:\Wuthering Waves\Wuthering Waves Game\Wuthering Waves.exe"
)

$ErrorActionPreference = "Continue"

if (-not (Test-Path $Exe))    { Write-Host "ERROR: client not found: $Exe"  -ForegroundColor Red; exit 2 }
if (-not (Test-Path $CmdExe)) { Write-Host "ERROR: tool not found: $CmdExe" -ForegroundColor Red; exit 3 }
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }

$stamp    = Get-Date -Format "yyyyMMdd_HHmmss"
$capTpl   = Join-Path $OutDir ("wuwa_" + $stamp)
$diagLog  = Join-Path $OutDir ("diag_" + $stamp + ".txt")
$workDir  = Split-Path -Parent $Exe

function Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss.fff"), $msg
    Write-Host $line
    Add-Content -Path $diagLog -Value $line -Encoding UTF8
}

# ---- launch-chain tracker ----------------------------------------------------
# Logs every new game / launcher / anti-cheat process together with its parent and
# full command line, so we can see the REAL chain (launcher.exe -> launcher_main.exe
# -> Wuthering Waves.exe -> Client-Win64-Shipping.exe) and exactly which arguments
# the real launcher hands to the render client.
$script:seenProcs = @{}
$script:lastTree  = $null

function TrackGameChain() {
    $now = Get-Date
    if ($script:lastTree -and ($now - $script:lastTree).TotalMilliseconds -lt 700) { return }
    $script:lastTree = $now
    $list = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match "Wuthering|launcher|Client-Win64|^ACE|SGuard|KRLauncher|KRInstall" }
    foreach ($q in $list) {
        if (-not $script:seenProcs.ContainsKey($q.ProcessId)) {
            $script:seenProcs[$q.ProcessId] = $true
            $cmd = $q.CommandLine
            if (-not $cmd) { $cmd = "" }
            if ($cmd.Length -gt 320) { $cmd = $cmd.Substring(0, 320) + "..." }
            Log ("  chain: PID={0,-7} PPID={1,-7} {2}  cmd={3}" -f $q.ProcessId, $q.ParentProcessId, $q.Name, $cmd)
        }
    }
}

# seed: everything already running is noise, only report what appears from now on
foreach ($q in (Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) { $script:seenProcs[$q.ProcessId] = $true }


Log "client   : $Exe"
Log "args     : -krqlv=$Quality"
Log "captures : ${capTpl}_frameNNNN.rdc"
Log "diag log : $diagLog"

# kill stale instances so we do not measure the wrong process (never in watch mode)
if (-not $WatchOnly) {
    Get-Process -Name "Client-Win64-Shipping" -ErrorAction SilentlyContinue | ForEach-Object {
        Log ("stale client PID {0} found, terminating it first" -f $_.Id)
        try { $_.Kill() } catch { }
    }
    Start-Sleep -Milliseconds 500
}

$pid2 = 0

if (-not $WatchOnly) {
    # ---- launch ----
    if ($ShellMode) {
        $shellDir = Split-Path -Parent $ShellExe
        $argList = @("capture", "-c", $capTpl, "-d", $shellDir, "--opt-hook-children", $ShellExe, "-krqlv=$Quality")
    } else {
        $argList = @("capture", "-c", $capTpl, "-d", $workDir, $Exe, "-krqlv=$Quality")
    }
    Log ("launch   : `"$CmdExe`" " + ($argList -join " "))
    $launchOut = & $CmdExe @argList 2>&1 | ForEach-Object { $_.ToString() }
    foreach ($l in $launchOut) { if ($l.Trim().Length -gt 0) { Log ("launcher> " + $l.Trim()) } }

    # ---- find the render process ----
    $findSec = if ($ShellMode) { 60 } else { 30 }
    $deadline = (Get-Date).AddSeconds($findSec)
    while ((Get-Date) -lt $deadline) {
        TrackGameChain
        $p = Get-Process -Name "Client-Win64-Shipping" -ErrorAction SilentlyContinue | Sort-Object StartTime | Select-Object -Last 1
        if ($p) { $pid2 = $p.Id; break }
        Start-Sleep -Milliseconds 250
    }
    if ($pid2 -eq 0) {
        Log "RESULT: render process never appeared. The shell/launcher path was used, or the client aborted before creating the process."
        exit 4
    }
} else {
    # ---- watch mode: wait for a render process started by someone else ----
    Log "WATCH ONLY - start the game the way you want to test, I will measure it."
    Log "  (the real launch chain and every command line will be logged below)"
    $deadline = (Get-Date).AddSeconds($WatchWaitSec)
    while ((Get-Date) -lt $deadline) {
        TrackGameChain
        $p = Get-Process -Name "Client-Win64-Shipping" -ErrorAction SilentlyContinue | Sort-Object StartTime | Select-Object -Last 1
        if ($p) {
            # ignore instances that predate this script run
            if ($p.StartTime -gt (Get-Date).AddSeconds(-$WatchWaitSec)) { $pid2 = $p.Id; break }
        }
        Start-Sleep -Milliseconds 200
    }
    if ($pid2 -eq 0) {
        Log ("RESULT: no render process appeared within {0}s." -f $WatchWaitSec)
        exit 4
    }
}

$proc     = Get-Process -Id $pid2 -ErrorAction SilentlyContinue
$t0       = Get-Date
Log ("client started: PID {0}" -f $pid2)

# ---- watch it ----
$seen     = @{}
$exitCode = $null
$alive    = $true
while (((Get-Date) - $t0).TotalSeconds -lt $TimeoutSec) {
    TrackGameChain
    try { $proc.Refresh() } catch { }
    if ($proc.HasExited) { $alive = $false; break }

    $mods = @()
    try { $mods = $proc.Modules | ForEach-Object { $_.ModuleName } } catch { }

    foreach ($m in $mods) {
        if ($m -match "Tinecma|ACE|SGuard|kuro|CrashSight|anticheat" -and -not $seen.ContainsKey($m)) {
            $seen[$m] = ((Get-Date) - $t0).TotalSeconds
            Log ("  +{0,6:N2}s  module loaded: {1}" -f $seen[$m], $m)
        }
    }
    Start-Sleep -Milliseconds 300
}

if ($alive) {
    Log ("RESULT: still alive after {0}s - startup checks + injection survived. Press F12 in the game window to capture." -f $TimeoutSec)
    exit 0
}

try { $exitCode = $proc.ExitCode } catch { $exitCode = "unknown" }
$life = ((Get-Date) - $t0).TotalSeconds
Log ("RESULT: process exited after {0:N2}s, exit code {1}" -f $life, $(if ($exitCode -is [int]) { "0x{0:X8}" -f $exitCode } else { $exitCode }))
Log "loaded modules of interest before death:"
foreach ($k in $seen.Keys) { Log ("    {0,-32} at +{1:N2}s" -f $k, $seen[$k]) }
if ($seen.Count -eq 0) { Log "    (none - module enumeration was blocked, or nothing matching loaded)" }

Log ""
Log "how to read this:"
Log "  exit 0x00000000 / 0x00000001  -> the game exited by itself (kuro startup check), not a crash"
Log "  exit 0xC0000xxx              -> AV / unhandled exception inside the game"
Log "  exit 0xFFFFFFFF / 0x00000002 -> TerminateProcess, i.e. the anti-cheat killed it"
Log "  TinecmaTool.dll loaded then death a few seconds later -> module-level anti-cheat detection"
exit 0
