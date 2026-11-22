"""The API/ACP Host exposes bounded read-only material output decoding."""
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from src import server
from test_material_shader_outputs import document


def setup_file(tmp_path, monkeypatch):
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(document()), encoding="utf-8")
    monkeypatch.setattr(server, "_evidence_roots", lambda: [tmp_path])
    monkeypatch.setattr(server, "ipc", SimpleNamespace(call=lambda *a, **kw: pytest.fail("offline decode must not touch replay")))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def run(path, sha, **kwargs):
    return json.loads(asyncio.run(server.decode_material_shader_outputs(
        str(path), sha, "SP_PCD3D_SM6", **kwargs)))


def test_readonly_receipt_route_pages_outputs_without_replay(tmp_path, monkeypatch):
    path, sha = setup_file(tmp_path, monkeypatch)
    result = run(path, sha, start_output=1, max_outputs=1)
    assert "error" not in result, result
    assert result["outputCount"] == 3 and result["outputs"][0]["byteOffset"] == 8
    assert result["page"] == {"startOutput": 1, "returnedOutputs": 1, "nextStartOutput": 2}
    assert result["evidenceFiles"][0]["sha256"] == sha
    assert not result["captureComparisonAvailable"] and not result["applied"]
    assert result["valueConsistentVariantIndexes"] == []
    assert result["variants"][0]["valueConsistent"] is None


def test_hash_and_authorized_roots_are_enforced(tmp_path, monkeypatch):
    path, sha = setup_file(tmp_path, monkeypatch)
    assert "error" in run(path, "0" * 64)
    monkeypatch.setattr(server, "_evidence_roots", lambda: [tmp_path / "unrelated"])
    assert "error" in run(path, sha)


def test_changed_file_and_oversize_evidence_are_refused(tmp_path, monkeypatch):
    path, sha = setup_file(tmp_path, monkeypatch)
    path.write_bytes(b"{}")
    assert "error" in run(path, sha)
    with path.open("wb") as stream:
        stream.truncate(8 * 1024 * 1024 + 1)
    assert "error" in run(path, sha)


def test_exact_hash_bound_override_chain_is_consumed(tmp_path, monkeypatch):
    from test_material_shader_outputs import instance
    path, sha = setup_file(tmp_path, monkeypatch)
    leaf = instance("MI_Leaf", "M_Base", 7)
    leaf_path = tmp_path / "leaf.json"
    leaf_path.write_text(json.dumps(leaf["document"]), encoding="utf-8")
    chain = [{"path": str(leaf_path), "sha256": hashlib.sha256(leaf_path.read_bytes()).hexdigest(),
              "assetPath": leaf["assetPath"]}]
    result = run(path, sha, material_chain_json=json.dumps(chain))
    assert "error" not in result, result
    assert result["outputs"][1]["components"][0]["value"] == 7
    chain[0]["sha256"] = "0" * 64
    assert "error" in run(path, sha, material_chain_json=json.dumps(chain))


def test_hash_bound_embedded_override_preview_is_consumed(tmp_path, monkeypatch):
    from test_material_shader_outputs import instance

    path, sha = setup_file(tmp_path, monkeypatch)
    leaf = instance("MI_Leaf", "M_Base", 9)
    combined = {
        "schema": "bread-material-shader-evidence/v1",
        "materials": [{
            "objectName": "MI_Leaf",
            "materialPreview": leaf["document"][0],
            "variants": [],
        }],
    }
    leaf_path = tmp_path / "leaf-combined.json"
    leaf_path.write_text(json.dumps(combined), encoding="utf-8")
    chain = [{
        "path": str(leaf_path),
        "sha256": hashlib.sha256(leaf_path.read_bytes()).hexdigest(),
        "assetPath": leaf["assetPath"],
    }]

    result = run(path, sha, material_chain_json=json.dumps(chain))

    assert "error" not in result, result
    assert result["outputs"][1]["components"][0]["value"] == 9


@pytest.mark.parametrize("kwargs", [{"max_outputs": 129}, {"max_outputs": 0}, {"start_output": -1},
                                    {"material_chain_json": "{}"}, {"material_chain_json": "[{}]"},
                                    {"bind": -1}, {"event_id": -1}])
def test_input_bounds_do_not_fall_through_to_replay(tmp_path, monkeypatch, kwargs):
    path, sha = setup_file(tmp_path, monkeypatch)
    assert "error" in run(path, sha, **kwargs)


def test_capture_read_is_exact_bind_and_checks_session_afterward(tmp_path, monkeypatch):
    path, sha = setup_file(tmp_path, monkeypatch)
    calls = []
    context = {"capture": {"captureSessionId": "a" * 32, "path": "capture.rdc", "api": "D3D12"}}
    monkeypatch.setattr(server, "_active_live_context", lambda **kw: context)
    def call(method, params, **kw):
        calls.append((method, params))
        return {"constantBuffers": [{"bind": 5, "space": 0, "floats": [70, 50, 0, 0]}]}
    monkeypatch.setattr(server, "ipc", SimpleNamespace(call=call))
    result = run(path, sha, event_id=7, bind=5)
    assert "error" not in result, result
    assert result["outputCount"] == 3 and result["captureEvidence"]["eventId"] == 7
    assert calls == [("get_constant_buffer_values", {"event_id": 7, "stage": "fragment"})]
    assert "error" in run(path, sha, event_id=7, bind=6)
    contexts = iter([context, {"capture": {**context["capture"], "captureSessionId": "b" * 32}}])
    monkeypatch.setattr(server, "_active_live_context", lambda **kw: next(contexts))
    assert "error" in run(path, sha, event_id=7, bind=5)


