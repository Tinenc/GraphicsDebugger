"""Resolve target parameters from captured physical constant-buffer storage.

Names are a selector and a display hint, never the value authority. Reflected
offsets/strides describe all APIs; no Direct3D register packing is assumed here.
Missing data is a reported restoration gap, so an asset-only package remains
useful. Incorrect or ambiguous supplied mappings fail before publication.
"""
from __future__ import annotations

import hashlib
import base64
import json
import math
from pathlib import Path
import re
import struct
from typing import Any, Mapping


SCHEMA = "bread-cbuffer-transfer/v1"
MAX_BYTES = 64 * 1024 * 1024
MAX_FIELDS = 65536
_STAGES = {"vertex": "vs", "fragment": "ps", "pixel": "ps", "compute": "cs",
           "geometry": "gs", "hull": "hs", "domain": "ds", "tesscontrol": "hs",
           "tesseval": "ds", "fs": "ps", "vert": "vs", "frag": "ps",
           "tcs": "hs", "tes": "ds", "as": "task", "ms": "mesh"}
_SCALARS = {"float": ("f", 4), "half": ("e", 2), "double": ("d", 8),
            "int": ("i", 4), "uint": ("I", 4), "bool": ("I", 4),
            "short": ("h", 2), "ushort": ("H", 2), "byte": ("b", 1),
            "ubyte": ("B", 1), "long": ("q", 8), "ulong": ("Q", 8)}


class CBufferTransferError(ValueError):
    pass


def _stage(value: Any) -> str:
    text = str(value or "").lower()
    return _STAGES.get(text, text)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _read(root: Path, record: Mapping, label: str) -> bytes:
    relative = record.get("path")
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise CBufferTransferError(label + " path is invalid")
    parts = relative.split("/")
    if any(part in ("", ".", "..") or ":" in part for part in parts):
        raise CBufferTransferError(label + " path escaped the source package")
    path = root.joinpath(*parts)
    for part in (path, *path.parents):
        if part == root.parent:
            break
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise CBufferTransferError(label + " path is redirected")
    if not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise CBufferTransferError(label + " file is missing or too large")
    data = path.read_bytes()
    if len(data) > MAX_BYTES or hashlib.sha256(data).hexdigest() != record.get("sha256"):
        raise CBufferTransferError(label + " hash changed")
    if "size" in record and len(data) != record["size"]:
        raise CBufferTransferError(label + " size changed")
    return data


def _scalar(field: Mapping) -> str:
    value = str(field.get("baseType") or field.get("type") or "").lower().split(".")[-1]
    value = re.sub(r"[0-9].*$", "", value)
    return {"sint": "int", "sbyte": "byte", "sshort": "short", "slong": "long"}.get(value, value)


def _integer(value: Any, default: int = 0) -> int:
    return value if type(value) is int and value >= 0 else default


