"""CPU-only checkpoint profiler analysis; inclusive scopes are never added to their children."""
from __future__ import print_function

import argparse
from collections import defaultdict, deque
import hashlib
import json
from pathlib import Path

from analyze_eid_timing import metric, parse_records, summarize


APIS = ("D3D11", "D3D12", "OpenGL", "Vulkan")
SCOPE_NOTE = (
    "Replay wall time is the SetFrameEvent application-call latency; native readback is separate. "
    "Backend ReplayLog, ReplayChunk, PipelineFetch and checkpoint-copy records are inclusive "
    "diagnostic scopes and are not added together. PipelineFetch may contain an internal replay. "
    "Chunk decode/execute/unclassified partition one chunk; wait/submit are nested subsets. "
    "Backend self sections plus unscoped partition one ReplayLog, not its nested ReplayLogs. "
    "Only explicit replay_depth=0 backend records may be summed; legacy nesting remains unknown. "
    "All times are CPU-observed wall time, not GPU timestamps or scanout; p50/p95 use nearest rank."
)


def stats(values):
    values = [value for value in values if isinstance(value, (int, float))]
    return metric(values) if values else None


def classify_visit(visit):
    phase = visit.get("phase", "unknown")
    context = ("mutation" if phase.startswith("mutation") else "budget" if phase == "tinyBudget"
               else "reenable" if phase == "reenabled" else "normal")
    before, after = visit.get("before", {}), visit.get("after", {})
    hits = after.get("hits", 0) - before.get("hits", 0)
    builds = after.get("builds", 0) - before.get("builds", 0)
    if visit.get("force"):
        category = "forcedReference"
    elif hits > 0 and builds > 0:
        category = "restoreAndBuild"
    elif hits > 0:
        category = "restoreHit"
    elif builds > 0:
        category = "coldCheckpointBuild"
    else:
        category = "canonicalFallbackOrNoReplay"
    return context, category, hits, builds


def sample_records(sample, report_path):
    # Recorder stores exactly the measured offset slice in the JSON. Prefer that immutable
    # evidence; reading a whole live driver log would mix capture loading, readback and other runs.
    if isinstance(sample.get("timingRecords"), list):
        return sample["timingRecords"]
    path = Path(sample.get("timingSlice", ""))
    if not path.is_file():
        path = report_path.parent / path.name
    if path.is_file():
        return parse_records(path.read_text(encoding="utf-8-sig", errors="replace"))
    return []


def record_metrics(records, fields):
    return {field: value for field in fields
            for value in [stats([record.get(field) for record in records])] if value is not None}


