"""Draft editing and final shader publication boundaries."""

from __future__ import annotations

from pathlib import Path
import hashlib
import json
from unittest import mock

import pytest

import test_shader_organization as harness
from src import server
from src.shader_workspace.source_language import organization_capabilities


def test_capabilities_describe_operations_without_a_fixed_workflow() -> None:
    caps = organization_capabilities("hlsl")
    assert caps["rename"] is True
    assert caps["structureExtraction"] is True
    assert caps["boundedRewrite"] is True
    assert caps["finalCompileAbiAvailable"] is True
    assert "hostIde" not in caps
    assert "preferredExtractLines" not in caps


@pytest.fixture
def work():
    case = harness.ShaderOrganizationTests(methodName="runTest")
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()
        case.doCleanups()


def test_begin_only_snapshots_source_without_compiling_or_requiring_templates(work) -> None:
    revision = work.service.revision
    with mock.patch.object(
        work.service, "compile", side_effect=AssertionError("begin must not compile")
    ), mock.patch.object(
        work.manager, "_validate_candidate", side_effect=AssertionError("begin must not validate ABI")
    ):
        opened = work.manager.begin(str(work.root), {"eid": 11384})
    assert opened["baselineCompiledAndAbiValidated"] is False
    assert opened["semanticContext"]["eid"] == 11384
    assert Path(opened["proposalSourcePath"]).read_bytes() == work.original
    assert opened["proposalSourceSha256"] == opened["baselineSourceSha256"]
    assert "analysisTemplate" not in opened
    assert "templateContract" not in opened
    assert "requiredChecks" not in opened
    assert Path(opened["analysisPath"]).read_bytes() == b""
    assert Path(opened["flowchartPath"]).read_bytes() == b""
    assert json.loads(Path(opened["moduleDescriptionsPath"]).read_text())["modules"] == []
    assert work.service.revision == revision


def test_restructure_reports_static_draft_and_defers_compile_abi_to_final(work) -> None:
    opened = work._begin()
    with mock.patch.object(
        work.service, "compile", side_effect=AssertionError("draft edit must not compile")
    ), mock.patch.object(
        work.manager, "_validate_candidate", side_effect=AssertionError("draft edit must not validate ABI")
    ):
        result = work.manager.restructure(
            str(work.root), opened["organizationId"], "rewrite",
            expected_proposal_sha256=opened["proposalSourceSha256"],
            name="commentOnlyBatch",
            edits=[{
                "startLine": 1,
                "endLine": 1,
                "replacement": "// clarified output\nfloat4 MainPS() : SV_Target { return 1.0; }",
            }],
        )
    assert result["accepted"] is True
    assert result["validationTier"] == "proposal-draft"
    assert result["completedChecks"] == ["bounded-source-static-check"]
    assert "deferredFinalChecks" not in result
    assert result["replayAbiChecked"] is False
    assert result["canonicalSourceUpdated"] is False
    assert Path(result["draftViewPath"]).read_text(encoding="utf-8").startswith(
        "// clarified output"
    )
    assert work.service.source_path.read_bytes() == work.original


def test_save_draft_syncs_current_source_without_compiling_or_applying(work) -> None:
    opened = work._begin()
    proposal = Path(opened["proposalSourcePath"])
    proposal.write_bytes(b"// readable draft\n" + work.original)
    revision = work.service.revision
    with mock.patch.object(
        work.service, "compile", side_effect=AssertionError("save must not compile")
    ), mock.patch.object(
        work.manager, "_validate_candidate", side_effect=AssertionError("save must not validate ABI")
    ):
        saved = work.manager.save_draft(str(work.root), opened["organizationId"])

    assert saved["ok"] is True
    assert saved["promoted"] is True
    assert saved["published"] is False
    assert saved["applied"] is False
    assert saved["wholeSourceValidated"] is False
    assert saved["safeToStartLiveEdit"] is False
    assert work.service.revision == revision
    assert work.service.source_path.read_bytes() == proposal.read_bytes()
    assert (Path(opened["sessionPath"]).parent / "baseline-source.hlsl").read_bytes() == work.original
    gate = json.loads(Path(saved["gatePath"]).read_text(encoding="utf-8"))
    assert gate["decision"] == "editable-draft"
    assert gate["finalBinding"]["complete"] is False
    with pytest.raises(harness.ShaderOrganizationError):
        work.manager.validate_publication_gate(str(work.root))
    assert work.manager.begin(str(work.root))["organizationId"] == opened["organizationId"]


def test_editor_source_save_needs_no_running_live_job_and_defers_compile(work):
    opened = work.manager.begin(str(work.root))
    expected = opened["proposalSourceSha256"]
    workspace_id = work.service.status(str(work.root))["workspaceId"]
    replacement = "// saved from editable preview\n" + work.original.decode("utf-8")
    with mock.patch.object(work.service, "compile", side_effect=AssertionError("no compile on draft save")):
        saved = work.manager.save_editor_source(str(work.root), workspace_id, expected, replacement)
        retry = work.manager.save_editor_source(str(work.root), workspace_id, expected, replacement)
    assert saved == retry
    assert saved["compileScheduled"] is False and saved["sourceOnly"] is True
    assert work.service.source_path.read_text(encoding="utf-8") == replacement
    assert Path(opened["proposalSourcePath"]).read_text(encoding="utf-8") == replacement
    gate = json.loads((work.root / "ai/organize-code-gate.json").read_text())
    assert gate["decision"] == "editable-draft"
    assert work.manager.begin(str(work.root))["organizationId"] == opened["organizationId"]


