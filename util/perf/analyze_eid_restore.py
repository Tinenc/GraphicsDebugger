"""CPU-only, bounded attribution of measured initial-resource restore records.

Reads explicit checkpoint-profile JSON and each measured timing slice. No capture,
RenderDoc import, GPU work, resource readback or mutation occurs. ResourceId joins
are allowed only through an inventory manifest naming the same capture. Native
copy counters, upload-cache source sizes, and metadata logical bytes stay separate.
"""
from __future__ import print_function

import argparse
from collections import Counter, defaultdict, deque
import hashlib
import json
from pathlib import Path
import re

from analyze_eid_checkpoints import classify_visit, stats
from analyze_eid_timing import parse_records


MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_VISITS = 4096
MAX_RESOURCES = 100000
TRIPLE = re.compile(r"ResourceId::(\d+):(\d+)/(\d+)/(\d+)")
SOURCE_FIELDS = ("cpuMemcpyBytes", "gpuUploadBufferBytes", "gpuUploadFootprintBytes", "gpuUploadWholeCopies",
                 "gpuDefaultBufferBytes", "gpuDefaultFootprintBytes", "gpuDefaultWholeCopies",
                 "gpuOtherBufferBytes", "gpuOtherFootprintBytes", "gpuOtherWholeCopies")
SOURCE_TUPLE = re.compile(r"ResourceId::(\d+):" + "/".join([r"(\d+)"] * len(SOURCE_FIELDS)))
COUNTERS = (
    "count.initialUploadCacheHits", "count.initialUploadCacheBytes",
    "count.initialUploadCacheFallbackBytes", "count.initialGPUCopyBufferBytes",
    "count.initialGPUCopyFootprintBytes",
)
NOTES = [
    "All milliseconds are CPU-observed inclusive wall times; no GPU timestamp or input-to-scanout measurement.",
    "Scope timings are not added to their children. Initial source-cache bytes describe source buffers, not actual transfer bytes.",
    "actualCopyBytes is the native recorded initial-copy accounting, not measured PCIe traffic or complete checkpoint restore traffic.",
    "Metadata logicalBytes is an API size estimate, not native allocation size or restore work.",
    "Whole-resource copy count is a count, not bytes. Missing per-resource detail remains unavailable.",
    "Bounded native detail retains its omission counters. Resource type/window attribution covers listed resources only; omitted bytes have no inferred type or use.",
    "No reported GetUsage in an interval and a first Clear do not prove safe omission; byte coverage, aliases and resume state remain unverified.",
    "Different captures and EIDs are different workloads, even with matching binaries. These tables diagnose differences, not controlled speedup.",
    "p50/p95 use nearest rank. With even sample counts this differs slightly from the arithmetic midpoint median used in earlier summaries.",
]


class Reader:
    def __init__(self, file_limit=MAX_FILE_BYTES, total_limit=MAX_TOTAL_BYTES):
        self.file_limit, self.total_limit, self.bytes_read = file_limit, total_limit, 0

    def read(self, path):
        path = Path(path)
        size = path.stat().st_size
        if size > self.file_limit or size > self.total_limit - self.bytes_read:
            raise ValueError("Input byte limit exceeded: " + str(path))
        # Also bound the read if the file grows after stat.
        with path.open("rb") as source:
            data = source.read(min(self.file_limit, self.total_limit - self.bytes_read) + 1)
        self.bytes_read += len(data)
        if len(data) > self.file_limit or self.bytes_read > self.total_limit:
            raise ValueError("Input grew beyond byte limit: " + str(path))
        return data

    def json(self, path):
        return json.loads(self.read(path).decode("utf-8-sig"))


def canonical_capture(path):
    return str(Path(path).resolve()).replace("\\", "/").casefold()


def resolve_sidecar(value, parent):
    path = Path(value)
    return path if path.is_file() else parent / path.name


