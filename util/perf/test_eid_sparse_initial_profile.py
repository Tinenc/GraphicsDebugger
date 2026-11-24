import copy
from types import SimpleNamespace
import unittest

from eid_sparse_initial_profile import (
    KINDS, PAYLOAD_BYTES, SCHEMA, SEQUENCE, SWITCH, TEXTURE_BYTES, TEXTURE_NAMES,
    accept_visits, cold_start_issues, control_issues, environment, fixture_events, named_textures,
    output_pattern, sparse_evidence, texture_pattern)


RESOURCE = "ResourceId::10"


def native_proof(retained=False, dirty=False, disabled=False, seed=False):
    rows = [{"api": "D3D12SparseInitial", "op": "prepare", "component": 0, "resource": RESOURCE,
             "eligible": 1, "enabled": int(not disabled), "firstReset": 1,
             "recordingAllowed": int(not disabled), "preInitialDirty": int(dirty),
             "retained": int(retained), "residentCopyBytes": TEXTURE_BYTES if retained else 0,
             "generation": 9}]
    if retained:
        rows.append({"api": "D3D12SparseInitial", "op": "retain", "component": 0,
                     "resource": RESOURCE, "avoidedRecordedCopyBytes": TEXTURE_BYTES, "generation": 9})
    elif not disabled and (dirty or seed):
        rows.append({"api": "D3D12SparseInitial", "op": "submit", "component": 0,
                     "published": 1, "recordedCopyBytes": TEXTURE_BYTES, "generation": 9})
    return rows


def visits(disabled=False):
    rows = []
    for index, (region, kind) in enumerate(zip(SEQUENCE, KINDS)):
        rebuild = kind in ("rebuildAfterCopy", "rebuildAfterMapping")
        retained = not disabled and index != 0 and not rebuild
        actual = 0 if retained else TEXTURE_BYTES
        proof = sparse_evidence(native_proof(retained, rebuild, disabled, index == 0), RESOURCE, disabled)
        restore = {"issues": [], "records": [{"enabled": 1, "shadow": 0, "actualCopyBytes": actual + 400,
                   "resources": [{"resource": RESOURCE, "actualCopyBytes": actual, "wholeResourceCopies": 0},
                                 {"resource": "ResourceId::20", "actualCopyBytes": 400, "wholeResourceCopies": 0}]}]}
        rows.append({"label": "visit%d" % index, "eventId": 100 + region * 10, "region": region,
                     "kind": kind, "force": False, "sparse": proof, "restore": restore})
    return rows


def actions():
    result = []
    def row(eid, name, chunk, **fields):
        value = dict(eventId=eid, name=name, commandBoundary=False, beginPass=False, endPass=False,
                     draw=False, clear=False, marker=False,
                     exactAPI={"chunkIndex": chunk, "chunkName": "SetMarker"})
        value.update(fields)
        result.append(value)
    for region in range(7):
        base = 100 + 10 * region
        row(base, "=> ExecuteCommandLists(1)[0]: Reset()", region * 100, commandBoundary=True, beginPass=True)
        if region < 6:
            if region in (2, 4):
                row(base + 1, "Sparse initial copy B" if region == 2 else "Sparse initial initialize mapped C", region * 100 + 10, marker=True)
                row(base + 2, "CopyTextureRegion", region * 100 + 20,
                    exactAPI={"chunkName": "CopyTextureRegion", "chunkIndex": region * 100 + 20})
            row(base + 3, "Sparse initial region %d" % region, region * 100 + 30, marker=True)
            row(base + 4, "DrawInstanced", region * 100 + 40, draw=True,
                exactAPI={"chunkName": "DrawInstanced", "chunkIndex": region * 100 + 40})
        row(base + 8, "=> ExecuteCommandLists(1)[0]: Close()", region * 100 + 80, commandBoundary=True, endPass=True)
    return result


class SparseDiscoveryTests(unittest.TestCase):
    def test_real_marker_draws_and_copy_mapping_operations_are_required(self):
        mapping = [{"chunkIndex": 350}, {"chunkIndex": 610}]
        found = fixture_events(actions(), mapping)
        self.assertEqual(len(found["selectedEvents"]), 6)
        self.assertEqual(len(found["submissionGroups"]), 7)  # the next-frame reset is not a seventh draw
        for changed in ([], [{"chunkIndex": 10}, {"chunkIndex": 610}],
                        [{"chunkIndex": 350}, {"chunkIndex": 530}]):
            with self.assertRaises(ValueError):
                fixture_events(actions(), changed)
        bad = actions()
        next(r for r in bad if r["name"] == "Sparse initial region 1")["marker"] = False
        with self.assertRaises(ValueError):
            fixture_events(bad, mapping)
        bad = actions()
        next(r for r in bad if r["name"] == "CopyTextureRegion")["exactAPI"]["chunkName"] = "SetMarker"
        with self.assertRaises(ValueError):
            fixture_events(bad, mapping)

    def test_complete_named_native_shapes_are_not_guessed(self):
        descriptions = [SimpleNamespace(name=name, resourceId=i + 1) for i, name in enumerate(TEXTURE_NAMES)]
        textures = [SimpleNamespace(resourceId=i + 1, width=width, height=128, depth=1, mips=1,
                                    arraysize=1, msSamp=1, format=SimpleNamespace(Name=lambda: "RGBA8_UNORM"))
                    for i, width in enumerate((128, 768))]
        self.assertEqual(sum(r["bytes"] for r in named_textures(descriptions, textures)), PAYLOAD_BYTES)
        textures[0].mips = 2
        with self.assertRaises(ValueError):
            named_textures(descriptions, textures)