def test_editor_save_of_current_immutable_preview_updates_only_working_source(work):
    opened = work.manager.begin(str(work.root))
    proposal = Path(opened["proposalSourcePath"])
    preview = work.root / "ai" / "immutable-preview.hlsl"
    draft = b"// current preview\n" + work.original
    proposal.write_bytes(draft)
    preview.write_bytes(draft)
    work.manager.checkpoint(str(work.root), opened["organizationId"])
    expected = hashlib.sha256(draft).hexdigest()
    workspace_id = work.service.status(str(work.root))["workspaceId"]
    replacement = "// user's changes\n" + draft.decode("utf-8")
    with mock.patch.object(work.service, "compile", side_effect=AssertionError("no compile")):
        saved = work.manager.save_editor_source(
            str(work.root), workspace_id, expected, replacement)
    assert saved["sourceOnly"] is True and saved["compileScheduled"] is False
    assert preview.read_bytes() == draft
    assert work.service.source_path.read_text(encoding="utf-8") == replacement
    assert (Path(opened["sessionPath"]).parent / "baseline-source.hlsl").read_bytes() == work.original


def test_editor_source_save_without_compile_eligibility_is_draft_only(work):
    base_reader = work.service.status

    def source_status(path):
        status = base_reader(path)
        status["schema"] = "renderdoc-ultra-shader-source-edit-status/v1"
        status["source"]["compileEligible"] = False
        status["representation"]["compileEligible"] = False
        return status

    work.service.source_edit_status = source_status
    with mock.patch.object(work.service, "compile", side_effect=AssertionError("no compile")):
        result = work.manager.save_editor_source(
            str(work.root), base_reader(str(work.root))["workspaceId"],
            hashlib.sha256(work.original).hexdigest(), "// edit\n" + work.original.decode())
    assert result["sourceOnly"] is True and result["compileScheduled"] is False
    with pytest.raises(harness.ShaderOrganizationError):
        work.manager.validate_publication_gate(str(work.root))


def test_editor_source_save_keeps_newer_ai_proposal_and_foreign_workspace(work):
    from src.shader_workspace.errors import WorkspaceWatchConflictError
    opened = work.manager.begin(str(work.root))
    workspace_id = work.service.status(str(work.root))["workspaceId"]
    proposal = Path(opened["proposalSourcePath"])
    newer = b"// AI has newer work\n" + work.original
    proposal.write_bytes(newer)
    before = {str(path.relative_to(work.root)): path.read_bytes()
              for path in (work.root / "ai").rglob("*") if path.is_file()}
    with pytest.raises(WorkspaceWatchConflictError):
        work.manager.save_editor_source(str(work.root), workspace_id,
                                        opened["proposalSourceSha256"], "// editor\n" + work.original.decode())
    assert proposal.read_bytes() == newer and work.service.source_path.read_bytes() == work.original
    assert before == {str(path.relative_to(work.root)): path.read_bytes()
                      for path in (work.root / "ai").rglob("*") if path.is_file()}
    with pytest.raises(harness.ShaderOrganizationError, match="identity changed"):
        work.manager.save_editor_source(str(work.root), "f" * 32,
                                        opened["proposalSourceSha256"], "// editor\n" + work.original.decode())


@pytest.mark.parametrize("published", [False, True])
def test_stale_editor_save_does_not_begin_draft_or_invalidate_publication(work, published):
    from src.shader_workspace.errors import WorkspaceWatchConflictError
    ai = work.root / "ai"
    ai.mkdir(exist_ok=True)
    gate = ai / "organize-code-gate.json"
    if published:
        gate.write_text(json.dumps({"decision": "accepted-executable-editable",
                                   "finalBinding": {"sourceSha256": hashlib.sha256(work.original).hexdigest()}}))
    before = {str(path.relative_to(ai)): path.read_bytes() for path in ai.rglob("*") if path.is_file()}
    with mock.patch.object(work.manager, "begin", wraps=work.manager.begin) as begin:
        with pytest.raises(WorkspaceWatchConflictError):
            work.manager.save_editor_source(str(work.root), work.service.status(str(work.root))["workspaceId"],
                                            "f" * 64, "// stale editor\n" + work.original.decode())
        begin.assert_not_called()
    assert before == {str(path.relative_to(ai)): path.read_bytes() for path in ai.rglob("*") if path.is_file()}
    assert work.service.source_path.read_bytes() == work.original


