"""A view of what a draw is: recorded, revised, checked back — and never decided here.

An earlier version of this scored a table and announced an answer. These tests pin the
shape that replaced it: the Host gathers evidence and draws no conclusion, the record
is free text written by whoever knows, revising beats duplicating, verification is
deduplicated, and a contradiction is never quietly kept.
"""

from __future__ import annotations

import asyncio
import json
import base64
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from src import draw_intent, server


def _png_bytes(width: int = 4, height: int = 4) -> bytes:
    """A real, minimal PNG, so the inline path is exercised on a file and not a stub."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _summary(blocks):
    """The receipt from an image tool result, which is a list of content blocks."""

    return json.loads(blocks[0])


class RecordShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "draw-intents.json"
        self.addCleanup(self._directory.cleanup)

    def test_an_intent_is_free_text_at_whatever_granularity_fits(self) -> None:
        for wording in ("the stockings", "second specular pass on hair",
                        "whatever writes the velocity buffer", "不确定，看着像布料"):
            record = draw_intent.validate_record({"intent": wording, "eventIds": [12]})
            self.assertEqual(record["intent"], wording)
            self.assertEqual(record["confidence"], "working")

    def test_an_empty_intent_is_refused_because_it_records_nothing(self) -> None:
        with self.assertRaises(draw_intent.DrawIntentError):
            draw_intent.validate_record({"intent": "   "})
        with self.assertRaises(draw_intent.DrawIntentError):
            draw_intent.validate_record({"intent": "x", "confidence": "0.73"})

    def test_the_record_says_what_it_applies_to(self) -> None:
        record = draw_intent.upsert_record({
            "intent": "the stockings", "eventIds": [4213],
            "capturePath": "D:/caps/a.rdc", "shaderFingerprint": "abc123",
            "targetSignature": "R11G11B10", "markerPath": "Frame/BasePass/Character",
        }, self.path)
        self.assertEqual(record["eventIds"], [4213])
        self.assertEqual(record["capturePath"], "D:/caps/a.rdc")
        self.assertEqual(record["markerPath"], "Frame/BasePass/Character")

    def test_the_same_shader_at_another_event_joins_the_record_it_has(self) -> None:
        """Two event ids drawing one shader is one thing, not two records."""

        draw_intent.upsert_record({"intent": "the stockings", "eventIds": [4213],
                                   "shaderFingerprint": "abc123"}, self.path)
        second = draw_intent.upsert_record({"intent": "the stockings", "eventIds": [4590],
                                            "shaderFingerprint": "abc123"}, self.path)
        records, _ = draw_intent.load_records(self.path)
        self.assertEqual(len(records), 1)
        self.assertEqual(second["eventIds"], [4213, 4590])
        self.assertEqual(second["revision"], 1)

    def test_recording_a_different_intent_revises_rather_than_duplicating(self) -> None:
        draw_intent.upsert_record({"intent": "some transparent thing", "eventIds": [7],
                                   "shaderFingerprint": "f1"}, self.path)
        revised = draw_intent.upsert_record({"intent": "sheer stockings", "eventIds": [7],
                                             "shaderFingerprint": "f1"}, self.path)
        records, _ = draw_intent.load_records(self.path)
        self.assertEqual(len(records), 1)
        self.assertEqual(revised["revision"], 2)
        self.assertEqual(revised["supersededIntent"], "some transparent thing")

    def test_a_user_stated_intent_is_recorded_as_theirs(self) -> None:
        record = draw_intent.upsert_record({
            "intent": "丝袜", "source": "user", "confidence": "user-stated",
            "searchTerms": ["丝袜", "Stocking"], "eventIds": [9]}, self.path)
        self.assertEqual(record["source"], "user")
        self.assertEqual(record["confidence"], "user-stated")
        self.assertIn("丝袜", record["searchTerms"])


class VerificationTests(unittest.TestCase):
    """Checking the guess against the code, without asking the same question twice."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "draw-intents.json"
        self.record = draw_intent.upsert_record(
            {"intent": "sheer stockings", "eventIds": [7], "shaderFingerprint": "f1",
             "algorithms": ["Charlie sheen", "inverted Fresnel opacity"]}, self.path)
        self.addCleanup(self._directory.cleanup)

    def test_a_found_expectation_corroborates_a_working_intent(self) -> None:
        updated, repeats = draw_intent.apply_checks(
            self.record, [{"expected": "a Charlie sheen lobe", "outcome": "found"}])
        self.assertEqual(updated["confidence"], "corroborated")
        self.assertEqual(repeats, [])

    def test_the_same_check_twice_costs_nothing_and_changes_nothing(self) -> None:
        once, _ = draw_intent.apply_checks(
            self.record, [{"expected": "a Charlie sheen lobe", "outcome": "found"}])
        twice, repeats = draw_intent.apply_checks(
            once, [{"expected": "A Charlie   sheen lobe!", "outcome": "found"}])
        # Reported in the normalised form that made it a repeat, so it is obvious why.
        self.assertEqual(repeats, ["A Charlie sheen lobe!"])
        self.assertEqual(len(twice["checks"]), 1)

    def test_a_contradiction_downgrades_the_record_rather_than_being_dropped(self) -> None:
        updated, _ = draw_intent.apply_checks(self.record, [
            {"expected": "a Charlie sheen lobe", "outcome": "found"},
            {"expected": "alpha rising with 1-NdotV", "outcome": "contradicted",
             "note": "alpha comes straight from a texture"},
        ])
        self.assertEqual(updated["confidence"], "contradicted")
        self.assertEqual(len(updated["checks"]), 2)

    def test_re_answering_a_check_differently_is_not_a_repeat(self) -> None:
        once, _ = draw_intent.apply_checks(
            self.record, [{"expected": "a ramp texture", "outcome": "inconclusive"}])
        twice, repeats = draw_intent.apply_checks(
            once, [{"expected": "a ramp texture", "outcome": "absent"}])
        self.assertEqual(repeats, [])
        self.assertEqual(twice["checks"][0]["outcome"], "absent")

    def test_a_revised_intent_does_not_inherit_the_old_claim_s_checks(self) -> None:
        verified, _ = draw_intent.apply_checks(
            self.record, [{"expected": "a Charlie sheen lobe", "outcome": "found"}])
        draw_intent.save_records([verified], self.path)
        revised = draw_intent.upsert_record(
            {"intent": "hair, not stockings", "eventIds": [7], "shaderFingerprint": "f1"},
            self.path)
        self.assertEqual(revised["revision"], 2)
        self.assertEqual(revised["checks"], [])


class LookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "draw-intents.json"
        draw_intent.upsert_record({"intent": "the stockings", "eventIds": [4213],
                                   "capturePath": "D:/a.rdc", "shaderFingerprint": "f1"},
                                  self.path)
        self.addCleanup(self._directory.cleanup)

    def test_the_shader_is_the_better_key_because_event_ids_move(self) -> None:
        records, _ = draw_intent.load_records(self.path)
        found = draw_intent.find_record(records, shader_fingerprint="f1", event_id=99999)
        self.assertEqual(found["intent"], "the stockings")

    def test_an_event_id_alone_still_finds_it_within_the_same_capture(self) -> None:
        records, _ = draw_intent.load_records(self.path)
        self.assertIsNotNone(
            draw_intent.find_record(records, capture_path="D:/a.rdc", event_id=4213))
        self.assertIsNone(
            draw_intent.find_record(records, capture_path="D:/other.rdc", event_id=4213))

    def test_an_unreadable_file_reports_it_instead_of_raising(self) -> None:
        self.path.write_text("{ not json", encoding="utf-8")
        records, diagnostics = draw_intent.load_records(self.path)
        self.assertEqual(records, [])
        self.assertTrue(diagnostics)


class GeometryFootprintTests(unittest.TestCase):
    def test_a_fullscreen_triangle_covers_the_clip_volume(self) -> None:
        state = server._draw_intent_geometry(
            {"positions": [[-1, -1, 0, 1], [3, -1, 0, 1], [-1, 3, 0, 1]]})
        self.assertGreaterEqual(state["ndcCoverage"], 0.99)

    def test_the_same_vertex_count_in_one_corner_covers_almost_nothing(self) -> None:
        state = server._draw_intent_geometry(
            {"positions": [[-1.0, -1.0, 0, 1], [-0.9, -1.0, 0, 1], [-1.0, -0.9, 0, 1]]})
        self.assertLess(state["ndcCoverage"], 0.01)

    def test_perspective_positions_are_divided_by_w(self) -> None:
        state = server._draw_intent_geometry(
            {"positions": [{"x": -2.0, "y": -2.0, "z": 0.0, "w": 2.0},
                           {"x": 2.0, "y": 2.0, "z": 0.0, "w": 2.0}]})
        self.assertEqual(state["ndcBounds"]["minX"], -1.0)
        self.assertEqual(state["ndcBounds"]["maxX"], 1.0)

    def test_nothing_usable_reports_nothing_rather_than_zero(self) -> None:
        self.assertEqual(server._draw_intent_geometry({"positions": []}), {})
        self.assertEqual(server._draw_intent_geometry({"positions": ["nonsense"]}), {})


