"""Only reviewed scheduling changes reuse an authenticated native ABI proof."""
import copy
from unittest import mock

import pytest

from src import project_restore
from tests import test_shader_restore_receipts as receipts


@pytest.fixture
def case():
    fixture = receipts.ShaderRestoreReceiptTests()
    fixture.setUp()
    yield fixture
    fixture.doCleanups()


def previous_receipt(case, old_index=0, change=None):
    receipt = case.fully_applied_receipt()
    module, controller = case.bridge(session="8" * 32)
    protocol = module._shader_restore_protocol
    payload = copy.deepcopy(receipt["payload"])
    previous = payload["physicalValidationBinding"]
    previous["algorithms"][0]["codeSha256"] = protocol._RESTORE_SCHEDULING_UPGRADES[old_index][0]
    if change == "native":
        previous["nativeBinaries"] = [{"name": "renderdoc.dll", "sha256": "1" * 64}]
    elif change == "rules":
        previous["abiRulesVersion"] += 1
    elif change == "dxil":
        previous["algorithms"][1]["codeSha256"] = "1" * 64
    elif change == "unknown":
        previous["algorithms"][0]["codeSha256"] = "1" * 64
    payload["validationPolicySha256"] = protocol.physical_validation_binding_sha256(previous)
    return module, controller, module._shader_restore_receipt_store.seal(payload)


@pytest.mark.parametrize("old_index", [0, 1])
def test_reviewed_previous_native_proof_reuses_validation_without_version_cold_rebuild(case, old_index):
    module, controller, receipt = previous_receipt(case, old_index)
    hints = module._shader_restore_protocol.compatible_validation_policies(module._shader_restore_physical_validation_binding)
    destination = module._shader_restore_protocol._RESTORE_SCHEDULING_UPGRADES[old_index][1]
    current_bridge = module._shader_restore_physical_validation_binding['algorithms'][0]['codeSha256']
    if current_bridge != destination:
        # A physical interface parser change is not the reviewed scheduling-only
        # upgrade. Never extend that exemption automatically to a new validator.
        assert receipt['payload']['validationPolicySha256'] not in hints
        with pytest.raises(RuntimeError, match='policy or replay binary changed'):
            module._apply_shader_candidate_shared_resource(case.warm_params(receipt))
        assert not any(c[0] in ('build','replace') for c in controller.calls)
        return
    assert receipt["payload"]["validationPolicySha256"] in hints
    with mock.patch.object(module, "_dxil_evidence_from_live_disassembly", side_effect=AssertionError("no printer")), \
         mock.patch.object(module, "_physical_shader_interface_sha256", side_effect=AssertionError("no ABI repeat")):
        result = module._apply_shader_candidate_shared_resource(case.warm_params(receipt))
    assert result["configured"] and result["freshPhysicalAbiValidationPerformed"] is False
    assert any(c[0] == "replace" for c in controller.calls)


@pytest.mark.parametrize("change", ["native", "rules", "dxil", "unknown"])
def test_authenticated_but_different_validation_domain_still_requires_fresh_validation(case, change):
    module, controller, receipt = previous_receipt(case, change=change)
    with pytest.raises(RuntimeError, match="policy or replay binary changed"):
        module._apply_shader_candidate_shared_resource(case.warm_params(receipt))
    assert not any(c[0] in ("build", "replace") for c in controller.calls)


def test_forged_receipt_cannot_use_a_compatibility_hint(case):
    module, controller, receipt = previous_receipt(case)
    receipt["signature"] = "0" * 64
    with pytest.raises(ValueError):
        module._apply_shader_candidate_shared_resource(case.warm_params(receipt))
    assert not controller.calls


def test_host_reuses_previous_origin_only_when_current_bridge_advertises_that_policy(monkeypatch):
    physical = {"schema": "replaylab-shader-physical-validation-binding/v1", "schemaVersion": 1,
                "abiRulesVersion": 1, "algorithms": [], "nativeBinaries": []}
    old_policy = project_restore._canonical_sha256(physical)
    saved = {"replacement": {"restoreValidationReceipt": {"payload": {
        "physicalValidationBinding": physical, "validationPolicySha256": old_policy}}}}
    monkeypatch.setattr(project_restore, "_load_workspace_shader_pair_validation", lambda status: saved)
    status = {"latestCompiled": {"abiGate": {"status": "compatible"}}}
    assert project_restore._load_shader_pair_validation(None, status, "a" * 64, (old_policy,)) is saved
    assert project_restore._load_shader_pair_validation(None, status, "a" * 64) is None


def test_exact_reviewed_batch_core_transition_does_not_admit_other_binaries_or_rules(case):
    module,unused=case.bridge()
    protocol=module._shader_restore_protocol
    previous={'schema':'replaylab-shader-physical-validation-binding/v1','schemaVersion':1,
        'abiRulesVersion':1,'algorithms':[
            {'role':'bridge-physical-shader-validation','codeSha256':'180ddeade13ea396669ad0450eb6179536b934be841b85cc3350659d9f043dab'},
            {'role':'captured-dxbc-declarations','codeSha256':'88551ab0c655066923a23797329ec7af7be0885881769f54ad2c592677f1ada6'},
            {'role':'captured-dxil-layout-access','codeSha256':'4ec86a8455d825be83c25acbc1f8f397cde69a08a9a672198033295f14bc4f2d'},
            {'role':'signed-shader-pair-verification','codeSha256':'957cde71a21cde3c31d9fa8c441fff813d4a410f7260d5e9cf1d0324ab3287ea'}],
        'nativeBinaries':[{'name':'renderdoc.dll','sha256':'1fc6382beafbed92fa9f029ac7c1365543de8dcbb4aa3693db87c43a6d25b61e'}]}
    current=copy.deepcopy(previous)
    current['algorithms'][0]['codeSha256']='f67a38aa325d67137a43386b4eeb0e23b5734939b121d5fe2149ec653ecc2ecd'
    current['nativeBinaries'][0]['sha256']='6c548f8b44400752215ec7dc6199ef1ba8c4b6903c4d99469827bad347c597ec'
    assert protocol.compatible_validation_binding(previous,current)
    assert protocol.physical_validation_binding_sha256(previous) in protocol.compatible_validation_policies(current)
    for change in ('native','rules','algorithm'):
        other=copy.deepcopy(current)
        if change=='native': other['nativeBinaries'][0]['sha256']='1'*64
        elif change=='rules': other['abiRulesVersion']+=1
        else: other['algorithms'][1]['codeSha256']='2'*64
        assert not protocol.compatible_validation_binding(previous,other)
        assert protocol.physical_validation_binding_sha256(previous) not in protocol.compatible_validation_policies(other)
