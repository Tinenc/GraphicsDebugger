"""Fail-closed CBuffer ABI repair for a narrow SPIRV-Cross HLSL shape.

SPIRV-Cross commonly represents a stripped DXIL CBuffer as one rounded-up
``float4 member[N] : packoffset(c0)`` array.  That is useful as an algorithmic
view, but it changes a non-16-byte buffer envelope and loses integer load
overloads.  This module repairs only that mechanically provable case from a
captured CBuffer envelope and a hash-bound DXIL physical-access signature.

It deliberately does *not* repair resources, samplers, entry signatures,
interpolation, root signatures, or shader algorithms.  It also does not infer
source member names, matrices, structures, or signedness. A single-assignment
unsigned min-plus-constant index can be expanded into a bounded switch over
independently proven physical lanes. Other dynamic indices remain unsupported.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .dxil_access_signature import (
    DxilCBufferAccessBlock,
    DxilCBufferAccessSignature,
)

MAX_HLSL_CHARS = 64 * 1024 * 1024
MAX_CBUFFERS = 16_384
MAX_REWRITES = 262_144
_IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
_CBUFFER = re.compile(
    rf"\bcbuffer\s+(?P<block>{_IDENTIFIER})\s*:\s*"
    rf"register\s*\(\s*b(?P<bind>\d+)\s*,\s*space(?P<space>\d+)\s*\)\s*"
    rf"\{{(?P<body>.*?)\}}\s*;",
    re.DOTALL,
)
_SIMPLE_BODY = re.compile(
    rf"\s*float4\s+(?P<member>{_IDENTIFIER})\s*"
    rf"\[\s*(?P<count>\d+)(?:[uU])?\s*\]\s*:\s*"
    rf"packoffset\s*\(\s*c0(?:\.x)?\s*\)\s*;\s*",
    re.DOTALL,
)
_COMPONENT_INDEX = {
    "x": 0,
    "r": 0,
    "y": 1,
    "g": 1,
    "z": 2,
    "b": 2,
    "w": 3,
    "a": 3,
}
_COMPONENT_NAME = "xyzw"


class HlslCBufferAbiRepairError(ValueError):
    """The HLSL and physical evidence do not admit the narrow safe repair."""


@dataclass(frozen=True)
class RepairedCBuffer:
    space: int
    bind: int
    byte_size: int
    original_member: str
    access_count: int
    tail_guard_added: bool
    naming_source: str = "lane"
    asset_member_count: int = 0
    asset_fallback_reason: str = ""


@dataclass(frozen=True)
class HlslCBufferAbiRepairResult:
    hlsl: str
    repaired_blocks: tuple[RepairedCBuffer, ...]

    @property
    def asset_layout_used(self) -> bool:
        return any(block.naming_source in ("asset", "asset-partial") for block in self.repaired_blocks)


@dataclass(frozen=True)
class _AssetMember:
    name: str
    offset: int
    size: int
    base: str
    columns: int
    elements: int


_ASSET_BASE_TYPES = {
    "float": "float", "half": "half", "min16float": "min16float",
    "int": "int", "uint": "uint", "bool": "bool", "dword": "uint",
}
_FLOAT_BASES = {"float", "half", "min16float"}
_INT_BASES = {"int", "uint", "bool"}
_HLSL_RESERVED = {
    "asm", "bool", "break", "case", "cbuffer", "centroid", "class", "column_major",
    "compile", "const", "continue", "default", "discard", "do", "double", "else",
    "export", "extern", "false", "float", "for", "fxgroup", "groupshared", "half",
    "if", "in", "inline", "inout", "int", "interface", "line", "lineadj", "linear",
    "matrix", "min16float", "min16int", "min16uint", "namespace", "nointerpolation",
    "noperspective", "out", "packoffset", "pass", "point", "precise", "register",
    "return", "row_major", "sample", "sampler", "shared", "snorm", "stateblock",
    "static", "string", "struct", "switch", "technique", "texture", "triangle",
    "triangleadj", "true", "typedef", "uint", "uniform", "unorm", "unsigned",
    "vector", "vertexfragment", "void", "volatile", "while", "dword",
}


def _asset_members(
    block: Mapping[str, Any], byte_size: int
) -> tuple[list[_AssetMember], str]:
    """Normalize one asset block's members; return (members, fallback reason)."""

    if not isinstance(block, Mapping):
        return [], "asset block is not an object"
    if _integer(block.get("byteSize"), "asset byteSize", minimum=1) != byte_size:
        return [], "asset block byte size differs from the captured block"
    raw_fields = block.get("fields")
    if not isinstance(raw_fields, Sequence) or isinstance(raw_fields, (str, bytes)) or not raw_fields:
        return [], "asset block has no member layout"
    if len(raw_fields) > 4096:
        return [], "asset block member count exceeds its limit"
    members: list[_AssetMember] = []
    names: set[str] = set()
    for field in raw_fields:
        if not isinstance(field, Mapping):
            return [], "asset member is not an object"
        name = str(field.get("name", "") or "")
        if not re.fullmatch(_IDENTIFIER, name) or name.casefold() in _HLSL_RESERVED or name.startswith("__replaylab_"):
            return [], "asset member name %r is not a usable HLSL identifier" % name[:64]
        if name in names:
            return [], "asset member name %r is duplicated" % name[:64]
        names.add(name)
        base = _ASSET_BASE_TYPES.get(str(field.get("baseType", "") or "").casefold())
        rows = field.get("rows", 1)
        columns = field.get("columns", 1)
        elements = field.get("elements", 1)
        if base is None or field.get("members"):
            return [], "asset member %s has a struct/unsupported type" % name
        if (isinstance(rows, bool) or not isinstance(rows, int) or rows != 1
                or isinstance(columns, bool) or not isinstance(columns, int) or not 1 <= columns <= 4
                or isinstance(elements, bool) or not isinstance(elements, int) or not 1 <= elements <= 4096):
            return [], "asset member %s is a matrix or has an unsupported shape" % name
        offset = _integer(field.get("byteOffset"), "asset member byteOffset")
        size = _integer(field.get("byteSize"), "asset member byteSize", minimum=1)
        if offset % 4 or offset + size > byte_size:
            return [], "asset member %s lies outside the captured block" % name
        if elements > 1 and offset % 16:
            return [], "asset member %s array is not register aligned" % name
        members.append(_AssetMember(name, offset, size, base, columns, elements))
    members.sort(key=lambda item: item.offset)
    for previous, current in zip(members, members[1:]):
        if previous.offset + previous.size > current.offset:
            return [], "asset members %s and %s overlap" % (previous.name, current.name)
    return members, ""


