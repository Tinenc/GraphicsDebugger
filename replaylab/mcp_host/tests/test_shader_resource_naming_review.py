import copy
import hashlib
import json

import pytest

from src import shader_resource_naming_review as naming
from src.shader_semantic_delivery import delivery_state


SOURCE = b"cbuffer Material:register(b5){float tint;} Texture2D BaseColor:register(t4,space1); Texture2D _32:register(t8);"
TARGET = {"eventId": 77, "stage": "fragment", "rawSha256": "c" * 64, "captureId": "a" * 64}


def prepare(root, configured=False, **extra):
    naming.prepare(root, {"target": TARGET, "assetRootConfigured": configured, **extra})


def review(status="not-configured"):
    return {"assets": {"status": status, "evidence": "Read the configured project asset state", "usage": "Keep the source-derived color role; no recovered asset name is claimed"},
            "textures": [{"selector": "resource:BaseColor", "status": "retained", "evidence": "The sampled RGB feeds the material base color"},
                         {"selector": "resource:_32", "status": "unresolved", "evidence": "No corresponding unpacked texture was identified; preserve its binding"}],
            "reconciliation": "Reviewed both texture roles and every material CBuffer after organizing the calculations"}


def test_public_naming_review_uses_current_glsl_manifest_not_old_hlsl(tmp_path, monkeypatch):
    from src import server

    source = b"#version 450\nlayout(set=2,binding=0) uniform Material { vec4 tint; };\n"
    (tmp_path / "source").mkdir()
    (tmp_path / "source/shader.glsl").write_bytes(source)
    (tmp_path / "source/shader.hlsl").write_bytes(SOURCE)
    (tmp_path / "manifest.json").write_text(json.dumps({"source": {"path": "source/shader.glsl"}}))
    prepare(tmp_path)
    monkeypatch.setattr(server, "_workspace_root_for_files", lambda unused: tmp_path)
    value = review()
    value["textures"] = []
    result = json.loads(server.record_shader_resource_naming_review.__wrapped__(
        str(tmp_path), hashlib.sha256(source).hexdigest(), value))
    assert "error" not in result, result
    assert result["resourceNamingDelivery"]["status"] == "complete"
    assert result["cbufferSemanticDelivery"]["sourceSha256"] == hashlib.sha256(source).hexdigest()
    assert server._shader_cbuffer_semantic_delivery(str(tmp_path))["required"] is True


@pytest.mark.parametrize("source_path", ["../outside.glsl", "", "source/missing.glsl"])
def test_saved_source_resolver_rejects_absent_or_outside_manifest_source(tmp_path, source_path):
    from src.shader_organization_review import canonical_source_path

    (tmp_path / "manifest.json").write_text(json.dumps({"source": {"path": source_path}}))
    with pytest.raises((ValueError, OSError)):
        canonical_source_path(str(tmp_path))


def test_new_workspace_requires_assets_and_every_texture_and_stale_source_invalidates(tmp_path):
    prepare(tmp_path)
    assert naming.delivery_state(tmp_path, SOURCE)["status"] == "pending"
    missing = review()
    missing["textures"].pop()
    with pytest.raises(ValueError, match="every declared"):
        naming.record(tmp_path, SOURCE, missing)
    result = naming.record(tmp_path, SOURCE, review())
    assert result["status"] == "complete" and result["textureCoverage"]["unresolvedCount"] == 1
    assert naming.delivery_state(tmp_path, SOURCE + b"\n//changed")["reason"] == "resource-naming-source-stale"
    before = (tmp_path / naming.PATH).read_bytes()
    prepare(tmp_path, diagnostics=["not an input"], retainedPanelPublication={"written": 99})
    assert (tmp_path / naming.PATH).read_bytes() == before
    naming.prepare(tmp_path, {"target": {**TARGET, "eventId": 78}, "assetRootConfigured": False})
    assert naming.delivery_state(tmp_path, SOURCE)["reason"] == "resource-naming-target-or-assets-changed"


def test_naming_quality_is_agent_review_not_count_based_approval_or_automatic_shortening(tmp_path):
    source = (b"cbuffer Scene:register(b0){float4 weights[8];}; "
              b"Texture2D UserChosenSceneColor:register(t1); "
              b"Texture2D UserChosenHistoryColor:register(t2);")
    prepare(tmp_path)
    # Existing indexed paths and explicit user labels are not targets for a
    # heuristic prefix stripper, even when the next naming review is pending.
    aliases = tmp_path / "user-aliases.json"
    original = json.dumps({"aliases": ["lights[3].color", "lights[7].color"],
                           "userNames": ["UserChosenSceneColor", "UserChosenHistoryColor"]}).encode()
    aliases.write_bytes(original)
    pending = naming.delivery_state(tmp_path, source)
    guidance = pending["namingQualityReview"]
    assert guidance["authority"] == "agent-review-required"
    assert guidance["countsAreCoverageOnly"] is True and guidance["recordIn"] == "reconciliation"
    value = review()
    value["textures"] = [{"selector": "resource:" + name, "status": "retained",
                          "evidence": why} for name, why in (
        ("UserChosenSceneColor", "User-authored label for the current scene contribution"),
        ("UserChosenHistoryColor", "User-authored label; History distinguishes the temporal input"))]
    value["reconciliation"] = ("Retained both user choices. Current scene and temporal history remain distinguishable; "
                                "the indexed light paths still identify different elements. No cosmetic rename is needed.")
    completed = naming.record(tmp_path, source, value)
    assert completed["status"] == "complete"
    assert completed["namingQualityReview"]["authority"] == "agent-review-required"
    saved = json.loads((tmp_path / naming.PATH).read_bytes())["review"]
    assert saved["hostSemanticApproval"] is False
    assert saved["reconciliation"] == value["reconciliation"]
    assert [row["selector"] for row in saved["textures"]] == [row["selector"] for row in value["textures"]]
    assert aliases.read_bytes() == original


