"""CPU tests only: native-like controllers below never open a GPU or a real capture."""
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

import eid_auto_checkpoint_profile as probe
from test_eid_shared_buffer_profile import Controller, FixedController, Resource, FIXED_VALUES


class AutomaticController(FixedController):
    def __init__(self, fixed_hits=False, **kwargs):
        super().__init__(**kwargs)
        self.fixed_hits, self.setter_count, self.references = fixed_hits, 0, 0

    def ConfigureReplayCheckpoints(self, *args):
        raise AssertionError("The auto probe must never explicitly configure checkpoints")

    def SetSharedBufferPatch(self, *args):
        self.setter_count += 1
        self.enabled = self.fixed_hits and self.setter_count >= 3
        return super().SetSharedBufferPatch(*args)

    def SetFrameEvent(self, event, force):
        if not force:
            self.references += 1
        return super().SetFrameEvent(event, force)

    def GetReplayCheckpointStatus(self):
        value = json.loads(super().GetReplayCheckpointStatus())
        value["automatic"] = {"mode": "automatic", "state": "retained" if self.stats["hits"] else "observing",
                               "naturalReferenceVisits": self.references, "points": []}
        return json.dumps(value)


def discovery(capture="fixture.rdc", digest="a" * 64):
    return {"schema": "renderdoc-shared-buffer-fixture/v1", "ok": True, "capture": capture,
            "captureSha256": digest, "profileArguments": ["--data-hex", "aabbccdd"],
            "bindings": [{"eventId": event, "buffer": 1, "absoluteOffset": 4} for event in (10, 20)]}


