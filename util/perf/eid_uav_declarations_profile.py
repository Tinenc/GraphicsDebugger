"""Bounded, byte-exact D3D12 UAV declaration-range acceptance on the named four-draw fixture.

Run the disabled control first, then the enabled run with --control-report in a fresh native
process. Main uses the GPU; coordinate replay ownership. Pure helpers do not import renderdoc.
Canonical readbacks are validation evidence only and are never supplied to replay as input.
"""
from __future__ import print_function

import argparse
from array import array
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback

from analyze_eid_restore import parse_restore_records
from eid_checkpoint_profile import parse_status, selected_event_evidence, status_issues
from eid_history_profile import Recorder, resource_number
from eid_live_forward_profile import texture_requests
from eid_portable_forward_profile import select_events, sequence
from eid_shared_buffer_profile import binary_identity, build_identity, unchanged_checkpoint_issues


SCHEMA = "renderdoc-uav-declarations-profile/v1"
BUFFER_NAMES = ("UAVDeclarations_Written0", "UAVDeclarations_Written1",
                "UAVDeclarations_Static2", "UAVDeclarations_Static3")
BUFFER_BYTES = (131072, 131072, 4194304, 4194304)
STATIC_BYTES = sum(BUFFER_BYTES[2:])
MAX_REPORT_BYTES = 32 * 1024 * 1024


def environment(disabled):
    return {"RENDERDOC_EID_TIMING": "1", "RENDERDOC_REPLAY_CHECKPOINTS": "0",
            "RENDERDOC_D3D12_ENABLE_UAV_DECLARATION_RANGES": "0" if disabled else "1",
            "RENDERDOC_D3D12_DISABLE_LIVE_FORWARD": "1",
            "RENDERDOC_D3D12_ENABLE_PREPARED_REPLAY": "0",
            "RENDERDOC_D3D12_DISABLE_PREPARED_REPLAY": "1",
            "RENDERDOC_D3D12_DISABLE_DIRTY_RESTORE": "0",
            "RENDERDOC_D3D12_DIRTY_RESTORE_SHADOW": "0",
            "RENDERDOC_D3D12_DISABLE_ACTIVE_WRITE_STAGES": "0",
            "RENDERDOC_D3D12_CHECKPOINT_FULL_CLEAR_PREFIX": "0",
            "RENDERDOC_D3D12_ENABLE_CHECKPOINT_DESCRIPTOR_OVERLAY": "0",
            "RENDERDOC_D3D12_DISABLE_CHECKPOINT_RESIDENT_REUSE": "1",
            "RENDERDOC_D3D12_DISABLE_CHECKPOINT_INITIAL_SOURCES": "1",
            "RENDERDOC_D3D12_CHECKPOINT_FAIL_AFTER_INITIAL": "0",
            "RENDERDOC_D3D12_INITIAL_SOURCE_CACHE_ORDER": "legacy"}


def named_buffers(descriptions, buffers):
    result = []
    for name, length in zip(BUFFER_NAMES, BUFFER_BYTES):
        matches = [r for r in descriptions if str(r.name) == name]
        if len(matches) != 1:
            raise ValueError("Expected exactly one named fixture buffer: " + name)
        matches = [b for b in buffers if b.resourceId == matches[0].resourceId]
        if len(matches) != 1 or int(matches[0].length) != length:
            raise ValueError("Fixture buffer has unexpected native length: " + name)
        result.append({"name": name, "resource": str(matches[0].resourceId), "bytes": length})
    if len({r["resource"] for r in result}) != 4:
        raise ValueError("Fixture buffers must have four distinct original resource identities")
    return result


