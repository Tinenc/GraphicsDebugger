"""Generated source must be validated delivery, never just a successful text write."""
import hashlib
import json
from pathlib import Path
from unittest import mock

import pytest

from src import server
from test_shader_host_ide_finish import work


def _captured(work, *, exact_code=False):
    original = work.root / "captured.bin"
    original.write_bytes(b"captured executable")
    work.service.original_raw_sha256 = hashlib.sha256(original.read_bytes()).hexdigest()
    read_status = work.service.status
    def status(path):
        value = read_status(path)
        value["baseline"].update(path="captured.bin", codeSha256=(
            work.service.baseline_code_sha if exact_code else "a" * 64))
        return value
    work.service.status = status
    return original


def _observation(baseline, candidate, *, accepted=True):
    return dict(schema="renderdoc-ai-shader-organization-observation/v1",
                scope="captured-event-output", accepted=accepted,
                semanticEquivalenceVerified=False, replayRestored=True,
                metrics={"outputs": []}, tolerances={}, sampleCount=12,
                baselineSourceSha256=baseline["sourceSha256"],
                candidateSourceSha256=candidate["sourceSha256"],
                baselineCandidateRawSha256=baseline["rawSha256"],
                candidateRawSha256=candidate["rawSha256"], reason="numeric mismatch" if not accepted else "")


def test_reconstructed_delivery_compares_captured_binary_not_recompiled_source(work):
    original = _captured(work)
    seen = []
    def compare(path, baseline, candidate):
        seen.append((baseline, candidate))
        assert Path(baseline["candidateBlobPath"]) == original
        assert baseline["sourceSha256"] is None
        return _observation(baseline, candidate)
    work.manager._compare_candidates = compare
    with mock.patch.object(work.service, "compile", wraps=work.service.compile) as compile_source:
        delivered = work.manager.validate_source_delivery(str(work.root))
    assert delivered["complete"] and delivered["readyForApply"] and not delivered["applied"]
    assert compile_source.call_count == 1 and len(seen) == 1
    assert work.service.source_path.read_bytes() == work.original


def test_exact_captured_code_reuses_compile_without_unneeded_replay_compare(work):
    _captured(work, exact_code=True)
    work.manager._compare_candidates = mock.Mock(side_effect=AssertionError("identical code needs no A/B"))
    with mock.patch.object(work.service, "compile", wraps=work.service.compile) as compile_source:
        first = work.manager.validate_source_delivery(str(work.root))
        second = work.manager.validate_source_delivery(str(work.root))
    assert first["complete"] and second["complete"] and second["compileReused"]
    assert compile_source.call_count == 1


@pytest.mark.parametrize("failure", ["compile", "numeric", "include-change"])
def test_failed_delivery_preserves_source_and_names_repair_step(work, failure):
    _captured(work)
    work.manager._compare_candidates = lambda path, baseline, candidate: _observation(
        baseline, candidate, accepted=failure != "numeric")
    if failure == "compile":
        work.service.compile = mock.Mock(side_effect=RuntimeError("missing symbol at line 12"))
    elif failure == "include-change":
        read_status = work.service.status
        def status(path):
            value = read_status(path)
            value["sourceDependenciesCurrent"] = False
            return value
        work.service.status = status
    delivered = work.manager.validate_source_delivery(str(work.root))
    assert not delivered["complete"] and not delivered["readyForApply"]
    assert delivered["repairRequired"] and delivered["nextAction"]
    assert work.service.source_path.read_bytes() == work.original
    if failure == "numeric":
        assert delivered["observation"]["sampleCount"] == 12
        assert delivered["compileValidated"] and delivered["abiValidated"]


