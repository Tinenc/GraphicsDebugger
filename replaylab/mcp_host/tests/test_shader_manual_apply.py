"""Manual saves execute one native transaction and never request ABI evidence."""
import copy
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src import shader_manual_edit


def fixture(monkeypatch, tmp_path, api="vulkan", encoding="spirv"):
    status = {"workspaceId": "1" * 32, "workspacePath": str(tmp_path), "revision": 9,
        "capture": {"captureId": "2" * 64, "catalogPath": str(tmp_path / 'catalog.json'),
                    "catalogSha256": "3" * 64},
        "shader": {"resourceId": 41, "shaderKey": "captured-fragment"},
        "stage": "fragment", "entryPoint": "main", "baseline": {"rawSha256": "4" * 64}}
    binding = {"workspaceId": status["workspaceId"], "captureId": "2" * 64,
        "revision": 9, "candidateRawSha256": "5" * 64, "sourceSha256": "6" * 64,
        "sourceDependenciesDigest": "7" * 64, "baselineRawSha256": "4" * 64,
        "api": api, "encoding": encoding, "stage": "fragment", "entryPoint": "main",
        "targetProfile": "ps_5_0" if api.startswith('d3d') else encoding}
    request = {"binding": binding, "params": {"candidate_path": str(tmp_path / 'candidate.bin'),
        "expected_sha256": "5" * 64, "encoding": encoding, "stage": "fragment", "entry_point": "main"}}
    forbidden = Mock(side_effect=AssertionError("A manual save invoked ABI validation"))
    service = SimpleNamespace(status=Mock(return_value=status),
        manual_candidate_apply_request=Mock(return_value=request), candidate_reflection_request=forbidden)
    host = SimpleNamespace(_normalise_expected_shader_candidate=lambda r, h: {'revision': r, 'candidateRawSha256': h},
        _shader_workspace_service=lambda: service, _require_shader_candidate_binding=lambda *a: "6" * 64,
        _ensure_workspace_capture_catalog_identity=Mock(return_value={'captureSessionId': "8" * 32}),
        _strict_lower_hex=lambda value, *a: value, _sync_shader_override_capture=Mock(),
        list_exact_shader_family_uses=Mock(return_value=[73, 45]),
        _require_workspace_live_edit_quiescent=Mock(), _SHADER_OVERRIDE_GUARD=threading.Lock(),
        _SHARED_SHADER_REPLACEMENT_RECORDS={}, _fresh_shader_candidate_validation=forbidden,
        _apply_shader_workspace_shared_resource_with_live_abi_lease=forbidden,
        _takeover_idle_foreign_shared_shader_owner=Mock(return_value={'retryApply': True,
            'expectedOwner': {'replacementId': "a" * 32}}))
    def apply(method, params, timeout):
        assert method == 'apply_shader_candidate_shared_resource'
        assert params['abi_validation_mode'] == 'manual-fast'
        assert not any(key.startswith('abi_') for key in params if key != 'abi_validation_mode')
        return {"schema": "renderdoc-ultra-shared-shader-replacement/v1", "schemaVersion": 1,
            **binding, "captureSessionId": "8" * 32, "originalResourceId": 41,
            "originalRawSha256": "4" * 64, "anchorEventId": 45, "operationId": params['operation_id'],
            "resourceScoped": True, "eventScoped": False, "allOriginalResourceUsesAffected": True,
            "configured": True, "originalShaderPreserved": True, "singleOwner": True,
            "abiValidationMode": "manual-fast", "manualEdit": True, "replayAbiValidated": False,
            "validationScope": "manual-edit-unvalidated", "freshPhysicalAbiValidationPerformed": False,
            "postApplyAnchorVerified": False, "replacementId": "9" * 32,
            "idempotent": False, "metadataOnly": False, "targetBuiltOnce": True, "targetReused": False,
            "operationCreatedReplacement": True, "supersededTargetFreed": False, "targetResourceId": 100}
    host.ipc = SimpleNamespace(call=Mock(side_effect=apply))
    monkeypatch.setattr(shader_manual_edit, '_ANCHORS', shader_manual_edit.OrderedDict())
    return SimpleNamespace(host=host, status=status, request=request, service=service, forbidden=forbidden, native=apply)


