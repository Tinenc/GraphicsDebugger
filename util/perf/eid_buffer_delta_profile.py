"""Validate two shared scalar edits at a fixed EID in an isolated offline replay process.

Use run_eid_isolated_python.ps1. Running main opens the explicitly named capture and uses the GPU;
importing helpers does not. Binding EIDs and original buffer/absolute offsets are explicit evidence
requests, never trusted substitutes for native reflection and descriptor checks. Run --disable-delta
in a separate process for the control. Bytes/pixels and invalidation cutoffs are mandatory; optional
middle-checkpoint acceptance requires actual retained-version hits. No UI latency or all-UAV claim.
"""
from __future__ import print_function

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import sys
import traceback

from eid_checkpoint_profile import compare_event, parse_status, selected_event_evidence
from eid_history_profile import resource_number
from eid_live_forward_profile import texture_requests, texture_witness
from eid_portable_forward_edit_profile import readback_replay_issues
from eid_shared_buffer_profile import (SharedBufferRecorder, binary_identity, build_identity,
                                       read_window, unchanged_checkpoint_issues, unsigned64)


APIS = {"D3D11": "D3D11BufferPatch", "D3D12": "D3D12BufferPatch",
        "OpenGL": "OpenGLBufferPatch", "OpenGLES": "OpenGLBufferPatch", "Vulkan": "VulkanBufferPatch"}
PREPARATION_LIVE_FORWARD = {
    "D3D11": ("RENDERDOC_D3D11_ENABLE_LIVE_FORWARD", "0"),
    "D3D12": ("RENDERDOC_D3D12_DISABLE_LIVE_FORWARD", "1"),
    "OpenGL": ("RENDERDOC_GL_ENABLE_LIVE_FORWARD", "0"),
    "OpenGLES": ("RENDERDOC_GL_ENABLE_LIVE_FORWARD", "0"),
    "Vulkan": ("RENDERDOC_VULKAN_ENABLE_LIVE_FORWARD", "0"),
}
UINT64_MAX = (1 << 64) - 1


