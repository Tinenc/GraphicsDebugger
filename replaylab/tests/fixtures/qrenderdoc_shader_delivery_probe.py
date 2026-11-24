"""Read the real Shader Workspace and native CBuffer views with --ui-python.

The caller loads/selects the intended capture and opens its live workspace.
This probe waits for that state; it never loads a capture, selects an event,
edits a source, compiles, replaces, or changes aliases. Temporary read-only
CBuffer viewers are closed after their title/model and images are recorded.

Required: REPLAYLAB_SHADER_DELIVERY_OUTPUT (JSON receipt).
Optional: REPLAYLAB_SHADER_DELIVERY_WORKSPACE, _SOURCE_SHA256, _EVENT_ID,
          _TIMEOUT_SECONDS (prefix REPLAYLAB_SHADER_DELIVERY for each).
"""
from __future__ import print_function

import hashlib
import json
import os
import sys
import time
import traceback

from PySide2 import QtCore, QtWidgets
import renderdoc as rd
import qrenderdoc as qrd


EXPECTED_ALIASES = ["GlobalsCB", "ViewCB", "ToonMainLightCB", "CartoonBasePassCB",
                    "MaterialCollection0CB", "MaterialCB"]


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _lf(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _rows(widget):
    """Read the actual native RDTreeWidget model, not the alias JSON store."""
    result = []
    for child in widget.findChildren(QtWidgets.QWidget):
        if child.objectName() != "fixedVars" or not child.inherits("QTreeView"):
            continue
        # qrenderdoc's embedded PySide exposes native Qt wrappers without a
        # separately importable shiboken2 Python package.
        view = child
        model = view.model()
        if model is None:
            continue
        for row in range(min(model.rowCount(), 24)):
            result.append([str(model.data(model.index(row, column), QtCore.Qt.DisplayRole) or "")
                           for column in range(min(model.columnCount(), 4))])
    return result


class _DeliveryProbe(object):
    def __init__(self):
        self.output = os.environ.get("REPLAYLAB_SHADER_DELIVERY_OUTPUT", "")
        if not self.output:
            raise RuntimeError("REPLAYLAB_SHADER_DELIVERY_OUTPUT is required")
        self.started = time.time()
        self.timeout = int(os.environ.get("REPLAYLAB_SHADER_DELIVERY_TIMEOUT_SECONDS", "240"))
        self.workspace_id = os.environ.get("REPLAYLAB_SHADER_DELIVERY_WORKSPACE", "")
        self.source_sha = os.environ.get("REPLAYLAB_SHADER_DELIVERY_SOURCE_SHA256", "")
        self.event_id = int(os.environ.get("REPLAYLAB_SHADER_DELIVERY_EVENT_ID", "11329"))
        self.viewers = []
        self.result = {"schema": "replaylab-shader-delivery-ui/v1", "ok": False,
                       "processId": os.getpid(), "captureMutated": False,
                       "sourceWritten": False, "aliasStoreRead": False,
                       "stage": "waiting-for-workspace"}
        self.timer = None

    def write(self):
        with open(self.output, "w", encoding="utf-8") as stream:
            json.dump(self.result, stream, ensure_ascii=False, indent=2)

    def start(self):
        # PythonShell executes the script on a worker thread. Create the timer
        # only after InvokeOntoUIThread has transferred control to the UI.
        self.timer = QtCore.QTimer()
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.poll)
        self.write()
        self.timer.start()
        self.poll()

    def poll(self):
        try:
            bridge = sys.modules.get("renderdoc_mcp_bridge")
            context = getattr(bridge, "_ctx", None)
            panel = getattr(bridge, "_shader_workspace_ui", None)
            if context is None or panel is None or int(context.CurEvent()) != self.event_id:
                return self.wait("waiting-for-capture-and-workspace")
            document = panel._current_document()
            if (not document or document.get("editor") is None or
                    (self.workspace_id and document.get("workspaceId") != self.workspace_id) or
                    document.get("documentKind") != "live-edit" or
                    document["editor"].IsReadOnly() or document.get("dirty")):
                return self.wait("waiting-for-selected-editable-document")
            if self.source_sha and document.get("rawSha256") != self.source_sha:
                return self.wait("waiting-for-published-source")
            if not panel.top.isVisible() or not panel.flow_graph.isVisible():
                return self.wait("waiting-for-visible-workspace-and-flow")
            workspace_model = (panel._document_model(document["workspaceId"]) or
                               document.get("workspace") or {})
            graph = workspace_model.get("flowGraph") or {}
            if not graph.get("nodes") or graph.get("graphKind") != "algorithm-dataflow":
                self.result["graphAvailability"] = workspace_model.get("flowGraphStatus")
                return self.wait("waiting-for-published-algorithm-graph")
            self.timer.stop()
            self.context = context
            self.event_before = int(context.CurEvent())
            self.session_before = getattr(bridge, "_capture_session_id", "")
            self.capture_workspace(panel, document)
            # Read-only native viewers use the current reflected constant blocks.
            reflection = context.CurPipelineState().GetShaderReflection(rd.ShaderStage.Pixel)
            if reflection is None:
                raise RuntimeError("No pixel Shader reflection at the selected event")
            existing = list(QtWidgets.QApplication.allWidgets())
            for index, block in enumerate(reflection.constantBlocks):
                viewer = context.ViewConstantBuffer(rd.ShaderStage.Pixel, index, 0)
                widget = viewer.Widget()
                created = widget not in existing
                if created:
                    # Native BufferViewer deliberately defers reads while
                    # hidden; expose this temporary read-only viewer first.
                    context.AddDockWindow(widget, qrd.DockReference.TransientPopupArea,
                                          context.GetPipelineViewer().Widget(), 0.3)
                self.viewers.append((index, str(block.name), viewer, widget,
                                     created))
            self.viewers_started = time.time()
            self.result["stage"] = "waiting-for-native-cbuffer-models"
            self.write()
            QtCore.QTimer.singleShot(1500, self.finish)
        except Exception:
            self.fail()

    def wait(self, stage):
        self.result["stage"] = stage
        if time.time() - self.started >= self.timeout:
            raise RuntimeError("Timed out: " + stage)
        self.write()

    def capture_workspace(self, panel, document):
        source_path = document["sourcePath"]
        with open(source_path, "rb") as stream:
            raw = stream.read()
        ui_source = str(document["editor"].GetText())
        projected_source = panel._document_source_text(document)
        raw_text = raw.decode("utf-8-sig")
        view = panel.flow_graph.view
        graph = (panel._document_model(document["workspaceId"]) or
                 document.get("workspace") or {}).get("flowGraph") or {}
        bounds = view.scene().itemsBoundingRect()
        projected = view.mapFromScene(bounds).boundingRect()
        visible = view.viewport().rect().adjusted(-2, -2, 2, 2)
        normalized = view.graph_info()
        locations = []
        source_lines = _lf(raw_text).splitlines()
        for node in graph.get("nodes", []):
            location = node.get("source") or {}
            line = int(location.get("line", 0))
            locations.append({"id": node.get("id"), "label": node.get("label"),
                              "symbol": node.get("name"), "line": line,
                              "column": location.get("column"),
                              "code": source_lines[line - 1] if 0 < line <= len(source_lines) else ""})
        # Exercise the same signal used by a graph-node click against the
        # actual current project document, not a standalone scratch editor.
        original_line = document["editor"].GetCursorLine()
        navigation = []
        normalized_nodes = {node["id"]: node for node in view._graph["nodes"]}
        for location in locations:
            view._activate_node(normalized_nodes[location["id"]])
            current = panel._current_document()
            navigation.append({"id": location["id"], "expectedLine": location["line"],
                "actualLine": current["editor"].GetCursorLine(),
                "sameDocument": current["workspaceId"] == document["workspaceId"],
                "passed": current["workspaceId"] == document["workspaceId"] and
                          current["editor"].GetCursorLine() == location["line"]})
        document["editor"].GoToLine(original_line, 1)
        screenshot = os.path.splitext(self.output)[0] + "-workspace.png"
        if not panel.top.grab().save(screenshot):
            raise RuntimeError("Workspace screenshot could not be written")
        entry_lines = [index + 1 for index, line in enumerate(source_lines)
                       if line.strip() == "void MainPS_inner()"]
        entry_screenshot = ""
        if len(entry_lines) == 1:
            document["editor"].GoToLine(min(len(source_lines), entry_lines[0] + 45), 1)
            document["editor"].GoToLine(entry_lines[0], 1)
            entry_screenshot = os.path.splitext(self.output)[0] + "-algorithm-entry.png"
            if not panel.top.grab().save(entry_screenshot):
                raise RuntimeError("Algorithm entry screenshot could not be written")
            document["editor"].GoToLine(original_line, 1)
        self.result.update({"workspaceId": document["workspaceId"],
            "sourcePath": source_path, "documentKind": document.get("documentKind"),
            "readOnly": bool(document["editor"].IsReadOnly()),
            "rawSourceSha256": _sha(raw), "documentRawSha256": document.get("rawSha256"),
            "uiTextSha256": _sha(ui_source.encode("utf-8")),
            "projectedTextSha256": _sha(projected_source.encode("utf-8")),
            "uiTextMatchesCanonicalIgnoringNewlines": _lf(ui_source) == _lf(raw_text),
            "graphSourceMatchesCanonical": graph.get("sourceSha256") == _sha(raw),
            "graphKind": graph.get("graphKind"), "layoutDirection": graph.get("layoutDirection"),
            "nodeCount": len(graph.get("nodes", [])), "edgeCount": len(graph.get("edges", [])),
            "locations": locations, "graphMetadata": normalized,
            "navigation": navigation,
            "allGraphNodesNavigate": bool(navigation) and all(item["passed"] for item in navigation),
            "autoFit": bool(view._auto_fit), "wholeGraphFits": visible.contains(projected),
            "scaleX": view.transform().m11(), "scaleY": view.transform().m22(),
            "viewportWidth": view.viewport().width(), "viewportHeight": view.viewport().height(),
            "screenshot": screenshot})
        self.result.update({"algorithmEntryScreenshot": entry_screenshot,
                            "algorithmEntryLine": entry_lines[0] if len(entry_lines) == 1 else None})

    def finish(self):
        waiting_for_models = False
        try:
            if (self.viewers and time.time() - self.viewers_started < 60 and
                    any(not _rows(item[3]) for item in self.viewers)):
                waiting_for_models = True
                QtCore.QTimer.singleShot(500, self.finish)
                return
            aliases = []
            for index, original_name, unused_viewer, widget, created in self.viewers:
                title = str(widget.windowTitle())
                found = [name for name in EXPECTED_ALIASES if "<" + name + ">" in title]
                rows = _rows(widget)
                screenshot = os.path.splitext(self.output)[0] + "-cbuffer-%d.png" % index
                if created:
                    widget.resize(900, 600)
                saved = widget.grab().save(screenshot)
                aliases.append({"slot": index, "originalName": original_name,
                                "windowTitle": title, "aliasesDisplayed": found,
                                "modelRows": rows, "screenshot": screenshot if saved else ""})
            displayed = set(name for item in aliases for name in item["aliasesDisplayed"])
            bridge = sys.modules.get("renderdoc_mcp_bridge")
            self.result.update({"stage": "complete", "cbufferViews": aliases,
                "allSixAliasesDisplayed": all(name in displayed for name in EXPECTED_ALIASES),
                "allNativeCBufferModelsLoaded": bool(aliases) and all(item["modelRows"] for item in aliases),
                "captureSessionUnchanged": self.session_before == getattr(bridge, "_capture_session_id", ""),
                "eventUnchanged": self.event_before == int(self.context.CurEvent())})
            self.result["ok"] = all((self.result["uiTextMatchesCanonicalIgnoringNewlines"],
                self.result["graphSourceMatchesCanonical"], self.result["wholeGraphFits"],
                self.result["allGraphNodesNavigate"],
                self.result["graphKind"] == "algorithm-dataflow",
                self.result["allSixAliasesDisplayed"], self.result["captureSessionUnchanged"],
                self.result["eventUnchanged"], not self.result["readOnly"]))
            self.write()
        except Exception:
            self.fail()
        finally:
            if not waiting_for_models:
                self.cleanup()

    def cleanup(self):
        for unused_index, unused_name, unused_viewer, widget, created in self.viewers:
            if created:
                widget.close()
        self.viewers = []

    def fail(self):
        self.timer.stop()
        self.result.update({"ok": False, "error": traceback.format_exc()})
        self.write()
        self.cleanup()


probe = _DeliveryProbe()
setattr(sys.modules["renderdoc_mcp_bridge"], "_shader_delivery_probe", probe)
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(probe.start)