class _LaneNaming:
    """Current behaviour: one scalar field per physically accessed lane."""

    source = "lane"
    fallback_reason = ""
    member_count = 0

    def __init__(self, space: int, bind: int, fields: Mapping[int, str], byte_size: int):
        self.space, self.bind, self.fields, self.byte_size = space, bind, dict(fields), byte_size
        self.tail_offset = byte_size - 4
        self.tail_added = self.tail_offset not in self.fields

    def names(self) -> set[str]:
        result = {_field_name(self.space, self.bind, offset, kind) for offset, kind in self.fields.items()}
        if self.tail_added:
            result.add(_tail_field_name(self.space, self.bind, self.tail_offset))
        return result

    def lane_float(self, offset: int) -> str:
        return _lane_as_float(self.fields, space=self.space, bind=self.bind, offset=offset)

    def member_lines(self) -> list[str]:
        lines = []
        for offset, return_type in sorted(self.fields.items()):
            register, component = offset // 16, _COMPONENT_NAME[(offset % 16) // 4]
            hlsl_type = "float" if return_type == "f32" else "uint"
            lines.append(
                f"    {hlsl_type} {_field_name(self.space, self.bind, offset, return_type)} "
                f": packoffset(c{register}.{component});"
            )
        if self.tail_added:
            register, component = self.tail_offset // 16, _COMPONENT_NAME[(self.tail_offset % 16) // 4]
            lines.append(
                f"    uint {_tail_field_name(self.space, self.bind, self.tail_offset)} "
                f": packoffset(c{register}.{component});"
            )
        return lines


