"""Navigation policy tests; native marker motion is tested in the Qt fixture."""
from __future__ import annotations

import ast
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2] / 'renderdoc_mcp_bridge'


def _module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HIERARCHY = _module('project_shader_flow_hierarchy')
HISTORY = _module('project_shader_flow_graph').GraphNavigationHistory
SOURCE = 'float Evaluate(float value) {\n  float result = value * 2;\n  return result;\n}\n'
BINDING = {'instanceId': 'owner', 'captureSessionId': 'session', 'capturePath': 'capture.rdc'}


class Editor:
    def __init__(self):
        self.source = SOURCE
        self.markers = {}
        self.next_marker = 0
        self.removed_markers = []
        self.cursor = None

    def GetText(self):
        return self.source

    def CreateLineAnchor(self, line):
        marker = self.next_marker
        self.next_marker += 1
        self.markers[marker] = line
        return marker

    def GetLineAnchorLine(self, marker):
        return self.markers.get(marker, 0)

    def RemoveLineAnchor(self, marker):
        self.markers.pop(marker, None)
        self.removed_markers.append(marker)

    def GoToLine(self, line, column):
        self.cursor = (line, column)


def panel(buffer=SOURCE, function='Evaluate', editor_type=Editor):
    source = (ROOT / 'project_shader_workspace.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    names = {'_function_line_span', '_anchor_graph', '_flow_token_anchor', '_flow_anchor_position',
             '_prune_flow_anchors',
             '_function_bookmark', '_bookmarked_function',
             '_flow_graph_node_activated', '_flow_graph_subgraph_requested', '_open_function_graph'}
    methods = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {'_shader_flow_hierarchy': HIERARCHY, 'json': json, 'copy': copy}
    exec(compile(ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[])), str(ROOT), 'exec'), namespace)
    cls = type('PanelHarness', (), {name: namespace[name] for name in names})
    value = cls()
    document = {'editor': editor_type(), 'workspaceId': 'workspace', 'documentToken': 1,
                'sourcePath': 'source/shader.hlsl', 'captureBinding': dict(BINDING),
                'available': True, 'textRevision': 0, 'dirty': False}
    value._current_document = lambda: document
    value._current_capture_binding = lambda: BINDING
    value.unavailable = []
    value.opened = []
    value._flow_graph_target_unavailable = lambda: value.unavailable.append(True)
    value.flow_graph = SimpleNamespace(_history=HISTORY())
    value.flow_graph.set_graph = lambda graph, **kwargs: value.flow_graph._history.reset(graph, kwargs)

    def open_subgraph(graph, **kwargs):
        value.opened.append(copy.deepcopy(graph))
        value.flow_graph._history.open(graph, kwargs)

    value.flow_graph.open_subgraph = open_subgraph
    # The real async delivery is covered in test_shader_source_flow_async.
    # These tests exercise navigation after a current-buffer result arrives.
    def source_hierarchy(document):
        try:
            return HIERARCHY.SourceFlowHierarchy(document['editor'].source)
        except ValueError:
            return None
    value._source_hierarchy = source_hierarchy
    def sync_flow(document):
        hierarchy = source_hierarchy(document)
        graph = copy.deepcopy(value._document_model(document['workspaceId'])['flowGraph'])
        value._anchor_graph(document, graph, hierarchy)
        document['flowProjectionIncomplete'] = hierarchy is None
        value.flow_graph.set_graph(graph)
    value._sync_flow_graph = sync_flow
    document['editor'].source = buffer
    hierarchy = value._source_hierarchy(document)
    function_id = hierarchy.function_identity(function)
    line = value._function_line_span(hierarchy, hierarchy.functions[function_id])[0]
    graph = {'graphId': 'accepted-root', 'graphKind': 'algorithm-dataflow',
             'sourceSha256': hierarchy.source_sha256, 'nodes': [
                 {'id': 'stage', 'name': function, 'source': {'line': line}}]}
    value._document_model = lambda workspace_id: {'flowGraph': graph}
    value._flow_graph_signature = None
    value._sync_flow_graph(document)
    issued = value.flow_graph._history.current()[0]
    payload = dict(issued['nodes'][0]['source'], functionId=function_id)
    return value, document, payload


def test_dirty_buffer_navigation_uses_moving_bookmark_not_accepted_hash_or_line():
    value, document, payload = panel()
    editor = document['editor']
    editor.source = '// new user line\n' + SOURCE
    editor.markers = {key: line + 1 for key, line in editor.markers.items()}
    document.update(textRevision=1, dirty=True, rawSha256='a-new-intentional-user-version')
    value._flow_graph_node_activated(payload)
    assert editor.cursor[0] == 2
    assert not value.unavailable


