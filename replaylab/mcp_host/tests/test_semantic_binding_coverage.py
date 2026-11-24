import hashlib

import pytest

from src.shader_workspace.semantic_coverage import binding_coverage, coverage_state, source_inventory
from src.shader_workspace.naming_delivery import semantic_delivery


def identity():
    return {"rawSha256": "a" * 64, "activeDisplayShaderRawSha256": "b" * 64,
            "blocks": [{"bind": 5, "space": 0, "leaves": [{"offset": 0, "byteSize": 65536}]}],
            "activeDisplayBlocks": [
                {"bind": bind, "space": 0, "block": "CB%d" % bind,
                 "leaves": [{"offset": 16, "byteSize": 8}]} for bind in (1, 3, 5)]}


def fields(bind, offsets=(16, 20)):
    return [{"bind": bind, "space": 0, "name": "test", "components": [{"offset": offset} for offset in offsets]}]


def test_one_material_block_does_not_complete_view_and_lighting_blocks():
    value = binding_coverage(identity(), fields(5), [])
    assert value["bindingCount"] == 3
    assert value["reviewedBindingCount"] == 1
    assert value["pendingBindings"] == [{"bind": 1, "space": 0, "missingLaneCount": 2},
                                         {"bind": 3, "space": 0, "missingLaneCount": 2}]
    assert coverage_state({"bindingCoverage": value})["complete"] is False


def test_each_physical_lane_is_accounted_and_unknown_stays_distinct_from_named():
    value = binding_coverage(identity(), fields(1) + fields(3) + fields(5, (16,)),
        [{"bind": 5, "unresolvedOffsets": [20], "evidence": "Branch selector read is understood; original mode enum unavailable"}])
    assert value["complete"] is True
    assert value["unresolvedLaneCount"] == 1
    assert value["bindings"][-1]["semanticOffsets"] == [16]
    assert value["bindings"][-1]["unresolvedOffsets"] == [20]
    assert value["namingComplete"] is False
    assert value["namedLaneCount"] == 5
    assert value["reviewedUnusedLaneCount"] == 0
    checked = coverage_state({"bindingCoverage": value})
    assert checked["complete"] is True and checked["namingComplete"] is False


def test_proven_unused_lanes_are_distinct_from_missing_or_unknown_names():
    value = binding_coverage(identity(), fields(1) + fields(3) + fields(5, (16,)), [{
        "bind": 5, "unresolvedOffsets": [], "evidence": "The current source consumes only the first lane.",
        "unusedOffsets": [20], "unusedEvidence": "All accesses are fixed member x; all branches inspected."}])
    assert value["complete"] is True and value["namingComplete"] is True
    assert value["namedLaneCount"] == 5 and value["reviewedUnusedLaneCount"] == 1
    assert value["unresolvedLaneCount"] == 0
    checked = coverage_state({"bindingCoverage": value})
    assert checked["namingComplete"] is True and checked["reviewedUnusedLaneCount"] == 1
    retained = binding_coverage(identity(), fields(1), [], value)
    assert retained["bindings"][-1]["unusedOffsets"] == [20]
    assert retained["namingComplete"] is True


@pytest.mark.parametrize("changes", [
    {"unusedEvidence": ""}, {"unusedOffsets": [20, 20]}, {"unusedOffsets": [True]},
    {"unusedOffsets": [16]}, {"unusedOffsets": [24]}, {"unresolvedOffsets": [20]},
])
def test_unused_review_requires_evidence_and_disjoint_physical_lanes(changes):
    review = {"bind": 5, "unresolvedOffsets": [], "evidence": "Reviewed actual consumers.",
              "unusedOffsets": [20], "unusedEvidence": "No reachable access in any current branch.", **changes}
    with pytest.raises(ValueError):
        binding_coverage(identity(), fields(5, (16,)), [review])


