#!/usr/bin/env python3
"""Full diagnostic run for the launcher-graphic-IAT gate.

Arms ETW, cold-starts Steam, injects, launches COD, then reads the newest
TinecmaTool log and reports verdicts tuned to the cod.exe skip-gate build:
  1. did the launcher host skip its graphics IAT ("Skipping <driver> hooks
     (graphics IAT disabled in launcher host)" x several drivers, and NO
     "Graphics IAT hook installed: module=cod.exe ... dxgi/d3d12" lines)
  2. did cod.exe survive (no crash) and hand the DLL off to sp26-cod.exe
  3. did sp26-cod.exe register graphics hooks + create a D3D12 device

  python run_diag.py
"""
import csv
import glob
import os
import re
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
STEAM = r"C:\Program Files (x86)\Steam\steam.exe"
INJ = os.path.join(HERE, "inject_steam.py")
ETL = os.path.join(HERE, "rxd-" + time.strftime("%Y%m%d-%H%M") + ".etl")
XML = ETL[:-4] + ".xml"
LOGDIR = os.path.join(os.environ.get("TEMP", ""), "TinecmaTool")

# gate marker: every graphics driver's RegisterHooks prints this when the
# launcher host (cod.exe) is detected. One line per skipped driver.
SKIP_GATE = "graphics IAT disabled in launcher host"
# when the gate is active, cod.exe must NOT install dxgi/d3d12 IAT hooks.
# Any of these proves the gate failed.
BAD_IAT = "Graphics IAT hook installed: module=cod.exe"
# a D3D12 device actually being created (any host) - we want sp26's, not dxdiag's.
DEVICE = "New D3D12 device"
NAMES = {"steam.exe", "steamwebhelper.exe", "cod.exe", "sp26-cod.exe",
         "bootstrapper.exe", "codCrashHandler.exe"}


def running():
    out = subprocess.check_output(["tasklist", "/FO", "CSV", "/NH"],
                                 text=True, errors="replace")
    return [r for r in csv.reader(out.splitlines()) if r and r[0].lower() in NAMES]


def pid_of(line):
    m = re.match(r"(?:RDOC|QTRD) (\d+):", line)
    return m.group(1) if m else None


def newest_log():
    files = [p for p in glob.glob(os.path.join(LOGDIR, "*.log"))]
    if not files:
        return None
    files.sort(key=os.path.getmtime)
    # Skip GUI/replay-tool logs: an injection log always contains "Loading into",
    # while a replayed .rdc in the GUI writes a "loaded in replay application"
    # header instead. A stale GUI log can be newer than the game's injection log.
    for p in reversed(files):
        try:
            with open(p, encoding="utf-8", errors="replace") as fp:
                head = fp.read(65536)
        except OSError:
            continue
        if "Loading into" in head or "capturing application" in head:
            return p
    return files[-1]


