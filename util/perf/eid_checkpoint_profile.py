"""Standalone real checkpoint/replay validation. Main opens a capture and uses the GPU.

Coordinate execution with other replay processes. Full native output readbacks are separately
timed; forced reference replays must bypass checkpoint save/restore without dropping the cache.
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
from types import SimpleNamespace

from eid_history_profile import Recorder, integer_list, resource_number


def parse_events(text):
    events = integer_list(text)
    if not 2 <= len(events) <= 8:
        raise ValueError("Provide two to eight distinct positive EIDs")
    return sorted(events)


def parse_status(text):
    status = json.loads(str(text))
    if status.get("schema") != "renderdoc-replay-checkpoints/v1":
        raise ValueError("Unsupported checkpoint status schema")
    for field in ("byteBudget", "maxCheckpoints", "checkpointCount", "allocatedBytes", "sharedBytes",
                  "builds", "hits", "misses", "invalidations", "rejected"):
        if not isinstance(status.get(field), int) or status[field] < 0:
            raise ValueError("Invalid checkpoint counter: " + field)
    return status


def status_issues(status, disabled=False):
    errors = []
    if (0 in status.get("events", [])) != (status.get("hasInitialCheckpoint") is True):
        errors.append("event zero requires an explicitly retained initial checkpoint")
    if status["allocatedBytes"] > status["byteBudget"]:
        errors.append("checkpoint allocation exceeds configured budget")
    if status["checkpointCount"] > status["maxCheckpoints"]:
        errors.append("checkpoint count exceeds configured limit")
    if disabled and (status.get("enabled") or status["checkpointCount"] or status["allocatedBytes"]
                     or status["sharedBytes"] or status.get("events")):
        errors.append("disable did not release checkpoint storage")
    return errors


def force_bypass_issues(before, after):
    # Hits/builds and retained versions must stay unchanged even when the same EID is forced twice.
    return ["forced replay changed " + key for key in
            ("hits", "builds", "checkpointCount", "allocatedBytes", "sharedBytes", "events",
             "hasInitialCheckpoint")
            if before.get(key) != after.get(key)]


def compare_event(observed, canonical):
    if len(canonical) != 2:
        return "incomplete"
    if canonical[0] != canonical[1]:
        return "inconclusiveCanonicalUnstable"
    if not observed:
        return "incomplete"
    return "passed" if all(value == canonical[0] for value in observed) else "failedStableCanonicalMismatch"


def shader_encoding_names(values, enum_type):
    # Native rdcarray<ShaderEncoding> bindings may expose Python ints rather than enum wrappers.
    # Compare actual enum constants; their numerical values are owned by the loaded RenderDoc API.
    known = [(name, getattr(enum_type, name)) for name in
             ("Unknown", "DXBC", "HLSL", "GLSL", "SPIRV", "OpenGLSPIRV", "DXIL")
             if hasattr(enum_type, name)]
    return [next((name for name, constant in known if value == constant),
                 "Unknown(%s)" % str(value)) for value in values]


def glsl_fragment_fixture(evidence):
    """Accept only an evidenced float4 RT0 interface; never infer an unknown capture ABI."""
    if evidence.get("api") not in ("OpenGL", "Vulkan") or "GLSL" not in evidence.get("encodings", []):
        raise ValueError("GL/Vulkan mutation requires an advertised GLSL target encoding")
    if evidence.get("stage") not in ("Pixel", "Fragment") or evidence.get("entryPoint") != "main":
        raise ValueError("GL/Vulkan mutation requires a reflected main fragment entry point")
    outputs = evidence.get("outputSignature", [])
    if len(outputs) != 1:
        raise ValueError("GL/Vulkan mutation requires exactly one reflected color output")
    output = outputs[0]
    if (output.get("systemValue") != "ColorOutput" or output.get("regIndex") != 0 or
            output.get("varType") != "Float" or output.get("compCount") != 4 or
            output.get("regChannelMask") != 15 or output.get("stream") != 0 or
            output.get("perPrimitiveRate")):
        raise ValueError("GL/Vulkan mutation requires an unpacked float4 color output at location 0")
    targets = evidence.get("boundColorTargets", [])
    if (len(targets) != 1 or targets[0].get("slot") != 0 or
            targets[0].get("componentType") not in ("Float", "UNorm", "UNormSRGB", "SNorm")):
        raise ValueError("GL/Vulkan mutation requires exactly one float-compatible bound RT0")
    # Desktop GLSL 330 supports explicit fragment output locations. Vulkan GLSL uses 450.
    version = "330 core" if evidence["api"] == "OpenGL" else "450"
    return ("#version " + version + "\nlayout(location = 0) out vec4 checkpointColor;\n"
            "void main() { checkpointColor = vec4(0.19, 0.73, 0.41, 1.0); }\n")


def output_subresources(texture, binding):
    def invalid_range(kind):
        details = {
            "binding": {name: str(getattr(binding, name, "unavailable")) for name in
                        ("resource", "firstMip", "numMips", "firstSlice", "numSlices")},
            "texture": {name: str(getattr(texture, name, "unavailable")) for name in
                        ("resourceId", "type", "width", "height", "depth", "mips", "arraysize", "msSamp")}}
        return ValueError("Invalid bound output " + kind + " range: " + json.dumps(details, sort_keys=True))

    first_mip, mip_count = int(binding.firstMip), int(binding.numMips)
    if first_mip >= int(texture.mips) or mip_count < 1:
        raise invalid_range("mip")
    mip_count = min(mip_count, int(texture.mips) - first_mip)
    result = []
    for mip in range(first_mip, first_mip + mip_count):
        if int(texture.depth) > 1:
            # Public GetTextureData returns the whole 3D mip volume; the view slice is retained
            # separately in the binding witness, never silently represented as one depth pixel.
            slices = [0]
        else:
            first_slice, count = int(binding.firstSlice), int(binding.numSlices)
            if first_slice >= int(texture.arraysize) or count < 1:
                raise invalid_range("slice")
            slices = range(first_slice, min(int(texture.arraysize), first_slice + count))
        for slice_index in slices:
            for sample in range(max(1, int(texture.msSamp))):
                result.append((mip, slice_index, sample))
    return result


def resolve_extra_textures(names, resources, textures):
    """Resolve explicit whole-resource witnesses once; ambiguous/missing names cannot be skipped."""
    if len(names) > 16 or len(set(names)) != len(names) or any(not name for name in names):
        raise ValueError("Provide at most sixteen distinct nonempty extra texture names")
    result = []
    for name in names:
        named = [r for r in resources if str(r.name) == name]
        if len(named) != 1:
            raise ValueError("Extra texture name is missing or ambiguous: " + name)
        matches = [t for t in textures if t.resourceId == named[0].resourceId]
        if len(matches) != 1:
            raise ValueError("Extra resource is not exactly one captured texture: " + name)
        texture = matches[0]
        if any(int(getattr(texture, key)) < 1 for key in
               ("width", "height", "depth", "mips", "arraysize")):
            raise ValueError("Extra texture has invalid native dimensions: " + name)
        result.append((name, texture))
    return result


def resolve_extra_buffers(names, resources, buffers):
    if len(names) > 16 or len(set(names)) != len(names) or any(not name for name in names):
        raise ValueError("Provide at most sixteen distinct nonempty extra buffer names")
    result = []
    for name in names:
        named = [r for r in resources if str(r.name) == name]
        if len(named) != 1:
            raise ValueError("Extra buffer name is missing or ambiguous: " + name)
        matches = [b for b in buffers if b.resourceId == named[0].resourceId]
        if len(matches) != 1 or not 0 < int(matches[0].length) <= (1 << 64) - 1:
            raise ValueError("Extra resource is not exactly one bounded captured buffer: " + name)
        result.append((name, matches[0]))
    return result


def full_buffer_witness(controller, buffers, budget):
    total = sum(int(buffer.length) for _, buffer in buffers)
    if total > budget:
        raise RuntimeError("Every extra buffer must fit the remaining read budget; none may be omitted")
    result = []
    for name, buffer in buffers:
        raw = bytes(controller.GetBufferData(buffer.resourceId, 0, int(buffer.length)))
        if len(raw) != int(buffer.length):
            raise RuntimeError("Incomplete full-buffer readback: " + name)
        result.append(dict(name=name, resource=str(buffer.resourceId), bytes=len(raw),
                           sha256=hashlib.sha256(raw).hexdigest()))
    return result


def selected_event_evidence(controller, events):
    """Read existing structured metadata only; do not infer an API EID from its nearest draw."""
    structured = controller.GetStructuredFile()
    wanted = set(events)
    found = {}

    def describe(api_event):
        index = int(api_event.chunkIndex)
        if index < 0 or index >= len(structured.chunks):
            raise RuntimeError("Selected API event has an invalid structured chunk index")
        chunk = structured.chunks[index]
        return {"eventId": int(api_event.eventId), "chunkIndex": index,
                "chunkId": int(chunk.metadata.chunkID), "chunkName": str(chunk.name),
                "fileOffset": int(api_event.fileOffset)}

    def walk(actions):
        for action in actions:
            action_events = list(action.events)
            selected = [value for value in action_events if int(value.eventId) in wanted]
            for value in selected:
                row = describe(value)
                row.update(status="exactAPIEvent", owningActionEventId=int(action.eventId),
                           owningActionFlags=str(action.flags),
                           # These real API events expose e.g. BeginRenderPass -> Bind -> Draw;
                           # their names are evidence, not a guessed semantic pass classification.
                           owningActionContext=[describe(item) for item in action_events[-32:]],
                           owningActionContextTruncated=len(action_events) > 32)
                previous = found.get(int(value.eventId))
                if previous and previous["chunkIndex"] != row["chunkIndex"]:
                    raise RuntimeError("Selected EID maps to conflicting structured chunks")
                found[int(value.eventId)] = row
            walk(action.children)

    walk(controller.GetRootActions())
    return [found.get(event, {"eventId": event, "status": "noExactAPIEvent"}) for event in events]


def main():
    env = lambda name, default="": os.environ.get("RENDERDOC_CHECKPOINT_PROFILE_" + name, default)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", default=env("CAPTURE"))
    parser.add_argument("--events", default=env("EVENTS"))
    parser.add_argument("--output", default=env("OUTPUT"))
    parser.add_argument("--budget-mib", type=int, default=0, help="0 selects automatic capture-size budget")
    parser.add_argument("--max-checkpoints", type=int, default=0)
    parser.add_argument("--max-read-mib", type=int, default=512)
    parser.add_argument("--extra-texture", action="append", default=[],
                        help="Also validate every subresource of this exact named texture at each EID")
    parser.add_argument("--extra-buffer", action="append", default=[],
                        help="Also validate every byte of this exact named buffer at each EID")
    parser.add_argument("--min-hits", type=int, default=1)
    parser.add_argument("--min-full-clear-restores", type=int, default=0,
                        help="Require actual checkpoint restores that omit a proven full-clear resource")
    parser.add_argument("--steady-rounds", type=int, default=0,
                        help="Additional alternating normal visits with checkpoints enabled and disabled")
    parser.add_argument("--verify-reconfigure", action="store_true",
                        help="Require repeated identical configuration to retain live checkpoints and counters")
    parser.add_argument("--validate-mutations", action="store_true",
                        help="Validate a temporary global pixel shader replacement on D3D11/D3D12 or evidenced GL/Vulkan RT0 fixtures; D3D11 also tests event CBuffer set/clear")
    parser.add_argument("--max-seconds", type=int, default=300)
    parser.add_argument("--build-label", default=env("BUILD_LABEL", "unspecified"))
    args = parser.parse_args()
    try:
        events = parse_events(args.events)
    except ValueError as error:
        parser.error(str(error))
    if (not args.capture or not args.output or args.budget_mib < 0 or
            not 0 <= args.max_checkpoints <= 64 or args.max_read_mib <= 0 or
            args.min_hits < 0 or args.min_full_clear_restores < 0 or args.max_seconds <= 0 or
            not 0 <= args.steady_rounds <= 32):
        parser.error("Capture/output and valid positive limits are required")
    capture, output = Path(args.capture).resolve(), Path(args.output).resolve()
    if not capture.is_file() or output.exists():
        parser.error("Capture must exist and output must be a new evidence path")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"schema": "renderdoc-checkpoint-profile/v1", "ok": False, "samples": [],
              "capture": str(capture), "captureBytes": capture.stat().st_size, "events": events,
              "buildLabel": args.build_label, "processId": os.getpid(), "visits": [],
              "replayEnvironment": {name: os.environ.get(name) for name in (
                  "RENDERDOC_D3D12_DISABLE_ACTIVE_WRITE_STAGES",
                  "RENDERDOC_D3D12_ENABLE_UAV_DECLARATION_RANGES",
                  "RENDERDOC_D3D12_ENABLE_SPARSE_INITIAL_RESIDENCY",
                  "RENDERDOC_D3D12_DISABLE_CHECKPOINT_RESIDENT_REUSE",
                  "RENDERDOC_D3D12_DISABLE_CHECKPOINT_INITIAL_SOURCES",
                  "RENDERDOC_D3D12_ENABLE_CHECKPOINT_DESCRIPTOR_OVERLAY",
                  "RENDERDOC_D3D12_CHECKPOINT_FULL_CLEAR_PREFIX",
                  "RENDERDOC_D3D12_CHECKPOINT_FAIL_AFTER_INITIAL",
                  "RENDERDOC_D3D12_INITIAL_SOURCE_CACHE_ORDER",
                   "RENDERDOC_GL_DISABLE_STATIC_CHECKPOINT_SHARING",
                   "RENDERDOC_GL_DISABLE_CHECKPOINT_TEXTURE_VERSIONS",
                   "RENDERDOC_VULKAN_DISABLE_CHECKPOINT_CONTENT_REUSE",
                   "RENDERDOC_VULKAN_DISABLE_CHECKPOINT_RESIDENT_SKIP",
                   "RENDERDOC_D3D12_DISABLE_CHECKPOINT_TYPED_ALIASES",
                   "RENDERDOC_D3D12_PRESERVE_CAPTURED_UPLOAD_HEAPS",
                   "RENDERDOC_D3D12_DISABLE_CPU_WRITE_DIRTY_RANGES")},
              "configuration": vars(args), "issues": [], "eventComparisons": [],
              "coverage": "Full native pixels of every bound color/depth output view (all view mips, "
                          "array slices and samples; whole volume for 3D), plus pipeline binding witness. "
                          "Explicit extra textures cover all subresources; explicit extra buffers cover every byte. "
                          "Other unrequested UAVs/buffers are not covered; shared-resource edits use separate probes.",
              "forceMeaning": "SetFrameEvent(force=True) bypasses checkpoint save/restore; never use it for hit measurements",
              "mutationCoverage": "Separate event-CBuffer fixture remains available; no shared edit is fabricated"}
    recorder = Recorder(report, output, output.with_suffix(".rdoc.log"), args.max_seconds)
    recorder.publish()
    os.environ["RENDERDOC_EID_TIMING"] = "1"
    import renderdoc as rd
    # Record the actually loaded modules, not only an operator-supplied build label.
    # Import here because the shared-buffer probe also reuses our comparison helpers.
    from eid_shared_buffer_profile import build_identity, readback_replay_issues
    report["buildIdentity"] = build_identity(rd, None)
    capture_file, controller = None, None
    extra_textures = []
    extra_buffers = []
    rd.InitialiseReplay(rd.GlobalEnvironment(), [])

    def status():
        value = parse_status(controller.GetReplayCheckpointStatus())
        report["issues"].extend(status_issues(value))
        return value

    def configure(enabled):
        controller.ConfigureReplayCheckpoints(enabled, args.budget_mib * 1024 * 1024, args.max_checkpoints)
        value = status()
        report["issues"].extend(status_issues(value, disabled=not enabled))
        return value

    def output_witness():
        pipe = controller.GetPipelineState()
        textures = {str(t.resourceId): t for t in controller.GetTextures()}
        witness = {"eventId": int(controller.GetCurrentEventID()), "topology": str(pipe.GetPrimitiveTopology()),
                   "shaders": {}, "bindings": [], "pixels": []}
        # Keep a partial witness if metadata validation or a native readback fails mid-output.
        report["lastOutputWitness"] = witness
        for name in ("Vertex", "Hull", "Domain", "Geometry", "Pixel", "Compute", "Task", "Mesh"):
            stage = getattr(rd.ShaderStage, name, None)
            if stage is not None:
                witness["shaders"][name] = str(pipe.GetShader(stage))
        targets = [("color", slot, binding) for slot, binding in enumerate(pipe.GetOutputTargets())]
        targets.append(("depth", 0, pipe.GetDepthTarget()))
        requests, unique = [], set()
        budget = args.max_read_mib * 1024 * 1024
        estimated = 0
        for kind, slot, binding in targets:
            descriptor = {"kind": kind, "slot": slot, "resource": str(binding.resource),
                          "firstMip": int(binding.firstMip), "numMips": int(binding.numMips),
                          "firstSlice": int(binding.firstSlice), "numSlices": int(binding.numSlices),
                          "format": binding.format.Name()}
            witness["bindings"].append(descriptor)
            if resource_number(binding.resource) in (None, 0):
                continue
            texture = textures.get(str(binding.resource))
            if texture is None:
                raise RuntimeError("Bound output is absent from texture descriptions: " + json.dumps(descriptor))
            descriptor["texture"] = {name: str(getattr(texture, name)) for name in
                                     ("resourceId", "type", "width", "height", "depth", "mips", "arraysize", "msSamp")}
            for sub in output_subresources(texture, binding):
                key = (str(binding.resource),) + sub
                if key in unique:
                    continue
                unique.add(key)
                # Conservative allocation budget; GetTextureData may expose padded/native planes.
                mip = sub[0]
                estimated += max(1, int(texture.width) >> mip) * max(1, int(texture.height) >> mip) * max(1, int(texture.depth) >> mip) * 16
                requests.append((key, texture.resourceId, sub))
        if extra_textures:
            witness["extraTextures"] = []
        for name, texture in extra_textures:
            witness["extraTextures"].append({"name": name, "resource": str(texture.resourceId),
                                               "format": texture.format.Name(),
                                               "mips": int(texture.mips), "slices": int(texture.arraysize),
                                               "samples": int(texture.msSamp)})
            whole = SimpleNamespace(firstMip=0, numMips=int(texture.mips), firstSlice=0,
                                    numSlices=int(texture.arraysize))
            for sub in output_subresources(texture, whole):
                key = (str(texture.resourceId),) + sub
                if key in unique:
                    continue
                unique.add(key)
                mip = sub[0]
                estimated += max(1, int(texture.width) >> mip) * max(1, int(texture.height) >> mip) * max(1, int(texture.depth) >> mip) * 16
                requests.append((key, texture.resourceId, sub))
        if not requests:
            raise RuntimeError("No bound color or depth output; choose render actions for pixel validation")
        if estimated + sum(int(buffer.length) for _, buffer in extra_buffers) > budget:
            raise RuntimeError("Every bound output must fit max-read-mib; no output is silently omitted")
        actual = 0
        for key, resource, sub in requests:
            raw = bytes(controller.GetTextureData(resource, rd.Subresource(*sub)))
            if not raw:
                raise RuntimeError("Empty native output readback")
            actual += len(raw)
            if actual > budget:
                raise RuntimeError("Native output readback exceeded max-read-mib")
            witness["pixels"].append({"resource": key[0], "mip": sub[0], "slice": sub[1], "sample": sub[2],
                                      "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        if extra_buffers:
            witness["extraBuffers"] = full_buffer_witness(controller, extra_buffers, budget - actual)
        fatal = controller.GetFatalErrorStatus()
        if fatal.code != rd.ResultCode.Succeeded:
            raise RuntimeError("Native replay failed: " + str(fatal))
        return witness

    def visit(phase, event, force=False):
        before = status()
        _, timing = recorder.measure(phase + ".replay", lambda: controller.SetFrameEvent(event, force), event)
        after = status()
        if force:
            report["issues"].extend(force_bypass_issues(before, after))
        pixels, readback = recorder.measure(phase + ".nativeOutputHash", output_witness, event)
        # A getter must not repair a stale native output after the measured EID request.
        report["issues"].extend(phase + ".readback: " + issue for issue in
                                readback_replay_issues(readback["timingRecords"]))
        after_readback = status()
        if int(pixels["eventId"]) != event:
            report["issues"].append("effective controller EID differs from requested EID " + str(event))
        row = {"phase": phase, "eventId": event, "force": force, "before": before, "after": after,
               "afterReadback": after_readback, "replayWallMs": timing["wallMs"],
               "readbackWallMs": readback["wallMs"], "witness": pixels}
        report["visits"].append(row)
        recorder.publish()
        return row

    def validate_mutations():
        api = controller.GetAPIProperties().pipelineType
        if api not in (rd.GraphicsAPI.D3D11, rd.GraphicsAPI.D3D12, rd.GraphicsAPI.OpenGL, rd.GraphicsAPI.Vulkan):
            raise RuntimeError("Mutation fixture requires D3D11, D3D12, OpenGL or Vulkan")
        event = events[-1]
        originals = [visit("mutation.original", event, True)["witness"] for _ in range(2)]
        if compare_event([originals[0]], originals) != "passed":
            raise RuntimeError("Mutation fixture original forced references are unstable")
        original_pixels = originals[0]["pixels"]
        pipe = controller.GetPipelineState()
        original = pipe.GetShader(rd.ShaderStage.Pixel)
        if original == rd.ResourceId.Null():
            raise RuntimeError("Mutation EID must have a pixel shader")
        is_d3d11 = api == rd.GraphicsAPI.D3D11
        enum_name = lambda value: str(value).rsplit(".", 1)[-1]
        encodings = list(controller.GetTargetShaderEncodings())
        mutation = {"eventId": event, "checks": [], "api": enum_name(api),
                    "originalShader": str(original),
                    "targetEncodings": shader_encoding_names(encodings, rd.ShaderEncoding),
                    "targetEncodingValues": [int(value) for value in encodings],
                    "cbufferScope": "event-local fixture, not the future shared resource patch API"}
        report["mutations"] = mutation
        encoding = rd.ShaderEncoding.HLSL
        source = ("cbuffer CheckpointProbe : register(b13) { float4 checkpointColor; };\n"
                  "float4 main() : SV_Target0 { return checkpointColor; }" if is_d3d11 else
                  "float4 main() : SV_Target0 { return float4(0.19, 0.73, 0.41, 1.0); }")
        if api in (rd.GraphicsAPI.OpenGL, rd.GraphicsAPI.Vulkan):
            reflection = pipe.GetShaderReflection(rd.ShaderStage.Pixel)
            evidence = {"api": enum_name(api), "encodings": mutation["targetEncodings"],
                        "stage": enum_name(reflection.stage) if reflection else None,
                        "entryPoint": str(reflection.entryPoint) if reflection else None,
                        "outputSignature": [], "boundColorTargets": []}
            mutation["fixtureEvidence"] = evidence
            if reflection:
                for sig in reflection.outputSignature:
                    evidence["outputSignature"].append({
                        "varName": str(sig.varName), "systemValue": enum_name(sig.systemValue),
                        "regIndex": int(sig.regIndex), "varType": enum_name(sig.varType),
                        "compCount": int(sig.compCount), "regChannelMask": int(sig.regChannelMask),
                        "channelUsedMask": int(sig.channelUsedMask), "stream": int(sig.stream),
                        "perPrimitiveRate": bool(sig.perPrimitiveRate)})
            for slot, target in enumerate(pipe.GetOutputTargets()):
                if target.resource != rd.ResourceId.Null():
                    evidence["boundColorTargets"].append({"slot": slot, "resource": str(target.resource),
                                                          "format": target.format.Name(),
                                                          "componentType": enum_name(target.format.compType)})
            source = glsl_fragment_fixture(evidence)
            encoding = rd.ShaderEncoding.GLSL
        if encoding not in encodings:
            raise RuntimeError("Mutation target encoding is not advertised by this replay backend")
        mutation.update(sourceEncoding=shader_encoding_names([encoding], rd.ShaderEncoding)[0], source=source)
        shader, errors = controller.BuildTargetShader("main", encoding,
                                                      source.encode("utf-8"), rd.ShaderCompileFlags(),
                                                      rd.ShaderStage.Pixel)
        mutation["shaderErrors"] = str(errors)
        if shader == rd.ResourceId.Null():
            raise RuntimeError("Mutation fixture shader compilation failed: " + str(errors))
        mutation["replacementShader"] = str(shader)

        def check_pair(name):
            # Move away first so force=false cannot return an already-materialized forced result.
            start = status()
            observed, hits = [], 0
            for _ in range(2):
                controller.SetFrameEvent(events[0], False)
                row = visit(name, event)
                observed.append(row["witness"])
                hits += row["after"]["hits"] - row["before"]["hits"]
            after = status()
            reference = [visit(name + ".canonical", event, True)["witness"] for _ in range(2)]
            verdict = compare_event(observed, reference)
            mutation["checks"].append({"phase": name, "status": verdict, "observedHits": hits,
                                        "navigationHits": after["hits"] - start["hits"] - hits,
                                        "checkpointCoverage": "restored" if hits else "canonicalFallbackOnly"})
            if verdict != "passed":
                report["issues"].append("mutation output mismatch: " + name)
            if not hits:
                report["issues"].append("mutation checkpoint reuse was not observed: " + name)
            return observed[-1]["pixels"]

        override_installed = False
        replaced = False
        try:
            mutation["beforeReplace"] = status()
            replaced = True
            controller.ReplaceResource(original, shader)
            mutation["afterReplace"] = status()
            if mutation["afterReplace"]["invalidations"] <= mutation["beforeReplace"]["invalidations"]:
                report["issues"].append("global shader replacement did not invalidate checkpoints")
            if mutation["afterReplace"]["checkpointCount"]:
                report["issues"].append("global shader replacement retained old checkpoints")
            changed_pixels = check_pair("mutation.shaderReplaced")
            if changed_pixels == original_pixels:
                report["issues"].append("fixture replacement did not visibly change output")
            if is_d3d11:
                # expectedBuffer=Null enforces that fixture b13 is unbound; the native setter
                # rejects a capture with an existing binding instead of overwriting it.
                error = controller.SetEventCBufferOverride(
                    event, rd.ShaderStage.Pixel, 13, original, rd.ResourceId.Null(), 0, 0,
                    struct.pack("<4f", 0.82, 0.12, 0.31, 1.0), 0)
                if error:
                    raise RuntimeError("CBuffer mutation fixture rejected: " + str(error))
                override_installed = True
                mutation["afterCBufferSet"] = status()
                if any(saved >= event for saved in mutation["afterCBufferSet"].get("events", [])):
                    report["issues"].append("event CBuffer edit retained a dependent suffix checkpoint")
                patched_pixels = check_pair("mutation.cbufferSet")
                if patched_pixels == changed_pixels:
                    report["issues"].append("fixture CBuffer patch did not visibly change output")
                controller.ClearEventCBufferOverride(event, rd.ShaderStage.Pixel, 13, 0)
                override_installed = False
                if check_pair("mutation.cbufferCleared") != changed_pixels:
                    report["issues"].append("CBuffer clear did not recover the replaced shader result")
            mutation["beforeRemoveReplacement"] = status()
            controller.RemoveReplacement(original)
            replaced = False
            mutation["afterRemoveReplacement"] = status()
            if mutation["afterRemoveReplacement"]["invalidations"] <= mutation["beforeRemoveReplacement"]["invalidations"]:
                report["issues"].append("shader removal did not invalidate checkpoints")
            if mutation["afterRemoveReplacement"]["checkpointCount"]:
                report["issues"].append("shader removal retained old checkpoints")
            if check_pair("mutation.shaderRestored") != original_pixels:
                report["issues"].append("removing the shader replacement did not recover original pixels")
        finally:
            cleanups = []
            if override_installed:
                cleanups.append(("clearCBuffer", lambda: controller.ClearEventCBufferOverride(event, rd.ShaderStage.Pixel, 13, 0)))
            if replaced:
                cleanups.append(("removeReplacement", lambda: controller.RemoveReplacement(original)))
            cleanups.append(("freeTargetShader", lambda: controller.FreeTargetResource(shader)))
            for name, operation in cleanups:
                try:
                    operation()
                except Exception as error:
                    mutation.setdefault("cleanupErrors", []).append({"operation": name, "error": str(error)})
                    report["issues"].append("mutation cleanup failed: " + name)
        recorder.publish()

    try:
        rd.SetDebugLogFile(str(output.with_suffix(".rdoc.log")))
        capture_file = rd.OpenCaptureFile()
        result, _ = recorder.measure("capture.openFile", lambda: capture_file.OpenFile(str(capture), "", None))
        if result != rd.ResultCode.Succeeded:
            raise RuntimeError("OpenFile failed: " + str(result))
        report["captureDriver"] = capture_file.DriverName()
        opened, _ = recorder.measure("capture.openReplay", lambda: capture_file.OpenCapture(rd.ReplayOptions(), None))
        result, controller = opened
        if result != rd.ResultCode.Succeeded or controller is None:
            raise RuntimeError("OpenCapture failed: " + str(result))
        extra_textures = resolve_extra_textures(args.extra_texture, controller.GetResources(),
                                                controller.GetTextures())
        extra_buffers = resolve_extra_buffers(args.extra_buffer, controller.GetResources(),
                                               controller.GetBuffers())
        report["extraBufferResources"] = [dict(name=name, resource=str(buffer.resourceId),
                                                bytes=int(buffer.length)) for name, buffer in extra_buffers]
        report["extraTextureResources"] = [{"name": name, "resource": str(texture.resourceId)}
                                             for name, texture in extra_textures]
        report["selectedEventEvidence"], _ = recorder.measure(
            "capture.selectedEventEvidence", lambda: selected_event_evidence(controller, events))
        if any(row["status"] != "exactAPIEvent" for row in report["selectedEventEvidence"]):
            report["issues"].append("one or more selected EIDs have no exact structured API-event evidence")
        report["initialStatus"] = status()
        report["initialDisable"] = configure(False)
        report["enabledStatus"] = configure(True)
        start_hits = report["enabledStatus"]["hits"]
        for phase, sequence in (("forward", events), ("reverse", events[-2::-1]),
                                ("forwardAgain", events[1:])):
            for event in sequence:
                visit(phase, event)
        for repeat in range(args.steady_rounds):
            for event in events:
                row = visit("steadyEnabled", event)
                row["repeat"] = repeat
        report["checkpointPhaseStatus"] = status()
        if args.verify_reconfigure:
            previous = report["checkpointPhaseStatus"]
            repeated = configure(True)
            report["repeatedConfiguration"] = {"before": previous, "after": repeated}
            for key in ("checkpointCount", "allocatedBytes", "sharedBytes", "builds", "hits",
                        "misses", "invalidations", "events", "hasInitialCheckpoint"):
                if previous.get(key) != repeated.get(key):
                    report["issues"].append("identical configuration discarded state: " + key)
            if previous["checkpointCount"] == 0:
                report["issues"].append("configuration retention requires a live checkpoint")
        report["observedHits"] = report["checkpointPhaseStatus"]["hits"] - start_hits
        if report["observedHits"] < args.min_hits:
            report["issues"].append("required checkpoint hits were not observed")
        report["fullClearRestoreCount"] = sum(
            1 for sample in report["samples"] for row in sample.get("timingRecords", [])
            if row.get("api") == "D3D12Checkpoint" and row.get("op") == "restore"
            and row.get("fullClearInitialCopiesDeferred", 0) > 0)
        if report["fullClearRestoreCount"] < args.min_full_clear_restores:
            report["issues"].append("required native full-clear copy omissions were not observed")
        for event in events:
            canonical = [visit("canonical", event, True)["witness"] for _ in range(2)]
            observed = [v["witness"] for v in report["visits"] if v["eventId"] == event and not v["force"]]
            verdict = compare_event(observed, canonical)
            report["eventComparisons"].append({"eventId": event, "status": verdict,
                                                "checkpointVisits": len(observed)})
        report["disabledStatus"] = configure(False)
        for repeat in range(args.steady_rounds):
            for event in events:
                row = visit("steadyDisabled", event)
                row["repeat"] = repeat
                reference = [v["witness"] for v in report["visits"]
                             if v["phase"] == "canonical" and v["eventId"] == event]
                report["eventComparisons"].append({"eventId": event, "phase": "steadyDisabled",
                                                   "status": compare_event([row["witness"]], reference)})
        report["reenabledStatus"] = configure(True)
        for event in events:
            row = visit("reenabled", event)
            reference = [v["witness"] for v in report["visits"] if v["phase"] == "canonical" and v["eventId"] == event]
            report["eventComparisons"].append({"eventId": event, "phase": "reenabled",
                                                "status": compare_event([row["witness"]], reference)})
        report["reenabledFinalStatus"] = status()
        if args.validate_mutations:
            validate_mutations()
        controller.ConfigureReplayCheckpoints(True, 1, 1)
        report["tinyBudgetInitialStatus"] = status()
        for event in events:
            row = visit("tinyBudget", event)
            reference = [v["witness"] for v in report["visits"]
                         if v["phase"] == "canonical" and v["eventId"] == event]
            report["eventComparisons"].append({"eventId": event, "phase": "tinyBudget",
                                                "status": compare_event([row["witness"]], reference)})
        report["tinyBudgetFinalStatus"] = status()
        if report["tinyBudgetFinalStatus"]["checkpointCount"] or report["tinyBudgetFinalStatus"]["allocatedBytes"]:
            report["issues"].append("one-byte budget retained checkpoint state")
        report["finalDisabledStatus"] = configure(False)
        report["measurementComplete"] = True
        report["ok"] = not report["issues"] and all(c["status"] == "passed" for c in report["eventComparisons"])
        report["verdict"] = ("passed" if report["ok"] and report["observedHits"] else
                             "canonicalPassedWithoutCheckpointReuse" if report["ok"] else "failedOrInconclusive")
    except Exception as error:
        report.update(error=str(error), traceback=traceback.format_exc())
    finally:
        def cleanup(phase, operation):
            try:
                return recorder.measure(phase, operation, cleanup=True)[0]
            except Exception as error:
                report.setdefault("cleanupErrors", []).append(
                    {"phase": phase, "error": str(error), "traceback": traceback.format_exc()})
                report["ok"] = False
                report["verdict"] = "cleanupFailed"
                return None

        if controller is not None:
            report["cleanupDisabledStatus"] = cleanup("cleanup.disable", lambda: configure(False))
        # ShutdownReplay removes the diagnostic log. Preserve it before either native shutdown,
        # then refresh after controller cleanup to retain destruction diagnostics as well.
        def preserve_native_log():
            if recorder.log_path.is_file():
                native_log = output.with_suffix(".native.log")
                native_log.write_bytes(recorder.log_path.read_bytes())
                report["nativeLog"] = str(native_log)
                report["nativeLogBytes"] = native_log.stat().st_size
            else:
                report.setdefault("nativeLogUnavailable", str(recorder.log_path))

        cleanup("cleanup.preserveNativeLog", preserve_native_log)
        if controller is not None:
            cleanup("cleanup.replay", controller.Shutdown)
        if capture_file is not None:
            cleanup("cleanup.captureFile", capture_file.Shutdown)
        cleanup("cleanup.refreshNativeLog", preserve_native_log)
        cleanup("cleanup.runtime", rd.ShutdownReplay)
        recorder.publish()
    print(json.dumps({"ok": report["ok"], "output": str(output), "verdict": report.get("verdict"),
                      "observedHits": report.get("observedHits")}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
