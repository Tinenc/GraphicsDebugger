from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import json
import queue
import sys
import threading
import time
import unittest
import types
from pathlib import Path
from unittest import mock

import pytest


REPLAYLAB_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = REPLAYLAB_ROOT.parent
INTEGRATION_ROOT = REPLAYLAB_ROOT / "renderdoc_mcp_bridge"
MODULE_PATH = INTEGRATION_ROOT / "project_shader_workspace.py"
BRIDGE_PATH = INTEGRATION_ROOT / "__init__.py"
I18N_EXTRACTOR_PATH = REPOSITORY_ROOT / "util" / "i18n" / (
    "extract_replaylab_translations.py"
)

SHADER_CONSOLE_APIS = {
    "console_read_batch",
    "known_source_locator",
    "load_related_shader_source_document",
    "cancellable_console_reads",
    "load_console_snapshot",
    "load_authorized_source_document",
    "reload_saved_shader_source_preview",
    "normalize_capture_binding",
}


def test_native_edit_matches_capture_stage_rid_entry_and_shared_bindings(tmp_path):
    module = _load_without_qt()
    capture = str(tmp_path / "capture.rdc")
    binding = {"capturePath": capture}
    request = {"capturePath": capture, "resourceId": "723", "stage": "fragment", "entryPoint": "MainPS"}
    physical = dict(request, resourceId=723, stage="PS")
    workspaces = {
        "other-entry": dict(physical, entryPoint="Other"),
        "other-stage": dict(physical, stage="VS"),
        "other-capture": dict(physical, capturePath=str(tmp_path / "other.rdc")),
        "shared": {"capturePath": capture, "resourceId": 900, "relatedWorkspaceBindings": [physical]},
        "exact": physical,
    }
    assert module.shader_target_workspace_ids(workspaces, request, binding) == ["shared", "exact"]
    assert module.shader_target_workspace_ids(workspaces, request, {"capturePath": str(tmp_path / "new.rdc")}) == []


def test_later_cached_selection_demotes_an_earlier_pending_open(tmp_path):
    panel, unused, panel_type, workspace, unused_source, unused_opened = _canonical_display_fixture(tmp_path)
    panel._pending_open = {'slow': {'select': True, 'quiet': False}}
    assert panel_type._open_workspace(panel, workspace['workspaceId'], quiet=True)
    assert panel._pending_open['slow']['select'] is False


def test_known_source_refresh_does_not_rebuild_catalog_or_search_project(tmp_path):
    from test_shader_console import ShaderConsoleFixture,console
    fixture=ShaderConsoleFixture(tmp_path);fixture.write_live();fixture.write_watch()
    model=console.load_console_snapshot(str(fixture.registry),fixture.capture_binding)['workspaces'][0]
    module=_load_without_qt();paths=[str(fixture.registry),str(fixture.source)]
    request=dict(stop=threading.Event(),registryPath=str(fixture.registry),captureBinding=fixture.capture_binding,
        watchedPaths=paths,previousSignature=module._paths_signature(paths),force=True,documentIds=[fixture.WORKSPACE_ID],
        knownSources=[model])
    with mock.patch.object(module,'_shader_console_protocol',console), \
            mock.patch.object(console,'load_console_snapshot',side_effect=AssertionError('No catalog reload')):
        result=module._build_workspace_snapshot(request)
    assert result['sourceOnly'] and not result['sourceErrors']
    assert result['sources'][fixture.WORKSPACE_ID]['text']==fixture.source.read_bytes().decode('utf-8')


def _parse(path):
    return ast.parse(
        path.read_text(encoding="utf-8"),
        filename=str(path),
        feature_version=(3, 6),
    )


def _function(tree, name):
    matches = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    if len(matches) != 1:
        raise AssertionError("expected exactly one function named %s" % name)
    return matches[0]


def _method(tree, name):
    matches = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    if len(matches) != 1:
        raise AssertionError("expected exactly one method named %s" % name)
    return matches[0]


def test_failed_source_discovery_is_unavailable_rather_than_missing():
    tree = ast.Module(body=[copy.deepcopy(_method(_parse(MODULE_PATH), 'navigate_shader_target'))], type_ignores=[])
    namespace = {'_normal_path': lambda value: value.casefold(),
                 'shader_target_workspace_ids': lambda *args: []}
    exec(compile(ast.fix_missing_locations(tree), str(MODULE_PATH), 'exec'), namespace)
    binding = {'capturePath':'capture.rdc','captureSessionId':'a'*32}
    panel = types.SimpleNamespace(_alive=True,_shutting_down=False,_capture_binding=binding,
        _workspaces={},_snapshot_running=False,_snapshot={'loaded':True},_target_refresh_error=False,
        _source_discovery_error='shared registry unreadable',_current_capture_binding=lambda: binding,
        _current_workspace_id=lambda: '')
    result = namespace['navigate_shader_target'](panel, dict(binding))
    assert result['status']=='unavailable' and result['diagnostic']=='shared registry unreadable'
    panel._source_discovery_error=''
    assert namespace['navigate_shader_target'](panel, dict(binding))['status']=='missing'


def _attribute_calls(node):
    return {
        item.func.attr
        for item in ast.walk(node)
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
    }


def _name_calls(node):
    return {
        item.func.id
        for item in ast.walk(node)
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Name)
    }


def _load_without_qt():
    spec = importlib.util.spec_from_file_location(
        "replaylab_project_shader_workspace_no_qt", MODULE_PATH
    )
    if spec is None or spec.loader is None:
        raise AssertionError("could not create the shader-workspace module spec")
    module = importlib.util.module_from_spec(spec)
    without_qt = {
        "PySide2": None,
        "PySide2.QtCore": None,
        "PySide2.QtGui": None,
        "PySide2.QtWidgets": None,
    }
    with mock.patch.dict(sys.modules, without_qt):
        spec.loader.exec_module(module)
    return module


