"""Deciding that two captures contain the same Shader, without asking which capture.

The identity these tests describe replaced one that could not answer the question at
all: it hashed the capture path and RenderDoc's ResourceId, and the field data was three
stored catalogs of one .rdc, identical in path, API, stage, entry point and Shader bytes,
differing only in 853 / 860 / 12212. What is left after removing everything volatile is
the Shader's own bytes, its ABI, and the shape of its decompiled source -- in that order,
because each is weaker than the one before and licenses less.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import shader_reuse as reuse
from src import shared_shader_library as library


ONE = "3c" * 32
TWO = "ab" * 32
THREE = "7f" * 32


def _shader(raw=ONE, interface=TWO, stage="fragment", entry="MainPS", code=""):
    return {"rawSha256": raw, "interfaceHash": interface, "stage": stage,
            "entryPoint": entry, "codeSha256": code}


class IdentityTests(unittest.TestCase):
    def test_an_identity_is_only_facts_about_the_shader(self) -> None:
        identity = reuse.shader_identity(dict(_shader(), resourceId=860,
                                              capturePath="D:/x/6.rdc",
                                              eventIds=[11329]))
        self.assertEqual(set(identity), set(reuse.IDENTITY_FIELDS))
        for volatile in ("resourceId", "capturePath", "eventIds"):
            self.assertNotIn(volatile, identity)

    def test_hashes_are_compared_case_insensitively(self) -> None:
        self.assertEqual(
            reuse.shader_identity(_shader(raw=ONE.upper()))["rawSha256"], ONE)

    def test_something_with_no_hash_at_all_is_not_an_identity(self) -> None:
        with self.assertRaises(ValueError):
            reuse.shader_identity({"stage": "fragment", "entryPoint": "MainPS"})


class MatchTierTests(unittest.TestCase):
    def test_identical_bytes_are_the_same_shader_whatever_else_differs(self) -> None:
        verdict = reuse.match(
            dict(_shader(), resourceId=853, capturePath="D:/a/6.rdc"),
            dict(_shader(), resourceId=12212, capturePath="E:/b/other.rdc"))
        self.assertEqual(verdict["tier"], "bytes")
        self.assertTrue(verdict["evidenceIsConclusive"])
        self.assertIn("organizedSource", verdict["suggestedReuse"])

    def test_the_same_code_in_a_different_container_still_counts(self) -> None:
        verdict = reuse.match(_shader(raw=ONE, code=THREE),
                              _shader(raw=TWO, interface="cd" * 32, code=THREE))
        self.assertEqual(verdict["tier"], "bytes")
        self.assertIn("debug", verdict["evidence"]["note"])

    def test_a_matching_abi_does_not_establish_names_or_the_body(self) -> None:
        verdict = reuse.match(_shader(raw=ONE), _shader(raw=THREE))
        self.assertEqual(verdict["tier"], "interface")
        self.assertFalse(verdict["evidenceIsConclusive"])
        self.assertEqual(verdict["suggestedReuse"], [])
        self.assertFalse(verdict["semanticIdentityVerified"])
        self.assertNotIn("organizedSource", verdict["suggestedReuse"])

    def test_different_stages_are_never_the_same_shader(self) -> None:
        verdict = reuse.match(_shader(stage="fragment"),
                              _shader(stage="vertex"))
        self.assertEqual(verdict["tier"], "none")
        self.assertIn("stages", verdict["reason"])

    def test_a_source_match_carries_nothing_by_itself(self) -> None:
        text = "float4 MainPS() { float3 a = normalize(b) * 2.0; return float4(a, 1); }"
        renamed = "float4 MainPS() { float3 q = normalize(z) * 2.0; return float4(q, 1); }"
        verdict = reuse.match(
            _shader(raw=ONE, interface=TWO), _shader(raw=THREE, interface="cd" * 32),
            current_source=reuse.source_fingerprint(text, "MainPS"),
            candidate_source=reuse.source_fingerprint(renamed, "MainPS"))
        self.assertEqual(verdict["tier"], "source")
        self.assertEqual(verdict["suggestedReuse"], [])
        self.assertFalse(verdict["evidenceIsConclusive"])
        self.assertTrue(verdict["evidence"]["source"]["entryPointMatches"])

    def test_two_unrelated_shaders_match_at_no_tier(self) -> None:
        verdict = reuse.match(_shader(raw=ONE, interface=TWO),
                              _shader(raw=THREE, interface="cd" * 32))
        self.assertEqual(verdict["tier"], "none")

    def test_same_program_with_different_material_bindings_only_suggests_code_candidates(self) -> None:
        for candidate in (_shader(), _shader(raw=THREE, code=ONE)):
            with self.subTest(candidate=candidate):
                current = dict(_shader(code=ONE), materialAsset="MI_Hat", textureResourceId=860,
                               drawIntent="opaque color")
                prior = dict(candidate, materialAsset="MI_Coat", textureResourceId=853,
                             drawIntent="shadow mask")
                verdict = reuse.match(current, prior)
                # Physical matching and its existing cache consumers are unchanged.
                self.assertEqual(verdict["tier"], "bytes")
                self.assertTrue(verdict["evidenceIsConclusive"])
                self.assertEqual(verdict["evidenceScope"], "shader-physical-match")
                self.assertEqual(verdict["suggestedReuse"], ["organizedSource"])
                self.assertFalse(verdict["semanticIdentityVerified"])
                self.assertEqual(set(verdict["semanticEvidenceRequired"]), {
                    "currentMaterialAsset", "currentBoundResources", "currentDrawContext"})


class ReuseToolSemanticScopeTests(unittest.TestCase):
    def test_public_match_and_capture_plan_keep_semantic_parts_as_prior_evidence(self) -> None:
        from src import server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stored_parts = {"organizedSource": "float4 MainPS() { return 1; }",
                            "cbufferNames": {"b5": {"16": "HatTint"}},
                            "aliasTable": {"cbuffer5.unknown[1]": "HatTint"},
                            "drawIntents": [{"intent": "hat opaque color"}]}
            library.publish_entry(root, {**_shader(), **stored_parts})
            with mock.patch.object(server, "_active_project_root_for_library", return_value=root):
                exact = json.loads(server.find_reusable_shader_work(
                    raw_sha256=ONE, interface_hash=TWO, stage="fragment"))
                interface = json.loads(server.find_reusable_shader_work(
                    raw_sha256=THREE, interface_hash=TWO, stage="fragment"))
                plan = json.loads(server.plan_capture_shader_reuse([
                    _shader(), _shader(raw=THREE)]))
                library.record_applied(root, "D:/captures/prior.rdc", [
                    {"rawSha256": ONE, "tier": "bytes", "parts": list(stored_parts)}])
                prior = json.loads(server.find_reusable_shader_work(
                    raw_sha256=ONE, interface_hash=TWO, stage="fragment",
                    capture_path="D:/captures/prior.rdc"))
            for result in (exact, interface, plan, prior):
                self.assertNotIn("error", result, result)
                self.assertFalse(result["applied"])
            self.assertEqual(exact["matches"][0]["suggestedReuse"], ["organizedSource"])
            self.assertEqual(interface["matches"][0]["suggestedReuse"], [])
            self.assertFalse(exact["matches"][0]["semanticIdentityVerified"])
            self.assertFalse(interface["matches"][0]["semanticIdentityVerified"])
            self.assertEqual(plan["exact"][0]["suggestedReuse"], ["organizedSource"])
            self.assertEqual(plan["interface"][0]["suggestedReuse"], [])
            self.assertIsNotNone(prior["alreadyAppliedHere"])
            self.assertNotIn("next", prior)
            self.assertNotIn("next", plan)
            # Existing named parts are preserved and available for evidence-based review.
            self.assertEqual(library.load_entry(root, ONE)["parts"], stored_parts)


class SourceFingerprintTests(unittest.TestCase):
    def test_generated_names_do_not_change_the_shape(self) -> None:
        """The decompiler renames temporaries between builds; the maths is the same."""

        left = reuse.source_fingerprint(
            "float4 MainPS() { float3 albedo = tex.Sample(samp, uv).rgb;"
            " return float4(albedo * ndotl, 1.0); }", "MainPS")
        right = reuse.source_fingerprint(
            "float4 MainPS() { float3 base = tex.Sample(samp, texcoord).rgb;"
            " return float4(base * lambert, 1.0); }", "MainPS")
        self.assertEqual(left["entryShapeHash"], right["entryShapeHash"])
        self.assertEqual(reuse.compare_source(left, right)["similarity"], 1.0)

    def test_intrinsics_are_not_names_so_swapping_one_does_change_it(self) -> None:
        left = reuse.source_fingerprint("float f() { return dot(a, b); }")
        right = reuse.source_fingerprint("float f() { return cross(a, b); }")
        self.assertNotEqual(left["shapeHashes"], right["shapeHashes"])

    def test_comments_and_formatting_do_not_change_anything(self) -> None:
        left = reuse.source_fingerprint("float f() { return a + b; }")
        right = reuse.source_fingerprint(
            "float f() {\n    // sum them\n    return a\n        + b;\n}")
        self.assertEqual(left["shapeHashes"], right["shapeHashes"])

    def test_similarity_is_weighted_by_size_because_boilerplate_is_everywhere(self) -> None:
        """Measured on two real decompiled Shaders: 10 shared helpers, nothing alike.

        A decompiler emits the same tiny accessors into every file. Counting functions
        called those two Shaders 10% similar; weighting by token count called them
        0.65% similar, which is the truth.
        """

        boilerplate = ("uint loadWord(uint i) { return asuint(buf.Load(i)); } "
                       "void discardPixel(bool c) { if (c) discard; } ")
        big_body = " ".join("float v%d = lerp(x%d, y%d, w%d);" % (n, n, n, n)
                            for n in range(120))
        left = reuse.source_fingerprint(
            boilerplate + "float4 MainPS() { %s return 1; }" % big_body, "MainPS")
        right = reuse.source_fingerprint(
            boilerplate + "float4 MainPS() { return float4(1,0,0,1); }", "MainPS")
        comparison = reuse.compare_source(left, right)
        self.assertEqual(comparison["sharedFunctionCount"], 2)
        self.assertFalse(comparison["entryPointMatches"])
        self.assertGreater(comparison["similarity"], 0.4)
        self.assertLess(comparison["weightedSimilarity"], 0.1)

    def test_the_shared_and_unshared_functions_are_named(self) -> None:
        left = reuse.source_fingerprint(
            "float shared() { return a * b; } float only() { return c - d; }")
        right = reuse.source_fingerprint("float shared() { return q * r; }")
        comparison = reuse.compare_source(left, right)
        self.assertEqual(comparison["sharedFunctionNames"], ["shared"])
        self.assertEqual(comparison["onlyInLeft"], ["only"])

    def test_a_file_with_no_functions_is_empty_rather_than_an_error(self) -> None:
        fingerprint = reuse.source_fingerprint("cbuffer B { float x; };")
        self.assertEqual(fingerprint["functionCount"], 0)
        self.assertEqual(fingerprint["entryShapeHash"], "")


class BestMatchTests(unittest.TestCase):
    def test_the_strongest_tier_wins_and_the_others_are_kept(self) -> None:
        report = reuse.best_match(
            _shader(raw=ONE, interface=TWO),
            [
                {"entryId": "weak", "rawSha256": THREE, "interfaceHash": TWO,
                 "stage": "fragment", "entryPoint": "MainPS"},
                {"entryId": "exact", "rawSha256": ONE, "interfaceHash": TWO,
                 "stage": "fragment", "entryPoint": "MainPS"},
            ])
        self.assertEqual(report["tier"], "bytes")
        self.assertEqual(report["matches"][0]["candidate"]["entryId"], "exact")
        # The weaker match is reported too: a second entry usually means the library
        # has near-duplicates worth merging, and hiding it makes that invisible.
        self.assertEqual(report["matchCount"], 2)

    def test_an_empty_library_is_no_match_rather_than_a_failure(self) -> None:
        report = reuse.best_match(_shader(), [])
        self.assertEqual(report["tier"], "none")
        self.assertEqual(report["matchCount"], 0)


class SharedLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)

    def test_work_is_stored_under_the_shader_not_under_the_capture(self) -> None:
        written = library.publish_entry(self.root, {
            "rawSha256": ONE, "stage": "fragment", "entryPoint": "MainPS",
            "organizedSource": "float4 MainPS() { return 1; }",
            "provenance": {"capturePath": "D:/x/6.rdc", "eventIds": [11329]},
        })
        self.assertIn(ONE, written["path"])
        entry = library.load_entry(self.root, ONE)
        self.assertEqual(entry["parts"]["organizedSource"], "float4 MainPS() { return 1; }")
        self.assertEqual(entry["provenance"]["eventIds"], [11329])

    def test_a_second_capture_adds_to_the_entry_instead_of_erasing_it(self) -> None:
        library.publish_entry(self.root, {
            "rawSha256": ONE, "organizedSource": "expensive work",
            "provenance": {"capturePath": "D:/x/6.rdc"}})
        library.publish_entry(self.root, {
            "rawSha256": ONE, "cbufferNames": {"b0": {"0": "ViewMatrix"}},
            "provenance": {"capturePath": "D:/x/7.rdc"}})
        entry = library.load_entry(self.root, ONE)
        self.assertEqual(entry["partNames"], ["cbufferNames", "organizedSource"])
        self.assertEqual(entry["parts"]["organizedSource"], "expensive work")
        # The capture that produced the earlier work is not forgotten.
        self.assertEqual(entry["history"][0]["capturePath"], "D:/x/6.rdc")

    def test_overwriting_has_to_be_asked_for(self) -> None:
        library.publish_entry(self.root, {"rawSha256": ONE, "organizedSource": "first"})
        library.publish_entry(self.root, {"rawSha256": ONE, "notes": "second"},
                              replace_parts=True)
        entry = library.load_entry(self.root, ONE)
        self.assertEqual(entry["partNames"], ["notes"])

    def test_only_the_parts_asked_for_are_read_back(self) -> None:
        library.publish_entry(self.root, {
            "rawSha256": ONE, "organizedSource": "a very large file",
            "cbufferNames": {"b0": {}}})
        entry = library.load_entry(self.root, ONE, parts=("cbufferNames",))
        self.assertEqual(entry["partNames"], ["cbufferNames"])
        self.assertNotIn("organizedSource", entry["parts"])

    def test_an_entry_that_stores_nothing_is_refused(self) -> None:
        with self.assertRaises(library.SharedShaderLibraryError):
            library.publish_entry(self.root, {"rawSha256": ONE})
        with self.assertRaises(library.SharedShaderLibraryError):
            library.publish_entry(self.root, {"rawSha256": "not-a-hash",
                                              "notes": "x"})

    def test_the_shortlist_narrows_by_game_and_stage(self) -> None:
        library.publish_entry(self.root, {
            "rawSha256": ONE, "stage": "fragment", "gameId": "silverpalace",
            "notes": "a"})
        library.publish_entry(self.root, {
            "rawSha256": TWO, "stage": "vertex", "gameId": "silverpalace",
            "notes": "b"})
        library.publish_entry(self.root, {
            "rawSha256": THREE, "stage": "fragment", "gameId": "other-game",
            "notes": "c"})
        found = {item["rawSha256"] for item in library.candidates(
            self.root, stage="fragment", game_id="silverpalace")}
        self.assertEqual(found, {ONE})

    def test_the_summary_says_what_is_there_without_reading_every_entry(self) -> None:
        self.assertEqual(library.library_summary(self.root)["entryCount"], 0)
        library.publish_entry(self.root, {"rawSha256": ONE, "stage": "fragment",
                                          "organizedSource": "x"})
        summary = library.library_summary(self.root)
        self.assertEqual(summary["entryCount"], 1)
        self.assertEqual(summary["byStage"], {"fragment": 1})
        self.assertEqual(summary["byPart"], {"organizedSource": 1})

    def test_a_missing_entry_is_none_and_a_missing_library_is_empty(self) -> None:
        self.assertIsNone(library.load_entry(self.root, ONE))
        self.assertFalse(library.library_summary(self.root)["exists"])

    def test_the_library_and_the_matcher_agree_on_what_a_candidate_is(self) -> None:
        """The end-to-end shape: publish under one capture, find from another."""

        text = "float4 MainPS() { return float4(albedo * ndotl, 1); }"
        library.publish_entry(self.root, {
            "rawSha256": ONE, "stage": "fragment", "entryPoint": "MainPS",
            "interfaceHash": TWO, "organizedSource": text,
            "sourceFingerprint": reuse.source_fingerprint(text, "MainPS"),
            "provenance": {"capturePath": "D:/captures/6.rdc"}})

        report = reuse.best_match(
            # A different capture: different path, different ResourceId, same Shader.
            {"rawSha256": ONE, "interfaceHash": TWO, "stage": "fragment",
             "entryPoint": "MainPS"},
            library.candidates(self.root, stage="fragment"))
        self.assertEqual(report["tier"], "bytes")
        self.assertEqual(report["matches"][0]["candidate"]["rawSha256"], ONE)
        self.assertIn("organizedSource", report["matches"][0]["suggestedReuse"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class ComponentIdentityTests(unittest.TestCase):
    """The ABI in parts, so a partial match says which part moved.

    Everything these digests are built from is reflection derived from the bytecode.
    Confirmed against the field data: three catalogs of one .rdc, ResourceIds 853 /
    860 / 12212, identical rawSha256, codeSha256, interfaceHash and layoutHash.
    """

    @staticmethod
    def _reflection(bind=3, semantic="TEXCOORD", cbuffer_size=64):
        return {
            "inputSignature": [{"regIndex": 0, "semanticName": semantic,
                                "compType": "Float"}],
            "outputSignature": [{"regIndex": 0, "semanticName": "SV_Target",
                                 "compType": "Float"}],
            "readOnlyResources": [{"space": 0, "bind": bind,
                                   "resourceType": "Texture2D", "isTexture": True}],
            "samplers": [{"space": 0, "bind": 1}],
            "constantBlocks": [{"name": "Globals", "byteSize": cbuffer_size,
                                "variables": []}],
        }

    def test_the_parts_are_independent(self) -> None:
        base = reuse.component_identity(self._reflection())
        moved_binding = reuse.component_identity(self._reflection(bind=7))
        self.assertEqual(
            reuse.compare_components(base, moved_binding),
            {"signatures": "same", "resourceBindings": "different",
             "cbufferLayout": "same", "agreeing": ["layout", "signature"]})

    def test_a_changed_signature_shows_up_as_the_signature(self) -> None:
        base = reuse.component_identity(self._reflection())
        renamed = reuse.component_identity(self._reflection(semantic="COLOR"))
        comparison = reuse.compare_components(base, renamed)
        self.assertEqual(comparison["signatures"], "different")
        self.assertEqual(comparison["resourceBindings"], "same")

    def test_a_grown_cbuffer_shows_up_as_the_layout(self) -> None:
        base = reuse.component_identity(self._reflection())
        grown = reuse.component_identity(self._reflection(cbuffer_size=96))
        self.assertEqual(reuse.compare_components(base, grown)["cbufferLayout"],
                         "different")

    def test_nothing_capture_local_reaches_a_digest(self) -> None:
        """Same reflection, different capture: the digests must not notice."""

        first = self._reflection()
        second = dict(self._reflection(), shaderResourceId=1000000000000000853,
                      resourceId=1000000000000000853, capturePath="D:/a/6.rdc",
                      eventIds=[11329], name="ps_0")
        third = dict(self._reflection(), shaderResourceId=1000000000000012212,
                     resourceId=1000000000000012212, capturePath="E:/b/7.rdc",
                     eventIds=[42], name="ps_9")
        digests = [reuse.component_identity(record)
                   for record in (first, second, third)]
        self.assertEqual(digests[0], digests[1])
        self.assertEqual(digests[1], digests[2])

    def test_an_absent_side_is_unknown_rather_than_different(self) -> None:
        base = reuse.component_identity(self._reflection())
        comparison = reuse.compare_components(base, {})
        self.assertEqual(comparison["signatures"], "unknown")
        self.assertEqual(comparison["agreeing"], [])

    def test_the_breakdown_travels_with_a_verdict(self) -> None:
        components = reuse.component_identity(self._reflection())
        other = reuse.component_identity(self._reflection(bind=7))
        verdict = reuse.match(dict(_shader(raw=ONE), **components),
                              dict(_shader(raw=ONE), **other))
        self.assertEqual(verdict["tier"], "bytes")
        self.assertEqual(verdict["evidence"]["components"]["resourceBindings"],
                         "different")


class AppliedLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.capture = str(Path(self._directory.name) / "6.rdc")

    def test_an_unknown_capture_is_empty_rather_than_an_error(self) -> None:
        record = library.applied_for_capture(self.root, self.capture)
        self.assertFalse(record["known"])
        self.assertEqual(record["shaders"], {})

    def test_what_was_applied_is_recalled_by_the_shader_s_own_bytes(self) -> None:
        library.record_applied(self.root, self.capture, [
            {"rawSha256": ONE, "tier": "bytes", "workspaceId": "w1",
             "parts": ["organizedSource", "cbufferNames"]}])
        record = library.applied_for_capture(self.root, self.capture)
        self.assertTrue(record["known"])
        self.assertEqual(record["shaders"][ONE]["tier"], "bytes")
        self.assertEqual(record["shaders"][ONE]["parts"],
                         ["cbufferNames", "organizedSource"])
        self.assertTrue(record["shaders"][ONE]["appliedAt"])

    def test_a_second_capture_keeps_its_own_list(self) -> None:
        other = str(Path(self._directory.name) / "7.rdc")
        library.record_applied(self.root, self.capture,
                               [{"rawSha256": ONE, "tier": "bytes"}])
        library.record_applied(self.root, other,
                               [{"rawSha256": TWO, "tier": "interface"}])
        self.assertEqual(
            set(library.applied_for_capture(self.root, self.capture)["shaders"]), {ONE})
        self.assertEqual(
            set(library.applied_for_capture(self.root, other)["shaders"]), {TWO})

    def test_recording_the_same_shader_again_updates_rather_than_duplicates(self) -> None:
        library.record_applied(self.root, self.capture,
                               [{"rawSha256": ONE, "tier": "interface"}])
        library.record_applied(self.root, self.capture,
                               [{"rawSha256": ONE, "tier": "bytes"}])
        shaders = library.applied_for_capture(self.root, self.capture)["shaders"]
        self.assertEqual(len(shaders), 1)
        self.assertEqual(shaders[ONE]["tier"], "bytes")

    def test_an_invented_tier_is_refused(self) -> None:
        with self.assertRaises(library.SharedShaderLibraryError):
            library.record_applied(self.root, self.capture,
                                   [{"rawSha256": ONE, "tier": "probably"}])
        with self.assertRaises(library.SharedShaderLibraryError):
            library.record_applied(self.root, self.capture, [])

    def test_the_same_file_spelled_the_same_way_is_one_entry(self) -> None:
        library.record_applied(self.root, self.capture,
                               [{"rawSha256": ONE, "tier": "bytes"}])
        # Normalisation is the platform's: Windows folds case and separators, POSIX
        # does not, because POSIX paths really are case sensitive.
        self.assertTrue(library.applied_for_capture(
            self.root, str(Path(self.capture)))["known"])


class FieldEvidenceTests(unittest.TestCase):
    """The real numbers this design was built from, kept as a regression.

    Eight catalog exports in one project, of one fragment Shader called MainPS, taken
    from two different .rdc files. RenderDoc handed out six different ResourceIds
    across them -- 747, 754, 853, 860, 3162, 12212 -- and every identity digest was
    byte-identical. Any future change that lets a ResourceId or a capture path back
    into a reuse key will fail here.
    """

    RESOURCE_IDS_BY_CAPTURE = {
        r"D:\Study\抓帧\白银之城\1.rdc": (747, 754, 3162),
        r"D:\Study\抓帧\白银之城\6.rdc": (853, 860, 12212),
    }
    RAW = "3c4afb434934308919e884efa26523f832807f2adc839d3a3754ceb559255205"
    INTERFACE = "7bb7ee6d2f" + "0" * 54
    LAYOUT = "c4b2add76c" + "0" * 54
    CODE = "2a80b22e6d" + "0" * 54

    def _observations(self):
        for capture, resource_ids in self.RESOURCE_IDS_BY_CAPTURE.items():
            for resource_id in resource_ids:
                yield {
                    "rawSha256": self.RAW, "codeSha256": self.CODE,
                    "interfaceHash": self.INTERFACE, "layoutHash": self.LAYOUT,
                    "stage": "fragment", "entryPoint": "MainPS",
                    # The volatile half, which must reach no digest and no key.
                    "shaderResourceId": resource_id, "resourceId": resource_id,
                    "capturePath": capture,
                }

    def test_every_observation_is_the_same_shader(self) -> None:
        observations = list(self._observations())
        self.assertEqual(len(observations), 6)
        first = observations[0]
        for other in observations[1:]:
            verdict = reuse.match(first, other)
            self.assertEqual(verdict["tier"], "bytes", other["capturePath"])
            self.assertTrue(verdict["evidenceIsConclusive"])

    def test_the_identity_is_one_value_across_both_capture_files(self) -> None:
        identities = {json.dumps(reuse.shader_identity(observation), sort_keys=True)
                      for observation in self._observations()}
        self.assertEqual(len(identities), 1)

    def test_one_library_entry_serves_both_capture_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            observations = list(self._observations())
            # Organized once, from whichever capture happened to be open first.
            library.publish_entry(root, {
                "rawSha256": self.RAW, "interfaceHash": self.INTERFACE,
                "stage": "fragment", "entryPoint": "MainPS",
                "organizedSource": "float4 MainPS() { return 1; }",
                "cbufferNames": {"b0": {"0": "ViewMatrix"}},
                "provenance": {"capturePath": observations[0]["capturePath"]}})
            # Found from the other one, whose ResourceIds share nothing with the first.
            report = reuse.best_match(
                observations[-1], library.candidates(root, stage="fragment"))
            self.assertEqual(report["tier"], "bytes")
            self.assertIn("organizedSource", report["matches"][0]["suggestedReuse"])


class SharedLibraryDurabilityTests(unittest.TestCase):
    """What the library stores has to survive being added to.

    Both of these were silent: a later publish that only carried a note rebuilt the
    entry without its identity, and the exact-bytes entry was then filtered out of
    every shortlist by the interface hash it no longer had.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.raw = "a" * 64
        self.interface = "b" * 64

    def _publish(self, **entry):
        # Parts are top-level keys on the entry, not a nested object.
        base = {"rawSha256": self.raw, "stage": "fragment", "entryPoint": "MainPS"}
        base.update(entry)
        return library.publish_entry(self.root, base)

    def test_a_later_publish_does_not_erase_what_the_shader_is(self) -> None:
        self._publish(interfaceHash=self.interface, layoutHash="c" * 64,
                      gameId="silver", organizedSource="float4 main() {}")
        # A second capture adds only CBuffer names.
        self._publish(cbufferNames={"0": "PerView"})

        entry = library.load_entry(self.root, self.raw)
        self.assertEqual(entry["interfaceHash"], self.interface)
        self.assertEqual(entry["layoutHash"], "c" * 64)
        self.assertEqual(entry["gameId"], "silver")
        self.assertEqual(sorted(entry["parts"]), ["cbufferNames", "organizedSource"])

        # ...and it is still findable by the interface it never restated.
        found = library.candidates(self.root, interface_hash=self.interface)
        self.assertEqual([item["rawSha256"] for item in found], [self.raw])

    def test_the_exact_bytes_entry_is_never_filtered_out(self) -> None:
        """Bytes are proof; no weaker key may hide the entry that needs no judgement."""

        self._publish(interfaceHash=self.interface, gameId="silver",
                      organizedSource="float4 main() {}")

        # A stale or differently-computed interface hash, a different stage name, and
        # another game id: each used to exclude the one certain match.
        for query in ({"interface_hash": "d" * 64},
                      {"stage": "vertex"},
                      {"game_id": "another-game"}):
            found = library.candidates(
                self.root, raw_sha256=self.raw, **query)
            self.assertIn(self.raw, [item["rawSha256"] for item in found], query)

        # Without the bytes, those filters still apply.
        self.assertEqual(
            library.candidates(self.root, interface_hash="d" * 64), [])