class _AssetNaming:
    """Declare the captured block exactly as the matched unpacked asset does.

    The asset supplies names, vector widths and the members the code never
    reads; the captured envelope and the DXIL access evidence still decide
    admissibility lane by lane, so a wrong-version asset falls back to lane
    naming instead of producing a plausible but false layout.
    """

    source = "asset"

    def __init__(self, space: int, bind: int, fields: Mapping[int, str], byte_size: int,
                 members: Sequence[_AssetMember], source_text: str,
                 declaration_ranges: Sequence[tuple[int, int]], *, allow_partial: bool = True):
        self.space, self.bind, self.fields, self.byte_size = space, bind, dict(fields), byte_size
        self.members = list(members)
        self.member_count = len(self.members)
        self.fallback_reason = ""
        self.synthetic_count = 0
        self._lane_member: dict[int, tuple[_AssetMember, int, int]] = {}
        # Partial knowledge is still knowledge: an accessed lane no asset
        # member covers keeps its physical lane name instead of discarding
        # the names the asset does provide for the other members.
        if allow_partial:
            synthetic: list[_AssetMember] = []
            for offset, return_type in sorted(self.fields.items()):
                if any(m.offset <= offset < m.offset + m.size for m in self.members):
                    continue
                synthetic.append(_AssetMember(
                    _field_name(space, bind, offset, return_type), offset, 4,
                    "float" if return_type == "f32" else "uint", 1, 1))
            if synthetic:
                self.members = sorted(self.members + synthetic, key=lambda m: m.offset)
                self.synthetic_count = len(synthetic)
                self.source = "asset-partial"
        for offset, return_type in self.fields.items():
            member = next((m for m in self.members if m.offset <= offset < m.offset + m.size), None)
            if member is None:
                self.fallback_reason = "accessed lane at byte %d has no asset member" % offset
                return
            local = offset - member.offset
            element, component = local // 16, (local % 16) // 4
            if member.elements == 1 and member.columns * 4 <= local:
                self.fallback_reason = "accessed lane at byte %d exceeds asset member %s" % (offset, member.name)
                return
            if member.elements > 1 and (element >= member.elements or component >= member.columns):
                self.fallback_reason = "accessed lane at byte %d exceeds asset array %s" % (offset, member.name)
                return
            if member.elements == 1:
                element, component = 0, local // 4
            compatible = member.base in _FLOAT_BASES if return_type == "f32" else member.base in _INT_BASES
            if not compatible:
                self.fallback_reason = (
                    "DXIL %s access at byte %d contradicts asset member %s (%s)"
                    % (return_type, offset, member.name, member.base)
                )
                return
            self._lane_member[offset] = (member, element, component)
        last = self.members[-1]
        self.tail_offset = byte_size - 4
        self.tail_added = last.offset + last.size < byte_size
        if self.tail_added and self.tail_offset < last.offset + last.size:
            self.fallback_reason = "asset layout tail does not align with the captured block size"
            return
        for member in self.members:
            pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(member.name)}(?![A-Za-z0-9_])")
            for occurrence in pattern.finditer(source_text):
                if not any(start <= occurrence.start() < end for start, end in declaration_ranges):
                    self.fallback_reason = "asset member name %s already exists in the HLSL" % member.name
                    return

    def names(self) -> set[str]:
        result = {member.name for member in self.members}
        if self.tail_added:
            result.add(_tail_field_name(self.space, self.bind, self.tail_offset))
        return result

    def lane_float(self, offset: int) -> str:
        member, element, component = self._lane_member[offset]
        expression = member.name
        if member.elements > 1:
            expression += f"[{element}]"
        if member.columns > 1:
            expression += "." + _COMPONENT_NAME[component]
        if member.base in _INT_BASES:
            return f"asfloat({expression})"
        if member.base != "float":
            return f"float({expression})"
        return expression

    def member_lines(self) -> list[str]:
        lines = []
        for member in self.members:
            register, component = member.offset // 16, _COMPONENT_NAME[(member.offset % 16) // 4]
            hlsl_type = member.base + (str(member.columns) if member.columns > 1 else "")
            suffix = f"[{member.elements}]" if member.elements > 1 else ""
            packing = f"c{register}" if member.elements > 1 or (member.offset % 16 == 0 and member.columns == 4) else f"c{register}.{component}"
            lines.append(f"    {hlsl_type} {member.name}{suffix} : packoffset({packing});")
        if self.tail_added:
            register, component = self.tail_offset // 16, _COMPONENT_NAME[(self.tail_offset % 16) // 4]
            lines.append(
                f"    uint {_tail_field_name(self.space, self.bind, self.tail_offset)} "
                f": packoffset(c{register}.{component});"
            )
        return lines


@dataclass(frozen=True)
class _BaselineBlock:
    space: int
    bind: int
    bind_array_size: int
    byte_size: int
    layout_completeness: str


@dataclass(frozen=True)
class _Declaration:
    start: int
    end: int
    block_name: str
    member_name: str
    count: int
    space: int
    bind: int


