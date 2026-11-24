"""Check automatic idle checkpoint construction on any explicitly named capture.

Run through run_eid_isolated_python.ps1. Native safety/VRAM refusals are reported as failures when
--require-build is selected; they are never silently turned into successful performance results.
"""
import argparse
import json
import math
import os
from pathlib import Path
import traceback

from eid_auto_checkpoint_profile import immutable_status_issues
from eid_checkpoint_profile import compare_event, parse_status, status_issues
from eid_idle_checkpoint_profile import idle_issues, parse_receipt as parse_idle_receipt
from eid_portable_forward_edit_profile import readback_replay_issues
from eid_shared_buffer_profile import SharedBufferRecorder, output_witness, binary_identity, build_identity


CHECKPOINT_APIS = {'D3D11Checkpoint', 'D3D12Checkpoint', 'OpenGLCheckpoint', 'VulkanCheckpoint'}


def parse_receipt(text):
    value = parse_idle_receipt(text)
    if 'continueIdle' in value and type(value['continueIdle']) is not bool:
        raise ValueError('Optional continueIdle receipt field must be boolean')
    return value


def replay_records(sample):
    return [row for row in sample.get('timingRecords', [])
            if readback_replay_issues([row])]


def checkpoint_records(sample, operation):
    return [row for row in sample.get('timingRecords', [])
            if row.get('api') in CHECKPOINT_APIS and row.get('op') == operation
            and row.get('success', 1) == 1 and row.get('attempt', 'successful') == 'successful']


def planning_rejection_issues(row, event, mutation, generation):
    """Permit bounded CPU planning costs, never GPU work or an unproven retirement."""
    receipt, before, after = row['receipt'], row['before'], row['after']
    issues = []
    if (receipt['currentEventId'], receipt['mutationGeneration'], receipt['bufferGeneration']) != (
            event, mutation, generation) or not receipt['materialized']:
        issues.append('planning rejection changed current EID, generations, or materialized state')
    if receipt['expectedEventId'] != row['arguments'][0]:
        issues.append('receipt does not identify the requested EID')
    if row['beforeWitness'] != row['afterWitness']:
        issues.append('planning rejection changed pipeline or bound color/depth pixels')
    if receipt['performed'] or receipt['workUnits'] or receipt['builds'] or receipt['hits'] or receipt['replayMS']:
        issues.append('planning rejection receipt contains GPU work')
    if replay_records(row['timing']):
        issues.append('planning rejection interval contains native driver replay/materialization')
    if any(record.get('api') in CHECKPOINT_APIS and record.get('op') in ('save', 'restore')
           for record in row['timing'].get('timingRecords', [])):
        issues.append('planning rejection interval contains native checkpoint work')
    # Configuration may admit/reject an empty native policy and report its reason. Existing GPU
    # storage, native work counters, edit scope and all unrelated fields must stay identical.
    allowed_status = {'automatic', 'enabled', 'rejected', 'reason', 'candidatePlanKnown',
                      'candidateEvents', 'canTryOtherCandidates'}
    for key in set(before) | set(after):
        if key not in allowed_status and before.get(key) != after.get(key):
            issues.append('planning rejection changed native ' + key)
    if (after['rejected'] < before['rejected'] or
            (before.get('enabled') and not after.get('enabled') and before['events'])):
        issues.append('planning rejection discarded native admission evidence')
    for name in ('checkpointCountBefore', 'checkpointCountAfter'):
        status = before if name.endswith('Before') else after
        if receipt[name] != status['checkpointCount']:
            issues.append('planning rejection receipt differs from native checkpoint counts')
    for name in ('lastSaveEvent', 'lastResumeEvent', 'hasInitialCheckpoint'):
        if receipt[name] != after.get(name):
            issues.append('planning rejection receipt differs from native ' + name)

    first, last = before['automatic'], after['automatic']
    candidate = receipt['candidateEventId']
    if (not receipt['candidateSelected'] or not 0 <= candidate < event or
            candidate not in [item.get('eventId') for item in before.get('candidates', [])]):
        issues.append('planning rejection did not select an evidenced earlier candidate')
    retired_before, retired_after = first.get('rejectedCandidates'), last.get('rejectedCandidates')
    if (not isinstance(retired_before, list) or not isinstance(retired_after, list) or
            any(type(value) is not int or value < 0 for value in retired_before + retired_after) or
            len(set(retired_before)) != len(retired_before) or len(set(retired_after)) != len(retired_after) or
            candidate in retired_before or set(retired_after) != set(retired_before) | {candidate}):
        issues.append('planning rejection did not newly retire exactly the selected candidate')
    trial_before, trial_after, limit = first.get('trialCount'), last.get('trialCount'), last.get('trialLimit')
    if (any(type(value) is not int for value in (trial_before, trial_after, limit)) or
            first.get('trialLimit') != limit or not 0 <= trial_before < trial_after <= limit or
            trial_after != trial_before + 1):
        issues.append('planning rejection has no bounded forward trial progress')
    elif receipt.get('continueIdle', False) and trial_after >= limit:
        issues.append('planning rejection continued after its finite trial budget')
    allowed_policy = {'state', 'reason', 'trialCount', 'incrementalCostMS', 'configurationPending',
                      'idleTotalMS', 'rejectedCandidates'}
    for key in set(first) | set(last):
        if key not in allowed_policy and first.get(key) != last.get(key):
            issues.append('planning rejection changed automatic ' + key)
    for key in ('incrementalCostMS', 'idleTotalMS'):
        values = (first.get(key), last.get(key))
        if (any(type(value) not in (float, int) or not math.isfinite(value) or value < 0 for value in values)
                or values[1] < values[0]):
            issues.append('planning rejection has invalid CPU cost accounting: ' + key)
    if receipt.get('continueIdle', False) and (after['byteBudget'] <= 0 or after['maxCheckpoints'] <= 0):
        issues.append('planning rejection continued without a positive GPU storage budget')
    return issues


