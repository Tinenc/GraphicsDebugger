import copy
import json
import struct
import unittest
from unittest.mock import patch

from eid_d3d12_shader_buffer_profile import (D3D12EditProbe, exercise_combination, replay_evidence)
from eid_shared_buffer_profile import byte_witness


EVENTS = [20, 38, 56, 74]
VALUES = [struct.pack("<4f", *v) for v in ((0.8, 0.1, 0.3, 1.0), (0.1, 0.85, 0.2, 1.0))]


def native_records(previous, target, force=False, hit=False):
    return [
        {"api": "D3D12LiveForward", "from": previous, "target": target, "enabled": 1,
         "hit": int(hit), "initialRestoreSkipped": int(hit),
         "replayedEvents": target - previous - 1 if hit else 0},
        {"api": "D3D12", "type": "WithoutDraw", "replay_depth": 0,
         "range": "%d->%d" % (previous + 1 if hit else 1, target), "partial": int(hit),
         "section.applyInitial_calls": 0 if hit else 1},
        {"api": "D3D12", "type": "OnlyDraw", "replay_depth": 0,
         "range": "0->%d" % target, "partial": 1},
        {"api": "ReplayDriverBoundary", "type": "WithoutDraw", "eid": target},
        {"api": "ReplayDriverBoundary", "type": "OnlyDraw", "eid": target},
    ]


class NativeReplayEvidenceTests(unittest.TestCase):
    def test_correctness_accepts_real_fallback_without_claiming_hits(self):
        result = replay_evidence(native_records(20, 38), 20, 38)
        self.assertFalse(result["issues"])
        self.assertEqual(result["matchingHits"], 0)
        self.assertEqual(result["optimizationAcceptance"], "notClaimedByCorrectnessProbe")
        self.assertFalse(replay_evidence(native_records(38, 20, True), 38, 20, True)["issues"])

    def test_optional_live_hit_requires_exact_source_range_and_skipped_initial(self):
        rows = native_records(20, 38, hit=True)
        self.assertFalse(replay_evidence(rows, 20, 38)["issues"])
        for field, value in (("from", 19), ("target", 39), ("replayedEvents", 18),
                             ("initialRestoreSkipped", 0), ("enabled", 0)):
            changed = copy.deepcopy(rows)
            changed[0][field] = value
            self.assertTrue(replay_evidence(changed, 20, 38)["issues"])
        self.assertTrue(replay_evidence(rows, 20, 38, force=True)["issues"])

    def test_absent_native_or_only_outer_success_never_passes(self):
        rows = native_records(20, 38)
        for index in range(len(rows)):
            with self.subTest(missing=index):
                self.assertTrue(replay_evidence(rows[:index] + rows[index + 1:], 20, 38)["issues"])
        self.assertTrue(replay_evidence([], 20, 38)["issues"])

    def test_wrong_range_partial_nested_or_no_initial_restore_fails(self):
        for index, field, value in ((1, "range", "1->37"), (1, "partial", 1),
                                    (1, "replay_depth", 1), (1, "section.applyInitial_calls", 0),
                                    (2, "range", "0->37"), (3, "eid", 37)):
            rows = native_records(20, 38)
            rows[index][field] = value
            self.assertTrue(replay_evidence(rows, 20, 38, force=True)["issues"])

    def test_disabled_control_and_duplicate_prefix_are_checked(self):
        rows = native_records(20, 38)
        rows[0]["enabled"] = 0
        self.assertFalse(replay_evidence(rows, 20, 38, disabled=True)["issues"])
        self.assertTrue(replay_evidence(rows, 20, 38)["issues"])
        rows = native_records(20, 38)
        rows.append(dict(rows[1]))
        self.assertTrue(replay_evidence(rows, 20, 38)["issues"])


