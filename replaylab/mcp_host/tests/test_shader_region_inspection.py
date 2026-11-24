from __future__ import annotations

import hashlib
import json

import pytest

from src.shader_workspace.inspection import _Program, inspect_region


def names(result, category):
    return [row['name'] for row in result['io'][category]]


def test_region_can_reuse_exact_program_but_rejects_different_source():
    source = "float Main(float x)\n{\n    float y = x * 2;\n    return y;\n}\n"
    program = _Program(source)
    expected = inspect_region(source, 3, 3, _extraction_only=True)
    assert inspect_region(source, 3, 3, _extraction_only=True,
                          _program=program) == expected
    with pytest.raises(ValueError, match="does not match"):
        inspect_region(source.replace("2", "3"), 3, 3, _extraction_only=True,
                       _program=program)


def test_region_keeps_precision_array_types_and_full_late_reference_counts():
    source = '\n'.join(['float Main(precise float value, float samples[4])', '{',
        '    precise float result = value * samples[0];'] +
        ['    result += value;' for _ in range(25)] + ['    return result;', '}'])
    result = inspect_region(source, 3, 3)
    assert result['boundary']['completeStatements']
    assert names(result, 'inputs') == ['value', 'samples']
    assert result['io']['inputs'][0]['qualifiers'] == ['precise']
    assert result['io']['inputs'][1]['array']
    output = result['io']['outputs'][0]
    assert output['name'] == 'result' and output['qualifiers'] == ['precise']
    assert output['references']['afterCount'] == 26
    assert output['references']['afterWriteCount'] == 25
    assert output['references']['lastLine'] == 29
    assert output['references']['countsComplete']
    assert names(result, 'externalReferences') == []  # f/u literal suffixes are not symbols


@pytest.mark.parametrize('span', [(4, 4), (5, 5), (3, 6)])
def test_for_header_is_not_a_complete_statement(span):
    source = '\n'.join(['void Main()', '{', '    for (', '        int i = 0;',
        '        i < 3;', '        ++i)', '    {', '        consume(i);', '    }', '}'])
    result = inspect_region(source, *span)
    assert not result['boundary']['completeStatements']
    assert {'startLine': 3, 'endLine': 9, 'kind': 'for'} in result['boundary']['suggestedRanges']


def test_if_else_and_do_while_are_indivisible_and_inner_statements_still_inspectable():
    source = '\n'.join(['float Main(float x)', '{', '    if (x > 0)', '    {', '        x += 1;',
        '    }', '    else if (x < 0)', '    {', '        x -= 1;', '    }',
        '    do', '    {', '        x *= 2;', '    } while (x < 10);', '    return x;', '}'])
    assert not inspect_region(source, 3, 6)['boundary']['completeStatements']
    assert inspect_region(source, 3, 10)['boundary']['completeStatements']
    assert inspect_region(source, 5, 5)['boundary']['completeStatements']
    assert not inspect_region(source, 11, 13)['boundary']['completeStatements']
    assert inspect_region(source, 11, 14)['boundary']['completeStatements']


def test_window_returns_complete_prefix_and_carries_unfinished_statement_forward():
    source = '\n'.join([
        'float Main(float x)', '{', '    x += 1;', '    if (x > 2)',
        '    {', '        x *= 2;', '    }', '    return x;', '}',
    ])
    boundary = inspect_region(source, 3, 5)['boundary']
    assert boundary['completeStatements'] is False
    assert boundary['completePrefixRange'] == {'startLine': 3, 'endLine': 3}
    assert boundary['carryForwardRange'] == {'startLine': 4, 'endLine': 7}
    assert 'nextReadStartLine' not in boundary
    continued = inspect_region(source, boundary['carryForwardRange']['startLine'], 7)['boundary']
    assert continued['completeStatements'] is True
    assert continued['carryForwardRange'] is None
    assert 'selectionPolicy' not in continued


def test_shadowed_local_does_not_escape_into_later_outer_same_named_value():
    source = '\n'.join(['float Main(float x)', '{', '    float value = x;', '    {',
        '        float value = 2;', '        x += value;', '    }', '    return value + x;', '}'])
    result = inspect_region(source, 5, 6)
    assert names(result, 'outputs') == []
    assert names(result, 'inouts') == ['x']
    assert result['io']['internalLocalCount'] == 1


