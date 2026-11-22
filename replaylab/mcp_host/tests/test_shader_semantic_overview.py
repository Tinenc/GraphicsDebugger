"""Host-prepared semantic sidecars resolve identically in the native UI model."""
import copy
import hashlib
import json

import pytest

from src.shader_workspace.semantic_overview import prepare_overview, save_overview
from test_shader_stage_outline import Hierarchy, outline


SOURCE = '''float Shared(float x) { return x * 0.5f; }
float Surface(float x) {
    float y = Shared(x);
    return y * 2;
}
float Lighting(float x) { return Shared(x) + 1; }
float Main(float x) { return Lighting(Surface(x)); }
'''


def stage(identity, title, function, **bounds):
    return {'id': identity, 'title': title, 'anchors': [dict(function=function, **bounds)]}


def test_host_and_ui_roundtrip_function_ranges_helpers_and_persistence(tmp_path):
    source_before = SOURCE
    data = prepare_overview(SOURCE, [
        dict(stage('surface', 'Surface', 'Surface'), helperSignatures=['Shared']),
        stage('lighting', 'Lighting', 'Lighting')], 'Main')
    path = save_overview(tmp_path, data)
    assert path == tmp_path / 'ai' / 'semantic-overview.json'
    assert json.loads(path.read_text('utf-8')) == data
    assert data['sourceSha256'] == hashlib.sha256(SOURCE.encode()).hexdigest()
    index = Hierarchy(SOURCE)
    resolved = outline.resolve_semantic_overview(index, data)
    assert resolved['unresolved'] == []
    rows = outline.build_stage_outline(index, 'Main', data)
    assert [row['title'] for row in rows] == ['Surface', 'Lighting']
    assert all(row['anchorState'] == 'exact' for row in rows)
    assert outline.stage_for_line(index, rows, 1, rows[1]['id']) == rows[1]['id']
    assert SOURCE == source_before and '@stage' not in SOURCE and '@overview' not in SOURCE


@pytest.mark.parametrize('source', ['\n\n' + SOURCE, SOURCE.replace('    float y', '\n    // A new note\n    float y'),
                                   SOURCE.replace('float y = Shared(x);\n', 'float y = Shared(x);')])
def test_range_reanchors_after_line_or_comment_edits(source):
    data = prepare_overview(SOURCE, [stage('scale', 'Scale', 'Surface', startLine=4, endLine=4)], 'Main')
    index = Hierarchy(source)
    resolved = outline.resolve_semantic_overview(index, data)
    assert not resolved['unresolved']
    row = resolved['rows'][0]
    span = row['spans'][0]
    assert 'return y * 2' in source.splitlines()[span['startLine'] - 1]
    assert row['anchorState'] == 'reanchored'


def test_body_edit_preserves_function_and_structurally_corresponding_range():
    data = prepare_overview(SOURCE, [stage('surface', 'Surface', 'Surface'),
        stage('scale', 'Scale', 'Surface', startLine=4, endLine=4)], 'Main')
    edited = SOURCE.replace('return y * 2', 'return y * 3')
    resolved = outline.resolve_semantic_overview(Hierarchy(edited), data)
    assert [row['semanticId'] for row in resolved['rows']] == ['surface', 'scale']
    assert resolved['unresolved'] == []


def test_unique_function_rename_preserves_stage_and_real_deletion_is_unresolved():
    data = prepare_overview(SOURCE, [stage('lighting', 'Lighting', 'Lighting')], 'Main')
    edited = SOURCE.replace('Lighting', 'Shade')
    result = outline.resolve_semantic_overview(Hierarchy(edited), data)
    assert len(result['rows']) == 1 and not result['unresolved']
    assert result['rows'][0]['spans'][0]['functionName'] == 'Shade'
    deleted = SOURCE.replace('float Lighting(float x) { return Shared(x) + 1; }', '')
    assert not outline.resolve_semantic_overview(Hierarchy(deleted), data)['rows']


def test_repeated_statement_occurrence_is_only_trusted_with_unchanged_function_body():
    source = '''float Main(float x) {
    x *= 2;
    x *= 2;
    return x;
}
'''
    data = prepare_overview(source, [stage('second', 'Second contribution', 'Main', startLine=3, endLine=3)], 'Main')
    moved = '\n' + source
    exact = outline.resolve_semantic_overview(Hierarchy(moved), data)
    assert exact['rows'][0]['spans'][0]['startLine'] == 4
    ambiguous = source.replace('return x;', 'x += 1;\n    return x;')
    result = outline.resolve_semantic_overview(Hierarchy(ambiguous), data)
    assert not result['rows'] and result['unresolved'][0]['reason'] == 'range-missing-or-ambiguous'