def test_editor_save_rechecks_project_after_waiting_before_any_draft_mutation(work):
    from contextlib import contextmanager
    @contextmanager
    def switched_project():
        raise RuntimeError("active project selection changed")
        yield
    workspace_id = work.service.status(str(work.root))["workspaceId"]
    expected = hashlib.sha256(work.original).hexdigest()
    with mock.patch.object(work.manager, "begin", wraps=work.manager.begin) as begin:
        with pytest.raises(RuntimeError, match="selection changed"):
            work.manager.save_editor_source(str(work.root), workspace_id, expected,
                                            "// pending save\n" + work.original.decode(),
                                            commit_guard=switched_project)
        begin.assert_not_called()
    assert work.service.source_path.read_bytes() == work.original
    assert not (work.root / "ai/organize-code-gate.json").exists()


def test_editor_save_retry_of_identical_published_source_does_not_reopen_it(work):
    gate = work.root / "ai/organize-code-gate.json"
    gate.parent.mkdir(exist_ok=True)
    published = json.dumps({"decision": "accepted-executable-editable",
                            "finalBinding": {"sourceSha256": hashlib.sha256(work.original).hexdigest()}}).encode()
    gate.write_bytes(published)
    with mock.patch.object(work.manager, "begin", wraps=work.manager.begin) as begin:
        saved = work.manager.save_editor_source(
            str(work.root), work.service.status(str(work.root))["workspaceId"],
            "f" * 64, work.original.decode())
        begin.assert_not_called()
    assert saved["rawSha256"] == hashlib.sha256(work.original).hexdigest()
    assert gate.read_bytes() == published
    assert not (work.root / "ai/organization-sessions").exists()


def test_save_draft_uses_last_synced_hash_and_preserves_concurrent_source(work) -> None:
    opened = work._begin()
    proposal = Path(opened["proposalSourcePath"])
    proposal.write_bytes(b"// first\n" + work.original)
    first = work.manager.save_draft(str(work.root), opened["organizationId"])
    proposal.write_bytes(b"// second\n" + work.original)
    work.service.source_path.write_bytes(b"// independent edit\n" + work.original)

    with pytest.raises(harness.ShaderOrganizationError, match="changed since this draft"):
        work.manager.save_draft(str(work.root), opened["organizationId"])
    assert work.service.source_path.read_bytes().startswith(b"// independent edit")
    assert first["sourceSha256"] != harness.organization_module._sha256(proposal.read_bytes())


def test_save_draft_cas_preserves_editor_change_during_source_swap(work) -> None:
    opened = work._begin()
    proposal = Path(opened["proposalSourcePath"])
    proposal.write_bytes(b"// organized\n" + work.original)
    independent = b"// saved by editor\n" + work.original
    original_cas = harness.organization_module._source_compare_and_swap

    def editor_wins(path, expected, replacement, label):
        Path(path).write_bytes(independent)
        return original_cas(path, expected, replacement, label)

    with mock.patch.object(
        harness.organization_module, "_source_compare_and_swap", side_effect=editor_wins
    ):
        with pytest.raises(harness.ShaderOrganizationError, match="changed during draft save"):
            work.manager.save_draft(str(work.root), opened["organizationId"])

    assert work.service.source_path.read_bytes() == independent
    session = json.loads(Path(opened["sessionPath"]).read_text(encoding="utf-8"))
    assert not session.get("syncedDraftSourceSha256")


def test_save_draft_metadata_failure_rolls_back_only_unchanged_source(work) -> None:
    opened = work._begin()
    proposal = Path(opened["proposalSourcePath"])
    proposal.write_bytes(b"// organized\n" + work.original)
    with mock.patch.object(
        harness.organization_module,
        "apply_workspace_mutation",
        side_effect=RuntimeError("metadata unavailable"),
    ):
        with pytest.raises(RuntimeError, match="metadata unavailable"):
            work.manager.save_draft(str(work.root), opened["organizationId"])
    assert work.service.source_path.read_bytes() == work.original

    independent = b"// saved by editor\n" + work.original

    def editor_after_cas(*args, **kwargs):
        work.service.source_path.write_bytes(independent)
        raise RuntimeError("metadata unavailable")

    with mock.patch.object(
        harness.organization_module,
        "apply_workspace_mutation",
        side_effect=editor_after_cas,
    ):
        with pytest.raises(harness.ShaderOrganizationError, match="external source edit"):
            work.manager.save_draft(str(work.root), opened["organizationId"])
    assert work.service.source_path.read_bytes() == independent


def test_save_draft_resumes_after_interruption_between_source_and_metadata(work) -> None:
    opened = work._begin()
    proposal = Path(opened["proposalSourcePath"])
    proposal.write_bytes(b"// organized\n" + work.original)
    with mock.patch.object(
        harness.organization_module,
        "apply_workspace_mutation",
        side_effect=SystemExit("interrupted after source CAS"),
    ):
        with pytest.raises(SystemExit, match="interrupted after source CAS"):
            work.manager.save_draft(str(work.root), opened["organizationId"])

    assert work.service.source_path.read_bytes() == proposal.read_bytes()
    resumed_manager = harness.ShaderOrganizationManager(
        lambda: work.service, work.manager._validate_candidate
    )
    assert resumed_manager.begin(str(work.root))["organizationId"] == opened["organizationId"]
    saved = resumed_manager.save_draft(str(work.root), opened["organizationId"])
    assert saved["sourceSha256"] == harness.organization_module._sha256(proposal.read_bytes())
    assert work.service.source_path.read_bytes() == proposal.read_bytes()


