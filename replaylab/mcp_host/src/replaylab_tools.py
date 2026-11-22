"""Process-owner shader tool discovery for a portable ReplayLab host.

Only fixed local paths are discovered at host startup. MCP callers cannot
select executables, commands, download URLs, or rewrite the generated registry.
The resulting files remain hash-pinned by ``toolchain_config``.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Mapping

from .shader_workspace.io import atomic_write_bytes, atomic_write_json, read_json_object
from .shader_workspace.toolchain_config import generate_official_toolchain_configs


TOOLS_ENV = "RENDERDOC_ULTRA_SHADER_TOOLS_CONFIG"
ROUTES_ENV = "RENDERDOC_ULTRA_SHADER_TOOLCHAINS_CONFIG"
VERSION_PART = re.compile(r"^(\d+)(?:\.(\d+))*(?:[^0-9].*)?$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DISCOVERY_SCHEMA = "replaylab-shader-tool-discovery/v2"
MANAGED_CONFIG_SCHEMA = "replaylab-managed-shader-config/v1"
MAX_STATUS_BYTES = 1024 * 1024


def _config_root(environment: Mapping[str, str]) -> Path:
    configured = str(environment.get("REPLAYLAB_CONFIG_DIR", "") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    local = str(environment.get("LOCALAPPDATA", "") or "").strip()
    if local:
        return (Path(local).expanduser() / "ReplayLab" / "config").resolve()
    return (Path.home() / ".config" / "ReplayLab").resolve()


def _regular(path: Path | None) -> Path | None:
    if path is None:
        return None
    expanded = path.expanduser()
    # Check the caller-visible path before resolve(), which follows the final
    # link and would otherwise make a symlink look like a regular file.
    if expanded.is_symlink():
        return None
    try:
        resolved = expanded.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved.is_file():
        return None
    return resolved


def _windows_sdk_candidates(environment: Mapping[str, str], name: str) -> list[Path]:
    roots = []
    for variable in ("ProgramFiles(x86)", "ProgramFiles"):
        raw = str(environment.get(variable, "") or "").strip()
        if raw:
            roots.append(Path(raw) / "Windows Kits" / "10" / "bin")
    result: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        result.extend(root.glob("*/x64/%s.exe" % name))
        result.extend(root.glob("x64/%s.exe" % name))
    return [path for path in (_regular(value) for value in result) if path]


def _version_key(path: Path) -> tuple[int, ...]:
    parts = []
    for token in path.parts:
        if re.fullmatch(r"\d+(?:\.\d+)+", token):
            parts = [int(value) for value in token.split(".")]
    return tuple(parts or [0])


def _discover_direct(
    executable_dir: Path, environment: Mapping[str, str], file_name: str, sdk_name: str = ""
) -> Path | None:
    bundled = _regular(executable_dir / "tools" / file_name)
    if bundled:
        return bundled
    if os.name == "nt" and sdk_name:
        candidates = _windows_sdk_candidates(environment, sdk_name)
        if candidates:
            return max(candidates, key=_version_key)
    return None


def discover(executable_dir: Path, environment: Mapping[str, str]) -> dict[str, Path | None]:
    tools = executable_dir / "tools"
    community = tools / "community-hlsl-decompiler"
    community_executable = _regular(community / "cmd_Decompiler.exe")
    community_dependency = _regular(community / "D3DCompiler_46.dll")
    # The upstream 1.3.16 executable imports D3DCOMPILER_46.dll by name.  Never
    # register it unless that exact side-by-side dependency is present; falling
    # back to an arbitrary System32/working-directory DLL would defeat pinning.
    if community_executable is None or community_dependency is None:
        community_executable = None
        community_dependency = None
    renodx_decompiler = _regular(tools / "decomp.exe")
    renodx_dxcompiler = _regular(tools / "dxcompiler.dll")
    # RenoDX's SM6 decompiler loads dxcompiler.dll at runtime.  Registering a
    # lone executable would allow an unpinned DLL search or guarantee a runtime
    # failure, so startup discovery treats the provider as unavailable unless
    # the exact side-by-side dependency can also be pinned by the generator.
    if renodx_decompiler is None or renodx_dxcompiler is None:
        renodx_decompiler = None
    dxil_spirv = _regular(tools / "dxil-spirv.exe")
    dxil_spirv_dependency = _regular(tools / "dxil-spirv-c-shared.dll")
    # The current upstream Windows CMake build links the CLI against this
    # side-by-side shared library. Do not register a provider that will die at
    # process start with STATUS_DLL_NOT_FOUND. A deliberately static custom
    # build can still be supplied through an explicit process-owner registry.
    if os.name == "nt" and (
        dxil_spirv is None or dxil_spirv_dependency is None
    ):
        dxil_spirv = None
        dxil_spirv_dependency = None
    return {
        "dxc": _discover_direct(executable_dir, environment, "dxc.exe", "dxc"),
        "fxc": _discover_direct(executable_dir, environment, "fxc.exe", "fxc"),
        # Unlike fxc.exe/dxc.exe, never adopt a workstation SDK/System32 DLL.
        # The in-process compiler is trusted only when explicitly packaged.
        "d3dcompiler": _regular(tools / "d3dcompiler_47.dll"),
        "spirv_cross": _regular(tools / "spirv-cross.exe"),
        # Khronos renamed the standalone executable to glslang in 12.3.0.
        # Accept both fixed bundled names; never search PATH for either.
        "glslang": (
            _regular(tools / "glslangValidator.exe")
            or _regular(tools / "glslang.exe")
        ),
        "spirv_dis": _regular(tools / "spirv-dis.exe"),
        "spirv_as": _regular(tools / "spirv-as.exe"),
        "spirv_val": _regular(tools / "spirv-val.exe"),
        "hlsl_decompiler": community_executable,
        "hlsl_decompiler_dependency": community_dependency,
        "dxil_spirv": dxil_spirv,
        "dxil_spirv_dependency": dxil_spirv_dependency,
        "renodx_decompiler": renodx_decompiler,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _same_path(left: object, right: Path) -> bool:
    try:
        candidate = Path(str(left)).expanduser().resolve(strict=False)
        expected = right.expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return os.path.normcase(str(candidate)) == os.path.normcase(str(expected))


def _managed_config_evidence(
    status_path: Path, tool_path: Path, route_path: Path
) -> dict[str, Any]:
    result: dict[str, Any] = {"managed": False, "record": None}
    if status_path.is_symlink():
        result["reason"] = "The previous status path is a symlink"
        return result
    try:
        status = read_json_object(status_path, MAX_STATUS_BYTES, "Bread shader status")
    except (OSError, ValueError) as exc:
        result["reason"] = "No valid Bread ownership status is available: %s" % exc
        return result
    if status.get("schema") != DISCOVERY_SCHEMA:
        result["reason"] = (
            "The previous status predates managed dual-hash ownership or uses an unknown schema"
        )
        return result
    record = status.get("managedConfig")
    if not isinstance(record, dict):
        result["reason"] = "The previous status does not claim a managed config pair"
        return result
    expected_fields = {
        "schema",
        "owner",
        "toolConfigPath",
        "routeConfigPath",
        "toolConfigSha256",
        "routeConfigSha256",
    }
    if set(record) != expected_fields:
        result["reason"] = "The managed config ownership record has unexpected fields"
        return result
    if record.get("schema") != MANAGED_CONFIG_SCHEMA or record.get("owner") != "ReplayLab":
        result["reason"] = "The managed config ownership record has an unexpected compatibility owner id"
        return result
    if not _same_path(record.get("toolConfigPath"), tool_path) or not _same_path(
        record.get("routeConfigPath"), route_path
    ):
        result["reason"] = "The managed config ownership record names different paths"
        return result
    tool_hash = str(record.get("toolConfigSha256", "")).lower()
    route_hash = str(record.get("routeConfigSha256", "")).lower()
    if not SHA256_PATTERN.fullmatch(tool_hash) or not SHA256_PATTERN.fullmatch(route_hash):
        result["reason"] = "The managed config ownership record contains invalid hashes"
        return result
    result["record"] = dict(record)
    if tool_path.is_symlink() or route_path.is_symlink():
        result["reason"] = "A managed config path became a symlink"
        return result
    try:
        current_tool_hash = _sha256_file(tool_path)
        current_route_hash = _sha256_file(route_path)
    except OSError as exc:
        result["reason"] = "A managed config cannot be hashed: %s" % exc
        return result
    if current_tool_hash != tool_hash or current_route_hash != route_hash:
        result["reason"] = (
            "A default config differs from the exact hashes in the last Bread status"
        )
        return result
    result["managed"] = True
    result["reason"] = "Both current config hashes match the last Bread status"
    return result


def _restore_file(path: Path, previous: bytes | None) -> None:
    if previous is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    else:
        atomic_write_bytes(path, previous)


def _commit_managed_generation(
    tool_path: Path,
    route_path: Path,
    status_path: Path,
    tool_bytes: bytes,
    route_bytes: bytes,
    report: Mapping[str, Any],
) -> None:
    previous_tool = tool_path.read_bytes() if tool_path.exists() else None
    previous_route = route_path.read_bytes() if route_path.exists() else None
    try:
        atomic_write_bytes(tool_path, tool_bytes)
        atomic_write_bytes(route_path, route_bytes)
        atomic_write_json(status_path, dict(report))
    except BaseException:
        rollback_errors = []
        for path, previous in (
            (tool_path, previous_tool),
            (route_path, previous_route),
        ):
            try:
                _restore_file(path, previous)
            except BaseException as rollback_exc:
                rollback_errors.append("%s: %s" % (path, rollback_exc))
        if rollback_errors:
            raise RuntimeError(
                "Shader config commit failed and rollback was incomplete: %s"
                % "; ".join(rollback_errors)
            )
        raise


def _generation_kwargs(
    found: Mapping[str, Path | None], adapter_host: Path | None = None
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        key: value
        for key, value in found.items()
        if key not in {"hlsl_decompiler_dependency", "dxil_spirv_dependency"}
    }
    dependency = found.get("hlsl_decompiler_dependency")
    if found.get("hlsl_decompiler") is not None and dependency is not None:
        kwargs["extra_fixed_files"] = {"hlsl-decompiler": [dependency]}
    dxil_spirv_dependency = found.get("dxil_spirv_dependency")
    if found.get("dxil_spirv") is not None and dxil_spirv_dependency is not None:
        kwargs.setdefault("extra_fixed_files", {})["dxil-spirv"] = [
            dxil_spirv_dependency
        ]
    # All entry points use the independently built shader runner. Neither the
    # calling Host executable nor unrelated Host code is compiler identity.
    kwargs["adapter_host"] = adapter_host
    # Vulkan environment is deliberately explicit. Bundled SPIR-V tools
    # default to Vulkan 1.2 for this distribution, never from capture data.
    kwargs["spirv_target_env"] = "vulkan1.2" if (
        found["glslang"] or found["spirv_val"]
    ) else None
    # OpenGL and Vulkan SPIR-V share the same physical word stream, but they
    # are different capture-derived semantic environments. Enable the
    # separately-labelled OpenGL routes whenever a fixed SPIR-V provider is
    # present; individual routes are still emitted only for the exact tools
    # that were discovered and hash-pinned.
    kwargs["enable_opengl_spirv"] = any(
        found.get(name) is not None
        for name in ("spirv_cross", "glslang", "spirv_dis", "spirv_as", "spirv_val")
    )
    return kwargs


def _stage_generation(
    config: Path, found: Mapping[str, Path | None], adapter_host: Path | None = None
) -> tuple[bytes, bytes, dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix=".shader-config-stage-", dir=str(config)) as raw:
        staging = Path(raw)
        staged_tool = staging / "shader-tools.v1.json"
        staged_route = staging / "shader-toolchains.v1.json"
        generated = generate_official_toolchain_configs(
            staged_tool,
            staged_route,
            overwrite=False,
            **_generation_kwargs(found, adapter_host),
        )
        return staged_tool.read_bytes(), staged_route.read_bytes(), generated


def _normalized_operation_for_relocation(operation: Any) -> tuple[Any, ...] | None:
    """Return one path-independent, still hash-bound operation identity.

    Managed startup discovery writes absolute paths because the runner must never
    resolve an executable through ``PATH``.  Moving an otherwise byte-identical
    ReplayLab bundle therefore changes the JSON/config digest even though the
    fixed command is unchanged.  This signature removes only those absolute
    path spellings.  Executable/fixed-file names and SHA-256 values, argument
    templates, timeout, placeholders, and side-by-side directory relationships
    remain part of the comparison.

    Duplicate fixed-file name/hash identities are deliberately rejected instead
    of guessing which relocated file an argument refers to.
    """

    executable_identity = (
        "executable",
        operation.executable.name.casefold(),
        operation.executable_sha256,
    )
    fixed_identities = [
        ("fixed", path.name.casefold(), digest)
        for path, digest in operation.fixed_file_hashes
    ]
    if len(set(fixed_identities)) != len(fixed_identities):
        return None

    path_tokens: dict[str, str] = {
        str(operation.executable): "<%s:%s:%s>" % executable_identity
    }
    parent_groups: dict[str, list[str]] = {
        os.path.normcase(str(operation.executable.parent)): [
            path_tokens[str(operation.executable)]
        ]
    }
    for (path, unused_digest), identity in zip(
        operation.fixed_file_hashes, fixed_identities, strict=True
    ):
        token = "<%s:%s:%s>" % identity
        path_tokens[str(path)] = token
        parent_groups.setdefault(os.path.normcase(str(path.parent)), []).append(token)

    # Longest first prevents a directory-prefix spelling from touching a longer
    # literal path. Generated configs use the exact resolved spelling, while
    # IGNORECASE preserves Windows path semantics.
    replacements = sorted(
        path_tokens.items(), key=lambda item: len(item[0]), reverse=True
    )
    flags = re.IGNORECASE if os.name == "nt" else 0

    def normalize_argument(value: str) -> str:
        result = value
        for raw_path, token in replacements:
            result = re.sub(
                re.escape(raw_path), lambda unused: token, result, flags=flags
            )
        return result

    directory_relationships = tuple(
        sorted(tuple(sorted(tokens)) for tokens in parent_groups.values())
    )
    return (
        operation.tool_id,
        operation.operation,
        executable_identity,
        tuple(sorted(fixed_identities)),
        directory_relationships,
        tuple(normalize_argument(argument) for argument in operation.arguments),
        tuple(sorted(operation.placeholders)),
        float(operation.timeout_seconds),
    )


def _catalog_capabilities_for_relocation(
    catalog: Any,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Canonicalise route metadata while excluding only path-derived digests."""

    def canonical(values: Any) -> tuple[str, ...]:
        records = []
        for value in values:
            record = value.as_dict()
            record.pop("toolConfigDigest", None)
            records.append(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        return tuple(sorted(records))

    return canonical(catalog.routes()), canonical(catalog.validators())


def _managed_configs_relocation_evidence(
    config: Path,
    current_tool_path: Path,
    current_route_path: Path,
    staged_tool: bytes,
    staged_route: bytes,
    expected_current: Mapping[str, Any],
) -> dict[str, Any]:
    """Prove whether staged configs differ only by local absolute paths.

    Both catalogs are parsed through the ordinary strict loaders, so every
    referenced executable and fixed file is freshly hashed before equivalence is
    considered. Any malformed/tampered/missing file or behavioral metadata
    change returns a non-equivalent result and lets the normal managed refresh
    path run.
    """

    result: dict[str, Any] = {
        "equivalent": False,
        "operationCount": 0,
        "routeCount": 0,
        "validatorCount": 0,
    }
    try:
        from .shader_workspace.toolchains import ToolchainCatalog

        with tempfile.TemporaryDirectory(
            prefix=".shader-relocation-compare-", dir=str(config)
        ) as raw:
            staging = Path(raw)
            staged_tool_path = staging / "shader-tools.v1.json"
            staged_route_path = staging / "shader-toolchains.v1.json"
            staged_tool_path.write_bytes(staged_tool)
            staged_route_path.write_bytes(staged_route)
            current_catalog = ToolchainCatalog(current_tool_path, current_route_path)
            staged_catalog = ToolchainCatalog(staged_tool_path, staged_route_path)

            current_operations = tuple(
                _normalized_operation_for_relocation(operation)
                for operation in current_catalog.registry.operations()
            )
            staged_operations = tuple(
                _normalized_operation_for_relocation(operation)
                for operation in staged_catalog.registry.operations()
            )
            result.update(
                {
                    "operationCount": len(current_operations),
                    "routeCount": len(current_catalog.routes()),
                    "validatorCount": len(current_catalog.validators()),
                }
            )
            if None in current_operations or None in staged_operations:
                result["reason"] = (
                    "duplicate fixed-file name/hash identities make relocation ambiguous"
                )
                return result
            if current_operations != staged_operations:
                result["reason"] = (
                    "an executable hash, fixed-file hash/name/layout, command template, "
                    "timeout, placeholder, or operation set changed"
                )
                return result
            if _catalog_capabilities_for_relocation(
                current_catalog
            ) != _catalog_capabilities_for_relocation(staged_catalog):
                result["reason"] = "route or validator capabilities changed"
                return result

            # Do not let a concurrent config edit become newly legitimised by
            # the status write below. The last ownership record must still bind
            # the exact current bytes after both strict catalogs were loaded.
            if (
                _sha256_file(current_tool_path)
                != expected_current.get("toolConfigSha256")
                or _sha256_file(current_route_path)
                != expected_current.get("routeConfigSha256")
            ):
                result["reason"] = "managed config bytes changed during relocation proof"
                return result
    except Exception as exc:
        result["reason"] = "%s: %s" % (type(exc).__name__, exc)
        return result

    result["equivalent"] = True
    result["reason"] = (
        "all strict operation and capability identities match after removing only "
        "absolute path spellings"
    )
    return result


def _automatic_config_directory(
    root: Path, executable_dir: Path, adapter_host: Path | None
) -> Path:
    """Keep automatic discovery local to its installation and adapter owner.

    The old shared default pair may be a hand-maintained user configuration.
    Preserve that choice unless the exact dual-hash ownership record proves
    that ReplayLab generated both files. Proven legacy managed files are left
    untouched for existing consumers, while new discovery uses its own scope.
    """

    legacy_tools = root / "shader-tools.v1.json"
    legacy_routes = root / "shader-toolchains.v1.json"
    if any(path.exists() or path.is_symlink() for path in (legacy_tools, legacy_routes)):
        evidence = _managed_config_evidence(
            root / "shader-toolchain-status.json", legacy_tools, legacy_routes
        )
        if not evidence["managed"]:
            return root
    owner = (
        ["bundle"]
        if adapter_host is not None
        else [
            "source",
            os.path.normcase(str(Path(sys.executable).resolve())),
            os.path.normcase(str(Path(__file__).resolve().parent)),
        ]
    )
    identity = [os.path.normcase(str(executable_dir)), *owner]
    key = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return root / "shader-toolchains" / key


@contextmanager
def _configuration_lock(config: Path):
    """Serialize startup ownership checks and dual-file commits per scope."""

    lock_path = config / ".shader-toolchain-config.lock"
    if lock_path.is_symlink():
        raise RuntimeError("Shader configuration lock must not be a symlink")
    with lock_path.open("a+b") as stream:
        # Windows byte-range locks require one byte, even in a new lock file.
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + 60.0
        while True:
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Timed out waiting for shader configuration startup lock")
                time.sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def configure_environment(
    executable_dir: Path | None = None,
    environment: dict[str, str] | None = None,
) -> dict[str, Any]:
    env = os.environ if environment is None else environment
    owner_tools = str(env.get(TOOLS_ENV, "") or "").strip()
    owner_routes = str(env.get(ROUTES_ENV, "") or "").strip()
    if bool(owner_tools) != bool(owner_routes):
        raise RuntimeError(
            "%s and %s must be configured together" % (TOOLS_ENV, ROUTES_ENV)
        )
    if owner_tools and owner_routes:
        return {
            "source": "process-owner",
            "toolConfigPath": str(Path(owner_tools).expanduser().resolve()),
            "routeConfigPath": str(Path(owner_routes).expanduser().resolve()),
            "generated": False,
        }

    base = (
        executable_dir.resolve()
        if executable_dir is not None
        else Path(sys.executable).resolve().parent
    )
    # A source diagnostic explicitly targeting a packaged installation also
    # uses that installation's fixed adapter. Frozen entry points must never
    # silently fall back to another executable or to temporary Python files.
    adapter_host = _regular(base / "ReplayLabShaderTools.exe")
    if adapter_host is None and (
        getattr(sys, "frozen", False) or _regular(base / "BreadMCP.exe") is not None
    ):
        raise RuntimeError(
            "The packaged shader runner is missing: %s; deploy the complete runtime before using shader tools"
            % (base / "ReplayLabShaderTools.exe")
        )
    config = _automatic_config_directory(_config_root(env), base, adapter_host)
    # Keep capture-only startup usable if discovery fails, but make later
    # shader operations resolve this intended scope instead of silently using
    # a different installation's legacy global pair after a lock/write error.
    env[TOOLS_ENV] = str(config / "shader-tools.v1.json")
    env[ROUTES_ENV] = str(config / "shader-toolchains.v1.json")
    config.mkdir(parents=True, exist_ok=True)
    with _configuration_lock(config):
        return _configure_automatic_environment(base, config, adapter_host, env)


def _configure_automatic_environment(
    base: Path, config: Path, adapter_host: Path | None, env: dict[str, str]
) -> dict[str, Any]:
    tool_path = config / "shader-tools.v1.json"
    route_path = config / "shader-toolchains.v1.json"
    status_path = config / "shader-toolchain-status.json"
    env[TOOLS_ENV] = str(tool_path)
    env[ROUTES_ENV] = str(route_path)

    found = discover(base, env)
    report: dict[str, Any] = {
        "schema": DISCOVERY_SCHEMA,
        "generatedUnixMs": int(time.time() * 1000),
        "executableDirectory": str(base),
        "adapterHost": str(adapter_host) if adapter_host is not None else None,
        "toolConfigPath": str(tool_path),
        "routeConfigPath": str(route_path),
        "tools": {key: (str(value) if value else None) for key, value in found.items()},
        "binaryPathsWereDiscovered": any(found.values()),
        "generated": False,
        "downloadsPerformed": False,
        "notes": [
            "FXC/DXC are discovered only from bundled files or explicit Windows SDK roots; arbitrary PATH entries are never trusted or executed.",
            "D3DCompiler_47 is discovered only as ReplayLab/tools/d3dcompiler_47.dll and is used by a fixed compile-only adapter with no include handler or caller flags.",
            "Community/SPIR-V tools are enabled only when bundled under ReplayLab/tools or supplied through an explicit process-owner registry.",
            "The GPL community HLSL decompiler is enabled only when its compliant distribution is present under tools/community-hlsl-decompiler.",
            "The community RenoDX Shader Model 6 decompiler is enabled only for the fixed ReplayLab/tools/decomp.exe path with a side-by-side dxcompiler.dll; both files are SHA-256 pinned and no executable is obtained from PATH or an asset tree.",
            "Automatic registries are isolated by installation directory and adapter owner; bundled entry points use the independently built ReplayLabShaderTools.exe. Host-only upgrades do not change shader tool identity. Explicit or user-modified default configurations are preserved.",
            "Within one installation, path-only provider relocation preserves the existing managed catalog while every pinned file remains integrity-valid; missing bytes or changed selected commands/capabilities require explicit workspace migration.",
        ],
    }
    tool_present = tool_path.exists() or tool_path.is_symlink()
    route_present = route_path.exists() or route_path.is_symlink()
    if tool_present and route_present:
        evidence = _managed_config_evidence(status_path, tool_path, route_path)
        if evidence.get("record") is not None:
            # Preserve the last trusted hashes in a blocked status report. If
            # the user restores those exact bytes later, ownership can be
            # proven again without treating the modified files as ours.
            report["managedConfig"] = evidence["record"]
        report["ownershipEvidence"] = str(evidence.get("reason", "unavailable"))
        if not evidence["managed"]:
            report["source"] = "existing-unmanaged-config"
            report["refreshBlockedReason"] = report["ownershipEvidence"]
            report["error"] = (
                "Existing default shader configs were not replaced because Bread "
                "cannot prove they are its untouched managed pair."
            )
            atomic_write_json(status_path, report)
        elif not any(found.values()):
            report["source"] = "managed-config-no-tools-found"
            report["error"] = (
                "No shader compiler/decompiler is currently discoverable; the proven "
                "managed config pair was preserved."
            )
            atomic_write_json(status_path, report)
        else:
            staged_tool, staged_route, generated = _stage_generation(config, found, adapter_host)
            staged_tool_hash = hashlib.sha256(staged_tool).hexdigest()
            staged_route_hash = hashlib.sha256(staged_route).hexdigest()
            current = evidence["record"]
            report.update(generated)
            # The generic config generator receives explicit, already-resolved
            # paths and therefore reports that it did not perform discovery.
            # At this host layer the paths were discovered immediately above;
            # keep that process-owner evidence instead of letting the generic
            # diagnostic overwrite it with a misleading false value.
            report["binaryPathsWereDiscovered"] = any(found.values())
            report["toolConfigPath"] = str(tool_path)
            report["routeConfigPath"] = str(route_path)
            report["managedConfig"] = {
                "schema": MANAGED_CONFIG_SCHEMA,
                "owner": "ReplayLab",
                "toolConfigPath": str(tool_path.resolve()),
                "routeConfigPath": str(route_path.resolve()),
                "toolConfigSha256": staged_tool_hash,
                "routeConfigSha256": staged_route_hash,
            }
            if (
                staged_tool_hash == current["toolConfigSha256"]
                and staged_route_hash == current["routeConfigSha256"]
            ):
                report["source"] = "existing-managed-config"
                report["generated"] = False
                atomic_write_json(status_path, report)
            else:
                relocation = _managed_configs_relocation_evidence(
                    config,
                    tool_path,
                    route_path,
                    staged_tool,
                    staged_route,
                    current,
                )
                report["relocationEvidence"] = relocation
                if relocation.get("equivalent") is True:
                    # Keep the exact bytes/path bindings that existing
                    # workspaces already pinned. The newly discovered copy is
                    # not adopted merely because ReplayLab launched from a
                    # different installation directory.
                    report["source"] = "existing-managed-config-relocated-copy"
                    report["generated"] = False
                    report["managedConfig"] = dict(current)
                    report["toolRegistrySha256"] = current["toolConfigSha256"]
                    atomic_write_json(status_path, report)
                else:
                    report["source"] = "startup-refresh"
                    report["generated"] = True
                    _commit_managed_generation(
                        tool_path,
                        route_path,
                        status_path,
                        staged_tool,
                        staged_route,
                        report,
                    )
    elif tool_present or route_present:
        report["source"] = "incomplete-existing-config"
        report["error"] = "Only one Bread shader config exists; refusing to overwrite it"
        atomic_write_json(status_path, report)
    elif any(found.values()):
        staged_tool, staged_route, generated = _stage_generation(config, found, adapter_host)
        report.update(generated)
        report["binaryPathsWereDiscovered"] = any(found.values())
        report["toolConfigPath"] = str(tool_path)
        report["routeConfigPath"] = str(route_path)
        report["source"] = "startup-discovery"
        report["generated"] = True
        report["managedConfig"] = {
            "schema": MANAGED_CONFIG_SCHEMA,
            "owner": "ReplayLab",
            "toolConfigPath": str(tool_path.resolve()),
            "routeConfigPath": str(route_path.resolve()),
            "toolConfigSha256": hashlib.sha256(staged_tool).hexdigest(),
            "routeConfigSha256": hashlib.sha256(staged_route).hexdigest(),
        }
        _commit_managed_generation(
            tool_path,
            route_path,
            status_path,
            staged_tool,
            staged_route,
            report,
        )
    else:
        report["source"] = "no-tools-found"
        report["error"] = (
            "No shader compiler/decompiler was found. Install the Windows SDK "
            "or provide legally redistributable tools in ReplayLab/tools."
        )
        atomic_write_json(status_path, report)
    return report
