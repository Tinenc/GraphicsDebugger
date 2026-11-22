import copy
import hashlib
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest

from analyze_eid_restore import SOURCE_FIELDS
from eid_uav_declarations_profile import (
    BUFFER_BYTES, BUFFER_NAMES, SCHEMA, STATIC_BYTES, compare_restores, control_issues,
    declaration_evidence, environment, named_buffers, native_replay_present, output_pattern,
    read_payload, read_state, restore_evidence, static_pattern, written_pattern)


def restore_text(trimmed=False, shadow=0):
    static = 0 if trimmed else 4194304
    resources = [("ResourceId::12", static, 4194304 - static),
                 ("ResourceId::13", static, 4194304 - static), ("ResourceId::14", 131072, 0)]
    actual = sum(row[1] for row in resources)
    source = " ".join("%s=%d" % (key, actual if key == "gpuDefaultBufferBytes" else 0)
                      for key in SOURCE_FIELDS)
    tuples = ",".join(rid + ":" + "/".join(str(value if i == 4 else 0) for i in range(10))
                      for rid, value, _ in resources)
    detail = " ".join("%s:%d/%d/0" % row for row in resources)
    return ("api=D3D12DirtyRestore enabled=1 shadow=%d actualCopyBytes=%d skippedStaticBytes=%d "
            "wholeResourceCopies=0 dirtyResources=%d dirtyHeaps=0 reason=submittedWrites "
            "restoreTimingVersion=2 %s resourceDetailCount=3 omittedResourceCount=0 "
            "omittedActualCopyBytes=0 omittedSkippedStaticBytes=0 omittedWholeResourceCopies=0 "
            "resourceSourceCopies=%s resourceActualSkippedWhole= %s\n" %
            (shadow, actual, STATIC_BYTES if trimmed else 0, 2 if trimmed else 4, source, tuples, detail))


class NativeUAVProofTests(unittest.TestCase):
    def row(self, disabled=False):
        return {"api": "D3D12DirtyWriteCutoff", "requestedEnd": 42, "declaredUAVs": int(not disabled),
                "declarationHits": 0 if disabled else 1, "declarationFallbacks": 0,
                "uavSlotsBefore": 0 if disabled else 4, "uavSlotsAfter": 0 if disabled else 2,
                "proofValid": 1}

    def test_only_submitted_proof_counts_as_trim(self):
        self.assertEqual(declaration_evidence([self.row()], 43, False)["nativeTrimRecords"], 1)
        plans = [{"api": "D3D12UAVDeclarationPSO", "complete": 1, "ranges": 1}]
        self.assertEqual(declaration_evidence(plans, 43, False)["nativeTrimRecords"], 0)
        for field, value in (("declarationHits", 0), ("declaredUAVs", 0), ("uavSlotsAfter", 4)):
            row = dict(self.row(), **{field: value})
            self.assertEqual(declaration_evidence([row], 43, False)["nativeTrimRecords"], 0)

    def test_disabled_rejects_real_or_claimed_trim(self):
        self.assertFalse(declaration_evidence([self.row(True)], 43, True)["issues"])
        self.assertTrue(declaration_evidence([self.row()], 43, True)["issues"])
        row = dict(self.row(True), uavSlotsBefore=4, uavSlotsAfter=2)
        self.assertTrue(declaration_evidence([row], 43, True)["issues"])

    def test_unknown_target_invalid_proof_and_live_forward_are_rejected(self):
        for key, value in (("requestedEnd", 44), ("proofValid", 0), ("declarationHits", True),
                           ("uavSlotsAfter", -1)):
            self.assertTrue(declaration_evidence([dict(self.row(), **{key: value})], 43, False)["issues"])
        self.assertTrue(declaration_evidence([{"api": "D3D12LiveForward", "hit": 1}], 43, False)["issues"])

    def test_real_d3d12_range_identity_uses_range_not_an_invented_eid_field(self):
        row = {"api": "D3D12", "type": "WithoutDraw", "range": "1->43", "replay_depth": 0}
        self.assertTrue(native_replay_present([row], 43))
        for key, value in (("range", "2->43"), ("range", "1->42"), ("replay_depth", 1),
                           ("type", "OnlyDraw")):
            self.assertFalse(native_replay_present([dict(row, **{key: value})], 43))


