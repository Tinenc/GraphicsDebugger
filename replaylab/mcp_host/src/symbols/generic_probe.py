"""Engine-agnostic content probe for any unpacked asset file.

The generic layer is what every engine gets, recognized or not: readable
identifiers and paths inside the bytes (names, parameters, referenced
assets), distinctive numbers (parameter values that can be matched against
constant-buffer lanes), embedded images (fingerprinted for texture identity)
and embedded shader containers.  Engine-specific readers only *add*
precision on top — exact import tables, mounts, classes, mip tables — they
are never required for retrieval to work.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath
import re
from typing import Any

from .engines.unreal_package import numeric_fingerprints
from .imaging import ImageDecodeError, decode_dds, decode_png, fingerprint

PROBE_SCHEMA = "renderdoc-ultra-generic-content-probe/v1"
MAX_PROBE_BYTES = 4 * 1024 * 1024
MAX_STRINGS = 384
MAX_PATH_REFERENCES = 128
MAX_EMBEDDED_IMAGES = 4
_ASCII = re.compile(rb"[A-Za-z_][A-Za-z0-9_./\\:-]{3,255}")
_UTF16 = re.compile(rb"(?:[A-Za-z_]\x00)(?:[A-Za-z0-9_./\\:-]\x00){3,255}")
_PATHLIKE = re.compile(r"^(?:[A-Za-z]:)?[/\\]?(?:[A-Za-z0-9_.-]+[/\\])+[A-Za-z0-9_.-]+$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{3,127}$")
_STOP = {
    "None", "null", "true", "false", "Class", "Object", "Package", "Script", "Engine", "Default",
    "float", "float2", "float3", "float4", "int", "uint", "bool", "half", "void", "return",
    "struct", "const", "static", "inline", "Texture2D", "SamplerState", "cbuffer", "register",
}
_MAGICS = (
    (b"\x89PNG\r\n\x1a\n", "png"), (b"DDS ", "dds"), (b"\xabKTX 20\xbb\r\n\x1a\n", "ktx2"),
    (b"\xabKTX 11\xbb\r\n\x1a\n", "ktx"), (b"\xff\xd8\xff", "jpeg"), (b"RIFF", "riff"),
)
_SHADER_MAGICS = ((b"DXBC", "dxbc"), (b"\x03\x02\x23\x07", "spirv"), (b"MTLB", "metallib"), (b"#version", "glsl"))


def _decode_strings(data: bytes) -> tuple[list[str], list[str]]:
    """Identifier-like strings and path-like strings, most frequent first."""

    counts: dict[str, int] = {}
    paths: dict[str, int] = {}

    def take(text: str) -> None:
        if len(text) > 255:
            return
        if _PATHLIKE.match(text) and ("/" in text or "\\" in text):
            paths[text] = paths.get(text, 0) + 1
            return
        if _IDENT.match(text) and text not in _STOP and not text.isupper() or (
                text.isupper() and "_" in text and len(text) >= 6 and _IDENT.match(text)):
            counts[text] = counts.get(text, 0) + 1

    for match in _ASCII.finditer(data):
        take(match.group(0).decode("ascii", errors="ignore"))
    for match in _UTF16.finditer(data):
        try:
            take(match.group(0).decode("utf-16-le", errors="strict"))
        except UnicodeDecodeError:
            continue
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [text for text, _ in ranked[:MAX_STRINGS]], [text for text, _ in sorted(paths.items(), key=lambda item: -item[1])[:MAX_PATH_REFERENCES]]


def _embedded_images(data: bytes) -> list[dict[str, Any]]:
    """Fingerprint small images embedded in a blob (PNG/DDS payloads)."""

    found: list[dict[str, Any]] = []
    for magic, kind in _MAGICS[:2]:
        start = 0
        while len(found) < MAX_EMBEDDED_IMAGES:
            offset = data.find(magic, start)
            if offset < 0:
                break
            start = offset + 1
            try:
                if kind == "png":
                    end = data.find(b"IEND", offset)
                    if end < 0:
                        continue
                    rgba, width, height = decode_png(data[offset:end + 8])
                    fmt = "PNG"
                else:
                    rgba, width, height, fmt = decode_dds(data[offset:offset + 4 * 1024 * 1024])
                if width * height > 512 * 512:
                    found.append({"offset": offset, "kind": kind, "available": False,
                                  "reason": "embedded image larger than 512x512; skipped"})
                    continue
                item = fingerprint(rgba, width, height)
                item.update({"offset": offset, "kind": kind, "available": True, "pixelFormat": fmt,
                             "sizeX": width, "sizeY": height})
                found.append(item)
            except (ImageDecodeError, ValueError, IndexError, Exception):
                continue
    return found


def _embedded_shaders(data: bytes) -> dict[str, int]:
    return {kind: data.count(magic) for magic, kind in _SHADER_MAGICS if data.count(magic)}


def probe(data: bytes, relative: str, *, numeric: bool = True, images: bool = True) -> dict[str, Any]:
    """Generic evidence for one file; bounded to the first 4 MiB."""

    window = data[:MAX_PROBE_BYTES]
    strings, paths = _decode_strings(window)
    result: dict[str, Any] = {
        "probe": PROBE_SCHEMA,
        "probedBytes": len(window),
        "probeTruncated": len(data) > len(window),
        "strings": strings,
        "pathReferences": paths,
        "embeddedShaders": _embedded_shaders(window),
    }
    if numeric:
        numbers = numeric_fingerprints(window)
        result["numericValues"] = numbers["values"]
        result["numericRuns"] = numbers["runs"]
    if images:
        embedded = _embedded_images(window)
        if embedded:
            result["embeddedImages"] = [{k: v for k, v in item.items() if k != "thumbnail"} for item in embedded]
            first = next((item for item in embedded if item.get("available")), None)
            if first is not None and "textureFingerprint" not in result:
                result["textureFingerprint"] = {k: v for k, v in first.items() if k not in ("offset", "kind")}
    return result


def mention_references(strings: Iterable[str], paths: Iterable[str], stem_index: Mapping[str, str],
                       own_stem: str) -> list[dict[str, Any]]:
    """Turn strings that equal another catalog file's stem into heuristic relations."""

    references: list[dict[str, Any]] = []
    seen: set[str] = set()
    for text in list(strings) + [PurePosixPath(str(path).replace("\\", "/")).stem for path in paths]:
        key = str(text).casefold()
        if len(key) < 6 or key == own_stem.casefold() or key in seen:
            continue
        target = stem_index.get(key)
        if not target:
            continue
        seen.add(key)
        references.append({
            "relation": "mentions-asset",
            "catalogRootRelative": True,
            "targetPath": target,
            "evidence": {"mention": str(text)[:128], "derivation": "string-equals-file-stem", "heuristic": True},
        })
        if len(references) >= 64:
            break
    return references
