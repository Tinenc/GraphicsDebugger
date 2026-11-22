import hashlib
import json

import pytest

from src.shader_organization_review import review_state, track_result
from src.shader_semantic_delivery import delivery_state, publication_state, save_display_identity


SOURCE_SHA = "a" * 64
CANDIDATE_SHA = "b" * 64


def publish(root, **changes):
    receipt = {
        "schema": "replaylab-cbuffer-name-publication-receipt/v1",
        "semanticSourceSha256": SOURCE_SHA,
        "activeDisplayShaderRawSha256": CANDIDATE_SHA,
        "shaderRawSha256": "d" * 64,
        "applied": True, "partial": False,
        "counts": {"requestedSemanticFields": 3, "requestedDisplaySemanticFields": 3, "writtenDisplaySemanticFields": 2,
                   "keptUserDisplaySemanticFields": 1},
        "bindingCoverage": {"schema": "bread-cbuffer-binding-coverage/v1", "complete": True,
                       "bindings": [{"bind": 5, "space": 0, "status": "complete", "missingOffsets": []}]},
        "acceptance": {"requestedSemanticFieldsAccountedFor": True,
                       "currentDisplaySemanticFieldsAccountedFor": True},
        **changes,
    }
    path = root / "ai/cbuffer-name-publication.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt), encoding="utf-8")
    save_display_identity(str(root), {"status": "current", "originalRawSha256": receipt["shaderRawSha256"],
        "activeDisplayShaderRawSha256": receipt["activeDisplayShaderRawSha256"],
        "activeReplacementShaderRawSha256": receipt.get("activeReplacementShaderRawSha256",
            receipt["activeDisplayShaderRawSha256"] if receipt["activeDisplayShaderRawSha256"] != receipt["shaderRawSha256"] else "")})
    return receipt


def test_current_saved_source_and_current_display_receipt_are_independent_requirements(tmp_path):
    contract = {"required": True, "candidateRawSha256": CANDIDATE_SHA}
    assert delivery_state(str(tmp_path), SOURCE_SHA, contract)["reason"] == "publication-receipt-missing"
    publish(tmp_path)
    assert delivery_state(str(tmp_path), SOURCE_SHA, contract)["status"] == "complete"
    assert delivery_state(str(tmp_path), "c" * 64, contract)["reason"] == "publication-source-stale"
    assert delivery_state(str(tmp_path), SOURCE_SHA, {**contract, "candidateRawSha256": "d" * 64})["status"] == "complete"
    publish(tmp_path, semanticSourceSha256=None)
    assert delivery_state(str(tmp_path), SOURCE_SHA, contract)["reason"] == "publication-source-stale"


def test_large_all_binding_receipt_remains_readable_after_publication(tmp_path):
    publish(tmp_path, bindingCoverage={
        "schema": "bread-cbuffer-binding-coverage/v1", "complete": True,
        "bindings": [{"bind": 0, "space": 2, "status": "complete", "missingOffsets": [],
                      "requiredOffsets": list(range(0, 65536, 4)),
                      "semanticOffsets": list(range(0, 65536, 4)),
                      "reviewEvidence": "same indexed field consumers" * 4000}]})
    assert (tmp_path / "ai/cbuffer-name-publication.json").stat().st_size > 256 * 1024
    assert delivery_state(str(tmp_path), SOURCE_SHA, {"required": True})["status"] == "complete"


@pytest.mark.parametrize("changes", [
    {"partial": True}, {"applied": False}, {"acceptance": {}},
    {"acceptance": {"currentDisplaySemanticFieldsAccountedFor": False}},
    {"counts": {"requestedDisplaySemanticFields": 3, "writtenDisplaySemanticFields": 1, "keptUserDisplaySemanticFields": 1}},
    {"counts": {"requestedDisplaySemanticFields": 0, "writtenDisplaySemanticFields": 0, "keptUserDisplaySemanticFields": 0}},
    {"counts": {"requestedDisplaySemanticFields": True, "writtenDisplaySemanticFields": 1, "keptUserDisplaySemanticFields": 0}},
])
def test_applied_or_ordinary_aliases_do_not_hide_incomplete_semantic_display(tmp_path, changes):
    publish(tmp_path, **changes)
    assert delivery_state(str(tmp_path), SOURCE_SHA, {"required": True})["status"] == "pending"


