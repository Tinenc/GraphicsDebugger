"""Strict physical CBuffer access extraction for RenderDoc's DXC DXIL text.

This is the Python 3.6-compatible Bridge counterpart of the Host parser in
``shader_workspace.adapters.dxil_access_signature``.  It is intentionally not
an LLVM parser: only fixed, statically-bound forms emitted by Microsoft's DXC
printer are accepted.  Unproven dynamic indices, indirect use, contradictions, or accesses outside
the reflected CBuffer byte envelope fails closed.

The Bridge runs this parser on disassembly obtained directly from the live
ReplayController.  Caller-provided ABI reports or hashes never enter this
calculation.
"""

import hashlib
import json
import re


DXIL_CBUFFER_ACCESS_SCHEMA = "renderdoc-ultra-dxil-cbuffer-access/v1"
DXIL_CBUFFER_ACCESS_VERSION = 1
MAX_IR_BYTES = 64 * 1024 * 1024
MAX_IR_LINES = 1000000
MAX_LINE_CHARS = 64 * 1024
MAX_METADATA_NODES = 131072
MAX_CBUFFER_RECORDS = 16384
MAX_HANDLES = 262144
MAX_LOADS = 262144
MAX_ACCESSES = 262144
MAX_CBUFFER_BYTES = 1 << 30
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DXIL_TRIPLE_RE = re.compile(
    r'(?im)^\s*target\s+triple\s*=\s*"dxil(?:-[^"]*)?"\s*$'
)
_SSA = r'%(?:[-A-Za-z$._0-9]+|"(?:[^"\\]|\\.)+")'
_SCALAR_WIDTHS = {
    "i16": 2,
    "f16": 2,
    "i32": 4,
    "f32": 4,
    "i64": 8,
    "f64": 8,
}
_LLVM_SCALARS = {
    "i16": "i16",
    "f16": "half",
    "i32": "i32",
    "f32": "float",
    "i64": "i64",
    "f64": "double",
}


class DxilAccessSignatureError(ValueError):
    """DXIL text did not provide complete, unambiguous physical evidence."""


def _canonical_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


class DxilCBufferAccess(object):
    __slots__ = ("byte_offset", "byte_width", "return_type")

    def __init__(self, byte_offset, byte_width, return_type):
        self.byte_offset = byte_offset
        self.byte_width = byte_width
        self.return_type = return_type

    @property
    def byte_end(self):
        return self.byte_offset + self.byte_width

    def sort_key(self):
        return (self.byte_offset, self.byte_width, self.return_type)

    def to_dict(self):
        return {
            "byteOffset": self.byte_offset,
            "byteWidth": self.byte_width,
            "returnType": self.return_type,
        }

    def __hash__(self):
        return hash(self.sort_key())

    def __eq__(self, other):
        return (
            isinstance(other, DxilCBufferAccess)
            and self.sort_key() == other.sort_key()
        )


class DxilCBufferAccessBlock(object):
    __slots__ = (
        "space", "bind", "bind_array_size", "byte_size",
        "envelope_start", "envelope_end", "accesses",
    )

    def __init__(self, space, bind, bind_array_size, byte_size,
                 envelope_start, envelope_end, accesses):
        self.space = space
        self.bind = bind
        self.bind_array_size = bind_array_size
        self.byte_size = byte_size
        self.envelope_start = envelope_start
        self.envelope_end = envelope_end
        self.accesses = tuple(accesses)

    def physical_dict(self):
        return {
            "space": self.space,
            "bind": self.bind,
            "bindArraySize": self.bind_array_size,
            "byteSize": self.byte_size,
            "accessEnvelope": {
                "byteOffset": self.envelope_start,
                "byteEnd": self.envelope_end,
            },
            "accesses": [item.to_dict() for item in self.accesses],
        }


class DxilCBufferAccessSignature(object):
    __slots__ = ("source_sha256", "source_byte_size", "blocks")

    def __init__(self, source_sha256, source_byte_size, blocks):
        self.source_sha256 = source_sha256
        self.source_byte_size = source_byte_size
        self.blocks = tuple(blocks)

    @property
    def signature_sha256(self):
        return hashlib.sha256(_canonical_json({
            "schema": DXIL_CBUFFER_ACCESS_SCHEMA,
            "schemaVersion": DXIL_CBUFFER_ACCESS_VERSION,
            "blocks": [block.physical_dict() for block in self.blocks],
        })).hexdigest()

    def block(self, space, bind):
        for item in self.blocks:
            if item.space == space and item.bind == bind:
                return item
        return None

    def to_dict(self):
        return {
            "schema": DXIL_CBUFFER_ACCESS_SCHEMA,
            "schemaVersion": DXIL_CBUFFER_ACCESS_VERSION,
            "sourceSha256": self.source_sha256,
            "sourceByteSize": self.source_byte_size,
            "signatureSha256": self.signature_sha256,
            "blocks": [block.physical_dict() for block in self.blocks],
        }