@dataclass(frozen=True)
class _Edit:
    start: int
    end: int
    replacement: str


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise HlslCBufferAbiRepairError(f"{label} is invalid")
    return value


def _baseline_blocks(values: Sequence[Mapping[str, Any]]) -> dict[tuple[int, int], _BaselineBlock]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise HlslCBufferAbiRepairError("baseline_constant_blocks must be a sequence")
    if len(values) > MAX_CBUFFERS:
        raise HlslCBufferAbiRepairError("baseline CBuffer count exceeds its limit")
    result: dict[tuple[int, int], _BaselineBlock] = {}
    for index, value in enumerate(values):
        if not isinstance(value, Mapping):
            raise HlslCBufferAbiRepairError(
                f"baseline_constant_blocks[{index}] is not an object"
            )
        block = _BaselineBlock(
            _integer(value.get("space"), "baseline space"),
            _integer(value.get("bind"), "baseline bind"),
            _integer(
                value.get("bindArraySize"),
                "baseline bindArraySize",
                minimum=1,
            ),
            _integer(value.get("byteSize"), "baseline byteSize", minimum=1),
            str(value.get("layoutCompleteness", "") or "").strip().casefold(),
        )
        if block.layout_completeness not in {
            "complete",
            "partial",
            "opaque",
            "ambiguous",
        }:
            raise HlslCBufferAbiRepairError(
                "baseline layoutCompleteness is invalid"
            )
        key = (block.space, block.bind)
        if key in result:
            raise HlslCBufferAbiRepairError("duplicate baseline CBuffer binding")
        result[key] = block
    return result


def _declarations(source: str) -> dict[tuple[int, int], _Declaration]:
    declarations: dict[tuple[int, int], _Declaration] = {}
    for match in _CBUFFER.finditer(source):
        if len(declarations) >= MAX_CBUFFERS:
            raise HlslCBufferAbiRepairError("HLSL CBuffer count exceeds its limit")
        space = int(match.group("space"), 10)
        bind = int(match.group("bind"), 10)
        key = (space, bind)
        if key in declarations:
            raise HlslCBufferAbiRepairError("duplicate HLSL CBuffer binding")
        body = _SIMPLE_BODY.fullmatch(match.group("body"))
        # Retain non-simple declarations in the index.  They are harmless when
        # their captured layout is complete, but an ambiguous block below must
        # match this exact shape or the repair fails.
        member = body.group("member") if body is not None else ""
        count = int(body.group("count"), 10) if body is not None else 0
        declarations[key] = _Declaration(
            match.start(),
            match.end(),
            match.group("block"),
            member,
            count,
            space,
            bind,
        )
    return declarations


def _physical_accesses(
    baseline: _BaselineBlock,
    access: DxilCBufferAccessBlock | None,
) -> dict[int, str]:
    if access is None:
        raise HlslCBufferAbiRepairError(
            "ambiguous CBuffer has no DXIL access evidence"
        )
    if (
        baseline.bind_array_size != 1
        or access.bind_array_size != 1
        or access.space != baseline.space
        or access.bind != baseline.bind
        or access.byte_size != baseline.byte_size
        or not access.accesses
    ):
        raise HlslCBufferAbiRepairError(
            "ambiguous CBuffer access envelope contradicts its baseline"
        )
    if baseline.byte_size % 4:
        raise HlslCBufferAbiRepairError(
            "simple HLSL repair requires a four-byte CBuffer envelope"
        )
    result: dict[int, str] = {}
    for item in access.accesses:
        if (
            item.byte_width != 4
            or item.byte_offset % 4
            or item.return_type not in {"f32", "i32"}
            or item.byte_offset + item.byte_width > baseline.byte_size
        ):
            raise HlslCBufferAbiRepairError(
                "simple HLSL repair supports aligned f32/i32 accesses only"
            )
        if item.byte_offset in result:
            raise HlslCBufferAbiRepairError(
                "CBuffer byte lane has multiple physical access types"
            )
        result[item.byte_offset] = item.return_type
    return result


