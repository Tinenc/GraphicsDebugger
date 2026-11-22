"""CPU-only native-evidence and immediate-readback guards; no RenderDoc import or GPU."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import eid_buffer_alias_checkpoint_profile as probe


def status(**changes):
    value = dict(schema="renderdoc-replay-checkpoints/v1", byteBudget=4096, maxCheckpoints=3,
                 checkpointCount=1, allocatedBytes=512, sharedBytes=64, builds=1, hits=0,
                 misses=0, invalidations=0, rejected=0, enabled=True, events=[10])
    value.update(changes)
    return value


def restore(**changes):
    value = dict(api="D3D12Checkpoint", op="restore", event=10, target=39, prefixSkipped=1,
                 bufferAliasComponents=2, bufferAliasCopyBytes=256)
    value.update(changes)
    return value


def canonical(event=40, buffer_hash="bufferA", pixel_hash="pixelA"):
    return {"buffers": [{"resource": "ResourceId::1", "bytes": 8, "sha256": buffer_hash}],
            "output": {"eventId": event, "pixels": [{"resource": "ResourceId::8", "bytes": 4,
                                                     "sha256": pixel_hash}]}}


class Recorder:
    def __init__(self, records=None):
        self.records = records or []
        self.calls = []

    def measure(self, label, callback, event):
        self.calls.append((label, event))
        return callback(), {"wallMs": 1, "timingRecords": self.records}


class Controller:
    def __init__(self):
        self.calls = []
        self.stats = status()
        self.truncated = False
        self.mutate_status = False
        self.event = 40

    def GetReplayCheckpointStatus(self):
        return json.dumps(self.stats)

    def GetCurrentEventID(self):
        return self.event

    def GetBufferData(self, resource, offset, count):
        self.calls.append((resource, offset, count))
        if self.mutate_status:
            self.stats["hits"] += 1
        return bytes(range(count - int(self.truncated)))


class BufferAliasCheckpointTests(unittest.TestCase):
    def test_discovery_requires_four_exact_distinct_direct_draws(self):
        self.assertEqual(probe.select_draws([40, 10, 30, 20], ""), [10, 20, 30, 40])
        self.assertEqual(probe.select_draws([10, 20, 30, 40, 50], "10,20,30,50"), [10, 20, 30, 50])
        for available, requested in (([10, 20, 30], ""), ([10, 20, 30, 40, 50], ""),
                                      ([10, 20, 30, 40], "10,20,30,41"),
                                      ([10, 20, 30, 40], "10,30,20,40"),
                                      ([10, 20, 30, 40], "10,20,20,40")):
            with self.assertRaises(ValueError):
                probe.select_draws(available, requested)

    def test_names_resolve_native_ids_and_complete_lengths(self):
        resources = [SimpleNamespace(name=n, resourceId=i + 71) for i, n in enumerate(probe.DEFAULT_NAMES)]
        buffers = [SimpleNamespace(resourceId=i + 71, length=64) for i in range(4)]
        result, total = probe.resolve_buffers(probe.DEFAULT_NAMES, resources, buffers, 256)
        self.assertEqual(total, 256)
        self.assertEqual([r[1] for r in result], [71, 72, 73, 74])
        for names, res, bufs, budget in ((probe.DEFAULT_NAMES, resources + resources[:1], buffers, 256),
                                         (probe.DEFAULT_NAMES, resources[:-1], buffers, 256),
                                         (probe.DEFAULT_NAMES, resources, buffers[:-1], 256),
                                         (probe.DEFAULT_NAMES, resources, buffers, 255),
                                         (probe.DEFAULT_NAMES[:3], resources, buffers, 256),
                                         (probe.DEFAULT_NAMES[:3] + [""], resources, buffers, 256)):
            with self.assertRaises(ValueError):
                probe.resolve_buffers(names, res, bufs, budget)

    def test_duplicate_native_identity_is_not_two_aliases(self):
        resources = [SimpleNamespace(name=n, resourceId=71) for n in probe.DEFAULT_NAMES]
        with self.assertRaises(ValueError):
            probe.resolve_buffers(probe.DEFAULT_NAMES, resources,
                                  [SimpleNamespace(resourceId=71, length=64)], 256)

    def test_native_restore_requires_exact_request_and_copy(self):
        result = probe.alias_restore_evidence([restore()], 40, status(), status(hits=1))
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["copyBytes"], 256)
        for changes in (dict(target=40), dict(event=11), dict(prefixSkipped=0),
                        dict(bufferAliasComponents=None), dict(bufferAliasCopyBytes=None),
                        dict(bufferAliasComponents=0), dict(bufferAliasCopyBytes=0),
                        dict(bufferAliasCopyBytes=-1), dict(bufferAliasCopyBytes=True)):
            self.assertTrue(probe.alias_restore_evidence([restore(**changes)], 40, status(),
                                                         status(hits=1))["issues"])

    def test_hit_counter_alone_cannot_pass(self):
        self.assertTrue(probe.alias_restore_evidence([], 40, status(), status(hits=1))["issues"])
        self.assertTrue(probe.alias_restore_evidence([restore()], 40, status(), status())["issues"])

    def test_disabled_alias_control_rejects_physical_copy(self):
        self.assertTrue(probe.alias_restore_evidence([restore()], 40, status(), status(hits=1),
                                                     disabled=True)["issues"])
        result = probe.alias_restore_evidence([restore(bufferAliasComponents=0, bufferAliasCopyBytes=0)],
                                              40, status(), status(hits=1), disabled=True)
        self.assertEqual(result["issues"], [])

    def test_forced_reference_requires_native_initial_replay_and_no_hit(self):
        initial = dict(api="D3D12", type="WithoutDraw", range="1->40", partial=0, replay_depth=0,
                       **{"section.applyInitial_calls": 1})
        self.assertEqual(probe.alias_restore_evidence([initial], 40, status(), status(),
                                                      force=True)["issues"], [])
        self.assertTrue(probe.alias_restore_evidence([], 40, status(), status(), force=True)["issues"])
        self.assertTrue(probe.alias_restore_evidence([initial, restore()], 40, status(), status(hits=1),
                                                     force=True)["issues"])
        for changes in (dict(partial=1), {"section.applyInitial_calls": 0}, dict(range="1->39"),
                        dict(replay_depth=1)):
            wrong = dict(initial, **changes)
            self.assertTrue(probe.alias_restore_evidence([wrong], 40, status(), status(), force=True)["issues"])

    def test_buffer_mismatch_fails_even_when_all_output_pixels_equal(self):
        good, bad = canonical(), canonical(buffer_hash="wrong")
        visits = [dict(eventId=40, force=False, witness=bad)]
        rows, issues = probe.comparisons(visits, {40: [good, good]}, [40])
        self.assertEqual(rows[0]["status"], "failedStableCanonicalMismatch")
        self.assertTrue(issues)

    def test_stale_output_binding_and_unstable_reference_do_not_pass(self):
        good, bad = canonical(), canonical(pixel_hash="stale")
        rows, issues = probe.comparisons([dict(eventId=40, force=False, witness=bad)],
                                         {40: [good, good]}, [40])
        self.assertTrue(issues)
        self.assertEqual(rows[0]["status"], "failedStableCanonicalMismatch")
        self.assertTrue(probe.comparisons([dict(eventId=40, force=False, witness=good)],
                                         {40: [good, bad]}, [40])[1])
        self.assertTrue(probe.comparisons([], {40: [good, good]}, [40])[1])

    def test_content_variation_rejects_static_picture_fixture(self):
        a, b = canonical(10), canonical(20, "bufferB", "pixelB")
        evidence, issues = probe.changing_content_evidence({10: [a, a], 20: [b, b]}, [10, 20])
        self.assertEqual(issues, [])
        self.assertEqual(evidence["distinctBufferStates"], 2)
        for b in (canonical(20), canonical(20, "bufferB"), canonical(20, pixel_hash="pixelB")):
            self.assertTrue(probe.changing_content_evidence({10: [a, a], 20: [b, b]}, [10, 20])[1])

    def make_probe(self, records=None):
        controller, recorder, report = Controller(), Recorder(records), {"issues": []}
        run = probe.AliasProbe(controller, None, recorder, report,
                               [("alias%d" % i, i + 1, 8) for i in range(4)], [], [8], 128, False)
        return run, controller, recorder

    def test_immediate_witness_reads_all_bytes_without_switch_or_force(self):
        run, controller, recorder = self.make_probe()
        with patch.object(probe, "texture_witness", return_value=canonical()["output"]) as output:
            witness, _ = run.read("ordinary", 40)
        self.assertEqual(controller.calls, [(1, 0, 8), (2, 0, 8), (3, 0, 8), (4, 0, 8)])
        self.assertEqual(len(witness["buffers"]), 4)
        self.assertEqual(output.call_args[0][-1], 96)
        self.assertEqual(recorder.calls, [("ordinary.readback", 40)])

    def test_truncated_or_overbudget_buffer_readback_fails(self):
        run, controller, _ = self.make_probe()
        controller.truncated = True
        with self.assertRaises(RuntimeError):
            run.read("bad", 40)
        controller.truncated, run.maximum = False, 31
        with self.assertRaises(RuntimeError):
            run.read("bad", 40)

    def test_implicit_replay_repair_and_stale_eid_fail(self):
        run, controller, _ = self.make_probe([dict(api="D3D12", type="OnlyDraw")])
        with patch.object(probe, "texture_witness", return_value=canonical()["output"]):
            with self.assertRaisesRegex(RuntimeError, "implicit replay"):
                run.read("bad", 40)
        run, controller, _ = self.make_probe()
        controller.event = 39
        with patch.object(probe, "texture_witness", return_value=canonical()["output"]):
            with self.assertRaisesRegex(RuntimeError, "stale EID"):
                run.read("bad", 40)

    def test_readback_mutation_and_budget_release_are_rejected(self):
        run, controller, _ = self.make_probe()
        controller.mutate_status = True
        with patch.object(probe, "texture_witness", return_value=canonical()["output"]):
            with self.assertRaisesRegex(RuntimeError, "checkpoint state changed"):
                run.read("bad", 40)
        controller.stats = status(allocatedBytes=4097)
        run.status()
        self.assertTrue(run.report["issues"])
        run.report["issues"] = []
        controller.stats = status(enabled=False)
        run.status(disabled=True)
        self.assertTrue(run.report["issues"])
        run.report["issues"] = []
        controller.stats = status(enabled=False, checkpointCount=0, allocatedBytes=0, sharedBytes=0, events=[])
        run.status(disabled=True)
        self.assertEqual(run.report["issues"], [])


if __name__ == "__main__":
    unittest.main()
