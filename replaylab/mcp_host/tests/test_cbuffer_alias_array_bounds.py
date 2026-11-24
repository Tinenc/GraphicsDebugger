"""Reflected struct arrays must never alias a neighboring element's storage."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


def module():
    path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "cbuffer_alias_identity.py"
    spec = importlib.util.spec_from_file_location("cbuffer_array_bounds", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def array(children, *, stride=16, elements=2):
    return {"path": "items", "absoluteByteOffset": 0, "elements": elements,
            "arrayByteStride": stride, "byteSize": stride * elements, "members": children}


def leaf(path="items.color", offset=0, size=12, columns=3):
    return {"path": path, "absoluteByteOffset": offset, "byteSize": size,
            "rows": 1, "columns": columns, "baseType": "float"}


def test_final_struct_does_not_require_unused_trailing_stride_padding():
    diagnostics = {}
    values = module()._leaves_for([array([leaf()])], 28, diagnostics)
    assert [(item["path"], item["offset"], item["byteSize"]) for item in values] == [
        ("items[0].color", 0, 12), ("items[1].color", 16, 12)]
    assert diagnostics["issueCount"] == 0


@pytest.mark.parametrize("invalid", [leaf(offset=16), leaf(offset=-4), leaf(size=20, columns=4)])
def test_child_cannot_map_outside_its_own_array_element(invalid):
    diagnostics = {}
    assert module()._leaves_for([array([invalid])], 128, diagnostics) == []
    assert diagnostics["issueCount"] > 0


def test_outer_stride_cannot_make_missing_inner_array_offset_valid():
    inner = {"path": "items.weights", "absoluteByteOffset": -1, "elements": 2,
             "arrayByteStride": 4, "members": [leaf("items.weights.value", 0, 4, 1)]}
    diagnostics = {}
    assert module()._leaves_for([array([inner])], 64, diagnostics) == []
    assert {issue["reason"] for issue in diagnostics["issues"]} == {"missing-array-layout"}


def test_nested_array_cannot_spill_into_next_struct_element():
    inner = {"path": "items.weights", "absoluteByteOffset": 8, "elements": 3,
             "arrayByteStride": 4, "members": [leaf("items.weights.value", 8, 4, 1)]}
    diagnostics = {}
    assert module()._leaves_for([array([inner])], 64, diagnostics) == []
    assert {issue["reason"] for issue in diagnostics["issues"]} == {"array-outside-parent"}


def test_missing_scalar_array_stride_is_not_guessed_from_natural_width():
    value = leaf("weights", 0, 32, 1)
    value["elements"] = 8
    diagnostics = {}
    assert module()._leaves_for([value], 32, diagnostics) == []
    assert diagnostics["issues"][0]["reason"] == "missing-array-layout"


def test_small_stride_cannot_silently_truncate_vector_type():
    value = leaf("vectors", 0, 16, 4)
    value.update(elements=2, arrayByteStride=8)
    diagnostics = {}
    assert module()._leaves_for([value], 32, diagnostics) == []
    assert diagnostics["issues"][0]["reason"] == "array-stride-smaller-than-element"


def test_partial_overlaps_are_omitted_but_unrelated_rows_survive():
    diagnostics = {}
    values = module()._leaves_for([leaf("a", 0, 16, 4), leaf("b", 12, 8, 2),
                                  leaf("c", 20, 4, 1)], 24, diagnostics)
    assert [(item["path"], item["offset"]) for item in values] == [("c", 20)]
    assert diagnostics["issueCount"] == 2
    assert {issue["reason"] for issue in diagnostics["issues"]} == {"overlapping-field"}


def test_global_expansion_bound_limits_multiple_large_arrays(monkeypatch):
    target = module()
    monkeypatch.setattr(target, "MAX_ALIAS_LEAVES", 3)
    value = leaf("weights", 0, 64, 1)
    value.update(elements=16, arrayByteStride=4)
    diagnostics = {}
    values = target._leaves_for([value], 64, diagnostics)
    assert len(values) == 3
    assert diagnostics["issues"] == [{"path": "weights", "reason": "mapping-expansion-limit"}]


def test_visit_bound_stops_invalid_nested_members_without_collecting_leaves(monkeypatch):
    target = module()
    monkeypatch.setattr(target, "MAX_ALIAS_FIELD_VISITS", 10)
    value = array([leaf(offset=16)] * 40)
    diagnostics = {}
    assert target._leaves_for([value], 64, diagnostics) == []
    assert diagnostics["issues"][-1]["reason"] == "mapping-expansion-limit"
    assert diagnostics["issueCount"] <= 10


def test_child_paths_must_follow_the_original_reflected_parent():
    diagnostics = {}
    assert module()._leaves_for([array([leaf("other.color")])], 32, diagnostics) == []
    assert {issue["reason"] for issue in diagnostics["issues"]} == {"field-path-outside-parent"}


def test_partial_mapping_exposes_omissions_in_identity_receipt():
    block = SimpleNamespace(name="Instances", fixedBindNumber=0, fixedBindSetOrSpace=2,
                            byteSize=32, variables=[SimpleNamespace(name="items", byteOffset=0)])
    values = [array([leaf(), leaf("items.invalid", 16, 4, 1)])]
    result = module().reflection_alias_blocks(SimpleNamespace(constantBlocks=[block]),
                                             lambda _: values, lambda *_: False)[0]
    assert len(result["leaves"]) == 2
    assert result["memberMappingAvailable"] is True
    assert result["memberMappingComplete"] is False
    assert result["memberMappingIssueCount"] == 2
    assert [item["path"] for item in result["memberMappingIssues"]] == [
        "items[0].invalid", "items[1].invalid"]
