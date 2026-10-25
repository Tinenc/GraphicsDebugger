#!/usr/bin/env python3
"""Drive the TinecmaTool global hook without the GUI.

What StartGlobalHook does internally, and what this reproduces:

  1. spawn `TinecmaToolcmd globalhook --match=... --capfile=... --debuglog=...`
     which creates the shared memory TinecmaToolGlobalHookData64/32 and then
     blocks reading stdin (that pipe is the liveness signal for the session)
  2. write HKLM AppInit_DLLs + LoadAppInit_DLLs so every new process loads
     TinecmaToolshim64.dll / shim32.dll - the shim compares the full exe path
     against the match string and only then loads TinecmaTool.dll

Modes:
  python globalhook_guard.py start [--match sp26-cod.exe]   start session + registry
  python globalhook_guard.py guard                          keepalive only (run detached)
  python globalhook_guard.py stop                           restore registry, end session
  python globalhook_guard.py status                         show registry + session state

Needs admin (HKLM write).
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TOOL = os.path.join(ROOT, "x64", "Development", "TinecmaToolcmd.exe")
SHIM64 = os.path.join(ROOT, "x64", "Development", "TinecmaToolshim64.dll")
SHIM32 = os.path.join(ROOT, "Win32", "Development", "TinecmaToolshim32.dll")
CAPTURES = os.path.join(ROOT, "artifacts", "steam-cod-captures")
STATE = os.path.join(HERE, "globalhook-state.json")

KEY_NATIVE = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Windows"
KEY_WOW32 = r"SOFTWARE\Wow6432Node\Microsoft\Windows NT\CurrentVersion\Windows"
MAP64 = "TinecmaToolGlobalHookData64"

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenFileMappingA.restype = wt.HANDLE
kernel32.OpenFileMappingA.argtypes = [wt.DWORD, wt.BOOL, wt.LPCSTR]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.GetShortPathNameW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.DWORD]
kernel32.GetShortPathNameW.restype = wt.DWORD


def short_path(p):
    buf = ctypes.create_unicode_buffer(1024)
    n = kernel32.GetShortPathNameW(p, buf, 1024)
    if n == 0 or n >= len(p) + 1:
        return None
    return buf.value.replace("/", "\\")


def session_alive():
    h = kernel32.OpenFileMappingA(0x0004, False, MAP64.encode("ascii"))  # FILE_MAP_READ
    if not h:
        return False
    kernel32.CloseHandle(h)
    return True


def read_reg():
    import winreg
    out = {}
    for key, label in ((KEY_NATIVE, "native"), (KEY_WOW32, "wow32")):
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key)
        entry = {}
        for name in ("AppInit_DLLs", "LoadAppInit_DLLs"):
            try:
                entry[name] = winreg.QueryValueEx(k, name)[0]
            except FileNotFoundError:
                entry[name] = None
        winreg.CloseKey(k)
        out[label] = entry
    return out


def write_reg(shim_map):
    import winreg
    for key, shimpath in ((KEY_NATIVE, shim_map.get("native")),
                          (KEY_WOW32, shim_map.get("wow32"))):
        if not shimpath:
            continue
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_READ | winreg.KEY_WRITE)
        winreg.SetValueEx(k, "LoadAppInit_DLLs", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(k, "AppInit_DLLs", 0, winreg.REG_SZ, shimpath)
        winreg.CloseKey(k)


def restore_reg(saved):
    import winreg
    for key, label in ((KEY_NATIVE, "native"), (KEY_WOW32, "wow32")):
        entry = saved.get(label, {})
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_READ | winreg.KEY_WRITE)
        winreg.SetValueEx(k, "LoadAppInit_DLLs", 0, winreg.REG_DWORD, int(entry.get("LoadAppInit_DLLs") or 0))
        winreg.SetValueEx(k, "AppInit_DLLs", 0, winreg.REG_SZ, entry.get("AppInit_DLLs") or "")
        winreg.CloseKey(k)


def guard():
    """Hold the globalhook child's stdin open so the session stays alive."""
    match = sys.argv[2] if len(sys.argv) > 2 else "sp26-cod.exe"
    os.makedirs(CAPTURES, exist_ok=True)
    cmd = [TOOL, "globalhook",
           "--match=%s" % match,
           "--capfile=%s" % os.path.join(CAPTURES, "steam-cod"),
           "--debuglog=%s" % os.path.join(CAPTURES, "globalhook-runtime.log"),
           # capopts is mandatory even though it accepts an empty value; omitting it makes the
           # command fail with "need option: --capopts" and then die with 0xC0000409
           "--capopts="]
    child = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
    with open(os.path.join(HERE, "globalhook-guard.pid"), "w") as fp:
        fp.write(str(os.getpid()))
    print("guard pid=%d child pid=%d match=%s" % (os.getpid(), child.pid, match), flush=True)
    try:
        while child.poll() is None:
            time.sleep(1)
        print("child exited rc=%s" % child.returncode, flush=True)
        out = child.stdout.read() or b""
        print(out.decode("mbcs", "replace")[:800], flush=True)
    except KeyboardInterrupt:
        child.terminate()
    return 0


