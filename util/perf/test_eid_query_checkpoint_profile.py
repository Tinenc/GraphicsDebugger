"""CPU-only tests: structured fixture metadata and every RenderDoc call are Python fakes."""
import contextlib
import io
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import eid_query_checkpoint_profile as probe
from test_eid_shared_buffer_profile import Controller, Resource


FLAGS = SimpleNamespace(Drawcall=1, CommandBufferBoundary=2, BeginPass=4, EndPass=8,
                        SetMarker=16, Clear=32)


class QueryController(Controller):
    def __init__(self, repeated=False, fallback=False, stale_query=False, wrong_depth=False,
                 force_mutates=False, missing_slot_proof=False):
        super().__init__()
        self.repeated, self.fallback = repeated, fallback
        self.stale_query, self.wrong_depth = stale_query, wrong_depth
        self.force_mutates, self.missing_slot_proof = force_mutates, missing_slot_proof
        self.buffer = Resource(177)
        self.log = None
        self.chunks, self.actions = [], []
        self.draws = [113, 156, 199, 242]
        self.first_resolve = 196
        variant = "repeated-submit" if repeated else "mixed-prefix-suffix"
        marker_groups = [["variant " + variant, "prefix slot 0 = 1"], ["gap draw"],
                         ["repeated mixed resolve" if repeated else "mixed resolve expected 1,0",
                          "inspect resolved buffer"],
                         ["repeated mixed resolve", "inspect resolved buffer"] if repeated else
                         ["overwrite native slot 0 = 0", "final draw expected 1,0"]]
        previous_draw_chunk = None
        for group, event in enumerate(self.draws):
            self.chunk("ID3D12CommandQueue::ExecuteCommandLists")
            self.action(event - 13, "=> ExecuteCommandLists(1)[0]: Reset(ResourceId::777)", 2 | 4)
            for offset, name in enumerate(marker_groups[group]):
                self.action(event - 10 + offset, probe.PREFIX + name, 16, "SetMarker")
            if group == 2 or (repeated and group == 3):
                self.action(event - 3, "ResolveQueryData", 0, "ResolveQueryData")
            chunk = previous_draw_chunk if repeated and group == 3 else self.chunk(
                "ID3D12GraphicsCommandList::DrawInstanced")
            self.action(event, "DrawInstanced", 1, chunk=chunk)
            previous_draw_chunk = chunk
            self.action(event + 4, "=> ExecuteCommandLists(1)[0]: Close(ResourceId::777)", 2 | 8)

    def chunk(self, name):
        index = len(self.chunks)
        self.chunks.append(SimpleNamespace(name=name, metadata=SimpleNamespace(chunkID=1000 + index)))
        return index

    def action(self, event, name, flags, api=None, chunk=None):
        if api:
            chunk = self.chunk("ID3D12GraphicsCommandList::" + api)
        events = [] if chunk is None else [SimpleNamespace(eventId=event, chunkIndex=chunk,
                                                          fileOffset=10000 + chunk * 64)]
        self.actions.append(SimpleNamespace(eventId=event, flags=flags, children=[], events=events,
                                            outputs=[], GetName=lambda structured, n=name: n))

    def GetRootActions(self):
        return self.actions

    def GetStructuredFile(self):
        return SimpleNamespace(chunks=self.chunks)

    def GetResources(self):
        return [SimpleNamespace(name=probe.RESULT_NAME, resourceId=self.buffer)]

    def SetFrameEvent(self, event, force):
        self.event, self.force = event, force
        if force:
            if self.force_mutates:
                self.stats["misses"] += 1
            return
        if not self.enabled:
            return
        post_resolve = event in self.draws[2:]
        if self.fallback and post_resolve:
            self.stats["misses"] += 1
            self.stats["reason"] = "Target crosses repeated query resolve dependency"
            detail = "op=queryFallback event=117 target=%d firstResolve=%d queryType=BINARY_OCCLUSION" % (
                event - 1, self.first_resolve)
        elif self.stats["checkpointCount"] and event > 117:
            self.stats["hits"] += 1
            self.stats["lastResumeEvent"] = 117
            detail = "op=restore event=117 target=%d restoredQuerySlots=%d total_ms=0.001" % (
                event - 1, 0 if self.missing_slot_proof else 1)
        elif event > self.draws[0]:
            self.stats.update(checkpointCount=1, allocatedBytes=64, sharedBytes=64, events=[117])
            self.stats["builds"] += 1
            detail = "op=save event=117 total_ms=0.001"
        else:
            self.stats["misses"] += 1
            detail = "op=fallback event=0"
        if self.log:
            with self.log.open("a", encoding="utf-8") as stream:
                stream.write("[EIDTiming] api=D3D12Checkpoint " + detail + "\n")

    def GetBufferData(self, resource, offset, count):
        if resource != self.buffer:
            raise ValueError("Profiler guessed the wrong captured resource ID")
        raw = bytearray(64)
        for position, value in probe.SENTINELS.items():
            struct.pack_into("<Q", raw, position, value)
        first = 1 if self.event in self.draws[2:] else 0
        if self.repeated and self.event == self.draws[3]:
            first = 0
            if self.stale_query and self.enabled and not self.force:
                first = 1
        struct.pack_into("<QQ", raw, 16, first, 0)
        return bytes(raw[offset:offset + count])

    def GetTextureData(self, resource, sub):
        result = struct.pack("<II", resource.value, self.event) + self.GetBufferData(self.buffer, 16, 16)
        if self.wrong_depth and self.enabled and not self.force and resource == self.depth:
            result += b"wrong depth, even though Buffer and color agree"
        return result


