"""Source review precedes final effects; diagnostic checkpoints retain a draft."""

import hashlib
import json
from pathlib import Path
from unittest import mock

import pytest

from src import server
from src.shader_organization_review import CHECKS, make_review, source_snapshot
import test_shader_organization as harness
from test_shader_self_review_tool import scope_assessment


@pytest.fixture
def draft(monkeypatch):
    work = harness.ShaderOrganizationTests(methodName="runTest")
    work.setUp()
    opened = work._begin()
    monkeypatch.setattr(server, "_shader_organization_manager", lambda: work.manager)
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: work.service)
    monkeypatch.setattr(server, "_shader_cbuffer_display_observation", lambda status: {})
    # These unit tests exercise review/compile sequencing with a standalone
    # workspace. Project registration has separate delivery integration tests.
    monkeypatch.setattr(server, "_register_active_project_workspace_receipt",
                        lambda *args, **kwargs: {"ok": True})
    try:
        yield work, opened
    finally:
        work.tearDown()
        work.doCleanups()


def assess(work, opened, verdict="complete"):
    snapshot = source_snapshot(str(work.root), opened["organizationId"])
    from test_shader_semantic_overview_review import prepared_fixture
    review = make_review(
        snapshot, snapshot["sourceSha256"], verdict,
        {key: "The entire MainPS calculation and its unchanged output were inspected" for key in CHECKS},
        [] if verdict == "complete" else ["Remaining branch calculations need grouping"],
        [] if verdict == "complete" else ["Group the remaining calculations in the working copy"], "",
        "Existing source already satisfies the requested scope" if not snapshot["changed"] else "",
        scope_assessment(snapshot),
        prepared_overview=prepared_fixture(snapshot, scope_assessment(snapshot)) if verdict == "complete" else None,
        require_overview=True,
    )
    Path(snapshot["reviewPath"]).write_text(json.dumps(review), encoding="utf-8")


@pytest.mark.parametrize("assessment", ["missing", "needs_work", "stale"])
@pytest.mark.parametrize("strict_validation", [False, True])
def test_unready_final_has_no_physical_effect_and_retains_same_draft(draft, assessment, strict_validation):
    work, opened = draft
    proposal = Path(opened["proposalSourcePath"])
    if assessment != "missing":
        assess(work, opened, "needs_work" if assessment == "needs_work" else "complete")
    if assessment == "stale":
        proposal.write_bytes(b"// later change\n" + work.original)
    with mock.patch.object(work.service, "compile", side_effect=AssertionError("no premature compile")), \
         mock.patch.object(server, "apply_shader_workspace_shared_resource", side_effect=AssertionError("no premature apply")):
        result = json.loads(server.finish_shader_workspace_organization(
            str(work.root), opened["organizationId"], validate_for_apply=strict_validation, apply_shared_resource=True,
            source_review_policy="strict",
        ))
    assert result["state"] == "source-review-pending" and result["recoverable"]
    assert not result["applied"] and result["draftPreserved"]
    assert work.manager.begin(str(work.root))["organizationId"] == opened["organizationId"]
    assert work.service.source_path.read_bytes() == work.original


def test_complete_source_review_allows_final_validation_without_prior_compile(draft):
    work, opened = draft
    proposal = Path(opened["proposalSourcePath"])
    proposal.write_bytes(b"// final readable source\n" + work.original)
    assess(work, opened)
    with mock.patch.object(work.service, "compile", wraps=work.service.compile) as compile_source:
        result = json.loads(server.finish_shader_workspace_organization(
            str(work.root), opened["organizationId"], validate_for_apply=True,
        ))
    assert result["ok"] and result["wholeSourceValidated"] and not result["draftPreserved"], json.dumps(result)
    # One baseline and one final candidate, no intermediate candidates.
    assert compile_source.call_count == 2
    assert work.service.source_path.read_bytes() == proposal.read_bytes()
    assert work.manager.validate_publication_gate(str(work.root))["finalSourceSha256"] == hashlib.sha256(proposal.read_bytes()).hexdigest()


