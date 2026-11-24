"""Manual owner resume/rollback carries identity without inventing an ABI proof."""
import copy
from unittest import mock

import pytest

from src import server
from tests.test_shader_live_edit_native_resume import ResumeFixture
from tests.test_server_pass_live import ServerRestoreCurrentShaderTests


MANUAL = {"abiValidationMode": "manual-fast", "manualEdit": True,
          "validationScope": "manual-edit-unvalidated", "replayAbiValidated": False}


def manual_resume(tmp_path, imported=False):
    fixture = ResumeFixture(tmp_path)
    candidate = fixture.value["latestCompiled"]
    candidate.update(validationScope="manual-edit-unvalidated", replayAbiValidated=False,
                     abiCompatible=False, requiresReplayAbiValidation=False,
                     abiGate={"status": "skipped-manual-edit"})
    if imported:
        candidate["projectImport"] = {"manualEdit": True}
    else:
        candidate["manualFastCompile"] = True
    fixture.inventory["replacements"][0].update(MANUAL, physicalAbiSha256="")
    return fixture


@pytest.mark.parametrize("imported", [False, True])
@pytest.mark.parametrize("physical", ["", "d" * 64])
def test_manual_native_resume_keeps_unvalidated_state_and_never_compiles(tmp_path, imported, physical):
    fixture = manual_resume(tmp_path, imported)
    fixture.inventory["replacements"][0]["physicalAbiSha256"] = physical
    with fixture.patches(), mock.patch.object(server.ipc, "call", fixture.native):
        result = fixture.recover()
    assert result["manualEdit"] and result["applied"]
    assert result["mutationPerformed"] is False
    assert result["validation"]["validationScope"] == "manual-edit-unvalidated"
    assert result["validation"]["replayAbiValidated"] is False
    assert result["validation"]["freshPhysicalValidationPerformed"] is False
    assert result["validation"]["freshLiveCaptureValidationPerformed"] is False
    fixture.native.assert_called_once()
    fixture.service.compile.assert_not_called()


@pytest.mark.parametrize("key,value", [("manualEdit", False), ("replayAbiValidated", True),
    ("validationScope", "current-native-mapping"), ("abiValidationMode", ""),
    ("restoreValidationReceipt", {"forged": True}), ("physicalAbiSha256", "invalid")])
def test_manual_native_resume_rejects_incomplete_or_false_provenance(tmp_path, key, value):
    fixture = manual_resume(tmp_path)
    fixture.inventory["replacements"][0][key] = value
    with fixture.patches(), mock.patch.object(server.ipc, "call", fixture.native):
        with pytest.raises((RuntimeError, ValueError)):
            fixture.recover()


@pytest.fixture
def current_restore():
    fixture = ServerRestoreCurrentShaderTests()
    fixture.setUpClass()
    fixture.setUp()
    fixture.physical_abi = ""
    fixture.replacements[0].update(MANUAL, physicalAbiSha256="")
    native = fixture.ipc.call
    def call(method, params=None, timeout=None):
        result = native(method, params, timeout)
        if method == "restore_shader_candidate_shared_resource_exact" and result.get("rollback"):
            result["rollback"].update(MANUAL)
        return result
    fixture.ipc.call = call
    try:
        yield fixture
    finally:
        fixture.tearDown()


def test_current_shader_restore_accepts_exact_manual_owner_without_physical_abi(current_restore):
    fixture = current_restore
    result = fixture._restore("pixel")
    assert "error" not in result, result
    assert result["restored"] and result["applied"]
    assert result["physicalAbiSha256"] == ""
    assert not fixture.replacements


@pytest.mark.parametrize("key,value", [("manualEdit", False), ("replayAbiValidated", True),
    ("abiValidationMode", ""), ("validationScope", "current-native-mapping")])
def test_current_restore_rejects_incomplete_manual_owner_before_mutation(current_restore, key, value):
    fixture = current_restore
    fixture.replacements[0][key] = value
    result = fixture._restore()
    assert "error" in result
    assert len(fixture.replacements) == 1
    assert not any(x[0] == "restore_shader_candidate_shared_resource_exact" for x in fixture.ipc.calls)


def rollback_receipt():
    return {**MANUAL, "stage": "fragment", "entryPoint": "main", "api": "vulkan",
            "encoding": "spirv", "originalResourceId": 77, "targetResourceId": 100,
            "originalRawSha256": "a" * 64, "candidateRawSha256": "b" * 64,
            "physicalAbiSha256": "", "shaderFamilyId": "c" * 64,
            "resourceScoped": True, "eventScoped": False, "alreadyRestored": False,
            "idempotentRollback": False, "cleared": True, "originalShaderPreserved": True,
            "targetFreed": True}


def test_manual_rollback_receipt_is_valid_without_host_local_memory():
    result = rollback_receipt()
    assert server._valid_shader_rollback_physical_result(result, status={}, local=None, resource_scoped=True)
    result.update(alreadyRestored=True, idempotentRollback=True)
    assert server._valid_shader_rollback_physical_result(result, status={}, local=None, resource_scoped=True)


@pytest.mark.parametrize("key,value", [("manualEdit", False), ("replayAbiValidated", True),
    ("abiValidationMode", ""), ("validationScope", "current-native-mapping"),
    ("restoreValidationReceipt", {"false": True}), ("physicalAbiSha256", "invalid"),
    ("originalRawSha256", ""), ("candidateRawSha256", ""), ("shaderFamilyId", "")])
def test_rollback_rejects_missing_identity_or_incomplete_manual_marker(key, value):
    result = rollback_receipt()
    result[key] = value
    assert not server._valid_shader_rollback_physical_result(result, status={}, local=None, resource_scoped=True)


def test_event_scoped_rollback_cannot_use_manual_shared_policy():
    result = rollback_receipt()
    result.update(resourceScoped=False, eventScoped=True)
    assert not server._valid_shader_rollback_physical_result(result, status={}, local=None, resource_scoped=False)