@pytest.mark.parametrize("ready,apply_requested", [(True, False), (False, True), (True, True)])
def test_default_decompile_delivery_controls_apply_after_validation(monkeypatch, ready, apply_requested):
    service, manager = mock.Mock(), mock.Mock()
    service.reuse_pristine_decompiled_source.return_value = dict(sourceSha256="a" * 64)
    delivery = dict(schema="bread-shader-delivery/v1", complete=ready, readyForApply=ready,
                    repairRequired=not ready, applied=False, expectedRevision=3,
                    expectedCandidateRawSha256="b" * 64)
    manager.validate_source_delivery.return_value = delivery
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: service)
    monkeypatch.setattr(server, "_shader_organization_manager", lambda: manager)
    apply = mock.Mock(return_value=json.dumps({"applied": True, "replacement": {"replacementId": "c" * 32}}))
    monkeypatch.setattr(server, "apply_shader_workspace_shared_resource", apply)
    result = json.loads(server.decompile_shader_workspace("workspace", apply_shared_resource=apply_requested))
    assert result["ok"] is ready and result["delivery"]["complete"] is ready
    assert apply.call_count == int(ready and apply_requested)
    manager.validate_source_delivery.assert_called_once_with("workspace", expected_source_sha256="a" * 64)
    assert result["renderDocReplacement"]["applied"] == (ready and apply_requested)


def test_reference_and_explicit_source_only_decompile_never_validate(monkeypatch):
    service = mock.Mock()
    service.decompile_reference.return_value = {}
    service.reuse_pristine_decompiled_source.return_value = {}
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: service)
    monkeypatch.setattr(server, "_shader_organization_manager", mock.Mock(side_effect=AssertionError("no validation")))
    reference = json.loads(server.decompile_shader_workspace("workspace", reference_only=True))
    assert reference["operation"] == "decompile-reference"
    draft = json.loads(server.decompile_shader_workspace("workspace", validate_for_apply=False))
    assert draft["ok"] and not draft["delivery"]["complete"] and not draft["delivery"]["readyForApply"]


def test_decompile_cannot_validate_unreviewed_active_organization(monkeypatch):
    service = mock.Mock()
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: service)
    monkeypatch.setattr(server, "_require_organization_source_review", mock.Mock(
        side_effect=server.ShaderOrganizationError("whole-source review pending")))
    validate = mock.Mock()
    monkeypatch.setattr(server, "_shader_source_delivery_result", validate)
    result = json.loads(server.decompile_shader_workspace("workspace", apply_shared_resource=True))
    assert result["ok"] is False and result["delivery"]["readyForApply"] is False
    service.reuse_pristine_decompiled_source.assert_not_called()
    service.decompile.assert_not_called()
    validate.assert_not_called()


def test_source_delivery_does_not_claim_to_close_reviewed_organization(monkeypatch):
    organization_id = "d" * 32
    monkeypatch.setattr(server, "_require_organization_source_review", lambda path: organization_id)
    monkeypatch.setattr(server, "_shader_source_delivery_result", lambda *args: {
        "complete": True, "readyForApply": True, "applied": False})
    result = json.loads(server.validate_shader_workspace_delivery("workspace"))
    assert result["ok"] and result["delivery"]["organizationStillOpen"]
    assert result["delivery"]["organizationId"] == organization_id
    assert result["delivery"]["nextAction"] == "finish-reviewed-draft"


@pytest.mark.parametrize("apply_outcome", [{"applied": False, "error": "replacement mismatch"},
                                          {"applied": True}, RuntimeError("apply failed")])
def test_finish_apply_failure_cannot_report_success(monkeypatch, apply_outcome):
    monkeypatch.setattr("src.shader_organization_review.publication_review", lambda *args: {"ready": True, "sourceSha256": "a" * 64})
    manager = mock.Mock()
    manager.finish_draft.return_value = dict(ok=True, wholeSourceValidated=True, applied=False,
        expectedRevision=4, expectedCandidateRawSha256="b" * 64)
    monkeypatch.setattr(server, "_shader_organization_manager", lambda: manager)
    apply = mock.Mock(side_effect=apply_outcome if isinstance(apply_outcome, Exception) else None,
                      return_value=json.dumps(apply_outcome) if isinstance(apply_outcome, dict) else None)
    monkeypatch.setattr(server, "apply_shader_workspace_shared_resource", apply)
    result = json.loads(server.finish_shader_workspace_organization("workspace", "f" * 32,
                        validate_for_apply=False, apply_shared_resource=True))
    manager.finish_draft.assert_called_once()
    manager.save_draft.assert_not_called()
    assert not result["ok"] and not result["delivery"]["complete"]
    assert result["recoverable"] and result["delivery"]["repairRequired"]


