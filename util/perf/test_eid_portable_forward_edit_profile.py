"""No native library is imported; every controller operation below is a Python fake."""
import copy
import json
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from eid_portable_forward_edit_profile import (EditProbe, compare_configuration, mutation_issues,
                                               navigation, shared_identity, shared_member_offset,
                                               readback_replay_issues, stable_configuration, validate_shader_with_buffer,
                                               validate_d3d11_shader_rejection,
                                               values_from_hex)
from eid_shared_buffer_profile import byte_witness


VALUES = [struct.pack("<4f", *v) for v in ((0.8, 0.1, 0.3, 1), (0.1, 0.85, 0.2, 1))]
EVENTS = [31, 35, 39, 43]


def witness(event, edited=False):
    return {"buffer": {"patchHex": "ab" if edited else "00"}, "output": {
        "eventId": event, "pixels": [{"bytes": 4, "sha256": "edit" if edited else "original"}],
        "regions": [{"eventId": e, "uintBits": [e + (100 if edited else 0), 0, 0, 1]} for e in EVENTS]}}


class ConfigurationTests(unittest.TestCase):
    def test_values_are_two_distinct_exact_finite_float4_blocks(self):
        self.assertEqual(values_from_hex(",".join(value.hex() for value in VALUES)), VALUES)
        for text in ("00,01", VALUES[0].hex(), VALUES[0].hex() + "," + VALUES[0].hex(),
                     VALUES[0].hex() + "," + struct.pack("<4f", float("nan"), 0, 0, 1).hex()):
            with self.assertRaises(ValueError):
                values_from_hex(text)

    def test_sharing_uses_resource_and_exact_absolute_member_offset(self):
        rows = [{"buffer": 12, "absoluteOffset": 256, "bytes": 16} for _ in EVENTS]
        self.assertEqual(shared_identity(rows), (12, 256, 16))
        for key, value in (("buffer", 13), ("absoluteOffset", 272), ("bytes", 32)):
            changed = copy.deepcopy(rows)
            changed[2][key] = value
            with self.assertRaises(ValueError):
                shared_identity(changed)
        with self.assertRaises(ValueError):
            shared_identity(rows[:3])

    def test_reflection_in_bounds_does_not_authorize_a_truncated_bound_cb_view(self):
        self.assertEqual(shared_member_offset(1024, 272, 256, 272), 1280)
        # Same underlying buffer and reflected layout, but the active descriptor stops early.
        for view_size in (0, 16, 256, 271):
            with self.subTest(view_size=view_size), self.assertRaises(ValueError):
                shared_member_offset(1024, view_size, 256, 272)
        self.assertEqual(shared_member_offset(256, 16, 0, 16), 256)
        self.assertEqual(shared_member_offset(256, (1 << 64) - 1, 0, 16), 256)
        for values in ((0, 1024, 256, 271), (0, -1, 0, 16),
                       ((1 << 64) - 8, 16, 0, 16), (0, 16, -1, 16)):
            with self.assertRaises(ValueError):
                shared_member_offset(*values)

    def test_native_setter_requires_exact_generation_and_no_old_cursor_hit(self):
        row = {"api": "ReplayController", "op": "shared_buffer_patch", "event": 35,
               "generation": 2, "bytes": 16, "ranges": 1}
        self.assertFalse(mutation_issues([row], 35, 1, 2, 16))
        for records, before, after in (([], 1, 2), ([row], 2, 2),
                                      ([dict(row, generation=3)], 1, 2),
                                      ([dict(row, event=43)], 1, 2),
                                      ([dict(row, bytes=32)], 1, 2),
                                      ([row, row], 1, 2),
                                      ([row, {"api": "D3D11LiveForward", "hit": 1}], 1, 2)):
            self.assertTrue(mutation_issues(records, 35, before, after, 16))
        undo = dict(row, ranges=0, bytes=0)
        self.assertFalse(mutation_issues([undo], 35, 1, 2, 0))

    def data(self, edited=True):
        baseline = {event: witness(event) for event in EVENTS}
        references = {event: [witness(event, edited), witness(event, edited)] for event in EVENTS}
        observed = [{"eventId": event, "witness": witness(event, edited)} for event in EVENTS]
        return baseline, references, observed

    def test_all_four_shared_draws_must_change_and_match_forced_outputs(self):
        baseline, refs, visits = self.data()
        rows, issues = compare_configuration(visits, refs, baseline, EVENTS, witness(35, True))
        self.assertFalse(issues)
        self.assertTrue(all(row["ownRegionChanged"] for row in rows))
        refs[39] = [copy.deepcopy(baseline[39]) for _ in range(2)]
        self.assertTrue(compare_configuration(visits, refs, baseline, EVENTS)[1])

    def test_stale_immediate_image_cannot_be_hidden_by_later_good_navigation(self):
        baseline, refs, visits = self.data()
        stale = witness(35, False)
        stale["buffer"] = copy.deepcopy(witness(35, True)["buffer"])
        self.assertTrue(compare_configuration(visits, refs, baseline, EVENTS, stale)[1])

    def test_undo_requires_original_bytes_and_full_output(self):
        baseline, refs, visits = self.data(edited=False)
        self.assertFalse(compare_configuration(visits, refs, baseline, EVENTS,
                                               require_original=True)[1])
        for values in refs.values():
            for value in values:
                value["output"]["pixels"][0]["sha256"] = "stale unbound texture"
        self.assertTrue(compare_configuration(visits, refs, baseline, EVENTS, require_original=True)[1])

    def test_unstable_reference_and_zero_output_effect_never_pass(self):
        baseline, refs, visits = self.data()
        refs[35][1]["buffer"]["patchHex"] = "ff"
        self.assertTrue(compare_configuration(visits, refs, baseline, EVENTS)[1])
        baseline, refs, visits = self.data(False)
        self.assertTrue(compare_configuration(visits, refs, baseline, EVENTS)[1])

    def test_post_edit_navigation_starts_at_fixed_B_then_exercises_all_draws(self):
        self.assertEqual(navigation(EVENTS)[0], ("postEditBD", 43))
        self.assertEqual([row[1] for row in navigation(EVENTS)], [43, 31, 35, 43, 31, 39, 43])

    def test_readback_rejects_actual_replay_evidence_but_allows_display_work(self):
        self.assertFalse(readback_replay_issues([
            {"api": "RenderTexture", "type": "Draw", "total_ms": 1},
            {"api": "D3D11", "op": "GetTextureData", "total_ms": 1}]))
        for row in ({"api": "MaterializeEvent"}, {"api": "ReplayLog"},
                    {"api": "ReplayDriverBoundary", "type": "OnlyDraw"},
                    {"api": "ReplayChunk", "type": "WithoutDraw"}):
            self.assertTrue(readback_replay_issues([row]))
        for api in ("D3D11", "D3D12", "OpenGL", "Vulkan"):
            for replay_type in ("Full", "WithoutDraw", "OnlyDraw"):
                self.assertTrue(readback_replay_issues([{"api": api, "type": replay_type}]))


