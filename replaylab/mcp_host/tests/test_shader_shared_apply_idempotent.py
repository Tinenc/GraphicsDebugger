"""Direct apply observes exact watcher delivery without re-entering its writer."""
import copy
import hashlib
import json
from types import SimpleNamespace
from unittest import mock

import pytest

from src import server


@pytest.fixture
def delivered_pair(monkeypatch, tmp_path):
    source = tmp_path / "shader.hlsl"
    candidate = tmp_path / "shader.dxil"
    source.write_bytes(b"float4 MainPS() : SV_Target { return 1; }\n")
    candidate.write_bytes(b"exact compiled native candidate")
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    candidate_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
    status = {
        "workspaceId": "1" * 32, "workspacePath": str(tmp_path), "revision": 110,
        "capture": {"captureId": "2" * 64, "capturePath": str(tmp_path / "frame.rdc"), "api": "GraphicsAPI.D3D12"},
        "shader": {"resourceId": 1000000000000000723, "rawSha256": "3" * 64},
        "baseline": {"rawSha256": "3" * 64, "abiSha256": "4" * 64},
        "stage": "fragment", "entryPoint": "MainPS", "targetProfile": {"profile": "ps_6_6"},
        "representation": {"lane": "executable-editable", "compileEligible": True},
        "source": {"path": str(source), "currentSha256": source_sha, "present": True},
        "latestCompiled": {"revision": 110, "rawSha256": candidate_sha, "sourceSha256": source_sha,
                           "kind": "DXIL", "candidateBlobPath": str(candidate),
                           "abiGate": {"status": "compatible", "requestId": "a" * 64, "reportSha256": "b" * 64}},
    }
    # This is the actual bounded Bridge inventory shape: rows omit the
    # capture/session envelope and the original ABI request/report fields.
    row = {
        "replacementId": "5" * 32, "operationId": "6" * 32, "workspaceId": "1" * 32,
        "revision": 110, "anchorEventId": 11371, "stage": "fragment", "entryPoint": "MainPS",
        "api": "d3d12", "encoding": "dxil", "originalResourceId": "1000000000000000723",
        "originalRawSha256": "3" * 64, "candidateRawSha256": candidate_sha, "physicalAbiSha256": "4" * 64,
        "targetResourceId": 1000000000000002479, "resourceScoped": True, "eventScoped": False,
        "allOriginalResourceUsesAffected": True, "originalShaderPreserved": True,
        "postApplyValidationFailed": False,
    }
    inventory = {"schema": "renderdoc-ultra-shared-shader-replacement-list/v1", "schemaVersion": 1,
                 "captureId": "2" * 64, "captureSessionId": "7" * 32,
                 "count": 1, "replacements": [row], "originalShadersPreserved": True, "replayMutation": False}
    context = {"capture": {"path": str(tmp_path / "frame.rdc"), "api": "D3D12", "captureSessionId": "7" * 32}}
    service = mock.Mock()
    service.status.side_effect = lambda unused: copy.deepcopy(status)
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: service)
    live_context = mock.Mock(side_effect=lambda **unused: copy.deepcopy(context))
    monkeypatch.setattr(server, "_active_live_context", live_context)
    ipc_call = mock.Mock(side_effect=lambda method, params, timeout: copy.deepcopy(inventory))
    monkeypatch.setattr(server.ipc, "call", ipc_call)
    monkeypatch.setattr(server, "_SCOPED_CAPTURE_SESSION_ID", "7" * 32)
    fresh = mock.Mock(side_effect=RuntimeError("existing protected apply path"))
    monkeypatch.setattr(server, "_fresh_shader_candidate_validation", fresh)
    guard = mock.Mock(side_effect=AssertionError("no writer admission for readback"))
    monkeypatch.setattr(server, "_require_workspace_live_edit_quiescent", guard)
    remember = mock.Mock(side_effect=AssertionError("no new effect or publication"))
    monkeypatch.setattr(server, "_remember_shared_shader_replacement", remember)
    monkeypatch.setattr(server, "_remember_route_success", remember)
    return SimpleNamespace(status=status, row=row, inventory=inventory, context=context,
        service=service, fresh=fresh, guard=guard, remember=remember, ipc_call=ipc_call,
        live_context=live_context, source=source, candidate=candidate, workspace=tmp_path)