class _CBufferRecord(object):
    __slots__ = ("resource_id", "space", "lower_bind", "bind_count", "byte_size", "resource_type")

    def __init__(self, resource_id, space, lower_bind, bind_count, byte_size, resource_type=""):
        self.resource_id = resource_id
        self.space = space
        self.lower_bind = lower_bind
        self.bind_count = bind_count
        self.byte_size = byte_size
        self.resource_type = resource_type


class _Handle(object):
    __slots__ = ("record", "bind", "annotated_size")

    def __init__(self, record, bind, annotated_size=None):
        self.record = record
        self.bind = bind
        self.annotated_size = annotated_size


class _LegacyLoad(object):
    __slots__ = (
        "result", "result_type", "return_type", "handle",
        "register_indices", "definition_line",
    )

    def __init__(self, result, result_type, return_type, handle,
                 register_indices, definition_line):
        self.result = result
        self.result_type = result_type
        self.return_type = return_type
        self.handle = handle
        self.register_indices = register_indices
        self.definition_line = definition_line


def _strip_comment(line):
    quoted = False
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quoted:
            escaped = True
            continue
        if character == '"':
            quoted = not quoted
        elif character == ";" and not quoted:
            return line[:index]
    return line


def _split_items(value, label):
    result = []
    start = 0
    depth = 0
    quoted = False
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quoted:
            escaped = True
            continue
        if character == '"':
            quoted = not quoted
            continue
        if quoted:
            continue
        if character in "{[(<":
            depth += 1
        elif character in "}])>":
            depth -= 1
            if depth < 0:
                raise DxilAccessSignatureError(
                    "%s has unbalanced delimiters" % label)
        elif character == "," and depth == 0:
            result.append(value[start:index].strip())
            start = index + 1
    if quoted or depth != 0:
        raise DxilAccessSignatureError("%s has unbalanced delimiters" % label)
    result.append(value[start:].strip())
    return result


def _metadata_items(value, label):
    match = re.fullmatch(r"!\{(.*)\}", value.strip())
    if match is None:
        raise DxilAccessSignatureError("%s is not a metadata tuple" % label)
    return _split_items(match.group(1), label)


def _metadata_ref(value, label):
    match = re.fullmatch(r"!(\d+)", value.strip())
    if match is None:
        raise DxilAccessSignatureError(
            "%s is not a metadata reference" % label)
    return int(match.group(1), 10)


def _typed_integer(value, llvm_type, label):
    match = re.fullmatch(r"%s\s+(\d+)" % re.escape(llvm_type), value.strip())
    if match is None:
        raise DxilAccessSignatureError(
            "%s is not a static %s" % (label, llvm_type))
    return int(match.group(1), 10)