def _leaves(fields: list, base: int = 0, prefix: str = "", depth: int = 0):
    if depth > 32:
        raise CBufferTransferError("CBuffer layout exceeds the nesting limit")
    for item in fields:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or item.get("path") or "")
        path = prefix + "." + name if prefix else str(item.get("path") or name)
        relative = item.get("byteOffset", item.get("offset"))
        if type(relative) is not int or relative < 0:
            yield {"path": path, "issue": "cbuffer_layout_offset_missing"}
            continue
        start = base + relative
        elements = max(1, _integer(item.get("elements"), 1))
        stride = _integer(item.get("arrayByteStride"))
        if elements > MAX_FIELDS or (elements > 1 and not stride):
            yield {"path": path, "issue": "cbuffer_array_stride_unavailable"}
            continue
        members = item.get("members") or []
        if members:
            for index in range(elements):
                child_prefix = path + ("[%d]" % index if elements > 1 else "")
                yield from _leaves(members, start + index * stride, child_prefix, depth + 1)
            continue
        scalar = _scalar(item)
        if scalar not in _SCALARS or item.get("bitFieldSize"):
            yield {"path": path, "issue": "cbuffer_scalar_type_unsupported"}
            continue
        size = _SCALARS[scalar][1]
        rows = max(1, _integer(item.get("rows"), 1))
        columns = max(1, _integer(item.get("columns"), 1))
        # Legacy evidence may have a type string without rows/columns.
        type_match = re.fullmatch(r"(?:float|half|double|int|uint|bool)([1-4])?(?:x([1-4]))?", str(item.get("type", "")))
        if type_match and "rows" not in item and "columns" not in item:
            rows, columns = ((int(type_match[1]), int(type_match[2])) if type_match[2]
                             else (1, int(type_match[1] or 1)))
        if rows * columns > 16:
            yield {"path": path, "issue": "cbuffer_shape_unsupported"}
            continue
        matrix = rows > 1 and columns > 1
        matrix_stride = _integer(item.get("matrixByteStride"))
        if matrix and not matrix_stride:
            yield {"path": path, "issue": "cbuffer_matrix_stride_unavailable"}
            continue
        offsets = []
        for index in range(elements):
            array_base = start + index * stride
            for row in range(rows):
                for column in range(columns):
                    delta = ((row * matrix_stride + column * size) if item.get("rowMajor")
                             else (column * matrix_stride + row * size)) if matrix else (row * columns + column) * size
                    offsets.append(array_base + delta)
        extent = item.get("byteSize")
        if type(extent) is int and extent > 0 and any(offset + size > start + extent for offset in offsets):
            yield {"path": path, "issue": "cbuffer_shape_exceeds_reflected_extent"}
            continue
        yield {"path": path, "displayName": item.get("displayName", path),
               "type": scalar + ("%dx%d" % (rows, columns) if matrix else str(rows * columns) if rows * columns > 1 else "")
                       + ("[%d]" % elements if elements > 1 else ""),
               "scalarType": scalar, "componentByteSize": size, "offsets": offsets,
               "rows": rows, "columns": columns, "elements": elements,
               "rowMajor": bool(item.get("rowMajor")), "matrix": matrix,
               "required": item.get("used") is not False and item.get("staticallyUnused") is not True,
               "declaredByteSize": item.get("byteSize"), "start": start}


def _live_values(block: Mapping) -> dict[str, Mapping]:
    result = {}
    def walk(values, prefix=""):
        for value in values:
            if not isinstance(value, Mapping):
                continue
            name = str(value.get("name") or "")
            path = (name if not prefix or name.startswith(prefix + "[") else
                    prefix + name if name.startswith("[") else prefix + "." + name)
            if value.get("members"):
                walk(value["members"], path)
            else:
                result[path] = value
    walk(block.get("variables") or [])
    return result


def _values(leaf: Mapping, raw: bytes | None, live: Mapping | None):
    scalar, offsets = leaf["scalarType"], leaf["offsets"]
    code, size = _SCALARS[scalar]
    if raw is not None:
        if any(offset < 0 or offset + size > len(raw) for offset in offsets):
            return None, "cbuffer_value_outside_captured_window"
        values = [struct.unpack_from("<" + code, raw, offset)[0] for offset in offsets]
    elif live is not None:
        if scalar not in ("float", "int", "uint", "bool") and "value" not in live:
            return None, "cbuffer_live_scalar_encoding_unavailable"
        # Older Bridge values can omit trailing float zeros. Their declared
        # reflected shape is required before restoring those omitted zeros.
        key = "float" if scalar in ("float", "half", "double") else "uint" if scalar in ("uint", "bool", "ushort", "ubyte", "ulong") else "int"
        value = live.get("value", live.get(key, live.get({"float": "fvalue", "uint": "uvalue", "int": "ivalue"}[key])))
        if value is None and live.get("rows") and live.get("columns") and not live.get("floatNonFinite"):
            value = []
        values = list(value) if isinstance(value, list) else [value] if type(value) in (int, float, bool) else None
        if values is not None and len(values) < len(offsets) and "value" not in live:
            if _integer(live.get("rows")) * _integer(live.get("columns")) == len(offsets):
                values += [0.0] * (len(offsets) - len(values))
        if values is None or len(values) != len(offsets):
            return None, "cbuffer_live_value_shape_unavailable"
    else:
        return None, "cbuffer_value_unavailable"
    if any(type(value) not in (int, float, bool) or not math.isfinite(value) for value in values):
        return None, "cbuffer_non_finite_value"
    if scalar == "bool":
        values = [bool(value) for value in values]
    return values, None


