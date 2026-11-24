"""Read-only checkpoint sizing evidence. Running main opens a capture and uses the GPU.

After OpenCapture this only requests API metadata: no SetFrameEvent, texture/buffer
readback, checkpoint creation, capture mutation, or GUI. Helpers are CPU-testable.
Logical resource bytes and conservative lifetime sets are NOT native VRAM sizes.
"""
from __future__ import print_function

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import sys
import time
import traceback


WRITE_USAGES = {"StreamOut", "ColorTarget", "DepthStencilTarget", "Clear", "Discard",
                "GenMips", "Resolve", "ResolveDst", "Copy", "CopyDst", "CPUWrite"}
READ_USAGES = {"VertexBuffer", "IndexBuffer", "InputTarget", "Indirect", "ResolveSrc", "CopySrc"}
NEUTRAL_USAGES = {"Unused", "Barrier", "Shader"}
STAGE_PATTERNS = [
    ("depth/prepass", r"depth|pre.?pass|z.?prepass"),
    ("GBuffer/basepass", r"g.?buffer|base.?pass"),
    ("lighting", r"light(?:ing|s)?|deferred.?shad"),
    ("translucency", r"translucen|transparent"),
    ("postprocess", r"post.?process|tone.?map|temporal.?aa|upscal"),
]


def usage_kind(name):
    name = name.rsplit(".", 1)[-1]
    if name in WRITE_USAGES or name.endswith("_RWResource"):
        return "potentialWrite"
    if name in READ_USAGES or name.endswith("_Constants") or name.endswith("_Resource"):
        return "read"
    return "neutral" if name in NEUTRAL_USAGES else "unknown"


def usage_summary(pairs):
    """Resource-wide lifetimes; intentionally do not infer a full overwrite from a write."""
    pairs = [(int(eid), str(name).rsplit(".", 1)[-1]) for eid, name in pairs if int(eid) > 0]
    writes = [eid for eid, name in pairs if usage_kind(name) == "potentialWrite"]
    reads = [eid for eid, name in pairs if usage_kind(name) == "read"]
    unknown = sorted({name for _, name in pairs if usage_kind(name) == "unknown"})
    constants = sorted({eid for eid, name in pairs if name.endswith("_Constants")})
    return {"usageCount": len(pairs), "usageKinds": dict(sorted(Counter(n for _, n in pairs).items())),
            "firstUse": min((e for e, _ in pairs), default=None),
            "lastUse": max((e for e, _ in pairs), default=None),
            "firstPotentialWrite": min(writes, default=None),
            "lastPotentialWrite": max(writes, default=None),
            "firstReportedRead": min(reads, default=None), "unknownUsageKinds": unknown,
            "firstConstantConsumption": constants[0] if constants else None,
            "constantConsumptionCount": len(constants),
            "constantConsumptionSample": constants[:32]}


def usage_window(pairs, start, end):
    """Observed uses in (checkpoint, target]; never an authority to skip restoration."""
    if not 0 <= start < end:
        raise ValueError("Usage window requires 0 <= checkpoint < target")
    pairs = sorted((int(eid), str(name).rsplit(".", 1)[-1]) for eid, name in pairs
                   if start < int(eid) <= end)
    first = pairs[0][0] if pairs else None
    first_kinds = sorted({name for eid, name in pairs if eid == first})
    result = usage_summary(pairs)
    result.update(afterEventId=start, throughEventId=end, firstUseKinds=first_kinds,
                  noReportedUse=not pairs, startsWithReportedClear="Clear" in first_kinds,
                  fullOverwriteProven=False, safeToSkipRestore=False,
                  byteRanges=None, subresourceRanges=None,
                  conclusion="resource-wide GetUsage observation only; alias/coverage/resume proof required")
    return result


def parse_windows(value, maximum=16):
    windows = sorted({tuple(int(part) for part in item.strip().split(":"))
                      for item in value.split(",") if item.strip()})
    if len(windows) > maximum or any(len(w) != 2 or not 0 <= w[0] < w[1] for w in windows):
        raise ValueError("At most %d windows checkpoint:target with 0 <= checkpoint < target" % maximum)
    return windows


