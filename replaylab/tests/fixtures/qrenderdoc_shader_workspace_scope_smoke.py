"""Bounded --ui-python acceptance probe for the real Shader Workspace/native editor.

Run in a newly opened, isolated qrenderdoc with NO capture loaded. Set
REPLAYLAB_SHADER_SCOPE_SMOKE_OUTPUT to a new absolute JSON receipt path.
The first phase reads the installed panel and real historical registry.
The second phase loads another copy of the installed panel module and uses
an explicitly simulated, memory-only Host. It exercises real Qt widgets,
native keystrokes, Ctrl+S, the normal save worker, and capture transitions.

This is UI-layer evidence, NOT real Host authorization/compile/ABI/replacement
evidence. No RDC is opened, no HLSL or registry is written, and no application
settings are saved. Only the requested JSON receipt and adjacent PNGs are
written. The parent test driver owns qrenderdoc startup and normal shutdown.
"""
from __future__ import print_function

import copy
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import uuid


SOURCE = (
    "float Shade(float value) {\n"
    "  return value * 0.5;\n"
    "}\n"
    "float4 MainPS(float4 pos : SV_Position) : SV_Target {\n"
    "  float value = Shade(pos.x);\n"
    "  return float4(value, 0, 0, 1);\n"
    "}\n")
OUTPUT_ENV = "REPLAYLAB_SHADER_SCOPE_SMOKE_OUTPUT"


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text_evidence(text):
    """Exact synthetic-source diagnostics; never normalize a native newline mismatch."""
    raw = text.encode("utf-8")
    crlf = text.count("\r\n")
    return {"text": text, "repr": repr(text), "utf8Hex": raw.hex(),
            "characterCount": len(text), "byteCount": len(raw), "sha256": _sha(text),
            "newlines": {"crlf": crlf, "lfOnly": text.count("\n") - crlf,
                         "crOnly": text.count("\r") - crlf}}