def summarize_scope_records(records):
    backend = [record for record in records if record.get("api") in APIS]
    pipeline = [record for record in records if record.get("api") == "PipelineFetch"]
    chunks = [record for record in records if record.get("api") == "ReplayChunk"]
    checkpoint = [record for record in records if str(record.get("api", "")).endswith("Checkpoint")]
    backend_groups, chunk_groups, checkpoint_groups = defaultdict(list), defaultdict(list), defaultdict(list)
    for record in backend:
        backend_groups[(record.get("api"), record.get("type"), record.get("replay_depth", "unknown"))].append(record)
    for record in chunks:
        chunk_groups[(record.get("driver"), record.get("type"), record.get("replay_depth", "unknown"),
                      record.get("chunkId"), record.get("chunk"))].append(record)
    for record in checkpoint:
        checkpoint_groups[(record.get("api"), record.get("op"))].append(record)

    issues = []
    for record in pipeline:
        phases = ("backend_state_ms", "descriptor_access_ms", "descriptor_queries_ms", "publish_ms")
        if all(field in record for field in phases) and abs(sum(record[field] for field in phases) - record.get("total_ms", 0)) > 0.01:
            issues.append("PipelineFetch phase total mismatch at line %s" % record.get("line"))
    for record in chunks:
        if abs(sum(record.get(field, 0) for field in ("decode_ms", "execute_ms", "unclassified_ms"))
               - record.get("total_ms", 0)) > 0.01:
            issues.append("ReplayChunk phase total mismatch at line %s" % record.get("line"))
    for record in checkpoint:
        phases = ()
        if record.get("op") in ("saveCopy", "restoreCopy"):
            phases = ("drain_ms", "record_ms", "submitwait_ms")
        elif record.get("op") == "restore" and "validate_ms" in record:
            phases = ("validate_ms", "resource_restore_ms", "bind_finalize_ms")
        elif record.get("op") == "save" and "resource_prepare_copy_ms" in record:
            phases = ("resource_prepare_copy_ms", "counter_readback_ms", "finalize_ms")
        elif record.get("op") == "save" and "prepare_ms" in record:
            phases = ("prepare_ms", "resource_copy_ms", "bind_finalize_ms")
        if phases and all(field in record for field in phases) and abs(sum(record[field] for field in phases) - record.get("total_ms", 0)) > 0.01:
            issues.append("Checkpoint operation phase total mismatch at line %s" % record.get("line"))

    backend_summary = []
    for key, group in sorted(backend_groups.items(), key=lambda value: str(value[0])):
        measured = summarize(group)
        backend_summary.append({"api": key[0], "type": key[1], "replayDepth": key[2],
                                "recordCount": len(group), "metricsPerCall": measured["metrics"]})
        issues.extend(measured["consistency_issues"])
    chunk_summary = []
    for key, group in chunk_groups.items():
        chunk_summary.append({"driver": key[0], "type": key[1], "replayDepth": key[2],
                              "chunkId": key[3], "chunk": key[4], "recordCount": len(group),
                              "chunkCalls": sum(row.get("calls", 0) for row in group),
                              "metricsPerRecord": record_metrics(group, (
                                  "decode_ms", "execute_ms", "unclassified_ms", "decode_self_ms",
                                  "execute_self_ms", "unclassified_self_ms", "wait_ms", "submit_ms", "total_ms"))})
    chunk_summary.sort(key=lambda row: row["metricsPerRecord"].get("total_ms", {}).get("max", 0), reverse=True)
    return {
        "backend": backend_summary,
        "topLevelBackendSumMs": (sum(row.get("total_ms", 0) for row in backend if row["replay_depth"] == 0)
                                 if backend and all("replay_depth" in row for row in backend) else None),
        "backendNestingKnown": bool(backend) and all("replay_depth" in row for row in backend),
        "pipeline": {"recordCount": len(pipeline), "metricsPerCall": record_metrics(pipeline, (
            "total_ms", "backend_state_ms", "descriptor_access_ms", "descriptor_queries_ms", "publish_ms",
            "accesses", "store_queries"))},
        "chunkTypes": chunk_summary,
        "checkpointOperations": [
            {"api": key[0], "operation": key[1], "recordCount": len(group),
             "metricsPerCall": record_metrics(group, (
                 "total_ms", "drain_ms", "record_ms", "submitwait_ms", "resource_prepare_copy_ms",
                 "counter_readback_ms", "prepare_ms", "resource_copy_ms", "validate_ms",
                 "resource_restore_ms", "bind_finalize_ms", "finalize_ms", "bytes", "allocatedBytes", "sharedBytes"))}
            for key, group in sorted(checkpoint_groups.items(), key=lambda value: str(value[0]))],
        "consistencyIssues": issues,
    }


