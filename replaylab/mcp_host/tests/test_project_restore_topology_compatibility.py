"""Only verified historical topology aliases may cross stored/live hash boundaries."""
import copy

import pytest

from src import project_restore as restore
from src.shader_workspace.abi import parse_shader_abi
from tests.test_shader_abi import complete_interface


def pair(tmp_path):
    interface = complete_interface()
    interface["outputTopology"] = ""
    old = parse_shader_abi(interface)
    fresh = parse_shader_abi(dict(interface, outputTopology="Unknown"))
    status = {
        "workspacePath": str(tmp_path), "workspaceId": "1" * 32,
        "stage": old.stage, "entryPoint": old.entry_point, "revision": 1,
        "shader": {"resourceId": 7},
        "capture": {"api": "D3D11", "captureId": "2" * 64,
                    "capturePath": str(tmp_path / "test.rdc")},
        "source": {"present": True, "currentSha256": "3" * 64},
        "baseline": {"abiSha256": old.sha256, "compiledInterface": interface,
                     "rawSha256": "4" * 64},
        "latestCompiled": {"kind": "DXBC", "revision": 1,
                           "rawSha256": "5" * 64, "sourceSha256": "3" * 64},
        "targetProfile": {"profile": "ps_5_0"},
    }
    material = restore._shader_pair_validation_material(status)
    receipt = {"signature": "6" * 64, "payload": {
        "schema": "replaylab-validated-shader-restore/v1", "captureSha256": "7" * 64,
        **{key: value for key, value in material.items()},
        "physicalAbiSha256": fresh.sha256, "abiRequestId": "8" * 64,
        "abiReportSha256": "9" * 64,
    }}
    replacement = {
        "schema": "renderdoc-ultra-shared-shader-replacement/v1", "schemaVersion": 1,
        "workspaceId": status["workspaceId"], "revision": 1,
        "captureId": status["capture"]["captureId"], "originalResourceId": 7,
        "resourceScoped": True, "eventScoped": False, "allOriginalResourceUsesAffected": True,
        **material, "physicalAbiSha256": fresh.sha256,
        "restoreValidationReceipt": receipt, "abiRequestId": "8" * 64,
        "abiReportSha256": "9" * 64,
    }
    lease = {"abiLeaseRequestId": "8" * 64, "abiLeaseReportSha256": "9" * 64,
             "abiLeaseCandidateRawSha256": "5" * 64, "abiLeaseRevision": 1}
    return status, material, receipt, replacement, lease


def test_verified_pair_round_trip_preserves_stored_hash_and_signed_payload(tmp_path):
    status, material, receipt, replacement, lease = pair(tmp_path)
    original = copy.deepcopy((status, material, receipt, replacement))
    assert not restore._shader_pair_receipt_matches(receipt, material)
    assert restore._shader_pair_receipt_matches(receipt, material, status)
    path = restore._shader_pair_validation_path(tmp_path, status)
    assert restore._persist_workspace_shader_pair_validation(status, replacement, lease)
    cached = restore._load_workspace_shader_pair_validation(status)
    assert cached["pair"] == material
    assert cached["replacement"]["restoreValidationReceipt"] == receipt
    assert restore._shader_pair_validation_path(tmp_path, status) == path
    assert (status, material, receipt, replacement) == original


@pytest.mark.parametrize("field,value", [
    ("originalRawSha256", "a" * 64), ("candidateRawSha256", "b" * 64),
    ("physicalAbiSha256", "c" * 64), ("api", "vulkan"), ("stage", "vertex"),
    ("entryPoint", "other"), ("encoding", "spirv"), ("targetProfile", "ps_4_0"),
])
def test_pair_rejects_other_payload_differences(tmp_path, field, value):
    status, material, receipt, replacement, lease = pair(tmp_path)
    receipt["payload"][field] = value
    assert not restore._shader_pair_receipt_matches(receipt, material, status)
    assert not restore._persist_workspace_shader_pair_validation(status, replacement, lease)