def main():
    print("[1/5] arming ETW (keyword 0x50) ...", flush=True)
    subprocess.run(["logman", "stop", "codrxd", "-ets"], capture_output=True)
    r = subprocess.run(["logman", "start", "codrxd", "-p",
                        "Microsoft-Windows-Kernel-Process", "0x50", "5",
                        "-o", ETL, "-ets"], capture_output=True)
    print("      etw rc=%d" % r.returncode, flush=True)

    print("[2/5] cold start Steam ...", flush=True)

    def steam_main():
        return [x[1] for x in running() if x[0].lower() == "steam.exe"]

    def wait_steam_ready(deadline_s):
        # wait until a steam.exe exists AND the web UI helper has come up
        t0 = time.time()
        while time.time() - t0 < deadline_s:
            have_main = bool(steam_main())
            have_ui = any(x[0].lower() == "steamwebhelper.exe" for x in running())
            if have_main and have_ui:
                return True
            time.sleep(1)
        return bool(steam_main())

    subprocess.Popen([STEAM])
    if not wait_steam_ready(150):
        print("      steam.exe did not come up; re-launching once ...", flush=True)
        subprocess.Popen([STEAM])
        wait_steam_ready(120)
    # let the process table / Steam handshake settle so injection targets a stable PID
    time.sleep(30)
    spid = steam_main()
    print("      steam_pid=%s (settled)" % spid, flush=True)

    print("[3/5] injecting ...", flush=True)
    if not steam_main():
        # still not up: last-chance launch and wait again
        subprocess.Popen([STEAM])
        wait_steam_ready(120)
        spid = steam_main()
        print("      re-launched steam_pid=%s" % spid, flush=True)
    rj = subprocess.run([sys.executable, INJ], capture_output=True, text=True)
    injected = False
    for line in (rj.stdout or "").splitlines():
        if any(k in line for k in ("[ok]", "[inj ] rc", "Launched as ID", "Registering", "[ready]", "[warn]")):
            print("      " + line, flush=True)
        if "Launched as ID" in line:
            injected = True
    if not injected:
        print("      [!] injection did not report 'Launched as ID' - aborting early", flush=True)
        print("[done] etl=%s" % ETL, flush=True)
        return 2

    print("[4/5] launching COD, watching for sp26 ...", flush=True)
    subprocess.Popen(["explorer.exe", "steam://rungameid/1938090"])
    firsts = {}
    t1 = time.time()
    sp26_pid = None
    retried = False
    while time.time() - t1 < 320:
        for x in running():
            k = x[0].lower()
            if k not in firsts:
                firsts[k] = round(time.time() - t1, 1)
                if k in ("cod.exe", "sp26-cod.exe", "codCrashHandler.exe"):
                    print("      +%-4ds %-20s pid=%s" % (firsts[k], k, x[1]), flush=True)
            if k == "sp26-cod.exe" and sp26_pid is None:
                sp26_pid = x[1]
        if sp26_pid:
            print("      >>> sp26-cod.exe reached, pid=%s" % sp26_pid, flush=True)
            break
        # If 60s in and cod.exe never appeared, the steam:// was likely swallowed
        # (Steam still settling after repeated crashes). Retry once.
        if (not retried) and ("cod.exe" not in firsts) and (time.time() - t1) > 60:
            retried = True
            print("      (60s, no cod.exe yet) re-firing steam:// ...", flush=True)
            subprocess.Popen(["explorer.exe", "steam://rungameid/1938090"])
        time.sleep(0.5)

    time.sleep(6)
    print("[5/5] stopping ETW + reading verdicts ...", flush=True)
    subprocess.run(["logman", "stop", "codrxd", "-ets"], capture_output=True)
    subprocess.run(["tracerpt", ETL, "-o", XML, "-of", "XML", "-y"],
                   capture_output=True, timeout=600)

    skip_gate, k32_skip, cod_iat, sp26_loaded, device = [], [], [], [], []
    cod_pids, sp26_pids = set(), set()
    lg = newest_log()
    if lg:
        with open(lg, encoding="utf-8", errors="replace") as fp:
            for line in fp:
                pid = pid_of(line)
                if "Loading into" in line and "sp26-cod" in line:
                    sp26_loaded.append(line.rstrip())
                    if pid:
                        sp26_pids.add(pid)
                if "Loading into" in line and re.search(r"cod\.exe$", line, re.I) and \
                        "sp26" not in line.lower():
                    if pid:
                        cod_pids.add(pid)
                if SKIP_GATE in line:
                    skip_gate.append(line.rstrip())
                    if pid:
                        pass
                if "Skipping library-loader IAT in launcher host" in line:
                    k32_skip.append(line.rstrip())
                if "Graphics IAT hook installed" in line and "module=cod.exe" in line:
                    cod_iat.append(line.rstrip())
                if "New D3D12 device" in line and pid in sp26_pids:
                    device.append(line.rstrip())

    print("\n================ VERDICTS ================", flush=True)
    print("newest log: %s" % lg, flush=True)
    print("\n[1] launcher graphics-driver skip (per driver): %d line(s)" % len(skip_gate))
    for h in skip_gate:
        print("      " + h, flush=True)
    print("\n[2] kernel32 library-loader skip in cod.exe: %d line(s)" % len(k32_skip))
    for h in k32_skip[:6]:
        print("      " + h, flush=True)
    print("\n[3] cod.exe IAT installs still present (want 0): %d" % len(cod_iat))
    for h in cod_iat[:8]:
        print("      " + h, flush=True)
    print("\n[4] sp26-cod.exe loaded in log: %d line(s)" % len(sp26_loaded))
    for h in sp26_loaded[:4]:
        print("      " + h, flush=True)
    print("\n[5] New D3D12 device in sp26-cod.exe: %d" % len(device))
    for h in device[:4]:
        print("      " + h, flush=True)
    print("\n[6] sp26-cod.exe live now: %s" % (sp26_pid or "NO"), flush=True)
    print("[done] etl=%s" % ETL, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
