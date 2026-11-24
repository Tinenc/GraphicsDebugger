from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from src.shader_workspace.equivalence import apply_renames, declared_symbols, rename_only_equivalent
from src.shader_workspace.flow_graph import _function_records
from src.shader_workspace.outline import outline
from src.shader_workspace.source_language import source_language, validate_language_renames


GLSL = """#version 450
layout(location=0) in vec2 uv;
layout(location=0) out vec4 color;
layout(binding=0, std140) uniform Params { vec4 tint; } material;
vec4 shade(vec2 coord)
{
    vec4 result = material.tint * vec4(coord, 1.0, 1.0);
    return result;
}
void main()
{
    vec4 shaded = shade(uv);
    color = shaded;
}
"""


def test_glsl_without_version_uses_interface_syntax_and_hlsl_markers_win():
    assert source_language("layout(location=0) out vec4 color; void main() { color = vec4(1); }") == "glsl"
    assert source_language("in vec2 uv; out vec4 color; void main() { color = vec4(uv, 0, 1); }") == "glsl"
    assert source_language("Texture2D inputTexture : register(t0); float4 Main() : SV_Target { return 0; }") == "hlsl"


def test_glsl_interfaces_functions_and_outline_are_source_bound():
    assert [item["name"] for item in _function_records(GLSL)] == ["shade", "main"]
    symbols = declared_symbols(GLSL)
    assert symbols == {"resources": [], "cbuffers": ["material"], "inputs": ["uv"], "outputs": ["color"]}
    result = outline(GLSL, entry_point="main")
    assert result["sourceFormat"] == "glsl"
    assert result["declarations"]["entryOutputs"] == ["color"]
    assert result["organizationCapabilities"]["rename"] is True
    assert result["organizationCapabilities"]["structureExtraction"] is False


@pytest.mark.parametrize("old,new", [
    ("material", "properties"), ("tint", "baseColor"), ("uv", "texcoord"),
    ("color", "outputColor"), ("main", "renamedEntry"), ("result", "gl_Position"),
    ("result", "vec4"), ("result", "texture"), ("result", "foo__bar"),
])
def test_glsl_names_cannot_change_interfaces_or_become_reserved(old, new):
    with pytest.raises(ValueError):
        validate_language_renames(GLSL, {old: new}, "glsl", "main")


def test_glsl_local_and_helper_names_preserve_interface_tokens():
    names = {"shade": "computeTint", "coord": "sampleCoord", "result": "tintedColor", "shaded": "surfaceColor"}
    validate_language_renames(GLSL, names, "glsl", "main")
    proposal = apply_renames(GLSL, names)
    assert rename_only_equivalent(GLSL, proposal, names)["equal"] is True
    assert declared_symbols(proposal) == declared_symbols(GLSL)


def test_glsl_function_local_selector_can_separate_an_interface_shadow():
    source = """layout(location=0) in vec2 _8;
layout(location=0) out vec4 color;
void helper() { vec2 _8 = vec2(1.0); color = vec4(_8, 0.0, 1.0); }
void main() { helper(); }
"""
    names = {"helper::_8": "localUv"}
    validate_language_renames(source, names, "glsl", "main")
    proposal = apply_renames(source, names)
    assert "in vec2 _8;" in proposal
    assert "vec2 localUv = vec2(1.0)" in proposal
    assert "vec4(localUv, 0.0, 1.0)" in proposal
    assert rename_only_equivalent(source, proposal, names)["equal"] is True


def test_glsl_local_selector_can_use_a_helper_name_renamed_in_the_same_ledger():
    source = """layout(location=0) out vec4 color;
void helper() { vec4 _6 = vec4(1.0); color = _6; }
void main() { helper(); }
"""
    names = {"helper": "writeColor", "writeColor::_6": "resultColor"}
    validate_language_renames(source, names, "glsl", "main")
    proposal = apply_renames(source, names)
    assert "void writeColor()" in proposal
    assert "vec4 resultColor = vec4(1.0)" in proposal
    assert "color = resultColor" in proposal


def test_glsl_field_selector_and_unbound_local_selector_are_rejected():
    with pytest.raises(ValueError, match="preserve interfaces and fields"):
        validate_language_renames(GLSL, {"material.tint": "baseColor"}, "glsl", "main")
    with pytest.raises(ValueError, match="not uniquely source-bound"):
        validate_language_renames(GLSL, {"shade::missing": "value"}, "glsl", "main")


@pytest.mark.parametrize("source,old", [
    ("#version 450\nuniform float scalar; float helper(float scalar) { return scalar; }\nvoid main() {}", "scalar"),
    ("#version 450\nvoid main() { vec2 st = vec2(1); vec2 other = st.st; }", "st"),
    ("#version 450\n#define VALUE \\\n+ scalar\nvoid main() { float scalar = 1; }", "scalar"),
])
def test_glsl_same_line_globals_swizzles_and_macro_continuations_are_protected(source, old):
    with pytest.raises(ValueError):
        validate_language_renames(source, {old: "renamedValue"}, "glsl", "main")


@pytest.mark.parametrize("target,environment", [("-V", "vulkan1.0"), ("-G", "opengl4.5")])
def test_installed_glsl_tools_compile_and_validate_renamed_source(tmp_path, target, environment):
    # Repository-independent installations may provide their own smoke harness;
    # this test claims only locally installed compiler/validator execution.
    if os.name == "nt":
        tool_root = Path(__file__).resolve().parents[3] / "x64" / "Development" / "replaylab" / "tools"
        if not (tool_root / "glslang.exe").is_file():
            tool_root = Path(__file__).resolve().parents[4] / "x64" / "Development" / "replaylab" / "tools"
        compiler, validator = (str(tool_root / name) for name in ("glslang.exe", "spirv-val.exe"))
    else:
        # WSL may see the packaged .exe files, but their path namespace cannot
        # consume a POSIX /tmp source. Exercise native tools when installed.
        compiler = shutil.which("glslangValidator") or shutil.which("glslang")
        validator = shutil.which("spirv-val")
    if not all(tool and Path(tool).is_file() for tool in (compiler, validator)):
        pytest.skip("installed GLSL compiler and SPIR-V validator are unavailable")
    names = {"shade": "computeTint", "coord": "sampleCoord", "result": "tintedColor", "shaded": "surfaceColor"}
    validate_language_renames(GLSL, names, "glsl", "main")
    evidence = []
    for label, text in (("baseline", GLSL), ("organized", apply_renames(GLSL, names))):
        source, output = tmp_path / (label + ".frag"), tmp_path / (label + ".spv")
        source.write_text(text, encoding="utf-8")
        subprocess.run([compiler, target, "-S", "frag", "-o", str(output), str(source)],
                       check=True, capture_output=True, text=True, timeout=15)
        subprocess.run([validator, "--target-env", environment, str(output)],
                       check=True, capture_output=True, text=True, timeout=15)
        evidence.append({"sourceSha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                         "binarySha256": hashlib.sha256(output.read_bytes()).hexdigest()})
    (tmp_path / "verification.json").write_text(json.dumps({"target": target, "environment": environment,
        "compileAndSpirvValidation": True, "replayAbiValidated": False, "evidence": evidence}), encoding="utf-8")