class FakeRecorder:
    def __init__(self, controller):
        self.controller = controller

    def measure(self, label, callback, event=None, cleanup=False):
        self.controller.trace.append(label)
        value = callback()
        records = []
        if label.endswith(".setter"):
            records = [{"api": "ReplayController", "op": "shared_buffer_patch", "event": 35,
                        "generation": self.controller.generation, "ranges": 1, "bytes": 16}]
        return value, {"wallMs": 1.0, "timingRecords": records}

    def publish(self):
        pass


class ImmediateSetterTests(unittest.TestCase):
    def make(self, stale=False):
        ctrl = SimpleNamespace(generation=0, value=b"\0" * 16, trace=[], event=35, stale=stale)
        status = {"schema": "renderdoc-replay-checkpoints/v1", "enabled": False, "events": [],
                  "hasInitialCheckpoint": False}
        for field in ("byteBudget", "maxCheckpoints", "checkpointCount", "allocatedBytes", "sharedBytes",
                      "builds", "hits", "misses", "invalidations", "rejected"):
            status[field] = 0
        ctrl.GetReplayCheckpointStatus = lambda: json.dumps(status)
        ctrl.GetSharedBufferPatchGeneration = lambda: ctrl.generation
        ctrl.GetCurrentEventID = lambda: ctrl.event
        def set_value(buffer, offset, data):
            ctrl.value, ctrl.generation = data, ctrl.generation + 1
            return ""
        ctrl.SetSharedBufferPatch = set_value
        ctrl.GetBufferData = lambda resource, begin, count: ctrl.value
        ctrl.SetFrameEvent = lambda *args: self.fail("Immediate slider witness must not call SetFrameEvent")
        report = {"issues": [], "mutations": [], "totalReadbackBytes": 0}
        recorder = FakeRecorder(ctrl)
        run = EditProbe(ctrl, None, recorder, report, "D3D11", EVENTS, [], {}, [], 12, 256,
                        (256, 16, 0), 1024, False, 1)
        return run, ctrl, report

    def test_consecutive_setters_read_immediately_at_same_eid(self):
        run, ctrl, report = self.make()
        def output(*args):
            return {"eventId": 35, "pixels": [{"bytes": 4, "sha256": ctrl.value.hex()}], "regions": []}
        with patch("eid_portable_forward_edit_profile.complete_witness", side_effect=output):
            first = run.change("value1", VALUES[0])
            second = run.change("value2", VALUES[1])
        self.assertNotEqual(first, second)
        self.assertEqual(ctrl.event, 35)
        self.assertEqual(ctrl.trace, ["value1.setter", "value1.immediate.readback",
                                      "value2.setter", "value2.immediate.readback"])
        self.assertFalse(report["issues"])

    def test_stale_eid_after_setter_fails_before_any_repair(self):
        run, ctrl, report = self.make()
        original = ctrl.SetSharedBufferPatch
        def stale(*args):
            original(*args)
            ctrl.event = 43
            return ""
        ctrl.SetSharedBufferPatch = stale
        with patch("eid_portable_forward_edit_profile.complete_witness", return_value={
                "eventId": 43, "pixels": [], "regions": []}):
            with self.assertRaises(RuntimeError):
                run.change("value1", VALUES[0])

    def test_correct_pixels_after_implicit_readback_repair_are_rejected(self):
        for record in ({"api": "MaterializeEvent", "eid": 35, "ok": 1},
                       {"api": "D3D11", "type": "OnlyDraw"}):
            run, ctrl, report = self.make()
            original = run.recorder.measure
            def measure(label, *args, **kwargs):
                value, timing = original(label, *args, **kwargs)
                if label.endswith(".readback"):
                    timing["timingRecords"].append(record)
                return value, timing
            run.recorder.measure = measure
            def output(*args):
                return {"eventId": 35, "pixels": [{"bytes": 4, "sha256": ctrl.value.hex()}], "regions": []}
            with patch("eid_portable_forward_edit_profile.complete_witness", side_effect=output):
                with self.assertRaisesRegex(RuntimeError, "implicit replay"):
                    run.change("value1", VALUES[0])
            self.assertEqual(report["mutations"], [])

    def test_enabled_phase_cannot_pass_without_a_positive_native_hit(self):
        run, ctrl, report = self.make()
        run.report.update(visits=[], phases=[])
        run.baseline = {event: witness(event) for event in EVENTS}
        def visit(label, event, force=False, expected=None):
            return {"eventId": event, "witness": witness(event, True),
                    "liveForward": {"matchingHits": 0}}
        run.visit = visit
        run.phase("value", VALUES[0])
        self.assertTrue(any("positive hit requirement failed" in issue for issue in report["issues"]))


