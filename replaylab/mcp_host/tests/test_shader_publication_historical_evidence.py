"""Metadata publication retains exact archived evidence without executing tools."""
import copy
import hashlib
from pathlib import Path
from unittest import mock

import pytest

from src import shader_final_publication as publication
from tests.test_projects import _compiler_binding


def fixture(tmp_path):
    profile = {"profile": "ps_5_0", "containerFormat": "dxbc"}
    status = {"artifactReadOnly": True, "workspacePath": str(tmp_path / "旧源码"),
        "workspaceId": "1" * 32, "capture": {"captureId": "2" * 64},
        "shader": {"resourceId": 17}, "stage": "fragment", "entryPoint": "main",
        "source": {"currentSha256": "3" * 64, "format": "hlsl"},
        "baseline": {"rawSha256": "4" * 64, "abiSha256": "5" * 64},
        "targetProfile": profile, "routePlan": {"planSha256": "3" * 64},
        "compilerBinding": None, "assemblyDisassemblyArtifacts": [],
        "latestCompiled": {"sourceSha256": "3" * 64, "rawSha256": "6" * 64, "revision": 2}}
    old = {"knowledgeId": "7" * 32, "workspaceId": status["workspaceId"],
        "workspacePath": status["workspacePath"], "captureId": status["capture"]["captureId"],
        "originResourceId": 17, "stage": "fragment", "entryPoint": "main", "sourceSha256": "3" * 64,
        "rawSha256": "4" * 64, "baselineAbiSha256": "5" * 64, "targetProfile": profile,
        "compiledCandidate": {"sourceSha256": "3" * 64, "rawSha256": "6" * 64,
            "sourceRevision": 2, "targetProfile": profile}, "compilerBinding": _compiler_binding(profile)}
    artifact = tmp_path / "原始反汇编.dxbc-asm"
    artifact.write_bytes(b"ps_5_0\nret\n")
    old["assemblyDisassemblyArtifacts"] = [{"kind": "native-disassembly", "role": "baseline",
        "format": "dxbc-asm", "inputFormat": "dxbc", "inputRawSha256": "4" * 64,
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(), "byteSize": artifact.stat().st_size,
        "routePlanSha256": "3" * 64, "toolId": "archived", "toolConfigDigest": "8" * 64,
        "executableSha256": "9" * 64, "routeId": "archived-dxbc", "operation": "decompile",
        "physicalEvidence": True, "displayEvidenceOnly": True, "editableSource": False,
        "applyAuthorization": False, "artifactPath": artifact.name}]
    weak = {**copy.deepcopy(old), "knowledgeId": "a" * 32, "compilerBinding": None,
            "assemblyDisassemblyArtifacts": []}
    replay, project = mock.Mock(), mock.Mock()
    project.replay_service.return_value = replay
    replay.find_current_workspace_source_head.return_value = {
        "headKey": "b" * 64, "generation": 2, "knowledge": weak}
    replay._load.return_value = {"knowledge": {"workspaces": [old, weak]}}
    replay._workspace_knowledge_from_manifest.side_effect = lambda m, k: next(
        copy.deepcopy(item) for item in m["knowledge"]["workspaces"] if item["knowledgeId"] == k)
    replay._artifact.side_effect = lambda path, label: tmp_path / path
    return project, status, old, artifact


def test_repairs_weaker_current_head_from_exact_immutable_history(tmp_path):
    project, status, old, artifact = fixture(tmp_path)
    before = copy.deepcopy(status), copy.deepcopy(old), artifact.read_bytes()
    result = publication.preserve_historical_artifacts(project, status)
    assert result["compilerBinding"] == old["compilerBinding"]
    assert result["compilerBinding"]["executionAuthority"] is False
    assert result["assemblyDisassemblyArtifacts"][0]["path"] == str(artifact)
    assert result["historicalPublicationEvidence"]["knowledgeIds"] == [old["knowledgeId"]]
    assert result["historicalPublicationEvidence"]["expectedHead"]["knowledgeId"] == "a" * 32
    assert (status, old, artifact.read_bytes()) == before
    project.register_workspace.assert_not_called()


@pytest.mark.parametrize("field,value", [("sourceSha256", "c" * 64), ("rawSha256", "c" * 64),
    ("targetProfile", {"profile": "ps_4_0"}), ("originResourceId", 18),
    ("baselineAbiSha256", "c" * 64), ("workspaceId", "d" * 32), ("workspacePath", "other")])
def test_different_original_source_profile_or_owner_never_borrows_evidence(tmp_path, field, value):
    project, status, old, artifact = fixture(tmp_path)
    old[field] = value
    result = publication.preserve_historical_artifacts(project, status)
    assert result["compilerBinding"] is None
    assert result["assemblyDisassemblyArtifacts"] == []
    assert "historicalPublicationEvidence" not in result


@pytest.mark.parametrize("field,value", [("rawSha256", "c" * 64), ("sourceRevision", 3),
                                       ("targetProfile", {"profile": "ps_4_0"})])
def test_changed_candidate_never_borrows_evidence(tmp_path, field, value):
    project, status, old, artifact = fixture(tmp_path)
    old["compiledCandidate"][field] = value
    assert publication.preserve_historical_artifacts(project, status)["compilerBinding"] is None


@pytest.mark.parametrize("damage", ["compiler", "assembly", "conflicting-current"])
def test_existing_archived_evidence_mismatch_is_rejected(tmp_path, damage):
    project, status, old, artifact = fixture(tmp_path)
    if damage == "compiler":
        old["compilerBinding"]["bindingSha256"] = "f" * 64
    elif damage == "assembly":
        artifact.write_bytes(b"corrupt")
    else:
        status["compilerBinding"] = _compiler_binding(status["targetProfile"], flag="-O0")
    with pytest.raises(RuntimeError):
        publication.preserve_historical_artifacts(project, status)
