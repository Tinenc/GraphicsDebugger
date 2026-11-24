import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from src import ipc_client


class IPCClientTests(unittest.TestCase):
    def _paths(self, root):
        return {
            "IPC_DIR": root,
            "REQUEST_DIR": os.path.join(root, "requests"),
            "RESPONSE_DIR": os.path.join(root, "responses"),
        }

    def _write_instance(
        self,
        instance_root,
        name,
        *,
        loaded,
        activity,
        started=None,
        pid=101,
        stopped=False,
    ):
        directory = os.path.join(instance_root, name)
        os.makedirs(directory, exist_ok=True)
        with open(
            os.path.join(directory, "instance.json"), "w", encoding="utf-8"
        ) as stream:
            json.dump(
                {
                    "instanceId": name,
                    "pid": pid,
                    "heartbeatUnixMs": int(time.time() * 1000),
                    "lastActivityUnixMs": activity,
                    "startedUnixMs": activity if started is None else started,
                    "captureLoaded": loaded,
                    "stopped": stopped,
                },
                stream,
            )
        return directory

    def test_per_request_response_requires_matching_id(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.multiple(
            ipc_client, **self._paths(root)
        ):
            def bridge():
                request_dir = os.path.join(root, "requests")
                response_dir = os.path.join(root, "responses")
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    names = [
                        name for name in os.listdir(request_dir)
                        if name.endswith(".json")
                    ] if os.path.isdir(request_dir) else []
                    if names:
                        name = names[0]
                        with open(os.path.join(request_dir, name), encoding="utf-8") as stream:
                            request = json.load(stream)
                        os.makedirs(response_dir, exist_ok=True)
                        with open(
                            os.path.join(response_dir, name), "w", encoding="utf-8"
                        ) as stream:
                            json.dump(
                                {"id": request["id"], "result": {"value": "中文"}},
                                stream,
                                ensure_ascii=False,
                            )
                        return
                    time.sleep(0.01)

            worker = threading.Thread(target=bridge)
            worker.start()
            result = ipc_client.IPCClient(timeout=2.0).call("ping", {})
            worker.join(2.0)

            self.assertEqual({"value": "中文"}, result)
            self.assertEqual([], os.listdir(os.path.join(root, "responses")))

    def test_ascii_escaped_bridge_response_round_trips_chinese_capture_path(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.multiple(
            ipc_client, **self._paths(root)
        ):
            observed_wire = []

            def bridge():
                request_dir = os.path.join(root, "requests")
                response_dir = os.path.join(root, "responses")
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    names = (
                        [name for name in os.listdir(request_dir) if name.endswith(".json")]
                        if os.path.isdir(request_dir)
                        else []
                    )
                    if not names:
                        time.sleep(0.01)
                        continue
                    name = names[0]
                    with open(os.path.join(request_dir, name), encoding="utf-8") as stream:
                        request = json.load(stream)
                    os.makedirs(response_dir, exist_ok=True)
                    response_path = os.path.join(response_dir, name)
                    payload = json.dumps(
                        {
                            "id": request["id"],
                            "result": {"capturePath": r"D:\抓帧\白银之城\4.rdc"},
                        },
                        ensure_ascii=True,
                    )
                    observed_wire.append(payload)
                    with open(response_path, "w", encoding="utf-8") as stream:
                        stream.write(payload)
                    return

            worker = threading.Thread(target=bridge)
            worker.start()
            result = ipc_client.IPCClient(timeout=2.0).call(
                "get_current_renderdoc_context", {}
            )
            worker.join(2.0)

            self.assertEqual(r"D:\抓帧\白银之城\4.rdc", result["capturePath"])
            self.assertTrue(observed_wire)
            self.assertTrue(observed_wire[0].isascii())
            self.assertIn(r"\u6293\u5e27", observed_wire[0])

    def test_mismatched_response_id_is_rejected_and_cleaned(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.multiple(
            ipc_client, **self._paths(root)
        ):
            def bridge():
                request_dir = os.path.join(root, "requests")
                response_dir = os.path.join(root, "responses")
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    names = [
                        name for name in os.listdir(request_dir)
                        if name.endswith(".json")
                    ] if os.path.isdir(request_dir) else []
                    if names:
                        os.makedirs(response_dir, exist_ok=True)
                        with open(
                            os.path.join(response_dir, names[0]), "w", encoding="utf-8"
                        ) as stream:
                            json.dump({"id": "wrong", "result": {}}, stream)
                        return
                    time.sleep(0.01)

            worker = threading.Thread(target=bridge)
            worker.start()
            with self.assertRaisesRegex(RuntimeError, "response id"):
                ipc_client.IPCClient(timeout=2.0).call("ping", {})
            worker.join(2.0)

            self.assertEqual([], os.listdir(os.path.join(root, "responses")))

    def test_selects_most_recent_live_capture_instance(self):
        with tempfile.TemporaryDirectory() as temporary:
            instance_root = os.path.join(temporary, "instances")
            os.makedirs(instance_root)
            now = int(time.time() * 1000)
            for name, loaded, activity, pid in (
                ("older-loaded", True, now - 1000, 101),
                ("newer-unloaded", False, now, 202),
                ("newer-loaded", True, now, 303),
            ):
                directory = os.path.join(instance_root, name)
                os.makedirs(directory)
                with open(
                    os.path.join(directory, "instance.json"), "w", encoding="utf-8"
                ) as stream:
                    json.dump(
                        {
                            "instanceId": name,
                            "pid": pid,
                            "heartbeatUnixMs": now,
                            "lastActivityUnixMs": activity,
                            "startedUnixMs": activity,
                            "captureLoaded": loaded,
                            "stopped": False,
                        },
                        stream,
                    )
            with mock.patch.object(ipc_client, "INSTANCE_ROOT", instance_root), mock.patch.object(
                ipc_client.IPCClient, "_process_alive", return_value=True
            ):
                client = ipc_client.IPCClient()
                request_dir, response_dir = client._instance_mailbox()
            self.assertIn("newer-loaded", request_dir)
            self.assertIn("newer-loaded", response_dir)
            self.assertEqual("newer-loaded", client.instance_id)

    def test_implicit_selection_rechecks_and_switches_to_new_loaded_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            instance_root = os.path.join(temporary, "instances")
            os.makedirs(instance_root)
            now = int(time.time() * 1000)
            self._write_instance(
                instance_root,
                "45028",
                loaded=False,
                activity=now,
                pid=45028,
            )

            with mock.patch.object(
                ipc_client, "INSTANCE_ROOT", instance_root
            ), mock.patch.object(
                ipc_client.IPCClient, "_process_alive", return_value=True
            ):
                client = ipc_client.IPCClient()
                first_request_dir, _ = client._instance_mailbox()
                self.assertIn("45028", first_request_dir)
                self.assertEqual("45028", client.instance_id)

                self._write_instance(
                    instance_root,
                    "11088",
                    loaded=True,
                    activity=now - 1000,
                    pid=11088,
                )
                second_request_dir, _ = client._instance_mailbox()

            self.assertIn("11088", second_request_dir)
            self.assertEqual("11088", client.instance_id)

    def test_explicit_selection_stays_fixed_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            instance_root = os.path.join(temporary, "instances")
            os.makedirs(instance_root)
            now = int(time.time() * 1000)
            explicit_directory = self._write_instance(
                instance_root,
                "45028",
                loaded=False,
                activity=now - 1000,
                pid=45028,
            )
            self._write_instance(
                instance_root,
                "11088",
                loaded=True,
                activity=now,
                pid=11088,
            )

            with mock.patch.object(
                ipc_client, "INSTANCE_ROOT", instance_root
            ), mock.patch.object(
                ipc_client.IPCClient, "_process_alive", return_value=True
            ):
                client = ipc_client.IPCClient(instance_id="45028")
                request_dir, _ = client._instance_mailbox()
                self.assertIn("45028", request_dir)
                self.assertEqual("45028", client.instance_id)

                with open(
                    os.path.join(explicit_directory, "instance.json"),
                    "r",
                    encoding="utf-8",
                ) as stream:
                    manifest = json.load(stream)
                manifest["stopped"] = True
                with open(
                    os.path.join(explicit_directory, "instance.json"),
                    "w",
                    encoding="utf-8",
                ) as stream:
                    json.dump(manifest, stream)

                with self.assertRaisesRegex(RuntimeError, "45028"):
                    client._instance_mailbox()
                self.assertEqual("45028", client.instance_id)

    def test_direct_tool_binding_pins_one_instance_without_mutating_client(self):
        with tempfile.TemporaryDirectory() as temporary:
            instance_root = os.path.join(temporary, "instances")
            os.makedirs(instance_root)
            now = int(time.time() * 1000)
            self._write_instance(
                instance_root, "origin", loaded=True, activity=now - 1000, pid=101
            )
            self._write_instance(
                instance_root, "current", loaded=True, activity=now, pid=202
            )
            with mock.patch.object(
                ipc_client, "INSTANCE_ROOT", instance_root
            ), mock.patch.object(
                ipc_client.IPCClient, "_process_alive", return_value=True
            ):
                client = ipc_client.IPCClient()
                with ipc_client.bind_direct_tool_call("origin", "a" * 32):
                    request_dir, _ = client._instance_mailbox()
                    self.assertIn("origin", request_dir)
                    self.assertEqual("", client.instance_id)
                request_dir, _ = client._instance_mailbox()

            self.assertIn("current", request_dir)
            self.assertEqual("current", client.instance_id)

    def test_direct_tool_binding_injects_and_protects_capture_session(self):
        client = ipc_client.IPCClient()
        with mock.patch.object(client, "_call_locked", return_value={"ok": True}) as call:
            with ipc_client.bind_direct_tool_call("qrenderdoc-pinned", "b" * 32):
                result = client.call("get_pipeline_state", {"event_id": 7})
                with self.assertRaisesRegex(RuntimeError, "frozen capture session"):
                    client.call(
                        "get_pipeline_state",
                        {
                            "event_id": 7,
                            "__replaylab_expected_capture_session_id": "c" * 32,
                        },
                    )

        self.assertEqual({"ok": True}, result)
        sent = call.call_args.args[1]
        self.assertEqual(7, sent["event_id"])
        self.assertEqual(
            "b" * 32, sent["__replaylab_expected_capture_session_id"]
        )


if __name__ == "__main__":
    unittest.main()