class QueryCheckpointTests(unittest.TestCase):
    def discovery(self, controller):
        rows, _ = probe.collect_actions(controller, SimpleNamespace(ActionFlags=FLAGS))
        return probe.discover_query_fixture(rows, probe.query_api_events(controller))

    def test_actual_eids_and_repeated_chunk_identity_come_from_metadata(self):
        for repeat in (False, True):
            controller = QueryController(repeated=repeat)
            discovery = self.discovery(controller)
            self.assertEqual([row["eventId"] for row in discovery["targets"]], controller.draws)
            self.assertEqual(discovery["targets"][3]["expectedQuery"], [0, 0] if repeat else [1, 0])
            self.assertIsNone(discovery["targets"][0]["expectedQuery"])
            self.assertEqual(discovery["firstResolveEvent"], 196)
        # Matching text cannot masquerade as execution of the unchanged recorded list.
        controller.actions[-2].events[0].fileOffset += 1
        with self.assertRaisesRegex(ValueError, "same recorded draw"):
            self.discovery(controller)

    def test_markers_without_resolve_api_evidence_are_rejected(self):
        controller = QueryController()
        for chunk in controller.chunks:
            if chunk.name.endswith("ResolveQueryData"):
                chunk.name = "ID3D12GraphicsCommandList::ResourceBarrier"
        with self.assertRaisesRegex(ValueError, "ResolveQueryData API evidence"):
            self.discovery(controller)
        controller = QueryController()
        controller.actions[1].GetName = lambda structured: "Unrelated marker"
        with self.assertRaisesRegex(ValueError, "variant marker"):
            self.discovery(controller)

    def test_uint64_high_words_and_all_neighbor_sentinels_are_checked(self):
        controller = QueryController()
        controller.event = controller.draws[2]
        raw = bytearray(controller.GetBufferData(controller.buffer, 0, 64))
        self.assertFalse(probe.result_witness(raw, [1, 0])["issues"])
        struct.pack_into("<Q", raw, 16, 0x100000001)
        self.assertIn("UINT64", " ".join(probe.result_witness(raw, [1, 0])["issues"]))
        raw[63] ^= 1
        self.assertIn("sentinel", " ".join(probe.result_witness(raw, None)["issues"]))
        with self.assertRaisesRegex(ValueError, "complete 64-byte"):
            probe.result_witness(raw[:32], [1, 0])

    def run_fake(self, controller, extra=()):
        capture_file = SimpleNamespace(OpenFile=lambda *args: 0, DriverName=lambda: "D3D12",
                                       OpenCapture=lambda *args: (0, controller), Shutdown=lambda: None)
        rd = SimpleNamespace(InitialiseReplay=lambda *args: None, ShutdownReplay=lambda: None,
                             SetDebugLogFile=lambda path: setattr(controller, "log", Path(path)),
                             OpenCaptureFile=lambda: capture_file, GlobalEnvironment=lambda: None,
                             ReplayOptions=lambda: None, ResultCode=SimpleNamespace(Succeeded=0),
                             ResourceId=Resource, ActionFlags=FLAGS, Subresource=lambda *args: args,
                             ShaderStage=SimpleNamespace(Vertex=0, Pixel=1))
        with tempfile.TemporaryDirectory() as directory:
            capture, output = Path(directory) / "fixture.rdc", Path(directory) / "report.json"
            capture.write_bytes(b"CPU fake only; never opened by native RenderDoc")
            with patch.dict(sys.modules, {"renderdoc": rd}), patch.dict(os.environ, {}, clear=False), \
                    patch.object(probe, "build_identity", return_value={"nativeModules": [{"sha256": "fake"}]}), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = probe.main(["--capture", str(capture), "--output", str(output), "--rounds", "1"] + list(extra))
            return result, json.loads(output.read_text(encoding="utf-8"))

    def test_completed_mixed_query_requires_saved_slot_restore_and_exact_output_parity(self):
        controller = QueryController()
        result, report = self.run_fake(controller)
        self.assertEqual(result, 0, report.get("error", report["issues"]))
        self.assertEqual(report["resultBuffer"]["resource"], "ResourceId::177")
        self.assertGreater(report["coverage"]["querySlotRestoreVisits"], 0)
        self.assertTrue(all(row["status"] == "passed" for row in report["comparisons"]))
        self.assertTrue(all(len(row["witness"]["output"]["pixels"]) == 2 for row in report["visits"]))
        self.assertTrue(all(row["hits"] == row["builds"] == 0 for row in report["visits"] if row["force"]))
        self.assertTrue(all(sample["backendReplayTotalMs"] is None for sample in report["samples"]))
        self.assertTrue(controller.closed)
        self.assertFalse(controller.enabled)

    def test_repeat_explicit_fallback_preserves_different_per_eid_results(self):
        controller = QueryController(repeated=True, fallback=True)
        result, report = self.run_fake(controller)
        self.assertEqual(result, 0, report.get("error", report["issues"]))
        self.assertEqual(report["coverage"]["coverage"], "explicitQuerySafetyFallback")
        final = [v for v in report["visits"] if v["eventId"] == controller.draws[3]]
        self.assertTrue(all(v["witness"]["query"]["queryValues"] == [0, 0] for v in final))
        result, report = self.run_fake(QueryController(repeated=True, fallback=True), ("--min-hits", "1"))
        self.assertEqual(result, 1)
        self.assertIn("required post-resolve", " ".join(report["issues"]))

    def test_repeat_stale_native_query_is_not_hidden_by_successful_force(self):
        result, report = self.run_fake(QueryController(repeated=True, stale_query=True))
        self.assertEqual(result, 1)
        self.assertIn("UINT64", " ".join(report["issues"]))
        self.assertTrue(any(row["status"] == "failedStableCanonicalMismatch" for row in report["comparisons"]))

    def test_wrong_depth_fails_even_when_native_query_and_color_match(self):
        result, report = self.run_fake(QueryController(wrong_depth=True))
        self.assertEqual(result, 1)
        self.assertTrue(all(not row["witness"]["query"]["issues"] for row in report["visits"]))
        self.assertIn("failedStableCanonicalMismatch", " ".join(report["issues"]))

    def test_force_mutation_and_unproven_query_slot_reuse_are_failures(self):
        controller = QueryController(force_mutates=True)
        result, report = self.run_fake(controller)
        self.assertEqual(result, 1)
        self.assertIn("checkpoint state changed: misses", " ".join(report["issues"]))
        self.assertTrue(controller.closed)
        result, report = self.run_fake(QueryController(missing_slot_proof=True))
        self.assertEqual(result, 1)
        self.assertGreater(report["coverage"]["postResolveHits"], 0)
        self.assertIn("saved query-slot restore", " ".join(report["issues"]))


if __name__ == "__main__":
    unittest.main()
