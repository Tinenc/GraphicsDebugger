"""Stage navigation follows current source responsibilities and exact ownership."""
import ast
from types import SimpleNamespace
from unittest import mock

import pytest
from src.shader_workspace.semantic_overview import prepare_overview

from test_shader_workspace_panel import MODULE_PATH, _load_without_qt


workspace = _load_without_qt()
outline = workspace._shader_stage_outline
Hierarchy = workspace._shader_flow_hierarchy.SourceFlowHierarchy

SOURCE = """// Shared adjustment.
float Shared(float x) { return x * 2; }
// Prepare the surface.
float Surface(float x) { return Shared(x); }
// Evaluate the illumination.
float Lighting(float x) { return Shared(x); }
// Compose the final appearance.
float Compose(float x) { return x + 1; }
float MainInner(float x) {
    float surface = Surface(x);
    float lighting = Lighting(surface);
    return Compose(lighting);
}
float Main(float x) { return MainInner(x); }
"""


def test_actual_orchestration_unwraps_interface_wrapper_and_preserves_stage_order():
    index = Hierarchy(SOURCE)
    stages = outline.build_stage_outline(index, "Main")
    assert [row['functionName'] for row in stages] == ['Surface', 'Lighting', 'Compose']
    assert [row['title'] for row in stages] == [
        'Prepare the surface.', 'Evaluate the illumination.', 'Compose the final appearance.']
    assert [row['order'] for row in stages] == [1, 2, 3]
    assert outline.stage_for_line(index, stages, 11) == stages[1]['id']
    assert outline.stage_for_line(index, stages, 4) == stages[0]['id']


def test_shared_helper_retains_selected_call_path_and_does_not_duplicate_the_shader():
    index = Hierarchy(SOURCE)
    stages = outline.build_stage_outline(index, "Main")
    assert outline.stage_for_line(index, stages, 2, stages[1]['id']) == stages[1]['id']
    assert outline.stage_for_line(index, stages, 2, stages[0]['id']) == stages[0]['id']
    assert outline.stage_for_line(index, stages, 2) == stages[0]['id']
    assert outline.stage_for_line(index, stages, 1) is None


def test_sections_belong_to_current_code_not_an_unbound_header_checklist():
    source = """// §1 Header that does not bind a stage.
// §2 Another unbound title.
float Main(float x) {
    // @stage Prepare sample coordinates
    float y = x * 2;
    // §2 Remap the sampled value
    return y * 3;
}
"""
    index = Hierarchy(source)
    stages = outline.build_stage_outline(index, 'Main')
    assert [row['title'] for row in stages] == [
        'Prepare sample coordinates', 'Remap the sampled value']
    assert [row['line'] for row in stages] == [4, 6]
    assert outline.stage_for_line(index, stages, 5) == stages[0]['id']
    assert outline.stage_for_line(index, stages, 7) == stages[1]['id']


def test_source_rewrite_moves_definitions_and_recomputes_responsibilities():
    first = Hierarchy(SOURCE)
    edited = Hierarchy('// Added header\n' + SOURCE.replace('Lighting', 'Shade'))
    stages = outline.build_stage_outline(edited, 'Main')
    assert stages[1]['functionName'] == 'Shade'
    assert stages[1]['line'] == 7
    assert stages[1]['sourceSha256'] != first.source_sha256
    assert outline.stage_for_line(edited, stages, 7) == stages[1]['id']


def test_repeated_phase_calls_share_a_row_without_merging_conditional_branches():
    source = 'float Pass(float x) { return x; }\nfloat Main(float x) {\n' \
             'x=Pass(x);\nif(x>0) { x=Pass(x); }\nreturn Pass(x);\n}\n'
    index = Hierarchy(source)
    stages = outline.build_stage_outline(index, 'Main')
    assert len(stages) == 2
    assert [outline.stage_for_line(index, stages, line) for line in (3, 4, 5)] == [
        stages[0]['id'], stages[1]['id'], stages[0]['id']]
    assert any(context['kind'] == 'if' for span in stages[1]['callSpans']
               for context in span['contexts'])


