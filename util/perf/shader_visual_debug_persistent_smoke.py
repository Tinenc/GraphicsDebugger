"""Owned real-GPU acceptance for persistent Shader value debugging (Python 3.6).

Run qrenderdoc --python in a private profile with BREAD_SHADER_VISUAL_PERSISTENT_CONFIG.
The config uses the five-API Shared_Buffer captures/source files from the earlier visual
fixture. This script opens independent replay controllers, never a user's UI project.
It does not launch a process. Every GPU mutation is undone before releasing owned targets.
Optional config serverURL (or BREAD_SHADER_VISUAL_REMOTE_URL) selects an already running
matching remote server. Captures then open exclusively through IRemoteServer/ReplayProxy;
there is no local replay fallback. Server lifetime remains owned by the external launcher.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
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


def run_capture(root, config, row, ordinal, capture_factory=None):
    import renderdoc as rd
    bridge = load("bread_persistent_bridge_%d" % ordinal,
                  root / "replaylab/renderdoc_mcp_bridge/__init__.py")
    visual = load("bread_persistent_manager_%d" % ordinal,
                  root / "replaylab/renderdoc_mcp_bridge/shader_visual_debug.py")
    native = load("bread_persistent_native_%d" % ordinal,
                  root / "replaylab/tests/fixtures/qrenderdoc_shader_visual_debug_smoke.py")
    capture = controller = manager = prior = original = None
    row.update(checks=[], operations=[], scope="actual-Manager-ReplayDriver-native-GPU",
               linuxGpu=False, androidDevice=False, unrelatedShaderDraw=False)

    def check(name, value, detail=None):
        row["checks"].append(dict(name=name, passed=bool(value), detail=detail))
        if not value:
            raise AssertionError(name + ": " + str(detail))

    try:
        capture = capture_factory() if capture_factory is not None else rd.OpenCaptureFile()
        status = capture.OpenFile(config["capture"], "", None)
        if status != rd.ResultCode.Succeeded:
            raise RuntimeError("OpenFile: " + str(status))
        status, controller = capture.OpenCapture(rd.ReplayOptions(), None)
        if status != rd.ResultCode.Succeeded:
            raise RuntimeError("OpenCapture: " + str(status))
        if row.get("remote") is not None:
            check("captureOpenedThroughRealReplayProxy", row["remote"].get("remoteCaptureOpened") is True)
        check("persistentNativeGetterAvailable", hasattr(controller, "GetTargetShaderReplacement"))
        leaves = list(actions(controller.GetRootActions()))
        draw_flags = rd.ActionFlags.Drawcall | rd.ActionFlags.MeshDispatch
        groups = {}
        for action in leaves:
            if action.children or not action.flags & draw_flags:
                continue
            event = int(action.eventId)
            controller.SetFrameEvent(event, True)
            shader = controller.GetPipelineState().GetShader(rd.ShaderStage.Pixel)
            targets = native._snapshot(controller, event)
            if native._null(shader) or not targets:
                continue
            key = (str(shader), tuple((t["index"], t["format"], str(t["resource"])) for t in targets))
            groups.setdefault(key, []).append(event)
        choices = [events for events in groups.values() if len(events) >= 4]
        check("fourRealDrawsShareShaderAndAttachment", bool(choices))
        draws = max(choices, key=lambda events: events[-1])[-4:]
        first, anchor = draws[0], draws[-1]
        between = anchor - 1
        check("betweenEventPrecedesAnchorAndFollowsEarlierDraws", draws[-2] <= between < anchor)
        check("betweenIsNotDraw", not any(int(a.eventId) == between and a.flags & draw_flags for a in leaves))
        after = max(int(a.eventId) for a in leaves)
        check("afterEventExists", after >= anchor)
        events = list(dict.fromkeys([first, between, anchor, after]))
        controller.SetFrameEvent(anchor, True)
        original = controller.GetPipelineState().GetShader(rd.ShaderStage.Pixel)
        target = native._snapshot(controller, anchor)[0]
        row.update(api=bridge._shader_api_token(controller), draws=draws, anchorEventId=anchor,
                   earlierEventId=first, betweenEventId=between, afterEventId=after,
                   originalResourceId=str(original), targetResourceId=str(target["resource"]),
                   captureSha256=hashlib.sha256(Path(config["capture"]).read_bytes()).hexdigest())
        check("freshCaptureHasNoReplacement", native._null(controller.GetTargetShaderReplacement(original)))

        def snapshot(event):
            controller.SetFrameEvent(event, True)
            raw = bytes(controller.GetTextureData(target["resource"], target["sub"]))
            if not raw:
                raise RuntimeError("Empty GPU texture readback")
            samples = []
            for region in range(4):
                x = int((region + 0.5) * target["width"] / 4)
                y = target["height"] // 2
                pixel = controller.PickPixel(target["resource"], x, y, target["sub"], target["componentType"])
                samples.append(dict(region=region, x=x, y=y, value=[float(v) for v in pixel.floatValue]))
            return dict(sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw), samples=samples)

        def hashes(snapshots):
            return {event: value["sha256"] for event, value in snapshots.items()}

        baseline = {event: snapshot(event) for event in events}
        api = controller.GetAPIProperties().pipelineType
        language, prior_source = native._source(api, config.get("name") == "OpenGLES", target["index"])
        prior, messages = native._build(controller, language, prior_source)
        controller.SetFrameEvent(anchor, True)
        error = controller.ReplaceTargetShaderChecked(original, prior)
        check("preexistingCheckedReplacementInstalled", not error, str(error))
        check("priorApplyPreservesSelectedEvent", int(controller.GetCurrentEventID()) == anchor)
        check("nativeGetterReportsPrior", controller.GetTargetShaderReplacement(original) == prior)
        prior_outputs = {event: snapshot(event) for event in events}
        check("priorActuallyChangesRT", hashes(prior_outputs) != hashes(baseline))
        check("priorHasExpectedPixel", all(abs(a - b) < 0.015 for a, b in zip(
            prior_outputs[anchor]["samples"][0]["value"], [.75, .5, .125, 1])))
        row.update(priorTargetId=str(prior), priorCompilerMessages=messages,
                   baselineOutputs=baseline, priorOutputs=prior_outputs)

        bridge._capture_session_id = "owned-persistent-%d" % ordinal
        bridge._active_capture_token = ("smoke", str(Path(config["capture"]).resolve()))
        bridge._replay = lambda callback: callback(controller)
        bridge._notify_shader_replay_changed = lambda: None
        driver = visual.ReplayDriver(bridge)
        if config.get("compilerContractFile"):
            recipe = json.loads(Path(config["compilerContractFile"]).read_text(encoding="utf-8"))
            inspect_original = driver.inspect
            def inspect_bound(params, previous=None):
                facts = inspect_original(params, previous=previous)
                facts["workspaceCompiler"] = recipe
                facts["compileIdentity"]["compiler"] = driver.bound_compiler.identity(recipe)
                facts["flagValues"] = list(recipe["flags"])
                facts["compileIdentity"]["flags"] = list(recipe["flags"])
                facts["flagsNeedPreprocess"] = driver.bound_compiler.needs_preprocess(recipe)
                return facts
            driver.inspect = inspect_bound
            row["compilerContractSha256"] = recipe["identitySha256"]
        actual_compile, actual_apply = driver.compile, driver.apply
        counts = dict(compile=0, apply=0)

        def compile_target(*args):
            counts["compile"] += 1
            return actual_compile(*args)

        def apply_target(*args, **kwargs):
            counts["apply"] += 1
            return actual_apply(*args, **kwargs)

        driver.compile, driver.apply = compile_target, apply_target
        manager = visual.Manager(driver)
        controller.SetFrameEvent(anchor, True)
        request = dict(captureBinding={"captureSessionId": bridge._capture_session_id,
                                      "capturePath": bridge._active_capture_token[1]},
                       ownerId="owned-persistent", requestGeneration=1, eventId=anchor,
                       sourceFile=config["sourceFile"], entryPoint=config["entryPoint"])
        opened = manager.operation("capture-source", request)
        check("captureSourceReady", opened.get("status") == "ready", opened.get("message"))
        source = opened["text"]
        raw = source.encode("utf-8")
        start = raw.index(b"probe +")
        request.update(source=source, sourceSha256=hashlib.sha256(raw).hexdigest(),
                       byteStart=start, byteEnd=start + 5, sourcePath=opened["sourcePath"],
                       rootSourcePath=opened["rootSourcePath"], includeRoot=opened["includeRoot"],
                       language=config["language"], stage="fragment", shaderId=opened["resourceId"],
                       expectedOriginalSha256=opened["shaderRawSha256"], member="", targetIndex=-1,
                       independentFloatOutput=False)
        # Deliberately omit scope/mode to verify both product defaults.

        def operation(name, expected, **changes):
            request.update(changes)
            request["requestGeneration"] += 1
            # The editor's immutable Shader anchor stays put when observation EID changes.
            request["eventId"] = anchor
            before = int(controller.GetCurrentEventID())
            began = time.perf_counter()
            result = manager.operation(name, request)
            row["operations"].append(dict(operation=name, eventId=before, requestAnchorEventId=anchor,
                elapsedMs=(time.perf_counter() - began) * 1000, result=result))
            check(name + "Status", result.get("status") == expected, result)
            check(name + "DoesNotSelectAnotherEID", int(controller.GetCurrentEventID()) == before)
            request["sessionId"] = result.get("sessionId", "")
            return result

        controller.SetFrameEvent(between, True)
        started = operation("start", "active")
        check("freshStartAwayFromAnchorPreservesObservationEID", int(controller.GetCurrentEventID()) == between and
              started.get("anchorEventId") == anchor)
        check("defaultScopePersistsAcrossEvents", started.get("scope") == "shader-resource" and
              started.get("persistentAcrossEvents") is True and started.get("eventScoped") is False)
        check("firstStartCompiledOnce", counts == dict(compile=1, apply=1) and not started["cacheHit"], counts)
        debug = controller.GetTargetShaderReplacement(original)
        check("nativeGlobalTargetChanged", not native._null(debug) and debug != prior)
        debug_generation = int(controller.GetShaderReplacementGeneration(0))
        debug_outputs = {}
        # No follow/apply notification occurs during these real EID replays. Earlier Draws
        # must already be debugged in the prefix before the anchor Draw even executes.
        for event in [between, first, after, anchor]:
            debug_outputs[event] = snapshot(event)
            check("resourceOwnerSurvivesEID:%d" % event,
                  controller.GetTargetShaderReplacement(original) == debug)
            check("EIDDoesNotMutateReplacement:%d" % event,
                  int(controller.GetShaderReplacementGeneration(0)) == debug_generation)
            check("noPerEIDCompileOrApply:%d" % event, counts == dict(compile=1, apply=1), dict(counts))
            executed = sum(draw <= event for draw in draws)
            for sample in debug_outputs[event]["samples"][:executed]:
                check("prefixGrayscale:%d:region%d" % (event, sample["region"]),
                      all(abs(a - b) < 0.015 for a, b in zip(sample["value"], [.25, .25, .25, 1])), sample)
            check("actualRTDiffersFromPrior:%d" % event,
                  debug_outputs[event]["sha256"] != prior_outputs[event]["sha256"])
        row["debugOutputs"] = debug_outputs
        controller.SetFrameEvent(between, True)
        followed = operation("follow", "active")
        check("legacyFollowKeepsSessionAndTarget", followed["sessionId"] == started["sessionId"] and
              controller.GetTargetShaderReplacement(original) == debug and counts == dict(compile=1, apply=1))
        operation("compare", "comparing", original=True)
        check("compareRestoresBorrowedPrior", controller.GetTargetShaderReplacement(original) == prior)
        compared = {event: snapshot(event) for event in events}
        check("compareRestoresExactPriorPixels", hashes(compared) == hashes(prior_outputs))
        controller.SetFrameEvent(between, True)
        operation("compare", "active", original=False)
        check("compareReturnReusesTarget", controller.GetTargetShaderReplacement(original) == debug and counts["compile"] == 1)
        check("compareReturnRestoresExactDebugPixels", snapshot(between)["sha256"] == debug_outputs[between]["sha256"])
        controller.SetFrameEvent(after, True)
        operation("stop", "stopped")
        check("stopAtOtherEIDRestoresPriorOwner", controller.GetTargetShaderReplacement(original) == prior)
        stopped = {event: snapshot(event) for event in events}
        check("stopRestoresExactPriorPixels", hashes(stopped) == hashes(prior_outputs))
        controller.SetFrameEvent(anchor, True)
        restarted = operation("start", "active")
        check("restartUsesCachedShader", restarted["cacheHit"] is True and counts["compile"] == 1 and
              controller.GetTargetShaderReplacement(original) == debug, dict(counts))
        operation("stop", "stopped")
        manager.shutdown()
        manager = None
        check("shutdownDoesNotFreeBorrowedPrior", controller.GetTargetShaderReplacement(original) == prior and
              snapshot(anchor)["sha256"] == prior_outputs[anchor]["sha256"])
        check("cleanupPrior", not controller.RemoveTargetShaderReplacementChecked(original))
        restored = {event: snapshot(event) for event in events}
        check("cleanupRestoresExactCapturePixels", hashes(restored) == hashes(baseline))
        check("cleanupLeavesNoReplacement", native._null(controller.GetTargetShaderReplacement(original)))
        controller.FreeTargetResource(prior)
        prior = None
        row.update(ok=True, compileCount=counts["compile"], applyCount=counts["apply"],
                   debugTargetId=str(debug), compareOutputs=compared, stoppedOutputs=stopped,
                   finalOutputs=restored, downstreamSamplingCoverage="not-present-in-shared-buffer-fixture")
    finally:
        if manager is not None:
            manager.shutdown()
        if controller is not None:
            if prior is not None:
                error = controller.RemoveTargetShaderReplacementChecked(original)
                if error:
                    raise RuntimeError("Cleanup restore failed: " + str(error))
                controller.FreeTargetResource(prior)
            controller.Shutdown()
        if capture is not None:
            capture.Shutdown()


def main():
    path = Path(os.environ["BREAD_SHADER_VISUAL_PERSISTENT_CONFIG"])
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    root, output = Path(config["repository"]), Path(config["output"])
    output.parent.mkdir(parents=True, exist_ok=True)
    endpoint = str(config.get("serverURL") or os.environ.get("BREAD_SHADER_VISUAL_REMOTE_URL") or "").strip()
    remote_adapter = (load("bread_persistent_remote_adapter",
                      root / "replaylab/tests/fixtures/shader_visual_debug_remote_smoke.py")
                      if endpoint else None)
    report = dict(schema="bread-shader-visual-persistent-smoke/v1", ok=False, pid=os.getpid(),
                  transport="IRemoteServer.OpenCapture/ReplayProxy" if endpoint else "local-replay",
                  serverURL=endpoint, localReplayFallback=False,
                  captures=[], sourceHashes={name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                  for name in ("replaylab/renderdoc_mcp_bridge/shader_visual_debug.py",
                               "renderdoc/replay/replay_controller.cpp",
                               "renderdoc/core/replay_proxy.cpp")})
    for ordinal, item in enumerate(config["captures"]):
        row = dict(name=item.get("name"), capture=item["capture"], ok=False)
        report["captures"].append(row)
        factory = None
        if remote_adapter is not None:
            evidence = dict(transport="IRemoteServer.OpenCapture/ReplayProxy", serverURL=endpoint,
                            remoteCaptureOpened=False, localReplayFallback=False,
                            checkedOperations=["ReplaceTargetShaderChecked",
                                               "RemoveTargetShaderReplacementChecked"])
            row["remote"] = evidence
            factory = lambda: remote_adapter._RemoteCapture(endpoint, evidence)
        try:
            run_capture(root, item, row, ordinal, capture_factory=factory)
        except BaseException:
            row.update(ok=False, error=traceback.format_exc())
        if row.get("remote") is not None:
            evidence = row["remote"]
            row["ok"] = bool(row["ok"] and evidence.get("remoteCaptureOpened") and
                             evidence.get("remoteCaptureClosed") and evidence.get("remoteConnectionClosed"))
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["ok"] = bool(report["captures"]) and all(row["ok"] for row in report["captures"])
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
