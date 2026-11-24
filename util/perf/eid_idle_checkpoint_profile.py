"""Validate one bounded native idle checkpoint maintenance request on a shared-buffer fixture.

Running this script opens the explicit capture and uses its GPU. Use the bundled fresh-process
runner and coordinate GPU ownership. Discovery supplies actual resource/EID identities; packed
values are explicit and have no assumed API alignment or shader type.
"""
from __future__ import print_function

import argparse
import json
import math
import os
from pathlib import Path
import sys
import traceback

from eid_auto_checkpoint_profile import discovery_parameters, immutable_status_issues
from eid_checkpoint_profile import compare_event, parse_status, selected_event_evidence, status_issues
from eid_history_profile import resource_number
from eid_shared_buffer_profile import (SharedBufferRecorder, binary_identity, build_identity,
                                       byte_witness, fixed_checkpoint_evidence, output_witness,
                                       patch_bytes, read_window, window_checks)


def parse_values(text, width):
    values = [patch_bytes(item, 1024 * 1024) for item in text.split(",")]
    if not 2 <= len(values) <= 8 or len(set(values)) != len(values):
        raise ValueError("Supply two to eight distinct packed values")
    if any(len(value) != width for value in values):
        raise ValueError("Every value must match the discovered native byte range width")
    return values


def parse_receipt(text):
    value = json.loads(str(text))
    if value.get("schema") != "renderdoc-replay-checkpoint-maintenance/v1":
        raise ValueError("Unsupported idle maintenance receipt schema")
    for key in ("performed", "candidateSelected", "hasInitialCheckpoint", "materialized"):
        if type(value.get(key)) is not bool:
            raise ValueError("Missing boolean receipt field: " + key)
    for key in ("expectedEventId", "currentEventId", "mutationGeneration", "bufferGeneration",
                "configurationRevision", "candidateEventId", "workUnits", "builds", "hits",
                "checkpointCountBefore", "checkpointCountAfter", "lastSaveEvent", "lastResumeEvent"):
        if type(value.get(key)) is not int or value[key] < 0:
            raise ValueError("Missing nonnegative integer receipt field: " + key)
    for key in ("configurationMS", "replayMS", "totalMS"):
        if type(value.get(key)) not in (int, float) or not math.isfinite(value[key]) or value[key] < 0:
            raise ValueError("Missing finite duration receipt field: " + key)
    if not isinstance(value.get("reason"), str) or not isinstance(value.get("backendReason"), str):
        raise ValueError("Missing maintenance reason")
    if value["workUnits"] != int(value["performed"]):
        raise ValueError("Maintenance must perform at most one work unit")
    return value


def native_replay_records(sample):
    return [row for row in sample.get("timingRecords", []) if row.get("api") in
            ("D3D11", "D3D12", "OpenGL", "Vulkan", "ReplayDriverBoundary", "MaterializeEvent")]


def idle_issues(row, event, mutation, generation, expected_reason=None, repeat=False):
    receipt, before, after = row["receipt"], row["before"], row["after"]
    issues = []
    if (receipt["currentEventId"], receipt["mutationGeneration"], receipt["bufferGeneration"]) != (
            event, mutation, generation) or not receipt["materialized"]:
        issues.append("maintenance changed current EID, generations, or materialized state")
    if receipt["expectedEventId"] != row["arguments"][0]:
        issues.append("receipt does not identify the requested EID")
    if row["beforeWitness"] != row["afterWitness"]:
        issues.append("maintenance changed edited bytes, neighbors, pipeline, or bound color/depth pixels")
    if expected_reason and receipt["reason"] != expected_reason:
        issues.append("wrong stale-request reason: " + receipt["reason"])
    if (expected_reason or repeat) and receipt["performed"]:
        issues.append("stale or repeated request performed native work")
    if not receipt["performed"]:
        issues.extend(immutable_status_issues(before, after))
        if receipt["builds"] or receipt["hits"] or receipt["replayMS"] or receipt["configurationMS"]:
            issues.append("no-op receipt contains native work")
        if row["nativeReplayRecords"]:
            issues.append("no-op interval contains native driver replay/materialization")
    else:
        if not receipt["candidateSelected"] or not receipt["candidateEventId"] < event:
            issues.append("maintenance did not select an earlier candidate")
        for key in ("builds", "hits"):
            if after[key] < before[key] or receipt[key] != after[key] - before[key]:
                issues.append("receipt does not match native " + key + " delta")
        if (receipt["checkpointCountBefore"], receipt["checkpointCountAfter"]) != (
                before["checkpointCount"], after["checkpointCount"]):
            issues.append("receipt does not match native checkpoint counts")
        if receipt["reason"] not in ("checkpointBuilt", "replayedWithoutNewCheckpoint"):
            issues.append("maintenance did not complete coherently: " + receipt["reason"])
    return sorted(set(issues))


