"""Naming a draw's constants from the material that produced them.

Material arrays are candidate correspondence, not physical GPU layouts. Values
and individual binding evidence must support every proposed name.
"""

from __future__ import annotations

import unittest

from src import material_naming as naming


MATERIAL = {
    "Name": "MI_Vulture01_Body_A01",
    "Type": "MaterialInstanceConstant",
    "Properties": {
        "ScalarParameterValues": [
            {"ParameterInfo": {"Name": "NormalIntensity"}, "ParameterValue": 0.8231471},
            {"ParameterInfo": {"Name": "Roughness"}, "ParameterValue": 0.4417283},
            {"ParameterInfo": {"Name": "WetnessIntensity"}, "ParameterValue": 0.6612934},
            {"ParameterInfo": {"Name": "AnisotropyStrength"}, "ParameterValue": 0.2938471},
        ],
        "VectorParameterValues": [
            {"ParameterInfo": {"Name": "EmissiveTint"},
             "ParameterValue": {"R": 0.1273844, "G": 0.9182736, "B": 0.3746152, "A": 1.0}},
        ],
        "TextureParameterValues": [
            {"ParameterInfo": {"Name": "BaseMap"},
             "ParameterValue": {"ObjectPath": "/Game/.../T_Vulture01_Body_A01_D.T_Vulture01_Body_A01_D"}},
            {"ParameterInfo": {"Name": "NormalMap"},
             "ParameterValue": {"ObjectPath": "/Game/.../T_Vulture01_Body_A01_N.T_Vulture01_Body_A01_N"}},
            {"ParameterInfo": {"Name": "MRAMap"},
             "ParameterValue": {"ObjectPath": "/Game/.../T_Vulture01_Body_A01_MRA.T_Vulture01_Body_A01_MRA"}},
        ],
    },
}


class ParseTests(unittest.TestCase):
    def test_the_parameter_list_is_read_from_any_of_the_usual_spellings(self):
        for document in (MATERIAL, [MATERIAL], {"exports": [MATERIAL]}):
            parsed = naming.parse_material_parameters(document)
            self.assertEqual(parsed["objectName"], "MI_Vulture01_Body_A01")
            self.assertEqual([item["name"] for item in parsed["scalars"]],
                             ["NormalIntensity", "Roughness", "WetnessIntensity", "AnisotropyStrength"])
            self.assertEqual([item["name"] for item in parsed["vectors"]], ["EmissiveTint"])
            self.assertEqual([item["name"] for item in parsed["textures"]],
                             ["BaseMap", "NormalMap", "MRAMap"])

    def test_a_document_without_parameters_says_so(self):
        with self.assertRaises(naming.MaterialNamingError):
            naming.parse_material_parameters({"Name": "SK_Vulture", "Type": "SkeletalMesh"})


class MemberNameTests(unittest.TestCase):
    def _material(self):
        return naming.parse_material_parameters(MATERIAL)

    def test_a_matched_value_names_its_lane(self):
        blocks = [{"bind": 2, "space": 0,
                   "floats": [0.0, 1.0, 0.8231471, 0.0, 0.4417283, 0.0]}]
        result = naming.propose_member_names(self._material(), blocks)
        by_lane = {item["lane"]: item for item in result["proposals"]}
        self.assertEqual(by_lane[2]["parameter"], "NormalIntensity")
        self.assertEqual(by_lane[2]["basis"], "matched")
        self.assertEqual(by_lane[2]["byteOffset"], 8)
        self.assertEqual(by_lane[4]["parameter"], "Roughness")
        self.assertEqual(result["matchedCount"], 2)

    def test_two_anchors_name_the_whole_run_including_the_round_numbers(self):
        # The four scalars laid out in order. Two of them hold values every
        # material holds (1.0, 0.0), so they can never match by value -- and they
        # are exactly the ones that need naming from the run.
        blocks = [{"bind": 2, "space": 0,
                   "floats": [0.8231471, 1.0, 0.6612934, 0.0]}]
        material = self._material()
        material["scalars"][1]["value"] = 1.0
        material["scalars"][3]["value"] = 0.0
        result = naming.propose_member_names(material, blocks)
        by_lane = {item["lane"]: item for item in result["proposals"]}
        self.assertEqual([by_lane[lane]["parameter"] for lane in range(4)],
                         ["NormalIntensity", "Roughness", "WetnessIntensity", "AnisotropyStrength"])
        self.assertEqual(by_lane[0]["basis"], "matched")
        self.assertFalse(by_lane[0]["provisional"])
        self.assertEqual(by_lane[0]["markedName"], "NormalIntensity")
        # The ones read from the run carry the uncertainty in the name itself.
        self.assertEqual(by_lane[1]["basis"], "aligned")
        self.assertTrue(by_lane[1]["provisional"])
        self.assertEqual(by_lane[1]["markedName"], "Roughness_u")
        self.assertEqual(by_lane[3]["markedName"], "AnisotropyStrength_u")
        self.assertEqual(result["matchedCount"], 2)
        self.assertEqual(result["alignedCount"], 2)

    def test_one_distinctive_value_and_matching_window_support_provisional_names(self):
        blocks = [{"bind": 2, "space": 0, "floats": [0.8231471, 1.0, 0.0, 0.0]}]
        material = self._material()
        for scalar, value in zip(material["scalars"][1:], [1.0, 0.0, 0.0]):
            scalar["value"] = value
        result = naming.propose_member_names(material, blocks)
        bases = {item["lane"]: item["basis"] for item in result["proposals"]}
        self.assertEqual(bases[0], "matched")
        self.assertEqual(bases[1], "aligned")
        self.assertEqual(result["alignedCount"], 3)
        self.assertEqual(result["orderedCount"], 0)

    def test_anchors_that_contradict_each_other_name_nothing_from_order(self):
        # NormalIntensity at lane 0 (offset 0) and WetnessIntensity at lane 9
        # (offset 7): the parameters are not laid out end to end here.
        blocks = [{"bind": 2, "space": 0,
                   "floats": [0.8231471, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.6612934]}]
        result = naming.propose_member_names(self._material(), blocks)
        self.assertEqual(result["matchedCount"], 2)
        self.assertEqual(result["alignedCount"] + result["orderedCount"], 0)
        self.assertTrue(any("different parameter offsets" in item.get("reason", "")
                            for item in result["ambiguous"]))

    def test_values_only_mode_proposes_nothing_it_cannot_prove(self):
        blocks = [{"bind": 2, "space": 0,
                   "floats": [0.8231471, 1.0, 0.6612934, 0.0]}]
        result = naming.propose_member_names(self._material(), blocks, fill_gaps=False)
        self.assertEqual(result["alignedCount"] + result["orderedCount"], 0)
        self.assertEqual(result["matchedCount"], 2)

    def test_a_vector_needs_all_four_components(self):
        good = [{"bind": 0, "space": 0,
                 "floats": [0.0, 0.1273844, 0.9182736, 0.3746152, 1.0]}]
        result = naming.propose_member_names(self._material(), good, fill_gaps=False)
        components = [item for item in result["proposals"] if item.get("kind") == "vector"]
        self.assertEqual([item["component"] for item in components], list("RGBA"))
        self.assertEqual({item["parameter"] for item in components}, {"EmissiveTint"})

        partial = [{"bind": 0, "space": 0,
                    "floats": [0.1273844, 0.9182736, 0.0, 0.0]}]
        result = naming.propose_member_names(self._material(), partial, fill_gaps=False)
        self.assertFalse([item for item in result["proposals"] if item.get("kind") == "vector"])

    def test_a_value_at_several_lanes_is_ambiguous_rather_than_assigned(self):
        blocks = [{"bind": 1, "space": 0,
                   "floats": [0.4417283, 0.0, 0.4417283]}]
        result = naming.propose_member_names(self._material(), blocks)
        self.assertFalse([item for item in result["proposals"] if item["parameter"] == "Roughness"])
        self.assertTrue(any(item["parameter"] == "Roughness" for item in result["ambiguous"]))

    def test_round_numbers_are_never_evidence(self):
        # Nothing anchors this block, so nothing is named: a run has to be placed
        # by at least one value before its order means anything.
        material = naming.parse_material_parameters({
            "Name": "MI_Plain", "Type": "MaterialInstanceConstant",
            "Properties": {"ScalarParameterValues": [
                {"ParameterInfo": {"Name": "Opacity"}, "ParameterValue": 1.0}]},
        })
        blocks = [{"bind": 0, "space": 0, "floats": [1.0, 1.0, 1.0]}]
        result = naming.propose_member_names(material, blocks)
        self.assertEqual(result["proposals"], [])

    def test_the_marker_is_a_marker_not_a_convention(self):
        blocks = [{"bind": 2, "space": 0, "floats": [0.8231471, 1.0, 0.6612934, 0.0]}]
        material = self._material()
        material["scalars"][1]["value"] = 1.0
        material["scalars"][3]["value"] = 0.0
        result = naming.propose_member_names(material, blocks, provisional_suffix="?guess")
        provisional = [item for item in result["proposals"] if item["provisional"]]
        self.assertTrue(provisional)
        self.assertTrue(all(item["markedName"].endswith("?guess") for item in provisional))
        self.assertEqual(result["provisionalSuffix"], "?guess")


