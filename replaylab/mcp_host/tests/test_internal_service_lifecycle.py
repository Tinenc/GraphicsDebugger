from __future__ import annotations

import ast
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest import mock


REPLAYLAB_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPLAYLAB_ROOT / "renderdoc_mcp_bridge" / "__init__.py"
PROJECT_CLIENT_PATH = (
    REPLAYLAB_ROOT / "renderdoc_mcp_bridge" / "project_restore_client.py"
)
PROJECT_LAUNCHER_PATH = (
    REPLAYLAB_ROOT / "renderdoc_mcp_bridge" / "project_launcher.py"
)


class InternalServiceLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module_name = "renderdoc_mcp_bridge_internal_lifecycle_contract"
        cls.previous_renderdoc = sys.modules.get("renderdoc")
        cls.previous_qrenderdoc = sys.modules.pop("qrenderdoc", None)
        sys.modules["renderdoc"] = types.ModuleType("renderdoc")
        spec = importlib.util.spec_from_file_location(
            cls.module_name,
            BRIDGE_PATH,
            submodule_search_locations=[str(BRIDGE_PATH.parent)],
        )
        assert spec is not None and spec.loader is not None
        cls.bridge = importlib.util.module_from_spec(spec)
        sys.modules[cls.module_name] = cls.bridge
        spec.loader.exec_module(cls.bridge)

    @classmethod
    def tearDownClass(cls) -> None:
        for name in list(sys.modules):
            if name == cls.module_name or name.startswith(cls.module_name + "."):
                sys.modules.pop(name, None)
        if cls.previous_renderdoc is None:
            sys.modules.pop("renderdoc", None)
        else:
            sys.modules["renderdoc"] = cls.previous_renderdoc
        if cls.previous_qrenderdoc is not None:
            sys.modules["qrenderdoc"] = cls.previous_qrenderdoc

    def setUp(self) -> None:
        self.bridge._replaylab_host_executable = ""
        self.bridge._replaylab_host_control_process = None
        self.bridge._replaylab_host_owner_active = False
        self.bridge._replaylab_internal_host_executable = ""
        self.bridge._replaylab_internal_control_process = None
        self.bridge._replaylab_internal_owner_active = False
        with self.bridge._replaylab_control_processes_lock:
            self.bridge._replaylab_control_processes.clear()

    def test_project_ui_and_internal_start_do_not_require_external_mcp(self) -> None:
        source = BRIDGE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(BRIDGE_PATH))
        register = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_register_project_launcher_ui"
        )
        register_source = ast.get_source_segment(source, register)
        self.assertIn("_find_replaylab_internal_host", register_source)
        self.assertNotIn("_find_replaylab_host()", register_source)
        self.assertNotIn("ReplayLabMCP.exe unavailable", register_source)

        configured_internal = os.environ.pop("REPLAYLAB_INTERNAL_HOST", None)
        configured_mcp = os.environ.pop("REPLAYLAB_MCP_HOST", None)
        original_file = self.bridge.__file__
        original_has_ui = self.bridge.HAS_UI
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                integration = (
                    root / "integrations" / "replaylab" / "python"
                    / "renderdoc_mcp_bridge"
                )
                integration.mkdir(parents=True)
                bundle = root / "replaylab"
                bundle.mkdir()
                internal = bundle / "ReplayLabInternal.exe"
                internal.write_bytes(b"internal")
                self.bridge.__file__ = str(integration / "__init__.py")
                self.bridge.HAS_UI = False

                self.assertEqual(
                    str(internal.resolve()),
                    self.bridge._find_replaylab_internal_host(),
                )
                self.assertEqual("", self.bridge._find_replaylab_host())

                completed = _CompletedProcess()
                with mock.patch.object(
                    self.bridge.subprocess, "Popen", return_value=completed
                ) as popen, mock.patch.object(
                    self.bridge, "_track_replaylab_control_process",
                    wraps=self.bridge._track_replaylab_control_process,
                ) as track:
                    self.assertTrue(self.bridge._start_replaylab_internal_host())
                    self.assertTrue(completed.finished.wait(1.0))
                command = popen.call_args.args[0]
                self.assertEqual(str(internal.resolve()), command[0])
                self.assertEqual("ensure", command[1])
                self.assertNotIn("ReplayLabMCP", " ".join(command))
                track.assert_called_once_with(
                    completed, "internal-ensure", timeout_seconds=75.0)
        finally:
            self.bridge.__file__ = original_file
            self.bridge.HAS_UI = original_has_ui
            self.bridge._replaylab_internal_owner_active = False
            if configured_internal is not None:
                os.environ["REPLAYLAB_INTERNAL_HOST"] = configured_internal
            if configured_mcp is not None:
                os.environ["REPLAYLAB_MCP_HOST"] = configured_mcp

    def test_embedded_clients_only_launch_bounded_helpers_and_never_release_host(self) -> None:
        project_client_source = PROJECT_CLIENT_PATH.read_text(encoding="utf-8")
        project_launcher_source = PROJECT_LAUNCHER_PATH.read_text(encoding="utf-8")
        owners = {function.name for function in ast.walk(ast.parse(project_client_source))
                  if isinstance(function, ast.FunctionDef) and any(
                      isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                      and isinstance(call.func.value, ast.Name) and call.func.value.id == 'subprocess'
                      and call.func.attr == 'Popen' for call in ast.walk(function))}
        self.assertEqual({'_project_transport', '_ensure_host_when_missing'}, owners)
        self.assertNotIn("ensure_internal_owner", project_client_source)
        self.assertNotIn("release_registered_internal_owners", project_client_source)
        self.assertNotIn("subprocess.Popen", project_launcher_source)
        self.assertNotIn("cancel_active_host_processes", project_launcher_source)

    def test_start_owns_external_mcp_by_default_with_explicit_opt_out(self) -> None:
        bridge = self.bridge
        previous = os.environ.pop("REPLAYLAB_EXTERNAL_MCP_AUTOSTART", None)
        calls = []

        def exercise(value=None):
            if value is None:
                os.environ.pop("REPLAYLAB_EXTERNAL_MCP_AUTOSTART", None)
            else:
                os.environ["REPLAYLAB_EXTERNAL_MCP_AUTOSTART"] = value
            calls.clear()
            with mock.patch.object(
                bridge, "_start_replaylab_internal_host",
                side_effect=lambda: calls.append("internal") or True,
            ), mock.patch.object(
                bridge, "_start_replaylab_host",
                side_effect=lambda: calls.append("external") or True,
            ), mock.patch.object(
                bridge, "_reset_symbol_state",
            ), mock.patch.object(
                bridge, "_current_capture_token", return_value=None,
            ), mock.patch.object(
                bridge, "_publish_console_event",
            ), mock.patch.dict(
                os.environ,
            ):
                # A real GUI owns its environment for its whole lifetime.
                # This in-process startup probe must not pin later IPC or
                # project-console tests to its now-disposed fake instance.
                bridge.start("test", object())
            return list(calls)

        try:
            self.assertEqual(["internal", "external"], exercise())
            self.assertEqual(["internal"], exercise("0"))
            self.assertEqual(["internal"], exercise("false"))
            self.assertEqual(["internal"], exercise("NO"))
            self.assertEqual(["internal"], exercise("off"))
            self.assertEqual(["internal", "external"], exercise(""))
            self.assertEqual(["internal", "external"], exercise("unexpected"))
            self.assertEqual(["internal", "external"], exercise("true"))
            self.assertEqual(["internal", "external"], exercise("1"))
        finally:
            if previous is None:
                os.environ.pop("REPLAYLAB_EXTERNAL_MCP_AUTOSTART", None)
            else:
                os.environ["REPLAYLAB_EXTERNAL_MCP_AUTOSTART"] = previous

    def test_stop_closes_internal_callers_before_releasing_host(self) -> None:
        calls = []

        def record(name):
            def invoke(*args, **kwargs):
                del args, kwargs
                calls.append(name)
            return invoke

        bridge = self.bridge
        bridge._ctx = None
        bridge._poller = None
        bridge._capture_viewer = None
        bridge._active_capture_token = "capture"
        bridge._unregister_project_launcher_ui = record("project")
        bridge._unregister_shader_workspace_ui = record("shader-workspace")
        bridge._publish_instance_manifest = record("manifest")
        bridge._release_replaylab_internal_host_owner = record("internal-release")
        bridge._release_replaylab_host_owner = record("mcp-release")
        bridge._unregister_shader_console_ui = record("shader-console")
        bridge._unregister_pass_panel_ui = record("pass")
        bridge._unregister_material_panel_ui = record("material")
        bridge._release_event_shader_overrides = record("event-shaders")
        bridge._release_shared_shader_replacements = record("shared-shaders")
        bridge._release_event_cbuffer_snapshots = record("cbuffers")
        bridge._reset_symbol_state = record("symbols")

        bridge.stop()

        self.assertLess(calls.index("project"), calls.index("internal-release"))
        self.assertLess(
            calls.index("shader-workspace"), calls.index("internal-release")
        )
        self.assertEqual(1, calls.count("internal-release"))

    def test_shader_workspace_is_a_retryable_hard_teardown_gate(self) -> None:
        bridge = self.bridge
        calls = []
        attempts = 0

        def shader_workspace():
            nonlocal attempts
            attempts += 1
            calls.append("shader-workspace")
            if attempts == 1:
                raise RuntimeError("save worker still active")

        def record(name):
            def invoke(*args, **kwargs):
                del args, kwargs
                calls.append(name)
            return invoke

        with mock.patch.object(
            bridge, "_unregister_project_launcher_ui", side_effect=record("project")
        ), mock.patch.object(
            bridge, "_unregister_shader_workspace_ui", side_effect=shader_workspace
        ), mock.patch.object(
            bridge, "_release_replaylab_internal_host_owner",
            side_effect=record("internal-release"),
        ), mock.patch.object(
            bridge, "_release_replaylab_host_owner", side_effect=record("mcp-release")
        ), mock.patch.object(
            bridge, "_publish_instance_manifest", side_effect=record("manifest")
        ), mock.patch.object(
            bridge, "_unregister_shader_console_ui", side_effect=record("shader-console")
        ), mock.patch.object(
            bridge, "_unregister_pass_panel_ui", side_effect=record("pass")
        ), mock.patch.object(
            bridge, "_unregister_material_panel_ui", side_effect=record("material")
        ), mock.patch.object(
            bridge, "_release_event_shader_overrides", side_effect=record("event-shaders")
        ), mock.patch.object(
            bridge, "_release_shared_shader_replacements", side_effect=record("shared-shaders")
        ), mock.patch.object(
            bridge, "_release_event_cbuffer_snapshots", side_effect=record("cbuffers")
        ), mock.patch.object(
            bridge, "_reset_symbol_state", side_effect=record("symbols")
        ):
            bridge._ctx = None
            bridge._poller = None
            bridge._capture_viewer = None
            bridge._active_capture_token = "capture"

            with self.assertRaisesRegex(RuntimeError, "save worker still active"):
                bridge.stop()
            self.assertNotIn("internal-release", calls)
            self.assertNotIn("mcp-release", calls)
            self.assertEqual("capture", bridge._active_capture_token)

            bridge.stop()

        self.assertEqual(2, calls.count("project"))
        self.assertEqual(2, calls.count("shader-workspace"))
        self.assertEqual(1, calls.count("internal-release"))
        self.assertEqual(1, calls.count("mcp-release"))

    def test_shader_workspace_unregister_retains_failed_panel_for_retry(self) -> None:
        bridge = self.bridge

        class RetryPanel:
            def __init__(self):
                self.attempts = 0

            def shutdown(self):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("save worker still active")

        panel = RetryPanel()
        callback = lambda: None
        bridge._shader_workspace_ui = panel
        bridge._shader_workspace_callbacks = [callback]

        with self.assertRaisesRegex(RuntimeError, "save worker still active"):
            bridge._unregister_shader_workspace_ui()
        self.assertIs(panel, bridge._shader_workspace_ui)
        self.assertEqual([callback], bridge._shader_workspace_callbacks)

        bridge._unregister_shader_workspace_ui()
        self.assertIsNone(bridge._shader_workspace_ui)
        self.assertEqual([], bridge._shader_workspace_callbacks)

    def test_internal_owner_release_is_idempotent_and_service_scoped(self) -> None:
        calls = []
        bridge = self.bridge
        bridge._replaylab_internal_owner_active = True
        bridge._replaylab_internal_host_executable = "C:/ReplayLabInternal.exe"
        bridge._drain_replaylab_control_processes = (
            lambda label, timeout_seconds=5.0:
            calls.append(("drain", label, timeout_seconds))
        )
        bridge._release_packaged_owner = (
            lambda executable, label: calls.append(("release", executable, label))
        )

        bridge._release_replaylab_internal_host_owner()
        bridge._release_replaylab_internal_host_owner()

        self.assertEqual(
            [
                ("drain", "internal-ensure", 5.0),
                ("release", "C:/ReplayLabInternal.exe", "internal host"),
            ],
            calls,
        )

    def test_control_reaper_is_bounded_and_drain_does_not_mix_services(self) -> None:
        timed_out = _TimedOutProcess()
        self.bridge._track_replaylab_control_process(timed_out, "mcp-ensure")
        self.assertTrue(timed_out.finished.wait(1.0))
        self.assertEqual([30.0, 2.0], timed_out.waits)
        self.assertEqual(1, timed_out.kills)
        with self.bridge._replaylab_control_processes_lock:
            self.assertNotIn(timed_out, self.bridge._replaylab_control_processes)

        internal = _HangingProcess()
        external = _HangingProcess()
        with self.bridge._replaylab_control_processes_lock:
            self.bridge._replaylab_control_processes.update({
                internal: "internal-ensure",
                external: "mcp-ensure",
            })
        self.bridge._drain_replaylab_control_processes(
            "internal-ensure", timeout_seconds=0.0
        )
        self.assertEqual(1, internal.kills)
        self.assertEqual(0, external.kills)
        with self.bridge._replaylab_control_processes_lock:
            self.assertNotIn(internal, self.bridge._replaylab_control_processes)
            self.assertIn(external, self.bridge._replaylab_control_processes)


class _CompletedProcess(object):
    def __init__(self):
        self.finished = threading.Event()

    def wait(self, timeout=None):
        del timeout
        self.finished.set()
        return 0

    def poll(self):
        return 0

    def kill(self):
        raise AssertionError("a completed ensure process must not be killed")


class _TimedOutProcess(object):
    def __init__(self):
        self.waits = []
        self.kills = 0
        self.finished = threading.Event()

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if len(self.waits) == 1:
            raise TimeoutError("still running")
        self.finished.set()
        return 0

    def poll(self):
        return None if not self.finished.is_set() else 0

    def kill(self):
        self.kills += 1


class _HangingProcess(object):
    def __init__(self):
        self.kills = 0
        self.finished = False

    def wait(self, timeout=None):
        del timeout
        if not self.finished:
            raise TimeoutError("still running")
        return 0

    def poll(self):
        return 0 if self.finished else None

    def kill(self):
        self.kills += 1
        self.finished = True


if __name__ == "__main__":
    unittest.main()
