from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
MODULE_PATH = REPOSITORY / "renderdoc_mcp_bridge" / "resource_export.py"
BRIDGE_PATH = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
HOST_MODULE_PATH = REPOSITORY / "mcp_host" / "src" / "resource_export.py"
INDEXED_FLAG = 0x010000


def _rd_module():
    return types.SimpleNamespace(
        ActionFlags=types.SimpleNamespace(Indexed=INDEXED_FLAG)
    )


def load_resource_export():
    spec = importlib.util.spec_from_file_location(
        "renderdoc_mcp_bridge_resource_export_helpers", MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_host_resource_export():
    spec = importlib.util.spec_from_file_location(
        "replaylab_host_resource_export_contract", HOST_MODULE_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _ResourceId:
    def __init__(self, value: int) -> None:
        self.value = value

    def __int__(self) -> int:
        return self.value

    def __bool__(self) -> bool:
        return bool(self.value)


class _Format:
    def __init__(
        self,
        *,
        comp_type: str = "Float",
        count: int = 1,
        width: int = 4,
        special: bool = False,
        bgra: bool = False,
    ) -> None:
        self.compType = comp_type
        self.compCount = count
        self.compByteWidth = width
        self.type = "Regular" if not special else "R10G10B10A2"
        self.flags = 0
        self._special = special
        self._bgra = bgra

    def Name(self) -> str:
        return "%s%d" % (self.compType, self.compCount)

    def Special(self) -> bool:
        return self._special

    def BGRAOrder(self) -> bool:
        return self._bgra

    def SRGBCorrected(self) -> bool:
        return self.compType == "UNormSRGB"

    def BlockFormat(self) -> bool:
        return False

    def ElementSize(self) -> int:
        return self.compCount * self.compByteWidth

    def YUVSubsampling(self) -> int:
        return 0

    def YUVPlaneCount(self) -> int:
        return 1


class _MeshController:
    def __init__(self, vertex_values: list[float], index_values: list[int] | None) -> None:
        self.vertex_id = _ResourceId(11)
        self.index_id = _ResourceId(22)
        self.vertex_data = struct.pack("<%df" % len(vertex_values), *vertex_values)
        self.index_data = (
            struct.pack("<%dH" % len(index_values), *index_values)
            if index_values is not None
            else b""
        )
        self.buffer_reads: list[int] = []

    def GetBuffers(self):
        values = [types.SimpleNamespace(resourceId=self.vertex_id)]
        if self.index_data:
            values.append(types.SimpleNamespace(resourceId=self.index_id))
        return values

    def GetResources(self):
        return []

    def GetBufferData(self, resource, offset, length):
        value = int(resource)
        self.buffer_reads.append(value)
        data = self.vertex_data if value == 11 else self.index_data
        return data[offset : offset + length]


def _pipeline(controller: _MeshController, indexed: bool):
    vertex_binding = types.SimpleNamespace(
        resourceId=controller.vertex_id,
        byteOffset=0,
        byteSize=len(controller.vertex_data),
        byteStride=4,
    )
    if indexed:
        index_binding = types.SimpleNamespace(
            resourceId=controller.index_id,
            byteOffset=0,
            byteSize=len(controller.index_data),
            byteStride=2,
        )
    else:
        index_binding = types.SimpleNamespace(
            resourceId=_ResourceId(0), byteOffset=0, byteSize=0, byteStride=0
        )
    attribute = types.SimpleNamespace(
        name="POSITION0",
        vertexBuffer=0,
        byteOffset=0,
        perInstance=False,
        instanceRate=0,
        format=_Format(),
        genericEnabled=False,
        genericValue=None,
        used=True,
        floatCastWrong=False,
    )
    return types.SimpleNamespace(
        GetIBuffer=lambda: index_binding,
        GetVBuffers=lambda: [vertex_binding],
        GetVertexInputs=lambda: [attribute],
        GetPrimitiveTopology=lambda: "TriangleList",
        IsRestartEnabled=lambda: False,
    )


class ResourceExportPureHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_resource_export()

    def test_new_export_scaffolds_use_bread_defaults_and_keep_protocol_ids(self) -> None:
        host = load_host_resource_export()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            spec = {
                "material": {},
                "shader": {"unityShaderSource": "Shader \"Bread/Test\" {}\n"},
            }

            unity_files = host.ResourceExportManager._write_unity_scaffold(output, spec, {})
            blender_files = host.ResourceExportManager._write_blender_scaffold(output, spec, {})

            self.assertIn("UnityURP/Shaders/BreadExport.shader", unity_files)
            self.assertNotIn("UnityURP/Shaders/ReplayLabExport.shader", unity_files)
            self.assertIn("# Bread Unity URP adapter", (output / "UnityURP" / "README.md").read_text(encoding="utf-8"))
            blender_script = (output / "Blender" / "import_resource.py").read_text(encoding="utf-8")
            self.assertIn('"BreadMaterial"', blender_script)
            self.assertIn("Bread import complete", blender_script)
            self.assertNotIn("ReplayLabMaterial", blender_script)
            self.assertIn("Blender/adapter.json", blender_files)
            # Serialized protocol IDs remain backward compatible.
            adapter = json.loads((output / "Blender" / "adapter.json").read_text(encoding="utf-8"))
            self.assertEqual("replaylab-blender-adapter/v1", adapter["schema"])

    def test_real_host_ticket_shape_passes_capture_validation(self) -> None:
        operation_id = "1" * 32
        capture_session_id = "2" * 32
        capture_id = "3" * 64
        with tempfile.TemporaryDirectory() as temporary:
            managed_root = Path(temporary) / "managed"
            host = load_host_resource_export()
            prepared = host.ResourceExportManager(managed_root).prepare_ticket(
                operation_id=operation_id,
                event_id=47,
                instance=2,
                view=0,
                asset_name="hero",
                capture_session_id=capture_session_id,
                capture_id=capture_id,
                api="D3D12",
            )
            ticket = prepared["ticket"]
            request_sha256 = prepared["operation"]["requestSha256"]
            capture_dir = managed_root / "operations" / operation_id / "capture"
            params = {
                "event_id": 47,
                "instance": 2,
                "view": 0,
                "request_sha256": request_sha256,
                "export_ticket": ticket,
                "ticket_path": prepared["ticketPath"],
                "ticket_sha256": prepared["ticketSha256"],
            }

            validated = self.module.validate_capture_request(
                params, capture_id, capture_session_id
            )

            self.assertEqual(request_sha256, validated["requestSha256"])
            self.assertEqual(str(capture_dir), validated["artifactDir"])
            self.assertEqual(
                self.module.canonical_request_sha256(
                    capture_id, capture_session_id, 47, 2, 0
                ),
                validated["selectionSha256"],
            )

    def test_nonzero_view_fails_until_exact_multiview_selection_exists(self) -> None:
        with self.assertRaisesRegex(ValueError, "multiview"):
            self.module.validate_inspect_request(
                {"event_id": 1, "instance": 0, "view": 1},
                "",
                "2" * 32,
            )

    def test_ticket_event_mismatch_fails_closed(self) -> None:
        operation_id = "1" * 32
        session = "2" * 32
        request_sha = "4" * 64
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "managed"
            capture = root / "operations" / operation_id / "capture"
            capture.mkdir(parents=True)
            ticket = {
                "ticket_id": operation_id,
                "managed_root": str(root),
                "relative_dir": "operations/%s/capture" % operation_id,
                "request_sha256": request_sha,
                "event_id": 99,
            }
            with self.assertRaisesRegex(ValueError, "event_id"):
                self.module.validate_capture_request(
                    {
                        "event_id": 7,
                        "instance": 0,
                        "view": 0,
                        "request_sha256": request_sha,
                        "export_ticket": ticket,
                    },
                    "",
                    session,
                )

    def test_fake_capture_bundle_is_accepted_by_host_record_capture(self) -> None:
        operation_id = "5" * 32
        session = "6" * 32
        host = load_host_resource_export()
        with tempfile.TemporaryDirectory() as temporary:
            manager = host.ResourceExportManager(Path(temporary) / "managed")
            prepared = manager.prepare_ticket(
                operation_id=operation_id,
                event_id=23,
                instance=0,
                view=0,
                asset_name="triangle",
                capture_session_id=session,
                api="D3D11",
            )
            request = self.module.validate_capture_request(
                {
                    "event_id": 23,
                    "instance": 0,
                    "view": 0,
                    "request_sha256": prepared["operation"]["requestSha256"],
                    "export_ticket": prepared["ticket"],
                    "ticket_path": prepared["ticketPath"],
                    "ticket_sha256": prepared["ticketSha256"],
                },
                "",
                session,
            )
            controller = _MeshController([0.0, 1.0, 2.0], [0, 1, 2])
            pipeline = _pipeline(controller, True)
            action = types.SimpleNamespace(
                numIndices=3,
                numVertices=3,
                numInstances=1,
                indexOffset=0,
                vertexOffset=0,
                instanceOffset=0,
                baseVertex=0,
                eventId=23,
                flags=INDEXED_FLAG,
                customName="triangle",
                children=[],
            )
            controller.GetRootActions = lambda: [action]
            controller.GetPipelineState = lambda: pipeline
            controller.GetTextures = lambda: []
            controller.GetAPIProperties = lambda: types.SimpleNamespace(
                pipelineType="D3D11"
            )

            capture_state = self.module.capture_controller(
                controller, _rd_module(), request, []
            )
            response = self.module.finalize_capture(capture_state, {})
            operation = manager.adopt_completed_capture(operation_id)
            self.assertIsNotNone(operation)
            operation = manager.record_capture(operation_id, response)

            self.assertEqual("captured", operation["status"])
            self.assertEqual("replaylab-resource-export-capture/v1", response["schema"])
            self.assertEqual(1, response["schemaVersion"])
            self.assertEqual("replaylab-resource-export-bundle/v1", response["bundle"]["schema"])
            self.assertEqual(1, response["bundle"]["schemaVersion"])
            self.assertEqual("", response["captureId"])
            self.assertEqual(session, response["captureSessionId"])
            self.assertTrue(all("size" in record for record in response["bundle"]["files"]))

    def test_partial_capture_without_manifest_is_not_replayed_or_adopted(self) -> None:
        host = load_host_resource_export()
        with tempfile.TemporaryDirectory() as temporary:
            manager = host.ResourceExportManager(Path(temporary) / "managed")
            operation_id = "7" * 32
            manager.prepare_ticket(
                operation_id=operation_id,
                event_id=3,
                instance=0,
                view=0,
                asset_name="partial",
                capture_session_id="8" * 32,
                api="D3D11",
            )
            capture = (
                Path(temporary)
                / "managed"
                / "operations"
                / operation_id
                / "capture"
            )
            (capture / "orphan.bin").write_bytes(b"partial")
            with self.assertRaisesRegex(
                host.ResourceExportError, "partial files without a completion manifest"
            ):
                manager.adopt_completed_capture(operation_id)

    def test_regular_decode_preserves_normalisation_and_bgra(self) -> None:
        fmt = self.module.normalise_resource_format(
            _Format(comp_type="UNorm", count=4, width=1, bgra=True)
        )
        values = self.module.decode_regular_vertex_element(
            fmt, bytes([0, 64, 128, 255])
        )
        self.assertAlmostEqual(128.0 / 255.0, values[0])
        self.assertAlmostEqual(64.0 / 255.0, values[1])
        self.assertEqual(0.0, values[2])
        self.assertEqual(1.0, values[3])

    def test_special_format_never_manufactures_canonical_values(self) -> None:
        fmt = self.module.normalise_resource_format(
            _Format(comp_type="UNorm", count=4, width=1, special=True)
        )
        with self.assertRaises(self.module.UnsupportedVertexFormat):
            self.module.decode_regular_vertex_element(fmt, b"\0\0\0\0")

    def test_integer_ids_are_not_rounded_into_float32(self) -> None:
        fmt = self.module.normalise_resource_format(_Format(comp_type="UInt", count=1, width=4))
        self.assertEqual(self.module.encode_canonical_float32(fmt, [struct.pack("<I", 65535)]),
                         struct.pack("<f", 65535))
        with self.assertRaisesRegex(self.module.UnsupportedVertexFormat, "lose bits"):
            self.module.encode_canonical_float32(fmt, [struct.pack("<I", 16777217)])

    def test_float64_precision_loss_retains_raw_instead_of_rounding(self) -> None:
        fmt = self.module.normalise_resource_format(_Format(comp_type="Double", count=1, width=8))
        self.assertEqual(self.module.encode_canonical_float32(fmt, [struct.pack("<d", 0.5)]), struct.pack("<f", 0.5))
        for value in (1.000000000000001, 1e300):
            with self.assertRaises(self.module.UnsupportedVertexFormat):
                self.module.encode_canonical_float32(fmt, [struct.pack("<d", value)])

    def test_attribute_location_is_never_fabricated_from_layout_order(self) -> None:
        controller = _MeshController([0.0, 1.0], None)
        pipeline = _pipeline(controller, False)
        attribute = pipeline.GetVertexInputs()[0]
        record = self.module._attribute_metadata(
            attribute, 9, pipeline.GetVBuffers()
        )
        self.assertIsNone(record["location"])
        self.assertEqual("unavailable", record["locationStatus"])
        self.assertEqual("inferred", record["semanticStatus"])

        attribute.location = 7
        observed = self.module._attribute_metadata(
            attribute, 9, pipeline.GetVBuffers()
        )
        self.assertEqual(7, observed["location"])
        self.assertEqual("observed", observed["locationStatus"])

    def _captured_position_values(self, *, indexed: bool) -> tuple[list[int], list[float]]:
        controller = _MeshController(
            [float(value) for value in range(32)], [1, 3] if indexed else None
        )
        pipeline = _pipeline(controller, indexed)
        action = types.SimpleNamespace(
            numIndices=2,
            numVertices=2,
            numInstances=1,
            indexOffset=0,
            vertexOffset=7,
            instanceOffset=0,
            baseVertex=5,
            eventId=19,
            flags=INDEXED_FLAG if indexed else 0,
        )
        request = {"instance": 0, "view": 0}
        with tempfile.TemporaryDirectory() as temporary:
            writer = self.module._ArtifactWriter(temporary)
            mesh = self.module._mesh(
                controller, pipeline, action, request, _rd_module(), writer, True
            )
            logical_path = Path(temporary) / mesh["logicalVertexIndices"]["path"]
            logical_raw = logical_path.read_bytes()
            logical = list(struct.unpack("<%dI" % (len(logical_raw) // 4), logical_raw))
            canonical = mesh["attributes"][0]["canonical"]
            payload = (Path(temporary) / canonical["path"]).read_bytes()
            values = list(struct.unpack("<%df" % (len(payload) // 4), payload))
            return logical, values

    def test_vulkan_zero_stride_reads_one_bound_element(self) -> None:
        controller = _MeshController([0.0, 3.25, 2.0], None)
        pipeline = _pipeline(controller, False)
        binding = pipeline.GetVBuffers()[0]
        binding.byteStride = 0
        attribute = pipeline.GetVertexInputs()[0]
        attribute.byteOffset = 4
        record = self.module._attribute_metadata(attribute, 0, [binding])
        with tempfile.TemporaryDirectory() as temporary:
            writer = self.module._ArtifactWriter(temporary)
            self.module._capture_attribute(
                controller, attribute, record, [1, 9, 20], types.SimpleNamespace(),
                {"api": "Vulkan", "instance": 0}, writer,
            )
            self.assertEqual("captured", record["canonical"]["status"])
            self.assertEqual(4, record["raw"]["sourceSpanByteLength"])
            path = Path(temporary) / record["canonical"]["path"]
            self.assertEqual((3.25, 3.25, 3.25), struct.unpack("<3f", path.read_bytes()))

    def test_indexed_draw_applies_base_vertex_but_not_vertex_offset_twice(self) -> None:
        logical, values = self._captured_position_values(indexed=True)
        self.assertEqual([6, 8], logical)
        self.assertEqual([6.0, 8.0], values)

    def test_nonindexed_draw_applies_vertex_offset_exactly_once(self) -> None:
        logical, values = self._captured_position_values(indexed=False)
        self.assertEqual([7, 8], logical)
        self.assertEqual([7.0, 8.0], values)

    def test_nonindexed_draw_ignores_a_stale_index_buffer_binding(self) -> None:
        controller = _MeshController(
            [float(value) for value in range(32)], [1, 3]
        )
        pipeline = _pipeline(controller, True)
        action = types.SimpleNamespace(
            numIndices=2,
            numVertices=2,
            numInstances=1,
            indexOffset=0,
            vertexOffset=7,
            instanceOffset=0,
            baseVertex=5,
            eventId=20,
            flags=0,
        )
        with tempfile.TemporaryDirectory() as temporary:
            writer = self.module._ArtifactWriter(temporary)
            mesh = self.module._mesh(
                controller,
                pipeline,
                action,
                {"instance": 0, "view": 0},
                _rd_module(),
                writer,
                True,
            )
            logical = (Path(temporary) / mesh["logicalVertexIndices"]["path"]).read_bytes()
        self.assertFalse(mesh["indexed"])
        self.assertEqual([7, 8], list(struct.unpack("<2I", logical)))
        self.assertNotIn(22, controller.buffer_reads)

    def test_indexed_draw_without_a_valid_stride_fails_closed(self) -> None:
        controller = _MeshController([0.0, 1.0, 2.0], [0, 1, 2])
        pipeline = _pipeline(controller, True)
        pipeline.GetIBuffer().byteStride = 0
        action = types.SimpleNamespace(
            numIndices=3,
            numVertices=3,
            numInstances=1,
            indexOffset=0,
            vertexOffset=0,
            instanceOffset=0,
            baseVertex=0,
            eventId=21,
            flags=INDEXED_FLAG,
        )
        with tempfile.TemporaryDirectory() as temporary:
            writer = self.module._ArtifactWriter(temporary)
            with self.assertRaisesRegex(RuntimeError, "index byte stride"):
                self.module._mesh(
                    controller,
                    pipeline,
                    action,
                    {"instance": 0, "view": 0},
                    _rd_module(),
                    writer,
                    True,
                )

    def _captured_instance_values(self, divisor: int) -> list[float]:
        controller = _MeshController(
            [float(value) for value in range(16)], None
        )
        pipeline = _pipeline(controller, False)
        attribute = pipeline.GetVertexInputs()[0]
        attribute.perInstance = True
        attribute.instanceRate = divisor
        action = types.SimpleNamespace(
            numIndices=2,
            numVertices=2,
            numInstances=8,
            indexOffset=0,
            vertexOffset=0,
            instanceOffset=5,
            baseVertex=0,
            eventId=22,
            flags=0,
        )
        with tempfile.TemporaryDirectory() as temporary:
            writer = self.module._ArtifactWriter(temporary)
            mesh = self.module._mesh(
                controller,
                pipeline,
                action,
                {"instance": 3, "view": 0},
                _rd_module(),
                writer,
                True,
            )
            canonical = mesh["attributes"][0]["canonical"]
            payload = (Path(temporary) / canonical["path"]).read_bytes()
        return list(struct.unpack("<%df" % (len(payload) // 4), payload))

    def test_instance_divisor_zero_selects_first_instance_constant(self) -> None:
        self.assertEqual([5.0, 5.0], self._captured_instance_values(0))

    def test_instance_divisor_advances_after_first_instance(self) -> None:
        self.assertEqual([6.0, 6.0], self._captured_instance_values(2))

    def test_msaa_dds_maps_all_samples_to_array_slices(self) -> None:
        class TextureSave:
            def __init__(self) -> None:
                self.slice = types.SimpleNamespace(sliceIndex=0)
                self.sample = types.SimpleNamespace(mapToArray=False, sampleIndex=0xFFFFFFFF)

        resource_id = _ResourceId(91)
        texture_description = types.SimpleNamespace(resourceId=resource_id)
        observed = {}

        class Controller:
            def GetTextures(self):
                return [texture_description]

            def SaveTexture(self, save, path):
                observed["mapToArray"] = save.sample.mapToArray
                observed["sampleIndex"] = save.sample.sampleIndex
                Path(path).write_bytes(b"DDS payload")

        rd_module = types.SimpleNamespace(
            FileType=types.SimpleNamespace(DDS="DDS"),
            TextureSave=TextureSave,
            CompType=types.SimpleNamespace(Typeless="Typeless"),
        )
        texture = {
            "resourceId": 91,
            "resource": {"sampleCount": 4},
        }
        with tempfile.TemporaryDirectory() as temporary:
            writer = self.module._ArtifactWriter(temporary)
            self.module._save_texture_dds(
                Controller(), rd_module, texture, writer
            )
        self.assertTrue(observed["mapToArray"])
        self.assertEqual(0, observed["sampleIndex"])
        self.assertEqual("exact", texture["samplePolicy"]["status"])
        self.assertEqual("all-samples-as-array", texture["samplePolicy"]["mode"])

    def test_cbuffer_physical_identity_binds_stage_binding_and_offset(self) -> None:
        block = {
            "bind": 2,
            "space": 1,
            "bufferId": 73,
            "byteOffset": 256,
        }
        identity, physical_id = self.module._constant_buffer_physical_identity(
            "vertex", 0, block
        )
        _, other_stage = self.module._constant_buffer_physical_identity(
            "fragment", 0, block
        )
        _, other_offset = self.module._constant_buffer_physical_identity(
            "vertex", 0, dict(block, byteOffset=512)
        )
        self.assertEqual("vertex", identity["stage"])
        self.assertEqual(2, identity["bind"])
        self.assertEqual(256, identity["byteOffset"])
        self.assertNotEqual(physical_id, other_stage)
        self.assertNotEqual(physical_id, other_offset)


class ResourceExportBridgeRegistrationTests(unittest.TestCase):
    def test_methods_are_registered_as_distinct_capture_bound_routes(self) -> None:
        fake_renderdoc = types.ModuleType("renderdoc")
        previous = sys.modules.get("renderdoc")
        sys.modules["renderdoc"] = fake_renderdoc
        try:
            spec = importlib.util.spec_from_file_location(
                "renderdoc_mcp_bridge_resource_export_contract", BRIDGE_PATH
            )
            assert spec is not None and spec.loader is not None
            bridge = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bridge)
        finally:
            if previous is None:
                sys.modules.pop("renderdoc", None)
            else:
                sys.modules["renderdoc"] = previous

        self.assertIs(
            bridge.METHODS["inspect_resource_export"], bridge._inspect_resource_export
        )
        self.assertIs(
            bridge.METHODS["capture_resource_export_bundle"],
            bridge._capture_resource_export_bundle,
        )
        self.assertIsNot(
            bridge.METHODS["capture_resource_export_bundle"],
            bridge.METHODS["export_to_unity"],
        )
        ping = bridge._ping({})
        self.assertIn("resource-export-capture-v1", ping["capabilities"])
        self.assertEqual(
            "replaylab-resource-export-bundle/v1",
            ping["schemas"]["resourceExportBundle"],
        )

    def test_inspection_restores_the_original_event_and_records_receipt(self) -> None:
        fake_renderdoc = types.ModuleType("renderdoc")
        previous = sys.modules.get("renderdoc")
        sys.modules["renderdoc"] = fake_renderdoc
        try:
            spec = importlib.util.spec_from_file_location(
                "renderdoc_mcp_bridge_resource_export_restore", BRIDGE_PATH
            )
            assert spec is not None and spec.loader is not None
            bridge = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(bridge)
        finally:
            if previous is None:
                sys.modules.pop("renderdoc", None)
            else:
                sys.modules["renderdoc"] = previous

        events = []
        controller = object()
        bridge._ctx = types.SimpleNamespace(
            IsCaptureLoaded=lambda: True,
            CurEvent=lambda: 17,
        )
        bridge._capture_session_id = "a" * 32
        bridge._replay = lambda callback: callback(controller)
        bridge._set_frame_event = lambda unused_ctrl, event_id, unused_force: events.append(
            event_id
        )
        bridge._available_shader_stages = lambda *unused_args: []
        bridge._resource_export_shader_evidence = lambda *unused_args: {}
        bridge._resource_export_protocol.validate_inspect_request = (
            lambda unused_params, unused_capture, unused_session: {"eventId": 91}
        )
        bridge._resource_export_protocol.inspect_controller = (
            lambda unused_ctrl, unused_rd, unused_request, unused_stages: {
                "boundShaderStages": []
            }
        )

        result = bridge._inspect_resource_export(
            {"expected_capture_session_id": "a" * 32}
        )

        self.assertEqual(events, [91, 17])
        self.assertEqual(
            result["replayRestoration"],
            {"originalEventId": 17, "restoredEventId": 17, "restored": True},
        )


if __name__ == "__main__":
    unittest.main()