class ShaderRejectionTests(unittest.TestCase):
    def make(self, mutation=None, accepted=False, repair=False):
        run, ctrl, report = ImmediateSetterTests.make(self)
        ctrl.shader_generation, ctrl.pixel_shader, ctrl.bad_output = 7, 77, False
        ctrl.GetShaderReplacementGeneration = lambda event: ctrl.shader_generation
        ctrl.GetPipelineState = lambda: SimpleNamespace(GetShader=lambda stage: ctrl.pixel_shader)
        run.rd = SimpleNamespace(ShaderStage=SimpleNamespace(Pixel="PS", Compute="CS"),
                                 ShaderEncoding=SimpleNamespace(HLSL="HLSL"),
                                 ShaderCompileFlags=lambda: None,
                                 ResourceId=SimpleNamespace(Null=lambda: 0))
        def build(entry, encoding, source, flags, stage):
            self.assertEqual((entry, encoding, stage), ("main", "HLSL", "CS"))
            self.assertIn(b"numthreads(1, 1, 1)", source)
            return 99, ""
        ctrl.BuildTargetShader = build
        def replace(original, target):
            self.assertEqual((original, target), (55, 99))
            if mutation == "pixels":
                ctrl.bad_output = True
            elif mutation == "event":
                ctrl.event = 43
            elif mutation == "bufferGeneration":
                ctrl.generation += 1
            elif mutation == "shaderGeneration":
                ctrl.shader_generation += 1
            elif mutation == "binding":
                ctrl.pixel_shader = 99
            elif mutation == "buffer":
                ctrl.value = b"\xff" * 16
            return "" if accepted else "The target shader stage differs from the captured stage"
        ctrl.ReplaceTargetShaderChecked = replace
        ctrl.FreeTargetResource = lambda *args: self.fail("Never free a possibly accepted target before witnessing rejection")
        def output(event, bad=False):
            return {"eventId": event, "pixels": [{"bytes": 4, "sha256": "bad" if bad else "acceptedPS"}],
                    "regions": [{"eventId": e, "uintBits": [e, 0, 0, 1]} for e in EVENTS]}
        refs = {event: [{"buffer": byte_witness(ctrl.value, 0, 16), "output": output(event)}
                        for _ in range(2)] for event in EVENTS}
        if repair:
            measure = run.recorder.measure
            def hidden_repair(label, *args, **kwargs):
                value, timing = measure(label, *args, **kwargs)
                if label == "shaderRejection.immediate.readback":
                    timing["timingRecords"].append({"api": "MaterializeEvent"})
                return value, timing
            run.recorder.measure = hidden_repair
        return run, ctrl, report, refs, lambda *args: output(ctrl.event, ctrl.bad_output)

    def test_wrong_stage_is_rejected_and_immediate_old_shader_state_is_unchanged(self):
        run, ctrl, report, refs, output = self.make()
        targets = []
        with patch("eid_portable_forward_edit_profile.complete_witness", side_effect=output):
            validate_d3d11_shader_rejection(run, 55, refs, targets)
        self.assertEqual(report["shaderRejectionAcceptance"],
                         "passedWrongStageRejectedWithoutChangingAcceptedState")
        self.assertFalse(report["issues"])
        self.assertEqual(targets, [99])
        self.assertEqual(ctrl.trace, ["shaderRejection.before.readback", "shaderRejection.buildCS",
                                      "shaderRejection.replaceChecked", "shaderRejection.immediate.readback"])

    def test_rejection_with_mutated_pixels_or_state_fails_without_replay_repair(self):
        for mutation in ("pixels", "event", "bufferGeneration", "shaderGeneration", "binding", "buffer"):
            with self.subTest(mutation=mutation):
                run, ctrl, report, refs, output = self.make(mutation=mutation)
                with patch("eid_portable_forward_edit_profile.complete_witness", side_effect=output):
                    with self.assertRaises(RuntimeError):
                        validate_d3d11_shader_rejection(run, 55, refs, [])
                self.assertEqual(report["shaderRejectionAcceptance"], "failedOrIncomplete")
                self.assertIn("shaderRejection.immediate.readback", ctrl.trace)

    def test_wrong_stage_success_or_hidden_readback_repair_cannot_pass(self):
        for arguments in ({"accepted": True}, {"repair": True}):
            run, ctrl, report, refs, output = self.make(**arguments)
            with patch("eid_portable_forward_edit_profile.complete_witness", side_effect=output):
                with self.assertRaises(RuntimeError):
                    validate_d3d11_shader_rejection(run, 55, refs, [])
            self.assertEqual(report["shaderRejectionAcceptance"], "failedOrIncomplete")

    def test_other_backends_do_not_claim_or_run_the_d3d11_negative_probe(self):
        run, ctrl, report, refs, output = self.make()
        for api in ("D3D12", "OpenGL", "OpenGLES", "Vulkan"):
            run.api = api
            validate_d3d11_shader_rejection(run, 55, refs, [])
            self.assertEqual(report["shaderRejectionAcceptance"], "notRequestedNotTested")
        self.assertEqual(ctrl.trace, [])