def test_visual_code_requires_current_reviewed_shader(draft, monkeypatch, tmp_path):
    from src import visual_replies

    work, opened = draft
    monkeypatch.setattr(visual_replies, "root_path", lambda: tmp_path / "visuals")
    monkeypatch.setattr(server, "get_current_renderdoc_context",
                        lambda: json.dumps({"capture": {"captureSessionId": "capture-test"}}))
    report = mock.Mock()
    monkeypatch.setattr(server, "current_task_report_store", lambda: report)
    report.enabled = False
    report.register_reply_visual.return_value = True

    def publish_code():
        digest = hashlib.sha256(work.service.source_path.read_bytes()).hexdigest()
        block = dict(workspacePath=str(work.root), expectedSourceSha256=digest, startLine=1, endLine=2)
        cards = [
            server.VisualReplyCard(type="code", title="Key calculation", **block),
            server.VisualReplyCard(type="diagram", title="Flow", nodes=[
                {"id": "calculation", "text": "Calculation", "codeBlocks": [block]},
            ]),
        ]
        return json.loads(server.publish_visual_reply("Shader explanation", cards))

    assert "Organize and validate this Shader" in publish_code()["error"]
    report.register_reply_visual.assert_not_called()
    proposal = Path(opened["proposalSourcePath"])
    proposal.write_bytes(b"// Reviewed calculation\n" + work.original)
    assess(work, opened)
    final = json.loads(server.finish_shader_workspace_organization(
        str(work.root), opened["organizationId"], validate_for_apply=True, source_review_policy="strict",
    ))
    assert final["wholeSourceValidated"], final
    published = publish_code()
    assert published.get("publishedToChat"), published
    manifest = json.loads(Path(published["path"]).read_text(encoding="utf-8"))
    finalized_sha = work.manager.validate_publication_gate(str(work.root))["finalSourceSha256"]
    for excerpt in (manifest["cards"][0]["code"], manifest["cards"][1]["nodes"][0]["codeBlocks"][0]):
        assert "Reviewed calculation" in excerpt["text"]
        assert excerpt["sha256"] == finalized_sha
    work.service.source_path.write_bytes(b"// Later unvalidated change\n" + proposal.read_bytes())
    assert "Organize and validate this Shader" in publish_code()["error"]
    assert report.register_reply_visual.call_count == 1


@pytest.mark.parametrize("flag", ["validate_for_apply", "apply_shared_resource", "start_live_edit"])
def test_checkpoint_rejects_physical_effect_flags(draft, flag):
    work, opened = draft
    proposal = Path(opened["proposalSourcePath"])
    proposal.write_bytes(b"// intermediate draft\n" + work.original)
    assess(work, opened, "needs_work")
    with mock.patch.object(work.service, "compile", side_effect=AssertionError("no checkpoint compile")), \
         mock.patch.object(server, "apply_shader_workspace_shared_resource", side_effect=AssertionError("no checkpoint apply")):
        result = json.loads(server.finish_shader_workspace_organization(
            str(work.root), opened["organizationId"], publication_mode="checkpoint", **{flag: True},
        ))
    assert "checkpoint cannot" in result["error"]
    assert work.service.source_path.read_bytes() == work.original
    assert work.manager.begin(str(work.root))["organizationId"] == opened["organizationId"]


def test_proposal_checkpoint_preserves_canonical_and_same_editable_session(draft):
    work, opened = draft
    proposal = Path(opened["proposalSourcePath"])
    proposed = b"// intermediate draft\n" + work.original
    proposal.write_bytes(proposed)
    with mock.patch.object(work.service, "compile", side_effect=AssertionError("no checkpoint compile")), \
         mock.patch.object(server, "ipc") as bridge:
        result = json.loads(server.finish_shader_workspace_organization(
            str(work.root), opened["organizationId"], publication_mode="checkpoint", show_in_workspace=True,
        ))
    assert result["ok"] and result["draftPreserved"] and not result["wholeSourceValidated"]
    assert result["state"] == "proposal-checkpoint" and not result["promoted"]
    assert result["sourceSha256"] == hashlib.sha256(proposed).hexdigest()
    assert work.service.source_path.read_bytes() == work.original
    bridge.call.assert_not_called()
    assert work.manager.begin(str(work.root))["organizationId"] == opened["organizationId"]
    work.manager.authorize_edit(str(work.root), opened["organizationId"])
    proposal.write_bytes(b"// repaired final draft\n" + work.original)
    assess(work, opened)
    final = json.loads(server.finish_shader_workspace_organization(
        str(work.root), opened["organizationId"], validate_for_apply=True,
    ))
    assert final["ok"] and not final["draftPreserved"]


