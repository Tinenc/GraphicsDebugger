"""Validate shared float4 edits and live-forward invalidation on four-draw native fixtures.

Main opens only the named capture and uses the GPU. Coordinate GPU ownership before running.
Two values are first applied consecutively at one EID, with immediate native witnesses and no
intervening SetFrameEvent. Each identical value configuration then gets navigation, real-hit
requirements, and independent forced references at every shared draw. Undo must recover baseline.
An optional supplied fixture shader uses the checked native replacement transaction. This proves
native identity/pipeline acceptance, not the Shader Workbench's separate complete ABI validation.
While that replacement remains installed, both live values and their undo are tested again against
independent shader-only references before the replacement itself is removed.
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

from eid_checkpoint_profile import compare_event, parse_status, selected_event_evidence, status_issues
from eid_history_profile import Recorder, resource_number
from eid_live_forward_profile import texture_requests
from eid_portable_forward_profile import (BACKENDS, complete_witness, live_evidence,
                                          profile_environment, select_events, viewport_probe)
from eid_shared_buffer_profile import (binary_identity, build_identity, byte_witness, read_window,
                                       unchanged_checkpoint_issues)


def values_from_hex(text):
    values = [bytes.fromhex(item.strip()) for item in text.split(",")]
    if len(values) != 2 or values[0] == values[1] or any(len(value) != 16 for value in values):
        raise ValueError("Provide exactly two distinct 16-byte float4 values")
    if any(not all(math.isfinite(f) for f in struct.unpack("<4f", value)) for value in values):
        raise ValueError("Fixture float4 values must be finite")
    return values


def shared_identity(bindings):
    if len(bindings) != 4:
        raise ValueError("All four actual draws must expose the shared member")
    identities = {(row["buffer"], row["absoluteOffset"], row["bytes"]) for row in bindings}
    if len(identities) != 1 or any(row["bytes"] != 16 or not row["buffer"] or
                                  row["absoluteOffset"] < 0 for row in bindings):
        raise ValueError("Draws do not share one original buffer and exact 16-byte member range")
    return next(iter(identities))


def shared_member_offset(binding_offset, descriptor_size, member_offset, block_size):
    """Check the member relative to BOTH the reflected block and the currently bound view."""
    maximum = (1 << 64) - 1
    if any(type(value) is not int or not 0 <= value <= maximum for value in
           (binding_offset, descriptor_size, member_offset, block_size)):
        raise ValueError("Shared member metadata must use valid unsigned byte ranges")
    if member_offset > block_size or 16 > block_size - member_offset:
        raise ValueError("Shared float4 exceeds its reflected constant block")
    # Vulkan's whole-range sentinel is bounded later against the actual captured buffer.
    # Zero is an empty Descriptor range, not the BoundVBuffer whole-buffer sentinel.
    if descriptor_size != maximum and (member_offset > descriptor_size or
                                       16 > descriptor_size - member_offset):
        raise ValueError("Shared float4 exceeds the currently bound CBuffer descriptor range")
    if binding_offset > maximum - member_offset or binding_offset + member_offset > maximum - 16:
        raise ValueError("Shared float4 absolute buffer range overflows")
    return binding_offset + member_offset


def mutation_issues(records, event, before_generation, after_generation, expected_bytes):
    issues = []
    if after_generation <= before_generation:
        issues.append("Accepted shared edit did not advance its mutation generation")
    rows = [row for row in records if row.get("api") == "ReplayController" and
            row.get("op") == "shared_buffer_patch"]
    if len(rows) != 1 or rows[0].get("event") != event or rows[0].get("generation") != after_generation:
        issues.append("Missing exact controller shared-edit generation/event evidence")
    elif rows[0].get("bytes") != expected_bytes or rows[0].get("ranges") != int(expected_bytes > 0):
        issues.append("Controller shared-edit byte range differs from the accepted configuration")
    if any(row.get("hit") == 1 for row in records if row.get("api") in set(BACKENDS.values())):
        issues.append("Fixed-EID mutation reused a live cursor from the previous edit generation")
    return issues


def compare_configuration(visits, references, baseline, events, immediate=None, require_original=False):
    rows, issues = [], []
    for event in events:
        canonical = references.get(event, [])
        observed = [row["witness"] for row in visits if row["eventId"] == event]
        if immediate is not None and immediate["output"]["eventId"] == event:
            observed.append(immediate)
        verdict = compare_event(observed, canonical)
        stable = len(canonical) == 2 and canonical[0] == canonical[1]
        own_region = lambda witness: next((r for r in witness["output"]["regions"]
                                           if r["eventId"] == event), None)
        original = baseline[event]
        before, after = own_region(original), own_region(canonical[0]) if stable else None
        changed = bool(stable and before and after and before["uintBits"] != after["uintBits"])
        if stable and require_original and canonical[0] != original:
            verdict = "failedOriginalStateNotRecovered"
        if not require_original and stable and not changed:
            issues.append("EID%d shared consumer has no changed own-region output" % event)
        rows.append({"eventId": event, "status": verdict, "ownRegionChanged": changed,
                     "observedVisits": len(observed), "equalsOriginal": stable and canonical[0] == original})
        if verdict != "passed":
            issues.append("EID%d: %s" % (event, verdict))
    return rows, issues


def navigation(events):
    a, b, c, d = events
    # The caller's fixed edit endpoint is B. First test whether any continued B->D state is new.
    return [("postEditBD", d), ("backwardDA", a), ("AB", b), ("BD", d),
            ("backwardDA2", a), ("AC", c), ("CD", d)]


def readback_replay_issues(records):
    """A correct final image is insufficient if the witness getter repaired stale replay state."""
    native = {"D3D11", "D3D12", "OpenGL", "Vulkan", "ReplayDriverBoundary", "ReplayChunk"}
    return ["Resource readback performed implicit replay: api=%s type=%s" %
            (row.get("api"), row.get("type", "materialize")) for row in records
            if row.get("api") in ("MaterializeEvent", "ReplayLog") or
            (row.get("api") in native and "type" in row)]


class EditProbe:
    def __init__(self, controller, rd, recorder, report, api, events, requests, resources, probes,
                 buffer, offset, window, maximum, disabled, min_hits):
        self.controller, self.rd, self.recorder, self.report = controller, rd, recorder, report
        self.api, self.events, self.requests, self.resources, self.probes = api, events, requests, resources, probes
        self.buffer, self.offset, self.window, self.maximum = buffer, offset, window, maximum
        self.disabled, self.min_hits = disabled, min_hits
        self.generation = int(controller.GetSharedBufferPatchGeneration())
        self.baseline = {}

    def issue(self, label, values):
        self.report["issues"].extend(label + ": " + value for value in values)

    def status(self):
        value = parse_status(self.controller.GetReplayCheckpointStatus())
        self.issue("checkpointStatus", status_issues(value, disabled=True))
        return value

    def read(self, label, event, expected=None):
        controller, rd = self.controller, self.rd
        begin, count, relative = self.window
        before = self.status()
        def witness():
            raw = bytes(controller.GetBufferData(self.buffer, begin, count))
            if len(raw) != count or (expected is not None and raw[relative:relative + 16] != expected):
                raise RuntimeError("Native shared member bytes do not match the active value")
            return {"buffer": byte_witness(raw, relative, 16), "output": complete_witness(
                controller, rd, self.requests, self.resources, self.probes, self.maximum - count)}
        value, timing = self.recorder.measure(label + ".readback", witness, event)
        implicit_replay = readback_replay_issues(timing["timingRecords"])
        if implicit_replay:
            raise RuntimeError("; ".join(implicit_replay))
        if (int(controller.GetCurrentEventID()) != event or value["output"]["eventId"] != event or
                int(controller.GetSharedBufferPatchGeneration()) != self.generation):
            raise RuntimeError("Readback exposed a stale EID or changed patch generation")
        self.issue(label, unchanged_checkpoint_issues(before, self.status()))
        self.report["totalReadbackBytes"] += count + sum(row["bytes"] for row in value["output"]["pixels"])
        return value, timing

    def visit(self, label, event, force=False, expected=None):
        previous, before = int(self.controller.GetCurrentEventID()), self.status()
        _, timing = self.recorder.measure(label + ".replay",
            lambda: self.controller.SetFrameEvent(event, force), event)
        evidence = live_evidence(timing["timingRecords"], self.api, previous, event,
                                 self.disabled, force, allow_hit=not force and event > previous)
        self.issue(label, evidence["issues"])
        native_api = "OpenGL" if self.api == "OpenGLES" else self.api
        if force and not any(row.get("api") == native_api and row.get("type") == "WithoutDraw"
                             for row in timing["timingRecords"]):
            self.issue(label, ["Forced reference lacks an independent native prefix replay"])
        self.issue(label, unchanged_checkpoint_issues(before, self.status()))
        witness, readback = self.read(label, event, expected)
        row = {"label": label, "eventId": event, "force": force, "generation": self.generation,
               "replayWallMs": timing["wallMs"], "readbackWallMs": readback["wallMs"],
               "liveForward": evidence, "witness": witness}
        self.report["visits"].append(row)
        self.recorder.publish()
        return row

    def change(self, label, value):
        controller, event = self.controller, self.events[1]
        if int(controller.GetCurrentEventID()) != event:
            raise RuntimeError("Slider setter must run at the fixed B EID")
        before = self.generation
        callback = (lambda: controller.SetSharedBufferPatch(self.buffer, self.offset, value)) if value is not None else (
            lambda: controller.ClearSharedBufferPatch(self.buffer, self.offset, 16))
        error, timing = self.recorder.measure(label + ".setter", callback, event)
        if str(error):
            raise RuntimeError("Shared setter rejected: " + str(error))
        self.generation = int(controller.GetSharedBufferPatchGeneration())
        self.issue(label, mutation_issues(timing["timingRecords"], event, before, self.generation,
                                          0 if value is None else 16))
        immediate, readback = self.read(label + ".immediate", event, value)
        row = {"label": label, "eventId": event, "generationBefore": before,
               "generationAfter": self.generation, "valueHex": None if value is None else value.hex(),
               "setterWallMs": timing["wallMs"], "readbackWallMs": readback["wallMs"],
               "timingRecords": timing["timingRecords"], "immediateWitness": immediate}
        self.report["mutations"].append(row)
        self.recorder.publish()
        return immediate

    def phase(self, label, expected, immediate=None, original=False):
        observed = [self.visit(label + "." + suffix, event, expected=expected)
                    for suffix, event in navigation(self.events)]
        hits = sum(row["liveForward"]["matchingHits"] for row in observed)
        if (self.disabled and hits) or (not self.disabled and hits < self.min_hits):
            self.issue(label, ["Native positive hit requirement failed: %d observed, %d required" %
                               (hits, 0 if self.disabled else self.min_hits)])
        a, b, c, d = self.events
        references = {event: [self.visit(label + ".reference%d.EID%d" % (repeat + 1, event),
                                         event, True, expected)["witness"] for repeat in range(2)]
                      for event in (a, c, d, b)}
        rows, issues = compare_configuration(observed, references, self.baseline, self.events,
                                               immediate, original)
        self.issue(label, issues)
        result = {"label": label, "matchingForwardHits": hits, "comparisons": rows,
                  "referenceGeneration": self.generation}
        self.report["phases"].append(result)
        self.recorder.publish()
        return references


def stable_configuration(references, events):
    if set(references) != set(events):
        raise RuntimeError("Every shared consumer needs its own complete configuration references")
    baseline = {}
    for event in events:
        pair = references[event]
        if (len(pair) != 2 or pair[0] != pair[1] or
                pair[0]["output"]["eventId"] != event or not pair[0]["output"]["pixels"]):
            raise RuntimeError("Missing or unstable complete configuration references at EID%d" % event)
        buffer = pair[0].get("buffer", {})
        if (buffer.get("patchBytes") != 16 or type(buffer.get("bytes")) is not int or
                buffer["bytes"] < 16 or not isinstance(buffer.get("patchHex"), str) or
                len(bytes.fromhex(buffer["patchHex"])) != 16):
            raise RuntimeError("Configuration reference lacks the complete shared float4 bytes")
        baseline[event] = pair[0]
    return baseline


def validate_shader_with_buffer(run, values, shader_only_references):
    """The caller has committed a checked replacement and keeps it installed throughout this test."""
    report = run.report
    report["shaderWithBufferAcceptance"] = "failedOrIncomplete"
    issue_start = len(report["issues"])
    if len(values) != 2 or values[0] == values[1]:
        raise RuntimeError("Shader/shared-buffer combination requires two distinct live values")
    shader_only = stable_configuration(shader_only_references, run.events)
    original_baseline = run.baseline
    report["shaderWithBuffer"] = {"fixedEvent": run.events[1], "shaderOnlyBaseline": shader_only,
                                 "phaseLabels": [], "immediatePairDifferent": False,
                                 "patchUndoCompletedWithReplacementInstalled": False}
    try:
        # Compare against the replacement's own output: a shader that ignores this CBuffer must
        # not pass merely because replacing the shader already changed the original image.
        run.baseline = shader_only
        immediate = [run.change("shaderWithBuffer.sliderValue%d" % (index + 1), value)
                     for index, value in enumerate(values)]
        different = immediate[0]["output"]["pixels"] != immediate[1]["output"]["pixels"]
        report["shaderWithBuffer"]["immediatePairDifferent"] = different
        if not different:
            run.issue("shaderWithBuffer", ["Consecutive shared values did not immediately change replacement-shader output"])

        # Only now may navigation and canonical references run; neither can repair an already
        # saved stale immediate witness. Each phase also tests every shared consumer's own region.
        for index, value in enumerate(values):
            label = "shaderWithBuffer.value%d" % (index + 1)
            run.change(label + ".installForReference", value)
            references = run.phase(label, value, immediate[index])
            stable_configuration(references, run.events)
            report["shaderWithBuffer"]["phaseLabels"].append(label)
        undo = run.change("shaderWithBuffer.patchUndo", None)
        label = "shaderWithBuffer.patchUndone"
        references = run.phase(label, None, undo, original=True)
        restored = stable_configuration(references, run.events)
        if restored != shader_only:
            run.issue(label, ["Shared patch undo did not restore the saved shader-only bytes and full outputs"])
        report["shaderWithBuffer"]["phaseLabels"].append(label)
        report["shaderWithBuffer"]["patchUndoCompletedWithReplacementInstalled"] = True
        if len(report["issues"]) == issue_start:
            report["shaderWithBufferAcceptance"] = "passedReplacementShaderWithSharedBufferAndUndo"
    finally:
        run.baseline = original_baseline
        run.recorder.publish()


def shader_rejection_issues(error, before, after, expected, observed):
    issues = []
    if not isinstance(error, str) or not error.strip():
        issues.append("The wrong-stage shader replacement did not return a rejection diagnostic")
    for field in ("eventId", "sharedBufferGeneration", "shaderGeneration", "pixelShader"):
        if field not in before or field not in after or before[field] != after[field]:
            issues.append("Rejected shader replacement changed " + field)
    if observed != expected:
        issues.append("Rejected shader replacement changed the accepted shader's complete buffer/output witness")
    return issues


def validate_d3d11_shader_rejection(run, original_shader, shader_only_references, temporary_targets):
    """Reject a generated CS while the accepted PS remains installed; never replay to repair it."""
    report, controller, rd = run.report, run.controller, run.rd
    if run.api != "D3D11":
        report["shaderRejectionAcceptance"] = "notRequestedNotTested"
        return
    report["shaderRejectionAcceptance"] = "failedOrIncomplete"
    detail = report["shaderRejection"] = {"kind": "computeTargetForCapturedPixelShader"}
    event = run.events[1]
    source = b"[numthreads(1, 1, 1)] void main(uint3 dispatchId : SV_DispatchThreadID) {}\n"
    try:
        expected = stable_configuration(shader_only_references, run.events)[event]
        def state():
            return {"eventId": int(controller.GetCurrentEventID()),
                    "sharedBufferGeneration": int(controller.GetSharedBufferPatchGeneration()),
                    "shaderGeneration": int(controller.GetShaderReplacementGeneration(event)),
                    "pixelShader": str(controller.GetPipelineState().GetShader(rd.ShaderStage.Pixel))}
        before = detail["before"] = state()
        if before["eventId"] != event or before["sharedBufferGeneration"] != run.generation:
            raise RuntimeError("Shader rejection must begin at the accepted shader-only B state")
        before_witness, _ = run.read("shaderRejection.before", event)
        detail["beforeWitness"] = before_witness
        if before_witness != expected:
            raise RuntimeError("Shader-only state differs from its complete references before rejection")
        built, build_timing = run.recorder.measure("shaderRejection.buildCS", lambda:
            controller.BuildTargetShader("main", rd.ShaderEncoding.HLSL, source,
                                         rd.ShaderCompileFlags(), rd.ShaderStage.Compute))
        target, compile_error = built
        detail.update(sourceSha256=hashlib.sha256(source).hexdigest(),
                      compileDiagnostics=str(compile_error), targetResource=str(target))
        if target == rd.ResourceId.Null():
            raise RuntimeError("Generated wrong-stage shader could not be compiled: " + str(compile_error))
        # Do not free this resource before observing rejection. If a broken implementation did
        # install it, the outer cleanup must first restore the original mapping or shut down.
        temporary_targets.append(target)
        error, timing = run.recorder.measure("shaderRejection.replaceChecked", lambda:
            controller.ReplaceTargetShaderChecked(original_shader, target), event)
        detail.update(diagnostic=str(error), timingRecords=timing["timingRecords"], after=state())
        observed, readback = run.read("shaderRejection.immediate", event)
        detail["immediateWitness"] = observed
        issues = shader_rejection_issues(error, before, detail["after"], expected, observed)
        issues.extend(readback_replay_issues(build_timing["timingRecords"]))
        issues.extend(readback_replay_issues(timing["timingRecords"]))
        issues.extend(readback_replay_issues(readback["timingRecords"]))
        run.issue("shaderRejection", issues)
        if issues:
            raise RuntimeError("; ".join(issues))
        report["shaderRejectionAcceptance"] = "passedWrongStageRejectedWithoutChangingAcceptedState"
    except Exception as error:
        detail["error"] = str(error)
        raise
    finally:
        run.recorder.publish()


def discover(controller, rd, report, recorder, args, maximum):
    actions = []
    def walk(nodes):
        for action in nodes:
            if action.flags & rd.ActionFlags.Drawcall:
                actions.append(action)
            walk(action.children)
    walk(controller.GetRootActions())
    events = select_events([int(action.eventId) for action in actions], args.events)
    if any(action.flags & rd.ActionFlags.Indirect for action in actions if int(action.eventId) in events):
        raise RuntimeError("Selected shared consumer draws must be direct")
    evidence = selected_event_evidence(controller, events)
    if any(row["status"] != "exactAPIEvent" for row in evidence):
        raise RuntimeError("Selected draw lacks exact native API-event evidence")
    report["events"], report["selectedEventEvidence"] = events, evidence
    textures, buffer_list = controller.GetTextures(), controller.GetBuffers()
    by_id, outputs, probes, bindings, shaders = {str(t.resourceId): t for t in textures}, set(), [], [], []
    resource = None
    for event in events:
        recorder.measure("discovery.EID%d" % event, lambda e=event: controller.SetFrameEvent(e, True), event)
        pipe = controller.GetPipelineState()
        reflection = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
        if reflection is None:
            raise RuntimeError("Shared fixture needs Pixel/Fragment reflection")
        matches = []
        for index, block in enumerate(reflection.constantBlocks):
            for member in block.variables:
                if str(member.name) != args.member:
                    continue
                if (not block.bufferBacked or block.inlineDataBytes or block.compileConstants or
                        int(block.bindArraySize) != 1 or int(member.type.rows) != 1 or
                        int(member.type.columns) != 4 or int(member.type.elements) != 1 or
                        member.type.baseType != rd.VarType.Float):
                    raise RuntimeError("Fixture member must be one ordinary buffer-backed float4")
                descriptor = pipe.GetConstantBlock(rd.ShaderStage.Pixel, index, 0).descriptor
                if descriptor.resource == rd.ResourceId.Null():
                    raise RuntimeError("Reflected shared member has no original captured buffer")
                offset = shared_member_offset(int(descriptor.byteOffset), int(descriptor.byteSize),
                                              int(member.byteOffset), int(block.byteSize))
                matches.append({"eventId": event, "buffer": resource_number(descriptor.resource),
                                "bindingOffset": int(descriptor.byteOffset),
                                "bindingByteSize": int(descriptor.byteSize), "memberOffset": int(member.byteOffset),
                                "absoluteOffset": offset, "bytes": 16, "blockIndex": index})
                resource = descriptor.resource
        if len(matches) != 1:
            raise RuntimeError("Every draw must have exactly one reflected member named " + args.member)
        bindings += matches
        shaders.append(pipe.GetShader(rd.ShaderStage.Pixel))
        targets = list(pipe.GetOutputTargets())
        bound = targets + [pipe.GetDepthTarget()]
        outputs.update(resource_number(b.resource) for b in bound if b.resource != rd.ResourceId.Null())
        if not targets or str(targets[0].resource) not in by_id:
            raise RuntimeError("Fixture needs an actual RT0 at each draw")
        probes.append(viewport_probe(event, report["captureDriver"], pipe.GetViewport(0),
                                      by_id[str(targets[0].resource)], targets[0]))
    number, offset, _ = shared_identity(bindings)
    found = [buffer for buffer in buffer_list if resource_number(buffer.resourceId) == number]
    if len(found) != 1:
        raise RuntimeError("Shared resource is not one original captured buffer")
    window = read_window(int(found[0].length), offset, 16, args.neighbors)
    if None in outputs or not 1 <= len(outputs) <= 16:
        raise RuntimeError("Output coverage must contain one to sixteen actual textures")
    if len({(p["resource"], tuple(p["subresource"]), p["x"], p["y"]) for p in probes}) != 4:
        raise RuntimeError("Four distinct output region probes are required")
    descriptions, requests, estimated = texture_requests(textures, sorted(outputs), maximum - window[1])
    report.update(sharedBindings=bindings, sharedBuffer=number, byteOffset=offset,
                  readWindow={"offset": window[0], "bytes": window[1], "relative": window[2]},
                  textures=descriptions, regionProbes=probes, estimatedReadbackBytes=estimated + window[1])
    if args.shader_source and len({str(shader) for shader in shaders}) != 1:
        raise RuntimeError("Checked shader fixture must share the same original pixel shader at all four draws")
    return events, requests, {str(r): r for r, _ in requests}, probes, resource, offset, window, shaders[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--events", default="")
    parser.add_argument("--member", default="tint")
    parser.add_argument("--values-hex", default=",".join(struct.pack("<4f", *v).hex() for v in
                         ((0.8, 0.1, 0.3, 1.0), (0.1, 0.85, 0.2, 1.0))))
    parser.add_argument("--neighbors", type=int, default=16)
    parser.add_argument("--min-hits", type=int, default=1)
    parser.add_argument("--disable-live-forward", action="store_true")
    parser.add_argument("--max-read-mib", type=int, default=256)
    parser.add_argument("--max-seconds", type=int, default=360)
    parser.add_argument("--build-label", default="unspecified")
    parser.add_argument("--shader-source", help="Optional exact ABI-compatible replacement source; at most 64 KiB")
    parser.add_argument("--shader-encoding", choices=("HLSL", "GLSL"))
    parser.add_argument("--shader-entry", default="main")
    args = parser.parse_args(argv)
    try:
        values = values_from_hex(args.values_hex)
        if (not 0 <= args.neighbors <= 256 or not 1 <= args.min_hits <= 5 or
                not 1 <= args.max_read_mib <= 256 or not 1 <= args.max_seconds <= 600):
            raise ValueError("neighbors must be 0..256, min-hits 1..5, read budget 1..256 MiB, time 1..600 s")
        if not args.member or len(args.member) > 256:
            raise ValueError("Provide one bounded reflected member name")
        capture, output = Path(args.capture).resolve(), Path(args.output).resolve()
        if not capture.is_file() or any(path.exists() for path in
                (output, output.with_suffix(".rdoc.log"), output.with_suffix(".native.log"))):
            raise ValueError("Capture must exist and output evidence stem must be unused")
        source = None
        if bool(args.shader_source) != bool(args.shader_encoding):
            raise ValueError("Shader source and encoding must be supplied together")
        if args.shader_source:
            source_path = Path(args.shader_source).resolve()
            if not source_path.is_file() or not 1 <= source_path.stat().st_size <= 65536:
                raise ValueError("Shader source must be a nonempty file of at most 64 KiB")
            source = source_path.read_bytes()
    except (ValueError, OSError) as error:
        parser.error(str(error))
    output.parent.mkdir(parents=True, exist_ok=True)
    environment = profile_environment(args.disable_live_forward)
    os.environ.update(environment)
    report = {"schema": "renderdoc-portable-forward-edit-profile/v1", "ok": False,
              "measurementComplete": False, "capture": str(capture), "configuration": vars(args),
              "environment": environment, "processId": os.getpid(), "samples": [], "visits": [],
              "mutations": [], "phases": [], "issues": [], "totalReadbackBytes": 0,
              "shaderAcceptance": "requested" if source else "notRequestedNotTested",
              "shaderWithBufferAcceptance": "requested" if source else "notRequestedNotTested",
              "shaderRejectionAcceptance": "notRequestedNotTested",
              "limits": ["One reflected float4 shared by four actual Pixel/Fragment draws",
                         "Complete fixed output texture set plus exact buffer window; no all-UAV guarantee",
                         "Immediate slider witnesses precede any explicit EID setter",
                         "No-hit enabled configurations fail acceptance; disabled controls require zero hits",
                         "Native checked shader identity/pipeline acceptance; complete Workbench ABI validation is separate",
                         "Readback synchronization means setter wall is not UI drag or scanout latency"]}
    recorder = Recorder(report, output, output.with_suffix(".rdoc.log"), args.max_seconds)
    recorder.publish()
    rd, controller, cap, initialized = None, None, None, False
    run, target_shader, original_shader = None, None, None
    temporary_shader_targets = []
    shader_active, patch_active = False, False
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
        if report["captureDriver"] not in BACKENDS:
            raise RuntimeError("Unsupported native edit acceptance API")
        if source is not None and report["captureDriver"] == "D3D11":
            report["shaderRejectionAcceptance"] = "requested"
        opened, _ = recorder.measure("capture.openReplay", lambda: cap.OpenCapture(rd.ReplayOptions(), None))
        result, controller = opened
        if result != rd.ResultCode.Succeeded or controller is None:
            raise RuntimeError("OpenCapture failed: " + str(result))
        controller.ConfigureReplayCheckpoints(False, 0, 0)
        args_tuple = discover(controller, rd, report, recorder, args, args.max_read_mib * 1024 * 1024)
        events, requests, resources, probes, buffer, offset, window, original_shader = args_tuple
        run = EditProbe(controller, rd, recorder, report, report["captureDriver"], events, requests,
                        resources, probes, buffer, offset, window, args.max_read_mib * 1024 * 1024,
                        args.disable_live_forward, args.min_hits)
        report["initialCheckpointStatus"] = run.status()
        for event in (events[0], events[2], events[3], events[1]):
            canonical = [run.visit("baseline.reference%d.EID%d" % (i + 1, event), event, True)["witness"]
                         for i in range(2)]
            if canonical[0] != canonical[1]:
                raise RuntimeError("Unedited native baseline is unstable at EID%d" % event)
            run.baseline[event] = canonical[0]
        if any(bytes.fromhex(run.baseline[e]["buffer"]["patchHex"]) in values for e in events):
            raise RuntimeError("Both requested values must differ from every original shared member value")
        # Establish a real old-generation cursor at B before the consecutive slider updates.
        run.visit("slider.seedA", events[0])
        run.visit("slider.seedB", events[1])
        immediate = []
        patch_active = True
        for index, value in enumerate(values):
            immediate.append(run.change("slider.value%d" % (index + 1), value))
        if immediate[0]["output"]["pixels"] == immediate[1]["output"]["pixels"]:
            run.issue("slider", ["Consecutive values did not immediately change native output pixels"])
        # Reference the same byte configurations later, without an intervening replay concealing
        # either saved immediate witness. Generation numbers are recorded separately from pixels.
        for index, value in enumerate(values):
            run.change("value%d.installForReference" % (index + 1), value)
            run.phase("value%d" % (index + 1), value, immediate[index])
        undo = run.change("shared.undo", None)
        patch_active = False
        run.phase("shared.undone", None, undo, original=True)
        if source is not None:
            encoding = getattr(rd.ShaderEncoding, args.shader_encoding)
            if encoding not in controller.GetTargetShaderEncodings():
                raise RuntimeError("Selected shader source encoding is not supported by the backend")
            built, _ = recorder.measure("shader.build", lambda: controller.BuildTargetShader(
                args.shader_entry, encoding, source, rd.ShaderCompileFlags(), rd.ShaderStage.Pixel))
            target_shader, error = built
            report["shaderSource"] = {"path": str(Path(args.shader_source).resolve()),
                                       "sha256": hashlib.sha256(source).hexdigest(), "diagnostics": str(error)}
            if target_shader == rd.ResourceId.Null():
                raise RuntimeError("Replacement shader compilation failed: " + str(error))
            run.visit("shader.seedA", events[0])
            run.visit("shader.seedB", events[1])
            # If the wrapper raises after committing, cleanup must still try checked rollback
            # before freeing the target resource. A returned native rejection committed nothing.
            shader_active = True
            error, timing = recorder.measure("shader.replaceChecked", lambda:
                controller.ReplaceTargetShaderChecked(original_shader, target_shader), events[1])
            if str(error):
                shader_active = False
                raise RuntimeError("Checked native shader transaction rejected: " + str(error))
            if any(r.get("hit") == 1 for r in timing["timingRecords"] if r.get("api") in set(BACKENDS.values())):
                run.issue("shader.replace", ["Shader edit reused a pre-edit live cursor"])
            changed, _ = run.read("shader.immediate", events[1])
            shader_only_references = run.phase("shader.replaced", None, changed)
            validate_d3d11_shader_rejection(run, original_shader, shader_only_references,
                                            temporary_shader_targets)
            # Keep cleanup armed before the first setter: an exception may happen after native
            # acceptance. The helper must undo the shared patch while the checked shader remains.
            patch_active = True
            validate_shader_with_buffer(run, values, shader_only_references)
            patch_active = False
            run.visit("shader.undoSeedA", events[0])
            run.visit("shader.undoSeedB", events[1])
            error, timing = recorder.measure("shader.undoChecked", lambda:
                controller.RemoveTargetShaderReplacementChecked(original_shader), events[1])
            if str(error):
                raise RuntimeError("Checked shader rollback rejected: " + str(error))
            shader_active = False
            if any(r.get("hit") == 1 for r in timing["timingRecords"] if r.get("api") in set(BACKENDS.values())):
                run.issue("shader.undo", ["Shader rollback reused the replaced-shader live cursor"])
            restored, _ = run.read("shader.undoImmediate", events[1])
            run.phase("shader.undone", None, restored, original=True)
            report["shaderAcceptance"] = "completedSeePhaseVerdicts"
        report["measurementComplete"], report["ok"] = True, not report["issues"]
        report["verdict"] = (("passedDisabledEditControl" if args.disable_live_forward else
                              "passedSharedEditAndNativeForwardParity") if report["ok"] else "failedOrInconclusive")
    except Exception as error:
        report.update(ok=False, error=str(error), traceback=traceback.format_exc(), verdict="failedOrInconclusive")
    finally:
        def cleanup(label, callback):
            try:
                value, _ = recorder.measure(label, callback, cleanup=True)
                if isinstance(value, str) and value:
                    raise RuntimeError(value)
                return True
            except Exception as error:
                report.setdefault("cleanupErrors", []).append({"phase": label, "error": str(error)})
                report.update(ok=False, verdict="cleanupFailed")
                return False
        if controller is not None:
            if shader_active:
                shader_active = not cleanup("cleanup.shaderUndo", lambda:
                    controller.RemoveTargetShaderReplacementChecked(original_shader))
            if patch_active:
                cleanup("cleanup.sharedUndo", controller.ClearSharedBufferPatches)
            if target_shader is not None and target_shader != rd.ResourceId.Null() and not shader_active:
                cleanup("cleanup.shaderFree", lambda: controller.FreeTargetResource(target_shader))
            if not shader_active:
                for temporary_target in temporary_shader_targets:
                    cleanup("cleanup.shaderRejectionTargetFree", lambda target=temporary_target:
                        controller.FreeTargetResource(target))
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
