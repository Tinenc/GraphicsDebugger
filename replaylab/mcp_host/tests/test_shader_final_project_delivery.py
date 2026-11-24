"""Final delivery must publish and display the same current project source."""
from contextlib import nullcontext
import copy
import hashlib
import json
from unittest import mock

import pytest

from src import project_restore, server, shader_final_publication


SOURCE = "d" * 64
CANDIDATE = "b" * 64
KNOWLEDGE = "c" * 32
SESSION = "e" * 32
UI_ID = "f" * 64
FINISHED = {"ok": True, "applied": False, "wholeSourceValidated": True,
            "sourceSha256": SOURCE, "expectedCandidateRawSha256": CANDIDATE,
            "expectedRevision": 7, "organizationId": "a" * 32}
REGISTRATION = {"result": {"knowledge": {"knowledgeId": KNOWLEDGE}}}
PREVIEW = {"shown": True, "readOnly": False, "sourceSha256": SOURCE,
           "workspaceId": UI_ID, "captureSessionId": SESSION}
STATE = {
    "selectedWorkspaceId": UI_ID, "captureBinding": {"captureSessionId": SESSION},
    "workspaces": [{"workspaceId": UI_ID, "visibleInWorkspaceTree": True,
                    "editorEligible": True, "openInTab": True,
                    "projectSourceIdentity": {"knowledgeId": KNOWLEDGE}}],
    "documents": [{"workspaceId": UI_ID, "available": True, "readOnly": False,
                   "dirty": False, "sourceSha256": SOURCE, "loadedSourceSha256": SOURCE}],
}


@pytest.mark.parametrize("failure", ["", "register", "show", "retired", "old-head", "read-only",
                                   "wrong-source", "stale-loaded"])
def test_final_delivery_orders_effects_and_preserves_apply_on_late_failure(monkeypatch, failure):
    order = []
    manager = mock.Mock()
    manager.finish_draft.side_effect = lambda *a, **k: order.append("finish") or dict(FINISHED)
    monkeypatch.setattr(server, "_shader_organization_manager", lambda: manager)
    monkeypatch.setattr("src.shader_organization_review.publication_review",
                        lambda *a: {"ready": True, "sourceSha256": SOURCE})
    monkeypatch.setattr("src.shader_workspace.organization._draft_status", lambda *a: {})
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: mock.Mock())
    monkeypatch.setattr(server, "_shader_cbuffer_delivery_from_status",
                        lambda *a, **k: {"required": True, "status": "complete"})
    monkeypatch.setattr('src.shader_overview_delivery.saved_source_hashes',lambda *a:{SOURCE})
    monkeypatch.setattr('src.shader_overview_delivery.display_receipt',lambda *a:{'complete':True})
    active = mock.Mock()
    active.status.return_value = {"scopeConfigured": True, "projectRoot": "D:/project"}
    monkeypatch.setattr(server, "_active_project_service", lambda: active)

    def register(*args, **kwargs):
        order.append("register")
        assert kwargs["expected_finish"] == FINISHED
        assert kwargs["replacement_id"] == "3" * 32
        assert kwargs["embed_capture_index"] is False
        if failure == "register":
            raise RuntimeError("project disk unavailable")
        return {"projectCommitted": True, "derivedAssociationWritten": True,
                "knowledge": {"knowledgeId": KNOWLEDGE},
                "workspaceRegistration": copy.deepcopy(REGISTRATION["result"])}

    def show(*args):
        order.append("show")
        if failure == "show":
            return json.dumps({"ok": False, "error": "panel unavailable"})
        return json.dumps(PREVIEW)

    def read(method, params, **kwargs):
        order.append("readback")
        assert method == "get_shader_workspace_ui_state"
        state = copy.deepcopy(STATE)
        if failure == "retired":
            state["workspaces"][0]["visibleInWorkspaceTree"] = False
        if failure == "old-head":
            state["workspaces"][0]["projectSourceIdentity"]["knowledgeId"] = "1" * 32
        if failure == "read-only":
            state["documents"][0]["readOnly"] = True
        if failure == "wrong-source":
            state["documents"][0]["sourceSha256"] = "2" * 64
        if failure == "stale-loaded":
            state["documents"][0]["loadedSourceSha256"] = "2" * 64
        return state

    monkeypatch.setattr(project_restore, "sync_active_shader_workspace_after_apply", register)
    monkeypatch.setattr(server, "_register_active_project_workspace_receipt",
                        mock.Mock(side_effect=AssertionError("finish must not register knowledge twice")))
    monkeypatch.setattr(server, "show_shader_workspace_source", show)
    monkeypatch.setattr(server.ipc, "call", read)
    monkeypatch.setattr(server, "apply_shader_workspace_shared_resource",
        lambda *a, **k: order.append("apply") or json.dumps(
            {"applied": True, "replacement": {"replacementId": "3" * 32}}))
    monkeypatch.setattr(server, "start_shader_workspace_live_edit",
        lambda *a: order.append("live-edit") or json.dumps({"started": True}))
    result = json.loads(server.finish_shader_workspace_organization(
        "D:/workspace", "a" * 32, apply_shared_resource=True, start_live_edit=True,
        show_in_workspace=True))
    assert order == ["finish", "apply", "register", "live-edit"] + (
        [] if failure == "register" else ["show"] + ([] if failure == "show" else ["readback"]))
    assert result["applied"] is True and result["delivery"]["applied"] is True
    assert result["delivery"]["readyForApply"] is True
    assert result["ok"] is (not failure)
    assert result["delivery"]["complete"] is (not failure)
    if failure:
        assert result["recoverable"] and result["delivery"]["repairRequired"]
    assert result["replacementRegistration"]["automatic"] is True
    assert result["replacementRegistration"]["ok"] is (failure != "register")
    if failure == "register":
        assert result["delivery"]["recovery"]["tool"] == "register_active_project_shader_replacement"
        assert result["delivery"]["recovery"]["arguments"]["replacement_id"] == "3" * 32
    manager.finish_draft.assert_called_once()


