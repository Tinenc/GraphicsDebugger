"""Manual saves compile/apply without replay ABI work or candidate proof claims."""
import hashlib
import os
from types import SimpleNamespace as NS

import pytest

from tests import test_shader_restore_receipts as receipts


@pytest.fixture
def case():
    fixture = receipts.ShaderRestoreReceiptTests()
    fixture.setUp()
    yield fixture
    fixture.doCleanups()


def manual(case, api="D3D12", encoding="dxil"):
    bridge, ctrl = case.bridge()
    attributes = {"dxbc": "DXBC", "dxil": "DXIL", "spirv": "SPIRV",
                  "opengl-spirv": "OpenGLSPIRV", "glsl": "GLSL"}
    attribute = attributes[encoding]
    setattr(bridge.rd.ShaderEncoding, attribute, attribute)
    ctrl.api = "GraphicsAPI." + api
    ctrl.GetTargetShaderEncodings = lambda: [attribute]
    ctrl.ReplaceResource = ctrl.ReplaceTargetShaderChecked
    ctrl.RemoveReplacement = ctrl.RemoveTargetShaderReplacementChecked
    bridge._known_catalog_capture_ids = {"c" * 64: os.path.normcase(str(case.capture))}
    params = dict(case.params(), abi_validation_mode="manual-fast", encoding=encoding,
                  expected_capture_session_id="7" * 32, expected_profile="fixture-profile")
    params.pop("abi_request_id")
    params.pop("abi_report_sha256")
    return bridge, ctrl, params


def no_validation(bridge):
    def forbidden(*args, **kwargs):
        pytest.fail("Manual editing invoked an ABI/replay validation path")
    bridge._validate_live_shader_event = forbidden
    bridge._physical_shader_interface_sha256 = forbidden
    bridge._physical_shader_interfaces_compatible = forbidden
    bridge._dxil_evidence_from_live_disassembly = forbidden
    bridge._dxil_ambiguous_interface_compatible = forbidden
    bridge._shader_restore_receipt_store.seal = forbidden


@pytest.mark.parametrize("api,encoding", [
    ("D3D11", "dxbc"), ("D3D12", "dxbc"), ("D3D12", "dxil"),
    ("Vulkan", "spirv"), ("OpenGL", "opengl-spirv"), ("OpenGL", "glsl"),
])
def test_manual_first_and_later_edits_never_replay_or_validate_abi(case, api, encoding):
    bridge, ctrl, params = manual(case, api, encoding)
    no_validation(bridge)
    result = bridge._apply_shader_candidate_shared_resource(params)
    assert result["configured"] and result["manualEdit"]
    assert result["validationScope"] == "manual-edit-unvalidated"
    assert result["replayAbiValidated"] is False
    assert result["freshPhysicalAbiValidationPerformed"] is False
    assert result["postApplyAnchorVerified"] is False
    assert result["abiRequestId"] == result["abiReportSha256"] == ""
    assert result["abiLeaseRequestId"] is None
    assert result["sourceSha256"] == params["expected_source_sha256"]
    assert "restoreValidationReceipt" not in result
    assert [c for c in ctrl.calls if c[0] == "reflect"] == [("reflect", 77)]
    assert not any(c[0] == "event" for c in ctrl.calls)
    ctrl.calls.clear()
    case.candidate.write_bytes(b"another compiled candidate")
    params.update(revision=2, expected_sha256=hashlib.sha256(case.candidate.read_bytes()).hexdigest())
    later = bridge._apply_shader_candidate_shared_resource(params)
    assert later["targetBuiltOnce"] and later["supersededTargetCached"]
    assert not any(c[0] in ("reflect", "entries", "event") for c in ctrl.calls)
    assert all(v >= 0 for v in later["timingsMs"].values())


def test_identical_binary_updates_source_without_build_or_replace(case):
    bridge, ctrl, params = manual(case)
    no_validation(bridge)
    first = bridge._apply_shader_candidate_shared_resource(params)
    ctrl.calls.clear()
    params.update(revision=2, expected_source_sha256="e" * 64)
    current = bridge._apply_shader_candidate_shared_resource(params)
    assert current["replacementId"] == first["replacementId"]
    assert current["metadataOnly"] and current["targetReused"]
    assert current["sourceSha256"] == "e" * 64 and current["revision"] == 2
    assert not ctrl.calls