def static_pattern(index):
    if index not in (2, 3):
        raise ValueError("Only fixture static buffers 2/3 have this pattern")
    values = array("I", ((0x10000000 if index == 2 else 0x20000000) +
                         (3 if index == 2 else 5) * i for i in range(BUFFER_BYTES[index] // 4)))
    if values.itemsize != 4:
        raise RuntimeError("A native uint32 array is required")
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def written_pattern(region, index):
    if region not in range(4) or index not in (0, 1):
        raise ValueError("Fixture region/index must be 0..3 and 0..1")
    values = array("I", [0]) * (256 * 128)
    for i in range(256 * 128):
        x, part = i % 256, (i % 256) // 64
        if part <= region and ((x ^ part) & 1) == index:
            values[i] = (part + 1) * 65536 + ((3 * i) & 255) * 256 + ((5 * i) & 255) + 1
    if values.itemsize != 4:
        raise RuntimeError("A native uint32 array is required")
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def output_pattern(region):
    if region not in range(4):
        raise ValueError("Fixture region must be 0..3")
    pixels = bytearray(b"\x00\x00\x00\xff" * (256 * 128))
    for i in range(256 * 128):
        part = (i % 256) // 64
        if part <= region:
            left, right = (3 * i) & 255, (5 * i) & 255
            value = (part + 1) * 65536 + left * 256 + right + 1
            pixels[4 * i:4 * i + 4] = bytes((left, right, (value >> 4) & 255, 255))
    return bytes(pixels)


def native_replay_present(records, event):
    return any(r.get("api") == "D3D12" and r.get("type") == "WithoutDraw" and
               r.get("range") in ("0->%d" % event, "1->%d" % event) and
               r.get("replay_depth") == 0 for r in records)


def declaration_evidence(records, event, disabled):
    rows = [r for r in records if r.get("api") == "D3D12DirtyWriteCutoff"]
    issues, trims = [], 0
    for row in rows:
        fields = ("declaredUAVs", "declarationHits", "declarationFallbacks", "uavSlotsBefore",
                  "uavSlotsAfter", "proofValid", "requestedEnd")
        if any(type(row.get(k)) is not int or row[k] < 0 for k in fields):
            issues.append("Native UAV declaration evidence has missing/invalid counters")
            continue
        if row["requestedEnd"] not in (event - 1, event):
            issues.append("Native write-bound record does not belong to the requested target")
        if row["declaredUAVs"] not in (0, 1) or row["proofValid"] != 1:
            issues.append("Native submitted write proof is not valid")
        if disabled:
            if row["declaredUAVs"] or row["declarationHits"] or row["uavSlotsAfter"] < row["uavSlotsBefore"]:
                issues.append("Disabled control unexpectedly trimmed a UAV declaration range")
        elif (row["proofValid"] == 1 and row["declaredUAVs"] == 1 and row["declarationHits"] > 0 and
              0 < row["uavSlotsAfter"] < row["uavSlotsBefore"] and
              row["requestedEnd"] in (event - 1, event)):
            trims += 1
    for row in records:
        if row.get("api") == "D3D12LiveForward" and row.get("hit") != 0:
            issues.append("Live forward must not bypass the initial-restore experiment")
    return {"records": rows, "nativeTrimRecords": trims, "issues": issues}


def restore_evidence(text):
    rows, issues, mirrors = parse_restore_records(text)
    for row in rows:
        for key in ("actualCopyBytes", "dirtyResources", "dirtyHeaps", "wholeResourceCopies"):
            if type(row.get(key)) is not int or row[key] < 0:
                issues.append("Invalid native initial-restore counter: " + key)
        if (row.get("restoreTimingVersion") != 2 or row.get("shadow") != 0 or
                row.get("resourceDetailStatus") != "complete" or
                row.get("sourceDetailStatus") != "complete"):
            issues.append("Fixture needs complete, non-shadow native byte/copy accounting")
    return {"records": rows, "mirrorRecordsRemoved": mirrors, "issues": issues}


def restore_summary(evidence, static_resources):
    rows = evidence["records"]
    # The seed after independent forced references can legitimately need a canonical reset.
    eligible = (bool(rows) and not evidence["issues"] and
                all(r.get("enabled") == 1 and r.get("reason") == "submittedWrites" for r in rows))
    resources = [r for row in rows for r in row["resources"]]
    return {"eligible": eligible, "restoreCalls": len(rows),
            "actualCopyBytes": sum(r.get("actualCopyBytes", 0) for r in rows),
            "dirtyResources": sum(r.get("dirtyResources", 0) for r in rows),
            "wholeResourceCopyCounts": {rid: sum(r["wholeResourceCopies"] for r in resources if r["resource"] == rid)
                                         for rid in sorted({r["resource"] for r in resources if r["wholeResourceCopies"]})},
            "staticActualBytes": {rid: sum(r["actualCopyBytes"] for r in resources if r["resource"] == rid)
                                  for rid in static_resources}}


def compare_restores(visits, control_visits, static_resources, minimum):
    issues, rows = [], []
    if [(v["label"], v["eventId"]) for v in visits] != [
            (v["label"], v["eventId"]) for v in control_visits]:
        return [], ["Control and enabled runs have different ordinary visit sequences"]
    for visit, control in zip(visits, control_visits):
        on = restore_summary(visit["restore"], static_resources)
        off = restore_summary(control["restore"], static_resources)
        # Unknown-size whole-resource copies are not converted into byte savings. Require their
        # exact resource/count union to stay identical, and neither static buffer to use one.
        whole_equal = on["wholeResourceCopyCounts"] == off["wholeResourceCopyCounts"]
        comparable = (on["eligible"] and off["eligible"] and on["restoreCalls"] == off["restoreCalls"]
                      and whole_equal and not any(rid in on["wholeResourceCopyCounts"] for rid in static_resources))
        saved = off["actualCopyBytes"] - on["actualCopyBytes"]
        count_saved = off["dirtyResources"] - on["dirtyResources"]
        static_saved = all(off["staticActualBytes"][rid] >= BUFFER_BYTES[2] and
                           on["staticActualBytes"][rid] == 0 for rid in static_resources)
        reduced = comparable and static_saved and saved >= STATIC_BYTES and count_saved >= 2
        rows.append({"label": visit["label"], "eventId": visit["eventId"], "enabled": on,
                     "disabled": off, "comparable": comparable, "actualBytesSaved": saved,
                     "dirtyResourcesSaved": count_saved, "bothStaticCopiesEliminated": static_saved,
                     "measuredReduction": reduced})
        if comparable and (saved < 0 or count_saved < 0):
            issues.append(visit["label"] + ": enabled run increased measured initial restoration")
    if sum(r["measuredReduction"] for r in rows) < minimum:
        issues.append("Insufficient visits with actual byte/resource reduction and both static copies eliminated")
    return rows, issues


def identity_key(identity):
    return identity.get("sha256"), identity.get("bytes")


def control_issues(report, control):
    issues = []
    if (control.get("schema") != SCHEMA or control.get("ok") is not True or
            control.get("measurementComplete") is not True or
            control.get("configuration", {}).get("disable_declarations") is not True):
        issues.append("Control report is not a completed successful disabled run")
    if identity_key(report["captureIdentity"]) != identity_key(control.get("captureIdentity", {})):
        issues.append("Control capture bytes/hash differ")
    current, prior = report["buildIdentity"], control.get("buildIdentity", {})
    if (not current.get("nativeModules") or not prior.get("nativeModules") or
            sorted(identity_key(v) for v in current["nativeModules"]) !=
            sorted(identity_key(v) for v in prior.get("nativeModules", [])) or
            identity_key(current["pythonModule"]) != identity_key(prior.get("pythonModule", {}))):
        issues.append("Control did not load the same native library and Python binding")
    for key in ("events", "buffers", "textures"):
        if report[key] != control.get(key):
            issues.append("Control fixture identity differs: " + key)
    on, off = dict(report["environment"]), dict(control.get("environment", {}))
    field = "RENDERDOC_D3D12_ENABLE_UAV_DECLARATION_RANGES"
    if on.pop(field, None) != "1" or off.pop(field, None) != "0" or on != off:
        issues.append("Control environment differs beyond the declaration-range switch")
    return issues


def read_payload(path, entry, maximum):
    if (type(entry.get("offset")) is not int or entry["offset"] < 0 or
            type(entry.get("bytes")) is not int or not 0 < entry["bytes"] <= maximum or
            entry["offset"] + entry["bytes"] > path.stat().st_size):
        raise RuntimeError("Canonical byte payload is out of bounds")
    with path.open("rb") as stream:
        stream.seek(entry["offset"])
        raw = stream.read(entry["bytes"])
    if len(raw) != entry["bytes"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
        raise RuntimeError("Canonical byte payload is truncated or changed")
    return raw


def read_state(controller, rd, buffers, ids, requests, expected_buffers, expected_pixels, maximum):
    pipe = controller.GetPipelineState()
    witness = {"eventId": int(controller.GetCurrentEventID()), "buffers": [], "pixels": [],
               "bindings": [], "shaders": {}, "topology": str(pipe.GetPrimitiveTopology())}
    for name in ("Vertex", "Hull", "Domain", "Geometry", "Pixel", "Compute", "Task", "Mesh"):
        stage = getattr(rd.ShaderStage, name, None)
        if stage is not None:
            witness["shaders"][name] = str(pipe.GetShader(stage))
    fixed = {str(resource) for resource, _ in requests}
    targets = [("color", i, b) for i, b in enumerate(pipe.GetOutputTargets())]
    targets.append(("depth", 0, pipe.GetDepthTarget()))
    for kind, slot, b in targets:
        if resource_number(b.resource) and str(b.resource) not in fixed:
            raise RuntimeError("Bound output escaped the fixed complete readback coverage")
        witness["bindings"].append({"kind": kind, "slot": slot, "resource": str(b.resource),
            "firstMip": int(b.firstMip), "numMips": int(b.numMips), "firstSlice": int(b.firstSlice),
            "numSlices": int(b.numSlices), "format": b.format.Name()})
    payload = bytearray()
    for index, spec in enumerate(buffers):
        raw = bytes(controller.GetBufferData(ids[spec["resource"]], 0, spec["bytes"]))
        if len(raw) != spec["bytes"]:
            raise RuntimeError("Incomplete native fixture buffer: " + spec["name"])
        if raw != expected_buffers[index]:
            raise RuntimeError("Full fixture buffer differs from independently calculated values: " + spec["name"])
        witness["buffers"].append(dict(spec, sha256=hashlib.sha256(raw).hexdigest()))
        payload.extend(raw)
    for resource, sub in requests:
        raw = bytes(controller.GetTextureData(resource, rd.Subresource(*sub)))
        if not raw or len(raw) > maximum - len(payload):
            raise RuntimeError("Empty or over-budget full native output readback")
        if raw != expected_pixels[(str(resource), sub)]:
            raise RuntimeError("Full fixture output differs from independently calculated RGBA bytes")
        witness["pixels"].append({"resource": str(resource), "subresource": list(sub),
                                   "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        payload.extend(raw)
    if len(payload) > maximum or controller.GetFatalErrorStatus().code != rd.ResultCode.Succeeded:
        raise RuntimeError("Native witness exceeded budget or encountered a fatal replay error")
    return witness, bytes(payload)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--disable-declarations", action="store_true")
    parser.add_argument("--control-report", help="Completed same-binary disabled JSON; mandatory when enabled")
    parser.add_argument("--min-reduced-visits", type=int, default=2)
    parser.add_argument("--max-read-mib", type=int, default=16)
    parser.add_argument("--max-seconds", type=int, default=300)
    parser.add_argument("--build-label", default="unspecified")
    args = parser.parse_args(argv)
    try:
        if not 9 <= args.max_read_mib <= 64 or not 1 <= args.max_seconds <= 600 or not 1 <= args.min_reduced_visits <= 5:
            raise ValueError("Bounds: max-read-mib 9..64, max-seconds 1..600, min-reduced-visits 1..5")
        if args.disable_declarations == bool(args.control_report):
            raise ValueError("Enabled mode requires --control-report; disabled mode must omit it")
        capture, output = Path(args.capture).resolve(), Path(args.output).resolve()
        if not capture.is_file() or any(output.with_suffix(s).exists() for s in
                (".json", ".rdoc.log", ".native.log", ".payload.bin")) or output.suffix != ".json":
            raise ValueError("Capture must exist and the .json evidence stem must be unused")
        control_path = Path(args.control_report).resolve() if args.control_report else None
        control = None
        if control_path:
            if not control_path.is_file() or control_path.stat().st_size > MAX_REPORT_BYTES:
                raise ValueError("Control report missing or exceeds 32 MiB")
            control = json.loads(control_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as error:
        parser.error(str(error))
    output.parent.mkdir(parents=True, exist_ok=True)
    configured = environment(args.disable_declarations)
    before = {key: os.environ.get(key) for key in configured}
    os.environ.update(configured)
    report = {"schema": SCHEMA, "ok": False, "measurementComplete": False,
              "capture": str(capture), "configuration": vars(args), "processId": os.getpid(),
              "environmentBefore": before, "environment": {key: os.environ.get(key) for key in configured},
              "samples": [], "visits": [], "references": [], "issues": [],
              "limits": ["One named D3D12 finite-root UAV fixture, not bindless/alias/platform acceptance",
                         "Checkpoint, prepared replay and live-forward are disabled in both runs",
                         "Full four-buffer and fixed texture payloads are compared as bytes, not only hashes",
                         "Only four canonical payloads are retained; each bounded by max-read-mib",
                         "Native restore counters describe recorded copy ranges, not PCIe traffic",
                         "Readbacks synchronize GPU; setter wall time is not input-to-display latency"]}
    recorder = Recorder(report, output, output.with_suffix(".rdoc.log"), args.max_seconds)
    recorder.publish()
    maximum = args.max_read_mib * 1024 * 1024
    payload_path = output.with_suffix(".payload.bin")
    rd, controller, cap, initialized = None, None, None, False
    canonical = {}
    buffers, ids, requests, expected_buffers, expected_pixels = [], {}, [], {}, {}

    def status():
        value = parse_status(controller.GetReplayCheckpointStatus())
        problems = status_issues(value, disabled=True)
        if problems:
            raise RuntimeError("Disabled checkpoint contract failed: " + "; ".join(problems))
        return value

    def visit(label, event, force=False):
        before_status = status()
        _, timing = recorder.measure(label + ".replay", lambda: controller.SetFrameEvent(event, force), event)
        after_status = status()
        if int(controller.GetCurrentEventID()) != event:
            raise RuntimeError("Replay did not select the exact requested EID")
        if not native_replay_present(timing["timingRecords"], event):
            raise RuntimeError("Missing actual native D3D12 WithoutDraw evidence")
        result, read_timing = recorder.measure(label + ".readback", lambda: read_state(
            controller, rd, buffers, ids, requests, expected_buffers[event], expected_pixels[event], maximum), event)
        witness, raw = result
        if int(controller.GetCurrentEventID()) != event or unchanged_checkpoint_issues(after_status, status()):
            raise RuntimeError("Readonly full-state probe disturbed the selected replay/checkpoints")
        if unchanged_checkpoint_issues(before_status, after_status):
            raise RuntimeError("Checkpoint state changed during a checkpoint-disabled visit")
        evidence = declaration_evidence(timing["timingRecords"], event, args.disable_declarations)
        restore = restore_evidence(Path(timing["timingSlice"]).read_text(encoding="utf-8"))
        report["issues"].extend(label + ": " + issue for issue in evidence["issues"] + restore["issues"])
        row = {"label": label, "eventId": event, "force": force, "witness": witness,
               "declarations": evidence, "restore": restore, "replayWallMs": timing["wallMs"],
               "readbackWallMs": read_timing["wallMs"]}
        return row, raw

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
            raise RuntimeError("The named fixture must be a valid D3D12 capture")
        opened, _ = recorder.measure("capture.openReplay", lambda: cap.OpenCapture(rd.ReplayOptions(), None))
        result, controller = opened
        if result != rd.ResultCode.Succeeded or controller is None:
            raise RuntimeError("OpenCapture failed: " + str(result))
        controller.ConfigureReplayCheckpoints(False, 0, 0)
        report["initialCheckpointStatus"] = status()
        descriptions, native_buffers = controller.GetResources(), controller.GetBuffers()
        buffers = report["buffers"] = named_buffers(descriptions, native_buffers)
        ids = {str(b.resourceId): b.resourceId for b in native_buffers}
        static_expected = {i: static_pattern(i) for i in (2, 3)}
        actions = []
        def collect(nodes):
            for action in nodes:
                if action.flags & rd.ActionFlags.Drawcall:
                    actions.append(action)
                collect(action.children)
        collect(controller.GetRootActions())
        events = report["events"] = select_events([int(a.eventId) for a in actions])
        if any(a.flags & rd.ActionFlags.Indirect for a in actions):
            raise RuntimeError("Fixture requires exactly four direct draw actions")
        report["selectedEventEvidence"] = selected_event_evidence(controller, events)
        if any(r["status"] != "exactAPIEvent" for r in report["selectedEventEvidence"]):
            raise RuntimeError("Fixture draw EIDs lack exact API-event identity")
        discovered = set()
        for event in events:
            recorder.measure("discovery.EID%d" % event, lambda e=event: controller.SetFrameEvent(e, True), event)
            pipe = controller.GetPipelineState()
            for b in list(pipe.GetOutputTargets()) + [pipe.GetDepthTarget()]:
                if b.resource != rd.ResourceId.Null():
                    discovered.add(resource_number(b.resource))
        if None in discovered or not 1 <= len(discovered) <= 16:
            raise RuntimeError("Fixture needs one to sixteen complete bound output textures")
        report["textures"], requests, estimated = texture_requests(
            controller.GetTextures(), sorted(discovered), maximum - sum(BUFFER_BYTES))
        report["maximumEstimatedWitnessBytes"] = estimated + sum(BUFFER_BYTES)
        outputs = [r for r in descriptions if str(r.name) == "UAVDeclarations_Output"]
        if (len(outputs) != 1 or len(requests) != 1 or
                str(requests[0][0]) != str(outputs[0].resourceId) or requests[0][1] != (0, 0, 0)):
            raise RuntimeError("Fixture needs its single complete named UAVDeclarations_Output texture")
        for region, event in enumerate(events):
            expected_buffers[event] = dict(static_expected)
            expected_buffers[event].update({i: written_pattern(region, i) for i in (0, 1)})
            expected_pixels[event] = {(str(requests[0][0]), (0, 0, 0)): output_pattern(region)}
        if control is not None:
            report["controlReportIdentity"] = binary_identity(control_path)
            problems = control_issues(report, control)
            if problems:
                raise RuntimeError("; ".join(problems))
            control_payload = control_path.with_suffix(".payload.bin")
            if control_payload.stat().st_size > maximum * 4 or identity_key(binary_identity(control_payload)) != identity_key(control.get("payloadIdentity", {})):
                raise RuntimeError("Disabled control canonical payload identity/budget mismatch")
        # Collect independent forced references first, preserving full canonical bytes on disk.
        # No reference payload is ever installed as a replay resource or checkpoint.
        payload_path.write_bytes(b"")
        for event in events:
            first, raw = visit("reference1.EID%d" % event, event, True)
            entry = {"offset": payload_path.stat().st_size, "bytes": len(raw),
                     "sha256": hashlib.sha256(raw).hexdigest()}
            with payload_path.open("ab") as stream:
                stream.write(raw)
            first["payload"] = entry
            canonical[event] = first
            report["references"].append(first)
            second, second_raw = visit("reference2.EID%d" % event, event, True)
            second["byteEqualReference1"] = second_raw == raw
            if not second["byteEqualReference1"] or second["witness"] != first["witness"]:
                report["issues"].append("Unstable independent canonical full bytes at EID%d" % event)
            if control is not None:
                prior = [r for r in control["references"] if r["eventId"] == event and "payload" in r]
                if len(prior) != 1:
                    raise RuntimeError("Control needs one stored canonical payload per draw")
                second["byteEqualDisabledControl"] = raw == read_payload(control_payload, prior[0]["payload"], maximum)
                if not second["byteEqualDisabledControl"] or first["witness"] != prior[0]["witness"]:
                    report["issues"].append("Cross-run canonical bytes differ at EID%d" % event)
            report["references"].append(second)
            recorder.publish()
        # Both executable UAV buffers must actually change at every newly added draw.
        report["writtenBufferContribution"] = []
        for previous, event in zip(events, events[1:]):
            a, b = canonical[previous]["witness"]["buffers"], canonical[event]["witness"]["buffers"]
            changed = all(a[i]["sha256"] != b[i]["sha256"] for i in (0, 1))
            report["writtenBufferContribution"].append({"eventId": event, "bothWrittenBuffersChanged": changed})
            if not changed:
                report["issues"].append("Both UAV writes must contribute changed data at EID%d" % event)
        for label, event, _ in sequence(events):
            row, raw = visit(label, event)
            row["byteEqualCanonical"] = raw == read_payload(payload_path, canonical[event]["payload"], maximum)
            if not row["byteEqualCanonical"] or row["witness"] != canonical[event]["witness"]:
                report["issues"].append(label + ": full buffer/output bytes differ from forced references")
            report["visits"].append(row)
            recorder.publish()
        report["nativeTrimRecords"] = sum(v["declarations"]["nativeTrimRecords"] for v in report["visits"])
        if not all(v["declarations"]["records"] and v["restore"]["records"] for v in report["visits"][1:]):
            report["issues"].append("Every non-seed visit needs native submitted-write and initial-restore records")
        if not args.disable_declarations:
            if report["nativeTrimRecords"] == 0:
                report["issues"].append("No actual submitted native declaration trim was observed")
            report["restoreReduction"], problems = compare_restores(
                report["visits"], control["visits"], [b["resource"] for b in buffers[2:]], args.min_reduced_visits)
            report["issues"].extend(problems)
        report["payloadIdentity"] = binary_identity(payload_path)
        report["measurementComplete"], report["ok"] = True, not report["issues"]
        report["verdict"] = (("passedUntrimmedControlAndFullBytes" if args.disable_declarations else
                              "passedNativeTrimRestoreReductionAndFullBytes") if report["ok"] else "failedOrInconclusive")
    except Exception as error:
        report.update(ok=False, error=str(error), traceback=traceback.format_exc(), verdict="failedOrInconclusive")
    finally:
        for label, callback in (("cleanup.replay", controller.Shutdown if controller else None),
                                ("cleanup.capture", cap.Shutdown if cap else None),
                                ("cleanup.runtime", rd.ShutdownReplay if initialized else None)):
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
