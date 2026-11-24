import ast
import importlib.util
import tempfile
import threading
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "project_launcher.py"
SPEC = importlib.util.spec_from_file_location("replaylab_project_launcher_test", MODULE_PATH)
project_launcher = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(project_launcher)


def _class_method_source(class_name, method_name):
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    class_node = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    method_node = next(
        node for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == method_name
    )
    return ast.get_source_segment(source, method_node)


def _project(active=True):
    return {
        "schema": "replaylab-project-workspace/v1",
        "schemaVersion": 1,
        "workspaceId": "1" * 32,
        "name": "Example",
        "state": "bound",
        "projectRoot": str(Path("C:/ReplayLab/projects/example")),
        "scope": {
            "gameId": "game",
            "buildId": "build",
            "platform": "windows-x64",
            "apiFamily": "d3d11",
        },
        "assetRoot": None,
        "active": active,
        "captureLibrary": {
            "schema": "replaylab-project-capture-library/v1",
            "schemaVersion": 1,
            "workspaceId": "1" * 32,
            "count": 1,
            "captures": [
                {
                    "captureRefId": "2" * 64,
                    "path": str(Path("C:/captures/frame.rdc")),
                    "displayName": "frame.rdc",
                    "available": True,
                    "sizeBytes": 123,
                    "modifiedTimeNs": 456,
                    "thumbnail": {"state": "placeholder"},
                    "contentIdentity": {
                        "schema": "replaylab-rdc-content-identity/v1",
                        "algorithm": "sha256-full",
                        "sha256": "4" * 64,
                        "sizeBytes": 123,
                        "modifiedTimeNs": 456,
                        "fileIdentity": "1:2",
                    },
                }
            ],
        },
    }


