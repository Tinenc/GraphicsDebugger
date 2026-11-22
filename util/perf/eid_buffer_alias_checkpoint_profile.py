"""Native D3D12 buffer-alias checkpoint correctness, full bytes and full output coverage.

Main opens only the explicitly named capture and uses the GPU. Coordinate execution with the
isolated runner. Resource IDs and four direct draw EIDs come from loaded public capture metadata.
No shader or buffer edits; the disabled-alias control uses the same capture and evidence contract.
"""
from __future__ import print_function

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback

from eid_checkpoint_profile import (compare_event, force_bypass_issues, parse_status,
                                    selected_event_evidence, status_issues)
from eid_history_profile import Recorder, integer_list, resource_number
from eid_live_forward_profile import texture_requests, texture_witness
from eid_shared_buffer_profile import (binary_identity, build_identity, readback_replay_issues,
                                       unchanged_checkpoint_issues)


DEFAULT_NAMES = ["CheckpointBufferAlias%d" % index for index in range(4)]


def select_draws(actions, requested):
    available = sorted(set(actions))
    events = integer_list(requested) if requested else available
    if len(events) != 4 or events != sorted(set(events)) or any(e not in available for e in events):
        raise ValueError("Select exactly four distinct ascending actual direct draw EIDs")
    return events


def resolve_buffers(names, resources, buffers, maximum):
    if len(names) != 4 or len(set(names)) != 4 or any(not n or len(n) > 256 for n in names):
        raise ValueError("Exactly four distinct bounded buffer names are required")
    result, total = [], 0
    for name in names:
        named = [r for r in resources if str(r.name) == name]
        if len(named) != 1:
            raise ValueError("Alias buffer name is missing or ambiguous: " + name)
        found = [b for b in buffers if b.resourceId == named[0].resourceId]
        if len(found) != 1 or resource_number(found[0].resourceId) in (None, 0):
            raise ValueError("Named alias is not a unique captured buffer: " + name)
        count = int(found[0].length)
        if count < 1 or count > maximum - total:
            raise ValueError("Complete alias buffer bytes exceed max-read-mib")
        total += count
        result.append((name, found[0].resourceId, count))
    if len({str(r) for _, r, _ in result}) != 4:
        raise ValueError("Alias names must resolve to four distinct original resource IDs")
    return result, total


def alias_restore_evidence(records, event, before, after, disabled=False, force=False):
    rows = [r for r in records if r.get("api") == "D3D12Checkpoint" and r.get("op") == "restore"]
    issues, copied = [], 0
    if after["hits"] - before["hits"] != len(rows):
        issues.append("Checkpoint hit counter does not match actual native restore records")
    for row in rows:
        count, size = row.get("bufferAliasComponents"), row.get("bufferAliasCopyBytes")
        if row.get("target") != event - 1 or row.get("event") not in before.get("events", []):
            issues.append("Native restore checkpoint or target differs from this ordinary request")
        if row.get("prefixSkipped") != 1:
            issues.append("Native restore did not establish skipped prefix replay")
        if type(count) is not int or count < 0 or type(size) is not int or size < 0:
            issues.append("Native restore is missing valid physical alias copy counters")
        elif bool(count) != bool(size):
            issues.append("Native alias component count and copied byte count disagree")
        else:
            copied += size
        if disabled and (count or size):
            issues.append("Disabled alias control copied a physical alias checkpoint")
    if force:
        issues += force_bypass_issues(before, after)
        if rows:
            issues.append("Forced canonical replay restored a checkpoint")
        if not any(r.get("api") == "D3D12" and r.get("type") == "WithoutDraw" and
                   r.get("range") == "1->%d" % event and r.get("partial") == 0 and
                   r.get("replay_depth") == 0 and r.get("section.applyInitial_calls", 0) > 0
                   for r in records):
            issues.append("Forced canonical reference lacks independent native initial replay")
    return {"records": rows, "copyBytes": copied, "issues": issues}


