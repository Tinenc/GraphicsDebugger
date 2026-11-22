"""Display-only semantic projections of immutable constant-buffer byte lanes.

Names express the Agent's evidence, never ABI authority. Physical components
are checked against captured reflection independently of semantic confidence.
"""

from __future__ import annotations

from collections.abc import Mapping
from bisect import bisect_left
import re
from typing import Any

# A 64 KiB CBuffer can contain 16384 independently meaningful 32-bit fields.
# Keep the serialized-store byte limit as a second bound on verbose records.
MAX_FIELDS = 16384
_TYPE = re.compile(r"^(float|int|uint)([234])?$")


def reflected_component_offsets(leaf: Mapping[str, Any]) -> list[int]:
    """Return physical lane starts; matrix stride padding is not a component."""
    start, size = leaf.get("offset"), leaf.get("byteSize")
    if (type(start) is not int or type(size) is not int or start < 0
            or start % 4 or size < 4 or size > 65536):
        return []
    relative = leaf.get("componentOffsets")
    if relative is None:
        return list(range(start, start + size - 3, 4))
    if (not isinstance(relative, list) or not 1 <= len(relative) <= 16
            or any(type(offset) is not int or offset < 0 or offset % 4
                   or offset + 4 > size for offset in relative)
            or len(set(relative)) != len(relative)):
        return []
    return [start + offset for offset in relative]


def clean_fields(fields: Any) -> list[dict[str, Any]]:
    """Validate portable view syntax; reflection validation happens separately."""
    from .aliases import is_valid_alias

    if not isinstance(fields, list) or not 1 <= len(fields) <= MAX_FIELDS:
        raise ValueError("semantic fields must contain 1-%d fields" % MAX_FIELDS)
    result = []
    names, offsets = set(), set()
    for index, raw in enumerate(fields):
        if not isinstance(raw, Mapping):
            raise ValueError("each semantic field must be an object")
        name, type_text = raw.get("name"), raw.get("type")
        match = _TYPE.fullmatch(type_text) if isinstance(type_text, str) else None
        # This is a panel label, not an executable Shader identifier. Keep the
        # same printable-label contract as user aliases and the native reader.
        # Chinese names, spaces and role/index separators never alter the ABI.
        if not isinstance(name, str) or not is_valid_alias(name):
            raise ValueError("semantic field %d name must contain 1-64 printable display characters without leading/trailing whitespace" % index)
        if not match:
            raise ValueError("semantic field %d (%s) type must be a float/int/uint scalar or vector" % (index, name))
        if name in names:
            raise ValueError("duplicate semantic field name: " + name)
        names.add(name)
        components = raw.get("components")
        if not isinstance(components, list) or len(components) != int(match.group(2) or 1):
            raise ValueError("semantic field components must match its declared vector width")
        clean_components = []
        for item in components:
            offset = item.get("offset") if isinstance(item, Mapping) else None
            if type(offset) is not int or offset < 0 or offset % 4 or offset > 16 * 1024 * 1024:
                raise ValueError("semantic component needs an aligned non-negative 32-bit byte offset")
            if offset in offsets:
                raise ValueError("semantic components overlap at byte offset %d" % offset)
            offsets.add(offset)
            clean_components.append({"offset": offset})
        status, provenance = raw.get("status"), raw.get("provenance")
        if status not in {"confirmed", "inferred"} or provenance not in {"asset", "shader", "combined"}:
            raise ValueError("semantic fields require explicit confirmed/inferred status and asset/shader/combined provenance")
        evidence, description = raw.get("evidence"), raw.get("description", "")
        for text, label, required in ((evidence, "evidence", True), (description, "description", False)):
            if (not isinstance(text, str) or len(text) > 2000 or "\x00" in text
                    or (required and not text.strip())):
                raise ValueError("semantic field %s must be bounded text%s" % (label, " and non-empty" if required else ""))
        result.append({"name": name, "type": type_text, "components": clean_components,
                       "status": status, "provenance": provenance,
                       "evidence": evidence.strip(), "description": description.strip()})
    return result


