"""Physical CBuffer envelopes from stripped SM4/5 DXBC declarations.

SHEX/SHDR gives register, space, binding count and float4 capacity, not source
member types. RDEF-bearing, DXIL and malformed containers never use this path.
Token definitions follow renderdoc/driver/shaders/dxbc/dxbc_bytecode_ops.cpp.
Compatible with the application's Python 3.6 runtime.
"""
import hashlib
import struct


def declarations(raw):
    if not isinstance(raw, bytes) or len(raw) < 36 or raw[:4] != b"DXBC":
        return {}
    try:
        total, count = struct.unpack_from("<II", raw, 24)
        if total != len(raw) or count > 128 or 32 + count * 4 > total:
            return {}
        chunks, ranges = {}, []
        for index in range(count):
            offset = struct.unpack_from("<I", raw, 32 + index * 4)[0]
            if offset < 32 + count * 4 or offset + 8 > total:
                return {}
            tag, size = struct.unpack_from("<4sI", raw, offset)
            end = offset + 8 + size
            if end > total or any(offset < b and a < end for a, b in ranges):
                return {}
            if tag in chunks:
                return {}
            ranges.append((offset, end))
            chunks[tag] = raw[offset + 8:end]
        if b"RDEF" in chunks or b"DXIL" in chunks:
            return {}
        programs = [chunks[k] for k in (b"SHDR", b"SHEX") if k in chunks]
        if len(programs) != 1 or len(programs[0]) < 8 or len(programs[0]) % 4:
            return {}
        words = struct.unpack("<%dI" % (len(programs[0]) // 4), programs[0])
        major, minor = (words[0] >> 4) & 15, words[0] & 15
        if major not in (4, 5) or minor > 1 or words[1] != len(words):
            return {}
        sm51 = major == 5 and minor == 1
        result, pos = {}, 2
        while pos < len(words):
            token = words[pos]
            opcode, length = token & 2047, (token >> 24) & 127
            if opcode == 53:  # CUSTOMDATA stores its length in the next word.
                length = words[pos + 1] if pos + 1 < len(words) else 0
            if length < 1 or pos + length > len(words):
                return {}
            if opcode in (158, 162):  # DCL_UAV_STRUCTURED / DCL_RESOURCE_STRUCTURED
                row = words[pos:pos + length]
                dimensions = 3 if sm51 else 1
                if token & 0x80000000 or len(row) != (7 if sm51 else 4):
                    return {}
                operand = row[1]
                if (operand & 0x80000000 or (operand >> 12) & 255 != (30 if opcode == 158 else 7) or
                        (operand >> 20) & 3 != dimensions or
                        any((operand >> (22 + 3*i)) & 7 for i in range(dimensions))):
                    return {}
                register, stride, space, count_bind = row[2], row[3], 0, 1
                if sm51:
                    register, last, stride, space = row[3:7]
                    if last < register or last == 0xFFFFFFFF: return {}
                    count_bind = last - register + 1
                key = ('uav' if opcode == 158 else 'srv', space, register)
                if stride <= 0 or stride > 2048 or stride % 4 or key in result: return {}
                result[key] = {'byteSize':stride, 'bindArraySize':count_bind,
                    'source':'dxbc-stripped-structured-buffer-declaration',
                    'rawSha256':hashlib.sha256(raw).hexdigest()}
            if opcode == 89:  # DCL_CONSTANT_BUFFER, immediate register indices.
                row = words[pos:pos + length]
                if token & 0x80000000 or len(row) != (7 if sm51 else 4):
                    return {}
                operand = row[1]
                dimensions = (operand >> 20) & 3
                if (operand & 0x80000000 or (operand >> 12) & 255 != 8 or
                        dimensions != (3 if sm51 else 2) or
                        any((operand >> (22 + 3 * i)) & 7 for i in range(dimensions))):
                    return {}
                if sm51:
                    register, last, vectors, space = row[3:7]
                    if last < register or last == 0xFFFFFFFF:
                        return {}  # Unbounded capacity cannot be proven here.
                    count_bind = last - register + 1
                else:
                    register, vectors, space, count_bind = row[2], row[3], 0, 1
                if vectors == 0 or vectors > 4096 or (space, register) in result:
                    return {}
                result[(space, register)] = {
                    "byteSize": vectors * 16, "bindArraySize": count_bind,
                    "source": "dxbc-stripped-constant-buffer-declaration",
                    "rawSha256": hashlib.sha256(raw).hexdigest(),
                    "shaderModel": "%d.%d" % (major, minor),
                }
            pos += length
        return result
    except (struct.error, IndexError, ValueError):
        return {}


def matches_synthetic_fields(block, evidence):
    """Recognize only RenderDoc's declaration-derived fallback, not typed RDEF."""
    if not evidence or block.get("byteSize") != evidence["byteSize"]:
        return False
    if block.get("bindArraySize") != evidence["bindArraySize"]:
        return False
    # Native array wrappers need their own proven shape; leave them strict.
    if evidence["bindArraySize"] != 1 or not block.get("bufferBacked"):
        return False
    fields = block.get("fields") or []
    if len(fields) != evidence["byteSize"] // 16:
        return False
    for index, field in enumerate(fields):
        info = field.get("typeInfo") or {}
        if (field.get("byteOffset") != index * 16 or field.get("byteSize") != 16 or
                field.get("rows") != 1 or field.get("columns") != 4 or
                field.get("elements") not in (0, 1) or field.get("members") or
                info.get("name") != "float4" or
                str(field.get("baseType")).lower() not in ("float", "vartype.float") or
                field.get("name") != "cb%d_v%d" % (block["bind"], index)):
            return False
    return True


def project_opaque_resource_types(available, candidates):
    """Project only proven opaque structured element envelopes for comparison."""
    for candidate in candidates:
        for captured in available:
            original_type = captured.get('variableType', {})
            if original_type.get('layoutCompleteness') != 'opaque': continue
            if any(captured.get(k) != candidate.get(k) for k in captured if k != 'variableType'): continue
            target_type = candidate.get('variableType', {})
            if (target_type.get('byteSize') == original_type.get('byteSize') and
                    target_type.get('arrayByteStride') == original_type.get('arrayByteStride') and
                    target_type.get('elements') == 1):
                candidate['variableType'] = dict(original_type)
                break