class ResourceGraphTests(unittest.TestCase):
    """Relationships only: who wrote what this event reads, who reads what it writes."""

    def _graph(self, responses: dict, **kwargs: object) -> dict:
        def call(name, arguments=None, timeout=None):
            if name == "find_by_resource":
                key = "find_by_resource:%s" % (arguments or {}).get("resource_id")
                return json.dumps(responses.get(key, {"candidates": []}))
            if name == "inspect_event":
                key = "inspect_event:%s" % (arguments or {}).get("event_id")
                return json.dumps(responses.get(key, responses.get("inspect_event", {})))
            return json.dumps(responses.get(name, {"error": "unavailable"}))

        with mock.patch.object(server, "_call", side_effect=call):
            return json.loads(server.get_event_resource_graph(event_id=30, **kwargs))

    def test_producers_and_consumers_are_split_by_event_order(self) -> None:
        payload = self._graph({
            "inspect_event:30": {"pipeline": {"colorTargets": [
                {"resourceId": 77, "name": "SceneColor", "format": "R11G11B10_FLOAT"}]}},
            "get_bound_textures": {"textures": []},
            "find_by_resource:77": {"resourceDisplayName": "SceneColor", "count": 3,
                                    "candidates": [
                {"eventId": 12, "usage": "ColorTarget", "actionName": "Sky",
                 "markerPath": ["Frame"], "isDrawDispatch": True},
                {"eventId": 30, "usage": "ColorTarget", "actionName": "Legs"},
                {"eventId": 44, "usage": "PS_Resource", "actionName": "Tonemap",
                 "markerPath": ["Frame", "Post"], "isDrawDispatch": True},
            ]},
        })
        self.assertEqual(payload["evidenceClass"], "observed")
        edge = payload["edges"][0]
        self.assertEqual(edge["resourceName"], "SceneColor")
        self.assertEqual([row["eventId"] for row in edge["before"]], [12])
        self.assertEqual([row["eventId"] for row in edge["after"]], [44])
        # The engine's own names travel with the relationship.
        self.assertEqual(edge["after"][0]["usage"], "PS_Resource")
        self.assertEqual(edge["after"][0]["markerPath"], ["Frame", "Post"])

    def test_the_graph_states_no_conclusion_and_admits_its_limits(self) -> None:
        payload = self._graph({
            "inspect_event:30": {"pipeline": {}},
            "get_bound_textures": {"textures": [{"resourceId": 5}]},
            "find_by_resource:5": {"candidates": []},
        })
        for absent in ("hypotheses", "vocabulary", "intent", "state"):
            self.assertNotIn(absent, payload)
        self.assertTrue(any("not causation" in item for item in payload["limitations"]))

    def test_depth_follows_one_hop_further_through_draw_events(self) -> None:
        payload = self._graph({
            "inspect_event:30": {"pipeline": {"colorTargets": [{"resourceId": 77}]}},
            "inspect_event:12": {"pipeline": {"colorTargets": [{"resourceId": 88}]}},
            "get_bound_textures": {"textures": []},
            "find_by_resource:77": {"candidates": [
                {"eventId": 12, "usage": "ColorTarget", "isDrawDispatch": True}]},
            "find_by_resource:88": {"candidates": [
                {"eventId": 4, "usage": "ColorTarget", "isDrawDispatch": True}]},
        }, depth=2)
        reached = {edge["eventId"] for edge in payload["edges"]}
        self.assertEqual(reached, {30, 12})

    def test_a_bad_event_id_is_refused(self) -> None:
        self.assertIn("positive integer",
                      json.loads(server.get_event_resource_graph(event_id=0))["error"])


