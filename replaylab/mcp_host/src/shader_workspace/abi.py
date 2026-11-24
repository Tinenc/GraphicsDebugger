"""Fail-closed shader ABI model and replay-reflection contract.

This module deliberately does not infer ABI compatibility from DXBC hashes or
decompiled HLSL.  A candidate must be reflected by RenderDoc's replay driver,
then every physical interface fact represented here is compared with the
captured baseline.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence

from .adapters.dxil_access_signature import DxilCBufferAccessSignature
from .errors import AbiValidationError


ABI_SCHEMA = "renderdoc-ultra-shader-abi/v1"
ABI_VERSION = 1
REFLECTION_REQUEST_SCHEMA = "renderdoc-ultra-shader-reflection-request/v1"
REFLECTION_RESPONSE_SCHEMA = "renderdoc-ultra-shader-reflection/v1"
REFLECTION_VERSION = 1

MAX_SIGNATURE_PARAMETERS = 1024
MAX_BINDINGS_PER_CATEGORY = 16384
MAX_CONSTANT_BLOCKS = 1024
MAX_CONSTANT_FIELDS = 65536
MAX_FIELD_DEPTH = 32
MAX_DIFFERENCES = 256
MAX_STRING_LENGTH = 4096
MAX_SHADER_BYTES = 512 * 1024 * 1024
UINT32_MAX = (1 << 32) - 1
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_STAGE_ALIASES = {
    "pixel": "fragment",
    "ps": "fragment",
    "fs": "fragment",
    "vs": "vertex",
    "gs": "geometry",
    "hs": "hull",
    "ds": "domain",
    "cs": "compute",
    "as": "task",
    "amplification": "task",
    "ms": "mesh",
    "tessctrl": "hull",
    "tesseval": "domain",
}
_STAGES = frozenset(
    {
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
    }
)
_D3D11_STAGES = frozenset(
    {"vertex", "hull", "domain", "geometry", "fragment", "compute"}
)
_D3D12_STAGES = frozenset(
    {"vertex", "hull", "domain", "geometry", "fragment", "compute", "task", "mesh"}
)
_VULKAN_REPLACEABLE_STAGES = _D3D12_STAGES
_OPENGL_REPLACEABLE_STAGES = _D3D11_STAGES


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def normalize_stage(value: Any) -> str:
    if not isinstance(value, str):
        raise AbiValidationError("shader stage must be a string")
    stage = (
        value.strip()
        .lower()
        .replace("shaderstage.", "")
        .replace("_", "")
        .replace("-", "")
    )
    stage = _STAGE_ALIASES.get(stage, stage)
    if stage not in _STAGES:
        raise AbiValidationError("unsupported shader stage: %s" % value)
    return stage


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AbiValidationError("%s must be an object" % path)
    return value


def _list(value: Any, path: str, maximum: int) -> list[Any]:
    if not isinstance(value, list):
        raise AbiValidationError("%s must be an array" % path)
    if len(value) > maximum:
        raise AbiValidationError("%s exceeds the %d item limit" % (path, maximum))
    return value


def _text(value: Any, path: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise AbiValidationError("%s must be a string" % path)
    if len(value) > MAX_STRING_LENGTH:
        raise AbiValidationError("%s exceeds the string length limit" % path)
    if not allow_empty and not value:
        raise AbiValidationError("%s must not be empty" % path)
    return value


def _integer(
    value: Any,
    path: str,
    *,
    minimum: int = 0,
    maximum: int = UINT32_MAX,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AbiValidationError("%s must be an integer" % path)
    if value < minimum or value > maximum:
        raise AbiValidationError(
            "%s must be between %d and %d" % (path, minimum, maximum)
        )
    return value


def _boolean(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise AbiValidationError("%s must be a boolean" % path)
    return value


def _enum(value: Any, path: str) -> str:
    text = _text(value, path)
    return text.strip().lower().rsplit(".", 1)[-1].replace("_", "").replace(" ", "")


def _base_type(value: Any, path: str) -> str:
    token = _enum(value, path)
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
    return aliases.get(token, token)


def _alias(
    value: Mapping[str, Any],
    path: str,
    names: Sequence[str],
    parser: Callable[[Any, str], Any],
    *,
    required: bool = True,
    default: Any = None,
) -> Any:
    found = [(name, value[name]) for name in names if name in value]
    if not found:
        if required:
            raise AbiValidationError(
                "%s.%s is required" % (path, "/".join(names))
            )
        return default
    parsed = [parser(item, "%s.%s" % (path, name)) for name, item in found]
    first = parsed[0]
    if any(item != first for item in parsed[1:]):
        raise AbiValidationError(
            "%s has conflicting aliases: %s" % (path, ", ".join(names))
        )
    return first


def _required_array(
    value: Mapping[str, Any], path: str, names: Sequence[str], maximum: int
) -> list[Any]:
    return _alias(
        value,
        path,
        names,
        lambda item, item_path: _list(item, item_path, maximum),
    )


@dataclass(frozen=True)
class SignatureParameter:
    semantic: str
    semantic_index_name: str
    semantic_index: int
    register: int
    system_value: str
    component_type: str
    component_count: int
    stream: int
    register_mask: int
    used_mask: int
    per_primitive_rate: bool
    need_semantic_index: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "semanticName": self.semantic,
            "semanticIdxName": self.semantic_index_name,
            "semanticIndex": self.semantic_index,
            "regIndex": self.register,
            "systemValue": self.system_value,
            "compType": self.component_type,
            "compCount": self.component_count,
            "stream": self.stream,
            "regChannelMask": self.register_mask,
            "channelUsedMask": self.used_mask,
            "perPrimitiveRate": self.per_primitive_rate,
            "needSemanticIndex": self.need_semantic_index,
        }

    def physical_dict(self) -> dict[str, Any]:
        """Return only the caller-visible signature contract.

        ``channelUsedMask`` is backend-specific compiler analysis. In DXBC
        output signatures it marks lanes *never written*, while input
        signatures mark lanes always read. It is not an input
        or output linkage declaration, and packed DXIL elements may report the
        same aggregate usage on every row while a recompile reports usage per
        row.  Keep it in ``to_dict()`` for raw audit/reparse, but never bind an
        ABI hash or replacement decision to it.
        """

        result = self.to_dict()
        result.pop("channelUsedMask")
        return result


@dataclass(frozen=True)
class ShaderVariableType:
    base_type: str
    rows: int
    columns: int
    elements: int
    array_byte_stride: int
    matrix_byte_stride: int
    row_major: bool
    flags: int
    pointer_type_id: int
    byte_size: int
    members: tuple["ConstantField", ...]
    layout_completeness: str = "complete"

    def to_dict(self) -> dict[str, Any]:
        result = {
            "baseType": self.base_type,
            "rows": self.rows,
            "columns": self.columns,
            "elements": self.elements,
            "arrayByteStride": self.array_byte_stride,
            "matrixByteStride": self.matrix_byte_stride,
            "rowMajor": self.row_major,
            "flags": self.flags,
            "pointerTypeID": self.pointer_type_id,
            "byteSize": self.byte_size,
            "members": [member.to_dict() for member in self.members],
        }
        if self.layout_completeness == "opaque":
            result["layoutCompleteness"] = "opaque"
        return result


@dataclass(frozen=True)
class ResourceBinding:
    space: int
    bind: int
    array_size: int
    descriptor_type: str
    texture_type: str
    variable_type: ShaderVariableType
    is_texture: bool
    has_sampler: bool
    is_input_attachment: bool
    is_read_only: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "space": self.space,
            "bind": self.bind,
            "arraySize": self.array_size,
            "descriptorType": self.descriptor_type,
            "textureType": self.texture_type,
            "variableType": self.variable_type.to_dict(),
            "isTexture": self.is_texture,
            "hasSampler": self.has_sampler,
            "isInputAttachment": self.is_input_attachment,
            "isReadOnly": self.is_read_only,
        }


@dataclass(frozen=True)
class SamplerBinding:
    space: int
    bind: int
    array_size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "space": self.space,
            "bind": self.bind,
            "arraySize": self.array_size,
        }


@dataclass(frozen=True)
class ConstantField:
    byte_offset: int
    byte_size: int
    base_type: str
    rows: int
    columns: int
    elements: int
    array_byte_stride: int
    matrix_byte_stride: int
    row_major: bool
    flags: int
    pointer_type_id: int
    bit_field_offset: int
    bit_field_size: int
    members: tuple["ConstantField", ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "byteOffset": self.byte_offset,
            "byteSize": self.byte_size,
            "baseType": self.base_type,
            "rows": self.rows,
            "columns": self.columns,
            "elements": self.elements,
            "arrayByteStride": self.array_byte_stride,
            "matrixByteStride": self.matrix_byte_stride,
            "rowMajor": self.row_major,
            "flags": self.flags,
            "pointerTypeID": self.pointer_type_id,
            "bitFieldOffset": self.bit_field_offset,
            "bitFieldSize": self.bit_field_size,
            "members": [member.to_dict() for member in self.members],
        }


@dataclass(frozen=True)
class ConstantBlock:
    space: int
    bind: int
    array_size: int
    byte_size: int
    buffer_backed: bool
    inline_data_bytes: bool
    compile_constants: bool
    fields: tuple[ConstantField, ...]
    layout_completeness: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "space": self.space,
            "bind": self.bind,
            "bindArraySize": self.array_size,
            "byteSize": self.byte_size,
            "bufferBacked": self.buffer_backed,
            "inlineDataBytes": self.inline_data_bytes,
            "compileConstants": self.compile_constants,
            "fields": [field.to_dict() for field in self.fields],
            "layoutCompleteness": self.layout_completeness,
        }


@dataclass(frozen=True)
class ShaderAbi:
    stage: str
    entry_point: str
    inputs: tuple[SignatureParameter, ...]
    outputs: tuple[SignatureParameter, ...]
    read_only_resources: tuple[ResourceBinding, ...]
    read_write_resources: tuple[ResourceBinding, ...]
    samplers: tuple[SamplerBinding, ...]
    constant_blocks: tuple[ConstantBlock, ...]
    thread_group_size: tuple[int, ...]
    output_topology: str
    dynamic_linkage_interface_count: int

    def physical_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "entryPoint": self.entry_point,
            "inputSignature": [item.physical_dict() for item in self.inputs],
            "outputSignature": [item.physical_dict() for item in self.outputs],
            "readOnlyResources": [
                item.to_dict() for item in self.read_only_resources
            ],
            "readWriteResources": [
                item.to_dict() for item in self.read_write_resources
            ],
            "samplers": [item.to_dict() for item in self.samplers],
            "constantBlocks": [item.to_dict() for item in self.constant_blocks],
            "threadGroupSize": list(self.thread_group_size),
            "outputTopology": self.output_topology,
            "dynamicLinkageInterfaceCount": self.dynamic_linkage_interface_count,
        }

    def to_dict(self) -> dict[str, Any]:
        result = {
            "schema": ABI_SCHEMA,
            "schemaVersion": ABI_VERSION,
            "interfaceComplete": True,
        }
        result.update(self.physical_dict())
        # Preserve usage analysis in the serialised reflection evidence even
        # though it is intentionally outside the callable physical ABI.
        result["inputSignature"] = [item.to_dict() for item in self.inputs]
        result["outputSignature"] = [item.to_dict() for item in self.outputs]
        return result

    @property
    def sha256(self) -> str:
        return _digest(self.physical_dict())


@dataclass(frozen=True)
class AbiComparison:
    compatible: bool
    baseline_sha256: str
    candidate_sha256: str
    differences: tuple[dict[str, Any], ...]
    differences_truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "compatible": self.compatible,
            "baselineAbiSha256": self.baseline_sha256,
            "candidateAbiSha256": self.candidate_sha256,
            "differences": [dict(value) for value in self.differences],
            "differencesTruncated": self.differences_truncated,
        }


def _parse_signature(value: Any, path: str, *, output: bool = False) -> SignatureParameter:
    item = _mapping(value, path)
    semantic = _alias(item, path, ("semanticName", "semantic"), _text).lower()
    semantic_index_name = _alias(
        item, path, ("semanticIdxName",), _text
    ).lower()
    semantic_index = _alias(
        item, path, ("semanticIndex", "index"), _integer
    )
    register = _alias(
        item,
        path,
        ("regIndex", "location"),
        lambda raw, raw_path: _integer(raw, raw_path, minimum=-1),
    )
    system_value = _alias(item, path, ("systemValue",), _enum)
    component_type = _alias(
        item, path, ("compType", "componentType"), _base_type
    )
    component_count = _alias(
        item,
        path,
        ("compCount", "componentCount"),
        lambda raw, raw_path: _integer(raw, raw_path, maximum=4),
    )
    stream = _alias(item, path, ("stream",), _integer)
    register_mask = _alias(
        item,
        path,
        ("regChannelMask", "registerMask"),
        lambda raw, raw_path: _integer(raw, raw_path, maximum=0xF),
    )
    used_mask = _alias(
        item,
        path,
        ("channelUsedMask", "usedMask"),
        lambda raw, raw_path: _integer(raw, raw_path, maximum=0xF),
    )
    # RenderDoc preserves DXBC's raw ReadWriteMask. For outputs its polarity
    # is inverted: a declared xy output may validly have a zw (0xC) mask.
    # Retain those original bits for old-record round trips, rather than
    # guessing a conversion or weakening the physical register-mask gate.
    if not output and used_mask & ~register_mask:
        raise AbiValidationError("%s used mask is outside its register mask" % path)
    if component_count and register_mask.bit_count() != component_count:
        raise AbiValidationError(
            "%s component count does not match its register mask" % path
        )
    return SignatureParameter(
        semantic,
        semantic_index_name,
        semantic_index,
        register,
        system_value,
        component_type,
        component_count,
        stream,
        register_mask,
        used_mask,
        _alias(item, path, ("perPrimitiveRate",), _boolean),
        _alias(item, path, ("needSemanticIndex",), _boolean),
    )


def _parse_variable_type(
    value: Any, path: str, budget: "_FieldBudget"
) -> ShaderVariableType:
    item = _mapping(value, path)
    byte_size = _alias(item, path, ("byteSize",), _integer)
    members = _required_array(item, path, ("members",), MAX_CONSTANT_FIELDS)
    parsed_members = tuple(
        sorted(
            (
                _parse_field(
                    member,
                    "%s.members[%d]" % (path, index),
                    budget,
                    depth=0,
                    parent_absolute_offset=0,
                    allow_runtime_arrays=True,
                )
                for index, member in enumerate(members)
            ),
            key=lambda member: _canonical_json(member.to_dict()),
        )
    )
    base_type = _alias(item, path, ("baseType",), _base_type)
    aggregate = base_type == "struct" and bool(parsed_members)
    completeness = item.get("layoutCompleteness", "complete")
    if completeness not in {"complete", "opaque"}:
        raise AbiValidationError("%s has an invalid resource element layout" % path)
    opaque = completeness == "opaque"
    elements = _alias(item, path, ("elements",),
                      lambda raw, raw_path: _integer(raw, raw_path, minimum=1))
    array_stride = _alias(item, path, ("arrayByteStride",), _integer)
    if opaque and (base_type != "unknown" or parsed_members or elements != 1 or
                   byte_size <= 0 or array_stride != byte_size or byte_size % 4 or
                   item.get("rows") != 0 or item.get("columns") != 0):
        raise AbiValidationError("%s has an invalid opaque structured element" % path)
    member_limit = byte_size
    if elements == 0xFFFFFFFF:
        if byte_size != 0 or array_stride <= 0:
            raise AbiValidationError("%s has an invalid runtime array layout" % path)
        member_limit = array_stride
    elif aggregate and elements > 1:
        if array_stride <= 0 or array_stride * (elements - 1) >= byte_size:
            raise AbiValidationError("%s has an invalid struct array stride" % path)
        member_limit = min(array_stride, byte_size - array_stride * (elements - 1))
    for member in parsed_members:
        if member.byte_offset + member.byte_size > member_limit:
            raise AbiValidationError("%s contains a member outside its byte size" % path)
    _validate_field_ranges(parsed_members, path + ".members")
    return ShaderVariableType(
        base_type,
        _alias(
            item,
            path,
            ("rows",),
            lambda raw, raw_path: _integer(raw, raw_path, minimum=0 if aggregate or opaque else 1),
        ),
        _alias(
            item,
            path,
            ("columns",),
            lambda raw, raw_path: _integer(raw, raw_path, minimum=0 if aggregate or opaque else 1),
        ),
        elements,
        array_stride,
        _alias(item, path, ("matrixByteStride",), _integer),
        _alias(item, path, ("rowMajor",), _boolean),
        _alias(item, path, ("flags",), _integer),
        _alias(item, path, ("pointerTypeID",), _integer),
        byte_size,
        parsed_members,
        completeness,
    )


def _parse_binding(
    value: Any,
    path: str,
    budget: "_FieldBudget",
    *,
    expected_read_only: bool,
) -> ResourceBinding:
    item = _mapping(value, path)
    binding = ResourceBinding(
        _alias(
            item, path, ("space", "fixedBindSetOrSpace"), _integer
        ),
        _alias(item, path, ("bind", "fixedBindNumber"), _integer),
        _alias(
            item,
            path,
            ("arraySize", "bindArraySize"),
            lambda raw, raw_path: _integer(raw, raw_path, minimum=1),
        ),
        _alias(
            item,
            path,
            ("descriptorType", "resourceType", "resType"),
            _enum,
        ),
        _alias(item, path, ("textureType", "dimension"), _enum),
        _alias(
            item,
            path,
            ("variableType",),
            lambda raw, raw_path: _parse_variable_type(raw, raw_path, budget),
        ),
        _alias(item, path, ("isTexture",), _boolean),
        _alias(item, path, ("hasSampler",), _boolean),
        _alias(item, path, ("isInputAttachment",), _boolean),
        _alias(item, path, ("isReadOnly",), _boolean),
    )
    if binding.is_read_only is not expected_read_only:
        raise AbiValidationError(
            "%s isReadOnly contradicts its resource category" % path
        )
    if binding.variable_type.layout_completeness == "opaque" and (
            binding.is_texture or binding.texture_type != "buffer"):
        raise AbiValidationError("%s opaque elements require a buffer resource" % path)
    return binding


def _parse_sampler(value: Any, path: str) -> SamplerBinding:
    item = _mapping(value, path)
    return SamplerBinding(
        _alias(item, path, ("space", "fixedBindSetOrSpace"), _integer),
        _alias(item, path, ("bind", "fixedBindNumber"), _integer),
        _alias(
            item,
            path,
            ("arraySize", "bindArraySize"),
            lambda raw, raw_path: _integer(raw, raw_path, minimum=1),
        ),
    )


class _FieldBudget:
    def __init__(self) -> None:
        self.count = 0

    def consume(self, path: str) -> None:
        self.count += 1
        if self.count > MAX_CONSTANT_FIELDS:
            raise AbiValidationError(
                "%s exceeds the %d total constant-field limit"
                % (path, MAX_CONSTANT_FIELDS)
            )


def _validate_field_ranges(
    fields: Sequence[ConstantField], path: str
) -> None:
    """Require every annotated sibling range to be physically verifiable."""

    ranges: list[tuple[int, int]] = []
    for field in fields:
        if field.elements == 0xFFFFFFFF:
            start, end = field.byte_offset * 8, 1 << 128
        elif field.bit_field_size:
            start = field.byte_offset * 8 + field.bit_field_offset
            end = start + field.bit_field_size
        else:
            start = field.byte_offset * 8
            end = (field.byte_offset + field.byte_size) * 8
        ranges.append((start, end))
    ranges.sort()
    previous_end = -1
    for start, end in ranges:
        if start < previous_end:
            raise AbiValidationError(
                "%s contains overlapping annotated access ranges" % path
            )
        previous_end = end


def _parse_field(
    value: Any,
    path: str,
    budget: _FieldBudget,
    *,
    depth: int,
    parent_absolute_offset: int,
    allow_runtime_arrays: bool = False,
) -> ConstantField:
    if depth > MAX_FIELD_DEPTH:
        raise AbiValidationError(
            "%s exceeds the %d level nesting limit" % (path, MAX_FIELD_DEPTH)
        )
    budget.consume(path)
    item = _mapping(value, path)
    byte_offset = _alias(item, path, ("byteOffset", "offset"), _integer)
    absolute_offset = parent_absolute_offset + byte_offset
    if "relativeOffset" in item:
        relative = _integer(item["relativeOffset"], path + ".relativeOffset")
        if relative != byte_offset:
            raise AbiValidationError("%s relative offset is inconsistent" % path)
    if "absoluteByteOffset" in item:
        declared_absolute = _integer(
            item["absoluteByteOffset"], path + ".absoluteByteOffset"
        )
        if declared_absolute != absolute_offset:
            raise AbiValidationError("%s absolute offset is inconsistent" % path)

    runtime_array = item.get("elements") == 0xFFFFFFFF
    if runtime_array and not allow_runtime_arrays:
        raise AbiValidationError("%s runtime arrays are only valid in resource layouts" % path)
    byte_size = _alias(
        item,
        path,
        ("byteSize",),
        lambda raw, raw_path: _integer(raw, raw_path, minimum=0 if runtime_array else 1),
    )
    base_type = _alias(item, path, ("baseType",), _base_type)
    aggregate = base_type == "struct" and bool(item.get("members", item.get("fields", [])))
    rows = _alias(
        item,
        path,
        ("rows",),
        lambda raw, raw_path: _integer(raw, raw_path, minimum=0 if aggregate else 1),
    )
    columns = _alias(
        item,
        path,
        ("columns", "cols"),
        lambda raw, raw_path: _integer(raw, raw_path, minimum=0 if aggregate else 1),
    )
    elements = _alias(
        item,
        path,
        ("elements",),
        lambda raw, raw_path: _integer(raw, raw_path, minimum=1),
    )
    array_stride = _alias(
        item, path, ("arrayByteStride", "arrayStride"), _integer
    )
    matrix_stride = _alias(
        item, path, ("matrixByteStride", "matrixStride"), _integer
    )
    row_major = _alias(item, path, ("rowMajor",), _boolean)
    type_info_value = item.get("typeInfo")
    type_info = (
        _mapping(type_info_value, path + ".typeInfo")
        if type_info_value is not None
        else None
    )
    if "flags" in item:
        type_flags = _integer(item["flags"], path + ".flags")
    elif type_info is not None and "flags" in type_info:
        type_flags = _integer(type_info["flags"], path + ".typeInfo.flags")
    else:
        raise AbiValidationError("%s.flags/typeInfo.flags is required" % path)
    pointer_type_id = _alias(item, path, ("pointerTypeID",), _integer)
    if type_info is not None:
        duplicate_values = {
            "baseType": (base_type, _base_type),
            "rows": (rows, _integer),
            "columns": (columns, _integer),
            "elements": (elements, _integer),
            "arrayByteStride": (array_stride, _integer),
            "matrixByteStride": (matrix_stride, _integer),
            "rowMajor": (row_major, _boolean),
        }
        for key, (expected, parser) in duplicate_values.items():
            if key not in type_info:
                raise AbiValidationError("%s.typeInfo.%s is required" % (path, key))
            actual = parser(type_info[key], "%s.typeInfo.%s" % (path, key))
            if actual != expected:
                raise AbiValidationError(
                    "%s typeInfo.%s contradicts its flattened value" % (path, key)
                )
    bit_offset = _alias(
        item,
        path,
        ("bitFieldOffset",),
        lambda raw, raw_path: _integer(raw, raw_path, maximum=63),
        required=False,
        default=0,
    )
    bit_size = _alias(
        item,
        path,
        ("bitFieldSize",),
        lambda raw, raw_path: _integer(raw, raw_path, maximum=64),
        required=False,
        default=0,
    )
    if bit_size == 0 and bit_offset != 0:
        raise AbiValidationError("%s has a bit offset without a bit-field size" % path)
    if bit_size and bit_offset + bit_size > 64:
        raise AbiValidationError("%s bit field exceeds 64 bits" % path)

    children = _alias(
        item,
        path,
        ("members", "fields"),
        lambda raw, raw_path: _list(raw, raw_path, MAX_CONSTANT_FIELDS),
        required=False,
        default=[],
    )
    parsed_children = tuple(
        sorted(
            (
                _parse_field(
                    child,
                    "%s.members[%d]" % (path, index),
                    budget,
                    depth=depth + 1,
                    parent_absolute_offset=absolute_offset,
                    allow_runtime_arrays=allow_runtime_arrays,
                )
                for index, child in enumerate(children)
            ),
            key=lambda child: _canonical_json(child.to_dict()),
        )
    )
    member_limit = byte_size
    if runtime_array:
        if byte_size != 0 or array_stride <= 0 or bit_size:
            raise AbiValidationError("%s has an invalid runtime array layout" % path)
        member_limit = array_stride
    elif aggregate and elements > 1:
        if array_stride <= 0 or array_stride * (elements - 1) >= byte_size:
            raise AbiValidationError("%s has an invalid struct array stride" % path)
        member_limit = min(array_stride, byte_size - array_stride * (elements - 1))
    for child in parsed_children:
        if child.byte_offset + child.byte_size > member_limit:
            raise AbiValidationError("%s contains a member outside its byte size" % path)
    _validate_field_ranges(parsed_children, path + ".members")
    return ConstantField(
        byte_offset,
        byte_size,
        base_type,
        rows,
        columns,
        elements,
        array_stride,
        matrix_stride,
        row_major,
        type_flags,
        pointer_type_id,
        bit_offset,
        bit_size,
        parsed_children,
    )


def _parse_constant_block(
    value: Any, path: str, budget: _FieldBudget
) -> ConstantBlock:
    item = _mapping(value, path)
    byte_size = _alias(
        item, path, ("byteSize", "size"), _integer
    )
    fields = _required_array(
        item, path, ("fields", "variables"), MAX_CONSTANT_FIELDS
    )
    # Older bridge builds serialised stripped DXIL storage placeholders as one
    # or more overlapping/out-of-range fields named exactly ``unknown`` with an
    # unnamed typeInfo.  These are not source-member annotations.  Preserve the
    # block's binding/count/storage/byte envelope, but do not invent a UInt4 or
    # byte-tail layout from those placeholders.  Typed or otherwise malformed
    # fields still go through the strict parser and fail closed.
    fields, derived_completeness = _filter_legacy_unknown_placeholders(
        fields, byte_size
    )
    declared_completeness = item.get("layoutCompleteness")
    if declared_completeness is None:
        layout_completeness = derived_completeness
    else:
        layout_completeness = str(declared_completeness or "").strip().casefold()
        if layout_completeness not in {"complete", "partial", "opaque", "ambiguous"}:
            raise AbiValidationError(
                "%s.layoutCompleteness is invalid" % path
            )
        # A producer may conservatively lower a physically complete layout to
        # partial/ambiguous, but it must never upgrade or relabel uncertainty.
        # In particular, filtered legacy placeholders make the derived layout
        # ambiguous; accepting a declared opaque/partial value here would turn
        # contradictory ranges into an apply-eligible comparison lane.
        allowed_declarations = {
            "complete": {"complete", "partial", "ambiguous"},
            "partial": {"partial", "ambiguous"},
            "opaque": {"opaque", "ambiguous"},
            "ambiguous": {"ambiguous"},
        }
        if (
            layout_completeness not in allowed_declarations[derived_completeness]
            or (layout_completeness == "partial" and not fields)
            or (layout_completeness == "opaque" and fields)
        ):
            raise AbiValidationError(
                "%s.layoutCompleteness contradicts its fields" % path
            )
    parsed_fields = tuple(
        sorted(
            (
                _parse_field(
                    field,
                    "%s.fields[%d]" % (path, index),
                    budget,
                    depth=0,
                    parent_absolute_offset=0,
                )
                for index, field in enumerate(fields)
            ),
            key=lambda field: _canonical_json(field.to_dict()),
        )
    )
    for field in parsed_fields:
        if field.byte_offset + field.byte_size > byte_size:
            raise AbiValidationError("%s contains a field outside its byte size" % path)
    _validate_field_ranges(parsed_fields, path + ".fields")
    return ConstantBlock(
        _alias(item, path, ("space", "registerSpace"), _integer),
        _alias(item, path, ("bind", "bindNumber"), _integer),
        _alias(
            item,
            path,
            ("arraySize", "bindArraySize"),
            lambda raw, raw_path: _integer(raw, raw_path, minimum=1),
        ),
        byte_size,
        _alias(item, path, ("bufferBacked",), _boolean),
        _alias(item, path, ("inlineDataBytes",), _boolean),
        _alias(item, path, ("compileConstants",), _boolean),
        parsed_fields,
        layout_completeness,
    )


def _filter_legacy_unknown_placeholders(
    fields: Sequence[Any], parent_byte_size: int
) -> tuple[list[Any], str]:
    """Drop only contradictory old-bridge DXIL placeholders, field by field.

    Names are not ABI, so ``unknown`` plus an empty reflected type name is not
    sufficient to discard a layout. Complete unnamed ranges remain strict even
    when a sibling placeholder is malformed; only when no reliable range
    remains does the block naturally become opaque.
    """

    if not fields:
        return [], "opaque"
    retained: list[Any] = []
    for field in fields:
        legacy_unknown = (
            isinstance(field, Mapping)
            and str(field.get("name", "") or "").strip().casefold() == "unknown"
            and isinstance(field.get("typeInfo"), Mapping)
            and not str(field["typeInfo"].get("name", "") or "").strip()
        )
        if not legacy_unknown:
            retained.append(field)
            continue
        try:
            offset = int(field.get("byteOffset", field.get("offset", -1)))
            size = int(field.get("byteSize", field.get("size", -1)))
            rows = int(field.get("rows", 0))
            columns = int(field.get("columns", 0))
            elements = int(field.get("elements", 0))
        except (TypeError, ValueError, OverflowError):
            # This exact anonymous legacy placeholder carries no usable range.
            continue
        base_type = str(field.get("baseType", "") or "").strip().casefold()
        if (
            offset < 0
            or size <= 0
            or rows <= 0
            or columns <= 0
            or elements <= 0
            or base_type in {"", "unknown", "vartype.unknown"}
            or offset + size > parent_byte_size
        ):
            continue
        bit_offset = field.get("bitFieldOffset", 0)
        bit_size = field.get("bitFieldSize", 0)
        try:
            bit_offset = int(bit_offset)
            bit_size = int(bit_size)
        except (TypeError, ValueError, OverflowError):
            continue
        if bit_size:
            if bit_offset < 0 or bit_size < 0 or bit_offset + bit_size > size * 8:
                continue
        elif bit_offset:
            continue
        retained.append(field)
    retained = _drop_conflicting_legacy_unknown_fields(retained)
    if len(retained) != len(fields):
        return retained, "ambiguous"
    return retained, "complete"


def _raw_field_bit_range(field: Mapping[str, Any]) -> tuple[int, int] | None:
    try:
        offset = int(field.get("byteOffset", field.get("offset", -1)))
        size = int(field.get("byteSize", field.get("size", -1)))
        bit_offset = int(field.get("bitFieldOffset", 0))
        bit_size = int(field.get("bitFieldSize", 0))
    except (TypeError, ValueError, OverflowError):
        return None
    if offset < 0 or size <= 0:
        return None
    if bit_size:
        return offset * 8 + bit_offset, offset * 8 + bit_offset + bit_size
    return offset * 8, (offset + size) * 8


def _drop_conflicting_legacy_unknown_fields(fields: Sequence[Any]) -> list[Any]:
    """Remove ambiguous anonymous overlaps without erasing known ranges."""

    legacy: list[tuple[int, tuple[int, int] | None, Mapping[str, Any]]] = []
    known: list[tuple[int, tuple[int, int] | None, Mapping[str, Any]]] = []
    for index, field in enumerate(fields):
        if not isinstance(field, Mapping):
            # Preserve malformed non-mappings so the strict parser rejects them.
            continue
        is_legacy = (
            str(field.get("name", "") or "").strip().casefold() == "unknown"
            and isinstance(field.get("typeInfo"), Mapping)
            and not str(field["typeInfo"].get("name", "") or "").strip()
        )
        entry = (index, _raw_field_bit_range(field), field)
        (legacy if is_legacy else known).append(entry)
    dropped: set[int] = set()
    for legacy_index, legacy_range, _field in legacy:
        if legacy_range is None:
            dropped.add(legacy_index)
            continue
        if any(
            known_range is not None
            and legacy_range[0] < known_range[1]
            and known_range[0] < legacy_range[1]
            for _known_index, known_range, _known_field in known
        ):
            dropped.add(legacy_index)
    for left_position, (left_index, left_range, left_field) in enumerate(legacy):
        if left_range is None:
            continue
        for right_index, right_range, right_field in legacy[left_position + 1 :]:
            if (
                right_range is None
                or left_range[0] >= right_range[1]
                or right_range[0] >= left_range[1]
            ):
                continue
            if left_range == right_range and dict(left_field) == dict(right_field):
                dropped.add(right_index)
            else:
                dropped.add(left_index)
                dropped.add(right_index)
    return [field for index, field in enumerate(fields) if index not in dropped]


def _unique_bindings(
    values: Sequence[ResourceBinding | SamplerBinding], path: str
) -> tuple[ResourceBinding | SamplerBinding, ...]:
    ordered = tuple(sorted(values, key=lambda item: _canonical_json(item.to_dict())))
    seen: set[tuple[int, int]] = set()
    for item in ordered:
        key = (item.space, item.bind)
        if key in seen:
            raise AbiValidationError(
                "%s contains duplicate space/register binding %d/%d"
                % (path, item.space, item.bind)
            )
        seen.add(key)
    return ordered


def _unique_blocks(values: Sequence[ConstantBlock]) -> tuple[ConstantBlock, ...]:
    ordered = tuple(sorted(values, key=lambda item: _canonical_json(item.to_dict())))
    seen: set[tuple[int, int]] = set()
    for item in ordered:
        key = (item.space, item.bind)
        if key in seen:
            raise AbiValidationError(
                "constantBlocks contains duplicate space/register binding %d/%d"
                % key
            )
        seen.add(key)
    return ordered


def parse_shader_abi(
    reflection: Any,
    *,
    expected_stage: str | None = None,
    expected_entry_point: str | None = None,
) -> ShaderAbi:
    """Parse a complete reflection object into a strict physical ABI model."""

    value = _mapping(reflection, "compiledInterface")
    if "schema" in value and (
        value.get("schema") != ABI_SCHEMA or value.get("schemaVersion") != ABI_VERSION
    ):
        raise AbiValidationError("unsupported shader ABI schema/version")
    if value.get("interfaceComplete") is not True:
        raise AbiValidationError("compiledInterface.interfaceComplete must be true")

    stage_value = value.get("stage", expected_stage)
    if stage_value is None:
        raise AbiValidationError("compiledInterface.stage is required")
    stage = normalize_stage(stage_value)
    if expected_stage is not None and stage != normalize_stage(expected_stage):
        raise AbiValidationError("compiledInterface.stage does not match the request")

    entry_value = value.get("entryPoint", expected_entry_point)
    if entry_value is None:
        raise AbiValidationError("compiledInterface.entryPoint is required")
    entry = _text(entry_value, "compiledInterface.entryPoint", allow_empty=False)
    if expected_entry_point is not None and entry != _text(
        expected_entry_point, "expected_entry_point", allow_empty=False
    ):
        raise AbiValidationError("compiledInterface.entryPoint does not match the request")

    inputs_raw = _required_array(
        value, "compiledInterface", ("inputSignature",), MAX_SIGNATURE_PARAMETERS
    )
    outputs_raw = _required_array(
        value, "compiledInterface", ("outputSignature",), MAX_SIGNATURE_PARAMETERS
    )

    def signatures(values: list[Any], path: str) -> tuple[SignatureParameter, ...]:
        parsed = tuple(
            sorted(
                (
                    _parse_signature(item, "%s[%d]" % (path, index),
                                     output=path == "outputSignature")
                    for index, item in enumerate(values)
                ),
                key=lambda item: _canonical_json(item.physical_dict()),
            )
        )
        physical_parameters = tuple(
            _canonical_json(item.physical_dict()) for item in parsed
        )
        if len(set(physical_parameters)) != len(physical_parameters):
            raise AbiValidationError("%s contains duplicate parameters" % path)
        return parsed

    budget = _FieldBudget()

    def bindings(name: str, expected_read_only: bool) -> tuple[ResourceBinding, ...]:
        raw = _required_array(
            value, "compiledInterface", (name,), MAX_BINDINGS_PER_CATEGORY
        )
        return tuple(_unique_bindings(
            tuple(
                _parse_binding(
                    item,
                    "%s[%d]" % (name, index),
                    budget,
                    expected_read_only=expected_read_only,
                )
                for index, item in enumerate(raw)
            ),
            name,
        ))

    def samplers() -> tuple[SamplerBinding, ...]:
        raw = _required_array(
            value, "compiledInterface", ("samplers",), MAX_BINDINGS_PER_CATEGORY
        )
        return tuple(_unique_bindings(
            tuple(
                _parse_sampler(item, "samplers[%d]" % index)
                for index, item in enumerate(raw)
            ),
            "samplers",
        ))

    block_values = _required_array(
        value,
        "compiledInterface",
        ("constantBlocks", "constantBuffers"),
        MAX_CONSTANT_BLOCKS,
    )
    blocks = _unique_blocks(
        tuple(
            _parse_constant_block(item, "constantBlocks[%d]" % index, budget)
            for index, item in enumerate(block_values)
        )
    )

    dimensions = _required_array(
        value, "compiledInterface", ("threadGroupSize",), 3
    )
    if stage == "compute":
        if len(dimensions) != 3:
            raise AbiValidationError(
                "compute shader threadGroupSize must contain exactly three dimensions"
            )
        thread_group = tuple(
            _integer(item, "threadGroupSize[%d]" % index, minimum=1)
            for index, item in enumerate(dimensions)
        )
    else:
        if dimensions:
            raise AbiValidationError(
                "non-compute shader threadGroupSize must be an empty array"
            )
        thread_group = ()

    output_topology = _alias(
        value, "compiledInterface", ("outputTopology",), _enum
    )
    dynamic_linkage_count = _alias(
        value,
        "compiledInterface",
        ("dynamicLinkageInterfaceCount",),
        _integer,
    )

    return ShaderAbi(
        stage,
        entry,
        signatures(inputs_raw, "inputSignature"),
        signatures(outputs_raw, "outputSignature"),
        bindings("readOnlyResources", True),
        bindings("readWriteResources", False),
        samplers(),
        blocks,
        thread_group,
        output_topology,
        dynamic_linkage_count,
    )


def _append_differences(
    baseline: Any,
    candidate: Any,
    path: str,
    differences: list[dict[str, Any]],
) -> bool:
    if len(differences) >= MAX_DIFFERENCES:
        return True
    if isinstance(baseline, dict) and isinstance(candidate, dict):
        truncated = False
        for key in sorted(set(baseline) | set(candidate)):
            child_path = "%s.%s" % (path, key) if path else key
            if key not in baseline:
                differences.append(
                    {"path": child_path, "kind": "added", "candidate": candidate[key]}
                )
            elif key not in candidate:
                differences.append(
                    {"path": child_path, "kind": "removed", "baseline": baseline[key]}
                )
            else:
                truncated = (
                    _append_differences(
                        baseline[key], candidate[key], child_path, differences
                    )
                    or truncated
                )
            if len(differences) >= MAX_DIFFERENCES:
                return True
        return truncated
    if isinstance(baseline, list) and isinstance(candidate, list):
        truncated = False
        for index in range(max(len(baseline), len(candidate))):
            child_path = "%s[%d]" % (path, index)
            if index >= len(baseline):
                differences.append(
                    {
                        "path": child_path,
                        "kind": "added",
                        "candidate": candidate[index],
                    }
                )
            elif index >= len(candidate):
                differences.append(
                    {
                        "path": child_path,
                        "kind": "removed",
                        "baseline": baseline[index],
                    }
                )
            else:
                truncated = (
                    _append_differences(
                        baseline[index], candidate[index], child_path, differences
                    )
                    or truncated
                )
            if len(differences) >= MAX_DIFFERENCES:
                return True
        return truncated
    if baseline != candidate:
        differences.append(
            {
                "path": path,
                "kind": "changed",
                "baseline": baseline,
                "candidate": candidate,
            }
        )
    return False


def _ambiguous_access_match(
    captured: Mapping[str, Any],
    candidate: Mapping[str, Any],
    baseline_access: DxilCBufferAccessSignature | None,
    candidate_access: DxilCBufferAccessSignature | None,
) -> tuple[bool, str, str | None, str | None]:
    """Prove one ambiguous block from exact, independently derived DXIL loads."""

    if baseline_access is None or candidate_access is None:
        return False, "access-signature-unavailable", None, None
    space = int(captured["space"])
    bind = int(captured["bind"])
    baseline_block = baseline_access.block(space, bind)
    candidate_block = candidate_access.block(space, bind)
    baseline_digest = (
        hashlib.sha256(_canonical_json(baseline_block.physical_dict())).hexdigest()
        if baseline_block is not None
        else None
    )
    candidate_digest = (
        hashlib.sha256(_canonical_json(candidate_block.physical_dict())).hexdigest()
        if candidate_block is not None
        else None
    )
    if baseline_block is None or candidate_block is None:
        return False, "access-signature-block-missing", baseline_digest, candidate_digest
    if (
        captured.get("space") != candidate.get("space")
        or captured.get("bind") != candidate.get("bind")
        or captured.get("bindArraySize") != 1
        or candidate.get("bindArraySize") != 1
        or baseline_block.space != space
        or candidate_block.space != space
        or baseline_block.bind != bind
        or candidate_block.bind != bind
        or baseline_block.bind_array_size != captured.get("bindArraySize")
        or candidate_block.bind_array_size != candidate.get("bindArraySize")
        or baseline_block.byte_size != captured.get("byteSize")
        or candidate_block.byte_size != candidate.get("byteSize")
        or not baseline_block.accesses
        or not candidate_block.accesses
    ):
        return False, "access-signature-envelope-mismatch", baseline_digest, candidate_digest
    if baseline_block.physical_dict() == candidate_block.physical_dict():
        return True, "access-signature-exact", baseline_digest, candidate_digest
    # An edit routinely lets the compiler drop loads the original performed
    # (a constant written over an output dead-codes the math feeding it). Every
    # load the candidate still performs is then one the captured shader also
    # performed, at the same offset, width and type: the reconstructed layout
    # is exercised only where the capture proved it. New offsets, or a changed
    # width/type at a shared offset, are not proven by the capture and stay
    # rejected.
    baseline_loads = set(baseline_block.accesses)
    candidate_loads = set(candidate_block.accesses)
    if candidate_loads and candidate_loads < baseline_loads:
        return True, "access-signature-subset", baseline_digest, candidate_digest
    return False, "access-signature-mismatch", baseline_digest, candidate_digest


def _comparison_interface(abi: ShaderAbi) -> dict[str, Any]:
    """Interpret the legacy zero-enum spelling without rewriting sealed hashes.

    Older bridges serialized Topology.Unknown (integer zero) as an empty
    string. Both spellings mean no reflected output topology. Keep the stored
    physical dictionary and its digest intact so existing manifests, reports,
    and restore receipts remain independently verifiable.
    """
    value = abi.physical_dict()
    if value["outputTopology"] == "unknown":
        value["outputTopology"] = ""
    return value


def same_physical_shader_abi(left: ShaderAbi, right: ShaderAbi) -> bool:
    """Exact captured-interface identity, including legacy absent topology."""
    return _comparison_interface(left) == _comparison_interface(right)


def physical_shader_abi_hashes(abi: ShaderAbi) -> frozenset[str]:
    """Verified digests for the two historical absent-topology spellings only."""
    value = abi.physical_dict()
    hashes = {abi.sha256}
    if value["outputTopology"] in {"", "unknown"}:
        value["outputTopology"] = "unknown" if value["outputTopology"] == "" else ""
        hashes.add(_digest(value))
    return frozenset(hashes)


def compare_shader_abi(
    baseline: ShaderAbi,
    candidate: ShaderAbi,
    *,
    baseline_dxil_access: DxilCBufferAccessSignature | None = None,
    candidate_dxil_access: DxilCBufferAccessSignature | None = None,
) -> AbiComparison:
    """Compare physical ABIs and return bounded, stable differences.

    A captured typed CBuffer keeps the existing exact recursive layout check.
    If the captured block is opaque, no member type can be proven: comparison
    is limited to its binding/space/count/storage facts and byte size.  Candidate
    fields have already passed recursive bounds and overlap validation, so they
    can describe that byte range without being mistaken for captured facts.
    """

    baseline_value = _comparison_interface(baseline)
    candidate_value = _comparison_interface(candidate)
    for category in ("readOnlyResources", "readWriteResources"):
        for candidate_binding in candidate_value[category]:
            for captured in baseline_value[category]:
                original_type = captured["variableType"]
                if original_type.get("layoutCompleteness") != "opaque": continue
                if any(captured[k] != candidate_binding.get(k) for k in captured if k != "variableType"): continue
                target_type = candidate_binding["variableType"]
                if (target_type["byteSize"] == original_type["byteSize"] and
                        target_type["arrayByteStride"] == original_type["arrayByteStride"] and
                        target_type["elements"] == 1):
                    candidate_binding["variableType"] = dict(original_type)
                    break
    baseline_blocks = {
        (block["space"], block["bind"]): block
        for block in baseline_value["constantBlocks"]
    }
    forced_differences: list[dict[str, Any]] = []
    for block_index, block in enumerate(candidate_value["constantBlocks"]):
        captured = baseline_blocks.get((block["space"], block["bind"]))
        if captured is None:
            continue
        baseline_completeness = captured.get("layoutCompleteness", "complete")
        candidate_completeness = block.get("layoutCompleteness", "complete")
        if baseline_completeness == "ambiguous":
            matched, reason, baseline_digest, candidate_digest = (
                _ambiguous_access_match(
                    captured,
                    block,
                    baseline_dxil_access,
                    candidate_dxil_access,
                )
            )
            if not matched:
                forced_differences.append(
                    {
                        "path": "constantBlocks[%d].layoutCompleteness" % block_index,
                        "kind": "ambiguous-layout-apply-ineligible",
                        "reason": reason,
                        "baseline": baseline_completeness,
                        "candidate": candidate_completeness,
                        "baselineAccessBlockSha256": baseline_digest,
                        "candidateAccessBlockSha256": candidate_digest,
                    }
                )
            else:
                captured["fields"] = []
                block["fields"] = []
                captured["layoutCompleteness"] = "ambiguous"
                block["layoutCompleteness"] = "ambiguous"
        elif (
            baseline_completeness == "complete"
            and candidate_completeness == "ambiguous"
        ):
            forced_differences.append(
                {
                    "path": "constantBlocks[%d].layoutCompleteness" % block_index,
                    "kind": "ambiguous-layout-apply-ineligible",
                    "reason": "candidate-ambiguous-baseline-complete",
                    "baseline": baseline_completeness,
                    "candidate": candidate_completeness,
                    "baselineAccessBlockSha256": None,
                    "candidateAccessBlockSha256": None,
                }
            )
        elif baseline_completeness == "opaque":
            block["fields"] = []
            block["layoutCompleteness"] = "opaque"
        elif baseline_completeness == "partial":
            captured_fields = captured["fields"]
            if all(field in block["fields"] for field in captured_fields):
                block["fields"] = list(captured_fields)
                block["layoutCompleteness"] = "partial"
    differences: list[dict[str, Any]] = list(forced_differences)
    truncated = _append_differences(
        baseline_value, candidate_value, "", differences
    )
    return AbiComparison(
        compatible=not differences and not truncated,
        baseline_sha256=baseline.sha256,
        candidate_sha256=candidate.sha256,
        differences=tuple(differences),
        differences_truncated=truncated,
    )


def build_candidate_reflection_request(
    *,
    workspace_id: str,
    capture_id: str,
    api: str,
    candidate_path: str | Path,
    candidate_sha256: str,
    candidate_byte_size: int,
    candidate_kind: str,
    revision: int,
    stage: str,
    entry_point: str,
    baseline_abi_sha256: str,
    target_profile: str,
) -> dict[str, Any]:
    """Build the exact Host-to-bridge candidate-reflection request contract.

    The bridge consumes only ``params``.  The surrounding binding is retained
    by the Host so the response cannot be accidentally applied to another
    workspace, capture, revision, or candidate.
    """

    workspace = _text(workspace_id, "workspace_id", allow_empty=False)
    capture = _text(capture_id, "capture_id", allow_empty=False)
    api_value = _text(api, "api", allow_empty=False)
    api_token = api_value.strip().lower().replace("graphicsapi.", "")
    api_token = {
        "direct3d11": "d3d11",
        "direct3d12": "d3d12",
        "vk": "vulkan",
        "gl": "opengl",
    }.get(api_token, api_token)
    if api_token not in {"d3d11", "d3d12", "vulkan", "opengl"}:
        raise AbiValidationError(
            "the native replay ABI gate supports D3D11/D3D12/Vulkan/OpenGL only"
        )
    stage_value = normalize_stage(stage)
    candidate_encoding = _text(candidate_kind, "candidate_kind", allow_empty=False).lower()
    if api_token == "d3d11":
        if stage_value not in _D3D11_STAGES or candidate_encoding != "dxbc":
            raise AbiValidationError("D3D11 candidate reflection requires native DXBC")
    elif api_token == "d3d12" and (
        stage_value not in _D3D12_STAGES
        or candidate_encoding not in {"dxbc", "dxil"}
    ):
        raise AbiValidationError(
            "D3D12 candidate reflection requires native DXBC/DXIL and a pipeline shader stage"
        )
    elif api_token == "vulkan" and (
        stage_value not in _VULKAN_REPLACEABLE_STAGES
        or candidate_encoding != "spirv"
    ):
        raise AbiValidationError(
            "Vulkan candidate reflection requires native SPIR-V and a graphics/compute pipeline stage"
        )
    elif api_token == "opengl" and (
        stage_value not in _OPENGL_REPLACEABLE_STAGES
        or candidate_encoding not in {"opengl-spirv", "glsl"}
    ):
        raise AbiValidationError(
            "OpenGL candidate reflection requires native OpenGL SPIR-V and a legacy pipeline stage"
        )
    entry = _text(entry_point, "entry_point", allow_empty=False)
    profile = _text(target_profile, "target_profile", allow_empty=False).lower()
    if api_token in {"d3d11", "d3d12"}:
        profile_match = re.fullmatch(
            r"(ps|vs|gs|hs|ds|cs|as|ms)_(\d+)_(\d+)", profile
        )
        profile_stage = {
            "ps": "fragment", "vs": "vertex", "gs": "geometry", "hs": "hull",
            "ds": "domain", "cs": "compute", "as": "task", "ms": "mesh",
        }
        if (
            profile_match is None
            or profile_stage[profile_match.group(1)] != stage_value
        ):
            raise AbiValidationError(
                "target_profile must exactly match the reflected shader stage"
            )
    else:
        expected_profiles = {"spirv"} if api_token == "vulkan" else {"opengl-spirv", "glsl"}
        if profile not in expected_profiles:
            raise AbiValidationError(
                "target_profile must preserve the capture-bound SPIR-V environment"
            )
    digest = _text(candidate_sha256, "candidate_sha256", allow_empty=False).lower()
    if not SHA256_RE.fullmatch(digest):
        raise AbiValidationError("candidate_sha256 must be a lowercase SHA-256")
    baseline_digest = _text(
        baseline_abi_sha256, "baseline_abi_sha256", allow_empty=False
    ).lower()
    if not SHA256_RE.fullmatch(baseline_digest):
        raise AbiValidationError("baseline_abi_sha256 must be a lowercase SHA-256")
    size = _integer(
        candidate_byte_size,
        "candidate_byte_size",
        minimum=1,
        maximum=MAX_SHADER_BYTES,
    )
    revision_value = _integer(revision, "revision", minimum=1)
    path = Path(candidate_path).expanduser().resolve()
    if not path.is_file():
        raise AbiValidationError("candidate_path is not a file: %s" % path)
    if path.stat().st_size != size:
        raise AbiValidationError("candidate_path byte size changed")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != digest:
        raise AbiValidationError("candidate_path SHA-256 changed")

    binding = {
        "workspaceId": workspace,
        "captureId": capture,
        "revision": revision_value,
        "candidateRawSha256": digest,
        "candidateByteSize": size,
        "baselineAbiSha256": baseline_digest,
        "stage": stage_value,
        "entryPoint": entry,
        "api": api_token,
        "encoding": candidate_encoding,
        "targetProfile": profile,
    }
    params = {
        "candidate_path": str(path),
        "expected_sha256": digest,
        "stage": stage_value,
        "entry_point": entry,
        "encoding": candidate_encoding,
        "target_profile": profile,
    }
    request_id = _digest(
        {
            "method": "reflect_shader_candidate",
            "binding": binding,
            "params": params,
        }
    )
    return {
        "schema": REFLECTION_REQUEST_SCHEMA,
        "schemaVersion": REFLECTION_VERSION,
        "requestId": request_id,
        "method": "reflect_shader_candidate",
        "binding": binding,
        "params": params,
        "requiredReplaySequence": [
            "BuildTargetShader(%s candidate)" % candidate_encoding.upper(),
            "GetShader(ResourceId.Null(), targetId, entryPoint)",
            "serialize complete ShaderReflection interface",
            "FreeTargetResource(targetId)",
        ],
    }


def validate_candidate_reflection_response(
    request: Any,
    response: Any,
    baseline: ShaderAbi,
    *,
    baseline_dxil_access: DxilCBufferAccessSignature | None = None,
    candidate_dxil_access: DxilCBufferAccessSignature | None = None,
) -> tuple[ShaderAbi, AbiComparison, dict[str, Any]]:
    """Validate bridge evidence and compare its reflected candidate ABI.

    All replay lifecycle flags and both candidate-byte hashes are mandatory.
    The returned evidence is safe to persist in an audit report.  An ABI
    mismatch is represented by ``comparison.compatible == False``; malformed
    or incomplete evidence raises :class:`AbiValidationError`.
    """

    request_value = _mapping(request, "reflectionRequest")
    if (
        request_value.get("schema") != REFLECTION_REQUEST_SCHEMA
        or request_value.get("schemaVersion") != REFLECTION_VERSION
        or request_value.get("method") != "reflect_shader_candidate"
    ):
        raise AbiValidationError("unsupported candidate reflection request")
    binding = _mapping(request_value.get("binding"), "reflectionRequest.binding")
    request_params = _mapping(
        request_value.get("params"), "reflectionRequest.params"
    )
    expected_request_id = _digest(
        {
            "method": "reflect_shader_candidate",
            "binding": dict(binding),
            "params": dict(request_params),
        }
    )
    if request_value.get("requestId") != expected_request_id:
        raise AbiValidationError("candidate reflection request binding changed")
    if binding.get("baselineAbiSha256") != baseline.sha256:
        raise AbiValidationError("baseline ABI does not match the reflection request")
    if set(request_params) != {
        "candidate_path",
        "expected_sha256",
        "stage",
        "entry_point",
        "encoding",
        "target_profile",
    }:
        raise AbiValidationError("candidate reflection request params are invalid")
    if (
        request_params.get("expected_sha256")
        != binding.get("candidateRawSha256")
        or normalize_stage(request_params.get("stage")) != binding.get("stage")
        or request_params.get("entry_point") != binding.get("entryPoint")
        or request_params.get("encoding") != binding.get("encoding")
        or request_params.get("target_profile") != binding.get("targetProfile")
    ):
        raise AbiValidationError(
            "candidate reflection request params contradict their binding"
        )
    candidate_path = Path(
        _text(
            request_params.get("candidate_path"),
            "reflectionRequest.params.candidate_path",
            allow_empty=False,
        )
    ).expanduser().resolve()
    if not candidate_path.is_file():
        raise AbiValidationError("candidate reflection request path is not a file")
    if candidate_path.stat().st_size != binding.get("candidateByteSize"):
        raise AbiValidationError("candidate reflection request file size changed")
    if hashlib.sha256(candidate_path.read_bytes()).hexdigest() != binding.get(
        "candidateRawSha256"
    ):
        raise AbiValidationError("candidate reflection request file hash changed")

    value = _mapping(response, "reflectionResponse")
    if (
        value.get("schema") != REFLECTION_RESPONSE_SCHEMA
        or value.get("schemaVersion") != REFLECTION_VERSION
    ):
        raise AbiValidationError("unsupported candidate reflection response")
    expected_hash = binding.get("candidateRawSha256")
    expected_size = binding.get("candidateByteSize")
    if value.get("rawSha256") != expected_hash:
        raise AbiValidationError("reflected candidate rawSha256 does not match")
    if value.get("reflectedRawSha256") != expected_hash:
        raise AbiValidationError("ShaderReflection raw bytes do not match the candidate")
    if value.get("byteSize") != expected_size:
        raise AbiValidationError("reflected candidate byteSize does not match")
    if value.get("encoding") != binding.get("encoding"):
        raise AbiValidationError("candidate reflection encoding changed")
    if normalize_stage(value.get("stage")) != binding.get("stage"):
        raise AbiValidationError("candidate reflection stage does not match")
    if value.get("entryPoint") != binding.get("entryPoint"):
        raise AbiValidationError("candidate reflection entry point does not match")
    if value.get("reflectedEntryPoint") != binding.get("entryPoint"):
        raise AbiValidationError("ShaderReflection entry point does not match")
    for field in (
        "buildTargetShaderSucceeded",
        "reflectionSucceeded",
        "freeTargetResourceSucceeded",
    ):
        if value.get(field) is not True:
            raise AbiValidationError("reflectionResponse.%s must be true" % field)

    candidate_interface = _mapping(
        value.get("compiledInterface"), "compiledInterface"
    )
    baseline_requires_ambiguous_access = any(
        block.layout_completeness == "ambiguous"
        for block in baseline.constant_blocks
    )
    projected_dxil_cbuffer_completeness = False
    if candidate_interface.get("interfaceComplete") is not True:
        # RenderDoc deliberately reports a DXIL interface as incomplete when
        # DXC cannot provide source-member type annotations for one or more
        # CBuffers.  This one candidate-only projection makes the already
        # implemented physical-access gate reachable; it does not relax the
        # public ABI parser or manufacture fields/names.  Every non-CBuffer
        # fact is still parsed strictly below, and compare_shader_abi() still
        # requires exact independently extracted byte-offset/width/type tuples.
        if candidate_interface.get("interfaceComplete") is not False:
            raise AbiValidationError(
                "compiledInterface.interfaceComplete must be true"
            )
        if (
            binding.get("api") != "d3d12"
            or binding.get("encoding") != "dxil"
            or not baseline_requires_ambiguous_access
            or baseline_dxil_access is None
            or candidate_dxil_access is None
        ):
            raise AbiValidationError(
                "compiledInterface.interfaceComplete must be true"
            )
        if (
            baseline_dxil_access.signature_sha256
            != candidate_dxil_access.signature_sha256
        ):
            raise AbiValidationError(
                "incomplete DXIL CBuffer interfaces require exact access signatures"
            )

        raw_blocks = _required_array(
            candidate_interface,
            "compiledInterface",
            ("constantBlocks", "constantBuffers"),
            MAX_CONSTANT_BLOCKS,
        )
        ambiguous_blocks = 0
        for index, raw_block in enumerate(raw_blocks):
            block_path = "compiledInterface.constantBlocks[%d]" % index
            block = _mapping(raw_block, block_path)
            completeness = block.get("layoutCompleteness")
            if completeness not in {"complete", "ambiguous"}:
                raise AbiValidationError(
                    "%s.layoutCompleteness cannot be projected" % block_path
                )
            if completeness == "complete":
                if "layoutEvidence" in block:
                    raise AbiValidationError(
                        "%s.layoutEvidence contradicts a complete layout"
                        % block_path
                    )
                continue

            ambiguous_blocks += 1
            array_size = _alias(
                block,
                block_path,
                ("arraySize", "bindArraySize"),
                lambda raw, raw_path: _integer(raw, raw_path, minimum=1),
            )
            if array_size != 1:
                raise AbiValidationError(
                    "%s ambiguous DXIL arrays are not supported" % block_path
                )
            _alias(
                block,
                block_path,
                ("byteSize", "size"),
                lambda raw, raw_path: _integer(raw, raw_path, minimum=1),
            )
            layout_evidence = _mapping(
                block.get("layoutEvidence"), block_path + ".layoutEvidence"
            )
            if layout_evidence.get("kind") != "dxc-dxil-buffer-definition":
                raise AbiValidationError(
                    "%s.layoutEvidence.kind is invalid" % block_path
                )
            annotation_status = layout_evidence.get("typeAnnotation")
            if annotation_status not in {"absent", "unavailable"}:
                raise AbiValidationError(
                    "%s.layoutEvidence.typeAnnotation is invalid" % block_path
                )
            has_source_hash = "sourceSha256" in layout_evidence
            has_access_hash = "accessSignatureSha256" in layout_evidence
            if has_source_hash != has_access_hash or (
                annotation_status == "absent" and not has_source_hash
            ):
                raise AbiValidationError(
                    "%s.layoutEvidence hashes are incomplete" % block_path
                )
            if has_source_hash:
                source_hash = layout_evidence.get("sourceSha256")
                access_hash = layout_evidence.get("accessSignatureSha256")
                if not isinstance(source_hash, str) or not SHA256_RE.fullmatch(
                    source_hash
                ):
                    raise AbiValidationError(
                        "%s.layoutEvidence.sourceSha256 is invalid" % block_path
                    )
                if access_hash != candidate_dxil_access.signature_sha256:
                    raise AbiValidationError(
                        "%s.layoutEvidence access signature changed" % block_path
                    )
        if not ambiguous_blocks:
            raise AbiValidationError(
                "incomplete DXIL interface has no ambiguous CBuffer"
            )

        candidate_interface = dict(candidate_interface)
        candidate_interface["interfaceComplete"] = True
        projected_dxil_cbuffer_completeness = True

    candidate = parse_shader_abi(
        candidate_interface,
        expected_stage=str(binding.get("stage")),
        expected_entry_point=str(binding.get("entryPoint")),
    )
    if binding.get("api") == "d3d11" and baseline.dynamic_linkage_interface_count != 0:
        raise AbiValidationError(
            "D3D11 dynamic-linkage baselines are not supported by this ABI gate"
        )
    if binding.get("api") == "d3d11" and candidate.dynamic_linkage_interface_count != 0:
        raise AbiValidationError(
            "candidate uses unsupported D3D11 dynamic-linkage interfaces"
        )
    effective_baseline_access = (
        baseline_dxil_access if baseline_requires_ambiguous_access else None
    )
    effective_candidate_access = (
        candidate_dxil_access if baseline_requires_ambiguous_access else None
    )
    comparison = compare_shader_abi(
        baseline,
        candidate,
        baseline_dxil_access=effective_baseline_access,
        candidate_dxil_access=effective_candidate_access,
    )
    evidence = {
        "requestId": request_value.get("requestId"),
        "responseSha256": _digest(dict(value)),
        "rawSha256": expected_hash,
        "byteSize": expected_size,
        "stage": binding.get("stage"),
        "entryPoint": binding.get("entryPoint"),
        "api": binding.get("api"),
        "encoding": binding.get("encoding"),
        "targetProfile": binding.get("targetProfile"),
        "buildTargetShaderSucceeded": True,
        "reflectionSucceeded": True,
        "freeTargetResourceSucceeded": True,
        "reflectedRawSha256": value.get("reflectedRawSha256"),
        "compiledInterface": candidate.to_dict(),
    }
    if projected_dxil_cbuffer_completeness:
        evidence["interfaceCompletenessProjection"] = {
            "scope": "dxil-cbuffer-type-annotation-only",
            "rawInterfaceComplete": False,
            "projectedInterfaceComplete": True,
            "baselineAccessSignatureSha256": (
                baseline_dxil_access.signature_sha256
                if baseline_dxil_access is not None
                else None
            ),
            "candidateAccessSignatureSha256": (
                candidate_dxil_access.signature_sha256
                if candidate_dxil_access is not None
                else None
            ),
            "doesNotReconstructFieldsOrNames": True,
        }
    if effective_baseline_access is not None or effective_candidate_access is not None:
        evidence["dxilCBufferAccess"] = {
            "baseline": (
                effective_baseline_access.to_dict()
                if effective_baseline_access is not None
                else None
            ),
            "candidate": (
                effective_candidate_access.to_dict()
                if effective_candidate_access is not None
                else None
            ),
            "scope": "ambiguous-layout-physical-access-only",
            "doesNotReconstructFieldsOrNames": True,
        }
    return candidate, comparison, evidence