class ActualRestoreTests(unittest.TestCase):
    def visits(self, trimmed):
        return [{"label": label, "eventId": event, "restore": restore_evidence(restore_text(trimmed))}
                for label, event in (("AB", 43), ("BD", 55))]

    def test_complete_actual_accounting_proves_reduced_resources_and_bytes(self):
        self.assertFalse(restore_evidence(restore_text())["issues"])
        self.assertFalse(restore_evidence(restore_text(True))["issues"])
        rows, issues = compare_restores(self.visits(True), self.visits(False),
                                        ["ResourceId::12", "ResourceId::13"], 2)
        self.assertFalse(issues)
        self.assertTrue(all(r["measuredReduction"] and r["actualBytesSaved"] == STATIC_BYTES
                            and r["dirtyResourcesSaved"] == 2 for r in rows))

    def test_equal_copies_or_missing_evidence_cannot_pass_even_with_equal_pixels(self):
        on, off = self.visits(False), self.visits(False)
        self.assertTrue(compare_restores(on, off, ["ResourceId::12", "ResourceId::13"], 1)[1])
        on = self.visits(True)
        on[0]["restore"] = {"records": [], "issues": []}
        self.assertTrue(compare_restores(on, off, ["ResourceId::12", "ResourceId::13"], 2)[1])
        on = self.visits(True)
        on[1]["eventId"] += 1
        self.assertTrue(compare_restores(on, off, ["ResourceId::12", "ResourceId::13"], 1)[1])

    def test_shadow_truncation_or_one_static_copy_remaining_is_not_acceptance(self):
        self.assertTrue(restore_evidence(restore_text(shadow=1))["issues"])
        self.assertTrue(restore_evidence(restore_text().replace("resourceDetailCount=3", "resourceDetailCount=4"))["issues"])
        on = self.visits(True)
        for visit in on:
            visit["restore"]["records"][0]["resources"][0]["actualCopyBytes"] = 1
        self.assertTrue(compare_restores(on, self.visits(False), ["ResourceId::12", "ResourceId::13"], 1)[1])

    def test_unknown_size_whole_copies_must_match_exact_resources_and_counts(self):
        on, off = self.visits(True), self.visits(False)
        for visits in (on, off):
            for visit in visits:
                visit["restore"]["records"][0]["resources"].append(
                    {"resource": "ResourceId::99", "actualCopyBytes": 0,
                     "skippedStaticBytes": 0, "wholeResourceCopies": 1})
        self.assertFalse(compare_restores(on, off, ["ResourceId::12", "ResourceId::13"], 2)[1])
        on[0]["restore"]["records"][0]["resources"][-1]["resource"] = "ResourceId::98"
        self.assertTrue(compare_restores(on, off, ["ResourceId::12", "ResourceId::13"], 2)[1])


