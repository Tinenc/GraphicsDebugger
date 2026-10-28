#!/usr/bin/env python3
"""Inject sp26-cod.exe directly, bypassing cod.exe entirely.

Rationale (proven across 2026-10-26/27 recon): TinecmaTool.dll physically
residing inside cod.exe is what trips the game's module-scan watchdog, even
when the DLL is 100% inert (zero IAT, zero hooks, zero init). So the ONLY
viable path is:
  1. cold-start Steam UNINJECTED and let the game boot naturally ->
     cod.exe survives -> sp26-cod.exe is created (baseline-proven)
  2. while sp26-cod.exe is alive, inject TinecmaTool.dll DIRECTLY into it via
     `TinecmaToolcmd inject --PID=<sp26>` (sp26's basename != "cod.exe", so the
     inert branch does NOT hit and it gets the full D3D12 hook set)
  3. check whether sp26 accepts the injection (VirtualAllocEx not err=5),
     survives, and logs "Loading into ...sp26-cod.exe" + "Registering D3D12
     hooks" + "New D3D12 device".

Open question this answers: is sp26-cod.exe a protected process (like cod.exe,
OpenProcess/VirtualAllocEx err=5, 1/44 success) or injectable?

  python sp26_direct.py
"""
import csv
import glob
import os
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
STEAM = r"C:\Program Files (x86)\Steam\steam.exe"
TOOL = os.path.join(ROOT, "x64", "Development", "TinecmaToolcmd.exe")
CAPTURES = os.path.join(ROOT, "artifacts", "steam-cod-captures")
LOGDIR = os.path.join(os.environ.get("TEMP", ""), "TinecmaTool")
ETL = os.path.join(HERE, "sp26-" + time.strftime("%Y%m%d-%H%M") + ".etl")
XML = ETL[:-4] + ".xml"

NAMES = {"steam.exe", "steamwebhelper.exe", "cod.exe", "sp26-cod.exe",
         "bootstrapper.exe", "codCrashHandler.exe", "codbrokerservice.exe"}


def running():
    out = subprocess.check_output(["tasklist", "/FO", "CSV", "/NH"],
                                 text=True, errors="replace")
    return [r for r in csv.reader(out.splitlines()) if r and r[0].lower() in NAMES]


def sp26_pid():
    for x in running():
        if x[0].lower() == "sp26-cod.exe":
            return x[1]
    return None


def cod_pids():
    return [x[1] for x in running() if x[0].lower() == "cod.exe"]


def arm_etw():
    subprocess.run(["logman", "stop", "sp26etw", "-ets"], capture_output=True)
    r = subprocess.run(["logman", "start", "sp26etw", "-p",
                        "Microsoft-Windows-Kernel-Process", "0x50", "5",
                        "-o", ETL, "-ets"], capture_output=True)
    print("[etw] rc=%d" % r.returncode, flush=True)


def stop_etw():
    subprocess.run(["logman", "stop", "sp26etw", "-ets"], capture_output=True)
    subprocess.run(["tracerpt", ETL, "-o", XML, "-of", "XML", "-y"],
                   capture_output=True, timeout=600)


