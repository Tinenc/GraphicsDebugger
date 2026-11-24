"""Serial native mutation and GUI publication races, without Qt or a GPU."""

import ast
from pathlib import Path
import unittest

import test_shared_cbuffer as shared_fixture
from test_native_material_panel import _Bridge, _selection, panel


class MaterialEditQueueTests(unittest.TestCase):
    def setUp(self):
        self.ctrl = shared_fixture._Controller()
        self.edits = shared_fixture.shared.SharedCBufferEdits(
            lambda value: 202 if value is self.ctrl.resource else 0)
        self.bridge = _Bridge()
        self.jobs = []
        self.observed = []
        self.owner = object()

        def snapshot(params):
            value = self.bridge.snapshot(params)
            value["bufferLength"] = 4096
            return self.edits.register(value, self.ctrl)

        def dispatch(params, clear, cancel, complete):
            self.jobs.append((params, clear, cancel, complete))

        self.model = panel.MaterialPanelController(
            snapshot, None, None, self.bridge.context, dispatch)
        status = self.model.expose(_selection())
        self.sid = status["sessionId"]
        self.pid = status["parameters"][0]["parameterId"]

    def submit(self, text, parameter=None):
        self.model.submit_text(self.sid, parameter or self.pid, text,
            lambda result, error: self.observed.append((result, error)), self.owner)

    def execute(self):
        params, clear, cancel, complete = self.jobs.pop(0)
        try:
            if cancel.is_set():
                raise RuntimeError("cancelled before native work")
            response = self.edits.rollback(params, self.ctrl) if clear else self.edits.apply(params, self.ctrl)
            error = None
        except Exception as exc:
            response, error = None, exc
        return lambda: complete(response, error)

    def test_continuous_input_publishes_executed_versions_then_latest_pending(self):
        self.submit("0.1")
        self.submit("0.2")
        self.submit("0.3")
        self.assertEqual(len(self.jobs), 1)
        self.assertEqual(len(self.model.edit_queue.pending), 1)
        first_publication = self.execute()
        self.assertEqual(self.model.status(self.sid)["revision"], 0)
        self.assertEqual(len(self.jobs), 0)
        first_publication()
        self.assertEqual(self.observed[0][0]["generation"], 1)
        self.assertEqual(self.model.status(self.sid)["revision"], 1)
        self.assertEqual(len(self.jobs), 1)
        self.execute()()
        self.assertAlmostEqual(self.model.status(self.sid)["parameters"][0]["value"][0], 0.3)
        self.assertEqual([value[0]["generation"] for value in self.observed], [1, 2])
        self.assertFalse(self.model.edit_queue.busy)

    def test_latest_value_of_other_fields_is_not_lost(self):
        other = self.model.expose(_selection(fieldName="flags", fieldByteOffset=20, fieldBaseType="UInt"))
        other_id = other["parameters"][1]["parameterId"]
        self.submit("0.1")
        self.submit("7", other_id)
        self.submit("0.2")
        self.submit("9", other_id)
        while self.jobs:
            self.execute()()
        self.assertEqual([entry[0] for entry in self.ctrl.sets], [64, 84, 64])
        status = self.model.status(self.sid)
        self.assertEqual(status["parameters"][1]["value"], [9])

    def test_closed_panel_cancels_queued_native_work(self):
        self.submit("1")
        self.submit("2")
        self.model.edit_queue.cancel_owner(self.owner)
        self.execute()()
        self.assertEqual(self.ctrl.sets, [])
        self.assertEqual(self.observed, [])
        self.assertFalse(self.model.edit_queue.busy)

    def test_completed_native_edit_survives_close_before_gui_publication(self):
        self.submit("0.25")
        publish = self.execute()
        self.model.edit_queue.cancel_owner(self.owner)
        publish()
        self.assertEqual(self.observed, [])
        self.assertEqual(self.model.status(self.sid)["generation"], 1)
        self.assertEqual(self.model.status(self.sid)["parameters"][0]["value"], [0.25])

    def test_capture_reset_rejects_old_completed_publication(self):
        self.submit("0.25")
        publish = self.execute()
        self.model.forget_all()
        publish()
        self.assertEqual(self.model.list_status(), [])
        self.assertEqual(self.observed, [])
        self.assertFalse(self.model.edit_queue.busy)

    def test_reset_releases_discarded_callback_and_late_callback_cannot_clear_new_job(self):
        self.submit("0.25")
        old_publish = self.execute()
        self.model.forget_all()
        self.assertFalse(self.model.edit_queue.busy)
        self.edits.forget()
        self.ctrl.generation = 0
        self.bridge.capture_session_id = "f" * 32
        status = self.model.expose(_selection())
        self.sid = status["sessionId"]
        self.pid = status["parameters"][0]["parameterId"]
        self.submit("0.75")
        old_publish()
        self.assertTrue(self.model.edit_queue.busy)
        self.execute()()
        self.assertEqual(self.model.status(self.sid)["parameters"][0]["value"], [0.75])
        self.assertEqual(len(self.observed), 1)

    def test_failed_version_does_not_advance_revision_before_pending_retry(self):
        self.submit("0.25")
        self.submit("0.5")
        self.ctrl.fail_set = True
        self.execute()()
        self.assertIsNotNone(self.observed[0][1])
        self.assertEqual(self.model.status(self.sid)["revision"], 0)
        self.ctrl.fail_set = False
        self.execute()()
        self.assertEqual(self.model.status(self.sid)["revision"], 1)

    def test_external_native_edit_expires_pending_values_without_retrying_or_locking_reopen(self):
        self.submit("0.25")
        self.submit("0.5")
        self.ctrl.generation += 1
        self.execute()()
        self.assertEqual(len(self.observed), 1)
        self.assertTrue(getattr(self.observed[0][1], "snapshot_expired", False))
        self.assertEqual(self.ctrl.sets, [])
        self.assertEqual(self.jobs, [])
        self.assertFalse(self.model.edit_queue.busy)
        self.assertEqual(self.model.list_status(), [])
        status = self.model.expose(_selection())
        self.sid = status["sessionId"]
        self.pid = status["parameters"][0]["parameterId"]
        self.submit("0.75")
        self.execute()()
        self.assertEqual(self.model.status(self.sid)["parameters"][0]["value"], [0.75])

    def test_clear_waits_for_committed_edit_and_drops_pending_values(self):
        self.submit("0.25")
        self.submit("0.5")
        self.model.submit_rollback(self.sid,
            lambda result, error: self.observed.append((result, error)), self.owner)
        self.execute()()
        self.assertTrue(self.jobs[0][1])
        self.execute()()
        self.assertEqual(len(self.ctrl.sets), 1)
        self.assertEqual(self.ctrl.clears, [(64, 4)])
        self.assertEqual(self.model.list_status(), [])

    def test_invalid_pending_text_is_not_sent_to_native(self):
        self.submit("0.25")
        self.submit("-")
        self.execute()()
        self.assertEqual(len(self.ctrl.sets), 1)
        self.assertIsNotNone(self.observed[-1][1])
        self.assertFalse(self.model.edit_queue.busy)

    def test_queue_supports_python_36(self):
        source = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "material_edit_queue.py"
        ast.parse(source.read_text(encoding="utf-8"), feature_version=(3, 6))


if __name__ == "__main__":
    unittest.main()
