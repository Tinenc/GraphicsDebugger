#!/usr/bin/env python3
"""Read-only module timeline sampler for cod.exe (S4a).

Deliberately touches nothing inside the target:
  - no DLL injection, no hooking, no memory reads
  - only OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) + EnumProcessModulesEx +
    GetModuleFileNameExW, i.e. the same class of access Task Manager uses

What it answers: does cod.exe load dxgi.dll at all, in what order relative to
d3d12.dll / D3D12Core.dll, and where does the capture DLL show up. That decides
which factory route the game takes (see docs/STEAM_COD_S2_FACTORY_BRIDGE.md §3).

Usage:
  python module_timeline.py --process cod.exe --interval 0.02 --timeout 900
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

# Explicit signatures: HMODULE/HANDLE are pointer-sized. Without these ctypes marshals them as
# 32-bit ints and a 64-bit module handle raises OverflowError.
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.GetExitCodeProcess.restype = wt.BOOL
kernel32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]

psapi.EnumProcessModulesEx.restype = wt.BOOL
psapi.EnumProcessModulesEx.argtypes = [wt.HANDLE, ctypes.POINTER(wt.HMODULE), wt.DWORD,
                                       ctypes.POINTER(wt.DWORD), wt.DWORD]
psapi.GetModuleFileNameExW.restype = wt.DWORD
psapi.GetModuleFileNameExW.argtypes = [wt.HANDLE, wt.HMODULE, wt.LPWSTR, wt.DWORD]
psapi.GetModuleInformation.restype = wt.BOOL
psapi.GetModuleInformation.argtypes = [wt.HANDLE, wt.HMODULE, ctypes.c_void_p, wt.DWORD]

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
LIST_MODULES_ALL = 0x03
MAX_PATH = 260

# Modules worth calling out live; everything else still lands in the CSV.
WATCH = (
    "d3d12.dll", "d3d11.dll", "dxgi.dll", "d3d12core.dll", "d3d12sdklayers.dll",
    "thx_sdk.dll", "nvwgf2umx.dll", "amdvlk", "vulkan-1.dll", "gfsdk_aftermath",
    "gameinput", "codbroker", "randgrid", "azureattest", "tinecmatool", "renderdoc",
    "win32u.dll", "dwmapi.dll", "opengl32.dll",
)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * MAX_PATH),
    ]


def find_pids(exe_name):
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or int(snap) == -1:
        return []
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    pids = []
    ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        if entry.szExeFile.lower() == exe_name.lower():
            pids.append((entry.th32ProcessID, entry.th32ParentProcessID))
        ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    kernel32.CloseHandle(snap)
    return pids


def open_process(pid):
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    return handle


def list_modules(handle):
    needed = wt.DWORD(0)
    psapi.EnumProcessModulesEx(handle, None, 0, ctypes.byref(needed), LIST_MODULES_ALL)
    if not needed.value:
        return []
    count = needed.value // ctypes.sizeof(wt.HMODULE)
    buf = (wt.HMODULE * count)()
    if not psapi.EnumProcessModulesEx(handle, buf, ctypes.sizeof(buf), ctypes.byref(needed),
                                      LIST_MODULES_ALL):
        return []
    total = needed.value // ctypes.sizeof(wt.HMODULE)
    out = []
    path_buf = ctypes.create_unicode_buffer(MAX_PATH)
    for i in range(min(count, total + 8)):
        hmod = buf[i]
        if not hmod:
            continue
        length = psapi.GetModuleFileNameExW(handle, hmod, path_buf, MAX_PATH)
        if not length:
            continue
        out.append((hmod, path_buf.value))
    return out


def watch_hit(path):
    low = path.lower()
    return any(w in low for w in WATCH)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--process", default="cod.exe")
    ap.add_argument("--interval", type=float, default=0.02)
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--out", default="module_timeline.csv")
    args = ap.parse_args()

    print("[wait] watching for %s (interval %.0f ms, timeout %.0f s)"
          % (args.process, args.interval * 1000, args.timeout), flush=True)

    started = time.time()
    rows = []
    seen = {}
    handle = None
    pid = None
    attach_failed = False
    announced_exit = False

    while time.time() - started < args.timeout:
        if pid is None:
            found = find_pids(args.process)
            if not found:
                time.sleep(0.05)
                continue
            pid, ppid = found[0]
            handle = open_process(pid)
            if handle:
                print("[found] %s pid=%d ppid=%d at +%.3fs"
                      % (args.process, pid, ppid, time.time() - started), flush=True)
            else:
                err = ctypes.get_last_error()
                print("[warn ] %s pid=%d exists but OpenProcess failed (err=%d). "
                      "Protected process? Falling back to polling only."
                      % (args.process, pid, err), flush=True)
                attach_failed = True

        if attach_failed:
            if not find_pids(args.process) and not announced_exit:
                print("[exit ] process gone at +%.3fs" % (time.time() - started), flush=True)
                announced_exit = True
                break
            time.sleep(0.25)
            continue

        mods = list_modules(handle)
        now = time.time() - started
        for base, path in mods:
            key = path.lower()
            if key in seen:
                continue
            seen[key] = now
            name = os.path.basename(path)
            rows.append((now, name, base, path))
            if watch_hit(path):
                print("[mod  ] +%07.3fs  %-28s base=0x%016X  %s"
                      % (now, name, int(base), path), flush=True)

        # process gone?
        code = wt.DWORD(0)
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259:
            print("[exit ] %s exited code=0x%08X at +%.3fs (%d modules seen)"
                  % (args.process, code.value, now, len(rows)), flush=True)
            break

        time.sleep(args.interval)

    with open(args.out, "w", encoding="utf-8", newline="") as fp:
        fp.write("t_seconds,module,base,path\n")
        for t, name, base, path in rows:
            fp.write("%.4f,%s,0x%016X,%s\n" % (t, name, int(base), path))

    print("[done ] %d modules recorded -> %s" % (len(rows), os.path.abspath(args.out)), flush=True)

    # Timeline of the graphics-relevant subset only.
    interesting = [r for r in rows if watch_hit(r[3])]
    if interesting:
        print("[order] graphics-relevant load order:", flush=True)
        for t, name, base, path in interesting:
            print("        +%07.3fs %s" % (t, name), flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