class FakeController:
    def __init__(self, stale=False, repair=None, wrong_patch_undo=False, wrong_shader_undo=False):
        self.event, self.generation, self.value, self.visible = 38, 0, b"\0" * 16, b"\0" * 16
        self.shader, self.patched, self.trace, self.records = False, False, [], []
        self.stale, self.repair = stale, repair
        self.wrong_patch_undo, self.wrong_shader_undo = wrong_patch_undo, wrong_shader_undo
        self.shader_was_removed = False

    def GetReplayCheckpointStatus(self):
        status = {"schema": "renderdoc-replay-checkpoints/v1", "enabled": False, "events": [],
                  "hasInitialCheckpoint": False}
        for field in ("byteBudget", "maxCheckpoints", "checkpointCount", "allocatedBytes", "sharedBytes",
                      "builds", "hits", "misses", "invalidations", "rejected"):
            status[field] = 0
        return json.dumps(status)

    def GetCurrentEventID(self):
        return self.event

    def GetSharedBufferPatchGeneration(self):
        return self.generation

    def SetFrameEvent(self, event, force):
        self.trace.append(("SetFrameEvent", event, force))
        self.records.extend(native_records(self.event, event, force))
        self.event, self.visible = event, self.value

    def SetSharedBufferPatch(self, resource, offset, data):
        assert self.shader
        self.value, self.patched = data, True
        self.generation += 1
        if not self.stale:
            self.visible = data
        self.records.append({"api": "ReplayController", "op": "shared_buffer_patch", "event": self.event,
                             "generation": self.generation, "ranges": 1, "bytes": 16})
        return ""

    def ClearSharedBufferPatch(self, resource, offset, count):
        assert self.shader
        self.trace.append(("ClearPatch", self.shader))
        self.value = self.visible = b"\xff" * 16 if self.wrong_patch_undo else b"\0" * 16
        self.patched = False
        self.generation += 1
        self.records.append({"api": "ReplayController", "op": "shared_buffer_patch", "event": self.event,
                             "generation": self.generation, "ranges": 0, "bytes": 0})
        return ""

    def GetBufferData(self, resource, begin, count):
        if self.patched and self.repair:
            self.visible = self.value
            self.records.append(dict(self.repair))
        return b"L" * 16 + self.value + b"R" * 16

    def ReplaceTargetShaderChecked(self, original, target):
        self.trace.append(("ReplaceShader", original, target))
        self.shader = True
        return ""

    def RemoveTargetShaderReplacementChecked(self, original):
        assert not self.patched
        self.trace.append(("RemoveShader", self.patched))
        self.shader, self.shader_was_removed = False, True
        return ""

    def output(self, event=None):
        event = self.event if event is None else event
        code = 1 if self.visible == VALUES[0] else 2 if self.visible == VALUES[1] else 0
        shift = (100 if self.shader else 0) + code * 10
        depth = "staleDepth" if self.wrong_shader_undo and self.shader_was_removed else "originalDepth"
        return {"eventId": event, "shaders": {"Pixel": "replacement" if self.shader else "original"},
                "pixels": [{"bytes": 64, "sha256": "%d:%d" % (event, shift)},
                           {"bytes": 64, "sha256": depth}],
                "regions": [{"eventId": e, "uintBits": [e + shift, 0, 0, 1]} for e in EVENTS]}


class FakeRecorder:
    def __init__(self, controller):
        self.controller = controller

    def measure(self, label, callback, event=None, cleanup=False):
        self.controller.trace.append(label)
        self.controller.records = []
        value = callback()
        return value, {"wallMs": 1.0, "timingRecords": list(self.controller.records)}

    def publish(self):
        pass


