"""CPU-only validation of the delta acceptance harness; no native replay module is imported."""
import copy
import json
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from eid_buffer_delta_profile import (DeltaProbe, changed_runs, checkpoint_evidence, compact_upload_evidence, comparison,
                                      configure_environment, delta_evidence, expected_window, fixture_event_evidence,
                                      member_component, phase_acceptance, scalar_bytes, validate_binding_pair,
                                      validate_parameters)


def arguments(**overrides):
    values = dict(early_event=11, late_event=31, fixed_event=41, early_buffer=12,
                  early_offset=16, late_buffer=13, late_offset=32, same_buffer_ranges=False,
                  early_value="0.25", late_values="0.375,0.75,0.875", expected_cutoff=None,
                  expected_disabled_cutoff=None, neighbors=16, max_read_mib=1, max_seconds=60,
                  checkpoint_event=21, budget_mib=64, max_checkpoints=4,
                  require_checkpoint_reuse=False, disable_delta=False)
    values.update(overrides)
    return SimpleNamespace(**values)


def checkpoint_status():
    status = {"schema": "renderdoc-replay-checkpoints/v1", "enabled": True,
              "hasInitialCheckpoint": False, "events": [21], "lastResumeEvent": 0,
              "sharedBufferPatchFirstAffectedEvent": 0, "sharedBufferPatchGeneration": 0}
    for name in ("byteBudget", "maxCheckpoints", "checkpointCount", "allocatedBytes", "sharedBytes",
                 "builds", "hits", "misses", "invalidations", "rejected"):
        status[name] = 0
    status.update(byteBudget=1024, maxCheckpoints=4, checkpointCount=1, allocatedBytes=16, builds=1)
    return status


