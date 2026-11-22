"""Bounded native post-transform geometry reads (compatible with Python 3.6).

MeshFormat already points at the positional element. Do not guess its offset
from a Shader signature or reinterpret arbitrary vertex inputs as positions.
"""

import math
import struct


MAX_VERTEX_BYTES = 8 * 1024 * 1024


def bounded_integer(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("%s must be an integer in %d..%d" % (name, minimum, maximum))
    return value


def read_mesh(ctrl, mesh, max_vertices=4096, max_indices=12288):
    """Read an index prefix and its bounded vertex window; report every omission.

    Returned indices address positions, not the original vertex buffer. None
    marks a restart or an unread/invalid vertex and must break a strip. This is
    geometric evidence for one instance/view, not raster coverage evidence.
    """
    bounded_integer(max_vertices, "max_vertices", 1, 16384)
    bounded_integer(max_indices, "max_indices", 1, 49152)
    count = int(mesh.numIndices)
    stride = int(mesh.vertexByteStride)
    index_stride = int(mesh.indexByteStride)
    result = {
        "numIndices": count, "topology": str(mesh.topology).rsplit(".", 1)[-1],
        "indexStride": index_stride, "vertexStride": stride,
        "positions": [], "indices": [], "complete": False,
        "coordinateSpace": "clip" if mesh.unproject else "shader-output",
        "flipY": bool(mesh.flipY), "baseVertex": int(mesh.baseVertex),
        "allowRestart": bool(mesh.allowRestart), "restartIndex": int(mesh.restartIndex),
        "status": str(mesh.status), "limitations": [],
    }
    limits = result["limitations"]
    if count <= 0 or not int(mesh.vertexResourceId):
        limits.append("no-post-transform-position-buffer")
        return result
    fmt = mesh.format
    components, width = int(fmt.compCount), int(fmt.compByteWidth)
    result["positionFormat"] = {"components": components, "byteWidth": width,
                                "componentType": str(fmt.compType), "type": str(fmt.type)}
    if (str(fmt.type).rsplit(".", 1)[-1] != "Regular" or
            str(fmt.compType).rsplit(".", 1)[-1] != "Float" or
            width not in (2, 4, 8) or components not in (3, 4) or
            stride < width * components or stride > MAX_VERTEX_BYTES):
        limits.append("unsupported-position-format-or-stride")
        return result
    take = min(count, max_indices)
    if take < count:
        limits.append("index-prefix-only")
    raw_indices = []
    if index_stride:
        if index_stride not in (1, 2, 4) or not int(mesh.indexResourceId):
            limits.append("unavailable-index-buffer-or-format")
            return result
        offset = int(mesh.indexByteOffset)
        if offset < 0:
            limits.append("invalid-index-offset")
            return result
        data = bytes(ctrl.GetBufferData(mesh.indexResourceId, offset, take * index_stride))
        actual = min(take, len(data) // index_stride)
        if actual < take:
            limits.append("short-index-read")
        decoder = struct.Struct("<" + {1: "B", 2: "H", 4: "I"}[index_stride])
        for i in range(actual):
            value = decoder.unpack_from(data, i * index_stride)[0]
            raw_indices.append(None if mesh.allowRestart and value == int(mesh.restartIndex)
                               else value + int(mesh.baseVertex))
    else:
        raw_indices = list(range(take))
    valid = [index for index in raw_indices if index is not None and index >= 0]
    if not valid:
        limits.append("no-readable-indexed-vertices")
        return result
    first = min(valid)
    vertex_count = min(max(valid) - first + 1, max_vertices, MAX_VERTEX_BYTES // stride)
    if any(index < 0 or index >= first + vertex_count for index in raw_indices if index is not None):
        limits.append("vertex-window-only")
    offset = int(mesh.vertexByteOffset) + first * stride
    if offset < 0 or offset > (1 << 63) - MAX_VERTEX_BYTES:
        limits.append("invalid-vertex-offset")
        return result
    data = bytes(ctrl.GetBufferData(mesh.vertexResourceId, offset, vertex_count * stride))
    decoder = struct.Struct("<" + {2: "e", 4: "f", 8: "d"}[width] * components)
    # A native read can end immediately after the final positional element,
    # without the unused tail padding in its stride.
    actual = min(vertex_count, 0 if len(data) < decoder.size else
                 1 + (len(data) - decoder.size) // stride)
    if actual < vertex_count:
        limits.append("short-vertex-read")
    positions = []
    for i in range(actual):
        position = list(decoder.unpack_from(data, i * stride))
        if not all(math.isfinite(value) for value in position):
            positions.append(None)
            if "non-finite-position" not in limits:
                limits.append("non-finite-position")
        else:
            positions.append(position)
    result["positions"] = positions
    result["vertexStart"] = first
    result["indices"] = [index - first if index is not None and
                         0 <= index - first < len(positions) and
                         positions[index - first] is not None else None for index in raw_indices]
    result["readIndexCount"] = len(raw_indices)
    result["readVertexCount"] = len(positions)
    result["complete"] = not limits and not result["status"]
    return result
