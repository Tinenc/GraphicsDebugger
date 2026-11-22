"""Validate real shared Buffer range edits with native bytes and all bound output pixels.

Main opens a capture and uses the GPU. Run in a matching standalone RenderDoc Python process,
with GPU ownership coordinated separately. --remote [localhost] connects to an existing server;
it never starts a server, transfers a capture, or changes the capture file.
"""
from __future__ import print_function

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import traceback

from eid_checkpoint_profile import (compare_event, force_bypass_issues, output_subresources,
                                    parse_events, parse_status, selected_event_evidence,
                                    status_issues)
from eid_history_profile import Recorder, resource_number
from discover_d3d12_checkpoint_fixture import capture_hash


UINT64_MAX = (1 << 64) - 1


def unsigned64(text):
    value = int(text, 16 if str(text).lower().startswith("0x") else 10)
    if not 0 <= value <= UINT64_MAX:
        raise ValueError("Value must fit an unsigned 64-bit integer")
    return value


def patch_bytes(text, maximum):
    try:
        data = bytes.fromhex(text)
    except ValueError:
        raise ValueError("data-hex must contain complete hexadecimal bytes")
    if not data or len(data) > maximum:
        raise ValueError("Patch must be nonempty and fit max-patch-bytes")
    return data


def fixed_patch_values(text, count, maximum):
    if text is None:
        return []
    values = [patch_bytes(value.strip(), maximum) for value in text.split(",")]
    if not 4 <= len(values) <= 16 or len(set(values)) != len(values):
        raise ValueError("fixed-values-hex requires four to sixteen distinct byte sequences")
    if any(len(value) != count for value in values):
        raise ValueError("Every fixed value must have the same byte length as data-hex")
    if sum(len(value) for value in values) > 32 * 1024 * 1024:
        raise ValueError("Combined fixed values exceed 32 MiB")
    return values


def unchanged_checkpoint_issues(before, after):
    # Diagnostic lastResumeEvent/reason can describe the latest call. Counters, retained storage,
    # configuration, and the actual shared-edit generation must not change during force/readback.
    fields = ("hits", "builds", "misses", "invalidations", "rejected", "checkpointCount",
              "allocatedBytes", "sharedBytes", "events", "enabled", "byteBudget", "maxCheckpoints",
              "sharedBufferPatchGeneration", "sharedBufferPatchFirstAffectedEvent", "hasInitialCheckpoint")
    return ["checkpoint state changed: " + field for field in fields
            if before.get(field) != after.get(field)]


def readback_replay_issues(records):
    """Correct bytes do not prove immediate refresh when a resource getter repaired replay state."""
    native = {"D3D11", "D3D12", "OpenGL", "Vulkan", "ReplayDriverBoundary", "ReplayChunk"}
    return ["Resource readback performed implicit replay: api=%s type=%s" %
            (row.get("api"), row.get("type", "materialize")) for row in records
            if row.get("api") in ("MaterializeEvent", "ReplayLog") or
            (row.get("api") in native and "type" in row)]


def fixed_checkpoint_evidence(before, after, generation):
    cutoff = after.get("sharedBufferPatchFirstAffectedEvent")
    if not isinstance(cutoff, int) or not 0 <= cutoff < (1 << 32):
        raise RuntimeError("Fixed-value probe requires the native first-affected-event status field")
    if after.get("sharedBufferPatchGeneration") != generation:
        raise RuntimeError("Native checkpoint status and shared patch generation disagree")
    hits = after["hits"] - before["hits"]
    if hits < 0:
        raise RuntimeError("Native checkpoint hit counter decreased during the setter")
    def valid_prefix(status, event):
        return (isinstance(event, int) and 0 <= event < cutoff and
                (event > 0 or status.get("hasInitialCheckpoint") is True))
    prefix = sorted(event for event in before.get("events", []) if valid_prefix(before, event))
    remaining = sorted(event for event in prefix if event in after.get("events", []) and
                       valid_prefix(after, event))
    resume = after.get("lastResumeEvent")
    return {"firstAffectedEvent": cutoff, "setterNativeHits": hits,
            "setterResumeEvent": resume if hits else None,
            "setterResumeKind": ("FrameInitial" if resume == 0 and valid_prefix(after, resume)
                                  else "Event") if hits else None,
            "setterResumeBeforeCutoff": valid_prefix(after, resume) if hits else None,
            "beforeCutoffEvents": prefix, "beforeCutoffEventsStillPresent": remaining,
            "beforeCutoffEventsMissing": sorted(set(prefix) - set(remaining)),
            "afterEventsAtOrAfterCutoff": sorted(event for event in after.get("events", [])
                                                 if event >= cutoff),
            "retainedVersionIdentityProven": False,
            "retentionMeaning": "Only events strictly before cutoff are eligible for reuse. "
                                "The same EID in before/after status does not prove version identity; "
                                "the setter may rebuild later checkpoints."}


