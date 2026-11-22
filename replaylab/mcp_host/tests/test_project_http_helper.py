import base64
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from src import project_http_helper as helper

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location("project_helper_client",ROOT/"renderdoc_mcp_bridge/project_restore_client.py")
client=importlib.util.module_from_spec(spec);spec.loader.exec_module(client)


@contextmanager
def peer(delay=0,redirect=False):
    received=threading.Event();calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length',0)))
            calls.append(self.path);received.set();time.sleep(delay)
            try:
                body=b'{"observed":true}'
                self.send_response(302 if redirect else 200)
                if redirect:self.send_header('Location','http://example.invalid/forbidden')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError,OSError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield server.server_port,received,calls
    finally:server.shutdown();server.server_close();thread.join(2)


def transport(monkeypatch,cancel):
    original=subprocess.Popen;children=[]
    def start(command,**kwargs):
        assert command[1:]==['project-request']
        environment=kwargs['env'];environment['PYTHONPATH']=str(ROOT/'mcp_host')
        child=original([sys.executable,str(ROOT/'packaging/internal_host_entry.py'),'project-request'],**kwargs)
        children.append(child);return child
    monkeypatch.setattr(client.subprocess,'Popen',start)
    def check():
        if cancel.is_set():raise client.ReplayLabRestoreClientError('cancelled')
    instance=SimpleNamespace(internal_executable=str(sys.executable),monotonic=time.monotonic,_check_cancelled=check)
    return instance,children


def test_long_project_response_is_not_cut_off_by_the_native_half_second_wait(monkeypatch):
    cancel=threading.Event();instance,children=transport(monkeypatch,cancel)
    with peer(delay=1.2) as (port,received,calls):
        status,body=client.ReplayLabInternalProjectClient._project_transport(instance,port,'a'*48,'POST',client.PROJECT_OPERATION_PATH,b'{}',5,cancel,1)
    assert status==200 and json.loads(body)=={'observed':True} and len(calls)==1
    assert all(child.poll() is not None for child in children)


@pytest.mark.parametrize('cancelled',[False,True])
def test_project_wait_has_a_real_deadline_and_cancellation_without_retry_or_orphan(monkeypatch,cancelled):
    cancel=threading.Event();instance,children=transport(monkeypatch,cancel)
    with peer(delay=1.5) as (port,received,calls):
        def stop():
            received.wait(2);cancel.set()
        if cancelled:threading.Thread(target=stop,daemon=True).start()
        started=time.monotonic()
        with pytest.raises(client.ReplayLabRestoreClientError,match='cancelled' if cancelled else 'timed out'):
            client.ReplayLabInternalProjectClient._project_transport(instance,port,'a'*48,'POST',client.PROJECT_OPERATION_PATH,b'{}',3 if cancelled else .6,cancel,1)
        assert time.monotonic()-started<2
    assert len(calls)<=1 and all(child.poll() is not None for child in children)


def test_fixed_helper_does_not_follow_redirects_or_use_ambient_proxy(monkeypatch):
    monkeypatch.setenv('HTTP_PROXY','http://127.0.0.1:1')
    with peer(redirect=True) as (port,received,calls):
        value=helper.perform(dict(port=port,token='a'*48,method='POST',path=client.PROJECT_OPERATION_PATH,timeout=2,body=base64.b64encode(b'{}').decode()))
    assert value['status']==302 and len(calls)==1


def test_helper_rejects_arbitrary_endpoints_before_sending_credentials():
    with pytest.raises(ValueError):
        helper.perform(dict(port=12345,token='a'*48,method='POST',path='/other',timeout=1,body=''))


def test_cancel_remains_responsive_when_a_cold_helper_has_not_started_reading_stdin(monkeypatch):
    cancel=threading.Event();original=subprocess.Popen;children=[]
    def launch(command,**kwargs):
        child=original([sys.executable,'-c','import time; time.sleep(15)'],**kwargs)
        children.append(child);return child
    monkeypatch.setattr(client.subprocess,'Popen',launch)
    def check():
        if cancel.is_set():raise client.ReplayLabRestoreClientError('cancelled')
    instance=SimpleNamespace(internal_executable=sys.executable,monotonic=time.monotonic,_check_cancelled=check)
    timer=threading.Timer(.2,cancel.set);timer.start();started=time.monotonic()
    try:
        with pytest.raises(client.ReplayLabRestoreClientError,match='cancelled'):
            client.ReplayLabInternalProjectClient._project_transport(instance,12345,'a'*48,'POST',client.PROJECT_OPERATION_PATH,b'x'*(256*1024),5,cancel,1)
        assert time.monotonic()-started<2
        assert all(child.poll() is not None for child in children)
    finally:timer.cancel()