@pytest.mark.parametrize("mode", ["checkpoint", "final"])
def test_unvalidated_save_never_advances_project_head(monkeypatch, mode):
    manager = mock.Mock()
    manager.save_proposal.return_value = {"ok": True, "proposalSourcePath": "proposal.hlsl"}
    manager.save_draft.return_value = {"ok": True, "sourceSha256": SOURCE}
    monkeypatch.setattr(server, "_shader_organization_manager", lambda: manager)
    register = mock.Mock(side_effect=AssertionError("must not publish a source draft"))
    monkeypatch.setattr(server, "_register_active_project_workspace_receipt", register)
    result = json.loads(server.finish_shader_workspace_organization(
        "D:/workspace", "a" * 32, publication_mode=mode, validate_for_apply=False))
    assert not result["delivery"]["complete"]
    register.assert_not_called()
    manager.finish_draft.assert_not_called()


@pytest.mark.parametrize("changed", ["revision", "source", "candidate"])
def test_final_registration_checks_binding_inside_publication_lease(monkeypatch, tmp_path, changed):
    active = mock.Mock()
    active.status.return_value = {"workspaceId": "a" * 32, "projectRoot": str(tmp_path)}
    active.pin_active_project.return_value = nullcontext()
    service = mock.Mock()
    service.status.return_value = {"revision": 7}
    monkeypatch.setattr(server, "_active_project_service", lambda: active)
    monkeypatch.setattr(server, "_shader_workspace_artifact_service", lambda: service)
    monkeypatch.setattr(server, "_require_managed_active_workspace", lambda *a: tmp_path)
    monkeypatch.setattr(server, "_require_same_active_project", lambda *a: None)
    monkeypatch.setattr(server, "_workspace_active_capture_context", lambda *a, **k: {})
    monkeypatch.setattr(server, "_active_capture_identity", lambda *a: ("f" * 64, SESSION))
    status = {"revision": 8 if changed == "revision" else 7}
    monkeypatch.setattr(server, "_current_workspace_publication_binding", lambda *a: (
        "1" * 64 if changed == "source" else SOURCE,
        "2" * 64 if changed == "candidate" else CANDIDATE))
    guard = mock.Mock()
    monkeypatch.setattr(server, "_ensure_active_workspace_project_capture", guard)
    monkeypatch.setattr(server, "_register_reviewed_workspace_with_readback",
        lambda *a, **k: k["before_publish"](status))
    with pytest.raises(RuntimeError, match="validated Shader binding changed"):
        server._register_active_project_workspace_receipt(str(tmp_path), expected_finish=FINISHED)
    guard.assert_not_called()