def test_no_requirement_does_not_create_publication_or_io_obligation(tmp_path):
    assert delivery_state(str(tmp_path / "missing"), SOURCE_SHA, {"required": False})["status"] == "not-applicable"


def test_proven_unused_reflection_can_be_reviewed_without_inventing_semantic_fields(tmp_path):
    from src.shader_workspace.semantic_coverage import binding_coverage
    coverage = binding_coverage({"rawSha256": "d" * 64, "blocks": [{"bind": 1, "leaves": [
        {"offset": 16, "byteSize": 8}]}]}, [], [{"bind": 1, "unresolvedOffsets": [],
        "evidence": "Reflected storage retained; current source has no consumers.",
        "unusedOffsets": [16, 20], "unusedEvidence": "Inspected all fixed and dynamic access paths."}])
    receipt = publish(tmp_path, bindingCoverage=coverage, counts={
        "requestedSemanticFields": 0, "requestedDisplaySemanticFields": 0,
        "writtenDisplaySemanticFields": 0, "keptUserDisplaySemanticFields": 0})
    result = publication_state(receipt, SOURCE_SHA, CANDIDATE_SHA)
    assert result["status"] == "complete"
    assert result["bindingCoverage"]["namedLaneCount"] == 0
    assert result["bindingCoverage"]["reviewedUnusedLaneCount"] == 2


def test_typed_original_semantics_do_not_require_a_replacement_mirror(tmp_path):
    receipt = publish(tmp_path, shaderRawSha256=CANDIDATE_SHA,
        counts={"requestedSemanticFields": 3, "writtenSemanticFields": 3,
                "requestedDisplaySemanticFields": 0, "writtenDisplaySemanticFields": 0,
                "keptUserDisplaySemanticFields": 0})
    assert publication_state(receipt, SOURCE_SHA)["status"] == "complete"
    receipt["activeDisplayShaderRawSha256"] = "e" * 64
    assert publication_state(receipt, SOURCE_SHA)["status"] == "pending"


@pytest.mark.parametrize("display_candidate", [False, True])
def test_verified_owner_not_reflection_lookup_proves_current_candidate(tmp_path, display_candidate):
    receipt = publish(tmp_path, activeReplacementShaderRawSha256=CANDIDATE_SHA,
        activeDisplayShaderRawSha256=CANDIDATE_SHA if display_candidate else "d" * 64)
    if not display_candidate:
        receipt["counts"].update(requestedDisplaySemanticFields=0,
                                writtenDisplaySemanticFields=0, keptUserDisplaySemanticFields=0)
    assert publication_state(receipt, SOURCE_SHA, CANDIDATE_SHA)["status"] == "complete"
    assert publication_state(receipt, SOURCE_SHA, "c" * 64)["status"] == "complete"
    original_display = receipt["activeDisplayShaderRawSha256"]
    receipt["activeDisplayShaderRawSha256"] = "f" * 64
    assert publication_state(receipt, SOURCE_SHA, CANDIDATE_SHA)["status"] == "pending"
    receipt["activeDisplayShaderRawSha256"] = original_display
    # Explicitly absent owner cannot fall back to even a candidate-like display.
    receipt["activeReplacementShaderRawSha256"] = ""
    assert publication_state(receipt, SOURCE_SHA, CANDIDATE_SHA)["status"] == ("pending" if display_candidate else "complete")


