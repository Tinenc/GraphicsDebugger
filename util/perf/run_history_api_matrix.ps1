#Requires -Version 5.1
<# Runs the prepared History matrix sequentially. This USES THE GPU when invoked.
   A timeout stops the matrix without launching another replay process. #>
[CmdletBinding()]
param(
  [string]$Matrix,
  [string]$OutputDirectory,
  [string]$Configuration = 'Development',
  [Parameter(Mandatory=$true)][string]$BuildLabel,
  [int]$ProcessTimeoutSeconds = 180
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'history_matrix_acceptance.ps1')
$historyRepo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
if(-not $Matrix) { $Matrix = Join-Path $historyRepo 'output/eid-history-20260905/history-api-matrix.json' }
$historyMatrixPath = (Resolve-Path -LiteralPath $Matrix).Path
$historyMatrix = Get-Content -Raw -LiteralPath $historyMatrixPath | ConvertFrom-Json
if($historyMatrix.schema -ne 'renderdoc-history-api-matrix/v1' -or -not $historyMatrix.entries) {
  throw 'Unsupported or empty History API matrix'
}
if($ProcessTimeoutSeconds -lt 1) { throw 'ProcessTimeoutSeconds must be positive' }
if(-not $OutputDirectory) {
  $OutputDirectory = Join-Path (Split-Path -Parent $historyMatrixPath) ('matrix-run-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
}
if(Test-Path -LiteralPath $OutputDirectory) { throw 'Choose a new matrix output directory to preserve prior evidence' }
[void](New-Item -ItemType Directory -Path $OutputDirectory)
$historyOutput = (Resolve-Path -LiteralPath $OutputDirectory).Path
$historyShell = (Get-Process -Id $PID).Path
$historyRunner = Join-Path $PSScriptRoot 'run_bundled_replay_python.ps1'
$historyScript = Join-Path $PSScriptRoot 'eid_history_profile.py'
$historySummaryPath = Join-Path $historyOutput 'matrix-results.json'
$historySummary = @{schema='renderdoc-history-api-matrix-results/v1'; ok=$false;
  matrix=$historyMatrixPath; buildLabel=$BuildLabel; runs=@()}
$historySaved = @{}
try {
  foreach($historyEntry in $historyMatrix.entries) {
    $historyEnvironment = @{}
    foreach($property in $historyEntry.environment.PSObject.Properties) {
      $historyEnvironment[$property.Name] = [string]$property.Value
    }
    $historyReport = Join-Path $historyOutput ([string]$historyEntry.outputFile)
    $historyEnvironment['RENDERDOC_EID_HISTORY_OUTPUT'] = $historyReport
    $historyEnvironment['RENDERDOC_EID_HISTORY_BUILD_LABEL'] = $BuildLabel
    foreach($key in $historyEnvironment.Keys) {
      if(-not $historySaved.ContainsKey($key)) {
        $historySaved[$key] = [Environment]::GetEnvironmentVariable($key,'Process')
      }
      [Environment]::SetEnvironmentVariable($key,$historyEnvironment[$key],'Process')
    }
    $historyRun = @{api=$historyEntry.api; output=$historyReport; status='starting'}
    $historySummary.runs += $historyRun
    $historySummary | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $historySummaryPath -Encoding UTF8
    $historyArguments = @('-NoProfile','-File',('"' + $historyRunner + '"'),'-Script',
      ('"' + $historyScript + '"'),'-Configuration',$Configuration)
    $historyProcess = Start-Process -FilePath $historyShell -ArgumentList $historyArguments -WindowStyle Hidden -PassThru `
      -RedirectStandardOutput (Join-Path $historyOutput ($historyEntry.api + '.stdout.log')) `
      -RedirectStandardError (Join-Path $historyOutput ($historyEntry.api + '.stderr.log'))
    $historyRun.pid = $historyProcess.Id
    $historyRun.status = 'running'
    $historySummary | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $historySummaryPath -Encoding UTF8
    if(-not $historyProcess.WaitForExit($ProcessTimeoutSeconds * 1000)) {
      $historyRun.status = 'timeout-process-still-running'
      throw "Replay PID $($historyProcess.Id) exceeded the deadline. Inspect its phase report; no next API was started."
    }
    $historyRun.exitCode = $historyProcess.ExitCode
    if(-not (Test-Path -LiteralPath $historyReport)) { throw 'Replay process produced no report' }
    $historyResult = Get-Content -Raw -LiteralPath $historyReport | ConvertFrom-Json
    $historyVerdict = Test-HistoryMatrixResult -Result $historyResult -Entry $historyEntry `
      -Acceptance $historyMatrix.acceptance -ExitCode $historyProcess.ExitCode
    $historyRun.canonicalStatus = $historyResult.thumbnailValidation.status
    $historyRun.previewRecords = $historyVerdict.previewRecords
    $historyRun.acceptanceFailures = $historyVerdict.failures
    $historyRun.snapshotBackendKind = $historyVerdict.snapshotBackendKind
    $historyPass = $historyVerdict.passed
    $historyRun.status = if($historyPass) {'passed'} else {'failed'}
    if(-not $historyPass) { throw "History acceptance failed for $($historyEntry.api); inspect its report before continuing." }
  }
  $historySummary.ok = $true
} finally {
  $historySummary | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $historySummaryPath -Encoding UTF8
  foreach($key in $historySaved.Keys) {
    [Environment]::SetEnvironmentVariable($key,$historySaved[$key],'Process')
  }
}
Write-Output $historySummaryPath