def scalar_bytes(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Scalar values must be finite")
    try:
        result = struct.pack("<f", value)
    except (OverflowError, struct.error):
        raise ValueError("Scalar value does not fit a 32-bit float")
    if not math.isfinite(struct.unpack("<f", result)[0]):
        raise ValueError("Scalar value does not fit a finite 32-bit float")
    return result


def changed_runs(before, after):
    if len(before) != len(after):
        raise ValueError("Scalar delta comparison requires the same byte layout")
    runs, changed, in_run = 0, 0, False
    for left, right in zip(before, after):
        different = left != right
        changed += int(different)
        runs += int(different and not in_run)
        in_run = different
    return runs, changed


def compact_upload_evidence(records, api, mode, before, after, active_bytes):
    """An allocation/copy claim requires the exact native commit, separately from pixel parity."""
    if api not in ("OpenGL", "OpenGLES") or mode not in ("compact", "full"):
        raise ValueError("Compact upload acceptance is specific to GL/GLES and an explicit mode")
    count = changed_runs(before, after)[1]
    if not 0 < count <= len(after) <= active_bytes:
        raise ValueError("Upload comparison needs a changed stable layout and its full active payload")
    rows = [r for r in records if r.get("api") == APIS[api] and r.get("op") == "set"]
    expected = {"uploadReused": int(mode == "compact"),
                "uploadWrittenBytes": count if mode == "compact" else active_bytes}
    issues = []
    if len(rows) != 1:
        issues.append("Upload acceptance needs exactly one native commit record")
    else:
        for key, value in expected.items():
            if type(rows[0].get(key)) is not int or rows[0][key] != value:
                issues.append("Upload %s must equal %d" % (key, value))
    return {"mode": mode, "expected": expected, "records": rows, "issues": issues}


def configure_environment(disabled, checkpoints=False, environ=None):
    environ = os.environ if environ is None else environ
    requested = {"RENDERDOC_EID_TIMING": "1", "RENDERDOC_REPLAY_CHECKPOINTS": "1" if checkpoints else "0",
                 "RENDERDOC_REPLAY_DISABLE_BUFFER_PATCH_DELTA": "1" if disabled else "0",
                 "RENDERDOC_D3D12_DISABLE_BUFFER_PATCH_PREFIX": "0"}
    for key, value in requested.items():
        environ[key] = value
    return {key: environ.get(key) for key in requested}


def validate_parameters(args):
    if not 0 < args.early_event < args.late_event <= args.fixed_event < (1 << 32):
        raise ValueError("Require 0 < early-event < late-event <= fixed-event < 2^32")
    for name in ("early", "late"):
        number, offset = getattr(args, name + "_buffer"), getattr(args, name + "_offset")
        if not 0 < number <= UINT64_MAX or not 0 <= offset <= UINT64_MAX - 4 or offset % 4:
            raise ValueError("Each original buffer and aligned four-byte offset must be valid")
    same = args.early_buffer == args.late_buffer
    if same != args.same_buffer_ranges:
        raise ValueError("Use --same-buffer-ranges exactly when both original buffers are identical")
    if same and abs(args.early_offset - args.late_offset) < 4:
        raise ValueError("The two scalar edit ranges must not overlap")
    if not 0 <= args.neighbors <= 4096 or not 1 <= args.max_read_mib <= 4096 or args.max_seconds <= 0:
        raise ValueError("Invalid readback or time budget")
    values = [scalar_bytes(value.strip()) for value in args.late_values.split(",")]
    if not 2 <= len(values) <= 8 or len(set(values)) != len(values):
        raise ValueError("late-values must name two to eight distinct float32 representations")
    early = scalar_bytes(args.early_value)
    if args.expected_cutoff is None:
        args.expected_cutoff = args.late_event
    if args.expected_disabled_cutoff is None:
        args.expected_disabled_cutoff = args.early_event
    if not 0 <= args.expected_disabled_cutoff < args.expected_cutoff <= args.late_event:
        raise ValueError("Expected enabled cutoff must improve on the disabled cutoff")
    if args.checkpoint_event is not None and not args.early_event < args.checkpoint_event < args.expected_cutoff:
        raise ValueError("checkpoint-event must be strictly between early consumption and the late cutoff")
    if not 1 <= args.budget_mib <= 4096 or not 1 <= args.max_checkpoints <= 64:
        raise ValueError("Checkpoint budget and count must be bounded positive values")
    if args.require_checkpoint_reuse and (args.checkpoint_event is None or len(values) < 3):
        raise ValueError("Checkpoint reuse acceptance requires a middle EID and at least three late values")
    return early, values


def member_component(binding_offset, descriptor_size, block_size, member_offset, columns, absolute):
    """Validate one float component against both reflected storage and the active descriptor."""
    metadata = (binding_offset, descriptor_size, block_size, member_offset, absolute)
    if any(type(value) is not int or not 0 <= value <= UINT64_MAX for value in metadata):
        raise ValueError("Invalid unsigned reflected or bound range")
    if not 1 <= columns <= 4 or absolute < binding_offset:
        raise ValueError("Requested offset is outside the bound scalar/vector")
    relative = absolute - binding_offset
    if relative < member_offset or (relative - member_offset) % 4 or relative - member_offset >= columns * 4:
        raise ValueError("Requested offset is not a float component of this member")
    if (relative > block_size or 4 > block_size - relative or absolute > UINT64_MAX - 4 or
            (descriptor_size != UINT64_MAX and
             (relative > descriptor_size or 4 > descriptor_size - relative))):
        raise ValueError("Scalar exceeds the reflected block or active CBuffer view")
    return (relative - member_offset) // 4


def bound_member(controller, rd, stage, event, number, offset):
    pipe = controller.GetPipelineState()
    reflection = pipe.GetShaderReflection(stage)
    if reflection is None:
        raise RuntimeError("Requested stage has no native reflection at EID%d" % event)
    matches = []
    for index, block in enumerate(reflection.constantBlocks):
        if (not block.bufferBacked or block.inlineDataBytes or block.compileConstants or
                int(block.bindArraySize) != 1):
            continue
        descriptor = pipe.GetConstantBlock(stage, index, 0).descriptor
        if resource_number(descriptor.resource) != number:
            continue
        for member in block.variables:
            if (member.type.baseType != rd.VarType.Float or int(member.type.rows) != 1 or
                    int(member.type.elements) != 1):
                continue
            try:
                component = member_component(int(descriptor.byteOffset), int(descriptor.byteSize),
                    int(block.byteSize), int(member.byteOffset), int(member.type.columns), offset)
            except ValueError:
                continue
            matches.append({"eventId": event, "buffer": number, "absoluteOffset": offset,
                            "bytes": 4, "blockIndex": index, "member": str(member.name),
                            "component": component, "bindingOffset": int(descriptor.byteOffset),
                            "bindingByteSize": int(descriptor.byteSize),
                            "memberOffset": int(member.byteOffset), "blockSize": int(block.byteSize)})
    if len(matches) != 1:
        raise RuntimeError("Exactly one actual bound scalar/vector component must prove EID%d buffer %d offset %d" %
                           (event, number, offset))
    return matches[0]


def validate_binding_pair(early, late, same):
    if early["eventId"] >= late["eventId"]:
        raise ValueError("Binding evidence must be ordered")
    if same:
        if early["buffer"] != late["buffer"] or early["absoluteOffset"] == late["absoluteOffset"]:
            raise ValueError("Same-buffer evidence must identify distinct storage ranges")
        # The earlier active view must not cover the later scalar: otherwise this fixture cannot
        # distinguish resource-wide consumers from a correctly conservative byte-range index.
        begin, count = early["bindingOffset"], early["bindingByteSize"]
        offset = late["absoluteOffset"]
        if count == UINT64_MAX or (offset < begin + count and offset + 4 > begin):
            raise ValueError("Early binding overlaps the later scalar; no positive range-index fixture")
    elif early["buffer"] == late["buffer"]:
        raise ValueError("Distinct-buffer mode needs two actual original buffers")


def delta_evidence(records, api, event, generation, before, after, cutoff, disabled, same):
    issues = []
    rows = [row for row in records if row.get("api") == APIS[api] and row.get("op") == "set"]
    runs, count = changed_runs(before, after)
    expected = {"firstAffected": cutoff, "deltaKnown": 1, "deltaEnabled": int(not disabled),
                "changedRanges": runs, "changedBytes": count, "changedBuffers": 1}
    if len(rows) != 1:
        issues.append("Missing unique native buffer-delta commit record")
    else:
        for key, value in expected.items():
            if type(rows[0].get(key)) is not int or rows[0][key] != value:
                issues.append("Native %s must equal %d" % (key, value))
        if same and api in ("D3D11", "D3D12", "OpenGL", "OpenGLES", "Vulkan") and not disabled and rows[0].get("rangeIndexKnown") != 1:
            issues.append("Same-buffer positive case lacks a complete native range-read index")
    controller = [row for row in records if row.get("api") == "ReplayController" and
                  row.get("op") == "shared_buffer_patch"]
    expected_controller = {"event": event, "generation": generation, "first_affected_event": cutoff,
                           "ranges": 2, "bytes": 8}
    if len(controller) != 1 or any(controller[0].get(key) != value
                                   for key, value in expected_controller.items()):
        issues.append("Controller event, generation, active patches, or cutoff disagrees with native evidence")
    return {"nativeRecords": rows, "expected": expected, "issues": issues}


def checkpoint_evidence(before, after, early_event, cutoff, requested):
    candidates = [event for event in before.get("events", [])
                  if type(event) is int and early_event < event < cutoff]
    if requested is not None:
        candidates = [event for event in candidates if event == requested]
    hits = after.get("hits", 0) - before.get("hits", 0)
    builds = after.get("builds", 0) - before.get("builds", 0)
    resume = after.get("lastResumeEvent")
    retained = resume in candidates and resume in after.get("events", [])
    # A newly built checkpoint at the same EID cannot stand in for the previous native version.
    accepted = bool(hits > 0 and builds == 0 and retained)
    return {"accepted": accepted, "candidateEventsBefore": candidates,
            "candidateEventsAfter": [event for event in candidates if event in after.get("events", [])],
            "nativeHits": hits, "nativeBuilds": builds, "lastResumeEvent": resume,
            "reason": "hitRetainedMiddleCheckpointWithoutRebuild" if accepted else
                      "noProvenRetainedMiddleCheckpointHit",
            "before": before, "after": after}


def phase_acceptance(deltas, disabled, require_checkpoint):
    reuse = bool(len(deltas) >= 2 and all(row["checkpointEvidence"]["accepted"] for row in deltas))
    correct_delta = bool(deltas and all(not row["deltaEvidence"]["issues"] for row in deltas))
    invalidation_control = bool(disabled and correct_delta and
        deltas[0]["checkpointEvidence"]["candidateEventsBefore"] and
        all(not row["checkpointEvidence"]["candidateEventsAfter"] and
            not row["checkpointEvidence"]["accepted"] for row in deltas))
    return {"cutoffReductionAccepted": bool(not disabled and correct_delta),
            "checkpointReuseAccepted": reuse,
            "checkpointInvalidationControlAccepted": invalidation_control,
            "issues": ["Required consecutive setters did not prove retained middle-checkpoint hits"]
                      if require_checkpoint and not disabled and not reuse else []}


def expected_window(window, baseline, active, specs):
    result = bytearray(baseline)
    if len(result) != window["bytes"]:
        raise ValueError("Baseline does not cover the complete buffer window")
    for name, value in active.items():
        spec = specs[name]
        if spec["buffer"] != window["buffer"]:
            continue
        begin = max(window["offset"], spec["offset"])
        end = min(window["offset"] + window["bytes"], spec["offset"] + 4)
        if begin < end:
            result[begin - window["offset"]:end - window["offset"]] = value[begin - spec["offset"]:end - spec["offset"]]
    return bytes(result)


def fixture_event_evidence(controller, rd, args):
    events = sorted(set((args.early_event, args.late_event, args.fixed_event)))
    checked = sorted(set(events + ([] if args.checkpoint_event is None else [args.checkpoint_event])))
    evidence = selected_event_evidence(controller, checked)
    if any(row["status"] != "exactAPIEvent" for row in evidence):
        raise RuntimeError("Binding/fixed EIDs must be exact captured API events")
    actions = {}
    def walk(nodes):
        for action in nodes:
            actions[int(action.eventId)] = action
            walk(action.children)
    walk(controller.GetRootActions())
    for event in events:
        action = actions.get(event)
        if action is None or not action.flags & rd.ActionFlags.Drawcall or action.flags & rd.ActionFlags.Indirect:
            raise RuntimeError("This bounded fixture probe requires actual direct draw EIDs")
    # The retained checkpoint can be a native submission/Close boundary. It is not a shader
    # binding to inspect or draw to select. Exact API evidence is required above; preparation
    # and each measured setter separately require that exact native checkpoint to be retained.
    return events, evidence


def discover(controller, rd, recorder, report, args, maximum):
    events, evidence = fixture_event_evidence(controller, rd, args)
    buffers = {resource_number(buffer.resourceId): buffer for buffer in controller.GetBuffers()}
    specs, windows = {}, []
    for name in ("early", "late"):
        number, offset = getattr(args, name + "_buffer"), getattr(args, name + "_offset")
        if number not in buffers:
            raise RuntimeError("Requested ID is absent from the original captured buffer list")
        begin, count, _ = read_window(int(buffers[number].length), offset, 4, args.neighbors)
        specs[name] = {"buffer": number, "resource": buffers[number].resourceId, "offset": offset}
        windows.append({"buffer": number, "offset": begin, "bytes": count})
    outputs, bindings = set(), {}
    for event in events:
        recorder.measure("discovery.EID%d" % event, lambda e=event: controller.SetFrameEvent(e, True), event)
        if int(controller.GetCurrentEventID()) != event:
            raise RuntimeError("Discovery selected a different EID")
        for name in ("early", "late"):
            if event == getattr(args, name + "_event"):
                spec = specs[name]
                bindings[name] = bound_member(controller, rd, getattr(rd.ShaderStage, args.stage), event,
                                               spec["buffer"], spec["offset"])
        pipe = controller.GetPipelineState()
        outputs.update(resource_number(target.resource) for target in
                       list(pipe.GetOutputTargets()) + [pipe.GetDepthTarget()]
                       if target.resource != rd.ResourceId.Null())
    validate_binding_pair(bindings["early"], bindings["late"], args.same_buffer_ranges)
    if None in outputs or not 1 <= len(outputs) <= 16:
        raise RuntimeError("Every discovered bound output must fit one to sixteen actual textures")
    descriptions, requests, estimated = texture_requests(controller.GetTextures(), sorted(outputs),
                                                        maximum - sum(window["bytes"] for window in windows))
    report.update(selectedEventEvidence=evidence, sharedBindings=bindings, windows=windows,
                  textures=descriptions, estimatedReadbackBytes=estimated + sum(w["bytes"] for w in windows))
    return specs, windows, requests, outputs


class DeltaProbe:
    def __init__(self, controller, rd, recorder, report, args, specs, windows, requests, outputs):
        self.controller, self.rd, self.recorder, self.report, self.args = controller, rd, recorder, report, args
        self.specs, self.windows, self.requests, self.outputs = specs, windows, requests, outputs
        self.active, self.original = {}, None
        self.generation = int(controller.GetSharedBufferPatchGeneration())

    def read(self, label):
        controller, event = self.controller, self.args.fixed_event
        if int(controller.GetCurrentEventID()) != event:
            raise RuntimeError("Fixed-EID witness observed a different event before readback")
        status_before = parse_status(controller.GetReplayCheckpointStatus())
        def callback():
            raw_windows, rows = [], []
            for index, window in enumerate(self.windows):
                resource = next(spec["resource"] for spec in self.specs.values() if spec["buffer"] == window["buffer"])
                raw = bytes(controller.GetBufferData(resource, window["offset"], window["bytes"]))
                if len(raw) != window["bytes"]:
                    raise RuntimeError("Incomplete scalar/neighbor buffer readback")
                if self.original is not None and raw != expected_window(window, self.original[index], self.active, self.specs):
                    raise RuntimeError("Active scalar or neighboring bytes differ from the expected storage contents")
                raw_windows.append(raw)
                rows.append(dict(window, hex=raw.hex(), sha256=hashlib.sha256(raw).hexdigest()))
            output = texture_witness(controller, self.rd, self.requests, self.outputs,
                                     self.args.max_read_mib * 1024 * 1024 - sum(w["bytes"] for w in self.windows))
            if self.original is None:
                self.original = raw_windows
            return {"buffers": rows, "output": output}
        witness, timing = self.recorder.measure(label + ".readback", callback, event)
        implicit_replay = readback_replay_issues(timing["timingRecords"])
        if implicit_replay:
            raise RuntimeError("; ".join(implicit_replay))
        status_after = parse_status(controller.GetReplayCheckpointStatus())
        if (int(controller.GetCurrentEventID()) != event or witness["output"]["eventId"] != event or
                int(controller.GetSharedBufferPatchGeneration()) != self.generation):
            raise RuntimeError("Readback changed EID or exposed a stale mutation generation")
        self.report["issues"].extend(label + ": " + issue for issue in
                                     unchanged_checkpoint_issues(status_before, status_after))
        return witness, timing

    def change(self, label, name, value, require_delta=False):
        controller, event = self.controller, self.args.fixed_event
        if int(controller.GetCurrentEventID()) != event:
            raise RuntimeError("Scalar setter must execute without switching the fixed EID")
        previous, before_generation = self.active.get(name), self.generation
        before_status = parse_status(controller.GetReplayCheckpointStatus())
        if name is None:
            callback = controller.ClearSharedBufferPatches
        else:
            spec = self.specs[name]
            callback = ((lambda: controller.SetSharedBufferPatch(spec["resource"], spec["offset"], value))
                        if value is not None else
                        (lambda: controller.ClearSharedBufferPatch(spec["resource"], spec["offset"], 4)))
        error, timing = self.recorder.measure(label + ".setter", callback, event)
        if str(error):
            raise RuntimeError("Native scalar edit rejected: " + str(error))
        if name is None:
            self.active.clear()
        elif value is None:
            self.active.pop(name)
        else:
            self.active[name] = value
        self.generation = int(controller.GetSharedBufferPatchGeneration())
        if self.generation <= before_generation:
            raise RuntimeError("Accepted changed scalar did not advance mutation generation")
        # Crucially, this read precedes every explicit replay/reference call after the setter.
        witness, readback = self.read(label + ".immediate")
        status = parse_status(controller.GetReplayCheckpointStatus())
        row = {"label": label, "eventId": event, "generationBefore": before_generation,
               "generationAfter": self.generation, "activeValues": {k: v.hex() for k, v in self.active.items()},
               "setterWallMs": timing["wallMs"], "readbackWallMs": readback["wallMs"],
               "timingRecords": timing["timingRecords"], "immediateWitness": witness,
               "firstAffectedEvent": status.get("sharedBufferPatchFirstAffectedEvent")}
        if require_delta:
            cutoff = (self.args.expected_disabled_cutoff if self.args.disable_delta else self.args.expected_cutoff)
            row["deltaEvidence"] = delta_evidence(timing["timingRecords"], self.report["captureDriver"], event,
                self.generation, previous, value, cutoff, self.args.disable_delta, self.args.same_buffer_ranges)
            self.report["issues"].extend(label + ": " + issue for issue in row["deltaEvidence"]["issues"])
            if getattr(self.args, "expect_upload_reuse", None):
                row["uploadEvidence"] = compact_upload_evidence(timing["timingRecords"],
                    self.report["captureDriver"], self.args.expect_upload_reuse, previous, value,
                    sum(len(payload) for payload in self.active.values()))
                self.report["issues"].extend(label + ": " + issue for issue in row["uploadEvidence"]["issues"])
            if status.get("sharedBufferPatchFirstAffectedEvent") != cutoff:
                self.report["issues"].append(label + ": status cutoff differs from the expected native boundary")
            row["checkpointEvidence"] = checkpoint_evidence(before_status, status, self.args.early_event,
                self.args.expected_cutoff, self.args.checkpoint_event)
        self.report["mutations"].append(row)
        self.recorder.publish()
        return witness

    def prepare_checkpoint(self, environ=None):
        if self.args.checkpoint_event is None:
            self.report["checkpointPreparation"] = {"requested": False}
            return
        environ = os.environ if environ is None else environ
        key, disabled_value = PREPARATION_LIVE_FORWARD[self.report["captureDriver"]]
        previous_value = environ.get(key)
        preparation = {"requested": True, "visits": [], "maximumRounds": 3,
            "candidatePresent": False, "liveForwardOverride": {
                "variable": key, "previousValue": previous_value, "preparationValue": disabled_value}}
        self.report["checkpointPreparation"] = preparation

        def visit(label, event):
            before = parse_status(self.controller.GetReplayCheckpointStatus())
            _, timing = self.recorder.measure(label, lambda: self.controller.SetFrameEvent(event, False), event)
            if (int(self.controller.GetCurrentEventID()) != event or
                    int(self.controller.GetSharedBufferPatchGeneration()) != self.generation):
                raise RuntimeError("Checkpoint preparation changed the requested EID or edit generation")
            after = parse_status(self.controller.GetReplayCheckpointStatus())
            preparation["visits"].append({"eventId": event, "before": before, "after": after,
                                           "timingRecords": timing["timingRecords"]})
            return after

        try:
            # Forward continuation deliberately bypasses checkpoint saves on several backends.
            # Disable it only for this bounded preparation, preserving the process's exact setting
            # for all measured scalar setters. Normal replay and the native checkpoint policy stay on.
            environ[key] = disabled_value
            for repeat in range(preparation["maximumRounds"]):
                # A checkpoint stores state AFTER its EID; selecting that same draw only gives a
                # WithoutDraw prefix ending one event earlier. Replay to the later consumer instead,
                # so its prefix includes the requested middle point without executing that consumer.
                # Visiting the early consumer first also lets the native edit anchor be built before
                # it would otherwise take precedence over a newly selected middle candidate.
                visit("checkpointPreparation.round%d.early" % (repeat + 1), self.args.early_event)
                after = visit("checkpointPreparation.round%d.late" % (repeat + 1), self.args.late_event)
                if self.args.checkpoint_event in after.get("events", []):
                    break
            after = visit("checkpointPreparation.returnFixed", self.args.fixed_event)
            preparation["candidatePresent"] = self.args.checkpoint_event in after.get("events", [])
            preparation["reason"] = ("requestedMiddleCheckpointPresent" if preparation["candidatePresent"] else
                                     "nativePolicyDidNotRetainRequestedMiddleCheckpoint")
        finally:
            if previous_value is None:
                environ.pop(key, None)
            else:
                environ[key] = previous_value
            preparation["liveForwardOverride"]["restoredValue"] = environ.get(key)
            self.recorder.publish()
        witness, _ = self.read("checkpointPreparation.fixedWitness")
        preparation["fixedWitness"] = witness
        self.recorder.publish()

    def references(self, label):
        references = []
        event = self.args.fixed_event
        native_api = "OpenGL" if self.report["captureDriver"] == "OpenGLES" else self.report["captureDriver"]
        for repeat in range(2):
            prefix = "%s.reference%d" % (label, repeat + 1)
            before = parse_status(self.controller.GetReplayCheckpointStatus())
            _, timing = self.recorder.measure(prefix + ".replay", lambda: self.controller.SetFrameEvent(event, True), event)
            if not any(row.get("api") == native_api and row.get("type") == "WithoutDraw"
                       for row in timing["timingRecords"]):
                raise RuntimeError("Forced reference lacks an independent native prefix replay")
            after = parse_status(self.controller.GetReplayCheckpointStatus())
            self.report["issues"].extend(prefix + ": " + issue for issue in unchanged_checkpoint_issues(before, after))
            witness, _ = self.read(prefix)
            references.append(witness)
        self.report["references"][label] = references
        if references[0] != references[1]:
            raise RuntimeError("Two independent forced references disagree for " + label)
        self.recorder.publish()
        return references


def comparison(label, immediate, references, baseline, original=False):
    status = compare_event([immediate], references)
    stable = len(references) == 2 and references[0] == references[1]
    changed = stable and references[0]["output"]["pixels"] != baseline["output"]["pixels"]
    if stable and original and references[0] != baseline:
        status = "failedOriginalNotRecovered"
    if stable and not original and not changed:
        status = "failedNoVisibleScalarEffect"
    return {"label": label, "status": status, "outputChanged": changed}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("capture", "output"):
        parser.add_argument("--" + name, required=True)
    for name in ("early-event", "late-event", "fixed-event"):
        parser.add_argument("--" + name, type=int, required=True)
    for name in ("early-buffer", "early-offset", "late-buffer", "late-offset"):
        parser.add_argument("--" + name, type=unsigned64, required=True)
    parser.add_argument("--same-buffer-ranges", action="store_true")
    parser.add_argument("--stage", choices=("Vertex", "Hull", "Domain", "Geometry", "Pixel"), default="Pixel")
    parser.add_argument("--early-value", default="0.25")
    parser.add_argument("--late-values", default="0.375,0.75,0.875")
    parser.add_argument("--expected-cutoff", type=int)
    parser.add_argument("--expected-disabled-cutoff", type=int)
    parser.add_argument("--disable-delta", action="store_true")
    parser.add_argument("--expect-upload-reuse", choices=("compact", "full"),
                        help="Require GL/GLES native changed-byte reuse or its full-payload control")
    parser.add_argument("--checkpoint-event", type=int)
    parser.add_argument("--budget-mib", type=int, default=256)
    parser.add_argument("--max-checkpoints", type=int, default=4)
    parser.add_argument("--require-checkpoint-reuse", action="store_true")
    parser.add_argument("--neighbors", type=int, default=16)
    parser.add_argument("--max-read-mib", type=int, default=256)
    parser.add_argument("--max-seconds", type=int, default=360)
    parser.add_argument("--build-label", default="unspecified")
    args = parser.parse_args(argv)
    try:
        early_value, late_values = validate_parameters(args)
        capture, output = Path(args.capture).resolve(), Path(args.output).resolve()
        root = Path(__file__).resolve().parents[2]
        output.relative_to(root)
        config = Path(os.environ.get("RENDERDOC_CONFIG_DIR", "")).resolve()
        config.relative_to(root)
        if not os.environ.get("RENDERDOC_CONFIG_DIR") or not config.is_dir():
            raise ValueError("Run with the worktree-owned isolated Python launcher/configuration")
        if not capture.is_file() or output.exists():
            raise ValueError("Capture must exist and output must be a new worktree evidence path")
    except (ValueError, OSError) as error:
        parser.error(str(error))
    output.parent.mkdir(parents=True, exist_ok=True)
    environment = configure_environment(args.disable_delta, args.checkpoint_event is not None)
    environment["RENDERDOC_GL_DISABLE_BUFFER_PATCH_UPLOAD_REUSE"] = os.environ.get(
        "RENDERDOC_GL_DISABLE_BUFFER_PATCH_UPLOAD_REUSE")
    report = {"schema": "renderdoc-buffer-delta-profile/v1", "ok": False, "measurementComplete": False,
              "capture": str(capture), "configuration": vars(args), "environment": environment,
              "processId": os.getpid(), "samples": [], "mutations": [], "references": {}, "issues": [],
              "limits": ["Explicit actual bound scalar/vector float32 components; no layout inference",
                         "Complete discovered color/depth textures and both scalar/neighbor windows",
                         "Checkpoint cutoff reduction and actual retained-version hits are separately accepted",
                         "No shader replacement or all-UAV coverage in this probe",
                         "Immediate witnesses precede explicit reference replays; no UI latency claim"]}
    recorder = SharedBufferRecorder(report, output, output.with_suffix(".rdoc.log"), args.max_seconds)
    recorder.publish()
    rd, controller, cap, initialized = None, None, None, False
    try:
        import renderdoc as rd
        identity = build_identity(rd, None)
        if not identity["nativeModules"]:
            raise RuntimeError("Cannot prove the actual loaded replay module identity")
        for entry in [identity["pythonModule"]] + identity["nativeModules"]:
            Path(entry["path"]).resolve().relative_to(root)
        report["buildIdentity"] = identity
        rd.InitialiseReplay(rd.GlobalEnvironment(), [])
        initialized = True
        rd.SetDebugLogFile(str(recorder.log_path))
        report["captureIdentity"], _ = recorder.measure("capture.identity", lambda: binary_identity(capture))
        cap = rd.OpenCaptureFile()
        result, _ = recorder.measure("capture.openFile", lambda: cap.OpenFile(str(capture), "", None))
        if result != rd.ResultCode.Succeeded:
            raise RuntimeError("OpenFile failed: " + str(result))
        report["captureDriver"] = str(cap.DriverName())
        if args.expect_upload_reuse and (report["captureDriver"] not in ("OpenGL", "OpenGLES") or
                                         args.disable_delta):
            raise ValueError("Upload reuse comparison requires GL/GLES with delta invalidation enabled")
        if report["captureDriver"] not in APIS:
            raise RuntimeError("Unsupported buffer-delta fixture backend")
        opened, _ = recorder.measure("capture.openReplay", lambda: cap.OpenCapture(rd.ReplayOptions(), None))
        result, controller = opened
        if result != rd.ResultCode.Succeeded or controller is None:
            raise RuntimeError("OpenCapture failed: " + str(result))
        controller.ConfigureReplayCheckpoints(args.checkpoint_event is not None,
                                              args.budget_mib * 1024 * 1024, args.max_checkpoints)
        run = DeltaProbe(controller, rd, recorder, report, args, *discover(
            controller, rd, recorder, report, args, args.max_read_mib * 1024 * 1024))
        baseline = run.references("baseline")[0]
        for name, values in (("early", [early_value]), ("late", late_values)):
            window = run.windows[0 if name == "early" else 1]
            offset = run.specs[name]["offset"] - window["offset"]
            raw = run.original[0 if name == "early" else 1][offset:offset + 4]
            if raw in values:
                raise RuntimeError("Every requested scalar must differ from its original bytes")
        # Keep this sequence uninterrupted: a reference replay here would hide stale setter output.
        early_immediate = run.change("slider.early", "early", early_value)
        immediate = [run.change("slider.lateV1", "late", late_values[0])]
        run.prepare_checkpoint()
        # Once the seed/preparation ends, every subsequent value is an uninterrupted fixed-EID edit.
        for index, value in enumerate(late_values[1:], 2):
            immediate.append(run.change("slider.lateV%d" % index, "late", value, require_delta=True))
            if immediate[-2]["output"]["pixels"] == immediate[-1]["output"]["pixels"]:
                report["issues"].append("Consecutive late scalar values did not immediately change native output")
        deltas = [row for row in report["mutations"] if "deltaEvidence" in row]
        acceptance = phase_acceptance(deltas, args.disable_delta, args.require_checkpoint_reuse)
        report["issues"].extend(acceptance.pop("issues"))
        report.update(acceptance)
        run.change("reference.earlyOnly", "late", None)
        results = [comparison("earlyOnly", early_immediate, run.references("earlyOnly"), baseline)]
        for index, value in enumerate(late_values):
            label = "bothV%d" % (index + 1)
            run.change("reference.install." + label, "late", value)
            results.append(comparison(label, immediate[index], run.references(label), baseline))
        undone = run.change("slider.undoAll", None, None)
        results.append(comparison("undo", undone, run.references("undo"), baseline, original=True))
        report["comparisons"] = results
        report["issues"].extend(row["label"] + ": " + row["status"] for row in results if row["status"] != "passed")
        report.update(measurementComplete=True, ok=not report["issues"])
        report["verdict"] = ("passedDisabledDeltaControl" if args.disable_delta else "passedFixedEIDScalarDelta") if report["ok"] else "failedOrInconclusive"
    except Exception as error:
        report.update(ok=False, error=str(error), traceback=traceback.format_exc(), verdict="failedOrInconclusive")
    finally:
        def cleanup(label, callback):
            try:
                value, _ = recorder.measure(label, callback, cleanup=True)
                if isinstance(value, str) and value:
                    raise RuntimeError(value)
            except Exception as error:
                report.setdefault("cleanupErrors", []).append({"phase": label, "error": str(error)})
                report.update(ok=False, verdict="cleanupFailed")
        if controller is not None:
            cleanup("cleanup.sharedUndo", controller.ClearSharedBufferPatches)
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
    print(json.dumps({"ok": report["ok"], "output": str(output), "verdict": report.get("verdict")}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
