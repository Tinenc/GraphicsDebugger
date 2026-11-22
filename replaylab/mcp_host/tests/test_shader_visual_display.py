"""Display shape, channel rejection, and real compiler coverage."""
import hashlib
import importlib.util
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


visual = load("bread_display_visual", "replaylab/renderdoc_mcp_bridge/shader_visual_instrument.py")
samples = load("bread_display_cases", "replaylab/tests/fixtures/shader_visual_display_cases.py")
SOURCE_HASH = hashlib.sha256(Path(visual.__file__).read_bytes()).hexdigest()


@pytest.fixture(scope="session", autouse=True)
def source_identity(record_testsuite_property):
    record_testsuite_property("instrumenterSha256AtCollection", SOURCE_HASH)


@pytest.mark.parametrize("language", ["hlsl", "glsl"])
@pytest.mark.parametrize("mode", ["custom", "raw", "grayscale", "normal", "range", "coverage"])
@pytest.mark.parametrize("width,channel", [(1, 1), (2, 2), (3, 3)])
def test_unavailable_channel_is_rejected(language, mode, width, channel):
    case = next(c for c in samples.cases(language) if c["name"] == ("custom-scalar" if width == 1 else "custom-float" + str(width)))
    plan = visual.inspect(case["source"], case["byteStart"], language=language)
    with pytest.raises(visual.VisualDebugError, match="channel-out-of-range"):
        visual.instrument(plan, mode=mode, channel=channel)


MATRIX = [(route, case) for route, lang, gles in (
    ("fxc", "hlsl", False), ("dxc-dxil", "hlsl", False), ("dxc-spirv", "hlsl", False),
    ("glslang-vulkan", "glsl", False), ("glslang-opengl", "glsl", False),
    ("glslang-gles", "glsl", True)) for case in samples.cases(lang, gles)]


@pytest.mark.parametrize("route,case", MATRIX, ids=[r + ":" + c["name"] for r, c in MATRIX])
def test_display_real_compiler(route, case, tmp_path):
    kind = route.split("-")[0]
    tool = os.environ.get("BREAD_VISUAL_" + kind.upper()) or shutil.which({"fxc": "fxc", "dxc": "dxc", "glslang": "glslangValidator"}[kind])
    if not tool or not Path(tool).is_file():
        pytest.skip("Actual " + kind + " compiler unavailable")
    plan = visual.inspect(case["source"], case["byteStart"], language=case["language"])
    target = dict(index=0, components=case["targetComponents"], componentType=case["targetType"])
    generated = visual.instrument(plan, target=target, mode=case["mode"], channel=case["channel"],
        range_min=case.get("rangeMin", 0), range_max=case.get("rangeMax", 1))
    shader = tmp_path / ("display.hlsl" if case["language"] == "hlsl" else "display.frag")
    shader.write_text(generated["source"], encoding="utf-8")
    output = tmp_path / "display.bin"
    if kind == "fxc":
        command = [tool, "/nologo", "/T", "ps_5_0", "/E", "main", "/Fo", str(output), str(shader)]
    elif kind == "dxc":
        command = [tool, "-T", "ps_6_0", "-E", "main", "-Fo", str(output), str(shader)]
        if route == "dxc-spirv":
            command += ["-spirv", "-fspv-target-env=vulkan1.0"]
    else:
        command = [tool, "-S", "frag"]
        if route != "glslang-gles":
            command += ["-V" if route.endswith("vulkan") else "-G", "-o", str(output)]
        command += [str(shader)]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=30)
    assert result.returncode == 0, result.stdout
    if case["targetType"] == "uint":
        assert "float(_bread_visual_value" not in generated["source"]
