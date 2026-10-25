#!/usr/bin/env python3
"""Parse an ETW trace (tracerpt XML) into per-process module load timelines.

Works around the two things that bite when reading cod.exe:
  - EnumProcessModulesEx on cod.exe returns ERROR_ACCESS_DENIED (Ricochet strips the
    handle rights), so the module list cannot be read from outside
  - tracerpt CSV mangles variable-length payloads, so XML is used instead

Usage:
  tracerpt cod-run.etl -o cod-run.xml -of XML -y
  python parse_etw_images.py cod-run.xml [out.csv]
"""
import collections
import csv
import sys
import xml.etree.ElementTree as ET

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
IMAGE_LOAD = "5"

WATCH = (
    "d3d12.dll", "d3d11.dll", "dxgi.dll", "d3d12core.dll", "d3d12sdklayers.dll",
    "thx_sdk.dll", "nvwgf2umx.dll", "vwgf2umx", "amdvlk", "vulkan-1.dll",
    "gfsdk_aftermath", "gameinput", "codbroker", "randgrid", "azureattest",
    "tinecmatool", "renderdoc", "win32u.dll", "dwmapi.dll", "opengl32.dll",
    "dxil", "d3dcompiler",
)


def load_events(xml_path):
    root = ET.parse(xml_path).getroot()
    out = []
    for ev in root.findall("e:Event", NS):
        if ev.findtext("e:System/e:EventID", "", NS) != IMAGE_LOAD:
            continue
        t = ev.find("e:System/e:TimeCreated", NS)
        stamp = t.get("SystemTime") if t is not None else ""
        data = {d.get("Name"): (d.text or "").strip()
                for d in ev.findall("e:EventData/e:Data", NS)}
        out.append((stamp, data.get("ProcessID", ""), data.get("ImageName", ""),
                    data.get("ImageBase", "")))
    out.sort(key=lambda r: r[0])
    return out


def short(name):
    # \Device\HarddiskVolume3\Windows\System32\ntdll.dll -> ntdll.dll
    return name.rsplit("\\", 1)[-1]


def watch_hit(name):
    low = name.lower()
    return any(w in low for w in WATCH)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    xml_path = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else "cod-modules.csv"
    # cod.exe is only the HQ launcher; the renderer is sp26-cod.exe inside sp26\.
    target = sys.argv[3] if len(sys.argv) > 3 else "cod.exe"

    events = load_events(xml_path)
    print("ImageLoad events in trace: %d" % len(events))

    by_pid = collections.OrderedDict()
    for stamp, pid, img, base in events:
        by_pid.setdefault(pid, []).append((stamp, img, base))

    # Which PIDs ever loaded the target image? Those are the runs we care about.
    cod_pids = [pid for pid, rows in by_pid.items()
                if any(short(img).lower().endswith(target.lower()) for _, img, _ in rows)]

    if not cod_pids:
        print("no %s image load found in this trace" % target)
        # Still dump everything for inspection
        for pid, rows in list(by_pid.items())[:5]:
            print("  pid %s: %d modules (first: %s)" % (pid, len(rows), short(rows[0][1])))
        return 2

    print("%s process ids seen: %s" % (target, ", ".join(cod_pids)))

    with open(out_path, "w", encoding="utf-8", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["pid", "t_seconds", "module", "image_base", "image_path"])
        for pid in cod_pids:
            rows = by_pid[pid]
            t0 = rows[0][0]
            base_dt = _parse_time(t0)
            print("\n=== pid %s : %d modules ===" % (pid, len(rows)))
            print("  graphics-relevant load order:")
            for stamp, img, base in rows:
                t = (_parse_time(stamp) - base_dt).total_seconds() if base_dt else 0.0
                w.writerow([pid, "%.4f" % t, short(img), base, img])
                if watch_hit(img):
                    print("    +%07.3fs  %-24s %s" % (t, short(img), base))

    print("\nCSV -> %s" % out_path)
    return 0


def _parse_time(stamp):
    # tracerpt emits nanosecond precision with an odd offset, e.g.
    # 2026-10-25T23:10:08.753777500+07:59 - strptime's %f only takes 6 digits, so
    # fromisoformat (3.11+, accepts any fraction length) is tried first.
    import datetime
    try:
        return datetime.datetime.fromisoformat(stamp)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.datetime.strptime(stamp, fmt)
        except ValueError:
            continue
    return None


if __name__ == "__main__":
    sys.exit(main())