@pytest.mark.parametrize("physical_revision", [109, 110])
def test_already_applied_current_pair_returns_truthful_readback_without_fresh_abi_or_apply(delivered_pair, physical_revision):
    case = delivered_pair
    case.row["revision"] = physical_revision
    result = json.loads(server.apply_shader_workspace_shared_resource(
        str(case.workspace), 110, case.status["latestCompiled"]["rawSha256"], "8" * 32))
    assert result["applied"] and result["idempotent"]
    assert result["appliedByThisCall"] is False and result["replayMutation"] is False
    assert result["binding"]["revision"] == 110
    assert result["binding"]["sourceSha256"] == case.status["source"]["currentSha256"]
    assert result["replacement"] == case.row
    assert result["replacement"]["operationId"] == "6" * 32
    assert result["validation"]["physicalCandidateRevision"] == physical_revision
    assert result["validation"]["freshPhysicalValidationPerformed"] is False
    assert "requestId" not in result["validation"]
    assert "projectSync" not in result
    case.fresh.assert_not_called(); case.guard.assert_not_called(); case.remember.assert_not_called()
    assert [call.args[0] for call in case.ipc_call.call_args_list] == ["get_shared_shader_replacements"]


def test_failed_installed_candidate_is_neither_idempotent_success_nor_blind_retry(delivered_pair):
    case = delivered_pair
    case.row["postApplyValidationFailed"] = True
    result = json.loads(server.apply_shader_workspace_shared_resource(
        str(case.workspace), 110, case.status["latestCompiled"]["rawSha256"], "8" * 32))
    assert "failed post-apply validation" in result["error"]
    assert not result.get("applied")
    case.fresh.assert_not_called()
    case.guard.assert_not_called()
    case.remember.assert_not_called()
    assert [call.args[0] for call in case.ipc_call.call_args_list] == ["get_shared_shader_replacements"]


def test_same_original_at_another_requested_eid_reuses_shared_effect_without_rewriting_anchor(delivered_pair, monkeypatch):
    case = delivered_pair
    actual_binding = {"stage": "fragment", "entryPoint": "MainPS",
        "resourceId": case.status["shader"]["resourceId"], "rawSha256": case.status["baseline"]["rawSha256"]}
    bound = mock.Mock(return_value=actual_binding)
    monkeypatch.setattr(server, "_active_bound_shader", bound)
    result = json.loads(server.apply_shader_workspace_shared_resource(str(case.workspace),
        110, case.status["latestCompiled"]["rawSha256"], "8" * 32, expected_anchor_event_id=11384))
    assert result["applied"] and result["idempotent"] and result["appliedByThisCall"] is False
    assert result["replayMutation"] is False
    assert result["replacement"]["anchorEventId"] == 11371
    assert result["replacement"]["operationId"] == "6" * 32
    assert result["requestedAnchorEventId"] == 11384
    assert result["requestedBinding"] == {"eventId": 11384, "stage": "fragment", "entryPoint": "MainPS",
        "originalResourceId": case.status["shader"]["resourceId"],
        "originalRawSha256": case.status["baseline"]["rawSha256"], "source": "current-capture-binding"}
    bound.assert_called_once_with(case.context, 11384, "fragment")
    case.fresh.assert_not_called(); case.guard.assert_not_called(); case.remember.assert_not_called()
    assert [call.args[0] for call in case.ipc_call.call_args_list] == ["get_shared_shader_replacements"]


