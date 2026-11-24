"""D3D12 checked shader + shared float4 correctness on four discovered native draws.

This GPU probe does not require or claim live-forward/checkpoint acceleration. Its four-draw
fixture may span separate submissions. Use the isolated replay runner and coordinate GPU access.
Native checked replacement proves a transaction, not the Shader Workbench's complete ABI gate.
"""
from __future__ import print_function

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import traceback

from eid_history_profile import Recorder
from eid_live_forward_profile import live_evidence
from eid_portable_forward_edit_profile import (EditProbe, compare_configuration, discover,
    navigation, stable_configuration, validate_shader_with_buffer, values_from_hex)
from eid_shared_buffer_profile import binary_identity, build_identity, unchanged_checkpoint_issues


def replay_evidence(records, previous, target, force=False, disabled=False):
    """Require actual D3D12 prefix and target work; optional hits must still prove their range."""
    hits = [r for r in records if r.get("api") == "D3D12LiveForward" and r.get("hit") == 1]
    evidence = live_evidence(records, previous, target,
                             bool(hits) and not force and target > previous, force)
    issues = evidence["issues"]
    for row in evidence["records"]:
        if row.get("enabled") != int(not disabled):
            issues.append("Native D3D12 live-forward enable state differs from this run")
    if disabled and hits:
        issues.append("Disabled D3D12 control unexpectedly used live forward")
    native = [r for r in records if r.get("api") == "D3D12" and r.get("replay_depth") == 0]
    prefix = [r for r in native if r.get("type") == "WithoutDraw"]
    draw = [r for r in native if r.get("type") == "OnlyDraw" and
            r.get("range") == "0->%d" % target and r.get("partial") == 1]
    start = previous + 1 if hits else 1
    if len(prefix) != 1 or prefix[0].get("range") != "%d->%d" % (start, target):
        issues.append("Missing unique D3D12 prefix replay for the exact requested range")
    elif prefix[0].get("partial") != int(bool(hits)):
        issues.append("D3D12 prefix partial flag disagrees with actual live-forward evidence")
    elif not hits and prefix[0].get("section.applyInitial_calls", 0) < 1:
        issues.append("Independent D3D12 prefix did not prove initial state restoration")
    if not draw:
        issues.append("Missing D3D12 native target draw for the exact requested EID")
    for kind in ("WithoutDraw", "OnlyDraw"):
        boundaries = [r for r in records if r.get("api") == "ReplayDriverBoundary" and
                      r.get("type") == kind and r.get("eid") == target]
        if len(boundaries) != 1:
            issues.append("Missing unique native controller boundary for " + kind)
    evidence.update(nativePrefix=prefix, nativeDraw=draw,
                    optimizationAcceptance="notClaimedByCorrectnessProbe")
    return evidence


class D3D12EditProbe(EditProbe):
    def visit(self, label, event, force=False, expected=None):
        previous, before = int(self.controller.GetCurrentEventID()), self.status()
        _, timing = self.recorder.measure(label + ".replay",
            lambda: self.controller.SetFrameEvent(event, force), event)
        evidence = replay_evidence(timing["timingRecords"], previous, event, force, self.disabled)
        self.issue(label, evidence["issues"])
        self.issue(label, unchanged_checkpoint_issues(before, self.status()))
        witness, readback = self.read(label, event, expected)
        row = {"label": label, "eventId": event, "force": force, "generation": self.generation,
               "replayWallMs": timing["wallMs"], "readbackWallMs": readback["wallMs"],
               "nativeReplay": evidence, "witness": witness}
        self.report["visits"].append(row)
        self.recorder.publish()
        return row

    def change(self, label, value):
        witness = super().change(label, value)
        self.check_mutation_cursor(label, self.report["mutations"][-1]["timingRecords"])
        return witness

    def check_mutation_cursor(self, label, records):
        if any(r.get("hit") == 1 for r in records if r.get("api") == "D3D12LiveForward"):
            self.issue(label, ["Mutation reused a D3D12 live cursor from the previous configuration"])

    def phase(self, label, expected, immediate=None, original=False):
        observed = [self.visit(label + "." + suffix, event, expected=expected)
                    for suffix, event in navigation(self.events)]
        a, b, c, d = self.events
        references = {event: [self.visit(label + ".reference%d.EID%d" % (repeat + 1, event),
                                         event, True, expected)["witness"] for repeat in range(2)]
                      for event in (a, c, d, b)}
        rows, issues = compare_configuration(observed, references, self.baseline, self.events,
                                               immediate, original)
        self.issue(label, issues)
        self.report["phases"].append({"label": label, "comparisons": rows,
            "referenceGeneration": self.generation,
            "observedNativeForwardHits": sum(row["nativeReplay"]["matchingHits"] for row in observed),
            "optimizationAcceptance": "notClaimedByCorrectnessProbe"})
        self.recorder.publish()
        return references


