"""Real GPU acceptance for generated Shader value previews (Python 3.6).

Run in a NEW standalone replay process using matching renderdoc.pyd, the bundled
replay Python launcher, or qrenderdoc --python. No desktop automation is used.
Input is --capture-results from util/perf/capture_api_smoke.py, or --capture RDC.
Environment equivalents: BREAD_SHADER_VISUAL_SMOKE_INPUT, _CAPTURE, _OUTPUT.

For each capture this compiles a known Shader and its generated value probe,
verifies actual sampled RT values, proves a sibling Draw is unchanged, then
restores a preexisting event override and checks its exact texture-byte hash.
Manifest entries may set priorScope="global" to test an existing global Shader
replacement instead, including restoration of the captured output after cleanup.
targetComponents/targetType select actual narrow or integer attachments; probeType
can additionally test signed or Boolean values converted into a floating target.
drawMarker selects draws following a named marker in larger acceptance captures.
independentFloatOutput tests HDR/negative RGBA32F storage, private attachments,
native mode switching and cache reuse. priorIndependentFloatOutput also verifies
restoration of a preexisting independent preview. Captured attachments are checked
against their contents before this Draw, since the Draw is redirected to scratch.
It always restores the original override and frees only its own target Shaders.
It does not certify platforms absent from the supplied capture manifest.
"""
from __future__ import print_function

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import traceback

import renderdoc as rd


