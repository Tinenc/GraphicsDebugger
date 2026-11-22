"""Real Manager/ReplayDriver EID-follow acceptance in an owned replay process.

Run qrenderdoc --python with a private profile. BREAD_SHADER_VISUAL_FOLLOW_CONFIG
names JSON with repository/output and a list of capture/sourceFile/entryPoint/
language records. No user application, project, or capture is opened in UI.
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


def actions(roots):
    for action in roots:
        yield action
        for child in actions(action.children):
            yield child


def run_capture(root, config, report, ordinal):
    import renderdoc as rd
    bridge = load("bread_follow_bridge_%d" % ordinal, root / "replaylab/renderdoc_mcp_bridge/__init__.py")
    visual = load("bread_follow_manager_%d" % ordinal, root / "replaylab/renderdoc_mcp_bridge/shader_visual_debug.py")
    native = load("bread_follow_native_%d" % ordinal, root / "replaylab/tests/fixtures/qrenderdoc_shader_visual_debug_smoke.py")
    controller = capture = manager = None
    try:
        capture = rd.OpenCaptureFile()
        status = capture.OpenFile(config["capture"], "", None)
        if status != rd.ResultCode.Succeeded:
            raise RuntimeError("OpenFile: " + str(status))
        status, controller = capture.OpenCapture(rd.ReplayOptions(), None)
        if status != rd.ResultCode.Succeeded:
            raise RuntimeError("OpenCapture: " + str(status))
        leaves = list(actions(controller.GetRootActions()))
        draw_flags = rd.ActionFlags.Drawcall | rd.ActionFlags.MeshDispatch
        groups = {}
        for action in leaves:
            if action.children or not action.flags & draw_flags:
                continue
            controller.SetFrameEvent(int(action.eventId), True)
            pipeline = controller.GetPipelineState()
            shader = pipeline.GetShader(rd.ShaderStage.Pixel)
            if native._null(shader):
                continue
            targets = native._snapshot(controller, int(action.eventId))
            if not targets:
                continue
            key = (str(shader), tuple((t["index"], t["format"]) for t in targets))
            groups.setdefault(key, []).append(int(action.eventId))
        choices = [events for events in groups.values() if len(events) >= 2]
        if not choices:
            raise AssertionError("Fixture needs two draws with the same fragment shader and output signature")
        first, second = max(choices, key=lambda events: events[-1])[-2:]
        non_draws = [int(a.eventId) for a in leaves if not a.flags & draw_flags and int(a.eventId) > 0]
        if not non_draws:
            raise AssertionError("Fixture needs a real non-draw action")
        paused_event = non_draws[0]
        report.update(api=bridge._shader_api_token(controller), events=[first, second], nonDrawEvent=paused_event)
        baseline = {event: native._hashes(native._snapshot(controller, event)) for event in (first, second)}
        controller.SetFrameEvent(first, True)
        if any(controller.GetEventShaderOverride(event, rd.ShaderStage.Pixel).active for event in (first, second, paused_event)):
            raise AssertionError("Fresh capture unexpectedly has an event shader override")
        bridge._capture_session_id = "owned-follow-%d" % ordinal
        bridge._active_capture_token = ("smoke", str(Path(config["capture"]).resolve()))
        bridge._replay = lambda callback: callback(controller)
        bridge._notify_shader_replay_changed = lambda: None
        driver = visual.ReplayDriver(bridge)
        compile_count = [0]
        actual_compile = driver.compile
        def counted_compile(*args):
            compile_count[0] += 1
            return actual_compile(*args)
        driver.compile = counted_compile
        manager = visual.Manager(driver)
        request = dict(captureBinding={"captureSessionId": bridge._capture_session_id,
                                      "capturePath": bridge._active_capture_token[1]},
                       ownerId="owned-follow", requestGeneration=1, eventId=first,
                       sourceFile=config["sourceFile"], entryPoint=config["entryPoint"])
        opened = manager.operation("capture-source", request)
        if opened.get("status") != "ready":
            raise RuntimeError("Capture source: " + repr(opened))
        source = opened["text"]
        raw = source.encode("utf-8")
        start = raw.index(b"probe +")
        request.update(source=source, sourceSha256=hashlib.sha256(raw).hexdigest(),
                       byteStart=start, byteEnd=start + 5, sourcePath=opened["sourcePath"],
                       rootSourcePath=opened["rootSourcePath"], includeRoot=opened["includeRoot"],
                       language=config["language"], stage="fragment", shaderId=opened["resourceId"],
                       expectedOriginalSha256=opened["shaderRawSha256"], member="", targetIndex=-1,
                       independentFloatOutput=True)
        # Omit mode deliberately: the actual Manager default must be custom.
        report["operations"] = []
        def operation(name, event, expected, stale_session_id=None):
            controller.SetFrameEvent(event, True)
            request.update(eventId=event, requestGeneration=request["requestGeneration"] + 1)
            if stale_session_id is not None:
                request["sessionId"] = stale_session_id
            submitted_session_id = request.get("sessionId", "")
            result = manager.operation(name, request)
            public = dict(result)
            texture = public.get("previewTexture")
            if texture is not None:
                public["previewTexture"] = dict(resourceId=str(texture.resourceId), format=texture.format.Name())
            report["operations"].append(dict(operation=name, eventId=event, result=public,
                                              submittedSessionId=submitted_session_id))
            if result.get("status") != expected:
                raise AssertionError("Unexpected " + name + " result: " + repr(result))
            request["sessionId"] = result.get("sessionId", "")
            expected_active = event if expected == "active" else None
            states = {eid: bool(controller.GetEventShaderOverride(eid, rd.ShaderStage.Pixel).active)
                      for eid in (first, second, paused_event)}
            if any(active != (eid == expected_active) for eid, active in states.items()):
                raise AssertionError("An override remains on a non-current event: " + repr(states))
            if expected == "active":
                texture = controller.GetEventShaderPreviewTexture(event, rd.ShaderStage.Pixel)
                target = dict(resource=texture.resourceId, width=texture.width, height=texture.height,
                              sub=rd.Subresource(), componentType=rd.CompType.Float)
                pixels = native._pixel_matches(controller, target, [.25, .25, .25, 1])
                if len(pixels) != 4:
                    raise AssertionError("Default custom scalar did not render on followed draw")
                report["operations"][-1]["matchingPixels"] = pixels
            return result
        started = operation("start", first, "active")
        if started["cacheHit"]:
            raise AssertionError("Initial start unexpectedly hit cache")
        followed = operation("follow", second, "active")
        if not followed["cacheHit"] or compile_count[0] != 1:
            raise AssertionError("Same-shader follow recompiled")
        paused = operation("follow", paused_event, "paused")
        if paused["reason"] != "not-draw":
            raise AssertionError("Non-draw pause used wrong reason")
        restored_after_pause = {event: native._hashes(native._snapshot(controller, event)) for event in (first, second)}
        if restored_after_pause != baseline:
            raise AssertionError("Pause did not restore original outputs at both draws")
        resumed = operation("follow", first, "active")
        if not resumed["cacheHit"] or compile_count[0] != 1:
            raise AssertionError("Resume did not reuse cached target")
        operation("stop", first, "stopped")
        restored = {event: native._hashes(native._snapshot(controller, event)) for event in (first, second)}
        if restored != baseline:
            raise AssertionError("Stop did not restore original outputs at both draws")
        # Model UI requests queued before it consumes the newest receipts:
        # generations advance, while every follow and stop carries the first
        # session ID. The actual Manager/native calls remain synchronous here;
        # this is stale-receipt coverage, not a claim of simultaneous GPU calls.
        rapid_start = operation("start", first, "active")
        stale_id = rapid_start["sessionId"]
        for event in (second, first, second):
            rapid_follow = operation("follow", event, "active", stale_id)
            if not rapid_follow["cacheHit"] or compile_count[0] != 1:
                raise AssertionError("Stale-receipt follow did not reuse the compiled target")
        if rapid_follow["sessionId"] == stale_id:
            raise AssertionError("Fixture did not exercise an outdated session ID")
        operation("stop", second, "stopped", stale_id)
        rapid_restored = {event: native._hashes(native._snapshot(controller, event)) for event in (first, second)}
        if rapid_restored != baseline:
            raise AssertionError("Stale-receipt Stop did not restore both draws")
        report.update(ok=True, compileCount=compile_count[0], originalOutputs=baseline,
                      pauseRestoredOutputs=restored_after_pause, stoppedOutputs=restored,
                      staleReceiptLoopRestoredOutputs=rapid_restored, staleReceiptStopSucceeded=True)
    finally:
        if manager is not None:
            manager.shutdown()
        if controller is not None:
            controller.Shutdown()
        if capture is not None:
            capture.Shutdown()


def main():
    config = json.loads(Path(os.environ["BREAD_SHADER_VISUAL_FOLLOW_CONFIG"]).read_text(encoding="utf-8-sig"))
    root = Path(config["repository"])
    output = Path(config["output"])
    output.parent.mkdir(parents=True, exist_ok=True)
    report = dict(schema="bread-shader-visual-follow-smoke/v1", ok=False, pid=os.getpid(), captures=[],
                  sourceHashes={name: hashlib.sha256((root / "replaylab/renderdoc_mcp_bridge" / name).read_bytes()).hexdigest()
                                for name in ("shader_visual_debug.py", "shader_visual_instrument.py")})
    for ordinal, item in enumerate(config["captures"]):
        row = dict(capture=item["capture"], name=item.get("name"), ok=False)
        report["captures"].append(row)
        try:
            run_capture(root, item, row, ordinal)
        except BaseException:
            row["error"] = traceback.format_exc()
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["ok"] = bool(report["captures"]) and all(r["ok"] for r in report["captures"])
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
