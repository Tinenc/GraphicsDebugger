"""Bounded native validation of current-state D3D12 forward continuation.

Main opens the explicitly named capture and uses the GPU. Coordinate GPU ownership first.
All requested textures, including outputs of skipped-over draws that are no longer bound,
are read immediately after each visit. Independent forced references are collected last.
"""
from __future__ import print_function

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback

from eid_checkpoint_profile import parse_status, selected_event_evidence, status_issues
from eid_history_profile import Recorder, integer_list, resource_number
from eid_shared_buffer_profile import binary_identity, build_identity


def parameters(events, resources):
    events, resources = integer_list(events), integer_list(resources)
    if len(events) != 4 or events != sorted(events):
        raise ValueError("Provide exactly four distinct ascending actual draw EIDs")
    if not 1 <= len(resources) <= 16:
        raise ValueError("Provide one to sixteen distinct positive texture IDs")
    return events, resources


def sequence(events):
    a, b, c, d = events
    return [("seedA", a, False, False), ("skipTwoDrawsAD", d, False, True),
            ("backwardDA", a, False, False), ("adjacentDrawsAB", b, False, True),
            ("skipOneDrawBD", d, False, True), ("forceA", a, True, False),
            ("afterForceAD", d, False, False), ("recoveryA", a, False, False),
            ("recoveryAD", d, False, True)]


def live_evidence(records, previous, target, require_hit, force=False):
    """Only exact native request records establish a hit; absent logs prove nothing."""
    live = [r for r in records if r.get("api") == "D3D12LiveForward"]
    issues = []
    if not live:
        issues.append("Missing native D3D12LiveForward request evidence")
    if len(live) > 1:
        issues.append("More than one native live-forward root request in one measured setter")
    for row in live:
        if row.get("target") != target:
            issues.append("Native live-forward target differs from the requested EID")
        if row.get("hit") not in (0, 1) or type(row.get("hit")) is not int:
            issues.append("Native live-forward hit flag is invalid")
        if row.get("hit") == 1:
            if force or not require_hit:
                issues.append("Forced/backward/fallback control unexpectedly used live forward")
            if row.get("from") != previous:
                issues.append("Native hit came from a different materialized source EID")
            if row.get("enabled") != 1 or row.get("initialRestoreSkipped") != 1:
                issues.append("Native hit did not prove initial restoration was skipped")
            if target <= previous or row.get("replayedEvents") != target - previous - 1:
                issues.append("Native hit did not replay the exact intervening API-event range")
        elif row.get("initialRestoreSkipped") != 0 or row.get("replayedEvents") != 0:
            issues.append("Native fallback claims skipped initialization or incremental events")
    matched = [r for r in live if r.get("hit") == 1 and r.get("from") == previous
               and r.get("target") == target]
    if require_hit and not matched:
        issues.append("Required native forward-continuation hit was absent")
    return {"records": live, "matchingHits": len(matched), "issues": issues}


def texture_requests(textures, numbers, maximum):
    selected, requests, estimated = [], [], 0
    for number in numbers:
        matches = [t for t in textures if resource_number(t.resourceId) == number]
        if len(matches) != 1:
            raise ValueError("Requested texture ID is not unique in native metadata: %d" % number)
        texture = matches[0]
        dims = [int(getattr(texture, name)) for name in
                ("width", "height", "depth", "mips", "arraysize", "msSamp")]
        if any(n < 1 for n in dims[:5]) or dims[5] < 0 or dims[3] > 32:
            raise ValueError("Invalid native texture dimensions")
        selected.append({"resource": str(texture.resourceId), "resourceId": number,
                         "width": dims[0], "height": dims[1], "depth": dims[2],
                         "mips": dims[3], "arraysize": dims[4], "samples": dims[5],
                         "format": texture.format.Name()})
        for mip in range(dims[3]):
            size = max(1, dims[0] >> mip) * max(1, dims[1] >> mip) * max(1, dims[2] >> mip) * 16
            for layer in range(1 if dims[2] > 1 else dims[4]):
                for sample in range(max(1, dims[5])):
                    estimated += size
                    if estimated > maximum:
                        raise ValueError("Full requested texture readback exceeds max-read-mib")
                    requests.append((texture.resourceId, (mip, layer, sample)))
    return selected, requests, estimated


