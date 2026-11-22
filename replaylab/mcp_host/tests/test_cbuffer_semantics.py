"""Semantic CBuffer presentation preserves physical layout and scoped evidence."""

from __future__ import annotations

import copy
import json

import pytest

from src.shader_workspace import aliases
from src.shader_workspace import cbuffer_alias_publication as publication
from src.shader_workspace.cbuffer_semantics import clean_fields, normalize_fields, select_physical_layouts


def field(name="baseColor", offsets=(0, 4, 8, 12), **extra):
    return {"name": name, "type": "float" + (str(len(offsets)) if len(offsets) > 1 else ""),
            "components": [{"offset": offset} for offset in offsets],
            "status": "inferred", "provenance": "shader", "evidence": "shader.hlsl:123 applies color",
            "description": "材质的基础颜色", **extra}


def identity():
    return {"blocks": [{"block": "Captured", "bind": 5, "space": 0, "byteSize": 32,
                        "members": [{"name": "unknown", "offset": 0}],
                        "leaves": [{"path": "unknown[0]", "offset": 0, "byteSize": 16,
                                    "baseType": "float", "componentByteSize": 4},
                                   {"path": "unknown[1]", "offset": 16, "byteSize": 16,
                                    "baseType": "float", "componentByteSize": 4}]}]}


def test_groups_scalar_lanes_and_splits_packed_vectors_without_mutating_reflection():
    original = identity()
    before = copy.deepcopy(original)
    result = normalize_fields(original, [field(bind=5), field("roughness", (16,), bind=5),
                                         field("uvScale", (28, 24), bind=5)])
    assert original == before
    assert result[(5, 0)][0]["type"] == "float4"
    assert result[(5, 0)][2]["components"] == [{"offset": 28}, {"offset": 24}]


@pytest.mark.parametrize("label", ["世界到视空间[0]·存储行", "底色 染色", "法线强度", "物体[12].变换[3]", "baseColor"])
def test_display_labels_are_not_shader_identifiers(label, tmp_path, monkeypatch):
    original = identity()
    before = copy.deepcopy(original)
    fields = normalize_fields(original, [field(label, bind=5)])[(5, 0)]
    assert fields[0]["name"] == label
    project, block, key, scope = setup_store(tmp_path, monkeypatch)
    aliases.publish_aliases(block["block"], block["members"], [],
                            shader_scope=scope, semantic_fields=fields)
    assert aliases.read_semantic_view(key, shader_scope=scope)["fields"][0]["name"] == label
    assert original == before


@pytest.mark.parametrize("label", ["", " 颜色", "颜色 ", "颜色\n值", "颜色\x00", "名" * 65])
def test_display_label_errors_identify_name_without_loosening_layout(label):
    with pytest.raises(ValueError, match="field 0 name"):
        clean_fields([field(label)])
    with pytest.raises(ValueError, match="field 0 .*type must"):
        clean_fields([field("底色", type="float4x4")])


def test_chinese_display_label_still_requires_the_exact_physical_type():
    with pytest.raises(ValueError, match="reinterpret"):
        normalize_fields(identity(), [field("底色染色", type="uint4", bind=5)])


def test_large_instance_array_names_resolve_each_current_physical_lane():
    count = 5400
    block = {"block": "Instances", "bind": 0, "space": 2, "byteSize": count * 4,
             "leaves": [{"offset": index * 4, "byteSize": 4, "baseType": "float"}
                        for index in range(count)]}
    fields = [field("instances[%d].weight" % index, (index * 4,), bind=0, space=2)
              for index in range(count)]
    result = normalize_fields({"blocks": [block]}, fields)[(0, 2)]
    assert len(result) == count
    assert result[-1]["components"] == [{"offset": (count - 1) * 4}]
    block["leaves"].append({"offset": 4, "byteSize": 12, "baseType": "float"})
    with pytest.raises(ValueError, match="unique"):
        normalize_fields({"blocks": [block]}, fields)