def test_repeated_composite_calls_do_not_duplicate_their_calculations():
    source = 'float Leaf(float x){return x;}\nfloat Pass(float x){return Leaf(x);}\n' \
             'float Main(float x){\nx=Pass(x);\nreturn Pass(x);\n}'
    stages = outline.build_stage_outline(Hierarchy(source), 'Main')
    assert [row['functionName'] for row in stages] == ['Leaf']
    assert {span['startLine'] for span in stages[0]['callSpans']} >= {2, 4, 5}


def test_invalid_or_unambiguous_entry_does_not_invent_techniques():
    index = Hierarchy('float Main() { return 1; }')
    assert outline.build_stage_outline(index, 'Absent') == []
    stages = outline.build_stage_outline(index, 'Main')
    assert len(stages) == 1 and stages[0]['title'] == 'Main'
    overloaded = Hierarchy('float Main(float x){return x;}\nfloat Main(float2 x){return x.x;}')
    assert outline.build_stage_outline(overloaded, 'Main') == []


def test_glsl_orchestration_has_the_same_stage_navigation_as_hlsl():
    source = SOURCE.replace('float Main(float x)', 'void main()').replace(
        'return MainInner(x);', 'float color = MainInner(1.0);').replace('float ', 'vec4 ')
    index = Hierarchy(source)
    stages = outline.build_stage_outline(index, 'main')
    assert [row['functionName'] for row in stages] == ['Surface', 'Lighting', 'Compose']
    assert outline.stage_for_line(index, stages, 6) == stages[1]['id']


def test_independent_overview_splits_one_function_into_real_calculations_without_graph_nodes():
    source = '''// @overview coords: Reconstruct coordinates
// @overview sample: Decode the sampled value
// @overview missing: This has no source anchor
float Main(float x) {
    // @stage[coords] Reconstruct coordinates
    float uv = x * 2;
    // @stage[sample] Decode the sampled value
    return uv * uv;
}
'''
    index = Hierarchy(source)
    index.function_graph = mock.Mock(side_effect=AssertionError('Overview must not copy graph nodes'))
    stages = outline.build_stage_outline(index, 'Main')
    assert [row['annotationId'] for row in stages] == ['coords', 'sample']
    assert [row['functionName'] for row in stages] == ['Main', 'Main']
    assert [row['title'] for row in stages] == ['Reconstruct coordinates', 'Decode the sampled value']
    assert [outline.stage_for_line(index, stages, line) for line in (6, 8)] == [
        stages[0]['id'], stages[1]['id']]
    assert all(row['provenance'] == 'source-annotation' for row in stages)


def test_one_calculation_can_span_several_functions_with_context_appropriate_navigation():
    source = '''// @overview remap: Remap the coordinate and its result
float Finish(float value) {
    // @stage[remap] Convert the result
    return value * 2;
}
float Main(float value) {
    // @stage[remap] Prepare the coordinate
    float uv = value + 1;
    return Finish(uv);
}
'''
    index = Hierarchy(source)
    stages = outline.build_stage_outline(index, 'Main')
    assert len(stages) == 1 and len(stages[0]['spans']) == 2
    assert [span['functionName'] for span in stages[0]['spans']] == ['Main', 'Finish']
    assert outline.navigation_span(index, stages[0], 8)['functionName'] == 'Main'
    assert outline.navigation_span(index, stages[0], 4)['functionName'] == 'Finish'
    assert outline.stage_for_line(index, stages, 4) == outline.stage_for_line(index, stages, 8)


def test_semantic_overview_order_is_independent_of_function_definition_order():
    source = '''// @overview initial: Initial conversion
// @overview final: Final adjustment
float Finish(float x) {
    // @stage[final] Final adjustment
    return x * 2;
}
float Prepare(float x) {
    // @stage[initial] Initial conversion
    return x + 1;
}
float Main(float x) { return Finish(Prepare(x)); }
'''
    stages = outline.build_stage_outline(Hierarchy(source), 'Main')
    assert [row['annotationId'] for row in stages] == ['initial', 'final']


