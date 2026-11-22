"""Bounded D3D11/OpenGL/OpenGLES/Vulkan live-forward acceptance on four actual draws.

Main opens the explicitly named capture and uses the GPU; coordinate replay ownership first.
Pure helpers and tests do not import renderdoc. Run explicit, backend-default and disabled modes
in separate processes. Backend-default acceptance removes inherited enable overrides first.
All output textures observed at the four draws remain in the fixed readback set, even when
unbound later. Viewport-centre probes must prove both middle draws changed persistent pixels.
"""
from __future__ import print_function

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import traceback

from eid_checkpoint_profile import parse_status, selected_event_evidence, status_issues
from eid_history_profile import Recorder, integer_list, resource_number
from eid_live_forward_profile import comparisons, texture_requests
from eid_shared_buffer_profile import (binary_identity, build_identity, output_witness,
                                       unchanged_checkpoint_issues)


BACKENDS = {"D3D11": "D3D11LiveForward", "OpenGL": "GLLiveForward",
            "OpenGLES": "GLLiveForward", "Vulkan": "VulkanLiveForward"}
RANGES = ("AB", "BD", "AC", "CD")
LIVE_FORWARD_ENV = ("RENDERDOC_D3D11_ENABLE_LIVE_FORWARD", "RENDERDOC_GL_ENABLE_LIVE_FORWARD",
                    "RENDERDOC_VULKAN_ENABLE_LIVE_FORWARD")


def profile_environment(disabled, backend_default=False):
    if disabled and backend_default:
        raise ValueError("Backend-default and disabled live-forward modes are mutually exclusive")
    enabled = None if backend_default else ("0" if disabled else "1")
    return dict({"RENDERDOC_EID_TIMING": "1", "RENDERDOC_REPLAY_CHECKPOINTS": "0"},
                **{key: enabled for key in LIVE_FORWARD_ENV})


def configure_profile_environment(disabled, backend_default=False, environ=None):
    """Apply before loading native code; null in the evidence means the variable is absent."""
    if environ is None:
        environ = os.environ
    requested = profile_environment(disabled, backend_default)
    for key, value in requested.items():
        if value is None:
            environ.pop(key, None)
        else:
            environ[key] = value
    # Read back actual process values/presence rather than merely reporting requested settings.
    return {"values": {key: environ.get(key) for key in requested},
            "present": {key: key in environ for key in requested}}


def requested_ranges(text):
    values = text.split(",") if text else []
    if len(values) != len(set(values)) or any(value not in RANGES for value in values):
        raise ValueError("Expected hit ranges must be distinct members of AB,BD,AC,CD")
    return values


def select_events(draws, requested=""):
    if not draws or draws != sorted(set(draws)) or any(type(e) is not int or e <= 0 for e in draws):
        raise ValueError("Native draw EIDs must be distinct, positive and ascending")
    events = integer_list(requested) if requested else list(draws)
    if len(events) != 4 or events != sorted(events):
        raise ValueError("Select exactly four ascending actual draw EIDs; use --events for larger captures")
    if [event for event in draws if events[0] <= event <= events[-1]] != events:
        raise ValueError("Selected EIDs must be four consecutive actual draws")
    return events


def sequence(events):
    a, b, c, d = events
    return [("seedA", a, None), ("AB", b, "AB"), ("BD", d, "BD"),
            ("backwardDA", a, None), ("AC", c, "AC"), ("CD", d, "CD")]