@pytest.mark.parametrize("changes", [
    {"unusedOffsets": [20], "unusedEvidence": ""},
    {"semanticOffsets": None}, {"semanticOffsets": [16, 20, 20]},
    {"semanticOffsets": [True]}, {"semanticOffsets": [16]},
    {"memberMappingIssueCount": 1},
])
def test_malformed_receipt_cannot_claim_all_names_complete(changes):
    coverage = binding_coverage(identity(), fields(1) + fields(3) + fields(5), [])
    coverage["bindings"][-1].update(changes)
    result = coverage_state({"bindingCoverage": coverage})
    assert result["complete"] is False and result["namingComplete"] is False


def test_legacy_coverage_without_lane_details_never_claims_all_names_recovered():
    result = coverage_state({"bindingCoverage": {"schema": "bread-cbuffer-binding-coverage/v1",
        "complete": True, "bindings": [{"bind": 1, "status": "complete", "missingOffsets": []}]}})
    assert result["complete"] is True and result["namingComplete"] is False


@pytest.mark.parametrize("review", [
    {"bind": 5, "unresolvedOffsets": [16], "evidence": "overlaps renamed lane"},
    {"bind": 5, "unresolvedOffsets": [24], "evidence": "outside reflected binding"},
    {"bind": 5, "unresolvedOffsets": [20], "evidence": ""},
    {"bind": 2, "unresolvedOffsets": [20], "evidence": "wrong block"},
    {"bind": True, "unresolvedOffsets": [20], "evidence": "not an integer"},
])
def test_unknown_review_cannot_hide_unrelated_or_unnamed_uninspected_bytes(review):
    with pytest.raises(ValueError):
        binding_coverage(identity(), fields(5, (16,)), [review])


def test_sequential_publications_retain_untouched_blocks_only_for_same_layout():
    old = binding_coverage(identity(), fields(5), [])
    complete = binding_coverage(identity(), fields(1) + fields(3), [], old)
    assert complete["complete"]
    changed = identity()
    changed["activeDisplayBlocks"][-1]["leaves"][0]["byteSize"] = 12
    assert binding_coverage(changed, fields(1) + fields(3), [], old)["complete"] is False


def test_replacing_one_block_view_drops_its_old_accounted_lanes():
    old = binding_coverage(identity(), fields(1) + fields(3) + fields(5), [])
    result = binding_coverage(identity(), fields(5, (16,)), [], old)
    assert result["pendingBindings"] == [{"bind": 5, "space": 0, "missingLaneCount": 1}]


def test_partial_reflection_cannot_complete_from_only_surviving_named_lanes():
    value = identity()
    value["activeDisplayBlocks"][-1].update(
        memberMappingComplete=False,
        memberMappingIssues=[{"path": "instances[1].weight", "reason": "field-outside-parent"}],
        memberMappingIssueCount=1)
    coverage = binding_coverage(value, fields(1) + fields(3) + fields(5), [])
    assert coverage["complete"] is False
    pending = coverage["pendingBindings"]
    assert len(pending) == 1 and pending[0]["bind"] == 5
    assert pending[0]["missingLaneCount"] == 0
    assert pending[0]["reason"] == "reflected-member-mapping-incomplete"
    assert pending[0]["mappingIssues"] == [
        {"path": "instances[1].weight", "reason": "field-outside-parent"}]
    assert coverage_state({"bindingCoverage": coverage})["complete"] is False


def test_old_review_cannot_cover_newly_omitted_members_with_identical_surviving_offsets():
    old = binding_coverage(identity(), fields(1) + fields(3) + fields(5), [])
    value = identity()
    value["activeDisplayBlocks"][-1]["memberMappingComplete"] = False
    coverage = binding_coverage(value, [], [], old)
    assert coverage["complete"] is False
    assert coverage["bindings"][-1]["semanticOffsets"] == [16, 20]
    assert coverage["bindings"][-1]["status"] == "pending"
    # A stale/malformed stored summary cannot override the partial row either.
    coverage["complete"] = True
    coverage["bindings"][-1]["status"] = "complete"
    assert coverage_state({"bindingCoverage": coverage})["complete"] is False


def test_matrix_or_unknown_roles_remain_reviewable_when_physical_mapping_is_complete():
    value = identity()
    value["activeDisplayBlocks"][-1]["leaves"][0].update(semanticMappingAvailable=False)
    coverage = binding_coverage(value, fields(1) + fields(3), [{
        "bind": 5, "unresolvedOffsets": [16, 20],
        "evidence": "A reflected matrix row is retained in the physical view; not regrouped as scalar parameters."}])
    assert coverage["complete"] is True
    assert coverage["unresolvedLaneCount"] == 2


