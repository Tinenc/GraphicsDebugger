"""The verified path reuses proofs but preserves transactional live ownership."""
import copy
import json
from types import SimpleNamespace

import pytest

from src import project_restore, server


def test_optional_workspace_pair_cache_failure_preserves_remembered_effect(monkeypatch, tmp_path):
    applied = {
        "replacementId": "1" * 32, "captureId": "a" * 64,
        "captureSessionId": "7" * 32, "workspaceId": "2" * 32,
        "revision": 4, "stage": "fragment", "entryPoint": "main", "api": "d3d11",
        "encoding": "dxbc", "originalResourceId": 70,
        "originalRawSha256": "b" * 64, "candidateRawSha256": "d" * 64,
        "anchorEventId": 20, "physicalAbiSha256": "9" * 64,
        "shaderFamilyId": "6" * 64, "targetResourceId": 101,
    }
    lease = {"abiLeaseRequestId": "e" * 64, "abiLeaseReportSha256": "f" * 64,
             "abiLeaseCandidateRawSha256": "d" * 64, "abiLeaseRevision": 4}
    records = {}
    monkeypatch.setattr(server, "_SHARED_SHADER_REPLACEMENT_RECORDS", records)
    def cache_failure(*args, **kwargs):
        raise OSError("optional derived cache is unavailable")
    monkeypatch.setattr(project_restore, "_persist_workspace_shader_pair_validation", cache_failure)
    server._remember_shared_shader_replacement(applied, status={"workspacePath": str(tmp_path)}, abi_lease=lease)
    assert records[applied["replacementId"]]["targetResourceId"] == 101
    assert records[applied["replacementId"]]["candidateRawSha256"] == "d" * 64