def parse_restore_records(text):
    """Collapse only consecutive opposite-sink mirror records, not repeated native calls."""
    rows, issues, removed = [], [], 0
    previous = None
    for line_number, line in enumerate(text.splitlines(), 1):
        marker = line.find("api=D3D12DirtyRestore ")
        if marker < 0:
            continue
        payload = line[marker:].strip()
        sink = "rdclog" if "RDOC " in line[:marker] else "append"
        if previous and previous[0] == payload and previous[1] != sink and not previous[2]:
            previous = (payload, sink, True)
            removed += 1
            continue
        previous = (payload, sink, False)
        parsed = parse_records(payload)[0]
        row = {key: parsed.get(key) for key in (
            "enabled", "shadow", "actualCopyBytes", "skippedStaticBytes", "wholeResourceCopies",
            "dirtyResources", "dirtyHeaps", "reason", "restoreTimingVersion", "resourceDetailCount",
            "omittedResourceCount", "omittedActualCopyBytes", "omittedSkippedStaticBytes",
            "omittedWholeResourceCopies") + SOURCE_FIELDS}
        row["line"] = line_number
        detail = payload.split("resourceActualSkippedWhole=", 1)
        row["resourceDetailStatus"] = "unavailable"
        row["resources"] = []
        if len(detail) == 2:
            seen = set()
            for match in TRIPLE.finditer(detail[1]):
                rid, actual, skipped, whole = match.groups()
                if rid in seen:
                    issues.append("Duplicate ResourceId in restore line %d: %s" % (line_number, rid))
                seen.add(rid)
                row["resources"].append({"resource": "ResourceId::" + rid,
                                         "actualCopyBytes": int(actual), "skippedStaticBytes": int(skipped),
                                         "wholeResourceCopies": int(whole)})
                if len(seen) > MAX_RESOURCES:
                    raise ValueError("Per-resource restore limit exceeded")
            residue = TRIPLE.sub("", detail[1]).strip()
            valid = not residue and len(seen) == len(row["resources"])
            for field in ("actualCopyBytes", "skippedStaticBytes", "wholeResourceCopies"):
                observed = sum(resource[field] for resource in row["resources"])
                if row.get("restoreTimingVersion") == 2:
                    omitted_key = "omitted" + field[0].upper() + field[1:]
                    omitted = row.get(omitted_key)
                    if not isinstance(omitted, int) or omitted < 0:
                        valid = False
                        issues.append("Missing/invalid v2 omission accounting at line %d: %s" % (line_number, omitted_key))
                    else:
                        observed += omitted
                if observed != row.get(field):
                    valid = False
                    issues.append("Restore line %d %s detail sum %d differs from aggregate %r" %
                                  (line_number, field, observed, row.get(field)))
            if residue:
                issues.append("Malformed/truncated restore detail at line %d" % line_number)
            if row.get("restoreTimingVersion") == 2 and row.get("resourceDetailCount") != len(row["resources"]):
                valid = False
                issues.append("v2 resource detail count mismatch at line %d" % line_number)
            if row.get("restoreTimingVersion") == 2 and (
                    not isinstance(row.get("omittedResourceCount"), int) or row["omittedResourceCount"] < 0):
                valid = False
                issues.append("Invalid v2 omitted resource count at line %d" % line_number)
            row["resourceDetailStatus"] = ("bounded" if row.get("omittedResourceCount", 0) else "complete") if valid else "incomplete"
        row["sourceDetailStatus"] = "unavailable"
        source_detail = parsed.get("resourceSourceCopies")
        if row.get("restoreTimingVersion") == 2 and isinstance(source_detail, str):
            source_resources = {}
            valid_source = True
            if source_detail != "none":
                for part in source_detail.split(","):
                    match = SOURCE_TUPLE.fullmatch(part)
                    if not match:
                        valid_source = False
                        continue
                    resource = "ResourceId::" + match.group(1)
                    if resource in source_resources:
                        valid_source = False
                    source_resources[resource] = dict(zip(SOURCE_FIELDS, map(int, match.groups()[1:])))
            if set(source_resources) != {r["resource"] for r in row["resources"]}:
                valid_source = False
            for resource in row["resources"]:
                resource["sourceCopies"] = source_resources.get(resource["resource"])
            for field in SOURCE_FIELDS:
                aggregate = row.get(field)
                visible = sum(values[field] for values in source_resources.values())
                if not isinstance(aggregate, int) or aggregate < visible or (
                        not row.get("omittedResourceCount") and aggregate != visible):
                    valid_source = False
            source_bytes = sum(row.get(field) or 0 for field in SOURCE_FIELDS if field.endswith("Bytes"))
            source_whole = sum(row.get(field) or 0 for field in SOURCE_FIELDS if field.endswith("WholeCopies"))
            if source_bytes != row.get("actualCopyBytes") or source_whole != row.get("wholeResourceCopies"):
                valid_source = False
            row["sourceDetailStatus"] = row["resourceDetailStatus"] if valid_source else "incomplete"
            if not valid_source:
                issues.append("v2 source-copy tuple/count/aggregate mismatch at line %d" % line_number)
        rows.append(row)
    return rows, issues, removed


