"""Restore narrow SPIRV-Cross Direct3D interfaces from captured reflection.

This is an explicit recovery transformation, never reflection evidence. It
requires fresh compilation, replay ABI validation, and numerical comparison.
The supported raw-buffer lowering is one uint per 32-bit scalar word; element
width and stride come from the captured StructuredBuffer, not guessed names.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from .hlsl_cbuffer_abi_repair import HlslCBufferAbiRepairError

_ID = r"[A-Za-z_][A-Za-z0-9_]*"

# Scalar declaration spellings SPIRV-Cross (and hand repairs) emit inside
# ``SPIRV_Cross_Input`` mapped onto the reflection component types RenderDoc
# reports for the same physical row.  ``bool`` system values such as
# ``SV_IsFrontFace`` reflect as one uint component; 16-bit spellings reflect
# either natively (``half``) or widened (``float``) depending on the driver.
_INPUT_TYPE_COMP_TYPES = {
    "float": ("float",),
    "uint": ("uint",),
    "int": ("int",),
    "dword": ("uint",),
    "bool": ("uint", "int"),
    "half": ("half", "float"),
    "min16float": ("half", "float"),
    "min10float": ("half", "float"),
    "float16_t": ("half", "float"),
    "double": ("double",),
    "min16int": ("int",),
    "min12int": ("int",),
    "int16_t": ("int",),
    "min16uint": ("uint",),
    "uint16_t": ("uint",),
}
_INPUT_TYPES = "|".join(sorted(_INPUT_TYPE_COMP_TYPES, key=len, reverse=True))
_INPUT_QUALIFIERS = "nointerpolation|centroid|sample|noperspective|linear|precise"
_BODY_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


def _error(message: str) -> None:
    raise HlslCBufferAbiRepairError(message)


def _signature(source: str, baseline: Mapping[str, Any]) -> str:
    match = re.search(r"\bstruct\s+SPIRV_Cross_Input\s*\{(?P<body>[^{}]*)\}\s*;", source)
    if match is None:
        if not baseline.get("inputSignature"):
            return source
        _error("SPIRV-Cross input declaration is unavailable")
    rows = baseline.get("inputSignature", [])
    declared = []
    consumed = set()
    pattern = re.compile(
        rf"\s*(?P<qual>(?:(?:{_INPUT_QUALIFIERS})\s+)*)"
        rf"(?P<type>{_INPUT_TYPES})(?P<width>[1-4]?)\s+(?P<name>{_ID})"
        rf"(?P<array>\[\s*(?P<count>\d+)\s*\])?\s*:\s*(?P<semantic>{_ID})\s*;"
    )
    # Comments never carry ABI; blank them so an annotated struct still parses.
    body = _BODY_COMMENT.sub(lambda found: " " * len(found.group(0)), match.group("body"))
    position = 0
    for field in pattern.finditer(body):
        if body[position:field.start()].strip():
            _error("unsupported SPIRV-Cross input declaration: %r"
                   % body[position:field.start()].strip()[:80])
        position = field.end()
        count = int(field.group("count") or 1)
        if not 1 <= count <= 1024:
            _error("SPIRV-Cross input array exceeds its limit")
        semantic = field.group("semantic")
        location = re.fullmatch(r"TEXCOORD(\d+)", semantic, re.IGNORECASE)
        if location:
            register = int(location.group(1))
            selected = [r for r in rows if r["regIndex"] == register and r["systemValue"] == "undefined"]
        else:
            selected = [r for r in rows if r["semanticIdxName"].lower() == semantic.lower()]
        if len(selected) != 1:
            _error("SPIRV-Cross input does not have one captured physical binding")
        first = selected[0]
        selected = [first]
        for offset in range(1, count):
            next_rows = [r for r in rows if r["regIndex"] == first["regIndex"] + offset
                         and r["semanticName"] == first["semanticName"]
                         and r["semanticIndex"] == first["semanticIndex"] + offset]
            if len(next_rows) != 1:
                _error("SPIRV-Cross input array does not preserve captured rows")
            selected.extend(next_rows)
        accepted_types = _INPUT_TYPE_COMP_TYPES[field.group("type")]
        for row in selected:
            key = (row["regIndex"], row["regChannelMask"])
            if key in consumed or row["compType"] not in accepted_types or row["compCount"] != int(field.group("width") or 1):
                _error("SPIRV-Cross input component type or physical row mismatch at %s"
                       % field.group("semantic"))
            consumed.add(key)
        declaration = (f"    {field.group('qual')}{field.group('type')}{field.group('width')} "
                       f"{field.group('name')}{field.group('array') or ''} : {first['semanticIdxName']};")
        declared.append((first["regIndex"], first["regChannelMask"], declaration))
    if body[position:].strip():
        _error("unsupported SPIRV-Cross input declaration tail: %r"
               % body[position:].strip()[:80])
    for row in rows:
        key = (row["regIndex"], row["regChannelMask"])
        if key in consumed:
            continue
        if row["channelUsedMask"] != 0 or row["systemValue"] != "isfrontface" or row["compCount"] != 1:
            _error("SPIRV-Cross omitted a captured input that cannot be safely restored")
        name = f"__replaylab_unused_frontface_r{row['regIndex']}"
        if re.search(rf"\b{name}\b", source):
            _error("generated input field name already exists")
        declared.append((row["regIndex"], row["regChannelMask"], f"    bool {name} : SV_IsFrontFace;"))
    body = "\n" + "\n".join(item[2] for item in sorted(declared)) + "\n"
    return source[:match.start("body")] + body + source[match.end("body"):]


def _calls(source: str, name: str, declaration: tuple[int, int], *, scalar: bool) -> list[tuple[int, int, str]]:
    result = []
    for occurrence in re.finditer(rf"\b{re.escape(name)}\b", source):
        if declaration[0] <= occurrence.start() < declaration[1]:
            continue
        start = occurrence.start()
        prefix = re.match(r"\s*\.\s*Load\s*\(", source[occurrence.end():])
        if prefix is None:
            _error("buffer recovery encountered a non-Load resource use")
        argument_start = occurrence.end() + prefix.end()
        depth, cursor = 1, argument_start
        while cursor < len(source) and depth:
            if source[cursor] == "(":
                depth += 1
            elif source[cursor] == ")":
                depth -= 1
            cursor += 1
        if depth or cursor - argument_start > 4096:
            _error("buffer recovery Load argument is unbounded")
        expression = source[argument_start:cursor - 1]
        if any(character in expression for character in ";{}"):
            _error("buffer recovery Load has unsupported syntax")
        if scalar:
            suffix = re.match(r"\s*\.\s*x\b", source[cursor:])
            if suffix is None:
                _error("structured-buffer lowering must consume exactly one uint word")
            cursor += suffix.end()
        result.append((start, cursor, expression))
        if len(result) > 262144:
            _error("buffer recovery exceeds its rewrite budget")
    return result


def repair_spirv_cross_hlsl_interface(source: str, *, baseline_interface: Mapping[str, Any]) -> str:
    """Recover physical input declarations and scalar/vector buffer bindings."""
    if not isinstance(source, str) or not 0 < len(source) <= 64 * 1024 * 1024 or "\x00" in source:
        _error("SPIRV-Cross interface source size is invalid")
    source = _signature(source, baseline_interface)
    baseline_resources = {(r["space"], r["bind"]): r for r in baseline_interface.get("readOnlyResources", [])}
    declarations = re.compile(
        rf"\bBuffer<(?P<type>uint|float|int)(?P<width>[1-4]?)>\s+(?P<name>{_ID})\s*:\s*"
        rf"register\(t(?P<bind>\d+),\s*space(?P<space>\d+)\)\s*;"
    )
    edits = []
    for declaration in declarations.finditer(source):
        space, bind = int(declaration.group("space")), int(declaration.group("bind"))
        resource = baseline_resources.get((space, bind))
        if resource is None:
            _error("generated buffer has no captured binding")
        typ = resource["variableType"]
        base, width = typ["baseType"], typ["columns"]
        structured = resource["descriptorType"] == "buffer"
        if (resource["descriptorType"] not in {"buffer", "typedbuffer"} or resource["arraySize"] != 1
                or base not in {"float", "uint", "int"} or not 1 <= width <= 4
                or typ["rows"] != 1 or typ["elements"] != 1 or typ["members"]
                or typ["byteSize"] != width * 4):
            _error("captured buffer element is outside the narrow recovery format")
        old_base, old_width = declaration.group("type"), int(declaration.group("width") or 1)
        if not structured and old_base == base and old_width == width:
            continue
        name = declaration.group("name")
        helper = f"__replaylab_load_s{space}_t{bind}"
        if re.search(rf"\b{helper}\b", source):
            _error("generated buffer helper name already exists")
        if structured and (old_base != "uint" or old_width != 4):
            _error("structured-buffer recovery requires SPIRV-Cross uint-word lowering")
        if not structured and (old_base != base or old_width != 4):
            _error("typed-buffer recovery has an incompatible scalar type")
        element_type = base + (str(width) if width > 1 else "")
        resource_type = "StructuredBuffer" if structured else "Buffer"
        replacement = f"{resource_type}<{element_type}> {name} : register(t{bind}, space{space});\n"
        if structured:
            value = f"{name}.Load(index / {width}u)[index % {width}u]" if width > 1 else f"{name}.Load(index)"
            value = f"asuint({value})" if base != "uint" else value
            replacement += f"uint {helper}(uint index) {{ return {value}; }}"
        else:
            values = [f"{name}.Load(index)"] + ["0"] * (4 - width)
            replacement += f"{base}4 {helper}(uint index) {{ return {base}4({', '.join(values)}); }}"
        calls = _calls(source, name, declaration.span(), scalar=structured)
        if not calls:
            _error("buffer recovery has no generated Load uses")
        edits.append((declaration.start(), declaration.end(), replacement))
        edits.extend((start, end, f"{helper}({expression})") for start, end, expression in calls)
    previous = -1
    for start, end, _ in sorted(edits):
        if start < previous:
            _error("SPIRV-Cross interface recovery edits overlap")
        previous = end
    for start, end, replacement in sorted(edits, reverse=True):
        source = source[:start] + replacement + source[end:]
    return source
