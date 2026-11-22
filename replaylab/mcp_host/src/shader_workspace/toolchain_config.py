"""Generate hash-pinned example configs for trusted shader command-line tools.

This utility performs no downloads and never searches ``PATH``.  Every binary
must be supplied by absolute path by the local operator.  The generated route
catalog is then cryptographically bound to the exact generated tool registry.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from .errors import ToolConfigurationError
from .io import atomic_write_json
from .toolchains import route_record, toolchain_document, validator_record
from .tools import TOOLS_SCHEMA, TOOLS_VERSION, ToolRegistry

DXC_SOURCE = "https://github.com/microsoft/DirectXShaderCompiler"
DXC_LICENSE = (
    "Use the license/notice shipped with the locally supplied DXC distribution"
)
FXC_SOURCE = "https://learn.microsoft.com/windows/win32/direct3dtools/dx-graphics-tools-fxc"
D3DCOMPILE_SOURCE = (
    "https://learn.microsoft.com/windows/win32/api/d3dcompiler/"
    "nf-d3dcompiler-d3dcompile"
)
SPIRV_CROSS_SOURCE = (
    "https://github.com/KhronosGroup/SPIRV-Cross/blob/main/main.cpp"
)
GLSLANG_SOURCE = "https://github.com/KhronosGroup/glslang"
SPIRV_TOOLS_SOURCE = "https://github.com/KhronosGroup/SPIRV-Tools"
HLSL_DECOMPILER_SOURCE = (
    "https://github.com/bo3b/3Dmigoto/blob/master/"
    "HLSLDecompiler/cmd_Decompiler/cmd_Decompiler.cpp"
)
DXBC_ASSEMBLER_1316_SOURCE = (
    "https://github.com/bo3b/3Dmigoto/blob/cbc4993/"
    "HLSLDecompiler/cmd_Decompiler/cmd_Decompiler.cpp"
)
DXIL_SPIRV_SOURCE = (
    "https://github.com/HansKristian-Work/dxil-spirv/blob/master/dxil_spirv.cpp"
)
RENODX_DECOMPILER_SOURCE = (
    "https://github.com/clshortfuse/renodx/blob/main/src/decompiler/cli.cpp"
)

_GLSLANG_STAGE_ARGUMENTS = {
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
_OPENGL_GLSLANG_STAGES = frozenset(
    {"vertex", "hull", "domain", "geometry", "fragment", "compute"}
)
_SPIRV_TOOLS_OPENGL_TARGET_ENV = "opengl4.5"
_VULKAN_TARGET_ENVS = frozenset(
    {
        "vulkan1.0",
        "vulkan1.1",
        "vulkan1.2",
        "vulkan1.3",
    }
)


def _vulkan_target_env(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    normalised = str(value).strip().casefold()
    if normalised not in _VULKAN_TARGET_ENVS:
        raise ToolConfigurationError(
            "unsupported Vulkan SPIR-V target environment %r; expected one of %s"
            % (value, ", ".join(sorted(_VULKAN_TARGET_ENVS)))
        )
    return normalised


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _executable(value: str | Path | None, label: str) -> Path | None:
    if value is None:
        return None
    source = Path(value).expanduser()
    if not source.is_absolute():
        raise ToolConfigurationError("%s path must be absolute" % label)
    if source.is_symlink() or not source.is_file():
        raise ToolConfigurationError("%s must be a regular non-symlink file" % label)
    return source.resolve()


def _fixed_files(
    provider: str,
    executable: Path,
    extra_fixed_files: Mapping[str, Sequence[str | Path]],
) -> dict[str, str]:
    paths: list[Path] = []
    if provider in {"dxc", "renodx-decompiler"}:
        # Official DXC release archives normally place these beside dxc.exe.
        # RenoDX decomp.exe likewise loads dxcompiler.dll for DXIL
        # disassembly. Pin these side-by-side implementations when present.
        for name in ("dxcompiler.dll", "dxil.dll"):
            candidate = executable.parent / name
            if candidate.is_file() and not candidate.is_symlink():
                paths.append(candidate.resolve())
    for raw in extra_fixed_files.get(provider, ()):
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            raise ToolConfigurationError("extra fixed files must use absolute paths")
        if candidate.is_symlink() or not candidate.is_file():
            raise ToolConfigurationError(
                "extra fixed file must be regular and non-symlink: %s" % candidate
            )
        paths.append(candidate.resolve())
    unique: dict[str, Path] = {}
    for path in paths:
        if path == executable:
            raise ToolConfigurationError("executable must not be repeated as a fixed file")
        unique[str(path).casefold()] = path
    return {str(unique[key]): _hash(unique[key]) for key in sorted(unique)}


def generate_official_toolchain_configs(
    tool_config_path: str | Path,
    route_config_path: str | Path,
    *,
    dxc: str | Path | None = None,
    fxc: str | Path | None = None,
    d3dcompiler: str | Path | None = None,
    spirv_cross: str | Path | None = None,
    glslang: str | Path | None = None,
    spirv_dis: str | Path | None = None,
    spirv_as: str | Path | None = None,
    spirv_val: str | Path | None = None,
    spirv_target_env: str | None = None,
    hlsl_decompiler: str | Path | None = None,
    dxil_spirv: str | Path | None = None,
    renodx_decompiler: str | Path | None = None,
    adapter_host: str | Path | None = None,
    enable_opengl_spirv: bool = False,
    extra_fixed_files: Mapping[str, Sequence[str | Path]] | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Generate trusted examples from explicitly supplied local binaries.

    The historical function name is retained for API compatibility.  Generated
    catalogs can also contain the explicitly requested community adapters; the
    route metadata labels those implementations and their licenses precisely.
    """

    tool_path = Path(tool_config_path).expanduser().resolve()
    route_path = Path(route_config_path).expanduser().resolve()
    if tool_path == route_path:
        raise ToolConfigurationError("tool and route config paths must differ")
    if not overwrite and (tool_path.exists() or route_path.exists()):
        raise ToolConfigurationError("refusing to overwrite an existing config")
    extras = dict(extra_fixed_files or {})
    if not isinstance(enable_opengl_spirv, bool):
        raise ToolConfigurationError("enable_opengl_spirv must be a boolean")
    unknown_extra = set(extras).difference(
        {
            "dxc",
            "fxc",
            "spirv-cross",
            "glslang",
            "spirv-dis",
            "spirv-as",
            "spirv-val",
            "hlsl-decompiler",
            "dxil-spirv",
            "renodx-decompiler",
        }
    )
    if unknown_extra:
        raise ToolConfigurationError(
            "unknown extra-fixed-file provider: %s" % ", ".join(sorted(unknown_extra))
        )
    binaries = {
        "dxc": _executable(dxc, "dxc"),
        "fxc": _executable(fxc, "fxc"),
        "d3dcompiler": _executable(d3dcompiler, "d3dcompiler"),
        "spirv-cross": _executable(spirv_cross, "spirv-cross"),
        "glslang": _executable(glslang, "glslang"),
        "spirv-dis": _executable(spirv_dis, "spirv-dis"),
        "spirv-as": _executable(spirv_as, "spirv-as"),
        "spirv-val": _executable(spirv_val, "spirv-val"),
        "hlsl-decompiler": _executable(hlsl_decompiler, "hlsl-decompiler"),
        "dxil-spirv": _executable(dxil_spirv, "dxil-spirv"),
        "renodx-decompiler": _executable(
            renodx_decompiler, "renodx-decompiler"
        ),
    }
    if binaries["renodx-decompiler"] is not None:
        dependency = binaries["renodx-decompiler"].parent / "dxcompiler.dll"
        if dependency.is_symlink() or not dependency.is_file():
            raise ToolConfigurationError(
                "renodx-decompiler requires a regular non-symlink "
                "side-by-side dxcompiler.dll"
            )
    packaged_adapter_host = _executable(adapter_host, "adapter-host")
    target_env = _vulkan_target_env(spirv_target_env)
    if target_env is None and (
        binaries["glslang"] is not None or binaries["spirv-val"] is not None
    ):
        raise ToolConfigurationError(
            "--spirv-target-env is required for glslang/spirv-val; "
            "the Vulkan/SPIR-V environment will not be guessed"
        )
    if not any(binaries.values()):
        raise ToolConfigurationError("at least one explicit shader tool path is required")
    tools: dict[str, dict[str, Any]] = {}

    def add(
        tool_id: str,
        provider: str,
        operation: str,
        arguments: list[str],
        timeout: int = 120,
    ) -> None:
        executable = binaries[provider]
        if executable is None:
            return
        identity = {
            "executable": str(executable),
            "executableSha256": _hash(executable),
            "fixedFileHashes": _fixed_files(provider, executable, extras),
        }
        record = tools.get(tool_id)
        if record is None:
            record = dict(identity)
            tools[tool_id] = record
        elif any(record.get(key) != value for key, value in identity.items()):
            raise ToolConfigurationError(
                "one tool id cannot combine different executable identities"
            )
        if operation in record:
            raise ToolConfigurationError(
                "duplicate generated operation %s.%s" % (tool_id, operation)
            )
        record[operation] = {"args": arguments, "timeoutSeconds": timeout}

    if binaries["fxc"] is not None:
        add(
            "fxc-exact-profile",
            "fxc",
            "compile",
            [
                "/nologo",
                "/Zi",
                "/T",
                "{target_profile}",
                "/E",
                "{entry_point}",
                "/Fo",
                "{output}",
                "{input}",
            ],
        )
        add(
            "fxc-exact-profile",
            "fxc",
            "decompile",
            ["/nologo", "/dumpbin", "/Fc", "{output}", "{input}"],
        )
    if binaries["d3dcompiler"] is not None:
        dll = binaries["d3dcompiler"]
        if dll.name.casefold() != "d3dcompiler_47.dll":
            raise ToolConfigurationError(
                "d3dcompiler path must be named d3dcompiler_47.dll"
            )
        adapter = (
            Path(__file__).resolve().parent
            / "adapters"
            / "d3dcompiler_adapter.py"
        )
        python = packaged_adapter_host or Path(sys.executable).resolve()
        dll_hash = _hash(dll)
        fixed = {str(dll): dll_hash}
        if packaged_adapter_host is None:
            if adapter.is_symlink() or not adapter.is_file():
                raise ToolConfigurationError("bundled D3DCompiler adapter is missing")
            fixed[str(adapter)] = _hash(adapter)
        tools["d3dcompiler47-exact-profile"] = {
            "executable": str(python),
            "executableSha256": _hash(python),
            "fixedFileHashes": fixed,
            "compile": {
                "args": (
                    ["adapter-d3dcompile"]
                    if packaged_adapter_host
                    else [str(adapter)]
                )
                + [
                    "--dll",
                    str(dll),
                    "--dll-sha256",
                    dll_hash,
                    "--input",
                    "{input}",
                    "--output",
                    "{output}",
                    "--entry",
                    "{entry_point}",
                    "--target",
                    "{target_profile}",
                ],
                "timeoutSeconds": 120,
            },
        }
    if binaries["dxc"] is not None:
        dxc_command = binaries["dxc"]
        common = [
            "-T",
            "{target_profile}",
            "-E",
            "{entry_point}",
            "-Fo",
            "{output}",
            "{input}",
        ]
        add("dxc-exact-profile-dxil", "dxc", "compile", list(common))
        add(
            "dxc-exact-profile-dxil",
            "dxc",
            "decompile",
            ["-dumpbin", "-Fc", "{output}", "{input}"],
        )
        if target_env is not None:
            add(
                "dxc-exact-profile-spirv",
                "dxc",
                "compile",
                ["-spirv", "-fspv-target-env=%s" % target_env] + common,
            )
        dxcompiler = dxc_command.parent / "dxcompiler.dll"
        if dxcompiler.is_file() and not dxcompiler.is_symlink():
            dxcompiler = dxcompiler.resolve()
            adapter = (
                Path(__file__).resolve().parent
                / "adapters"
                / "dxil_assembler_adapter.py"
            )
            python = packaged_adapter_host or Path(sys.executable).resolve()
            dll_hash = _hash(dxcompiler)
            fixed = _fixed_files("dxc", dxc_command, extras)
            # _fixed_files includes a regular side-by-side dxcompiler.dll, but
            # keep this assignment explicit so the operation can never be
            # generated without the exact DLL hash repeated in its arguments.
            fixed[str(dxcompiler)] = dll_hash
            if packaged_adapter_host is None:
                if adapter.is_symlink() or not adapter.is_file():
                    raise ToolConfigurationError("bundled DXIL assembler adapter is missing")
                fixed[str(adapter)] = _hash(adapter)
            tools["dxc-official-dxil-assembler"] = {
                "executable": str(python),
                "executableSha256": _hash(python),
                "fixedFileHashes": fixed,
                "compile": {
                    "args": (
                        ["adapter-dxil-assemble"]
                        if packaged_adapter_host
                        else [str(adapter)]
                    )
                    + [
                        "--dll",
                        str(dxcompiler),
                        "--dll-sha256",
                        dll_hash,
                        "--input",
                        "{input}",
                        "--output",
                        "{output}",
                    ],
                    "timeoutSeconds": 120,
                },
            }
    if binaries["spirv-cross"] is not None:
        command = binaries["spirv-cross"]
        adapter = (
            Path(__file__).resolve().parent
            / "adapters"
            / "spirv_cross_adapter.py"
        )
        python = packaged_adapter_host or Path(sys.executable).resolve()
        fixed = _fixed_files("spirv-cross", command, extras)
        fixed[str(command)] = _hash(command)
        if packaged_adapter_host is None:
            if adapter.is_symlink() or not adapter.is_file():
                raise ToolConfigurationError("bundled SPIRV-Cross adapter is missing")
            fixed[str(adapter)] = _hash(adapter)
        common_adapter_arguments = (["adapter-spirv-cross"] if packaged_adapter_host
                                    else [str(adapter)]) + [
            "--tool",
            str(command),
            "--input",
            "{input}",
            "--output",
            "{output}",
            "--entry",
            "{entry_point}",
            "--captured-entry",
            "{captured_entry_point}",
            "--stage",
            "{stage}",
            "--target-api",
            "{target_api}",
        ]
        tools["spirv-cross-hlsl"] = {
            "executable": str(python),
            "executableSha256": _hash(python),
            "fixedFileHashes": dict(fixed),
            "decompile": {
                "args": common_adapter_arguments
                + [
                    "--mode",
                    "hlsl",
                    "--target-profile",
                    "{target_profile}",
                ],
                "timeoutSeconds": 120,
            },
        }
        tools["spirv-cross-vulkan-glsl"] = {
            "executable": str(python),
            "executableSha256": _hash(python),
            "fixedFileHashes": dict(fixed),
            "decompile": {
                "args": common_adapter_arguments + ["--mode", "vulkan-glsl"],
                "timeoutSeconds": 120,
            },
        }
        tools["spirv-cross-vulkan-hlsl"] = {
            "executable": str(python),
            "executableSha256": _hash(python),
            "fixedFileHashes": dict(fixed),
            "decompile": {
                "args": common_adapter_arguments + ["--mode", "vulkan-hlsl"],
                "timeoutSeconds": 120,
            },
        }
        if enable_opengl_spirv:
            tools["spirv-cross-opengl-glsl"] = {
                "executable": str(python),
                "executableSha256": _hash(python),
                "fixedFileHashes": dict(fixed),
                "decompile": {
                    "args": common_adapter_arguments + ["--mode", "opengl-glsl"],
                    "timeoutSeconds": 120,
                },
            }
    if binaries["glslang"] is not None:
        for stage, glslang_stage in _GLSLANG_STAGE_ARGUMENTS.items():
            if stage in {"vertex", "hull", "domain", "geometry", "fragment", "compute"}:
                add(
                    "glslang-hlsl-%s-vulkan" % stage,
                    "glslang", "compile",
                    ["-D", "-V", "--target-env", target_env, "-S", glslang_stage,
                     "-e", "{entry_point}", "--hlsl-iomap", "--hlsl-offsets",
                     "--hlsl-dx-position-w", "-o", "{output}", "{input}"],
                )
            add(
                "glslang-%s-vulkan" % stage,
                "glslang",
                "compile",
                [
                    "-V",
                    "--target-env",
                    str(target_env),
                    "-S",
                    glslang_stage,
                    "-e",
                    "{entry_point}",
                    "-o",
                    "{output}",
                    "{input}",
                ],
            )
            if enable_opengl_spirv and stage in _OPENGL_GLSLANG_STAGES:
                add(
                    "glslang-%s-opengl" % stage,
                    "glslang",
                    "compile",
                    [
                        "-G",
                        "--target-env",
                        "opengl",
                        "-S",
                        glslang_stage,
                        "-e",
                        "{entry_point}",
                        "-o",
                        "{output}",
                        "{input}",
                    ],
                )
    add(
        "spirv-tools-dis",
        "spirv-dis",
        "decompile",
        ["{input}", "-o", "{output}"],
    )
    add(
        "spirv-tools-as",
        "spirv-as",
        "compile",
        ["{input}", "-o", "{output}"],
    )
    add(
        "spirv-tools-val",
        "spirv-val",
        "validate",
        ["--target-env", str(target_env), "{input}"],
    )
    if enable_opengl_spirv:
        add(
            "spirv-tools-val-opengl",
            "spirv-val",
            "validate",
            ["--target-env", _SPIRV_TOOLS_OPENGL_TARGET_ENV, "{input}"],
        )
    add(
        "dxil-spirv-transform",
        "dxil-spirv",
        "transform",
        [
            "{input}",
            "--output",
            "{output}",
            "--validate",
        ],
    )
    if binaries["renodx-decompiler"] is not None:
        command = binaries["renodx-decompiler"]
        adapter = (
            Path(__file__).resolve().parent
            / "adapters"
            / "renodx_decompiler_adapter.py"
        )
        python = packaged_adapter_host or Path(sys.executable).resolve()
        command_hash = _hash(command)
        fixed = _fixed_files("renodx-decompiler", command, extras)
        fixed[str(command)] = command_hash
        if packaged_adapter_host is None:
            if adapter.is_symlink() or not adapter.is_file():
                raise ToolConfigurationError("bundled RenoDX adapter is missing")
            fixed[str(adapter)] = _hash(adapter)
        tools["community-renodx-dxil-decompiler"] = {
            "executable": str(python),
            "executableSha256": _hash(python),
            "fixedFileHashes": fixed,
            "decompile": {
                "args": (
                    ["adapter-renodx"]
                    if packaged_adapter_host
                    else [str(adapter)]
                )
                + [
                    "--tool",
                    str(command),
                    "--tool-sha256",
                    command_hash,
                    "--input",
                    "{input}",
                    "--output",
                    "{output}",
                    "--workspace",
                    "{workspace_dir}",
                    "--raw-sha256",
                    "{raw_sha256}",
                    "--entry",
                    "{entry_point}",
                    "--stage",
                    "{stage}",
                ],
                "timeoutSeconds": 120,
            },
        }
    if binaries["hlsl-decompiler"] is not None:
        command = binaries["hlsl-decompiler"]
        hlsl_adapter = (
            Path(__file__).resolve().parent
            / "adapters"
            / "hlsl_decompiler_adapter.py"
        )
        assembly_adapter = (
            Path(__file__).resolve().parent
            / "adapters"
            / "dxbc_assembly_adapter.py"
        )
        python = packaged_adapter_host or Path(sys.executable).resolve()
        command_hash = _hash(command)
        fixed = _fixed_files("hlsl-decompiler", command, extras)
        fixed[str(command)] = command_hash
        if packaged_adapter_host is None:
            if hlsl_adapter.is_symlink() or not hlsl_adapter.is_file():
                raise ToolConfigurationError("bundled HLSLDecompiler adapter is missing")
            fixed[str(hlsl_adapter)] = _hash(hlsl_adapter)
        tools["community-hlsl-decompiler"] = {
            "executable": str(python),
            "executableSha256": _hash(python),
            "fixedFileHashes": fixed,
            "decompile": {
                "args": (["adapter-hlsl"] if packaged_adapter_host
                         else [str(hlsl_adapter)]) + [
                    "--entry-point",
                    "{entry_point}",
                    "--tool",
                    str(command),
                    "--input",
                    "{input}",
                    "--output",
                    "{output}",
                ],
                "timeoutSeconds": 120,
            },
        }
        assembly_fixed = _fixed_files("hlsl-decompiler", command, extras)
        assembly_fixed[str(command)] = command_hash
        if packaged_adapter_host is None:
            if assembly_adapter.is_symlink() or not assembly_adapter.is_file():
                raise ToolConfigurationError("bundled DXBC assembly adapter is missing")
            assembly_fixed[str(assembly_adapter)] = _hash(assembly_adapter)
        adapter_prefix = (
            ["adapter-dxbc-assembly"]
            if packaged_adapter_host
            else [str(assembly_adapter)]
        )
        common_assembly_arguments = adapter_prefix + [
            "--tool",
            str(command),
            "--tool-sha256",
            command_hash,
            "--input",
            "{input}",
            "--output",
            "{output}",
            "--baseline-sha256",
            "{raw_sha256}",
        ]
        tools["community-dxbc-assembly"] = {
            "executable": str(python),
            "executableSha256": _hash(python),
            "fixedFileHashes": assembly_fixed,
            "decompile": {
                "args": [adapter_prefix[0], "--mode", "disassemble"]
                + common_assembly_arguments[1:],
                "timeoutSeconds": 120,
            },
            "compile": {
                "args": [adapter_prefix[0], "--mode", "assemble"]
                + common_assembly_arguments[1:]
                + ["--baseline", "{baseline}"],
                "timeoutSeconds": 120,
            },
        }
    if not tools:
        raise ToolConfigurationError("the supplied paths did not produce any tool records")
    atomic_write_json(
        tool_path,
        {"schema": TOOLS_SCHEMA, "schemaVersion": TOOLS_VERSION, "tools": tools},
    )
    registry = ToolRegistry(tool_path)
    routes = []
    legacy_stages = [
        "vertex",
        "hull",
        "domain",
        "geometry",
        "fragment",
        "compute",
    ]
    dxil_stages = legacy_stages + ["task", "mesh"]
    vulkan_stages = dxil_stages + [
        "raygen",
        "intersection",
        "anyhit",
        "closesthit",
        "miss",
        "callable",
    ]
    if binaries["d3dcompiler"] is not None:
        routes.append(
            route_record(
                registry,
                route_id="d3dcompiler47-hlsl-dxbc-exact-profile",
                tool_id="d3dcompiler47-exact-profile",
                operation="compile",
                input_format="hlsl",
                output_format="dxbc",
                stages=legacy_stages,
                fidelity="native-compile",
                # Lower is preferred. Keep this distinct from FXC so automatic
                # routing remains unique when both implementations are present.
                priority=5,
                implementation="Microsoft D3DCompiler_47 D3DCompile",
                source_url=D3DCOMPILE_SOURCE,
                license_name="Microsoft DirectX redistribution terms",
                caveats=[
                    "Compile-only portable adapter; it does not recover HLSL or make DXBC disassembly reassemblable.",
                    "The adapter passes no macros/include handler and fixes both D3DCompile flag words to zero.",
                    "The target profile is read from the captured SHDR/SHEX token; output still requires complete ABI validation."
                ],
            )
        )
    if binaries["fxc"] is not None:
        routes.append(
            route_record(
                registry,
                route_id="fxc-hlsl-dxbc-exact-profile",
                tool_id="fxc-exact-profile",
                operation="compile",
                input_format="hlsl",
                output_format="dxbc",
                stages=legacy_stages,
                fidelity="native-compile",
                priority=10,
                implementation="Microsoft FXC",
                source_url=FXC_SOURCE,
                license_name="Microsoft Windows SDK license",
                caveats=[
                    "The profile is read from the captured SHDR/SHEX token; extraction failure is fatal."
                ],
            )
        )
        routes.append(
            route_record(
                registry,
                route_id="fxc-dxbc-native-disassembly",
                tool_id="fxc-exact-profile",
                operation="decompile",
                input_format="dxbc",
                output_format="dxbc-asm",
                stages=legacy_stages + ["library"],
                fidelity="ir-disassembly",
                priority=5,
                implementation="Microsoft FXC /dumpbin",
                source_url=FXC_SOURCE,
                license_name="Microsoft Windows SDK license",
                caveats=[
                    "Read-only explanatory assembly; Shader Model 4/5 DXBC assembly has no supported general Microsoft reassembler."
                ],
            )
        )
    if binaries["dxc"] is not None:
        routes.append(
            route_record(
                registry,
                route_id="dxc-hlsl-dxil-exact-profile",
                tool_id="dxc-exact-profile-dxil",
                operation="compile",
                input_format="hlsl",
                output_format="dxil",
                stages=dxil_stages,
                fidelity="native-compile",
                priority=10,
                implementation="Microsoft DXC",
                source_url=DXC_SOURCE,
                license_name=DXC_LICENSE,
                caveats=[
                    "The profile is read from DxilProgramHeader.ProgramVersion; this high-level HLSL route is intentionally not registered for Library/Ray exports."
                ],
            )
        )
        routes.append(
            route_record(
                registry,
                route_id="dxc-dxil-native-disassembly",
                tool_id="dxc-exact-profile-dxil",
                operation="decompile",
                input_format="dxil",
                output_format="dxil-asm",
                stages=["*"],
                fidelity="ir-disassembly",
                priority=5,
                implementation="Microsoft DXC -dumpbin",
                source_url=DXC_SOURCE,
                license_name=DXC_LICENSE,
                caveats=[
                    "DXIL/LLVM text is executable-editable only when paired with the pinned official IDxcAssembler route and the untouched round-trip probe passes; otherwise it remains explanatory only.",
                    "A successful assembly still does not sign, validate ABI compatibility, or authorize replay replacement."
                ],
            )
        )
        if "dxc-official-dxil-assembler" in tools:
            routes.append(
                route_record(
                    registry,
                    route_id="dxc-dxil-assemble-to-container",
                    tool_id="dxc-official-dxil-assembler",
                    operation="compile",
                    input_format="dxil-asm",
                    output_format="dxil",
                    stages=["*"],
                    fidelity="ir-assembly",
                    priority=1,
                    implementation="Microsoft DXC IDxcAssembler::AssembleToContainer",
                    source_url=DXC_SOURCE,
                    license_name=DXC_LICENSE,
                    caveats=[
                        "Official IDxcAssembler converts DXIL LLVM text to a DXIL container; it does not recover HLSL or original source structure.",
                        "The adapter accepts only a strict UTF-8 DXIL target-triple module and fixed job-local input/output paths through an absolute SHA-256-pinned dxcompiler.dll.",
                        "Assembly and container validation do not prove edited instruction semantics, resource compatibility, root-signature compatibility, or replay safety.",
                        "The candidate must pass fresh RenderDoc reflection, exact ABI comparison, and driver/replay acceptance before apply."
                    ],
                )
            )
        if target_env is not None:
            routes.append(
                route_record(
                    registry,
                    route_id="dxc-hlsl-spirv-exact-profile",
                    tool_id="dxc-exact-profile-spirv",
                    operation="compile",
                    input_format="hlsl",
                    output_format="spirv",
                    stages=legacy_stages,
                    fidelity="native-compile",
                    priority=20,
                    implementation="Microsoft DXC SPIR-V backend",
                    source_url=DXC_SOURCE,
                    license_name=DXC_LICENSE,
                    caveats=[
                        "The explicit trusted Vulkan target environment is %s; descriptor mapping options still affect ABI."
                        % target_env
                    ],
                )
            )
    for stage in _GLSLANG_STAGE_ARGUMENTS:
        if binaries["glslang"] is not None:
            if stage in legacy_stages:
                routes.append(route_record(
                    registry, route_id="glslang-hlsl-spirv-%s" % stage,
                    tool_id="glslang-hlsl-%s-vulkan" % stage, operation="compile",
                    input_format="hlsl", output_format="spirv", stages=[stage],
                    fidelity="native-compile", priority=10,
                    implementation="Khronos glslang HLSL frontend", source_url=GLSLANG_SOURCE,
                    target_apis=["vulkan"], license_name="See upstream LICENSE",
                    caveats=["HLSL authoring preserves explicit Vulkan locations and register spaces; the actual candidate must still pass reflection and replay validation."]))
            routes.append(
                route_record(
                    registry,
                    route_id="glslang-glsl-spirv-%s" % stage,
                    tool_id="glslang-%s-vulkan" % stage,
                    operation="compile",
                    input_format="glsl",
                    output_format="spirv",
                    stages=[stage],
                    fidelity="native-compile",
                    priority=10,
                    implementation="Khronos glslang",
                    source_url=GLSLANG_SOURCE,
                    license_name="See upstream LICENSE.txt",
                    caveats=[
                        "The generated example uses Vulkan semantics (-V) and explicit target environment %s."
                        % target_env
                    ],
                )
            )
            if enable_opengl_spirv and stage in _OPENGL_GLSLANG_STAGES:
                routes.append(
                    route_record(
                        registry,
                        route_id="glslang-glsl-opengl-spirv-%s" % stage,
                        tool_id="glslang-%s-opengl" % stage,
                        operation="compile",
                        input_format="glsl",
                        output_format="opengl-spirv",
                        stages=[stage],
                        fidelity="native-compile",
                        priority=10,
                        implementation="Khronos glslang",
                        source_url=GLSLANG_SOURCE,
                        license_name="See upstream LICENSE.txt",
                        caveats=[
                            "The generated operation uses -G and --target-env opengl; the OpenGL capture ABI still requires fresh replay reflection validation."
                        ],
                    )
                )
    if binaries["hlsl-decompiler"] is not None:
        routes.append(
            route_record(
                registry,
                route_id="community-hlsl-decompiler-dxbc-hlsl",
                tool_id="community-hlsl-decompiler",
                operation="decompile",
                input_format="dxbc",
                output_format="hlsl",
                stages=legacy_stages,
                fidelity="reconstructed-source",
                priority=80,
                implementation="3DMigoto cmd_Decompiler",
                source_url=HLSL_DECOMPILER_SOURCE,
                license_name="GPL-3.0 (3DMigoto; verify the supplied binary)",
                caveats=[
                    "Community Shader Model 4/5 reconstruction; it cannot recover original names, macros, comments, or source structure.",
                    "The upstream CLI source says its -V pass proves only that reconstructed HLSL compiles, not that behavior or ABI matches.",
                    "The operator must verify the provenance and license of the exact hash-pinned CMD_Decompiler binary."
                ],
            )
        )
        routes.append(
            route_record(
                registry,
                route_id="community-3dmigoto-dxbc-disassemble",
                tool_id="community-dxbc-assembly",
                operation="decompile",
                input_format="dxbc",
                output_format="dxbc-asm",
                stages=legacy_stages,
                fidelity="ir-disassembly",
                priority=1,
                implementation="3DMigoto 1.3.16 Flugan disassembler",
                source_url=DXBC_ASSEMBLER_1316_SOURCE,
                license_name="GPL-3.0 (3DMigoto; verify the supplied binary)",
                caveats=[
                    "Instruction-level DXBC assembly, not HLSL or recovered source.",
                    "The adapter invokes -d -V --stop-on-failure; upstream 1.3.16 writes .asm and validates the untouched disassembly by reassembling/comparing DXBC sections.",
                    "A successful untouched validation does not prove that later edited instructions are semantically equivalent."
                ],
            )
        )
        routes.append(
            route_record(
                registry,
                route_id="community-3dmigoto-dxbc-assemble",
                tool_id="community-dxbc-assembly",
                operation="compile",
                input_format="dxbc-asm",
                output_format="dxbc",
                stages=legacy_stages,
                fidelity="ir-assembly",
                priority=1,
                implementation="3DMigoto 1.3.16 Flugan assembler",
                source_url=DXBC_ASSEMBLER_1316_SOURCE,
                license_name="GPL-3.0 (3DMigoto; verify the supplied binary)",
                caveats=[
                    "The adapter invokes -a --copy-reflection with a job-local SHA-256-verified snapshot of the exact captured baseline.",
                    "Upstream 1.3.16 has no implemented -V validation branch after -a, so the adapter deliberately does not present -V as an assembly validator.",
                    "Assembly output is only an executable candidate; container/profile checks and fresh RenderDoc reflection ABI equality are still mandatory before apply.",
                    "ABI equality and successful GPU acceptance do not prove semantic equivalence."
                ],
            )
        )
    if binaries["renodx-decompiler"] is not None:
        routes.append(
            route_record(
                registry,
                route_id="community-renodx-dxil-hlsl",
                tool_id="community-renodx-dxil-decompiler",
                operation="decompile",
                input_format="dxil",
                output_format="hlsl",
                stages=dxil_stages,
                fidelity="reconstructed-source",
                priority=40,
                implementation="RenoDX decomp.exe",
                source_url=RENODX_DECOMPILER_SOURCE,
                license_name="MIT (RenoDX; verify the supplied binary)",
                caveats=[
                    "Third-party/community Shader Model 6 reconstruction; this is not a Microsoft or engine-vendor DXIL-to-HLSL recovery guarantee.",
                    "The fixed adapter invokes upstream src/decompiler/cli.cpp with only job-local input/output paths; optional flatten/control-flow flags are not enabled.",
                    "The source-patched decompiler resolves static DXIL input element/row/column metadata and emits every physical row; the adapter restores the captured entry name, requires a complete captured parameter list, and never synthesizes a missing row.",
                    "Dynamic input row/column operands, unsupported metadata/component/interpolation shapes, and non-flat or multi-row outputs fail closed so another registered route or native IR can be tried.",
                    "Recovered HLSL cannot restore original names, macros, comments, or source structure and is not proof of semantic equivalence.",
                    "The untouched result must still compile with the captured profile and pass fresh container, interface, resource-binding, constant-buffer, and pipeline ABI validation before apply."
                ],
            )
        )
    if binaries["dxil-spirv"] is not None:
        for source_format in ("dxil", "dxbc"):
            format_caveat = (
                "This is a Shader Model 6 DXIL input route."
                if source_format == "dxil"
                else
                "Legacy Shader Model 4/5 DXBC support requires a compatible dxil-spirv build containing the upstream dxbc-spirv integration; the executable name and registration hash alone do not prove that feature is present."
            )
            routes.append(
                route_record(
                    registry,
                    route_id="community-%s-spirv" % source_format,
                    tool_id="dxil-spirv-transform",
                    operation="transform",
                    input_format=source_format,
                    output_format="spirv",
                    stages=dxil_stages if source_format == "dxil" else legacy_stages,
                    fidelity="custom-transform",
                    priority=120,
                    implementation="dxil-spirv",
                    source_url=DXIL_SPIRV_SOURCE,
                    license_name="MIT",
                    caveats=[
                        "Community DirectX-to-Vulkan translation, not source recovery; descriptor/root-signature remapping may be required.",
                        format_caveat,
                        "The exact hash-pinned executable must run successfully, emit one structurally recognized SPIR-V module, and pass the untouched HLSL reverse-compile probe before editable source is published.",
                        "Structural SPIR-V recognition and reverse compilation do not prove equivalence to the DirectX pipeline; only a separately bound spirv-val route may claim SPIR-V validation.",
                        "The argument order is taken from upstream dxil_spirv.cpp print_help/parser; the exact local executable is SHA-256 pinned."
                    ],
                )
            )
    if binaries["spirv-cross"] is not None:
        routes.append(route_record(
            registry, route_id="spirv-cross-vulkan-hlsl", tool_id="spirv-cross-vulkan-hlsl",
            operation="decompile", input_format="spirv", output_format="hlsl",
            stages=legacy_stages, fidelity="reconstructed-source", priority=90,
            implementation="Khronos SPIRV-Cross Vulkan HLSL authoring", source_url=SPIRV_CROSS_SOURCE,
            target_apis=["vulkan"], license_name="See upstream LICENSE",
            caveats=["Uses SM 6 authoring syntax, not a captured DirectX profile. Explicit Vulkan interface locations are retained; generated source still requires candidate validation."]))
        routes.append(
            route_record(
                registry,
                route_id="spirv-cross-spirv-hlsl",
                tool_id="spirv-cross-hlsl",
                operation="decompile",
                input_format="spirv",
                output_format="hlsl",
                stages=dxil_stages,
                fidelity="reconstructed-source",
                priority=100,
                implementation="Khronos SPIRV-Cross",
                source_url=SPIRV_CROSS_SOURCE,
                license_name="See upstream LICENSE",
                caveats=[
                    "The pinned adapter selects the physical entry/stage and derives --shader-model from the exact DirectX target profile; reconstruction still cannot recover original names or structure."
                ],
            )
        )
        if enable_opengl_spirv:
            routes.append(
                route_record(
                    registry,
                    route_id="spirv-cross-opengl-spirv-glsl",
                    tool_id="spirv-cross-opengl-glsl",
                    operation="decompile",
                    input_format="opengl-spirv",
                    output_format="glsl",
                    stages=legacy_stages,
                    fidelity="reconstructed-source",
                    priority=100,
                    implementation="Khronos SPIRV-Cross",
                    source_url=SPIRV_CROSS_SOURCE,
                    license_name="See upstream LICENSE",
                    caveats=[
                        "The adapter requires the physical OpenGL API and deliberately omits --vulkan-semantics; original names and source structure remain unrecoverable."
                    ],
                )
            )
        routes.append(
            route_record(
                registry,
                route_id="spirv-cross-spirv-glsl",
                tool_id="spirv-cross-vulkan-glsl",
                operation="decompile",
                input_format="spirv",
                output_format="glsl",
                stages=vulkan_stages,
                fidelity="reconstructed-source",
                priority=100,
                implementation="Khronos SPIRV-Cross",
                source_url=SPIRV_CROSS_SOURCE,
                license_name="See upstream LICENSE",
                caveats=[
                    "The pinned adapter selects the physical entry/stage and requires Vulkan API; reconstructed GLSL still cannot recover original names or source structure."
                ],
            )
        )
    if binaries["spirv-dis"] is not None:
        routes.append(
            route_record(
                registry,
                route_id="spirv-tools-disassemble",
                tool_id="spirv-tools-dis",
                operation="decompile",
                input_format="spirv",
                output_format="spirv-asm",
                stages=["*"],
                fidelity="ir-disassembly",
                priority=5,
                implementation="Khronos SPIRV-Tools",
                source_url=SPIRV_TOOLS_SOURCE,
                license_name="Apache-2.0",
            )
        )
        if enable_opengl_spirv:
            routes.append(
                route_record(
                    registry,
                    route_id="spirv-tools-disassemble-opengl",
                    tool_id="spirv-tools-dis",
                    operation="decompile",
                    input_format="opengl-spirv",
                    output_format="opengl-spirv-asm",
                    stages=["*"],
                    fidelity="ir-disassembly",
                    priority=5,
                    implementation="Khronos SPIRV-Tools",
                    source_url=SPIRV_TOOLS_SOURCE,
                    license_name="Apache-2.0",
                    caveats=[
                        "The text syntax is physically shared with Vulkan SPIR-V; the OpenGL capture encoding remains a separate semantic fact."
                    ],
                )
            )
    if binaries["spirv-as"] is not None:
        routes.append(
            route_record(
                registry,
                route_id="spirv-tools-assemble",
                tool_id="spirv-tools-as",
                operation="compile",
                input_format="spirv-asm",
                output_format="spirv",
                stages=["*"],
                fidelity="ir-assembly",
                priority=5,
                implementation="Khronos SPIRV-Tools",
                source_url=SPIRV_TOOLS_SOURCE,
                license_name="Apache-2.0",
                caveats=["Assembly success is not SPIR-V validation; run spirv-val separately."],
            )
        )
        if enable_opengl_spirv:
            routes.append(
                route_record(
                    registry,
                    route_id="spirv-tools-assemble-opengl",
                    tool_id="spirv-tools-as",
                    operation="compile",
                    input_format="opengl-spirv-asm",
                    output_format="opengl-spirv",
                    stages=["*"],
                    fidelity="ir-assembly",
                    priority=5,
                    implementation="Khronos SPIRV-Tools",
                    source_url=SPIRV_TOOLS_SOURCE,
                    license_name="Apache-2.0",
                    caveats=[
                        "Assembly success is not OpenGL-environment validation; run spirv-val --target-env opengl4.5 separately."
                    ],
                )
            )
    validators = []
    if binaries["spirv-val"] is not None:
        validators.append(
            validator_record(
                registry,
                validator_id="spirv-tools-validate",
                tool_id="spirv-tools-val",
                input_format="spirv",
                stages=["*"],
                priority=5,
                implementation="Khronos SPIRV-Tools spirv-val",
                source_url=SPIRV_TOOLS_SOURCE,
                license_name="Apache-2.0",
                caveats=[
                    "The trusted config fixes --target-env=%s; it is never inferred from the SPIR-V header or a generic Vulkan API label."
                    % target_env,
                    "Validator coverage is intentionally one-sided/incomplete according to upstream; replay ABI validation remains separate."
                ],
            )
        )
        if enable_opengl_spirv:
            validators.append(
                validator_record(
                    registry,
                    validator_id="spirv-tools-validate-opengl",
                    tool_id="spirv-tools-val-opengl",
                    input_format="opengl-spirv",
                    stages=["*"],
                    priority=5,
                    implementation="Khronos SPIRV-Tools spirv-val",
                    source_url=SPIRV_TOOLS_SOURCE,
                    license_name="Apache-2.0",
                    caveats=[
                        "The trusted operation fixes --target-env=opengl4.5, the newest OpenGL environment exposed by SPIRV-Tools; validator success does not prove OpenGL program-interface or replay compatibility."
                    ],
                )
            )
    atomic_write_json(
        route_path, toolchain_document(tool_path, routes, validators)
    )
    # Loading both files is the final generator self-check.
    from .toolchains import ToolchainCatalog

    catalog = ToolchainCatalog(tool_path, route_path)
    return {
        "toolConfigPath": str(tool_path),
        "routeConfigPath": str(route_path),
        "toolCount": len(tools),
        "routeCount": len(catalog.routes()),
        "validatorCount": len(catalog.validators()),
        "toolRegistrySha256": catalog.tool_registry_sha256,
        "binaryPathsWereDiscovered": False,
        "downloadsPerformed": False,
        "spirvTargetEnv": target_env,
        "openGlSpirvEnabled": enable_opengl_spirv,
    }


