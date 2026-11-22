"""Physical capture values, semantic lanes and transfer coverage stay connected."""
import hashlib
import base64
import json
import struct
import types

import pytest

from src.cbuffer_transfer import CBufferTransferError, resolve_cbuffer_transfer
from src.resource_export import ResourceExportManager, ResourceExportError
from tests.test_bridge_resource_export import load_resource_export


def source(tmp_path, blocks, *, stage="fragment", api="Vulkan", raw=None):
    root = tmp_path / "source"
    root.mkdir(exist_ok=True)
    for index, block in enumerate(blocks):
        if raw is not None:
            data = raw[index] if isinstance(raw, list) else raw
            name = "cb-%d.bin" % index
            (root / name).write_bytes(data)
            block["raw"] = {"status": "captured", "path": name, "size": len(data),
                            "sha256": hashlib.sha256(data).hexdigest()}
    evidence = {"api": api, "constantBuffers": blocks}
    data = json.dumps(evidence).encode()
    name = stage + ".json"
    (root / name).write_bytes(data)
    return root, {"shaders": [{"stage": stage, "rawSha256": "a" * 64,
        "evidencePath": name, "evidenceSha256": hashlib.sha256(data).hexdigest()}]}


def block(fields, **kwargs):
    return {"name": "Material", "bind": 2, "space": 7, "bufferId": 100,
            "byteOffset": 256, "byteSize": 64, "layoutFields": fields, **kwargs}


def field(name="Color", offset=0, **kwargs):
    return {"name": name, "path": name, "byteOffset": offset, "byteSize": 16,
            "type": "float4", "baseType": "VarType.Float", "rows": 1, "columns": 4, **kwargs}


def mapping(variable="Color", **kwargs):
    return {"source": {"stage": "ps", "block": "Material", "variable": variable},
            "classification": "material", "target": "_Color", **kwargs}


@pytest.mark.parametrize("api", ["D3D11", "D3D12", "Vulkan", "OpenGL"])
def test_values_come_from_bound_byte_window_for_every_capture_api(tmp_path, api):
    root, bundle = source(tmp_path, [block([field()])], api=api, raw=struct.pack("<4f", .1, .2, 3, 4))
    result = resolve_cbuffer_transfer(bundle, {"constantBuffers": [mapping()]}, root)
    assert result["coverage"]["complete"]
    assert result["properties"][0]["value"] == pytest.approx([.1, .2, 3, 4])
    assert result["mappings"][0]["sourceIdentity"]["physicalIdentity"]["byteOffset"] == 256
    assert result["mappings"][0]["sourceIdentity"]["offsets"] == [0, 4, 8, 12]


def test_supplied_value_must_match_source_scalar_bits(tmp_path):
    root, bundle = source(tmp_path, [block([field()])], raw=struct.pack("<4f", .1, 2, 3, 4))
    spec = {"constantBuffers": [mapping(value=[.1, 2, 3, 4])]}
    assert resolve_cbuffer_transfer(bundle, spec, root)["coverage"]["complete"]
    spec["constantBuffers"][0]["value"][1] = 200
    with pytest.raises(CBufferTransferError, match="differs from captured"):
        resolve_cbuffer_transfer(bundle, spec, root)


def test_used_unmapped_fields_are_gaps_even_when_all_selected_mappings_work(tmp_path):
    root, bundle = source(tmp_path, [block([field(), field("Lighting", 16)])], raw=struct.pack("<8f", *range(8)))
    result = resolve_cbuffer_transfer(bundle, {"constantBuffers": [mapping()]}, root)
    assert result["coverage"]["requiredLaneCount"] == 8
    assert result["coverage"]["unmappedLaneCount"] == 4
    assert any(gap["code"] == "cbuffer_used_lanes_unmapped" for gap in result["gaps"])


def test_same_name_in_distinct_bindings_requires_physical_selector(tmp_path):
    root, bundle = source(tmp_path, [block([field()]), block([field()], bind=4)], raw=[struct.pack("<4f", *range(4))] * 2)
    with pytest.raises(CBufferTransferError, match="exactly one"):
        resolve_cbuffer_transfer(bundle, {"constantBuffers": [mapping()]}, root)
    catalog = resolve_cbuffer_transfer(bundle, {}, root)
    chosen = catalog["fields"][0]
    spec = {"constantBuffers": [mapping(source={"physicalId": chosen["physicalId"], "byteOffset": 0, "type": "float4"})]}
    result = resolve_cbuffer_transfer(bundle, spec, root)
    assert len(result["properties"]) == 1 and result["coverage"]["unmappedLaneCount"] == 4


