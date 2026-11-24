"""Exercise the real visual-debug Manager and ReplayDriver on one explicit capture.

Use matching renderdoc bindings, or qrenderdoc --python with a fresh profile whose
UI.config disables first-run analytics. Set BREAD_SHADER_VISUAL_DEBUG_SMOKE_CONFIG
to an absolute JSON path containing capture, output, repository, expression, and
optionally eventId/sourceFile/entryPoint/language. The script always writes a
receipt and exits; it never starts bridge services or opens a user capture in UI.
"""
from __future__ import print_function

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import traceback


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run(config, report):
    import renderdoc as rd
    root = Path(config["repository"]).resolve()
    bridge = load_module("bread_visual_debug_smoke_bridge", root / "replaylab/renderdoc_mcp_bridge/__init__.py")
    visual = load_module("bread_visual_debug_smoke_manager", root / "replaylab/renderdoc_mcp_bridge/shader_visual_debug.py")
    controller, capture, manager = None, None, None
    # qrenderdoc has already initialized replay before executing --python.
    initialize = "pyrenderdoc" not in globals()
    if initialize:
        rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        capture = rd.OpenCaptureFile()
        result = capture.OpenFile(str(Path(config["capture"]).resolve()), "", None)
        if result != rd.ResultCode.Succeeded:
            raise RuntimeError("OpenFile failed: " + str(result))
        result, controller = capture.OpenCapture(rd.ReplayOptions(), None)
        if result != rd.ResultCode.Succeeded or controller is None:
            raise RuntimeError("OpenCapture failed: " + str(result))
        event = int(config.get("eventId", 0))
        if not event:
            pending, draws = list(controller.GetRootActions()), []
            while pending:
                action = pending.pop(0)
                pending.extend(action.children)
                if action.flags & rd.ActionFlags.Drawcall:
                    draws.append(int(action.eventId))
            if not draws:
                raise RuntimeError("Capture has no draw")
            event = draws[-1]
        controller.SetFrameEvent(event, True)
        bridge._capture_session_id = "isolated-smoke-capture"
        bridge._active_capture_token = ("smoke", str(Path(config["capture"]).resolve()))
        bridge._replay = lambda callback: callback(controller)
        bridge._notify_shader_replay_changed = lambda: None
        driver = visual.ReplayDriver(bridge)
        manager = visual.Manager(driver)
        binding = {"captureSessionId": bridge._capture_session_id,
                   "capturePath": bridge._active_capture_token[1]}
        request = {"captureBinding": binding, "eventId": event,
                   "ownerId": "isolated-smoke", "requestGeneration": 1}
        for field in ("sourceFile", "entryPoint", "targetProfile"):
            if config.get(field):
                request[field] = config[field]
        opened = manager.operation("capture-source", request)
        report["source"] = {key: value for key, value in opened.items() if key != "text"}
        if opened.get("status") != "ready":
            raise RuntimeError("capture-source failed: " + repr(opened))
        source = opened["text"]
        raw, expression = source.encode("utf-8"), str(config["expression"]).encode("utf-8")
        start = int(config["byteStart"]) if "byteStart" in config else raw.index(expression)
        request.update(source=source, sourceSha256=hashlib.sha256(raw).hexdigest(),
            byteStart=start, byteEnd=start + len(expression), sourcePath=opened["sourcePath"],
            rootSourcePath=opened["rootSourcePath"], includeRoot=opened["includeRoot"],
            entryPoint=config.get("entryPoint") or opened["entryPoint"],
            language=config.get("language") or opened["language"], stage="fragment",
            shaderId=opened["resourceId"], expectedOriginalSha256=opened["shaderRawSha256"],
            mode="raw", member=config.get("member", ""), targetIndex=int(config.get("targetIndex", -1)))
        request["independentFloatOutput"] = bool(config.get("independentFloatOutput", False))
        receipts = report["operations"] = []

        def operation(name, **options):
            request["requestGeneration"] += 1
            params = dict(request, **options)
            receipt = manager.operation(name, params)
            public = dict(receipt)
            texture = public.get("previewTexture")
            if texture is not None:
                public["previewTexture"] = {"resourceId": str(int(texture.resourceId)),
                    "width": texture.width, "height": texture.height, "format": texture.format.Name(),
                    "samples": texture.msSamp}
            receipts.append({"operation": name, "result": public})
            if receipt.get("status") == "error":
                raise RuntimeError(name + " failed: " + repr(receipt))
            return receipt

        def output_hash(index):
            if request["independentFloatOutput"]:
                state = controller.GetEventShaderOverride(event, rd.ShaderStage.Pixel)
                if state.active and state.independentFloatOutput:
                    texture = controller.GetEventShaderPreviewTexture(event, rd.ShaderStage.Pixel)
                    data = bytes(controller.GetTextureData(texture.resourceId, rd.Subresource()))
                    if not data:
                        raise RuntimeError("Float preview data is empty")
                    return hashlib.sha256(data).hexdigest()
            descriptor = controller.GetPipelineState().GetOutputTargets()[index]
            sub = rd.Subresource(int(getattr(descriptor, "firstMip", 0)),
                                 int(getattr(descriptor, "firstSlice", 0)), 0)
            data = bytes(controller.GetTextureData(descriptor.resource, sub))
            if not data or len(data) > 64 * 1024 * 1024:
                raise RuntimeError("Expected a nonempty output of at most 64 MiB")
            return hashlib.sha256(data).hexdigest()

        inspected = operation("inspect")
        targets = inspected["targets"]
        chosen = request["targetIndex"]
        if chosen < 0:
            chosen = next((target["index"] for target in targets if target["components"] == 4), targets[0]["index"])
        original = output_hash(chosen)
        started = operation("start")
        request["sessionId"] = started["sessionId"]
        debug = output_hash(chosen)
        if config.get("requireChanged", True) and debug == original:
            raise AssertionError("Preview did not change the output pixels")
        repeated = operation("start")
        if not repeated["cacheHit"] or output_hash(chosen) != debug:
            raise AssertionError("Repeated preview was not a stable cache hit")
        for mode in config.get("modes", ["grayscale", "normal", "range", "coverage"]):
            operation("start", mode=mode, rangeMin=-1.0, rangeMax=1.0)
        cached = operation("start", mode="raw")
        if not cached["cacheHit"] or output_hash(chosen) != debug:
            raise AssertionError("Mode round trip did not reuse identical raw preview")
        operation("compare", original=True)
        if output_hash(chosen) != original:
            raise AssertionError("Original comparison differs from pre-debug rendering")
        operation("compare", original=False)
        if output_hash(chosen) != debug:
            raise AssertionError("Comparison release did not restore preview")
        operation("stop")
        restored = output_hash(chosen)
        if restored != original:
            raise AssertionError("Stop did not restore pre-debug rendering")
        report.update(ok=True, api=bridge._shader_api_token(controller), eventId=event,
                      targetIndex=chosen, originalSha256=original, debugSha256=debug,
                      restoredSha256=restored)
    finally:
        if manager is not None:
            manager.shutdown()
        if controller is not None:
            controller.Shutdown()
        if capture is not None:
            capture.Shutdown()
        if initialize:
            rd.ShutdownReplay()


def main():
    config = json.loads(Path(os.environ["BREAD_SHADER_VISUAL_DEBUG_SMOKE_CONFIG"]).read_text(encoding="utf-8-sig"))
    output = Path(config["output"])
    report = {"schema": "bread-shader-visual-debug-smoke/v1", "ok": False,
              "pid": os.getpid(), "capture": config["capture"]}
    try:
        run(config, report)
    except BaseException:
        report["error"] = traceback.format_exc()
    finally:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    try:
        code = main()
    except BaseException:
        # qrenderdoc otherwise opens its normal UI after a non-SystemExit error.
        traceback.print_exc()
        code = 1
    raise SystemExit(code)