def fixed_fast_issues(rows):
    # A new range forces a full replay. The next same-layout update may build its prefix anchor.
    steady = rows[2:]
    issues = []
    if any(row["checkpointEvidence"]["firstAffectedEvent"] == 0 for row in steady):
        issues.append("steady fixed-value updates did not establish a proven first consumer")
    if sum(row["checkpointEvidence"]["setterNativeHits"] > 0 for row in steady) < 2:
        issues.append("fewer than two steady fixed-value setters restored a native checkpoint")
    if any(row["checkpointEvidence"]["setterNativeHits"] > 0 and
           not row["checkpointEvidence"]["setterResumeBeforeCutoff"] for row in steady):
        issues.append("a steady setter hit did not prove a resume point strictly before the first consumer")
    return issues


def read_window(length, offset, count, neighbors):
    if min(length, offset, count, neighbors) < 0 or not count:
        raise ValueError("Invalid buffer window")
    if length > UINT64_MAX or offset > UINT64_MAX - count or offset + count > length:
        raise ValueError("Patch window exceeds the actual captured buffer")
    begin = max(0, offset - neighbors)
    end = min(length, offset + count + neighbors)
    return begin, end - begin, offset - begin


def roundtrip(events):
    if len(events) < 2 or len(set(events)) != len(events):
        raise ValueError("Roundtrip requires distinct EIDs")
    return list(events) + list(events[-2::-1])


def byte_witness(raw, relative, count):
    raw = bytes(raw)
    if relative < 0 or count < 1 or relative + count > len(raw):
        raise ValueError("Incomplete native buffer window")
    region = raw[relative:relative + count]
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "patchBytes": len(region), "patchSha256": hashlib.sha256(region).hexdigest(),
            "patchHex": region.hex() if len(region) <= 4096 else None,
            "beforeHex": raw[:relative].hex(), "afterHex": raw[relative + count:].hex()}


def window_checks(raw, baseline, patch, relative, require_neighbors=False):
    if len(raw) != len(baseline) or relative + len(patch) > len(raw):
        return {"patchMatches": False, "neighborsEqualBaseline": False,
                "issues": ["native buffer readback length differs from the requested window"]}
    end = relative + len(patch)
    matches = raw[relative:end] == patch
    neighbors = raw[:relative] == baseline[:relative] and raw[end:] == baseline[end:]
    issues = [] if matches else ["native buffer bytes do not contain the active shared patch"]
    if require_neighbors and not neighbors:
        issues.append("fixture requires unchanged neighbors but neighboring bytes differ")
    return {"patchMatches": matches, "neighborsEqualBaseline": neighbors, "issues": issues}


def state_comparison(observed, canonical, baseline, require_original=False):
    verdict = compare_event(observed, canonical)
    stable = len(canonical) == 2 and canonical[0] == canonical[1]
    result = {"status": verdict,
              "visibleEffect": ("changedBoundOutputPixels" if canonical[0]["output"]["pixels"] !=
                                baseline["output"]["pixels"] else "noVisibleEffect") if stable else
                               "inconclusiveCanonicalUnstable"}
    result["equalsUneditedBaseline"] = canonical[0] == baseline if stable else None
    if stable and require_original and canonical[0] != baseline:
        result["status"] = "failedOriginalStateNotRecovered"
    return result


def summarize_times(visits):
    groups = {}
    for row in visits:
        key = (row["mode"], row["stage"], row["eventId"], row["force"])
        groups.setdefault(key, []).append(row)
    result = []
    for key in sorted(groups):
        rows = groups[key]
        item = dict(zip(("mode", "stage", "eventId", "force"), key))
        item["count"] = len(rows)
        for name in ("replayWallMs", "bufferReadbackWallMs", "outputReadbackWallMs"):
            values = [row[name] for row in rows]
            item[name] = {"median": statistics.median(values), "min": min(values), "max": max(values)}
        result.append(item)
    return result


class SharedBufferRecorder(Recorder):
    def publish(self):
        # No remote backend log does not mean zero remote replay time. Also do not add nested
        # backend scopes to their parents when retaining the common Recorder's diagnostics.
        for sample in self.report["samples"]:
            if "timingRecords" not in sample:
                continue
            backend = [r for r in sample["timingRecords"] if r.get("api") in
                       ("D3D11", "D3D12", "OpenGL", "Vulkan") and
                       isinstance(r.get("total_ms"), (int, float))]
            top = [r for r in backend if r.get("replay_depth") == 0]
            known = bool(top) and all("replay_depth" in r for r in backend)
            sample["backendTimingAvailable"] = known
            sample["backendReplayCalls"] = len(top) if known else None
            sample["backendReplayTotalMs"] = sum(r["total_ms"] for r in top) if known else None
            sample["nonReplayResidualMs"] = (sample["wallMs"] - sample["backendReplayTotalMs"]
                                               if known else None)
            # Raw records remain available; an inclusive nested summary is not an additive cost.
            sample["backendSummary"] = None
        Recorder.publish(self)


def binary_identity(path):
    path = Path(path).resolve()
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": capture_hash(path)}