class ReplayLabProjectLauncherTests(unittest.TestCase):
    def test_main_window_moves_destructive_actions_to_safe_context_menus(self):
        launcher_init = _class_method_source(
            "ReplayLabProjectLauncher", "__init__")
        new_project_init = _class_method_source("NewProjectDialog", "__init__")
        project_menu = _class_method_source(
            "ReplayLabProjectLauncher", "_show_project_context_menu")
        capture_menu = _class_method_source(
            "ReplayLabProjectLauncher", "_show_capture_context_menu")

        self.assertNotIn('QPushButton("Remove Project")', launcher_init)
        self.assertNotIn('QPushButton("Refresh")', launcher_init)
        self.assertNotIn('QPushButton("Remove from Project")', launcher_init)
        self.assertIn('"Remove Selected"', new_project_init)
        self.assertIn("QCoreApplication.translate", new_project_init)

        self.assertIn("self._show_project_context_menu", launcher_init)
        self.assertIn("self._show_capture_context_menu", launcher_init)
        self.assertIn("self.remove_project_shortcut", launcher_init)
        self.assertIn("self.remove_capture_shortcut", launcher_init)
        self.assertEqual(2, launcher_init.count("QtGui.QKeySequence.Delete"))
        self.assertGreaterEqual(
            launcher_init.count("QtCore.Qt.WidgetWithChildrenShortcut"), 2)

        self.assertIn('"Refresh Projects"', project_menu)
        self.assertIn('"Remove Project"', project_menu)
        self.assertIn("QCoreApplication.translate", project_menu)
        self.assertIn("self.project_list.itemAt(position)", project_menu)
        self.assertIn("item is not None", project_menu)
        self.assertIn("self.refresh()", project_menu)
        self.assertIn("self._remove_selected_project()", project_menu)

        self.assertIn('"Open Selected Frame"', capture_menu)
        self.assertIn('"Remove from Project"', capture_menu)
        self.assertIn("QCoreApplication.translate", capture_menu)
        self.assertIn("self.capture_list.itemAt(position)", capture_menu)
        self.assertIn("self.capture_list.clearSelection()", capture_menu)
        self.assertIn("QtCore.QItemSelectionModel.NoUpdate", capture_menu)
        self.assertIn("self.capture_list.selectedItems()", capture_menu)
        self.assertIn("item is not None", capture_menu)
        self.assertIn("self._remove_selected_captures()", capture_menu)

    def test_open_progress_hides_launcher_for_native_load_and_recovers_on_error(self):
        launcher_init = _class_method_source(
            "ReplayLabProjectLauncher", "__init__")
        set_busy = _class_method_source(
            "ReplayLabProjectLauncher", "_set_busy")
        open_capture = _class_method_source(
            "ReplayLabProjectLauncher", "_open_capture_path")

        self.assertIn("QtWidgets.QProgressBar()", launcher_init)
        self.assertIn("self.operation_progress.setRange(0, 0)", launcher_init)
        self.assertIn("self.operation_progress.setVisible(self._busy)", set_busy)
        self.assertLess(
            open_capture.index("self.hide()"),
            open_capture.index("self.open_capture(path)"))
        self.assertLess(
            open_capture.index("self.open_capture(path)"),
            open_capture.index("self.close()"))
        self.assertIn("self.show()", open_capture)
        self.assertIn("self.raise_()", open_capture)
        self.assertIn("self.activateWindow()", open_capture)
        self.assertIn("self._set_busy(False)", open_capture)

    def test_post_load_restore_progress_uses_the_status_bar_and_is_ui_polled(self):
        launcher_init = _class_method_source(
            "ReplayLabProjectLauncher", "__init__")
        open_capture = _class_method_source(
            "ReplayLabProjectLauncher", "_open_capture_path")
        start_restore = _class_method_source(
            "ReplayLabProjectLauncher", "_start_capture_restore")
        restore_finished = _class_method_source(
            "ReplayLabProjectLauncher", "_capture_restore_finished")
        task_init = _class_method_source(
            "ReplayLabRestoreStatusTask", "__init__")
        install_status = _class_method_source(
            "ReplayLabRestoreStatusTask", "_install_status_widget")
        remove_status = _class_method_source(
            "ReplayLabRestoreStatusTask", "_remove_status_widget")
        task_start = _class_method_source(
            "ReplayLabRestoreStatusTask", "start")
        publish_progress = _class_method_source(
            "ReplayLabRestoreStatusTask", "publish_progress")
        drain_progress = _class_method_source(
            "ReplayLabRestoreStatusTask", "_drain_progress")
        terminal_failure = _class_method_source(
            "ReplayLabRestoreStatusTask", "_show_terminal_failure")
        abandon_restore = _class_method_source(
            "ReplayLabRestoreStatusTask", "abandon")
        abandon_capture = _class_method_source(
            "ReplayLabProjectLauncher", "abandon_capture_restore")
        launcher_finished = _class_method_source(
            "ReplayLabProjectLauncher", "_dialog_finished")

        self.assertIn("restore_capture=None", launcher_init)
        self.assertIn("self.restore_capture = restore_capture", launcher_init)
        self.assertLess(
            open_capture.index("self.open_capture(path)"),
            open_capture.index("self._start_capture_restore(project_root, path)"))
        self.assertIn("if self.restore_capture is None", open_capture)
        self.assertIn("open_error = _capture_open_error(opened)", open_capture)
        self.assertIn('getattr(parent, "statusBar"', start_restore)
        self.assertIn('find_child(', start_restore)
        self.assertIn('QtWidgets.QStatusBar, "statusBar"', start_restore)
        self.assertIn("ReplayLabRestoreStatusTask(", start_restore)
        self.assertIn("task.start()", start_restore)
        self.assertNotIn(".show()", start_restore)
        self.assertNotIn(".raise_()", start_restore)
        self.assertNotIn(".activateWindow()", start_restore)

        self.assertIn("QtCore.QObject.__init__(self, parent)", task_init)
        self.assertIn("self._progress_timer.setInterval(25)", task_init)
        self.assertIn("queue.Queue(MAX_RESTORE_PROGRESS_EVENTS)", task_init)
        self.assertIn("QtWidgets.QLabel(widget)", install_status)
        self.assertIn("QtCore.Qt.PlainText", install_status)
        self.assertIn("QtWidgets.QSizePolicy.Ignored", install_status)
        self.assertIn("QtWidgets.QProgressBar(widget)", install_status)
        self.assertIn("add_widget(widget, 1)", install_status)
        self.assertNotIn(".showMessage(", install_status)
        self.assertIn("remove_widget(widget)", remove_status)
        self.assertIn("widget.deleteLater()", remove_status)
        self.assertIn('"phase": "restore-dispatch"', task_start)
        self.assertLess(
            task_start.index('"phase": "restore-dispatch"'),
            task_start.index("self.executor.submit("))
        self.assertIn("self.executor.submit(", task_start)
        self.assertIn("self.publish_progress", task_start)
        self.assertIn("self._progress_events.put_nowait(progress)", publish_progress)
        self.assertIn("self._progress_events.get_nowait()", drain_progress)
        self.assertIn("latest[\"current\"]", drain_progress)
        self.assertIn("_restore_status_text(latest)", drain_progress)
        self.assertIn('getattr(self.status_bar, "showMessage"', terminal_failure)
        self.assertIn("show_message(text, 30000)", terminal_failure)
        self.assertNotIn("QMessageBox", terminal_failure)
        self.assertIn("self._progress_timer.stop()", abandon_restore)
        self.assertIn("self._remove_status_widget()", abandon_restore)
        self.assertIn("self.deleteLater()", abandon_restore)
        self.assertIn("restore_task.abandon()", abandon_capture)
        self.assertIn("self.async_executor.cancel_owner", abandon_capture)
        self.assertIn("self.close()", abandon_capture)
        self.assertIn("restore_task.abandon()", launcher_finished)

        self.assertNotIn("QtWidgets.QMessageBox", restore_finished)
        self.assertNotIn("exec_", restore_finished)
        self.assertIn("Background project restore failed", restore_finished)
        self.assertIn("self.close()", restore_finished)

    def test_restore_progress_contract_is_bounded_and_formats_counts(self):
        value = project_launcher._normalise_restore_progress({
            "phase": "hash",
            "message": "Checking shared Shader hashes",
            "current": 7,
            "total": 12,
        })
        self.assertEqual("hash", value["phase"])
        self.assertEqual(7, value["current"])
        self.assertEqual(12, value["total"])
        self.assertEqual(
            "Checking shared Shader hashes (7 / 12)",
            project_launcher._restore_progress_text(value))

        item_value = project_launcher._normalise_restore_progress({
            "phase": "export-shader-bindings",
            "message": "",
            "current": 3,
            "total": 9,
            "item": "EID 418",
        })
        self.assertEqual("EID 418", item_value["item"])
        self.assertEqual(
            "Exporting Shader bindings (3 / 9) · Current item: EID 418",
            project_launcher._restore_progress_text(item_value))
        self.assertEqual(
            "Project recovery: Exporting Shader bindings "
            "(3 / 9) · Current item: EID 418",
            project_launcher._restore_status_text(item_value))
        self.assertNotIn("\n", project_launcher._restore_status_text(item_value))

        expected_local_phases = {
            "restore-dispatch": "Scheduling project recovery",
            "client-validate": "Validating the project recovery request",
            "host-state": "Checking the project service state",
            "host-health": "Verifying the project service identity",
            "restore-submit": "Starting the project recovery job",
            "restore-queued": "Project recovery is queued",
            "restore-running": "The project service is running project recovery",
            "result-validate": "Validating the project recovery result",
            "index-required": (
                "A unique Shader resource index is required before project data can be restored"
            ),
            "resource-inventory": "Indexing unique Shader resources",
            "resource-match": "Matching exact project Shader artifacts",
            "resource-anchor": "Validating one representative EID per Shader",
        }
        for phase, expected in expected_local_phases.items():
            with self.subTest(phase=phase):
                self.assertEqual(
                    expected,
                    project_launcher._restore_progress_text({
                        "phase": phase, "message": "", "current": 0,
                        "total": 0,
                    }))

        clamped = project_launcher._normalise_restore_progress({
            "phase": "apply", "message": "Applying", "current": 20, "total": 3,
        })
        self.assertEqual(3, clamped["current"])
        with self.assertRaisesRegex(
                project_launcher.ProjectLauncherError, "must be an object"):
            project_launcher._normalise_restore_progress("not-an-object")
        with self.assertRaisesRegex(
                project_launcher.ProjectLauncherError, "counts"):
            project_launcher._normalise_restore_progress({
                "phase": "hash", "message": "Checking", "current": -1, "total": 2,
            })
        with self.assertRaisesRegex(
                project_launcher.ProjectLauncherError, "text"):
            project_launcher._normalise_restore_progress({
                "phase": "hash", "message": "Checking", "current": 1,
                "total": 2, "item": "x" * 1025,
            })

    def test_explicit_open_and_restore_failures_are_distinguished(self):
        self.assertEqual("", project_launcher._capture_open_error(None))
        self.assertEqual("", project_launcher._capture_open_error({"opened": "frame.rdc"}))
        self.assertIn("not opened", project_launcher._capture_open_error(False))
        self.assertEqual(
            "native load failed",
            project_launcher._capture_open_error({"error": "native load failed"}))
        self.assertEqual(
            "RenderDoc did not finish opening the selected RDC",
            project_launcher._capture_open_error({
                "errorId": "renderdoc-open-incomplete",
            }))
        self.assertEqual(
            "future-open-failure: native diagnostic",
            project_launcher._capture_open_error({
                "errorId": "future-open-failure",
                "error": {"message": "native diagnostic"},
            }))

        self.assertEqual("", project_launcher._restore_result_error({"restored": True}))
        self.assertEqual(
            "restore failed",
            project_launcher._restore_result_error(None, "restore failed"))
        self.assertEqual(
            "hash mismatch",
            project_launcher._restore_result_error({
                "error": {"message": "hash mismatch"},
            }))
        self.assertEqual(
            "The selected RDC is no longer the active RenderDoc capture",
            project_launcher._restore_result_error({
                "errorId": "capture-no-longer-active",
            }))
        self.assertEqual(
            "The active capture changed while project data was being restored",
            project_launcher._restore_result_error({
                "errorId": "capture-changed-during-restore",
            }))
        self.assertEqual(
            "This qrenderdoc build cannot restore project data after opening an RDC",
            project_launcher._restore_result_error({
                "errorId": "project-restore-client-unavailable",
            }))
        self.assertEqual(
            "The running project service does not match this qrenderdoc build. Close "
            "qrenderdoc and Codex, deploy the matching runtime, then restart "
            "qrenderdoc.",
            project_launcher._restore_result_error({
                "errorId": "host-runtime-incompatible",
            }))
        action_required = {
            "completed": False,
            "outcome": "action-required",
            "reasonId": "catalog-index-required",
        }
        self.assertTrue(
            project_launcher._restore_action_required(action_required)
        )
        self.assertEqual(
            "", project_launcher._restore_result_error(action_required)
        )
        self.assertEqual(
            "future-action",
            project_launcher._restore_result_error({
                "completed": False,
                "outcome": "action-required",
                "reasonId": "future-action",
            }),
        )
        self.assertEqual(
            "future-restore-failure",
            project_launcher._restore_result_error({
                "errorId": "future-restore-failure",
            }))
        self.assertEqual(
            "future-restore-failure: restore diagnostic",
            project_launcher._restore_result_error({
                "errorId": "future-restore-failure",
                "error": {"message": "restore diagnostic"},
            }))

    def test_qt_format_does_not_reprocess_dynamic_percent_tokens(self):
        self.assertEqual(
            "A|B%1|C%20",
            project_launcher._qt_format("%1|%2|%3", "A", "B%1", "C%20"))
        self.assertEqual(
            "%4 remains",
            project_launcher._qt_format("%4 remains", "A", "B", "C"))

    def test_thumbnail_reason_ids_localize_stable_outcomes(self):
        unavailable = project_launcher._thumbnail_result(
            "missing", "legacy English", reason_id="rdc-unavailable")
        self.assertEqual(
            "RDC file is unavailable",
            project_launcher._thumbnail_reason_text(unavailable))
        failed = project_launcher._thumbnail_result(
            "error", "legacy English", reason_id="read-failed",
            reason_detail="decoder status")
        self.assertEqual(
            "Embedded thumbnail read failed: decoder status",
            project_launcher._thumbnail_reason_text(failed))
        diagnostic = project_launcher._thumbnail_result(
            "error", "third-party diagnostic")
        self.assertEqual(
            "third-party diagnostic",
            project_launcher._thumbnail_reason_text(diagnostic))
        future = project_launcher._thumbnail_result(
            "error", reason_id="future-thumbnail-failure",
            reason_detail="decoder diagnostic")
        self.assertEqual(
            "future-thumbnail-failure: decoder diagnostic",
            project_launcher._thumbnail_reason_text(future))

    def test_capture_updates_replace_only_the_current_project_snapshot(self):
        current = project_launcher.validate_project(_project(active=True))
        updated = dict(current)
        updated["active"] = False
        updated["captures"] = []

        projects, row = project_launcher.replace_project_snapshot(
            [current], updated)

        self.assertEqual(0, row)
        self.assertTrue(projects[0]["active"])
        self.assertEqual([], projects[0]["captures"])
        self.assertEqual(1, len(current["captures"]))
        self.assertFalse(updated["active"])
        with self.assertRaisesRegex(
                project_launcher.ProjectLauncherError, "not present"):
            project_launcher.replace_project_snapshot(
                [current], {"workspaceId": "f" * 32})

        add_captures = _class_method_source(
            "ReplayLabProjectLauncher", "_add_captures")
        remove_captures = _class_method_source(
            "ReplayLabProjectLauncher", "_remove_selected_captures")
        self.assertIn("self._apply_updated_project(updated)", add_captures)
        self.assertIn("self._apply_updated_project(updated)", remove_captures)
        self.assertNotIn("self.refresh(", add_captures)
        self.assertNotIn("self.refresh(", remove_captures)

    def test_async_callback_registry_discards_results_after_owner_close(self):
        registry = project_launcher.AsyncCallbackRegistry()
        delivered = []
        first = registry.register("window-a", delivered.append, "first")
        second = registry.register("window-b", delivered.append, "second")
        self.assertEqual(2, len(registry))
        registry.cancel_owner("window-a")
        self.assertIsNone(registry.take(first))
        remaining = registry.take(second)
        self.assertEqual("window-b", remaining[0])
        remaining[1]("safe")
        self.assertEqual(["safe"], delivered)

    def test_startup_gate_skips_headless_loading_and_direct_capture_open(self):
        allowed = project_launcher.launcher_startup_allowed
        self.assertTrue(allowed([]))
        self.assertFalse(allowed(["--unittest", "[eventbrowser]"]))
        self.assertFalse(allowed(["--replayhost=build-machine"]))
        self.assertFalse(allowed(["--targetcontrol=localhost:38920"]))
        self.assertFalse(allowed(["--python=automation.py"]))
        self.assertFalse(allowed(["C:/captures/frame.rdc"]))
        self.assertFalse(allowed([], capture_loaded=True))
        self.assertFalse(allowed([], capture_loading=True))
        self.assertFalse(allowed([], ui_available=False))

    def test_capture_path_auto_association_requires_one_unique_path(self):
        first = project_launcher.validate_project(_project(active=False))
        capture_path = first["captures"][0]["path"]
        self.assertEqual(
            first["workspaceId"],
            project_launcher.unique_project_for_capture(
                [first], capture_path,
                first["captures"][0]["contentIdentity"])["workspaceId"],
        )
        self.assertIsNone(
            project_launcher.unique_project_for_capture(
                [first], "C:/captures/not-registered.rdc",
                first["captures"][0]["contentIdentity"]))
        replaced = dict(first["captures"][0]["contentIdentity"])
        replaced["sha256"] = "5" * 64
        self.assertEqual(
            first["workspaceId"],
            project_launcher.unique_project_for_capture(
                [first], capture_path, replaced)["workspaceId"])
        self.assertEqual(
            first["workspaceId"],
            project_launcher.unique_project_for_capture(
                [first], capture_path, None)["workspaceId"])
        second_value = _project(active=False)
        second_value["workspaceId"] = "3" * 32
        second_value["captureLibrary"]["workspaceId"] = "3" * 32
        second = project_launcher.validate_project(second_value)
        self.assertIsNone(
            project_launcher.unique_project_for_capture(
                [first, second], capture_path,
                first["captures"][0]["contentIdentity"]))

    def test_client_uses_fixed_shell_free_project_commands(self):
        calls = []

        def runner(arguments):
            calls.append(arguments)
            if arguments == ["project-list"]:
                return {
                    "schema": "replaylab-project-list/v1",
                    "projects": [_project()],
                }
            if arguments[0] == "project-match-capture":
                project = _project()
                return {
                    "schema": "replaylab-capture-project-match/v1",
                    "matchCount": 1,
                    "matches": [{"workspaceId": project["workspaceId"]}],
                    "activated": True,
                    "project": {
                        "workspaceId": project["workspaceId"],
                        "projectRoot": project["projectRoot"],
                        "name": project["name"],
                    },
                }
            if arguments[0] == "project-unregister":
                return {
                    "schema": "replaylab-project-unregister/v1",
                    "removed": True,
                    "projectFilesDeleted": False,
                }
            return _project()

        client = project_launcher.ReplayLabProjectClient(
            "C:/ReplayLab/ReplayLabInternal.exe", runner=runner)
        projects = client.list_projects()
        self.assertEqual("Example", projects[0]["name"])
        client.create_project(
            "New", "C:/ReplayLab/portable/New",
            capture_files=["C:/captures/a.rdc"])
        self.assertEqual("project-create", calls[1][0])
        self.assertIn("--project-root", calls[1])
        self.assertNotIn("--game-id", calls[1])
        self.assertIn("--capture", calls[1])
        client.register_existing_project("C:/ReplayLab/portable/Existing")
        self.assertEqual("project-register", calls[2][0])
        client.unregister_project("C:/ReplayLab/portable/Existing")
        self.assertEqual("project-unregister", calls[3][0])
        client.activate("C:/ReplayLab/projects/example")
        self.assertEqual("project-activate", calls[4][0])
        client.configure_project(
            "C:/ReplayLab/projects/example",
            game_id="game",
            build_id="build",
            platform="windows-x64",
            api_family="d3d11",
            asset_root="C:/assets",
        )
        self.assertEqual("project-configure", calls[5][0])
        self.assertIn("--asset-root", calls[5])
        client.remove_captures(
            "C:/ReplayLab/projects/example", ["2" * 64])
        self.assertEqual("project-remove-capture", calls[6][0])
        self.assertIn("--capture-ref-id", calls[6])
        match = client.match_and_activate_capture(
            "C:/captures/a.rdc",
            "C:/ReplayLab/ipc-v2/instances/one/lease.json",
            "a" * 32)
        self.assertTrue(match["activated"])
        self.assertEqual("project-match-capture", calls[7][0])
        self.assertIn("--association-lease", calls[7])

    def test_client_exposes_unavailable_registry_records_for_removal(self):
        def runner(arguments):
            self.assertEqual(["project-list"], arguments)
            return {
                "schema": "replaylab-project-list/v1",
                "projects": [],
                "rejected": [{
                    "workspaceId": "a" * 32,
                    "projectRoot": "C:/Moved/Old Project",
                    "name": "Old Project",
                    "reason": "workspace manifest is unavailable",
                }],
            }

        client = project_launcher.ReplayLabProjectClient(
            "C:/ReplayLab/ReplayLabInternal.exe", runner=runner)
        projects = client.list_projects()
        self.assertEqual(1, len(projects))
        self.assertTrue(projects[0]["unavailable"])
        self.assertEqual([], projects[0]["captures"])
        self.assertIn("manifest", projects[0]["unavailableReason"])

    def test_project_client_localizes_stable_unavailable_reason_id(self):
        def runner(arguments):
            self.assertEqual(["project-list"], arguments)
            return {
                "schema": "replaylab-project-list/v1",
                "projects": [],
                "rejected": [{
                    "workspaceId": "b" * 32,
                    "projectRoot": "C:/Moved/Invalid Project",
                    "name": "Invalid Project",
                    "reasonId": "project-validation-failed",
                    "reasonDetail": "workspace manifest is unavailable",
                }],
            }

        client = project_launcher.ReplayLabProjectClient(
            "C:/ReplayLab/ReplayLabInternal.exe", runner=runner)
        project = client.list_projects()[0]
        self.assertEqual(
            "Project files are unavailable or invalid: workspace manifest is unavailable",
            project_launcher._unavailable_project_reason_text(project))
        self.assertEqual(
            "project-validation-failed", project["unavailableReasonId"])

    def test_project_client_uses_internal_service_without_mcp_subprocess(self):
        calls = []
        original_resolve = (
            project_launcher._internal_client_protocol.resolve_internal_executable)
        original_client = (
            project_launcher._internal_client_protocol.ReplayLabInternalProjectClient)

        class _InternalClient(object):
            def __init__(self, executable, request_timeout_seconds):
                calls.append(("construct", executable, request_timeout_seconds))

            def invoke(self, operation, parameters):
                calls.append((operation, parameters))
                return {
                    "schema": "replaylab-project-list/v1",
                    "projects": [_project()],
                }

        project_launcher._internal_client_protocol.resolve_internal_executable = (
            lambda unused: "C:/ReplayLab/ReplayLabInternal.exe")
        project_launcher._internal_client_protocol.ReplayLabInternalProjectClient = (
            _InternalClient)
        try:
            client = project_launcher.ReplayLabProjectClient(
                "C:/ReplayLab/ReplayLabInternal.exe", timeout_seconds=7)
            projects = client.list_projects()
        finally:
            project_launcher._internal_client_protocol.resolve_internal_executable = (
                original_resolve)
            project_launcher._internal_client_protocol.ReplayLabInternalProjectClient = (
                original_client)

        self.assertEqual("Example", projects[0]["name"])
        self.assertEqual(
            ("construct", "C:/ReplayLab/ReplayLabInternal.exe", 7), calls[0])
        self.assertEqual(("list-projects", {}), calls[1])
        source = (
            _class_method_source("ReplayLabProjectClient", "__init__") +
            _class_method_source("ReplayLabProjectClient", "_run_internal_request"))
        self.assertNotIn("Popen", source)
        self.assertNotIn("project-list\"]", source)

    def test_internal_project_operation_rejects_unknown_or_duplicate_options(self):
        with self.assertRaisesRegex(
                project_launcher.ProjectLauncherError, "not allowed"):
            project_launcher._internal_project_operation(["stdio"])
        with self.assertRaisesRegex(
                project_launcher.ProjectLauncherError, "duplicate"):
            project_launcher._internal_project_operation([
                "project-activate", "--project-root", "C:/one",
                "--project-root", "C:/two",
            ])

    def test_async_executor_shutdown_uses_only_bounded_wait(self):
        shutdown = _class_method_source("AsyncProjectExecutor", "shutdown")
        self.assertIn("waitForDone(max(1, int(timeout_ms)))", shutdown)
        self.assertIn("activeThreadCount() > 0", shutdown)
        self.assertNotIn("while self.pool.activeThreadCount()", shutdown)
        self.assertNotIn("waitForDone()", shutdown)

    def test_async_executor_result_timer_runs_only_while_callbacks_are_pending(self):
        constructor = _class_method_source("AsyncProjectExecutor", "__init__")
        submit = _class_method_source("AsyncProjectExecutor", "submit")
        drain = _class_method_source("AsyncProjectExecutor", "_drain_results")
        cancel = (
            _class_method_source("AsyncProjectExecutor", "cancel_owner")
            + _class_method_source("AsyncProjectExecutor", "cancel_all")
        )
        self.assertNotIn("self._result_timer.start()", constructor)
        self.assertIn("if not self._result_timer.isActive():", submit)
        self.assertIn("self._result_timer.start()", submit)
        self.assertIn("if len(self.registry) == 0:", drain)
        self.assertIn("self._result_timer.stop()", drain + cancel)

    def test_shutdown_has_no_per_operation_project_process(self):
        source = _class_method_source("ReplayLabProjectClient", "__init__")
        self.assertNotIn("Popen", source)
        shutdown = _class_method_source("AsyncProjectExecutor", "shutdown")
        self.assertNotIn("cancel_active_host_processes", shutdown)

    def test_thumbnail_reader_uses_metadata_handle_without_opening_replay(self):
        calls = []

        class ResultCode:
            Succeeded = object()

        class FileType:
            PNG = object()

        class Thumbnail:
            data = b"png-data"
            width = 128
            height = 72

        class Capture:
            def OpenFile(self, path, filetype, progress):
                calls.append(("OpenFile", path, filetype, progress))
                return ResultCode.Succeeded

            def GetThumbnail(self, filetype, max_edge):
                calls.append(("GetThumbnail", filetype, max_edge))
                return Thumbnail()

            def Shutdown(self):
                calls.append(("Shutdown",))

            def OpenCapture(self, *unused_args):
                raise AssertionError("thumbnail reader must never create replay")

        class FakeRenderDoc:
            @staticmethod
            def OpenCaptureFile():
                calls.append(("OpenCaptureFile",))
                return Capture()

        FakeRenderDoc.ResultCode = ResultCode
        FakeRenderDoc.FileType = FileType

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "frame.rdc"
            path.write_bytes(b"rdc")
            reader = project_launcher.RenderDocThumbnailReader(FakeRenderDoc, 256)
            result = reader(str(path))
        self.assertEqual("ready", result["state"])
        self.assertEqual(b"png-data", result["data"])
        self.assertEqual(
            ["OpenCaptureFile", "OpenFile", "GetThumbnail", "Shutdown"],
            [call[0] for call in calls],
        )

    def test_thumbnail_reader_rejects_cross_thread_renderdoc_use(self):
        class FakeRenderDoc:
            pass

        reader = project_launcher.RenderDocThumbnailReader(FakeRenderDoc)
        errors = []

        def invoke():
            try:
                reader("C:/capture.rdc")
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=invoke)
        thread.start()
        thread.join()
        self.assertEqual(1, len(errors))
        self.assertIsInstance(errors[0], project_launcher.ThumbnailReadError)
        self.assertIn("owning UI thread", str(errors[0]))

    def test_thumbnail_cache_is_revision_keyed_bounded_and_negative_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "a.rdc"
            second = root / "b.rdc"
            first.write_bytes(b"a")
            second.write_bytes(b"b")
            calls = []

            def loader(path):
                calls.append(path)
                return {
                    "state": "ready", "reason": "", "data": b"1234",
                    "width": 2, "height": 2,
                }

            cache = project_launcher.ThumbnailCache(
                maximum_entries=1, maximum_bytes=8)
            one = cache.get_or_load(str(first), loader)
            hit = cache.get_or_load(str(first), loader)
            self.assertFalse(one["cacheHit"])
            self.assertTrue(hit["cacheHit"])
            self.assertEqual(1, len(calls))
            cache.get_or_load(str(second), loader)
            cache.get_or_load(str(first), loader)
            self.assertEqual(3, len(calls))

            first.write_bytes(b"changed-size")
            revised = cache.get_or_load(str(first), loader)
            self.assertFalse(revised["cacheHit"])
            self.assertEqual(4, len(calls))

            missing = cache.get_or_load(str(root / "missing.rdc"), loader)
            self.assertEqual("missing", missing["state"])
            self.assertEqual(4, len(calls))

    def test_project_scoped_create_omits_user_visible_scope_arguments(self):
        calls = []

        def runner(arguments):
            calls.append(arguments)
            value = _project()
            value["state"] = "bound"
            value["scope"] = {
                "gameId": "replaylab-project", "buildId": "1" * 32,
                "platform": "", "apiFamily": ""
            }
            return value

        client = project_launcher.ReplayLabProjectClient(
            "C:/ReplayLab/ReplayLabInternal.exe", runner=runner)
        result = client.create_project("Portable", "C:/Projects/Portable")
        self.assertEqual("bound", result["state"])
        self.assertIn("--project-root", calls[0])
        self.assertNotIn("--game-id", calls[0])
        with self.assertRaisesRegex(
                project_launcher.ProjectLauncherError, "both be supplied"):
            client.create_project(
                "Partial", "C:/Projects/Partial", game_id="game")

    def test_project_scoped_settings_only_submit_the_asset_root(self):
        values = project_launcher._project_settings_payload(
            True,
            "replaylab-project",
            "1" * 32,
            "windows-x64",
            "d3d12",
            " C:/Assets ",
        )
        self.assertEqual({"asset_root": "C:/Assets"}, values)

        legacy = project_launcher._project_settings_payload(
            False, "game", "build", "windows-x64", "d3d12", "C:/Assets")
        self.assertEqual("game", legacy["game_id"])
        self.assertEqual("windows-x64", legacy["platform"])

    def test_invalid_host_documents_are_rejected_before_ui_use(self):
        value = _project()
        value["captureLibrary"]["workspaceId"] = "3" * 32
        with self.assertRaisesRegex(
                project_launcher.ProjectLauncherError, "capture library"):
            project_launcher.validate_project(value)


if __name__ == "__main__":
    unittest.main()