def _load_i18n_extractor():
    spec = importlib.util.spec_from_file_location(
        "shader_workspace_i18n_extractor", I18N_EXTRACTOR_PATH
    )
    if spec is None or spec.loader is None:
        raise AssertionError("could not create the ReplayLab i18n extractor spec")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ShaderWorkspaceModuleContractTests(unittest.TestCase):
    def test_current_view_updates_live_save_hint_when_editor_is_deferred(self):
        module = _load_without_qt()
        placeholder = mock.Mock()
        placeholder.flags.return_value = 1
        namespace = {
            "QtCore": types.SimpleNamespace(
                QCoreApplication=types.SimpleNamespace(translate=lambda _context, text: text),
                Qt=types.SimpleNamespace(ItemIsSelectable=1)),
            "QtWidgets": types.SimpleNamespace(QTreeWidgetItem=lambda _text: placeholder),
            "_latest_event": module._latest_event,
        }
        tree = _parse(MODULE_PATH)
        methods = ast.Module(body=[copy.deepcopy(_method(tree, name)) for name in
            ("_refresh_current_view", "_status_text")], type_ignores=[])
        exec(compile(ast.fix_missing_locations(methods), str(MODULE_PATH), "exec"), namespace)
        document = {"workspaceId": "current", "documentKind": "live-edit",
                    "available": True, "dirty": False, "editor": mock.Mock()}
        workspace = {"autoApplyReady": True, "liveEdit": {"activeRevision": 21}}
        panel = mock.Mock()
        panel._documents = {"current": document}
        panel._current_document.return_value = document
        panel._document_model.return_value = workspace
        panel._current_problems.return_value = []
        panel._status_text = lambda value: namespace["_status_text"](panel, value)
        refresh = lambda: namespace["_refresh_current_view"](panel)

        refresh()
        live_hint = panel.subtitle.setText.call_args.args[0]
        live_status = panel.status_label.setText.call_args.args[0]
        workspace["autoApplyReady"] = False
        refresh()
        readonly_hint = panel.subtitle.setText.call_args.args[0]
        self.assertEqual(readonly_hint, live_hint)
        self.assertIn("Ctrl+S saves, compiles and replaces", readonly_hint)
        self.assertNotEqual(readonly_hint, live_status)
        self.assertIn("checks are still pending", panel.status_label.setText.call_args.args[0])
        panel.save_button.setEnabled.assert_called_with(True)
        panel.save_all_button.setEnabled.assert_called_with(False)

        # An explicitly read-only source still cannot advertise saving if a
        # stale model happens to retain a ready live-edit snapshot.
        document["readOnly"] = True
        workspace["autoApplyReady"] = True
        refresh()
        self.assertEqual(panel.subtitle.setText.call_args.args[0], readonly_hint)
        panel.save_button.setEnabled.assert_called_with(False)
        # Explicit preview edits use the same save-and-apply contract.
        document["documentKind"] = "organization-preview"
        refresh()
        self.assertEqual(panel.subtitle.setText.call_args.args[0], readonly_hint)

    def test_compiler_locations_are_bound_to_the_saved_source_revision(self):
        method = _method(_parse(MODULE_PATH), "_workspace_problems")
        namespace = {"_latest_event": _load_without_qt()._latest_event}
        tree = ast.Module(body=[method], type_ignores=[])
        exec(compile(ast.fix_missing_locations(tree), str(MODULE_PATH), "exec"), namespace)
        problems = namespace["_workspace_problems"]
        workspace = {
            "watch": {"events": [{"type": "compileFailed", "sequence": 4,
                                   "requestedSourceSha256": "old", "message": "Old compile failed"}]},
            "entries": [{"cursorSequence": 4, "severity": "error", "line": 11, "text": "Bad old line"}],
        }
        self.assertEqual(problems(None, workspace, "new"), [])
        self.assertEqual(problems(None, workspace, "old")[0]["line"], 11)
        workspace["watch"]["events"].append({"type": "compileSucceeded", "sequence": 5})
        self.assertEqual(problems(None, workspace, "old"), [])

    def test_module_remains_python_36_parseable(self):
        _parse(MODULE_PATH)

    def test_module_imports_without_qt_and_exposes_defensive_panel(self):
        module = _load_without_qt()
        self.assertFalse(module.HAS_QT)
        self.assertTrue(hasattr(module, "ReplayLabShaderWorkspacePanel"))
        with self.assertRaisesRegex(Exception, "PySide2|Qt"):
            module.ReplayLabShaderWorkspacePanel(None, None)

    def test_module_has_no_process_execution_surface(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for forbidden in ("subprocess", "os.system", "QProcess"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_preview_bridge_binds_capture_session_and_never_applies(self):
        tree = _parse(BRIDGE_PATH)
        handler = _function(tree, "_show_shader_organization_preview")
        calls = _name_calls(handler)
        self.assertIn("_require_capture_identity", calls)
        self.assertIn("_shader_override_hex", calls)
        self.assertIn("_shader_workspace_capture_binding", calls)
        self.assertIn("_invoke_on_ui_sync", calls)
        source = ast.get_source_segment(
            BRIDGE_PATH.read_text(encoding="utf-8"), handler)
        self.assertIsNotNone(source)
        self.assertIn("load_verified_organization_preview", source)
        self.assertIn('"liveEditStarted": False', source)
        self.assertIn('"replayApplied": False', source)

        # canonicalSourceUpdated describes the verified source receipt: saved
        # canonical source and a separate compile preview have different states.
        # Displaying either starts no replay/live edit. An already writable
        # canonical source is selected without adding a read-only duplicate.
        session = "1" * 32
        capture_id = "2" * 64
        organization_id = "3" * 32
        source_sha = "4" * 64
        binding = {"instanceId": "offline-preview", "captureSessionId": session,
                   "capturePath": "D:/capture.rdc"}
        params = {"expected_capture_id": capture_id,
                  "expected_capture_session_id": session,
                  "organization_id": organization_id,
                  "expected_source_sha256": source_sha,
                  "workspace_path": "D:/workspace"}
        for canonical_saved in (False, True):
            with self.subTest(canonical_saved=canonical_saved):
                opened = {"captureId": capture_id, "captureBinding": binding,
                          "organizationId": organization_id, "rawSha256": source_sha,
                          "workspaceId": "5" * 64, "sourceWorkspaceId": "6" * 32,
                          "canonicalSourceUpdated": canonical_saved}
                loader = mock.Mock(return_value=opened)
                panel = mock.Mock()
                panel._capture_binding = binding
                reused = canonical_saved
                panel.show_organization_preview.return_value = {
                    "shown": True, "workspaceId": opened["sourceWorkspaceId"] if reused else opened["workspaceId"],
                    "documentKind": "live-edit" if reused else "organization-preview",
                    "readOnly": not reused, "canonicalWorkspaceReused": reused}
                namespace = {
                    "_require_capture_identity": lambda value: value,
                    "_shader_override_hex": lambda value, *_args: value,
                    "_capture_session_id": session,
                    # This function-only harness has no JsonPoller admission;
                    # retain the production fresh-Qt binding fallback.
                    "_request_admitted_capture_identity": lambda: None,
                    "_shader_workspace_capture_binding": lambda: binding,
                    "_shader_console_protocol": types.SimpleNamespace(
                        load_verified_organization_preview=loader),
                    "_invoke_on_ui_sync": lambda callback: callback(),
                    "_shader_workspace_ui": panel,
                    "_shader_workspace_callbacks": [],
                }
                executable = ast.Module(body=[handler], type_ignores=[])
                exec(compile(ast.fix_missing_locations(executable), str(BRIDGE_PATH), "exec"),
                     namespace)
                with mock.patch("builtins.open", side_effect=AssertionError(
                        "preview display must not rewrite source")):
                    receipt = namespace[handler.name](params)
                loader.assert_called_once_with(
                    params["workspace_path"], organization_id, source_sha,
                    capture_binding=binding)
                panel.show_organization_preview.assert_called_once_with(opened)
                panel.show.assert_called_once_with()
                self.assertEqual(receipt["canonicalSourceUpdated"], canonical_saved)
                self.assertTrue(receipt["shown"])
                self.assertEqual(receipt["readOnly"], not reused)
                self.assertEqual(receipt["canonicalWorkspaceReused"], reused)
                self.assertEqual(receipt["workspaceId"], opened["sourceWorkspaceId"] if reused else opened["workspaceId"])
                self.assertEqual(receipt["previewWorkspaceId"], opened["workspaceId"])
                self.assertEqual(receipt["sourceWorkspaceId"], opened["sourceWorkspaceId"])
                self.assertFalse(receipt["liveEditStarted"])
                self.assertFalse(receipt["replayApplied"])

    def test_dock_close_and_first_tab_have_explicit_lifecycle_hooks(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("@QtCore.Slot(result=bool)\n        def checkAllowClose", source)
        self.assertIn("self._tab_changed(index)", source)
        refresh = _method(_parse(MODULE_PATH), "refresh")
        self.assertIn("watchedPaths", {node.value for node in ast.walk(refresh)
                                      if isinstance(node, ast.Constant) and isinstance(node.value, str)})
        self.assertIn("capture_changed", _attribute_calls(refresh))

    def test_tab_order_and_dirty_state_follow_the_native_editor(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("self._reordering_tabs = True", source)
        self.assertIn(
            "item for item in self._tab_workspace_ids() if item in pinned",
            source,
        )
        self.assertIn("editor.SetTextChangedCallback(", source)
        self.assertIn("editor.SetSavePoint()", source)
        self.assertIn('document.get("baselineEditorText", "")', source)
        self.assertIn('document["editor"].GetText() != baseline', source)

    def test_host_save_runs_off_ui_and_results_are_drained_on_ui_timer(self):
        tree = _parse(MODULE_PATH)
        worker = _function(tree, "_save_worker_loop")
        save_method = _method(tree, "_save_document")
        drain_method = _method(tree, "_drain_save_results")
        self.assertIn("save", _attribute_calls(worker))
        self.assertNotIn("save", _attribute_calls(save_method))
        self.assertIn("put", _attribute_calls(save_method))
        self.assertIn("get_nowait", _attribute_calls(drain_method))
        worker_names = {
            node.id for node in ast.walk(worker) if isinstance(node, ast.Name)
        }
        self.assertFalse({"QtCore", "QtGui", "QtWidgets"} & worker_names)

        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("threading.Thread(", source)
        self.assertIn("target=_save_worker_loop", source)
        self.assertIn('getattr(save_client, "request_pending", None)', source)
        self.assertIn("stop_event.wait(0.05)", source)
        self.assertIn(
            "self.save_result_timer.timeout.connect(self._drain_save_results)",
            source,
        )
        self.assertIn("if not self.save_result_timer.isActive():", source)
        self.assertIn("self.save_result_timer.stop()", source)
        self.assertIn('document.get("documentToken")', source)
        self.assertIn('pending.get("saveToken")', source)
        self.assertIn("self._stop_save_worker()", source)

    def test_save_worker_join_is_bounded_and_reports_pending_transport(self):
        module = _load_without_qt()
        release = threading.Event()
        worker = threading.Thread(target=release.wait)
        worker.daemon = True
        worker.start()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(
                    RuntimeError, "Host request pending: yes"):
                module._join_save_worker(worker, 0.02, lambda: True)
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            release.set()
            worker.join(1.0)
        self.assertFalse(worker.is_alive())

    def test_save_worker_join_confirms_a_finished_worker(self):
        module = _load_without_qt()
        worker = threading.Thread(target=lambda: None)
        worker.start()
        self.assertTrue(
            module._join_save_worker(worker, 0.5, lambda: False))
        self.assertFalse(worker.is_alive())

    def test_panel_shutdown_gates_ui_cleanup_on_worker_join(self):
        tree = _parse(MODULE_PATH)
        stop = _method(tree, "_stop_save_worker")
        shutdown = _method(tree, "shutdown")
        destroyed = _method(tree, "_destroyed")
        confirm_close = _method(tree, "_confirm_panel_close")
        self.assertIn("join", _attribute_calls(_function(
            tree, "_join_save_worker")))
        self.assertIn("is_alive", _attribute_calls(_function(
            tree, "_join_save_worker")))
        self.assertIn("_save_transport_pending", {
            node.attr for node in ast.walk(stop)
            if isinstance(node, ast.Attribute)
        })
        self.assertIn("stop", _attribute_calls(shutdown))
        self.assertIn("close", _attribute_calls(shutdown))
        self.assertIn("deleteLater", _attribute_calls(shutdown))
        shutdown_source = ast.get_source_segment(
            MODULE_PATH.read_text(encoding="utf-8"), shutdown)
        self.assertIsNotNone(shutdown_source)
        self.assertLess(
            shutdown_source.index("self._stop_save_worker()"),
            shutdown_source.index("widget.close()"),
        )
        self.assertIn("_stop_save_worker", _attribute_calls(confirm_close))
        self.assertIn("_shutting_down", {
            node.attr for node in ast.walk(destroyed)
            if isinstance(node, ast.Attribute)
        })

    def test_commit_unknown_hash_model_is_exact_and_preserves_editor_buffer(self):
        module = _load_without_qt()
        self.assertEqual(
            "committed", module._classify_commit_unknown("base", "sent", "sent")
        )
        self.assertEqual(
            "pending", module._classify_commit_unknown("base", "sent", "base")
        )
        self.assertEqual(
            "external", module._classify_commit_unknown("base", "sent", "other")
        )

        tree = _parse(MODULE_PATH)
        promote = _method(tree, "_promote_submitted_save")
        self.assertNotIn("setPlainText", _attribute_calls(promote))
        source = MODULE_PATH.read_text(encoding="utf-8")
        for field in (
            '"baseHash"', '"submittedHash"', '"textRevision"',
            '"submittedEditorText"',
        ):
            self.assertIn(field, source)
        self.assertIn('document["rawSha256"] = task["submittedHash"]', source)
        self.assertIn('document["baselineEditorText"] = task[', source)
        self.assertIn('document["conflict"] = True', source)
        self.assertIn("self._save_transport_pending()", source)
        self.assertIn("self._resolve_uncommitted_save(document)", source)

    def test_checkable_button_connections_select_bool_overload(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "self.files_button.clicked[bool].connect(self._toggle_file_tree)",
            source)
        self.assertIn(
            "self.flow_button.clicked[bool].connect(self._toggle_flow_graph)",
            source)

    def test_worker_classifies_uncertain_result_without_touching_qt(self):
        module = _load_without_qt()
        task = {
            "liveEditId": "a" * 32,
            "authorizationId": "b" * 32,
            "workspaceId": "c" * 32,
            "baseHash": "d" * 64,
            "sourceText": "float4 main() : SV_Target { return 0; }",
        }

        class UncertainClient:
            def save(self, *unused_arguments):
                raise module._shader_save_client_protocol.ReplayLabShaderSaveUncertain(
                    "timeout"
                )

        tasks = queue.Queue()
        results = queue.Queue()
        tasks.put(task)
        tasks.put(None)
        module._save_worker_loop(
            UncertainClient(), tasks, results, threading.Event()
        )
        outcome = results.get_nowait()
        self.assertEqual("unknown", outcome["kind"])
        self.assertIs(task, outcome["task"])

    def test_ui_reads_through_console_and_saves_only_through_host_client(self):
        tree = _parse(MODULE_PATH)

        module_aliases = set()
        direct_api_aliases = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module_name = str(node.module or "")
            if module_name.endswith("shader_console"):
                for alias in node.names:
                    if alias.name in SHADER_CONSOLE_APIS:
                        direct_api_aliases[alias.asname or alias.name] = alias.name
            for alias in node.names:
                if alias.name == "shader_console":
                    module_aliases.add(alias.asname or alias.name)

        # Direct-file import fallback modules conventionally include
        # ``shader_console`` in their private protocol variable name.
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and "shader_console" in node.id.lower():
                module_aliases.add(node.id)

        used_apis = set()
        unexpected_module_calls = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if isinstance(function, ast.Name) and function.id in direct_api_aliases:
                used_apis.add(direct_api_aliases[function.id])
            elif (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id in module_aliases
            ):
                if function.attr in SHADER_CONSOLE_APIS:
                    used_apis.add(function.attr)
                else:
                    unexpected_module_calls.add(function.attr)

        self.assertEqual(SHADER_CONSOLE_APIS, used_apis)
        self.assertEqual(set(), unexpected_module_calls)
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("save_client.save(", source)
        self.assertNotIn("save_authorized_source_document(", source)
        self.assertIn("There is no direct-disk fallback", source)

        # Editor/attachment source reads stay behind the validated console API.
        # The file worker may read bounded manifest/catalog JSON for EID labels;
        # exempt that one reader, without allowing direct source IO elsewhere.
        direct_io = {
            "open", "listdir", "scandir", "walk", "glob", "rglob",
            "read_text", "read_bytes", "write_text", "write_bytes",
            "QFile", "QSaveFile",
        }
        reader = _function(tree, "_read_bounded_json")
        without_metadata_reader = ast.Module(
            body=[node for node in tree.body if node is not reader], type_ignores=[])
        called = _name_calls(without_metadata_reader) | _attribute_calls(without_metadata_reader)
        self.assertEqual(set(), called & direct_io)
        opens = [node for node in ast.walk(reader) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "open"]
        self.assertEqual(1, len(opens))
        self.assertEqual("rb", ast.literal_eval(opens[0].args[1]))
        self.assertNotIn("write", _attribute_calls(reader))

    def test_project_library_is_not_a_workspace_panel_source_or_navigation_path(self):
        tree = _parse(MODULE_PATH)
        source = MODULE_PATH.read_text(encoding="utf-8")
        refresh = _method(tree, "refresh")
        opener = _method(tree, "_open_workspace")
        disk_refresh = _method(tree, "_refresh_document_from_disk")
        reload_current = _method(tree, "_reload_current")
        saver = _method(tree, "_save_document")

        for method in (refresh, opener, disk_refresh, reload_current):
            self.assertNotIn("load_project_shader_library_snapshot", _attribute_calls(method))
            self.assertNotIn("load_project_shader_source_document", _attribute_calls(method))
        self.assertIn("SetReadOnly", _attribute_calls(opener))
        self.assertIn("SetReadOnly", _attribute_calls(disk_refresh))
        saver_source = ast.get_source_segment(source, saver)
        self.assertIsNotNone(saver_source)
        self.assertIn('document.get("readOnly")', saver_source)
        self.assertLess(
            saver_source.index('document.get("readOnly")'),
            saver_source.index("self._save_tasks.put(task)"),
        )
        self.assertNotIn("Project Library", {node.value for node in ast.walk(tree)
                                             if isinstance(node, ast.Constant) and isinstance(node.value, str)})
        self.assertNotIn("_project_documents", {node.attr for node in ast.walk(tree)
                                                if isinstance(node, ast.Attribute)})

    def test_file_navigator_defaults_hidden_and_flow_graph_follows_tabs(self):
        tree = _parse(MODULE_PATH)
        source = MODULE_PATH.read_text(encoding="utf-8")
        constructors = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        ]
        constructor = max(constructors, key=lambda node: len(list(ast.walk(node))))
        tab_changed = _method(tree, "_tab_changed")
        graph_sync = _method(tree, "_sync_flow_graph")
        graph_navigation = _method(tree, "_flow_graph_node_activated")

        self.assertIn('state.get("filesVisible") is True', source)
        self.assertIn('state.get("flowVisible") is not False', source)
        self.assertIn(
            "self.files_button.clicked[bool].connect(self._toggle_file_tree)",
            source)
        self.assertIn(
            "self.flow_button.clicked[bool].connect(self._toggle_flow_graph)",
            source)
        self.assertIn('QtGui.QKeySequence("Ctrl+B")', source)
        constructor_source = ast.get_source_segment(source, constructor)
        self.assertIsNotNone(constructor_source)
        self.assertLess(
            constructor_source.index("header.addWidget(self.files_button)"),
            constructor_source.index("outer.addWidget(title)"),
        )
        self.assertLess(constructor_source.index("outer.addLayout(header)"),
                        constructor_source.index("outer.addWidget(title)"))
        self.assertIn("self.subtitle.setWordWrap(True)", constructor_source)
        self.assertIn("actions.addWidget(self.save_button)", constructor_source)
        self.assertIn("ShaderFlowGraphPanel", _attribute_calls(constructor))
        self.assertIn("nodeActivated", {
            node.attr for node in ast.walk(constructor)
            if isinstance(node, ast.Attribute)
        })
        self.assertIn("_sync_flow_graph", {
            node.attr for node in ast.walk(tab_changed)
            if isinstance(node, ast.Attribute)
        })
        self.assertIn("set_graph", _attribute_calls(graph_sync))
        self.assertIn("clear_graph", _attribute_calls(graph_sync))
        self.assertIn("_anchor_graph", _attribute_calls(graph_sync))
        self.assertNotIn("stale", {keyword.arg for node in ast.walk(graph_sync)
                                  if isinstance(node, ast.Call) for keyword in node.keywords})

        navigation_calls = _attribute_calls(graph_navigation)
        self.assertIn("GoToLine", navigation_calls)
        self.assertNotIn("open", navigation_calls)
        self.assertIn("_current_capture_binding", navigation_calls)

    def test_project_module_is_discovered_and_clean_under_i18n_gate(self):
        self.assertTrue(MODULE_PATH.match("project_*.py"))
        self.assertIn(MODULE_PATH, set(INTEGRATION_ROOT.rglob("project_*.py")))

        extractor = _load_i18n_extractor()
        messages, errors = extractor.extract(MODULE_PATH)
        self.assertEqual([], errors)
        self.assertTrue(messages)
        self.assertEqual(
            {"ReplayLabProjects"},
            {context for context, unused_source in messages},
        )


class ShaderWorkspaceBridgeContractTests(unittest.TestCase):
    def test_bridge_registers_unregisters_and_shows_workspace_by_default(self):
        tree = _parse(BRIDGE_PATH)
        register = _function(tree, "_register_shader_workspace_ui")
        unregister = _function(tree, "_unregister_shader_workspace_ui")
        start = _function(tree, "start")
        stop = _function(tree, "stop")

        module_names = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        }
        self.assertIn("_shader_workspace_ui", module_names)
        self.assertIn("_shader_workspace_callbacks", module_names)

        register_attributes = _attribute_calls(register)
        self.assertIn("ReplayLabShaderWorkspacePanel", register_attributes)
        self.assertIn("RegisterWindowMenu", register_attributes)
        self.assertIn("_show", _name_calls(register))

        self.assertIn("_register_shader_workspace_ui", _name_calls(start))
        self.assertIn(
            "_unregister_shader_workspace_ui",
            {
                node.id
                for node in ast.walk(stop)
                if isinstance(node, ast.Name)
            },
        )
        self.assertIn("shutdown", _attribute_calls(unregister))
        unregister_source = ast.get_source_segment(
            BRIDGE_PATH.read_text(encoding="utf-8"), unregister)
        self.assertIsNotNone(unregister_source)
        self.assertLess(
            unregister_source.index("panel.shutdown()"),
            unregister_source.index("_shader_workspace_ui = None"),
        )


class _Tabs:
    def __init__(self):
        self.ids = []
        self.selected = -1
        self.blocked = False

    def count(self):
        return len(self.ids)

    def removeTab(self, index):
        self.ids.pop(index)

    def blockSignals(self, blocked):
        self.blocked = blocked

    def setCurrentIndex(self, index):
        self.selected = index

    def currentIndex(self):
        return self.selected

    def tabData(self, index):
        return self.ids[index] if 0 <= index < len(self.ids) else ""


class _DeferredExecutor:
    def __init__(self):
        self.jobs = []
        self.cancelled = set()

    def submit(self, owner, operation, callback):
        self.jobs.append((owner, operation, callback))

    def cancel_owner(self, owner):
        self.cancelled.add(owner)


def _finish_refresh(panel):
    owner, operation, callback = panel._snapshot_executor.jobs.pop(0)
    result = operation()
    if owner not in panel._snapshot_executor.cancelled:
        callback(result, "")
    return result


def _scoped_panel(tmp_path, binding):
    """Execute production panel methods with offline native-widget doubles."""
    module = _load_without_qt()
    namespace = dict(vars(module))
    namespace["QtCore"] = types.SimpleNamespace(
        Qt=types.SimpleNamespace(UserRole=256),
        QCoreApplication=types.SimpleNamespace(translate=lambda _context, text: text))
    namespace["QtWidgets"] = types.SimpleNamespace(
        QMessageBox=mock.Mock(), QTreeWidgetItem=lambda _text: mock.Mock())
    names = ("_current_capture_binding", "_can_resume_document", "capture_changed", "refresh", "_document_model",
             "_open_workspace", "_save_document", "_confirm_panel_close", "_snapshot_ready",
             "_apply_workspace_snapshot", "_cached_source_document", "_cancel_snapshot_refresh",
             "_filesystem_changed", "_reset_watched_paths", "_request_live_usage",
             "_drain_usage_results", "_add_workspace_to_chat",
             "show_organization_preview", "_discard_organization_preview",
             "_canonical_organization_preview_workspace", "_organization_preview_display_state",
             "_canonical_workspace_id", "_aliased_source_preview_workspace",
             "_document_source_text",
             "_preview_replaces_clean_capture_source", "_document_has_local_work",
             "_retire_superseded_project_sources",
             "_rebind_project_documents",
             "_retire_rebound_document",
             "_refresh_saved_organization_source", "_workspace_tree_signature", "_render_tree",
             "_apply_loaded_document")
    names += ("_load_state", "_save_state", "_close_document", "_close_other_documents", "shutdown")
    names += ("_definition_requested", "_open_related_definition", "_promote_submitted_save")
    tree = _parse(MODULE_PATH)
    methods = ast.Module(body=[copy.deepcopy(_method(tree, name)) for name in names], type_ignores=[])
    exec(compile(ast.fix_missing_locations(methods), str(MODULE_PATH), "exec"), namespace)
    panel_type = type("ScopedPanelHarness", (), {name: namespace[name] for name in names})
    panel = panel_type()
    provider = {"binding": binding}
    panel._capture_binding_provider = lambda: provider["binding"]
    panel._capture_binding = panel._current_capture_binding()
    panel._alive = True
    panel._shutting_down = False
    panel._snapshot_executor = _DeferredExecutor()
    panel._owns_snapshot_executor = False
    panel._snapshot_owner = "panel-test"
    panel._snapshot_generation = 0
    panel._snapshot_running = False
    panel._snapshot_again = False
    panel._snapshot_stop = threading.Event()
    panel._source_documents = {}
    panel._organization_previews = {}
    panel._source_request_states = {}
    panel._pending_open = {}
    panel._pending_reload = {}
    panel._last_disk_signature = None
    panel._snapshot_error_retry_at = 0.0
    panel._snapshot_error_retry_delay = module.SNAPSHOT_ERROR_RETRY_MIN_SECONDS
    panel._tree_signature = None
    panel._flow_graph_signature = None
    panel._restored = False
    panel._snapshot = None
    panel._workspaces = {}
    panel._document_aliases = {}
    panel._shader_labels = {}
    panel._documents = {}
    panel._live_check = {}
    panel._local_analysis = {}
    panel._usage_cache = {}
    panel._usage_pending = set()
    panel._usage_results = queue.Queue()
    panel._usage_generation = 0
    panel._workspace_event_ids = {}
    panel._tree_items = {}
    panel._pinned = []
    panel._restore_open = []
    panel._restore_active = ""
    panel._restore_default_document = True
    panel._closed_document_ids = set()
    panel._saved_tab_state = {}
    panel._files_visible = False
    panel._flow_visible = True
    panel._stages_visible = True
    panel._debug_snapshots_visible = False
    settings = {}
    panel._settings = types.SimpleNamespace(
        setValue=lambda key, value: settings.update({key: value}), values=settings,
        value=lambda key, default="": settings.get(key, default))
    panel.registry_path = str(tmp_path / "registry.json")
    panel.project_selection_path = str(tmp_path / "active-project.json")
    panel.tab_bar = _Tabs()
    panel.file_tree = mock.Mock()
    panel.flow_graph = mock.Mock()
    panel.editor_stack = mock.Mock()
    panel.ctx = mock.Mock()
    panel.ctx.GetResources.return_value = []
    panel.empty_page = object()
    panel.top = object()
    panel.status_label = mock.Mock()
    panel._save_tasks = queue.Queue()
    panel._watched_paths = [panel.registry_path, panel.project_selection_path]
    panel.change_timer = mock.Mock()
    panel._refresh_current_view = mock.Mock()
    panel._render_tree = mock.Mock()
    panel._rerender_tree_keep_selection = mock.Mock()
    panel._refresh_document_from_disk = mock.Mock()
    panel._reset_watched_paths = mock.Mock()
    panel._tab_changed = mock.Mock()
    panel._tab_index = lambda key: panel.tab_bar.ids.index(key) if key in panel.tab_bar.ids else -1

    def opened(key, **_kwargs):
        panel.tab_bar.ids.append(key)
        return True

    panel._open_workspace = mock.Mock(side_effect=opened)
    panel._tab_workspace_ids = lambda: list(panel.tab_bar.ids)

    def source_document(_registry, authorization_id, key, capture_binding=None):
        snapshot = getattr(module._shader_console_protocol.load_console_snapshot, "return_value", {})
        workspace = next(item for item in snapshot.get("workspaces", [])
                         if item["workspaceId"] == key)
        source = Path(workspace.setdefault("sourcePath", str(tmp_path / (key + ".hlsl"))))
        source.parent.mkdir(parents=True, exist_ok=True)
        if not source.exists():
            source.write_text("float4 MainPS() { return 0; }", encoding="utf-8")
        info = source.stat()
        return {"workspaceId": key, "authorizationId": authorization_id,
                "captureBinding": capture_binding, "sourcePath": str(source),
                "text": source.read_text(encoding="utf-8"), "rawSha256": "3" * 64,
                "modifiedNs": info.st_mtime_ns, "size": info.st_size,
                "liveEditId": (workspace.get("liveEdit") or {}).get("liveEditId"),
                "watchId": (workspace.get("watch") or {}).get("watchId")}

    module._shader_console_protocol.load_authorized_source_document = source_document
    return panel, provider, module._shader_console_protocol, panel_type


def _capture_binding(tmp_path, session="1" * 32):
    return {"instanceId": "offline-gui", "captureSessionId": session,
            "capturePath": str(tmp_path / "capture.rdc")}


def _canonical_display_fixture(tmp_path):
    panel, provider, protocol, panel_type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    canonical_id, preview_id = "a" * 32, "b" * 64
    text, source_sha = "float4 MainPS() : SV_Target { return 1; }", "c" * 64
    workspace = {"workspaceId": canonical_id, "workspacePath": str(tmp_path),
                 "captureId": "d" * 64, "capturePath": panel._capture_binding["capturePath"],
                 "stage": "fragment", "entryPoint": "MainPS", "resourceId": 1000000000000000723,
                 "sourcePath": str(tmp_path / "source/shader.hlsl"), "editorEligible": True,
                 "autoApplyReady": True, "authorizationId": "e" * 32,
                 "liveEdit": {"liveEditId": "f" * 32}}
    source = {"workspaceId": canonical_id, "captureBinding": dict(panel._capture_binding),
              "sourcePath": workspace["sourcePath"], "rawSha256": source_sha, "text": text,
              "authorizationId": workspace["authorizationId"], "liveEditId": "f" * 32}
    opened = {**workspace, "workspaceId": preview_id, "sourceWorkspaceId": canonical_id,
              "captureBinding": dict(panel._capture_binding), "documentKind": "organization-preview",
              "readOnly": True, "canonicalSourceUpdated": True, "organizationId": "1" * 32,
              "rawSha256": source_sha, "text": text, "fileName": "shader.hlsl"}
    panel._workspaces[canonical_id] = workspace
    panel._source_documents[canonical_id] = source

    def document(key, read_only):
        editor = mock.Mock()
        loaded = panel._source_documents.get(key) or opened
        loaded_text = loaded.get("text", text)
        editor.GetText.return_value = loaded_text
        editor.IsReadOnly.return_value = read_only
        editor.SetReadOnly.side_effect = lambda value: setattr(editor.IsReadOnly, "return_value", value)
        return {"workspaceId": key, "workspace": panel._workspaces[key], "editor": editor,
                "documentKind": panel._workspaces[key].get("documentKind", "live-edit"), "readOnly": read_only,
                "captureBinding": dict(panel._capture_binding), "available": True, "dirty": False,
                "rawSha256": loaded.get("rawSha256", source_sha), "baselineEditorText": loaded_text}

    panel._documents[canonical_id] = document(canonical_id, False)
    panel.tab_bar.ids = [canonical_id]

    def open_once(key, **options):
        if key not in panel.tab_bar.ids:
            panel.tab_bar.ids.append(key)
        if key not in panel._documents:
            panel._documents[key] = document(key, panel._workspaces[key].get("readOnly", False))
        if options.get("select", True):
            panel.tab_bar.setCurrentIndex(panel._tab_index(key))
        return True

    def close_preview(key, prompt=False):
        assert key != canonical_id and prompt is False
        if any(panel._documents.get(key, {}).get(field) for field in ("dirty", "saveInFlight", "commitUnknown")):
            return False
        panel._documents.pop(key, None)
        panel._source_documents.pop(key, None)
        if key in panel.tab_bar.ids:
            panel.tab_bar.ids.remove(key)
        return True

    panel._open_workspace = mock.Mock(side_effect=open_once)
    panel._close_document = mock.Mock(side_effect=close_preview)
    panel._apply_loaded_document = mock.Mock()
    return panel, provider, panel_type, workspace, source, opened


@pytest.mark.parametrize("canonical_source", [False, True])
def test_preview_bridge_preserves_current_project_head_and_eid_for_both_source_loaders(canonical_source):
    handler = _function(_parse(BRIDGE_PATH), "_show_shader_organization_preview")
    session, capture, sha = "1" * 32, "2" * 64, "3" * 64
    binding = {"captureSessionId": session, "capturePath": "D:/capture.rdc"}
    project = {"knowledgeId": "4" * 32}
    opened = {"captureId": capture, "captureBinding": binding, "rawSha256": sha,
              "workspaceId": "5" * 64, "sourceWorkspaceId": "6" * 32,
              "organizationId": "" if canonical_source else "7" * 32}
    bound = dict(opened, projectSourceIdentity=project, projectSourceBinding=project, eventIds=[61408])
    protocol = mock.Mock()
    protocol.load_saved_shader_source_preview.return_value = opened
    protocol.load_verified_organization_preview.return_value = opened
    protocol.bind_project_shader_source_display.return_value = bound
    panel = mock.Mock()
    panel._capture_binding = binding
    panel.show_organization_preview.return_value = {
        "shown": True, "workspaceId": opened["workspaceId"],
        "documentKind": "organization-preview", "readOnly": False}
    namespace = {"_require_capture_identity": lambda value: value,
                 "_shader_override_hex": lambda value, *_args: value,
                 "_capture_session_id": session, "_request_admitted_capture_identity": lambda: None,
                 "_shader_workspace_capture_binding": lambda: binding,
                 "_shader_console_protocol": protocol, "_invoke_on_ui_sync": lambda callback: callback(),
                 "_shader_workspace_ui": panel, "_shader_workspace_callbacks": []}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[handler], type_ignores=[])),
                 str(BRIDGE_PATH), "exec"), namespace)
    receipt = namespace[handler.name]({
        "canonical_source": canonical_source, "expected_capture_id": capture,
        "expected_capture_session_id": session, "organization_id": opened["organizationId"],
        "expected_source_sha256": sha, "workspace_path": "D:/workspace",
        "project_source_binding": project})
    protocol.bind_project_shader_source_display.assert_called_once_with(opened, project)
    panel.show_organization_preview.assert_called_once_with(bound)
    assert receipt["eventIds"] == [61408]
    assert receipt["projectSourceIdentity"] == receipt["projectSourceBinding"] == project