def scope_measurements(records):
    """Keep replay type and nesting separate; v2 omitted counters are defined as zero."""
    groups = defaultdict(list)
    for row in records:
        if row.get("api") in ("D3D11", "D3D12", "OpenGL", "Vulkan"):
            groups[(row["api"], row.get("type"), row.get("replay_depth", "unknown"))].append(row)
    result = []
    for key, rows in sorted(groups.items(), key=lambda item: str(item[0])):
        fields = {field for row in rows for field in row if field in COUNTERS or field in (
            "total_ms", "applyInitial_ms", "contextReplay_ms", "explicitWait_ms") or
            field.startswith("section.initial")}
        metrics = {}
        for field in sorted(fields | set(COUNTERS)):
            values = [row[field] for row in rows if isinstance(row.get(field), (int, float))]
            # Do not turn v1 or absent logs into measured zero counters.
            if field in COUNTERS:
                values += [0 for row in rows if field not in row and row.get("timing_version") == 2]
            metrics[field] = stats(values)
        result.append({"api": key[0], "replayType": key[1], "replayDepth": key[2],
                       "recordCount": len(rows), "metricsPerCall": metrics})
    return result


def metadata_for(entry, checkpoint, target):
    if not entry:
        return {"status": "unavailable", "logicalBytes": None, "window": None}
    window = next((row for row in entry.get("usageWindows", []) if
                   row.get("afterEventId") == checkpoint and row.get("throughEventId") == target), None)
    return {"status": "observedMetadata", "name": entry.get("name"), "kind": entry.get("kind"),
            "logicalBytes": entry.get("logicalBytes"), "format": entry.get("format"),
            "width": entry.get("width"), "height": entry.get("height"),
            "creationFlags": entry.get("creationFlags"), "usage": entry.get("usage"), "window": window}


def load_inventory(path, reader):
    report = reader.json(path)
    if report.get("schema") != "renderdoc-checkpoint-inventory/v1" or not report.get("capture"):
        raise ValueError("Not a capture-bound inventory manifest: " + str(path))
    resources_path = resolve_sidecar(report.get("resourceInventoryFile", ""), path.parent)
    resources = reader.json(resources_path)
    if not isinstance(resources, list) or len(resources) > MAX_RESOURCES:
        raise ValueError("Inventory resource limit/type invalid")
    index = {}
    for entry in resources:
        key = entry["resource"]
        if key in index:
            raise ValueError("Duplicate inventory ResourceId " + key)
        index[key] = entry
    return canonical_capture(report["capture"]), index, {
        "path": str(path), "resourcePath": str(resources_path), "usageComplete": report.get("usageComplete"),
        "capture": report["capture"], "resourceCount": len(index)}


