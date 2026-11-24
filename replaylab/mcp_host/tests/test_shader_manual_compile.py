"""Manual saves compile quickly without acquiring AI-organization ABI evidence."""
import copy
import hashlib
import json
import os
from unittest import mock

import pytest

from src.shader_workspace import workspace as ws
from src.shader_workspace.errors import AbiValidationError, ManifestValidationError
from src.shader_workspace.manual_compile import compiler_identity
from tests.test_native_shader_compile import native_workspace, response
from tests.test_shader_workspace import make_dxbc
from tests.test_spirv_workspace import spirv_workspace_fixture, configure_native_spirv_assembly


def test_manual_native_skips_abi_evidence_but_keeps_honest_policy(native_workspace):
    service, root = native_workspace
    service._native_compile_provider = lambda request: response(request, make_dxbc(b"manual output"))
    with mock.patch.object(service, "_ensure_dedicated_baseline_dxil_disassembly", side_effect=AssertionError("ABI evidence")), \
         mock.patch.object(service, "_attempt_dedicated_dxil_disassembly", side_effect=AssertionError("ABI evidence")):
        compiled = service.compile_for_live_edit(root)
    status = service.status(root)
    latest = status["latestCompiled"]
    assert compiled["manualFastCompile"] is True
    assert latest["manualFastCompile"] is True
    assert latest["validationScope"] == "manual-edit-unvalidated"
    assert latest["abiGate"]["status"] == "skipped-manual-edit"
    assert latest["requiresReplayAbiValidation"] is False
    assert latest["replayAbiValidated"] is latest["abiCompatible"] is False


def test_manual_apply_request_never_loads_or_requires_baseline_abi(native_workspace):
    service, root = native_workspace
    service.compile_for_live_edit(root)
    with mock.patch.object(service, "_load_manifest", side_effect=AssertionError("full status")), \
         mock.patch.object(service, "_manifest_baseline_abi", side_effect=AssertionError("ABI parse")):
        request = service.manual_candidate_apply_request(root)
    source = (root / "source/shader.hlsl").read_bytes()
    assert request["binding"]["sourceSha256"] == hashlib.sha256(source).hexdigest()
    assert request["binding"]["sourceDependenciesDigest"] == ""
    assert "baselineAbiSha256" not in request["binding"] and "requestId" not in request
    assert request["replayAbiValidated"] is False
    assert request["candidateBlobBase64"]


def test_existing_binary_replay_service_supports_manual_request_without_tool_registry(native_workspace):
    from src.shader_workspace.artifact_reader import ShaderWorkspaceReplayService
    service, root = native_workspace
    service.compile_for_live_edit(root)
    replay = ShaderWorkspaceReplayService()
    with mock.patch.object(replay._validator, "_process_catalog", side_effect=AssertionError("tool registry")):
        request = replay.manual_candidate_apply_request(root)
    assert request["validationScope"] == "manual-edit-unvalidated"
    assert request["replayAbiValidated"] is False
    assert request["binding"]["candidateRawSha256"] == service.status(root)["latestCompiled"]["rawSha256"]


@pytest.mark.parametrize("change", ["source", "blob", "archived-source", "baseline", "revision"])
def test_manual_request_rejects_changed_compilation_binding(native_workspace, change):
    service, root = native_workspace
    service.compile_for_live_edit(root)
    manifest = json.loads((root / "manifest.json").read_text())
    latest = manifest["latestCompiled"]
    paths = {"source": root / "source/shader.hlsl", "blob": root / latest["blobPath"],
             "archived-source": root / latest["sourcePath"], "baseline": root / manifest["baseline"]["path"]}
    if change == "revision":
        manifest["revision"] += 1
        (root / "manifest.json").write_text(json.dumps(manifest))
    else:
        paths[change].write_bytes(paths[change].read_bytes() + b" changed")
    with pytest.raises(ManifestValidationError):
        service.manual_candidate_apply_request(root)


def test_manual_source_toggle_reuses_bound_compile_without_forged_receipt(native_workspace):
    service, root = native_workspace
    path = root / "source/shader.hlsl"
    first_text = path.read_bytes()
    calls = []
    def provider(request):
        calls.append(request)
        return response(request, make_dxbc(request["expected_sha256"].encode()))
    service._native_compile_provider = provider
    with mock.patch.object(ws, "run_operation", wraps=ws.run_operation) as compile_process:
        first = service.compile_for_live_edit(root)
        path.write_bytes(first_text + b"\n// newer\n")
        second = service.compile_for_live_edit(root)
        path.write_bytes(first_text)
        third = service.compile_for_live_edit(root)
        assert compile_process.call_count == 2
    assert [first["revision"], second["revision"], third["revision"]] == [1, 2, 3]
    assert not calls and third["compilationCacheHit"] is True
    assert third["rawSha256"] == first["rawSha256"]
    assert third["sourceSha256"] == first["sourceSha256"] != second["sourceSha256"]
    assert service.status(root)["latestCompiled"]["replayAbiValidated"] is False
    service.manual_candidate_apply_request(root)
    # Strict AI compilation does not consume this unvalidated cache.
    fourth = service.compile(root)
    assert len(calls) == 1 and fourth["requiresReplayAbiValidation"] is True


