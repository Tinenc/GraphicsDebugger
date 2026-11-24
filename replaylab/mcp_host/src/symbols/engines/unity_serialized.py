"""Header-level reader for Unity serialized files and asset bundles.

Enough to answer "what is this?" without a full type-tree walk: the serialized
file header gives the format version, the Unity version string and the object
table, and each object's class id names the asset type. Bundles are recognised
by their signature and reported as containers.

This is deliberately shallow. Extracting Unity *content* (pixels, meshes) is the
job of a dedicated extractor registered as an asset reader; classification is
what the search and matching chain needs from the Host itself.
"""

from __future__ import annotations

import struct
from typing import Any


# Unity class ids that matter for capture-to-asset matching.
CLASS_KINDS: dict[int, tuple[str, str]] = {
    1: ("other", "GameObject"),
    4: ("other", "Transform"),
    21: ("material", "Material"),
    23: ("other", "MeshRenderer"),
    25: ("other", "Renderer"),
    28: ("texture", "Texture2D"),
    33: ("other", "MeshFilter"),
    43: ("mesh", "Mesh"),
    48: ("shader", "Shader"),
    74: ("animation", "AnimationClip"),
    83: ("audio", "AudioClip"),
    89: ("texture", "Cubemap"),
    91: ("animation", "AnimatorController"),
    114: ("other", "MonoBehaviour"),
    115: ("other", "MonoScript"),
    117: ("texture", "Texture3D"),
    137: ("mesh", "SkinnedMeshRenderer"),
    142: ("package", "AssetBundle"),
    187: ("texture", "Texture2DArray"),
    213: ("texture", "Sprite"),
}

BUNDLE_SIGNATURES = (b"UnityFS", b"UnityWeb", b"UnityRaw", b"UnityArchive")
MAX_OBJECTS = 4096


class UnitySerializedError(ValueError):
    """The bytes are not a Unity serialized file this reader understands."""


def _cstring(data: bytes, offset: int, limit: int = 256) -> tuple[str, int]:
    end = data.find(b"\x00", offset, offset + limit)
    if end < 0:
        raise UnitySerializedError("unterminated string")
    return data[offset:end].decode("utf-8", "replace"), end + 1


def parse_bundle(data: bytes) -> dict[str, Any] | None:
    """Recognise an asset bundle container and read its declared version."""

    for signature in BUNDLE_SIGNATURES:
        if not data.startswith(signature):
            continue
        try:
            name, offset = _cstring(data, 0)
            version = struct.unpack_from(">I", data, offset)[0]
            player, offset = _cstring(data, offset + 4)
            engine, unused = _cstring(data, offset)
        except (UnitySerializedError, struct.error):
            player = engine = ""
            version = 0
        return {
            "engine": "unity",
            "reader": "unity-bundle",
            "kind": "package",
            "container": signature.decode("ascii"),
            "formatVersion": int(version),
            "unityVersion": engine or player,
            "note": ("an asset bundle holds many objects; register an extractor to read its "
                     "contents, or point the asset root at an already-extracted tree"),
        }
    return None


def parse_serialized(data: bytes) -> dict[str, Any]:
    """Read a SerializedFile header and its object class ids."""

    if len(data) < 32:
        raise UnitySerializedError("file is too small to be a serialized file")
    metadata_size, file_size, version, data_offset = struct.unpack_from(">IIII", data, 0)
    if not 6 <= version <= 30:
        raise UnitySerializedError("unsupported serialized file version %d" % version)
    offset = 16
    if version >= 9:
        endianness = data[offset]
        offset += 4
    else:
        # Pre-v9 metadata lives at the end of the file and starts with its
        # endian flag. A prefix-only read cannot claim to have classified it.
        offset = file_size - metadata_size
        if offset < 16 or offset >= len(data):
            raise UnitySerializedError("legacy serialized metadata is unavailable in this read")
        endianness = data[offset]
        offset += 1
    if endianness not in (0, 1):
        raise UnitySerializedError("invalid serialized metadata endian flag")
    if version >= 22:
        # Large-file header: sizes move to 64-bit after the legacy fields.
        # Header integers remain big-endian; the flag applies only to metadata.
        metadata_size, file_size, data_offset = struct.unpack_from(">IQQ", data, offset)[0:3]
        offset += 4 + 8 + 8 + 8
    order = ">" if endianness else "<"
    unity_version = ""
    if version >= 7:
        unity_version, offset = _cstring(data, offset)
    if len(data) < offset + 8:
        raise UnitySerializedError("truncated serialized file header")
    target_platform = 0
    if version >= 8:
        target_platform = struct.unpack_from(order + "i", data, offset)[0]
        offset += 4
    has_type_tree = True
    if version >= 13:
        has_type_tree = bool(data[offset])
        offset += 1
    type_count = struct.unpack_from(order + "i", data, offset)[0]
    offset += 4
    if not 0 <= type_count <= 65536:
        raise UnitySerializedError("implausible type count")
    class_ids: list[int] = []
    for _ in range(min(type_count, MAX_OBJECTS)):
        if len(data) < offset + 4:
            break
        class_id = struct.unpack_from(order + "i", data, offset)[0]
        class_ids.append(int(class_id))
        offset += 4
        if version >= 16:
            offset += 1                           # stripped
        if version >= 17:
            offset += 2                           # script type index
        if version >= 13:
            if (version < 16 and class_id < 0) or (version >= 16 and class_id == 114):
                offset += 16                      # script hash
            offset += 16                          # type hash
        if offset > len(data):
            class_ids.pop()
            break
        if has_type_tree:
            # A full type tree needs the node walk; classification does not.
            break
    kinds: dict[str, int] = {}
    classes: list[str] = []
    for class_id in class_ids:
        kind, name = CLASS_KINDS.get(class_id, ("other", "Class%d" % class_id))
        kinds[kind] = kinds.get(kind, 0) + 1
        if name not in classes:
            classes.append(name)
    primary = "other"
    for candidate in ("texture", "mesh", "material", "shader", "animation", "audio"):
        if kinds.get(candidate):
            primary = candidate
            break
    return {
        "engine": "unity",
        "reader": "unity-serialized",
        "kind": primary,
        "className": classes[0] if classes else "",
        "unityVersion": unity_version,
        "targetPlatform": int(target_platform),
        "formatVersion": int(version),
        "objectClasses": classes[:16],
        "objectKindCounts": kinds,
        "classificationScope": "serialized-types",
        "classificationComplete": len(class_ids) == type_count,
        "typeTreeStripped": not has_type_tree,
        "declaredFileSize": int(file_size),
        "dataOffset": int(data_offset),
        "metadataSize": int(metadata_size),
    }


def parse(data: bytes) -> dict[str, Any] | None:
    """Bundle or serialized file, or None when neither."""

    bundle = parse_bundle(data)
    if bundle is not None:
        return bundle
    try:
        return parse_serialized(data)
    except (UnitySerializedError, struct.error, UnicodeError, IndexError):
        return None


__all__ = ["CLASS_KINDS", "UnitySerializedError", "parse", "parse_bundle", "parse_serialized"]
