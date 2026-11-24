"""Fail-closed SPIRV-Cross command adapter.

SPIRV-Cross otherwise selects the first module entry point and defaults HLSL
output to Shader Model 3.0.  This adapter accepts only workspace-derived
stage/entry/profile/API facts, cross-checks them, and builds the fixed CLI used
by the hash-pinned tool registry.  It never searches ``PATH`` or invokes a
shell.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Sequence


_STAGE_TO_SPIRV_CROSS = {
    "vertex": "vert",
    "hull": "tesc",
    "domain": "tese",
    "geometry": "geom",
    "fragment": "frag",
    "compute": "comp",
    "task": "task",
    "mesh": "mesh",
    "raygen": "rgen",
    "intersection": "rint",
    "anyhit": "rahit",
    "closesthit": "rchit",
    "miss": "rmiss",
    "callable": "rcall",
}
_STAGE_TO_PROFILE_PREFIX = {
    "vertex": "vs",
    "hull": "hs",
    "domain": "ds",
    "geometry": "gs",
    "fragment": "ps",
    "compute": "cs",
    "task": "as",
    "mesh": "ms",
}
_PROFILE_RE = re.compile(r"^(ps|vs|gs|hs|ds|cs|ms|as)_(\d+)_(\d+)$")
_HLSL_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _normalise_api(value: str) -> str:
    api = str(value).strip().casefold().replace(" ", "")
    if api.startswith("graphicsapi."):
        api = api[len("graphicsapi.") :]
    return {
        "direct3d11": "d3d11",
        "direct3d12": "d3d12",
        "vk": "vulkan",
    }.get(api, api)


def derive_spirv_cross_arguments(
    *,
    source: str,
    output: str,
    mode: str,
    entry_point: str,
    captured_entry_point: str = "",
    stage: str,
    target_api: str,
    target_profile: str = "",
) -> list[str]:
    """Derive one exact SPIRV-Cross argument vector from physical facts."""

    normalised_stage = str(stage).strip().casefold()
    spirv_stage = _STAGE_TO_SPIRV_CROSS.get(normalised_stage)
    if spirv_stage is None:
        raise ValueError("unsupported SPIRV-Cross stage: %s" % stage)
    entry = str(entry_point).strip()
    if not entry or len(entry) > 512 or "\x00" in entry:
        raise ValueError("entry point must be a bounded non-empty string")
    api = _normalise_api(target_api)
    captured_entry = str(captured_entry_point or entry).strip()
    if not captured_entry or len(captured_entry) > 512 or "\x00" in captured_entry:
        raise ValueError("captured entry point must be a bounded non-empty string")
    arguments = [str(source)]
    if mode in {"hlsl", "vulkan-hlsl"}:
        if mode == "hlsl" and api not in {"d3d11", "d3d12"}:
            raise ValueError("HLSL reconstruction requires a physical Direct3D API")
        if mode == "vulkan-hlsl" and api != "vulkan":
            raise ValueError("Vulkan HLSL authoring requires physical Vulkan API")
        if normalised_stage not in _STAGE_TO_PROFILE_PREFIX:
            raise ValueError("SPIRV-Cross HLSL does not support this stage")
        if not _HLSL_IDENTIFIER_RE.fullmatch(entry):
            raise ValueError("physical HLSL entry point is not a source identifier")
        if not _HLSL_IDENTIFIER_RE.fullmatch(captured_entry):
            raise ValueError("captured HLSL entry point is not a source identifier")
        # This is an authoring dialect, not an invented captured DirectX
        # profile. SPIRV-Cross needs SM 6 syntax for descriptor register spaces.
        profile = ("%s_6_0" % _STAGE_TO_PROFILE_PREFIX[normalised_stage]
                   if mode == "vulkan-hlsl" else str(target_profile).strip().casefold())
        if mode == "vulkan-hlsl" and str(target_profile).strip():
            raise ValueError("Vulkan HLSL must not relabel the physical target profile")
        match = _PROFILE_RE.fullmatch(profile)
        if match is None:
            raise ValueError("HLSL reconstruction requires an exact target profile")
        expected_prefix = _STAGE_TO_PROFILE_PREFIX[normalised_stage]
        if match.group(1) != expected_prefix:
            raise ValueError(
                "target profile %s contradicts physical stage %s"
                % (profile, normalised_stage)
            )
        major = int(match.group(2))
        minor = int(match.group(3))
        if major < 3 or minor > 9:
            raise ValueError("target profile cannot be represented by SPIRV-Cross")
        if api == "d3d11" and major >= 6:
            raise ValueError("Shader Model 6 profile contradicts physical D3D11 API")
        shader_model = str(major * 10 + minor)
        arguments.extend(
            [
                # SPIRV-Cross applies --rename-entry-point before resolving
                # --entry. Select the mechanically renamed output name here;
                # ``entry`` remains the independently parsed physical module
                # entry and is still the rename source below.
                "--entry",
                captured_entry,
                "--stage",
                spirv_stage,
                "--hlsl",
                "--shader-model",
                shader_model,
                # Any explicit rename makes SPIRV-Cross emit the selected
                # physical entry name instead of silently changing it to main.
                "--rename-entry-point",
                entry,
                captured_entry,
                spirv_stage,
            ]
        )
    elif mode == "vulkan-glsl":
        if api != "vulkan":
            raise ValueError("Vulkan GLSL reconstruction requires physical Vulkan API")
        if str(target_profile).strip():
            raise ValueError("Vulkan GLSL reconstruction must not invent a DirectX profile")
        arguments.extend(
            ["--entry", entry, "--stage", spirv_stage, "--vulkan-semantics"]
        )
    elif mode == "opengl-glsl":
        if api != "opengl":
            raise ValueError("OpenGL GLSL reconstruction requires physical OpenGL API")
        if str(target_profile).strip():
            raise ValueError("OpenGL GLSL reconstruction must not invent a DirectX profile")
        # Absence of --vulkan-semantics is intentional. RenderDoc likewise
        # configures a distinct SPIRV-Cross tool for OpenGL-flavoured SPIR-V.
        # The capture encoding is the authority because the SPIR-V header alone
        # cannot identify the consumer environment.
        arguments.extend(["--entry", entry, "--stage", spirv_stage])
    else:
        raise ValueError("unsupported SPIRV-Cross adapter mode")
    arguments.extend(["--output", str(output)])
    return arguments


def preserve_vulkan_locations(source: str) -> str:
    """Keep sparse captured locations through the HLSL compiler's IO mapper.

    SPIRV-Cross encodes physical locations as TEXCOORD/SV_Target indices in its
    generated interface structs. HLSL compilers can otherwise compact them
    (for example location 8 becomes 6), even with --hlsl-iomap enabled.
    Builtins keep their system semantics and are never assigned a location.
    """
    def structure(match: re.Match[str]) -> str:
        body = re.sub(
            r"(?m)^(\s*)([^;\r\n]+\s*:\s*(?:TEXCOORD|SV_Target)(\d+)\s*;)",
            lambda field: "%s[[vk::location(%s)]] %s" %
                (field.group(1), field.group(3), field.group(2)), match.group(2))
        return match.group(1) + body + match.group(3)
    return re.sub(r"(\bstruct\s+SPIRV_Cross_(?:Input|Output)\s*\{)(.*?)(\};)",
                  structure, source, flags=re.S)


def preserve_readonly_array_buffers(source: str, reflection: dict) -> str:
    """Retain a captured scalar/vector storage array's physical element layout.

    SPIRV-Cross lowers storage arrays to ByteAddressBuffer, changing reflection
    from (for example) float4/stride16 to uint/stride4. Word access helpers retain
    the original typed array while preserving Load/Load2/Load3/Load4 bit values,
    including dynamically addressed loads across element boundaries.
    """
    types = reflection.get("types", {})
    element_types = {"float": ("float", 1), "int": ("int", 1), "uint": ("uint", 1)}
    for prefix, scalar in (("vec", "float"), ("ivec", "int"), ("uvec", "uint")):
        for size in (2, 4):
            element_types[prefix + str(size)] = (scalar + str(size), size)
    for resource in reflection.get("ssbos", []):
        fields = types.get(resource.get("type"), {}).get("members", [])
        if (resource.get("readonly") is not True or len(fields) != 1
                or fields[0].get("offset") != 0 or fields[0].get("array") != [0]
                or fields[0].get("type") not in element_types):
            continue
        typename, width = element_types[fields[0]["type"]]
        stride = width * 4
        if fields[0].get("array_stride") != stride:
            continue
        binding, space = resource.get("binding"), resource.get("set", 0)
        if type(binding) is not int or type(space) is not int:
            continue
        declaration = re.compile(r"\bByteAddressBuffer\s+(\w+)\s*:\s*register\(t" +
                                 str(binding) + r"\s*,\s*space" + str(space) + r"\)\s*;")
        matches = list(declaration.finditer(source))
        if len(matches) != 1:
            continue
        name = matches[0].group(1)
        helper = "_bread_" + name + "_word"
        if re.search(r"\b" + re.escape(helper) + r"\b", source):
            continue
        # Only the pure word-load interface is supported here. Keep every other
        # storage shape/method unchanged for the ordinary replay ABI gate.
        methods = set(re.findall(r"\b" + re.escape(name) + r"\.([A-Za-z_]\w*)\s*\(", source))
        if not methods <= {"Load", "Load2", "Load3", "Load4"}:
            continue
        expression = "asuint(%s[address / %du])" % (name, stride)
        if width != 1:
            expression += "[(address %% %du) / 4u]" % stride
        replacement = ("StructuredBuffer<%s> %s : register(t%d, space%d);\n" %
                       (typename, name, binding, space))
        replacement += "uint %s(uint address) { return %s; }\n" % (helper, expression)
        for method in sorted(methods):
            count = int(method[4:] or "1")
            result_type = "uint" + (str(count) if count > 1 else "")
            parts = ["%s(address + %du)" % (helper, component * 4) for component in range(count)]
            value = parts[0] if count == 1 else "%s(%s)" % (result_type, ", ".join(parts))
            replacement += "%s %s_%s(uint address) { return %s; }\n" % (result_type, helper, method, value)
        source = declaration.sub(lambda _: replacement, source, count=1)
        source = re.sub(r"\b" + re.escape(name) + r"\.(Load[234]?)\s*\(",
                        lambda match: helper + "_" + match.group(1) + "(", source)
    return source


def _regular_file(raw: str, label: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise RuntimeError("%s must be absolute" % label)
    if path.is_symlink():
        raise RuntimeError("%s must be a regular non-symlink file" % label)
    path = path.resolve(strict=True)
    if not path.is_file():
        raise RuntimeError("%s must be a regular non-symlink file" % label)
    return path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument(
        "--mode", choices=("hlsl", "vulkan-hlsl", "vulkan-glsl", "opengl-glsl"), required=True
    )
    parser.add_argument("--entry", required=True)
    # Optional only for backwards compatibility with previously generated
    # registries.  The current first-party registry always supplies it; when
    # absent, derive_spirv_cross_arguments deterministically preserves the
    # physical entry name instead of guessing another capture-visible name.
    parser.add_argument("--captured-entry", default="")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--target-api", required=True)
    parser.add_argument("--target-profile", default="")
    parsed = parser.parse_args(argv)

    tool = _regular_file(parsed.tool, "SPIRV-Cross")
    source = _regular_file(parsed.input, "input")
    raw_output = Path(parsed.output)
    if not raw_output.is_absolute():
        raise RuntimeError("output must be absolute")
    if raw_output.is_symlink():
        raise RuntimeError("refusing to overwrite adapter output")
    output = raw_output.resolve()
    if output.exists() or output.is_symlink():
        raise RuntimeError("refusing to overwrite adapter output")
    if output.parent.is_symlink() or not output.parent.is_dir():
        raise RuntimeError("output parent must be a regular directory")

    command = [str(tool)] + derive_spirv_cross_arguments(
        source=str(source),
        output=str(output),
        mode=parsed.mode,
        entry_point=parsed.entry,
        captured_entry_point=parsed.captured_entry,
        stage=parsed.stage,
        target_api=parsed.target_api,
        target_profile=parsed.target_profile,
    )
    completed = subprocess.run(
        command,
        cwd=str(source.parent),
        stdin=subprocess.DEVNULL,
        stdout=None,
        stderr=None,
        shell=False,
        env=dict(os.environ),
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        return int(completed.returncode or 1)
    if output.is_symlink() or not output.is_file() or output.stat().st_size <= 0:
        raise RuntimeError("SPIRV-Cross did not produce its expected output")
    if parsed.mode == "vulkan-hlsl":
        reflected = subprocess.run([str(tool), str(source), "--reflect"], cwd=str(source.parent),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, shell=False, check=False, timeout=60,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if reflected.returncode or len(reflected.stdout) > 64 * 1024 * 1024:
            raise RuntimeError("SPIRV-Cross could not reflect the HLSL storage layout")
        text = preserve_vulkan_locations(output.read_text(encoding="utf-8"))
        text = preserve_readonly_array_buffers(text, json.loads(reflected.stdout))
        output.write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("adapter error: %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        raise SystemExit(1)
