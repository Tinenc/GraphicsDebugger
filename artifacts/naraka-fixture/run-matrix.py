"""Run both fixtures under TinecmaToolcmd and report what the tool log saw.

Question: Naraka created its device without any "Graphics function resolved"
line and without a wrapped device, despite the hooks being installed. This
harness isolates the route by which the device entry point is resolved:

    import   through the PE import table (IAT slot)
    dynamic  LoadLibraryA + GetProcAddress
    manual   hand-walked export directory, never calls GetProcAddress

The manual case is the VMProtect model. Run with no arguments for the full
matrix, or pass specific modes (they apply to every selected API).

    python run-matrix.py
    python run-matrix.py manual
    python run-matrix.py --api=d3d11 manual
"""

import glob
import os
import subprocess
import sys
import time

REPO = r"C:\Program Files\GraphicsDebugger-Naraka"
TOOL = os.path.join(REPO, "x64", "Development", "TinecmaToolcmd.exe")
FIXDIR = os.path.join(REPO, "artifacts", "naraka-fixture")
LOGDIR = os.path.join(os.environ["LOCALAPPDATA"], "Temp", "TinecmaTool")
CAPDIR = os.path.join(os.environ["LOCALAPPDATA"], "Temp", "Naraka", "fixture-tests")

EXES = {
    "d3d11": os.path.join(FIXDIR, "naraka_fixture.exe"),
    "d3d12": os.path.join(FIXDIR, "naraka_fixture12.exe"),
}

LOGNAMES = {"d3d11": "fixture.log", "d3d12": "fixture12.log"}

KEYS = {
    "d3d11": [
        ("Loading into", "dll injected"),
        ("Registering D3D11 hooks", "d3d11 hooks registered"),
        ("Graphics function resolved:", "GetProcAddress interception"),
        ("Graphics IAT hook installed", "IAT slot patched"),
        ("Graphics call entered", "hook actually called"),
        ("created wrapped device", "device wrapped"),
        ("Graphics first present", "present intercept"),
    ],
    "d3d12": [
        ("Loading into", "dll injected"),
        ("Registering D3D12 hooks", "d3d12 hooks registered"),
        ("Graphics function resolved:", "GetProcAddress interception"),
        ("Graphics IAT hook installed", "IAT slot patched"),
        ("Graphics device returned", "device create returned"),
        ("Graphics queue wrapped", "command queue wrapped"),
        ("Graphics native factory bridge installed", "M4 factory bridge"),
        ("created wrapped device", "device wrapped"),
        ("Graphics first present", "present intercept"),
    ],
}


def newest_logs():
    return set(glob.glob(os.path.join(LOGDIR, "RenderDoc_*.log")))


def run(api, mode):
    exe = EXES[api]
    if not os.path.exists(exe):
        print("missing exe for %s: %s" % (api, exe))
        return None

    capdir = os.path.join(CAPDIR, api, mode)
    os.makedirs(capdir, exist_ok=True)
    before = newest_logs()

    args = [
        TOOL, "capture", "-w", "--opt-disallow-fullscreen",
        "-c", os.path.join(capdir, "fixture"),
        "-d", FIXDIR,
        exe,
        "-resolve=" + mode,
        "-frames=10",
        "-capture-frame=4",
        "-capture-dir=" + capdir,
    ]

    t0 = time.time()
    p = subprocess.run(args, capture_output=True, timeout=240)
    dur = time.time() - t0

    log = None
    new = sorted(newest_logs() - before, key=os.path.getmtime)
    if new:
        log = new[-1]

    fixlog = os.path.join(FIXDIR, LOGNAMES[api])
    fixture_out = ""
    if os.path.exists(fixlog):
        fixture_out = open(fixlog, encoding="utf-8", errors="replace").read()

    text = ""
    if log and os.path.exists(log):
        text = open(log, encoding="utf-8", errors="replace").read()

    print("=" * 78)
    print("%s / %s   exit=%d   %.1fs" % (api, mode, p.returncode, dur))
    print("log = %s" % (log or "<none>"))

    print("-- fixture --")
    for line in fixture_out.strip().splitlines():
        print("   " + line)

    print("-- tool log --")
    hits = {}
    for needle, label in KEYS[api]:
        hit = text.count(needle)
        hits[label] = hit
        print("   [%s] %-30s %s" % ("YES" if hit else "no ", label, ("x%d" % hit) if hit else ""))

    rdcs = glob.glob(os.path.join(capdir, "*.rdc"))
    print("-- rdc --")
    for r in rdcs:
        print("   %s (%.2f MB)" % (os.path.basename(r), os.path.getsize(r) / 1048576.0))
    if not rdcs:
        print("   <none>")

    return {"api": api, "mode": mode, "hits": hits, "rdcs": rdcs}


def main():
    argv = sys.argv[1:]
    apis = ["d3d11", "d3d12"]
    modes = []
    for a in argv:
        if a.startswith("--api="):
            apis = [a.split("=", 1)[1]]
        else:
            modes.append(a)
    if not modes:
        modes = ["import", "dynamic", "manual"]

    summary = []
    for api in apis:
        for m in modes:
            r = run(api, m)
            if r:
                summary.append(r)

    print("=" * 78)
    print("SUMMARY")
    print("   %-6s %-8s %-10s %-10s %-10s %s" % ("api", "mode", "resolved", "wrapped", "present", "rdc"))
    for r in summary:
        h = r["hits"]
        print("   %-6s %-8s %-10s %-10s %-10s %d" % (
            r["api"], r["mode"],
            "yes" if h.get("GetProcAddress interception", 0) else "-",
            "yes" if h.get("device wrapped", 0) else "NO",
            "yes" if h.get("present intercept", 0) else "NO",
            len(r["rdcs"])))


if __name__ == "__main__":
    main()