def test_open_draft_annotations_do_not_lock_physical_tools_or_promote_proposal(draft):
    work, opened = draft
    proposal = Path(opened["proposalSourcePath"])
    proposal.write_bytes(b"// still being organized\n" + work.original)
    assert server._require_organization_source_review(str(work.root)) == opened["organizationId"]
    assert work.service.source_path.read_bytes() == work.original
    assert proposal.read_bytes() != work.service.source_path.read_bytes()


def test_default_finish_reports_missing_review_and_runs_physical_validation(draft):
    work, opened = draft
    proposal = Path(opened["proposalSourcePath"])
    proposal.write_bytes(b"// final source with assessment pending\n" + work.original)
    result = json.loads(server.finish_shader_workspace_organization(
        str(work.root), opened["organizationId"], validate_for_apply=True))
    assert result["ok"] and result["wholeSourceValidated"], result
    assert result["sourceReviewObservations"]["diagnosticsOnly"] is True
    assert result["sourceReviewObservations"]["assessment"]["ready"] is False
    assert result["sourceReviewObservations"]["assessment"]


def test_direct_compile_accepts_reviewed_saved_proposal(draft):
    work, opened = draft
    Path(opened["proposalSourcePath"]).write_bytes(b"// organized\n" + work.original)
    assess(work, opened)
    saved = json.loads(server.finish_shader_workspace_organization(str(work.root), opened["organizationId"], validate_for_apply=False))
    assert saved["ok"]
    with mock.patch.object(work.service, "compile", wraps=work.service.compile) as compile_source:
        result = json.loads(server.compile_shader_workspace(str(work.root)))
    assert "error" not in result and compile_source.call_count == 1


def test_failed_final_can_repair_same_proposal_and_finish(draft):
    work, opened = draft
    proposal = Path(opened["proposalSourcePath"])
    proposal.write_bytes(b"// candidate with failure\n" + work.original)
    assess(work, opened)
    original_compile = work.service.compile
    def compile_with_failure(path):
        if work.service.source_path.read_bytes().startswith(b"// candidate with failure"):
            raise harness.ShaderOrganizationError("compiler reports a concrete candidate error")
        return original_compile(path)
    with mock.patch.object(work.service, "compile", side_effect=compile_with_failure):
        failed = json.loads(server.finish_shader_workspace_organization(
            str(work.root), opened["organizationId"], validate_for_apply=True,
        ))
    assert not failed["ok"] and failed["draftPreserved"] and failed["previousSourceRestored"]
    assert work.manager.begin(str(work.root))["organizationId"] == opened["organizationId"]
    proposal.write_bytes(b"// repaired final\n" + work.original)
    assess(work, opened)
    final = json.loads(server.finish_shader_workspace_organization(
        str(work.root), opened["organizationId"], validate_for_apply=True,
    ))
    assert final["ok"] and final["wholeSourceValidated"]


@pytest.mark.parametrize("strict_validation", [False, True])
def test_change_between_review_admission_and_source_read_never_compiles(draft, monkeypatch, strict_validation):
    work, opened = draft
    proposal = Path(opened["proposalSourcePath"])
    proposal.write_bytes(b"// reviewed source\n" + work.original)
    assess(work, opened)
    def raced_manager():
        proposal.write_bytes(b"// new unreviewed source\n" + work.original)
        return work.manager
    monkeypatch.setattr(server, "_shader_organization_manager", raced_manager)
    with mock.patch.object(work.service, "compile", side_effect=AssertionError("no stale source compile")):
        result = json.loads(server.finish_shader_workspace_organization(
            str(work.root), opened["organizationId"],
            validate_for_apply=strict_validation, apply_shared_resource=True,
        ))
    assert "changed after source self-review" in result["error"]
    assert work.service.source_path.read_bytes() == work.original
    assert work.manager.begin(str(work.root))["organizationId"] == opened["organizationId"]