@pytest.mark.parametrize("changed", ["resource", "original", "entry", "stage", "missing", "unavailable"])
def test_other_eid_must_actually_bind_same_captured_original_before_readback(delivered_pair, monkeypatch, changed):
    case = delivered_pair
    original = {"stage": "fragment", "entryPoint": "MainPS",
        "resourceId": case.status["shader"]["resourceId"], "rawSha256": case.status["baseline"]["rawSha256"]}
    if changed == "resource": original["resourceId"] += 1
    elif changed == "original": original["rawSha256"] = "9" * 64
    elif changed == "entry": original["entryPoint"] = "OtherPS"
    elif changed == "stage": original["stage"] = "vertex"
    elif changed == "missing": original = None
    bound = mock.Mock(return_value=original,
        side_effect=RuntimeError("actual original unavailable") if changed == "unavailable" else None)
    monkeypatch.setattr(server, "_active_bound_shader", bound)
    result = json.loads(server.apply_shader_workspace_shared_resource(str(case.workspace),
        110, case.status["latestCompiled"]["rawSha256"], "8" * 32, expected_anchor_event_id=11384))
    assert result["error"] == "existing protected apply path"
    assert not result.get("applied")
    case.fresh.assert_called_once()
    assert "requestedBinding" not in result


@pytest.mark.parametrize("changed", [
    "workspace", "resource", "resource-float", "original", "candidate", "stage", "entry", "api", "encoding",
    "physical-abi", "future-revision", "invalid-revision", "scope", "preserved", "source-binding", "source-file",
    "candidate-file", "capture-path", "capture-api", "session", "inventory-session", "inventory-schema", "inventory-count", "duplicate",
])
def test_nonexact_or_stale_readback_keeps_existing_validation_and_owner_path(delivered_pair, changed):
    case = delivered_pair
    if changed == "workspace": case.row["workspaceId"] = "9" * 32
    elif changed == "resource": case.row["originalResourceId"] = 1000000000000000755
    elif changed == "resource-float": case.row["originalResourceId"] = float(1000000000000000723)
    elif changed == "original": case.row["originalRawSha256"] = "9" * 64
    elif changed == "candidate": case.row["candidateRawSha256"] = "9" * 64
    elif changed == "stage": case.row["stage"] = "vertex"
    elif changed == "entry": case.row["entryPoint"] = "OtherPS"
    elif changed == "api": case.row["api"] = "vulkan"
    elif changed == "encoding": case.row["encoding"] = "spirv"
    elif changed == "physical-abi": case.row["physicalAbiSha256"] = "9" * 64
    elif changed == "future-revision": case.row["revision"] = 111
    elif changed == "invalid-revision": case.row["revision"] = True
    elif changed == "scope": case.row["eventScoped"] = True
    elif changed == "preserved": case.row["originalShaderPreserved"] = False
    elif changed == "source-binding": case.status["latestCompiled"]["sourceSha256"] = "9" * 64
    elif changed == "source-file": case.source.write_bytes(b"new uncompiled source")
    elif changed == "candidate-file": case.candidate.write_bytes(b"changed native bytes")
    elif changed == "capture-path": case.context["capture"]["path"] = str(case.workspace / "other.rdc")
    elif changed == "capture-api": case.context["capture"]["api"] = "Vulkan"
    elif changed == "session": case.context["capture"]["captureSessionId"] = "9" * 32
    elif changed == "inventory-session": case.inventory["captureSessionId"] = "9" * 32
    elif changed == "inventory-schema": case.inventory["schema"] = "other/v1"
    elif changed == "inventory-count": case.inventory["count"] = 2
    else:
        case.inventory["replacements"].append(copy.deepcopy(case.row))
        case.inventory["count"] = 2
    result = json.loads(server.apply_shader_workspace_shared_resource(
        str(case.workspace), 110, case.status["latestCompiled"]["rawSha256"], "8" * 32))
    assert result["error"] == "existing protected apply path"
    case.fresh.assert_called_once()
    assert not result.get("applied")


