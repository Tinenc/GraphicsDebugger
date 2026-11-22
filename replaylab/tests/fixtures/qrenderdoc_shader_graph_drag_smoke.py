"""Real Qt fixed-graph/hierarchy regression; no capture/source/replay access.

REPLAYLAB_SHADER_GRAPH_DRAG_OUTPUT selects the JSON/PNG evidence. Exercises
the native mouse-grab path with nodes now fixed, and real double-click and
back/forward button events. The scratch window cannot quit the application.
"""
from __future__ import print_function

import json
import os
import sys
import traceback

from PySide2 import QtCore, QtGui, QtWidgets
from renderdoc_mcp_bridge.project_shader_flow_graph import ShaderFlowGraphPanel
from renderdoc_mcp_bridge.project_shader_flow_hierarchy import SourceFlowHierarchy


def _settle(milliseconds=30):
    loop = QtCore.QEventLoop()
    QtCore.QTimer.singleShot(milliseconds, loop.quit)
    loop.exec_()


def _mouse(view, kind, point, button, buttons):
    viewport = view.viewport()
    local = QtCore.QPointF(point)
    screen = QtCore.QPointF(viewport.mapToGlobal(point))
    event = QtGui.QMouseEvent(kind, local, local, screen, button, buttons, QtCore.Qt.NoModifier)
    QtWidgets.QApplication.sendEvent(viewport, event)


def _fits(view):
    projected = view.mapFromScene(view.scene().itemsBoundingRect()).boundingRect()
    return view.viewport().rect().adjusted(-2, -2, 2, 2).contains(projected)


def _button_click(button):
    point = button.rect().center()
    local = QtCore.QPointF(point)
    screen = QtCore.QPointF(button.mapToGlobal(point))
    for kind, buttons in ((QtCore.QEvent.MouseButtonPress, QtCore.Qt.LeftButton),
                          (QtCore.QEvent.MouseButtonRelease, QtCore.Qt.NoButton)):
        QtWidgets.QApplication.sendEvent(button, QtGui.QMouseEvent(
            kind, local, local, screen, QtCore.Qt.LeftButton, buttons, QtCore.Qt.NoModifier))
    _settle()


def _double_click(view, node_id):
    item = view._nodes[node_id]
    view.centerOn(item.sceneBoundingRect().center())
    point = view.mapFromScene(item.sceneBoundingRect().center())
    _mouse(view, QtCore.QEvent.MouseButtonPress, point, QtCore.Qt.LeftButton, QtCore.Qt.LeftButton)
    _mouse(view, QtCore.QEvent.MouseButtonRelease, point, QtCore.Qt.LeftButton, QtCore.Qt.NoButton)
    _mouse(view, QtCore.QEvent.MouseButtonDblClick, point, QtCore.Qt.LeftButton, QtCore.Qt.LeftButton)
    _mouse(view, QtCore.QEvent.MouseButtonRelease, point, QtCore.Qt.LeftButton, QtCore.Qt.NoButton)
    _settle(100)


def _single_click(view, node_id):
    item = view._nodes[node_id]
    view.centerOn(item.sceneBoundingRect().center())
    point = view.mapFromScene(item.sceneBoundingRect().center())
    _mouse(view, QtCore.QEvent.MouseButtonPress, point, QtCore.Qt.LeftButton, QtCore.Qt.LeftButton)
    _mouse(view, QtCore.QEvent.MouseButtonRelease, point, QtCore.Qt.LeftButton, QtCore.Qt.NoButton)
    # A single click is deferred by the double-click interval so an ensuing
    # double-click can cancel it; wait past that window for the real navigation.
    _settle(QtWidgets.QApplication.doubleClickInterval() + 80)


