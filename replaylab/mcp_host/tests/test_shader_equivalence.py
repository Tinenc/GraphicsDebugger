"""Rename-only equivalence, Host-applied renames, and factual verdicts."""

from __future__ import annotations

import unittest

from src.shader_workspace import equivalence as eq

BASELINE = """cbuffer cb0 : register(b0) { float4 v0 : packoffset(c0); float4x4 m0; };
Texture2D<float4> t0 : register(t0);
SamplerState s0 : register( s0 );
struct PSIn { float4 pos : SV_Position; float2 uv : TEXCOORD0; };
float3 f0(float3 a, float b) { return a * b + v0.xyz; }
float4 MainPS(PSIn i) : SV_Target0 {
  float4 c = t0.Sample(s0, i.uv);
  return float4(f0(c.rgb, v0.w), c.a);
}
"""
RENAMES = {"cb0": "MaterialCB", "v0": "tintAndAlpha", "t0": "tAlbedo", "s0": "sLinear", "f0": "applyTint", "uv": "texcoord"}


class RenameTests(unittest.TestCase):
    def test_host_applied_renames_keep_registers_and_are_rename_only_equivalent(self) -> None:
        clean = eq.validate_renames(BASELINE, RENAMES)
        proposal = eq.apply_renames(BASELINE, clean)
        self.assertIn("Texture2D<float4> tAlbedo : register(t0);", proposal)
        self.assertIn("SamplerState sLinear : register( s0 );", proposal)
        self.assertIn("float4 tintAndAlpha : packoffset(c0);", proposal)
        self.assertIn("tAlbedo.Sample(sLinear, i.texcoord)", proposal)
        proposal, missing = eq.insert_function_comments(
            proposal, {"applyTint": "Multiplies the sample by the tint.", "MainPS": "Entry point.", "ghost": "x"}
        )
        self.assertEqual(missing, ["ghost"])
        self.assertIn("// [ai] Multiplies the sample by the tint.\nfloat3 applyTint(", proposal)
        self.assertTrue(eq.rename_only_equivalent(BASELINE, proposal, clean)["equal"])
        # idempotent comments
        again, _ = eq.insert_function_comments(proposal, {"MainPS": "Entry point (v2)."})
        self.assertEqual(again.count("// [ai] Entry point"), 1)

    def test_unsafe_renames_are_refused_by_name(self) -> None:
        for renames, fragment in (
            ({"Sample": "x"}, "cannot be renamed"),
            ({"v0": "float4"}, "reserved HLSL word"),
            ({"v0": "t0"}, "reserved HLSL word"),
            ({"v0": "m0"}, "already exists"),
            ({"zzz": "y"}, "does not occur"),
            ({"v0": "SV_Target"}, "reserved HLSL word"),
            ({"v0": "newName", "m0": "newName"}, "would collide"),
        ):
            with self.assertRaises(eq.EquivalenceError) as raised:
                eq.validate_renames(BASELINE, renames)
            self.assertIn(fragment, str(raised.exception))

    def test_code_change_reports_a_source_location_without_directing_the_agent(self) -> None:
        clean = eq.validate_renames(BASELINE, RENAMES)
        proposal = eq.apply_renames(BASELINE, clean).replace("a * b", "b * a")
        result = eq.rename_only_equivalent(BASELINE, proposal, clean)
        self.assertFalse(result["equal"])
        first = result["differences"][0]
        self.assertEqual(first["baselineLine"], 5)
        self.assertIn("a * b", first["baseline"])
        self.assertIn("b * a", first["proposal"])
        reasons = eq.differences_to_reasons(result["differences"])
        self.assertEqual(reasons[0]["code"], "code-changed")
        self.assertEqual(reasons[0]["location"], "proposal.hlsl:5")
        self.assertNotIn("direction", reasons[0])
        self.assertTrue(reasons[0]["message"])

    def test_comments_and_whitespace_never_count_as_changes(self) -> None:
        proposal = "// header\n" + BASELINE.replace("{ return", "{\n    return /* tint */")
        self.assertTrue(eq.rename_only_equivalent(BASELINE, proposal)["equal"])


