import unittest

from analyze_eid_timing import compare_by_eid, parse_records, select_records, summarize


class TimingAnalysisTests(unittest.TestCase):
    def test_old_records_do_not_claim_unmeasured_details(self):
        records = parse_records(
            "api=D3D12 range=1->20 type=Full total_ms=100 explicitWait_ms=20\n"
            "api=D3D12 range=1->30 type=Full total_ms=80 timing_version=2 "
            "section.contextReplay_self_ms=60 section.explicitWait_self_ms=20 "
            "explicitWait_ms=20 wait.queueSignal_ms=20 count.initialDescriptors=100\n"
            "api=D3D12 range=1->40 type=Full total_ms=10 timing_version=2 unscoped_ms=10\n"
        )
        report = summarize(select_records(records))
        self.assertEqual(report["metrics"]["total_ms"]["samples"], 3)
        self.assertEqual(report["metrics"]["wait.queueSignal_ms"]["samples"], 2)
        self.assertEqual(report["metrics"]["count.initialDescriptors"]["mean"], 50)
        self.assertEqual(report["consistency_issues"], [])

    def test_overlapping_inclusive_fields_are_not_added(self):
        records = parse_records(
            "api=D3D12 range=1->50 type=Full total_ms=100 timing_version=2 "
            "applyInitial_ms=40 contextReplay_ms=60 explicitWait_ms=70 "
            "section.applyInitial_ms=40 section.applyInitial_self_ms=20 "
            "section.contextReplay_ms=60 section.contextReplay_self_ms=10 "
            "section.explicitWait_ms=70 section.explicitWait_self_ms=70 "
            "wait.initialRestore_ms=20 wait.queueSignal_ms=50 unscoped_ms=0"
        )
        self.assertEqual(summarize(records)["consistency_issues"], [])
        records[0]["section.contextReplay_self_ms"] += 2
        self.assertEqual(len(summarize(records)["consistency_issues"]), 1)

    def test_eid_comparison_filters_changed_mix(self):
        baseline = parse_records(
            "api=D3D12 range=1->10 type=Full total_ms=100\n"
            "api=D3D12 range=1->10 type=Full total_ms=120\n"
            "api=D3D12 range=1->20 type=Full total_ms=500"
        )
        current = parse_records(
            "api=D3D12 range=1->10 type=Full total_ms=80\n"
            "api=D3D12 range=1->30 type=Full total_ms=200"
        )
        comparison = compare_by_eid(current, baseline)
        self.assertEqual(len(comparison), 1)
        self.assertEqual(comparison[0]["baseline_median_ms"], 110)
        self.assertEqual(comparison[0]["delta_ms"], -30)

    def test_parser_tolerates_log_prefixes_and_partial_records(self):
        records = parse_records(
            "[EIDTiming] api=D3D12 range=1->20 type=Full total_ms=10.0\n"
            "api=D3D12 range=1->30 type=Full total_ms=NaN\n"
            "api=ScrubSnapshotHit eid=20 total_ms=0.7\n"
            "api=D3D12 range=1->40 type=Full total_ms=\n"
            "commit EID 50: queueWait 30ms"
        )
        self.assertEqual(len(select_records(records)), 1)
        self.assertEqual(len(select_records(records, "ScrubSnapshotHit", "")), 1)

    def test_last_applies_after_eid_and_type_selection(self):
        records = parse_records(
            "api=D3D12 range=1->10 type=Full total_ms=10\n"
            "api=D3D12 range=1->10 type=OnlyDraw total_ms=5\n"
            "api=D3D12 range=1->20 type=Full total_ms=20\n"
            "api=D3D12 range=1->10 type=Full total_ms=30"
        )
        selected = select_records(records, eids={10}, last=1)
        self.assertEqual([r["total_ms"] for r in selected], [30])


if __name__ == "__main__":
    unittest.main()