@pytest.mark.parametrize("compiled_history", [False, True])
def test_save_draft_can_generate_navigation_graph_without_validation(work, compiled_history) -> None:
    if compiled_history:
        work.service.compile(str(work.root))
    previous_revision = work.service.revision
    opened = work._begin()
    proposal = Path(opened["proposalSourcePath"])
    proposal.write_bytes(b"// captured output\n" + work.original)
    saved = work.manager.save_draft(
        str(work.root), opened["organizationId"], generate_flow_graph=True
    )
    assert saved["flowGraph"]["generated"] is True
    graph = json.loads(Path(saved["flowGraph"]["artifactPath"]).read_text())
    assert graph["sourceRevision"] == work.service.revision
    assert graph["origin"] == "editable-draft"
    assert graph["sourceSha256"] == saved["sourceSha256"]
    from src.shader_workspace.flow_graph import validate_shader_flow_graph
    validate_shader_flow_graph(
        graph,
        expected_workspace_id=work.service.workspace_id,
        expected_source_sha256=hashlib.sha256(proposal.read_bytes()).hexdigest(),
        expected_source_revision=previous_revision,
    )
    assert work.service.revision == previous_revision
    assert graph["policy"]["navigationOnly"] is True
    assert graph["policy"]["compileAuthorization"] is False
    assert graph["policy"]["applyAuthorization"] is False


def test_explicit_finish_after_save_validates_original_baseline_and_current_draft(work) -> None:
    opened = work._begin()
    proposal = Path(opened["proposalSourcePath"])
    proposal.write_bytes(b"// readable draft\n" + work.original)
    saved = work.manager.save_draft(str(work.root), opened["organizationId"])

    finished = work.manager.finish_draft(str(work.root), opened["organizationId"])

    assert finished["ok"] is True
    assert finished["wholeSourceValidated"] is True
    assert work.service.source_path.read_bytes() == proposal.read_bytes()
    gate = work.manager.validate_publication_gate(str(work.root))
    assert gate["baselineSourceSha256"] == opened["baselineSourceSha256"]
    assert gate["finalSourceSha256"] == saved["sourceSha256"]


def test_explicit_finish_failure_restores_last_saved_editable_source(work) -> None:
    opened = work._begin()
    proposal = Path(opened["proposalSourcePath"])
    proposal.write_bytes(b"// first saved draft\n" + work.original)
    saved = work.manager.save_draft(str(work.root), opened["organizationId"])
    saved_bytes = work.service.source_path.read_bytes()
    proposal.write_bytes(b"// next draft\n" + work.original)
    original_validate = work.manager._validate_candidate

    def candidate_abi_failure(*args):
        if work.service.source_path.read_bytes() != work.original:
            raise harness.ShaderOrganizationError("candidate ABI mismatch")
        return original_validate(*args)

    with mock.patch.object(work.manager, "_validate_candidate", side_effect=candidate_abi_failure):
        result = work.manager.finish_draft(str(work.root), opened["organizationId"])
    assert result["ok"] is False
    assert result["previousSourceRestored"] is True
    assert result["baselineRestored"] is False
    assert work.service.source_path.read_bytes() == saved_bytes
    assert saved["sourceSha256"] == harness.organization_module._sha256(saved_bytes)


def test_finish_refuses_concurrent_source_change_without_overwriting_it(work) -> None:
    opened = work._begin()
    Path(opened["proposalSourcePath"]).write_text(
        "// readable\nfloat4 MainPS() : SV_Target { return 1.0; }\n",
        encoding="utf-8",
    )
    concurrent = b"float4 MainPS() : SV_Target { return 0.5; }\n"
    original_cas = harness.organization_module._source_compare_and_swap

    def race(path, expected, replacement, label):
        if replacement.startswith(b"// readable"):
            Path(path).write_bytes(concurrent)
            return False
        return original_cas(path, expected, replacement, label)

    with mock.patch.object(harness.organization_module, "_source_compare_and_swap", side_effect=race):
        with pytest.raises(harness.ShaderOrganizationError, match="changed before finishing"):
            work.manager.finish_draft(str(work.root), opened["organizationId"])
    assert work.service.source_path.read_bytes() == concurrent
    assert Path(opened["proposalSourcePath"]).read_text(encoding="utf-8").startswith("// readable")