def _parse_cbuffer_records(lines, allow_binding_arrays=False):
    nodes = {}
    resources = None
    for line in lines:
        code = _strip_comment(line).strip()
        if not code:
            continue
        named = re.fullmatch(r"!dx\.resources\s*=\s*(!\{.*\})", code)
        if named is not None:
            if resources is not None:
                raise DxilAccessSignatureError(
                    "duplicate !dx.resources metadata")
            resources = named.group(1)
            continue
        node = re.fullmatch(r"!(\d+)\s*=\s*(!\{.*\})", code)
        if node is not None:
            identity = int(node.group(1), 10)
            if identity in nodes:
                raise DxilAccessSignatureError(
                    "duplicate metadata node !%d" % identity)
            if len(nodes) >= MAX_METADATA_NODES:
                raise DxilAccessSignatureError(
                    "DXIL metadata exceeds its node limit")
            nodes[identity] = node.group(2)
    if resources is None:
        raise DxilAccessSignatureError(
            "DXIL text has no !dx.resources metadata")
    resource_items = _metadata_items(resources, "!dx.resources")
    if len(resource_items) == 1:
        root_id = _metadata_ref(resource_items[0], "!dx.resources[0]")
        if root_id not in nodes:
            raise DxilAccessSignatureError("DXIL resource root is missing")
        resource_items = _metadata_items(nodes[root_id], "resource root")
    if len(resource_items) != 4:
        raise DxilAccessSignatureError(
            "DXIL resource root must contain four classes")
    cbv_value = resource_items[2]
    if cbv_value == "null":
        return {}
    cbv_id = _metadata_ref(cbv_value, "DXIL CBV list")
    if cbv_id not in nodes:
        raise DxilAccessSignatureError("DXIL CBV list is missing")
    cbv_items = _metadata_items(nodes[cbv_id], "DXIL CBV list")
    if len(cbv_items) > MAX_CBUFFER_RECORDS:
        raise DxilAccessSignatureError(
            "DXIL CBuffer metadata exceeds its limit")
    records = {}
    bindings = set()
    for index, item in enumerate(cbv_items):
        record_id = _metadata_ref(item, "DXIL CBV list item")
        if record_id not in nodes:
            raise DxilAccessSignatureError("DXIL CBuffer record is missing")
        values = _metadata_items(nodes[record_id], "DXIL CBuffer record")
        if len(values) < 7:
            raise DxilAccessSignatureError("DXIL CBuffer record is truncated")
        resource_id = _typed_integer(values[0], "i32", "CBuffer resource ID")
        space = _typed_integer(values[3], "i32", "CBuffer register space")
        lower = _typed_integer(values[4], "i32", "CBuffer lower binding")
        count = _typed_integer(values[5], "i32", "CBuffer binding count")
        byte_size = _typed_integer(values[6], "i32", "CBuffer byte size")
        if count != 1 and not allow_binding_arrays:
            raise DxilAccessSignatureError(
                "CBuffer binding arrays are not safe for ambiguous-layout repair")
        if not 1 <= count <= MAX_CBUFFER_RECORDS or lower + count > 0x100000000:
            raise DxilAccessSignatureError("CBuffer binding range is outside its limit")
        if not 1 <= byte_size <= MAX_CBUFFER_BYTES:
            raise DxilAccessSignatureError(
                "CBuffer byte size is outside its limit")
        binding = (space, lower)
        if resource_id in records or binding in bindings:
            raise DxilAccessSignatureError("duplicate DXIL CBuffer identity")
        if resource_id != index:
            raise DxilAccessSignatureError(
                "DXIL CBuffer resource IDs are not in fixed range order")
        records[resource_id] = _CBufferRecord(
            resource_id, space, lower, count, byte_size, values[1])
        bindings.add(binding)
    ranges = sorted((value.space, value.lower_bind, value.lower_bind + value.bind_count)
                    for value in records.values())
    for previous, current in zip(ranges, ranges[1:]):
        if previous[0] == current[0] and current[1] < previous[2]:
            raise DxilAccessSignatureError("overlapping DXIL CBuffer binding ranges")
    return records


def _parse_cbuf_return_types(lines):
    result = {}
    pattern = re.compile(
        r"^%dx\.types\.CBufRet\.([A-Za-z0-9]+)\s*=\s*"
        r"type\s*\{(.*)\}\s*$"
    )
    for line in lines:
        code = _strip_comment(line).strip()
        match = pattern.fullmatch(code)
        if match is None:
            continue
        suffix = match.group(1)
        expected_scalar = _LLVM_SCALARS.get(suffix)
        if expected_scalar is None:
            continue
        if suffix in result:
            raise DxilAccessSignatureError(
                "duplicate CBufRet type definition")
        scalars = _split_items(match.group(2), "CBufRet type")
        if not scalars or any(value != expected_scalar for value in scalars):
            raise DxilAccessSignatureError(
                "CBufRet has an unexpected element layout")
        width = _SCALAR_WIDTHS[suffix]
        widths = tuple(width for unused in scalars)
        if sum(widths) > 16:
            raise DxilAccessSignatureError(
                "CBufRet exceeds one legacy register")
        result[suffix] = widths
    return result


def _static_handle(handles, token, label):
    if token not in handles:
        raise DxilAccessSignatureError(
            "%s is not a statically bound CBuffer handle" % label)
    return handles[token]


