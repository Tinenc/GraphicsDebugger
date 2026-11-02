<#
.SYNOPSIS
    Wait for the Naraka renderer to appear, then inject the capture core into it
    as fast as possible.

.DESCRIPTION
    The whole point of this script is latency. Injecting into the renderer only
    helps if it happens before the game creates its D3D12 device - RenderDoc can
    only wrap a device it watched being created, and there is no second chance in
    that session.

    Measured on this machine: the renderer appears about 12s after StartGame and
    creates its device about 14s after that. A batch loop polling with `ping` and
    a fresh `tasklist` per tick took ~10s just to notice the process, which put
    the injection level with device creation. It lost the race by a second or
    two - hooks for sl.common.dll landed at +13s, the device at about +14s.

    So the wait happens here, in one process, polling Get-Process every 100ms
    with no per-tick process spawn, and the injector is started the moment the
    process shows up. Timings are printed so the next run can be judged on
    numbers instead of impressions.

.PARAMETER ProcessName
    Renderer executable name. Default NarakaBladepoint.exe.

.PARAMETER ToolCmd
    Path to TinecmaToolcmd.exe.

.PARAMETER CaptureDir
    Capture file name template directory.

.PARAMETER TimeoutSeconds
    Give up waiting after this long. Default 300.

.EXAMPLE
    .\wait-and-inject.ps1 -ToolCmd "C:\...\TinecmaToolcmd.exe" -CaptureDir "C:\...\captures"
#>

[CmdletBinding()]
param(
    [string]$ProcessName = 'NarakaBladepoint.exe',
    [Parameter(Mandatory = $true)][string]$ToolCmd,
    [Parameter(Mandatory = $true)][string]$CaptureDir,
    [int]$TimeoutSeconds = 300,
    [int]$PollMs = 100
)

$ErrorActionPreference = 'Continue'

$baseName = [IO.Path]::GetFileNameWithoutExtension($ProcessName)
$watch = [Diagnostics.Stopwatch]::StartNew()

Write-Host ("[wait] polling every {0}ms for {1} ..." -f $PollMs, $ProcessName)

$proc = $null
while ($watch.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
    $proc = Get-Process -Name $baseName -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($proc) { break }
    Start-Sleep -Milliseconds $PollMs
}

if (-not $proc) {
    Write-Host ("[fail] {0} never appeared within {1}s" -f $ProcessName, $TimeoutSeconds)
    exit 3
}

$detectedAt = $watch.Elapsed.TotalSeconds
$startedAt = $null
try { $startedAt = $proc.StartTime } catch { }
Write-Host ("[found] {0} pid={1}  detected +{2:N1}s after polling began" -f $ProcessName, $proc.Id, $detectedAt)

if (-not (Test-Path $CaptureDir)) { New-Item -ItemType Directory -Force -Path $CaptureDir | Out-Null }

# Straight to it: every millisecond here comes out of the window before the
# device exists.
$injectWatch = [Diagnostics.Stopwatch]::StartNew()
$out = & $ToolCmd inject --PID=$($proc.Id) -c (Join-Path $CaptureDir 'naraka') 2>&1
$injectWatch.Stop()
$rc = $LASTEXITCODE

foreach ($line in $out) { Write-Host ("  " + $line) }

Write-Host ("[inject] returned after {0:N1}s" -f $injectWatch.Elapsed.TotalSeconds)

if ($proc.HasExited) {
    Write-Host "[fail] the renderer died during the injection"
    exit 4
}

# TinecmaToolcmd's exit code is not a reliable signal in this fork - every
# command, including `version`, returns 0xC0000409. The real verdict comes from
# the core's own log, which WaitForWrappedDevice checks next.
Write-Host ("[note] tool exit code 0x{0:X8} is ignored - unreliable in this build" -f ($rc -band 0xFFFFFFFF))

exit 0