@pytest.mark.parametrize("race", ["source", "candidate", "revision", "session"])
def test_changes_during_readback_are_not_reported_as_current(delivered_pair, race):
    case = delivered_pair
    def query(*unused_args, **unused_kwargs):
        if race == "source": case.source.write_bytes(b"concurrent editor save")
        elif race == "candidate": case.candidate.write_bytes(b"concurrent candidate publication")
        elif race == "revision":
            case.status["revision"] = 111
            case.status["latestCompiled"]["revision"] = 111
        else: case.context["capture"]["captureSessionId"] = "9" * 32
        return copy.deepcopy(case.inventory)
    case.ipc_call.side_effect = query
    result = json.loads(server.apply_shader_workspace_shared_resource(
        str(case.workspace), 110, case.status["latestCompiled"]["rawSha256"], "8" * 32))
    assert result["error"] == "existing protected apply path"
    case.fresh.assert_called_once()


@pytest.mark.parametrize("changed", ["old-request", "new-request", "other-hash", "other-anchor", "boolean-anchor", "bound-session"])
def test_explicit_request_and_frozen_capture_binding_remain_exact(delivered_pair, changed):
    case = delivered_pair
    revision, candidate, anchor = 110, case.status["latestCompiled"]["rawSha256"], None
    if changed == "old-request": revision = 109
    elif changed == "new-request": revision = 111
    elif changed == "other-hash": candidate = "9" * 64
    elif changed == "other-anchor": anchor = 11378
    elif changed == "boolean-anchor": anchor = True
    from src.ipc_client import bind_direct_tool_call
    session = "9" * 32 if changed == "bound-session" else "7" * 32
    with bind_direct_tool_call("exact-instance", session):
        result = json.loads(server.apply_shader_workspace_shared_resource(
            str(case.workspace), revision, candidate, "8" * 32, expected_anchor_event_id=anchor))
    assert result["error"] == "existing protected apply path"
    case.fresh.assert_called_once()


@pytest.mark.parametrize("owner", ["same-workspace-changed-candidate", "different-workspace"])
def test_nonexact_active_writer_still_hits_the_existing_quiescent_guard(delivered_pair, owner):
    case = delivered_pair
    if owner == "different-workspace": case.row["workspaceId"] = "9" * 32
    else: case.row["candidateRawSha256"] = "9" * 64
    binding = {"workspaceId": case.status["workspaceId"], "captureId": "2" * 64,
        "revision": 110, "candidateRawSha256": case.status["latestCompiled"]["rawSha256"],
        "baselineAbiSha256": "4" * 64}
    request = {"binding": binding, "requestId": "a" * 64, "params": {}}
    case.service.candidate_reflection_request.return_value = request
    case.fresh.side_effect = None
    case.fresh.return_value = (case.service, copy.deepcopy(case.status), request,
        {"replayAbiValidated": True, "abiReportSha256": "b" * 64})
    case.guard.side_effect = RuntimeError("stop the exact Shader live-edit session before applying its shared replacement")
    result = json.loads(server.apply_shader_workspace_shared_resource(
        str(case.workspace), 110, case.status["latestCompiled"]["rawSha256"], "8" * 32,
        expected_anchor_event_id=11371))
    assert "stop the exact Shader live-edit session" in result["error"]
    case.guard.assert_called_once()
    assert [call.args[0] for call in case.ipc_call.call_args_list] == [
        "get_shared_shader_replacements", "get_shared_shader_replacements"]
    case.remember.assert_not_called()