@pytest.fixture
def restore_fixture(monkeypatch, tmp_path):
    statuses, records, requests = [], [], {}
    for index in range(2):
        workspace = str(tmp_path / str(index))
        workspace_id = str(index + 1) * 32
        gate = {"status": "compatible", "requestId": "e" * 64,
                "reportSha256": "f" * 64}
        status = {
            "workspaceId": workspace_id, "workspacePath": workspace, "revision": 4,
            "capture": {"capturePath": str(tmp_path / "test.rdc"), "captureId": "a" * 64},
            "shader": {"resourceId": 70 + index}, "stage": "fragment", "entryPoint": "main",
            "baseline": {"rawSha256": "b" * 64},
            "source": {"currentSha256": "c" * 64},
            "latestCompiled": {"revision": 4, "rawSha256": "d" * 64,
                               "sourceSha256": "c" * 64, "abiGate": gate},
            "representation": {"lane": "executable-editable", "compileEligible": True},
        }
        binding = {"workspaceId": workspace_id, "captureId": "a" * 64,
                   "revision": 4, "candidateRawSha256": "d" * 64,
                   "baselineAbiSha256": "9" * 64, "stage": "fragment",
                   "entryPoint": "main", "api": "d3d11", "encoding": "dxbc"}
        requests[workspace] = {"binding": binding, "requestId": "e" * 64, "params": {}}
        previous = {"workspaceId": workspace_id, "revision": 4,
                    "candidateRawSha256": "d" * 64, "originalRawSha256": "b" * 64,
                    "physicalAbiSha256": "9" * 64, "stage": "fragment", "entryPoint": "main",
                    "api": "d3d11", "encoding": "dxbc", "anchorEventId": 20 + index,
                    "restoreValidationReceipt": {"payload": {}, "signature": "8" * 64}}
        records.append({"candidateRevision": 4, "candidateRawSha256": "d" * 64,
                        "sourceSha256": "c" * 64, "managedWorkspace": {"workspaceId": workspace_id},
                        "replacement": previous,
                        "validation": {"requestId": "e" * 64, "abiReportSha256": "f" * 64}})
        statuses.append(status)
    snapshot = {"schema": project_restore._VERIFIED_RDC_RESTORE_SCHEMA,
                "schemaVersion": project_restore._VERIFIED_RDC_RESTORE_VERSION,
                "capturePath": str(tmp_path / "test.rdc"), "captureSha256": "0" * 64,
                "shaders": records}
    state = SimpleNamespace(statuses=statuses, records=records, snapshot=snapshot,
                            calls=[], mutate_source=False, malformed=False, fail_second=False,
                            current_rid_offset=0, recovery_broken=False, duplicate_live_owner=False,
                            response_overrides={})
    def status(path):
        result = copy.deepcopy(next(item for item in statuses if item["workspacePath"] == path))
        if state.mutate_source:
            result["source"]["currentSha256"] = "2" * 64
        return result
    service = SimpleNamespace(status=status, candidate_reflection_request=lambda path: requests[path])
    monkeypatch.setattr(server, "_shader_workspace_service", lambda: service)
    monkeypatch.setattr(server, "_verified_restore_catalog_binding", lambda status: {})
    monkeypatch.setattr(server, "_remember_shared_shader_replacement", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_sync_shader_override_capture", lambda unused: None)
    monkeypatch.setattr(server, "_SHADER_OVERRIDE_CAPTURE_KEY", "")
    def call(method, params, **kwargs):
        state.calls.append((method, copy.deepcopy(params)))
        if method == "recover_shader_candidate_shared_resource_operation":
            return {
                "schema": "renderdoc-ultra-shared-shader-operation-recovery/v1", "schemaVersion": 1,
                "operationId": params["operation_id"], "captureId": params["expected_capture_id"],
                "captureSessionId": params["expected_capture_session_id"], "workspaceId": params["workspace_id"],
                "originalShaderPreserved": True, "strictReceipt": not state.recovery_broken, "recovered": True,
                "mutationPresent": False, "status": "already-recovered", "operationKnown": True,
                "operationCreatedReplacement": True, "replacementId": params["workspace_id"], "rollback": None,
            }
        assert method == "apply_shader_candidate_shared_resource", "fast restore must not reflect or export"
        if state.fail_second and len(state.calls) == 2:
            raise RuntimeError("second target failed")
        result = {
            "schema": "renderdoc-ultra-shared-shader-replacement/v1", "schemaVersion": 1,
            "replacementId": params["workspace_id"], "operationId": params["operation_id"],
            "operationCreatedReplacement": True, "captureId": "a" * 64,
            "captureSessionId": "7" * 32, "workspaceId": params["workspace_id"], "revision": 4,
            "anchorEventId": params["event_id"], "stage": "fragment", "entryPoint": "main",
            "api": "d3d11", "encoding": "dxbc", "originalResourceId": params["expected_original_resource_id"] + state.current_rid_offset,
            "originalRawSha256": "b" * 64, "candidateRawSha256": "d" * 64,
            "physicalAbiSha256": "9" * 64, "shaderFamilyId": "6" * 64, "targetResourceId": 101,
            "abiRequestId": "e" * 64, "abiReportSha256": "f" * 64,
            "resourceScoped": True, "eventScoped": False, "allOriginalResourceUsesAffected": True,
            "configured": True, "originalShaderPreserved": True, "singleOwner": True,
            "checkedDerivedPipelineRebuild": False, "capturedRootSignaturesReused": False,
            "postApplyAnchorVerified": False, "idempotent": False, "metadataOnly": False,
            "targetBuiltOnce": True, "targetReused": False, "supersededReplacementId": None,
            "supersededTargetFreed": True, "abiValidationMode": "verified-rdc-restore",
            "freshPhysicalAbiValidationPerformed": False,
            "restoreValidationReceipt": params["restore_validation_receipt"],
        }
        if state.malformed:
            result["physicalAbiSha256"] = "0" * 64
        if state.duplicate_live_owner:
            result["originalResourceId"] = 1070
        result.update(state.response_overrides)
        return result
    monkeypatch.setattr(server, "ipc", SimpleNamespace(call=call))
    return state


def run(state):
    return server._apply_verified_project_restore(state.snapshot, state.statuses, "7" * 32, "5" * 32)


def test_warm_restore_reuses_proofs_with_current_resource_ids(restore_fixture):
    state = restore_fixture
    state.current_rid_offset = 1000
    result = run(state)
    assert result["applied"] and len(result["items"]) == 2
    assert [item["replacement"]["originalResourceId"] for item in result["items"]] == [1070, 1071]
    assert all(item["validation"]["reusedValidation"] for item in result["items"])
    assert all(params["expected_source_sha256"] == "c" * 64 for _, params in state.calls)


def _pair_fixture(state):
    status = state.statuses[0]
    status["capture"]["api"] = "D3D11"
    status["baseline"]["abiSha256"] = "9" * 64
    status["source"]["present"] = True
    status["targetProfile"] = {"profile": "ps_5_0"}
    status["latestCompiled"]["kind"] = "DXBC"
    status["latestCompiled"]["abiGate"].update(
        candidateAbiSha256="2" * 64, reportPath="revisions/0004/abi-report.json")
    receipt = {"payload": {
        "schema": "replaylab-validated-shader-restore/v1", "captureSha256": "0" * 64,
        "originalRawSha256": "b" * 64, "candidateRawSha256": "d" * 64,
        "physicalAbiSha256": "9" * 64, "api": "d3d11", "stage": "fragment",
        "entryPoint": "main", "encoding": "dxbc",
        # Origin binding deliberately differs from the current workspace.
        "workspaceId": "8" * 32, "revision": 1, "sourceSha256": "1" * 64,
    }, "signature": "8" * 64}
    prior = {"replacement": {"restoreValidationReceipt": receipt}}
    state.response_overrides["abiValidationMode"] = "verified-shader-pair"
    return status, prior


def test_pair_restore_uses_current_binding_without_reflection_or_catalog_export(restore_fixture):
    state = restore_fixture
    status, prior = _pair_fixture(state)
    result = server._apply_project_shader_pair_restore(
        status, prior, "7" * 32, "5" * 32, 20)
    assert result["applied"] is True
    assert result["validation"]["reusedValidation"] is True
    assert result["validation"]["freshPhysicalValidationPerformed"] is False
    assert len(state.calls) == 1
    method, params = state.calls[0]
    assert method == "apply_shader_candidate_shared_resource"
    assert params["abi_validation_mode"] == "verified-shader-pair"
    assert params["use_current_anchor_resource"] is True
    assert params["expected_source_sha256"] == "c" * 64
    assert params["workspace_id"] == status["workspaceId"]
    assert params["expected_original_resource_id"] == 70
    assert params["restore_validation_receipt"] == prior["replacement"]["restoreValidationReceipt"]


def _set_restore_platform(state, api, encoding, target_mode):
    reused = target_mode != "built"
    for status, record in zip(state.statuses, state.records):
        status["capture"]["api"] = api
        status["latestCompiled"]["kind"] = encoding
        request = server._shader_workspace_service().candidate_reflection_request(status["workspacePath"])
        request["binding"].update(api=api, encoding=encoding)
        record["replacement"].update(api=api, encoding=encoding)
    state.response_overrides.update(
        api=api, encoding=encoding, checkedDerivedPipelineRebuild=api != "d3d11",
        capturedRootSignaturesReused=api == "d3d12", postApplyAnchorVerified=api in {"vulkan", "opengl"},
        idempotent=target_mode == "idempotent", metadataOnly=target_mode == "metadata",
        operationCreatedReplacement=not reused, targetBuiltOnce=not reused, targetReused=reused,
        freshPhysicalAbiValidationPerformed=encoding == "glsl" and not reused,
    )


@pytest.mark.parametrize("api,encoding", [
    ("d3d11", "dxbc"), ("d3d12", "dxil"), ("vulkan", "spirv"),
    ("opengl", "glsl"), ("opengl", "opengl-spirv"),
])
@pytest.mark.parametrize("target_mode", ["built", "idempotent", "metadata"])
def test_verified_restore_accepts_actual_driver_validation_contract(restore_fixture, api, encoding, target_mode):
    state = restore_fixture
    _set_restore_platform(state, api, encoding, target_mode)
    result = run(state)
    assert result["applied"] is True
    expected_fresh = encoding == "glsl" and target_mode == "built"
    assert all(item["validation"]["freshPhysicalValidationPerformed"] is expected_fresh
               for item in result["items"])
    assert all(item["validation"]["reusedValidation"] is True for item in result["items"])


@pytest.mark.parametrize('api,encoding', [('vulkan', 'spirv'), ('opengl', 'glsl'), ('opengl', 'opengl-spirv')])
def test_verified_restore_uses_mapping_only_with_signed_receipt_and_checked_pipelines(restore_fixture, api, encoding):
    state = restore_fixture
    _set_restore_platform(state, api, encoding, 'built')
    server.ipc.supports_verified_restore_mapping = True
    state.response_overrides.update(postApplyAnchorVerified=False, postApplyMappingVerified=True)
    result = run(state)
    assert result['applied']
    assert all(params.get('verify_replacement_mapping') is True for method, params in state.calls
               if method == 'apply_shader_candidate_shared_resource')


@pytest.mark.parametrize('field,value', [('postApplyMappingVerified', False),
    ('checkedDerivedPipelineRebuild', False), ('abiValidationMode', 'manual-fast'),
    ('restoreValidationReceipt', None)])
def test_unproven_mapping_never_substitutes_for_anchor_validation(restore_fixture, field, value):
    state = restore_fixture
    _set_restore_platform(state, 'vulkan', 'spirv', 'built')
    state.response_overrides.update(postApplyAnchorVerified=False, postApplyMappingVerified=True)
    state.response_overrides[field] = value
    with pytest.raises(RuntimeError):
        run(state)


@pytest.mark.parametrize("target_mode", ["built", "idempotent", "metadata"])
def test_glsl_pair_restore_retains_inline_driver_validation(restore_fixture, target_mode):
    state = restore_fixture
    status, prior = _pair_fixture(state)
    _set_restore_platform(state, "opengl", "glsl", target_mode)
    status["targetProfile"] = {"profile": "glsl"}
    prior["replacement"]["restoreValidationReceipt"]["payload"].update(api="opengl", encoding="glsl")
    result = server._apply_project_shader_pair_restore(status, prior, "7" * 32, "5" * 32, 20)
    assert result["applied"] is True
    assert result["validation"]["freshPhysicalValidationPerformed"] is (target_mode == "built")
    assert result["validation"]["reusedValidation"] is True


@pytest.mark.parametrize("reported", [False, None, 1])
def test_glsl_new_target_requires_boolean_inline_validation_evidence(restore_fixture, reported):
    state = restore_fixture
    _set_restore_platform(state, "opengl", "glsl", "built")
    state.response_overrides["freshPhysicalAbiValidationPerformed"] = reported
    with pytest.raises(RuntimeError, match="freshPhysicalAbiValidationPerformed"):
        run(state)
    assert state.calls[-1][0] == "recover_shader_candidate_shared_resource_operation"


def test_pair_restore_forwards_current_native_profile_to_unchanged_signed_proof(restore_fixture, monkeypatch):
    state = restore_fixture
    status, prior = _pair_fixture(state)
    profile = status["targetProfile"]["profile"]
    prior["replacement"]["restoreValidationReceipt"]["payload"]["targetProfile"] = profile
    request = server._shader_workspace_service().candidate_reflection_request(status["workspacePath"])
    request["binding"]["targetProfile"] = profile
    request["params"]["target_profile"] = profile
    signed_before = copy.deepcopy(prior["replacement"]["restoreValidationReceipt"])
    original_call = server.ipc.call

    def profile_checked_call(method, params, **kwargs):
        assert params["expected_profile"] == profile
        assert params["restore_validation_receipt"] == signed_before
        return original_call(method, params, **kwargs)

    monkeypatch.setattr(server.ipc, "call", profile_checked_call)
    result = server._apply_project_shader_pair_restore(status, prior, "7" * 32, "5" * 32, 20)
    assert result["applied"] and result["validation"]["reusedValidation"]
    assert result["validation"]["freshPhysicalValidationPerformed"] is False
    assert prior["replacement"]["restoreValidationReceipt"] == signed_before
    assert [method for method, unused in state.calls] == ["apply_shader_candidate_shared_resource"]


def test_pair_restore_does_not_copy_wrong_signed_profile_into_current_native_binding(restore_fixture):
    state = restore_fixture
    status, prior = _pair_fixture(state)
    prior["replacement"]["restoreValidationReceipt"]["payload"]["targetProfile"] = "ps_6_6"
    assert status["targetProfile"]["profile"] == "ps_5_0"
    with pytest.raises(RuntimeError, match="differs from the current candidate"):
        server._apply_project_shader_pair_restore(status, prior, "7" * 32, "5" * 32, 20)
    assert not state.calls


@pytest.mark.parametrize("field", ["originalRawSha256", "candidateRawSha256", "physicalAbiSha256", "api", "stage"])
def test_pair_restore_rejects_changed_immutable_pair_before_replay(restore_fixture, field):
    state = restore_fixture
    status, prior = _pair_fixture(state)
    prior["replacement"]["restoreValidationReceipt"]["payload"][field] = "0" * 64
    with pytest.raises(RuntimeError, match="differs from the current candidate"):
        server._apply_project_shader_pair_restore(status, prior, "7" * 32, "5" * 32, 20)
    assert not state.calls


@pytest.mark.parametrize("version", [1, 3, None, "2"])
def test_stale_or_unknown_memo_version_never_reaches_replay(restore_fixture, version):
    state = restore_fixture
    state.snapshot["schemaVersion"] = version
    with pytest.raises(RuntimeError, match="incomplete workspace set"):
        run(state)
    assert not state.calls


def test_persisted_restore_keeps_cache_miss_and_new_write_receipt(monkeypatch, tmp_path):
    from src import internal_tool_service
    path = tmp_path / "restore.json"
    monkeypatch.setattr(internal_tool_service, "_restore_result_record_path", lambda *args: path)
    result = {"completed": True, "cache": {"hit": False,
              "reason": "verified-rdc-cache-miss: validation-policy-changed"},
              "verifiedRdcCache": {"hit": False, "written": True,
                                   "reason": "validated-content-snapshot"},
              "editing": {"attempted": 2, "enabled": 2, "failed": 0, "items": []}}
    internal_tool_service.persist_restore_result(str(tmp_path), str(tmp_path / "test.rdc"), result, None)
    stored = json.loads(path.read_text(encoding="utf-8"))
    for field in ("cache", "verifiedRdcCache", "editing"):
        assert stored[field] == result[field]


@pytest.mark.parametrize("outer_error", [None, "worker failure"])
def test_persisted_restore_keeps_nested_error_type_and_outer_error_precedence(monkeypatch, tmp_path, outer_error):
    from src import internal_tool_service
    path = tmp_path / "restore.json"
    monkeypatch.setattr(internal_tool_service, "_restore_result_record_path", lambda *args: path)
    result = {"completed": False, "error": "RestoreContextUnavailable: IPCBridgeTimeoutError: Timeout (60s)",
              "errorType": "RestoreContextUnavailable",
              "contextRecovery": [{"errorType": "IPCBridgeTimeoutError", "error": "Timeout (60s)", "recovered": False}]}
    internal_tool_service.persist_restore_result(str(tmp_path), str(tmp_path / "test.rdc"), result, outer_error)
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["status"] == "failed"
    assert stored["error"] == (outer_error or result["error"])
    assert stored["errorType"] == result["errorType"]
    assert stored["contextRecovery"] == result["contextRecovery"]


@pytest.mark.parametrize("field,value", [
    ("abiValidationMode", "full-replay-report-and-inline-physical"),
    ("freshPhysicalAbiValidationPerformed", True),
    ("restoreValidationReceipt", {"payload": {}, "signature": "0" * 64}),
    ("operationId", "0" * 32),
    ("captureSessionId", "0" * 32),
])
def test_fast_response_mismatch_identifies_the_field_and_recovers(restore_fixture, field, value):
    state = restore_fixture
    state.response_overrides[field] = value
    with pytest.raises(RuntimeError, match=field) as failure:
        run(state)
    assert "'payload'" not in str(failure.value)
    assert state.calls[-1][0] == "recover_shader_candidate_shared_resource_operation"


@pytest.mark.parametrize("field", ["sourceSha256", "candidateRawSha256"])
def test_changed_source_or_binary_rejected_before_any_apply(restore_fixture, field):
    state = restore_fixture
    state.records[1][field] = "0" * 64
    with pytest.raises(RuntimeError):
        run(state)
    assert not state.calls


def test_edit_during_restore_rolls_back_the_whole_new_set(restore_fixture):
    state = restore_fixture
    state.mutate_source = True
    with pytest.raises(RuntimeError, match="changed during"):
        run(state)
    rollbacks = [params["workspace_id"] for method, params in state.calls if method.startswith("recover")]
    assert rollbacks == ["2" * 32, "1" * 32]


def test_malformed_response_cleans_up_proven_owned_target(restore_fixture):
    state = restore_fixture
    state.malformed = True
    with pytest.raises(RuntimeError, match="physicalAbiSha256"):
        run(state)
    assert state.calls[-1][0] == "recover_shader_candidate_shared_resource_operation"


def test_second_failure_rolls_back_first_success(restore_fixture):
    state = restore_fixture
    state.fail_second = True
    with pytest.raises(RuntimeError, match="second target failed"):
        run(state)
    recovered = [params["workspace_id"] for method, params in state.calls if method.startswith("recover")]
    assert recovered == ["2" * 32, "1" * 32]


def test_unproven_cleanup_stops_restore_instead_of_trying_cold_again(restore_fixture):
    from src.project_restore import RestoreRollbackFailed
    state = restore_fixture
    state.fail_second = state.recovery_broken = True
    with pytest.raises(RestoreRollbackFailed):
        run(state)


def test_distinct_old_resources_cannot_map_to_one_live_owner(restore_fixture):
    state = restore_fixture
    state.duplicate_live_owner = True
    with pytest.raises(RuntimeError, match="same live Shader"):
        run(state)
    assert len([method for method, _ in state.calls if method.startswith("recover")]) == 2


def test_batch_failure_recovers_only_failed_item_and_preserves_other_successes(restore_fixture,monkeypatch):
    state=restore_fixture
    original=server.ipc.call
    batches=[]
    def call(method,params,**kwargs):
        if method!='apply_shader_candidate_shared_resource_batch':
            return original(method,params,**kwargs)
        batches.append(params)
        rows=[]
        for index,item in enumerate(params['items']):
            if index==1:
                rows.append({'ok':False,'error':'one stale item'})
            else:
                rows.append({'ok':True,'result':original('apply_shader_candidate_shared_resource',item)})
        return {'captureSessionId':'7'*32,'items':rows}
    monkeypatch.setattr(server,'ipc',SimpleNamespace(call=call,supports_shader_restore_batches=True))
    result=run(state)
    assert not result['applied'] and len(result['items'])==1 and len(result['itemErrors'])==1
    recovered=[params['workspace_id'] for method,params in state.calls if method.startswith('recover')]
    assert recovered==['2'*32]
    assert len(batches)==1