def live_evidence(records, api, previous, target, disabled=False, force=False,
                  require_hit=False, allow_hit=True):
    """Missing evidence is never a hit; each backend must prove its exact native range."""
    expected = BACKENDS[api]
    all_live = [row for row in records if row.get("api") in set(BACKENDS.values())]
    rows = [row for row in all_live if row.get("api") == expected]
    issues = []
    if len(rows) > 1 or len(all_live) != len(rows):
        issues.append("Unexpected or duplicate native live-forward root evidence")
    # Vulkan logs ordinary roots only. Forced references are instead required to show a real
    # canonical backend call by the caller; D3D11/GL also log force roots with hit=0.
    if not rows and not (force and api == "Vulkan"):
        issues.append("Missing native %s request evidence" % expected)
    valid_hits = 0
    for row in rows:
        row_issues = []
        for field in ("target", "enabled", "hit", "initialRestoreSkipped"):
            if type(row.get(field)) is not int:
                row_issues.append("Invalid native integer field: " + field)
        if row.get("target") != target or row.get("enabled") != int(not disabled):
            row_issues.append("Native target or enable state differs from the requested operation")
        if row.get("hit") not in (0, 1):
            row_issues.append("Native hit flag must be zero or one")
        success = row.get("success" if api == "D3D11" else "succeeded")
        if type(success) is not int or success != 1:
            row_issues.append("Native replay did not report successful completion")
        hit = row.get("hit") == 1
        if hit:
            if disabled or force or not allow_hit or target <= previous:
                row_issues.append("Disabled/forced/backward/control operation used live forward")
            if row.get("from") != previous or type(row.get("from")) is not int:
                row_issues.append("Native hit used a different materialized source EID")
            if row.get("initialRestoreSkipped") != 1:
                row_issues.append("Native hit did not prove initialization was skipped")
            if api in ("OpenGL", "OpenGLES"):
                if (row.get("replayedFirst") != previous + 1 or
                        row.get("replayedLast") != target - 1):
                    row_issues.append("Native GL hit did not replay the exact intervening API range")
            else:
                if row.get("replayedEvents") != target - previous - 1:
                    row_issues.append("Native hit did not replay every intervening API event")
                if api == "D3D11" and row.get("range") != "%d-%d" % (previous + 1, target - 1):
                    row_issues.append("Native D3D11 interval boundaries do not match")
        elif row.get("initialRestoreSkipped") != 0:
            row_issues.append("Native fallback claimed skipped initialization")
        elif api not in ("OpenGL", "OpenGLES") and row.get("replayedEvents") != 0:
            row_issues.append("Native fallback claimed incremental events")
        if hit and not row_issues:
            valid_hits += 1
        issues.extend(row_issues)
    if require_hit and not valid_hits:
        issues.append("Required native positive hit for this exact range was absent")
    return {"records": rows, "matchingHits": valid_hits, "issues": issues}


def viewport_probe(event, api, viewport, texture, binding):
    values = [float(getattr(viewport, key)) for key in ("x", "y", "width", "height")]
    if any(not math.isfinite(value) for value in values) or not values[2] or not values[3]:
        raise ValueError("A finite nonempty viewport is required for the regional pixel witness")
    if int(binding.numMips) != 1 or int(binding.numSlices) != 1 or int(texture.depth) != 1:
        raise ValueError("Regional fixture probes require a single 2D RT0 view")
    mip, layer = int(binding.firstMip), int(binding.firstSlice)
    if mip < 0 or mip >= int(texture.mips) or layer < 0 or layer >= int(texture.arraysize):
        raise ValueError("Regional fixture probe view is outside the native texture")
    width, height = max(1, int(texture.width) >> mip), max(1, int(texture.height) >> mip)
    x, y = math.floor(values[0] + values[2] / 2), math.floor(values[1] + values[3] / 2)
    if api in ("OpenGL", "OpenGLES"):
        y = height - y - 1
    if not 0 <= x < width or not 0 <= y < height:
        raise ValueError("Viewport centre is outside RT0; select a compatible four-region fixture")
    return {"eventId": event, "resource": str(binding.resource),
            "subresource": [mip, layer, 0], "x": x, "y": y,
            "viewport": dict(zip(("x", "y", "width", "height"), values))}