def test_branch_and_loop_annotations_keep_their_own_ranges_and_execution_conditions():
    source = '''float Main(float x) {
    if (x > 0) {
        // @stage[adjust] Adjust only positive values
        x *= 2;
    }
    x = x;
    for (int i = 0; i < 3; ++i) {
        // @stage[accumulate] Accumulate the repeated contribution
        x += i;
    }
    return x;
}
'''
    index = Hierarchy(source)
    stages = outline.build_stage_outline(index, 'Main')
    assert [row['spans'][0]['endLine'] for row in stages] == [5, 10]
    assert [row['spans'][0]['contexts'][0]['kind'] for row in stages] == ['if', 'for']
    assert outline.stage_for_line(index, stages, 6) is None
    assert outline.stage_for_line(index, stages, 11) is None


def test_legacy_fallback_keeps_main_responsibilities_and_associates_supporting_helpers():
    source = '''// Reconstruct a distance.
float Distance(float x) { return x * 2; }
// Decode the sample.
float Decode(float x) { return x * x; }
float Surface(float x) { return Decode(Distance(x)); }
// Evaluate the contribution.
float Contribution(float x) { return x + 1; }
float Lighting(float x) { return Contribution(x); }
float Main(float x) { return Lighting(Surface(x)); }
'''
    index = Hierarchy(source)
    index.function_graph = mock.Mock(side_effect=AssertionError('No diagram dependency'))
    stages = outline.build_stage_outline(index, 'Main')
    assert [row['functionName'] for row in stages] == ['Surface', 'Lighting']
    assert outline.stage_for_line(index, stages, 2) == stages[0]['id']
    assert outline.stage_for_line(index, stages, 7) == stages[1]['id']
    assert all(row['provenance'] == 'source-derived' for row in stages)


def test_legacy_fallback_does_not_promote_each_ordinary_comment_into_a_stage():
    source = '''float Main(float x) {
    // Reconstruct the coordinate.
    float uv = x * 2;
    // Adjust the value range.
    return uv + 1;
}
'''
    stages = outline.build_stage_outline(Hierarchy(source), 'Main')
    assert [row['title'] for row in stages] == ['Main']
    assert [row['functionName'] for row in stages] == ['Main']


def test_unreachable_annotations_do_not_become_fake_overview_stages():
    source = '''// @overview absent: Unused calculation
float Unused(float x) {
    // @stage[absent] Unused calculation
    return x * 2;
}
float Main(float x) { return x; }
'''
    stages = outline.build_stage_outline(Hierarchy(source), 'Main')
    assert len(stages) == 1 and stages[0]['functionName'] == 'Main'
    assert stages[0]['provenance'] == 'source-derived'


def _activation_method():
    tree = ast.parse(MODULE_PATH.read_text('utf8'))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == '_stage_activated')
    scope = dict(workspace.__dict__, QtCore=SimpleNamespace(Qt=SimpleNamespace(UserRole=256)))
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(MODULE_PATH), 'exec'), scope)
    return scope['_stage_activated']


@pytest.mark.parametrize('stale', ['', 'capture', 'source', 'unavailable'])
def test_directory_activation_links_exact_current_definition_and_function_graph(stale):
    index = Hierarchy(SOURCE)
    stages = outline.build_stage_outline(index, 'Main')
    editor = mock.Mock()
    editor.GetCursorLine.return_value = 1
    document = {'available': True, 'captureBinding': 'current', 'sourceFlowKey': 'revision',
                'stageOutline': stages, 'hierarchy': index, 'workspaceId': 'workspace', 'editor': editor}
    panel = SimpleNamespace(_current_document=lambda: document,
        _current_capture_binding=lambda: 'current', _source_flow_key=lambda d: 'revision',
        _anchor_graph=mock.Mock(), _toggle_flow_graph=mock.Mock(), flow_graph=mock.Mock(),
        _prune_flow_anchors=mock.Mock(), _sync_stage_cursor=mock.Mock(), _open_function_graph=mock.Mock())
    if stale == 'capture': document['captureBinding'] = 'other'
    if stale == 'source': document['sourceFlowKey'] = 'old-revision'
    if stale == 'unavailable': document['available'] = False
    _activation_method()(panel, SimpleNamespace(data=lambda role: stages[1]['id']))
    if stale:
        editor.GoToLine.assert_not_called()
        panel._open_function_graph.assert_not_called()
    else:
        editor.GoToLine.assert_called_once_with(6, 7)
        panel._open_function_graph.assert_called_once_with(document, index, stages[1]['functionId'], direct=True)
        assert document['activeStage'] == stages[1]['id']


