"""Read actual post-VS channels through RenderDoc's cross-API mesh interface.

Packing follows BufferViewer::ConfigureColumnsForShader: position first,
minimum 32-bit components and API-specific vector alignment. Padding and
synthetic replay ResourceIds are not observable Shader values.
"""
import struct


def _name(value):
    return str(getattr(value, "name", value)).rsplit(".", 1)[-1].lower()


def outputs(bridge, ctrl, event_id, output_limit, total_limit):
    rd = bridge.rd
    pipeline = ctrl.GetPipelineState()
    reflection = pipeline.GetShaderReflection(rd.ShaderStage.Vertex)
    if reflection is None:
        raise ValueError("vertex output reflection is unavailable")
    pending, action = list(ctrl.GetRootActions()), None
    while pending:
        item = pending.pop()
        if int(item.eventId) == event_id:
            action = item
            break
        pending.extend(item.children)
    if action is None:
        raise ValueError("vertex output action is unavailable")
    # Ordinary D3D11 draws leave numInstances at zero. Real instanced draws
    # (including indirect ones) must keep a zero count instead of inventing
    # an instance for the numerical comparison.
    flags = int(action.flags)
    instances = int(action.numInstances) if flags & (0x020000 | 0x080000) else 1
    if instances <= 0:
        raise ValueError("vertex output action has zero or unresolved instance count")
    views = max(1, int(pipeline.MultiviewBroadcastCount()))
    if instances * views > 1024:
        raise ValueError("vertex instance/view range exceeds observation budget")
    signature = [sig for sig in reflection.outputSignature
                 if int(sig.stream) == 0 and _name(sig.systemValue) != "outputindices"]
    signature.sort(key=lambda sig: _name(sig.systemValue) != "position")
    aligned = pipeline.HasAlignedPostVSData(rd.MeshDataStage.VSOut)
    fields, offset = [], 0
    for index, sig in enumerate(signature):
        if getattr(sig, "perPrimitiveRate", False):
            raise ValueError("vertex output has an unexpected per-primitive channel")
        token, count = _name(sig.varType), int(sig.compCount)
        width = 8 if token in ("double", "ulong", "slong") else 4
        if token in ("float", "half", "double"):
            kind = "Float"
        elif token in ("uint", "ushort", "ubyte", "ulong"):
            kind = "UInt"
        elif token in ("sint", "sshort", "sbyte", "slong"):
            kind = "SInt"
        else:
            raise ValueError("vertex output scalar type is unavailable: " + token)
        if count < 1 or count > 4:
            raise ValueError("vertex output component count is invalid")
        alignment = width * (4 if count > 2 else count) if aligned else 1
        offset = (offset + alignment - 1) // alignment * alignment
        fields.append((index, offset, width, count, kind))
        offset += width * count
    if not fields:
        raise ValueError("vertex output signature is empty")
    result, total = [], 0
    for instance in range(instances):
        for view in range(views):
            mesh = ctrl.GetPostVSData(instance, view, rd.MeshDataStage.VSOut)
            if not bridge._resource_id_int(mesh.vertexResourceId):
                raise ValueError("post-VS readback is unavailable: " + str(mesh.status))
            count, stride = int(mesh.numIndices), int(mesh.vertexByteStride)
            if count <= 0 or stride < offset or count * stride > output_limit:
                raise ValueError("post-VS range is empty, invalid or exceeds observation budget")
            indices = list(range(count))
            if bridge._resource_id_int(mesh.indexResourceId):
                index_width = int(mesh.indexByteStride)
                if index_width not in (1, 2, 4):
                    raise ValueError("post-VS index width is invalid")
                index_raw = bytes(ctrl.GetBufferData(mesh.indexResourceId,
                    int(mesh.indexByteOffset), count * index_width))
                if len(index_raw) != count * index_width:
                    raise ValueError("post-VS index readback is incomplete")
                indices = list(struct.unpack("<%d%s" % (count, {1: "B", 2: "H", 4: "I"}[index_width]), index_raw))
                if pipeline.IsRestartEnabled():
                    restart = int(pipeline.GetRestartIndex()) & ((1 << (8 * index_width)) - 1)
                    indices = [i for i in indices if i != restart]
                indices = [i + int(mesh.baseVertex) for i in indices]
            if not indices or min(indices) < 0:
                raise ValueError("post-VS indices have no valid vertices")
            size = (max(indices) + 1) * stride
            total += max(size, count * stride)
            if size > output_limit or total > total_limit:
                raise ValueError("post-VS readback exceeds observation budget")
            raw = bytes(ctrl.GetBufferData(mesh.vertexResourceId, int(mesh.vertexByteOffset), size))
            if len(raw) != size:
                raise ValueError("post-VS vertex readback is incomplete")
            for index, field_offset, width, components, kind in fields:
                length = width * components
                data = b"".join(raw[i * stride + field_offset:i * stride + field_offset + length] for i in indices)
                result.append(({"kind": "vertex-channel", "index": index, "instance": instance,
                    "view": view, "width": len(indices), "height": 1, "formatType": "Regular",
                    "componentType": kind, "componentByteWidth": width, "componentCount": components,
                    "topology": str(mesh.topology)}, data))
    return result