def _field_name(space: int, bind: int, offset: int, return_type: str) -> str:
    register = offset // 16
    component = _COMPONENT_NAME[(offset % 16) // 4]
    return (
        f"__replaylab_s{space}_b{bind}_c{register}_{component}_{return_type}"
    )


def _tail_field_name(space: int, bind: int, offset: int) -> str:
    register = offset // 16
    component = _COMPONENT_NAME[(offset % 16) // 4]
    return f"__replaylab_s{space}_b{bind}_c{register}_{component}_tail"


def _lane_as_float(
    fields: Mapping[int, str],
    *,
    space: int,
    bind: int,
    offset: int,
) -> str:
    return_type = fields[offset]
    name = _field_name(space, bind, offset, return_type)
    return name if return_type == "f32" else f"asfloat({name})"


def _replacement_expression(
    *,
    register: int,
    swizzle: str | None,
    naming: Any,
) -> tuple[str, set[int]]:
    fields = naming.fields
    base = register * 16
    if swizzle:
        offsets = [base + _COMPONENT_INDEX[item.casefold()] * 4 for item in swizzle]
        if any(offset not in fields for offset in offsets):
            raise HlslCBufferAbiRepairError(
                "HLSL references a CBuffer lane absent from DXIL access evidence"
            )
        values = [naming.lane_float(offset) for offset in offsets]
        if len(values) == 1:
            return values[0], set(offsets)
        return f"float{len(values)}({', '.join(values)})", set(offsets)

    values: list[str] = []
    consumed: set[int] = set()
    for lane in range(4):
        offset = base + lane * 4
        if offset in fields:
            values.append(naming.lane_float(offset))
            consumed.add(offset)
        else:
            values.append("0.0f")
    if not consumed:
        raise HlslCBufferAbiRepairError(
            "HLSL references a register absent from DXIL access evidence"
        )
    return f"float4({', '.join(values)})", consumed


def _reference_edits(
    source: str,
    declaration: _Declaration,
    declaration_ranges: Sequence[tuple[int, int]],
    naming: Any,
) -> tuple[list[_Edit], set[int], str]:
    member = declaration.member_name
    reference = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(member)}\s*"
        rf"\[\s*(?P<index>\d+(?:[uU])?|{_IDENTIFIER})\s*\]"
        rf"(?:\s*\.\s*(?P<swizzle>[xyzwrgbaXYZWRGBA]{{1,4}}))?"
    )
    token = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(member)}(?![A-Za-z0-9_])"
    )

    def inside_declaration(position: int) -> bool:
        return any(start <= position < end for start, end in declaration_ranges)

    matches = [
        match
        for match in reference.finditer(source)
        if not inside_declaration(match.start())
    ]
    by_start = {match.start(): match for match in matches}
    for occurrence in token.finditer(source):
        if inside_declaration(occurrence.start()):
            continue
        if occurrence.start() not in by_start:
            raise HlslCBufferAbiRepairError(
                "CBuffer member has a dynamic or unsupported HLSL use"
            )
    if not matches:
        raise HlslCBufferAbiRepairError(
            "DXIL access evidence has no matching HLSL CBuffer use"
        )
    edits: list[_Edit] = []
    consumed: set[int] = set()
    helpers: dict[str, str] = {}
    dynamic_expansions = 0
    for match in matches:
        index = match.group("index")
        if not re.fullmatch(r"\d+[uU]?", index):
            lower, upper = _bounded_hlsl_index(source, index)
            dynamic_expansions += upper - lower + 1
            if dynamic_expansions > MAX_REWRITES:
                raise HlslCBufferAbiRepairError("dynamic CBuffer recovery exceeds its expansion budget")
            if upper >= declaration.count:
                raise HlslCBufferAbiRepairError("bounded HLSL CBuffer index exceeds its array")
            helper = f"__replaylab_load_s{declaration.space}_b{declaration.bind}_c{lower}_{upper}"
            if re.search(rf"\b{re.escape(helper)}\b", source):
                raise HlslCBufferAbiRepairError("generated CBuffer helper already exists")
            cases = []
            for register in range(lower, upper + 1):
                _, offsets = _replacement_expression(
                    register=register, swizzle=match.group("swizzle"), naming=naming,
                )
                consumed.update(offsets)
                value, _ = _replacement_expression(
                    register=register, swizzle=None, naming=naming,
                )
                cases.append(f"        case {register}u: return {value};")
            helpers[helper] = "\n".join([
                f"float4 {helper}(uint index)", "{", "    switch (index)", "    {",
                *cases, "        default: return 0.0f;", "    }", "}",
            ])
            replacement = f"{helper}({index})"
            if match.group("swizzle"):
                replacement += "." + match.group("swizzle")
            edits.append(_Edit(match.start(), match.end(), replacement))
            continue
        register = int(index.rstrip("uU"), 10)
        if register >= declaration.count:
            raise HlslCBufferAbiRepairError(
                "HLSL CBuffer index exceeds its declared array"
            )
        replacement, offsets = _replacement_expression(
            register=register,
            swizzle=match.group("swizzle"),
            naming=naming,
        )
        edits.append(_Edit(match.start(), match.end(), replacement))
        consumed.update(offsets)
        if len(edits) > MAX_REWRITES:
            raise HlslCBufferAbiRepairError("HLSL rewrite count exceeds its limit")
    return edits, consumed, "\n\n".join(helpers.values())