def test_matrix_rows_can_group_noncontiguous_lanes_but_never_padding():
    from src.shader_workspace.semantic_coverage import binding_coverage
    # A column-major float3x2, with 16-byte columns. The rows each span two
    # columns, while byte offsets 12 and 28 are padding rather than parameters.
    block = {"block": "Matrices", "bind": 0, "space": 0, "byteSize": 32,
             "leaves": [{"path": "basis", "offset": 0, "byteSize": 28,
                         "baseType": "float", "componentByteSize": 4,
                         "componentOffsets": [0, 16, 4, 20, 8, 24]}]}
    identity = {"blocks": [block]}
    rows = [field("basis.row%d" % row, (row * 4, 16 + row * 4), bind=0) for row in range(3)]
    assert normalize_fields(identity, rows)[(0, 0)] == clean_fields(rows)
    coverage = binding_coverage(identity, rows, [])
    assert coverage["complete"]
    assert coverage["bindings"][0]["requiredOffsets"] == [0, 4, 8, 16, 20, 24]
    for offset in (12, 28):
        with pytest.raises(ValueError, match="unique reflected"):
            normalize_fields(identity, [field(offsets=(offset,), bind=0)])


@pytest.mark.parametrize("relative", [[0, 0], [0, 32], [2], [], "0", [False]])
def test_invalid_matrix_component_offsets_cannot_be_named(relative):
    from src.shader_workspace.semantic_coverage import reflected_bindings
    block = identity()["blocks"][0]
    block["leaves"][0]["componentOffsets"] = relative
    with pytest.raises(ValueError, match="unique reflected"):
        normalize_fields({"blocks": [block]}, [field(offsets=(0,), bind=5)])
    binding = reflected_bindings({"blocks": [block]})[0]
    assert not binding["memberMappingComplete"]
    assert binding["memberMappingIssues"][0]["reason"] == "invalid-component-offsets"


@pytest.mark.parametrize("invalid", [
    field(offsets=(0, 4, 8, 32), bind=5), field(offsets=(2,), bind=5),
    field(offsets=(0, 0), bind=5), field(bind=True), field(type="double4", bind=5),
    field(type="uint4", bind=5), field(evidence="", bind=5), field(status="guessed", bind=5),
    field(components=[{"offset": 0}], bind=5), field(bind=7),
])
def test_rejects_unreadable_or_ambiguous_physical_mapping(invalid):
    with pytest.raises(ValueError):
        normalize_fields(identity(), [invalid])


def test_reflected_overlap_half_lane_and_cross_field_overlap_are_rejected():
    value = identity()
    value["blocks"][0]["leaves"].append({"offset": 4, "byteSize": 4})
    with pytest.raises(ValueError, match="unique"):
        normalize_fields(value, [field(bind=5)])
    value = identity()
    value["blocks"][0]["leaves"][0]["componentByteSize"] = 2
    with pytest.raises(ValueError, match="32-bit"):
        normalize_fields(value, [field(bind=5)])
    with pytest.raises(ValueError, match="overlap"):
        normalize_fields(identity(), [field(bind=5), field("other", (4,), bind=5)])


def setup_store(tmp_path, monkeypatch):
    project, user = tmp_path / "project", tmp_path / "user"
    project.mkdir()
    monkeypatch.setattr(aliases, "project_store_root", lambda: project)
    monkeypatch.setattr(aliases, "user_store_root", lambda: user)
    block = identity()["blocks"][0]
    key = aliases.layout_sha256(block["block"], block["members"])
    scope = {"shaderRawSha256": "a" * 64, "shaderResourceId": "77", "stage": "fragment",
             "bind": 5, "space": 0, "layoutSha256": key}
    return project, block, key, scope


