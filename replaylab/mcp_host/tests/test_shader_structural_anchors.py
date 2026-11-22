"""Cross-language relocation through the actual persistent snapshot/UI adapters."""
import ast
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import time

import pytest

from test_shader_visual_snapshots import snapshots, make, resolve, byte_selection, IDENTITY
from test_shader_outline_live_anchors import Hierarchy, outline, live_module, Editor
from src.shader_workspace.semantic_overview import prepare_overview

BRIDGE = Path(__file__).resolve().parents[2] / 'renderdoc_mcp_bridge'
spec = importlib.util.spec_from_file_location('navigation_history_test', str(BRIDGE / 'shader_navigation_history.py'))
history = importlib.util.module_from_spec(spec)
spec.loader.exec_module(history)


def picked(old, new, selection, expected=None, occurrence=0):
    snapshot = snapshots.make_snapshot(old, byte_selection(old, selection, occurrence), IDENTITY)
    saved = copy.deepcopy(snapshot)
    result = resolve(snapshot, new)
    assert result['status'] == 'resolved', result
    assert result['selection']['expression'] == (expected or selection)
    assert snapshot == saved  # relocation never rewrites immutable input
    return result['selection']


@pytest.mark.parametrize('typ', ['float', 'float3', 'vec3', 'half3', 'uint'])
def test_external_local_rename_and_literal_edit(typ):
    old = typ + ' Main(' + typ + ' x){' + typ + ' result=x*2;return result;}'
    new = old.replace('result', 'lighting').replace('*2', '*3')
    picked(old, new, 'result', 'lighting', occurrence=1)


def test_parameter_and_function_rename_together():
    old = 'float Main(float x){float result=x*2;return result;}'
    new = old.replace('Main', 'Shade').replace(' x', ' value').replace('=x', '=value')
    picked(old, new, 'result', occurrence=1)


@pytest.mark.parametrize('callee', ['mix', 'lerp', 'smoothstep'])
def test_call_parameter_occurrence_uses_expression_role(callee):
    old = 'float Main(float a,float t){return ' + callee + '(a,a,t);}'
    new = old.replace(',t);', ',t+0.25);')
    # The second a is a distinct source use, even though both bind to one declaration.
    at = old.index('(a,a,') + 3
    record = snapshots.make_snapshot(old, {'byteStart':at,'byteEnd':at+1}, IDENTITY)
    resolved = resolve(record, new)
    assert resolved['status'] == 'resolved', resolved
    result = resolved['selection']
    assert result['byteStart'] == new.index('(a,a,') + 3


def test_repeated_member_call_arguments_require_the_same_callee():
    old = 'float Main(float a,float t){return surface.mix(a,a,t);}'
    # Select the second argument without counting characters in "Main" etc.
    at = old.index('(a,a,') + 3
    record = snapshots.make_snapshot(old, {'byteStart':at,'byteEnd':at+1}, IDENTITY)
    assert resolve(record, old.replace(',t);', ',t+0.25);'))['status'] == 'resolved'
    changed = old.replace('surface.mix', 'other.mix').replace(',t);', ',t+0.25);')
    assert resolve(record, changed)['status'] == 'unresolved'


def test_selected_call_tracks_changed_arguments_and_callee():
    old = 'float Main(float x){float result=lerp(x,0.5,0.2);return result;}'
    new = old.replace('lerp(x,0.5,0.2)', 'mix(x,0.8,0.25)')
    picked(old, new, 'lerp(x,0.5,0.2)', 'mix(x,0.8,0.25)')


def test_wrapping_an_existing_use_keeps_its_declaration():
    old = 'float Main(float x){return x;}'
    new = old.replace('return x;', 'return saturate(x);')
    picked(old, new, 'x', occurrence=1)


def test_statement_move_and_initializer_update():
    old = 'float Main(float x){float a=x*2;float b=x+3;float c=x-4;return a+b+c;}'
    new = 'float Main(float x){float b=x+3;float c=x-4;float a=x*5;return a+b+c;}'
    picked(old, new, 'x*2', 'x*5')


