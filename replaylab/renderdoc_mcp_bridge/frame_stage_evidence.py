"""Exact event texture bindings and navigation shared by frame-flow tools (Python 3.6)."""

from types import SimpleNamespace
import math


def texture_slice_view(texture, descriptor, kind, mip):
    """Resolve native view bounds without treating volume depth as array layers.

    Vulkan reports firstSlice=numSlices=0 for a true Texture3D view. D3D
    Texture3D SRVs likewise have no W-slice restriction, but retain Descriptor's
    default numSlices=1. RTV/UAV W ranges and 2D views of a volume are real
    restrictions and must still be honoured. All returned bounds refer to the
    selected mip, whose depth can differ from the resource's base depth.
    """
    depth = int(getattr(texture, "depth", 1))
    dimension = int(getattr(texture, "dimension", 3 if depth > 1 else 2))
    arrays = int(getattr(texture, "arraysize", 1))
    first = int(descriptor.firstSlice)
    count = int(descriptor.numSlices)
    if dimension not in (1, 2, 3) or depth <= 0 or arrays <= 0 or mip < 0:
        raise RuntimeError("the bound texture dimensions are unavailable")
    if first < 0 or count < 0:
        raise RuntimeError("the output binding slice range is invalid")
    volume = dimension == 3
    mip_depth = max(1, depth >> mip) if volume else 1
    total = mip_depth if volume else arrays
    raw_view_type = getattr(descriptor, "textureType", "")
    view_type = str(getattr(raw_view_type, "name", raw_view_type)).rsplit(".", 1)[-1].replace(" ", "")
    whole_volume = (volume and view_type == "Texture3D" and first == 0 and
                    (count == 0 or kind == "srv"))
    if whole_volume:
        count = mip_depth
    if count <= 0 or first >= total:
        raise RuntimeError("the output binding slice range is invalid")
    # Native all-remaining sentinels (e.g. D3D WSize) are clipped to the actual
    # mip depth, never allowed to manufacture an out-of-resource slice.
    count = min(count, total - first)
    return {
        "textureDimension": dimension, "resourceDepth": depth,
        "mipDepth": mip_depth, "sliceKind": "depth" if volume else "array",
        "viewSliceFirst": first, "viewSliceCount": count,
        "sliceRangeBasis": "whole-volume-view" if whole_volume else "native-view-range",
    }


def _basic_name(obj):
    return str(obj.type.basetype).rsplit(".", 1)[-1]


def _integer_child(obj, name):
    child = obj.FindChild(name) if obj is not None else None
    if child is None or _basic_name(child) not in ("UnsignedInteger", "SignedInteger", "Enum"):
        return None
    return int(child.AsInt())


def _named_child(obj, name):
    child = obj.FindChild(name)
    if child is not None:
        return child
    for index in range(int(obj.NumChildren())):
        candidate = obj.GetChild(index)
        if str(candidate.name).casefold() == name.casefold():
            return candidate
    return None


def _exact_chunks(action, structured):
    if action is None:
        return
    for event in getattr(action, "events", []) or []:
        index = int(event.chunkIndex)
        if int(event.eventId) == int(action.eventId) and 0 <= index < len(structured.chunks):
            yield index, structured.chunks[index]