class _MemoryHost(object):
    """Simulated source/scope/CAS; deliberately no filesystem or replay writer."""

    def __init__(self, protocol, root):
        self.protocol = protocol
        self.root = root
        self.source_path = os.path.join(root, "source", "shader.hlsl")
        self.capture_path = os.path.join(root, "ui-only.rdc")
        self.workspace_id = uuid.uuid4().hex
        self.capture_id = "a" * 64
        self.text = SOURCE
        self.modified_ns = 1000000000
        self.binding = None
        self.calls = []
        self.companions = []
        self.resource_id = 1
        self.lock = threading.RLock()
        self.graph = {
            "schema": "replaylab-shader-flow-graph/v1", "schemaVersion": 1,
            "graphKind": "algorithm-dataflow", "graphId": "scope-ui-memory-graph",
            "sourceSha256": _sha(SOURCE),
            "nodes": [
                {"id": "shade", "name": "Shade", "label": "Scale the input",
                 "kind": "computation", "source": {"line": 2, "column": 3, "symbol": "Shade"}},
                {"id": "call", "name": "MainPS", "label": "Evaluate the source value",
                 "kind": "computation", "source": {"line": 5, "column": 3, "symbol": "MainPS"}},
                {"id": "output", "name": "MainPS", "label": "Write the output color",
                 "kind": "computation", "source": {"line": 6, "column": 3, "symbol": "MainPS"}}],
            "edges": [{"source": "shade", "target": "call", "kind": "data"},
                      {"source": "call", "target": "output", "kind": "data"}],
        }

    def __getattr__(self, name):
        return getattr(self.protocol, name)

    def reopen(self):
        with self.lock:
            self.binding = self.protocol.normalize_capture_binding({
                "instanceId": "scope-smoke-%d" % os.getpid(),
                "captureSessionId": uuid.uuid4().hex,
                "capturePath": self.capture_path})
            self.authorization_id = uuid.uuid4().hex
            self.live_id = uuid.uuid4().hex
            self.watch_id = uuid.uuid4().hex
            for companion in self.companions:
                companion.reopen()
                companion.binding = copy.deepcopy(self.binding)
            return copy.deepcopy(self.binding)

    def add_companion(self):
        with self.lock:
            companion = _MemoryHost(self.protocol, self.root + "-companion")
            companion.capture_path = self.capture_path
            companion.resource_id = 2 + len(self.companions)
            companion.reopen()
            companion.binding = copy.deepcopy(self.binding)
            self.companions.append(companion)
            return companion

    def current_binding(self):
        with self.lock:
            return copy.deepcopy(self.binding)

    def close_capture(self):
        with self.lock:
            self.binding = None
            for companion in self.companions:
                companion.close_capture()

    def _require_scope(self, binding):
        if self.binding is None or binding != self.binding:
            raise RuntimeError("UI-only memory Host rejected a stale capture binding")

    def load_console_snapshot(self, registry_path=None, capture_binding=None):
        del registry_path
        with self.lock:
            workspaces = []
            if capture_binding is not None:
                self._require_scope(capture_binding)
                workspaces = [{
                    "workspaceId": self.workspace_id, "workspacePath": self.root,
                    "sourcePath": self.source_path, "captureId": self.capture_id,
                    "capturePath": self.binding["capturePath"],
                    "captureBinding": copy.deepcopy(self.binding),
                    "shaderRawSha256": "b" * 64, "stage": "fragment",
                    "entryPoint": "MainPS", "resourceId": self.resource_id,
                    "authorizationId": self.authorization_id,
                    "editorEligible": True, "autoApplyReady": True,
                    "flowGraph": copy.deepcopy(self.graph),
                    "flowGraphStatus": {"available": True, "status": "current"},
                    "watch": {"watchId": self.watch_id, "state": "running", "events": []},
                    "liveEdit": {"liveEditId": self.live_id, "state": "running", "events": []},
                    "entries": [], "watchedPaths": [],
                }]
                for companion in self.companions:
                    workspaces.extend(companion.load_console_snapshot(
                        capture_binding=capture_binding)["workspaces"])
            return {"captureBinding": copy.deepcopy(capture_binding),
                    "workspaces": workspaces, "entries": [], "errors": [], "watchedPaths": []}

    def load_authorized_source_document(self, registry_path, authorization_id,
                                        workspace_id, capture_binding=None):
        del registry_path
        with self.lock:
            for companion in self.companions:
                if workspace_id == companion.workspace_id:
                    return companion.load_authorized_source_document(
                        None, authorization_id, workspace_id, capture_binding)
            self._require_scope(capture_binding)
            if authorization_id != self.authorization_id or workspace_id != self.workspace_id:
                raise RuntimeError("UI-only memory Host rejected a stale source identity")
            return {
                "captureBinding": copy.deepcopy(self.binding),
                "authorizationId": self.authorization_id, "workspaceId": self.workspace_id,
                "workspacePath": self.root, "sourcePath": self.source_path,
                "liveEditId": self.live_id, "watchId": self.watch_id,
                "fileName": "shader.hlsl", "stage": "fragment", "entryPoint": "MainPS",
                "resourceId": self.resource_id, "text": self.text, "rawSha256": _sha(self.text),
                "modifiedNs": self.modified_ns, "size": len(self.text.encode("utf-8")),
            }

    def request_pending(self):
        return False

    def save(self, live_id, authorization_id, workspace_id, expected_hash, text):
        with self.lock:
            for companion in self.companions:
                if workspace_id == companion.workspace_id:
                    return companion.save(live_id, authorization_id, workspace_id, expected_hash, text)
            if (self.binding is None or live_id != self.live_id or
                    authorization_id != self.authorization_id or workspace_id != self.workspace_id):
                raise RuntimeError("UI-only memory Host rejected stale save authority")
            if expected_hash != _sha(self.text):
                raise RuntimeError("UI-only memory Host rejected a source CAS mismatch")
            source_hash = _sha(text)
            self.calls.append({
                "liveEditId": live_id, "authorizationId": authorization_id,
                "workspaceId": workspace_id, "baseHash": expected_hash,
                "submittedHash": source_hash, "byteSize": len(text.encode("utf-8")),
                "binding": copy.deepcopy(self.binding), "threadId": threading.get_ident(),
                "simulation": "memory-only-ui-boundary",
            })
            self.text = text
            self.modified_ns += 1000000
            return {"rawSha256": source_hash, "size": len(text.encode("utf-8")),
                    "modifiedNs": self.modified_ns, "changed": source_hash != expected_hash,
                    "compileScheduled": False, "watchId": self.watch_id}


