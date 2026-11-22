"""The real live callback/public apply boundary admits only its exact worker.

Replay and reflection are controlled fixtures; no application or user files
are accessed. The production apply guard and response validator stay active.
"""
import copy
import json
from types import SimpleNamespace
from unittest import mock

import pytest

from src import internal_tool_service, server, shader_live_edit
from src.ipc_client import IPCBridgeResponseError, IPCBridgeTimeoutError, bind_direct_tool_call
from src.shader_live_edit import ShaderLiveEditManager
from test_shader_live_edit import _FakeWatchManager, _wait_until


def owner_case(monkeypatch, tmp_path, api="vulkan", fault="", legacy_topology=False):
    # These cases exercise the explicit strict API's ownership gate. Ordinary
    # editor saves use the separately tested manual transaction.
    monkeypatch.setattr(server, "_shader_live_edit_apply_candidate",
                        server._shader_live_edit_apply_candidate_with_abi)
    binding = {"instanceId": "owner-gui", "captureSessionId": "7" * 32,
               "capturePath": str(tmp_path / "frame.rdc")}
    revision = 7 if fault == "wrong-revision" else 6
    candidate = "9" * 64 if fault == "wrong-hash" else "6" * 64
    encoding = {"d3d11": "dxbc", "d3d12": "dxil", "vulkan": "spirv", "opengl": "glsl"}[api]
    status = {"workspaceId": "1" * 32, "workspacePath": str(tmp_path), "revision": revision,
        "capture": {"captureId": "2" * 64, "capturePath": binding["capturePath"],
                    "catalogPath": str(tmp_path / "catalog.json"), "api": api},
        "shader": {"resourceId": 77, "shaderKey": "controlled", "rawSha256": "3" * 64},
        "stage": "fragment", "entryPoint": "main",
        "baseline": {"abiSha256": "4" * 64, "rawSha256": "3" * 64},
        "representation": {"lane": "executable-editable", "compileEligible": True},
        "source": {"currentSha256": "c" * 64},
        "latestCompiled": {"revision": revision, "rawSha256": candidate, "sourceSha256": "c" * 64,
                           "abiGate": {"status": "compatible", "requestId": "a" * 64, "reportSha256": "b" * 64}}}
    request = {"binding": {"workspaceId": status["workspaceId"], "captureId": "2" * 64,
        "revision": revision, "candidateRawSha256": candidate, "stage": "fragment",
        "entryPoint": "main", "api": api, "encoding": encoding,
        "baselineAbiSha256": "4" * 64}, "requestId": "a" * 64, "params": {}}
    anchor = {"eventId": 321, "originalResourceId": 77, "originalRawSha256": "3" * 64,
              "physicalAbiSha256": "4" * 64}
    native_abi = "4" * 64
    if legacy_topology:
        from tests.test_shader_abi import complete_interface
        from src.shader_workspace.abi import parse_shader_abi
        interface = complete_interface()
        native_abi = parse_shader_abi(interface).sha256
        interface["outputTopology"] = ""
        old_abi = parse_shader_abi(interface).sha256
        status["baseline"].update(abiSha256=old_abi, compiledInterface=interface)
        request["binding"]["baselineAbiSha256"] = old_abi
        anchor["physicalAbiSha256"] = old_abi
    service = mock.Mock()
    service.status.side_effect = lambda path: copy.deepcopy(status)
    service.candidate_reflection_request.return_value = request
    fresh = mock.Mock(return_value=(service, copy.deepcopy(status), request,
        {"replayAbiValidated": True, "abiReportSha256": "b" * 64}))
    monkeypatch.setattr(server, "_shared_live_abi_lease", lambda path: None)
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: service)
    monkeypatch.setattr(server, "_fresh_shader_candidate_validation", fresh)
    monkeypatch.setattr(server, "_observe_current_shared_shader_candidate", lambda *args: None)
    monkeypatch.setattr(server, "list_exact_shader_family_uses", lambda *args: [321])
    monkeypatch.setattr(server, "_shader_family_event_bindings", lambda **kwargs: [anchor])
    monkeypatch.setattr(server, "_remember_shared_shader_replacement", mock.Mock())
    monkeypatch.setattr(server, "_remember_route_success", mock.Mock(return_value={}))
    monkeypatch.setattr(server, "_shader_cbuffer_delivery_from_status", lambda status: {})
    monkeypatch.setattr(server, "_SHADER_OVERRIDE_CAPTURE_KEY", binding["captureSessionId"])
    def native(method, params, timeout):
        assert method == "apply_shader_candidate_shared_resource"
        if fault == "native-failure":
            raise RuntimeError("controlled native failure")
        return {"schema": "renderdoc-ultra-shared-shader-replacement/v1", "schemaVersion": 1,
            **request["binding"], "captureSessionId": binding["captureSessionId"],
            "anchorEventId": anchor["eventId"], "originalResourceId": 77, "originalRawSha256": "3" * 64,
            "physicalAbiSha256": native_abi, "abiRequestId": request["requestId"], "abiReportSha256": "b" * 64,
            "resourceScoped": True, "eventScoped": False, "allOriginalResourceUsesAffected": True,
            "configured": True, "originalShaderPreserved": True, "singleOwner": True,
            "checkedDerivedPipelineRebuild": api in {"d3d12", "vulkan", "opengl"},
            "capturedRootSignaturesReused": api == "d3d12", "postApplyAnchorVerified": api in {"vulkan", "opengl"},
            "replacementId": "5" * 32, "operationId": params["operation_id"],
            "operationCreatedReplacement": True, "idempotent": False, "metadataOnly": False,
            "targetBuiltOnce": True, "targetReused": False, "targetResourceId": 909,
            "supersededTargetFreed": False, "shaderFamilyId": "d" * 64}
    ipc = mock.Mock(side_effect=native)
    monkeypatch.setattr(server.ipc, "call", ipc)
    monkeypatch.setattr(internal_tool_service, "_shader_capture_session_state", lambda *args, **kwargs: "current")
    session = internal_tool_service._freeze_shader_live_session(
        binding["instanceId"], binding["captureSessionId"], 1234, binding["capturePath"])
    production_apply = session.apply_candidate
    def invoke(path, unused_revision, unused_raw):
        if fault == "wrong-capture":
            with bind_direct_tool_call("foreign-gui", "8" * 32):
                return server._shader_live_edit_apply_candidate_with_abi(path, revision, candidate)
        return production_apply(path, revision, candidate)
    session = type(session)(session.binding, session.state, invoke)
    watch = _FakeWatchManager()
    manager = ShaderLiveEditManager(watch, server._shader_live_edit_apply_candidate_with_abi,
        poll_seconds=0.005, join_seconds=0.5, bind_session=lambda path: session)
    monkeypatch.setattr(internal_tool_service, "_SHADER_LIVE_EDIT_MANAGER",
                        None if fault == "missing-manager" else manager)
    local_owner = manager.require_local_apply_owner
    def checked_local_owner(path, expected, direct_binding):
        key = shader_live_edit._workspace_key(tmp_path.resolve())
        if fault == "foreign-owner":
            # A second actual manager entry blocks the owner even though no
            # public serialized status or remote list call is involved.
            with manager._lock:
                manager._jobs["f" * 32] = SimpleNamespace(workspace=tmp_path, is_terminal=lambda: False)
            try:
                return local_owner(path, expected, direct_binding)
            finally:
                with manager._lock:
                    manager._jobs.pop("f" * 32)
        if fault == "missing-owner":
            with manager._lock:
                active = manager._active_by_workspace.pop(key)
            try:
                return local_owner(path, expected, direct_binding)
            finally:
                with manager._lock:
                    manager._active_by_workspace[key] = active
        if fault == "replaced-owner-object":
            with manager._lock:
                active = manager._active_by_workspace[key]
                original_job = manager._jobs[active]
                manager._jobs[active] = SimpleNamespace(live_edit_id=active)
                try:
                    return local_owner(path, expected, direct_binding)
                finally:
                    manager._jobs[active] = original_job
        return local_owner(path, expected, direct_binding)
    monkeypatch.setattr(manager, "require_local_apply_owner", checked_local_owner)
    def owners(*args):
        value = manager.list()
        if fault == "foreign-owner":
            other = dict(value["sessions"][0], liveEditId="f" * 32,
                         captureBinding={**binding, "instanceId": "foreign-gui"})
            value["sessions"].append(other)
        elif fault == "missing-owner":
            value["sessions"] = []
        return value
    remote = mock.Mock(side_effect=owners)
    monkeypatch.setattr(server, "_shader_live_edit_remote", remote)
    reset_observed = []
    record = shader_live_edit._LiveEditJob._record_locked
    def checked_record(job, event, *args, **kwargs):
        if event in {"applySucceeded", "applyFailed"}:
            reset_observed.append(shader_live_edit._LIVE_APPLY_OWNER.get() is None)
        return record(job, event, *args, **kwargs)
    monkeypatch.setattr(shader_live_edit._LiveEditJob, "_record_locked", checked_record)
    return SimpleNamespace(manager=manager, watch=watch, binding=binding, ipc=ipc,
        status=status, request=request, anchor=anchor, fresh=fresh, reset_observed=reset_observed,
        remote=remote)