def intermediate_coverage(references, events):
    """Each middle region must change at its own draw, and that exact value must survive at D."""
    if any(len(references.get(event, [])) != 2 or references[event][0] != references[event][1]
           for event in events):
        return [], ["Stable independent references are required to prove intermediate draw contribution"]
    def regions(event):
        return {row["eventId"]: row for row in references[event][0]["regions"]}
    final, rows, issues = regions(events[-1]), [], []
    for index in (1, 2):
        event, preceding = events[index], events[index - 1]
        before, after, at_end = regions(preceding).get(event), regions(event).get(event), final.get(event)
        coordinate_keys = ("resource", "subresource", "x", "y")
        complete = all(value is not None for value in (before, after, at_end))
        same_pixel = complete and all(before[key] == after[key] == at_end[key] for key in coordinate_keys)
        changed = bool(same_pixel and before["uintBits"] != after["uintBits"])
        retained = bool(changed and after["uintBits"] == at_end["uintBits"])
        row = {"eventId": event, "precedingEventId": preceding, "changedAtOwnDraw": changed,
               "persistedThroughD": retained, "before": before, "after": after, "atD": at_end}
        rows.append(row)
        if not retained:
            issues.append("EID%d has no changed persistent viewport-centre pixel; omitted middle draw "
                          "cannot be ruled out by this fixture" % event)
    return rows, issues