def test_finish_rejects_candidate_abi_failure_and_restores_baseline(work) -> None:
    opened = work._begin()
    proposal = Path(opened["proposalSourcePath"])
    proposal.write_text(
        "// readable\nfloat4 MainPS() : SV_Target { return 1.0; }\n",
        encoding="utf-8",
    )
    original_validate = work.manager._validate_candidate

    def candidate_abi_failure(*args):
        if work.service.source_path.read_bytes() != work.original:
            raise harness.ShaderOrganizationError("candidate ABI mismatch")
        return original_validate(*args)

    with mock.patch.object(work.manager, "_validate_candidate", side_effect=candidate_abi_failure):
        result = work.manager.finish_draft(str(work.root), opened["organizationId"])
    assert result["ok"] is False
    assert result["promoted"] is False
    assert "candidate ABI mismatch" in result["reason"]
    assert "numericRecovery" not in result
    assert work.service.source_path.read_bytes() == work.original
    assert proposal.read_text(encoding="utf-8").startswith("// readable")


def test_finish_never_promotes_abi_compatible_but_equivalence_rejected_draft(work) -> None:
    opened = work._begin()
    proposal = Path(opened["proposalSourcePath"])
    proposal.write_text(
        "// semantic-change\nfloat4 MainPS() : SV_Target { return 0.0; }\n",
        encoding="utf-8",
    )

    def rejected_observation(*args):
        value = work._observation(*args)
        value["accepted"] = False
        value["reason"] = "captured output changed"
        return value

    work.manager._compare_candidates = mock.Mock(side_effect=rejected_observation)
    result = work.manager.finish_draft(
        str(work.root), opened["organizationId"]
    )
    assert result["ok"] is False
    assert result["promoted"] is False
    assert result["applied"] is False
    assert result["draftPreserved"] is True
    assert result["baselineRestored"] is True
    assert result["equivalenceLevel"] == "rejected"
    assert result["observation"]["accepted"] is False
    recovery = result["numericRecovery"]
    assert recovery["tool"] == "diagnose_shader_organization_candidate"
    assert recovery["arguments"] == {
        "workspace_path": str(work.root),
        "expected_source_sha256": hashlib.sha256(work.original).hexdigest(),
    }
    assert recovery["proposalSourceSha256"] == hashlib.sha256(proposal.read_bytes()).hexdigest()
    assert recovery["completeSourceReviewRequired"] is False
    assert recovery["publicationAllowed"] is False
    assert "captured output changed" in result["reason"]
    assert work.service.source_path.read_bytes() == work.original
    assert proposal.read_text(encoding="utf-8").startswith("// semantic-change")
    session = Path(opened["sessionPath"]).read_text(encoding="utf-8")
    assert '"state": "drafting"' in session


def test_finalized_source_gate_matches_current_candidate(work) -> None:
    opened = work._begin()
    Path(opened["proposalSourcePath"]).write_text(
        "// Organized draft with remaining functions.\n"
        "float4 MainPS() : SV_Target { return 1.0; }\n",
        encoding="utf-8",
    )

    finished = work.manager.finish_draft(
        str(work.root), opened["organizationId"]
    )

    assert finished["ok"] is True
    assert finished["promoted"] is True
    assert finished["wholeSourceValidated"] is True
    assert finished["semanticOrganizationVerified"] is False
    assert "organizationComplete" not in finished
    gate = json.loads(Path(finished["gatePath"]).read_text(encoding="utf-8"))
    assert gate["publicationContract"] == "validated-source"
    assert work.manager.validate_publication_gate(str(work.root)) == gate

    sealed = json.loads(Path(opened["sessionPath"]).read_text(encoding="utf-8"))
    for field in (
        "renameProvenance",
        "flowGraphId",
        "flowGraphSha256",
    ):
        assert field in gate and field in sealed
        assert gate[field] == sealed[field]
    assert gate["baselineReviewRevision"] > 0
    assert gate["baselineReviewCandidateRawSha256"]
    assert gate["baselineReviewAbiRequestId"]
    assert gate["baselineReviewAbiReportSha256"]


def test_finalized_source_receipt_opens_in_editor_reader(work) -> None:
    from test_shader_console import console

    opened = work._begin()
    Path(opened["proposalSourcePath"]).write_bytes(b"// readable\n" + work.original)
    finished = work.manager.finish_draft(str(work.root), opened["organizationId"])

    assert finished["ok"] is True
    gate = work.manager.validate_publication_gate(str(work.root))
    console._validate_organization_source_receipt(
        str(work.root), str(work.service.source_path), gate
    )


def test_finish_generates_optional_function_call_graph_from_final_source(work) -> None:
    opened = work._begin()
    Path(opened["proposalSourcePath"]).write_text(
        "// MainPS returns the captured color output.\n"
        + work.original.decode("utf-8"),
        encoding="utf-8",
    )

    finished = work.manager.finish_draft(
        str(work.root), opened["organizationId"], generate_flow_graph=True
    )

    assert finished["ok"] is True
    assert finished["flowGraph"]["generated"] is True
    graph = json.loads(Path(finished["flowGraph"]["artifactPath"]).read_text())
    assert graph["sourceSha256"] == finished["finalBinding"]["sourceSha256"]
    assert graph["sourceRevision"] == finished["finalBinding"]["revision"]
    assert graph["nodes"][0]["name"] == "MainPS"
    assert "captured color output" in graph["nodes"][0]["description"]
    assert work.manager.validate_publication_gate(str(work.root))["wholeSourceValidated"] is True