class CombinedExecutionTests(unittest.TestCase):
    def make(self, **kwargs):
        controller = FakeController(**kwargs)
        report = {"issues": [], "mutations": [], "visits": [], "phases": [], "totalReadbackBytes": 0}
        run = D3D12EditProbe(controller, None, FakeRecorder(controller), report, "D3D12", EVENTS,
                            [], {}, [], 320, 256, (240, 48, 16), 1024, False, 0)
        run.baseline = {e: {"buffer": byte_witness(b"L" * 16 + b"\0" * 16 + b"R" * 16, 16, 16),
                            "output": controller.output(e)} for e in EVENTS}
        return run, controller, report

    def execute(self, run, controller):
        active = {"shader": False, "patch": False}
        with patch("eid_portable_forward_edit_profile.complete_witness", side_effect=lambda *a: controller.output()):
            exercise_combination(run, VALUES, 41, 99, active)
        return active

    def test_two_values_are_consecutive_without_eid_repair_then_both_undos_match(self):
        run, controller, report = self.make()
        self.assertEqual(self.execute(run, controller), {"shader": False, "patch": False})
        self.assertFalse(report["issues"])
        self.assertEqual(report["shaderWithBufferAcceptance"], "passedReplacementShaderWithSharedBufferAndUndo")
        first = controller.trace.index("shaderWithBuffer.sliderValue1.setter")
        self.assertEqual(controller.trace[first:first + 4], [
            "shaderWithBuffer.sliderValue1.setter", "shaderWithBuffer.sliderValue1.immediate.readback",
            "shaderWithBuffer.sliderValue2.setter", "shaderWithBuffer.sliderValue2.immediate.readback"])
        self.assertLess(controller.trace.index(("ClearPatch", True)),
                        controller.trace.index(("RemoveShader", False)))
        for phase in report["phases"]:
            self.assertEqual(phase["optimizationAcceptance"], "notClaimedByCorrectnessProbe")
            self.assertEqual(phase["observedNativeForwardHits"], 0)
            for event in EVENTS:
                refs = [r for r in report["visits"] if r["label"].startswith(phase["label"] + ".reference")
                        and r["eventId"] == event and r["force"]]
                self.assertEqual(len(refs), 2)

    def test_stale_immediate_output_cannot_pass_after_correct_force_references(self):
        run, controller, report = self.make(stale=True)
        self.execute(run, controller)
        self.assertTrue(report["issues"])
        self.assertEqual(report["shaderWithBufferAcceptance"], "failedOrIncomplete")

    def test_getter_repair_with_correct_pixels_fails_before_first_reference(self):
        for record in ({"api": "MaterializeEvent"}, {"api": "ReplayLog"},
                       {"api": "D3D12", "type": "OnlyDraw"},
                       {"api": "ReplayDriverBoundary", "type": "WithoutDraw"},
                       {"api": "ReplayChunk", "type": "Full"}):
            run, controller, report = self.make(stale=True, repair=record)
            with self.assertRaisesRegex(RuntimeError, "implicit replay"):
                self.execute(run, controller)
            self.assertFalse(any(isinstance(item, str) and item.startswith("shaderWithBuffer.value1.")
                                 for item in controller.trace))

    def test_patch_undo_checks_all_bytes_with_shader_still_installed(self):
        run, controller, report = self.make(wrong_patch_undo=True)
        self.execute(run, controller)
        self.assertTrue(any("failedOriginalStateNotRecovered" in issue for issue in report["issues"]))
        self.assertEqual(report["shaderWithBufferAcceptance"], "failedOrIncomplete")

    def test_shader_undo_checks_other_full_output_bytes_not_only_probe(self):
        run, controller, report = self.make(wrong_shader_undo=True)
        self.execute(run, controller)
        self.assertTrue(any("shader.undone" in issue and "failedOriginalStateNotRecovered" in issue
                            for issue in report["issues"]))

    def test_checked_rejection_is_not_fabricated_as_success(self):
        run, controller, report = self.make()
        controller.ReplaceTargetShaderChecked = lambda *a: "native structure mismatch"
        with self.assertRaisesRegex(RuntimeError, "native structure mismatch"):
            self.execute(run, controller)
        self.assertNotIn("shaderAcceptance", report)
        self.assertEqual(report["mutations"], [])

    def test_d3d12_old_generation_live_hit_during_mutation_is_rejected(self):
        run, controller, report = self.make()
        original = controller.SetSharedBufferPatch
        def invalid(*args):
            error = original(*args)
            controller.records.append({"api": "D3D12LiveForward", "hit": 1})
            return error
        controller.SetSharedBufferPatch = invalid
        self.execute(run, controller)
        self.assertTrue(any("previous configuration" in issue for issue in report["issues"]))


if __name__ == "__main__":
    unittest.main()
