#!/usr/bin/env python3
"""Diagnose whether the inject actually lands in the COD renderer.

TinecmaToolcmd verifies a running-process inject by enumerating the target's
modules (CreateToolhelp32Snapshot / TH32CS_SNAPMODULE). On cod.exe that call is
denied by Ricochet, so a successful inject can still be reported as rc=4
(InjectionFailed). This script bypasses the question: it watches ImageLoad via
ETW (kernel side, no target handle) and reports whether TinecmaTool.dll really
entered the process.

Run elevated. It will start the game through Steam.

  python diag_inject.py [--no-launch] [--wait 180]
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
ROOT = os.path.dirname(os.path.dirname(HERE))
TOOL = os.path.join(ROOT, "x64", "Development", "TinecmaToolcmd.exe")
ETL = os.path.join(HERE, "diag-inject.etl")
XML = os.path.join(HERE, "diag-inject.xml")
APPID = "1938090"
TARGET = "sp26-cod.exe"

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
kernel32.CloseHandle.argtypes = [wt.HANDLE]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)), ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


def find_pid(exe_name):
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or int(snap) == -1:
        return None
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    pid = None
    ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        if entry.szExeFile.lower() == exe_name.lower():
            pid = entry.th32ProcessID
            break
        ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    kernel32.CloseHandle(snap)
    return pid


def etw_start(session, etl):
    for f in (etl,):
        if os.path.exists(f):
            os.remove(f)
    subprocess.run(["logman", "stop", session, "-ets"], capture_output=True)
    r = subprocess.run(["logman", "start", session, "-p", "Microsoft-Windows-Kernel-Process",
                        "0x40", "5", "-o", etl, "-ets"], capture_output=True)
    return r.returncode == 0


def etw_stop(session, etl, xml):
    subprocess.run(["logman", "stop", session, "-ets"], capture_output=True)
    subprocess.run(["tracerpt", etl, "-o", xml, "-of", "XML", "-y"], capture_output=True, timeout=600)
    return os.path.exists(xml)


def scan_images(xml, pids):
    NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
    root = ET.parse(xml).getroot()
    hits = []
    for ev in root.findall("e:Event", NS):
        if ev.findtext("e:System/e:EventID", "", NS) != "5":
            continue
        d = {x.get("Name"): (x.text or "").strip() for x in ev.findall("e:EventData/e:Data", NS)}
        pid = d.get("ProcessID", "")
        if pid not in pids:
            continue
        img = d.get("ImageName", "")
        name = img.rsplit("\\", 1)[-1]
        hits.append((name.lower(), img))
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-launch", action="store_true")
    ap.add_argument("--wait", type=float, default=180)
    args = ap.parse_args()

    session = "codiag"
    print("[1/5] starting ETW session ...", flush=True)
    if not etw_start(session, ETL):
        print("      ETW start failed (needs admin)")
        return 2

    if args.no_launch:
        print("[2/5] not launching, waiting for an existing %s" % TARGET, flush=True)
    else:
        print("[2/5] asking Steam to launch app %s ..." % APPID, flush=True)
        # explorer routes the URI as the desktop user, which matters when we are elevated
        subprocess.Popen(["explorer.exe", "steam://rungameid/%s" % APPID])
        time.sleep(2)
        if find_pid(TARGET) is None:
            os.startfile("steam://rungameid/%s" % APPID)

    print("[3/5] waiting for %s (up to %.0fs) ..." % (TARGET, args.wait), flush=True)
    pid = None
    t0 = time.time()
    while time.time() - t0 < args.wait:
        pid = find_pid(TARGET)
        if pid:
            print("      found pid=%d at +%.1fs" % (pid, time.time() - t0), flush=True)
            break
        time.sleep(0.5)

    if not pid:
        print("      %s never started - aborting" % TARGET)
        etw_stop(session, ETL, XML)
        return 3

    print("[4/5] injecting ...", flush=True)
    r = subprocess.run([TOOL, "inject", "--PID=%d" % pid], capture_output=True)
    print("      rc=%d" % r.returncode)
    for stream in (r.stdout, r.stderr):
        text = (stream or b"").decode("mbcs", "replace").strip()
        if text:
            print("      " + text.replace("\n", "\n      "))

    print("      letting it settle for 10s ...", flush=True)
    time.sleep(10)

    print("[5/5] stopping ETW and scanning ImageLoad ...", flush=True)
    if not etw_stop(session, ETL, XML):
        print("      tracerpt failed")
        return 4

    hits = scan_images(XML, {str(pid)})
    names = [n for n, _ in hits]
    print("\n      pid %s loaded %d modules in this window" % (pid, len(hits)))

    injected = [p for n, p in hits if "tinecmatool" in n or "renderdoc" in n]
    if injected:
        print("\n[RESULT] INJECTED - capture DLL is inside the process:")
        for p in injected:
            print("         " + p)
        print("\n         rc=4 was a false negative: TH32CS_SNAPMODULE is denied,")
        print("         so TinecmaToolcmd could not see its own DLL.")
    else:
        print("\n[RESULT] NOT INJECTED - no TinecmaTool.dll in the ImageLoad stream")
        interesting = [n for n in names if any(k in n for k in
                                              ("d3d12", "dxgi", "vulkan", "nvwgf"))]
        if interesting:
            print("         graphics modules seen: %s" % ", ".join(sorted(set(interesting))))

    print("\n      trace: %s" % XML)
    return 0 if injected else 5


if __name__ == "__main__":
    sys.exit(main())
