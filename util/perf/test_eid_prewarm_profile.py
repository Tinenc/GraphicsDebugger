"""CPU-only acceptance adversaries; no native module or capture/GPU is opened."""
import ast
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import eid_prewarm_profile as probe
from test_eid_shared_buffer_profile import Controller, Resource


class PrewarmController(Controller):
    def __init__(self, mode='correct'):
        super().__init__()
        self.mode, self.mutation, self.log_path = mode, 0, None
        self.corrupt = False
        self.last_operation = 'load'
        self.force_serial = 0
        self.trace = []
        self.stats.update(hasInitialCheckpoint=False, lastSaveEvent=0, lastResumeEvent=0,
                          sharedBufferPatchGeneration=0, sharedBufferPatchFirstAffectedEvent=0,
                          candidates=[{'eventId': 5}, {'eventId': 15}], automatic={
                              'mode': 'automatic', 'state': 'observing', 'idlePrewarmEnabled': True,
                              'naturalReferenceVisits': 0, 'observedSavingsMS': 0,
                              'points': [], 'idleWorkUnits': 0, 'trialCount': 0, 'trialLimit': 8,
                              'rejectedCandidates': [], 'incrementalCostMS': 0.0, 'idleTotalMS': 0.0})
        if mode == 'planningBounded':
            self.stats['candidates'] = [{'eventId': event} for event in range(1, 20)]
            self.stats['automatic']['trialLimit'] = 32

    def log(self, text):
        if self.log_path is not None and self.mode != 'missingInstrumentation':
            with self.log_path.open('a', encoding='utf-8') as stream:
                stream.write(text + '\n')

    def replay_log(self):
        self.log('api=D3D11 type=WithoutDraw replay_depth=0 total_ms=0.1')
        self.log('api=D3D11 type=OnlyDraw replay_depth=0 total_ms=0.1')

    def GetShaderReplacementGeneration(self, event):
        return self.mutation

    def SetFrameEvent(self, event, force):
        self.event, self.force, self.corrupt = event, force, False
        self.last_operation = 'force' if force else 'visit'
        self.trace.append((self.last_operation, event))
        self.replay_log()
        if force:
            self.force_serial += 1
            if self.mode == 'forceCounter':
                self.stats['misses'] += 1
            if self.mode == 'forceCheckpoint':
                self.log('api=D3D11Checkpoint op=restore event=5 success=1')
        elif any(e < event for e in self.stats['events']):
            saved = max(e for e in self.stats['events'] if e < event)
            self.stats['hits'] += 1
            self.stats['lastResumeEvent'] = saved
            if self.mode != 'falseHit':
                self.log('api=D3D11Checkpoint op=restore event=%d success=1' % saved)
        if self.mode == 'normalDepth' and not force:
            self.corrupt = True

    def GetTextureData(self, resource, sub):
        self.trace.append(('read', self.event, self.last_operation, resource.value))
        if self.last_operation in ('idle', 'visit'):
            if self.mode in ('materializeRepair', 'onlyDrawRepair', 'planningReadRepair'):
                self.log('api=MaterializeEvent eid=%d ok=1 total_ms=0.1' % self.event
                         if self.mode in ('materializeRepair', 'planningReadRepair') else
                         'api=D3D11 type=OnlyDraw replay_depth=0 total_ms=0.1')
                self.corrupt = False  # Correct bytes must not excuse getter-side replay.
            if self.mode == 'readGeneration':
                self.generation += 1
        value = super().GetTextureData(resource, sub)
        if self.mode == 'unstableReference' and self.force:
            value += str(self.force_serial).encode('ascii')
        if self.corrupt and resource == self.depth:
            value += b'wrong depth before reference repairs it'
        return value

    def RunIdleCheckpointMaintenance(self, event, mutation, generation):
        before = copy.deepcopy(self.stats)
        self.last_operation = 'idle'
        self.trace.append(('idle', event, mutation, generation))
        reason, performed, selected = 'noMissingIdleCandidate', False, 0
        continue_idle = None
        plan_before = before['automatic']['rejectedCandidates']
        if event != self.event:
            reason = 'staleEvent'
        elif mutation != self.mutation:
            reason = 'staleMutation'
        elif generation != self.generation:
            reason = 'staleBufferGeneration'
        elif self.mode == 'unsupported':
            reason = 'backendDisabledOrUnsupported'
        elif self.mode.startswith('planning') and (not plan_before or self.mode in (
                'planningDuplicate', 'planningBounded')):
            reason, selected, continue_idle = 'candidateUnavailable', 5, True
            if self.mode == 'planningBounded':
                selected = len(plan_before) + 1
            policy = self.stats['automatic']
            policy['trialCount'] += 1
            policy['incrementalCostMS'] += 0.2
            policy['idleTotalMS'] += 0.2
            policy.update(state='prewarming', reason='idleStagePrebuild', configurationPending=True)
            if selected not in policy['rejectedCandidates']:
                policy['rejectedCandidates'].append(selected)
            self.stats.update(enabled=True, rejected=before['rejected'] + 1, reason='no native boundary')
            if self.mode == 'planningNoRetirement':
                policy['rejectedCandidates'] = []
            elif self.mode == 'planningNoProgress':
                policy['trialCount'] -= 1
            elif self.mode == 'planningTrialExhausted':
                policy['trialCount'] = policy['trialLimit']
            elif self.mode == 'planningNoBudget':
                self.stats['byteBudget'] = 0
            elif self.mode == 'planningReplay':
                self.replay_log()
            elif self.mode == 'planningFailedSave':
                self.log('api=D3D11Checkpoint op=save event=5 success=0 attempt=rejected')
            elif self.mode == 'planningFailedRestore':
                self.log('api=D3D11Checkpoint op=restore event=5 success=0')
            elif self.mode == 'planningDepth':
                self.corrupt = True
            elif self.mode == 'planningGeneration':
                self.mutation += 1
            elif self.mode == 'planningPolicyGain':
                policy['observedSavingsMS'] += 5
            elif self.mode == 'planningWrongType':
                continue_idle = 'true'
            elif self.mode == 'planningTerminal':
                continue_idle = False
        elif self.mode == 'noBuild':
            reason, performed, selected = 'replayedWithoutNewCheckpoint', True, 5
            # Real native failure is suppressed on the same generation after the first attempt.
            if any(item == ('failedBuild',) for item in self.trace):
                reason, performed, selected = 'alreadyAttempted', False, 0
            else:
                self.trace.append(('failedBuild',))
                self.replay_log()
                self.stats['automatic']['idleWorkUnits'] += 1
        elif any(candidate['eventId'] not in before['events'] + plan_before for candidate in before['candidates']):
            selected = next(candidate['eventId'] for candidate in before['candidates']
                            if candidate['eventId'] not in before['events'] + plan_before)
            if self.mode == 'planningDuplicateBuild':
                selected = 5
            reason, performed = 'checkpointBuilt', True
            self.stats.update(enabled=True, events=before['events'] + [selected],
                              checkpointCount=before['checkpointCount'] + 1,
                              allocatedBytes=before['allocatedBytes'] + 64,
                              builds=before['builds'] + 1, lastSaveEvent=selected)
            self.stats['automatic']['idleWorkUnits'] += 1
            self.stats['automatic']['trialCount'] += 1
            self.replay_log()
            if self.mode != 'missingSaveLog':
                self.log('api=D3D11Checkpoint op=save event=%d attempt=successful' % selected)
            if self.mode == 'idleDepth':
                self.corrupt = True
            elif self.mode == 'idleGeneration':
                self.mutation += 1
            elif self.mode == 'fakeSavings':
                self.stats['automatic']['observedSavingsMS'] += 1
        stale = reason.startswith('stale')
        if stale and self.mode == 'staleCounter':
            self.stats['misses'] += 1
        if stale and self.mode == 'staleReplay':
            self.replay_log()
        receipt = dict(schema='renderdoc-replay-checkpoint-maintenance/v1', performed=performed,
                       reason=reason, expectedEventId=event, currentEventId=self.event,
                       mutationGeneration=self.mutation, bufferGeneration=self.generation,
                       configurationRevision=1, candidateSelected=bool(selected), candidateEventId=selected,
                       workUnits=int(performed), builds=self.stats['builds'] - before['builds'],
                       hits=self.stats['hits'] - before['hits'],
                       checkpointCountBefore=0 if stale else before['checkpointCount'],
                       checkpointCountAfter=0 if stale else self.stats['checkpointCount'],
                       lastSaveEvent=0 if stale else self.stats['lastSaveEvent'],
                       lastResumeEvent=0 if stale else self.stats['lastResumeEvent'],
                       hasInitialCheckpoint=False, materialized=True,
                       configurationMS=0.05 if reason == 'candidateUnavailable' else 0.0,
                       replayMS=0.1 if performed else 0.0, totalMS=0.2, backendReason='')
        if continue_idle is not None:
            receipt['continueIdle'] = continue_idle
        if performed:
            overrides = {'forgedBuild': {'builds': receipt['builds'] + 1},
                         'forgedCount': {'checkpointCountAfter': 7},
                         'forgedSave': {'lastSaveEvent': 9},
                         'forgedGeneration': {'bufferGeneration': self.generation + 1},
                         'forgedRevision': {'configurationRevision': 2},
                         'unmaterialized': {'materialized': False}}
            receipt.update(overrides.get(self.mode, {}))
        return json.dumps(receipt)

    def Shutdown(self):
        self.closed = True
        self.trace.append(('shutdownController',))
        self.log('controller shutdown reached')
        if self.mode == 'cleanupFailure':
            raise RuntimeError('synthetic controller cleanup failure')