def _bounded_hlsl_index(source: str, name: str) -> tuple[int, int]:
    """Accept a single-assignment uint min(value, bound) + offset only."""
    definition = re.compile(
        rf"\buint\s+{re.escape(name)}\s*=\s*min\((?P<value>[^;\r\n]+),\s*"
        rf"(?P<maximum>\d+)[uU]\s*\)\s*\+\s*(?P<offset>\d+)[uU]\s*;"
    )
    matches = list(definition.finditer(source))
    if len(matches) != 1:
        raise HlslCBufferAbiRepairError("CBuffer member has a dynamic or unsupported HLSL use")
    match = matches[0]
    value = match.group("value").strip()
    uint_value = re.fullmatch(_IDENTIFIER, value)
    resource_value = re.fullmatch(rf"(?P<resource>{_IDENTIFIER})\.Load\(.+\)\.x", value)
    if uint_value:
        typed = re.search(rf"\buint\s+{re.escape(value)}\b", source)
    elif resource_value:
        typed = re.search(rf"\bBuffer<uint[1-4]?>\s+{re.escape(resource_value.group('resource'))}\s*:", source)
    else:
        typed = None
    if not typed:
        raise HlslCBufferAbiRepairError("bounded HLSL min operand is not provably uint")
    for occurrence in re.finditer(rf"\b{re.escape(name)}\b", source):
        if match.start() <= occurrence.start() < match.end():
            continue
        before = source[:occurrence.start()].rstrip()
        after = source[occurrence.end():].lstrip()
        if not before.endswith("[") or not after.startswith("]"):
            raise HlslCBufferAbiRepairError("bounded HLSL index has an unsupported additional use")
    maximum, offset = int(match.group("maximum")), int(match.group("offset"))
    if maximum >= 4096 or offset + maximum > 0xFFFFFFFF:
        raise HlslCBufferAbiRepairError("bounded HLSL index exceeds its range budget")
    return offset, offset + maximum


def _declaration_text(
    declaration: _Declaration,
    naming: Any,
    newline: str,
) -> tuple[str, bool]:
    lines = [
        (
            f"cbuffer {declaration.block_name} : "
            f"register(b{declaration.bind}, space{declaration.space})"
        ),
        "{",
    ]
    lines.extend(naming.member_lines())
    lines.extend(("};",))
    return newline.join(lines), bool(naming.tail_added)


def _apply_edits(source: str, edits: Sequence[_Edit]) -> str:
    ordered = sorted(edits, key=lambda item: (item.start, item.end))
    previous_end = -1
    for item in ordered:
        if item.start < 0 or item.end < item.start or item.end > len(source):
            raise HlslCBufferAbiRepairError("HLSL edit range is invalid")
        if item.start < previous_end:
            raise HlslCBufferAbiRepairError("HLSL repair edits overlap")
        previous_end = item.end
    result = source
    for item in reversed(ordered):
        result = result[: item.start] + item.replacement + result[item.end :]
    return result


