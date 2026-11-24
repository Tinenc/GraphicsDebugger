"""Summarize bounded ReplayChunk records without opening a capture or using the GPU."""
from __future__ import print_function

import argparse
import json
from pathlib import Path

from analyze_eid_timing import parse_records, event_id


FIELDS = ("calls", "decodeBoundaries", "decode_ms", "execute_ms", "unclassified_ms",
          "decode_self_ms", "execute_self_ms", "unclassified_self_ms", "wait_ms", "submit_ms", "total_ms")


def summarize_chunks(records, driver="D3D12", replay_type="", eids=None):
    selected = [r for r in records if r.get("api") == "ReplayChunk" and r.get("driver") == driver
                and (not replay_type or r.get("type") == replay_type)
                and (not eids or event_id(r) in eids)]
    grouped, problems = {}, []
    for record in selected:
        key = (record.get("chunkId"), record.get("chunk"), record.get("type"))
        group = grouped.setdefault(key, dict(chunkId=key[0], chunk=key[1], type=key[2], records=0,
                                             **{field: 0 for field in FIELDS}))
        group["records"] += 1
        for field in FIELDS:
            group[field] += record.get(field, 0)
        total = record.get("total_ms", 0)
        phases = sum(record.get(field, 0) for field in ("decode_ms", "execute_ms", "unclassified_ms"))
        if abs(total - phases) > 0.01:
            problems.append({"line": record.get("line"), "check": "decode + execute + unclassified != total"})
        if record.get("decodeBoundaries", 0) > record.get("calls", 0):
            problems.append({"line": record.get("line"), "check": "more decode boundaries than chunks"})
        for phase in ("decode", "execute", "unclassified"):
            if record.get(phase + "_self_ms", 0) > record.get(phase + "_ms", 0) + 0.005:
                problems.append({"line": record.get("line"), "check": phase + " self exceeds inclusive"})
    return {"schema": "renderdoc-chunk-profile/v1", "driver": driver, "recordCount": len(selected),
            "meaning": "Aggregated wall time across selected replays; wait/submit overlap phase totals. "
                       "Execution includes replay wrapper state maintenance and native API recording, "
                       "not GPU execution timestamps. Other is the bounded top-type remainder.",
            "totals": {field: sum(r.get(field, 0) for r in selected) for field in FIELDS},
            "chunks": sorted(grouped.values(), key=lambda c: c["total_ms"], reverse=True),
            "consistencyIssues": problems}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--driver", default="D3D12")
    parser.add_argument("--type", default="", dest="replay_type")
    parser.add_argument("--eids", default="")
    parser.add_argument("--json", type=Path, dest="output")
    args = parser.parse_args()
    records = []
    for path in args.logs:
        records.extend(parse_records(path.read_text(encoding="utf-8-sig", errors="replace")))
    eids = {int(v) for v in args.eids.split(",") if v.strip()}
    report = summarize_chunks(records, args.driver, args.replay_type, eids)
    print(report["meaning"])
    print("{:<46} {:>7} {:>10} {:>10} {:>10} {:>10}".format(
        "Chunk / replay type", "calls", "decode", "execute", "exec_self", "unknown"))
    for c in report["chunks"]:
        print("{:<46} {:>7} {:>10.3f} {:>10.3f} {:>10.3f} {:>10.3f}".format(
            str(c["chunk"]) + " / " + str(c["type"]), c["calls"], c["decode_ms"],
            c["execute_ms"], c["execute_self_ms"], c["unclassified_ms"]))
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if report["recordCount"] and not report["consistencyIssues"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