class TextureNameTests(unittest.TestCase):
    def test_a_recorded_link_names_only_its_own_slot(self):
        material = naming.parse_material_parameters(MATERIAL)
        bound = [{"resourceId": 1}, {"resourceId": 2}, {"resourceId": 3}]
        links = [{"slot": 1, "asset": "/Game/.../T_Vulture01_Body_A01_N.uasset"}]
        result = naming.propose_texture_names(material, bound, links)
        by_slot = {item["slot"]: item for item in result["proposals"]}
        self.assertEqual(by_slot[1]["parameter"], "NormalMap")
        self.assertEqual(by_slot[1]["basis"], "matched")
        self.assertEqual(by_slot[1]["markedName"], "NormalMap")
        self.assertFalse(by_slot[1]["autoApplyEligible"])
        self.assertEqual(result["withheldCount"], 1)
        self.assertEqual(set(by_slot), {1})
        self.assertEqual(result["unnamedSlots"], [0, 2])

    def test_an_exact_stored_content_link_is_not_agent_semantic_confirmation(self):
        material = naming.parse_material_parameters(MATERIAL)
        bound = [{"resourceId": 7, "bind": 3, "space": 2}]
        result = naming.propose_texture_names(material, bound, [{
            "slot": 0,
            "asset": "/Game/.../T_Vulture01_Body_A01_D.uasset",
            "resourceId": 7,
            "bindPoint": 3,
            "space": 2,
            "hostVerified": True,
            "hostEvidenceKind": "pixel-same-image",
        }])
        (proposal,) = result["proposals"]
        self.assertEqual(proposal["parameter"], "BaseMap")
        self.assertFalse(proposal["contentMatchEligible"])
        self.assertFalse(proposal["autoApplyEligible"])
        self.assertEqual(result["autoApplyCount"], 0)

    def test_a_link_that_contradicts_the_order_names_only_itself(self):
        # The only texture link says slot 0 is the material's third parameter, so
        # the bindings are not that array end to end: nothing is read from order.
        material = naming.parse_material_parameters(MATERIAL)
        bound = [{"resourceId": 1}, {"resourceId": 2}, {"resourceId": 3}]
        links = [{"slot": 0, "asset": "/Game/.../T_Vulture01_Body_A01_MRA.uasset"},
                 {"slot": 1, "asset": "/Game/.../T_Vulture01_Body_A01_N.uasset"}]
        result = naming.propose_texture_names(material, bound, links)
        self.assertEqual(result["matchedCount"], 2)
        self.assertEqual(result["alignedCount"] + result["orderedCount"], 0)
        self.assertTrue(any("different parameter offsets" in item["reason"]
                            for item in result["ambiguous"]))

    def test_equal_counts_do_not_prove_the_slot_order(self):
        material = naming.parse_material_parameters(MATERIAL)
        bound = [{"resourceId": 10}, {"resourceId": 11}, {"resourceId": 12}]
        result = naming.propose_texture_names(material, bound, [])
        self.assertEqual(result["proposals"], [])
        self.assertEqual(result["unnamedSlots"], [0, 1, 2])

    def test_unequal_counts_name_nothing(self):
        material = naming.parse_material_parameters(MATERIAL)
        result = naming.propose_texture_names(material, [{"resourceId": 10}, {"resourceId": 11}], [])
        self.assertEqual(result["proposals"], [])


