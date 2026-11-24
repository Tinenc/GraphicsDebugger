from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest


SOURCE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge/frame_stage_evidence.py"
spec = spec_from_file_location("frame_stage_evidence", SOURCE)
evidence = module_from_spec(spec)
spec.loader.exec_module(evidence)


def resource_id(value):
    return int(value or 0)


class Structured:
    def __init__(self, name, kind="Struct", value=None, children=(), type_name=""):
        self.name, self.value, self.children = name, value, list(children)
        self.type = SimpleNamespace(basetype="SDBasic." + kind, name=type_name or kind)

    def FindChild(self, name):
        return next((c for c in self.children if c.name == name), None)

    def NumChildren(self):
        return len(self.children)

    def GetChild(self, index):
        return self.children[index]

    def AsInt(self):
        return self.value

    AsResourceId = AsInt
    AsFloat = AsInt
    AsBool = AsInt
    AsString = AsInt


def indirect_chunk(eid=62778, count=3, kind="draw"):
    names = ("VertexCountPerInstance", "InstanceCount") if kind == "draw" else (
        "ThreadGroupCountX", "ThreadGroupCountY", "ThreadGroupCountZ")
    data = Structured("ArgumentData", children=[Structured(name, "UnsignedInteger", count if i == 0 else 1)
                      for i, name in enumerate(names)],
                      type_name="D3D12_DRAW_ARGUMENTS" if kind == "draw" else "D3D12_DISPATCH_ARGUMENTS")
    chunk = Structured("native decoded", "Chunk", children=[
        Structured("CommandIndex", "UnsignedInteger", 0), Structured("ArgumentIndex", "UnsignedInteger", 0),
        Structured("ArgumentSignature", children=[Structured("Type", "Enum", 0)]), data])
    action = SimpleNamespace(eventId=eid, flags=0x080000 | (2 if kind == "draw" else 4), parent=None,
                             events=[SimpleNamespace(eventId=eid, chunkIndex=0)], children=[],
                             numIndices=999, numInstances=999)
    return action, SimpleNamespace(chunks=[chunk])


@pytest.mark.parametrize("kind", ["draw", "dispatch"])
@pytest.mark.parametrize("count", [0, 3])
def test_indirect_workload_uses_exact_native_argument_data_not_name_or_action_defaults(kind, count):
    action, structured = indirect_chunk(count=count, kind=kind)
    action.customName = "IndirectDraw(999, 999)"
    result = evidence.resolved_indirect_workload(action, structured)
    assert result["status"] == "resolved"
    assert result["nonZeroWorkload"] is (count > 0)
    assert result["eventId"] == 62778
    assert result["chunkIndex"] == 0
    assert result["source"] == "native-structured-indirect-arguments"
    action.events[0].eventId -= 1
    assert evidence.resolved_indirect_workload(action, structured)["status"] == "unresolved"


@pytest.mark.parametrize("missing", ["ArgumentData", "CommandIndex", "ArgumentIndex", "ArgumentSignature"])
def test_default_indirect_zero_cannot_be_promoted_without_complete_native_provenance(missing):
    action, structured = indirect_chunk(count=0)
    structured.chunks[0].children = [x for x in structured.chunks[0].children if x.name != missing]
    assert evidence.resolved_indirect_workload(action, structured)["nonZeroWorkload"] is None


