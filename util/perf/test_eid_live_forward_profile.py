import copy
import unittest

from eid_live_forward_profile import (comparisons, intermediate_coverage, live_evidence,
                                      parameters, sequence, texture_requests)


class LiveForwardEvidenceTests(unittest.TestCase):
    def hit(self):
        return {"api": "D3D12LiveForward", "from": 31, "target": 43, "enabled": 1,
                "hit": 1, "initialRestoreSkipped": 1, "replayedEvents": 11}

    def test_hit_requires_exact_request_and_range(self):
        self.assertFalse(live_evidence([self.hit()], 31, 43, True)["issues"])
        for key, value in (("from", 30), ("target", 42), ("initialRestoreSkipped", 0),
                           ("replayedEvents", 12), ("enabled", 0), ("hit", True)):
            record = self.hit()
            record[key] = value
            with self.subTest(key=key):
                self.assertTrue(live_evidence([record], 31, 43, True)["issues"])

    def test_missing_duplicate_and_forced_hits_rejected(self):
        self.assertTrue(live_evidence([], 31, 43, True)["issues"])
        self.assertTrue(live_evidence([self.hit(), self.hit()], 31, 43, True)["issues"])
        self.assertTrue(live_evidence([self.hit()], 31, 43, True, force=True)["issues"])
        self.assertTrue(live_evidence([self.hit()], 31, 43, False)["issues"])

    def test_honest_fallback_does_not_claim_saved_work(self):
        record = dict(self.hit(), hit=0, initialRestoreSkipped=0, replayedEvents=0)
        self.assertFalse(live_evidence([record], 31, 43, False)["issues"])
        self.assertTrue(live_evidence([record], 31, 43, True)["issues"])
        record["initialRestoreSkipped"] = 1
        self.assertTrue(live_evidence([record], 31, 43, False)["issues"])

    def test_sequence_contains_backward_force_and_recovery_controls(self):
        rows = sequence([31, 35, 39, 43])
        self.assertIn(("backwardDA", 31, False, False), rows)
        self.assertIn(("forceA", 31, True, False), rows)
        self.assertIn(("afterForceAD", 43, False, False), rows)
        self.assertEqual(rows[-1], ("recoveryAD", 43, False, True))

    def test_arguments_are_bounded_and_preserve_order(self):
        self.assertEqual(parameters("31,35,39,43", "1,2")[0], [31, 35, 39, 43])
        for events, resources in (("31,35,39", "1"), ("35,31,39,43", "1"),
                                   ("31,31,39,43", "1"), ("31,35,39,43", "1,1")):
            with self.assertRaises(ValueError):
                parameters(events, resources)


def witness(event, bound, changed):
    return {"eventId": event,
            "bindings": [{"resource": "ResourceId::%d" % bound}],
            "pixels": [{"resource": "ResourceId::%d" % resource, "subresource": [0, 0, 0],
                        "bytes": 4, "sha256": "draw" if resource in changed else "clear"}
                       for resource in (1, 2, 3, 4)]}


class LiveForwardPixelTests(unittest.TestCase):
    def references(self):
        return {event: [witness(event, index, set(range(1, index + 1))) for _ in range(2)]
                for index, event in enumerate((31, 35, 39, 43), 1)}

    def test_changed_intermediate_outputs_not_bound_at_final_are_required(self):
        refs = self.references()
        rows, issues = intermediate_coverage(refs, [31, 35, 39, 43])
        self.assertFalse(issues)
        self.assertEqual(rows[0]["changedIntermediateOutputsUnboundAtD"], ["ResourceId::2"])
        self.assertEqual(rows[1]["changedIntermediateOutputsUnboundAtD"], ["ResourceId::3"])

    def test_prior_frame_static_pixels_cannot_prove_intermediate_execution(self):
        refs = self.references()
        for value in refs[31]:
            value["pixels"] = copy.deepcopy(refs[43][0]["pixels"])
        self.assertTrue(intermediate_coverage(refs, [31, 35, 39, 43])[1])

    def test_unchanged_final_rt_does_not_hide_wrong_unbound_intermediate(self):
        refs = self.references()
        observed = copy.deepcopy(refs[43][0])
        observed["pixels"][1]["sha256"] = "clear"
        rows, issues = comparisons([{"eventId": 43, "witness": observed}], refs,
                                  [31, 35, 39, 43])
        self.assertTrue(issues)
        self.assertEqual(rows[-1]["status"], "failedStableCanonicalMismatch")

    def test_unstable_reference_is_inconclusive(self):
        refs = self.references()
        refs[35][1]["pixels"][0]["sha256"] = "unstable"
        self.assertTrue(intermediate_coverage(refs, [31, 35, 39, 43])[1])
        self.assertEqual(comparisons([], refs, [35])[0][0]["status"],
                         "inconclusiveCanonicalUnstable")


if __name__ == "__main__":
    unittest.main()
