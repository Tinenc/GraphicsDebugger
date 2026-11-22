import copy
import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from eid_portable_forward_profile import (BACKENDS, LIVE_FORWARD_ENV, complete_witness,
                                          configure_profile_environment, intermediate_coverage,
                                          live_evidence, main, profile_environment, requested_ranges,
                                          select_events, sequence, viewport_probe)


class PortableForwardEvidenceTests(unittest.TestCase):
    def hit(self, api):
        row = {"api": BACKENDS[api], "from": 31, "target": 43, "enabled": 1,
               "hit": 1, "initialRestoreSkipped": 1}
        if api in ("OpenGL", "OpenGLES"):
            row.update(replayedFirst=32, replayedLast=42, succeeded=1)
        else:
            row.update(replayedEvents=11)
            if api == "D3D11":
                row.update(range="32-42", success=1)
            else:
                row.update(succeeded=1)
        return row

    def test_each_backend_requires_its_exact_native_range(self):
        for api in BACKENDS:
            with self.subTest(api=api):
                row = self.hit(api)
                self.assertEqual(live_evidence([row], api, 31, 43, require_hit=True)["matchingHits"], 1)
                for field, value in (("from", 30), ("target", 42), ("enabled", 0),
                                     ("initialRestoreSkipped", 0), ("hit", True)):
                    broken = dict(row, **{field: value})
                    result = live_evidence([broken], api, 31, 43, require_hit=True)
                    self.assertTrue(result["issues"])
                    self.assertEqual(result["matchingHits"], 0)

    def test_backend_interval_fields_are_not_interchangeable(self):
        for api in BACKENDS:
            row = self.hit(api)
            field = "replayedFirst" if api in ("OpenGL", "OpenGLES") else "replayedEvents"
            row[field] += 1
            self.assertTrue(live_evidence([row], api, 31, 43, require_hit=True)["issues"])
        row = dict(self.hit("D3D11"), range="31-43")
        self.assertTrue(live_evidence([row], "D3D11", 31, 43, require_hit=True)["issues"])

    def test_missing_failed_duplicate_or_wrong_backend_cannot_pass(self):
        for api in BACKENDS:
            row = self.hit(api)
            self.assertTrue(live_evidence([], api, 31, 43)["issues"])
            self.assertTrue(live_evidence([row, row], api, 31, 43)["issues"])
            row["success" if api == "D3D11" else "succeeded"] = 0
            self.assertEqual(live_evidence([row], api, 31, 43)["matchingHits"], 0)
        self.assertTrue(live_evidence([self.hit("Vulkan")], "D3D11", 31, 43)["issues"])

    def test_force_disabled_and_backward_controls_reject_any_hit(self):
        for api in BACKENDS:
            row = self.hit(api)
            for arguments in ({"force": True}, {"disabled": True}, {"allow_hit": False}):
                self.assertTrue(live_evidence([row], api, 31, 43, **arguments)["issues"])
            self.assertTrue(live_evidence([row], api, 44, 43)["issues"])
        # Vulkan's intentional lack of force root logs is allowed only for a forced call.
        self.assertFalse(live_evidence([], "Vulkan", 31, 43, force=True)["issues"])

    def test_honest_fallback_is_diagnostic_but_not_positive_acceptance(self):
        for api in BACKENDS:
            row = dict(self.hit(api), hit=0, initialRestoreSkipped=0)
            if api not in ("OpenGL", "OpenGLES"):
                row["replayedEvents"] = 0
            self.assertFalse(live_evidence([row], api, 31, 43)["issues"])
            self.assertTrue(live_evidence([row], api, 31, 43, require_hit=True)["issues"])
            row["enabled"] = 0
            self.assertFalse(live_evidence([row], api, 31, 43, disabled=True)["issues"])

    def test_four_draw_sequence_and_actual_eid_discovery(self):
        events = [31, 35, 39, 43]
        self.assertEqual(select_events(events), events)
        self.assertEqual(select_events([1] + events + [51], "31,35,39,43"), events)
        self.assertEqual([row[1] for row in sequence(events)], [31, 35, 43, 31, 39, 43])
        for draws, chosen in ((events + [51], ""), (events, "31,35,39"),
                              ([31, 35, 37, 39, 43], "31,35,39,43"),
                              (events, "31,39,35,43"), (events, "31,35,39,39")):
            with self.assertRaises(ValueError):
                select_events(draws, chosen)

    def test_environment_and_expected_ranges_are_explicit(self):
        enabled, disabled = profile_environment(False), profile_environment(True)
        for key in ("RENDERDOC_D3D11_ENABLE_LIVE_FORWARD", "RENDERDOC_GL_ENABLE_LIVE_FORWARD",
                    "RENDERDOC_VULKAN_ENABLE_LIVE_FORWARD"):
            self.assertEqual(enabled[key], "1")
            self.assertEqual(disabled[key], "0")
        self.assertEqual(enabled["RENDERDOC_REPLAY_CHECKPOINTS"], "0")
        self.assertEqual(requested_ranges("AB,BD"), ["AB", "BD"])
        for text in ("AD", "AB,AB", "ab", "AB,"):
            with self.assertRaises(ValueError):
                requested_ranges(text)

    def test_backend_default_removes_inherited_overrides_and_records_actual_absence(self):
        for inherited in ("0", "1", "", None):
            with self.subTest(inherited=inherited):
                environment = {"unrelated": "preserved"}
                if inherited is not None:
                    environment.update({key: inherited for key in LIVE_FORWARD_ENV})
                evidence = configure_profile_environment(False, True, environment)
                for key in LIVE_FORWARD_ENV:
                    self.assertNotIn(key, environment)
                    self.assertIsNone(evidence["values"][key])
                    self.assertFalse(evidence["present"][key])
                self.assertEqual(environment["unrelated"], "preserved")
                self.assertEqual(evidence["values"]["RENDERDOC_EID_TIMING"], "1")
                self.assertEqual(evidence["values"]["RENDERDOC_REPLAY_CHECKPOINTS"], "0")
                self.assertTrue(evidence["present"]["RENDERDOC_REPLAY_CHECKPOINTS"])

    def test_explicit_modes_override_inherited_environment_and_reject_conflicting_modes(self):
        for disabled in (False, True):
            environment = {key: "stale" for key in LIVE_FORWARD_ENV}
            evidence = configure_profile_environment(disabled, environ=environment)
            for key in LIVE_FORWARD_ENV:
                self.assertEqual(environment[key], "0" if disabled else "1")
                self.assertEqual(evidence["values"][key], environment[key])
                self.assertTrue(evidence["present"][key])
        with self.assertRaises(ValueError):
            profile_environment(True, True)

    def test_default_cli_requires_exact_hit_ranges_and_is_exclusive_with_disabled(self):
        base = ["--capture", "unused.rdc", "--output", "unused.json"]
        for flags, expected in ((["--default-live-forward"], "requires explicit --expect-hit-ranges"),
                                (["--default-live-forward", "--disable-live-forward"],
                                 "not allowed with argument"),
                                (["--default-live-forward", "--expect-hit-ranges", "AB,AD"],
                                 "distinct members of AB,BD,AC,CD")):
            with self.subTest(flags=flags):
                errors = io.StringIO()
                with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as stopped:
                    main(base + flags)
                self.assertEqual(stopped.exception.code, 2)
                self.assertIn(expected, errors.getvalue())