def resolved_indirect_workload(action, structured):
    """Read native, event-exact decoded arguments; default action zeros are not proof.

    D3D12 FinaliseExecuteIndirectEvents creates ArgumentData from the captured
    parameter readback, alongside CommandIndex/ArgumentIndex/ArgumentSignature.
    No formatted event name, current buffer bytes, or nearby event is consulted.
    """
    if action is None or not int(action.flags) & 0x080000:
        return None
    for index, chunk in _exact_chunks(action, structured):
        data = chunk.FindChild("ArgumentData")
        command = _integer_child(chunk, "CommandIndex")
        argument = _integer_child(chunk, "ArgumentIndex")
        if data is None or command is None or argument is None or command < 0 or argument < 0:
            continue
        if chunk.FindChild("ArgumentSignature") is None:
            continue
        type_name = str(data.type.name)
        if type_name in ("D3D12_DRAW_ARGUMENTS", "D3D12_DRAW_INDEXED_ARGUMENTS"):
            keys = (("VertexCountPerInstance", "vertexCount") if type_name == "D3D12_DRAW_ARGUMENTS"
                    else ("IndexCountPerInstance", "indexCount")), ("InstanceCount", "instanceCount")
            if not int(action.flags) & 0x0002:
                continue
        elif type_name in ("D3D12_DISPATCH_ARGUMENTS", "D3D12_DISPATCH_MESH_ARGUMENTS"):
            keys = tuple(("ThreadGroupCount" + c, "groupCount" + c) for c in "XYZ")
            if not int(action.flags) & (0x0004 | 0x0008):
                continue
        elif type_name == "D3D12_DISPATCH_RAYS_DESC":
            keys = (("Width", "groupCountX"), ("Height", "groupCountY"), ("Depth", "groupCountZ"))
            if not int(action.flags) & 0x4000:
                continue
        else:
            continue
        workload = {target: _integer_child(data, source) for source, target in keys}
        if any(value is None or not 0 <= value <= 0xffffffff for value in workload.values()):
            continue
        return {"status": "resolved", "source": "native-structured-indirect-arguments",
                "eventId": int(action.eventId), "chunkIndex": index,
                "commandIndex": command, "argumentIndex": argument,
                "argumentType": type_name, "workload": workload,
                "nonZeroWorkload": all(value > 0 for value in workload.values())}
    return {"status": "unresolved", "eventId": int(action.eventId), "nonZeroWorkload": None,
            "source": "native-structured-indirect-arguments", "workload": {}}