def test_edit_undo_reuses_native_target_and_bounds_inactive_cache(case):
    bridge, ctrl, params = manual(case)
    no_validation(bridge)
    first_bytes = case.candidate.read_bytes()
    first = bridge._apply_shader_candidate_shared_resource(params)
    case.candidate.write_bytes(b"second candidate")
    params.update(revision=2, expected_sha256=hashlib.sha256(case.candidate.read_bytes()).hexdigest())
    second = bridge._apply_shader_candidate_shared_resource(params)
    case.candidate.write_bytes(first_bytes)
    params.update(revision=3, expected_sha256=hashlib.sha256(first_bytes).hexdigest())
    ctrl.calls.clear()
    undo = bridge._apply_shader_candidate_shared_resource(params)
    assert undo["targetResourceId"] == first["targetResourceId"]
    assert undo["targetCacheHit"] and undo["targetReused"] and not undo["targetBuiltOnce"]
    assert not any(c[0] in ("build", "reflect", "entries", "event") for c in ctrl.calls)
    assert undo["targetResourceId"] != second["targetResourceId"]
    for revision in range(4, 16):
        case.candidate.write_bytes(("candidate-%d" % revision).encode())
        params.update(revision=revision, expected_sha256=hashlib.sha256(case.candidate.read_bytes()).hexdigest())
        bridge._apply_shader_candidate_shared_resource(params)
    assert len(bridge._shared_shader_manual_target_cache) == 8
    active = bridge._shared_shader_replacements[77]["targetResourceId"]
    assert ("free", active) not in ctrl.calls and ("free", 77) not in ctrl.calls
    assert sum(c[0] == "free" for c in ctrl.calls) >= 3
    bridge._release_shared_shader_replacements()
    assert not bridge._shared_shader_manual_target_cache
    assert not bridge._shared_shader_replacements
    assert ("free", active) in ctrl.calls and ("free", 77) not in ctrl.calls


@pytest.mark.parametrize("field,value", [
    ("expected_capture_session_id", "8" * 32),
    ("expected_original_resource_id", 78),
    ("expected_original_sha256", "0" * 64),
    ("entry_point", "OtherMain"), ("stage", "vertex"),
    ("expected_sha256", "0" * 64),
])
def test_manual_identity_mismatch_never_replaces(case, field, value):
    bridge, ctrl, params = manual(case)
    no_validation(bridge)
    params[field] = value
    with pytest.raises((RuntimeError, ValueError)):
        bridge._apply_shader_candidate_shared_resource(params)
    assert not ctrl.replacements and not bridge._shared_shader_replacements
    assert not any(c[0] == "build" for c in ctrl.calls)


@pytest.mark.parametrize("failure", ["compile", "replace", "foreign-owner"])
def test_manual_failure_keeps_last_good_owner(case, failure):
    bridge, ctrl, params = manual(case)
    no_validation(bridge)
    first = bridge._apply_shader_candidate_shared_resource(params)
    case.candidate.write_bytes(b"bad next candidate")
    params.update(revision=2, expected_sha256=hashlib.sha256(case.candidate.read_bytes()).hexdigest())
    if failure == "compile":
        ctrl.BuildTargetShader = lambda *a: (receipts.ResourceId(), "compile failed")
    elif failure == "replace":
        ctrl.ReplaceTargetShaderChecked = lambda *a: "driver pipeline creation failed"
    else:
        bridge._ctx.replacements[77] = receipts.ResourceId(999)
    with pytest.raises(RuntimeError):
        bridge._apply_shader_candidate_shared_resource(params)
    assert ctrl.replacements[77] == first["targetResourceId"]
    assert bridge._shared_shader_replacements[77]["replacementId"] == first["replacementId"]
    assert ("free", first["targetResourceId"]) not in ctrl.calls