@pytest.mark.parametrize('changed', [
    'float Main(float x){float a=x*2;float b=x*2;return a+b;}',
    'float Main(float x){float a=x*2;return x;}',
    'float Main(float x){if(x>0){float a=x*2;return a;}return x;}',
    'float Main(int x){float result=x*2;return result;}',
    'float Main(float x){float result=x*2;return result;}float Main(float x){return x;}',
])
def test_ambiguous_binding_scope_or_overload_never_guesses(changed):
    old = 'float Main(float x){float result=x*2;return result;}'
    assert resolve(make(old, 'result;'), changed)['status'] == 'unresolved'


def test_struct_member_and_call_role_survive_parameter_rename():
    old = 'struct S{float v;};float Main(S s,float t){return mix(s.v,t,s.v);}'
    new = old.replace('S s', 'S surface').replace('s.v', 'surface.v')
    picked(old, new, 's.v', 'surface.v', occurrence=1)


def test_unrelated_member_rename_is_not_inferred_from_same_byte_offset():
    old = 'struct S{float a;float b;};float Main(S s){return s.a;}'
    assert resolve(make(old, 's.a'), old.replace('return s.a', 'return s.b'))['status'] == 'unresolved'


def test_annotation_reopen_tracks_multiline_range_rename_and_comments():
    old = 'float Main(float x){\nfloat r=x*2;\nr+=x;\nreturn r;\n}'
    overview = prepare_overview(old, [{'id':'lighting','title':'光照','description':'原注释',
        'anchors':[{'function':'Main','startLine':2,'endLine':3}]}], 'Main')
    new = old.replace('float r', '// 中文说明 🌈\nfloat light').replace('r+=', 'light+=').replace('return r', 'return light').replace('*2', '*3')
    resolved = outline.resolve_semantic_overview(Hierarchy(new), overview)
    assert not resolved['unresolved']
    assert resolved['rows'][0]['description'] == '原注释'
    assert resolved['rows'][0]['spans'][0]['startLine'] == 3
    assert resolved['rows'][0]['spans'][0]['endLine'] == 4


def test_projected_flow_function_description_keeps_review_hash_stale():
    old = 'float Lighting(float x){return x*2;}float Main(float x){return Lighting(x);}'
    overview = prepare_overview(old, [{'id':'light','title':'光照计算','description':'计算光照',
        'anchors':[{'function':'Lighting'}]}], 'Main')
    new = old.replace('Lighting', 'Shade').replace('*2', '*3')
    index = Hierarchy(new)
    projected = outline.reanchor_semantic_overview(index, overview)
    assert projected['sourceSha256'] == overview['sourceSha256'] != index.source_sha256
    index.set_function_annotations(projected)
    assert index.function_description(index.function_identity('Shade')) == '计算光照'


def test_legacy_overview_without_token_history_still_loads_and_renames_unchanged_body():
    old = 'float Main(float x){return x*2;}'
    overview = prepare_overview(old, [{'id':'m','title':'Main','anchors':[{'function':'Main'}]}], 'Main')
    overview.pop('navigationBodies')
    result = outline.resolve_semantic_overview(Hierarchy(old.replace('Main', 'Shade')), overview)
    assert result['rows'][0]['spans'][0]['functionName'] == 'Shade'


def test_navigation_checkpoint_reopen_and_discard_boundaries(tmp_path):
    path = tmp_path / '源码.hlsl'
    old = 'float Main(float x){\nfloat r=x*2;\nreturn r;\n}'
    overview = prepare_overview(old, [{'id':'m','title':'说明','anchors':[{'function':'Main','startLine':2,'endLine':2}]}], 'Main')
    identity = dict(IDENTITY, path=str(path), workspace=str(tmp_path))
    store = history.NavigationHistory(str(tmp_path / 'cache'), identity)
    assert store.read(overview) is None and not Path(store.io.path).exists()
    path.write_text(old, encoding='utf8')
    editor = Editor(old)
    live = live_module.LiveOutlineAnchors(editor)
    projected = live.update(Hierarchy(old), overview)
    assert store.write(overview, projected, old)
    new = old.replace('r=x*2', 'light=x*3').replace('return r', 'return light')
    updated = outline.reanchor_semantic_overview(Hierarchy(new), projected)
    assert not store.write(overview, updated, new)  # editor content not committed
    assert store.read(overview) == projected
    path.write_text(new, encoding='utf8')
    assert store.write(overview, updated, new)
    reopened = history.NavigationHistory(str(tmp_path / 'cache'), identity).read(overview)
    assert outline.resolve_semantic_overview(Hierarchy(new), reopened)['rows']
    stale = copy.deepcopy(overview)
    stale['stages'][0]['title'] = 'A newer annotation head'
    assert store.read(stale) is None