def test_target_text_can_change_without_invalidating_its_bookmark():
    value, document, payload = panel()
    document['editor'].source = SOURCE.replace('value * 2', 'value * 5')
    document.update(textRevision=1, dirty=True)
    value._flow_graph_node_activated(payload)
    assert document['editor'].cursor[0] == 1


def test_function_rename_keeps_the_bookmarked_declaration_and_body():
    value, document, payload = panel()
    document['editor'].source = SOURCE.replace('Evaluate', 'Unrelated')
    document.update(textRevision=1)
    value._flow_graph_node_activated(payload)
    assert document['editor'].cursor[0] == 1
    value._flow_graph_subgraph_requested(payload)
    assert value.opened[0]['functionName'] == 'Unrelated'


def test_whole_bookmarked_line_deletion_is_not_retargeted_to_nearby_statement():
    value, document, payload = panel()
    document['editor'].markers.clear()
    value._flow_graph_node_activated(payload)
    assert document['editor'].cursor is None
    assert value.unavailable


def test_unknown_anchor_and_other_capture_cannot_navigate_or_expand():
    value, document, payload = panel()
    value._flow_graph_node_activated(dict(payload, anchorId='forged'))
    value._flow_graph_subgraph_requested(dict(payload, anchorId='forged'))
    document['captureBinding'] = dict(BINDING, captureSessionId='previous')
    value._flow_graph_node_activated(payload)
    value._flow_graph_subgraph_requested(payload)
    assert document['editor'].cursor is None
    assert not value.opened


def test_expansion_uses_current_buffer_and_rejects_deleted_function():
    value, document, payload = panel()
    document['editor'].source = SOURCE.replace('value * 2', 'value * 5')
    document.update(textRevision=1, dirty=True)
    value._flow_graph_subgraph_requested(payload)
    assert value.opened and value.opened[0]['sourceSha256'] == value._source_hierarchy(document).source_sha256
    document['editor'].source = '// Function removed\n'
    document['editor'].markers.clear()
    document.update(textRevision=2)
    value._flow_graph_subgraph_requested(payload)
    assert len(value.opened) == 1
    assert value.unavailable


def test_old_project_graph_relocates_to_actual_function_body_not_old_line():
    value, document, _ = panel()
    hierarchy = value._source_hierarchy(document)
    graph = {'graphId': 'old-project-graph', 'sourceSha256': 'old-source', 'nodes': [
        {'id': 'stage', 'name': 'Evaluate', 'source': {'line': 400}}]}
    graph = hierarchy.project_root(graph)
    value._anchor_graph(document, graph, hierarchy)
    value._flow_graph_node_activated(graph['nodes'][0]['source'])
    assert document['editor'].cursor[0] == 1


def test_renamed_function_and_parameter_survive_tab_reprojection():
    value, document, payload = panel()
    document['editor'].source = SOURCE.replace('Evaluate', 'Renamed').replace('value', 'inputValue')
    document['textRevision'] = 1
    value._flow_graph_signature = ('another-tab',)
    value._sync_flow_graph(document)
    node = value.flow_graph._history.current()[0]['nodes'][0]
    assert node['source']['anchorId'] == payload['anchorId']
    assert node['source']['symbol'] == 'Renamed'
    value._flow_graph_node_activated(node['source'])
    value._flow_graph_subgraph_requested(node['source'])
    assert document['editor'].cursor[0] == 1
    assert value.opened[-1]['functionName'] == 'Renamed'


def test_graph_reprojected_during_incomplete_edit_recovers_without_another_tab_switch():
    value, document, payload = panel()
    document['editor'].source = SOURCE.replace('}', '')
    document['textRevision'] = 1
    value._flow_graph_signature = ('another-tab',)
    value._sync_flow_graph(document)
    assert document['flowProjectionIncomplete']
    assert not value.flow_graph._history.current()[0]['nodes'][0].get('functionId')
    document['editor'].source = SOURCE
    document['textRevision'] = 2
    value._sync_flow_graph(document)
    node = value.flow_graph._history.current()[0]['nodes'][0]
    assert not document['flowProjectionIncomplete']
    assert node['functionId'] and node['source']['anchorId'] == payload['anchorId']


CALL_SOURCE = ('float A(float x) {\n return x*2;\n}\n'
               'float B(float x) {\n return x*3;\n}\n'
               'float Main(float x) {\n return A(x);\n}\n')


def call_panel():
    value, document, root = panel(CALL_SOURCE, 'Main')
    value._flow_graph_subgraph_requested(root)
    node = next(node for node in value.opened[-1]['nodes']
                if node.get('kind') == 'function-call')
    return value, document, dict(node['source'], functionId=node['functionId'])


