"""Deterministic project-owned catalog for unpacked game assets.

The catalog is intentionally broader than the Shader symbol database.  It
indexes ordinary asset files and only records relationships that fixed parsers
can point back to bytes or exact path/GUID references.  Semantic guesses remain
an Agent inference and are never written as verified edges.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import struct
import time
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlparse
import uuid
import zlib


CATALOG_SCHEMA = "renderdoc-ultra-unpacked-asset-catalog/v1"
CATALOG_VERSION = 1
DATABASE_SCHEMA_VERSION = 2
CATALOG_FILENAME = "asset-catalog-v1.sqlite3"
CONTENT_MANIFEST_SCHEMA = "renderdoc-ultra-asset-content-manifest/v1"
CONTENT_MANIFEST_FILENAME = "asset-content-manifest-v1.json"

MAX_QUERY_RESULTS = 100
MAX_QUERY_IDS = 100
MAX_RELATIONSHIPS_PER_ASSET = 256
MAX_INDEXED_REFERENCES_PER_ASSET = 256
MAX_PARSE_BYTES = 16 * 1024 * 1024
HARD_MAX_FILES = 500_000
DEFAULT_MAX_FILES = HARD_MAX_FILES
# A path-only catalog row is cheap (no content read/hash), so a large unpacked
# tree may legitimately contain more names than a headers/full scan should ever
# admit. Keep the deep-scan bound unchanged and give names-only its own bound.
DEFAULT_NAMES_ONLY_MAX_FILES = 1_000_000
HARD_NAMES_ONLY_MAX_FILES = DEFAULT_NAMES_ONLY_MAX_FILES
# ``max_files`` limits catalog rows. This independent bound limits the actual
# directory walk, including directories and files that names-only deliberately
# ignores. It prevents an accidentally selected build/output tree from turning
# a cheap activation scan into an effectively unbounded walk.
HARD_MAX_FILESYSTEM_ENTRIES = 2_000_000
ENGINE_DETECTION_SAMPLE_FILES = 50_000
DEFAULT_MAX_FILE_SIZE_MB = 4096
HARD_MAX_FILE_SIZE_MB = 16_384
HASH_CHUNK_BYTES = 4 * 1024 * 1024

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ASSET_ID_RE = re.compile(r"^asset-v1-[0-9a-f]{64}$")
_UNITY_GUID_RE = re.compile(r"(?m)^\s*guid:\s*([0-9a-fA-F]{32})\s*$")
_UNITY_REFERENCE_GUID_RE = re.compile(r"\bguid:\s*([0-9a-fA-F]{32})\b")
_SHADER_INCLUDE_RE = re.compile(
    r"(?m)^\s*#\s*include\s*[<\"]([^>\"\r\n]+)[>\"]"
)

_KIND_EXTENSIONS: dict[str, frozenset[str]] = {
    "mesh": frozenset(
        {".obj", ".fbx", ".gltf", ".glb", ".dae", ".ply", ".stl", ".abc"}
    ),
    "scene": frozenset({".blend", ".usd", ".usda", ".usdc"}),
    "texture": frozenset(
        {
            ".png",
            ".jpg",
            ".jpeg",
            ".dds",
            ".ktx",
            ".ktx2",
            ".tga",
            ".bmp",
            ".tif",
            ".tiff",
            ".exr",
            ".hdr",
            ".webp",
        }
    ),
    "material": frozenset({".mtl", ".mat", ".material"}),
    "shader": frozenset(
        {
            ".hlsl",
            ".hlsli",
            ".glsl",
            ".vert",
            ".frag",
            ".comp",
            ".geom",
            ".tesc",
            ".tese",
            ".spv",
            ".dxbc",
            ".dxil",
            ".shader",
            ".compute",
            ".usf",
            ".ush",
        }
    ),
    "animation": frozenset({".anim", ".animation"}),
    "skeleton": frozenset({".skel", ".skeleton", ".rig"}),
    "package": frozenset({".uasset", ".umap", ".uexp"}),
    "archive": frozenset({".pak", ".ucas", ".utoc", ".bundle", ".unity3d"}),
    "metadata": frozenset({".meta", ".json", ".yaml", ".yml", ".xml"}),
}
_KNOWN_KINDS = frozenset((*_KIND_EXTENSIONS, "binary", "other"))
# Bulk payloads: texture mips, audio, fonts, code, localisation, pipeline
# caches. They are catalogued by name only -- nothing inside them describes a
# material or a shader, and probing them fills the index with noise.
_BULK_SUFFIXES = frozenset(
    {
        ".ubulk", ".uptnl", ".ufont", ".ttf", ".otf", ".pyc", ".py", ".dll", ".pdb", ".exe",
        ".so", ".dylib", ".wav", ".ogg", ".mp3", ".mp4", ".webm", ".bk2", ".mov", ".bnk", ".wem",
        ".ini", ".svg", ".res", ".locres", ".locmeta", ".upipelinecache", ".ushaderbytecode",
        ".pdf", ".zip", ".7z", ".rar",
    }
)
# Formats for which the catalog, a built-in engine reader, or a registered
# asset-reader route has a concrete interpretation. A names-only activation
# scan stores only these paths. Unknown/custom formats remain discoverable by
# the direct bounded search tools and can be indexed by an explicit
# headers/full scan; unrelated payload/cache files must not consume the shallow
# catalog's row limit.
_NAMES_ONLY_SUFFIXES = frozenset().union(*_KIND_EXTENSIONS.values(), {
    ".bin", ".dat", ".bytes",
    ".asset", ".assets", ".prefab", ".ress", ".sharedassets", ".resource",
    ".pck", ".tscn", ".tres", ".gd", ".uproject", ".uplugin",
    ".ushaderbytecode",
})
_NAMES_ONLY_FILENAMES = frozenset({
    "globalgamemanagers", "level0", "project.godot",
})
_WINDOWS_REPARSE_POINT = 0x0400


class AssetCatalogError(RuntimeError):
    """A fail-closed unpacked-asset catalog error."""


def _replace_with_retry(temporary: Path, path: Path, *, attempts: int = 12) -> None:
    """Publish a catalog even while a reader still holds the old file open.

    On Windows ``os.replace`` fails with a sharing violation (WinError 5/32) while
    any process has the target open — and the catalog is read by every status and
    retrieval call. A scan that walked 190k files must not be thrown away because a
    status poll landed in the same millisecond, so retry briefly before giving up.
    """

    delay = 0.05
    for attempt in range(attempts):
        try:
            os.replace(str(temporary), str(path))
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 1.8, 1.5)
        except OSError as error:
            if getattr(error, "winerror", None) not in (5, 32) or attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 1.8, 1.5)


class AssetCatalogScanLimitError(AssetCatalogError):
    """A bounded scan stopped before publication and retained the old catalog."""

    def __init__(self, *, maximum_files: int, observed_files: int):
        super().__init__(
            "asset root holds more than %d files (walked %d before stopping): raise max_files "
            "for this call, or narrow the configured root to the cooked content"
            % (maximum_files, observed_files)
        )
        self.maximum_files = maximum_files
        self.observed_files = observed_files


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _filesystem_root(path: Path) -> bool:
    return path.parent == path


def _is_redirected(path: Path) -> bool:
    """Reject symbolic links and Windows junction/other reparse entries."""

    try:
        status = path.lstat()
    except OSError:
        return path.is_symlink()
    if path.is_symlink() or bool(
        int(getattr(status, "st_file_attributes", 0)) & _WINDOWS_REPARSE_POINT
    ):
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


def _validate_root(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise AssetCatalogError("asset root must be absolute")
    if _is_redirected(raw):
        raise AssetCatalogError("asset root must not be a symbolic link or reparse point")
    root = raw.resolve(strict=True)
    if not root.is_dir() or _filesystem_root(root):
        raise AssetCatalogError("asset root must be a non-root directory")
    return root


def _validate_database_path(value: str | Path) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise AssetCatalogError("catalog database path must be absolute")
    if raw.exists() and _is_redirected(raw):
        raise AssetCatalogError("catalog database must not be redirected")
    parent = raw.parent.resolve(strict=True)
    if _is_redirected(parent) or not parent.is_dir():
        raise AssetCatalogError("catalog database parent is redirected or unavailable")
    return parent / raw.name


def _classify(path: PurePosixPath) -> str:
    suffix = path.suffix.casefold()
    for kind, extensions in _KIND_EXTENSIONS.items():
        if suffix in extensions:
            return kind
    return "binary" if suffix in {".bin", ".dat", ".bytes"} else "other"


def _format_name(path: PurePosixPath) -> str:
    suffix = path.suffix.casefold().lstrip(".")
    return suffix or "extensionless"


def _names_only_admitted(path: PurePosixPath) -> bool:
    """Whether a shallow catalog can do useful work with this asset name."""

    return (
        path.suffix.casefold() in _NAMES_ONLY_SUFFIXES
        or path.name.casefold() in _NAMES_ONLY_FILENAMES
    )


def _hash_regular_file(path: Path, expected_size: int) -> str:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(HASH_CHUNK_BYTES)
            if not block:
                break
            total += len(block)
            digest.update(block)
    if total != expected_size:
        raise AssetCatalogError("asset changed size while it was hashed: %s" % path)
    return digest.hexdigest()


_WINDOWS_CHANGE_API: tuple[Any, ...] | None = None


def _file_change_token(path: Path, stat: os.stat_result) -> int:
    """OS content-change token that cannot be restored with ``utime``."""

    if os.name != "nt":
        return int(stat.st_ctime_ns)
    try:
        global _WINDOWS_CHANGE_API
        import ctypes
        from ctypes import wintypes

        if _WINDOWS_CHANGE_API is None:
            class FileBasicInfo(ctypes.Structure):
                _fields_ = [
                    ("CreationTime", ctypes.c_longlong),
                    ("LastAccessTime", ctypes.c_longlong),
                    ("LastWriteTime", ctypes.c_longlong),
                    ("ChangeTime", ctypes.c_longlong),
                    ("FileAttributes", wintypes.DWORD),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_file = kernel32.CreateFileW
            create_file.argtypes = [
                wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
            ]
            create_file.restype = wintypes.HANDLE
            get_info = kernel32.GetFileInformationByHandleEx
            get_info.argtypes = [
                wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ]
            get_info.restype = wintypes.BOOL
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL
            _WINDOWS_CHANGE_API = (
                create_file, get_info, close_handle, FileBasicInfo,
                ctypes.c_void_p(-1).value,
            )
        create_file, get_info, close_handle, FileBasicInfo, invalid = _WINDOWS_CHANGE_API
        handle = create_file(
            str(path), 0x0080, 0x00000001 | 0x00000002 | 0x00000004,
            None, 3, 0x00000080, None,
        )
        if handle in (None, invalid):
            raise OSError(ctypes.get_last_error(), "CreateFileW failed")
        try:
            info = FileBasicInfo()
            if not get_info(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
                raise OSError(ctypes.get_last_error(), "FileBasicInfo failed")
            return int(info.ChangeTime)
        finally:
            close_handle(handle)
    except (AttributeError, OSError, ValueError):
        return int(stat.st_ctime_ns)


def _content_manifest_file_sha256(path: Path) -> str:
    """Hash one dirty manifest member; split out for regression instrumentation."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _content_manifest_document(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a catalog-owner manifest before it can seed incremental hashes."""

    document = dict(value)
    supplied = str(document.pop("documentSha256", "") or "").lower()
    if not _SHA256_RE.fullmatch(supplied) or supplied != _canonical_sha256(document):
        raise AssetCatalogError("asset content manifest document seal is invalid")
    if document.get("schema") != CONTENT_MANIFEST_SCHEMA:
        raise AssetCatalogError("asset content manifest schema is unsupported")
    files = document.get("files")
    if not isinstance(files, list) or len(files) > HARD_MAX_FILES:
        raise AssetCatalogError("asset content manifest file set is invalid")
    rows: list[dict[str, Any]] = []
    for item in files:
        if not isinstance(item, Mapping):
            raise AssetCatalogError("asset content manifest row is invalid")
        relative = str(item.get("relativePath") or "")
        digest = str(item.get("contentSha256") or "").lower()
        size = item.get("size")
        if (
            not relative or len(relative) > 4096 or "\x00" in relative
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or not isinstance(size, int) or isinstance(size, bool) or size < 0
            or not _SHA256_RE.fullmatch(digest)
        ):
            raise AssetCatalogError("asset content manifest row fields are invalid")
        rows.append({
            "relativePath": relative,
            "size": size,
            "contentSha256": digest,
        })
    if _canonical_sha256(rows) != document.get("contentRootSha256"):
        raise AssetCatalogError("asset content manifest Merkle root is invalid")
    return {**document, "documentSha256": supplied}


def _safe_reference(source_relative: str, raw_reference: str) -> str:
    value = unquote(str(raw_reference or "").strip()).replace("\\", "/")
    if not value or "\x00" in value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or value.startswith("/"):
        return ""
    reference = PurePosixPath(value)
    combined = PurePosixPath(source_relative).parent / reference
    stack: list[str] = []
    for part in combined.parts:
        if part in ("", "."):
            continue
        if part == "..":
            if not stack:
                return ""
            stack.pop()
        else:
            stack.append(part)
    return "/".join(stack)


def _bounded_text(data: bytes) -> tuple[str, str]:
    try:
        return data.decode("utf-8-sig", errors="strict"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), "utf-8-replacement"


