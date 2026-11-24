"""Reopen an owned visual acceptance configuration on a private desktop."""
import argparse
import ctypes
from ctypes import wintypes as wt
import json
import os
from pathlib import Path
import subprocess
import time

parser=argparse.ArgumentParser();parser.add_argument('--app',type=Path,required=True)
parser.add_argument('--run',type=Path,required=True)
parser.add_argument('--ui-script',type=Path)
parser.add_argument('--result-name',default='visual-history-result.json')
parser.add_argument('--shader-config-workspace',type=Path)
parser.add_argument('--timeout',type=int,default=200)
parser.add_argument('--bundled-agent',action='store_true')
parser.add_argument('--use-user-local-data',action='store_true')
args=parser.parse_args()
repo=Path(__file__).resolve().parents[2];root=args.run.resolve(strict=True)
if not args.ui_script:
    assert json.loads((root/'ui-result.json').read_text(encoding='utf-8'))['modelPublishedTypes']
env={k:v for k,v in os.environ.items()if not k.startswith(('REPLAYLAB_','RENDERDOC_','BREAD_','PYTHON','QT_'))}
env.update(RENDERDOC_CONFIG_DIR=str(root/'config'),LOCALAPPDATA=str(root/'local'),APPDATA=str(root/'roaming'),
           RD_ANNOTATION_RUN=str(root),PYTHONIOENCODING='utf-8',RD_VISUAL_FIXTURE_ROOT=str(repo/'replaylab/tests/fixtures'))
if args.use_user_local_data:
    # Reuse the connection's own provider state without copying account files.
    # RENDERDOC_CONFIG_DIR still isolates the Qt conversation/configuration store.
    env['LOCALAPPDATA']=os.environ['LOCALAPPDATA']
if args.bundled_agent:
    env['REPLAYLAB_AI_CLI']=str(args.app.resolve().parent/'replaylab/replaylab-ai.exe')
if args.shader_config_workspace:
    plan=json.loads((args.shader_config_workspace/'manifest.json').read_text(encoding='utf-8'))['routePlan']
    env['RENDERDOC_ULTRA_SHADER_TOOLS_CONFIG']=str(Path(plan['toolRegistryPath']).resolve(strict=True))
    env['RENDERDOC_ULTRA_SHADER_TOOLCHAINS_CONFIG']=str(Path(plan['routeCatalogPath']).resolve(strict=True))
class Startup(ctypes.Structure):
    _fields_=[('cb',wt.DWORD),('reserved',wt.LPWSTR),('desktop',wt.LPWSTR),('title',wt.LPWSTR),
      ('x',wt.DWORD),('y',wt.DWORD),('cx',wt.DWORD),('cy',wt.DWORD),('charsx',wt.DWORD),('charsy',wt.DWORD),
      ('fill',wt.DWORD),('flags',wt.DWORD),('show',wt.WORD),('reservedSize',wt.WORD),
      ('reserved2',ctypes.c_void_p),('stdin',wt.HANDLE),('stdout',wt.HANDLE),('stderr',wt.HANDLE)]
class Process(ctypes.Structure):
    _fields_=[('process',wt.HANDLE),('thread',wt.HANDLE),('pid',wt.DWORD),('tid',wt.DWORD)]
user=ctypes.WinDLL('user32',use_last_error=True);kernel=ctypes.WinDLL('kernel32',use_last_error=True)
user.CreateDesktopW.argtypes=[wt.LPCWSTR,ctypes.c_void_p,ctypes.c_void_p,wt.DWORD,wt.DWORD,ctypes.c_void_p];user.CreateDesktopW.restype=wt.HANDLE
user.CloseDesktop.argtypes=[wt.HANDLE]
kernel.CreateProcessW.argtypes=[wt.LPCWSTR,wt.LPWSTR,ctypes.c_void_p,ctypes.c_void_p,wt.BOOL,wt.DWORD,
    ctypes.c_void_p,wt.LPCWSTR,ctypes.POINTER(Startup),ctypes.POINTER(Process)]
kernel.WaitForSingleObject.argtypes=[wt.HANDLE,wt.DWORD];kernel.CloseHandle.argtypes=[wt.HANDLE]
kernel.TerminateProcess.argtypes=[wt.HANDLE,wt.UINT]
name='BreadVisualHistory'+str(os.getpid());desktop=user.CreateDesktopW(name,None,None,0,0x01ff,None)
if not desktop:raise ctypes.WinError(ctypes.get_last_error())
startup=Startup();startup.cb=ctypes.sizeof(Startup);startup.desktop=name;process=Process()
command=ctypes.create_unicode_buffer(subprocess.list2cmdline([str(args.app.resolve()),'--ui-python',
    str(args.ui_script.resolve()if args.ui_script else repo/'replaylab/tests/fixtures/qrenderdoc_visual_reply_history.py')]))
block=ctypes.create_unicode_buffer('\0'.join(k+'='+v for k,v in sorted(env.items()))+'\0\0')
try:
    if not kernel.CreateProcessW(str(args.app.resolve()),command,None,None,False,0x08000400,block,str(root),ctypes.byref(startup),ctypes.byref(process)):
        raise ctypes.WinError(ctypes.get_last_error())
    deadline=time.monotonic()+args.timeout
    while kernel.WaitForSingleObject(process.process,1000)==258:
        if time.monotonic()>deadline:
            kernel.TerminateProcess(process.process,1);raise TimeoutError('Owned history process timed out')
finally:
    if process.thread:kernel.CloseHandle(process.thread)
    if process.process:kernel.CloseHandle(process.process)
    user.CloseDesktop(desktop)
result=json.loads((root/args.result_name).read_text(encoding='utf-8'))
print(json.dumps(result,ensure_ascii=False));raise SystemExit(0 if result.get('ok') else 1)
