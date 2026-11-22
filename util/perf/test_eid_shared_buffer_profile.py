"""CPU-only shared-buffer probe tests; every native call in integration tests is a Python fake."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import eid_shared_buffer_profile as probe


class Resource:
    def __init__(self, value=0):
        self.value = value

    def __str__(self):
        return "ResourceId::%d" % self.value

    def __eq__(self, other):
        return isinstance(other, Resource) and self.value == other.value

    @staticmethod
    def Null():
        return Resource()


class Controller:
    def __init__(self, propagate=False, wrong_normal_pixels=False, no_visible_effect=False):
        self.event, self.force, self.generation, self.patch = 10, False, 0, None
        self.propagate, self.wrong_normal_pixels = propagate, wrong_normal_pixels
        self.no_visible_effect = no_visible_effect
        self.closed, self.enabled, self.remote_mode, self.remote_closed = False, False, False, False
        self.stats = dict(schema="renderdoc-replay-checkpoints/v1", supported=True, enabled=False,
                          byteBudget=1024, maxCheckpoints=3, checkpointCount=0, allocatedBytes=0,
                          sharedBytes=0, builds=0, hits=0, misses=0, invalidations=0, rejected=0,
                          events=[])
        self.buffer, self.color, self.depth = Resource(1), Resource(20), Resource(21)
        format_value = SimpleNamespace(Name=lambda: "R8G8B8A8_UNORM")
        self.targets = [SimpleNamespace(resource=resource, firstMip=0, numMips=1, firstSlice=0,
                                        numSlices=1, format=format_value) for resource in
                        (self.color, self.depth)]
        self.textures = [SimpleNamespace(resourceId=resource, width=1, height=1, depth=1,
                                         arraysize=1, mips=1, msSamp=1) for resource in
                         (self.color, self.depth)]

    def GetSharedBufferPatchGeneration(self):
        return self.generation

    def invalidate(self):
        self.stats.update(checkpointCount=0, allocatedBytes=0, sharedBytes=0, events=[])
        self.stats["invalidations"] += 1

    def SetSharedBufferPatch(self, resource, offset, data):
        value = (resource.value, offset, bytes(data))
        if value != self.patch:
            self.patch = value
            self.generation += 1
            self.invalidate()
        return ""

    def ClearSharedBufferPatch(self, resource, offset, count):
        if self.patch and (resource.value, offset, count) != (self.patch[0], self.patch[1], len(self.patch[2])):
            return "wrong range"
        return self.ClearSharedBufferPatches()

    def ClearSharedBufferPatches(self):
        if self.patch is not None:
            self.patch = None
            self.generation += 1
            self.invalidate()
        return ""

    def ConfigureReplayCheckpoints(self, enabled, budget, maximum):
        self.enabled = enabled
        self.stats.update(enabled=enabled, byteBudget=budget or 1024, maxCheckpoints=maximum or 3)
        if not enabled:
            self.invalidate()

    def GetReplayCheckpointStatus(self):
        return json.dumps(self.stats)

    def SetFrameEvent(self, event, force):
        self.event, self.force = event, force
        if self.enabled and not force:
            if self.stats["checkpointCount"]:
                self.stats["hits"] += 1
            else:
                self.stats.update(checkpointCount=1, allocatedBytes=64, sharedBytes=64, events=[10])
                self.stats["builds"] += 1

    def GetCurrentEventID(self):
        return self.event

    def GetBuffers(self):
        return [SimpleNamespace(resourceId=self.buffer, length=64, creationFlags="Constants")]

    def GetBufferData(self, resource, offset, count):
        data = bytearray(range(64))
        if self.patch:
            _, start, content = self.patch
            data[start:start + len(content)] = content
            if self.propagate and content != bytes(range(start, start + len(content))):
                # Simulate a captured CopyBufferRegion from the patch to its nonoverlapping neighbor.
                data[start + len(content):start + 2 * len(content)] = content
        return bytes(data[offset:offset + count])

    def GetTextures(self):
        return self.textures

    def GetTextureData(self, resource, sub):
        source = bytes(range(64)) if self.no_visible_effect else self.GetBufferData(self.buffer, 0, 64)
        result = bytes([resource.value, self.event]) + source
        if self.wrong_normal_pixels and self.patch and not self.force and resource == self.depth:
            result += b"wrong depth after ordinary replay"
        return result

    def GetPipelineState(self):
        return SimpleNamespace(GetPrimitiveTopology=lambda: "TriangleList", GetShader=lambda stage: Resource(30),
                               GetOutputTargets=lambda: [self.targets[0]], GetDepthTarget=lambda: self.targets[1])

    def GetAPIProperties(self):
        return SimpleNamespace(pipelineType="D3D12", remoteReplay=self.remote_mode)

    def GetStructuredFile(self):
        return SimpleNamespace(chunks=[SimpleNamespace(name="Draw", metadata=SimpleNamespace(chunkID=100))])

    def GetRootActions(self):
        return [SimpleNamespace(eventId=e, flags=1, children=[],
                                events=[SimpleNamespace(eventId=e, chunkIndex=0, fileOffset=e * 10)])
                for e in (10, 20)]

    def GetUsage(self, resource):
        return [SimpleNamespace(eventId=e, usage="PS_Constants") for e in (10, 20)]

    def GetFatalErrorStatus(self):
        return SimpleNamespace(code=0)

    def Shutdown(self):
        self.closed = True


FIXED_VALUES = "01020304,05060708,11121314,15161718"


class FixedController(Controller):
    """A rendered-state fake: bytes can update while the already-rendered depth image stays stale."""
    def __init__(self, stale_pixels=False, stale_eid=False, no_hits=False, force_changes_counter=False):
        super().__init__()
        self.stale_pixels, self.stale_eid = stale_pixels, stale_eid
        self.no_hits, self.force_changes_counter = no_hits, force_changes_counter
        self.first_affected = 0
        self.call_trace = []
        self.rendered = {}
        self.render()

    def render(self):
        self.rendered = {resource.value: Controller.GetTextureData(self, resource, None)
                         for resource in (self.color, self.depth)}

    def SetSharedBufferPatch(self, resource, offset, data):
        value = bytes(data)
        old = self.patch
        prefix = 5 in self.stats["events"]
        old_rendered = dict(self.rendered)
        self.call_trace.append(("set", value.hex()))
        result = super().SetSharedBufferPatch(resource, offset, data)
        self.first_affected = 10 if old and old[:2] == self.patch[:2] and len(old[2]) == len(value) else 0
        if self.enabled and self.first_affected:
            self.stats.update(checkpointCount=1, allocatedBytes=64, sharedBytes=64, events=[5])
            if prefix and not self.no_hits:
                self.stats["hits"] += 1
                self.stats["lastResumeEvent"] = 5
            else:
                self.stats["builds"] += 1
        self.force = False
        self.render()
        if value.hex() in FIXED_VALUES.split(","):
            if self.stale_pixels:
                # Live Buffer contains the new bytes, color is fresh, but depth was never redrawn.
                self.rendered[self.depth.value] = old_rendered[self.depth.value]
            if self.stale_eid:
                self.event = 10
        return result

    def ClearSharedBufferPatches(self):
        result = super().ClearSharedBufferPatches()
        self.first_affected = 0
        self.render()
        return result

    def SetFrameEvent(self, event, force):
        self.call_trace.append(("replay", event, force))
        super().SetFrameEvent(event, force)
        if force and self.force_changes_counter and self.patch and self.patch[2].hex() in FIXED_VALUES.split(","):
            self.stats["misses"] += 1
        self.render()

    def GetReplayCheckpointStatus(self):
        result = dict(self.stats)
        result.update(sharedBufferPatchGeneration=self.generation,
                      sharedBufferPatchFirstAffectedEvent=self.first_affected)
        return json.dumps(result)

    def GetBufferData(self, resource, offset, count):
        self.call_trace.append(("buffer", offset, count))
        return super().GetBufferData(resource, offset, count)

    def GetTextureData(self, resource, sub):
        self.call_trace.append(("texture", resource.value))
        return self.rendered[resource.value]


class RepairingFixedController(FixedController):
    """A getter silently redraws stale output and emits real-format native timing evidence."""
    def __init__(self, stage, record):
        self.repair_stage, self.repair_record, self.repair_pending = stage, record, False
        self.repaired = False
        super().__init__(stale_pixels=True)

    def SetSharedBufferPatch(self, resource, offset, data):
        result = super().SetSharedBufferPatch(resource, offset, data)
        self.repair_pending = bytes(data).hex() in FIXED_VALUES.split(",")
        return result

    def repair(self, stage):
        if self.repair_pending and stage == self.repair_stage:
            self.repair_pending = False
            self.render()
            self.repaired = True
            self.call_trace.append(("implicitReplay", stage))
            with self.native_log_path.open("a", encoding="utf-8") as stream:
                stream.write("[EIDTiming] " + " ".join("%s=%s" % item
                                                      for item in self.repair_record.items()) + "\n")

    def GetBufferData(self, resource, offset, count):
        self.repair("buffer")
        return super().GetBufferData(resource, offset, count)

    def GetTextureData(self, resource, sub):
        self.repair("output")
        return super().GetTextureData(resource, sub)


class SharedBufferProfileTests(unittest.TestCase):
    def test_raw_readback_evidence_rejects_replay_without_requiring_aggregate_timings(self):
        records = [{"api": "MaterializeEvent"}, {"api": "ReplayLog"}]
        records += [{"api": api, "type": kind} for api, kind in
                    (("D3D11", "WithoutDraw"), ("D3D12", "OnlyDraw"), ("OpenGL", "Full"),
                     ("Vulkan", "WithoutDraw"), ("ReplayDriverBoundary", "OnlyDraw"),
                     ("ReplayChunk", "Full"))]
        self.assertEqual(len(probe.readback_replay_issues(records)), len(records))
        self.assertFalse(probe.readback_replay_issues([
            {"api": "RenderTexture", "type": "display"},
            {"api": "D3D12", "op": "GetTextureData"},
            {"api": "D3D12BufferPatch", "op": "set", "uploadReused": 1}]))

    def test_ranges_have_no_api_alignment_or_64k_assumption(self):
        self.assertEqual(probe.unsigned64("0x100000001"), 4294967297)
        self.assertEqual(probe.read_window(1 << 34, (1 << 33) + 3, 7, 16),
                         ((1 << 33) - 13, 39, 16))
        self.assertEqual(probe.read_window(10, 0, 3, 16), (0, 10, 0))
        self.assertEqual(probe.read_window(10, 8, 2, 16), (0, 10, 8))
        for offset, count in ((9, 2), (10, 1), (0, 0), (-1, 3), ((1 << 64) - 1, 2)):
            with self.assertRaises(ValueError):
                probe.read_window(10, offset, count, 16)
        for value in ("-1", str(1 << 64)):
            with self.assertRaises(ValueError):
                probe.unsigned64(value)
        self.assertEqual(probe.patch_bytes("01 ab 00", 3), b"\x01\xab\x00")
        for value in ("", "0", "gg", "01020304"):
            with self.assertRaises(ValueError):
                probe.patch_bytes(value, 3)

    def test_dependency_propagation_is_not_automatically_an_out_of_range_write(self):
        baseline = b"abcdefgh"
        changed = b"abXXYYgh"
        relaxed = probe.window_checks(changed, baseline, b"XX", 2)
        self.assertTrue(relaxed["patchMatches"])
        self.assertFalse(relaxed["neighborsEqualBaseline"])
        self.assertEqual(relaxed["issues"], [])
        self.assertTrue(probe.window_checks(changed, baseline, b"XX", 2, True)["issues"])
        self.assertTrue(probe.window_checks(baseline, baseline, b"XX", 2)["issues"])

    def test_comparison_requires_stable_full_pixels_and_real_buffer_bytes(self):
        a = {"buffer": {"patchSha256": "a"}, "output": {"pixels": ["color", "depth"]}}
        b = {"buffer": {"patchSha256": "b"}, "output": {"pixels": ["color", "depth"]}}
        wrong_depth = {"buffer": b["buffer"], "output": {"pixels": ["color", "wrongDepth"]}}
        self.assertEqual(probe.state_comparison([b], [b, b], a)["visibleEffect"], "noVisibleEffect")
        self.assertEqual(probe.state_comparison([b], [b, a], a)["status"], "inconclusiveCanonicalUnstable")
        self.assertEqual(probe.state_comparison([a], [b, b], a)["status"], "failedStableCanonicalMismatch")
        self.assertEqual(probe.state_comparison([wrong_depth], [b, b], a)["status"], "failedStableCanonicalMismatch")
        self.assertEqual(probe.state_comparison([b], [b, b], a, True)["status"], "failedOriginalStateNotRecovered")
        self.assertEqual(probe.roundtrip([10, 20]), [10, 20, 10])
        self.assertEqual(probe.roundtrip([10, 20, 30]), [10, 20, 30, 20, 10])

    def run_fake_probe(self, controller, extra=()):
        capture_file = SimpleNamespace(OpenFile=lambda *args: 0, DriverName=lambda: "D3D12",
                                       OpenCapture=lambda *args: (0, controller), Shutdown=lambda: None)
        rd = SimpleNamespace(InitialiseReplay=lambda *args: None, ShutdownReplay=lambda: None,
                             SetDebugLogFile=lambda path: setattr(controller, "native_log_path", Path(path)),
                             OpenCaptureFile=lambda: capture_file,
                             GlobalEnvironment=lambda: None, ReplayOptions=lambda: None,
                             ResultCode=SimpleNamespace(Succeeded=0), ResourceId=Resource,
                             ActionFlags=SimpleNamespace(Drawcall=1),
                             Subresource=lambda *args: args, ShaderStage=SimpleNamespace(Pixel=0))
        def remote_open(*args):
            controller.remote_mode = True
            return 0, controller
        def remote_close(value):
            self.assertIs(value, controller)
            controller.remote_closed = True
        remote = SimpleNamespace(LocalProxies=lambda: ["D3D11", "OpenGL"], OpenCapture=remote_open,
                                  CloseCapture=remote_close, ShutdownConnection=lambda: None)
        rd.CreateRemoteServerConnection = lambda host: (0, remote)
        rd.RemoteServer = SimpleNamespace(NoPreference=-1)
        with tempfile.TemporaryDirectory() as directory:
            capture, output = Path(directory) / "fixture.rdc", Path(directory) / "evidence.json"
            capture.write_bytes(b"CPU fake only; never opened by RenderDoc")
            arguments = ["--capture", str(capture), "--events", "10,20", "--buffer", "1",
                         "--offset", "4", "--data-hex", "aa bb cc dd", "--output", str(output)] + list(extra)
            with patch.dict(sys.modules, {"renderdoc": rd}), patch.dict(os.environ, {}, clear=False), \
                    patch.object(probe, "build_identity", return_value={"nativeModules": [{"sha256": "fake"}]}), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = probe.main(arguments)
            return result, json.loads(output.read_text(encoding="utf-8"))

    def test_full_schedule_requires_generation_roundtrips_clear_and_force_equivalence(self):
        controller = Controller()
        result, report = self.run_fake_probe(controller, ("--require-neighbors-unchanged",
                                                          "--require-identity-unchanged"))
        self.assertEqual(result, 0, report.get("error", report["issues"]))
        self.assertTrue(report["ok"])
        self.assertEqual(report["changedByteEvents"], [10, 20])
        self.assertEqual(report["visibleEffect"], "changedBoundOutputPixels")
        self.assertEqual(len(report["modeResults"]), 2)
        self.assertGreater(report["modeResults"][1]["observedNormalHits"], 0)
        self.assertTrue(all(row["status"] == "passed" for row in report["comparisons"]))
        self.assertTrue(all(len(v["witness"]["output"]["pixels"]) == 2 for v in report["visits"]))
        self.assertTrue(all(v["replayWallMs"] >= 0 and v["outputReadbackWallMs"] >= 0 for v in report["visits"]))
        self.assertIsNone(controller.patch)
        self.assertFalse(controller.enabled)
        self.assertTrue(controller.closed)
        # No native timing logs were emitted by a Python fake. They must remain unavailable.
        self.assertTrue(all(s["backendReplayTotalMs"] is None for s in report["samples"]))

    def test_no_visible_effect_is_not_reported_as_a_visible_slider_success(self):
        result, report = self.run_fake_probe(Controller(no_visible_effect=True))
        self.assertEqual(result, 0, report.get("error", report["issues"]))
        self.assertEqual(report["visibleEffect"], "noVisibleEffect")
        self.assertEqual(report["verdict"], "passedNativeBytesAndReferencesNoVisibleEffect")

    def test_native_normal_depth_mismatch_fails_even_with_nonempty_readbacks(self):
        controller = Controller(wrong_normal_pixels=True)
        result, report = self.run_fake_probe(controller)
        self.assertEqual(result, 1)
        self.assertFalse(report["ok"])
        self.assertTrue(any(c["status"] == "failedStableCanonicalMismatch" for c in report["comparisons"]))
        self.assertIsNone(controller.patch)
        self.assertTrue(controller.closed)

    def test_same_buffer_copy_propagation_is_optional_strict_fixture_failure(self):
        result, report = self.run_fake_probe(Controller(propagate=True))
        self.assertEqual(result, 0, report.get("error", report["issues"]))
        self.assertTrue(any(not v["windowChecks"]["neighborsEqualBaseline"] for v in report["visits"]
                            if "windowChecks" in v))
        result, report = self.run_fake_probe(Controller(propagate=True), ("--require-neighbors-unchanged",))
        self.assertEqual(result, 1)
        self.assertTrue(any("neighbor" in issue for issue in report["issues"]))

    def test_remote_controller_uses_remote_close_and_does_not_claim_unverified_server_build(self):
        controller = Controller()
        result, report = self.run_fake_probe(controller, ("--remote",))
        self.assertEqual(result, 0, report.get("error", report["issues"]))
        self.assertTrue(report["remoteReplay"])
        self.assertFalse(report["remoteBuildVerified"])
        self.assertFalse(report["sameBuildPerformanceComparisonEligible"])
        self.assertTrue(controller.remote_closed)
        self.assertFalse(controller.closed, "Remote controllers must use remote.CloseCapture")

    def test_server_identity_hashes_are_from_loaded_modules_and_must_match(self):
        rd = SimpleNamespace(__file__="binding.pyd")
        def identity(path):
            return {"path": path, "sha256": "a" if path in ("client.dll", "server.dll") else "b"}
        with patch.object(probe, "binary_identity", side_effect=identity), \
                patch.object(probe, "loaded_native_paths", side_effect=[["client.dll"], ["server.dll"]]):
            self.assertTrue(probe.build_identity(rd, 123)["sameNativeBuild"])
        with patch.object(probe, "binary_identity", side_effect=identity), \
                patch.object(probe, "loaded_native_paths", side_effect=[["client.dll"], ["old-server.dll"]]):
            with self.assertRaises(RuntimeError):
                probe.build_identity(rd, 123)

    def test_fixed_value_arguments_are_bounded_generic_bytes(self):
        self.assertEqual(probe.fixed_patch_values(None, 4, 1024), [])
        # No Float32 restriction: one-byte integer channels and seven-byte payloads are valid.
        self.assertEqual(probe.fixed_patch_values("00,7f,80,ff", 1, 7), [b"\0", b"\x7f", b"\x80", b"\xff"])
        self.assertEqual(len(probe.fixed_patch_values("00000000000000,01000000000000,02000000000000,03000000000000", 7, 7)), 4)
        for invalid in ("00,01,02", "00,01,02,02", "00,01,02,0102", "00,01,02,gg", "00,01,02,"):
            with self.assertRaises(ValueError):
                probe.fixed_patch_values(invalid, 1, 10)

    def test_fixed_setter_refresh_is_observed_before_any_reference_replay(self):
        controller = FixedController()
        result, report = self.run_fake_probe(controller, ("--fixed-values-hex", FIXED_VALUES, "--require-fast"))
        self.assertEqual(result, 0, report.get("error", report["issues"]))
        fixed = report["fixedValues"]
        self.assertEqual(len(fixed["rows"]), 4)
        self.assertEqual(fixed["steadyHitUpdates"], 2)
        self.assertTrue(fixed["fastVerified"])
        self.assertEqual(fixed["rows"][0]["checkpointEvidence"]["firstAffectedEvent"], 0)
        trace = controller.call_trace
        first_set = trace.index(("set", fixed["rows"][0]["valueHex"]))
        first_reference = next(i for i in range(first_set + 1, len(trace)) if trace[i][0] == "replay")
        live_sets = [item[1] for item in trace[first_set:first_reference] if item[0] == "set"]
        # The final entry reinstalls value 1 only after all four live witnesses were saved.
        self.assertEqual(live_sets, FIXED_VALUES.split(",") + [FIXED_VALUES.split(",")[0]])
        for row in fixed["rows"]:
            self.assertGreater(row["generationAfter"], row["generationBefore"])
            self.assertEqual(row["comparison"]["status"], "passed")
            self.assertEqual(len(row["immediateWitness"]["output"]["pixels"]), 2)
            self.assertGreaterEqual(row["setterWallMs"], 0)
            self.assertNotIn("replayWallMs", row, "The immediate setter witness must not invent a replay call")
            trace = controller.call_trace
            begin = trace.index(("set", row["valueHex"]))
            end = next(i for i in range(begin + 1, len(trace)) if trace[i][0] == "replay")
            self.assertIn(("buffer", 0, 24), trace[begin + 1:end])
            self.assertIn(("texture", 20), trace[begin + 1:end])
            self.assertIn(("texture", 21), trace[begin + 1:end])
            self.assertEqual(trace[end], ("replay", 20, True))
            for reference in row["forceReferenceVisits"]:
                visit = report["visits"][reference]
                self.assertEqual(visit["generation"], row["referenceGeneration"])
                self.assertGreater(row["referenceGeneration"], row["generationAfter"])
                self.assertFalse(probe.unchanged_checkpoint_issues(visit["before"], visit["afterReadback"]))
        self.assertEqual(fixed["clearComparison"]["status"], "passed")

    def test_correct_pixels_from_implicit_buffer_or_texture_repair_fail_before_references(self):
        for stage, record in (("buffer", {"api": "MaterializeEvent", "eid": 20}),
                              ("output", {"api": "D3D12", "type": "OnlyDraw"}),
                              ("output", {"api": "ReplayDriverBoundary", "type": "Full"}),
                              ("buffer", {"api": "ReplayChunk", "type": "WithoutDraw"})):
            with self.subTest(stage=stage, record=record):
                controller = RepairingFixedController(stage, record)
                result, report = self.run_fake_probe(controller, ("--fixed-values-hex", FIXED_VALUES))
                self.assertEqual(result, 1)
                self.assertTrue(controller.repaired)
                self.assertFalse(report["ok"])
                self.assertIn("implicit replay", report["error"])
                self.assertEqual(len(report["fixedValues"]["rows"]), 1)
                self.assertFalse(any(row["mode"] == "fixedValues" and row["force"]
                                     for row in report["visits"]))
                records = [row for sample in report["samples"] for row in sample.get("timingRecords", [])]
                self.assertTrue(probe.readback_replay_issues(records))

    def test_fixed_clear_immediate_output_cannot_hide_behind_successful_force_undo(self):
        class StaleClearController(FixedController):
            def ClearSharedBufferPatches(self):
                old, rendered = self.patch, dict(self.rendered)
                result = super().ClearSharedBufferPatches()
                if old and old[2].hex() in FIXED_VALUES.split(","):
                    self.rendered[self.depth.value] = rendered[self.depth.value]
                return result
        result, report = self.run_fake_probe(StaleClearController(), ("--fixed-values-hex", FIXED_VALUES))
        self.assertEqual(result, 1)
        self.assertEqual(report["fixedValues"]["clearComparison"]["status"], "failedStableCanonicalMismatch")
        self.assertFalse(report["fixedValues"]["fastVerified"])

    def test_fixed_wrong_pixels_are_not_repaired_by_good_force_references(self):
        result, report = self.run_fake_probe(FixedController(stale_pixels=True), ("--fixed-values-hex", FIXED_VALUES))
        self.assertEqual(result, 1)
        self.assertTrue(all(row["windowChecks"]["patchMatches"] for row in report["fixedValues"]["rows"]))
        self.assertTrue(any(row["comparison"]["status"] == "failedStableCanonicalMismatch"
                            for row in report["fixedValues"]["rows"]))

    def test_fixed_stale_eid_is_rejected_before_a_force_can_hide_it(self):
        result, report = self.run_fake_probe(FixedController(stale_eid=True), ("--fixed-values-hex", FIXED_VALUES))
        self.assertEqual(result, 1)
        self.assertIn("stale current EID", report["error"])
        self.assertEqual(len(report["fixedValues"]["rows"]), 1)

    def test_fixed_fallback_is_reported_and_strict_fast_requires_two_steady_hits(self):
        result, report = self.run_fake_probe(FixedController(no_hits=True), ("--fixed-values-hex", FIXED_VALUES))
        self.assertEqual(result, 0, report.get("error", report["issues"]))
        self.assertFalse(report["fixedValues"]["fastVerified"])
        result, report = self.run_fake_probe(FixedController(no_hits=True),
                                              ("--fixed-values-hex", FIXED_VALUES, "--require-fast"))
        self.assertEqual(result, 1)
        self.assertIn("fewer than two", " ".join(report["issues"]))

    def test_fixed_force_cannot_silently_change_miss_counters(self):
        result, report = self.run_fake_probe(FixedController(force_changes_counter=True),
                                              ("--fixed-values-hex", FIXED_VALUES))
        self.assertEqual(result, 1)
        self.assertIn("checkpoint state changed: misses", " ".join(report["issues"]))

    def test_initial_checkpoint_needs_explicit_native_storage_and_hit_evidence(self):
        before = {"hits": 3, "events": [0], "hasInitialCheckpoint": True}
        after = dict(before, hits=4, lastResumeEvent=0, sharedBufferPatchGeneration=8,
                     sharedBufferPatchFirstAffectedEvent=10)
        evidence = probe.fixed_checkpoint_evidence(before, after, 8)
        self.assertEqual(evidence["beforeCutoffEventsStillPresent"], [0])
        self.assertEqual(evidence["setterResumeKind"], "FrameInitial")
        self.assertTrue(evidence["setterResumeBeforeCutoff"])
        self.assertFalse(probe.status_issues(dict(allocatedBytes=64, byteBudget=128,
                         checkpointCount=1, maxCheckpoints=3, **after)))
        after["hasInitialCheckpoint"] = False
        evidence = probe.fixed_checkpoint_evidence(before, after, 8)
        self.assertFalse(evidence["setterResumeBeforeCutoff"])
        self.assertEqual(evidence["beforeCutoffEventsStillPresent"], [])
        self.assertTrue(probe.status_issues(dict(allocatedBytes=64, byteBudget=128,
                        checkpointCount=1, maxCheckpoints=3, **after)))
        after["hasInitialCheckpoint"], after["hits"] = True, 3
        self.assertIsNone(probe.fixed_checkpoint_evidence(before, after, 8)["setterResumeKind"])

    def test_retained_event_evidence_excludes_the_cutoff_and_does_not_claim_version_identity(self):
        before = {"hits": 3, "events": [5, 10, 15]}
        after = {"hits": 4, "events": [5, 10, 15], "lastResumeEvent": 5,
                 "sharedBufferPatchGeneration": 8, "sharedBufferPatchFirstAffectedEvent": 10}
        evidence = probe.fixed_checkpoint_evidence(before, after, 8)
        self.assertEqual(evidence["beforeCutoffEventsStillPresent"], [5])
        self.assertEqual(evidence["afterEventsAtOrAfterCutoff"], [10, 15])
        self.assertFalse(evidence["retainedVersionIdentityProven"])
        self.assertTrue(evidence["setterResumeBeforeCutoff"])
        bad = dict(evidence, setterResumeBeforeCutoff=False)
        self.assertTrue(probe.fixed_fast_issues([{}, {}, {"checkpointEvidence": bad}, {"checkpointEvidence": bad}]))
        with self.assertRaises(RuntimeError):
            probe.fixed_checkpoint_evidence(before, after, 9)


if __name__ == "__main__":
    unittest.main()