def test_column_major_matrix_and_strided_array_preserve_logical_values(tmp_path):
    matrix = field("Transform", rows=2, columns=3, type="float2x3", byteSize=48, matrixByteStride=16, rowMajor=False)
    array = field("Weights", 48, rows=1, columns=1, type="float", elements=2, arrayByteStride=16, byteSize=32)
    raw = bytearray(80)
    for offset, value in [(0, 1), (4, 4), (16, 2), (20, 5), (32, 3), (36, 6), (48, 7), (64, 8)]:
        struct.pack_into("<f", raw, offset, value)
    root, bundle = source(tmp_path, [block([matrix, array], byteSize=80)], raw=bytes(raw))
    result = resolve_cbuffer_transfer(bundle, {"constantBuffers": [mapping("Transform", target="_Transform"), mapping("Weights", target="_Weights")]}, root)
    assert result["coverage"]["complete"]
    assert result["runtimeBindings"][0]["value"] == [1, 2, 3, 4, 5, 6]
    assert result["runtimeBindings"][1]["value"] == [7, 8]


def test_live_inline_constants_need_shape_and_do_not_require_buffer_resource(tmp_path):
    b = block([field()], bufferId=0, variablesStatus="available", variables=[{
        "name": "Color", "type": "VarType.Float", "rows": 1, "columns": 4, "float": [3, 2]}])
    root, bundle = source(tmp_path, [b])
    result = resolve_cbuffer_transfer(bundle, {"constantBuffers": [mapping()]}, root)
    assert result["properties"][0]["value"] == [3, 2, 0, 0]
    assert result["coverage"]["complete"]


def test_tampered_raw_bytes_are_rejected(tmp_path):
    root, bundle = source(tmp_path, [block([field()])], raw=struct.pack("<4f", *range(4)))
    (root / "cb-0.bin").write_bytes(b"x" * 16)
    with pytest.raises(CBufferTransferError, match="hash changed"):
        resolve_cbuffer_transfer(bundle, {}, root)


def test_semantic_merged_lanes_reuse_saved_names_and_source_bound_nonuse(tmp_path, monkeypatch):
    from src.shader_workspace import aliases
    root, bundle = source(tmp_path, [block([field()])], raw=struct.pack("<4f", 2, 4, 6, 8))
    view = {"sourceSha256": "b" * 64, "fields": [{"name": "SurfaceParams", "type": "float2", "components": [{"offset": 0}, {"offset": 8}], "evidence": "current source consumers"}],
            "usageReview": {"unusedComponents": [{"offset": 4, "type": "float"}, {"offset": 12, "type": "float"}]}}
    monkeypatch.setattr(aliases, "read_semantic_view", lambda key, shader_scope: view)
    spec = {"shader": {"organizedSources": [{"stage": "fragment", "sourceSha256": "b" * 64}]},
            "constantBuffers": [mapping("SurfaceParams", target="_SurfaceParams")]}
    result = resolve_cbuffer_transfer(bundle, spec, root)
    assert result["properties"][0]["value"] == [2, 6]
    assert result["coverage"]["reviewedUnusedLaneCount"] == 2 and result["coverage"]["complete"]
    spec["shader"]["organizedSources"][0]["sourceSha256"] = "c" * 64
    with pytest.raises(CBufferTransferError, match="exactly one"):
        resolve_cbuffer_transfer(bundle, spec, root)


def test_bridge_missing_values_make_partial_and_native_artifact_survives(tmp_path):
    bridge = load_resource_export()
    writer = bridge._ArtifactWriter(str(tmp_path))
    request = {key: "x" for key in ("ticketId", "requestSha256", "selectionSha256", "captureId", "captureSessionId")}
    request.update(eventId=23, instance=0, view=0, artifactDir=str(tmp_path))
    state = {"request": request, "writer": writer, "bundle": {"mesh": {"attributes": []}, "textures": []}}
    evidence = {"fragment": {"shaderId": 1, "constantBuffers": [block([field()], variablesStatus="unavailable")]}}
    bridge.attach_raw_evidence(state, evidence, {"shaderRaw": {"fragment": b"native shader"}})
    result = bridge.finalize_capture(state, evidence)
    assert result["status"] == "partial"
    assert result["bundle"]["completion"]["incomplete"] == ["cbuffer.fragment.0.values:unavailable"]
    assert result["bundle"]["shaders"][0]["rawArtifact"]["path"] == "shaders/fragment.native.bin"