@pytest.mark.parametrize("owner", [None, "", "BAD", "b" * 63])
def test_original_or_malformed_owner_cannot_claim_candidate_apply(tmp_path, owner):
    receipt = publish(tmp_path, activeReplacementShaderRawSha256=owner)
    assert publication_state(receipt, SOURCE_SHA, CANDIDATE_SHA)["status"] == "pending"
    receipt.pop("activeReplacementShaderRawSha256")
    receipt["shaderRawSha256"] = CANDIDATE_SHA
    state = publication_state(receipt, SOURCE_SHA, CANDIDATE_SHA)
    assert state["status"] == "complete" and state["shaderExecutionModified"] is False
    receipt.pop("shaderRawSha256")
    assert publication_state(receipt, SOURCE_SHA, CANDIDATE_SHA)["status"] == "pending"


def test_preserved_user_semantic_fields_are_delivered_without_overwriting_them(tmp_path):
    receipt = publish(tmp_path, counts={"requestedSemanticFields": 3, "writtenSemanticFields": 0,
        "keptUserSemanticFields": 3, "requestedDisplaySemanticFields": 3,
        "writtenDisplaySemanticFields": 0, "keptUserDisplaySemanticFields": 3})
    assert publication_state(receipt, SOURCE_SHA, CANDIDATE_SHA)["status"] == "complete"


