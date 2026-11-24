"""Include edits invalidate current candidates without corrupting frozen history."""
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest import mock

import pytest

from src.shader_workspace import workspace as workspace_module
from src.shader_workspace.compile_source_unit import validate_snapshot
from src.shader_workspace.errors import ManifestValidationError
from src.shader_workspace.organization import ShaderOrganizationManager
from tests import test_shader_workspace as fixture_module
from tests import test_shader_workspace_routes as route_fixture_module
from test_shader_host_ide_finish import work


@pytest.fixture
def workspace_case():
    fixture = fixture_module.ShaderWorkspaceTests()
    fixture.setUp()
    try:
        service, root, unused = fixture._create("include-workspace")
        source = Path(service.decompile(root)["sourcePath"])
        header = source.parent / "lighting.hlsli"
        nested = source.parent / "shared" / "math.hlsli"
        nested.parent.mkdir()
        nested.write_text("float Square(float x) { return x * x; }\n")
        header.write_text('#include "shared/math.hlsli"\nfloat Shade() { return Square(0.5); }\n')
        source.write_text('#include "lighting.hlsli"\nfloat4 main() : SV_Target { return Shade(); }\n')
        yield service, root, source, header, nested
    finally:
        fixture.tearDown()


def test_direct_compile_uses_frozen_nested_includes_and_header_only_edit_is_stale(workspace_case):
    service, root, source, header, nested = workspace_case
    compiled = service.compile(root)
    proof = compiled["sourceDependencies"]
    assert len(proof["documents"]) == 3
    assert service.status(root)["sourceDependenciesCurrent"] is True
    compiled_input = (root / proof["inputPath"]).read_text()
    assert str(header).replace("\\", "/") not in compiled_input
    assert "source-input/01.hlsli" in compiled_input
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    nested.write_text("float Square(float x) { return x; }\n")
    status = service.status(root)
    assert status["source"]["currentSha256"] == source_sha
    assert status["latestCompiled"]["sourceSha256"] == source_sha
    assert status["sourceDependenciesCurrent"] is False
    assert status["abiValidationStatus"] == "source-changed"
    assert validate_snapshot(root, source, source.read_bytes(), proof) == proof["inputSha256"]


def test_removed_current_header_leaves_old_compile_readable_and_recompile_fails_clearly(workspace_case):
    service, root, source, header, nested = workspace_case
    compiled = service.compile(root)
    header.unlink()
    status = service.status(root)
    assert status["sourceDependenciesCurrent"] is False
    assert status["source"]["dependencyError"]
    assert validate_snapshot(root, source, source.read_bytes(), compiled["sourceDependencies"])
    with pytest.raises(ValueError, match="missing"):
        service.compile(root)
    assert service.status(root)["revision"] == compiled["revision"]


@pytest.mark.parametrize("field", ["inputPath", "originalPath"])
def test_changed_frozen_header_is_rejected_even_when_main_is_unchanged(workspace_case, field):
    service, root, source, header, nested = workspace_case
    proof = service.compile(root)["sourceDependencies"]
    (root / proof["documents"][1][field]).write_text("float Shade() { return 0; }\n")
    with pytest.raises(ManifestValidationError, match="compiled include snapshot"):
        service.status(root)


def test_edit_while_compiler_runs_cannot_mutate_its_inputs(workspace_case):
    service, root, source, header, nested = workspace_case
    run = workspace_module.run_operation
    original = header.read_bytes()
    def compiler(operation, values, job, **kwargs):
        frozen = job / "source-input" / "01.hlsli"
        assert frozen.is_file()
        before = frozen.read_bytes()
        header.write_text("float Shade() { return 0; }\n")
        assert frozen.read_bytes() == before
        return run(operation, values, job, **kwargs)
    with mock.patch.object(workspace_module, "run_operation", compiler):
        compiled = service.compile(root)
    proof = compiled["sourceDependencies"]
    assert (root / proof["documents"][1]["originalPath"]).read_bytes() == original
    assert service.status(root)["sourceDependenciesCurrent"] is False


def test_live_check_freezes_includes_without_publishing_revision(workspace_case):
    service, root, source, header, nested = workspace_case
    before = (root / "manifest.json").read_bytes()
    result = service.compile_check(root, source.read_text())
    assert result["ok"] is True, result
    assert (root / "manifest.json").read_bytes() == before


def test_live_check_header_diagnostic_cannot_point_into_main_buffer(workspace_case):
    service, root, source, header, nested = workspace_case
    hlsl_header = header.with_suffix(".hlsl")
    header.rename(hlsl_header)
    source.write_text(source.read_text().replace("lighting.hlsli", "lighting.hlsl"))
    run = workspace_module.run_operation

    def compiler(operation, values, job, **kwargs):
        result = run(operation, values, job, **kwargs)
        (job / "stderr.log").write_text(
            "%s:23:4: error: invalid include expression\n" % (job / "source-input" / "01.hlsl"))
        return replace(result, return_code=1)

    with mock.patch.object(workspace_module, "run_operation", compiler):
        checked = service.compile_check(root, source.read_text())
    assert checked["ok"] is False
    assert all(item["line"] == 0 for item in checked["diagnostics"])
    assert "outside" in checked["diagnostics"][0]["message"]


