"""CPU-only idle probe tests. All native APIs below are Python fakes, with rendered state."""
import ast
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import eid_idle_checkpoint_profile as probe
from test_eid_auto_checkpoint_profile import discovery
from test_eid_shared_buffer_profile import FixedController, Resource, FIXED_VALUES


class IdleController(FixedController):
    def __init__(self, mode="correct", **kwargs):
        super().__init__(**kwargs)
        self.mode, self.attempted, self.log_path = mode, False, None
        self.mutation = 7
        self.bad_neighbor = False
        self.stats["candidates"] = [{"eventId": 15, "moduleId": 1, "priority": 1}]
        self.idle_trace = []

    def GetShaderReplacementGeneration(self, event):
        return self.mutation

    def log_replay(self):
        if self.log_path and self.mode != "missingInstrumentation":
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write("api=D3D12 type=WithoutDraw replay_depth=0 total_ms=0.1\n")

    def SetFrameEvent(self, event, force):
        self.bad_neighbor = False
        super().SetFrameEvent(event, force)
        self.log_replay()

    def GetBufferData(self, resource, offset, count):
        result = bytearray(super().GetBufferData(resource, offset, count))
        if getattr(self, "bad_neighbor", False) and offset == 0 and result:
            result[0] ^= 1
        return bytes(result)

    def RunIdleCheckpointMaintenance(self, event, mutation, generation):
        self.idle_trace.append((event, mutation, generation))
        before = copy.deepcopy(self.stats)
        reason, performed = "checkpointBuilt", False
        if event != self.event:
            reason = "staleEvent"
        elif mutation != self.mutation:
            reason = "staleMutation"
        elif generation != self.generation:
            reason = "staleBufferGeneration"
        elif self.attempted and self.mode != "repeatWork":
            reason = "alreadyAttempted"
        elif self.mode == "unsupported":
            reason = "backendDisabledOrUnsupported"
        elif self.mode == "noCandidate":
            reason = "noMissingExplicitCandidate"
        else:
            self.attempted, performed = True, True
            self.stats["builds"] += 1
            self.stats.update(events=[5, 15], checkpointCount=2, allocatedBytes=128, sharedBytes=64,
                              lastSaveEvent=15)
            self.log_replay()
            if self.mode == "wrongDepth":
                self.rendered[self.depth.value] += b"broken only until next forced reference"
            elif self.mode == "wrongNeighbor":
                self.bad_neighbor = True
            elif self.mode == "wrongGeneration":
                self.mutation += 1
            elif self.mode == "staleEID":
                self.event = 10
        if reason.startswith("stale") and self.mode == "staleCounter":
            self.stats["misses"] += 1
        if reason.startswith("stale") and self.mode == "staleReplay":
            self.log_replay()
        return json.dumps({"schema": "renderdoc-replay-checkpoint-maintenance/v1",
            "performed": performed, "reason": reason, "expectedEventId": event,
            "currentEventId": self.event, "mutationGeneration": self.mutation,
            "bufferGeneration": self.generation, "configurationRevision": 1,
            "candidateSelected": performed, "candidateEventId": 15 if performed else 0,
            "workUnits": int(performed), "builds": self.stats["builds"] - before["builds"],
            "hits": self.stats["hits"] - before["hits"],
            "checkpointCountBefore": before["checkpointCount"] if performed else 0,
            "checkpointCountAfter": self.stats["checkpointCount"] if performed else 0,
            "lastSaveEvent": self.stats.get("lastSaveEvent", 0), "lastResumeEvent": 5,
            "hasInitialCheckpoint": False, "materialized": True,
            "configurationMS": 0.0, "replayMS": 0.1 if performed else 0.0,
            "totalMS": 0.2 if performed else 0.01, "backendReason": self.mode})


