"""Restore admission distinguishes source preparation from captured program identity."""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import project_restore


PLATFORMS = [
    ("d3d11", "GraphicsAPI.D3D11", "DXBC", "hlsl"),
    ("d3d12", "GraphicsAPI.D3D12", "DXIL", "hlsl"),
    ("d3d12", "GraphicsAPI.D3D12", "DXBC", "hlsl"),
    ("vulkan", "GraphicsAPI.Vulkan", "SPIRV", "glsl"),
    ("opengl", "GraphicsAPI.OpenGL", "GLSL", "glsl"),
    ("opengl", "GraphicsAPI.OpenGL", "OPENGL-SPIRV", "glsl"),
]


@pytest.fixture(params=PLATFORMS, ids=[item[0] + "-" + item[2] for item in PLATFORMS])
def target(tmp_path, request):
    api, capture_api, encoding, language = request.param
    root = tmp_path / "project" / "workspaces" / "shaders"
    workspace = root / "current-capture"
    workspace.mkdir(parents=True)
    capture_path = tmp_path / "current.rdc"
    capture_path.write_bytes(b"current capture")
    source_bytes = b"// exact saved project source\n"
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    identity = {
        "resourceId": 72473, "rawSha256": "a" * 64, "api": api,
        "encoding": encoding.lower(), "stage": "fragment", "entryPoint": "main",
        "anchorEventId": 53,
    }
    status = {
        "schema": "renderdoc-ultra-shader-workspace-status/v1",
        "workspacePath": str(workspace), "workspaceId": "b" * 32,
        "capture": {"captureId": "c" * 64, "catalogSha256": "d" * 64,
                    "capturePath": str(capture_path), "api": capture_api},
        "shader": {key: identity[key] for key in ("resourceId", "rawSha256")},
        "baseline": {"rawSha256": identity["rawSha256"], "kind": encoding},
        "stage": "fragment", "entryPoint": "main", "revision": 0,
        "latestCompiled": None, "targetProfile": {"apiEnvironment": api},
        "source": {"present": False, "lane": "binary-only", "format": "", "currentSha256": None},
    }
    return SimpleNamespace(
        root=root, workspace=workspace, capture_path=capture_path, identity=identity,
        status=status, language=language, source_bytes=source_bytes, source_sha=source_sha,
        active={"projectRoot": str(tmp_path / "project"),
                "paths": {"shaderWorkspaces": str(root)}})


def validate(target, status=None):
    return project_restore._validate_reuse_workspace_status(
        target.status if status is None else status, target.active, target.workspace,
        target.identity, "c" * 64, "d" * 64)


def editable(target):
    return {"present": True, "lane": "executable-editable", "format": target.language,
            "currentSha256": target.source_sha, "modifiedSinceDecompile": False}


def test_binary_only_target_reports_missing_source_not_wrong_original(target):
    with pytest.raises(RuntimeError, match="no executable editable source") as failure:
        validate(target)
    assert "source.present" in str(failure.value)
    assert "physical identity" not in str(failure.value)


@pytest.mark.parametrize("field,value", [
    ("capture.api", "GraphicsAPI.Unknown"),
    ("capture.captureId", "e" * 64),
    ("shader.resourceId", 208035),
    ("shader.rawSha256", "e" * 64),
    ("baseline.rawSha256", "e" * 64),
    ("baseline.kind", "OTHER"),
    ("stage", "vertex"),
    ("entryPoint", "other"),
])
def test_source_recovery_cannot_hide_a_changed_target(target, field, value):
    status = copy.deepcopy(target.status)
    parts = field.split(".")
    container = status if len(parts) == 1 else status[parts[0]]
    container[parts[-1]] = value
    with pytest.raises(RuntimeError, match="physical identity") as failure:
        validate(target, status)
    assert field in str(failure.value)