def _bounded_register_ranges(lines):
    """Prove only unsigned i32 UMin(x, constant) plus non-wrapping constants.

    An unknown operand is never itself a range proof. The UMin instruction
    supplies the finite envelope; additions must fit uint32 without wrapping.
    This is an access envelope, not proof of shader numerical equivalence.
    """
    ranges = {}
    umin = re.compile(
        r"^(%s)\s*=\s*(?:tail\s+)?call\s+i32\s+@dx\.op\.binary\.i32"
        r"\(i32\s+40,\s+i32\s+%s,\s+i32\s+(\d+)\)\s*$" % (_SSA, _SSA)
    )
    addition = re.compile(
        r"^(%s)\s*=\s*add(?P<flags>(?:\s+(?:nuw|nsw))*)\s+i32\s+(%s),\s*(\d+)\s*$"
        % (_SSA, _SSA)
    )
    for line in lines:
        code = _strip_comment(line).strip()
        match = umin.fullmatch(code)
        if match is not None:
            result, upper = match.groups()
            upper = int(upper, 10)
            if upper >= 4096:
                continue
            if result in ranges:
                raise DxilAccessSignatureError("duplicate bounded index definition")
            ranges[result] = (0, upper)
            continue
        match = addition.fullmatch(code)
        if match is not None:
            result, flags, operand, constant = match.groups()
            if operand not in ranges:
                continue
            lower, upper = ranges[operand]
            constant = int(constant, 10)
            if constant > 0xFFFFFFFF or upper + constant > 0xFFFFFFFF:
                continue
            if "nsw" in flags and upper + constant > 0x7FFFFFFF:
                continue
            if result in ranges:
                raise DxilAccessSignatureError("duplicate bounded index definition")
            ranges[result] = (lower + constant, upper + constant)
    return ranges


def _register_indices(token, ranges):
    if token.isdigit():
        value = int(token, 10)
        if value <= 0xFFFFFFFF:
            return (value,)
    elif token in ranges:
        lower, upper = ranges[token]
        return tuple(range(lower, upper + 1))
    raise DxilAccessSignatureError(
        "CBufferLoadLegacy is not a supported static DXC form or proven bounded index")