class PortableViewportTests(unittest.TestCase):
    def fixture(self):
        return (SimpleNamespace(x=100, y=20, width=100, height=200),
                SimpleNamespace(width=400, height=300, depth=1, mips=1, arraysize=1),
                SimpleNamespace(resource="ResourceId::1", firstMip=0, numMips=1,
                                firstSlice=0, numSlices=1))

    def test_gl_origin_is_normalized_and_negative_vk_viewport_supported(self):
        vp, tex, binding = self.fixture()
        self.assertEqual(viewport_probe(31, "D3D11", vp, tex, binding)["y"], 120)
        self.assertEqual(viewport_probe(31, "OpenGL", vp, tex, binding)["y"], 179)
        self.assertEqual(viewport_probe(31, "OpenGLES", vp, tex, binding)["y"], 179)
        vp.y, vp.height = 220, -200
        self.assertEqual(viewport_probe(31, "Vulkan", vp, tex, binding)["y"], 120)

    def test_invalid_or_multiview_probe_is_rejected(self):
        for change in (lambda v, t, b: setattr(v, "width", 0),
                       lambda v, t, b: setattr(v, "x", float("nan")),
                       lambda v, t, b: setattr(v, "x", 500),
                       lambda v, t, b: setattr(b, "numSlices", 2),
                       lambda v, t, b: setattr(t, "depth", 4),
                       lambda v, t, b: setattr(b, "firstMip", 3)):
            vp, tex, binding = self.fixture()
            change(vp, tex, binding)
            with self.assertRaises(ValueError):
                viewport_probe(31, "D3D11", vp, tex, binding)


