"""Non-use stays an explicit scoped observation, separate from parameter names."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from src import server
from src.shader_workspace import aliases
from src.shader_workspace.semantic_usage import SCHEMA, attach_usage_reviews, clean_usage_review
import test_material_naming_mcp as naming_tests

ORIGINAL, ACTIVE, SOURCE = "a" * 64, "b" * 64, "c" * 64


def usage(offsets=(4, 8, 12)):
    return {"schema": SCHEMA, "unusedComponents": [{"offset": value, "type": "float"} for value in offsets],
            "evidence": "All helpers and branches consume only the first scalar; remaining lanes have no consumers."}


def semantic_field():
    return {"name": "roughness", "type": "float", "components": [{"offset": 0}],
            "status": "inferred", "provenance": "shader", "evidence": "Direct roughness consumer."}


def block():
    return {"block": "Material", "bind": 0, "space": 1, "byteSize": 16,
            "members": [{"name": "values", "offset": 0}],
            "leaves": [{"path": "values", "offset": 0, "byteSize": 16, "baseType": "float"}]}


def scope(raw=ACTIVE):
    value = block()
    return {"shaderRawSha256": raw, "stage": "fragment", "bind": 0, "space": 1,
            "layoutSha256": aliases.layout_sha256(value["block"], value["members"])}


def publish(root, fields=None, review=None, raw=ACTIVE, source=SOURCE, **kwargs):
    value = block()
    return aliases.publish_aliases(value["block"], value["members"], [], root=root,
        shader_scope=scope(raw), semantic_fields=fields, semantic_usage_review=review,
        semantic_source_sha256=source, semantic_captured_shader_sha256=ORIGINAL, **kwargs)


def read(root, raw=ACTIVE):
    return aliases._read_semantic_views(root / (scope()["layoutSha256"] + ".json"), shader_scope=scope(raw))


@pytest.mark.parametrize("change", [
    {"unusedComponents": [{"offset": 4, "type": "unknown"}]},
    {"unusedComponents": [{"offset": True, "type": "float"}]},
    {"unusedComponents": [{"offset": 2, "type": "float"}]},
    {"unusedComponents": [{"offset": 4, "type": "float"}] * 2},
    {"evidence": ""}, {"schema": "other"},
])
def test_usage_metadata_has_bounded_distinct_typed_lanes(change):
    with pytest.raises(ValueError):
        clean_usage_review({**usage(), **change})


def test_view_persists_typed_review_under_exact_scope_and_source(tmp_path):
    publish(tmp_path, [semantic_field()], usage())
    view = read(tmp_path)[0]
    assert view["usageReview"] == usage()
    assert view["sourceSha256"] == SOURCE
    assert view["capturedShaderRawSha256"] == ORIGINAL
    assert read(tmp_path, ORIGINAL) == []
    assert view["fields"] == [{**semantic_field(), "description": ""}]


def test_review_only_update_preserves_same_source_names(tmp_path):
    publish(tmp_path, [semantic_field()])
    publish(tmp_path, review=usage())
    assert len(read(tmp_path)[0]["fields"]) == 1
    assert read(tmp_path)[0]["usageReview"] == usage()


def test_new_source_cannot_inherit_an_old_usage_status(tmp_path):
    publish(tmp_path, [semantic_field()], usage())
    publish(tmp_path, [semantic_field()], source="d" * 64)
    assert "usageReview" not in read(tmp_path)[0]
    # A new-source review-only update also does not borrow old semantic names.
    publish(tmp_path, review=usage((0, 4, 8, 12)), source="e" * 64)
    assert read(tmp_path)[0]["fields"] == []


def test_wholly_unused_view_is_readable_and_old_versions_are_reclaimed(tmp_path):
    publish(tmp_path, review=usage((0, 4, 8, 12)), raw="d" * 64)
    publish(tmp_path, review=usage((0, 4, 8, 12)), semantic_retention={
        "protectedShaderRawSha256": [ORIGINAL, ACTIVE], "keepPrevious": 0})
    assert read(tmp_path)[0]["fields"] == []
    assert read(tmp_path, "d" * 64) == []


def test_usage_requires_current_source_and_disjoint_names(tmp_path):
    with pytest.raises(ValueError, match="current source"):
        publish(tmp_path, review=usage(), source="")
    with pytest.raises(ValueError, match="overlap"):
        publish(tmp_path, [semantic_field()], usage((0,)))


def test_user_view_wins_over_ai_usage_update(tmp_path):
    value = block()
    aliases.publish_aliases(value["block"], value["members"], [], source="user", root=tmp_path,
        shader_scope=scope(), semantic_fields=[semantic_field()], semantic_source_sha256=SOURCE)
    result = publish(tmp_path, review=usage())
    assert result["semanticViewKeptUser"]
    assert "usageReview" not in read(tmp_path)[0]


def test_malformed_saved_review_does_not_mark_parameters_unused(tmp_path):
    publish(tmp_path, [semantic_field()], usage())
    path = tmp_path / (scope()["layoutSha256"] + ".json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["semanticViews"][0]["usageReview"]["unusedComponents"][0]["type"] = "unknown"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert "usageReview" not in read(tmp_path)[0]
    assert len(read(tmp_path)[0]["fields"]) == 1


def prepared_identity():
    return {"rawSha256": ORIGINAL, "activeDisplayShaderRawSha256": ACTIVE,
            "shaderResourceId": "123", "stage": "fragment", "blocks": [block()],
            "activeDisplayBlocks": [block()]}


def coverage():
    return {"bindings": [{"bind": 0, "space": 1, "unusedOffsets": [4, 8, 12],
            "unusedEvidence": usage()["evidence"], "reviewEvidence": "All branches reviewed.",
            "unresolvedOffsets": [], "semanticOffsets": [0]}]}


@pytest.mark.parametrize("change", ["type", "padding", "overlap", "unknown", "partial"])
def test_original_mirror_cannot_claim_mismatched_layout_lanes_unused(change):
    identity = prepared_identity()
    leaf = identity["blocks"][0]["leaves"][0]
    if change == "type":
        leaf["baseType"] = "uint"
    elif change == "padding":
        leaf["componentOffsets"] = [0, 4, 8]
    elif change == "overlap":
        identity["blocks"][0]["leaves"].append(copy.deepcopy(leaf))
    elif change == "unknown":
        leaf["semanticMappingAvailable"] = False
    else:
        identity["blocks"][0]["memberMappingComplete"] = False
    publications = []
    skipped = attach_usage_reviews(identity, coverage(), publications, SOURCE, {(0, 1)})
    assert len(skipped) == 1 and skipped[0]["shaderRawSha256"] == ORIGINAL
    assert len(publications) == 1
    assert publications[0]["shaderScope"]["shaderRawSha256"] == ACTIVE
    assert publications[0]["semanticUsageReview"] == usage()


def test_unknown_offsets_never_become_unused_or_padding():
    reviewed = coverage()
    reviewed["bindings"][0].update(unusedOffsets=[4], unresolvedOffsets=[8, 12])
    publications = []
    assert attach_usage_reviews(prepared_identity(), reviewed, publications, SOURCE, {(0, 1)}) == []
    assert len(publications) == 2
    assert all(item["semanticUsageReview"]["unusedComponents"] == [{"offset": 4, "type": "float"}]
               for item in publications)


def test_unbound_legacy_source_does_not_publish_usage():
    publications = []
    assert attach_usage_reviews(prepared_identity(), coverage(), publications, "", {(0, 1)}) == []
    assert publications == []


def test_usage_attached_to_existing_row_publication_carries_source_binding():
    publications = [{"bind": 0, "space": 1, "block": "Material", "members": block()["members"],
                     "entries": [{"path": "values", "offset": 0, "alias": "controls"}],
                     "shaderScope": scope(ORIGINAL)}]
    attach_usage_reviews(prepared_identity(), coverage(), publications, SOURCE, {(0, 1)})
    assert all(item["semanticSourceSha256"] == SOURCE
               and item["semanticCapturedShaderSha256"] == ORIGINAL for item in publications)


def test_revised_unknown_review_clears_old_unused_status(tmp_path):
    publish(tmp_path, [semantic_field()], usage())
    reviewed = coverage()
    reviewed["bindings"][0].update(unusedOffsets=[], unresolvedOffsets=[4, 8, 12], unusedEvidence="")
    publications = []
    attach_usage_reviews(prepared_identity(), reviewed, publications, SOURCE, {(0, 1)})
    active = next(item for item in publications if item["shaderScope"]["shaderRawSha256"] == ACTIVE)
    publish(tmp_path, review=active["semanticUsageReview"])
    assert read(tmp_path)[0]["usageReview"]["unusedComponents"] == []
    assert len(read(tmp_path)[0]["fields"]) == 1


@pytest.fixture
def server_case(monkeypatch):
    case = naming_tests.CBufferAliasPublicationMCPTests()
    case.setUp()
    source = b"void main() {}\n"
    (case.workspace / "shader.glsl").write_bytes(source)
    case.source_sha = hashlib.sha256(source).hexdigest()
    case.block = block()
    case.identity.update(prepared_identity())
    monkeypatch.setattr(server, "_alias_publication_workspace_status", lambda unused: {
        "workspacePath": str(case.workspace), "workspaceId": "workspace-a",
        "source": {"path": "shader.glsl"}})
    monkeypatch.setattr(server, "_active_project_service",
        lambda: SimpleNamespace(replay_service=lambda: None))
    # Cleanup authority is independent: an unavailable registry must not erase
    # successful names or explicit usage-review publication.
    monkeypatch.setattr(server.ipc, "call", lambda *args, **kwargs: {})
    try:
        yield case
    finally:
        # setUp installed unittest patches first. Remove the nested pytest
        # patches before doCleanups, otherwise pytest restores those mocks
        # after unittest restored production functions, leaking into later tests.
        monkeypatch.undo()
        case.tearDown()
        case.doCleanups()


def test_host_publishes_typed_usage_in_same_atomic_alias_transaction(server_case):
    case = server_case
    result = case.call_publish([], semantic_fields=[{**semantic_field(), "bind": 0, "space": 1}],
        semantic_source_sha256=case.source_sha, binding_reviews=[{
            "bind": 0, "space": 1, "unresolvedOffsets": [], "evidence": "Reviewed all consumers.",
            "unusedOffsets": [4, 8, 12], "unusedEvidence": usage()["evidence"]}])
    assert "error" not in result, result
    assert result["bindingCoverage"]["namingComplete"]
    assert result["usageReviewSkipped"] == []
    views = aliases._read_semantic_views(aliases.layout_alias_path(scope()["layoutSha256"]))
    assert {view["shaderScope"]["shaderRawSha256"] for view in views} == {ORIGINAL, ACTIVE}
    assert all(view["usageReview"] == usage() and view["sourceSha256"] == case.source_sha for view in views)


def test_host_review_only_for_wholly_unused_binding_is_visible(server_case):
    case = server_case
    result = case.call_publish([], semantic_source_sha256=case.source_sha, binding_reviews=[{
        "bind": 0, "space": 1, "unresolvedOffsets": [], "evidence": "All branches reviewed.",
        "unusedOffsets": [0, 4, 8, 12], "unusedEvidence": "This entry has no consumer of this declared block."}])
    assert "error" not in result, result
    assert result["applied"] and result["counts"]["writtenSemanticFields"] == 0
    views = aliases._read_semantic_views(aliases.layout_alias_path(scope()["layoutSha256"]))
    assert len(views) == 2 and all(view["fields"] == [] for view in views)
    assert all(len(view["usageReview"]["unusedComponents"]) == 4 for view in views)