def _instrumenter():
    path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "shader_visual_instrument.py"
    spec = importlib.util.spec_from_file_location("_bread_visual_gpu_instrument", str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _leaves(actions):
    for action in actions:
        if action.children:
            for child in _leaves(action.children):
                yield child
        elif action.flags & rd.ActionFlags.Drawcall:
            yield action


def _marked_draws(actions, marker):
    previous = ""
    for action in actions:
        if action.children:
            for child in _marked_draws(action.children, marker):
                yield child
        elif action.flags & rd.ActionFlags.SetMarker:
            previous = str(action.customName)
        elif action.flags & rd.ActionFlags.Drawcall and previous == marker:
            yield action


def _null(resource):
    return resource == rd.ResourceId.Null()


def _target_format(target, textures):
    # Some APIs omit the view format when the attachment inherits its texture's
    # format. The generic descriptor then has compCount=0.
    return target.format if int(target.format.compCount) else textures[target.resource].format


def _snapshot(controller, event, target_index=None):
    controller.SetFrameEvent(int(event), True)
    pipeline = controller.GetPipelineState()
    textures = {texture.resourceId: texture for texture in controller.GetTextures()}
    records = []
    for index, target in enumerate(pipeline.GetOutputTargets()):
        if target_index is not None and index != target_index:
            continue
        if _null(target.resource) or target.resource not in textures:
            continue
        sub = rd.Subresource(max(0, int(target.firstMip)), max(0, int(target.firstSlice)), 0)
        raw = bytes(controller.GetTextureData(target.resource, sub))
        if not raw:
            raise RuntimeError("Empty render-target readback at EID %d" % event)
        texture = textures[target.resource]
        fmt = _target_format(target, textures)
        records.append({"index": index, "resource": target.resource, "sub": sub,
                        "width": max(1, int(texture.width) >> sub.mip),
                        "height": max(1, int(texture.height) >> sub.mip),
                        "format": str(fmt.Name()), "componentType": fmt.compType,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "bytes": len(raw)})
    return records


def _hashes(snapshot):
    return [{"index": x["index"], "resource": str(x["resource"]), "format": x["format"],
             "sha256": x["sha256"], "bytes": x["bytes"]} for x in snapshot]


def _source(api, gles, index, components=4, component_type="float", probe_type=None):
    probe_type = probe_type or component_type
    base = {"float": "float", "uint": "uint", "sint": "int"}[component_type]
    probe_base = {"float": "float", "uint": "uint", "sint": "int", "bool": "bool"}[probe_type]
    first = {"float": "probe + 0.5", "uint": "probe + 3u", "sint": "probe + 17",
             "bool": base + "(probe) + 9"}[probe_type]
    values = ([first, "0.5", "0.125", "1.0"] if probe_type == "float" else
              [first, "5", "2", "1"])
    initial = {"float": "0.25", "uint": "7u", "sint": "-7", "bool": "true"}[probe_type]
    values = ", ".join(values[:components])
    if api in (rd.GraphicsAPI.D3D11, rd.GraphicsAPI.D3D12):
        output = base + (str(components) if components > 1 else "")
        return ("hlsl", "%s main() : SV_Target%d { %s probe = %s; return %s(%s); }" %
                (output, index, probe_base, initial, output, values))
    prefix = "#version 450\n" if api == rd.GraphicsAPI.Vulkan else "#version 320 es\nprecision highp float;\nprecision highp int;\n" if gles else "#version 330 core\n"
    output = (base if components == 1 else
              {"float": "vec", "uint": "uvec", "sint": "ivec"}[component_type] + str(components))
    return ("glsl", prefix + "layout(location=%d) out %s color;\n" % (index, output) +
            "void main() { %s probe = %s; color = %s(%s); }\n" % (probe_base, initial, output, values))


def _build(controller, language, source):
    encoding = rd.ShaderEncoding.HLSL if language == "hlsl" else rd.ShaderEncoding.GLSL
    if encoding not in list(controller.GetTargetShaderEncodings()):
        raise RuntimeError("Replay backend does not accept " + language)
    flags = rd.ShaderCompileFlags()
    if language == "hlsl":
        profile = rd.ShaderCompileFlag()
        profile.name, profile.value = "@cmdline", "/T ps_5_0"
        flags.flags = [profile]
    resource, messages = controller.BuildTargetShader("main", encoding, source.encode("utf-8"), flags, rd.ShaderStage.Pixel)
    if _null(resource):
        raise RuntimeError("Native target compiler failed: " + str(messages))
    return resource, str(messages)


def _set(controller, event, original, replacement, target, independent=False):
    controller.SetFrameEvent(int(event), True)
    error = controller.SetEventShaderOverride(int(event), rd.ShaderStage.Pixel, original, replacement, target, independent)
    if error:
        raise RuntimeError("Event override failed: " + str(error))


def _restore(controller, event, state):
    controller.SetFrameEvent(int(event), True)
    if state.active:
        error = controller.SetEventShaderOverride(int(event), rd.ShaderStage.Pixel,
                    state.expectedOriginal, state.replacement, int(state.outputTarget),
                    bool(getattr(state, "independentFloatOutput", False)))
        if error:
            raise RuntimeError("Event override restoration failed: " + str(error))
    else:
        controller.ClearEventShaderOverride(int(event), rd.ShaderStage.Pixel)
    controller.SetFrameEvent(int(event), True)


def _pixel_matches(controller, target, expected):
    # Use native sampling rather than assuming channel order, row pitch,
    # compression or the attachment's numeric storage width.
    matches = []
    for gy in range(1, 16):
        for gx in range(1, 16):
            x = min(target["width"] - 1, gx * target["width"] // 16)
            y = min(target["height"] - 1, gy * target["height"] // 16)
            pixel = controller.PickPixel(target["resource"], x, y, target["sub"], target["componentType"])
            field = (pixel.uintValue if target["componentType"] == rd.CompType.UInt else
                     pixel.intValue if target["componentType"] == rd.CompType.SInt else pixel.floatValue)
            value = [float(v) for v in field]
            if all(abs(a - b) < 0.009 for a, b in zip(value, expected)):
                matches.append({"x": x, "y": y, "value": value})
                if len(matches) == 4:
                    return matches
    return matches


def _preview_snapshot(controller, event):
    texture = controller.GetEventShaderPreviewTexture(event, rd.ShaderStage.Pixel)
    if _null(texture.resourceId):
        raise RuntimeError("Independent preview texture was not exposed")
    if texture.format.compType != rd.CompType.Float or int(texture.format.compCount) != 4 or int(texture.format.compByteWidth) != 4:
        raise RuntimeError("Independent preview silently changed RGBA32_FLOAT storage")
    if int(texture.mips) != 1:
        raise RuntimeError("Independent preview did not normalize the selected view to mip0")
    sub = rd.Subresource(0, 0, 0)
    raw = bytes(controller.GetTextureData(texture.resourceId, sub))
    if not raw:
        raise RuntimeError("Independent preview has empty readback")
    return [{"index": 0, "resource": texture.resourceId, "sub": sub,
             "width": int(texture.width), "height": int(texture.height),
             "format": str(texture.format.Name()), "componentType": rd.CompType.Float,
             "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}]


def _validate_float_preview(controller, event, original, target_index, api, entry, textures,
                            sibling, sibling_baseline, baseline, owned, report):
    controller.SetFrameEvent(event, True)
    pipeline = controller.GetPipelineState()
    descriptors = list(pipeline.GetOutputTargets()) + [pipeline.GetDepthTarget()]
    captured = [(target.resource, rd.Subresource(max(0, int(target.firstMip)),
                 max(0, int(target.firstSlice)), 0)) for target in descriptors if not _null(target.resource)]

    def attachment_hashes():
        return [{"resource": str(resource), "sha256": hashlib.sha256(bytes(controller.GetTextureData(resource, sub))).hexdigest()}
                for resource, sub in captured]

    # All storage writes before an action are visible at its preceding EID. Keep
    # the target Draw's descriptors, since that preceding action may bind others.
    controller.SetFrameEvent(event - 1, True)
    prefix = attachment_hashes()
    gles = "GLES" in str(entry.get("api", "")) or "GLES" in str(entry.get("demo", ""))
    language, source = _source(api, gles, target_index)
    prior, prior_messages = _build(controller, language, source)
    owned.append(prior)
    prior_float = bool(entry.get("priorIndependentFloatOutput", False))
    _set(controller, event, original, prior, target_index, prior_float)
    prior_state = controller.GetEventShaderOverride(event, rd.ShaderStage.Pixel)
    prior_snapshot = (_preview_snapshot(controller, event) if prior_float else
                      _snapshot(controller, event, target_index))
    if not _pixel_matches(controller, prior_snapshot[0], [0.75, 0.5, 0.125, 1]):
        raise RuntimeError("Preexisting preview did not render its known value")
    if language == "hlsl":
        hdr_source = "float4 main() : SV_Target%d { float4 probe = float4(-7.0, 2.5, 0.125, 1.0); return probe; }" % target_index
    else:
        prefix_source = source[:source.index("layout(")]
        hdr_source = prefix_source + "layout(location=%d) out vec4 color;\nvoid main() { vec4 probe = vec4(-7.0, 2.5, 0.125, 1.0); color = probe; }\n" % target_index
    module = _instrumenter()
    plan = module.inspect(hdr_source, hdr_source.rindex("probe"), language=language)
    generated = module.instrument(plan, target={"index": target_index, "components": 4, "componentType": "float"})
    debug, debug_messages = _build(controller, language, generated["source"])
    owned.append(debug)
    _set(controller, event, original, debug, target_index, True)
    debug_snapshot = _preview_snapshot(controller, event)
    untouched = attachment_hashes()
    pixels = _pixel_matches(controller, debug_snapshot[0], [-7, 2.5, 0.125, 1])
    _set(controller, event, original, debug, target_index, True)
    repeated = _preview_snapshot(controller, event)
    _set(controller, event, original, debug, target_index, False)
    original_mode_hidden = _null(controller.GetEventShaderPreviewTexture(event, rd.ShaderStage.Pixel).resourceId)
    _set(controller, event, original, debug, target_index, True)
    returned = _preview_snapshot(controller, event)
    sibling_debug = _hashes(_snapshot(controller, sibling))
    _restore(controller, event, prior_state)
    restored = (_preview_snapshot(controller, event) if prior_float else
                _snapshot(controller, event, target_index))
    current = controller.GetEventShaderOverride(event, rd.ShaderStage.Pixel)
    checks = {"actualNegativeAndHDR": bool(pixels),
              "normalizedDimensions": (debug_snapshot[0]["width"], debug_snapshot[0]["height"]) == (baseline[0]["width"], baseline[0]["height"]),
              "capturedAttachmentsRetainBeforeDraw": untouched == prefix,
              "scratchReused": _hashes(repeated) == _hashes(debug_snapshot),
              "originalRTModeHidesPreview": original_mode_hidden,
              "floatRoundTripStable": _hashes(returned) == _hashes(debug_snapshot),
              "siblingDrawUnchanged": sibling_debug == sibling_baseline,
              "preexistingOverrideRestored": _hashes(restored) == _hashes(prior_snapshot),
              "preexistingModeRestored": bool(current.independentFloatOutput) == prior_float,
              "capturedShaderIdentityPreserved": controller.GetPipelineState().GetShader(rd.ShaderStage.Pixel) == original}
    report.update(independentFloatOutput=True, priorIndependentFloatOutput=prior_float,
                  checks=checks, debugPixels=pixels, beforeDrawAttachments=prefix,
                  capturedAttachmentsDuringPreview=untouched, debugOutput=_hashes(debug_snapshot),
                  repeatedOutput=_hashes(repeated), floatRoundTripOutput=_hashes(returned),
                  priorOutput=_hashes(prior_snapshot), restoredOutput=_hashes(restored),
                  compilerMessages=[prior_messages, debug_messages], ok=all(checks.values()))


def validate_capture(entry, manifest_directory):
    started = time.perf_counter()
    report = {"api": entry.get("api", ""), "requestedAPI": entry.get("api", ""), "capture": entry.get("capture"), "ok": False,
              "priorScope": entry.get("priorScope", "event")}
    capture, controller, saved_override, event = None, None, None, None
    global_prior = False
    owned = []
    try:
        filename = Path(entry["capture"])
        if not filename.is_absolute():
            filename = manifest_directory / filename
        report["capture"] = str(filename.resolve())
        capture = rd.OpenCaptureFile()
        status = capture.OpenFile(str(filename), "", None)
        if status != rd.ResultCode.Succeeded:
            raise RuntimeError("Capture open failed: " + str(status))
        options = rd.ReplayOptions()
        options.apiValidation = bool(entry.get("apiValidation", False))
        status, controller = capture.OpenCapture(options, None)
        if status != rd.ResultCode.Succeeded or controller is None:
            raise RuntimeError("Replay open failed: " + str(status))
        api = controller.GetAPIProperties().pipelineType
        report["api"] = str(api)
        textures = {texture.resourceId: texture for texture in controller.GetTextures()}
        roots = controller.GetRootActions()
        draws = list(_leaves(roots))
        if len(draws) < 2:
            raise RuntimeError("Fixture requires two Draws to verify event isolation")
        original, target_index = None, None
        candidates = list(_marked_draws(roots, entry["drawMarker"])) if entry.get("drawMarker") else draws
        components = int(entry.get("targetComponents", 4))
        component_type = str(entry.get("targetType", "float"))
        probe_type = str(entry.get("probeType", component_type))
        allowed_types = ({"float": (rd.CompType.Float, rd.CompType.UNorm, rd.CompType.SNorm, rd.CompType.UNormSRGB),
                          "uint": (rd.CompType.UInt,), "sint": (rd.CompType.SInt,)}[component_type])
        for draw in reversed(candidates):
            controller.SetFrameEvent(int(draw.eventId), True)
            pipeline = controller.GetPipelineState()
            shader = pipeline.GetShader(rd.ShaderStage.Pixel)
            if _null(shader):
                continue
            for index, target in enumerate(pipeline.GetOutputTargets()):
                if _null(target.resource) or target.resource not in textures:
                    continue
                fmt = _target_format(target, textures)
                samples_match = ("targetSamples" not in entry or
                                 int(textures[target.resource].msSamp) == int(entry["targetSamples"]))
                if int(fmt.compCount) == components and fmt.compType in allowed_types and samples_match:
                    event, original, target_index = int(draw.eventId), shader, index
                    break
            if event is not None:
                break
        if event is None:
            raise RuntimeError("No raster Draw matches requested %s/%d-channel target and marker" % (component_type, components))
        sibling = next(int(draw.eventId) for draw in draws if int(draw.eventId) != event)
        saved_override = controller.GetEventShaderOverride(event, rd.ShaderStage.Pixel)
        baseline = _snapshot(controller, event, target_index)
        sibling_baseline = _hashes(_snapshot(controller, sibling))
        if not sibling_baseline:
            raise RuntimeError("Sibling Draw has no readable output")
        report.update(event=event, siblingEvent=sibling, targetIndex=target_index,
                      targetComponents=components, targetType=component_type,
                      probeType=probe_type,
                      baseline=_hashes(baseline), siblingBaseline=sibling_baseline)
        if entry.get("independentFloatOutput"):
            _validate_float_preview(controller, event, original, target_index, api, entry, textures,
                                    sibling, sibling_baseline, baseline, owned, report)
            return report
        language, source = _source(api, "GLES" in str(entry.get("api", "")) or "GLES" in str(entry.get("demo", "")), target_index, components, component_type, probe_type)
        prior, prior_messages = _build(controller, language, source)
        owned.append(prior)
        if report["priorScope"] == "global":
            controller.ReplaceResource(original, prior)
            global_prior = True
            # The baseline for an unrelated Draw is now the existing global
            # replacement, which the temporary event overlay must preserve.
            sibling_baseline = _hashes(_snapshot(controller, sibling))
            report["siblingBaseline"] = sibling_baseline
        else:
            _set(controller, event, original, prior, target_index)
        prior_snapshot = _snapshot(controller, event, target_index)
        expected_prior = ([0.75, 0.5, 0.125, 1.0] if probe_type == "float" else [10, 5, 2, 1])[:components]
        expected_debug = [{"float": 0.25, "uint": 7, "sint": -7, "bool": 1}[probe_type], 0, 0, 1][:components]
        prior_pixels = _pixel_matches(controller, prior_snapshot[0], expected_prior)
        if not prior_pixels:
            raise RuntimeError("No covered pixel matched the preexisting fixture Shader")
        prior_state = controller.GetEventShaderOverride(event, rd.ShaderStage.Pixel)
        module = _instrumenter()
        plan = module.inspect(source, source.rindex("probe"), language=language)
        generated = module.instrument(plan, target={"index": target_index, "components": components, "componentType": component_type})
        compile_started = time.perf_counter()
        debug, debug_messages = _build(controller, language, generated["source"])
        report["compileMs"] = (time.perf_counter() - compile_started) * 1000
        owned.append(debug)
        apply_started = time.perf_counter()
        _set(controller, event, original, debug, target_index)
        debug_snapshot = _snapshot(controller, event, target_index)
        report["applyAndReadbackMs"] = (time.perf_counter() - apply_started) * 1000
        pixels = _pixel_matches(controller, debug_snapshot[0], expected_debug)
        sibling_debug = _hashes(_snapshot(controller, sibling))
        _restore(controller, event, prior_state)
        restored = _hashes(_snapshot(controller, event, target_index))
        checks = {"actualScalarAndPadding": bool(pixels),
                  "renderingChanged": _hashes(debug_snapshot) != _hashes(prior_snapshot),
                  "siblingDrawUnchanged": sibling_debug == sibling_baseline,
                  "preexistingOverrideRestored": restored == _hashes(prior_snapshot),
                  "capturedShaderIdentityPreserved": controller.GetPipelineState().GetShader(rd.ShaderStage.Pixel) == original}
        report.update(checks=checks, debugPixels=pixels, priorPixels=prior_pixels,
                      debugOutput=_hashes(debug_snapshot), priorOutput=_hashes(prior_snapshot),
                      restoredOutput=restored, siblingDebug=sibling_debug,
                      compilerMessages=[prior_messages, debug_messages], ok=all(checks.values()))
    except Exception:
        report["error"] = traceback.format_exc()
        if controller is not None:
            report["debugMessages"] = [str(message.description) for message in controller.GetDebugMessages()]
    finally:
        if controller is not None:
            try:
                if saved_override is not None:
                    _restore(controller, event, saved_override)
                    report["finalRestorationSucceeded"] = True
            except Exception:
                report["ok"] = False
                report["restorationError"] = traceback.format_exc()
            if global_prior:
                try:
                    controller.RemoveReplacement(original)
                    final = _hashes(_snapshot(controller, event, target_index))
                    report["globalRemovalRestoredCaptured"] = final == _hashes(baseline)
                    report["ok"] = bool(report["ok"] and report["globalRemovalRestoredCaptured"])
                except Exception:
                    report["ok"] = False
                    report["globalRestorationError"] = traceback.format_exc()
            for target in reversed(owned):
                try:
                    controller.FreeTargetResource(target)
                except Exception:
                    report["ok"] = False
                    report["freeError"] = traceback.format_exc()
            controller.Shutdown()
        if capture is not None:
            capture.Shutdown()
        report["elapsedSeconds"] = time.perf_counter() - started
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-results", default=os.environ.get("BREAD_SHADER_VISUAL_SMOKE_INPUT"))
    parser.add_argument("--capture", default=os.environ.get("BREAD_SHADER_VISUAL_SMOKE_CAPTURE"))
    parser.add_argument("--output", default=os.environ.get("BREAD_SHADER_VISUAL_SMOKE_OUTPUT"))
    args = parser.parse_args()
    if not args.output or not (args.capture_results or args.capture):
        parser.error("Provide capture input and an explicit output report path")
    directory = Path.cwd()
    entries = [{"capture": args.capture, "ok": True}] if args.capture else []
    if args.capture_results:
        manifest = Path(args.capture_results).resolve()
        directory = manifest.parent
        entries += json.loads(manifest.read_text(encoding="utf-8"))["captures"]
    report = {"schema": "bread-shader-visual-gpu-smoke/v1", "ok": False,
              "python": sys.version, "captures": []}
    rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        for entry in entries:
            report["captures"].append(validate_capture(entry, directory))
            Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
        report["ok"] = bool(report["captures"]) and all(item["ok"] for item in report["captures"])
    finally:
        rd.ShutdownReplay()
        Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("BREAD_SHADER_VISUAL_SMOKE=" + json.dumps({"ok": report["ok"], "output": args.output}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