@pytest.mark.parametrize("already_open", [False, True])
def test_deferred_include_navigation_runs_after_owner_tab_is_available(tmp_path, already_open):
    panel, unused, panel_type, workspace, unused_source, unused_opened = _canonical_display_fixture(tmp_path)
    if not already_open:
        panel.tab_bar.ids = []
        panel._insert_tab = lambda key, unused: panel.tab_bar.ids.append(key) or 0
    panel._definition_requested = mock.Mock()
    request = ("D:/project/source/common.hlsli", 62, 2)
    assert panel_type._open_workspace(panel, workspace["workspaceId"], quiet=True, definition=request)
    panel._definition_requested.assert_called_once_with(
        *request, owner_workspace_id=workspace["workspaceId"])


def test_repeated_preview_of_same_saved_shader_reuses_actual_writable_document(tmp_path):
    panel, unused_provider, panel_type, workspace, source, opened = _canonical_display_fixture(tmp_path)
    canonical_id = workspace["workspaceId"]
    editor = panel._documents[canonical_id]["editor"]
    # Exercise the real existing-tab branch, not a mock document merger.
    panel._open_workspace = types.MethodType(panel_type._open_workspace, panel)
    first = panel.show_organization_preview(opened)
    second = panel.show_organization_preview(opened)
    assert first == second
    assert first == {"shown": True, "workspaceId": canonical_id, "documentKind": "live-edit",
                     "readOnly": False, "canonicalWorkspaceReused": True}
    assert list(panel._workspaces) == [canonical_id]
    assert list(panel._documents) == [canonical_id]
    assert panel.tab_bar.ids == [canonical_id]
    assert panel._organization_previews == {}
    assert panel._documents[canonical_id]["editor"] is editor
    panel.ctx.CreateShaderCodeEditor.assert_not_called()
    editor.SetText.assert_not_called()
    editor.SetReadOnly.assert_not_called()
    panel._apply_loaded_document.assert_not_called()


def _bind_pending_project_preview(workspace, opened):
    identity = {"projectId": "1" * 32, "knowledgeId": "2" * 32,
                "originalRawSha256": "3" * 64, "sourceSha256": opened["rawSha256"],
                "stage": opened["stage"], "entryPoint": opened["entryPoint"]}
    workspace["shaderRawSha256"] = opened["shaderRawSha256"] = identity["originalRawSha256"]
    opened["projectSourceIdentity"] = identity
    opened["projectSourceBinding"] = dict(identity, captureSessionId=opened["captureBinding"]["captureSessionId"],
                                         resourceId=opened["resourceId"])
    opened.update(sourceWorkspaceId="9" * 32, workspacePath="C:/project/recovered",
                  sourcePath="C:/project/recovered/source/shader.hlsl", captureId="8" * 64,
                  eventIds=[61408])
    return identity


def test_current_pending_head_reuses_equal_source_in_current_physical_workspace(tmp_path):
    panel, unused, panel_type, workspace, source, opened = _canonical_display_fixture(tmp_path)
    identity = _bind_pending_project_preview(workspace, opened)
    workspace["autoApplyReady"] = False
    receipt = panel.show_organization_preview(opened)
    assert receipt["workspaceId"] == workspace["workspaceId"]
    assert receipt["canonicalProjectSourceReused"] == identity
    assert list(panel._workspaces) == [workspace["workspaceId"]]
    assert panel._source_documents[workspace["workspaceId"]]["sourcePath"] == source["sourcePath"]
    assert panel._workspace_event_ids[workspace["workspaceId"]] == [61408]
    assert workspace["autoApplyReady"] is False
    panel._apply_loaded_document.assert_not_called()

@pytest.mark.parametrize("dirty", [False, True])
def test_current_head_reusing_canonical_editor_retires_old_clean_project_preview(tmp_path, dirty):
    panel, unused, unused_type, workspace, source, opened = _canonical_display_fixture(tmp_path)
    identity = _bind_pending_project_preview(workspace, opened)
    old_id = "7" * 64
    old_workspace = dict(workspace, workspaceId=old_id, sourceWorkspaceId="6" * 32,
                         documentKind="organization-preview", sourcePath="C:/project/old/source/shader.hlsl",
                         projectSourceIdentity=dict(identity, knowledgeId="0" * 32, sourceSha256="5" * 64))
    old_source = dict(opened, workspaceId=old_id, sourceWorkspaceId="6" * 32,
                      sourcePath=old_workspace["sourcePath"], rawSha256="5" * 64, text="older saved head")
    editor = mock.Mock()
    editor.GetText.return_value = "user edit" if dirty else old_source["text"]
    old_document = dict(panel._documents[workspace["workspaceId"]], workspaceId=old_id,
                        workspace=old_workspace, editor=editor, rawSha256="5" * 64,
                        baselineEditorText=old_source["text"], dirty=dirty)
    panel._workspaces[old_id] = old_workspace
    panel._source_documents[old_id] = old_source
    panel._documents[old_id] = old_document
    panel._organization_previews[old_id] = {"workspace": old_workspace, "opened": old_source}
    panel.tab_bar.ids.append(old_id)
    panel._pinned = [old_id]
    panel._restore_open = [old_id]
    panel._restore_active = old_id
    generation = panel._snapshot_generation

    result = panel.show_organization_preview(opened)

    assert result["workspaceId"] == workspace["workspaceId"]
    assert result["canonicalProjectSourceReused"] == identity
    assert panel._workspace_event_ids[workspace["workspaceId"]] == [61408]
    assert panel._snapshot_generation > generation  # Old-head asynchronous reads are stale.
    if dirty:
        assert panel._documents[old_id] is old_document
        assert editor.GetText() == "user edit"
        assert old_id in panel._organization_previews
        panel._close_document.assert_not_called()
    else:
        assert set(panel._workspaces) == set(panel._documents) == {workspace["workspaceId"]}
        assert old_id not in panel._organization_previews
        assert old_id not in panel._source_documents
        assert panel._pinned == panel._restore_open == [workspace["workspaceId"]]
        assert panel._restore_active == workspace["workspaceId"]


