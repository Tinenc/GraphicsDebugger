"""Native mutation acceptance is distinct from asynchronous preview publication."""

import unittest

import test_cbuffer_bridge as fixture


class MaterialPublicationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.EventCBufferBridgeTests()
        self.fixture.setUp()
        self.bridge = self.fixture.bridge
        self.waiting = []
        self.receipts = []
        self.fixture.context.RefreshReplayStatus = lambda event, generation, complete: self.waiting.append(
            (event, generation, complete))
        self.bridge._notify_shader_replay_changed = lambda: self.fail("Generic forced refresh must not be used")
        self.envelope = {"response": {"configured": True, "revision": 3, "generation": 8},
            "error": None, "eventId": 77, "generation": 8,
            "captureSessionId": fixture.CAPTURE_SESSION_ID}

    def publish(self):
        self.bridge._publish_shared_cbuffer_operation(self.envelope,
            lambda response, error: self.receipts.append((response, error)))

    def test_no_acknowledgement_before_panel_drain_and_native_event_is_used(self):
        self.fixture.controller.event = 78
        self.publish()
        self.assertEqual(self.receipts, [])
        self.assertEqual(self.waiting[0][:2], (77, 8))
        self.waiting[0][2](True)
        self.assertEqual(len(self.receipts), 1)
        self.assertTrue(self.receipts[0][0]["previewPublished"])
        self.assertEqual(self.receipts[0][0]["replayedEventId"], 77)

    def test_false_publication_keeps_successful_native_mutation(self):
        self.publish()
        self.waiting[0][2](False)
        response, error = self.receipts[0]
        self.assertIsNone(error)
        self.assertTrue(response["configured"])
        self.assertEqual(response["generation"], 8)
        self.assertEqual(response["revision"], 3)
        self.assertFalse(response["previewPublished"])

    def test_completion_is_once_even_if_callback_reenters_then_native_binding_throws(self):
        def unusual(event, generation, complete):
            complete(True)
            complete(False)
            raise RuntimeError("injected callback teardown")
        self.fixture.context.RefreshReplayStatus = unusual
        self.publish()
        self.assertEqual(len(self.receipts), 1)
        self.assertTrue(self.receipts[0][0]["previewPublished"])

    def test_publication_enqueue_error_does_not_make_native_edit_retryable(self):
        def fail_enqueue(*_args):
            raise RuntimeError("missing publication API")
        self.fixture.context.RefreshReplayStatus = fail_enqueue
        self.publish()
        self.assertEqual(len(self.receipts), 1)
        response, error = self.receipts[0]
        self.assertIsNone(error)
        self.assertFalse(response["previewPublished"])
        self.assertIn("missing publication API", response["previewDiagnostic"])

    def test_capture_change_finishes_false_without_touching_new_capture(self):
        self.bridge._capture_session_id = "f" * 32
        self.publish()
        self.assertEqual(self.waiting, [])
        self.assertEqual(len(self.receipts), 1)
        self.assertFalse(self.receipts[0][0]["previewPublished"])
        self.assertIsNone(self.receipts[0][1])

    def test_partial_clear_error_is_retained_after_publication(self):
        self.envelope["response"] = None
        error = RuntimeError("partial clear failed")
        self.envelope["error"] = error
        self.publish()
        self.waiting[0][2](True)
        self.assertEqual(self.receipts, [(None, error)])


if __name__ == "__main__":
    unittest.main()