def loaded_native_paths(pid):
    if os.name == "nt":
        # Read only the explicitly selected process's loaded RenderDoc modules, not its command
        # line or environment. A server binary path alone would not prove what it actually loaded.
        script = ("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding\n"
                  "@(Get-Process -Id %d -ErrorAction Stop).Modules | "
                  "Where-Object { $_.ModuleName -in @('renderdoc.dll', 'system_load.dll') } | "
                  "ForEach-Object { $_.FileName } | ConvertTo-Json -Compress" % pid)
        powershell = str(Path(os.environ["SystemRoot"]) / "System32" / "WindowsPowerShell" /
                         "v1.0" / "powershell.exe")
        raw = subprocess.check_output([powershell, "-NoProfile", "-NonInteractive", "-Command", script],
                                      creationflags=0x08000000, timeout=20).decode("utf-8-sig").strip()
        values = json.loads(raw) if raw else []
        return [values] if isinstance(values, str) else values
    maps = Path("/proc") / str(pid) / "maps"
    if not maps.is_file():
        return []
    paths = set()
    for line in maps.read_text(encoding="utf-8").splitlines():
        fields = line.split(None, 5)
        if len(fields) == 6 and Path(fields[5]).name in ("librenderdoc.so", "libVIVO50_SystemLoad.so"):
            paths.add(fields[5])
    return sorted(paths)


def build_identity(rd, remote_pid):
    native = [binary_identity(path) for path in loaded_native_paths(os.getpid())]
    result = {"pythonModule": binary_identity(rd.__file__), "nativeModules": native,
              "version": str(rd.GetVersionString()) if hasattr(rd, "GetVersionString") else None,
              "commit": str(rd.GetCommitHash()) if hasattr(rd, "GetCommitHash") else None}
    if not native:
        raise RuntimeError("Cannot establish the loaded native replay library identity")
    if remote_pid:
        remote = [binary_identity(path) for path in loaded_native_paths(remote_pid)]
        result["remoteServerProcessId"] = remote_pid
        result["remoteNativeModules"] = remote
        result["sameNativeBuild"] = bool(remote) and {r["sha256"] for r in remote} == {
            r["sha256"] for r in native}
        if not result["sameNativeBuild"]:
            raise RuntimeError("The selected server process did not load the same native replay build")
    return result