def do_start(match):
    os.makedirs(CAPTURES, exist_ok=True)

    s64, s32 = short_path(SHIM64), short_path(SHIM32)
    if not s64:
        print("[fail] no 8.3 short path for", SHIM64)
        return 2
    print("[ok] shim64 short path: %s" % s64)
    print("[ok] shim32 short path: %s" % (s32 or "<none>"))

    if not session_alive():
        print("[..] starting guard (detached)")
        d = subprocess.Popen([sys.executable, os.path.abspath(__file__), "guard", match],
                             creationflags=0x00000008 | 0x00000200,  # DETACHED_PROCESS|NEW_PROCESS_GROUP
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True)
        print("[ok] guard launched pid=%d" % d.pid)
        for _ in range(20):
            time.sleep(0.25)
            if session_alive():
                break
    else:
        print("[ok] an existing global hook session is already alive")

    if not session_alive():
        print("[fail] shared memory %s never appeared - globalhook child died" % MAP64)
        return 3
    print("[ok] shared memory %s is alive" % MAP64)

    saved = read_reg()
    with open(STATE, "w", encoding="utf-8") as fp:
        json.dump(saved, fp, indent=2)
    print("[ok] saved previous registry ->", STATE)
    print("     before:", json.dumps(saved))

    write_reg({"native": s64, "wow32": s32})
    print("[ok] registry written:")
    for label, entry in read_reg().items():
        print("     %-6s %s" % (label, entry))
    print("\nNow launch the game from Steam. Verify with:")
    print("  python artifacts\\steam-cod-recon\\diag_hook_verify.py")
    print("Restore afterwards with:")
    print("  python artifacts\\steam-cod-recon\\globalhook_guard.py stop")
    return 0


def do_stop():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as fp:
            saved = json.load(fp)
        restore_reg(saved)
        print("[ok] registry restored to:", json.dumps(saved))
    else:
        print("[warn] no saved state, writing empty values")
        restore_reg({"native": {}, "wow32": {}})

    pidfile = os.path.join(HERE, "globalhook-guard.pid")
    if os.path.exists(pidfile):
        try:
            pid = int(open(pidfile).read().strip())
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
            print("[ok] guard pid %d killed" % pid)
        except Exception as e:
            print("[warn] could not kill guard:", e)
        os.remove(pidfile)

    for p in ("TinecmaToolcmd.exe",):
        subprocess.run(["taskkill", "/IM", p, "/F"], capture_output=True)
    print("[ok] session torn down; shared memory gone:", not session_alive())
    return 0


def do_status():
    print("registry:", json.dumps(read_reg()))
    print("session alive:", session_alive())
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    mode = sys.argv[1]
    if mode == "guard":
        sys.exit(guard())
    if mode == "start":
        match = "sp26-cod.exe"
        for a in sys.argv[2:]:
            if a.startswith("--match"):
                match = a.split("=", 1)[-1]
        sys.exit(do_start(match))
    if mode == "stop":
        sys.exit(do_stop())
    if mode == "status":
        sys.exit(do_status())
    print(__doc__)
    sys.exit(1)
