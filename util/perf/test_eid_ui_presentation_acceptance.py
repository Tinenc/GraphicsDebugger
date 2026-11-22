"""CPU-only tests of UI evidence decisions; never imports PySide2 or starts a GUI."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

import eid_ui_presentation_acceptance as ui


def manifest():
    events = [15, 29, 47, 66]
    return {"schema": "renderdoc-d3d12-checkpoint-fixture/v1", "ok": True,
            "captureDriver": "D3D12", "selectedEvents": events, "capture": "small-fixture.rdc",
            "bindings": [{"eventId": event, "colorTargets": [{"slot": 0, "resource": "ResourceId::15"}]}
                         for event in events]}


def state():
    return {"pending": False, "hidden": False, "displayedEvent": 66,
            "displayedResource": "ResourceId::15", "currentEvent": 66, "selectedEvent": 66,
            "resource": "ResourceId::15", "scrubbing": True}


class UIPresentationEvidenceTests(unittest.TestCase):
    def test_nested_loading_loop_cannot_start_scrub_before_loadcapture_returns(self):
        # The real failing run had IsCaptureLoaded=true while LoadCapture still had its initial
        # SetEventID(lastAction) and OnCaptureLoaded callbacks pending on the caller's stack.
        self.assertFalse(ui.loading_complete(False, True, False))
        self.assertFalse(ui.loading_complete(False, True, True))
        self.assertFalse(ui.loading_complete(True, False, False))
        self.assertFalse(ui.loading_complete(True, True, True))
        self.assertTrue(ui.loading_complete(True, True, False))

    def test_private_config_accepts_json_or_existing_launcher_json_file(self):
        self.assertEqual(ui.load_configuration('{"manifest":"test.json"}'), {"manifest": "test.json"})
        with patch.object(Path, "read_text", return_value='{"manifest":"file.json"}') as read:
            self.assertEqual(ui.load_configuration("isolated-config.json"), {"manifest": "file.json"})
            read.assert_called_once_with(encoding="utf-8-sig")

    def test_fixture_requires_four_discovered_shared_rt0_draws_and_isolated_new_output(self):
        isolation = Path(__file__).resolve().parents[2] / "output" / "gui-isolation" / "unit-test"
        config = {"output": str(isolation / "ui-result.json"), "isolationRoot": str(isolation)}
        with patch.object(Path, "exists", return_value=False), patch.object(Path, "is_dir", return_value=True):
            self.assertEqual(ui.fixture_inputs(config, manifest())["events"], [15, 29, 47, 66])
            bad = manifest()
            bad["bindings"][2]["colorTargets"][0]["resource"] = "ResourceId::16"
            with self.assertRaisesRegex(ValueError, "share"):
                ui.fixture_inputs(config, bad)
            bad = manifest()
            bad["bindings"][1]["colorTargets"][0]["slot"] = 1
            with self.assertRaisesRegex(ValueError, "RT0"):
                ui.fixture_inputs(config, bad)
            outside = dict(config, output=str(isolation.parent / "outside.json"))
            with self.assertRaisesRegex(ValueError, "inside"):
                ui.fixture_inputs(outside, manifest())
        with patch.object(Path, "exists", return_value=True), patch.object(Path, "is_dir", return_value=True):
            with self.assertRaisesRegex(ValueError, "new file"):
                ui.fixture_inputs(config, manifest())

    def test_default_rt0_must_publish_while_scrub_is_still_held(self):
        evidence = state()
        self.assertTrue(ui.presentation_ready(evidence, 66, "ResourceId::15", 66, held=True))
        evidence["scrubbing"] = False
        with self.assertRaisesRegex(ValueError, "held scrub"):
            ui.presentation_ready(evidence, 66, "ResourceId::15", 66, held=True)
        evidence = state()
        evidence["resource"] = "ResourceId::0"
        self.assertFalse(ui.presentation_ready(evidence, 66, "ResourceId::15", 66))
        evidence = state()
        evidence["selectedEvent"] = 29
        self.assertFalse(ui.presentation_ready(evidence, 66, "ResourceId::15", 66))

    def test_historical_preview_is_distinct_from_global_event_navigation(self):
        evidence = state()
        evidence["displayedEvent"] = 29
        self.assertTrue(ui.presentation_ready(evidence, 29, "ResourceId::15", 66))
        self.assertFalse(ui.presentation_ready(evidence, 29, "ResourceId::15", 29))
        evidence["currentEvent"] = evidence["selectedEvent"] = 29
        self.assertTrue(ui.presentation_ready(evidence, 29, "ResourceId::15", 29))

    def test_pending_tab_cannot_retain_old_completed_identity(self):
        evidence = state()
        evidence["pending"] = True
        with self.assertRaisesRegex(ValueError, "prematurely"):
            ui.presentation_ready(evidence, 66, "ResourceId::15", 66, strict=True)
        evidence["displayedEvent"] = evidence["displayedResource"] = None
        self.assertFalse(ui.presentation_ready(evidence, 66, "ResourceId::15", 66, strict=True))

    def test_completed_current_tab_cannot_publish_history_identity(self):
        evidence = state()
        evidence["displayedEvent"] = 29
        # In-flight scrub may still show the prior committed event, but completed tab routing may not.
        self.assertFalse(ui.presentation_ready(evidence, 66, "ResourceId::15", 66))
        with self.assertRaisesRegex(ValueError, "previous display"):
            ui.presentation_ready(evidence, 66, "ResourceId::15", 66, strict=True)

    def test_full_surface_hash_catches_stale_image_despite_correct_metadata(self):
        current = {"size": [256, 128], "sha256": "current", "blankRGB": False}
        old = dict(current, sha256="history")
        ui.check_surface(current, current)
        with self.assertRaisesRegex(ValueError, "pixels differ"):
            ui.check_surface(old, current)
        with self.assertRaises(ValueError):
            ui.check_surface(dict(current, size=[128, 128]), current)

    def test_pending_visible_output_allows_only_gpu_clear_or_exact_new_source(self):
        pending = state()
        pending.update(pending=True, displayedEvent=None, displayedResource=None)
        current = {"size": [256, 128], "sha256": "current", "blankRGB": False}
        self.assertEqual(ui.check_pending_surface(pending, dict(current, blankRGB=True), current), "cleared")
        self.assertEqual(ui.check_pending_surface(pending, current, current), "new-source-before-ui-completion")
        with self.assertRaises(ValueError):
            ui.check_pending_surface(pending, dict(current, sha256="history"), current)
        pending["displayedEvent"] = 29
        with self.assertRaises(ValueError):
            ui.check_pending_surface(pending, current, current)


if __name__ == "__main__":
    unittest.main()
