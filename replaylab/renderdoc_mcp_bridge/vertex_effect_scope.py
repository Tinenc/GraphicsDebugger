"""Identify draw dependencies from actual geometry streams, independent of ResourceId."""

import hashlib
import re
import struct


MAX_CANDIDATES = 256
MAX_INDICES = 600_000
MAX_VERTEX_WINDOW = 32 * 1024 * 1024


def _draws(actions, draw_flag, last_event):
    for action in actions:
        if action.eventId <= last_event and action.flags & draw_flag:
            yield action
        yield from _draws(action.children, draw_flag, last_event)


def _geometry(bridge, ctrl, action, pipe):
    count = int(action.numIndices)
    if not 0 < count <= MAX_INDICES:
        return None
    inputs = [a for a in pipe.GetVertexInputs() if a.used and not a.perInstance and
              not a.genericEnabled and a.format.compCount >= 3]
    named = [a for a in inputs if re.search(r'(^|[_.])(?:position|pos)(?:0|$)', str(a.name), re.I)]
    # Stripped layouts commonly put the position first. Exact stream bytes and
    # index connectivity are still required before a second resource is edited.
    attribute = named[0] if len(named) == 1 else inputs[0] if inputs else None
    buffers = pipe.GetVBuffers()
    if attribute is None or not 0 <= attribute.vertexBuffer < len(buffers):
        return None
    vertex = buffers[attribute.vertexBuffer]
    vertex_id = bridge._resource_id_int(vertex.resourceId)
    if not vertex_id or not vertex.byteStride:
        return None
    indexed = bool(action.flags & bridge.rd.ActionFlags.Indexed)
    ib = pipe.GetIBuffer()
    index_size = int(ib.byteStride) if indexed else 0
    index_offset = int(ib.byteOffset) + int(action.indexOffset) * index_size if indexed else 0
    index_id = bridge._resource_id_int(ib.resourceId) if indexed else 0
    restart = None
    if indexed:
        if index_size not in (1, 2, 4) or not index_id:
            return None
        raw = bytes(ctrl.GetBufferData(ib.resourceId, index_offset, count * index_size))
        if len(raw) != count * index_size:
            return None
        indices = [x[0] for x in struct.iter_unpack({1: '<B', 2: '<H', 4: '<I'}[index_size], raw)]
        base = int(action.baseVertex)
        if pipe.IsRestartEnabled():
            restart = int(pipe.GetRestartIndex()) & ((1 << (index_size * 8)) - 1)
    else:
        indices = range(count)
        base = int(action.vertexOffset)
    used = [index + base for index in indices if index != restart]
    if not used or min(used) < 0:
        return None
    first, last = min(used), max(used)
    stride = int(vertex.byteStride)
    width = int(attribute.format.ElementSize())
    start = int(vertex.byteOffset) + int(attribute.byteOffset) + first * stride
    length = (last - first) * stride + width
    if not width or length > MAX_VERTEX_WINDOW:
        return None
    raw = bytes(ctrl.GetBufferData(vertex.resourceId, start, length))
    if len(raw) != length:
        return None
    positions = hashlib.sha256()
    connectivity = hashlib.sha256()
    remap = {}
    for index in indices:
        if index == restart:
            connectivity.update(b'\xff\xff\xff\xff')
            continue
        relative = index + base - first
        if relative not in remap:
            remap[relative] = len(remap)
            positions.update(raw[relative * stride:relative * stride + width])
        connectivity.update(struct.pack('<I', remap[relative]))
    return dict(vertex=vertex.resourceId, index=ib.resourceId if indexed else None,
                vertexId=vertex_id, indexId=index_id, indexOffset=index_offset,
                indexSize=count * index_size, vertexOffset=start, vertexSize=length,
                positionFormat=bridge._fmt_str(attribute.format), positionSha256=positions.hexdigest(),
                connectivitySha256=connectivity.hexdigest(), topology=str(pipe.GetPrimitiveTopology()),
                vertexCount=len(remap), namedPosition=bool(named))


