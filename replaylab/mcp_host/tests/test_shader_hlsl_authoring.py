from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from src.shader_workspace import ShaderKey, ShaderWorkspaceService, ToolchainCatalog, ToolchainRoutingError
from src.shader_workspace.adapters.spirv_cross_adapter import (
    derive_spirv_cross_arguments, preserve_vulkan_locations, preserve_readonly_array_buffers,
)
from src.shader_workspace.authoring import retain_function_comments
from src.shader_workspace.catalog import capture_identity
from src.shader_workspace.toolchain_config import generate_official_toolchain_configs


def test_sparse_locations_keep_builtins_and_output_indices():
    source = """struct SPIRV_Cross_Input
{
    float4 p : SV_Position;
    float2 uv : TEXCOORD0;
    uint objectIndex : TEXCOORD8;
};
struct SPIRV_Cross_Output
{
    float4 color : SV_Target3;
};
"""
    changed = preserve_vulkan_locations(source)
    assert "[[vk::location(8)]] uint objectIndex : TEXCOORD8;" in changed
    assert "[[vk::location(3)]] float4 color : SV_Target3;" in changed
    assert "    float4 p : SV_Position;" in changed
    assert "[[vk::location" not in changed.split("SV_Position")[0]
    with pytest.raises(ValueError, match="physical Vulkan"):
        derive_spirv_cross_arguments(source="a.spv", output="a.hlsl", mode="vulkan-hlsl",
                                    entry_point="main", stage="fragment", target_api="OpenGL")


def test_comments_follow_unique_matching_function_only():
    old = "// Compute lighting.\nfloat Lighting(float x) { return x; }\n// Overload.\nfloat Other(float x){return x;}\n// Other overload.\nfloat Other(float x,float y){return y;}"
    new = "float Lighting(float x) { return x; }\nfloat Other(float x) { return x; }"
    text, retained = retain_function_comments(old, new)
    assert retained == ["Lighting"]
    assert text.startswith("// Compute lighting.\n")
    assert "// Overload" not in text


def test_unsupported_storage_layout_is_not_reinterpreted():
    source = "ByteAddressBuffer rows : register(t4, space2);\nuint4 Get(uint a){return rows.Load4(a);}"
    reflection = {"types": {"x": {"members": [{"type": "vec4", "offset": 0, "array": [0], "array_stride": 16}]}},
                  "ssbos": [{"type": "x", "readonly": True, "set": 2, "binding": 4}]}
    changed = preserve_readonly_array_buffers(source, reflection)
    assert "StructuredBuffer<float4> rows : register(t4, space2);" in changed
    assert "rows.Load4" not in changed
    assert "address / 16u" in changed
    assert "address % 16u" in changed
    reflection["types"]["x"]["members"][0]["array_stride"] = 32
    assert preserve_readonly_array_buffers(source, reflection) == source


GLSL = """#version 450
layout(location=8) flat in uint item;
layout(location=0) in vec2 uv;
layout(location=3) out vec4 color;
layout(std430, set=2, binding=4) readonly buffer Rows { vec4 values[]; } rows;
layout(std140, set=1, binding=0) uniform Material { vec4 tint; float strength; } material;
// Scale the sampled object row.
vec4 EvaluateObject(vec2 coords) { return rows.values[item] * material.tint * material.strength; }
void main() { color = EvaluateObject(uv) + vec4(gl_FragCoord.w); }
"""


def _shape(reflection, typename):
    if typename not in reflection.get("types", {}):
        return typename
    return [{key: (_shape(reflection, value) if key == "type" else value)
             for key, value in member.items() if key != "name"}
            for member in reflection["types"][typename].get("members", [])]


def _physical(reflection, category):
    return sorted([{key: (_shape(reflection, value) if key == "type" else value)
                    for key, value in item.items() if key != "name"}
                   for item in reflection.get(category, [])],
                  key=lambda item: (item.get("set", 0), item.get("binding", 0), item.get("location", 0)))


def test_real_vulkan_current_source_migration_and_layout(tmp_path):
    tools = Path(__file__).resolve().parents[3] / "x64" / "Release" / "replaylab" / "tools"
    tools = tools.resolve()
    glslang, cross = tools / "glslang.exe", tools / "spirv-cross.exe"
    if not glslang.is_file() or not cross.is_file():
        pytest.skip("installed glslang/SPIRV-Cross integration tools unavailable")
    source = tmp_path / "original.frag"
    source.write_text(GLSL, encoding="utf-8")
    binary = tmp_path / "baseline.spv"
    subprocess.run([str(glslang), "-V", "--target-env", "vulkan1.1", "-S", "frag",
                    "-o", str(binary), str(source)], capture_output=True, check=True)
    raw = binary.read_bytes()
    key = ShaderKey(11, "fragment", "main", hashlib.sha256(raw).hexdigest())
    catalog = {"schema": "renderdoc-ultra-capture-symbols/v1", "schemaVersion": 1,
               "capturePath": "D:/captures/hlsl-authoring.rdc", "api": "GraphicsAPI.Vulkan",
               "shaders": [{**key.as_dict(), "rawPath": "baseline.spv", "rawSize": len(raw)}],
               "uses": [{**key.as_dict(), "eventId": 17}]}
    catalog["captureId"] = capture_identity(catalog, [key])
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    config, routes = tmp_path / "tools.json", tmp_path / "routes.json"
    generate_official_toolchain_configs(config, routes, glslang=glslang,
                                       spirv_cross=cross, spirv_target_env="vulkan1.1")
    planner = ToolchainCatalog(config, routes)
    assert planner.plan_automatic_recovery("spirv", "fragment", target_api="vulkan")["editableFormat"] == "hlsl"
    service = ShaderWorkspaceService(config, routes)
    original = tmp_path / "glsl-workspace"
    service.create(catalog_path, key.catalog_key, original, editable_format="glsl")
    service.decompile(original)
    working = original / "source" / "shader.glsl"
    # A saved edit that differs from captured source must survive conversion.
    current = GLSL.replace("material.strength; }", "material.strength * 0.75; }")
    working.write_text(current, encoding="utf-8")
    before = working.read_bytes()
    before_manifest = (original / "manifest.json").read_bytes()
    with pytest.raises(ToolchainRoutingError, match="source changed"):
        service.convert_to_hlsl(original, expected_source_sha256="0" * 64)
    result = service.convert_to_hlsl(original, expected_source_sha256=hashlib.sha256(before).hexdigest())
    assert working.read_bytes() == before
    assert (original / "manifest.json").read_bytes() == before_manifest
    hlsl = Path(result["sourcePath"]).read_text(encoding="utf-8")
    assert "0.75" in hlsl and "// Scale the sampled object row." in hlsl
    assert result["status"]["source"]["format"] == "hlsl"
    assert result["applied"] is False and result["semanticEquivalenceVerified"] is False
    assert Path(result["preservedSourcePath"]).read_bytes() == before
    compiled = result["compiled"]["candidateBlobPath"]
    original_reflection = json.loads(subprocess.check_output([str(cross), str(binary), "--reflect"]))
    new_reflection = json.loads(subprocess.check_output([str(cross), compiled, "--reflect"]))
    for category in ("inputs", "outputs", "ubos", "ssbos"):
        assert _physical(original_reflection, category) == _physical(new_reflection, category), category
    # Fragment coordinate w survives the SPIRV-Cross/HLSL convention bridge.
    compile_binding = result["status"]["routePlan"]["compile"][0]["operation"]
    registered = json.loads(config.read_text(encoding="utf-8"))
    assert "--hlsl-dx-position-w" in registered["tools"][compile_binding["toolId"]]["compile"]["args"]