def output_witness(controller, rd, maximum):
    pipe = controller.GetPipelineState()
    textures = {str(t.resourceId): t for t in controller.GetTextures()}
    witness = {"eventId": int(controller.GetCurrentEventID()),
               "topology": str(pipe.GetPrimitiveTopology()), "shaders": {}, "bindings": [], "pixels": []}
    for name in ("Vertex", "Hull", "Domain", "Geometry", "Pixel", "Compute", "Task", "Mesh"):
        stage = getattr(rd.ShaderStage, name, None)
        if stage is not None:
            witness["shaders"][name] = str(pipe.GetShader(stage))
    targets = [("color", slot, b) for slot, b in enumerate(pipe.GetOutputTargets())]
    targets.append(("depth", 0, pipe.GetDepthTarget()))
    requests, seen, estimated = [], set(), 0
    for kind, slot, binding in targets:
        row = {"kind": kind, "slot": slot, "resource": str(binding.resource),
               "firstMip": int(binding.firstMip), "numMips": int(binding.numMips),
               "firstSlice": int(binding.firstSlice), "numSlices": int(binding.numSlices),
               "format": binding.format.Name()}
        witness["bindings"].append(row)
        if binding.resource == rd.ResourceId.Null():
            continue
        texture = textures.get(str(binding.resource))
        if texture is None:
            raise RuntimeError("Bound output is absent from texture metadata: " + json.dumps(row))
        for sub in output_subresources(texture, binding):
            key = (str(binding.resource),) + sub
            if key in seen:
                continue
            seen.add(key)
            estimated += (max(1, int(texture.width) >> sub[0]) * max(1, int(texture.height) >> sub[0]) *
                          max(1, int(texture.depth) >> sub[0]) * 16)
            requests.append((binding.resource, sub))
    if not requests or estimated > maximum:
        raise RuntimeError("All bound color/depth subresources must fit max-read-mib; none may be omitted")
    total = 0
    for resource, sub in requests:
        raw = bytes(controller.GetTextureData(resource, rd.Subresource(*sub)))
        total += len(raw)
        if not raw or total > maximum:
            raise RuntimeError("Empty or over-budget native output readback")
        witness["pixels"].append({"resource": str(resource), "subresource": list(sub),
                                   "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    fatal = controller.GetFatalErrorStatus()
    if fatal.code != rd.ResultCode.Succeeded:
        raise RuntimeError("Native replay failed: " + str(fatal))
    return witness


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("capture", "events", "buffer", "offset", "data-hex", "output"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--remote", nargs="?", const="localhost",
                        help="Connect to an existing localhost/127.0.0.1 replay server")
    parser.add_argument("--remote-server-pid", type=int,
                        help="Optional explicit server PID to verify its loaded native DLL/SO hash")
    parser.add_argument("--require-neighbors-unchanged", action="store_true",
                        help="Strict fixture check; leave off when captured writes can propagate patched bytes")
    parser.add_argument("--require-identity-unchanged", action="store_true",
                        help="Require unchanged original bytes/pixels where the identity window matches; use only a known static fixture")
    parser.add_argument("--fixed-values-hex",
                        help="Optional four to sixteen distinct comma-separated packed byte values, each the data-hex length; test setter refresh at the last selected draw")
    parser.add_argument("--require-fast", action="store_true",
                        help="Require at least two native setter checkpoint hits after range introduction and one anchor warmup in the fixed-value stage")
    parser.add_argument("--neighbors", type=int, default=16)
    parser.add_argument("--max-patch-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--max-read-mib", type=int, default=512)
    parser.add_argument("--budget-mib", type=int, default=0)
    parser.add_argument("--max-checkpoints", type=int, default=0)
    parser.add_argument("--min-checkpoint-hits", type=int, default=0)
    parser.add_argument("--max-seconds", type=int, default=300)
    parser.add_argument("--build-label", default="unspecified")
    args = parser.parse_args(argv)
    try:
        events = parse_events(args.events)
        buffer_id, offset = unsigned64(args.buffer), unsigned64(args.offset)
        data = patch_bytes(args.data_hex, args.max_patch_bytes)
        fixed_values = fixed_patch_values(args.fixed_values_hex, len(data), args.max_patch_bytes)
        if args.require_fast and not fixed_values:
            raise ValueError("require-fast requires fixed-values-hex")
        if not buffer_id or offset > UINT64_MAX - len(data):
            raise ValueError("Use a non-null captured Buffer ID and a non-overflowing byte range")
        if (not 0 <= args.neighbors <= 4096 or not 0 < args.max_patch_bytes <= 32 * 1024 * 1024 or
                args.max_read_mib <= 0 or args.budget_mib < 0 or not 0 <= args.max_checkpoints <= 64 or
                args.min_checkpoint_hits < 0 or args.max_seconds <= 0):
            raise ValueError("Invalid sample or resource limits")
        if args.remote and not re.fullmatch(r"(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?", args.remote):
            raise ValueError("This probe accepts only an explicitly local replay server")
        if args.remote_server_pid is not None and (not args.remote or args.remote_server_pid <= 0):
            raise ValueError("remote-server-pid requires --remote and a positive PID")
    except ValueError as error:
        parser.error(str(error))
    capture, output = Path(args.capture).resolve(), Path(args.output).resolve()
    if not capture.is_file() or any(p.exists() for p in
                                   (output, output.with_suffix(".rdoc.log"), output.with_suffix(".native.log"))):
        parser.error("Capture must exist; choose a new output evidence stem")
    output.parent.mkdir(parents=True, exist_ok=True)
    configuration = dict(vars(args))
    configuration["data_hex"] = data.hex() if len(data) <= 4096 else "omitted; see dataSha256"
    if fixed_values:
        configuration["fixed_values_hex"] = ([value.hex() for value in fixed_values] if len(data) <= 4096
                                              else "omitted; see fixedValues.rows.valueSha256")
    environment = {"RENDERDOC_EID_TIMING": "1", "RENDERDOC_D3D12_ENABLE_PREPARED_REPLAY": "0",
                   "RENDERDOC_D3D12_DISABLE_PREPARED_REPLAY": "1"}
    os.environ.update(environment)
    report = {"schema": "renderdoc-shared-buffer-profile/v1", "ok": False, "configuration": configuration,
              "capture": str(capture), "events": events, "buffer": buffer_id, "byteOffset": offset,
              "dataBytes": len(data), "dataSha256": hashlib.sha256(data).hexdigest(),
              "environment": environment, "samples": [], "visits": [], "mutations": [],
              "comparisons": [], "issues": [], "processId": os.getpid(), "modeResults": [],
              "limits": ["Typed Buffer range test; no UI, display/scanout, or capture-file mutation",
                         "Replay wall time excludes explicit Buffer/output readback; setters may themselves replay",
                         "Native accessor bytes and every bound RT/depth view are checked, not every UAV",
                         "Canonical references use the identical patch configuration and two force replays",
                         "Neighbor differences from the unedited frame can be legal dependency propagation",
                         "No visible pixel change is noVisibleEffect, never proof of a working slider",
                         "Remote wall time includes network/proxy transport; server timing may be unavailable"]}
    recorder = SharedBufferRecorder(report, output, output.with_suffix(".rdoc.log"), args.max_seconds)
    recorder.publish()
    rd, cap, controller, remote = None, None, None, None
    initialised = False
    baseline_raw, baseline_witness = {}, {}
    resource, begin, size, relative = None, None, None, None
    generation = None

    def status():
        value = parse_status(controller.GetReplayCheckpointStatus())
        report["issues"].extend(status_issues(value))
        return value

    def configure(enabled):
        controller.ConfigureReplayCheckpoints(enabled, args.budget_mib * 1024 * 1024, args.max_checkpoints)
        value = status()
        report["issues"].extend(status_issues(value, disabled=not enabled))
        return value

    def change(label, operation, require_advance=True, cleanup=False):
        before = int(controller.GetSharedBufferPatchGeneration())
        result, timing = recorder.measure(label, operation, cleanup=cleanup)
        if str(result):
            raise RuntimeError(label + " rejected: " + str(result))
        after = int(controller.GetSharedBufferPatchGeneration())
        if after < before or (require_advance and after <= before):
            raise RuntimeError("Shared patch generation did not advance after " + label)
        report["mutations"].append({"operation": label, "generationBefore": before,
                                    "generationAfter": after, "setterWallMs": timing["wallMs"],
                                    "checkpointStatus": status()})
        return after

    def visit(mode, stage, event, force, expected=None, save_baseline=False):
        before = status()
        _, timing = recorder.measure(mode + "." + stage + ".replay",
                                     lambda: controller.SetFrameEvent(event, force), event)
        after = status()
        if force:
            report["issues"].extend(force_bypass_issues(before, after))
        actual_generation = int(controller.GetSharedBufferPatchGeneration())
        if actual_generation != generation:
            raise RuntimeError("Replay changed the shared patch generation")
        raw, buffer_timing = recorder.measure(mode + "." + stage + ".bufferReadback",
                                             lambda: bytes(controller.GetBufferData(resource, begin, size)), event)
        check_readback(mode + "." + stage + ".bufferReadback", buffer_timing)
        if len(raw) != size:
            raise RuntimeError("Incomplete native GetBufferData window")
        output_pixels, pixel_timing = recorder.measure(
            mode + "." + stage + ".outputReadback",
            lambda: output_witness(controller, rd, args.max_read_mib * 1024 * 1024), event)
        check_readback(mode + "." + stage + ".outputReadback", pixel_timing)
        if output_pixels["eventId"] != event:
            raise RuntimeError("Effective EID differs from the requested EID")
        witness = {"buffer": byte_witness(raw, relative, len(data)), "output": output_pixels}
        row = {"mode": mode, "stage": stage, "eventId": event, "force": force,
               "generation": actual_generation, "before": before, "afterReplay": after,
               "afterReadback": status(), "replayWallMs": timing["wallMs"],
               "bufferReadbackWallMs": buffer_timing["wallMs"],
               "outputReadbackWallMs": pixel_timing["wallMs"], "witness": witness}
        if mode == "fixedValues" and force:
            row["forceCheckpointIssues"] = unchanged_checkpoint_issues(before, row["afterReadback"])
            report["issues"].extend(stage + ".force: " + issue for issue in row["forceCheckpointIssues"])
        if expected is not None:
            row["windowChecks"] = window_checks(raw, baseline_raw[event], expected, relative,
                                                 args.require_neighbors_unchanged)
            report["issues"].extend(row["windowChecks"]["issues"])
        if save_baseline and event not in baseline_raw:
            baseline_raw[event], baseline_witness[event] = raw, witness
        report["visits"].append(row)
        recorder.publish()
        return witness

    def check_readback(label, timing):
        issues = readback_replay_issues(timing["timingRecords"])
        if issues:
            report["issues"].extend(label + ": " + issue for issue in issues)
            raise RuntimeError(label + ": " + "; ".join(issues))

    def validate_stage(mode, stage, expected=None, original=False):
        observed = {event: [] for event in events}
        for event in roundtrip(events):
            observed[event].append(visit(mode, stage, event, False, expected))
        for event in events:
            canonical = [visit(mode, stage + ".forceReference", event, True, expected) for _ in range(2)]
            identity_matches = (stage == "identity" and baseline_raw[event][relative:relative + len(data)] == expected)
            comparison = state_comparison(observed[event], canonical, baseline_witness[event],
                                          original or (identity_matches and args.require_identity_unchanged))
            comparison.update(mode=mode, stage=stage, eventId=event, normalVisits=len(observed[event]),
                              identityMatchesThisEventBaseline=identity_matches)
            report["comparisons"].append(comparison)
            if comparison["status"] != "passed":
                report["issues"].append("%s/%s/EID%d: %s" % (mode, stage, event, comparison["status"]))
        recorder.publish()

    def validate_fixed_values():
        nonlocal generation
        event = events[-1]
        fixed = {"eventId": event, "rows": [], "requireFast": args.require_fast,
                 "meaning": "All fixed values are applied consecutively with immediate Buffer/all-bound-output "
                            "witnesses before any force reference. Resource getters must not perform implicit "
                            "replay. References later reinstall identical bytes in separately recorded generations. "
                            "Setter wall excludes subsequent native readbacks.",
                 "warmup": "Value 1 introduces the range; value 2 may establish the prefix anchor."}
        report["fixedValues"] = fixed
        # Position only once. In particular, never repair a stale setter result with a replay before
        # reading it. Force references below cannot hide a stale immediate witness.
        visit("fixedValues", "position", event, False)
        for index, value in enumerate(fixed_values):
            label = "fixedValues.value%d" % (index + 1)
            before, before_generation = status(), generation
            generation = change(label + ".set", lambda: controller.SetSharedBufferPatch(resource, offset, value))
            mutation = report["mutations"][-1]
            after = mutation["checkpointStatus"]
            row = {"index": index + 1, "eventId": event, "valueSha256": hashlib.sha256(value).hexdigest(),
                   "valueHex": value.hex() if len(value) <= 4096 else None,
                   "generationBefore": before_generation, "generationAfter": generation,
                   "setterWallMs": mutation["setterWallMs"], "beforeSetter": before,
                   "afterSetter": after, "checkpointEvidence": fixed_checkpoint_evidence(before, after, generation)}
            fixed["rows"].append(row)
            if int(controller.GetCurrentEventID()) != event:
                raise RuntimeError("Fixed-value setter left a stale current EID")
            raw, buffer_timing = recorder.measure(label + ".immediateBufferReadback",
                lambda: bytes(controller.GetBufferData(resource, begin, size)), event)
            check_readback(label + ".immediateBufferReadback", buffer_timing)
            pixels, pixel_timing = recorder.measure(label + ".immediateOutputReadback",
                lambda: output_witness(controller, rd, args.max_read_mib * 1024 * 1024), event)
            check_readback(label + ".immediateOutputReadback", pixel_timing)
            if pixels["eventId"] != event or int(controller.GetSharedBufferPatchGeneration()) != generation:
                raise RuntimeError("Fixed-value immediate output has a stale EID or generation")
            if len(raw) != size:
                raise RuntimeError("Incomplete fixed-value native Buffer window")
            witness = {"buffer": byte_witness(raw, relative, len(value)), "output": pixels}
            row.update(immediateWitness=witness, afterImmediateReadback=status(),
                       bufferReadbackWallMs=buffer_timing["wallMs"], outputReadbackWallMs=pixel_timing["wallMs"],
                       windowChecks=window_checks(raw, baseline_raw[event], value, relative,
                                                  args.require_neighbors_unchanged))
            report["issues"].extend(row["windowChecks"]["issues"])
            row["readbackCheckpointIssues"] = unchanged_checkpoint_issues(after, row["afterImmediateReadback"])
            report["issues"].extend(label + ".readback: " + issue for issue in row["readbackCheckpointIssues"])
            recorder.publish()

        # Preserve every immediate witness before running any independent replay. Reinstalling the
        # same byte configuration later may advance its generation; it cannot alter the saved image.
        previous_pixels = None
        for index, (value, row) in enumerate(zip(fixed_values, fixed["rows"])):
            label = "fixedValues.value%d" % (index + 1)
            generation = change("fixedValues.referenceValue%d.install" % (index + 1),
                                lambda: controller.SetSharedBufferPatch(resource, offset, value))
            row["referenceGeneration"] = generation
            canonical = []
            row["forceReferenceVisits"] = []
            for _ in range(2):
                canonical.append(visit("fixedValues", "value%d.forceReference" % (index + 1), event, True, value))
                row["forceReferenceVisits"].append(len(report["visits"]) - 1)
            row["comparison"] = state_comparison([row["immediateWitness"]], canonical, baseline_witness[event])
            stable = canonical[0] == canonical[1]
            row["pixelsChangedFromPreviousValue"] = (canonical[0]["output"]["pixels"] != previous_pixels
                                                        if stable and previous_pixels is not None else None)
            previous_pixels = canonical[0]["output"]["pixels"] if stable else None
            if row["comparison"]["status"] != "passed":
                report["issues"].append(label + ": " + row["comparison"]["status"])
            recorder.publish()
        fixed["observedSetterHits"] = sum(row["checkpointEvidence"]["setterNativeHits"] for row in fixed["rows"])
        fixed["steadyHitUpdates"] = sum(row["checkpointEvidence"]["setterNativeHits"] > 0
                                         for row in fixed["rows"][2:])
        fixed["fastIssues"] = fixed_fast_issues(fixed["rows"])
        fixed["fastVerified"] = not fixed["fastIssues"] and all(
            row["comparison"]["status"] == "passed" and not row["windowChecks"]["issues"] and
            not row["readbackCheckpointIssues"] and all(
                not report["visits"][ref]["forceCheckpointIssues"] for ref in row["forceReferenceVisits"])
            for row in fixed["rows"])
        if args.require_fast:
            report["issues"].extend(fixed["fastIssues"])
        times = [row["setterWallMs"] for row in fixed["rows"]]
        fixed["setterWallMs"] = {"count": len(times), "median": statistics.median(times),
                                "min": min(times), "max": max(times)}
        fixed["visibleEffect"] = ("changedBoundOutputPixels" if any(
            row["comparison"]["visibleEffect"] == "changedBoundOutputPixels" for row in fixed["rows"])
                                  else "noVisibleEffect")
        fixed["visibleValueTransitions"] = sum(row["pixelsChangedFromPreviousValue"] is True
                                                for row in fixed["rows"])
        generation = change("fixedValues.clearAll", controller.ClearSharedBufferPatches)
        before_clear_readback = status()
        if int(controller.GetCurrentEventID()) != event:
            raise RuntimeError("Fixed-value clear left a stale current EID")
        raw, buffer_timing = recorder.measure("fixedValues.clearAll.immediateBufferReadback",
            lambda: bytes(controller.GetBufferData(resource, begin, size)), event)
        check_readback("fixedValues.clearAll.immediateBufferReadback", buffer_timing)
        pixels, pixel_timing = recorder.measure("fixedValues.clearAll.immediateOutputReadback",
            lambda: output_witness(controller, rd, args.max_read_mib * 1024 * 1024), event)
        check_readback("fixedValues.clearAll.immediateOutputReadback", pixel_timing)
        if (len(raw) != size or pixels["eventId"] != event or
                int(controller.GetSharedBufferPatchGeneration()) != generation):
            raise RuntimeError("Fixed-value clear witness has incomplete bytes or a stale EID/generation")
        fixed["clearImmediateWitness"] = {"buffer": byte_witness(raw, relative, len(data)), "output": pixels}
        fixed["clearReadbackCheckpointIssues"] = unchanged_checkpoint_issues(before_clear_readback, status())
        report["issues"].extend("fixedValues.clear.readback: " + issue
                                for issue in fixed["clearReadbackCheckpointIssues"])
        restored = [visit("fixedValues", "cleared.forceReference", event, True) for _ in range(2)]
        fixed["clearComparison"] = state_comparison([fixed["clearImmediateWitness"]], restored,
                                                    baseline_witness[event], True)
        fixed["fastVerified"] &= (fixed["clearComparison"]["status"] == "passed" and
                                  not fixed["clearReadbackCheckpointIssues"])
        if fixed["clearComparison"]["status"] != "passed":
            report["issues"].append("fixedValues.clear: " + fixed["clearComparison"]["status"])

    try:
        import renderdoc as rd
        required = ("SetSharedBufferPatch", "ClearSharedBufferPatch", "ClearSharedBufferPatches",
                    "GetSharedBufferPatchGeneration")
        report["buildIdentity"] = build_identity(rd, args.remote_server_pid)
        report["remoteBuildVerified"] = bool(args.remote_server_pid) if args.remote else None
        report["sameBuildPerformanceComparisonEligible"] = not args.remote or bool(args.remote_server_pid)
        rd.InitialiseReplay(rd.GlobalEnvironment(), [])
        initialised = True
        rd.SetDebugLogFile(str(recorder.log_path))
        report["captureIdentity"], _ = recorder.measure("capture.identity", lambda: binary_identity(capture))
        cap = rd.OpenCaptureFile()
        result, _ = recorder.measure("capture.openFile", lambda: cap.OpenFile(str(capture), "", None))
        if result != rd.ResultCode.Succeeded:
            raise RuntimeError("OpenFile failed: " + str(result))
        report["captureDriver"] = str(cap.DriverName())
        if args.remote:
            connection, _ = recorder.measure("remote.connect", lambda: rd.CreateRemoteServerConnection(args.remote))
            result, remote = connection
            if result != rd.ResultCode.Succeeded or remote is None:
                raise RuntimeError("Remote connection/protocol rejected: " + str(result))
            report["remoteLocalProxies"] = list(remote.LocalProxies())
            opened, _ = recorder.measure("capture.openRemoteReplay", lambda: remote.OpenCapture(
                rd.RemoteServer.NoPreference, str(capture), rd.ReplayOptions(), None))
        else:
            opened, _ = recorder.measure("capture.openReplay", lambda: cap.OpenCapture(rd.ReplayOptions(), None))
        result, controller = opened
        if result != rd.ResultCode.Succeeded or controller is None:
            raise RuntimeError("OpenCapture failed: " + str(result))
        if any(not hasattr(controller, name) for name in required):
            raise RuntimeError("Loaded Python bindings/native build lack the shared Buffer public API")
        props = controller.GetAPIProperties()
        report["replayAPI"], report["remoteReplay"] = str(props.pipelineType), bool(props.remoteReplay)
        if bool(props.remoteReplay) != bool(args.remote):
            raise RuntimeError("Requested replay mode differs from actual controller mode")
        report["selectedEventEvidence"] = selected_event_evidence(controller, events)
        if any(row["status"] != "exactAPIEvent" for row in report["selectedEventEvidence"]):
            raise RuntimeError("Every selected EID must have exact structured API-event evidence")
        if fixed_values:
            def is_selected_draw(actions):
                return any((int(action.eventId) == events[-1] and
                            bool(action.flags & rd.ActionFlags.Drawcall)) or is_selected_draw(action.children)
                           for action in actions)
            if not is_selected_draw(controller.GetRootActions()):
                raise RuntimeError("fixed-values-hex requires the last selected EID to be an actual draw")
        buffers = [b for b in controller.GetBuffers() if resource_number(b.resourceId) == buffer_id]
        if len(buffers) != 1:
            raise RuntimeError("buffer must identify one actual captured Buffer description")
        buffer = buffers[0]
        resource = buffer.resourceId
        begin, size, relative = read_window(int(buffer.length), offset, len(data), args.neighbors)
        report["bufferDescription"] = {"resource": str(resource), "length": int(buffer.length),
                                       "creationFlags": str(buffer.creationFlags)}
        report["bufferUsage"] = [{"eventId": int(u.eventId), "usage": str(u.usage)}
                                  for u in controller.GetUsage(resource)]
        report["readWindow"] = {"offset": begin, "bytes": size, "patchRelativeOffset": relative}
        generation = change("initial.clearAll", controller.ClearSharedBufferPatches, False)
        report["initialDisabledStatus"] = configure(False)
        for event in events:
            canonical = [visit("initial", "baseline", event, True, save_baseline=True) for _ in range(2)]
            if compare_event([canonical[0]], canonical) != "passed":
                raise RuntimeError("Unedited canonical native bytes/pixels are unstable at EID " + str(event))
        changed_events = [event for event in events if baseline_raw[event][relative:relative + len(data)] != data]
        report["changedByteEvents"] = changed_events
        if len(changed_events) < 2:
            raise RuntimeError("Requested edit must differ from the original native bytes at two or more EIDs")
        identity_event = changed_events[0]
        identity = baseline_raw[identity_event][relative:relative + len(data)]
        report["identitySourceEvent"] = identity_event
        report["identityMeaning"] = ("Original bytes at the evidenced source event, held as a persistent patch. "
                                      "Equal end-state bytes alone do not prove that all earlier consumers saw "
                                      "those bytes; identity pixel differences are recorded separately.")
        report["identityBytes"] = byte_witness(identity, 0, len(identity))
        for mode, enabled in (("checkpointsDisabled", False), ("checkpointsEnabled", True)):
            mode_result = {"mode": mode, "initialStatus": configure(enabled)}
            report["modeResults"].append(mode_result)
            generation = change(mode + ".setIdentity", lambda: controller.SetSharedBufferPatch(resource, offset, identity))
            validate_stage(mode, "identity", identity)
            generation = change(mode + ".setChanged", lambda: controller.SetSharedBufferPatch(resource, offset, data))
            validate_stage(mode, "changed", data)
            if enabled:
                generation = change(mode + ".clearAll", controller.ClearSharedBufferPatches)
            else:
                generation = change(mode + ".clearRange", lambda: controller.ClearSharedBufferPatch(resource, offset, len(data)))
            validate_stage(mode, "cleared", original=True)
            mode_result["finalStatus"] = status()
            mode_result["observedNormalHits"] = sum(max(0, v["afterReplay"]["hits"] - v["before"]["hits"])
                                                   for v in report["visits"] if v["mode"] == mode and not v["force"])
        if fixed_values:
            validate_fixed_values()
        report["finalDisabledStatus"] = configure(False)
        hits = report["modeResults"][-1]["observedNormalHits"]
        if hits < args.min_checkpoint_hits:
            report["issues"].append("Required real checkpoint hits were not observed")
        changed = [row for row in report["comparisons"] if row["stage"] == "changed"]
        report["visibleEffect"] = ("changedBoundOutputPixels" if any(
            row["visibleEffect"] == "changedBoundOutputPixels" for row in changed) else "noVisibleEffect")
        report["timingSummary"] = summarize_times(report["visits"])
        report["measurementComplete"] = True
        report["ok"] = not report["issues"]
        report["verdict"] = ("passedNativeBytesAndPixels" if report["ok"] and report["visibleEffect"] != "noVisibleEffect"
                             else "passedNativeBytesAndReferencesNoVisibleEffect" if report["ok"] else "failedOrInconclusive")
    except Exception as error:
        report.update(error=str(error), traceback=traceback.format_exc(), ok=False, verdict="failedOrInconclusive")
    finally:
        def cleanup(label, callback):
            try:
                return recorder.measure(label, callback, cleanup=True)[0]
            except Exception as error:
                report.setdefault("cleanupErrors", []).append({"phase": label, "error": str(error)})
                report.update(ok=False, verdict="cleanupFailed")
        if controller is not None:
            def clear_for_cleanup():
                error = str(controller.ClearSharedBufferPatches())
                if error:
                    raise RuntimeError(error)
            cleanup("cleanup.clearAll", clear_for_cleanup)
            cleanup("cleanup.disable", lambda: configure(False))
        def preserve_log():
            if recorder.log_path.is_file():
                path = output.with_suffix(".native.log")
                path.write_bytes(recorder.log_path.read_bytes())
                report["nativeLog"] = str(path)
        cleanup("cleanup.preserveLog", preserve_log)
        if controller is not None:
            cleanup("cleanup.closeReplay", lambda: remote.CloseCapture(controller) if remote else controller.Shutdown())
        if remote is not None:
            cleanup("cleanup.remoteConnection", remote.ShutdownConnection)
        if cap is not None:
            cleanup("cleanup.captureFile", cap.Shutdown)
        cleanup("cleanup.refreshLog", preserve_log)
        if initialised:
            cleanup("cleanup.runtime", rd.ShutdownReplay)
        recorder.publish()
    print(json.dumps({"ok": report["ok"], "output": str(output), "verdict": report.get("verdict"),
                      "visibleEffect": report.get("visibleEffect")}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
