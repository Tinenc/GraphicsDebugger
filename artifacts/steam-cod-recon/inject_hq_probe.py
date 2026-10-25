#!/usr/bin/env python3
"""Inject the HQ shell as early as possible and report whether it is inspectable.

cod.exe accepted the DLL in the race test. The open question is whether its module
list can be enumerated - TinecmaToolcmd only runs the INTERNAL_* configuration
calls (capture file, log file, capture options incl. hookIntoChildProcesses) when
FindRemoteDLL succeeds. If enumeration works on cod.exe, a plain inject finishes
the whole setup and the shell will inject sp26-cod.exe itself when it spawns it.

  python inject_hq_probe.py [--launch]
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
TOOL = os.path.join(ROOT, "x64", "Development", "TinecmaToolcmd.exe")
CAPTURES = os.path.join(ROOT, "artifacts", "steam-cod-captures")
APPID = "1938090"

k = ctypes.WinDLL("kernel32", use_last_error=True)
ps = ctypes.WinDLL("psapi", use_last_error=True)
k.OpenProcess.restype = wt.HANDLE
k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k.CreateToolhelp32Snapshot.restype = wt.HANDLE
k.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
k.CloseHandle.argtypes = [wt.HANDLE]
ps.EnumProcessModulesEx.argtypes = [wt.HANDLE, ctypes.POINTER(wt.HMODULE), wt.DWORD,
                                    ctypes.POINTER(wt.DWORD), wt.DWORD]
ps.GetModuleFileNameExW.argtypes = [wt.HANDLE, wt.HMODULE, wt.LPWSTR, wt.DWORD]


class PE32(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("c", wt.DWORD), ("pid", wt.DWORD),
                ("d", ctypes.POINTER(ctypes.c_ulong)), ("m", wt.DWORD), ("t", wt.DWORD),
                ("pp", wt.DWORD), ("pr", ctypes.c_long), ("f", wt.DWORD), ("n", wt.WCHAR * 260)]


def find(name):
    s = k.CreateToolhelp32Snapshot(2, 0)
    e = PE32()
    e.dwSize = ctypes.sizeof(PE32)
    r = None
    ok = k.Process32FirstW(s, ctypes.byref(e))
    while ok:
        if e.n.lower() == name.lower():
            r = e.pid
            break
        ok = k.Process32NextW(s, ctypes.byref(e))
    k.CloseHandle(s)
    return r


def modules_of(pid):
    h = k.OpenProcess(0x1000 | 0x400 | 0x10, False, pid)
    if not h:
        return None, ctypes.get_last_error(), 0
    need = wt.DWORD(0)
    ok = ps.EnumProcessModulesEx(h, None, 0, ctypes.byref(need), 3)
    err = ctypes.get_last_error()
    names = []
    if ok and need.value:
        buf = (wt.HMODULE * (need.value // 8))()
        ps.EnumProcessModulesEx(h, buf, ctypes.sizeof(buf), ctypes.byref(need), 3)
        b = ctypes.create_unicode_buffer(260)
        for i in range(need.value // 8):
            if ps.GetModuleFileNameExW(h, buf[i], b, 260):
                names.append(os.path.basename(b.value))
    k.CloseHandle(h)
    return (names if ok else None), err, need.value


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--launch", action="store_true")
    ap.add_argument("--watch", type=float, default=120)
    a = ap.parse_args()

    os.makedirs(CAPTURES, exist_ok=True)
    if a.launch and not find("cod.exe"):
        print("[launch] asking Steam to start app %s" % APPID, flush=True)
        subprocess.Popen(["explorer.exe", "steam://rungameid/%s" % APPID])

    print("[race] waiting for cod.exe (5ms poll) ...", flush=True)
    t0 = time.perf_counter()
    pid = None
    while time.perf_counter() - t0 < a.watch:
        pid = find("cod.exe")
        if pid:
            break
        time.sleep(0.005)

    if not pid:
        print("[fail] cod.exe never appeared")
        return 2
    print("[hit ] cod.exe pid=%d at +%.3fs" % (pid, time.perf_counter() - t0), flush=True)

    before, err_before, _ = modules_of(pid)
    print("[pre ] enumerable=%s err=%d count=%s"
          % (before is not None, err_before, len(before) if before else "-"), flush=True)

    t1 = time.perf_counter()
    r = subprocess.run([TOOL, "inject", "--PID=%d" % pid, "--opt-hook-children"],
                       capture_output=True)
    print("[inj ] rc=%d in %.0fms" % (r.returncode, (time.perf_counter() - t1) * 1000), flush=True)
    for s in (r.stdout, r.stderr):
        t = (s or b"").decode("mbcs", "replace").strip()
        if t:
            print("       " + t.replace("\n", "\n       "), flush=True)

    time.sleep(4)
    after, err_after, _ = modules_of(pid)
    print("[post] enumerable=%s err=%d count=%s"
          % (after is not None, err_after, len(after) if after else "-"), flush=True)
    if after:
        hit = [n for n in after if "tinecma" in n.lower()]
        print("[post] TinecmaTool.dll in cod.exe: %s" % (hit or "NO"), flush=True)

    logs = sorted(
        ((os.path.getmtime(os.path.join(d, f)), os.path.join(d, f))
         for d in [os.path.join(os.environ.get("TEMP", ""), "TinecmaTool")]
         if os.path.isdir(d) for f in os.listdir(d) if f.endswith(".log")),
        reverse=True)[:2]
    print("\n[logs] newest:", flush=True)
    for mtime, p in logs:
        print("       %s  (%s)" % (time.strftime("%H:%M:%S", time.localtime(mtime)),
                                   os.path.basename(p)), flush=True)

    print("\n[next] if TinecmaTool.dll is in cod.exe and a log appeared, the shell is")
    print("       fully hooked - start the campaign and sp26-cod.exe gets injected on spawn.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