def test_empty_include_file_is_a_valid_frozen_dependency(workspace_case):
    service, root, source, header, nested = workspace_case
    header.write_bytes(b"")
    source.write_text('#include "lighting.hlsli"\nfloat4 main() : SV_Target { return 1; }\n')
    compiled = service.compile(root)
    assert compiled["sourceDependencies"]["documents"][1]["sourceSha256"] == hashlib.sha256(b"").hexdigest()
    assert service.status(root)["sourceDependenciesCurrent"] is True


def test_compile_accepts_proven_inactive_missing_include(workspace_case):
    service, root, source, header, nested = workspace_case
    source.write_text('#if 0\n#include "absent.hlsli"\n#endif\n'
                      'float4 main() : SV_Target { return 1; }\n')
    compiled = service.compile(root)
    assert len(compiled["sourceDependencies"]["documents"]) == 1
    assert compiled["sourceDependencies"]["digest"] == ""
    assert service.status(root)["sourceDependenciesCurrent"] is True


def test_source_only_begin_preserves_editability_with_an_incomplete_include(work):
    from src.shader_workspace.organization import ShaderOrganizationError
    original = '#include "missing.hlsli"\n' + work.original.decode()
    work.service.source_path.write_bytes(original.encode())
    with pytest.raises(ShaderOrganizationError, match="include graph is unavailable"):
        work.manager.begin(str(work.root))
    changed = original + "\n// Still editing the incomplete source.\n"
    saved = work.manager.save_editor_source(
        str(work.root), work.service.workspace_id,
        hashlib.sha256(original.encode()).hexdigest(), changed)
    assert saved["sourceOnly"] and saved["compileScheduled"] is False
    assert work.service.source_path.read_text() == changed
    context = json.loads((work.root / "ai/organization-sessions" /
        saved["organizationId"] / "semantic-context.json").read_text())
    assert context["sourceDependencyDiagnostic"]["validationDeferred"] is True


def test_tool_modifying_its_frozen_header_does_not_publish_a_candidate(workspace_case):
    service, root, source, header, nested = workspace_case
    run = workspace_module.run_operation
    def tampering_compiler(operation, values, job, **kwargs):
        result = run(operation, values, job, **kwargs)
        (job / "source-input" / "01.hlsli").write_text("float Shade() { return 3; }\n")
        return result
    with mock.patch.object(workspace_module, "run_operation", tampering_compiler):
        with pytest.raises(ManifestValidationError, match="compiled include snapshot"):
            service.compile(root)
    assert service.status(root)["revision"] == 0


def test_portable_main_only_candidate_cache_cannot_bypass_include_compile(workspace_case):
    service, root, source, header, nested = workspace_case
    from src.shader_workspace.errors import ToolExecutionError
    with pytest.raises(ToolExecutionError, match="no include dependency binding"):
        service.hydrate_project_candidate(root, {})


def test_organization_does_not_reuse_a_header_stale_candidate():
    status = {"latestCompiled": {"sourceSha256": "a" * 64, "revision": 1,
              "abiGate": {"status": "compatible"}, "abiCompatible": True, "replayAbiValidated": True,
              "requiresReplayAbiValidation": False, "semanticEquivalenceVerified": False,
              "validationScope": "replay-reflection-abi-compatible"},
              "source": {"currentSha256": "a" * 64}, "revision": 1,
              "abiValidationStatus": "compatible", "abiCompatible": True, "replayAbiValidated": True,
              "sourceDependenciesCurrent": False}
    service = mock.Mock()
    service.status.return_value = status
    manager = object.__new__(ShaderOrganizationManager)
    assert manager._current_compile_and_validation(service, Path("/unused"), "a" * 64) is None


def test_final_binding_seals_dependency_digest():
    binding = ShaderOrganizationManager._candidate_binding("a" * 64,
        {"revision": 1, "rawSha256": "b" * 64, "codeSha256": "c" * 64,
         "sourceDependencies": {"digest": "d" * 64}},
        {"requestId": "e" * 64, "abiReportSha256": "f" * 64})
    assert binding["sourceDependenciesDigest"] == "d" * 64


def test_route_compile_binds_first_hop_to_rewritten_input_not_main_file():
    fixture = route_fixture_module.ShaderWorkspaceRouteChainTests()
    fixture.setUp()
    try:
        service = fixture.service()
        root = fixture.root / "include-route"
        service.create(fixture.catalog_path, fixture.key.catalog_key, root, editable_format="hlsl")
        source = Path(service.decompile(root)["sourcePath"])
        header = source.parent / "lighting.hlsli"
        header.write_text("float4 Lighting() { return 0.5; }\n")
        source.write_text('#include "lighting.hlsli"\nfloat4 main() : SV_Target { return Lighting(); }\n')
        compiled = service.compile(root)
        status = service.status(root)
        proof = compiled["sourceDependencies"]
        assert proof["inputSha256"] != compiled["sourceSha256"]
        assert status["latestCompiled"]["compileHops"][0]["inputSha256"] == proof["inputSha256"]
        assert status["sourceDependenciesCurrent"] is True
        header.write_text("float4 Lighting() { return 1; }\n")
        assert service.status(root)["sourceDependenciesCurrent"] is False
    finally:
        fixture.tearDown()