def test_decode_returns_publishable_combined_row_and_physical_layout_receipt(
    tmp_path, monkeypatch
):
    from src.shader_workspace import aliases

    path, sha = setup_file(tmp_path, monkeypatch)
    context = {
        "capture": {
            "captureId": "capture-a",
            "captureSessionId": "a" * 32,
            "path": "capture.rdc",
            "api": "D3D12",
        }
    }
    block = {
        "block": "Material",
        "bind": 5,
        "space": 0,
        "members": [{"name": "unknown", "offset": 0}],
        "leaves": [{"path": "unknown[0]", "offset": 0, "byteSize": 16}],
    }
    raw_sha = "a" * 64
    workspace = tmp_path / "workspace"
    workspace_status = {"workspacePath": str(workspace), "workspaceId": "b" * 32}
    status_calls = []
    identity_calls = []
    monkeypatch.setattr(server, "_active_live_context", lambda **kw: context)
    monkeypatch.setattr(
        server,
        "_active_bound_shader",
        lambda *args, **kwargs: {"rawSha256": raw_sha},
    )
    def lightweight_status(path_value):
        status_calls.append(path_value)
        return workspace_status

    def cbuffer_identity(path_value, **kwargs):
        identity_calls.append((path_value, kwargs))
        assert kwargs["status"] is workspace_status
        assert kwargs["status_loader"] is lightweight_status
        return {
            "stage": "fragment",
            "rawSha256": raw_sha,
            "blocks": [block],
        }

    monkeypatch.setattr(server, "_alias_publication_workspace_status", lightweight_status)
    monkeypatch.setattr(
        server,
        "_shader_workspace_service",
        lambda: pytest.fail("material decode loaded ShaderWorkspaceService"),
    )
    monkeypatch.setattr(
        server,
        "_shader_toolchain_catalog",
        lambda: pytest.fail("material decode loaded the Shader tool registry"),
    )
    monkeypatch.setattr(
        server, "_shader_organization_cbuffer_alias_identity", cbuffer_identity
    )

    def call(method, params, **unused):
        assert method == "get_constant_buffer_values"
        assert params == {"event_id": 7, "stage": "fragment"}
        return {
            "constantBuffers": [{
                "bind": 5,
                "space": 0,
                "floats": [70, 50, 0, 0],
            }]
        }

    monkeypatch.setattr(server, "ipc", SimpleNamespace(call=call))
    result = run(
        path,
        sha,
        event_id=7,
        bind=5,
        workspace_path=str(workspace),
    )
    assert "error" not in result, result
    assert status_calls == [str(workspace)]
    assert len(identity_calls) == 1
    assert result["cbufferAliasRows"] == [{
        "bind": 5,
        "space": 0,
        "byteOffset": 0,
        "alias": "Dimensions.xy | Seed | Threshold",
        "decodedRanges": [
            {"byteOffset": 0, "byteSize": 8, "outputType": "Float2"},
            {"byteOffset": 8, "byteSize": 4, "outputType": "Float1"},
            {"byteOffset": 12, "byteSize": 4, "outputType": "Float1"},
        ],
    }]
    receipt = result["compiledLayoutReceipt"]
    assert receipt["authority"] == "evidence-only"
    assert receipt["shaderRawSha256"] == raw_sha
    assert receipt["layoutSha256"] == aliases.layout_sha256(
        block["block"], block["members"]
    )
    assert receipt["capturePlatformCompatible"] is True
    assert receipt["suggestedRows"][0]["alias"] == (
        "Dimensions.xy | Seed | Threshold"
    )
    assert result["readyForAliasPublication"] is True


def test_api_acp_share_registered_readonly_tool_and_effect_manifest():
    from replaylab_agent.policy import AGENT_LOCAL_TOOLS, READ_ONLY_TOOLS, tool_effects, validate_registered_tool_effect_manifest
    registered = frozenset(kwargs.get("name") or function.__name__
                           for function, unused, kwargs in server.mcp._registrations)
    assert {
        "list_asset_tool_operations",
        "run_asset_tool_operation",
        "uex_material_shader_maps",
        "decode_material_shader_outputs",
    } <= registered
    assert "decode_material_shader_outputs" in registered & READ_ONLY_TOOLS
    assert not tool_effects("decode_material_shader_outputs")
    assert validate_registered_tool_effect_manifest(registered) == registered - AGENT_LOCAL_TOOLS


def test_renderdoc_graphics_api_enum_matches_same_platform_shader_map():
    assert server._capture_platform_matches_material_platform(
        "GraphicsAPI.D3D12", "SP_PCD3D_SM6"
    ) is True


def test_renderdoc_graphics_api_enum_rejects_different_platform_shader_map():
    assert server._capture_platform_matches_material_platform(
        "GraphicsAPI.Vulkan", "SP_PCD3D_SM6"
    ) is False