def analyze_report(report, report_path):
    samples = defaultdict(deque)
    for sample in report.get("samples", []):
        samples[(sample.get("phase"), sample.get("eventId"))].append(sample)
    groups, rows, issues = defaultdict(list), [], []
    for index, visit in enumerate(report.get("visits", [])):
        context, category, hits, builds = classify_visit(visit)
        queue = samples[(visit.get("phase", "") + ".replay", visit.get("eventId"))]
        sample = queue.popleft() if queue else {}
        if not sample:
            issues.append("No measured replay sample for visit %d" % index)
        records = sample_records(sample, report_path)
        row = {"index": index, "phase": visit.get("phase"), "context": context,
               "category": category, "eventId": visit.get("eventId"), "force": bool(visit.get("force")),
               "hits": hits, "builds": builds, "replayWallMs": visit.get("replayWallMs"),
               "readbackWallMs": visit.get("readbackWallMs"), "sampleStatus": sample.get("status"),
               "logStart": sample.get("logStart"), "logEnd": sample.get("logEnd"),
               "timingSlice": sample.get("timingSlice"), "reason": visit.get("after", {}).get("reason"),
               "allocatedBytes": visit.get("after", {}).get("allocatedBytes"),
               "sharedBytes": visit.get("after", {}).get("sharedBytes"),
               "checkpointCount": visit.get("after", {}).get("checkpointCount"),
               "savedEvents": visit.get("after", {}).get("events"),
               "scopes": summarize_scope_records(records)}
        rows.append(row)
        groups[(context, category, visit.get("eventId"))].append((row, records))

    summaries = []
    for (context, category, event), values in sorted(groups.items(), key=lambda value: str(value[0])):
        summaries.append({"context": context, "category": category, "eventId": event, "visitCount": len(values),
                          "replayWallMs": stats([row["replayWallMs"] for row, _ in values]),
                          "readbackWallMs": stats([row["readbackWallMs"] for row, _ in values]),
                          "hits": sum(row["hits"] for row, _ in values),
                          "builds": sum(row["builds"] for row, _ in values),
                          "scopes": summarize_scope_records([record for _, records in values for record in records])})
    mutation = report.get("mutations", {})
    mutation_checks = []
    for check in mutation.get("checks", []):
        matching = [row for row in rows if row["phase"] == check.get("phase") and not row["force"]
                    and row["eventId"] == mutation.get("eventId")]
        mutation_checks.append(dict(check, measuredTargetCalls=len(matching),
                                    targetHitDeltaFromMeasuredVisits=sum(row["hits"] for row in matching)))
    forced_changes = []
    for index, visit in enumerate(report.get("visits", [])):
        if visit.get("force"):
            changed = [key for key in ("hits", "builds", "checkpointCount", "allocatedBytes", "sharedBytes", "events")
                       if visit.get("before", {}).get(key) != visit.get("after", {}).get(key)]
            if changed:
                forced_changes.append({"visitIndex": index, "phase": visit.get("phase"), "changed": changed})

    def mutation_pixels(phase):
        return [visit.get("witness", {}).get("pixels") for visit in report.get("visits", [])
                if visit.get("phase") == phase and visit.get("force")]

    original = mutation_pixels("mutation.original")
    replacement = mutation_pixels("mutation.shaderReplaced.canonical")
    restored = mutation_pixels("mutation.shaderRestored.canonical")
    # Legacy profilers sometimes kept only one mutation-original reference. Missing evidence is
    # unknown, not an assertion that two native images differed.
    stable = lambda values: values[0] == values[1] if len(values) == 2 and all(value is not None for value in values) else None
    return {"path": str(report_path.resolve()), "api": report.get("captureDriver"),
            "capture": report.get("capture"), "buildLabel": report.get("buildLabel"), "events": report.get("events"),
            "ok": report.get("ok", False), "measurementComplete": report.get("measurementComplete", False),
            "verdict": report.get("verdict"), "error": report.get("error"),
            "observedHits": report.get("observedHits"), "eventComparisons": report.get("eventComparisons", []),
            "selectedEventEvidence": report.get("selectedEventEvidence", []),
            "profilerIssues": report.get("issues", []), "cleanupErrors": report.get("cleanupErrors", []),
            "configuration": report.get("configuration"),
            "checkpointPhaseStatus": report.get("checkpointPhaseStatus", {}),
            "lifecycleStatus": {key: report[key] for key in (
                "initialStatus", "initialDisable", "enabledStatus", "disabledStatus", "reenabledStatus",
                "reenabledFinalStatus", "tinyBudgetInitialStatus", "tinyBudgetFinalStatus",
                "finalDisabledStatus", "cleanupDisabledStatus") if key in report},
            "mutations": mutation, "mutationChecksWithMeasuredTargetHits": mutation_checks,
            "mutationPixels": {"originalForcedStable": stable(original),
                               "originalForcedSampleCount": len(original),
                               "replacementForcedStable": stable(replacement),
                               "replacementForcedSampleCount": len(replacement),
                               "restoredForcedStable": stable(restored),
                               "restoredForcedSampleCount": len(restored),
                               "visibleReplacementChange": original[0] != replacement[0]
                               if stable(original) and stable(replacement) else None,
                               "restoredOriginalPixels": original[0] == restored[0]
                               if stable(original) and stable(restored) else None},
            "forcedReplayCacheChanges": forced_changes,
            "analysisIssues": issues, "groups": summaries, "visits": rows,
            "unmatchedOrNonVisitSamples": [
                {"phase": sample.get("phase"), "eventId": sample.get("eventId"), "status": sample.get("status"),
                 "wallMs": sample.get("wallMs"), "timingSlice": sample.get("timingSlice")}
                for remaining in samples.values() for sample in remaining]}