def test_event_api_evidence_reads_parent_buffer_offsets_signature_and_decoded_child_without_replay():
    action, structured = indirect_chunk()
    parent = SimpleNamespace(eventId=62777, parent=None, flags=0x20000,
        events=[SimpleNamespace(eventId=62777, chunkIndex=1)], children=[action])
    action.parent = parent
    call = Structured("ExecuteIndirect", "Chunk", children=[
        Structured("pCommandSignature", "Resource", 9), Structured("pArgumentBuffer", "Resource", 2**54),
        Structured("ArgumentBufferOffset", "UnsignedInteger", 4096), Structured("MaxCommandCount", "UnsignedInteger", 7),
        Structured("pCountBuffer", "Resource", 44), Structured("CountBufferOffset", "UnsignedInteger", 12)])
    signature = Structured("CreateCommandSignature", "Chunk", children=[
        Structured("pDesc", children=[Structured("ByteStride", "UnsignedInteger", 16)])])
    structured.chunks += [call, signature]
    ctx = SimpleNamespace(GetAction=lambda eid: {62777: parent, 62778: action}.get(eid),
                          GetStructuredFile=lambda: structured,
                          GetResource=lambda rid: SimpleNamespace(initialisationChunks=[2]))
    result = evidence.event_api_evidence(ctx, 62778, resource_id)
    assert result["parentEventId"] == 62777
    assert result["indirect"]["call"] == dict(commandSignatureId=9, argumentBufferId=2**54,
        argumentBufferOffset=4096, maxCommandCount=7, countBufferId=44, countBufferOffset=12)
    assert result["indirect"]["decoded"]["nonZeroWorkload"] is True
    assert result["parentChunks"][0]["eventId"] == 62777
    assert result["signatureChunks"][0]["parameters"]["children"][0]["children"][0]["value"] == 16
    assert result["truncated"] is False
    parent_result = evidence.event_api_evidence(ctx, 62777, resource_id)
    assert parent_result["indirect"]["status"] == "container"
    assert parent_result["indirect"]["children"][0]["eventId"] == 62778
    call.children.append(Structured("LargeArray", "Array", children=[Structured("item", "UnsignedInteger", 0)] * 5000))
    result = evidence.event_api_evidence(ctx, 62777, resource_id, max_nodes=32)
    assert result["truncated"] is True
    assert result["indirect"]["call"]["argumentBufferId"] == 2**54
    with pytest.raises(ValueError, match="exact captured"):
        evidence.event_api_evidence(ctx, 62779, resource_id)


def test_non_action_api_event_reads_its_own_chunk_without_using_next_draw():
    chunk = Structured("SetConstantBuffers", "Chunk", children=[Structured("StartSlot", "UnsignedInteger", 4)])
    next_draw = SimpleNamespace(eventId=20, flags=2, parent=None, children=[],
        events=[SimpleNamespace(eventId=19, chunkIndex=0), SimpleNamespace(eventId=20, chunkIndex=1)])
    ctx = SimpleNamespace(GetAction=lambda eid: None, CurRootActions=lambda: [next_draw],
                          GetStructuredFile=lambda: SimpleNamespace(chunks=[chunk, Structured("Draw", "Chunk")]))
    result = evidence.event_api_evidence(ctx, 19, resource_id)
    assert result["eventId"] == 19 and result["isAction"] is False
    assert result["ownerActionEventId"] == 20
    assert result["exactChunks"][0]["parameters"]["name"] == "SetConstantBuffers"
    assert result["indirect"]["decoded"] is None
    with pytest.raises(ValueError, match="exact captured"):
        evidence.event_api_evidence(ctx, 18, resource_id)


def test_bridge_api_parameter_rpc_is_capture_bound_and_never_replays_or_navigates(monkeypatch):
    import sys
    from test_bridge_composite_readonly import load_bridge
    bridge = load_bridge()
    monkeypatch.setitem(sys.modules, bridge.__name__, bridge)
    monkeypatch.setitem(sys.modules, bridge.__name__ + ".frame_stage_evidence", evidence)
    action, structured = indirect_chunk(count=0)
    bridge._ctx = SimpleNamespace(GetAction=lambda eid: action, GetStructuredFile=lambda: structured)
    bridge._capture_session_id = "a" * 32
    bridge._request_capture_loaded = lambda: True
    bridge._invoke_on_ui_sync = lambda callback: callback()
    bridge._replay = lambda callback: pytest.fail("Read-only API parameters must not replay the frame")
    result = bridge._inspect_event_api_parameters({"event_id": 62778, "expected_capture_session_id": "a" * 32})
    assert result["captureSessionId"] == "a" * 32
    assert result["indirect"]["decoded"]["nonZeroWorkload"] is False
    with pytest.raises(RuntimeError, match="different capture"):
        bridge._inspect_event_api_parameters({"event_id": 62778, "expected_capture_session_id": "b" * 32})


