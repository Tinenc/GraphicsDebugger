# capture_wuwa.ps1
# Launch the Wuthering Waves RENDER process directly under TinecmaTool (RenderDoc)
# and pass the mandatory quality-tier argument that the "kuro" startup check needs.
#
# Why this exists:
#   Since game 3.7 the client assets are split into HD / SD / UHD packs and the
#   client MUST be told which tier to mount via -krqlv=<hd|sd|uhd>.
#   If that argument is missing (or invalid) the client aborts right after
#   mounting paks with:
#       Fatal error: [File:Unknown] [Line: 54] kuro: Use launcher to start game!
#   That message is NOT an anti-cheat / parent-process check, it is a missing
#   command-line parameter.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File capture_wuwa.ps1
#   powershell -ExecutionPolicy Bypass -File capture_wuwa.ps1 -Quality sd
#   powershell -ExecutionPolicy Bypass -File capture_wuwa.ps1 -ExtraArgs "-launcher_login"
#
# After the game window appears press F12 (or PrintScreen) to capture a frame.

param(
    [ValidateSet("hd", "sd", "uhd")][string]$Quality = "hd",
    [string]$Exe = "D:\Wuthering Waves\Wuthering Waves Game\Client\Binaries\Win64\Client-Win64-Shipping.exe",
    [string]$CmdExe = "C:\Program Files\GraphicsDebugger-Wuthering-Waves\x64\Development\TinecmaToolcmd.exe",
    [string]$OutDir = "$env:LOCALAPPDATA\Temp\RenderDoc",
    [string]$ExtraArgs = "",
    [switch]$HookChildren,
    [switch]$Callstacks
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $CmdExe)) {
    Write-Host "ERROR: injector not found: $CmdExe" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $Exe)) {
    Write-Host "ERROR: game client not found: $Exe" -ForegroundColor Red
    Write-Host "Pass the real path with -Exe <path>"
    exit 2
}
if (-not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
}

$workDir = Split-Path -Parent $Exe
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$capTemplate = Join-Path $OutDir ("wuwa_" + $stamp)

# mandatory tier argument first, then any user extras
$gameArgs = @("-krqlv=$Quality")
if ($ExtraArgs.Trim().Length -gt 0) {
    $gameArgs += ($ExtraArgs.Trim() -split "\s+")
}

# NOTE: do NOT insert "--" before $Exe - renderdoccmd's parser has no end-of-options
# marker and would treat "--" itself as the executable name.
$argList = @("capture", "-c", $capTemplate, "-d", $workDir)
if ($HookChildren) { $argList += "--opt-hook-children" }
if ($Callstacks)   { $argList += "--opt-capture-callstacks" }
$argList += $Exe
$argList += $gameArgs

Write-Host ""
Write-Host "Executable : $Exe"        -ForegroundColor Cyan
Write-Host "Arguments  : $($gameArgs -join ' ')"
Write-Host "WorkDir    : $workDir"
Write-Host "Captures   : ${capTemplate}_frameNNNN.rdc"
Write-Host ""
Write-Host "Launching..." -ForegroundColor DarkGray
Write-Host ("  `"" + $CmdExe + "`" " + ($argList -join " ")) -ForegroundColor DarkGray
Write-Host ""

# renderdoccmd returns the target-control ident as exit code on success, so the
# exit code is NOT a success flag - inspect the text instead.
$out = & $CmdExe @argList 2>&1 | ForEach-Object { $_.ToString() }
$out | Where-Object { $_ -notmatch "^\s*$" } | ForEach-Object { Write-Host "  $_" }

$joined = ($out -join "`n")
Write-Host ""
if ($joined -match "Failed to create" -or $joined -match "Failed to") {
    Write-Host "LAUNCH FAILED - see message above." -ForegroundColor Red
    exit 3
} elseif ($joined -match "Launched as ID") {
    Write-Host "LAUNCH OK - d3d12 delay-import hooks should now be installed in the client." -ForegroundColor Green
} else {
    Write-Host "LAUNCH state unclear - check the log under $OutDir" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. Wait for the game window (RenderDoc overlay should say 'Capturing')."
Write-Host "  2. Press F12 as soon as you see a rendered scene; the .rdc lands in $OutDir"
Write-Host "  3. If the client dies in a few seconds with no capture, the anti-cheat"
Write-Host "     killed it. Never launch the shell / launcher through RenderDoc and"
Write-Host "     never enable -HookChildren (it injects ACE-Setup64 too)."