def test_strict_validation_after_manual_cannot_use_manual_metadata_as_abi_proof(case):
    bridge, ctrl, params = manual(case)
    bridge._apply_shader_candidate_shared_resource(params)
    params.pop("abi_validation_mode")
    params.update(abi_request_id="a" * 64, abi_report_sha256="d" * 64)
    original_validate = bridge._validate_live_shader_event
    checks = []
    def checked(*args, **kwargs):
        checks.append("strict-live-identity")
        return original_validate(*args, **kwargs)
    bridge._validate_live_shader_event = checked
    bridge._dxil_evidence_from_live_disassembly = lambda *a: (None, None)
    bridge._dxil_ambiguous_interface_compatible = lambda *a, **kw: False
    with pytest.raises(RuntimeError, match="physical ABI differs"):
        bridge._apply_shader_candidate_shared_resource(params)
    assert checks == ["strict-live-identity"]
    assert bridge._shared_shader_replacements[77]["abiValidationMode"] == "manual-fast"
    assert len([c for c in ctrl.calls if c[0] == "build"]) == 2


def test_manual_original_identity_can_be_rolled_back_without_fake_physical_abi(case):
    bridge, ctrl, params = manual(case)
    first = bridge._apply_shader_candidate_shared_resource(params)
    assert first["physicalAbiSha256"] == ""
    exact = dict(expected_capture_id="c" * 64, expected_capture_session_id="7" * 32,
                 workspace_id="b" * 32, expected_event_id=321, stage="fragment", entry_point="main")
    for request, field in (("original_resource_id", "originalResourceId"), ("original_sha256", "originalRawSha256"),
            ("replacement_id", "replacementId"), ("operation_id", "operationId"), ("revision", "revision"),
            ("anchor_event_id", "anchorEventId"), ("api", "api"), ("encoding", "encoding"),
            ("candidate_sha256", "candidateRawSha256"), ("physical_abi_sha256", "physicalAbiSha256"),
            ("shader_family_id", "shaderFamilyId"), ("target_resource_id", "targetResourceId")):
        exact["expected_" + request] = first[field]
    restored = bridge._restore_shader_candidate_shared_resource_exact(exact)
    assert restored["state"] == "rolled-back"
    assert restored["rollback"]["manualEdit"] is True
    assert restored["rollback"]["replayAbiValidated"] is False
    assert restored["rollback"]["validationScope"] == "manual-edit-unvalidated"
    assert not ctrl.replacements and not bridge._shared_shader_replacements
    again = bridge._rollback_shader_candidate_shared_resource({
        "replacement_id": first["replacementId"], "expected_capture_id": "c" * 64,
        "workspace_id": "b" * 32, "expected_capture_session_id": "7" * 32})
    assert again["alreadyRestored"] is True and again["manualEdit"] is True
    assert again["replayAbiValidated"] is False


def test_handoff_and_ui_supersede_preserve_one_original_owner(case):
    bridge, ctrl, params = manual(case)
    first = bridge._apply_shader_candidate_shared_resource(params)
    owner = bridge._shared_shader_replacements[77]
    handoff = {key: owner[key] for key in ("replacementId", "workspaceId", "captureId",
        "captureSessionId", "candidateRawSha256", "revision")}
    case.candidate.write_bytes(b"authored source candidate")
    params.update(workspace_id="e" * 32, revision=2, expected_owner=handoff,
                  expected_sha256=hashlib.sha256(case.candidate.read_bytes()).hexdigest())
    def forbidden_unregister(*args):
        pytest.fail("Manual supersede should register the new owner in one UI refresh")
    bridge._ctx.UnregisterReplacement = forbidden_unregister
    second = bridge._apply_shader_candidate_shared_resource(params)
    assert second["workspaceId"] == "e" * 32
    assert ctrl.replacements[77] == second["targetResourceId"]
    assert second["replacementId"] != first["replacementId"]


def test_inventory_never_describes_manual_owner_as_validated(case):
    bridge, ctrl, params = manual(case)
    bridge._apply_shader_candidate_shared_resource(params)
    result = bridge._get_shared_shader_replacements({"expected_capture_id": "c" * 64,
        "expected_capture_session_id": "7" * 32, "include_validation_receipts": True})
    row, = result["replacements"]
    assert row["manualEdit"] and row["validationScope"] == "manual-edit-unvalidated"
    assert row["replayAbiValidated"] is False and "restoreValidationReceipt" not in row
    assert row["sourceSha256"] == params["expected_source_sha256"]
