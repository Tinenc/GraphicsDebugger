from __future__ import annotations

import hashlib
import importlib.util
import math
import struct
from pathlib import Path
import sys
import tempfile
import types
import unittest


REPOSITORY = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"


def load_bridge():
    fake_renderdoc = types.ModuleType("renderdoc")
    previous = sys.modules.get("renderdoc")
    sys.modules["renderdoc"] = fake_renderdoc
    try:
        spec = importlib.util.spec_from_file_location(
            "renderdoc_mcp_bridge_event_texture_evidence_contract", BRIDGE_PATH
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("renderdoc", None)
        else:
            sys.modules["renderdoc"] = previous


class _TextureSave:
    def __init__(self) -> None:
        self.resourceId = None
        self.typeCast = None
        self.destType = None
        self.mip = -1
        self.slice = types.SimpleNamespace(sliceIndex=-1)
        self.sample = types.SimpleNamespace(mapToArray=True, sampleIndex=-1)
        self.comp = types.SimpleNamespace(blackPoint=0.0, whitePoint=1.0)
        self.channelExtract = -1


class _Controller:
    def __init__(self, current, *, texture_byte_size=1024 * 1024) -> None:
        self.current = current
        self.events = []
        self.saved = []
        self.texture_byte_size = texture_byte_size
        self.color = types.SimpleNamespace(
            resource=101,
            firstMip=1,
            numMips=1,
            firstSlice=2,
            numSlices=1,
        )
        self.depth = types.SimpleNamespace(
            resource=202,
            firstMip=0,
            numMips=1,
            firstSlice=0,
            numSlices=1,
        )

    def SetFrameEvent(self, event_id, force):
        self.events.append((event_id, force))
        self.current[0] = event_id

    def GetRootActions(self):
        return [types.SimpleNamespace(eventId=41, children=[])]

    def GetPipelineState(self):
        return types.SimpleNamespace(
            GetOutputTargets=lambda: [self.color],
            GetDepthTarget=lambda: self.depth,
        )

    def GetTextures(self):
        return [
            types.SimpleNamespace(
                resourceId=101,
                width=128,
                height=64,
                depth=1,
                arraysize=4,
                mips=4,
                msSamp=2,
                byteSize=self.texture_byte_size,
                format=types.SimpleNamespace(name="RGBA16_FLOAT", compCount=4),
            ),
            types.SimpleNamespace(
                resourceId=202,
                width=128,
                height=64,
                depth=1,
                arraysize=1,
                mips=1,
                msSamp=1,
                byteSize=self.texture_byte_size,
                format=types.SimpleNamespace(name="D32_FLOAT", compCount=1),
            ),
        ]

    def GetMinMax(self, resource, subresource, cast):
        return (
            types.SimpleNamespace(floatValue=[0.2, 0.1, 0.3, 0.4]),
            types.SimpleNamespace(floatValue=[0.8, 0.9, 0.7, 1.0]),
        )

    def SaveTexture(self, save, path):
        self.saved.append((save, path))
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"evidence")
        return types.SimpleNamespace(code="succeeded")


