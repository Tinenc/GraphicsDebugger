#!/usr/bin/env python3
"""Summarize EID driver logs without adding overlapping timing intervals.

Python standard library only. Supports both the original driver records and timing_version=2.
Times measure CPU wall time, including time blocked on the GPU; they are not GPU timestamps.
"""

import argparse
import json
import math
import re
import statistics
from pathlib import Path


FIELDS = re.compile(r"([A-Za-z][A-Za-z0-9_.]*)=([^\s]+)")
PRIMARY_TIMINGS = (
    "total_ms", "applyInitial_ms", "contextReplay_ms", "explicitWait_ms", "explicitSubmit_ms"
)


def number(value):
    try:
        result = int(value) if re.fullmatch(r"[+-]?\d+", value) else float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


def parse_records(text):
    records = []
    for line_number, line in enumerate(text.splitlines(), 1):
        fields = dict(FIELDS.findall(line))
        if "api" not in fields:
            continue
        record = {"line": line_number}
        for key, value in fields.items():
            numeric = number(value)
            record[key] = value if numeric is None else numeric
        records.append(record)
    return records


def percentile(values, fraction):
    """Nearest-rank percentile; a single sample stays unchanged."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def metric(values):
    return {
        "samples": len(values),
        "mean": statistics.mean(values),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "max": max(values),
    }


def event_id(record):
    if isinstance(record.get("eid"), int):
        return record["eid"]
    match = re.fullmatch(r"\d+->(\d+)", str(record.get("range", "")))
    return int(match.group(1)) if match else None


def select_records(records, api="D3D12", replay_type="Full", eids=None, last=None):
    selected = [
        record for record in records
        if record.get("api") == api
        and (not replay_type or record.get("type") == replay_type)
        and isinstance(record.get("total_ms"), (int, float))
        and (not eids or event_id(record) in eids)
    ]
    return selected[-last:] if last else selected


def summarize(records):
    result = {"samples": len(records), "metrics": {}, "consistency_issues": []}
    if not records:
        return result
    keys = set(PRIMARY_TIMINGS)
    for record in records:
        keys.update(key for key in record if key.startswith(("section.", "wait.", "count.")))
        if record.get("timing_version") == 2:
            keys.add("unscoped_ms")

    for key in sorted(keys):
        # Version 2 omits zero detail counters/sections. Older records contain no detail evidence:
        # do not silently interpret missing fields in those records as measured zeros.
        detail = key.startswith(("section.", "wait.", "count.")) or key == "unscoped_ms"
        values = [
            record.get(key, 0) for record in records
            if isinstance(record.get(key), (int, float))
            or (detail and record.get("timing_version") == 2 and key not in record)
        ]
        if values:
            result["metrics"][key] = metric(values)

    for record in records:
        if record.get("timing_version") != 2:
            continue
        self_time = sum(
            value for key, value in record.items()
            if key.startswith("section.") and key.endswith("_self_ms")
            and isinstance(value, (int, float))
        )
        wait_time = sum(
            value for key, value in record.items()
            if key.startswith("wait.") and key.endswith("_ms")
            and isinstance(value, (int, float))
        )
        # Each interval is rounded to 0.001 ms independently in the source log.
        for label, observed, expected in (
            ("self + unscoped", self_time + record.get("unscoped_ms", 0), record["total_ms"]),
            ("wait contexts", wait_time, record.get("explicitWait_ms", 0)),
        ):
            if abs(observed - expected) > 0.05:
                result["consistency_issues"].append({
                    "source": record.get("source"), "line": record["line"],
                    "check": label, "observed_ms": observed, "expected_ms": expected,
                })
    return result


def compare_by_eid(current, baseline):
    """Compare medians for the same selected EIDs to avoid a changed EID mix hiding regressions."""
    def grouped(records):
        result = {}
        for record in records:
            eid = event_id(record)
            if eid is not None:
                result.setdefault(eid, []).append(record["total_ms"])
        return result

    current_eids, baseline_eids = grouped(current), grouped(baseline)
    result = []
    for eid in sorted(current_eids.keys() & baseline_eids.keys()):
        before = statistics.median(baseline_eids[eid])
        after = statistics.median(current_eids[eid])
        result.append({
            "eid": eid, "baseline_samples": len(baseline_eids[eid]),
            "current_samples": len(current_eids[eid]), "baseline_median_ms": before,
            "current_median_ms": after, "delta_ms": after - before,
            "change_percent": (after / before - 1) * 100 if before else None,
        })
    return result


def load_records(paths):
    records = []
    for path in paths:
        for record in parse_records(path.read_text(encoding="utf-8-sig", errors="replace")):
            record["source"] = str(path)
            records.append(record)
    return records


def render_report(report):
    lines = [f"{report['api']} / {report['replay_type'] or 'all types'}: {report['samples']} replays"]
    lines.append("CPU wall time in ms. Inclusive phases overlap; do not add them together.")
    metrics = report["metrics"]
    groups = [
        ("Inclusive timings", [key for key in PRIMARY_TIMINGS if key in metrics]),
        ("Disjoint section self time", sorted(
            key for key in metrics if key.endswith("_self_ms") or key == "unscoped_ms")),
        ("CPU wait reasons (subset of explicitWait)", sorted(
            key for key in metrics if key.startswith("wait.") and key.endswith("_ms"))),
        ("Counts / bytes per replay", sorted(key for key in metrics if key.startswith("count."))),
    ]
    for title, keys in groups:
        if not keys:
            continue
        lines.extend(["", title, f"{'metric':48} {'n':>5} {'mean':>12} {'p50':>12} {'p95':>12} {'max':>12}"])
        for key in keys:
            item = metrics[key]
            lines.append(f"{key:48} {item['samples']:5} {item['mean']:12.3f} "
                         f"{item['p50']:12.3f} {item['p95']:12.3f} {item['max']:12.3f}")
    if "comparison" in report:
        lines.extend(["", "Matched EID median comparison (capture/configuration must also match)",
                      f"{'EID':>8} {'baseline ms':>14} {'current ms':>14} {'delta ms':>14}"])
        for item in report["comparison"]:
            lines.append(f"{item['eid']:8} {item['baseline_median_ms']:14.3f} "
                         f"{item['current_median_ms']:14.3f} {item['delta_ms']:14.3f}")
        if not report["comparison"]:
            lines.append("No matching EIDs; no like-for-like latency comparison is available.")
    if report["consistency_issues"]:
        lines.append(f"\nTiming decomposition mismatch: {len(report['consistency_issues'])} checks failed.")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--api", default="D3D12")
    parser.add_argument("--type", default="Full", dest="replay_type",
                        help="Replay type, or an empty string for all / non-driver records")
    parser.add_argument("--eids", help="Comma-separated destination EIDs")
    parser.add_argument("--last", type=int, help="Last N matching records in input order")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--json", type=Path, dest="json_path")
    args = parser.parse_args()
    if args.last is not None and args.last < 1:
        parser.error("--last must be positive")
    try:
        eids = {int(item) for item in args.eids.split(",")} if args.eids else None
    except ValueError:
        parser.error("--eids must contain comma-separated integer EIDs")

    def select(records):
        return select_records(records, args.api, args.replay_type, eids, args.last)

    current = select(load_records(args.logs))
    report = summarize(current)
    report.update({"api": args.api, "replay_type": args.replay_type})
    if args.baseline:
        baseline = select(load_records([args.baseline]))
        report["baseline"] = summarize(baseline)
        report["comparison"] = compare_by_eid(current, baseline)
    print(render_report(report))
    if args.json_path:
        args.json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if current and not report["consistency_issues"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