class CropTests(unittest.TestCase):
    """Context is finite, so the picture is cropped to where the draw actually landed."""

    def test_a_drawn_box_becomes_a_pixel_rectangle_with_the_y_axis_flipped(self) -> None:
        # A box in the upper-left quadrant of NDC is the upper-left of the image.
        rectangle = server._crop_from_ndc(
            {"minX": -1.0, "maxX": 0.0, "minY": 0.0, "maxY": 1.0},
            {"width": 1000, "height": 1000}, 0.0)
        self.assertEqual(rectangle, {"x": 0, "y": 0, "width": 500, "height": 500})

    def test_the_margin_widens_the_box_by_a_fraction_of_its_own_size(self) -> None:
        tight = server._crop_from_ndc({"minX": -0.2, "maxX": 0.2, "minY": -0.2, "maxY": 0.2},
                                      {"width": 1000, "height": 1000}, 0.0)
        loose = server._crop_from_ndc({"minX": -0.2, "maxX": 0.2, "minY": -0.2, "maxY": 0.2},
                                      {"width": 1000, "height": 1000}, 0.5)
        self.assertEqual(tight["width"], 200)
        self.assertEqual(loose["width"], 400)
        self.assertLess(loose["x"], tight["x"])

    def test_the_rectangle_never_leaves_the_target(self) -> None:
        rectangle = server._crop_from_ndc(
            {"minX": -1.0, "maxX": 1.0, "minY": -1.0, "maxY": 1.0},
            {"width": 800, "height": 600}, 2.0)
        # A box already covering everything is not worth cropping.
        self.assertIsNone(rectangle)
        edge = server._crop_from_ndc({"minX": 0.9, "maxX": 1.0, "minY": 0.9, "maxY": 1.0},
                                     {"width": 800, "height": 600}, 1.0)
        self.assertLessEqual(edge["x"] + edge["width"], 800)
        self.assertLessEqual(edge["y"] + edge["height"], 600)

    def test_a_degenerate_box_is_not_cropped_to(self) -> None:
        self.assertIsNone(server._crop_from_ndc(
            {"minX": 0.5, "maxX": 0.5, "minY": 0.0, "maxY": 1.0},
            {"width": 100, "height": 100}, 0.0))
        self.assertIsNone(server._crop_from_ndc(
            {"minX": -1, "maxX": 1, "minY": -1, "maxY": 1}, {"width": 0, "height": 0}, 0.0))


class DrawImageTests(unittest.TestCase):
    def _capture(self, responses: dict, evidence: dict, **kwargs: object) -> tuple[dict, dict]:
        seen: dict = {}

        def call(name, arguments=None, timeout=None):
            if name == "inspect_event":
                return json.dumps(responses.get("inspect_event", {}))
            return json.dumps(responses.get(name, {"error": "unavailable"}))

        def save(**arguments):
            seen.update(arguments)
            return json.dumps(evidence)

        with mock.patch.object(server, "_call", side_effect=call), \
                mock.patch.object(server, "_capture_event_texture_evidence_receipt", side_effect=save):
            return _summary(server.capture_draw_image(event_id=30, **kwargs)), seen

    def test_the_crop_is_worked_out_from_where_the_draw_landed(self) -> None:
        payload, seen = self._capture(
            {"inspect_event": {"pipeline": {"colorTargets": [
                {"resourceId": 1, "width": 1000, "height": 1000}]}},
             "get_post_vs_data": {"eventId": 30, "complete": True, "allInstances": True,
                                  "finalPositionStage": True,
                                  "coordinateSpace": "clip", "indices": [0, 1, 2],
                                  "viewport": {"x": 0, "y": 0, "width": 1000, "height": 1000},
                                  "positions": [[-1, 0, 0, 1], [0, 0, 0, 1], [-1, 1, 0, 1]]}},
            {"artifact": {"status": "saved", "output": {"path": "C:/e/rt.png", "byteSize": 900,
                                                        "reduction": {"scaledTo": {"width": 256}}}}},
            margin=0.0, max_dimension=256)
        self.assertTrue(payload["saved"])
        self.assertEqual(seen["crop"], {"x": 0, "y": 0, "width": 500, "height": 500})
        self.assertEqual(seen["max_dimension"], 256)
        self.assertEqual(payload["path"], "C:/e/rt.png")

    def test_the_whole_target_is_kept_when_asked(self) -> None:
        unused, seen = self._capture(
            {"inspect_event": {"pipeline": {"colorTargets": [{"width": 100, "height": 100}]}}},
            {"artifact": {"status": "saved", "output": {"path": "C:/e/rt.png"}}},
            crop_to_draw=False)
        del unused
        self.assertIsNone(seen["crop"])

    def test_a_target_that_cannot_be_saved_reports_it_rather_than_failing(self) -> None:
        payload, unused = self._capture(
            {"inspect_event": {"pipeline": {}}},
            {"error": "no render target is bound"})
        del unused
        self.assertFalse(payload["saved"])
        self.assertIn("no render target", payload["reason"])

    def test_a_bad_event_id_is_refused(self) -> None:
        self.assertIn("positive integer",
                      _summary(server.capture_draw_image(event_id=0))["error"])