def test_compile_is_not_display_mutation_but_actual_display_change_invalidates_receipt(tmp_path):
    publish(tmp_path)
    contract = {"required": True, "candidateRawSha256": CANDIDATE_SHA}
    manifest = {"latestCompiled": {"sourceSha256": SOURCE_SHA, "rawSha256": "c" * 64}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    result = delivery_state(str(tmp_path), SOURCE_SHA, contract)
    assert result["status"] == "complete"
    assert result["candidateRawSha256"] == "c" * 64
    save_display_identity(str(tmp_path), {"status": "current", "originalRawSha256": "d" * 64,
        "activeDisplayShaderRawSha256": "c" * 64, "activeReplacementShaderRawSha256": "c" * 64})
    assert delivery_state(str(tmp_path), SOURCE_SHA, contract)["reason"] == "publication-display-target-stale"
    publish(tmp_path, activeDisplayShaderRawSha256="c" * 64)
    assert delivery_state(str(tmp_path), SOURCE_SHA, contract)["status"] == "complete"
    # Session routing and live numeric edits do not change semantic source or
    # compiled bytes and must not force a new naming pass or compile.
    manifest["capture"] = {"captureSessionId": "new-session"}
    manifest["cbufferOverrides"] = {"roughness": 0.25}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert delivery_state(str(tmp_path), SOURCE_SHA, contract)["status"] == "complete"


def test_unreadable_current_candidate_cannot_keep_old_display_complete(tmp_path):
    publish(tmp_path)
    (tmp_path / "manifest.json").write_text("{")
    assert delivery_state(str(tmp_path), SOURCE_SHA, {"required": True})["reason"] == "current-original-identity-unavailable"


def test_unapplied_candidate_does_not_invalidate_all_binding_original_publication(tmp_path):
    original = "d" * 64
    publish(tmp_path, shaderRawSha256=original, activeDisplayShaderRawSha256=original,
        activeReplacementShaderRawSha256="", counts={"requestedSemanticFields": 7975,
            "writtenSemanticFields": 7975, "requestedDisplaySemanticFields": 0,
            "writtenDisplaySemanticFields": 0, "keptUserDisplaySemanticFields": 0},
        bindingCoverage={"schema": "bread-cbuffer-binding-coverage/v1", "complete": True,
            "bindings": [{"bind": index, "space": 0, "status": "complete", "missingOffsets": []}
                         for index in range(7)]})
    manifest = {"baseline": {"rawSha256": original}, "latestCompiled": {
        "sourceSha256": SOURCE_SHA, "rawSha256": CANDIDATE_SHA}}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    state = delivery_state(str(tmp_path), SOURCE_SHA, {"required": True})
    assert state["status"] == "complete"
    assert state["displayOnly"] is True and state["shaderExecutionModified"] is False
    assert state["activeDisplayShaderRawSha256"] == original
    # A real different captured original still invalidates the publication.
    manifest["baseline"]["rawSha256"] = "e" * 64
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert delivery_state(str(tmp_path), SOURCE_SHA, {"required": True})["reason"] == "publication-display-target-stale"


def test_missing_current_display_observation_requests_metadata_refresh_not_recompile(tmp_path):
    receipt = publish(tmp_path)
    (tmp_path / "ai/cbuffer-display-identity.json").unlink()
    pending = delivery_state(str(tmp_path), SOURCE_SHA, {"required": True})
    assert pending["reason"] == "publication-display-identity-refresh-required"
    assert pending["recompileRequired"] is False
    assert pending["tool"] == "publish_cbuffer_aliases"
    save_display_identity(str(tmp_path), {"status": "current", "originalRawSha256": receipt["shaderRawSha256"],
        "activeDisplayShaderRawSha256": receipt["activeDisplayShaderRawSha256"],
        "activeReplacementShaderRawSha256": CANDIDATE_SHA})
    assert delivery_state(str(tmp_path), SOURCE_SHA, {"required": True})["status"] == "complete"


@pytest.mark.parametrize("name,envelope", [
    ("get_shader_workspace_status", True), ("compile_shader_workspace", True),
    ("apply_shader_workspace_shared_resource", False),
])
def test_non_finish_routes_refresh_existing_review_obligation_only(tmp_path, name, envelope):
    args = {"workspace_path": str(tmp_path)}
    payload = {"cbufferSemanticDelivery": {"required": True, "candidateRawSha256": "c" * 64}}
    payload = {"result": payload} if envelope else payload
    targets = {}
    track_result(targets, name, payload, args, False)
    assert targets == {}  # Status inspection alone is not a request to edit.
    key = str(tmp_path.resolve()).replace("\\", "/").casefold()
    targets[key] = {"workspacePath": str(tmp_path), "organizationId": "e" * 32,
                    "sourceWorkObserved": True,
                    "cbufferSemanticDelivery": {"required": True, "candidateRawSha256": CANDIDATE_SHA}}
    track_result(targets, name, payload, args, False)
    assert targets[key]["cbufferSemanticDelivery"] == {"required": True, "candidateRawSha256": "c" * 64}
    assert targets[key]["sourceWorkObserved"] is True


def test_ledger_carries_required_effect_across_finish_and_reads_publication_without_tool_polling(tmp_path):
    organization = "e" * 32
    source = b"float4 MainPS() : SV_Target { return 1; }"
    source_sha = hashlib.sha256(source).hexdigest()
    (tmp_path / "source.hlsl").write_bytes(source)
    session_path = tmp_path / "ai/organization-sessions" / organization / "session.json"
    session_path.parent.mkdir(parents=True)
    session_path.write_text(json.dumps({"organizationId": organization, "state": "finished",
        "sourcePath": "source.hlsl", "baselineSourceSha256": source_sha}))
    targets = {}
    payload = {"workspacePath": str(tmp_path), "organizationId": organization,
               "cbufferSemanticDelivery": {"required": True}}
    track_result(targets, "begin_shader_workspace_organization", payload, {}, False)
    track_result(targets, "finish_shader_workspace_organization", {
        **payload, "cbufferSemanticDelivery": {"required": True, "candidateRawSha256": CANDIDATE_SHA}}, {}, False)
    row = review_state(targets)["targets"][0]
    assert row["cbufferSemanticDelivery"]["status"] == "pending"
    publish(tmp_path, semanticSourceSha256=source_sha)
    row = review_state(targets)["targets"][0]
    assert row["cbufferSemanticDelivery"]["status"] == "complete"
    # A later source edit invalidates only the effect's source identity; an old
    # successful display publication does not bless the new working copy.
    (tmp_path / "source.hlsl").write_bytes(source + b"\n// new names")
    assert review_state(targets)["targets"][0]["cbufferSemanticDelivery"]["reason"] == "publication-source-stale"
    # A model cannot drop the established requirement by beginning another draft.
    track_result(targets, "begin_shader_workspace_organization", {
        **payload, "organizationId": "f" * 32, "cbufferSemanticDelivery": {"required": False}}, {}, False)
    assert next(iter(targets.values()))["cbufferSemanticDelivery"]["required"] is True
