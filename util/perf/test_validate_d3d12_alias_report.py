"""Synthetic report acceptance tests; no native imports, GPU, or capture access."""
import copy
import unittest

from discover_d3d12_checkpoint_fixture import ALIAS_TEXTURE_NAMES
from test_discover_d3d12_checkpoint_fixture import alias_actions
from validate_d3d12_alias_report import validate_alias_report


def reports(mode):
    events = [12, 112, 212, 312]
    identities = {name: "ResourceId::" + str(100 + i) for i, name in enumerate(ALIAS_TEXTURE_NAMES)}
    discovery = {"schema": "renderdoc-d3d12-checkpoint-fixture/v1", "ok": True,
                 "capture": "alias.rdc", "captureBytes": 12345, "selectedEvents": events,
                 "actions": alias_actions(mode)}
    profile = {"schema": "renderdoc-checkpoint-profile/v1", "ok": True, "measurementComplete": True,
               "capture": "alias.rdc", "captureBytes": 12345, "events": events, "issues": [],
               "configuration": {"extra_texture": list(ALIAS_TEXTURE_NAMES)},
               "extraTextureResources": [{"name": name, "resource": resource}
                                         for name, resource in identities.items()],
               "observedHits": 2, "visits": [], "samples": []}
    for region, event in enumerate(events):
        hashes = [str(region + 1) * 64, "0" * 64]
        if mode == "read" and region >= 2:
            hashes[1] = str(region) * 64
        witness = {"eventId": event,
                   "extraTextures": [{"name": name, "resource": resource, "format": "R8G8B8A8_UNORM",
                                      "mips": 1, "slices": 1, "samples": 1}
                                     for name, resource in identities.items()],
                   "pixels": [{"resource": resource, "mip": 0, "slice": 0, "sample": 0,
                               "bytes": 256 * 128 * 4, "sha256": hashes[i]}
                              for i, resource in enumerate(identities.values())]}
        for phase, force in (("forward", False), ("reverse", False), ("canonical", True), ("canonical", True)):
            profile["visits"].append({"phase": phase, "eventId": event, "force": force,
                                      "witness": copy.deepcopy(witness)})
        if region >= 2:
            profile["samples"].append({"phase": "forward.replay", "eventId": event, "status": "complete",
                                       "timingRecords": [{"api": "D3D12Checkpoint", "op": "restore",
                                                          "event": 120, "target": event - 1,
                                                          "fullClearInitialCopiesDeferred": int(mode == "clear"),
                                                          "fullClearAllocationBytes": 131072 if mode == "clear" else 0},
                                                         {"api": "D3D12ClearProof", "checkpoint": 120,
                                                          "target": event - 1, "eligibleResources": 2,
                                                          "approved": int(mode == "clear"),
                                                          "aliasBarriersPassed": int(mode != "split"),
                                                          "blockedCandidates": int(mode == "read"),
                                                          "barrierStops": int(mode == "split"),
                                                          "stopReason": "splitOrUnknownBarrier" if mode == "split" else "complete"}]})
    return discovery, profile


