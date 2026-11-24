from __future__ import annotations

import json
import unittest

from src.shader_workspace.outline import function_slice, line_slice, outline, paged_outline, symbol_slices


def test_symbol_slices_find_local_declaration_and_uses_without_comment_string_hits():
    source = '// _8737\n/* _8737\n multiline */\nconst char* text = "_8737";\nfloat F(float value) {\n  float _8737 = value * value;\n  return _8737;\n}'
    result = symbol_slices(source, ['_8737', 'missing'])
    assert result['symbols'][0]['occurrences'] == 2
    assert [r['line'] for r in result['symbols'][0]['locations']] == [6, 7]
    assert result['symbols'][1]['occurrences'] == 0
    assert 'float _8737 = value * value' in result['text']
    assert result['truncated'] is False


def test_symbol_slices_bound_output_and_do_not_starve_later_names():
    names = ['localValue' + str(i) for i in range(16)]
    source = '\n'.join('\n'.join('float %s = %s;' % (name, ' + '.join(['1.0'] * 500))
                                  for name in names) for _ in range(20))
    result = symbol_slices(source, names)
    assert len(result['text']) <= 24000
    assert len(result['text'].splitlines()) <= 96
    assert result['truncated']
    assert all(item['occurrences'] == 20 and len(item['locations']) == 12 for item in result['symbols'])
    assert all(name in result['text'] for name in names)


def test_symbol_slices_reject_patterns_and_unbounded_or_invalid_identifiers():
    import pytest
    for symbols in ([], '_8737', ['.*'], ['a'] * 17, ['a' * 257], [None], [True]):
        with pytest.raises(ValueError):
            symbol_slices('float x = 1;', symbols)


def test_anonymous_examples_have_exact_lexical_scope_and_include_unused_globals():
    source = ('float _9900;\n// _9901\nfloat First(float _123)\n{\n'
              '  float _456 = _123; // _9902\n  return _456;\n}\n'
              'float Second(float value) { float _456 = value; return _456; }\n')
    result = outline(source)['anonymity']
    assert result['anonymousLocalExamples'] == [
        {'name': '_123', 'line': 3, 'function': 'First', 'renameSelector': 'First::_123'},
        {'name': '_456', 'line': 5, 'function': 'First', 'renameSelector': 'First::_456'},
        {'name': '_456', 'line': 8, 'function': 'Second', 'renameSelector': 'Second::_456'},
        {'name': '_9900', 'line': 1, 'function': '', 'renameSelector': '_9900'},
    ]
    assert not result['anonymousLocalExamplesTruncated']


def test_anonymous_examples_are_bounded_and_expose_later_work_after_names_are_resolved():
    source = 'float MainPS()\n{\n' + ''.join('float _%s = 1;\n' % (1000+i) for i in range(80)) + 'return 1;\n}'
    first = outline(source)['anonymity']
    assert len(first['anonymousLocalExamples']) == 64 and first['anonymousLocalExamplesTruncated']
    import re
    named = re.sub(r'\b_10(?:[0-5][0-9]|6[0-3])\b', 'resolvedValue', source)
    remaining = outline(named)['anonymity']
    assert remaining['anonymousLocalExamples'][0]['name'] == '_1064'
    assert len(remaining['anonymousLocalExamples']) == 16 and not remaining['anonymousLocalExamplesTruncated']


def test_anonymous_examples_preserve_same_line_function_scope():
    examples = outline('float A() { float _123 = 1; return _123; } float B() { float _456 = 2; return _456; }')['anonymity']['anonymousLocalExamples']
    assert [(item['name'], item['function']) for item in examples] == [('_123', 'A'), ('_456', 'B')]


def test_paged_outline_exposes_numbered_generic_locals_without_cbuffer_false_positive():
    source = (
        'cbuffer Globals { float mainPSinnerValue139; };\n'
        'float4 MainPS(float inputValue : TEXCOORD0) : SV_Target {\n'
        '  float mainPSinnerValue140 = inputValue + mainPSinnerValue139;\n'
        '  return mainPSinnerValue140;\n}\n'
    )
    summary = paged_outline(source, entry_point='MainPS')
    assert summary['anonymity']['anonymousLocals'] == 1
    assert summary['anonymity']['anonymousLocalExamples'] == [
        {'name': 'mainPSinnerValue140', 'line': 3, 'function': 'MainPS',
         'renameSelector': 'MainPS::mainPSinnerValue140'}
    ]
    full = outline(source, entry_point='MainPS')
    assert full['anonymity']['anonymousLocals'] == 1
    assert full['anonymity']['anonymousLocalExamples'][0]['name'] == 'mainPSinnerValue140'