def extract_dxil_cbuffer_access_signature(source, expected_sha256=None):
    """Return exact static CBuffer accesses from hash-bound DXIL LLVM text."""

    if not isinstance(source, bytes) or not 1 <= len(source) <= MAX_IR_BYTES:
        raise DxilAccessSignatureError(
            "DXIL access evidence must contain 1..%d bytes" % MAX_IR_BYTES)
    source_sha256 = hashlib.sha256(source).hexdigest()
    if expected_sha256 is not None:
        if (not isinstance(expected_sha256, str) or
                _SHA256_RE.fullmatch(expected_sha256) is None):
            raise DxilAccessSignatureError(
                "expected DXIL evidence hash is invalid")
        if source_sha256 != expected_sha256:
            raise DxilAccessSignatureError(
                "DXIL access evidence hash mismatch")
    if b"\x00" in source:
        raise DxilAccessSignatureError(
            "DXIL access evidence contains NUL bytes")
    try:
        text = source.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise DxilAccessSignatureError(
            "DXIL access evidence is not strict UTF-8")
    if _DXIL_TRIPLE_RE.search(text) is None:
        raise DxilAccessSignatureError(
            "DXIL access evidence has no DXIL target triple")
    lines = text.splitlines()
    if (len(lines) > MAX_IR_LINES or
            any(len(line) > MAX_LINE_CHARS for line in lines)):
        raise DxilAccessSignatureError(
            "DXIL access evidence exceeds text bounds")

    bounded_ranges = _bounded_register_ranges(lines)
    records = _parse_cbuffer_records(lines)
    return_types = _parse_cbuf_return_types(lines)
    handles = {}
    legacy_loads = {}
    accesses = {}

    old_handle = re.compile(
        r"^(%s)\s*=\s*(?:tail\s+)?call\s+%%dx\.types\.Handle\s+"
        r"@dx\.op\.createHandle\(i32\s+57,\s+i8\s+2,\s+i32\s+(\d+),\s+"
        r"i32\s+(\d+),\s+i1\s+false\)\s*$" % _SSA
    )
    binding_handle = re.compile(
        r"^(%s)\s*=\s*(?:tail\s+)?call\s+%%dx\.types\.Handle\s+"
        r"@dx\.op\.createHandleFromBinding\(i32\s+217,\s+"
        r"%%dx\.types\.ResBind\s+\{\s*i32\s+(\d+),\s+i32\s+(\d+),\s+"
        r"i32\s+(\d+),\s+i8\s+2\s*\},\s+i32\s+(\d+),\s+i1\s+false\)\s*$"
        % _SSA
    )
    annotate = re.compile(
        r"^(%s)\s*=\s*(?:tail\s+)?call\s+%%dx\.types\.Handle\s+"
        r"@dx\.op\.annotateHandle\(i32\s+216,\s+%%dx\.types\.Handle\s+(%s),\s+"
        r"%%dx\.types\.ResourceProperties\s+\{\s*i32\s+\d+,\s+i32\s+(\d+)\s*\}\)\s*$"
        % (_SSA, _SSA)
    )
    legacy = re.compile(
        r"^(%s)\s*=\s*(?:tail\s+)?call\s+%%dx\.types\.CBufRet\.([A-Za-z0-9]+)\s+"
        r"@dx\.op\.cbufferLoadLegacy\.([A-Za-z0-9]+)\(i32\s+59,\s+"
        r"%%dx\.types\.Handle\s+(%s),\s+i32\s+(%s|\d+)\)\s*$"
        % (_SSA, _SSA, _SSA)
    )
    scalar = re.compile(
        r"^(?:%s\s*=\s*)?(?:tail\s+)?call\s+(half|float|double|i16|i32|i64)\s+"
        r"@dx\.op\.cbufferLoad\.([A-Za-z0-9]+)\(i32\s+58,\s+"
        r"%%dx\.types\.Handle\s+(%s),\s+i32\s+(\d+),\s+i32\s+(\d+)\)\s*$"
        % (_SSA, _SSA)
    )

    for line_number, line in enumerate(lines):
        code = _strip_comment(line).strip()
        if not code or code.startswith("declare "):
            continue
        match = old_handle.fullmatch(code)
        if match is not None:
            result, resource_id_text, bind_text = match.groups()
            if result in handles or len(handles) >= MAX_HANDLES:
                raise DxilAccessSignatureError(
                    "duplicate or excessive DXIL handles")
            resource_id = int(resource_id_text, 10)
            bind = int(bind_text, 10)
            if resource_id not in records:
                raise DxilAccessSignatureError(
                    "CreateHandle CBuffer range is missing")
            record = records[resource_id]
            if bind != record.lower_bind:
                raise DxilAccessSignatureError(
                    "CreateHandle binding contradicts metadata")
            handles[result] = _Handle(record, bind)
            continue
        match = binding_handle.fullmatch(code)
        if match is not None:
            result, lower_text, upper_text, space_text, bind_text = match.groups()
            if result in handles or len(handles) >= MAX_HANDLES:
                raise DxilAccessSignatureError(
                    "duplicate or excessive DXIL handles")
            lower = int(lower_text, 10)
            upper = int(upper_text, 10)
            space = int(space_text, 10)
            bind = int(bind_text, 10)
            matching = [
                record for record in records.values()
                if record.space == space and record.lower_bind == lower
            ]
            if upper != lower or bind != lower or len(matching) != 1:
                raise DxilAccessSignatureError(
                    "CreateHandleFromBinding is dynamic or contradicts metadata")
            handles[result] = _Handle(matching[0], bind)
            continue
        match = annotate.fullmatch(code)
        if match is not None:
            result, source_handle, size_text = match.groups()
            if source_handle not in handles:
                continue
            if result in handles or len(handles) >= MAX_HANDLES:
                raise DxilAccessSignatureError(
                    "duplicate or excessive DXIL handles")
            handle = _static_handle(
                handles, source_handle, "AnnotateHandle source")
            size = int(size_text, 10)
            if size != handle.record.byte_size:
                raise DxilAccessSignatureError(
                    "AnnotateHandle size contradicts metadata")
            handles[result] = _Handle(handle.record, handle.bind, size)
            continue
        if "@dx.op.cbufferLoadLegacy" in code:
            match = legacy.fullmatch(code)
            if match is None:
                raise DxilAccessSignatureError(
                    "CBufferLoadLegacy is not a supported static DXC form")
            result, result_type, overload, handle_token, register_text = match.groups()
            if result_type != overload or result_type not in return_types:
                raise DxilAccessSignatureError(
                    "CBufferLoadLegacy return type is inconsistent")
            if result in legacy_loads or len(legacy_loads) >= MAX_LOADS:
                raise DxilAccessSignatureError(
                    "duplicate or excessive legacy CBuffer loads")
            legacy_loads[result] = _LegacyLoad(
                result, result_type, overload,
                _static_handle(
                    handles, handle_token, "CBufferLoadLegacy handle"),
                _register_indices(register_text, bounded_ranges), line_number)
            continue
        if "@dx.op.cbufferLoad" in code:
            match = scalar.fullmatch(code)
            if match is None:
                raise DxilAccessSignatureError(
                    "CBufferLoad is not a supported static DXC form")
            llvm_type, overload, handle_token, offset_text, alignment_text = (
                match.groups())
            if _LLVM_SCALARS.get(overload) != llvm_type:
                raise DxilAccessSignatureError(
                    "CBufferLoad return type is inconsistent")
            width = _SCALAR_WIDTHS[overload]
            offset = int(offset_text, 10)
            alignment = int(alignment_text, 10)
            if alignment <= 0 or alignment & (alignment - 1):
                raise DxilAccessSignatureError(
                    "CBufferLoad alignment is invalid")
            handle = _static_handle(
                handles, handle_token, "CBufferLoad handle")
            access = DxilCBufferAccess(offset, width, overload)
            if access.byte_end > handle.record.byte_size:
                raise DxilAccessSignatureError(
                    "CBufferLoad exceeds its CBuffer envelope")
            accesses.setdefault(
                (handle.record.space, handle.bind), set()).add(access)
            if sum(len(value) for value in accesses.values()) > MAX_ACCESSES:
                raise DxilAccessSignatureError(
                    "DXIL CBuffer accesses exceed their limit")

    extract = re.compile(
        r"^(?:%s\s*=\s*)?extractvalue\s+%%dx\.types\.CBufRet\.([A-Za-z0-9]+)\s+"
        r"(%s),\s*(\d+)(?:\s*,\s*!.*)?\s*$" % (_SSA, _SSA)
    )
    codes, legacy_users = _index_legacy_users(lines, legacy_loads)
    for result, load in legacy_loads.items():
        lanes = set()
        for line_number in legacy_users.get(result, ()):
            if line_number == load.definition_line:
                continue
            code = codes[line_number]
            match = extract.fullmatch(code)
            if match is None or match.group(2) != result:
                raise DxilAccessSignatureError(
                    "CBufferLoadLegacy result has a non-extractvalue user")
            result_type, unused_result, lane_text = match.groups()
            if result_type != load.result_type:
                raise DxilAccessSignatureError(
                    "extractvalue CBufRet type is inconsistent")
            lanes.add(int(lane_text, 10))
        if not lanes:
            raise DxilAccessSignatureError(
                "CBufferLoadLegacy has no provable extracted lane")
        widths = return_types[load.result_type]
        for lane in lanes:
            if lane >= len(widths):
                raise DxilAccessSignatureError(
                    "CBufferLoadLegacy lane is out of range")
            lane_offset = sum(widths[:lane])
            for register_index in load.register_indices:
                access = DxilCBufferAccess(
                    register_index * 16 + lane_offset,
                    widths[lane], load.return_type)
                if access.byte_end > load.handle.record.byte_size:
                    raise DxilAccessSignatureError(
                        "CBufferLoadLegacy lane exceeds its CBuffer envelope")
                accesses.setdefault(
                    (load.handle.record.space, load.handle.bind), set()).add(access)
                if sum(len(value) for value in accesses.values()) > MAX_ACCESSES:
                    raise DxilAccessSignatureError(
                        "DXIL CBuffer accesses exceed their limit")

    blocks = []
    by_binding = {
        (item.space, item.lower_bind): item for item in records.values()
    }
    for binding, values in sorted(accesses.items()):
        space, bind = binding
        record = by_binding.get((space, bind))
        if record is None or not values:
            raise DxilAccessSignatureError(
                "CBuffer access has no exact metadata binding")
        ordered = tuple(sorted(values, key=lambda item: item.sort_key()))
        blocks.append(DxilCBufferAccessBlock(
            space, bind, record.bind_count, record.byte_size,
            min(item.byte_offset for item in ordered),
            max(item.byte_end for item in ordered), ordered))
    return DxilCBufferAccessSignature(source_sha256, len(source), tuple(blocks))