def test_semantic_view_replaces_same_scope_preserves_user_alias_and_other_shader(tmp_path, monkeypatch):
    project, block, key, scope = setup_store(tmp_path, monkeypatch)
    def publish(fields, target=scope, entries=()):
        return aliases.publish_aliases(block["block"], block["members"], list(entries),
                                       shader_scope=target, semantic_fields=fields, semantic_source_sha256="c" * 64)
    publish([field()])
    other_scope = dict(scope, shaderRawSha256="b" * 64)
    publish([field("otherColor")], other_scope)
    publish([field("surfaceColor")], dict(scope, shaderResourceId="99"))
    aliases.publish_aliases(block["block"], block["members"],
                            [{"path": "unknown[1]", "offset": 16, "alias": "User setting"}], "user")
    stored = json.loads((project / (key + ".json")).read_text(encoding="utf-8"))
    assert len(stored["semanticViews"]) == 2
    assert stored["aliases"][0]["source"] == "user"
    assert aliases.read_semantic_view(key, shader_scope=scope)["fields"][0]["name"] == "surfaceColor"
    assert aliases.read_semantic_view(key, shader_scope=other_scope)["fields"][0]["name"] == "otherColor"
    assert aliases.read_semantic_view(key, shader_scope=dict(scope, shaderRawSha256="d" * 64)) is None
    assert aliases.read_semantic_view(key) is None


def test_user_semantic_view_wins_and_next_source_context_sees_persisted_fields(tmp_path, monkeypatch):
    _, block, key, scope = setup_store(tmp_path, monkeypatch)
    aliases.publish_aliases(block["block"], block["members"], [], "user",
                            shader_scope=scope, semantic_fields=[field("userColor")])
    result = aliases.publish_aliases(block["block"], block["members"], [],
                                     shader_scope=scope, semantic_fields=[field()])
    assert not result["applied"] and result["semanticFieldsKeptUser"] == 1
    source = "cbuffer Candidate:register(b5){float x:packoffset(c0.x);};"
    capture = {"rawSha256": "a" * 64, "stage": "fragment", "blocks": [block]}
    assert aliases.evidence_for_source(source, capture)["blocks"][0]["semanticView"]["fields"][0]["name"] == "userColor"


def test_atomic_publication_replays_semantic_receipt_without_mutation(tmp_path, monkeypatch):
    project, block, key, scope = setup_store(tmp_path, monkeypatch)
    batch = {"block": block["block"], "members": block["members"], "entries": [],
             "shaderScope": scope, "semanticFields": [field()], "semanticSourceSha256": "c" * 64}
    arguments = dict(workspace_root=tmp_path / "workspace", operation_id="semantic-test-001",
                     request_sha256="1" * 64, caller_request_sha256="2" * 64,
                     publications=[batch], build_result=lambda rows: {"published": rows})
    result = publication.publish_transaction(**arguments)
    snapshot = (project / (key + ".json")).read_bytes()
    replay = publication.publish_transaction(**arguments)
    assert result["published"][0]["semanticFieldsWritten"] == 1
    assert replay["published"] == result["published"]
    assert (project / (key + ".json")).read_bytes() == snapshot


def test_atomic_same_layout_original_and_compiled_views_accumulate(tmp_path, monkeypatch):
    project, block, key, scope = setup_store(tmp_path, monkeypatch)
    batches = [{"block": block["block"], "members": block["members"], "entries": [],
                "shaderScope": dict(scope, shaderRawSha256=raw * 64), "semanticFields": [field(name)]}
               for raw, name in (("a", "originalColor"), ("b", "compiledColor"))]
    publication.publish_transaction(
        workspace_root=tmp_path / "workspace", operation_id="same-layout-original-mirror",
        request_sha256="1" * 64, caller_request_sha256="2" * 64,
        publications=batches, build_result=lambda rows: {"published": rows})
    stored = json.loads((project / (key + ".json")).read_text(encoding="utf-8"))
    assert len(stored["semanticViews"]) == 2
    assert aliases.read_semantic_view(key, shader_scope=scope)["fields"][0]["name"] == "originalColor"
    assert aliases.read_semantic_view(key, shader_scope=dict(scope, shaderRawSha256="b" * 64))["fields"][0]["name"] == "compiledColor"


