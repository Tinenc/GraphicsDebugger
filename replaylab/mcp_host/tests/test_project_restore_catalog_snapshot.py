"""Reopening an unchanged Shader must not confuse catalog metadata with code."""
import copy
import json

import pytest

from src import project_restore
from tests import test_project_restore as fixtures


@pytest.fixture
def target(tmp_path):
    root = tmp_path / "shaders"
    workspace = root / "current"
    workspace.mkdir(parents=True)
    identity = dict(resourceId=90, rawSha256="a" * 64, stage="fragment", entryPoint="main",
                    api="d3d11", encoding="dxbc")
    status = dict(schema="renderdoc-ultra-shader-workspace-status/v1", workspacePath=str(workspace),
                  workspaceId="b" * 32, capture=dict(captureId="c" * 64, catalogSha256="d" * 64,
                                                    api="D3D11"),
                  shader=dict(resourceId=90, rawSha256=identity["rawSha256"]),
                  baseline=dict(rawSha256=identity["rawSha256"], kind="DXBC", abiSha256="f" * 64),
                  stage="fragment", entryPoint="main", source=dict(present=True,
                      lane="executable-editable", format="hlsl", currentSha256="e" * 64))
    active = {"paths": {"shaderWorkspaces": str(root)}}
    return status, active, workspace, identity


@pytest.mark.parametrize("api,kind,encoding", [
    ("D3D11", "DXBC", "dxbc"), ("D3D12", "DXIL", "dxil"),
    ("OpenGL", "GLSL", "glsl"), ("Vulkan", "SPIRV", "spirv")])
def test_catalog_revision_is_not_original_shader_identity(target, api, kind, encoding):
    status, active, workspace, identity = target
    status["capture"]["api"], status["baseline"]["kind"] = api, kind
    identity.update(api=project_restore._asset_api(api), encoding=encoding)
    for fresh_catalog in (None, "0" * 64, status["capture"]["catalogSha256"]):
        assert project_restore._validate_reuse_workspace_status(
            status, active, workspace, identity, status["capture"]["captureId"], fresh_catalog) is status
    assert status["capture"]["catalogSha256"] == "d" * 64


@pytest.mark.parametrize("field", ["capture", "resource", "shader-raw", "baseline-raw", "stage", "entry", "api", "encoding"])
def test_catalog_change_never_relaxes_actual_shader_identity(target, field):
    status, active, workspace, identity = target
    status = copy.deepcopy(status)
    changes = {
        "capture": lambda: status["capture"].update(captureId="0" * 64),
        "resource": lambda: status["shader"].update(resourceId=91),
        "shader-raw": lambda: status["shader"].update(rawSha256="0" * 64),
        "baseline-raw": lambda: status["baseline"].update(rawSha256="0" * 64),
        "stage": lambda: status.update(stage="vertex"),
        "entry": lambda: status.update(entryPoint="other"),
        "api": lambda: status["capture"].update(api="Vulkan"),
        "encoding": lambda: status["baseline"].update(kind="SPIRV"),
    }
    changes[field]()
    with pytest.raises(RuntimeError, match="physical identity"):
        project_restore._validate_reuse_workspace_status(status, active, workspace, identity, "c" * 64, "0" * 64)


def test_existing_workspace_compiles_and_applies_after_bounded_catalog_refresh():
    case = fixtures.ProjectRestoreTests(methodName="runTest")
    case.setUp()
    try:
        def configure(host, replay):
            original = host.get_active_shader_workspace_path
            def refreshed(*args, **kwargs):
                located = json.loads(original(*args, **kwargs))
                located["catalogSha256"] = "0" * 64
                return json.dumps(located)
            host.get_active_shader_workspace_path = refreshed
        result, host, unused_replay, unused_progress = case._run(
            index_present=False, auto_prepare=True, precreate_target_workspace=True,
            target_source_modified=False, host_mutator=configure)
        assert result["completed"], result
        assert result["shaders"]["applied"] == 1
        assert host.compile_calls == host.apply_calls == 1
    finally:
        case.tearDown()
