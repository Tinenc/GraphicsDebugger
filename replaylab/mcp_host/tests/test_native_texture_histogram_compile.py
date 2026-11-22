"""Compile the embedded histogram shaders, not just the C++ resource wrapper.

The replay core builds all variants at capture-open time with warnings treated
as errors. C++/resource compilation cannot detect a broken HLSL variant, and an
invalid shader blob used to surface as a capture-open crash.
"""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def _fxc():
    explicit = shutil.which("fxc.exe")
    if explicit:
        return Path(explicit)
    kits = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Windows Kits/10/bin"
    candidates = sorted(kits.glob("*/x64/fxc.exe"), reverse=True) if os.name == "nt" else []
    return candidates[0] if candidates else None


FXC = _fxc()
SHADER = Path(__file__).resolve().parents[3] / "renderdoc/data/hlsl/histogram.hlsl"
VARIANTS = [(resource_type, base_type, entry)
            for resource_type in (1, 2, 3, 4, 5, 6, 7, 9)
            for base_type in (0, 1, 2)
            for entry in (["RENDERDOC_TileMinMaxCS", "RENDERDOC_HistogramCS"] +
                          (["RENDERDOC_ResultMinMaxCS"] if resource_type == 1 else []))]


@pytest.mark.skipif(FXC is None, reason="Windows SDK FXC compiler is not installed")
@pytest.mark.parametrize("resource_type,base_type,entry", VARIANTS)
def test_native_histogram_shader_variants_compile_with_warnings_as_errors(
        tmp_path, resource_type, base_type, entry):
    output = tmp_path / "histogram.dxbc"
    result = subprocess.run([
        str(FXC), "/nologo", "/WX", "/T", "cs_5_0", "/E", entry,
        "/D", "SHADER_RESTYPE=" + str(resource_type),
        "/D", "SHADER_BASETYPE=" + str(base_type),
        "/Fo", str(output), str(SHADER),
    ], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.read_bytes().startswith(b"DXBC")
