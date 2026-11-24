"""UI navigation consumes native byte ranges, never frozen display columns."""
from test_shader_workspace_navigation import Editor, panel


class RangeEditor(Editor):
    """Native range contract fixture; real edit/undo tracking has Qt smoke coverage."""
    def __init__(self):
        super().__init__()
        self.ranges = {}
        self.next_range = 1000
        self.removed_ranges = []

    def CreateRangeAnchor(self, first, last):
        handle = self.next_range
        self.next_range += 1
        self.ranges[handle] = [first, last]
        return handle

    def GetRangeAnchorRange(self, handle):
        return self.ranges.get(handle, [])

    def RemoveRangeAnchor(self, handle):
        self.ranges.pop(handle, None)
        self.removed_ranges.append(handle)

    def insert(self, position, text):
        byte = len(self.source[:position].encode('utf-8'))
        size = len(text.encode('utf-8'))
        for span in self.ranges.values():
            if span:
                span[:] = [value + size if value >= byte else value for value in span]
        line = self.source.count('\n', 0, position) + 1
        self.markers = {key: old + text.count('\n') if old >= line else old
                        for key, old in self.markers.items()}
        self.source = self.source[:position] + text + self.source[position:]


SOURCE = ('float A(float x) { return x; } float B(float x) { return x*2; }\n'
          'float Main(float x) { x = A(x); x = B(x); return x; }\n')


def current_column(source, offset):
    return len(source[:offset].rsplit('\n', 1)[-1].expandtabs(4)) + 1


def open_calls():
    value, document, payload = panel(SOURCE, 'Main', RangeEditor)
    value._flow_graph_subgraph_requested(payload)
    calls = [node for node in value.opened[-1]['nodes'] if node.get('kind') == 'function-call']
    assert [node['name'] for node in calls] == ['A', 'B']
    return value, document, calls


def test_same_line_call_column_follows_utf8_comments_and_tabs():
    value, document, payload = panel(SOURCE, 'B', RangeEditor)
    editor = document['editor']
    editor.insert(editor.source.index('x = B(x)'), '\t/* 中文🙂 */ ')
    document['textRevision'] += 1
    value._flow_graph_node_activated(payload)
    expected = editor.source.index('B(x)')
    assert editor.cursor == (2, current_column(editor.source, expected))
    assert not value.unavailable


def test_local_two_calls_on_one_line_expand_the_selected_statement_after_edit():
    value, document, calls = open_calls()
    editor = document['editor']
    editor.insert(editor.source.index('x = B(x)'), '/* 新计算 */ x += 0.1; ')
    document['textRevision'] += 1
    value._flow_graph_node_activated(calls[1]['source'])
    expected = editor.source.index('x = B(x)')
    assert editor.cursor == (2, current_column(editor.source, expected))
    value._flow_graph_subgraph_requested(calls[1]['source'])
    assert value.opened[-1]['functionName'] == 'B'
    assert not value.unavailable


def test_same_line_function_declarations_use_their_own_live_ranges():
    value, document, payload = panel(SOURCE, 'B', RangeEditor)
    editor = document['editor']
    editor.insert(0, '// inserted\n')
    document['textRevision'] += 1
    value._flow_graph_subgraph_requested(payload)
    assert value.opened[-1]['functionName'] == 'B'
    expected = editor.source.index('B(float')
    assert editor.cursor == (2, current_column(editor.source, expected))


def test_deleted_call_never_falls_back_to_another_call_on_the_same_line():
    value, document, calls = open_calls()
    editor = document['editor']
    anchor = document['flowAnchors'][calls[1]['source']['anchorId']]
    editor.ranges[anchor['rangeMarker']] = []
    editor.cursor = None
    value._flow_graph_node_activated(calls[1]['source'])
    value._flow_graph_subgraph_requested(calls[1]['source'])
    assert editor.cursor is None
    assert len(value.opened) == 1
    assert len(value.unavailable) == 2


def test_deleted_declaration_does_not_bind_other_same_line_declaration():
    value, document, payload = panel(SOURCE, 'B', RangeEditor)
    anchor = document['flowAnchors'][payload['anchorId']]
    document['editor'].ranges[document['flowFunctionRanges'][anchor['functionMarker']]] = []
    value._flow_graph_node_activated(payload)
    assert document['editor'].cursor is None
    assert value.unavailable


def test_old_native_editor_recomputes_unique_call_column_from_current_source():
    source = SOURCE.replace(' } float B', ' }\nfloat B')
    value, document, payload = panel(source, 'B')
    editor = document['editor']
    editor.source = source.replace('x = B(x)', '\t/* 中文 */ x = B(x)')
    document['textRevision'] += 1
    value._flow_graph_node_activated(payload)
    expected = editor.source.index('B(x)')
    assert editor.cursor == (3, current_column(editor.source, expected))


def test_reprojecting_graph_releases_range_handles_outside_retained_history():
    value, document, calls = open_calls()
    editor = document['editor']
    old = document['flowAnchors'][calls[1]['source']['anchorId']]['rangeMarker']
    editor.insert(editor.source.index('return x; }\n', editor.source.index('float Main')), 'x += 1; ')
    document['textRevision'] += 1
    hierarchy = value._source_hierarchy(document)
    value._open_function_graph(document, hierarchy, hierarchy.function_identity('Main'))
    assert old not in editor.ranges
    assert old in editor.removed_ranges
    live = {anchor['rangeMarker'] for anchor in document['flowAnchors'].values()}
    live.update(document['flowFunctionRanges'].values())
    assert set(editor.ranges) == live
