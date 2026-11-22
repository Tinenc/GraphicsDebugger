"""Saved shader evidence survives tool updates; execution integrity does not weaken."""
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from unittest import mock

import pytest

from src import server
from src.shader_workspace.artifact_reader import ShaderWorkspaceArtifactReader, ShaderWorkspaceReplayService, _ArtifactValidator
from src.shader_workspace.errors import ManifestValidationError, ToolConfigurationError
from src.shader_workspace.workspace import ShaderWorkspaceService
import test_shader_workspace as support
import test_dxil_access_signature as dxil_support
import test_shader_abi as abi_support
from tests.test_spirv_workspace import spirv_workspace_fixture, configure_native_spirv_assembly
from tests.test_native_glsl_workspace import glsl_fixture
from src.shader_workspace.catalog import capture_identity


@pytest.fixture
def compiled_workspace():
    fixture = abi_support.ShaderWorkspaceAbiGateTests()
    fixture.setUp()
    try:
        service, root, _created, compiled, interface = fixture._compiled_workspace("artifact-reader")
        service.validate_candidate_reflection(root, abi_support.reflection_response(
            interface, compiled["rawSha256"], Path(compiled["candidateBlobPath"]).stat().st_size))
        yield fixture.fixture, service, root
    finally:
        fixture.tearDown()


def test_saved_candidate_can_be_reflected_after_compiler_configuration_is_removed(compiled_workspace):
    fixture, original_service, root = compiled_workspace
    before = original_service.status(root)
    request_before = original_service.candidate_reflection_request(root)
    report = json.loads((root/before['latestCompiled']['abiGate']['reportPath']).read_text('utf-8'))
    fixture.config.unlink()
    with (mock.patch('src.shader_workspace.workspace.ToolRegistry', side_effect=AssertionError('no tool registry')),
          mock.patch('src.shader_workspace.workspace.run_operation', side_effect=AssertionError('no compiler'))):
        replay = ShaderWorkspaceReplayService()
        assert replay.candidate_reflection_request(root) == request_before
        assert replay.validate_candidate_reflection(root, report['response'])['replayAbiValidated']
        assert replay.status(root)['source'] == before['source']
    assert not hasattr(replay, 'compile')


@pytest.mark.parametrize("damage", ["missing-registry", "broken-json", "stale-unrelated-tool", "changed-compiler"])
def test_saved_status_is_independent_of_current_tool_registry(compiled_workspace, damage):
    fixture, service, root = compiled_workspace
    before = service.status(root)
    if damage == "missing-registry":
        fixture.config.unlink()
    elif damage == "broken-json":
        fixture.config.write_text("invalid registry", encoding="utf8")
    elif damage == "stale-unrelated-tool":
        config = json.loads(fixture.config.read_text("utf8"))
        config["tools"]["community-dxbc-assembly"] = dict(config["tools"]["good"], executableSha256="0" * 64)
        fixture.config.write_text(json.dumps(config), encoding="utf8")
    else:
        fixture.fake_tool.write_text("# upgraded compiler\n", encoding="utf8")
    saved = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in root.rglob("*") if path.is_file()}
    with mock.patch("src.shader_workspace.workspace.ToolRegistry", side_effect=AssertionError("tool initialized")), \
         mock.patch("src.shader_workspace.workspace.run_operation", side_effect=AssertionError("tool executed")):
        reader = ShaderWorkspaceArtifactReader()
        result = reader.status(root)
        editable = reader.source_edit_status(root)
    assert result["source"] == before["source"]
    assert result["latestCompiled"] == before["latestCompiled"]
    assert result["baseline"] == before["baseline"] and result["targetProfile"] == before["targetProfile"]
    assert result["replayAbiValidated"] is True  # preserved historical report, not a new replay
    assert result["artifactReadOnly"] is True and result["currentReplayValidated"] is False
    assert result["executionAuthority"] is False and result["representation"]["applyEligible"] is False
    assert editable["source"]["currentSha256"] == result["source"]["currentSha256"]
    assert not any(hasattr(reader, name) for name in ("compile", "decompile", "import_project_candidate"))
    assert saved == {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in root.rglob("*") if path.is_file()}
    with pytest.raises((ToolConfigurationError, ValueError, OSError)):
        ShaderWorkspaceService(fixture.config).compile(root)


@pytest.mark.parametrize("artifact", ["baseline", "candidate", "abi-report", "candidate-source", "original-identity"])
def test_artifact_read_keeps_hash_identity_and_saved_abi_integrity_checks(compiled_workspace, artifact):
    _fixture, _service, root = compiled_workspace
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf8"))
    if artifact == "original-identity":
        manifest["shader"]["rawSha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf8")
    else:
        path = root / ({"baseline": manifest["baseline"]["path"],
                        "candidate": manifest["latestCompiled"]["blobPath"],
                        "abi-report": manifest["latestCompiled"]["abiGate"]["reportPath"],
                        "candidate-source": manifest["latestCompiled"]["sourcePath"]}[artifact])
        path.write_bytes(path.read_bytes() + b"changed")
    with pytest.raises(ManifestValidationError):
        ShaderWorkspaceArtifactReader().status(root)


