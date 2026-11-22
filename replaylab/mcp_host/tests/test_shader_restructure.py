from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest.mock import patch

from src.shader_workspace.equivalence import apply_renames, rename_only_equivalent, validate_renames
from src.shader_workspace.restructure import (
    RestructureError,
    _function_spans,
    anchor_extractions,
    apply_operations,
    extract_function,
    prepare_rewrite,
    structure_report,
)


def test_large_inferred_interface_reports_counts_without_a_review_order():
    names = ["value%d" % i for i in range(26)]
    source = "float Main(%s)\n{\n    float sum = %s;\n    return sum;\n}\n" % (
        ", ".join("float " + name for name in names), " + ".join(names),
    )
    result = extract_function(source, 3, 3, "SumInputs")
    assert result["info"]["parameterCount"] == 26
    assert result["info"]["inputs"] == names
    assert "float SumInputs(" in result["source"]
    assert result["info"]["interface"] == {
        "inputCount": 26, "inoutCount": 0, "outputCount": 1,
    }
    assert "interfaceReview" not in result["info"]


def test_fifty_two_live_outputs_are_reported_as_interface_facts():
    body = ["    float contribution%d = inputValue + %d.0;" % (index, index)
            for index in range(52)]
    source = (
        "float Main(float inputValue)\n{\n"
        + "\n".join(body)
        + "\n    return "
        + " + ".join("contribution%d" % index for index in range(52))
        + ";\n}\n"
    )
    result = extract_function(source, 3, 54, "PrepareContributions")
    info = result["info"]
    assert len(info["outParams"]) == 52
    assert info["interface"]["outputCount"] == 52
    assert "interfaceReview" not in info


SOURCE = "\r\n".join([
    "cbuffer View : register(b0)",
    "{",
    "    float4 ViewSize : packoffset(c0);",
    "    float Exposure : packoffset(c1.x);",
    "};",
    "Texture2D<float4> BaseColor : register(t0);",
    "SamplerState Linear : register(s0);",
    "static float2 in_uv;",
    "static float4 out_color;",
    "",
    "void frag_main()",
    "{",
    "    float2 _10 = in_uv * ViewSize.xy;",          # 13
    "    float4 _11 = BaseColor.Sample(Linear, _10);",  # 14
    "    float _12 = _11.x * Exposure;",                # 15
    "    float _13 = _11.y * Exposure;",                # 16
    "    float3 _14 = float3(_12, _13, _11.z);",        # 17
    "    float _15 = dot(_14, float3(0.2126f, 0.7152f, 0.0722f));",  # 18
    "    if (_15 > 1.0f)",                                # 19
    "    {",                                              # 20
    "        _14 = _14 / _15;",                           # 21
    "    }",                                              # 22
    "    out_color = float4(_14, 1.0f);",                 # 23
    "}",
    "",
    "float4 MainPS(float2 uv : TEXCOORD0) : SV_Target0",
    "{",
    "    in_uv = uv;",
    "    frag_main();",
    "    return out_color;",
    "}",
    "",
])


NESTED_OUTPUT_SOURCE = "\n".join([
    "float4 MainPS(float4 inputValue : TEXCOORD0) : SV_Target",
    "{",
    "    uint mode = uint(inputValue.x) & 7u;",
    "    bool selected = mode == 1u;",
    "    float value;",
    "    if (selected)",
    "    {",
    "        value = inputValue.y * 2.0f;",
    "    }",
    "    else",
    "    {",
    "        float branchValue;",
    "        if (mode == 2u)",
    "        {",
    "            branchValue = inputValue.y * 3.0f;",
    "        }",
    "        else",
    "        {",
    "            float nestedValue;",
    "            if (mode == 3u)",
    "            {",
    "                nestedValue = inputValue.z * 4.0f;",
    "            }",
    "            else",
    "            {",
    "                nestedValue = inputValue.w * 5.0f;",
    "            }",
    "            branchValue = nestedValue;",
    "        }",
    "        value = branchValue;",
    "    }",
    "    return float4(value, float(mode), selected ? 1.0f : 0.0f, 1.0f);",
    "}",
    "",
])