class PrewarmProfileTests(unittest.TestCase):
    def run_fake(self, mode='correct', required=True, max_idle=4):
        controller = PrewarmController(mode)
        def capture_shutdown():
            controller.trace.append(('shutdownCapture',))
        def runtime_shutdown():
            controller.trace.append(('shutdownRuntime',))
            controller.log('runtime shutdown reached')
        capture_file = SimpleNamespace(OpenFile=lambda *args: 0,
            OpenCapture=lambda *args: (0, controller), Shutdown=capture_shutdown)
        rd = SimpleNamespace(InitialiseReplay=lambda *args: None, ShutdownReplay=runtime_shutdown,
            SetDebugLogFile=lambda path: setattr(controller, 'log_path', Path(path)),
            OpenCaptureFile=lambda: capture_file, GlobalEnvironment=lambda: None, ReplayOptions=lambda: None,
            ResultCode=SimpleNamespace(Succeeded=0), ResourceId=Resource,
            Subresource=lambda *args: args, ShaderStage=SimpleNamespace(Pixel=0))
        with tempfile.TemporaryDirectory() as directory:
            capture, output = Path(directory) / 'fixture.rdc', Path(directory) / 'nested' / 'report.json'
            capture.write_bytes(b'CPU fake input; never opened with a native library')
            args = ['--capture', str(capture), '--events', '10,20,10', '--output', str(output),
                    '--build-label', 'cpu-fake', '--max-idle', str(max_idle)]
            if required:
                args += ['--require-build', '--require-hit']
            with patch.dict(sys.modules, {'renderdoc': rd}), patch.dict(os.environ), \
                    patch.object(probe, 'build_identity', return_value={'nativeModules': ['CPU fake']}), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = probe.main(args)
            report = json.loads(output.read_text(encoding='utf-8'))
            log = output.with_suffix('.native.log')
            return result, report, controller, log.read_text(encoding='utf-8') if log.exists() else ''

    def test_real_recorder_full_outputs_and_double_refs_prove_native_builds_and_hits(self):
        result, report, controller, log = self.run_fake()
        self.assertEqual(result, 0, report['issues'])
        self.assertEqual(report['actualIdleBuilds'], 2)
        self.assertEqual(report['actualVisitHits'], 3)
        self.assertEqual(report['prewarmComparison'], 'passed')
        self.assertEqual(len(report['staleRequests']), 3)
        self.assertFalse(report['repeatRequest']['receipt']['performed'])
        for row in report['visits']:
            self.assertEqual(row['comparison'], 'passed')
            self.assertEqual(len(row['references']), 2)
            self.assertEqual(len(row['witness']['pixels']), 2)  # Color AND depth.
        first_visit = controller.trace.index(('visit', 10))
        first_reference = controller.trace.index(('force', 10))
        self.assertTrue(any(item[0] == 'read' for item in controller.trace[first_visit:first_reference]))
        ordinary = [index for index, item in enumerate(controller.trace) if item[0] == 'visit']
        self.assertEqual(len(ordinary), 3)
        self.assertLess(max(ordinary), first_reference)
        self.assertFalse(any(item[0] == 'force'
                             for item in controller.trace[min(ordinary):max(ordinary) + 1]))
        self.assertIn('runtime shutdown reached', log)

    def test_corrupt_depth_is_caught_before_forced_references_can_repair_it(self):
        for mode in ('idleDepth', 'normalDepth'):
            result, report, _, _ = self.run_fake(mode)
            self.assertEqual(result, 1, mode)
            self.assertTrue(report['issues'])
            self.assertTrue(report.get('measurementComplete'), report['issues'])
            if mode == 'normalDepth':
                self.assertTrue(all(row['references'][0] == row['references'][1] for row in report['visits']))

    def test_retired_planning_candidate_continues_to_different_native_build_and_real_hit(self):
        result, report, controller, _ = self.run_fake('planningCorrect')
        self.assertEqual(result, 0, report['issues'])
        rejected, built = report['idle'][:2]
        self.assertEqual(rejected['receipt']['candidateEventId'], 5)
        self.assertFalse(rejected['receipt']['performed'])
        self.assertTrue(rejected['receipt']['continueIdle'])
        self.assertEqual(rejected['beforeWitness'], rejected['afterWitness'])
        self.assertFalse(rejected['nativeReplayRecords'])
        self.assertFalse(probe.checkpoint_records(rejected['timing'], 'save'))
        self.assertFalse(probe.checkpoint_records(rejected['timing'], 'restore'))
        self.assertEqual(built['receipt']['candidateEventId'], 15)
        self.assertEqual(report['actualIdleBuilds'], 1)
        self.assertEqual(report['actualVisitHits'], 1)
        self.assertEqual(report['prewarmComparison'], 'passed')
        self.assertTrue(all(row['comparison'] == 'passed' for row in report['visits']))
        first_idle = controller.trace.index(('idle', 20, 0, 0))
        next_idle = controller.trace.index(('idle', 20, 0, 0), first_idle + 1)
        self.assertTrue(any(row[0] == 'read' for row in controller.trace[first_idle + 1:next_idle]))
        self.assertFalse(any(row[0] == 'force' for row in controller.trace[first_idle + 1:next_idle]))

    def test_rejected_candidate_cannot_be_retried_even_if_it_later_claims_a_real_build(self):
        for mode in ('planningDuplicate', 'planningDuplicateBuild'):
            result, report, _, _ = self.run_fake(mode)
            self.assertEqual(result, 1, report['issues'])
            self.assertIn('previously retired candidate', '\n'.join(report['issues']))
            self.assertEqual(report['actualIdleBuilds'], 0)

    def test_planning_rejection_cannot_hide_gpu_work_or_generation_and_image_changes(self):
        expected = {'planningReplay': 'native driver replay', 'planningFailedSave': 'native checkpoint work',
                    'planningFailedRestore': 'native checkpoint work', 'planningDepth': 'color/depth pixels',
                    'planningGeneration': 'generations', 'planningPolicyGain': 'observedSavingsMS',
                    'planningReadRepair': 'implicit replay'}
        for mode, message in expected.items():
            result, report, _, _ = self.run_fake(mode, required=False)
            self.assertEqual(result, 1, mode)
            self.assertIn(message, '\n'.join(report['issues']))
            self.assertEqual(len(report['idle']), 1, mode)

    def test_continue_requires_boolean_new_retirement_finite_trial_and_positive_storage_budget(self):
        expected = {'planningNoRetirement': 'newly retire', 'planningNoProgress': 'forward trial progress',
                    'planningTrialExhausted': 'trial', 'planningNoBudget': 'positive GPU storage budget',
                    'planningWrongType': 'must be boolean'}
        for mode, message in expected.items():
            result, report, _, _ = self.run_fake(mode, required=False)
            self.assertEqual(result, 1, mode)
            self.assertIn(message, '\n'.join(report['issues']))

    def test_forward_planning_is_bounded_to_sixteen_total_nonstale_calls(self):
        result, report, controller, _ = self.run_fake('planningBounded', required=False, max_idle=16)
        self.assertEqual(result, 0, report['issues'])
        self.assertEqual(len(report['idle']), 16)
        self.assertEqual([row['receipt']['candidateEventId'] for row in report['idle']], list(range(1, 17)))
        self.assertEqual(controller.trace.count(('idle', 20, 0, 0)), 16)
        self.assertEqual(report['actualIdleBuilds'], 0)
        self.assertEqual(report['actualVisitHits'], 0)

    def test_terminal_planning_rejection_must_suppress_further_native_work(self):
        # The fake deliberately builds a different candidate after continueIdle=false: this is
        # forbidden even though the same action is valid after a finite-progress true receipt.
        result, report, _, _ = self.run_fake('planningTerminal', required=False)
        self.assertEqual(result, 1)
        self.assertIn('repeated native work or planning', '\n'.join(report['issues']))

    def test_correct_pixels_with_getter_side_replay_are_not_immediate_success(self):
        for mode in ('materializeRepair', 'onlyDrawRepair'):
            result, report, _, _ = self.run_fake(mode)
            self.assertEqual(result, 1, mode)
            self.assertIn('implicit replay', '\n'.join(report['issues']))
            self.assertEqual(report['prewarmComparison'], 'passed')

    def test_stale_requests_must_be_true_noops(self):
        for mode in ('staleCounter', 'staleReplay'):
            result, report, _, _ = self.run_fake(mode)
            self.assertEqual(result, 1, mode)
            self.assertTrue(all(row['issues'] for row in report['staleRequests']))

    def test_receipt_claims_must_match_state_generation_revision_and_native_saves(self):
        for mode in ('forgedBuild', 'forgedCount', 'forgedSave', 'forgedGeneration', 'forgedRevision',
                     'missingSaveLog', 'unmaterialized', 'fakeSavings'):
            result, report, _, _ = self.run_fake(mode)
            self.assertEqual(result, 1, mode)
            self.assertTrue(any(row['issues'] for row in report['idle']), mode)

    def test_native_mutation_cannot_hide_behind_an_unchanged_image(self):
        for mode in ('idleGeneration', 'readGeneration'):
            result, report, _, _ = self.run_fake(mode)
            self.assertEqual(result, 1, mode)
            self.assertIn('generation', '\n'.join(report['issues']))

    def test_forced_reference_must_bypass_native_checkpoints_and_leave_cost_evidence_alone(self):
        for mode in ('forceCounter', 'forceCheckpoint'):
            result, report, _, _ = self.run_fake(mode)
            self.assertEqual(result, 1, mode)
            self.assertTrue(any('initial:' in issue for issue in report['issues']))

    def test_counter_only_hit_or_uninstrumented_success_does_not_pass(self):
        result, report, _, _ = self.run_fake('falseHit')
        self.assertEqual(result, 1)
        self.assertIn('matching successful restore', '\n'.join(report['issues']))
        self.assertEqual(report['actualVisitHits'], 0)
        result, report, _, _ = self.run_fake('missingInstrumentation', required=False)
        self.assertEqual(result, 1)
        self.assertIn('no-op absence is unverified', '\n'.join(report['issues']))

    def test_two_disagreeing_forced_references_are_inconclusive_even_if_one_matches(self):
        result, report, _, _ = self.run_fake('unstableReference')
        self.assertEqual(result, 1)
        self.assertEqual(report['prewarmComparison'], 'inconclusiveCanonicalUnstable')
        self.assertTrue(all(row['comparison'] == 'inconclusiveCanonicalUnstable' for row in report['visits']))

    def test_invalid_cli_and_old_timing_slices_are_rejected_before_native_import(self):
        with tempfile.TemporaryDirectory() as directory:
            capture, output = Path(directory) / 'fixture.rdc', Path(directory) / 'report.json'
            capture.write_bytes(b'CPU fake')
            base = ['--capture', str(capture), '--events', '10,20', '--output', str(output), '--build-label', 'cpu']
            for extra in (['--events', '10,10'], ['--events', 'not-an-eid'], ['--events', '0,10'],
                          ['--max-seconds', '0'], ['--max-idle', '17']):
                with self.subTest(extra=extra), contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as error:
                    probe.main(base + extra)
                self.assertEqual(error.exception.code, 2)
                self.assertFalse(output.exists())
            output.with_name('report.01.timing.log').write_text('old evidence', encoding='utf-8')
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                probe.main(base)
            self.assertEqual(error.exception.code, 2)

    def test_unsupported_and_failed_build_are_not_claimed_as_optimization(self):
        for mode in ('unsupported', 'noBuild'):
            result, report, _, _ = self.run_fake(mode, required=False)
            self.assertEqual(result, 0, report['issues'])
            self.assertEqual(report['actualIdleBuilds'], 0)
            self.assertEqual(report['actualVisitHits'], 0)
            result, report, _, _ = self.run_fake(mode)
            self.assertEqual(result, 1)
            self.assertIn('Required native idle checkpoint build', '\n'.join(report['issues']))

    def test_cleanup_failure_keeps_failure_and_still_closes_capture_runtime_and_log(self):
        result, report, controller, log = self.run_fake('cleanupFailure')
        self.assertEqual(result, 1)
        self.assertTrue(controller.closed)
        self.assertIn(('shutdownCapture',), controller.trace)
        self.assertIn(('shutdownRuntime',), controller.trace)
        self.assertIn('cleanup.controller:', '\n'.join(report['issues']))
        self.assertIn('runtime shutdown reached', log)

    def test_python36_compatibility(self):
        self.assertTrue(ast.parse(Path(probe.__file__).read_text(encoding='utf-8'), feature_version=(3, 6)).body)


if __name__ == '__main__':
    unittest.main()
