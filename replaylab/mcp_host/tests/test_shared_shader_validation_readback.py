"""Final publication can read an existing byte-pair proof without replay work."""

from copy import deepcopy
from types import SimpleNamespace
from unittest import mock

import pytest

from tests.test_bridge_catalog_rebind import _load_bridge


@pytest.fixture
def inventory():
    bridge = _load_bridge()
    token = ("capture", "path")
    bridge._capture_session_id = "a" * 32
    bridge._known_catalog_capture_ids = {"b" * 64: "path"}
    bridge._shared_shader_replacement_capture_token = token
    bridge._shared_shader_mutation_epoch = 7
    row = {
        "captureId": "b" * 64, "captureSessionId": "a" * 32,
        "captureToken": token, "replacementId": "c" * 32,
        "operationId": "d" * 32, "workspaceId": "e" * 32,
        "revision": 110, "anchorEventId": 11384,
        "stageName": "fragment", "entryPoint": "MainPS",
        "api": "d3d12", "encoding": "dxil",
        "expectedOriginalResourceId": 1000000000000000723,
        "expectedOriginalSha256": "1" * 64,
        "candidateRawSha256": "2" * 64, "physicalAbiSha256": "3" * 64,
        "familyId": "4" * 64, "targetResourceId": 123,
        "candidatePath": "private-path", "originalResource": object(),
        "abiRequestId": "5" * 64, "abiReportSha256": "6" * 64,
        "restoreValidationReceipt": {"payload": {"revision": 110}, "signature": "opaque"},
        "abiValidationMode": "inline-physical-with-initial-lease",
        "freshPhysicalAbiValidationPerformed": True,
        "abiLeaseRevision": 109,
    }
    bridge._shared_shader_replacements = {row["expectedOriginalResourceId"]: row}
    params = {"expected_capture_id": row["captureId"],
              "expected_capture_session_id": row["captureSessionId"]}
    with mock.patch.object(bridge, "_require_capture_identity", return_value=row["captureId"]), \
            mock.patch.object(bridge, "_request_capture_loaded", return_value=True), \
            mock.patch.object(bridge, "_current_capture_token", return_value=token), \
            mock.patch.object(bridge, "_replay", side_effect=AssertionError("readback must not replay")):
        yield bridge, row, params


def test_default_inventory_stays_small_and_omits_private_paths(inventory):
    bridge, unused, params = inventory
    result = bridge._get_shared_shader_replacements(params)
    assert result["replayMutation"] is False
    assert "restoreValidationReceipt" not in result["replacements"][0]
    assert "candidatePath" not in result["replacements"][0]
    assert result["replacements"][0]["postApplyValidationFailed"] is False


def test_current_instance_inventory_does_not_need_an_initial_catalog_id(inventory):
    bridge, row, unused = inventory
    result = bridge._get_shared_shader_replacements({})
    assert not result['replayMutation'] and result['count'] == 1
    assert result['captureSessionId'] == row['captureSessionId']
    with pytest.raises(ValueError):
        bridge._get_shared_shader_replacements({'expected_capture_id':'invalid'})


def test_default_inventory_exposes_failed_native_validation_without_full_receipts(inventory):
    bridge, row, params = inventory
    row["postApplyValidationFailed"] = True
    observed, = bridge._get_shared_shader_replacements(params)["replacements"]
    assert observed["postApplyValidationFailed"] is True
    assert "restoreValidationReceipt" not in observed


