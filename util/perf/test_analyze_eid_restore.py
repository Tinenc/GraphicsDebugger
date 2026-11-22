"""CPU-only proof of restore-accounting boundaries and missing-evidence handling."""
from pathlib import Path
import unittest
from unittest.mock import patch, MagicMock

from analyze_eid_restore import (Reader, analyze_profile, load_inventory, metadata_for,
                                 parse_restore_records, scope_measurements, SOURCE_FIELDS)


def line(actual=64, skipped=32, whole=0, detail=" ResourceId::7:64/32/0"):
    return ("api=D3D12DirtyRestore enabled=1 shadow=0 actualCopyBytes=%d skippedStaticBytes=%d "
            "wholeResourceCopies=%d dirtyResources=1 dirtyHeaps=1 reason=submittedWrites "
            "resourceActualSkippedWhole=%s" % (actual, skipped, whole, detail))


class RestoreAttributionTests(unittest.TestCase):
    def test_v2_bounded_resources_validate_omission_and_real_source_copy_bytes(self):
        fields = dict.fromkeys(SOURCE_FIELDS, 0)
        fields["gpuUploadBufferBytes"] = 64
        fields["gpuDefaultFootprintBytes"] = 32
        header = " ".join("%s=%s" % (key, value) for key, value in fields.items())
        suffix = ("restoreTimingVersion=2 resourceDetailCount=1 omittedResourceCount=1 "
                  "omittedActualCopyBytes=32 omittedSkippedStaticBytes=0 omittedWholeResourceCopies=0 " +
                  header + " resourceSourceCopies=ResourceId::7:0/64/0/0/0/0/0/0/0/0 ")
        payload = line(actual=96).replace("resourceActualSkippedWhole=", suffix + "resourceActualSkippedWhole=")
        rows, issues, _ = parse_restore_records(payload)
        self.assertEqual(issues, [])
        self.assertEqual(rows[0]["resourceDetailStatus"], "bounded")
        self.assertEqual(rows[0]["sourceDetailStatus"], "bounded")
        self.assertEqual(rows[0]["gpuDefaultFootprintBytes"], 32)
        self.assertEqual(rows[0]["resources"][0]["sourceCopies"]["gpuUploadBufferBytes"], 64)
        bad = payload.replace("gpuUploadBufferBytes=64", "gpuUploadBufferBytes=65")
        rows, issues, _ = parse_restore_records(bad)
        self.assertTrue(issues)
        self.assertEqual(rows[0]["sourceDetailStatus"], "incomplete")

    def test_legacy_source_copy_bytes_stay_unknown(self):
        rows, _, _ = parse_restore_records(line())
        self.assertIsNone(rows[0]["gpuUploadBufferBytes"])
        self.assertEqual(rows[0]["sourceDetailStatus"], "unavailable")

    def test_dual_sink_mirror_is_removed_but_repeated_real_calls_are_retained(self):
        payload = line()
        text = "\n".join(["RDOC 123: Log - " + payload, payload,
                          "RDOC 123: Log - " + payload, payload])
        rows, issues, removed = parse_restore_records(text)
        self.assertEqual((len(rows), removed, issues), (2, 2, []))
        self.assertEqual(rows[0]["resources"][0]["actualCopyBytes"], 64)
        rows, issues, removed = parse_restore_records(payload + "\n" + payload)
        self.assertEqual((len(rows), removed), (2, 0))

    def test_detail_sums_must_match_and_truncation_does_not_invent_resources(self):
        for payload in (line(actual=65), line(detail=" ResourceId::7:64/32/0 ..."),
                        line(actual=128, skipped=64, detail=" ResourceId::7:64/32/0 ResourceId::7:64/32/0")):
            rows, issues, _ = parse_restore_records(payload)
            self.assertEqual(rows[0]["resourceDetailStatus"], "incomplete")
            self.assertTrue(issues)

    def test_aggregate_without_resource_details_stays_unavailable(self):
        rows, issues, _ = parse_restore_records(line().split("resourceActualSkippedWhole=")[0])
        self.assertEqual(rows[0]["actualCopyBytes"], 64)
        self.assertEqual(rows[0]["resourceDetailStatus"], "unavailable")
        self.assertEqual(rows[0]["resources"], [])
        self.assertEqual(issues, [])

    def test_cache_source_bytes_are_not_copy_bytes_and_nested_scopes_are_separate(self):
        records = [{"api": "D3D12", "type": "WithoutDraw", "replay_depth": 0, "timing_version": 2,
                    "total_ms": 20, "applyInitial_ms": 4, "count.initialUploadCacheBytes": 4096,
                    "count.initialGPUCopyBufferBytes": 64},
                   {"api": "D3D12", "type": "WithoutDraw", "replay_depth": 1, "timing_version": 2,
                    "total_ms": 5, "count.initialGPUCopyBufferBytes": 8}]
        scopes = scope_measurements(records)
        root = next(s for s in scopes if s["replayDepth"] == 0)["metricsPerCall"]
        self.assertEqual(len(scopes), 2)
        self.assertEqual(root["count.initialUploadCacheBytes"]["mean"], 4096)
        self.assertEqual(root["count.initialGPUCopyBufferBytes"]["mean"], 64)
        self.assertEqual(root["count.initialUploadCacheFallbackBytes"]["mean"], 0)
        self.assertEqual(root["total_ms"]["mean"], 20)
        legacy = scope_measurements([{"api": "D3D12", "type": "WithoutDraw", "total_ms": 7}])[0]
        self.assertIsNone(legacy["metricsPerCall"]["count.initialUploadCacheBytes"])

    def test_exact_usage_window_only_and_metadata_size_never_becomes_measured_copy(self):
        entry = {"logicalBytes": 4096, "kind": "buffer", "usageWindows": [
            {"afterEventId": 10, "throughEventId": 20, "noReportedUse": True, "safeToSkipRestore": False}]}
        self.assertIsNotNone(metadata_for(entry, 10, 20)["window"])
        self.assertIsNone(metadata_for(entry, 0, 20)["window"])
        self.assertEqual(metadata_for(entry, 10, 20)["logicalBytes"], 4096)
        self.assertNotIn("actualCopyBytes", metadata_for(entry, 10, 20))

    def test_same_eid_forced_and_steady_visits_never_merge(self):
        source = {"schema": "renderdoc-checkpoint-profile/v1", "capture": "test.rdc", "visits": [], "samples": []}
        for phase, forced in (("steadyEnabled", False), ("canonical", True)):
            source["visits"].append({"phase": phase, "eventId": 20, "force": forced,
                "replayWallMs": 10 if not forced else 100, "before": {"hits": 1},
                "after": {"hits": 2 if not forced else 1, "enabled": True}})
            source["samples"].append({"phase": phase + ".replay", "eventId": 20, "status": "complete",
                "timingRecords": [{"api": "D3D12DirtyRestore", "actualCopyBytes": 64}]})
        result = analyze_profile(source, Path("test.json"), Reader())
        self.assertEqual(len(result["groups"]), 2)
        forced = next(g for g in result["groups"] if g["force"])
        steady = next(g for g in result["groups"] if not g["force"])
        self.assertEqual(forced["category"], "forcedReference")
        self.assertEqual(steady["replayWallMs"]["p50"], 10)
        self.assertEqual(steady["resourceDetailStatus"], "unavailableOrIncomplete")
        self.assertEqual(steady["largestCopiedResources"], [])

    def test_input_limits_are_checked_before_open(self):
        reader = Reader(file_limit=4)
        with patch.object(Path, "stat") as stat, patch.object(Path, "open") as opened:
            stat.return_value.st_size = 5
            with self.assertRaises(ValueError):
                reader.read(Path("anything"))
            opened.assert_not_called()

    def test_inventory_rejects_duplicate_resource_identity(self):
        reader = MagicMock()
        reader.json.side_effect = [{"schema": "renderdoc-checkpoint-inventory/v1", "capture": "a.rdc",
                                   "resourceInventoryFile": "a.resources.json"},
                                  [{"resource": "ResourceId::7"}, {"resource": "ResourceId::7"}]]
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            load_inventory(Path("a.json"), reader)


if __name__ == "__main__":
    unittest.main()