def _prepare_watcher_admission_race(case):
    binding = {"workspaceId": case.status["workspaceId"], "captureId": "2" * 64,
        "revision": 110, "candidateRawSha256": case.status["latestCompiled"]["rawSha256"],
        "baselineAbiSha256": "4" * 64}
    request = {"binding": binding, "requestId": "a" * 64, "params": {}}
    case.service.candidate_reflection_request.return_value = request
    case.fresh.side_effect = None
    case.fresh.return_value = (case.service, copy.deepcopy(case.status), request,
        {"replayAbiValidated": True, "abiReportSha256": "b" * 64})
    case.guard.side_effect = RuntimeError(
        "stop the exact Shader live-edit session before applying its shared replacement")
    empty_inventory = dict(copy.deepcopy(case.inventory), count=0, replacements=[])
    case.ipc_call.side_effect = [empty_inventory, copy.deepcopy(case.inventory)]


@pytest.mark.parametrize("explicit_pair", [False, True])
def test_watcher_finishing_during_validation_returns_existing_effect_at_admission(delivered_pair, explicit_pair):
    case = delivered_pair
    _prepare_watcher_admission_race(case)
    kwargs = {"expected_revision": 110,
              "expected_candidate_raw_sha256": case.status["latestCompiled"]["rawSha256"]} if explicit_pair else {}
    result = json.loads(server.apply_shader_workspace_shared_resource(
        str(case.workspace), operation_id="8" * 32, expected_anchor_event_id=11371, **kwargs))
    assert result["applied"] and result["idempotent"]
    assert result["appliedByThisCall"] is False and result["replayMutation"] is False
    assert result["replacement"] == case.row
    assert result["binding"]["sourceSha256"] == case.status["source"]["currentSha256"]
    assert result["validation"]["freshPhysicalValidationPerformed"] is False
    case.fresh.assert_called_once()
    case.guard.assert_called_once()
    case.remember.assert_not_called()
    assert [call.args[0] for call in case.ipc_call.call_args_list] == [
        "get_shared_shader_replacements", "get_shared_shader_replacements"]


@pytest.mark.parametrize("change", ["candidate", "workspace", "new-revision", "new-source", "readback-error"])
def test_nonexact_watcher_race_preserves_admission_error_without_another_apply(delivered_pair, change):
    case = delivered_pair
    _prepare_watcher_admission_race(case)
    empty_inventory = dict(copy.deepcopy(case.inventory), count=0, replacements=[])
    second = copy.deepcopy(case.inventory)
    if change == "candidate": second["replacements"][0]["candidateRawSha256"] = "9" * 64
    elif change == "workspace": second["replacements"][0]["workspaceId"] = "9" * 32
    elif change == "readback-error": second = RuntimeError("readback unavailable")
    elif change in {"new-revision", "new-source"}:
        def refuse_after_new_candidate(*unused_args, **unused_kwargs):
            if change == "new-revision":
                case.status["revision"] = 111
                case.status["latestCompiled"]["revision"] = 111
            else:
                case.source.write_bytes(b"float4 MainPS() : SV_Target { return 1; } // new source\n")
                source_sha = hashlib.sha256(case.source.read_bytes()).hexdigest()
                case.status["source"]["currentSha256"] = source_sha
                case.status["latestCompiled"]["sourceSha256"] = source_sha
            raise RuntimeError("stop the exact Shader live-edit session before applying its shared replacement")
        case.guard.side_effect = refuse_after_new_candidate
    case.ipc_call.side_effect = [empty_inventory, second]
    # Omitting an expected pair must still freeze the candidate validated by
    # this request, rather than adopting a later candidate during the retry.
    result = json.loads(server.apply_shader_workspace_shared_resource(
        str(case.workspace), operation_id="8" * 32, expected_anchor_event_id=11371))
    assert result["error"] == "stop the exact Shader live-edit session before applying its shared replacement"
    assert not result.get("applied")
    case.fresh.assert_called_once()
    case.guard.assert_called_once()
    case.remember.assert_not_called()
    assert all(call.args[0] == "get_shared_shader_replacements" for call in case.ipc_call.call_args_list)