def test_partial_annotations_preserve_other_reachable_calculations():
    source = """float A(float x) {
    // @stage[first] First operation
    return x * 2;
}
// Essential second operation.
float B(float x) { return x * 3; }
float Main(float x) {
    x = A(x);
    return B(x);
}
"""
    index = Hierarchy(source)
    stages = outline.build_stage_outline(index, 'Main')
    first = next(row for row in stages if row['title'] == 'First operation')
    second = next(row for row in stages if row['functionName'] == 'B')
    assert stages.index(first) < stages.index(second)
    assert outline.stage_for_line(index, stages, 6) == second['id']
    assert outline.stage_for_line(index, stages, 9) == second['id']


def test_inline_semantic_directory_uses_current_range_column_not_function_start():
    for prefix in ('', '/* 中文 */\t'):
        source = 'float Main(float x) { x += 1; ' + prefix + 'x += 2; return x; }'
        index = Hierarchy(source)
        record = index.functions[index.function_identity('Main')]
        overview = {'version': 1, 'stages': [{'id': 'second', 'title': 'Second', 'anchors': [
            {'kind': 'range', 'functionSignature': outline._signature(record['signature']),
             'tokenSignature': 'x += 2 ;'}]}]}
        stages = outline.build_stage_outline(index, 'Main', overview)
        editor = mock.Mock()
        editor.GetCursorLine.return_value = 1
        document = {'available': True, 'captureBinding': 'current', 'sourceFlowKey': 'revision',
                    'stageOutline': stages, 'hierarchy': index, 'editor': editor}
        panel = SimpleNamespace(_current_document=lambda: document,
            _current_capture_binding=lambda: 'current', _source_flow_key=lambda d: 'revision',
            _toggle_flow_graph=mock.Mock(), _open_function_graph=mock.Mock(), _sync_stage_cursor=mock.Mock())
        _activation_method()(panel, SimpleNamespace(data=lambda role: 'semantic:second'))
        editor.GoToLine.assert_called_once_with(1, len(source[:source.index('x += 2')].expandtabs(4)) + 1)


def test_call_result_arithmetic_retains_its_own_commented_calculation():
    source = """// Sample.
float A(float x) { return x; }
float Main(float x) {
    // Convert intensity.
    return A(x) * 2;
}
"""
    index = Hierarchy(source)
    overview = prepare_overview(source, [
        {'id': 'sample', 'title': 'Sample', 'anchors': [{'function': 'A'}]},
        {'id': 'convert', 'title': 'Convert intensity.',
         'anchors': [{'function': 'Main', 'startLine': 4, 'endLine': 5}]}], 'Main')
    stages = outline.build_stage_outline(index, 'Main', overview)
    conversion = next(row for row in stages if row['title'] == 'Convert intensity.')
    assert conversion['functionName'] == 'Main'
    assert any(span['startLine'] <= 5 <= span['endLine'] for span in conversion['spans'])
    assert outline.stage_for_line(index, stages, 5, conversion['id']) == conversion['id']


def test_distinct_uncommented_blocks_preserve_execution_order_between_calls():
    source = """// First.
float A(float x) { return x + 1; }
// Second.
float B(float x) { return x + 2; }
float Main(float x) {
    float y = x * 2;
    y = A(y);
    y = y * 3;
    y = B(y);
    return y * 4;
}
"""
    index = Hierarchy(source)
    overview = prepare_overview(source, [
        {'id': 'prepare', 'title': 'Prepare', 'anchors': [{'function': 'Main', 'startLine': 6, 'endLine': 6}]},
        {'id': 'first', 'title': 'First', 'anchors': [{'function': 'A'}]},
        {'id': 'adjust', 'title': 'Adjust', 'anchors': [{'function': 'Main', 'startLine': 8, 'endLine': 8}]},
        {'id': 'second', 'title': 'Second', 'anchors': [{'function': 'B'}]},
        {'id': 'output', 'title': 'Output', 'anchors': [{'function': 'Main', 'startLine': 10, 'endLine': 10}]}], 'Main')
    stages = outline.build_stage_outline(index, 'Main', overview)
    assert [row['functionName'] for row in stages] == ['Main', 'A', 'Main', 'B', 'Main']
    own = [row for row in stages if row['functionName'] == 'Main']
    assert len({row['id'] for row in own}) == 3
    assert [outline.stage_for_line(index, stages, line) for line in (6, 8, 10)] == [
        row['id'] for row in own]


