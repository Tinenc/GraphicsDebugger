"""Planning reads observations only; no replay mutation, scans, or setup jobs."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

import pytest

from src import server


def _context(event_id=42):
    return {
        "schema": "renderdoc-ultra-current-context/v1", "schemaVersion": 1,
        "capture": {"loaded": True, "captureId": "a" * 64,
                    "captureSessionId": "b" * 32, "path": "C:/Capture/frame.rdc", "api": "D3D11"},
        "events": {"currentEventId": event_id, "selectedEventId": event_id + 1},
        "shaders": {"bound": [{"stage": "fragment", "resourceId": "123",
                               "entryPoint": "main", "rawSha256": "c" * 64,
                               "capturedOriginalIdentity": {
                                   "stage": "fragment", "resourceId": "123", "entryPoint": "main",
                                   "rawSha256": "d" * 64, "captureSessionId": "b" * 32}}]},
    }


def _project():
    return SimpleNamespace(status=lambda: {"assetRootConfigured": False})


def _steps(result):
    return {step["id"]: step for step in result["steps"]}


def test_plan_uses_current_original_shader_and_never_fabricates_capture_fingerprint():
    with mock.patch.object(server, "_active_live_context", return_value=_context()) as context, \
            mock.patch.object(server, "_active_project_service", return_value=_project()), \
            mock.patch.object(server.ipc, "call", side_effect=AssertionError("no additional bridge call")):
        result = json.loads(server.get_draw_workflow_plan("organize_shader"))
    context.assert_called_once_with(allow_missing_capture_id=True, require_event_state=True)
    assert result["execution"] == "not_started"
    assert result["applied"] is False
    assert result["observedContext"]["eventId"] == 42
    assert result["observedContext"]["shaderSha256"] == "d" * 64
    assert result["observedContext"]["shaderIdentityObserved"] is True
    assert "captureFingerprint" in _steps(result)["capture_evidence"]["requiredEvidence"]
    assert all(step["status"] != "reuse" for step in result["steps"])
    assert result["target"] is None


@pytest.mark.parametrize("event_id,stage", [(99, "ps"), (42, "vs")])
def test_explicit_different_target_does_not_reuse_ui_shader_hash(event_id, stage):
    with mock.patch.object(server, "_active_live_context", return_value=_context()), \
            mock.patch.object(server, "_active_project_service", return_value=_project()), \
            mock.patch.object(server.ipc, "call", side_effect=AssertionError("do not select another event")):
        result = json.loads(server.get_draw_workflow_plan("organize_shader", event_id=event_id, stage=stage))
    assert result["observedContext"]["eventId"] == event_id
    assert result["observedContext"]["shaderIdentityObserved"] is False
    assert "shaderSha256" not in result["observedContext"]
    assert "shaderSha256" in _steps(result)["shader_source"]["requiredEvidence"]


def test_missing_capture_or_project_still_returns_advice_not_success_or_blocked_pipeline():
    with mock.patch.object(server, "_active_live_context", side_effect=RuntimeError("No RenderDoc capture is loaded")), \
            mock.patch.object(server, "_active_project_service", side_effect=RuntimeError("no active project")):
        result = json.loads(server.get_draw_workflow_plan("export_unity", source_engine="custom", project_path="D:/Unity"))
    assert "error" not in result
    assert result["sourceEngine"] == "custom"
    assert result["execution"] == "not_started"
    assert result["target"] == {"engine": "unity", "pipeline": "unverified"}
    assert {item["code"] for item in result["diagnostics"]} == {
        "capture_context_unavailable", "project_context_unavailable"}
    assert _steps(result)["asset_lookup"]["status"] == "optional_skip"
    assert _steps(result)["resource_export"]["status"] == "run"


def test_validation_advice_never_touches_capture_or_source_asset_project():
    with mock.patch.object(server, "_active_live_context", side_effect=AssertionError("no capture required")), \
            mock.patch.object(server, "_active_project_service", side_effect=AssertionError("no source project required")):
        result = json.loads(server.get_draw_workflow_plan("validate_unity", project_path="D:/Unity"))
    assert set(_steps(result)) == {"unity_connection", "unity_validation"}
    assert result["observedContext"]["captureContextRequested"] is False
    assert result["observedContext"]["captureLoaded"] is None


def test_saved_reader_selection_infers_source_engine_but_not_current_availability(tmp_path):
    dictionary = tmp_path / "dictionary"
    dictionary.mkdir()
    assets = tmp_path / "Assets"
    assets.mkdir()
    selection = {"assetRoot": str(assets), "engine": "unreal",
                 "selections": {"unreal": {"properties": {"reader": "uex", "parsed": 3}}}}
    (dictionary / server.ASSET_READER_SELECTION_FILENAME).write_text(json.dumps(selection), encoding="utf-8")
    project = SimpleNamespace(status=lambda: {
        "assetRootConfigured": True, "assetRootAvailable": True, "assetRoot": str(assets)})
    with mock.patch.object(server, "_active_live_context", return_value=_context()), \
            mock.patch.object(server, "_active_project_service", return_value=project), \
            mock.patch.object(server, "_active_asset_catalog_context", return_value=(
                project, {}, assets, dictionary / "catalog.sqlite")) as catalog, \
            mock.patch.object(server, "_load_asset_reader_selection", side_effect=AssertionError("no mkdir side effect")):
        result = json.loads(server.get_draw_workflow_plan("resolve_names"))
    catalog.assert_called_once_with(require_catalog=False, create_dictionary=False)
    assert result["sourceEngine"] == "unreal"
    assert result["observedContext"]["selectedReaderId"] == "uex"
    assert _steps(result)["asset_lookup"]["reason"] == "reader_availability_unknown"
    assert _steps(result)["name_resolution"]["status"] == "run"


def test_unavailable_game_root_skips_assistance_without_touching_index():
    project = SimpleNamespace(status=lambda: {
        "assetRootConfigured": True, "assetRootAvailable": False, "assetRoot": "D:/Offline"})
    with mock.patch.object(server, "_active_live_context", return_value=_context()), \
            mock.patch.object(server, "_active_project_service", return_value=project), \
            mock.patch.object(server, "_active_asset_catalog_context", side_effect=AssertionError("no index needed")):
        result = json.loads(server.get_draw_workflow_plan("organize_shader"))
    assert _steps(result)["asset_lookup"]["status"] == "optional_skip"
    assert _steps(result)["shader_organization"]["status"] == "run"


def test_plan_tool_annotations_and_schema_reject_caller_artifact_claims():
    registration = next(item for item in server.mcp._registrations if item[0].__name__ == "get_draw_workflow_plan")
    assert registration[2]["annotations"] == {
        "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
    with pytest.raises(TypeError):
        server.get_draw_workflow_plan("organize_shader", artifacts=[{"status": "complete"}])
    assert "error" in json.loads(server.get_draw_workflow_plan("organize_shader", event_id=True))
