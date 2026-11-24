"""CPU-only history selection, evidence recording and correctness-control tests."""
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location("eid_history_profile", str(Path(__file__).with_name("eid_history_profile.py")))
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)


class DirectRecorder:
    def __init__(self):
        self.phases = []

    def measure(self, phase, callback, event=None, cleanup=False):
        self.phases.append((phase, event, cleanup))
        return callback(), {}


class Controller:
    def __init__(self):
        self.event = 100
        self.replays = []

    def SetFrameEvent(self, event, force):
        self.event = event
        self.replays.append((event, force))

    def GetCurrentEventID(self):
        return self.event


class Output:
    def __init__(self, payloads):
        self.payloads = iter(payloads)
        self.display_count = 0

    def SetTextureDisplay(self, source):
        pass

    def Display(self):
        self.display_count += 1

    def ReadbackOutputTexture(self):
        return next(self.payloads)


class HistoryProfileTests(unittest.TestCase):
    def test_candidate_priority_and_conservative_subresources(self):
        usage = [(0, "ColorTarget"), (3, "PS_Resource"), (4, "ColorTarget"), (4, "Clear"),
                 (6, "PS_RWResource"), (7, "GenMips"), (8, "Discard")]
        self.assertEqual(profile.build_candidates(usage, 0), [
            {"eventId": 4, "usage": "Clear"}, {"eventId": 6, "usage": "PS_RWResource"},
            {"eventId": 8, "usage": "Discard"}])
        self.assertEqual(len(profile.build_candidates(usage, 2)), 4)

    def test_three_nearby_and_one_distant(self):
        candidates = [{"eventId": eid, "usage": "ColorTarget"} for eid in range(10, 101, 10)]
        selected = profile.select_candidates(candidates, 53, 4, [])
        self.assertEqual([item["eventId"] for item in selected], [40, 50, 60, 100])
        self.assertEqual(len(profile.select_candidates(candidates[:2], 53, 4, [])), 2)
        with self.assertRaises(ValueError):
            profile.select_candidates([{"eventId": 8, "usage": "Discard"}], 8, 1, [8])

    def test_single_explicit_history_draw(self):
        candidates = [{"eventId": 2, "usage": "Clear"}, {"eventId": 9, "usage": "ColorTarget"}]
        self.assertEqual(profile.select_candidates(candidates, 2, 1, [9]), [candidates[1]])
        with self.assertRaises(ValueError):
            profile.select_candidates(candidates, 2, 1, [2, 9])

    def test_live_output_resolves_replay_generated_id_without_guessing(self):
        old_id, new_id = "ResourceId::1000000000000000428", "ResourceId::1000000000000000057"
        textures = [SimpleNamespace(resourceId=old_id), SimpleNamespace(resourceId=new_id)]
        self.assertIs(profile.texture_from_color_output(textures, [SimpleNamespace(resource=new_id)], 0), textures[1])
        with self.assertRaises(RuntimeError):
            profile.texture_from_color_output(textures, [SimpleNamespace(resource="ResourceId::0")], 0)
        with self.assertRaises(RuntimeError):
            profile.texture_from_color_output(textures[:1], [SimpleNamespace(resource=new_id)], 0)

    def test_long_history_thumbnail_budget(self):
        self.assertEqual(profile.preview_size(1920, 1080, 0, 1), (256, 144))
        width, height = profile.preview_size(1920, 1080, 0, 1504)
        self.assertLessEqual(width * height * 3 * 1504, 48 * 1024 * 1024)

    def test_recorder_publishes_before_native_call_and_keeps_only_its_log_interval(self):
        with tempfile.TemporaryDirectory(prefix="eid-history-unit-") as temporary:
            root = Path(temporary)
            log, report_path = root / "native.log", root / "report.json"
            log.write_text("api=D3D12 range=1->1 total_ms=999\n", encoding="utf-8")
            report = {"samples": []}
            recorder = profile.Recorder(report, report_path, log, 30)

            def fake_native():
                pending = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertEqual(pending["samples"][-1]["status"], "running")
                with log.open("a", encoding="utf-8") as stream:
                    stream.write("api=D3D12 range=1->29385 type=Full total_ms=0.001\n")
                    stream.write("api=HistoryPreview eid=29385 headless=1 snapshotroute=0 total_ms=0.002\n")
                return 42

            value, sample = recorder.measure("history.headlessDisplayAtEvent", fake_native, 29385)
            self.assertEqual(value, 42)
            self.assertEqual(sample["backendReplayCalls"], 1)
            self.assertEqual(sample["backendReplayTotalMs"], 0.001)
            self.assertEqual(len(sample["timingRecords"]), 2)
            self.assertEqual(sample["status"], "complete")

    def run_control(self, historical, canonical):
        controller, output, recorder, result = Controller(), Output(canonical), DirectRecorder(), {}
        profile.validate_thumbnail(controller, output, object(), 20, 100,
                                   profile.byte_record(historical), recorder, result, len(historical))
        self.assertEqual(controller.replays, [(20, True), (20, True), (100, True)])
        self.assertEqual(output.display_count, 2)
        return result

    def test_nonempty_wrong_thumbnail_fails_against_stable_canonical(self):
        result = self.run_control(b"bad", [b"yes", b"yes"])
        self.assertEqual(result["status"], "failedStableCanonicalMismatch")

    def test_unstable_canonical_is_inconclusive(self):
        result = self.run_control(b"yes", [b"yes", b"bad"])
        self.assertEqual(result["status"], "inconclusiveCanonicalUnstable")

    def test_identical_canonical_control_passes(self):
        self.assertEqual(self.run_control(b"yes", [b"yes", b"yes"])["status"], "passed")

    def test_failed_readback_still_restores_anchor(self):
        controller, recorder = Controller(), DirectRecorder()
        with self.assertRaises(RuntimeError):
            profile.validate_thumbnail(controller, Output([b""]), object(), 20, 100,
                                       profile.byte_record(b"yes"), recorder, {}, 3)
        self.assertEqual(controller.GetCurrentEventID(), 100)
        self.assertEqual(recorder.phases[-1], ("validation.restoreAnchor", 100, True))


if __name__ == "__main__":
    unittest.main()