def test_changed_call_expands_current_callee_instead_of_old_target():
    value, document, payload = call_panel()
    document['editor'].source = CALL_SOURCE.replace('return A(x)', 'return B(x)')
    document['textRevision'] = 1
    value._flow_graph_subgraph_requested(payload)
    assert value.opened[-1]['functionName'] == 'B'
    assert not value.unavailable


def test_removed_call_is_rejected_even_when_old_callee_still_exists():
    value, document, payload = call_panel()
    document['editor'].source = CALL_SOURCE.replace('return A(x)', 'return x')
    document['textRevision'] = 1
    value._flow_graph_subgraph_requested(payload)
    assert len(value.opened) == 1
    assert value.unavailable


def test_deleted_call_marker_and_deleted_owner_cannot_expand_surviving_callee():
    for marker_name in ('marker', 'functionMarker'):
        value, document, payload = call_panel()
        anchor = document['flowAnchors'][payload['anchorId']]
        document['editor'].markers[anchor[marker_name]] = 0
        value._flow_graph_subgraph_requested(payload)
        assert len(value.opened) == 1
        assert value.unavailable


def test_ambiguous_same_line_calls_are_not_guessed():
    value, document, payload = call_panel()
    document['editor'].source = CALL_SOURCE.replace('return A(x)', 'return A(x) + B(x)')
    document['textRevision'] = 1
    value._flow_graph_subgraph_requested(payload)
    assert len(value.opened) == 1
    assert value.unavailable


def test_fresh_recreated_function_has_new_marker_while_old_history_stays_invalid():
    value, document, root = panel()
    value._flow_graph_subgraph_requested(root)
    previous = value.opened[-1]
    old_payload = previous['nodes'][0]['source']
    old_marker = document['flowAnchors'][old_payload['anchorId']]['functionMarker']
    document['editor'].markers[old_marker] = 0
    # Delete and recreate/undo can yield the exact same source hash and function ID.
    document['textRevision'] = 2
    hierarchy = value._source_hierarchy(document)
    current = hierarchy.function_graph(hierarchy.function_identity('Evaluate'))
    value._anchor_graph(document, current, hierarchy)
    fresh_payload = current['nodes'][0]['source']
    fresh_marker = document['flowAnchors'][fresh_payload['anchorId']]['functionMarker']
    assert fresh_payload['anchorId'] != old_payload['anchorId']
    assert fresh_marker != old_marker
    document['editor'].cursor = None
    value._flow_graph_node_activated(old_payload)
    assert value.unavailable and document['editor'].cursor is None
    value._flow_graph_node_activated(fresh_payload)
    assert document['editor'].cursor[0] == 1


def test_reprojected_same_function_releases_superseded_native_markers():
    value, document, root = panel()
    for index in range(80):
        document['editor'].source = SOURCE.replace('value * 2', 'value * %d' % (index + 3))
        document['textRevision'] = index + 1
        value._flow_graph_subgraph_requested(root)
    assert value.flow_graph._history.state()['length'] == 2
    # Reopening the same function updates its history entry, not a new ancestor.
    assert len(document['flowAnchors']) == 2
    assert len(document['editor'].markers) == len(document['flowAnchors']) + 1
    assert document['editor'].removed_markers
    abandoned = value.opened[-1]['nodes'][0]['source']
    document['editor'].source = SOURCE.replace('value * 2', 'value * 777')
    document['textRevision'] = 81
    value._flow_graph_subgraph_requested(root)
    assert abandoned['anchorId'] not in document['flowAnchors']
    value._flow_graph_node_activated(abandoned)
    assert value.unavailable


def test_hard_cap_expires_old_history_without_rebinding_it():
    value, document, root = panel()
    value._flow_graph_subgraph_requested(root)
    expired = value.opened[-1]['nodes'][0]['source']
    document['editor'].source = SOURCE.replace('value * 2', 'value * 5')
    document['textRevision'] = 1
    value._flow_graph_subgraph_requested(root)
    current = value.opened[-1]
    value._prune_flow_anchors(document, current, max_anchors=2)
    assert len(document['flowAnchors']) == 2
    assert len(document['editor'].markers) == 3
    value._flow_graph_node_activated(expired)
    value._flow_graph_subgraph_requested(expired)
    assert len(value.unavailable) == 2
    assert root['anchorId'] in document['flowAnchors']
    current_function = next(node for node in current['nodes']
                            if node.get('kind') == 'function-summary')
    assert current_function['source']['anchorId'] in document['flowAnchors']


def test_same_line_declarations_remain_ambiguous_after_bookmark_binding():
    source = 'float Evaluate(float value) { return value; } float Other() { return 1; }'
    value, document, root = panel(source)
    value._flow_graph_node_activated(root)
    value._flow_graph_subgraph_requested(root)
    assert len(value.unavailable) == 2
    assert document['editor'].cursor is None
