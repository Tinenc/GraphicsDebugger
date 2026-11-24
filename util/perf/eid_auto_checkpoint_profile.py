"""Fresh-process native validation of default automatic checkpoint admission.

This script opens the explicitly supplied capture and uses the GPU when run. It never calls
ConfigureReplayCheckpoints. Coordinate its execution with other native replay processes.
"""
from __future__ import print_function

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
import traceback

from eid_checkpoint_profile import (compare_event, force_bypass_issues, parse_events, parse_status,
                                    selected_event_evidence, status_issues)
from eid_history_profile import resource_number
from eid_shared_buffer_profile import (SharedBufferRecorder, binary_identity, build_identity,
                                       byte_witness, fixed_checkpoint_evidence, fixed_patch_values,
                                       output_witness, patch_bytes, read_window,
                                       unchanged_checkpoint_issues, window_checks)


def discovery_parameters(document):
    if document.get("schema") != "renderdoc-shared-buffer-fixture/v1" or document.get("ok") is not True:
        raise ValueError("A successful shared-buffer fixture discovery report is required")
    bindings = document.get("bindings", [])
    if len(bindings) < 2:
        raise ValueError("Discovery must prove at least two actual shared-buffer consumers")
    identities = {(int(row["buffer"]), int(row["absoluteOffset"])) for row in bindings}
    if len(identities) != 1:
        raise ValueError("Discovery consumers do not identify one shared native byte range")
    events = parse_events(",".join(str(row["eventId"]) for row in bindings))
    buffer_id, offset = next(iter(identities))
    arguments = document.get("profileArguments", [])
    if "--data-hex" not in arguments or arguments.index("--data-hex") + 1 == len(arguments):
        raise ValueError("Discovery must state its observable patch width")
    width = len(patch_bytes(arguments[arguments.index("--data-hex") + 1], 1024 * 1024))
    digest = document.get("captureSha256", "")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest.lower()):
        raise ValueError("Discovery has no valid capture identity")
    return {"capture": document["capture"], "events": [events[0], events[-1]],
            "buffer": buffer_id, "offset": offset, "width": width, "captureSha256": digest.lower()}


def automatic_status(text):
    status = parse_status(text)
    automatic = status.get("automatic", {})
    if automatic.get("mode") != "automatic":
        raise ValueError("The controller is not in default automatic mode; do not explicitly configure it")
    if automatic.get("state") not in ("observing", "trial", "retained", "rejected"):
        raise ValueError("Missing or unsupported automatic admission state")
    return status


def immutable_status_issues(before, after):
    issues = force_bypass_issues(before, after) + unchanged_checkpoint_issues(before, after)
    if before.get("automatic") != after.get("automatic"):
        issues.append("force/readback changed automatic admission evidence")
    return sorted(set(issues))


def execution_evidence(before, after, sample):
    records = [row for row in sample.get("timingRecords", [])
               if row.get("api") == "ReplayCheckpointAuto"]
    result = {"automaticRecords": records, "countersReset": any(
        after[name] < before[name] for name in ("builds", "hits"))}
    for name in ("builds", "hits"):
        delta = after[name] - before[name]
        values = [row.get(name) for row in records]
        # The controller logs deltas after incremental reconfiguration. A raw counter reset
        # cannot be converted into a measured zero or a guessed number of successful restores.
        result[name] = (sum(values) if values and all(isinstance(v, (int, float)) for v in values)
                        else delta if delta >= 0 else None)
    result["reference"] = any(row.get("reference") == 1 for row in records) if records else None
    result["state"] = after["automatic"]["state"]
    return result