def test_saved_source_keeps_current_project_head_and_exact_event(monkeypatch, tmp_path):
    (tmp_path / "source").mkdir()
    text = b"float4 MainPS() : SV_Target { return 1; }\n"
    (tmp_path / "source/shader.hlsl").write_bytes(text)
    source_sha = hashlib.sha256(text).hexdigest()
    manifest = {"source": {"path": "source/shader.hlsl"},
                "workspaceId": "f" * 32, "targetProfile": {"profile": "ps_6_0"},
                "baseline": {"abiSha256": "e" * 64},
                "shader": {"resourceId": 728, "rawSha256": "b" * 64},
                "stage": "fragment", "entryPoint": "MainPS",
                "capture": {"captureId": "c" * 64, "capturePath": "D:/game/1.rdc"}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    active = mock.Mock()
    active.status.return_value = {"replayProject": {"projectId": "a" * 32}}
    active.replay_service.return_value.find_current_workspace_source_head.return_value = {
        "projectId": "a" * 32, "knowledge": {"knowledgeId": KNOWLEDGE}}
    monkeypatch.setattr(server, "_active_project_service", lambda: active)
    monkeypatch.setattr(server, "_require_managed_active_workspace", lambda *a: tmp_path)
    monkeypatch.setattr(server, "_shader_organization_anchor_event", lambda *a: 61408)
    monkeypatch.setattr(server, "_SCOPED_CAPTURE_SESSION_ID", SESSION)
    show = mock.Mock(return_value=json.dumps({"shown": True}))
    monkeypatch.setattr(server, "_show_project_shader_source", show)
    assert json.loads(server.show_shader_workspace_source(str(tmp_path)))["shown"]
    args = show.call_args.kwargs
    assert args["knowledge_id"] == KNOWLEDGE
    assert args["identity"]["anchorEventId"] == 61408
    assert args["identity"]["resourceId"] == 728
    assert args["capture_session_id"] == SESSION
    active.replay_service.return_value.find_current_workspace_source_head.assert_called_once_with(
        source_sha256=source_sha, raw_sha256="b" * 64, stage="fragment", entry_point="MainPS",
        workspace_id="f" * 32, capture_id="c" * 64, target_profile={"profile": "ps_6_0"},
        baseline_abi_sha256="e" * 64)


def test_partial_derived_association_keeps_published_knowledge_and_exact_retry(monkeypatch):
    host = mock.Mock()
    host._active_project_service.return_value.status.return_value = {
        "scopeConfigured": True, "projectRoot": "项目/旧名称"}
    monkeypatch.setattr(project_restore, "sync_active_shader_workspace_after_apply", lambda **kw: {
        "projectCommitted": True, "derivedAssociationWritten": False,
        "knowledge": {"knowledgeId": KNOWLEDGE}, "workspaceRegistration": REGISTRATION["result"]})
    result = shader_final_publication.publish_finished_replacement(host, "项目/源码目录", FINISHED,
        {"replacement": {"replacementId": "3" * 32}})
    assert result["registration"]["ok"] is True
    assert result["registration"]["result"] == REGISTRATION["result"]
    replacement = result["replacementRegistration"]
    assert replacement["ok"] is False
    assert replacement["recovery"]["arguments"]["workspace_path"] == "项目/源码目录"
    assert replacement["recovery"]["arguments"]["replacement_id"] == "3" * 32
    assert replacement["recompileRequired"] is replacement["reapplyRequired"] is False


def test_unconfigured_project_keeps_original_source_publication_route(monkeypatch):
    host = mock.Mock()
    host._active_project_service.return_value.status.return_value = {"scopeConfigured": False}
    sync = mock.Mock(side_effect=AssertionError("unconfigured project"))
    monkeypatch.setattr(project_restore, "sync_active_shader_workspace_after_apply", sync)
    assert shader_final_publication.publish_finished_replacement(host, "old/workspace", FINISHED, {}) is None
    sync.assert_not_called()


@pytest.mark.parametrize("change", ["source", "revision", "candidate"])
def test_changed_finished_binding_fails_before_revalidation_or_replay(monkeypatch, tmp_path, change):
    source = "1" * 64 if change == "source" else SOURCE
    candidate = "2" * 64 if change == "candidate" else CANDIDATE
    revision = 8 if change == "revision" else 7
    status = {"revision": revision, "source": {"currentSha256": source},
              "latestCompiled": {"revision": revision, "rawSha256": candidate, "sourceSha256": source}}
    host = mock.Mock()
    host._require_managed_active_workspace.return_value = tmp_path
    host._shader_workspace_artifact_service.return_value.status.return_value = status
    host._shader_workspace_service.side_effect = AssertionError("metadata cannot initialize executable tools")
    host._current_workspace_publication_binding.return_value = (source, candidate)
    monkeypatch.setattr(project_restore, "_host_module", lambda: host)
    with pytest.raises(RuntimeError, match="validated Shader binding changed"):
        project_restore.sync_active_shader_workspace_after_apply(tmp_path, revision, candidate, "3" * 32,
                                                                 expected_finish=FINISHED)
    host._active_live_context.assert_not_called()
    host._fresh_shader_candidate_validation.assert_not_called()
    host._shader_workspace_service.assert_not_called()


def test_recovery_document_publication_does_not_take_a_second_watcher_source_lease(monkeypatch, tmp_path):
    status = {"revision": 7, "workspaceId": "a" * 32, "stage": "fragment", "entryPoint": "main",
        "capture": {"captureId": "b" * 64}, "baseline": {"rawSha256": "c" * 64}, "targetProfile": {},
        "source": {"currentSha256": SOURCE}, "replayAbiValidated": True, "abiCompatible": True,
        "latestCompiled": {"revision": 7, "sourceSha256": SOURCE, "rawSha256": CANDIDATE,
            "abiGate": {"status": "compatible"}, "replayAbiValidated": True, "abiCompatible": True,
            "requiresReplayAbiValidation": False, "semanticEquivalenceVerified": False,
            "validationScope": "replay-reflection-abi-compatible"}}
    host = mock.Mock()
    host._current_workspace_publication_binding.return_value = (SOURCE, CANDIDATE)
    host._validated_active_workspace_registration.side_effect = lambda receipt, *a: receipt
    project, service = mock.Mock(), mock.Mock()
    service.status.return_value = status
    project.replay_service.return_value.find_current_workspace_source_head.return_value = None
    project.register_workspace.return_value = {"knowledge": {"knowledgeId": KNOWLEDGE, "analysisSha256": "e" * 64},
        "flowchartArtifacts": [{"path": "kept.mmd"}], "flowGraphArtifacts": [{"path": "kept.json"}]}
    monkeypatch.setattr(project_restore, "_host_module", lambda: host)
    monkeypatch.setattr(shader_final_publication, "recover_finished_binding", lambda *a: FINISHED)
    lease = mock.Mock(side_effect=AssertionError("watcher already owns source lease"))
    monkeypatch.setattr(server, "WorkspaceSourceTransactionLease", lease)
    result = project_restore._register_applied_workspace_with_readback(project, service, tmp_path, status)
    project.register_workspace.assert_called_once_with(status, include_explanatory=True)
    lease.assert_not_called()
    assert result["recoveredCurrentReview"] is True
    assert result["knowledge"]["analysisSha256"] == "e" * 64


@pytest.mark.parametrize("change", ["", "source", "revision", "candidate", "review", "missing-sidecar", "stale-sidecar"])
def test_retry_preserves_only_current_complete_review_and_bound_documents(monkeypatch, tmp_path, change):
    from src.shader_workspace.explanations import BINDING_SCHEMA
    root = tmp_path / "旧项目名称" / "源码目录"
    ai = root / "ai"
    ai.mkdir(parents=True)
    source_path = root / "自定义着色器.hlsl"
    source_path.write_bytes(b"float4 main():SV_Target { return 1; }")
    source = hashlib.sha256(source_path.read_bytes()).hexdigest()
    status = {"workspacePath": str(root), "revision": 7, "stage": "fragment", "entryPoint": "main",
              "baseline": {"rawSha256": "e" * 64},
              "source": {"path": str(source_path), "currentSha256": source},
              "latestCompiled": {"rawSha256": CANDIDATE, "sourceSha256": source}}
    final = {"complete": True, "sourceSha256": source, "revision": 7, "candidateRawSha256": CANDIDATE}
    if change == "source":
        final["sourceSha256"] = "1" * 64
    elif change == "revision":
        final["revision"] = 8
    elif change == "candidate":
        final["candidateRawSha256"] = "2" * 64
    (ai / "organize-code-gate.json").write_text(json.dumps({"finishedCurrentDraft": True,
        "publicationContract": "validated-source", "organizationId": "a" * 32, "finalBinding": final}))
    document = ai / "analysis.md"
    document.write_bytes("旧项目分析\n".encode("utf-8"))
    if change != "missing-sidecar":
        (ai / "analysis.md.source-binding.json").write_text(json.dumps({"schema": BINDING_SCHEMA,
            "artifactSha256": hashlib.sha256(document.read_bytes()).hexdigest(),
            "sourcePath": str(source_path.resolve()), "sourceSha256": "9" * 64 if change == "stale-sidecar" else source,
            "stage": "fragment", "entryPoint": "main", "originalRawSha256": "e" * 64}))
    monkeypatch.setattr("src.shader_organization_review.publication_review", lambda *a: {
        "ready": change != "review", "sourceSha256": source})
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = shader_final_publication.recover_finished_binding(root, status)
    assert (result is not None) is (change == "")
    if result:
        assert result["sourceSha256"] == source and result["expectedRevision"] == 7
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert source_path.is_file()  # Never rename/migrate the user's real source.
