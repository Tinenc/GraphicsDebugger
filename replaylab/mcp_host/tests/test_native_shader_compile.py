"""Native replay source routes retain exact target identity and safe fallback."""
import hashlib
import importlib.util
import json
import os
import struct
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import pytest

from src.shader_workspace import workspace as ws
from src.shader_workspace.errors import ManifestValidationError, ToolExecutionError
from src.shader_workspace.native_compile import SCHEMA, invoke
from src.shader_workspace.organization import ShaderOrganizationManager, ShaderOrganizationError
from tests import test_shader_workspace as fixtures
from tests.test_shader_workspace import make_dxbc
from tests.test_shader_workspace_routes import make_dxil


def response(request, raw=None, status="succeeded"):
    value = dict(schema=SCHEMA, captureSessionId="a" * 32,
        capturePath=os.path.normcase(os.path.abspath(request["capture_path"])),
        sourceSha256=request["expected_sha256"], sourceFormat=request["source_format"],
        targetApi=request["target_api"], stage=request["stage"], entryPoint=request["entry_point"],
        targetProfile=request["target_profile"], outputFormat=request["output_format"], status=status,
        reason="native-compiler-diagnostic" if status == "failed" else "",
        buildTargetShaderSucceeded=status == "succeeded", freeTargetResourceSucceeded=True,
        replayApplied=False, buildMessages="")
    if raw is not None:
        path = Path(request["candidate_path"]).parent / "native.bin"
        path.write_bytes(raw)
        value.update(outputPath=str(path), rawSha256=hashlib.sha256(raw).hexdigest(), byteSize=len(raw))
    return value


@pytest.fixture
def native_workspace():
    case = fixtures.ShaderWorkspaceTests(); case.setUp()
    try:
        service, root, unused = case._create("native-source")
        service.decompile(root)
        yield service, root
    finally:
        case.tearDown()


def test_native_source_precedes_external_compiler_and_remains_pending_abi(native_workspace):
    service, root = native_workspace
    service._native_compile_provider = lambda request: response(request, make_dxbc(b"native output"))
    with mock.patch.object(ws, "run_operation", side_effect=AssertionError("external compiler must not run")):
        compiled = service.compile(root)
    status = service.status(root)
    assert compiled["compilerBackend"] == "renderdoc-native"
    assert status["latestCompiled"]["requiresReplayAbiValidation"]
    assert not status["latestCompiled"]["replayAbiValidated"]
    assert status["abiValidationStatus"] == "blocked-baseline-interface-unavailable"


def test_native_diagnostic_falls_back_without_changing_source(native_workspace):
    service, root = native_workspace
    source = (root / "source/shader.hlsl").read_bytes()
    calls = []
    def compiler(request):
        calls.append(request)
        return response(request, status="failed")
    service._native_compile_provider = compiler
    result = service.compile(root)
    assert len(calls) == 1 and "nativeCompile" not in service.status(root)["latestCompiled"]
    assert result["revision"] == 1 and (root / "source/shader.hlsl").read_bytes() == source
    manifest = json.loads((root / "manifest.json").read_text())
    attempt = next(row for row in manifest["history"] if row["operation"] == "native-compile-attempt")
    assert attempt["reason"] == "native-compiler-diagnostic"


def test_native_wrong_source_evidence_never_falls_back_as_if_valid(native_workspace):
    service, root = native_workspace
    service._native_compile_provider = lambda request: {**response(request, make_dxbc()), "sourceSha256": "0" * 64}
    with pytest.raises(ToolExecutionError, match="mismatched"):
        service.compile(root)
    assert service.status(root)["latestCompiled"] is None


@pytest.mark.parametrize("corrupt", [{"buildTargetShaderSucceeded":False},
    {"reason":"native-replay-unavailable"}, {"reason":"native-replay-unavailable","status":"unsupported"}])