def test_refreshed_stage_does_not_claim_output_candidate_for_native_zero_instances(monkeypatch):
    import sys
    from test_bridge_composite_readonly import load_bridge
    bridge = load_bridge()
    monkeypatch.setitem(sys.modules, bridge.__name__, bridge)
    monkeypatch.setitem(sys.modules, bridge.__name__ + ".frame_stage_evidence", evidence)
    action, structured = indirect_chunk(count=0)
    bridge._ctx = SimpleNamespace(GetStructuredFile=lambda: structured)
    bridge._capture_session_id = "a" * 32
    bridge._request_visible_event_id = lambda fallback: 65445
    bridge._available_shader_stages = lambda names: []
    bridge._draw_call_details_on_replay = lambda ctrl, eid: {"workload": {}, "shaders": {},
        "participation": {"outputAffectingCandidate": True}, "actualWorkState": "output-affecting-candidate"}
    events = []
    bridge._set_frame_event = lambda ctrl, eid, force: events.append(eid)
    bridge._replay = lambda callback: callback(SimpleNamespace(GetRootActions=lambda: [action],
        GetPipelineState=lambda: SimpleNamespace(), GetTextures=lambda: []))
    result = bridge._inspect_frame_render_stage({"event_id": 62778, "expected_capture_session_id": "a" * 32})
    assert result["details"]["participation"]["nonZeroWorkload"] is False
    assert result["details"]["participation"]["outputAffectingCandidate"] is False
    assert result["details"]["workload"]["indirectArgumentsResolved"] is True
    assert events == [62778, 65445]


def texture(rid, width=1024, height=512, components=4):
    return SimpleNamespace(resourceId=rid, width=width, height=height,
                           format=SimpleNamespace(name="RGBA16_FLOAT", compCount=components))


def binding(rid, mip=0, layer=0, wrapped=True, num_mips=1, num_slices=1):
    descriptor = SimpleNamespace(resource=rid, firstMip=mip, firstSlice=layer,
                                 numMips=num_mips, numSlices=num_slices)
    return SimpleNamespace(descriptor=descriptor) if wrapped else descriptor


class Pipeline:
    def __init__(self, outputs=(), depth=None, uavs=None, srvs=None):
        self.outputs, self.depth = outputs, depth
        self.uavs, self.srvs = uavs or {}, srvs or {}
        self.calls = []

    def GetOutputTargets(self):
        self.calls.append(("rtv",))
        return self.outputs

    def GetDepthTarget(self):
        self.calls.append(("dsv",))
        return self.depth

    def GetReadWriteResources(self, stage, only_used=True):
        self.calls.append(("uav", stage, only_used))
        return self.uavs.get(stage, ())

    def GetReadOnlyResources(self, stage, only_used=True):
        self.calls.append(("srv", stage, only_used))
        return self.srvs.get(stage, ())


def inventory(pipeline, textures, kind, shaders):
    controller = SimpleNamespace(GetTextures=lambda: textures)
    return evidence.inventory(controller, pipeline, {"actionKind": kind, "shaders": shaders},
                              [("vertex", "VS"), ("fragment", "PS"), ("compute", "CS")],
                              resource_id, lambda fmt: fmt.name)


@pytest.mark.parametrize("kind", ["graphics", "compute"])
def test_all_empty_shader_execution_inventory_is_not_complete(kind):
    result = inventory(Pipeline(), [], kind, {})
    assert result["expectedOutputs"] == [] and result["textureInputs"] == []
    assert result["bindingsComplete"] is False
    assert result["bindingErrors"][0]["errorId"] == "empty-pipeline-state"
    assert result["bindingErrors"][0]["kind"] == "pipeline-state"


@pytest.mark.parametrize("flags", [0x0002, 0x0004, 0x0008, 0x4000, 0x080002])
def test_native_draw_and_dispatch_flags_detect_empty_state_even_if_kind_is_missing(flags):
    action = SimpleNamespace(flags=flags, eventId=42)
    result = evidence.validate_pipeline_state({"shaders": {}, "inputs": [], "outputs": []}, action)
    assert result["errorId"] == "empty-pipeline-state" and result["eventId"] == 42
    controller = SimpleNamespace(GetTextures=lambda: [])
    result = evidence.inventory(controller, Pipeline(), {}, [], resource_id, str, action)
    assert result["bindingsComplete"] is False
    result = evidence.inventory(controller, Pipeline(), {"actionKind": "utility"}, [], resource_id, str, action)
    assert result["bindingsComplete"] is False