class _DragProbe(object):
    def run(self):
        output = os.environ.get("REPLAYLAB_SHADER_GRAPH_DRAG_OUTPUT", "")
        if not output:
            raise RuntimeError("REPLAYLAB_SHADER_GRAPH_DRAG_OUTPUT is required")
        result = {"schema": "replaylab-shader-fixed-graph-hierarchy-smoke/v1", "ok": False,
                  "processId": os.getpid(), "captureMutated": False,
                  "sourceWritten": False, "checks": {}}
        host, view = None, None
        pressed = False
        point = QtCore.QPoint()
        try:
            host = QtWidgets.QWidget()
            host.setAttribute(QtCore.Qt.WA_QuitOnClose, False)
            host.setWindowTitle(QtCore.QCoreApplication.translate("ReplayLabProjects", "Shader algorithm flow"))
            host.resize(660, 820)
            layout = QtWidgets.QVBoxLayout(host)
            panel = ShaderFlowGraphPanel(host)
            layout.addWidget(panel)
            hierarchy = SourceFlowHierarchy(
                "float Square(float x) { return x*x; }\n"
                "float Refine(float x) { if(x>0) { return Square(x); } return -x; }\n"
                "float Main(float x) { float y=Refine(x); return y; }\n",
                {"documentId": "scratch-buffer"})
            activations = []
            expansions = []
            panel.nodeActivated.connect(lambda payload: activations.append(dict(payload)))

            def expand(payload):
                expansions.append(dict(payload))
                child = hierarchy.function_graph(payload["functionId"])
                for node in child["nodes"]:
                    node["source"]["anchorId"] = "scratch:" + node["id"]
                panel.open_subgraph(child)

            panel.subgraphRequested.connect(expand)
            graph = {"schema": "replaylab-shader-flow-graph/v1", "schemaVersion": 1,
                     "graphId": "scratch-drag-regression", "graphKind": "algorithm-dataflow",
                     "nodes": [{"id": "stage-%d" % i, "label": "Computation stage %d" % i,
                                "name": "Main", "source": {"line": 3, "column": 23,
                                    "anchorId": "scratch:stage-%d" % i}}
                               for i in range(12)],
                     "edges": [{"source": "stage-%d" % i, "target": "stage-%d" % (i + 1), "kind": "data"}
                               for i in range(11)]}
            graph = hierarchy.project_root(graph)
            panel.set_graph(graph)
            host.show()
            _settle(200)
            view = panel.view
            checks = result["checks"]
            checks["initialWholeFit"] = view._auto_fit and _fits(view)
            view.actual_size()
            first = view._nodes["stage-0"]
            view.centerOn(first.sceneBoundingRect().center())
            _settle()
            initial_position = QtCore.QPointF(first.pos())
            initial_rect = QtCore.QRectF(view.scene().sceneRect())
            point = view.mapFromScene(first.sceneBoundingRect().center())
            _mouse(view, QtCore.QEvent.MouseButtonPress, point, QtCore.Qt.LeftButton, QtCore.Qt.LeftButton)
            pressed = True
            checks["nativeMouseGrabEstablished"] = view.scene().mouseGrabberItem() is first
            frozen = []
            start = QtCore.QPoint(point)
            for index in range(1, 9):
                point = start + QtCore.QPoint(-index * 22, -index * 95)
                _mouse(view, QtCore.QEvent.MouseMove, point, QtCore.Qt.NoButton, QtCore.Qt.LeftButton)
                _settle(15)
                frozen.append(view.scene().sceneRect() == initial_rect)
            checks["sceneRangeFrozenThroughoutDrag"] = all(frozen)
            checks["nodeStayedFixedDuringDrag"] = first.pos() == initial_position
            _mouse(view, QtCore.QEvent.MouseButtonRelease, point, QtCore.Qt.LeftButton, QtCore.Qt.NoButton)
            pressed = False
            _settle(120)
            checks["mouseGrabReleased"] = view.scene().mouseGrabberItem() is None
            checks["rangeStayedFixedAfterRelease"] = (view.scene().sceneRect() == initial_rect and
                view.scene().sceneRect().contains(first.sceneBoundingRect()))
            moved = QtCore.QPointF(first.pos())
            checks["manualZoomPreserved"] = not view._auto_fit and abs(view._zoom - 1.0) < 1e-8
            panel.set_graph(graph)
            _settle()
            checks["fixedPositionSurvivesRefresh"] = view._nodes["stage-0"].pos() == moved
            checks["manualZoomSurvivesRefresh"] = not view._auto_fit and abs(view._zoom - 1.0) < 1e-8
            view.fit_graph()
            _settle()
            checks["fitAfterDrag"] = view._auto_fit and _fits(view)
            view.reset_layout()
            _settle()
            checks["resetRestoresLayoutAndWholeFit"] = view._nodes["stage-0"].pos() == initial_position and _fits(view)
            view.actual_size()
            center_before = view.mapToScene(view.viewport().rect().center())
            pan_point = view.viewport().rect().center()
            _mouse(view, QtCore.QEvent.MouseButtonPress, pan_point, QtCore.Qt.MiddleButton, QtCore.Qt.MiddleButton)
            _mouse(view, QtCore.QEvent.MouseMove, pan_point - QtCore.QPoint(35, 45), QtCore.Qt.NoButton, QtCore.Qt.MiddleButton)
            _mouse(view, QtCore.QEvent.MouseButtonRelease, pan_point - QtCore.QPoint(35, 45), QtCore.Qt.MiddleButton, QtCore.Qt.NoButton)
            checks["canvasStillPans"] = view.mapToScene(view.viewport().rect().center()) != center_before
            _single_click(view, "stage-1")
            checks["singleClickNavigatesToUse"] = bool(activations) and activations[-1].get("nodeId") == "stage-1"
            activations_before_expand = len(activations)
            _double_click(view, "stage-0")
            checks["doubleClickDoesNotAlsoNavigate"] = len(activations) == activations_before_expand
            checks["doubleClickEntersActualFunction"] = view.graph_info().get("functionId") == hierarchy.function_identity("Main")
            checks["innerGraphContainsActualCalculation"] = any(
                "Refine(x)" in node["label"] for node in view.graph_info()["nodes"])
            call_id = next(node["id"] for node in view.graph_info()["nodes"] if node.get("functionId"))
            _double_click(view, call_id)
            checks["nestedDoubleClickEntersCallee"] = view.graph_info().get("functionId") == hierarchy.function_identity("Refine")
            checks["conditionalBranchesPreserved"] = {edge["kind"] for edge in view.graph_info()["edges"]} >= {"true", "false"}
            _button_click(panel.back_button)
            checks["backReturnsToParent"] = view.graph_info().get("functionId") == hierarchy.function_identity("Main")
            _button_click(panel.forward_button)
            checks["forwardReturnsToChild"] = view.graph_info().get("functionId") == hierarchy.function_identity("Refine")
            _button_click(panel.back_button)
            _button_click(panel.back_button)
            checks["backReturnsToCompleteTopLevel"] = len(view._nodes) == 12 and not panel.back_button.isEnabled()
            checks["singleClickKeepsOpaqueAnchor"] = bool(activations) and all(
                payload.get("anchorId", "").startswith("scratch:") for payload in activations)
            checks["expansionUsesRealFunctionIds"] = len(expansions) == 2 and all(
                payload["functionId"] in hierarchy.functions for payload in expansions)
            panel.fit_graph()
            _settle()
            checks["topLevelStillFitsAfterHistory"] = _fits(view)
            result["ok"] = all(checks.values())
            image_path = os.path.splitext(output)[0] + ".png"
            if host.grab().save(image_path):
                result["screenshot"] = image_path
        except Exception:
            result["error"] = traceback.format_exc()
        finally:
            if pressed and view is not None:
                _mouse(view, QtCore.QEvent.MouseButtonRelease, point, QtCore.Qt.LeftButton, QtCore.Qt.NoButton)
            if host is not None:
                host.close()
                host.deleteLater()
            with open(output, "w", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)


probe = _DragProbe()
setattr(sys.modules["renderdoc_mcp_bridge"], "_shader_graph_drag_probe", probe)
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(probe.run)
