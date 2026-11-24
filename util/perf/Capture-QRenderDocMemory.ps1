# Snapshot q_render_soco_doc / ReplayLab memory with VMMap + ETW-friendly counters.
# Usage (qrenderdoc already open with capture). From any directory:
#   powershell -ExecutionPolicy Bypass -File D:\Study\GitHub\renderdocUltra\util\perf\Capture-QRenderDocMemory.ps1 -Label baseline
# Or from the repo root:
#   powershell -ExecutionPolicy Bypass -File util/perf/Capture-QRenderDocMemory.ps1 -Label baseline
# Optional:
#   -ProcessId 12345
#   -Label baseline|after-describe|after-organize
#   -EtwSeconds 15   (needs admin for kernel providers; skipped if not elevated)

param(
  [int]$ProcessId = 0,
  [string]$Label = "snapshot",
  [string]$OutputRoot = "",
  [int]$EtwSeconds = 0
)

$ErrorActionPreference = "Stop"
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path (Join-Path $ScriptRoot "..\..")).Path

function Find-Vmmap {
  $paths = @(
    "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Microsoft.Sysinternals.VMMap_Microsoft\*\vmmap.exe",
    "$env:ProgramFiles\Sysinternals\vmmap.exe",
    "${env:ProgramFiles(x86)}\Sysinternals\vmmap.exe",
    "C:\Sysinternals\vmmap.exe"
  )
  foreach ($pattern in $paths) {
    $hit = Get-Item $pattern -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($hit) { return $hit.FullName }
  }
  $cmd = Get-Command vmmap.exe -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  return $null
}

function Resolve-TargetPid([int]$requested) {
  if ($requested -gt 0) {
    $p = Get-Process -Id $requested -ErrorAction SilentlyContinue
    if (-not $p) { throw "PID $requested not found" }
    return $p
  }
  $candidates = Get-Process -ErrorAction SilentlyContinue |
    Where-Object { $_.ProcessName -match 'q_render_soco_doc|qrenderdoc' }
  if (-not $candidates) {
    throw "No q_render_soco_doc / qrenderdoc process. Open Development qrenderdoc with the capture first."
  }
  if (@($candidates).Count -gt 1) {
    $candidates | Format-Table Id, ProcessName, WorkingSet64 -AutoSize | Out-Host
    throw "Multiple UI processes; pass -ProcessId"
  }
  return $candidates[0]
}

$proc = Resolve-TargetPid $ProcessId
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
if (-not $OutputRoot) {
  $OutputRoot = Join-Path $env:LOCALAPPDATA "ReplayLab\memory-profiles"
}
$outDir = Join-Path $OutputRoot ("{0}-{1}-{2}" -f $stamp, $Label, $proc.Id)
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

# Windows PowerShell 5.1 cannot parse try/catch inside a hashtable literal.
$processPath = $null
try { $processPath = $proc.Path } catch { $processPath = $null }

$summary = [ordered]@{
  capturedAt = (Get-Date).ToString("o")
  label = $Label
  pid = $proc.Id
  processName = $proc.ProcessName
  path = $processPath
  workingSetBytes = $proc.WorkingSet64
  privateBytes = $proc.PrivateMemorySize64
  virtualBytes = $proc.VirtualMemorySize64
  pagedBytes = $proc.PagedMemorySize64
  peakWorkingSetBytes = $proc.PeakWorkingSet64
  handleCount = $proc.HandleCount
  threadCount = $proc.Threads.Count
}
$summaryPath = Join-Path $outDir "summary.json"
($summary | ConvertTo-Json -Depth 4) | Set-Content -Encoding UTF8 $summaryPath

# Performance counters (Private Bytes / Working Set)
try {
  $counters = @(
    ("\Process({0})\Private Bytes" -f $proc.ProcessName),
    ("\Process({0})\Working Set" -f $proc.ProcessName),
    ("\Process({0})\Virtual Bytes" -f $proc.ProcessName),
    ("\Process({0})\Pool Nonpaged Bytes" -f $proc.ProcessName),
    ("\Process({0})\Pool Paged Bytes" -f $proc.ProcessName)
  )
  $samples = Get-Counter -Counter $counters -ErrorAction SilentlyContinue
  if ($samples) {
    $samples.CounterSamples |
      Select-Object Path, CookedValue |
      ConvertTo-Json |
      Set-Content -Encoding UTF8 (Join-Path $outDir "perf-counters.json")
  }
} catch {
  $_ | Out-File (Join-Path $outDir "perf-counters.error.txt")
}

$vmmap = Find-Vmmap
$vmmapOut = Join-Path $outDir "vmmap.mmp"
if ($vmmap) {
  Write-Host "Running VMMap against PID $($proc.Id) ..."
  # -accepteula -p <pid> -c <output.mmp> dumps a full address-space snapshot
  & $vmmap -accepteula -p $proc.Id -c $vmmapOut
  if (Test-Path $vmmapOut) {
    Write-Host "VMMap dump: $vmmapOut"
  } else {
    # Some builds use .csv export via -s
    $csv = Join-Path $outDir "vmmap.csv"
    & $vmmap -accepteula -p $proc.Id -s $csv
    Write-Host "VMMap csv attempt: $csv exists=$(Test-Path $csv)"
  }
} else {
  "VMMap not installed. winget install Microsoft.Sysinternals.VMMap" |
    Set-Content (Join-Path $outDir "vmmap.missing.txt")
  Write-Host "WARN: VMMap not found; only summary/counters written."
}

# Optional short ETW heap/virtual-alloc sample (admin)
if ($EtwSeconds -gt 0) {
  $xperf = "C:\Program Files (x86)\Windows Kits\10\Windows Performance Toolkit\xperf.exe"
  if (Test-Path $xperf) {
    $etl = Join-Path $outDir "heap-sample.etl"
    try {
      & $xperf -on PROC_THREAD+LOADER+MEMINFO+VIRT_ALLOC -f $etl
      Start-Sleep -Seconds $EtwSeconds
      & $xperf -d $etl
      Write-Host "ETW sample: $etl"
    } catch {
      $_ | Out-File (Join-Path $outDir "etw.error.txt")
      Write-Host "ETW failed (often needs admin): $_"
    }
  }
}

Write-Host ""
Write-Host "Snapshot written to: $outDir"
Write-Host ("WS={0:N2} GiB  Private={1:N2} GiB" -f ($proc.WorkingSet64/1GB), ($proc.PrivateMemorySize64/1GB))
Write-Host $outDir