def complete_witness(controller, rd, requests, resources, probes, maximum):
    witness = output_witness(controller, rd, maximum)
    allowed = {str(resource) for resource in resources.values()}
    if any(row["resource"] not in allowed for row in witness["pixels"]):
        raise RuntimeError("A bound output is outside the fixed texture coverage")
    pixels = {(row["resource"], tuple(row["subresource"])): row for row in witness["pixels"]}
    total = sum(row["bytes"] for row in pixels.values())
    ordered = []
    for resource, sub in requests:
        key = (str(resource), sub)
        if key not in pixels:
            raw = bytes(controller.GetTextureData(resource, rd.Subresource(*sub)))
            total += len(raw)
            if not raw or total > maximum:
                raise RuntimeError("Empty or over-budget fixed output texture readback")
            pixels[key] = {"resource": str(resource), "subresource": list(sub), "bytes": len(raw),
                           "sha256": hashlib.sha256(raw).hexdigest()}
        ordered.append(pixels[key])
    witness["pixels"] = ordered
    witness["regions"] = []
    for probe in probes:
        value = controller.PickPixel(resources[probe["resource"]], probe["x"], probe["y"],
                                     rd.Subresource(*probe["subresource"]), rd.CompType.Typeless)
        # PixelValue is a union; uintValue preserves exact returned float/integer bits, including
        # NaNs, without JSON rounding or non-standard NaN tokens. Never compare a display PNG.
        witness["regions"].append(dict(probe, uintBits=[int(word) for word in value.uintValue]))
    if controller.GetFatalErrorStatus().code != rd.ResultCode.Succeeded:
        raise RuntimeError("Native output probe reported a fatal replay error")
    return witness


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--events", default="", help="Optional four consecutive actual draw EIDs")
    parser.add_argument("--resources", default="", help="Optional exact fixed texture IDs; must cover all discovered outputs")
    parser.add_argument("--expect-hit-ranges", default="", help="Optional exact required ranges: AB,BD,AC,CD")
    parser.add_argument("--min-hits", type=int, default=1)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--disable-live-forward", action="store_true")
    mode.add_argument("--default-live-forward", action="store_true",
                      help="Unset all backend enable overrides; requires --expect-hit-ranges")
    parser.add_argument("--max-read-mib", type=int, default=256)
    parser.add_argument("--max-seconds", type=int, default=240)
    parser.add_argument("--build-label", default="unspecified")
    args = parser.parse_args(argv)
    try:
        required = requested_ranges(args.expect_hit_ranges)
        numbers = integer_list(args.resources) if args.resources else []
        if numbers and not 1 <= len(numbers) <= 16:
            raise ValueError("Fixed resource set must contain one to sixteen textures")
        if (not 1 <= args.min_hits <= 4 or not 1 <= args.max_read_mib <= 256 or
                not 1 <= args.max_seconds <= 600):
            raise ValueError("min-hits must be 1..4, max-read-mib 1..256, max-seconds 1..600")
        if args.disable_live_forward and required:
            raise ValueError("Disabled control must not request positive hit ranges")
        if args.default_live_forward and not required:
            raise ValueError("Backend-default acceptance requires explicit --expect-hit-ranges")
        capture, output = Path(args.capture).resolve(), Path(args.output).resolve()
        if not capture.is_file() or any(path.exists() for path in
                (output, output.with_suffix(".rdoc.log"), output.with_suffix(".native.log"))):
            raise ValueError("Capture must exist and output evidence stem must be unused")
    except (ValueError, OSError) as error:
        parser.error(str(error))
    output.parent.mkdir(parents=True, exist_ok=True)
    environment_evidence = configure_profile_environment(args.disable_live_forward,
                                                        args.default_live_forward)
    environment = environment_evidence["values"]
    report = {"schema": "renderdoc-portable-forward-profile/v1", "ok": False,
              "measurementComplete": False, "capture": str(capture), "configuration": vars(args),
              "environment": environment, "environmentPresence": environment_evidence["present"],
              "processId": os.getpid(), "samples": [], "visits": [],
              "forceReferences": [], "issues": [], "totalReadbackBytes": 0,
              "limits": ["Each run validates one actual capture/API; successful loading is not platform acceptance",
                         "All bound color/depth views and every subresource of the fixed output set are hashed",
                         "Middle draw contribution requires changed, persistent viewport-centre pixels",
                         "No UAV/buffer mutation acceptance is implied by output texture parity",
                         "Readbacks synchronize between visits; setter time is not end-to-end drag latency",
                         "Enabled acceptance always requires positive native hits; a fallback-only run cannot pass"]}
    recorder = Recorder(report, output, output.with_suffix(".rdoc.log"), args.max_seconds)
    recorder.publish()
    rd, controller, cap, initialized = None, None, None, False
    maximum = args.max_read_mib * 1024 * 1024
    api, requests, resources, probes = None, [], {}, []

    def status():
        value = parse_status(controller.GetReplayCheckpointStatus())
        report["issues"].extend(status_issues(value, disabled=True))
        return value

    def visit(label, event, force=False, require_hit=False, allow_hit=True, reference=False):
        previous, before = int(controller.GetCurrentEventID()), status()
        _, timing = recorder.measure(label + ".replay", lambda: controller.SetFrameEvent(event, force), event)
        after = status()
        evidence = live_evidence(timing["timingRecords"], api, previous, event,
                                 args.disable_live_forward, force, require_hit, allow_hit)
        report["issues"].extend(label + ": " + value for value in evidence["issues"])
        if force and not any(row.get("api") == ("OpenGL" if api == "OpenGLES" else api)
                             and row.get("type") == "WithoutDraw" for row in timing["timingRecords"]):
            report["issues"].append(label + ": forced reference lacks an independent native prefix replay")
        if int(controller.GetCurrentEventID()) != event:
            raise RuntimeError("Native setter did not publish the requested actual EID")
        witness, readback = recorder.measure(label + ".readback",
            lambda: complete_witness(controller, rd, requests, resources, probes, maximum), event)
        if witness["eventId"] != event or int(controller.GetCurrentEventID()) != event:
            raise RuntimeError("Readonly witness changed the selected EID")
        after_read = status()
        report["issues"].extend(label + ": " + issue for issue in
                                 unchanged_checkpoint_issues(before, after))
        report["issues"].extend(label + ": " + issue for issue in
                                 unchanged_checkpoint_issues(after, after_read))
        report["totalReadbackBytes"] += sum(row["bytes"] for row in witness["pixels"])
        row = {"label": label, "eventId": event, "previousEventId": previous, "force": force,
               "requireHit": require_hit, "liveForward": evidence, "witness": witness,
               "replayWallMs": timing["wallMs"], "readbackWallMs": readback["wallMs"],
               "checkpointBefore": before, "checkpointAfter": after, "checkpointAfterReadback": after_read}
        (report["forceReferences"] if reference else report["visits"]).append(row)
        recorder.publish()
        return witness

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
        api = str(cap.DriverName())
        if api not in BACKENDS:
            raise RuntimeError("Unsupported capture driver for this probe: " + api)
        report["captureDriver"] = api
        opened, _ = recorder.measure("capture.openReplay", lambda: cap.OpenCapture(rd.ReplayOptions(), None))
        result, controller = opened
        if result != rd.ResultCode.Succeeded or controller is None:
            raise RuntimeError("OpenCapture failed: " + str(result))
        properties = controller.GetAPIProperties()
        report["apiProperties"] = {key: str(getattr(properties, key)) for key in
                                   ("pipelineType", "localRenderer", "vendor", "degraded", "remoteReplay")}
        controller.ConfigureReplayCheckpoints(False, 0, 0)
        report["initialCheckpointStatus"] = status()
        actions = []
        def collect(nodes):
            for action in nodes:
                if action.flags & rd.ActionFlags.Drawcall:
                    actions.append(action)
                collect(action.children)
        collect(controller.GetRootActions())
        events = select_events([int(action.eventId) for action in actions], args.events)
        report["events"] = events
        if any(action.flags & rd.ActionFlags.Indirect for action in actions if int(action.eventId) in events):
            raise RuntimeError("The selected fixture draws must be direct native actions")
        report["selectedEventEvidence"] = selected_event_evidence(controller, events)
        if any(row["status"] != "exactAPIEvent" for row in report["selectedEventEvidence"]):
            raise RuntimeError("Every selected draw needs exact native API-event evidence")
        textures = controller.GetTextures()
        by_id = {str(texture.resourceId): texture for texture in textures}
        discovered = set()
        report["discovery"] = []
        # These forced metadata visits precede the measured sequence. They never count as a hit
        # or a canonical reference, and their pixels are not retained as replay inputs.
        for event in events:
            recorder.measure("discovery.EID%d" % event, lambda e=event: controller.SetFrameEvent(e, True), event)
            if int(controller.GetCurrentEventID()) != event:
                raise RuntimeError("Discovery did not select the exact native draw")
            pipe = controller.GetPipelineState()
            outputs = list(pipe.GetOutputTargets())
            bindings = outputs + [pipe.GetDepthTarget()]
            bound = [binding for binding in bindings if binding.resource != rd.ResourceId.Null()]
            discovered.update(resource_number(binding.resource) for binding in bound)
            if not outputs or outputs[0].resource == rd.ResourceId.Null():
                raise RuntimeError("Four-region fixture requires a bound RT0 at every selected draw")
            texture = by_id.get(str(outputs[0].resource))
            if texture is None:
                raise RuntimeError("RT0 texture is absent from native metadata")
            probe = viewport_probe(event, api, pipe.GetViewport(0), texture, outputs[0])
            probes.append(probe)
            report["discovery"].append({"eventId": event, "boundResources": [str(b.resource) for b in bound],
                                         "probe": probe})
        if None in discovered or not 1 <= len(discovered) <= 16:
            raise RuntimeError("Discovered output identities must contain one to sixteen native textures")
        if numbers and not discovered.issubset(numbers):
            raise RuntimeError("Explicit resource list omits a discovered bound output")
        numbers = numbers or sorted(discovered)
        if len({(p["resource"], tuple(p["subresource"]), p["x"], p["y"]) for p in probes}) != 4:
            raise RuntimeError("The four draws must expose four distinct regional pixel witnesses")
        report["textures"], requests, report["estimatedReadbackBytesPerWitness"] = texture_requests(
            textures, numbers, maximum)
        resources = {str(resource): resource for resource, _ in requests}
        report["fixedResources"], report["regionProbes"] = numbers, probes
        for label, event, interval in sequence(events):
            visit(label, event, require_hit=interval in required, allow_hit=interval is not None)
        references = {}
        for event in events:
            references[event] = [visit("reference%d.EID%d" % (repeat + 1, event), event,
                                        force=True, allow_hit=False, reference=True) for repeat in range(2)]
        report["comparisons"], issues = comparisons(report["visits"], references, events)
        report["issues"].extend(issues)
        report["intermediateCoverage"], issues = intermediate_coverage(references, events)
        report["issues"].extend(issues)
        hits = sum(row["liveForward"]["matchingHits"] for row in report["visits"])
        report["matchingForwardHits"] = hits
        if args.disable_live_forward:
            if hits:
                report["issues"].append("Disabled control unexpectedly used live forward")
        elif hits < args.min_hits:
            report["issues"].append("Only %d positive native hits; at least %d required" % (hits, args.min_hits))
        report["measurementComplete"], report["ok"] = True, not report["issues"]
        report["verdict"] = (("passedDisabledControlAndNativeParity" if args.disable_live_forward else
                              "passedNativeForwardAndCompleteOutputParity") if report["ok"] else
                             "failedOrInconclusive")
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