def test_dxil_route_artifacts_survive_removed_tools_and_keep_physical_ir_checks():
    fixture = dxil_support.DxilAccessWorkspaceEvidenceTests()
    fixture.setUp()
    try:
        service, root, _manifest, _compiled = fixture._compiled_hlsl_evidence_manifest("artifact-dxil")
        before = service.status(root)
        fixture.tools_path.unlink()
        fixture.routes_path.unlink()
        after = ShaderWorkspaceArtifactReader().status(root)
        assert after["latestCompiled"] == before["latestCompiled"]
        assert after["source"] == before["source"]
        assert after["assemblyDisassemblyArtifacts"] == before["assemblyDisassemblyArtifacts"]
        manifest = json.loads((root / "manifest.json").read_text("utf8"))
        archived = _ArtifactValidator()
        candidate_record = manifest["dxilDisassemblyEvidence"]["candidate"]
        assert archived._dedicated_dxil_asm_evidence(root, manifest, "candidate") is not None
        for field in ("inputDetection", "outputDetection"):
            damaged = deepcopy(manifest)
            record = damaged["dxilDisassemblyEvidence"]["candidate"]
            record[field] = {}
            for entry in damaged["history"]:
                if entry.get("candidateDxilDisassemblyEvidence") == candidate_record:
                    entry["candidateDxilDisassemblyEvidence"] = deepcopy(record)
            assert archived._dedicated_dxil_asm_evidence(root, damaged, "candidate") is None
        damaged = deepcopy(manifest)
        for entry in damaged["history"]:
            if entry.get("candidateDxilDisassemblyEvidence") == candidate_record:
                entry["outputSha256"] = "0" * 64
        assert archived._dedicated_dxil_asm_evidence(root, damaged, "candidate") is None
        job_path = root / candidate_record["jobPath"] / "job.json"
        saved_job = job_path.read_bytes()
        for field, value in (("environmentKeys", [123]), ("stdoutPath", "other.log"),
                             ("stderrPath", "other.log"), ("unknownField", "unexpected")):
            job = json.loads(saved_job)
            job[field] = value
            job_path.write_text(json.dumps(job), encoding="utf8")
            assert archived._dedicated_dxil_asm_evidence(root, manifest, "candidate") is None
        job_path.write_bytes(saved_job)
        assert archived._dedicated_dxil_asm_evidence(root, manifest, "candidate") is not None
    finally:
        fixture.tearDown()


def test_public_status_uses_artifact_reader_without_initializing_execution_factory(compiled_workspace, monkeypatch):
    fixture, _service, root = compiled_workspace
    fixture.config.write_text("broken registry", encoding="utf8")
    monkeypatch.setattr(server, "_shader_workspace_service", mock.Mock(side_effect=AssertionError("execution factory")))
    monkeypatch.setattr(server, "_shader_organization_manager", mock.Mock(side_effect=RuntimeError("no manager")))
    monkeypatch.setattr(server, "_shader_cbuffer_delivery_from_status", lambda status: None)
    response = json.loads(server.get_shader_workspace_status(str(root)))
    assert "error" not in response
    result = response["result"]
    assert result["artifactReadOnly"] is True
    assert result["latestCompiled"]["abiGate"]["status"] == "compatible"


def test_native_glsl_saved_candidate_needs_no_current_tool_or_compile_provider(glsl_fixture):
    fixture = glsl_fixture
    service, root = fixture["service"], fixture["root"] / "artifact-glsl"
    service.create(fixture["catalogPath"], fixture["key"], root)
    service.compile(root)
    before = service.status(root)
    service.registry.path.unlink()
    fixture["routesPath"].unlink()
    after = ShaderWorkspaceArtifactReader().status(root)
    assert after["latestCompiled"] == before["latestCompiled"]
    assert after["compilerBinding"] == before["compilerBinding"]
    assert after["currentReplayValidated"] is False


@pytest.mark.parametrize("api,shader_format", [("Vulkan", "spirv"), ("OpenGL", "opengl-spirv")])
def test_native_spirv_saved_validator_jobs_need_no_current_executable(spirv_workspace_fixture, api, shader_format):
    fixture = spirv_workspace_fixture
    catalog = json.loads(fixture["catalogPath"].read_text("utf8"))
    catalog["api"] = api
    catalog["captureId"] = capture_identity(catalog, [fixture["key"]])
    fixture["catalogPath"].write_text(json.dumps(catalog), encoding="utf8")
    service = configure_native_spirv_assembly(fixture, shader_format=shader_format)
    root = fixture["root"] / "artifact-native"
    service.create(fixture["catalogPath"], fixture["key"], root)
    service.decompile(root)
    service.compile(root)
    before = service.status(root)
    service.registry.path.unlink()
    fixture["routesPath"].unlink()
    after = ShaderWorkspaceArtifactReader().status(root)
    assert after["latestCompiled"] == before["latestCompiled"]
    assert after["source"] == before["source"]
    assert after["baseline"] == before["baseline"]
    assert after["currentReplayValidated"] is False