def test_native_bad_success_never_publishes_a_corrupt_manifest(native_workspace, corrupt):
    service, root=native_workspace
    before=(root / "manifest.json").read_bytes()
    service._native_compile_provider=lambda request:{**response(request,make_dxbc()),**corrupt}
    with pytest.raises(ToolExecutionError): service.compile(root)
    assert (root / "manifest.json").read_bytes()==before
    assert service.status(root)["latestCompiled"] is None


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
def test_source_edit_during_native_compile_is_preserved_and_not_published(native_workspace, outcome):
    service, root = native_workspace
    path = root / "source/shader.hlsl"
    def edit(request):
        path.write_text("// concurrent user edit\nfloat4 main() : SV_Target { return 2; }")
        return response(request, make_dxbc() if outcome == "succeeded" else None, status=outcome)
    service._native_compile_provider = edit
    with pytest.raises(ManifestValidationError, match="source changed"):
        service.compile(root)
    assert service.status(root)["latestCompiled"] is None
    assert path.read_text().startswith("// concurrent user edit")


def test_wrong_native_profile_falls_back_to_captured_profile(native_workspace):
    service, root = native_workspace
    blob = bytearray(make_dxbc()); blob[44] = 0x51
    service._native_compile_provider = lambda request: response(request, bytes(blob))
    result = service.compile(root)
    assert "nativeCompile" not in service.status(root)["latestCompiled"]
    manifest = json.loads((root / "manifest.json").read_text())
    assert any(row.get("reason") == "native-output-contract-mismatch" for row in manifest["history"])


def test_explicit_fixed_compiler_recovery_never_attempts_native(native_workspace):
    service, root = native_workspace
    service._native_compile_provider = mock.Mock(side_effect=AssertionError("native route already failed"))
    service.compile(root, prefer_native=False)
    service._native_compile_provider.assert_not_called()


def test_hlsl_library_uses_its_registered_compiler_when_native_replay_cannot_build_it():
    """A native optimization cannot block a valid compile-only library route."""
    case = fixtures.ShaderWorkspaceTests(); case.setUp()
    try:
        raw = bytearray(make_dxil())
        chunk = struct.unpack_from("<I", raw, 32)[0]
        struct.pack_into("<I", raw, chunk + 8, 0x00060063)  # Library, SM 6.3.
        raw = bytes(raw)
        case.key = ws.ShaderKey(92, "raygen", "RayMain", hashlib.sha256(raw).hexdigest())
        catalog_path = case._write_catalog(raw)
        catalog = json.loads(catalog_path.read_text())
        catalog["api"] = "D3D12"
        from src.shader_workspace.catalog import capture_identity
        catalog["captureId"] = capture_identity(catalog, [case.key])
        catalog_path.write_text(json.dumps(catalog))
        service, root, _ = case._create("hlsl-library")
        service.decompile(root)
        source_before = (root / "source/shader.hlsl").read_bytes()
        service._native_compile_provider = mock.Mock(
            side_effect=AssertionError("RenderDoc native source route does not accept lib_6_3"))
        compiled = service.compile(root)
        service._native_compile_provider.assert_not_called()
        status = service.status(root)
        assert Path(compiled["candidateBlobPath"]).read_bytes() == raw
        assert (root / "source/shader.hlsl").read_bytes() == source_before
        assert status["targetProfile"]["profile"] == "lib_6_3"
        assert status["latestCompiled"]["requiresReplayAbiValidation"] is True
        assert status["latestCompiled"]["abiCompatible"] is False
        manifest = json.loads((root / "manifest.json").read_text())
        assert any(row.get("reason") == "native-source-stage-unavailable"
                   for row in manifest["history"])
    finally:
        case.tearDown()


def test_native_artifact_tampering_invalidates_candidate(native_workspace):
    service, root = native_workspace
    service._native_compile_provider = lambda request: response(request, make_dxbc())
    result = service.compile(root)
    (root / result["nativeCompile"]["inputPath"]).write_text("changed")
    with pytest.raises(ManifestValidationError, match="binding changed"):
        service.status(root)


def test_native_compiler_has_no_include_handler_uses_frozen_external_route(native_workspace):
    service, root = native_workspace
    path = root / "source/shader.hlsl"
    path.write_text('#include "shared.hlsli"\nfloat4 main() : SV_Target { return One(); }')
    (path.parent / "shared.hlsli").write_text("float4 One() { return 1; }")
    service._native_compile_provider = mock.Mock(side_effect=AssertionError("cannot resolve includes natively"))
    result = service.compile(root)
    assert len(result["sourceDependencies"]["documents"]) == 2
    assert service.status(root)["sourceDependenciesCurrent"]