def normalize_fields(identity: Mapping[str, Any], fields: Any) -> dict[tuple[int, int], list[dict[str, Any]]]:
    """Bind semantic fields to unique reflected lanes without changing layout."""
    if not isinstance(fields, list) or len(fields) > MAX_FIELDS:
        raise ValueError("semantic_fields_json must be an array with at most %d fields" % MAX_FIELDS)
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for item in fields:
        if not isinstance(item, Mapping):
            raise ValueError("semantic field must be an object")
        bind, space = item.get("bind"), item.get("space", 0)
        if type(bind) is not int or bind < 0 or type(space) is not int or space < 0:
            raise ValueError("semantic field requires non-negative integer bind and space")
        grouped.setdefault((bind, space), []).append(dict(item))
    for key, values in grouped.items():
        blocks = [block for block in identity.get("blocks", []) if isinstance(block, Mapping)
                  and (block.get("bind"), block.get("space", 0)) == key]
        if len(blocks) != 1:
            raise ValueError("semantic field has no unique current reflected block")
        block = blocks[0]
        cleaned = clean_fields(values)
        # Resolve only requested lanes once. Repeatedly walking a large reflected
        # instance array for every vector component made publication quadratic.
        requested = sorted({component["offset"] for field in cleaned for component in field["components"]})
        lane_leaves = {}
        for leaf in block.get("leaves", []):
            if not isinstance(leaf, Mapping):
                continue
            start, size = leaf.get("offset"), leaf.get("byteSize")
            if type(start) is not int or type(size) is not int or size < 4:
                continue
            valid_offsets = set(reflected_component_offsets(leaf))
            for offset in requested[bisect_left(requested, start):bisect_left(requested, start + size - 3)]:
                if offset not in valid_offsets:
                    continue
                lane_leaves[offset] = None if offset in lane_leaves else leaf
        for field in cleaned:
            scalar = _TYPE.fullmatch(field["type"]).group(1)
            for component in field["components"]:
                offset = component["offset"]
                leaf = lane_leaves.get(offset)
                if leaf is None or (block.get("byteSize", 0) > 0 and offset + 4 > block["byteSize"]):
                    raise ValueError("semantic component has no unique reflected 32-bit lane at byte offset %d" % offset)
                if (leaf.get("semanticMappingAvailable") is False or
                        (offset - leaf["offset"]) % 4 or leaf.get("componentByteSize", 4) != 4):
                    raise ValueError("semantic component does not address a reflected 32-bit lane")
                base = str(leaf.get("baseType") or "").lower().split(".")[-1]
                if base and base != scalar:
                    raise ValueError("semantic field type cannot reinterpret reflected %s as %s" % (base, scalar))
        grouped[key] = cleaned
    return grouped


def select_physical_layouts(identity: Mapping[str, Any], fields: Any) -> tuple[dict, dict, list]:
    """Use verified active typed lanes when stripped original placeholders fail.

    The active reflection comes only from the Bridge's checked replacement
    binding. This does not reinterpret a typed capture or change an ABI rule.
    """
    from .aliases import has_placeholder_members

    if not isinstance(fields, list) or len(fields) > MAX_FIELDS:
        raise ValueError("semantic_fields_json must be an array with at most %d fields" % MAX_FIELDS)
    grouped = {}
    for field in fields:
        if not isinstance(field, Mapping):
            raise ValueError("semantic field must be an object")
        bind, space = field.get("bind"), field.get("space", 0)
        if type(bind) is not int or bind < 0 or type(space) is not int or space < 0:
            raise ValueError("semantic field requires non-negative integer bind and space")
        grouped.setdefault((bind, space), []).append(field)
    original, active, unavailable = {}, {}, []
    for key, values in grouped.items():
        try:
            original.update(normalize_fields(identity, values))
            continue
        except ValueError as error:
            original_error = error
        blocks = [block for block in identity.get("blocks", []) if isinstance(block, Mapping)
                  and (block.get("bind"), block.get("space", 0)) == key]
        active_sha = str(identity.get("activeDisplayShaderRawSha256") or "")
        members = blocks[0].get("members") or [] if len(blocks) == 1 else []
        # A complete unknown[N] storage array can be labelled "annotated" by
        # the shape checker. It still has no source types. Inspect its names,
        # including leaves, instead of treating that shape label as authority.
        leaves = blocks[0].get("leaves") or [] if len(blocks) == 1 else []
        if (len(blocks) != 1
                or not members or not all(has_placeholder_members([member]) for member in members)
                or not all(isinstance(leaf, Mapping) and re.fullmatch(
                    r"unknown(?:\[\d+\])?", str(leaf.get("path") or ""), re.I) for leaf in leaves)
                or not re.fullmatch(r"[0-9a-f]{64}", active_sha)
                or active_sha == identity.get("rawSha256")):
            raise original_error
        bound = normalize_fields({"blocks": identity.get("activeDisplayBlocks") or []}, values)
        original_size = blocks[0].get("byteSize", 0)
        if type(original_size) is not int or original_size <= 0 or any(
                component["offset"] + 4 > original_size
                for field in bound[key] for component in field["components"]):
            raise ValueError("active semantic fields exceed the captured physical CBuffer extent")
        active.update(bound)
        unavailable.append({"bind": key[0], "space": key[1], "reason": str(original_error),
                            "basis": "verified-active-reflection"})
    return original, active, unavailable
