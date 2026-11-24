"""CPU-only classification and inclusive-scope accounting checks."""
from pathlib import Path
import unittest

from analyze_eid_checkpoints import analyze_report, classify_visit, summarize_scope_records


class CheckpointAnalysisTests(unittest.TestCase):
    def test_restore_and_build_is_separate_and_force_wins(self):
        visit = dict(phase="forward", before=dict(hits=2, builds=1), after=dict(hits=3, builds=2))
        self.assertEqual(classify_visit(visit)[1:], ("restoreAndBuild", 1, 1))
        visit["force"] = True
        self.assertEqual(classify_visit(visit)[1], "forcedReference")
        visit.update(force=False, phase="tinyBudget")
        self.assertEqual(classify_visit(visit)[0], "budget")
        visit["phase"] = "reenabled"
        self.assertEqual(classify_visit(visit)[0], "reenable")

    def test_nested_backend_and_pipeline_are_not_added(self):
        rows = [dict(api="Vulkan", type="Full", total_ms=10, replay_depth=0),
                dict(api="Vulkan", type="OnlyDraw", total_ms=3, replay_depth=1),
                dict(api="PipelineFetch", total_ms=5, backend_state_ms=1,
                     descriptor_access_ms=3, descriptor_queries_ms=0.5, publish_ms=0.5)]
        result = summarize_scope_records(rows)
        self.assertEqual(result["topLevelBackendSumMs"], 10)
        self.assertEqual(result["pipeline"]["metricsPerCall"]["total_ms"]["mean"], 5)
        del rows[0]["replay_depth"]
        self.assertIsNone(summarize_scope_records(rows)["topLevelBackendSumMs"])

    def test_repeated_visits_match_distinct_measured_slices(self):
        visit = dict(phase="canonical", eventId=17, force=True, before={}, after={}, replayWallMs=2)
        report = dict(visits=[visit, visit], samples=[
            dict(phase="canonical.replay", eventId=17, timingRecords=[], logStart=10, status="complete"),
            dict(phase="canonical.replay", eventId=17, timingRecords=[], logStart=30, status="complete")])
        result = analyze_report(report, Path("fixture.json"))
        self.assertEqual([row["logStart"] for row in result["visits"]], [10, 30])
        self.assertEqual(result["groups"][0]["replayWallMs"]["p95"], 2)
        self.assertEqual(result["analysisIssues"], [])

    def test_checkpoint_child_phases_are_checked_but_not_added_to_parent(self):
        rows = [dict(api="VulkanCheckpoint", op="save", total_ms=8),
                dict(api="VulkanCheckpoint", op="saveCopy", total_ms=5,
                     drain_ms=2, record_ms=1, submitwait_ms=2)]
        result = summarize_scope_records(rows)
        self.assertEqual(result["consistencyIssues"], [])
        self.assertEqual(len(result["checkpointOperations"]), 2)
        rows[1]["record_ms"] = 7
        self.assertTrue(summarize_scope_records(rows)["consistencyIssues"])

    def test_mutation_target_counts_exclude_away_visits_and_missing_pixels_are_unknown(self):
        target = dict(phase="mutation.shaderReplaced", eventId=17, force=False,
                      before=dict(hits=4), after=dict(hits=5), replayWallMs=2)
        away = dict(target, eventId=16, before=dict(hits=1), after=dict(hits=4))
        original = dict(target, phase="mutation.original", force=True,
                        before=dict(hits=5), after=dict(hits=5), witness=dict(pixels=["original"]))
        report = dict(visits=[away, target, original], samples=[
            dict(phase=value["phase"] + ".replay", eventId=value["eventId"], timingRecords=[])
            for value in (away, target, original)],
            mutations=dict(eventId=17, checks=[dict(phase="mutation.shaderReplaced", observedHits=4)]))
        result = analyze_report(report, Path("fixture.json"))
        check = result["mutationChecksWithMeasuredTargetHits"][0]
        self.assertEqual(check["targetHitDeltaFromMeasuredVisits"], 1)
        self.assertEqual(check["measuredTargetCalls"], 1)
        self.assertIsNone(result["mutationPixels"]["originalForcedStable"])
        self.assertEqual(result["mutationPixels"]["originalForcedSampleCount"], 1)
        self.assertEqual(result["mutations"]["checks"][0]["observedHits"], 4)


if __name__ == "__main__":
    unittest.main()