def event_api_evidence(ctx, event_id, resource_id, max_depth=6, max_nodes=512):
    """Bounded read of native API parameters, without selecting or replaying EIDs."""
    if (isinstance(event_id, bool) or not isinstance(event_id, int) or event_id <= 0 or
            isinstance(max_depth, bool) or not isinstance(max_depth, int) or not 1 <= max_depth <= 12 or
            isinstance(max_nodes, bool) or not isinstance(max_nodes, int) or not 32 <= max_nodes <= 4096):
        raise ValueError("API evidence requires a positive EID, depth 1..12 and nodes 32..4096")
    action = ctx.GetAction(event_id)
    owner_action_id = None
    if action is None or int(action.eventId) != event_id:
        # Bind/set/copy API events may belong to the following action's event
        # list without themselves being an ActionDescription. Read that exact
        # APIEvent; never substitute the following Draw's parameters/workload.
        roots = getattr(ctx, "CurRootActions", None)
        pending = list(roots() or []) if callable(roots) else []
        action, visited = None, 0
        while pending and visited < 65536:
            owner = pending.pop()
            visited += 1
            exact = [event for event in (getattr(owner, "events", []) or [])
                     if int(event.eventId) == event_id]
            if exact:
                owner_action_id = int(owner.eventId)
                action = SimpleNamespace(eventId=event_id, events=exact, flags=0, parent=None, children=[])
                break
            pending.extend(getattr(owner, "children", []) or [])
        if action is None:
            raise ValueError("API evidence requires an exact captured action or API event EID; no adjacent event is substituted")
    structured = ctx.GetStructuredFile()
    remaining, truncated = [max_nodes], [False]

    def encode(obj, depth=0):
        if remaining[0] <= 0:
            truncated[0] = True
            return None
        remaining[0] -= 1
        basic = _basic_name(obj)
        result = {"name": str(obj.name)[:256], "type": str(obj.type.name)[:256], "kind": basic}
        if basic in ("UnsignedInteger", "SignedInteger", "Enum"):
            result["value"] = int(obj.AsInt())
        elif basic == "Resource":
            result["value"] = resource_id(obj.AsResourceId())
        elif basic == "Float":
            value = float(obj.AsFloat())
            result["value"] = value if math.isfinite(value) else str(value)
        elif basic == "Boolean":
            result["value"] = bool(obj.AsBool())
        elif basic in ("String", "Character"):
            result["value"] = str(obj.AsString())[:1024]
        elif basic == "Null":
            result["value"] = None
        count = int(obj.NumChildren())
        if count:
            result["childCount"] = count
            result["children"] = []
            if depth < max_depth:
                for i in range(min(count, remaining[0])):
                    item = encode(obj.GetChild(i), depth + 1)
                    if item is not None:
                        result["children"].append(item)
                    if remaining[0] <= 0:
                        break
            if len(result["children"]) < count:
                result["truncated"] = truncated[0] = True
        return result

    def chunks(owner):
        return [{"eventId": int(owner.eventId), "chunkIndex": i, "parameters": encode(c)}
                for i, c in _exact_chunks(owner, structured)]

    parent = getattr(action, "parent", None)
    result = {"schema": "bread-event-api-parameters/v1", "eventId": event_id,
              "isAction": owner_action_id is None, "ownerActionEventId": owner_action_id,
              "parentEventId": int(parent.eventId) if parent is not None else 0,
              "exactChunks": chunks(action), "parentChunks": [], "signatureChunks": [],
              "indirect": {"decoded": resolved_indirect_workload(action, structured)}}
    owner, call_chunk = action, None
    for unused in range(32):
        if owner is None:
            break
        for index, chunk in _exact_chunks(owner, structured):
            if ((chunk.FindChild("pCommandSignature") is not None and chunk.FindChild("pArgumentBuffer") is not None)
                    or (str(chunk.name) in ("vkCmdDrawIndexedIndirect", "vkCmdDrawIndirect", "vkCmdDispatchIndirect")
                        and _named_child(chunk, "buffer") is not None)):
                call_chunk = chunk
                result["indirect"]["parentEventId"] = int(owner.eventId)
                if owner is not action:
                    result["parentChunks"] = chunks(owner)
                break
        if call_chunk is not None:
            break
        owner = getattr(owner, "parent", None)
    if call_chunk is not None:
        call = {}
        if str(call_chunk.name).startswith("vkCmd"):
            buffer = _named_child(call_chunk, "buffer")
            offset_node = _named_child(call_chunk, "offset")
            count_node = _named_child(call_chunk, "drawCount")
            offset = int(offset_node.AsInt()) if offset_node is not None else None
            count = int(count_node.AsInt()) if count_node is not None else None
            if buffer is not None and offset is not None:
                call.update(argumentBufferId=resource_id(buffer.AsResourceId()), argumentOffset=offset,
                    drawCount=count if count is not None else 1, command=str(call_chunk.name))
                result["indirect"]["call"] = call
        for source, target in (("pCommandSignature", "commandSignatureId"), ("pArgumentBuffer", "argumentBufferId"),
                               ("pCountBuffer", "countBufferId")):
            child = call_chunk.FindChild(source)
            if child is not None and _basic_name(child) == "Resource":
                call[target] = resource_id(child.AsResourceId())
            elif child is not None and _basic_name(child) == "Null":
                call[target] = 0
        for source, target in (("MaxCommandCount", "maxCommandCount"), ("ArgumentBufferOffset", "argumentBufferOffset"),
                               ("CountBufferOffset", "countBufferOffset")):
            value = _integer_child(call_chunk, source)
            if value is not None:
                call[target] = value
        result["indirect"]["call"] = call
        signature = call_chunk.FindChild("pCommandSignature")
        if signature is not None and _basic_name(signature) == "Resource":
            description = ctx.GetResource(signature.AsResourceId())
            for index in list(getattr(description, "initialisationChunks", []) or [])[:32]:
                index = int(index)
                if 0 <= index < len(structured.chunks):
                    result["signatureChunks"].append({"chunkIndex": index, "parameters": encode(structured.chunks[index])})
        children = getattr(owner, "children", []) or []
        result["indirect"]["children"] = [value for value in
            (resolved_indirect_workload(child, structured) for child in children[:128]) if value is not None]
        result["indirect"]["childrenTruncated"] = len(children) > 128
    result["indirect"]["status"] = (result["indirect"]["decoded"] or {}).get("status", "container" if call_chunk is not None else "not-indirect")
    result["truncated"] = truncated[0]
    return result


