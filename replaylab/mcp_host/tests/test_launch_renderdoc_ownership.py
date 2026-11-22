from __future__ import annotations

import ast
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from src.ipc_client import IPCClient


def load_launch(namespace):
    """Exercise the real launch function without starting the server runtime."""
    path = Path(__file__).resolve().parents[1] / "src" / "server.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    launch = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == "launch_renderdoc")
    launch.decorator_list = []
    exec(compile(ast.Module(body=[launch], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["launch_renderdoc"]


class LaunchRenderdocOwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.capture = self.root / "synthetic.rdc"
        self.capture.write_bytes(b"synthetic capture path only")
        self.ipc = mock.Mock(spec=IPCClient)
        self.ipc.is_bridge_alive.return_value = False
        self.ipc.bound_instance_state.return_value = None
        self.launch_process = mock.Mock(return_value=SimpleNamespace(pid=789))
        self.find_exe = mock.Mock(return_value="synthetic-qrenderdoc.exe")
        self.clients = mock.Mock()
        self.clients.return_value.is_bridge_alive.return_value = True
        self.call = mock.Mock(return_value=json.dumps({"opened": True}))
        self.namespace = {
            "ipc": self.ipc, "IPCClient": self.clients, "INSTANCE_ROOT": str(self.root),
            "os": os, "json": json, "logger": logging.getLogger(__name__),
            "time": SimpleNamespace(sleep=mock.Mock()),
            "subprocess": SimpleNamespace(Popen=self.launch_process, DEVNULL=subprocess.DEVNULL),
            "_ok": json.dumps, "_call": self.call, "_find_renderdoc_exe": self.find_exe,
            "_get_renderdoc_search_paths": mock.Mock(return_value=[]),
        }
        self.launch = load_launch(self.namespace)

    def test_busy_or_missing_pinned_owner_never_starts_a_second_application(self):
        for alive, status in ((True, "busy"), (False, "bound-instance-unavailable")):
            with self.subTest(alive=alive):
                self.ipc.bound_instance_state.return_value = {"instanceId": "bound", "alive": alive}
                result = json.loads(self.launch(str(self.capture)))
                self.assertFalse(result["launched"])
                self.assertEqual(status, result["status"])
        self.launch_process.assert_not_called()
        self.find_exe.assert_not_called()

    def test_responsive_owner_reuses_open_capture_without_executable_discovery(self):
        self.ipc.is_bridge_alive.return_value = True
        result = json.loads(self.launch(str(self.capture)))
        self.assertTrue(result["opened"])
        self.call.assert_called_once_with("open_capture", {"capture_path": str(self.capture)})
        self.launch_process.assert_not_called()
        self.find_exe.assert_not_called()

    def test_other_instances_cannot_make_a_new_process_appear_ready(self):
        self.clients._manifest.return_value = {"instanceId": "older", "pid": 456}
        result = json.loads(self.launch(str(self.capture)))
        self.assertTrue(result["launched"])
        self.assertFalse(result["bridgeReady"])
        self.clients.assert_not_called()
        self.ipc.is_bridge_alive.assert_called_once()

    def test_new_process_readiness_requires_its_exact_manifest_and_pid(self):
        (self.root / "spawned").mkdir()
        self.clients._manifest.side_effect = lambda directory: (
            {"instanceId": "spawned", "pid": 789} if Path(directory).name == "spawned" else None
        )
        result = json.loads(self.launch(str(self.capture)))
        self.assertTrue(result["launched"])
        self.assertTrue(result["bridgeReady"])
        self.assertEqual("spawned", result["instanceId"])
        self.assertEqual(789, result["pid"])
        self.clients.assert_called_once_with(instance_id="spawned")


if __name__ == "__main__":
    unittest.main()