class AnchorTests(unittest.TestCase):
    """The ways a draw can be placed against a material, and what each is worth."""

    ROUND = {
        "Name": "MI_Round", "Type": "MaterialInstanceConstant",
        "Properties": {"ScalarParameterValues": [
            {"ParameterInfo": {"Name": name}, "ParameterValue": value}
            for name, value in [("Opacity", 1.0), ("Metallic", 0.0), ("Roughness", 0.5),
                                ("AO", 1.0), ("Tiling", 2.0), ("RimPower", 4.0)]]},
    }

    def test_an_ordered_window_places_a_run_no_single_value_could(self):
        # Every parameter is a number every material holds, so equality proves
        # nothing anywhere. The *sequence* is what is rare.
        material = naming.parse_material_parameters(self.ROUND)
        blocks = [{"bind": 3, "space": 0,
                   "floats": [0.0, 0.0, 0.0, 1.0, 0.0, 0.5, 1.0, 2.0, 4.0, 7.5]}]
        result = naming.propose_member_names(material, blocks)
        by_lane = {item["lane"]: item["parameter"] for item in result["proposals"]}
        self.assertEqual([by_lane[lane] for lane in range(3, 9)],
                         ["Opacity", "Metallic", "Roughness", "AO", "Tiling", "RimPower"])
        self.assertEqual(result["matchedCount"], 0)
        window = [item for item in result["anchors"] if item["kind"] == "window"]
        self.assertEqual(window[0]["strength"], "strong")
        self.assertIn("no other offset", window[0]["evidence"])

    def test_a_run_of_one_repeated_value_is_not_a_pattern(self):
        material = naming.parse_material_parameters({
            "Name": "MI_Flat", "Type": "MaterialInstanceConstant",
            "Properties": {"ScalarParameterValues": [
                {"ParameterInfo": {"Name": name}, "ParameterValue": 1.0}
                for name in ("A", "B", "C", "D")]}})
        blocks = [{"bind": 0, "space": 0, "floats": [1.0] * 12}]
        result = naming.propose_member_names(material, blocks)
        self.assertEqual(result["proposals"], [])
        self.assertEqual([item for item in result["anchors"] if item["kind"] == "window"], [])

    def test_a_window_that_fits_in_several_places_says_so_and_stays_weak(self):
        material = naming.parse_material_parameters({
            "Name": "MI_Repeat", "Type": "MaterialInstanceConstant",
            "Properties": {"ScalarParameterValues": [
                {"ParameterInfo": {"Name": name}, "ParameterValue": value}
                for name, value in [("A", 1.0), ("B", 0.0), ("C", 1.0)]]}})
        # The same three values occur twice in the block: order alone cannot choose.
        blocks = [{"bind": 0, "space": 0, "floats": [1.0, 0.0, 1.0, 9.5, 1.0, 0.0, 1.0]}]
        result = naming.propose_member_names(material, blocks)
        self.assertEqual(result["proposals"], [])
        self.assertTrue(any("does not choose between them" in item["evidence"]
                            for item in result["anchors"] if item["kind"] == "window"))
        self.assertTrue(any("different parameter offsets" in item.get("reason", "")
                            for item in result["ambiguous"]))

    def test_an_anchor_from_the_agent_settles_what_the_host_cannot(self):
        material = naming.parse_material_parameters({
            "Name": "MI_Repeat", "Type": "MaterialInstanceConstant",
            "Properties": {"ScalarParameterValues": [
                {"ParameterInfo": {"Name": name}, "ParameterValue": value}
                for name, value in [("A", 1.0), ("B", 0.0), ("C", 1.0)]]}})
        blocks = [{"bind": 0, "space": 0, "floats": [1.0, 0.0, 1.0, 9.5, 1.0, 0.0, 1.0]}]
        result = naming.propose_member_names(
            material, blocks,
            anchors=[{"fixes": "cbuffer-lane", "lane": 4, "parameter": "A",
                      "why": "the debug output shows this register driving alpha"}])
        by_lane = {item["lane"]: item["parameter"] for item in result["proposals"]}
        self.assertEqual([by_lane[lane] for lane in (4, 5, 6)], ["A", "B", "C"])
        self.assertNotIn(0, by_lane)
        self.assertTrue(any(item["kind"] == "agent" for item in result["anchors"]))

    def test_an_anchor_naming_a_parameter_the_material_lacks_is_refused(self):
        material = naming.parse_material_parameters(self.ROUND)
        with self.assertRaises(Exception) as raised:
            naming.propose_member_names(material, [{"bind": 0, "space": 0, "floats": [1.0]}],
                                        anchors=[{"lane": 0, "parameter": "BaseColorTint"}])
        self.assertIn("no scalar parameter called", str(raised.exception))

    def test_the_layout_is_read_from_the_data_not_assumed(self):
        # The cook put the vector first; the scalars-only layout cannot explain
        # lane 0..3, and the layout that can is the one reported.
        material = naming.parse_material_parameters({
            "Name": "MI_Packed", "Type": "MaterialInstanceConstant",
            "Properties": {
                "VectorParameterValues": [
                    {"ParameterInfo": {"Name": "Tint"},
                     "ParameterValue": {"R": 0.1273844, "G": 0.9182736, "B": 0.3746152, "A": 1.0}}],
                "ScalarParameterValues": [
                    {"ParameterInfo": {"Name": "Roughness"}, "ParameterValue": 0.5},
                    {"ParameterInfo": {"Name": "Metallic"}, "ParameterValue": 0.0},
                    {"ParameterInfo": {"Name": "Opacity"}, "ParameterValue": 1.0}]},
        })
        blocks = [{"bind": 1, "space": 0,
                   "floats": [0.1273844, 0.9182736, 0.3746152, 1.0, 0.5, 0.0, 1.0]}]
        result = naming.propose_member_names(material, blocks)
        by_lane = {item["lane"]: item["parameter"] for item in result["proposals"]}
        self.assertEqual([by_lane[lane] for lane in (4, 5, 6)],
                         ["Roughness", "Metallic", "Opacity"])
        self.assertIn("vectors-then-scalars",
                      [item["layout"] for item in result["layoutsTried"]])

    def test_a_texture_anchor_from_the_agent_is_one_inferred_slot(self):
        material = naming.parse_material_parameters(MATERIAL)
        bound = [{"resourceId": 7}, {"resourceId": 8}, {"resourceId": 9}, {"resourceId": 10}]
        result = naming.propose_texture_names(
            material, bound, [],
            anchors=[{"fixes": "texture-slot", "slot": 1, "parameter": "BaseMap",
                      "why": "I compared the bound pixels with the asset thumbnail"}])
        by_slot = {item["slot"]: item["markedName"] for item in result["proposals"]}
        self.assertEqual(by_slot, {1: "BaseMap_u"})
        self.assertFalse(result["proposals"][0]["contentMatchEligible"])
        self.assertFalse(result["proposals"][0]["autoApplyEligible"])
        self.assertEqual(result["withheldCount"], 1)
        self.assertEqual(result["unnamedSlots"], [0, 2, 3])

    def test_an_explicit_agent_content_match_is_rebound_and_auto_applied(self):
        material = naming.parse_material_parameters(MATERIAL)
        bound = [{"resourceId": 7, "bind": 3, "space": 2}]
        result = naming.propose_texture_names(
            material, bound, [], anchors=[{
                "fixes": "texture-slot", "slot": 0, "parameter": "BaseMap",
                "contentMatch": True,
                "asset": "/Game/.../T_Vulture01_Body_A01_D.uasset",
                "evidenceKind": "same-image",
                "resourceId": 7, "bindPoint": 3, "space": 2,
                "why": "I compared the decoded bound pixels with this unpacked texture",
            }])
        (proposal,) = result["proposals"]
        self.assertEqual(proposal["parameter"], "BaseMap")
        self.assertEqual(proposal["basis"], "matched")
        self.assertTrue(proposal["contentMatchEligible"])
        self.assertTrue(proposal["autoApplyEligible"])
        self.assertEqual(proposal["markedName"], "BaseMap")

    def test_agent_content_match_with_a_stale_live_tuple_is_withheld(self):
        material = naming.parse_material_parameters(MATERIAL)
        bound = [{"resourceId": 7, "bind": 3, "space": 2}]
        result = naming.propose_texture_names(
            material, bound, [], anchors=[{
                "fixes": "texture-slot", "slot": 0, "parameter": "BaseMap",
                "contentMatch": True,
                "asset": "/Game/.../T_Vulture01_Body_A01_D.uasset",
                "evidenceKind": "same-image",
                "resourceId": 8, "bindPoint": 3, "space": 2,
                "why": "content comparison from an older event",
            }])
        (proposal,) = result["proposals"]
        self.assertFalse(proposal["contentMatchEligible"])
        self.assertFalse(proposal["autoApplyEligible"])
        self.assertIn("does not match", proposal["withheldReason"])

    def test_agent_content_match_with_a_duplicate_live_tuple_is_withheld(self):
        material = naming.parse_material_parameters(MATERIAL)
        bound = [
            {"resourceId": 7, "bind": 3, "space": 2},
            {"resourceId": 7, "bind": 3, "space": 2},
        ]
        result = naming.propose_texture_names(
            material, bound, [], anchors=[{
                "fixes": "texture-slot", "slot": 0, "parameter": "BaseMap",
                "contentMatch": True,
                "asset": "/Game/.../T_Vulture01_Body_A01_D.uasset",
                "evidenceKind": "same-image",
                "resourceId": 7, "bindPoint": 3, "space": 2,
                "why": "decoded pixels match the unpacked texture",
            }])
        (proposal,) = result["proposals"]
        self.assertFalse(proposal["contentMatchEligible"])
        self.assertFalse(proposal["autoApplyEligible"])
        self.assertIn("not unique", proposal["withheldReason"])


