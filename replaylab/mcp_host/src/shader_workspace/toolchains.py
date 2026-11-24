"""Format-aware routing for hash-pinned external shader tools.

The route catalog never supplies a command, executable, environment variable,
or argument.  It can only select an operation that has already passed the
strict :mod:`shader_workspace.tools` registry validation.  This separation is
important when an AI agent chooses a route: the agent chooses an ID and formats,
not a process invocation.

High-level output reconstructed from bytecode is deliberately labelled as such.
Neither a successful route nor a round-trip smoke test proves semantic or replay
ABI equivalence; the RenderDoc replay-reflection ABI gate remains authoritative.
"""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..symbols.dxbc import fingerprint_bytes
from .catalog import normalize_stage
from .errors import (
    ShaderWorkspaceError,
    ToolConfigurationError,
    ToolExecutionError,
    ToolchainRoutingError,
)
from .io import atomic_write_bytes, atomic_write_json, read_json_object
from .tools import ProcessResult, ToolOperation, ToolRegistry, run_operation

TOOLCHAIN_SCHEMA = "renderdoc-ultra-shader-toolchains/v1"
TOOLCHAIN_VERSION = 1
SMOKE_SCHEMA = "renderdoc-ultra-shader-round-trip-smoke/v1"
RECOVERY_PLAN_SCHEMA = "renderdoc-ultra-shader-recovery-plan/v1"
MAX_CONFIG_BYTES = 4 * 1024 * 1024
MAX_ROUTES = 1024
MAX_CAVEATS = 16
MAX_CAVEAT_CHARS = 2048
MAX_SHADER_BYTES = 512 * 1024 * 1024
MAX_ROUTE_HOPS = 4
ROUTE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

FORMATS = frozenset(
    {
        "dxbc",
        "dxbc-asm",
        "dxil",
        "dxil-asm",
        "spirv",
        "spirv-asm",
        "opengl-spirv",
        "opengl-spirv-asm",
        "hlsl",
        "glsl",
        "essl",
        "msl",
        "wgsl",
        "slang",
    }
)
FORMAT_ALIASES = {
    "spir-v": "spirv",
    "spv": "spirv",
    "spir-v-asm": "spirv-asm",
    "spvasm": "spirv-asm",
    "vulkanspirv": "spirv",
    "vulkan-spirv": "spirv",
    "vulkanspirvasm": "spirv-asm",
    "vulkan-spirv-asm": "spirv-asm",
    "openglspirv": "opengl-spirv",
    "opengl-spv": "opengl-spirv",
    "openglspirvasm": "opengl-spirv-asm",
    "opengl-spv-asm": "opengl-spirv-asm",
    "dxbc-disassembly": "dxbc-asm",
    "dxil-disassembly": "dxil-asm",
}
TEXT_FORMATS = frozenset(
    {
        "dxbc-asm",
        "dxil-asm",
        "spirv-asm",
        "opengl-spirv-asm",
        "hlsl",
        "glsl",
        "essl",
        "msl",
        "wgsl",
        "slang",
    }
)
BINARY_FORMATS = FORMATS.difference(TEXT_FORMATS)
HIGH_LEVEL_SOURCE_FORMATS = frozenset(
    {"hlsl", "glsl", "essl", "msl", "wgsl", "slang"}
)
NATIVE_ASSEMBLY_TO_BINARY = {
    "dxbc-asm": "dxbc",
    "dxil-asm": "dxil",
    "spirv-asm": "spirv",
    "opengl-spirv-asm": "opengl-spirv",
}
_NATIVE_EDITABLE_FALLBACKS = {
    "dxbc": ("dxbc-asm",),
    "dxil": ("dxil-asm",),
    "spirv": ("spirv-asm",),
    "opengl-spirv": ("opengl-spirv-asm",),
}
STAGES = frozenset(
    {
        "*",
        "vertex",
        "hull",
        "domain",
        "geometry",
        "fragment",
        "compute",
        "task",
        "mesh",
        "raygen",
        "intersection",
        "anyhit",
        "closesthit",
        "miss",
        "callable",
        "library",
    }
)
_RAY_STAGES = frozenset(
    {
        "raygen",
        "intersection",
        "anyhit",
        "closesthit",
        "miss",
        "callable",
    }
)
_REPLAY_REPLACEMENT_BLOCKED_STAGES = _RAY_STAGES | {"library"}
FIDELITIES = frozenset(
    {
        "native-compile",
        "ir-disassembly",
        "ir-assembly",
        "reconstructed-source",
        "custom-transform",
    }
)
OPERATIONS = frozenset({"decompile", "compile", "transform"})
_FORMAT_SUFFIX = {
    "dxbc": ".dxbc",
    "dxbc-asm": ".dxbc.txt",
    "dxil": ".dxil",
    "dxil-asm": ".ll",
    "spirv": ".spv",
    "spirv-asm": ".spvasm",
    "opengl-spirv": ".opengl.spv",
    "opengl-spirv-asm": ".opengl.spvasm",
    "hlsl": ".hlsl",
    "glsl": ".glsl",
    "essl": ".essl",
    "msl": ".metal",
    "wgsl": ".wgsl",
    "slang": ".slang",
}

_SPIRV_EXECUTION_MODEL_STAGE = {
    0: "vertex",
    1: "hull",
    2: "domain",
    3: "geometry",
    4: "fragment",
    5: "compute",
    5267: "task",
    5268: "mesh",
    5313: "raygen",
    5314: "intersection",
    5315: "anyhit",
    5316: "closesthit",
    5317: "miss",
    5318: "callable",
    5364: "task",
    5365: "mesh",
}


def normalize_format(value: Any) -> str:
    result = str(value or "").strip().lower().replace("_", "-")
    result = FORMAT_ALIASES.get(result, result)
    if result not in FORMATS:
        raise ToolchainRoutingError("unsupported shader format: %s" % (value,))
    return result