def test_missing_metadata_actions_require_role_review_without_fake_semantic_scoring(tmp_path):
    prepare(tmp_path)
    result = naming.aggregate({"required": True, "status": "pending"}, tmp_path, SOURCE)
    assert result["status"] == "pending"
    assert result["resourceNamingDelivery"]["namingQualityReview"]["countsAreCoverageOnly"]
    assert "score" not in result["resourceNamingDelivery"]
    assert {item["tool"] for item in result["nextActions"]} == {
        "publish_cbuffer_aliases", "get_shader_workspace_outline", "record_shader_resource_naming_review"}


def test_configured_assets_cannot_be_skipped_or_self_report_a_match(tmp_path):
    prepare(tmp_path, True, assetRootIdentity="a" * 64)
    with pytest.raises(ValueError, match="cannot be reported not-configured"):
        naming.record(tmp_path, SOURCE, review())
    with pytest.raises(ValueError, match="evidence_paths"):
        naming.record(tmp_path, SOURCE, review("matched"))
    assert naming.record(tmp_path, SOURCE, review("reader-unavailable"))["status"] == "complete"
    prepare(tmp_path, True, assetRootIdentity="b" * 64)
    assert naming.delivery_state(tmp_path, SOURCE)["reason"] == "resource-naming-target-or-assets-changed"


def test_actual_asset_evidence_is_read_and_bound_to_its_target_and_bytes(tmp_path):
    prepare(tmp_path, True)
    path = tmp_path / "ai/asset-resolution.json"
    path.write_text(json.dumps({"shaderRawSha256": TARGET["rawSha256"], "eventId": 77, "stage": "fragment"}))
    value = review("reused")
    value["assets"]["evidence_paths"] = ["ai/asset-resolution.json"]
    assert naming.record(tmp_path, SOURCE, value)["status"] == "complete"
    path.write_text(json.dumps({"shaderRawSha256": "d" * 64}))
    assert naming.delivery_state(tmp_path, SOURCE)["status"] == "pending"
    with pytest.raises(ValueError, match="different captured Shader"):
        naming.record(tmp_path, SOURCE, value)


def test_physical_names_require_explicit_unknown_disposition(tmp_path):
    prepare(tmp_path)
    value = review()
    value["textures"][1]["status"] = "retained"
    with pytest.raises(ValueError, match="physical placeholder"):
        naming.record(tmp_path, SOURCE, value)


def test_real_texture_receipt_without_row_hash_and_different_catalog_ids(tmp_path):
    from src import server
    from src.shader_workspace.resource_name_publication import publication_paths, write_manifest, write_ledger
    prepare(tmp_path)
    operation = "texture-publication"
    manifest_path, ledger_path = publication_paths(tmp_path, operation)
    binding = {"captureId": "b" * 64, "captureSessionId": "d" * 32, "eventId": 77, "stage": "fragment",
               "entryPoint": "MainPS", "shaderRawSha256": TARGET["rawSha256"], "shaderResourceId": 11}
    rows = [{"selector": "resource:BaseColor", "resourceId": 88, "bindNumber": 4, "registerSpace": 1, "alias": "BodyAlbedo"}]
    aliases = server._resource_alias_manifest_rows(rows, binding)
    manifest_sha = write_manifest(manifest_path, {"aliases": aliases})
    result = server._resource_alias_publication_receipt(rows=rows, aliases=aliases, replaced_targets=set(),
        bridge_receipt={"written": [{"resourceId": 88, "displayName": "BodyAlbedo"}]}, binding=binding, ledger_path=ledger_path)
    assert "rawSha256" not in result["rows"][0]
    write_ledger(ledger_path, {"operationId": operation, "workspaceRoot": str(tmp_path), "phase": "committed", "result": result,
                              "manifestSha256": manifest_sha})
    value = review()
    value["textures"][0].update(status="published", operation_id=operation)
    assert naming.record(tmp_path, SOURCE, value)["status"] == "complete"
    altered = json.loads(ledger_path.read_text())
    altered["result"]["eventId"] = 78
    write_ledger(ledger_path, altered)
    assert naming.delivery_state(tmp_path, SOURCE)["status"] == "pending"


def test_missing_or_false_ledger_hint_cannot_skip_new_source_resources(tmp_path):
    source = tmp_path / "shader.hlsl"
    source.write_bytes(SOURCE)
    for contract in ({}, {"required": False}):
        result = delivery_state(str(tmp_path), hashlib.sha256(SOURCE).hexdigest(), contract, source_path=str(source))
        assert result["required"] and result["status"] == "pending"
        assert result["resourceNamingDelivery"]["required"]
        assert {item["tool"] for item in result["nextActions"]} == {"publish_cbuffer_aliases", "get_shader_workspace_outline", "record_shader_resource_naming_review"}


def test_texture_only_shader_has_no_fake_cbuffer_obligation(tmp_path):
    source = b"Texture2D BaseColor:register(t4,space1);"
    prepare(tmp_path)
    value = review()
    value["textures"].pop()
    naming.record(tmp_path, source, value)
    from src.shader_workspace.naming_delivery import semantic_delivery
    result = naming.aggregate(semantic_delivery(source), tmp_path, source)
    assert result["required"] and not result["cbufferRequired"] and result["status"] == "complete"
    assert naming.inventory(b"layout(binding=4,set=2) uniform sampler2D _33;")["textures"][0]["space"] == 2


def test_redirected_write_is_rejected_even_after_failed_read(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    try:
        (workspace / "ai").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError, match="redirected"):
        prepare(workspace)
    assert not (outside / "resource-naming-review.json").exists()
