"""A failed first reconstruction remains recoverable in the next task Host."""
import json
from pathlib import Path
from unittest import mock

import pytest

from src.active_projects import ActiveReplayProjectService
from src.projects import ReplayProjectService
from src.shader_workspace.errors import ToolExecutionError
from tests import test_shader_workspace_mcp as fixture_module


@pytest.fixture
def failed_workspace():
    fixture_module.ShaderWorkspaceMcpFlowTests.setUpClass()
    fixture = fixture_module.ShaderWorkspaceMcpFlowTests()
    fixture.setUp()
    server = fixture.server
    active = ActiveReplayProjectService(fixture.root / "active.json", fixture.root / "projects")
    active.ensure_pending("Reconstruction retry")
    active.configure_scope(game_id="game", build_id="build", api_family="d3d11")
    fixture.bridge.instance_id = "123-" + "b" * 32
    fixture.bridge.context = {
        "schema": "renderdoc-ultra-current-context/v1", "schemaVersion": 1,
        "capture": {"loaded": True, "captureSessionId": fixture.bridge.capture_session_id,
                    "captureId": "", "path": "D:/captures/frame.rdc", "api": "D3D11"},
        "events": {"currentEventId": 321, "selectedEventId": 321},
        "shaders": {"bound": [{"stage": "fragment", "resourceId": fixture.key.resource_id,
                               "entryPoint": "main", "rawSha256": fixture.key.raw_sha256}]},
    }
    server._ACTIVE_CAPTURE_CATALOG_BINDINGS.clear()
    try:
        with mock.patch.object(server, "_ACTIVE_PROJECT_SERVICE", active):
            yield fixture, active
    finally:
        server._ACTIVE_CAPTURE_CATALOG_BINDINGS.clear()
        fixture.tearDown()


@pytest.mark.parametrize("different_revision", [False, True])
def test_retry_imports_exact_original_catalog_and_retains_source_empty_workspace(failed_workspace, different_revision):
    fixture, active = failed_workspace
    server = fixture.server
    service = server._shader_workspace_service()
    workspace = Path(active.status()["paths"]["shaderWorkspaces"]) / "failed-first-prepare"
    native_call = fixture.bridge.call

    def first_export(method, params, timeout=None):
        result = native_call(method, params, timeout)
        if method == "export_shader_catalog" and different_revision:
            path = Path(result["catalogPath"])
            catalog = json.loads(path.read_text(encoding="utf8"))
            catalog["errors"] = [{"code": "optional-debug-data-unavailable"}]
            path.write_text(json.dumps(catalog), encoding="utf8")
        return result

    with (mock.patch.object(server, "_shader_workspace_service", return_value=service),
          mock.patch.object(fixture.bridge, "call", side_effect=first_export),
          mock.patch.object(service, "decompile", side_effect=ToolExecutionError("native decompiler rejected input"))):
        first = json.loads(server.prepare_shader_workspace(
            321, "fragment", str(workspace), editable_format="hlsl", attempt_reconstruction=True))
    assert "error" not in first, first
    before = service.status(workspace)
    assert before["source"]["present"] is False
    manifest_bytes = (workspace / "manifest.json").read_bytes()
    original_revision = ReplayProjectService._capture_status_catalog_sha256(before["capture"])
    assert before["capture"]["catalogSha256"] != original_revision

    # A new task has no in-memory catalog, exactly like the native DXIL retry.
    server._ACTIVE_CAPTURE_CATALOG_BINDINGS.clear()
    def retry_call(method, params, timeout=None):
        # The Bridge can notify the persistent Host during any replay call.
        # Its restore checkpoint must be able to read the active selection.
        assert not active._selection_lock_path.exists(), method
        return native_call(method, params, timeout)
    with (mock.patch.object(server, "_shader_workspace_service", return_value=service),
          mock.patch.object(fixture.bridge, "call", side_effect=retry_call),
          mock.patch.object(service, "decompile", side_effect=AssertionError("retry did not request decompilation"))):
        retried = json.loads(server.prepare_shader_workspace(
            321, "fragment", str(workspace), editable_format="hlsl", admit_existing=True,
            expected_raw_sha256=fixture.key.raw_sha256,
            expected_capture_session_id=fixture.bridge.capture_session_id))
    assert "error" not in retried, retried
    result = retried["result"]
    assert result["created"]["admittedExisting"] is True
    assert result["workspaceStatus"]["source"]["present"] is False
    assert result["projectSourceReuse"] is None
    assert result["workspaceStatus"]["revision"] == before["revision"]
    assert (workspace / "manifest.json").read_bytes() == manifest_bytes
    plan = active.replay_service().reuse_plan(before["capture"]["captureId"], original_revision)
    assert plan["catalogSha256"] == original_revision
    assert not any(method == "apply_shared_shader_replacement" for method, _ in fixture.bridge.calls)
