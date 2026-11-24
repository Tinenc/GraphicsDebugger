"""Flow references retain their exact source/capture while menus are open."""
import ast
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from test_shader_workspace_panel import MODULE_PATH, _load_without_qt
from test_frame_flow_panel import MODULE as FRAME_PATH, load_module, document as frame_document


workspace = _load_without_qt()
frame = load_module()


def shader_document():
    return {'workspaceId': 'a' * 64, 'documentToken': 'current-editor', 'available': True,
            'sourcePath': 'D:/project/source/shader.hlsl', 'dirty': True, 'rawSha256': 'b' * 64,
            'captureBinding': {'captureSessionId': 'session-a', 'capturePath': 'D:/capture.rdc'},
            'workspace': {'workspacePath': 'D:/project/workspace', 'captureId': 'c' * 64,
                          'shaderRawSha256': 'd' * 64, 'stage': 'fragment', 'entryPoint': 'Main',
                          'resourceId': 9223372036854775900}}


def test_calculation_reference_distinguishes_captured_shader_saved_and_unsaved_source():
    document = shader_document()
    source = 'float A() {\n return 1;\n}\nfloat B() {\n return 2;\n}'
    scope = {'id': 'lighting', 'title': '照明', 'description': '两段共同完成照明',
             'spans': [{'functionId': 'A', 'functionName': 'A', 'startLine': 1, 'endLine': 3},
                       {'functionId': 'B', 'functionName': 'B', 'startLine': 4, 'endLine': 6}]}
    context, facts = workspace._calculation_attachment(document, scope, source, 99, 100, [21, 22])
    assert context['capture']['eventId'] == 21
    assert context['capture']['selectedEventId'] == 100
    assert context['capture']['captureSessionId'] == 'session-a'
    assert context['shader']['rawSha256'] == 'd' * 64
    assert context['shader']['resourceId'] == '9223372036854775900'
    assert facts['sourceSha256'] == hashlib.sha256(source.encode('utf8')).hexdigest()
    assert facts['savedSourceSha256'] == 'b' * 64
    assert facts['unsavedChanges'] is True
    assert [span['functionName'] for span in facts['spans']] == ['A', 'B']
    assert 'excerpts' not in facts
    assert 'return 1;' not in json.dumps(facts)
    assert facts['sourceIncluded'] is False
    assert facts['readSource']['tool'] == 'read_shader_workspace_source'
    assert facts['capturePath'] == 'D:/capture.rdc'


def test_calculation_references_are_bounded_and_missing_ranges_are_rejected():
    source = '\n'.join('float v%d = 1;' % index for index in range(10000))
    scope = {'spans': [{'functionId': 'A', 'startLine': 1, 'endLine': 10000}] * 1000}
    context, facts = workspace._calculation_attachment(shader_document(), scope, source)
    assert len(facts['spans']) == 64
    assert facts['spanCount'] == 1000
    assert 'excerpts' not in facts
    assert 'float v0 = 1;' not in json.dumps(facts)
    with pytest.raises(ValueError, match='current source range'):
        workspace._calculation_attachment(shader_document(), {'spans': [{'startLine': 0, 'endLine': 3}]}, source)


def method(path, name, scope):
    tree = ast.parse(path.read_text('utf8'))
    node = next(item for item in ast.walk(tree) if isinstance(item, ast.FunctionDef) and item.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), 'exec'), scope)
    return scope[name]


@pytest.mark.parametrize('change', ['none', 'capture', 'editor', 'source', 'unavailable'])
def test_stage_attachment_rechecks_menu_snapshot_without_changing_editor_or_replay(change):
    document = shader_document()
    document.update(sourceFlowKey='revision-a', stageOutline=[{'id': 'lighting', 'title': 'Light'}])
    panel = SimpleNamespace(_current_document=lambda: document,
        _current_capture_binding=lambda: shader_document()['captureBinding'],
        _source_flow_key=lambda current: 'revision-b' if change == 'source' else 'revision-a',
        _add_calculation_to_chat=mock.Mock())
    if change == 'capture': document['captureBinding'] = {'captureSessionId': 'other'}
    if change == 'editor': document['documentToken'] = 'other-editor'
    if change == 'unavailable': document['available'] = False
    callback = method(MODULE_PATH, '_add_stage_to_chat', {})
    callback(panel, 'lighting', 'current-editor', 'revision-a')
    assert panel._add_calculation_to_chat.call_count == (1 if change == 'none' else 0)