def category(entry):
    usage = entry.get("usage")
    if usage is None:
        return "usageNotAvailable"
    if usage["firstPotentialWrite"] is not None:
        return "observedPotentialWrite"
    if usage["unknownUsageKinds"]:
        return "unknownUsage"
    if entry["writeCapableCreationFlag"]:
        return "writeCapableWithoutReportedWrite"
    if usage["firstReportedRead"] is not None:
        return "observedReadOnlyCandidate"
    return "noReportedReadOrWrite"


def totals(entries):
    entries = list(entries)
    return {"resourceCount": len(entries), "logicalBytes": sum(e["logicalBytes"] for e in entries),
            "textureCount": sum(e["kind"] == "texture" for e in entries),
            "bufferCount": sum(e["kind"] == "buffer" for e in entries)}


def boundary_set(entries, event):
    # This is AFTER event: a checkpoint for replaying that action itself must precede it.
    chosen = [e for e in entries if e.get("usage") and
              e["usage"]["firstPotentialWrite"] is not None and
              e["usage"]["firstPotentialWrite"] <= event and e["usage"]["lastUse"] > event]
    result = totals(chosen)
    result["resources"] = [e["resource"] for e in chosen]
    result["largestResources"] = [{k: e[k] for k in ("resource", "name", "kind", "logicalBytes")}
                                  for e in sorted(chosen, key=lambda e: e["logicalBytes"], reverse=True)[:20]]
    result["criterion"] = "reported potential write at/before boundary AND any reported use after boundary"
    result["nativeVRAMBytes"] = None
    result["completeCheckpointBytes"] = None
    result["safetyValidated"] = False
    return result


def number(resource):
    match = re.fullmatch(r"(?:ResourceId::)?(\d+)", str(resource))
    return int(match.group(1)) if match else None


def marker_category(name):
    return next((category for category, pattern in STAGE_PATTERNS
                 if re.search(pattern, name, re.I)), None)


def action_metadata(roots, rd):
    """Use existing action tree/output metadata, without requesting pipeline state per EID."""
    markers, actions = [], []

    def walk(nodes, parents):
        first, last, draw_count = None, None, 0
        for action in nodes:
            eid, flags, name = int(action.eventId), action.flags, str(action.customName)
            is_marker = bool(flags & (rd.ActionFlags.PushMarker | rd.ActionFlags.SetMarker))
            path = parents + ([name] if is_marker and name else [])
            child_first, child_last, child_draws = walk(action.children, path) if action.children else (None, None, 0)
            is_draw = bool(flags & (rd.ActionFlags.Drawcall | rd.ActionFlags.MeshDispatch))
            is_action = bool(flags & (rd.ActionFlags.Drawcall | rd.ActionFlags.MeshDispatch |
                rd.ActionFlags.Dispatch | rd.ActionFlags.DispatchRay | rd.ActionFlags.Clear |
                rd.ActionFlags.Copy | rd.ActionFlags.Resolve | rd.ActionFlags.GenMips |
                rd.ActionFlags.Present | rd.ActionFlags.BuildAccStruct))
            if is_action and not action.children:
                outputs = [str(r) for r in action.outputs if number(r) not in (None, 0)]
                depth = str(action.depthOut) if number(action.depthOut) not in (None, 0) else None
                actions.append({"eventId": eid, "flags": str(flags), "customName": name,
                                "markerPath": parents, "draw": is_draw,
                                "outputs": outputs, "depthOutput": depth})
                child_first = child_last = eid
                child_draws = int(is_draw)
            if is_marker and name:
                markers.append({"markerEventId": eid, "name": name, "parentPath": parents,
                                "firstActionEvent": child_first, "lastActionEvent": child_last,
                                "drawCount": child_draws, "inferredCategory": marker_category(name),
                                "inferenceSource": "marker text only; engine semantics and resumability unverified"})
            if child_first is not None:
                first = child_first if first is None else min(first, child_first)
                last = child_last if last is None else max(last, child_last)
            draw_count += child_draws
        return first, last, draw_count

    walk(roots, [])
    actions.sort(key=lambda a: a["eventId"])
    groups = []
    # Only adjacent draw records may join; any intervening dispatch/copy/clear starts a group.
    for index, a in enumerate(actions):
        if not a["draw"]:
            continue
        signature = (a["outputs"], a["depthOutput"], a["markerPath"])
        previous = groups[-1] if groups else None
        if previous and previous["signature"] == signature and previous["lastActionIndex"] + 1 == index:
            previous["lastEventId"], previous["lastActionIndex"] = a["eventId"], index
            previous["drawCount"] += 1
        else:
            label = ("depth-output-only candidate" if a["depthOutput"] and not a["outputs"] else
                     "multiple-color-output candidate" if len(set(a["outputs"])) >= 2 else
                     "other output group")
            groups.append({"firstEventId": a["eventId"], "lastEventId": a["eventId"],
                           "drawCount": 1, "outputs": a["outputs"], "depthOutput": a["depthOutput"],
                           "markerPath": a["markerPath"], "outputClassification": label,
                           "signature": signature, "lastActionIndex": index})
    for group in groups:
        del group["signature"], group["lastActionIndex"]
    return {"actionCount": len(actions), "lastActionEvent": actions[-1]["eventId"] if actions else 0,
            "markers": markers, "outputGroups": groups}