def main():
    print("[1/6] ensuring clean shutdown of any running Steam/COD ...", flush=True)
    subprocess.run([STEAM, "-shutdown"], capture_output=True)
    for _ in range(45):
        if not running():
            break
        time.sleep(1)
    print("      remaining=%s" % [(r[0], r[1]) for r in running()], flush=True)

    arm_etw()

    print("[2/6] cold start Steam (UNINJECTED) ...", flush=True)
    subprocess.Popen([STEAM])
    t0 = time.time()
    ui = False
    while time.time() - t0 < 120:
        if any(x[0].lower() == "steamwebhelper.exe" for x in running()):
            ui = True
            break
        time.sleep(1)
    time.sleep(20)
    print("      ui=%s steam=%s" % (ui, [x[1] for x in running() if x[0].lower() == "steam.exe"]), flush=True)

    print("[3/6] launching game, waiting for sp26-cod.exe to appear naturally ...", flush=True)
    subprocess.Popen(["explorer.exe", "steam://rungameid/1938090"])
    t1 = time.time()
    seen = {}
    sp26 = None
    while time.time() - t1 < 150:
        for x in running():
            k = x[0].lower()
            if k in ("cod.exe", "sp26-cod.exe") and k not in seen:
                seen[k] = round(time.time() - t1, 1)
                print("      +%-4ds %-18s pid=%s" % (seen[k], k, x[1]), flush=True)
        sp26 = sp26_pid()
        if sp26:
            print("      >>> sp26-cod.exe reached, pid=%s" % sp26, flush=True)
            break
        time.sleep(0.5)
    if not sp26:
        print("[!] sp26-cod.exe never appeared in 150s - baseline may have changed; aborting", flush=True)
        stop_etw()
        return 1

    # give sp26 a few seconds to settle before we poke it
    time.sleep(5)

    print("[4/6] injecting TinecmaTool.dll directly into sp26-cod.exe pid=%s ..." % sp26, flush=True)
    capfile = os.path.join(CAPTURES, "steam-cod")
    r = subprocess.run([TOOL, "inject", "--PID=%s" % sp26, "-c", capfile],
                       capture_output=True, text=True, timeout=120)
    print("      inject rc=%d" % r.returncode, flush=True)
    for line in (r.stdout or "").splitlines():
        print("      " + line, flush=True)
    for line in (r.stderr or "").splitlines():
        print("      [err] " + line, flush=True)

    print("[5/6] watching sp26 survival for 40s ...", flush=True)
    alive_after = sp26_pid()
    for _ in range(40):
        time.sleep(1)
        if alive_after != sp26_pid() or sp26_pid() is None:
            print("      sp26 pid=%s state=%s" % (alive_after, sp26_pid()), flush=True)
            break
        alive_after = sp26_pid()
    sp26_alive_now = sp26_pid()

    print("[6/6] stopping ETW + reading verdicts ...", flush=True)
    stop_etw()

    lg = None
    files = [p for p in glob.glob(os.path.join(LOGDIR, "*.log"))]
    if files:
        files.sort(key=os.path.getmtime)
        for p in reversed(files):
            try:
                with open(p, encoding="utf-8", errors="replace") as fp:
                    head = fp.read(65536)
            except OSError:
                continue
            if "Loading into" in head or "capturing application" in head:
                lg = p
                break
        if lg is None:
            lg = files[-1]

    sp26_loaded, sp26_hooks, sp26_device = [], [], []
    if lg:
        with open(lg, encoding="utf-8", errors="replace") as fp:
            for line in fp:
                if "Loading into" in line and "sp26-cod" in line:
                    sp26_loaded.append(line.rstrip())
                if "Registering D3D12 hooks" in line:
                    sp26_hooks.append(line.rstrip())
                if "New D3D12 device" in line:
                    sp26_device.append(line.rstrip())

    print("\n================ SP26 DIRECT-INJECT VERDICTS ================", flush=True)
    print("newest log: %s" % lg, flush=True)
    print("\n[1] sp26-cod.exe 'Loading into' lines: %d" % len(sp26_loaded))
    for h in sp26_loaded[:4]:
        print("      " + h, flush=True)
    print("\n[2] 'Registering D3D12 hooks' lines: %d" % len(sp26_hooks))
    for h in sp26_hooks[:6]:
        print("      " + h, flush=True)
    print("\n[3] 'New D3D12 device' lines: %d" % len(sp26_device))
    for h in sp26_device[:6]:
        print("      " + h, flush=True)
    print("\n[4] sp26-cod.exe alive now (after 40s): %s" % sp26_alive_now, flush=True)
    print("\nINTERPRETATION", flush=True)
    print("  sp26 inject accepted + alive + hooks registered  -> DIRECT PATH WORKS; fix workflow", flush=True)
    print("  sp26 died with 0xC000005                          -> watchdog kills sp26 too; memory-inject dead", flush=True)
    print("  sp26 OpenProcess/VirtualAllocEx err=5              -> sp26 is protected; need kernel/other channel", flush=True)
    print("[done] etl=%s" % ETL, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