def graph_panel(source, anchor, owner='Main'):
    index = workspace._shader_flow_hierarchy.SourceFlowHierarchy(source)
    record = index.functions[index.function_identity(owner)]
    document = shader_document()
    document.update(flowAnchors={'issued-anchor': anchor}, editor=SimpleNamespace(
        GetLineAnchorLine=lambda marker: marker))
    panel = SimpleNamespace(_current_document=lambda: document,
        _current_capture_binding=lambda: document['captureBinding'], _source_hierarchy=lambda unused: index,
        _bookmarked_function=lambda *unused: record, _add_calculation_to_chat=mock.Mock(),
        _flow_graph_target_unavailable=mock.Mock(),
        _function_line_span=lambda current, item: (
            source.count('\n', 0, current.tokens[item['declaration']].start()) + 1,
            source.count('\n', 0, current.tokens[item['end']].start()) + 1))
    return panel, index


def test_graph_attachment_resolves_current_callee_after_source_rename():
    source = 'float Renamed(float x){return x*2;}\nfloat Main(float x){\nreturn Renamed(x);\n}'
    panel, index = graph_panel(source, {'functionMarker': 1, 'marker': 3, 'expansionKind': 'call'})
    payload = {'node': {'id': 'old-node', 'symbol': 'OldName', 'functionId': 'obsolete',
                       'navigation': {'anchorId': 'issued-anchor'}},
               'graph': {'sourceSha256': 'old-source', 'graphId': 'old-graph'}}
    method(MODULE_PATH, '_flow_graph_context_requested', {})(panel, payload)
    scope = panel._add_calculation_to_chat.call_args.args[1]
    assert scope['functionName'] == 'Renamed'
    assert scope['functionId'] == index.function_identity('Renamed')
    assert scope['spans'][0]['startLine'] == 1
    assert scope['spans'][1]['startLine'] == 3
    panel._flow_graph_target_unavailable.assert_not_called()


@pytest.mark.parametrize('source,anchor_id', [
    ('float Main(float x){return x;}', 'missing-anchor'),
    ('float Main(float x){\nreturn x;\n}', 'issued-anchor')])
def test_graph_attachment_rejects_unissued_or_removed_call(source, anchor_id):
    panel, index = graph_panel(source, {'functionMarker': 1, 'marker': 2, 'expansionKind': 'call'})
    method(MODULE_PATH, '_flow_graph_context_requested', {})(panel,
        {'node': {'navigation': {'anchorId': anchor_id}}, 'graph': {}})
    panel._add_calculation_to_chat.assert_not_called()
    panel._flow_graph_target_unavailable.assert_called_once()


def test_helper_group_attachment_contains_exact_calls_and_current_definitions():
    source = 'float A(float x){return x*2;}\nfloat B(float x){return x+1;}\nfloat Main(float x){\nx=A(x);\nreturn B(x);\n}'
    anchor = {'functionMarker': 1, 'marker': 4, 'expansionKind': 'stage'}
    panel, index = graph_panel(source, anchor)
    index.helper_function_ids = {index.function_identity('A'), index.function_identity('B')}
    graph = index.function_graph(index.function_identity('Main'), functions_only=True)
    group = next(node for node in graph['nodes'] if node.get('helperGroupId'))
    anchor['helperGroupId'] = group['helperGroupId']
    method(MODULE_PATH, '_flow_graph_context_requested', {})(panel,
        {'node': {'id': group['id'], 'label': 'Helpers', 'navigation': {'anchorId': 'issued-anchor'}},
         'graph': graph})
    scope = panel._add_calculation_to_chat.call_args.args[1]
    assert scope['kind'] == 'shader-helper-group'
    assert {span['functionName'] for span in scope['spans']} == {'Main', 'A', 'B'}
    assert [span['startLine'] for span in scope['spans'] if span['functionName'] == 'Main'] == [4, 5]
    assert scope['graph']['sourceSha256'] == index.source_sha256


