"""Host entry points preserve export identity and do not repeat capture work."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src import server, unity_integration, unity_restore, unity_visual_validation, unity_render_plan


@pytest.fixture
def service(monkeypatch):
    result = SimpleNamespace(
        inspect_project=Mock(return_value={"status": "ready-to-prepare", "connected": False}),
        prepare_project=Mock(return_value={"status": "running", "jobId": "connect-1"}),
        job_status=Mock(return_value={"status": "running", "jobId": "connect-1"}),
        cancel_job=Mock(return_value={"status": "running", "cancelRequested": True}),
        call_tool=AsyncMock(return_value={"status": "partial", "visualEquivalenceVerified": False}),
        start_import=AsyncMock(return_value={"status": "running", "jobId": "import-1"}),
    )
    monkeypatch.setattr(unity_integration, "shared_service", lambda: result)
    monkeypatch.setattr(server.ipc, "call", Mock(side_effect=AssertionError("Unity tools must not call RenderDoc")))
    return result


@pytest.mark.asyncio
async def test_inspection_does_not_prepare_or_launch(service):
    response = json.loads(await server.inspect_unity_project("project"))
    assert response["connected"] is False
    service.inspect_project.assert_called_once_with("project", None)
    service.prepare_project.assert_not_called()


@pytest.mark.asyncio
async def test_visual_comparison_does_not_prepare_unity_or_replay(service, monkeypatch):
    compare = Mock(return_value={"status": "compared", "withinTolerance": True,
                                 "visualEquivalenceVerified": False})
    monkeypatch.setattr(unity_visual_validation, "compare_render_images", compare)
    result = json.loads(await server.compare_unity_render_images("a", "b", "left", "right"))
    compare.assert_called_once_with("a", "b", "left", "right", channels="RGB",
        absolute_tolerance=1 / 255, max_changed_pixel_fraction=0.0)
    assert result["withinTolerance"] and not result["visualEquivalenceVerified"]
    service.prepare_project.assert_not_called()


def test_plan_validation_does_not_claim_project_implementation(service, monkeypatch):
    review = Mock(return_value={"status": "valid", "implementedInUnity": False})
    monkeypatch.setattr(unity_render_plan, "validate_render_plan", review)
    result = json.loads(server.validate_unity_render_plan({"passes": []}))
    assert not result["implementedInUnity"]
    service.call_tool.assert_not_called()


def test_prepare_poll_cancel_share_the_same_service(service):
    assert json.loads(server.start_unity_project_connection("project", start_editor=False))["jobId"] == "connect-1"
    service.prepare_project.assert_called_once_with("project", None, False, reconnect=False)
    assert json.loads(server.get_unity_integration_job("connect-1"))["status"] == "running"
    assert json.loads(server.cancel_unity_integration_job("connect-1"))["cancelRequested"]
    assert service.prepare_project.call_count == 1


@pytest.mark.asyncio
async def test_unity_tool_call_preserves_target_and_partial_result(service):
    response = json.loads(await server.call_unity_project_tool("project", "manage_material", {"action": "set_property"}))
    service.call_tool.assert_awaited_once_with("project", "manage_material", {"action": "set_property"})
    assert response["status"] == "partial"
    assert response["visualEquivalenceVerified"] is False


@pytest.mark.asyncio
async def test_import_reuses_finalized_managed_package_without_recapture(service, monkeypatch):
    manager = SimpleNamespace(status=Mock(return_value={"status": "finalized", "packagePath": "managed/package"}))
    monkeypatch.setattr(server, "_RESOURCE_EXPORT_MANAGER", manager)
    prepare = Mock(return_value={"requestPath": "project/Library/Bread/Requests/one.json", "gaps": [{"code": "unity_shader_required"}]})
    monkeypatch.setattr(unity_restore, "prepare_unity_import", prepare)
    response = json.loads(await server.import_resource_export_to_unity("operation-1", "project"))
    manager.status.assert_called_once_with("operation-1")
    prepare.assert_called_once_with("managed/package", "project")
    service.start_import.assert_awaited_once_with("project", prepare.return_value["requestPath"], None)
    assert response["jobId"] == "import-1"
    assert response["importRequest"]["gaps"][0]["code"] == "unity_shader_required"


@pytest.mark.asyncio
async def test_unfinalized_export_never_starts_import(service, monkeypatch):
    monkeypatch.setattr(server, "_RESOURCE_EXPORT_MANAGER", SimpleNamespace(status=lambda _: {"status": "captured"}))
    response = json.loads(await server.import_resource_export_to_unity("operation-1", "project"))
    assert response["status"] == "export-not-finalized"
    service.start_import.assert_not_awaited()
    service.prepare_project.assert_not_called()


@pytest.mark.asyncio
async def test_integrity_error_never_starts_editor(service, monkeypatch):
    monkeypatch.setattr(server, "_RESOURCE_EXPORT_MANAGER", SimpleNamespace(status=lambda _: {"status": "finalized", "packagePath": "package"}))
    monkeypatch.setattr(unity_restore, "prepare_unity_import", Mock(side_effect=unity_restore.UnityRestoreError("export package changed")))
    response = json.loads(await server.import_resource_export_to_unity("operation-1", "project"))
    assert response["type"] == "UnityRestoreError"
    service.start_import.assert_not_awaited()


@pytest.mark.asyncio
async def test_export_revision_reuses_verified_resources_without_editor_or_replay(service, monkeypatch):
    fork = Mock(return_value={"schema": "bread-resource-export-fork/v1", "operationId": "new-operation",
                              "needsCapture": False, "replayPerformed": False})
    monkeypatch.setattr(server, "_RESOURCE_EXPORT_MANAGER", SimpleNamespace(fork_capture=fork))
    result = json.loads(await server.fork_resource_export("parent", "verified-bundle-hash", "repair-mappings"))
    fork.assert_called_once_with("parent", "verified-bundle-hash", "repair-mappings")
    assert result["needsCapture"] is False and result["replayPerformed"] is False
    service.call_tool.assert_not_awaited()
    service.prepare_project.assert_not_called()