@pytest.mark.parametrize("flags", [0x0001, 0x0100, 0x0400, 0x0800, 0x1000, 0x8000, 0x0020])
def test_utility_and_marker_events_do_not_require_shader_bindings(flags):
    # Native action identity takes precedence over an inherited draw label.
    action = SimpleNamespace(flags=flags, eventId=42)
    details = {"actionKind": "graphics", "shaders": {}, "inputs": [], "outputs": []}
    assert evidence.validate_pipeline_state(details, action) is None
    controller = SimpleNamespace(GetTextures=lambda: [])
    result = evidence.inventory(controller, Pipeline(), details, [], resource_id, str, action)
    assert result["bindingsComplete"] is True and result["bindingErrors"] == []


def test_buffer_only_compute_and_shader_without_descriptors_remain_valid():
    for pipeline in (Pipeline(), Pipeline(uavs={"CS": [binding(21)]}, srvs={"CS": [binding(31)]})):
        result = inventory(pipeline, [], "compute", {"compute": {"shaderId": 7}})
        assert result["bindingsComplete"] is True and result["bindingErrors"] == []
        assert all(row["resourceType"] == "buffer" for row in result["expectedOutputs"] + result["textureInputs"])
    assert evidence.validate_pipeline_state({"actionKind": "compute", "shaders": {"compute": {"shaderId": 7}}}) is None


def test_depth_only_draw_without_pixel_shader_and_no_targets_shader_are_not_empty_pipeline():
    result = inventory(Pipeline(depth=binding(91)), [texture(91, components=1)], "graphics", {})
    assert result["bindingsComplete"] is True
    assert result["expectedOutputs"][0]["kind"] == "dsv"
    result = inventory(Pipeline(), [], "graphics", {"vertex": {"shaderId": 7}})
    assert result["bindingsComplete"] is True


@pytest.mark.parametrize("field,value", [
    ("inputs", [{"resourceId": 31, "access": "constant-buffer"}]),
    ("outputs", [{"resourceId": 21, "access": "stream-output"}]),
    ("possibleOutputs", [{"resourceId": 91}]),
    ("renderTargets", [91]), ("depthTarget", 91),
])
def test_non_texture_io_evidence_prevents_a_false_all_empty_diagnostic(field, value):
    details = {"actionKind": "graphics", "shaders": {}, field: value}
    assert evidence.validate_pipeline_state(details) is None
    result = evidence.inventory(SimpleNamespace(GetTextures=lambda: []), Pipeline(), details, [], resource_id, str)
    assert result["bindingsComplete"] is True


def test_graphics_inventory_preserves_all_mrt_slots_depth_and_large_resource_ids():
    ids = [2**60 + index for index in range(1, 7)]
    targets = [binding(ids[0]), binding(0), *[binding(rid) for rid in ids[1:]]]
    targets[3] = binding(ids[2], mip=2, layer=5, wrapped=False)
    pipeline = Pipeline(targets, binding(91, mip=1, layer=4))
    result = inventory(pipeline, [texture(rid) for rid in ids] + [texture(91, components=1)],
                       "graphics", {"fragment": {}})
    outputs = result["expectedOutputs"]
    assert [(row["kind"], row["index"], row["resourceId"]) for row in outputs] == [
        ("rtv", index, rid) for index, rid in zip((0, 2, 3, 4, 5, 6), ids)
    ] + [("dsv", 0, 91)]
    assert (outputs[2]["mip"], outputs[2]["slice"]) == (2, 5)
    assert (outputs[-1]["mip"], outputs[-1]["slice"], outputs[-1]["componentCount"]) == (1, 4, 1)
    assert all(row["captureSupported"] for row in outputs)
    assert result["bindingsComplete"] and not result["bindingErrors"]


