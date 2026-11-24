from types import SimpleNamespace

import pytest

from tests.test_bridge_dxil_access_gate import load_bridge
from tests.test_bridge_dxil_access_gate import ResourceId


def test_batch_retains_successes_reports_failed_item_and_always_flushes(monkeypatch):
    bridge = load_bridge()
    session = 'a'*32
    bridge._capture_session_id = session
    calls=[]
    controller=SimpleNamespace(BeginShaderReplacementBatch=lambda: calls.append('begin') or 7,
        EndShaderReplacementBatch=lambda token: calls.append(('end',token)) or True)
    monkeypatch.setattr(bridge,'_replay',lambda fn: fn(controller))
    monkeypatch.setattr(bridge,'_begin_replay_atomic_transaction',lambda: None)
    monkeypatch.setattr(bridge,'_notify_shader_replay_changed',lambda: calls.append('notify'))
    def apply(item):
        assert bridge._request_dispatch_state.defer_shader_notifications
        if item['operation_id']=='2'*32: raise ValueError('bad candidate')
        return {'operationId':item['operation_id']}
    monkeypatch.setattr(bridge,'_apply_shader_candidate_shared_resource',apply)
    items=[{'operation_id':str(i)*32,'expected_capture_session_id':session,
        'abi_validation_mode':'verified-rdc-restore'} for i in (1,2,3)]
    result=bridge._apply_shader_candidate_shared_resource_batch({'items':items})
    assert [r['ok'] for r in result['items']]==[True,False,True]
    assert result['items'][1]['operationId']=='2'*32
    assert calls==['begin',('end',7),'notify']
    assert not bridge._request_dispatch_state.defer_shader_notifications
    calls.clear()
    with pytest.raises(ValueError,match='Duplicate'):
        bridge._apply_shader_candidate_shared_resource_batch({'items':[items[0],items[0]]})
    assert calls==[]


def test_lost_batch_owner_never_claims_success(monkeypatch):
    bridge=load_bridge();bridge._capture_session_id='a'*32
    controller=SimpleNamespace(BeginShaderReplacementBatch=lambda:7,EndShaderReplacementBatch=lambda token:False)
    monkeypatch.setattr(bridge,'_replay',lambda fn:fn(controller))
    monkeypatch.setattr(bridge,'_begin_replay_atomic_transaction',lambda:None)
    monkeypatch.setattr(bridge,'_apply_shader_candidate_shared_resource',lambda item:{})
    with pytest.raises(RuntimeError,match='ownership changed'):
        bridge._apply_shader_candidate_shared_resource_batch({'items':[{'operation_id':'1'*32,
            'expected_capture_session_id':'a'*32,'abi_validation_mode':'verified-rdc-restore'}]})
    assert not bridge._request_dispatch_state.defer_shader_notifications


def test_large_restore_flushes_once_and_refuses_unbounded_requests(monkeypatch):
    bridge = load_bridge()
    bridge._capture_session_id = 'a' * 32
    ended = []
    ctrl = SimpleNamespace(BeginShaderReplacementBatch=lambda: 7,
        EndShaderReplacementBatch=lambda token: ended.append(token) or True)
    monkeypatch.setattr(bridge, '_replay', lambda fn: fn(ctrl))
    monkeypatch.setattr(bridge, '_begin_replay_atomic_transaction', lambda: None)
    monkeypatch.setattr(bridge, '_notify_shader_replay_changed', lambda: None)
    monkeypatch.setattr(bridge, '_apply_shader_candidate_shared_resource', lambda item: {})
    items = [{'operation_id': '%032x' % i, 'expected_capture_session_id': 'a' * 32,
              'abi_validation_mode': 'verified-rdc-restore'} for i in range(16)]
    assert len(bridge._apply_shader_candidate_shared_resource_batch({'items': items})['items']) == 16
    assert ended == [7]
    with pytest.raises(ValueError, match='1..16'):
        bridge._apply_shader_candidate_shared_resource_batch({'items': items + [items[0]]})
    assert ended == [7]


def test_native_mapping_confirmation_is_fresh_and_old_controllers_fall_back():
    bridge = load_bridge()
    original, target = ResourceId(7), ResourceId(8)
    assert bridge._verify_restored_shader_mapping(SimpleNamespace(), original, target) is False
    seen = []
    controller = SimpleNamespace(GetTargetShaderReplacement=lambda key: seen.append(key) or target)
    assert bridge._verify_restored_shader_mapping(controller, original, target) is True
    assert seen == [original]
    controller.GetTargetShaderReplacement = lambda key: ResourceId(9)
    with pytest.raises(RuntimeError, match='mapping differs'):
        bridge._verify_restored_shader_mapping(controller, original, target)
