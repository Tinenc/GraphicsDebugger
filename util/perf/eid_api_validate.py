"""Validate actual EID scrub replay against canonical replay on captured API demos.

Run with standalone Python 3.6 matching renderdoc.pyd, not inside qrenderdoc. Pass
--capture-results from capture_api_smoke.py and optionally --output/--driver-log.
Environment equivalents: RENDERDOC_EID_API_VALIDATE_INPUT, _OUTPUT, _DRIVER_LOG.
By default, timing evidence comes from an owned RenderDoc diagnostic log beside the report.
This reads native texture bytes for correctness; its timings are not UI latency.
"""
from __future__ import print_function

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import traceback

import renderdoc as rd


def leaf_actions(actions):
    for action in actions:
        if action.children:
            for child in leaf_actions(action.children):
                yield child
        else:
            yield action


def select_actions(controller, maximum):
    leaves = sorted(leaf_actions(controller.GetRootActions()), key=lambda a: a.eventId)
    eligible = [a for a in leaves if a.flags & (rd.ActionFlags.Clear | rd.ActionFlags.Drawcall)]
    draws = [a for a in eligible if a.flags & rd.ActionFlags.Drawcall]
    if len(eligible) < 2 or not draws:
        raise RuntimeError("Capture requires at least two Clear/Draw leaf actions and one Draw")
    # Always include the final draw whose output is a stable readback target for clear actions.
    selected = eligible[:maximum]
    if draws[-1] not in selected:
        selected[-1] = draws[-1]
    return sorted(selected, key=lambda a: a.eventId), draws[-1]


def descriptor_record(descriptor):
    return {"resource": str(descriptor.resource),
            "firstMip": int(descriptor.firstMip), "firstSlice": int(descriptor.firstSlice),
            "format": descriptor.format.Name(), "type": str(descriptor.type)}


def pipeline_record(controller):
    pipe = controller.GetPipelineState()
    stages = ("Vertex", "Hull", "Domain", "Geometry", "Pixel", "Compute")
    if controller.GetAPIProperties().pipelineType == rd.GraphicsAPI.Vulkan:
        stages += ("Task", "Mesh")
    return {"shaders": {stage: str(pipe.GetShader(getattr(rd.ShaderStage, stage)))
                        for stage in stages},
            "targets": [descriptor_record(target) for target in pipe.GetOutputTargets()],
            "depthTarget": descriptor_record(pipe.GetDepthTarget())}


def fatal_record(controller):
    status = controller.GetFatalErrorStatus()
    return {"code": str(status.code), "details": str(status),
            "ok": status.code == rd.ResultCode.Succeeded}


