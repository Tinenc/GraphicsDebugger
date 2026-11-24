"""Real CUE4Parse UMaterial cache shape preserves names/defaults, not GPU offsets."""
from copy import deepcopy

import pytest

from src import material_naming as naming


def info(name, association="GlobalParameter", index=-1):
    return {"Name": name, "Association": association, "Index": index}


def actual_cache_shape():
    # Minimized from a real M_CharacterPBR UEX export. The DoubleVector slot is
    # empty; textures are explicitly RuntimeEntries[3], not the third group.
    return [{"Type": "Material", "Name": "M_CharacterPBR", "Properties": {"BlendMode": "BLEND_Opaque"},
             "CachedExpressionData": {
                 "RuntimeEntries": {"ParameterInfoSet": [info("Normal Intensity"), info("Drips Speed")]},
                 "RuntimeEntries[1]": {"ParameterInfoSet": [info("DirtDissolveEdgeColor")]},
                 "RuntimeEntries[2]": {"ParameterInfoSet": []},
                 "RuntimeEntries[3]": {"ParameterInfoSet": [info("GratiaCloak AniateMask")]},
                 "ScalarValues": [1.0, -0.5],
                 "VectorValues": [{"R": 100.0, "G": 10.892993, "B": 0.0, "A": 1.0, "Hex": "FFFF00"}],
                 "DoubleVectorValues": [],
                 "TextureValues": [{"AssetPathName": "/Game/MasterMaterial/Temp_Resource/Texture/BaseMap/T_BaseMap_White_Linear.T_BaseMap_White_Linear", "SubPathString": ""}],
                 "ScalarPrimitiveDataIndexValues": [255, 255],
                 "FunctionInfos": [{"StateId": "unused"}],
             }}]


def test_current_cached_defaults_read_real_indexed_properties_without_layout_claim():
    result = naming.parse_material_parameters(actual_cache_shape())
    assert result["parameterSource"] == "unreal"
    assert [(p["name"], p["value"]) for p in result["scalars"]] == [("Normal Intensity", 1.0), ("Drips Speed", -0.5)]
    assert result["vectors"][0]["value"] == [100.0, 10.892993, 0.0, 1.0]
    assert result["textures"][0]["name"] == "GratiaCloak AniateMask"
    assert result["textures"][0]["objectPath"].endswith("T_BaseMap_White_Linear.T_BaseMap_White_Linear")
    assert result["defaultValuesOnly"] is True and result["gpuLayoutKnown"] is False
    assert result["scalars"][0]["parameterInfo"] == info("Normal Intensity")
    assert all("byteOffset" not in p and "lane" not in p for k in ("scalars", "vectors", "textures") for p in result[k])
    assert not result["parameterReadWarnings"]


def test_explicit_legacy_parameters_wrapper_uses_parameter_infos_and_texture_entry_two():
    document = {"Type": "Material", "Name": "M_Legacy", "CachedExpressionData": {"Parameters": {
        "RuntimeEntries": {"ParameterInfos": [info("Roughness")]},
        "RuntimeEntries[1]": {"ParameterInfos": []},
        "RuntimeEntries[2]": {"ParameterInfos": [info("BaseMap")]},
        "ScalarValues": [0.25], "VectorValues": [],
        "TextureValues": [{"ObjectPath": "Example/Content/Textures/T_Base.0"}]}}}
    result = naming.parse_material_parameters(document)
    assert result["scalars"][0]["value"] == 0.25
    assert result["textures"][0]["objectPath"] == "Example/Content/Textures/T_Base.0"


def test_struct_array_export_spelling_preserves_declared_slot_numbers():
    document = actual_cache_shape()
    cache = document[0]["CachedExpressionData"]
    cache["RuntimeEntries"] = [cache["RuntimeEntries"], cache.pop("RuntimeEntries[1]"),
                               cache.pop("RuntimeEntries[2]"), cache.pop("RuntimeEntries[3]")]
    result = naming.parse_material_parameters(document)
    assert len(result["scalars"]) == 2 and len(result["vectors"]) == len(result["textures"]) == 1


def test_mismatched_name_and_value_lengths_never_zip_or_guess_a_pair():
    document = actual_cache_shape()
    document[0]["CachedExpressionData"]["ScalarValues"] = [1.0]
    result = naming.parse_material_parameters(document)
    assert result["scalars"] == []
    assert result["vectors"]
    assert result["parameterReadWarnings"][0]["field"] == "ScalarValues"


@pytest.mark.parametrize("bad", [False, None, float("nan"), float("inf")])
def test_invalid_scalar_keeps_following_name_value_pair_at_its_original_index(bad):
    document = actual_cache_shape()
    document[0]["CachedExpressionData"]["ScalarValues"][0] = bad
    result = naming.parse_material_parameters(document)
    assert [(p["name"], p["value"]) for p in result["scalars"]] == [("Drips Speed", -0.5)]
    assert result["parameterReadWarnings"][0]["parameter"] == "Normal Intensity"


def test_layer_scope_is_retained_without_flattening_into_global_parameter_names():
    document = actual_cache_shape()
    document[0]["CachedExpressionData"]["RuntimeEntries"]["ParameterInfoSet"][1] = info("Normal Intensity", "LayerParameter", 0)
    result = naming.parse_material_parameters(document)
    assert len(result["scalars"]) == 1
    assert result["scopedParameters"] == [{"kind": "scalars", "name": "Normal Intensity", "value": -0.5,
        "parameterInfo": info("Normal Intensity", "LayerParameter", 0)}]


def test_instance_overrides_keep_priority_over_its_cached_defaults():
    document = actual_cache_shape()
    document[0]["Type"] = "MaterialInstanceConstant"
    document[0]["Properties"]["ScalarParameterValues"] = [{"ParameterInfo": info("Drips Speed"), "ParameterValue": -0.3}]
    result = naming.parse_material_parameters(document)
    assert result["scalars"] == [{"name": "Drips Speed", "value": -0.3}]
    assert not result.get("defaultValuesOnly")


def test_two_material_exports_require_selection_including_a_cached_base_material():
    document = actual_cache_shape()
    second = deepcopy(document[0])
    second["Name"] = "OtherMaterial"
    with pytest.raises(naming.MaterialNamingError, match="multiple material"):
        naming.parse_material_parameters(document + [second])