@pytest.mark.parametrize("api", ["d3d11", "d3d12", "vulkan", "opengl"])
@pytest.mark.parametrize("legacy_topology", [False, True])
def test_real_worker_can_enter_public_apply_without_exempting_external_call(monkeypatch, tmp_path, api, legacy_topology):
    case = owner_case(monkeypatch, tmp_path, api, legacy_topology=legacy_topology)
    try:
        started = case.manager.start(str(tmp_path), 100)
        case.watch.publish(started["watchId"], 6, "6" * 64)
        _wait_until(lambda: case.manager.poll(started["liveEditId"])["applySuccesses"] == 1)
        assert case.ipc.call_count == 1
        case.remote.assert_not_called()
        assert case.reset_observed == [True]
        with bind_direct_tool_call(case.binding["instanceId"], case.binding["captureSessionId"]):
            result = json.loads(server.apply_shader_workspace_shared_resource(str(tmp_path), 6, "6" * 64))
        assert "stop the exact" in result["error"]
        assert case.ipc.call_count == 1
        with pytest.raises(RuntimeError, match="stop the exact"):
            server._require_workspace_live_edit_quiescent(str(tmp_path), action="rolling back",
                expected_candidate={"revision": 6, "candidateRawSha256": "6" * 64})
    finally:
        case.manager.shutdown()


