import copy
import math
import pytest
from src.material_inspector import build_material_inspector


def present(annotation, prop):
    spec = {"constantBuffers": [{"target": prop["name"], "classification": "material", "inspector": annotation}]}
    before = copy.deepcopy((spec, prop))
    result = build_material_inspector(spec, [prop], [])
    assert (spec, prop) == before
    return result


def test_presentation_preserves_values_and_requires_explicit_semantics():
    prop = {"name": "_NormalColorPacked", "type": "vector", "value": [4, 3, 2, -8]}
    assert "control" not in present({}, prop)["properties"][0]
    row = present({"control": "color", "hdr": True, "alpha": False, "label": "主色"}, prop)["properties"][0]
    assert row["name"] == prop["name"] and row["label"] == "主色" and row["hdr"] is True
    assert row["alpha"] is False


@pytest.mark.parametrize("limits", [(0, 1), (-5, 20)])
def test_outside_range_value_is_not_clamped(limits):
    value = present({"control": "slider", "min": limits[0], "max": limits[1]},
                    {"name": "_Amount", "type": "float", "value": 200})
    assert value["properties"][0]["control"] == "slider"


@pytest.mark.parametrize("annotation", [None, {"control": "slider", "min": 1, "max": 0},
    {"control": "slider", "min": 0, "max": math.inf}, {"control": "color"}])
def test_invalid_optional_ui_falls_back(annotation):
    result = present(annotation, {"name": "_Scalar", "type": "float", "value": 4})
    assert result["diagnostics"]
    assert all("control" not in row for row in result["properties"])


def test_texture_labels_are_available_without_changing_binding():
    row = {"property": "_BaseMap", "inspector": {"label": "底色贴图", "tooltip": "EID1267 BaseColor"}}
    result = build_material_inspector({}, [], [row])
    assert result["properties"][0]["name"] == "_BaseMap"
    assert result["properties"][0]["label"] == "底色贴图"