@pytest.mark.parametrize("dirty", [False, True])
def test_pending_head_supersedes_clean_baseline_but_preserves_local_work(tmp_path, dirty):
    panel, unused, panel_type, workspace, source, opened = _canonical_display_fixture(tmp_path)
    _bind_pending_project_preview(workspace, opened)
    opened["rawSha256"] = "0" * 64
    opened["projectSourceIdentity"]["sourceSha256"] = opened["rawSha256"]
    canonical_id = workspace["workspaceId"]
    document = panel._documents[canonical_id]
    document["dirty"] = dirty
    if dirty:
        document["editor"].GetText.return_value = "unsaved manual edit"
    # The production close path may close a clean canonical editor too.
    def close_clean(key, prompt=False):
        assert not panel._document_has_local_work(key)
        panel._documents.pop(key, None)
        if key in panel.tab_bar.ids:
            panel.tab_bar.ids.remove(key)
        return True
    panel._close_document = mock.Mock(side_effect=close_clean)
    receipt = panel.show_organization_preview(opened)
    assert receipt["workspaceId"] == opened["workspaceId"]
    assert (canonical_id in panel._workspaces) is dirty
    if dirty:
        assert panel._documents[canonical_id]["editor"].GetText() == "unsaved manual edit"
        panel._close_document.assert_not_called()
    else:
        assert list(panel._workspaces) == [opened["workspaceId"]]
    assert panel._workspaces[opened["workspaceId"]]["autoApplyReady"] is False


def test_duplicate_project_binding_keeps_dirty_alternate_and_retargets_clean_pins(tmp_path):
    panel, unused, panel_type, workspace, source, opened = _canonical_display_fixture(tmp_path)
    old_id = workspace["workspaceId"]
    new_id = "7" * 32
    preferred = dict(workspace, workspaceId=new_id)
    document = panel._documents[old_id]
    document["editor"].GetText.return_value = "unsaved text not yet signalled"
    panel._pinned = [old_id]
    result = {"snapshot": {"workspaces": [preferred], "alternateWorkspaces": [workspace],
                           "documentAliases": {old_id: new_id}}, "eventIds": {},
              "watchedPaths": [], "existingPaths": [], "signature": ()}
    panel._apply_workspace_snapshot(result, panel._capture_binding)
    assert panel._pinned == [old_id]
    assert panel._workspaces[old_id]["retainedWorkingCopy"] is True
    assert panel._documents[old_id]["editor"].GetText() == "unsaved text not yet signalled"
    document["editor"].GetText.return_value = document["baselineEditorText"]
    panel._apply_workspace_snapshot(result, panel._capture_binding)
    assert panel._pinned == [new_id]
    assert list(panel._workspaces) == [new_id]


def test_tree_groups_same_rdc_catalogs_under_one_capture(tmp_path):
    panel, unused, panel_type, workspace, source, opened = _canonical_display_fixture(tmp_path)
    panel.file_tree = mock.Mock()
    panel._render_tree = types.MethodType(panel_type._render_tree, panel)
    other = dict(workspace, workspaceId="9" * 32, captureId="0" * 64, resourceId=999)
    panel._render_tree([workspace, other])
    assert panel.file_tree.addTopLevelItem.call_count == 1
    assert set(panel._tree_items) == {workspace["workspaceId"], other["workspaceId"]}


def test_tree_displays_numeric_eids_first_across_stages_and_unbound_last(tmp_path):
    panel, unused, panel_type, workspace, source, opened = _canonical_display_fixture(tmp_path)

    class TreeItem:
        def __init__(self, labels):
            self.label = labels[0]
            self.children = []
            self.metadata = {}

        def addChild(self, item):
            self.children.append(item)

        def setData(self, column, role, value):
            self.metadata[column, role] = value

        def setToolTip(self, column, value):
            self.tooltip = value

    panel._render_tree = types.MethodType(panel_type._render_tree, panel)
    panel._render_tree.__func__.__globals__["QtWidgets"].QTreeWidgetItem = TreeItem
    rows = [dict(workspace, workspaceId=key, stage=stage, entryPoint=entry)
            for key, stage, entry in (
                ("unbound", "vertex", "UnknownVS"), ("hundred", "vertex", "MainVS"),
                ("twenty", "fragment", "MainPS"), ("ten", "vertex", "MainVS"),
                ("twenty-again", "fragment", "OtherPS"))]
    panel._workspace_event_ids = {"hundred": [100], "twenty": [20], "ten": [10],
                                  "twenty-again": [90, 80, 20, 50, 60, 20]}
    panel._render_tree(rows)
    capture = panel.file_tree.addTopLevelItem.call_args.args[0]
    assert [leaf.metadata[0, 256] for leaf in capture.children] == [
        "ten", "twenty", "twenty-again", "hundred", "unbound"]
    assert [leaf.label for leaf in capture.children] == [
        "EID 10 · VS · MainVS · shader.hlsl", "EID 20 · PS · MainPS · shader.hlsl",
        "EID 20, 50, 60, 80 +1 · PS · OtherPS · shader.hlsl",
        "EID 100 · VS · MainVS · shader.hlsl", "VS · UnknownVS · shader.hlsl"]
    assert "20, 50, 60, 80, 90" in capture.children[2].tooltip
    assert all(not leaf.children for leaf in capture.children)
    assert set(panel._tree_items) == {row["workspaceId"] for row in rows}
    assert panel._workspace_tree_signature(rows) == panel._workspace_tree_signature(list(reversed(rows)))

    # Asynchronous usage discovery must reposition an unbound leaf without
    # changing its document identity or depending on lexical label order.
    panel._workspace_event_ids["unbound"] = [2]
    panel._render_tree(list(reversed(rows)))
    capture = panel.file_tree.addTopLevelItem.call_args.args[0]
    assert capture.children[0].metadata[0, 256] == "unbound"
    assert capture.children[0].label.startswith("EID 2 · VS · ")


def test_tree_event_order_is_numeric_defensive_and_keeps_working_copies_separate():
    module = _load_without_qt()
    rows = [{"workspaceId": "invalid", "capturePath": "D:/b.rdc"},
            {"workspaceId": "copy", "capturePath": "D:/a.rdc", "retainedWorkingCopy": True},
            {"workspaceId": "valid", "capturePath": "D:/b.rdc"},
            {"workspaceId": "copy-first", "capturePath": "D:/z.rdc", "retainedWorkingCopy": True}]
    events = {"invalid": [None, -4, 0, True, "1"], "valid": [99, 10, 10],
              "copy": [5], "copy-first": [2]}
    assert module._workspace_tree_event_ids(rows[0], events) == []
    assert module._workspace_tree_event_ids(rows[2], events) == [10, 99]
    assert [row["workspaceId"] for row in sorted(
        rows, key=lambda row: module._workspace_tree_sort_key(row, events))] == [
            "valid", "invalid", "copy-first", "copy"]


def test_repeated_same_preview_never_overwrites_unsignalled_native_edit(tmp_path):
    panel, unused, panel_type, workspace, source, opened = _canonical_display_fixture(tmp_path)
    panel._workspaces.clear()
    panel._source_documents.clear()
    panel._documents.clear()
    panel.tab_bar.ids = []
    assert panel.show_organization_preview(opened)["shown"] is True
    preview = panel._documents[opened["workspaceId"]]
    preview["editor"].GetText.return_value = "unsaved native text"
    assert preview["dirty"] is False
    panel._apply_loaded_document.reset_mock()
    assert panel.show_organization_preview(opened) is False
    assert preview["editor"].GetText() == "unsaved native text"
    panel._apply_loaded_document.assert_not_called()


def test_disk_refresh_detects_native_edit_before_dirty_signal_and_keeps_conflict(tmp_path):
    panel, unused, panel_type, workspace, source, opened = _canonical_display_fixture(tmp_path)
    key = workspace["workspaceId"]
    document = panel._documents[key]
    document["editor"].GetText.return_value = "unsaved native text"
    disk = dict(source, rawSha256="0" * 64, text="external source update")
    panel._cached_source_document = lambda unused: disk
    panel._update_tab = mock.Mock()
    panel._current_workspace_id = lambda: key
    panel._document_source_text = lambda value: str(value["editor"].GetText())
    namespace = panel.refresh.__func__.__globals__
    methods = ast.Module(body=[copy.deepcopy(_method(_parse(MODULE_PATH), name))
                              for name in ("_refresh_document_from_disk", "_sync_document_dirty")], type_ignores=[])
    exec(compile(ast.fix_missing_locations(methods), str(MODULE_PATH), "exec"), namespace)
    panel._sync_document_dirty = types.MethodType(namespace["_sync_document_dirty"], panel)
    namespace["_refresh_document_from_disk"](panel, key)
    assert document["dirty"] is True and document["conflict"] is True
    assert document["editor"].GetText() == "unsaved native text"
    panel._apply_loaded_document.assert_not_called()


@pytest.mark.parametrize("dirty", [False, True])
def test_switch_rdc_rebinds_clean_project_tab_and_keeps_dirty_source_only_copy(tmp_path, dirty):
    panel, provider, panel_type, workspace, source, opened = _canonical_display_fixture(tmp_path)
    identity = _bind_pending_project_preview(workspace, opened)
    workspace["projectSourceIdentity"] = identity
    old_id = workspace["workspaceId"]
    old_document = panel._documents[old_id]
    old_editor = old_document["editor"]
    original_path = old_document["sourcePath"] = workspace["sourcePath"]
    old_editor.IsReadOnly.return_value = True
    if dirty:
        old_editor.GetText.return_value = "unsignalled dirty HLSL"
    new_binding = dict(panel._capture_binding, capturePath=str(tmp_path / "other.rdc"), captureSessionId="8" * 32)
    provider["binding"] = new_binding
    panel._capture_binding = panel._current_capture_binding()
    new_binding = panel._capture_binding
    new_id = "7" * 32
    new_workspace = dict(workspace, workspaceId=new_id, resourceId=9230,
                         capturePath=new_binding["capturePath"], sourcePath=str(tmp_path / "current/source.hlsl"))
    panel._workspaces = {new_id: new_workspace}
    panel._source_documents = {new_id: dict(source, workspaceId=new_id, captureBinding=new_binding,
                                          sourcePath=new_workspace["sourcePath"])}
    panel._pinned = [old_id]
    panel._restore_open = []
    panel.tab_bar.ids = []
    def close_clean(key, prompt=False):
        assert key == old_id and not panel._document_has_local_work(key)
        panel._documents.pop(key)
        panel._pinned.remove(key)
        return True
    panel._close_document = mock.Mock(side_effect=close_clean)
    panel._rebind_project_documents(new_binding)
    if dirty:
        assert panel._documents[old_id] is old_document
        assert old_document["sourcePath"] == original_path
        assert old_document["captureBinding"] == new_binding
        assert old_document["available"] is True and old_editor.IsReadOnly() is False
        assert old_editor.GetText() == "unsignalled dirty HLSL"
        retained = panel._workspaces[old_id]
        assert retained["retainedWorkingCopy"] is True and retained["autoApplyReady"] is False
        assert retained["liveEdit"] == retained["watch"] == {}
        assert old_id in panel._restore_open
        panel._close_document.assert_not_called()
    else:
        assert old_id not in panel._documents
        assert panel._pinned == [new_id] and new_id in panel._restore_open
        panel._close_document.assert_called_once_with(old_id, prompt=False)
    old_editor.SetText.assert_not_called()


def test_saved_preview_unlocks_canonical_source_without_granting_live_edit(tmp_path):
    panel, unused_provider, panel_type, workspace, source, opened = _canonical_display_fixture(tmp_path)
    workspace["autoApplyReady"] = False
    document = panel._documents[workspace["workspaceId"]]
    document["readOnly"] = True
    document["documentKind"] = "shader"
    document["editor"].IsReadOnly.return_value = True
    panel._open_workspace = types.MethodType(panel_type._open_workspace, panel)

    receipt = panel.show_organization_preview(opened)

    assert receipt["workspaceId"] == workspace["workspaceId"]
    assert receipt["canonicalWorkspaceReused"] is True
    assert receipt["readOnly"] is False
    assert list(panel._documents) == [workspace["workspaceId"]]
    assert panel.tab_bar.ids == [workspace["workspaceId"]]
    assert panel._organization_previews == {}
    document["editor"].SetText.assert_not_called()
    document["editor"].SetReadOnly.assert_called_once_with(False)
    assert workspace["autoApplyReady"] is False


@pytest.mark.parametrize("changed", [
    "dirty", "saveInFlight", "commitUnknown", "pendingSave", "conflict", "editor-unsaved",
    "document-hash", "document-capture", "source-hash", "source-path", "source-capture",
    "source-owner", "source-authorization", "source-live-edit", "captureId", "stage",
    "entryPoint", "resourceId", "source-only", "not-eligible",
])
def test_preview_keeps_separate_draft_when_canonical_is_not_the_exact_clean_source(tmp_path, changed):
    panel, unused_provider, unused_type, workspace, source, opened = _canonical_display_fixture(tmp_path)
    canonical_id = workspace["workspaceId"]
    document = panel._documents[canonical_id]
    editor = document["editor"]
    if changed in ("dirty", "saveInFlight", "commitUnknown", "pendingSave", "conflict"):
        document[changed] = True
    elif changed == "editor-unsaved":
        editor.GetText.return_value = "user unsaved edit"
    elif changed == "document-hash":
        document["rawSha256"] = "0" * 64
    elif changed == "document-capture":
        document["captureBinding"] = _capture_binding(tmp_path, "0" * 32)
    elif changed.startswith("source-") and changed not in ("source-only",):
        source.update({{"source-hash": "rawSha256", "source-path": "sourcePath",
                        "source-capture": "captureBinding", "source-owner": "workspaceId",
                        "source-authorization": "authorizationId", "source-live-edit": "liveEditId"}[changed]:
                       _capture_binding(tmp_path, "0" * 32) if changed == "source-capture" else "different"})
    elif changed == "source-only":
        opened["canonicalSourceUpdated"] = False
    elif changed == "not-eligible":
        workspace["editorEligible"] = False
    else:
        opened[changed] = "different"
    before = dict(document)
    receipt = panel.show_organization_preview(opened)
    assert receipt["workspaceId"] == opened["workspaceId"] and receipt["readOnly"] is False
    assert receipt["canonicalWorkspaceReused"] is False
    assert canonical_id in panel._workspaces and opened["workspaceId"] in panel._workspaces
    assert panel._documents[canonical_id] == before
    editor.SetText.assert_not_called()
    panel._close_document.assert_not_called()


def test_read_only_preview_merges_when_same_saved_canonical_becomes_available(tmp_path):
    panel, unused_provider, unused_type, workspace, source, opened = _canonical_display_fixture(tmp_path)
    canonical_id = workspace["workspaceId"]
    panel._workspaces.clear()
    panel._source_documents.clear()
    panel._documents.clear()
    panel.tab_bar.ids = []
    assert panel.show_organization_preview(opened)["readOnly"] is False
    assert list(panel._workspaces) == [opened["workspaceId"]]
    panel._source_documents[canonical_id] = source
    snapshot = {"snapshot": {"workspaces": [workspace]}, "eventIds": {},
                "watchedPaths": [], "existingPaths": [], "signature": ()}
    panel._apply_workspace_snapshot(snapshot, panel._capture_binding)
    assert list(panel._workspaces) == [canonical_id]
    assert list(panel._documents) == [canonical_id]
    assert panel.tab_bar.ids == [canonical_id]
    assert panel._organization_previews == {}
    panel._close_document.assert_called_once_with(opened["workspaceId"], prompt=False)
    assert panel._documents[canonical_id]["editor"].IsReadOnly() is False