def maintenance_issues(row, event, mutation, generation, revision, reason=None):
    receipt, before, after = row['receipt'], row['before'], row['after']
    planning = receipt['reason'] == 'candidateUnavailable' and not receipt['performed'] and not reason
    issues = (planning_rejection_issues(row, event, mutation, generation) if planning else
              idle_issues(row, event, mutation, generation, expected_reason=reason))
    if receipt.get('continueIdle', False) and not (planning or
            (receipt['performed'] and receipt['builds'] > 0 and receipt['reason'] == 'checkpointBuilt')):
        issues.append('continueIdle does not identify a new build or a retired planning candidate')
    if row['beforeContext'] != row['afterContext']:
        issues.append('maintenance changed actual native EID or edit generations')
    if receipt['configurationRevision'] != revision:
        issues.append('maintenance receipt changed configuration revision without configuration')
    if not receipt['performed'] and not planning:
        if before != after:
            issues.append('no-op changed checkpoint or automatic policy state')
        if checkpoint_records(row['timing'], 'save') or checkpoint_records(row['timing'], 'restore'):
            issues.append('no-op interval contains native checkpoint work')
        if reason and (receipt['candidateSelected'] or receipt['candidateEventId']):
            issues.append('stale request selected a checkpoint candidate')
    elif receipt['performed']:
        saves = checkpoint_records(row['timing'], 'save')
        if not replay_records(row['timing']):
            issues.append('performed maintenance has no instrumented native replay')
        if len(saves) != receipt['builds']:
            issues.append('receipt build count differs from successful native save records')
        if receipt['builds']:
            if (receipt['reason'] != 'checkpointBuilt' or
                    receipt['lastSaveEvent'] != after.get('lastSaveEvent') or
                    receipt['lastSaveEvent'] not in after['events'] or
                    not any(r.get('event') == receipt['lastSaveEvent'] for r in saves)):
                issues.append('claimed build has no matching retained native checkpoint')
        elif receipt['reason'] == 'checkpointBuilt':
            issues.append('receipt claims checkpointBuilt without a native build')
        if receipt['hasInitialCheckpoint'] != after.get('hasInitialCheckpoint'):
            issues.append('receipt initial checkpoint flag differs from native state')
        for key in ('naturalReferenceVisits', 'observedSavingsMS'):
            if before['automatic'].get(key) != after['automatic'].get(key):
                issues.append('idle prebuild manufactured automatic ' + key)
    return sorted(set(issues))