def reference(event, events):
    return {"eventId": event, "regions": [
        {"eventId": region, "resource": "ResourceId::1", "subresource": [0, 0, 0],
         "x": index * 100 + 50, "y": 100, "uintBits": [region if region <= event else 0, 0, 0, 1]}
        for index, region in enumerate(events)]}


class PortableContributionTests(unittest.TestCase):
    events = [31, 35, 39, 43]

    def refs(self):
        return {event: [reference(event, self.events), reference(event, self.events)] for event in self.events}

    def test_both_middle_regions_must_change_and_persist(self):
        rows, issues = intermediate_coverage(self.refs(), self.events)
        self.assertFalse(issues)
        self.assertTrue(all(row["changedAtOwnDraw"] and row["persistedThroughD"] for row in rows))

    def test_prior_frame_residue_does_not_prove_skipped_draw(self):
        refs = self.refs()
        for value in refs[31]:
            value["regions"][1]["uintBits"] = [35, 0, 0, 1]
        self.assertTrue(intermediate_coverage(refs, self.events)[1])

    def test_overwritten_missing_moved_or_unstable_region_cannot_pass(self):
        for modify in (lambda r: r[43][0]["regions"][1].update(uintBits=[99, 0, 0, 1]),
                       lambda r: r[43][0]["regions"].pop(1),
                       lambda r: r[35][0]["regions"][1].update(x=999)):
            refs = self.refs()
            modify(refs)
            self.assertTrue(intermediate_coverage(refs, self.events)[1])
            for event in refs:
                refs[event][1] = copy.deepcopy(refs[event][0])
            self.assertTrue(intermediate_coverage(refs, self.events)[1])

    def test_complete_witness_reuses_bound_hash_and_reads_unbound_output(self):
        bound = {"eventId": 31, "pixels": [{"resource": "r1", "subresource": [0, 0, 0],
                    "bytes": 4, "sha256": "bound"}]}
        calls = []
        controller = SimpleNamespace(
            GetTextureData=lambda resource, sub: calls.append((resource, sub)) or b"abcd",
            GetFatalErrorStatus=lambda: SimpleNamespace(code=0))
        rd = SimpleNamespace(Subresource=lambda *sub: sub, ResultCode=SimpleNamespace(Succeeded=0))
        with patch("eid_portable_forward_profile.output_witness", return_value=copy.deepcopy(bound)):
            result = complete_witness(controller, rd, [("r1", (0, 0, 0)), ("r2", (0, 0, 0))],
                                      {"r1": "r1", "r2": "r2"}, [], 8)
        self.assertEqual(calls, [("r2", (0, 0, 0))])
        self.assertEqual([row["resource"] for row in result["pixels"]], ["r1", "r2"])
        with patch("eid_portable_forward_profile.output_witness", return_value=copy.deepcopy(bound)):
            with self.assertRaises(RuntimeError):
                complete_witness(controller, rd, [("r2", (0, 0, 0))], {"r2": "r2"}, [], 8)
        with patch("eid_portable_forward_profile.output_witness", return_value=copy.deepcopy(bound)):
            with self.assertRaises(RuntimeError):
                complete_witness(controller, rd, [("r1", (0, 0, 0)), ("r2", (0, 0, 0))],
                                 {"r1": "r1", "r2": "r2"}, [], 7)


if __name__ == "__main__":
    unittest.main()