def test_manual_compile_cache_invalidates_when_compiler_file_changes(native_workspace):
    service, root = native_workspace
    with mock.patch.object(ws, "run_operation", wraps=ws.run_operation) as compiler:
        service.compile_for_live_edit(root)
        hit = service.compile_for_live_edit(root)
        assert hit["compilationCacheHit"] is True and compiler.call_count == 1
        manifest = json.loads((root / "manifest.json").read_text())
        operation = service.registry.operation(manifest["tools"]["compiler"]["toolId"], "compile")
        fixed_path = operation.fixed_file_hashes[0][0]
        stat = fixed_path.stat()
        os.utime(fixed_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2000000000))
        result = service.compile_for_live_edit(root)
        assert not result.get("compilationCacheHit") and compiler.call_count == 2


def test_manual_cache_tracks_include_bytes_and_old_cache_remains_readable(native_workspace):
    service, root = native_workspace
    path = root / "source/shader.hlsl"
    path.write_text('#include "shared.hlsli"\nfloat4 main():SV_Target { return Value(); }')
    header = path.parent / "shared.hlsli"
    header.write_text("float4 Value() { return 1; }")
    with mock.patch.object(ws, "run_operation", wraps=ws.run_operation) as compile_process:
        first = service.compile_for_live_edit(root)
        header.write_text("float4 Value() { return 2; }")
        service.compile_for_live_edit(root)
        header.write_text("float4 Value() { return 1; }")
        reused = service.compile_for_live_edit(root)
        assert compile_process.call_count == 2 and reused["compilationCacheHit"] is True
        assert reused["rawSha256"] == first["rawSha256"]
        header.write_text("float4 Value() { return 3; }")
        # Current headers may differ from a historical compile snapshot.
        assert service.status(root)["sourceDependenciesCurrent"] is False
        with pytest.raises(ManifestValidationError, match="expected saved candidate"):
            service.manual_candidate_apply_request(root)
        service.compile_for_live_edit(root)
        assert compile_process.call_count == 3


@pytest.mark.parametrize("field", ["targetProfile", "routePlan", "tools", "shader", "capture"])
def test_manual_cache_key_includes_original_and_compiler_contract(native_workspace, field):
    service, root = native_workspace
    before = json.loads((root / "manifest.json").read_text())
    after = copy.deepcopy(before)
    after[field] = {"changed": True}
    assert compiler_identity(before) != compiler_identity(after)


def test_manually_compiled_source_does_not_enter_strict_reflection(native_workspace):
    service, root = native_workspace
    service.compile_for_live_edit(root)
    with pytest.raises(AbiValidationError):
        service.candidate_reflection_request(root)


def test_failed_manual_compile_keeps_previous_candidate(native_workspace):
    service, root = native_workspace
    first = service.compile_for_live_edit(root)
    (root / "source/shader.hlsl").write_text("float4 main():SV_Target { broken; }")
    with mock.patch.object(ws, "run_operation", side_effect=RuntimeError("compiler error")):
        with pytest.raises(RuntimeError, match="compiler error"):
            service.compile_for_live_edit(root)
    assert service.status(root)["latestCompiled"]["rawSha256"] == first["rawSha256"]
    assert service.status(root)["latestCompiled"]["revision"] == first["revision"]


def test_manual_spirv_skips_both_validator_jobs_and_strict_compile_keeps_them(spirv_workspace_fixture):
    fixture = spirv_workspace_fixture
    service = configure_native_spirv_assembly(fixture)
    root = fixture["root"] / "manual-spirv"
    service.create(fixture["catalogPath"], fixture["key"], root, editable_format="spirv-asm")
    service.decompile(root)
    with mock.patch.object(service, "_run_bound_validators", side_effect=AssertionError("extra validator")):
        result = service.compile_for_live_edit(root)
    assert result["manualFastCompile"] is True
    assert not service.status(root)["latestCompiled"].get("validatorJobs")
    request = service.manual_candidate_apply_request(root)
    assert request["binding"]["api"] == "vulkan"
    assert request["binding"]["encoding"] == "spirv"
    with mock.patch.object(service, "_run_bound_validators", wraps=service._run_bound_validators) as validator:
        strict = service.compile(root)
    assert validator.call_count == 2
    assert strict["requiresReplayAbiValidation"] is True
    assert len(service.status(root)["latestCompiled"]["validatorJobs"]) == 1


def test_manual_dxil_route_avoids_dedicated_disassembly():
    from tests.test_shader_workspace_routes import ShaderWorkspaceRouteChainTests
    case = ShaderWorkspaceRouteChainTests(); case.setUp()
    try:
        service = case.service()
        root = case.root / "manual-dxil"
        service.create(case.catalog_path, case.key.catalog_key, root)
        service.decompile(root)
        with mock.patch.object(service, "_ensure_dedicated_baseline_dxil_disassembly", side_effect=AssertionError("baseline IR")), \
             mock.patch.object(service, "_attempt_dedicated_dxil_disassembly", side_effect=AssertionError("candidate IR")):
            compiled = service.compile_for_live_edit(root)
        assert compiled["kind"] == "DXIL" and compiled["manualFastCompile"] is True
        assert service.status(root)["latestCompiled"]["abiCompatible"] is False
    finally:
        case.tearDown()
