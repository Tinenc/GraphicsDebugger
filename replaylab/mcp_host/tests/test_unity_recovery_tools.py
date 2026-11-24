"""First-party recovery tools write real files and preserve actual validation gates."""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from src import unity_restoration as restoration
from test_unity_restoration import prepared, review  # noqa: F401 - fixture dependency
from test_unity_restoration_tools import invoke, registered  # noqa: F401


ASSETS = [
    {"path": "Shaders/History.compute", "content": "#pragma kernel Initialize\n[numthreads(1,1,1)] void Initialize() {}"},
    {"path": "Runtime/History.cs", "content": "public class History {}"},
    {"path": "Data/Seed.bytes", "contentBase64": base64.b64encode(bytes(range(16))).decode()},
]
PROVENANCE = {"classification": "history-reconstruction", "evidence": ["Organized Shader samples prior irradiance."]}


def authoring_arguments(registered):
    return {"restoration_id": registered.task, "assets": ASSETS,
            "purpose": "Reconstruct missing history with explicit initialization and warmup",
            "provenance": PROVENANCE}


@pytest.mark.asyncio
async def test_registered_authoring_writes_real_multifile_assets_then_refreshes_editor(registered):
    async def refresh(project_path, name, arguments):
        assert Path(project_path) == registered.project
        assert name == "bread_refresh_assets" and arguments == {}
        root = registered.project / "Assets/BreadRecovery" / registered.task
        assert (root / "Shaders/History.compute").read_text() == ASSETS[0]["content"]
        assert (root / "Runtime/History.cs").read_text() == ASSETS[1]["content"]
        assert (root / "Data/Seed.bytes").read_bytes() == bytes(range(16))
        registered.calls.append((name, arguments))
        return {"success": True, "data": {"status": "refresh-requested", "isCompiling": True}}

    registered.service.call_tool = refresh
    result = await invoke(registered, "apply_unity_recovery_assets", **authoring_arguments(registered))
    assert "error" not in result, result
    authored = result["assetAuthoring"]
    assert authored["changedFiles"] == 3
    assert authored["provenanceAuthority"] == "agent-derived"
    assert authored["compiled"] is False and authored["executed"] is False
    assert authored["editorRefresh"]["isCompiling"] is True
    assert result["complete"] is False and result["comparison"] is None
    assert registered.calls == [("bread_refresh_assets", {})]
    for row in authored["files"]:
        assert hashlib.sha256(Path(row["path"]).read_bytes()).hexdigest() == row["sha256"]
    assert result["recoveryAttempts"][-1]["outcome"] == "authored"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["structured-error", "transport-error"])
async def test_refresh_failure_preserves_written_files_and_leaves_validation_pending(registered, failure_mode):
    async def refresh(project_path, name, arguments):
        assert name == "bread_refresh_assets"
        if failure_mode == "transport-error":
            raise RuntimeError("Editor reload interrupted the MCP connection")
        return {"success": False, "error": "History.cs(4,12): error CS0103: Unknown input binding"}

    registered.service.call_tool = refresh
    result = await invoke(registered, "apply_unity_recovery_assets", **authoring_arguments(registered))
    assert "error" not in result, "A recoverable refresh failure must return the retained authoring receipt"
    authored = result["assetAuthoring"]
    assert authored["changedFiles"] == 3 and authored["error"]
    assert not authored["compiled"] and not authored["executed"]
    assert all(Path(row["path"]).is_file() for row in authored["files"])
    assert Path(authored["manifestPath"]).is_file()
    assert result["complete"] is False and result["comparison"] is None
    assert result["status"] == "needs-repair"
    assert result["recoveryAttempts"][-1]["failed"] is True
    assert result["recoveryPlan"]["routes"], "A compile/connection failure needs alternative repair routes"


@pytest.mark.asyncio
async def test_retry_identical_assets_is_idempotent_and_can_retry_editor_refresh(registered):
    async def refresh(project_path, name, arguments):
        registered.calls.append((name, arguments))
        return {"success": True, "data": {"status": "refresh-requested"}}

    registered.service.call_tool = refresh
    first = await invoke(registered, "apply_unity_recovery_assets", **authoring_arguments(registered))
    again = await invoke(registered, "apply_unity_recovery_assets", **authoring_arguments(registered))
    assert again["assetAuthoring"]["changedFiles"] == 0 and again["assetAuthoring"]["idempotent"]
    assert again["assetAuthoring"]["revision"] == first["assetAuthoring"]["revision"]
    assert again["revision"] == first["revision"]
    assert registered.calls == [("bread_refresh_assets", {}), ("bread_refresh_assets", {})]
    assert not again["complete"]


