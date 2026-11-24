"""Real Qt Shader-card regression in an isolated, capture-free qrenderdoc.

Set RENDERDOC_CONFIG_DIR and LOCALAPPDATA below an absolute system-temp root,
REPLAYLAB_SHADER_CARDS_OUTPUT to a JSON path there, and
REPLAYLAB_SHADER_CARDS_SOURCE_ROOT to the repository's canonical bridge folder.
Run with --ui-python.
This fixture imports the working-tree renderer directly, writes a JSON/PNG
receipt, and closes only its own isolated process. It never accesses a capture.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import traceback

from PySide2 import QtCore, QtGui, QtWidgets


def _load(name, filename):
    path = Path(os.environ["REPLAYLAB_SHADER_CARDS_SOURCE_ROOT"]).resolve() / filename
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CardsProbe(object):
    def run(self):
        output = Path(os.environ["REPLAYLAB_SHADER_CARDS_OUTPUT"]).resolve()
        scratch = str(Path(tempfile.gettempdir()).resolve()).lower()
        for path in (output, Path(os.environ["RENDERDOC_CONFIG_DIR"]).resolve(),
                     Path(os.environ["LOCALAPPDATA"]).resolve()):
            if os.path.commonpath([str(path).lower(), scratch]) != scratch:
                raise RuntimeError("Shader-card probe requires system-temp isolation")
        result = {"ok": False, "processId": os.getpid(), "captureMutated": False,
                  "sourceWritten": False, "checks": {}}
        pyrenderdoc.GetMainWindow().Widget().hide()
        host = None
        try:
            graph_module = _load("bread_shader_cards", "project_shader_flow_graph.py")
            hierarchy_module = _load("bread_shader_card_hierarchy", "project_shader_flow_hierarchy.py")
            hierarchy = hierarchy_module.SourceFlowHierarchy(
                "// Prepare the sampled surface color.\n"
                "float3 PrepareSurfaceColor(float3 color) { return color; }\n"
                "// Combine the surface with the evaluated light.\n"
                "float3 CombineSurfaceWithEvaluatedIndirectLight(float3 color, float3 light) { return color*light; }\n"
                "// Evaluate the surface and combine its lighting.\n"
                "float3 Main(float3 color, float3 light) {\n"
                "float3 surface = PrepareSurfaceColor(color);\n"
                "return CombineSurfaceWithEvaluatedIndirectLight(surface, light); }\n",
                {"documentId": "scratch", "relativePath": "scratch.hlsl"})
            host = QtWidgets.QWidget()
            host.setAttribute(QtCore.Qt.WA_QuitOnClose, False)
            host.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
            host.resize(480, 750)
            palette = host.palette()
            palette.setColor(QtGui.QPalette.Base, QtGui.QColor(38, 38, 38))
            palette.setColor(QtGui.QPalette.Text, QtGui.QColor(238, 238, 238))
            palette.setColor(QtGui.QPalette.Window, QtGui.QColor(52, 52, 52))
            palette.setColor(QtGui.QPalette.Mid, QtGui.QColor(72, 72, 72))
            host.setPalette(palette)
            layout = QtWidgets.QVBoxLayout(host)
            panel = graph_module.ShaderFlowGraphPanel(host)
            layout.addWidget(panel)
            graph = hierarchy.function_graph(hierarchy.function_identity("Main"), functions_only=True)
            panel.set_graph(graph)
            host.show()
            QtWidgets.QApplication.processEvents()
            nodes = list(panel.view._nodes.values())
            checks = result["checks"]
            checks["threeActualFunctions"] = len(nodes) == 3
            checks["commentBeforeName"] = all(
                node._description.pos().y() + node._description.boundingRect().height() <= node._title.pos().y()
                for node in nodes)
            checks["functionNameLarger"] = all(
                node._title.font().pointSizeF() > node._description.font().pointSizeF()
                for node in nodes)
            checks["onlyCommentAndName"] = all(
                len([item for item in node.childItems() if isinstance(item, QtWidgets.QGraphicsTextItem)]) == 2
                for node in nodes)
            checks["noInterfaceProse"] = all(
                not any(text in item.toPlainText() for text in ("in:", "out:", "float3 Main("))
                for node in nodes for item in node.childItems()
                if isinstance(item, QtWidgets.QGraphicsTextItem))
            checks["wrappedNameInsideCard"] = all(
                node._title.pos().y() + node._title.boundingRect().height() <= node.boundingRect().height()
                for node in nodes)
            checks["interfaceMetadataRetained"] = all(node.node["io"]["writes"] for node in nodes)
            checks["currentSourceExecutionOrder"] = [node.node["symbol"] for node in nodes] == [
                "Main", "PrepareSurfaceColor", "CombineSurfaceWithEvaluatedIndirectLight"]
            checks["executionEdgesRetained"] = len(panel.view.graph_info()["edges"]) == 2
            long_node = nodes[-1]
            checks["longNameVisible"] = long_node._title.toPlainText() == "CombineSurfaceWithEvaluatedIndirectLight"
            checks["descriptionTextPreserved"] = long_node._description.toPlainText() == "Combine the surface with the evaluated light."
            result["screenshot"] = str(output.with_suffix(".png"))
            checks["imageSaved"] = host.grab().save(result["screenshot"])
            result["ok"] = all(checks.values())
        except Exception:
            result["error"] = traceback.format_exc()
        finally:
            if host is not None:
                host.close()
                host.deleteLater()
            with open(str(output), "w", encoding="utf-8") as stream:
                json.dump(result, stream, indent=2)
            QtCore.QTimer.singleShot(50, pyrenderdoc.GetMainWindow().Widget().close)


probe = CardsProbe()
sys.modules["__main__"]._bread_shader_cards_probe = probe
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(probe.run)
