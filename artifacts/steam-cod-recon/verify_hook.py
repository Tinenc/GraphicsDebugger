#!/usr/bin/env python3
"""Watch for the renderer and report whether the global-hook shim landed in it.

Passive: only an ETW ImageLoad session. No injection, no target handle.

  python verify_hook.py [--wait 300] [--settle 45]
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ETL = os.path.join(HERE, "hookverify.etl")
XML = os.path.join(HERE, "hookverify.xml")
TARGET = "sp26-cod.exe"

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.CloseHandle.argtypes = [wt.HANDLE]


class PE32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)), ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD), ("szExeFile", wt.WCHAR * 260),
    ]


def find_pid(name):
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or int(snap) == -1:
        return None
    e = PE32()
    e.dwSize = ctypes.sizeof(PE32)
    pid = None
    ok = kernel32.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        if e.szExeFile.lower() == name.lower():
            pid = e.th32ProcessID
            break
        ok = kernel32.Process32NextW(snap, ctypes.byref(e))
    kernel32.CloseHandle(snap)
    return pid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=float, default=300)
    ap.add_argument("--settle", type=float, default=45)
    a = ap.parse_args()

    for f in (ETL, XML):
        if os.path.exists(f):
            os.remove(f)
    subprocess.run(["logman", "stop", "codhook", "-ets"], capture_output=True)
    r = subprocess.run(["logman", "start", "codhook", "-p", "Microsoft-Windows-Kernel-Process",
                        "0x40", "5", "-o", ETL, "-ets"], capture_output=True)
    if r.returncode != 0:
        print("[fail] ETW start failed (admin required)")
        return 2
    print("[ok] ETW running. Launch the game from Steam now.", flush=True)

    pid = None
    t0 = time.time()
    while time.time() - t0 < a.wait:
        pid = find_pid(TARGET)
        if pid:
            break
        time.sleep(0.5)

    if not pid:
        print("[fail] %s never started" % TARGET)
        subprocess.run(["logman", "stop", "codhook", "-ets"], capture_output=True)
        return 3

    print("[ok] %s pid=%d at +%.0fs, settling %.0fs for module loads ..."
          % (TARGET, pid, time.time() - t0, a.settle), flush=True)
    time.sleep(a.settle)

    subprocess.run(["logman", "stop", "codhook", "-ets"], capture_output=True)
    print("[..] parsing trace ...", flush=True)
    subprocess.run(["tracerpt", ETL, "-o", XML, "-of", "XML", "-y"], capture_output=True, timeout=600)

    NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
    root = ET.parse(XML).getroot()
    mods = []
    for ev in root.findall("e:Event", NS):
        if ev.findtext("e:System/e:EventID", "", NS) != "5":
            continue
        d = {x.get("Name"): (x.text or "").strip() for x in ev.findall("e:EventData/e:Data", NS)}
        if d.get("ProcessID") == str(pid):
            mods.append(d.get("ImageName", ""))

    names = [m.rsplit("\\", 1)[-1] for m in mods]
    print("      %d modules recorded for pid %d" % (len(names), pid))

    hits = [m for m in mods if "tinecmatool" in m.lower() or "renderdoc" in m.lower()]
    if hits:
        print("\n[RESULT] GLOBAL HOOK WORKED - capture DLL is inside the renderer:")
        for h in hits:
            print("         " + h)
        print("\n         next: press the capture hotkey in game, then check")
        print("         artifacts\\steam-cod-captures\\ for a .rdc")
    else:
        print("\n[RESULT] shim did not reach the renderer")
        gra = sorted({n for n in names if any(k in n.lower() for k in ("d3d12", "dxgi", "nvwgf"))})
        print("         graphics modules seen: %s" % (", ".join(gra) or "<none>"))
        print("         shim log: %%TEMP%%\\TinecmaTool\\ ... and globalhook-runtime.log")
    return 0 if hits else 5


if __name__ == "__main__":
    sys.exit(main())