def test_stripped_unknown_storage_uses_verified_active_float_lanes_without_bitcast():
    value = identity()
    original = value["blocks"][0]
    active = copy.deepcopy(original)
    active.update(block="CaptureConstantsB5", members=[{"name": "cb5_c0_x", "offset": 0}],
                  memberMappingKind="annotated")
    original.update(members=[{"name": "unknown", "offset": 0}] * 13,
                    leaves=[{"path": "unknown[1]", "offset": 16, "byteSize": 16, "baseType": "uint"}],
                    memberMappingKind="placeholder")
    value.update(rawSha256="a" * 64, activeDisplayShaderRawSha256="b" * 64, activeDisplayBlocks=[active])
    before = copy.deepcopy(value)
    old, current, notices = select_physical_layouts(value, [field(bind=5), field("roughness", (16,), bind=5)])
    assert old == {}
    assert len(current[(5, 0)]) == 2
    assert notices[0]["basis"] == "verified-active-reflection"
    assert value == before
    with pytest.raises(ValueError, match="reinterpret"):
        select_physical_layouts(value, [field(bind=5, type="uint4")])
    original["memberMappingKind"] = "annotated"
    assert select_physical_layouts(value, [field(bind=5)])[1][(5, 0)][0]["name"] == "baseColor"


@pytest.mark.parametrize("mapping_kind", ["annotated", "placeholder"])
def test_complete_unknown_storage_array_uses_verified_active_types(mapping_kind):
    value = identity()
    original = value["blocks"][0]
    original["memberMappingKind"] = mapping_kind
    for leaf in original["leaves"]:
        leaf["baseType"] = "uint"
    active = copy.deepcopy(original)
    active.update(members=[{"name": "color", "offset": 0}], memberMappingKind="annotated")
    active["leaves"][0].update(path="color", baseType="float")
    active["leaves"][1].update(path="mode", baseType="int")
    value.update(rawSha256="a" * 64, activeDisplayShaderRawSha256="b" * 64, activeDisplayBlocks=[active])
    old, current, _ = select_physical_layouts(value, [field(bind=5)])
    assert not old and current[(5, 0)][0]["type"] == "float4"
    # Active signedness and the captured byte extent remain authoritative.
    with pytest.raises(ValueError, match="reinterpret reflected int as uint"):
        select_physical_layouts(value, [field(bind=5), field("mode", (16,), type="uint", bind=5)])
    original["byteSize"] = 8
    with pytest.raises(ValueError, match="captured physical CBuffer extent"):
        select_physical_layouts(value, [field(bind=5)])


@pytest.mark.parametrize("named_original", ["member", "leaf"])
def test_named_original_types_cannot_be_overridden_by_an_active_layout(named_original):
    value = identity()
    original = value["blocks"][0]
    original["memberMappingKind"] = "annotated"
    for leaf in original["leaves"]:
        leaf["baseType"] = "uint"
    active = copy.deepcopy(original)
    active["leaves"][0]["baseType"] = "float"
    if named_original == "member":
        original["members"][0]["name"] = "realIntegerParameters"
    else:
        original["leaves"][0]["path"] = "unknown[0].realIntegerParameters"
    value.update(rawSha256="a" * 64, activeDisplayShaderRawSha256="b" * 64, activeDisplayBlocks=[active])
    with pytest.raises(ValueError, match="reinterpret reflected uint as float"):
        select_physical_layouts(value, [field(bind=5)])


