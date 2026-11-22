import pytest

from src.shader_workspace import AbiValidationError
from tests.test_shader_abi import complete_interface, reflection_response
from tests import test_shader_workspace as support
from src.symbols.matcher import _normalise_catalog


@pytest.fixture
def prepared():
    helper = support.ShaderWorkspaceTests()
    helper.setUp()
    try:
        service, workspace, _ = helper._create("baseline-recovery")
        service.decompile(workspace)
        service.compile(workspace)
        before = service.status(workspace)
        response = reflection_response(complete_interface(), helper.key.raw_sha256, len(helper.blob))
        response["captureId"] = before["capture"]["captureId"]
        yield helper, service, workspace, before, response
    finally:
        helper.tearDown()


def test_missing_baseline_recovers_exact_binary_without_recompiling(prepared):
    helper, service, workspace, before, response = prepared
    old_catalog = helper.catalog.read_bytes()
    request = service.baseline_reflection_request(workspace)
    receipt = service.recover_baseline_reflection(workspace, request, response)
    after = service.status(workspace)
    assert helper.catalog.read_bytes() == old_catalog
    assert after["source"] == before["source"]
    assert after["revision"] == before["revision"]
    assert after["latestCompiled"]["rawSha256"] == before["latestCompiled"]["rawSha256"]
    assert after["baseline"]["abiStatus"] == "captured-reflection-complete"
    assert after["latestCompiled"]["abiGate"]["status"] == "pending-replay-reflection"
    assert receipt["candidateApplied"] is False
    assert after["baselineInterfaceRecovery"] == receipt
    _, normalized = _normalise_catalog(receipt["catalogPath"])
    assert normalized["shaders"][0]["interfaceComplete"] is True
    # Fresh candidate validation now works without recompiling the saved source.
    service.candidate_reflection_request(workspace)
    result = service.validate_candidate_reflection(workspace, response)
    assert result["replayAbiValidated"] is True


def test_stripped_storage_envelopes_recover_through_persistent_catalog(prepared):
    _, service, workspace, before, response = prepared
    interface = response['compiledInterface']
    for block in interface['constantBlocks']:
        block.update(fields=[], layoutCompleteness='opaque')
    resource = interface['readOnlyResources'][0]
    resource.update(resourceType='DescriptorType.Buffer', descriptorType='DescriptorType.Buffer',
        textureType='TextureType.Buffer', isTexture=False)
    resource['variableType'].update(baseType='VarType.Unknown',rows=0,columns=0,elements=1,
        byteSize=16,arrayByteStride=16,matrixByteStride=0,members=[],layoutCompleteness='opaque')
    receipt = service.recover_baseline_reflection(workspace, service.baseline_reflection_request(workspace), response)
    _, normalized = _normalise_catalog(receipt['catalogPath'])
    assert normalized['shaders'][0]['interfaceComplete'] is True
    after = service.status(workspace)
    assert after['source'] == before['source']
    assert after['latestCompiled']['rawSha256'] == before['latestCompiled']['rawSha256']


@pytest.mark.parametrize("field,value", [
    ("rawSha256", "0" * 64), ("reflectedRawSha256", "0" * 64),
    ("captureId", "0" * 64), ("freeTargetResourceSucceeded", False),
    ("entryPoint", "other"), ("encoding", "spirv"),
])
def test_recovery_rejects_mismatched_proof_without_changing_manifest(prepared, field, value):
    _, service, workspace, _, response = prepared
    saved = (workspace / "manifest.json").read_bytes()
    response[field] = value
    with pytest.raises(AbiValidationError):
        service.recover_baseline_reflection(workspace, service.baseline_reflection_request(workspace), response)
    assert (workspace / "manifest.json").read_bytes() == saved


def test_recovery_rejects_stale_request_after_new_compile(prepared):
    _, service, workspace, _, response = prepared
    request = service.baseline_reflection_request(workspace)
    service.compile(workspace)
    with pytest.raises(AbiValidationError, match="workspace changed"):
        service.recover_baseline_reflection(workspace, request, response)


def test_normal_validation_recovers_baseline_before_candidate(prepared, monkeypatch):
    from tests.test_shader_workspace_mcp import load_server_module
    _, service, workspace, before, response = prepared
    server = load_server_module()
    response["captureSessionId"] = "1" * 32
    calls = []
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: service)
    monkeypatch.setattr(server, "_ensure_workspace_capture_catalog_identity",
                        lambda *args, **kwargs: calls.append("bind"))
    monkeypatch.setattr(server, "_sync_shader_override_capture", lambda session: None)
    def reflect(method, params, timeout):
        assert method == "reflect_shader_candidate"
        assert params["expected_capture_id"] == before["capture"]["captureId"]
        calls.append(params["candidate_path"])
        return response
    monkeypatch.setattr(server.ipc, "call", reflect)
    _, after, _, validated = server._fresh_shader_candidate_validation(str(workspace))
    assert calls[0] == "bind"
    assert calls[1].replace("\\", "/").endswith("baseline/shader.bin")
    assert calls[2].replace("\\", "/").endswith("revisions/0001/shader.bin")
    assert validated["replayAbiValidated"] is True
    assert after["source"] == before["source"]

