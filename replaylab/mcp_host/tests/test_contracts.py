from __future__ import annotations

import ast
import builtins
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]


class McpContractTests(unittest.TestCase):
    def test_capture_load_defers_project_restore_without_starting_client_or_subprocess_inline(self) -> None:
        bridge_path = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
        source = bridge_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(bridge_path))
        lifecycle = next(
            candidate
            for statement in tree.body
            if isinstance(statement, ast.If)
            for candidate in statement.body
            if isinstance(candidate, ast.ClassDef)
            and candidate.name == "CaptureLifecycleViewer"
        )
        on_capture_loaded = next(
            node
            for node in lifecycle.body
            if isinstance(node, ast.FunctionDef) and node.name == "OnCaptureLoaded"
        )

        calls = {"schedule": 0, "projectClient": 0, "popen": 0}

        def _record(name):
            def _callback(*args, **kwargs):
                del args, kwargs
                calls[name] += 1
            return _callback

        namespace = {
            "_active_capture_token": None,
            "_replay_recovery_path": "",
            "_material_panel_ui": None,
            "_material_panel_controller": None,
            "_pass_panel_ui": None,
            "_abandon_project_launcher_restore": lambda: False,
            "_reset_symbol_state": lambda restore_names: None,
            "_current_capture_token": lambda: "capture-token",
            "_publish_instance_manifest": lambda **kwargs: None,
            "_publish_console_event": lambda *args, **kwargs: None,
            "_shader_workspace_capture_binding": lambda: None,
            "_notify_shader_capture_changed": lambda binding: None,
            "_schedule_project_auto_activation": _record("schedule"),
            "_schedule_frame_render_flow_restore": lambda: None,
            "_project_launcher_protocol": types.SimpleNamespace(
                ReplayLabProjectClient=_record("projectClient")
            ),
            "subprocess": types.SimpleNamespace(Popen=_record("popen")),
        }
        executable = ast.Module(body=[on_capture_loaded], type_ignores=[])
        ast.fix_missing_locations(executable)
        exec(compile(executable, str(bridge_path), "exec"), namespace)

        # ``OnCaptureLoaded`` invalidates capture-local event selection via
        # ``self._remember_current_events()``; the isolated exec needs a fake
        # self that carries that no-op so this contract test still exercises
        # only the project-client/subprocess restraint it is about.
        fake_self = types.SimpleNamespace(_remember_current_events=lambda: None)
        namespace["OnCaptureLoaded"](fake_self)

        self.assertEqual("capture-token", namespace["_active_capture_token"])
        self.assertEqual(
            {"schedule": 1, "projectClient": 0, "popen": 0}, calls
        )

    def test_bridge_restore_client_imports_without_embedded_network_modules(self) -> None:
        bridge_path = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
        fake_renderdoc = types.ModuleType("renderdoc")
        previous_renderdoc = sys.modules.get("renderdoc")
        previous_qrenderdoc = sys.modules.pop("qrenderdoc", None)
        original_import = builtins.__import__
        module_name = "renderdoc_mcp_bridge_optional_restore_contract"

        def _without_network_modules(
            name, globals=None, locals=None, fromlist=(), level=0
        ):
            root = str(name or "").split(".", 1)[0]
            if root in ("urllib", "http", "socket", "_socket"):
                raise ImportError("optional embedded network transport unavailable")
            return original_import(name, globals, locals, fromlist, level)

        sys.modules["renderdoc"] = fake_renderdoc
        builtins.__import__ = _without_network_modules
        try:
            spec = importlib.util.spec_from_file_location(
                module_name,
                bridge_path,
                submodule_search_locations=[str(bridge_path.parent)],
            )
            bridge = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = bridge
            spec.loader.exec_module(bridge)
        finally:
            builtins.__import__ = original_import
            for name in list(sys.modules):
                if name == module_name or name.startswith(module_name + "."):
                    sys.modules.pop(name, None)
            if previous_renderdoc is None:
                sys.modules.pop("renderdoc", None)
            else:
                sys.modules["renderdoc"] = previous_renderdoc
            if previous_qrenderdoc is not None:
                sys.modules["qrenderdoc"] = previous_qrenderdoc

        self.assertIsNotNone(bridge._project_restore_client_protocol)
        self.assertEqual("", bridge._project_restore_client_import_error)
        self.assertTrue(hasattr(
            bridge._project_restore_client_protocol,
            "ReplayLabRestoreHostClient",
        ))
        self.assertIsNotNone(bridge._shader_console_protocol)
        self.assertIsNotNone(bridge._dxil_access_protocol)

    def test_bridge_stop_hard_gates_on_project_executor_failure(self) -> None:
        bridge_path = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
        fake_renderdoc = types.ModuleType("renderdoc")
        previous_renderdoc = sys.modules.get("renderdoc")
        previous_qrenderdoc = sys.modules.pop("qrenderdoc", None)
        sys.modules["renderdoc"] = fake_renderdoc
        try:
            spec = importlib.util.spec_from_file_location(
                "renderdoc_mcp_bridge_stop_contract", bridge_path
            )
            bridge = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bridge)
        finally:
            if previous_renderdoc is None:
                sys.modules.pop("renderdoc", None)
            else:
                sys.modules["renderdoc"] = previous_renderdoc
            if previous_qrenderdoc is not None:
                sys.modules["qrenderdoc"] = previous_qrenderdoc

        calls = []

        class _Poller(object):
            def stop(self):
                calls.append("poller")
                raise RuntimeError("poller failure")

        class _Context(object):
            def RemoveCaptureViewer(self, viewer):
                self.viewer = viewer
                calls.append("viewer")

        def _record(name):
            def _callback(*args, **kwargs):
                del args, kwargs
                calls.append(name)
            return _callback

        def _project_failure():
            calls.append("project")
            raise RuntimeError("executor failure")

        bridge._poller = _Poller()
        bridge._ctx = _Context()
        bridge._capture_viewer = object()
        bridge._active_capture_token = "capture"
        bridge._publish_instance_manifest = _record("manifest")
        bridge._release_replaylab_host_owner = _record("host")
        bridge._unregister_shader_console_ui = _record("console")
        bridge._unregister_event_browser_filter_ui = _record("filters")
        bridge._unregister_project_launcher_ui = _project_failure
        bridge._unregister_pass_panel_ui = _record("pass")
        bridge._unregister_material_panel_ui = _record("material")
        bridge._release_event_shader_overrides = _record("shader-overrides")
        bridge._release_shared_shader_replacements = _record("shader-replacements")
        bridge._release_event_cbuffer_snapshots = _record("cbuffer")
        bridge._reset_symbol_state = _record("symbols")

        with self.assertRaisesRegex(RuntimeError, "executor failure"):
            bridge.stop()

        self.assertEqual(["project"], calls)
        self.assertIsNotNone(bridge._poller)
        self.assertIsNotNone(bridge._capture_viewer)
        self.assertEqual("capture", bridge._active_capture_token)
        self.assertIsNotNone(bridge._ctx)

    def test_project_executor_requires_explicit_shutdown_confirmation(self) -> None:
        bridge_path = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
        fake_renderdoc = types.ModuleType("renderdoc")
        previous_renderdoc = sys.modules.get("renderdoc")
        previous_qrenderdoc = sys.modules.pop("qrenderdoc", None)
        module_name = "renderdoc_mcp_bridge_executor_shutdown_contract"
        sys.modules["renderdoc"] = fake_renderdoc
        try:
            spec = importlib.util.spec_from_file_location(module_name, bridge_path)
            bridge = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = bridge
            spec.loader.exec_module(bridge)
        finally:
            if previous_renderdoc is None:
                sys.modules.pop("renderdoc", None)
            else:
                sys.modules["renderdoc"] = previous_renderdoc
            if previous_qrenderdoc is not None:
                sys.modules["qrenderdoc"] = previous_qrenderdoc

        calls = []

        class _Executor(object):
            def __init__(self):
                self.results = [False, True]

            def shutdown(self):
                calls.append("shutdown")
                return self.results.pop(0)

        bridge._project_restore_client_protocol = types.SimpleNamespace(
            begin_restore_shutdown=lambda: calls.append("begin") or 9,
            wait_restore_quiescent=lambda generation, timeout: (
                calls.append(("wait", generation, timeout)) or True
            ),
        )
        bridge._project_async_executor = _Executor()
        bridge._project_launcher_ui = None
        bridge._project_launcher_callbacks = []
        bridge._project_thumbnail_cache = None
        bridge._project_thumbnail_reader = None
        bridge._project_startup_timer = None
        bridge._cancel_project_auto_activation = lambda: None
        executor = bridge._project_async_executor

        with self.assertRaisesRegex(RuntimeError, "did not confirm shutdown"):
            bridge._unregister_project_launcher_ui()
        self.assertIs(bridge._project_async_executor, executor)

        bridge._unregister_project_launcher_ui()
        self.assertIsNone(bridge._project_async_executor)
        self.assertEqual(
            ["begin", "shutdown", "begin", "shutdown", ("wait", 9, 0.0)],
            calls,
        )

    def test_native_shutdown_gates_context_and_extension_destruction(self) -> None:
        capture_source = (
            REPOSITORY.parent / "qrenderdoc" / "Code" / "CaptureContext.cpp"
        ).read_text(encoding="utf-8")
        destructor_start = capture_source.index("CaptureContext::~CaptureContext()")
        destructor_end = capture_source.index(
            "void CaptureContext::Begin", destructor_start
        )
        destructor = capture_source[destructor_start:destructor_end]
        self.assertGreaterEqual(
            destructor.count("PythonContext::ShutdownInternalIntegrations()"), 2
        )
        fatal = destructor.index("qFatal(")
        extension_shutdown = destructor.index("PythonContext::ShutdownExtensions()")
        replay_shutdown = destructor.index("m_Replay.CloseThread()")
        self.assertLess(fatal, extension_shutdown)
        self.assertLess(fatal, replay_shutdown)

        python_source = (
            REPOSITORY.parent
            / "qrenderdoc"
            / "Code"
            / "pyrenderdoc"
            / "PythonContext.cpp"
        ).read_text(encoding="utf-8")
        global_start = python_source.index("void PythonContext::GlobalShutdown()")
        global_shutdown = python_source[global_start:]
        failure_gate = global_shutdown.index("if(!internalIntegrationsStopped")
        extension_shutdown = global_shutdown.index("ShutdownExtensions();")
        self.assertLess(failure_gate, extension_shutdown)

    def test_bridge_atomic_json_is_ascii_wire_and_round_trips_chinese_path(self) -> None:
        bridge_path = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
        fake_renderdoc = types.ModuleType("renderdoc")
        previous_renderdoc = sys.modules.get("renderdoc")
        previous_qrenderdoc = sys.modules.pop("qrenderdoc", None)
        sys.modules["renderdoc"] = fake_renderdoc
        try:
            spec = importlib.util.spec_from_file_location(
                "renderdoc_mcp_bridge_unicode_wire_contract", bridge_path
            )
            bridge = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bridge)
        finally:
            if previous_renderdoc is None:
                sys.modules.pop("renderdoc", None)
            else:
                sys.modules["renderdoc"] = previous_renderdoc
            if previous_qrenderdoc is not None:
                sys.modules["qrenderdoc"] = previous_qrenderdoc

        expected = {
            "id": "unicode-response",
            "result": {
                "capturePath": r"D:\抓帧\白银之城\4.rdc",
                "error": "无法读取中文路径",
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "response.json"
            bridge._atomic_write_json(str(path), expected)
            wire = path.read_bytes()

        self.assertTrue(wire.isascii())
        self.assertIn(b"\\u6293\\u5e27", wire)
        self.assertEqual(expected, json.loads(wire.decode("ascii")))

        original_file = bridge.__file__
        original_has_ui = bridge.HAS_UI
        configured = os.environ.pop("REPLAYLAB_MCP_HOST", None)
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                integration_dir = (
                    root
                    / "integrations"
                    / "replaylab"
                    / "python"
                    / "renderdoc_mcp_bridge"
                )
                integration_dir.mkdir(parents=True)
                host_dir = root / "replaylab"
                host_dir.mkdir()
                unicode_host = host_dir / "BreadMCP.unicode.exe"
                legacy_host = host_dir / "BreadMCP.exe"
                unicode_host.write_bytes(b"unicode-host")
                legacy_host.write_bytes(b"legacy-host")
                bridge.__file__ = str(integration_dir / "__init__.py")
                bridge.HAS_UI = False

                self.assertEqual(
                    str(unicode_host.resolve()), bridge._find_replaylab_host()
                )
        finally:
            bridge.__file__ = original_file
            bridge.HAS_UI = original_has_ui
            if configured is not None:
                os.environ["REPLAYLAB_MCP_HOST"] = configured

    def test_project_capture_open_uses_current_replay_options_contract_once(self) -> None:
        bridge_path = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
        fake_renderdoc = types.ModuleType("renderdoc")
        previous_renderdoc = sys.modules.get("renderdoc")
        previous_qrenderdoc = sys.modules.pop("qrenderdoc", None)
        sys.modules["renderdoc"] = fake_renderdoc
        try:
            spec = importlib.util.spec_from_file_location(
                "renderdoc_mcp_bridge_open_capture_contract", bridge_path
            )
            bridge = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bridge)
        finally:
            if previous_renderdoc is None:
                sys.modules.pop("renderdoc", None)
            else:
                sys.modules["renderdoc"] = previous_renderdoc
            if previous_qrenderdoc is not None:
                sys.modules["qrenderdoc"] = previous_qrenderdoc

        replay_options = object()

        class _Context(object):
            def __init__(self, error=None, on_load=None, loads=True):
                self.calls = []
                self.error = error
                self.on_load = on_load
                self.loads = loads
                self.loaded = False
                self.capture_path = ""

            def Config(self):
                return types.SimpleNamespace(DefaultReplayOptions=replay_options)

            def LoadCapture(self, *arguments):
                self.calls.append(arguments)
                if self.error is not None:
                    raise self.error
                self.loaded = self.loads
                self.capture_path = arguments[0] if self.loads else ""
                if self.on_load is not None:
                    self.on_load(*arguments)

            def IsCaptureLoaded(self):
                return self.loaded

            def GetCaptureFilename(self):
                return self.capture_path

        lifecycle_effects = []
        bridge._release_event_shader_overrides = lambda: lifecycle_effects.append("shader-overrides")
        bridge._release_shared_shader_replacements = lambda: lifecycle_effects.append("shared-replacements")
        bridge._release_event_cbuffer_snapshots = lambda: lifecycle_effects.append("cbuffer-snapshots")
        def _reset_symbol_state(restore_names):
            lifecycle_effects.append(("reset-symbols", restore_names))
            bridge._capture_session_id = "c" * 32

        bridge._reset_symbol_state = _reset_symbol_state
        bridge._current_capture_token = lambda: "capture-token"
        path = "C:/captures/frame.rdc"
        context = _Context()
        bridge._ctx = context
        expected_open = {
            "opened": os.path.abspath(path),
            "captureSessionId": "c" * 32,
        }
        self.assertEqual(expected_open, bridge._open_capture({"capture_path": path}))
        self.assertEqual(
            [(path, replay_options, path, False, True)], context.calls
        )
        self.assertFalse(bridge._consume_preselected_project_capture(path))

        # An Agent retrying its current RDC must not emit capture callbacks,
        # clear a live replacement, mint a new session, or cancel its own turn.
        prior_effects = list(lifecycle_effects)
        prior_token = bridge._active_capture_token
        same_path = os.path.join(os.path.dirname(path), ".", os.path.basename(path))
        unchanged = bridge._open_capture({"capture_path": same_path})
        self.assertEqual({**expected_open, "alreadyOpen": True}, unchanged)
        self.assertEqual(1, len(context.calls))
        self.assertEqual(prior_effects, lifecycle_effects)
        self.assertEqual(prior_token, bridge._active_capture_token)
        with self.assertRaisesRegex(ValueError, "force_reload must be a boolean"):
            bridge._open_capture({"capture_path": path, "force_reload": "true"})
        self.assertEqual(prior_effects, lifecycle_effects)
        self.assertEqual(expected_open, bridge._open_capture({
            "capture_path": path, "force_reload": True,
        }))
        self.assertEqual(2, len(context.calls))
        self.assertGreater(len(lifecycle_effects), len(prior_effects))
        other_path = "C:/captures/other-frame.rdc"
        switched = bridge._open_capture({"capture_path": other_path})
        self.assertEqual(os.path.abspath(other_path), switched["opened"])
        self.assertNotIn("alreadyOpen", switched)
        self.assertEqual(3, len(context.calls))

        association_skips = []
        project_context = _Context(
            on_load=lambda capture_path, *unused: association_skips.append(
                bridge._consume_preselected_project_capture(capture_path)
            )
        )
        bridge._ctx = project_context
        self.assertEqual(
            expected_open, bridge._open_capture_for_preselected_project(path)
        )
        self.assertEqual([True], association_skips)
        self.assertEqual(
            [(path, replay_options, path, False, True)], project_context.calls
        )
        # An explicit project activation retains its existing open/association
        # lifecycle even when that project happens to use the same loaded RDC.
        self.assertEqual(expected_open, bridge._open_capture_for_preselected_project(path))
        self.assertEqual([True, True], association_skips)
        self.assertEqual(2, len(project_context.calls))

        lease_writes = []
        status_messages = []
        project_context.GetCaptureFilename = lambda: path
        bridge.HAS_UI = True
        bridge._ctx = project_context
        bridge._project_preselected_capture_path = bridge._capture_path_key(path)
        bridge._atomic_write_json = lambda *args: lease_writes.append(args)
        bridge._show_project_background_status = (
            lambda *args: status_messages.append(args)
        )
        bridge.QtCore = types.SimpleNamespace(
            QCoreApplication=types.SimpleNamespace(
                translate=lambda unused_context, source: source
            )
        )
        bridge._schedule_project_auto_activation()
        self.assertEqual([], lease_writes)
        self.assertEqual(
            [("Using the project selected for this RDC", 5000)],
            status_messages,
        )

        failing = _Context(TypeError("binding failure"))
        bridge._ctx = failing
        with self.assertRaisesRegex(TypeError, "binding failure"):
            bridge._open_capture({"capture_path": path})
        self.assertEqual(1, len(failing.calls))

        bridge._ctx = failing
        with self.assertRaisesRegex(TypeError, "binding failure"):
            bridge._open_capture_for_preselected_project(path)
        self.assertFalse(bridge._consume_preselected_project_capture(path))

        incomplete = _Context(loads=False)
        bridge._ctx = incomplete
        failure = bridge._open_capture({"capture_path": path})
        self.assertEqual("renderdoc-open-incomplete", failure["errorId"])
        self.assertIn("did not finish opening", failure["error"])
        self.assertEqual(os.path.abspath(path), failure["requested"])

    def test_project_launcher_arguments_do_not_require_embedded_sys_argv(self) -> None:
        bridge_path = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
        fake_renderdoc = types.ModuleType("renderdoc")
        previous_renderdoc = sys.modules.get("renderdoc")
        previous_qrenderdoc = sys.modules.pop("qrenderdoc", None)
        sys.modules["renderdoc"] = fake_renderdoc
        try:
            spec = importlib.util.spec_from_file_location(
                "renderdoc_mcp_bridge_project_arguments_contract", bridge_path
            )
            bridge = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bridge)
        finally:
            if previous_renderdoc is None:
                sys.modules.pop("renderdoc", None)
            else:
                sys.modules["renderdoc"] = previous_renderdoc
            if previous_qrenderdoc is not None:
                sys.modules["qrenderdoc"] = previous_qrenderdoc

        bridge.HAS_UI = False
        bridge.sys = types.SimpleNamespace()
        self.assertEqual([], bridge._project_launcher_process_arguments())

        class _Application(object):
            @staticmethod
            def arguments():
                return ["qrenderdoc.exe", "--replayhost=worker"]

        bridge.HAS_UI = True
        bridge.QtCore = types.SimpleNamespace(
            QCoreApplication=types.SimpleNamespace(instance=lambda: _Application())
        )
        self.assertEqual(
            ["--replayhost=worker"],
            bridge._project_launcher_process_arguments(),
        )

    def test_pass_panel_close_reopen_and_unregister_are_instance_bound(self) -> None:
        bridge_path = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
        fake_renderdoc = types.ModuleType("renderdoc")
        previous_renderdoc = sys.modules.get("renderdoc")
        previous_qrenderdoc = sys.modules.pop("qrenderdoc", None)
        sys.modules["renderdoc"] = fake_renderdoc
        try:
            spec = importlib.util.spec_from_file_location(
                "renderdoc_mcp_bridge_pass_lifecycle_contract", bridge_path
            )
            bridge = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bridge)
        finally:
            if previous_renderdoc is None:
                sys.modules.pop("renderdoc", None)
            else:
                sys.modules["renderdoc"] = previous_renderdoc
            if previous_qrenderdoc is not None:
                sys.modules["qrenderdoc"] = previous_qrenderdoc

        class _Panel(object):
            instances = []

            def __init__(self, ctx, qrd_module, set_event, closed_callback):
                self.ctx = ctx
                self.qrd_module = qrd_module
                self.set_event = set_event
                self.closed_callback = closed_callback
                self.show_count = 0
                self.shutdown_count = 0
                self.__class__.instances.append(self)

            def show(self):
                self.show_count += 1

            def shutdown(self):
                self.shutdown_count += 1

        class _Extensions(object):
            def __init__(self):
                self.window_callbacks = []
                self.panel_callbacks = []

            def RegisterWindowMenu(self, _base, _path, callback):
                self.window_callbacks.append(callback)

            def RegisterPanelMenu(self, _base, _path, callback):
                self.panel_callbacks.append(callback)

        extensions = _Extensions()
        context = types.SimpleNamespace(Extensions=lambda: extensions)
        bridge.qrd = types.SimpleNamespace(
            WindowMenu=types.SimpleNamespace(Window="window"),
            PanelMenu=types.SimpleNamespace(EventBrowser="event-browser"),
        )
        bridge._pass_panel_protocol = types.SimpleNamespace(
            MiniQtPassPanel=_Panel
        )

        bridge._register_pass_panel_ui(context)
        first = bridge._pass_panel_ui
        self.assertIsNotNone(first.closed_callback)
        first_close = first.closed_callback
        first_close()
        self.assertIsNone(bridge._pass_panel_ui)

        extensions.window_callbacks[0](None, None)
        second = bridge._pass_panel_ui
        self.assertIsNot(first, second)
        self.assertEqual(second.show_count, 1)

        # A delayed destroyed callback from the old native QWidget is scoped to
        # that old panel and cannot discard the newly-created dock.
        first_close()
        self.assertIs(bridge._pass_panel_ui, second)

        bridge._unregister_pass_panel_ui()
        self.assertIsNone(bridge._pass_panel_ui)
        self.assertEqual(second.shutdown_count, 1)
        self.assertEqual(bridge._pass_panel_callbacks, [])

    def test_replay_project_tools_are_registered(self) -> None:
        server_path = REPOSITORY / "mcp_host" / "src" / "server.py"
        tree = ast.parse(server_path.read_text(encoding="utf-8"))
        tools = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "tool"
                for decorator in node.decorator_list
            )
        }
        expected = {
            "create_replay_project",
            "get_replay_project_status",
            "import_replay_project_capture",
            "register_replay_project_workspace",
            "register_replay_project_alias_table",
            "save_replay_project_material_knowledge",
            "get_replay_project_reuse_plan",
            "seed_shader_workspace_from_replay_project",
        }
        self.assertTrue(expected.issubset(tools), expected - tools)
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        create_arguments = {
            argument.arg
            for argument in functions["create_replay_project"].args.args
        }
        self.assertIn("game_id", create_arguments)
        self.assertIn("build_id", create_arguments)
        self.assertNotIn("scope", create_arguments)
        seed_arguments = {
            argument.arg
            for argument in functions[
                "seed_shader_workspace_from_replay_project"
            ].args.args
        }
        self.assertEqual(
            seed_arguments, {"project_path", "knowledge_id", "workspace_path"}
        )

    def test_symbol_tools_are_registered(self) -> None:
        server_path = REPOSITORY / "mcp_host" / "src" / "server.py"
        tree = ast.parse(server_path.read_text(encoding="utf-8"))
        tools = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "tool"
                for decorator in node.decorator_list
            )
        }
        expected = {
            "export_capture_symbol_catalog",
            "scan_asset_symbols",
            "match_capture_assets",
            "resolve_capture_symbols",
            "get_symbol_match_cases",
            "get_symbol_match_evidence",
            "list_symbol_matches",
            "submit_symbol_match",
            "export_symbol_alias_table",
            "apply_symbol_alias_table",
            "rollback_symbol_alias_table",
        }
        self.assertTrue(expected.issubset(tools), expected - tools)

        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        submit_args = {
            argument.arg for argument in functions["submit_symbol_match"].args.args
        }
        self.assertIn("entry_point", submit_args)
        self.assertNotIn("user_confirmed", submit_args)
        apply_args = {
            argument.arg
            for argument in functions["apply_symbol_alias_table"].args.args
        }
        self.assertNotIn("confirmed_only", apply_args)
        self.assertNotIn("path", apply_args)
        self.assertIn("database_path", apply_args)
        self.assertIn("capture_id", apply_args)
        rollback_args = {
            argument.arg
            for argument in functions["rollback_symbol_alias_table"].args.args
        }
        self.assertEqual(
            rollback_args, {"capture_id", "expected_transaction_id"}
        )

    def test_bridge_symbol_methods_are_present(self) -> None:
        bridge_path = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
        source = bridge_path.read_text(encoding="utf-8")
        ast.parse(source)
        self.assertIn('"export_shader_catalog": _export_shader_catalog', source)
        self.assertIn('"reflect_shader_candidate": _reflect_shader_candidate', source)
        self.assertIn(
            '"apply_shader_candidate_override": _apply_shader_candidate_override',
            source,
        )
        self.assertIn(
            '"apply_shader_candidate_overrides": _apply_shader_candidate_overrides',
            source,
        )
        self.assertIn(
            '"apply_shader_candidate_shared_resource": '
            "_apply_shader_candidate_shared_resource",
            source,
        )
        self.assertIn(
            '"rollback_shader_candidate_override": _rollback_shader_candidate_override',
            source,
        )
        self.assertIn(
            '"rollback_shader_candidate_overrides": _rollback_shader_candidate_overrides',
            source,
        )
        self.assertIn(
            '"rollback_shader_candidate_shared_resource": '
            "_rollback_shader_candidate_shared_resource",
            source,
        )
        self.assertIn('"load_symbol_aliases": _load_symbol_aliases', source)
        self.assertIn(
            '"rollback_symbol_aliases": _rollback_symbol_aliases', source
        )

        tree = ast.parse(source)
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
        }
        shader_info_source = ast.get_source_segment(
            source, functions["_get_shader_info"]
        )
        self.assertIn("_available_shader_stages", shader_info_source)
        self.assertIn("unsupported shader stage", shader_info_source)
        self.assertIn('"api"', shader_info_source)
        self.assertIn('"encoding"', shader_info_source)
        self.assertIn('"entryPoints"', shader_info_source)
        self.assertIn('"compiledInterface"', shader_info_source)
        self.assertIn("_compiled_shader_interface(refl)", shader_info_source)
        self.assertIn("_pipeline_shader_entry_selection", shader_info_source)
        self.assertIn("_shader_entry_selection_failure", shader_info_source)
        self.assertIn("_shader_reflection_identity_failure", shader_info_source)
        selector_source = ast.get_source_segment(
            source, functions["_pipeline_shader_entry_selection"]
        )
        selection_failure_source = ast.get_source_segment(
            source, functions["_shader_entry_selection_failure"]
        )
        reflection_failure_source = ast.get_source_segment(
            source, functions["_shader_reflection_identity_failure"]
        )
        self.assertIn('"shader-entry-point-ambiguous"', selector_source)
        self.assertIn('"shader-entry-point-unavailable"', selector_source)
        self.assertIn('"unknowns"', selection_failure_source)
        self.assertIn(
            '"shader-reflection-identity-mismatch"', reflection_failure_source
        )
        self.assertIn('"entryPointSelection"', shader_info_source)
        self.assertIn('"roleInference"', shader_info_source)
        self.assertIn('"variablesStatus"', shader_info_source)
        self.assertIn('"sectionStatus"', shader_info_source)
        self.assertIn('"evidenceContract"', shader_info_source)
        self.assertNotIn(
            "rd.ShaderStage.Fragment)", shader_info_source,
            "unknown task/mesh/ray stages must not silently inspect the pixel shader",
        )
        self.assertIn('(\"task\", \"Task\")', source)
        self.assertIn('(\"mesh\", \"Mesh\")', source)

    def test_shader_catalog_restore_export_uses_async_replay_job_protocol(self) -> None:
        bridge_path = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
        source = bridge_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(bridge_path))
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
        }
        begin_source = ast.get_source_segment(
            source, functions["_begin_export_shader_catalog"]
        )
        export_source = ast.get_source_segment(
            source, functions["_export_shader_catalog"]
        )
        consume_source = ast.get_source_segment(
            source, functions["_consume_export_shader_catalog"]
        )

        self.assertIn("Replay().AsyncInvoke(", begin_source)
        self.assertNotIn("BlockInvoke", begin_source)
        self.assertIn("catalog, blobs = _do(replay_controller)", export_source)
        self.assertIn("defer_publication=True", source)
        self.assertIn("_commit_catalog_cache_publication(", consume_source)
        quiet_node = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "CONSOLE_QUIET_REQUEST_METHODS"
                for target in node.targets
            )
        )
        quiet_source = ast.get_source_segment(source, quiet_node)
        self.assertIn('"poll_export_shader_catalog"', quiet_source)
        for method in (
            "begin_export_shader_catalog",
            "poll_export_shader_catalog",
            "cancel_export_shader_catalog",
            "consume_export_shader_catalog",
        ):
            self.assertIn('"%s": _%s' % (method, method), source)

    def test_shader_catalog_async_job_is_lightweight_pollable_and_consumed_once(self) -> None:
        bridge_path = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
        fake_renderdoc = types.ModuleType("renderdoc")
        previous_renderdoc = sys.modules.get("renderdoc")
        previous_qrenderdoc = sys.modules.pop("qrenderdoc", None)
        module_name = "renderdoc_mcp_bridge_async_catalog_contract"
        sys.modules["renderdoc"] = fake_renderdoc
        try:
            spec = importlib.util.spec_from_file_location(module_name, bridge_path)
            bridge = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = bridge
            spec.loader.exec_module(bridge)

            callbacks = []
            exported = []
            published = []
            capture_path = os.path.abspath("C:/captures/frame.rdc")
            capture_token = ("loaded", os.path.normcase(capture_path))

            class Replay:
                def AsyncInvoke(self, method, tag=""):
                    assert callable(method) and isinstance(tag, str)
                    self.tag = tag
                    callbacks.append(method)

            class Context:
                def IsCaptureLoaded(self):
                    return True

                def CurEvent(self):
                    return 42

                def GetCaptureFilename(self):
                    return capture_path

                def Replay(self):
                    return Replay()

            bridge._ctx = Context()
            bridge._capture_session_id = "c" * 32
            bridge._active_capture_token = capture_token
            bridge._available_shader_stages = (
                lambda unused=None: [("vertex", object())]
            )

            def fake_export(params, replay_controller=None,
                            capture_snapshot=None, cancel_event=None,
                            progress_job_id="", defer_publication=False,
                            defer_finalization=False):
                del params, cancel_event
                self.assertIsNotNone(replay_controller)
                self.assertTrue(defer_publication)
                self.assertTrue(defer_finalization)
                exported.append(progress_job_id)
                bridge._update_shader_catalog_export_progress(
                    progress_job_id, 418, 3, 9
                )
                return {"captureSnapshot": capture_snapshot}

            def fake_finalize(collected, cancel_event=None, defer_publication=False):
                self.assertTrue(defer_publication)
                self.assertEqual(capture_token, collected["captureSnapshot"]["captureToken"])
                return {
                    "schema": bridge.SYMBOL_CATALOG_SCHEMA,
                    "schemaVersion": bridge.SYMBOL_CATALOG_VERSION,
                    "captureId": "d" * 64,
                    "catalogPath": os.path.abspath("C:/catalog.json"),
                    "blobDirectory": os.path.abspath("C:/blobs"),
                    "counts": {},
                    "identityEvidenceSha256": "e" * 64,
                    "_deferredPublication": {"prepared": True},
                }

            bridge._export_shader_catalog = fake_export
            bridge._finalize_shader_catalog_export = fake_finalize
            bridge._commit_catalog_cache_publication = published.append

            started = bridge._begin_export_shader_catalog({
                "output_dir": os.path.abspath("C:/export"),
                "event_ids": [418],
            })
            self.assertEqual("queued", started["status"])
            self.assertEqual([], exported)
            self.assertEqual(1, len(callbacks))

            callbacks.pop(0)(object())
            with bridge._shader_catalog_export_jobs_guard:
                workers = tuple(bridge._shader_catalog_finalize_workers)
            for worker in workers:
                worker.join(2.0)
                self.assertFalse(worker.is_alive())
            polled = bridge._poll_export_shader_catalog({
                "job_id": started["jobId"],
            })
            self.assertEqual("completed", polled["status"])
            self.assertEqual((3, 9, 418), (
                polled["current"], polled["total"], polled["eventId"],
            ))
            self.assertNotIn("result", polled)

            consumed = bridge._consume_export_shader_catalog({
                "job_id": started["jobId"],
            })
            self.assertEqual("completed", consumed["status"])
            self.assertEqual("d" * 64, consumed["result"]["captureId"])
            self.assertEqual(1, len(published))
            with self.assertRaisesRegex(ValueError, "Unknown or expired"):
                bridge._consume_export_shader_catalog({
                    "job_id": started["jobId"],
                })

            queued = bridge._begin_export_shader_catalog({
                "output_dir": os.path.abspath("C:/export-two"),
                "event_ids": [419],
            })
            cancelled = bridge._cancel_export_shader_catalog({
                "job_id": queued["jobId"],
            })
            self.assertEqual("cancelled", cancelled["status"])
            self.assertTrue(cancelled["cancellationRequested"])
            callbacks.pop(0)(object())
            self.assertEqual(1, len(exported))
        finally:
            for name in list(sys.modules):
                if name == module_name or name.startswith(module_name + "."):
                    sys.modules.pop(name, None)
            if previous_renderdoc is None:
                sys.modules.pop("renderdoc", None)
            else:
                sys.modules["renderdoc"] = previous_renderdoc
            if previous_qrenderdoc is not None:
                sys.modules["qrenderdoc"] = previous_qrenderdoc

    def test_bridge_compiled_interface_helpers_are_python_only_and_lossless(self) -> None:
        bridge_path = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
        fake_renderdoc = types.ModuleType("renderdoc")
        previous = sys.modules.get("renderdoc")
        sys.modules["renderdoc"] = fake_renderdoc
        try:
            spec = importlib.util.spec_from_file_location(
                "renderdoc_mcp_bridge_contract", bridge_path
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            if previous is None:
                sys.modules.pop("renderdoc", None)
            else:
                sys.modules["renderdoc"] = previous

        parameter = types.SimpleNamespace(
            varName="uv",
            semanticName="TEXCOORD",
            semanticIdxName="TEXCOORD0",
            semanticIndex=0,
            regIndex=1,
            systemValue="Undefined",
            varType="Float",
            compCount=2,
            stream=0,
            regChannelMask=3,
            channelUsedMask=3,
            perPrimitiveRate=False,
            needSemanticIndex=False,
        )
        signature = module._signature_parameter_dict(parameter)
        self.assertEqual(signature["semanticName"], "TEXCOORD")
        self.assertEqual(signature["regIndex"], 1)
        self.assertEqual(signature["compCount"], 2)
        self.assertEqual(signature["compType"], "Float")

        variable_type = types.SimpleNamespace(
            name="float4",
            baseType="Float",
            rows=1,
            columns=4,
            elements=1,
            arrayByteStride=0,
            matrixByteStride=0,
            flags=0,
            pointerTypeID=0xFFFFFFFF,
            members=[],
            RowMajor=lambda: False,
        )
        binding = types.SimpleNamespace(
            name="MainTex",
            fixedBindSetOrSpace=2,
            fixedBindNumber=5,
            bindArraySize=1,
            descriptorType="ReadOnlyImage",
            textureType="Texture2D",
            variableType=variable_type,
            isTexture=True,
            hasSampler=False,
            isInputAttachment=False,
            isReadOnly=True,
        )
        physical = module._shader_binding_dict(binding)
        self.assertEqual((physical["space"], physical["bind"]), (2, 5))
        self.assertTrue(physical["isTexture"])
        self.assertEqual(physical["descriptorType"], "ReadOnlyImage")
        self.assertEqual(physical["variableType"]["columns"], 4)
        sampler = module._shader_sampler_dict(
            types.SimpleNamespace(
                name="LinearSampler",
                fixedBindSetOrSpace=0,
                fixedBindNumber=3,
                bindArraySize=1,
            )
        )
        self.assertEqual(sampler, {
            "name": "LinearSampler", "space": 0, "bind": 3, "arraySize": 1,
        })
        self.assertEqual(module._thread_group_size(
            types.SimpleNamespace(stage="ShaderStage.Compute", dispatchThreadsDimension=[8, 8, 1])
        ), [8, 8, 1])

        opaque_type = types.SimpleNamespace(
            name="",
            baseType="VarType.UInt",
            rows=1,
            columns=4,
            elements=0,
            arrayByteStride=16,
            matrixByteStride=16,
            flags=1,
            pointerTypeID=0xFFFFFFFF,
            members=[],
            RowMajor=lambda: True,
        )
        opaque_block = types.SimpleNamespace(
            name="cbuffer0",
            fixedBindNumber=0,
            fixedBindSetOrSpace=0,
            bindArraySize=1,
            byteSize=8,
            bufferBacked=True,
            inlineDataBytes=False,
            compileConstants=False,
            variables=[
                types.SimpleNamespace(
                    name="unknown", byteOffset=0, type=opaque_type
                )
            ],
        )
        opaque = module._constant_block_dict(opaque_block, 0)
        self.assertEqual(opaque["byteSize"], 8)
        self.assertEqual(opaque["fields"], [])
        self.assertEqual(opaque["layoutCompleteness"], "ambiguous")

        opaque_full_register = types.SimpleNamespace(**vars(opaque_block))
        opaque_full_register.byteSize = 16
        opaque_full_register.variables = [
            types.SimpleNamespace(
                name="unknown",
                byteOffset=0,
                type=types.SimpleNamespace(
                    name="",
                    baseType="VarType.UInt",
                    rows=1,
                    columns=4,
                    elements=1,
                    arrayByteStride=16,
                    matrixByteStride=16,
                    flags=1,
                    pointerTypeID=0xFFFFFFFF,
                    members=[],
                    RowMajor=lambda: True,
                ),
            )
        ]
        # A complete unnamed physical range is not sufficient evidence of an
        # opaque block.  Retain it so later ABI comparison remains strict.
        self.assertEqual(
            len(module._constant_block_dict(opaque_full_register, 0)["fields"]), 1
        )

        dxil_layout_evidence = {
            "kind": "dxc-dxil-buffer-definition",
            "typeAnnotation": "absent",
            "sourceSha256": "a" * 64,
            "accessSignatureSha256": "b" * 64,
        }
        unannotated = module._constant_block_dict(
            opaque_full_register,
            0,
            layout_evidence=dxil_layout_evidence,
        )
        self.assertEqual(unannotated["layoutCompleteness"], "ambiguous")
        self.assertEqual(len(unannotated["fields"]), 1)
        self.assertEqual(unannotated["layoutEvidence"], dxil_layout_evidence)

        # Display/member names do not participate in this classification.
        renamed_unannotated = types.SimpleNamespace(**vars(opaque_full_register))
        renamed_unannotated.name = "ArbitraryBlockName"
        renamed_unannotated.variables = [types.SimpleNamespace(
            name="ArbitraryMemberName",
            byteOffset=0,
            type=opaque_full_register.variables[0].type,
        )]
        renamed = module._constant_block_dict(
            renamed_unannotated,
            0,
            layout_evidence=dxil_layout_evidence,
        )
        self.assertEqual(renamed["layoutCompleteness"], "ambiguous")
        self.assertEqual(
            module._physical_shader_interface({
                "stage": "fragment",
                "entryPoint": "main",
                "inputSignature": [],
                "outputSignature": [],
                "readOnlyResources": [],
                "readWriteResources": [],
                "samplers": [],
                "constantBlocks": [unannotated],
                "threadGroupSize": [],
                "outputTopology": "TriangleList",
                "dynamicLinkageInterfaceCount": 0,
            }),
            module._physical_shader_interface({
                "stage": "fragment",
                "entryPoint": "main",
                "inputSignature": [],
                "outputSignature": [],
                "readOnlyResources": [],
                "readWriteResources": [],
                "samplers": [],
                "constantBlocks": [renamed],
                "threadGroupSize": [],
                "outputTopology": "TriangleList",
                "dynamicLinkageInterfaceCount": 0,
            }),
        )

        typed_block = types.SimpleNamespace(
            name="PerMaterial",
            fixedBindNumber=0,
            fixedBindSetOrSpace=0,
            bindArraySize=1,
            byteSize=16,
            bufferBacked=True,
            inlineDataBytes=False,
            compileConstants=False,
            variables=[
                types.SimpleNamespace(
                    name="Tint", byteOffset=0, type=variable_type
                )
            ],
        )
        typed = module._constant_block_dict(typed_block, 0)
        self.assertEqual(len(typed["fields"]), 1)
        self.assertEqual(typed["fields"][0]["byteSize"], 16)

        partial_block = types.SimpleNamespace(**vars(typed_block))
        partial_block.byteSize = 16
        partial_block.variables = list(typed_block.variables) + list(opaque_block.variables)
        partial_fields = module._constant_block_dict(partial_block, 0)["fields"]
        self.assertEqual(len(partial_fields), 1)
        self.assertEqual(partial_fields[0]["name"], "Tint")

        overlapping_unknown = types.SimpleNamespace(
            name="unknown", byteOffset=0, type=opaque_full_register.variables[0].type
        )
        mixed_overlap = types.SimpleNamespace(**vars(typed_block))
        mixed_overlap.variables = list(typed_block.variables) + [overlapping_unknown]
        mixed_fields = module._constant_block_dict(mixed_overlap, 0)["fields"]
        self.assertEqual(len(mixed_fields), 1)
        self.assertEqual(mixed_fields[0]["name"], "Tint")

        def reflection(block):
            return types.SimpleNamespace(
                stage="Pixel",
                entryPoint="main",
                encoding="DXIL",
                inputSignature=[],
                outputSignature=[],
                readOnlyResources=[],
                readWriteResources=[],
                samplers=[],
                constantBlocks=[block],
                interfaces=[],
                outputTopology="TriangleList",
                dispatchThreadsDimension=[0, 0, 0],
            )

        same_range_typed_block = types.SimpleNamespace(**vars(typed_block))
        same_range_typed_block.byteSize = 8
        same_range_typed_block.variables = [
            types.SimpleNamespace(
                name="PackedValue",
                byteOffset=0,
                type=types.SimpleNamespace(
                    name="",
                    baseType="VarType.UInt",
                    rows=1,
                    columns=2,
                    elements=1,
                    arrayByteStride=0,
                    matrixByteStride=0,
                    flags=1,
                    pointerTypeID=0xFFFFFFFF,
                    members=[],
                    RowMajor=lambda: True,
                ),
            )
        ]
        # The captured uint4 placeholder exceeds this 8-byte block and was
        # already classified opaque. No captured member type is established;
        # a valid uint2 describes the same verifiable binding/storage/range.
        # This mirrors Host test_opaque_cbuffer_compares_only_verifiable_physical_range.
        self.assertTrue(module._physical_shader_interfaces_compatible(
            reflection(opaque_block), reflection(same_range_typed_block)
        ))
        different_range = types.SimpleNamespace(**vars(same_range_typed_block))
        different_range.byteSize = 12
        self.assertFalse(module._physical_shader_interfaces_compatible(
            reflection(opaque_block), reflection(different_range)
        ))
        for field, value in (("fixedBindNumber", 1), ("fixedBindSetOrSpace", 2),
                             ("bindArraySize", 2), ("bufferBacked", False)):
            with self.subTest(opaque_binding_field=field):
                changed_binding = types.SimpleNamespace(**vars(same_range_typed_block))
                setattr(changed_binding, field, value)
                self.assertFalse(module._physical_shader_interfaces_compatible(
                    reflection(opaque_block), reflection(changed_binding)
                ))
        different_typed = types.SimpleNamespace(**vars(same_range_typed_block))
        changed_type = types.SimpleNamespace(**vars(same_range_typed_block.variables[0].type))
        changed_type.baseType = "VarType.Float"
        different_typed.variables = [types.SimpleNamespace(
            name="PackedValue", byteOffset=0, type=changed_type)]
        # Known typed layouts still retain their exact member contract even
        # when byte sizes match (as does the typed 44-byte size/packing gate).
        self.assertFalse(module._physical_shader_interfaces_compatible(
            reflection(same_range_typed_block), reflection(different_typed)
        ))

        parser_path = (
            REPOSITORY / "renderdoc_mcp_bridge" / "dxil_access_signature.py"
        )
        parser_spec = importlib.util.spec_from_file_location(
            "replaylab_bridge_dxil_access_contract", parser_path
        )
        parser_module = importlib.util.module_from_spec(parser_spec)
        parser_spec.loader.exec_module(parser_module)
        from tests.test_dxil_access_signature import REAL_SHAPE_DXIL

        exact_access = parser_module.extract_dxil_cbuffer_access_signature(
            REAL_SHAPE_DXIL
        )
        self.assertTrue(module._physical_shader_interfaces_compatible(
            reflection(opaque_block), reflection(opaque_block),
            baseline_dxil_access=exact_access,
            candidate_dxil_access=exact_access,
        ))
        changed_type_ir = REAL_SHAPE_DXIL.replace(
            b"%20 = call %dx.types.CBufRet.i32 @dx.op.cbufferLoadLegacy.i32",
            b"%20 = call %dx.types.CBufRet.f32 @dx.op.cbufferLoadLegacy.f32",
        ).replace(
            b"%21 = extractvalue %dx.types.CBufRet.i32 %20, 0",
            b"%21 = extractvalue %dx.types.CBufRet.f32 %20, 0",
        )
        changed_access = parser_module.extract_dxil_cbuffer_access_signature(
            changed_type_ir
        )
        # Access tuples are not member-layout evidence for an opaque block.
        # This is ABI compatibility only, not numerical/semantic equivalence.
        self.assertTrue(module._physical_shader_interfaces_compatible(
            reflection(opaque_block), reflection(opaque_block),
            baseline_dxil_access=exact_access,
            candidate_dxil_access=changed_access,
        ))
        self.assertFalse(module._physical_shader_interfaces_compatible(
            reflection(same_range_typed_block), reflection(opaque_block),
            baseline_dxil_access=exact_access,
            candidate_dxil_access=exact_access,
        ))

    def test_bridge_reflects_exact_candidate_and_always_frees_target(self) -> None:
        bridge_path = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
        fake_renderdoc = types.ModuleType("renderdoc")

        class ResourceId:
            def __init__(self, value=0):
                self.value = value

            def __int__(self):
                return self.value

        fake_renderdoc.ResourceId = ResourceId
        fake_renderdoc.ShaderEncoding = types.SimpleNamespace(DXBC="DXBC")
        fake_renderdoc.ShaderStage = types.SimpleNamespace(
            Pixel="Pixel", Fragment="Pixel"
        )
        fake_renderdoc.ShaderCompileFlags = lambda: object()
        previous = sys.modules.get("renderdoc")
        sys.modules["renderdoc"] = fake_renderdoc
        try:
            spec = importlib.util.spec_from_file_location(
                "renderdoc_mcp_bridge_candidate_contract", bridge_path
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            if previous is None:
                sys.modules.pop("renderdoc", None)
            else:
                sys.modules["renderdoc"] = previous

        blob = b"DXBC candidate bytes"
        reflection = types.SimpleNamespace(
            stage="Pixel",
            entryPoint="main",
            encoding="DXBC",
            rawBytes=blob,
            inputSignature=[],
            outputSignature=[],
            readOnlyResources=[],
            readWriteResources=[],
            samplers=[],
            constantBlocks=[],
            outputTopology="TriangleList",
            dispatchThreadsDimension=[0, 0, 0],
        )

        class Controller:
            def __init__(self):
                self.freed = []

            def GetAPIProperties(self):
                return types.SimpleNamespace(pipelineType="GraphicsAPI.D3D11")

            def GetTargetShaderEncodings(self):
                return ["DXBC"]

            def BuildTargetShader(self, entry, encoding, source, flags, stage):
                self.build = (entry, encoding, source, stage)
                return ResourceId(77), ""

            def GetShaderEntryPoints(self, shader):
                return [types.SimpleNamespace(name="main", stage="Pixel")]

            def GetShader(self, pipeline, shader, entry):
                return reflection

            def FreeTargetResource(self, shader):
                self.freed.append(int(shader))

        controller = Controller()
        module._ctx = types.SimpleNamespace(IsCaptureLoaded=lambda: True)
        module._replay = lambda callback: callback(controller)

        exact_entry = types.SimpleNamespace(name="main", stage="Pixel")
        self.assertIs(
            module._exact_shader_entry([exact_entry], "main", "fragment"),
            exact_entry,
        )
        self.assertIsNone(module._exact_shader_entry(
            [types.SimpleNamespace(name="main", stage="Vertex")],
            "main",
            "fragment",
        ))
        self.assertIsNone(module._exact_shader_entry(
            [exact_entry, types.SimpleNamespace(name="main", stage="Pixel")],
            "main",
            "fragment",
        ))

        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / "candidate.dxbc"
            candidate.write_bytes(blob)
            result = module._reflect_shader_candidate(
                {
                    "candidate_path": str(candidate),
                    "expected_sha256": hashlib.sha256(blob).hexdigest(),
                    "stage": "fragment",
                    "entry_point": "main",
                    "encoding": "dxbc",
                }
            )
            with self.assertRaisesRegex(ValueError, "expected_sha256"):
                module._reflect_shader_candidate(
                    {
                        "candidate_path": str(candidate),
                        "expected_sha256": "0" * 64,
                        "stage": "fragment",
                        "entry_point": "main",
                    }
                )

        self.assertTrue(result["buildTargetShaderSucceeded"])
        self.assertTrue(result["reflectionSucceeded"])
        self.assertTrue(result["freeTargetResourceSucceeded"])
        self.assertEqual(result["compiledInterface"]["stage"], "fragment")
        self.assertEqual(result["compiledInterface"]["entryPoint"], "main")
        self.assertEqual(
            result["compiledInterface"]["outputTopology"], "TriangleList"
        )
        self.assertEqual(controller.freed, [77])

    def test_bridge_event_shader_override_is_scoped_and_target_owned(self) -> None:
        bridge_path = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
        fake_renderdoc = types.ModuleType("renderdoc")

        class ResourceId:
            def __init__(self, value=0):
                self.value = value

            def __int__(self):
                return self.value

        fake_renderdoc.ResourceId = ResourceId
        fake_renderdoc.ShaderEncoding = types.SimpleNamespace(DXBC="DXBC")
        fake_renderdoc.ShaderStage = types.SimpleNamespace(
            Pixel="Pixel", Fragment="Pixel"
        )
        fake_renderdoc.ShaderCompileFlags = lambda: object()
        previous = sys.modules.get("renderdoc")
        sys.modules["renderdoc"] = fake_renderdoc
        try:
            spec = importlib.util.spec_from_file_location(
                "renderdoc_mcp_bridge_override_contract", bridge_path
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            if previous is None:
                sys.modules.pop("renderdoc", None)
            else:
                sys.modules["renderdoc"] = previous

        baseline = b"captured DXBC bytes"
        candidate = b"edited DXBC bytes"
        original_id = ResourceId(77)
        original_reflection = types.SimpleNamespace(
            entryPoint="main", rawBytes=baseline, interfaces=[]
        )

        class Pipeline:
            def GetShader(self, stage):
                return original_id

            def GetShaderEntryPoint(self, stage):
                return "main"

            def GetGraphicsPipelineObject(self):
                return ResourceId(5)

        class Controller:
            def __init__(self):
                self.calls = []
                self.freed = []
                self.override_error = ""
                self.fail_event = None
                self.next_target = 99
                self.target_blobs = {}
                self.current_event = 0

            def GetAPIProperties(self):
                return types.SimpleNamespace(pipelineType="GraphicsAPI.D3D11")

            def SetFrameEvent(self, event, force):
                self.calls.append(("SetFrameEvent", event, force))
                self.current_event = event

            def GetPipelineState(self):
                return Pipeline()

            def GetD3D11PipelineState(self):
                return types.SimpleNamespace(
                    pixelShader=types.SimpleNamespace(
                        resourceId=original_id, classInstances=[]
                    )
                )

            def GetShaderEntryPoints(self, shader):
                return [types.SimpleNamespace(name="main", stage="Pixel")]

            def GetShader(self, pipeline, shader, entry):
                if int(shader) == int(original_id):
                    return original_reflection
                return types.SimpleNamespace(
                    entryPoint="main",
                    rawBytes=self.target_blobs[int(shader)],
                    interfaces=[],
                )

            def GetTargetShaderEncodings(self):
                return ["DXBC"]

            def BuildTargetShader(self, entry, encoding, source, flags, stage):
                self.calls.append(("BuildTargetShader", source))
                target = ResourceId(self.next_target)
                self.next_target += 1
                self.target_blobs[int(target)] = bytes(source)
                return target, ""

            def SetEventShaderOverride(self, event, stage, original, target):
                self.calls.append(
                    ("SetEventShaderOverride", event, int(original), int(target))
                )
                if event != self.current_event:
                    return "eventId must be the currently selected effective event"
                if self.fail_event == event:
                    return "live override validation failed"
                return self.override_error

            def ClearEventShaderOverride(self, event, stage):
                self.calls.append(("ClearEventShaderOverride", event))

            def FreeTargetResource(self, target):
                self.freed.append(int(target))

        controller = Controller()
        capture_path = "D:/captures/frame.rdc"
        capture_id = "c" * 64
        second_capture_id = "e" * 64
        module._ctx = types.SimpleNamespace(
            IsCaptureLoaded=lambda: True,
            CurEvent=lambda: 5,
            GetCaptureFilename=lambda: capture_path,
        )
        module._symbol_capture_snapshot = {
            "captureId": capture_id,
            "capturePath": capture_path,
            "shaderKeys": set(),
            "usesByKey": {},
        }
        canonical_capture_path = os.path.normcase(os.path.abspath(capture_path))
        module._known_catalog_capture_ids = {
            capture_id: canonical_capture_path,
            second_capture_id: canonical_capture_path,
        }
        module._capture_session_id = "7" * 32
        module._replay = lambda callback: callback(controller)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "candidate.dxbc"
            path.write_bytes(candidate)
            def apply_params(event_id, expected_capture_id=capture_id):
                return {
                    "candidate_path": str(path),
                    "expected_sha256": hashlib.sha256(candidate).hexdigest(),
                    "expected_capture_id": expected_capture_id,
                    "event_id": event_id,
                    "stage": "fragment",
                    "entry_point": "main",
                    "encoding": "dxbc",
                    "expected_original_resource_id": 77,
                    "expected_original_sha256": hashlib.sha256(baseline).hexdigest(),
                    "workspace_id": "b" * 32,
                    "revision": 4,
                    "abi_request_id": "a" * 64,
                    "abi_report_sha256": "d" * 64,
                }

            applied = module._apply_shader_candidate_override(apply_params(321))
            self.assertTrue(applied["eventScoped"])
            self.assertTrue(applied["appliedToCurrentEvent"])
            self.assertTrue(applied["originalShaderPreserved"])
            self.assertEqual(controller.calls[0], ("SetFrameEvent", 321, True))
            self.assertEqual(controller.calls[-1], ("SetFrameEvent", 5, True))
            self.assertEqual(controller.freed, [])
            self.assertFalse(
                any(call[0] == "ReplaceResource" for call in controller.calls)
            )

            # Repeating the exact apply is idempotent: no second target and no
            # second event override are created.
            repeated = module._apply_shader_candidate_override(apply_params(321))
            self.assertEqual(repeated["overrideId"], applied["overrideId"])
            self.assertTrue(repeated["idempotent"])
            self.assertEqual(
                len([call for call in controller.calls
                     if call[0] == "BuildTargetShader"]), 1)
            module._ctx.CurEvent = lambda: 0
            zero_start = len(controller.calls)
            module._apply_shader_candidate_override(apply_params(321))
            self.assertFalse(
                any(
                    call == ("SetFrameEvent", 0, True)
                    for call in controller.calls[zero_start:]
                )
            )
            module._ctx.CurEvent = lambda: 5

            # Exporting another event/stage subset for the same physical capture
            # produces a different catalog captureId. Both identities and both
            # event overrides remain usable, while the same candidate bytes and
            # physical ABI reuse one target Shader family. Per-workspace audit
            # request/report IDs do not split that physical target.
            module._symbol_capture_snapshot["captureId"] = second_capture_id
            second_params = apply_params(322, second_capture_id)
            second_params.update({
                "workspace_id": "f" * 32,
                "revision": 1,
                "abi_request_id": "2" * 64,
                "abi_report_sha256": "3" * 64,
            })
            second = module._apply_shader_candidate_override(second_params)
            self.assertEqual(len(module._event_shader_overrides), 2)
            self.assertEqual(second["targetResourceId"], applied["targetResourceId"])
            self.assertEqual(second["shaderFamilyId"], applied["shaderFamilyId"])
            self.assertEqual(second["targetReferenceCount"], 2)
            self.assertTrue(second["targetShared"])
            self.assertEqual(
                len([call for call in controller.calls
                     if call[0] == "BuildTargetShader"]), 1)

            rolled_back = module._rollback_shader_candidate_override(
                {
                    "override_id": applied["overrideId"],
                    "expected_capture_id": capture_id,
                }
            )
            self.assertFalse(rolled_back["targetFreed"])
            self.assertEqual(rolled_back["targetReferenceCount"], 1)
            self.assertEqual(controller.freed, [])
            self.assertEqual(controller.current_event, 5)
            module._ctx.CurEvent = lambda: 0
            zero_rollback_start = len(controller.calls)
            second_rolled_back = module._rollback_shader_candidate_override(
                {
                    "override_id": second["overrideId"],
                    "expected_capture_id": second_capture_id,
                }
            )
            self.assertFalse(
                any(
                    call == ("SetFrameEvent", 0, True)
                    for call in controller.calls[zero_rollback_start:]
                )
            )
            module._ctx.CurEvent = lambda: 5
            self.assertTrue(second_rolled_back["targetFreed"])
            self.assertEqual(controller.freed, [99])

            different_rid = apply_params(321)
            different_rid["events"] = [
                {
                    "event_id": 321,
                    "expected_original_resource_id": 77,
                    "expected_original_sha256": hashlib.sha256(
                        baseline).hexdigest(),
                },
                {
                    "event_id": 322,
                    "expected_original_resource_id": 78,
                    "expected_original_sha256": hashlib.sha256(
                        baseline).hexdigest(),
                },
            ]
            with self.assertRaisesRegex(
                    ValueError, "preserve one captured shader ResourceId"):
                module._apply_shader_candidate_overrides(different_rid)

            # Preserve an older event override if a later family transaction
            # fails after changing its first event. The just-built candidate
            # target is freed; captured Shader 77 is never freed.
            module._symbol_capture_snapshot["captureId"] = capture_id
            restored_original = module._apply_shader_candidate_override(
                apply_params(321))
            controller.fail_event = 322
            replacement = b"second edited DXBC bytes"
            replacement_path = Path(temporary) / "replacement.dxbc"
            replacement_path.write_bytes(replacement)
            batch_params = apply_params(321)
            batch_params.update({
                "candidate_path": str(replacement_path),
                "expected_sha256": hashlib.sha256(replacement).hexdigest(),
                "abi_report_sha256": "e" * 64,
                "events": [
                    {
                        "event_id": 321,
                        "expected_original_resource_id": 77,
                        "expected_original_sha256": hashlib.sha256(
                            baseline).hexdigest(),
                    },
                    {
                        "event_id": 322,
                        "expected_original_resource_id": 77,
                        "expected_original_sha256": hashlib.sha256(
                            baseline).hexdigest(),
                    },
                ],
            })
            with self.assertRaisesRegex(
                    RuntimeError, "event 322: live override validation failed"):
                module._apply_shader_candidate_overrides(batch_params)
            self.assertIn(
                restored_original["overrideId"], module._event_shader_overrides)
            self.assertEqual(
                module._event_shader_overrides[
                    restored_original["overrideId"]]["targetResourceId"], 100)
            self.assertIn(101, controller.freed)
            self.assertNotIn(77, controller.freed)

            controller.fail_event = None
            restored_rollback = module._rollback_shader_candidate_override({
                "override_id": restored_original["overrideId"],
                "expected_capture_id": capture_id,
            })
            self.assertTrue(restored_rollback["targetFreed"])

            original_reflection.interfaces = ["dynamic-interface"]
            with self.assertRaisesRegex(RuntimeError, "dynamic linkage interfaces"):
                module._apply_shader_candidate_override(apply_params(323))
            self.assertEqual(controller.current_event, 5)
            original_reflection.interfaces = []
            controller.override_error = "live override validation failed"
            with self.assertRaisesRegex(RuntimeError, "live override validation failed"):
                module._apply_shader_candidate_override(apply_params(321))
        self.assertTrue(rolled_back["cleared"])
        self.assertTrue(second_rolled_back["targetFreed"])
        self.assertNotIn(77, controller.freed)

    def test_bridge_shared_shader_replacement_preserves_one_resource_owner(self) -> None:
        from unittest import mock

        bridge_path = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
        fake_renderdoc = types.ModuleType("renderdoc")

        class ResourceId:
            def __init__(self, value=0):
                self.value = value

            def __int__(self):
                return self.value

        fake_renderdoc.ResourceId = ResourceId
        fake_renderdoc.ShaderEncoding = types.SimpleNamespace(
            DXBC="DXBC", SPIRV="SPIRV", OpenGLSPIRV="OpenGLSPIRV"
        )
        fake_renderdoc.ShaderStage = types.SimpleNamespace(
            Pixel="Pixel", Fragment="Pixel"
        )
        fake_renderdoc.ShaderCompileFlags = lambda: object()
        previous = sys.modules.get("renderdoc")
        sys.modules["renderdoc"] = fake_renderdoc
        try:
            spec = importlib.util.spec_from_file_location(
                "renderdoc_mcp_bridge_shared_contract", bridge_path
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            if previous is None:
                sys.modules.pop("renderdoc", None)
            else:
                sys.modules["renderdoc"] = previous

        baseline = b"captured shared DXBC"
        candidate = b"edited shared DXBC"

        def reflection(blob):
            return types.SimpleNamespace(
                entryPoint="main", rawBytes=blob, interfaces=[], encoding="DXBC"
            )

        class Pipeline:
            def __init__(self, original):
                self.original = original

            def GetShader(self, stage):
                return self.original

            def GetShaderEntryPoint(self, stage):
                return "main"

            def GetGraphicsPipelineObject(self):
                return ResourceId(5)

        class Controller:
            def __init__(self):
                self.original_id = ResourceId(77)
                self.original_blobs = {77: baseline, 78: baseline}
                self.target_blobs = {}
                self.next_target = 100
                self.calls = []
                self.freed = []
                self.replacements = {}
                self.api = "GraphicsAPI.D3D11"
                self.fail_checked_target = None
                self.fail_checked_remove = False

            def GetAPIProperties(self):
                return types.SimpleNamespace(pipelineType=self.api)

            def SetFrameEvent(self, event, force):
                self.calls.append(("SetFrameEvent", event, force))

            def GetPipelineState(self):
                return Pipeline(self.original_id)

            def GetD3D11PipelineState(self):
                return types.SimpleNamespace(
                    pixelShader=types.SimpleNamespace(
                        resourceId=self.original_id, classInstances=[]
                    )
                )

            def GetShaderEntryPoints(self, shader):
                return [types.SimpleNamespace(name="main", stage="Pixel")]

            def GetShader(self, pipeline, shader, entry):
                value = int(shader)
                # D3D12 resolves the canonical captured Shader ResourceId
                # through the active resource replacement when reflecting a
                # replayed pipeline.  This is the real behaviour that exposed
                # the rev1 -> rev2 supersede bug: the pipeline still reports
                # ResourceId 77 while GetShader(77) returns target 100's bytes.
                # Vulkan/OpenGL GetShader look up the requested creation
                # record, even after a checked pipeline replacement.
                if self.api == "GraphicsAPI.D3D12" and value in self.replacements:
                    value = int(self.replacements[value])
                blob = self.original_blobs.get(value, self.target_blobs.get(value))
                return reflection(blob) if blob is not None else None

            def GetTargetShaderEncodings(self):
                return ["DXBC", "SPIRV", "OpenGLSPIRV"]

            def BuildTargetShader(self, entry, encoding, source, flags, stage):
                target = ResourceId(self.next_target)
                self.next_target += 1
                self.target_blobs[int(target)] = bytes(source)
                self.calls.append(("BuildTargetShader", int(target)))
                return target, ""

            def ReplaceResource(self, original, target):
                self.calls.append(("ReplaceResource", int(original), int(target)))
                self.replacements[int(original)] = target

            def RemoveReplacement(self, original):
                self.calls.append(("RemoveReplacement", int(original)))
                self.replacements.pop(int(original), None)

            def ReplaceTargetShaderChecked(self, original, target):
                self.calls.append((
                    "ReplaceTargetShaderChecked", int(original), int(target)))
                if int(target) == self.fail_checked_target:
                    return "injected checked PSO rebuild failure"
                self.replacements[int(original)] = target
                return ""

            def RemoveTargetShaderReplacementChecked(self, original):
                self.calls.append((
                    "RemoveTargetShaderReplacementChecked", int(original)))
                if self.fail_checked_remove:
                    return "injected checked PSO rollback failure"
                self.replacements.pop(int(original), None)
                return ""

            def FreeTargetResource(self, target):
                self.calls.append(("FreeTargetResource", int(target)))
                self.freed.append(int(target))

        class Context:
            def __init__(self):
                self.replacements = {}
                self.resources = [
                    types.SimpleNamespace(resourceId=ResourceId(77)),
                    types.SimpleNamespace(resourceId=ResourceId(78)),
                ]
                self.calls = []
                self.fail_next_register = False

            def IsCaptureLoaded(self):
                return True

            def CurEvent(self):
                return 5

            def GetCaptureFilename(self):
                return "D:/captures/shared.rdc"

            def GetResources(self):
                return self.resources

            def IsResourceReplaced(self, original):
                return int(original) in self.replacements

            def GetResourceReplacement(self, original):
                return self.replacements.get(int(original), ResourceId())

            def RegisterReplacement(self, original, target):
                self.calls.append(("RegisterReplacement", int(original), int(target)))
                if self.fail_next_register:
                    self.fail_next_register = False
                    raise RuntimeError("register failed")
                self.replacements[int(original)] = target

            def UnregisterReplacement(self, original):
                self.calls.append(("UnregisterReplacement", int(original)))
                self.replacements.pop(int(original), None)

        controller = Controller()
        context = Context()
        capture_id = "c" * 64
        capture_path = context.GetCaptureFilename()
        module._ctx = context
        module._symbol_capture_snapshot = {
            "captureId": capture_id,
            "capturePath": capture_path,
            "shaderKeys": set(),
            "usesByKey": {},
        }
        module._known_catalog_capture_ids = {
            capture_id: os.path.normcase(os.path.abspath(capture_path))
        }
        module._capture_session_id = "7" * 32
        module._replay = lambda callback: callback(controller)

        with tempfile.TemporaryDirectory() as temporary:
            candidate_path = Path(temporary) / "candidate.dxbc"
            candidate_path.write_bytes(candidate)

            def params(workspace="b" * 32, revision=1, blob=candidate,
                       path=candidate_path, resource_id=77):
                return {
                    "candidate_path": str(path),
                    "expected_sha256": hashlib.sha256(blob).hexdigest(),
                    "expected_capture_id": capture_id,
                    "event_id": 321,
                    "stage": "fragment",
                    "entry_point": "main",
                    "encoding": "dxbc",
                    "expected_original_resource_id": resource_id,
                    "expected_original_sha256": hashlib.sha256(baseline).hexdigest(),
                    "workspace_id": workspace,
                    "revision": revision,
                    "abi_request_id": "a" * 64,
                    "abi_report_sha256": "d" * 64,
                }

            first_params = params()
            first_params["operation_id"] = "1" * 32
            first = module._apply_shader_candidate_shared_resource(first_params)
            self.assertTrue(first["resourceScoped"])
            self.assertFalse(first["eventScoped"])
            self.assertTrue(first["allOriginalResourceUsesAffected"])
            self.assertEqual(int(context.replacements[77]), 100)
            self.assertEqual(int(controller.replacements[77]), 100)
            self.assertNotIn(77, controller.freed)

            repeated = module._apply_shader_candidate_shared_resource(first_params)
            self.assertEqual(repeated["replacementId"], first["replacementId"])
            self.assertTrue(repeated["idempotent"])
            self.assertEqual(repeated["operationId"], "1" * 32)
            self.assertFalse(repeated["operationCreatedReplacement"])
            self.assertEqual(
                len([call for call in controller.calls
                     if call[0] == "BuildTargetShader"]), 1
            )

            renewed_params = params(revision=2)
            renewed_params.update(abi_request_id="2" * 64, abi_report_sha256="3" * 64)
            metadata = module._apply_shader_candidate_shared_resource(renewed_params)
            self.assertEqual(metadata["replacementId"], first["replacementId"])
            self.assertTrue(metadata["metadataOnly"])
            self.assertEqual(metadata["abiLeaseRequestId"], "2" * 64)
            self.assertEqual(metadata["abiLeaseReportSha256"], "3" * 64)
            self.assertEqual(metadata["abiLeaseRevision"], 2)

            leased_params = params(revision=3)
            leased_params.update({
                "abi_validation_mode": "inline-physical-with-initial-lease",
                "abi_report_sha256": "3" * 64,
                "abi_lease_request_id": "2" * 64,
                "abi_lease_candidate_sha256": hashlib.sha256(
                    candidate
                ).hexdigest(),
                "abi_lease_revision": 2,
                "expected_physical_abi_sha256": first[
                    "physicalAbiSha256"
                ],
            })
            leased = module._apply_shader_candidate_shared_resource(
                leased_params
            )
            self.assertTrue(leased["metadataOnly"])
            self.assertEqual(
                leased["abiValidationMode"],
                "inline-physical-with-initial-lease",
            )
            self.assertTrue(leased["preliminaryReflectionSkipped"])
            self.assertTrue(leased["freshPhysicalAbiValidationPerformed"])
            self.assertEqual(leased["abiLeaseRevision"], 2)

            forged_lease = dict(leased_params)
            forged_lease["abi_lease_request_id"] = "0" * 64
            with self.assertRaisesRegex(RuntimeError, "lease does not match"):
                module._apply_shader_candidate_shared_resource(forged_lease)

            with self.assertRaisesRegex(RuntimeError, "different workspace"):
                module._apply_shader_candidate_shared_resource(
                    params(workspace="f" * 32)
                )

            replacement_blob = b"second edited shared DXBC"
            replacement_path = Path(temporary) / "replacement.dxbc"
            replacement_path.write_bytes(replacement_blob)
            # A different executable may use the renewed physical-interface
            # lease; retain the original target until checked replace and UI
            # registration succeed, without requiring executable hash equality.
            replacement_params = dict(
                leased_params, revision=4, candidate_path=str(replacement_path),
                expected_sha256=hashlib.sha256(replacement_blob).hexdigest())

            # A GUI registration failure restores both the replay mapping and
            # the old UI reverse mapping, while freeing only the new target.
            context.fail_next_register = True
            with self.assertRaisesRegex(RuntimeError, "register failed"):
                module._apply_shader_candidate_shared_resource(replacement_params)
            self.assertEqual(int(context.replacements[77]), 100)
            self.assertEqual(int(controller.replacements[77]), 100)
            self.assertIn(101, controller.freed)
            self.assertIn(first["replacementId"], {
                value["replacementId"]
                for value in module._shared_shader_replacements.values()
            })

            replaced = module._apply_shader_candidate_shared_resource(
                replacement_params
            )
            self.assertNotEqual(replaced["replacementId"], first["replacementId"])
            self.assertEqual(replaced["abiLeaseRevision"], 2)
            self.assertEqual(replaced["abiLeaseRequestId"], "2" * 64)
            self.assertEqual(int(context.replacements[77]), 102)
            self.assertEqual(int(controller.replacements[77]), 102)
            self.assertIn(100, controller.freed)
            unregister_index = max(
                index for index, call in enumerate(context.calls)
                if call == ("UnregisterReplacement", 77)
            )
            register_index = max(
                index for index, call in enumerate(context.calls)
                if call == ("RegisterReplacement", 77, 102)
            )
            self.assertLess(unregister_index, register_index)

            listed = module._get_shared_shader_replacements({
                "expected_capture_id": capture_id,
                "expected_capture_session_id": "7" * 32,
            })
            self.assertEqual(
                listed["schema"],
                "renderdoc-ultra-shared-shader-replacement-list/v1",
            )
            self.assertEqual(listed["count"], 1)
            self.assertFalse(listed["replayMutation"])
            public_replacement = listed["replacements"][0]
            self.assertEqual(
                public_replacement["replacementId"], replaced["replacementId"]
            )
            self.assertEqual(public_replacement["originalResourceId"], 77)
            self.assertNotIn("candidatePath", public_replacement)
            self.assertNotIn("targetResource", public_replacement)
            self.assertIs(
                module.METHODS["get_shared_shader_replacements"],
                module._get_shared_shader_replacements,
            )

            context.replacements[77] = ResourceId(900)
            with self.assertRaisesRegex(RuntimeError, "no longer owned"):
                module._apply_shader_candidate_shared_resource(
                    replacement_params
                )
            context.replacements[77] = ResourceId(102)

            old_limit = module.MAX_SHARED_SHADER_REPLACEMENTS
            module.MAX_SHARED_SHADER_REPLACEMENTS = 1
            controller.original_id = ResourceId(78)
            try:
                with self.assertRaisesRegex(RuntimeError, "replacement limit"):
                    module._apply_shader_candidate_shared_resource(
                        params(workspace="e" * 32, resource_id=78)
                    )
            finally:
                controller.original_id = ResourceId(77)
                module.MAX_SHARED_SHADER_REPLACEMENTS = old_limit

            exact_params = {
                "expected_capture_id": capture_id,
                "expected_capture_session_id": "7" * 32,
                "workspace_id": "b" * 32,
                "expected_event_id": 5,
                "stage": replaced["stage"],
                "entry_point": replaced["entryPoint"],
                "expected_original_resource_id": replaced[
                    "originalResourceId"
                ],
                "expected_original_sha256": replaced[
                    "originalRawSha256"
                ],
                "expected_replacement_id": replaced["replacementId"],
                "expected_operation_id": replaced["operationId"],
                "expected_revision": replaced["revision"],
                "expected_anchor_event_id": replaced["anchorEventId"],
                "expected_api": replaced["api"],
                "expected_encoding": replaced["encoding"],
                "expected_candidate_sha256": replaced[
                    "candidateRawSha256"
                ],
                "expected_physical_abi_sha256": replaced[
                    "physicalAbiSha256"
                ],
                "expected_shader_family_id": replaced["shaderFamilyId"],
                "expected_target_resource_id": replaced["targetResourceId"],
            }
            context.replacements[77] = ResourceId(900)
            with self.assertRaisesRegex(RuntimeError, "no longer owned"):
                module._restore_shader_candidate_shared_resource_exact(
                    exact_params
                )
            context.replacements[77] = ResourceId(102)

            exact_restore = (
                module._restore_shader_candidate_shared_resource_exact(
                    exact_params
                )
            )
            self.assertEqual(exact_restore["state"], "rolled-back")
            self.assertTrue(exact_restore["linearized"])
            self.assertTrue(exact_restore["stateAtLinearizationVerified"])
            self.assertGreaterEqual(exact_restore["mutationEpoch"], 1)
            rolled_back = exact_restore["rollback"]
            self.assertTrue(rolled_back["cleared"])
            self.assertTrue(rolled_back["targetFreed"])
            self.assertEqual(
                rolled_back["operationId"], replaced["operationId"]
            )
            self.assertNotIn(77, context.replacements)
            self.assertNotIn(77, controller.replacements)
            self.assertIn(102, controller.freed)
            self.assertNotIn(77, controller.freed)

            absent_params = {
                "expected_capture_id": capture_id,
                "expected_capture_session_id": "7" * 32,
                "workspace_id": "b" * 32,
                "expected_event_id": 5,
                "stage": "fragment",
                "entry_point": "main",
                "expected_original_resource_id": 77,
                "expected_original_sha256": hashlib.sha256(
                    baseline
                ).hexdigest(),
            }
            absent = module._restore_shader_candidate_shared_resource_exact(
                absent_params
            )
            self.assertEqual(absent["state"], "absent")
            self.assertIsNone(absent["rollback"])
            self.assertTrue(absent["alreadyRestored"])

            module._event_shader_overrides["a" * 32] = {
                "eventId": 5,
                "stageName": "fragment",
            }
            with self.assertRaisesRegex(RuntimeError, "event-scoped"):
                module._restore_shader_candidate_shared_resource_exact(
                    absent_params
                )
            module._event_shader_overrides.clear()
            self.assertIs(
                module.METHODS[
                    "restore_shader_candidate_shared_resource_exact"
                ],
                module._restore_shader_candidate_shared_resource_exact,
            )

            # A stock Editor replacement is foreign state. The bridge refuses
            # to overwrite it and does not build or remove anything.
            context.replacements[77] = ResourceId(900)
            with self.assertRaisesRegex(RuntimeError, "foreign RenderDoc replacement"):
                module._restore_shader_candidate_shared_resource_exact(
                    absent_params
                )
            build_count = len([
                call for call in controller.calls
                if call[0] == "BuildTargetShader"
            ])
            with self.assertRaisesRegex(RuntimeError, "foreign RenderDoc replacement"):
                module._apply_shader_candidate_shared_resource(params())
            self.assertEqual(int(context.replacements[77]), 900)
            self.assertEqual(
                len([call for call in controller.calls
                     if call[0] == "BuildTargetShader"]), build_count
            )

            # D3D12 uses the checked replay transaction. If the initial GUI
            # registration fails and restoring the previous checked target also
            # fails, the bridge must expose that result, retain (not free) the
            # still-live new target, and make replay/UI/bookkeeping agree so the
            # returned recovery ID can be rolled back later.
            context.replacements.clear()
            controller.replacements.clear()
            module._forget_shared_shader_replacements()
            controller.api = "GraphicsAPI.D3D12"
            d3d12_first = module._apply_shader_candidate_shared_resource(
                params(revision=10)
            )
            old_d3d12_target = d3d12_first["targetResourceId"]

            # A different candidate from the same ReplayLab workspace must
            # supersede the active target without asking the caller to roll the
            # last-good revision back first.  If the checked D3D12 PSO rebuild
            # fails, the old replay/UI owner and rollback token remain intact.
            checked_failure_blob = b"D3D12 rejected supersede candidate"
            checked_failure_path = Path(temporary) / "d3d12-rejected.dxbc"
            checked_failure_path.write_bytes(checked_failure_blob)
            checked_failure_params = params(
                revision=11,
                blob=checked_failure_blob,
                path=checked_failure_path,
            )
            checked_failure_params["abi_report_sha256"] = "0" * 64
            rejected_target = controller.next_target
            controller.fail_checked_target = rejected_target
            with self.assertRaisesRegex(
                    RuntimeError, "Checked D3D12 Shader replacement failed"):
                module._apply_shader_candidate_shared_resource(
                    checked_failure_params)
            self.assertEqual(int(context.replacements[77]), old_d3d12_target)
            self.assertEqual(int(controller.replacements[77]), old_d3d12_target)
            self.assertIn(rejected_target, controller.freed)
            self.assertEqual(
                next(iter(module._shared_shader_replacements.values()))[
                    "replacementId"
                ],
                d3d12_first["replacementId"],
            )
            controller.fail_checked_target = None

            # A workspace is bound to one captured original Shader identity;
            # it cannot reuse its ownership token to start a second original.
            controller.original_id = ResourceId(78)
            with self.assertRaisesRegex(
                    RuntimeError, "different captured Shader ResourceId"):
                module._apply_shader_candidate_shared_resource(
                    params(revision=12, resource_id=78)
                )
            controller.original_id = ResourceId(77)

            d3d12_blob = b"D3D12 checked recovery candidate"
            d3d12_path = Path(temporary) / "d3d12-recovery.dxbc"
            d3d12_path.write_bytes(d3d12_blob)
            d3d12_params = params(
                revision=11, blob=d3d12_blob, path=d3d12_path
            )
            d3d12_params["abi_report_sha256"] = "1" * 64
            context.fail_next_register = True
            controller.fail_checked_target = old_d3d12_target
            with self.assertRaisesRegex(
                    RuntimeError,
                    "checked replay rollback failed.*remains active and tracked"):
                module._apply_shader_candidate_shared_resource(d3d12_params)

            recovery = next(iter(module._shared_shader_replacements.values()))
            recovery_target = recovery["targetResourceId"]
            self.assertTrue(recovery["uiSynchronized"])
            self.assertEqual(int(context.replacements[77]), recovery_target)
            self.assertEqual(
                int(controller.replacements[77]), recovery_target)
            self.assertNotIn(recovery_target, controller.freed)
            self.assertIn(old_d3d12_target, controller.freed)

            controller.fail_checked_target = None
            recovered_rollback = (
                module._rollback_shader_candidate_shared_resource({
                    "replacement_id": recovery["replacementId"],
                    "expected_capture_id": capture_id,
                    "workspace_id": "b" * 32,
                })
            )
            self.assertTrue(recovered_rollback["cleared"])
            self.assertIn(recovery_target, controller.freed)
            self.assertNotIn(77, controller.replacements)
            self.assertNotIn(77, context.replacements)

            # Catalog projections are not separate physical resource owners.
            # A quiesced foreign workspace can transfer only its exact token;
            # failed candidate/UI mutations preserve the existing last-good pair.
            handoff_first_params = dict(params(revision=20), operation_id="8" * 32)
            handoff_first = module._apply_shader_candidate_shared_resource(handoff_first_params)
            old_target = handoff_first["targetResourceId"]
            other_catalog = "e" * 64
            module._known_catalog_capture_ids[other_catalog] = os.path.normcase(
                os.path.abspath(capture_path))
            owner = {key: handoff_first[key] for key in (
                "replacementId", "workspaceId", "captureId", "captureSessionId",
                "candidateRawSha256", "revision")}
            transfer = dict(params(workspace="f" * 32),
                            expected_capture_id=other_catalog, expected_owner=owner,
                            operation_id="9" * 32)
            for key, wrong in (("revision", 19), ("candidateRawSha256", "0" * 64),
                               ("captureSessionId", "0" * 32), ("replacementId", "0" * 32)):
                with self.subTest(handoff_field=key), self.assertRaisesRegex(
                        RuntimeError, "handoff owner changed"):
                    module._apply_shader_candidate_shared_resource(dict(
                        transfer, expected_owner=dict(owner, **{key: wrong})))
            controller.fail_checked_target = controller.next_target
            with self.assertRaisesRegex(RuntimeError, "Checked D3D12 Shader replacement failed"):
                module._apply_shader_candidate_shared_resource(transfer)
            controller.fail_checked_target = None
            self.assertEqual(int(controller.replacements[77]), old_target)
            self.assertEqual(int(context.replacements[77]), old_target)
            self.assertNotIn(old_target, controller.freed)
            context.fail_next_register = True
            with self.assertRaisesRegex(RuntimeError, "register failed"):
                module._apply_shader_candidate_shared_resource(transfer)
            self.assertEqual(int(controller.replacements[77]), old_target)
            self.assertEqual(module._shared_shader_replacements[77]["workspaceId"], "b" * 32)
            self.assertNotIn(old_target, controller.freed)
            handed_off = module._apply_shader_candidate_shared_resource(transfer)
            self.assertEqual(handed_off["workspaceId"], "f" * 32)
            self.assertEqual(handed_off["captureId"], other_catalog)
            self.assertNotEqual(handed_off["replacementId"], handoff_first["replacementId"])
            self.assertFalse(handed_off["metadataOnly"])
            self.assertIn(old_target, controller.freed)
            with self.assertRaisesRegex(RuntimeError, "without its exact active replacement"):
                module._apply_shader_candidate_shared_resource(handoff_first_params)
            self.assertTrue(module._apply_shader_candidate_shared_resource(transfer)["idempotent"])
            module._rollback_shader_candidate_shared_resource({
                "replacement_id": handed_off["replacementId"],
                "expected_capture_id": other_catalog, "workspace_id": "f" * 32})

            # Vulkan and OpenGL native SPIR-V use the same checked, derived-
            # pipeline transaction and then re-read the live anchor.  This is
            # deliberately separate from Ray/Library replacement, which the
            # bridge rejects before reaching this path.
            for api, encoding, suffix in (
                ("GraphicsAPI.Vulkan", "spirv", "spv"),
                ("GraphicsAPI.OpenGL", "opengl-spirv", "gl.spv"),
            ):
                with self.subTest(api=api):
                    context.replacements.clear()
                    controller.replacements.clear()
                    module._forget_shared_shader_replacements()
                    controller.api = api
                    native_blob = ("checked %s candidate" % api).encode("ascii")
                    native_path = Path(temporary) / ("candidate." + suffix)
                    native_path.write_bytes(native_blob)
                    native_params = params(
                        workspace=("c" if api.endswith("Vulkan") else "d") * 32,
                        revision=20,
                        blob=native_blob,
                        path=native_path,
                    )
                    native_params["encoding"] = encoding
                    native_params["abi_report_sha256"] = (
                        "2" if api.endswith("Vulkan") else "3"
                    ) * 64

                    native = module._apply_shader_candidate_shared_resource(
                        native_params
                    )
                    self.assertTrue(native["checkedDerivedPipelineRebuild"])
                    self.assertTrue(native["postApplyAnchorVerified"])
                    self.assertEqual(native["api"], api.split(".")[-1].lower())
                    self.assertEqual(native["encoding"], encoding)
                    self.assertEqual(controller.GetShader(ResourceId(5), ResourceId(77), None).rawBytes,
                                     baseline)
                    self.assertEqual(controller.GetShader(ResourceId(), ResourceId(native["targetResourceId"]),
                                                          None).rawBytes, native_blob)
                    self.assertTrue(module._apply_shader_candidate_shared_resource(native_params)["idempotent"])
                    # Captured reflection must not excuse a changed UI owner.
                    target = context.replacements[77]
                    context.replacements[77] = ResourceId(999)
                    with self.assertRaisesRegex(RuntimeError, "no longer owned"):
                        module._apply_shader_candidate_shared_resource(native_params)
                    context.replacements[77] = target
                    self.assertIn(
                        (
                            "ReplaceTargetShaderChecked",
                            77,
                            native["targetResourceId"],
                        ),
                        controller.calls,
                    )

                    native_rollback = (
                        module._rollback_shader_candidate_shared_resource({
                            "replacement_id": native["replacementId"],
                            "expected_capture_id": capture_id,
                            "workspace_id": native_params["workspace_id"],
                        })
                    )
                    self.assertTrue(native_rollback["cleared"])
                    # Post-apply anchor failure followed by checked rollback
                    # failure leaves the new target bound. Retain and track it
                    # without a success proof, including during orphan cleanup.
                    validate_live = module._validate_live_shader_event

                    def fail_post_apply(*args, **kwargs):
                        if kwargs.get("owned_shared_replacement") is not None:
                            raise RuntimeError("injected post-apply anchor failure")
                        return validate_live(*args, **kwargs)

                    controller.fail_checked_remove = True
                    with mock.patch.object(module, "_validate_live_shader_event", side_effect=fail_post_apply), \
                            self.assertRaisesRegex(RuntimeError, "checked replay rollback failed.*active and tracked"):
                        module._apply_shader_candidate_shared_resource(native_params)
                    retained = module._shared_shader_replacements[77]
                    retained_target = retained["targetResourceId"]
                    self.assertTrue(retained["postApplyValidationFailed"])
                    self.assertNotIn("restoreValidationReceipt", retained)
                    self.assertEqual(int(controller.replacements[77]), retained_target)
                    self.assertEqual(int(context.replacements[77]), retained_target)
                    self.assertNotIn(retained_target, controller.freed)
                    self.assertNotIn(retained["targetResource"], module._shared_shader_orphan_targets)
                    module._release_replay_transient_caches({"clear_replay_cache": False})
                    self.assertNotIn(retained_target, controller.freed)
                    with self.assertRaisesRegex(RuntimeError, "requires recovery"):
                        module._apply_shader_candidate_shared_resource(native_params)
                    self.assertNotIn("restoreValidationReceipt", retained)
                    controller.fail_checked_remove = False
                    recovered = module._rollback_shader_candidate_shared_resource({
                        "replacement_id": retained["replacementId"],
                        "expected_capture_id": capture_id,
                        "workspace_id": native_params["workspace_id"],
                    })
                    self.assertTrue(recovered["cleared"])
                    self.assertIn(retained_target, controller.freed)
                    self.assertIn(
                        ("RemoveTargetShaderReplacementChecked", 77),
                        controller.calls,
                    )

    def test_shader_workspace_tools_keep_commands_out_of_mcp_contract(self) -> None:
        server_path = REPOSITORY / "mcp_host" / "src" / "server.py"
        source = server_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        expected = {
            "get_shader_toolchain_capabilities",
            "plan_shader_toolchain_route",
            "plan_shader_recovery",
            "run_shader_toolchain_round_trip_smoke",
            "prepare_shader_workspace",
            "try_shader_workspace_recovery_candidate",
            "get_current_shader_representation",
            "create_shader_workspace",
            "decompile_shader_workspace",
            "get_shader_workspace_representation",
            "promote_shader_workspace_explanation",
            "compile_shader_workspace",
            "migrate_shader_workspace_toolchain",
            "begin_shader_workspace_organization",
            "finish_shader_workspace_organization",
            "get_shader_workspace_status",
            "start_shader_workspace_watch",
            "poll_shader_workspace_watch",
            "stop_shader_workspace_watch",
            "validate_shader_workspace_candidate",
            "apply_shader_workspace_candidate",
            "list_shader_workspace_family_uses",
            "apply_shader_workspace_candidate_family",
            "apply_shader_workspace_shared_resource",
            "register_active_project_shader_replacement",
            "register_active_project_material_session",
            "rollback_shader_workspace_override",
            "rollback_shader_workspace_override_family",
            "rollback_shader_workspace_shared_resource",
        }
        self.assertTrue(expected.issubset(functions), expected - set(functions))
        forbidden = {
            "config_path",
            "tool_config_path",
            "executable",
            "executable_path",
            "args",
            "arguments",
            "command",
            "env",
            "environment",
        }
        for name in expected:
            arguments = {argument.arg for argument in functions[name].args.args}
            self.assertFalse(arguments.intersection(forbidden), (name, arguments))

        prepare_arguments = {
            argument.arg
            for argument in functions["prepare_shader_workspace"].args.args
        }
        self.assertNotIn("decompiler_tool_id", prepare_arguments)
        self.assertNotIn("compiler_tool_id", prepare_arguments)
        prepare_doc = ast.get_docstring(functions["prepare_shader_workspace"])
        self.assertIsNotNone(prepare_doc)
        self.assertNotIn("specialist", prepare_doc.casefold())

        prepare_source = ast.get_source_segment(
            source, functions["prepare_shader_workspace"]
        )
        self.assertIn('"export_shader_catalog"', prepare_source)
        self.assertNotIn("ReplaceResource", prepare_source)
        self.assertNotIn("replace_shader", prepare_source)
        self.assertIn("RENDERDOC_ULTRA_SHADER_TOOLS_CONFIG", source)
        self.assertIn("RENDERDOC_ULTRA_SHADER_TOOLCHAINS_CONFIG", source)
        start_arguments = {
            argument.arg
            for argument in functions["start_shader_workspace_watch"].args.args
        }
        self.assertEqual(start_arguments, {"workspace_path", "debounce_ms"})
        for name in {
            "poll_shader_workspace_watch",
            "stop_shader_workspace_watch",
        }:
            arguments = {argument.arg for argument in functions[name].args.args}
            self.assertEqual(arguments, {"watch_id"})

    def test_active_mcp_contracts_use_child_agent_vocabulary(self) -> None:
        expected_literals = {
            REPOSITORY / "mcp_host" / "src" / "resource_export.py": (
                "child-Agent-produced semantic plans",
                "specialist-produced semantic plans",
            ),
            REPOSITORY / "mcp_host" / "src" / "unpacked_asset_jobs.py": (
                '"decisionOwner": "child-agent-with-project-evidence-or-user"',
                "specialist-with-project-evidence-or-user",
            ),
        }
        for path, (current, retired) in expected_literals.items():
            source = path.read_text(encoding="utf-8")
            self.assertIn(current, source)
            self.assertNotIn(retired, source)

        server_path = REPOSITORY / "mcp_host" / "src" / "server.py"
        source = server_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for name in {
            "_current_workspace_publication_binding",
            "query_active_shader_asset_evidence",
            "register_replay_project_workspace",
        }:
            doc = ast.get_docstring(functions[name])
            self.assertIsNotNone(doc)
            self.assertNotIn("specialist", doc.casefold())

    def test_launch_renderdoc_does_not_accept_an_executable_override(self) -> None:
        server_path = REPOSITORY / "mcp_host" / "src" / "server.py"
        tree = ast.parse(server_path.read_text(encoding="utf-8"))
        launch = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "launch_renderdoc"
        )
        arguments = {argument.arg for argument in launch.args.args}
        self.assertEqual(arguments, {"capture_path"})
        self.assertNotIn("renderdoc_path", arguments)


if __name__ == "__main__":
    unittest.main()
