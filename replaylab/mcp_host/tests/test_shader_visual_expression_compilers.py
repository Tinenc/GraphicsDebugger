"""Independent real compiler coverage for helper and call-expression probes.

Set BREAD_VISUAL_FXC, BREAD_VISUAL_DXC and BREAD_VISUAL_GLSLANG to executables.
Missing tools skip only their route. No executable is copied from the host.
"""
import importlib.util
import hashlib
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[3]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, str(ROOT / relative))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


visual = load("bread_expression_visual", "replaylab/renderdoc_mcp_bridge/shader_visual_instrument.py")
samples = load("bread_expression_cases", "replaylab/tests/fixtures/shader_visual_expression_cases.py")
INSTRUMENTER_SHA = hashlib.sha256(Path(visual.__file__).read_bytes()).hexdigest()


@pytest.fixture(scope="session", autouse=True)
def record_source_identity(record_testsuite_property):
    record_testsuite_property("instrumenterSha256AtCollection", INSTRUMENTER_SHA)
    for module in (samples,):
        record_testsuite_property("casesSha256AtCollection",
                                 hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest())


def compiler(route):
    kind = route.split("-")[0]
    executable = os.environ.get("BREAD_VISUAL_" + kind.upper()) or shutil.which(
        {"fxc": "fxc", "dxc": "dxc", "glslang": "glslangValidator"}[kind])
    if not executable or not Path(executable).is_file():
        pytest.skip("Actual " + kind + " compiler unavailable")
    return executable


MATRIX = [(route, case) for route, language in (
    ("fxc", "hlsl"), ("dxc-dxil", "hlsl"), ("dxc-spirv", "hlsl"),
    ("glslang-vulkan", "glsl"), ("glslang-opengl", "glsl")) for case in samples.cases(language)]


@pytest.mark.parametrize("route,case", MATRIX, ids=[route + ":" + case["name"] for route, case in MATRIX])
def test_real_expression_program(route, case, tmp_path):
    tool = compiler(route)
    plan = visual.inspect(case["source"], case["byteStart"], language=case["language"])
    result = visual.instrument(plan, member=case["member"], iteration=case["iteration"])
    shader = tmp_path / ("probe.hlsl" if case["language"] == "hlsl" else "probe.frag")
    shader.write_text(result["source"], encoding="utf-8")
    output = tmp_path / "probe.bin"
    if route == "fxc":
        command = [tool, "/nologo", "/T", "ps_5_0", "/E", "main", "/Fo", str(output), str(shader)]
    elif route.startswith("dxc"):
        command = [tool, "-T", "ps_6_0", "-E", "main", "-Fo", str(output), str(shader)]
        if route == "dxc-spirv":
            command += ["-spirv", "-fspv-target-env=vulkan1.0"]
    else:
        command = [tool, "-S", "frag", "-V" if route.endswith("vulkan") else "-G",
                   "-o", str(output), str(shader)]
    compiled = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True, timeout=30)
    assert compiled.returncode == 0, compiled.stdout
    assert output.is_file() and output.stat().st_size > 32
    if case["name"] == "index-postincrement-once":
        assert result["source"].count("index++") == 1


@pytest.mark.parametrize("needle,skip,member", [
    ("rim = ((RimColorIntensity", 0, ""),
    ("rim *= rimAlbedo", 0, ""),
    ("return rim + backRim", 0, ""),
    ("rim + backRim", 0, ""),
    ("lerp(0.25f.xxx", 0, ""),
    ("EvaluateRimLighting(surface,", len("EvaluateRimLighting("), ".shadingNormal"),
    ("intensityTexture.SampleLevel", 0, ""),
])
def test_user_shader_helper_return_real_dxc_spirv(tmp_path, needle, skip, member):
    source_file = os.environ.get("BREAD_VISUAL_USER_SHADER")
    if not source_file:
        pytest.skip("User-owned source snapshot not supplied")
    tool = compiler("dxc")
    source = Path(source_file).read_text(encoding="utf-8-sig")
    offset = source.index(needle) + skip
    plan = visual.inspect(source, len(source[:offset].encode("utf-8")), entry_point="main")
    result = visual.instrument(plan, member=member)
    shader = tmp_path / "user-rim.hlsl"
    shader.write_text(result["source"], encoding="utf-8")
    compiled = subprocess.run([tool, "-T", "ps_6_0", "-E", "main", "-spirv",
        "-fspv-target-env=vulkan1.0", "-fvk-use-dx-layout", "-Fo", str(tmp_path / "user-rim.spv"), str(shader)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=60)
    assert compiled.returncode == 0, compiled.stdout


def test_user_glsl_mix_real_glslang_spirv(tmp_path):
    source_file = os.environ.get("BREAD_VISUAL_USER_GLSL")
    if not source_file:
        pytest.skip("User-owned GLSL snapshot not supplied")
    tool = compiler("glslang")
    source = Path(source_file).read_text(encoding="utf-8-sig")
    offset = source.index("mix(contrasted")
    prepared = visual.prepare_preprocess(source, len(source[:offset].encode("utf-8")))
    before = tmp_path / "before.frag"
    before.write_text(prepared["source"], encoding="utf-8")
    processed = subprocess.run([tool, "-E", "-S", "frag", str(before)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
    assert processed.returncode == 0, processed.stderr
    mapped = visual.finish_preprocess(processed.stdout, prepared)
    plan = visual.inspect(mapped["source"], mapped["byteStart"], mapped["byteEnd"], language="glsl")
    result = visual.instrument(plan)
    shader = tmp_path / "user-mix.frag"
    shader.write_text(result["source"], encoding="utf-8")
    compiled = subprocess.run([tool, "-V", "-S", "frag", "-o", str(tmp_path / "user-mix.spv"), str(shader)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=60)
    assert compiled.returncode == 0, compiled.stdout