def test_public_outline_pages_every_large_declaration_selector(tmp_path, monkeypatch):
    import json
    from src import server

    source = "\n".join(
        "Texture2D _%d : register(t%d);" % (index, index)
        for index in range(600)
    ) + "\nfloat4 MainPS():SV_Target{return 0;}"
    path = tmp_path / "shader.hlsl"
    path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(server, "_workspace_file_context", lambda unused: (tmp_path, {}))
    monkeypatch.setattr(server, "_workspace_file_target", lambda root, relative: root / relative)

    summary_text = server.get_shader_workspace_outline(
        str(tmp_path), relative_path="shader.hlsl"
    )
    summary = json.loads(summary_text)
    assert summary["requestedSection"] == "summary"
    assert summary["outlineSections"]["declarations"]["total"] == 600
    assert "declarations" not in summary
    assert len(summary_text.encode("utf-8")) < 40_000

    selectors = []
    offset = 0
    while True:
        payload_text = server.get_shader_workspace_outline(
            str(tmp_path), relative_path="shader.hlsl",
            section="declarations", offset=offset, limit=64,
        )
        payload = json.loads(payload_text)
        page = payload["page"]
        selectors.extend(item["renameSelector"] for item in page["items"])
        assert len(page["items"]) <= 64
        assert len(payload_text.encode("utf-8")) < 24 * 1024
        assert "declarations" not in payload
        assert "functions" not in payload
        assert "symbolUse" not in payload
        if not page["hasMore"]:
            assert page["nextOffset"] is None
            break
        assert page["nextOffset"] > offset
        offset = page["nextOffset"]
    assert selectors == ["resource:_%d" % index for index in range(600)]
    assert len(set(selectors)) == 600

    clamped = json.loads(server.get_shader_workspace_outline(
        str(tmp_path), relative_path="shader.hlsl",
        section="declarations", limit=65,
    ))
    assert clamped["page"]["limit"] == 64
    assert clamped["page"]["returned"] == 64
    assert clamped["page"]["nextOffset"] == 64


def test_oversized_outline_limit_clamps_without_losing_pagination():
    source = "\n".join("Texture2D tex%d : register(t%d);" % (i, i)
                       for i in range(130))
    first = paged_outline(source, section="declarations", limit=100)
    page = first["page"]
    assert page["limit"] == 64
    assert page["returned"] == 64
    assert page["total"] == 130
    assert page["hasMore"] is True
    assert page["nextOffset"] == 64
    names = [item["renameSelector"] for item in page["items"]]
    while page["hasMore"]:
        page = paged_outline(source, section="declarations", limit=100,
                             offset=page["nextOffset"])["page"]
        names.extend(item["renameSelector"] for item in page["items"])
    assert names == ["resource:tex%d" % i for i in range(130)]
    assert page["nextOffset"] is None


def test_outline_still_rejects_invalid_limit_and_offset():
    import pytest
    for limit in (0, -1, True, 2.0, "100", None):
        with pytest.raises(ValueError, match="positive integer"):
            paged_outline("", limit=limit)
    for offset in (-1, True, 2.0, "0", None):
        with pytest.raises(ValueError, match="non-negative integer"):
            paged_outline("", offset=offset, limit=100)