def read_state(controller, requested, textures, fixed_targets):
    event_id = int(controller.GetCurrentEventID())
    pipeline = pipeline_record(controller)
    pipe = controller.GetPipelineState()
    specs = [(target.resource, max(0, int(target.firstMip)), max(0, int(target.firstSlice)))
             for target in list(pipe.GetOutputTargets()) + [pipe.GetDepthTarget()]]
    specs += [(resource, 0, 0) for resource in fixed_targets]
    records = []
    seen = set()
    for resource, mip, slice_index in specs:
        key = (str(resource), mip, slice_index)
        if resource == rd.ResourceId.Null() or key in seen or resource not in textures:
            continue
        seen.add(key)
        texture = textures[resource]
        subresource = rd.Subresource(mip, slice_index, 0)
        data = bytes(controller.GetTextureData(resource, subresource))
        if not data:
            raise RuntimeError("Empty native texture readback: " + str(resource))
        records.append({"resource": str(resource), "mip": mip, "slice": slice_index,
                        "sample": 0, "width": int(texture.width), "height": int(texture.height),
                        "format": texture.format.Name(), "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()})
    records.sort(key=lambda record: (record["resource"], record["mip"], record["slice"]))
    fatal = fatal_record(controller)
    if not fatal["ok"]:
        raise RuntimeError("Fatal replay status: " + fatal["details"])
    if event_id != requested or not records:
        raise RuntimeError("Event identity mismatch or no readable render targets at EID %d" % requested)
    return {"eventId": event_id, "pipeline": pipeline, "targets": records, "fatal": fatal}


def log_position(path):
    return path.stat().st_size if path.is_file() else 0


def log_since(path, offset):
    # The Windows driver briefly holds an exclusive append handle.
    for retry in range(100):
        try:
            with path.open("rb") as source:
                source.seek(offset)
                return source.read().decode("utf-8", "replace").splitlines()
        except PermissionError:
            time.sleep(0.01)
    raise RuntimeError("Could not read driver timing evidence: " + str(path))


def replay_evidence(lines, event_id):
    relevant = [line for line in lines if re.search(
        r"\bapi=(?:ScrubReplay|ScrubCommit|ScrubSnapshotHit|ScrubCommitCacheHit)\b", line)]
    replay = [line for line in relevant if re.search(
        r"\bapi=ScrubReplay\s+eid=%d\b" % event_id, line)]
    commit = [line for line in relevant if re.search(
        r"\bapi=ScrubCommit\s+eid=%d\b" % event_id, line)]
    snapshot = [line for line in relevant if "SnapshotHit" in line or "CommitCacheHit" in line]
    commit_without_replay = len(commit) == 1 and re.search(r"\breplay_ms=0\.000\b", commit[0]) is not None
    return {"ok": len(replay) == 1 and commit_without_replay and not snapshot,
            "realReplayRecords": len(replay), "commitWithoutReplay": commit_without_replay,
            "snapshotHitRecords": len(snapshot), "lines": relevant}


def comparable(state):
    return {"eventId": state["eventId"], "pipeline": state["pipeline"], "targets": state["targets"]}


def validate_capture(entry, manifest_dir, driver_log, maximum):
    result = {"api": entry.get("api"), "capture": entry.get("capture"), "ok": False,
              "samples": []}
    capture_file = None
    controller = None
    started = time.perf_counter()
    try:
        if not entry.get("ok") or not entry.get("capture"):
            raise RuntimeError("The capture manifest entry was not successful")
        capture_path = Path(entry["capture"])
        if not capture_path.is_absolute():
            capture_path = manifest_dir / capture_path
        capture_path = capture_path.resolve()
        result["capture"] = str(capture_path)
        capture_file = rd.OpenCaptureFile()
        status = capture_file.OpenFile(str(capture_path), "", None)
        result["openFileStatus"] = str(status)
        if status != rd.ResultCode.Succeeded:
            raise RuntimeError("OpenFile failed: " + str(status))
        result["driver"] = capture_file.DriverName()
        if entry.get("driver") and result["driver"] != entry["driver"]:
            raise RuntimeError("Capture driver does not match the manifest")
        status, controller = capture_file.OpenCapture(rd.ReplayOptions(), None)
        result["openReplayStatus"] = str(status)
        if status != rd.ResultCode.Succeeded or controller is None:
            raise RuntimeError("OpenCapture failed: " + str(status))

        result["replayAPI"] = str(controller.GetAPIProperties().pipelineType)
        textures = {texture.resourceId: texture for texture in controller.GetTextures()}
        selected, last_draw = select_actions(controller, maximum)
        # Captured action metadata avoids warming pipeline feedback before the first scrub visit.
        fixed_targets = [resource for resource in list(last_draw.outputs) + [last_draw.depthOut]
                         if resource != rd.ResourceId.Null() and resource in textures]
        if not fixed_targets:
            raise RuntimeError("The final Draw leaf has no captured readable output texture")
        result["fixedReadbackSourceEvent"] = int(last_draw.eventId)
        result["fixedReadbackTargets"] = [str(resource) for resource in fixed_targets]
        result["actions"] = [{"eventId": int(action.eventId), "flags": str(action.flags),
                              "name": str(action.customName)} for action in selected]
        first_visits = {}

        for direction, actions in (("forward", selected), ("reverse", list(reversed(selected)))):
            for action in actions:
                event_id = int(action.eventId)
                sample = {"direction": direction, "eventId": event_id}
                result["samples"].append(sample)
                offset = log_position(driver_log)
                begin = time.perf_counter()
                controller.SetSelectedEventID(event_id)
                controller.ScrubFrameEvent(event_id)
                controller.CommitScrubFrameEvent(event_id)
                sample["scrubAndPipelineMs"] = (time.perf_counter() - begin) * 1000.0
                sample["scrub"] = read_state(controller, event_id, textures, fixed_targets)
                sample["evidence"] = replay_evidence(log_since(driver_log, offset), event_id)

                begin = time.perf_counter()
                controller.SetSelectedEventID(event_id)
                controller.SetFrameEvent(event_id, True)
                sample["canonicalReplayMs"] = (time.perf_counter() - begin) * 1000.0
                sample["canonical"] = read_state(controller, event_id, textures, fixed_targets)
                sample["pipelineIdentical"] = sample["scrub"]["pipeline"] == sample["canonical"]["pipeline"]
                sample["nativePixelsIdentical"] = sample["scrub"]["targets"] == sample["canonical"]["targets"]
                state = comparable(sample["scrub"])
                sample["repeatIdentical"] = event_id not in first_visits or first_visits[event_id] == state
                first_visits.setdefault(event_id, state)
                sample["ok"] = (sample["pipelineIdentical"] and sample["nativePixelsIdentical"]
                                and sample["repeatIdentical"] and sample["evidence"]["ok"])

        result["fatal"] = fatal_record(controller)
        result["debugMessages"] = [{"severity": str(message.severity),
                                    "description": str(message.description)}
                                   for message in controller.GetDebugMessages()]
        result["ok"] = result["fatal"]["ok"] and all(sample.get("ok", False) for sample in result["samples"])
    except Exception as error:
        result.update({"error": str(error), "traceback": traceback.format_exc()})
        if controller is not None:
            try:
                result["fatal"] = fatal_record(controller)
            except Exception:
                pass
    finally:
        if controller is not None:
            controller.Shutdown()
        if capture_file is not None:
            capture_file.Shutdown()
        result["elapsedMs"] = (time.perf_counter() - started) * 1000.0
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-results", default=os.environ.get("RENDERDOC_EID_API_VALIDATE_INPUT"))
    parser.add_argument("--output", default=os.environ.get("RENDERDOC_EID_API_VALIDATE_OUTPUT"))
    parser.add_argument("--driver-log", default=os.environ.get("RENDERDOC_EID_API_VALIDATE_DRIVER_LOG"))
    parser.add_argument("--max-events", type=int, default=6)
    args = parser.parse_args()
    if "pyrenderdoc" in globals():
        parser.error("Use a standalone replay Python process")
    if not args.capture_results or args.max_events < 2:
        parser.error("--capture-results and --max-events >= 2 are required")
    manifest_path = Path(args.capture_results).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    entries = manifest.get("captures", [])
    if not entries:
        parser.error("The manifest contains no captures")
    output = Path(args.output).resolve() if args.output else manifest_path.with_name("eid-api-validation.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    os.environ["RENDERDOC_EID_TIMING"] = "1"
    result = {"schema": "renderdoc-eid-api-validation/v1", "ok": False,
              "python": sys.version, "captureManifest": str(manifest_path),
              "captures": [],
              "comparison": "actual Scrub+Commit versus forced canonical; forward and reverse native-byte readbacks"}

    def publish():
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        if args.driver_log:
            driver_log = Path(args.driver_log).resolve()
            result["timingLogKind"] = "explicit driver timing log"
        else:
            # An embedded Python host may live in a read-only system directory. RDCLOG carries
            # the same common-core replay records in a writable, report-owned diagnostic file.
            rd.SetDebugLogFile(str(output.with_suffix(".rdoc.log")))
            driver_log = Path(rd.GetLogFile()).resolve()
            result["timingLogKind"] = "RenderDoc diagnostic log, EIDTiming records"
        result["driverTimingLog"] = str(driver_log)
        for entry in entries:
            result["captures"].append(validate_capture(entry, manifest_path.parent, driver_log, args.max_events))
            publish()
        result["ok"] = all(entry["ok"] for entry in result["captures"])
    finally:
        rd.ShutdownReplay()
        publish()
    print(json.dumps({"ok": result["ok"], "output": str(output),
                      "captures": [{"api": entry["api"], "ok": entry["ok"],
                                    "samples": len(entry["samples"])} for entry in result["captures"]]}))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