class PackingTests(unittest.TestCase):
    """A constant buffer is 16-byte rows, and the padding that leaves is real."""

    PACKED = {
        "Name": "MI_Packed", "Type": "MaterialInstanceConstant",
        "Properties": {
            "ScalarParameterValues": [
                {"ParameterInfo": {"Name": name}, "ParameterValue": value}
                for name, value in [("Opacity", 1.0), ("Metallic", 0.0), ("Roughness", 0.5),
                                    ("AO", 1.0), ("Tiling", 2.0)]],
            "VectorParameterValues": [
                {"ParameterInfo": {"Name": "Tint"},
                 "ParameterValue": {"R": 0.7312845, "G": 0.2194736,
                                    "B": 0.4461523, "A": 1.0}}]},
    }

    def test_a_vector_is_bounced_to_the_next_register_and_the_gap_stays_unnamed(self):
        # Five scalars fill one row and a quarter; the float4 may not straddle, so
        # it starts at lane 8 and lanes 5..7 are padding no parameter occupies. A
        # model that packs end to end would look for the colour at lane 5, miss it,
        # and then name every scalar after it three lanes early.
        material = naming.parse_material_parameters(self.PACKED)
        blocks = [{"bind": 2, "space": 0,
                   "floats": [1.0, 0.0, 0.5, 1.0, 2.0, 0.0, 0.0, 0.0,
                              0.7312845, 0.2194736, 0.4461523, 1.0]}]
        result = naming.propose_member_names(material, blocks)
        by_lane = {item["lane"]: item["parameter"] for item in result["proposals"]}
        self.assertEqual([by_lane[lane] for lane in range(5)],
                         ["Opacity", "Metallic", "Roughness", "AO", "Tiling"])
        self.assertEqual([by_lane[lane] for lane in range(8, 12)], ["Tint"] * 4)
        self.assertFalse([lane for lane in (5, 6, 7) if lane in by_lane])

    def test_the_layout_places_the_padding_not_the_naming(self):
        from src import shader_anchors

        material = naming.parse_material_parameters(self.PACKED)
        layouts = shader_anchors.parameter_layouts(material)
        padded = next(item for item in layouts
                      if item["order"] == "scalars-then-vectors"
                      and "d3d-cbuffer" in item["packings"])
        placed = [(item["parameter"] + (item["component"] or ""), item["laneOffset"])
                  for item in padded["entries"]]
        self.assertEqual(placed[4], ("Tiling", 4))
        self.assertEqual(placed[5], ("TintR", 8))
        # Every platform rule agrees about a float4, so they arrive as one candidate.
        self.assertEqual(sorted(padded["packings"]),
                         ["d3d-cbuffer", "metal", "std140", "std430"])
        # ... and the order that puts the vector first needs no padding at all.
        first = next(item for item in layouts
                     if item["order"] == "vectors-then-scalars")["entries"]
        self.assertEqual([item["laneOffset"] for item in first[:6]], [0, 1, 2, 3, 4, 5])

    def test_the_platform_rules_are_merged_where_they_agree_and_split_where_they_do_not(self):
        from src import shader_anchors

        # A float2 is where D3D and std140 part company: D3D only forbids crossing
        # a 16-byte row, so lane 5 is legal; std140 wants an 8-byte alignment.
        material = {"scalars": [{"name": name, "value": float(index)}
                                for index, name in enumerate("abcde")],
                    "vectors": [{"name": "Tiling", "value": [2.0, 3.0]}]}
        layouts = [item for item in shader_anchors.parameter_layouts(material)
                   if item["order"] == "scalars-then-vectors"]
        by_rule = {name: item for item in layouts for name in item["packings"]}
        def first_component(item):
            return next(entry["laneOffset"] for entry in item["entries"]
                        if entry["component"] == "R")
        self.assertEqual(first_component(by_rule["d3d-cbuffer"]), 5)
        self.assertEqual(first_component(by_rule["std140"]), 6)
        self.assertIs(by_rule["std430"], by_rule["std140"])
        self.assertIs(by_rule["metal"], by_rule["std140"])

    def test_a_run_starting_with_a_vector_cannot_begin_off_a_register_boundary(self):
        material = naming.parse_material_parameters(self.PACKED)
        blocks = [{"bind": 2, "space": 0,
                   "floats": [0.0, 0.7312845, 0.2194736, 0.4461523, 1.0, 1.0, 0.0, 0.5]}]
        result = naming.propose_member_names(material, blocks)
        impossible = [item for item in result["alignments"]
                      if item["layout"].startswith("vectors-then-scalars")
                      and not item["possible"]]
        self.assertTrue(impossible)
        self.assertIn("16-byte register boundary", impossible[0]["reason"])


