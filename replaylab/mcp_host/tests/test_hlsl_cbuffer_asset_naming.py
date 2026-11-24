from __future__ import annotations

import unittest

from src.shader_workspace.adapters.hlsl_cbuffer_abi_repair import (
    HlslCBufferAbiRepairError,
    repair_spirv_cross_hlsl_cbuffers,
)
from src.shader_workspace.asset_layout import (
    ASSET_LAYOUT_SCHEMA,
    AssetLayoutHintError,
    hint_from_relation_view,
    normalize_layout_hint,
)
from tests.test_hlsl_cbuffer_abi_repair import baseline, block, signature

SOURCE = """cbuffer _cb0 : register(b0, space0)
{
    float4 _cb0_m0[3] : packoffset(c0);
};

float4 MainPS() : SV_Target0
{
    float3 tint = _cb0_m0[0u].xyz;
    float cutoff = _cb0_m0[2u].x;
    uint mode = asuint(_cb0_m0[2u]).y;
    return float4(tint * cutoff, mode);
}
"""


def hint(fields, byte_size=44, key="0:0", relation=None):
    return normalize_layout_hint({
        "schema": ASSET_LAYOUT_SCHEMA, "schemaVersion": 1,
        "relation": relation or {"retriever": "test", "layoutHashEqual": True},
        "blocks": {key: {"byteSize": byte_size, "fields": fields}},
    })


ASSET_FIELDS = [
    {"name": "BaseColor", "byteOffset": 0, "byteSize": 16, "baseType": "float", "columns": 4},
    {"name": "Emissive", "byteOffset": 16, "byteSize": 16, "baseType": "float", "columns": 4},
    {"name": "Cutoff", "byteOffset": 32, "byteSize": 4, "baseType": "float"},
    {"name": "BlendMode", "byteOffset": 36, "byteSize": 4, "baseType": "uint"},
    {"name": "Padding", "byteOffset": 40, "byteSize": 4, "baseType": "float"},
]


class AssetNamingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.access = signature(block(0, 44, [(0, 4, "f32"), (4, 4, "f32"), (8, 4, "f32"), (32, 4, "f32"), (36, 4, "i32")]))
        self.blocks = [baseline(0, 44)]

    def test_asset_layout_names_members_and_keeps_unused_ones(self) -> None:
        result = repair_spirv_cross_hlsl_cbuffers(
            SOURCE, baseline_constant_blocks=self.blocks, dxil_access=self.access,
            asset_layout=hint(ASSET_FIELDS))
        text = result.hlsl
        self.assertIn("float4 BaseColor : packoffset(c0);", text)
        self.assertIn("float4 Emissive : packoffset(c1);", text)
        self.assertIn("float Cutoff : packoffset(c2.x);", text)
        self.assertIn("uint BlendMode : packoffset(c2.y);", text)
        self.assertIn("float Padding : packoffset(c2.z);", text)
        self.assertIn("float3(BaseColor.x, BaseColor.y, BaseColor.z)", text)
        self.assertIn("float cutoff = Cutoff;", text)
        self.assertIn("asfloat(BlendMode)", text)
        self.assertNotIn("__replaylab_", text)
        self.assertTrue(result.asset_layout_used)
        repaired = result.repaired_blocks[0]
        self.assertEqual((repaired.naming_source, repaired.asset_member_count, repaired.tail_guard_added), ("asset", 5, False))

    def test_wrong_version_asset_falls_back_to_lane_naming_with_reason(self) -> None:
        wrong = [dict(ASSET_FIELDS[0]), {"name": "Cutoff", "byteOffset": 16, "byteSize": 4, "baseType": "float"},
                 {"name": "Tail", "byteOffset": 20, "byteSize": 24, "baseType": "float", "columns": 4, "elements": 2}]
        result = repair_spirv_cross_hlsl_cbuffers(
            SOURCE, baseline_constant_blocks=self.blocks, dxil_access=self.access,
            asset_layout=hint(wrong))
        self.assertIn("__replaylab_s0_b0_c2_y_i32", result.hlsl)
        self.assertFalse(result.asset_layout_used)
        self.assertEqual(result.repaired_blocks[0].naming_source, "lane")
        self.assertIn("array is not register aligned", result.repaired_blocks[0].asset_fallback_reason)
        # Same shape, different size: the captured envelope wins.
        result = repair_spirv_cross_hlsl_cbuffers(
            SOURCE, baseline_constant_blocks=self.blocks, dxil_access=self.access,
            asset_layout=hint(ASSET_FIELDS, byte_size=48))
        self.assertIn("byte size differs", result.repaired_blocks[0].asset_fallback_reason)
        # A type contradiction at an accessed lane is a fallback, not a guess.
        typed = [dict(f) for f in ASSET_FIELDS]
        typed[3]["baseType"] = "float"
        result = repair_spirv_cross_hlsl_cbuffers(
            SOURCE, baseline_constant_blocks=self.blocks, dxil_access=self.access,
            asset_layout=hint(typed))
        self.assertIn("contradicts asset member BlendMode", result.repaired_blocks[0].asset_fallback_reason)
        # A colliding name in the HLSL also falls back.
        colliding = [dict(f) for f in ASSET_FIELDS]
        colliding[2]["name"] = "cutoff"
        result = repair_spirv_cross_hlsl_cbuffers(
            SOURCE, baseline_constant_blocks=self.blocks, dxil_access=self.access,
            asset_layout=hint(colliding))
        self.assertIn("already exists", result.repaired_blocks[0].asset_fallback_reason)

    def test_asset_output_is_deterministic_and_identical_without_hint_otherwise(self) -> None:
        plain = repair_spirv_cross_hlsl_cbuffers(SOURCE, baseline_constant_blocks=self.blocks, dxil_access=self.access)
        again = repair_spirv_cross_hlsl_cbuffers(SOURCE, baseline_constant_blocks=self.blocks, dxil_access=self.access,
                                                 asset_layout=hint(ASSET_FIELDS, key="0:7"))
        self.assertEqual(plain.hlsl, again.hlsl)
        self.assertEqual(plain.repaired_blocks, again.repaired_blocks)

    def test_hint_normalization_and_relation_view_selection(self) -> None:
        with self.assertRaises(AssetLayoutHintError):
            normalize_layout_hint({"schema": ASSET_LAYOUT_SCHEMA, "schemaVersion": 1, "relation": {}, "blocks": {}})
        view = {"assetCandidates": [
            {"candidateId": 1, "assetRecord": {"assetPath": "weak.bin"},
             "identityRelation": {"physicalIdentityProgramVerified": False, "score": 0.9, "basis": {}},
             "cbufferEvidence": {"asset": {"metadataTrusted": True, "blocks": [
                 {"space": 0, "bind": 0, "byteSize": 44, "fields": ASSET_FIELDS}]},
                 "layoutRelation": {"layoutHashEqual": False}}},
            {"candidateId": 2, "assetRecord": {"assetPath": "same.bin"},
             "identityRelation": {"physicalIdentityProgramVerified": True, "score": 0.5, "basis": {"rawHashEqual": True}},
             "cbufferEvidence": {"asset": {"metadataTrusted": True, "blocks": [
                 {"space": 0, "bind": 0, "byteSize": 44, "variables": [
                     {"name": "BaseColor", "offset": 0, "size": 16, "type": "float4", "columns": 4},
                     {"name": "Rest", "offset": 16, "size": 28, "type": "float", "elements": 2, "columns": 4}]}]},
                 "layoutRelation": {"layoutHashEqual": True}}},
        ]}
        picked = hint_from_relation_view(view)
        self.assertEqual(picked["relation"]["candidateId"], 2)
        self.assertEqual(picked["relation"]["retriever"], "symbol-database-identity")
        self.assertEqual([f["name"] for f in picked["blocks"]["0:0"]["fields"]], ["BaseColor", "Rest"])
        self.assertEqual(picked["blocks"]["0:0"]["fields"][0]["baseType"], "float")
        self.assertIsNone(hint_from_relation_view({"assetCandidates": [view["assetCandidates"][0]]}))


if __name__ == "__main__":
    unittest.main()


class PartialAssetNamingTests(AssetNamingTests):
    def test_partial_layout_names_known_members_and_keeps_lane_names_elsewhere(self) -> None:
        partial = [ASSET_FIELDS[0], ASSET_FIELDS[2]]  # BaseColor @0, Cutoff @32 only
        result = repair_spirv_cross_hlsl_cbuffers(
            SOURCE, baseline_constant_blocks=self.blocks, dxil_access=self.access,
            asset_layout=hint(partial))
        text = result.hlsl
        self.assertIn("float4 BaseColor : packoffset(c0);", text)
        self.assertIn("float Cutoff : packoffset(c2.x);", text)
        self.assertIn("uint __replaylab_s0_b0_c2_y_i32 : packoffset(c2.y);", text)
        self.assertIn("asfloat(__replaylab_s0_b0_c2_y_i32)", text)
        block = result.repaired_blocks[0]
        self.assertEqual(block.naming_source, "asset-partial")
        self.assertTrue(result.asset_layout_used)
        self.assertTrue(block.tail_guard_added)  # 44-byte block ends after the synthetic lane at 36