def analyze_profile(report, path, reader, inventory=None, top=20):
    if report.get("schema") != "renderdoc-checkpoint-profile/v1":
        raise ValueError("Expected renderdoc-checkpoint-profile/v1: " + str(path))
    visits, samples = report.get("visits", []), report.get("samples", [])
    if len(visits) > MAX_VISITS or len(samples) > MAX_VISITS * 4:
        raise ValueError("Profile visit/sample limit exceeded")
    pending = defaultdict(deque)
    for sample in samples:
        pending[(sample.get("phase"), sample.get("eventId"))].append(sample)
    rows, issues, grouped = [], [], defaultdict(list)
    for index, visit in enumerate(visits):
        context, category, hits, builds = classify_visit(visit)
        queue = pending[(visit.get("phase", "") + ".replay", visit.get("eventId"))]
        sample = queue.popleft() if queue else {}
        records, restores, source, removed = [], [], None, 0
        value = sample.get("timingSlice")
        if value:
            sidecar = resolve_sidecar(value, path.parent)
            if sidecar.is_file():
                raw = reader.read(sidecar)
                text = raw.decode("utf-8-sig", errors="replace")
                records = parse_records(text)
                restores, detail_issues, removed = parse_restore_records(text)
                issues.extend("visit %d: %s" % (index, issue) for issue in detail_issues)
                source = {"path": str(sidecar), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        if source is None:
            records = sample.get("timingRecords", [])
            if not all(isinstance(row, dict) for row in records):
                raise ValueError("Invalid parsed timingRecords")
            restores = [dict(row, resources=[], resourceDetailStatus="unavailable") for row in records
                        if row.get("api") == "D3D12DirtyRestore"]
            issues.append("visit %d: raw timing slice unavailable; resource detail unavailable" % index)
        after = visit.get("after", {})
        row = {"index": index, "eventId": visit.get("eventId"), "phase": visit.get("phase"),
               "context": context, "category": category, "force": bool(visit.get("force")),
               "checkpointEnabled": after.get("enabled"), "reportedLastResumeEvent": after.get("lastResumeEvent"),
               "lastResumeEvent": after.get("lastResumeEvent") if hits > 0 else None,
               "hits": hits, "builds": builds, "replayWallMs": visit.get("replayWallMs"),
               "readbackWallMs": visit.get("readbackWallMs"), "sampleStatus": sample.get("status"),
               "scopeRecords": records, "restoreRecords": restores, "source": source,
               "mirroredRestoreRecordsRemoved": removed}
        rows.append(row)
        # Keep initialization/disabled transitions, forced calls, and steady state visibly separate.
        reasons = tuple(sorted({str(r.get("reason")) for r in restores}))
        key = (row["phase"], context, category, row["eventId"], row["force"], row["checkpointEnabled"], reasons)
        grouped[key].append(row)

    summaries = []
    for key, values in sorted(grouped.items(), key=lambda item: str(item[0])):
        restores = [r for value in values for r in value["restoreRecords"]]
        complete = [r for r in restores if r["resourceDetailStatus"] == "complete"]
        usable = [r for r in restores if r["resourceDetailStatus"] in ("complete", "bounded")]
        resource_values = defaultdict(list)
        for record in usable:
            for entry in record["resources"]:
                resource_values[entry["resource"]].append(entry)
        checkpoint_values = sorted({v["lastResumeEvent"] for v in values if v["lastResumeEvent"] is not None})
        checkpoint = checkpoint_values[0] if len(checkpoint_values) == 1 else None
        resources = []
        for resource, samples_for_resource in resource_values.items():
            resources.append({"resource": resource, "recordCount": len(samples_for_resource),
                "measuredPerRestore": {field: stats([s[field] for s in samples_for_resource]) for field in
                                       ("actualCopyBytes", "skippedStaticBytes", "wholeResourceCopies")},
                "measuredSourceCopies": {field: stats([(s.get("sourceCopies") or {}).get(field)
                                                      for s in samples_for_resource]) for field in SOURCE_FIELDS},
                "metadata": metadata_for((inventory or {}).get(resource), checkpoint, key[3])})
        resources.sort(key=lambda r: (-r["measuredPerRestore"]["actualCopyBytes"]["mean"], r["resource"]))
        by_type = defaultdict(list)
        by_window = defaultdict(list)
        for resource in resources:
            by_type[resource["metadata"].get("kind") or "unavailable"].append(resource)
            window = resource["metadata"].get("window")
            window_class = ("unavailable" if window is None else "noReportedUseCandidate"
                            if window.get("noReportedUse") else "firstUseIncludesClearCandidate"
                            if window.get("startsWithReportedClear") else "reportedUse")
            by_window[window_class].append(resource)
        summaries.append({"phase": key[0], "context": key[1], "category": key[2], "eventId": key[3],
            "force": key[4], "checkpointEnabled": key[5], "restoreReasons": list(key[6]),
            "visitCount": len(values), "sampleStatusCounts": dict(Counter(v["sampleStatus"] for v in values)),
            "resumeEvents": checkpoint_values, "replayWallMs": stats([v["replayWallMs"] for v in values]),
            "readbackWallMs": stats([v["readbackWallMs"] for v in values]), "restoreRecordCount": len(restores),
            "completeResourceDetailRecords": len(complete),
            "boundedResourceDetailRecords": sum(r["resourceDetailStatus"] == "bounded" for r in restores),
            "measuredSourceCopiesPerRestore": {field: stats([r.get(field) for r in restores]) for field in SOURCE_FIELDS},
            "omittedResourceDetailPerRestore": {field: stats([r.get(field) for r in restores]) for field in (
                "omittedResourceCount", "omittedActualCopyBytes", "omittedSkippedStaticBytes", "omittedWholeResourceCopies")},
            "measuredPerRestore": {field: stats([r.get(field) for r in restores]) for field in (
                "actualCopyBytes", "skippedStaticBytes", "wholeResourceCopies", "dirtyResources", "dirtyHeaps")},
            "resourceTypeAccounting": {kind: {
                "listedResources": len(group),
                "sumOfReportedResourceMeanActualCopyBytes": sum(r["measuredPerRestore"]["actualCopyBytes"]["mean"] for r in group),
                "estimatedLogicalBytesOfListedResources": sum(r["metadata"].get("logicalBytes") or 0 for r in group)
                    if kind != "unavailable" else None} for kind, group in sorted(by_type.items())},
            "resourceDetailStatus": ("complete" if len(complete) == len(restores) else "bounded")
                if restores and len(usable) == len(restores) else "unavailableOrIncomplete",
            "usageWindowCandidates": {kind: {
                "resourceCount": len(group),
                "sumOfReportedResourceMeanActualCopyBytes": sum(r["measuredPerRestore"]["actualCopyBytes"]["mean"] for r in group),
                "safeToSkipRestore": False,
                "qualification": "resource-wide metadata only; require independent alias, range coverage and backend proof"}
                for kind, group in sorted(by_window.items())},
            "largestCopiedResources": resources[:top],
            "scopeMeasurements": scope_measurements([record for v in values for record in v["scopeRecords"]]),
            "mirroredRestoreRecordsRemoved": sum(v["mirroredRestoreRecordsRemoved"] for v in values)})
    for row in rows:
        del row["scopeRecords"]
        for record in row["restoreRecords"]:
            record["resourceCount"] = len(record.pop("resources"))
    return {"path": str(path), "capture": report.get("capture"), "captureDriver": report.get("captureDriver"),
            "buildIdentity": report.get("buildIdentity"), "buildLabel": report.get("buildLabel"),
            "sourceAccepted": report.get("ok"), "measurementComplete": report.get("measurementComplete"),
            "configuration": report.get("configuration"), "issues": issues, "groups": summaries, "visits": rows}


def render_markdown(report):
    lines = ["# Initial restore resource attribution", ""] + ["- " + note for note in NOTES]
    lines += ["", "Only explicit steadyEnabled/steadyDisabled groups appear below. Other phases and failures remain in JSON.", "",
              "| Run | Capture | EID | CP | Resume | Reason | n | Replay p50 ms | Initial copied bytes/restore | Actual UPLOAD copy bytes/restore | Actual DEFAULT copy bytes/restore | Detail |",
              "|---|---|---:|---|---|---|---:|---:|---:|---:|---:|---|"]
    def mean(metric):
        return "unavailable" if not metric else "%.0f" % metric["mean"]
    def source_bytes(group, source):
        metrics = group["measuredSourceCopiesPerRestore"]
        buffer = metrics.get("gpu" + source + "BufferBytes")
        footprint = metrics.get("gpu" + source + "FootprintBytes")
        if not buffer or not footprint:
            return "unavailable"
        return "%.0f" % (buffer["mean"] + footprint["mean"])
    for run in report["runs"]:
        for group in run["groups"]:
            if group["phase"] not in ("steadyEnabled", "steadyDisabled") or group["force"]:
                continue
            wall = group["replayWallMs"]
            label = str(run.get("buildLabel") or Path(run["path"]).stem).replace("|", "\\|")
            if run.get("sourceAccepted") is not True:
                label += " (unaccepted)"
            lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                label, Path(run["capture"]).name, group["eventId"], group["checkpointEnabled"], group["resumeEvents"],
                ", ".join(group["restoreReasons"]),
                group["visitCount"], "%.3f" % wall["p50"] if wall else "unavailable",
                mean(group["measuredPerRestore"]["actualCopyBytes"]),
                source_bytes(group, "Upload"), source_bytes(group, "Default"),
                group["resourceDetailStatus"]))
    for run in report["runs"]:
        for group in run["groups"]:
            if group["phase"] != "steadyEnabled" or group["force"]:
                continue
            lines += ["", "## %s EID %s, checkpoint enabled" % (Path(run["capture"]).name, group["eventId"]), "",
                      "| Resource | Type | Mean recorded initial copy bytes | Metadata logical bytes | Name |",
                      "|---|---|---:|---:|---|"]
            for row in group["largestCopiedResources"][:10]:
                meta = row["metadata"]
                lines.append("| %s | %s | %s | %s | %s |" % (row["resource"], meta.get("kind", "unavailable"),
                    mean(row["measuredPerRestore"]["actualCopyBytes"]), meta.get("logicalBytes"),
                    str(meta.get("name", "unavailable")).replace("|", "\\|")))
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", action="append", required=True, type=Path)
    parser.add_argument("--inventory", action="append", default=[], type=Path,
                        help="Optional same-capture checkpoint inventory manifests")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()
    if len(args.profile) > 8 or len(args.inventory) > 8 or not 1 <= args.top <= 100:
        parser.error("At most 8 profiles/inventories and 1 <= top <= 100")
    if args.output.exists() or (args.markdown and args.markdown.exists()):
        parser.error("Output paths must be new")
    reader, inventories, inventory_sources = Reader(), {}, []
    for path in args.inventory:
        capture, index, source = load_inventory(path.resolve(), reader)
        if capture in inventories:
            parser.error("Duplicate capture inventories are ambiguous")
        inventories[capture] = index
        inventory_sources.append(source)
    result = {"schema": "renderdoc-restore-attribution/v1", "notes": NOTES, "inventorySources": inventory_sources, "runs": []}
    for path in args.profile:
        path = path.resolve()
        raw = reader.read(path)
        source = json.loads(raw.decode("utf-8-sig"))
        run = analyze_profile(source, path, reader, inventories.get(canonical_capture(source.get("capture", ""))), args.top)
        run["sourceSha256"] = hashlib.sha256(raw).hexdigest()
        result["runs"].append(run)
    result["inputBytesRead"] = reader.bytes_read
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(result), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "runs": len(result["runs"]),
                      "issues": sum(len(run["issues"]) for run in result["runs"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