def shader_transaction(run, label, callback):
    if int(run.controller.GetCurrentEventID()) != run.events[1]:
        raise RuntimeError("Shader transaction must run at the fixed B EID")
    error, timing = run.recorder.measure(label, callback, run.events[1])
    if str(error):
        raise RuntimeError("Checked native shader transaction rejected: " + str(error))
    run.check_mutation_cursor(label, timing["timingRecords"])
    return run.read(label + ".immediate", run.events[1])[0]


def exercise_combination(run, values, original_shader, target_shader, active):
    """Keep rollback ownership armed before each native mutation, including Python exceptions."""
    run.visit("shader.seedA", run.events[0])
    run.visit("shader.seedB", run.events[1])
    active["shader"] = True
    changed = shader_transaction(run, "shader.replaceChecked", lambda:
        run.controller.ReplaceTargetShaderChecked(original_shader, target_shader))
    shader_only = run.phase("shader.replaced", None, changed)
    stable_configuration(shader_only, run.events)
    active["patch"] = True
    validate_shader_with_buffer(run, values, shader_only)
    active["patch"] = False
    # The helper's two patch-undo references finish at B with the shader still installed.
    restored = shader_transaction(run, "shader.undoChecked", lambda:
        run.controller.RemoveTargetShaderReplacementChecked(original_shader))
    active["shader"] = False
    run.phase("shader.undone", None, restored, original=True)
    run.report["shaderAcceptance"] = "completedSeePhaseVerdicts"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("capture", "output", "shader-source"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--events", default="")
    parser.add_argument("--member", default="tint")
    parser.add_argument("--values-hex", default=",".join(struct.pack("<4f", *v).hex() for v in
        ((0.8, 0.1, 0.3, 1.0), (0.1, 0.85, 0.2, 1.0))))
    parser.add_argument("--shader-entry", default="main")
    parser.add_argument("--neighbors", type=int, default=16)
    parser.add_argument("--max-read-mib", type=int, default=256)
    parser.add_argument("--max-seconds", type=int, default=360)
    parser.add_argument("--disable-live-forward", action="store_true")
    parser.add_argument("--build-label", default="unspecified")
    args = parser.parse_args(argv)
    try:
        values = values_from_hex(args.values_hex)
        if (not 0 <= args.neighbors <= 256 or not 1 <= args.max_read_mib <= 256 or
                not 1 <= args.max_seconds <= 600 or not 1 <= len(args.member) <= 256 or
                not 1 <= len(args.shader_entry) <= 256):
            raise ValueError("Invalid bounded member, entry, read budget, neighbor or time limit")
        capture, output, source_path = (Path(value).resolve() for value in
                                       (args.capture, args.output, args.shader_source))
        if not capture.is_file() or any(path.exists() for path in
                (output, output.with_suffix(".rdoc.log"), output.with_suffix(".native.log"))):
            raise ValueError("Capture must exist and output evidence stem must be unused")
        if not source_path.is_file() or not 1 <= source_path.stat().st_size <= 65536:
            raise ValueError("Explicit HLSL source must contain 1..65536 bytes")
        source = source_path.read_bytes()
    except (ValueError, OSError) as error:
        parser.error(str(error))
    output.parent.mkdir(parents=True, exist_ok=True)
    environment = {"RENDERDOC_EID_TIMING": "1", "RENDERDOC_REPLAY_CHECKPOINTS": "0",
                   "RENDERDOC_D3D12_ENABLE_PREPARED_REPLAY": "0",
                   "RENDERDOC_D3D12_DISABLE_PREPARED_REPLAY": "1",
                   "RENDERDOC_D3D12_DISABLE_LIVE_FORWARD": "1" if args.disable_live_forward else "0"}
    os.environ.update(environment)
    report = {"schema": "renderdoc-d3d12-shader-buffer-profile/v1", "ok": False,
              "measurementComplete": False, "capture": str(capture), "configuration": vars(args),
              "environment": environment, "processId": os.getpid(), "samples": [], "visits": [],
              "mutations": [], "phases": [], "issues": [], "totalReadbackBytes": 0,
              "shaderAcceptance": "requested", "shaderWithBufferAcceptance": "requested",
              "optimizationAcceptance": "notClaimedByCorrectnessProbe",
              "limits": ["Four discovered direct draws, one reflected shared float4, all bound color/depth outputs",
                         "Checkpoints and prepared replay disabled; optional native live hits are only observations",
                         "Both fixed-EID values have immediate no-repair witnesses before any reference replay",
                         "Native checked shader transaction; complete Workbench ABI validation remains separate",
                         "D3D12 on this OS/driver only; readback wall is not UI or scanout latency"]}
    recorder = Recorder(report, output, output.with_suffix(".rdoc.log"), args.max_seconds)
    recorder.publish()
    rd, controller, cap, initialized, target_shader = None, None, None, False, None
    original_shader, active = None, {"shader": False, "patch": False}
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
            raise RuntimeError("This correctness probe requires a real D3D12 capture")
        opened, _ = recorder.measure("capture.openReplay", lambda: cap.OpenCapture(rd.ReplayOptions(), None))
        result, controller = opened
        if result != rd.ResultCode.Succeeded or controller is None:
            raise RuntimeError("OpenCapture failed: " + str(result))
        controller.ConfigureReplayCheckpoints(False, 0, 0)
        maximum = args.max_read_mib * 1024 * 1024
        events, requests, resources, probes, buffer, offset, window, original_shader = discover(
            controller, rd, report, recorder, args, maximum)
        run = D3D12EditProbe(controller, rd, recorder, report, "D3D12", events, requests, resources,
                            probes, buffer, offset, window, maximum, args.disable_live_forward, 0)
        report["initialCheckpointStatus"] = run.status()
        for event in (events[0], events[2], events[3], events[1]):
            pair = [run.visit("baseline.reference%d.EID%d" % (i + 1, event), event, True)["witness"]
                    for i in range(2)]
            run.baseline.update(stable_configuration({event: pair}, [event]))
        if any(bytes.fromhex(run.baseline[e]["buffer"]["patchHex"]) in values for e in events):
            raise RuntimeError("Both live values must differ from each original shared member value")
        if rd.ShaderEncoding.HLSL not in controller.GetTargetShaderEncodings():
            raise RuntimeError("Native D3D12 backend does not support HLSL target compilation")
        built, _ = recorder.measure("shader.build", lambda: controller.BuildTargetShader(
            args.shader_entry, rd.ShaderEncoding.HLSL, source, rd.ShaderCompileFlags(), rd.ShaderStage.Pixel))
        target_shader, error = built
        report["shaderSource"] = {"path": str(source_path), "sha256": hashlib.sha256(source).hexdigest(),
                                  "diagnostics": str(error)}
        if target_shader == rd.ResourceId.Null():
            raise RuntimeError("Replacement HLSL compilation failed: " + str(error))
        exercise_combination(run, values, original_shader, target_shader, active)
        report["measurementComplete"] = True
        report["ok"] = not report["issues"] and report["shaderWithBufferAcceptance"] == (
            "passedReplacementShaderWithSharedBufferAndUndo")
        report["verdict"] = "passedD3D12ShaderBufferCorrectness" if report["ok"] else "failedOrInconclusive"
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
            if active["patch"]:
                active["patch"] = not cleanup("cleanup.sharedUndo", controller.ClearSharedBufferPatches)
            if active["shader"]:
                active["shader"] = not cleanup("cleanup.shaderUndo", lambda:
                    controller.RemoveTargetShaderReplacementChecked(original_shader))
            if target_shader is not None and target_shader != rd.ResourceId.Null() and not active["shader"]:
                cleanup("cleanup.shaderFree", lambda: controller.FreeTargetResource(target_shader))
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