def test_descriptor_ranges_are_preserved_for_producer_consumer_matching():
    pipeline = Pipeline(srvs={"PS": [binding(31, mip=1, layer=2, num_mips=4, num_slices=6)]})
    result = inventory(pipeline, [texture(31)], "graphics", {"fragment": {}})
    row = result["textureInputs"][0]
    assert result["inventoryVersion"] == 3
    assert (row["mip"], row["numMips"], row["slice"], row["numSlices"]) == (1, 4, 2, 6)
    del pipeline.srvs["PS"][0].descriptor.numMips
    row = inventory(pipeline, [texture(31)], "graphics", {"fragment": {}})["textureInputs"][0]
    assert row["numMips"] is None  # A missing range is not a single-mip view.


@pytest.mark.parametrize("kind,count", [("uav", 0), ("srv", 0), ("srv", 1)])
@pytest.mark.parametrize("view_type", ["TextureType.Texture3D", "Texture 3D", SimpleNamespace(name="Texture3D")])
def test_native_volume_views_expose_mip_depth_instead_of_array_layer_sentinels(kind, count, view_type):
    volume = texture(31)
    volume.dimension, volume.depth, volume.arraysize = 3, 33, 1
    view = binding(31, mip=2, num_slices=count)
    view.descriptor.textureType = view_type
    pipeline = Pipeline(**{"uavs" if kind == "uav" else "srvs": {"CS": [view]}})
    result = inventory(pipeline, [volume], "compute", {"compute": {}})
    row = (result["expectedOutputs"] if kind == "uav" else result["textureInputs"])[0]
    assert result["bindingsComplete"]
    assert row["numSlices"] == row["viewSliceCount"] == row["mipDepth"] == 8
    assert row["textureDimension"] == 3 and row["resourceDepth"] == 33
    assert row["sliceKind"] == "depth" and row["depthSlice"] == 0
    assert row["nativeDescriptor"]["numSlices"] == count


@pytest.mark.parametrize("kind,view_type,first,count,expected", [
    ("uav", "Texture3D", 3, 2, (3, 2)),
    ("rtv", "Texture3D", 3, 65535, (3, 5)),
    ("srv", "Texture2DArray", 3, 2, (3, 2)),
])
def test_volume_restricted_views_preserve_w_slice_bounds(kind, view_type, first, count, expected):
    volume = SimpleNamespace(dimension=3, depth=32, arraysize=1)
    descriptor = SimpleNamespace(textureType=view_type, firstSlice=first, numSlices=count)
    actual = evidence.texture_slice_view(volume, descriptor, kind, 2)
    assert (actual["viewSliceFirst"], actual["viewSliceCount"]) == expected


@pytest.mark.parametrize("dimension,view_type,first,count", [
    (2, "Texture2DArray", 0, 0), (2, "Texture3D", 0, 0),
    (3, "Texture2DArray", 0, 0), (3, "Texture3D", 1, 0),
    (3, "Texture3D", 8, 1), (3, "Unknown", 0, 0),
])
def test_only_proven_full_volume_views_can_resolve_zero_slice_counts(dimension, view_type, first, count):
    texture = SimpleNamespace(dimension=dimension, depth=32 if dimension == 3 else 1, arraysize=8)
    descriptor = SimpleNamespace(textureType=view_type, firstSlice=first, numSlices=count)
    with pytest.raises(RuntimeError, match="slice range"):
        evidence.texture_slice_view(texture, descriptor, "uav", 2)


def test_single_depth_mip_is_still_a_volume_not_an_array():
    actual = evidence.texture_slice_view(SimpleNamespace(dimension=3, depth=8, arraysize=1),
        SimpleNamespace(textureType="Texture3D", firstSlice=0, numSlices=0), "uav", 3)
    assert actual["mipDepth"] == actual["viewSliceCount"] == 1
    assert actual["sliceKind"] == "depth"