def test_draft_endpoints_use_only_local_source_reader(draft, monkeypatch):
    work, opened = draft
    strict_status = work.service.status
    def edit_status(path):
        result = strict_status(path)
        result["schema"] = "renderdoc-ultra-shader-source-edit-status/v1"
        result["validationScope"] = "source-edit-only"
        result["latestCompiled"] = None
        return result
    monkeypatch.setattr(work.service, "source_edit_status", edit_status, raising=False)
    monkeypatch.setattr(work.service, "status", mock.Mock(side_effect=AssertionError("no candidate ABI or toolchain validation")))
    monkeypatch.setattr(work.manager, "_cbuffer_alias_identity", mock.Mock(side_effect=AssertionError("no live alias fetch")))
    resumed = work.manager.begin(str(work.root))
    assert resumed["organizationId"] == opened["organizationId"]
    contents = "// complete working copy\n" + work.original.decode("utf-8")
    written = json.loads(server.write_shader_workspace_organization_file(
        str(work.root), opened["organizationId"], "proposal", content=contents,
        expected_proposal_sha256=opened["proposalSourceSha256"],
    ))
    assert "error" not in written
    checkpoint = json.loads(server.finish_shader_workspace_organization(
        str(work.root), opened["organizationId"], publication_mode="checkpoint"))
    assert checkpoint["state"] == "proposal-checkpoint"
    assert work.service.source_path.read_bytes() == work.original
    saved = json.loads(server.finish_shader_workspace_organization(str(work.root), opened["organizationId"], validate_for_apply=False))
    assert saved["ok"] and saved["validationDeferred"]
    assert work.service.source_path.read_text(encoding="utf-8") == contents
    work.service.status.assert_not_called()
    work.manager._cbuffer_alias_identity.assert_not_called()


def test_new_begin_only_fetches_aliases_when_live_evidence_was_requested(draft):
    work, opened = draft
    callback = mock.Mock(side_effect=RuntimeError("optional live evidence unavailable"))
    work.manager._cbuffer_alias_identity = callback
    work.manager.begin(str(work.root), restart=True)
    callback.assert_not_called()
    result = work.manager.begin(str(work.root), {"includeLiveBindingEvidence": True}, restart=True)
    callback.assert_called_once()
    assert result["semanticContext"]["cbufferAliasEvidenceUnavailable"]["sourceOrganizationBlocked"] is False


def test_real_local_workspace_editing_survives_unusable_candidate(monkeypatch):
    import test_shader_workspace as workspace_harness
    from src.shader_workspace.organization import ShaderOrganizationManager
    case = workspace_harness.ShaderWorkspaceTests(methodName="runTest")
    case.setUp()
    try:
        service, root, _ = case._create("organization-text-only")
        service.decompile(root)
        manifest_path = root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["latestCompiled"] = {"path": "missing-candidate.dxbc"}
        manifest["tools"]["compiler"]["executableSha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        manager = ShaderOrganizationManager(lambda: service, mock.Mock(side_effect=AssertionError("no draft ABI")))
        monkeypatch.setattr(server, "_shader_workspace_service", lambda: service)
        monkeypatch.setattr(server, "_shader_organization_manager", lambda: manager)
        with mock.patch.object(service, "status", side_effect=AssertionError("no candidate validation")), \
             mock.patch.object(service, "compile", side_effect=AssertionError("no draft compile")), \
             mock.patch.object(server.ipc, "call", side_effect=AssertionError("no replay ownership")):
            opened = json.loads(server.begin_shader_workspace_organization(str(root)))
            assert "error" not in opened
            canonical = root / "source" / "shader.hlsl"
            original = canonical.read_bytes()
            contents = "// continued local text editing\n" + original.decode("utf-8")
            written = json.loads(server.write_shader_workspace_organization_file(
                str(root), opened["organizationId"], "proposal", content=contents))
            assert "error" not in written
            checkpoint = json.loads(server.finish_shader_workspace_organization(
                str(root), opened["organizationId"], publication_mode="checkpoint"))
            assert checkpoint["ok"] and canonical.read_bytes() == original
            saved = json.loads(server.finish_shader_workspace_organization(str(root), opened["organizationId"], validate_for_apply=False))
            assert saved["ok"] and canonical.read_bytes() == contents.encode("utf-8")
    finally:
        case.tearDown()
        case.doCleanups()


def test_resume_restores_current_draft_pointer(draft):
    from src.shader_workspace.organization import active_draft_id
    work, opened = draft
    gate = work.root / "ai" / "organize-code-gate.json"
    gate.unlink()
    assert active_draft_id(work.root) == ""
    resumed = work.manager.begin(str(work.root))
    assert resumed["organizationId"] == opened["organizationId"]
    assert active_draft_id(work.root) == opened["organizationId"]
