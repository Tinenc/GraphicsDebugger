"""Native D3D12_Checkpoint_Query acceptance in a fresh matching RenderDoc Python process.

Main opens the explicitly supplied capture and uses the GPU. Coordinate execution separately.
Fixture markers and exact API events discover EIDs; no captured resource/EID is hardcoded.
"""
from __future__ import print_function

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import struct
import sys
import traceback

from discover_d3d12_checkpoint_fixture import collect_actions, select_fixture_events
from eid_checkpoint_profile import compare_event, parse_status, status_issues
from eid_shared_buffer_profile import (SharedBufferRecorder, binary_identity, build_identity,
                                       output_witness, unchanged_checkpoint_issues)


RESULT_NAME = "QueryCheckpointResults"
PREFIX = "Query checkpoint "
SENTINELS = {0: 0x1122334455667788, 8: 0x8877665544332211,
             32: 0x1020304050607080, 40: 0x8070605040302010,
             48: 0xabcdef0123456789, 56: 0x9876543210fedcba}


def query_api_events(controller):
    structured = controller.GetStructuredFile()
    found = {}
    def walk(actions):
        for action in actions:
            for event in action.events:
                index = int(event.chunkIndex)
                if not 0 <= index < len(structured.chunks):
                    raise ValueError("Fixture API event has an invalid chunk index")
                chunk = structured.chunks[index]
                if str(chunk.name).endswith(("BeginQuery", "EndQuery", "ResolveQueryData")):
                    row = {"eventId": int(event.eventId), "chunkIndex": index,
                           "chunkName": str(chunk.name), "fileOffset": int(event.fileOffset)}
                    if row["eventId"] in found and found[row["eventId"]] != row:
                        raise ValueError("Conflicting query API event identity")
                    found[row["eventId"]] = row
            walk(action.children)
    walk(controller.GetRootActions())
    return [found[event] for event in sorted(found)]


def discover_query_fixture(actions, api_events):
    selected = select_fixture_events(actions)
    groups = [group for group in selected["submissionGroups"] if group["draws"]]
    if len(groups) != 4:
        raise ValueError("Query fixture must have four completed single-draw submissions")
    variants = {name: sum(row["name"] == PREFIX + "variant " + name for row in actions)
                for name in ("mixed-prefix-suffix", "repeated-submit")}
    if sorted(variants.values()) != [0, 1]:
        raise ValueError("Expected exactly one recognized query fixture variant marker")
    variant = next(name for name, count in variants.items() if count)
    repeated = variant == "repeated-submit"
    required = [["prefix slot 0 = 1"], ["gap draw"],
                ["repeated mixed resolve" if repeated else "mixed resolve expected 1,0",
                 "inspect resolved buffer"],
                ["repeated mixed resolve", "inspect resolved buffer"] if repeated else
                ["overwrite native slot 0 = 0", "final draw expected 1,0"]]
    targets = []
    for index, group in enumerate(groups):
        begin, end = group["begin"]["eventId"], group["end"]["eventId"]
        draw = group["draws"][0]
        markers = [row for row in actions if begin < row["eventId"] < end and row["name"].startswith(PREFIX)]
        for name in required[index]:
            matches = [row for row in markers if row["name"] == PREFIX + name and row["eventId"] < draw["eventId"]]
            if len(matches) != 1:
                raise ValueError("Missing/ambiguous preceding fixture marker: " + name)
        resolves = [row for row in api_events if begin < row["eventId"] < draw["eventId"]
                    and row["chunkName"].endswith("ResolveQueryData")]
        if len(resolves) != (1 if index == 2 or (repeated and index == 3) else 0):
            raise ValueError("ResolveQueryData API evidence does not match the marker variant")
        targets.append({"eventId": draw["eventId"], "role": ("prefix", "gap", "inspect", "final")[index],
                        "expectedQuery": ([0, 0] if repeated and index == 3 else [1, 0]) if index >= 2 else None,
                        "drawAPI": draw["exactAPI"], "markers": markers, "resolveAPI": resolves,
                        "submissionBegin": begin, "submissionEnd": end})
    if repeated:
        # One unchanged command list executed twice must retain its serialized Draw identity.
        a, b = targets[2]["drawAPI"], targets[3]["drawAPI"]
        if (a.get("chunkIndex"), a.get("fileOffset")) != (b.get("chunkIndex"), b.get("fileOffset")):
            raise ValueError("Repeated-submit markers do not reference the same recorded draw chunk")
    return {"variant": variant, "targets": targets,
            "firstResolveEvent": targets[2]["resolveAPI"][0]["eventId"],
            "meaning": "Expected post-resolve UINT64 values come from the matching fixture contract. "
                       "Pre-resolve values can contain the previous captured frame and are only compared to force."}