class EventTextureEvidenceBridgeTests(unittest.TestCase):
    def _fixture(self, *, texture_byte_size=1024 * 1024):
        bridge = load_bridge()
        bridge.rd.TextureSave = _TextureSave
        bridge.rd.FileType = types.SimpleNamespace(PNG="png")
        bridge.rd.AlphaMapping = types.SimpleNamespace(Discard="discard")
        bridge.rd.CompType = types.SimpleNamespace(Typeless="typeless")
        bridge.rd.ResultCode = types.SimpleNamespace(Succeeded="succeeded")
        bridge.rd.Subresource = lambda mip, array_slice, sample: (
            mip,
            array_slice,
            sample,
        )
        current = [7]
        controller = _Controller(current, texture_byte_size=texture_byte_size)
        bridge._capture_session_id = "a" * 32
        bridge._ctx = types.SimpleNamespace(CurEvent=lambda: current[0])
        bridge._replay = lambda callback: callback(controller)
        # Replay and Qt image decoding are native acceptance concerns. Keep
        # this protocol fixture's deliberately synthetic PNG opaque at export.
        bridge._reduce_saved_image = lambda path, crop, maximum, opaque_rgb=False, flip_y=False: {
            "opaqueRgbProcessed": opaque_rgb} if opaque_rgb else None
        return bridge, controller

    def _params(self, request_dir: Path, **overrides):
        value = {
            "event_id": 41,
            "attachment_kind": "rtv",
            "attachment_index": 0,
            "channel": "r",
            "mip": 1,
            "slice": 2,
            "sample": 1,
            "range_mode": "subresource-minmax",
            "black_point": None,
            "white_point": None,
            "request_id": "b" * 32,
            "artifact_dir": str(request_dir),
            "expected_capture_session_id": "a" * 32,
        }
        value.update(overrides)
        return value

    def test_composite_is_registered_separately_from_legacy_save(self) -> None:
        bridge, unused_controller = self._fixture()
        self.assertIs(
            bridge.METHODS["capture_event_texture_evidence"],
            bridge._capture_event_texture_evidence,
        )
        self.assertIsNot(
            bridge.METHODS["capture_event_texture_evidence"],
            bridge.METHODS["save_texture"],
        )

    def test_diagram_export_retains_alpha_and_rgb_under_transparency(self):
        bridge, controller = self._fixture()
        bridge.rd.AlphaMapping.Preserve = "preserve"
        seen = []
        bridge._reduce_saved_image = lambda path, crop, maximum, **options: seen.append(options)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / ("eid-41-" + "b" * 32)
            folder.mkdir()
            result = bridge._capture_event_texture_evidence(
                self._params(folder, channel="rgba", preserve_alpha=True, max_dimension=128))
        self.assertEqual(controller.saved[0][0].alpha, "preserve")
        self.assertEqual(result["artifact"]["display"]["alphaMode"], "preserved")
        self.assertTrue(seen[0]["preserve_channels"])
        self.assertFalse(seen[0]["opaque_rgb"])

    def test_orientation_is_optional_boolean_and_part_of_export_receipt(self):
        for orientation in (None, False, True):
            bridge, controller = self._fixture()
            seen = []
            bridge._reduce_saved_image = lambda path, crop, maximum, **options: seen.append(options) or options
            with tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary) / ("eid-41-" + "b" * 32)
                folder.mkdir()
                params = self._params(folder)
                if orientation is not None:
                    params["flip_y"] = orientation
                result = bridge._capture_event_texture_evidence(params)
            self.assertIs(result["artifact"]["display"]["flipY"], orientation is True)
            self.assertIs(seen[0]["flip_y"], orientation is True)
            self.assertEqual(controller.events, [(41, True), (7, True)])
        for invalid in (0, 1, "true", None):
            bridge, controller = self._fixture()
            with self.assertRaisesRegex(ValueError, "boolean"):
                bridge._capture_event_texture_evidence({"flip_y": invalid})
            self.assertFalse(controller.events)

    def test_flip_is_vertical_after_native_crop_and_hashes_the_pixels_actually_saved(self):
        from PIL import Image
        from unittest.mock import patch
        bridge = load_bridge()

        class QImage:
            Format_RGB32 = "rgb"
            def __init__(self, source):
                self.image = source if isinstance(source, Image.Image) else Image.open(source).copy()
            def isNull(self): return False
            def width(self): return self.image.width
            def height(self): return self.image.height
            def convertToFormat(self, unused): return QImage(self.image.convert("RGB"))
            def copy(self, left, top, width, height):
                return QImage(self.image.crop((left, top, left + width, top + height)))
            def mirrored(self, horizontal, vertical):
                assert horizontal is False and vertical is True
                return QImage(self.image.transpose(Image.Transpose.FLIP_TOP_BOTTOM))
            def save(self, path, format):
                self.image.save(path, format)
                return True

        fake_qt = types.ModuleType("PySide2")
        fake_qt.QtGui = types.SimpleNamespace(QImage=QImage)
        pixels = [(10 + x, 20 + y, 0, 0) for y in range(4) for x in range(3)]
        with tempfile.TemporaryDirectory() as temporary, patch.dict(sys.modules, {"PySide2": fake_qt}):
            path = Path(temporary) / "asymmetric.png"
            source = Image.new("RGBA", (3, 4)); source.putdata(pixels); source.save(path)
            original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            crop = {"x": 1, "y": 1, "width": 2, "height": 3}
            result = bridge._reduce_saved_image(str(path), crop, None, opaque_rgb=True, flip_y=True)
            actual = Image.open(path)
            self.assertEqual(actual.size, (2, 3))
            self.assertEqual([actual.getpixel((x, y)) for y in range(3) for x in range(2)], [
                (11, 23, 0), (12, 23, 0), (11, 22, 0), (12, 22, 0), (11, 21, 0), (12, 21, 0)])
            self.assertTrue(result["flipYProcessed"])
            size, digest = bridge._event_texture_evidence_hash(str(path), 1024 * 1024)
            self.assertNotEqual(digest, original_hash)
            self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(size, path.stat().st_size)
            before = path.read_bytes()
            self.assertIsNone(bridge._reduce_saved_image(str(path), None, None))
            self.assertEqual(path.read_bytes(), before)

    def _float_bits_fixture(self):
        bridge, controller = self._fixture()
        bridge.rd.CompType.UInt = "uint"
        textures = controller.GetTextures()
        textures[0].msSamp = 1
        textures[0].dimension = 2
        textures[0].format = types.SimpleNamespace(
            name="R32_UINT", compCount=1, compByteWidth=4, compType="uint", Special=lambda: False)
        controller.GetTextures = lambda: textures
        raw = struct.pack("<2048f", *([0.0] * 2000 + [0.5] * 47 + [1.0]))
        reads, exports = [], []
        def read(resource, subresource):
            reads.append((controller.current[0], resource, subresource))
            return raw
        controller.GetTextureData = read
        controller.GetMinMax = lambda *args: self.fail("GPU integer min/max must not interpret float bits")
        def export(path, gray, width, height):
            exports.append((bytes(gray), width, height))
            Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"decoded-gray")
        bridge._save_decoded_grayscale_evidence = export
        return bridge, controller, raw, reads, exports

    def test_float_bits_reads_complete_exact_subresource_and_keeps_export_contract(self):
        bridge, controller, raw, reads, exports = self._float_bits_fixture()
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / ("eid-41-" + "b" * 32); folder.mkdir()
            result = bridge._capture_event_texture_evidence(self._params(
                folder, sample=0, range_mode="visible-distribution", value_decode="float32-bits"))
        self.assertEqual(reads, [(41, 101, (1, 2, 0))])
        self.assertEqual(controller.events, [(41, True), (7, True)])
        self.assertFalse(controller.saved)
        self.assertEqual(len(exports[0][0]), 2048)
        self.assertEqual(exports[0][1:], (64, 32))
        self.assertGreater(max(exports[0][0][2000:]), 0)
        display = result["artifact"]["display"]
        self.assertEqual(display["valueDecode"], "float32-bits")
        self.assertEqual(display["decoding"]["sourceBytes"], len(raw))
        self.assertEqual(display["decoding"]["sourceSha256"], hashlib.sha256(raw).hexdigest())
        self.assertTrue(display["decoding"]["fullSubresource"])
        self.assertEqual(display["alphaMode"], "opaque-rgb")

    def test_float_bits_rejects_truncated_data_and_restores_event(self):
        bridge, controller, raw, reads, exports = self._float_bits_fixture()
        controller.GetTextureData = lambda *args: raw[:7500]
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / ("eid-41-" + "b" * 32); folder.mkdir()
            with self.assertRaisesRegex(ValueError, "exactly"):
                bridge._capture_event_texture_evidence(self._params(
                    folder, sample=0, value_decode="float32-bits"))
            self.assertFalse(list(folder.iterdir()))
        self.assertEqual(controller.events, [(41, True), (7, True)])

    def test_float_bits_refuses_unknown_or_incompatible_physical_format(self):
        for change in ("compByteWidth", "msSamp", "dimension", "compCount"):
            bridge, controller, raw, reads, exports = self._float_bits_fixture()
            texture = controller.GetTextures()[0]
            setattr(texture.format if change.startswith("comp") else texture, change, 3)
            if change == "dimension":
                # Keep the depth slice valid so this case tests the decode
                # format restriction rather than an earlier view-bound error.
                controller.color.firstSlice = 0
            with tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary) / ("eid-41-" + "b" * 32); folder.mkdir()
                with self.assertRaisesRegex(ValueError, "R32_UINT"):
                    bridge._capture_event_texture_evidence(self._params(
                        folder, sample=0, slice=0 if change == "dimension" else 2,
                        value_decode="float32-bits"))
            self.assertFalse(reads)
            self.assertEqual(controller.events, [(41, True), (7, True)])

    def test_float_bits_all_nonfinite_minmax_does_not_invent_range(self):
        bridge, controller, raw, reads, exports = self._float_bits_fixture()
        controller.GetTextureData = lambda *args: struct.pack("<2048f", *([float("nan")] * 2048))
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / ("eid-41-" + "b" * 32); folder.mkdir()
            with self.assertRaisesRegex(ValueError, "No finite decoded texels"):
                bridge._capture_event_texture_evidence(self._params(
                    folder, sample=0, value_decode="float32-bits"))
        self.assertEqual(controller.events, [(41, True), (7, True)])

    def test_rgba_preview_discards_alpha_and_large_atlas_requires_small_preview(self):
        bridge, controller = self._fixture(texture_byte_size=128 * 1024 * 1024)
        textures = controller.GetTextures()
        textures[0].width, textures[0].height = 32768, 4096  # mip 1 = 16384 x 2048
        controller.GetTextures = lambda: textures
        bridge._reduce_saved_image = lambda path, crop, maximum, opaque_rgb=False, flip_y=False: {"scaledTo": {"width": 2048, "height": 256}, "opaqueRgbProcessed": opaque_rgb}
        with tempfile.TemporaryDirectory() as directory:
            request = Path(directory) / ("eid-41-" + "b" * 32)
            request.mkdir()
            result = bridge._capture_event_texture_evidence(self._params(request, channel="rgba", max_dimension=2048))
            self.assertEqual(result["artifact"]["status"], "saved")
            self.assertEqual(controller.saved[0][0].alpha, "discard")
            self.assertEqual(result["artifact"]["display"]["alphaMode"], "opaque-rgb")
            request = Path(directory) / ("eid-41-" + "c" * 32)
            request.mkdir()
            result = bridge._capture_event_texture_evidence(self._params(request, channel="rgba", request_id="c" * 32))
            self.assertEqual(result["artifact"]["skipReason"], "subresource-pixel-budget-exceeded")

    def test_compute_uav_exports_active_binding_and_restores_selection(self) -> None:
        bridge, controller = self._fixture()
        bridge.rd.ShaderStage = types.SimpleNamespace(Compute="compute-stage")
        controller.GetRootActions = lambda: [types.SimpleNamespace(eventId=41, children=[], flags="Dispatch")]
        seen = []
        def resources(stage, only_used):
            seen.append((stage, only_used))
            return [types.SimpleNamespace(descriptor=controller.color)]
        controller.GetPipelineState = lambda: types.SimpleNamespace(GetReadWriteResources=resources)
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / ("eid-41-%s" % ("b" * 32))
            folder.mkdir()
            result = bridge._capture_event_texture_evidence(self._params(
                folder, attachment_kind="uav", stage="compute"))
            self.assertEqual(result["artifact"]["attachment"]["stage"], "compute")
            self.assertEqual(seen, [("compute-stage", True)])
            self.assertEqual(controller.events, [(41, True), (7, True)])

    def test_compute_cannot_export_inactive_fragment_input(self) -> None:
        bridge, controller = self._fixture()
        bridge.rd.ShaderStage = types.SimpleNamespace(Fragment="fragment-stage")
        controller.GetRootActions = lambda: [types.SimpleNamespace(eventId=41, children=[], flags="Dispatch")]
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / ("eid-41-%s" % ("b" * 32))
            folder.mkdir()
            with self.assertRaisesRegex(ValueError, "does not execute"):
                bridge._capture_event_texture_evidence(self._params(
                    folder, attachment_kind="srv", stage="fragment"))
            self.assertFalse(controller.saved)
            self.assertEqual(controller.events, [(41, True), (7, True)])

    def test_volume_exports_exact_selected_depth_slice_with_native_range_provenance(self):
        for kind, raw_count in (("uav", 0), ("srv", 0), ("srv", 1)):
            with self.subTest(kind=kind, raw_count=raw_count):
                bridge, controller = self._fixture()
                bridge.rd.ShaderStage = types.SimpleNamespace(Compute="compute-stage")
                controller.GetRootActions = lambda: [types.SimpleNamespace(eventId=41, children=[], flags="Dispatch")]
                textures = controller.GetTextures()
                textures[0].dimension, textures[0].depth, textures[0].arraysize = 3, 33, 1
                controller.GetTextures = lambda: textures
                controller.color.firstSlice, controller.color.numSlices = 0, raw_count
                controller.color.textureType = "Texture3D"
                resources = lambda stage, only_used: [types.SimpleNamespace(descriptor=controller.color)]
                controller.GetPipelineState = lambda: types.SimpleNamespace(
                    GetReadWriteResources=resources, GetReadOnlyResources=resources)
                with tempfile.TemporaryDirectory() as temporary:
                    folder = Path(temporary) / ("eid-41-" + "b" * 32)
                    folder.mkdir()
                    result = bridge._capture_event_texture_evidence(self._params(
                        folder, attachment_kind=kind, stage="compute", slice=15))
                attachment = result["artifact"]["attachment"]
                self.assertEqual(attachment["descriptor"]["numSlices"], 16)
                self.assertEqual(attachment["nativeDescriptor"]["numSlices"], raw_count)
                self.assertEqual(attachment["mipDepth"], 16)
                self.assertEqual(attachment["resourceDepth"], 33)
                self.assertEqual(attachment["depthSlice"], 15)
                self.assertEqual(attachment["sliceKind"], "depth")
                self.assertEqual(controller.saved[0][0].slice.sliceIndex, 15)
                self.assertEqual(controller.events, [(41, True), (7, True)])

    def test_volume_export_rejects_depth_outside_selected_mip_and_restores_event(self):
        bridge, controller = self._fixture()
        textures = controller.GetTextures()
        textures[0].dimension, textures[0].depth, textures[0].arraysize = 3, 8, 1
        controller.GetTextures = lambda: textures
        controller.color.textureType = "Texture3D"
        controller.color.firstSlice, controller.color.numSlices = 0, 0
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / ("eid-41-" + "b" * 32)
            folder.mkdir()
            with self.assertRaisesRegex(RuntimeError, "requested slice"):
                bridge._capture_event_texture_evidence(self._params(folder, slice=4))
        self.assertFalse(controller.saved)
        self.assertEqual(controller.events, [(41, True), (7, True)])

    def test_integer_output_range_uses_integer_pixel_values_and_view_cast(self) -> None:
        for component, field, low, high, typeless in (
                ("uint", "uintValue", 20, 1000, False),
                ("sint", "intValue", -250, 500, True)):
            with self.subTest(component=component):
                bridge, controller = self._fixture()
                bridge.rd.CompType = types.SimpleNamespace(Typeless="typeless", UInt="uint", SInt="sint")
                textures = controller.GetTextures()
                textures[0].format.compType = "typeless" if typeless else component
                controller.color.format = types.SimpleNamespace(compType=component)
                controller.GetTextures = lambda: textures
                seen = []
                def minmax(resource, subresource, cast):
                    seen.append(cast)
                    return (types.SimpleNamespace(**{field: [low]*4, "floatValue": [float('nan')]*4}),
                            types.SimpleNamespace(**{field: [high]*4, "floatValue": [float('nan')]*4}))
                controller.GetMinMax = minmax
                with tempfile.TemporaryDirectory() as temporary:
                    folder = Path(temporary) / ("eid-41-%s" % ("b" * 32)); folder.mkdir()
                    result = bridge._capture_event_texture_evidence(self._params(folder))
                    self.assertEqual(result["artifact"]["display"]["blackPoint"], low)
                    self.assertEqual(result["artifact"]["display"]["whitePoint"], high)
                    expected = component if typeless else "typeless"
                    self.assertEqual(seen, [expected])
                    self.assertEqual(controller.saved[0][0].typeCast, expected)

    def test_saves_exact_bound_channel_range_and_restores_event(self) -> None:
        bridge, controller = self._fixture()
        with tempfile.TemporaryDirectory() as temporary:
            request_dir = Path(temporary) / ("eid-41-%s" % ("b" * 32))
            request_dir.mkdir()
            result = bridge._capture_event_texture_evidence(
                self._params(request_dir)
            )

            self.assertEqual(result["schema"], "replaylab-event-texture-evidence/v1")
            self.assertEqual(result["artifact"]["status"], "saved")
            self.assertEqual(controller.events, [(41, True), (7, True)])
            save, temporary_path = controller.saved[0]
            self.assertEqual(save.resourceId, 101)
            self.assertEqual(save.mip, 1)
            self.assertEqual(save.slice.sliceIndex, 2)
            self.assertEqual(save.sample.sampleIndex, 1)
            self.assertEqual(save.channelExtract, 0)
            self.assertEqual(save.comp.blackPoint, 0.2)
            self.assertEqual(save.comp.whitePoint, 0.8)
            self.assertFalse(Path(temporary_path).exists())
            output = result["artifact"]["output"]
            output_path = Path(output["path"])
            self.assertEqual(output_path.parent, request_dir)
            self.assertEqual(output["mimeType"], "image/png")
            self.assertEqual(
                output["sha256"], hashlib.sha256(output_path.read_bytes()).hexdigest()
            )

    def test_depth_is_raw_component_zero_with_explicit_unit_range(self) -> None:
        bridge, controller = self._fixture()
        with tempfile.TemporaryDirectory() as temporary:
            request_dir = Path(temporary) / ("eid-41-%s" % ("b" * 32))
            request_dir.mkdir()
            result = bridge._capture_event_texture_evidence(
                self._params(
                    request_dir,
                    attachment_kind="dsv",
                    channel="depth",
                    mip=0,
                    slice=0,
                    sample=0,
                    range_mode="explicit",
                    black_point=0.25,
                    white_point=0.75,
                )
            )

            save = controller.saved[0][0]
            self.assertEqual(save.resourceId, 202)
            self.assertEqual(save.channelExtract, 0)
            self.assertEqual(save.comp.blackPoint, 0.25)
            self.assertEqual(save.comp.whitePoint, 0.75)
            self.assertEqual(
                result["artifact"]["display"]["depthInterpretation"],
                "raw-resource-component-0",
            )

    def test_unbound_slot_fails_closed_and_still_restores_event(self) -> None:
        bridge, controller = self._fixture()
        with tempfile.TemporaryDirectory() as temporary:
            request_dir = Path(temporary) / ("eid-41-%s" % ("b" * 32))
            request_dir.mkdir()
            with self.assertRaisesRegex(RuntimeError, "binding index is unavailable"):
                bridge._capture_event_texture_evidence(
                    self._params(request_dir, attachment_index=1)
                )
            self.assertEqual(controller.events, [(41, True), (7, True)])
            self.assertEqual(list(request_dir.iterdir()), [])

    def test_source_budget_returns_skip_receipt_without_saving(self) -> None:
        bridge, controller = self._fixture(
            texture_byte_size=256 * 1024 * 1024 + 1
        )
        with tempfile.TemporaryDirectory() as temporary:
            request_dir = Path(temporary) / ("eid-41-%s" % ("b" * 32))
            request_dir.mkdir()
            result = bridge._capture_event_texture_evidence(
                self._params(request_dir, range_mode="native")
            )

            self.assertEqual(result["artifact"]["status"], "skipped")
            self.assertEqual(
                result["artifact"]["skipReason"],
                "source-size-budget-exceeded",
            )
            self.assertEqual(controller.saved, [])
            self.assertEqual(controller.events, [(41, True), (7, True)])
            self.assertEqual(list(request_dir.iterdir()), [])

    def test_flat_subresource_range_returns_a_verified_skip(self) -> None:
        bridge, controller = self._fixture()
        controller.GetMinMax = lambda resource, subresource, cast: (
            types.SimpleNamespace(floatValue=[0.5, 0.5, 0.5, 0.5]),
            types.SimpleNamespace(floatValue=[0.5, 0.5, 0.5, 0.5]),
        )
        with tempfile.TemporaryDirectory() as temporary:
            request_dir = Path(temporary) / ("eid-41-%s" % ("b" * 32))
            request_dir.mkdir()
            result = bridge._capture_event_texture_evidence(
                self._params(request_dir)
            )

            self.assertEqual(result["artifact"]["status"], "skipped")
            self.assertEqual(
                result["artifact"]["skipReason"], "subresource-range-flat"
            )
            self.assertEqual(result["artifact"]["display"]["blackPoint"], 0.5)
            self.assertEqual(result["artifact"]["display"]["whitePoint"], 0.5)
            self.assertEqual(controller.saved, [])
            self.assertEqual(controller.events, [(41, True), (7, True)])

    def test_auto_exposure_uses_whole_rgb_distribution_and_preserves_capture(self):
        bridge, controller = self._fixture()
        pixels = [(0.0, 0.0, 0.0, 1e20)] * 100
        pixels += [(0.01 + index / 100000.0,) * 3 + (1e20,) for index in range(1000)]
        pixels += [(65504.0,) * 3 + (1e20,)]
        controller.GetMinMax = lambda *args: (
            types.SimpleNamespace(floatValue=[0, 0, 0, 1e20]),
            types.SimpleNamespace(floatValue=[65504, 65504, 65504, 1e20]))
        queries = []
        def histogram(resource, subresource, cast, lower, upper, channels):
            queries.append((resource, subresource, channels))
            counts = [0] * 256
            for pixel in pixels:
                for index, value in enumerate(pixel):
                    if channels[index] and math.isfinite(value) and lower <= value <= upper:
                        bucket = min(255, int((value - lower) / (upper - lower) * 256))
                        counts[bucket] += 1
            return counts
        controller.GetHistogram = histogram
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / ("eid-41-" + "b" * 32)
            folder.mkdir()
            result = bridge._capture_event_texture_evidence(self._params(
                folder, channel="rgba", range_mode="visible-distribution"))
        display = result["artifact"]["display"]
        self.assertEqual(result["artifact"]["status"], "saved")
        self.assertEqual(display["blackPoint"], 0)
        self.assertTrue(0.019 < display["whitePoint"] < 0.021)
        self.assertEqual(display["subresourceMax"], [65504, 65504, 65504, 1e20])
        self.assertEqual(display["distribution"]["sampleCount"], 3003)
        self.assertTrue(all(query == (101, (1, 2, 1), [True, True, True, False]) for query in queries))
        self.assertEqual(controller.events, [(41, True), (7, True)])

    def test_present_uses_action_backbuffer_not_the_bound_ui_texture(self):
        bridge, controller = self._fixture()
        controller.GetRootActions = lambda: [types.SimpleNamespace(eventId=41, children=[], flags=0x100, copyDestination=101)]
        controller.GetPipelineState = lambda: (_ for _ in ()).throw(AssertionError("Present must not read stale PS bindings"))
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / ("eid-41-" + "b" * 32)
            folder.mkdir()
            result = bridge._capture_event_texture_evidence(self._params(
                folder, attachment_kind="present", channel="rgba", mip=0, slice=0, sample=0, range_mode="native"))
        self.assertTrue(result["artifact"]["attachment"]["presentedBackbuffer"])
        self.assertEqual(controller.saved[0][0].resourceId, 101)
        self.assertEqual(controller.events, [(41, True), (7, True)])

    def test_present_rejects_non_present_event_instead_of_using_current_ps_output(self):
        bridge, controller = self._fixture()
        controller.GetRootActions = lambda: [types.SimpleNamespace(eventId=41, children=[], flags=1, copyDestination=101)]
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / ("eid-41-" + "b" * 32)
            folder.mkdir()
            with self.assertRaisesRegex(ValueError, "exact Present"):
                bridge._capture_event_texture_evidence(self._params(
                    folder, attachment_kind="present", channel="rgba", mip=0, slice=0, sample=0, range_mode="native"))
        self.assertFalse(controller.saved)
        self.assertEqual(controller.events, [(41, True), (7, True)])

    def test_nonempty_host_directory_is_rejected_before_replay(self) -> None:
        bridge, controller = self._fixture()
        with tempfile.TemporaryDirectory() as temporary:
            request_dir = Path(temporary) / ("eid-41-%s" % ("b" * 32))
            request_dir.mkdir()
            (request_dir / "unexpected.txt").write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "new empty directory"):
                bridge._capture_event_texture_evidence(self._params(request_dir))
            self.assertEqual(controller.events, [])

    def test_depth_range_outside_unit_interval_is_rejected(self) -> None:
        bridge, controller = self._fixture()
        with tempfile.TemporaryDirectory() as temporary:
            request_dir = Path(temporary) / ("eid-41-%s" % ("b" * 32))
            request_dir.mkdir()
            with self.assertRaisesRegex(ValueError, r"within \[0, 1\]"):
                bridge._capture_event_texture_evidence(
                    self._params(
                        request_dir,
                        attachment_kind="dsv",
                        channel="depth",
                        mip=0,
                        slice=0,
                        sample=0,
                        range_mode="explicit",
                        black_point=-0.1,
                        white_point=1.0,
                    )
                )
            self.assertEqual(controller.events, [])


if __name__ == "__main__":
    unittest.main()