def test_preview_merge_removes_the_rendered_leaf_before_reusing_same_host_snapshot(tmp_path):
    panel, unused_provider, panel_type, workspace, source, opened = _canonical_display_fixture(tmp_path)
    canonical_id, preview_id = workspace["workspaceId"], opened["workspaceId"]
    panel.file_tree = mock.Mock()
    panel._render_tree = types.MethodType(panel_type._render_tree, panel)
    snapshot = {"snapshot": {"workspaces": [workspace]}, "eventIds": {},
                "watchedPaths": [], "existingPaths": [], "signature": ()}
    panel._apply_workspace_snapshot(snapshot, panel._capture_binding)
    assert set(panel._tree_items) == {canonical_id}

    # Publication arrives before the asynchronous file reader has refreshed
    # the canonical bytes. Its preview must be visible until that read ends.
    panel._source_documents.pop(canonical_id)
    assert panel.show_organization_preview(opened)["canonicalWorkspaceReused"] is False
    assert set(panel._tree_items) == {canonical_id, preview_id}

    # The next Host snapshot has the same canonical IDs as the first. It must
    # still remove the preview leaf, rather than trust that old tree signature.
    panel._source_documents[canonical_id] = source
    panel._apply_workspace_snapshot(snapshot, panel._capture_binding)
    assert set(panel._tree_items) == set(panel._workspaces) == {canonical_id}
    assert preview_id not in panel._documents
    assert panel.tab_bar.ids == [canonical_id]
    renders = panel.file_tree.clear.call_count
    panel._apply_workspace_snapshot(snapshot, panel._capture_binding)
    assert panel.file_tree.clear.call_count == renders


@pytest.mark.parametrize("editable", [True, False])
def test_new_saved_result_refreshes_clean_editor_in_place_and_preserves_authorization(tmp_path, editable):
    panel, unused_provider, panel_type, workspace, source, opened = _canonical_display_fixture(tmp_path)
    canonical_id = workspace["workspaceId"]
    source["modifiedNs"] = 10
    workspace["autoApplyReady"] = editable
    document = panel._documents[canonical_id]
    document["autoApplyError"] = "earlier save could not start compilation"
    document["editor"].IsReadOnly.return_value = not editable
    document["readOnly"] = not editable
    opened.update(rawSha256="0" * 64, modifiedNs=20,
                  text="float4 MainPS() : SV_Target { return 2; }")

    def set_editor_text(text):
        document["editor"].GetText.return_value = text

    document["editor"].SetText.side_effect = set_editor_text
    panel._loading_documents = set()
    panel._newline_style = lambda unused: "\n"
    panel._update_tab = mock.Mock()
    panel._apply_loaded_document = mock.Mock(
        wraps=types.MethodType(panel_type._apply_loaded_document, panel))
    panel._open_workspace = types.MethodType(panel_type._open_workspace, panel)
    before_generation = panel._snapshot_generation
    receipt = panel.show_organization_preview(opened)

    assert receipt["canonicalWorkspaceReused"] is True
    assert receipt["workspaceId"] == canonical_id
    assert receipt["readOnly"] is False
    assert list(panel._workspaces) == list(panel._documents) == [canonical_id]
    assert panel.tab_bar.ids == [canonical_id]
    assert not panel._organization_previews
    assert panel._source_documents[canonical_id]["authorizationId"] == source["authorizationId"]
    assert panel._source_documents[canonical_id]["liveEditId"] == source["liveEditId"]
    assert panel._source_documents[canonical_id]["rawSha256"] == opened["rawSha256"]
    assert document["editor"].GetText() == opened["text"]
    assert document["autoApplyError"] == ""
    assert panel._snapshot_generation == before_generation + 1
    panel._apply_loaded_document.assert_called_once()
    if editable:
        document["editor"].SetReadOnly.assert_not_called()
    else:
        document["editor"].SetReadOnly.assert_called_once_with(False)


@pytest.mark.parametrize("state", ["dirty", "saveInFlight", "commitUnknown", "pendingSave",
                                   "conflict", "unsaved-text", "older-preview"])
def test_new_saved_result_never_replaces_unsaved_or_newer_canonical_editor(tmp_path, state):
    panel, unused_provider, unused_type, workspace, source, opened = _canonical_display_fixture(tmp_path)
    source["modifiedNs"] = 10
    opened.update(rawSha256="0" * 64, modifiedNs=20,
                  text="float4 MainPS() : SV_Target { return 2; }")
    document = panel._documents[workspace["workspaceId"]]
    if state == "unsaved-text":
        document["editor"].GetText.return_value = "unsaved user content"
    elif state == "older-preview":
        opened["modifiedNs"] = 5
    else:
        document[state] = True
    receipt = panel.show_organization_preview(opened)
    assert receipt["canonicalWorkspaceReused"] is False
    assert panel._source_documents[workspace["workspaceId"]] is source
    assert document["rawSha256"] == source["rawSha256"]
    panel._apply_loaded_document.assert_not_called()


@pytest.mark.parametrize("changed", ["different-proposal", "different-path", "preview-dirty", "preview-save", "preview-unknown", "preview-unsaved"])
def test_later_canonical_snapshot_preserves_other_or_unsaved_preview(tmp_path, changed):
    panel, unused_provider, unused_type, workspace, source, opened = _canonical_display_fixture(tmp_path)
    canonical_id = workspace["workspaceId"]
    panel._workspaces.clear()
    panel._source_documents.clear()
    panel._documents.clear()
    panel.tab_bar.ids = []
    assert panel.show_organization_preview(opened)["readOnly"] is False
    if changed in ("different-proposal", "different-path"):
        source["rawSha256"] = "0" * 64
        if changed == "different-proposal":
            panel._organization_previews[opened["workspaceId"]]["opened"]["canonicalSourceUpdated"] = False
        else:
            source["sourcePath"] = str(tmp_path / "other.hlsl")
    elif changed == "preview-unsaved":
        panel._documents[opened["workspaceId"]]["editor"].GetText.return_value = "user unsaved edit"
    else:
        panel._documents[opened["workspaceId"]][{
            "preview-dirty": "dirty", "preview-save": "saveInFlight", "preview-unknown": "commitUnknown"}[changed]] = True
    panel._source_documents[canonical_id] = source
    snapshot = {"snapshot": {"workspaces": [workspace]}, "eventIds": {},
                "watchedPaths": [], "existingPaths": [], "signature": ()}
    panel._apply_workspace_snapshot(snapshot, panel._capture_binding)
    assert opened["workspaceId"] in panel._workspaces
    assert opened["workspaceId"] in panel._documents
    panel._close_document.assert_not_called()


def test_old_clean_saved_preview_is_retired_for_newer_current_canonical_source(tmp_path):
    panel, unused_provider, unused_type, workspace, source, opened = _canonical_display_fixture(tmp_path)
    canonical_id = workspace["workspaceId"]
    panel._workspaces.clear()
    panel._source_documents.clear()
    panel._documents.clear()
    panel.tab_bar.ids = []
    assert panel.show_organization_preview(opened)["readOnly"] is False
    source.update(rawSha256="0" * 64, text="float4 MainPS() : SV_Target { return 2; }")
    panel._source_documents[canonical_id] = source
    snapshot = {"snapshot": {"workspaces": [workspace]}, "eventIds": {},
                "watchedPaths": [], "existingPaths": [], "signature": ()}
    panel._apply_workspace_snapshot(snapshot, panel._capture_binding)
    assert list(panel._workspaces) == [canonical_id]
    assert list(panel._documents) == [canonical_id]
    assert panel._documents[canonical_id]["rawSha256"] == "0" * 64
    assert panel._documents[canonical_id]["editor"].GetText() == source["text"]
    assert panel.tab_bar.ids == [canonical_id]
    panel._close_document.assert_called_once_with(opened["workspaceId"], prompt=False)


@pytest.mark.parametrize("preserve", [False, "different-proposal", "dirty"])
def test_new_saved_preview_retires_only_a_clean_old_canonical_display_copy(tmp_path, preserve):
    panel, unused_provider, unused_type, workspace, source, opened = _canonical_display_fixture(tmp_path)
    panel._workspaces.clear()
    panel._source_documents.clear()
    panel._documents.clear()
    panel.tab_bar.ids = []
    if preserve == "different-proposal":
        opened["canonicalSourceUpdated"] = False
    assert panel.show_organization_preview(opened)["readOnly"] is False
    if preserve == "dirty":
        panel._documents[opened["workspaceId"]]["dirty"] = True
    newer = {**opened, "workspaceId": "d" * 64, "organizationId": "2" * 32,
             "canonicalSourceUpdated": True, "rawSha256": "0" * 64,
             "text": "float4 MainPS() : SV_Target { return 2; }"}
    assert panel.show_organization_preview(newer)["readOnly"] is False
    if preserve:
        assert opened["workspaceId"] in panel._workspaces
        assert opened["workspaceId"] in panel._documents
    else:
        assert list(panel._workspaces) == [newer["workspaceId"]]
        assert panel.tab_bar.ids == [newer["workspaceId"]]


def test_organization_preview_refreshes_one_tab_and_bounds_new_organizations(tmp_path):
    panel, _provider, _protocol, _panel_type = _scoped_panel(
        tmp_path, _capture_binding(tmp_path))

    def open_once(key, **_kwargs):
        if key not in panel.tab_bar.ids:
            panel.tab_bar.ids.append(key)
        return True

    closed = []

    def close_preview(key, prompt=False):
        assert prompt is False
        closed.append(key)
        panel._documents.pop(key, None)
        if key in panel.tab_bar.ids:
            panel.tab_bar.ids.remove(key)
        panel._source_documents.pop(key, None)
        return True

    panel._open_workspace = mock.Mock(side_effect=open_once)
    panel._close_document = close_preview
    panel._apply_loaded_document = mock.Mock()

    def preview(document_id, source_workspace_id, organization_id,
                source_sha, modified_ns):
        return {
            "workspaceId": document_id,
            "sourceWorkspaceId": source_workspace_id,
            "workspacePath": str(tmp_path / source_workspace_id),
            "organizationId": organization_id,
            "documentKind": "organization-preview",
            "readOnly": True,
            "captureBinding": dict(panel._capture_binding),
            "captureId": "f" * 64,
            "capturePath": panel._capture_binding["capturePath"],
            "stage": "fragment",
            "entryPoint": "MainPS",
            "resourceId": 17,
            "sourcePath": str(tmp_path / (source_sha + ".hlsl")),
            "fileName": source_sha + ".hlsl",
            "rawSha256": source_sha,
            "validationTier": "segment-compile-check",
            "completedChecks": ["compile"],
            "deferredFinalChecks": ["replay-abi", "equivalence"],
            "text": "float4 MainPS() : SV_Target { return 1; }",
            "size": 48,
            "modifiedNs": modified_ns,
        }

    stable_id = "a" * 64
    source_workspace = "1" * 32
    assert panel.show_organization_preview(preview(
        stable_id, source_workspace, "2" * 32, "3" * 64, 1))
    assert panel.show_organization_preview(preview(
        stable_id, source_workspace, "2" * 32, "4" * 64, 2))
    assert panel.tab_bar.ids == [stable_id]
    assert len(panel._organization_previews) == 1
    assert panel._organization_previews[stable_id]["opened"]["rawSha256"] == "4" * 64

    # A genuinely different draft retains its read-only document. A later
    # organization showing the same bytes/path can retire a redundant preview.
    panel._documents[stable_id] = {"readOnly": True}
    replacement_id = "b" * 64
    assert panel.show_organization_preview(preview(
        replacement_id, source_workspace, "5" * 32, "6" * 64, 3))
    assert stable_id not in closed
    assert stable_id in panel._organization_previews
    assert panel.tab_bar.ids == [stable_id, replacement_id]

    identical_id = "c" * 64
    panel._documents[replacement_id] = {"readOnly": True}
    assert panel.show_organization_preview(preview(
        identical_id, source_workspace, "7" * 32, "6" * 64, 4))
    assert replacement_id in closed
    assert replacement_id not in panel._organization_previews
    assert identical_id in panel._organization_previews

    panel._documents[identical_id] = {"readOnly": True}
    for index in range(10):
        document_id = hashlib.sha256(("preview-%d" % index).encode()).hexdigest()
        source_id = ("%032x" % (index + 100))[-32:]
        organization_id = ("%032x" % (index + 200))[-32:]
        source_sha = hashlib.sha256(("source-%d" % index).encode()).hexdigest()
        assert panel.show_organization_preview(preview(
            document_id, source_id, organization_id, source_sha, 10 + index))
        panel._documents[document_id] = {"readOnly": True}
    assert len(panel._organization_previews) <= 8
    assert len(panel.tab_bar.ids) <= 8


@pytest.mark.parametrize("source_kind", ["cached", "editor", "missing", "stale-cache", "stale-capture"])
def test_chat_attachment_uses_only_authorized_source_or_explicit_identity_only(tmp_path, source_kind):
    panel, provider, unused_protocol, unused_type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    panel.ctx.CurEvent.return_value = 61408
    panel.ctx.CurSelectedEvent.return_value = 61408
    workspace_id = "a" * 32
    source_path = str(tmp_path / "source.hlsl")
    workspace = {"workspaceId": workspace_id, "sourcePath": source_path,
                 "authorizationId": "b" * 32, "capturePath": panel._capture_binding["capturePath"],
                 "stage": "fragment", "entryPoint": "MainPS"}
    panel._workspaces[workspace_id] = workspace
    panel._workspace_event_ids[workspace_id] = [61408]
    original_text = "\n".join("float cached_%d;" % index for index in range(170))
    panel._source_documents[workspace_id] = {
        "captureBinding": dict(panel._capture_binding), "authorizationId": "b" * 32,
        "sourcePath": source_path, "text": original_text}
    if source_kind == "editor":
        panel._documents[workspace_id] = {
            "available": True, "captureBinding": dict(panel._capture_binding),
            "sourcePath": source_path, "authorizationId": "b" * 32,
            "editor": types.SimpleNamespace(GetText=lambda: "float unsaved_current_edit;")}
    elif source_kind == "missing":
        panel._source_documents.clear()
    elif source_kind == "stale-cache":
        panel._source_documents[workspace_id]["authorizationId"] = "c" * 32
    elif source_kind == "stale-capture":
        provider["binding"] = _capture_binding(tmp_path, session="2" * 32)
    with mock.patch("builtins.open", side_effect=AssertionError("UI must not read a source path")):
        panel._add_workspace_to_chat(workspace_id)
    if source_kind == "stale-capture":
        panel.ctx.AddTextToAIAssistant.assert_not_called()
        return
    panel.ctx.AddTextToAIAssistant.assert_called_once()
    text = panel.ctx.AddTextToAIAssistant.call_args.args[1]
    facts = json.loads(text)
    context = json.loads(panel.ctx.AddTextToAIAssistant.call_args.args[3])
    assert "workspacePath" in facts and context["shader"]["entryPoint"] == "MainPS"
    assert "float cached_0;" not in text and "float unsaved_current_edit;" not in text
    assert facts["sourceIncluded"] is False
    if source_kind in ("cached", "editor"):
        expected = "float unsaved_current_edit;" if source_kind == "editor" else original_text
        assert facts["sourceSha256"] == hashlib.sha256(expected.encode()).hexdigest()
        assert facts["readSource"]["tool"] == "read_shader_workspace_source"
    else:
        assert facts["sourceState"] == "saved-file-not-open"
        assert "Shader reference added" in panel.status_label.setText.call_args.args[0]


