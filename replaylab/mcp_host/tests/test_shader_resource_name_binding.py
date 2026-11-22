"""Texture display-name publication trusts AI semantics, not Host workflow tokens."""

import hashlib
import inspect
import json
from types import SimpleNamespace
from unittest import mock

import pytest

from src import server


CAPTURE_ID = "a" * 64
CAPTURE_SESSION_ID = "b" * 32
RAW_SHA = "c" * 64


def _write_alias_workspace(tmp_path):
    shader_root = tmp_path / "workspaces" / "shaders" / "v2"
    workspace = shader_root / ("f" * 32)
    baseline = workspace / "baseline" / "shader.bin"
    catalog = workspace / "capture" / "shader_catalog.v1.json"
    baseline.parent.mkdir(parents=True)
    catalog.parent.mkdir(parents=True)
    baseline_bytes = b"captured shader bytes"
    baseline.write_bytes(baseline_bytes)
    catalog.write_text("{}", encoding="utf-8")
    raw_sha256 = hashlib.sha256(baseline_bytes).hexdigest()
    manifest = {
        "schema": "renderdoc-ultra-shader-workspace/v1",
        "schemaVersion": 1,
        "workspaceId": "f" * 32,
        "stage": "fragment",
        "entryPoint": "MainPS",
        "capture": {
            "captureId": "d" * 64,
            "capturePath": "D:/captures/frame.rdc",
            "catalogPath": str(catalog),
            "catalogSha256": hashlib.sha256(catalog.read_bytes()).hexdigest(),
            "api": "D3D11",
        },
        "shader": {
            "resourceId": 11,
            "stage": "fragment",
            "entryPoint": "MainPS",
            "rawSha256": raw_sha256,
            "shaderKey": "11:fragment:MainPS:%s" % raw_sha256,
        },
        "baseline": {
            "path": "baseline/shader.bin",
            "byteSize": len(baseline_bytes),
            "rawSha256": raw_sha256,
        },
    }
    (workspace / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return shader_root, workspace, baseline, manifest


def _row(resource_id: int = 77) -> dict:
    return {
        "selector": "resource:_32",
        "resourceId": resource_id,
        "bindNumber": 4,
        "registerSpace": 1,
        "alias": "T_Body_BaseMap",
        "assetPath": "/Game/T_Body_Base",
        "hostEvidenceKind": "pixel-same-image",
    }


def _install_current_target(monkeypatch, tmp_path, *, bound_resource_id: int = 77):
    calls = []
    status = {
        "workspaceId": "f" * 32,
        "workspacePath": str(tmp_path),
        "stage": "fragment",
        "entryPoint": "MainPS",
        "capture": {
            "captureId": "d" * 64,
            "capturePath": "D:/captures/frame.rdc",
            "catalogPath": str(tmp_path / "shader_catalog.v1.json"),
            "catalogSha256": "e" * 64,
            "api": "D3D11",
        },
        "shader": {
            "resourceId": 11,
            "shaderKey": "11:fragment:MainPS:%s" % RAW_SHA,
            "rawSha256": RAW_SHA,
        },
    }
    context = {
        "capture": {
            "captureId": CAPTURE_ID,
            "captureSessionId": CAPTURE_SESSION_ID,
        }
    }
    current_shader = {
        "resourceId": 11,
        "rawSha256": RAW_SHA,
        "stage": "fragment",
        "entryPoint": "MainPS",
    }
    service = mock.Mock(
        side_effect=AssertionError("alias binding loaded ShaderWorkspaceService")
    )
    toolchain = mock.Mock(
        side_effect=AssertionError("alias binding loaded the Shader tool registry")
    )
    monkeypatch.setattr(server, "_shader_workspace_service", service)
    monkeypatch.setattr(server, "_shader_toolchain_catalog", toolchain)
    monkeypatch.setattr(
        server, "_alias_publication_workspace_status", lambda unused: status
    )
    monkeypatch.setattr(
        server,
        "_ensure_workspace_capture_catalog_identity",
        lambda workspace, **kwargs: calls.append(
            ("rebind", str(workspace), kwargs.get("require_active_catalog"))
        ) or {"captureSessionId": CAPTURE_SESSION_ID},
    )
    monkeypatch.setattr(
        server,
        "_active_live_context",
        lambda **unused: calls.append(("context", "")) or context,
    )
    monkeypatch.setattr(
        server, "_active_bound_shader", lambda unused, event_id, stage: current_shader
    )
    monkeypatch.setattr(
        server,
        "_require_same_active_capture",
        lambda expected: expected,
    )
    monkeypatch.setattr(
        server,
        "ipc",
        SimpleNamespace(
            call=lambda method, params, **unused: [
                {
                    "resourceId": bound_resource_id,
                    "bind": 4,
                    "space": 1,
                }
            ]
        ),
    )
    return calls, service, toolchain


def test_current_binding_needs_no_asset_authorization_or_task_receipt(
    tmp_path, monkeypatch
):
    calls, service, toolchain = _install_current_target(monkeypatch, tmp_path)
    rows, binding = server._current_resource_alias_publication_binding(
        tmp_path, [_row()], event_id=11384, stage="fragment"
    )

    assert rows == [_row()]
    assert binding["captureId"] == CAPTURE_ID
    assert binding["captureSessionId"] == CAPTURE_SESSION_ID
    assert binding["eventId"] == 11384
    assert binding["shaderRawSha256"] == RAW_SHA
    assert binding["shaderResourceId"] == 11
    assert len(binding["bindingId"]) == 32
    assert "authorizationId" not in binding
    assert "authorizationHmacSha256" not in binding
    assert "assetResolutionSha256" not in binding
    assert calls[:2] == [
        ("rebind", str(tmp_path), True),
        ("context", ""),
    ]
    service.assert_not_called()
    toolchain.assert_not_called()


def test_current_binding_accepts_agent_name_without_semantic_permission_fields(
    tmp_path, monkeypatch
):
    _install_current_target(monkeypatch, tmp_path)
    row = _row()
    row.pop("assetPath")
    row.pop("hostEvidenceKind")

    rows, binding = server._current_resource_alias_publication_binding(
        tmp_path, [row], event_id=11384, stage="fragment"
    )

    assert rows == [{
        **row,
        "assetPath": "",
        "hostEvidenceKind": "",
    }]
    manifest_row = server._resource_alias_manifest_rows(rows, binding)[0]
    assert manifest_row["metadata"]["assetPath"] is None
    assert manifest_row["metadata"]["semanticEvidenceKind"] is None


def test_resource_alias_optional_provenance_is_not_a_semantic_allowlist():
    row = _row()
    row["hostEvidenceKind"] = "agent-verified-decoded-pixels"

    assert server.ShaderOrganizationManager._canonical_resource_alias_rows([row]) == [row]


def test_current_binding_rejects_a_resource_not_bound_at_the_requested_slot(
    tmp_path, monkeypatch
):
    _install_current_target(monkeypatch, tmp_path, bound_resource_id=78)

    with pytest.raises(RuntimeError, match="does not identify one current bound texture"):
        server._current_resource_alias_publication_binding(
            tmp_path, [_row()], event_id=11384, stage="fragment"
        )


def test_alias_workspace_status_is_physical_and_does_not_load_tool_registry(
    tmp_path, monkeypatch
):
    shader_root, workspace, baseline, manifest = _write_alias_workspace(tmp_path)
    monkeypatch.setattr(
        server,
        "_active_project_service",
        lambda: SimpleNamespace(
            status=lambda: {"paths": {"shaderWorkspaces": str(shader_root)}}
        ),
    )
    service = mock.Mock(
        side_effect=AssertionError("lightweight alias status loaded tool service")
    )
    toolchain = mock.Mock(
        side_effect=AssertionError("lightweight alias status loaded tool registry")
    )
    monkeypatch.setattr(server, "_shader_workspace_service", service)
    monkeypatch.setattr(server, "_shader_toolchain_catalog", toolchain)

    status = server._alias_publication_workspace_status(str(workspace))

    assert status["workspacePath"] == str(workspace.resolve())
    assert status["workspaceId"] == manifest["workspaceId"]
    assert status["shader"]["rawSha256"] == manifest["shader"]["rawSha256"]
    service.assert_not_called()
    toolchain.assert_not_called()

    baseline.write_bytes(b"changed shader bytes")
    with pytest.raises(RuntimeError, match="baseline bytes changed"):
        server._alias_publication_workspace_status(str(workspace))


def test_cbuffer_identity_uses_supplied_physical_status_loader(
    tmp_path, monkeypatch
):
    status = {
        "workspaceId": "f" * 32,
        "capture": {"captureId": "d" * 64},
        "stage": "fragment",
        "entryPoint": "MainPS",
        "shader": {"resourceId": 11},
        "baseline": {"rawSha256": RAW_SHA},
    }
    loader = mock.Mock(return_value=status)
    ensure = mock.Mock(return_value={
        "captureSessionId": CAPTURE_SESSION_ID,
        "activeCatalogPublished": True,
        "activeCaptureId": CAPTURE_ID,
    })
    monkeypatch.setattr(server, "_ensure_workspace_capture_catalog_identity", ensure)
    monkeypatch.setattr(server, "_shader_organization_anchor_event", lambda unused: 11384)
    monkeypatch.setattr(
        server,
        "_active_live_context",
        lambda **unused: {
            "schema": "renderdoc-ultra-current-context/v1",
            "schemaVersion": 1,
            "capture": {
                "loaded": True,
                "captureId": CAPTURE_ID,
                "captureSessionId": CAPTURE_SESSION_ID,
            },
        },
    )
    monkeypatch.setattr(
        server,
        "_active_bound_shader",
        lambda unused_context, unused_event, unused_stage: {
            "resourceId": 22,
            "stage": "fragment",
            "entryPoint": "MainPS",
            "rawSha256": RAW_SHA,
        },
    )
    same_capture = mock.Mock()
    monkeypatch.setattr(server, "_require_same_active_capture", same_capture)
    service = mock.Mock(
        side_effect=AssertionError("CBuffer identity loaded ShaderWorkspaceService")
    )
    toolchain = mock.Mock(
        side_effect=AssertionError("CBuffer identity loaded tool registry")
    )
    monkeypatch.setattr(server, "_shader_workspace_service", service)
    monkeypatch.setattr(server, "_shader_toolchain_catalog", toolchain)

    def ipc_call(method, params, **unused):
        if method == "get_shader_cbuffer_alias_identity":
            assert params["expected_capture_id"] == CAPTURE_ID
            assert params["expected_original_resource_id"] == 22
            return {
                "captureId": CAPTURE_ID,
                "captureSessionId": CAPTURE_SESSION_ID,
                "eventId": 11384,
                "stage": "fragment",
                "entryPoint": "MainPS",
                "shaderResourceId": 22,
                "rawSha256": RAW_SHA,
                "blocks": [],
            }
        raise AssertionError(method)

    monkeypatch.setattr(server, "ipc", SimpleNamespace(call=ipc_call))
    result = server._shader_organization_cbuffer_alias_identity(
        str(tmp_path), status=status, status_loader=loader
    )

    assert result["captureSessionId"] == CAPTURE_SESSION_ID
    assert result["captureId"] == CAPTURE_ID
    assert result["shaderResourceId"] == 22
    ensure.assert_called_once_with(
        str(tmp_path), status=status, status_loader=loader,
        require_active_catalog=True,
    )
    loader.assert_not_called()
    same_capture.assert_called_once()
    service.assert_not_called()
    toolchain.assert_not_called()


def test_public_tool_schema_has_no_resource_authorization_parameter():
    parameters = inspect.signature(server.publish_shader_resource_names).parameters
    assert "resource_alias_authorization" not in parameters
    assert set(("workspace_path", "rows_json", "operation_id", "event_id", "stage")) <= set(
        parameters
    )