class ShaderCombinationTests(unittest.TestCase):
    def make(self, stale_immediate=False, ignores_buffer=False, wrong_undo=False):
        ctrl = SimpleNamespace(event=35, generation=0, value=b"\0" * 16, patch_active=False,
                               shader_active=True, trace=[])
        status = {"schema": "renderdoc-replay-checkpoints/v1", "enabled": False, "events": [],
                  "hasInitialCheckpoint": False}
        for field in ("byteBudget", "maxCheckpoints", "checkpointCount", "allocatedBytes", "sharedBytes",
                      "builds", "hits", "misses", "invalidations", "rejected"):
            status[field] = 0
        ctrl.GetReplayCheckpointStatus = lambda: json.dumps(status)
        ctrl.GetCurrentEventID = lambda: ctrl.event
        ctrl.GetSharedBufferPatchGeneration = lambda: ctrl.generation
        ctrl.GetBufferData = lambda *args: ctrl.value
        ctrl.SetFrameEvent = lambda *args: self.fail("Live combined edits must not repair output by switching EID")
        def set_value(buffer, offset, data):
            self.assertTrue(ctrl.shader_active, "Replacement must remain installed during every shared setter")
            ctrl.value, ctrl.patch_active = data, True
            ctrl.generation += 1
            return ""
        def clear_value(buffer, offset, count):
            self.assertTrue(ctrl.shader_active, "Shared patch must be removed before the replacement shader")
            ctrl.value, ctrl.patch_active = (b"\xff" * 16 if wrong_undo else b"\0" * 16), False
            ctrl.generation += 1
            return ""
        ctrl.SetSharedBufferPatch, ctrl.ClearSharedBufferPatch = set_value, clear_value

        class Recorder(FakeRecorder):
            def measure(self, *args, **kwargs):
                value, timing = FakeRecorder.measure(self, *args, **kwargs)
                for record in timing["timingRecords"]:
                    if not self.controller.patch_active:
                        record.update(ranges=0, bytes=0)
                return value, timing
        report = {"issues": [], "mutations": [], "visits": [], "phases": [], "totalReadbackBytes": 0}
        run = EditProbe(ctrl, None, Recorder(ctrl), report, "D3D11", EVENTS, [], {}, [], 12, 256,
                        (256, 16, 0), 1024, False, 1)
        def output(event, value, shader, patched, stale=False):
            code = 0 if ignores_buffer or not patched or stale else 1 if value == VALUES[0] else 2
            shift = (100 if shader else 0) + 10 * code
            return {"eventId": event, "pixels": [{"bytes": 4, "sha256": "%d:%d" % (event, shift)}],
                    "shaders": {"Pixel": "replacement" if shader else "original"},
                    "regions": [{"eventId": e, "uintBits": [e + shift, 0, 0, 1]} for e in EVENTS]}
        def native_witness(event, shader, value=b"\0" * 16, patched=False):
            return {"buffer": byte_witness(value, 0, 16), "output": output(event, value, shader, patched)}
        def immediate_output(*args):
            return output(ctrl.event, ctrl.value, ctrl.shader_active, ctrl.patch_active, stale_immediate)
        def visit(label, event, force=False, expected=None):
            ctrl.trace.append(label)
            ctrl.event = event
            return {"eventId": event, "witness": native_witness(event, ctrl.shader_active, ctrl.value, ctrl.patch_active),
                    "liveForward": {"matchingHits": 0 if force else 1}}
        run.visit = visit
        original = {event: native_witness(event, False) for event in EVENTS}
        run.baseline = original
        shader_refs = {event: [native_witness(event, True), native_witness(event, True)] for event in EVENTS}
        return run, ctrl, report, original, shader_refs, immediate_output

    def test_combined_setters_are_consecutive_and_undo_keeps_replacement_installed(self):
        run, ctrl, report, original, refs, output = self.make()
        with patch("eid_portable_forward_edit_profile.complete_witness", side_effect=output):
            validate_shader_with_buffer(run, VALUES, refs)
        self.assertEqual(report["shaderWithBufferAcceptance"], "passedReplacementShaderWithSharedBufferAndUndo")
        self.assertFalse(report["issues"])
        self.assertTrue(ctrl.shader_active)
        self.assertFalse(ctrl.patch_active)
        self.assertIs(run.baseline, original)
        self.assertEqual(ctrl.trace[:4], ["shaderWithBuffer.sliderValue1.setter",
            "shaderWithBuffer.sliderValue1.immediate.readback", "shaderWithBuffer.sliderValue2.setter",
            "shaderWithBuffer.sliderValue2.immediate.readback"])
        self.assertEqual(len(report["phases"]), 3)
        for label in report["shaderWithBuffer"]["phaseLabels"]:
            # Every configuration must retain both forced references for every actual consumer.
            for event in EVENTS:
                self.assertIn(label + ".reference1.EID%d" % event, ctrl.trace)
                self.assertIn(label + ".reference2.EID%d" % event, ctrl.trace)
        self.assertTrue(report["shaderWithBuffer"]["patchUndoCompletedWithReplacementInstalled"])

    def test_later_correct_replays_cannot_hide_stale_combined_immediate_output(self):
        run, ctrl, report, original, refs, output = self.make(stale_immediate=True)
        with patch("eid_portable_forward_edit_profile.complete_witness", side_effect=output):
            validate_shader_with_buffer(run, VALUES, refs)
        self.assertEqual(report["shaderWithBufferAcceptance"], "failedOrIncomplete")
        self.assertTrue(any("failedStableCanonicalMismatch" in issue for issue in report["issues"]))
        self.assertIs(run.baseline, original)

    def test_replacement_that_ignores_shared_buffer_cannot_pass_on_shader_effect_alone(self):
        run, ctrl, report, original, refs, output = self.make(ignores_buffer=True)
        with patch("eid_portable_forward_edit_profile.complete_witness", side_effect=output):
            validate_shader_with_buffer(run, VALUES, refs)
        self.assertEqual(report["shaderWithBufferAcceptance"], "failedOrIncomplete")
        self.assertTrue(any("no changed own-region output" in issue for issue in report["issues"]))

    def test_patch_undo_compares_full_shader_only_buffer_and_output(self):
        run, ctrl, report, original, refs, output = self.make(wrong_undo=True)
        with patch("eid_portable_forward_edit_profile.complete_witness", side_effect=output):
            validate_shader_with_buffer(run, VALUES, refs)
        self.assertEqual(report["shaderWithBufferAcceptance"], "failedOrIncomplete")
        self.assertTrue(any("failedOriginalStateNotRecovered" in issue for issue in report["issues"]))
        self.assertTrue(ctrl.shader_active)

    def test_missing_or_unstable_shader_references_fail_before_any_setter(self):
        run, ctrl, report, original, refs, output = self.make()
        for bad in ({event: pair for event, pair in refs.items() if event != 39},
                    refs):
            broken = copy.deepcopy(bad)
            if len(broken) == 4:
                broken[35][1]["output"]["pixels"] = []
            with self.assertRaises(RuntimeError):
                validate_shader_with_buffer(run, VALUES, broken)
        self.assertEqual(ctrl.trace, [])
        self.assertIs(run.baseline, original)
        self.assertEqual(report["shaderWithBufferAcceptance"], "failedOrIncomplete")

        missing_bytes = copy.deepcopy(refs)
        for row in missing_bytes[35]:
            row["buffer"] = {}
        with self.assertRaises(RuntimeError):
            stable_configuration(missing_bytes, EVENTS)

    def test_missing_combination_reference_restores_baseline_and_never_reports_pass(self):
        run, ctrl, report, original, refs, output = self.make()
        run.phase = lambda *args, **kwargs: {}
        with patch("eid_portable_forward_edit_profile.complete_witness", side_effect=output):
            with self.assertRaises(RuntimeError):
                validate_shader_with_buffer(run, VALUES, refs)
        self.assertIs(run.baseline, original)
        self.assertEqual(report["shaderWithBufferAcceptance"], "failedOrIncomplete")


if __name__ == "__main__":
    unittest.main()
