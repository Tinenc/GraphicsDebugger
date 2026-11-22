"""Navigation resolves current source, not cached function names or line numbers."""
import ast
from types import SimpleNamespace
from unittest import mock

from test_shader_workspace_panel import MODULE_PATH, _load_without_qt


workspace = _load_without_qt()


def methods():
    names = {"_resolve_flow_history", "_open_function_graph", "_bookmarked_function"}
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf8"))
    selected = [node for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(MODULE_PATH), "exec"),
         workspace.__dict__, namespace)
    return type("NavigationMethods", (), namespace)


def make_panel(source):
    index = workspace._shader_flow_hierarchy.SourceFlowHierarchy(source)
    binding = {"captureSessionId": "current"}
    document = {"available": True, "captureBinding": binding, "workspaceId": "doc",
                "flowAnchors": {"issued-parent": {"functionMarker": 19}},
                "editor": SimpleNamespace(GetLineAnchorLine=lambda _marker: 3)}
    panel = methods()()
    panel._current_document = lambda: document
    panel._current_capture_binding = lambda: binding
    panel._source_hierarchy = lambda _document: index
    panel._flow_graph_target_unavailable = mock.Mock()
    panel._anchor_graph = mock.Mock(side_effect=lambda document, graph, hierarchy, **kw: graph)
    panel._prune_flow_anchors = mock.Mock()
    panel.flow_graph = mock.Mock()
    return panel, document, index


def test_back_rebuilds_renamed_parent_using_moved_native_declaration_anchor():
    panel, document, index = make_panel("// inserted\n// purpose\nvoid Renamed(){}\nvoid Child(){}")
    old = {"graphId": "execution:Old", "functionName": "Old", "sourceSha256": "old",
           "nodes": [{"kind": "function-summary", "source": {"line": 1, "anchorId": "issued-parent"}}]}
    refreshed, options = panel._resolve_flow_history(old, {"stale": True})
    assert refreshed["functionName"] == "Renamed"
    assert refreshed["sourceSha256"] == index.source_sha256
    assert refreshed["nodes"][0]["source"]["line"] == 3
    assert "stale" not in options
    document["editor"].GetLineAnchorLine = lambda _marker: 0
    assert panel._resolve_flow_history(old, {}) is None
    panel._flow_graph_target_unavailable.assert_called_once()


def test_directory_jump_reconstructs_caller_chain_without_unrelated_history():
    panel, document, index = make_panel(
        "void Child(){}\nvoid Parent(){Child();}\nvoid Main(){Parent();}\nvoid Other(){}")
    root = index.project_root({"entryPoint": "Main"}, functions_only=True)
    document["sourceFlowGraph"] = root
    panel._open_function_graph(document, index, index.function_identity("Child"), direct=True)
    opened = panel.flow_graph.open_subgraph.call_args
    ancestors = opened.kwargs["ancestors"]
    assert [graph["functionId"] for graph, _ in ancestors] == [
        index.function_identity("Main"), index.function_identity("Parent")]
    assert opened.args[0]["functionId"] == index.function_identity("Child")
    panel._open_function_graph(document, index, index.function_identity("Other"), direct=True)
    assert panel.flow_graph.open_subgraph.call_args.kwargs["ancestors"] == []