def repair_spirv_cross_hlsl_cbuffers(
    source: str,
    *,
    baseline_constant_blocks: Sequence[Mapping[str, Any]],
    dxil_access: DxilCBufferAccessSignature,
    asset_layout: Mapping[str, Any] | None = None,
) -> HlslCBufferAbiRepairResult:
    """Repair statically provable ambiguous CBuffers in SPIRV-Cross HLSL.

    Only captured blocks explicitly labelled ``ambiguous`` are changed.  Every
    such block must be a single rounded ``float4`` array at ``packoffset(c0)``,
    every HLSL use must have a static or proven bounded register index, and all consumed lanes
    must exactly cover the original DXIL f32/i32 access evidence.  Otherwise the
    whole operation fails without returning partially repaired source.

    ``asset_layout`` (``renderdoc-ultra-asset-cbuffer-layout-hint/v1``) may
    supply the matched unpacked asset's member layout per ``"space:bind"``.
    A block is declared with those names and widths only when its byte size
    equals the captured envelope, every DXIL-accessed lane lands inside a
    member of the matching scalar class, and no member name collides with the
    HLSL; otherwise that block keeps the physical lane naming and records why.
    """

    if not isinstance(source, str) or not source or len(source) > MAX_HLSL_CHARS:
        raise HlslCBufferAbiRepairError("HLSL source size is invalid")
    if "\x00" in source:
        raise HlslCBufferAbiRepairError("HLSL source contains NUL characters")
    if not isinstance(dxil_access, DxilCBufferAccessSignature):
        raise HlslCBufferAbiRepairError("dxil_access has the wrong type")

    baselines = _baseline_blocks(baseline_constant_blocks)
    ambiguous = {
        key: block
        for key, block in baselines.items()
        if block.layout_completeness == "ambiguous"
    }
    if not ambiguous:
        raise HlslCBufferAbiRepairError("there are no ambiguous CBuffers to repair")
    declarations = _declarations(source)
    declaration_ranges = tuple(
        (item.start, item.end) for item in declarations.values()
    )
    newline = "\r\n" if "\r\n" in source else "\n"
    edits: list[_Edit] = []
    repaired: list[RepairedCBuffer] = []
    generated_names: set[str] = set()

    for key, baseline in sorted(ambiguous.items()):
        declaration = declarations.get(key)
        if declaration is None:
            raise HlslCBufferAbiRepairError(
                "ambiguous baseline CBuffer is missing from HLSL"
            )
        if not declaration.member_name:
            raise HlslCBufferAbiRepairError(
                "ambiguous HLSL CBuffer is not the simple SPIRV-Cross shape"
            )
        expected_count = (baseline.byte_size + 15) // 16
        if declaration.count != expected_count:
            raise HlslCBufferAbiRepairError(
                "SPIRV-Cross CBuffer array does not match the rounded baseline size"
            )
        access_block = dxil_access.block(baseline.space, baseline.bind)
        fields = _physical_accesses(baseline, access_block)
        naming: Any = _LaneNaming(baseline.space, baseline.bind, fields, baseline.byte_size)
        fallback_reason = ""
        asset_block = (
            asset_layout.get("blocks", {}).get("%d:%d" % (baseline.space, baseline.bind))
            if isinstance(asset_layout, Mapping) and isinstance(asset_layout.get("blocks"), Mapping)
            else None
        )
        if asset_block is not None:
            try:
                members, fallback_reason = _asset_members(asset_block, baseline.byte_size)
            except HlslCBufferAbiRepairError as exc:
                members, fallback_reason = [], str(exc)
            if not fallback_reason:
                candidate = _AssetNaming(
                    baseline.space, baseline.bind, fields, baseline.byte_size,
                    members, source, declaration_ranges,
                )
                if candidate.fallback_reason:
                    fallback_reason = candidate.fallback_reason
                else:
                    naming = candidate
        generated_names.update(naming.names())
        reference_edits, consumed, helpers = _reference_edits(
            source,
            declaration,
            declaration_ranges,
            naming,
        )
        if consumed != set(fields):
            raise HlslCBufferAbiRepairError(
                "HLSL uses do not cover the complete DXIL access evidence"
            )
        replacement, tail_added = _declaration_text(
            declaration,
            naming,
            newline,
        )
        if helpers:
            replacement += newline + newline + helpers.replace("\n", newline)
        edits.extend(reference_edits)
        edits.append(_Edit(declaration.start, declaration.end, replacement))
        repaired.append(
            RepairedCBuffer(
                baseline.space,
                baseline.bind,
                baseline.byte_size,
                declaration.member_name,
                len(fields),
                tail_added,
                naming.source,
                naming.member_count,
                fallback_reason[:512],
            )
        )

    for name in generated_names:
        if not name.startswith("__replaylab_"):
            continue  # asset member names were checked outside their declaration
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", source):
            raise HlslCBufferAbiRepairError(
                "generated CBuffer field name already exists in HLSL"
            )
    if len(edits) > MAX_REWRITES:
        raise HlslCBufferAbiRepairError("HLSL rewrite count exceeds its limit")
    return HlslCBufferAbiRepairResult(
        _apply_edits(source, edits),
        tuple(repaired),
    )
