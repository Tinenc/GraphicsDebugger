#Requires -Version 5.1
<# Each phase uses the GPU; schedule it serially. This launcher does not build or start qrenderdoc.
   Capture uses an injected native demo; Discover derives EIDs from the resulting RDC; Profile
   uses only the successful discovery manifest and the existing full-pixel mutation validator. #>
[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)][ValidateSet('Capture','Discover','Profile')][string]$Phase,
  [ValidateSet('Checkpoint','Simple')][string]$Demo = 'Checkpoint',
  [string]$Configuration = 'Development',
  [string]$Demos,
  [string]$Capture,
  [string]$CaptureManifest,
  [string]$DiscoveryManifest,
  [string]$OutputDirectory,
  [string]$BuildLabel = 'unspecified'
)
$ErrorActionPreference = 'Stop'
$fixtureRepo = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
if(-not $OutputDirectory) {
  $OutputDirectory = Join-Path $fixtureRepo 'output/eid-checkpoints-20260905/d3d12-fixture'
}
$fixtureOutput = [IO.Path]::GetFullPath($OutputDirectory)
[void][IO.Directory]::CreateDirectory($fixtureOutput)
$fixtureRunner = Join-Path $PSScriptRoot 'run_bundled_replay_python.ps1'
$fixturePreviousTiming = $env:RENDERDOC_EID_TIMING
try {
  $env:RENDERDOC_EID_TIMING = '1'
  if($Phase -eq 'Capture') {
    if(-not $Demos) { $Demos = Join-Path $fixtureRepo 'util/test/demos_x64.exe' }
    $fixtureTest = if($Demo -eq 'Checkpoint') { 'D3D12_Checkpoint_Triangle' } else { 'D3D12_Simple_Triangle' }
    & $fixtureRunner -Configuration $Configuration -Script (Join-Path $PSScriptRoot 'capture_api_smoke.py') `
      -ScriptArguments @('--apis','D3D12','--d3d12-demo',$fixtureTest,'--demos',$Demos,
        '--output',(Join-Path $fixtureOutput 'captures'),'--timeout','120','--frame','5')
    return
  }
  if($Phase -eq 'Discover') {
    if($Capture -and $CaptureManifest) { throw 'Use Capture or CaptureManifest, not both.' }
    if($CaptureManifest) {
      $fixtureCaptured = Get-Content -Raw -LiteralPath $CaptureManifest | ConvertFrom-Json
      $fixtureEntries = @($fixtureCaptured.captures | Where-Object { $_.api -eq 'D3D12' })
      if(-not $fixtureCaptured.ok -or $fixtureEntries.Count -ne 1 -or
         -not $fixtureEntries[0].ok -or $fixtureEntries[0].driver -ne 'D3D12') {
        throw 'Capture manifest must contain exactly one successful native D3D12 capture.'
      }
      $Capture = $fixtureEntries[0].capture
    }
    if(-not $Capture) { throw 'Discover requires the actual Capture or CaptureManifest path.' }
    if(-not $DiscoveryManifest) { $DiscoveryManifest = Join-Path $fixtureOutput 'discovery.json' }
    $fixtureArguments = @('--capture',$Capture,'--output',$DiscoveryManifest)
    if($Demo -eq 'Simple') { $fixtureArguments += '--allow-single-submit' }
    & $fixtureRunner -Configuration $Configuration -Script (Join-Path $PSScriptRoot 'discover_d3d12_checkpoint_fixture.py') `
      -ScriptArguments $fixtureArguments
    return
  }
  if(-not $DiscoveryManifest) { $DiscoveryManifest = Join-Path $fixtureOutput 'discovery.json' }
  $fixtureEvidence = Get-Content -Raw -LiteralPath $DiscoveryManifest | ConvertFrom-Json
  if($fixtureEvidence.schema -ne 'renderdoc-d3d12-checkpoint-fixture/v1' -or
     -not $fixtureEvidence.ok -or -not $fixtureEvidence.readyForCheckpointMutation -or
     $fixtureEvidence.captureDriver -ne 'D3D12') {
    throw 'Profile requires a successful multi-submit D3D12 discovery; Simple_Triangle is not accepted as reuse evidence.'
  }
  # Use the framework directly: a PS 5.1 child can inherit a PS 7 module search path where
  # Get-FileHash is unavailable, even though native replay and file access work normally.
  $fixtureDigest = [Security.Cryptography.SHA256]::Create()
  $fixtureStream = [IO.File]::OpenRead($fixtureEvidence.capture)
  try {
    $fixtureHash = [BitConverter]::ToString($fixtureDigest.ComputeHash($fixtureStream)).Replace('-', '')
  } finally {
    $fixtureStream.Dispose()
    $fixtureDigest.Dispose()
  }
  if($fixtureHash -ne $fixtureEvidence.captureSha256) { throw 'Capture changed after discovery.' }
  $fixtureEvents = @($fixtureEvidence.selectedEvents)
  if($fixtureEvents.Count -lt 2 -or $fixtureEvents.Count -gt 8 -or
     @($fixtureEvents | Where-Object { $_ -isnot [ValueType] -or $_ -le 0 }).Count) {
    throw 'Discovery EIDs must contain two to eight actual positive integer events.'
  }
  $fixtureProfile = Join-Path $fixtureOutput 'checkpoint-profile.json'
  if(Test-Path -LiteralPath $fixtureProfile) { throw 'Choose a new OutputDirectory to preserve existing profiling evidence.' }
  & $fixtureRunner -Configuration $Configuration -Script (Join-Path $PSScriptRoot 'eid_checkpoint_profile.py') `
    -ScriptArguments @('--capture',$fixtureEvidence.capture,'--events',($fixtureEvents -join ','),
      '--output',$fixtureProfile,'--max-seconds','300','--min-hits','1','--validate-mutations','--build-label',$BuildLabel)
} finally {
  $env:RENDERDOC_EID_TIMING = $fixturePreviousTiming
}
