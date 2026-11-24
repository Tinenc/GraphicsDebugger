from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from src import ipc_client


class IPCBusyInstanceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.patch = mock.patch.object(ipc_client, "INSTANCE_ROOT", str(self.root))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.alive = mock.patch.object(ipc_client.IPCClient, "_process_alive", return_value=True)
        self.alive_probe = self.alive.start()
        self.addCleanup(self.alive.stop)
        self.write_manifest("busy", stale=True)
        self.write_manifest("fresh", stale=False)

    def write_manifest(self, instance_id, *, stale, **changes):
        root = self.root / instance_id
        root.mkdir(exist_ok=True)
        now = int(time.time() * 1000)
        value = {
            "instanceId": instance_id, "pid": 123,
            "heartbeatUnixMs": now - 60000 if stale else now,
            "startedUnixMs": now, "lastActivityUnixMs": now,
            "captureLoaded": True, "stopped": False,
        }
        value.update(changes)
        (root / "instance.json").write_text(json.dumps(value), encoding="utf-8")

    def test_explicit_busy_owner_remains_bound_while_discovery_uses_fresh_owner(self):
        client = ipc_client.IPCClient(instance_id="busy")
        self.assertEqual(self.root / "busy" / "requests", Path(client._instance_mailbox()[0]))
        state = client.bound_instance_state()
        self.assertTrue(state["alive"])
        self.assertFalse(state["heartbeatFresh"])
        self.assertEqual("busy", client.instance_id)
        implicit = ipc_client.IPCClient()
        self.assertEqual(self.root / "fresh" / "requests", Path(implicit._instance_mailbox()[0]))
        self.assertIsNone(implicit.bound_instance_state())

    def test_direct_call_binding_takes_precedence_even_with_a_stale_heartbeat(self):
        client = ipc_client.IPCClient(instance_id="fresh")
        with ipc_client.bind_direct_tool_call("busy", "a" * 32):
            self.assertEqual(self.root / "busy" / "requests", Path(client._instance_mailbox()[0]))
            self.assertEqual("busy", client.bound_instance_state()["instanceId"])
        self.assertEqual("fresh", client.instance_id)

    def test_stopped_dead_and_mismatched_owners_do_not_fall_back_to_other_instances(self):
        client = ipc_client.IPCClient(instance_id="busy")
        for changes in ({"stopped": True}, {"instanceId": "fresh"}, {"pid": 0}):
            with self.subTest(changes=changes):
                self.write_manifest("busy", stale=True, **changes)
                with self.assertRaisesRegex(RuntimeError, "not alive: busy"):
                    client._instance_mailbox()
                self.assertFalse(client.bound_instance_state()["alive"])
        self.write_manifest("busy", stale=True)
        self.alive_probe.return_value = False
        with self.assertRaisesRegex(RuntimeError, "not alive: busy"):
            client._instance_mailbox()

    def test_stale_owner_can_answer_an_ordinary_bounded_call(self):
        request_dir = self.root / "busy" / "requests"
        response_dir = self.root / "busy" / "responses"
        response_dir.mkdir()
        observed = []

        def bridge():
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                requests = list(request_dir.glob("*.json"))
                if requests:
                    request = json.loads(requests[0].read_text(encoding="utf-8"))
                    observed.append(request)
                    (response_dir / requests[0].name).write_text(json.dumps({
                        "id": request["id"], "result": {"status": "ok"},
                    }), encoding="utf-8")
                    return
                time.sleep(0.01)

        worker = threading.Thread(target=bridge)
        worker.start()
        try:
            client = ipc_client.IPCClient(instance_id="busy", timeout=1)
            with ipc_client.bind_direct_tool_call("busy", "a" * 32):
                self.assertEqual({"status": "ok"}, client.call("ping", {}))
        finally:
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual("a" * 32, observed[0]["params"]["__replaylab_expected_capture_session_id"])
        self.assertEqual([], list(request_dir.glob("*")))
        self.assertEqual([], list(response_dir.glob("*")))

    def test_timeout_reports_live_owner_as_busy_and_cleans_only_its_request(self):
        request_dir = self.root / "busy" / "requests"
        request_dir.mkdir()
        unrelated = request_dir / "another-request.json"
        unrelated.write_text("{}", encoding="utf-8")
        client = ipc_client.IPCClient(instance_id="busy")
        with self.assertRaisesRegex(ipc_client.IPCBridgeBusyError, "still running but busy or unresponsive"):
            client.call("ping", {}, timeout=0.05)
        self.assertEqual([unrelated], list(request_dir.glob("*")))
        self.assertEqual("busy", client.instance_id)

    def test_unsent_busy_diagnostic_uses_bread_product_name(self):
        message = str(ipc_client.IPCClient._unsent_busy_error("inspect_event", 2.0))
        self.assertIn("Bread qrenderdoc bridge is busy", message)
        self.assertNotIn("ReplayLab", message)

    def test_implicit_v2_timeout_reports_actual_owner_without_making_discovery_a_binding(self):
        client = ipc_client.IPCClient()
        with self.assertRaisesRegex(ipc_client.IPCBridgeBusyError, "instance fresh.*still running"):
            client.call("ping", {}, timeout=0.05)
        self.assertEqual("fresh", client.instance_id)
        self.assertIsNone(client.bound_instance_state())

    def test_workflow_binding_keeps_admitted_owner_when_heartbeat_stalls(self):
        self.write_manifest("fresh", stale=False, captureSessionId="a" * 32)
        client = ipc_client.IPCClient()
        with client.bind_capture_session("a" * 32):
            self.assertEqual(("fresh", "a" * 32), ipc_client._CALL_BINDING.get())
            self.write_manifest("fresh", stale=True)
            self.write_manifest("new-owner", stale=False, lastActivityUnixMs=int(time.time() * 1000) + 1000)
            self.assertEqual(self.root / "fresh" / "requests", Path(client._instance_mailbox()[0]))
            self.assertTrue(client.bound_instance_state()["alive"])
        self.assertIsNone(ipc_client._CALL_BINDING.get())
        self.assertEqual(self.root / "new-owner" / "requests", Path(client._instance_mailbox()[0]))

    def test_workflow_binding_cannot_change_frozen_session(self):
        client = ipc_client.IPCClient()
        with ipc_client.bind_direct_tool_call("busy", "a" * 32):
            with self.assertRaisesRegex(RuntimeError, "frozen capture session"):
                with client.bind_capture_session("b" * 32):
                    self.fail("mismatched session must never be admitted")

    def test_frozen_capture_admission_finds_its_stale_owner_before_newer_window(self):
        self.write_manifest("busy", stale=True, captureSessionId="a" * 32)
        self.write_manifest("fresh", stale=False, captureSessionId="b" * 32)
        client = ipc_client.IPCClient()
        with client.bind_capture_session("a" * 32):
            self.assertEqual(("busy", "a" * 32), ipc_client._CALL_BINDING.get())
            self.assertEqual(self.root / "busy" / "requests", Path(client._instance_mailbox()[0]))
            self.assertEqual("busy", client.instance_id)
        self.assertIsNone(ipc_client._CALL_BINDING.get())

    def test_ambiguous_frozen_capture_owners_are_not_selected(self):
        self.write_manifest("busy", stale=True, captureSessionId="a" * 32)
        self.write_manifest("fresh", stale=False, captureSessionId="a" * 32)
        client = ipc_client.IPCClient()
        with self.assertRaisesRegex(RuntimeError, "Multiple.*frozen capture session"):
            with client.bind_capture_session("a" * 32):
                self.fail("ambiguous owners must not be admitted")
        self.assertIsNone(ipc_client._CALL_BINDING.get())

    def test_timeout_owner_is_derived_from_sent_mailbox_even_if_discovery_cache_changes(self):
        client = ipc_client.IPCClient()
        original_sleep = time.sleep

        def other_discovery(seconds):
            client.instance_id = "busy"
            original_sleep(seconds)

        with mock.patch.object(ipc_client.time, "sleep", side_effect=other_discovery):
            with self.assertRaisesRegex(ipc_client.IPCBridgeBusyError, "instance fresh.*still running"):
                client.call("ping", {}, timeout=0.05)

    def test_missing_or_unreadable_target_never_selects_other_modern_window(self):
        self.write_manifest("fresh", stale=False, captureSessionId="b" * 32)
        for damage in ("missing", "unreadable", "stopped"):
            with self.subTest(damage=damage):
                self.write_manifest("busy", stale=True, captureSessionId="a" * 32)
                manifest = self.root / "busy" / "instance.json"
                if damage == "missing":
                    manifest.unlink()
                elif damage == "unreadable":
                    manifest.write_text("{broken", encoding="utf-8")
                else:
                    self.write_manifest("busy", stale=True, captureSessionId="a" * 32, stopped=True)
                client = ipc_client.IPCClient()
                with mock.patch.object(client, "_instance_mailbox") as rediscover:
                    with self.assertRaisesRegex(ipc_client.IPCBridgeUnavailableError, "owner is unavailable.*not sent"):
                        with client.bind_capture_session("a" * 32):
                            self.fail("an unavailable owner cannot redirect the workflow")
                    rediscover.assert_not_called()
                self.assertEqual("", client.instance_id)
                self.assertIsNone(ipc_client._CALL_BINDING.get())

    def test_unreadable_v2_remnants_do_not_grant_legacy_fallback(self):
        for name in ("busy", "fresh"):
            (self.root / name / "instance.json").unlink()
        client = ipc_client.IPCClient()
        with mock.patch.object(client, "_instance_mailbox") as rediscover:
            with self.assertRaises(ipc_client.IPCBridgeUnavailableError):
                with client.bind_capture_session("a" * 32):
                    self.fail("unreadable v2 metadata is not legacy-only")
            rediscover.assert_not_called()

    def test_v2_free_environment_retains_legacy_only_compatibility(self):
        legacy_environment = self.root / "legacy-only"
        legacy_environment.mkdir()
        with mock.patch.object(ipc_client, "INSTANCE_ROOT", str(legacy_environment)):
            client = ipc_client.IPCClient()
            with client.bind_capture_session("a" * 32):
                self.assertIsNone(ipc_client._CALL_BINDING.get())
                self.assertEqual((ipc_client.REQUEST_DIR, ipc_client.RESPONSE_DIR), client._mailbox())
                self.assertIsNone(client.bound_instance_state())

    def test_unreadable_v2_discovery_does_not_grant_legacy_fallback(self):
        client = ipc_client.IPCClient()
        with mock.patch.object(ipc_client.os, "listdir", side_effect=PermissionError("unreadable")):
            with self.assertRaisesRegex(ipc_client.IPCBridgeUnavailableError, "discovery is unreadable"):
                with client.bind_capture_session("a" * 32):
                    self.fail("unreadable discovery cannot select legacy")

    def test_previously_selected_v2_owner_does_not_become_legacy_only_when_root_disappears(self):
        client = ipc_client.IPCClient()
        client._instance_mailbox()
        self.assertEqual("fresh", client.instance_id)
        with mock.patch.object(ipc_client.os, "listdir", side_effect=FileNotFoundError("removed")):
            with self.assertRaises(ipc_client.IPCBridgeUnavailableError):
                with client.bind_capture_session("a" * 32):
                    self.fail("a vanished selected owner cannot redirect to legacy")


if __name__ == "__main__":
    unittest.main()
