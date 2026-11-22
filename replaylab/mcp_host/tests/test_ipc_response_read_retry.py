import errno
import json
import threading
import time

import pytest

from src import ipc_client


@pytest.mark.parametrize('failure',[PermissionError,FileNotFoundError])
def test_transient_response_read_retries_without_dispatching_again(tmp_path,monkeypatch,failure):
    requests=tmp_path/'requests';responses=tmp_path/'responses'
    requests.mkdir();responses.mkdir()
    monkeypatch.setattr(ipc_client,'IPC_DIR',str(tmp_path))
    monkeypatch.setattr(ipc_client,'REQUEST_DIR',str(requests))
    monkeypatch.setattr(ipc_client,'RESPONSE_DIR',str(responses))
    monkeypatch.setattr(ipc_client,'INSTANCE_ROOT',str(tmp_path/'no-instances'))
    received=[];denied=[]
    real_open=open
    def read_once(path,mode='r',*args,**kwargs):
        if str(responses) in str(path) and mode=='r' and not denied:
            denied.append(str(path))
            raise failure(errno.EACCES if failure is PermissionError else errno.ENOENT,'transient response reader')
        return real_open(path,mode,*args,**kwargs)
    monkeypatch.setattr(ipc_client,'open',read_once,raising=False)
    def bridge():
        until=time.monotonic()+3
        while time.monotonic()<until:
            rows=list(requests.glob('*.json'))
            if rows:
                request=json.loads(rows[0].read_text())
                received.append(request['id'])
                (responses/rows[0].name).write_text(json.dumps({'id':request['id'],'result':{'applied':True}}))
                return
            time.sleep(.01)
    worker=threading.Thread(target=bridge);worker.start()
    try:
        assert ipc_client.IPCClient(timeout=2).call('apply-once',{})=={'applied':True}
    finally:worker.join(3)
    assert len(received)==len(denied)==1
    assert list(requests.iterdir())==list(responses.iterdir())==[]