def test_exact_lease_metadata_does_not_copy_full_proofs_or_unrelated_workspaces(inventory):
    bridge, row, params = inventory
    row.update(abiLeaseRequestId="5" * 64, abiLeaseReportSha256="6" * 64,
               abiLeaseCandidateRawSha256="2" * 64)
    # An intentionally non-JSON proof proves lease readback never traverses it.
    row["restoreValidationReceipt"] = object()
    other = dict(row, workspaceId="f" * 32, replacementId="a" * 32,
                 expectedOriginalResourceId=555)
    bridge._shared_shader_replacements[555] = other
    result = bridge._get_shared_shader_replacements({
        **params, "lease_metadata_only": True, "workspace_id": row["workspaceId"],
        "replacement_id": row["replacementId"],
    })
    assert result["leaseMetadataOnly"] is True
    assert result["queryWorkspaceId"] == row["workspaceId"]
    assert result["queryReplacementId"] == row["replacementId"]
    assert result["count"] == 1
    observed, = result["replacements"]
    assert observed["abiLeaseRevision"] == 109
    assert observed["abiLeaseRequestId"] == row["abiLeaseRequestId"]
    assert "restoreValidationReceipt" not in observed
    assert "abiReportSha256" not in observed
    assert "freshPhysicalAbiValidationPerformed" not in observed


@pytest.mark.parametrize("extra", [
    {"lease_metadata_only": True},
    {"lease_metadata_only": True, "workspace_id": "e" * 32},
    {"lease_metadata_only": True, "workspace_id": "e" * 32,
     "replacement_id": "c" * 32, "include_validation_receipts": True},
    {"lease_metadata_only": "true"},
])
def test_lease_readback_requires_exact_scope_and_small_response(inventory, extra):
    bridge, unused, params = inventory
    with pytest.raises(ValueError):
        bridge._get_shared_shader_replacements({**params, **extra})


def test_requested_proof_is_actual_physical_history_without_new_validation(inventory):
    bridge, row, params = inventory
    result = bridge._get_shared_shader_replacements({**params, "include_validation_receipts": True})
    observed = result["replacements"][0]
    assert observed["restoreValidationReceipt"] == row["restoreValidationReceipt"]
    assert observed["revision"] == 110
    assert observed["abiLeaseRevision"] == 109
    assert observed["operationId"] == row["operationId"]
    assert observed["freshPhysicalAbiValidationPerformed"] is True
    assert observed["captureSessionId"] == row["captureSessionId"]
    assert observed["schema"] == "renderdoc-ultra-shared-shader-replacement/v1"
    assert "candidatePath" not in observed
    assert bridge._shared_shader_mutation_epoch == 7
    observed["restoreValidationReceipt"]["payload"]["revision"] = 999
    assert row["restoreValidationReceipt"]["payload"]["revision"] == 110


def test_missing_proof_remains_missing(inventory):
    bridge, row, params = inventory
    del row["restoreValidationReceipt"]
    result = bridge._get_shared_shader_replacements({**params, "include_validation_receipts": True})
    assert "restoreValidationReceipt" not in result["replacements"][0]
    assert "restoredOriginalResourceId" not in result["replacements"][0]


def test_verified_restore_mapping_is_opt_in_exact_readback(inventory):
    bridge, row, params = inventory
    row["restoredOriginalResourceId"] = 1000000000000000555
    assert "restoredOriginalResourceId" not in bridge._get_shared_shader_replacements(
        params)["replacements"][0]
    result = bridge._get_shared_shader_replacements(
        dict(params, include_validation_receipts=True, include_all_capture_catalogs=True))
    assert result["replacements"][0]["restoredOriginalResourceId"] == 1000000000000000555
    assert result["replacements"][0]["originalResourceId"] == row["expectedOriginalResourceId"]
    assert bridge._shared_shader_mutation_epoch == 7


@pytest.mark.parametrize("field,value", [("captureId", "f" * 64),
                                         ("captureSessionId", "f" * 32),
                                         ("captureToken", ("other", "path"))])
def test_foreign_lifecycle_record_never_exposes_its_proof(inventory, field, value):
    bridge, row, params = inventory
    row[field] = value
    assert bridge._get_shared_shader_replacements({**params, "include_validation_receipts": True})["count"] == 0


@pytest.mark.parametrize("value", [1, "true", None])
def test_proof_readback_option_requires_boolean(inventory, value):
    bridge, unused, params = inventory
    with pytest.raises(ValueError, match="boolean"):
        bridge._get_shared_shader_replacements({**params, "include_validation_receipts": value})


