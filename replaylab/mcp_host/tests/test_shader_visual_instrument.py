"""Behavioral and real-compiler checks for temporary value visualizations."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess

import pytest


MODULE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "shader_visual_instrument.py"
SPEC = importlib.util.spec_from_file_location("bread_visual_instrument_test", MODULE)
visual = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(visual)


def selected(source, needle, language="hlsl", occurrence=0):
    offset = -1
    for _ in range(occurrence + 1):
        offset = source.index(needle, offset + 1)
    return visual.inspect(source, len(source[:offset].encode("utf-8")), language=language)


HLSL = """struct Inputs { float4 position : SV_Position; float2 uv : TEXCOORD0; };
struct Outputs { float2 color : SV_Target0; uint id : SV_Target1; };
Outputs main(Inputs input) {
    float3 lighting = float3(input.uv, 0.25);
    Outputs result;
    result.color = lighting.xy;
    result.id = 77u;
    return result;
}
"""


GLSL = """#version 450
layout(location=0) in vec2 uv;
layout(location=0) out vec2 color;
void main() {
    vec3 lighting = vec3(uv, 0.25);
    color = lighting.xy;
}
"""


def test_rhs_exact_read_and_output_widening_preserves_other_semantics():
    plan = selected(HLSL, "lighting.xy")
    assert plan["selection"]["expression"] == "lighting.xy"
    result = visual.instrument(plan)
    assert "_bread_visual_capture(lighting.xy)" in result["source"]
    assert "float2 color : SV_Target0" in result["source"]
    assert "float4 _bread_visual_o0 : SV_Target0" in result["source"]
    assert "uint _bread_visual_o1 : SV_Target1" in result["source"]
    assert result["semantics"] == "observe-at-expression"
    assert result["selectedOutputSignature"]["originalType"] == "float2"
    assert "input.position" in result["source"]


def test_rhs_read_of_self_assignment_is_not_changed_to_after_assignment():
    source = "float4 main() : SV_Target0 { float x=0.2; x = x + 0.5; return x; }"
    result = visual.instrument(selected(source, "x +"))
    assert "x = _bread_visual_capture(x) + 0.5" in result["source"]


@pytest.mark.parametrize("nested", [False, True])
def test_input_texcoord_arrays_pass_through_without_output_layout_rejection(nested):
    inputs = "struct Input { float4 position : SV_Position; float4 uv[2] : TEXCOORD0; };"
    if nested:
        inputs += "\nstruct Outer { Input inner; };"
    signature = "Outer input" if nested else "Input input"
    path = "input.inner" if nested else "input"
    source = inputs + "\nfloat4 main(" + signature + ") : SV_Target0 { float value = " + path + ".uv[1].x; return value; }"
    generated = visual.instrument(selected(source, "value ="))
    assert "float4 uv[2] : TEXCOORD0" in generated["source"]
    assert signature in generated["source"]
    assert path + ".position" in generated["source"]
    assert "_bread_visual_capture(value)" in generated["source"]


def test_output_array_rejection_is_preserved_separately_from_input_arrays():
    source = "struct Output {float4 color[2] : SV_Target0;}; Output main() {float x=0.5;Output o=(Output)0;return o;}"
    with pytest.raises(visual.VisualDebugError, match="output-array-layout-unsupported"):
        visual.instrument(selected(source, "x=0.5"))


def test_current_source_model_is_shared_but_each_expression_keeps_its_own_position(monkeypatch):
    parsed = []
    original_parse = visual._parse
    def parse(source, language):
        parsed.append(source)
        return original_parse(source, language)
    monkeypatch.setattr(visual, "_parse", parse)
    source = "float4 main():SV_Target0 {float x=0.2;float y=x+0.3;return y;}"
    prepared = visual.prepare_source(source)
    plans = [visual.inspect(source, source.index(needle), prepared=prepared) for needle in ["x=", "y="]]
    assert len(parsed) == 1
    assert plans[0]["model"] is plans[1]["model"]
    assert plans[0]["selection"]["expression"] == "x"
    assert plans[1]["selection"]["expression"] == "y"
    assert "_bread_visual_capture(x)" in visual.instrument(plans[0])["source"]
    assert "_bread_visual_capture(y)" in visual.instrument(plans[1])["source"]
    with pytest.raises(visual.VisualDebugError, match="stale-source-plan"):
        visual.inspect(source.replace("0.2", "0.4"), source.index("x="), prepared=prepared)
    with pytest.raises(visual.VisualDebugError, match="stale-source-plan"):
        visual.inspect(source, source.index("x="), language="glsl", prepared=prepared)


def test_lhs_assignment_observes_converted_assigned_value_once():
    source = "float4 main() : SV_Target0 { int x=2; x += 0.5; return x; }"
    result = visual.instrument(selected(source, "x +="))
    assert "_bread_visual_capture(x += 0.5)" in result["source"]
    assert "int _bread_visual_capture(int " in result["source"]


def test_declaration_probe_follows_initializer():
    source = "float4 main() : SV_Target0 { float x=0.2; return x; }"
    result = visual.instrument(selected(source, "x=0.2"))
    assert "float x=0.2;\n_bread_visual_capture(x);" in result["source"]


def test_index_side_effect_evaluates_once():
    source = "float4 main() : SV_Target0 { float a[2]={0.2,0.4}; int i=0; return a[i++]; }"
    result = visual.instrument(selected(source, "a[i++];"))
    assert result["source"].count("i++") == 1
    assert "_bread_visual_capture(a[i++])" in result["source"]


def test_structure_array_matrix_choices_and_typed_capture():
    source = """struct Material { float3 normal; float roughness[2]; float3x3 transform; };
