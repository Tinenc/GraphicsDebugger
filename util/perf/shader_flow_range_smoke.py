"""Real native editor + production flow/outline positioning, in an owned process."""
import ast
import copy
import importlib.util
import json
import os
from pathlib import Path
import traceback
import types


def run(context):
    from PySide2 import QtCore, QtGui, QtWidgets
    app = QtWidgets.QApplication.instance()
    report = {'ok': False, 'pid': os.getpid(), 'checks': []}
    output = Path(os.environ['BREAD_SHADER_ANCHOR_SMOKE_OUTPUT'])
    root = Path(os.environ['BREAD_SHADER_ANCHOR_SOURCE_ROOT'])
    bridge = root / 'replaylab' / 'renderdoc_mcp_bridge'
    host = QtWidgets.QWidget()
    host.setAttribute(QtCore.Qt.WA_DontShowOnScreen, True)
    editor = context.CreateShaderCodeEditor(host)
    widget = editor.Widget()
    layout = QtWidgets.QVBoxLayout(host)
    layout.addWidget(widget)
    host.show()
    target = widget.findChild(QtWidgets.QWidget, 'shaderCodeEditor')

    def load(name):
        spec = importlib.util.spec_from_file_location('_smoke_' + name, str(bridge / (name + '.py')))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def check(name, result, detail=None):
        report['checks'].append({'name': name, 'passed': bool(result), 'detail': detail})
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        if not result:
            raise AssertionError(name + ': ' + str(detail))

    def edit(first, last, text):
        editor.SelectRange(first, last)
        event = QtGui.QKeyEvent(QtCore.QEvent.KeyPress,
            QtCore.Qt.Key_unknown if text else QtCore.Qt.Key_Delete, QtCore.Qt.NoModifier, text)
        app.sendEvent(target, event)
        app.processEvents()

    try:
        hierarchy_module = load('project_shader_flow_hierarchy')
        outline_module = load('project_shader_stage_outline')
        live_module = load('project_shader_outline_anchors')
        source = ('float A(float x) { return x; } float B(float x) { return x*2; }\n'
                  'float Main(float x) { x = A(x); x = B(x); return x; }\n')
        editor.SetText(source)
        names = {'_function_line_span', '_flow_token_anchor', '_flow_anchor_position',
                 '_function_bookmark', '_bookmarked_function', '_anchor_graph',
                 '_prune_flow_anchors', '_flow_graph_node_activated',
                 '_flow_graph_subgraph_requested', '_open_function_graph'}
        tree = ast.parse((bridge / 'project_shader_workspace.py').read_text(encoding='utf-8'))
        methods = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in names]
        scope = {'copy': copy}
        exec(compile(ast.Module(body=methods, type_ignores=[]), '<production-flow-methods>', 'exec'), scope)
        panel = type('FlowMethods', (), {name: scope[name] for name in names})()
        document = {'editor': editor, 'available': True, 'workspaceId': 'owned-test',
                    'documentToken': 1, 'captureBinding': 'owned-test'}
        panel._current_document = lambda: document
        panel._current_capture_binding = lambda: 'owned-test'
        panel._source_hierarchy = lambda unused: hierarchy_module.SourceFlowHierarchy(str(editor.GetText()))
        unavailable, opened = [], []
        panel._flow_graph_target_unavailable = lambda: unavailable.append(True)
        history = types.SimpleNamespace(_entries=[])
        def open_graph(graph, **unused):
            opened.append(graph)
            history._entries[:] = [(graph, {})]
        panel.flow_graph = types.SimpleNamespace(_history=history, open_subgraph=open_graph)
        index = panel._source_hierarchy(document)
        graph = index.function_graph(index.function_identity('Main'), functions_only=True)
        panel._anchor_graph(document, graph, index)
        history._entries[:] = [(graph, {})]
        calls = [node for node in graph['nodes'] if node.get('kind') == 'function-call']
        position = source.encode('utf-8').index(b'x = B(x)')
        insertion = '\t/* 中文 */ x += 0.1; '
        edit(position, position, insertion)
        panel._flow_graph_node_activated(calls[1]['source'])
        current = str(editor.GetText())
        expected = current.index('x = B(x)')
        column = len(current[:expected].rsplit('\n', 1)[-1].expandtabs(4)) + 1
        check('sameLineUtf8AndTabNavigation', (editor.GetCursorLine(), editor.GetCursorColumn()) == (2, column),
              {'actual': [editor.GetCursorLine(), editor.GetCursorColumn()], 'expected': [2, column]})
        panel._flow_graph_subgraph_requested(calls[1]['source'])
        check('sameLineSelectedCallExpandsB', opened[-1]['functionName'] == 'B' and not unavailable)
        check('sameLineDefinitionColumn', editor.GetCursorColumn() == source.index('B(float') + 1,
              editor.GetCursorColumn())

        editor.SetText('float Main(float x) {\n float y = x*2;\n return y;\n}\n')
        index = panel._source_hierarchy(document)
        record = index.functions[index.function_identity('Main')]
        overview = {'version': 1, 'stages': [{'id': 'shade', 'title': 'Shade', 'anchors': [
            {'kind': 'range', 'functionSignature': outline_module._signature(record['signature']),
             'tokenSignature': 'float y = x * 2 ;'}]}]}
        live = live_module.LiveOutlineAnchors(editor)
        mapped = live.update(index, overview)
        check('directoryInitiallyBinds', outline_module.resolve_semantic_overview(index, mapped)['rows'][0]['id'] == 'semantic:shade')
        data = str(editor.GetText()).encode('utf-8')
        first = data.index(b'float y')
        edit(first, first, '/* 中文 */ float other = 0.1;\n ')
        data = str(editor.GetText()).encode('utf-8')
        operator = data.index(b' = x') + 1
        edit(operator, operator + 1, '+=')
        index = panel._source_hierarchy(document)
        mapped = live.update(index, overview)
        rows = outline_module.resolve_semantic_overview(index, mapped)['rows']
        check('directoryTracksCompoundAssignmentAndInsertedLines', len(rows) == 1 and
              rows[0]['spans'][0]['startLine'] == 3 and
              mapped['stages'][0]['anchors'][0]['tokenSignature'] == 'float y += x * 2 ;', rows)
        data = str(editor.GetText()).encode('utf-8')
        first = data.index(b'float y')
        last = data.index(b';', first) + 1
        edit(first, last, '')
        # A separate paste is a new statement, not an in-place replacement.
        editor.GoToLine(4, 1)
        edit(first, first, 'float y += x*2;')
        index = panel._source_hierarchy(document)
        mapped = live.update(index, overview)
        check('directoryDeletedIdentityDoesNotRebindSameText',
              not outline_module.resolve_semantic_overview(index, mapped)['rows'])
        live.clear()
        report['ok'] = True
    except BaseException:
        report['error'] = traceback.format_exc()
    finally:
        editor.ClearCallbacks()
        layout.removeWidget(widget)
        widget.setParent(None)
        context.DestroyShaderCodeEditor(editor)
        host.close()
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        context.GetMainWindow().Widget().close()
        QtCore.QCoreApplication.exit(0 if report['ok'] else 1)


if 'pyrenderdoc' in globals():
    pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(lambda: run(pyrenderdoc))
