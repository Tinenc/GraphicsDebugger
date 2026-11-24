import json
from types import SimpleNamespace

import pytest

from src import shader_live_edit_client as client, ipc_client, internal_host


def binding():
    return {'instanceId':'321-'+'a'*32,'captureSessionId':'b'*32,'capturePath':'D:/frame.rdc'}


def test_task_uses_bound_application_runtime_even_with_foreign_inherited_environment(tmp_path,monkeypatch):
    monkeypatch.setattr(ipc_client,'INSTANCE_ROOT',str(tmp_path/'instances'))
    monkeypatch.setenv('REPLAYLAB_INTERNAL_RUNTIME_DIR',str(tmp_path/'foreign-host'))
    assert client._directory_for_binding(binding())==tmp_path/'instances'/binding()['instanceId']/'internal-runtime'
    with pytest.raises(RuntimeError): client._directory_for_binding({'instanceId':'../foreign'})


def test_missing_service_recovers_before_dispatch_without_any_shell_permission(tmp_path,monkeypatch):
    calls=[];attempts=[]
    monkeypatch.setattr(client,'_directory_for_binding',lambda *args:tmp_path)
    def endpoint(root):
        attempts.append(root)
        if len(attempts)<3: raise RuntimeError('missing state')
        return 3456,'c'*32,{},'token'
    monkeypatch.setattr(client,'_endpoint',endpoint)
    monkeypatch.setattr(client.time,'sleep',lambda seconds:None)
    monkeypatch.setattr(ipc_client,'IPCClient',lambda **kwargs:SimpleNamespace(call=lambda *a,**k:calls.append((a,k))))
    monkeypatch.setattr(internal_host,'_validate_generation',lambda *args:'c'*32)
    def respond(request,timeout):
        body=json.loads(request.data)
        return {**body,'hostInstanceId':'c'*32,'result':{'applied':True}}
    monkeypatch.setattr(client,'_read_response',respond)
    assert client.request('apply',{},binding())=={'applied':True}
    assert len(calls)==1 and calls[0][0][0]=='ensure_internal_shader_service'
    assert len(attempts)==3


def test_mutation_reply_loss_never_restarts_or_repeats_the_operation(tmp_path,monkeypatch):
    calls=[]
    monkeypatch.setattr(client,'_directory_for_binding',lambda *args:tmp_path)
    monkeypatch.setattr(client,'_endpoint',lambda root:(3456,'c'*32,{},'token'))
    monkeypatch.setattr(ipc_client,'IPCClient',lambda **kwargs:(_ for _ in ()).throw(AssertionError('No restart after dispatch')))
    def lost(request,timeout):
        calls.append(request)
        raise TimeoutError('reply lost after dispatch')
    monkeypatch.setattr(client,'_read_response',lost)
    with pytest.raises(TimeoutError):client.request('apply',{},binding())
    assert len(calls)==1