def present_output_binding(action, ctrl, resource_id):
    """Resolve the presented backbuffer from the Present action, never its stale PS RTs.

    RenderDoc D3D11/D3D12/Vulkan/OpenGL readers record the actual presented image
    in ActionDescription.copyDestination. A missing target is not permission to
    substitute the last UI render target or the largest texture in the capture.
    """
    if action is None or not (int(getattr(action, "flags", 0)) & 0x0100):
        raise ValueError("Final output capture requires an exact Present event")
    target = getattr(action, "copyDestination", None)
    wanted = resource_id(target)
    if not wanted:
        raise ValueError("This Present event does not identify its presented texture")
    texture = next((item for item in ctrl.GetTextures() or []
                    if resource_id(item.resourceId) == wanted), None)
    if texture is None or int(getattr(texture, "dimension", 2)) != 2:
        raise ValueError("The presented resource is not an available two-dimensional texture")
    return SimpleNamespace(resource=texture.resourceId, firstMip=0, numMips=1,
                           firstSlice=0, numSlices=1)


def bindings(pipeline, kind, stage=None):
    if kind == "rtv":
        return list(pipeline.GetOutputTargets() or [])
    if kind == "dsv":
        return [pipeline.GetDepthTarget()]
    method = getattr(pipeline, "GetReadWriteResources" if kind == "uav" else "GetReadOnlyResources")
    try:
        return list(method(stage, True) or [])
    except TypeError:
        return list(method(stage) or [])


def validate_pipeline_state(details, action=None):
    """Report an entirely missing Shader-execution state, not missing textures.

    A buffer-only dispatch, depth-only draw, or Shader with no resource bindings
    is valid evidence here. Clear/copy/Present do not use a Shader pipeline at
    all. This narrow guard only prevents an empty replay-state read from being
    recorded as a successful Draw/Dispatch inspection; it does not certify the
    completeness or correctness of a nonempty pipeline.
    """
    shader_execution = details.get("actionKind") in ("graphics", "compute")
    if action is not None:
        raw_flags = action.get("flags") if isinstance(action, dict) else getattr(action, "flags", None)
        try:
            flags = int(raw_flags)
        except (TypeError, ValueError, OverflowError):
            flags = None
        if flags is not None:
            # Native ActionFlags: Drawcall, Dispatch, MeshDispatch, DispatchRay.
            shader_execution = bool(flags & (0x0002 | 0x0004 | 0x0008 | 0x4000))
            # Clear, Present, Copy, Resolve, GenMips and BuildAccStruct must not
            # inherit a preceding draw's classification.
            if flags & (0x0001 | 0x0100 | 0x0400 | 0x0800 | 0x1000 | 0x8000):
                shader_execution = False
    if not shader_execution or details.get("shaders"):
        return None
    if any(details.get(key) for key in (
            "inputs", "outputs", "possibleOutputs", "renderTargets", "depthTarget",
            "expectedOutputs", "textureInputs")):
        return None
    event_id = details.get("eventId")
    if event_id is None and action is not None:
        event_id = action.get("eventId") if isinstance(action, dict) else getattr(action, "eventId", None)
    return {
        "errorId": "empty-pipeline-state", "eventId": event_id,
        "error": "The Draw/Dispatch inspection returned no Shader or input/output bindings; "
                 "the replay pipeline state must be checked again before using this evidence",
    }


