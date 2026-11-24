"""Converting authored sources must preserve buffers and run outside the UI thread."""
import ast
from types import SimpleNamespace
from unittest import mock

import pytest

from test_shader_workspace_panel import MODULE_PATH, _load_without_qt
from test_shader_source_flow_async import Executor


def panel():
    workspace = _load_without_qt()
    names = {'_convert_current_to_hlsl', '_hlsl_conversion_ready'}
    nodes = [node for node in ast.walk(ast.parse(MODULE_PATH.read_text('utf8')))
             if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {}
    scope = dict(workspace.__dict__, QtCore=SimpleNamespace(QCoreApplication=SimpleNamespace(
        translate=lambda context, text: text)))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(MODULE_PATH), 'exec'), scope, namespace)
    instance = type('ConversionPanel', (), namespace)()
    instance.binding = {'captureSessionId': 'session'}
    instance._current_capture_binding = lambda: instance.binding
    editor = mock.Mock()
    editor.GetText.return_value = 'void main() {}'
    document = {'workspaceId': 'workspace', 'available': True, 'documentToken': 5,
                'sourcePath': 'shader.glsl', 'rawSha256': 'a' * 64,
                'baselineEditorText': editor.GetText(), 'editor': editor,
                'captureBinding': instance.binding}
    instance._documents = {'workspace': document}
    instance._current_document = lambda: document
    instance._document_workspace_path = lambda key, doc: 'workspace/path'
    instance._snapshot_generation = 1
    instance.project_selection_path = 'project'
    instance._snapshot_owner = 'owner'
    instance._snapshot_executor = Executor()
    instance._update_conversion_button = mock.Mock()
    instance._save_client = mock.Mock()
    instance._save_client.live_edit_operation.return_value = {
        'sourcePreserved': True, 'workspacePath': 'new-hlsl', 'sourcePath': 'new-hlsl/shader.hlsl',
        'compiled': True, 'status': 'ok', 'readyForEditing': True}
    instance.status_label = mock.Mock()
    instance.refresh = mock.Mock()
    instance._alive = True
    instance._shutting_down = False
    return instance, document


def test_conversion_submits_only_saved_source_hash_off_the_ui_thread():
    instance, document = panel()
    instance._convert_current_to_hlsl()
    instance._save_client.live_edit_operation.assert_not_called()
    assert document['conversionInFlight']
    instance._snapshot_executor.deliver()
    instance._save_client.live_edit_operation.assert_called_once_with('convert-hlsl', {
        'source_workspace_path': 'workspace/path', 'expected_source_sha256': 'a' * 64}, instance.binding)
    assert 'conversionInFlight' not in document
    document['editor'].SetText.assert_not_called()
    instance.refresh.assert_called_once_with(True)


@pytest.mark.parametrize('condition', ['dirty', 'conflict', 'saveInFlight', 'commitUnknown',
                                      'pendingSave', 'text-changed', 'wrong-capture', 'hlsl'])
def test_conversion_preserves_unsettled_buffers_and_wrong_context(condition):
    instance, document = panel()
    if condition == 'text-changed':
        document['editor'].GetText.return_value += '// unsaved'
    elif condition == 'wrong-capture':
        instance.binding = {'captureSessionId': 'other'}
    elif condition == 'hlsl':
        document['sourcePath'] = 'shader.hlsl'
    else:
        document[condition] = True
    instance._convert_current_to_hlsl()
    assert not instance._snapshot_executor.jobs
    instance._save_client.live_edit_operation.assert_not_called()
    document['editor'].SetText.assert_not_called()


@pytest.mark.parametrize('condition', ['capture', 'source', 'document', 'project', 'closed'])
def test_conversion_finishing_after_context_change_does_not_reopen_or_overwrite(condition):
    instance, document = panel()
    instance._convert_current_to_hlsl()
    if condition == 'capture': instance.binding = {'captureSessionId': 'other'}
    if condition == 'source': document['rawSha256'] = 'b' * 64
    if condition == 'document': document['documentToken'] += 1
    if condition == 'project': instance.project_selection_path = 'other'
    if condition == 'closed': instance._alive = False
    instance._snapshot_executor.deliver()
    instance.refresh.assert_not_called()
    document['editor'].SetText.assert_not_called()


def test_conversion_error_is_explained_and_original_source_stays_editable():
    instance, document = panel()
    instance._convert_current_to_hlsl()
    unused, callback = instance._snapshot_executor.jobs[0]
    callback(None, 'shader.glsl:2: unknown type float4')
    assert 'unknown type float4' in instance.status_label.setText.call_args.args[0]
    assert 'conversionInFlight' not in document
    instance.refresh.assert_not_called()
    document['editor'].SetText.assert_not_called()


def test_conversion_registry_refresh_does_not_discard_its_own_success():
    instance, document = panel()
    instance._convert_current_to_hlsl()
    instance._snapshot_generation += 1  # Registering the new workspace updates the watched registry.
    instance._snapshot_executor.deliver()
    instance.refresh.assert_called_once_with(True)


def test_conversion_abi_failure_is_not_reported_as_a_success():
    instance, document = panel()
    instance._convert_current_to_hlsl()
    unused, callback = instance._snapshot_executor.jobs[0]
    callback({'readyForEditing': False, 'abiValidation': {'error': 'location changed'}}, None)
    assert 'location changed' in instance.status_label.setText.call_args.args[0]
    instance.refresh.assert_not_called()