@pytest.mark.asyncio
async def test_recovery_asset_authoring_can_continue_without_an_editor_connection(registered):
    async def unexpected(*args, **kwargs):
        raise AssertionError("refresh_editor=False must not require the Editor")

    registered.service.call_tool = unexpected
    result = await invoke(registered, "apply_unity_recovery_assets", **authoring_arguments(registered), refresh_editor=False)
    assert result["assetAuthoring"]["changedFiles"] == 3
    assert "editorRefresh" not in result["assetAuthoring"]
    assert result["assetAuthoring"]["nextAction"]["arguments"]["tool_name"] == "bread_refresh_assets"
    assert not result["complete"]


@pytest.mark.asyncio
async def test_capability_inspection_failure_keeps_recovery_routes_available(registered):
    async def missing_tool(project_path, name, arguments):
        assert Path(project_path) == registered.project
        assert name == "bread_inspect_rendering" and arguments == {}
        return {"success": False, "error": "Provider has no bread_inspect_rendering tool"}

    registered.service.call_tool = missing_tool
    result = await invoke(registered, "plan_unity_restoration_recovery", restoration_id=registered.task)
    assert "error" not in result, result
    assert "bread_inspect_rendering" in result["capabilityInspectionError"]
    assert result["recoveryPlan"]["terminal"] is False
    routes = {row["id"]: row for row in result["recoveryPlan"]["routes"]}
    assert routes["custom-versioned-adapter"]["state"] == "available"
    assert "inspect-and-repair-connection" in routes
    assert not result["complete"]


@pytest.mark.asyncio
async def test_capability_readback_disables_compute_without_disabling_other_implementations(registered):
    with registered.store.transaction(registered.task) as state:
        state["binding"]["requestGaps"] = [{"code": "compute-unsupported"}]

    async def capabilities(project_path, name, arguments):
        assert name == "bread_inspect_rendering"
        return {"success": True, "data": {"renderPipeline": "Built-in", "unityVersion": "2021.3.1f1",
            "graphicsDeviceType": "OpenGLES3", "supportsComputeShaders": False}}

    registered.service.call_tool = capabilities
    result = await invoke(registered, "plan_unity_restoration_recovery", restoration_id=registered.task)
    routes = {row["id"]: row for row in result["recoveryPlan"]["routes"]}
    assert routes["native-compute-adapter"]["state"] == "capability-unavailable"
    assert routes["raster-equivalent-adapter"]["state"] == "available"
    assert routes["cpu-staged-adapter"]["state"] == "available"
    assert registered.store.load(registered.task)["targetCapabilities"]["supportsComputeShaders"] is False


@pytest.mark.asyncio
async def test_registered_visual_goal_is_fixed_and_idempotent(registered):
    with registered.store.transaction(registered.task) as state:
        state["acceptance"] = restoration._acceptance({"mode": "strict-pixel", "absoluteTolerance": 0})
    first = await invoke(registered, "use_unity_visual_fidelity_goal", restoration_id=registered.task)
    again = await invoke(registered, "use_unity_visual_fidelity_goal", restoration_id=registered.task)
    assert first["acceptance"] == restoration._acceptance({"mode": "visual-fidelity"})
    assert first["previousAcceptance"]["mode"] == "strict-pixel"
    assert again["acceptance"] == first["acceptance"]
    assert again["revision"] == first["revision"]
    assert registered.calls == []


@pytest.mark.asyncio
async def test_direct_registered_failed_render_revokes_a_previously_complete_verdict(registered):
    before = await invoke(registered, "render_unity_restoration", restoration_id=registered.task)
    assert before["comparison"]["withinTolerance"]
    assert review(registered.store, registered.task)["complete"]

    async def fail(project_path, name, arguments):
        assert name == "bread_render_capture"
        return {"success": False, "error": "Current compute dispatch failed during rendering"}

    registered.service.call_tool = fail
    result = await invoke(registered, "render_unity_restoration", restoration_id=registered.task)
    assert "Current compute dispatch failed" in result["error"]
    current = registered.store.status(registered.task)
    assert not current["complete"]
    assert current["inspection"] is None and current["comparison"] is None and current["portingReview"] is None
    assert current["lastToolFailure"]["tool"] == "render_unity_restoration"
    assert current["recoveryPlan"]["routes"]