def inventory(ctrl, pipeline, details, stages, resource_id, format_name, action=None):
    textures = {resource_id(t.resourceId): t for t in ctrl.GetTextures() or []}
    outputs, inputs = [], []
    errors = []

    def result():
        checked = dict(details, expectedOutputs=outputs, textureInputs=inputs)
        failure = validate_pipeline_state(checked, action)
        if failure is not None:
            errors.append(dict(failure, kind="pipeline-state", stage=""))
        return {"inventoryVersion": 3, "expectedOutputs": outputs, "textureInputs": inputs,
                "bindingErrors": errors, "bindingsComplete": not errors}

    def add(kind, stage_name, stage, destination):
        try:
            values = bindings(pipeline, kind, stage)
        except Exception as error:
            errors.append({"kind": kind, "stage": stage_name, "error": str(error)[:240]})
            return
        for index, value in enumerate(values):
            descriptor = getattr(value, "descriptor", value)
            rid = resource_id(getattr(descriptor, "resource", None))
            if not rid:
                continue
            texture = textures.get(rid)
            row = {
                "kind": kind, "index": index, "stage": stage_name, "resourceId": rid,
                "resourceType": "texture" if texture is not None else "buffer",
                "mip": max(0, int(getattr(descriptor, "firstMip", 0))),
                "slice": max(0, int(getattr(descriptor, "firstSlice", 0))), "sample": 0,
                "numMips": int(descriptor.numMips) if hasattr(descriptor, "numMips") else None,
                "numSlices": int(descriptor.numSlices) if hasattr(descriptor, "numSlices") else None,
                "format": format_name(texture.format) if texture is not None else "",
                "width": int(texture.width) if texture is not None else 0,
                "height": int(texture.height) if texture is not None else 0,
                "componentCount": int(getattr(texture.format, "compCount", 0)) if texture is not None else 0,
                "captureSupported": texture is not None,
            }
            if texture is not None:
                row["textureDimension"] = int(getattr(texture, "dimension", 0))
                row["resourceDepth"] = int(getattr(texture, "depth", 1))
                if row["textureDimension"] == 3:
                    row["nativeDescriptor"] = {
                        "firstMip": row["mip"], "numMips": row["numMips"],
                        "firstSlice": row["slice"], "numSlices": row["numSlices"],
                    }
                    try:
                        view = texture_slice_view(texture, descriptor, kind, row["mip"])
                        row.update(view)
                        row["numSlices"] = view["viewSliceCount"]
                        row["depthSlice"] = row["slice"]
                    except (AttributeError, TypeError, ValueError, RuntimeError) as error:
                        row["captureSupported"] = False
                        errors.append({"kind": kind, "stage": stage_name, "resourceId": rid,
                                       "error": str(error)[:240]})
            destination.append(row)

    if details.get("actionKind") == "utility" and action is not None:
        # Query resolves and buffer copies write resources without Shader UAV
        # bindings. Use the exact action operands and the native buffer catalog;
        # an absent texture description alone is not proof that a resource is a
        # buffer. Inherited draw/dispatch bindings do not describe this action.
        operands = [("copy-source", getattr(action, "copySource", None), inputs),
                    ("copy-destination", getattr(action, "copyDestination", None), outputs)]
        candidates = [(kind, resource_id(rid), destination)
                      for kind, rid, destination in operands if resource_id(rid)]
        if candidates:
            try:
                buffers = {resource_id(b.resourceId) for b in ctrl.GetBuffers() or []}
                for kind, rid, destination in candidates:
                    if rid in textures:
                        # Utility texture previews use the existing action-output
                        # capture path, not an inherited Shader attachment.
                        continue
                    if rid not in buffers:
                        errors.append({"kind": kind, "stage": "", "resourceId": rid,
                                       "error": "Action resource is absent from the native resource catalogs"})
                        continue
                    destination.append({
                        "kind": kind, "index": 0, "stage": "", "resourceId": rid,
                        "resourceType": "buffer", "mip": 0, "slice": 0, "sample": 0,
                        "format": "", "width": 0, "height": 0, "componentCount": 0,
                        "captureSupported": False,
                    })
            except Exception as error:
                errors.append({"kind": "action-resources", "stage": "", "error": str(error)[:240]})
        return result()

    if details.get("actionKind") == "graphics":
        add("rtv", "fragment", None, outputs)
        add("dsv", "fragment", None, outputs)
    for name, stage in stages:
        if name in details.get("shaders", {}):
            add("uav", name, stage, outputs)
            add("srv", name, stage, inputs)
    return result()