class SparseOracleTests(unittest.TestCase):
    def test_full_texture_and_output_patterns_cover_last_pixel_and_untouched_regions(self):
        for pattern in range(3):
            data = texture_pattern(pattern)
            self.assertEqual(len(data), TEXTURE_BYTES)
            self.assertEqual(data[-4:], bytes((128 if pattern == 1 else 127,
                                               128 if pattern == 2 else 127,
                                               (17, 101, 203)[pattern], 255)))
        for region in range(6):
            output = output_pattern(region)
            self.assertEqual(len(output), TEXTURE_BYTES * 6)
            for part in range(6):
                offset = (127 * 768 + part * 128 + 127) * 4
                expected = texture_pattern(part // 2)[-4:] if part <= region else b"\0\0\0\xff"
                self.assertEqual(output[offset:offset + 4], expected)
        self.assertNotEqual(output_pattern(0), output_pattern(1))
        with self.assertRaises(ValueError):
            output_pattern(6)


class SparseAcceptanceTests(unittest.TestCase):
    def test_loading_transition_uses_real_copy_counters_and_does_not_relax_active_proof(self):
        # The captured v13 cold setter has real generic initial-copy counters but deliberately
        # no D3D12SparseInitial/DirtyRestore detail while its device is still Loading.
        row = {"api": "D3D12", "type": "WithoutDraw", "range": "1->15", "replay_depth": 0,
               "partial": 0, "section.applyInitial_calls": 1, "count.initialSparseResources": 1,
               "count.initialGPUCopyFootprintBytes": 458752}
        self.assertFalse(cold_start_issues([row], 15))
        self.assertTrue(sparse_evidence([row], RESOURCE, True)["issues"])
        for field, value in (("count.initialGPUCopyFootprintBytes", 0), ("count.initialSparseResources", 0),
                             ("section.applyInitial_calls", 0), ("range", "1->14")):
            self.assertTrue(cold_start_issues([dict(row, **{field: value})], 15))

    def test_true_retention_and_actual_copy_mapping_rebuilds_pass(self):
        off, on = visits(True), visits()
        self.assertFalse(accept_visits(off, RESOURCE, True)[1])
        comparison, issues = accept_visits(on, RESOURCE, False, off)
        self.assertFalse(issues)
        self.assertTrue(comparison[0]["excludedSeed"])
        self.assertEqual(sum(bool(r.get("passedActualRebuild")) for r in comparison), 2)
        self.assertEqual(sum(bool(r.get("passedActualReduction")) for r in comparison), 6)

    def test_plan_or_hash_success_without_real_skips_and_copy_reduction_fails(self):
        for mutation in ("retainMissing", "copyUnchanged", "totalUnchanged", "baselineMissing"):
            on, off = visits(), visits(True)
            if mutation == "retainMissing":
                on[1]["sparse"]["retains"] = []
            elif mutation == "copyUnchanged":
                on[1]["restore"] = copy.deepcopy(off[1]["restore"])
            elif mutation == "totalUnchanged":
                on[1]["restore"]["records"][0]["actualCopyBytes"] = off[1]["restore"]["records"][0]["actualCopyBytes"]
            else:
                on[4]["sparse"]["submits"] = []
            self.assertTrue(accept_visits(on, RESOURCE, False, off)[1], mutation)

    def test_stale_seed_after_either_mutation_cannot_pass(self):
        for index in (4, 7):
            on = visits()
            on[index]["sparse"] = sparse_evidence(native_proof(True), RESOURCE, False)
            self.assertTrue(accept_visits(on, RESOURCE, False, visits(True))[1])
        on = visits()
        on[5]["force"] = True
        self.assertTrue(accept_visits(on, RESOURCE, False, visits(True))[1])
        on = visits()
        on[8]["restore"]["records"] = []
        self.assertTrue(accept_visits(on, RESOURCE, False, visits(True))[1])

    def test_wrong_generation_disabled_retention_and_incomplete_gate_are_rejected(self):
        rows = native_proof(True)
        rows[-1]["generation"] += 1
        self.assertTrue(sparse_evidence(rows, RESOURCE, False)["issues"])
        self.assertTrue(sparse_evidence(native_proof(True), RESOURCE, True)["issues"])
        rows = native_proof()
        rows[0].update(eligible=0, reason="initialTextureCopyNotComplete")
        self.assertTrue(sparse_evidence(rows, RESOURCE, False)["issues"])
        rows = native_proof(True)
        rows[0]["preInitialDirty"] = True
        self.assertTrue(sparse_evidence(rows, RESOURCE, False)["issues"])
        proof = sparse_evidence(native_proof(True) * 2, RESOURCE, False)
        self.assertFalse(proof["issues"])
        self.assertEqual(proof["mirrorRecordsRemoved"], 2)

    def test_references_disable_both_reuse_paths_and_control_requires_same_build(self):
        self.assertEqual(environment(False)[SWITCH], "1")
        self.assertEqual(environment(False, True)[SWITCH], "0")
        self.assertEqual(environment(False, True)["RENDERDOC_D3D12_DISABLE_DIRTY_RESTORE"], "1")
        identity = {"sha256": "abc", "bytes": 42}
        on = {"schema": SCHEMA, "processId": 2, "captureIdentity": identity,
              "buildIdentity": {"nativeModules": [identity], "pythonModule": identity},
              "events": [1, 2, 3, 4, 5, 6], "textures": ["T", "O"],
              "referenceEnvironment": environment(True, True), "environment": environment(False)}
        off = copy.deepcopy(on)
        off.update(processId=1, ok=True, measurementComplete=True,
                   configuration={"disable_residency": True}, environment=environment(True))
        self.assertFalse(control_issues(on, off))
        off["buildIdentity"]["nativeModules"][0] = {"sha256": "different", "bytes": 42}
        self.assertTrue(control_issues(on, off))


if __name__ == "__main__":
    unittest.main()
