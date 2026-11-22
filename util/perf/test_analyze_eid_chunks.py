"""CPU-only log accounting regression tests."""
import unittest

from analyze_eid_chunks import summarize_chunks
from analyze_eid_timing import parse_records


class ChunkTimingTests(unittest.TestCase):
    def test_wait_is_not_added_to_phase_total_and_remainder_is_retained(self):
        records = parse_records("""
api=ReplayChunk driver=D3D12 range=1->20 type=WithoutDraw chunkId=1 chunk=Draw calls=5 decodeBoundaries=5 decode_ms=2 execute_ms=8 unclassified_ms=0 decode_self_ms=2 execute_self_ms=3 unclassified_self_ms=0 wait_ms=5 submit_ms=0 total_ms=10
api=ReplayChunk driver=D3D12 range=1->20 type=WithoutDraw chunkId=4294967295 chunk=other calls=2 decodeBoundaries=0 decode_ms=0 execute_ms=0 unclassified_ms=4 decode_self_ms=0 execute_self_ms=0 unclassified_self_ms=4 wait_ms=0 submit_ms=0 total_ms=4
api=ReplayChunk driver=Vulkan range=1->20 type=WithoutDraw chunkId=1 chunk=Draw calls=1 decodeBoundaries=1 decode_ms=2 execute_ms=8 unclassified_ms=0 total_ms=10
""")
        report = summarize_chunks(records, "D3D12", "WithoutDraw", {20})
        self.assertEqual(report["recordCount"], 2)
        self.assertEqual(report["totals"]["total_ms"], 14)
        self.assertEqual(report["totals"]["calls"], 7)
        self.assertEqual(report["totals"]["decodeBoundaries"], 5)
        self.assertEqual(report["chunks"][-1]["chunk"], "other")
        self.assertEqual(report["consistencyIssues"], [])

    def test_corrupt_phase_accounting_is_rejected(self):
        record = dict(api="ReplayChunk", driver="D3D12", type="OnlyDraw", chunkId=1,
                      chunk="Draw", calls=1, decodeBoundaries=2, decode_ms=2, execute_ms=8,
                      execute_self_ms=9, unclassified_ms=0, total_ms=15)
        self.assertEqual(len(summarize_chunks([record])["consistencyIssues"]), 3)


if __name__ == "__main__":
    unittest.main()