def test_fresh_proof_retains_exact_profile_token():
    bridge = _load_bridge()
    bridge._capture_session_id = "a" * 32
    bridge._known_catalog_identity_pairs = {("capture", "catalog")}
    row = {"captureSessionId": "a" * 32, "physicalAbiSha256": "3" * 64, "api": "d3d11"}
    payload = {"encoding": "dxbc"}
    params = {"expected_source_sha256": "1" * 64,
              "workspace_catalog_binding": {"projection": {}, "scope": {}},
              "expected_profile": "ps_5_0"}
    store = SimpleNamespace(seal=lambda value: {"payload": deepcopy(value)})
    with mock.patch.object(bridge, "_shader_restore_binding", return_value=payload), \
            mock.patch.object(bridge, "_shader_restore_catalog_pairs", return_value=(("capture", "catalog"), ("stable", "catalog"))), \
            mock.patch.object(bridge, "_current_capture_token", return_value=("capture", "path")), \
            mock.patch.object(bridge, "_shader_restore_receipt_store", store), \
            mock.patch.object(bridge, "_shader_restore_capture_digests", SimpleNamespace(digest=lambda *args: "7" * 64)):
        bridge._attach_shader_restore_receipt(row, params, None)
    assert row["restoreValidationReceipt"]["payload"]["targetProfile"] == "ps_5_0"


def test_editor_inventory_before_catalog_registration_is_explicit_absence(inventory):
    bridge, row, params = inventory
    bridge._known_catalog_capture_ids.clear()
    bridge._shared_shader_replacements.clear()
    result = bridge._get_shared_shader_replacements(params)
    assert result["catalogReady"] is False
    assert result["count"] == 0 and result["replacements"] == []
    assert result["replayMutation"] is False
    bridge._require_capture_identity.assert_not_called()
    bridge._shared_shader_replacements[1] = row
    with pytest.raises(RuntimeError, match="lost its catalog identity"):
        bridge._get_shared_shader_replacements(params)


def test_editor_inventory_cannot_hide_wrong_session_or_closed_capture(inventory):
    bridge, unused, params = inventory
    bridge._known_catalog_capture_ids.clear()
    bridge._shared_shader_replacements.clear()
    with pytest.raises(RuntimeError, match="session changed"):
        bridge._get_shared_shader_replacements({**params, "expected_capture_session_id": "f" * 32})
    bridge._request_capture_loaded.return_value = False
    with pytest.raises(RuntimeError, match="No capture loaded"):
        bridge._get_shared_shader_replacements(params)


def test_ownership_query_spans_catalogs_but_keeps_owner_identity(inventory):
    bridge, row, params = inventory
    other_catalog = "f" * 64
    bridge._known_catalog_capture_ids[other_catalog] = "path"
    request = dict(params, expected_capture_id=other_catalog)
    assert bridge._get_shared_shader_replacements(request)["replacements"] == []
    result = bridge._get_shared_shader_replacements(
        dict(request, include_all_capture_catalogs=True))
    assert result["captureId"] == other_catalog
    assert result["replacements"][0]["captureId"] == row["captureId"]
    assert result["replacements"][0]["captureSessionId"] == row["captureSessionId"]
    assert result["replacements"][0]["workspaceId"] == row["workspaceId"]
    assert result["replayMutation"] is False
    for field, value in (("captureSessionId", "f" * 32),
                         ("captureToken", ("other", "path"))):
        original = row[field]
        row[field] = value
        assert bridge._get_shared_shader_replacements(
            dict(request, include_all_capture_catalogs=True))["count"] == 0
        row[field] = original


def test_all_catalog_inventory_requires_registered_query_and_boolean(inventory):
    bridge, unused, params = inventory
    with pytest.raises(ValueError, match="boolean"):
        bridge._get_shared_shader_replacements(dict(params, include_all_capture_catalogs=1))
    with pytest.raises(RuntimeError, match="current catalog identity"):
        bridge._get_shared_shader_replacements(dict(
            params, expected_capture_id="f" * 64, include_all_capture_catalogs=True))