@pytest.mark.parametrize('api,encoding', [('d3d11', 'dxbc'), ('d3d12', 'dxbc'), ('d3d12', 'dxil'),
    ('vulkan', 'spirv'), ('opengl', 'opengl-spirv'), ('opengl', 'glsl')])
def test_manual_apply_is_one_native_transaction_without_abi(monkeypatch, tmp_path, api, encoding):
    case = fixture(monkeypatch, tmp_path, api, encoding)
    result = shader_manual_edit.apply_manual_candidate(case.host, str(tmp_path), 9, '5' * 64)
    assert result['applied'] is True and result['validation']['replayAbiValidated'] is False
    assert result['validation']['freshPhysicalValidationPerformed'] is False
    assert result['validation']['manualEdit'] is True
    assert result['sourceIdentity']['sourceDependenciesDigest'] == '7' * 64
    assert result['rollback']['replacementId'] == '9' * 32
    case.host.ipc.call.assert_called_once()
    case.forbidden.assert_not_called()
    stored = case.host._SHARED_SHADER_REPLACEMENT_RECORDS['9' * 32]
    assert stored['workspacePath'] == str(tmp_path) and stored['manualEdit'] is True
    assert not any(key.startswith('abiLease') for key in stored)


def test_manual_anchor_cached_only_for_same_catalog_and_capture(monkeypatch, tmp_path):
    case = fixture(monkeypatch, tmp_path)
    for _ in range(2):
        shader_manual_edit.apply_manual_candidate(case.host, str(tmp_path), 9, '5' * 64)
    case.host.list_exact_shader_family_uses.assert_called_once()
    case.status['capture']['catalogSha256'] = 'b' * 64
    shader_manual_edit.apply_manual_candidate(case.host, str(tmp_path), 9, '5' * 64)
    assert case.host.list_exact_shader_family_uses.call_count == 2
    shader_manual_edit._anchor_event(case.host, case.status, 'c' * 32)
    assert case.host.list_exact_shader_family_uses.call_count == 3


def test_restore_uses_current_inventory_anchor_without_reusing_old_catalog_event(monkeypatch, tmp_path):
    case = fixture(monkeypatch, tmp_path)
    case.host.ipc.call.side_effect = lambda *a, **k: {**case.native(*a, **k), 'anchorEventId': 97}
    result = shader_manual_edit.apply_manual_candidate(case.host, str(tmp_path), 9, '5' * 64,
                                                       expected_anchor_event_id=97)
    assert result['replacement']['anchorEventId'] == 97
    assert case.host.ipc.call.call_args.args[1]['event_id'] == 97
    case.host.list_exact_shader_family_uses.assert_not_called()
    case.forbidden.assert_not_called()


@pytest.mark.parametrize('key,value', [('sourceSha256', 'f' * 64), ('revision', 10),
    ('workspaceId', 'f' * 32), ('baselineRawSha256', 'f' * 64)])
def test_stale_source_or_candidate_is_not_applied(monkeypatch, tmp_path, key, value):
    case = fixture(monkeypatch, tmp_path)
    case.request['binding'][key] = value
    with pytest.raises(RuntimeError, match='changed before manual apply'):
        shader_manual_edit.apply_manual_candidate(case.host, str(tmp_path), 9, '5' * 64)
    case.host.ipc.call.assert_not_called()


@pytest.mark.parametrize('key,value', [('replayAbiValidated', True), ('originalResourceId', 99),
    ('captureSessionId', 'f' * 32), ('candidateRawSha256', 'f' * 64), ('postApplyAnchorVerified', True),
    ('restoreValidationReceipt', {'invalid': True})])
def test_mismatched_native_result_does_not_publish_success(monkeypatch, tmp_path, key, value):
    case = fixture(monkeypatch, tmp_path)
    case.host.ipc.call.side_effect = lambda *a, **k: {**case.native(*a, **k), key: value}
    with pytest.raises(RuntimeError):
        shader_manual_edit.apply_manual_candidate(case.host, str(tmp_path), 9, '5' * 64)
    assert case.host._SHARED_SHADER_REPLACEMENT_RECORDS == {}


