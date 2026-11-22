"""Byte-exact acceptance of the six-draw D3D12_Sparse_Initial_Residency fixture.

Run an independent disabled process first, then an enabled process with --control-report.
Main uses the GPU; importing the pure acceptance helpers never imports renderdoc.
"""
from __future__ import print_function

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback

from discover_d3d12_checkpoint_fixture import collect_actions, select_fixture_events
from eid_checkpoint_profile import parse_status, selected_event_evidence, status_issues
from eid_history_profile import Recorder, resource_number
from eid_live_forward_profile import texture_requests
from eid_shared_buffer_profile import binary_identity, build_identity, unchanged_checkpoint_issues
from eid_uav_declarations_profile import (
    environment as base_environment, identity_key, native_replay_present, read_payload,
    read_state, restore_evidence)


SCHEMA = "renderdoc-sparse-initial-profile/v1"
SWITCH = "RENDERDOC_D3D12_ENABLE_SPARSE_INITIAL_RESIDENCY"
TEXTURE_NAMES = ("SparseInitial_Texture", "SparseInitial_Output")
TEXTURE_BYTES = 128 * 128 * 4
PAYLOAD_BYTES = TEXTURE_BYTES * 7
SEQUENCE = (0, 1, 0, 2, 0, 1, 4, 0, 1)
KINDS = ("excludedSeed", "readRetain", "readRetain", "retainThenCopy", "rebuildAfterCopy",
         "readRetain", "retainThenMapping", "rebuildAfterMapping", "readRetain")
MAX_REPORT_BYTES = 32 * 1024 * 1024


def environment(disabled, reference=False):
    result = base_environment(True)
    result.update({SWITCH: "0" if disabled or reference else "1",
                   "RENDERDOC_D3D12_DISABLE_DIRTY_RESTORE": "1" if reference else "0",
                   "RENDERDOC_D3D12_CONSERVATIVE_REPLAY_SYNC": "0",
                   "RENDERDOC_D3D12_DISABLE_SPARSE_DIRTY_CLOSURE": "0",
                   "RENDERDOC_D3D12_DISABLE_DIRTY_WRITE_CUTOFF": "0"})
    return result


def cold_start_issues(records, event):
    # OpenCapture leaves the device in Loading. Its first root really applies initial contents,
    # but the detailed sparse/dirty logs intentionally only describe ActiveReplaying restores.
    # Prove that excluded transition with the normal root counters; do not use it as a reference
    # or as measured reuse. Every subsequent reference/ordinary visit keeps the stricter logs.
    roots = [r for r in records if r.get("api") == "D3D12" and r.get("type") == "WithoutDraw" and
             r.get("range") in ("0->%d" % event, "1->%d" % event) and r.get("replay_depth") == 0]
    if len(roots) != 1:
        return ["Cold transition requires one actual native root WithoutDraw replay"]
    row = roots[0]
    if (row.get("partial") != 0 or row.get("section.applyInitial_calls") != 1 or
            row.get("count.initialSparseResources") != 1 or
            row.get("count.initialGPUCopyFootprintBytes", 0) < TEXTURE_BYTES):
        return ["Cold transition lacks actual complete sparse initial-copy execution evidence"]
    return []


def texture_pattern(pattern):
    if type(pattern) is not int or pattern not in range(3):
        raise ValueError("Sparse texture pattern must be 0, 1 or 2")
    data = bytearray(TEXTURE_BYTES)
    for y in range(128):
        for x in range(128):
            offset = (y * 128 + x) * 4
            data[offset:offset + 4] = bytes((255 - x if pattern == 1 else x,
                                            255 - y if pattern == 2 else y,
                                            (17, 101, 203)[pattern], 255))
    return bytes(data)


