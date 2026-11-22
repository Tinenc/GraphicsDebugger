"""CPU-only history routing regression; no renderdoc module or GPU is loaded."""
import copy
import json
from types import SimpleNamespace
import unittest

from eid_history_profile import byte_record
from eid_history_fallback_profile import (HistoryProbe, displayed_issues, history_evidence,
                                          stable_references)


def evidence():
    return {"api": "HistoryPreview", "eid": 35, "headless": 1, "snapshotroute": 0, "currentstate": 0}


class HistoryEvidenceTests(unittest.TestCase):
    def test_only_native_headless_non_snapshot_historical_replay_proves_route(self):
        self.assertFalse(history_evidence([evidence()], 35, True)["issues"])
        self.assertTrue(history_evidence([], 35, True)["issues"])
        for field, value in (("eid", 43), ("headless", 0), ("snapshotroute", 1),
                             ("currentstate", 1), ("headless", True)):
            self.assertTrue(history_evidence([dict(evidence(), **{field: value})], 35, True)["issues"])
        self.assertTrue(history_evidence([evidence()] * 5, 35, True)["issues"])
        self.assertTrue(history_evidence([evidence()], 35, False)["issues"])
        self.assertFalse(history_evidence([], 35, False)["issues"])

    def test_reference_stability_includes_complete_native_outputs(self):
        refs = {35: [{"rgb": "history", "nativeOutputs": "B"} for _ in range(2)],
                43: [{"rgb": "live", "nativeOutputs": "D"} for _ in range(2)]}
        self.assertFalse(stable_references(refs, 35, 43))
        changed = copy.deepcopy(refs)
        changed[35][1]["nativeOutputs"] = "unstable depth"
        self.assertTrue(stable_references(changed, 35, 43))
        changed = copy.deepcopy(refs)
        changed[35] = changed[43]
        self.assertTrue(stable_references(changed, 35, 43))
        self.assertTrue(stable_references({35: refs[35], 43: []}, 35, 43))

    def test_selected_event_or_rgb_mismatch_is_not_accepted(self):
        self.assertFalse(displayed_issues("B", "B", 43, 43))
        self.assertTrue(displayed_issues("D", "B", 43, 43))
        self.assertTrue(displayed_issues("B", "B", 35, 43))


class FakeController:
    def __init__(self):
        self.event, self.edited, self.outputs, self.records, self.trace = 43, False, [], [], []

    def SetFrameEvent(self, event, force):
        self.trace.append(("SetFrameEvent", event, force))
        self.event = event
        for output in self.outputs:
            output.dirty = True

    def GetCurrentEventID(self):
        return self.event

    def GetFatalErrorStatus(self):
        return SimpleNamespace(code=0)

    def GetReplayCheckpointStatus(self):
        value = {"schema": "renderdoc-replay-checkpoints/v1", "enabled": False,
                 "events": [], "hasInitialCheckpoint": False}
        for key in ("byteBudget", "maxCheckpoints", "checkpointCount", "allocatedBytes", "sharedBytes",
                    "builds", "hits", "misses", "invalidations", "rejected"):
            value[key] = 0
        return json.dumps(value)


class FakeOutput:
    def __init__(self, controller, broken=False):
        self.ctrl, self.broken, self.dirty, self.history, self.rgb = controller, broken, True, None, b""
        controller.outputs.append(self)

    def pixels(self, event):
        return bytes([event, int(self.ctrl.edited), 0]) * 2

    def GetDimensions(self):
        return (2, 1)

    def SetTextureDisplay(self, source):
        self.ctrl.trace.append(("SetTextureDisplay",))
        self.history, self.dirty = None, True

    def DisplayTextureAtEvent(self, event, source):
        self.ctrl.trace.append(("DisplayTextureAtEvent", event))
        self.history, self.rgb, self.dirty = event, self.pixels(event), False
        self.ctrl.records.append(evidence())

    def Display(self):
        self.ctrl.trace.append(("Display",))
        if self.history is not None and self.dirty and not self.broken:
            self.DisplayTextureAtEvent(self.history, None)
        elif self.dirty:
            self.rgb, self.dirty = self.pixels(self.ctrl.event), False

    def ReadbackOutputTexture(self):
        return self.rgb


class FakeRecorder:
    def __init__(self, controller):
        self.ctrl = controller

    def measure(self, label, callback, event=None):
        self.ctrl.records = []
        value = callback()
        return value, {"wallMs": 1.0, "timingRecords": list(self.ctrl.records)}

    def publish(self):
        pass


class HistoryRoutingTests(unittest.TestCase):
    def make(self, broken=False):
        ctrl = FakeController()
        output = FakeOutput(ctrl, broken)
        report = {"issues": [], "operations": []}
        rd = SimpleNamespace(ResultCode=SimpleNamespace(Succeeded=0))
        run = HistoryProbe(ctrl, output, None, rd, FakeRecorder(ctrl), report, None, 35, 43, (2, 1), 1024)
        return run, ctrl, output, report

    def test_dirty_display_retains_B_until_explicit_live_set(self):
        run, ctrl, output, report = self.make()
        b, d = byte_record(output.pixels(35)), byte_record(output.pixels(43))
        run.live("live", d)
        run.history_select("select", b)
        run.dirty_redraw("dirty", b)
        run.live("exit", d)
        self.assertFalse(report["issues"])
        self.assertEqual(ctrl.event, 43)
        self.assertEqual([row for row in ctrl.trace if row[0] == "SetFrameEvent"], [("SetFrameEvent", 43, True)])
        self.assertIsNone(output.history)

    def test_old_fallback_behaviour_showing_live_D_after_dirty_is_caught(self):
        run, ctrl, output, report = self.make(broken=True)
        b = byte_record(output.pixels(35))
        run.history_select("select", b)
        run.dirty_redraw("dirty", b)
        self.assertTrue(any("RGB differs" in issue for issue in report["issues"]))
        self.assertTrue(any("evidence is missing" in issue for issue in report["issues"]))

    def test_shared_value_redraws_history_B_without_selecting_B_in_controller(self):
        run, ctrl, output, report = self.make()
        run.history_select("select", byte_record(output.pixels(35)))
        def mutate():
            ctrl.edited, output.dirty = True, True
            output.Display()
        result = run.operation("edit", mutate)
        self.assertEqual(result["rgb"], byte_record(output.pixels(35)))
        self.assertNotEqual(result["rgb"], byte_record(output.pixels(43)))
        self.assertEqual(ctrl.event, 43)
        self.assertFalse(report["issues"])


if __name__ == "__main__":
    unittest.main()