def test_exact_foreign_owner_handoff_preserves_manual_policy(monkeypatch, tmp_path):
    case = fixture(monkeypatch, tmp_path)
    calls = []
    def apply(*args, **kwargs):
        calls.append(copy.deepcopy(args[1]))
        if len(calls) == 1:
            raise RuntimeError('owned by a different workspace')
        return case.native(*args, **kwargs)
    case.host.ipc.call.side_effect = apply
    assert shader_manual_edit.apply_manual_candidate(case.host, str(tmp_path), 9, '5' * 64)['applied']
    assert calls[1]['expected_owner'] == {'replacementId': 'a' * 32}
    assert calls[0]['operation_id'] == calls[1]['operation_id']
    case.forbidden.assert_not_called()


def test_build_error_preserves_last_success_and_never_falls_back_to_validation(monkeypatch, tmp_path):
    case = fixture(monkeypatch, tmp_path)
    case.host._SHARED_SHADER_REPLACEMENT_RECORDS['old'] = {'preserved': True}
    case.host.ipc.call.side_effect = RuntimeError('driver compile failed')
    with pytest.raises(RuntimeError, match='driver compile failed'):
        shader_manual_edit.apply_manual_candidate(case.host, str(tmp_path), 9, '5' * 64)
    assert case.host._SHARED_SHADER_REPLACEMENT_RECORDS == {'old': {'preserved': True}}
    case.host.ipc.call.assert_called_once()
    case.forbidden.assert_not_called()


def test_busy_other_editor_is_rejected_before_native_mutation(monkeypatch, tmp_path):
    case = fixture(monkeypatch, tmp_path)
    case.host._require_workspace_live_edit_quiescent.side_effect = RuntimeError('another editor is applying')
    with pytest.raises(RuntimeError, match='another editor'):
        shader_manual_edit.apply_manual_candidate(case.host, str(tmp_path), 9, '5' * 64)
    case.host.ipc.call.assert_not_called()


def test_editor_callback_dispatches_manual_policy_without_strict_fallback(monkeypatch, tmp_path):
    from src import server
    apply = Mock(return_value={'applied': True, 'manualEdit': True})
    monkeypatch.setattr(shader_manual_edit, 'apply_manual_candidate', apply)
    strict = Mock(side_effect=AssertionError('strict ABI fallback invoked'))
    monkeypatch.setattr(server, '_shader_live_edit_apply_candidate_with_abi', strict)
    result = server._shader_live_edit_apply_candidate(str(tmp_path), 4, 'a' * 64)
    assert result['manualEdit'] is True
    apply.assert_called_once_with(server, str(tmp_path), 4, 'a' * 64)
    strict.assert_not_called()


@pytest.mark.parametrize('lost_reply', [False, True])
def test_uncertain_operation_is_recovered_by_exact_token(monkeypatch, tmp_path, lost_reply):
    from src.ipc_client import IPCBridgeTimeoutError
    case = fixture(monkeypatch, tmp_path)
    calls = []
    def invoke(method, params, timeout):
        calls.append((method, copy.deepcopy(params)))
        if method == 'apply_shader_candidate_shared_resource':
            if lost_reply:
                raise IPCBridgeTimeoutError('reply was lost')
            return {**case.native(method, params, timeout), 'originalResourceId': 99}
        assert method == 'recover_shader_candidate_shared_resource_operation'
        return {'schema': 'renderdoc-ultra-shared-shader-operation-recovery/v1', 'schemaVersion': 1,
            'operationId': params['operation_id'], 'captureId': '2' * 64, 'captureSessionId': '8' * 32,
            'workspaceId': '1' * 32, 'originalShaderPreserved': True, 'strictReceipt': True,
            'recovered': True, 'mutationPresent': False, 'status': 'absent',
            'operationKnown': False, 'operationCreatedReplacement': False}
    case.host.ipc.call.side_effect = invoke
    with pytest.raises(RuntimeError, match='reply was lost|identity mismatch'):
        shader_manual_edit.apply_manual_candidate(case.host, str(tmp_path), 9, '5' * 64)
    assert [row[0] for row in calls] == ['apply_shader_candidate_shared_resource',
                                       'recover_shader_candidate_shared_resource_operation']
    assert calls[0][1]['operation_id'] == calls[1][1]['operation_id']
    assert case.host._SHARED_SHADER_REPLACEMENT_RECORDS == {}