def fmt(value):
    return "—" if value is None else "%.3f" % value


def markdown(report):
    lines = ["# Replay checkpoint measurement findings", "", SCOPE_NOTE, "",
             "ColdCheckpointBuild means a new checkpoint allocation/copy, not cold capture opening. "
             "RestoreAndBuild stays separate from a pure restore hit. Mutation and tiny-budget "
             "validation and disable/re-enable lifecycle checks are excluded from the ordinary latency table. "
             "Its hits correspond to the initial forward/reverse/forward-again phase. Each EID is reported separately; "
             "small fixture sample counts do not establish real-game responsiveness.", ""]
    if report.get("apisWithoutSelectedEvidence"):
        lines.extend(["APIs without selected evidence: `%s`. No result is inferred for these APIs." %
                      ", ".join(report["apisWithoutSelectedEvidence"]), ""])
    lines.extend(["| API | Selected EIDs | Saved checkpoints | Ordinary hits | Retained bytes | Shared bytes | Acceptance |",
                  "|---|---|---|---:|---:|---:|---|"])
    for run in report["runs"]:
        checkpoint = run["checkpointPhaseStatus"]
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            run["api"], run["events"], checkpoint.get("events", []), run["observedHits"],
            checkpoint.get("allocatedBytes", "—"), checkpoint.get("sharedBytes", "—"), run["verdict"]))
    lines.extend(["", "Retained/shared sizes are the backend's checkpoint status at the end of ordinary visits. "
                  "They are not total process VRAM, complete capture sizes, or comparable cross-API workloads. "
                  "The backend-defined shared field can include retained static resource contents or reused versions; "
                  "zero does not imply that every resource was copied. All forced visits' checkpoint counters/storage "
                  "are independently compared before and after in the companion JSON.", ""])
    for run in report["runs"]:
        lines.extend(["## %s — %s" % (run["api"], Path(run["path"]).name), "",
                      "Source: [%s](%s). Build: `%s`. Acceptance: `%s`, complete=%s, hits=%s." % (
                          Path(run["path"]).name, run["path"].replace("\\", "/"), run["buildLabel"],
                          run["verdict"] or "incomplete", run["measurementComplete"], run["observedHits"]), ""])
        if run["error"] or run["profilerIssues"] or run["cleanupErrors"]:
            lines.extend(["Failure/limitation: `%s`" % (run["error"] or run["profilerIssues"] or run["cleanupErrors"]), ""])
        if run["mutationChecksWithMeasuredTargetHits"]:
            lines.extend(["Mutation target-hit counts below are recomputed from the measured target visits' "
                          "before/after counters; away-navigation hits are not included.", "",
                          "| Mutation | Verdict | Target calls | Measured target hits | Reported navigation hits |",
                          "|---|---|---:|---:|---:|"])
            for check in run["mutationChecksWithMeasuredTargetHits"]:
                lines.append("| %s | %s | %s | %s | %s |" % (
                    check["phase"], check.get("status"), check["measuredTargetCalls"],
                    check["targetHitDeltaFromMeasuredVisits"], check.get("navigationHits", "not separately recorded")))
            pixels = run["mutationPixels"]
            lines.extend(["", "Native forced-reference pixels: original stable=%s; replacement stable=%s; "
                          "visible replacement change=%s; restored stable=%s; restored original=%s. "
                          "Forced replay cache-change violations=%s." % (
                              pixels["originalForcedStable"], pixels["replacementForcedStable"],
                              pixels["visibleReplacementChange"], pixels["restoredForcedStable"],
                              pixels["restoredOriginalPixels"], len(run["forcedReplayCacheChanges"])), ""])
            if any(pixels[key] is None for key in ("originalForcedStable", "replacementForcedStable", "restoredForcedStable")):
                lines.extend(["`None` means fewer than two recorded native pixel references or missing witness data; "
                              "it does not mean unstable pixels. Original/replaced/restored reference counts: %s/%s/%s." % (
                                  pixels["originalForcedSampleCount"], pixels["replacementForcedSampleCount"],
                                  pixels["restoredForcedSampleCount"]), ""])
        lines.extend(["| EID | Path | n | Replay p50 ms | Replay p95 ms | Readback p50 ms | Builds | Hits |",
                      "|---:|---|---:|---:|---:|---:|---:|---:|"])
        for group in run["groups"]:
            if group["context"] != "normal":
                continue
            wall, readback = group["replayWallMs"] or {}, group["readbackWallMs"] or {}
            lines.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
                group["eventId"], group["category"], group["visitCount"], fmt(wall.get("p50")),
                fmt(wall.get("p95")), fmt(readback.get("p50")), group["builds"], group["hits"]))
        lines.extend(["", "Backend means below are per logged call, grouped by replay type and nesting depth. "
                      "Unknown depth records are not summed. Decode/execute self exclude timed children; "
                      "wait is an inclusive diagnostic column and must not be added to phase totals.", "",
                      "| EID/path | Replay type/depth | n | Total mean ms | Decode self ms | Execute self ms | Explicit wait ms |",
                      "|---|---|---:|---:|---:|---:|---:|"])
        for group in run["groups"]:
            if group["context"] != "normal":
                continue
            for backend in group["scopes"]["backend"]:
                metrics = backend["metricsPerCall"]
                mean = lambda field: fmt(metrics.get(field, {}).get("mean"))
                lines.append("| %s/%s | %s/%s | %s | %s | %s | %s | %s |" % (
                    group["eventId"], group["category"], backend["type"], backend["replayDepth"],
                    backend["recordCount"], mean("total_ms"), mean("section.chunkDecode_self_ms"),
                    mean("section.chunkExecute_self_ms"), mean("explicitWait_ms")))
        lines.extend(["", "Pipeline publication is a separate inclusive scope; it can contain backend replay. "
                      "These columns partition PipelineFetch only.", "",
                      "| EID/path | n | State mean ms | Access mean ms | Descriptor queries mean ms | Publish mean ms | Total mean ms |",
                      "|---|---:|---:|---:|---:|---:|---:|"])
        for group in run["groups"]:
            if group["context"] != "normal":
                continue
            pipeline = group["scopes"]["pipeline"]
            metrics = pipeline["metricsPerCall"]
            if not pipeline["recordCount"]:
                continue
            mean = lambda field: fmt(metrics.get(field, {}).get("mean"))
            lines.append("| %s/%s | %s | %s | %s | %s | %s | %s |" % (
                group["eventId"], group["category"], pipeline["recordCount"], mean("backend_state_ms"),
                mean("descriptor_access_ms"), mean("descriptor_queries_ms"), mean("publish_ms"), mean("total_ms")))
        operations = [(group, operation) for group in run["groups"] if group["context"] == "normal"
                      for operation in group["scopes"]["checkpointOperations"]
                      if "total_ms" in operation["metricsPerCall"]]
        if operations:
            lines.extend(["", "Checkpoint scopes below are also inclusive: saveCopy/restoreCopy are children "
                          "of Vulkan save/restore and must not be added to those parents. D3D11/GL save "
                          "rows describe successful attempts; rejected budget retries remain in replay wall time. "
                          "Recorded copy time includes CPU API calls and implicit waits, not GPU timestamps.", "",
                          "| EID/path | Operation | n | Total mean ms | Phase means ms | Bytes |",
                          "|---|---|---:|---:|---|---|"])
            for group, operation in operations:
                metrics = operation["metricsPerCall"]
                phases = "; ".join("%s=%s" % (field[:-3], fmt(value["mean"]))
                                   for field, value in metrics.items() if field.endswith("_ms") and field != "total_ms")
                storage = "; ".join("%s=%.0f" % (field, metrics[field]["mean"])
                                    for field in ("bytes", "allocatedBytes", "sharedBytes") if field in metrics)
                lines.append("| %s/%s | %s | %s | %s | %s | %s |" % (
                    group["eventId"], group["category"], operation["operation"], operation["recordCount"],
                    fmt(metrics["total_ms"]["mean"]), phases or "—", storage or "—"))
        else:
            lines.extend(["", "No checkpoint detail operation rows are present in this run's measured log slices. "
                          "Save/restore subphase costs are unavailable; they are not inferred from replay residuals."])
        lines.extend(["", "Complete chunk-type diagnostics, checkpoint-copy operations, matched sample offsets, "
                      "mutation/budget tests and acceptance comparisons are retained in the companion JSON.", ""])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="Profiler JSON files or directories (direct children only)")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--latest-per-capture-events", action="store_true",
                        help="Keep the newest report for the same API/capture/selected-EID tuple; failed reports are not hidden")
    args = parser.parse_args()
    paths = sorted(set(path for item in args.paths for path in (item.glob("*.json") if item.is_dir() else [item])))
    selected, skipped = [], []
    for path in paths:
        try:
            raw = path.read_bytes()
            source = json.loads(raw.decode("utf-8-sig"))
            if source.get("schema") != "renderdoc-checkpoint-profile/v1":
                continue
            selected.append((path, source, hashlib.sha256(raw).hexdigest()))
        except (OSError, ValueError, AttributeError) as error:
            skipped.append({"path": str(path), "reason": str(error)})
    if args.latest_per_capture_events:
        latest = {}
        for row in selected:
            key = (row[1].get("captureDriver"), row[1].get("capture"), tuple(row[1].get("events", [])))
            if key not in latest or row[0].stat().st_mtime_ns > latest[key][0].stat().st_mtime_ns:
                latest[key] = row
        selected = sorted(latest.values(), key=lambda row: str(row[0]))
    report = {"schema": "renderdoc-checkpoint-analysis/v1", "meaning": SCOPE_NOTE,
              "sourceSelection": "latest-per-capture-events" if args.latest_per_capture_events else "all-explicit-profiler-inputs",
              "skippedUnreadableInputs": skipped, "runs": []}
    selected.sort(key=lambda row: (APIS.index(row[1].get("captureDriver"))
                                  if row[1].get("captureDriver") in APIS else len(APIS), str(row[0])))
    for path, source, digest in selected:
        run = analyze_report(source, path)
        run["sourceSha256"] = digest
        report["runs"].append(run)
    report["apisWithoutSelectedEvidence"] = [api for api in APIS if not any(run["api"] == api for run in report["runs"])]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"runs": len(report["runs"]), "output": str(args.output),
                      "incompleteOrFailed": [Path(run["path"]).name for run in report["runs"] if not run["ok"]]}))
    # A failing native validation is evidence to report, not an analyzer failure.
    return 0 if report["runs"] and not skipped else 1


if __name__ == "__main__":
    sys_exit = main()
    raise SystemExit(sys_exit)
