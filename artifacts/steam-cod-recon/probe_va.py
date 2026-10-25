#!/usr/bin/env python3
"""Probe cod.exe for a window where VirtualAllocEx is permitted.

Nothing is injected: allocate 1 byte, free it, close. Just records whether the
allocation is allowed at each sample, so we can see whether Ricochet's protection
is permanent or has a time window worth racing for.

  python probe_va.py [--launch] [--for 150] [--every 2]
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
APPID = "1938090"
PROC_QUERY = 0x0400
PROC_VM_OPERATION = 0x0008
PROC_VM_READ = 0x0010

k = ctypes.WinDLL("kernel32", use_last_error=True)
k.OpenProcess.restype = wt.HANDLE
k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k.CloseHandle.argtypes = [wt.HANDLE]
k.VirtualAllocEx.restype = ctypes.c_void_p
k.VirtualAllocEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD, wt.DWORD]
k.VirtualFreeEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_size_t, wt.DWORD]
k.CreateToolhelp32Snapshot.restype = wt.HANDLE
k.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]


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


def probe(pid):
    """Returns (ok, stage, err). stage says which call failed first."""
    ctypes.set_last_error(0)
    h = k.OpenProcess(PROC_QUERY | PROC_VM_OPERATION | PROC_VM_READ, False, pid)
    if not h:
        return False, "OpenProcess", ctypes.get_last_error()
    try:
        ctypes.set_last_error(0)
        addr = k.VirtualAllocEx(h, None, 1, 0x3000, 0x04)  # COMMIT|RESERVE, PAGE_READWRITE
        if not addr:
            return False, "VirtualAllocEx", ctypes.get_last_error()
        k.VirtualFreeEx(h, addr, 0, 0x8000)
        return True, "-", 0
    finally:
        k.CloseHandle(h)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--launch", action="store_true")
    ap.add_argument("--for", dest="dur", type=float, default=150)
    ap.add_argument("--every", type=float, default=2.0)
    ap.add_argument("--target", default="cod.exe")
    a = ap.parse_args()

    if a.launch and not find(a.target):
        print("[launch] Steam -> app %s" % APPID, flush=True)
        subprocess.Popen(["explorer.exe", "steam://rungameid/%s" % APPID])

    print("[wait] %s ..." % a.target, flush=True)
    t0 = time.perf_counter()
    pid = None
    while time.perf_counter() - t0 < a.dur:
        pid = find(a.target)
        if pid:
            break
        time.sleep(0.2)
    if not pid:
        print("[fail] never appeared")
        return 2

    t_seen = time.perf_counter() - t0
    print("[hit ] %s pid=%d at +%.1fs" % (a.target, pid, t_seen), flush=True)
    print("[probe] every %.1fs for %.0fs, 1 byte each\n" % (a.every, a.dur), flush=True)

    ok_count = fail_count = 0
    first_ok = None
    rows = []
    t_end = time.perf_counter() + a.dur
    while time.perf_counter() < t_end:
        if not find(a.target):
            print("[gone] process exited at +%.1fs" % (time.perf_counter() - t0), flush=True)
            break
        ok, stage, err = probe(pid)
        el = time.perf_counter() - t0
        rows.append((el, ok, stage, err))
        if ok:
            ok_count += 1
            if first_ok is None:
                first_ok = el
            print("  +%6.1fs  ALLOW                     (<= window here)" % el, flush=True)
        else:
            fail_count += 1
            print("  +%6.1fs  denied at %-16s err=%d" % (el, stage, err), flush=True)
        time.sleep(a.every)

    print("\n[summary] allowed %d / denied %d" % (ok_count, fail_count), flush=True)
    if first_ok is not None:
        print("          first allow at +%.1fs" % first_ok)
        print("          => a usable window exists; an injector is worth writing")
    else:
        print("          no allow at any sample => VirtualAllocEx is permanently denied")
        print("          => writing an injector would be wasted effort on this target")
    return 0


if __name__ == "__main__":
    sys.exit(main())