def test_compute_inventory_uses_uav_and_srv_without_inherited_graphics_targets():
    pipeline = Pipeline([binding(11)], binding(12),
                        uavs={"CS": [binding(21, mip=3), binding(22)]},
                        srvs={"CS": [binding(0), binding(31, layer=2)]})
    result = inventory(pipeline, [texture(rid) for rid in (11, 12, 21, 31)],
                       "compute", {"compute": {}})
    assert not any(call[0] in ("rtv", "dsv") for call in pipeline.calls)
    assert [(row["kind"], row["resourceId"]) for row in result["expectedOutputs"]] == [
        ("uav", 21), ("uav", 22)]
    assert result["expectedOutputs"][0]["stage"] == "compute"
    buffer = result["expectedOutputs"][1]
    assert buffer["resourceType"] == "buffer" and not buffer["captureSupported"]
    assert (buffer["width"], buffer["height"], buffer["format"]) == (0, 0, "")
    assert [(row["index"], row["resourceId"], row["slice"]) for row in result["textureInputs"]] == [(1, 31, 2)]


def test_missing_binding_query_reports_partial_inventory_instead_of_hiding_it():
    class BrokenInputs(Pipeline):
        def GetReadOnlyResources(self, stage, only_used=True):
            raise RuntimeError("descriptor unavailable")

    result = inventory(BrokenInputs(uavs={"CS": [binding(21)]}), [texture(21)],
                       "compute", {"compute": {}})
    assert result["expectedOutputs"][0]["resourceId"] == 21
    assert result["bindingsComplete"] is False
    assert result["bindingErrors"] == [{"kind": "srv", "stage": "compute", "error": "descriptor unavailable"}]


def test_binding_query_supports_the_single_stage_api_signature():
    class LegacyPipeline:
        def GetReadWriteResources(self, stage):
            assert stage == "CS"
            return [binding(21)]

    assert evidence.bindings(LegacyPipeline(), "uav", "CS")[0].descriptor.resource == 21


class NavigationContext:
    def __init__(self, event=42, textures=(), pipeline=None, flags=2, destination=0):
        self.action = SimpleNamespace(eventId=event, flags=flags, copyDestination=destination) if event is not None else None
        self.textures = textures
        self.pipeline = pipeline or Pipeline()
        self.selected_event = 7
        self.calls = []
        self.followed_resources = {}
        self.current_resource = 0

    def GetAction(self, event):
        return self.action

    def GetTextures(self):
        return self.textures

    def CurPipelineState(self):
        return self.pipeline

    def SetEventID(self, excluded, selected, current):
        self.selected_event = selected
        self.calls.append(("event", excluded, selected, current))

    def ShowTextureViewer(self):
        self.calls.append(("show",))

    def GetTextureViewer(self):
        return self

    def ViewTexture(self, rid, component_type, focus):
        pytest.fail("Frame navigation must reuse the followed output, not open a locked texture tab")

    def ViewFollowedResource(self, follow, stage, index, array_element):
        coordinates = (follow, stage, index, array_element)
        self.calls.append(("follow",) + coordinates)
        self.current_resource = self.followed_resources.get(coordinates, 0)

    def GetCurrentResource(self):
        return self.current_resource

    def SetSelectedSubresource(self, subresource):
        self.calls.append(("subresource", subresource.mip, subresource.slice, subresource.sample))

    def SetChannelVisibility(self, red, green, blue, alpha):
        self.calls.append(("channels", red, green, blue, alpha))

    def SetHistogramRange(self, low, high):
        self.calls.append(("range", low, high))


RD = SimpleNamespace(CompType=SimpleNamespace(Typeless="typeless"),
                     ShaderStage=SimpleNamespace(Pixel="PS", Compute="CS", Vertex="VS"),
                     Subresource=lambda mip, layer, sample: SimpleNamespace(mip=mip, slice=layer, sample=sample))
QRD = SimpleNamespace(FollowType=SimpleNamespace(OutputColor="output-color", OutputDepth="output-depth",
                                               ReadWrite="read-write", ReadOnly="read-only"))