def comparisons(visits, references, events):
    result, issues = [], []
    for event in events:
        observed = [v["witness"] for v in visits if not v["force"] and v["eventId"] == event]
        status = compare_event(observed, references.get(event, []))
        result.append({"eventId": event, "status": status, "ordinaryVisits": len(observed)})
        if status != "passed":
            issues.append("EID%d full alias bytes/output comparison: %s" % (event, status))
    return result, issues


def changing_content_evidence(references, events):
    if any(len(references.get(e, [])) != 2 or references[e][0] != references[e][1] for e in events):
        return {}, ["Stable full references are required to establish changing alias contents"]
    witnesses = [references[e][0] for e in events]
    buffers = [json.dumps(w["buffers"], sort_keys=True) for w in witnesses]
    pixels = [json.dumps(w["output"]["pixels"], sort_keys=True) for w in witnesses]
    evidence = {"distinctBufferStates": len(set(buffers)), "distinctPixelStates": len(set(pixels))}
    issues = []
    if evidence["distinctBufferStates"] < 2:
        issues.append("Alias fixture did not exercise changing complete buffer contents")
    if evidence["distinctPixelStates"] < 2:
        issues.append("Alias fixture did not exercise changing output pixels")
    return evidence, issues


class AliasProbe:
    def __init__(self, controller, rd, recorder, report, buffers, requests, numbers, maximum, disabled):
        self.controller, self.rd, self.recorder, self.report = controller, rd, recorder, report
        self.buffers, self.requests, self.numbers = buffers, requests, numbers
        self.maximum, self.disabled = maximum, disabled

    def status(self, disabled=False):
        value = parse_status(self.controller.GetReplayCheckpointStatus())
        self.report["issues"].extend(status_issues(value, disabled))
        return value

    def read(self, label, event):
        before = self.status()
        def witness():
            result, total = [], 0
            for name, resource, count in self.buffers:
                raw = bytes(self.controller.GetBufferData(resource, 0, count))
                if len(raw) != count or count > self.maximum - total:
                    raise RuntimeError("Alias buffer readback is truncated or over budget: " + name)
                total += len(raw)
                result.append({"name": name, "resource": str(resource), "bytes": len(raw),
                               "sha256": hashlib.sha256(raw).hexdigest()})
            output = texture_witness(self.controller, self.rd, self.requests, self.numbers,
                                     self.maximum - total)
            if int(self.controller.GetCurrentEventID()) != event or output["eventId"] != event:
                raise RuntimeError("Immediate alias witness exposes a stale EID")
            return {"buffers": result, "output": output}
        value, sample = self.recorder.measure(label + ".readback", witness, event)
        issues = readback_replay_issues(sample["timingRecords"])
        issues += unchanged_checkpoint_issues(before, self.status())
        if issues:
            raise RuntimeError("; ".join(issues))
        return value, sample

    def visit(self, label, event, force=False):
        before = self.status()
        _, sample = self.recorder.measure(label + ".replay", lambda:
            self.controller.SetFrameEvent(event, force), event)
        after = self.status()
        evidence = alias_restore_evidence(sample["timingRecords"], event, before, after,
                                           self.disabled, force)
        self.report["issues"].extend(label + ": " + issue for issue in evidence["issues"])
        witness, readback = self.read(label, event)
        row = {"label": label, "eventId": event, "force": force, "before": before, "after": after,
               "replayWallMs": sample["wallMs"], "readbackWallMs": readback["wallMs"],
               "nativeAliasRestore": evidence, "witness": witness}
        self.report["visits"].append(row)
        self.recorder.publish()
        return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--events", default="", help="Empty discovers exactly four native direct draws")
    parser.add_argument("--buffer-name", action="append", default=[])
    parser.add_argument("--budget-mib", type=int, default=64)
    parser.add_argument("--max-checkpoints", type=int, default=3)
    parser.add_argument("--max-read-mib", type=int, default=256)
    parser.add_argument("--max-seconds", type=int, default=300)
    parser.add_argument("--disable-buffer-aliases", action="store_true")
    parser.add_argument("--build-label", default="unspecified")
    args = parser.parse_args(argv)
    capture, output = Path(args.capture).resolve(), Path(args.output).resolve()
    if (not capture.is_file() or any(p.exists() for p in
            (output, output.with_suffix(".rdoc.log"), output.with_suffix(".native.log"))) or
            not 1 <= args.budget_mib <= 4096 or not 1 <= args.max_checkpoints <= 16 or
            not 1 <= args.max_read_mib <= 512 or not 1 <= args.max_seconds <= 600):
        parser.error("Capture must exist, evidence stem must be fresh, and limits must be bounded")
    names = args.buffer_name or DEFAULT_NAMES
    output.parent.mkdir(parents=True, exist_ok=True)
    environment = {"RENDERDOC_EID_TIMING": "1", "RENDERDOC_REPLAY_CHECKPOINTS": "1",
                   "RENDERDOC_D3D12_DISABLE_PREPARED_REPLAY": "1",
                   "RENDERDOC_D3D12_ENABLE_PREPARED_REPLAY": "0",
                   "RENDERDOC_D3D12_DISABLE_LIVE_FORWARD": "1",
                   "RENDERDOC_D3D12_DISABLE_CHECKPOINT_BUFFER_ALIASES":
                       "1" if args.disable_buffer_aliases else "0"}
    os.environ.update(environment)
    report = {"schema": "renderdoc-buffer-alias-checkpoint-profile/v1", "ok": False,
              "measurementComplete": False, "configuration": vars(args), "capture": str(capture),
              "environment": environment, "processId": os.getpid(), "samples": [], "visits": [],
              "issues": [], "limits": ["Four named original buffers read completely at every visit",
                  "All subresources of every color/depth output bound at the four discovered draws",
                  "Forced references run twice after ordinary visits without clearing checkpoints",
                  "Physical alias restore requires native copy counters, not just equal pictures",
                  "No shader/CB edits; cross-vendor performance and typed-texture aliasing untested"]}
    recorder = Recorder(report, output, output.with_suffix(".rdoc.log"), args.max_seconds)
    recorder.publish()
    rd, controller, cap, initialized = None, None, None, False
    try:
        import renderdoc as rd
        report["buildIdentity"] = build_identity(rd, None)
        rd.InitialiseReplay(rd.GlobalEnvironment(), [])
        initialized = True
        rd.SetDebugLogFile(str(recorder.log_path))
        report["captureIdentity"], _ = recorder.measure("capture.identity", lambda: binary_identity(capture))
        cap = rd.OpenCaptureFile()
        result, _ = recorder.measure("capture.openFile", lambda: cap.OpenFile(str(capture), "", None))
        if result != rd.ResultCode.Succeeded or str(cap.DriverName()) != "D3D12":
            raise RuntimeError("Capture did not open as native D3D12")
        report["captureDriver"] = str(cap.DriverName())
        opened, _ = recorder.measure("capture.openReplay", lambda: cap.OpenCapture(rd.ReplayOptions(), None))
        result, controller = opened
        if result != rd.ResultCode.Succeeded or controller is None:
            raise RuntimeError("Native D3D12 replay could not be opened")
        controller.ConfigureReplayCheckpoints(False, 0, 0)
        actions = []
        def walk(nodes):
            for action in nodes:
                if action.flags & rd.ActionFlags.Drawcall and not action.flags & rd.ActionFlags.Indirect:
                    actions.append(int(action.eventId))
                walk(action.children)
        walk(controller.GetRootActions())
        events = select_draws(actions, args.events)
        evidence = selected_event_evidence(controller, events)
        if any(row["status"] != "exactAPIEvent" for row in evidence):
            raise RuntimeError("Every draw requires an exact existing structured API event")
        report.update(events=events, selectedEventEvidence=evidence)
        maximum = args.max_read_mib * 1024 * 1024
        buffers, buffer_bytes = resolve_buffers(names, controller.GetResources(), controller.GetBuffers(), maximum)
        report["buffers"] = [{"name": name, "resource": str(resource), "bytes": count}
                             for name, resource, count in buffers]
        numbers = set()
        for event in events:
            recorder.measure("discovery.EID%d" % event, lambda e=event:
                controller.SetFrameEvent(e, True), event)
            pipe = controller.GetPipelineState()
            bound = list(pipe.GetOutputTargets()) + [pipe.GetDepthTarget()]
            numbers.update(resource_number(t.resource) for t in bound if t.resource != rd.ResourceId.Null())
        if None in numbers or not 1 <= len(numbers) <= 16:
            raise RuntimeError("All actual outputs must be one to sixteen captured textures")
        textures, requests, estimated = texture_requests(controller.GetTextures(), sorted(numbers), maximum - buffer_bytes)
        report.update(textures=textures, estimatedReadbackBytes=estimated + buffer_bytes)
        probe = AliasProbe(controller, rd, recorder, report, buffers, requests, sorted(numbers),
                           maximum, args.disable_buffer_aliases)
        recorder.measure("checkpoint.configure", lambda: controller.ConfigureReplayCheckpoints(
            True, args.budget_mib * 1024 * 1024, args.max_checkpoints))
        report["configuredCheckpointStatus"] = probe.status()
        a, b, c, d = events
        for index, event in enumerate((a, d, a, d, c, d, a, b, d, a, c, d)):
            probe.visit("ordinary.%02d" % index, event)
        report["retainedCheckpointStatus"] = probe.status()
        references = {event: [probe.visit("reference%d.EID%d" % (repeat + 1, event), event, True)["witness"]
                              for repeat in range(2)] for event in events}
        report["comparisons"], issues = comparisons(report["visits"], references, events)
        report["issues"] += issues
        report["changingContentEvidence"], issues = changing_content_evidence(references, events)
        report["issues"] += issues
        copies = [v for v in report["visits"] if not v["force"] and v["nativeAliasRestore"]["copyBytes"] > 0]
        report["nativeAliasRestoreVisits"] = len(copies)
        report["nativeAliasRestoreBytes"] = sum(v["nativeAliasRestore"]["copyBytes"] for v in copies)
        if args.disable_buffer_aliases:
            if copies:
                report["issues"].append("Disabled control unexpectedly restored physical alias snapshots")
        elif not copies:
            report["issues"].append("No actual physical alias checkpoint restore was observed")
        recorder.measure("checkpoint.disable", lambda: controller.ConfigureReplayCheckpoints(False, 0, 0))
        report["disabledCheckpointStatus"] = probe.status(disabled=True)
        # Freeing checkpoint-only sources must preserve the currently materialised ordinary result.
        last, _ = probe.read("afterDisable", events[-1])
        if last != references[events[-1]][0]:
            report["issues"].append("Disabling checkpoints changed the current full resource/output state")
        report["measurementComplete"] = True
        report["ok"] = not report["issues"]
        report["verdict"] = ("passedDisabledAliasControl" if args.disable_buffer_aliases else
                             "passedPhysicalBufferAliasRestoration") if report["ok"] else "failedOrInconclusive"
    except Exception as error:
        report.update(ok=False, verdict="failedOrInconclusive", error=str(error), traceback=traceback.format_exc())
    finally:
        def cleanup(label, callback):
            try:
                recorder.measure(label, callback, cleanup=True)
            except Exception as error:
                report.setdefault("cleanupErrors", []).append({"phase": label, "error": str(error)})
                report.update(ok=False, verdict="cleanupFailed")
        if controller is not None:
            cleanup("cleanup.checkpoints", lambda: controller.ConfigureReplayCheckpoints(False, 0, 0))
            cleanup("cleanup.replay", controller.Shutdown)
        if cap is not None:
            cleanup("cleanup.capture", cap.Shutdown)
        if recorder.log_path.is_file():
            native = output.with_suffix(".native.log")
            native.write_bytes(recorder.log_path.read_bytes())
            report["nativeLog"] = str(native)
        if initialized:
            cleanup("cleanup.runtime", rd.ShutdownReplay)
        recorder.publish()
    print(json.dumps({"ok": report["ok"], "output": str(output), "verdict": report.get("verdict")}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