def result_witness(raw, expected):
    raw = bytes(raw)
    if len(raw) != 64:
        raise ValueError("QueryCheckpointResults must return its complete 64-byte buffer")
    values = list(struct.unpack_from("<QQ", raw, 16))
    neighbors = {str(offset): struct.unpack_from("<Q", raw, offset)[0] for offset in SENTINELS}
    issues = []
    if any(neighbors[str(offset)] != value for offset, value in SENTINELS.items()):
        issues.append("query resolve modified a sentinel outside [16,32)")
    if expected is not None and values != list(expected):
        issues.append("native UINT64 query results differ from the expected per-EID values")
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "hex": raw.hex(),
            "offsets": [16, 24], "queryValues": values, "sentinelValues": neighbors,
            "expectedQuery": expected, "issues": issues}


def coverage_verdict(variant, visits, minimum_hits):
    targets = [v for v in visits if v["mode"] == "enabled" and not v["force"] and v["expectedQuery"] is not None]
    hits = sum(v["hits"] for v in targets)
    query_hits = sum(v["queryRestoreProven"] for v in targets)
    fallback = [v for v in targets if not v["hits"] and v["queryFallbackEvidence"]]
    issues = []
    if hits < minimum_hits:
        issues.append("required post-resolve checkpoint hits were not observed")
    if variant == "mixed-prefix-suffix" and minimum_hits and not query_hits:
        issues.append("no saved query-slot restore before the mixed Resolve was evidenced")
    if variant == "repeated-submit" and not hits and not fallback:
        issues.append("zero repeated-submit hits without an explicit query-safety fallback")
    return {"postResolveHits": hits, "querySlotRestoreVisits": query_hits,
            "explicitQueryFallbackVisits": len(fallback), "issues": issues,
            "coverage": "querySlotRestore" if query_hits else "checkpointRestore" if hits else
                        "explicitQuerySafetyFallback" if fallback else "noCheckpointCoverage"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expect-variant", choices=("auto", "mixed-prefix-suffix", "repeated-submit"), default="auto")
    parser.add_argument("--rounds", type=int, default=2)
    parser.add_argument("--min-hits", type=int, default=None,
                        help="Default: mixed requires one post-resolve hit; repeated permits explicit query fallback")
    parser.add_argument("--budget-mib", type=int, default=0)
    parser.add_argument("--max-checkpoints", type=int, default=0)
    parser.add_argument("--max-read-mib", type=int, default=64)
    parser.add_argument("--max-seconds", type=int, default=300)
    parser.add_argument("--build-label", default="unspecified")
    args = parser.parse_args(argv)
    capture, output = Path(args.capture).resolve(), Path(args.output).resolve()
    if not capture.is_file() or any(path.exists() for path in
            (output, output.with_suffix(".native.log"), output.with_suffix(".rdoc.log"))):
        parser.error("Use an existing explicit capture and a new output evidence stem")
    if (not 1 <= args.rounds <= 4 or args.budget_mib < 0 or not 0 <= args.max_checkpoints <= 64 or
            args.max_read_mib <= 0 or args.max_seconds <= 0 or (args.min_hits is not None and args.min_hits < 0)):
        parser.error("Invalid bounded sample/resource limits")
    output.parent.mkdir(parents=True, exist_ok=True)
    environment = {"RENDERDOC_EID_TIMING": "1", "RENDERDOC_D3D12_ENABLE_PREPARED_REPLAY": "0",
                   "RENDERDOC_D3D12_DISABLE_PREPARED_REPLAY": "1"}
    os.environ.update(environment)
    report = {"schema": "renderdoc-query-checkpoint-profile/v1", "ok": False, "capture": str(capture),
              "configuration": vars(args), "environment": environment, "samples": [], "visits": [],
              "comparisons": [], "issues": [], "processId": os.getpid(),
              "limits": ["Fresh standalone native replay; no capture mutation, GUI or scanout measurement",
                         "Replay wall excludes separately measured 64-byte Buffer and all bound RT/depth readbacks",
                         "UINT64 results and all six sentinel words are exact; color/depth native bytes use SHA256",
                         "Force bypasses checkpoint save/restore; zero reuse is not reported as acceleration"]}
    recorder = SharedBufferRecorder(report, output, output.with_suffix(".rdoc.log"), args.max_seconds)
    recorder.publish()
    rd, cap, controller, initialised = None, None, None, False

    def status():
        value = parse_status(controller.GetReplayCheckpointStatus())
        report["issues"].extend(status_issues(value))
        return value

    def configure(enabled):
        controller.ConfigureReplayCheckpoints(enabled, args.budget_mib * 1024 * 1024, args.max_checkpoints)
        result = status()
        report["issues"].extend(status_issues(result, disabled=not enabled))
        return result

    def visit(mode, phase, target, force=False):
        event = target["eventId"]
        before = status()
        _, timing = recorder.measure(mode + "." + phase + ".replay",
                                     lambda: controller.SetFrameEvent(event, force), event)
        after = status()
        raw, byte_timing = recorder.measure(mode + "." + phase + ".queryReadback",
                    lambda: bytes(controller.GetBufferData(resource, 0, 64)), event)
        query = result_witness(raw, target["expectedQuery"])
        pixels, pixel_timing = recorder.measure(mode + "." + phase + ".outputReadback",
                    lambda: output_witness(controller, rd, args.max_read_mib * 1024 * 1024), event)
        if pixels["eventId"] != event:
            raise RuntimeError("Native current EID differs from the discovered target")
        readback_status = status()
        report["issues"].extend(query["issues"])
        report["issues"].extend("readback: " + issue for issue in unchanged_checkpoint_issues(after, readback_status))
        if force or mode == "disabled":
            report["issues"].extend("force/disabled: " + issue for issue in unchanged_checkpoint_issues(before, after))
        hits, builds = after["hits"] - before["hits"], after["builds"] - before["builds"]
        if min(hits, builds) < 0:
            raise RuntimeError("Checkpoint counters decreased during a replay")
        records = timing.get("timingRecords", [])
        restored = [r for r in records if r.get("api") == "D3D12Checkpoint" and r.get("op") == "restore"]
        query_proven = bool(hits and any(r.get("restoredQuerySlots", 0) > 0 and
                    0 < r.get("event", 0) < report["discovery"]["firstResolveEvent"] for r in restored))
        reason = str(after.get("reason", ""))
        fallback = [r for r in records if r.get("api") == "D3D12Checkpoint" and r.get("op") == "queryFallback"]
        if not hits and "query" in reason.lower():
            fallback.append({"source": "checkpointStatus.reason", "reason": reason})
        row = {"mode": mode, "phase": phase, "eventId": event, "role": target["role"], "force": force,
               "expectedQuery": target["expectedQuery"], "before": before, "after": after,
               "afterReadback": readback_status, "hits": hits, "builds": builds,
               "classification": "force" if force else "build" if builds else "hit" if hits else "fallback",
               "queryRestoreProven": query_proven, "queryRestoreRecords": restored,
               "queryFallbackEvidence": fallback, "replayWallMs": timing["wallMs"],
               "queryReadbackWallMs": byte_timing["wallMs"], "outputReadbackWallMs": pixel_timing["wallMs"],
               "witness": {"query": query, "output": pixels}}
        report["visits"].append(row)
        recorder.publish()
        return row["witness"]

    try:
        import renderdoc as rd
        report["buildIdentity"] = build_identity(rd, None)
        report["captureIdentity"] = binary_identity(capture)
        rd.InitialiseReplay(rd.GlobalEnvironment(), [])
        initialised = True
        rd.SetDebugLogFile(str(recorder.log_path))
        cap = rd.OpenCaptureFile()
        result, _ = recorder.measure("capture.openFile", lambda: cap.OpenFile(str(capture), "", None))
        if result != rd.ResultCode.Succeeded or str(cap.DriverName()) != "D3D12":
            raise RuntimeError("Explicit capture must open as an actual D3D12 capture")
        opened, _ = recorder.measure("capture.openReplay", lambda: cap.OpenCapture(rd.ReplayOptions(), None))
        result, controller = opened
        if result != rd.ResultCode.Succeeded or controller is None:
            raise RuntimeError("OpenCapture failed: " + str(result))
        report["initialDisabledStatus"] = configure(False)
        actions, submits = collect_actions(controller, rd)
        report["discovery"] = discover_query_fixture(actions, query_api_events(controller))
        report["executeCommandListsChunks"] = submits
        if len(submits) != 4:
            raise RuntimeError("Query fixture needs four actual serialized queue submissions")
        variant = report["discovery"]["variant"]
        if args.expect_variant != "auto" and args.expect_variant != variant:
            raise RuntimeError("Capture markers disagree with --expect-variant")
        named = [r for r in controller.GetResources() if str(r.name) == RESULT_NAME]
        if len(named) != 1:
            raise RuntimeError("Expected exactly one named QueryCheckpointResults resource")
        resource = named[0].resourceId
        buffers = [b for b in controller.GetBuffers() if b.resourceId == resource]
        if len(buffers) != 1 or int(buffers[0].length) != 64:
            raise RuntimeError("Named QueryCheckpointResults is not an actual 64-byte Buffer")
        report["resultBuffer"] = {"resource": str(resource), "name": RESULT_NAME, "length": 64}
        targets = report["discovery"]["targets"]
        references = {}
        # Reference before ordinary replays also proves that the captured variant actually
        # produces 1,0 -> 0,0 for repeated submits, independent of checkpoint implementation.
        for target in targets:
            references[target["eventId"]] = [visit("disabled", "canonicalBefore", target, True) for _ in range(2)]
        for mode, enabled in (("disabled", False), ("enabled", True)):
            report[mode + "Status"] = configure(enabled)
            for round_index in range(args.rounds):
                sequence = targets + targets[-2::-1]
                for target in sequence:
                    visit(mode, "round%d" % round_index, target)
            for target in targets:
                canonical = [visit(mode, "canonicalAfter", target, True) for _ in range(2)]
                observed = [v["witness"] for v in report["visits"] if v["mode"] == mode and
                            not v["force"] and v["eventId"] == target["eventId"]]
                observed.extend(references[target["eventId"]])
                verdict = compare_event(observed, canonical)
                report["comparisons"].append({"mode": mode, "eventId": target["eventId"], "status": verdict})
                if verdict != "passed":
                    report["issues"].append(mode + "/EID" + str(target["eventId"]) + ": " + verdict)
            report[mode + "FinalStatus"] = status()
        minimum = args.min_hits if args.min_hits is not None else (1 if variant == "mixed-prefix-suffix" else 0)
        report["coverage"] = coverage_verdict(variant, report["visits"], minimum)
        report["issues"].extend(report["coverage"]["issues"])
        report["finalDisabledStatus"] = configure(False)
        report["timingSummary"] = []
        for mode in ("disabled", "enabled"):
            for target in targets:
                for classification in ("build", "hit", "fallback", "force"):
                    rows = [v for v in report["visits"] if v["mode"] == mode and v["eventId"] == target["eventId"]
                            and v["classification"] == classification]
                    if rows:
                        report["timingSummary"].append({"mode": mode, "eventId": target["eventId"],
                            "classification": classification, "count": len(rows), **{
                                field: {"median": statistics.median(v[field] for v in rows),
                                        "min": min(v[field] for v in rows), "max": max(v[field] for v in rows)}
                                for field in ("replayWallMs", "queryReadbackWallMs", "outputReadbackWallMs")}})
        report["measurementComplete"] = True
        report["ok"] = not report["issues"]
        report["verdict"] = ("passedNativeQueryAndOutputParity_" + report["coverage"]["coverage"]
                             if report["ok"] else "failedOrInconclusive")
    except Exception as error:
        report.update(ok=False, error=str(error), traceback=traceback.format_exc(), verdict="failedOrInconclusive")
    finally:
        def cleanup(label, callback):
            try:
                return recorder.measure(label, callback, cleanup=True)[0]
            except Exception as error:
                report.setdefault("cleanupErrors", []).append({"phase": label, "error": str(error)})
                report.update(ok=False, verdict="cleanupFailed")
        if controller is not None:
            cleanup("cleanup.disable", lambda: configure(False))
        def preserve():
            if recorder.log_path.is_file():
                path = output.with_suffix(".native.log")
                path.write_bytes(recorder.log_path.read_bytes())
                report["nativeLog"] = str(path)
        cleanup("cleanup.preserveLog", preserve)
        if controller is not None:
            cleanup("cleanup.controller", controller.Shutdown)
        if cap is not None:
            cleanup("cleanup.capture", cap.Shutdown)
        cleanup("cleanup.refreshLog", preserve)
        if initialised:
            cleanup("cleanup.runtime", rd.ShutdownReplay)
        recorder.publish()
    print(json.dumps({"ok": report["ok"], "output": str(output), "verdict": report.get("verdict")}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