def test_active_view_readback_uses_compiled_layout_when_original_placeholder_has_no_view(tmp_path, monkeypatch):
    _, block, key, scope = setup_store(tmp_path, monkeypatch)
    active_block = dict(block, block="CaptureConstantsB5", members=[{"name": "cb5_c0_x", "offset": 0}])
    aliases.publish_aliases(active_block["block"], active_block["members"], [],
                            shader_scope=dict(scope, shaderRawSha256="b" * 64, layoutSha256=""),
                            semantic_fields=[field()], semantic_captured_shader_sha256="a" * 64)
    capture = {"rawSha256": "a" * 64, "stage": "fragment", "blocks": [block],
               "activeDisplayShaderRawSha256": "b" * 64, "activeDisplayBlocks": [active_block]}
    result = aliases.evidence_for_source("cbuffer Candidate:register(b5){float4 color;};", capture)
    record = result["blocks"][0]
    assert record["semanticViewBasis"] == "verified-active-reflection"
    assert record["semanticView"]["capturedShaderRawSha256"] == "a" * 64
    assert record["semanticView"]["shaderScope"]["shaderRawSha256"] == "b" * 64


def test_previous_candidate_names_are_review_leads_not_current_semantics(tmp_path, monkeypatch):
    _, block, _, scope = setup_store(tmp_path, monkeypatch)
    active_block = dict(block, block="CaptureConstantsB5", members=[{"name": "cb5_c0_x", "offset": 0}])
    for candidate, captured, name in (("b", "a", "previousColor"), ("d", "e", "otherCaptureColor")):
        aliases.publish_aliases(active_block["block"], active_block["members"], [],
                                shader_scope=dict(scope, shaderRawSha256=candidate * 64, layoutSha256=""),
                                semantic_fields=[field(name)], semantic_source_sha256="f" * 64,
                                semantic_captured_shader_sha256=captured * 64)
    capture = {"rawSha256": "a" * 64, "stage": "fragment", "blocks": [block],
               "activeDisplayShaderRawSha256": "c" * 64, "activeDisplayBlocks": [active_block]}
    record = aliases.evidence_for_source("cbuffer Candidate:register(b5){float4 color;};", capture)["blocks"][0]
    assert "semanticView" not in record
    history = record["semanticViewHistory"]
    assert len(history) == 1
    assert history[0]["fields"][0]["name"] == "previousColor"
    assert history[0]["reviewRequired"] is True
    assert history[0]["appliedToCurrentShader"] is False
    assert history[0]["sourceSha256"] == "f" * 64
    assert history[0]["shaderScope"]["shaderRawSha256"] == "b" * 64
    # A different captured Shader cannot inherit the hint, even with identical layout.
    capture["rawSha256"] = "9" * 64
    record = aliases.evidence_for_source("cbuffer Candidate:register(b5){float4 color;};", capture)["blocks"][0]
    assert "semanticViewHistory" not in record


def test_previous_semantic_review_hints_are_bounded_and_binding_scoped(tmp_path, monkeypatch):
    _, block, key, scope = setup_store(tmp_path, monkeypatch)
    fields = [field("control%d" % i, (i * 4,)) for i in range(129)]
    for candidate in ("b", "c", "d"):
        aliases.publish_aliases(block["block"], block["members"], [],
                                shader_scope=dict(scope, shaderRawSha256=candidate * 64),
                                semantic_fields=fields, semantic_captured_shader_sha256="a" * 64)
    arguments = dict(captured_sha256="a" * 64, stage="fragment", bind=5, space=0,
                     active_sha256="e" * 64)
    history = aliases._previous_semantic_views(key, **arguments)
    assert len(history) == 2
    assert all(len(view["fields"]) == 128 and view["totalFields"] == 129
               and view["fieldsTruncated"] for view in history)
    assert aliases._previous_semantic_views(key, **dict(arguments, bind=6)) == []
    assert aliases._previous_semantic_views(key, **dict(arguments, space=1)) == []
    assert aliases._previous_semantic_views(key, **dict(arguments, stage="vertex")) == []