def choose_boundaries(action_data, explicit, maximum):
    chosen = {event: {"eventId": event, "source": "explicit requested event", "semantics": "unclassified"}
              for event in explicit}
    for category_name, _ in STAGE_PATTERNS:
        candidates = [m for m in action_data["markers"] if m["inferredCategory"] == category_name
                      and m["lastActionEvent"] and m["drawCount"]]
        if candidates and len(chosen) < maximum:
            best = max(candidates, key=lambda m: m["drawCount"])
            chosen.setdefault(best["lastActionEvent"], {"eventId": best["lastActionEvent"],
                "source": "largest matching marker region", "semantics": category_name,
                "marker": best["name"], "markerFirstAction": best["firstActionEvent"]})
    for label in ("depth-output-only candidate", "multiple-color-output candidate"):
        groups = [g for g in action_data["outputGroups"] if g["outputClassification"] == label]
        if groups and len(chosen) < maximum:
            best = max(groups, key=lambda g: g["drawCount"])
            chosen.setdefault(best["lastEventId"], {"eventId": best["lastEventId"],
                "source": "largest contiguous draw output group", "semantics": label,
                "firstEvent": best["firstEventId"], "outputs": best["outputs"],
                "depthOutput": best["depthOutput"]})
    return [chosen[e] for e in sorted(chosen)]