def test_flow_graph_failure_does_not_roll_back_validated_source(work) -> None:
    opened = work._begin()
    proposal = Path(opened["proposalSourcePath"])
    proposal.write_bytes(b"// readable\n" + work.original)

    with mock.patch.object(
        harness.organization_module, "build_shader_flow_graph",
        side_effect=RuntimeError("graph builder unavailable"),
    ):
        finished = work.manager.finish_draft(
            str(work.root), opened["organizationId"], generate_flow_graph=True
        )

    assert finished["ok"] is True and finished["promoted"] is True
    assert finished["flowGraph"]["generated"] is False
    assert "graph builder unavailable" in finished["flowGraph"]["diagnostic"]
    assert work.service.source_path.read_bytes() == proposal.read_bytes()
    assert work.manager.validate_publication_gate(str(work.root))["wholeSourceValidated"] is True


def test_unvalidated_draft_can_finish_after_fresh_final_baseline_proof(work) -> None:
    with mock.patch.object(work.service, "compile", side_effect=RuntimeError("compiler unavailable")):
        opened = work._begin()
    assert opened["baselineCompiledAndAbiValidated"] is False
    assert not (Path(opened["sessionPath"]).parent / "baseline-compile.json").exists()

    finished = work.manager.finish_draft(str(work.root), opened["organizationId"])
    assert finished["ok"] is True
    gate = work.manager.validate_publication_gate(str(work.root))
    assert gate["baselineCompiledAndAbiValidated"] is True
    assert (Path(opened["sessionPath"]).parent / "baseline-compile.json").is_file()
    assert (Path(opened["sessionPath"]).parent / "baseline-abi.json").is_file()


@pytest.mark.parametrize(
    "failure_phase",
    ["artifact", "session", "completed-store", "checkpoint-store", "root-gate"],
)
def test_finish_publication_failure_restores_retryable_draft(
    work, failure_phase
) -> None:
    opened = work._begin()
    proposal = Path(opened["proposalSourcePath"])
    proposal.write_text(
        "// organized-name-only\nfloat4 MainPS() : SV_Target { return 1.0; }\n",
        encoding="utf-8",
    )
    expected_error = "injected %s failure" % failure_phase
    root_gate = work.root / "ai" / "organize-code-gate.json"
    root_gate_before = root_gate.read_bytes() if root_gate.is_file() else None

    if failure_phase == "artifact":
        original_write = harness.organization_module.atomic_write_bytes

        def fail_artifact(path, content):
            if Path(path).name == "organized.hlsl":
                raise RuntimeError(expected_error)
            return original_write(path, content)

        patcher = mock.patch.object(
            harness.organization_module,
            "atomic_write_bytes",
            side_effect=fail_artifact,
        )
    elif failure_phase == "session":
        original_write = harness.organization_module.atomic_write_json
        session_path = Path(opened["sessionPath"]).resolve()

        def fail_session(path, value):
            if Path(path).resolve() == session_path:
                raise RuntimeError(expected_error)
            return original_write(path, value)

        patcher = mock.patch.object(
            harness.organization_module,
            "atomic_write_json",
            side_effect=fail_session,
        )
    elif failure_phase == "completed-store":
        patcher = mock.patch.object(
            work.manager._completed_reviews,
            "remember",
            side_effect=RuntimeError(expected_error),
        )
    elif failure_phase == "checkpoint-store":
        patcher = mock.patch.object(
            work.manager._checkpoints,
            "remember",
            side_effect=RuntimeError(expected_error),
        )
    else:
        original_publish = work.manager._publish_root_gate

        def publish_then_fail(*args, **kwargs):
            original_publish(*args, **kwargs)
            raise RuntimeError(expected_error)

        patcher = mock.patch.object(
            work.manager,
            "_publish_root_gate",
            side_effect=publish_then_fail,
        )

    with patcher, pytest.raises(harness.ShaderOrganizationError) as raised:
        work.manager.finish_draft(str(work.root), opened["organizationId"])

    assert expected_error in str(raised.value)
    assert work.service.source_path.read_bytes() == work.original
    assert proposal.read_text(encoding="utf-8").startswith(
        "// organized-name-only"
    )
    session = json.loads(Path(opened["sessionPath"]).read_text(encoding="utf-8"))
    assert session["state"] == "drafting"
    assert session["organizationId"] == opened["organizationId"]
    assert work.manager._checkpoints.contains(session) is True
    assert (
        root_gate.read_bytes() if root_gate.is_file() else None
    ) == root_gate_before


def test_glsl_host_ide_disables_mechanical_extract() -> None:
    caps = organization_capabilities("glsl")
    assert caps["structureExtraction"] is False
    assert caps["rename"] is True
    assert caps["finalCompileAbiAvailable"] is True