def test_live_usage_queries_replay_once_and_only_ui_drain_publishes(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    panel, unused_provider, unused_protocol, unused_type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    panel.ctx.GetResources.return_value = [types.SimpleNamespace(resourceId=77)]
    workspace = {"workspaceId": "one", "resourceId": 77,
                 "capturePath": panel._capture_binding["capturePath"]}
    panel._workspaces["one"] = workspace
    panel._request_live_usage([workspace])
    invoke = panel.ctx.Replay.return_value.AsyncInvoke
    invoke.assert_called_once()
    assert panel._workspace_event_ids == {}
    replay_callback = invoke.call_args.args[0]
    controller = types.SimpleNamespace(GetUsage=lambda resource: [
        types.SimpleNamespace(eventId=event) for event in (7, 3, 7, 0)])
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(replay_callback, controller).result(timeout=5)
    assert panel._workspace_event_ids == {}
    panel._drain_usage_results()
    assert panel._workspace_event_ids == {"one": [3, 7]}
    panel._rerender_tree_keep_selection.assert_called_once()
    panel._request_live_usage([workspace])
    invoke.assert_called_once()


@pytest.mark.parametrize("events", [[11371, 11384], []])
def test_live_usage_survives_representative_catalog_refresh_without_requery(tmp_path, events):
    binding = _capture_binding(tmp_path)
    panel, unused_provider, unused_protocol, unused_type = _scoped_panel(tmp_path, binding)
    panel.ctx.GetResources.return_value = [types.SimpleNamespace(resourceId=723)]
    workspace = {"workspaceId": "one", "resourceId": 723, "capturePath": binding["capturePath"],
                 "editorEligible": True}
    panel._workspaces["one"] = workspace
    panel._request_live_usage([workspace])
    invoke = panel.ctx.Replay.return_value.AsyncInvoke
    invoke.call_args.args[0](types.SimpleNamespace(GetUsage=lambda _rid: [
        types.SimpleNamespace(eventId=event) for event in events]))
    panel._drain_usage_results()
    snapshot = {"snapshot": {"workspaces": [workspace]}, "eventIds": {"one": [11371]},
                "watchedPaths": [], "existingPaths": [], "signature": ()}
    panel._apply_workspace_snapshot(snapshot, binding)
    panel._apply_workspace_snapshot(snapshot, binding)
    assert panel._workspace_event_ids["one"] == events
    invoke.assert_called_once()


def test_live_usage_is_shared_by_same_resource_workspaces_and_cached_late_arrivals(tmp_path):
    binding = _capture_binding(tmp_path)
    panel, unused_provider, unused_protocol, unused_type = _scoped_panel(tmp_path, binding)
    panel.ctx.GetResources.return_value = [types.SimpleNamespace(resourceId=723)]
    one = {"workspaceId": "one", "resourceId": 723, "capturePath": binding["capturePath"]}
    two = dict(one, workspaceId="two")
    panel._workspaces = {"one": one, "two": two}
    panel._request_live_usage([one, two])
    invoke = panel.ctx.Replay.return_value.AsyncInvoke
    invoke.assert_called_once()
    invoke.call_args.args[0](types.SimpleNamespace(GetUsage=lambda _rid: [
        types.SimpleNamespace(eventId=11384), types.SimpleNamespace(eventId=11371)]))
    panel._drain_usage_results()
    assert panel._workspace_event_ids == {"one": [11371, 11384], "two": [11371, 11384]}
    three = dict(one, workspaceId="three")
    panel._workspaces["three"] = three
    panel._request_live_usage([three])
    assert panel._workspace_event_ids["three"] == [11371, 11384]
    invoke.assert_called_once()


def test_live_usage_distinct_shaders_survive_native_tag_coalescing(tmp_path):
    binding = _capture_binding(tmp_path)
    panel, unused_provider, unused_protocol, unused_type = _scoped_panel(tmp_path, binding)
    resources = (721, 722, 723)
    panel.ctx.GetResources.return_value = [types.SimpleNamespace(resourceId=rid) for rid in resources]
    workspaces = [{"workspaceId": str(rid), "resourceId": rid,
                   "capturePath": binding["capturePath"]} for rid in resources]
    panel._workspaces = {row["workspaceId"]: row for row in workspaces}
    # Same behavior as ReplayManager::PushInvoke: a new tag replaces the older
    # callback still queued under that tag, with no callback/cancellation signal.
    queued = {}
    panel.ctx.Replay.return_value.AsyncInvoke.side_effect = lambda callback, tag: queued.update({tag: callback})
    panel._request_live_usage(workspaces)
    assert len(queued) == 3
    queried = []
    controller = types.SimpleNamespace(GetUsage=lambda rid: (
        queried.append(rid) or [types.SimpleNamespace(eventId=rid * 10 + i) for i in (1, 2)]))
    for callback in queued.values():
        callback(controller)
    panel._drain_usage_results()
    assert queried == list(resources)
    assert not panel._usage_pending
    assert panel._workspace_event_ids == {str(rid): [rid * 10 + 1, rid * 10 + 2] for rid in resources}
    panel._request_live_usage(workspaces)
    assert panel.ctx.Replay.return_value.AsyncInvoke.call_count == 3


def test_project_document_unions_all_current_capture_physical_bindings(tmp_path):
    module = _load_without_qt()
    binding = _capture_binding(tmp_path)
    panel, unused_provider, unused_protocol, unused_type = _scoped_panel(tmp_path, binding)
    panel.ctx.GetResources.return_value = [types.SimpleNamespace(resourceId=rid) for rid in (721, 722, 723)]
    primary = {"workspaceId": "one", "resourceId": 721, "capturePath": binding["capturePath"]}
    primary["relatedWorkspaceBindings"] = [
        dict(primary, workspaceId="two", resourceId=722),
        dict(primary, workspaceId="duplicate", resourceId=722),
        dict(primary, workspaceId="other-rdc", resourceId=723, capturePath=str(tmp_path / "other.rdc"))]
    panel._workspaces = {"one": primary}
    panel._workspace_event_ids["one"] = [99]
    panel._request_live_usage([primary])
    calls = panel.ctx.Replay.return_value.AsyncInvoke.call_args_list
    assert len(calls) == 2  # Once per original resource, no replay/EID traversal.
    controller = types.SimpleNamespace(GetUsage=lambda rid: [
        types.SimpleNamespace(eventId=eid) for eid in ({721: [10, 11], 722: [11, 20]}[rid])])
    calls[0].args[0](controller)
    panel._drain_usage_results()
    assert not module._workspace_usage_complete(primary, binding, panel._usage_cache)
    calls[1].args[0](controller)
    panel._drain_usage_results()
    assert module._workspace_usage_complete(primary, binding, panel._usage_cache)
    assert panel._workspace_event_ids == {"one": [10, 11, 20]}
    snapshot = {"snapshot": {"workspaces": [primary]}, "eventIds": {"one": [99]},
                "watchedPaths": [], "existingPaths": [], "signature": ()}
    primary["editorEligible"] = True
    panel._apply_workspace_snapshot(snapshot, binding)
    assert panel._workspace_event_ids == {"one": [10, 11, 20]}
    assert panel.ctx.Replay.return_value.AsyncInvoke.call_count == 2


def test_catalog_event_list_does_not_truncate_common_shaders(tmp_path):
    module = _load_without_qt()
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"uses": [
        {"resourceId": 77, "eventId": event} for event in range(1, 5001)]}), encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "manifest.json").write_text(json.dumps({
        "capture": {"catalogPath": str(catalog)}, "shader": {"resourceId": 77}}), encoding="utf-8")
    assert module._workspace_event_ids([{"workspaceId": "one", "workspacePath": str(workspace)}]) == {
        "one": list(range(1, 5001))}


def test_catalog_events_never_merge_another_capture_through_a_related_document(tmp_path):
    module = _load_without_qt()
    workspaces = []
    for number in (1, 2):
        path = tmp_path / ("workspace%d" % number)
        path.mkdir()
        capture = str(tmp_path / ("capture%d.rdc" % number))
        catalog = path / "catalog.json"
        catalog.write_text(json.dumps({"uses": [{"resourceId": 77, "eventId": number * 10}]}), encoding="utf-8")
        (path / "manifest.json").write_text(json.dumps({
            "capture": {"catalogPath": str(catalog), "capturePath": capture},
            "shader": {"resourceId": 77}}), encoding="utf-8")
        workspaces.append({"workspaceId": str(number), "workspacePath": str(path), "capturePath": capture})
    workspaces[0]["relatedWorkspaceBindings"] = [workspaces[1]]
    assert module._workspace_event_ids([workspaces[0]]) == {"1": [10]}
    # A rebound document must not inherit an old manifest's same numeric RID.
    workspaces[0]["capturePath"] = str(tmp_path / "new.rdc")
    assert module._workspace_event_ids([workspaces[0]]) == {}


@pytest.mark.parametrize("count", [2, 101, 4100])
@pytest.mark.parametrize("current_capture", [True, False])
def test_context_menu_all_event_pages_are_reachable_without_replay_queries(tmp_path, count, current_capture):
    module = _load_without_qt()

    class Signal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self):
            for callback in self.callbacks:
                callback()

    class Action:
        def __init__(self, text):
            self.text, self.enabled = text, True
            self.triggered = Signal()

        def setEnabled(self, enabled):
            self.enabled = enabled

        def setToolTip(self, _text):
            pass

    class Menu:
        instances = []

        def __init__(self, _parent=None):
            self.items, self.pages = [], []
            self.aboutToShow = Signal()
            self.instances.append(self)

        def addAction(self, text):
            item = Action(text)
            self.items.append(item)
            return item

        def addMenu(self, _text):
            item = Menu(self)
            self.pages.append(item)
            return item

        def addSeparator(self):
            pass

        def actions(self):
            return list(self.items)

        def exec_(self, _position):
            pass

    namespace = dict(vars(module))
    namespace.update(QtCore=types.SimpleNamespace(Qt=types.SimpleNamespace(UserRole=32),
        QCoreApplication=types.SimpleNamespace(translate=lambda _context, text: text)),
        QtWidgets=types.SimpleNamespace(QMenu=Menu))
    tree = ast.Module(body=[copy.deepcopy(_method(_parse(MODULE_PATH), "_tree_context_menu"))], type_ignores=[])
    exec(compile(ast.fix_missing_locations(tree), str(MODULE_PATH), "exec"), namespace)
    binding = _capture_binding(tmp_path)
    workspace = {"workspaceId": "one", "resourceId": 723,
                 "capturePath": binding["capturePath"] if current_capture else str(tmp_path / "other.rdc")}
    events = list(range(11371, 11371 + count))
    panel = types.SimpleNamespace(file_tree=mock.Mock(), _workspaces={"one": workspace},
        _workspace_event_ids={"one": events}, _current_capture_binding=lambda: binding,
        _usage_cache={}, _jump_to_event=mock.Mock(), _open_workspace=mock.Mock(),
        _add_workspace_to_chat=mock.Mock(), ctx=mock.Mock())
    if current_capture:
        panel._usage_cache[module._workspace_usage_key(workspace, binding)] = events
    panel.file_tree.itemAt.return_value.data.return_value = "one"
    namespace["_tree_context_menu"](panel, object())
    root = Menu.instances[0]
    assert any(action.text == ("Used by %d event(s)" if current_capture else
                              "Known uses: %d event(s)") % count for action in root.items)
    for page in root.pages:
        assert not page.items  # No action per event until this page is opened.
        page.aboutToShow.emit()
        page.aboutToShow.emit()
        assert 1 <= len(page.items) <= 48
    actions = [action for menu in Menu.instances for action in menu.items
               if action.text.startswith("Jump to EID ")]
    assert [int(action.text.rsplit(" ", 1)[1]) for action in actions] == events
    assert all(action.enabled == current_capture for action in actions)
    if current_capture:
        for action in actions:
            action.triggered.emit()
        assert [call.args[0] for call in panel._jump_to_event.call_args_list] == events
    panel.ctx.Replay.assert_not_called()


@pytest.mark.parametrize("delivery", ["before-query", "after-queue", "before-refresh", "same-binding-reopen"])
def test_live_usage_from_previous_capture_never_publishes_or_clears_new_pending(tmp_path, delivery):
    first = _capture_binding(tmp_path)
    panel, provider, unused_protocol, unused_type = _scoped_panel(tmp_path, first)
    panel.ctx.GetResources.return_value = [types.SimpleNamespace(resourceId=77)]
    workspace = {"workspaceId": "one", "resourceId": 77, "capturePath": first["capturePath"]}
    panel._workspaces["one"] = workspace
    panel._workspace_event_ids["one"] = [99]
    panel._request_live_usage([workspace])
    invoke = panel.ctx.Replay.return_value.AsyncInvoke
    old_callback = invoke.call_args.args[0]
    old_controller = types.SimpleNamespace(GetUsage=mock.Mock(return_value=[
        types.SimpleNamespace(eventId=11)]))
    if delivery in ("after-queue", "before-refresh", "same-binding-reopen"):
        old_callback(old_controller)
        assert not panel._usage_results.empty()
    provider["binding"] = _capture_binding(tmp_path, session="2" * 32)
    if delivery == "before-refresh":
        # A session can change before the next UI refresh calls capture_changed.
        panel._drain_usage_results()
        assert panel._usage_cache == {}
        assert panel._workspace_event_ids == {"one": [99]}
        panel._request_live_usage([workspace])
        invoke.assert_called_once()
        panel._rerender_tree_keep_selection.assert_not_called()
        return
    if delivery == "same-binding-reopen":
        # Generation invalidation is independent of the session ID check.
        panel.capture_changed(None)
        provider["binding"] = first
    panel.capture_changed(provider["binding"])
    assert panel._workspace_event_ids == {}
    panel._workspaces["one"] = workspace
    panel._request_live_usage([workspace])
    new_callback = invoke.call_args.args[0]
    new_pending = set(panel._usage_pending)
    assert new_pending
    if delivery == "before-query":
        old_callback(old_controller)
        old_controller.GetUsage.assert_not_called()
        assert panel._usage_results.empty()
    panel._drain_usage_results()
    assert panel._usage_pending == new_pending
    assert panel._usage_cache == {} and panel._workspace_event_ids == {}
    panel._rerender_tree_keep_selection.assert_not_called()
    new_callback(types.SimpleNamespace(GetUsage=lambda _rid: [types.SimpleNamespace(eventId=22)]))
    panel._drain_usage_results()
    assert not panel._usage_pending
    assert panel._workspace_event_ids == {"one": [22]}
    panel._rerender_tree_keep_selection.assert_called_once()