def _bounded_reference_result(
    metadata: Mapping[str, Any], references: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Bound persisted relationship observations and disclose lost coverage."""

    result_metadata = dict(metadata)
    observed = len(references)
    indexed = min(observed, MAX_INDEXED_REFERENCES_PER_ASSET)
    result_metadata["referenceCountObserved"] = observed
    result_metadata["referenceCountIndexed"] = indexed
    result_metadata["referencesTruncated"] = observed > indexed
    return result_metadata, [dict(item) for item in references[:indexed]]


def _gltf_document(data: bytes, suffix: str) -> Mapping[str, Any] | None:
    payload = data
    if suffix == ".glb":
        if len(data) < 20 or data[:4] != b"glTF":
            return None
        version, total_length = struct.unpack_from("<II", data, 4)
        if version != 2 or total_length > len(data):
            return None
        chunk_length, chunk_type = struct.unpack_from("<II", data, 12)
        if chunk_type != 0x4E4F534A or 20 + chunk_length > total_length:
            return None
        payload = data[20 : 20 + chunk_length]
    try:
        value = json.loads(payload.decode("utf-8-sig", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _parse_gltf(data: bytes, suffix: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    document = _gltf_document(data, suffix)
    if document is None:
        return {"parser": "gltf-v2", "valid": False}, []
    meshes = document.get("meshes") if isinstance(document.get("meshes"), list) else []
    materials = (
        document.get("materials") if isinstance(document.get("materials"), list) else []
    )
    accessors = (
        document.get("accessors") if isinstance(document.get("accessors"), list) else []
    )
    primitives = sum(
        len(item.get("primitives", []))
        for item in meshes
        if isinstance(item, Mapping) and isinstance(item.get("primitives"), list)
    )
    references: list[dict[str, Any]] = []
    for relation, key in (("uses-buffer", "buffers"), ("uses-texture", "images")):
        values = document.get(key)
        if not isinstance(values, list):
            continue
        for index, item in enumerate(values):
            if isinstance(item, Mapping) and isinstance(item.get("uri"), str):
                references.append(
                    {
                        "relation": relation,
                        "reference": item["uri"],
                        "evidence": {"parser": "gltf-v2", "jsonPath": "%s[%d].uri" % (key, index)},
                    }
                )
    metadata = {
        "parser": "gltf-v2",
        "valid": True,
        "meshCount": len(meshes),
        "primitiveCount": primitives,
        "materialCount": len(materials),
        "textureCount": len(document.get("textures", []))
        if isinstance(document.get("textures"), list)
        else 0,
        "skinCount": len(document.get("skins", []))
        if isinstance(document.get("skins"), list)
        else 0,
        "animationCount": len(document.get("animations", []))
        if isinstance(document.get("animations"), list)
        else 0,
        "accessorCount": len(accessors),
        "extensionsUsed": sorted(
            str(item)[:128]
            for item in (document.get("extensionsUsed") or [])
            if isinstance(item, str)
        )[:128],
    }
    return metadata, references


def _parse_obj(text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    counts = Counter()
    references: list[dict[str, Any]] = []
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        token, _, rest = line.partition(" ")
        if token in {"v", "vt", "vn", "f", "o", "g", "usemtl"}:
            counts[token] += 1
        elif token.casefold() == "mtllib":
            for item in rest.split():
                references.append(
                    {
                        "relation": "uses-material-library",
                        "reference": item,
                        "evidence": {"parser": "obj", "line": line_number},
                    }
                )
    return {
        "parser": "obj",
        "positionCount": counts["v"],
        "texcoordCount": counts["vt"],
        "normalCount": counts["vn"],
        "faceCount": counts["f"],
        "objectCount": counts["o"],
        "groupCount": counts["g"],
        "materialUseCount": counts["usemtl"],
    }, references


def _parse_mtl(text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    material_count = 0
    references: list[dict[str, Any]] = []
    texture_commands = {
        "map_ka",
        "map_kd",
        "map_ks",
        "map_ke",
        "map_d",
        "map_bump",
        "bump",
        "disp",
        "decal",
        "norm",
    }
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        token, _, rest = line.partition(" ")
        lowered = token.casefold()
        if lowered == "newmtl":
            material_count += 1
        elif lowered in texture_commands and rest.strip():
            # Texture options precede the final filename. Quoted paths are not
            # fully parsed here; the exact source line remains evidence.
            candidate = rest.strip().split()[-1]
            references.append(
                {
                    "relation": "uses-texture",
                    "reference": candidate.strip('"'),
                    "evidence": {"parser": "mtl", "line": line_number, "command": token},
                }
            )
    return {"parser": "mtl", "materialCount": material_count}, references


def _parse_shader(text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    references = [
        {
            "relation": "includes-shader-source",
            "reference": match.group(1),
            "evidence": {
                "parser": "preprocessor-include",
                "byteOffset": len(text[: match.start()].encode("utf-8", errors="replace")),
            },
        }
        for match in _SHADER_INCLUDE_RE.finditer(text)
    ]
    return {
        "parser": "shader-source",
        "includeCount": len(references),
        "lineCount": text.count("\n") + (1 if text else 0),
    }, references


def _parse_unity_text(text: str, suffix: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    declared = _UNITY_GUID_RE.search(text) if suffix == ".meta" else None
    references: list[dict[str, Any]] = []
    for match in _UNITY_REFERENCE_GUID_RE.finditer(text):
        guid = match.group(1).lower()
        if declared is not None and guid == declared.group(1).lower():
            continue
        references.append(
            {
                "relation": "references-unity-guid",
                "guid": guid,
                "evidence": {"parser": "unity-yaml-guid", "characterOffset": match.start(1)},
            }
        )
    return {
        "parser": "unity-text",
        "declaredGuid": declared.group(1).lower() if declared else "",
        "referencedGuidCount": len(references),
    }, references


def _parse_json_asset(data: bytes) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        value = json.loads(data.decode("utf-8-sig", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"parser": "json", "valid": False}, []
    if not isinstance(value, Mapping):
        return {"parser": "json", "valid": True, "rootType": type(value).__name__}, []
    schema = str(value.get("schema", "") or "")
    references: list[dict[str, Any]] = []
    if schema == "uex.blender-bundle.v1":
        for index, mesh in enumerate(value.get("meshes", [])):
            if isinstance(mesh, Mapping) and isinstance(mesh.get("relativePath"), str):
                references.append(
                    {
                        "relation": "contains-lod-geometry",
                        "reference": mesh["relativePath"],
                        "evidence": {"parser": schema, "jsonPath": "meshes[%d].relativePath" % index},
                    }
                )
        if isinstance(value.get("ueModel"), str) and value["ueModel"]:
            references.append(
                {
                    "relation": "contains-uemodel",
                    "reference": value["ueModel"],
                    "evidence": {"parser": schema, "jsonPath": "ueModel"},
                }
            )
        for index, texture in enumerate(value.get("textures", [])):
            if isinstance(texture, Mapping) and isinstance(texture.get("relativePath"), str):
                references.append(
                    {
                        "relation": "uses-texture",
                        "reference": texture["relativePath"],
                        "evidence": {"parser": schema, "jsonPath": "textures[%d].relativePath" % index},
                    }
                )
        for index, material in enumerate(value.get("materials", [])):
            if not isinstance(material, Mapping):
                continue
            for field, relation in (
                ("materialJson", "uses-material-record"),
                ("rawMaterialJson", "uses-raw-material-record"),
            ):
                if isinstance(material.get(field), str) and material[field]:
                    references.append(
                        {
                            "relation": relation,
                            "reference": material[field],
                            "evidence": {"parser": schema, "jsonPath": "materials[%d].%s" % (index, field)},
                        }
                    )
            base_color = material.get("baseColor")
            if isinstance(base_color, Mapping) and isinstance(base_color.get("relativePath"), str):
                references.append(
                    {
                        "relation": "uses-base-color-texture",
                        "reference": base_color["relativePath"],
                        "evidence": {"parser": schema, "jsonPath": "materials[%d].baseColor.relativePath" % index},
                    }
                )
            for parameter_field in (
                "directTextureParameters",
                "textureParameters",
            ):
                parameters = material.get(parameter_field)
                if not isinstance(parameters, list):
                    continue
                for parameter_index, parameter in enumerate(parameters):
                    if (
                        isinstance(parameter, Mapping)
                        and isinstance(parameter.get("relativePath"), str)
                        and parameter["relativePath"]
                    ):
                        references.append(
                            {
                                "relation": "uses-texture-parameter",
                                "reference": parameter["relativePath"],
                                "evidence": {
                                    "parser": schema,
                                    "jsonPath": "materials[%d].%s[%d].relativePath"
                                    % (index, parameter_field, parameter_index),
                                },
                            }
                        )
        materials = value.get("materials") if isinstance(value.get("materials"), list) else []
        textures = value.get("textures") if isinstance(value.get("textures"), list) else []
        return {
            "parser": schema,
            "valid": True,
            "sourceAsset": str(value.get("sourceAsset", "") or "")[:4096],
            "assetType": str(value.get("assetType", "") or "")[:128],
            "meshName": str(value.get("meshName", "") or "")[:512],
            "skeleton": str(value.get("skeleton", "") or "")[:4096],
            "boneCount": value.get("boneCount"),
            "sourceLodCount": value.get("sourceLodCount"),
            "exportedLodCount": len(value.get("meshes", []))
            if isinstance(value.get("meshes"), list)
            else 0,
            "morphTargetCount": value.get("morphTargetCount"),
            "materialCount": len(materials),
            "textureCount": len(textures),
            "warningCount": len(value.get("warnings", []))
            if isinstance(value.get("warnings"), list)
            else 0,
            "materialApproximationOnly": True,
        }, references
    if schema in {"uex.blender-assembly.v1", "uex.blender-validation.v1"}:
        bundles = value.get("bundles") if isinstance(value.get("bundles"), list) else []
        return {
            "parser": schema,
            "valid": True,
            "status": str(value.get("status", "") or "")[:64],
            "ok": value.get("ok") if isinstance(value.get("ok"), bool) else None,
            "sourceAssetCount": len(value.get("sourceAssets", []))
            if isinstance(value.get("sourceAssets"), list)
            else 0,
            "bundleCount": len(bundles),
            "geometrySource": str(value.get("geometrySource", "") or "")[:64],
            "validationReportPresent": bool(value.get("validationReport")),
        }, []
    # UEX asset-report manifests are completion markers even when older/newer
    # builds do not expose a stable schema token. Recognise them by their exact
    # four portable outputs but keep statistics in the report JSON itself.
    output_names = {
        "asset-report.html",
        "asset-report.json",
        "asset-summary.csv",
    }
    encoded = _canonical_bytes(value).decode("utf-8")
    if all(name in encoded for name in output_names):
        return {
            "parser": "uex-asset-report-manifest",
            "valid": True,
            "completionMarker": True,
            "inventoryId": value.get("inventoryId", value.get("inventory")),
            "portableOutputs": sorted(output_names | {"asset-report-manifest.json"}),
        }, []
    return {
        "parser": "json",
        "valid": True,
        "schema": schema,
        "reason": "no fixed asset relationship parser for this JSON schema",
    }, []


def _parse_png(data: bytes) -> dict[str, Any]:
    if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return {"parser": "png-header", "valid": False}
    width, height, bit_depth, color_type = struct.unpack_from(">IIBB", data, 16)
    chunks: set[bytes] = set()
    offset = 8
    while offset + 12 <= len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4 : offset + 8]
        if length > len(data) - offset - 12:
            break
        chunks.add(kind)
        offset += 12 + length
        if kind == b"IEND":
            break
    return {
        "parser": "png-header",
        "valid": True,
        "width": width,
        "height": height,
        "bitDepth": bit_depth,
        "colorType": color_type,
        "colorSpaceEvidence": (
            "srgb-chunk" if b"sRGB" in chunks else "gamma-chunk" if b"gAMA" in chunks else "unspecified"
        ),
    }


def _parse_dds(data: bytes) -> dict[str, Any]:
    if len(data) < 128 or data[:4] != b"DDS " or struct.unpack_from("<I", data, 4)[0] != 124:
        return {"parser": "dds-header", "valid": False}
    height, width = struct.unpack_from("<II", data, 12)
    fourcc = data[84:88].rstrip(b"\x00").decode("ascii", errors="replace")
    dxgi = struct.unpack_from("<I", data, 128)[0] if fourcc == "DX10" and len(data) >= 148 else None
    srgb_dxgi = {29, 72, 75, 78, 91, 93, 99}
    return {
        "parser": "dds-header",
        "valid": True,
        "width": width,
        "height": height,
        "fourCC": fourcc,
        "dxgiFormat": dxgi,
        "colorSpaceEvidence": "srgb-dxgi-format" if dxgi in srgb_dxgi else "unspecified",
    }


def _parse_ktx2(data: bytes) -> dict[str, Any]:
    magic = b"\xabKTX 20\xbb\r\n\x1a\n"
    if len(data) < 68 or data[:12] != magic:
        return {"parser": "ktx2-header", "valid": False}
    vk_format, type_size, width, height, depth, layers, faces, levels = struct.unpack_from(
        "<8I", data, 12
    )
    # Vulkan sRGB formats are not contiguous across every extension. This fixed
    # set covers the core uncompressed/BC/ETC2/ASTC families most often unpacked.
    srgb_vk = {
        43, 50, 57, 64, 71, 78, 85, 92, 99, 106, 113, 120, 127, 134,
        137, 140, 143, 146, 149, 152, 155, 158, 161, 164, 167, 170, 173, 176,
    }
    return {
        "parser": "ktx2-header",
        "valid": True,
        "vkFormat": vk_format,
        "typeSize": type_size,
        "width": width,
        "height": height,
        "depth": depth,
        "layers": layers,
        "faces": faces,
        "levels": levels,
        "colorSpaceEvidence": "srgb-vk-format" if vk_format in srgb_vk else "unspecified",
    }


def _parse_jpeg(data: bytes) -> dict[str, Any]:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return {"parser": "jpeg-header", "valid": False}
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        offset += 2
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            break
        length = struct.unpack_from(">H", data, offset)[0]
        if length < 2 or offset + length > len(data):
            break
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and length >= 8:
            bits = data[offset + 2]
            height, width = struct.unpack_from(">HH", data, offset + 3)
            return {
                "parser": "jpeg-header",
                "valid": True,
                "width": width,
                "height": height,
                "bitDepth": bits,
                "colorSpaceEvidence": "unspecified",
            }
        offset += length
    return {"parser": "jpeg-header", "valid": False}


# Bump whenever a parser's output shape changes so incremental scans re-parse
# unchanged files instead of carrying old metadata forward.
PARSER_REVISION = 3


MAX_IMAGE_FINGERPRINT_BYTES = 2 * 1024 * 1024
# An explicitly selected reader preview can be a normal 4K game texture. Keep
# this allowance separate from the cheap generic directory-indexing budget.
MAX_READER_PREVIEW_BYTES = 80 * 1024 * 1024
MAX_PREVIEW_INPUT_BYTES = 256 * 1024 * 1024


def _preview_input_identity(root: Path, files: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Verify the decoder's complete, caller-attested input closure by bytes."""
    if not isinstance(files, (list, tuple)) or not 1 <= len(files) <= 32:
        raise AssetCatalogError("texture preview requires a bounded sourceFiles closure")
    checked, seen, total = [], set(), 0
    for item in files:
        relative = str(item.get("relativePath", "")).replace("\\", "/")
        digest = str(item.get("sha256", ""))
        if (relative in seen or not relative or any(part in {"", ".", ".."} for part in relative.split("/"))
                or not _SHA256_RE.fullmatch(digest)):
            raise AssetCatalogError("texture preview source identity is invalid")
        path = root
        for part in relative.split("/"):
            path = path / part
            if _is_redirected(path):
                raise AssetCatalogError("texture preview source is redirected")
        if not _within(root, path.resolve()) or not path.is_file():
            raise AssetCatalogError("texture preview source is unavailable")
        total += path.stat().st_size
        if total > MAX_PREVIEW_INPUT_BYTES:
            raise AssetCatalogError("texture preview source closure exceeds its byte bound")
        actual = _hash_regular_file(path, path.stat().st_size)
        if actual != digest:
            raise AssetCatalogError("texture preview source content changed: %s" % relative)
        checked.append({"relativePath": relative, "sha256": digest, "size": path.stat().st_size})
        seen.add(relative)
    return checked


def _preview_bytes(path: Path, digest: str) -> bytes:
    if (not _SHA256_RE.fullmatch(digest) or not path.is_file()
            or any(_is_redirected(parent) for parent in (path, *path.parents))):
        raise AssetCatalogError("texture preview output identity is invalid")
    if path.stat().st_size > MAX_READER_PREVIEW_BYTES:
        raise AssetCatalogError("texture preview exceeds the admitted image byte bound")
    data = path.read_bytes()
    if len(data) > MAX_READER_PREVIEW_BYTES or hashlib.sha256(data).hexdigest() != digest:
        raise AssetCatalogError("texture preview output content changed")
    return data


def _image_fingerprint(data: bytes, suffix: str, *, reader_preview: bool = False) -> dict[str, Any]:
    """Thumbnail fingerprint for a plain image file (PNG/DDS), bounded."""

    from .symbols.imaging import ImageDecodeError, decode_dds, decode_png, fingerprint

    byte_bound = MAX_READER_PREVIEW_BYTES if reader_preview else MAX_IMAGE_FINGERPRINT_BYTES
    if len(data) > byte_bound:
        return {"available": False, "reason": "image exceeds the %d-byte fingerprint bound" % byte_bound}
    try:
        if suffix == ".png":
            rgba, width, height = decode_png(data)
            fmt = "PNG"
        else:
            rgba, width, height, fmt = decode_dds(data)
        if not reader_preview and width * height > 1024 * 1024:
            return {"available": False, "reason": "image is larger than 1M pixels; fingerprint skipped"}
        result = fingerprint(rgba, width, height)
        result.update({"available": True, "pixelFormat": fmt, "sizeX": width, "sizeY": height})
        return result
    except (ImageDecodeError, ValueError, struct.error, zlib.error, IndexError) as error:
        return {"available": False, "reason": str(error)[:200]}


def _generic_probe(relative: str, data: bytes, engine_context: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from .symbols.generic_probe import mention_references, probe

    metadata = {"parser": "generic-content-probe/v1", "engine": getattr(engine_context, "engine", "unknown")}
    # descriptors (materials, configs) are small; large blobs are geometry or
    # animation whose floats would only add noise to the value index
    metadata.update(probe(data, relative, numeric=_numeric_probe_admitted(relative, data)))
    stem_index = getattr(engine_context, "stem_index", None) or {}
    references = mention_references(metadata.get("strings") or [], metadata.get("pathReferences") or [],
                                    stem_index, PurePosixPath(relative).stem) if stem_index else []
    return metadata, references


def _parse_asset(
    relative: str, kind: str, data: bytes, engine_context: Any = None, sibling_class: str = ""
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    suffix = PurePosixPath(relative).suffix.casefold()
    if kind == "package":
        from .symbols.engines import parse_engine_asset

        parsed = parse_engine_asset(engine_context, relative, data, sibling_class)
        if parsed is not None:
            return _bounded_reference_result(*parsed)
    if kind in {"other", "binary", "package"} or (kind == "metadata" and suffix not in {".json", ".meta", ".yaml", ".yml", ".xml"}):
        # Generic layer: any engine, any format — strings, numbers, embedded
        # images, embedded shaders, and name mentions resolved to files.
        return _bounded_reference_result(*_generic_probe(relative, data, engine_context))
    if suffix in {".gltf", ".glb"}:
        metadata, references = _parse_gltf(data, suffix)
    elif suffix == ".png":
        metadata, references = _parse_png(data), []
        metadata = dict(metadata)
        metadata["textureFingerprint"] = _image_fingerprint(data, suffix)
    elif suffix == ".dds":
        metadata, references = _parse_dds(data), []
        metadata = dict(metadata)
        metadata["textureFingerprint"] = _image_fingerprint(data, suffix)
    elif suffix == ".ktx2":
        metadata, references = _parse_ktx2(data), []
    elif suffix in {".jpg", ".jpeg"}:
        metadata, references = _parse_jpeg(data), []
    elif suffix == ".json":
        metadata, references = _parse_json_asset(data)
    elif suffix in {".obj", ".mtl", ".mat", ".material", ".meta", ".yaml", ".yml"} or kind == "shader":
        text, encoding = _bounded_text(data)
        if suffix == ".obj":
            metadata, references = _parse_obj(text)
        elif suffix == ".mtl":
            metadata, references = _parse_mtl(text)
        elif kind == "shader":
            metadata, references = _parse_shader(text)
        else:
            metadata, references = _parse_unity_text(text, suffix)
        metadata["textEncoding"] = encoding
        if suffix in {".mtl", ".mat", ".material", ".yaml", ".yml"}:
            metadata = dict(metadata)
            metadata["numericValues"] = _text_numeric_values(text)
    else:
        metadata, references = (
            {"parser": "none", "reason": "format metadata requires an admitted provider"},
            [],
        )
    return _bounded_reference_result(metadata, references)


def _asset_identity(relative: str, content_sha256: str, size: int, modified_ns: int) -> str:
    material = {
        "relativePath": relative,
        "contentSha256": content_sha256,
        "size": size,
        "modifiedNs": 0 if content_sha256 else modified_ns,
    }
    return "asset-v1-" + _canonical_sha256(material)


# ---------------------------------------------------------------------------
# Storage: one live SQLite index, updated in place.
#
# The catalog is an index of the unpacked tree, not a copy of it: every row is
# a path plus its fingerprint (size, mtime, content hash) and the small parsed
# summary a reviewer needs. A scan walks the tree and touches only the rows
# whose file changed; rows whose file disappeared are dropped once the walk is
# complete. Nothing is rewritten wholesale, and a reader never has to re-hash
# the whole catalog: every row carries its own digest, verified when the row
# is delivered, and the catalog identity is the XOR of those digests.
# ---------------------------------------------------------------------------

_ASSET_COLUMNS = (
    "asset_id, relative_path, name, kind, format, size, modified_ns, content_sha256, "
    "hash_status, parse_status, metadata_json, row_sha256"
)
_ASSET_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assets (
    asset_id TEXT PRIMARY KEY,
    relative_path TEXT NOT NULL UNIQUE,
    folded_path TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    format TEXT NOT NULL,
    size INTEGER NOT NULL,
    modified_ns INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    hash_status TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    declared_guid TEXT NOT NULL DEFAULT '',
    class_name TEXT NOT NULL DEFAULT '',
    parser_revision INTEGER NOT NULL DEFAULT 0,
    generation INTEGER NOT NULL DEFAULT 0,
    row_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS assets_kind_idx ON assets(kind, format);
CREATE INDEX IF NOT EXISTS assets_hash_idx ON assets(content_sha256);
CREATE INDEX IF NOT EXISTS assets_folded_idx ON assets(folded_path);
CREATE INDEX IF NOT EXISTS assets_guid_idx ON assets(declared_guid);
CREATE INDEX IF NOT EXISTS assets_generation_idx ON assets(generation);
CREATE TABLE IF NOT EXISTS relationships (
    source_asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
    relation TEXT NOT NULL,
    target_asset_id TEXT REFERENCES assets(asset_id) ON DELETE SET NULL,
    target_path TEXT NOT NULL,
    target_guid TEXT NOT NULL,
    target_folded TEXT NOT NULL DEFAULT '',
    evidence_kind TEXT NOT NULL,
    confidence REAL NOT NULL,
    verified INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    UNIQUE(source_asset_id, relation, target_asset_id, target_path, target_guid, evidence_kind)
);
CREATE INDEX IF NOT EXISTS relationships_source_idx ON relationships(source_asset_id);
CREATE INDEX IF NOT EXISTS relationships_target_idx ON relationships(target_asset_id);
CREATE INDEX IF NOT EXISTS relationships_kind_idx ON relationships(evidence_kind);
CREATE TABLE IF NOT EXISTS asset_values (
    value_key TEXT NOT NULL, asset_id TEXT NOT NULL,
    PRIMARY KEY(value_key, asset_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS asset_values_asset_idx ON asset_values(asset_id);
CREATE TABLE IF NOT EXISTS asset_thumbnails (
    asset_id TEXT PRIMARY KEY, v0 INTEGER, v1 INTEGER, v2 INTEGER, v3 INTEGER,
    v4 INTEGER, v5 INTEGER, v6 INTEGER, v7 INTEGER, mean_r INTEGER, mean_g INTEGER, mean_b INTEGER,
    width INTEGER, height INTEGER, flat INTEGER, pixel_format TEXT
);
"""
_TEMPORARY_CATALOG_MAX_AGE_SECONDS = 6 * 3600
MAX_NUMERIC_VALUES_PER_ASSET = 256
MAX_NUMERIC_RUNS_PER_ASSET = 64


def _database_connection(path: Path, *, read_only: bool) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(str(path), isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    if read_only:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA temp_store = MEMORY")
    else:
        # Readers (status polls, retrieval) keep working while a scan writes.
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        connection.execute("PRAGMA temp_store = MEMORY")
    return connection


def _validate_schema(connection: sqlite3.Connection) -> dict[str, str]:
    tables = {
        str(row["name"])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        if not str(row["name"]).startswith("sqlite_")
    }
    if not {"metadata", "assets", "relationships"}.issubset(tables):
        raise AssetCatalogError("asset catalog database is incomplete")
    metadata = {
        str(row["key"]): str(row["value"])
        for row in connection.execute("SELECT key, value FROM metadata")
    }
    if metadata.get("schema") != CATALOG_SCHEMA or metadata.get("schemaVersion") != str(DATABASE_SCHEMA_VERSION):
        raise AssetCatalogError("asset catalog database schema is unsupported")
    if not _SHA256_RE.fullmatch(metadata.get("catalogSha256", "")):
        raise AssetCatalogError("asset catalog database has no valid catalog identity")
    return metadata


def _row_digest(item: Mapping[str, Any]) -> str:
    """The per-row manifest: what a reader is promised about one asset."""

    return _canonical_sha256(
        {
            "assetId": item["assetId"],
            "relativePath": item["relativePath"],
            "kind": item["kind"],
            "format": item["format"],
            "size": int(item["size"]),
            "contentSha256": item["contentSha256"],
            "hashStatus": item["hashStatus"],
            "parseStatus": item["parseStatus"],
            "metadata": item["metadata"],
        }
    )


def _row_record(row: sqlite3.Row) -> dict[str, Any]:
    """Decode one ``_ASSET_COLUMNS`` row and verify it against its own digest."""

    item = {
        "assetId": row["asset_id"],
        "relativePath": row["relative_path"],
        "name": row["name"],
        "kind": row["kind"],
        "format": row["format"],
        "size": int(row["size"]),
        "modifiedNs": int(row["modified_ns"]),
        "contentSha256": row["content_sha256"],
        "hashStatus": row["hash_status"],
        "parseStatus": row["parse_status"],
        "metadata": json.loads(row["metadata_json"]),
    }
    if _row_digest(item) != str(row["row_sha256"] or ""):
        raise AssetCatalogError(
            "asset catalog contents do not match its embedded manifest hash"
        )
    return item


def _catalog_identity(root_path: str, build_id: str, aggregate: int, asset_count: int) -> str:
    return _canonical_sha256(
        {
            "schema": CATALOG_SCHEMA,
            "schemaVersion": CATALOG_VERSION,
            "rootPath": os.path.normcase(str(root_path)),
            "buildId": str(build_id),
            "assetDigest": "%064x" % (aggregate & ((1 << 256) - 1)),
            "assetCount": int(asset_count),
        }
    )


def _aggregate_row_digests(connection: sqlite3.Connection) -> tuple[int, int]:
    aggregate = 0
    count = 0
    for row in connection.execute("SELECT row_sha256 FROM assets"):
        aggregate ^= int(str(row["row_sha256"]), 16)
        count += 1
    return aggregate, count


def _validate_catalog_identity(
    connection: sqlite3.Connection, metadata: Mapping[str, str]
) -> None:
    """Recompute the catalog identity from the per-row digests (used by audits)."""

    aggregate, count = _aggregate_row_digests(connection)
    actual = _catalog_identity(
        metadata.get("rootPath", ""), metadata.get("buildId", ""), aggregate, count
    )
    if actual != metadata.get("catalogSha256"):
        raise AssetCatalogError(
            "asset catalog contents do not match its embedded manifest hash"
        )


def _safe_json(text: Any) -> dict[str, Any]:
    try:
        value = json.loads(text) if text else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _fts5_available(db: sqlite3.Connection) -> bool:
    try:
        db.execute("CREATE VIRTUAL TABLE temp.__fts_probe USING fts5(x)")
        db.execute("DROP TABLE temp.__fts_probe")
        return True
    except sqlite3.Error:
        return False


def _remove_database_files(path: Path) -> None:
    """Remove an unusable catalog, or fail before it can be reinitialised.

    Windows commonly refuses to unlink a SQLite database while another reader
    still owns a handle. Continuing after that failure is unsafe: the schema
    creation statements are all ``IF NOT EXISTS`` and the metadata insert would
    otherwise relabel the still-populated old database as belonging to a new
    root/build. The caller may retry after the reader closes, but it must never
    reuse the old bytes under a new identity.
    """

    failures: list[str] = []
    for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm"),
                      path.with_name(path.name + "-journal")):
        try:
            if candidate.exists():
                if candidate.is_symlink() or not candidate.is_file():
                    failures.append("%s is not a regular file" % candidate.name)
                    continue
                candidate.unlink()
        except OSError as error:
            failures.append("%s: %s" % (candidate.name, str(error)[:160]))
    remaining = [
        candidate.name
        for candidate in (
            path,
            path.with_name(path.name + "-wal"),
            path.with_name(path.name + "-shm"),
            path.with_name(path.name + "-journal"),
        )
        if candidate.exists()
    ]
    if failures or remaining:
        detail = "; ".join(
            failures
            + (["still present: %s" % ", ".join(remaining)] if remaining else [])
        )
        raise AssetCatalogError(
            "cannot retire the stale asset catalog; close active catalog readers and retry: %s"
            % detail
        )


def _empty_catalog_metadata(root: Path, build_id: str, token_index: str) -> dict[str, str]:
    now = _utc_now()
    return {
        "schema": CATALOG_SCHEMA,
        "schemaVersion": str(DATABASE_SCHEMA_VERSION),
        "catalogSha256": _catalog_identity(str(root), build_id, 0, 0),
        "assetDigest": "%064x" % 0,
        "rootPath": str(root),
        "buildId": build_id,
        "scanMode": "incremental",
        "createdAt": now,
        "updatedAt": now,
        "engine": "unknown",
        "engineJson": "{}",
        "parserRevision": str(PARSER_REVISION),
        "complete": "false",
        "progressJson": "{}",
        "generation": "0",
        "tokenIndex": token_index,
    }


def _initialise_catalog_database(
    db: sqlite3.Connection,
    root: Path,
    build_id: str,
    *,
    reset_existing: bool,
) -> dict[str, str]:
    """Create or transactionally reset one catalog for an exact root/build.

    A valid catalog that belongs to another root/build is reset through the
    same SQLite connection. Readers either retain their old snapshot or see
    the committed empty/new catalog; no observer can see old rows under new
    metadata. If the reset cannot acquire its write/schema lock, rollback is
    explicit and the old catalog identity remains intact.
    """

    reset_sql = ""
    if reset_existing:
        reset_sql = """
DROP TABLE IF EXISTS asset_tokens;
DROP TABLE IF EXISTS relationships;
DROP TABLE IF EXISTS asset_values;
DROP TABLE IF EXISTS asset_thumbnails;
DROP TABLE IF EXISTS assets;
DROP TABLE IF EXISTS metadata;
"""
    try:
        # executescript commits any implicit transaction before it starts, so
        # put the BEGIN in the script and deliberately leave it open until the
        # token index and sealed metadata have also been created.
        db.executescript("BEGIN IMMEDIATE;\n" + reset_sql + _ASSET_SCHEMA_SQL)
        if _fts5_available(db):
            db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS asset_tokens USING fts5("
                "asset_id UNINDEXED, tokens, tokenize='unicode61')"
            )
            token_index = "fts5"
        else:
            db.execute(
                "CREATE TABLE IF NOT EXISTS asset_tokens ("
                "asset_id TEXT PRIMARY KEY, tokens TEXT NOT NULL)"
            )
            token_index = "plain"
        metadata = _empty_catalog_metadata(root, build_id, token_index)
        db.executemany(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES(?, ?)",
            sorted(metadata.items()),
        )
        db.execute("COMMIT")
        return metadata
    except BaseException:
        if db.in_transaction:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise


def _cleanup_stale_temporaries(path: Path) -> int:
    """Drop abandoned ``.<name>.tmp-*`` catalogs left by interrupted older scans."""

    removed = 0
    prefix = ".%s.tmp-" % path.name
    cutoff = time.time() - _TEMPORARY_CATALOG_MAX_AGE_SECONDS
    try:
        entries = list(path.parent.iterdir())
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith(prefix) or entry.is_symlink() or not entry.is_file():
            continue
        try:
            if entry.stat().st_mtime > cutoff:
                continue
            entry.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def _reference_target(source_relative: str, reference: Mapping[str, Any]) -> tuple[str, str]:
    """(target_path, target_guid) for one parser reference, both bounded."""

    target_guid = str(reference.get("guid", "") or "").lower()
    if target_guid:
        return "", target_guid
    if reference.get("catalogRootRelative") is True:
        stored_target = PurePosixPath(str(reference.get("targetPath", "") or ""))
        if (
            not stored_target.is_absolute()
            and stored_target.parts
            and all(part not in {"", ".", ".."} for part in stored_target.parts)
        ):
            return stored_target.as_posix(), ""
        return "", ""
    return _safe_reference(
        source_relative,
        str(reference.get("reference", reference.get("targetPath", "")) or ""),
    ), ""


class _CatalogWriter:
    """In-place mutations of one catalog, inside the caller's transaction."""

    def __init__(self, db: sqlite3.Connection, metadata: Mapping[str, str]):
        self.db = db
        self.fts = metadata.get("tokenIndex") == "fts5"
        self.aggregate = int(metadata.get("assetDigest", "0") or "0", 16)

    # -- rows -------------------------------------------------------------
    def lookup(self, relative: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT rowid, asset_id, size, modified_ns, content_sha256, hash_status, parse_status, "
            "kind, class_name, parser_revision, row_sha256 FROM assets WHERE relative_path=?",
            (relative,),
        ).fetchone()

    def stamp(self, rowids: Sequence[int], generation: int) -> None:
        for start in range(0, len(rowids), 500):
            chunk = rowids[start:start + 500]
            self.db.execute(
                "UPDATE assets SET generation=? WHERE rowid IN (%s)" % ",".join("?" for _ in chunk),
                (generation, *chunk),
            )

    def delete(self, rowid: int, asset_id: str, row_sha256: str) -> None:
        if self.fts:
            self.db.execute("DELETE FROM asset_tokens WHERE rowid=?", (rowid,))
        else:
            self.db.execute("DELETE FROM asset_tokens WHERE asset_id=?", (asset_id,))
        self.db.execute("DELETE FROM asset_values WHERE asset_id=?", (asset_id,))
        self.db.execute("DELETE FROM asset_thumbnails WHERE asset_id=?", (asset_id,))
        # Relationships from this asset cascade; those pointing at it lose
        # their target and are resolved again at publication.
        self.db.execute("DELETE FROM assets WHERE rowid=?", (rowid,))
        self.aggregate ^= int(str(row_sha256), 16)

    def write(self, record: Mapping[str, Any], generation: int) -> int:
        relative = str(record["relativePath"])
        metadata = record["metadata"]
        row_sha256 = _row_digest(record)
        declared_guid = str(metadata.get("declaredGuid", "") or "").lower()
        cursor = self.db.execute(
            "INSERT INTO assets(asset_id, relative_path, folded_path, name, kind, format, size, "
            "modified_ns, content_sha256, hash_status, parse_status, metadata_json, declared_guid, "
            "class_name, parser_revision, generation, row_sha256) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record["assetId"], relative, relative.casefold(), PurePosixPath(relative).name,
                record["kind"], record["format"], int(record["size"]), int(record["modifiedNs"]),
                record["contentSha256"], record["hashStatus"], record["parseStatus"],
                _canonical_bytes(metadata).decode("utf-8"), declared_guid,
                str(metadata.get("className", "") or "")[:256],
                int(metadata.get("parserRevision", 0) or 0), generation, row_sha256,
            ),
        )
        rowid = int(cursor.lastrowid)
        self.aggregate ^= int(row_sha256, 16)
        asset_id = str(record["assetId"])
        tokens = asset_tokens(relative, metadata)
        if tokens:
            if self.fts:
                self.db.execute("INSERT INTO asset_tokens(rowid, asset_id, tokens) VALUES(?, ?, ?)",
                                (rowid, asset_id, " ".join(tokens)))
            else:
                self.db.execute("INSERT OR REPLACE INTO asset_tokens(asset_id, tokens) VALUES(?, ?)",
                                (asset_id, " ".join(tokens)))
        keys = [key for key in (metadata.get("numericValues") or ())
                if isinstance(key, str) and len(key) <= 24]
        if keys:
            self.db.executemany("INSERT OR IGNORE INTO asset_values(value_key, asset_id) VALUES(?, ?)",
                                [(key, asset_id) for key in keys])
        # A cooked material keeps its class in the package (``.uasset``) and its
        # parameter values in the payload (``.uexp``). Retrieval ranks packages --
        # that is where the class is -- so values attached only to the payload can
        # never score, and the strongest signal there is (constant-buffer values
        # against material parameters) was silently doing nothing.
        self._share_values_with_package(relative, asset_id, keys)
        self.write_thumbnail(asset_id, metadata.get("textureFingerprint"))
        self._write_references(asset_id, relative, record.get("references") or [])
        return rowid

    def write_thumbnail(self, asset_id: str, fingerprint: Any) -> None:
        """Update only derived image evidence, retaining original reference edges."""
        self.db.execute("DELETE FROM asset_thumbnails WHERE asset_id=?", (asset_id,))
        if isinstance(fingerprint, Mapping) and fingerprint.get("available") is True:
            variants = fingerprint.get("dhashVariants") or []
            if len(variants) == 8:
                mean = fingerprint.get("meanColor") or [0, 0, 0, 0]
                signed = [int(value) - (1 << 64) if int(value) >= (1 << 63) else int(value) for value in variants]
                self.db.execute(
                    "INSERT OR REPLACE INTO asset_thumbnails VALUES(%s)" % ",".join("?" * 16),
                    (asset_id, *signed, int(mean[0]), int(mean[1]), int(mean[2]),
                     int(fingerprint.get("sizeX") or 0), int(fingerprint.get("sizeY") or 0),
                     1 if fingerprint.get("flat") else 0, str(fingerprint.get("pixelFormat") or "")),
                )

    def _share_values_with_package(self, relative: str, asset_id: str, keys: list[str]) -> None:
        """Keep a package and its export payload on the same numeric evidence."""

        lowered = relative.casefold()
        try:
            if lowered.endswith(".uexp"):
                if not keys:
                    return
                stem = relative[: -len(".uexp")]
                for suffix in (".uasset", ".umap"):
                    row = self.db.execute(
                        "SELECT asset_id FROM assets WHERE relative_path=?", (stem + suffix,)).fetchone()
                    if row is not None:
                        self.db.executemany(
                            "INSERT OR IGNORE INTO asset_values(value_key, asset_id) VALUES(?, ?)",
                            [(key, str(row["asset_id"])) for key in keys])
                        return
                return
            if lowered.endswith((".uasset", ".umap")):
                # The package can be written after its payload (a rescan that only
                # touched the package), so pull the payload's values across too.
                payload = relative[: relative.rfind(".")] + ".uexp"
                rows = self.db.execute(
                    "SELECT v.value_key AS value_key FROM asset_values v "
                    "JOIN assets a ON a.asset_id = v.asset_id WHERE a.relative_path=? LIMIT ?",
                    (payload, MAX_NUMERIC_VALUES_PER_ASSET)).fetchall()
                if rows:
                    self.db.executemany(
                        "INSERT OR IGNORE INTO asset_values(value_key, asset_id) VALUES(?, ?)",
                        [(str(row["value_key"]), asset_id) for row in rows])
        except sqlite3.Error:
            # Numeric evidence is an accelerator; a failure here must not fail a scan.
            pass

    def _write_references(self, asset_id: str, relative: str, references: Sequence[Mapping[str, Any]]) -> None:
        deduplicated: dict[tuple[str, str, str], dict[str, Any]] = {}
        for reference in references:
            relation = str(reference.get("relation", "references") or "references")[:128]
            target_path, target_guid = _reference_target(relative, reference)
            evidence = dict(reference.get("evidence") or {})
            evidence["sourceRelativePath"] = relative
            evidence.setdefault("candidateCount", 0)
            evidence.setdefault("derivationClass", "observed")
            evidence.setdefault("resolutionStatus", "unresolved")
            key = (relation, target_path, target_guid)
            existing = deduplicated.get(key)
            if existing is None:
                deduplicated[key] = {"relation": relation, "targetPath": target_path,
                                     "targetGuid": target_guid, "evidence": evidence}
            else:
                existing["evidence"]["observationCount"] = int(
                    existing["evidence"].get("observationCount", 1)) + 1
        if not deduplicated:
            return
        self.db.executemany(
            "INSERT OR IGNORE INTO relationships(source_asset_id, relation, target_asset_id, target_path, "
            "target_guid, target_folded, evidence_kind, confidence, verified, evidence_json) "
            "VALUES(?, ?, NULL, ?, ?, ?, 'parser-reference', 0.0, 0, ?)",
            [
                (asset_id, item["relation"], item["targetPath"], item["targetGuid"],
                 item["targetPath"].casefold(), _canonical_bytes(item["evidence"]).decode("utf-8"))
                for item in deduplicated.values()
            ],
        )

    # -- derived edges ----------------------------------------------------
    def resolve_references(self) -> None:
        """Bind unresolved parser references to the assets now in the index."""

        db = self.db
        # A target that vanished leaves a verified edge with no target; it is
        # re-examined below like any other unresolved edge.
        db.execute(
            "UPDATE relationships SET verified=0, confidence=0.0 "
            "WHERE target_asset_id IS NULL AND verified=1 AND evidence_kind='parser-reference'"
        )
        pending = db.execute(
            "SELECT rowid, target_path, target_guid, target_folded, evidence_json FROM relationships "
            "WHERE target_asset_id IS NULL AND evidence_kind='parser-reference' "
            "AND (target_path<>'' OR target_guid<>'')"
        ).fetchall()
        for row in pending:
            guid = str(row["target_guid"] or "")
            if guid:
                candidates = [str(item["asset_id"]) for item in db.execute(
                    "SELECT asset_id FROM assets WHERE declared_guid=? LIMIT 3", (guid,))]
            else:
                candidates = [str(item["asset_id"]) for item in db.execute(
                    "SELECT asset_id FROM assets WHERE relative_path=? LIMIT 1", (row["target_path"],))]
                if not candidates:
                    candidates = [str(item["asset_id"]) for item in db.execute(
                        "SELECT asset_id FROM assets WHERE folded_path=? LIMIT 3", (row["target_folded"],))]
            evidence = _safe_json(row["evidence_json"])
            previous = int(evidence.get("candidateCount", 0) or 0)
            if len(candidates) == 1:
                evidence.update({"candidateCount": 1, "derivationClass": "deterministic-derived",
                                 "resolutionStatus": "exact"})
                db.execute(
                    "UPDATE relationships SET target_asset_id=?, confidence=1.0, verified=1, evidence_json=? "
                    "WHERE rowid=?",
                    (candidates[0], _canonical_bytes(evidence).decode("utf-8"), int(row["rowid"])),
                )
            elif len(candidates) != previous or evidence.get("resolutionStatus") not in {"ambiguous", "unresolved"}:
                evidence.update({"candidateCount": len(candidates), "derivationClass": "observed",
                                 "resolutionStatus": "ambiguous" if candidates else "unresolved"})
                db.execute(
                    "UPDATE relationships SET confidence=?, verified=0, evidence_json=? WHERE rowid=?",
                    (0.5 if candidates else 0.0, _canonical_bytes(evidence).decode("utf-8"), int(row["rowid"])),
                )

    def rebuild_same_content(self) -> None:
        """Identical bytes under two paths: one deterministic edge per duplicate."""

        db = self.db
        db.execute("DELETE FROM relationships WHERE evidence_kind='content-sha256'")
        rows: list[tuple[Any, ...]] = []
        group_hash = ""
        canonical: tuple[str, str] | None = None
        for row in db.execute(
            "SELECT content_sha256, asset_id, relative_path FROM assets WHERE content_sha256<>'' "
            "ORDER BY content_sha256, folded_path, relative_path"
        ):
            digest = str(row["content_sha256"])
            if digest != group_hash:
                group_hash = digest
                canonical = (str(row["asset_id"]), str(row["relative_path"]))
                continue
            assert canonical is not None
            rows.append((
                str(row["asset_id"]), canonical[0], canonical[1],
                _canonical_bytes({"contentSha256": digest, "derivationClass": "deterministic-derived",
                                  "resolutionStatus": "exact"}).decode("utf-8"),
            ))
            if len(rows) >= 2000:
                self._insert_same_content(rows)
                rows = []
        if rows:
            self._insert_same_content(rows)

    def _insert_same_content(self, rows: Sequence[tuple[Any, ...]]) -> None:
        self.db.executemany(
            "INSERT OR IGNORE INTO relationships(source_asset_id, relation, target_asset_id, target_path, "
            "target_guid, target_folded, evidence_kind, confidence, verified, evidence_json) "
            "VALUES(?, 'same-content', ?, ?, '', '', 'content-sha256', 1.0, 1, ?)",
            rows,
        )

    # -- publication --------------------------------------------------------
    def publish(self, *, root: Path, build_id: str, scan_mode: str, engine: Mapping[str, Any],
                complete: bool, progress_note: Mapping[str, Any] | None, generation: int,
                recompute: bool = False) -> str:
        """Refresh the metadata row set and return the catalog identity."""

        db = self.db
        if recompute:
            self.aggregate, count = _aggregate_row_digests(db)
        else:
            count = int(db.execute("SELECT COUNT(*) AS n FROM assets").fetchone()["n"])
        identity = _catalog_identity(str(root), build_id, self.aggregate, count)
        now = _utc_now()
        values = {
            "schema": CATALOG_SCHEMA,
            "schemaVersion": str(DATABASE_SCHEMA_VERSION),
            "catalogSha256": identity,
            "assetDigest": "%064x" % (self.aggregate & ((1 << 256) - 1)),
            "rootPath": str(root),
            "buildId": build_id,
            "scanMode": scan_mode,
            "updatedAt": now,
            "engine": str(engine.get("engine", "unknown")),
            "engineJson": _canonical_bytes(dict(engine)).decode("utf-8"),
            "parserRevision": str(PARSER_REVISION),
            "complete": "true" if complete else "false",
            "progressJson": _canonical_bytes(dict(progress_note or {})).decode("utf-8"),
            "generation": str(int(generation)),
            "tokenIndex": "fts5" if self.fts else "plain",
        }
        if complete:
            values["valueIndex"] = str(int(db.execute("SELECT COUNT(*) AS n FROM asset_values").fetchone()["n"]))
            values["thumbnailIndex"] = str(int(db.execute("SELECT COUNT(*) AS n FROM asset_thumbnails").fetchone()["n"]))
        db.executemany("INSERT OR REPLACE INTO metadata(key, value) VALUES(?, ?)", sorted(values.items()))
        db.execute("INSERT OR IGNORE INTO metadata(key, value) VALUES('createdAt', ?)", (now,))
        return identity


def _bounded_numeric_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the value index proportionate: a few hundred keys per asset at most."""

    result = dict(metadata)
    values = result.get("numericValues")
    if isinstance(values, list) and len(values) > MAX_NUMERIC_VALUES_PER_ASSET:
        result["numericValues"] = values[:MAX_NUMERIC_VALUES_PER_ASSET]
        result["numericValuesTruncated"] = True
    runs = result.get("numericRuns")
    if isinstance(runs, list) and len(runs) > MAX_NUMERIC_RUNS_PER_ASSET:
        result["numericRuns"] = runs[:MAX_NUMERIC_RUNS_PER_ASSET]
        result["numericRunsTruncated"] = True
    return result


def _numeric_probe_admitted(relative: str, data: bytes) -> bool:
    """Whether a generic file is worth scanning for material-parameter floats.

    Bulk payloads (texture mips, audio, fonts, code, localisation) hold no
    parameter values; scanning them fills the value index with noise and
    megabytes per file. Small unknown descriptors are still scanned.
    """

    suffix = PurePosixPath(relative).suffix.casefold()
    return suffix not in _BULK_SUFFIXES and len(data) <= 256 * 1024


_TEXT_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])-?\d+\.\d{4,}(?:[eE][-+]?\d+)?(?![A-Za-z0-9_.])")


def _text_numeric_values(text: str, limit: int = 512) -> list[str]:
    """Distinctive decimal literals in a text asset (Unity YAML, MTL, JSON)."""

    from .symbols.engines.unreal_package import is_distinctive_value, value_key

    values: list[str] = []
    seen: set[str] = set()
    for match in _TEXT_NUMBER.finditer(text):
        try:
            value = float(match.group(0))
        except ValueError:
            continue
        # Round-trip through float32 so the key matches a GPU-side lane.
        value = struct.unpack("<f", struct.pack("<f", value))[0]
        if not is_distinctive_value(value):
            continue
        key = value_key(value)
        if key in seen:
            continue
        seen.add(key)
        values.append(key)
        if len(values) >= limit:
            break
    return values


_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_TOKEN_FIELDS = ("names", "textures", "parentMaterials", "materialFunctions", "shaderTypes",
                 "vertexFactories", "defines", "className", "exportClasses", "meshes",
                 "parameterCollections", "parameters", "textureParameters", "keywords",
                 "strings", "pathReferences")
MAX_TOKENS_PER_ASSET = 4096


def asset_tokens(relative: str, metadata: Mapping[str, Any]) -> list[str]:
    """Lower-case retrieval tokens: path words, identifiers, camel-case parts."""

    seen: dict[str, None] = {}

    def add(text: Any) -> None:
        if not isinstance(text, str) or not text:
            return
        if text.startswith("/"):
            text = text.rsplit("/", 1)[-1].split(".")[-1]
        for word in _TOKEN_SPLIT.split(text):
            if not word:
                continue
            seen.setdefault(word.casefold(), None)
            parts = [part for part in _CAMEL.split(word) if len(part) >= 2]
            if len(parts) > 1:
                for part in parts:
                    seen.setdefault(part.casefold(), None)

    add(PurePosixPath(relative).stem)
    for part in PurePosixPath(relative).parts[:-1]:
        add(part)
    for field in _TOKEN_FIELDS:
        value = metadata.get(field)
        if isinstance(value, str):
            add(value)
        elif isinstance(value, list):
            for item in value[:1024]:
                add(item if isinstance(item, str) else (item.get("name") if isinstance(item, Mapping) else None))
        if len(seen) >= MAX_TOKENS_PER_ASSET:
            break
    return list(seen)[:MAX_TOKENS_PER_ASSET]


_MATERIAL_CLASS_NAMES = frozenset({
    "Material", "MaterialInstanceConstant", "MaterialInstanceDynamic", "MaterialFunction",
    "MaterialFunctionInstance", "MaterialParameterCollection",
})
_HEADER_KINDS = frozenset({"material", "shader", "metadata", "scene", "mesh"})
HEADER_DEPTH_MAX_BYTES = 512 * 1024
HEADER_DEPTH_MATERIAL_EXPORT_MAX_BYTES = 8 * 1024 * 1024


_TEXTURE_CLASS_NAMES = frozenset({
    "Texture2D", "TextureCube", "VolumeTexture", "Texture2DArray", "TextureCubeArray", "TextureLightProfile",
})


def _deep_admitted(depth_mode: str, kind: str, relative: str, size: int, material_stems: set[str],
                   stem_classes: Mapping[str, str] | None = None) -> bool:
    """Decide whether this file gets hashed+parsed at the requested depth."""

    if depth_mode == "full":
        return True
    if depth_mode == "names":
        return False
    suffix = PurePosixPath(relative).suffix.casefold()
    if suffix in _BULK_SUFFIXES:
        return False
    if kind == "package":
        if suffix in {".uasset", ".umap"}:
            return size <= HEADER_DEPTH_MAX_BYTES * 4
        if suffix == ".uexp":
            # A material's cooked export carries shader types; a texture's
            # carries its small inline mips (the fingerprint). Files sort so
            # the .uasset header comes first and its class is known here.
            stem = PurePosixPath(relative).stem
            if stem in material_stems:
                return size <= HEADER_DEPTH_MATERIAL_EXPORT_MAX_BYTES
            if stem_classes is not None and stem_classes.get(stem) in _TEXTURE_CLASS_NAMES:
                return size <= HEADER_DEPTH_MAX_BYTES * 4
            return False
        return False
    if kind in _HEADER_KINDS:
        return size <= HEADER_DEPTH_MAX_BYTES * 8
    if kind == "texture":
        return size <= HEADER_DEPTH_MAX_BYTES  # header-only parsers, small files
    if kind in {"other", "binary", "metadata"}:
        # generic probe of small unknown files (Unity .asset/.prefab, custom
        # engine descriptors); big serialized blobs stay deferred
        return size <= HEADER_DEPTH_MAX_BYTES
    return False


class AssetCatalogService:
    """Build and query one active-project unpacked-asset relationship catalog."""

    def __init__(self, database_path: str | Path):
        self.database_path = _validate_database_path(database_path)

    @staticmethod
    def capabilities() -> dict[str, Any]:
        return {
            "schema": "renderdoc-ultra-unpacked-asset-capabilities/v1",
            "schemaVersion": 1,
            "catalogSchema": CATALOG_SCHEMA,
            "assetKinds": sorted(_KNOWN_KINDS),
            "fixedParsers": {
                "mesh": ["glTF 2.0 JSON/GLB", "OBJ"],
                "material": ["glTF 2.0 material references", "MTL", "Unity YAML/GUID"],
                "texture": ["PNG", "DDS/DX10", "KTX2", "JPEG"],
                "shader": ["bounded preprocessor include graph"],
            },
            "providerRequired": [
                "semantic FBX or Blend internals",
                "cooked engine containers",
                "proprietary mesh/material/shader layouts",
            ],
            "scanModes": {
                "incremental": "keep rows whose path/size/mtime are unchanged; rewrite the rest in place",
                "full": "rehash and reparse every admitted regular file",
            },
            "relationshipEvidence": [
                "exact relative path reference",
                "exact Unity GUID reference",
                "identical content SHA-256",
            ],
            "policy": {
                "configuredActiveProjectRootOnly": True,
                "symlinksFollowed": False,
                "arbitraryModelPathsAccepted": False,
                "semanticNameGuessPersisted": False,
                "heuristicRelationshipsPersisted": False,
                "contentVerifiedDelivery": True,
                "rowDigestVerifiedOnDelivery": True,
                "indexNotCopy": True,
            },
            "limits": {
                "defaultMaximumFiles": DEFAULT_MAX_FILES,
                "hardMaximumFiles": HARD_MAX_FILES,
                "defaultNamesOnlyMaximumFiles": DEFAULT_NAMES_ONLY_MAX_FILES,
                "hardNamesOnlyMaximumFiles": HARD_NAMES_ONLY_MAX_FILES,
                "hardMaximumFilesystemEntries": HARD_MAX_FILESYSTEM_ENTRIES,
                "defaultMaximumFileSizeMiB": DEFAULT_MAX_FILE_SIZE_MB,
                "hardMaximumFileSizeMiB": HARD_MAX_FILE_SIZE_MB,
                "maximumQueryResults": MAX_QUERY_RESULTS,
                "maximumIndexedReferencesPerAsset": MAX_INDEXED_REFERENCES_PER_ASSET,
            },
        }

    def scan(
        self,
        asset_root: str | Path,
        *,
        build_id: str = "",
        scan_mode: str = "incremental",
        max_files: int | None = None,
        max_file_size_mb: int = DEFAULT_MAX_FILE_SIZE_MB,
        depth: str = "full",
        scope_prefixes: Sequence[str] = (),
        progress: Any = None,
        checkpoint_seconds: float = 90.0,
    ) -> dict[str, Any]:
        """Bring the index up to date with the tree.

        The catalog is one live SQLite index, updated in place: a file whose
        size and mtime are unchanged keeps its row; a changed or new file is
        hashed/parsed and its row rewritten; a file that is gone loses its row
        once the walk is complete. Checkpoints are plain commits, so a reader
        always sees the last committed state and an interrupted scan resumes
        from it.

        ``depth`` chooses how much work each file gets: ``names`` walks only
        (path tokens, no hashing/parsing — seconds); ``headers`` hashes and
        parses the small descriptive files (package headers, materials,
        shaders, metadata, and a material's cooked export sibling) and defers
        bulk/large files; ``full`` hashes and parses everything admitted by
        the size bounds. ``scope_prefixes`` restricts the deep work to files
        whose catalog-relative path starts with one of the prefixes.
        Deferred records can always be upgraded by a later scan or by
        ``hydrate``.
        """

        root = _validate_root(asset_root)
        if not isinstance(build_id, str) or len(build_id) > 256 or "\x00" in build_id:
            raise AssetCatalogError("build_id must be a bounded string")
        mode = str(scan_mode or "").strip().casefold()
        if mode not in {"incremental", "full"}:
            raise AssetCatalogError("scan_mode must be incremental or full")
        depth_mode = str(depth or "full").strip().casefold()
        if depth_mode not in {"full", "headers", "names"}:
            raise AssetCatalogError("depth must be full, headers or names")
        prefixes: list[str] = []
        for prefix in scope_prefixes or ():
            text = str(prefix or "").replace("\\", "/").strip("/")
            if not text or ".." in text.split("/") or len(text) > 4096:
                raise AssetCatalogError("scope_prefixes entries must be relative catalog paths")
            prefixes.append(text.casefold())
        if len(prefixes) > 256:
            raise AssetCatalogError("scope_prefixes exceeds its bound")

        def in_scope(relative_path: str) -> bool:
            if not prefixes:
                return True
            folded = relative_path.casefold()
            return any(folded == prefix or folded.startswith(prefix + "/") or folded.startswith(prefix)
                       for prefix in prefixes)
        if max_files is None:
            max_files = (
                DEFAULT_NAMES_ONLY_MAX_FILES
                if depth_mode == "names"
                else DEFAULT_MAX_FILES
            )
        maximum_admitted_files = (
            HARD_NAMES_ONLY_MAX_FILES if depth_mode == "names" else HARD_MAX_FILES
        )
        if (
            isinstance(max_files, bool)
            or not isinstance(max_files, int)
            or not 1 <= max_files <= maximum_admitted_files
        ):
            raise AssetCatalogError("max_files is outside the fixed scan bound")
        if (
            isinstance(max_file_size_mb, bool)
            or not isinstance(max_file_size_mb, int)
            or not 1 <= max_file_size_mb <= HARD_MAX_FILE_SIZE_MB
        ):
            raise AssetCatalogError("max_file_size_mb is outside the fixed scan bound")
        maximum_file_size = max_file_size_mb * 1024 * 1024

        skipped_symlinks = 0
        reused = 0
        hashed = 0
        parse_failures = 0
        walked = 0
        visited_files = 0
        filesystem_entries = 0
        skipped_unsupported = 0
        deferred = 0
        written = 0
        engine_context = self._detect_engine(
            root,
            ENGINE_DETECTION_SAMPLE_FILES,
            include_unknown=depth_mode != "names",
        )
        engine = engine_context.to_dict()
        db, metadata = self._open_writable(root, build_id)
        try:
            writer = _CatalogWriter(db, metadata)
            generation = int(metadata.get("generation", "0") or "0") + 1
            db.execute("BEGIN")
            pending_stamps: list[int] = []
            last_checkpoint = time.monotonic()
            checkpoints = 0

            def _note() -> dict[str, Any]:
                return {
                    "walked": walked,
                    "visited": visited_files,
                    "skippedUnsupported": skipped_unsupported,
                    "hashed": hashed,
                    "reused": reused,
                    "deferred": deferred,
                    "written": written,
                }

            def _checkpoint() -> None:
                nonlocal last_checkpoint, checkpoints
                writer.stamp(pending_stamps, generation)
                pending_stamps.clear()
                note = _note()
                note.update({"checkpoint": checkpoints + 1, "at": _utc_now()})
                writer.publish(root=root, build_id=build_id, scan_mode=mode, engine=engine,
                               complete=False, progress_note=note, generation=generation)
                db.execute("COMMIT")
                db.execute("BEGIN")
                checkpoints += 1
                last_checkpoint = time.monotonic()

            def _stat_record(relative: str, resolved: Path, size: int, modified_ns: int, kind: str,
                             deep: bool, material_stems: set[str], stem_classes: dict[str, str]) -> dict[str, Any]:
                nonlocal hashed, deferred, parse_failures
                if not deep:
                    deferred += 1
                    return {
                        "assetId": _asset_identity(relative, "", size, modified_ns),
                        "relativePath": relative,
                        "kind": kind,
                        "format": _format_name(PurePosixPath(relative)),
                        "size": size,
                        "modifiedNs": modified_ns,
                        "contentSha256": "",
                        "hashStatus": "deferred",
                        "parseStatus": "deferred",
                        "metadata": {"parser": "deferred", "reason": "outside the requested scan depth/scope"},
                        "references": [],
                    }
                content_sha256 = ""
                hash_status = "skipped-size"
                parsed: dict[str, Any] = {
                    "parser": "none",
                    "reason": "file exceeds the admitted per-file hash/parse bound",
                }
                references: list[dict[str, Any]] = []
                parse_status = "skipped-size"
                if size <= maximum_file_size:
                    content_sha256 = _hash_regular_file(resolved, size)
                    after_hash = resolved.stat()
                    if int(after_hash.st_size) != size or int(after_hash.st_mtime_ns) != modified_ns:
                        raise AssetCatalogError("asset changed while it was hashed: %s" % resolved)
                    hashed += 1
                    hash_status = "sha256"
                    if size <= MAX_PARSE_BYTES:
                        try:
                            data = resolved.read_bytes()
                            after_read = resolved.stat()
                            if int(after_read.st_size) != size or int(after_read.st_mtime_ns) != modified_ns:
                                raise AssetCatalogError("asset changed while it was parsed: %s" % resolved)
                            parsed, references = _parse_asset(
                                relative, kind, data, engine_context,
                                sibling_class=stem_classes.get(PurePosixPath(relative).stem, ""),
                            )
                            parsed["parserRevision"] = PARSER_REVISION
                            parse_status = "parsed" if parsed.get("parser") != "none" else "provider-required"
                            if kind == "package" and parsed.get("className"):
                                stem_classes[PurePosixPath(relative).stem] = str(parsed["className"])
                            if kind == "package" and str(parsed.get("className", "")) in _MATERIAL_CLASS_NAMES:
                                material_stems.add(PurePosixPath(relative).stem)
                        except (OSError, ValueError, struct.error, json.JSONDecodeError) as error:
                            parsed = {"parser": "error", "errorType": type(error).__name__}
                            references = []
                            parse_status = "parse-error"
                            parse_failures += 1
                    else:
                        parse_status = "skipped-parse-size"
                        parsed = {"parser": "none", "reason": "file exceeds fixed parser byte bound"}
                return {
                    "assetId": _asset_identity(relative, content_sha256, size, modified_ns),
                    "relativePath": relative,
                    "kind": kind,
                    "format": _format_name(PurePosixPath(relative)),
                    "size": size,
                    "modifiedNs": modified_ns,
                    "contentSha256": content_sha256,
                    "hashStatus": hash_status,
                    "parseStatus": parse_status,
                    "metadata": _bounded_numeric_metadata(parsed),
                    "references": references,
                }

            for directory, directory_names, file_names in os.walk(root, followlinks=False):
                material_stems: set[str] = set()
                stem_classes: dict[str, str] = {}
                directory_path = Path(directory)
                if _is_redirected(directory_path):
                    raise AssetCatalogError(
                        "asset scan traversed a symbolic link or reparse point"
                    )
                safe_directories: list[str] = []
                for name in sorted(directory_names, key=str.casefold):
                    filesystem_entries += 1
                    if filesystem_entries > HARD_MAX_FILESYSTEM_ENTRIES:
                        raise AssetCatalogError(
                            "asset root exceeds the fixed filesystem traversal bound"
                        )
                    candidate = directory_path / name
                    if _is_redirected(candidate):
                        skipped_symlinks += 1
                    else:
                        safe_directories.append(name)
                directory_names[:] = safe_directories
                for name in sorted(file_names, key=str.casefold):
                    filesystem_entries += 1
                    visited_files += 1
                    if filesystem_entries > HARD_MAX_FILESYSTEM_ENTRIES:
                        raise AssetCatalogError(
                            "asset root exceeds the fixed filesystem traversal bound"
                        )
                    relative_name = PurePosixPath(
                        *(directory_path.relative_to(root).parts if directory_path != root else ()),
                        name,
                    )
                    if depth_mode == "names" and not _names_only_admitted(relative_name):
                        skipped_unsupported += 1
                        continue
                    walked += 1
                    if progress is not None and walked % 500 == 0:
                        try:
                            progress({"phase": "scan", **_note()})
                        except Exception:
                            pass
                    if walked > max_files:
                        raise AssetCatalogScanLimitError(
                            maximum_files=max_files, observed_files=walked
                        )
                    path = directory_path / name
                    if _is_redirected(path) or not path.is_file():
                        skipped_symlinks += 1
                        continue
                    resolved = path.resolve(strict=True)
                    if not _within(root, resolved):
                        raise AssetCatalogError("asset file escaped the configured root")
                    before = resolved.stat()
                    relative = resolved.relative_to(root).as_posix()
                    size = int(before.st_size)
                    modified_ns = int(before.st_mtime_ns)
                    kind = _classify(PurePosixPath(relative))
                    deep = in_scope(relative) and _deep_admitted(
                        depth_mode, kind, relative, size, material_stems, stem_classes)
                    old = writer.lookup(relative)
                    old_current = bool(
                        mode == "incremental"
                        and old is not None
                        and int(old["size"]) == size
                        and int(old["modified_ns"]) == modified_ns
                    )
                    old_complete = bool(
                        old_current
                        and old["content_sha256"]
                        and (
                            old["parse_status"] not in {"parsed", "provider-required", "parse-error"}
                            or int(old["parser_revision"]) == PARSER_REVISION
                        )
                    )
                    if old_complete or (old_current and not deep):
                        reused += 1
                        pending_stamps.append(int(old["rowid"]))
                        if str(old["kind"]) == "package" and old["class_name"]:
                            reused_class = str(old["class_name"])
                            stem_classes[PurePosixPath(relative).stem] = reused_class
                            if reused_class in _MATERIAL_CLASS_NAMES:
                                material_stems.add(PurePosixPath(relative).stem)
                    else:
                        record = _stat_record(relative, resolved, size, modified_ns, kind, deep,
                                              material_stems, stem_classes)
                        if old is not None:
                            writer.delete(int(old["rowid"]), str(old["asset_id"]), str(old["row_sha256"]))
                        writer.write(record, generation)
                        written += 1
                    if len(pending_stamps) >= 2000:
                        writer.stamp(pending_stamps, generation)
                        pending_stamps.clear()
                    if checkpoint_seconds and time.monotonic() - last_checkpoint >= checkpoint_seconds:
                        _checkpoint()
                        if progress is not None:
                            try:
                                progress({"phase": "checkpoint", **_note(), "checkpoint": checkpoints})
                            except Exception:
                                pass
            writer.stamp(pending_stamps, generation)
            pending_stamps.clear()
            if progress is not None:
                try:
                    progress({"phase": "publish", **_note()})
                except Exception:
                    pass
            # The walk covered the whole tree: rows nobody touched are files
            # that no longer exist.
            removed = 0
            while True:
                stale_rows = db.execute(
                    "SELECT rowid, asset_id, row_sha256 FROM assets "
                    "WHERE generation < ? LIMIT 2000",
                    (generation,),
                ).fetchall()
                if not stale_rows:
                    break
                for row in stale_rows:
                    writer.delete(
                        int(row["rowid"]),
                        str(row["asset_id"]),
                        str(row["row_sha256"]),
                    )
                    removed += 1
            if written or removed or metadata.get("complete") != "true":
                writer.resolve_references()
                writer.rebuild_same_content()
            catalog_sha256 = writer.publish(
                root=root, build_id=build_id, scan_mode=mode, engine=engine, complete=True,
                progress_note={**_note(), "removed": removed, "checkpoints": checkpoints, "at": _utc_now()},
                generation=generation, recompute=True)
            db.execute("COMMIT")
            summary = self._scan_summary(db)
        except BaseException:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            db.close()
        return {
            "schema": "renderdoc-ultra-unpacked-asset-index/v1",
            "schemaVersion": 1,
            "catalogSha256": catalog_sha256,
            "databasePath": str(self.database_path),
            "rootPath": str(root),
            "buildId": build_id,
            "scanMode": mode,
            "depth": depth_mode,
            "scopePrefixes": prefixes,
            "engine": engine,
            "assetCount": summary["assetCount"],
            "deferredCount": summary["deferredCount"],
            "relationshipCount": summary["relationshipCount"],
            "verifiedRelationshipCount": summary["verifiedRelationshipCount"],
            "unresolvedRelationshipCount": summary["relationshipCount"] - summary["verifiedRelationshipCount"],
            "countsByKind": summary["countsByKind"],
            "countsByFormat": summary["countsByFormat"],
            "cache": {"reusedFiles": reused, "hashedFiles": hashed, "checkpoints": checkpoints,
                      "writtenFiles": written, "removedFiles": removed},
            "complete": True,
            "diagnostics": {
                "skippedSymlinks": skipped_symlinks,
                "parseFailures": parse_failures,
                "weakIdentityFiles": summary["weakIdentityFiles"],
                "referenceTruncatedAssets": summary["referenceTruncatedAssets"],
            },
            "coverage": {
                "complete": True,
                "truncated": False,
                "walkedFiles": walked,
                "visitedFiles": visited_files,
                "filesystemEntries": filesystem_entries,
                "skippedUnsupportedFiles": skipped_unsupported,
                "maximumFiles": max_files,
                "maximumFilesystemEntries": HARD_MAX_FILESYSTEM_ENTRIES,
                "maximumFileSizeMiB": max_file_size_mb,
            },
            "policy": {
                "projectOwnedCatalog": True,
                "indexNotCopy": True,
                "semanticGuessesPersisted": False,
                "heuristicRelationshipsPersisted": False,
                "unresolvedReferencesRemainUnverified": True,
            },
        }

    @staticmethod
    def _scan_summary(db: sqlite3.Connection) -> dict[str, Any]:
        def _count(sql: str, *parameters: Any) -> int:
            return int(db.execute(sql, parameters).fetchone()["n"])
        return {
            "assetCount": _count("SELECT COUNT(*) AS n FROM assets"),
            "deferredCount": _count("SELECT COUNT(*) AS n FROM assets WHERE hash_status='deferred'"),
            "relationshipCount": _count("SELECT COUNT(*) AS n FROM relationships"),
            "verifiedRelationshipCount": _count("SELECT COUNT(*) AS n FROM relationships WHERE verified=1"),
            "weakIdentityFiles": _count("SELECT COUNT(*) AS n FROM assets WHERE content_sha256=''"),
            "referenceTruncatedAssets": _count(
                "SELECT COUNT(*) AS n FROM assets WHERE metadata_json LIKE '%\"referencesTruncated\":true%'"),
            "countsByKind": {str(row["kind"]): int(row["n"]) for row in db.execute(
                "SELECT kind, COUNT(*) AS n FROM assets GROUP BY kind ORDER BY kind")},
            "countsByFormat": {str(row["format"]): int(row["n"]) for row in db.execute(
                "SELECT format, COUNT(*) AS n FROM assets GROUP BY format ORDER BY format")},
        }

    def hydrate(self, asset_root: str | Path, relative_paths: Sequence[str], *,
                max_files: int = DEFAULT_MAX_FILES) -> dict[str, Any]:
        """Upgrade a few deferred records to full hash+parse without a walk."""

        root = _validate_root(asset_root)
        wanted: list[str] = []
        for value in list(relative_paths)[:256]:
            text = str(value or "").replace("\\", "/").strip("/")
            parts = text.split("/")
            if not text or any(part in {"", ".", ".."} for part in parts) or len(text) > 4096:
                raise AssetCatalogError("hydrate paths must be relative catalog paths")
            if text not in wanted:
                wanted.append(text)
        db, metadata = self._open_read_only()
        try:
            build_id = str(metadata.get("buildId", "") or "")
            if Path(metadata.get("rootPath", "")).resolve() != root:
                raise AssetCatalogError("asset catalog belongs to another configured root")
        finally:
            db.close()
        engine_context = self._stored_engine(metadata) or self._detect_engine(root, max_files)
        db, metadata = self._open_writable(root, build_id)
        hydrated: list[str] = []
        missing: list[str] = []
        try:
            writer = _CatalogWriter(db, metadata)
            generation = int(metadata.get("generation", "0") or "0")
            db.execute("BEGIN")
            for relative in wanted:
                cursor = root
                redirected = False
                for part in PurePosixPath(relative).parts:
                    cursor = cursor / part
                    if _is_redirected(cursor):
                        redirected = True
                        break
                if redirected or not cursor.is_file():
                    missing.append(relative)
                    continue
                resolved = cursor.resolve(strict=True)
                if not _within(root, resolved):
                    raise AssetCatalogError("asset file escaped the configured root")
                before = resolved.stat()
                size = int(before.st_size)
                modified_ns = int(before.st_mtime_ns)
                kind = _classify(PurePosixPath(relative))
                sibling = writer.lookup(PurePosixPath(relative).with_suffix(".uasset").as_posix())
                stem_classes = {PurePosixPath(relative).stem: str(sibling["class_name"])} \
                    if sibling is not None and sibling["class_name"] else {}
                material_stems = {PurePosixPath(relative).stem} \
                    if stem_classes.get(PurePosixPath(relative).stem) in _MATERIAL_CLASS_NAMES else set()
                record = self._hydrate_record(relative, resolved, size, modified_ns, kind,
                                              engine_context, stem_classes, material_stems)
                old = writer.lookup(relative)
                if old is not None:
                    writer.delete(int(old["rowid"]), str(old["asset_id"]), str(old["row_sha256"]))
                writer.write(record, generation if old is None else max(generation, 1))
                hydrated.append(relative)
            if hydrated:
                writer.resolve_references()
                writer.rebuild_same_content()
            catalog_sha256 = writer.publish(
                root=root, build_id=build_id, scan_mode=metadata.get("scanMode", "incremental"),
                engine=_safe_json(metadata.get("engineJson", "")) or {"engine": metadata.get("engine", "unknown")},
                complete=metadata.get("complete") == "true",
                progress_note=_safe_json(metadata.get("progressJson", "")), generation=generation)
            db.execute("COMMIT")
        except BaseException:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            db.close()
        return {
            "schema": "renderdoc-ultra-unpacked-asset-hydrate/v1",
            "schemaVersion": 1,
            "catalogSha256": catalog_sha256,
            "hydrated": hydrated,
            "missing": missing,
        }

    def index_texture_preview(self, asset_root: str | Path, relative_path: str,
                              preview_path: str | Path, *, expected_preview_sha256: str,
                              expected_source_sha256: str, provenance: Mapping[str, Any]) -> dict[str, Any]:
        """Index one Host-verified reader output under its exact original asset.

        This private API requires the caller to authenticate the reader receipt
        and its complete input closure. It does not turn a supplied PNG or a
        perceptual match into physical GPU identity. Search rechecks all inputs.
        """
        root = _validate_root(asset_root)
        identity_db, identity_metadata = self._open_read_only()
        try:
            if Path(identity_metadata.get("rootPath", "")).resolve() != root:
                raise AssetCatalogError("texture preview catalog belongs to another configured root")
        finally:
            identity_db.close()
        relative = str(relative_path).replace("\\", "/")
        if not isinstance(provenance, Mapping):
            raise AssetCatalogError("texture preview requires verified reader provenance")
        operation = str(provenance.get("operationId", ""))
        receipt_sha = str(provenance.get("receiptSha256", ""))
        if not operation or len(operation) > 256 or not _SHA256_RE.fullmatch(receipt_sha):
            raise AssetCatalogError("texture preview requires a verified operation and receipt digest")
        inputs = _preview_input_identity(root, provenance.get("sourceFiles"))
        if not any(item["relativePath"] == relative and item["sha256"] == expected_source_sha256 for item in inputs):
            raise AssetCatalogError("texture preview input closure does not bind the original asset")
        output = Path(preview_path).resolve()
        data = _preview_bytes(Path(preview_path), expected_preview_sha256)
        fingerprint = _image_fingerprint(data, ".png", reader_preview=True)
        if not fingerprint.get("available"):
            return {"indexed": False, "relativePath": relative,
                    "reason": fingerprint.get("reason")}
        # Reuse a current hashed owner. Hydrating it again would unnecessarily
        # rebuild reference indexes for an already known original asset.
        current = self.query(relative_paths=[relative], include_relationships=False, limit=1)
        if not current["assets"] or current["assets"][0]["contentSha256"] != expected_source_sha256:
            self.hydrate(root, [relative])
        db, metadata = self._open_read_only()
        try:
            build_id = str(metadata.get("buildId", ""))
        finally:
            db.close()
        # A preview augments an existing catalog. It must never recreate a
        # catalog selected by another writer, or publish from metadata read
        # before that writer committed a different row aggregate.
        db = _database_connection(self.database_path, read_only=False)
        try:
            db.execute("BEGIN IMMEDIATE")
            metadata = _validate_schema(db)
            if (Path(metadata.get("rootPath", "")).resolve() != root
                    or metadata.get("buildId", "") != build_id):
                raise AssetCatalogError("texture preview catalog changed during registration")
            row = db.execute("SELECT " + _ASSET_COLUMNS + " FROM assets WHERE relative_path=?", (relative,)).fetchone()
            if row is None:
                raise AssetCatalogError("texture preview original asset is not indexed")
            record = _row_record(row)
            if record["contentSha256"] != expected_source_sha256:
                raise AssetCatalogError("texture preview original asset changed during registration")
            # Recheck the closure after hydration before committing derived data.
            _preview_input_identity(root, inputs)
            _preview_bytes(output, expected_preview_sha256)
            binding = {"operationId": operation, "receiptSha256": receipt_sha,
                       "sourceFiles": inputs, "previewPath": str(output),
                       "previewSha256": expected_preview_sha256,
                       "colorSpace": str(provenance.get("colorSpace", "unspecified"))[:64],
                       "channels": str(provenance.get("channels", "reader-decoded-rgba"))[:64]}
            fingerprint["previewProvenance"] = binding
            fingerprint["evidenceKind"] = "decoded-reader-preview"
            record["metadata"] = {**record["metadata"], "textureFingerprint": fingerprint}
            new_digest = _row_digest(record)
            writer = _CatalogWriter(db, metadata)
            writer.aggregate ^= int(str(row["row_sha256"]), 16) ^ int(new_digest, 16)
            db.execute("UPDATE assets SET metadata_json=?,row_sha256=? WHERE asset_id=?",
                       (_canonical_bytes(record["metadata"]).decode("utf-8"), new_digest, record["assetId"]))
            writer.write_thumbnail(record["assetId"], fingerprint)
            identity = writer.publish(root=root, build_id=build_id,
                scan_mode=metadata.get("scanMode", "incremental"),
                engine=_safe_json(metadata.get("engineJson", "")),
                complete=metadata.get("complete") == "true", generation=int(metadata.get("generation", "0")),
                progress_note=_safe_json(metadata.get("progressJson", "")))
            db.execute("COMMIT")
        except BaseException:
            try:
                db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            db.close()
        return {"indexed": True, "catalogSha256": identity, "assetId": record["assetId"],
                "relativePath": relative, "previewSha256": expected_preview_sha256,
                "sourceFiles": inputs, "physicalIdentityVerified": False,
                "evidenceKind": "decoded-reader-preview", "flat": bool(fingerprint.get("flat"))}

    def resolve_unreal_texture_paths(self, asset_root: str | Path, references: Sequence[str], *,
                                     reader_mount_roots: Sequence[str | Path] = (),
                                     hydrate_current: bool = False) -> dict[str, Any]:
        """Resolve complete object references through recorded mounts and current bytes.

        UEX's numeric export suffix is admitted only for export zero of a
        single-export texture package. No basename search or guessed directory
        prefix participates in this mapping.
        """
        from .symbols.engines.unreal_package import map_object_path

        root = _validate_root(asset_root)
        values = list(dict.fromkeys(references))
        if len(values) > 200 or any(not isinstance(item, str) or len(item) > 4096 for item in values):
            raise AssetCatalogError("texture reference resolution exceeds its bounded path limit")
        if hydrate_current and len(values) != 1:
            raise AssetCatalogError("texture preview hydration requires one exact reference")
        reader_roots: set[Path] = set()
        for value in list(reader_mount_roots)[:16]:
            candidate = Path(value)
            if not _is_redirected(candidate) and candidate.is_dir():
                candidate = candidate.resolve()
                if candidate == root or root in candidate.parents:
                    reader_roots.add(candidate)
        db, metadata = self._open_read_only()
        try:
            if Path(metadata.get("rootPath", "")).resolve() != root:
                raise AssetCatalogError("texture references belong to another configured catalog root")
            engine = _safe_json(metadata.get("engineJson", ""))
            if engine.get("engine") != "unreal":
                return {"available": False, "reason": "catalog-is-not-unreal", "resolved": {}}
            mounts = engine.get("mounts") or {}
            resolved_paths: dict[str, str] = {}
            unavailable: list[dict[str, str]] = []
            checked: dict[str, dict[str, Any] | None] = {}
            for reference in values:
                text = reference.strip().replace("\\", "/")
                if "'" in text and text.endswith("'"):
                    text = text.split("'", 1)[1][:-1]
                package, separator, suffix = text.rpartition(".")
                numeric_export = bool(separator and suffix.isdecimal())
                if separator and (numeric_export or suffix.casefold() == "uasset"
                                  or suffix == PurePosixPath(package).name):
                    text = package
                elif separator:
                    unavailable.append({"reference": reference, "reason": "unsupported-object-export"})
                    continue
                if numeric_export and suffix != "0":
                    unavailable.append({"reference": reference, "reason": "export-identity-unavailable"})
                    continue
                candidates: set[str] = set()
                if text.startswith("/"):
                    mapped = map_object_path(text, mounts)
                    if mapped:
                        candidates.add(mapped)
                elif ":" not in text and all(part not in {"", ".", ".."} for part in text.split("/")):
                    candidates.add(text + ".uasset")
                    for mount_root in reader_roots:
                        target = mount_root / (text + ".uasset")
                        candidates.add(target.relative_to(root).as_posix())
                if hydrate_current:
                    # The optional preview path upgrades only one already-listed
                    # package. A directory-only catalog is enough to locate it,
                    # but neither a basename nor a competing mount is guessed.
                    indexed = []
                    for relative in candidates:
                        if not any(relative.startswith(str(base).rstrip("/") + "/") for base in mounts.values()):
                            continue
                        path = root / relative
                        if (not path.is_file() or path.stat().st_size > MAX_PREVIEW_INPUT_BYTES
                                or root not in path.resolve().parents
                                or any(_is_redirected(part) for part in (path, *path.parents))):
                            continue
                        row = db.execute("SELECT " + _ASSET_COLUMNS + " FROM assets WHERE relative_path=?",
                                         (relative,)).fetchone()
                        if row is not None:
                            indexed.append((relative, path, _row_record(row)))
                    if len(indexed) == 1:
                        relative, path, record = indexed[0]
                        known_class = record["metadata"].get("className")
                        if (not known_class or known_class in _TEXTURE_CLASS_NAMES) and (
                                not record["contentSha256"] or not known_class
                                or path.stat().st_size != record["size"]
                                or _hash_regular_file(path, path.stat().st_size) != record["contentSha256"]):
                            db.close()
                            self.hydrate(root, [relative], max_files=1)
                            return self.resolve_unreal_texture_paths(
                                root, values, reader_mount_roots=reader_mount_roots)
                    elif len(indexed) > 1:
                        unavailable.append({"reference": reference, "reason": "ambiguous-current-assets"})
                        continue
                matches: set[str] = set()
                for relative in candidates:
                    # Reader-root paths must also belong to the catalog's
                    # recorded content mounts, including plugin mounts.
                    if not any(relative.startswith(str(base).rstrip("/") + "/") for base in mounts.values()):
                        continue
                    if relative not in checked:
                        checked[relative] = None
                        path = root / relative
                        if _is_redirected(path) or not path.is_file() or root not in path.resolve().parents:
                            continue
                        row = db.execute("SELECT " + _ASSET_COLUMNS + " FROM assets WHERE relative_path=?",
                                         (relative,)).fetchone()
                        if row is None:
                            continue
                        record = _row_record(row)
                        size = path.stat().st_size
                        if (size != record["size"] or size > MAX_PREVIEW_INPUT_BYTES
                                or not record["contentSha256"]
                                or _hash_regular_file(path, size) != record["contentSha256"]):
                            continue
                        checked[relative] = record
                    record = checked[relative]
                    if record is None:
                        continue
                    details = record["metadata"]
                    if details.get("className") not in _TEXTURE_CLASS_NAMES:
                        continue
                    if numeric_export and details.get("exportCount") != 1:
                        continue
                    matches.add(relative)
                if len(matches) == 1:
                    resolved_paths[reference] = next(iter(matches))
                else:
                    unavailable.append({"reference": reference,
                                        "reason": "ambiguous-current-assets" if matches else
                                                  "no-current-exact-mounted-texture"})
            return {"available": True, "catalogSha256": metadata["catalogSha256"],
                    "resolved": resolved_paths, "unavailable": unavailable}
        finally:
            db.close()

    @staticmethod
    def _hydrate_record(relative: str, resolved: Path, size: int, modified_ns: int, kind: str,
                        engine_context: Any, stem_classes: dict[str, str],
                        material_stems: set[str]) -> dict[str, Any]:
        content_sha256 = ""
        hash_status = "skipped-size"
        parsed: dict[str, Any] = {"parser": "none", "reason": "file exceeds the admitted per-file hash/parse bound"}
        references: list[dict[str, Any]] = []
        parse_status = "skipped-size"
        if size <= DEFAULT_MAX_FILE_SIZE_MB * 1024 * 1024:
            content_sha256 = _hash_regular_file(resolved, size)
            hash_status = "sha256"
            if size <= MAX_PARSE_BYTES:
                try:
                    data = resolved.read_bytes()
                    parsed, references = _parse_asset(
                        relative, kind, data, engine_context,
                        sibling_class=stem_classes.get(PurePosixPath(relative).stem, ""))
                    parsed["parserRevision"] = PARSER_REVISION
                    parse_status = "parsed" if parsed.get("parser") != "none" else "provider-required"
                except (OSError, ValueError, struct.error, json.JSONDecodeError) as error:
                    parsed = {"parser": "error", "errorType": type(error).__name__}
                    references = []
                    parse_status = "parse-error"
            else:
                parse_status = "skipped-parse-size"
                parsed = {"parser": "none", "reason": "file exceeds fixed parser byte bound"}
        return {
            "assetId": _asset_identity(relative, content_sha256, size, modified_ns),
            "relativePath": relative,
            "kind": kind,
            "format": _format_name(PurePosixPath(relative)),
            "size": size,
            "modifiedNs": modified_ns,
            "contentSha256": content_sha256,
            "hashStatus": hash_status,
            "parseStatus": parse_status,
            "metadata": _bounded_numeric_metadata(parsed),
            "references": references,
        }

    @staticmethod
    def _stored_engine(metadata: Mapping[str, str]) -> Any:
        """The engine context the last scan recorded, so a hydration skips the walk."""

        from .symbols.engines import EngineContext

        stored = _safe_json(metadata.get("engineJson", ""))
        engine = str(stored.get("engine", "") or "")
        if not engine:
            return None
        return EngineContext(engine, stored.get("details") or {}, stored.get("mounts") or {})

    @staticmethod
    def _detect_engine(
        root: Path,
        max_files: int,
        *,
        include_unknown: bool = False,
    ) -> Any:
        """Cheap name pre-pass so parsers know the engine and asset relations.

        A names-only scan deliberately admits only supported formats.  A
        headers/full scan also indexes unknown formats, so its relationship
        index must see those same names; otherwise a material can mention an
        asset which is present in the catalog but impossible to resolve.
        """

        from .symbols.engines import detect_engine

        sample_limit = max(1, min(int(max_files), ENGINE_DETECTION_SAMPLE_FILES))
        visited_entries = 0
        relative_paths: list[str] = []
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            directory_path = Path(directory)
            safe_directories: list[str] = []
            for name in sorted(directory_names, key=str.casefold):
                visited_entries += 1
                if visited_entries > HARD_MAX_FILESYSTEM_ENTRIES:
                    raise AssetCatalogError(
                        "asset root exceeds the fixed filesystem traversal bound"
                    )
                if not _is_redirected(directory_path / name):
                    safe_directories.append(name)
            directory_names[:] = safe_directories
            base = directory_path.relative_to(root).as_posix() if directory_path != root else ""
            for name in sorted(file_names, key=str.casefold):
                visited_entries += 1
                if visited_entries > HARD_MAX_FILESYSTEM_ENTRIES:
                    raise AssetCatalogError(
                        "asset root exceeds the fixed filesystem traversal bound"
                    )
                relative = "%s/%s" % (base, name) if base else name
                if (
                    not include_unknown
                    and not _names_only_admitted(PurePosixPath(relative))
                ):
                    continue
                relative_paths.append(relative)
                if len(relative_paths) >= sample_limit:
                    break
            if len(relative_paths) >= sample_limit:
                break
        return detect_engine(relative_paths)

    def _open_writable(self, root: Path, build_id: str) -> tuple[sqlite3.Connection, dict[str, str]]:
        """The live index for this root/build: opened in place, created if absent.

        A valid catalog of another root or build is transactionally emptied
        before its new identity is published. An older schema or damaged file
        must be removed completely; a Windows sharing violation is a surfaced
        retryable failure, never permission to reuse old rows under new
        metadata. Abandoned temporaries of older whole-file rewrites are
        removed here too.
        """

        _cleanup_stale_temporaries(self.database_path)
        path = self.database_path
        recreate = not path.exists()
        reset_existing = False
        metadata: dict[str, str] = {}
        if not recreate:
            if path.is_symlink() or not path.is_file():
                raise AssetCatalogError("catalog database must be a regular file")
            try:
                with closing(_database_connection(path, read_only=False)) as probe:
                    metadata = _validate_schema(probe)
                    if (os.path.normcase(str(metadata.get("rootPath", ""))) != os.path.normcase(str(root))
                            or str(metadata.get("buildId", "")) != build_id):
                        reset_existing = True
            except (AssetCatalogError, sqlite3.Error):
                recreate = True
            if recreate:
                _remove_database_files(path)
                metadata = {}
        db = _database_connection(path, read_only=False)
        try:
            if recreate or reset_existing:
                metadata = _initialise_catalog_database(
                    db, root, build_id, reset_existing=reset_existing
                )
        except BaseException:
            db.close()
            raise
        return db, metadata

    def prepare_root(self, asset_root: str | Path, *, build_id: str = "") -> dict[str, Any]:
        """Synchronously bind storage to one root/build before a background scan.

        This is intentionally a metadata/storage preparation only: it performs
        no tree walk, hashing or parsing.  Root switches therefore surface a
        sharing/transaction failure to the initiating call instead of first
        reporting a background job and failing later after the old catalog was
        accidentally relabelled.
        """

        root = _validate_root(asset_root)
        if not isinstance(build_id, str) or len(build_id) > 256 or "\x00" in build_id:
            raise AssetCatalogError("build_id must be a bounded string")
        db, metadata = self._open_writable(root, build_id)
        try:
            return {
                "rootPath": str(root),
                "buildId": build_id,
                "catalogSha256": str(metadata.get("catalogSha256") or ""),
                "generation": int(metadata.get("generation", "0") or 0),
                "complete": metadata.get("complete") == "true",
            }
        finally:
            db.close()

    def _open_read_only(self) -> tuple[sqlite3.Connection, dict[str, str]]:
        if self.database_path.is_symlink() or not self.database_path.is_file():
            raise AssetCatalogError("active project has no prepared unpacked-asset catalog")
        connection = _database_connection(self.database_path, read_only=True)
        try:
            # One snapshot for the whole read, even while a scan commits.
            connection.execute("BEGIN")
            metadata = _validate_schema(connection)
        except Exception:
            connection.close()
            raise
        return connection, metadata

    def verify(self) -> dict[str, Any]:
        """Audit every row digest against the catalog identity (not on the read path)."""

        db, metadata = self._open_read_only()
        try:
            _validate_catalog_identity(db, metadata)
            checked = 0
            for row in db.execute("SELECT %s FROM assets" % _ASSET_COLUMNS):
                _row_record(row)
                checked += 1
            return {"verified": True, "assetCount": checked, "catalogSha256": metadata["catalogSha256"]}
        finally:
            db.close()

    def content_manifest_status(self, asset_root: str | Path) -> dict[str, Any]:
        """Read the catalog-owner content generation without walking the tree."""

        root = _validate_root(asset_root)
        db, metadata = self._open_read_only()
        try:
            if Path(metadata.get("rootPath", "")).resolve() != root:
                raise AssetCatalogError(
                    "asset content manifest belongs to another configured root"
                )
            schema = metadata.get("contentManifestSchema", "")
            catalog_sha = metadata.get("catalogSha256", "")
            catalog_generation = int(metadata.get("generation", "0") or 0)
            owner_catalog_sha = metadata.get("contentManifestCatalogSha256", "")
            owner_catalog_generation = int(
                metadata.get("contentManifestCatalogGeneration", "-1") or -1
            )
            document_sha = metadata.get("contentManifestDocumentSha256", "")
            content_root = metadata.get("contentManifestRootSha256", "")
            snapshot = metadata.get("contentManifestSnapshotSha256", "")
            generation = int(metadata.get("contentManifestGeneration", "0") or 0)
            file_count = int(metadata.get("contentManifestFileCount", "-1") or -1)
            byte_size = int(metadata.get("contentManifestByteSize", "-1") or -1)
            manifest_path = self.database_path.parent / CONTENT_MANIFEST_FILENAME
            if (
                schema != CONTENT_MANIFEST_SCHEMA
                or owner_catalog_sha != catalog_sha
                or owner_catalog_generation != catalog_generation
                or not _SHA256_RE.fullmatch(document_sha)
                or not _SHA256_RE.fullmatch(content_root)
                or not _SHA256_RE.fullmatch(snapshot)
                or generation <= 0 or file_count < 0 or byte_size < 0
                or manifest_path.is_symlink() or not manifest_path.is_file()
            ):
                raise AssetCatalogError(
                    "asset content manifest is missing or stale for this catalog generation"
                )
            return {
                "ready": True,
                "assetRootManifestGeneration": generation,
                "assetRootManifestSha256": content_root,
                "assetRootSnapshotSha256": snapshot,
                "assetRootFileCount": file_count,
                "assetRootByteSize": byte_size,
                "assetRootManifestDocumentSha256": document_sha,
                "assetRootManifestCatalogGeneration": catalog_generation,
            }
        finally:
            db.close()

    def refresh_content_manifest(self, asset_root: str | Path) -> dict[str, Any]:
        """Publish one complete per-file content generation owned by this catalog.

        This is the expensive boundary run by the background/explicit index
        owner.  It walks the full unpacked tree, reuses SHA-256 only when the OS
        change token is unchanged, and publishes a Merkle root into catalog
        metadata.  Ordinary gates then call :meth:`content_manifest_status` and
        do no walk/stat/content reads.
        """

        root = _validate_root(asset_root)
        db, metadata = self._open_read_only()
        try:
            if Path(metadata.get("rootPath", "")).resolve() != root:
                raise AssetCatalogError(
                    "asset content manifest belongs to another configured root"
                )
            catalog_sha = str(metadata["catalogSha256"])
            catalog_generation = int(metadata.get("generation", "0") or 0)
            prior_document_sha = str(
                metadata.get("contentManifestDocumentSha256", "") or ""
            )
            prior_generation = int(
                metadata.get("contentManifestGeneration", "0") or 0
            )
        finally:
            db.close()

        manifest_path = self.database_path.parent / CONTENT_MANIFEST_FILENAME
        prior: dict[str, Any] | None = None
        if _SHA256_RE.fullmatch(prior_document_sha):
            try:
                if manifest_path.stat().st_size > 512 * 1024 * 1024:
                    raise AssetCatalogError("asset content manifest exceeds its byte bound")
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                checked = _content_manifest_document(loaded)
                if (
                    checked["documentSha256"] == prior_document_sha
                    and os.path.normcase(str(checked.get("assetRoot") or ""))
                    == os.path.normcase(str(root))
                ):
                    prior = checked
            except (OSError, ValueError, AssetCatalogError):
                prior = None

        excluded = self.database_path.parent.resolve()

        def tree_metadata() -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = []
            for directory, directory_names, file_names in os.walk(
                root, followlinks=False
            ):
                directory_path = Path(directory)
                if _is_redirected(directory_path):
                    raise AssetCatalogError(
                        "asset content manifest traversed a redirected directory"
                    )
                safe_directories: list[str] = []
                for name in sorted(directory_names, key=str.casefold):
                    candidate = directory_path / name
                    resolved = candidate.resolve()
                    if resolved == excluded or _is_redirected(candidate):
                        continue
                    safe_directories.append(name)
                directory_names[:] = safe_directories
                for name in sorted(file_names, key=str.casefold):
                    path = directory_path / name
                    if _is_redirected(path) or not path.is_file():
                        continue
                    resolved = path.resolve(strict=True)
                    if not _within(root, resolved) or _within(excluded, resolved):
                        continue
                    if len(rows) >= HARD_MAX_FILES:
                        raise AssetCatalogScanLimitError(
                            maximum_files=HARD_MAX_FILES,
                            observed_files=len(rows) + 1,
                        )
                    stat = resolved.stat()
                    rows.append({
                        "relativePath": resolved.relative_to(root).as_posix(),
                        "size": int(stat.st_size),
                        "mtimeNs": int(stat.st_mtime_ns),
                        "changeToken": _file_change_token(resolved, stat),
                        "fileId": int(getattr(stat, "st_ino", 0) or 0),
                    })
            return rows

        metadata_rows = tree_metadata()
        metadata_snapshot = _canonical_sha256(metadata_rows)
        prior_rows = {
            str(item.get("relativePath") or ""): item
            for item in ((prior or {}).get("files") or [])
            if isinstance(item, Mapping)
        }
        files: list[dict[str, Any]] = []
        for item in metadata_rows:
            relative = str(item["relativePath"])
            old = prior_rows.get(relative)
            fingerprint = (
                item["size"], item["mtimeNs"], item["changeToken"], item["fileId"]
            )
            old_fingerprint = (
                old.get("size"), old.get("mtimeNs"),
                old.get("changeToken"), old.get("fileId"),
            ) if isinstance(old, Mapping) else ()
            content_sha = (
                str(old.get("contentSha256") or "").lower()
                if old_fingerprint == fingerprint else ""
            )
            if not _SHA256_RE.fullmatch(content_sha):
                path = root / PurePosixPath(relative)
                before = path.stat()
                before_fingerprint = (
                    int(before.st_size), int(before.st_mtime_ns),
                    _file_change_token(path, before),
                    int(getattr(before, "st_ino", 0) or 0),
                )
                content_sha = _content_manifest_file_sha256(path)
                after = path.stat()
                after_fingerprint = (
                    int(after.st_size), int(after.st_mtime_ns),
                    _file_change_token(path, after),
                    int(getattr(after, "st_ino", 0) or 0),
                )
                if before_fingerprint != fingerprint or after_fingerprint != fingerprint:
                    raise AssetCatalogError(
                        "asset root changed while its content manifest was read"
                    )
            files.append({**item, "contentSha256": content_sha})
        if tree_metadata() != metadata_rows:
            raise AssetCatalogError(
                "asset root changed while its content manifest generation was built"
            )
        content_rows = [
            {
                "relativePath": item["relativePath"],
                "size": item["size"],
                "contentSha256": item["contentSha256"],
            }
            for item in files
        ]
        payload = {
            "schema": CONTENT_MANIFEST_SCHEMA,
            "schemaVersion": 1,
            "assetRoot": str(root),
            "catalogSha256": catalog_sha,
            "catalogGeneration": catalog_generation,
            "generation": max(prior_generation, int((prior or {}).get("generation") or 0)) + 1,
            "metadataSnapshotSha256": metadata_snapshot,
            "contentRootSha256": _canonical_sha256(content_rows),
            "fileCount": len(files),
            "byteSize": sum(int(item["size"]) for item in files),
            "files": files,
        }
        payload["documentSha256"] = _canonical_sha256(payload)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = manifest_path.with_name(manifest_path.name + ".tmp")
        temporary.write_text(
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ),
            encoding="utf-8",
        )
        _content_manifest_document(payload)
        os.replace(temporary, manifest_path)

        writable = _database_connection(self.database_path, read_only=False)
        try:
            writable.execute("BEGIN IMMEDIATE")
            current = _validate_schema(writable)
            if (
                current.get("catalogSha256") != catalog_sha
                or int(current.get("generation", "0") or 0) != catalog_generation
                or Path(current.get("rootPath", "")).resolve() != root
            ):
                raise AssetCatalogError(
                    "asset catalog generation changed during content-manifest publication"
                )
            values = {
                "contentManifestSchema": CONTENT_MANIFEST_SCHEMA,
                "contentManifestGeneration": str(payload["generation"]),
                "contentManifestDocumentSha256": payload["documentSha256"],
                "contentManifestRootSha256": payload["contentRootSha256"],
                "contentManifestSnapshotSha256": metadata_snapshot,
                "contentManifestFileCount": str(payload["fileCount"]),
                "contentManifestByteSize": str(payload["byteSize"]),
                "contentManifestCatalogSha256": catalog_sha,
                "contentManifestCatalogGeneration": str(catalog_generation),
                "contentManifestUpdatedAt": _utc_now(),
            }
            writable.executemany(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES(?, ?)",
                sorted(values.items()),
            )
            writable.execute("COMMIT")
        except BaseException:
            try:
                writable.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            writable.close()
        return self.content_manifest_status(root)

    def status(self) -> dict[str, Any]:
        db, metadata = self._open_read_only()
        try:
            asset_count = int(db.execute("SELECT COUNT(*) AS n FROM assets").fetchone()["n"])
            relationship_count = int(
                db.execute("SELECT COUNT(*) AS n FROM relationships").fetchone()["n"]
            )
            counts = {
                str(row["kind"]): int(row["n"])
                for row in db.execute("SELECT kind, COUNT(*) AS n FROM assets GROUP BY kind ORDER BY kind")
            }
            result = {
                "schema": "renderdoc-ultra-unpacked-asset-index-status/v1",
                "schemaVersion": 1,
                "ready": True,
                "catalogSha256": metadata["catalogSha256"],
                "rootPath": metadata["rootPath"],
                "buildId": metadata.get("buildId", ""),
                "scanMode": metadata.get("scanMode", ""),
                "createdAt": metadata.get("createdAt", ""),
                "updatedAt": metadata.get("updatedAt", metadata.get("createdAt", "")),
                "generation": int(metadata.get("generation", "0") or 0),
                "databasePath": str(self.database_path),
                "engine": metadata.get("engine", "unknown"),
                "tokenIndex": metadata.get("tokenIndex", "none"),
                "assetCount": asset_count,
                "relationshipCount": relationship_count,
                "countsByKind": counts,
                "complete": metadata.get("complete", "true") != "false",
                "progress": _safe_json(metadata.get("progressJson", "")),
            }
            try:
                result["contentManifest"] = self.content_manifest_status(
                    metadata["rootPath"]
                )
            except AssetCatalogError:
                result["contentManifest"] = {"ready": False}
            return result
        finally:
            db.close()

    def query(
        self,
        *,
        text: str = "",
        relative_paths: Sequence[str] = (),
        kinds: Sequence[str] = (),
        sha256: str = "",
        asset_ids: Sequence[str] = (),
        limit: int = 20,
        include_relationships: bool = True,
    ) -> dict[str, Any]:
        if not isinstance(text, str) or len(text) > 512 or "\x00" in text:
            raise AssetCatalogError("text query must be a bounded string")
        paths = tuple(str(path).replace("\\", "/") for path in relative_paths)
        if (len(paths) > MAX_QUERY_IDS or len(set(paths)) != len(paths)
                or any(not path or len(path) > 4096 or "\x00" in path
                       or any(part in {"", ".", ".."} for part in path.split("/")) for path in paths)):
            raise AssetCatalogError("relative_paths must be bounded exact catalog paths")
        normalized_kinds = tuple(str(item).strip().casefold() for item in kinds)
        if len(normalized_kinds) > len(_KNOWN_KINDS) or len(set(normalized_kinds)) != len(normalized_kinds):
            raise AssetCatalogError("kinds must be a small unique list")
        if set(normalized_kinds) - _KNOWN_KINDS:
            raise AssetCatalogError("query contains an unknown asset kind")
        digest = str(sha256 or "").strip().lower()
        if digest and not _SHA256_RE.fullmatch(digest):
            raise AssetCatalogError("sha256 must be lowercase hexadecimal")
        normalized_ids = tuple(str(item).strip() for item in asset_ids)
        if len(normalized_ids) > MAX_QUERY_IDS or len(set(normalized_ids)) != len(normalized_ids):
            raise AssetCatalogError("asset_ids must be a bounded unique list")
        if any(not _ASSET_ID_RE.fullmatch(item) for item in normalized_ids):
            raise AssetCatalogError("asset_ids contains an invalid asset identity")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_QUERY_RESULTS:
            raise AssetCatalogError("limit is outside the fixed query bound")
        if not isinstance(include_relationships, bool):
            raise AssetCatalogError("include_relationships must be a boolean")
        db, metadata = self._open_read_only()
        try:
            clauses: list[str] = []
            parameters: list[Any] = []
            if paths:
                clauses.append("relative_path IN (%s)" % ",".join("?" for _ in paths))
                parameters.extend(paths)
            if text.strip():
                clauses.append("(relative_path LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\')")
                escaped = text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                parameters.extend(["%%%s%%" % escaped, "%%%s%%" % escaped])
            if normalized_kinds:
                clauses.append("kind IN (%s)" % ",".join("?" for _ in normalized_kinds))
                parameters.extend(normalized_kinds)
            if digest:
                clauses.append("content_sha256=?")
                parameters.append(digest)
            if normalized_ids:
                clauses.append("asset_id IN (%s)" % ",".join("?" for _ in normalized_ids))
                parameters.extend(normalized_ids)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = db.execute(
                "SELECT " + _ASSET_COLUMNS + " FROM assets"
                + where
                + " ORDER BY kind, relative_path LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
            truncated = len(rows) > limit
            rows = rows[:limit]
            assets: list[dict[str, Any]] = []
            for row in rows:
                # Every delivered row is checked against its own digest.
                item = _row_record(row)
                if include_relationships:
                    relation_rows = db.execute(
                        "SELECT relation, target_asset_id, target_path, target_guid, evidence_kind, "
                        "confidence, verified, evidence_json FROM relationships WHERE source_asset_id=? "
                        "ORDER BY relation, target_path LIMIT ?",
                        (row["asset_id"], MAX_RELATIONSHIPS_PER_ASSET + 1),
                    ).fetchall()
                    item["relationships"] = []
                    for relation in relation_rows[:MAX_RELATIONSHIPS_PER_ASSET]:
                        evidence = json.loads(relation["evidence_json"])
                        item["relationships"].append(
                            {
                                "relation": relation["relation"],
                                "targetAssetId": relation["target_asset_id"],
                                "targetPath": relation["target_path"],
                                "targetGuid": relation["target_guid"],
                                "evidenceKind": relation["evidence_kind"],
                                "confidence": float(relation["confidence"]),
                                "verified": bool(relation["verified"]),
                                "derivationClass": evidence.get(
                                    "derivationClass", "observed"
                                ),
                                "resolutionStatus": evidence.get(
                                    "resolutionStatus", "unresolved"
                                ),
                                "evidence": evidence,
                            }
                        )
                    item["relationshipsTruncated"] = len(relation_rows) > MAX_RELATIONSHIPS_PER_ASSET
                assets.append(item)
            return {
                "schema": "renderdoc-ultra-unpacked-asset-query/v1",
                "schemaVersion": 1,
                "catalogSha256": metadata["catalogSha256"],
                "rootPath": metadata["rootPath"],
                "query": {
                    "text": text.strip(),
                    "relativePaths": list(paths),
                    "kinds": list(normalized_kinds),
                    "sha256": digest,
                    "assetIds": list(normalized_ids),
                    "limit": limit,
                },
                "count": len(assets),
                "truncated": truncated,
                "assets": assets,
                "policy": {
                    "readOnly": True,
                    "semanticGuessing": False,
                    "heuristicRelationshipsPersisted": False,
                    "unverifiedRelationsExplicit": True,
                },
            }
        finally:
            db.close()

    # -- scope: the directory-first way of finding things -------------------
    @staticmethod
    def _scope_bounds(prefixes: Sequence[str]) -> list[tuple[str, str]]:
        bounds: list[tuple[str, str]] = []
        for prefix in prefixes or ():
            text = str(prefix or "").replace("\\", "/").strip("/")
            if ".." in text.split("/") or len(text) > 4096:
                raise AssetCatalogError("scope prefixes must be relative catalog paths")
            folded = text.casefold()
            if folded:
                bounds.append((folded + "/", folded + "/\uffff"))
            else:
                bounds.append(("", "\uffff"))
        if len(bounds) > 64:
            raise AssetCatalogError("at most 64 scope prefixes per request")
        return bounds

    def directory_profile(self, *, depth: int = 3, prefix: str = "", kinds: Sequence[str] = (),
                          limit: int = 200) -> dict[str, Any]:
        """What lives where: file counts by kind and format per directory.

        Built from the names index alone, so it works before anything has been
        parsed. This is how a reader (or the AI) decides which folders are
        worth opening for a material, a texture set or a mesh -- by the shape
        of the tree, not by guessing names.
        """

        if isinstance(depth, bool) or not isinstance(depth, int) or not 1 <= depth <= 8:
            raise AssetCatalogError("depth must be between 1 and 8")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 2000:
            raise AssetCatalogError("limit must be between 1 and 2000")
        wanted = {str(item).strip().casefold() for item in kinds if str(item).strip()}
        if wanted - _KNOWN_KINDS:
            raise AssetCatalogError("profile contains an unknown asset kind")
        bounds = self._scope_bounds([prefix]) if prefix else []
        base_depth = len([part for part in str(prefix or "").replace("\\", "/").strip("/").split("/") if part])
        db, metadata = self._open_read_only()
        try:
            sql = "SELECT relative_path, kind, format, parse_status FROM assets"
            parameters: list[Any] = []
            if bounds:
                sql += " WHERE folded_path >= ? AND folded_path < ?"
                parameters.extend(bounds[0])
            folders: dict[str, dict[str, Any]] = {}
            total = 0
            for row in db.execute(sql, parameters):
                kind = str(row["kind"])
                if wanted and kind not in wanted:
                    continue
                total += 1
                parts = str(row["relative_path"]).split("/")[:-1]
                cut = min(len(parts), base_depth + depth)
                directory = "/".join(parts[:cut])
                entry = folders.get(directory)
                if entry is None:
                    entry = folders[directory] = {"directory": directory, "files": 0, "parsed": 0,
                                                  "byKind": {}, "byFormat": {}}
                entry["files"] += 1
                if str(row["parse_status"]) not in ("deferred",):
                    entry["parsed"] += 1
                entry["byKind"][kind] = entry["byKind"].get(kind, 0) + 1
                fmt = str(row["format"])
                entry["byFormat"][fmt] = entry["byFormat"].get(fmt, 0) + 1
            ranked = sorted(folders.values(), key=lambda item: (-item["files"], item["directory"]))
            for entry in ranked:
                entry["byFormat"] = dict(sorted(entry["byFormat"].items(), key=lambda kv: -kv[1])[:8])
                entry["byKind"] = dict(sorted(entry["byKind"].items(), key=lambda kv: -kv[1]))
            return {
                "schema": "renderdoc-ultra-unpacked-asset-directory-profile/v1",
                "schemaVersion": 1,
                "catalogSha256": metadata["catalogSha256"],
                "engine": metadata.get("engine", "unknown"),
                "prefix": str(prefix or ""),
                "depth": depth,
                "kinds": sorted(wanted),
                "totalFiles": total,
                "directoryCount": len(ranked),
                "directories": ranked[:limit],
                "truncated": len(ranked) > limit,
            }
        finally:
            db.close()

    def list_scope(self, prefixes: Sequence[str], *, kinds: Sequence[str] = (),
                   class_names: Sequence[str] = (), parsed_only: bool = False,
                   deferred_only: bool = False, limit: int = 400) -> list[dict[str, Any]]:
        """Assets under the given folders, optionally by kind / class / parse state."""

        bounds = self._scope_bounds(prefixes)
        if not bounds:
            return []
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
            raise AssetCatalogError("limit must be between 1 and 5000")
        wanted_kinds = {str(item).strip().casefold() for item in kinds if str(item).strip()}
        if wanted_kinds - _KNOWN_KINDS:
            raise AssetCatalogError("scope contains an unknown asset kind")
        wanted_classes = {str(item) for item in class_names if isinstance(item, str) and item}
        db, unused = self._open_read_only()
        try:
            found: list[dict[str, Any]] = []
            seen: set[str] = set()
            for low, high in bounds:
                clauses = ["folded_path >= ?", "folded_path < ?"]
                parameters: list[Any] = [low, high]
                if wanted_kinds:
                    clauses.append("kind IN (%s)" % ",".join("?" for _ in wanted_kinds))
                    parameters.extend(sorted(wanted_kinds))
                if wanted_classes:
                    clauses.append("class_name IN (%s)" % ",".join("?" for _ in wanted_classes))
                    parameters.extend(sorted(wanted_classes))
                if parsed_only:
                    clauses.append("parse_status = 'parsed'")
                if deferred_only:
                    clauses.append("parse_status = 'deferred'")
                for row in db.execute(
                    "SELECT asset_id, relative_path, kind, format, size, parse_status, hash_status, class_name "
                    "FROM assets WHERE " + " AND ".join(clauses) + " ORDER BY relative_path LIMIT ?",
                    (*parameters, limit - len(found) + 1),
                ):
                    if row["relative_path"] in seen:
                        continue
                    seen.add(row["relative_path"])
                    found.append({
                        "assetId": row["asset_id"], "relativePath": row["relative_path"],
                        "kind": row["kind"], "format": row["format"], "size": int(row["size"]),
                        "parseStatus": row["parse_status"], "hashStatus": row["hash_status"],
                        "className": row["class_name"],
                    })
                    if len(found) >= limit:
                        return found
            return found
        finally:
            db.close()

    def hydrate_scope(self, asset_root: str | Path, prefixes: Sequence[str], *,
                      kinds: Sequence[str] = (), max_files: int = 200) -> dict[str, Any]:
        """Parse the descriptive files under a few folders -- and nothing else.

        Package headers first (they say what class each asset is); then the
        cooked exports of materials and textures, so locally decodable values
        and thumbnails become searchable. Unsupported images request a reader
        preview explicitly. Bulk data is never touched by this parser.
        """

        if isinstance(max_files, bool) or not isinstance(max_files, int) or not 1 <= max_files <= 2000:
            raise AssetCatalogError("max_files must be between 1 and 2000")
        wanted_kinds = tuple(kinds) if kinds else ("package", "material", "metadata", "shader", "texture", "mesh")
        deferred = [item for item in self.list_scope(prefixes, kinds=wanted_kinds, deferred_only=True,
                                                     limit=min(5000, max_files * 4))
                    if PurePosixPath(str(item["relativePath"])).suffix.casefold() not in _BULK_SUFFIXES
                    and PurePosixPath(str(item["relativePath"])).suffix.casefold() != ".uexp"]
        headers = [str(item["relativePath"]) for item in deferred][:max_files]
        hydrated: list[str] = []
        missing: list[str] = []
        for start in range(0, len(headers), 64):
            receipt = self.hydrate(asset_root, headers[start:start + 64])
            hydrated.extend(receipt.get("hydrated") or [])
            missing.extend(receipt.get("missing") or [])
        # Second pass includes texture payloads. Hydrating only their uasset
        # headers leaves the very pixels the user selected unsearchable.
        owners = self.list_scope(prefixes, kinds=("package",),
                                class_names=sorted(_MATERIAL_CLASS_NAMES | _TEXTURE_CLASS_NAMES),
                                parsed_only=True, limit=max_files)
        exports = []
        texture_exports = []
        for item in owners:
            relative = str(item["relativePath"])
            if relative.casefold().endswith((".uasset", ".umap")):
                exports.append(PurePosixPath(relative).with_suffix(".uexp").as_posix())
                if item.get("className") in _TEXTURE_CLASS_NAMES:
                    texture_exports.append(exports[-1])
        pending = [path for path in exports if path not in hydrated]
        if pending:
            already = {str(item["relativePath"]) for item in self.list_scope(
                prefixes, kinds=("package",), parsed_only=True, limit=min(5000, max_files * 4))}
            pending = [path for path in pending if path not in already][:max_files]
            for start in range(0, len(pending), 64):
                receipt = self.hydrate(asset_root, pending[start:start + 64])
                hydrated.extend(receipt.get("hydrated") or [])
                missing.extend(receipt.get("missing") or [])
        texture_evidence = []
        for start in range(0, len(texture_exports), MAX_QUERY_IDS):
            found = self.query(relative_paths=texture_exports[start:start + MAX_QUERY_IDS],
                               include_relationships=False, limit=MAX_QUERY_IDS)
            for item in found["assets"]:
                fp = (item.get("metadata") or {}).get("textureFingerprint") or {}
                texture_evidence.append({"relativePath": item["relativePath"], "available": fp.get("available") is True,
                                         "reason": fp.get("reason") or ("" if fp.get("available") else "pixel payload not decoded")})
        return {
            "schema": "renderdoc-ultra-unpacked-asset-hydrate-scope/v1",
            "schemaVersion": 1,
            "prefixes": [str(item) for item in prefixes],
            "kinds": list(wanted_kinds),
            "candidates": len(deferred),
            "hydrated": hydrated,
            "missing": missing,
            "materialExports": len(exports) - len(texture_exports),
            "textureExports": len(texture_exports),
            "textureEvidence": texture_evidence[:64],
            "textureEvidenceTruncated": len(texture_evidence) > 64,
            "truncated": len(deferred) > max_files,
        }

    def coverage(self) -> dict[str, Any]:
        """What the catalog could and could not read, with reasons.

        Tells a reviewer why evidence may be missing: packed archives that
        were never unpacked, compressed shader archives whose binaries cannot
        be indexed, packages that failed to parse (encrypted or foreign
        formats), and files deferred by the scan depth.
        """

        db, metadata = self._open_read_only()
        try:
            by_status: dict[str, dict[str, int]] = {}
            for row in db.execute("SELECT kind, parse_status, COUNT(*) AS n FROM assets GROUP BY kind, parse_status"):
                by_status.setdefault(str(row["kind"]), {})[str(row["parse_status"])] = int(row["n"])
            reasons: dict[str, int] = {}
            examples: dict[str, str] = {}
            for row in db.execute(
                "SELECT relative_path, metadata_json FROM assets WHERE parse_status IN ('parse-error') "
                "OR metadata_json LIKE '%\"parseStatus\":\"failed\"%' LIMIT 2000"
            ):
                stored = json.loads(row["metadata_json"])
                reason = str(stored.get("reason") or stored.get("errorType") or "unknown")[:160]
                reasons[reason] = reasons.get(reason, 0) + 1
                examples.setdefault(reason, str(row["relative_path"]))
            archives = {
                str(row["format"]): int(row["n"])
                for row in db.execute("SELECT format, COUNT(*) AS n FROM assets WHERE kind='archive' GROUP BY format")
            }
            deferred = int(db.execute("SELECT COUNT(*) AS n FROM assets WHERE parse_status='deferred'").fetchone()["n"])
            try:
                engine = json.loads(metadata.get("engineJson") or "{}")
            except ValueError:
                engine = {}
            limitations: list[str] = []
            details = engine.get("details") or {}
            if archives:
                limitations.append(
                    "%d packed archive file(s) (%s) are still packed; assets inside them are invisible until unpacked"
                    % (sum(archives.values()), ", ".join(sorted(archives))))
            if int(details.get("shaderArchiveCount") or 0):
                limitations.append(
                    "%d Unreal .ushaderbytecode archive(s) present: shader binaries are compressed and not indexable, "
                    "so hash identity is unavailable; retrieval relies on names, relations and cbuffer values"
                    % int(details["shaderArchiveCount"]))
            if reasons:
                top = sorted(reasons.items(), key=lambda item: -item[1])[:5]
                limitations.append(
                    "%d file(s) failed to parse: %s" % (
                        sum(reasons.values()),
                        "; ".join("%s (x%d, e.g. %s)" % (reason, count, examples[reason]) for reason, count in top)))
            if deferred:
                limitations.append(
                    "%d file(s) deferred by scan depth (bulk/large data not hashed or parsed); candidates are hydrated on demand"
                    % deferred)
            if engine.get("engine") in (None, "", "unknown"):
                limitations.append("engine not recognized from file names: only path/name/text tokens are available")
            unfingerprinted: dict[str, int] = {}
            for row in db.execute(
                "SELECT metadata_json FROM assets WHERE metadata_json LIKE '%\"textureFingerprint\"%' "
                "AND metadata_json LIKE '%\"available\":false%' LIMIT 5000"
            ):
                stored = json.loads(row["metadata_json"])
                reason = str(((stored.get("textureFingerprint") or {}).get("reason")) or "unknown")[:120]
                unfingerprinted[reason] = unfingerprinted.get(reason, 0) + 1
            if unfingerprinted:
                top = sorted(unfingerprinted.items(), key=lambda item: -item[1])[:4]
                limitations.append("%d texture(s) have no content fingerprint: %s" % (
                    sum(unfingerprinted.values()), "; ".join("%s (x%d)" % item for item in top)))
            return {
                "schema": "renderdoc-ultra-unpacked-asset-coverage/v1",
                "schemaVersion": 1,
                "catalogSha256": metadata["catalogSha256"],
                "engine": engine,
                "parserRevision": metadata.get("parserRevision"),
                "tokenIndex": metadata.get("tokenIndex", "none"),
                "valueIndexRows": int(db.execute("SELECT COUNT(*) AS n FROM asset_values").fetchone()["n"]),
                "thumbnailRows": int(db.execute("SELECT COUNT(*) AS n FROM asset_thumbnails").fetchone()["n"]),
                "indexCoverage": "complete" if metadata.get("complete") == "true" else "partial",
                "byKindAndStatus": by_status,
                "parseFailureReasons": dict(sorted(reasons.items(), key=lambda item: -item[1])[:20]),
                "archives": archives,
                "deferredCount": deferred,
                "limitations": limitations,
            }
        finally:
            db.close()

    def search_thumbnails(self, dhash: int, *, mean_color: Sequence[int] | None = None,
                          max_distance: int = 12, limit: int = 20) -> dict[str, Any]:
        """Texture assets whose thumbnail hash is within ``max_distance`` bits.

        The query hash is compared with every stored dihedral variant, so a
        mirrored/rotated copy of the same image matches; flat (uniform)
        images are excluded because they match everything.
        """

        from .symbols.imaging import hamming

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_QUERY_RESULTS:
            raise AssetCatalogError("limit is outside the fixed query bound")
        query = int(dhash) & 0xFFFFFFFFFFFFFFFF
        names = ("identity", "mirror-x", "rotate-90", "rotate-90+mirror", "rotate-180", "mirror-y", "rotate-270", "rotate-270+mirror")
        db, metadata = self._open_read_only()
        try:
            # Partial scans and bounded hydration already populate this table.
            # The final-scan metadata counter is not an availability gate.
            indexed_count = int(db.execute("SELECT COUNT(*) AS n FROM asset_thumbnails").fetchone()["n"])
            matches = []
            for row in db.execute("SELECT * FROM asset_thumbnails WHERE flat=0"):
                best = None
                for index in range(8):
                    stored = int(row["v%d" % index]) & 0xFFFFFFFFFFFFFFFF
                    distance = hamming(query, stored)
                    if best is None or distance < best[0]:
                        best = (distance, index)
                if best is not None and best[0] <= max_distance:
                    color_delta = None
                    if mean_color is not None and len(mean_color) >= 3:
                        color_delta = int(abs(int(mean_color[0]) - int(row["mean_r"])) + abs(int(mean_color[1]) - int(row["mean_g"]))
                                          + abs(int(mean_color[2]) - int(row["mean_b"])))
                    matches.append((best[0], color_delta if color_delta is not None else 0, row["asset_id"], names[best[1]],
                                    color_delta, int(row["width"]), int(row["height"]), str(row["pixel_format"] or "")))
            matches.sort(key=lambda item: (item[0], item[1], item[2]))
            candidates, unavailable = [], []
            for distance, _, asset_id, transform, color_delta, width, height, pixel_format in matches:
                asset = db.execute("SELECT " + _ASSET_COLUMNS + " FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
                if asset is None:
                    continue
                record = _row_record(asset)
                binding = (record["metadata"].get("textureFingerprint") or {}).get("previewProvenance")
                if binding:
                    try:
                        _preview_input_identity(Path(metadata["rootPath"]), binding.get("sourceFiles"))
                        _preview_bytes(Path(binding["previewPath"]), binding["previewSha256"])
                    except (AssetCatalogError, OSError, ValueError, TypeError, KeyError) as error:
                        unavailable.append({"relativePath": asset["relative_path"], "reason": str(error)[:200]})
                        continue
                # Luminance hashes cannot tell channel-swapped variants apart;
                # a large mean-colour difference demotes an exact hash hit.
                verdict = "same-image" if distance <= 6 else "plausible"
                if verdict == "same-image" and color_delta is not None and color_delta > 96:
                    verdict = "plausible"
                candidates.append({
                    "assetId": asset_id, "relativePath": asset["relative_path"], "kind": asset["kind"],
                    "contentSha256": asset["content_sha256"], "distance": distance, "transform": transform,
                    "meanColorDelta": color_delta, "sizeX": width, "sizeY": height, "pixelFormat": pixel_format,
                    "verdict": verdict,
                    "physicalIdentityVerified": False,
                    "evidenceKind": "decoded-reader-preview" if binding else "decoded-asset-thumbnail",
                })
                if len(candidates) >= limit:
                    break
            return {"schema": "renderdoc-ultra-unpacked-asset-thumbnail-search/v1", "schemaVersion": 1,
                    "catalogSha256": metadata["catalogSha256"], "queryHash": query, "maxDistance": max_distance,
                    "indexedThumbnails": indexed_count, "candidates": candidates,
                    "indexCoverage": "complete" if metadata.get("complete") == "true" else "partial",
                    "unavailableEvidence": unavailable[:32],
                    "absenceProven": False}
        finally:
            db.close()

    def search_values(self, values: Sequence[float | str], *, limit: int = 50) -> dict[str, Any]:
        """Assets containing the given distinctive numeric values.

        ``values`` are floats (rounded to float32 keys) or ready-made keys.
        Returns, per asset, which keys matched plus the asset's numeric runs
        so a caller can test adjacency against neighbouring cbuffer lanes.
        """

        from .symbols.engines.unreal_package import is_distinctive_value, value_key

        keys: list[str] = []
        for item in values:
            if isinstance(item, str):
                key = item.strip()
            elif isinstance(item, (int, float)) and not isinstance(item, bool):
                value = struct.unpack("<f", struct.pack("<f", float(item)))[0]
                if not is_distinctive_value(value):
                    continue
                key = value_key(value)
            else:
                continue
            if key and len(key) <= 24 and key not in keys:
                keys.append(key)
        if len(keys) > 4096:
            keys = keys[:4096]
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_QUERY_RESULTS:
            raise AssetCatalogError("limit is outside the fixed query bound")
        db, metadata = self._open_read_only()
        try:
            if not keys:
                return {"schema": "renderdoc-ultra-unpacked-asset-value-search/v1", "schemaVersion": 1,
                        "catalogSha256": metadata["catalogSha256"], "queryKeys": [], "candidates": [],
                        "reason": "no distinctive values in the query"}
            hits: dict[str, list[str]] = {}
            popularity: dict[str, int] = {}
            for start in range(0, len(keys), 500):
                chunk = keys[start:start + 500]
                rows = db.execute(
                    "SELECT value_key, asset_id FROM asset_values WHERE value_key IN (%s)" % ",".join("?" for _ in chunk),
                    chunk,
                ).fetchall()
                for row in rows:
                    popularity[str(row["value_key"])] = popularity.get(str(row["value_key"]), 0) + 1
                    hits.setdefault(str(row["asset_id"]), []).append(str(row["value_key"]))
            ranked = sorted(hits.items(), key=lambda item: (-len(set(item[1])), item[0]))
            candidates = []
            for asset_id, matched in ranked[:limit]:
                row = db.execute(
                    "SELECT asset_id, relative_path, kind, content_sha256, metadata_json FROM assets WHERE asset_id=?",
                    (asset_id,),
                ).fetchone()
                if row is None:
                    continue
                stored = json.loads(row["metadata_json"])
                unique = sorted(set(matched))
                candidates.append({
                    "assetId": row["asset_id"],
                    "relativePath": row["relative_path"],
                    "kind": row["kind"],
                    "contentSha256": row["content_sha256"],
                    "className": stored.get("className", ""),
                    "matchedValues": unique,
                    "matchedCount": len(unique),
                    # values shared by many assets (engine defaults) count less
                    "rareMatchedCount": sum(1 for key in unique if popularity.get(key, 0) <= 3),
                    "numericRuns": [run for run in (stored.get("numericRuns") or [])
                                    if any(key in unique for key in run.get("values", []))][:64],
                })
            return {
                "schema": "renderdoc-ultra-unpacked-asset-value-search/v1",
                "schemaVersion": 1,
                "catalogSha256": metadata["catalogSha256"],
                "queryKeys": keys,
                "keyPopularity": popularity,
                "candidates": candidates,
                "indexCoverage": "complete" if metadata.get("complete") == "true" else "partial",
                "absenceProven": False,
            }
        finally:
            db.close()

    def referrers(self, asset_id: str, *, relation: str = "", limit: int = 50) -> dict[str, Any]:
        """Assets whose relationships point at ``asset_id`` (reverse edges)."""

        identity = str(asset_id or "").strip()
        if not _ASSET_ID_RE.fullmatch(identity):
            raise AssetCatalogError("asset_id is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_QUERY_RESULTS:
            raise AssetCatalogError("limit is outside the fixed query bound")
        db, metadata = self._open_read_only()
        try:
            clauses = ["r.target_asset_id=?"]
            parameters: list[Any] = [identity]
            if relation:
                clauses.append("r.relation=?")
                parameters.append(str(relation)[:128])
            rows = db.execute(
                "SELECT r.relation, r.verified, a.asset_id, a.relative_path, a.kind, a.metadata_json "
                "FROM relationships r JOIN assets a ON a.asset_id = r.source_asset_id "
                "WHERE " + " AND ".join(clauses) + " ORDER BY a.relative_path LIMIT ?",
                (*parameters, limit + 1),
            ).fetchall()
            items = []
            for row in rows[:limit]:
                stored = json.loads(row["metadata_json"])
                items.append({
                    "assetId": row["asset_id"], "relativePath": row["relative_path"], "kind": row["kind"],
                    "relation": row["relation"], "verified": bool(row["verified"]),
                    "className": stored.get("className", ""),
                })
            return {"schema": "renderdoc-ultra-unpacked-asset-referrers/v1", "schemaVersion": 1,
                    "catalogSha256": metadata["catalogSha256"], "assetId": identity,
                    "referrers": items, "truncated": len(rows) > limit}
        finally:
            db.close()

    def expand_reference_neighborhood(self, asset_ids: Sequence[str], *,
                                      max_depth: int = 4, max_assets: int = 64) -> dict[str, Any]:
        """Follow exact indexed dependencies around a few content search anchors.

        This is an index traversal, never a filesystem walk or an identity
        assertion. Reader adapters supply the typed edges; names and arbitrary
        string mentions cannot create them. Related textures are returned as
        leaves so a shared utility texture cannot bridge unrelated materials.
        """
        ids = list(dict.fromkeys(str(item) for item in asset_ids))
        if (not 1 <= len(ids) <= 8 or any(not _ASSET_ID_RE.fullmatch(item) for item in ids)
                or type(max_depth) is not int or not 1 <= max_depth <= 4
                or type(max_assets) is not int or not len(ids) <= max_assets <= 64):
            raise AssetCatalogError("reference neighborhood exceeds its fixed search bounds")
        relations = {
            "uses-texture", "uses-base-color-texture", "uses-texture-parameter",
            "parent-material", "uses-material", "uses-material-function",
            "uses-material-library", "uses-material-record", "uses-raw-material-record",
            "uses-shader", "references-shader", "includes-shader-source",
            "references-unity-guid", "references-mesh", "contains-lod-geometry",
            "contains-uemodel", "cooked-export-of",
        }
        db, metadata = self._open_read_only()
        try:
            assets, paths, queue, expanded = {}, {}, [], set()
            missing, truncated, unresolved = [], False, []

            def read(identity):
                if identity not in assets:
                    row = db.execute("SELECT " + _ASSET_COLUMNS + " FROM assets WHERE asset_id=?", (identity,)).fetchone()
                    if row is None:
                        return None
                    assets[identity] = _row_record(row)
                return assets[identity]

            for identity in ids:
                if read(identity) is None:
                    missing.append(identity)
                    continue
                paths[identity] = [{"anchorAssetId": identity, "steps": []}]
                queue.append((identity, identity, []))
            index = 0
            while index < len(queue):
                identity, anchor, steps = queue[index]
                index += 1
                if (identity, anchor) in expanded or len(steps) >= max_depth:
                    continue
                expanded.add((identity, anchor))
                asset = assets[identity]
                if identity not in ids and (asset["kind"] == "texture" or
                        str(asset["metadata"].get("className", "")).startswith("Texture")):
                    continue
                neighbors = []
                for direction, column, other in (("dependency", "source_asset_id", "target_asset_id"),
                                                   ("referrer", "target_asset_id", "source_asset_id")):
                    rows = db.execute(
                        "SELECT source_asset_id, target_asset_id, relation, verified, evidence_kind, "
                        "target_path FROM relationships WHERE " + column + "=? AND relation IN (" +
                        ",".join("?" for unused in relations) + ") ORDER BY relation, " + other + " LIMIT 33",
                        (identity, *sorted(relations))).fetchall()
                    truncated = truncated or len(rows) > 32
                    for row in rows[:32]:
                        target = row[other]
                        if not target or not row["verified"]:
                            if direction == "dependency" and len(unresolved) < 24:
                                unresolved.append({"sourcePath": asset["relativePath"], "relation": row["relation"],
                                                   "targetPath": row["target_path"], "status": "unresolved-index-edge"})
                            continue
                        neighbors.append((target, row["relation"], direction, row["evidence_kind"]))
                # Unity GUID references target .meta records. Connect only the
                # exact adjacent owner/sidecar paths already present in the index.
                relative = asset["relativePath"]
                sidecar_path = relative[:-5] if relative.endswith(".meta") else relative + ".meta"
                sidecar = db.execute("SELECT asset_id, declared_guid FROM assets WHERE relative_path=?",
                                     (sidecar_path,)).fetchone()
                if sidecar is not None and (sidecar["declared_guid"] or asset["metadata"].get("declaredGuid")):
                    neighbors.append((sidecar["asset_id"], "unity-guid-sidecar", "sidecar", "exact-adjacent-path"))
                for target, relation, direction, evidence_kind in neighbors:
                    if target == anchor or any(step["fromAssetId"] == target for step in steps):
                        continue
                    if target not in assets and len(assets) >= max_assets:
                        truncated = True
                        continue
                    target_asset = read(target)
                    if target_asset is None:
                        continue
                    if any(path["anchorAssetId"] == anchor for path in paths.get(target, [])):
                        continue
                    extended = steps + [{"fromAssetId": identity, "toAssetId": target,
                                         "fromPath": relative, "toPath": target_asset["relativePath"],
                                         "relation": relation, "direction": direction,
                                         "evidenceKind": evidence_kind, "catalogReferenceVerified": True}]
                    paths.setdefault(target, []).append({"anchorAssetId": anchor, "steps": extended})
                    queue.append((target, anchor, extended))
            return {"schema": "bread-asset-reference-neighborhood/v1", "schemaVersion": 1,
                    "catalogSha256": metadata["catalogSha256"], "anchorAssetIds": ids,
                    "assets": [{**asset, "anchorPaths": paths.get(identity, [])}
                               for identity, asset in assets.items()],
                    "missingAnchors": missing, "unresolvedEdges": unresolved, "truncated": truncated,
                    "bounds": {"maxDepth": max_depth, "maxAssets": max_assets, "edgesPerDirection": 32},
                    "physicalIdentityVerified": False, "filesystemScanned": False,
                    "note": "exact catalog relationships expand search leads; they do not identify the captured material"}
        finally:
            db.close()

    def search_tokens(
        self,
        tokens: Sequence[str],
        *,
        kinds: Sequence[str] = (),
        class_names: Sequence[str] = (),
        limit: int = 20,
    ) -> dict[str, Any]:
        """Rank assets by overlap with a bag of query tokens (no hashes needed).

        Each matching token contributes an IDF-style weight (rarer tokens in
        this catalog count more), so a distinctive texture name outweighs a
        generic word such as ``base`` or ``color``.  The per-asset result lists
        the matched tokens so a reviewer can see *why* it ranked.
        """

        query_tokens: list[str] = []
        for token in tokens:
            if not isinstance(token, str):
                continue
            for word in _TOKEN_SPLIT.split(token):
                folded = word.casefold()
                if 2 <= len(folded) <= 64 and folded not in query_tokens:
                    query_tokens.append(folded)
        if not query_tokens or len(query_tokens) > 256:
            raise AssetCatalogError("token search needs 1 to 256 tokens")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_QUERY_RESULTS:
            raise AssetCatalogError("limit is outside the fixed query bound")
        normalized_kinds = {str(item).strip().casefold() for item in kinds}
        if normalized_kinds - _KNOWN_KINDS:
            raise AssetCatalogError("token search contains an unknown asset kind")
        wanted_classes = {str(item) for item in class_names if isinstance(item, str) and item}
        db, metadata = self._open_read_only()
        try:
            if metadata.get("tokenIndex") not in {"fts5", "plain"}:
                raise AssetCatalogError("asset catalog was indexed without retrieval tokens; rescan it")
            total = max(1, int(db.execute("SELECT COUNT(*) AS n FROM asset_tokens").fetchone()["n"]))
            weights: dict[str, float] = {}
            hits: dict[str, dict[str, Any]] = {}
            for token in query_tokens:
                if metadata.get("tokenIndex") == "fts5":
                    rows = db.execute(
                        "SELECT asset_id FROM asset_tokens WHERE asset_tokens MATCH ? LIMIT 20000",
                        ('"%s"' % token.replace('"', '""'),),
                    ).fetchall()
                else:
                    rows = db.execute(
                        "SELECT asset_id FROM asset_tokens WHERE ' ' || tokens || ' ' LIKE ? LIMIT 20000",
                        ("%% %s %%" % token,),
                    ).fetchall()
                if not rows:
                    continue
                weight = math.log(1.0 + total / (1.0 + len(rows)))
                weights[token] = weight
                for row in rows:
                    entry = hits.setdefault(str(row["asset_id"]), {"score": 0.0, "matched": []})
                    entry["score"] += weight
                    entry["matched"].append(token)
            if not hits:
                return {"schema": "renderdoc-ultra-unpacked-asset-token-search/v1", "schemaVersion": 1,
                        "catalogSha256": metadata["catalogSha256"], "queryTokens": query_tokens,
                        "tokenWeights": {}, "candidates": []}
            ordered = sorted(hits.items(), key=lambda item: (-item[1]["score"], item[0]))
            candidates = []
            for asset_id, entry in ordered:
                row = db.execute(
                    "SELECT asset_id, relative_path, kind, format, size, content_sha256, parse_status, "
                    "metadata_json FROM assets WHERE asset_id=?", (asset_id,)
                ).fetchone()
                if row is None:
                    continue
                if normalized_kinds and str(row["kind"]) not in normalized_kinds:
                    continue
                stored = json.loads(row["metadata_json"])
                if wanted_classes and str(stored.get("className", "")) not in wanted_classes:
                    continue
                candidates.append({
                    "assetId": row["asset_id"],
                    "relativePath": row["relative_path"],
                    "kind": row["kind"],
                    "format": row["format"],
                    "size": int(row["size"]),
                    "contentSha256": row["content_sha256"],
                    "parseStatus": row["parse_status"],
                    "className": stored.get("className", ""),
                    "engine": stored.get("engine", ""),
                    "score": round(entry["score"], 4),
                    "matchedTokens": sorted(set(entry["matched"])),
                    "matchedCount": len(set(entry["matched"])),
                    "queryCoverage": round(len(set(entry["matched"])) / len(query_tokens), 4),
                })
                if len(candidates) >= limit:
                    break
            return {
                "schema": "renderdoc-ultra-unpacked-asset-token-search/v1",
                "schemaVersion": 1,
                "catalogSha256": metadata["catalogSha256"],
                "engine": metadata.get("engine", "unknown"),
                "queryTokens": query_tokens,
                "tokenWeights": {token: round(weight, 4) for token, weight in weights.items()},
                "candidates": candidates,
            }
        finally:
            db.close()

    def resolve(
        self,
        asset_root: str | Path,
        *,
        asset_id: str,
        expected_catalog_sha256: str,
    ) -> dict[str, Any]:
        root = _validate_root(asset_root)
        identity = str(asset_id or "").strip()
        if not _ASSET_ID_RE.fullmatch(identity):
            raise AssetCatalogError("asset_id is invalid")
        expected = str(expected_catalog_sha256 or "").strip().lower()
        if not _SHA256_RE.fullmatch(expected):
            raise AssetCatalogError("expected_catalog_sha256 is invalid")
        db, metadata = self._open_read_only()
        try:
            if metadata["catalogSha256"] != expected:
                raise AssetCatalogError("asset catalog changed after candidate selection")
            if Path(metadata["rootPath"]).resolve() != root:
                raise AssetCatalogError("asset catalog belongs to another configured root")
            row = db.execute(
                "SELECT " + _ASSET_COLUMNS + " FROM assets WHERE asset_id=?",
                (identity,),
            ).fetchone()
            if row is None:
                raise AssetCatalogError("asset identity is not present in the current catalog")
            stored = _row_record(row)
            relative = PurePosixPath(str(row["relative_path"]))
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                raise AssetCatalogError("stored asset path is unsafe")
            cursor = root
            for part in relative.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise AssetCatalogError("asset delivery path traverses a symbolic link")
            path = cursor.resolve(strict=True)
            if not _within(root, path) or not path.is_file():
                raise AssetCatalogError("asset delivery path escaped or disappeared")
            stat_value = path.stat()
            if int(stat_value.st_size) != int(row["size"]):
                raise AssetCatalogError("asset size changed after indexing")
            content_sha256 = str(row["content_sha256"] or "")
            if not content_sha256:
                raise AssetCatalogError("asset was not content-hashed and cannot be delivered as verified evidence")
            actual_sha256 = _hash_regular_file(path, int(row["size"]))
            if actual_sha256 != content_sha256:
                raise AssetCatalogError("asset content changed after indexing")
            return {
                "schema": "renderdoc-ultra-unpacked-asset-delivery/v1",
                "schemaVersion": 1,
                "catalogSha256": expected,
                "assetId": identity,
                "relativePath": relative.as_posix(),
                "absolutePath": str(path),
                "kind": row["kind"],
                "format": row["format"],
                "size": int(row["size"]),
                "contentSha256": actual_sha256,
                "metadata": stored["metadata"],
                "verified": True,
                "policy": {
                    "configuredActiveProjectRootOnly": True,
                    "catalogRevisionBound": True,
                    "contentRehashedAtDelivery": True,
                    "executionAuthorized": False,
                },
            }
        finally:
            db.close()


__all__ = [
    "AssetCatalogError",
    "AssetCatalogScanLimitError",
    "AssetCatalogService",
    "CATALOG_FILENAME",
    "CATALOG_SCHEMA",
    "CATALOG_VERSION",
    "DEFAULT_MAX_FILES",
    "DEFAULT_NAMES_ONLY_MAX_FILES",
    "DEFAULT_MAX_FILE_SIZE_MB",
    "HARD_MAX_FILES",
    "HARD_NAMES_ONLY_MAX_FILES",
    "HARD_MAX_FILE_SIZE_MB",
]