class SourceBoundRenameTests(unittest.TestCase):
    CBUFFER_SHADOWS = """cbuffer MaterialCB : register(b0) {
  float4 _2 : packoffset(c0);
};
struct OtherData { float4 _2; };
float4 ReadMaterial() { return _2; }
float4 MainPS(OtherData input) : SV_Target0 {
  float4 _2 = input._2;
  return _2 + ReadMaterial();
}
"""

    CONSTANT_BUFFER_ARRAY = """struct MaterialData { float4 _3; };
struct OtherData { float4 _3; };
ConstantBuffer<MaterialData> materials[4] : register(b1);
ConstantBuffer<MaterialData> fallback : register(b2);
float4 MainPS(OtherData materials, uint index : TEXCOORD0) : SV_Target0 {
  float4 _3 = materials._3;
  return fallback._3 + _3;
}
float4 ReadIndexed(uint index) { return materials[index]._3; }
"""

    def test_cbuffer_selector_renames_only_flat_physical_field_identity(self) -> None:
        clean = eq.validate_renames(self.CBUFFER_SHADOWS, {"MaterialCB._2": "materialTint"})
        proposal = eq.apply_renames(self.CBUFFER_SHADOWS, clean)
        self.assertIn("float4 materialTint : packoffset(c0);", proposal)
        self.assertIn("return materialTint;", proposal)
        self.assertIn("struct OtherData { float4 _2; };", proposal)
        self.assertIn("float4 _2 = input._2;", proposal)
        self.assertTrue(eq.rename_only_equivalent(self.CBUFFER_SHADOWS, proposal, clean)["equal"])

    def test_plain_identifier_with_field_and_local_identities_is_rejected(self) -> None:
        with self.assertRaisesRegex(eq.EquivalenceError, "multiple declaration identities"):
            eq.validate_renames(self.CBUFFER_SHADOWS, {"_2": "materialTint"})
        with self.assertRaisesRegex(eq.EquivalenceError, "multiple declaration identities"):
            eq.apply_renames(self.CBUFFER_SHADOWS, {"_2": "materialTint"})

    def test_constant_buffer_array_owner_selects_struct_field_not_shadowing_owner(self) -> None:
        clean = eq.validate_renames(
            self.CONSTANT_BUFFER_ARRAY, {"materials._3": "packedMaterialColor"}
        )
        proposal = eq.apply_renames(self.CONSTANT_BUFFER_ARRAY, clean)
        self.assertIn("struct MaterialData { float4 packedMaterialColor; };", proposal)
        self.assertIn("fallback.packedMaterialColor", proposal)
        self.assertIn("materials[index].packedMaterialColor", proposal)
        self.assertIn("OtherData { float4 _3; }", proposal)
        self.assertIn("float4 _3 = materials._3;", proposal)
        self.assertTrue(eq.rename_only_equivalent(self.CONSTANT_BUFFER_ARRAY, proposal, clean)["equal"])

    def test_function_local_selector_separates_same_spelling_between_functions(self) -> None:
        source = """float4 First() { float4 _9 = 1.0; return _9; }
float4 Second() { float4 _9 = 2.0; return _9; }
"""
        with self.assertRaisesRegex(eq.EquivalenceError, "multiple declaration identities"):
            eq.validate_renames(source, {"_9": "value"})
        clean = eq.validate_renames(source, {"First::_9": "firstValue"})
        proposal = eq.apply_renames(source, clean)
        self.assertIn("float4 firstValue = 1.0; return firstValue;", proposal)
        self.assertIn("float4 _9 = 2.0; return _9;", proposal)
        self.assertTrue(eq.rename_only_equivalent(source, proposal, clean)["equal"])

    def test_selectors_written_against_prior_owner_and_function_renames_are_canonicalized(self) -> None:
        source = """cbuffer cb0 { float4 _4; };
float4 Helper() { float4 _5 = _4; return _5; }
"""
        requested = {
            "cb0": "MaterialCB",
            "MaterialCB._4": "materialColor",
            "Helper": "ReadMaterial",
            "ReadMaterial::_5": "resultColor",
        }
        clean = eq.validate_renames(source, requested)
        self.assertIn("cb0._4", clean)
        self.assertIn("Helper::_5", clean)
        proposal = eq.apply_renames(source, clean)
        self.assertIn("cbuffer MaterialCB { float4 materialColor; };", proposal)
        self.assertIn("float4 ReadMaterial() { float4 resultColor = materialColor; return resultColor; }", proposal)
        self.assertTrue(eq.rename_only_equivalent(source, proposal, clean)["equal"])

    def test_function_local_selector_refuses_shadowed_declarations(self) -> None:
        source = """float4 MainPS() : SV_Target0 {
  float4 _7 = 1.0;
  if (_7.x > 0.0) { float4 _7 = 2.0; }
  return _7;
}
"""
        with self.assertRaisesRegex(eq.EquivalenceError, "not a unique declaration"):
            eq.validate_renames(source, {"MainPS::_7": "value"})

    def test_unsupported_or_unbound_selector_is_explicitly_rejected(self) -> None:
        with self.assertRaisesRegex(eq.EquivalenceError, "unsupported rename selector"):
            eq.validate_renames(self.CBUFFER_SHADOWS, {"MaterialCB[0]._2": "value"})
        with self.assertRaisesRegex(eq.EquivalenceError, "does not identify one"):
            eq.validate_renames(self.CBUFFER_SHADOWS, {"Missing._2": "value"})

    def test_glsl_interface_is_protected_while_scoped_local_can_rename(self) -> None:
        source = """layout(location = 0) in vec2 _8;
layout(location = 0) out vec4 outColor;
void helper() { vec2 _8 = vec2(1.0); outColor = vec4(_8, 0.0, 1.0); }
void main() { helper(); }
"""
        with self.assertRaisesRegex(eq.EquivalenceError, "multiple declaration identities"):
            eq.validate_renames(source, {"_8": "uv"})
        clean = eq.validate_renames(source, {"helper::_8": "localUv"})
        proposal = eq.apply_renames(source, clean)
        self.assertIn("in vec2 _8;", proposal)
        self.assertIn("vec2 localUv = vec2(1.0)", proposal)
        self.assertIn("vec4(localUv, 0.0, 1.0)", proposal)