class RestoreHost:
    def __init__(self, target):
        self.target = target
        self.status = target.status
        self.prepare_calls = 0
        self.lookups = []
        self.session_id = "f" * 32
        self.other_status = {}

    def _shader_workspace_service(self):
        return SimpleNamespace(status=self.read_status)

    def read_status(self, workspace):
        if Path(workspace) == self.target.workspace:
            return copy.deepcopy(self.status)
        return copy.deepcopy(self.other_status[str(Path(workspace))])

    def get_shader_workspace_status(self, workspace):
        return json.dumps({"schema": "renderdoc-ultra-shader-workspace-mcp/v1",
                           "operation": "status", "result": self.read_status(workspace)})

    def get_active_shader_workspace_path(self, event_id, stage, **expected):
        self.lookups.append(expected)
        identity = self.target.identity
        assert expected == {
            "expected_resource_id": identity["resourceId"],
            "expected_entry_point": identity["entryPoint"],
            "expected_raw_sha256": identity["rawSha256"],
            "expected_capture_session_id": self.session_id,
        }
        return json.dumps({
            "schema": "replaylab-active-shader-workspace-path/v1", "schemaVersion": 1,
            "captureId": "c" * 64, "captureSessionId": self.session_id,
            "catalogSha256": "d" * 64, "workspacePath": str(self.target.workspace),
            "eventId": event_id, "stage": stage, "entryPoint": identity["entryPoint"],
            "shaderResourceId": identity["resourceId"], "rawSha256": identity["rawSha256"],
            "applied": False,
        })

    def prepare_shader_workspace(self, event_id, stage, workspace, **expected):
        self.prepare_calls += 1
        identity = self.target.identity
        assert expected == {
            "expected_resource_id": identity["resourceId"],
            "expected_entry_point": identity["entryPoint"],
            "expected_raw_sha256": identity["rawSha256"],
            "expected_capture_id": "c" * 64,
            "expected_capture_session_id": self.session_id,
            "admit_existing": True,
        }
        assert event_id == identity["anchorEventId"]
        assert stage == identity["stage"]
        self.status["source"] = editable(self.target)
        source = Path(workspace) / "source" / ("shader." + self.target.language)
        source.parent.mkdir()
        source.write_bytes(self.target.source_bytes)
        return json.dumps({
            "schema": "renderdoc-ultra-shader-workspace-mcp/v1", "operation": "prepare",
            "result": {"workspacePath": workspace,
                "created": {"admittedExisting": True, "sourcePreserved": True},
                "binding": {"workspacePath": workspace, "captureId": "c" * 64,
                    "captureSessionId": self.session_id, "eventId": event_id, "stage": stage,
                    "entryPoint": identity["entryPoint"], "originalResourceId": identity["resourceId"],
                    "originalRawSha256": identity["rawSha256"]}}})


def prepare(target, host, **kwargs):
    return project_restore._prepare_reuse_workspace(
        host, target.active, target.identity, host.session_id, "d" * 64,
        expected_anchor_event_id=target.identity["anchorEventId"],
        expected_source_sha256=target.source_sha, **kwargs)


def test_baseline_only_existing_target_is_prepared_then_checked(target):
    host = RestoreHost(target)
    workspace, status, capture_id, catalog = prepare(target, host)
    assert workspace == target.workspace
    assert status["source"] == editable(target)
    assert capture_id == "c" * 64 and catalog == "d" * 64
    assert host.prepare_calls == 1


def test_cross_capture_original_source_owner_is_not_treated_as_current_resource(target):
    host = RestoreHost(target)
    historical = target.root / "historical-capture"
    historical.mkdir()
    source = historical / ("shader." + target.language)
    source.write_bytes(b"// unsaved historical edit\n")
    before = source.read_bytes()
    old = copy.deepcopy(target.status)
    old.update(workspacePath=str(historical), workspaceId="1" * 32)
    old["capture"].update(capturePath=str(target.capture_path.with_name("older.rdc")),
                          captureId="2" * 64, catalogSha256="3" * 64)
    old["shader"]["resourceId"] = 208035
    old["source"] = {**editable(target), "currentSha256": hashlib.sha256(before).hexdigest(),
                     "modifiedSinceDecompile": True}
    host.other_status[str(historical)] = old
    knowledge = {"registeredWorkspace": {"path": str(historical), "workspaceId": "1" * 32},
                 "sourceSha256": target.source_sha, "targetProfile": old["targetProfile"]}
    workspace, status, _, _ = prepare(target, host, inventory_mode=True,
                                    registered_knowledge=knowledge, capture_path=target.capture_path)
    assert workspace == target.workspace
    assert status["shader"]["resourceId"] == 72473
    assert host.prepare_calls == 1
    assert source.read_bytes() == before
    assert host.other_status[str(historical)] == old


def test_unpublished_current_source_is_preserved_before_prepare(target):
    host = RestoreHost(target)
    host.status["source"] = {**editable(target), "currentSha256": "e" * 64,
                             "modifiedSinceDecompile": True}
    with pytest.raises(project_restore.RestoreLocalEdit):
        prepare(target, host)
    assert host.prepare_calls == 0


def test_catalog_snapshot_hash_is_not_the_original_program_identity(target):
    status = copy.deepcopy(target.status)
    status["source"] = editable(target)
    status["capture"]["catalogSha256"] = "e" * 64
    assert validate(target, status) == status
