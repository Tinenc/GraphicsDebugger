"""Run two real GUIs on a private desktop with one disposable user profile."""
import argparse
import ctypes
from ctypes import wintypes as wt
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time


class Startup(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("reserved", wt.LPWSTR), ("desktop", wt.LPWSTR), ("title", wt.LPWSTR),
        ("x", wt.DWORD), ("y", wt.DWORD), ("sx", wt.DWORD), ("sy", wt.DWORD), ("cx", wt.DWORD),
        ("cy", wt.DWORD), ("fill", wt.DWORD), ("flags", wt.DWORD), ("show", wt.WORD),
        ("reserved2size", wt.WORD), ("reserved2", ctypes.c_void_p), ("stdin", wt.HANDLE),
        ("stdout", wt.HANDLE), ("stderr", wt.HANDLE)]


class Process(ctypes.Structure):
    _fields_ = [("process", wt.HANDLE), ("thread", wt.HANDLE), ("pid", wt.DWORD), ("tid", wt.DWORD)]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True); parser.add_argument("--receipt", type=Path, required=True)
    args=parser.parse_args()
    kernel=ctypes.WinDLL("kernel32",use_last_error=True); user=ctypes.WinDLL("user32",use_last_error=True)
    user.CreateDesktopW.restype=wt.HANDLE
    user.CreateDesktopW.argtypes=[wt.LPCWSTR,ctypes.c_void_p,ctypes.c_void_p,wt.DWORD,wt.DWORD,ctypes.c_void_p]
    user.CloseDesktop.argtypes=[wt.HANDLE]
    kernel.CreateProcessW.argtypes=[wt.LPCWSTR,wt.LPWSTR,ctypes.c_void_p,ctypes.c_void_p,wt.BOOL,
        wt.DWORD,ctypes.c_void_p,wt.LPCWSTR,ctypes.POINTER(Startup),ctypes.POINTER(Process)]
    kernel.WaitForSingleObject.argtypes=[wt.HANDLE,wt.DWORD]
    kernel.TerminateProcess.argtypes=[wt.HANDLE,wt.UINT]; kernel.CloseHandle.argtypes=[wt.HANDLE]
    desktop_name="BreadInstanceCheck"+str(os.getpid())
    desktop=user.CreateDesktopW(desktop_name,None,None,0,0x1ff,None)
    if not desktop: raise ctypes.WinError(ctypes.get_last_error())
    processes=[]
    try:
        with tempfile.TemporaryDirectory(prefix="bread-multiple-apps-", ignore_cleanup_errors=True) as work:
            work=Path(work)
            env={k:v for k,v in os.environ.items() if not k.startswith(("REPLAYLAB_","RENDERDOC_","BREAD_","PYTHON","QT_"))}
            for key,name in {"LOCALAPPDATA":"local","APPDATA":"roaming","USERPROFILE":"user","RENDERDOC_CONFIG_DIR":"ui","REPLAYLAB_CONFIG_DIR":"config"}.items():
                folder=work/name;folder.mkdir();env[key]=str(folder)
            env["HOME"]=env["USERPROFILE"]
            # Suppress first-run tips/update dialogs before the UI script runs.
            config = {"rdocConfigData":1,"UILanguage":"en_US","Analytics_TotalOptOut":True,
                      "CheckUpdate_AllowChecks":False,"Tips_HasSeenFirst":True}
            for folder in (work/"ui",work/"local/RenderDocBread/ui"):
                folder.mkdir(parents=True,exist_ok=True)
                (folder/"UI.config").write_text(json.dumps(config),encoding="utf-8")
            script=work/"observe.py"
            script.write_text('''import os,json
from PySide2 import QtCore,QtWidgets
def observe():
    keys=("REPLAYLAB_INSTANCE_ID","REPLAYLAB_RUNTIME_DIR","REPLAYLAB_INTERNAL_RUNTIME_DIR",
          "RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE","RENDERDOC_ULTRA_PROJECT_REGISTRY")
    with open(os.environ["BREAD_INSTANCE_REPORT"],"w") as stream:json.dump({k:os.environ.get(k) for k in keys},stream)
    QtCore.QTimer.singleShot(8000,pyrenderdoc.GetMainWindow().Widget().close)
def start():
    app=QtWidgets.QApplication.instance()
    app._bread_instance_timer=QtCore.QTimer(app)
    app._bread_instance_timer.setSingleShot(True)
    app._bread_instance_timer.timeout.connect(observe)
    app._bread_instance_timer.start(2500)
pyrenderdoc.Extensions().GetMiniQtHelper().InvokeOntoUIThread(start)
''',encoding="utf-8")
            for i in range(2):
                child_env=dict(env,BREAD_INSTANCE_REPORT=str(work/(str(i)+".json")))
                block=ctypes.create_unicode_buffer("\0".join(k+"="+v for k,v in sorted(child_env.items()))+"\0\0")
                startup=Startup();startup.cb=ctypes.sizeof(startup);startup.desktop=desktop_name
                process=Process();command=ctypes.create_unicode_buffer(subprocess.list2cmdline([str(args.app.resolve()),"--ui-script",str(script)]))
                if not kernel.CreateProcessW(str(args.app.resolve()),command,None,None,False,0x08000400,block,str(work),ctypes.byref(startup),ctypes.byref(process)):
                    raise ctypes.WinError(ctypes.get_last_error())
                processes.append(process)
            deadline=time.monotonic()+40
            while not all((work/(str(i)+".json")).exists() for i in range(2)):
                if time.monotonic()>=deadline:
                    debug={"files":[str(p.relative_to(work)) for p in work.rglob("*") if p.is_file()],
                           "bridgeLogs":[p.read_text(encoding="utf-8",errors="replace")[-6000:] for p in (work/"local/ReplayLab/logs").glob("bridge-*.jsonl")]}
                    args.receipt.with_suffix(".debug.json").write_text(json.dumps(debug,indent=2),encoding="utf-8")
                    raise RuntimeError("Two-GUI observation timed out")
                time.sleep(.1)
            rows=[json.loads((work/(str(i)+".json")).read_text()) for i in range(2)]
            for key in ("REPLAYLAB_INSTANCE_ID","REPLAYLAB_RUNTIME_DIR","REPLAYLAB_INTERNAL_RUNTIME_DIR","RENDERDOC_ULTRA_ACTIVE_PROJECT_STATE"):
                if not rows[0][key] or rows[0][key]==rows[1][key]:raise RuntimeError("Shared application routing: "+key)
            if not rows[0]["RENDERDOC_ULTRA_PROJECT_REGISTRY"] or rows[0]["RENDERDOC_ULTRA_PROJECT_REGISTRY"]!=rows[1]["RENDERDOC_ULTRA_PROJECT_REGISTRY"]:
                raise RuntimeError("Project library must remain shared")
            for process in processes:
                if kernel.WaitForSingleObject(process.process,25000)!=0:raise RuntimeError("Private GUI did not close")
            args.receipt.write_text(json.dumps({"ok":True,"instances":2,"sameUserProfile":True,
                "checks":["distinct-instance-binding","distinct-mcp-service","distinct-internal-service",
                    "distinct-active-project-selection","shared-project-library","normal-window-close"],
                "scope":"Actual GUI routing; independent project selection and concurrent writes are tested separately. No model calls, captures or Unity changes."},indent=2),encoding="utf-8")
    finally:
        # Only processes created by this private-desktop fixture are eligible.
        for process in processes:
            if kernel.WaitForSingleObject(process.process,0)!=0:kernel.TerminateProcess(process.process,1);kernel.WaitForSingleObject(process.process,5000)
            kernel.CloseHandle(process.thread);kernel.CloseHandle(process.process)
        user.CloseDesktop(desktop)
        if "work" in locals() and Path(work).exists():
            from verify_core_release import clean_owned_temporary
            clean_owned_temporary(Path(work), "bread-multiple-apps-")


if __name__=="__main__":main()
