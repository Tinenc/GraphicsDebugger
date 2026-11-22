# CPU-only acceptance checks, shared by the matrix runner and its fixtures.
function Test-HistoryMatrixResult {
  param($Result, $Entry, $Acceptance, [int]$ExitCode)
  $failures = [Collections.Generic.List[string]]::new()
  $previews = @($Result.samples | Where-Object phase -eq 'history.headlessDisplayAtEvent' |
    ForEach-Object { $_.timingRecords } | Where-Object api -eq 'HistoryPreview')
  $histories = @($Result.history)
  if($ExitCode -ne 0 -or -not $Result.ok -or -not $Result.measurementComplete) {
    $failures.Add('Measurement did not complete successfully')
  }
  if($Result.captureDriver -ne $Entry.captureDriver -or $histories.Count -ne 1) {
    $failures.Add('Expected driver and exactly one History sample')
  }
  if($Result.thumbnailValidation.status -ne 'passed' -or
     -not $Result.thumbnailValidation.canonicalRepeatIdentical -or
     -not $Result.thumbnailValidation.historyMatchesCanonical -or
     $Result.thumbnailValidation.restoredControllerEvent -ne $Entry.anchor) {
    $failures.Add('Canonical image control or anchor restoration failed')
  }
  if($previews.Count -ne 1 -or $previews[0].headless -ne 1 -or $previews[0].snapshotroute -ne 0) {
    $failures.Add('Expected exactly one headless HistoryPreview without a snapshot shortcut')
  }
  if($Acceptance.requireCurrentState) {
    if($Entry.anchor -ne $Entry.historyEvent -or $previews.Count -ne 1 -or
       $previews[0].currentstate -ne 1 -or $previews[0].replay_ms -ne 0 -or
       $previews[0].restore_ms -ne 0) {
      $failures.Add('Current-state preview replayed, restored, or did not use the current-state branch')
    }
    $display = @($Result.samples | Where-Object phase -eq 'history.headlessDisplayAtEvent')
    if($display.Count -ne 1 -or $display[0].status -ne 'complete' -or $display[0].backendReplayCalls -ne 0) {
      $failures.Add('Current-state display contains a backend replay or incomplete evidence')
    }
  }
  if($Acceptance.requireSnapshotResolved) {
    if($histories.Count -ne 1 -or -not $histories[0].snapshotResolved -or
       -not $histories[0].snapshotResource -or $histories[0].snapshotResource -eq $Result.texture.resource) {
      $failures.Add('Persistent snapshot was unavailable or did not create a distinct resource')
    }
  }
  if($Acceptance.requireSnapshotStagesNoReplay) {
    foreach($phase in @('history.resolvePersistentSnapshot', 'history.snapshotDisplay',
                        'history.snapshotRGBReadback', 'history.resolveSameSnapshotAgain')) {
      $samples = @($Result.samples | Where-Object phase -eq $phase)
      if($samples.Count -ne 1 -or $samples[0].status -ne 'complete' -or $samples[0].backendReplayCalls -ne 0) {
        $failures.Add("Snapshot phase replayed or lacks complete evidence: $phase")
      }
    }
  }
  if($Acceptance.requireSnapshotIdentityReuse -and
     ($histories.Count -ne 1 -or -not $histories[0].sameSnapshotNoReplay)) {
    $failures.Add('Repeated resolve did not reuse the same snapshot without replay')
  }
  if($Acceptance.requireSnapshotRGBMatch) {
    if($histories.Count -ne 1 -or -not $histories[0].twoPathsRGBIdentical -or
       -not $histories[0].transientRGB.sha256 -or -not $histories[0].snapshotRGB.sha256 -or
       $histories[0].transientRGB.sha256 -ne $histories[0].snapshotRGB.sha256 -or
       $histories[0].transientRGB.bytes -ne $histories[0].snapshotRGB.bytes) {
      $failures.Add('Direct preview and snapshot RGB payloads differ or are absent')
    }
  }
  # Native/proxy topology is not exposed by the public snapshot API. A future caller may demand
  # native proof, but unknown evidence must then fail rather than being inferred from success.
  if($Acceptance.requireNativeSnapshot -and
     ($histories.Count -ne 1 -or $histories[0].snapshotBackendKind -ne 'nativeGPU')) {
    $failures.Add('Native GPU snapshot path was not positively identified')
  }
  return [pscustomobject]@{passed=($failures.Count -eq 0); failures=@($failures.ToArray());
    previewRecords=$previews; snapshotBackendKind=$histories[0].snapshotBackendKind}
}