class IdleCheckpointProfileTests(unittest.TestCase):
    def run_fake(self, controller, require=True, extra=None):
        capture_file = SimpleNamespace(OpenFile=lambda *args: 0, DriverName=lambda: "D3D12",
            OpenCapture=lambda *args: (0, controller), Shutdown=lambda: None)
        rd = SimpleNamespace(InitialiseReplay=lambda *args: None, ShutdownReplay=lambda: None,
            SetDebugLogFile=lambda value: setattr(controller, "log_path", Path(value)),
            OpenCaptureFile=lambda: capture_file, GlobalEnvironment=lambda: None, ReplayOptions=lambda: None,
            ResultCode=SimpleNamespace(Succeeded=0), ResourceId=Resource,
            ActionFlags=SimpleNamespace(Drawcall=1), Subresource=lambda *args: args,
            ShaderStage=SimpleNamespace(Pixel=0))
        with tempfile.TemporaryDirectory() as directory:
            capture, manifest, output = (Path(directory) / name for name in ("fixture.rdc", "discovery.json", "report.json"))
            capture.write_bytes(b"fake capture, never opened by a native runtime")
            manifest.write_text(json.dumps(discovery(str(capture))), encoding="utf-8")
            arguments = ["--discovery", str(manifest), "--fixed-values-hex", FIXED_VALUES,
                         "--output", str(output), "--require-neighbors-unchanged"]
            if require:
                arguments.append("--require-idle-build")
            arguments += extra or []
            with patch.dict(sys.modules, {"renderdoc": rd}), \
                    patch.object(probe, "build_identity", return_value={"nativeModules": [{"sha256": "fake"}]}), \
                    patch.object(probe, "binary_identity", return_value={"sha256": "a" * 64}), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = probe.main(arguments)
            return result, json.loads(output.read_text(encoding="utf-8"))

    def test_one_work_unit_preserves_edits_and_both_outputs_before_force(self):
        controller = IdleController()
        result, report = self.run_fake(controller)
        self.assertEqual(result, 0, report.get("error", report["issues"]))
        self.assertEqual(report["verdict"], "passedIdleBuildAndNativeParity")
        self.assertTrue(report["setupEvidence"]["readyForIdleBuild"])
        self.assertEqual(report["setupEvidence"]["retainedBeforeFirstConsumer"], [5])
        self.assertEqual(len(report["staleRequests"]), 3)
        self.assertTrue(all(not row["nativeReplayRecords"] and not row["issues"] for row in report["staleRequests"]))
        self.assertEqual(report["validRequest"]["receipt"]["builds"], 1)
        self.assertEqual(report["repeatRequest"]["receipt"]["reason"], "alreadyAttempted")
        self.assertTrue(all(row["status"] == "passed" for row in report["comparisons"]))
        self.assertEqual(len(report["validRequest"]["afterWitness"]["output"]["pixels"]), 2)
        self.assertTrue(report["nativeReplayLogCoverage"])
        self.assertLess(report["totalReadbackBytes"], 512 * 1024 * 1024)
        self.assertTrue(controller.closed)
        self.assertIsNone(controller.patch)
        self.assertFalse(report["disabledStatus"]["enabled"])

    def test_wrong_immediate_depth_fails_even_when_double_force_repairs_it(self):
        result, report = self.run_fake(IdleController("wrongDepth"))
        self.assertEqual(result, 1)
        self.assertIn("bound color/depth pixels", " ".join(report["validRequest"]["issues"]))
        self.assertTrue(any(row["phase"] == "maintenance" and row["status"] == "failedStableCanonicalMismatch"
                            for row in report["comparisons"]))
        self.assertEqual(report["clear"]["comparison"], "passed")

    def test_stale_requests_cannot_touch_counters_or_replay(self):
        for mode in ("staleCounter", "staleReplay"):
            result, report = self.run_fake(IdleController(mode))
            self.assertEqual(result, 1)
            self.assertTrue(all(row["issues"] for row in report["staleRequests"]))

    def test_maintenance_neighbor_corruption_cannot_hide_behind_correct_patch_bytes(self):
        result, report = self.run_fake(IdleController("wrongNeighbor"))
        self.assertEqual(result, 1)
        row = report["validRequest"]
        self.assertEqual(row["beforeWitness"]["buffer"]["patchHex"], row["afterWitness"]["buffer"]["patchHex"])
        self.assertNotEqual(row["beforeWitness"]["buffer"]["beforeHex"], row["afterWitness"]["buffer"]["beforeHex"])
        self.assertTrue(row["issues"])

    def test_mutation_generation_and_current_eid_changes_are_rejected(self):
        result, report = self.run_fake(IdleController("wrongGeneration"))
        self.assertEqual(result, 1)
        self.assertIn("generation", " ".join(report["validRequest"]["issues"]))
        result, report = self.run_fake(IdleController("staleEID"))
        self.assertEqual(result, 1)
        self.assertIn("stale current EID", report["error"])
        self.assertEqual(report["requestAttempts"][-1]["receipt"]["currentEventId"], 10)

    def test_repeat_must_be_noop_and_force_must_not_change_checkpoint_counters(self):
        result, report = self.run_fake(IdleController("repeatWork"))
        self.assertEqual(result, 1)
        self.assertIn("repeated request", " ".join(report["repeatRequest"]["issues"]))
        result, report = self.run_fake(IdleController(force_changes_counter=True))
        self.assertEqual(result, 1)
        self.assertIn("misses", " ".join(report["issues"]))

    def test_no_work_is_truthful_but_cannot_pass_required_idle_save(self):
        for mode in ("unsupported", "noCandidate"):
            result, report = self.run_fake(IdleController(mode), require=False)
            self.assertEqual(result, 0, report.get("error", report["issues"]))
            self.assertEqual(report["verdict"], "passedContractWithoutIdleBuild")
            self.assertEqual(report["validRequest"]["receipt"]["builds"], 0)
            result, report = self.run_fake(IdleController(mode))
            self.assertEqual(result, 1)
            self.assertIn("Required idle save", " ".join(report["issues"]))

    def test_missing_instrumentation_cannot_establish_native_noop(self):
        result, report = self.run_fake(IdleController("missingInstrumentation"))
        self.assertEqual(result, 1)
        self.assertFalse(report["nativeReplayLogCoverage"])
        self.assertIn("absence is unverified", " ".join(report["issues"]))

    def test_receipt_bounds_and_generic_packed_values(self):
        receipt = json.loads(IdleController().RunIdleCheckpointMaintenance(10, 7, 0))
        self.assertEqual(probe.parse_receipt(json.dumps(receipt))["workUnits"], 1)
        for fields in ({"workUnits": 2}, {"builds": -1}, {"hits": True}, {"totalMS": float("nan")},
                       {"schema": "unknown"}, {"performed": 1}):
            bad = dict(receipt, **fields)
            with self.assertRaises(ValueError):
                probe.parse_receipt(json.dumps(bad))
        self.assertEqual(probe.parse_values("010203,040506", 3), [b"\x01\x02\x03", b"\x04\x05\x06"])
        for text, width in (("01", 1), ("01,01", 1), ("01,0203", 2), ("01,xx", 1)):
            with self.assertRaises(ValueError):
                probe.parse_values(text, width)
        tree = ast.parse(Path(probe.__file__).read_text(encoding="utf-8"), feature_version=(3, 6))
        self.assertTrue(tree.body)


if __name__ == "__main__":
    unittest.main()
