# CPU-only checks. Does not launch the matrix or load RenderDoc.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'history_matrix_acceptance.ps1')
function New-HistoryAcceptanceFixture {
  $preview = [pscustomobject]@{api='HistoryPreview'; eid=11; headless=1; snapshotroute=0;
    currentstate=1; replay_ms=0; restore_ms=0}
  $samples = @([pscustomobject]@{phase='history.headlessDisplayAtEvent'; status='complete';
    backendReplayCalls=0; timingRecords=@($preview)})
  foreach($phase in @('history.resolvePersistentSnapshot','history.snapshotDisplay',
                      'history.snapshotRGBReadback','history.resolveSameSnapshotAgain')) {
    $samples += [pscustomobject]@{phase=$phase; status='complete'; backendReplayCalls=0; timingRecords=@()}
  }
  return [pscustomobject]@{
    entry=[pscustomobject]@{captureDriver='D3D11'; anchor=11; historyEvent=11}
    acceptance=[pscustomobject]@{requireCurrentState=$true; requireSnapshotResolved=$true;
      requireSnapshotStagesNoReplay=$true; requireSnapshotIdentityReuse=$true;
      requireSnapshotRGBMatch=$true; requireNativeSnapshot=$false}
    result=[pscustomobject]@{ok=$true; measurementComplete=$true; captureDriver='D3D11'; samples=$samples;
      texture=[pscustomobject]@{resource='ResourceId::71'};
      thumbnailValidation=[pscustomobject]@{status='passed'; canonicalRepeatIdentical=$true;
        historyMatchesCanonical=$true; restoredControllerEvent=11};
      history=@([pscustomobject]@{snapshotResolved=$true; snapshotResource='ResourceId::1000000000000000090';
        snapshotBackendKind='native-or-portable; public API does not expose the branch';
        sameSnapshotNoReplay=$true; twoPathsRGBIdentical=$true;
        transientRGB=[pscustomobject]@{bytes=3;sha256='same'};
        snapshotRGB=[pscustomobject]@{bytes=3;sha256='same'}})}
  }
}
function Assert-HistoryAcceptance($Fixture, [bool]$Expected, [string]$Name) {
  $verdict = Test-HistoryMatrixResult -Result $Fixture.result -Entry $Fixture.entry `
    -Acceptance $Fixture.acceptance -ExitCode 0
  if($verdict.passed -ne $Expected) { throw "$Name failed: $($verdict.failures -join '; ')" }
}
$fixture = New-HistoryAcceptanceFixture
Assert-HistoryAcceptance $fixture $true 'Current-state valid evidence'
$fixture.result.samples[0].timingRecords[0].currentstate = 0
Assert-HistoryAcceptance $fixture $false 'Wrong branch'
$fixture = New-HistoryAcceptanceFixture
$fixture.result.samples[0].timingRecords[0].restore_ms = 0.001
Assert-HistoryAcceptance $fixture $false 'Extra restore'
$fixture = New-HistoryAcceptanceFixture
$fixture.result.samples[0].backendReplayCalls = 1
Assert-HistoryAcceptance $fixture $false 'Hidden display replay'
$fixture = New-HistoryAcceptanceFixture
$fixture.result.samples[1].backendReplayCalls = 1
Assert-HistoryAcceptance $fixture $false 'Cold snapshot replay'
$fixture = New-HistoryAcceptanceFixture
$fixture.result.history[0].snapshotRGB.sha256 = 'different'
Assert-HistoryAcceptance $fixture $false 'Snapshot RGB differs despite summary flag'
$fixture = New-HistoryAcceptanceFixture
$fixture.result.history[0].sameSnapshotNoReplay = $false
Assert-HistoryAcceptance $fixture $false 'Snapshot identity not reused'
$fixture = New-HistoryAcceptanceFixture
$fixture.result.history[0].snapshotResource = $fixture.result.texture.resource
Assert-HistoryAcceptance $fixture $false 'Snapshot aliases the live resource'
$fixture = New-HistoryAcceptanceFixture
$fixture.acceptance.requireNativeSnapshot = $true
Assert-HistoryAcceptance $fixture $false 'Unknown native path is not proof'
$fixture = New-HistoryAcceptanceFixture
$fixture.result.samples = @($fixture.result.samples[0])
$fixture.result.samples[0].timingRecords[0].currentstate = 0
$fixture.result.samples[0].timingRecords[0].replay_ms = 5
$fixture.result.samples[0].backendReplayCalls = 2
$fixture.acceptance = [pscustomobject]@{}
Assert-HistoryAcceptance $fixture $true 'Original matrix keeps its original acceptance'
Write-Output '10 CPU-only History matrix acceptance fixtures passed; no RenderDoc loaded'