def _parse_pin(values: Sequence[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for value in values:
        if "=" not in value:
            raise ToolConfigurationError("--pin must use PROVIDER=ABSOLUTE_PATH")
        provider, path = value.split("=", 1)
        result.setdefault(provider, []).append(path)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate hash-pinned shader tool and capability-route configs"
    )
    parser.add_argument("--tools-output", required=True)
    parser.add_argument("--routes-output", required=True)
    parser.add_argument("--dxc")
    parser.add_argument("--fxc")
    parser.add_argument("--d3dcompiler")
    parser.add_argument("--spirv-cross")
    parser.add_argument("--glslang")
    parser.add_argument("--spirv-dis")
    parser.add_argument("--spirv-as")
    parser.add_argument("--spirv-val")
    parser.add_argument(
        "--spirv-target-env",
        help=(
            "explicit Vulkan SPIR-V environment for generated compilers/validator; "
            "no default is inferred"
        ),
    )
    parser.add_argument("--hlsl-decompiler")
    parser.add_argument("--dxil-spirv")
    parser.add_argument("--renodx-decompiler")
    parser.add_argument(
        "--adapter-host",
        help="trusted packaged Bread shader-tool executable that dispatches fixed adapter subcommands",
    )
    parser.add_argument(
        "--enable-opengl-spirv",
        action="store_true",
        help=(
            "generate distinct OpenGLSPIRV/OpenGLSPIRVAsm routes; never treat "
            "them as Vulkan SPIR-V"
        ),
    )
    parser.add_argument(
        "--pin",
        action="append",
        default=[],
        metavar="PROVIDER=ABSOLUTE_PATH",
        help="hash-pin an additional DLL/data file used by a provider",
    )
    parser.add_argument("--force", action="store_true")
    arguments = parser.parse_args(argv)
    result = generate_official_toolchain_configs(
        arguments.tools_output,
        arguments.routes_output,
        dxc=arguments.dxc,
        fxc=arguments.fxc,
        d3dcompiler=arguments.d3dcompiler,
        spirv_cross=arguments.spirv_cross,
        glslang=arguments.glslang,
        spirv_dis=arguments.spirv_dis,
        spirv_as=arguments.spirv_as,
        spirv_val=arguments.spirv_val,
        spirv_target_env=arguments.spirv_target_env,
        hlsl_decompiler=arguments.hlsl_decompiler,
        dxil_spirv=arguments.dxil_spirv,
        renodx_decompiler=arguments.renodx_decompiler,
        adapter_host=arguments.adapter_host,
        enable_opengl_spirv=arguments.enable_opengl_spirv,
        extra_fixed_files=_parse_pin(arguments.pin),
        overwrite=arguments.force,
    )
    for key in sorted(result):
        print("%s: %s" % (key, result[key]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