class VerdictTests(unittest.TestCase):
    def test_verdict_reports_facts_without_review_budget_or_next_action(self) -> None:
        verdict = eq.build_verdict(
            accepted=False,
            level="rejected",
            reasons=[eq.reason(
                "code-changed", "line 5 differs", location="proposal.hlsl:5",
                direction="revert", target="old source", example="old identifier",
            )],
        )
        self.assertEqual(verdict["schema"], eq.REVIEW_VERDICT_SCHEMA)
        self.assertEqual(verdict["accepted"], False)
        self.assertEqual(verdict["reasons"][0]["location"], "proposal.hlsl:5")
        for key in ("direction", "target", "example", "round", "maxRounds", "nextStep"):
            self.assertNotIn(key, verdict)
            self.assertNotIn(key, verdict["reasons"][0])

    def test_accepted_verdict_cannot_hide_physical_blocker(self) -> None:
        with self.assertRaisesRegex(ValueError, "blocking review reasons"):
            eq.build_verdict(
                accepted=True, level="exact-code",
                reasons=[eq.reason("abi-incompatible", "candidate ABI differs")],
            )


class DiagramAndAnalysisTests(unittest.TestCase):
    def test_mermaid_is_deterministic_and_safe(self) -> None:
        graph = {
            "entryPoint": "MainPS",
            "nodes": [
                {"id": "a", "name": "MainPS", "kind": "entry-point", "description": "entry",
                 "signature": "float4 MainPS(float2 uv) : SV_Target0", "inputs": ["uv"],
                 "outputs": ["SV_Target0"]},
                {"id": "b", "name": "applyTint", "kind": "function", "description": 'tint "x" <b>',
                 "signature": "float3 applyTint(float3 color)", "inputs": ["color"],
                 "outputs": ["return"]},
                {"id": "stage", "name": "MainPS", "label": "Execute assignment",
                 "kind": "computation", "description": "float hidden = uv.x;",
                 "sourceRange": {"startLine": 4, "endLine": 4}},
            ],
            "edges": [{"source": "a", "target": "b", "kind": "calls"},
                      {"source": "stage", "target": "a", "kind": "data",
                       "values": ["hidden"]}],
        }
        text = eq.mermaid_from_flow_graph(graph)
        self.assertTrue(text.startswith("flowchart TD\n"))
        self.assertIn("MainPS — entry", text)
        self.assertNotIn("signature:", text)
        self.assertIn("in: uv", text)
        self.assertIn("out: SV_Target0", text)
        self.assertIn("n0 --> n1", text)
        self.assertNotIn("<b>", text)
        self.assertNotIn("Execute assignment", text)
        self.assertNotIn("float hidden", text)
        self.assertNotIn("hidden", text)
        self.assertEqual(text, eq.mermaid_from_flow_graph(graph))
        self.assertIn("classDef entry", eq.mermaid_from_flow_graph({"nodes": [], "edges": []}))