class CropForwardingTests(unittest.TestCase):
    """A crop rectangle is four whole pixels or it is nothing."""

    def test_a_valid_rectangle_passes_through(self) -> None:
        self.assertEqual(server._event_texture_crop({"x": 1, "y": 2, "width": 3, "height": 4}),
                         {"x": 1, "y": 2, "width": 3, "height": 4})
        self.assertIsNone(server._event_texture_crop(None))

    def test_anything_else_is_refused(self) -> None:
        for invalid in ({"x": 0, "y": 0, "width": 0, "height": 4},
                        {"x": -1, "y": 0, "width": 1, "height": 1},
                        {"x": 0, "y": 0, "width": "wide", "height": 1},
                        {"x": 0, "y": 0, "width": True, "height": 1},
                        {"x": 0, "y": 0}):
            with self.assertRaises(ValueError):
                server._event_texture_crop(invalid)


class IntentToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "draw-intents.json"
        patcher = mock.patch.object(server, "_draw_intents_path", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._directory.cleanup)

    def _call_returns(self, responses: dict):
        def call(name, arguments=None, timeout=None):
            return json.dumps(responses.get(name, {"error": "unavailable"}))
        return mock.patch.object(server, "_call", side_effect=call)

    def test_recording_and_reading_back_round_trips(self) -> None:
        with self._call_returns({"get_capture_status": {"capturePath": "D:/a.rdc"}}):
            written = json.loads(server.record_draw_intent(
                event_id=30, intent="sheer stockings", source="user",
                confidence="user-stated", search_terms=["Stocking", "丝袜"],
                algorithms=["Charlie sheen"]))
            self.assertTrue(written["applied"])
            self.assertFalse(written["revised"])
            read = json.loads(server.get_draw_intent(event_id=30))
        self.assertEqual(read["record"]["intent"], "sheer stockings")
        self.assertIn("丝袜", read["record"]["searchTerms"])

    def test_verifying_before_recording_is_refused_with_the_reason(self) -> None:
        with self._call_returns({"get_capture_status": {"capturePath": "D:/a.rdc"}}):
            payload = json.loads(server.verify_draw_intent(
                event_id=30, checks=[{"expected": "a sheen lobe", "outcome": "found"}]))
        self.assertIn("record_draw_intent first", payload["error"])

    def test_a_contradicting_check_is_reported_and_the_record_downgraded(self) -> None:
        with self._call_returns({"get_capture_status": {"capturePath": "D:/a.rdc"}}):
            server.record_draw_intent(event_id=30, intent="sheer stockings")
            payload = json.loads(server.verify_draw_intent(event_id=30, checks=[
                {"expected": "alpha rising with 1-NdotV", "outcome": "contradicted"}]))
        self.assertEqual(payload["confidence"], "contradicted")
        self.assertTrue(payload["record"]["checks"])
        self.assertNotIn("next", payload)

    def test_the_same_verification_twice_is_reported_as_a_repeat(self) -> None:
        with self._call_returns({"get_capture_status": {"capturePath": "D:/a.rdc"}}):
            server.record_draw_intent(event_id=30, intent="sheer stockings")
            checks = [{"expected": "a Charlie sheen lobe", "outcome": "found"}]
            server.verify_draw_intent(event_id=30, checks=checks)
            payload = json.loads(server.verify_draw_intent(event_id=30, checks=checks))
        self.assertEqual(payload["repeated"], ["a Charlie sheen lobe"])

    def test_bad_arguments_are_refused(self) -> None:
        self.assertIn("positive integer",
                      json.loads(server.record_draw_intent(event_id=0, intent="x"))["error"])
        with self._call_returns({"get_capture_status": {"capturePath": "D:/a.rdc"}}):
            self.assertIn("required",
                          json.loads(server.record_draw_intent(event_id=1, intent=" "))["error"])
            self.assertIn("non-empty",
                          json.loads(server.verify_draw_intent(event_id=1, checks=[]))["error"])


if __name__ == "__main__":
    unittest.main()