class FixtureByteTests(unittest.TestCase):
    def test_native_names_lengths_and_unique_original_resources_are_required(self):
        descriptions = [SimpleNamespace(name=name, resourceId=i + 1) for i, name in enumerate(BUFFER_NAMES)]
        buffers = [SimpleNamespace(resourceId=i + 1, length=size) for i, size in enumerate(BUFFER_BYTES)]
        self.assertEqual([b["bytes"] for b in named_buffers(descriptions, buffers)], list(BUFFER_BYTES))
        with self.assertRaises(ValueError):
            named_buffers(descriptions + [descriptions[0]], buffers)
        buffers[2].length -= 4
        with self.assertRaises(ValueError):
            named_buffers(descriptions, buffers)

    def test_full_static_patterns_include_last_element(self):
        for index, base, slope in ((2, 0x10000000, 3), (3, 0x20000000, 5)):
            data = static_pattern(index)
            self.assertEqual(len(data), BUFFER_BYTES[index])
            self.assertEqual(struct.unpack_from("<I", data)[0], base)
            self.assertEqual(struct.unpack_from("<I", data, len(data) - 4)[0],
                             base + slope * (BUFFER_BYTES[index] // 4 - 1))

    def test_written_arrays_preserve_all_prior_regions_and_both_dynamic_indices(self):
        for region in range(4):
            buffers = [written_pattern(region, i) for i in (0, 1)]
            for element in (0, 1, 63, 64, 65, 127, 128, 191, 192, 255, 256, 32767):
                x, part = element % 256, (element % 256) // 64
                expected = ((part + 1) * 65536 + ((3 * element) & 255) * 256 +
                            ((5 * element) & 255) + 1)
                for index in (0, 1):
                    value = struct.unpack_from("<I", buffers[index], element * 4)[0]
                    self.assertEqual(value, expected if part <= region and ((x ^ part) & 1) == index else 0)

    def test_full_output_has_opaque_clear_and_independent_integer_colour_formula(self):
        initial, final = output_pattern(0), output_pattern(3)
        self.assertEqual(len(final), 256 * 128 * 4)
        self.assertEqual(initial[64 * 4:65 * 4], b"\x00\x00\x00\xff")
        element = 65
        value = 2 * 65536 + ((3 * element) & 255) * 256 + ((5 * element) & 255) + 1
        self.assertEqual(final[element * 4:element * 4 + 4],
                         bytes(((3 * element) & 255, (5 * element) & 255, (value >> 4) & 255, 255)))

    def test_payload_requires_complete_bytes_identity_and_bounds(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "canonical.payload.bin"
            path.write_bytes(b"prefixABCDsuffix")
            entry = {"offset": 6, "bytes": 4, "sha256": hashlib.sha256(b"ABCD").hexdigest()}
            self.assertEqual(read_payload(path, entry, 4), b"ABCD")
            for changed in (dict(entry, offset=99), dict(entry, bytes=5), dict(entry, sha256="bad"),
                            dict(entry, offset=True)):
                with self.assertRaises(RuntimeError):
                    read_payload(path, changed, 4)
            path.write_bytes(b"prefixABC")
            with self.assertRaises(RuntimeError):
                read_payload(path, entry, 4)

    def test_native_read_checks_actual_full_bytes_before_generating_hash_witness(self):
        null = SimpleNamespace(resource=0, firstMip=0, numMips=1, firstSlice=0, numSlices=1,
                               format=SimpleNamespace(Name=lambda: "RGBA8"))
        bound = copy.copy(null)
        bound.resource = 2
        pipe = SimpleNamespace(GetPrimitiveTopology=lambda: "triangles", GetOutputTargets=lambda: [bound],
                               GetDepthTarget=lambda: null)
        controller = SimpleNamespace(GetPipelineState=lambda: pipe, GetCurrentEventID=lambda: 43,
            GetBufferData=lambda *args: b"ABCD", GetTextureData=lambda *args: b"RGBA",
            GetFatalErrorStatus=lambda: SimpleNamespace(code=0))
        rd = SimpleNamespace(ShaderStage=SimpleNamespace(), Subresource=lambda *args: args,
                             ResultCode=SimpleNamespace(Succeeded=0))
        spec = [{"name": "test", "resource": "1", "bytes": 4}]
        args = (controller, rd, spec, {"1": 1}, [(2, (0, 0, 0))], {0: b"ABCD"},
                {("2", (0, 0, 0)): b"RGBA"}, 8)
        witness, raw = read_state(*args)
        self.assertEqual(raw, b"ABCDRGBA")
        self.assertEqual(witness["buffers"][0]["bytes"], 4)
        controller.GetBufferData = lambda *args: b"ABCE"
        with self.assertRaises(RuntimeError):
            read_state(*args)
        controller.GetBufferData = lambda *args: b"ABC"
        with self.assertRaises(RuntimeError):
            read_state(*args)


class ControlIdentityTests(unittest.TestCase):
    def reports(self):
        identity = {"sha256": "core", "bytes": 100}
        on = {"schema": SCHEMA, "captureIdentity": {"sha256": "capture", "bytes": 999},
              "buildIdentity": {"pythonModule": identity, "nativeModules": [identity]},
              "events": [10, 20, 30, 40], "buffers": [1, 2, 3, 4], "textures": [5],
              "environment": environment(False)}
        off = copy.deepcopy(on)
        off.update(ok=True, measurementComplete=True, configuration={"disable_declarations": True},
                   environment=environment(True))
        return on, off

    def test_only_the_declaration_switch_may_differ(self):
        on, off = self.reports()
        self.assertFalse(control_issues(on, off))
        for key, value in (("RENDERDOC_REPLAY_CHECKPOINTS", "1"),
                           ("RENDERDOC_D3D12_DISABLE_LIVE_FORWARD", "0")):
            changed = copy.deepcopy(off)
            changed["environment"][key] = value
            self.assertTrue(control_issues(on, changed))

    def test_failed_other_capture_or_other_binary_control_is_rejected(self):
        on, off = self.reports()
        for modify in (lambda r: r.update(ok=False), lambda r: r.update(measurementComplete=False),
                       lambda r: r["captureIdentity"].update(sha256="other"),
                       lambda r: r["buildIdentity"]["nativeModules"][0].update(sha256="other"),
                       lambda r: r["configuration"].update(disable_declarations=False),
                       lambda r: r.update(events=[1, 2, 3, 4])):
            changed = copy.deepcopy(off)
            modify(changed)
            self.assertTrue(control_issues(on, changed))


if __name__ == "__main__":
    unittest.main()
