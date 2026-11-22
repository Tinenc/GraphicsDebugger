"""Headless regression for persistent historical output after dirty redraw and shared edits.

Main uses the GPU. Run only under the replay owner's serial GPU queue. It discovers four actual
draws but replays only B and D: controller D, displayed history B. The public API has no output
resize setter, so force-D dirtiness exercises the same Display dirty branch as resize/expose.
No source snapshot is requested; native HistoryPreview logs must prove the headless fallback.
"""
from __future__ import print_function

import argparse
import json
import os
from pathlib import Path
import struct
import sys
import traceback

from eid_checkpoint_profile import parse_status, selected_event_evidence, status_issues
from eid_history_profile import Recorder, byte_record, resource_number
from eid_portable_forward_edit_profile import shared_member_offset
from eid_portable_forward_profile import profile_environment, select_events
from eid_shared_buffer_profile import binary_identity, build_identity, output_witness, read_window


def history_evidence(records, history, required):
    rows = [row for row in records if row.get("api") == "HistoryPreview"]
    issues = []
    if required and not rows:
        issues.append("Required native historical fallback replay evidence is missing")
    if not required and rows:
        issues.append("Explicit live display unexpectedly replayed the historical source")
    if len(rows) > 4:
        issues.append("Unbounded repeated history replay in one operation")
    for row in rows:
        for field, expected in (("eid", history), ("headless", 1), ("snapshotroute", 0), ("currentstate", 0)):
            if type(row.get(field)) is not int or row[field] != expected:
                issues.append("Historical fallback evidence differs at " + field)
    return {"records": rows, "issues": issues}


def stable_references(references, history, anchor):
    issues = []
    for event in (history, anchor):
        values = references.get(event, [])
        if len(values) != 2 or values[0] != values[1]:
            issues.append("EID%d has incomplete or unstable independent forced references" % event)
    if not issues and references[history][0]["rgb"] == references[anchor][0]["rgb"]:
        issues.append("History B and live D are identical; this fixture cannot reveal a source switch")
    return issues


def displayed_issues(rgb, expected, actual_event, anchor):
    issues = []
    if actual_event != anchor:
        issues.append("Historical display changed the controller's selected EID")
    if expected is not None and rgb != expected:
        issues.append("Complete displayed RGB differs from its independent forced reference")
    return issues


class HistoryProbe:
    def __init__(self, controller, output, reference_output, rd, recorder, report, source,
                 history, anchor, dimensions, maximum):
        self.controller, self.output, self.reference_output, self.rd = controller, output, reference_output, rd
        self.recorder, self.report, self.source = recorder, report, source
        self.history, self.anchor, self.dimensions, self.maximum = history, anchor, dimensions, maximum

    def issues(self, label, values):
        self.report["issues"].extend(label + ": " + value for value in values)

    def read_rgb(self, output):
        dimensions = output.GetDimensions()
        actual = (int(dimensions[0]), int(dimensions[1]))
        if actual != self.dimensions:
            raise RuntimeError("Native output canvas differs from the full selected texture dimensions")
        value = byte_record(output.ReadbackOutputTexture(), self.dimensions[0] * self.dimensions[1] * 3)
        if self.controller.GetFatalErrorStatus().code != self.rd.ResultCode.Succeeded:
            raise RuntimeError("Native historical display reported a fatal replay error")
        return value

    def operation(self, label, callback, expected=None, history_required=True):
        _, timing = self.recorder.measure(label, callback, self.history if history_required else self.anchor)
        rgb, readback = self.recorder.measure(label + ".rgb", lambda: self.read_rgb(self.output))
        current = int(self.controller.GetCurrentEventID())
        evidence = history_evidence(timing["timingRecords"], self.history, history_required)
        self.issues(label, evidence["issues"])
        self.issues(label, displayed_issues(rgb, expected, current, self.anchor))
        status = parse_status(self.controller.GetReplayCheckpointStatus())
        self.issues(label, status_issues(status, disabled=True))
        row = {"label": label, "requestedHistoryEID": self.history if history_required else None,
               "controllerEventAfter": current, "expectedControllerEID": self.anchor, "rgb": rgb,
               "historyEvidence": evidence, "wallMs": timing["wallMs"],
               "readbackWallMs": readback["wallMs"], "checkpointStatus": status}
        self.report["operations"].append(row)
        self.recorder.publish()
        return row

    def live(self, label, expected):
        def callback():
            self.output.SetTextureDisplay(self.source)
            self.output.Display()
        return self.operation(label, callback, expected, history_required=False)

    def history_select(self, label, expected):
        return self.operation(label, lambda: self.output.DisplayTextureAtEvent(self.history, self.source), expected)

    def dirty_redraw(self, label, expected):
        def callback():
            self.controller.SetFrameEvent(self.anchor, True)
            self.output.Display()
        return self.operation(label, callback, expected)

    def references(self, label):
        references = {}
        for event in (self.history, self.anchor):
            values = []
            for index in range(2):
                def replay(e=event):
                    self.controller.SetFrameEvent(e, True)
                    self.reference_output.SetTextureDisplay(self.source)
                    self.reference_output.Display()
                _, timing = self.recorder.measure(label + ".EID%d.reference%d" % (event, index + 1), replay, event)
                native_api = "OpenGL" if self.report["captureDriver"] == "OpenGLES" else self.report["captureDriver"]
                if not any(r.get("api") == native_api and r.get("type") == "WithoutDraw"
                           for r in timing["timingRecords"]):
                    self.issues(label, ["Forced reference lacks an independent native prefix replay"])
                def witness():
                    return {"rgb": self.read_rgb(self.reference_output),
                            "nativeOutputs": output_witness(self.controller, self.rd, self.maximum)}
                value, _ = self.recorder.measure(label + ".EID%d.reference%d.readback" % (event, index + 1), witness, event)
                if int(self.controller.GetCurrentEventID()) != event:
                    raise RuntimeError("Reference display/readback changed the selected EID")
                values.append(value)
                self.report["references"].append({"label": label, "eventId": event, "repeat": index + 1,
                                                  "force": True, "witness": value})
            references[event] = values
        self.issues(label, stable_references(references, self.history, self.anchor))
        self.recorder.publish()
        return references


