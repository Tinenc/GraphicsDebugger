#!/usr/bin/env python3
"""One-shot: bring Steam up, wait for its UI, inject it with hookIntoChildProcesses,
then arm ETW so the child injection can be verified.

Steam is the only process in this chain without anti-cheat, and it is fully
enumerable, so the inject completes setup properly (rc=0 + "Loading into
steam.exe" + "Registering D3D12 hooks"). Everything Steam spawns afterwards is
injected at creation time, before any anti-cheat guard exists in that process.

  python setup_hook_chain.py            start Steam if needed, inject, arm ETW
  python setup_hook_chain.py --no-etw   skip the ETW session
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
ETL = os.path.join(HERE, "children.etl")
STEAM = r"C:\Program Files (x86)\Steam\steam.exe"
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


def wait_for(name, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pids_of(name):
            return time.time() - t0
        time.sleep(0.25)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-etw", action="store_true")
    a = ap.parse_args()

    if not pids_of("steam.exe"):
        print("[1/5] Steam not running - starting it ...", flush=True)
        if not os.path.exists(STEAM):
            print("      [fail] %s not found" % STEAM)
            return 2
        subprocess.Popen([STEAM])
        dt = wait_for("steam.exe", 90)
        if dt is None:
            print("      [fail] steam.exe never appeared")
            return 2
        print("      steam.exe up after %.1fs" % dt)
    else:
        print("[1/5] Steam already running")

    print("[2/5] waiting for steamwebhelper (UI ready) ...", flush=True)
    dt = wait_for("steamwebhelper.exe", 90)
    if dt is None:
        print("      [warn] steamwebhelper not seen; continuing anyway")
    else:
        print("      UI helper up after %.1fs, letting it settle 4s" % dt)
    time.sleep(4)

    pid = pids_of("steam.exe")[0]
    present = has_module(pid, "TinecmaTool.dll")
    print("[3/5] steam.exe pid=%d, dll present=%s" % (pid, present), flush=True)

    if present is True:
        print("      already injected - skipping")
    else:
        r = subprocess.run([TOOL, "inject", "--PID=%d" % pid, "--opt-hook-children",
                            "-c", os.path.join(CAPTURES, "steam-cod")], capture_output=True)
        print("      inject rc=%d" % r.returncode, flush=True)
        txt = ((r.stdout or b"") + (r.stderr or b"")).decode("mbcs", "replace").strip()
        if txt:
            print("      " + txt.replace("\n", "\n      "))
        if r.returncode != 0:
            print("      [fail] inject did not report success")
            return 3

    print("[4/5] verifying initialisation in the log ...", flush=True)
    try:
        logs = [os.path.join(LOGDIR, f) for f in os.listdir(LOGDIR) if f.endswith(".log")]
        logs.sort(key=os.path.getmtime, reverse=True)
        if logs:
            body = open(logs[0], encoding="utf-8", errors="replace").read()
            ok = all(s in body for s in ("Loading into", "Registering D3D12 hooks"))
            print("      newest log %s -> hooks registered: %s"
                  % (os.path.basename(logs[0]), ok), flush=True)
            if not ok:
                print("      [warn] log exists but hooks are not registered - check it")
        else:
            print("      [warn] no log found")
    except OSError as e:
        print("      [warn] log check failed:", e)

    if not a.no_etw:
        print("[5/5] arming ETW ...", flush=True)
        subprocess.run(["logman", "stop", "codchild", "-ets"], capture_output=True)
        if os.path.exists(ETL):
            os.remove(ETL)
        r = subprocess.run(["logman", "start", "codchild", "-p", "Microsoft-Windows-Kernel-Process",
                            "0x40", "5", "-o", ETL, "-ets"], capture_output=True)
        print("      ETW %s" % ("armed" if r.returncode == 0 else "FAILED (admin?)"), flush=True)
    else:
        print("[5/5] ETW skipped")

    print("\n[ready] 现在从 Steam 启动游戏。")
    print("        cod.exe 会在创建瞬间被注入，之后它 spawn 的 sp26-cod.exe 同理。")
    print("        开完告诉我，我停 ETW 解析结果。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