def test_saved_deleted_navigation_identity_does_not_revive_from_identical_text(tmp_path):
    source = 'float Main(float x){return x*2;}'
    overview = prepare_overview(source, [{'id':'m','title':'M','anchors':[{'function':'Main'}]}], 'Main')
    path = tmp_path / 'test.hlsl'; path.write_text(source, encoding='utf8')
    store = history.NavigationHistory(str(tmp_path / 'cache'), dict(IDENTITY, path=str(path)))
    deleted = copy.deepcopy(overview)
    deleted['stages'][0]['anchors'][0]['functionSignature'] = ''
    assert store.write(overview, deleted, source)
    loaded = store.read(overview)
    assert not outline.resolve_semantic_overview(Hierarchy(source), loaded)['rows']


def test_navigation_corruption_is_disposable_and_cannot_replace_annotation_text(tmp_path):
    source = 'float Main(float x){return x;}'
    overview = prepare_overview(source, [{'id':'m','title':'M','anchors':[{'function':'Main'}]}], 'Main')
    path=tmp_path/'test.hlsl'; path.write_text(source, encoding='utf8')
    store=history.NavigationHistory(str(tmp_path/'cache'), dict(IDENTITY,path=str(path)))
    changed=copy.deepcopy(overview);changed['stages'][0]['title']='wrong'
    assert not store.write(overview, changed, source)
    assert store.write(overview,overview,source)
    Path(store.io.path).write_text('{broken',encoding='utf8')
    assert store.read(overview) is None
    assert path.read_text(encoding='utf8') == source


def test_new_embedded_modules_are_python36_compatible_and_bounded():
    for name in ('shader_structural_anchors.py', 'shader_navigation_history.py'):
        ast.parse((BRIDGE/name).read_text(encoding='utf8'), feature_version=(3,6))
    engine=snapshots._structural
    old=('x','+=','1',';')*3000
    begin=time.monotonic()
    assert engine.relocate(old,old,0,1) is None
    assert time.monotonic()-begin < 1


def test_exact_annotation_text_in_a_different_scope_is_not_the_original():
    old = 'float Main(float x){\nfloat r=x*2;\nr+=x;\nreturn r;\n}'
    overview = prepare_overview(old, [{'id':'m','title':'M','anchors':[
        {'function':'Main','startLine':3,'endLine':3}]}], 'Main')
    new = old.replace('r+=x;', 'if(x>0){r+=x;}')
    assert not outline.resolve_semantic_overview(Hierarchy(new), overview)['rows']


def test_temporary_unbound_checkpoint_retains_evidence_for_later_repair(tmp_path):
    old = 'float Main(float x){\nfloat r=x*2;\nreturn r;\n}'
    overview = prepare_overview(old, [{'id':'m','title':'M','anchors':[
        {'function':'Main','startLine':2,'endLine':2}]}], 'Main')
    changed = old.replace('float r=x*2;', 'float a=x*2;float b=x*2;').replace('return r;', 'return a+b;')
    editor = Editor(changed)
    projected = live_module.LiveOutlineAnchors(editor).update(Hierarchy(changed), overview)
    anchor = projected['stages'][0]['anchors'][0]
    assert anchor['navigationUnresolved'] and anchor['functionSignature']
    path = tmp_path / 'shader.hlsl'; path.write_text(changed, encoding='utf8')
    store = history.NavigationHistory(str(tmp_path/'cache'), dict(IDENTITY,path=str(path)))
    assert store.write(overview, projected, changed)
    reopened = store.read(overview)
    repaired = old.replace('float r', 'float light').replace('return r', 'return light')
    assert outline.resolve_semantic_overview(Hierarchy(repaired), reopened)['rows']