def texture_witness(controller, rd, requests, numbers, maximum):
    pipe = controller.GetPipelineState()
    witness = {"eventId": int(controller.GetCurrentEventID()), "bindings": [], "shaders": {},
               "topology": str(pipe.GetPrimitiveTopology()), "pixels": []}
    for name in ("Vertex", "Hull", "Domain", "Geometry", "Pixel", "Compute", "Task", "Mesh"):
        stage = getattr(rd.ShaderStage, name, None)
        if stage is not None:
            witness["shaders"][name] = str(pipe.GetShader(stage))
    targets = [("color", slot, binding) for slot, binding in enumerate(pipe.GetOutputTargets())]
    targets.append(("depth", 0, pipe.GetDepthTarget()))
    for kind, slot, binding in targets:
        number = resource_number(binding.resource)
        if number is None or (number and number not in numbers):
            raise RuntimeError("Bound output is outside the explicit texture coverage")
        witness["bindings"].append({"kind": kind, "slot": slot, "resource": str(binding.resource),
            "firstMip": int(binding.firstMip), "numMips": int(binding.numMips),
            "firstSlice": int(binding.firstSlice), "numSlices": int(binding.numSlices),
            "format": binding.format.Name()})
    total = 0
    for resource, subresource in requests:
        raw = bytes(controller.GetTextureData(resource, rd.Subresource(*subresource)))
        total += len(raw)
        if not raw or total > maximum:
            raise RuntimeError("Empty or over-budget native texture readback")
        witness["pixels"].append({"resource": str(resource), "subresource": list(subresource),
            "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        del raw
    if controller.GetFatalErrorStatus().code != rd.ResultCode.Succeeded:
        raise RuntimeError("Native replay reported a fatal error")
    return witness


def comparisons(visits, references, events):
    issues, rows = [], []
    for event in events:
        canonical = references.get(event, [])
        observed = [row["witness"] for row in visits if row["eventId"] == event]
        if len(canonical) != 2:
            verdict = "incompleteReferences"
        elif canonical[0] != canonical[1]:
            verdict = "inconclusiveCanonicalUnstable"
        elif any(value != canonical[0] for value in observed):
            verdict = "failedStableCanonicalMismatch"
        else:
            verdict = "passed" if observed else "stableReferenceOnly"
        rows.append({"eventId": event, "status": verdict, "observedVisits": len(observed)})
        if verdict not in ("passed", "stableReferenceOnly"):
            issues.append("EID%d: %s" % (event, verdict))
    return rows, issues


def intermediate_coverage(references, events):
    """A pre-existing end-of-frame image cannot demonstrate replay of a skipped draw."""
    rows, issues = [], []
    if any(len(references.get(event, [])) != 2 or references[event][0] != references[event][1]
           for event in events):
        return [], ["Stable references are required to establish intermediate-output coverage"]
    def pixels(witness):
        return {(p["resource"], tuple(p["subresource"])): (p["bytes"], p["sha256"])
                for p in witness["pixels"]}
    def bound(witness):
        return {b["resource"] for b in witness["bindings"] if resource_number(b["resource"])}
    a, b, c, d = events
    initial, final = pixels(references[a][0]), pixels(references[d][0])
    final_bound = bound(references[d][0])
    for event in (b, c):
        at_draw = pixels(references[event][0])
        candidates = bound(references[event][0]) - final_bound
        proven = sorted({key[0] for key in final if key[0] in candidates and key in initial
                         and initial[key] != final[key] and at_draw.get(key) == final[key]})
        rows.append({"eventId": event, "changedIntermediateOutputsUnboundAtD": proven})
        if not proven:
            issues.append("EID%d has no changed intermediate output unbound at D; static prior-frame "
                          "contents could conceal an omitted draw" % event)
    return rows, issues


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True)
    parser.add_argument("--events", required=True)
    parser.add_argument("--resources", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-read-mib", type=int, default=256)
    parser.add_argument("--max-seconds", type=int, default=180)
    parser.add_argument("--disable-live-forward", action="store_true")
    parser.add_argument("--build-label", default="unspecified")
    args = parser.parse_args(argv)
    try:
        events, numbers = parameters(args.events, args.resources)
        if not 1 <= args.max_read_mib <= 256 or not 1 <= args.max_seconds <= 600:
            raise ValueError("Read budget must be 1..256 MiB and time budget 1..600 seconds")
        capture, output = Path(args.capture).resolve(), Path(args.output).resolve()
        if not capture.is_file() or any(path.exists() for path in
                (output, output.with_suffix(".rdoc.log"), output.with_suffix(".native.log"))):
            raise ValueError("Capture must exist and output evidence stem must be unused")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    output.parent.mkdir(parents=True, exist_ok=True)
    environment = {"RENDERDOC_EID_TIMING": "1", "RENDERDOC_REPLAY_CHECKPOINTS": "0",
                   "RENDERDOC_D3D12_ENABLE_PREPARED_REPLAY": "0",
                   "RENDERDOC_D3D12_DISABLE_PREPARED_REPLAY": "1",
                   "RENDERDOC_D3D12_DISABLE_LIVE_FORWARD": "1" if args.disable_live_forward else "0"}
    os.environ.update(environment)
    report = {"schema": "renderdoc-live-forward-profile/v1", "ok": False, "measurementComplete": False,
              "configuration": vars(args), "environment": environment, "processId": os.getpid(),
              "capture": str(capture), "events": events, "requestedResources": numbers,
              "samples": [], "visits": [], "forceReferences": [], "issues": [],
              "totalReadbackBytes": 0,
              "limits": ["One same-submission D3D12 fixture; this is not full-platform acceptance",
                         "All supplied native texture subresources and bound pipeline witness are compared",
                         "No UI operation, mutation, screenshot cache, or unrequested resource scan",
                         "Readbacks serialize GPU work between visits; setter wall is not drag/scanout latency",
                         "Read budget bounds each full witness; only hashes are retained between visits"]}
    recorder = Recorder(report, output, output.with_suffix(".rdoc.log"), args.max_seconds)
    recorder.publish()
    rd, controller, cap, initialized = None, None, None, False
    requests = []
    maximum = args.max_read_mib * 1024 * 1024

    def status():
        value = parse_status(controller.GetReplayCheckpointStatus())
        report["issues"].extend(status_issues(value, disabled=True))
        return value

    def visit(label, event, force, require_hit, reference=False):
        previous = int(controller.GetCurrentEventID())
        before = status()
        _, timing = recorder.measure(label + ".replay", lambda: controller.SetFrameEvent(event, force), event)
        after = status()
        evidence = live_evidence(timing["timingRecords"], previous, event, require_hit, force)
        report["issues"].extend(label + ": " + value for value in evidence["issues"])
        if int(controller.GetCurrentEventID()) != event:
            raise RuntimeError("SetFrameEvent did not publish the requested EID")
        witness, readback = recorder.measure(label + ".readback",
            lambda: texture_witness(controller, rd, requests, numbers, maximum), event)
        if witness["eventId"] != event or int(controller.GetCurrentEventID()) != event:
            raise RuntimeError("Readonly texture witness changed the current EID")
        after_read = status()
        for key in ("hits", "builds", "checkpointCount", "allocatedBytes", "events"):
            if before.get(key) != after.get(key) or after.get(key) != after_read.get(key):
                report["issues"].append(label + ": disabled checkpoint state changed: " + key)
        report["totalReadbackBytes"] += sum(p["bytes"] for p in witness["pixels"])
        row = {"label": label, "eventId": event, "previousEventId": previous, "force": force,
               "requireHit": require_hit, "replayWallMs": timing["wallMs"],
               "readbackWallMs": readback["wallMs"], "liveForward": evidence,
               "checkpointBefore": before, "checkpointAfter": after, "witness": witness}
        (report["forceReferences"] if reference else report["visits"]).append(row)
        recorder.publish()
        return witness

    try:
        import renderdoc as rd
        report["buildIdentity"] = build_identity(rd, None)
        rd.InitialiseReplay(rd.GlobalEnvironment(), [])
        initialized = True
        rd.SetDebugLogFile(str(recorder.log_path))
        report["captureIdentity"], _ = recorder.measure("capture.identity", lambda: binary_identity(capture))
        cap = rd.OpenCaptureFile()
        result, _ = recorder.measure("capture.openFile", lambda: cap.OpenFile(str(capture), "", None))
        if result != rd.ResultCode.Succeeded:
            raise RuntimeError("OpenFile failed: " + str(result))
        report["captureDriver"] = str(cap.DriverName())
        if report["captureDriver"] != "D3D12":
            raise RuntimeError("This native live-forward fixture probe requires D3D12")
        opened, _ = recorder.measure("capture.openReplay", lambda: cap.OpenCapture(rd.ReplayOptions(), None))
        result, controller = opened
        if result != rd.ResultCode.Succeeded or controller is None:
            raise RuntimeError("OpenCapture failed: " + str(result))
        controller.ConfigureReplayCheckpoints(False, 0, 0)
        report["initialCheckpointStatus"] = status()
        report["selectedEventEvidence"] = selected_event_evidence(controller, events)
        if any(row["status"] != "exactAPIEvent" for row in report["selectedEventEvidence"]):
            raise RuntimeError("Every selected EID needs exact native structured API-event evidence")
        def collect_draws(actions):
            result = []
            for action in actions:
                if action.flags & rd.ActionFlags.Drawcall:
                    result.append(int(action.eventId))
                result.extend(collect_draws(action.children))
            return result
        draws = sorted(collect_draws(controller.GetRootActions()))
        if [event for event in draws if events[0] <= event <= events[-1]] != events:
            raise RuntimeError("The four EIDs must be consecutive actual draw actions")
        metadata = controller.GetTextures()
        report["textures"], requests, report["estimatedReadbackBytesPerWitness"] = texture_requests(
            metadata, numbers, maximum)
        for label, event, force, require_hit in sequence(events):
            visit(label, event, force, require_hit and not args.disable_live_forward)
        references = {}
        # Never put forced references between ordinary visits: force deliberately invalidates
        # the live state. The explicit forceA control above is part of the measured sequence.
        for event in events:
            references[event] = [visit("reference%d.EID%d" % (repeat + 1, event), event, True,
                                       False, reference=True) for repeat in range(2)]
        report["comparisons"], issues = comparisons(report["visits"], references, events)
        report["issues"].extend(issues)
        report["intermediateCoverage"], issues = intermediate_coverage(references, events)
        report["issues"].extend(issues)
        report["matchingForwardHits"] = sum(row["liveForward"]["matchingHits"] for row in report["visits"])
        report["measurementComplete"] = True
        report["ok"] = not report["issues"]
        report["verdict"] = ("passedDisabledControlAndNativeParity" if args.disable_live_forward
                             else "passedNativeLiveForwardAndAllTextureParity") if report["ok"] else "failedOrInconclusive"
    except Exception as error:
        report.update(ok=False, error=str(error), traceback=traceback.format_exc(), verdict="failedOrInconclusive")
    finally:
        for label, callback in [("cleanup.replay", controller.Shutdown if controller else None),
                                ("cleanup.captureFile", cap.Shutdown if cap else None),
                                ("cleanup.runtime", rd.ShutdownReplay if initialized else None)]:
            if callback is not None:
                try:
                    recorder.measure(label, callback, cleanup=True)
                except Exception as error:
                    report.setdefault("cleanupErrors", []).append({"phase": label, "error": str(error)})
                    report.update(ok=False, verdict="cleanupFailed")
        if recorder.log_path.is_file():
            native = output.with_suffix(".native.log")
            native.write_bytes(recorder.log_path.read_bytes())
            report["nativeLog"] = str(native)
        recorder.publish()
    print(json.dumps({"ok": report["ok"], "output": str(output), "verdict": report.get("verdict")}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
