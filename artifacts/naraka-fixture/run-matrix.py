"""Run the fixture under TinecmaToolcmd once per resolution strategy and report
what the tool log says about each one.

The question being answered: Naraka's D3D11 device was created without any
"Graphics function resolved" line and without a wrapped device, even though the
hooks were installed. This harness isolates the route by which the device is
created and shows which routes our hooks actually observe.
"""

import glob
import os
import subprocess
import sys
import time

REPO = r"C:\Program Files\GraphicsDebugger-Naraka"
TOOL = os.path.join(REPO, "x64", "Development", "TinecmaToolcmd.exe")
FIXDIR = os.path.join(REPO, "artifacts", "naraka-fixture")
FIXTURE = os.path.join(FIXDIR, "naraka_fixture.exe")
LOGDIR = os.path.join(os.environ["LOCALAPPDATA"], "Temp", "TinecmaTool")
CAPDIR = os.path.join(os.environ["LOCALAPPDATA"], "Temp", "Naraka", "fixture-tests")

KEYS = [
    ("Loading into", "dll injected"),
    ("Registering D3D11 hooks", "d3d11 hooks registered"),
    ("Graphics function resolved:", "GetProcAddress interception"),
    ("Graphics IAT hook installed", "IAT slot patched"),
    ("Graphics call entered", "hooked function actually called"),
    ("created wrapped device", "device wrapped"),
    ("Graphics first present", "present intercept"),
]


def newest_logs():
    return set(glob.glob(os.path.join(LOGDIR, "RenderDoc_*.log")))


def run(mode):
    capdir = os.path.join(CAPDIR, mode)
    os.makedirs(capdir, exist_ok=True)
    before = newest_logs()

    args = [
        TOOL, "capture", "-w", "--opt-disallow-fullscreen",
        "-c", os.path.join(capdir, "fixture"),
        "-d", FIXDIR,
        FIXTURE,
        "-resolve=" + mode,
        "-frames=10",
        "-capture-frame=4",
        "-capture-dir=" + capdir,
    ]

    t0 = time.time()
    p = subprocess.run(args, capture_output=True, timeout=180)
    dur = time.time() - t0

    after = newest_logs()
    new = sorted(after - before, key=os.path.getmtime)
    log = new[-1] if new else None

    fixlog = os.path.join(FIXDIR, "fixture.log")
    fixture_out = open(fixlog, encoding="utf-8", errors="replace").read() if os.path.exists(fixlog) else ""

    text = ""
    if log and os.path.exists(log):
        text = open(log, encoding="utf-8", errors="replace").read()

    print("=" * 78)
    print("mode = %s   exit=%d   %.1fs" % (mode, p.returncode, dur))
    print("log  = %s" % (log or "<none>"))

    if p.stderr:
        err = p.stderr.decode("utf-8", "replace").strip()
        if err:
            print("tool stderr: %s" % err.splitlines()[0][:160])

    print("-- fixture --")
    for line in fixture_out.strip().splitlines():
        print("   " + line)

    print("-- tool log --")
    for needle, label in KEYS:
        hit = text.count(needle)
        mark = "YES" if hit else "no "
        print("   [%s] %-32s %s" % (mark, label, ("x%d" % hit) if hit else ""))

    rdcs = glob.glob(os.path.join(capdir, "*.rdc"))
    print("-- rdc --")
    for r in rdcs:
        print("   %s (%.2f MB)" % (os.path.basename(r), os.path.getsize(r) / 1048576.0))
    if not rdcs:
        print("   <none>")

    return log, text, rdcs


def main():
    modes = sys.argv[1:] or ["import", "dynamic", "manual"]
    for m in modes:
        run(m)
    print("=" * 78)


if __name__ == "__main__":
    main()