def test_live_usage_finishing_during_capture_reopen_is_discarded_before_enqueue(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    first = _capture_binding(tmp_path)
    panel, provider, unused_protocol, unused_type = _scoped_panel(tmp_path, first)
    panel.ctx.GetResources.return_value = [types.SimpleNamespace(resourceId=77)]
    workspace = {"workspaceId": "one", "resourceId": 77, "capturePath": first["capturePath"]}
    panel._workspaces["one"] = workspace
    panel._request_live_usage([workspace])
    callback = panel.ctx.Replay.return_value.AsyncInvoke.call_args.args[0]
    started, resume = threading.Event(), threading.Event()

    def get_usage(_rid):
        started.set()
        assert resume.wait(timeout=5)
        return [types.SimpleNamespace(eventId=11)]

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(callback, types.SimpleNamespace(GetUsage=get_usage))
        try:
            assert started.wait(timeout=5)
            provider["binding"] = _capture_binding(tmp_path, session="2" * 32)
            panel.capture_changed(provider["binding"])
        finally:
            resume.set()
        future.result(timeout=5)
    assert panel._usage_results.empty()
    assert panel._usage_cache == {} and panel._workspace_event_ids == {}
    panel._rerender_tree_keep_selection.assert_not_called()


@pytest.mark.parametrize("change", ["removed", "resource", "path"])
def test_live_usage_result_requires_workspace_to_still_bind_requested_resource(tmp_path, change):
    first = _capture_binding(tmp_path)
    panel, unused_provider, unused_protocol, unused_type = _scoped_panel(tmp_path, first)
    panel.ctx.GetResources.return_value = [types.SimpleNamespace(resourceId=77)]
    workspace = {"workspaceId": "one", "resourceId": 77, "capturePath": first["capturePath"]}
    panel._workspaces["one"] = workspace
    panel._request_live_usage([workspace])
    callback = panel.ctx.Replay.return_value.AsyncInvoke.call_args.args[0]
    callback(types.SimpleNamespace(GetUsage=lambda _rid: [types.SimpleNamespace(eventId=11)]))
    if change == "removed":
        panel._workspaces.clear()
    elif change == "resource":
        workspace["resourceId"] = 88
    else:
        workspace["capturePath"] = str(tmp_path / "another.rdc")
    panel._drain_usage_results()
    assert not panel._usage_pending
    assert panel._usage_cache == {} and panel._workspace_event_ids == {}
    panel._rerender_tree_keep_selection.assert_not_called()


def test_event_metadata_reader_stays_bounded_even_when_file_grows_after_stat(tmp_path):
    module = _load_without_qt()
    path = tmp_path / "catalog.json"
    path.write_bytes(b'{"uses": []}')
    assert module._read_bounded_json(str(path), 32) == {"uses": []}
    for invalid in (b"[]", b"{broken", b"\xff", b'{"padding":"' + b"x" * 64 + b'"}'):
        path.write_bytes(invalid)
        with mock.patch.object(module.os.path, "getsize", return_value=1):
            assert module._read_bounded_json(str(path), 32) is None


def test_refresh_returns_before_blocked_loader_and_only_ui_callback_publishes(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    panel, unused_provider, protocol, unused_type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    ui_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()

    def load(*unused, **unused_keywords):
        assert threading.get_ident() != ui_thread
        entered.set()
        assert release.wait(5)
        return {"workspaces": [], "watchedPaths": []}

    with mock.patch.object(protocol, "load_console_snapshot", side_effect=load) as loader:
        panel.refresh(True)
        loader.assert_not_called()
        owner, operation, callback = panel._snapshot_executor.jobs.pop()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(operation)
            try:
                assert entered.wait(5)
                assert panel._snapshot is None
                panel._render_tree.assert_not_called()
            finally:
                release.set()
            result = pending.result(timeout=5)
        assert panel._snapshot is None
        callback(result, "")
    assert panel._snapshot == {"workspaces": [], "watchedPaths": []}


def test_source_error_snapshot_stabilizes_but_file_changes_refresh_it(tmp_path):
    module = _load_without_qt()
    registry = tmp_path / "registry.json"
    source = tmp_path / "shader.hlsl"
    registry.write_text("{}", encoding="utf-8")
    source.write_text("float4 MainPS() { return 0; }", encoding="utf-8")
    watched = [str(registry), str(source)]
    workspace = {
        "workspaceId": "a" * 32,
        "workspacePath": str(tmp_path),
        "sourcePath": str(source),
        "authorizationId": "b" * 32,
        "editorEligible": True,
        "liveEdit": {"liveEditId": "c" * 32},
        "watch": {"watchId": "d" * 32},
    }
    snapshot = {"workspaces": [workspace], "watchedPaths": watched}
    request = {
        "stop": threading.Event(),
        "registryPath": str(registry),
        "projectSelectionPath": "",
        "captureBinding": None,
        "watchedPaths": watched,
        "previousSignature": None,
        "force": False,
        "documentIds": [],
    }
    protocol = module._shader_console_protocol
    with mock.patch.object(protocol, "load_console_snapshot", return_value=snapshot) as loader, \
            mock.patch.object(
                protocol, "load_authorized_source_document",
                side_effect=RuntimeError("live-edit session stopped")) as source_loader:
        first = module._build_workspace_snapshot_files(request)
        assert first["sourceErrors"] == {"a" * 32: "live-edit session stopped"}
        assert first["signature"] is not None

        second_request = dict(
            request, watchedPaths=first["watchedPaths"],
            previousSignature=first["signature"])
        assert module._build_workspace_snapshot_files(second_request) == {
            "unchanged": True}
        assert loader.call_count == 1 and source_loader.call_count == 1

        source.write_text(
            "float4 MainPS() { return float4(1, 0, 0, 1); }",
            encoding="utf-8")
        third = module._build_workspace_snapshot_files(second_request)
        assert not third.get("unchanged")
        assert loader.call_count == 2 and source_loader.call_count == 2


@pytest.mark.parametrize("read_failed", [False, True])
def test_uncertain_source_only_preview_save_reads_canonical_commit_not_frozen_preview(tmp_path, read_failed):
    panel, unused_provider, panel_type, workspace, source, opened = _canonical_display_fixture(tmp_path)
    opened.update(canonicalSourceUpdated=False, sourcePath=str(tmp_path / "ai/frozen/shader.hlsl"))
    panel.show_organization_preview(opened)
    key = opened["workspaceId"]
    document = panel._documents[key]
    submitted = "// saved despite timeout\n" + opened["text"]
    submitted_sha = hashlib.sha256(submitted.encode()).hexdigest()
    document.update(commitUnknown=True, saveInFlight=False, dirty=True,
                    pendingSave={"sourceOnly": True, "baseHash": opened["rawSha256"],
                                 "submittedHash": submitted_sha})
    canonical = Path(workspace["sourcePath"])
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(submitted, encoding="utf-8")
    info = canonical.stat()
    committed = dict(opened, sourcePath=str(canonical), rawSha256=submitted_sha,
                     text=submitted, modifiedNs=info.st_mtime_ns, size=info.st_size,
                     canonicalSourceUpdated=True)
    namespace = panel.refresh.__func__.__globals__
    protocol = namespace["_shader_console_protocol"]
    with mock.patch.object(protocol, "load_console_snapshot", return_value={"workspaces": [], "watchedPaths": []}), \
         mock.patch.object(protocol, "reload_saved_shader_source_preview", return_value=committed,
                           side_effect=RuntimeError("source temporarily unreadable") if read_failed else None) as reader:
        panel.refresh(True)
        unused_owner, operation, callback = panel._snapshot_executor.jobs.pop()
        result = operation()
        assert reader.call_args.args[0]["readCanonicalSource"] is True
        callback(result, "")
    methods = ast.Module(body=[copy.deepcopy(_method(_parse(MODULE_PATH), "_refresh_document_from_disk"))], type_ignores=[])
    exec(compile(ast.fix_missing_locations(methods), str(MODULE_PATH), "exec"), namespace)
    panel._promote_submitted_save = mock.Mock()
    panel._resolve_uncommitted_save = mock.Mock()
    namespace["_refresh_document_from_disk"](panel, key)
    if read_failed:
        assert key not in panel._source_documents
        panel._promote_submitted_save.assert_not_called()
        assert document["commitUnknown"] is True
    else:
        metadata = panel._promote_submitted_save.call_args.args[1]
        assert metadata["sourcePath"] == str(canonical) and metadata["rawSha256"] == submitted_sha
    panel._resolve_uncommitted_save.assert_not_called()


def test_source_error_snapshot_retries_with_bounded_backoff(tmp_path):
    panel, unused_provider, unused_protocol, unused_type = _scoped_panel(
        tmp_path, _capture_binding(tmp_path))
    failed = {
        "snapshot": {"workspaces": []},
        "sources": {},
        "sourceErrors": {"stopped": "live-edit session stopped"},
        "staleSources": [],
        "eventIds": {},
        "watchedPaths": [],
        "existingPaths": [],
        "signature": ("stable",),
    }
    with mock.patch.object(time, "monotonic", return_value=100.0):
        panel._apply_workspace_snapshot(failed, panel._capture_binding)
    assert panel._snapshot_error_retry_at == 130.0
    assert panel._snapshot_error_retry_delay == 60.0

    method_globals = panel.refresh.__func__.__globals__
    original_builder = method_globals["_build_workspace_snapshot"]
    method_globals["_build_workspace_snapshot"] = lambda request: request
    try:
        with mock.patch.object(time, "monotonic", return_value=130.0):
            panel.refresh()
        unused_owner, operation, unused_callback = panel._snapshot_executor.jobs.pop()
        assert operation()["force"] is True
    finally:
        method_globals["_build_workspace_snapshot"] = original_builder

    panel._snapshot_running = False
    recovered = dict(failed, sourceErrors={}, signature=("recovered",))
    with mock.patch.object(time, "monotonic", return_value=200.0):
        panel._apply_workspace_snapshot(recovered, panel._capture_binding)
    assert panel._snapshot_error_retry_at == 0.0
    assert panel._snapshot_error_retry_delay == 30.0


def test_forced_refreshes_coalesce_without_starving_current_result(tmp_path):
    panel, unused_provider, protocol, unused_type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    with mock.patch.object(protocol, "load_console_snapshot", return_value={
            "workspaces": [], "watchedPaths": []}):
        panel.refresh(True)
        for unused in range(100):
            panel.refresh(True)
        assert len(panel._snapshot_executor.jobs) == 1
        _finish_refresh(panel)
    assert panel._snapshot is not None
    assert len(panel._snapshot_executor.jobs) == 1


def test_click_after_snapshot_started_survives_older_read_but_real_failure_finishes(tmp_path):
    panel, unused_provider, unused_protocol, unused_type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    panel._restore_default_document = False
    panel._request_live_usage = mock.Mock()
    workspace = {'workspaceId': 'late', 'editorEligible': True, 'stage': 'fragment',
                 'sourcePath': str(tmp_path / 'shader.hlsl'),
                 'capturePath': panel._capture_binding['capturePath']}
    options = {'select': True, 'quiet': False}
    panel._pending_open = {'late': options}
    result = dict(snapshot={'workspaces': [workspace]}, sources={}, sourceErrors={}, staleSources=[],
                  eventIds={}, watchedPaths=[], existingPaths=[], signature=('stable',))
    panel._apply_workspace_snapshot(result, panel._capture_binding)
    assert panel._pending_open == {'late': options}
    assert panel._snapshot_again is True
    result['sourceErrors'] = {'late': 'real read failure'}
    panel._apply_workspace_snapshot(result, panel._capture_binding)
    assert panel._pending_open == {}
    assert panel._last_open_error == 'real read failure'


@pytest.mark.parametrize("change", ["textRevision", "documentToken", "rawSha256", "saveInFlight"])
def test_late_source_cache_never_overwrites_new_edit_or_save_state(tmp_path, change):
    panel, unused_provider, unused_protocol, unused_type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    document = {"documentToken": 1, "textRevision": 0, "rawSha256": "a" * 64}
    panel._documents["shader"] = document
    panel._workspaces["shader"] = {"authorizationId": "b" * 32, "liveEdit": {"liveEditId": "live"}}
    panel._source_documents["shader"] = {
        "captureBinding": panel._capture_binding, "authorizationId": "b" * 32, "liveEditId": "live"}
    panel._source_request_states["shader"] = (
        1, 0, "a" * 64, None, False, False, None)
    assert panel._cached_source_document("shader") is not None
    document[change] = True if change == "saveInFlight" else "changed"
    assert panel._cached_source_document("shader") is None


def _dirty_document(panel, key="old-workspace"):
    editor = mock.Mock()
    editor.GetText.return_value = "unsaved HLSL buffer"
    document = {"workspaceId": key, "editor": editor, "dirty": True,
                "available": True, "captureBinding": panel._capture_binding,
                "saveQueued": True, "documentToken": 7}
    panel._documents[key] = document
    panel._workspaces[key] = {"workspaceId": key, "autoApplyReady": True}
    panel.tab_bar.ids.append(key)
    return document


def test_no_capture_refresh_never_reads_old_registry_or_restores_project_documents(tmp_path):
    panel, _provider, protocol, _type = _scoped_panel(tmp_path, None)
    panel._pinned = ["historical-library", "historical-workspace"]
    panel._restore_open = list(panel._pinned)
    with mock.patch.object(protocol, "_load_generic_console_entries", return_value={
            "entries": [], "errors": [], "watchedPaths": []}), \
            mock.patch.object(protocol, "_read_json", side_effect=AssertionError("unscoped registry read")), \
            mock.patch.object(protocol, "load_project_shader_library_snapshot") as library:
        panel.refresh(force=True)
        _finish_refresh(panel)
    assert panel._snapshot["captureBinding"] is None
    assert panel._snapshot["workspaces"] == []
    assert panel._workspaces == {}
    assert panel.tab_bar.count() == 0
    panel._open_workspace.assert_not_called()
    library.assert_not_called()


def test_capture_changed_hides_old_tabs_disables_native_navigation_and_keeps_dirty_buffer(tmp_path):
    panel, provider, _protocol, _type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    document = _dirty_document(panel)
    provider["binding"] = None
    panel.capture_changed(None)
    assert panel.tab_bar.count() == 0 and not panel.tab_bar.blocked
    assert panel._workspaces == {} and panel._document_model(document["workspaceId"]) is None
    assert panel._documents[document["workspaceId"]] is document
    assert document["dirty"] is True and document["available"] is False
    assert document["saveQueued"] is False
    assert document["editor"].GetText() == "unsaved HLSL buffer"
    document["editor"].SetText.assert_not_called()
    document["editor"].SetReadOnly.assert_called_with(True)
    document["editor"].SetNavigationEnabled.assert_called_with(False)
    panel.editor_stack.setCurrentWidget.assert_called_with(panel.empty_page)
    panel.flow_graph.clear_graph.assert_called_with(graph_key="shader-workspace-empty")


def test_snapshot_returning_after_capture_switch_is_not_published(tmp_path):
    first = _capture_binding(tmp_path)
    second = _capture_binding(tmp_path, "2" * 32)
    panel, provider, protocol, _type = _scoped_panel(tmp_path, first)
    document = _dirty_document(panel)

    def late_snapshot(*_args, **kwargs):
        assert kwargs["capture_binding"] == panel._capture_binding
        provider["binding"] = second
        return {"workspaces": [{"workspaceId": "old-result", "editorEligible": True}]}

    with mock.patch.object(protocol, "load_console_snapshot", side_effect=late_snapshot):
        panel.refresh(force=True)
        _finish_refresh(panel)
    assert panel._snapshot is None and panel._workspaces == {}
    assert panel._capture_binding == protocol.normalize_capture_binding(second)
    assert panel.tab_bar.count() == 0
    assert document["dirty"] is True and not document["available"]
    panel._render_tree.assert_not_called()
    panel._open_workspace.assert_not_called()


def test_authorized_document_returning_after_capture_close_never_creates_native_editor(tmp_path):
    panel, provider, protocol, panel_type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    panel._workspaces["current"] = {"workspaceId": "current", "authorizationId": "a" * 32}

    def late_document(*_args, **kwargs):
        assert kwargs["capture_binding"] == panel._capture_binding
        provider["binding"] = None
        return {"sourceText": "old HLSL"}

    with mock.patch.object(protocol, "load_authorized_source_document", side_effect=late_document):
        assert panel_type._open_workspace(panel, "current", quiet=True) is False
    assert panel._documents == {} and panel.tab_bar.count() == 0
    panel.ctx.CreateShaderCodeEditor.assert_not_called()


def test_restore_tabs_only_uses_current_capture_authorized_projection(tmp_path):
    panel, _provider, protocol, _type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    old = _dirty_document(panel)
    panel._restore_open = ["old-workspace", "current"]
    panel._pinned = ["historical-library"]
    snapshot = {"workspaces": [
        {"workspaceId": "current", "authorizationId": "a" * 32, "editorEligible": True},
        {"workspaceId": "rejected", "editorEligible": False}], "watchedPaths": []}
    with mock.patch.object(protocol, "load_console_snapshot", return_value=snapshot) as loader:
        panel.refresh(force=True)
        _finish_refresh(panel)
    loader.assert_called_once_with(panel.registry_path, capture_binding=panel._capture_binding,
                                   include_console_events=False)
    assert set(panel._workspaces) == {"current", "old-workspace"}
    assert panel._workspaces['old-workspace']['metadataUnavailable'] is True
    assert panel.tab_bar.ids == ["old-workspace", "current"]
    assert [call.args[0] for call in panel._open_workspace.call_args_list] == ["current"]
    assert old["dirty"] is True and old["available"] is False
    old["editor"].SetNavigationEnabled.assert_called_with(False)


def test_save_rejects_changed_capture_binding_even_before_the_next_ui_refresh(tmp_path):
    panel, provider, _protocol, _type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    document = _dirty_document(panel)
    provider["binding"] = _capture_binding(tmp_path, "2" * 32)
    assert panel._save_document(document["workspaceId"]) is False
    assert panel._save_tasks.empty()
    assert document["dirty"] is True
    document["editor"].SetSavePoint.assert_not_called()


def test_saved_editor_remains_visible_when_metadata_refresh_cannot_rebind_it(tmp_path):
    panel, unused_provider, unused_protocol, unused_type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    document = _dirty_document(panel)
    document.update(dirty=False, baselineEditorText=document['editor'].GetText())
    result = dict(snapshot={'workspaces': []}, sources={}, sourceErrors={}, eventIds={},
                  watchedPaths=[], existingPaths=[], signature=('unchanged',))
    panel._apply_workspace_snapshot(result, panel._capture_binding)
    assert document['workspaceId'] in panel.tab_bar.ids
    assert panel._workspaces[document['workspaceId']]['metadataUnavailable']
    assert not document['available'] and not document['readOnly']
    document['editor'].SetReadOnly.assert_called_with(False)
    document['editor'].SetText.assert_not_called()


def test_closing_panel_still_prompts_for_dirty_buffers_hidden_by_capture_change(tmp_path):
    panel, provider, _protocol, _type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    document = _dirty_document(panel)
    provider["binding"] = None
    panel.capture_changed(None)
    panel._native_editors_released = False
    panel._shutting_down = False
    panel._confirm_close_document = mock.Mock(return_value=False)
    panel._release_native_editors = mock.Mock()
    assert panel._confirm_panel_close() is False
    panel._confirm_close_document.assert_called_once_with(document)
    panel._release_native_editors.assert_not_called()


def _reopen_dirty_capture(tmp_path):
    panel, provider, protocol, panel_type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    document = _dirty_document(panel)
    document["sourcePath"] = str(tmp_path / "managed" / "source.hlsl")
    document["authorizationId"] = "old-authorization"
    panel._restore_open = [document["workspaceId"]]
    provider["binding"] = None
    panel.capture_changed(None)
    provider["binding"] = _capture_binding(tmp_path, "2" * 32)
    workspace = {"workspaceId": document["workspaceId"], "editorEligible": True,
                 "authorizationId": "fresh-authorization", "autoApplyReady": True,
                 "sourcePath": document["sourcePath"],
                 "liveEdit": {"liveEditId": "fresh-live-edit"}}

    def insert_tab(key, _document):
        panel.tab_bar.ids.append(key)
        return len(panel.tab_bar.ids) - 1

    panel._insert_tab = mock.Mock(side_effect=insert_tab)
    panel._open_workspace = types.MethodType(panel_type._open_workspace, panel)
    return panel, provider, protocol, document, workspace


def test_same_capture_and_managed_source_resume_dirty_buffer_with_fresh_host_authorization(tmp_path):
    panel, _provider, protocol, document, workspace = _reopen_dirty_capture(tmp_path)
    snapshot = {"workspaces": [workspace], "watchedPaths": []}
    with mock.patch.object(protocol, "load_console_snapshot", return_value=snapshot):
        panel.refresh(force=True)
        _finish_refresh(panel)
    assert panel.tab_bar.ids == [document["workspaceId"]]
    assert document["available"] is True and document["dirty"] is True
    assert document["captureBinding"] == panel._current_capture_binding()
    assert document["authorizationId"] == "fresh-authorization"
    document["editor"].SetNavigationEnabled.assert_called_with(True)
    document["editor"].SetText.assert_not_called()
    assert document["editor"].GetText() == "unsaved HLSL buffer"
    panel.ctx.CreateShaderCodeEditor.assert_not_called()

    panel._document_source_text = lambda item: item["editor"].GetText()
    panel._next_save_token = 0
    panel._sync_document_dirty = mock.Mock()
    panel.save_result_timer = mock.Mock()
    panel._save_state = mock.Mock()
    assert panel._save_document(document["workspaceId"]) is True
    task = panel._save_tasks.get_nowait()
    assert task["sourceText"] == "unsaved HLSL buffer"
    assert task["authorizationId"] == "fresh-authorization"
    assert task["liveEditId"] == "fresh-live-edit"
    assert task["manualApply"] is True


@pytest.mark.parametrize("dirty", [False, True])
def test_explicit_save_resumes_without_live_session_even_when_already_saved(tmp_path, dirty):
    panel, _provider, _protocol, document, workspace = _reopen_dirty_capture(tmp_path)
    workspace.update(autoApplyReady=False, liveEdit={}, watch={})
    document.update(available=True, readOnly=False, dirty=dirty,
                    captureBinding=panel._current_capture_binding())
    panel._workspaces[document["workspaceId"]] = workspace
    panel._document_source_text = lambda item: item["editor"].GetText()
    panel._next_save_token = 0
    panel._sync_document_dirty = mock.Mock()
    panel.save_result_timer = mock.Mock()
    panel._save_state = mock.Mock()
    assert panel._save_document(document["workspaceId"]) is True
    task = panel._save_tasks.get_nowait()
    assert task["manualApply"] is True
    assert task["liveEditId"] == ""
    client = mock.Mock()
    client.request_pending.return_value = False
    client.save_and_apply_source.return_value = {"rawSha256": task["submittedHash"]}
    tasks, results = queue.Queue(), queue.Queue()
    tasks.put(task)
    tasks.put(None)
    _load_without_qt()._save_worker_loop(client, tasks, results, threading.Event())
    assert results.get_nowait()["kind"] == "saved"
    client.save_and_apply_source.assert_called_once_with(task["workspacePath"],
        task["sourceWorkspaceId"], task["baseHash"], task["sourceText"], task["captureBinding"])
    client.save_source.assert_not_called()
    client.save.assert_not_called()


def test_restored_working_copy_clears_stale_readonly_without_losing_edits(tmp_path):
    panel, _provider, protocol, document, workspace = _reopen_dirty_capture(tmp_path)
    document["readOnly"] = True
    workspace.update(readOnly=True, autoApplyReady=False)
    with mock.patch.object(protocol, "load_console_snapshot", return_value={
            "workspaces": [workspace], "watchedPaths": []}):
        panel.refresh(force=True)
        _finish_refresh(panel)
    assert document["readOnly"] is False
    assert document["available"] is True and document["dirty"] is True
    assert document["editor"].GetText() == "unsaved HLSL buffer"
    document["editor"].SetReadOnly.assert_called_with(False)
    document["editor"].SetText.assert_not_called()


def test_transient_readonly_never_marks_unsaved_buffer_clean(tmp_path):
    panel, _provider, _protocol, _panel_type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    document = _dirty_document(panel)
    document.update(readOnly=True, baselineEditorText="saved source")
    panel._update_tab = mock.Mock()
    panel._current_workspace_id = lambda: document["workspaceId"]
    tree = _parse(MODULE_PATH)
    method = ast.Module(body=[copy.deepcopy(_method(tree, "_sync_document_dirty"))], type_ignores=[])
    namespace = {}
    exec(compile(ast.fix_missing_locations(method), str(MODULE_PATH), "exec"), namespace)
    namespace["_sync_document_dirty"](panel, document)
    assert document["dirty"] is True


@pytest.mark.parametrize("mismatch", ["capture-path", "source-path", "save-in-flight", "commit-unknown"])
def test_reopening_cannot_attach_dirty_buffer_to_another_source_or_pending_save(tmp_path, mismatch):
    panel, provider, protocol, document, workspace = _reopen_dirty_capture(tmp_path)
    original_binding = copy.deepcopy(document["captureBinding"])
    if mismatch == "capture-path":
        provider["binding"]["capturePath"] = str(tmp_path / "different.rdc")
    elif mismatch == "source-path":
        workspace["sourcePath"] = str(tmp_path / "another" / "source.hlsl")
    elif mismatch == "save-in-flight":
        document["saveInFlight"] = True
    else:
        document["commitUnknown"] = True
    with mock.patch.object(protocol, "load_console_snapshot", return_value={
            "workspaces": [workspace], "watchedPaths": []}):
        panel.refresh(force=True)
        _finish_refresh(panel)
    assert panel.tab_bar.count() == 0
    assert panel._documents[document["workspaceId"]] is document
    assert document["available"] is False and document["dirty"] is True
    assert document["captureBinding"] == original_binding
    assert document["authorizationId"] == "old-authorization"
    assert document["editor"].GetText() == "unsaved HLSL buffer"
    document["editor"].SetReadOnly.assert_called_with(True)
    document["editor"].SetNavigationEnabled.assert_called_with(False)
    document["editor"].SetText.assert_not_called()
    panel._insert_tab.assert_not_called()
    panel._refresh_document_from_disk.assert_not_called()
    assert panel._save_document(document["workspaceId"]) is False
    assert panel._save_tasks.empty()


def _tab_state_panel(tmp_path):
    panel, provider, protocol, panel_type = _scoped_panel(tmp_path, _capture_binding(tmp_path))
    panel.top = mock.Mock()
    panel._next_document_token = 0
    panel._loading_documents = set()
    panel._newline_style = lambda unused: "\n"
    panel._confirm_close_document = mock.Mock(return_value=True)
    panel._cached_source_document = lambda key: panel._source_documents.get(key)
    panel._stop_save_worker = mock.Mock()
    panel.poll_timer = mock.Mock()
    panel.save_result_timer = mock.Mock()
    panel.file_watcher = mock.Mock()
    panel.file_watcher.files.return_value = []
    panel.file_watcher.directories.return_value = []

    def native_editor(unused_parent):
        editor = mock.Mock()
        text = {"value": ""}
        editor.SetText.side_effect = lambda value: text.update(value=value)
        editor.GetText.side_effect = lambda: text["value"]
        return editor

    panel.ctx.CreateShaderCodeEditor.side_effect = native_editor
    panel._insert_tab = lambda key, unused: (panel.tab_bar.ids.append(key) or len(panel.tab_bar.ids) - 1)
    panel._open_workspace = types.MethodType(panel_type._open_workspace, panel)
    return panel, provider, protocol


def _restore_tab_preferences(panel, state):
    """Execute only the production constructor's plain saved-state assignments."""
    fields = {"_saved_tab_state", "_restore_default_document", "_closed_document_ids",
              "_pinned", "_restore_open", "_restore_active"}
    assignments = [node for node in ast.walk(_parse(MODULE_PATH))
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name) and target.value.id == "self"
            and target.attr in fields for target in node.targets)]
    # The constructor uniquely assigns these from `state`; later mutations
    # deliberately remain part of the real panel methods exercised below.
    assignments = [node for node in assignments if any(isinstance(child, ast.Name)
        and child.id == "state" for child in ast.walk(node)) or
        any(isinstance(target, ast.Attribute) and target.attr == "_closed_document_ids"
            for target in node.targets)]
    tree = ast.Module(body=[copy.deepcopy(node) for node in assignments], type_ignores=[])
    exec(compile(ast.fix_missing_locations(tree), str(MODULE_PATH), "exec"), {"self": panel, "state": state})


