"""Replay a saved workspace compiler contract and verify exact preview rollback.

Run with util/perf/run_bundled_replay_python.ps1. The JSON file named by
BREAD_WORKSPACE_PREVIEW_INPUT supplies capture, workspace, recipe, selection,
eventId, shaderId and report paths. No project or workspace files are written.
The Host must produce the contract before running this replay-only adapter.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback
import types
import uuid
import faulthandler
import importlib.util
import gc

_config = json.loads(Path(os.environ["BREAD_WORKSPACE_PREVIEW_INPUT"]).read_text(encoding="utf-8"))
_stacks = open(_config["report"] + ".stacks.log", "w")
faulthandler.dump_traceback_later(20, repeat=True, file=_stacks)

import renderdoc as rd

REPO = Path(_config["sourceRoot"]) if _config.get("sourceRoot") else Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "replaylab"))
from renderdoc_mcp_bridge import shader_visual_debug as visual
_bridge_path = REPO / "replaylab/renderdoc_mcp_bridge"
_spec = importlib.util.spec_from_file_location("_bread_workspace_replay_probe_bridge",
    str(_bridge_path / "__init__.py"), submodule_search_locations=[str(_bridge_path)])
bridge = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bridge
_spec.loader.exec_module(bridge)


def main():
    config = json.loads(Path(os.environ["BREAD_WORKSPACE_PREVIEW_INPUT"]).read_text(encoding="utf-8"))
    report = {"ok": False, "eventId": config["eventId"], "checks": []}
    capture, controller, engine = None, None, None
    if not config.get("embeddedGui"):
        rd.InitialiseReplay(rd.GlobalEnvironment(), [])
    try:
        capture = rd.OpenCaptureFile()
        status = capture.OpenFile(config["capture"], "", None)
        if status != rd.ResultCode.Succeeded:
            raise RuntimeError(str(status))
        status, controller = capture.OpenCapture(rd.ReplayOptions(), None)
        if status != rd.ResultCode.Succeeded:
            raise RuntimeError(str(status))
        controller.SetFrameEvent(config["eventId"], True)
        recipe = json.loads(Path(config["recipe"]).read_text(encoding="utf-8"))
        selection = json.loads(Path(config["selection"]).read_text(encoding="utf-8"))
        workspace = Path(config["workspace"])
        source = (workspace / "source/shader.hlsl").read_text(encoding="utf-8")
        original_source = (workspace / "source/shader.hlsl").read_bytes()
        shader = controller.GetPipelineState().GetShader(rd.ShaderStage.Pixel)
        assert bridge._resource_id_int(shader) == int(config["shaderId"])
        target = controller.GetPipelineState().GetOutputTargets()[0]
        sub = rd.Subresource(max(0, target.firstMip), max(0, target.firstSlice), 0)

        def output():
            controller.SetFrameEvent(config["eventId"], True)
            return bytes(controller.GetTextureData(target.resource, sub))

        before = output()
        assert before
        bridge._capture_session_id = uuid.uuid4().hex
        bridge._active_capture_token = ("isolated-workspace-preview", config["capture"])
        bridge._replay = lambda callback: callback(controller)

        def contract(operation, params, binding):
            assert operation == "compiler-contract"
            assert params["source_workspace_id"] == recipe["workspaceId"]
            assert params["expected_raw_sha256"] == recipe["originalRawSha256"]
            return recipe

        bridge._shader_workspace_ui = types.SimpleNamespace(
            _save_client=types.SimpleNamespace(live_edit_operation=contract))
        driver = visual.ReplayDriver(bridge)
        engine = visual.Manager(driver)
        params = dict(ownerId="workspace-replay-smoke", requestGeneration=1,
            workspaceId=recipe["workspaceId"], workspacePath=str(workspace),
            expectedOriginalSha256=recipe["originalRawSha256"],
            captureBinding=dict(captureSessionId=bridge._capture_session_id, capturePath=config["capture"]),
            eventId=config["eventId"], shaderId=str(config["shaderId"]), stage="fragment",
            language=recipe["language"], entryPoint=recipe["entryPoint"], source=source,
            sourcePath=str(workspace / "source/shader.hlsl"),
            byteStart=selection["byteStart"], byteEnd=selection["byteEnd"],
            targetIndex=0, member=selection["member"])
        inspected = engine.operation("inspect", params)
        report["selectedExpression"] = source.encode("utf-8")[selection["byteStart"]:selection["byteEnd"]].decode("utf-8")
        assert inspected.get("status") != "error", inspected
        report["checks"].append("actual-capture-interface-and-expression")
        started = engine.operation("start", dict(params, requestGeneration=2))
        assert started.get("status") == "active", started
        preview = output()
        assert len(preview) == len(before) and preview != before
        report["checks"].append("bound-compiler-instrumented-gpu-output")
        again = engine.operation("start", dict(params, requestGeneration=3))
        assert again.get("status") == "active" and again.get("cacheHit"), again
        assert output() == preview
        report["checks"].append("repeated-preview-cache-and-stability")
        stopped = engine.operation("stop", dict(params, requestGeneration=4))
        assert stopped.get("status") == "stopped", stopped
        assert output() == before
        report["checks"].append("exact-original-output-restored")
        assert (workspace / "source/shader.hlsl").read_bytes() == original_source
        report.update(ok=True, sourceSha256=hashlib.sha256(original_source).hexdigest(),
            beforeSha256=hashlib.sha256(before).hexdigest(), previewSha256=hashlib.sha256(preview).hexdigest(),
            previewBytes=len(preview), timings=started.get("timings"))
    except Exception:
        report["error"] = traceback.format_exc()
    finally:
        if engine is not None:
            engine.shutdown()
            engine = None
            gc.collect()
        if controller is not None:
            controller.Shutdown()
        if capture is not None:
            capture.Shutdown()
        if not config.get("embeddedGui"):
            rd.ShutdownReplay()
        Path(config["report"]).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not report["ok"]:
        if config.get("embeddedGui"):
            sys.exit(1)
        raise RuntimeError(report.get("error", "Workspace preview replay failed"))
    if config.get("embeddedGui"):
        sys.exit(0)


try:
    main()
finally:
    faulthandler.cancel_dump_traceback_later()
    _stacks.close()