def test_multi_file_unity_assets_are_hash_bound_and_cannot_collide(tmp_path):
    manager = ResourceExportManager(tmp_path / "exports")
    source_file = tmp_path / "include.hlsl"
    source_file.write_text("float BreadValue() { return 1; }", encoding="utf-8")
    digest = hashlib.sha256(source_file.read_bytes()).hexdigest()
    spec = manager._normalize_transfer_spec({"unity": {"assets": [
        {"path": "Shaders/Surface.hlsl", "kind": "include", "sourcePath": str(source_file), "sha256": digest},
        {"path": "Runtime/Effect.cs", "kind": "script", "content": "class BreadEffect {}"}]}})
    output = tmp_path / "target"
    manager._write_unity_scaffold(output, spec, {})
    assert (output / "UnityURP/Shaders/Surface.hlsl").read_bytes() == source_file.read_bytes()
    source_file.write_text("changed", encoding="utf-8")
    with pytest.raises(ResourceExportError, match="hash changed"):
        manager._unity_asset_bytes(spec["unity"]["assets"][0])
    for path in ("../escape.cs", "Shaders/BreadExport.shader", "CaptureSources/Original.shader"):
        with pytest.raises(ResourceExportError):
            manager._normalize_transfer_spec({"unity": {"assets": [{"path": path, "kind": "script", "content": "x"}]}})


class BufferController:
    def __init__(self):
        self.current = 23
        self.materialized = 23
        self.calls = []
    def GetBuffers(self):
        return [types.SimpleNamespace(resourceId=9, length=32)]
    def GetCurrentEventID(self):
        return self.current
    def ReplayFrameEventForAnalysis(self, event):
        self.calls.append(("analysis", event))
        self.materialized = event
    def RestoreFrameEventAfterAnalysis(self):
        self.calls.append(("restore", self.current))
        self.materialized = self.current
    def SetFrameEvent(self, event, force):
        self.calls.append(("select", event, force))
        self.current = self.materialized = event
    def GetBufferData(self, resource, offset, count):
        assert resource == 9
        return bytes([self.materialized]) * count


def test_buffer_windows_capture_before_inputs_and_after_uav_outputs(tmp_path):
    bridge = load_resource_export()
    controller = BufferController()
    requests = [{"resourceId": 9, "stage": "compute", "kind": "read-write-resource",
                 "descriptor": {"byteOffset": 4, "byteSize": 8, "elementByteSize": 4}}]
    buffers, gaps = bridge._capture_bound_buffers(controller, requests, {}, 23, bridge._ArtifactWriter(str(tmp_path)))
    assert not gaps and len(buffers) == 1
    record = buffers[0]
    assert record["capturePhase"] == "before-action" and record["replayEventId"] == 22
    assert (tmp_path / record["raw"]["path"]).read_bytes() == bytes([22]) * 8
    assert (tmp_path / record["postRaw"]["path"]).read_bytes() == bytes([23]) * 8
    assert controller.calls == [("analysis", 22), ("restore", 23), ("select", 23, True)]
    assert record["replayRestoration"]["restored"] and controller.current == 23


def test_buffer_snapshot_read_failure_still_restores_selected_eid(tmp_path):
    bridge = load_resource_export()
    controller = BufferController()
    controller.GetBufferData = lambda *args: (_ for _ in ()).throw(RuntimeError("read failed"))
    buffers, gaps = bridge._capture_bound_buffers(controller, [{"resourceId": 9, "stage": "vertex",
        "kind": "read-only-buffer", "descriptor": {"byteOffset": 0, "byteSize": 16}}], {}, 23,
        bridge._ArtifactWriter(str(tmp_path)))
    assert buffers[0]["status"] == "unavailable" and gaps[0]["reason"] == "read failed"
    assert controller.current == 23 and controller.calls[-1] == ("select", 23, True)


def test_buffer_snapshot_rejects_unverified_restore_and_unbounded_window(tmp_path):
    bridge = load_resource_export()
    controller = BufferController()
    controller.SetFrameEvent = lambda *args: setattr(controller, "current", 22)
    request = {"resourceId": 9, "stage": "vertex", "kind": "read-only-buffer",
               "descriptor": {"byteOffset": 0, "byteSize": 16}}
    with pytest.raises(RuntimeError, match="could not be restored"):
        bridge._capture_bound_buffers(controller, [request], {}, 23, bridge._ArtifactWriter(str(tmp_path)))
    controller = BufferController()
    controller.GetBuffers = lambda: [types.SimpleNamespace(resourceId=9, length=1024 ** 3)]
    buffers, gaps = bridge._capture_bound_buffers(controller, [dict(request, descriptor={"byteOffset": 0, "byteSize": 0})], {}, 23,
                                                  bridge._ArtifactWriter(str(tmp_path)))
    assert not buffers and "64 MiB" in gaps[0]["reason"] and not controller.calls


