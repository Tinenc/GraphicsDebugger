"""Apply an explicitly saved editor candidate without the AI publication ABI gate.

Source/capture/owner identities are still bound. They are cheap transaction
checks, not claims about the candidate's interface or semantic equivalence.
"""
from __future__ import annotations

import copy
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Mapping


_ANCHORS: OrderedDict[tuple, int] = OrderedDict()
_GUARD = threading.Lock()


def _anchor_event(host: Any, status: Mapping[str, Any], session: str) -> int:
    capture, shader = status["capture"], status["shader"]
    key = (session, status["workspaceId"], capture.get("catalogSha256"),
           capture["catalogPath"], shader["shaderKey"], shader["resourceId"],
           status["baseline"]["rawSha256"], status["stage"], status["entryPoint"])
    with _GUARD:
        cached = _ANCHORS.get(key)
        if cached is not None:
            _ANCHORS.move_to_end(key)
            return cached
    uses = host.list_exact_shader_family_uses(capture["catalogPath"], shader["shaderKey"])
    if not uses:
        raise RuntimeError("The saved Shader has no captured use in its catalog")
    event = min(int(value) for value in uses)
    if event < 1:
        raise RuntimeError("The saved Shader has an invalid captured event")
    with _GUARD:
        _ANCHORS[key] = event
        _ANCHORS.move_to_end(key)
        while len(_ANCHORS) > 128:
            _ANCHORS.popitem(last=False)
    return event


def _check_response(host: Any, result: Any, binding: Mapping[str, Any],
                    session: str, original: int, event: int, operation: str) -> dict:
    if not isinstance(result, dict):
        raise RuntimeError("RenderDoc returned an invalid manual Shader replacement")
    expected = {
        "schema": "renderdoc-ultra-shared-shader-replacement/v1", "schemaVersion": 1,
        "captureId": binding["captureId"], "captureSessionId": session,
        "workspaceId": binding["workspaceId"], "revision": binding["revision"],
        "stage": binding["stage"], "entryPoint": binding["entryPoint"],
        "api": binding["api"], "encoding": binding["encoding"],
        "originalResourceId": original, "originalRawSha256": binding["baselineRawSha256"],
        "candidateRawSha256": binding["candidateRawSha256"], "anchorEventId": event,
        "operationId": operation, "resourceScoped": True, "eventScoped": False,
        "allOriginalResourceUsesAffected": True, "configured": True,
        "originalShaderPreserved": True, "singleOwner": True,
        "abiValidationMode": "manual-fast", "manualEdit": True,
        "validationScope": "manual-edit-unvalidated", "replayAbiValidated": False,
        "freshPhysicalAbiValidationPerformed": False, "postApplyAnchorVerified": False,
    }
    for name, value in expected.items():
        if result.get(name) != value:
            raise RuntimeError("Manual Shader replacement identity mismatch at %s" % name)
    if result.get("restoreValidationReceipt"):
        raise RuntimeError("Manual Shader replacement incorrectly claimed an ABI receipt")
    host._strict_lower_hex(result.get("replacementId"), 32, "replacementId")
    for name in ("idempotent", "metadataOnly", "targetBuiltOnce", "targetReused",
                 "operationCreatedReplacement", "supersededTargetFreed"):
        if type(result.get(name)) is not bool:
            raise RuntimeError("Manual Shader replacement omitted %s" % name)
    if type(result.get("targetResourceId")) is not int or result["targetResourceId"] <= 0:
        raise RuntimeError("Manual Shader replacement target is invalid")
    return result


def _remember(host: Any, result: dict, status: dict) -> None:
    # Keep rollback/occupancy discoverable without manufacturing an ABI lease
    # or publishing to the strict validated-pair cache.
    record = {key: copy.deepcopy(result.get(key)) for key in (
        "replacementId", "captureId", "captureSessionId", "workspaceId", "revision",
        "stage", "entryPoint", "api", "encoding", "originalResourceId",
        "originalRawSha256", "candidateRawSha256", "anchorEventId", "shaderFamilyId",
        "targetResourceId", "abiValidationMode", "validationScope", "manualEdit",
        "operationId", "physicalAbiSha256", "sourceSha256", "targetProfile",
        "replayAbiValidated")}
    record["workspacePath"] = status["workspacePath"]
    with host._SHADER_OVERRIDE_GUARD:
        for token, previous in list(host._SHARED_SHADER_REPLACEMENT_RECORDS.items()):
            if (previous.get("captureSessionId") == result["captureSessionId"]
                    and previous.get("originalResourceId") == result["originalResourceId"]):
                host._SHARED_SHADER_REPLACEMENT_RECORDS.pop(token, None)
        host._SHARED_SHADER_REPLACEMENT_RECORDS[result["replacementId"]] = record


def _recover_operation(host: Any, status: dict, session: str, operation: str, error: Exception) -> None:
    """Resolve only this operation after a lost or malformed response."""
    from .project_restore import _recover_uncertain_workspace_apply, RestoreRollbackFailed

    try:
        recovered = _recover_uncertain_workspace_apply(host, status, operation, session)
        with host._SHADER_OVERRIDE_GUARD:
            token = recovered.get("replacementId")
            current = host._SHARED_SHADER_REPLACEMENT_RECORDS.get(token)
            if current and current.get("operationId") == operation:
                host._SHARED_SHADER_REPLACEMENT_RECORDS.pop(token, None)
    except Exception as recovery_error:
        raise RestoreRollbackFailed("Manual Shader operation needs recovery after %s: %s" % (
            type(error).__name__, str(recovery_error)[:350])) from error