def _overlap(a, a_size, b, b_size):
    return max(a, b) < min(a + a_size, b + b_size)


def _relationship(anchor, candidate):
    if (anchor['topology'], anchor['positionFormat'], anchor['positionSha256'], anchor['connectivitySha256']) == (
            candidate['topology'], candidate['positionFormat'], candidate['positionSha256'], candidate['connectivitySha256']):
        return ('identical-geometry' if anchor['vertexId'] == candidate['vertexId'] and
                anchor['indexId'] == candidate['indexId'] else 'duplicated-geometry')
    if (anchor['vertexId'] == candidate['vertexId'] and anchor['indexId'] and
            anchor['indexId'] == candidate['indexId'] and
            _overlap(anchor['indexOffset'], anchor['indexSize'], candidate['indexOffset'], candidate['indexSize']) and
            _overlap(anchor['vertexOffset'], anchor['vertexSize'], candidate['vertexOffset'], candidate['vertexSize'])):
        return 'overlapping-geometry'
    return None


def inspect(bridge, ctrl, event_id, observation_event, stage):
    """Check shared buffer uses and equal-count draws before replacing a VS.

    Exact byte/connectivity matches can span separately allocated assets. A bounded
    search cannot establish identity across remeshing or a different position encoding.
    """
    actions = {a.eventId: a for a in _draws(ctrl.GetRootActions(), bridge.rd.ActionFlags.Drawcall, observation_event)}
    action = actions.get(event_id)
    if action is None:
        return dict(complete=False, reason='source-draw-unavailable', matches=[])
    bridge._select_event_for_read(ctrl, event_id)
    pipe = ctrl.GetPipelineState()
    anchor = _geometry(bridge, ctrl, action, pipe)
    if anchor is None:
        return dict(complete=False, reason='source-geometry-unavailable', matches=[])
    related_resources = [anchor['vertex']] + ([anchor['index']] if anchor['indexId'] else [])
    shared = {int(use.eventId) for resource in related_resources for use in ctrl.GetUsage(resource)}
    candidates = [a for a in actions.values() if a.eventId != event_id and
                  (a.eventId in shared or a.numIndices == action.numIndices)]
    candidates.sort(key=lambda a: (a.eventId not in shared, abs(a.eventId - event_id)))
    matches, unresolved = [], []
    for candidate in candidates[:MAX_CANDIDATES]:
        bridge._select_event_for_read(ctrl, candidate.eventId)
        pipe = ctrl.GetPipelineState()
        geometry = _geometry(bridge, ctrl, candidate, pipe)
        if geometry is None:
            unresolved.append(int(candidate.eventId))
            continue
        relation = _relationship(anchor, geometry)
        if not relation:
            continue
        shader = pipe.GetShaderReflection(stage)
        shader_id = bridge._resource_id_int(pipe.GetShader(stage))
        raw = bridge._shader_raw_bytes(shader)
        depth = pipe.GetDepthTarget()
        outputs = [bridge._resource_id_int(v.resource) for v in pipe.GetOutputTargets()]
        matches.append(dict(eventId=int(candidate.eventId), relation=relation, shaderResourceId=shader_id,
                            shaderRawSha256=hashlib.sha256(raw).hexdigest() if raw else '',
                            entryPoint=str(pipe.GetShaderEntryPoint(stage)),
                            depthTarget=bridge._resource_id_int(depth.resource),
                            colorTargets=[rid for rid in outputs if rid],
                            instanceCount=int(candidate.numInstances), vertexCount=geometry['vertexCount']))
    return dict(complete=len(candidates) <= MAX_CANDIDATES and not unresolved,
                candidateCount=len(candidates), inspected=min(len(candidates), MAX_CANDIDATES),
                unresolvedEventIds=unresolved, matches=matches,
                coverage='shared-buffer-uses-and-equal-index-count-through-observation')
