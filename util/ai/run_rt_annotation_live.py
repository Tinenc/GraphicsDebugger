"""Real API model + production native composer, on a private Windows desktop.

Clones only the selected connection definition; OS-vault credentials remain in memory.
A loopback relay forwards unchanged requests to that connection's real endpoint and
records image counts/hashes, never headers or credentials. User settings stay untouched.
"""
from __future__ import annotations
import argparse
import base64
import ctypes
from ctypes import wintypes as wt
from dataclasses import asdict
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'replaylab/src'))
from replaylab_agent.profiles import ProfileStore, PROFILE_SCHEMA


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--app', type=Path, required=True)
    parser.add_argument('--capture', type=Path, required=True)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=900)
    parser.add_argument('--request-timeout', type=int, default=0,
                        help='Override provider timeout in the isolated test profile only')
    parser.add_argument('--spec', type=Path, help='Scenario event/resource/marks/prompts JSON')
    parser.add_argument('--bundled-agent', action='store_true', help='Use the application\'s frozen AgentHost')
    parser.add_argument('--ui-script',type=Path,help='Alternative owned UI acceptance fixture')
    parser.add_argument('--shader-config-workspace',type=Path,
                        help='Reuse this managed workspace\'s exact, integrity-checked compiler registry')
    args = parser.parse_args()
    profile = ProfileStore().load().profiles[args.profile]
    if profile.provider.kind != 'model':
        raise ValueError('This audited API fixture requires a model API connection')
    profile.resolve_model(args.model)
    upstream = profile.endpoint.rstrip('/')
    root = Path(tempfile.mkdtemp(prefix='bread-rt-annotation-live-'))
    for folder in ('config', 'local', 'roaming', 'logs'):
        (root / folder).mkdir()
    audit_lock = threading.Lock()
    request_index = [0]

    def audit(value):
        with audit_lock:
            with (root / 'provider-audit.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(value, ensure_ascii=False)+'\n')

    class Relay(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.0'

        def log_message(self, *unused):
            pass

        def do_GET(self):
            self.forward()

        def do_POST(self):
            self.forward()

        def forward(self):
            import httpx
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            with audit_lock:
                request_index[0] += 1
                number = request_index[0]
            images = []
            try:
                payload = json.loads(body) if body else {}
                def inspect(value):
                    if isinstance(value, dict):
                        for item in value.values(): inspect(item)
                    elif isinstance(value, list):
                        for item in value: inspect(item)
                    elif isinstance(value, str) and value.startswith('data:image/') and ';base64,' in value:
                        data = base64.b64decode(value.split(',', 1)[1])
                        images.append({'bytes':len(data), 'sha256':hashlib.sha256(data).hexdigest()})
                inspect(payload)
                audit({'request':number, 'path':self.path, 'model':payload.get('model'),
                       'inputImages':images, 'inputImageCount':len(images),
                       'messageCount':len(payload.get('messages', [])), 'timestamp':time.time()})
            except ValueError:
                audit({'request':number, 'nonJson':True})
            # Forward only required protocol headers. The auth value is never recorded.
            headers = {key:value for key,value in self.headers.items()
                       if key.lower() in ('authorization','content-type','accept','openai-organization','openai-project')}
            headers['Accept-Encoding'] = 'identity'
            try:
                with httpx.Client(timeout=httpx.Timeout(120, read=args.request_timeout or 600)) as client:
                    with client.stream(self.command, upstream+self.path, content=body, headers=headers) as response:
                        self.send_response(response.status_code)
                        self.send_header('Content-Type', response.headers.get('content-type','application/json'))
                        self.send_header('Connection','close')
                        self.end_headers()
                        audit({'request':number,'httpStatus':response.status_code})
                        for chunk in response.iter_raw():
                            self.wfile.write(chunk)
                            self.wfile.flush()
            except Exception as error:
                audit({'request':number,'transportError':type(error).__name__})

    relay = ThreadingHTTPServer(('127.0.0.1', 0), Relay)
    relay.daemon_threads = True
    thread = threading.Thread(target=relay.serve_forever, daemon=True)
    thread.start()
    clone = asdict(profile)
    clone.update(base_url='http://127.0.0.1:'+str(relay.server_port), model=args.model,
                 default_model=args.model, models=[args.model])
    clone['options'] = dict(clone.get('options') or {})
    if args.request_timeout:
        if not 30 <= args.request_timeout <= 1800:
            raise ValueError('Request timeout must be 30..1800 seconds')
        clone['options']['timeout_seconds'] = args.request_timeout
    profiles = root/'roaming/ReplayLab/ai/profiles.json'
    profiles.parent.mkdir(parents=True)
    profiles.write_text(json.dumps({'schema':PROFILE_SCHEMA,'activeProfile':profile.id,'profiles':[clone]}),encoding='utf-8')
    (root/'config/UI.config').write_text(json.dumps({'rdocConfigData':1,'UILanguage':'zh_CN',
        'Analytics_TotalOptOut':True,'CheckUpdate_AllowChecks':False,'Tips_HasSeenFirst':True}),encoding='utf-8')
    spec = {'capture':str(args.capture.resolve()),'profile':profile.id,'model':args.model,
            'eventsRoot':str(root/'local/ReplayLab/ai-tasks'),'timeout':args.timeout-15,
            'cases':[
                {'id':'object','prompt':'我圈出的这个物体是怎么一步步渲染出来的？请先说明你理解的是哪个物体，再查捕获证据，简洁说明主要绘制过程和关键 EID。只做分析，不修改捕获或着色器。',
                 'marks':[{'tool':'ellipse','points':[[60/960,175/640],[460/960,480/640]]}]},
                {'id':'color','prompt':'我圈出的这一小片背景为什么泛黄发亮？帮我追一下后处理对这块区域颜色的影响，说明哪些步骤实际改变了它。请结合标记和捕获证据简洁回答，只分析，不修改。',
                 'marks':[{'tool':'ellipse','points':[[72/960,290/640],[106/960,370/640]]},
                          {'tool':'arrow','points':[[260/960,300/640],[94/960,330/640]]}]}
            ]}
    if args.spec:
        scenario=json.loads(args.spec.read_text(encoding='utf-8'))
        for key in ('eventId','resourceId','switchEventId','cases','projectPath'):
            if key in scenario:spec[key]=scenario[key]
    if spec.get('projectPath'):
        original_state=Path(os.environ['LOCALAPPDATA'])/'ReplayLab/config/active-project.json'
        state=json.loads(original_state.read_text(encoding='utf-8'))
        if Path(state['projectRoot']).resolve()!=Path(spec['projectPath']).resolve():
            raise ValueError('Scenario project differs from the explicitly selected project')
        isolated_state=root/'local/ReplayLab/config/active-project.json'
        isolated_state.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(original_state,isolated_state)
        registry=json.loads((original_state.parent/'project-registry.json').read_text(encoding='utf-8'))
        registry['projects']=[row for row in registry['projects']if row['workspaceId']==state['workspaceId']]
        if len(registry['projects'])!=1:raise ValueError('Selected project registration is not unique')
        registry.pop('registrySha256',None)
        registry['registrySha256']=hashlib.sha256(json.dumps(registry,ensure_ascii=False,sort_keys=True,
            separators=(',',':'),allow_nan=False).encode('utf-8')).hexdigest()
        (isolated_state.parent/'project-registry.json').write_text(json.dumps(registry,ensure_ascii=False,indent=2),encoding='utf-8')
    (root/'spec.json').write_text(json.dumps(spec,ensure_ascii=False,indent=2),encoding='utf-8')
    env = {k:v for k,v in os.environ.items() if not k.startswith(('REPLAYLAB_','RENDERDOC_','BREAD_','PYTHON','QT_'))}
    env.update(RENDERDOC_CONFIG_DIR=str(root/'config'),LOCALAPPDATA=str(root/'local'),
               APPDATA=str(root/'roaming'),RD_ANNOTATION_RUN=str(root),PYTHONIOENCODING='utf-8')
    env['RD_VISUAL_FIXTURE_ROOT']=str(ROOT/'replaylab/tests/fixtures')
    if args.shader_config_workspace:
        manifest=json.loads((args.shader_config_workspace.resolve(strict=True)/'manifest.json').read_text(encoding='utf-8'))
        plan=manifest['routePlan']
        env['RENDERDOC_ULTRA_SHADER_TOOLS_CONFIG']=str(Path(plan['toolRegistryPath']).resolve(strict=True))
        env['RENDERDOC_ULTRA_SHADER_TOOLCHAINS_CONFIG']=str(Path(plan['routeCatalogPath']).resolve(strict=True))
        # The GUI's persistent owner has its own installation-scoped registry.
        # Validate/materialize that scope before spending a real model turn;
        # forwarding another workspace's environment alone does not stage it.
        sys.path.insert(0, str(ROOT/'replaylab/mcp_host'))
        from src.replaylab_tools import configure_environment, TOOLS_ENV, ROUTES_ENV
        owner_environment=dict(env)
        owner_environment.pop(TOOLS_ENV,None)
        owner_environment.pop(ROUTES_ENV,None)
        discovery=configure_environment(args.app.resolve().parent/'replaylab',owner_environment)
        if discovery.get('error') or not all(Path(owner_environment[key]).is_file()
                                             for key in (TOOLS_ENV,ROUTES_ENV)):
            raise RuntimeError('Owned test application lacks its Shader tool registry: '+str(discovery.get('error')))
    if args.bundled_agent:
        env['REPLAYLAB_AI_CLI']=str(args.app.resolve().parent/'replaylab/replaylab-ai.exe')
    print(json.dumps({'run':str(root),'model':args.model,'app':str(args.app.resolve())}),flush=True)
    launch = {'schema':'bread-rt-annotation-real-model-launch/v1','run':str(root),'model':args.model,
              'app':str(args.app.resolve()),'profile':profile.id}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(launch,indent=2),encoding='utf-8')

    class Startup(ctypes.Structure):
        _fields_=[('cb',wt.DWORD),('reserved',wt.LPWSTR),('desktop',wt.LPWSTR),('title',wt.LPWSTR),
          ('x',wt.DWORD),('y',wt.DWORD),('cx',wt.DWORD),('cy',wt.DWORD),('charsx',wt.DWORD),('charsy',wt.DWORD),
          ('fill',wt.DWORD),('flags',wt.DWORD),('show',wt.WORD),('reservedSize',wt.WORD),
          ('reserved2',ctypes.c_void_p),('stdin',wt.HANDLE),('stdout',wt.HANDLE),('stderr',wt.HANDLE)]
    class Process(ctypes.Structure):
        _fields_=[('process',wt.HANDLE),('thread',wt.HANDLE),('pid',wt.DWORD),('tid',wt.DWORD)]
    user=ctypes.WinDLL('user32',use_last_error=True);kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    user.CreateDesktopW.argtypes=[wt.LPCWSTR,ctypes.c_void_p,ctypes.c_void_p,wt.DWORD,wt.DWORD,ctypes.c_void_p]
    user.CreateDesktopW.restype=wt.HANDLE;user.CloseDesktop.argtypes=[wt.HANDLE]
    kernel.CreateProcessW.argtypes=[wt.LPCWSTR,wt.LPWSTR,ctypes.c_void_p,ctypes.c_void_p,wt.BOOL,wt.DWORD,
        ctypes.c_void_p,wt.LPCWSTR,ctypes.POINTER(Startup),ctypes.POINTER(Process)]
    kernel.WaitForSingleObject.argtypes=[wt.HANDLE,wt.DWORD];kernel.GetExitCodeProcess.argtypes=[wt.HANDLE,ctypes.POINTER(wt.DWORD)]
    kernel.CloseHandle.argtypes=[wt.HANDLE];kernel.TerminateProcess.argtypes=[wt.HANDLE,wt.UINT]
    desktop_name='BreadAnnotationLive'+str(os.getpid())
    desktop=user.CreateDesktopW(desktop_name,None,None,0,0x01ff,None)
    if not desktop: raise ctypes.WinError(ctypes.get_last_error())
    startup=Startup();startup.cb=ctypes.sizeof(Startup);startup.desktop=desktop_name
    process=Process()
    command=ctypes.create_unicode_buffer(subprocess.list2cmdline([str(args.app.resolve()),'--ui-python',
        str(args.ui_script.resolve() if args.ui_script else ROOT/'replaylab/tests/fixtures/qrenderdoc_rt_annotation_live.py')]))
    block=ctypes.create_unicode_buffer('\0'.join(k+'='+v for k,v in sorted(env.items()))+'\0\0')
    try:
        if not kernel.CreateProcessW(str(args.app.resolve()),command,None,None,False,0x08000400,block,str(root),ctypes.byref(startup),ctypes.byref(process)):
            raise ctypes.WinError(ctypes.get_last_error())
        deadline=time.monotonic()+args.timeout
        while kernel.WaitForSingleObject(process.process,1000)==258:
            if time.monotonic()>deadline:
                kernel.TerminateProcess(process.process,1)
                raise TimeoutError('Owned test process timed out')
        code=wt.DWORD();kernel.GetExitCodeProcess(process.process,ctypes.byref(code))
    finally:
        if process.thread: kernel.CloseHandle(process.thread)
        if process.process: kernel.CloseHandle(process.process)
        user.CloseDesktop(desktop)
        relay.shutdown();relay.server_close()
    result_path=root/'ui-result.json'
    result=json.loads(result_path.read_text(encoding='utf-8')) if result_path.is_file() else {'ok':False,'error':'UI fixture did not start'}
    result.update(run=str(root),exitCode=code.value,model=args.model)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'ok':result.get('ok'),'run':str(root),'error':result.get('error')}),flush=True)
    return 0 if result.get('ok') and code.value==0 else 1


if __name__=='__main__':
    raise SystemExit(main())
