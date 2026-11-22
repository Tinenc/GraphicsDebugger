"""Native outline identity projected through real sidecar preparation/resolution."""
import ast
import copy
import gc
import importlib.util
import weakref
from pathlib import Path

import pytest

from src.shader_workspace.semantic_overview import prepare_overview


BRIDGE = Path(__file__).resolve().parents[2] / 'renderdoc_mcp_bridge'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, str(BRIDGE / filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


live_module = load('outline_live_test', 'project_shader_outline_anchors.py')
outline = live_module._outline
Hierarchy = load('outline_hierarchy_test', 'project_shader_flow_hierarchy.py').SourceFlowHierarchy
SOURCE = '''// 法线与颜色 🌈
float Helper(float x) { return x * 2; }
float Main(float x) {
    float result = Helper(x);
    result = result * 3;
    return result;
}
'''
STATEMENT = 'result = result * 3;'


class Editor:
    """Explicit native identities; native Scintilla edit mapping has its own tests."""
    def __init__(self, source):
        self.source = source
        self.ranges = {}
        self.next_handle = 0

    def GetText(self):
        return self.source

    def CreateRangeAnchor(self, first, stop):
        self.next_handle += 1
        self.ranges[self.next_handle] = [first, stop]
        return self.next_handle

    def GetRangeAnchorRange(self, handle):
        return self.ranges.get(handle, [])

    def RemoveRangeAnchor(self, handle):
        self.ranges.pop(handle, None)

    def set_span(self, handle, text, occurrence=0):
        data, needle = self.source.encode('utf-8'), text.encode('utf-8')
        start = -1
        for _ in range(occurrence + 1):
            start = data.index(needle, start + 1)
        self.ranges[handle] = [start, start + len(needle)]

    def move(self, source, replacements=None):
        original = self.source.encode('utf-8')
        contents = {handle: original[span[0]:span[1]].decode('utf-8')
                    for handle, span in self.ranges.items() if span}
        self.source = source
        for handle, text in contents.items():
            self.set_span(handle, (replacements or {}).get(text, text))


def overview(source=SOURCE, function=False, helpers=False):
    anchor = {'function': 'Main'} if function else {'function': 'Main', 'startLine': 5, 'endLine': 5}
    stage = {'id': 'adjust', 'title': 'Adjust result', 'anchors': [anchor]}
    if helpers:
        stage['helperSignatures'] = ['Helper']
    return prepare_overview(source, [stage], 'Main')


def anchor(projected):
    return projected['stages'][0]['anchors'][0]


def rows(source, projected):
    return outline.resolve_semantic_overview(Hierarchy(source), projected)


def bound(function=False, helpers=False):
    original = overview(function=function, helpers=helpers)
    editor = Editor(SOURCE)
    live = live_module.LiveOutlineAnchors(editor)
    live.update(Hierarchy(SOURCE), original)
    return live, editor, original


def test_baseline_and_projected_metadata_are_independent_and_same_overview_reuses_handles():
    live, editor, original = bound()
    saved = copy.deepcopy(original)
    first = live.update(Hierarchy(SOURCE), copy.deepcopy(original))
    assert first == original
    assert first is not original
    first['stages'][0]['title'] = 'Modified copy'
    first['stages'][0]['anchors'][0]['tokenSignature'] = 'wrong'
    second = live.update(Hierarchy(SOURCE), original)
    assert second == saved == original
    assert editor.next_handle == 2


@pytest.mark.parametrize('operator', ['+=', '-=', '*=', '/='])
def test_insert_initial_value_and_change_assignment_keeps_original_statement(operator):
    live, editor, original = bound()
    replacement = 'result {} result * 3;'.format(operator)
    edited = SOURCE.replace(STATEMENT, 'result = 0.1;\n    ' + replacement)
    editor.move(edited, {STATEMENT: replacement})
    projected = live.update(Hierarchy(edited), original)
    resolved = rows(edited, projected)
    assert resolved['unresolved'] == []
    assert resolved['rows'][0]['spans'][0]['startLine'] == 6
    assert anchor(projected)['tokenSignature'] == 'result {} result * 3 ;'.format(operator)
    assert anchor(original)['tokenSignature'] == 'result = result * 3 ;'
    assert editor.next_handle == 2


def test_preceding_identical_statement_does_not_steal_established_native_identity():
    live, editor, original = bound()
    edited = SOURCE.replace(STATEMENT, STATEMENT + '\n    ' + STATEMENT)
    editor.move(edited)
    handle = live._bindings[('anchor', 0, 0)]['range']
    editor.set_span(handle, STATEMENT, occurrence=1)
    projected = live.update(Hierarchy(edited), original)
    resolved = rows(edited, projected)
    assert resolved['unresolved'] == []
    assert resolved['rows'][0]['spans'][0]['startLine'] == 6


def test_initial_identical_statements_are_ambiguous_even_when_old_offsets_match():
    source = SOURCE.replace(STATEMENT, STATEMENT + '\n    ' + STATEMENT)
    original = overview(source)
    editor = Editor(source)
    live = live_module.LiveOutlineAnchors(editor)
    projected = live.update(Hierarchy(source), original)
    assert not rows(source, projected)['rows']
    assert not editor.ranges


@pytest.mark.parametrize('function', [False, True])
def test_delete_and_recreate_same_bytes_never_rebinds(function):
    live, editor, original = bound(function=function)
    binding = live._bindings[('anchor', 0, 0)]
    handle = binding['function' if function else 'range']
    editor.ranges[handle] = []
    projected = live.update(Hierarchy(SOURCE), original)
    assert not rows(SOURCE, projected)['rows']
    editor.source = SOURCE + '// another edit\n'
    projected = live.update(Hierarchy(editor.source), copy.deepcopy(original))
    assert not rows(editor.source, projected)['rows']
    assert editor.next_handle == (1 if function else 2)


def test_native_undo_restores_original_identity_without_new_search():
    live, editor, original = bound()
    handle = live._bindings[('anchor', 0, 0)]['range']
    position = list(editor.ranges[handle])
    editor.ranges[handle] = []
    assert not rows(SOURCE, live.update(Hierarchy(SOURCE), original))['rows']
    editor.ranges[handle] = position
    assert rows(SOURCE, live.update(Hierarchy(SOURCE), original))['rows']
    assert editor.next_handle == 2


def test_function_rename_and_parameter_edit_update_function_and_helper_signatures():
    live, editor, original = bound(function=True, helpers=True)
    edited = SOURCE.replace('Main', 'Shade').replace('Helper', 'Remap').replace('float x', 'float2 x')
    editor.move(edited, {'Main': 'Shade', 'Helper': 'Remap'})
    projected = live.update(Hierarchy(edited), original)
    assert anchor(projected)['functionSignature'] == 'Shade ( float2 x )'
    assert anchor(projected)['functionName'] == 'Shade'
    assert projected['stages'][0]['helperSignatures'] == ['Remap ( float2 x )']
    assert rows(edited, projected)['unresolved'] == []


def test_range_owner_rename_is_derived_from_current_hierarchy():
    live, editor, original = bound()
    edited = SOURCE.replace('Main', 'Shade')
    editor.move(edited, {'Main': 'Shade'})
    projected = live.update(Hierarchy(edited), original)
    assert anchor(projected)['functionName'] == 'Shade'
    assert rows(edited, projected)['unresolved'] == []


def test_native_cut_move_to_another_function_follows_current_owner():
    live, editor, original = bound()
    edited = SOURCE.replace('return x * 2;', STATEMENT + ' return x * 2;').replace(
        '    ' + STATEMENT + '\n', '')
    editor.move(edited)
    projected = live.update(Hierarchy(edited), original)
    assert anchor(projected)['functionName'] == 'Helper'
    assert rows(edited, projected)['rows'][0]['spans'][0]['startLine'] == 2


def test_unmatched_initial_source_retries_after_repair_without_false_tombstone():
    original = overview()
    editor = Editor(SOURCE.replace(STATEMENT, 'discard;'))
    live = live_module.LiveOutlineAnchors(editor)
    assert not rows(editor.source, live.update(Hierarchy(editor.source), original))['rows']
    assert editor.next_handle == 0
    editor.source = SOURCE
    assert rows(SOURCE, live.update(Hierarchy(SOURCE), original))['rows']
    assert editor.next_handle == 2


def test_malformed_or_stale_hierarchy_defers_binding_until_valid_current_source():
    original = overview()
    editor = Editor(SOURCE[:-2])
    live = live_module.LiveOutlineAnchors(editor)
    assert anchor(live.update(None, original))['navigationUnresolved']
    assert anchor(live.update(Hierarchy(SOURCE), original))['navigationUnresolved']
    assert editor.next_handle == 0
    editor.source = SOURCE
    assert rows(SOURCE, live.update(Hierarchy(SOURCE), original))['rows']


def test_incomplete_live_edit_keeps_handles_and_recovers_after_repair():
    live, editor, original = bound()
    editor.source = SOURCE[:-2]
    assert anchor(live.update(None, original))['navigationUnresolved']
    editor.source = SOURCE
    assert rows(SOURCE, live.update(Hierarchy(SOURCE), original))['rows']
    assert editor.next_handle == 2


def test_utf8_crlf_and_comment_offsets_are_current_byte_offsets():
    live, editor, original = bound()
    edited = ('// 新增 🌈 注释\n' + SOURCE).replace('\n', '\r\n')
    editor.move(edited)
    projected = live.update(Hierarchy(edited), original)
    assert rows(edited, projected)['unresolved'] == []
    assert anchor(projected)['sourceRange'] == {'startLine': 6, 'endLine': 6}
    assert anchor(projected)['sourceSha256'] == Hierarchy(edited).source_sha256


def test_mid_utf8_endpoint_is_unresolved_instead_of_rounding_to_other_token():
    live, editor, original = bound()
    handle = live._bindings[('anchor', 0, 0)]['range']
    byte = SOURCE.encode('utf-8').index('法'.encode('utf-8')) + 1
    editor.ranges[handle] = [byte, byte + 1]
    assert not rows(SOURCE, live.update(Hierarchy(SOURCE), original))['rows']


def test_clear_releases_ranges_and_reload_recovers_structurally_mapped_metadata():
    live, editor, original = bound()
    edited = SOURCE.replace(STATEMENT, 'result += result * 3;')
    editor.move(edited, {STATEMENT: 'result += result * 3;'})
    assert rows(edited, live.update(Hierarchy(edited), original))['rows']
    live.clear()
    assert not editor.ranges
    assert rows(edited, live.update(Hierarchy(edited), original))['rows']


def test_changed_overview_identity_releases_old_ranges_and_rebinds_new_metadata():
    live, editor, original = bound()
    old_handles = set(editor.ranges)
    changed = copy.deepcopy(original)
    changed['stages'][0]['title'] = 'New semantic interpretation'
    live.update(Hierarchy(SOURCE), changed)
    assert old_handles.isdisjoint(editor.ranges)
    assert editor.next_handle == 4
    live.update(Hierarchy(SOURCE), None)
    assert not editor.ranges


def test_missing_native_api_returns_unchanged_deep_copy():
    original = overview()
    editor = type('LegacyEditor', (), {'GetText': lambda self: SOURCE})()
    live = live_module.LiveOutlineAnchors(editor)
    projected = live.update(Hierarchy(SOURCE), original)
    assert projected == original and projected is not original
    live.clear()


def test_failed_native_creation_releases_partial_binding_and_can_retry():
    live, editor, original = bound()
    live.clear()
    create = editor.CreateRangeAnchor
    def fail_second(first, stop):
        return -1 if editor.ranges else create(first, stop)
    editor.CreateRangeAnchor = fail_second
    assert not rows(SOURCE, live.update(Hierarchy(SOURCE), original))['rows']
    assert editor.ranges == {}
    editor.CreateRangeAnchor = create
    assert rows(SOURCE, live.update(Hierarchy(SOURCE), original))['rows']


def test_module_is_compatible_with_embedded_python_36():
    ast.parse((BRIDGE / 'project_shader_outline_anchors.py').read_text('utf-8'), feature_version=(3, 6))


def test_worker_prepare_keeps_all_lexing_off_editor_update(monkeypatch):
    index = Hierarchy(SOURCE)
    prepared = live_module.prepare(index)
    original = overview(helpers=True)
    editor = Editor(SOURCE)
    live = live_module.LiveOutlineAnchors(editor)
    def unexpected_lexing(source):
        raise AssertionError('Source lexing must remain on the existing worker')
    monkeypatch.setattr(outline, '_anchor_tokens', unexpected_lexing)
    projected = live.update(index, original)
    assert projected == original
    assert live_module.prepare(index) is prepared
    assert len(editor.ranges) == 3


def test_prepared_cache_is_released_with_its_hierarchy():
    index = Hierarchy(SOURCE)
    cached = weakref.ref(live_module.prepare(index))
    del index
    gc.collect()
    assert cached() is None
