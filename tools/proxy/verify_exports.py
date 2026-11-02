"""Verify each proxy re-exports the system DLL's export table exactly.

A proxy that is missing even one export is worse than no proxy at all: the host
process imports that name, the loader cannot satisfy it, and the game dies at
startup with no useful message. So the check is set equality on (ordinal, name),
not a spot check of the interesting functions.

Exit code is non-zero if any table is short, extra, or has a mismatched ordinal.

    python verify_exports.py
    python verify_exports.py --dir "C:/Program Files/GraphicsDebugger-Naraka/tools/proxy"
"""

import argparse
import glob
import os
import re
import subprocess
import sys

PAIRS = [("dxgi.dll", "dxgi.dll"), ("d3d11.dll", "d3d11.dll"), ("d3d12.dll", "d3d12.dll")]

ROW_NAMED = re.compile(r"^\s+(\d+)\s+(?:[0-9A-Fa-f]+\s+)?([0-9A-Fa-f]{8})\s+(\S+)\s*$")
ROW_NONAME = re.compile(r"^\s+(\d+)\s+([0-9A-Fa-f]{8})\s+\[NONAME\]\s*$")


def find_dumpbin():
    hits = glob.glob(
        r"C:\Program Files\Microsoft Visual Studio\**\bin\Hostx64\x64\dumpbin.exe", recursive=True
    )
    return hits[0] if hits else None


def exports_of(dumpbin, path):
    out = subprocess.run(
        [dumpbin, "-exports", path], capture_output=True, text=True, errors="replace"
    ).stdout

    table = {}
    for line in out.splitlines():
        m = ROW_NONAME.match(line)
        if m:
            table[int(m.group(1))] = None
            continue
        m = ROW_NAMED.match(line)
        if m:
            name = m.group(3)
            if name.startswith("[") or name.startswith(".") or name.lower() == "name":
                continue
            table[int(m.group(1))] = name
    return table


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--dumpbin", default=None)
    args = ap.parse_args()

    dumpbin = args.dumpbin or find_dumpbin()
    if not dumpbin:
        print("dumpbin.exe not found")
        return 2

    sysdir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    failures = 0

    for system_name, proxy_name in PAIRS:
        system_path = os.path.join(sysdir, system_name)
        proxy_path = os.path.join(args.dir, proxy_name)

        print("=" * 74)
        print("%s  vs  %s" % (system_path, proxy_path))

        if not os.path.exists(proxy_path):
            print("  FAIL: proxy not built")
            failures += 1
            continue

        want = exports_of(dumpbin, system_path)
        got = exports_of(dumpbin, proxy_path)

        missing = sorted(set(want) - set(got))
        extra = sorted(set(got) - set(want))

        mismatched = []
        for ord_ in sorted(set(want) & set(got)):
            if want[ord_] != got[ord_]:
                mismatched.append((ord_, want[ord_], got[ord_]))

        print("  system: %d exports   proxy: %d exports" % (len(want), len(got)))

        if missing:
            print("  FAIL missing ordinals: %s" % missing)
            failures += 1
        if extra:
            print("  FAIL extra ordinals:   %s" % extra)
            failures += 1
        if mismatched:
            for ord_, w, g in mismatched:
                print("  FAIL ordinal %d: system=%s proxy=%s" % (ord_, w, g))
            failures += 1

        if not (missing or extra or mismatched):
            print("  OK  export table matches exactly")

    print("=" * 74)
    if failures:
        print("RESULT: %d check(s) FAILED" % failures)
        return 1

    print("RESULT: all proxy export tables match the system DLLs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