def main():
    env = lambda name, default="": os.environ.get("RENDERDOC_CHECKPOINT_" + name, default)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", default=env("CAPTURE"))
    parser.add_argument("--output", default=env("OUTPUT"))
    parser.add_argument("--boundaries", default=env("BOUNDARIES", "29385,62176"))
    parser.add_argument("--max-boundaries", type=int, default=8)
    parser.add_argument("--usage-limit", type=int, default=0, help="0 means every listed texture/buffer; nonzero is incomplete")
    parser.add_argument("--max-seconds", type=float, default=300)
    parser.add_argument("--max-resources", type=int, default=100000)
    parser.add_argument("--max-usage-entries", type=int, default=4000000,
                        help="Stop before processing more returned resource-use records; preserve partial evidence")
    parser.add_argument("--usage-windows", default=env("USAGE_WINDOWS"),
                        help="Up to 16 comma-separated checkpoint:target windows; diagnostic only")
    parser.add_argument("--build-label", default=env("BUILD_LABEL", "unspecified"))
    args = parser.parse_args()
    if not args.capture or not args.output:
        parser.error("Explicit --capture and new --output are required")
    capture, output = Path(args.capture).resolve(), Path(args.output).resolve()
    explicit = sorted({int(v.strip()) for v in args.boundaries.split(",") if v.strip()})
    try:
        windows = parse_windows(args.usage_windows)
    except ValueError as error:
        parser.error(str(error))
    if (not capture.is_file() or output.exists() or not 0 < args.max_seconds <= 3600 or args.usage_limit < 0
            or not 1 <= args.max_resources <= 100000 or not 1 <= args.max_usage_entries <= 4000000
            or len(explicit) > args.max_boundaries or any(e <= 0 for e in explicit)):
        parser.error("Capture must exist, output must be new, and limits/events must be valid")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"schema": "renderdoc-checkpoint-inventory/v1", "ok": False,
              "capture": str(capture), "captureBytes": capture.stat().st_size,
              "buildLabel": args.build_label, "processId": os.getpid(), "samples": [],
              "usageWindows": [{"afterEventId": start, "throughEventId": end} for start, end in windows],
              "measurement": "API logical metadata, not native GPU allocation or complete checkpoint storage",
              "limits": ["GetUsage lacks byte/subresource ranges and complete alias dependencies",
                         "reported output bindings do not establish writes to every byte",
                         "no overwrite kills inferred; later writes and barriers count as later use",
                         "resources without reported writes are not proven immutable",
                         "safe multi-queue/command-list/query resume boundaries have not been validated",
                         "no checkpoint was saved/restored; no speedup or native VRAM measured"]}
    started = time.perf_counter()

    def publish():
        report["elapsedMs"] = (time.perf_counter() - started) * 1000
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def check_budget():
        if time.perf_counter() - started > args.max_seconds:
            raise RuntimeError("Soft budget reached between native calls; partial evidence retained")

    def measure(name, callback, cleanup=False):
        if not cleanup:
            check_budget()
        sample = {"phase": name, "status": "running"}
        report["samples"].append(sample)
        publish()
        before = time.perf_counter()
        try:
            result = callback()
            sample["status"] = "complete"
            return result
        finally:
            sample["wallMs"] = (time.perf_counter() - before) * 1000
            if sample["status"] == "running":
                sample["status"] = "failed"
            publish()

    publish()
    import renderdoc as rd
    controller, capture_file = None, None
    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    entries = []
    try:
        rd.SetDebugLogFile(str(output.with_suffix(".rdoc.log")))
        capture_file = rd.OpenCaptureFile()
        status = measure("capture.openFile", lambda: capture_file.OpenFile(str(capture), "", None))
        if status != rd.ResultCode.Succeeded:
            raise RuntimeError("OpenFile failed: " + str(status))
        report["captureDriver"] = capture_file.DriverName()
        status, controller = measure("capture.openReplay", lambda: capture_file.OpenCapture(rd.ReplayOptions(), None))
        if status != rd.ResultCode.Succeeded or controller is None:
            raise RuntimeError("OpenCapture failed: " + str(status))
        report["replayAPI"] = str(controller.GetAPIProperties().pipelineType)
        descriptions = measure("metadata.resources", controller.GetResources)
        names = {str(d.resourceId): str(d.name) for d in descriptions}
        report["resourceTypes"] = dict(sorted(Counter(str(d.type) for d in descriptions).items()))
        textures = measure("metadata.textures", controller.GetTextures)
        buffers = measure("metadata.buffers", controller.GetBuffers)
        if len(textures) + len(buffers) > args.max_resources:
            raise RuntimeError("Metadata resource count exceeds configured limit")
        native_ids = {}
        for kind, objects in (("texture", textures), ("buffer", buffers)):
            for item in objects:
                key = str(item.resourceId)
                if key in native_ids:
                    raise RuntimeError("Duplicate inventory ResourceId: " + key)
                native_ids[key] = item.resourceId
                writable = bool(item.creationFlags & (rd.TextureCategory.ColorTarget | rd.TextureCategory.DepthTarget |
                    rd.TextureCategory.ShaderReadWrite)) if kind == "texture" else bool(item.creationFlags & rd.BufferCategory.ReadWrite)
                entry = {"resource": key, "name": names.get(key, ""), "kind": kind,
                         "logicalBytes": int(item.byteSize if kind == "texture" else item.length),
                         "creationFlags": str(item.creationFlags), "writeCapableCreationFlag": writable}
                if kind == "texture":
                    entry.update(width=int(item.width), height=int(item.height), depth=int(item.depth),
                                 arraysize=int(item.arraysize), mips=int(item.mips), samples=int(item.msSamp),
                                 format=item.format.Name())
                entries.append(entry)
        report["inventory"] = totals(entries)
        stores = measure("metadata.descriptorStores", controller.GetDescriptorStores)
        report["descriptorStores"] = {"storeCount": len(stores),
            "descriptorCount": sum(int(s.descriptorCount) for s in stores),
            "reportedAddressSpanBytes": sum(int(s.descriptorCount) * int(s.descriptorByteSize) for s in stores),
            "sizeMeaning": "API descriptor address span; not serialized checkpoint bytes or native VRAM",
            "stores": [{"resource": str(s.resourceId), "descriptorCount": int(s.descriptorCount),
                        "descriptorByteSize": int(s.descriptorByteSize)} for s in stores]}
        action_data = measure("metadata.actions", lambda: action_metadata(controller.GetRootActions(), rd))
        action_path = output.with_name(output.stem + ".actions.json")
        action_path.write_text(json.dumps(action_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report["actionMetadataFile"] = str(action_path)
        boundaries = choose_boundaries(action_data, explicit, args.max_boundaries)
        report["boundarySelection"] = boundaries
        limited = entries[:args.usage_limit] if args.usage_limit else entries
        report["usageProgress"] = {"completed": 0, "requested": len(limited), "inventoryResources": len(entries),
                                   "returnedUsageEntries": 0}

        def collect_usage():
            for index, entry in enumerate(limited):
                check_budget()
                use = controller.GetUsage(native_ids[entry["resource"]])
                report["usageProgress"]["returnedUsageEntries"] += len(use)
                if report["usageProgress"]["returnedUsageEntries"] > args.max_usage_entries:
                    raise RuntimeError("Returned resource-use entries exceed configured limit")
                pairs = [(int(u.eventId), str(u.usage)) for u in use]
                entry["usage"] = usage_summary(pairs)
                if windows:
                    entry["usageWindows"] = [usage_window(pairs, start, end) for start, end in windows]
                report["usageProgress"]["completed"] = index + 1
                if (index + 1) % 128 == 0:
                    publish()
        measure("metadata.allResourceUsage", collect_usage)
        report["usageComplete"] = len(limited) == len(entries)
        grouped = {}
        for entry in entries:
            entry["classification"] = category(entry)
            grouped.setdefault(entry["classification"], []).append(entry)
        report["classifications"] = {name: totals(group) for name, group in sorted(grouped.items())}
        report["possiblyWritten"] = totals(e for e in entries if e["classification"] in (
            "observedPotentialWrite", "writeCapableWithoutReportedWrite", "unknownUsage", "usageNotAvailable"))
        report["boundaries"] = [dict(b, afterEvent=True, sizing=boundary_set(entries, b["eventId"])) for b in boundaries]
        report["ok"] = report["usageComplete"]
    except Exception as error:
        report.update(error=str(error), traceback=traceback.format_exc())
    finally:
        if entries:
            inventory_path = output.with_name(output.stem + ".resources.json")
            inventory_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            report["resourceInventoryFile"] = str(inventory_path)
        if controller is not None:
            measure("cleanup.replay", controller.Shutdown, cleanup=True)
        if capture_file is not None:
            capture_file.Shutdown()
        rd.ShutdownReplay()
        publish()
    print(json.dumps({"ok": report["ok"], "output": str(output),
                      "usageComplete": report.get("usageComplete", False)}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
