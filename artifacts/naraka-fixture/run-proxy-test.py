"""Exercise the proxy layer against a fixture that resolves the graphics API like Naraka does.

The proxy has two independent jobs, and they are tested separately because they
fail for different reasons:

  1. TRANSPARENCY - re-export the system DLL's whole table and forward. If this is
     wrong the game does not start. Testable right now, with the capture side
     switched off, so a failure can only be the forwarding logic.
  2. BRIDGE - route device/factory creation into the TinecmaTool core so the
     device comes back wrapped and can be captured. Needs a core build that
     exports TINECMATOOL_D3D11CreateDevice / TINECMATOOL_D3D12CreateDevice;
     reported as SKIP, never as PASS, when the deployed core is older.

Cases:
  control        no proxies. dxgi.dll must resolve to System32. This is the
                 negative control - without it "resolved from work dir" proves
                 nothing, because it might always match.
  passthrough    proxies present, capture mode=off. Device must still be created
                 and frames presented. This is the transparency test.
  gate-off       proxies present, capture mode=renderdoc, but the process is not
                 the configured target. Must behave like passthrough.
  d3d11 / d3d12  proxies present, capture mode=renderdoc. Bridge assertions.

    python run-proxy-test.py
    python run-proxy-test.py --only passthrough
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
PROXYDIR = os.path.join(REPO, "tools", "proxy")
CORE = os.path.join(REPO, "x64", "Development", "TinecmaTool.dll")
FIXTURE = os.path.join(HERE, "proxy_fixture.exe")
WORKROOT = os.path.join(os.environ["LOCALAPPDATA"], "Temp", "Naraka", "proxy-tests")

PROXIES = ["dxgi.dll", "d3d11.dll", "d3d12.dll"]

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


def core_has_bridge():
    """True when the deployed TinecmaTool.dll exports the proxy bridge symbols."""
    if not os.path.exists(CORE):
        return False
    try:
        with open(CORE, "rb") as f:
            blob = f.read()
    except OSError:
        return False
    return b"TINECMATOOL_D3D11CreateDevice" in blob and b"TINECMATOOL_D3D12CreateDevice" in blob


def prepare(case, with_proxies, target, mode):
    d = os.path.join(WORKROOT, case)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
    os.makedirs(d, exist_ok=True)

    shutil.copy2(FIXTURE, os.path.join(d, "proxy_fixture.exe"))
    if with_proxies:
        for p in PROXIES:
            src = os.path.join(PROXYDIR, p)
            if not os.path.exists(src):
                raise SystemExit("proxy %s not built - run tools\\proxy\\build.cmd" % p)
            shutil.copy2(src, os.path.join(d, p))

    capdir = os.path.join(d, "captures")
    logdir = os.path.join(d, "logs")
    os.makedirs(capdir, exist_ok=True)
    os.makedirs(logdir, exist_ok=True)

    if with_proxies:
        with open(os.path.join(d, "naraka_proxy.ini"), "w", encoding="utf-8") as f:
            f.write("[naraka]\n")
            f.write("mode=%s\n" % mode)
            f.write("target_process=%s\n" % target)
            f.write("rdoc_dll=%s\n" % CORE)
            f.write("capture_dir=%s\n" % capdir)
            f.write("log_dir=%s\n" % logdir)

    return d, capdir, logdir


def run_case(case, api, with_proxies, target, mode, frames, expect_capture):
    print("=" * 78)
    print(
        "CASE %s   api=%s   proxies=%s   mode=%s   target=%s"
        % (case, api, with_proxies, mode, target)
    )

    d, capdir, logdir = prepare(case, with_proxies, target, mode)
    exe = os.path.join(d, "proxy_fixture.exe")

    proc = subprocess.Popen(
        [exe, "-api=%s" % api, "-frames=%d" % frames],
        cwd=d,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )

    # Let the device come up before firing the trigger. Capturing on frame 0 is the
    # watchdog-stall pattern the design note warns about.
    time.sleep(2.0)
    if expect_capture:
        trig = os.path.join(capdir, "trigger_capture_proxy_fixture")
        with open(trig, "w") as f:
            f.write("CAPTURE")
        print("  trigger written: %s" % trig)

    done_file = os.path.join(capdir, "capture_done.txt")
    deadline = time.time() + 45
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        if expect_capture and os.path.exists(done_file):
            time.sleep(1.0)
            break
        time.sleep(0.25)

    killed = False
    if proc.poll() is None:
        killed = True
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    out = proc.stdout.read() if proc.stdout else ""
    print("-- fixture --" + ("  (terminated by harness)" if killed else ""))
    for line in out.strip().splitlines():
        print("   " + line)

    log_path = os.path.join(logdir, "naraka_proxy.log")
    log = ""
    if os.path.exists(log_path):
        log = open(log_path, encoding="utf-8", errors="replace").read()

    print("-- proxy log -- %s" % log_path)
    for line in log.strip().splitlines()[-18:]:
        print("   " + line)

    # ---- assertions -------------------------------------------------------
    # Provenance comes from the fixture's "resolve <name> : <path>" lines only.
    # Matching the whole stdout would always hit, since the exe path contains
    # the work directory.
    resolved = {}
    for line in out.splitlines():
        if line.strip().startswith("resolve "):
            _, rest = line.split("resolve ", 1)
            name, _, path = rest.partition(":")
            resolved[name.strip()] = path.strip()

    def from_workdir(name):
        p = resolved.get(name, "")
        return bool(p) and p.lower().startswith(d.lower())

    def from_system(name):
        return "\\system32\\" in resolved.get(name, "").lower()

    device_ok = "create device : hr=0x00000000" in out
    presented = "presented     :" in out

    checks = []  # (label, state)

    if not with_proxies:
        checks.append(("dxgi.dll resolved from System32", PASS if from_system("dxgi.dll") else FAIL))
        checks.append(
            ("%s.dll resolved from System32" % api, PASS if from_system("%s.dll" % api) else FAIL)
        )
        checks.append(("device created", PASS if device_ok else FAIL))
    else:
        checks.append(
            ("dxgi.dll resolved from work dir (proxy)", PASS if from_workdir("dxgi.dll") else FAIL)
        )
        checks.append(
            (
                "%s.dll resolved from work dir (proxy)" % api,
                PASS if from_workdir("%s.dll" % api) else FAIL,
            )
        )
        checks.append(("proxy log written", PASS if log else FAIL))
        checks.append(
            ("system DLL loaded by full path", PASS if "system dxgi loaded at" in log else FAIL)
        )

        target_recognised = "-> CAPTURE" if target == "proxy_fixture.exe" else "pass-through only"
        checks.append(
            (
                "gate decision logged (%s)" % target_recognised,
                PASS if target_recognised in log else FAIL,
            )
        )

        # The whole point of a proxy: the process must not notice it.
        checks.append(("device created through proxy", PASS if device_ok else FAIL))
        checks.append(("frames presented", PASS if presented else FAIL))

        if mode == "off" or target != "proxy_fixture.exe":
            checks.append(
                ("core not loaded (mode/target gate)", PASS if "core not loaded" in log else FAIL)
            )
        elif not core_has_bridge():
            # Degraded path: the deployed core cannot bridge, so the proxy must
            # decline to load it rather than leave a hooking core resident.
            checks.append(
                (
                    "stale core refused (no bridge exports)",
                    PASS if "REFUSING to load" in log else FAIL,
                )
            )
        else:
            checks.append(("bridge invoked", PASS if "BRIDGE " in log else FAIL))

    rdcs = glob.glob(os.path.join(capdir, "*.rdc"))
    print("-- rdc --")
    if rdcs:
        for r in rdcs:
            print("   %s (%.2f MB)" % (os.path.basename(r), os.path.getsize(r) / 1048576.0))
    else:
        print("   <none>")

    if expect_capture:
        if core_has_bridge():
            checks.append(("capture file produced", PASS if rdcs else FAIL))
        else:
            checks.append(("capture file produced (needs rebuilt core)", SKIP))

    print("-- verdict --")
    for label, state in checks:
        print("   [%s] %s" % (state, label))

    failed = [c for c in checks if c[1] == FAIL]
    return {
        "case": case,
        "api": api,
        "rdc": len(rdcs),
        "fail": len(failed),
        "skip": len([c for c in checks if c[1] == SKIP]),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
    ap.add_argument("--frames", type=int, default=240)
    args = ap.parse_args()

    if not os.path.exists(FIXTURE):
        raise SystemExit(
            "missing %s - build it with artifacts\\naraka-fixture\\build-fixture.cmd" % FIXTURE
        )

    has_bridge = core_has_bridge()
    print("core: %s" % CORE)
    print("core exports proxy bridge: %s" % has_bridge)
    if not has_bridge:
        print("  -> bridge assertions will be SKIP; rebuild the core to enable them.")
        print("     msbuild renderdoc.sln /m /p:Configuration=Development /p:Platform=x64")

    plan = [
        #  case           api      proxies target                mode         frames capture
        ("control", "d3d11", False, "proxy_fixture.exe", "renderdoc", 120, False),
        ("passthrough", "d3d11", True, "proxy_fixture.exe", "off", 120, False),
        ("gate-off", "d3d11", True, "not_the_target.exe", "renderdoc", 120, False),
        ("proxy-d3d11", "d3d11", True, "proxy_fixture.exe", "renderdoc", 240, True),
        ("proxy-d3d12", "d3d12", True, "proxy_fixture.exe", "renderdoc", 240, True),
    ]
    if args.only:
        plan = [p for p in plan if p[0] == args.only or p[1] == args.only]

    results = [run_case(*p) for p in plan]

    print("=" * 78)
    print("SUMMARY")
    print("   %-13s %-6s %-5s %-6s %s" % ("case", "api", "rdc", "skip", "verdict"))
    for r in results:
        print(
            "   %-13s %-6s %-5d %-6d %s"
            % (r["case"], r["api"], r["rdc"], r["skip"], "PASS" if r["fail"] == 0 else "FAIL")
        )

    failed = [r for r in results if r["fail"]]
    print()
    print("RESULT: %d/%d cases passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