def _index_legacy_users(lines, legacy_loads):
    """Index every matching SSA occurrence once, including overlapping names.

    The lookahead preserves the old ASCII token boundaries and also examines
    tokens embedded in quoted SSA names. Unknown consumers must still reject.
    """
    token = re.compile(
        r"(?=(?<![-A-Za-z$._0-9])(%s)(?![-A-Za-z$._0-9]))" % _SSA)
    codes = [_strip_comment(line).strip() for line in lines]
    users = {}
    for index, code in enumerate(codes):
        seen = set()
        for match in token.finditer(code):
            value = match.group(1)
            if value in legacy_loads and value not in seen:
                users.setdefault(value, []).append(index)
                seen.add(value)
    return codes, users


def extract_dxil_annotated_cbuffer_bindings(source, expected_sha256=None):
    """Prove source type annotations independently of optional access analysis.

    This never proves access equivalence or repairs a layout. It only prevents
    a complete native reflection from being downgraded when the narrower
    access parser cannot handle a valid program (for example CBV arrays).
    """
    if not isinstance(source, bytes) or not 1 <= len(source) <= MAX_IR_BYTES:
        raise DxilAccessSignatureError("DXIL type evidence size is outside its limit")
    digest = hashlib.sha256(source).hexdigest()
    if expected_sha256 is not None and (
            not isinstance(expected_sha256, str) or
            _SHA256_RE.fullmatch(expected_sha256) is None or digest != expected_sha256):
        raise DxilAccessSignatureError("DXIL type evidence hash mismatch")
    try:
        text = source.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise DxilAccessSignatureError("DXIL type evidence is not strict UTF-8")
    if "\x00" in text or _DXIL_TRIPLE_RE.search(text) is None:
        raise DxilAccessSignatureError("DXIL type evidence has no valid target triple")
    lines = text.splitlines()
    if len(lines) > MAX_IR_LINES or any(len(line) > MAX_LINE_CHARS for line in lines):
        raise DxilAccessSignatureError("DXIL type evidence exceeds text bounds")
    records = _parse_cbuffer_records(lines, allow_binding_arrays=True)
    nodes, structures = {}, {}
    roots = None
    for line in lines:
        code = _strip_comment(line).strip()
        named = re.fullmatch(r"!dx\.typeAnnotations\s*=\s*(!\{.*\})", code)
        if named is not None:
            if roots is not None:
                raise DxilAccessSignatureError("duplicate DXIL type annotation root")
            roots = _metadata_items(named.group(1), "DXIL type annotations")
        node = re.fullmatch(r"!(\d+)\s*=\s*(!\{.*\})", code)
        if node is not None:
            identity = int(node.group(1), 10)
            if identity in nodes or len(nodes) >= MAX_METADATA_NODES:
                raise DxilAccessSignatureError("duplicate or excessive DXIL metadata")
            nodes[identity] = node.group(2)
        structure = re.fullmatch(r"(" + _SSA + r")\s*=\s*type\s*\{(.*)\}", code)
        if structure is not None:
            name, body = structure.groups()
            if name in structures:
                raise DxilAccessSignatureError("duplicate DXIL structure type")
            structures[name] = _split_items(body, "DXIL structure") if body.strip() else []
    if roots is None:
        return ()

    def metadata(value):
        identity = _metadata_ref(value, "DXIL type annotation")
        if identity not in nodes:
            raise DxilAccessSignatureError("DXIL type annotation node is missing")
        return _metadata_items(nodes[identity], "DXIL type annotation")

    annotations = {}
    for root in roots:
        items = metadata(root)
        kind = _typed_integer(items[0], "i32", "DXIL annotation kind")
        if kind != 0:
            continue
        if len(items) % 2 != 1:
            raise DxilAccessSignatureError("DXIL structure annotations are truncated")
        for index in range(1, len(items), 2):
            match = re.fullmatch(r"(" + _SSA + r")\s+undef", items[index])
            if match is None:
                raise DxilAccessSignatureError("DXIL annotated structure type is invalid")
            name = match.group(1)
            if name in annotations:
                raise DxilAccessSignatureError("duplicate DXIL structure annotation")
            fields = metadata(items[index + 1])
            byte_size = _typed_integer(fields[0], "i32", "DXIL annotated byte size")
            shape = structures.get(name)
            if shape is None or len(fields) - 1 != len(shape):
                raise DxilAccessSignatureError("DXIL structure annotation contradicts its fields")
            for field in fields[1:]:
                values = metadata(field)
                if len(values) % 2:
                    raise DxilAccessSignatureError("DXIL field annotation is truncated")
                offsets = []
                for tag_index in range(0, len(values), 2):
                    tag = _typed_integer(values[tag_index], "i32", "DXIL field annotation tag")
                    if tag == 3:
                        offsets.append(_typed_integer(values[tag_index + 1], "i32", "DXIL field offset"))
                if len(offsets) != 1 or offsets[0] > byte_size:
                    raise DxilAccessSignatureError("DXIL field offset is missing or outside its type")
            annotations[name] = byte_size
    result = []
    for record in records.values():
        token = record.resource_type
        array = re.fullmatch(r"\[\s*(\d+)\s+x\s+(" + _SSA + r")\s*\]\s*\*\s+undef", token)
        scalar = re.fullmatch(r"(" + _SSA + r")\s*\*\s+undef", token)
        if array is not None and int(array.group(1), 10) == record.bind_count:
            name = array.group(2)
        elif scalar is not None and record.bind_count == 1:
            name = scalar.group(1)
        else:
            raise DxilAccessSignatureError("DXIL CBuffer type contradicts its binding range")
        if name not in annotations:
            continue
        if annotations[name] != record.byte_size:
            raise DxilAccessSignatureError("DXIL CBuffer annotation size contradicts reflection")
        result.append((record.space, record.lower_bind, record.byte_size))
    return tuple(sorted(result))