@pytest.mark.parametrize("change", ["missing", "tampered", "hash", "topology"])
def test_no_alias_without_complete_verified_physical_evidence(tmp_path, change):
    status, material, receipt, replacement, lease = pair(tmp_path)
    baseline = status["baseline"]
    if change == "missing":
        baseline.pop("compiledInterface")
    elif change == "tampered":
        baseline["compiledInterface"]["readOnlyResources"][0]["bind"] += 1
    elif change == "hash":
        baseline["abiSha256"] = "d" * 64
    else:
        baseline["compiledInterface"]["outputTopology"] = "TriangleList"
        baseline["abiSha256"] = parse_shader_abi(baseline["compiledInterface"]).sha256
        material["physicalAbiSha256"] = baseline["abiSha256"]
    assert not restore._shader_pair_receipt_matches(receipt, material, status)
    assert not restore._persist_workspace_shader_pair_validation(status, replacement, lease)


def publication_pair(tmp_path):
    status, material, receipt, replacement, lease = pair(tmp_path)
    status["baseline"]["kind"] = "DXBC"
    identity = {"resourceId": 7, "stage": status["stage"], "entryPoint": status["entryPoint"],
                "rawSha256": status["baseline"]["rawSha256"], "api": "d3d11", "encoding": "dxbc",
                "abiHash": status["baseline"]["abiSha256"], "layoutHash": "a" * 64,
                "stableKeyComplete": True, "anchorEventId": 3}
    record = {"identity": identity, "managedWorkspace": {"workspaceId": status["workspaceId"], "path": "workspace"},
              "candidateRevision": 1, "candidateRawSha256": status["latestCompiled"]["rawSha256"]}
    replacement["anchorEventId"] = 3
    return status, record, replacement


def test_realized_projection_keeps_signed_live_receipt_and_exact_record_validation(tmp_path):
    status, record, replacement = publication_pair(tmp_path)
    before = copy.deepcopy((status, record, replacement))
    realized = restore._realized_receipt_from_apply(record, {"replacement": replacement}, status)
    assert realized["physicalAbiSha256"] == record["identity"]["abiHash"]
    assert realized["physicalAbiSha256"] != replacement["physicalAbiSha256"]
    assert restore._normalised_receipts_for_records([record], [realized]) == [realized]
    assert (status, record, replacement) == before
    with pytest.raises(RuntimeError):
        restore._realized_receipt_from_apply(record, {"replacement": replacement})


@pytest.mark.parametrize("field,value", [
    ("physicalAbiSha256", "b" * 64), ("originalRawSha256", "c" * 64),
    ("candidateRawSha256", "d" * 64), ("originalResourceId", 8),
    ("workspaceId", "e" * 32), ("revision", 2), ("entryPoint", "other"),
    ("api", "vulkan"), ("encoding", "spirv"),
])
def test_realized_alias_projection_rejects_changed_binding(tmp_path, field, value):
    status, record, replacement = publication_pair(tmp_path)
    replacement[field] = value
    with pytest.raises(RuntimeError):
        restore._realized_receipt_from_apply(record, {"replacement": replacement}, status)


def test_realized_projection_rejects_tampered_layout_evidence(tmp_path):
    status, record, replacement = publication_pair(tmp_path)
    status["baseline"]["compiledInterface"]["constantBlocks"][0]["byteSize"] += 16
    with pytest.raises(Exception):
        restore._realized_receipt_from_apply(record, {"replacement": replacement}, status)


def test_publication_retains_verified_fresh_catalog_identity(tmp_path):
    status, record, replacement = publication_pair(tmp_path)
    record["identity"]["abiHash"] = replacement["physicalAbiSha256"]
    actual = restore._publication_shader_identity(record["identity"], status, replacement)
    assert actual["abiHash"] == record["identity"]["abiHash"]
    record["identity"]["abiHash"] = "f" * 64
    with pytest.raises(RuntimeError):
        restore._publication_shader_identity(record["identity"], status, replacement)
