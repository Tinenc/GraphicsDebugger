"""One fixed authenticated project request; no proxy, redirects, retries or capture operations."""
import base64
import json
import re
import sys
import os
import threading
import urllib.error
import urllib.request

MAX_BYTES = 16 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, url):
        return None


def perform(value):
    port = value.get('port')
    token = value.get('token', '')
    timeout = value.get('timeout')
    if (isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535
            or not isinstance(token, str) or re.fullmatch(r'[A-Za-z0-9_-]{32,128}', token) is None
            or not isinstance(timeout, (int, float)) or not 0 < timeout <= 90
            or value.get('method') != 'POST'
            or value.get('path') != '/replaylab/internal/project-operation'):
        raise ValueError('Invalid fixed project request')
    body = base64.b64decode(value.get('body', ''), validate=True)
    if len(body) > MAX_BYTES:
        raise ValueError('Project request exceeds its limit')
    request = urllib.request.Request(
        'http://127.0.0.1:%d/replaylab/internal/project-operation' % port,
        data=body, method='POST', headers={
            'Authorization': 'Bearer ' + token,
            'Content-Type': 'application/json; charset=utf-8', 'Accept': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        payload = response.read(MAX_BYTES + 1)
        if len(payload) > MAX_BYTES:
            raise ValueError('Project response exceeds its limit')
        return {'status': response.status, 'body': base64.b64encode(payload).decode('ascii')}


def main():
    # The one-file bootloader has its own parent process. Cancellation kills that
    # owned controller; its worker must not remain blocked in a slow HTTP read.
    if os.name=='nt' and getattr(sys,'frozen',False):
        import ctypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=(ctypes.c_uint32,ctypes.c_int,ctypes.c_uint32)
        kernel.OpenProcess.restype=ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes=(ctypes.c_void_p,ctypes.c_uint32)
        kernel.WaitForSingleObject.restype=ctypes.c_uint32
        kernel.CloseHandle.argtypes=(ctypes.c_void_p,)
        owner=kernel.OpenProcess(0x00100000,False,os.getppid())
        if not owner:return 3
        def watch():
            try:
                if kernel.WaitForSingleObject(owner,0xffffffff)==0:os._exit(3)
            finally:kernel.CloseHandle(owner)
        threading.Thread(target=watch,daemon=True).start()
    try:
        envelope_limit=4*((MAX_BYTES+2)//3)+4096
        raw = sys.stdin.buffer.read(envelope_limit + 1)
        if len(raw) > envelope_limit:
            raise ValueError('Helper input exceeds its limit')
        result = perform(json.loads(raw.decode('utf-8')))
    except Exception as error:
        result = {'errorType': type(error).__name__, 'error': str(error)[:1024]}
    sys.stdout.write(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