def setup_evidence(status, event):
    cutoff = status.get("sharedBufferPatchFirstAffectedEvent", 0)
    native = [eid for eid in status.get("events", [])
              if eid > 0 or status.get("hasInitialCheckpoint") is True]
    prefixes = [eid for eid in native if 0 < cutoff and eid < cutoff]
    newest = max([eid for eid in native if eid < event] or [-1])
    missing = [row["eventId"] for row in status.get("candidates", [])
               if newest < row["eventId"] < event and row["eventId"] not in native]
    return {"firstAffectedEvent": cutoff, "retainedBeforeFirstConsumer": prefixes,
            "newestNativePrefix": newest if newest >= 0 else None,
            "missingLaterCandidates": missing,
            "readyForIdleBuild": bool(prefixes and missing)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery", required=True, help="Successful shared-buffer discovery JSON")
    parser.add_argument("--capture", help="Optional relocated copy; SHA256 must match discovery")
    parser.add_argument("--fixed-values-hex", required=True, help="Two to eight distinct packed byte values")
    parser.add_argument("--output", required=True)
    parser.add_argument("--require-idle-build", action="store_true")
    parser.add_argument("--require-neighbors-unchanged", action="store_true")
    parser.add_argument("--budget-mib", type=int, default=128)
    parser.add_argument("--max-checkpoints", type=int, default=3)
    parser.add_argument("--max-read-mib", type=int, default=512,
                        help="Cumulative native output plus buffer readback budget, capped at 512 MiB")
    parser.add_argument("--max-seconds", type=int, default=300)
    parser.add_argument("--build-label", default="unspecified")
    args = parser.parse_args(argv)
    try:
        discovery = discovery_parameters(json.loads(Path(args.discovery).read_text(encoding="utf-8-sig")))
        values = parse_values(args.fixed_values_hex, discovery["width"])
        if not (1 <= args.max_read_mib <= 512 and 1 <= args.budget_mib <= 4096 and
                2 <= args.max_checkpoints <= 16 and args.max_seconds > 0):
            raise ValueError("Invalid bounded read, storage, checkpoint, or time limit")
        capture, output = Path(args.capture or discovery["capture"]).resolve(), Path(args.output).resolve()
        if not capture.is_file() or any(p.exists() for p in
                (output, output.with_suffix(".rdoc.log"), output.with_suffix(".native.log"))):
            raise ValueError("Capture must exist; choose a fresh output evidence stem")
    except (ValueError, KeyError, OSError) as error:
        parser.error(str(error))
    output.parent.mkdir(parents=True, exist_ok=True)
    environment = {"RENDERDOC_EID_TIMING": "1", "RENDERDOC_D3D12_ENABLE_PREPARED_REPLAY": "0",
                   "RENDERDOC_D3D12_DISABLE_PREPARED_REPLAY": "1"}
    os.environ.update(environment)
    target = discovery["events"][-1]
    report = {"schema": "renderdoc-idle-checkpoint-profile/v1", "ok": False,
              "capture": str(capture), "discovery": discovery, "configuration": vars(args),
              "processId": os.getpid(), "environment": environment, "samples": [], "issues": [],
              "setters": [], "forceReferences": [], "staleRequests": [], "comparisons": [],
              "totalReadbackBytes": 0,
              "limits": ["Explicit checkpoint configuration tests native maintenance, not automatic cost admission",
                         "One maintenance request may replay an entire suffix; workUnits is not a GPU dispatch count",
                         "Maintenance/setter wall excludes later validation readback; CPU wall is not GPU timestamp time",
                         "No-op native replay absence requires instrumented replay evidence elsewhere in this process",
                         "All bound color/depth subresources and the edited buffer window are checked; other UAVs are not",
                         "Neighbor propagation is legal during edited replay; maintenance itself must change no neighbor",
                         "No UI scheduling, frame publication, or screen scanout claim"]}
    recorder = SharedBufferRecorder(report, output, output.with_suffix(".rdoc.log"), args.max_seconds)
    recorder.publish()
    rd, cap, ctrl, initialized, patch_active = None, None, None, False, False
    resource, begin, size, relative, baseline_raw = None, 0, 0, 0, None

    def status():
        value = parse_status(ctrl.GetReplayCheckpointStatus())
        report["issues"].extend(status_issues(value))
        return value

    def read(label, expected=None):
        nonlocal baseline_raw
        before = status()
        event, mutation, generation = (int(ctrl.GetCurrentEventID()),
            int(ctrl.GetShaderReplacementGeneration(target)), int(ctrl.GetSharedBufferPatchGeneration()))
        if event != target:
            raise RuntimeError("Immediate witness has a stale current EID")
        remaining = args.max_read_mib * 1024 * 1024 - report["totalReadbackBytes"]
        if size >= remaining:
            raise RuntimeError("Cumulative 512 MiB-bounded read budget exhausted")
        raw, buffer_timing = recorder.measure(label + ".bufferReadback",
            lambda: bytes(ctrl.GetBufferData(resource, begin, size)), target)
        report["totalReadbackBytes"] += len(raw)
        if len(raw) != size:
            raise RuntimeError("Incomplete native buffer window")
        if baseline_raw is None:
            baseline_raw = raw
        pixels, output_timing = recorder.measure(label + ".outputReadback",
            lambda: output_witness(ctrl, rd, remaining - len(raw)), target)
        report["totalReadbackBytes"] += sum(row["bytes"] for row in pixels["pixels"])
        if (int(ctrl.GetCurrentEventID()), int(ctrl.GetShaderReplacementGeneration(target)),
                int(ctrl.GetSharedBufferPatchGeneration())) != (event, mutation, generation):
            raise RuntimeError("Native readback changed the current EID or mutation generation")
        if pixels["eventId"] != target:
            raise RuntimeError("Native output witness has a stale EID")
        report["issues"].extend(label + ": " + issue for issue in immutable_status_issues(before, status()))
        for sample in (buffer_timing, output_timing):
            if native_replay_records(sample):
                report["issues"].append(label + ": witness readback unexpectedly replayed/materialized the event")
        checks = window_checks(raw, baseline_raw, expected, relative, args.require_neighbors_unchanged) if expected is not None else None
        if checks:
            report["issues"].extend(label + ": " + issue for issue in checks["issues"])
        return {"buffer": byte_witness(raw, relative, discovery["width"]), "output": pixels}, {
            "bufferReadbackWallMs": buffer_timing["wallMs"], "outputReadbackWallMs": output_timing["wallMs"],
            "generation": generation, "mutationGeneration": mutation, "windowChecks": checks}

    def force(label, expected=None):
        before = status()
        _, timing = recorder.measure(label + ".replay", lambda: ctrl.SetFrameEvent(target, True), target)
        after = status()
        report["issues"].extend(label + ": " + issue for issue in immutable_status_issues(before, after))
        witness, read_times = read(label, expected)
        row = dict(before=before, after=after, replayWallMs=timing["wallMs"], witness=witness, **read_times)
        report["forceReferences"].append(row)
        return witness

    def compare(label, observed, references):
        verdict = compare_event(observed, references)
        report["comparisons"].append({"phase": label, "status": verdict})
        if verdict != "passed":
            report["issues"].append(label + ": " + verdict)
        return verdict

    def idle(label, arguments, expected, reason=None, repeat=False):
        before_witness, before_read = read(label + ".before", expected)
        before = status()
        row = {"phase": label, "arguments": list(arguments), "before": before,
               "beforeWitness": before_witness, "beforeReadback": before_read, "status": "running"}
        report.setdefault("requestAttempts", []).append(row)
        recorder.publish()
        receipt, timing = recorder.measure(label + ".maintenance",
            lambda: parse_receipt(ctrl.RunIdleCheckpointMaintenance(*arguments)), target)
        after = status()
        row.update(receipt=receipt, after=after, maintenanceWallMs=timing["wallMs"],
                   nativeReplayRecords=native_replay_records(timing), timingSlice=timing["timingSlice"])
        recorder.publish()
        after_witness, after_read = read(label + ".after", expected)
        row.update(afterWitness=after_witness, afterReadback=after_read, status="complete")
        row["issues"] = idle_issues(row, target, before_read["mutationGeneration"],
                                  before_read["generation"], reason, repeat)
        if any(before_read[key] != after_read[key] for key in ("generation", "mutationGeneration")):
            row["issues"].append("maintenance changed actual native mutation/buffer generation")
        report["issues"].extend(label + ": " + issue for issue in row["issues"])
        return row

    try:
        import renderdoc as rd
        report["buildIdentity"] = build_identity(rd, None)
        rd.InitialiseReplay(rd.GlobalEnvironment(), [])
        initialized = True
        rd.SetDebugLogFile(str(recorder.log_path))
        report["captureIdentity"], _ = recorder.measure("capture.identity", lambda: binary_identity(capture))
        if report["captureIdentity"]["sha256"] != discovery["captureSha256"]:
            raise RuntimeError("Discovery capture SHA256 differs from the actual input")
        cap = rd.OpenCaptureFile()
        result, _ = recorder.measure("capture.openFile", lambda: cap.OpenFile(str(capture), "", None))
        if result != rd.ResultCode.Succeeded:
            raise RuntimeError("OpenFile failed: " + str(result))
        report["captureDriver"] = str(cap.DriverName())
        opened, _ = recorder.measure("capture.openReplay", lambda: cap.OpenCapture(rd.ReplayOptions(), None))
        result, ctrl = opened
        if result != rd.ResultCode.Succeeded or ctrl is None:
            raise RuntimeError("OpenCapture failed: " + str(result))
        report["api"] = str(ctrl.GetAPIProperties().pipelineType)
        if not hasattr(ctrl, "RunIdleCheckpointMaintenance"):
            report["unsupported"] = "Matching native maintenance API/bindings are unavailable"
            raise RuntimeError(report["unsupported"])
        report["selectedEventEvidence"] = selected_event_evidence(ctrl, discovery["events"])
        if any(row["status"] != "exactAPIEvent" for row in report["selectedEventEvidence"]):
            raise RuntimeError("Every discovered consumer requires exact structured API evidence")
        def actual_draw(actions):
            return any((int(a.eventId) == target and bool(a.flags & rd.ActionFlags.Drawcall)) or
                       actual_draw(a.children) for a in actions)
        if not actual_draw(ctrl.GetRootActions()):
            raise RuntimeError("The maintenance target is not an actual captured draw")
        buffers = [b for b in ctrl.GetBuffers() if resource_number(b.resourceId) == discovery["buffer"]]
        if len(buffers) != 1:
            raise RuntimeError("Discovery buffer is absent from native metadata")
        resource = buffers[0].resourceId
        begin, size, relative = read_window(int(buffers[0].length), discovery["offset"], discovery["width"], 16)
        ctrl.ConfigureReplayCheckpoints(True, args.budget_mib * 1024 * 1024, args.max_checkpoints)
        report["configuredStatus"] = status()
        baseline = [force("baseline.reference%d" % (index + 1)) for index in range(2)]
        compare("baselineStability", [baseline[0]], baseline)
        identity = baseline_raw[relative:relative + discovery["width"]]
        if identity in values:
            raise RuntimeError("Every supplied fixed value must be an actual change from captured bytes")
        for index, value in enumerate(values):
            before, previous_generation = status(), int(ctrl.GetSharedBufferPatchGeneration())
            patch_active = True
            result, timing = recorder.measure("value%d.setter" % (index + 1),
                lambda: ctrl.SetSharedBufferPatch(resource, discovery["offset"], value), target)
            if str(result):
                raise RuntimeError("Shared-buffer setter rejected: " + str(result))
            generation = int(ctrl.GetSharedBufferPatchGeneration())
            if generation <= previous_generation:
                raise RuntimeError("Distinct value did not advance native generation")
            after = status()
            witness, read_times = read("value%d.immediate" % (index + 1), value)
            row = dict(valueHex=value.hex(), before=before, after=after, setterWallMs=timing["wallMs"],
                       witness=witness, checkpointEvidence=fixed_checkpoint_evidence(before, after, generation), **read_times)
            report["setters"].append(row)
            references = [force("value%d.reference%d" % (index + 1, n + 1), value) for n in range(2)]
            row["comparison"] = compare("value%d" % (index + 1), [witness], references)
            row["visibleEffect"] = ("changedBoundOutputPixels" if references[0]["output"]["pixels"] != baseline[0]["output"]["pixels"]
                                    else "noVisibleEffect") if references[0] == references[1] else "inconclusiveCanonicalUnstable"
            recorder.publish()
        report["setupEvidence"] = setup_evidence(status(), target)
        mutation, generation = int(ctrl.GetShaderReplacementGeneration(target)), int(ctrl.GetSharedBufferPatchGeneration())
        stale = [("staleEvent", (discovery["events"][0], mutation, generation)),
                 ("staleMutation", (target, mutation ^ 1, generation)),
                 ("staleBufferGeneration", (target, mutation, generation ^ 1))]
        for reason, arguments in stale:
            report["staleRequests"].append(idle(reason, arguments, values[-1], reason))
            recorder.publish()
        valid = idle("valid", (target, mutation, generation), values[-1])
        report["validRequest"] = valid
        if not valid["receipt"]["performed"] and valid["receipt"]["reason"] not in (
                "backendDisabledOrUnsupported", "noMissingExplicitCandidate"):
            report["issues"].append("Valid maintenance was rejected: " + valid["receipt"]["reason"])
        report["repeatRequest"] = idle("repeat", (target, mutation, generation), values[-1], repeat=True)
        references = [force("maintenance.reference%d" % (n + 1), values[-1]) for n in range(2)]
        compare("maintenance", [valid["beforeWitness"], valid["afterWitness"],
                report["repeatRequest"]["afterWitness"]], references)
        if args.require_idle_build and (valid["receipt"]["builds"] < 1 or not report["setupEvidence"]["readyForIdleBuild"]):
            report["issues"].append("Required idle save with retained pre-consumer prefix and missing later candidate was not observed")
        before_clear_generation = int(ctrl.GetSharedBufferPatchGeneration())
        result, timing = recorder.measure("clear.setter", ctrl.ClearSharedBufferPatches, target)
        if str(result):
            raise RuntimeError("Clear failed: " + str(result))
        patch_active = False
        if int(ctrl.GetSharedBufferPatchGeneration()) <= before_clear_generation:
            raise RuntimeError("Clear did not advance patch generation")
        cleared, clear_read = read("clear.immediate")
        report["clear"] = dict(setterWallMs=timing["wallMs"], witness=cleared, **clear_read)
        references = [force("clear.reference%d" % (n + 1)) for n in range(2)]
        report["clear"]["comparison"] = compare("clearRestoresOriginal", [cleared] + baseline, references)
        report["nativeReplayLogCoverage"] = any(native_replay_records(sample) for sample in report["samples"]
                                                 if sample["phase"].endswith(".replay"))
        if not report["nativeReplayLogCoverage"]:
            report["issues"].append("No instrumented native reference replay; no-op replay absence is unverified")
        report["finalStatus"] = status()
        report["measurementComplete"] = True
        report["maintenanceOutcome"] = ("built" if valid["receipt"]["builds"] else
            "replayedWithoutSave" if valid["receipt"]["performed"] else
            "unsupportedOrDisabledBackend" if valid["receipt"]["reason"] == "backendDisabledOrUnsupported" else
            "noEligibleCandidate")
        report["ok"] = not report["issues"]
        report["verdict"] = ("passedIdleBuildAndNativeParity" if valid["receipt"]["builds"] else
                             "passedContractWithoutIdleBuild") if report["ok"] else "failedOrInconclusive"
    except Exception as error:
        report.update(ok=False, error=str(error), traceback=traceback.format_exc(), verdict="failedOrInconclusive")
    finally:
        def cleanup(label, callback):
            try:
                recorder.measure(label, callback, cleanup=True)
            except Exception as error:
                report.setdefault("cleanupErrors", []).append({"phase": label, "error": str(error)})
                report.update(ok=False, verdict="cleanupFailed")
        if ctrl is not None:
            if patch_active:
                def clear():
                    result = str(ctrl.ClearSharedBufferPatches())
                    if result:
                        raise RuntimeError(result)
                cleanup("cleanup.clear", clear)
            def disable():
                ctrl.ConfigureReplayCheckpoints(False, args.budget_mib * 1024 * 1024, args.max_checkpoints)
                report["disabledStatus"] = parse_status(ctrl.GetReplayCheckpointStatus())
                errors = status_issues(report["disabledStatus"], disabled=True)
                if errors:
                    raise RuntimeError("; ".join(errors))
            cleanup("cleanup.disable", disable)
            cleanup("cleanup.controller", ctrl.Shutdown)
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