class FlowSpecTests(unittest.TestCase):
    SOURCE = """cbuffer MaterialCB : register(b0) { float4 tint; };
Texture2D<float4> tAlbedo : register(t0);
SamplerState sLinear : register(s0);
float3 applyTint(float3 a) { return a * tint.rgb; }
float4 MainPS(float2 uv : TEXCOORD0) : SV_Target0 { return float4(applyTint(tAlbedo.Sample(sLinear, uv).rgb), 1); }
"""

    def test_template_lists_declared_symbols_and_functions(self) -> None:
        symbols = eq.declared_symbols(self.SOURCE)
        self.assertEqual(symbols, {"resources": ["tAlbedo", "sLinear"], "cbuffers": ["MaterialCB"], "outputs": ["SV_Target0"]})
        template = eq.flow_spec_template(self.SOURCE, ["applyTint", "MainPS"], "MainPS")
        self.assertEqual(template["schema"], eq.FLOW_SPEC_SCHEMA)
        self.assertEqual([s["function"] for s in template["stages"]], ["MainPS"])
        self.assertEqual(template["stages"][0]["next"], [])
        self.assertEqual([item["function"] for item in template["computationRanges"]], ["applyTint", "MainPS"])
        self.assertIn("MaterialCB", template["resources"])

    def test_valid_spec_renders_fixed_layout_and_invalid_spec_is_explained(self) -> None:
        spec = {
            "schema": eq.FLOW_SPEC_SCHEMA,
            "inputs": ["uv"],
            "resources": ["tAlbedo", "sLinear", "MaterialCB"],
            "outputs": ["SV_Target0"],
            "stages": [
                {"id": "sample", "label": "Sample albedo at uv", "function": "MainPS", "reads": ["uv", "tAlbedo", "sLinear"], "next": ["tint"]},
                {"id": "tint", "label": "Multiply by material tint", "function": "applyTint", "reads": ["MaterialCB.tint"], "writes": ["SV_Target0"]},
            ],
        }
        bindings = eq.flow_value_bindings(self.SOURCE)
        clean, reasons = eq.validate_flow_spec(spec, functions=["applyTint", "MainPS"], resources=["tAlbedo", "sLinear", "MaterialCB"], outputs=["SV_Target0"], inputs=bindings["inputs"], fields=bindings["fields"])
        self.assertEqual(reasons, [])
        text = eq.mermaid_from_flow_spec(clean)
        self.assertTrue(text.startswith("flowchart TD\n"))
        for fragment in ("subgraph IN[Inputs]", "subgraph RES[Resources]", "subgraph FLOW[Computation]", "subgraph OUT[Outputs]", "==> ", "MaterialCB.tint"):
            self.assertIn(fragment, text)
        self.assertNotIn("<", text)
        self.assertEqual(text, eq.mermaid_from_flow_spec(clean))
        bad = dict(spec, stages=[
            {"id": "Bad Id", "label": "x"},
            {"id": "ok", "label": 'has "quotes"', "function": "MainPS"},
            {"id": "ghost", "label": "fine", "function": "nope", "next": ["missing"]},
            {"id": "w", "label": "writes wrong", "function": "MainPS", "writes": ["SV_Target7"], "reads": ["tNotDeclared"]},
        ])
        _clean, reasons = eq.validate_flow_spec(bad, functions=["applyTint", "MainPS"], resources=["tAlbedo"], outputs=["SV_Target0"])
        codes = [r["code"] for r in reasons]
        for code in ("flow-spec-id", "flow-spec-text", "flow-spec-function", "flow-spec-output", "flow-spec-read"):
            self.assertIn(code, codes)
        self.assertTrue(all(r["message"] for r in reasons))
        self.assertTrue(all("direction" not in r for r in reasons))


if __name__ == "__main__":
    unittest.main()


