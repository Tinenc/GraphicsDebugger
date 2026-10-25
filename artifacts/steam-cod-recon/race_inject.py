#!/usr/bin/env python3
"""Race the anti-cheat: inject the moment a COD process appears.

Rationale: Ricochet's protection is established during process init. Sampling at
~5ms and injecting immediately gives the best chance of landing before the guard
is in place. Two targets are raced:

  cod.exe       HQ shell. Injecting here with --opt-hook-children means the
                already-hooked shell injects sp26-cod.exe when it creates it
                (its CreateProcess runs inside our hooked process).
  sp26-cod.exe  the renderer itself, injected as early as we can see it.

Every attempt is timed and its rc recorded, then ETW is used to check whether the
DLL really landed (module enumeration is useless on a protected target).

  python race_inject.py [--launch] [--watch 180]
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
CAPTURES = os.path.join(ROOT, "artifacts", "steam-cod-captures")
ETL = os.path.join(HERE, "race.etl")
XML = os.path.join(HERE, "race.xml")
APPID = "1938090"
TARGETS = ["cod.exe", "sp26-cod.exe"]

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


def snapshot_pids():
    """One pass over the process list; returns {name: pid} for our targets."""
    out = {}
    snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or int(snap) == -1:
        return out
    e = PE32()
    e.dwSize = ctypes.sizeof(PE32)
    ok = kernel32.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        n = e.szExeFile.lower()
        if n in TARGETS and n not in out:
            out[n] = e.th32ProcessID
        ok = kernel32.Process32NextW(snap, ctypes.byref(e))
    kernel32.CloseHandle(snap)
    return out


def inject(pid, name):
    t0 = time.perf_counter()
    r = subprocess.run([TOOL, "inject", "--PID=%d" % pid, "--opt-hook-children"],
                       capture_output=True)
    dt = (time.perf_counter() - t0) * 1000.0
    msg = (r.stdout or b"") + (r.stderr or b"")
    return r.returncode, dt, msg.decode("mbcs", "replace").strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--launch", action="store_true", help="ask Steam to start the game")
    ap.add_argument("--watch", type=float, default=180)
    a = ap.parse_args()

    os.makedirs(CAPTURES, exist_ok=True)

    subprocess.run(["logman", "stop", "coderace", "-ets"], capture_output=True)
    for f in (ETL, XML):
        if os.path.exists(f):
            os.remove(f)
    r = subprocess.run(["logman", "start", "coderace", "-p", "Microsoft-Windows-Kernel-Process",
                        "0x40", "5", "-o", ETL, "-ets"], capture_output=True)
    print("[etw ] %s" % ("on" if r.returncode == 0 else "FAILED (need admin)"), flush=True)

    if a.launch:
        print("[launch] asking Steam to start app %s" % APPID, flush=True)
        subprocess.Popen(["explorer.exe", "steam://rungameid/%s" % APPID])

    print("[race] polling every 5ms for %s ..." % ", ".join(TARGETS), flush=True)

    done = set()
    results = []
    t_start = time.perf_counter()
    polls = 0
    while time.perf_counter() - t_start < a.watch and len(done) < len(TARGETS):
        pids = snapshot_pids()
        polls += 1
        for name, pid in pids.items():
            if name in done:
                continue
            appeared = (time.perf_counter() - t_start)
            rc, ms, msg = inject(pid, name)
            done.add(name)
            results.append((name, pid, appeared, rc, ms, msg))
            print("\n[hit ] %-13s pid=%-7d seen at +%.3fs" % (name, pid, appeared), flush=True)
            print("[inj ] took %.0fms  rc=%d" % (ms, rc), flush=True)
            if msg:
                print("       " + msg.replace("\n", "\n       "), flush=True)
        time.sleep(0.005)

    print("\n[poll] %d snapshots taken" % polls, flush=True)

    print("[ver ] letting the game settle 20s ...", flush=True)
    time.sleep(20)
    subprocess.run(["logman", "stop", "coderace", "-ets"], capture_output=True)
    subprocess.run(["tracerpt", ETL, "-o", XML, "-of", "XML", "-y"], capture_output=True, timeout=600)

    NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
    root = ET.parse(XML).getroot()
    per_pid = {}
    for ev in root.findall("e:Event", NS):
        if ev.findtext("e:System/e:EventID", "", NS) != "5":
            continue
        d = {x.get("Name"): (x.text or "").strip() for x in ev.findall("e:EventData/e:Data", NS)}
        pid = d.get("ProcessID", "")
        img = d.get("ImageName", "")
        per_pid.setdefault(pid, []).append(img)

    print("\n[result]")
    for name, pid, appeared, rc, ms, msg in results:
        mods = per_pid.get(str(pid), [])
        hit = [m for m in mods if "inecma" in m.lower()]
        print("  %-13s pid=%-7d rc=%-4d %d modules  injected=%s"
              % (name, pid, rc, len(mods), "YES" if hit else "NO"))
        for h in hit:
            print("       " + h)

    # children of cod.exe that we never raced are still worth reporting
    others = [(p, len(m)) for p, m in per_pid.items()
              if p not in {str(x[1]) for x in results} and
              any("inecma" in i.lower() for i in m)]
    if others:
        print("  other processes that got the DLL: %s" % others)

    return 0


if __name__ == "__main__":
    sys.exit(main())