class ParameterTests(unittest.TestCase):
    def test_native_checkpoint_boundary_need_not_be_a_draw_but_draw_bindings_still_must_be(self):
        nodes = [SimpleNamespace(eventId=event, flags=1, children=[]) for event in (11, 31, 41)]
        nodes.append(SimpleNamespace(eventId=21, flags=0, children=[]))
        controller = SimpleNamespace(GetRootActions=lambda: nodes)
        rd = SimpleNamespace(ActionFlags=SimpleNamespace(Drawcall=1, Indirect=2))
        exact = lambda _controller, events: [dict(eventId=e, status='exactAPIEvent') for e in events]
        with patch('eid_buffer_delta_profile.selected_event_evidence', side_effect=exact):
            draws, evidence = fixture_event_evidence(controller, rd, arguments())
            self.assertEqual(draws, [11, 31, 41])
            self.assertEqual([row['eventId'] for row in evidence], [11, 21, 31, 41])
            nodes[1].flags = 0
            with self.assertRaisesRegex(RuntimeError, 'direct draw'):
                fixture_event_evidence(controller, rd, arguments())
            nodes[1].flags = 3
            with self.assertRaisesRegex(RuntimeError, 'direct draw'):
                fixture_event_evidence(controller, rd, arguments())
        with patch('eid_buffer_delta_profile.selected_event_evidence',
                   return_value=[dict(eventId=21, status='missingAPIEvent')]):
            with self.assertRaisesRegex(RuntimeError, 'exact captured'):
                fixture_event_evidence(controller, rd, arguments())

    def test_scalar_delta_counts_actual_ieee_bytes_not_float_width(self):
        self.assertEqual(changed_runs(scalar_bytes(0.375), scalar_bytes(0.75)), (1, 2))
        self.assertEqual(changed_runs(b"\0" * 4, b"\1\0\1\0"), (2, 2))
        self.assertEqual(changed_runs(b"abcd", b"abcd"), (0, 0))
        for value in ("nan", "inf", "-inf", "1e100"):
            with self.assertRaises(ValueError):
                scalar_bytes(value)
        with self.assertRaises(ValueError):
            changed_runs(b"1", b"12")

    def test_parameters_require_actual_distinct_storage_and_positive_control(self):
        args = arguments()
        early, late = validate_parameters(args)
        self.assertEqual(len(early), 4)
        self.assertEqual(len(late), 3)
        self.assertEqual((args.expected_cutoff, args.expected_disabled_cutoff), (31, 11))
        validate_parameters(arguments(late_buffer=12, same_buffer_ranges=True))
        for overrides in ({"early_event": 31}, {"late_buffer": 12},
                          {"same_buffer_ranges": True}, {"early_offset": (1 << 64) - 1},
                          {"early_offset": 3}, {"late_values": "0.5,0.5000000000001"},
                          {"late_values": "0.5"}, {"expected_disabled_cutoff": 31},
                          {"checkpoint_event": 31}, {"checkpoint_event": None, "require_checkpoint_reuse": True},
                          {"late_values": "0.25,0.5", "require_checkpoint_reuse": True}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                validate_parameters(arguments(**overrides))

    def test_environment_is_explicit_before_loading_native_code(self):
        env = {"RENDERDOC_REPLAY_DISABLE_BUFFER_PATCH_DELTA": "1", "UNRELATED": "keep"}
        enabled = configure_environment(False, True, env)
        self.assertEqual(enabled["RENDERDOC_REPLAY_DISABLE_BUFFER_PATCH_DELTA"], "0")
        self.assertEqual(enabled["RENDERDOC_REPLAY_CHECKPOINTS"], "1")
        self.assertEqual(env["UNRELATED"], "keep")
        disabled = configure_environment(True, False, env)
        self.assertEqual(disabled["RENDERDOC_REPLAY_DISABLE_BUFFER_PATCH_DELTA"], "1")

    def test_native_member_must_fit_reflection_and_bound_view(self):
        self.assertEqual(member_component(256, 16, 16, 0, 4, 264), 2)
        self.assertEqual(member_component(256, (1 << 64) - 1, 16, 0, 4, 264), 2)
        for args in ((256, 8, 16, 0, 4, 264), (256, 16, 8, 0, 4, 264),
                     (256, 16, 16, 0, 4, 265), (256, 16, 16, 0, 4, 252),
                     (256, 16, 16, 0, 1, 260), ((1 << 64) - 2, 16, 16, 0, 1, (1 << 64) - 2)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                member_component(*args)

    def test_same_buffer_positive_requires_earlier_view_excludes_late_range(self):
        early = dict(buffer=12, eventId=11, absoluteOffset=0, bindingOffset=0, bindingByteSize=16)
        late = dict(buffer=12, eventId=31, absoluteOffset=256, bindingOffset=256, bindingByteSize=16)
        validate_binding_pair(early, late, True)
        for count in (257, (1 << 64) - 1):
            with self.assertRaises(ValueError):
                validate_binding_pair(dict(early, bindingByteSize=count), late, True)
        with self.assertRaises(ValueError):
            validate_binding_pair(early, late, False)

    def test_neighbors_account_for_the_other_scalar_in_the_same_buffer(self):
        specs = {"early": {"buffer": 12, "offset": 4}, "late": {"buffer": 12, "offset": 12}}
        window = {"buffer": 12, "offset": 2, "bytes": 16}
        baseline = bytes(range(16))
        result = expected_window(window, baseline, {"early": b"abcd", "late": b"WXYZ"}, specs)
        self.assertEqual(result, baseline[:2] + b"abcd" + baseline[6:10] + b"WXYZ" + baseline[14:])
        self.assertEqual(expected_window(window, baseline, {}, specs), baseline)


class EvidenceTests(unittest.TestCase):
    def test_compact_upload_cannot_pass_with_full_payload_or_missing_native_evidence(self):
        before, after = scalar_bytes(0.375), scalar_bytes(0.75)
        record = {"api": "OpenGLBufferPatch", "op": "set", "uploadReused": 1,
                  "uploadWrittenBytes": 2}
        check = lambda rows, mode="compact": compact_upload_evidence(
            rows, "OpenGL", mode, before, after, 8)["issues"]
        self.assertFalse(check([record]))
        self.assertFalse(check([dict(record, uploadReused=0, uploadWrittenBytes=8)], "full"))
        for rows in ([], [record, record], [dict(record, uploadReused=0)],
                     [dict(record, uploadWrittenBytes=8)], [dict(record, uploadWrittenBytes=True)],
                     [{"api": "OpenGLBufferPatch", "op": "set"}]):
            self.assertTrue(check(rows))
        self.assertTrue(check([record], "full"))
        for api, mode, original, size in (("Vulkan", "compact", before, 8),
                                          ("OpenGL", "unknown", before, 8),
                                          ("OpenGL", "compact", after, 8),
                                          ("OpenGL", "compact", before, 1)):
            with self.assertRaises(ValueError):
                compact_upload_evidence([record], api, mode, original, after, size)

    def records(self, disabled=False):
        cutoff = 11 if disabled else 31
        return [{"api": "D3D11BufferPatch", "op": "set", "firstAffected": cutoff,
                 "firstConsumer": 11, "deltaKnown": 1, "deltaEnabled": int(not disabled),
                 "changedRanges": 1, "changedBytes": 2, "changedBuffers": 1, "rangeIndexKnown": 1},
                {"api": "ReplayController", "op": "shared_buffer_patch", "event": 41,
                 "generation": 3, "first_affected_event": cutoff, "ranges": 2, "bytes": 8}]

    def evaluate(self, records, disabled=False):
        return delta_evidence(records, "D3D11", 41, 3, scalar_bytes(0.375), scalar_bytes(0.75),
                              11 if disabled else 31, disabled, True)["issues"]

    def test_cutoff_and_delta_require_exact_native_and_controller_records(self):
        self.assertFalse(self.evaluate(self.records()))
        self.assertFalse(self.evaluate(self.records(True), True))
        for key, value in (("firstAffected", 11), ("deltaKnown", 0), ("deltaEnabled", 0),
                           ("changedRanges", 2), ("changedBytes", 4), ("changedBuffers", 2),
                           ("rangeIndexKnown", 0)):
            records = self.records()
            records[0][key] = value
            self.assertTrue(self.evaluate(records), key)
        records = self.records()
        records[1]["event"] = 31
        self.assertTrue(self.evaluate(records))
        self.assertTrue(self.evaluate(self.records()[1:]))
        self.assertTrue(self.evaluate(self.records() + self.records()[:1]))

    def test_checkpoint_retention_requires_real_old_middle_point_hit_without_rebuild(self):
        before = checkpoint_status()
        after = dict(before, hits=1, lastResumeEvent=21)
        self.assertTrue(checkpoint_evidence(before, after, 11, 31, 21)["accepted"])
        for changes in ({"hits": 0}, {"builds": 2}, {"lastResumeEvent": 1}, {"events": []}):
            self.assertFalse(checkpoint_evidence(before, dict(after, **changes), 11, 31, 21)["accepted"])
        self.assertFalse(checkpoint_evidence(dict(before, events=[]), after, 11, 31, 21)["accepted"])
        self.assertFalse(checkpoint_evidence(before, after, 21, 31, 21)["accepted"])

    def test_disabled_control_allows_middle_invalidation_without_weakening_enabled_acceptance(self):
        before = checkpoint_status()
        after = dict(before, events=[10], hits=1, lastResumeEvent=10)
        later = dict(after, hits=2)
        deltas = [{"deltaEvidence": {"issues": []}, "checkpointEvidence": evidence} for evidence in
                  (checkpoint_evidence(before, after, 11, 31, 21),
                   checkpoint_evidence(after, later, 11, 31, 21))]
        enabled = phase_acceptance(deltas, False, True)
        self.assertTrue(enabled["issues"])
        self.assertFalse(enabled["checkpointReuseAccepted"])
        disabled = phase_acceptance(deltas, True, True)
        self.assertFalse(disabled["issues"])
        self.assertFalse(disabled["checkpointReuseAccepted"])
        self.assertFalse(disabled["cutoffReductionAccepted"])
        self.assertTrue(disabled["checkpointInvalidationControlAccepted"])
        deltas[0]["deltaEvidence"]["issues"] = ["wrong native cutoff"]
        self.assertFalse(phase_acceptance(deltas, True, True)["checkpointInvalidationControlAccepted"])

    def test_stale_immediate_result_and_undo_never_hide_behind_good_references(self):
        base = {"buffers": ["old"], "output": {"pixels": ["original"]}}
        edit = {"buffers": ["new"], "output": {"pixels": ["changed"]}}
        self.assertEqual(comparison("edit", edit, [edit, edit], base)["status"], "passed")
        self.assertNotEqual(comparison("edit", base, [edit, edit], base)["status"], "passed")
        self.assertNotEqual(comparison("edit", edit, [edit, base], base)["status"], "passed")
        self.assertNotEqual(comparison("undo", edit, [edit, edit], base, True)["status"], "passed")
        self.assertNotEqual(comparison("edit", base, [base, base], base)["status"], "passed")
        self.assertEqual(comparison("undo", base, [base, base], base, True)["status"], "passed")


class FakeRecorder:
    def __init__(self, controller):
        self.controller = controller

    def publish(self):
        pass

    def measure(self, label, callback, event=None, cleanup=False):
        self.controller.trace.append(label)
        value = callback()
        records = (self.controller.records if label.endswith((".setter", ".replay")) else
                   getattr(self.controller, "readback_records", []))
        return value, {"wallMs": 1.0, "timingRecords": copy.deepcopy(records)}


class ImmediateProbeTests(unittest.TestCase):
    def make(self):
        args = arguments()
        validate_parameters(args)
        controller = SimpleNamespace(event=41, generation=0, active={}, trace=[], records=[],
                                     status=checkpoint_status())
        controller.GetSharedBufferPatchGeneration = lambda: controller.generation
        controller.GetCurrentEventID = lambda: controller.event
        controller.GetReplayCheckpointStatus = lambda: json.dumps(controller.status)
        baseline = {12: bytes(range(64)), 13: bytes(range(64, 128))}
        def get_data(resource, offset, count):
            result = bytearray(baseline[resource])
            for (number, begin), value in controller.active.items():
                if number == resource:
                    result[begin:begin + 4] = value
            return bytes(result[offset:offset + count])
        controller.GetBufferData = get_data
        def set_value(resource, offset, value):
            previous = controller.active.get((resource, offset))
            controller.active[(resource, offset)] = value
            controller.generation += 1
            controller.status.update(sharedBufferPatchGeneration=controller.generation,
                                     sharedBufferPatchFirstAffectedEvent=31)
            controller.records = []
            if previous is not None:
                runs, count = changed_runs(previous, value)
                controller.records = [
                    {"api": "D3D11BufferPatch", "op": "set", "firstAffected": 31,
                     "deltaKnown": 1, "deltaEnabled": 1, "changedRanges": runs,
                     "changedBytes": count, "changedBuffers": 1},
                    {"api": "ReplayController", "op": "shared_buffer_patch", "event": 41,
                     "generation": controller.generation, "first_affected_event": 31,
                     "ranges": 2, "bytes": 8}]
            return ""
        controller.SetSharedBufferPatch = set_value
        controller.SetFrameEvent = lambda *unused: self.fail("Immediate value changes must not call SetFrameEvent")
        report = {"issues": [], "mutations": [], "references": {}, "captureDriver": "D3D11"}
        specs = {"early": {"buffer": 12, "resource": 12, "offset": 16},
                 "late": {"buffer": 13, "resource": 13, "offset": 32}}
        windows = [{"buffer": 12, "offset": 0, "bytes": 36},
                   {"buffer": 13, "offset": 16, "bytes": 36}]
        run = DeltaProbe(controller, None, FakeRecorder(controller), report, args, specs, windows, [], set())
        def output(*unused):
            return {"eventId": controller.event, "pixels": [str(sorted(controller.active.items()))]}
        return run, controller, report, output

    def test_consecutive_scalar_setters_read_outputs_before_any_reference_replay(self):
        run, controller, report, output = self.make()
        with patch("eid_buffer_delta_profile.texture_witness", side_effect=output):
            run.read("baseline")
            run.change("early", "early", scalar_bytes(0.25))
            first = run.change("lateV1", "late", scalar_bytes(0.375))
            second = run.change("lateV2", "late", scalar_bytes(0.75), True)
            third = run.change("lateV3", "late", scalar_bytes(0.875), True)
        self.assertNotEqual(first, second)
        self.assertNotEqual(second, third)
        self.assertFalse(report["issues"])
        self.assertEqual(controller.event, 41)
        self.assertEqual(controller.trace[-4:], ["lateV2.setter", "lateV2.immediate.readback",
                                                "lateV3.setter", "lateV3.immediate.readback"])

    def test_wrong_eid_from_setter_fails_before_readback_can_repair_it(self):
        run, controller, report, output = self.make()
        real = controller.SetSharedBufferPatch
        def stale(*args):
            real(*args)
            controller.event = 31
            return ""
        controller.SetSharedBufferPatch = stale
        with self.assertRaises(RuntimeError):
            run.change("early", "early", scalar_bytes(0.25))

    def test_correct_pixels_after_implicit_resource_read_replay_cannot_pass(self):
        for record in ({"api": "MaterializeEvent", "eid": 41, "ok": 1},
                       {"api": "D3D11", "type": "OnlyDraw"}):
            run, controller, report, output = self.make()
            with patch("eid_buffer_delta_profile.texture_witness", side_effect=output):
                run.read("baseline")
                controller.readback_records = [record]
                with self.assertRaisesRegex(RuntimeError, "implicit replay"):
                    run.change("early", "early", scalar_bytes(0.25))
            self.assertEqual(report["mutations"], [])

    def test_corrupt_neighbor_is_not_hidden_by_matching_patch_bytes(self):
        run, controller, report, output = self.make()
        with patch("eid_buffer_delta_profile.texture_witness", side_effect=output):
            run.read("baseline")
            read = controller.GetBufferData
            controller.GetBufferData = lambda *args: b"\xff" + read(*args)[1:]
            with self.assertRaises(RuntimeError):
                run.change("early", "early", scalar_bytes(0.25))

    def test_references_require_two_real_forced_prefixes(self):
        run, controller, report, output = self.make()
        calls = []
        def replay(event, force):
            calls.append((event, force))
            controller.records = [{"api": "D3D11", "type": "WithoutDraw"}]
        controller.SetFrameEvent = replay
        with patch("eid_buffer_delta_profile.texture_witness", side_effect=output):
            references = run.references("baseline")
        self.assertEqual(calls, [(41, True), (41, True)])
        self.assertEqual(len(references), 2)
        self.assertEqual(references[0], references[1])
        self.assertFalse(report["issues"])
        controller.SetFrameEvent = lambda *unused: setattr(controller, "records", [])
        with self.assertRaises(RuntimeError):
            run.references("missingNativePrefix")

    def test_preparation_builds_post_middle_with_later_prefix_and_restores_forward_setting(self):
        for original in (None, "1"):
            run, controller, report, output = self.make()
            controller.status.update(events=[], checkpointCount=0, builds=0)
            key = "RENDERDOC_D3D11_ENABLE_LIVE_FORWARD"
            env = {} if original is None else {key: original}
            calls = []
            def replay(event, force):
                self.assertEqual(env.get(key), "0", "Preparation must not skip saves via live-forward")
                self.assertFalse(force, "A forced reference bypasses checkpoint creation")
                calls.append(event)
                controller.event = event
                if event == run.args.early_event:
                    controller.status["events"] = [run.args.early_event - 1]
                elif event == run.args.late_event:
                    self.assertIn(run.args.early_event - 1, controller.status["events"])
                    controller.status["events"].append(run.args.checkpoint_event)
                controller.status["checkpointCount"] = len(controller.status["events"])
            controller.SetFrameEvent = replay
            with patch("eid_buffer_delta_profile.texture_witness", side_effect=output):
                run.prepare_checkpoint(env)
            self.assertEqual(calls, [11, 31, 41])
            self.assertNotIn(21, calls, "Selecting the checkpoint's own draw does not save its post-draw state")
            self.assertEqual(controller.event, 41)
            self.assertEqual(env.get(key), original)
            self.assertEqual(key in env, original is not None)
            self.assertTrue(report["checkpointPreparation"]["candidatePresent"])
            self.assertEqual(report["checkpointPreparation"]["liveForwardOverride"]["restoredValue"], original)

    def test_preparation_is_bounded_when_native_policy_cannot_create_requested_point(self):
        run, controller, report, output = self.make()
        controller.status.update(events=[10], checkpointCount=1)
        calls = []
        def replay(event, force):
            calls.append(event)
            controller.event = event
        controller.SetFrameEvent = replay
        env = {}
        with patch("eid_buffer_delta_profile.texture_witness", side_effect=output):
            run.prepare_checkpoint(env)
        self.assertEqual(calls, [11, 31, 11, 31, 11, 31, 41])
        self.assertEqual(controller.event, 41)
        self.assertEqual(env, {})
        self.assertFalse(report["checkpointPreparation"]["candidatePresent"])
        self.assertEqual(report["checkpointPreparation"]["reason"],
                         "nativePolicyDidNotRetainRequestedMiddleCheckpoint")

    def test_preparation_restores_process_setting_even_if_native_replay_raises(self):
        run, controller, report, output = self.make()
        key = "RENDERDOC_D3D11_ENABLE_LIVE_FORWARD"
        env = {key: "1"}
        def replay(*args):
            self.assertEqual(env[key], "0")
            raise RuntimeError("native preparation failure")
        controller.SetFrameEvent = replay
        with self.assertRaisesRegex(RuntimeError, "native preparation failure"):
            run.prepare_checkpoint(env)
        self.assertEqual(env, {key: "1"})
        self.assertFalse(report["checkpointPreparation"]["candidatePresent"])


if __name__ == "__main__":
    unittest.main()
