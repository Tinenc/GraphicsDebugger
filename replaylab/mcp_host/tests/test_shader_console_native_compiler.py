"""Native GLSL compiler provenance survives the Host-to-Bridge project reader."""
import copy
from pathlib import Path

import pytest

from src.active_projects import ActiveReplayProjectService
from src.projects import _compiler_binding_identity
from src.shader_workspace.native_glsl import compiler_binding, plan, profile
from tests.test_native_glsl_workspace import glsl_fixture
from tests.test_spirv_workspace import spirv_workspace_fixture
import test_shader_console as fixtures

console = fixtures.console


def native_binding(container="glsl"):
    target = profile("fragment", "OpenGL")
    target["containerFormat"] = container
    return compiler_binding({"targetProfile": target, "routePlan": plan("fragment")})


@pytest.mark.parametrize("container", ["glsl", "opengl-spirv"])
def test_bridge_and_host_read_the_same_native_compiler_identity(container):
    value = native_binding(container)
    assert console._project_compiler_binding_identity(value) == _compiler_binding_identity(value)
    assert "hops" not in value and value["executionAuthority"] is False


@pytest.mark.parametrize("field,value", [
    ("backend", "external"), ("api", "vulkan"), ("sourceFormat", "hlsl"),
    ("outputFormat", "spirv"), ("compilerContract", "other"),
    ("executionAuthority", True), ("evidenceOnly", False),
    ("routePlanSha256", "bad"), ("bindingSha256", "0" * 64),
    ("extraField", True), ("targetProfile", {"apiEnvironment": "vulkan", "containerFormat": "glsl"}),
])
def test_native_binding_still_rejects_wrong_platform_policy_or_digest(field, value):
    changed = copy.deepcopy(native_binding())
    changed[field] = value
    with pytest.raises(console.ShaderConsoleError):
        console._project_compiler_binding_identity(changed)


def test_real_native_workspace_can_be_published_and_opened_from_project_library(glsl_fixture):
    f = glsl_fixture
    selection = f["root"] / "config" / "active-project.json"
    active = ActiveReplayProjectService(selection, f["root"] / "projects")
    active.ensure_pending("Native OpenGL library")
    active.configure_scope(game_id="game", build_id="build", api_family="opengl")
    staged = active.import_capture(f["catalogPath"])["stagedCatalog"]
    root = Path(active.status()["paths"]["shaderWorkspaces"]) / "native-glsl"
    f["service"].create(staged["catalogPath"], f["key"], root)
    status = f["service"].status(root)
    registered = active.replay_service().register_workspace(status, include_compiled_candidate=False)["knowledge"]
    assert registered["compilerBinding"]["backend"] == "renderdoc-native"
    snapshot = console.load_project_shader_library_snapshot(str(selection))
    assert snapshot["available"] and not snapshot["errors"], snapshot
    document, = snapshot["documents"]
    assert document["sourceFormat"] == "glsl"
    loaded = console.load_project_shader_source_document(str(selection),
        document["projectWorkspaceId"], document["projectId"], document["knowledgeId"], document["sourceSha256"])
    assert loaded["sourceFormat"] == "glsl"
    assert Path(loaded["sourcePath"]).read_bytes() == f["blob"]