def output_pattern(region):
    if type(region) is not int or region not in range(6):
        raise ValueError("Sparse fixture region must be 0..5")
    patterns = [texture_pattern(i) for i in range(3)]
    result = bytearray(b"\x00\x00\x00\xff" * (768 * 128))
    for y in range(128):
        for part in range(region + 1):
            offset = (y * 768 + part * 128) * 4
            result[offset:offset + 512] = patterns[part // 2][y * 512:(y + 1) * 512]
    return bytes(result)


def fixture_events(actions, mappings):
    selection = select_fixture_events(actions)
    events = selection["selectedEvents"]
    if len(events) != 6:
        raise ValueError("Sparse fixture needs exactly six actual draw submissions")
    ordered = sorted(actions, key=lambda row: row["eventId"])
    for region, event in enumerate(events):
        index = next(i for i, row in enumerate(ordered) if row["eventId"] == event)
        marker = ordered[index - 1] if index else {}
        if (marker.get("name") != "Sparse initial region %d" % region or
                not marker.get("marker") or
                not (marker.get("exactAPI") or {}).get("chunkName", "").endswith("SetMarker")):
            raise ValueError("Draw must immediately follow its exact native region marker")
    chunks = [row["exactAPI"]["chunkIndex"] for row in selection["selectedActions"]]
    if chunks != sorted(set(chunks)) or len(mappings) != 2:
        raise ValueError("Expected distinct native draws and two real frame mapping operations")
    if not chunks[3] < mappings[0]["chunkIndex"] < chunks[4] < chunks[5] < mappings[1]["chunkIndex"]:
        raise ValueError("Mapping must change before region 4 and reset only after all targets")
    groups = [g for g in selection["submissionGroups"] if g["draws"]]
    for region, marker_name in ((2, "Sparse initial copy B"), (4, "Sparse initial initialize mapped C")):
        group = groups[region]
        rows = [r for r in ordered if group["begin"]["eventId"] < r["eventId"] < events[region]]
        names = [r["name"] for r in rows]
        if names.count(marker_name) != 1:
            raise ValueError("Missing exact fixture copy marker")
        next_index = names.index(marker_name) + 1
        if (next_index >= len(rows) or not
                (rows[next_index].get("exactAPI") or {}).get("chunkName", "").endswith("CopyTextureRegion")):
            raise ValueError("Copy marker must be followed by a real CopyTextureRegion action")
    return selection


def named_textures(descriptions, textures):
    result = []
    for name, width in zip(TEXTURE_NAMES, (128, 768)):
        named = [r for r in descriptions if str(r.name) == name]
        found = [t for t in textures if len(named) == 1 and t.resourceId == named[0].resourceId]
        if len(found) != 1:
            raise ValueError("Missing or duplicate named sparse fixture texture: " + name)
        t = found[0]
        if [int(getattr(t, k)) for k in ("width", "height", "depth", "mips", "arraysize", "msSamp")] != [width, 128, 1, 1, 1, 1]:
            raise ValueError("Sparse fixture requires the complete native single-mip RGBA8 texture")
        result.append({"name": name, "resource": str(t.resourceId), "resourceId": resource_number(t.resourceId),
                       "width": width, "height": 128, "bytes": width * 128 * 4, "format": t.format.Name()})
    if len({r["resource"] for r in result}) != 2:
        raise ValueError("Sparse asset and output must have distinct original identities")
    return result


def sparse_evidence(records, resource, disabled):
    rows, seen, mirrors, issues = [], set(), 0, []
    for row in records:
        if row.get("api") == "D3D12LiveForward" and row.get("hit") != 0:
            issues.append("Live-forward must remain disabled")
        if row.get("api") == "D3D12Prepared" and (row.get("enabled") != 0 or row.get("hits") != 0):
            issues.append("Prepared replay must remain disabled")
        if row.get("api") != "D3D12SparseInitial":
            continue
        key = tuple(sorted((k, v) for k, v in row.items() if k != "line"))
        if key in seen:
            mirrors += 1
            continue
        seen.add(key)
        rows.append(row)
    prepares = [r for r in rows if r.get("op") == "prepare"]
    submits = [r for r in rows if r.get("op") == "submit"]
    retains = [r for r in rows if r.get("op") == "retain"]
    if len(prepares) != 1:
        issues.append("Every measured root needs exactly one native sparse component preparation")
    for row in prepares:
        if (row.get("resource") != resource or row.get("eligible") != 1 or
                row.get("enabled") != int(not disabled) or row.get("firstReset") != 1):
            issues.append("Native complete-initial-copy eligibility/identity/switch was not established")
    for row in rows:
        fields = ("component", "generation") + {
            "prepare": ("eligible", "enabled", "firstReset", "recordingAllowed", "preInitialDirty", "retained", "residentCopyBytes"),
            "submit": ("published", "recordedCopyBytes"),
            "retain": ("avoidedRecordedCopyBytes",)}.get(row.get("op"), ())
        if row.get("op") not in ("prepare", "submit", "retain") or any(
                type(row.get(k)) is not int or row[k] < 0 for k in fields):
            issues.append("Missing or invalid native sparse transaction counters")
        for key in ("eligible", "enabled", "firstReset", "recordingAllowed", "preInitialDirty", "retained", "published"):
            if key in row and (type(row[key]) is not int or row[key] not in (0, 1)):
                issues.append("Invalid native sparse transaction boolean")
    if prepares:
        component = prepares[0].get("component")
        if any(r.get("component") != component or r.get("generation") != prepares[0].get("generation") for r in rows):
            issues.append("Sparse evidence mixed physical components or initial generations")
    if disabled and (retains or any(r.get("published") != 0 for r in submits) or
                     any(r.get("retained") != 0 for r in prepares)):
        issues.append("Disabled run unexpectedly retained or published sparse contents")
    return {"records": rows, "prepare": prepares[0] if len(prepares) == 1 else {},
            "submits": submits, "retains": retains, "mirrorRecordsRemoved": mirrors, "issues": issues}


def actual_copies(restore, resource):
    rows = restore.get("records", [])
    resources = [r for row in rows for r in row.get("resources", [])]
    return {"calls": len(rows), "total": sum(r.get("actualCopyBytes", 0) for r in rows),
            "texture": sum(r["actualCopyBytes"] for r in resources if r["resource"] == resource),
            "whole": {r["resource"]: r["wholeResourceCopies"] for r in resources if r["wholeResourceCopies"]},
            "complete": len(rows) == 1 and not restore.get("issues") and
                        all(r.get("enabled") == 1 and r.get("shadow") == 0 for r in rows)}


def accept_visits(visits, resource, disabled, control=None):
    issues, comparisons = [], []
    if [v.get("region") for v in visits] != list(SEQUENCE) or [v.get("kind") for v in visits] != list(KINDS):
        return [], ["Ordinary native sequence differs from the prescribed nine unforced visits"]
    if any(v.get("force") is not False for v in visits):
        issues.append("Ordinary sequence may not contain forced replays")
    if control is not None and [(v["eventId"], v["kind"]) for v in visits] != [
            (v["eventId"], v["kind"]) for v in control]:
        return [], issues + ["Enabled/control ordinary visits do not match"]
    for index, visit in enumerate(visits):
        proof = visit["sparse"]
        issues.extend(visit["label"] + ": " + value for value in proof["issues"] + visit["restore"]["issues"])
        on = actual_copies(visit["restore"], resource)
        if not index:
            comparisons.append({"label": visit["label"], "excludedSeed": True,
                                "reason": "First ordinary replay after canonical forced references"})
            continue
        if not on["complete"]:
            issues.append(visit["label"] + ": missing complete actual root copy accounting")
        if disabled:
            if on["texture"] < TEXTURE_BYTES:
                issues.append(visit["label"] + ": disabled control must really restore sparse texture bytes")
            continue
        prepare = proof["prepare"]
        retained_bytes = sum(r.get("avoidedRecordedCopyBytes", 0) for r in proof["retains"] if r.get("resource") == resource)
        off = actual_copies(control[index]["restore"], resource) if control is not None else {}
        if not off.get("complete") or off.get("calls") != on["calls"] or off.get("whole") != on["whole"]:
            issues.append(visit["label"] + ": disabled control copy evidence is not comparable")
        rebuild = visit["kind"] in ("rebuildAfterCopy", "rebuildAfterMapping")
        if rebuild:
            passed = (prepare.get("preInitialDirty") == 1 and prepare.get("retained") == 0 and
                      not proof["retains"] and on["texture"] >= TEXTURE_BYTES and
                      any(r.get("published") == 1 and r.get("recordedCopyBytes", 0) >= TEXTURE_BYTES
                          for r in proof["submits"]))
        else:
            passed = (prepare.get("preInitialDirty") == 0 and prepare.get("retained") == 1 and
                      retained_bytes >= TEXTURE_BYTES and on["texture"] == 0 and
                      off.get("texture", 0) - on["texture"] >= TEXTURE_BYTES and
                      off.get("total", 0) - on["total"] >= TEXTURE_BYTES)
        comparisons.append({"label": visit["label"], "kind": visit["kind"], "excludedSeed": False,
                            "enabledCopies": on, "disabledCopies": off, "retainedRecordedBytes": retained_bytes,
                            "passedActualRebuild" if rebuild else "passedActualReduction": passed})
        if not passed:
            issues.append(visit["label"] + ": required actual rebuild/reuse and byte reduction did not occur")
    return comparisons, issues


def control_issues(report, control):
    issues = []
    if (control.get("schema") != SCHEMA or control.get("ok") is not True or
            control.get("measurementComplete") is not True or
            control.get("configuration", {}).get("disable_residency") is not True or
            control.get("processId") == report["processId"]):
        issues.append("Control must be a completed successful disabled run from another process")
    if identity_key(report["captureIdentity"]) != identity_key(control.get("captureIdentity", {})):
        issues.append("Control capture bytes/hash differ")
    current, prior = report["buildIdentity"], control.get("buildIdentity", {})
    if (not current.get("nativeModules") or not prior.get("nativeModules") or
            sorted(identity_key(v) for v in current["nativeModules"]) !=
            sorted(identity_key(v) for v in prior.get("nativeModules", [])) or
            identity_key(current["pythonModule"]) != identity_key(prior.get("pythonModule", {}))):
        issues.append("Control native library/Python binding differs")
    for key in ("events", "textures", "referenceEnvironment"):
        if report[key] != control.get(key):
            issues.append("Control fixture/reference identity differs: " + key)
    on, off = dict(report["environment"]), dict(control.get("environment", {}))
    if on.pop(SWITCH, None) != "1" or off.pop(SWITCH, None) != "0" or on != off:
        issues.append("Control environment differs beyond the sparse residency switch")
    return issues


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--disable-residency", action="store_true")
    parser.add_argument("--control-report")
    parser.add_argument("--max-read-mib", type=int, default=2)
    parser.add_argument("--max-seconds", type=int, default=180)
    parser.add_argument("--build-label", default="unspecified")
    args = parser.parse_args(argv)
    try:
        if not 2 <= args.max_read_mib <= 16 or not 1 <= args.max_seconds <= 600:
            raise ValueError("Bounds: max-read-mib 2..16, max-seconds 1..600")
        if args.disable_residency == bool(args.control_report):
            raise ValueError("Enabled mode requires --control-report; disabled mode must omit it")
        capture, output = Path(args.capture).resolve(), Path(args.output).resolve()
        if not capture.is_file() or output.suffix != ".json" or any(output.with_suffix(s).exists() for s in
                (".json", ".rdoc.log", ".native.log", ".payload.bin")):
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
    configured, reference_env = environment(args.disable_residency), environment(True, reference=True)
    before = {key: os.environ.get(key) for key in configured}
    os.environ.update(reference_env)
    report = {"schema": SCHEMA, "ok": False, "measurementComplete": False, "capture": str(capture),
              "configuration": vars(args), "processId": os.getpid(), "environmentBefore": before,
              "environment": configured, "referenceEnvironment": reference_env,
              "samples": [], "visits": [], "references": [], "issues": [],
              "excludedSeedVisitIndices": [0], "limits": [
                  "One fully mapped reserved texture; no placed/sparse alias checkpoint acceptance",
                  "Two full native texture subresources per readback; six stored canonical payloads",
                  "Forced references disable sparse residency AND generic dirty-restore reuse",
                  "Checkpoints, live-forward, prepared replay and other experimental copy shortcuts disabled",
                  "Ordinary nine-visit sequence has no forced references between visits",
                  "Byte counters describe recorded copy footprints, not physical GPU bus traffic",
                  "GPU readbacks synchronize; setter wall time is not interactive presentation latency"]}
    recorder = Recorder(report, output, output.with_suffix(".rdoc.log"), args.max_seconds)
    recorder.publish()
    maximum = args.max_read_mib * 1024 * 1024
    payload_path = output.with_suffix(".payload.bin")
    controller = cap = None
    initialized = False
    canonical, expected = {}, {}

    def status():
        value = parse_status(controller.GetReplayCheckpointStatus())
        problems = status_issues(value, disabled=True)
        if problems:
            raise RuntimeError("Disabled checkpoint contract: " + "; ".join(problems))
        return value

    def visit(label, event, force=False):
        before_status = status()
        _, timing = recorder.measure(label + ".replay", lambda: controller.SetFrameEvent(event, force), event)
        after_status = status()
        if int(controller.GetCurrentEventID()) != event or not native_replay_present(timing["timingRecords"], event):
            raise RuntimeError("Missing exact selected EID/native root WithoutDraw evidence")
        result, read_timing = recorder.measure(label + ".readback", lambda: read_state(
            controller, rd, [], {}, requests, {}, expected[event], maximum), event)
        witness, raw = result
        if (len(raw) != PAYLOAD_BYTES or int(controller.GetCurrentEventID()) != event or
                unchanged_checkpoint_issues(after_status, status()) or unchanged_checkpoint_issues(before_status, after_status)):
            raise RuntimeError("Complete native probe disturbed selection/checkpoints or omitted bytes")
        active = {key: os.environ.get(key) for key in configured}
        proof = sparse_evidence(timing["timingRecords"], textures[0]["resource"], active[SWITCH] == "0")
        restore = restore_evidence(Path(timing["timingSlice"]).read_text(encoding="utf-8"))
        if len(restore["records"]) != 1:
            raise RuntimeError("Each setter needs one complete native initial restoration record")
        bound = [b for b in witness["bindings"] if resource_number(b["resource"])]
        if len(bound) != 1 or bound[0]["kind"] != "color" or bound[0]["slot"] != 0 or bound[0]["resource"] != textures[1]["resource"]:
            raise RuntimeError("Selected draw must bind only the named complete output at RT0")
        report["issues"].extend(label + ": " + issue for issue in proof["issues"] + restore["issues"])
        return {"label": label, "eventId": event, "force": force, "environment": active,
                "witness": witness, "sparse": proof, "restore": restore,
                "replayWallMs": timing["wallMs"], "readbackWallMs": read_timing["wallMs"]}, raw

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
            raise RuntimeError("Expected a valid D3D12 sparse fixture capture")
        opened, _ = recorder.measure("capture.openReplay", lambda: cap.OpenCapture(rd.ReplayOptions(), None))
        result, controller = opened
        if result != rd.ResultCode.Succeeded or controller is None:
            raise RuntimeError("OpenCapture failed: " + str(result))
        controller.ConfigureReplayCheckpoints(False, 0, 0)
        report["initialCheckpointStatus"] = status()
        actions, submits = collect_actions(controller, rd)
        structured = controller.GetStructuredFile()
        if len(structured.chunks) > 20000:
            raise RuntimeError("Sparse fixture structured discovery exceeds 20000 chunks")
        mappings = [{"chunkIndex": i, "chunkName": str(c.name)} for i, c in enumerate(structured.chunks)
                    if str(c.name).endswith("UpdateTileMappings")]
        report["fixture"] = fixture_events(actions, mappings)
        report["mappingOperations"], report["nativeSubmissions"] = mappings, submits
        events = report["events"] = report["fixture"]["selectedEvents"]
        report["selectedEventEvidence"] = selected_event_evidence(controller, events)
        if any(r["status"] != "exactAPIEvent" for r in report["selectedEventEvidence"]):
            raise RuntimeError("Fixture targets lack exact draw API evidence")
        textures = report["textures"] = named_textures(controller.GetResources(), controller.GetTextures())
        _, requests, estimated = texture_requests(controller.GetTextures(), [t["resourceId"] for t in textures], maximum)
        report["maximumEstimatedWitnessBytes"] = estimated
        if len(requests) != 2 or any(sub != (0, 0, 0) for _, sub in requests):
            raise RuntimeError("Expected exactly two complete single-subresource textures")
        for region, event in enumerate(events):
            expected[event] = {(textures[0]["resource"], (0, 0, 0)): texture_pattern(region // 2),
                               (textures[1]["resource"], (0, 0, 0)): output_pattern(region)}
        if control is not None:
            report["controlReportIdentity"] = binary_identity(control_path)
            problems = control_issues(report, control)
            if problems:
                raise RuntimeError("; ".join(problems))
            control_payload = control_path.with_suffix(".payload.bin")
            if control_payload.stat().st_size != PAYLOAD_BYTES * 6 or identity_key(binary_identity(control_payload)) != identity_key(control.get("payloadIdentity", {})):
                raise RuntimeError("Disabled canonical payload identity/complete byte budget differs")
        _, cold = recorder.measure("capture.coldWarmup", lambda: controller.SetFrameEvent(events[0], True), events[0])
        report["excludedColdWarmup"] = {
            "eventId": events[0], "force": True, "environment": dict(reference_env),
            "reason": "Loading-to-Active transition; excluded from both canonical references and performance acceptance",
            "issues": cold_start_issues(cold["timingRecords"], events[0])}
        if (report["excludedColdWarmup"]["issues"] or int(controller.GetCurrentEventID()) != events[0] or
                controller.GetFatalErrorStatus().code != rd.ResultCode.Succeeded):
            raise RuntimeError("Excluded cold replay did not establish a successful active replay state")
        # Independent forced references are taken with BOTH residency and dirty reuse disabled.
        # Keep this entire phase outside the unforced measured sequence.
        payload_path.write_bytes(b"")
        for event in events:
            first, raw = visit("reference1.EID%d" % event, event, True)
            first["payload"] = {"offset": payload_path.stat().st_size, "bytes": len(raw),
                                "sha256": hashlib.sha256(raw).hexdigest()}
            with payload_path.open("ab") as stream:
                stream.write(raw)
            canonical[event] = first
            report["references"].append(first)
            second, other = visit("reference2.EID%d" % event, event, True)
            second["byteEqualReference1"] = other == raw
            if other != raw or first["witness"] != second["witness"]:
                report["issues"].append("Independent forced references are unstable at EID%d" % event)
            if any(r.get("enabled") != 0 for row in (first, second) for r in row["restore"]["records"]):
                report["issues"].append("Forced reference reused dirty initial contents")
            if control is not None:
                prior = [r for r in control["references"] if r["eventId"] == event and "payload" in r]
                if len(prior) != 1:
                    raise RuntimeError("Control needs one stored canonical byte payload per target")
                second["byteEqualDisabledControl"] = raw == read_payload(control_payload, prior[0]["payload"], maximum)
                if not second["byteEqualDisabledControl"] or first["witness"] != prior[0]["witness"]:
                    report["issues"].append("Cross-process canonical bytes/bindings differ at EID%d" % event)
            report["references"].append(second)
            recorder.publish()
        os.environ.update(configured)
        report["environment"] = {key: os.environ.get(key) for key in configured}
        for index, (region, kind) in enumerate(zip(SEQUENCE, KINDS)):
            event = events[region]
            row, raw = visit("visit%d.%s.EID%d" % (index, kind, event), event)
            row.update(region=region, kind=kind, excludedSeed=index == 0,
                       byteEqualCanonical=raw == read_payload(payload_path, canonical[event]["payload"], maximum))
            if not row["byteEqualCanonical"] or row["witness"] != canonical[event]["witness"]:
                report["issues"].append(row["label"] + ": full bytes/bindings differ from canonical references")
            report["visits"].append(row)
            recorder.publish()
        report["acceptance"], problems = accept_visits(report["visits"], textures[0]["resource"],
                                                       args.disable_residency, control["visits"] if control else None)
        report["issues"].extend(problems)
        report["payloadIdentity"] = binary_identity(payload_path)
        report["measurementComplete"], report["ok"] = True, not report["issues"]
        report["verdict"] = (("passedDisabledCanonicalControl" if args.disable_residency else
                              "passedSparseRetentionCopyMappingInvalidationAndFullBytes") if report["ok"] else "failedOrInconclusive")
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