def _active_block(block: Mapping, shader: Mapping, organized: Mapping | None):
    identity = (organized or {}).get("cbufferIdentity") or {}
    active_sha = identity.get("activeDisplayShaderRawSha256")
    if (identity.get("schema") != "replaylab-cbuffer-alias-identity/v1"
            or identity.get("rawSha256") != shader.get("rawSha256")
            or _stage(identity.get("stage")) != _stage(shader.get("stage"))
            or not isinstance(active_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", active_sha)
            or active_sha == identity.get("rawSha256")
            or active_sha != identity.get("activeReplacementShaderRawSha256")):
        return None
    key = (block.get("bind"), block.get("space", block.get("registerSpace", 0)))
    matches = [item for item in identity.get("activeDisplayBlocks", []) if
               (item.get("bind"), item.get("space", 0)) == key]
    if (len(matches) != 1 or matches[0].get("memberMappingComplete") is not True
            or matches[0].get("memberMappingIssues") or not matches[0].get("leaves")):
        return None
    candidate = dict(matches[0], rawSha256=active_sha)
    extent = _integer(block.get("byteSize", block.get("size")))
    if extent <= 0 or _integer(candidate.get("byteSize")) > extent:
        return None
    return candidate


def _active_leaves(active: Mapping, extent: int):
    """Expand verified active typed lanes; never infer matrix padding or shape."""
    for leaf in active.get("leaves", []):
        scalar, start, size = _scalar(leaf), leaf.get("offset"), leaf.get("byteSize")
        if (scalar not in _SCALARS or leaf.get("semanticMappingAvailable") is False
                or type(start) is not int or type(size) is not int or start < 0 or size <= 0
                or start + size > extent):
            yield {"path": leaf.get("path", ""), "issue": "cbuffer_active_layout_unavailable"}
            continue
        width = _SCALARS[scalar][1]
        if leaf.get("componentByteSize", width) != width:
            yield {"path": leaf.get("path", ""), "issue": "cbuffer_active_layout_type_mismatch"}
            continue
        offsets = leaf.get("componentOffsets", list(range(0, size, width)))
        if (not isinstance(offsets, list) or not offsets or len(set(offsets)) != len(offsets)
                or any(type(offset) is not int or offset < 0 or offset + width > size for offset in offsets)):
            yield {"path": leaf.get("path", ""), "issue": "cbuffer_active_layout_offsets_invalid"}
            continue
        for index, offset in enumerate(offsets):
            name = str(leaf.get("path", "")) + (".component%d" % index if len(offsets) > 1 else "")
            yield {"path": name, "displayName": name, "type": scalar, "scalarType": scalar,
                   "componentByteSize": width, "offsets": [start + offset], "rows": 1,
                   "columns": 1, "elements": 1, "matrix": False, "required": True,
                   "layoutAuthority": "verified-active-reflection"}


def _semantic_view(block: Mapping, shader: Mapping, organized: Mapping | None):
    if not organized or not organized.get("sourceSha256"):
        return None
    from .shader_workspace import aliases
    members = [{"name": str(field.get("name", "")), "offset": field.get("byteOffset", field.get("offset"))}
               for field in block.get("layoutFields", []) if isinstance(field, Mapping)]
    key = aliases.layout_sha256(str(block.get("name", "")), members)
    alias_stage = {"vs": "vertex", "ps": "fragment", "cs": "compute", "gs": "geometry",
                   "hs": "hull", "ds": "domain"}.get(_stage(shader.get("stage")), shader.get("stage"))
    scope = {"shaderRawSha256": shader.get("rawSha256"), "stage": alias_stage,
             "bind": block.get("bind"), "space": block.get("space", block.get("registerSpace", 0)),
             "layoutSha256": key}
    view = aliases.read_semantic_view(key, shader_scope=scope)
    if view and view.get("sourceSha256") == organized.get("sourceSha256"):
        return view
    active = _active_block(block, shader, organized)
    if active:
        key = aliases.layout_sha256(active["block"], active["members"])
        view = aliases.read_semantic_view(key, shader_scope=dict(scope,
            shaderRawSha256=active["rawSha256"], layoutSha256=key))
        if view and view.get("sourceSha256") == organized.get("sourceSha256"):
            return view
    return None


def _block_identity(stage: str, index: int, block: Mapping):
    identity = dict(block.get("physicalIdentity") or {
        "stage": stage, "constantBlockIndex": index, "bind": block.get("bind"),
        "registerSpace": block.get("registerSpace", block.get("space")),
        "bufferResourceId": block.get("bufferResourceId", block.get("bufferId", 0)),
        "byteOffset": block.get("byteOffset")})
    return identity, _digest(identity)


def _catalog(bundle: Mapping, spec: Mapping, root: Path):
    fields, gaps, required, unused = [], [], set(), set()
    organized = {_stage(item.get("stage")): item for item in spec.get("shader", {}).get("organizedSources", [])
                 if isinstance(item, Mapping)}
    for shader in bundle.get("shaders", []):
        if not isinstance(shader, Mapping):
            continue
        stage = _stage(shader.get("stage"))
        evidence = json.loads(_read(root, {"path": shader.get("evidencePath"),
                                         "sha256": shader.get("evidenceSha256")}, "Shader evidence"))
        for index, block in enumerate(evidence.get("constantBuffers", [])):
            if not isinstance(block, Mapping):
                continue
            identity, physical_id = _block_identity(str(shader.get("stage", "")), index, block)
            if block.get("physicalId") and block["physicalId"] != physical_id:
                raise CBufferTransferError("CBuffer physical identity hash changed")
            binding = {"stage": stage, "physicalId": physical_id, "physicalIdentity": identity,
                       "block": str(block.get("name", "")), "blockDisplayName": block.get("displayName", block.get("name", ""))}
            raw_record = block.get("raw") or {}
            raw = _read(root, raw_record, "CBuffer bytes") if raw_record.get("status") == "captured" else None
            value_evidence = [dict(raw_record)] if raw is not None else []
            # A draw can alias a CBuffer through a writable storage view. Use
            # the pre-action window for overlapping lanes, never its output.
            buffer_id = identity.get("bufferResourceId")
            block_offset = _integer(identity.get("byteOffset"))
            block_size = len(raw) if raw is not None else _integer(block.get("byteSize", block.get("size")))
            before_applied = False
            for captured_buffer in bundle.get("buffers", []):
                if (not buffer_id or captured_buffer.get("resourceId") != buffer_id
                        or captured_buffer.get("capturePhase") != "before-action"
                        or captured_buffer.get("status") != "captured"):
                    continue
                offset, size = captured_buffer.get("byteOffset"), captured_buffer.get("byteSize")
                if type(offset) is not int or type(size) is not int:
                    continue
                start, end = max(block_offset, offset), min(block_offset + block_size, offset + size)
                if start >= end:
                    continue
                before = _read(root, captured_buffer.get("raw") or {}, "pre-action CBuffer alias")
                if len(before) != size:
                    raise CBufferTransferError("pre-action CBuffer alias window length changed")
                if raw is None and (start != block_offset or end != block_offset + block_size):
                    continue
                patched = bytearray(raw if raw is not None else b"\0" * block_size)
                patched[start - block_offset:end - block_offset] = before[start - offset:end - offset]
                raw = bytes(patched)
                before_applied = True
                value_evidence.append({**captured_buffer["raw"], "capturePhase": "before-action",
                                       "byteOffset": offset, "selectedEventId": captured_buffer.get("selectedEventId")})
            live = _live_values(block) if block.get("variablesStatus") in ("available", "captured") else {}
            layouts = []
            for layout in block.get("layoutFields") or []:
                layout = dict(layout)
                captured_variable = live.get(layout.get("path", layout.get("name", "")), {})
                if not layout.get("type") and captured_variable.get("type"):
                    layout["type"] = captured_variable["type"]
                layouts.append(layout)
            leaves = list(_leaves(layouts))
            active = _active_block(block, shader, organized.get(stage))
            placeholder = bool(layouts) and all(re.fullmatch(r"unknown(?:\[\d+\])?", str(item.get("name", "")), re.I) for item in layouts)
            if active and (placeholder or not leaves):
                leaves = list(_active_leaves(active, _integer(block.get("byteSize", block.get("size")))))
            if not leaves and _integer(block.get("byteSize", block.get("size"))) > 0:
                gaps.append({"code": "cbuffer_layout_unavailable", **binding})
            lanes = {}
            block_fields = []
            for leaf in leaves:
                if "issue" in leaf:
                    gaps.append({"code": leaf["issue"], "variable": leaf["path"], **binding})
                    continue
                live_value = live.get(leaf["path"])
                if raw is None and leaf["elements"] > 1 and live_value is None:
                    width = leaf["rows"] * leaf["columns"]
                    combined = []
                    for index in range(leaf["elements"]):
                        element = dict(leaf, offsets=leaf["offsets"][index * width:(index + 1) * width], elements=1)
                        element_values, element_issue = _values(element, None, live.get(leaf["path"] + "[%d]" % index))
                        if element_issue:
                            combined = []
                            break
                        combined.extend(element_values)
                    if combined:
                        live_value = {"value": combined}
                values, issue = _values(leaf, raw, live_value)
                record = {**binding, **leaf, "value": values[0] if values and len(values) == 1 else values,
                          "valueSource": "captured-bytes-before-action" if before_applied else "captured-bytes" if raw is not None else "captured-live-values",
                          "valueEvidence": value_evidence}
                record["fieldId"] = _digest({"physicalId": physical_id, "offsets": leaf["offsets"], "type": leaf["type"]})
                if issue and leaf["required"]:
                    gaps.append({"code": issue, "fieldId": record["fieldId"], **binding})
                block_fields.append(record)
                for offset in leaf["offsets"]:
                    lane = (physical_id, offset, leaf["componentByteSize"])
                    if leaf["required"]:
                        required.add(lane)
                    if offset in lanes:
                        gaps.append({"code": "cbuffer_overlapping_reflection", "offset": offset, **binding})
                    lanes[offset] = (leaf["scalarType"], leaf["componentByteSize"],
                                     values[leaf["offsets"].index(offset)] if values else None)
            view = _semantic_view(block, shader, organized.get(stage))
            if view:
                for semantic in view.get("fields", []):
                    scalar = _scalar(semantic)
                    offsets = [component.get("offset") for component in semantic.get("components", [])]
                    if not offsets or any(offset not in lanes or lanes[offset][:2] != (scalar, 4) for offset in offsets):
                        gaps.append({"code": "cbuffer_semantic_layout_mismatch", "name": semantic.get("name"), **binding})
                        continue
                    values = [lanes[offset][2] for offset in offsets]
                    block_fields.append({**binding, "path": semantic["name"], "displayName": semantic["name"],
                        "type": semantic["type"], "scalarType": scalar, "componentByteSize": 4,
                        "offsets": offsets, "rows": 1, "columns": len(offsets), "elements": 1,
                        "matrix": False, "required": True, "semanticProjection": True,
                        "semanticSourceSha256": view["sourceSha256"], "semanticEvidence": semantic.get("evidence"),
                        "value": (values[0] if len(values) == 1 else values) if all(value is not None for value in values) else None,
                        "valueSource": "captured-physical-lanes",
                        "valueEvidence": value_evidence,
                        "fieldId": _digest({"physicalId": physical_id, "offsets": offsets, "type": semantic["type"]})})
                for component in (view.get("usageReview") or {}).get("unusedComponents", []):
                    offset, scalar = component.get("offset"), component.get("type")
                    if offset in lanes and lanes[offset][:2] == (scalar, 4):
                        unused.add((physical_id, offset, 4))
                wholly_unused = {field["fieldId"] for field in block_fields if all(
                    (physical_id, offset, field["componentByteSize"]) in unused for offset in field["offsets"])}
                gaps[:] = [gap for gap in gaps if gap.get("fieldId") not in wholly_unused]
            fields.extend(block_fields)
            if len(fields) > MAX_FIELDS:
                raise CBufferTransferError("CBuffer field count exceeds the transfer limit")
    return fields, gaps, required - unused, unused


def _matches(field: Mapping, source: Any) -> bool:
    names = {field["path"], str(field.get("displayName", "")), field["block"] + "." + field["path"],
             field["stage"] + "." + field["block"] + "." + field["path"], field["fieldId"]}
    if isinstance(source, str):
        return source in names
    if not isinstance(source, Mapping):
        return False
    if source.get("stage") is not None and _stage(source["stage"]) != field["stage"]:
        return False
    for key in ("physicalId", "fieldId"):
        if key in source and source[key] != field[key]:
            return False
    physical = source.get("physicalIdentity")
    if physical is not None and (not isinstance(physical, Mapping) or any(
            field["physicalIdentity"].get(key) != value for key, value in physical.items())):
        return False
    if "block" in source and source["block"] not in (field["block"], field["blockDisplayName"]):
        return False
    variable = source.get("variable", source.get("parameterId", source.get("stableId", source.get("id"))))
    if variable is not None and variable not in names:
        return False
    offsets = source.get("componentOffsets")
    if offsets is not None and offsets != field["offsets"]:
        return False
    offset = source.get("byteOffset", source.get("offset"))
    if offset is not None and offset != field["offsets"][0]:
        return False
    if "type" in source and source["type"] != field["type"]:
        return False
    return any(key in source for key in ("physicalId", "physicalIdentity", "fieldId", "variable", "parameterId", "stableId", "id", "byteOffset", "offset", "componentOffsets"))


def _same_value(proposed: Any, captured: Any, scalar: str) -> bool:
    left = proposed if isinstance(proposed, list) else [proposed]
    right = captured if isinstance(captured, list) else [captured]
    if len(left) != len(right):
        return False
    code = _SCALARS[scalar][0]
    try:
        if scalar == "bool":
            return all(type(value) is bool and value == expected for value, expected in zip(left, right))
        return all(type(value) in (float, int) and math.isfinite(value)
                   and struct.pack("<" + code, value) == struct.pack("<" + code, expected)
                   for value, expected in zip(left, right))
    except (struct.error, OverflowError, TypeError):
        return False


def _physical_projection(fields: list, source: Any):
    """Group explicitly selected typed lanes without guessing their meaning."""
    if (not isinstance(source, Mapping) or not source.get("physicalId")
            or not isinstance(source.get("componentOffsets"), list)
            or any(key in source for key in ("variable", "parameterId", "stableId", "id", "fieldId"))):
        return None
    match = re.fullmatch(r"(float|half|double|int|uint|bool|short|ushort|byte|ubyte|long|ulong)([1-4])?(?:x([1-4]))?(?:\[([1-9][0-9]*)\])?", str(source.get("type", "")))
    if not match:
        return None
    scalar = match[1]
    rows, columns = (int(match[2]), int(match[3])) if match[3] else (1, int(match[2] or 1))
    elements = int(match[4] or 1)
    offsets = source["componentOffsets"]
    if (len(offsets) != rows * columns * elements or len(offsets) > MAX_FIELDS
            or any(type(offset) is not int or offset < 0 for offset in offsets)
            or len(set(offsets)) != len(offsets)):
        raise CBufferTransferError("CBuffer projected shape does not match distinct physical offsets")
    candidates = [field for field in fields if field["physicalId"] == source["physicalId"]
                  and ("stage" not in source or _stage(source["stage"]) == field["stage"])]
    if not candidates:
        return None
    lanes = {}
    for field in candidates:
        values = field["value"] if isinstance(field["value"], list) else [field["value"]]
        for index, offset in enumerate(field["offsets"]):
            value = values[index] if len(values) == len(field["offsets"]) else None
            lane = (field["scalarType"], field["componentByteSize"], value)
            if offset in lanes and lanes[offset] != lane:
                raise CBufferTransferError("CBuffer projected lane has conflicting captured types or values")
            lanes[offset] = lane
    if any(offset not in lanes or lanes[offset][:2] != (scalar, _SCALARS[scalar][1]) for offset in offsets):
        raise CBufferTransferError("CBuffer projected type cannot reinterpret captured physical lanes")
    values = [lanes[offset][2] for offset in offsets]
    return {**candidates[0], "path": "CapturedProjection", "displayName": "CapturedProjection",
            "type": source["type"], "scalarType": scalar, "componentByteSize": _SCALARS[scalar][1],
            "offsets": offsets, "rows": rows, "columns": columns, "elements": elements,
            "matrix": rows > 1 and columns > 1,
            "value": (values[0] if len(values) == 1 else values) if all(value is not None for value in values) else None,
            "fieldId": _digest({"physicalId": source["physicalId"], "offsets": offsets, "type": source["type"]}),
            "projectionAuthority": "explicit-typed-physical-lanes"}


def _physical_payload(field: Mapping, root: Path):
    """Preserve source bits (including non-canonical bool encodings) in buffers."""
    evidence = field.get("valueEvidence") or []
    if not evidence:
        return None
    origin = _integer(field["physicalIdentity"].get("byteOffset"))
    width = field["componentByteSize"]
    sources = [(_integer(record.get("byteOffset"), origin), _read(root, record, "CBuffer raw binding")) for record in evidence]
    chunks = []
    for offset in field["offsets"]:
        chosen = None
        for base, payload in sources:
            relative = origin + offset - base
            if 0 <= relative and relative + width <= len(payload):
                chosen = payload[relative:relative + width]
        if chosen is None:
            return None
        chunks.append(chosen)
    return b"".join(chunks)
def resolve_cbuffer_transfer(bundle: Mapping, spec: Mapping, capture_dir: str | Path) -> dict:
    """Read captured values and return material/runtime bindings plus lane coverage.

    ``source`` supports legacy unique names and physicalId/fieldId, stage,
    byteOffset/componentOffsets and type selectors. A supplied value must encode
    exactly the captured scalar bits; omitting it uses the captured value.
    """
    fields, gaps, required, unused = _catalog(bundle, spec, Path(capture_dir).resolve())
    mappings, properties, runtime, covered = [], [], [], set()
    targets = {}
    for mapping in spec.get("constantBuffers", []):
        candidates = [field for field in fields if _matches(field, mapping.get("source"))]
        # An unchanged semantic name can describe the identical physical field.
        candidates = list({field["fieldId"]: field for field in candidates}.values())
        if not candidates:
            projected = _physical_projection(fields, mapping.get("source"))
            if projected is not None:
                candidates = [projected]
        if len(candidates) != 1:
            raise CBufferTransferError("CBuffer source must identify exactly one captured physical field")
        field = candidates[0]
        classification, target = mapping.get("classification"), str(mapping.get("target") or "")
        if mapping.get("binding") is not None and (mapping["binding"] != "raw-buffer" or classification != "material"):
            raise CBufferTransferError("CBuffer raw-buffer binding is only valid for material data")
        duplicate_target = False
        if target and classification in ("material", "system"):
            signature = (classification, field["type"], field["value"])
            if target in targets:
                if targets[target] != signature:
                    raise CBufferTransferError("CBuffer target has conflicting source values or types: " + target)
                duplicate_target = True
            targets[target] = signature
        resolved = dict(mapping, sourceIdentity={key: field[key] for key in (
            "physicalId", "fieldId", "physicalIdentity", "stage", "offsets", "type")},
            capturedValue=field["value"], valueAuthority=field["valueSource"], valueEvidence=field.get("valueEvidence", []),
            displayName=field["displayName"])
        if field["value"] is None:
            gaps.append({"code": "cbuffer_mapping_value_unavailable", "fieldId": field["fieldId"], "target": target})
        elif "value" in mapping and not _same_value(mapping["value"], field["value"], field["scalarType"]):
            raise CBufferTransferError("CBuffer value differs from captured physical bytes: " + target)
        else:
            resolved["value"] = field["value"]
        if classification == "unknown":
            gaps.append({"code": "cbuffer_mapping_unresolved", "fieldId": field["fieldId"]})
        elif classification in ("material", "system"):
            if field["value"] is not None:
                kind = "system" if classification == "system" else "array" if field["elements"] > 1 else "matrix" if field["matrix"] else "property"
                declared = mapping.get("type", "auto") or "auto"
                if classification == "material" and ((declared == "color" and (kind != "property" or len(field["offsets"]) != 4))
                        or (declared == "matrix" and not field["matrix"])
                        or (declared in ("float", "int", "uint", "bool") and len(field["offsets"]) != 1)):
                    raise CBufferTransferError("CBuffer target property type conflicts with its captured shape: " + target)
                raw_binding = classification == "material" and mapping.get("binding") == "raw-buffer"
                if raw_binding:
                    values = field["value"] if isinstance(field["value"], list) else [field["value"]]
                    code, width = _SCALARS[field["scalarType"]]
                    payload = _physical_payload(field, Path(capture_dir).resolve())
                    if payload is None:
                        payload = struct.pack("<%d%s" % (len(values), code), *values)
                    stride = field["rows"] * field["columns"] * width
                    if stride < 4 or stride % 4 or stride > 2048:
                        raise CBufferTransferError("raw CBuffer field requires a four-byte-aligned StructuredBuffer element")
                    covered.update((field["physicalId"], offset, width) for offset in field["offsets"])
                    buffer_binding = {"name": target, "kind": "buffer", "type": "buffer", "bufferType": "structured",
                        "bufferBase64": base64.b64encode(payload).decode("ascii"), "stride": stride,
                        "sourceIdentity": resolved["sourceIdentity"], "scalarType": field["scalarType"],
                        "rows": field["rows"], "columns": field["columns"], "elements": field["elements"],
                        "valueOrder": "row-major-logical", "value": field["value"],
                        "valueSha256": hashlib.sha256(payload).hexdigest()}
                    if not duplicate_target:
                        runtime.append(buffer_binding)
                    mappings.append(resolved)
                    continue
                if classification == "material" and field["scalarType"] in ("double", "long", "ulong"):
                    gaps.append({"code": "cbuffer_64bit_value_requires_raw_buffer", "target": target,
                                 "fieldId": field["fieldId"], "scalarType": field["scalarType"]})
                    mappings.append(resolved)
                    continue
                covered.update((field["physicalId"], offset, field["componentByteSize"]) for offset in field["offsets"])
                target_type = declared if declared != "auto" else (
                    "matrix" if field["matrix"] else "vector" if len(field["offsets"]) > 1 else
                    "float" if field["scalarType"] in ("float", "half", "double") else "int")
                binding = {"name": target, "value": field["value"], "type": target_type,
                    "sourceIdentity": resolved["sourceIdentity"], "scalarType": field["scalarType"],
                    "rows": field["rows"], "columns": field["columns"], "elements": field["elements"],
                    "valueOrder": "row-major-logical", "kind": kind}
                if duplicate_target:
                    pass  # Shared VS/PS material properties retain both source mappings.
                elif kind == "property":
                    properties.append(binding)
                else:
                    runtime.append(binding)
        else:
            raise CBufferTransferError("CBuffer classification must be material, system or unknown")
        mappings.append(resolved)
    missing = required - covered
    for physical_id in sorted({lane[0] for lane in missing}):
        lanes = [{"offset": offset, "byteSize": size} for identity, offset, size in sorted(missing) if identity == physical_id]
        gaps.append({"code": "cbuffer_used_lanes_unmapped", "physicalId": physical_id, "lanes": lanes})
    return {"schema": SCHEMA, "fields": fields, "mappings": mappings,
            "properties": properties, "runtimeBindings": runtime, "gaps": gaps,
            "coverage": {"requiredLaneCount": len(required), "mappedLaneCount": len(required & covered),
                         "reviewedUnusedLaneCount": len(unused), "unmappedLaneCount": len(missing),
                         "complete": not missing and not gaps}}