@pytest.mark.parametrize("apply_requested", [False, True])
@pytest.mark.parametrize("metadata_complete", [False, True])
def test_finish_only_applies_when_requested(apply_requested: bool, metadata_complete: bool, monkeypatch) -> None:
    monkeypatch.setattr("src.shader_workspace.organization._draft_status", lambda *a: {})
    monkeypatch.setattr("src.shader_final_publication.publish_finished_replacement", lambda *a: None)
    monkeypatch.setattr(server, "_shader_cbuffer_delivery_from_status", lambda *a, **k: {
        "required": True, "status": "complete" if metadata_complete else "pending"})
    monkeypatch.setattr(server, "_register_active_project_workspace_receipt", lambda *a, **k: {})
    monkeypatch.setattr("src.shader_organization_review.publication_review", lambda *args: {"ready": True, "sourceSha256": "d" * 64})
    organization_id = "a" * 32
    order: list[str] = []
    manager = mock.Mock()
    manager.finish_draft.return_value = {
        "schema": "replaylab-shader-organization-finish/v1",
        "ok": True,
        "promoted": True,
        "applied": False,
        "organizationId": organization_id,
        "workspacePath": "D:/workspace",
        "expectedRevision": 7,
        "expectedCandidateRawSha256": "b" * 64,
        "wholeSourceValidated": True,
    }
    manager.save_draft.return_value = {
        "schema": "replaylab-shader-organization-save/v1",
        "ok": True,
        "promoted": True,
        "published": False,
        "applied": False,
        "organizationId": organization_id,
        "workspacePath": "D:/workspace",
        "sourceSha256": "d" * 64,
    }

    service = mock.Mock()
    def compile_source(unused_workspace):
        order.append("shader-compiled")
        return {
            "revision": 7,
            "rawSha256": "b" * 64,
            "sourceSha256": "d" * 64,
        }
    service.compile.side_effect = compile_source

    def apply(*unused_args, **unused_kwargs):
        order.append("shader-applied")
        return json.dumps({"applied": True, "replacement": {"replacementId": "c" * 32}})

    with mock.patch.object(
        server, "_shader_organization_manager", return_value=manager
    ), mock.patch.object(
        server, "_shader_workspace_service", return_value=service
    ), mock.patch.object(
        server, "apply_shader_workspace_shared_resource", side_effect=apply
    ) as apply_mock:
        result = json.loads(server.finish_shader_workspace_organization(
            "D:/workspace", organization_id,
            apply_shared_resource=apply_requested,
            start_live_edit=False,
            publication_mode="final",
        ))

    assert result["applied"] is apply_requested
    assert order == (["shader-applied"] if apply_requested else [])
    assert result["delivery"]["sourceDeliveryComplete"] is True
    assert result["delivery"]["complete"] is metadata_complete
    assert result["delivery"]["wholeTaskComplete"] is metadata_complete
    if not metadata_complete:
        assert result["delivery"]["nextAction"] == "complete-resource-naming-without-recompiling"
    service.compile.assert_not_called()  # finish owns the single final validation
    if apply_requested:
        assert "registration" in result
        apply_call = apply_mock.call_args
        assert apply_call.kwargs["expected_revision"] == 7
        assert apply_call.kwargs["expected_candidate_raw_sha256"] == "b" * 64
    assert manager.finish_draft.call_args.args == ("D:/workspace", organization_id)
    manager.save_draft.assert_not_called()


def test_finish_can_explicitly_request_strict_organization_validation(monkeypatch) -> None:
    monkeypatch.setattr(server, "_register_active_project_workspace_receipt", lambda *a, **k: {})
    monkeypatch.setattr("src.shader_organization_review.publication_review", lambda *args: {"ready": True, "sourceSha256": "d" * 64})
    organization_id = "a" * 32
    manager = mock.Mock()
    manager.finish_draft.return_value = {"ok": True, "applied": False, "wholeSourceValidated": True}
    with mock.patch.object(server, "_shader_organization_manager", return_value=manager):
        result = json.loads(server.finish_shader_workspace_organization(
            "D:/workspace", organization_id, validate_for_apply=True, publication_mode="final",
        ))
    assert result["wholeSourceValidated"] is True
    manager.finish_draft.assert_called_once_with(
        "D:/workspace", organization_id, generate_flow_graph=False, expected_reviewed_source_sha256="d" * 64,
    )
    manager.save_draft.assert_not_called()


@pytest.mark.parametrize("preview_available", [True, False])
def test_source_save_can_show_preview_without_compile_or_apply(preview_available) -> None:
    organization_id = "a" * 32
    source_sha = "d" * 64
    manager = mock.Mock()
    manager.save_draft.return_value = {"ok": True, "applied": False, "sourceSha256": source_sha}
    preview = {"shown": True, "readOnly": True, "replayApplied": False}
    with mock.patch.object(server, "_shader_organization_manager", return_value=manager), \
         mock.patch.object(server, "_alias_publication_workspace_status", return_value={}), \
         mock.patch.object(server, "_workspace_active_capture_context", return_value={}), \
         mock.patch.object(server, "_active_capture_identity", return_value=("b" * 64, "c" * 32)), \
         mock.patch.object(server.ipc, "call", return_value=preview,
                           side_effect=None if preview_available else RuntimeError("panel unavailable")) as display, \
         mock.patch.object(server, "_shader_workspace_service", side_effect=AssertionError("no compiler needed")), \
         mock.patch.object(server, "apply_shader_workspace_shared_resource", side_effect=AssertionError("no replay needed")):
        result = json.loads(server.finish_shader_workspace_organization(
            "D:/workspace", organization_id, show_in_workspace=True, validate_for_apply=False,
        ))
    assert result["ok"] is preview_available and result["applied"] is False
    assert result["preview"]["shown"] is preview_available
    assert result["preview"]["readOnly"] is True
    assert result["preview"]["replayApplied"] is False
    if not preview_available:
        assert result["delivery"]["complete"] is False
        assert result["delivery"]["nextAction"] == "show-saved-source-without-recompiling"
    display.assert_called_once_with("show_shader_organization_preview", {
        "workspace_path": "D:/workspace", "organization_id": organization_id,
        "expected_source_sha256": source_sha, "expected_capture_id": "b" * 64,
        "expected_capture_session_id": "c" * 32,
    }, timeout=30.0)


