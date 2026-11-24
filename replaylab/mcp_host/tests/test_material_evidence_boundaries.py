"""Actual material fields retain identity without inventing GPU layout."""
import pytest

from src import material_naming as naming


def unity_material(texture, *, source="bundles/level0.assets"):
    return {"m_Name": "M_Rock", "sourceFile": source, "m_SavedProperties": {
        "m_Floats": [["_Glossiness", 0.4417283]],
        "m_TexEnvs": [{"Key": "_MainTex", "Value": {
            "m_Texture": texture, "m_Scale": {"x": 2.0, "y": 3.0},
            "m_Offset": {"x": 0.25, "y": 0.5}}}]}}


def test_unity_actual_pptr_and_texenv_fields_are_preserved_without_fabricating_a_path():
    material = naming.parse_material_parameters(unity_material({"m_FileID": 0, "m_PathID": 42}))
    assert material["scalars"] == [{"name": "_Glossiness", "value": 0.4417283}]
    texture = material["textures"][0]
    assert texture["objectPath"] == ""
    assert texture["objectReference"] == {
        "fileId": 0, "pathId": 42, "sourceFile": "bundles/level0.assets", "isNull": False}
    assert texture["scale"] == [2.0, 3.0]
    assert texture["offset"] == [0.25, 0.5]
    assert material["vectors"] == []  # TexEnv does not prove a physical _MainTex_ST member.
    result = naming.propose_texture_names(material, [{"resourceId": 7}], [
        {"slot": 0, "objectReference": texture["objectReference"]}])
    assert result["matchedCount"] == 1


@pytest.mark.parametrize("change", [
    {"fileId": 1}, {"sourceFile": "bundles/level1.assets"}, {"sourceFile": ""},
    {"pathId": 0}, {"pathId": True}, {"fileId": -1},
])
def test_unity_pptr_identity_requires_same_source_and_both_ids(change):
    material = naming.parse_material_parameters(unity_material({"m_FileID": 0, "m_PathID": 42}))
    reference = {**material["textures"][0]["objectReference"], **change}
    result = naming.propose_texture_names(material, [{"resourceId": 7}], [
        {"slot": 0, "objectReference": reference}])
    assert result["proposals"] == []


def test_unity_null_reference_stays_null_even_with_a_stale_display_name():
    material = naming.parse_material_parameters(unity_material({
        "m_FileID": 0, "m_PathID": 0, "m_Name": "old_texture"}))
    assert material["textures"][0]["objectReference"]["isNull"] is True
    assert material["textures"][0]["objectPath"] == ""


def test_different_directories_with_the_same_leaf_do_not_match():
    material = naming.parse_material_parameters({"textures": {"Base": "/Game/A/T_Rock.T_Rock"}})
    assert naming.propose_texture_names(material, [{"resourceId": 7}], [
        {"slot": 0, "asset": "/Game/B/T_Rock.uasset"}])["proposals"] == []
    assert naming.propose_texture_names(material, [{"resourceId": 7}], [
        {"slot": 0, "asset": "/Game/A/T_Rock.uasset"}])["matchedCount"] == 1


def test_reused_texture_cannot_choose_between_two_material_parameter_names():
    material = naming.parse_material_parameters({"textures": {
        "Base": "/Game/A/T", "Mask": "/Game/A/T"}})
    result = naming.propose_texture_names(material, [{"resourceId": 7}], [
        {"slot": 0, "asset": "/Game/A/T"}])
    assert result["proposals"] == []


def test_conflicting_links_do_not_emit_two_aliases_for_one_slot():
    material = naming.parse_material_parameters({"textures": {"A": "/Game/A", "B": "/Game/B"}})
    result = naming.propose_texture_names(material, [{"resourceId": 7}], [
        {"slot": 0, "asset": "/Game/A"}, {"slot": 0, "asset": "/Game/B"}])
    assert result["proposals"] == []
    assert result["ambiguous"]


def test_material_array_offsets_cannot_override_conflicting_captured_values():
    material = naming.parse_material_parameters({"scalars": {
        "AnchorA": 0.8231471, "Roughness": 0.4417283,
        "AnchorB": 0.6612934, "Anisotropy": 0.2938471}})
    result = naming.propose_member_names(material, [{"bind": 2, "space": 3,
        "floats": [0.8231471, 1.0, 0.6612934, 0.0]}])
    assert {(p["parameter"], p["byteOffset"]) for p in result["proposals"]} == {
        ("AnchorA", 0), ("AnchorB", 8)}
    assert {p["parameter"] for p in result["valueConflicts"]} >= {"Roughness", "Anisotropy"}


def test_reordered_material_matches_actual_offsets_independently_of_array_order():
    material = naming.parse_material_parameters({"scalars": {
        "A": 0.8231471, "B": 0.4417283, "C": 0.6612934}})
    result = naming.propose_member_names(material, [{"bind": 2, "space": 1,
        "floats": [0.6612934, 0.0, 0.8231471, 0.4417283]}])
    assert {p["parameter"]: p["byteOffset"] for p in result["proposals"]} == {"A": 8, "B": 12, "C": 0}


