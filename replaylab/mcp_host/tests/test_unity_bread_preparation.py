"""Prefer existing Bread proof and retain exact drafts for target-side recovery."""
import json
from types import SimpleNamespace

from mcp.server.fastmcp import FastMCP
import pytest

from src import shader_organization_review, shader_semantic_delivery
from src.unity_shader_sources import collect_organized_sources, verify_source_files
from src.unity_restoration_requirements import annotate_transferred_shader_reviews, _summary
from tests.test_unity_restoration import write


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    sha = write(root / "source/shader.hlsl", "float4 Main() : SV_Target { return 1; }")
    status = {"workspaceId": "w", "stage": "fragment", "capture": {"api": "Vulkan"}, "entryPoint": "Main",
        "baseline": {"rawSha256": "a" * 64}, "source": {"present": True,
            "path": str(root / "source/shader.hlsl"), "currentSha256": sha, "format": "hlsl", "dependenciesDigest": ""},
        "latestCompiled": {"sourceSha256": sha, "rawSha256": "b" * 64},
        "sourceDependenciesCurrent": True, "abiCompatible": True}
    gate = {"finalSourceSha256": sha, "organizationId": "c" * 32, "wholeSourceValidated": True,
        "organizedCompiledAndAbiValidated": True, "finalCandidateRawSha256": "b" * 64,
        "publicationContract": "validated-source", "equivalenceLevel": "observational-pass"}
    monkeypatch.setattr(shader_organization_review, "publication_review", lambda *args: {"ready": True, "sourceSha256": sha})
    monkeypatch.setattr(shader_semantic_delivery, "delivery_state", lambda *args, **kwargs: {"required": False})
    return root, sha, status, gate


def test_default_read_reuses_proof_without_compiling_or_downgrading(workspace):
    root, sha, status, gate = workspace
    write(root / "ai/organize-code-gate.json", gate)
    reads = []
    def loader(path):
        reads.append(path)
        return status
    result = collect_organized_sources([str(root)], loader, require_replay_validation=False)
    row = result["sources"][0]
    assert result["ready"] and reads == [str(root)]
    assert row["validation"]["mode"] == "validated-source" and row["validation"]["replayValidated"] is True
    assert set(row["validation"]["passedChecks"]) == {"compilation", "abi", "organization-review", "equivalence"}
    assert row["sourcePreparation"]["status"] == "prepared"
    assert verify_source_files([row]) == []


def test_incomplete_output_proof_keeps_compile_abi_and_allows_unity_preparation(workspace):
    root, sha, status, gate = workspace
    result = collect_organized_sources([str(root)], lambda _: status, require_replay_validation=False)
    row = result["sources"][0]
    assert result["ready"] and row["sourcePreparation"]["preferredLocation"] == "bread"
    assert row["sourcePreparation"]["fallbackLocation"] == "unity"
    assert row["sourcePreparation"]["missingChecks"]
    assert row["validation"]["replayValidated"] is False
    assert set(row["validation"]["passedChecks"]) == {"compilation", "abi"}
    status["sourceDependenciesCurrent"] = False
    changed = collect_organized_sources([str(root)], lambda _: status, require_replay_validation=False)
    assert changed["sources"][0]["validation"]["passedChecks"] == []


def test_display_only_failure_does_not_discard_validated_source_or_recompile(workspace, monkeypatch):
    root, _, status, gate = workspace
    write(root / "ai/organize-code-gate.json", gate)
    monkeypatch.setattr(shader_semantic_delivery, "delivery_state", lambda *args, **kwargs: {
        "required": True, "status": "pending", "reason": "publication-display-identity-refresh-required"})
    result = collect_organized_sources([str(root)], lambda _: status, require_replay_validation=False)
    row = result["sources"][0]
    assert result["ready"] and row["validation"]["replayValidated"] is True
    assert row["validation"]["mode"] == "validated-source"
    assert "equivalence" in row["validation"]["passedChecks"]
    assert row["sourcePreparation"]["status"] == "prepared" and row["sourcePreparation"]["displayGaps"]
    assert not row["sourcePreparation"]["missingChecks"]
    assert verify_source_files([row]) == []


def draft(workspace):
    root, _, status, _ = workspace
    organization_id = "d" * 32
    path = "ai/organization-sessions/" + organization_id + "/organized-proposal.hlsl"
    sha = write(root / path, "float4 EvaluateColor() { return 1; } float4 Main() : SV_Target { return EvaluateColor(); }")
    session = {"organizationId": organization_id, "workspaceId": status["workspaceId"],
        "stage": status["stage"], "entryPoint": status["entryPoint"], "originalRawSha256": status["baseline"]["rawSha256"],
        "proposalSourcePath": path, "proposalSourceSha256": sha, "sourceFormat": "hlsl"}
    write(root / ("ai/organization-sessions/" + organization_id + "/session.json"), session)
    return organization_id, path, sha, session


