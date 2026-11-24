#Requires -Version 5.1
<# Build an isolated core, Python binding, CLI, and local offline Python host.
   IncludeGui additionally builds the local GUI and deploys its fixed bridge.
   No application is launched and no ReplayLab sidecar runtime is frozen. #>
[CmdletBinding()]
param([ValidatePattern('^[A-Za-z0-9_-]+$')][string]$Label = 'baseline',
      [switch]$AllowForeignGui,
      [switch]$IncludeGui)
$ErrorActionPreference = 'Stop'
$buildRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path.TrimEnd('\')
if(-not (Test-Path -LiteralPath (Join-Path $buildRoot 'WORKTREE_ISOLATION.md'))) {
  throw 'This builder requires an explicitly isolated worktree.'
}
function Assert-InBuildRoot([string]$Path) {
  $full = [IO.Path]::GetFullPath($Path)
  if(-not $full.StartsWith($buildRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "Build output escapes the isolated worktree: $full"
  }
  $ancestor = $full
  while($ancestor -and $ancestor.Length -ge $buildRoot.Length) {
    if(Test-Path -LiteralPath $ancestor) {
      if((Get-Item -Force -LiteralPath $ancestor).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Build path contains a reparse point: $ancestor"
      }
    }
    $ancestor = Split-Path -Parent $ancestor
  }
  return $full
}
$buildOutput = Assert-InBuildRoot (Join-Path $buildRoot 'output/eid-perf-20260908')
$buildBin = Assert-InBuildRoot (Join-Path $buildRoot 'x64/Development')
$buildTemp = Assert-InBuildRoot (Join-Path $buildOutput "temp-build-$Label")
foreach($directory in @($buildOutput,$buildBin,$buildTemp)) {
  [void](New-Item -ItemType Directory -Force -Path $directory)
}
$receiptPath = Join-Path $buildOutput "build-$Label-receipt.json"
if(Test-Path -LiteralPath $receiptPath) { throw "Receipt already exists; use a fresh label: $receiptPath" }
$buildGuardNames = @('MSBuild.exe','q_render_soco_doc.exe','qrenderdoc.exe',
  'render_soco_doccmd.exe','renderdoccmd.exe','eid_replay_python.exe')
$processSnapshot = @(Get-CimInstance Win32_Process)
$conflicts = @($processSnapshot | Where-Object { $_.Name -in $buildGuardNames })
$ignoredIdleWorkers = @()
$ignoredForeignGuis = @()
if($conflicts.Count) {
  # MSBuild leaves reusable worker processes after their build owner exits. Never
  # kill another owner's worker, and never treat an active worker as idle.
  Start-Sleep -Milliseconds 600
  $secondSnapshot = @(Get-CimInstance Win32_Process)
  $activeConflicts = @()
  foreach($candidate in $conflicts) {
    $current = $secondSnapshot | Where-Object { $_.ProcessId -eq $candidate.ProcessId } | Select-Object -First 1
    if(-not $current) { continue }
    $isWorker = $candidate.Name -eq 'MSBuild.exe' -and
      $candidate.CommandLine -match '(?i)/nodemode:\d+\b' -and
      $candidate.CommandLine -match '(?i)/nodeReuse:true\b'
    $identityStable = $candidate.CreationDate -eq $current.CreationDate -and
      $candidate.ExecutablePath -eq $current.ExecutablePath -and $candidate.CommandLine -eq $current.CommandLine
    if($AllowForeignGui -and $identityStable -and
       $candidate.Name -in @('q_render_soco_doc.exe','qrenderdoc.exe') -and
       $candidate.ExecutablePath -and
       -not $candidate.ExecutablePath.StartsWith($buildRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
      # Compilation touches only this worktree. A foreign GUI can remain open if neither its
      # executable nor any loaded DLL belongs to our output tree. This exception never applies
      # to other build workers or replay hosts. The replay runner separately records GPU sharing.
      $foreignModules = @()
      try { $foreignModules = @((Get-Process -Id $candidate.ProcessId -ErrorAction Stop).Modules) } catch {}
      $modulesInBuild = @($foreignModules | Where-Object {
        $_.FileName.StartsWith($buildRoot + '\', [StringComparison]::OrdinalIgnoreCase)
      })
      if($foreignModules.Count -gt 0 -and $modulesInBuild.Count -eq 0) {
        $ignoredForeignGuis += @{pid=$candidate.ProcessId;path=$candidate.ExecutablePath;
          created=$candidate.CreationDate.ToUniversalTime().ToString('o');
          loadedModulesInspected=$foreignModules.Count;modulesInBuildRoot=0;terminated=$false}
        Write-Output "Foreign GUI $($candidate.ProcessId) uses no isolated build modules; left running. Replay runs separately record GPU-sharing conditions."
        continue
      }
    }
    $parentPresent = @($processSnapshot + $secondSnapshot | Where-Object { $_.ProcessId -eq $candidate.ParentProcessId }).Count -gt 0
    $childrenPresent = @($processSnapshot + $secondSnapshot | Where-Object { $_.ParentProcessId -eq $candidate.ProcessId -and $_.Name -ne 'conhost.exe' }).Count -gt 0
    $compilerPresent = @($processSnapshot + $secondSnapshot | Where-Object { $_.Name -in @('cl.exe','link.exe','c1.exe','c1xx.exe','c2.exe') }).Count -gt 0
    $activityStable = $true
    foreach($counter in @('KernelModeTime','UserModeTime','ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')) {
      if($null -eq $candidate.$counter -or $null -eq $current.$counter -or $candidate.$counter -ne $current.$counter) { $activityStable = $false }
    }
    if($isWorker -and $identityStable -and -not $parentPresent -and -not $childrenPresent -and -not $compilerPresent -and $activityStable) {
      $ignoredIdleWorkers += @{pid=$candidate.ProcessId;parentPid=$candidate.ParentProcessId;path=$candidate.ExecutablePath;
        created=$candidate.CreationDate.ToUniversalTime().ToString('o');commandLine=$candidate.CommandLine;observationMs=600;
        cpuAndIoStable=$true;parentAbsent=$true;nonConsoleChildrenAbsent=$true;compilerAbsent=$true;terminated=$false}
      Write-Output "Ignoring proven idle orphan MSBuild worker $($candidate.ProcessId); process left untouched."
    } else {
      $activeConflicts += $current
    }
  }
  # A new build/replay process appearing during the observation also blocks this build.
  foreach($current in ($secondSnapshot | Where-Object { $_.Name -in $buildGuardNames })) {
    if(-not ($conflicts | Where-Object { $_.ProcessId -eq $current.ProcessId })) { $activeConflicts += $current }
  }
  if($activeConflicts.Count) {
    $blockedPath = Join-Path $buildOutput "build-$Label-blocked.json"
    $blocked = @($activeConflicts | ForEach-Object {
      @{pid=$_.ProcessId;name=$_.Name;path=$_.ExecutablePath;startedUtc=$_.CreationDate.ToUniversalTime().ToString('o')}
    })
    @{schema='eid-process-guard/v1';mode='build';allowed=$false;observedUtc=[DateTime]::UtcNow.ToString('o');
      worktree=$buildRoot;processes=$blocked;terminatedAnyProcess=$false} |
      ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $blockedPath -Encoding UTF8
    throw "Another active native build or replay process exists; coordinate its owner. Guard receipt: $blockedPath"
  }
}
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
$msbuild = & $vswhere -latest -requires Microsoft.Component.MSBuild -find 'MSBuild\**\Bin\MSBuild.exe' | Select-Object -First 1
$vsRoot = & $vswhere -latest -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath | Select-Object -First 1
if(-not $msbuild -or -not $vsRoot) { throw 'MSBuild / MSVC not available.' }
$buildSolution = $buildRoot + '\'
function Source-Identity {
  $paths = @(& git -C $buildRoot ls-files --cached --others --exclude-standard -- renderdoc renderdoccmd qrenderdoc/Code/pyrenderdoc util/WindowsSDKTarget.props util/WindowsSDKFix.props util/perf/eid_replay_python.cpp)
  if($LASTEXITCODE) { throw 'Cannot enumerate build source files.' }
  $paths += 'util/perf/eid_replay_python.cpp'
  if($IncludeGui) {
    $paths += @(& git -C $buildRoot ls-files --cached --others --exclude-standard -- qrenderdoc replaylab/renderdoc_mcp_bridge)
    if($LASTEXITCODE) { throw 'Cannot enumerate GUI/integration source files.' }
  }
  $result = @()
  foreach($relative in ($paths | Sort-Object -Unique)) {
    $path = Join-Path $buildRoot $relative
    if((Test-Path -LiteralPath $path -PathType Leaf) -and $relative -match '\.(cpp|cxx|c|h|hpp|hxx|inl|i|inc|hlsl|glsl|vert|frag|comp|txt|in|cmake|vcxproj|props|rc|def|xml|json|lib|exe|ui|qrc|ts|qm|py)$') {
      $result += @{path=$relative;sha256=(Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash}
    }
  }
  return $result
}
$receipt = [ordered]@{
  schema='eid-isolated-build/v1';label=$Label;worktree=$buildRoot;configuration='Development';platform='x64';
  replayOptimizeDevelopment=$true;msbuild=$msbuild;solutionDir=$buildSolution;maxCpu=2;
  ignoredIdleWorkers=$ignoredIdleWorkers;
  allowForeignGui=[bool]$AllowForeignGui;ignoredForeignGuis=$ignoredForeignGuis;
  startedUtc=[DateTime]::UtcNow.ToString('o');gitHead=(& git -C $buildRoot rev-parse HEAD);
  branch=(& git -C $buildRoot branch --show-current);sourceBefore=@(Source-Identity);steps=@();
  success=$false;gpuReplayRun=$false;guiRun=$false;guiBuilt=[bool]$IncludeGui;sidecarsFrozen=$false
}
$receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $buildOutput "build-$Label-inputs.json") -Encoding UTF8
$savedLocation = Get-Location
$savedTemp = $env:TEMP
$savedTmp = $env:TMP
try {
  Set-Location -LiteralPath $buildRoot
  $env:TEMP = $buildTemp
  $env:TMP = $buildTemp
  $steps = @(
    @{project='renderdoc/renderdoc.vcxproj';name='core'},
    @{project='renderdoc/renderdoc_no_export.vcxproj';name='system-load'},
    @{project='qrenderdoc/Code/pyrenderdoc/pyrenderdoc_module.vcxproj';name='pyd'},
    @{project='renderdoccmd/renderdoccmd.vcxproj';name='cmd'})
  if($IncludeGui) { $steps += @{project='qrenderdoc/qrenderdoc_local.vcxproj';name='gui'} }
  foreach($step in $steps) {
    $log = Join-Path $buildOutput "build-$Label-$($step.name).log"
    $started = [DateTime]::UtcNow
    Write-Output "Building $($step.name); log: $log"
    & $msbuild $step.project /m:2 /nr:false /t:Build /nologo /verbosity:minimal /p:Configuration=Development /p:Platform=x64 /p:ReplayOptimizeDevelopment=true /p:SkipReplayLabRuntime=true "/p:SolutionDir=$buildSolution" *> $log
    $code = $LASTEXITCODE
    $receipt.steps += @{name=$step.name;project=$step.project;log=$log;exitCode=$code;elapsedSeconds=([DateTime]::UtcNow-$started).TotalSeconds}
    if($code -ne 0) { throw "Native $($step.name) failed; inspect $log" }
  }
  foreach($pair in @(
    @('qrenderdoc/3rdparty/python/x64/python36.dll','python36.dll'),
    @('qrenderdoc/3rdparty/python/python36.zip','python36.zip'),
    @('qrenderdoc/3rdparty/python/x64/_ctypes.pyd','_ctypes.pyd'))) {
    Copy-Item -LiteralPath (Join-Path $buildRoot $pair[0]) -Destination (Assert-InBuildRoot (Join-Path $buildBin $pair[1])) -Force
  }
  $compiler = 'C:/Program Files (x86)/Windows Kits/10/Redist/D3D/x64/d3dcompiler_47.dll'
  if(-not (Test-Path -LiteralPath $compiler)) { throw 'Windows SDK d3dcompiler_47.dll missing.' }
  Copy-Item -LiteralPath $compiler -Destination (Assert-InBuildRoot (Join-Path $buildBin 'd3dcompiler_47.dll')) -Force
  $hostBatch = Join-Path $buildTemp 'build-python-host.cmd'
  $hostLog = Join-Path $buildOutput "build-$Label-python-host.log"
  @(
    '@echo off',
    ('call "{0}\VC\Auxiliary\Build\vcvars64.bat" >nul' -f $vsRoot),
    'if errorlevel 1 exit /b %errorlevel%',
    ('cl.exe /nologo /O2 /MT /EHsc /W4 /WX /Fe"{0}\eid_replay_python.exe" /Fo"{1}\eid_replay_python.obj" "{2}\util\perf\eid_replay_python.cpp"' -f $buildBin,$buildTemp,$buildRoot),
    'exit /b %errorlevel%'
  ) | Set-Content -LiteralPath $hostBatch -Encoding ASCII
  & $env:ComSpec /d /c $hostBatch *> $hostLog
  $code = $LASTEXITCODE
  $receipt.steps += @{name='python-host';log=$hostLog;exitCode=$code}
  if($code -ne 0) { throw "Local Python host failed; inspect $hostLog" }
  $receipt.sourceAfter = @(Source-Identity)
  $beforeText = $receipt.sourceBefore | ConvertTo-Json -Depth 4 -Compress
  $afterText = $receipt.sourceAfter | ConvertTo-Json -Depth 4 -Compress
  $receipt.sourceStable = ($beforeText -ceq $afterText)
  if(-not $receipt.sourceStable) { throw 'Build source changed during baseline compilation; rebuild using a fresh label.' }
  $receipt.binaries = @()
  $binaryPaths = @('renderdoc.dll','system_load.dll','pymodules/renderdoc.pyd','render_soco_doccmd.exe','eid_replay_python.exe','python36.dll','python36.zip','d3dcompiler_47.dll')
  if($IncludeGui) {
    $binaryPaths += @('q_render_soco_doc.exe','Qt5Core.dll','Qt5Gui.dll','Qt5Widgets.dll',
      'Qt5Network.dll','Qt5Svg.dll','shiboken2.dll','PySide2/pyside2.dll',
      'PySide2/QtCore.pyd','PySide2/QtGui.pyd','PySide2/QtWidgets.pyd','qtplugins/platforms/qwindows.dll')
  }
  foreach($relative in $binaryPaths) {
    $info = Get-Item -LiteralPath (Join-Path $buildBin $relative)
    $receipt.binaries += @{path=$info.FullName;bytes=$info.Length;modifiedUtc=$info.LastWriteTimeUtc.ToString('o');sha256=(Get-FileHash -LiteralPath $info.FullName -Algorithm SHA256).Hash}
  }
  $receipt.success = $true
  $configurationSource = Get-Content -LiteralPath (Join-Path $buildRoot 'renderdoc/os/os_specific.cpp') -Raw
  if($configurationSource.Contains('RENDERDOC_CONFIG_DIR')) {
    @{schema='eid-isolated-runtime/v1';buildReceipt=$receiptPath;worktree=$buildRoot;configurationOverride=$true;guiBuilt=[bool]$IncludeGui;binaries=$receipt.binaries} |
      ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $buildBin 'eid-isolated-runtime.json') -Encoding UTF8
  }
} catch {
  $receipt.error = $_.Exception.Message
  throw
} finally {
  $receipt.completedUtc = [DateTime]::UtcNow.ToString('o')
  $receipt | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $receiptPath -Encoding UTF8
  $env:TEMP = $savedTemp
  $env:TMP = $savedTmp
  Set-Location -LiteralPath $savedLocation.Path
}
Write-Output "Build receipt: $receiptPath"