class AliasReportTests(unittest.TestCase):
    def test_positive_and_both_conservative_negative_reports(self):
        for mode in ("clear", "read", "split"):
            discovery, profile = reports(mode)
            result = validate_alias_report(discovery, profile, mode)
            self.assertTrue(result["ok"], result["issues"])
            self.assertEqual(result["coveredVisits"], 16)
            self.assertEqual(result["ordinaryFullClearRestoreRecords"], 2 if mode == "clear" else 0)

    def test_candidate_counts_do_not_replace_actual_restore_copy_omissions(self):
        discovery, profile = reports("clear")
        for sample in profile["samples"]:
            sample["timingRecords"] = [{"api": "D3D12ClearProof", "approved": 100,
                                         "aliasBarriersPassed": 10}]
        profile["fullClearRestoreCount"] = 100
        result = validate_alias_report(discovery, profile, "clear")
        self.assertFalse(result["ok"])
        self.assertTrue(any("actual ordinary" in issue for issue in result["issues"]))

    def test_negative_cases_reject_one_actual_omission_even_when_pixels_match(self):
        for mode in ("read", "split"):
            discovery, profile = reports(mode)
            profile["samples"][0]["timingRecords"][0]["fullClearInitialCopiesDeferred"] = 1
            result = validate_alias_report(discovery, profile, mode)
            self.assertFalse(result["ok"])
            self.assertTrue(any("Negative alias" in issue for issue in result["issues"]))

    def test_clear_must_cross_alias_from_a_real_early_checkpoint(self):
        for missing in ("aliasBarriersPassed", "checkpoint"):
            discovery, profile = reports("clear")
            for sample in profile["samples"]:
                sample["timingRecords"][1][missing] = 0
            self.assertFalse(validate_alias_report(discovery, profile, "clear")["ok"])
        discovery, profile = reports("clear")
        for sample in profile["samples"]:
            sample["timingRecords"][0]["event"] = 0
            sample["timingRecords"][1]["checkpoint"] = 0
        self.assertFalse(validate_alias_report(discovery, profile, "clear")["ok"])

    def test_actual_visit_force_flag_controls_counting_without_a_phase_whitelist(self):
        discovery, profile = reports("clear")
        for visit in profile["visits"]:
            if visit["phase"] == "forward":
                visit["phase"] = "futureInteractivePhase"
        for sample in profile["samples"]:
            sample["phase"] = "futureInteractivePhase.replay"
        self.assertTrue(validate_alias_report(discovery, profile, "clear")["ok"])
        for visit in profile["visits"]:
            if visit["phase"] == "futureInteractivePhase":
                visit["force"] = True
        self.assertFalse(validate_alias_report(discovery, profile, "clear")["ok"])

    def test_old_bound_output_only_reports_cannot_pass(self):
        for phase in ("forward", "canonical"):
            discovery, profile = reports("read")
            visit = next(row for row in profile["visits"] if row["phase"] == phase)
            visit["witness"]["pixels"].pop()
            self.assertFalse(validate_alias_report(discovery, profile, "read")["ok"])
        discovery, profile = reports("read")
        profile["configuration"]["extra_texture"] = [ALIAS_TEXTURE_NAMES[0]]
        self.assertFalse(validate_alias_report(discovery, profile, "read")["ok"])

    def test_stable_parity_still_requires_old_target_to_reach_witness(self):
        discovery, profile = reports("read")
        # A broken demo could consistently clear W in normal and forced modes: parity alone passes.
        for row in profile["visits"]:
            row["witness"]["pixels"][1]["sha256"] = "0" * 64
        result = validate_alias_report(discovery, profile, "read")
        self.assertFalse(result["ok"])
        self.assertEqual(sum("does not preserve" in issue for issue in result["issues"]), 2)

    def test_stale_ordinary_witness_and_partial_native_data_fail(self):
        discovery, profile = reports("read")
        visit = next(row for row in profile["visits"] if row["phase"] == "forward" and row["eventId"] == 212)
        visit["witness"]["pixels"][1]["sha256"] = "0" * 64
        result = validate_alias_report(discovery, profile, "read")
        self.assertTrue(any("Ordinary T/W" in issue for issue in result["issues"]))
        discovery, profile = reports("clear")
        profile["visits"][0]["witness"]["pixels"][0]["bytes"] = 4
        self.assertFalse(validate_alias_report(discovery, profile, "clear")["ok"])

    def test_unstable_references_wrong_capture_and_wrong_mode_fail(self):
        discovery, profile = reports("read")
        canonical = next(row for row in profile["visits"] if row["phase"] == "canonical")
        canonical["witness"]["pixels"][0]["sha256"] = "f" * 64
        self.assertFalse(validate_alias_report(discovery, profile, "read")["ok"])
        discovery, profile = reports("read")
        profile["capture"] = "different.rdc"
        self.assertFalse(validate_alias_report(discovery, profile, "read")["ok"])
        with self.assertRaises(ValueError):
            validate_alias_report(discovery, profile, "clear")


if __name__ == "__main__":
    unittest.main()