def test_completed_source_can_be_displayed_without_resaving_or_compiling(tmp_path) -> None:
    (tmp_path / "ai").mkdir()
    (tmp_path / "source").mkdir()
    source = b"float4 MainPS() : SV_Target { return 1; }\n"
    (tmp_path / "source" / "shader.hlsl").write_bytes(source)
    source_sha = hashlib.sha256(source).hexdigest()
    organization_id = "a" * 32
    (tmp_path / "manifest.json").write_text(json.dumps({
        "source": {"path": "source/shader.hlsl"},
    }))
    (tmp_path / "ai" / "organize-code-gate.json").write_text(json.dumps({
        "decision": "accepted-executable-editable", "organizationId": organization_id,
        "finalBinding": {"sourceSha256": source_sha},
    }))
    context = {"capture": {"loaded": True, "captureSessionId": "d" * 32}}
    with mock.patch.object(server, "_active_project_service"), \
         mock.patch.object(server, "_require_managed_active_workspace", return_value=tmp_path), \
         mock.patch.object(server, "_alias_publication_workspace_status", side_effect=AssertionError("display needs no baseline validation")), \
         mock.patch.object(server, "_workspace_active_capture_context", side_effect=AssertionError("display needs no catalog binding")), \
         mock.patch.object(server.ipc, "call", side_effect=[context, {"shown": True, "readOnly": True, "replayApplied": False}]) as display, \
         mock.patch.object(server, "_shader_workspace_service", side_effect=AssertionError("no compile required")), \
         mock.patch.object(server, "_shader_organization_manager", side_effect=AssertionError("no resave required")):
        result = json.loads(server.show_shader_workspace_source(str(tmp_path)))
    assert result["shown"] is True and result["replayApplied"] is False
    assert display.call_args.args[0] == "show_shader_organization_preview"
    assert display.call_args.args[1]["expected_source_sha256"] == source_sha
    assert display.call_args.args[1]["canonical_source"] is True
    assert "organization_id" not in display.call_args.args[1]
    assert "expected_capture_id" not in display.call_args.args[1]


def test_finish_apply_reuses_current_compiled_candidate(monkeypatch) -> None:
    monkeypatch.setattr("src.shader_workspace.organization._draft_status", lambda *a: {})
    monkeypatch.setattr("src.shader_final_publication.publish_finished_replacement", lambda *a: None)
    monkeypatch.setattr(server, "_shader_cbuffer_delivery_from_status", lambda *a, **k: {"required": False, "status": "not-applicable"})
    monkeypatch.setattr(server, "_register_active_project_workspace_receipt", lambda *a, **k: {})
    monkeypatch.setattr("src.shader_organization_review.publication_review", lambda *args: {"ready": True, "sourceSha256": "d" * 64})
    organization_id = "a" * 32
    source_sha = "d" * 64
    candidate_sha = "b" * 64
    manager = mock.Mock()
    manager.finish_draft.return_value = {
        "ok": True, "applied": False, "sourceSha256": source_sha, "wholeSourceValidated": True,
        "expectedRevision": 7, "expectedCandidateRawSha256": candidate_sha,
    }
    service = mock.Mock()
    service.status.return_value = {
        "revision": 7,
        "source": {"currentSha256": source_sha},
        "latestCompiled": {
            "revision": 7, "rawSha256": candidate_sha,
            "sourceSha256": source_sha,
        },
    }
    with mock.patch.object(server, "_shader_organization_manager", return_value=manager), \
         mock.patch.object(server, "_shader_workspace_service", return_value=service), \
         mock.patch.object(server, "apply_shader_workspace_shared_resource",
                           return_value=json.dumps({"applied": True, "replacement": {"replacementId": "c" * 32}})) as apply_mock:
        result = json.loads(server.finish_shader_workspace_organization(
            "D:/workspace", organization_id, apply_shared_resource=True, publication_mode="final",
        ))
    assert result["applied"] is True
    assert result["delivery"]["complete"] is True
    manager.save_draft.assert_not_called()
    service.compile.assert_not_called()
    apply_mock.assert_called_once_with(
        "D:/workspace", expected_revision=7,
        expected_candidate_raw_sha256=candidate_sha,
    )