def test_per_instance_stream_is_available_as_a_buffer_with_selection_evidence(tmp_path):
    bridge = load_resource_export()
    controller = BufferController()
    mesh = {"instance": 3, "attributes": [{"index": 2, "perInstance": True, "instanceStepRate": 2,
            "buffer": {"resourceId": 9, "byteOffset": 0, "byteSize": 32, "byteStride": 16}}]}
    buffers, gaps = bridge._capture_bound_buffers(controller, [], mesh, 23, bridge._ArtifactWriter(str(tmp_path)))
    assert not gaps
    assert buffers[0]["bindings"][0]["access"] == {"attributeIndex": 2, "instanceStepRate": 2, "instance": 3}


def test_cbuffer_alias_uses_before_action_bytes_instead_of_writable_output(tmp_path):
    root, bundle = source(tmp_path, [block([field()], bufferId=9)], raw=struct.pack("<4f", 100, 200, 300, 400))
    before = struct.pack("<4f", 1, 2, 3, 4)
    (root / "before.bin").write_bytes(before)
    bundle["buffers"] = [{"resourceId": 9, "byteOffset": 256, "byteSize": 16,
        "status": "captured", "capturePhase": "before-action", "selectedEventId": 23,
        "raw": {"path": "before.bin", "sha256": hashlib.sha256(before).hexdigest(), "size": 16}}]
    result = resolve_cbuffer_transfer(bundle, {"constantBuffers": [mapping()]}, root)
    assert result["properties"][0]["value"] == [1, 2, 3, 4]
    assert result["mappings"][0]["valueAuthority"] == "captured-bytes-before-action"


def test_explicit_matrix_projection_assembles_typed_lanes_without_reinterpretation(tmp_path):
    root, bundle = source(tmp_path, [block([field("Row0", 0), field("Row1", 16)])], raw=struct.pack("<8f", *range(8)))
    catalog = resolve_cbuffer_transfer(bundle, {}, root)
    selected = {"physicalId": catalog["fields"][0]["physicalId"], "type": "float2x4",
                "componentOffsets": list(range(0, 32, 4))}
    spec = {"constantBuffers": [mapping(source=selected, target="_Matrix")]}
    result = resolve_cbuffer_transfer(bundle, spec, root)
    assert result["coverage"]["complete"]
    assert result["runtimeBindings"][0]["kind"] == "matrix"
    assert result["runtimeBindings"][0]["value"] == list(range(8))
    selected["type"] = "uint2x4"
    with pytest.raises(CBufferTransferError, match="cannot reinterpret"):
        resolve_cbuffer_transfer(bundle, spec, root)


def test_active_typed_layout_and_saved_semantic_name_recover_stripped_lanes(tmp_path, monkeypatch):
    from src.shader_workspace import aliases
    root, bundle = source(tmp_path, [block([field("unknown", baseType="VarType.UInt", type="uint4")])], raw=struct.pack("<4f", 1, 2, 3, 4))
    members = [{"name": "MaterialValues", "offset": 0}]
    active = {"block": "Readable", "bind": 2, "space": 7, "byteSize": 64, "members": members,
              "memberMappingComplete": True, "leaves": [{"path": "MaterialValues", "offset": 0,
                  "byteSize": 16, "baseType": "float", "componentByteSize": 4}]}
    identity = {"schema": "replaylab-cbuffer-alias-identity/v1", "rawSha256": "a" * 64,
        "stage": "fragment", "activeDisplayShaderRawSha256": "d" * 64,
        "activeReplacementShaderRawSha256": "d" * 64, "activeDisplayBlocks": [active]}
    def read_view(key, shader_scope):
        if shader_scope["shaderRawSha256"] != "d" * 64:
            return None
        return {"sourceSha256": "b" * 64, "fields": [{"name": "SurfaceColor", "type": "float4",
            "components": [{"offset": value} for value in (0, 4, 8, 12)], "evidence": "validated source"}]}
    monkeypatch.setattr(aliases, "read_semantic_view", read_view)
    spec = {"shader": {"organizedSources": [{"stage": "fragment", "sourceSha256": "b" * 64,
             "cbufferIdentity": identity}]}, "constantBuffers": [mapping("SurfaceColor")]}
    result = resolve_cbuffer_transfer(bundle, spec, root)
    assert result["coverage"]["complete"] and result["properties"][0]["value"] == [1, 2, 3, 4]
    identity["rawSha256"] = "e" * 64
    with pytest.raises(CBufferTransferError, match="exactly one"):
        resolve_cbuffer_transfer(bundle, spec, root)


