from unittest.mock import patch

from src.shader_workspace.inspection import _Program
from src.shader_workspace.quality import declared_binding_inventory, organization_quality


def test_many_anonymous_locals_use_linear_quality_reference_scan():
    source = (
        "float Compute(float inputValue)\n{\n"
        + "\n".join("    float _%d = inputValue + %d.0f;" % (1000 + i, i)
                    for i in range(300))
        + "\n    return _1299;\n}\n"
    )
    original_word = _Program.word
    calls = 0

    def counted_word(program, index):
        nonlocal calls
        calls += 1
        return original_word(program, index)

    with patch.object(_Program, "word", counted_word):
        metrics = organization_quality(source)["functions"][0]
    assert metrics["anonymousIdentifierCount"] == 300
    assert metrics["anonymousIdentifierLocations"][0] == {"name": "_1000", "line": 3}
    assert calls < 100_000  # 300 independent names must not scan the body 300 times

def test_custom_and_comma_locals_shadow_flat_cbuffer_without_regex_guesses():
    source = (
        "struct Scalar { float value; };\n"
        "cbuffer Material : register(b1) { float _2 : packoffset(c0.x); };\n"
        "float Compute() { Scalar _2; return _2.value; }\n"
        "float Other() { float value, _2 = 1.0; return _2; }"
    )
    quality = organization_quality(source)
    assert quality["physicalPlaceholderCount"] == 0
    assert all(item["anonymousIdentifierCount"] for item in quality["functions"])

def test_constant_buffer_array_and_owner_shadow_are_source_bound():
    source = (
        "struct MaterialData { float _2; };\n"
        "ConstantBuffer<MaterialData> materials[2] : register(b1);\n"
        "float Bound(uint i) { return materials[i]._2; }\n"
        "float Shadow(MaterialData materials) { return materials._2; }"
    )
    quality = organization_quality(source)
    assert quality["functions"][0]["physicalPlaceholders"] == ["materials._2"]
    assert quality["functions"][1]["physicalPlaceholders"] == []
    assert quality["functions"][1]["anonymousIdentifierCount"] == 1

def test_unbound_same_line_resources_get_distinct_stable_neutral_names():
    source = "Texture2D _32; Texture2D _36; float4 MainPS():SV_Target{return 0;}"
    rows = [item for item in declared_binding_inventory(source) if item["kind"] == "texture"]
    assert [item["renameSelector"] for item in rows] == ["resource:_32", "resource:_36"]
    assert len({item["neutralName"] for item in rows}) == 2
    assert all("AtLine1D" in item["neutralName"] for item in rows)

def test_inventory_covers_modern_hlsl_resource_classes_without_use_sites():
    source = (
        "RasterizerOrderedTexture2D<float4> _20 : register(u0);\n"
        "RasterizerOrderedBuffer<uint> _21 : register(u1);\n"
        "RWStructuredBuffer<float4> _22 : register(u2);\n"
        "ByteAddressBuffer _23 : register(t3);\n"
        "RaytracingAccelerationStructure _24 : register(t4);\n"
        "SamplerComparisonState s5 : register(s5);\n"
        "float4 MainPS():SV_Target{return 0;}"
    )
    rows = declared_binding_inventory(source)
    assert {item["renameSelector"] for item in rows} == {
        "resource:_20", "resource:_21", "resource:_22", "resource:_23",
        "resource:_24", "resource:s5",
    }
    assert next(item for item in rows if item["name"] == "_24")["kind"] == "resource"

def test_inventory_keeps_generated_empty_and_unresolved_typed_cbuffer_owners():
    source = (
        "cbuffer cb0 : register(b0) {};\n"
        "ConstantBuffer<UnavailableLayout> cb1 : register(b1);\n"
        "float4 MainPS():SV_Target{return 0;}"
    )
    rows = declared_binding_inventory(source)
    assert {item["renameSelector"] for item in rows} == {"cbuffer:cb0", "cbuffer:cb1"}
    assert all(item["generatedPhysicalName"] for item in rows)