def apply_manual_candidate(host: Any, workspace_path: str, revision: int,
                           candidate_raw_sha256: str, *,
                           expected_anchor_event_id: int | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    expected = host._normalise_expected_shader_candidate(revision, candidate_raw_sha256)
    service = host._shader_workspace_service()
    status = service.status(workspace_path)
    source_sha = host._require_shader_candidate_binding(status, expected)
    request = service.manual_candidate_apply_request(workspace_path)
    binding = request["binding"]
    if any(binding.get(key) != value for key, value in {
        "workspaceId": status["workspaceId"], "captureId": status["capture"]["captureId"],
        "revision": revision, "candidateRawSha256": candidate_raw_sha256,
        "sourceSha256": source_sha, "baselineRawSha256": status["baseline"]["rawSha256"],
        "stage": status["stage"], "entryPoint": status["entryPoint"],
    }.items()):
        raise RuntimeError("The saved Shader changed before manual apply")
    rebound = host._ensure_workspace_capture_catalog_identity(workspace_path, status=status)
    session = host._strict_lower_hex(rebound.get("captureSessionId"), 32, "captureSessionId")
    host._sync_shader_override_capture(session)
    if expected_anchor_event_id is not None:
        if type(expected_anchor_event_id) is not int or not 0 < expected_anchor_event_id <= 0xFFFFFFFF:
            raise RuntimeError("The saved Shader has an invalid current restore event")
        # The restore's current inventory owns this anchor. The Bridge still
        # proves its exact original resource/bytes immediately before apply.
        event = expected_anchor_event_id
    else:
        event = _anchor_event(host, status, session)
    original = status["shader"]["resourceId"]
    operation = uuid.uuid4().hex
    host._require_workspace_live_edit_quiescent(
        workspace_path, action="applying", expected_candidate=expected)
    params = dict(request["params"], expected_capture_id=binding["captureId"],
        expected_capture_session_id=session, event_id=event,
        expected_original_resource_id=original,
        expected_original_sha256=binding["baselineRawSha256"],
        workspace_id=binding["workspaceId"], revision=revision,
        expected_source_sha256=source_sha, abi_validation_mode="manual-fast",
        expected_profile=binding.get("targetProfile"), operation_id=operation)
    prepared = time.perf_counter()
    try:
        raw = host.ipc.call("apply_shader_candidate_shared_resource", params, timeout=300.0)
    except Exception as error:
        from .ipc_client import IPCTransportError, IPCBridgeBusyError
        if isinstance(error, IPCTransportError) and not isinstance(error, IPCBridgeBusyError):
            _recover_operation(host, status, session, operation, error)
            raise
        if "owned by a different workspace" not in str(error):
            raise
        handoff = host._takeover_idle_foreign_shared_shader_owner(ipc=host.ipc,
            capture_id=binding["captureId"], capture_session_id=session,
            original_resource_id=original, claimant_workspace_id=binding["workspaceId"])
        if not (handoff.get("released") is True or handoff.get("retryApply") is True):
            raise RuntimeError("The captured Shader is still owned by another active editor") from error
        if handoff.get("expectedOwner") is not None:
            params["expected_owner"] = handoff["expectedOwner"]
        try:
            raw = host.ipc.call("apply_shader_candidate_shared_resource", params, timeout=300.0)
        except Exception as handoff_error:
            if isinstance(handoff_error, IPCTransportError) and not isinstance(handoff_error, IPCBridgeBusyError):
                _recover_operation(host, status, session, operation, handoff_error)
            raise
    try:
        applied = _check_response(host, raw, binding, session, original, event, operation)
    except Exception as response_error:
        _recover_operation(host, status, session, operation, response_error)
        raise
    finished = time.perf_counter()
    _remember(host, applied, status)
    return {
        "schema": "renderdoc-ultra-shader-shared-resource-apply-mcp/v1",
        "operation": "apply-shared-resource", "operationId": operation,
        "binding": dict(binding), "replacement": applied, "scope": "captured-resource",
        "applied": True, "idempotent": applied["idempotent"],
        "manualEdit": True, "abiValidationMode": "manual-fast",
        "validation": {"compatible": False, "requiresReplayAbiValidation": False,
            "replayAbiValidated": False, "semanticEquivalenceVerified": False,
            "manualEdit": True, "abiValidationMode": "manual-fast",
            "validationScope": "manual-edit-unvalidated",
            "preliminaryReplayReflectionSkipped": True,
            "freshPhysicalValidationPerformed": False,
            "freshLiveCaptureValidationPerformed": False,
            "captureSessionId": session},
        "sourceIdentity": {"sourceSha256": source_sha,
            "sourceDependenciesDigest": binding.get("sourceDependenciesDigest", "")},
        "timingsMs": {"prepare": (prepared - started) * 1000,
            "bridgeApply": (finished - prepared) * 1000,
            "total": (time.perf_counter() - started) * 1000},
        "rollback": {"tool": "rollback_shader_workspace_shared_resource",
            "workspacePath": status["workspacePath"], "replacementId": applied["replacementId"]},
    }