def test_for_initializer_scope_does_not_capture_outer_same_named_variable():
    source = '\n'.join(['float Main()', '{', '    float i = 10;', '    [unroll] for (int i=0; i<2; ++i)',
        '    {', '        float value = float(i);', '    }', '    return i;', '}'])
    result = inspect_region(source, 4, 7)
    assert result['boundary']['completeStatements']
    assert names(result, 'outputs') == []
    assert names(result, 'inouts') == []


def test_declared_out_arguments_and_unknown_calls_are_not_misreported_readonly():
    source = '\n'.join(['void SetValue(float a, out float b) { b = a; }',
        'float Main(float x)', '{', '    float y = 0;', '    SetValue(x, y);',
        '    UnknownMacro(y);', '    return y;', '}'])
    known = inspect_region(source, 5, 5)
    assert names(known, 'inputs') == ['x']
    assert names(known, 'inouts') == ['y']
    assert known['io']['complete']
    unknown = inspect_region(source, 6, 6)
    assert names(unknown, 'inouts') == ['y']
    assert not unknown['io']['complete']
    assert any(d['code'] == 'region-unknown-call-effects' for d in unknown['diagnostics'])


def test_conditional_assignment_preserves_existing_value_as_inout():
    source = 'float Main(float x)\n{\n    if (x > 0)\n    {\n        x = 2;\n    }\n    return x;\n}'
    result = inspect_region(source, 3, 6)
    assert names(result, 'inouts') == ['x']


def test_continue_in_switch_still_escapes_its_outer_loop():
    source = '\n'.join(['void Main()', '{', '    for (int i=0; i<2; ++i)', '    {',
        '        switch (i)', '        {', '            case 1: continue;', '            default: break;', '        }', '    }', '}'])
    result = inspect_region(source, 5, 9)
    assert result['boundary']['completeStatements']
    assert any(d['code'] == 'region-control-transfer' and d['token'] == 'continue' for d in result['diagnostics'])
    assert not any(d['code'] == 'region-control-transfer' for d in inspect_region(source, 3, 10)['diagnostics'])


def test_glsl_region_preserves_qualifiers_and_reports_capability_without_extract_authority():
    source = '\n'.join(['#version 450', 'layout(location=0) out vec4 color;',
        'void main()', '{', '    highp vec3 normal = vec3(1);',
        '    precise vec3 scaled = normal * 2.0;', '    color = vec4(scaled, 1);', '}'])
    result = inspect_region(source, 6, 6)
    assert result['sourceFormat'] == 'glsl'
    assert result['io']['inputs'][0]['type'] == 'vec3'
    assert result['io']['inputs'][0]['qualifiers'] == ['highp']
    assert result['io']['outputs'][0]['qualifiers'] == ['precise']
    assert not result['organizationCapabilities']['structureExtraction']
    assert result['applied'] is False and result['semanticProof'] is False


def test_large_cohesive_interface_reports_live_values_without_a_review_order():
    parameters = ', '.join('float v%d' % i for i in range(15))
    source = 'float Main('+parameters+')\n{\n    float total = '+' + '.join('v%d'%i for i in range(15))+';\n    return total;\n}'
    result = inspect_region(source, 3, 3)
    assert result['io']['inputsCount'] == 15
    assert result['io']['outputsCount'] == 1
    assert result['io']['interfaceValueCount'] == 16
    assert 'largeInterface' not in result['io']
    assert 'interfaceReview' not in result['io']
    assert 'next' not in result


def test_macro_and_control_transfer_are_diagnostics_not_acceptance():
    source = '#define FACTOR 2\nfloat Main(float x)\n{\n    x *= FACTOR;\n    return x;\n}'
    result = inspect_region(source, 4, 5)
    assert {d['code'] for d in result['diagnostics']} >= {'region-macro-reference', 'region-control-transfer'}
    assert not result['io']['complete']


def test_preprocessor_variant_outside_selected_lines_keeps_io_incomplete():
    source = '\n'.join(['#ifdef MODE', 'float Main(float x)', '{',
        '    float value = x;', '    return value;', '}', '#endif'])
    result = inspect_region(source, 4, 4)
    assert result['boundary']['completeStatements']
    assert not result['io']['complete']
    assert any(d['code'] == 'region-conditional-compilation' for d in result['diagnostics'])
    source = '\n'.join(['float Main(float x)', '{', '#ifdef MODE',
        '    float value = x;', '#else', '    float value = x * 2;', '#endif',
        '    x = value;', '    return x;', '}'])
    result = inspect_region(source, 8, 8)
    assert not result['io']['complete']
    assert any(d['code'] == 'region-conditional-compilation' for d in result['diagnostics'])


