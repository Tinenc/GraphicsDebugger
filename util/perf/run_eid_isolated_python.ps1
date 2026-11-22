#Requires -Version 5.1
<# Run a worktree-owned Python script using the locally built offline host.
   The requested script decides whether a capture/GPU is opened. Coordinate GPU use first. #>
[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][string]$Script,
  [string[]]$ScriptArguments = @(),
  [ValidatePattern('^[A-Za-z0-9_-]+$')][string]$RunLabel = 'offline',
  [switch]$RequireIdleReplay,
  [switch]$AllowForeignGui
)
$ErrorActionPreference = 'Stop'
$runRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path.TrimEnd('\')
if(-not (Test-Path -LiteralPath (Join-Path $runRoot 'WORKTREE_ISOLATION.md'))) { throw 'Not an isolated worktree.' }
function Assert-IsolatedPath([string]$Path) {
  $full = [IO.Path]::GetFullPath($Path)
  if(-not $full.StartsWith($runRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "Path escapes worktree: $full" }
  $ancestor = $full
  while($ancestor -and $ancestor.Length -ge $runRoot.Length) {
    if((Test-Path -LiteralPath $ancestor) -and ((Get-Item -Force -LiteralPath $ancestor).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
      throw "Runtime path contains a reparse point: $ancestor"
    }
    $ancestor = Split-Path -Parent $ancestor
  }
  return $full
}
$runBin = Join-Path $runRoot 'x64/Development'
$runScript = Assert-IsolatedPath (Resolve-Path -LiteralPath $Script).Path
if(-not $runScript.StartsWith($runRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Script must belong to this worktree.' }
foreach($relative in @('eid_replay_python.exe','python36.dll','python36.zip','pymodules/renderdoc.pyd','renderdoc.dll')) {
  if(-not (Test-Path -LiteralPath (Assert-IsolatedPath (Join-Path $runBin $relative)))) { throw "Missing local runtime: $relative" }
}
$identityPath = Assert-IsolatedPath (Join-Path $runBin 'eid-isolated-runtime.json')
if(-not (Test-Path -LiteralPath $identityPath)) { throw 'No isolated runtime build receipt. Build the configuration-directory override first.' }
$identity = Get-Content -LiteralPath $identityPath -Raw | ConvertFrom-Json
if(-not $identity.configurationOverride -or $identity.worktree -ne $runRoot) { throw 'Runtime receipt does not certify this worktree configuration override.' }
foreach($entry in $identity.binaries) {
  $path = Assert-IsolatedPath $entry.path
  if((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $entry.sha256) { throw "Binary differs from isolated build receipt: $path" }
}
$module = Get-Item -LiteralPath (Join-Path $runBin 'pymodules/renderdoc.pyd')
$newestHeader = Get-ChildItem -LiteralPath (Join-Path $runRoot 'renderdoc/api/replay') -Filter '*.h' -File | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
if($newestHeader -and $module.LastWriteTimeUtc -lt $newestHeader.LastWriteTimeUtc) { throw 'Python bindings predate the public API; rebuild the isolated core and binding.' }
$runTemp = Assert-IsolatedPath (Join-Path $runRoot "output/eid-perf-20260908/temp-$RunLabel")
$runConfig = Assert-IsolatedPath (Join-Path $runRoot "output/eid-perf-20260908/config-$RunLabel")
[void](New-Item -ItemType Directory -Force -Path $runTemp)
[void](New-Item -ItemType Directory -Force -Path $runConfig)
$saved = @{}
foreach($name in @('PATH','TEMP','TMP','PYTHONDONTWRITEBYTECODE','PYTHONNOUSERSITE','PYTHONPATH','PYTHONHOME','RENDERDOC_CONFIG_DIR')) { $saved[$name] = [Environment]::GetEnvironmentVariable($name,'Process') }
$savedLocation = Get-Location
try {
  Set-Location -LiteralPath $runRoot
  $env:PATH = $runBin + ';' + $env:PATH
  $env:TEMP = $runTemp
  $env:TMP = $runTemp
  $env:RENDERDOC_CONFIG_DIR = $runConfig
  $env:PYTHONDONTWRITEBYTECODE = '1'
  $env:PYTHONNOUSERSITE = '1'
  $env:PYTHONPATH = $null
  $env:PYTHONHOME = $null
  if($RequireIdleReplay -or $AllowForeignGui) {
    # A different checkout's GUI competes for the same GPU and its memory budget.
    # Check both upstream and fork names; never stop another owner's process.
    $replayNames = @('q_render_soco_doc.exe','qrenderdoc.exe','render_soco_doccmd.exe',
      'renderdoccmd.exe','eid_replay_python.exe')
    $otherReplay = @(Get-CimInstance Win32_Process | Where-Object {
      $_.ProcessId -ne $PID -and $_.Name -in $replayNames
    })
    $guardProcesses = @($otherReplay | ForEach-Object {
      @{pid=$_.ProcessId;name=$_.Name;path=$_.ExecutablePath;startedUtc=$_.CreationDate.ToUniversalTime().ToString('o')}
    })
    $permittedForeign = @()
    $blockingReplay = @()
    foreach($candidate in $otherReplay) {
      if($AllowForeignGui -and $candidate.Name -in @('q_render_soco_doc.exe','qrenderdoc.exe') -and
         $candidate.ExecutablePath -and
         -not $candidate.ExecutablePath.StartsWith($runRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        $foreignModules = @()
        try { $foreignModules = @((Get-Process -Id $candidate.ProcessId -ErrorAction Stop).Modules) } catch {}
        $sharedModules = @($foreignModules | Where-Object {
          $_.FileName.StartsWith($runRoot + '\', [StringComparison]::OrdinalIgnoreCase)
        })
        if($foreignModules.Count -gt 0 -and $sharedModules.Count -eq 0) {
          $permittedForeign += @{pid=$candidate.ProcessId;path=$candidate.ExecutablePath;
            loadedModulesInspected=$foreignModules.Count;modulesInRunRoot=0}
          continue
        }
      }
      $blockingReplay += $candidate
    }
    $guardPath = Assert-IsolatedPath (Join-Path $runRoot "output/eid-perf-20260908/replay-idle-$RunLabel.json")
    @{schema='eid-process-guard/v1';mode='replay';allowed=($blockingReplay.Count -eq 0);
      observedUtc=[DateTime]::UtcNow.ToString('o');worktree=$runRoot;processes=$guardProcesses;
      script=$runScript;scriptArguments=$ScriptArguments;allowForeignGui=[bool]$AllowForeignGui;
      permittedForeignGuis=$permittedForeign;gpuExclusiveAtStart=($otherReplay.Count -eq 0);
      exclusivePerformanceRunCertified=$false;terminatedAnyProcess=$false} |
      ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $guardPath -Encoding UTF8
    if($blockingReplay.Count) {
      throw "Another replay process exists; coordinate its owner before GPU replay. Guard receipt: $guardPath"
    }
    if($permittedForeign.Count) {
      Write-Output "Foreign GUI uses separate binaries and remains untouched. GPU correctness/copy-count checks may proceed; elapsed times do not certify exclusive-GPU performance. Guard receipt: $guardPath"
    }
  }
  & (Join-Path $runBin 'eid_replay_python.exe') -s -B $runScript @ScriptArguments
  if($LASTEXITCODE -ne 0) { throw "Offline script failed with exit code $LASTEXITCODE" }
} finally {
  foreach($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name,$saved[$name],'Process') }
  Set-Location -LiteralPath $savedLocation.Path
}