def test_two_identical_material_scalars_cannot_both_claim_one_captured_lane():
    material = naming.parse_material_parameters({"scalars": {"A": 0.8231471, "B": 0.8231471}})
    result = naming.propose_member_names(material, [{"bind": 0, "space": 0, "floats": [0.8231471]}])
    assert result["proposals"] == []
    assert len(result["ambiguous"]) == 2


def test_repeated_vector_does_not_silently_select_first_occurrence():
    values = [0.1273844, 0.9182736, 0.3746152, 1.0]
    material = naming.parse_material_parameters({"vectors": {"Tint": values}})
    result = naming.propose_member_names(material, [{"bind": 0, "space": 0, "floats": values * 2}],
                                         fill_gaps=False)
    assert result["proposals"] == []
    assert result["ambiguous"][0]["lanes"] == [0, 4]


@pytest.mark.parametrize("vector", [[0.25, None, 0.5, 1.0], [0.25, False, 0.5, 1.0],
                                    [0.25, float("nan"), 0.5, 1.0]])
def test_invalid_vector_components_are_not_compacted_into_new_lanes(vector):
    material = naming.parse_material_parameters({"vectors": {"Tint": vector}, "scalars": {"Keep": 0.25}})
    assert material["vectors"] == []


def test_unique_round_uv_tuples_are_candidates_without_compiled_width_evidence():
    material = naming.parse_material_parameters({"vectors": {
        "Drop Tilling&Offset": [300, 220, 0, 0],
        "Trail Size": [80, 60, 0, 0],
        "Drips Tilling&Offset": [15, 10, 0, 0]}})
    floats = [1, 0.5, 0, 1, 80, 60, 0, 0] + [1.0] * 8 + [300, 220, 0, 0] + [1.0] * 12 + [15, 10, 0, 0]
    result = naming.propose_member_names(material, [{"bind": 5, "space": 0, "floats": floats}], fill_gaps=False)
    assert result["matchedCount"] == 12
    assert {(p["parameter"], p["byteOffset"]) for p in result["proposals"]} == {
        (name, offset + component * 4)
        for name, offset in (("Trail Size", 16), ("Drop Tilling&Offset", 64), ("Drips Tilling&Offset", 128))
        for component in range(4)}
    assert all(p["relations"] == ["exact"] and p["provisional"] for p in result["proposals"])
    assert result["autoApplyCount"] == 0 and result["withheldCount"] == 12
    assert all(not p["semanticWidthVerified"] and not p["autoApplyEligible"] for p in result["proposals"])
    # Actual UE preshader counterexample: Trail Size is Float2 at bytes 16..23;
    # trailing equal zeros can be Trail Seed and Trail RandomMask, not B and A.
    assert naming.alias_rows(result["proposals"]) == []


@pytest.mark.parametrize("values", [[0, 0, 0, 0], [1, 1, 1, 1], [1, 0, 0, 1], [0.5, 1, 0, 0], [2, 2, 2, 1]])
def test_common_round_vector_fills_do_not_become_automatic_names(values):
    material = naming.parse_material_parameters({"vectors": {"Unknown": values}})
    result = naming.propose_member_names(material, [{"bind": 5, "space": 0, "floats": values}], fill_gaps=False)
    assert not result["proposals"]


def test_round_vector_requires_all_components_and_cannot_choose_repeated_or_competing_tuple():
    values = [300, 220, 0, 0]
    material = naming.parse_material_parameters({"vectors": {"Drop": values}})
    mismatch = naming.propose_member_names(material, [{"bind": 5, "space": 0, "floats": [300, 220, 1, 0]}], fill_gaps=False)
    assert not mismatch["proposals"]
    repeated = naming.propose_member_names(material, [{"bind": 5, "space": 0, "floats": values * 2}], fill_gaps=False)
    assert not repeated["proposals"]
    assert repeated["ambiguous"][0]["lanes"] == [0, 4]
    competing = naming.parse_material_parameters({"vectors": {"Drop": values, "Trail": values}})
    conflict = naming.propose_member_names(competing, [{"bind": 5, "space": 0, "floats": values}], fill_gaps=False)
    assert not conflict["proposals"]
    assert {a["parameter"] for a in conflict["ambiguous"] if "parameter" in a} == {"Drop", "Trail"}


def test_round_scalars_still_require_evidence_beyond_an_isolated_equal_value():
    material = naming.parse_material_parameters({"scalars": {"Drips Speed": -0.3, "Shadow Offset": 1.6}})
    result = naming.propose_member_names(material, [{"bind": 5, "space": 0, "floats": [-0.3, 1.6]}], fill_gaps=False)
    assert not result["proposals"]