class PrewarmProbe:
    def __init__(self, controller, rd, recorder, report, maximum):
        self.controller, self.rd, self.recorder, self.report = controller, rd, recorder, report
        self.maximum = maximum
        self.retired_candidates = set()

    def issue(self, label, issues):
        self.report['issues'].extend(label + ': ' + issue for issue in issues)

    def context(self):
        event = int(self.controller.GetCurrentEventID())
        return [event, int(self.controller.GetShaderReplacementGeneration(event)),
                int(self.controller.GetSharedBufferPatchGeneration())]

    def status(self):
        value = parse_status(self.controller.GetReplayCheckpointStatus())
        self.issue('checkpointStatus', status_issues(value))
        if (value.get('automatic', {}).get('mode') != 'automatic' or
                value['automatic'].get('idlePrewarmEnabled') is not True):
            raise RuntimeError('Automatic idle prewarm is not enabled in the actual controller')
        events = value.get('events', [])
        if (len(events) != value['checkpointCount'] or len(set(events)) != len(events) or
                any(type(event) is not int or not 0 <= event < 2**32 for event in events)):
            raise RuntimeError('Native checkpoint count/events are inconsistent')
        return value

    def witness(self, label, event):
        before, context = self.status(), self.context()
        if context[0] != event:
            raise RuntimeError('Witness started at a stale EID')
        remaining = self.maximum - self.report['totalReadbackBytes']
        if remaining <= 0:
            raise RuntimeError('Cumulative readback budget exhausted')
        value, sample = self.recorder.measure(
            label, lambda: output_witness(self.controller, self.rd, remaining), event)
        self.report['totalReadbackBytes'] += sum(row['bytes'] for row in value['pixels'])
        self.issue(label, readback_replay_issues(sample.get('timingRecords', [])))
        self.issue(label, immutable_status_issues(before, self.status()))
        if self.context() != context or value['eventId'] != event:
            raise RuntimeError('Witness changed or misreported the EID or edit generations')
        return value

    def force(self, label, event):
        before, context = self.status(), self.context()
        _, timing = self.recorder.measure(label + '.replay',
            lambda: self.controller.SetFrameEvent(event, True), event)
        after = self.status()
        self.issue(label, immutable_status_issues(before, after))
        current = self.context()
        if current[0] != event or current[1:] != context[1:]:
            raise RuntimeError('Forced reference changed EID or edit generations')
        if not replay_records(timing):
            self.issue(label, ['No instrumented native forced replay; no-op absence is unverified'])
        if checkpoint_records(timing, 'save') or checkpoint_records(timing, 'restore'):
            self.issue(label, ['Forced reference used native checkpoint save/restore'])
        pixels = self.witness(label + '.output', event)
        row = dict(eventId=event, before=before, after=after, timing=timing, witness=pixels)
        self.report['references'].append(row)
        return pixels

    def idle(self, label, arguments, baseline, revision=None, reason=None):
        context = self.context()
        row = dict(arguments=list(arguments), before=self.status(), beforeContext=context,
                   beforeWitness=baseline, phase=label)
        self.report['requestAttempts'].append(row)
        receipt, timing = self.recorder.measure(label,
            lambda: parse_receipt(self.controller.RunIdleCheckpointMaintenance(*arguments)), context[0])
        row.update(receipt=receipt, timing=timing, after=self.status(), afterContext=self.context(),
                   nativeReplayRecords=replay_records(timing), wallMs=timing['wallMs'])
        # Check the immediate image before any reference replay can repair it.
        readback_issues_before = len(self.report['issues'])
        row['afterWitness'] = self.witness(label + '.output', context[0])
        row['issues'] = maintenance_issues(row, *context,
            receipt['configurationRevision'] if revision is None else revision, reason=reason)
        if len(self.report['issues']) != readback_issues_before:
            row['issues'].append('Immediate maintenance witness failed strict readback checks')
        if receipt['candidateSelected'] and receipt['candidateEventId'] in self.retired_candidates:
            row['issues'].append('maintenance retried a previously retired candidate')
        if receipt['reason'] == 'candidateUnavailable' and not row['issues']:
            self.retired_candidates.add(receipt['candidateEventId'])
        self.issue(label, row['issues'])
        return row

    def visit(self, label, event):
        before, context = self.status(), self.context()
        _, timing = self.recorder.measure(label,
            lambda: self.controller.SetFrameEvent(event, False), event)
        after, current = self.status(), self.context()
        if current[0] != event or current[1:] != context[1:]:
            raise RuntimeError('Ordinary visit changed EID or edit generations')
        pixels = self.witness(label + '.output', event)
        # Configuration can reset cumulative counters. Require the exact native controller delta
        # in that case; never turn a negative raw difference into a guessed zero or hit.
        hits = after['hits'] - before['hits']
        records = [r for r in timing.get('timingRecords', []) if r.get('api') == 'ReplayCheckpointAuto']
        if hits < 0:
            if (len(records) != 1 or records[0].get('eid') != event or
                    type(records[0].get('hits')) is not int or not 0 <= records[0]['hits'] <= after['hits']):
                raise RuntimeError('Native hit counter reset without an exact automatic delta')
            hits = records[0]['hits']
        if hits and not any(r.get('event') == after.get('lastResumeEvent')
                            for r in checkpoint_records(timing, 'restore')):
            self.issue(label, ['Claimed native hit has no matching successful restore record'])
            hits = 0
        row = dict(eventId=event, before=before, after=after, wallMs=timing['wallMs'], timing=timing,
                   witness=pixels, references=[], comparison='pending', nativeHits=hits, phase=label)
        self.report['visits'].append(row)
        return row

    def verify_visit(self, row):
        label, event = row['phase'], row['eventId']
        refs = [self.force(label + '.reference%d' % repeat, event) for repeat in range(2)]
        verdict = compare_event([row['witness']], refs)
        row.update(references=refs, comparison=verdict)
        if verdict != 'passed':
            self.issue(label, ['Ordinary prewarmed image: ' + verdict])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', required=True)
    parser.add_argument('--events', required=True, help='Two or more real output EIDs in drag order')
    parser.add_argument('--output', required=True)
    parser.add_argument('--build-label', required=True)
    parser.add_argument('--require-build', action='store_true')
    parser.add_argument('--require-hit', action='store_true')
    parser.add_argument('--preserve-captured-upload-heaps', action='store_true',
                        help='D3D12 capacity A/B: retain captured UPLOAD heaps instead of remapping large buffers')
    parser.add_argument('--max-idle', type=int, default=6)
    parser.add_argument('--max-read-mib', type=int, default=512)
    parser.add_argument('--max-seconds', type=int, default=300)
    args = parser.parse_args(argv)
    try:
        events = [int(value) for value in args.events.split(',')]
    except ValueError:
        parser.error('EIDs must be comma-separated integers')
    output = Path(args.output).resolve()
    if not 2 <= len(events) <= 32 or len(set(events)) < 2 or any(not 0 < event < 2**32 for event in events):
        parser.error('Two to 32 visits including at least two distinct valid EIDs are required')
    if not 1 <= args.max_idle <= 16 or not 1 <= args.max_read_mib <= 4096 or args.max_seconds <= 0:
        parser.error('Work/read budgets must be bounded')
    if not Path(args.capture).is_file():
        parser.error('Capture must exist')
    if (any(path.exists() for path in (output, output.with_suffix('.rdoc.log'), output.with_suffix('.native.log')))
            or list(output.parent.glob(output.stem + '.*.timing.log'))):
        parser.error('Use a fresh evidence output stem')
    output.parent.mkdir(parents=True, exist_ok=True)
    environment = {'RENDERDOC_EID_TIMING': '1', 'RENDERDOC_REPLAY_CHECKPOINTS': 'auto',
                   'RENDERDOC_D3D12_PRESERVE_CAPTURED_UPLOAD_HEAPS':
                       '1' if args.preserve_captured_upload_heaps else '0',
                   'RENDERDOC_REPLAY_DISABLE_IDLE_PREWARM': '0',
                   'RENDERDOC_D3D12_ENABLE_PREPARED_REPLAY': '0',
                   'RENDERDOC_D3D12_DISABLE_PREPARED_REPLAY': '1'}
    os.environ.update(environment)
    report = dict(schema='renderdoc-idle-prewarm-profile/v1', ok=False, configuration=vars(args),
                  processId=os.getpid(), environment=environment, samples=[], idle=[], visits=[],
                  references=[], requestAttempts=[], staleRequests=[],
                  issues=[], totalReadbackBytes=0,
                  limits=['CPU wall includes shared-device contention; no exclusive-GPU timing claim',
                          'All bound color/depth outputs are compared, not unbound resources or all UAVs',
                          'One idle unit may execute a complete suffix; not a preemptible GPU time slice'])
    recorder = SharedBufferRecorder(report, output, output.with_suffix('.rdoc.log'), args.max_seconds)
    recorder.publish()
    rd = capture = controller = None
    initialized = False

    try:
        import renderdoc as rd
        rd.InitialiseReplay(rd.GlobalEnvironment(), [])
        initialized = True
        rd.SetDebugLogFile(str(output.with_suffix('.rdoc.log')))
        report['captureIdentity'] = binary_identity(Path(args.capture).resolve())
        report['buildIdentity'] = build_identity(rd, None)
        capture = rd.OpenCaptureFile()
        result, _ = recorder.measure('capture.openFile',
            lambda: capture.OpenFile(str(Path(args.capture).resolve()), '', None))
        if result != rd.ResultCode.Succeeded:
            raise RuntimeError(str(result))
        opened, _ = recorder.measure('capture.openReplay', lambda: capture.OpenCapture(rd.ReplayOptions(), None))
        result, controller = opened
        if result != rd.ResultCode.Succeeded:
            raise RuntimeError(str(result))
        target = max(events)
        probe = PrewarmProbe(controller, rd, recorder, report, args.max_read_mib * 1024 * 1024)
        # Capture loading may start on EID 0; mutation evidence for this unedited capture is zero.
        before_pixels = probe.force('initial', target)
        report['initialStatus'] = probe.status()
        _, mutation, generation = probe.context()
        revision = None
        stale_event = next(event for event in events if event != target)
        for label, arguments, reason in (
                ('stale-event', (stale_event, mutation, generation), 'staleEvent'),
                ('stale-mutation', (target, mutation + 1, generation), 'staleMutation'),
                ('stale-buffer', (target, mutation, generation + 1), 'staleBufferGeneration')):
            stale = probe.idle(label, arguments, before_pixels, revision, reason)
            revision = stale['receipt']['configurationRevision']
            report['staleRequests'].append(stale)
        report['staleRequest'] = report['staleRequests'][-1]['receipt']
        for index in range(args.max_idle):
            row = probe.idle('idle-%d' % index, (target, mutation, generation), before_pixels, revision)
            report['idle'].append(row)
            if row['issues']:
                break
            if row['receipt']['reason'] == 'candidateUnavailable' and row['receipt'].get('continueIdle', False):
                continue
            if not row['receipt']['performed'] or row['receipt']['builds'] == 0:
                if index + 1 < args.max_idle:
                    repeated = probe.idle('idle-repeat', (target, mutation, generation), before_pixels, revision)
                    report['repeatRequest'] = repeated
                    if repeated['receipt']['performed'] or repeated['receipt'].get('continueIdle', False):
                        probe.issue('idle-repeat', ['Unchanged failed/no-op request repeated native work or planning'])
                break
        report['prewarmedStatus'] = probe.status()
        refs = [probe.force('prewarm-reference%d' % repeat, target) for repeat in range(2)]
        verdict = compare_event([before_pixels] + [row['afterWitness'] for row in report['idle']], refs)
        report['prewarmComparison'] = verdict
        if verdict != 'passed':
            probe.issue('prewarm', ['Initial and idle images: ' + verdict])
        # Keep ordinary requests uninterrupted. A forced reference invalidates live resource
        # lineage and would turn every later request into a cold restore, hiding steady dragging.
        # Save each immediate witness now; run both independent references only after the sequence.
        for index, event in enumerate(events):
            probe.visit('visit-%d' % index, event)
        report['ordinarySequenceStatus'] = probe.status()
        for row in report['visits']:
            probe.verify_visit(row)
        builds = sum(row['receipt']['builds'] for row in report['idle'] if not row['issues'])
        hits = sum(row['nativeHits'] for row in report['visits'])
        report.update(actualIdleBuilds=builds, actualVisitHits=hits, finalStatus=probe.status(), measurementComplete=True)
        if args.require_build and builds == 0:
            report['issues'].append('Required native idle checkpoint build was not observed')
        if args.require_hit and hits == 0:
            report['issues'].append('Required native checkpoint hit was not observed')
        report['ok'] = not report['issues']
    except Exception:
        report['ok'] = False
        report['issues'].append(traceback.format_exc())
    finally:
        def cleanup(label, callback):
            try:
                recorder.measure(label, callback, cleanup=True)
            except Exception as error:
                report['ok'] = False
                report['issues'].append(label + ': ' + str(error))
        if controller is not None:
            cleanup('cleanup.controller', controller.Shutdown)
        if capture is not None:
            cleanup('cleanup.capture', capture.Shutdown)
        if initialized:
            cleanup('cleanup.runtime', rd.ShutdownReplay)
        try:
            if not recorder.log_path.is_file():
                raise RuntimeError('Native diagnostic log is missing')
            native = output.with_suffix('.native.log')
            native.write_bytes(recorder.log_path.read_bytes())
            report['nativeLog'] = str(native)
        except Exception as error:
            report['ok'] = False
            report['issues'].append('cleanup.log: ' + str(error))
        recorder.publish()
    print(json.dumps({'ok': report['ok'], 'output': str(output), 'issues': report['issues']}))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