class FrameEvidenceTests(unittest.TestCase):
    """What the frame says about its own buffer, with no asset in the room."""

    def _variance(self, rows):
        from src import shader_anchors

        return shader_anchors.lane_variance(
            [{"eventId": event, "floats": floats} for event, floats in rows])

    def test_lanes_are_partitioned_by_how_they_behave_across_draws(self):
        parameters = [1.0, 0.0, 0.5, 1.0]
        same = self._variance([(100, [3.1, 4.2, 5.3] + parameters),
                               (140, [9.7, 2.2, 0.4] + parameters),
                               (180, [0.5, 8.8, 7.1] + parameters)])
        self.assertEqual(same["varyingLanes"], [0, 1, 2])
        self.assertEqual(same["constantLanes"], [3, 4, 5, 6])
        self.assertEqual([(item["from"], item["to"], item["constant"])
                          for item in same["regions"]],
                         [(0, 2, False), (3, 6, True)])

    def test_one_draw_cannot_say_anything_varies(self):
        single = self._variance([(100, [1.0, 2.0, 3.0])])
        self.assertEqual(single["lanes"], [])
        self.assertIn("at least two draws", single["note"])

    def test_two_comparisons_narrow_the_block_from_both_ends(self):
        from src import shader_anchors

        parameters = [1.0, 0.0, 0.5, 1.0]
        same = self._variance([(100, [3.1, 4.2, 5.3] + parameters),
                               (140, [9.7, 2.2, 0.4] + parameters)])
        other = self._variance([(100, [3.1, 4.2, 5.3] + parameters),
                                (220, [1.4, 6.6, 2.5] + [0.25, 1.0, 0.0, 1.0])])
        narrowed = shader_anchors.material_lane_candidates(same, other, lane_count=7)
        # 0..2 move every draw of the same material; lane 6 is 1.0 in both
        # materials, so it is the frame's, not this material's.
        self.assertEqual(narrowed["lanes"], [3, 4, 5])
        self.assertEqual(len(narrowed["excluded"]), 2)
        self.assertTrue(narrowed["applied"])

    def test_narrowing_removes_a_rival_offset_the_values_alone_could_not(self):
        # The material's four values appear twice in the block: once as the real
        # parameters and once, by coincidence, inside the per-object region. The
        # values cannot choose; the fact that one of the two regions changes every
        # draw can.
        parameters = [2.0, 0.5, 4.0, 0.25]
        material = naming.parse_material_parameters({
            "Name": "MI_Twice", "Type": "MaterialInstanceConstant",
            "Properties": {"ScalarParameterValues": [
                {"ParameterInfo": {"Name": name}, "ParameterValue": value}
                for name, value in zip(("Tiling", "Roughness", "RimPower", "Wet"), parameters)]}})
        floats = parameters + parameters
        blocks = [{"bind": 1, "space": 0, "floats": floats}]
        self.assertEqual(naming.propose_member_names(material, blocks)["proposals"], [])

        from src import shader_anchors

        same = shader_anchors.lane_variance([
            {"eventId": 10, "floats": floats},
            {"eventId": 20, "floats": [7.5, 1.5, 9.5, 3.5] + parameters}])
        narrowed = shader_anchors.material_lane_candidates(same, lane_count=8)
        self.assertEqual(narrowed["lanes"], [4, 5, 6, 7])
        result = naming.propose_member_names(material, blocks,
                                             candidate_lanes=narrowed["lanes"])
        by_lane = {item["lane"]: item["parameter"] for item in result["proposals"]}
        self.assertEqual([by_lane[lane] for lane in range(4, 8)],
                         ["Tiling", "Roughness", "RimPower", "Wet"])
        self.assertEqual(result["candidateLanes"]["count"], 4)


