"""Saved source must restore before reconstruction on every source route."""
import copy
import hashlib
import json
import shutil
from types import SimpleNamespace
from unittest import mock

import pytest

from src.projects import ReplayProjectService
from src.shader_workspace.catalog import ShaderKey, capture_identity, load_selected_shader
from tests.test_spirv_workspace import spirv_workspace_fixture
from tests.test_native_glsl_workspace import glsl_fixture
from tests.test_shader_workspace_mcp import load_server_module


@pytest.mark.parametrize("fixture_name", ["spirv_workspace_fixture", "glsl_fixture"])
@pytest.mark.parametrize("existing_target", [False, True])
def test_prepare_restores_glsl_across_capture_and_resource_ids(request, fixture_name, existing_target):
    f = request.getfixturevalue(fixture_name)
    root, service = f["root"], f["service"]
    catalog = json.loads(f["catalogPath"].read_text())
    project = root / "project"
    ReplayProjectService.create(project, "Shared Shader", game_id="game", build_id="build",
                                platform="windows", api_family=catalog["api"].lower())
    replay = ReplayProjectService(project)
    replay.import_capture(f["catalogPath"])
    workspaces = root / "workspaces"
    origin, target = workspaces / "original", workspaces / "target"
    service.create(f["catalogPath"], f["key"], origin)
    if catalog["api"] == "Vulkan":
        service.decompile(origin)
    source = origin / "source/shader.glsl"
    source.write_bytes(source.read_bytes() + b"// organized project source\n")
    registered = replay.register_workspace(service.status(origin), include_compiled_candidate=False)
    source_bytes = source.read_bytes()

    # Different RDC, ResourceId and EID, identical original program.
    target_dir = root / "target-catalog"
    shutil.copytree(f["catalogPath"].parent, target_dir)
    target_catalog = target_dir / f["catalogPath"].name
    target_key = ShaderKey(9001, f["key"].stage, f["key"].entry_point, f["key"].raw_sha256)
    catalog["capturePath"] = "D:/captures/another-frame.rdc"
    catalog["shaders"][0].update(shaderKey=target_key.catalog_key, resourceId=9001, shaderResourceId=9001)
    catalog["uses"][0].update(shaderKey=target_key.catalog_key, resourceId=9001, eventId=202)
    catalog["captureId"] = capture_identity(catalog, [target_key])
    target_catalog.write_text(json.dumps(catalog), encoding="utf8")
    replay.import_capture(target_catalog)
    capture_record = next(row for row in replay._load()["captures"] if row["captureId"] == catalog["captureId"])
    target_catalog = project / capture_record["catalogPath"]
    if existing_target:
        service.create(target_catalog, target_key, target)
    selected = load_selected_shader(target_catalog, target_key)
    catalog_hash = hashlib.sha256(target_catalog.read_bytes()).hexdigest()
    active = SimpleNamespace(shader_workspace_path=lambda **kw: target, replay_service=lambda: replay,
        future_frame_plan=lambda capture, digest='':{'reusePlan':replay.reuse_plan(capture,digest)})
    binding = ({}, {"scopeConfigured": True, "paths": {"shaderWorkspaces": str(workspaces)}},
               {"catalogSha256": catalog_hash}, target_catalog)
    exported = {"captureSessionId": "a" * 32}
    server = load_server_module()
    with (mock.patch.object(server, "_shader_workspace_service", return_value=service),
          mock.patch.object(server, "_active_project_service", return_value=active),
          mock.patch.object(server, "_active_capture_catalog_binding", return_value=binding),
          mock.patch.object(server, "_active_capture_catalog_selection", return_value=(exported, target_catalog, selected, "b" * 64)),
          mock.patch.object(server, "_active_shader_route_binding_sha256", return_value="c" * 64),
          mock.patch.object(server, "_active_applied_shader_workspace", return_value=None),
          mock.patch.object(server, "_ensure_workspace_capture_catalog_identity"),
          mock.patch.object(server, "emit_console_event"),
          mock.patch.object(service, "decompile", side_effect=AssertionError("saved source needs no decompiler"))):
        result = json.loads(server.prepare_shader_workspace(202, "fragment", str(target),
                    admit_existing=existing_target, expected_raw_sha256=target_key.raw_sha256))
    assert "error" not in result, result
    prepared = result["result"]
    assert prepared["projectSourceReuse"]["knowledgeId"] == registered["knowledge"]["knowledgeId"]
    assert prepared["representation"]["lane"] == "executable-editable"
    assert (target / "source/shader.glsl").read_bytes() == source_bytes
    assert source.read_bytes() == source_bytes
    assert prepared["workspaceStatus"]["shader"]["resourceId"] == 9001
    assert prepared["workspaceStatus"]["latestCompiled"] is None


def test_native_source_import_eligibility_preserves_edits_and_compiled_revisions(glsl_fixture):
    from src.shader_workspace.project_source import can_import_project_source
    f = glsl_fixture
    target = f["root"] / "untouched"
    f["service"].create(f["catalogPath"], f["key"], target)
    status = f["service"].status(target)
    assert can_import_project_source(status)
    for path, value in [("hash", "e" * 64), ("origin", "verified-project-source"), ("revision", 1), ("compiled", {})]:
        changed = copy.deepcopy(status)
        if path == "hash": changed["source"]["currentSha256"] = value
        elif path == "origin": changed["source"]["origin"] = value
        elif path == "revision": changed["revision"] = value
        else: changed["latestCompiled"] = value
        assert not can_import_project_source(changed)