def test_sidecar_phase_groups_disjoint_spans_without_extra_generated_parent_rows():
    data = prepare_overview(SOURCE, [{'id': 'surface', 'title': 'Surface lighting', 'anchors': [
        {'function': 'Surface', 'startLine': 3, 'endLine': 3}, {'function': 'Lighting'}]}], 'Main')
    index = Hierarchy(SOURCE)
    rows = outline.build_stage_outline(index, 'Main', data)
    assert len(rows) == 1 and len(rows[0]['spans']) == 2
    assert outline.navigation_span(index, rows[0], 6)['functionName'] == 'Lighting'
    assert outline.navigation_span(index, rows[0], 3)['functionName'] == 'Surface'


def test_equal_titles_do_not_merge_different_branch_ranges():
    source = '''float Main(float x) {
    if (x > 0) {
        x *= 2;
    } else {
        x *= 3;
    }
    return x;
}
'''
    data = prepare_overview(source, [stage('positive', 'Lighting', 'Main', startLine=3, endLine=3),
                                    stage('negative', 'Lighting', 'Main', startLine=5, endLine=5)], 'Main')
    rows = outline.build_stage_outline(Hierarchy(source), 'Main', data)
    assert len(rows) == 2 and rows[0]['id'] != rows[1]['id']
    assert rows[0]['spans'][0]['contexts'][0]['kind'] == 'if'
    assert rows[1]['spans'][0]['contexts'][0]['kind'] == 'else'


def test_exact_duplicate_title_and_anchor_set_are_rejected_even_under_other_id():
    with pytest.raises(ValueError, match='same title and source anchors'):
        prepare_overview(SOURCE, [stage('one', 'Surface', 'Surface'), stage('two', 'Surface', 'Surface')], 'Main')


def test_overloaded_function_name_requires_an_explicit_signature():
    source = 'float F(float x){return x;}\nfloat F(float2 x){return x.x;}\nfloat Main(float x){return F(x);}'
    with pytest.raises(ValueError, match='one reachable source function'):
        prepare_overview(source, [stage('value', 'Value', 'F')], 'Main')
    data = prepare_overview(source, [{'id': 'value', 'title': 'Value',
                                    'anchors': [{'functionSignature': 'F(float x)'}]}], 'Main')
    resolved = outline.resolve_semantic_overview(Hierarchy(source), data)
    assert not resolved['unresolved'] and len(resolved['rows']) == 1
    rows = outline.build_stage_outline(Hierarchy(source), 'Main', data)
    assert len(rows) == 1 and rows[0]['provenance'] == 'semantic-overview'


def test_preexisting_duplicate_sidecar_rows_do_not_duplicate_the_directory():
    data = prepare_overview(SOURCE, [stage('surface', 'Surface', 'Surface')], 'Main')
    duplicate = copy.deepcopy(data['stages'][0])
    duplicate['id'] = 'old-duplicate'
    data['stages'].append(duplicate)
    resolved = outline.resolve_semantic_overview(Hierarchy(SOURCE), data)
    assert len(resolved['rows']) == 1
    assert resolved['unresolved'][0]['reason'] == 'duplicate-stage'


@pytest.mark.parametrize('anchor', [dict(function='Surface', startLine=1, endLine=2),
                                   dict(function='Surface', startLine=4, endLine=3),
                                   dict(function='Missing'), dict(function='Surface', startLine=True, endLine=4)])
def test_invalid_source_anchor_cannot_be_published(anchor):
    with pytest.raises(ValueError):
        prepare_overview(SOURCE, [{'id': 'bad', 'title': 'Bad', 'anchors': [anchor]}], 'Main')


def test_legacy_source_markers_do_not_override_new_semantic_sidecar():
    source = SOURCE.replace('    float y', '    // @stage[old] Old detail\n    float y')
    data = prepare_overview(source, [stage('surface', 'Surface', 'Surface')], 'Main')
    rows = outline.build_stage_outline(Hierarchy(source), 'Main', data)
    assert len(rows) == 1 and rows[0]['title'] == 'Surface'


def test_partial_invalid_sidecar_retains_valid_phases_without_synthetic_expansion():
    data = prepare_overview(SOURCE, [stage('surface', 'Surface', 'Surface'), stage('lighting', 'Lighting', 'Lighting')], 'Main')
    changed = SOURCE.replace('float Lighting(float x) { return Shared(x) + 1; }', '').replace('Lighting(Surface(x))', 'Surface(x)')
    rows = outline.build_stage_outline(Hierarchy(changed), 'Main', data)
    assert len(rows) == 1 and rows[0]['title'] == 'Surface'