class ValueRelationTests(unittest.TestCase):
    """The number in the frame is not always the number in the file."""

    @staticmethod
    def _half(value):
        import struct

        return struct.unpack("<e", struct.pack("<e", value))[0]

    @staticmethod
    def _srgb_to_linear(value):
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    def _material(self):
        return naming.parse_material_parameters({
            "Name": "M", "Type": "MaterialInstanceConstant",
            "Properties": {
                "ScalarParameterValues": [
                    {"ParameterInfo": {"Name": "Roughness"}, "ParameterValue": 0.4417283},
                    {"ParameterInfo": {"Name": "Smooth"}, "ParameterValue": 0.7214938}],
                "VectorParameterValues": [
                    {"ParameterInfo": {"Name": "Tint"},
                     "ParameterValue": {"R": 0.2341897, "G": 0.5619234,
                                        "B": 0.8173645, "A": 1.0}}]},
        })

    def test_a_half_precision_round_trip_still_matches_and_says_so(self):
        # A mobile or console buffer stores the parameter as a half; the asset
        # holds the float32 it was authored as. Same parameter, different bits.
        blocks = [{"bind": 0, "space": 0,
                   "floats": [0.0, self._half(0.4417283), 0.0, 0.0]}]
        result = naming.propose_member_names(self._material(), blocks)
        matched = [item for item in result["proposals"] if item["basis"] == "matched"]
        self.assertEqual(matched[0]["parameter"], "Roughness")
        self.assertEqual(matched[0]["relations"], ["float16"])
        self.assertIn("float16", matched[0]["evidence"])

    def test_a_value_a_few_ulp_away_matches_at_any_magnitude(self):
        # The window is relative, not a fixed number of decimals: it has to hold
        # at 0.3 and at 87 alike, because a float that went through a preshader
        # comes back a few ulp away whatever its exponent.
        material = naming.parse_material_parameters({
            "Name": "M", "Type": "MaterialInstanceConstant",
            "Properties": {"ScalarParameterValues": [
                {"ParameterInfo": {"Name": "Small"}, "ParameterValue": 0.3182746},
                {"ParameterInfo": {"Name": "Large"}, "ParameterValue": 87.31462}]}})
        blocks = [{"bind": 0, "space": 0,
                   "floats": [0.3182746 * (1 - 4e-6), 0.0, 87.31462 * (1 + 2e-6), 0.0]}]
        result = naming.propose_member_names(material, blocks, fill_gaps=False)
        self.assertEqual({item["parameter"] for item in result["proposals"]},
                         {"Small", "Large"})
        # ... but a value that is merely close-ish is a different value.
        far = [{"bind": 0, "space": 0, "floats": [0.3182746 * 1.01, 0.0, 0.0, 0.0]}]
        self.assertEqual(naming.propose_member_names(material, far)["proposals"], [])

    def test_a_colour_converted_on_upload_matches_only_when_that_is_asked_for(self):
        linear = [self._srgb_to_linear(value) for value in
                  (0.2341897, 0.5619234, 0.8173645, 1.0)]
        blocks = [{"bind": 0, "space": 0, "floats": linear + [0.0, 0.0]}]
        # Off by default: a colour space conversion is a claim about what the
        # engine did, not something to assume.
        plain = naming.propose_member_names(self._material(), blocks, fill_gaps=False)
        self.assertFalse([item for item in plain["proposals"] if item["parameter"] == "Tint"])
        asked = naming.propose_member_names(
            self._material(), blocks, fill_gaps=False,
            relations=("exact", "float16", "near", "srgb-to-linear"))
        tint = [item for item in asked["proposals"] if item["parameter"] == "Tint"]
        self.assertEqual([item["component"] for item in tint], list("RGBA"))
        self.assertIn("srgb-to-linear", tint[0]["relations"])
        self.assertIn("srgb-to-linear", asked["relationsTried"])

    def test_a_semantic_flip_is_available_but_never_on_by_default(self):
        from src import shader_anchors

        self.assertNotIn("one-minus", shader_anchors.DEFAULT_RELATIONS)
        material = naming.parse_material_parameters({
            "Name": "M", "Type": "MaterialInstanceConstant",
            "Properties": {"ScalarParameterValues": [
                {"ParameterInfo": {"Name": "Roughness"}, "ParameterValue": 0.4417283}]}})
        blocks = [{"bind": 0, "space": 0, "floats": [0.0, 1.0 - 0.4417283, 0.0]}]
        self.assertEqual(naming.propose_member_names(material, blocks)["proposals"], [])
        flipped = naming.propose_member_names(
            material, blocks, relations=("exact", "one-minus"))
        self.assertEqual(flipped["proposals"][0]["relations"], ["one-minus"])


class LaneTypeTests(unittest.TestCase):
    """A constant buffer is not all floats, and the bits say which lanes are not."""

    def _bits(self, *values):
        import struct

        return [struct.unpack("<f", struct.pack("<I", value))[0] for value in values]

    def test_bits_that_cannot_be_a_parameter_are_named_as_such(self):
        from src import shader_anchors

        floats = [1.0, 2.5, 0.0] + self._bits(7, 0xFFFFFFFF, 1024)
        found = shader_anchors.implausible_float_lanes(floats)
        self.assertEqual(found["lanes"], [3, 4, 5])
        # Zero stays a candidate: an empty integer and a zero parameter look alike,
        # and zero is a perfectly ordinary parameter value.
        self.assertNotIn(2, found["lanes"])

    def test_the_bits_narrow_the_search_alongside_the_other_two_eliminations(self):
        from src import shader_anchors

        floats = [1.0, 2.5] + self._bits(7, 3)
        narrowed = shader_anchors.material_lane_candidates(
            lane_count=4, implausible=shader_anchors.implausible_float_lanes(floats))
        self.assertEqual(narrowed["lanes"], [0, 1])
        self.assertTrue(narrowed["applied"])
        self.assertIn("not a number a material parameter could be",
                      narrowed["excluded"][0]["reason"])