@pytest.mark.parametrize("channel,visibility", [
    ("rgba", (True, True, True, False)),
    ("rgb", (True, True, True, False)),
    ("r", (True, False, False, False)),
    ("g", (False, True, False, False)),
    ("b", (False, False, True, False)),
    ("a", (False, False, False, True)),
    ("depth", (True, False, False, False)),
])
def test_preview_navigation_applies_exact_event_texture_subresource_channel_and_range(channel, visibility):
    rid = 2**60 + 7
    context = NavigationContext(textures=[texture(rid)], pipeline=Pipeline([
        binding(0, wrapped=False), binding(rid, wrapped=False)]))
    context.followed_resources[("output-color", "PS", 1, 0)] = rid
    result = evidence.navigate(context, RD, {
        "eventId": 42, "resourceId": str(rid), "mip": 2, "slice": 5, "sample": 3,
        "channel": channel, "displayMapping": {"blackPoint": -0.5, "whitePoint": 8.0},
    }, resource_id, QRD)
    assert result is True and context.selected_event == 42
    assert context.calls == [
        ("event", [], 42, 42), ("show",), ("follow", "output-color", "PS", 1, 0),
        ("subresource", 2, 5, 3), ("channels", *visibility), ("range", -0.5, 8.0),
    ]


@pytest.mark.parametrize("kind,index,expected", [
    ("rtv", 3, ("output-color", "PS", 3, 0)),
    ("dsv", 0, ("output-depth", "PS", 0, 0)),
])
@pytest.mark.parametrize("nested_mapping", [False, True])
def test_preview_navigation_uses_exact_attachment_slot_even_when_same_resource_is_bound_twice(kind, index, expected, nested_mapping):
    rid = 91
    context = NavigationContext(textures=[texture(rid)], pipeline=Pipeline(
        [binding(rid, wrapped=False), binding(0, wrapped=False),
         binding(90, wrapped=False), binding(rid, wrapped=False)], binding(rid, wrapped=False)))
    context.followed_resources[expected] = rid
    location = {"attachmentKind": kind, "attachmentIndex": index, "attachmentStage": "fragment"}
    value = {"eventId": 42, "resourceId": rid}
    value.update({"displayMapping": location} if nested_mapping else location)
    assert evidence.navigate(context, RD, value, resource_id, QRD)
    assert ("follow",) + expected in context.calls
    assert context.current_resource == rid


@pytest.mark.parametrize("kind,follow", [("uav", "read-write"), ("srv", "read-only")])
def test_descriptor_navigation_uses_access_index_and_array_element_instead_of_evidence_list_position(kind, follow):
    rid = 2**60 + 23
    used = binding(rid)
    used.access = SimpleNamespace(stage="CS", index=17, arrayElement=6)
    repeated = binding(rid)
    repeated.access = SimpleNamespace(stage="CS", index=4, arrayElement=0)
    pipeline = Pipeline([binding(rid, wrapped=False)], binding(rid, wrapped=False),
                        **{kind + "s": {"CS": [repeated, used]}})
    context = NavigationContext(textures=[texture(rid)], pipeline=pipeline, flags=0x0004)
    context.followed_resources[(follow, "CS", 17, 6)] = rid
    assert evidence.navigate(context, RD, {
        "eventId": 42, "resourceId": rid,
        "attachmentKind": kind, "attachmentIndex": 1, "attachmentStage": "compute",
    }, resource_id, QRD)
    assert ("follow", follow, "CS", 17, 6) in context.calls
    assert not any(call[0] in ("rtv", "dsv") for call in pipeline.calls)
    assert all(call[1] == "CS" for call in pipeline.calls)


def test_present_navigation_follows_current_output_instead_of_stale_ui_attachment():
    rid = 2**60 + 29
    pipeline = Pipeline([binding(90, wrapped=False)])
    context = NavigationContext(textures=[texture(90), texture(rid)], pipeline=pipeline,
                                flags=0x0100, destination=rid)
    context.followed_resources[("output-color", "PS", 0, 0)] = rid
    assert evidence.navigate(context, RD, {"eventId": 42, "resourceId": rid}, resource_id, QRD)
    assert ("follow", "output-color", "PS", 0, 0) in context.calls
    assert not pipeline.calls