@pytest.mark.parametrize("fault", ["wrong-revision", "wrong-hash", "wrong-capture",
                                   "foreign-owner", "missing-owner", "missing-manager",
                                   "replaced-owner-object", "native-failure"])
def test_worker_context_is_exact_and_resets_after_failure(monkeypatch, tmp_path, fault):
    case = owner_case(monkeypatch, tmp_path, fault=fault)
    try:
        started = case.manager.start(str(tmp_path), 100)
        case.watch.publish(started["watchId"], 6, "6" * 64)
        _wait_until(lambda: case.manager.poll(started["liveEditId"])["applyFailures"] == 1)
        assert case.ipc.call_count == int(fault == "native-failure")
        assert case.reset_observed == [True]
        assert shader_live_edit._LIVE_APPLY_OWNER.get() is None
    finally:
        case.manager.shutdown()


@pytest.mark.parametrize("mode", [
    "already-rolled-back", "already-renewed", "race-rollback", "race-renewal",
    "unknown-error", "timeout", "still-current", "invalid-inventory",
    "foreign-owner", "candidate-changed", "native-owner-mismatch", "untyped-error",
    "invalid-response", "post-validation-failed",
])
def test_stale_cross_process_lease_uses_fresh_validation_with_real_owner_guard(monkeypatch, tmp_path, mode):
    original_lease_lookup = server._shared_live_abi_lease
    case = owner_case(monkeypatch, tmp_path,
                      fault="foreign-owner" if mode == "foreign-owner" else "")
    lease = {
        "replacementId": "1" * 32, "captureId": "2" * 64,
        "captureSessionId": case.binding["captureSessionId"], "workspaceId": "1" * 32,
        "workspacePath": str(tmp_path), "revision": 3, "stage": "fragment", "entryPoint": "main",
        "anchorEventId": 321, "originalResourceId": 77, "originalRawSha256": "3" * 64,
        "candidateRawSha256": "4" * 64, "physicalAbiSha256": "4" * 64,
        "abiLeaseRevision": 3, "abiLeaseCandidateRawSha256": "4" * 64,
        "abiLeaseRequestId": "a" * 64, "abiLeaseReportSha256": "b" * 64,
    }
    monkeypatch.setattr(server, "_SHARED_SHADER_REPLACEMENT_RECORDS", {lease["replacementId"]: copy.deepcopy(lease)})
    monkeypatch.setattr(server, "_shared_live_abi_lease", original_lease_lookup)
    monkeypatch.setattr(server, "_ensure_workspace_capture_catalog_identity",
                        lambda *args, **kwargs: {"captureSessionId": case.binding["captureSessionId"]})
    monkeypatch.setattr(server, "_expected_shared_shader_anchor", lambda *args: copy.deepcopy(case.anchor))
    case.request["binding"]["baselineAbiSha256"] = "4" * 64
    normal_apply = case.ipc.side_effect
    calls = []
    reads = []

    def native(method, params, timeout):
        calls.append(method)
        if method == "get_shared_shader_replacements":
            reads.append(True)
            if mode == "invalid-inventory":
                return {"replacements": []}
            row = {**lease, "resourceScoped": True, "eventScoped": False,
                   "allOriginalResourceUsesAffected": True, "originalShaderPreserved": True}
            if mode == "post-validation-failed":
                row["postApplyValidationFailed"] = True
            stale = mode.startswith("already-") or mode in ("foreign-owner", "candidate-changed") or len(reads) > 1
            if mode == "still-current":
                stale = False
            rows = [row]
            if stale:
                if "renew" in mode:
                    # Same physical mapping can receive a fresh full ABI lease
                    # in another Host without changing its replacement token.
                    row["abiLeaseRequestId"] = "f" * 64
                else:
                    rows = []
            if mode == "candidate-changed":
                case.status["source"]["currentSha256"] = "d" * 64
            return {"schema": "renderdoc-ultra-shared-shader-replacement-list/v1", "schemaVersion": 1,
                "captureId": lease["captureId"], "captureSessionId": lease["captureSessionId"],
                "count": len(rows), "replacements": rows, "mutationEpoch": len(reads),
                "leaseMetadataOnly": True, "queryWorkspaceId": lease["workspaceId"],
                "queryReplacementId": lease["replacementId"],
                "originalShadersPreserved": True, "replayMutation": False}
        assert method == "apply_shader_candidate_shared_resource"
        if params.get("abi_validation_mode"):
            if mode == "unknown-error":
                raise IPCBridgeResponseError(-32000, "Physical ABI mismatch")
            if mode == "timeout":
                raise IPCBridgeTimeoutError("reply missing after apply")
            if mode == "native-owner-mismatch":
                raise IPCBridgeResponseError(-32000, "Shared Shader UI replacement owner changed")
            if mode == "untyped-error":
                raise RuntimeError("An inline ABI lease requires an active fully validated replacement")
            if mode == "invalid-response":
                return {"operationId": params["operation_id"], "schema": "unexpected"}
            message = ("The inline ABI lease does not match the active replacement" if "renew" in mode else
                       "An inline ABI lease requires an active fully validated replacement")
            raise IPCBridgeResponseError(-32000, message)
        return normal_apply(method, params, timeout)

    case.ipc.side_effect = native
    try:
        started = case.manager.start(str(tmp_path), 100)
        case.watch.publish(started["watchId"], 6, "6" * 64)
        success = mode in {"already-rolled-back", "already-renewed", "race-rollback", "race-renewal"}
        _wait_until(lambda: case.manager.poll(started["liveEditId"])["applySuccesses" if success else "applyFailures"] == 1)
        assert case.reset_observed == [True]
        full_applies = [call for call in case.ipc.call_args_list
                        if call.args[0] == "apply_shader_candidate_shared_resource" and
                        not call.args[1].get("abi_validation_mode")]
        assert len(full_applies) == int(success)
        if success:
            assert case.fresh.call_count == 1
            assert lease["replacementId"] not in server._SHARED_SHADER_REPLACEMENT_RECORDS
        elif mode in {"unknown-error", "timeout", "still-current", "invalid-inventory",
                      "native-owner-mismatch", "untyped-error", "invalid-response", "post-validation-failed"}:
            assert case.fresh.call_count == 0
            assert lease["replacementId"] in server._SHARED_SHADER_REPLACEMENT_RECORDS
        if mode.startswith("race-") or mode == "still-current":
            assert len(reads) == 2
        else:
            assert len(reads) == 1
    finally:
        case.manager.shutdown()


def test_lease_cache_retirement_preserves_concurrent_renewal(monkeypatch, tmp_path):
    old = {"replacementId": "1" * 32, "abiLeaseRevision": 3}
    renewed = dict(old, abiLeaseRevision=4)
    monkeypatch.setattr(server, "_SHARED_SHADER_REPLACEMENT_RECORDS", {old["replacementId"]: renewed})
    server._forget_stale_shared_live_abi_lease(old)
    assert server._SHARED_SHADER_REPLACEMENT_RECORDS[old["replacementId"]] == renewed