def prepared_frame(tmp_path):
    value = frame_document(tmp_path)
    stage = value['stages'][0]
    stage['images'][0].update(resourceId='ResourceId::500', semantic='场景照明',
        displayMapping={'mip': '2', 'slice': '3'}, sha256='e' * 64)
    stage['processGraph'] = {'nodes': [
        {'id': 'light', 'label': '计算光照', 'imageIndex': 0},
        {'id': 'compose', 'label': '合成', 'eventId': 12}],
        'edges': [{'from': 'light', 'to': 'compose', 'label': '照明颜色'}]}
    return frame.normalize_document(value)


def test_frame_texture_reference_keeps_stage_dependency_and_exact_subresource(tmp_path):
    document = prepared_frame(tmp_path)
    context, facts = frame.frame_flow_attachment(document,
        {'stageId': 'lighting', 'imageIndex': 0, 'nodeId': 'light'}, 99)
    assert context['capture']['eventId'] == 10
    assert context['capture']['selectedEventId'] == 99
    assert context['resource'] == {'kind': 'texture', 'resourceId': 'ResourceId::500',
                                  'name': 'HDR output', 'mip': 2, 'slice': 3}
    assert facts['eventIds'] == [8, 10, 12]
    assert facts['processGraph']['edges'] == [{'from': 'light', 'to': 'compose', 'label': '照明颜色'}]
    assert facts['images'][0]['sha256'] == 'e' * 64
    assert facts['images'][0]['navigation']['resourceId'] == 'ResourceId::500'
    assert 'path' not in facts['images'][0]
    assert facts['capturePath'] == document['capturePath']


@pytest.mark.parametrize('selection', [
    {'stageId': 'missing'}, {'stageId': 'lighting', 'nodeId': 'missing'},
    {'stageId': 'lighting', 'imageIndex': 100}, {'stageId': 'lighting', 'imageIndex': True},
    {'stageId': 'lighting', 'comparisonIndex': -1}])
def test_frame_reference_rejects_missing_or_changed_selection(tmp_path, selection):
    with pytest.raises(ValueError, match='unavailable'):
        frame.frame_flow_attachment(prepared_frame(tmp_path), selection)


def test_frame_reference_is_bounded_but_keeps_selected_media_and_snapshot_hash(tmp_path):
    document = prepared_frame(tmp_path)
    stage = document['stages'][0]
    original = copy.deepcopy(stage['images'][0])
    stage['images'] = [dict(original, title='Texture %d' % index, resourceId=str(100 + index))
                       for index in range(64)]
    stage['eventIds'] = list(range(1, 100001))
    context, facts = frame.frame_flow_attachment(document, {'stageId': 'lighting', 'imageIndex': 63})
    assert len(facts['eventIds']) == 256 and facts['eventCount'] == 100000
    assert len(facts['images']) == 16 and facts['images'][0]['imageIndex'] == 63
    assert context['resource']['resourceId'] == '163'
    assert facts['evidenceTruncated'] is True
    assert len(json.dumps(facts, ensure_ascii=False)) < 32000
    before = facts['stageSnapshotSha256']
    stage['summary'] = 'Changed analysis'
    assert frame.frame_flow_attachment(document, {'stageId': 'lighting'})[1]['stageSnapshotSha256'] != before


@pytest.mark.parametrize('change', ['none', 'replaced-document', 'capture'])
def test_frame_menu_is_bound_to_document_revision_and_capture(tmp_path, change):
    document = prepared_frame(tmp_path)
    panel = SimpleNamespace(document=document, ctx=SimpleNamespace(
        CurSelectedEvent=lambda: 99, AddTextToAIAssistant=mock.Mock()),
        _is_current=lambda: change != 'capture')
    payload = {'document': copy.deepcopy(document) if change == 'replaced-document' else document,
               'selection': {'stageId': 'lighting'}}
    scope = dict(frame.__dict__, _format=lambda text, value: text.replace('%1', value),
        QtCore=SimpleNamespace(QCoreApplication=SimpleNamespace(
        translate=lambda context, source: source)))
    method(FRAME_PATH, '_add_to_chat', scope)(panel, payload)
    assert panel.ctx.AddTextToAIAssistant.call_count == (1 if change == 'none' else 0)
