"""Real GPU oracle for expression instrumentation, in an owned replay process.

BREAD_SHADER_EXPRESSION_GPU_CONFIG contains repository, output and captures:
[{capture, language: hlsl|glsl, optional gles/eventId}]. Use qrenderdoc --python
with a fresh analytics-opted-out profile. This never opens the user's UI capture.
Each case checks expected RGBA32F pixels and exact original-attachment restoration.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import traceback


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run(config, report):
    import renderdoc as rd
    root = Path(config["repository"])
    native = load("bread_expression_native", root / "replaylab/tests/fixtures/qrenderdoc_shader_visual_debug_smoke.py")
    visual = load("bread_expression_instrument", root / "replaylab/renderdoc_mcp_bridge/shader_visual_instrument.py")
    compiler = load("bread_expression_compile", root / "replaylab/renderdoc_mcp_bridge/shader_visual_compile.py")
    samples = load("bread_expression_cases", root / config.get("casesModule", "replaylab/tests/fixtures/shader_visual_expression_cases.py"))
    report["instrumenterSha256"] = hashlib.sha256(Path(visual.__file__).read_bytes()).hexdigest()
    output = Path(config["output"])
    def save():
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for item in config["captures"]:
        capture = controller = None
        capture_report = {"capture": item["capture"], "language": item["language"], "cases": []}
        report["captures"].append(capture_report)
        try:
            capture = rd.OpenCaptureFile()
            status = capture.OpenFile(item["capture"], "", None)
            if status != rd.ResultCode.Succeeded:
                raise RuntimeError("Capture open failed: " + str(status))
            status, controller = capture.OpenCapture(rd.ReplayOptions(), None)
            if status != rd.ResultCode.Succeeded:
                raise RuntimeError("Replay open failed: " + str(status))
            event = int(item.get("eventId") or max(int(action.eventId) for action in native._leaves(controller.GetRootActions())))
            controller.SetFrameEvent(event, True)
            api = controller.GetAPIProperties().pipelineType
            capture_report.update(api=str(api), eventId=event)
            before = native._snapshot(controller, event)
            if not before:
                raise RuntimeError("No color target")
            slot = int(item.get("targetIndex", before[0]["index"]))
            original = controller.GetPipelineState().GetShader(rd.ShaderStage.Pixel)
            prior = controller.GetEventShaderOverride(event, rd.ShaderStage.Pixel)
            def build(source):
                if api == rd.GraphicsAPI.Vulkan and item["language"] == "hlsl":
                    compiled = compiler.Compiler(str(root / "x64/Development")).compile(
                        source, "hlsl", "vulkan", "main", "ps_6_0", exported_entry="main")
                    resource, messages = controller.BuildTargetShader("main", rd.ShaderEncoding.SPIRV,
                        compiled["bytes"], rd.ShaderCompileFlags(), rd.ShaderStage.Pixel)
                    if native._null(resource):
                        raise RuntimeError("SPIR-V install failed: " + str(messages))
                    return resource
                return native._build(controller, item["language"], source)[0]
            def preview_target():
                preview = controller.GetEventShaderPreviewTexture(event, rd.ShaderStage.Pixel)
                if native._null(preview.resourceId):
                    raise RuntimeError("No independent preview texture")
                return {"resource": preview.resourceId, "width": int(preview.width),
                        "height": int(preview.height), "sub": rd.Subresource(),
                        "componentType": rd.CompType.Float}
            # FXC and DXC's SPIR-V backend can differ in their legacy HLSL
            # short-circuit behavior. Establish this route's real behavior with
            # an uninstrumented side-effect control, then compare the probe to
            # that observed behavior instead of assuming one compiler dialect.
            if item["language"] == "hlsl":
                control = ("static float seen=0.0;bool visit(){seen=1.0;return true;}"
                           "float4 main():SV_Target%d{bool gate=false;bool value=gate&&visit();"
                           "return float4(seen,value?1.0:0.0,0,1);}") % slot
            else:
                prefix = ("#version 320 es\nprecision highp float;\n" if item.get("gles") else "#version 450\n")
                control = prefix + ("layout(location=%d)out vec4 color;float seen=0.0;"
                           "bool visit(){seen=1.0;return true;}void main(){bool gate=false;"
                           "bool value=gate&&visit();color=vec4(seen,value?1.0:0.0,0,1);}") % slot
            resource = build(control)
            try:
                native._set(controller, event, original, resource, slot, True)
                target = preview_target()
                eager = native._pixel_matches(controller, target, [1, 0, 0, 1])
                skipped = native._pixel_matches(controller, target, [0, 0, 0, 1])
                if bool(eager) == bool(skipped):
                    raise AssertionError("Short-circuit compiler control was inconclusive")
                capture_report["falseRhsEvaluatedByCompiler"] = bool(eager)
            finally:
                native._restore(controller, event, prior)
                controller.FreeTargetResource(resource)
            for case in samples.cases(item["language"], item.get("gles", False)):
                if case.get("compilerOnly"):
                    continue
                if config.get("caseNames") and case["name"] not in config["caseNames"]:
                    continue
                if item.get("caseNames") and case["name"] not in item["caseNames"]:
                    continue
                if case.get("targetComponents", 4) != item.get("targetComponents", 4) or case.get("targetType", "float") != item.get("targetType", "float"):
                    continue
                expected = case["expected"]
                if case["name"] in ("short-circuit-skipped-rhs", "short-circuit-call-skipped"):
                    expected = [.25, 0, 0, 1] if capture_report["falseRhsEvaluatedByCompiler"] else [1, 0, 1, 1]
                result = {"name": case["name"], "expected": expected, "ok": False}
                capture_report["cases"].append(result)
                resource = None
                try:
                    # Establish that the original program is legal for this
                    # actual driver. Desktop GLSL compiler implementations can
                    # disagree on mixed int/float intrinsic overloads.
                    baseline = build(case["source"])
                    controller.FreeTargetResource(baseline)
                    result["originalSourceCompiled"] = True
                    if config.get("baselineOnly"):
                        result["ok"] = True
                        continue
                    plan = visual.inspect(case["source"], case["byteStart"], language=item["language"])
                    generated = visual.instrument(plan, member=case["member"], iteration=case["iteration"],
                        target={"index": slot, "components": case.get("targetComponents", 4), "componentType": case.get("targetType", "float")},
                        mode=case.get("mode", "raw"), channel=case.get("channel", -1),
                        range_min=case.get("rangeMin", 0), range_max=case.get("rangeMax", 1))
                    result["selection"] = generated["selection"]
                    resource = build(generated["source"])
                    independent = bool(item.get("independentFloatOutput", True))
                    native._set(controller, event, original, resource, slot, independent)
                    target = preview_target() if independent else native._snapshot(controller, event, slot)[0]
                    result["outputFormat"] = target.get("format", "RGBA32_FLOAT")
                    matches = native._pixel_matches(controller, target, expected)
                    result["matchingPixels"] = matches
                    if len(matches) < 4:
                        raise AssertionError("Expected expression value absent from rendered pixels")
                    result["ok"] = True
                except BaseException:
                    result["error"] = traceback.format_exc()
                finally:
                    native._restore(controller, event, prior)
                    restored = native._snapshot(controller, event)
                    result["originalOutputRestored"] = native._hashes(before) == native._hashes(restored)
                    result["ok"] = result["ok"] and result["originalOutputRestored"]
                    if resource is not None and not native._null(resource):
                        controller.FreeTargetResource(resource)
                    save()
            capture_report["ok"] = all(case["ok"] for case in capture_report["cases"])
        except BaseException:
            capture_report["error"] = traceback.format_exc()
            capture_report["ok"] = False
        finally:
            if controller is not None:
                controller.Shutdown()
            if capture is not None:
                capture.Shutdown()
            save()
    report["ok"] = bool(report["captures"]) and all(item["ok"] for item in report["captures"])


def main():
    config = json.loads(Path(os.environ["BREAD_SHADER_EXPRESSION_GPU_CONFIG"]).read_text(encoding="utf-8-sig"))
    output = Path(config["output"])
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"schema": "bread-shader-expression-gpu/v1", "pid": os.getpid(), "ok": False, "captures": []}
    try:
        run(config, report)
    except BaseException:
        report["error"] = traceback.format_exc()
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