def test_real_published_semantic_store_is_reused_without_renaming(tmp_path, monkeypatch):
    from src.shader_workspace import aliases
    project, user = tmp_path / "aliases", tmp_path / "user"
    project.mkdir()
    monkeypatch.setattr(aliases, "project_store_root", lambda: project)
    monkeypatch.setattr(aliases, "user_store_root", lambda: user)
    root, bundle = source(tmp_path, [block([field()])], raw=struct.pack("<4f", 1, 2, 3, 4))
    members = [{"name": "Color", "offset": 0}]
    key = aliases.layout_sha256("Material", members)
    aliases.publish_aliases("Material", members, [], shader_scope={
        "shaderRawSha256": "a" * 64, "stage": "fragment", "bind": 2, "space": 7, "layoutSha256": key},
        semantic_fields=[{"name": "AlbedoTint", "type": "float4", "components": [{"offset": n} for n in (0, 4, 8, 12)],
                          "status": "inferred", "provenance": "shader", "evidence": "used by material evaluation"}],
        semantic_source_sha256="b" * 64)
    spec = {"shader": {"organizedSources": [{"stage": "ps", "sourceSha256": "b" * 64}]},
            "constantBuffers": [mapping("AlbedoTint")]}
    result = resolve_cbuffer_transfer(bundle, spec, root)
    assert result["mappings"][0]["displayName"] == "AlbedoTint"
    assert result["properties"][0]["value"] == [1, 2, 3, 4]
    assert result["coverage"]["complete"]


def test_joint_weight_channels_survive_without_inventing_a_skeleton(tmp_path):
    manager = ResourceExportManager(tmp_path / "exports")
    root, output = tmp_path / "source", tmp_path / "target"
    root.mkdir()
    output.mkdir()
    attributes = []
    for index, (name, width, values) in enumerate([
        ("Position", 3, [0, 0, 0, 1, 0, 0, 0, 1, 0]),
        ("Bones", 4, [0, 1, 2, 3] * 3), ("Weights", 4, [.125, .25, .5, .125] * 3)]):
        raw = struct.pack("<%df" % len(values), *values)
        path = "%d.bin" % index
        (root / path).write_bytes(raw)
        attributes.append({"index": index, "name": name, "canonical": {"status": "captured",
            "encoding": "float32le", "path": path, "sha256": hashlib.sha256(raw).hexdigest(), "count": 3, "components": width}})
    bundle = {"mesh": {"attributes": attributes, "indexed": False, "topology": "TriangleList"}}
    spec = manager._normalize_transfer_spec({"attributeMappings": [{"source": {"attributeIndex": index}, "target": target}
        for index, target in enumerate(("POSITION", "JOINTS_0", "WEIGHTS_0"))]})
    receipt = manager._write_gltf(output, root, bundle, "a" * 64, spec)
    assert receipt["attributes"] == ["POSITION", "_CAPTURE_JOINTS_0", "_CAPTURE_WEIGHTS_0"]
    assert [item["requestedSemantic"] for item in receipt["vertexChannels"]] == ["POSITION", "JOINTS_0", "WEIGHTS_0"]
    gltf = json.loads((output / "asset.gltf").read_text())
    assert "skins" not in gltf and "skin" not in gltf["nodes"][0]


def test_64bit_material_values_require_and_support_an_exact_raw_buffer(tmp_path):
    value = 1.0000000000000002
    raw = struct.pack("<d", value)
    root, bundle = source(tmp_path, [block([field("HighPrecision", type="double", baseType="VarType.Double",
                                           rows=1, columns=1, byteSize=8)])], raw=raw)
    spec = {"constantBuffers": [mapping("HighPrecision", target="_HighPrecision")]}
    result = resolve_cbuffer_transfer(bundle, spec, root)
    assert not result["properties"] and not result["coverage"]["complete"]
    assert any(gap["code"] == "cbuffer_64bit_value_requires_raw_buffer" for gap in result["gaps"])
    spec["constantBuffers"][0]["binding"] = "raw-buffer"
    result = resolve_cbuffer_transfer(bundle, spec, root)
    assert result["coverage"]["complete"]
    assert base64.b64decode(result["runtimeBindings"][0]["bufferBase64"]) == raw
    assert result["runtimeBindings"][0]["stride"] == 8


