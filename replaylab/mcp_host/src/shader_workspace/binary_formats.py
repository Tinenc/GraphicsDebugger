"""Physical executable fingerprints for portable shader workspace baselines.

This module validates container structure only.  It deliberately does not
infer a Vulkan/OpenGL environment from SPIR-V bytes, and it does not claim that
a structurally valid executable has a compatible replay ABI.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from typing import Any

from ..symbols.dxbc import ShaderContainer, fingerprint_bytes


@dataclass(frozen=True)
class PortableShaderExecutable:
    raw_sha256: str
    code_sha256: str
    byte_size: int
    kind: str
    shader_format: str
    chunks: tuple[Any, ...] = ()


def _normalise_hint(value: str | None) -> str:
    hint = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "spv": "spirv",
        "spir-v": "spirv",
        "vulkan-spirv": "spirv",
        "openglspv": "opengl-spirv",
        "openglspirv": "opengl-spirv",
        "opengl-spv": "opengl-spirv",
    }
    return aliases.get(hint, hint)


def _valid_directx(container: ShaderContainer | None, size: int) -> bool:
    return bool(
        container is not None
        and container.byte_size == size
        and any(
            chunk.fourcc in {"SHDR", "SHEX", "DXIL"}
            for chunk in container.chunks
        )
    )


def _fingerprint_spirv(
    data: bytes, *, semantic_format: str
) -> PortableShaderExecutable | None:
    if len(data) < 20 or len(data) % 4:
        return None
    little_magic = struct.unpack_from("<I", data, 0)[0]
    if little_magic == 0x07230203:
        endian = "<"
    elif little_magic == 0x03022307:
        endian = ">"
    else:
        return None
    words = struct.unpack(endian + "%dI" % (len(data) // 4), data)
    if words[0] != 0x07230203:
        return None
    version = words[1]
    major = (version >> 16) & 0xFF
    minor = (version >> 8) & 0xFF
    if major != 1 or minor > 6 or words[3] == 0 or words[4] != 0:
        return None
    cursor = 5
    while cursor < len(words):
        word_count = words[cursor] >> 16
        if word_count <= 0 or cursor + word_count > len(words):
            return None
        cursor += word_count
    digest = hashlib.sha256(data).hexdigest()
    return PortableShaderExecutable(
        raw_sha256=digest,
        # Until a format-specific semantic normalizer is explicitly registered,
        # the complete module is the only safe program identity.
        code_sha256=digest,
        byte_size=len(data),
        kind="OPENGL-SPIRV" if semantic_format == "opengl-spirv" else "SPIRV",
        shader_format=semantic_format,
    )


def fingerprint_shader_executable(
    data: bytes,
    label: str,
    *,
    format_hint: str | None = None,
) -> ShaderContainer | PortableShaderExecutable | None:
    """Return one exact supported executable or ``None``.

    DXBC/DXIL parsing reuses the existing strict container scanner. SPIR-V is
    structurally walked so truncated/zero-word-count instruction streams fail.
    ``format_hint`` may distinguish OpenGL SPIR-V from Vulkan SPIR-V, but may
    not contradict DirectX bytes.
    """

    hint = _normalise_hint(format_hint)
    if hint == "glsl":
        # This is source identity, never a claim that text has compiled. The
        # native build receipt and fresh reflection are separate mandatory gates.
        if not data or len(data) > 4 * 1024 * 1024 or b"\0" in data:
            return None
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeError:
            return None
        if not text.strip():
            return None
        digest = hashlib.sha256(data).hexdigest()
        return PortableShaderExecutable(digest, digest, len(data), "GLSL", "glsl")
    directx = fingerprint_bytes(data, label)
    if _valid_directx(directx, len(data)):
        detected = "dxil" if directx.kind == "DXIL" else "dxbc"
        if hint and hint != detected:
            return None
        return directx
    if hint and hint not in {"spirv", "opengl-spirv"}:
        return None
    return _fingerprint_spirv(data, semantic_format=hint or "spirv")


def executable_format(
    executable: ShaderContainer | PortableShaderExecutable,
) -> str:
    if isinstance(executable, PortableShaderExecutable):
        return executable.shader_format
    return "dxil" if executable.kind == "DXIL" else "dxbc"


def api_semantic_format(api: str, detected_format: str) -> str:
    """Bind identical SPIR-V words to their capture-declared environment."""

    token = str(api or "").strip().casefold().replace(" ", "")
    if token.startswith("graphicsapi."):
        token = token[len("graphicsapi.") :]
    if detected_format == "spirv" and token in {"opengl", "gl"}:
        return "opengl-spirv"
    return detected_format