def test_public_outline_symbol_scope_is_exact_source_bound(tmp_path, monkeypatch):
    import json
    import hashlib
    from src import server

    source = ("float exposure;\n"
              "float Main(float input) {\n"
              "  float _7 = input + exposure;\n"
              "  return _7;\n"
              "}\n")
    path = tmp_path / "shader.hlsl"
    path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(server, "_workspace_file_context", lambda unused: (tmp_path, {}))
    monkeypatch.setattr(server, "_workspace_file_target", lambda root, relative: root / relative)

    result = json.loads(server.get_shader_workspace_outline(
        str(tmp_path), relative_path="shader.hlsl", selector="Main::_7", limit=1,
    ))
    assert result["sourceSha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["identityScope"] == "exact-source-only"
    assert result["declaration"]["line"] == 3
    assert result["lexicalScope"]["function"] == "Main"
    assert result["references"]["boundCount"] == 1
    assert result["dependencies"]["status"] == "unknown"
    assert path.read_text(encoding="utf-8") == source

    mixed = json.loads(server.get_shader_workspace_outline(
        str(tmp_path), relative_path="shader.hlsl",
        selector="Main::_7", region="3-3",
    ))
    assert "choose only one" in mixed["error"]


def test_public_outline_symbol_table_pages_globals_and_one_function(tmp_path, monkeypatch):
    import hashlib
    from src import server

    source = ("cbuffer MaterialCB { float exposure; };\n"
              "float Main(float inputValue) {\n"
              "  float lit = inputValue * exposure;\n"
              "  return lit;\n"
              "}\n")
    path = tmp_path / "shader.hlsl"
    path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(server, "_workspace_file_context", lambda unused: (tmp_path, {}))
    monkeypatch.setattr(server, "_workspace_file_target", lambda root, relative: root / relative)

    global_view = json.loads(server.get_shader_workspace_outline(
        str(tmp_path), relative_path="shader.hlsl", section="symbol-table", limit=2,
    ))
    assert global_view["view"] == "global-declarations"
    assert global_view["sourceSha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert global_view["nextOffset"] is not None
    assert source not in json.dumps(global_view)

    function_view = json.loads(server.get_shader_workspace_outline(
        str(tmp_path), relative_path="shader.hlsl", section="symbol-table",
        function="Main", limit=32,
    ))
    assert function_view["view"] == "function-symbols"
    assert function_view["runtimeLivenessKnown"] is False
    assert any(row["name"] == "lit" and row["candidateDependencies"]["inputSymbols"]
               for row in function_view["rows"])
    assert path.read_text(encoding="utf-8") == source

    invalid = json.loads(server.get_shader_workspace_outline(
        str(tmp_path), relative_path="shader.hlsl", section="symbol-table", lines="1-2",
    ))
    assert "may be combined only with function" in invalid["error"]


def test_paged_sections_do_not_build_full_outline_and_inventory_once(monkeypatch):
    from src.shader_workspace import outline as outline_module
    from src.shader_workspace import quality

    source = "\n".join(
        "Texture2D _%d : register(t%d);" % (index, index)
        for index in range(128)
    ) + "\nfloat4 MainPS():SV_Target{return 0;}"
    real_inventory = quality.declared_binding_inventory
    calls = {"inventory": 0}

    def counted_inventory(text):
        calls["inventory"] += 1
        return real_inventory(text)

    monkeypatch.setattr(
        outline_module, "outline",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("paged section must not construct the full outline")
        ),
    )
    monkeypatch.setattr(quality, "declared_binding_inventory", counted_inventory)
    outline_module._clear_outline_index_cache()
    pages = [
        paged_outline(source, section="declarations", offset=offset, limit=32)
        for offset in (0, 32, 64)
    ]
    assert calls["inventory"] == 1
    assert all(page["page"]["returned"] > 0 for page in pages)
    assert all(
        len(json.dumps(page, ensure_ascii=True).encode("utf-8")) < 24 * 1024
        for page in pages
    )


def test_worst_function_page_is_projected_and_under_vendor_cap():
    members = ["__replaylab_s0_b0_c%d_x_f32" % index for index in range(64)]
    source = (
        "cbuffer Material : register(b0) {\n"
        + "\n".join("float %s;" % name for name in members)
        + "\n};\nfloat4 MainPS() : SV_Target {\n"
        + "\n".join(
            "float _%d = %s;" % (1000 + index, " + ".join(members))
            for index in range(12)
        )
        + "\nreturn 0;\n}\n"
    )
    page = paged_outline(
        source, entry_point="MainPS", section="functions", limit=64
    )
    encoded = json.dumps(page, ensure_ascii=True).encode("utf-8")
    assert len(encoded) < 24 * 1024
    assert "functions" not in page
    assert "entry" not in page
    row = page["page"]["items"][0]
    assert row["name"] == "MainPS"
    assert row["qualityCounts"]["physicalPlaceholderCount"] == 64
    assert "physicalPlaceholderReferences" not in row
    assert "quality" not in row


def test_one_pathological_declaration_cannot_bypass_page_budget():
    source = (
        "Texture2D<" + ("float4_" * 9000)
        + "> _5019 : register(t0);\n"
        + "float4 MainPS() : SV_Target { return 0; }\n"
    )
    page = paged_outline(source, section="declarations", limit=32)
    encoded = json.dumps(page, ensure_ascii=True, default=str).encode("utf-8")
    assert len(encoded) < 24 * 1024
    assert page["page"]["returned"] == 1
    row = page["page"]["items"][0]
    assert row["_projection"]["complete"] is False
    assert row["_projection"]["originalByteSize"] > 50_000
    assert row["_projection"]["originalSha256"]
    assert row["renameSelector"] == "resource:_5019"


def test_outline_exposes_whole_function_quality_and_physical_coverage_separately():
    source = ('cbuffer C { float _900; float __replaylab_s0_b0_c1_x_f32; };\n'
              'float Main(float x)\n{\n' +
              ''.join('  float _%d = x + _900 + __replaylab_s0_b0_c1_x_f32;\n' % (1000+i) for i in range(170)) +
              '  return x;\n}')
    result = outline(source, entry_point='Main')
    function = result['entry']
    assert function['quality']['statementCount'] == 171
    assert 'requiredSectionCommentCount' not in function['quality']
    assert 'sectionCommentsDistributed' not in function['quality']
    assert 'planning' not in function
    assert function['quality']['anonymousIdentifierCount'] == 170
    physical = {row['name']: row for row in function['physicalPlaceholderReferences']}
    assert physical['_900']['referenceCount'] == 170
    assert physical['_900']['locationsTruncated']
    assert len(physical['_900']['locations']) == 12
    assert physical['_900']['renameSelector'] == 'C._900'
    assert all(row['aliasState'] == 'not-inspected' and not row['semanticBindingProven'] for row in physical.values())
    assert 'sizeClass' not in result['totals']
    assert 'qualityPolicy' not in result
    assert "region" in result['availableCapabilities']['detailSlices']


def test_outline_quality_keeps_overloaded_functions_separate():
    source = 'float F(float x) { float _123 = x; return _123; }\nfloat F(float2 x) { return x.x; }'
    result = outline(source)
    assert [f['quality']['anonymousIdentifierCount'] for f in result['functions']] == [1, 0]

SOURCE = """cbuffer _41_43 : register(b0, space0)
{
    float __replaylab_s0_b0_c0_x_f32 : packoffset(c0.x);
    float __replaylab_s0_b0_c0_y_f32 : packoffset(c0.y);
    uint __replaylab_s0_b0_c2_z_i32 : packoffset(c2.z);
};

cbuffer View : register(b1, space0)
{
    float4 View_ViewSize : packoffset(c0);
    float ExposureScale : packoffset(c1.x);
};

Texture2D<float4> BaseColor : register(t0, space0);
SamplerState BaseColorSampler : register(s0, space0);
Texture2D<float4> Normal : register(t1, space0);

struct SPIRV_Cross_Input
{
    float4 gl_FragCoord : SV_Position;
    float2 in_var_TEXCOORD0 : TEXCOORD0;
};

struct SPIRV_Cross_Output
{
    float4 out_var_SV_Target0 : SV_Target0;
};

// helper doing the tone map
float3 ToneMap(float3 c)
{
    float _1001 = c.x * ExposureScale;
    return float3(_1001, c.y, c.z);
}

void MainPS_inner(float2 uv)
{
    float4 _2001 = BaseColor.Sample(BaseColorSampler, uv);
    float4 _2002 = Normal.Sample(BaseColorSampler, uv * View_ViewSize.xy);
    float3 _2003 = ToneMap(_2001.xyz + __replaylab_s0_b0_c0_x_f32);
}

SPIRV_Cross_Output MainPS(SPIRV_Cross_Input stage_input)
{
    MainPS_inner(stage_input.in_var_TEXCOORD0);
    SPIRV_Cross_Output stage_output;
    stage_output.out_var_SV_Target0 = float4(__replaylab_s0_b0_c0_y_f32, 0, 0, 1);
    return stage_output;
}
"""


class OutlineTests(unittest.TestCase):
    def test_outline_reports_declarations_functions_and_use_sites(self) -> None:
        result = outline(SOURCE, entry_point="MainPS")
        self.assertEqual(result["totals"]["functions"], 3)
        cbuffers = {item["name"]: item for item in result["declarations"]["cbuffers"]}
        self.assertEqual(cbuffers["_41_43"]["memberCount"], 3)
        self.assertEqual(cbuffers["_41_43"]["anonymousMembers"], 3)
        self.assertEqual(cbuffers["_41_43"]["renameSelector"], "cbuffer:_41_43")
        self.assertTrue(cbuffers["_41_43"]["generatedPhysicalName"])
        self.assertEqual(cbuffers["_41_43"]["neutralName"], "CBufferS0B0")
        self.assertEqual(
            [(row["renameSelector"], row["byteOffset"], row["byteSize"])
             for row in cbuffers["_41_43"]["members"]],
            [("_41_43.__replaylab_s0_b0_c0_x_f32", 0, 4),
             ("_41_43.__replaylab_s0_b0_c0_y_f32", 4, 4),
             ("_41_43.__replaylab_s0_b0_c2_z_i32", 40, 4)],
        )
        self.assertEqual(cbuffers["View"]["anonymousMembers"], 0)
        self.assertEqual([r["name"] for r in result["declarations"]["resources"]], ["BaseColor", "BaseColorSampler", "Normal"])
        entry = result["entry"]
        self.assertEqual(entry["name"], "MainPS")
        self.assertEqual(entry["calls"], ["MainPS_inner"])
        inner = next(f for f in result["functions"] if f["name"] == "MainPS_inner")
        self.assertEqual(inner["callers"], ["MainPS"])
        self.assertEqual(inner["textureSamples"], {"BaseColor": 1, "Normal": 1})
        self.assertGreaterEqual(inner["anonymousLocals"], 3)
        use = result["symbolUse"]
        self.assertEqual(use["ExposureScale"]["owner"], "View")
        self.assertEqual(use["ExposureScale"]["renameSelector"], "View.ExposureScale")
        self.assertEqual(use["ExposureScale"]["uses"], 1)
        self.assertEqual(use["__replaylab_s0_b0_c0_x_f32"]["uses"], 1)
        self.assertIn("__replaylab_s0_b0_c2_z_i32", result["anonymity"]["unusedDeclared"])
        self.assertEqual(result["anonymity"]["anonymousCBufferMembers"], 3)
        self.assertIn("region", result["availableCapabilities"]["detailSlices"])

    def test_slices_are_bounded_and_numbered(self) -> None:
        piece = function_slice(SOURCE, "ToneMap")
        self.assertTrue(piece["text"].splitlines()[0].strip().endswith("float3 ToneMap(float3 c)"))
        self.assertEqual(piece["endLine"] - piece["startLine"], 4)
        ranged = line_slice(SOURCE, 1, 3)
        self.assertEqual(ranged["text"].count("\n"), 2)
        self.assertTrue(ranged["text"].startswith("     1| cbuffer"))
        with self.assertRaises(KeyError):
            function_slice(SOURCE, "Nope")

    def test_physical_projection_uses_qualified_constant_buffer_binding(self) -> None:
        source = (
            "struct MaterialData { float _2; };\n"
            "ConstantBuffer<MaterialData> material : register(b1);\n"
            "cbuffer Flat : register(b2) { float _3; };\n"
            "float Main() { float _3 = 1.0; return material._2 + _3; }"
        )
        result = outline(source, entry_point="Main")
        rows = result["entry"]["physicalPlaceholderReferences"]
        self.assertEqual([row["name"] for row in rows], ["material._2"])
        self.assertEqual(rows[0]["owner"], "material")
        self.assertEqual(rows[0]["renameSelector"], "material._2")
        blocks = {item["name"]: item for item in result["declarations"]["cbuffers"]}
        self.assertTrue(blocks["material"]["typedBinding"])
        self.assertEqual(blocks["material"]["members"][0]["renameSelector"], "material._2")

    def test_outline_exposes_exact_selectors_for_unused_generated_resources(self) -> None:
        source = (
            "Texture2D _32; Texture2D _36; SamplerState s0 : register(s0);\n"
            "float4 MainPS() : SV_Target { return 0; }"
        )
        result = outline(source, entry_point="MainPS")
        resources = {item["name"]: item for item in result["declarations"]["resources"]}
        self.assertEqual(set(resources), {"_32", "_36", "s0"})
        self.assertEqual(resources["_32"]["renameSelector"], "resource:_32")
        self.assertEqual(resources["_36"]["renameSelector"], "resource:_36")
        self.assertEqual(resources["s0"]["resourceKind"], "sampler")
        self.assertTrue(all(item["generatedPhysicalName"] for item in resources.values()))
        self.assertEqual(len({item["neutralName"] for item in resources.values()}), 3)

    def test_outline_keeps_unresolved_typed_cbuffer_owner(self) -> None:
        source = (
            "ConstantBuffer<UnavailableLayout> cb1 : register(b1);\n"
            "float4 MainPS() : SV_Target { return 0; }"
        )
        block = outline(source, entry_point="MainPS")["declarations"]["cbuffers"][0]
        self.assertEqual(block["name"], "cb1")
        self.assertEqual(block["renameSelector"], "cbuffer:cb1")
        self.assertEqual(block["neutralName"], "CBufferS0B1")
        self.assertTrue(block["generatedPhysicalName"])
        self.assertEqual(block["members"], [])


if __name__ == "__main__":
    unittest.main()