def followed_binding(ctx, rd, qrd, value, resource_id):
    """Resolve a saved descriptor to the viewer's native following coordinates.

    Evidence indexes are list positions; SRV/UAV following uses access.index
    and arrayElement instead. Never substitute a locked resource tab.
    """
    wanted = int(value.get("resourceId") or 0)
    mapping = value.get("displayMapping") or {}
    kind = value.get("attachmentKind") or mapping.get("attachmentKind")
    index = value.get("attachmentIndex", mapping.get("attachmentIndex"))
    stage_name = value.get("attachmentStage") or mapping.get("attachmentStage")
    pixel = rd.ShaderStage.Pixel
    action = ctx.GetAction(int(value["eventId"]))
    flags = int(getattr(action, "flags", 0))
    # Copy, resolve, clear and Present expose their destination in the current
    # output thumbnail, regardless of the graphics pipeline left bound.
    if resource_id(getattr(action, "copyDestination", None)) == wanted:
        return qrd.FollowType.OutputColor, pixel, 0, 0
    pipeline = ctx.CurPipelineState()
    stages = [(name, getattr(rd.ShaderStage, enum)) for name, enum in (
        ("fragment", "Pixel"), ("compute", "Compute"), ("vertex", "Vertex"),
        ("hull", "Hull"), ("domain", "Domain"), ("geometry", "Geometry"),
        ("task", "Task"), ("mesh", "Mesh")) if hasattr(rd.ShaderStage, enum)]
    candidates = []
    if not flags & 0x0004:  # ActionFlags.Dispatch: no inherited graphics outputs.
        for i, descriptor in enumerate(bindings(pipeline, "rtv") if kind in (None, "", "rtv") else []):
            candidates.append(("rtv", "fragment", i, descriptor,
                               (qrd.FollowType.OutputColor, pixel, i, 0)))
        for descriptor in bindings(pipeline, "dsv") if kind in (None, "", "dsv") else []:
            candidates.append(("dsv", "fragment", 0, descriptor,
                               (qrd.FollowType.OutputDepth, pixel, 0, 0)))
    for binding_kind, follow in (("uav", qrd.FollowType.ReadWrite), ("srv", qrd.FollowType.ReadOnly)):
        if kind and kind != binding_kind:
            continue
        for name, stage in stages:
            if stage_name and stage_name != name:
                continue
            if flags & 0x0004 and name != "compute":
                continue
            for i, used in enumerate(bindings(pipeline, binding_kind, stage)):
                access = getattr(used, "access", None)
                descriptor = getattr(used, "descriptor", used)
                candidates.append((binding_kind, name, i, descriptor,
                    (follow, getattr(access, "stage", stage),
                     int(getattr(access, "index", i)), int(getattr(access, "arrayElement", 0)))))
    for binding_kind, name, i, descriptor, follow in candidates:
        if (resource_id(getattr(descriptor, "resource", None)) == wanted and
                (not kind or binding_kind == kind) and (index is None or i == int(index))):
            return follow
    raise ValueError("The frame diagram texture is not bound at this event")


def navigate(ctx, rd, value, resource_id, qrd=None):
    """UI-thread operation: navigate once, then select the captured texture view."""
    event = int(value["eventId"])
    action = ctx.GetAction(event)
    if action is None or int(action.eventId) != event:
        raise ValueError("The frame diagram event is no longer available")
    texture = None
    wanted = int(value.get("resourceId") or 0)
    if wanted:
        texture = next((t for t in ctx.GetTextures() if resource_id(t.resourceId) == wanted), None)
        if texture is None:
            raise ValueError("The frame diagram texture is no longer available")
    ctx.SetEventID([], event, event)
    if texture is not None:
        ctx.ShowTextureViewer()
        viewer = ctx.GetTextureViewer()
        if qrd is None:
            import qrenderdoc as qrd
        viewer.ViewFollowedResource(*followed_binding(ctx, rd, qrd, value, resource_id))
        if resource_id(viewer.GetCurrentResource()) != wanted:
            raise ValueError("The current output could not select the frame diagram texture")
        viewer.SetSelectedSubresource(rd.Subresource(int(value.get("mip") or 0),
                                                    int(value.get("slice") or 0),
                                                    int(value.get("sample") or 0)))
        channel = value.get("channel", "rgba")
        viewer.SetChannelVisibility(channel in ("rgba", "rgb", "r", "depth"),
                                    channel in ("rgba", "rgb", "g"),
                                    channel in ("rgba", "rgb", "b"), channel == "a")
        mapping = value.get("displayMapping") or {}
        if hasattr(viewer, "SetFlipY"):
            viewer.SetFlipY(mapping.get("flipY") is True)
        low, high = mapping.get("blackPoint"), mapping.get("whitePoint")
        if isinstance(low, (int, float)) and isinstance(high, (int, float)) and low < high:
            viewer.SetHistogramRange(low, high)
    return True