class IdentifierTests(unittest.TestCase):
    """An artist's parameter name is not an identifier, and both are needed."""

    def test_a_real_parameter_name_becomes_something_hlsl_accepts(self):
        self.assertEqual(naming.hlsl_identifier("Drop Tilling&Offset"), "Drop_Tilling_Offset")
        self.assertEqual(naming.hlsl_identifier("SDF Shadow HighLight Intensity Light"),
                         "SDF_Shadow_HighLight_Intensity_Light")
        self.assertEqual(naming.hlsl_identifier("2Sided"), "_2Sided")
        self.assertEqual(naming.hlsl_identifier("!!!"), "param")
        self.assertEqual(naming.hlsl_identifier("Wet Mask", {"Wet_Mask"}), "Wet_Mask_2")

    def test_every_proposal_carries_the_name_and_the_identifier(self):
        material = naming.parse_material_parameters({
            "Name": "MI_Real", "Type": "MaterialInstanceConstant",
            "Properties": {
                "ScalarParameterValues": [
                    {"ParameterInfo": {"Name": "Ink Strength"}, "ParameterValue": 0.5},
                    {"ParameterInfo": {"Name": "Drop Scale"}, "ParameterValue": 2.0},
                    {"ParameterInfo": {"Name": "Trail Speed"}, "ParameterValue": 4.0}]},
        })
        blocks = [{"bind": 2, "space": 0, "floats": [0.0, 0.5, 2.0, 4.0, 0.0]}]
        result = naming.propose_member_names(material, blocks)
        item = result["proposals"][0]
        # The material's own words survive for the report...
        self.assertEqual(item["name"], "Ink Strength")
        # ... and the spelling that can be written into the source sits beside it.
        self.assertEqual(item["identifier"], "Ink_Strength")
        self.assertTrue(item["markedIdentifier"].startswith("Ink_Strength"))
        self.assertTrue(all(" " not in entry["identifier"] for entry in result["proposals"]))

    def test_a_vector_component_gets_its_own_identifier(self):
        material = naming.parse_material_parameters({
            "Name": "MI_V", "Type": "MaterialInstanceConstant",
            "Properties": {"VectorParameterValues": [
                {"ParameterInfo": {"Name": "SDF DividingLine Color"},
                 "ParameterValue": {"R": 0.2341897, "G": 0.5619234,
                                    "B": 0.8173645, "A": 1.0}}]}})
        blocks = [{"bind": 0, "space": 0,
                   "floats": [0.2341897, 0.5619234, 0.8173645, 1.0]}]
        result = naming.propose_member_names(material, blocks)
        self.assertEqual([item["identifier"] for item in result["proposals"]],
                         ["SDF_DividingLine_Color_R", "SDF_DividingLine_Color_G",
                          "SDF_DividingLine_Color_B", "SDF_DividingLine_Color_A"])

    def test_texture_proposals_carry_an_identifier_and_their_bind_point(self):
        material = naming.parse_material_parameters(MATERIAL)
        bound = [{"resourceId": 1, "bindPoint": 7}, {"resourceId": 2, "bindPoint": 8},
                 {"resourceId": 3, "bindPoint": 9}]
        links = [{"slot": slot, "asset": item["objectPath"]}
                 for slot, item in enumerate(material["textures"])]
        result = naming.propose_texture_names(material, bound, links)
        self.assertEqual([item["identifier"] for item in result["proposals"]],
                         ["BaseMap", "NormalMap", "MRAMap"])
        self.assertEqual([item["bindPoint"] for item in result["proposals"]], [7, 8, 9])


class AliasRowTests(unittest.TestCase):
    """A rename reaches a lane the shader reads; an alias reaches the whole block."""

    def test_rows_are_composed_from_whatever_the_register_holds(self):
        proposals = [
            {"lane": 4, "bind": 2, "space": 0, "name": "Opacity", "markedName": "Opacity_u",
             "provisional": True},
            {"lane": 5, "bind": 2, "space": 0, "name": "Metallic", "markedName": "Metallic_u",
             "provisional": True},
            {"lane": 8, "bind": 2, "space": 0, "name": "Tint", "markedName": "Tint",
             "provisional": False, "component": "R", "workspaceSymbol": "__replaylab_s0_b2_c2_x_f32"},
            {"lane": 9, "bind": 2, "space": 0, "name": "Tint", "markedName": "Tint",
             "provisional": False, "component": "G"},
            {"lane": 10, "bind": 2, "space": 0, "name": "Tint", "markedName": "Tint",
             "provisional": False, "component": "B"},
            {"lane": 11, "bind": 2, "space": 0, "name": "Tint", "markedName": "Tint",
             "provisional": False, "component": "A"},
        ]
        rows = naming.alias_rows(proposals)
        self.assertEqual([item["row"] for item in rows], [1, 2])
        self.assertEqual([item["byteOffset"] for item in rows], [16, 32])
        # Two scalars sharing a register share one label...
        self.assertEqual(rows[0]["alias"], "Opacity_u | Metallic_u")
        # ... and a vector filling one says its name once, not four times.
        self.assertEqual(rows[1]["alias"], "Tint")
        # The row the shader actually reads is marked as such; the other is not,
        # and that is exactly the row a rename could never have reached.
        self.assertFalse(rows[0]["readByThisShader"])
        self.assertTrue(rows[1]["readByThisShader"])

    def test_a_label_too_long_for_the_panel_is_trimmed_not_rejected(self):
        proposals = [{"lane": index, "bind": 0, "space": 0,
                      "name": "Parameter With A Very Long Artist Name %d" % index,
                      "markedName": "Parameter With A Very Long Artist Name %d" % index,
                      "provisional": False} for index in range(4)]
        rows = naming.alias_rows(proposals)
        self.assertEqual(len(rows), 1)
        self.assertLessEqual(len(rows[0]["alias"]), 64)
        self.assertEqual(len(rows[0]["parameters"]), 4)


class MergeConflictTests(unittest.TestCase):
    def test_a_rival_layout_cannot_smuggle_a_parameter_in_at_another_lane(self):
        # Two candidate orders can explain the *same* vector at the same lanes and
        # then disagree completely about where the scalars are. The lanes never
        # collide, so a lane-only conflict test merges the rival's whole wrong run.
        material = naming.parse_material_parameters({
            "Name": "MI_Rival", "Type": "MaterialInstanceConstant",
            "Properties": {
                "VectorParameterValues": [
                    {"ParameterInfo": {"Name": "Tint"},
                     "ParameterValue": {"R": 0.2341897, "G": 0.5619234,
                                        "B": 0.8173645, "A": 0.4417283}}],
                "ScalarParameterValues": [
                    {"ParameterInfo": {"Name": "A"}, "ParameterValue": 2.0},
                    {"ParameterInfo": {"Name": "B"}, "ParameterValue": 0.5},
                    {"ParameterInfo": {"Name": "C"}, "ParameterValue": 4.0}]},
        })
        floats = [0.0] * 4 + [0.2341897, 0.5619234, 0.8173645, 0.4417283] + [2.0, 0.5, 4.0, 0.0]
        result = naming.propose_member_names(material, [{"bind": 1, "space": 0, "floats": floats}])
        by_parameter: dict[str, list[int]] = {}
        for item in result["proposals"]:
            by_parameter.setdefault(item["parameter"], []).append(item["lane"])
        for parameter, lanes in by_parameter.items():
            if parameter != "Tint":
                self.assertEqual(len(lanes), 1, "%s named at %s" % (parameter, lanes))
        self.assertEqual(sorted(by_parameter["Tint"]), [4, 5, 6, 7])