def test_empty_partial_mapping_reports_missing_layout_instead_of_inventing_offsets():
    value = identity()
    value["activeDisplayBlocks"][-1].update(leaves=[], memberMappingComplete=False,
        memberMappingIssues=[{"path": "instances", "reason": "missing-array-layout"}],
        memberMappingIssueCount=1)
    coverage = binding_coverage(value, fields(1) + fields(3), [{
        "bind": 5, "unresolvedOffsets": [], "evidence": "The reflection is missing the array stride."}])
    assert coverage["complete"] is False
    assert coverage["bindings"][-1]["requiredOffsets"] == []
    assert coverage["pendingBindings"][0]["reason"] == "reflected-member-mapping-incomplete"


def test_source_candidates_preserve_exact_lanes_not_math_or_invented_padding():
    source = '''
cbuffer View : register(b1, space2) {
 float x : packoffset(c1.x); float y : packoffset(c1.y); float z : packoffset(c1.z);
 uint flag : packoffset(c3.w);
};
cbuffer Material : register(b5) { float r; };
#define ViewDirection float3(x, y, z)
#define Intensity float4(0.0f, 0.0f, 0.0f, z)
#define PackedFlag flag
#define CrossBlock float2(x, r)
#define Repeated float2(x, x)
#define WrongType uint2(x, y)
#define Computed float2(x * 2, y)
'''
    rows = source_inventory(source)
    assert [(row["bind"], row["space"]) for row in rows] == [(1, 2), (5, 0)]
    candidates = rows[0]["candidates"]
    assert [row["name"] for row in candidates] == ["ViewDirection", "Intensity", "PackedFlag"]
    assert candidates[0]["components"] == [{"offset": 16}, {"offset": 20}, {"offset": 24}]
    assert candidates[1]["type"] == "float" and candidates[1]["sourceChannels"] == "w"
    assert candidates[1]["components"] == [{"offset": 24}]
    assert candidates[2]["type"] == "uint"
    assert all(row["semanticReviewRequired"] for row in candidates)


def test_old_single_block_receipt_cannot_bless_whole_shader():
    source = b"cbuffer C : register(b5) {float x;}"
    receipt = {"schema": "replaylab-cbuffer-name-publication-receipt/v1",
               "semanticSourceSha256": hashlib.sha256(source).hexdigest(),
               "shaderRawSha256": "a" * 64,
               "activeDisplayShaderRawSha256": "b" * 64, "applied": True, "partial": False,
               "counts": {"requestedSemanticFields": 1, "requestedDisplaySemanticFields": 1,
                          "writtenDisplaySemanticFields": 1, "keptUserDisplaySemanticFields": 0},
               "acceptance": {"requestedSemanticFieldsAccountedFor": True, "currentDisplaySemanticFieldsAccountedFor": True}}
    assert semantic_delivery(source, receipt)["reason"] == "all-binding-review-missing"
    receipt["bindingCoverage"] = binding_coverage(identity(), fields(5), [])
    assert semantic_delivery(source, receipt)["reason"] == "all-binding-review-incomplete"


def test_outline_delivers_each_binding_candidates_from_local_source_only(tmp_path, monkeypatch):
    import json
    from src import server
    path = tmp_path / "shader.hlsl"
    path.write_text("cbuffer C : register(b3) {float x;}\n#define intensity x\n")
    monkeypatch.setattr(server, "_workspace_file_context", lambda value: (tmp_path, {"source": {"path": str(path)}}))
    monkeypatch.setattr(server.ipc, "call", lambda *a, **k: pytest.fail("source inventory must not replay"))
    value = json.loads(server.get_shader_workspace_outline(str(tmp_path), section="cbuffer-semantics"))
    assert value["sourceSha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert value["page"]["total"] == 1
    assert value["page"]["items"][0]["bind"] == 3
    assert value["page"]["items"][0]["candidates"][0]["name"] == "intensity"