@pytest.mark.parametrize("location", [
    {}, {"attachmentKind": "rtv", "attachmentIndex": 3},
    {"attachmentKind": "uav", "attachmentIndex": 0, "attachmentStage": "compute"},
])
def test_missing_bound_attachment_never_falls_back_to_a_locked_texture_tab(location):
    context = NavigationContext(textures=[texture(91)], pipeline=Pipeline([binding(90, wrapped=False)]))
    with pytest.raises(ValueError, match="not bound"):
        evidence.navigate(context, RD, dict({"eventId": 42, "resourceId": 91}, **location), resource_id, QRD)
    assert not any(call[0] in ("follow", "subresource", "channels", "range") for call in context.calls)


def test_follow_selection_mismatch_does_not_modify_a_different_texture_display():
    context = NavigationContext(textures=[texture(91)], pipeline=Pipeline([binding(91, wrapped=False)]))
    context.followed_resources[("output-color", "PS", 0, 0)] = 90
    with pytest.raises(ValueError, match="could not select"):
        evidence.navigate(context, RD, {"eventId": 42, "resourceId": 91}, resource_id, QRD)
    assert not any(call[0] in ("subresource", "channels", "range") for call in context.calls)


@pytest.mark.parametrize("event,textures,value,error", [
    (None, [texture(91)], {"eventId": 42, "resourceId": 91}, "event"),
    (43, [texture(91)], {"eventId": 42, "resourceId": 91}, "event"),
    (42, [texture(90)], {"eventId": 42, "resourceId": 91}, "texture"),
])
def test_missing_or_changed_event_or_texture_never_changes_selection(event, textures, value, error):
    context = NavigationContext(event, textures)
    with pytest.raises(ValueError, match=error):
        evidence.navigate(context, RD, value, resource_id)
    assert context.selected_event == 7 and context.calls == []


def test_missing_event_argument_is_rejected_before_any_navigation():
    context = NavigationContext(textures=[texture(91)])
    with pytest.raises((KeyError, ValueError, TypeError)):
        evidence.navigate(context, RD, {"resourceId": 91}, resource_id)
    assert context.selected_event == 7 and context.calls == []


def test_stage_only_navigation_does_not_open_or_change_texture_view():
    context = NavigationContext()
    assert evidence.navigate(context, RD, {"eventId": 42}, resource_id)
    assert context.calls == [("event", [], 42, 42)]


def test_present_binding_uses_exact_backbuffer_instead_of_inherited_ui_target():
    presented = 2**60 + 17
    action = SimpleNamespace(flags=0x0100, copyDestination=presented, outputs=[91])
    controller = SimpleNamespace(GetTextures=lambda: [texture(91), texture(presented)])
    result = evidence.present_output_binding(action, controller, resource_id)
    assert result.resource == presented
    assert (result.firstMip, result.numMips, result.firstSlice, result.numSlices) == (0, 1, 0, 1)


@pytest.mark.parametrize("action", [
    None,
    SimpleNamespace(flags=2, copyDestination=91),
    SimpleNamespace(flags=0x0400, copyDestination=91),
    SimpleNamespace(flags=0x0100, copyDestination=0, outputs=[91]),
    SimpleNamespace(flags=0x0100, outputs=[91]),
])
def test_present_binding_rejects_wrong_action_or_missing_destination_without_resource_scan(action):
    def unexpected_scan():
        pytest.fail("An invalid Present request must fail before scanning capture textures")
    with pytest.raises(ValueError, match="Present event"):
        evidence.present_output_binding(action, SimpleNamespace(GetTextures=unexpected_scan), resource_id)


@pytest.mark.parametrize("textures", [
    [],
    [texture(90)],
    [SimpleNamespace(resourceId=91, dimension=1)],
    [SimpleNamespace(resourceId=91, dimension=3)],
])
def test_present_binding_rejects_absent_or_non_2d_target_instead_of_choosing_another_texture(textures):
    action = SimpleNamespace(flags=0x0100, copyDestination=91, outputs=[90])
    with pytest.raises(ValueError, match="available two-dimensional texture"):
        evidence.present_output_binding(action, SimpleNamespace(GetTextures=lambda: textures), resource_id)