def test_failed_proposal_can_transfer_without_promoting_or_forging_validation(workspace):
    root, original, status, _ = workspace
    organization_id, relative, sha, _ = draft(workspace)
    result = collect_organized_sources([str(root)], lambda _: status, require_replay_validation=False,
        organization_ids={str(root): organization_id})
    row = result["sources"][0]
    assert result["ready"] and row["sourceSha256"] == sha
    assert row["sourceSelection"] == {"kind": "organization-draft", "organizationId": organization_id, "proposalReceiptCurrent": True}
    assert row["validation"]["replayValidated"] is False
    assert row["validation"]["passedChecks"] == []
    assert verify_source_files([row]) == []
    assert (root / "source/shader.hlsl").read_text() == "float4 Main() : SV_Target { return 1; }"
    write(root / relative, "changed proposal")
    assert verify_source_files([row])
    with pytest.raises(ValueError, match="proposal changed"):
        collect_organized_sources([str(root)], lambda _: status, require_replay_validation=False,
            organization_ids={str(root): organization_id})
    edited_sha = write(root / relative, "explicitly observed edited draft")
    edited = collect_organized_sources([str(root)], lambda _: status, require_replay_validation=False,
        organization_ids={str(root): organization_id}, draft_sha256={str(root): edited_sha})
    assert edited["ready"] and edited["sources"][0]["sourceSha256"] == edited_sha
    assert edited["sources"][0]["sourceSelection"]["proposalReceiptCurrent"] is False


@pytest.mark.parametrize("wrong", ["stage", "entryPoint", "originalRawSha256", "workspaceId"])
def test_draft_selection_cannot_retarget_another_captured_program(workspace, wrong):
    root, _, status, _ = workspace
    organization_id, _, _, session = draft(workspace)
    session[wrong] = "vertex" if wrong == "stage" else "wrong"
    write(root / ("ai/organization-sessions/" + organization_id + "/session.json"), session)
    with pytest.raises(ValueError, match="different Shader"):
        collect_organized_sources([str(root)], lambda _: status, require_replay_validation=False,
            organization_ids={str(root): organization_id})


def test_exact_bound_draft_and_advisory_transfer_do_not_claim_other_source_proof(workspace):
    root, _, status, _ = workspace
    organization_id, relative, sha, _ = draft(workspace)
    sources = collect_organized_sources([str(root)], lambda _: status, require_replay_validation=False,
        organization_ids={str(root): organization_id})["sources"]
    summary = _summary({"restorationId": "e" * 32, "binding": {"operationId": "f" * 32}, "sources": sources})
    state = {"unityRestorations": {summary["restorationId"]: summary}}
    row = {"workspacePath": str(root), "organizationId": organization_id, "sourceSha256": sha}
    reviews = {"targets": [row, {**row, "sourceSha256": "0" * 64}, {**row, "organizationId": "0" * 32}]}
    annotated = annotate_transferred_shader_reviews(reviews, state)["targets"]
    assert annotated[0]["unityTransfer"]["originalReplayComplete"] is False
    assert "diagnosticsOnly" not in annotated[0]["unityTransfer"]
    for advisory in annotated[1:]:
        assert advisory["unityTransfer"]["diagnosticsOnly"] is True
        assert advisory["unityTransfer"]["originalReplayComplete"] is False
    assert "unityTransfer" not in row  # Original source review remains unchanged.
    write(root / relative, "changed after transfer")
    changed=annotate_transferred_shader_reviews(reviews,state)["targets"][0]
    assert changed["unityTransfer"]["diagnosticsOnly"] is True
    assert changed["unityTransfer"]["originalReplayComplete"] is False


@pytest.mark.asyncio
async def test_registered_source_read_works_without_constructing_an_executable_registry(workspace, monkeypatch):
    from src import unity_restoration_tools
    root, _, status, gate = workspace
    write(root / "ai/organize-code-gate.json", gate)
    monkeypatch.setenv("BREAD_UNITY_RESTORATION_ROOT", str(root.parent / "restorations"))
    def forbidden(*args, **kwargs):
        raise AssertionError("Reference evidence must not initialize compiler tools")
    monkeypatch.setattr(unity_restoration_tools.ShaderWorkspaceArtifactReader, "status", lambda self, path: status)
    host = SimpleNamespace(_shader_workspace_service=forbidden, _ok_compact=json.dumps,
        _err=lambda error: json.dumps({"error": str(error)}),
        ipc=SimpleNamespace(bound_instance_state=lambda: None),
        _shader_organization_cbuffer_alias_identity=forbidden)
    mcp = FastMCP("Bread source preparation")
    unity_restoration_tools.register_tools(mcp, host)
    result = await mcp.call_tool("get_unity_shader_transfer_sources", {"workspace_paths": [str(root)]})
    content = result[0] if isinstance(result, tuple) else result
    parsed = json.loads("".join(item.text for item in content if item.type == "text"))
    assert parsed["ready"] and parsed["sources"][0]["validation"]["mode"] == "validated-source"