def test_two_writers_do_not_lose_each_others_entries(tmp_path) -> None:
    """The library is shared between RenderDoc instances by design.

    ``_atomic_write`` makes each replace atomic, which is not the same as making a
    read-modify-write safe: without the lock the second writer rebuilds the index
    from its own stale snapshot and the first entry disappears from it.
    """

    import threading

    root = tmp_path / "project"
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def publish(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            library.publish_entry(root, {
                "rawSha256": "%064x" % index, "stage": "fragment",
                "entryPoint": "Main%d" % index, "notes": "entry %d" % index})
        except BaseException as error:  # noqa: BLE001 - reported below
            errors.append(error)

    threads = [threading.Thread(target=publish, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    summary = library.library_summary(root)
    assert summary["entryCount"] == 8
    assert len(library.candidates(root, limit=64)) == 8


def test_a_hand_edited_applied_ledger_does_not_crash_the_write(tmp_path) -> None:
    root = library.library_root(tmp_path / "project")
    root.mkdir(parents=True, exist_ok=True)
    (root / "applied.json").write_text(
        json.dumps({"schema": "x", "captures": {"junk": "not an object"}}),
        encoding="utf-8")

    result = library.record_applied(
        tmp_path / "project", "D:/captures/1.rdc",
        [{"rawSha256": "a" * 64, "tier": "bytes", "parts": ["organizedSource"]}])
    assert result["shaderCount"] == 1


def test_a_broken_index_is_rebuilt_from_the_entries_instead_of_shrinking(tmp_path) -> None:
    """The index is a cache; the entry files are the truth.

    An unreadable index used to be read as empty, and the next publish then wrote
    a complete replacement holding one entry: every other Shader's work stayed on
    disk and became invisible to every lookup, with nothing able to rebuild it.
    """

    root = tmp_path / "project"
    for index in range(3):
        library.publish_entry(root, {"rawSha256": "%064x" % (index + 1), "stage": "fragment",
                                     "entryPoint": "Main", "notes": "n%d" % index})
    index_path = library.library_root(root) / "index.json"
    index_path.write_text("{ this is not json", encoding="utf-8")

    # A reader sees the rebuilt view without touching the file...
    assert len(library.candidates(root, limit=64)) == 3
    assert index_path.read_text(encoding="utf-8").startswith("{ this")
    # ...and the next publish persists a complete index, not a one-entry one.
    library.publish_entry(root, {"rawSha256": "%064x" % 9, "stage": "fragment",
                                 "entryPoint": "Main", "notes": "new"})
    assert library.library_summary(root)["entryCount"] == 4
    # An explicit rebuild is available too.
    index_path.unlink()
    assert library.rebuild_index(root)["entryCount"] == 4


def test_a_shrunk_but_valid_index_is_repaired_from_the_entry_folders(tmp_path):
    """The old bug left a parseable index holding one entry; parseable is not complete."""

    import json

    from src import shared_shader_library as library

    for index, raw in enumerate(("a" * 64, "b" * 64, "c" * 64)):
        library.publish_entry(tmp_path, {
            "rawSha256": raw, "stage": "fragment", "entryPoint": "main",
            "api": "D3D12", "organizedSource": "float4 main%d() {}" % index,
        })
    index_path = library.library_root(tmp_path) / library.INDEX_FILENAME
    document = json.loads(index_path.read_text(encoding="utf-8"))
    assert len(document["entries"]) == 3
    document["entries"] = {"a" * 64: document["entries"]["a" * 64]}
    document["entryCount"] = 1
    index_path.write_text(json.dumps(document), encoding="utf-8")

    summary = library.library_summary(tmp_path)
    assert summary["entryCount"] == 3
    assert {item["rawSha256"] for item in library.candidates(tmp_path, stage="fragment")} >= {
        "a" * 64, "b" * 64, "c" * 64}