class _MemoryStatProxy(object):
    """Only this isolated module's exact synthetic source has simulated stat data."""

    def __init__(self, memory):
        self.memory = memory

    def __getattr__(self, name):
        return getattr(os, name)

    def stat(self, path, *args, **kwargs):
        memory = next((item for item in [self.memory] + self.memory.companions
                       if os.path.normcase(os.path.abspath(str(path))) == os.path.normcase(item.source_path)), None)
        if memory is None:
            return os.stat(path, *args, **kwargs)
        with memory.lock:
            class _Stat(object):
                pass
            value = _Stat()
            value.st_mtime_ns = memory.modified_ns
            value.st_mtime = value.st_mtime_ns / 1000000000.0
            value.st_size = len(memory.text.encode("utf-8"))
            value.st_mode = 0o100600
            return value


def _write_receipt(value):
    path = os.environ.get(OUTPUT_ENV, "")
    if not path or not os.path.isabs(path):
        raise RuntimeError(OUTPUT_ENV + " must select a new absolute JSON receipt path")
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


class _Runner(object):
    def run(self):
        result = {
            "schema": "replaylab-shader-workspace-scope-smoke/v1", "checks": {},
            "validationScope": "real-no-capture-panel-and-simulated-host-ui-only",
            "hostBoundarySimulated": True, "hostAcceptanceVerified": False,
            "captureMutated": False, "sourceFilesWritten": False,
            "compileOrReplayRequested": False, "applicationSettingsWritten": False,
            "graphNavigationScope": "real-installed-panel-signals-and-native-line-anchors",
        }
        panel = None
        isolated_name = None
        memory = None
        try:
            from PySide2 import QtCore, QtGui, QtWidgets
            import qrenderdoc as qrd
            bridge = sys.modules.get("renderdoc_mcp_bridge")
            context = getattr(bridge, "_ctx", None)
            actual = getattr(bridge, "_shader_workspace_ui", None)
            protocol = getattr(bridge, "_shader_console_protocol", None)
            installed = getattr(bridge, "_shader_workspace_protocol", None)
            if context is None or actual is None or protocol is None or installed is None:
                raise RuntimeError("The installed first-party Shader Workspace is unavailable")
            if context.IsCaptureLoaded():
                raise RuntimeError("This UI-only probe requires an isolated instance with no RDC loaded")

            def checkpoint(stage):
                result["stage"] = stage
                _write_receipt(result)

            def settle(milliseconds=60):
                loop = QtCore.QEventLoop()
                QtCore.QTimer.singleShot(milliseconds, loop.quit)
                loop.exec_()

            def key(widget, code, modifiers=QtCore.Qt.NoModifier, text=""):
                QtWidgets.QApplication.sendEvent(widget, QtGui.QKeyEvent(
                    QtCore.QEvent.KeyPress, code, modifiers, text))
                QtWidgets.QApplication.sendEvent(widget, QtGui.QKeyEvent(
                    QtCore.QEvent.KeyRelease, code, modifiers, text))

            def type_text(widget, text):
                for character in text:
                    key(widget, QtCore.Qt.Key_Return if character == "\n" else ord(character.upper()),
                        text=character)

            def mouse_click(target, point, double=False):
                local = QtCore.QPointF(point)
                screen = QtCore.QPointF(target.mapToGlobal(point))
                events = [(QtCore.QEvent.MouseButtonPress, QtCore.Qt.LeftButton),
                          (QtCore.QEvent.MouseButtonRelease, QtCore.Qt.NoButton)]
                if double:
                    events.extend([(QtCore.QEvent.MouseButtonDblClick, QtCore.Qt.LeftButton),
                                   (QtCore.QEvent.MouseButtonRelease, QtCore.Qt.NoButton)])
                for kind, buttons in events:
                    QtWidgets.QApplication.sendEvent(target, QtGui.QMouseEvent(
                        kind, local, local, screen, QtCore.Qt.LeftButton, buttons, QtCore.Qt.NoModifier))
                settle(100 if double else 40)

            def click_graph(node_id, double=False):
                view = panel.flow_graph.view
                # A tab change or expansion destroys old QGraphicsItems. Keep only the ID
                # across event loops and resolve the actual item again before sending input.
                view.centerOn(view._nodes[node_id].sceneBoundingRect().center())
                settle(30)
                point = view.mapFromScene(view._nodes[node_id].sceneBoundingRect().center())
                if not view.viewport().rect().contains(point):
                    raise RuntimeError("The requested graph node is outside the real viewport")
                mouse_click(view.viewport(), point, double=double)

            def replace_by_keys(editor, widget, old, new):
                before = editor.GetText()
                offset = before.find(old)
                if offset < 0:
                    raise RuntimeError("The expected scratch source token was not found: " + old)
                line = before.count("\n", 0, offset) + 1
                column = offset - before.rfind("\n", 0, offset)
                editor.GoToLine(line, column)
                for unused in old:
                    key(widget, QtCore.Qt.Key_Delete)
                type_text(widget, new)
                if editor.GetText() != before[:offset] + new + before[offset + len(old):]:
                    raise RuntimeError("Ordinary native keystrokes did not produce the exact requested source")

            def source_line(editor, text):
                source = editor.GetText()
                offset = source.find(text)
                if offset < 0:
                    raise RuntimeError("Expected scratch calculation is absent")
                return source.count("\n", 0, offset) + 1

            def wait_saved(document, expected_calls):
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if len(memory.calls) == expected_calls and not document.get("saveInFlight"):
                        return
                    settle(20)
                raise RuntimeError("Native Ctrl+S did not complete exactly one memory save within five seconds")

            def ui_state(selected):
                return {"tabs": selected.tab_bar.count(),
                        "treeRoots": selected.file_tree.topLevelItemCount(),
                        "treeDocuments": len(selected._tree_items),
                        "graphNodes": len(selected.flow_graph.view._nodes),
                        "workspaceCount": len(selected._workspaces),
                        "retainedDocuments": len(selected._documents),
                        "emptyPageCurrent": selected.editor_stack.currentWidget() is selected.empty_page}

            checks = result["checks"]
            checkpoint("real-no-capture-with-historical-registry")
            registry_path = protocol.default_registry_path()
            with open(registry_path, "rb") as stream:
                registry_bytes = stream.read(protocol.MAX_REGISTRY_BYTES + 1)
            if len(registry_bytes) > protocol.MAX_REGISTRY_BYTES:
                raise RuntimeError("Historical registry exceeds the production bound")
            registry = json.loads(registry_bytes.decode("utf-8"))
            result["history"] = {"registryPath": registry_path,
                                 "registrySha256": hashlib.sha256(registry_bytes).hexdigest(),
                                 "authorizationCount": len(registry.get("entries", []))}
            checks["realRegistryHasHistoricalAuthorizations"] = result["history"]["authorizationCount"] > 0
            checks["realCaptureProviderIsEmpty"] = bridge._shader_workspace_capture_binding() is None
            actual.refresh(True)
            result["realNoCapture"] = ui_state(actual)
            checks["realNoCaptureHasNoTabsTreeGraphOrDocuments"] = (
                all(result["realNoCapture"][field] == 0 for field in (
                    "tabs", "treeRoots", "treeDocuments", "graphNodes", "workspaceCount", "retainedDocuments"))
                and result["realNoCapture"]["emptyPageCurrent"])
            if not all(checks.values()):
                raise RuntimeError("The real no-capture panel did not remain empty with history present")

            # Keep all stubs in a separate module namespace loaded from the exact installed
            # source. The actual bridge's panel, protocol and os module are never patched.
            checkpoint("creating-isolated-product-panel")
            isolated_name = "renderdoc_mcp_bridge._scope_smoke_%s" % uuid.uuid4().hex
            spec = importlib.util.spec_from_file_location(isolated_name, installed.__file__)
            isolated = importlib.util.module_from_spec(spec)
            sys.modules[isolated_name] = isolated
            spec.loader.exec_module(isolated)
            with open(installed.__file__, "rb") as stream:
                result["panelSourceSha256"] = hashlib.sha256(stream.read()).hexdigest()
            result["panelSourcePath"] = installed.__file__
            root = os.path.join(tempfile.gettempdir(), "replaylab-scope-ui-%s" % uuid.uuid4().hex)
            memory = _MemoryHost(protocol, root)
            initial_binding = memory.reopen()
            isolated._shader_console_protocol = memory
            isolated.os = _MemoryStatProxy(memory)

            class ScratchPanel(isolated.ReplayLabShaderWorkspacePanel):
                def _load_state(self):
                    return {}

                def _save_state(self):
                    # Intercept UI persistence only; editor/save/capture behavior is inherited.
                    self._scratch_state_write_count = getattr(self, "_scratch_state_write_count", 0) + 1

            # Retain even a partially constructed panel so a missing native API or
            # injected-reader failure cannot orphan the already started save worker.
            panel = ScratchPanel.__new__(ScratchPanel)
            panel.__init__(context, qrd, save_client=memory,
                           capture_binding_provider=memory.current_binding)
            panel.top.setObjectName("replaylabShaderScopeSmokeScratch")
            panel.top.resize(1250, 760)
            panel.top.show()
            panel.top.raise_()
            panel.top.activateWindow()
            settle(200)
            document = panel._current_document()
            if document is None:
                raise RuntimeError("The simulated current authorization did not create a native document")
            editor = document["editor"]
            widget = editor.Widget().findChild(QtWidgets.QWidget, "shaderCodeEditor")
            if widget is None:
                raise RuntimeError("The real native Scintilla editor was not found")
            widget.setFocus(QtCore.Qt.OtherFocusReason)
            initial_state = ui_state(panel)
            result["simulatedCurrent"] = initial_state
            checks["simulatedCurrentShowsOneNativeSourceAndGraph"] = (
                initial_state["tabs"] == initial_state["treeDocuments"] == initial_state["workspaceCount"] == 1
                and initial_state["graphNodes"] > 0 and not initial_state["emptyPageCurrent"])
            checkpoint("real-panel-graph-bookmark-wiring")
            graph_events = []
            expansion_events = []
            panel.flow_graph.nodeActivated.connect(lambda value: graph_events.append(dict(value)))
            panel.flow_graph.subgraphRequested.connect(lambda value: expansion_events.append(dict(value)))
            shade_id = "shade"
            anchor_id = panel.flow_graph.view._nodes[shade_id].node["navigation"]["anchorId"]
            marker = document["flowAnchors"][anchor_id]["marker"]
            editor.GoToLine(1, 1)  # Setup only; graph clicks must move the cursor themselves.
            click_graph(shade_id)
            checks["panelGraphClickTargetsCalculation"] = (
                len(graph_events) == 1 and editor.GetCursorLine() == 2
                and editor.GetLineAnchorLine(marker) == 2)

            editor.GoToLine(1, 1)
            before_return = editor.GetText()
            expected_return = "\n" + SOURCE
            focus = QtWidgets.QApplication.focusWidget()
            return_evidence = {
                "before": _text_evidence(before_return),
                "expected": _text_evidence(expected_return),
                "cursorBefore": {"line": editor.GetCursorLine(), "column": editor.GetCursorColumn()},
                "targetObjectName": widget.objectName(),
                "focusObjectName": focus.objectName() if focus is not None else None,
                "globalKeyboardModifiers": int(QtWidgets.QApplication.keyboardModifiers()),
                "event": {"key": int(QtCore.Qt.Key_Return), "modifiers": 0, "textRepr": repr("\n")},
                "beforeExactlyMatchesInitialSource": before_return == SOURCE,
            }
            result["plainReturnEvidence"] = return_evidence
            key(widget, QtCore.Qt.Key_Return, text="\n")
            after_return = editor.GetText()
            return_evidence["actual"] = _text_evidence(after_return)
            return_evidence["cursorAfter"] = {"line": editor.GetCursorLine(), "column": editor.GetCursorColumn()}
            return_evidence["nativeMarkerLineAfter"] = editor.GetLineAnchorLine(marker)
            first_difference = next((index for index, pair in enumerate(zip(after_return, expected_return))
                                     if pair[0] != pair[1]), min(len(after_return), len(expected_return)))
            return_evidence["firstDifferentCharacter"] = (None if after_return == expected_return else first_difference)
            if after_return != expected_return:
                raise RuntimeError("Plain Return did not insert exactly one preceding line")
            editor.GoToLine(1, 1)
            click_graph(shade_id)
            checks["panelGraphBookmarkFollowsOrdinaryReturn"] = (
                editor.GetCursorLine() == 3 and editor.GetLineAnchorLine(marker) == 3)
            replace_by_keys(editor, widget, "0.5", "0.75")
            editor.GoToLine(1, 1)
            click_graph(shade_id)
            checks["panelGraphBookmarkSurvivesTargetTextEdit"] = (
                editor.GetCursorLine() == source_line(editor, "return value * 0.75;")
                and editor.GetLineAnchorLine(marker) == 3)

            replace_by_keys(editor, widget, "Shade", "ScaleInput")
            replace_by_keys(editor, widget, "Shade", "ScaleInput")
            renamed = editor.GetText()
            expected_renamed = ("\n" + SOURCE).replace("0.5", "0.75").replace("Shade(", "ScaleInput(")
            if renamed != expected_renamed:
                raise RuntimeError("Native rename did not preserve the exact scratch calculation")
            companion = memory.add_companion()
            panel.refresh(True)
            second_index = panel._tab_index(companion.workspace_id)
            main_index = panel._tab_index(memory.workspace_id)
            if second_index < 0 or main_index < 0:
                raise RuntimeError("Two real native source tabs were not created for the tab-return check")
            # Refresh selects a newly added tab automatically. Establish the main tab
            # first so the next mouse click proves an actual user-driven tab transition.
            mouse_click(panel.tab_bar, panel.tab_bar.tabRect(main_index).center())
            if panel._current_document() is not document:
                raise RuntimeError("The original native source tab could not be selected")
            mouse_click(panel.tab_bar, panel.tab_bar.tabRect(second_index).center())
            checks["bookmarkTestSwitchesToAnotherNativeDocument"] = (
                panel._current_document()["workspaceId"] == companion.workspace_id
                and panel._current_document()["editor"] is not editor)
            mouse_click(panel.tab_bar, panel.tab_bar.tabRect(main_index).center())
            editor.GoToLine(1, 1)
            click_graph(shade_id)
            checks["renamedFunctionSurvivesRealTabReturn"] = (
                panel._current_document() is document and editor.GetText() == renamed
                and editor.GetCursorLine() == 3 and editor.GetLineAnchorLine(marker) == 3
                and panel.flow_graph.view._nodes[shade_id].node["navigation"].get("symbol") == "ScaleInput")
            click_graph(shade_id, double=True)
            graph_info = panel.flow_graph.view.graph_info()
            checks["renamedFunctionDoubleClickExpandsCurrentSource"] = (
                len(expansion_events) == 1 and graph_info.get("functionName") == "ScaleInput"
                and panel.flow_graph.navigation_state().get("canBack") is True
                and any(node["navigation"].get("symbol") == "ScaleInput" for node in graph_info["nodes"]))
            child_id = next(node["id"] for node in graph_info["nodes"]
                            if node["navigation"].get("symbol") == "ScaleInput")
            editor.GoToLine(1, 1)
            click_graph(child_id)
            checks["expandedGraphClickUsesNativeCalculationBookmark"] = editor.GetCursorLine() == 3
            mouse_click(panel.flow_graph.back_button, panel.flow_graph.back_button.rect().center())
            if shade_id not in panel.flow_graph.view._nodes:
                raise RuntimeError("Real graph Back did not restore the accepted root graph")

            checkpoint("real-panel-deleted-calculation-is-unavailable")
            target_line = source_line(editor, "return value * 0.75;")
            before_delete = editor.GetText()
            lines = before_delete.splitlines(True)
            removed = lines[target_line - 1]
            editor.GoToLine(target_line, 1)
            # Scintilla reads global keyboard modifiers, so synthesized Shift+Down or
            # Ctrl+End cannot select reliably. Delete the exact ASCII line and its newline.
            for unused in removed:
                key(widget, QtCore.Qt.Key_Delete)
            expected_deleted = "".join(lines[:target_line - 1] + lines[target_line:])
            if editor.GetText() != expected_deleted:
                raise RuntimeError("Ordinary Delete did not remove the exact complete target line")
            sentinel_line = len(expected_deleted.splitlines())
            editor.GoToLine(sentinel_line, 1)
            click_graph(shade_id)
            unavailable = QtCore.QCoreApplication.translate(
                "ReplayLabProjects", "The linked calculation was removed or is incomplete. Finish editing the function before navigating.")
            checks["deletedCalculationClearlyInvalidatesOriginalBookmark"] = (
                editor.GetLineAnchorLine(marker) == 0 and editor.GetCursorLine() == sentinel_line
                and panel.status_label.text() == unavailable)
            result["graphBookmarkEvidence"] = {
                "anchorId": anchor_id, "nativeMarker": marker,
                "clickSignals": graph_events, "expansionSignals": expansion_events,
                "sourceAfterRenameSha256": _sha(renamed), "sourceAfterDeletionSha256": _sha(expected_deleted),
                "deletedLine": removed, "unavailableStatus": panel.status_label.text(),
                "cursorAfterInvalidClick": editor.GetCursorLine(),
                "invalidatedMarkerLine": editor.GetLineAnchorLine(marker),
                "sourceMutations": "ordinary Return, printable keys and unmodified Delete; no SetText or selection shortcuts",
            }
            # Keep the subsequent save/scope test syntactically complete. This is a new
            # statement; the old graph marker must remain invalid instead of rebinding to it.
            editor.GoToLine(target_line, 1)
            # Insert the blank line first: Enter after the indented statement would
            # intentionally auto-indent the following closing brace in the real editor.
            key(widget, QtCore.Qt.Key_Return, text="\n")
            editor.GoToLine(target_line, 1)
            type_text(widget, removed.rstrip("\r\n"))
            if editor.GetText() != before_delete:
                raise RuntimeError("Restoring scratch calculation text did not reproduce the exact source")
            checks["recreatedStatementDoesNotReviveDeletedOldBookmark"] = editor.GetLineAnchorLine(marker) == 0
            widget.setFocus(QtCore.Qt.OtherFocusReason)
            checkpoint("native-typing-and-control-s")
            editor.GoToLine(1, 1)
            type_text(widget, "// native UI save\n")
            settle()
            typed = editor.GetText()
            checks["nativeKeyboardActuallyChangesText"] = typed != SOURCE and "// native UI save" in typed
            checks["typingMarksDocumentDirty"] = document.get("dirty") is True
            if not checks["typingMarksDocumentDirty"]:
                raise RuntimeError("The native text callback did not mark the document dirty")
            key(widget, QtCore.Qt.Key_S, QtCore.Qt.ControlModifier)
            wait_saved(document, 1)
            checks["controlSUsesNormalBackgroundSaveWorker"] = (
                len(memory.calls) == 1 and memory.calls[0]["threadId"] != threading.get_ident()
                and memory.calls[0]["submittedHash"] == _sha(typed)
                and memory.text == typed and document.get("dirty") is False)

            checkpoint("capture-close-preserves-hidden-dirty-buffer")
            editor.GoToLine(1, 1)
            type_text(widget, "// retained draft\n")
            settle()
            draft = editor.GetText()
            retained_count = len(panel._documents)
            memory.close_capture()
            panel.capture_changed(None)
            closed = ui_state(panel)
            result["simulatedClosed"] = closed
            checks["captureCloseHidesTabsTreeAndGraph"] = (
                all(closed[field] == 0 for field in ("tabs", "treeRoots", "treeDocuments", "graphNodes", "workspaceCount"))
                and closed["emptyPageCurrent"])
            checks["captureCloseRetainsDirtyBufferWithoutSaving"] = (
                closed["retainedDocuments"] == retained_count and panel._documents[document["workspaceId"]] is document
                and document.get("dirty") is True and document.get("available") is False
                and editor.GetText() == draft and len(memory.calls) == 1)
            key(widget, QtCore.Qt.Key_X, text="x")
            checks["hiddenDraftNativeEditorIsReadOnly"] = editor.GetText() == draft

            checkpoint("same-path-fresh-scope-resumes-draft")
            fresh_binding = memory.reopen()
            panel.capture_changed(fresh_binding)
            panel.refresh(True)
            settle()
            reopened = panel._current_document()
            checks["samePathFreshScopeResumesSameDirtyNativeBuffer"] = (
                reopened is document and document["editor"] is editor and editor.GetText() == draft
                and document.get("dirty") is True and document.get("available") is True
                and document.get("captureBinding") == fresh_binding
                and fresh_binding["captureSessionId"] != initial_binding["captureSessionId"]
                and panel.tab_bar.count() >= 1 and len(panel.flow_graph.view._nodes) > 0)
            widget.setFocus(QtCore.Qt.OtherFocusReason)
            key(widget, QtCore.Qt.Key_S, QtCore.Qt.ControlModifier)
            wait_saved(document, 2)
            checks["reopenedSaveUsesFreshAuthorityAndPreservedDraft"] = (
                len(memory.calls) == 2 and memory.calls[1]["liveEditId"] == memory.live_id
                and memory.calls[1]["authorizationId"] == memory.authorization_id
                and memory.calls[1]["liveEditId"] != memory.calls[0]["liveEditId"]
                and memory.calls[1]["binding"] == fresh_binding
                and memory.calls[1]["submittedHash"] == _sha(draft)
                and document.get("dirty") is False)
            checks["syntheticSourceAndCaptureWereNeverWritten"] = not os.path.exists(root)
            checks["realPanelAndProtocolWereNotStubbed"] = (
                bridge._shader_workspace_ui is actual and installed._shader_console_protocol is protocol
                and bridge._shader_workspace_capture_binding() is None)
            result["simulatedSaveRequests"] = copy.deepcopy(memory.calls)
            result["settingsWritesIntercepted"] = panel._scratch_state_write_count
            result["screenshot"] = os.path.splitext(os.environ[OUTPUT_ENV])[0] + ".png"
            checks["screenshotWritten"] = bool(panel.top.grab().save(result["screenshot"]))
            result["ok"] = all(checks.values())
            result["stage"] = "complete"
        except Exception as exc:
            result.update(ok=False, error="%s: %s" % (type(exc).__name__, str(exc)))
        finally:
            try:
                if panel is not None:
                    panel.shutdown()
                    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
                    settle(30)
                    if (not panel._native_editors_released or panel._save_worker is not None or panel._alive):
                        raise RuntimeError("Scratch native panel or save worker did not finish shutdown")
                    result["checks"]["nativePanelAndWorkerActuallyDestroyed"] = True
                    result["scratchPanelShutdownCompleted"] = True
                else:
                    result["scratchPanelShutdownCompleted"] = False
            except Exception as exc:
                self.failed_panel = panel
                result.update(ok=False, cleanupError="%s: %s" % (type(exc).__name__, str(exc)))
            if isolated_name is not None:
                sys.modules.pop(isolated_name, None)
            if memory is not None:
                result["simulatedSaveRequests"] = copy.deepcopy(memory.calls)
            _write_receipt(result)
            print("REPLAYLAB_SHADER_SCOPE_SMOKE=" + json.dumps(result, ensure_ascii=False))


runner = _Runner()
if os.path.exists(os.environ.get(OUTPUT_ENV, "")):
    raise RuntimeError(OUTPUT_ENV + " must not overwrite an earlier acceptance receipt")
_write_receipt({"ok": False, "stage": "waiting-for-ui-callback",
                "validationScope": "real-no-capture-panel-and-simulated-host-ui-only"})
bridge = sys.modules.get("renderdoc_mcp_bridge")
if bridge is not None:
    bridge._runtime_shader_workspace_scope_smoke = runner
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(runner.run)
