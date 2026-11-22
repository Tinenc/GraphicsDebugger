"""Debug-only container changes may reuse source, never old replay authority."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from src.projects import ReplayProjectError
from src.shader_workspace import ShaderKey, ShaderWorkspaceService
from src.shader_workspace.catalog import load_selected_shader
from src.shader_workspace.toolchains import route_record, toolchain_document
from src.shader_workspace.tools import ToolRegistry
from tests import test_projects as project_fixtures
from tests import test_shader_workspace as workspace_fixtures
from tests.test_shader_workspace_mcp import load_server_module


def _debug_container(code=b"same captured program", debug=b"origin debug"):
    # Keep real DXBC chunk offsets/lengths; only the excluded debug chunk differs.
    return project_fixtures._dxbc(code, debug).replace(b"RDEF", b"SDBG", 1)


@pytest.fixture
def source_case():
    project = project_fixtures.ReplayProjectTests()
    tooling = workspace_fixtures.ShaderWorkspaceTests()
    project.setUp()
    tooling.setUp()
    try:
        registry = ToolRegistry(tooling.config)
        routes = [route_record(registry, route_id="source-" + operation, tool_id="good",
            operation=operation, input_format=source, output_format=target,
            stages=["fragment"], fidelity=fidelity, priority=1,
            implementation="deterministic source import fixture",
            source_url="https://example.invalid/source-import",
            caveats=["Fixture source is deterministic; this is not a GPU equivalence test."])
            for operation, source, target, fidelity in (
                ("decompile", "dxbc", "hlsl", "reconstructed-source"),
                ("compile", "hlsl", "dxbc", "native-compile"))]
        route_path = tooling.root / "source-routes.json"
        route_path.write_text(json.dumps(toolchain_document(tooling.config, routes, [])), encoding="utf8")
        workspaces = ShaderWorkspaceService(tooling.config, route_path)

        def create(label, *, code=b"same captured program", debug=b"target debug", mutate=None):
            catalog, shader, _ = project._catalog(label, _debug_container(code, debug), resource_id=901)
            if mutate is not None:
                document = json.loads(catalog.read_text(encoding="utf8"))
                mutate(document)
                catalog.write_text(json.dumps(document), encoding="utf8")
                shader = document["shaders"][0]
            imported = project.service.import_capture(catalog)
            capture_record = next(item for item in project.service._load()["captures"]
                                  if item["captureId"] == imported["captureId"])
            catalog = project.project / capture_record["catalogPath"]
            root = project.root / (label + "-workspace")
            key = ShaderKey(resource_id=901, stage="fragment", entry_point="main",
                            raw_sha256=shader["rawSha256"])
            workspaces.create(catalog, key, root, editable_format="hlsl")
            return root, workspaces.status(root)

        origin, _ = create("origin", debug=b"origin debug")
        workspaces.decompile(origin)
        knowledge = project.service.register_workspace(
            workspaces.status(origin), include_compiled_candidate=False)["knowledge"]
        yield project, workspaces, knowledge, create
    finally:
        tooling.tearDown()
        project.tearDown()


def test_debug_only_difference_imports_current_source_into_fresh_physical_workspace(source_case):
    project, workspaces, knowledge, create = source_case
    target, status = create("target")
    assert status["baseline"]["rawSha256"] != knowledge["rawSha256"]
    assert status["baseline"]["codeSha256"] == knowledge["codeSha256"]
    plan = project.service.reuse_plan(status["capture"]["captureId"])
    candidate, = plan["matches"][0]["workspaceCandidates"]
    assert candidate["matchMode"] == "code-interface-layout-exact"
    assert candidate["sourceSeedEligible"] is True
    assert candidate["activeCandidate"] is True
    bundle = project.service.prepare_workspace_source(knowledge["knowledgeId"], status)
    assert bundle["originalRawSha256"] == status["baseline"]["rawSha256"]
    assert bundle["baselineAbiSha256"] == status["baseline"]["abiSha256"]
    assert bundle["requiresCompile"] is True and bundle["requiresLiveAbiRevalidation"] is True
    assert bundle["applied"] is False
    imported = workspaces.import_project_source(target, bundle)
    assert imported["decompileInvoked"] is False and imported["compiled"] is False
    after = workspaces.status(target)
    assert after["latestCompiled"] is None and after["revision"] == 0
    assert Path(after["source"]["path"]).read_bytes().decode("utf8") == bundle["sourceText"]
    compiled = workspaces.compile(target)
    assert compiled["requiresReplayAbiValidation"] is True
    assert compiled["replayAbiValidated"] is False


def test_server_prepare_imports_verified_code_match_before_reconstruction(source_case):
    project, workspaces, knowledge, create = source_case
    target, status = create("server-target")
    catalog = Path(status["capture"]["catalogPath"])
    key = ShaderKey(901, "fragment", "main", status["baseline"]["rawSha256"])
    selected = load_selected_shader(catalog, key)
    active = SimpleNamespace(shader_workspace_path=lambda **kwargs: target,
                             replay_service=lambda: project.service)
    binding = ({}, {"scopeConfigured": True, "paths": {"shaderWorkspaces": str(project.root)}},
               {"catalogSha256": status["capture"]["catalogSha256"]}, catalog)
    server = load_server_module()
    with (mock.patch.object(server, "_shader_workspace_service", return_value=workspaces),
          mock.patch.object(server, "_active_project_service", return_value=active),
          mock.patch.object(server, "_active_capture_catalog_binding", return_value=binding),
          mock.patch.object(server, "_active_capture_catalog_selection",
                            return_value=({"captureSessionId": "a" * 32}, catalog, selected, "b" * 64)),
          mock.patch.object(server, "_active_shader_route_binding_sha256", return_value="c" * 64),
          mock.patch.object(server, "_active_applied_shader_workspace", return_value=None),
          mock.patch.object(server, "_ensure_workspace_capture_catalog_identity"),
          mock.patch.object(server, "emit_console_event"),
          mock.patch.object(workspaces, "decompile", side_effect=AssertionError("must use the saved source"))):
        result = json.loads(server.prepare_shader_workspace(
            202, "fragment", str(target), admit_existing=True,
            expected_raw_sha256=key.raw_sha256))
    assert "error" not in result, result
    prepared = result["result"]
    assert prepared["projectSourceReuse"]["knowledgeId"] == knowledge["knowledgeId"]
    assert prepared["projectSourceReuse"]["requiresCompile"] is True
    assert prepared["workspaceStatus"]["latestCompiled"] is None


@pytest.mark.parametrize("difference", ["code", "interface", "layout", "incomplete"])
def test_code_identity_alone_never_authorizes_source_import(source_case, difference):
    project, _workspaces, knowledge, create = source_case
    def mutate(document):
        shader = document["shaders"][0]
        if difference == "interface":
            shader["outputTopology"] = "LineList"
        elif difference == "layout":
            shader["constantBuffers"][0]["fields"][0]["baseType"] = "UInt"
        elif difference == "incomplete":
            shader["interfaceComplete"] = False
    _, status = create("different", code=b"other program" if difference == "code" else b"same captured program",
                       mutate=mutate)
    with pytest.raises(ReplayProjectError, match="code/interface/layout|abiSha256"):
        project.service.prepare_workspace_source(knowledge["knowledgeId"], status)


@pytest.mark.parametrize("area,field,value,reason", [
    ("baseline", "codeSha256", "f" * 64, "code/interface/layout"),
    ("baseline", "abiSha256", "f" * 64, "physical ABI"),
    ("capture", "api", "D3D12", "API/encoding"),
    ("baseline", "kind", "DXIL", "API/encoding"),
    ("capture", "captureId", "f" * 64, "different capture"),
    ("shader", "resourceId", 902, "imported capture shader"),
    ("targetProfile", "profile", "ps_5_1", "original Shader/profile"),
])
def test_import_requires_destination_catalog_and_current_physical_identity(source_case, area, field, value, reason):
    project, _workspaces, knowledge, create = source_case
    _, status = create("target")
    changed = copy.deepcopy(status)
    changed[area][field] = value
    with pytest.raises(ReplayProjectError, match=reason):
        project.service.prepare_workspace_source(knowledge["knowledgeId"], changed)