class TextureImageTests(unittest.TestCase):
    """An input texture usually says what a material is at a glance — if it fits."""

    def _capture(self, saved: dict, **kwargs: object) -> tuple[dict, dict]:
        seen: dict = {}

        def save(**arguments):
            seen.update(arguments)
            return json.dumps(saved)

        with mock.patch.object(server, "save_texture", side_effect=save):
            return _summary(server.capture_texture_image(resource_id=77, **kwargs)), seen

    @staticmethod
    def _raw(saved, **kwargs):
        """The content blocks themselves, to see whether the picture is among them."""

        with mock.patch.object(server, "save_texture",
                               side_effect=lambda **arguments: json.dumps(saved)):
            return server.capture_texture_image(resource_id=77, **kwargs)

    def test_the_budget_travels_with_the_save(self) -> None:
        payload, seen = self._capture(
            {"saved": "C:/t/tex.png", "reduction": {"scaledTo": {"width": 512}}},
            max_dimension=512)
        self.assertTrue(payload["saved"])
        self.assertEqual(seen["max_dimension"], 512)
        self.assertEqual(payload["reduction"], {"scaledTo": {"width": 512}})

    def test_a_crop_is_passed_through_for_detail_questions(self) -> None:
        unused, seen = self._capture({"saved": "C:/t/tex.png"},
                                     crop={"x": 0, "y": 0, "width": 128, "height": 128},
                                     max_dimension=4096)
        del unused
        self.assertEqual(seen["crop"], {"x": 0, "y": 0, "width": 128, "height": 128})
        self.assertEqual(seen["max_dimension"], 4096)

    def test_a_texture_that_cannot_be_saved_reports_it(self) -> None:
        payload, unused = self._capture({"error": "resource is not a texture"})
        del unused
        self.assertFalse(payload["saved"])
        self.assertIn("not a texture", payload["reason"])

    def test_a_bad_resource_id_is_refused(self) -> None:
        self.assertIn("positive integer",
                      _summary(server.capture_texture_image(resource_id=0))["error"])

    def test_the_picture_comes_back_in_the_result_not_only_as_a_path(self) -> None:
        """The API route has no file tool, so a path alone is not evidence there."""

        with tempfile.TemporaryDirectory() as directory:
            saved = Path(directory) / "tex.png"
            payload = _png_bytes()
            saved.write_bytes(payload)
            blocks = self._raw({"saved": str(saved)})

        self.assertEqual(len(blocks), 2)
        summary = json.loads(blocks[0])
        self.assertEqual(summary["inlineImage"], "included in this result")
        # The path is still there, because a runtime with a file tool should use it.
        self.assertTrue(summary["path"].endswith("tex.png"))
        content = blocks[1].to_image_content()
        self.assertEqual(content.mimeType, "image/png")
        self.assertEqual(base64.b64decode(content.data), payload)

    def test_the_path_alone_can_be_asked_for(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            saved = Path(directory) / "tex.png"
            saved.write_bytes(_png_bytes())
            blocks = self._raw({"saved": str(saved)}, inline=False)
        self.assertEqual(len(blocks), 1)
        self.assertIn("not requested", json.loads(blocks[0])["inlineImage"])

    def test_an_image_over_the_inline_budget_asks_for_a_tighter_crop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            saved = Path(directory) / "tex.png"
            saved.write_bytes(b"\x89PNG" + b"\x00" * server.INLINE_IMAGE_BYTE_BUDGET)
            blocks = self._raw({"saved": str(saved)})
        self.assertEqual(len(blocks), 1)
        summary = json.loads(blocks[0])
        self.assertTrue(summary["saved"])
        self.assertIn("tighter crop", summary["inlineImage"])

    def test_a_path_that_is_not_there_is_reported_rather_than_invented(self) -> None:
        blocks = self._raw({"saved": "C:/t/missing.png"})
        self.assertEqual(len(blocks), 1)
        self.assertEqual(json.loads(blocks[0])["inlineImage"],
                         "there is no saved image to return")

    def test_both_image_tools_warn_against_describing_an_unseen_picture(self) -> None:
        """The API route cannot open files at all, and a model must not invent the image."""

        texture, unused = self._capture({"saved": "C:/t/tex.png"})
        del unused

        def call(name, arguments=None, timeout=None):
            return json.dumps({"pipeline": {}})

        with mock.patch.object(server, "_call", side_effect=call), \
                mock.patch.object(server, "_capture_event_texture_evidence_receipt",
                                  return_value=json.dumps(
                                      {"artifact": {"status": "saved",
                                                    "output": {"path": "C:/e/rt.png"}}})):
            draw = _summary(server.capture_draw_image(event_id=30, crop_to_draw=False))
        for result in (texture, draw):
            self.assertTrue(result["saved"])
            self.assertEqual(result["inlineImage"], "there is no saved image to return")
            self.assertNotIn("next", result)