def test_unreachable_callers_do_not_claim_a_reachable_stage():
    source = """// Sample.
float A(float x) { return x; }
float Main(float x) { return A(x); }
float Unused(float x) { return A(x); }
"""
    index = Hierarchy(source)
    stages = outline.build_stage_outline(index, 'Main')
    assert outline.stage_for_line(index, stages, 3) == stages[0]['id']
    assert outline.stage_for_line(index, stages, 4) is None
    assert all(span['functionName'] != 'Unused' for row in stages
               for span in row.get('callSpans', []))


def test_repeated_call_locations_are_kept_once_each():
    source = """float A(float x) { return x * 2; }
float Main(float x) {
    x = A(x);
    return A(x);
}
"""
    stages = outline.build_stage_outline(Hierarchy(source), 'Main')
    assert len(stages) == 1
    spans = stages[0].get('callSpans', [])
    keys = [(span['functionId'], span['startLine'], span['endLine']) for span in spans]
    assert len(keys) == len(set(keys))
    assert {span['startLine'] for span in spans} == {3, 4}


def test_commented_initialization_and_output_are_real_calculation_sections():
    source = """float Main(float x) {
    // Initialize surface parameters.
    float y = x;
    // Write output.
    return y;
}
"""
    index = Hierarchy(source)
    overview = prepare_overview(source, [
        {'id': 'prepare', 'title': 'Initialize surface parameters.',
         'anchors': [{'function': 'Main', 'startLine': 3, 'endLine': 3}]},
        {'id': 'output', 'title': 'Write output.',
         'anchors': [{'function': 'Main', 'startLine': 5, 'endLine': 5}]}], 'Main')
    stages = outline.build_stage_outline(index, 'Main', overview)
    assert [row['title'] for row in stages] == [
        'Initialize surface parameters.', 'Write output.']
    assert outline.stage_for_line(index, stages, 3) == stages[0]['id']
    assert outline.stage_for_line(index, stages, 5) == stages[1]['id']


def test_ordinary_inline_caveats_do_not_split_an_existing_function_responsibility():
    source = '''// Decode the material response.
float Decode(float x) {
    float y = x * 2;
    // Preserve the original negative-value rounding behaviour.
    y = floor(y);
    // The scaling must happen after rounding.
    return y * 3;
}
float Main(float x) { return Decode(x); }
'''
    stages = outline.build_stage_outline(Hierarchy(source), 'Main')
    assert len(stages) == 1
    assert stages[0]['title'] == 'Decode the material response.'
    assert stages[0]['spans'][0]['endLine'] == 8


def test_repeated_sample_helpers_remain_inside_their_parent_calculation():
    source = '''float Tap(float x) { return x; }
// Filter the sampled signal.
float Filter(float x) {
    float y = Tap(x) * 0.5;
    y += Tap(x + 1) * 0.5;
    return y;
}
float Main(float x) { return Filter(x); }
'''
    index = Hierarchy(source)
    stages = outline.build_stage_outline(index, 'Main')
    assert len(stages) == 1 and stages[0]['functionName'] == 'Filter'
    assert outline.stage_for_line(index, stages, 1) == stages[0]['id']


def test_explicit_calculation_span_wins_over_an_enclosing_caller_stage():
    source = '''// @overview outer: Compose the result
// @overview inner: Decode the sample
float Sample(float x) {
    // @stage[inner] Decode the sample
    return x * 2;
}
float Main(float x) {
    // @stage[outer] Compose the result
    return Sample(x) + 1;
}
'''
    index = Hierarchy(source)
    stages = outline.build_stage_outline(index, 'Main')
    assert len(stages) == 2
    assert outline.stage_for_line(index, stages, 5, stages[0]['id']) == stages[1]['id']