class SwizzleRenameTests(unittest.TestCase):
    """A swizzle is component selection, not a name.

    Renaming one changes which channels the shader reads while still compiling,
    still having an identical ABI, and -- because the same map is applied to the
    baseline before the two are compared -- still proving "rename-only equivalent".
    Every gate in the pipeline accepted it.
    """

    BASELINE = ("float4 main(float4 uv : TEXCOORD0) : SV_Target\n"
                "{\n"
                "  float c = uv.xy.x;\n"
                "  return float4(c, c, c, 1);\n"
                "}\n")

    def test_a_swizzle_cannot_be_renamed(self) -> None:
        with self.assertRaises(eq.EquivalenceError) as refusal:
            eq.validate_renames(self.BASELINE, {"xy": "xz"})
        self.assertIn("swizzle", str(refusal.exception))

    def test_applying_a_swizzle_rename_changes_nothing(self) -> None:
        """Defence in depth: even handed a map that reached apply directly."""

        applied = eq.apply_renames(self.BASELINE, {"xy": "xz"})
        self.assertEqual(applied, self.BASELINE)

    def test_a_variable_whose_name_looks_like_a_swizzle_still_renames(self) -> None:
        """The common accidental case: a decompiler local called `x` or `r`."""

        source = ("float4 main() : SV_Target\n"
                  "{\n"
                  "  float x = 1.0;\n"
                  "  float3 v = float3(x, x, x);\n"
                  "  return float4(v.xy, v.z, x);\n"
                  "}\n")
        clean = eq.validate_renames(source, {"x": "brightness"})
        applied = eq.apply_renames(source, clean)
        self.assertIn("float brightness = 1.0;", applied)
        # ...and its swizzles are untouched.
        self.assertIn("v.xy", applied)
        self.assertIn("v.z", applied)
        self.assertNotIn("v.brightness", applied)

    def test_a_real_field_rename_after_a_dot_still_works(self) -> None:
        source = ("struct Params { float intensity; };\n"
                  "float4 main(Params p) : SV_Target { return p.intensity.xxxx; }\n")
        clean = eq.validate_renames(source, {"intensity": "strength"})
        applied = eq.apply_renames(source, clean)
        self.assertIn("p.strength.xxxx", applied)


class PhysicalDeclarationSelectorTests(unittest.TestCase):
    def test_block_member_and_resource_selectors_bind_duplicate_spellings(self) -> None:
        source = (
            "struct Params0 { float _1; };\n"
            "struct Params1 { float _1; };\n"
            "ConstantBuffer<Params0> cb0 : register(b0);\n"
            "ConstantBuffer<Params1> cb1 : register(b1);\n"
            "Texture2D _32 : register(t0);\n"
            "float4 MainPS(float _32 : TEXCOORD0) : SV_Target { return cb0._1 + cb1._1 + _32.x; }\n"
        )
        renames = {
            "cbuffer:cb0": "CBufferS0B0",
            "cbuffer:cb1": "CBufferS0B1",
            "cb0._1": "cbufferS0B0ScalarAtByte0",
            "cb1._1": "cbufferS0B1ScalarAtByte0",
            "resource:_32": "tTextureS0T0",
        }
        clean = eq.validate_renames(source, renames)
        applied = eq.apply_renames(source, clean)
        self.assertIn("ConstantBuffer<Params0> CBufferS0B0", applied)
        self.assertIn("ConstantBuffer<Params1> CBufferS0B1", applied)
        self.assertIn("float cbufferS0B0ScalarAtByte0", applied)
        self.assertIn("float cbufferS0B1ScalarAtByte0", applied)
        self.assertIn("Texture2D tTextureS0T0", applied)
        self.assertIn("MainPS(float _32", applied)

    def test_typed_texture_buffer_uses_cbuffer_selector(self) -> None:
        source = (
            "struct Params { float _1; };\n"
            "TextureBuffer<Params> cb0 : register(b0);\n"
            "float MainPS() : SV_Target { return cb0._1; }"
        )
        applied = eq.apply_renames(source, eq.validate_renames(source, {
            "cbuffer:cb0": "CBufferS0B0",
            "cb0._1": "cbufferS0B0ScalarAtByte0",
        }))
        self.assertIn("TextureBuffer<Params> CBufferS0B0", applied)
        self.assertIn("CBufferS0B0.cbufferS0B0ScalarAtByte0", applied)