def extract_dxil_cbuffer_evidence(source, expected_sha256=None):
    """Parse accesses once; unavailable layout remains explicit and lower-only."""
    access = extract_dxil_cbuffer_access_signature(source, expected_sha256)
    try:
        absent = _unannotated_from_validated_source(source)
    except DxilAccessSignatureError:
        absent = None
    return access, absent


def extract_dxil_unannotated_cbuffer_bindings(source, expected_sha256=None):
    """Return CBuffer bindings whose DXC definition has no type annotation.

    This consumes the same strict, hash-bound DXC DXIL text as the access
    extractor.  It deliberately does not inspect reflected/display names.
    Resource metadata establishes the binding and byte envelope; the fixed
    ``Buffer Definitions`` section establishes only whether a source type
    annotation exists.  Any count/order/size contradiction fails closed.

    The result is a tuple of ``(space, bind, byte_size)`` values.  Callers must
    still compare :func:`extract_dxil_cbuffer_access_signature` results for the
    original and candidate binaries before treating an unannotated layout as
    replacement-compatible.
    """

    # Reuse the complete strict parser first.  Besides validating bounds,
    # encoding, target triple, resource metadata, handles and load forms, this
    # also verifies ``expected_sha256`` without creating a second trust lane.
    extract_dxil_cbuffer_access_signature(
        source, expected_sha256=expected_sha256)
    return _unannotated_from_validated_source(source)