def test_structure_report_reuses_only_exact_source_and_entry_point():
    from src.shader_workspace import restructure
    from src.shader_workspace import outline as outline_module

    restructure._clear_structure_report_cache()
    with patch.object(outline_module, "outline", wraps=outline_module.outline) as scan:
        first = structure_report(SOURCE, "MainPS")
        repeated = structure_report(SOURCE, "MainPS")
        assert first == repeated
        assert scan.call_count == 1
        repeated["functions"][0]["name"] = "tampered"
        assert structure_report(SOURCE, "MainPS") == first
        assert scan.call_count == 1
        structure_report(SOURCE.replace("0.2126f", "0.3f"), "MainPS")
        structure_report(SOURCE, "frag_main")
        assert scan.call_count == 3
    restructure._clear_structure_report_cache()


class ExtractTests(unittest.TestCase):
    def test_extraction_reuses_region_program_parse(self) -> None:
        from src.shader_workspace import inspection

        with patch.object(inspection, "_tokens", wraps=inspection._tokens) as tokens, \
             patch.object(inspection, "_function_records",
                          wraps=inspection._function_records) as records:
            result = extract_function(SOURCE, 15, 17, "ApplyExposure")
        self.assertIn("float3 ApplyExposure", result["source"])
        self.assertEqual(tokens.call_count, 1)
        self.assertEqual(records.call_count, 1)

    def test_extract_returns_single_output_and_passes_inputs(self) -> None:
        result = extract_function(SOURCE, 15, 17, "ApplyExposure", comment="Scale the sampled colour by exposure.")
        info = result["info"]
        self.assertEqual(info["inputs"], ["_11"])
        self.assertEqual(info["returns"], "_14")
        self.assertEqual(info["returnType"], "float3")
        text = result["source"]
        self.assertIn("float3 ApplyExposure(float4 _11)", text)
        self.assertIn("    return _14;", text)
        self.assertIn("    float3 _14 = ApplyExposure(_11);", text)
        self.assertIn("// Scale the sampled colour by exposure.", text)
        # the helper is placed above frag_main and the file still has both callers
        self.assertLess(text.index("float3 ApplyExposure"), text.index("void frag_main"))
        self.assertNotIn("float _12 = _11.x * Exposure;\r\n    float _13", text.split("void frag_main")[1])

    def test_extract_with_inout_and_block(self) -> None:
        result = extract_function(SOURCE, 18, 22, "NormalizeLuminance")
        info = result["info"]
        self.assertEqual(info["inouts"], ["_14"])
        self.assertEqual(info["inputs"], [])
        self.assertEqual(info["returns"], None)
        self.assertIn("void NormalizeLuminance(inout float3 _14)", result["source"])
        self.assertIn("    NormalizeLuminance(_14);", result["source"])

    def test_multiple_outputs_become_out_parameters(self) -> None:
        result = extract_function(SOURCE, 15, 16, "SplitChannels")
        info = result["info"]
        self.assertEqual(info["outParams"], ["_12", "_13"])
        self.assertIn("void SplitChannels(float4 _11, out float replaylab_out__12, out float replaylab_out__13)", result["source"])
        self.assertIn("replaylab_out__12 = replaylab_local__12", result["source"])
        self.assertIn("replaylab_out__13 = replaylab_local__13", result["source"])
        self.assertIn("float replaylab_local__12", result["source"])
        self.assertIn("float replaylab_local__13", result["source"])
        self.assertIn("    float _12;\r\n    float _13;\r\n    SplitChannels(_11, _12, _13);", result["source"])
        self.assertEqual(info["generatedNames"]["_12"]["local"], "replaylab_local__12")
        self.assertEqual(info["generatedNames"]["_13"]["parameter"], "replaylab_out__13")

    def test_output_local_renames_preserve_nested_conditional_scopes(self) -> None:
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                source = NESTED_OUTPUT_SOURCE.replace("\n", newline)
                result = extract_function(
                    source, 3, len(source.splitlines()) - 2, "SelectModeBlend")
                self.assertEqual(result["info"]["outParams"], ["mode", "selected", "value"])
                helper = result["source"].split("float4 MainPS", 1)[0]
                self.assertEqual(helper.count("{"), 7)
                self.assertEqual(helper.count("}"), 7)
                self.assertIn(newline.join([
                    "    else",
                    "    {",
                    "        float branchValue;",
                    "        if (replaylab_local_mode == 2u)",
                    "        {",
                    "            branchValue = inputValue.y * 3.0f;",
                    "        }",
                    "        else",
                    "        {",
                    "            float nestedValue;",
                ]), helper)
                self.assertIn(newline.join([
                    "            branchValue = nestedValue;",
                    "        }",
                    "        replaylab_local_value = branchValue;",
                    "    }",
                    "    replaylab_out_mode = replaylab_local_mode;",
                ]), helper)
                self.assertNotIn("__replaylab_extract_body", result["source"])

    def test_qualified_inputs_and_single_output_keep_precision_and_const(self) -> None:
        source = "\n".join([
            "float4 MainPS(const precise float4 uv : TEXCOORD0) : SV_Target",
            "{",
            "    precise float a = uv.x * uv.y;",
            "    const precise float b = a + uv.z;",
            "    return b.xxxx;",
            "}",
        ])
        result = extract_function(source, 3, 4, "ComputeValues")
        self.assertEqual(result["info"]["inputs"], ["uv"])
        self.assertEqual(result["info"]["returns"], "b")
        self.assertIn("precise float ComputeValues(const precise float4 uv)", result["source"])
        self.assertIn("const precise float b = ComputeValues(uv);", result["source"])
        self.assertIn("    precise float a = uv.x * uv.y;", result["source"])

    def test_multiple_const_outputs_keep_initialized_declarations_and_avoid_names(self) -> None:
        source = "\n".join([
            "static const float replaylab_out_a = 2.0f;",
            "static const float replaylab_value_a = 3.0f;",
            "float4 MainPS(float4 uv : TEXCOORD0) : SV_Target",
            "{",
            "    const precise float a = uv.x * replaylab_out_a, b = a + uv.y;",
            "    return float4(a, b, replaylab_value_a, 1.0f);",
            "}",
        ])
        result = extract_function(source, 5, 5, "ComputeValues")
        self.assertEqual(result["info"]["outParams"], ["a", "b"])
        text = result["source"]
        self.assertIn("out precise float replaylab_out_a_1", text)
        self.assertIn("precise float replaylab_value_a_1;", text)
        self.assertIn("replaylab_out_a_1 = replaylab_local_a", text)
        self.assertIn("const precise float a = replaylab_value_a_1;", text)
        self.assertIn("const precise float b = replaylab_value_b;", text)
        self.assertIn(
            "const precise float replaylab_local_a = uv.x * replaylab_out_a, replaylab_local_b = replaylab_local_a + uv.y;",
            text,
        )

    def test_static_storage_and_qualified_arrays_crossing_boundary_are_refused(self) -> None:
        for declaration, reason in [
            ("static float a = 1.0f;", "static local"),
            ("const float a[2] = {1.0f, 2.0f};", "local array"),
            ("const float b = 1.0f, a[2] = {1.0f, 2.0f};", "local array"),
        ]:
            source = "float4 MainPS() : SV_Target\n{\n    " + declaration + "\n    return a;\n}\n"
            with self.subTest(declaration=declaration), self.assertRaisesRegex(RestructureError, reason):
                extract_function(source, 3, 3, "ComputeValues")

    def test_unsafe_ranges_are_refused_with_reasons(self) -> None:
        with self.assertRaisesRegex(RestructureError, "complete branch, loop or statement"):
            extract_function(SOURCE, 19, 20, "Bad")
        with self.assertRaisesRegex(RestructureError, "complete branch, loop or statement"):
            extract_function(SOURCE, 20, 21, "Bad")
        with self.assertRaisesRegex(RestructureError, "complete branch, loop or statement"):
            extract_function(SOURCE, 19, 19, "Bad")
        with self.assertRaisesRegex(RestructureError, "inside one function body"):
            extract_function(SOURCE, 12, 14, "Bad")
        with self.assertRaisesRegex(RestructureError, "contains 'return'"):
            extract_function(SOURCE, 29, 30, "Bad")
        with self.assertRaisesRegex(RestructureError, "already exists"):
            extract_function(SOURCE, 15, 17, "frag_main")

    def test_sibling_branch_locals_with_same_name_do_not_escape_helper_scope(self) -> None:
        source = "\n".join([
            "float Main(float x)", "{", "    float result = x;",
            "    if (x > 0)", "    {", "        float value = 2;",
            "        result += value;", "    }", "    if (x < 0)", "    {",
            "        float value = 3;", "        result += value;", "    }",
            "    return result;", "}", "",
        ])
        extracted = extract_function(source, 4, 8, "AccumulatePositive")
        self.assertEqual(extracted["info"]["inputs"], ["x"])
        self.assertEqual(extracted["info"]["inouts"], ["result"])
        self.assertIsNone(extracted["info"]["returns"])
        self.assertNotIn("return value;", extracted["source"])
        self.assertIn("AccumulatePositive(x, result);", extracted["source"])
        self.assertEqual(extracted["source"].count("float value ="), 2)

    def test_loop_initializer_does_not_capture_shadowed_outer_local(self) -> None:
        source = "\n".join([
            "float Main(float x)", "{", "    float i = 10;",
            "    for (int i = 0; i < 2; ++i)", "    {",
            "        x += float(i);", "    }", "    return x + i;", "}", "",
        ])
        extracted = extract_function(source, 4, 7, "AccumulateLoop")
        self.assertEqual(extracted["info"]["inputs"], [])
        self.assertEqual(extracted["info"]["inouts"], ["x"])
        self.assertNotIn("inout float i", extracted["source"])
        self.assertIn("AccumulateLoop(x);", extracted["source"])
        self.assertIn("return x + i;", extracted["source"])

    def test_do_while_tail_and_outer_loop_continue_need_whole_owner(self) -> None:
        source = "\n".join([
            "float Main(float x)", "{", "    do", "    {",
            "        x += 1;", "    } while (x < 2);",
            "    return x;", "}", "",
        ])
        with self.assertRaisesRegex(RestructureError, "include 3-6 \\(do\\)"):
            extract_function(source, 3, 5, "PartialDo")
        extracted = extract_function(source, 3, 6, "CompleteDo")
        self.assertIn("CompleteDo(x);", extracted["source"])
        nested = "\n".join([
            "void Main()", "{", "    for (int i = 0; i < 2; ++i)", "    {",
            "        switch (i)", "        {", "            case 1: continue;",
            "            default: break;", "        }", "    }", "}", "",
        ])
        with self.assertRaisesRegex(RestructureError, "contains 'continue'"):
            extract_function(nested, 5, 9, "MoveSwitch")
        case_label = "\n".join([
            "int Main(int x)", "{", "    switch (x)", "    {",
            "        case 1: x += 1;", "        default: x += 2;",
            "    }", "    return x;", "}", "",
        ])
        with self.assertRaisesRegex(RestructureError, "complete branch, loop or statement"):
            extract_function(case_label, 5, 5, "MoveCase")

    def test_braceless_loop_with_owned_break_can_move_as_one_statement(self) -> None:
        source = "\n".join([
            "float Main(float x)", "{",
            "    for (int i = 0; i < 2; ++i)",
            "        if (x > 0) break;",
            "    return x;", "}", "",
        ])
        extracted = extract_function(source, 3, 4, "BoundedLoop")
        self.assertIn("if (x > 0) break;", extracted["source"])
        self.assertIn("BoundedLoop(x);", extracted["source"])

    def test_operations_replay_in_order_and_report_structure(self) -> None:
        first = extract_function(SOURCE, 15, 17, "ApplyExposure")
        lines = first["source"].split("\r\n")
        start = next(i for i, line in enumerate(lines, 1) if "float _15 = dot" in line)
        end = start + 4  # if-block closes four lines later
        text, receipts = apply_operations(SOURCE, [
            {"operation": "extract", "startLine": 15, "endLine": 17, "name": "ApplyExposure"},
            {"operation": "extract", "startLine": start, "endLine": end, "name": "NormalizeLuminance"},
        ])
        self.assertEqual([r["name"] for r in receipts], ["ApplyExposure", "NormalizeLuminance"])
        report = structure_report(text, entry_point="MainPS")
        names = [f["name"] for f in report["functions"]]
        self.assertEqual(names, ["ApplyExposure", "NormalizeLuminance", "frag_main", "MainPS"])
        flagged = {f["name"]: f["flags"] for f in report["functions"]}
        self.assertTrue(any("anonymous-locals" in flag for flag in flagged["ApplyExposure"]))

    def test_source_bound_selectors_replay_extract_rewrite_and_remove(self) -> None:
        canonical = """cbuffer MaterialCB : register(b0) { float4 _2 : packoffset(c0); };
float4 MainPS() : SV_Target0
{
    float4 _7 = _2 * 0.5f;
    float4 _8 = _7 * 2.0f;
    return _8;
}
"""
        requested = {
            "MaterialCB._2": "materialTint",
            "MainPS::_7": "sampledTint",
            "MainPS::_8": "litColor",
        }
        renames = validate_renames(canonical, requested)
        renamed = apply_renames(canonical, renames)

        # Upgrade the historical line operation from the complete renamed
        # source. Its stored anchor remains in the canonical namespace.
        operations = anchor_extractions(
            renamed,
            [{"operation": "extract", "startLine": 4, "endLine": 4,
              "name": "ReadMaterial"}],
            renames,
        )
        self.assertIn("float4 _7 = _2 * 0.5f;", operations[0]["anchor"]["before"])
        extracted, receipts = apply_operations(renamed, operations, renames=renames)
        self.assertIn("float4 ReadMaterial()", extracted)
        self.assertIn("float4 sampledTint = materialTint * 0.5f;", extracted)
        self.assertIn("float4 sampledTint = ReadMaterial();", extracted)
        self.assertEqual(receipts[0]["returns"], "sampledTint")

        # Prepare a subsequent rewrite against aligned complete current and
        # canonical sources. The local selector never runs against the one-line
        # replacement in isolation.
        canonical_extracted, unused = apply_operations(canonical, operations, renames={})
        return_line = next(
            index for index, line in enumerate(extracted.splitlines(), 1)
            if line.strip() == "return litColor;"
        )
        rewrite = prepare_rewrite(
            extracted,
            [{"startLine": return_line, "endLine": return_line,
              "replacement": "    return litColor * 1.0f;"}],
            renames=renames,
            entry_point="MainPS",
            name="DocumentOutput",
            canonical_source=canonical_extracted,
        )
        self.assertIn("return _8 * 1.0f;", rewrite["edits"][0]["after"])
        combined = operations + [rewrite]
        replayed, receipts = apply_operations(renamed, combined, renames=renames)
        self.assertIn("return litColor * 1.0f;", replayed)
        self.assertEqual([item["operation"] for item in receipts], ["extract", "rewrite"])

        # Removing the rewrite deterministically restores the extracted source;
        # removing the extraction restores the fully renamed baseline.
        without_rewrite, unused = apply_operations(renamed, operations, renames=renames)
        self.assertEqual(without_rewrite, extracted)
        without_structure, unused = apply_operations(renamed, [], renames=renames)
        self.assertEqual(without_structure, renamed)
        self.assertTrue(rename_only_equivalent(canonical, renamed, renames)["equal"])

    def test_whole_extracted_helper_rewrite_uses_aligned_complete_sources(self) -> None:
        canonical = """cbuffer Constants : register(b0) { float Scale : packoffset(c0.x); };
float4 MainPS(float4 input : TEXCOORD0) : SV_Target
{
    float _first = input.x * Scale;
    float _second = input.y + Scale;
    float _tail = _first + _second;
    return _tail.xxxx;
}
"""
        renames = validate_renames(
            canonical, {"_first": "coverage", "_second": "weightedNormal"}
        )
        renamed = apply_renames(canonical, renames)
        extraction = anchor_extractions(
            renamed,
            [{"operation": "extract", "startLine": 4, "endLine": 5,
              "name": "EvaluateCoverage"}],
            renames,
        )
        current, unused = apply_operations(renamed, extraction, renames=renames)
        canonical_current, unused = apply_operations(canonical, extraction, renames={})
        helper = next(row for row in _function_spans(current)
                      if row["name"] == "EvaluateCoverage")
        replacement = "\n".join(
            current.splitlines()[helper["startLine"] - 1:helper["endLine"]]
        ).replace("input.x * Scale", "input.x * (Scale)")
        rewrite = prepare_rewrite(
            current,
            [{"startLine": helper["startLine"], "endLine": helper["endLine"],
              "replacement": replacement}],
            renames=renames,
            entry_point="MainPS",
            name="ClarifyCoverage",
            canonical_source=canonical_current,
        )
        replayed, receipts = apply_operations(
            renamed, extraction + [rewrite], renames=renames
        )
        self.assertIn("float replaylab_local_coverage = input.x * (Scale);", replayed)
        self.assertIn("replaylab_out_coverage = replaylab_local_coverage;", replayed)
        self.assertIn("EvaluateCoverage(input, coverage, weightedNormal);", replayed)
        self.assertEqual([item["operation"] for item in receipts], ["extract", "rewrite"])

    @unittest.skipUnless(os.name == "nt", "Native HLSL equivalence requires Windows")
    def test_real_d3dcompile_extractions_preserve_executable_instructions(self) -> None:
        from src.shader_workspace.adapters.d3dcompiler_adapter import _compile_bytes
        from src.shader_workspace.binary_formats import fingerprint_shader_executable

        dll = Path(r"C:\Program Files (x86)\Windows Kits\10\Redist\D3D\x64\d3dcompiler_47.dll")
        if not dll.is_file() or dll.is_symlink():
            self.skipTest("Windows SDK x64 d3dcompiler_47.dll is not installed")
        # Dynamic shader inputs keep these calculations in the executable. A
        # fake compile receipt cannot catch a missing crossing local, a duplicate
        # out parameter declaration, or changes to precise arithmetic.
        cases = {
            "ordinary_multiple_outputs": (SOURCE, 15, 16),
            "nested_conditional_multiple_outputs": (
                NESTED_OUTPUT_SOURCE, 3, len(NESTED_OUTPUT_SOURCE.splitlines()) - 2),
            "precise_const_single_output": ("\n".join([
                "float4 MainPS(const precise float4 uv : TEXCOORD0) : SV_Target",
                "{",
                "    precise float a = uv.x * uv.y;",
                "    const precise float b = a + uv.z;",
                "    return b.xxxx;",
                "}",
            ]), 3, 4),
            "precise_const_multiple_outputs": ("\n".join([
                "static const float replaylab_out_a = 2.0f;",
                "static const float replaylab_value_a = 3.0f;",
                "float4 MainPS(float4 uv : TEXCOORD0) : SV_Target",
                "{",
                "    const precise float a = uv.x * replaylab_out_a, b = a + uv.y;",
                "    return float4(a, b, replaylab_value_a, 1.0f);",
                "}",
            ]), 5, 5),
            "precise_inout_and_const_input": ("\n".join([
                "float4 MainPS(float4 uv : TEXCOORD0) : SV_Target",
                "{",
                "    const precise float scale = uv.w + 1.0f;",
                "    precise float a = uv.x * uv.y;",
                "    a += scale * uv.z;",
                "    return a.xxxx;",
                "}",
            ]), 5, 5),
            "matrix_qualifier": ("\n".join([
                "float4 MainPS(float4 uv : TEXCOORD0) : SV_Target",
                "{",
                "    row_major float2x2 transform = float2x2(uv.xy, uv.zw);",
                "    return float4(mul(transform, uv.xy), 0.0f, 1.0f);",
                "}",
            ]), 3, 3),
        }
        for case, (source, start, end) in cases.items():
            with self.subTest(case=case):
                extracted = extract_function(source, start, end, "ComputeValues")["source"]
                fingerprints = []
                for label, text in (("original", source), ("extracted", extracted)):
                    status, compiled, diagnostics = _compile_bytes(dll, text.encode("utf-8"), case + ".hlsl", "MainPS", "ps_5_0")
                    self.assertGreaterEqual(status, 0, label + ": " + diagnostics.decode("utf-8", errors="replace") + "\n" + text)
                    fingerprint = fingerprint_shader_executable(compiled, case)
                    self.assertIsNotNone(fingerprint)
                    fingerprints.append(fingerprint.code_sha256)
                self.assertEqual(fingerprints[0], fingerprints[1], case + " changed executable instructions")


if __name__ == "__main__":
    unittest.main()