def bridge_module():
    path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge/native_shader_compile.py"
    spec = importlib.util.spec_from_file_location("tested_native_compile", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def fake_bridge(tmp_path, api, language, encoding, *, supported=True, artifact=True):
    job = tmp_path / "jobs/native-compile-r0001-test"; job.mkdir(parents=True)
    path = job / ("input." + language); path.write_text("void main() {}")
    request = dict(candidate_path=str(path), expected_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        expected_capture_session_id="a" * 32, capture_path=str(tmp_path / "capture.rdc"),
        source_format=language, target_api=api, output_format=encoding,
        target_profile="ps_5_0" if language == "hlsl" else encoding, stage="fragment", entry_point="main")
    calls = []
    encodings = NS(HLSL="hlsl", GLSL="glsl")
    class Ctrl:
        def GetTargetShaderEncodings(self): return [language] if supported else []
        def GetShaderEntryPoints(self, target): return [NS(name="main", stage="fragment")]
        def GetShader(self, pipe, target, entry): return NS(encoding="ShaderEncoding." + encoding.replace("opengl-spirv", "OpenGLSPIRV"), raw=(path.read_bytes() if encoding == "glsl" else b"compiled") if artifact else b"")
        def FreeTargetResource(self, target): calls.append(("free", target))
    ctrl = Ctrl()
    def build(ctrl, entry, encoding, source, flags, stage):
        calls.append(("build", entry, encoding, source, flags.flags)); return 42, ""
    bridge = NS(_capture_session_id="a" * 32, _current_capture_token=lambda: (1, request["capture_path"]),
        _read_shader_candidate=lambda p: (p["candidate_path"], path.read_bytes(), p["expected_sha256"]),
        _normalise_stage_name=lambda s:s, _available_shader_stages=lambda s:[("fragment","fragment")],
        _shader_api_token=lambda c:api, _replay=lambda fn:fn(ctrl),
        _resource_id_int=lambda r:r or 0, _build_target_shader=build,
        _exact_shader_entry=lambda entries, name, stage:entries[0], _shader_raw_bytes=lambda r:r.raw,
        _register_orphan_shader_target=lambda target:calls.append(("orphan", target)),
        rd=NS(ShaderEncoding=encodings, ShaderCompileFlags=lambda:NS(flags=[]),
              ShaderCompileFlag=lambda:NS(), ResourceId=lambda:0))
    return bridge, request, calls, ctrl


@pytest.mark.parametrize("api,language,encoding", [("d3d11","hlsl","dxbc"), ("d3d12","hlsl","dxil"),
    ("vulkan","glsl","spirv"), ("opengl","glsl","glsl")])
def test_each_native_api_uses_advertised_source_encoding_and_frees_target(tmp_path, api, language, encoding):
    bridge, request, calls, ctrl = fake_bridge(tmp_path, api, language, encoding)
    result = bridge_module().compile_source(bridge, request)
    assert result["status"] == "succeeded" and result["replayApplied"] is False
    assert calls[0][2] == language and calls[-1] == ("free",42)
    assert Path(result["outputPath"]).read_bytes() == (Path(request["candidate_path"]).read_bytes() if encoding == "glsl" else b"compiled")
    if language == "hlsl": assert calls[0][-1][0].value == "/T ps_5_0"


@pytest.mark.parametrize("supported,artifact,reason", [(False,True,"source-encoding-unavailable"),
    (True,False,"native-compiled-artifact-unavailable")])
def test_actual_native_limit_reports_fallback_without_applying(tmp_path,supported,artifact,reason):
    bridge, request, calls, ctrl = fake_bridge(tmp_path,"opengl","glsl","glsl",supported=supported,artifact=artifact)
    result = bridge_module().compile_source(bridge, request)
    assert result["status"] == "unsupported" and result["reason"] == reason
    assert not (Path(request["candidate_path"]).parent / "native.bin").exists()
    assert calls[-1:] == ([("free",42)] if supported else [])


@pytest.mark.parametrize("invalid", ["mesh", "profile", "size"])
def test_native_glsl_rejects_unsupported_source_contract_before_driver_build(tmp_path, invalid):
    bridge, request, calls, _ = fake_bridge(tmp_path, "opengl", "glsl", "glsl")
    if invalid == "mesh":
        request["stage"] = "mesh"
        bridge._available_shader_stages = lambda stages: [("mesh", "mesh")]
    elif invalid == "profile":
        request["target_profile"] = "spirv"
    else:
        Path(request["candidate_path"]).write_bytes(b"a" * (4 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="OpenGL GLSL source/profile"):
        bridge_module().compile_source(bridge, request)
    assert calls == []


def test_native_capture_change_never_publishes_artifact(tmp_path):
    bridge, request, calls, ctrl = fake_bridge(tmp_path,"vulkan","glsl","spirv")
    def changed(target):
        calls.append(("free",target)); bridge._capture_session_id="b" * 32
    ctrl.FreeTargetResource=changed
    with pytest.raises(RuntimeError,match="capture changed"):
        bridge_module().compile_source(bridge,request)
    assert not (Path(request["candidate_path"]).parent / "native.bin").exists()


def test_host_native_lookup_is_capture_identity_only():
    ipc=mock.Mock(); ipc.call.return_value={"loaded":False}
    assert invoke(ipc,{})["reason"]=="native-replay-not-loaded"
    ipc.call.assert_called_once_with("get_capture_status",{"identity_only":True},timeout=30)


def test_host_rejects_response_from_another_capture_session():
    ipc=mock.Mock(); ipc.call.side_effect=[{"loaded":True,"captureSessionId":"a" * 32,
                                          "api":"GraphicsAPI.Vulkan","capturePath":"/capture.rdc"},
                                         {"captureSessionId":"b" * 32}]
    with pytest.raises(ToolExecutionError,match="another capture session"):
        invoke(ipc,{"target_api":"vulkan","capture_path":"/capture.rdc"})


def test_other_loaded_game_does_not_supply_native_compiler_environment():
    ipc=mock.Mock(); ipc.call.return_value={"loaded":True,"captureSessionId":"a"*32,
        "api":"GraphicsAPI.D3D11","capturePath":"/other.rdc"}
    result=invoke(ipc,{"target_api":"vulkan","capture_path":"/capture.rdc"})
    assert result["reason"]=="native-replay-target-unavailable" and ipc.call.call_count==1


@pytest.mark.parametrize("matching_proof", [True,False])
@pytest.mark.parametrize("external_route", [True, False])
def test_organization_retries_only_exact_native_abi_rejection_once(tmp_path, matching_proof, external_route):
    root=tmp_path.resolve(); source_sha="1" * 64
    compiled=[dict(revision=i,rawSha256=str(i+1)*64,codeSha256=str(i+1)*64,sourceSha256=source_sha)
              for i in (1,2)]
    compiled[0]["nativeCompile"]={"reportSha256":"4"*64}
    validation=[dict(revision=i,requestId=str(i+4)*64,abiReportSha256=str(i+6)*64,
                     compatible=i==2,replayAbiValidated=i==2,requiresReplayAbiValidation=i!=2)
                for i in (1,2)]
    failed={"source":{"currentSha256":source_sha},"lastAbiAttempt":{
        "status":"incompatible","revision":1,"candidateRawSha256":compiled[0]["rawSha256"],
        "sourceSha256":source_sha if matching_proof else "0"*64}}
    if not external_route:
        failed["routePlan"] = {"compile": [], "nativeSource": {"api": "opengl"}}
    final={"workspacePath":str(root),"revision":2,"source":{"currentSha256":source_sha},
        "latestCompiled":{**compiled[1],"abiCompatible":True,"abiGate":{
            "status":"compatible","requestId":validation[1]["requestId"],
            "reportSha256":validation[1]["abiReportSha256"]}}}
    service=NS(compile=mock.Mock(side_effect=compiled),status=mock.Mock(side_effect=[failed,final,failed]))
    manager=ShaderOrganizationManager(lambda:service, mock.Mock(side_effect=validation))
    manager._current_compile_and_validation=mock.Mock(return_value=None)
    if matching_proof and external_route:
        result, checked=manager._compile_and_validate(service,root,source_sha,"proposal")
        assert result["nativeCompileRecovery"]["failedRevision"]==1 and checked["compatible"]
        assert service.compile.call_args_list == [mock.call(str(root)),mock.call(str(root),prefer_native=False)]
    else:
        with pytest.raises(ShaderOrganizationError,match="did not pass"):
            manager._compile_and_validate(service,root,source_sha,"proposal")
        assert service.compile.call_count==1