class AutoCheckpointProfileTests(unittest.TestCase):
    def run_fake(self, controller, fixed=False, require=False):
        capture_file = SimpleNamespace(OpenFile=lambda *args: 0, DriverName=lambda: "D3D12",
                                       OpenCapture=lambda *args: (0, controller), Shutdown=lambda: None)
        rd = SimpleNamespace(InitialiseReplay=lambda *args: None, ShutdownReplay=lambda: None,
                             SetDebugLogFile=lambda *args: None, OpenCaptureFile=lambda: capture_file,
                             GlobalEnvironment=lambda: None, ReplayOptions=lambda: None,
                             ResultCode=SimpleNamespace(Succeeded=0), ResourceId=Resource,
                             ActionFlags=SimpleNamespace(Drawcall=1),
                             Subresource=lambda *args: args, ShaderStage=SimpleNamespace(Pixel=0))
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "fixture.rdc"
            output = Path(directory) / "report.json"
            manifest = Path(directory) / "discovery.json"
            capture.write_bytes(b"fake capture; no GPU")
            manifest.write_text(json.dumps(discovery(str(capture))), encoding="utf-8")
            arguments = ["--capture", str(capture), "--events", "10,20", "--rounds", "2", "--output", str(output)]
            if fixed:
                arguments += ["--discovery", str(manifest), "--fixed-values-hex", FIXED_VALUES,
                              "--require-neighbors-unchanged"]
            if require:
                arguments += ["--require-auto-hit"]
            with patch.dict(sys.modules, {"renderdoc": rd}), patch.dict(os.environ, {"RENDERDOC_REPLAY_CHECKPOINTS": ""}), \
                    patch.object(probe, "build_identity", return_value={"nativeModules": [{"sha256": "fake"}]}), \
                    patch.object(probe, "binary_identity", return_value={"sha256": "a" * 64}), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = probe.main(arguments)
            return result, json.loads(output.read_text(encoding="utf-8"))

    def test_observing_is_valid_correctness_but_cannot_pass_required_hit(self):
        result, report = self.run_fake(AutomaticController())
        self.assertEqual(result, 0, report.get("error", report["issues"]))
        self.assertEqual(len(report["normalVisits"]), 4)
        self.assertEqual(len(report["forceReferences"]), 4)
        self.assertEqual(report["verdict"], "passedNativeParityWithoutAutomaticHit")
        self.assertTrue(all(row["status"] == "passed" for row in report["comparisons"]))
        result, report = self.run_fake(AutomaticController(), require=True)
        self.assertEqual(result, 1)
        self.assertIn("No actual native", " ".join(report["issues"]))

    def test_default_fixed_entry_observes_setter_before_any_force_and_two_hits(self):
        controller = AutomaticController(fixed_hits=True)
        result, report = self.run_fake(controller, fixed=True, require=True)
        self.assertEqual(result, 0, report.get("error", report["issues"]))
        self.assertEqual(report["observedSetterHits"], 2)
        self.assertEqual(report["clearComparison"], "passed")
        for row in report["fixedValues"]:
            self.assertEqual(row["comparison"], "passed")
            self.assertEqual(len(row["witness"]["output"]["pixels"]), 2)
            trace = controller.call_trace
            start = trace.index(("set", row["valueHex"]))
            end = next(i for i in range(start + 1, len(trace)) if trace[i][0] == "replay")
            self.assertIn(("buffer", 0, 24), trace[start + 1:end])
            self.assertIn(("texture", 21), trace[start + 1:end])
            self.assertEqual(trace[end], ("replay", 20, True))
        self.assertTrue(controller.closed)
        self.assertIsNone(controller.patch)

    def test_wrong_immediate_depth_cannot_be_hidden_by_correct_force(self):
        result, report = self.run_fake(AutomaticController(fixed_hits=True, stale_pixels=True), fixed=True)
        self.assertEqual(result, 1)
        self.assertTrue(any(row["comparison"] == "failedStableCanonicalMismatch" for row in report["fixedValues"]))

    def test_stale_eid_is_rejected_before_reference_repairs_it(self):
        result, report = self.run_fake(AutomaticController(stale_eid=True), fixed=True)
        self.assertEqual(result, 1)
        self.assertIn("stale current EID", report["error"])

    def test_force_cannot_train_auto_or_change_native_counters(self):
        before = json.loads(AutomaticController().GetReplayCheckpointStatus())
        after = copy.deepcopy(before)
        after["automatic"]["naturalReferenceVisits"] += 1
        self.assertTrue(probe.immutable_status_issues(before, after))
        result, report = self.run_fake(AutomaticController(force_changes_counter=True), fixed=True)
        self.assertEqual(result, 1)
        self.assertIn("checkpoint state changed: misses", " ".join(report["issues"]))

    def test_discovery_requires_capture_identity_and_one_actual_range(self):
        params = probe.discovery_parameters(discovery())
        self.assertEqual((params["events"], params["buffer"], params["offset"], params["width"]),
                         ([10, 20], 1, 4, 4))
        for change in ({"ok": False}, {"captureSha256": ""}, {"bindings": []}):
            document = discovery()
            document.update(change)
            with self.assertRaises(ValueError):
                probe.discovery_parameters(document)
        document = discovery()
        document["bindings"][1]["buffer"] = 2
        with self.assertRaises(ValueError):
            probe.discovery_parameters(document)

    def test_reset_counter_is_unknown_without_exact_controller_delta(self):
        before = json.loads(AutomaticController().GetReplayCheckpointStatus())
        before["hits"], before["builds"] = 8, 4
        after = copy.deepcopy(before)
        after["hits"], after["builds"] = 1, 1
        result = probe.execution_evidence(before, after, {})
        self.assertIsNone(result["hits"])
        self.assertIsNone(result["builds"])
        result = probe.execution_evidence(before, after, {"timingRecords": [
            {"api": "ReplayCheckpointAuto", "hits": 1, "builds": 0, "reference": 0}]})
        self.assertEqual((result["hits"], result["builds"]), (1, 0))
        after.update(sharedBufferPatchGeneration=9, sharedBufferPatchFirstAffectedEvent=10,
                     lastResumeEvent=5, events=[5])
        evidence = probe.setter_checkpoint_evidence(before, after, 9, result)
        self.assertTrue(evidence["counterNormalizedFromNativeAutoRecord"])
        self.assertEqual(evidence["setterNativeHits"], 1)
        self.assertTrue(evidence["setterResumeBeforeCutoff"])
        with self.assertRaises(RuntimeError):
            probe.setter_checkpoint_evidence(before, after, 9, {"hits": None})

    def test_script_has_no_checkpoint_configuration_call_even_in_cleanup(self):
        tree = ast.parse(Path(probe.__file__).read_text(encoding="utf-8"))
        calls = [node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)]
        self.assertNotIn("ConfigureReplayCheckpoints", calls)
        with self.assertRaises(ValueError):
            probe.automatic_status(json.dumps(dict(json.loads(AutomaticController().GetReplayCheckpointStatus()),
                                                  automatic={"mode": "explicitOn", "state": "retained"})))


if __name__ == "__main__":
    unittest.main()
