"""Self-review is an Agent assertion tied to bytes, never a Host quality grade."""
import hashlib
import json

import pytest

from replaylab_agent.agent_report import TaskReportStore
from src.shader_organization_review import CHECKS, make_review, source_snapshot


def workspace(tmp_path, organization="a" * 32):
    root = tmp_path / "shader"
    session_dir = root / "ai" / "organization-sessions" / organization
    session_dir.mkdir(parents=True, exist_ok=True)
    source = root / "source.hlsl"
    source.write_bytes(b"float4 main() { return 0; }")
    proposal = session_dir / "proposal.hlsl"
    proposal.write_bytes(source.read_bytes())
    session = {
        "organizationId": organization, "state": "drafting",
        "sourcePath": "source.hlsl", "proposalSourcePath": str(proposal.relative_to(root)),
        "baselineSourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    }
    (session_dir / "session.json").write_text(json.dumps(session), encoding="utf-8")
    return root, organization, source, proposal


def reviewed(root, organization, verdict="complete", **kwargs):
    snapshot = source_snapshot(str(root), organization)
    return make_review(snapshot, snapshot["sourceSha256"], verdict,
                       {key: "MainPS uses material data and named lighting functions" for key in CHECKS},
                       kwargs.get("findings", []), kwargs.get("next_actions", []), kwargs.get("blocker", ""),
                       kwargs.get("no_change_reason", ""))


def observe_current_source(store, root, proposal):
    raw = proposal.read_bytes()
    store.record_result("read_shader_workspace_file", {
        "mode": "complete", "workspacePath": str(root), "text": raw.decode("utf-8"),
        "sizeBytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
        "coverage": {"byteOffset": 0, "returnedBytes": len(raw)}})


def test_review_survives_restart_save_and_invalidates_on_next_real_edit(tmp_path):
    root, organization, source, proposal = workspace(tmp_path)
    store = TaskReportStore("review-task", tmp_path / "events")
    store.record_result("begin_shader_workspace_organization", {
        "workspacePath": str(root), "organizationId": organization,
    })
    assert store.shader_organization_review_state()["targets"][0]["changed"] is False
    proposal.write_bytes(b"float4 MainPS() { return EvaluateLighting(); }")
    observe_current_source(store, root, proposal)
    receipt = reviewed(root, organization)
    store.record_result("record_shader_organization_review", receipt)
    restored = TaskReportStore("review-task", tmp_path / "events")
    row = restored.shader_organization_review_state()["targets"][0]
    assert row["fresh"] and row["changed"] and not row["saved"]
    source.write_bytes(proposal.read_bytes())
    row = restored.shader_organization_review_state()["targets"][0]
    assert row["fresh"] and row["saved"]
    proposal.write_bytes(proposal.read_bytes() + b"\n// next draft")
    assert not restored.shader_organization_review_state()["targets"][0]["fresh"]


def test_canonical_concurrent_edit_invalidates_proposal_review(tmp_path):
    root, organization, source, proposal = workspace(tmp_path)
    proposal.write_bytes(b"organized source")
    store = TaskReportStore("concurrent", tmp_path / "events")
    store.record_result("record_shader_organization_review", reviewed(root, organization))
    source.write_bytes(b"user's concurrent source")
    row = store.shader_organization_review_state()["targets"][0]
    assert row["sourceConflict"] and not row["fresh"]


def test_complete_rejects_no_edit_stale_hash_and_unresolved_findings(tmp_path):
    root, organization, source, proposal = workspace(tmp_path)
    with pytest.raises(ValueError, match="No source organization change"):
        reviewed(root, organization)
    proposal.write_bytes(b"organized source")
    with pytest.raises(ValueError, match="unresolved"):
        reviewed(root, organization, findings=["MainPS still has opaque scalar copies"])
    snapshot = source_snapshot(str(root), organization)
    with pytest.raises(ValueError, match="changed since"):
        make_review(snapshot, "0" * 64, "complete", {}, [], [], "")


def test_needs_work_and_blocked_have_actionable_reasons(tmp_path):
    root, organization, source, proposal = workspace(tmp_path)
    with pytest.raises(ValueError, match="concrete findings"):
        reviewed(root, organization, "needs_work")
    with pytest.raises(ValueError, match="actual reason"):
        reviewed(root, organization, "blocked")
    assert reviewed(root, organization, "needs_work", findings=["frame.r0 obscures normal"],
                    next_actions=["replace scalar aliases with normalWS"])['verdict'] == "needs_work"
    assert reviewed(root, organization, "blocked", blocker="Owned filesystem is unavailable")['blocker']


def test_unchanged_source_can_be_reviewed_but_cannot_claim_a_new_rewrite(tmp_path):
    root, organization, source, proposal = workspace(tmp_path)
    store = TaskReportStore("already-readable", tmp_path / "events")
    store.record_result("begin_shader_workspace_organization", {
        "workspacePath": str(root), "organizationId": organization,
    })
    receipt = reviewed(root, organization, no_change_reason="All requested paths already use clear material/lighting helpers")
    observe_current_source(store, root, proposal)
    store.record_result("record_shader_organization_review", receipt)
    row = store.shader_organization_review_state()["targets"][0]
    assert row["fresh"] and row["saved"] and not row["changed"]


def test_each_workspace_required_but_restart_supersedes_old_draft(tmp_path):
    first, org1, _, _ = workspace(tmp_path / "one")
    second, org2, _, _ = workspace(tmp_path / "two")
    store = TaskReportStore("multiple", tmp_path / "events")
    for root, organization in ((first, org1), (second, org2)):
        store.record_result("begin_shader_workspace_organization", {
            "workspacePath": str(root), "organizationId": organization,
        })
    _, restarted, _, _ = workspace(tmp_path / "one", "b" * 32)
    store.record_result("begin_shader_workspace_organization", {
        "workspacePath": str(first), "organizationId": restarted,
    })
    rows = store.shader_organization_review_state()["targets"]
    assert len(rows) == 2 and {row["organizationId"] for row in rows} == {org2, restarted}


def test_explanation_edits_and_failed_tools_do_not_require_shader_review(tmp_path):
    store = TaskReportStore("explanation", tmp_path / "events")
    payload = {"workspacePath": str(tmp_path), "organizationId": "a" * 32}
    store.record_result("write_shader_workspace_organization_file", payload, arguments={"kind": "analysis"})
    store.record_result("begin_shader_workspace_organization", {**payload, "ok": False})
    store.record_result("restructure_shader_workspace_source", payload, arguments={"operation": "list"})
    assert not store.shader_organization_review_state()["targets"]


def test_reopened_previous_result_is_not_a_new_organization_change(tmp_path):
    root, organization, source, proposal = workspace(tmp_path)
    proposal.write_bytes(b"previous task's organized source")
    store = TaskReportStore("reopened", tmp_path / "events")
    store.record_result("begin_shader_workspace_organization", {
        "workspacePath": str(root), "organizationId": organization,
        "proposalSourceSha256": hashlib.sha256(proposal.read_bytes()).hexdigest(),
    })
    row = store.shader_organization_review_state()["targets"][0]
    assert row["overallChanged"] and not row["changed"]
    proposal.write_bytes(b"this task's actual source improvement")
    assert store.shader_organization_review_state()["targets"][0]["changed"]


def test_first_mutation_uses_cas_preimage_not_historical_baseline(tmp_path):
    root, organization, source, proposal = workspace(tmp_path)
    old_proposal_sha = hashlib.sha256(proposal.read_bytes()).hexdigest()
    proposal.write_bytes(b"new current task proposal")
    store = TaskReportStore("first-write", tmp_path / "events")
    store.record_result("write_shader_workspace_organization_file", {
        "workspacePath": str(root), "organizationId": organization, "kind": "proposal",
    }, arguments={"expected_proposal_sha256": old_proposal_sha})
    assert store.shader_organization_review_state()["targets"][0]["changed"]
    other = TaskReportStore("finish-only", tmp_path / "events")
    other.record_result("finish_shader_workspace_organization", {
        "workspacePath": str(root), "organizationId": organization,
    })
    row = other.shader_organization_review_state()["targets"][0]
    assert row["overallChanged"] and not row["changed"]


def test_redirected_session_source_cannot_make_a_review(tmp_path):
    root, organization, source, proposal = workspace(tmp_path)
    session_path = proposal.parent / "session.json"
    session = json.loads(session_path.read_text())
    session["proposalSourcePath"] = str(tmp_path / "outside.hlsl")
    (tmp_path / "outside.hlsl").write_text("outside")
    session_path.write_text(json.dumps(session))
    with pytest.raises(ValueError):
        source_snapshot(str(root), organization)


def test_incomplete_persisted_receipt_cannot_bypass_review_shape(tmp_path):
    root, organization, source, proposal = workspace(tmp_path)
    store = TaskReportStore("bad-receipt", tmp_path / "events")
    store.record_result("begin_shader_workspace_organization", {
        "workspacePath": str(root), "organizationId": organization,
    })
    proposal.write_bytes(b"organized source")
    receipt = reviewed(root, organization)
    receipt.pop("checks")
    receipt["findings"] = ["still unreadable"]
    store.record_result("record_shader_organization_review", receipt)
    row = store.shader_organization_review_state()["targets"][0]
    assert row["changed"] and not row["fresh"]