def test_reference_and_source_only_paths_do_not_issue_cleanup_replay(monkeypatch):
    from src import host_runtime
    monkeypatch.setattr(host_runtime, "memory_snapshot", lambda: {})
    monkeypatch.setattr(host_runtime, "_append_trace", lambda *args: None)
    cleanup = mock.Mock()
    monkeypatch.setattr(host_runtime, "_auto_release_after_tool", cleanup)
    @host_runtime.instrument_tool_memory
    def decompile_shader_workspace(path, reference_only=False, validate_for_apply=True, apply_shared_resource=False):
        return path
    decompile_shader_workspace("source", reference_only=True)
    decompile_shader_workspace("source", validate_for_apply=False)
    cleanup.assert_not_called()
    decompile_shader_workspace("source")
    assert cleanup.call_count == 1


@pytest.mark.parametrize("arguments,touches", [({}, True), ({"validate_for_apply": None}, True),
    ({"publication_mode": "checkpoint"}, False), ({"validate_for_apply": False}, False),
    ({"validate_for_apply": False, "apply_shared_resource": True}, True),
    ({"validate_for_apply": False, "start_live_edit": True}, True)])
def test_finish_cleanup_obeys_effective_validation_default(monkeypatch, arguments, touches):
    from src import host_runtime
    monkeypatch.setattr(host_runtime, "memory_snapshot", lambda: {})
    monkeypatch.setattr(host_runtime, "_append_trace", lambda *args: None)
    cleanup = mock.Mock()
    monkeypatch.setattr(host_runtime, "_auto_release_after_tool", cleanup)
    @host_runtime.instrument_tool_memory
    def finish_shader_workspace_organization(path, validate_for_apply=None, publication_mode="final",
                                              apply_shared_resource=False, start_live_edit=False):
        return path
    finish_shader_workspace_organization("workspace", **arguments)
    assert cleanup.call_count == int(touches)


@pytest.mark.parametrize("tool", ["decompile_shader_workspace", "validate_shader_workspace_delivery"])
def test_delivery_apply_is_tracked_for_abandoned_task_cleanup(tmp_path, tool):
    from src.task_report import TaskReportStore, read_task_report_sidecars
    store = TaskReportStore("delivery-task", tmp_path)
    store.record_result(tool, {"ok": True, "delivery": {"apply": {
        "applied": True, "replacement": {"replacementId": "a" * 32}}}},
        arguments={"workspace_path": "D:/project/workspace"})
    pending = read_task_report_sidecars(tmp_path, "delivery-task")[1]["pendingShaderWork"]
    assert pending["D:/project/workspace"]["delivery"]["replacementId"] == "a" * 32


@pytest.mark.parametrize("large_evidence", [False, True])
def test_decompile_production_envelope_retains_apply_identity_for_cleanup(tmp_path, monkeypatch, large_evidence):
    from src.task_report import TaskReportStore, read_task_report_sidecars
    monkeypatch.setattr(server, "_asset_tool_output_root", lambda: tmp_path / "evidence")
    delivery = {"complete": True, "readyForApply": True, "repairRequired": False,
        "applied": True, "apply": {"applied": True, "replacement": {"replacementId": "b" * 32}}}
    if large_evidence:
        delivery["observation"] = {"diagnostics": "numeric evidence " * 10000}
    result = json.loads(server._shader_workspace_response("decompile", {
        "workspacePath": "D:/project/workspace", "sourceSha256": "a" * 64, "delivery": delivery}))
    assert result["ok"] and result["delivery"]["complete"]
    if large_evidence:
        assert result["completeContent"]["reader"]["tool"] == "read_evidence_file"
    store = TaskReportStore("envelope-task", tmp_path)
    store.record_result("decompile_shader_workspace", result,
                        arguments={"workspace_path": "D:/project/workspace"})
    pending = read_task_report_sidecars(tmp_path, "envelope-task")[1]["pendingShaderWork"]
    assert pending["D:/project/workspace"]["delivery"]["replacementId"] == "b" * 32