def find_member(pipe, rd, name):
    reflection = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
    if reflection is None:
        raise RuntimeError("Shared-edit fixture needs native Pixel/Fragment reflection")
    matches = []
    for index, block in enumerate(reflection.constantBlocks):
        for member in block.variables:
            if str(member.name) != name:
                continue
            if (not block.bufferBacked or block.inlineDataBytes or block.compileConstants or
                    int(block.bindArraySize) != 1 or int(member.type.rows) != 1 or
                    int(member.type.columns) != 4 or int(member.type.elements) != 1 or
                    member.type.baseType != rd.VarType.Float):
                raise RuntimeError("Shared-edit fixture member must be one ordinary float4")
            descriptor = pipe.GetConstantBlock(rd.ShaderStage.Pixel, index, 0).descriptor
            if descriptor.resource == rd.ResourceId.Null():
                raise RuntimeError("Shared float4 is not backed by an original buffer")
            offset = shared_member_offset(int(descriptor.byteOffset), int(descriptor.byteSize),
                                          int(member.byteOffset), int(block.byteSize))
            matches.append((descriptor.resource, offset))
    if len(matches) != 1:
        raise RuntimeError("Expected one shared reflected member named " + name)
    return matches[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--events", default="", help="Optional four actual consecutive draw EIDs; only B,D are replayed")
    parser.add_argument("--validate-shared-edit", action="store_true")
    parser.add_argument("--member", default="tint")
    parser.add_argument("--value-hex", default=struct.pack("<4f", 0.8, 0.1, 0.3, 1).hex())
    parser.add_argument("--max-read-mib", type=int, default=256)
    parser.add_argument("--max-seconds", type=int, default=240)
    parser.add_argument("--build-label", default="unspecified")
    args = parser.parse_args(argv)
    try:
        value = bytes.fromhex(args.value_hex)
        if len(value) != 16 or not 1 <= args.max_read_mib <= 256 or not 1 <= args.max_seconds <= 600:
            raise ValueError("Value must be 16 bytes, read budget 1..256 MiB and time 1..600 seconds")
        capture, destination = Path(args.capture).resolve(), Path(args.output).resolve()
        if not capture.is_file() or any(p.exists() for p in (destination, destination.with_suffix(".rdoc.log"),
                                                            destination.with_suffix(".native.log"))):
            raise ValueError("Capture must exist and output evidence stem must be unused")
    except (ValueError, OSError) as error:
        parser.error(str(error))
    destination.parent.mkdir(parents=True, exist_ok=True)
    environment = profile_environment(True)
    environment["RENDERDOC_D3D12_DISABLE_LIVE_FORWARD"] = "1"
    os.environ.update(environment)
    report = {"schema": "renderdoc-history-fallback-profile/v1", "ok": False, "measurementComplete": False,
              "capture": str(capture), "configuration": vars(args), "environment": environment,
              "samples": [], "operations": [], "references": [], "issues": [], "processId": os.getpid(),
              "sharedEditAcceptance": "requested" if args.validate_shared_edit else "notRequestedNotTested",
              "limits": ["Headless Texture output forces snapshotroute=0; no source snapshot is requested",
                         "Full selected RT0 RGB canvas plus all-bound-output native reference hashes",
                         "Only actual B and D EIDs are replayed; four draw identities are read from metadata",
                         "Force D dirtiness tests the Display branch used by resize/expose; no native UI resize",
                         "Readbacks synchronize the GPU; timings are not interactive drag latency"]}
    recorder = Recorder(report, destination, destination.with_suffix(".rdoc.log"), args.max_seconds)
    recorder.publish()
    rd, controller, cap, output, reference_output = None, None, None, None, None
    initialized, patch_active = False, False
    member_binding, old_bytes = None, None
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
        if report["captureDriver"] not in ("D3D11", "D3D12", "OpenGL", "OpenGLES", "Vulkan"):
            raise RuntimeError("Unsupported fixture API")
        opened, _ = recorder.measure("capture.openReplay", lambda: cap.OpenCapture(rd.ReplayOptions(), None))
        result, controller = opened
        if result != rd.ResultCode.Succeeded or controller is None:
            raise RuntimeError("OpenCapture failed: " + str(result))
        controller.ConfigureReplayCheckpoints(False, 0, 0)
        draws = []
        def walk(nodes):
            for action in nodes:
                if action.flags & rd.ActionFlags.Drawcall:
                    if action.flags & rd.ActionFlags.Indirect:
                        raise RuntimeError("History fixture must use direct draw actions")
                    draws.append(int(action.eventId))
                walk(action.children)
        walk(controller.GetRootActions())
        events = select_events(draws, args.events)
        history, anchor = events[1], events[3]
        report.update(discoveredDraws=events, historyEvent=history, anchorEvent=anchor,
                      selectedEventEvidence=selected_event_evidence(controller, [history, anchor]))
        if any(row["status"] != "exactAPIEvent" for row in report["selectedEventEvidence"]):
            raise RuntimeError("History and anchor need exact native API-event identities")
        textures = {str(t.resourceId): t for t in controller.GetTextures()}
        view, texture, subresource = None, None, None
        for event in (history, anchor):
            recorder.measure("discovery.EID%d" % event, lambda e=event: controller.SetFrameEvent(e, True), event)
            pipe = controller.GetPipelineState()
            targets = list(pipe.GetOutputTargets())
            if not targets or str(targets[0].resource) not in textures:
                raise RuntimeError("Both B and D must bind the fixture RT0")
            bound = targets[0]
            current = (str(bound.resource), int(bound.firstMip), int(bound.firstSlice))
            if view is not None and view != current:
                raise RuntimeError("History fixture must bind the same RT0 view at B and D")
            view, texture = current, textures[current[0]]
            if int(bound.numMips) != 1 or int(bound.numSlices) != 1 or int(texture.depth) != 1:
                raise RuntimeError("This fallback RGB fixture requires one 2D RT0 view")
            subresource = rd.Subresource(current[1], current[2], 0)
            if args.validate_shared_edit:
                binding = find_member(pipe, rd, args.member)
                if member_binding is not None and (str(binding[0]), binding[1]) != (str(member_binding[0]), member_binding[1]):
                    raise RuntimeError("B and D do not share the same original buffer member range")
                member_binding = binding
        maximum = args.max_read_mib * 1024 * 1024
        width, height = max(1, int(texture.width) >> view[1]), max(1, int(texture.height) >> view[1])
        if width * height * 3 > maximum:
            raise RuntimeError("Complete headless RGB canvas exceeds max-read-mib")
        source = rd.TextureDisplay()
        source.resourceId, source.subresource, source.typeCast = texture.resourceId, subresource, texture.format.compType
        source.scale, source.rangeMin, source.rangeMax = 1.0, 0.0, 1.0
        source.alpha = False
        report["display"] = {"resource": str(texture.resourceId), "mip": view[1], "slice": view[2],
                              "sample": 0, "width": width, "height": height, "rgbBytes": width * height * 3,
                              "format": texture.format.Name(), "scale": 1.0}
        output, _ = recorder.measure("output.create", lambda: controller.CreateOutput(
            rd.CreateHeadlessWindowingData(width, height), rd.ReplayOutputType.Texture))
        reference_output, _ = recorder.measure("referenceOutput.create", lambda: controller.CreateOutput(
            rd.CreateHeadlessWindowingData(width, height), rd.ReplayOutputType.Texture))
        if output is None or reference_output is None:
            raise RuntimeError("Headless output creation failed")
        run = HistoryProbe(controller, output, reference_output, rd, recorder, report, source,
                           history, anchor, (width, height), maximum)
        baseline = run.references("baseline")
        b, d = baseline[history][0]["rgb"], baseline[anchor][0]["rgb"]
        run.live("liveD", d)
        run.history_select("historyB", b)
        run.dirty_redraw("historyB.forceDDirtyDisplay", b)
        run.live("exitHistory.showD", d)
        if args.validate_shared_edit:
            resource, offset = member_binding
            buffers = [buffer for buffer in controller.GetBuffers() if buffer.resourceId == resource]
            if len(buffers) != 1:
                raise RuntimeError("Shared edit target must identify an original buffer")
            read_window(int(buffers[0].length), offset, 16, 0)
            old_bytes = bytes(controller.GetBufferData(resource, offset, 16))
            if len(old_bytes) != 16 or old_bytes == value:
                raise RuntimeError("Shared value must differ from the original native float4")
            report["sharedMember"] = {"resource": str(resource), "byteOffset": offset, "bytes": 16,
                                       "originalHex": old_bytes.hex(), "valueHex": value.hex()}
            run.history_select("edit.selectHistoryB", b)
            before_generation = int(controller.GetSharedBufferPatchGeneration())
            patch_active = True
            def mutate():
                error = controller.SetSharedBufferPatch(resource, offset, value)
                if str(error):
                    raise RuntimeError("Shared setter rejected: " + str(error))
                output.Display()
            immediate = run.operation("edit.fixedDHistoricalDisplay", mutate)
            after_generation = int(controller.GetSharedBufferPatchGeneration())
            if after_generation <= before_generation or bytes(controller.GetBufferData(resource, offset, 16)) != value:
                raise RuntimeError("Shared setter did not publish the requested generation/bytes")
            report["sharedGeneration"] = {"before": before_generation, "after": after_generation}
            if immediate["rgb"] == b:
                run.issues("edit", ["Selected history B remained unchanged after the shared value edit"])
            dirty = run.dirty_redraw("edit.forceDDirtyDisplay", immediate["rgb"])
            edited = run.references("edited")
            edited_b, edited_d = edited[history][0]["rgb"], edited[anchor][0]["rgb"]
            run.issues("edit.immediate", displayed_issues(immediate["rgb"], edited_b,
                                                           immediate["controllerEventAfter"], anchor))
            run.issues("edit.dirty", displayed_issues(dirty["rgb"], edited_b,
                                                       dirty["controllerEventAfter"], anchor))
            run.live("edit.exitHistory.showD", edited_d)
            run.history_select("undo.selectHistoryB", edited_b)
            def undo():
                error = controller.ClearSharedBufferPatch(resource, offset, 16)
                if str(error):
                    raise RuntimeError("Shared undo rejected: " + str(error))
                output.Display()
            run.operation("undo.fixedDHistoricalDisplay", undo, b)
            patch_active = False
            if bytes(controller.GetBufferData(resource, offset, 16)) != old_bytes:
                raise RuntimeError("Shared undo did not restore original native member bytes")
            run.dirty_redraw("undo.forceDDirtyDisplay", b)
            run.live("undo.exitHistory.showD", d)
            restored = run.references("undone")
            if restored != baseline:
                run.issues("undo", ["Complete native output/reference pixels differ from the original baseline"])
            report["sharedEditAcceptance"] = "completedSeeIssues"
        report["measurementComplete"], report["ok"] = True, not report["issues"]
        report["verdict"] = "passedPersistentHistoricalFallback" if report["ok"] else "failedOrInconclusive"
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
        if controller is not None and patch_active:
            cleanup("cleanup.patchUndo", controller.ClearSharedBufferPatches)
        if output is not None:
            cleanup("cleanup.output", output.Shutdown)
        if reference_output is not None:
            cleanup("cleanup.referenceOutput", reference_output.Shutdown)
        if controller is not None:
            cleanup("cleanup.replay", controller.Shutdown)
        if cap is not None:
            cleanup("cleanup.capture", cap.Shutdown)
        if recorder.log_path.is_file():
            native = destination.with_suffix(".native.log")
            native.write_bytes(recorder.log_path.read_bytes())
            report["nativeLog"] = str(native)
        if initialized:
            cleanup("cleanup.runtime", rd.ShutdownReplay)
        recorder.publish()
    print(json.dumps({"ok": report["ok"], "output": str(destination), "verdict": report.get("verdict")}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
