#!/usr/bin/env python3
"""Inject steam.exe with hookIntoChildProcesses, so the game is injected on spawn.

steam.exe has no anti-cheat and is fully enumerable, which means TinecmaToolcmd
gets a real module base and completes the whole setup (verified: rc=0,
"Loading into steam.exe", "Registering D3D12 hooks", INTERNAL_SetCaptureOptions).
Every process Steam creates after that is injected before it runs any code.

Idempotent: if the DLL is already in steam.exe it does not inject a second time.

  python inject_steam.py [--force]
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
LOGDIR = os.path.join(os.environ.get("TEMP", ""), "TinecmaTool")

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


def pids_of(name):
    s = k.CreateToolhelp32Snapshot(2, 0)
    e = PE32()
    e.dwSize = ctypes.sizeof(PE32)
    out = []
    ok = k.Process32FirstW(s, ctypes.byref(e))
    while ok:
        if e.n.lower() == name.lower():
            out.append(e.pid)
        ok = k.Process32NextW(s, ctypes.byref(e))
    k.CloseHandle(s)
    return out


def has_module(pid, needle):
    h = k.OpenProcess(0x1000 | 0x400 | 0x10, False, pid)
    if not h:
        return None
    need = wt.DWORD(0)
    ok = ps.EnumProcessModulesEx(h, None, 0, ctypes.byref(need), 3)
    found = False
    if ok and need.value:
        buf = (wt.HMODULE * (need.value // 8))()
        ps.EnumProcessModulesEx(h, buf, ctypes.sizeof(buf), ctypes.byref(need), 3)
        b = ctypes.create_unicode_buffer(260)
        for i in range(need.value // 8):
            if ps.GetModuleFileNameExW(h, buf[i], b, 260) and needle.lower() in b.value.lower():
                found = True
                break
    k.CloseHandle(h)
    return found


def newest_log():
    try:
        files = [os.path.join(LOGDIR, f) for f in os.listdir(LOGDIR) if f.endswith(".log")]
        files.sort(key=os.path.getmtime, reverse=True)
        return files[0] if files else None
    except OSError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    pids = pids_of("steam.exe")
    if not pids:
        print("[fail] steam.exe is not running - start Steam first")
        return 2
    pid = pids[0]
    print("[ok] steam.exe pid=%d (%d instance%s)" % (pid, len(pids), "" if len(pids) == 1 else "s"))

    present = has_module(pid, "TinecmaTool.dll")
    if present is None:
        print("[warn] cannot enumerate steam.exe modules - proceeding anyway")
    elif present and not a.force:
        print("[ok] TinecmaTool.dll is already inside steam.exe - skipping inject")
        print("     (pass --force to inject again)")
        print("\n[ready] start the game from Steam now; every child gets injected on spawn.")
        return 0

    print("[inj ] injecting with --opt-hook-children ...")
    t0 = time.perf_counter()
    r = subprocess.run([TOOL, "inject", "--PID=%d" % pid, "--opt-hook-children",
                        "-c", os.path.join(CAPTURES, "steam-cod")], capture_output=True)
    ms = (time.perf_counter() - t0) * 1000
    print("[inj ] rc=%d in %.0fms" % (r.returncode, ms))
    for s in (r.stdout, r.stderr):
        t = (s or b"").decode("mbcs", "replace").strip()
        if t:
            print("       " + t.replace("\n", "\n       "))

    log = newest_log()
    if log:
        with open(log, encoding="utf-8", errors="replace") as fp:
            body = fp.read()
        print("\n[log ] %s" % os.path.basename(log))
        for key in ("Loading into", "Registering Win32 system hooks", "Registering D3D12 hooks",
                    "INTERNAL_SetCaptureOptions", "hookIntoChildren"):
            if key in body:
                print("       + %s" % key)
        if "Loading into" not in body:
            print("       ! no 'Loading into' - initialisation did not complete")

    print("\n[ready] start the game from Steam now.")
    print("        cod.exe and sp26-cod.exe will be injected the moment they are created.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
