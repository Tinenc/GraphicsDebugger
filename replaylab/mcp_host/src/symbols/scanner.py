"""Asset directory inventory and deterministic shader extraction."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import struct
import time
from typing import Any, Callable, Iterable

from .dxbc import ShaderContainer, fingerprint_bytes, scan_file
from .providers import (
    ProviderIntegrityError,
    TrustedProviderResult,
    verify_attestation,
)
from .store import SymbolStore


SYMBOL_SCHEMA = "renderdoc-ultra-symbols/v1"
SKIP_DIRECTORIES = frozenset(
    (
        ".git",
        ".svn",
        ".renderdoc-ultra",
        "__pycache__",
        "Library",
        "Temp",
        "obj",
        "bin",
    )
)
TEXT_SHADER_EXTENSIONS = frozenset(
    (".hlsl", ".hlsli", ".shader", ".cg", ".cginc", ".usf", ".ush", ".glsl")
)
MAX_BOUND_CONTAINER_BYTES = 512 * 1024 * 1024


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def canonical_json_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonical_sort_key(value: Any) -> str:
    """Return the same stable JSON material used by the hash functions."""

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def normalize_stage(stage: Any) -> str:
    value = str(stage or "").strip().lower()
    aliases = {
        "pixel": "fragment",
        "ps": "fragment",
        "vs": "vertex",
        "gs": "geometry",
        "hs": "hull",
        "ds": "domain",
        "cs": "compute",
        "as": "task",
        "amplification": "task",
        "ms": "mesh",
        "raygeneration": "raygen",
        "ray generation": "raygen",
        "ray-generation": "raygen",
        "ray_generation": "raygen",
        "tess_ctrl": "hull",
        "tess_eval": "domain",
    }
    return aliases.get(value, value)


def _layout_material(shader: dict) -> list[dict]:
    buffers = shader.get(
        "constantBuffers",
        shader.get("constantBlocks", shader.get("constant_blocks", [])),
    ) or []
    if not isinstance(buffers, list):
        return []
    result: list[dict] = []
    for block in buffers:
        if not isinstance(block, dict):
            continue
        fields = block.get("fields", block.get("variables", [])) or []
        result.append(
            {
                "space": _safe_int(block.get("space", block.get("registerSpace", 0))),
                "bind": _safe_int(block.get("bind", block.get("bindNumber", -1)), -1),
                "arraySize": _safe_int(
                    block.get("arraySize", block.get("bindArraySize", 1)), 1
                ),
                "size": _safe_int(block.get("byteSize", block.get("size", 0))),
                "bufferBacked": bool(block.get("bufferBacked", True)),
                "inlineDataBytes": bool(block.get("inlineDataBytes", False)),
                "compileConstants": bool(block.get("compileConstants", False)),
                "fields": _normalize_fields(fields),
            }
        )
    result.sort(key=_canonical_sort_key)
    return result


def _normalise_base_type(field: dict) -> str:
    if field.get("members") or field.get("fields"):
        return "struct"
    value = str(field.get("baseType", field.get("type", "")) or "").strip().lower()
    value = value.rsplit(".", 1)[-1].replace("_", "").replace(" ", "")
    value = re.sub(r"\[\d*\]$", "", value)
    # When only an HLSL spelling is present, vector/matrix shape belongs in the
    # rows/columns fields and must not alter the scalar base type identity.
    value = re.sub(r"\d+(x\d+)?$", "", value)
    aliases = {
        "float32": "float",
        "single": "float",
        "sint": "int",
        "signedint": "int",
        "int32": "int",
        "unsignedint": "uint",
        "uint32": "uint",
        "float16": "half",
        "min16float": "half",
        "float64": "double",
    }
    return aliases.get(value, value)


def _normalize_fields(fields: Iterable[dict]) -> list[dict]:
    result: list[dict] = []
    for field in fields:
        if not isinstance(field, dict):
            continue
        children = field.get("members", field.get("fields", [])) or []
        result.append(
            {
                "offset": _safe_int(
                    field.get("byteOffset", field.get("offset", -1)), -1
                ),
                "byteSize": _safe_int(field.get("byteSize", 0)),
                "type": _normalise_base_type(field),
                "rows": _safe_int(field.get("rows", 1), 1),
                "columns": _safe_int(
                    field.get("columns", field.get("cols", 1)), 1
                ),
                "elements": _safe_int(field.get("elements", 1), 1),
                "arrayStride": _safe_int(
                    field.get("arrayStride", field.get("arrayByteStride", 0))
                ),
                "matrixStride": _safe_int(
                    field.get("matrixStride", field.get("matrixByteStride", 0))
                ),
                "rowMajor": bool(field.get("rowMajor", False)),
                "flags": _safe_int(field.get("flags", 0)),
                "pointerTypeID": _safe_int(
                    field.get("pointerTypeID", 0xFFFFFFFF)
                ),
                "bitFieldOffset": _safe_int(field.get("bitFieldOffset", 0)),
                "bitFieldSize": _safe_int(field.get("bitFieldSize", 0)),
                "members": _normalize_fields(children),
            }
        )
    result.sort(key=_canonical_sort_key)
    return result


def layout_hash(shader: dict) -> str:
    material = _layout_material(shader)
    return canonical_json_hash(material) if material else ""


def _normalise_signature(values: Iterable[dict]) -> list[dict]:
    result: list[dict] = []
    for value in values or []:
        if not isinstance(value, dict):
            continue
        result.append(
            {
                "location": _safe_int(
                    value.get("regIndex", value.get("location", -1)), -1
                ),
                "semantic": str(
                    value.get("semanticName", value.get("semantic", "")) or ""
                ).lower(),
                "semanticIndexName": str(
                    value.get("semanticIdxName", "") or ""
                ).lower(),
                "semanticIndex": _safe_int(
                    value.get("semanticIndex", value.get("index", 0))
                ),
                "systemValue": _enum_token(value.get("systemValue", "")),
                "componentType": _normalise_base_type(
                    {
                        "baseType": value.get(
                            "compType", value.get("componentType", "")
                        )
                    }
                ),
                "componentCount": _safe_int(
                    value.get("compCount", value.get("componentCount", 0))
                ),
                "stream": _safe_int(value.get("stream", 0)),
                "registerMask": _safe_int(
                    value.get("regChannelMask", value.get("registerMask", 0))
                ),
                "usedMask": _safe_int(
                    value.get("channelUsedMask", value.get("usedMask", 0))
                ),
                "perPrimitiveRate": bool(value.get("perPrimitiveRate", False)),
                "needSemanticIndex": bool(value.get("needSemanticIndex", False)),
            }
        )
    result.sort(key=_canonical_sort_key)
    return result


def _normalise_variable_type(value: Any) -> dict:
    if not isinstance(value, dict):
        # Legacy manifests sometimes expose only a scalar token. Retain it for
        # candidate ranking, but interface_is_complete() will not trust it as a
        # complete physical resource element layout.
        return {"legacyToken": _enum_token(value)}
    members = value.get("members", value.get("fields", [])) or []
    result = {
        "baseType": _normalise_base_type(value),
        "rows": _safe_int(value.get("rows", 0)),
        "columns": _safe_int(value.get("columns", value.get("cols", 0))),
        "elements": _safe_int(value.get("elements", 0)),
        "arrayStride": _safe_int(
            value.get("arrayByteStride", value.get("arrayStride", 0))
        ),
        "matrixStride": _safe_int(
            value.get("matrixByteStride", value.get("matrixStride", 0))
        ),
        "rowMajor": bool(value.get("rowMajor", False)),
        "flags": _safe_int(value.get("flags", 0)),
        "pointerTypeID": _safe_int(value.get("pointerTypeID", 0xFFFFFFFF)),
        "byteSize": _safe_int(value.get("byteSize", 0)),
        "members": _normalize_fields(members),
    }
    if value.get("layoutCompleteness") == "opaque":
        result["layoutCompleteness"] = "opaque"
    return result


def _normalise_bindings(values: Iterable[dict]) -> list[dict]:
    result: list[dict] = []
    for value in values or []:
        if not isinstance(value, dict):
            continue
        result.append(
            {
                "space": _safe_int(
                    value.get("space", value.get("fixedBindSetOrSpace", 0))
                ),
                "bind": _safe_int(
                    value.get("bind", value.get("fixedBindNumber", -1)), -1
                ),
                "arraySize": _safe_int(
                    value.get("arraySize", value.get("bindArraySize", 1)), 1
                ),
                "descriptorType": _enum_token(
                    value.get(
                        "descriptorType",
                        value.get("resourceType", value.get("resType", "")),
                    )
                ),
                "textureType": _enum_token(
                    value.get("textureType", value.get("dimension", ""))
                ),
                "variableType": _normalise_variable_type(
                    value.get("variableType", "")
                ),
                "isTexture": bool(value.get("isTexture", False)),
                "hasSampler": bool(value.get("hasSampler", False)),
                "isInputAttachment": bool(value.get("isInputAttachment", False)),
                "isReadOnly": bool(value.get("isReadOnly", False)),
            }
        )
    result.sort(key=_canonical_sort_key)
    return result


def _normalise_samplers(values: Iterable[dict]) -> list[dict]:
    result: list[dict] = []
    for value in values or []:
        if not isinstance(value, dict):
            continue
        result.append(
            {
                "space": _safe_int(
                    value.get("space", value.get("fixedBindSetOrSpace", 0))
                ),
                "bind": _safe_int(
                    value.get("bind", value.get("fixedBindNumber", -1)), -1
                ),
                "arraySize": _safe_int(
                    value.get("arraySize", value.get("bindArraySize", 1)), 1
                ),
            }
        )
    result.sort(key=_canonical_sort_key)
    return result


def _enum_token(value: Any) -> str:
    return str(value or "").strip().lower().rsplit(".", 1)[-1].replace("_", "")


_INTERFACE_LIST_KEYS = (
    "inputSignature",
    "outputSignature",
    "readOnlyResources",
    "readWriteResources",
    "samplers",
    "threadGroupSize",
)


_VARIABLE_TYPE_KEYS = (
    "baseType",
    "rows",
    "columns",
    "elements",
    "arrayByteStride",
    "matrixByteStride",
    "rowMajor",
    "flags",
    "pointerTypeID",
    "byteSize",
    "members",
)


def _is_physical_int(
    value: Any, minimum: int = 0, maximum: int | None = None
) -> bool:
    return bool(
        not isinstance(value, bool)
        and isinstance(value, int)
        and value >= minimum
        and (maximum is None or value <= maximum)
    )


def _alias_value(value: dict, *keys: str) -> Any:
    for key in keys:
        if key in value:
            return value[key]
    return None


def _aliases_are_consistent(
    value: dict,
    keys: tuple[str, ...],
    predicate: Callable[[Any], bool],
    normalise: Callable[[Any], Any] | None = None,
) -> bool:
    found = [value[key] for key in keys if key in value]
    if not found or not all(predicate(item) for item in found):
        return False
    convert = normalise or (lambda item: item)
    expected = convert(found[0])
    return all(convert(item) == expected for item in found[1:])


def _variable_type_is_complete(value: Any) -> bool:
    if not isinstance(value, dict) or not all(
        key in value for key in _VARIABLE_TYPE_KEYS
    ):
        return False
    members = value.get("members")
    opaque = value.get("layoutCompleteness") == "opaque"
    if opaque and (
            _normalise_base_type(value) != "unknown" or members != [] or
            value.get("elements") != 1 or value.get("rows") != 0 or value.get("columns") != 0 or
            not _is_physical_int(value.get("byteSize"), 1) or
            value.get("arrayByteStride") != value.get("byteSize") or value["byteSize"] % 4):
        return False
    aggregate = _normalise_base_type({"baseType": value.get("baseType")}) == "struct" and bool(members)
    if (
        not isinstance(value.get("baseType"), str)
        or not str(value.get("baseType", "")).strip()
        or not _is_physical_int(value.get("rows"), 0 if aggregate or opaque else 1)
        or not _is_physical_int(value.get("columns"), 0 if aggregate or opaque else 1)
        or not _is_physical_int(value.get("elements"), 1)
        or not _is_physical_int(value.get("arrayByteStride"))
        or not _is_physical_int(value.get("matrixByteStride"))
        or not isinstance(value.get("rowMajor"), bool)
        or not _is_physical_int(value.get("flags"))
        or not _is_physical_int(value.get("pointerTypeID"))
        or not _is_physical_int(value.get("byteSize"))
        or not isinstance(members, list)
    ):
        return False
    member_limit = value["byteSize"]
    if value["elements"] == 0xFFFFFFFF:
        if member_limit != 0 or value["arrayByteStride"] <= 0:
            return False
        member_limit = value["arrayByteStride"]
    elif aggregate and value["elements"] > 1:
        stride = value["arrayByteStride"]
        if stride <= 0 or stride * (value["elements"] - 1) >= member_limit:
            return False
        member_limit = min(stride, member_limit - stride * (value["elements"] - 1))
    return _fields_are_complete(members, member_limit, allow_runtime_arrays=True)


def _fields_are_complete(values: list, parent_size: int | None = None,
                         *, allow_runtime_arrays: bool = False) -> bool:
    requirements = (
        ("byteOffset", "offset"),
        ("byteSize",),
        ("baseType", "type"),
        ("rows",),
        ("columns", "cols"),
        ("elements",),
        ("arrayByteStride", "arrayStride"),
        ("matrixByteStride", "matrixStride"),
        ("rowMajor",),
        ("flags",),
        ("pointerTypeID",),
    )
    for value in values:
        if not isinstance(value, dict) or not all(
            _has_one(value, *keys) for keys in requirements
        ):
            return False
        offset = _alias_value(value, "byteOffset", "offset")
        byte_size = value.get("byteSize")
        children = value.get("members", value.get("fields", []))
        aggregate = _normalise_base_type({"baseType": value.get("baseType", value.get("type"))}) == "struct" and bool(children)
        runtime_array = value.get("elements") == 0xFFFFFFFF
        if runtime_array and not allow_runtime_arrays:
            return False
        if (
            not _aliases_are_consistent(
                value,
                ("byteOffset", "offset"),
                lambda item: _is_physical_int(item),
            )
            or not _is_physical_int(byte_size, 0 if runtime_array else 1)
            or not _aliases_are_consistent(
                value,
                ("baseType", "type"),
                lambda item: isinstance(item, str) and bool(item.strip()),
                lambda item: _normalise_base_type({"baseType": item}),
            )
            or not _is_physical_int(value.get("rows"), 0 if aggregate else 1)
            or not _aliases_are_consistent(
                value,
                ("columns", "cols"),
                lambda item: _is_physical_int(item, 0 if aggregate else 1),
            )
            or not _is_physical_int(value.get("elements"), 1)
            or not _aliases_are_consistent(
                value,
                ("arrayByteStride", "arrayStride"),
                lambda item: _is_physical_int(item),
            )
            or not _aliases_are_consistent(
                value,
                ("matrixByteStride", "matrixStride"),
                lambda item: _is_physical_int(item),
            )
            or not isinstance(value.get("rowMajor"), bool)
            or not _is_physical_int(value.get("flags"))
            or not _is_physical_int(value.get("pointerTypeID"))
            or (
                parent_size is not None
                and (offset + byte_size > parent_size)
            )
        ):
            return False
        bit_offset = value.get("bitFieldOffset", 0)
        bit_size = value.get("bitFieldSize", 0)
        if (
            not _is_physical_int(bit_offset, 0, 63)
            or not _is_physical_int(bit_size, 0, 64)
            or (bit_size == 0 and bit_offset != 0)
            or bit_offset + bit_size > 64
        ):
            return False
        member_limit = byte_size
        if runtime_array:
            stride = _alias_value(value, "arrayByteStride", "arrayStride")
            if byte_size != 0 or stride <= 0 or bit_size:
                return False
            # A runtime tail has no fixed end. It must be the last member and
            # must not overlap the known prefix or another unsized tail.
            for sibling in values:
                if sibling is value:
                    continue
                sibling_offset = _alias_value(sibling, "byteOffset", "offset")
                sibling_size = sibling.get("byteSize")
                if (not _is_physical_int(sibling_offset)
                        or not _is_physical_int(sibling_size)
                        or sibling_offset >= offset
                        or sibling_offset + sibling_size > offset):
                    return False
            member_limit = stride
        elif aggregate and value["elements"] > 1:
            stride = _alias_value(value, "arrayByteStride", "arrayStride")
            if stride <= 0 or stride * (value["elements"] - 1) >= byte_size:
                return False
            member_limit = min(stride, byte_size - stride * (value["elements"] - 1))
        if not isinstance(children, list) or not _fields_are_complete(
            children, member_limit, allow_runtime_arrays=allow_runtime_arrays
        ):
            return False
    return True


def _has_one(value: dict, *keys: str) -> bool:
    return any(key in value for key in keys)


def _records_have_physical_keys(values: list, kind: str, *, output: bool = False) -> bool:
    for value in values:
        if not isinstance(value, dict):
            return False
        if kind == "signature":
            requirements = (
                ("semanticName", "semantic"),
                ("semanticIdxName",),
                ("semanticIndex", "index"),
                ("regIndex", "location"),
                ("systemValue",),
                ("compType", "componentType"),
                ("compCount", "componentCount"),
                ("stream",),
                ("regChannelMask", "registerMask"),
                ("channelUsedMask", "usedMask"),
                ("perPrimitiveRate",),
                ("needSemanticIndex",),
            )
        elif kind == "binding":
            if (isinstance(value.get("variableType"), dict) and
                    value["variableType"].get("layoutCompleteness") == "opaque" and
                    (value.get("isTexture") is not False or
                     _enum_token(value.get("textureType")) != "buffer")):
                return False
            requirements = (
                ("space", "fixedBindSetOrSpace"),
                ("bind", "fixedBindNumber"),
                ("arraySize", "bindArraySize"),
                ("descriptorType", "resourceType", "resType"),
                ("textureType", "dimension"),
                ("variableType",),
                ("isTexture",),
                ("hasSampler",),
                ("isInputAttachment",),
                ("isReadOnly",),
            )
        elif kind == "sampler":
            requirements = (
                ("space", "fixedBindSetOrSpace"),
                ("bind", "fixedBindNumber"),
                ("arraySize", "bindArraySize"),
            )
        else:
            requirements = (
                ("space", "registerSpace"),
                ("bind", "bindNumber"),
                ("arraySize", "bindArraySize"),
                ("byteSize", "size"),
                ("bufferBacked",),
                ("inlineDataBytes",),
                ("compileConstants",),
            )
        if not all(_has_one(value, *keys) for keys in requirements):
            return False
        if kind == "signature":
            component_count = _alias_value(value, "compCount", "componentCount")
            register_mask = _alias_value(
                value, "regChannelMask", "registerMask"
            )
            used_mask = _alias_value(value, "channelUsedMask", "usedMask")
            if (
                not _aliases_are_consistent(
                    value,
                    ("semanticName", "semantic"),
                    lambda item: isinstance(item, str),
                    lambda item: item.lower(),
                )
                or not isinstance(value.get("semanticIdxName"), str)
                or not _aliases_are_consistent(
                    value,
                    ("semanticIndex", "index"),
                    lambda item: _is_physical_int(item),
                )
                or not _aliases_are_consistent(
                    value,
                    ("regIndex", "location"),
                    lambda item: _is_physical_int(item, -1),
                )
                or not isinstance(value.get("systemValue"), str)
                or not _aliases_are_consistent(
                    value,
                    ("compType", "componentType"),
                    lambda item: isinstance(item, str) and bool(item.strip()),
                    lambda item: _normalise_base_type({"baseType": item}),
                )
                or not _aliases_are_consistent(
                    value,
                    ("compCount", "componentCount"),
                    lambda item: _is_physical_int(item, 0, 4),
                )
                or not _is_physical_int(value.get("stream"))
                or not _aliases_are_consistent(
                    value,
                    ("regChannelMask", "registerMask"),
                    lambda item: _is_physical_int(item, 0, 0xF),
                )
                or not _aliases_are_consistent(
                    value,
                    ("channelUsedMask", "usedMask"),
                    lambda item: _is_physical_int(item, 0, 0xF),
                )
                or not isinstance(value.get("perPrimitiveRate"), bool)
                or not isinstance(value.get("needSemanticIndex"), bool)
                # DXBC outputs retain the raw never-written mask, whose bits
                # may be outside the declared lanes. Match the workspace ABI
                # parser while keeping input usage and physical masks strict.
                or (not output and used_mask & ~register_mask)
                or (
                    component_count
                    and register_mask.bit_count() != component_count
                )
            ):
                return False
        elif kind == "binding":
            if (
                not _aliases_are_consistent(
                    value,
                    ("space", "fixedBindSetOrSpace"),
                    lambda item: _is_physical_int(item),
                )
                or not _aliases_are_consistent(
                    value,
                    ("bind", "fixedBindNumber"),
                    lambda item: _is_physical_int(item),
                )
                or not _aliases_are_consistent(
                    value,
                    ("arraySize", "bindArraySize"),
                    lambda item: _is_physical_int(item, 1),
                )
                or not _aliases_are_consistent(
                    value,
                    ("descriptorType", "resourceType", "resType"),
                    lambda item: isinstance(item, str) and bool(item.strip()),
                    _enum_token,
                )
                or not _aliases_are_consistent(
                    value,
                    ("textureType", "dimension"),
                    lambda item: isinstance(item, str),
                    _enum_token,
                )
                or not _variable_type_is_complete(value.get("variableType"))
                or any(
                    not isinstance(value.get(key), bool)
                    for key in (
                        "isTexture",
                        "hasSampler",
                        "isInputAttachment",
                        "isReadOnly",
                    )
                )
            ):
                return False
        elif kind == "sampler":
            if (
                not _aliases_are_consistent(
                    value,
                    ("space", "fixedBindSetOrSpace"),
                    lambda item: _is_physical_int(item),
                )
                or not _aliases_are_consistent(
                    value,
                    ("bind", "fixedBindNumber"),
                    lambda item: _is_physical_int(item),
                )
                or not _aliases_are_consistent(
                    value,
                    ("arraySize", "bindArraySize"),
                    lambda item: _is_physical_int(item, 1),
                )
            ):
                return False
        else:
            if (
                not _aliases_are_consistent(
                    value,
                    ("space", "registerSpace"),
                    lambda item: _is_physical_int(item),
                )
                or not _aliases_are_consistent(
                    value,
                    ("bind", "bindNumber"),
                    lambda item: _is_physical_int(item),
                )
                or not _aliases_are_consistent(
                    value,
                    ("arraySize", "bindArraySize"),
                    lambda item: _is_physical_int(item, 1),
                )
                or not _aliases_are_consistent(
                    value,
                    ("byteSize", "size"),
                    lambda item: _is_physical_int(item),
                )
                or any(
                    not isinstance(value.get(key), bool)
                    for key in (
                        "bufferBacked",
                        "inlineDataBytes",
                        "compileConstants",
                    )
                )
            ):
                return False
    return True


def _bindings_are_unique(values: list) -> bool:
    seen: set[tuple[int, int]] = set()
    for value in values:
        binding = (
            _alias_value(value, "space", "fixedBindSetOrSpace", "registerSpace"),
            _alias_value(value, "bind", "fixedBindNumber", "bindNumber"),
        )
        if binding in seen:
            return False
        seen.add(binding)
    return True


def _signatures_are_unique(values: list) -> bool:
    material = _normalise_signature(values)
    return len(material) == len({_canonical_sort_key(value) for value in material})


def interface_is_complete(shader: dict) -> bool:
    """Return true only for an explicit, structurally complete ABI snapshot.

    Empty arrays are meaningful, so key presence is required. A manifest's
    ``interfaceComplete`` claim alone is never enough to turn omitted data into
    a complete interface.
    """

    if shader.get("interfaceComplete") is not True:
        return False
    if not all(
        key in shader and isinstance(shader[key], list)
        for key in _INTERFACE_LIST_KEYS
    ):
        return False
    if "outputTopology" not in shader or "dynamicLinkageInterfaceCount" not in shader:
        return False
    blocks = shader.get("constantBuffers", shader.get("constantBlocks"))
    if not isinstance(blocks, list):
        return False
    if not _records_have_physical_keys(shader["inputSignature"], "signature"):
        return False
    if not _records_have_physical_keys(shader["outputSignature"], "signature", output=True):
        return False
    if not _signatures_are_unique(shader["inputSignature"]):
        return False
    if not _signatures_are_unique(shader["outputSignature"]):
        return False
    for key in ("readOnlyResources", "readWriteResources"):
        if not _records_have_physical_keys(shader[key], "binding"):
            return False
        if not _bindings_are_unique(shader[key]):
            return False
    if any(not value.get("isReadOnly") for value in shader["readOnlyResources"]):
        return False
    if any(value.get("isReadOnly") for value in shader["readWriteResources"]):
        return False
    if not _records_have_physical_keys(shader["samplers"], "sampler"):
        return False
    if not _bindings_are_unique(shader["samplers"]):
        return False
    if not _records_have_physical_keys(blocks, "constantBuffer"):
        return False
    if not _bindings_are_unique(blocks):
        return False
    for block in blocks:
        fields = block.get("fields", block.get("variables"))
        block_size = _alias_value(block, "byteSize", "size")
        if not isinstance(fields, list) or not _fields_are_complete(
            fields, block_size
        ):
            return False
    if not isinstance(shader.get("outputTopology"), str):
        return False
    dynamic_count = shader.get("dynamicLinkageInterfaceCount")
    if (
        isinstance(dynamic_count, bool)
        or not isinstance(dynamic_count, int)
        or dynamic_count < 0
    ):
        return False
    dimensions = shader.get("threadGroupSize", [])
    if len(dimensions) not in (0, 3):
        return False
    if dimensions and any(not _is_physical_int(value, 1) for value in dimensions):
        return False
    stage = normalize_stage(shader.get("stage", ""))
    known_graphics_stages = {"vertex", "fragment", "geometry", "hull", "domain"}
    if stage == "compute" and len(dimensions) != 3:
        return False
    if stage in known_graphics_stages and dimensions:
        return False
    return True


def interface_hash(shader: dict) -> str:
    """Hash a complete name-independent shader interface when one is supplied."""

    dimensions = shader.get("threadGroupSize", []) or []
    if not isinstance(dimensions, list):
        dimensions = []
    material = {
        "inputs": _normalise_signature(
            shader.get("inputSignature", shader.get("inputs", [])) or []
        ),
        "outputs": _normalise_signature(
            shader.get("outputSignature", shader.get("outputs", [])) or []
        ),
        "readOnly": _normalise_bindings(
            shader.get("readOnlyResources", shader.get("resources", [])) or []
        ),
        "readWrite": _normalise_bindings(shader.get("readWriteResources", []) or []),
        "samplers": _normalise_samplers(shader.get("samplers", []) or []),
        # The interface digest is a complete physical ABI digest.  layout_hash
        # remains a useful independently queryable subset, but CB fields must
        # not disappear from the stronger interface identity.
        "constantBuffers": _layout_material(shader),
        "threadGroupSize": [_safe_int(value) for value in dimensions],
        "outputTopology": _enum_token(shader.get("outputTopology", "")),
        "dynamicLinkageInterfaceCount": _safe_int(
            shader.get("dynamicLinkageInterfaceCount", -1), -1
        ),
    }
    return canonical_json_hash(material)


def _inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _bound_manifest_container(
    shader: dict,
    root: Path,
    max_file_bytes: int,
) -> tuple[ShaderContainer | None, str, str]:
    """Physically validate an explicitly bound manifest container.

    Provider booleans are deliberately ignored. Verification exists only when
    bytes at the declared in-root path and offset parse as a real DXBC/DXIL
    container and reproduce every supplied digest.
    """

    reference = shader.get("blobPath", shader.get("containerPath"))
    if reference is None:
        return None, "", ""
    reference_text = str(reference or "").strip()
    if not reference_text:
        return None, "", "blobPath/containerPath must not be empty"

    candidate = Path(reference_text)
    candidate = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if not _inside(root, candidate):
        return None, "", "bound container escapes the asset root: %s" % reference_text
    if not candidate.is_file():
        return None, "", "bound container is not a regular file: %s" % reference_text

    try:
        file_size = candidate.stat().st_size
        if file_size > max_file_bytes:
            return (
                None,
                "",
                "bound container file exceeds the scan size limit: %s" % reference_text,
            )
        offset = int(shader.get("fileOffset", 0))
        if offset < 0 or offset > file_size - 32:
            return None, "", "bound container offset is outside the file"
        with candidate.open("rb") as stream:
            stream.seek(offset)
            header = stream.read(32)
            if len(header) != 32 or header[:4] != b"DXBC":
                return None, "", "no DXBC/DXIL container at the declared offset"
            total_size = struct.unpack_from("<I", header, 24)[0]
            if (
                total_size < 32
                or total_size > MAX_BOUND_CONTAINER_BYTES
                or total_size > file_size - offset
            ):
                return None, "", "bound container has an invalid byte size"
            declared_size = int(shader.get("byteSize", 0) or 0)
            if declared_size and declared_size != total_size:
                return None, "", "bound container byteSize does not match the file"
            stream.seek(offset)
            blob = stream.read(total_size)
    except (OSError, TypeError, ValueError, OverflowError, struct.error) as error:
        return None, "", str(error)

    container = fingerprint_bytes(blob, str(candidate))
    if container is None:
        return None, "", "bytes at the declared offset are not a valid shader container"
    declared_raw = str(shader.get("rawSha256", shader.get("raw_sha256", "")) or "").lower()
    declared_code = str(
        shader.get("codeSha256", shader.get("executableSha256", "")) or ""
    ).lower()
    if declared_raw and declared_raw != container.raw_sha256:
        return None, "", "bound container rawSha256 mismatch"
    if declared_code and declared_code != container.code_sha256:
        return None, "", "bound container codeSha256 mismatch"
    return container, str(candidate.relative_to(root)), ""


def _manifest_records(
    path: Path,
    root: Path,
    expected_build_id: str,
    max_bound_file_bytes: int,
) -> tuple[list[dict], bool, list[str]]:
    """Read a versioned symbol manifest or compatible top-level shader list."""

    try:
        if path.stat().st_size > 64 * 1024 * 1024:
            return [], False, ["symbol manifest exceeds the 64 MiB limit"]
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return [], False, [str(error)]

    if not isinstance(data, dict):
        return [], False, []
    shaders = data.get("shaders")
    if not isinstance(shaders, list):
        return [], False, []

    schema = str(data.get("schema", data.get("schemaVersion", "")))
    if schema.startswith("renderdoc-ultra-symbols/") and schema != SYMBOL_SCHEMA:
        return [], True, ["unsupported symbol manifest schema: %s" % schema]
    manifest_build = str(data.get("buildId", "") or "").strip()
    if (
        manifest_build
        and expected_build_id != "unknown-build"
        and manifest_build != expected_build_id
    ):
        return [], True, []
    # Generic JSON files are accepted only when shader entries carry a stable
    # fingerprint.  This prevents an arbitrary game configuration with a
    # `shaders` key from being mistaken for trusted symbol metadata.
    records: list[dict] = []
    record_errors: list[str] = []
    for index, shader in enumerate(shaders):
        if not isinstance(shader, dict):
            record_errors.append("shader record %d is not an object" % index)
            continue
        raw_hash = str(
            shader.get("rawSha256", shader.get("raw_sha256", "")) or ""
        ).lower()
        code_hash = str(
            shader.get("codeSha256", shader.get("executableSha256", "")) or ""
        ).lower()
        if not raw_hash and not code_hash:
            continue
        if raw_hash and not re.fullmatch(r"[0-9a-f]{64}", raw_hash):
            record_errors.append("shader record %d has an invalid rawSha256" % index)
            continue
        if code_hash and not re.fullmatch(r"[0-9a-f]{64}", code_hash):
            record_errors.append("shader record %d has an invalid codeSha256" % index)
            continue

        relative = str(
            shader.get("path", shader.get("assetPath", path.relative_to(root)))
        )
        display = str(
            shader.get("displayName", shader.get("name", Path(relative).stem))
        )
        metadata = dict(shader)
        bound_container, bound_relative, binding_error = _bound_manifest_container(
            shader, root, max_bound_file_bytes
        )
        if binding_error:
            record_errors.append("shader record %d: %s" % (index, binding_error))
        metadata["_symbolSource"] = {
            "manifest": str(path.relative_to(root)),
            "recordIndex": index,
            "schema": schema,
            "buildId": data.get("buildId", ""),
            "provider": data.get("provider", {}),
            "assetPath": relative,
        }
        metadata["_computedInterfaceHash"] = interface_hash(shader)
        metadata["_interfaceComplete"] = interface_is_complete(shader)
        # Manifests are input data, not an authority. Preserve the provider's
        # claim for audit, but never expose it as validator-owned verification.
        metadata["_claimedHashVerified"] = bool(shader.get("hashVerified", False))
        metadata["_hashVerified"] = bound_container is not None
        metadata["_hashVerification"] = (
            "scanner-bound-container"
            if bound_container is not None
            else "untrusted-manifest-claim"
        )
        # Byte binding proves program identity only. It does not authenticate
        # provider-supplied names, field layouts, or asset ownership metadata.
        metadata["_symbolMetadataTrusted"] = False
        if bound_container is not None:
            bound_metadata = bound_container.as_dict()
            bound_metadata["fileOffset"] = _safe_int(shader.get("fileOffset", 0))
            bound_metadata["relativePath"] = bound_relative
            metadata["_boundContainer"] = bound_metadata
        record = {
            "relativePath": relative,
            "fileOffset": _safe_int(shader.get("fileOffset", 0)),
            "byteSize": (
                bound_container.byte_size
                if bound_container is not None
                else _safe_int(shader.get("byteSize", 0))
            ),
            "kind": (
                bound_container.kind
                if bound_container is not None
                else str(shader.get("kind", shader.get("encoding", "")))
            ),
            "rawSha256": (
                bound_container.raw_sha256 if bound_container is not None else raw_hash
            ),
            "codeSha256": (
                bound_container.code_sha256 if bound_container is not None else code_hash
            ),
            "stage": normalize_stage(shader.get("stage", "")),
            "entryPoint": str(shader.get("entryPoint", shader.get("entry", ""))),
            "displayName": display,
            "layoutHash": layout_hash(shader) or str(shader.get("layoutHash", "")),
            "metadata": metadata,
            "provenance": (
                "manifest-bound-container"
                if bound_container is not None
                else "manifest:%s:%d" % (path.relative_to(root), index)
            ),
        }
        records.append(record)
    return records, True, record_errors


def _provider_field_layout_complete(field: Any, parent_size: int) -> bool:
    if not isinstance(field, dict):
        return False
    required = (
        ("name",),
        ("byteOffset", "offset"),
        ("byteSize", "size"),
        ("type", "baseType"),
        ("rows",),
        ("columns", "cols"),
        ("elements",),
        ("arrayStride", "arrayByteStride"),
        ("matrixStride", "matrixByteStride"),
        ("rowMajor",),
        ("flags",),
        ("pointerTypeID",),
        ("bitFieldOffset",),
        ("bitFieldSize",),
    )
    if not all(_has_one(field, *keys) for keys in required):
        return False
    if not str(field.get("name", "") or "").strip():
        return False
    if not str(field.get("type", field.get("baseType", "")) or "").strip():
        return False
    offset = _safe_int(field.get("byteOffset", field.get("offset", -1)), -1)
    size = _safe_int(field.get("byteSize", field.get("size", 0)), 0)
    rows = _safe_int(field.get("rows", 0), 0)
    columns = _safe_int(field.get("columns", field.get("cols", 0)), 0)
    elements = _safe_int(field.get("elements", 0), 0)
    if (
        offset < 0
        or size <= 0
        or rows <= 0
        or columns <= 0
        or elements <= 0
        or offset + size > parent_size
    ):
        return False
    children = field.get("members", field.get("fields", [])) or []
    if not isinstance(children, list):
        return False
    return all(_provider_field_layout_complete(child, size) for child in children)


def _provider_layout_is_complete(shader: dict) -> bool:
    blocks = shader.get("constantBuffers", shader.get("constantBlocks"))
    if not isinstance(blocks, list):
        return False
    bindings: set[tuple[int, int]] = set()
    for block in blocks:
        if not isinstance(block, dict):
            return False
        binding = (
            _safe_int(block.get("space", block.get("registerSpace", 0))),
            _safe_int(block.get("bind", block.get("bindNumber", -1)), -1),
        )
        if binding[1] < 0 or binding in bindings:
            return False
        bindings.add(binding)
        size = _safe_int(block.get("byteSize", block.get("size", 0)), 0)
        fields = block.get("fields", block.get("variables"))
        if size <= 0 or not isinstance(fields, list) or not fields:
            return False
        if not all(_provider_field_layout_complete(field, size) for field in fields):
            return False
    return True


def _declared_digest_matches(value: Any, computed: str) -> bool:
    declared = str(value or "").strip().lower()
    return not declared or bool(
        re.fullmatch(r"[0-9a-f]{64}", declared) and declared == computed
    )


def _trusted_provider_records(
    result: TrustedProviderResult,
) -> tuple[list[dict], dict]:
    """Convert an attested capability into independently revalidated rows."""

    manifest = result.manifest
    attestation = result.attestation
    artifacts = {artifact.relative_path: artifact for artifact in result.artifacts}
    audit_base = {
        "providerId": attestation.provider_id,
        "providerVersion": attestation.provider_version,
        "providerBinarySha256": attestation.provider_binary_sha256,
        "registrySha256": attestation.registry_sha256,
        "assetRootSha256": attestation.asset_root_sha256,
        "manifestSha256": attestation.manifest_sha256,
        "artifactsSha256": attestation.artifacts_sha256,
        "outputSchema": attestation.output_schema,
        "runId": attestation.run_id,
        "issuedAtUnixNs": attestation.issued_at_unix_ns,
        "durationMs": attestation.duration_ms,
        "provenance": attestation.provenance,
    }
    records: list[dict] = []
    validation_errors: list[dict] = []
    trusted_count = 0
    provisional_count = 0

    for index, shader in enumerate(manifest.get("shaders", [])):
        reasons: list[str] = []
        raw_claim = str(shader.get("rawSha256", "") or "").lower()
        code_claim = str(
            shader.get("codeSha256", shader.get("executableSha256", "")) or ""
        ).lower()
        artifact_reference = str(
            shader.get("blobPath", shader.get("containerPath", "")) or ""
        ).replace("\\", "/")
        artifact = artifacts.get(artifact_reference) if artifact_reference else None
        container: ShaderContainer | None = None
        exact_artifact_container = False
        raw_matches = False
        code_matches = False
        if artifact is None:
            reasons.append("no-provider-artifact-blob")
        else:
            container = fingerprint_bytes(
                artifact.data,
                "provider://%s/%s" % (attestation.provider_id, artifact_reference),
            )
            if container is None:
                reasons.append("artifact-is-not-dxbc-or-dxil")
            else:
                exact_artifact_container = container.byte_size == len(artifact.data)
                if not exact_artifact_container:
                    reasons.append("artifact-has-bytes-outside-container")
                raw_matches = _declared_digest_matches(
                    raw_claim, container.raw_sha256
                )
                code_matches = _declared_digest_matches(
                    code_claim, container.code_sha256
                )
                if not raw_matches:
                    reasons.append("raw-sha256-mismatch")
                if not code_matches:
                    reasons.append("code-sha256-mismatch")

        interface_complete = interface_is_complete(shader)
        computed_interface = interface_hash(shader) if interface_complete else ""
        interface_digest_matches = bool(
            interface_complete
            and _declared_digest_matches(
                shader.get("interfaceHash", ""), computed_interface
            )
        )
        if not interface_complete:
            reasons.append("incomplete-shader-interface")
        elif not interface_digest_matches:
            reasons.append("interface-hash-mismatch")

        layout_complete = _provider_layout_is_complete(shader)
        computed_layout = layout_hash(shader)
        layout_digest_matches = bool(
            layout_complete
            and _declared_digest_matches(shader.get("layoutHash", ""), computed_layout)
        )
        if not layout_complete:
            reasons.append("incomplete-cbuffer-layout")
        elif not layout_digest_matches:
            reasons.append("layout-hash-mismatch")

        normalized_stage = normalize_stage(shader.get("stage", ""))
        entry_point = str(shader.get("entryPoint", shader.get("entry", ""))).strip()
        identity_complete = bool(
            normalized_stage
            in {"vertex", "fragment", "geometry", "hull", "domain", "compute"}
            and entry_point
        )
        if not identity_complete:
            reasons.append("incomplete-stage-or-entry-point")

        physical_hash_verified = bool(
            container is not None
            and exact_artifact_container
            and raw_matches
            and code_matches
        )
        metadata_trusted = bool(
            physical_hash_verified
            and interface_complete
            and interface_digest_matches
            and layout_complete
            and layout_digest_matches
            and identity_complete
        )
        if metadata_trusted:
            trusted_count += 1
        else:
            provisional_count += 1
            if len(validation_errors) < 1000:
                validation_errors.append(
                    {"recordIndex": index, "reasons": list(dict.fromkeys(reasons))}
                )

        relative = str(
            shader.get(
                "path",
                shader.get(
                    "assetPath",
                    "provider/%s/shader-%d" % (attestation.provider_id, index),
                ),
            )
        )
        display = str(
            shader.get("displayName", shader.get("name", Path(relative).stem))
        )
        metadata = dict(shader)
        checks = {
            "artifactPresent": artifact is not None,
            "containerParsed": container is not None,
            "artifactExactlyOneContainer": exact_artifact_container,
            "rawSha256Matches": raw_matches,
            "codeSha256Matches": code_matches,
            "interfaceComplete": interface_complete,
            "interfaceHashRecomputed": interface_digest_matches,
            "layoutComplete": layout_complete,
            "layoutHashRecomputed": layout_digest_matches,
            "stageAndEntryPointComplete": identity_complete,
        }
        metadata["_symbolSource"] = {
            "providerId": attestation.provider_id,
            "providerVersion": attestation.provider_version,
            "recordIndex": index,
            "schema": manifest.get("schema", ""),
            "buildId": manifest.get("buildId", ""),
            "assetPath": relative,
        }
        metadata["_providerAudit"] = {
            **audit_base,
            "recordIndex": index,
            "artifactPath": artifact_reference,
            "artifactSha256": artifact.sha256 if artifact is not None else "",
            "checks": checks,
            "provisionalReasons": list(dict.fromkeys(reasons)),
        }
        metadata["_computedInterfaceHash"] = computed_interface
        metadata["_interfaceComplete"] = interface_complete
        metadata["_computedLayoutHash"] = computed_layout
        metadata["_layoutComplete"] = layout_complete
        metadata["_hashVerified"] = physical_hash_verified
        metadata["_hashVerification"] = (
            "trusted-provider-artifact"
            if physical_hash_verified
            else "trusted-provider-provisional"
        )
        metadata["_symbolMetadataTrusted"] = metadata_trusted
        if container is not None:
            bound = container.as_dict()
            bound["relativePath"] = artifact_reference
            metadata["_boundContainer"] = bound

        record = {
            "relativePath": "@provider/%s/%s"
            % (attestation.provider_id, relative),
            "fileOffset": 0,
            "byteSize": (
                container.byte_size
                if container is not None
                else _safe_int(shader.get("byteSize", 0))
            ),
            "kind": (
                container.kind
                if container is not None
                else str(shader.get("kind", shader.get("encoding", "")))
            ),
            "rawSha256": (
                container.raw_sha256
                if physical_hash_verified and container is not None
                else raw_claim
            ),
            "codeSha256": (
                container.code_sha256
                if physical_hash_verified and container is not None
                else code_claim
            ),
            "stage": normalized_stage,
            "entryPoint": entry_point,
            "displayName": display,
            "layoutHash": computed_layout or str(shader.get("layoutHash", "")),
            "metadata": metadata,
            "provenance": (
                "trusted-provider-artifact"
                if metadata_trusted
                else "trusted-provider-provisional"
            ),
        }
        records.append(record)

    audit = {
        **audit_base,
        "attestationVerified": True,
        "artifactCount": len(artifacts),
        "shaderRecords": len(records),
        "trustedShaderRecords": trusted_count,
        "provisionalShaderRecords": provisional_count,
        "status": "verified" if provisional_count == 0 else "provisional",
        "validationErrors": validation_errors,
    }
    return records, audit


def _iter_files(
    root: Path,
    max_files: int,
    on_error: Callable[[OSError], None] | None = None,
) -> Iterable[Path]:
    count = 0
    for current, directories, filenames in os.walk(root, onerror=on_error):
        directories[:] = sorted(d for d in directories if d not in SKIP_DIRECTORIES)
        for filename in sorted(filenames):
            yield Path(current) / filename
            count += 1
            if count >= max_files:
                return


@contextmanager
def _exclusive_scan_lock(lock_path: Path) -> Iterable[None]:
    """Serialize scans, recovering only OS-proven inactive owners, never by age."""

    from .. import active_projects as locks

    payload = locks._exclusive_project_lock_payload(secrets.token_hex(16))
    for attempt in range(2):
        try:
            # A complete, fsynced v2 payload becomes visible atomically. A crash
            # cannot leave an empty authoritative lock between create and write.
            locks._publish_exclusive_project_lock(lock_path, payload)
            break
        except FileExistsError as error:
            if not attempt and locks._archive_recoverable_exclusive_project_lock(
                lock_path, is_recoverable=_recoverable_scan_lock,
            ) is not None:
                continue
            snapshot = locks._lock_snapshot(lock_path)
            owner = snapshot[0].decode("utf-8", errors="replace")[:4096] if snapshot else "<unreadable>"
            raise RuntimeError(
                "Asset symbol scan is already locked for this root/build: %s; owner=%s. "
                "The owner is active or could not be proven inactive; its lock was preserved."
                % (lock_path, owner)
            ) from error

    try:
        yield
    finally:
        # Do not remove a lock that an operator replaced while this scan ran.
        try:
            snapshot = locks._lock_snapshot(lock_path)
            if snapshot is not None and snapshot[0] == payload:
                lock_path.unlink()
        except OSError:
            pass


def _recoverable_scan_lock(raw: bytes, metadata: os.stat_result) -> bool:
    """Accept v2 process identities or the old scanner's exact legacy format.

    Legacy timestamps cannot prove PID reuse. A live or inaccessible legacy PID
    stays locked; only an OS-confirmed dead PID authorizes archiving that file.
    """

    from .. import active_projects as locks

    if locks._recoverable_exclusive_project_lock(raw, metadata):
        return True
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"),
                           object_pairs_hook=locks._json_object_without_duplicate_keys)
    except (UnicodeError, ValueError):
        return False
    if not isinstance(value, dict) or set(value) != {"pid", "createdUnixNs", "token"}:
        return False
    pid, created, token = value["pid"], value["createdUnixNs"], value["token"]
    if (type(pid) is not int or not 0 < pid <= 0xFFFFFFFF
            or type(created) is not int or created <= 0 or not isinstance(token, str)):
        return False
    token_parts = re.fullmatch(r"([1-9][0-9]*):([1-9][0-9]*):[0-9a-f]{32}", token)
    if token_parts is None or int(token_parts.group(1)) != pid:
        return False
    return locks._process_lock_owner_state(pid)[0] == "dead"


def scan_asset_root(
    asset_root: str | Path,
    database_path: str | Path,
    build_id: str = "",
    max_files: int = 200000,
    max_file_size_mb: int = 2048,
) -> dict:
    root = Path(asset_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Asset root is not a directory: %s" % root)
    if max_files <= 0:
        raise ValueError("max_files must be positive")
    if max_file_size_mb <= 0:
        raise ValueError("max_file_size_mb must be positive")

    normalized_build = build_id.strip() or "unknown-build"
    scan_key = "%s\n%s" % (os.path.normcase(str(root)), normalized_build)
    scan_id = hashlib.sha256(scan_key.encode("utf-8")).hexdigest()[:24]
    resolved_database = Path(database_path).expanduser().resolve()
    lock_path = resolved_database.parent / (scan_id + ".scan.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive_scan_lock(lock_path):
        return _scan_asset_root_locked(
            root,
            resolved_database,
            normalized_build,
            scan_id,
            max_files,
            max_file_size_mb,
        )


def ingest_trusted_provider_result(
    result: TrustedProviderResult,
    asset_root: str | Path,
    database_path: str | Path,
    build_id: str = "",
    max_files: int = 200000,
    max_file_size_mb: int = 2048,
) -> dict:
    """Atomically rescan an asset root and ingest one attested provider result."""

    # This capability check intentionally precedes path handling, DB creation,
    # or any other observable ingestion action.
    if not verify_attestation(result):
        raise ProviderIntegrityError("Trusted provider result attestation is invalid")

    root = Path(asset_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Asset root is not a directory: %s" % root)
    if max_files <= 0:
        raise ValueError("max_files must be positive")
    if max_file_size_mb <= 0:
        raise ValueError("max_file_size_mb must be positive")
    normalized_build = build_id.strip() or "unknown-build"
    attestation = result.attestation
    expected_root_hash = hashlib.sha256(
        os.path.normcase(str(root)).encode("utf-8")
    ).hexdigest()
    if not secrets.compare_digest(
        expected_root_hash, attestation.asset_root_sha256
    ):
        raise ProviderIntegrityError(
            "Trusted provider result belongs to a different asset root"
        )
    if attestation.build_id != normalized_build:
        raise ProviderIntegrityError(
            "Trusted provider result belongs to a different build"
        )

    provider_records, provider_audit = _trusted_provider_records(result)
    scan_key = "%s\n%s" % (os.path.normcase(str(root)), normalized_build)
    scan_id = hashlib.sha256(scan_key.encode("utf-8")).hexdigest()[:24]
    resolved_database = Path(database_path).expanduser().resolve()
    lock_path = resolved_database.parent / (scan_id + ".scan.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive_scan_lock(lock_path):
        return _scan_asset_root_locked(
            root,
            resolved_database,
            normalized_build,
            scan_id,
            max_files,
            max_file_size_mb,
            trusted_provider_records=provider_records,
            trusted_provider_audit=provider_audit,
        )


def _scan_asset_root_locked(
    root: Path,
    resolved_database: Path,
    normalized_build: str,
    scan_id: str,
    max_files: int,
    max_file_size_mb: int,
    trusted_provider_records: list[dict] | None = None,
    trusted_provider_audit: dict | None = None,
) -> dict:
    store = SymbolStore(resolved_database)
    initial = {
        "schema": SYMBOL_SCHEMA,
        "scanId": scan_id,
        "root": str(root),
        "buildId": normalized_build,
        "status": "scanning",
    }
    if trusted_provider_audit is not None:
        initial["trustedProvider"] = trusted_provider_audit
    store.replace_asset_scan(scan_id, str(root), normalized_build, initial)
    previous_assets = store.list_asset_shaders(scan_id)
    previous_manifest_paths = {
        str(item.get("metadata", {}).get("_symbolSource", {}).get("manifest", ""))
        for item in previous_assets
        if str(item.get("provenance", "")).startswith("manifest")
    }
    previous_manifest_paths.discard("")

    extension_counts: Counter[str] = Counter()
    files_seen = 0
    bytes_seen = 0
    skipped_large = 0
    errors: list[dict] = []
    had_errors = False
    shader_ids: list[int] = []
    manifests = 0
    text_shader_files: list[str] = []
    max_bytes = max_file_size_mb * 1024 * 1024

    def record_error(path: Path, error: object) -> None:
        nonlocal had_errors
        had_errors = True
        if len(errors) < 1000:
            errors.append({"path": str(path), "error": str(error)})

    def traversal_error(error: OSError) -> None:
        record_error(Path(getattr(error, "filename", None) or root), error)

    for path in _iter_files(root, max_files, traversal_error):
        files_seen += 1
        # A database located under the asset root is live output, not an asset.
        # Its sidecar files can change while scanning and must also be ignored.
        if path.resolve() in (
            resolved_database,
            Path(str(resolved_database) + "-wal"),
            Path(str(resolved_database) + "-shm"),
            Path(str(resolved_database) + "-journal"),
            resolved_database.parent / (scan_id + ".scan.lock"),
        ):
            continue
        suffix = path.suffix.lower()
        extension_counts[suffix or "<none>"] += 1
        try:
            size = path.stat().st_size
        except OSError as error:
            record_error(path, error)
            continue
        bytes_seen += size

        if suffix in TEXT_SHADER_EXTENSIONS and len(text_shader_files) < 5000:
            text_shader_files.append(str(path.relative_to(root)))

        if suffix == ".json":
            records, recognized_manifest, manifest_errors = _manifest_records(
                path, root, normalized_build, max_bytes
            )
            relative_path = str(path.relative_to(root))
            if recognized_manifest:
                manifests += 1
            if recognized_manifest or relative_path in previous_manifest_paths:
                for error in manifest_errors:
                    record_error(path, error)
            for record in records:
                try:
                    shader_ids.append(store.add_asset_shader(scan_id, record))
                except (KeyError, TypeError, ValueError, sqlite3.Error) as error:
                    record_error(path, error)

        if size < 32:
            continue
        if size > max_bytes:
            skipped_large += 1
            continue

        try:
            for container in scan_file(path):
                relative = str(path.relative_to(root))
                display_name = path.stem
                if container.file_offset:
                    display_name += "@0x%x" % container.file_offset
                record = container.as_dict()
                record.update(
                    {
                        "relativePath": relative,
                        "displayName": display_name,
                        "stage": "",
                        "entryPoint": "",
                        "layoutHash": "",
                        "metadata": {
                            "container": container.as_dict(),
                            "sourceFileSize": size,
                        },
                        "provenance": "embedded-container",
                    }
                )
                shader_ids.append(store.add_asset_shader(scan_id, record))
        except (OSError, ValueError) as error:
            record_error(path, error)

    provider_ids: list[int] = []
    for record in trusted_provider_records or []:
        try:
            provider_id = store.add_asset_shader(scan_id, record)
            provider_ids.append(provider_id)
            shader_ids.append(provider_id)
        except (KeyError, TypeError, ValueError, sqlite3.Error) as error:
            record_error(root, error)

    partial_reasons: list[str] = []
    if files_seen >= max_files:
        partial_reasons.append("max-files-reached")
    if skipped_large:
        partial_reasons.append("large-files-skipped")
    if had_errors:
        partial_reasons.append("read-or-parse-errors")
    is_partial = bool(partial_reasons)
    if not is_partial:
        store.prune_asset_shaders(scan_id, shader_ids)
    unique_shader_count = len(store.list_asset_shaders(scan_id))
    summary = {
        "schema": SYMBOL_SCHEMA,
        "scanId": scan_id,
        "root": str(root),
        "buildId": normalized_build,
        "databasePath": str(resolved_database),
        "status": "partial" if is_partial else "complete",
        "filesSeen": files_seen,
        "bytesSeen": bytes_seen,
        "skippedLargeFiles": skipped_large,
        "shaderRecords": unique_shader_count,
        "recordsSeenThisRun": len(set(shader_ids)),
        "previousShaderRecords": len(previous_assets),
        "prunedMissingRecords": not is_partial,
        "partialReasons": partial_reasons,
        "manifestFiles": manifests,
        "textShaderFiles": text_shader_files,
        "extensionCounts": dict(extension_counts.most_common()),
        "errors": errors,
        "limits": {
            "maxFiles": max_files,
            "maxFileSizeMB": max_file_size_mb,
        },
    }
    if trusted_provider_audit is not None:
        summary["trustedProvider"] = {
            **trusted_provider_audit,
            "recordsStoredThisRun": len(set(provider_ids)),
        }
    store.update_asset_scan_summary(scan_id, summary)
    return summary