def _tab_snapshot(panel, tmp_path, ids, readable):
    workspaces, sources = [], {}
    for key in ids:
        workspace = {"workspaceId": key, "editorEligible": True, "autoApplyReady": True,
            "authorizationId": "a" * 32, "sourcePath": str(tmp_path / (key + ".hlsl")),
            "liveEdit": {"liveEditId": "b" * 32}, "entryPoint": "MainPS"}
        workspaces.append(workspace)
        if key in readable:
            sources[key] = {**workspace, "captureBinding": dict(panel._capture_binding),
                "text": "float4 MainPS() { return 1; }", "rawSha256": "c" * 64}
    result = {"snapshot": {"workspaces": workspaces}, "sources": sources,
        "sourceErrors": {}, "eventIds": {}, "watchedPaths": [], "existingPaths": [], "signature": ()}
    panel._source_documents = sources
    panel._apply_workspace_snapshot(result, panel._capture_binding)


def test_capture_close_then_panel_shutdown_preserves_actual_open_tab_choices(tmp_path):
    panel, provider, unused = _tab_state_panel(tmp_path)
    first, second = "1" * 32, "2" * 32
    panel._restore_open = [first, second]
    _tab_snapshot(panel, tmp_path, [first, second], [first, second])
    panel.tab_bar.setCurrentIndex(0)
    panel._save_state()
    before = panel._load_state()
    assert before["open"] == [first, second]
    assert before["active"] == first
    document = panel._documents[first]
    document["dirty"] = True
    document["editor"].SetText("unsaved buffer retained across capture close")
    provider["binding"] = None
    panel.capture_changed(None)
    assert panel.tab_bar.count() == 0
    panel.shutdown()
    assert panel._load_state() == before
    assert document["dirty"] is True and document["available"] is False
    assert document["editor"].GetText() == "unsaved buffer retained across capture close"
    panel.ctx.DestroyShaderCodeEditor.assert_not_called()


def test_saved_tabs_wait_for_late_sources_then_open_once_without_duplicate(tmp_path):
    panel, unused, unused_protocol = _tab_state_panel(tmp_path)
    ready, late = "1" * 32, "2" * 32
    panel._restore_open = [late, ready]
    _tab_snapshot(panel, tmp_path, [ready, late], [ready])
    assert panel.tab_bar.ids == [ready]
    assert panel._restore_open == [late]
    panel._save_state()
    assert set(panel._load_state()["open"]) == {ready, late}
    _tab_snapshot(panel, tmp_path, [ready, late], [ready, late])
    _tab_snapshot(panel, tmp_path, [ready, late], [ready, late])
    assert panel.tab_bar.ids == [ready, late]
    assert panel._restore_open == []
    assert panel.ctx.CreateShaderCodeEditor.call_count == 2


def test_explicit_close_cancels_late_restore_and_does_not_reopen_on_reappearing_model(tmp_path):
    panel, unused, unused_protocol = _tab_state_panel(tmp_path)
    ready, late = "1" * 32, "2" * 32
    panel._restore_open = [ready, late]
    _tab_snapshot(panel, tmp_path, [ready, late], [ready])
    assert panel._close_document(late)
    _tab_snapshot(panel, tmp_path, [ready], [ready])
    _tab_snapshot(panel, tmp_path, [ready, late], [ready, late])
    assert panel.tab_bar.ids == [ready]
    assert late not in panel._restore_open
    assert panel.ctx.CreateShaderCodeEditor.call_count == 1
    # An explicit later user/Agent show is still allowed.
    assert panel._open_workspace(late, quiet=True)
    assert panel.tab_bar.ids == [ready, late]


def test_user_closing_all_tabs_persists_empty_and_cancels_pending_restore(tmp_path):
    panel, provider, unused = _tab_state_panel(tmp_path)
    ready, late = "1" * 32, "2" * 32
    panel._restore_open = [ready, late]
    _tab_snapshot(panel, tmp_path, [ready, late], [ready])
    assert panel._close_document(ready)
    assert panel._load_state()["open"] == []
    assert panel._restore_open == []
    _tab_snapshot(panel, tmp_path, [ready, late], [ready, late])
    assert panel.tab_bar.ids == []
    provider["binding"] = None
    panel.capture_changed(None)
    panel.shutdown()
    assert panel._load_state()["open"] == []
    # A fresh panel respects an intentional empty saved list.
    fresh, unused, unused = _tab_state_panel(tmp_path)
    state = panel._load_state()
    _restore_tab_preferences(fresh, state)
    _tab_snapshot(fresh, tmp_path, [ready, late], [ready, late])
    assert fresh.tab_bar.count() == 0


def test_different_capture_does_not_inherit_pending_tabs_or_discard_dirty_buffers(tmp_path):
    panel, provider, unused = _tab_state_panel(tmp_path)
    first, late, other = "1" * 32, "2" * 32, "3" * 32
    panel._restore_open = [first, late]
    _tab_snapshot(panel, tmp_path, [first, late], [first])
    document = panel._documents[first]
    document["dirty"] = True
    document["editor"].SetText("old capture unsaved text")
    provider["binding"] = {**provider["binding"], "captureSessionId": "4" * 32,
        "capturePath": str(tmp_path / "another.rdc")}
    panel.capture_changed(provider["binding"])
    assert panel._restore_open == [] and panel._restore_active == ""
    _tab_snapshot(panel, tmp_path, [other], [other])
    assert panel.tab_bar.ids == [other]
    assert document["dirty"] is True and document["available"] is False
    assert document["editor"].GetText() == "old capture unsaved text"
    assert panel._load_state()["capturePath"].lower() == provider["binding"]["capturePath"].lower()


def test_legacy_empty_state_without_explicit_choice_still_opens_default_once(tmp_path):
    panel, unused, unused_protocol = _tab_state_panel(tmp_path)
    _restore_tab_preferences(panel, {"open": [], "pinned": [], "active": ""})
    ready = "1" * 32
    _tab_snapshot(panel, tmp_path, [ready], [ready])
    _tab_snapshot(panel, tmp_path, [ready], [ready])
    assert panel.tab_bar.ids == [ready]
    assert panel.ctx.CreateShaderCodeEditor.call_count == 1


if __name__ == "__main__":
    unittest.main()