def physical_spirv_entry_point(
    data: bytes, stage: str, preferred_entry_point: str = ""
) -> str:
    """Resolve the entry carried by one physical SPIR-V intermediate."""

    normalized_stage = normalize_stage(stage)
    if len(data) < 20 or len(data) % 4:
        raise ToolchainRoutingError("SPIR-V intermediate is truncated or unaligned")
    little_magic = struct.unpack_from("<I", data, 0)[0]
    if little_magic == 0x07230203:
        endian = "<"
    elif little_magic == 0x03022307:
        endian = ">"
    else:
        raise ToolchainRoutingError("SPIR-V intermediate has invalid magic")
    words = struct.unpack(endian + "%dI" % (len(data) // 4), data)
    version = words[1]
    if (
        ((version >> 16) & 0xFF) != 1
        or ((version >> 8) & 0xFF) > 6
        or words[3] == 0
        or words[4] != 0
    ):
        raise ToolchainRoutingError("SPIR-V intermediate has an invalid header")

    entries: list[tuple[str, str]] = []
    cursor = 5
    while cursor < len(words):
        instruction = words[cursor]
        word_count = instruction >> 16
        opcode = instruction & 0xFFFF
        if word_count == 0 or word_count > len(words) - cursor:
            raise ToolchainRoutingError("SPIR-V intermediate has an invalid instruction")
        if opcode == 15 and word_count >= 4:
            raw_name = bytearray()
            terminated = False
            for word in words[cursor + 3 : cursor + word_count]:
                encoded = word.to_bytes(4, "little")
                nul = encoded.find(b"\x00")
                raw_name.extend(encoded if nul < 0 else encoded[:nul])
                if nul >= 0:
                    terminated = True
                    break
                if len(raw_name) > 512:
                    break
            if not terminated:
                raise ToolchainRoutingError(
                    "SPIR-V intermediate entry point is unterminated or too long"
                )
            try:
                name = raw_name.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                raise ToolchainRoutingError(
                    "SPIR-V intermediate entry point is not UTF-8"
                ) from exc
            if not name or len(name) > 512:
                raise ToolchainRoutingError(
                    "SPIR-V intermediate entry point is empty or too long"
                )
            entries.append(
                (name, _SPIRV_EXECUTION_MODEL_STAGE.get(words[cursor + 1], ""))
            )
        cursor += word_count

    matches = sorted(
        {name for name, item_stage in entries if item_stage == normalized_stage}
    )
    preferred = str(preferred_entry_point or "")
    if preferred in matches:
        return preferred
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ToolchainRoutingError(
            "SPIR-V intermediate has no OpEntryPoint for stage %s" % normalized_stage
        )
    raise ToolchainRoutingError(
        "SPIR-V intermediate has ambiguous OpEntryPoint values for stage %s: %s"
        % (normalized_stage, ", ".join(matches))
    )


def _normalize_target_api(value: Any) -> str:
    api = str(value or "").strip().casefold().replace(" ", "")
    if api.startswith("graphicsapi."):
        api = api[len("graphicsapi.") :]
    api = {
        "direct3d11": "d3d11",
        "direct3d12": "d3d12",
        "vk": "vulkan",
        "gl": "opengl",
    }.get(api, api)
    if api and api not in {"d3d11", "d3d12", "vulkan", "opengl"}:
        raise ToolchainRoutingError("unsupported target API for auto-route: %s" % value)
    return api


def _require_format_for_api(api: str, shader_format: str) -> None:
    formats_by_api = {
        "d3d11": frozenset({"dxbc"}),
        "d3d12": frozenset({"dxbc", "dxil"}),
        "vulkan": frozenset({"spirv"}),
        # RenderDoc deliberately distinguishes Vulkan SPIR-V from OpenGL
        # extension-flavoured SPIR-V even though the physical word stream has
        # the same magic. Capture encoding, not the module header, is the
        # authority for this distinction.
        "opengl": frozenset({"opengl-spirv"}),
    }
    if api and shader_format not in formats_by_api[api]:
        raise ToolchainRoutingError(
            "target API %s does not accept captured container %s"
            % (api, shader_format)
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def _bounded_text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ToolConfigurationError("%s must be a string" % field)
    result = value.strip()
    if (not result and not allow_empty) or len(result) > MAX_CAVEAT_CHARS:
        raise ToolConfigurationError(
            "%s must contain %s..%d characters"
            % (field, 0 if allow_empty else 1, MAX_CAVEAT_CHARS)
        )
    if "\x00" in result:
        raise ToolConfigurationError("%s must not contain NUL" % field)
    return result


@dataclass(frozen=True)
class FormatDetection:
    format: str | None
    confidence: str
    evidence: tuple[str, ...]
    is_binary: bool | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "isBinary": self.is_binary,
        }


def _detect_text_format(text: str) -> FormatDetection:
    sample = text[:1024 * 1024]
    if re.search(r"\bOpCapability\b", sample) and re.search(
        r"\bOpMemoryModel\b", sample
    ):
        return FormatDetection(
            "spirv-asm", "strong", ("OpCapability and OpMemoryModel",), False
        )
    if "target triple = \"dxil-ms-dx\"" in sample or re.search(
        r"\bcall\s+[^\n]*@dx\.op\.", sample
    ):
        return FormatDetection(
            "dxil-asm", "strong", ("DXIL LLVM target/dx.op intrinsic",), False
        )
    if re.search(
        r"(?m)^\s*(?:ps|vs|gs|hs|ds|cs)_(?:4_[01]|5_[01])\s*$", sample
    ) and re.search(
        r"(?m)^\s*dcl_", sample
    ):
        return FormatDetection(
            "dxbc-asm", "strong", ("DXBC profile and dcl_ opcode",), False
        )

    wgsl_score = sum(
        marker in sample
        for marker in ("@vertex", "@fragment", "@compute", "@group(", "var<uniform>")
    )
    msl_score = sum(
        marker in sample
        for marker in ("using namespace metal", "[[stage_in]]", "texture2d<", "device ")
    )
    hlsl_score = sum(
        bool(re.search(pattern, sample))
        for pattern in (
            r"\bSV_[A-Za-z0-9_]+\b",
            r"\bcbuffer\s+[A-Za-z_]",
            r"\b(?:RW)?Texture(?:1D|2D|3D|Cube)(?:Array)?\b",
            r"\bregister\s*\(\s*[btsu]\d+",
            r"\b(?:float|half|uint|int)[1-4]x[1-4]\b",
        )
    )
    glsl_score = sum(
        bool(re.search(pattern, sample))
        for pattern in (
            r"(?m)^\s*#version\s+\d+",
            r"\bgl_(?:Position|FragCoord|FragColor|VertexID|InstanceID)\b",
            r"\blayout\s*\([^)]*(?:location|binding|set)\s*=",
            r"\b(?:sampler|image)(?:1D|2D|3D|Cube)(?:Array)?\b",
        )
    )
    scores = {"wgsl": wgsl_score, "msl": msl_score, "hlsl": hlsl_score, "glsl": glsl_score}
    best = max(scores.values())
    winners = sorted(name for name, score in scores.items() if score == best and score > 0)
    uniquely_strong_single_marker = (
        len(winners) == 1
        and (
            (winners[0] == "hlsl" and bool(re.search(r"\bSV_[A-Za-z0-9_]+\b", sample)))
            or (winners[0] == "glsl" and bool(re.search(r"(?m)^\s*#version\s+\d+", sample)))
        )
    )
    if (best >= 2 or uniquely_strong_single_marker) and len(winners) == 1:
        selected = winners[0]
        if selected == "glsl":
            version = re.search(
                r"(?m)^\s*#version\s+(\d+)(?:\s+(es))?\b", sample
            )
            # GLSL ES 1.00 uses ``#version 100`` without the later ``es``
            # suffix.  Treat that normative spelling as decisive; a precision
            # declaration is only supporting evidence and is never sufficient
            # by itself to relabel desktop GLSL.
            if version and (
                version.group(2) == "es" or version.group(1) == "100"
            ):
                selected = "essl"
        return FormatDetection(
            selected,
            "strong",
            ("language marker score %d" % best,),
            False,
        )
    if best == 1 and len(winners) == 1 and winners[0] in {"wgsl", "msl"}:
        return FormatDetection(
            winners[0], "weak", ("one language-specific marker",), False
        )
    return FormatDetection(
        None,
        "unknown",
        ("UTF-8 text is absent or language markers are ambiguous",),
        False,
    )


def detect_shader_format(data: bytes, hint: str | None = None) -> FormatDetection:
    """Detect well-known shader containers/IR and conservative source markers.

    A caller-supplied hint is only accepted when strong binary/text evidence does
    not contradict it.  Source languages overlap heavily, so ambiguous text is
    returned as unknown instead of guessed.
    """

    if not isinstance(data, bytes):
        raise TypeError("shader data must be bytes")
    detected: FormatDetection
    container = fingerprint_bytes(data)
    if (
        container is not None
        and container.byte_size == len(data)
        and any(chunk.fourcc in {"SHDR", "SHEX", "DXIL"} for chunk in container.chunks)
    ):
        detected = FormatDetection(
            container.kind.lower(),
            "exact",
            ("well-formed DXBC container and executable chunk",),
            True,
        )
    elif len(data) >= 20 and len(data) % 4 == 0:
        magic, version, unused_generator, bound, schema = struct.unpack_from(
            "<5I", data, 0
        )
        version_shape_valid = (
            version & 0xFF0000FF == 0 and ((version >> 16) & 0xFF) > 0
        )
        header_valid = version_shape_valid and bound > 0 and schema == 0
        if magic == 0x07230203:
            if header_valid:
                detected = FormatDetection(
                    "spirv",
                    "strong",
                    (
                        "SPIR-V little-endian magic, aligned words, and plausible header",
                        "full module validation is separate",
                    ),
                    True,
                )
            else:
                detected = FormatDetection(
                    None, "unknown", ("SPIR-V magic has a malformed header",), True
                )
        elif magic == 0x03022307:
            swapped = struct.unpack_from(">5I", data, 0)
            swapped_version = swapped[1]
            swapped_header_valid = (
                swapped_version & 0xFF0000FF == 0
                and ((swapped_version >> 16) & 0xFF) > 0
                and swapped[3] > 0
                and swapped[4] == 0
            )
            detected = (
                FormatDetection(
                    "spirv",
                    "strong",
                    (
                        "byte-swapped SPIR-V magic and plausible header",
                        "consumer compatibility and full validation are separate",
                    ),
                    True,
                )
                if swapped_header_valid
                else FormatDetection(
                    None, "unknown", ("byte-swapped SPIR-V magic has a malformed header",), True
                )
            )
        else:
            detected = FormatDetection(None, "unknown", ("unrecognized binary/text",), None)
    else:
        detected = FormatDetection(None, "unknown", ("unrecognized binary/text",), None)

    if detected.format is None:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            pass
        else:
            if "\x00" not in text:
                detected = _detect_text_format(text)

    if hint is None:
        return detected
    declared = normalize_format(hint)
    declared_semantic_spirv = {
        ("spirv", "opengl-spirv"),
        ("spirv-asm", "opengl-spirv-asm"),
    }
    if (
        detected.format is not None
        and detected.format != declared
        and (detected.format, declared) not in declared_semantic_spirv
    ):
        raise ToolchainRoutingError(
            "shader format hint %s contradicts detected %s evidence"
            % (declared, detected.format)
        )
    if detected.format is not None:
        if (detected.format, declared) in declared_semantic_spirv:
            return FormatDetection(
                declared,
                "declared-semantic-environment",
                detected.evidence
                + (
                    "capture encoding distinguishes OpenGL SPIR-V from the physically identical generic word stream",
                ),
                detected.is_binary,
            )
        return FormatDetection(
            detected.format,
            detected.confidence,
            detected.evidence + ("caller hint agrees",),
            detected.is_binary,
        )
    return FormatDetection(
        declared,
        "declared",
        detected.evidence + ("caller hint used because content was ambiguous",),
        declared in BINARY_FORMATS,
    )


def _read_shader_file(path: str | Path) -> tuple[Path, bytes]:
    source_path = Path(path).expanduser()
    if source_path.is_symlink() or not source_path.is_file():
        raise ToolchainRoutingError("shader path must be a regular non-symlink file")
    shader_path = source_path.resolve()
    size = shader_path.stat().st_size
    if size <= 0 or size > MAX_SHADER_BYTES:
        raise ToolchainRoutingError(
            "shader file must contain 1..%d bytes" % MAX_SHADER_BYTES
        )
    data = shader_path.read_bytes()
    if len(data) != size:
        raise ToolchainRoutingError("shader file changed while it was being read")
    return shader_path, data


def detect_shader_file(path: str | Path, hint: str | None = None) -> FormatDetection:
    unused_path, data = _read_shader_file(path)
    return detect_shader_format(data, hint)


@dataclass(frozen=True)
class RouteCapability:
    route_id: str
    tool_id: str
    operation: str
    tool_config_digest: str
    input_format: str
    output_format: str
    stages: tuple[str, ...]
    target_apis: tuple[str, ...]
    fidelity: str
    priority: int
    implementation: str
    source_url: str
    license_name: str
    caveats: tuple[str, ...]

    def supports_stage(self, stage: str) -> bool:
        return "*" in self.stages or stage in self.stages

    def supports_api(self, target_api: str) -> bool:
        return not target_api or target_api in self.target_apis

    @property
    def is_reconstructed(self) -> bool:
        return self.fidelity == "reconstructed-source"

    def as_dict(self) -> dict[str, Any]:
        return {
            "routeId": self.route_id,
            "toolId": self.tool_id,
            "operation": self.operation,
            "toolConfigDigest": self.tool_config_digest,
            "inputFormat": self.input_format,
            "outputFormat": self.output_format,
            "stages": list(self.stages),
            "targetApis": list(self.target_apis),
            "fidelity": self.fidelity,
            "priority": self.priority,
            "implementation": self.implementation,
            "sourceUrl": self.source_url,
            "license": self.license_name,
            "caveats": list(self.caveats),
            "requiresReplayAbiValidation": self.fidelity
            in {"native-compile", "reconstructed-source", "custom-transform"},
        }


@dataclass(frozen=True)
class ValidatorCapability:
    validator_id: str
    tool_id: str
    tool_config_digest: str
    input_format: str
    stages: tuple[str, ...]
    priority: int
    implementation: str
    source_url: str
    license_name: str
    caveats: tuple[str, ...]

    def supports_stage(self, stage: str) -> bool:
        return "*" in self.stages or stage in self.stages

    def as_dict(self) -> dict[str, Any]:
        return {
            "validatorId": self.validator_id,
            "toolId": self.tool_id,
            "operation": "validate",
            "toolConfigDigest": self.tool_config_digest,
            "inputFormat": self.input_format,
            "stages": list(self.stages),
            "priority": self.priority,
            "implementation": self.implementation,
            "sourceUrl": self.source_url,
            "license": self.license_name,
            "caveats": list(self.caveats),
        }


def _parse_stages(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > len(STAGES):
        raise ToolConfigurationError("%s must be a non-empty bounded array" % field)
    stages: set[str] = set()
    for raw_stage in value:
        stage = "*" if raw_stage == "*" else normalize_stage(raw_stage)
        if stage not in STAGES:
            raise ToolConfigurationError("%s has unsupported stage %s" % (field, raw_stage))
        stages.add(stage)
    if "*" in stages and len(stages) != 1:
        raise ToolConfigurationError("%s wildcard stage must be used alone" % field)
    return tuple(sorted(stages))


def _parse_validator(index: int, value: Any, registry: ToolRegistry) -> ValidatorCapability:
    field = "validators[%d]" % index
    if not isinstance(value, Mapping):
        raise ToolConfigurationError("%s must be an object" % field)
    allowed = {
        "validatorId",
        "toolId",
        "toolConfigDigest",
        "inputFormat",
        "stages",
        "priority",
        "implementation",
        "sourceUrl",
        "license",
        "caveats",
    }
    unknown = set(value).difference(allowed)
    if unknown:
        raise ToolConfigurationError(
            "%s has unknown fields: %s" % (field, ", ".join(sorted(unknown)))
        )
    validator_id = str(value.get("validatorId", ""))
    if not ROUTE_ID_RE.fullmatch(validator_id):
        raise ToolConfigurationError("%s.validatorId is invalid" % field)
    tool_id = str(value.get("toolId", ""))
    operation = registry.operation(tool_id, "validate")
    digest = str(value.get("toolConfigDigest", "")).lower()
    if not SHA256_RE.fullmatch(digest) or digest != operation.config_digest:
        raise ToolConfigurationError(
            "%s.toolConfigDigest does not match the pinned validator" % field
        )
    input_format = normalize_format(value.get("inputFormat"))
    stages = _parse_stages(value.get("stages"), field + ".stages")
    priority = value.get("priority", 100)
    if isinstance(priority, bool):
        raise ToolConfigurationError("%s.priority must be an integer" % field)
    try:
        priority = int(priority)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ToolConfigurationError("%s.priority must be an integer" % field) from exc
    if priority < 0 or priority > 10000:
        raise ToolConfigurationError("%s.priority must be between 0 and 10000" % field)
    implementation = _bounded_text(value.get("implementation", "custom"), field + ".implementation")
    source_url = _bounded_text(value.get("sourceUrl", ""), field + ".sourceUrl", allow_empty=True)
    if source_url and not source_url.startswith("https://"):
        raise ToolConfigurationError("%s.sourceUrl must use https" % field)
    license_name = _bounded_text(value.get("license", ""), field + ".license", allow_empty=True)
    caveats_value = value.get("caveats", [])
    if not isinstance(caveats_value, list) or len(caveats_value) > MAX_CAVEATS:
        raise ToolConfigurationError("%s.caveats must be a bounded array" % field)
    caveats = tuple(
        _bounded_text(item, "%s.caveats[%d]" % (field, item_index))
        for item_index, item in enumerate(caveats_value)
    )
    return ValidatorCapability(
        validator_id=validator_id,
        tool_id=tool_id,
        tool_config_digest=digest,
        input_format=input_format,
        stages=stages,
        priority=priority,
        implementation=implementation,
        source_url=source_url,
        license_name=license_name,
        caveats=caveats,
    )


def _parse_route(index: int, value: Any, registry: ToolRegistry) -> RouteCapability:
    field = "routes[%d]" % index
    if not isinstance(value, Mapping):
        raise ToolConfigurationError("%s must be an object" % field)
    allowed = {
        "routeId",
        "toolId",
        "operation",
        "toolConfigDigest",
        "inputFormat",
        "outputFormat",
        "stages",
        "targetApis",
        "fidelity",
        "priority",
        "implementation",
        "sourceUrl",
        "license",
        "caveats",
    }
    unknown = set(value).difference(allowed)
    if unknown:
        raise ToolConfigurationError(
            "%s has unknown fields: %s" % (field, ", ".join(sorted(unknown)))
        )
    route_id = str(value.get("routeId", ""))
    if not ROUTE_ID_RE.fullmatch(route_id):
        raise ToolConfigurationError("%s.routeId is invalid" % field)
    tool_id = str(value.get("toolId", ""))
    operation_name = str(value.get("operation", ""))
    if operation_name not in OPERATIONS:
        raise ToolConfigurationError(
            "%s.operation must be decompile, compile, or transform" % field
        )
    operation = registry.operation(tool_id, operation_name)
    digest = str(value.get("toolConfigDigest", "")).lower()
    if not SHA256_RE.fullmatch(digest) or digest != operation.config_digest:
        raise ToolConfigurationError(
            "%s.toolConfigDigest does not match the pinned operation" % field
        )
    input_format = normalize_format(value.get("inputFormat"))
    output_format = normalize_format(value.get("outputFormat"))
    if input_format == output_format:
        raise ToolConfigurationError("%s must change format" % field)
    stages_value = value.get("stages")
    if not isinstance(stages_value, list) or not stages_value or len(stages_value) > len(STAGES):
        raise ToolConfigurationError("%s.stages must be a non-empty bounded array" % field)
    stages: set[str] = set()
    for raw_stage in stages_value:
        stage = "*" if raw_stage == "*" else normalize_stage(raw_stage)
        if stage not in STAGES:
            raise ToolConfigurationError("%s has unsupported stage %s" % (field, raw_stage))
        stages.add(stage)
    if "*" in stages and len(stages) != 1:
        raise ToolConfigurationError("%s wildcard stage must be used alone" % field)
    target_apis_value = value.get("targetApis")
    if (
        not isinstance(target_apis_value, list)
        or not target_apis_value
        or len(target_apis_value) > 4
    ):
        raise ToolConfigurationError(
            "%s.targetApis must be a non-empty bounded array" % field
        )
    try:
        target_apis = tuple(
            sorted({_normalize_target_api(item) for item in target_apis_value})
        )
    except ToolchainRoutingError as exc:
        raise ToolConfigurationError(
            "%s.targetApis contains an unsupported API" % field
        ) from exc
    if "" in target_apis or len(target_apis) != len(target_apis_value):
        raise ToolConfigurationError(
            "%s.targetApis must contain unique concrete APIs" % field
        )
    fidelity = str(value.get("fidelity", ""))
    if fidelity not in FIDELITIES:
        raise ToolConfigurationError("%s.fidelity is unsupported" % field)
    if operation_name == "decompile" and input_format not in BINARY_FORMATS:
        raise ToolConfigurationError("%s.decompile input must be a binary format" % field)
    if operation_name == "compile" and output_format not in BINARY_FORMATS:
        raise ToolConfigurationError("%s.compile output must be a binary format" % field)
    if (input_format == "hlsl" and operation_name == "compile"
            and any(api in {"d3d11", "d3d12"} for api in target_apis)
            and "target_profile" not in operation.placeholders):
        raise ToolConfigurationError(
            "%s HLSL compiler must consume the exact {target_profile} placeholder" % field
        )
    if operation_name == "transform":
        if fidelity != "custom-transform":
            raise ToolConfigurationError(
                "%s.transform must be labelled custom-transform" % field
            )
    elif fidelity == "custom-transform":
        raise ToolConfigurationError(
            "%s custom-transform fidelity requires a transform operation" % field
        )
    elif operation_name == "decompile":
        if output_format in HIGH_LEVEL_SOURCE_FORMATS:
            if fidelity != "reconstructed-source":
                raise ToolConfigurationError(
                    "%s binary-to-source decompile must be labelled reconstructed-source"
                    % field
                )
        elif output_format in NATIVE_ASSEMBLY_TO_BINARY:
            if (
                fidelity != "ir-disassembly"
                or NATIVE_ASSEMBLY_TO_BINARY[output_format] != input_format
            ):
                raise ToolConfigurationError(
                    "%s native disassembly must preserve its binary family and use ir-disassembly"
                    % field
                )
        else:
            raise ToolConfigurationError(
                "%s decompile output must be high-level source or matching native assembly"
                % field
            )
    elif operation_name == "compile":
        if input_format in HIGH_LEVEL_SOURCE_FORMATS:
            if fidelity != "native-compile":
                raise ToolConfigurationError(
                    "%s source compile must be labelled native-compile" % field
                )
        elif input_format in NATIVE_ASSEMBLY_TO_BINARY:
            if (
                fidelity != "ir-assembly"
                or NATIVE_ASSEMBLY_TO_BINARY[input_format] != output_format
            ):
                raise ToolConfigurationError(
                    "%s native assembly must preserve its binary family and use ir-assembly"
                    % field
                )
        else:
            raise ToolConfigurationError(
                "%s compile input must be high-level source or matching native assembly"
                % field
            )
    priority_raw = value.get("priority", 100)
    if isinstance(priority_raw, bool):
        raise ToolConfigurationError("%s.priority must be an integer" % field)
    try:
        priority = int(priority_raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ToolConfigurationError("%s.priority must be an integer" % field) from exc
    if priority < 0 or priority > 10000:
        raise ToolConfigurationError("%s.priority must be between 0 and 10000" % field)
    implementation = _bounded_text(value.get("implementation", "custom"), field + ".implementation")
    source_url = _bounded_text(value.get("sourceUrl", ""), field + ".sourceUrl", allow_empty=True)
    if source_url and not source_url.startswith("https://"):
        raise ToolConfigurationError("%s.sourceUrl must use https" % field)
    license_name = _bounded_text(value.get("license", ""), field + ".license", allow_empty=True)
    caveats_value = value.get("caveats", [])
    if not isinstance(caveats_value, list) or len(caveats_value) > MAX_CAVEATS:
        raise ToolConfigurationError("%s.caveats must be a bounded array" % field)
    caveats = tuple(
        _bounded_text(item, "%s.caveats[%d]" % (field, caveat_index))
        for caveat_index, item in enumerate(caveats_value)
    )
    if fidelity == "reconstructed-source" and not caveats:
        raise ToolConfigurationError(
            "%s reconstructed-source route must state at least one caveat" % field
        )
    return RouteCapability(
        route_id=route_id,
        tool_id=tool_id,
        operation=operation_name,
        tool_config_digest=digest,
        input_format=input_format,
        output_format=output_format,
        stages=tuple(sorted(stages)),
        target_apis=target_apis,
        fidelity=fidelity,
        priority=priority,
        implementation=implementation,
        source_url=source_url,
        license_name=license_name,
        caveats=caveats,
    )


_KNOWN_MATRIX = (
    ("dxbc", "dxbc-asm", "FXC/RenderDoc can expose assembly, not original HLSL."),
    ("dxbc", "hlsl", "No Microsoft compiler provides a general DXBC-to-original-HLSL recovery guarantee."),
    ("hlsl", "dxbc", "The supported FXC/DXBC profile parser covers Shader Model 4.0 through 5.1."),
    ("dxil", "dxil-asm", "DXC disassembles DXIL to an LLVM/DXIL representation, not recovered HLSL."),
    ("dxil", "hlsl", "DXC does not provide a general DXIL-to-original-HLSL decompiler."),
    ("hlsl", "dxil", "DXC compiles Shader Model 6+ HLSL to DXIL."),
    ("spirv", "spirv-asm", "SPIRV-Tools provides the normative-style binary disassembly path."),
    ("spirv-asm", "spirv", "SPIRV-Tools assembles text but validation is a separate step."),
    ("spirv", "hlsl", "SPIRV-Cross reconstructs usable HLSL; names/source structure can be lost."),
    ("spirv", "glsl", "SPIRV-Cross reconstructs GLSL; target semantics/options must be selected deliberately."),
    ("hlsl", "spirv", "DXC supports HLSL-to-SPIR-V for Vulkan; binding/environment options affect ABI."),
    ("glsl", "spirv", "glslang is the Khronos reference GLSL front end and SPIR-V generator."),
    (
        "opengl-spirv",
        "opengl-spirv-asm",
        "OpenGL SPIR-V has the same physical magic as Vulkan SPIR-V but RenderDoc keeps the semantic environment distinct.",
    ),
    (
        "opengl-spirv-asm",
        "opengl-spirv",
        "SPIRV-Tools can assemble the text, but OpenGL-environment validation is separate.",
    ),
    (
        "opengl-spirv",
        "glsl",
        "SPIRV-Cross reconstructs OpenGL GLSL only when the capture encoding identifies OpenGL semantics.",
    ),
    (
        "glsl",
        "opengl-spirv",
        "glslang -G compiles GLSL under OpenGL SPIR-V semantics; this is not Vulkan SPIR-V.",
    ),
)


class ToolchainCatalog:
    """Immutable capability routes bound to one immutable tool-registry snapshot."""

    def __init__(
        self,
        tool_config_path: str | Path,
        route_config_path: str | Path,
        *,
        registry: ToolRegistry | None = None,
    ):
        requested_registry_path = Path(tool_config_path).expanduser().resolve()
        if registry is not None and registry.path != requested_registry_path:
            raise ToolConfigurationError(
                "supplied tool registry path does not match tool_config_path"
            )
        self.registry = registry or ToolRegistry(requested_registry_path)
        self.path = Path(route_config_path).expanduser().resolve()
        try:
            value = read_json_object(self.path, MAX_CONFIG_BYTES, "shader toolchain config")
        except ValueError as exc:
            raise ToolConfigurationError(str(exc)) from exc
        unknown_top = set(value).difference(
            {"schema", "schemaVersion", "toolRegistrySha256", "routes", "validators"}
        )
        if unknown_top:
            raise ToolConfigurationError(
                "unknown shader toolchain config fields: %s"
                % ", ".join(sorted(unknown_top))
            )
        if value.get("schema") != TOOLCHAIN_SCHEMA or value.get("schemaVersion") != TOOLCHAIN_VERSION:
            raise ToolConfigurationError("unsupported shader toolchain schema/version")
        expected_registry_hash = str(value.get("toolRegistrySha256", "")).lower()
        actual_registry_hash = _sha256_file(self.registry.path)
        if not SHA256_RE.fullmatch(expected_registry_hash) or expected_registry_hash != actual_registry_hash:
            raise ToolConfigurationError(
                "toolRegistrySha256 does not match the exact shader tool registry bytes"
            )
        route_values = value.get("routes")
        if not isinstance(route_values, list) or len(route_values) > MAX_ROUTES:
            raise ToolConfigurationError(
                "shader toolchain routes must contain 0..%d records" % MAX_ROUTES
            )
        routes = [_parse_route(index, item, self.registry) for index, item in enumerate(route_values)]
        ids = [item.route_id for item in routes]
        if len(set(ids)) != len(ids):
            raise ToolConfigurationError("shader toolchain routeId values must be unique")
        validator_values = value.get("validators", [])
        if not isinstance(validator_values, list) or len(validator_values) > MAX_ROUTES:
            raise ToolConfigurationError("shader toolchain validators must be a bounded array")
        validators = [
            _parse_validator(index, item, self.registry)
            for index, item in enumerate(validator_values)
        ]
        validator_ids = [item.validator_id for item in validators]
        if len(set(validator_ids)) != len(validator_ids):
            raise ToolConfigurationError("shader toolchain validatorId values must be unique")
        if not routes and not validators:
            raise ToolConfigurationError(
                "shader toolchain config must contain at least one route or validator"
            )
        self.tool_registry_sha256 = actual_registry_hash
        self._routes = tuple(sorted(routes, key=lambda item: item.route_id))
        self._validators = tuple(sorted(validators, key=lambda item: item.validator_id))

    def routes(self) -> tuple[RouteCapability, ...]:
        return self._routes

    def validators(self) -> tuple[ValidatorCapability, ...]:
        return self._validators

    @staticmethod
    def _is_direct_native_ir_round_trip(
        original: str,
        editable: str,
        forward: Sequence[RouteCapability],
        reverse: Sequence[RouteCapability],
    ) -> bool:
        """Accept only one-hop disassembly plus one-hop assembly fallback.

        This prevents a reconstructed-source or custom-transform chain from
        being relabelled as native IR merely because it happens to terminate at
        an assembly text format.
        """

        return bool(
            len(forward) == 1
            and len(reverse) == 1
            and forward[0].input_format == original
            and forward[0].output_format == editable
            and forward[0].fidelity == "ir-disassembly"
            and reverse[0].input_format == editable
            and reverse[0].output_format == original
            and reverse[0].fidelity == "ir-assembly"
        )

    def matching_validators(
        self, input_format: str, stage: str, *, require_integrity: bool = True
    ) -> tuple[ValidatorCapability, ...]:
        shader_format = normalize_format(input_format)
        normalized_stage = normalize_stage(stage)
        integrity = self._integrity() if require_integrity else {}
        result = [
            item
            for item in self._validators
            if item.input_format == shader_format
            and item.supports_stage(normalized_stage)
            and (
                not require_integrity
                or integrity[(item.tool_id, "validate")]["available"]
            )
        ]
        return tuple(sorted(result, key=lambda item: (item.priority, item.validator_id)))

    def strict_spirv_validator(
        self, input_format: str, stage: str
    ) -> ValidatorCapability:
        """Select one integrity-valid, explicitly identified ``spirv-val``.

        Native SPIR-V assembly is only a safe *automatic* fallback when the
        assembled module is checked in its capture-declared API environment.
        A validator capability is therefore selected with the same
        deterministic, fail-closed priority rule as a route.  Merely having a
        generic ``validate`` operation is insufficient: the trusted catalog
        must identify it as ``spirv-val`` and bind its exact operation digest.
        """

        shader_format = normalize_format(input_format)
        if shader_format not in {"spirv", "opengl-spirv"}:
            raise ToolchainRoutingError(
                "strict SPIR-V validation requires a Vulkan or OpenGL SPIR-V format"
            )
        normalized_stage = normalize_stage(stage)
        configured = self.matching_validators(
            shader_format, normalized_stage, require_integrity=False
        )
        ready = tuple(
            item
            for item in self.matching_validators(
                shader_format, normalized_stage, require_integrity=True
            )
            if "spirv-val" in item.implementation.casefold()
        )
        if not ready:
            reason = (
                "configured spirv-val integrity is unavailable"
                if any(
                    "spirv-val" in item.implementation.casefold()
                    for item in configured
                )
                else "no fixed spirv-val capability is configured"
            )
            raise ToolchainRoutingError(
                "%s for %s at stage %s"
                % (reason, shader_format, normalized_stage)
            )
        best_priority = min(item.priority for item in ready)
        best = tuple(item for item in ready if item.priority == best_priority)
        if len(best) != 1:
            raise ToolchainRoutingError(
                "strict spirv-val selection is ambiguous for %s at stage %s: %s"
                % (
                    shader_format,
                    normalized_stage,
                    ", ".join(item.validator_id for item in best),
                )
            )
        return best[0]

    def _integrity(self) -> dict[tuple[str, str], dict[str, Any]]:
        result: dict[tuple[str, str], dict[str, Any]] = {}
        for operation in self.registry.operations():
            key = (operation.tool_id, operation.operation)
            result[key] = self.registry.operation_integrity(*key)
        return result

    def matching_routes(
        self,
        input_format: str,
        output_format: str,
        stage: str,
        *,
        allow_reconstructed: bool = True,
        require_integrity: bool = True,
        target_api: str | None = None,
    ) -> tuple[RouteCapability, ...]:
        source = normalize_format(input_format)
        target = normalize_format(output_format)
        normalized_stage = normalize_stage(stage)
        api = _normalize_target_api(target_api)
        if normalized_stage not in STAGES or normalized_stage == "*":
            raise ToolchainRoutingError("a concrete supported shader stage is required")
        # A narrow lookup (for example DXIL disassembly evidence during a
        # workspace status read) must not hash every unrelated compiler and
        # companion DLL. Check only matching operations, once per lookup.
        # Keep these checks fresh: neither a previous lookup nor catalog
        # construction authorizes changed executable/dependency bytes.
        integrity: dict[tuple[str, str], dict[str, Any]] = {}
        result = []
        for route in self._routes:
            if route.input_format != source or route.output_format != target:
                continue
            if not route.supports_stage(normalized_stage):
                continue
            if not route.supports_api(api):
                continue
            if not allow_reconstructed and route.is_reconstructed:
                continue
            if require_integrity:
                key = (route.tool_id, route.operation)
                if key not in integrity:
                    integrity[key] = self.registry.operation_integrity(*key)
                if not integrity[key]["available"]:
                    continue
            result.append(route)
        return tuple(sorted(result, key=lambda item: (item.priority, item.route_id)))

    def find_path(
        self,
        input_format: str,
        output_format: str,
        stage: str,
        *,
        allow_reconstructed: bool = True,
        max_hops: int = MAX_ROUTE_HOPS,
        require_unique: bool = True,
        target_api: str | None = None,
    ) -> tuple[RouteCapability, ...]:
        source = normalize_format(input_format)
        target = normalize_format(output_format)
        normalized_stage = normalize_stage(stage)
        api = _normalize_target_api(target_api)
        if normalized_stage not in STAGES or normalized_stage == "*":
            raise ToolchainRoutingError("a concrete supported shader stage is required")
        if not isinstance(allow_reconstructed, bool):
            raise ToolchainRoutingError("allow_reconstructed must be a boolean")
        if (
            isinstance(max_hops, bool)
            or not isinstance(max_hops, int)
            or max_hops < 1
            or max_hops > MAX_ROUTE_HOPS
        ):
            raise ToolchainRoutingError("max_hops must be between 1 and %d" % MAX_ROUTE_HOPS)
        if source == target:
            return ()
        integrity = self._integrity()
        candidates = [
            route
            for route in self._routes
            if route.supports_stage(normalized_stage)
            and route.supports_api(api)
            and (allow_reconstructed or not route.is_reconstructed)
            and integrity[(route.tool_id, route.operation)]["available"]
        ]
        paths: list[tuple[RouteCapability, ...]] = []

        def visit(current: str, path: tuple[RouteCapability, ...], seen: frozenset[str]) -> None:
            if len(path) >= max_hops:
                return
            for route in candidates:
                if route.input_format != current or route.output_format in seen:
                    continue
                next_path = path + (route,)
                if not set.intersection(*(set(item.target_apis) for item in next_path)):
                    continue
                if route.output_format == target:
                    paths.append(next_path)
                else:
                    visit(route.output_format, next_path, seen | {route.output_format})

        visit(source, (), frozenset({source}))
        if not paths:
            raise ToolchainRoutingError(
                "no integrity-valid %s -> %s route supports stage %s%s"
                % (
                    source,
                    target,
                    normalized_stage,
                    " without reconstructed source" if not allow_reconstructed else "",
                )
            )

        def score(path: tuple[RouteCapability, ...]) -> tuple[Any, ...]:
            reconstructed = sum(item.is_reconstructed for item in path)
            custom = sum(item.fidelity == "custom-transform" for item in path)
            return (
                reconstructed,
                custom,
                sum(item.priority for item in path),
                len(path),
            )
        best_score = min(score(path) for path in paths)
        best = sorted(
            (path for path in paths if score(path) == best_score),
            key=lambda path: tuple(item.route_id for item in path),
        )
        if require_unique and len(best) != 1:
            raise ToolchainRoutingError(
                "auto-route is ambiguous for %s -> %s at stage %s: %s; "
                "adjust trusted priorities or choose an explicit route ID"
                % (
                    source,
                    target,
                    normalized_stage,
                    "; ".join(
                        "+".join(item.route_id for item in path) for path in best
                    ),
                )
            )
        return best[0]

    def candidate_paths(
        self,
        input_format: str,
        output_format: str,
        stage: str,
        *,
        allow_reconstructed: bool = True,
        max_hops: int = MAX_ROUTE_HOPS,
        require_integrity: bool = True,
        limit: int = 16,
        target_api: str | None = None,
    ) -> tuple[tuple[RouteCapability, ...], ...]:
        """Return bounded deterministic route candidates, best first.

        Unlike :meth:`find_path`, this method does not treat an equal-score tie
        as an error.  Recovery orchestration needs the complete, fixed set of
        registered alternatives so program logic (or an AI choosing only from
        those IDs) can retry another implementation without ever supplying a
        command line or executable path.
        """

        source = normalize_format(input_format)
        target = normalize_format(output_format)
        normalized_stage = normalize_stage(stage)
        api = _normalize_target_api(target_api)
        if normalized_stage not in STAGES or normalized_stage == "*":
            raise ToolchainRoutingError("a concrete supported shader stage is required")
        if not isinstance(allow_reconstructed, bool):
            raise ToolchainRoutingError("allow_reconstructed must be a boolean")
        if (
            isinstance(max_hops, bool)
            or not isinstance(max_hops, int)
            or max_hops < 1
            or max_hops > MAX_ROUTE_HOPS
        ):
            raise ToolchainRoutingError("max_hops must be between 1 and %d" % MAX_ROUTE_HOPS)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 128:
            raise ToolchainRoutingError("candidate path limit must be between 1 and 128")
        if source == target:
            return ((),)

        integrity = self._integrity() if require_integrity else {}
        routes = [
            route
            for route in self._routes
            if route.supports_stage(normalized_stage)
            and route.supports_api(api)
            and (allow_reconstructed or not route.is_reconstructed)
            and (
                not require_integrity
                or integrity[(route.tool_id, route.operation)]["available"]
            )
        ]
        discovered: list[tuple[RouteCapability, ...]] = []

        def visit(
            current: str,
            path: tuple[RouteCapability, ...],
            seen: frozenset[str],
        ) -> None:
            if len(path) >= max_hops:
                return
            for route in routes:
                if route.input_format != current or route.output_format in seen:
                    continue
                candidate = path + (route,)
                if not set.intersection(*(set(item.target_apis) for item in candidate)):
                    continue
                if route.output_format == target:
                    discovered.append(candidate)
                else:
                    visit(
                        route.output_format,
                        candidate,
                        seen | {route.output_format},
                    )

        visit(source, (), frozenset({source}))

        def score(path: tuple[RouteCapability, ...]) -> tuple[Any, ...]:
            return (
                sum(item.is_reconstructed for item in path),
                sum(item.fidelity == "custom-transform" for item in path),
                sum(item.priority for item in path),
                len(path),
                tuple(item.route_id for item in path),
            )

        ordered = sorted(discovered, key=score)
        return tuple(ordered[:limit])

    def plan_recovery(
        self,
        input_format: str,
        stage: str,
        *,
        editable_format: str | None = None,
        capture_encoding: str | None = None,
        target_api: str | None = None,
        allow_reconstructed: bool = True,
    ) -> dict[str, Any]:
        """Plan a recoverable workspace even when decompilation is unavailable.

        Program logic reports format, registered routes, integrity and route
        availability. The caller chooses how to use that evidence. A candidate
        can replace replay code only through the separate compile and ABI gates.
        """

        original = normalize_format(input_format)
        normalized_stage = normalize_stage(stage)
        if original not in BINARY_FORMATS:
            raise ToolchainRoutingError(
                "automatic recovery planning requires a captured binary shader format"
            )
        if not isinstance(allow_reconstructed, bool):
            raise ToolchainRoutingError("allow_reconstructed must be a boolean")
        if capture_encoding is not None:
            encoding = normalize_format(capture_encoding)
            if encoding != original:
                raise ToolchainRoutingError(
                    "capture encoding %s contradicts detected container %s"
                    % (encoding, original)
                )
        api = _normalize_target_api(target_api)
        _require_format_for_api(api, original)
        if original in {"spirv", "opengl-spirv"} and not api:
            raise ToolchainRoutingError(
                "SPIR-V target API/environment cannot be derived from the module; "
                "capture metadata must identify the API"
            )
        if editable_format is None:
            editable = "hlsl" if original in {"dxbc", "dxil"} else "glsl"
            if original == "spirv":
                try:
                    self.select_round_trip(original, normalized_stage, editable_format="hlsl",
                                           capture_encoding=original, target_api=api,
                                           allow_reconstructed=allow_reconstructed)
                except ToolchainRoutingError:
                    pass
                else:
                    editable = "hlsl"
        else:
            editable = normalize_format(editable_format)
        if editable not in TEXT_FORMATS:
            raise ToolchainRoutingError("recovery editable format must be textual")

        decompile_paths = self.candidate_paths(
            original,
            editable,
            normalized_stage,
            allow_reconstructed=allow_reconstructed,
            require_integrity=True,
            target_api=api,
        )
        compile_paths = self.candidate_paths(
            editable,
            original,
            normalized_stage,
            allow_reconstructed=allow_reconstructed,
            require_integrity=True,
            target_api=api,
        )
        configured_decompile_paths = self.candidate_paths(
            original,
            editable,
            normalized_stage,
            allow_reconstructed=allow_reconstructed,
            require_integrity=False,
            target_api=api,
        )
        configured_compile_paths = self.candidate_paths(
            editable,
            original,
            normalized_stage,
            allow_reconstructed=allow_reconstructed,
            require_integrity=False,
            target_api=api,
        )

        def profile_applicable(
            paths: Sequence[tuple[RouteCapability, ...]],
        ) -> tuple[tuple[RouteCapability, ...], ...]:
            if original in {"dxbc", "dxil"}:
                return tuple(paths)
            return tuple(
                path
                for path in paths
                if not any(
                    "target_profile"
                    in self.registry.operation(
                        route.tool_id, route.operation
                    ).placeholders
                    for route in path
                )
            )

        decompile_paths = profile_applicable(decompile_paths)
        compile_paths = profile_applicable(compile_paths)
        configured_decompile_paths = profile_applicable(
            configured_decompile_paths
        )
        configured_compile_paths = profile_applicable(configured_compile_paths)

        if decompile_paths and compile_paths:
            mode = "round-trip"
        elif compile_paths:
            mode = "compile-only-recovery"
        else:
            mode = "evidence-only"

        def path_ids(path: Sequence[RouteCapability]) -> list[str]:
            return [item.route_id for item in path]

        primary_decompile = decompile_paths[0] if decompile_paths else ()
        primary_compile = compile_paths[0] if compile_paths else ()
        diagnostics = [
            {
                "phase": "decompile",
                "category": (
                    "route-ready"
                    if decompile_paths
                    else (
                        "tool-integrity-unavailable"
                        if configured_decompile_paths
                        else "route-unavailable"
                    )
                ),
                "readyCandidateCount": len(decompile_paths),
                "configuredCandidateCount": len(configured_decompile_paths),
            },
            {
                "phase": "compile",
                "category": (
                    "route-ready"
                    if compile_paths
                    else (
                        "tool-integrity-unavailable"
                        if configured_compile_paths
                        else "route-unavailable"
                    )
                ),
                "readyCandidateCount": len(compile_paths),
                "configuredCandidateCount": len(configured_compile_paths),
            },
        ]
        chosen = list(primary_decompile) + list(primary_compile)
        if not api and any(
            "target_api"
            in self.registry.operation(item.tool_id, item.operation).placeholders
            for item in chosen
        ):
            raise ToolchainRoutingError(
                "the selected recovery route requires the physical target API; it "
                "will not be inferred from shader bytes"
            )
        strict_validator: ValidatorCapability | None = None
        native_spirv_lane = bool(
            original in {"spirv", "opengl-spirv"}
            and editable in _NATIVE_EDITABLE_FALLBACKS.get(original, ())
            and (primary_decompile or primary_compile)
        )
        if native_spirv_lane:
            if primary_decompile and not (
                len(primary_decompile) == 1
                and primary_decompile[0].input_format == original
                and primary_decompile[0].output_format == editable
                and primary_decompile[0].fidelity == "ir-disassembly"
            ):
                raise ToolchainRoutingError(
                    "native SPIR-V source recovery requires one direct "
                    "ir-disassembly route"
                )
            if primary_compile and not (
                len(primary_compile) == 1
                and primary_compile[0].input_format == editable
                and primary_compile[0].output_format == original
                and primary_compile[0].fidelity == "ir-assembly"
            ):
                raise ToolchainRoutingError(
                    "native SPIR-V compilation requires one direct ir-assembly route"
                )
            strict_validator = self.strict_spirv_validator(
                original, normalized_stage
            )
        return {
            "schema": RECOVERY_PLAN_SCHEMA,
            "schemaVersion": 1,
            "mode": mode,
            "inputFormat": original,
            "captureEncoding": original,
            "targetApi": api or None,
            "stage": normalized_stage,
            "editableFormat": editable,
            "decompileRouteIds": path_ids(primary_decompile),
            "decompilerToolIds": [item.tool_id for item in primary_decompile],
            "compileRouteIds": path_ids(primary_compile),
            "compilerToolIds": [item.tool_id for item in primary_compile],
            "capabilities": [item.as_dict() for item in chosen],
            "decompileCandidates": [path_ids(path) for path in decompile_paths],
            "compileCandidates": [path_ids(path) for path in compile_paths],
            "configuredButUnavailable": {
                "decompile": [path_ids(path) for path in configured_decompile_paths]
                if not decompile_paths
                else [],
                "compile": [path_ids(path) for path in configured_compile_paths]
                if not compile_paths
                else [],
            },
            "diagnostics": diagnostics,
            "containsReconstructedSource": any(
                item.is_reconstructed for item in primary_decompile
            ),
            "strictNativeIrValidationRequired": strict_validator is not None,
            "requiredValidators": (
                [strict_validator.as_dict()]
                if strict_validator is not None
                else []
            ),
            "lane": "candidate" if mode == "round-trip" else "evidence",
            "directReplayApplyAuthorized": False,
            "semanticEquivalenceVerified": False,
            "replayAbiValidationStillRequired": True,
            "guarantee": (
                "This plan binds available program routes and recovery choices only; "
                "it does not recover original source or prove semantic equivalence."
            ),
        }

    def plan_automatic_recovery(
        self,
        input_format: str,
        stage: str,
        *,
        capture_encoding: str | None = None,
        target_api: str | None = None,
        allow_reconstructed: bool = True,
    ) -> dict[str, Any]:
        """Prefer source, then select a complete native-IR round trip.

        A default request is a request for the best *usable* editable lane, not
        an assertion that high-level source exists.  HLSL/GLSL remains first;
        when it is only compile-only or evidence-only, a fixed same-family
        assembly lane wins if and only if both directions are hash-verified.
        Explicit ``editable_format`` requests continue to use
        :meth:`plan_recovery` and never switch languages behind the caller.
        """

        original = normalize_format(input_format)
        preferred = self.plan_recovery(
            original,
            stage,
            editable_format=None,
            capture_encoding=capture_encoding,
            target_api=target_api,
            allow_reconstructed=allow_reconstructed,
        )
        preferred_strict_error = ""
        if preferred.get("mode") == "round-trip":
            try:
                strict_preferred = self.select_round_trip(
                    original,
                    stage,
                    editable_format=str(preferred.get("editableFormat", "")),
                    capture_encoding=capture_encoding,
                    target_api=target_api,
                    allow_reconstructed=allow_reconstructed,
                )
            except ToolchainRoutingError as exc:
                preferred_strict_error = str(exc)
            else:
                if (
                    strict_preferred.get("decompileRouteIds")
                    == preferred.get("decompileRouteIds")
                    and strict_preferred.get("compileRouteIds")
                    == preferred.get("compileRouteIds")
                ):
                    return preferred
                preferred_strict_error = (
                    "strict preferred routes differ from recovery primary routes"
                )
        for editable in _NATIVE_EDITABLE_FALLBACKS.get(original, ()):
            try:
                strict = self.select_round_trip(
                    original,
                    stage,
                    editable_format=editable,
                    capture_encoding=capture_encoding,
                    target_api=target_api,
                    allow_reconstructed=False,
                )
                forward_count = len(strict.get("decompileRouteIds", []))
                capabilities = strict.get("capabilities", [])
                routes_by_id = {route.route_id: route for route in self._routes}
                forward = tuple(
                    routes_by_id[route_id]
                    for route_id in strict["decompileRouteIds"]
                )
                reverse = tuple(
                    routes_by_id[route_id]
                    for route_id in strict["compileRouteIds"]
                )
                if (
                    len(capabilities) != forward_count + len(reverse)
                    or not self._is_direct_native_ir_round_trip(
                        original, editable, forward, reverse
                    )
                ):
                    continue
                if original in {"spirv", "opengl-spirv"}:
                    validator = self.strict_spirv_validator(original, stage)
                    strict["strictNativeIrValidationRequired"] = True
                    strict["requiredValidators"] = [validator.as_dict()]
                alternate = self.plan_recovery(
                    original,
                    stage,
                    editable_format=editable,
                    capture_encoding=capture_encoding,
                    target_api=target_api,
                    allow_reconstructed=False,
                )
            except ToolchainRoutingError:
                continue
            if (
                alternate.get("mode") != "round-trip"
                or alternate.get("decompileRouteIds")
                != strict.get("decompileRouteIds")
                or alternate.get("compileRouteIds")
                != strict.get("compileRouteIds")
            ):
                continue
            alternate["strictNativeIrValidationRequired"] = bool(
                strict.get("strictNativeIrValidationRequired")
            )
            alternate["requiredValidators"] = list(
                strict.get("requiredValidators", [])
            )
            alternate["diagnostics"] = [
                {
                    "phase": "editable-selection",
                    "category": "automatic-native-ir-round-trip-fallback",
                    "preferredEditableFormat": preferred.get("editableFormat"),
                    "preferredMode": preferred.get("mode"),
                    "selectedEditableFormat": alternate.get("editableFormat"),
                    "preferredDiagnostics": list(
                        preferred.get("diagnostics", [])
                    ),
                    "preferredStrictError": preferred_strict_error,
                    "reason": (
                        "the preferred high-level lane had no complete round trip; "
                        "the selected native IR lane has fixed decompile and compile routes"
                    ),
                },
                *list(alternate.get("diagnostics", [])),
            ]
            return alternate
        if preferred_strict_error:
            raise ToolchainRoutingError(
                "preferred automatic shader routes are ambiguous or inconsistent, "
                "and no unique direct native-IR round trip is available: %s"
                % preferred_strict_error
            )
        return preferred

    def select_round_trip(
        self,
        input_format: str,
        stage: str,
        *,
        editable_format: str | None = None,
        capture_encoding: str | None = None,
        target_api: str | None = None,
        allow_reconstructed: bool = True,
    ) -> dict[str, Any]:
        """Fail-closed automatic decompiler/compiler selection.

        The selection is deterministic only when exactly one best route exists
        in each direction.  Tool IDs remain available through ``matching_routes``
        for an explicit advanced workflow, but an AI intent path cannot silently
        pick between equally ranked implementations.
        """

        original = normalize_format(input_format)
        normalized_stage = normalize_stage(stage)
        if not isinstance(allow_reconstructed, bool):
            raise ToolchainRoutingError("allow_reconstructed must be a boolean")
        if original not in BINARY_FORMATS:
            raise ToolchainRoutingError(
                "automatic round-trip planning requires a captured binary shader format"
            )
        if capture_encoding is not None:
            encoding = normalize_format(capture_encoding)
            if encoding != original:
                raise ToolchainRoutingError(
                    "capture encoding %s contradicts detected container %s"
                    % (encoding, original)
                )
        api = _normalize_target_api(target_api)
        _require_format_for_api(api, original)
        if original in {"spirv", "opengl-spirv"} and not api:
            raise ToolchainRoutingError(
                "SPIR-V target API/environment cannot be derived from the module; "
                "capture metadata must identify the API"
            )
        explicit_editable = bool(str(editable_format or "").strip())
        if explicit_editable:
            editable_candidates = (normalize_format(editable_format),)
        elif original in {"dxbc", "dxil"}:
            editable_candidates = (
                "hlsl",
                *_NATIVE_EDITABLE_FALLBACKS.get(original, ()),
            )
        elif original in {"spirv", "opengl-spirv"}:
            editable_candidates = (
                *(("hlsl",) if original == "spirv" else ()),
                "glsl",
                *_NATIVE_EDITABLE_FALLBACKS.get(original, ()),
            )
        else:
            raise ToolchainRoutingError(
                "no safe default editable language exists for %s" % original
            )
        failures = []
        forward: tuple[RouteCapability, ...] | None = None
        reverse: tuple[RouteCapability, ...] | None = None
        strict_validator: ValidatorCapability | None = None
        editable = ""
        for candidate in editable_candidates:
            if candidate not in TEXT_FORMATS:
                raise ToolchainRoutingError(
                    "auto-route editable format must be textual"
                )
            try:
                candidate_forward = self.find_path(
                    original,
                    candidate,
                    normalized_stage,
                    allow_reconstructed=allow_reconstructed,
                    require_unique=True,
                    target_api=api,
                )
                candidate_reverse = self.find_path(
                    candidate,
                    original,
                    normalized_stage,
                    allow_reconstructed=allow_reconstructed,
                    require_unique=True,
                    target_api=api,
                )
            except ToolchainRoutingError as exc:
                if explicit_editable:
                    raise
                failures.append("%s: %s" % (candidate, exc))
                continue
            if original not in {"dxbc", "dxil"} and any(
                "target_profile"
                in self.registry.operation(
                    route.tool_id, route.operation
                ).placeholders
                for route in candidate_forward + candidate_reverse
            ):
                message = (
                    "%s: route requires a Direct3D target profile that cannot "
                    "be derived from this captured format" % candidate
                )
                if explicit_editable:
                    raise ToolchainRoutingError(message)
                failures.append(message)
                continue
            if (
                candidate in _NATIVE_EDITABLE_FALLBACKS.get(original, ())
                and not self._is_direct_native_ir_round_trip(
                    original,
                    candidate,
                    candidate_forward,
                    candidate_reverse,
                )
            ):
                message = (
                    "%s: native-IR editing requires one direct ir-disassembly "
                    "and one direct ir-assembly route" % candidate
                )
                if explicit_editable:
                    raise ToolchainRoutingError(message)
                failures.append(message)
                continue
            candidate_validator = None
            if (
                original in {"spirv", "opengl-spirv"}
                and candidate in _NATIVE_EDITABLE_FALLBACKS.get(original, ())
            ):
                try:
                    candidate_validator = self.strict_spirv_validator(
                        original, normalized_stage
                    )
                except ToolchainRoutingError as exc:
                    if explicit_editable:
                        raise
                    failures.append("%s: %s" % (candidate, exc))
                    continue
            editable = candidate
            forward = candidate_forward
            reverse = candidate_reverse
            strict_validator = candidate_validator
            break
        if forward is None or reverse is None:
            raise ToolchainRoutingError(
                "no complete automatic shader round trip is available (%s)"
                % "; ".join(failures)
            )
        if not api and any(
            "target_api"
            in self.registry.operation(item.tool_id, item.operation).placeholders
            for item in forward + reverse
        ):
            raise ToolchainRoutingError(
                "the selected route requires the physical target API; it will not "
                "be inferred from the shader container"
            )
        capabilities = [item.as_dict() for item in forward + reverse]
        return {
            "inputFormat": original,
            "captureEncoding": original,
            "targetApi": api or None,
            "stage": normalized_stage,
            "editableFormat": editable,
            "decompileRouteIds": [item.route_id for item in forward],
            "decompilerToolIds": [item.tool_id for item in forward],
            "compileRouteIds": [item.route_id for item in reverse],
            "compilerToolIds": [item.tool_id for item in reverse],
            "capabilities": capabilities,
            "why": [
                "container/encoding, stage, target API, and requested editable language all match",
                "every selected executable and fixed file passed a fresh SHA-256 integrity probe",
                "each direction had exactly one best trusted route; ambiguity fails closed",
                (
                    "the preferred high-level lane was unavailable, so a complete native-IR lane was selected"
                    if not explicit_editable
                    and editable in _NATIVE_EDITABLE_FALLBACKS.get(original, ())
                    else "the preferred or explicitly requested editable lane was selected"
                ),
            ],
            "containsReconstructedSource": any(item.is_reconstructed for item in forward),
            "strictNativeIrValidationRequired": strict_validator is not None,
            "requiredValidators": (
                [strict_validator.as_dict()]
                if strict_validator is not None
                else []
            ),
            "lane": "candidate",
            "directReplayApplyAuthorized": False,
            "semanticEquivalenceVerified": False,
            "replayAbiValidationStillRequired": True,
            "candidateEligibleAfterRoundTripOnly": True,
            "guarantee": (
                "Selection and tool availability only. No route can guarantee recovery of "
                "the original high-level source from arbitrary compiled shader bytes."
            ),
        }

    def select_round_trip_for_file(
        self,
        shader_path: str | Path,
        stage: str,
        *,
        editable_format: str | None = None,
        capture_encoding: str | None = None,
        target_api: str | None = None,
        allow_reconstructed: bool = True,
    ) -> dict[str, Any]:
        unused_shader, data = _read_shader_file(shader_path)
        detection = detect_shader_format(data, capture_encoding)
        if detection.format is None:
            raise ToolchainRoutingError("captured shader format could not be detected")
        selection = self.select_round_trip(
            detection.format,
            stage,
            editable_format=editable_format,
            capture_encoding=capture_encoding or detection.format,
            target_api=target_api,
            allow_reconstructed=allow_reconstructed,
        )
        profile = None
        if detection.format in {"dxbc", "dxil"}:
            from .profiles import extract_target_profile

            profile = extract_target_profile(data, stage).as_dict()
        if any(
            "target_profile"
            in self.registry.operation(item["toolId"], item["operation"]).placeholders
            for item in selection["capabilities"]
        ) and profile is None:
            raise ToolchainRoutingError(
                "the automatic route needs a compiler target profile that cannot be "
                "derived from this shader format"
            )
        selection["inputDetection"] = detection.as_dict()
        selection["targetProfile"] = profile
        return selection

    def plan_recovery_for_file(
        self,
        shader_path: str | Path,
        stage: str,
        *,
        editable_format: str | None = None,
        capture_encoding: str | None = None,
        target_api: str | None = None,
        allow_reconstructed: bool = True,
    ) -> dict[str, Any]:
        """Detect one file and return a non-mutating recovery plan."""

        source, data = _read_shader_file(shader_path)
        detection = detect_shader_format(data, capture_encoding)
        if detection.format is None:
            raise ToolchainRoutingError("captured shader format could not be detected")
        if editable_format is None or not str(editable_format).strip():
            plan = self.plan_automatic_recovery(
                detection.format,
                stage,
                capture_encoding=capture_encoding or detection.format,
                target_api=target_api,
                allow_reconstructed=allow_reconstructed,
            )
        else:
            plan = self.plan_recovery(
                detection.format,
                stage,
                editable_format=editable_format,
                capture_encoding=capture_encoding or detection.format,
                target_api=target_api,
                allow_reconstructed=allow_reconstructed,
            )
        profile = None
        if detection.format in {"dxbc", "dxil"}:
            from .profiles import extract_target_profile

            profile = extract_target_profile(data, stage).as_dict()
        plan.update(
            {
                "inputPath": str(source),
                "inputDetection": detection.as_dict(),
                "inputRawSha256": hashlib.sha256(data).hexdigest(),
                "targetProfile": profile,
            }
        )
        return plan

    def diagnostic_matrix(self, stage: str) -> dict[str, Any]:
        normalized_stage = normalize_stage(stage)
        if normalized_stage not in STAGES or normalized_stage == "*":
            raise ToolchainRoutingError("a concrete supported shader stage is required")
        integrity = self._integrity()
        replay_stage_supported = (
            normalized_stage not in _REPLAY_REPLACEMENT_BLOCKED_STAGES
        )
        if normalized_stage in _RAY_STAGES:
            replay_unsupported_reason = (
                "Ray shader source/IR can use a registered round trip, but replay "
                "replacement is fail-closed until the exact export, shader-group, "
                "pipeline/state-object, local/global root ABI, and SBT records can "
                "be rebuilt and validated as one rollback-capable transaction."
            )
        elif normalized_stage == "library":
            replay_unsupported_reason = (
                "Library IR can use a registered round trip, but replay replacement "
                "is fail-closed until every export and its state-object associations "
                "can be rebuilt and validated as one rollback-capable transaction."
            )
        else:
            replay_unsupported_reason = None
        ready_routes = tuple(
            route
            for route in self._routes
            if route.supports_stage(normalized_stage)
            and integrity[(route.tool_id, route.operation)]["available"]
        )

        def unique_direct_route(
            values: Sequence[RouteCapability], source: str, target: str
        ) -> tuple[RouteCapability | None, str]:
            if not values:
                return None, ""

            def score(route: RouteCapability) -> tuple[int, int, int]:
                return (
                    int(route.is_reconstructed),
                    int(route.fidelity == "custom-transform"),
                    route.priority,
                )

            best_score = min(score(route) for route in values)
            best = tuple(
                sorted(
                    (route for route in values if score(route) == best_score),
                    key=lambda route: route.route_id,
                )
            )
            if len(best) != 1:
                return (
                    None,
                    "Automatic route selection is ambiguous for %s -> %s: %s."
                    % (
                        source,
                        target,
                        ", ".join(route.route_id for route in best),
                    ),
                )
            return best[0], ""

        rows = []
        known_pairs = {(source, target) for source, target, unused in _KNOWN_MATRIX}
        all_pairs = list(_KNOWN_MATRIX)
        for route in self._routes:
            pair = (route.input_format, route.output_format)
            if pair not in known_pairs:
                all_pairs.append((pair[0], pair[1], "Custom configured format edge."))
                known_pairs.add(pair)
        for source, target, boundary in all_pairs:
            configured = [
                route
                for route in self._routes
                if route.input_format == source
                and route.output_format == target
                and route.supports_stage(normalized_stage)
            ]
            ready = [
                route
                for route in configured
                if integrity[(route.tool_id, route.operation)]["available"]
            ]
            reverse_ready = [
                route
                for route in self._routes
                if route.input_format == target
                and route.output_format == source
                and route.supports_stage(normalized_stage)
                and integrity[(route.tool_id, route.operation)]["available"]
            ]
            round_trip_ready = bool(ready and reverse_ready)
            selected_forward, forward_selection_error = unique_direct_route(
                ready, source, target
            )
            selected_reverse, reverse_selection_error = unique_direct_route(
                reverse_ready, target, source
            )
            unique_round_trip_ready = bool(
                selected_forward is not None and selected_reverse is not None
            )
            strict_spirv_native = (source, target) in {
                ("spirv", "spirv-asm"),
                ("opengl-spirv", "opengl-spirv-asm"),
            }
            validation_format = (
                source if source in BINARY_FORMATS and reverse_ready else target
            )
            configured_validators = self.matching_validators(
                validation_format, normalized_stage, require_integrity=False
            )
            ready_validators = self.matching_validators(
                validation_format, normalized_stage, require_integrity=True
            )
            strict_validator_ready = True
            strict_validator_reason = ""
            if strict_spirv_native and round_trip_ready:
                try:
                    self.strict_spirv_validator(source, normalized_stage)
                except ToolchainRoutingError as exc:
                    strict_validator_ready = False
                    strict_validator_reason = str(exc)
            automatic_round_trip_ready = bool(
                unique_round_trip_ready
                and strict_validator_ready
            )
            if not configured:
                unsupported_reason = (
                    "No provider declares this format edge for the selected stage."
                )
            elif not ready:
                unsupported_reason = (
                    "The declared provider failed its executable/fixed-file "
                    "integrity check."
                )
            elif forward_selection_error:
                unsupported_reason = forward_selection_error
            elif not reverse_ready:
                unsupported_reason = (
                    "The representation is explanatory only because no "
                    "integrity-valid reverse compiler/assembler is configured."
                )
            elif reverse_selection_error:
                unsupported_reason = reverse_selection_error
            elif not strict_validator_ready:
                unsupported_reason = strict_validator_reason
            else:
                unsupported_reason = None
            rows.append(
                {
                    "inputFormat": source,
                    "outputFormat": target,
                    "stage": normalized_stage,
                    "status": "ready" if ready else ("integrity-failed" if configured else "missing"),
                    "configuredRouteIds": [item.route_id for item in configured],
                    "readyRouteIds": [item.route_id for item in ready],
                    "roundTripReady": round_trip_ready,
                    "automaticRoundTripReady": automatic_round_trip_ready,
                    "reverseRouteIds": [item.route_id for item in reverse_ready],
                    "readyValidatorIds": [
                        item.validator_id for item in ready_validators
                    ],
                    "recognized": True,
                    "analyzable": bool(ready) or source in TEXT_FORMATS,
                    "explanatory": bool(ready),
                    "editable": automatic_round_trip_ready,
                    "compilable": bool(reverse_ready),
                    "externalValidatorConfigured": bool(configured_validators),
                    "externalValidatorReady": bool(ready_validators),
                    "externalValidatorExecuted": False,
                    "replayAbiValidated": False,
                    "replayReplaceable": False,
                    "workspaceStageSupported": True,
                    "replayStageSupported": replay_stage_supported,
                    "replayUnsupportedReason": replay_unsupported_reason,
                    "unsupportedReason": unsupported_reason,
                    "containsReconstructedSource": any(item.is_reconstructed for item in ready),
                    "boundary": boundary,
                    "lane": (
                        "executable-editable"
                        if automatic_round_trip_ready
                        and target
                        in {
                            "dxbc-asm",
                            "dxil-asm",
                            "spirv-asm",
                            "opengl-spirv-asm",
                        }
                        else (
                            "explanation"
                            if target
                            in {
                                "dxbc-asm",
                                "dxil-asm",
                                "spirv-asm",
                                "opengl-spirv-asm",
                            }
                            else "candidate"
                        )
                    ),
                    "directReplayApplyAuthorized": False,
                    "replayAbiValidationStillRequired": target
                    in {"dxbc", "dxil", "spirv", "opengl-spirv"},
                }
            )

        format_capabilities = []
        content_detection = {
            "dxbc": "exact-container",
            "dxil": "exact-container",
            "spirv": "strong-header-plus-structural-walk-at-artifact-gates",
            "opengl-spirv": "capture-declared-environment-plus-physical-spirv",
            "dxbc-asm": "strong-text-markers",
            "dxil-asm": "strong-text-markers",
            "spirv-asm": "strong-text-markers",
            "opengl-spirv-asm": "capture-declared-environment-plus-text-markers",
            "hlsl": "conservative-source-markers",
            "glsl": "conservative-source-markers",
            "essl": "conservative-source-markers",
            "msl": "conservative-source-markers",
            "wgsl": "conservative-source-markers",
            "slang": "declared-vocabulary-only",
        }
        for shader_format in sorted(FORMATS):
            outgoing = tuple(
                route for route in ready_routes if route.input_format == shader_format
            )
            compiling = tuple(
                route
                for route in outgoing
                if route.output_format in BINARY_FORMATS
            )
            validators_for_format = self.matching_validators(
                shader_format, normalized_stage, require_integrity=True
            )
            vocabulary_only = shader_format == "slang" or (
                not outgoing
                and shader_format in {"essl", "msl", "wgsl", "slang"}
            )
            uniquely_editable_rows = tuple(
                row
                for row in rows
                if row["outputFormat"] == shader_format
                and row["automaticRoundTripReady"] is True
            )
            format_capabilities.append(
                {
                    "format": shader_format,
                    "recognized": True,
                    "contentDetection": content_detection[shader_format],
                    "analyzable": bool(outgoing) or (
                        shader_format in TEXT_FORMATS and not vocabulary_only
                    ),
                    "explanatory": bool(outgoing),
                    "editable": (
                        shader_format in TEXT_FORMATS
                        and bool(uniquely_editable_rows)
                    ),
                    "compilable": bool(compiling),
                    "externalValidatorReady": bool(validators_for_format),
                    "replayAbiValidated": False,
                    "replayReplaceable": False,
                    "workspaceStageSupported": True,
                    "replayStageSupported": replay_stage_supported,
                    "replayUnsupportedReason": replay_unsupported_reason,
                    "readyOutgoingRouteIds": [
                        route.route_id for route in outgoing
                    ],
                    "unsupportedReason": (
                        "The format name is recognized, but no integrity-valid "
                        "provider route exists for this stage; recognition is not "
                        "an end-to-end decompile/compile capability."
                        if not outgoing
                        else (
                            "No uniquely selectable complete round trip makes this "
                            "text format automatically editable."
                            if shader_format in TEXT_FORMATS
                            and not uniquely_editable_rows
                            else None
                        )
                    ),
                }
            )
        return {
            "schema": "renderdoc-ultra-shader-toolchain-diagnostics/v1",
            "schemaVersion": 1,
            "toolRegistryPath": str(self.registry.path),
            "toolRegistrySha256": self.tool_registry_sha256,
            "routeCatalogPath": str(self.path),
            "stage": normalized_stage,
            "operations": [
                integrity[key] for key in sorted(integrity, key=lambda item: (item[0], item[1]))
            ],
            "capabilityAxes": {
                "recognized": "The format identifier is accepted by the router; this alone is not analysis or conversion.",
                "analyzable": "Text can be inspected or an integrity-valid outgoing provider exists; this does not prove a semantic explanation.",
                "explanatory": "A ready forward route can emit evidence for review; it may be one-way.",
                "editable": "Exactly one best route is selectable in each direction and every strict static prerequisite is present; workspace probes must still run.",
                "compilable": "A ready route can emit a binary artifact; no validator, ABI, or replay claim is implied.",
                "externalValidator": "A fixed validator is configured and integrity-valid; diagnostics do not execute it.",
                "replayAbi": "Always false here; only fresh capture-bound replay reflection may validate ABI.",
                "replayReplaceable": "Always false here; route diagnostics never authorize or perform replacement.",
                "workspaceStageSupported": "The stage may use a real registered source/IR round trip; Ray/Library can be true here independently of replay replacement.",
                "replayStageSupported": "The stage class is admitted to the ordinary replay replacement transaction; Ray/Library are false until their export/state-object/SBT transaction exists.",
                "replayUnsupportedReason": "A stage-level replay boundary kept separate from route-level unsupportedReason.",
            },
            "formatCapabilities": format_capabilities,
            "matrix": rows,
            "guarantee": (
                "Availability and format routing only. Reconstructed source is not the original "
                "source, and successful compilation does not prove replay ABI compatibility."
            ),
        }

    @staticmethod
    def _require_process_success(result: ProcessResult) -> None:
        if result.launch_error:
            raise ToolExecutionError("external tool launch/integrity failure: %s" % result.launch_error)
        if result.timed_out:
            raise ToolExecutionError("external tool timed out")
        if result.return_code != 0:
            raise ToolExecutionError("external tool exited with code %s" % result.return_code)

    def _validate_file(
        self,
        shader_path: Path,
        shader_format: str,
        stage: str,
        run_root: Path,
        baseline_path: Path,
        target_profile: str,
        target_api: str,
        start_index: int,
    ) -> tuple[list[dict[str, Any]], int]:
        records: list[dict[str, Any]] = []
        validators = self.matching_validators(shader_format, stage)
        baseline_hash = _sha256_file(baseline_path)
        for validator in validators:
            job_dir = run_root / "jobs" / (
                "%03d-validate-%s" % (start_index, validator.validator_id)
            )
            job_dir.mkdir(parents=True, exist_ok=False)
            staged_input = job_dir / ("input" + _FORMAT_SUFFIX[shader_format])
            atomic_write_bytes(staged_input, shader_path.read_bytes())
            operation = self.registry.operation(validator.tool_id, "validate")
            result = run_operation(
                operation,
                {
                    "input": str(staged_input),
                    "output": str(job_dir / "unused-output"),
                    "baseline": str(baseline_path),
                    "job_dir": str(job_dir),
                    "workspace_dir": str(run_root),
                    "stage": stage,
                    "entry_point": "main",
                    "captured_entry_point": "main",
                    "revision": str(start_index),
                    "capture_id": "round-trip-smoke",
                    "raw_sha256": baseline_hash,
                    "target_profile": target_profile,
                    "target_api": target_api,
                },
                job_dir,
            )
            self._require_process_success(result)
            records.append(
                {
                    "validator": validator.as_dict(),
                    "jobPath": str(job_dir),
                    "inputSha256": _sha256_file(staged_input),
                    "stdoutPath": str(result.stdout_path),
                    "stderrPath": str(result.stderr_path),
                    "passed": True,
                }
            )
            start_index += 1
        return records, start_index

    def _execute_path(
        self,
        initial_path: Path,
        initial_format: str,
        path: Sequence[RouteCapability],
        run_root: Path,
        stage: str,
        entry_point: str,
        baseline_path: Path,
        target_profile: str,
        target_api: str,
        job_index: int,
    ) -> tuple[Path, str, list[dict[str, Any]], int, str]:
        current_path = initial_path
        current_format = initial_format
        current_entry_point = str(entry_point)
        jobs: list[dict[str, Any]] = []
        baseline_hash = _sha256_file(baseline_path)
        for route in path:
            if route.input_format != current_format:
                raise ToolchainRoutingError("route path is not contiguous")
            integrity = self.registry.operation_integrity(route.tool_id, route.operation)
            if not integrity["available"]:
                raise ToolExecutionError(
                    "tool integrity changed before smoke execution: %s" % route.route_id
                )
            operation = self.registry.operation(route.tool_id, route.operation)
            job_dir = run_root / "jobs" / ("%03d-%s" % (job_index, route.route_id))
            job_dir.mkdir(parents=True, exist_ok=False)
            staged_input = job_dir / ("input" + _FORMAT_SUFFIX[current_format])
            atomic_write_bytes(staged_input, current_path.read_bytes())
            output_path = job_dir / ("output" + _FORMAT_SUFFIX[route.output_format])
            hop_entry_point = current_entry_point
            if current_format in {"spirv", "opengl-spirv"}:
                hop_entry_point = physical_spirv_entry_point(
                    staged_input.read_bytes(), stage, current_entry_point
                )
            values = {
                "input": str(staged_input),
                "output": str(output_path),
                "baseline": str(baseline_path),
                "job_dir": str(job_dir),
                "workspace_dir": str(run_root),
                "stage": stage,
                "entry_point": hop_entry_point,
                "captured_entry_point": str(entry_point),
                "revision": str(job_index),
                "capture_id": "round-trip-smoke",
                "raw_sha256": baseline_hash,
                "target_profile": target_profile,
                "target_api": target_api,
            }
            result = run_operation(operation, values, job_dir)
            self._require_process_success(result)
            if output_path.is_symlink() or not output_path.is_file():
                raise ToolExecutionError("route %s did not create its declared output" % route.route_id)
            size = output_path.stat().st_size
            if size <= 0 or size > MAX_SHADER_BYTES:
                raise ToolExecutionError(
                    "route %s output must contain 1..%d bytes" % (route.route_id, MAX_SHADER_BYTES)
                )
            # SPIR-V binary/assembly does not encode whether the consumer is
            # Vulkan or OpenGL. Preserve the route's capture-derived semantic
            # encoding while still rejecting any physically contradictory
            # output.
            detected = detect_shader_file(output_path, route.output_format)
            if detected.format != route.output_format:
                raise ToolExecutionError(
                    "route %s declared %s but output detected as %s"
                    % (route.route_id, route.output_format, detected.format or "unknown")
                )
            jobs.append(
                {
                    "route": route.as_dict(),
                    "jobPath": str(job_dir),
                    "inputSha256": _sha256_file(staged_input),
                    "outputSha256": _sha256_file(output_path),
                    "outputDetection": detected.as_dict(),
                    "stdoutPath": str(result.stdout_path),
                    "stderrPath": str(result.stderr_path),
                }
            )
            current_path = output_path
            current_format = route.output_format
            if route.output_format in {"spirv", "opengl-spirv"}:
                current_entry_point = physical_spirv_entry_point(
                    output_path.read_bytes(), stage, hop_entry_point
                )
                jobs[-1]["physicalEntryPoint"] = current_entry_point
            elif (
                route.input_format in {"spirv", "opengl-spirv"}
                and route.output_format == "hlsl"
            ):
                # Only routes whose pinned operation consumes the separate
                # capture-name placeholder claim to rename the emitted HLSL
                # entry.  Generic/legacy routes retain the parsed physical
                # SPIR-V entry for the following compile hop.
                if "captured_entry_point" in operation.placeholders:
                    current_entry_point = str(entry_point)
                else:
                    current_entry_point = hop_entry_point
                jobs[-1]["physicalEntryPoint"] = hop_entry_point
                jobs[-1]["emittedEntryPoint"] = current_entry_point
            else:
                current_entry_point = hop_entry_point
            job_index += 1
        return current_path, current_format, jobs, job_index, current_entry_point

    def round_trip_smoke(
        self,
        input_path: str | Path,
        editable_format: str,
        output_directory: str | Path,
        stage: str,
        entry_point: str = "main",
        *,
        input_format_hint: str | None = None,
        allow_reconstructed: bool = True,
        target_profile: str | None = None,
        target_api: str | None = None,
    ) -> dict[str, Any]:
        """Run binary -> editable -> binary and record format-level evidence.

        ``formatSmokePassed`` only means both fixed processes succeeded and each
        output matched its declared container/text format.  It explicitly does
        not mean the reconstructed source or final shader is semantically or ABI
        equivalent to the capture.
        """

        source, source_bytes = _read_shader_file(input_path)
        if not isinstance(entry_point, str) or not entry_point.strip():
            raise ToolchainRoutingError("entry_point must be a non-empty string")
        if len(entry_point) > 512 or "\x00" in entry_point:
            raise ToolchainRoutingError("entry_point is too large or contains NUL")
        detection = detect_shader_format(source_bytes, input_format_hint)
        if detection.format is None:
            raise ToolchainRoutingError("input shader format is unknown; supply a non-conflicting hint")
        original_format = detection.format
        editable = normalize_format(editable_format)
        normalized_stage = normalize_stage(stage)
        target_api_value = _normalize_target_api(target_api)
        _require_format_for_api(target_api_value, original_format)
        if original_format in {"spirv", "opengl-spirv"} and not target_api_value:
            raise ToolchainRoutingError(
                "SPIR-V target API/environment cannot be derived from the module; "
                "capture metadata must identify the API"
            )
        from .profiles import extract_target_profile, validate_target_profile

        target_profile_details = None
        if original_format in {"dxbc", "dxil"}:
            parsed_profile = extract_target_profile(source_bytes, normalized_stage)
            target_profile_details = parsed_profile.as_dict()
            target_profile_value = parsed_profile.profile
            if target_profile is not None:
                asserted_profile = validate_target_profile(
                    target_profile, normalized_stage
                )
                if asserted_profile != target_profile_value:
                    raise ToolchainRoutingError(
                        "caller target profile contradicts the captured executable token"
                    )
        else:
            target_profile_value = ""
            if target_profile is not None:
                raise ToolchainRoutingError(
                    "target profiles are read-only facts derived from captured "
                    "DXBC/DXIL executable tokens; this input has no such token"
                )
        if editable not in TEXT_FORMATS:
            raise ToolchainRoutingError("editable_format must be a textual shader representation")
        forward = self.find_path(
            original_format,
            editable,
            normalized_stage,
            allow_reconstructed=allow_reconstructed,
            target_api=target_api_value,
        )
        reverse = self.find_path(
            editable,
            original_format,
            normalized_stage,
            allow_reconstructed=allow_reconstructed,
            target_api=target_api_value,
        )
        if not target_profile_value and any(
            "target_profile"
            in self.registry.operation(item.tool_id, item.operation).placeholders
            for item in forward + reverse
        ):
            raise ToolchainRoutingError(
                "the selected route requires an exact target profile, but it cannot "
                "be derived from this captured executable token"
            )
        if not target_api_value and any(
            "target_api"
            in self.registry.operation(item.tool_id, item.operation).placeholders
            for item in forward + reverse
        ):
            raise ToolchainRoutingError(
                "the selected route requires the physical target API, but it was "
                "not supplied by capture metadata"
            )
        destination = Path(output_directory).expanduser().resolve()
        if destination.exists():
            raise ToolchainRoutingError("round-trip output directory already exists")
        destination.mkdir(parents=True)
        (destination / "jobs").mkdir()
        original_copy = destination / ("original" + _FORMAT_SUFFIX[original_format])
        atomic_write_bytes(original_copy, source_bytes)
        report: dict[str, Any] = {
            "schema": SMOKE_SCHEMA,
            "schemaVersion": 1,
            "inputPath": str(source),
            "inputDetection": detection.as_dict(),
            "originalSha256": _sha256_file(original_copy),
            "stage": normalized_stage,
            "entryPoint": str(entry_point),
            "targetProfile": target_profile_value or None,
            "targetProfileDetails": target_profile_details,
            "targetApi": target_api_value or None,
            "editableFormat": editable,
            "forwardRouteIds": [item.route_id for item in forward],
            "reverseRouteIds": [item.route_id for item in reverse],
            "jobs": [],
            "formatSmokePassed": False,
            "byteIdentical": False,
            "semanticEquivalenceVerified": False,
            "replayAbiValidated": False,
            "externalValidationConfigured": False,
            "externalValidationPassed": False,
            "validatorJobs": [],
            "assurance": "process-and-format-only",
            "error": None,
        }
        try:
            (
                middle,
                middle_format,
                jobs,
                next_index,
                middle_entry_point,
            ) = self._execute_path(
                original_copy,
                original_format,
                forward,
                destination,
                normalized_stage,
                str(entry_point),
                original_copy,
                target_profile_value,
                target_api_value,
                1,
            )
            (
                final,
                final_format,
                reverse_jobs,
                unused_index,
                _final_entry_point,
            ) = self._execute_path(
                middle,
                middle_format,
                reverse,
                destination,
                normalized_stage,
                middle_entry_point,
                original_copy,
                target_profile_value,
                target_api_value,
                next_index,
            )
            jobs.extend(reverse_jobs)
            final_copy = destination / ("round-trip" + _FORMAT_SUFFIX[final_format])
            atomic_write_bytes(final_copy, final.read_bytes())
            validator_jobs, unused_index = self._validate_file(
                final_copy,
                final_format,
                normalized_stage,
                destination,
                original_copy,
                target_profile_value,
                target_api_value,
                unused_index,
            )
            report.update(
                {
                    "jobs": jobs,
                    "finalPath": str(final_copy),
                    "finalFormat": final_format,
                    "finalSha256": _sha256_file(final_copy),
                    "formatSmokePassed": final_format == original_format,
                    "byteIdentical": original_copy.read_bytes() == final_copy.read_bytes(),
                    "externalValidationConfigured": bool(validator_jobs),
                    "externalValidationPassed": bool(validator_jobs),
                    "validatorJobs": validator_jobs,
                }
            )
        except ShaderWorkspaceError as exc:
            report["error"] = "%s: %s" % (type(exc).__name__, exc)
        atomic_write_json(destination / "smoke-report.json", report)
        return report


def _default_route_target_apis(
    input_format: str, output_format: str
) -> tuple[str, ...]:
    """Derive conservative physical-API applicability for generated routes.

    Custom route documents still carry the resulting explicit ``targetApis``
    field.  The derivation exists only in the trusted config builder so the
    runtime never guesses applicability from a route selected by an agent.
    """

    source = normalize_format(input_format)
    target = normalize_format(output_format)
    formats = {source, target}
    if formats & {"opengl-spirv", "opengl-spirv-asm"}:
        return ("opengl",)
    if formats & {"dxil", "dxil-asm"}:
        return ("d3d12",)
    if formats & {"dxbc", "dxbc-asm"}:
        return ("d3d11", "d3d12")
    if formats & {"spirv", "spirv-asm"}:
        # SPIRV-Cross HLSL is used only as a Direct3D reconstruction step and
        # requires an exact Direct3D profile.  Other generic SPIR-V routes are
        # Vulkan-environment routes; OpenGL has distinct format identifiers.
        if target == "hlsl":
            return ("d3d11", "d3d12")
        return ("vulkan",)
    return ("d3d11", "d3d12", "opengl", "vulkan")


def route_record(
    registry: ToolRegistry,
    *,
    route_id: str,
    tool_id: str,
    operation: str,
    input_format: str,
    output_format: str,
    stages: Iterable[str],
    fidelity: str,
    priority: int,
    implementation: str,
    source_url: str,
    target_apis: Iterable[str] | None = None,
    license_name: str = "",
    caveats: Iterable[str] = (),
) -> dict[str, Any]:
    """Build one digest-bound route record for trusted config generators."""

    operation_record: ToolOperation = registry.operation(tool_id, operation)
    selected_target_apis = tuple(
        target_apis
        if target_apis is not None
        else _default_route_target_apis(input_format, output_format)
    )
    return {
        "routeId": route_id,
        "toolId": tool_id,
        "operation": operation,
        "toolConfigDigest": operation_record.config_digest,
        "inputFormat": input_format,
        "outputFormat": output_format,
        "stages": list(stages),
        "targetApis": list(selected_target_apis),
        "fidelity": fidelity,
        "priority": priority,
        "implementation": implementation,
        "sourceUrl": source_url,
        "license": license_name,
        "caveats": list(caveats),
    }


def validator_record(
    registry: ToolRegistry,
    *,
    validator_id: str,
    tool_id: str,
    input_format: str,
    stages: Iterable[str],
    priority: int,
    implementation: str,
    source_url: str,
    license_name: str = "",
    caveats: Iterable[str] = (),
) -> dict[str, Any]:
    operation = registry.operation(tool_id, "validate")
    return {
        "validatorId": validator_id,
        "toolId": tool_id,
        "toolConfigDigest": operation.config_digest,
        "inputFormat": input_format,
        "stages": list(stages),
        "priority": priority,
        "implementation": implementation,
        "sourceUrl": source_url,
        "license": license_name,
        "caveats": list(caveats),
    }


def toolchain_document(
    tool_config_path: str | Path,
    routes: Iterable[Mapping[str, Any]],
    validators: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Create a route document bound to the exact bytes of a tool registry."""

    path = Path(tool_config_path).expanduser().resolve()
    return {
        "schema": TOOLCHAIN_SCHEMA,
        "schemaVersion": TOOLCHAIN_VERSION,
        "toolRegistrySha256": _sha256_file(path),
        "routes": [dict(item) for item in routes],
        "validators": [dict(item) for item in validators],
    }