def test_inline_array_values_use_reflected_element_paths_and_shape(tmp_path):
    array = field("Offsets", type="float2", columns=2, elements=3, arrayByteStride=16, byteSize=48)
    values = [{"name": "Offsets", "members": [
        {"name": "[%d]" % index, "rows": 1, "columns": 2, "float": [index + 1]} for index in range(3)]}]
    root, bundle = source(tmp_path, [block([array], bufferId=0, variablesStatus="available", variables=values)])
    result = resolve_cbuffer_transfer(bundle, {"constantBuffers": [mapping("Offsets", target="_Offsets")]}, root)
    assert result["coverage"]["complete"]
    assert result["runtimeBindings"][0]["value"] == [1, 0, 2, 0, 3, 0]


def test_legacy_live_float_union_is_not_reinterpreted_as_double(tmp_path):
    f = field("HighPrecision", type="double", baseType="VarType.Double", rows=1, columns=1, byteSize=8)
    root, bundle = source(tmp_path, [block([f], bufferId=0, variablesStatus="available", variables=[{
        "name": "HighPrecision", "type": "VarType.Double", "rows": 1, "columns": 1, "float": [2]}])])
    result = resolve_cbuffer_transfer(bundle, {"constantBuffers": [mapping("HighPrecision", binding="raw-buffer")]}, root)
    assert not result["coverage"]["complete"] and not result["runtimeBindings"]
    assert any(gap["code"] == "cbuffer_live_scalar_encoding_unavailable" for gap in result["gaps"])


def test_hidden_append_counter_does_not_masquerade_as_complete_input(tmp_path):
    bridge = load_resource_export()
    controller = BufferController()
    requests = [{"resourceId": 9, "stage": "compute", "kind": "read-write-resource",
        "descriptor": {"byteOffset": 0, "byteSize": 16, "flags": 2, "bufferStructCount": 17}}]
    buffers, gaps = bridge._capture_bound_buffers(controller, requests, {}, 23, bridge._ArtifactWriter(str(tmp_path)))
    assert buffers[0]["status"] == "captured"
    assert gaps[0]["kind"] == "hidden-uav-counter" and gaps[0]["blocking"]
    assert gaps[0]["outputCounterValue"] == 17 and gaps[0]["outputCounterPhase"] == "after-action"


def test_dispatch_does_not_export_a_previous_draw_vertex_layout(tmp_path):
    bridge = load_resource_export()
    action = types.SimpleNamespace(flags=2)
    rd = types.SimpleNamespace(ActionFlags=types.SimpleNamespace(Dispatch=2))
    # An object without any pipeline methods proves this branch did not read
    # the previous draw's cached IA layout.
    mesh = bridge._action_mesh(None, object(), action, {"instance": 0, "view": 0}, rd, None, False)
    assert mesh["source"] == "not-applicable-dispatch" and not mesh["attributes"]
    assert mesh["managedMeshImport"]["reasonId"] == "dispatch-requires-render-plan"
    manager = ResourceExportManager(tmp_path / "exports")
    result = manager._write_gltf(tmp_path, tmp_path, {"mesh": mesh}, "a" * 64, {})
    assert result["geometryApplicable"] is False and result["executionKind"] == "effect"
    gltf = json.loads((tmp_path / "asset.gltf").read_text())
    assert not gltf.get("meshes") and not gltf.get("accessors") and not gltf.get("nodes")
    assert (tmp_path / "asset.bin").read_bytes() == b""


def test_raw_binding_keeps_noncanonical_bool_storage_bits(tmp_path):
    raw = struct.pack("<I", 2)
    root, bundle = source(tmp_path, [block([field("Flag", type="bool", baseType="VarType.Bool",
                                           rows=1, columns=1, byteSize=4)])], raw=raw)
    result = resolve_cbuffer_transfer(bundle, {"constantBuffers": [mapping("Flag", binding="raw-buffer")]}, root)
    assert result["coverage"]["complete"]
    assert base64.b64decode(result["runtimeBindings"][0]["bufferBase64"]) == raw