def test_ambiguous_user_overload_cannot_borrow_intrinsic_purity():
    source = '\n'.join(['void abs(out float value) { value = 1; }',
        'void abs(out int value) { value = 1; }', 'float Main(float x)', '{',
        '    abs(x);', '    return x;', '}'])
    result = inspect_region(source, 5, 5)
    assert names(result, 'inouts') == ['x']
    assert not result['io']['complete']
    assert any(d['code'] == 'region-unknown-call-effects' for d in result['diagnostics'])


def test_physical_names_are_coverage_facts_without_fabricated_alias_status():
    source = 'cbuffer C { float _123; float __replaylab_s0_b0_c1_x_f32; };\nfloat Main()\n{\n    float value = _123 + __replaylab_s0_b0_c1_x_f32;\n    return value;\n}'
    result = inspect_region(source, 4, 4)
    rows = result['physicalPlaceholderReferences']
    assert [r['name'] for r in rows] == ['_123', '__replaylab_s0_b0_c1_x_f32']
    assert all(r['aliasState'] == 'not-inspected' and not r['semanticBindingProven'] for r in rows)


def test_host_load_helper_name_is_not_a_physical_naming_gap():
    source = 'float __replaylab_load_s0_t1(uint i) { return float(i); }\nfloat Main()\n{\n    float value = __replaylab_load_s0_t1(0u);\n    return value;\n}'
    assert inspect_region(source, 4, 4)['physicalPlaceholderReferences'] == []


def test_region_physical_projection_uses_binding_identity_and_ignores_local_shadow():
    source = '\n'.join([
        'struct MaterialData { float _2; };',
        'ConstantBuffer<MaterialData> material : register(b1);',
        'cbuffer Flat : register(b2) { float _3; };',
        'float Main()', '{',
        '    float _3 = 1.0;',
        '    float value = material._2 + _3;',
        '    return value;', '}',
    ])
    result = inspect_region(source, 7, 7)
    assert [row['name'] for row in result['physicalPlaceholderReferences']] == ['material._2']


def test_region_physical_projection_follows_constant_buffer_array_owner():
    source = '\n'.join([
        'struct MaterialData { float _2; };',
        'ConstantBuffer<MaterialData> materials[2] : register(b1);',
        'float Main(uint i)', '{',
        '    float value = materials[i]._2;',
        '    return value;', '}',
    ])
    result = inspect_region(source, 5, 5)
    assert [row['name'] for row in result['physicalPlaceholderReferences']] == ['materials._2']
    assert result['physicalPlaceholderReferences'][0]['referenceCount'] == 1


def test_local_template_type_is_not_an_external_value():
    source = 'void Main()\n{\n    vector<float, 3> value;\n    value.x = 1;\n}'
    result = inspect_region(source, 3, 4)
    assert result['io']['internalLocalCount'] == 1
    assert names(result, 'externalReferences') == []


@pytest.mark.parametrize('span', [(True, 1), (0, 1), (3, 1), (1, 999), (1, 401)])
def test_invalid_or_oversized_region_is_rejected(span):
    with pytest.raises(ValueError):
        inspect_region('\n'.join(['float x;'] * 450), *span)


def test_public_region_entry_is_readonly_source_bound_and_rejects_mixed_modes(tmp_path, monkeypatch):
    from src import server
    source = 'float Main(float x)\n{\n    float result = x * 2;\n    return result;\n}'
    path = tmp_path / 'proposal.glsl'
    path.write_text(source, encoding='utf-8')
    monkeypatch.setattr(server, '_workspace_file_context', lambda _: (tmp_path, {}))
    monkeypatch.setattr(server, '_workspace_file_target', lambda root, relative: root / relative)
    before = path.read_bytes()
    result = json.loads(server.get_shader_workspace_outline(str(tmp_path), relative_path='proposal.glsl', region='3-3'))
    assert result['sourceSha256'] == hashlib.sha256(before).hexdigest()
    assert result['io']['outputs'][0]['name'] == 'result'
    assert path.read_bytes() == before
    error = json.loads(server.get_shader_workspace_outline(str(tmp_path), relative_path='proposal.glsl', region='3-3', symbols=['x']))
    assert error.get('error')
