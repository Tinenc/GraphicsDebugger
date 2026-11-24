"""A current native readback is neither a fresh apply nor arbitrary success."""
import copy
from types import SimpleNamespace
import pytest
from src import project_restore as restore


@pytest.fixture
def case(tmp_path):
    status = {"workspacePath": str(tmp_path), "workspaceId": "1"*32, "revision": 2,
        "stage": "fragment", "entryPoint": "main", "targetProfile": {"profile": "ps_5_0"},
        "capture": {"captureId": "2"*64, "api": "d3d11"}, "shader": {"resourceId": 77},
        "baseline": {"rawSha256": "a"*64, "abiSha256": "b"*64},
        "source": {"currentSha256": "6"*64},
        "latestCompiled": {"kind": "dxbc", "rawSha256": "7"*64, "sourceSha256": "6"*64,
            "replayAbiValidated": True, "abiCompatible": True, "abiGate": {"status": "compatible"}}}
    binding = {"workspaceId": status["workspaceId"], "captureId": "2"*64, "revision": 2,
        "candidateRawSha256": "7"*64, "sourceSha256": "6"*64, "stage": "fragment",
        "entryPoint": "main", "api": "d3d11", "encoding": "dxbc"}
    row = {"replacementId": "3"*32, "operationId": "4"*32, "workspaceId": status["workspaceId"],
        "revision": 2, "anchorEventId": 10, "originalResourceId": 77, "originalRawSha256": "a"*64,
        "candidateRawSha256": "7"*64, "physicalAbiSha256": "b"*64, "targetResourceId": 99,
        "stage": "fragment", "entryPoint": "main", "api": "d3d11", "encoding": "dxbc",
        "resourceScoped": True, "eventScoped": False, "allOriginalResourceUsesAffected": True,
        "originalShaderPreserved": True, "postApplyValidationFailed": False}
    response = {"schema": "renderdoc-ultra-shader-shared-resource-apply-mcp/v1", "operation": "apply-shared-resource",
        "operationId": "5"*32, "scope": "captured-resource", "applied": True, "idempotent": True,
        "appliedByThisCall": False, "replayMutation": False, "evidenceSource": "current-workspace-and-bridge",
        "binding": binding, "replacement": row, "validation": {"validationScope": "already-applied-live-resource",
            "captureSessionId": "c"*32, "compatible": True, "replayAbiValidated": True, "reusedValidation": True,
            "freshPhysicalValidationPerformed": False, "physicalCandidateRevision": 2, "baselineAbiSha256": "b"*64},
        "rollback": {"tool": "rollback_shader_workspace_shared_resource", "workspacePath": str(tmp_path), "replacementId": "3"*32}}
    current = copy.deepcopy(status)
    host = SimpleNamespace(_shader_workspace_artifact_service=lambda: SimpleNamespace(status=lambda path: current))
    def check():
        return restore._validate_workspace_apply_response(host, tmp_path, status, response,
            operation_id="5"*32, expected_revision=2, expected_candidate_raw_sha256="7"*64,
            expected_anchor_event_id=10, capture_session_id="c"*32)
    return response, current, check


def test_live_readback_preserves_its_original_evidence_without_inventing_new_abi(case):
    response, unused, check = case
    assert check() == response
    assert "requestId" not in response["validation"]
    assert "candidateByteSize" not in response["binding"]
    assert response["replacement"]["operationId"] != response["operationId"]


@pytest.mark.parametrize("part,field,value", [
    (None,"replayMutation",True), (None,"appliedByThisCall",True), (None,"evidenceSource","disk-cache"),
    ("replacement","originalResourceId",88), ("replacement","candidateRawSha256","8"*64),
    ("replacement","postApplyValidationFailed",True), ("replacement","targetResourceId",True),
    ("validation","captureSessionId","d"*32), ("validation","freshPhysicalValidationPerformed",True),
    ("binding","sourceSha256","0"*64), ("replacement","physicalAbiSha256","0"*64),
])
def test_foreign_or_relabelled_readback_is_rejected(case,part,field,value):
    response, unused, check = case
    (response if part is None else response[part])[field] = value
    with pytest.raises(RuntimeError): check()


def test_changed_source_after_native_observation_is_rejected(case):
    unused, current, check = case
    current["source"]["currentSha256"] = "0"*64
    with pytest.raises(RuntimeError): check()