def _unannotated_from_validated_source(source):
    """Private continuation after strict hash-bound access extraction."""
    try:
        text = source.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise DxilAccessSignatureError(
            "DXIL layout evidence is not strict UTF-8")
    lines = text.splitlines()
    records = _parse_cbuffer_records(lines)
    if not records:
        return tuple()

    definitions_start = None
    definitions_end = None
    for index, line in enumerate(lines):
        if re.fullmatch(r"\s*;\s*Buffer Definitions:\s*", line):
            if definitions_start is not None:
                raise DxilAccessSignatureError(
                    "duplicate DXIL Buffer Definitions section")
            definitions_start = index + 1
            continue
        if (definitions_start is not None and
                re.fullmatch(r"\s*;\s*Resource Bindings:\s*", line)):
            definitions_end = index
            break
    if definitions_start is None or definitions_end is None:
        raise DxilAccessSignatureError(
            "DXIL text has no complete Buffer Definitions section")

    section = lines[definitions_start:definitions_end]
    starts = []
    for index, line in enumerate(section):
        if re.fullmatch(r"\s*;\s*cbuffer(?:\s+[^{}]*)?\s*", line):
            starts.append(index)
    ordered_records = [records[index] for index in sorted(records)]
    if len(starts) != len(ordered_records):
        raise DxilAccessSignatureError(
            "DXIL CBuffer definitions contradict resource metadata")

    absent = []
    for position, (start, record) in enumerate(zip(starts, ordered_records)):
        end = starts[position + 1] if position + 1 < len(starts) else len(section)
        # A following resource-bind-info definition is not part of the CBuffer
        # definition and may itself contain the same absence marker.
        for index in range(start + 1, end):
            if re.fullmatch(r"\s*;\s*Resource bind info(?:\s+.*)?", section[index]):
                end = index
                break
        body = "\n".join(section[start:end])
        markers = re.findall(
            r"\[\s*(\d+)\s+x\s+i8\s*\]\s*"
            r"\(\s*type annotation not present\s*\)",
            body,
            flags=re.IGNORECASE,
        )
        if not markers:
            continue
        if len(markers) != 1 or int(markers[0], 10) != record.byte_size:
            raise DxilAccessSignatureError(
                "DXIL unannotated CBuffer size contradicts resource metadata")
        absent.append((record.space, record.lower_bind, record.byte_size))
    return tuple(sorted(absent))
