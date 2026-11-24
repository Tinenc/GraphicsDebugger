"""Bounded replay/History profiling in a standalone, matching RenderDoc Python process.

No capture edits, UI automation, or full-history sweep. Running this script DOES open
the capture and use the GPU; coordinate it with other replay processes first.
"""
from __future__ import print_function

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_eid_timing import parse_records, summarize


def build_candidates(usage, mip):
    """Match RenderTargetHistoryView's resource-wide conservative write candidates."""
    priorities = {"Clear": 100, "ResolveDst": 90, "Resolve": 90, "CopyDst": 80,
                  "Copy": 80, "ColorTarget": 70, "DepthStencilTarget": 60,
                  "GenMips": 50, "Discard": 40}
    unique = {}
    for event_id, usage_name in usage:
        name = usage_name.rsplit(".", 1)[-1]
        priority = priorities.get(name, 30 if name.endswith("_RWResource") else 0)
        if not event_id or not priority or (name == "GenMips" and mip == 0):
            continue
        if event_id not in unique or priority > unique[event_id][0]:
            unique[event_id] = (priority, name)
    return [{"eventId": eid, "usage": unique[eid][1]} for eid in sorted(unique)]


def select_candidates(candidates, anchor, maximum, requested):
    available = {item["eventId"]: item for item in candidates if item["usage"] != "Discard"}
    if requested:
        if len(requested) > maximum or any(eid not in available for eid in requested):
            raise ValueError("Requested history EIDs must be non-Discard candidates within the sample limit")
        return [available[eid] for eid in requested]
    if not available:
        return []
    events = sorted(available)
    before = [eid for eid in events if eid <= anchor]
    nearest = events.index(before[-1]) if before else 0
    left = max(0, min(nearest - 1, len(events) - 3))
    nearby = events[left:left + 3]
    chosen = []
    # Three neighbouring revisions around the anchor, then one distant endpoint.
    proposals = nearby + [events[-1], events[0], events[len(events) // 2]]
    if maximum < 4:
        proposals = [events[nearest], events[-1]] + proposals
    for eid in proposals:
        if len(chosen) >= maximum:
            break
        if eid not in chosen:
            chosen.append(eid)
    return [available[eid] for eid in chosen]


def preview_size(width, height, mip, count):
    aspect = float(max(1, width >> min(mip, 31))) / max(1, height >> min(mip, 31))
    w, h = 256, max(1, int(math.floor(256 / aspect + 0.5)))
    if h > 144:
        h, w = 144, max(1, int(math.floor(144 * aspect + 0.5)))
    budget = (48 * 1024 * 1024 / 3.0) / max(1, count)
    if w * h > budget:
        scale = math.sqrt(budget / (w * h))
        w, h = max(16, int(math.floor(w * scale))), max(16, int(math.floor(h * scale)))
    return w, h


def resource_number(resource):
    match = re.fullmatch(r"(?:ResourceId::)?(\d+)", str(resource))
    return int(match.group(1)) if match else None


def texture_from_color_output(textures, targets, slot):
    if slot < 0 or slot >= len(targets) or resource_number(targets[slot].resource) in (None, 0):
        raise RuntimeError("Requested live color output is unbound")
    matches = [texture for texture in textures if texture.resourceId == targets[slot].resource]
    if len(matches) != 1:
        raise RuntimeError("Live color output did not identify exactly one capture texture")
    return matches[0]


def position(path):
    return path.stat().st_size if path.is_file() else 0


def timing_slice(path, begin, end):
    if end < begin:
        raise RuntimeError("Diagnostic log was truncated during measurement")
    if end == begin:
        return [], []
    lines, tail = [], []
    with path.open("rb") as stream:
        stream.seek(begin)
        while stream.tell() < end:
            line = stream.readline(end - stream.tell()).decode("utf-8", "replace").rstrip()
            tail = (tail + [line])[-20:]
            if re.search(r"\bapi=\S+", line):
                lines.append(line)
    return lines, tail


class Recorder:
    def __init__(self, report, path, log_path, seconds):
        self.report, self.path, self.log_path = report, path, log_path
        self.started, self.seconds = time.perf_counter(), seconds

    def publish(self):
        self.path.write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def measure(self, phase, callback, event=None, cleanup=False):
        if not cleanup and time.perf_counter() - self.started > self.seconds:
            raise RuntimeError("Soft total budget reached between native operations")
        sample = {"phase": phase, "eventId": event, "status": "running",
                  "logStart": position(self.log_path),
                  "startedUTC": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                  "elapsedBeforeMs": (time.perf_counter() - self.started) * 1000.0}
        self.report["samples"].append(sample)
        self.publish()
        started = time.perf_counter()
        try:
            value = callback()
            sample["status"] = "complete"
            return value, sample
        except Exception as error:
            sample.update(status="failed", error=str(error))
            raise
        finally:
            sample["wallMs"] = (time.perf_counter() - started) * 1000.0
            sample["logEnd"] = position(self.log_path)
            lines, tail = timing_slice(self.log_path, sample["logStart"], sample["logEnd"])
            records = parse_records("\n".join(lines))
            backend = [r for r in records if r.get("api") in ("D3D11", "D3D12", "OpenGL", "Vulkan")
                       and isinstance(r.get("total_ms"), (int, float))]
            sample["timingRecords"] = records
            sample["backendReplayCalls"] = len(backend)
            sample["backendReplayTotalMs"] = sum(r["total_ms"] for r in backend)
            sample["nonReplayResidualMs"] = max(0.0, sample["wallMs"] - sample["backendReplayTotalMs"])
            sample["backendSummary"] = summarize(backend)
            sample["diagnosticTail"] = tail
            slice_path = self.path.with_name(self.path.stem + ".%02d.timing.log" % len(self.report["samples"]))
            slice_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            sample["timingSlice"] = str(slice_path)
            self.publish()


def byte_record(data, expected=None):
    data = bytes(data)
    if not data or (expected is not None and len(data) != expected):
        raise RuntimeError("Unexpected readback byte count: %d, expected %s" % (len(data), expected))
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def integer_list(text):
    values = [int(value.strip()) for value in text.split(",") if value.strip()]
    if any(value <= 0 for value in values) or len(values) != len(set(values)):
        raise ValueError("EIDs must be distinct positive integers")
    return values


def validate_thumbnail(controller, texture_output, source, event, anchor, expected, recorder,
                       result, expected_bytes):
    """Compare one historical preview with two independent canonical render/readbacks."""
    result.update(eventId=event, status="running", historicalRGB=expected, canonicalRGB=[])
    try:
        for repeat in range(2):
            recorder.measure("validation.canonicalReplay", lambda: controller.SetFrameEvent(event, True), event)
            texture_output.SetTextureDisplay(source)
            recorder.measure("validation.canonicalDisplay", texture_output.Display, event)
            data, _ = recorder.measure("validation.canonicalRGBReadback", texture_output.ReadbackOutputTexture, event)
            result["canonicalRGB"].append(byte_record(data, expected_bytes))
            del data
        first, second = result["canonicalRGB"]
        result["canonicalRepeatIdentical"] = first == second
        result["historyMatchesCanonical"] = expected == first and expected == second
        if first != second:
            result["status"] = "inconclusiveCanonicalUnstable"
        elif result["historyMatchesCanonical"]:
            result["status"] = "passed"
        else:
            result["status"] = "failedStableCanonicalMismatch"
    finally:
        # A budget check or comparison error must not leave the device at the control event.
        recorder.measure("validation.restoreAnchor", lambda: controller.SetFrameEvent(anchor, True),
                         anchor, cleanup=True)
        result["restoredControllerEvent"] = int(controller.GetCurrentEventID())
        if result["restoredControllerEvent"] != anchor:
            raise RuntimeError("Canonical thumbnail control did not restore the anchor")


def main():
    prefix = "RENDERDOC_EID_HISTORY_"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", default=os.environ.get(prefix + "CAPTURE"))
    parser.add_argument("--resource", type=int, default=os.environ.get(prefix + "RESOURCE"))
    parser.add_argument("--resource-output-event", type=int,
                        default=os.environ.get(prefix + "RESOURCE_OUTPUT_EVENT", "0"))
    parser.add_argument("--resource-output-slot", type=int,
                        default=os.environ.get(prefix + "RESOURCE_OUTPUT_SLOT", "0"))
    parser.add_argument("--anchor", type=int, default=os.environ.get(prefix + "ANCHOR"))
    parser.add_argument("--events", default=os.environ.get(prefix + "EVENTS", ""))
    parser.add_argument("--history-events", default=os.environ.get(prefix + "EVENTS_HISTORY", ""))
    parser.add_argument("--output", default=os.environ.get(prefix + "OUTPUT"))
    parser.add_argument("--build-label", default=os.environ.get(prefix + "BUILD_LABEL", "unspecified"))
    parser.add_argument("--repeats", type=int, default=os.environ.get(prefix + "REPEATS", "1"))
    parser.add_argument("--max-history-events", type=int, default=os.environ.get(prefix + "MAX_EVENTS", "4"))
    parser.add_argument("--history-path", choices=("transient", "snapshot", "both"),
                        default=os.environ.get(prefix + "PATH", "transient"))
    parser.add_argument("--mip", type=int, default=0)
    parser.add_argument("--slice", type=int, default=0)
    parser.add_argument("--sample", type=int, default=0)
    parser.add_argument("--max-snapshot-mib", type=float, default=256)
    parser.add_argument("--max-total-seconds", type=float, default=os.environ.get(prefix + "MAX_SECONDS", "300"))
    parser.add_argument("--skip-thumbnail-validation", action="store_true",
                        default=os.environ.get(prefix + "VALIDATE", "1") == "0")
    args = parser.parse_args()
    if not args.capture or not (args.resource or args.resource_output_event) or not args.anchor:
        parser.error("--capture, --anchor and either --resource or --resource-output-event are required")
    if args.resource and args.resource_output_event:
        parser.error("Select either an exact resource ID or a live color-output binding, not both")
    if args.resource_output_event < 0 or args.resource_output_slot < 0:
        parser.error("Color-output event and slot must be non-negative")
    if not 1 <= args.repeats <= 5 or not 1 <= args.max_history_events <= 6:
        parser.error("Use 1..5 repeats and 1..6 representative History events")
    if min(args.mip, args.slice, args.sample) < 0 or args.max_snapshot_mib <= 0 or args.max_total_seconds <= 0:
        parser.error("Subresource values must be non-negative and budgets must be positive")
    events = integer_list(args.events) or [args.anchor]
    if len(events) > 6:
        parser.error("At most six explicit replay EIDs may be sampled")
    capture_path = Path(args.capture).resolve()
    if not capture_path.is_file():
        parser.error("Capture file is missing: " + str(capture_path))
    default_dir = Path(__file__).resolve().parents[2] / "output/eid-history-20260905"
    output = Path(args.output).resolve() if args.output else default_dir / ("profile-" + time.strftime("%Y%m%d-%H%M%S") + ".json")
    if output.exists():
        parser.error("Choose a new output path; previous evidence is never overwritten")
    output.parent.mkdir(parents=True, exist_ok=True)
    log_path = output.with_suffix(".rdoc.log")
    report = {"schema": "renderdoc-eid-history-profile/v2", "ok": False, "samples": [],
              "processId": os.getpid(),
              "capture": str(capture_path), "captureBytes": capture_path.stat().st_size,
              "requestedResource": args.resource, "anchor": args.anchor, "events": events,
              "buildLabel": args.build_label, "python": sys.version, "diagnosticLog": str(log_path),
              "measurement": "CPU wall time including explicit GPU waits; headless application display, not scanout",
              "residualMeaning": "Non-replay residual includes allocation, copy recording, rendering, logging and bookkeeping; not a GPU timestamp",
              "softBudgetSeconds": args.max_total_seconds, "history": [],
              "nativeCallCancellation": "No safe Python cancellation; supervise capture.openReplay externally at 120 seconds",
              "historyPath": args.history_path, "measurementComplete": False,
              "thumbnailValidation": {"status": "pending" if not args.skip_thumbnail_validation else "notRequested"}}
    recorder = Recorder(report, output, log_path, args.max_total_seconds)
    recorder.publish()
    # Set before loading native modules or their first timing query.
    os.environ["RENDERDOC_EID_TIMING"] = "1"
    import renderdoc as rd
    capture_file, controller, texture_output = None, None, None
    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        rd.SetDebugLogFile(str(log_path))
        capture_file = rd.OpenCaptureFile()
        status, _ = recorder.measure("capture.openFile", lambda: capture_file.OpenFile(str(capture_path), "", None))
        if status != rd.ResultCode.Succeeded:
            raise RuntimeError("OpenFile failed: " + str(status))
        report["captureDriver"] = capture_file.DriverName()
        opened, _ = recorder.measure("capture.openReplay", lambda: capture_file.OpenCapture(rd.ReplayOptions(), None))
        status, controller = opened
        if status != rd.ResultCode.Succeeded or controller is None:
            raise RuntimeError("OpenCapture failed: " + str(status))
        report["replayAPI"] = str(controller.GetAPIProperties().pipelineType)
        if args.resource_output_event:
            recorder.measure("metadata.resolveColorOutputReplay", lambda: controller.SetFrameEvent(
                args.resource_output_event, True), args.resource_output_event)
        textures, _ = recorder.measure("metadata.textures", controller.GetTextures)
        if args.resource_output_event:
            targets = controller.GetPipelineState().GetOutputTargets()
            texture = texture_from_color_output(textures, targets, args.resource_output_slot)
            report["resourceSelection"] = {"kind": "liveColorOutput", "eventId": args.resource_output_event,
                                           "slot": args.resource_output_slot, "resource": str(texture.resourceId),
                                           "warmup": "Binding resolution requires one separately timed canonical replay"}
        else:
            texture = next((item for item in textures if resource_number(item.resourceId) == args.resource), None)
            report["resourceSelection"] = {"kind": "exactResourceId", "resource": args.resource}
        if texture is None:
            raise RuntimeError("Requested ResourceId is not a texture in this capture")
        if args.mip >= texture.mips or args.slice >= max(texture.arraysize, texture.depth):
            raise RuntimeError("Requested subresource lies outside the texture")
        source = rd.TextureDisplay()
        source.resourceId = texture.resourceId
        source.subresource = rd.Subresource(args.mip, args.slice, args.sample)
        source.typeCast = texture.format.compType
        source.scale = -1.0
        source.rangeMin = -1.0 if source.typeCast == rd.CompType.SNorm else 0.0
        source.rangeMax = 1.0
        if texture.creationFlags & rd.TextureCategory.DepthTarget or source.typeCast == rd.CompType.Depth:
            source.green = source.blue = False
        report["texture"] = {"resource": str(texture.resourceId), "width": int(texture.width),
                             "height": int(texture.height), "depth": int(texture.depth),
                             "arraysize": int(texture.arraysize), "mips": int(texture.mips),
                             "samples": int(texture.msSamp), "format": texture.format.Name(),
                             "byteSize": int(texture.byteSize), "mip": args.mip,
                             "slice": args.slice, "sample": args.sample}
        usage, _ = recorder.measure("history.getUsage", lambda: controller.GetUsage(texture.resourceId))
        candidates, _ = recorder.measure("history.filterCandidates", lambda: build_candidates(
            [(int(item.eventId), str(item.usage)) for item in usage], args.mip))
        report["usageCount"], report["candidateCount"] = len(usage), len(candidates)
        candidate_path = output.with_name(output.stem + ".candidates.json")
        candidate_path.write_text(json.dumps(candidates, indent=2) + "\n", encoding="utf-8")
        report["candidateFile"] = str(candidate_path)
        selected = select_candidates(candidates, args.anchor, args.max_history_events, integer_list(args.history_events))
        report["selectedHistoryEvents"] = selected
        width, height = preview_size(texture.width, texture.height, args.mip, len(candidates))
        report["thumbnail"] = {"width": width, "height": height, "expectedRGBBytes": width * height * 3}

        def fatal_check():
            fatal = controller.GetFatalErrorStatus()
            if fatal.code != rd.ResultCode.Succeeded:
                raise RuntimeError("Fatal replay error: " + str(fatal))

        for repeat in range(args.repeats):
            for eid in events:
                controller.SetSelectedEventID(eid)
                _, sample = recorder.measure("replay.scrub", lambda: controller.ScrubFrameEvent(eid), eid)
                sample["repeat"] = repeat
                sample["realScrubLogged"] = any(row.get("api") == "ScrubReplay" for row in sample["timingRecords"])
                sample["snapshotShortcutLogged"] = any("SnapshotHit" in str(row.get("api", "")) for row in sample["timingRecords"])
                _, sample = recorder.measure("replay.publishPipeline", lambda: controller.CommitScrubFrameEvent(eid), eid)
                sample["repeat"] = repeat
                sample["actualEventId"] = int(controller.GetCurrentEventID())
                fatal_check()
        # The history API restores this event. Keep its cost independent of which event was sampled last.
        _, _ = recorder.measure("history.establishAnchor", lambda: controller.SetFrameEvent(args.anchor, True), args.anchor)
        anchor = int(controller.GetCurrentEventID())
        report["effectiveAnchor"] = anchor
        texture_output, _ = recorder.measure("history.createHeadlessOutput", lambda: controller.CreateOutput(
            rd.CreateHeadlessWindowingData(width, height), rd.ReplayOutputType.Texture))
        if texture_output is None:
            raise RuntimeError("Headless texture output creation failed")
        snapshot_estimate = int(texture.byteSize) * len(selected)
        snapshot_allowed = snapshot_estimate <= args.max_snapshot_mib * 1024 * 1024
        report["snapshotBudget"] = {"limitMiB": args.max_snapshot_mib,
                                    "conservativeTotalSourceBytes": snapshot_estimate,
                                    "allowed": snapshot_allowed}
        for candidate in selected:
            eid = candidate["eventId"]
            history = dict(candidate)
            report["history"].append(history)
            if args.history_path in ("transient", "both"):
                _, _ = recorder.measure("history.headlessDisplayAtEvent", lambda: texture_output.DisplayTextureAtEvent(eid, source), eid)
                data, _ = recorder.measure("history.thumbnailRGBReadback", texture_output.ReadbackOutputTexture, eid)
                history["transientRGB"] = byte_record(data, width * height * 3)
                del data
            if args.history_path in ("snapshot", "both") and snapshot_allowed:
                snapshot = rd.TextureDisplay()
                resolved, _ = recorder.measure("history.resolvePersistentSnapshot", lambda: controller.ResolveEventTextureSnapshot(eid, source, snapshot), eid)
                history["snapshotResolved"] = bool(resolved)
                history["snapshotBackendKind"] = "native-or-portable; public API does not expose the branch"
                if resolved:
                    history["snapshotResource"] = str(snapshot.resourceId)
                    texture_output.SetTextureDisplay(snapshot)
                    _, _ = recorder.measure("history.snapshotDisplay", texture_output.Display, eid)
                    data, _ = recorder.measure("history.snapshotRGBReadback", texture_output.ReadbackOutputTexture, eid)
                    history["snapshotRGB"] = byte_record(data, width * height * 3)
                    del data
                    cached = rd.TextureDisplay()
                    hit, hit_sample = recorder.measure("history.resolveSameSnapshotAgain", lambda: controller.ResolveEventTextureSnapshot(eid, source, cached), eid)
                    history["sameSnapshotNoReplay"] = bool(hit) and cached.resourceId == snapshot.resourceId and hit_sample["backendReplayCalls"] == 0
                    if "transientRGB" in history:
                        history["twoPathsRGBIdentical"] = history["transientRGB"] == history["snapshotRGB"]
            elif args.history_path in ("snapshot", "both"):
                history["snapshotSkipped"] = "Conservative source-byte estimate exceeds the bounded snapshot budget"
            history["controllerEventAfterHistory"] = int(controller.GetCurrentEventID())
            if history["controllerEventAfterHistory"] != anchor:
                raise RuntimeError("History operation changed the controller's current event")
            fatal_check()
            recorder.publish()
        if not args.skip_thumbnail_validation:
            reference = next((item for item in report["history"]
                              if "transientRGB" in item or "snapshotRGB" in item), None)
            if reference is None:
                raise RuntimeError("No historical thumbnail exists for the canonical correctness control")
            expected = reference.get("transientRGB", reference.get("snapshotRGB"))
            validate_thumbnail(controller, texture_output, source, reference["eventId"], anchor,
                               expected, recorder, report["thumbnailValidation"], width * height * 3)
            fatal_check()
        report["measurementComplete"] = True
        report["ok"] = report["thumbnailValidation"]["status"] in ("passed", "notRequested")
    except Exception as error:
        report.update(error=str(error), traceback=traceback.format_exc())
    finally:
        if texture_output is not None:
            recorder.measure("cleanup.output", texture_output.Shutdown, cleanup=True)
        if controller is not None:
            recorder.measure("cleanup.replay", controller.Shutdown, cleanup=True)
        if capture_file is not None:
            capture_file.Shutdown()
        rd.ShutdownReplay()
        recorder.publish()
    print(json.dumps({"ok": report["ok"], "output": str(output), "candidateCount": report.get("candidateCount"),
                      "historySamples": len(report["history"]), "error": report.get("error"),
                      "thumbnailValidation": report["thumbnailValidation"]["status"]}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
