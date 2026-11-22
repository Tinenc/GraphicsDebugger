from __future__ import annotations

import ast
import copy
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


REPLAYLAB_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = (
    REPLAYLAB_ROOT / "renderdoc_mcp_bridge" / "project_shader_flow_graph.py"
)


def _load_without_qt():
    spec = importlib.util.spec_from_file_location(
        "replaylab_project_shader_flow_graph_no_qt", MODULE_PATH
    )
    if spec is None or spec.loader is None:
        raise AssertionError("could not create the Shader flow-graph module spec")
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


def _load_graph_methods_with_qt_stub():
    """Exercise scheduling callbacks without requiring an embedded Qt host."""
    core = types.SimpleNamespace(Signal=lambda *args: None,
                                 QRectF=lambda *args: tuple(args),
                                 QPointF=lambda x, y: (x, y))
    widgets = types.SimpleNamespace(QGraphicsPathItem=object, QGraphicsObject=object,
                                    QGraphicsView=object, QWidget=object)
    package = types.SimpleNamespace(QtCore=core, QtGui=types.SimpleNamespace(), QtWidgets=widgets)
    spec = importlib.util.spec_from_file_location("shader_graph_qt_callback_contract", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"PySide2": package}):
        spec.loader.exec_module(module)
    return module


class ShaderFlowGraphComponentTests(unittest.TestCase):
    def test_call_view_positions_every_function_in_one_vertical_column(self):
        module = _load_graph_methods_with_qt_stub()
        bounds = types.SimpleNamespace(width=lambda: 232.0, height=lambda: 90.0)
        view = types.SimpleNamespace(
            _graph={"graphKind": "function-call", "nodes": [
                {"id": "caller"}, {"id": "first"}, {"id": "second"}]},
            _nodes={key: types.SimpleNamespace(boundingRect=lambda: bounds)
                    for key in ("caller", "first", "second")},
        )
        positions = module.ShaderFlowGraphView._automatic_positions(view)
        self.assertEqual([positions[key][0] for key in (
            "caller", "first", "second")], [-116.0] * 3)
        self.assertEqual([positions[key][1] for key in (
            "caller", "first", "second")], [0.0, 166.0, 332.0])

    def test_module_is_python_36_parseable_and_imports_without_qt(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        ast.parse(
            source,
            filename=str(MODULE_PATH),
            feature_version=(3, 6),
        )
        # qrenderdoc's embedded PySide2 exposes QApplication.palette as an
        # instance method. Keep palette lookup widget-bound so constructing the
        # graph works in the real host, not only in newer standalone bindings.
        self.assertNotIn("QApplication.palette()", source)
        module = _load_without_qt()
        self.assertFalse(module.HAS_QT)
        with self.assertRaisesRegex(RuntimeError, "PySide2|Qt"):
            module.ShaderFlowGraphPanel()

    def test_normalization_bounds_navigation_and_discards_active_payloads(self):
        module = _load_without_qt()
        graph = {
            "schema": module.FLOW_GRAPH_SCHEMA,
            "graphId": "shader-a:7",
            "workspaceId": "workspace-a",
            "sourceSha256": "a" * 64,
            "nodes": [
                {
                    "id": "main",
                    "name": "MainPS",
                    "summary": "Writes the final color.",
                    "source": {
                        "workspaceId": "workspace-a",
                        "documentId": "main.hlsl",
                        "path": "organized/main.hlsl",
                        "line": 27,
                        "column": 5,
                        "command": "do-not-forward",
                    },
                    "command": "also-do-not-forward",
                },
                {
                    "id": "lighting",
                    "label": "EvaluateLighting",
                    "description": "Combines direct and indirect light.",
                    "line": 4,
                },
            ],
            "edges": [
                {"from": "main", "to": "lighting", "kind": "calls"},
                {"source": "missing", "target": "main", "kind": "calls"},
            ],
        }

        result = module.normalize_flow_graph(
            graph, source_sha256="b" * 64
        )

        self.assertEqual(result["schema"], module.FLOW_GRAPH_SCHEMA)
        self.assertEqual(result["graphKey"], "shader-a:7")
        self.assertTrue(result["stale"])
        self.assertEqual([node["id"] for node in result["nodes"]], [
            "main", "lighting"
        ])
        self.assertEqual(result["edges"], [{
            "source": "main", "target": "lighting", "kind": "calls"
        }])
        navigation = result["nodes"][0]["navigation"]
        self.assertEqual(navigation["workspaceId"], "workspace-a")
        self.assertEqual(navigation["documentId"], "main.hlsl")
        self.assertEqual(navigation["line"], 27)
        self.assertEqual(navigation["column"], 5)
        self.assertNotIn("command", navigation)
        self.assertNotIn("command", result["nodes"][0])

    def test_normalization_caps_large_graphs_and_deduplicates_edges(self):
        module = _load_without_qt()
        nodes = [
            {"id": "n%d" % index, "label": "Node %d" % index}
            for index in range(module.MAX_GRAPH_NODES + 3)
        ]
        edges = [
            {"source": "n0", "target": "n1", "kind": "calls"}
            for _unused in range(module.MAX_GRAPH_EDGES + 7)
        ]

        result = module.normalize_flow_graph({"nodes": nodes, "edges": edges})

        self.assertEqual(len(result["nodes"]), module.MAX_GRAPH_NODES)
        self.assertEqual(len(result["edges"]), 1)
        self.assertTrue(result["nodesTruncated"])
        self.assertTrue(result["edgesTruncated"])
        self.assertEqual(result["omittedNodeCount"], 3)
        self.assertEqual(result["omittedEdgeCount"], 7)

    def test_explicit_dirty_state_marks_bound_graph_stale(self):
        module = _load_without_qt()
        graph = {
            "workspaceId": "workspace-a",
            "sourceRevision": 12,
            "sourceSha256": "a" * 64,
            "nodes": [{"id": "main", "label": "main"}],
            "edges": [],
        }
        current = module.normalize_flow_graph(
            graph, source_sha256="a" * 64, stale=False
        )
        dirty = module.normalize_flow_graph(
            graph, source_sha256="a" * 64, stale=True
        )
        self.assertFalse(current["stale"])
        self.assertTrue(dirty["stale"])
        self.assertEqual(current["graphKey"], "workspace-a:12")

    def test_backend_truncation_metadata_survives_the_ui_projection(self):
        module = _load_without_qt()
        result = module.normalize_flow_graph({
            "nodes": [{"id": "main", "label": "main"}],
            "edges": [],
            "truncated": {
                "nodes": True,
                "edges": False,
                "omittedNodes": 12,
                "omittedEdges": 0,
            },
        })
        self.assertTrue(result["nodesTruncated"])
        self.assertFalse(result["edgesTruncated"])
        self.assertEqual(12, result["omittedNodeCount"])

    def test_legacy_algorithm_stage_is_not_admitted_to_visible_qt_graph(self):
        module = _load_without_qt()
        graph = module.normalize_flow_graph({
            "graphKind": "algorithm-dataflow", "layoutDirection": "TD",
            "nodes": [{"id": "sample", "name": "MainPS", "label": "Sample the captured texture",
                       "kind": "computation", "source": {"line": 65, "column": 7}}],
            "edges": [],
        })
        self.assertEqual(graph["graphKind"], "algorithm-dataflow")
        self.assertEqual(graph["nodes"], [])
        self.assertEqual(graph["edges"], [])

    def test_algorithm_display_keeps_comment_above_actual_function_name(self):
        module = _load_without_qt()
        raw = {
            "graphKind": "algorithm-dataflow", "graphId": "accepted-graph",
            "nodes": [{"id": "sample", "name": "PrepareNormal", "label": "Sample the normal map",
                       "description": "Sample the normal map", "source": {"line": 74, "column": 5}}],
            "edges": [],
        }
        original = copy.deepcopy(raw)
        graph = module.normalize_flow_graph(raw)
        node = graph["nodes"][0]
        projected = copy.deepcopy(graph)
        self.assertEqual(module._node_title(node), "PrepareNormal")
        self.assertEqual(module._node_description(node, algorithm=True), "Sample the normal map")
        self.assertEqual(module._node_description(node, algorithm=False), node["description"])
        self.assertEqual(graph, projected)
        self.assertEqual(raw, original)
        self.assertEqual(node["navigation"]["line"], 74)
        self.assertEqual(node["symbol"], "PrepareNormal")

    def test_compact_algorithm_display_keeps_distinct_computation_evidence(self):
        module = _load_without_qt()
        node = {"label": "Sample the normal map", "description": "Use the captured tangent basis."}
        self.assertEqual(module._node_description(node, algorithm=True), node["description"])
        # Whitespace alone carries no additional description. Other text does.
        node["description"] = "Sample the\nnormal map"
        self.assertEqual(module._node_description(node, algorithm=True), "")
        node["description"] = "Sample the normal map; preserve its alpha."
        self.assertEqual(module._node_description(node, algorithm=True), node["description"])

    def test_fixed_nodes_cannot_emit_a_rearrangement_or_change_position_cache(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("ItemIsMovable", source)
        self.assertNotIn("_positions_by_graph", source)
        self.assertNotIn("_node_moved", source)
        self.assertIn("def wheelEvent", source)
        self.assertIn("self._panning = True", source)

    def test_function_expansion_is_deferred_past_native_mouse_dispatch(self):
        module = _load_graph_methods_with_qt_stub()
        view = types.SimpleNamespace(_subgraph_timer=mock.Mock(), subgraphRequested=mock.Mock(),
                                     _pending_subgraph_request=None, _click_timer=mock.Mock(),
                                     _pending_click_node=None)
        node = {"id": "call", "symbol": "Caller", "functionId": "function-callee",
                "navigation": {"anchorId": "native-mark-7", "symbol": "Caller", "line": 20}}
        module.ShaderFlowGraphView._expand_node(view, node)
        view._subgraph_timer.start.assert_called_once_with(0)
        view.subgraphRequested.emit.assert_not_called()
        module.ShaderFlowGraphView._emit_subgraph_request(view)
        payload = view.subgraphRequested.emit.call_args.args[0]
        self.assertEqual(payload["functionId"], "function-callee")
        self.assertEqual(payload["symbol"], "Caller")
        self.assertEqual(payload["anchorId"], "native-mark-7")
        self.assertIsNone(view._pending_subgraph_request)
        view._subgraph_timer.reset_mock()
        module.ShaderFlowGraphView._expand_node(view, {"id": "label-only", "symbol": "NotAFunction"})
        view._subgraph_timer.start.assert_not_called()

    def test_interface_metadata_is_preserved_without_interface_text_renderer(self):
        module = _load_without_qt()
        raw = {"id": "normal", "name": "PrepareNormal", "kind": "function-call", "io": {"reads": ["direction"],
            "resources": ["NormalTexture", "direction"], "writes": ["normal", "return"],
            "locals": ["temporary"]}, "navigation": {"snippet": "return normalize(direction);"}}
        node = module.normalize_flow_graph({"nodes": [raw]})["nodes"][0]
        self.assertEqual(node["io"], raw["io"])
        self.assertEqual(module._node_title(node), "PrepareNormal")
        self.assertFalse(hasattr(module, "_io_summary_lines"))

    def test_native_anchor_and_statement_identity_survive_inert_projection(self):
        module = _load_without_qt()
        graph = module.normalize_flow_graph({"nodes": [{"id": "stage", "name": "Helper",
            "functionId": "function-verified", "source": {"anchorId": "native-mark-9",
            "symbol": "Caller", "snippet": "value =\n  Compute(x);", "statementId": "abc",
            "functionSignature": "Caller(float x)", "command": "ignored"}}]})
        navigation = graph["nodes"][0]["navigation"]
        self.assertEqual(navigation["anchorId"], "native-mark-9")
        self.assertEqual(navigation["symbol"], "Caller")
        self.assertNotIn("snippet", navigation)
        self.assertEqual(navigation["functionSignature"], "Caller(float x)")
        self.assertEqual(graph["nodes"][0]["functionId"], "function-verified")
        self.assertNotIn("command", navigation)

    def test_function_interface_is_retained_without_implementation_text(self):
        module = _load_without_qt()
        graph = module.normalize_flow_graph({"entryPoint": "MainPS", "nodes": [{
            "id": "main", "name": "MainPS", "kind": "entry-point",
            "label": "MainPS", "description": "float hidden = uv.x;",
            "signature": "float4 MainPS(float2 uv) : SV_Target0",
            "inputs": ["uv"], "outputs": ["SV_Target0"],
            "functionId": "function-main", "source": {"line": 4, "column": 3,
                "snippet": "float hidden = uv.x;"}}], "edges": []})
        node = graph["nodes"][0]
        self.assertEqual(node["description"], "Unknown")
        self.assertEqual(node["functionId"], "function-main")
        self.assertNotIn("snippet", node["navigation"])
        self.assertEqual(node["io"]["reads"], ["uv"])
        self.assertEqual(node["io"]["writes"], ["SV_Target0"])

    def test_graph_back_forward_preserves_top_level_and_discards_abandoned_future(self):
        module = _load_without_qt()
        history = module.GraphNavigationHistory()
        root = {"graphId": "accepted", "nodes": [{"id": "stage"}]}
        history.reset(root, {"graph_key": "root"})
        root["nodes"][0]["id"] = "external-change"
        history.open({"graphId": "material"})
        history.open({"graphId": "math"})
        self.assertEqual(history.back()[0]["graphId"], "material")
        self.assertTrue(history.state()["canForward"])
        self.assertEqual(history.back()[0]["nodes"][0]["id"], "stage")
        self.assertFalse(history.state()["canBack"])
        self.assertEqual(history.forward()[0]["graphId"], "material")
        history.open({"graphId": "other-function"})
        self.assertFalse(history.state()["canForward"])
        self.assertEqual(history.back()[0]["graphId"], "material")
        self.assertEqual(history.back()[1]["graph_key"], "root")

    def test_direct_function_jump_back_uses_parent_not_last_visited_function(self):
        module = _load_without_qt()
        history = module.GraphNavigationHistory()
        root = {"graphId": "entry"}
        history.reset(root)
        history.open({"graphId": "unrelated"})
        history.open({"graphId": "selected-child"}, ancestors=[
            (root, {}), ({"graphId": "actual-parent"}, {})])
        self.assertEqual(history.back()[0]["graphId"], "actual-parent")
        self.assertEqual(history.back()[0]["graphId"], "entry")
        self.assertFalse(history.state()["canBack"])
        history.open({"graphId": "entry"})
        self.assertFalse(history.state()["canBack"])  # Do not duplicate the current function.

    def test_parent_resolution_failure_cannot_display_stale_graph_or_move_cursor(self):
        module = _load_graph_methods_with_qt_stub()
        history = module.GraphNavigationHistory()
        history.reset({"graphId": "old-parent"})
        history.open({"graphId": "current-child"})
        panel = types.SimpleNamespace(_history=history, _navigation_resolver=mock.Mock(return_value=None),
            view=types.SimpleNamespace(graph_info=lambda: {"graphId": "current-child"}),
            _show_history=mock.Mock())
        module.ShaderFlowGraphPanel._navigate_history(panel, -1)
        self.assertEqual(history.current()[0]["graphId"], "current-child")
        panel._show_history.assert_not_called()
        panel._navigation_resolver.return_value = ({"graphId": "fresh-parent"}, {})
        module.ShaderFlowGraphPanel._navigate_history(panel, -1)
        self.assertEqual(history.current()[0]["graphId"], "fresh-parent")
        panel._show_history.assert_called_once()

    def test_deep_source_ancestry_retains_each_parent_and_recursive_visit_closes_path(self):
        module = _load_without_qt()
        history = module.GraphNavigationHistory()
        parents = [({"graphId": str(index)}, {}) for index in range(70)]
        history.open({"graphId": "child"}, ancestors=parents)
        for index in reversed(range(70)):
            self.assertEqual(history.back()[0]["graphId"], str(index))
        history.open({"graphId": "branch"})
        history.open({"graphId": "0"})
        self.assertEqual(history.state()["length"], 1)

    def test_chat_context_retains_source_identity_without_mutable_graph_aliases(self):
        module = _load_graph_methods_with_qt_stub()
        graph = {"sourceSha256": "a" * 64, "nodes": [{"id": "n"}]}
        node = {"id": "n", "navigation": {"anchorId": "issued", "documentId": "doc"}}
        view = types.SimpleNamespace(contextRequested=mock.Mock(), graph_info=lambda: graph,
                                    graph_key=lambda: "doc:rev1")
        module.ShaderFlowGraphView._request_context(view, node)
        payload = view.contextRequested.emit.call_args.args[0]
        self.assertEqual(payload["node"]["navigation"], node["navigation"])
        self.assertEqual(payload["graphKey"], "doc:rev1")
        node["navigation"]["anchorId"] = "replaced"
        graph["nodes"].clear()
        self.assertEqual(payload["node"]["navigation"]["anchorId"], "issued")
        self.assertEqual(len(payload["graph"]["nodes"]), 1)

    def test_scene_range_signal_cannot_reenter_geometry_update(self):
        module = _load_graph_methods_with_qt_stub()
        scene = mock.Mock()
        scene.sceneRect.return_value = None
        view = types.SimpleNamespace(_scene=scene, _nodes={}, _updating_scene_rect=False)
        scene.setSceneRect.side_effect = lambda _bounds: module.ShaderFlowGraphView._update_scene_rect(view)
        module.ShaderFlowGraphView._update_scene_rect(view)
        scene.setSceneRect.assert_called_once()
        self.assertFalse(view._updating_scene_rect)

    def test_loop_back_edge_is_visible_without_distorting_forward_layers(self):
        module = _load_graph_methods_with_qt_stub()
        nodes = [{"id": name} for name in ("predicate", "body", "output")]
        edges = [{"source": "predicate", "target": "body", "kind": "true"},
                 {"source": "body", "target": "predicate", "kind": "loop"},
                 {"source": "predicate", "target": "output", "kind": "false"}]
        self.assertEqual(module.ShaderFlowGraphView._depths(nodes, edges),
                         {"predicate": 0, "body": 1, "output": 1})


if __name__ == "__main__":
    unittest.main()