class EngineShapeTests(unittest.TestCase):
    """Every engine keeps named scalars, named vectors and named textures.

    Only the spelling differs, and this is where the spellings end -- what matches
    downstream never learns which engine the material came from."""

    def test_unity_material_properties_are_read(self):
        parsed = naming.parse_material_parameters({
            "m_Name": "M_Rock",
            "m_SavedProperties": {
                "m_Floats": [{"first": "_Glossiness", "second": 0.4417283},
                             {"first": "_Metallic", "second": 0.0}],
                "m_Colors": [{"first": "_Color",
                              "second": {"r": 0.2341897, "g": 0.5, "b": 0.25, "a": 1.0}}],
                "m_TexEnvs": [{"first": "_MainTex",
                               "second": {"m_Texture": {"m_Name": "T_Rock_D"}}}]},
        })
        self.assertEqual(parsed["parameterSource"], "unity")
        self.assertEqual(parsed["objectName"], "M_Rock")
        self.assertEqual([item["name"] for item in parsed["scalars"]],
                         ["_Glossiness", "_Metallic"])
        self.assertEqual(parsed["vectors"][0]["value"], [0.2341897, 0.5, 0.25, 1.0])
        self.assertEqual(parsed["textures"][0]["objectPath"], "T_Rock_D")

    def test_a_plain_shape_works_for_anything_else(self):
        parsed = naming.parse_material_parameters({
            "name": "whatever", "scalars": {"Roughness": 0.25, "Tiling": 2.0},
            "vectors": {"Tint": [1.0, 0.5, 0.25, 1.0]},
            "textures": {"Albedo": "tex/albedo"}})
        self.assertEqual(parsed["parameterSource"], "generic")
        self.assertEqual({item["name"] for item in parsed["scalars"]}, {"Roughness", "Tiling"})
        self.assertEqual(parsed["vectors"][0]["value"], [1.0, 0.5, 0.25, 1.0])

    def test_unreal_still_wins_and_says_so(self):
        parsed = naming.parse_material_parameters(MATERIAL)
        self.assertEqual(parsed["parameterSource"], "unreal")

    def test_a_document_with_no_parameters_says_what_it_looked_for(self):
        with self.assertRaises(naming.MaterialNamingError) as raised:
            naming.parse_material_parameters({"Name": "SK_Vulture", "Type": "SkeletalMesh"})
        message = str(raised.exception)
        self.assertIn("m_SavedProperties", message)
        self.assertIn("scalars", message)


class UnreadableBlockTests(unittest.TestCase):
    def test_a_block_with_no_readable_memory_is_named_not_skipped(self):
        # OpenGL default uniforms, D3D12 root constants and Vulkan push constants
        # are values in the command stream, not memory. Reporting that beats the
        # reader concluding the material was the wrong one.
        material = naming.parse_material_parameters(MATERIAL)
        result = naming.propose_member_names(material, [
            {"bind": 0, "space": 0, "floats": [], "status": "unavailable",
             "reason": "constant block has no bound buffer (inline/root constants or unbound)"},
            {"bind": 2, "space": 0, "floats": [0.8231471, 1.0, 0.6612934, 0.0]}])
        self.assertEqual(len(result["blocksUnreadable"]), 1)
        self.assertEqual(result["blocksUnreadable"][0]["block"], "b0:s0")
        self.assertIn("no bound buffer", result["blocksUnreadable"][0]["reason"])
        # ... and the block that could be read is still named.
        self.assertTrue(result["proposals"])


class ReaderChoiceTests(unittest.TestCase):
    """Which reader was right is reported and overridable, not a priority order."""

    BOTH = {
        "Name": "MI_Both", "Type": "MaterialInstanceConstant",
        "Properties": {"ScalarParameterValues": [
            {"ParameterInfo": {"Name": "Roughness"}, "ParameterValue": 0.5}]},
        "scalars": {"A": 1.0, "B": 2.0, "C": 3.0},
    }

    def test_every_reader_that_found_something_is_reported(self):
        parsed = naming.parse_material_parameters(self.BOTH)
        sources = {item["source"]: item for item in parsed["parameterSourceCandidates"]}
        self.assertEqual(set(sources), {"unreal", "generic"})
        self.assertEqual(sources["unreal"]["scalars"], 1)
        self.assertEqual(sources["generic"]["scalars"], 3)
        self.assertEqual(parsed["parameterSource"], "generic")
        self.assertIn("larger reading was taken", parsed["parameterSourceDecided"])

    def test_the_caller_can_overrule_the_choice(self):
        parsed = naming.parse_material_parameters(self.BOTH, source="unreal")
        self.assertEqual(parsed["parameterSource"], "unreal")
        self.assertEqual([item["name"] for item in parsed["scalars"]], ["Roughness"])
        self.assertIn("you asked", parsed["parameterSourceDecided"])

    def test_a_tie_says_it_is_a_tie_rather_than_picking_quietly(self):
        parsed = naming.parse_material_parameters({
            "Name": "MI_Tie", "Type": "MaterialInstanceConstant",
            "Properties": {"ScalarParameterValues": [
                {"ParameterInfo": {"Name": "R"}, "ParameterValue": 0.5}]},
            "scalars": {"A": 1.0}})
        self.assertIn("nothing here can tell which", parsed["parameterSourceDecided"])

    def test_an_unknown_source_names_the_ones_that_exist(self):
        with self.assertRaises(naming.MaterialNamingError) as raised:
            naming.parse_material_parameters(self.BOTH, source="godot")
        self.assertIn("unreal, unity, generic", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
