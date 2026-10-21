# inject_wuwa.ps1
# Inject TinecmaTool (RenderDoc) into a running Wuthering Waves render process,
# bypassing the qTinecmaTool "Allow process injection" UI setting entirely.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File inject_wuwa.ps1
#   powershell -ExecutionPolicy Bypass -File inject_wuwa.ps1 -Pid 12345
#   powershell -ExecutionPolicy Bypass -File inject_wuwa.ps1 -Match "Client-Win64-Shipping"
#
param(
    [Alias("Pid")][int]$TargetPid = 0,
    [string]$Match = "Client-Win64-Shipping",
    [string]$OutDir = "$env:LOCALAPPDATA\Temp\RenderDoc",
    [switch]$HookChildren
)

$ErrorActionPreference = "Stop"

$CmdExe = "C:\Program Files\GraphicsDebugger-Wuthering-Waves\x64\Development\TinecmaToolcmd.exe"
if (-not (Test-Path $CmdExe)) {
    Write-Host "ERROR: TinecmaToolcmd.exe not found at $CmdExe" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
    Write-Host "Created output dir: $OutDir"
}

# ---- 1. resolve target pid ----
if ($TargetPid -eq 0) {
    $procs = Get-Process | Where-Object { $_.ProcessName -like "*$Match*" }
    if (-not $procs) {
        Write-Host "ERROR: no running process matches '$Match'." -ForegroundColor Red
        Write-Host "Start the game through C:\Wuthering Waves\launcher.exe first,"
        Write-Host "get to the main lobby, then run this script again."
        exit 2
    }
    $procs | ForEach-Object {
        Write-Host ("  candidate: PID={0}  {1}  started={2}" -f $_.Id, $_.ProcessName, $_.StartTime)
    }
    if ($procs.Count -gt 1) {
        Write-Host "Multiple matches; using the first. Re-run with -Pid <n> to pick explicitly." -ForegroundColor Yellow
    }
    $TargetPid = $procs[0].Id
}

$proc = Get-Process -Id $TargetPid -ErrorAction SilentlyContinue
if (-not $proc) {
    Write-Host "ERROR: PID $TargetPid does not exist." -ForegroundColor Red
    exit 3
}

Write-Host ""
Write-Host ("Target : PID={0}  {1}" -f $proc.Id, $proc.ProcessName) -ForegroundColor Cyan
Write-Host ("OutDir : {0}" -f $OutDir)

# ---- 2. build args ----
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$capTemplate = Join-Path $OutDir ("wuwa_" + $stamp)
$debugLog = Join-Path $OutDir ("inject_" + $stamp + ".log")

$argList = @(
    "inject",
    "--PID=$TargetPid",
    "--capture-file=$capTemplate",
    "--debuglog=$debugLog"
)
if ($HookChildren) { $argList += "--opt-hook-children" }

Write-Host ""
Write-Host "Running:"
Write-Host ("  `"" + $CmdExe + "`" " + ($argList -join " ")) -ForegroundColor DarkGray
Write-Host ""

# ---- 3. inject ----
& $CmdExe @argList
$code = $LASTEXITCODE

Write-Host ""
if ($code -eq 0) {
    Write-Host "INJECT OK." -ForegroundColor Green
    Write-Host ""
    Write-Host "Now switch to the game window and press F12 to capture a frame."
    Write-Host ("Capture goes to: {0}_frameNNNN.rdc" -f $capTemplate)
    Write-Host ("Debug log      : {0}" -f $debugLog)
    Write-Host ""
    Write-Host "Note: capture keys only work while the game window has focus."
    Write-Host "If the process dies right after injecting, ACE killed it - check the log above."
} else {
    Write-Host ("INJECT FAILED, exit code {0}" -f $code) -ForegroundColor Red
    Write-Host ("See log: {0}" -f $debugLog)
    exit $code
}