def admission_issues(normal, fixed, require_hit):
    issues = []
    normal_hits = sum(row["execution"]["hits"] or 0 for row in normal)
    fixed_hits = [row for row in fixed if (row["execution"]["hits"] or 0) > 0]
    if require_hit and not fixed and normal_hits < 1:
        issues.append("No actual native automatic checkpoint hit was observed")
    if require_hit and fixed:
        if len(fixed_hits) < 2:
            issues.append("Fewer than two fixed-value setters actually restored automatic checkpoints")
        if any(not row["checkpointEvidence"]["setterResumeBeforeCutoff"] for row in fixed_hits):
            issues.append("A fixed-value hit did not resume strictly before the proven first consumer")
    return issues


def setter_checkpoint_evidence(before, after, generation, execution):
    measured = dict(before)
    if after["hits"] < before["hits"]:
        if execution["hits"] is None:
            raise RuntimeError("Setter reset native counters without an exact controller hit delta")
        # A pending auto rejection/layout reset can reconfigure before this setter's replay.
        # Preserve raw before/after status; normalize only the counter input to the common guard
        # using its native post-configuration delta, never by guessing a zero hit count.
        measured["hits"] = after["hits"] - execution["hits"]
        if measured["hits"] < 0:
            raise RuntimeError("Controller hit delta exceeds the setter's final native count")
    evidence = fixed_checkpoint_evidence(measured, after, generation)
    evidence["counterNormalizedFromNativeAutoRecord"] = measured["hits"] != before["hits"]
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture")
    parser.add_argument("--events", help="Exactly two actual API EIDs, alternated in ascending order")
    parser.add_argument("--output", required=True)
    parser.add_argument("--rounds", type=int, default=8)
    parser.add_argument("--require-auto-hit", action="store_true")
    parser.add_argument("--discovery", help="Existing successful shared-buffer fixture discovery JSON")
    parser.add_argument("--fixed-values-hex", help="Four to sixteen distinct same-width packed byte values")
    parser.add_argument("--require-neighbors-unchanged", action="store_true")
    parser.add_argument("--max-read-mib", type=int, default=512)
    parser.add_argument("--max-seconds", type=int, default=300)
    parser.add_argument("--build-label", default="unspecified")
    args = parser.parse_args(argv)
    discovery, values = None, []
    try:
        if os.environ.get("RENDERDOC_REPLAY_CHECKPOINTS", "") not in ("", "auto"):
            raise ValueError("Unset RENDERDOC_REPLAY_CHECKPOINTS (or use auto) for this probe")
        if not 1 <= args.rounds <= 32 or args.max_read_mib < 1 or args.max_seconds < 1:
            raise ValueError("rounds must be 1-32 and read/time limits must be positive")
        if args.discovery:
            discovery = discovery_parameters(json.loads(Path(args.discovery).read_text(encoding="utf-8-sig")))
        if args.fixed_values_hex:
            if not discovery:
                raise ValueError("Fixed values require the capture-bound --discovery report")
            values = fixed_patch_values(args.fixed_values_hex, discovery["width"], 1024 * 1024)
        capture = Path(args.capture or (discovery or {}).get("capture", "")).resolve()
        events = parse_events(args.events) if args.events else (discovery or {}).get("events", [])
        if len(events) != 2:
            raise ValueError("Provide exactly two actual EIDs or a successful fixture discovery")
        if values and events[-1] not in discovery["events"]:
            raise ValueError("The fixed target must be a discovered shared-buffer consumer")
        output = Path(args.output).resolve()
        if not capture.is_file() or any(p.exists() for p in
                (output, output.with_suffix(".rdoc.log"), output.with_suffix(".native.log"))):
            raise ValueError("Capture must exist; choose a fresh output evidence stem")
    except (ValueError, KeyError, OSError) as error:
        parser.error(str(error))
    output.parent.mkdir(parents=True, exist_ok=True)
    environment = {"RENDERDOC_EID_TIMING": "1", "RENDERDOC_D3D12_ENABLE_PREPARED_REPLAY": "0",
                   "RENDERDOC_D3D12_DISABLE_PREPARED_REPLAY": "1"}
    os.environ.update(environment)
    report = {"schema": "renderdoc-auto-checkpoint-profile/v1", "ok": False,
              "configuration": vars(args), "capture": str(capture), "events": events,
              "environment": dict(environment, RENDERDOC_REPLAY_CHECKPOINTS=os.environ.get(
                  "RENDERDOC_REPLAY_CHECKPOINTS", "<unset>")), "processId": os.getpid(),
              "samples": [], "normalVisits": [], "forceReferences": [], "comparisons": [],
              "fixedValues": [], "issues": [],
              "limits": ["No explicit checkpoint configuration, speculative replay, or UI operation",
                         "Replay/setter wall excludes later all-bound-output and buffer readbacks",
                         "Readback serializes GPU work between visits; this is not a GUI latency benchmark",
                         "Driver wall admission is not a GPU timestamp or screen scanout measurement",
                         "No visible effect is recorded separately from native byte/reference correctness"]}
    recorder = SharedBufferRecorder(report, output, output.with_suffix(".rdoc.log"), args.max_seconds)
    recorder.publish()
    rd, cap, controller, initialized = None, None, None, False
    resource, begin, size, relative, baseline_raw = None, 0, 0, 0, None
    patch_active = False

    def status():
        value = automatic_status(controller.GetReplayCheckpointStatus())
        report["issues"].extend(status_issues(value))
        return value

    def read(label, event, expected=None):
        before = status()
        if int(controller.GetCurrentEventID()) != event:
            raise RuntimeError("Immediate native witness has a stale current EID")
        generation = int(controller.GetSharedBufferPatchGeneration())
        witness, read_times = {}, {}
        if resource is not None:
            raw, timing = recorder.measure(label + ".bufferReadback",
                lambda: bytes(controller.GetBufferData(resource, begin, size)), event)
            if len(raw) != size:
                raise RuntimeError("Incomplete native buffer window")
            witness["buffer"] = byte_witness(raw, relative, discovery["width"])
            read_times["bufferReadbackWallMs"] = timing["wallMs"]
            if expected is not None:
                checks = window_checks(raw, baseline_raw, expected, relative, args.require_neighbors_unchanged)
                report["issues"].extend(label + ": " + issue for issue in checks["issues"])
                read_times["windowChecks"] = checks
        witness["output"], timing = recorder.measure(label + ".outputReadback",
            lambda: output_witness(controller, rd, args.max_read_mib * 1024 * 1024), event)
        read_times["outputReadbackWallMs"] = timing["wallMs"]
        if witness["output"]["eventId"] != event or int(controller.GetSharedBufferPatchGeneration()) != generation:
            raise RuntimeError("Immediate native output has a stale EID or patch generation")
        after = status()
        report["issues"].extend(label + ": " + issue for issue in immutable_status_issues(before, after))
        return witness, read_times

    def visit(label, event, force, expected=None):
        before = status()
        _, timing = recorder.measure(label + ".replay", lambda: controller.SetFrameEvent(event, force), event)
        after = status()
        if force:
            report["issues"].extend(label + ": " + issue for issue in immutable_status_issues(before, after))
        witness, read_times = read(label, event, expected)
        row = dict(eventId=event, force=force, before=before, after=after,
                   replayWallMs=timing["wallMs"], witness=witness,
                   execution=execution_evidence(before, after, timing), **read_times)
        (report["forceReferences"] if force else report["normalVisits"]).append(row)
        recorder.publish()
        return row

    def mutate(label, value):
        before = status()
        previous_generation = int(controller.GetSharedBufferPatchGeneration())
        result, timing = recorder.measure(label + ".setter",
            lambda: controller.SetSharedBufferPatch(resource, discovery["offset"], value), events[-1])
        if str(result):
            raise RuntimeError("Shared-buffer setter failed: " + str(result))
        generation = int(controller.GetSharedBufferPatchGeneration())
        if generation <= previous_generation:
            raise RuntimeError("A distinct shared value did not advance its native generation")
        after = status()
        # No SetFrameEvent occurs between this setter and the immediate live bytes/pixels.
        witness, read_times = read(label + ".immediate", events[-1], value)
        execution = execution_evidence(before, after, timing)
        return dict(valueHex=value.hex(), generationBefore=previous_generation, generationAfter=generation,
                    before=before, after=after, setterWallMs=timing["wallMs"], witness=witness,
                    execution=execution,
                    checkpointEvidence=setter_checkpoint_evidence(before, after, generation, execution), **read_times)

    try:
        import renderdoc as rd
        report["buildIdentity"] = build_identity(rd, None)
        rd.InitialiseReplay(rd.GlobalEnvironment(), [])
        initialized = True
        rd.SetDebugLogFile(str(recorder.log_path))
        report["captureIdentity"], _ = recorder.measure("capture.identity", lambda: binary_identity(capture))
        if discovery and report["captureIdentity"]["sha256"] != discovery["captureSha256"]:
            raise RuntimeError("Discovery capture hash differs from the actual input")
        cap = rd.OpenCaptureFile()
        result, _ = recorder.measure("capture.openFile", lambda: cap.OpenFile(str(capture), "", None))
        if result != rd.ResultCode.Succeeded:
            raise RuntimeError("OpenFile failed: " + str(result))
        report["captureDriver"] = str(cap.DriverName())
        opened, _ = recorder.measure("capture.openReplay", lambda: cap.OpenCapture(rd.ReplayOptions(), None))
        result, controller = opened
        if result != rd.ResultCode.Succeeded or controller is None:
            raise RuntimeError("OpenCapture failed: " + str(result))
        report["initialStatus"] = status()
        report["selectedEventEvidence"] = selected_event_evidence(controller, events)
        if any(row["status"] != "exactAPIEvent" for row in report["selectedEventEvidence"]):
            raise RuntimeError("Every selected EID requires exact structured API-event evidence")
        observed = {event: [] for event in events}
        for iteration in range(args.rounds):
            for event in events:
                row = visit("normal.round%d.EID%d" % (iteration + 1, event), event, False)
                row["round"] = iteration + 1
                observed[event].append(row["witness"])
        # Reference work is deliberately after the natural request sequence so it cannot train
        # auto admission or warm its first construction under a different replay route.
        for event in events:
            canonical = [visit("normal.reference%d.EID%d" % (i + 1, event), event, True)["witness"]
                         for i in range(2)]
            verdict = compare_event(observed[event], canonical)
            report["comparisons"].append({"eventId": event, "status": verdict,
                                            "normalVisits": len(observed[event])})
            if verdict != "passed":
                report["issues"].append("EID%d: %s" % (event, verdict))
        report["normalFinalStatus"] = status()
        if values:
            def actual_draw(actions):
                return any((int(action.eventId) == events[-1] and bool(action.flags & rd.ActionFlags.Drawcall))
                           or actual_draw(action.children) for action in actions)
            if not actual_draw(controller.GetRootActions()):
                raise RuntimeError("The fixed target is not an actual draw")
            buffers = [b for b in controller.GetBuffers() if resource_number(b.resourceId) == discovery["buffer"]]
            if len(buffers) != 1:
                raise RuntimeError("Discovery buffer is absent from native capture metadata")
            resource = buffers[0].resourceId
            begin, size, relative = read_window(int(buffers[0].length), discovery["offset"], discovery["width"], 16)
            baseline_raw, _ = recorder.measure("fixed.baselineBufferReadback",
                lambda: bytes(controller.GetBufferData(resource, begin, size)), events[-1])
            baseline, _ = read("fixed.baseline", events[-1])
            seed = baseline_raw[relative:relative + discovery["width"]]
            if seed in values:
                raise RuntimeError("Fixed values must differ from the captured identity seed")
            patch_active = True
            report["fixedIdentitySeed"] = mutate("fixed.identitySeed", seed)
            report["fixedWarmupMeaning"] = "Identity establishes layout through its forced replay; four later values allow one natural baseline, one build, and two independent hits."
            for index, value in enumerate(values):
                row = mutate("fixed.value%d" % (index + 1), value)
                report["fixedValues"].append(row)
                references = [visit("fixed.value%d.reference%d" % (index + 1, i + 1), events[-1], True, value)
                              for i in range(2)]
                canonical = [ref["witness"] for ref in references]
                row["comparison"] = compare_event([row["witness"]], canonical)
                row["visibleEffect"] = ("changedBoundOutputPixels" if canonical[0]["output"]["pixels"] !=
                                         baseline["output"]["pixels"] else "noVisibleEffect") if canonical[0] == canonical[1] else "inconclusiveCanonicalUnstable"
                if row["comparison"] != "passed":
                    report["issues"].append("fixed.value%d: %s" % (index + 1, row["comparison"]))
                recorder.publish()
            result, _ = recorder.measure("fixed.clear", controller.ClearSharedBufferPatches, events[-1])
            if str(result):
                raise RuntimeError("Clearing shared patches failed: " + str(result))
            patch_active = False
            cleared, _ = read("fixed.cleared.immediate", events[-1])
            references = [visit("fixed.cleared.reference%d" % (i + 1), events[-1], True)["witness"] for i in range(2)]
            report["clearComparison"] = compare_event([cleared, baseline], references)
            if report["clearComparison"] != "passed":
                report["issues"].append("clear: " + report["clearComparison"])
        report["finalStatus"] = status()
        report["issues"].extend(admission_issues(report["normalVisits"], report["fixedValues"], args.require_auto_hit))
        report["observedNormalHits"] = sum(row["execution"]["hits"] or 0 for row in report["normalVisits"])
        report["observedSetterHits"] = sum(row["execution"]["hits"] or 0 for row in report["fixedValues"])
        report["normalTiming"] = [{"eventId": event, "count": len(observed[event]),
            "replayWallMs": {"median": statistics.median([r["replayWallMs"] for r in report["normalVisits"] if r["eventId"] == event]),
                             "min": min(r["replayWallMs"] for r in report["normalVisits"] if r["eventId"] == event),
                             "max": max(r["replayWallMs"] for r in report["normalVisits"] if r["eventId"] == event)},
            "meaning": "Ordered baseline/trial/hit samples are preserved; mixed-phase summary is not steady-state speedup"}
            for event in events]
        report["ok"] = not report["issues"]
        report["verdict"] = ("passedAutomaticHitAndNativeParity" if report["observedNormalHits"] or report["observedSetterHits"]
                             else "passedNativeParityWithoutAutomaticHit") if report["ok"] else "failedOrInconclusive"
    except Exception as error:
        report.update(ok=False, error=str(error), traceback=traceback.format_exc(), verdict="failedOrInconclusive")
    finally:
        def cleanup(label, callback):
            try:
                recorder.measure(label, callback, cleanup=True)
            except Exception as error:
                report.setdefault("cleanupErrors", []).append({"phase": label, "error": str(error)})
                report.update(ok=False, verdict="cleanupFailed")
        if controller is not None:
            if patch_active:
                def clear():
                    error = str(controller.ClearSharedBufferPatches())
                    if error:
                        raise RuntimeError(error)
                cleanup("cleanup.clear", clear)
            cleanup("cleanup.replay", controller.Shutdown)
        if cap is not None:
            cleanup("cleanup.captureFile", cap.Shutdown)
        if initialized:
            cleanup("cleanup.runtime", rd.ShutdownReplay)
        if recorder.log_path.is_file():
            native = output.with_suffix(".native.log")
            native.write_bytes(recorder.log_path.read_bytes())
            report["nativeLog"] = str(native)
        recorder.publish()
    print(json.dumps({"ok": report["ok"], "output": str(output), "verdict": report.get("verdict")}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