float4 main() : SV_Target0 { Material m = (Material)0; return float4(m.normal,1); }"""
    plan = selected(source, "m =")
    assert {c["path"] for c in plan["choices"]} == {".normal", ".roughness[0]", ".roughness[1]", ".transform[0]", ".transform[1]", ".transform[2]"}
    result = visual.instrument(plan, ".transform[1]")
    assert "Material _bread_visual_capture(Material " in result["source"]
    assert "_bread_visual_value.transform[1].x" in result["source"]


def test_helper_loop_nth_hit_is_global_across_invocations():
    source = """float helper(float x) { return x * 2.0; }
float4 main() : SV_Target0 { float result=0; for(int i=0;i<3;i++) result+=helper(i); return result; }"""
    result = visual.instrument(selected(source, "x *"), iteration=2)
    assert "_bread_visual_count++ == 2" in result["source"]
    assert "return _bread_visual_capture(x) * 2.0" in result["source"]
    assert "helper(i)" in result["source"]


def test_uint_output_never_roundtrips_through_float32():
    source = "uint4 main() : SV_Target0 { uint id=4294967295u; return id; }"
    result = visual.instrument(selected(source, "id;"), target={"index": 0, "components": 4, "componentType": "uint"})
    assert "uint4(uint(_bread_visual_value), 0, 0, 1)" in result["source"]
    assert "float(_bread_visual_value)" not in result["source"]


def test_glsl_keeps_native_version_and_adapts_output_without_widening_original_math():
    result = visual.instrument(selected(GLSL, "lighting.xy", "glsl"))
    assert result["source"].startswith("#version 450")
    assert "vec2 _bread_visual_original_output;" in result["source"]
    assert "layout(location=0) out vec4 color;" in result["source"]
    assert "_bread_visual_original_output = _bread_visual_capture(lighting.xy)" in result["source"]
    assert "gl_FragCoord" in result["source"]


def test_unicode_positions_are_utf8_and_comments_cannot_supply_symbols():
    source = "// 法线\nfloat4 main() : SV_Target0 { float x=1; return x; }"
    plan = selected(source, "x;")
    assert plan["selection"]["line"] == 2
    assert plan["selection"]["byteStart"] == len(source[:source.index("x;")].encode("utf-8"))
    with pytest.raises(visual.VisualDebugError, match="invalid-utf8-position"):
        visual.inspect(source, 4)


@pytest.mark.parametrize("mode", ["raw", "grayscale", "normal", "range", "coverage"])
def test_modes_and_single_channel_targets(mode):
    result = visual.instrument(selected(HLSL, "lighting.xy"), target={"index": 0, "components": 1, "componentType": "float"}, mode=mode, range_min=-2, range_max=2, channel=1)
    assert result["selectedOutputSignature"]["type"] == "float"
    assert result["requiresCompilerValidation"]


def test_discard_and_clip_after_hit_are_suppressed_without_early_return():
    source = "float4 main(float2 uv:TEXCOORD0):SV_Target0 { float x=uv.x; clip(x-0.5); if(x>0.9) discard; return x; }"
    result = visual.instrument(selected(source, "x=uv"))
    assert "if (!_bread_visual_hit) clip(x-0.5);" in result["source"]
    assert "if(x>0.9) { if (!_bread_visual_hit) discard; }" in result["source"]


@pytest.mark.parametrize("source,needle,reason", [
    ("float4 main():SV_Target0 { float x; return x; }", "x;", "uninitialized-declaration"),
    ("float4 main():SV_Target0 {\n#if FOO\nfloat x=1;\n#else\nint x=2;\n#endif\nreturn x;}", "x;}", "preprocessed-source-required"),
    ("#define TWICE(v) ((v)+(v))\nfloat4 main():SV_Target0 {float x=1;return TWICE(x);}", "x);", "macro-position-unavailable"),
])
def test_unsupported_positions_fail_closed(source, needle, reason):
    with pytest.raises(visual.VisualDebugError, match=reason):
        selected(source, needle)


def test_preprocess_markers_follow_real_selected_expansion():
    source = "float4 main():SV_Target0 {float x=1;return x;}"
    prepared = visual.prepare_preprocess(source, source.index("x;}"))
    done = visual.finish_preprocess(prepared["source"], prepared)
    plan = visual.inspect(done["source"], done["byteStart"], done["byteEnd"])
    assert plan["selection"]["expression"] == "x"
    with pytest.raises(visual.VisualDebugError, match="preprocessed-selection-ambiguous"):
        visual.finish_preprocess(prepared["source"] * 2, prepared)
    with pytest.raises(visual.VisualDebugError, match="preprocessed-selection-ambiguous"):
        visual.finish_preprocess("", prepared)


@pytest.mark.parametrize("expression,kind", [("x++", "postfix-original-value"), ("++x", "prefix-updated-value"), ("x--", "postfix-original-value"), ("--x", "prefix-updated-value")])
def test_increment_captures_expression_value_without_duplicating_side_effect(expression, kind):
    source = "float4 main():SV_Target0 {float x=0.5; return " + expression + ";}"
    plan = visual.inspect(source, source.rindex("x"))
    result = visual.instrument(plan)
    assert plan["selection"]["positionKind"] == kind
    assert "_bread_visual_capture(" + expression + ")" in result["source"]
    assert result["source"].count(expression) == 1


def _compiler(name, environment):
    result = os.environ.get(environment) or shutil.which(name)
    if not result:
        pytest.skip("Set %s to run the actual compiler checks" % environment)
    return result


@pytest.mark.parametrize("source,needle,member", [
    (HLSL, "lighting.xy", ""),
    ("float4 main():SV_Target0 { float x=0.2; x = x+0.5; return x; }", "x = x", ""),
    ("float helper(float x) { return x*2; } float4 main():SV_Target0 {return helper(0.5);}", "x*2", ""),
    ("struct M {float3 n; float k;}; float4 main():SV_Target0 {M m=(M)0; return float4(m.n,1);}", "m=(", ".n"),
    ("void main(float2 uv:TEXCOORD0, out float2 color:SV_Target0) { float x=uv.x; color=x; }", "x;", ""),
])
def test_real_dxc_compiles_hlsl_instrumentation(tmp_path, source, needle, member):
    dxc = _compiler("dxc", "BREAD_VISUAL_DXC")
    generated = visual.instrument(selected(source, needle), member=member)
    shader = tmp_path / "visual.hlsl"
    shader.write_text(generated["source"], encoding="utf-8")
    result = subprocess.run([dxc, "-T", "ps_6_0", "-E", "main", str(shader), "-Fo", str(tmp_path / "visual.dxil")], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "visual.dxil").stat().st_size > 0


@pytest.mark.parametrize("version,target_args", [("450", ["-V"]), ("330 core", []), ("310 es", [])])
@pytest.mark.parametrize("mode", ["raw", "grayscale", "normal", "range", "coverage"])
def test_real_glslang_compiles_glsl_for_vulkan_gl_and_gles(tmp_path, version, target_args, mode):
    compiler = _compiler("glslangValidator", "BREAD_VISUAL_GLSLANG")
    source = GLSL.replace("#version 450", "#version " + version + ("\nprecision highp float;\nprecision highp int;" if "es" in version else ""))
    # Desktop GL 3.3 cannot put a location on a fragment varying input.
    if version == "330 core":
        source = source.replace("layout(location=0) in", "in")
    generated = visual.instrument(selected(source, "lighting.xy", "glsl"), mode=mode)
    shader = tmp_path / "visual.frag"
    shader.write_text(generated["source"], encoding="utf-8")
    args = [compiler, "-S", "frag"] + target_args + [str(shader)]
    if target_args:
        args += ["-o", str(tmp_path / "visual.spv")]
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_compiler_preprocessing_maps_conditional_source(tmp_path):
    dxc = _compiler("dxc", "BREAD_VISUAL_DXC")
    source = "#define ACTIVE 1\nfloat4 main():SV_Target0 {\n#if ACTIVE\nfloat x=0.25;\n#else\nint x=42;\n#endif\nreturn x;}"
    prepared = visual.prepare_preprocess(source, source.index("x;}"))
    shader, output = tmp_path / "before.hlsl", tmp_path / "after.hlsl"
    shader.write_text(prepared["source"], encoding="utf-8")
    result = subprocess.run([dxc, "-T", "ps_6_0", "-E", "main", "-P", "-Fi", str(output), str(shader)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    done = visual.finish_preprocess(output.read_text(encoding="utf-8-sig"), prepared)
    plan = visual.inspect(done["source"], done["byteStart"], done["byteEnd"])
    assert plan["valueType"] == "float"
    assert plan["selection"]["expression"] == "x"


def test_loop_variable_scope_does_not_hide_outer_after_loop():
    source = "float4 main():SV_Target0 {float x=0.5; for(int x=0;x<2;x++) {} return x;}"
    assert selected(source, "x;}")["valueType"] == "float"
    assert selected(source, "x<2")["valueType"] == "int"


def test_cbuffer_global_and_compound_declarators_are_resolved():
    source = "cbuffer C:register(b0) {float4 tint;} float4 main():SV_Target0 {float x=0,y=1; return tint*y;}"
    assert selected(source, "tint*y")["valueType"] == "float4"
    assert selected(source, "y;}")["valueType"] == "float"


@pytest.mark.parametrize("needle", ["a[2]={", "a);"])
def test_real_array_selection_chooses_element_without_losing_array_type(tmp_path, needle):
    dxc = _compiler("dxc", "BREAD_VISUAL_DXC")
    source = "float sum(float a[2]) {return a[0]+a[1];} float4 main():SV_Target0 {float a[2]={0.2,0.4}; return sum(a);}"
    plan = selected(source, needle)
    assert [c["path"] for c in plan["choices"]] == ["[0]", "[1]"]
    generated = visual.instrument(plan, "[1]")
    shader = tmp_path / "array.hlsl"
    shader.write_text(generated["source"], encoding="utf-8")
    result = subprocess.run([dxc, "-T", "ps_6_0", "-E", "main", str(shader), "-Fo", str(tmp_path / "array.dxil")], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("builtin,version", [("gl_FragColor", "100"), ("gl_FragData[0]", "120")])
def test_real_legacy_glsl_outputs(tmp_path, builtin, version):
    compiler = _compiler("glslangValidator", "BREAD_VISUAL_GLSLANG")
    source = "#version " + version + "\n" + ("precision mediump float;\n" if version == "100" else "")
    source += "varying vec2 uv;\nvoid main(){float x=uv.x; " + builtin + "=vec4(x); }"
    generated = visual.instrument(selected(source, "x);", "glsl"))
    shader = tmp_path / "gles2.frag"
    shader.write_text(generated["source"], encoding="utf-8")
    result = subprocess.run([compiler, "-S", "frag", str(shader)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_glsl_output_lvalue_selection_renames_inside_capture():
    result = visual.instrument(selected(GLSL, "color =", "glsl"))
    assert "_bread_visual_capture(_bread_visual_original_output = lighting.xy)" in result["source"]


@pytest.mark.parametrize("mode", ["raw", "grayscale", "normal", "range", "coverage"])
@pytest.mark.parametrize("components,kind", [(1, "float"), (2, "sint"), (4, "uint")])
def test_real_dxc_modes_and_typed_targets(tmp_path, mode, components, kind):
    dxc = _compiler("dxc", "BREAD_VISUAL_DXC")
    source = "[earlydepthstencil] float4 main(float4 pos:SV_Position):SV_Target0 {float3 value=pos.xyz; return float4(value,1);}"
    generated = visual.instrument(selected(source, "value,1"), mode=mode,
                                  target={"index": 0, "components": components, "componentType": kind})
    shader = tmp_path / "typed.hlsl"
    shader.write_text(generated["source"], encoding="utf-8")
    result = subprocess.run([dxc, "-T", "ps_6_0", "-E", "main", str(shader), "-Fo", str(tmp_path / "typed.dxil")], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("declaration,expression,expected", [
    ("vector<float,3> x=float3(0,1,2);", "x", "vector<float,3>"),
    ("unsigned int x=4000000000u;", "x", "uint"),
    ("double x=0.25;", "x", "double"),
    ("bool3 x=bool3(true,false,true);", "x", "bool3"),
    ("uint64_t x=4294967297;", "x", "uint64_t"),
])
def test_real_hlsl_numeric_types_preserve_capture_parameter_type(tmp_path, declaration, expression, expected):
    dxc = _compiler("dxc", "BREAD_VISUAL_DXC")
    source = "float4 main():SV_Target0 {" + declaration + " return float4(float3(" + expression + "),1);}"
    # HLSL scalar-to-vector constructors require an explicit cast.
    source = source.replace("float3(x)", "(float3)x")
    plan = selected(source, "x,1")
    assert plan["valueType"] == expected
    generated = visual.instrument(plan)
    shader = tmp_path / "numeric.hlsl"
    shader.write_text(generated["source"], encoding="utf-8")
    result = subprocess.run([dxc, "-T", "ps_6_0", "-E", "main", str(shader), "-Fo", str(tmp_path / "numeric.dxil")], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_hlsl_constantbuffer_object_field_and_glsl_uniform_block():
    hlsl = "struct Data {float3 tint;}; ConstantBuffer<Data> params:register(b0); float4 main():SV_Target0 {return float4(params.tint,1);}"
    assert selected(hlsl, "tint,1")["valueType"] == "float3"
    glsl = "#version 450\nlayout(binding=0) uniform Data {layout(offset=0) vec3 tint;} params; layout(location=0) out vec4 color; void main(){color=vec4(params.tint,1);}"
    assert selected(glsl, "tint,1", "glsl")["valueType"] == "vec3"


@pytest.mark.parametrize("declaration,expression", [("highp float x=0.25;", "vec4(x)"), ("bool x=true;", "vec4(float(x))")])
def test_real_gles_precision_and_boolean_probe(tmp_path, declaration, expression):
    compiler = _compiler("glslangValidator", "BREAD_VISUAL_GLSLANG")
    source = "#version 100\nprecision mediump float; void main(){" + declaration + " gl_FragColor=" + expression + ";}"
    plan = visual.inspect(source, source.rindex("x"), language="glsl")
    generated = visual.instrument(plan)
    shader = tmp_path / "precision.frag"
    shader.write_text(generated["source"], encoding="utf-8")
    result = subprocess.run([compiler, "-S", "frag", str(shader)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    if "highp float x" in source:
        assert "highp float _bread_visual_capture(highp float" in generated["source"]


@pytest.mark.parametrize("language,intrinsic,vector", [("hlsl", "lerp", "float3"), ("glsl", "mix", "vec3")])
def test_call_and_return_normalization_preserves_explicit_subexpression(language, intrinsic, vector):
    helper = "{0} helper({0} a, {0} b) {{ return {1}(a,b,0.25) + b; }}".format(vector, intrinsic)
    source = helper + ("float4 main():SV_Target0{return float4(helper(0.0,1.0),1);}" if language == "hlsl" else
                       "void main(){gl_FragColor=vec4(helper(vec3(0),vec3(1)),1);}")
    plan = selected(source, "return " + intrinsic, language)
    assert plan["selection"]["expression"] == intrinsic + "(a,b,0.25) + b"
    assert plan["valueType"] == vector
    call = selected(source, intrinsic + "(a,b", language)
    assert call["selection"]["expression"] == intrinsic + "(a,b,0.25)"
    assert call["selection"]["positionKind"] == "call-result"
    offset = source.index("a,b,")
    exact = visual.inspect(source, offset, offset + 1, language=language)
    assert exact["selection"]["expression"] == "a"
    prepared = visual.prepare_preprocess(source, source.index("return"))
    assert prepared["originalSelection"]["expression"] == intrinsic + "(a,b,0.25) + b"


def test_opaque_and_unnamed_signatures_do_not_block_other_functions():
    hlsl = "Texture2D<float4> tex;SamplerState samplerState;" +            "float unrelated(Texture3D<float4> resource,SamplerState state){return 0;}" +            "float helper(float x){return x;}float4 main():SV_Target0{return helper(0.5);}"
    assert selected(hlsl, "helper(0.5)")["valueType"] == "float"
    glsl = "float spvNMin(float,float);float other(texture3D tex,sampler state){return 0;}" +            "void main(){float x=0.5;gl_FragColor=vec4(x);}"
    assert selected(glsl, "x);", "glsl")["valueType"] == "float"
    direct = hlsl.replace("return helper(0.5);", "return tex.Load(int3(0,0,0));")
    assert selected(direct, "tex.Load")["valueType"] == "float4"


def test_struct_call_choice_keeps_one_evaluation_and_skips_opaque_leaf():
    source = "struct Payload{float3 tint;SamplerState samplerValue;};" +              "Payload make(){Payload p; p.tint=0.25;return p;}" +              "float4 main():SV_Target0 {Payload p=make();return float4(p.tint,1);}"
    plan = selected(source, "make();")
    assert plan["choices"] == [{"path": ".tint", "type": "float3"}]
    generated = visual.instrument(plan, ".tint")["source"]
    assert generated.count("make()") == 2  # definition and the one original call
    assert "_bread_visual_capture(make())" in generated
    assert "Payload _bread_visual_capture(Payload " in generated


def test_partial_numeric_word_expands_to_number_and_swizzle():
    source = "float4 main():SV_Target0 {return float4(0.25f.xxx,1);}"
    start = source.index("25f")
    plan = visual.inspect(source, start, start + 3)
    assert plan["selection"]["expression"] == "0.25f.xxx"
    assert plan["valueType"] == "float3"


@pytest.mark.parametrize("initializer,expected", [("for(int i=0;i<2;i++){}", "int"),
                                                 ("float first=0.2,second=0.3;", "float")])
def test_header_and_compound_initializers_are_captured_at_their_evaluation(initializer, expected):
    source = "float4 main():SV_Target0 {" + initializer + "return 0;}"
    needle = "i=0" if initializer.startswith("for") else "first=0.2"
    plan = selected(source, needle)
    generated = visual.instrument(plan)["source"]
    assert plan["insertAfter"] is None
    assert plan["initializerType"] == expected
    literal = "0" if expected == "int" else "0.2"
    assert "_bread_visual_capture((%s)(%s))" % (expected, literal) in generated


def test_out_argument_rejection_does_not_reject_whole_returning_call_or_index_read():
    source = "float update(inout float value){value+=0.2;return value;}" +              "float4 main():SV_Target0 {float values[2]={0.2,0.4};int i=0;return update(values[i]);}"
    assert selected(source, "update(values")["valueType"] == "float"
    assert selected(source, "i]);")["valueType"] == "int"
    with pytest.raises(visual.VisualDebugError, match="out-argument-position"):
        selected(source, "values[i]")


def test_call_overload_return_type_uses_exact_arguments_or_reports_ambiguity():
    source = "float choose(float x){return x;} int choose(int x){return x;}" +              "float4 main():SV_Target0 {return choose(1);}"
    assert selected(source, "choose(1)")["valueType"] == "int"
    with pytest.raises(visual.VisualDebugError, match="ambiguous-call-type"):
        selected(source.replace("choose(1)", "choose(true)"), "choose(true)")


def test_return_comma_expression_keeps_both_side_effects_in_one_capture_argument():
    source = "float helper(){float x=0; return x+=0.2,x+=0.3;}float4 main():SV_Target0{return helper();}"
    result = visual.instrument(selected(source, "return x+="))["source"]
    assert "return _bread_visual_capture((x+=0.2,x+=0.3));" in result


def test_partial_explicit_call_is_rejected_without_indexing_outside_selection():
    source = "float4 main():SV_Target0 {return lerp(0.2,0.8,0.5);}"
    start = source.index("lerp")
    with pytest.raises(visual.VisualDebugError, match="unsupported-value-expression"):
        visual.inspect(source, start, start + len("lerp(0.2"))


@pytest.mark.parametrize("expression,expected", [("lerp(0,1,0.5)", "float"),
    ("sin(1)", "float"), ("f16tof32(0x3800u)", "float"), ("f32tof16(0.5)", "uint")])
def test_intrinsic_numeric_result_does_not_narrow_to_integer_arguments(expression, expected):
    source = "float4 main():SV_Target0 {return " + expression + ";}"
    assert selected(source, expression)["valueType"] == expected


@pytest.mark.parametrize("expression,selection", [("2*a+b", "a+b"), ("a && b || c", "b || c"),
    ("a-b-c", "b-c"), ("a?b:c+d", "c+d")])
def test_explicit_selection_preserves_original_precedence_tree(expression, selection):
    source = "float4 main():SV_Target0 {float a=1,b=2,c=3,d=4;return " + expression + ";}"
    start = source.index(selection, source.index("return"))
    if expression == "a?b:c+d":
        assert visual.inspect(source, start, start + len(selection))["valueType"] == "float"
    else:
        with pytest.raises(visual.VisualDebugError, match="partial-expression-selection"):
            visual.inspect(source, start, start + len(selection))


@pytest.mark.parametrize("expression,selection", [("a+b*c", "b*c"), ("2*(a+b)", "a+b"),
    ("a&&b||c", "a&&b"), ("lerp(a+b,c,0.25)", "a+b")])
def test_explicit_complete_subtrees_remain_supported(expression, selection):
    source = "float4 main():SV_Target0 {float a=1,b=2,c=3;return " + expression + ";}"
    start = source.index(selection, source.index("return"))
    plan = visual.inspect(source, start, start + len(selection))
    assert plan["selection"]["expression"] == selection


@pytest.mark.parametrize("expression,expected", [("sign(x)", "int"),
    ("clamp(i,0.25,2.4)", "float"), ("lerp(0,1,float3(0.2,0.4,0.6))", "float3")])
def test_intrinsic_overload_types_preserve_caller_overload_dispatch(expression, expected):
    source = "float4 main():SV_Target0 {float x=0.5;int i=0;return " + expression + ";}"
    plan = selected(source, expression)
    assert plan["valueType"] == expected
    assert expected + " _bread_visual_capture(" + expected in visual.instrument(plan)["source"]


@pytest.mark.parametrize("expression,expected", [("a+b", "int"), ("a*b", "int"), ("-a", "int"),
    ("+a", "int"), ("~a", "int"), ("a&&b", "bool"), ("1.0L", "double"),
    ("0x100000000ull", "uint64_t"), ("0x100000000ll", "int64_t")])
def test_hlsl_arithmetic_promotions_and_wide_literals_are_not_narrowed(expression, expected):
    source = "float4 main():SV_Target0 {bool a=true,b=true;return " + expression + ";}"
    start = source.index(expression, source.index("return"))
    plan = visual.inspect(source, start, start + len(expression))
    assert plan["valueType"] == expected


@pytest.mark.parametrize("expression,expected", [("sin(wide)", "float"), ("lerp(wide,wide,wide)", "float"),
    ("length(double2(wide,wide))", "float"), ("dot(bool2(true,true),bool2(true,true))", "float"),
    ("dot(double2(wide,wide),double2(wide,wide))", "double"), ("abs(wide)", "double"),
    ("clamp(wide,wide,wide)", "double")])
def test_hlsl_float_intrinsic_contracts_are_distinct_from_arithmetic_overloads(expression, expected):
    source = "float4 main():SV_Target0 {double wide=0.5L;return " + expression + ";}"
    assert selected(source, expression)["valueType"] == expected


@pytest.mark.parametrize("expression,expected", [("saturate(wide)", "double"), ("saturate(bits)", "float2"),
    ("abs(bits)", "float2"), ("min(bits,bits)", "float2"), ("clamp(bits,bits,bits)", "float2"),
    ("dot(signedValue,unsignedValue)", "uint"), ("dot(small,signedValue)", "float")])
def test_hlsl_boolean_and_mixed_intrinsic_overloads(expression, expected):
    source = "float4 main():SV_Target0 {double wide=0.5L;bool2 bits=true;int2 signedValue=1;uint2 unsignedValue=2;half2 small=0.5;return " + expression + ";}"
    assert selected(source, expression)["valueType"] == expected


@pytest.mark.parametrize("expression,expected", [("wide+unsignedValue", "uint64_t"),
    ("small+signedValue", "min16float"), ("small+unsignedValue", "min16float")])
def test_hlsl_mixed_unsigned_width_and_minimum_precision_are_preserved(expression, expected):
    source = "float4 main():SV_Target0 {int64_t wide=1;uint unsignedValue=2;int signedValue=1;min16float small=0.5;return " + expression + ";}"
    start = source.index(expression, source.index("return"))
    assert visual.inspect(source, start, start + len(expression))["valueType"] == expected


@pytest.mark.parametrize("expression,expected", [("sin(1)", "float"), ("sin(ivec2(1))", "vec2")])
def test_desktop_glsl_floating_intrinsics_promote_integer_arguments(expression, expected):
    source = "#version 450\nlayout(location=0) out vec4 color;void main(){color=vec4(" + expression + ");}"
    plan = selected(source, expression, "glsl")
    assert plan["valueType"] == expected
    assert expected + " _bread_visual_capture(" + expected in visual.instrument(plan)["source"]


@pytest.mark.parametrize("target_args", [["-V"], ["-G"]])
def test_real_desktop_glsl_sin_integer_argument_keeps_float_result(tmp_path, target_args):
    compiler = _compiler("glslangValidator", "BREAD_VISUAL_GLSLANG")
    source = "#version 450\nlayout(location=0) out vec4 color;void main(){color=vec4(sin(1));}"
    generated = visual.instrument(selected(source, "sin(1)", "glsl"))
    shader = tmp_path / "integer_sin.frag"
    shader.write_text(generated["source"], encoding="utf-8")
    result = subprocess.run([compiler, "-S", "frag"] + target_args + [str(shader), "-o", str(tmp_path / "integer_sin.spv")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
