"""Registered MCP tools preserve the Unity Editor's actual request/readback contract.

These tests run the real persistent store and PNG comparison through FastMCP.
The fake Editor enforces BreadCaptureTools' project-local output restriction;
no installed Editor or user project is started or modified.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from mcp.server.fastmcp import FastMCP
import pytest

from src import unity_integration, unity_restoration_tools
from test_unity_restoration import png, prepared  # noqa: F401


@pytest.fixture
def registered(prepared, monkeypatch):
    store, task, observation, sources, bundle, project = prepared
    state = store.load(task)
    old_request = Path(state["binding"]["requestPath"])
    request = old_request.with_name(state["binding"]["requestSha256"] + ".json")
    old_request.rename(request)
    with store.transaction(task) as current:
        current["binding"]["requestPath"] = str(request)

    def unexpected_capture(*args, **kwargs):
        raise AssertionError("Existing reference must be reused without recapturing")

    host = SimpleNamespace(
        _ok_compact=lambda value: json.dumps(value),
        _err=lambda error: json.dumps({"error": str(error), "type": type(error).__name__}),
        _resource_export_live_identity=Mock(side_effect=unexpected_capture),
        _capture_event_texture_evidence_receipt=Mock(side_effect=unexpected_capture),
    )
    calls = []

    async def call_tool(project_path, name, arguments):
        assert Path(project_path).resolve() == project.resolve()
        assert Path(arguments["request_path"]).resolve() == request.resolve()
        assert request.name == state["binding"]["requestSha256"] + ".json"
        assert request.is_relative_to(project / "Library" / "Bread" / "Requests")
        calls.append((name, copy.deepcopy(arguments)))
        observed = copy.deepcopy(observation)
        observed.pop("image", None)
        if name == "bread_render_capture":
            assert set(arguments) == {"request_path", "width", "height", "output_path", "display_mapping", "target_output"}
            actual = store.load(task)
            assert arguments["display_mapping"] == actual["displayMapping"]
            assert arguments["target_output"] == actual["targetOutput"]
            assert (arguments["width"], arguments["height"]) == (8, 8)
            output = Path(arguments["output_path"]).resolve()
            allowed = project / "Library" / "Bread" / "Renders"
            # BreadCaptureTools.cs rejects evidence paths in the host task store.
            assert output.is_relative_to(allowed), "Editor requires project-local Library/Bread/Renders output"
            assert output.suffix == ".png" and not output.exists()
            output.parent.mkdir(parents=True, exist_ok=True)
            observed["image"] = png(output)
            observed["displayMapping"] = copy.deepcopy(arguments["display_mapping"])
            observed["targetOutput"] = copy.deepcopy(arguments["target_output"])
        else:
            assert name in {"bread_inspect_capture", "bread_apply_capture_configuration"}
            assert set(arguments) == {"request_path"}
        return {"success": True, "data": observed}

    service = SimpleNamespace(call_tool=call_tool)
    monkeypatch.setattr(unity_integration, "shared_service", lambda: service)
    mcp = FastMCP("Bread restoration contract test")
    unity_restoration_tools.register_tools(mcp, host)
    return SimpleNamespace(mcp=mcp, host=host, store=store, task=task,
                           calls=calls, service=service, project=project)


async def invoke(registered, name, **arguments):
    content = await registered.mcp.call_tool(name, arguments)
    assert isinstance(content, (list, tuple))
    if isinstance(content, tuple):
        content = content[0]  # FastMCP also returns its structured result envelope.
    return json.loads("".join(block.text for block in content if block.type == "text"))


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_mapping", [False, True])
async def test_registered_render_obeys_editor_output_and_mapping_contract(registered, explicit_mapping):
    if explicit_mapping:
        with registered.store.transaction(registered.task) as state:
            state["displayMapping"] = {"channel": "g", "range_mode": "explicit",
                "black_point": -2.0, "white_point": 6.0, "flip_y": True, "value_decode": "native"}
            state["targetOutput"] = {"kind": "global-texture", "name": "_BreadLightingOutput"}
    first = await invoke(registered, "render_unity_restoration", restoration_id=registered.task)
    assert "error" not in first, first
    assert first["comparison"]["withinTolerance"]
    assert first["comparison"]["rendered"]["path"] == registered.calls[0][1]["output_path"]
    second = await invoke(registered, "render_unity_restoration", restoration_id=registered.task)
    assert "error" not in second, second
    assert second["iterations"][-1]["progress"] == "unchanged"
    assert registered.calls[0][1]["output_path"] != registered.calls[1][1]["output_path"]
    registered.host._resource_export_live_identity.assert_not_called()
    registered.host._capture_event_texture_evidence_receipt.assert_not_called()


@pytest.mark.asyncio
async def test_registered_refresh_uses_exact_reference_selection_before_render(registered):
    state = registered.store.load(registered.task)
    registered.host._resource_export_live_identity.side_effect = None
    registered.host._resource_export_live_identity.return_value = ({}, "unused", state["capture"]["captureSessionId"], "Vulkan")
    registered.host._capture_event_texture_evidence_receipt.side_effect = None
    registered.host._capture_event_texture_evidence_receipt.return_value = json.dumps({
        "eventId": state["eventId"], "capture": state["capture"], "image": state["reference"]["image"]})
    result = await invoke(registered, "render_unity_restoration", restoration_id=registered.task, refresh_reference=True)
    assert "error" not in result, result
    assert result["comparison"]["withinTolerance"]
    registered.host._capture_event_texture_evidence_receipt.assert_called_once_with(**state["referenceSelection"])
    assert [name for name, _ in registered.calls] == ["bread_render_capture"]


@pytest.mark.asyncio
@pytest.mark.parametrize("repair", [False, True])
async def test_registered_check_uses_matching_editor_methods_and_request(registered, repair):
    result = await invoke(registered, "check_unity_restoration", restoration_id=registered.task, repair_configuration=repair)
    assert "error" not in result, result
    assert result["inspection"]["requestSha256"] == registered.store.load(registered.task)["binding"]["requestSha256"]
    expected = ["bread_apply_capture_configuration", "bread_inspect_capture"] if repair else ["bread_inspect_capture"]
    assert [name for name, _ in registered.calls] == expected
    assert result["comparison"] is None


@pytest.mark.asyncio
async def test_registered_editor_failure_does_not_generate_comparison(registered):
    async def fail(project, name, arguments):
        return {"success": False, "data": {"status": "needs-repair", "code": "unsupported_target_output"}}

    registered.service.call_tool = fail
    result = await invoke(registered, "render_unity_restoration", restoration_id=registered.task)
    assert "unsupported_target_output" in result["error"]
    assert registered.store.load(registered.task)["comparison"] is None


@pytest.mark.asyncio
async def test_registered_status_reads_store_without_unity_call(registered):
    result = await invoke(registered, "get_unity_restoration_status", restoration_id=registered.task)
    assert result["restorationId"] == registered.task
    assert registered.calls == []
