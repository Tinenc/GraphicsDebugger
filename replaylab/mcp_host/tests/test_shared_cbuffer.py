"""CPU contracts for native Material -> persistent, exact field-range transport."""

import ast
import base64
import copy
import hashlib
import importlib.util
from pathlib import Path
import struct
import types
import unittest
from unittest import mock

from test_native_material_panel import _Bridge, _selection, panel
import test_cbuffer_bridge as cbuffer_fixture
from test_cbuffer_bridge import CAPTURE_ID, CAPTURE_SESSION_ID, _value


MODULE_PATH = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "shared_cbuffer.py"
_spec = importlib.util.spec_from_file_location("shared_cbuffer_tested", MODULE_PATH)
shared = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(shared)


class _Controller:
    def __init__(self):
        self.generation = 0
        self.resource = object()
        self.sets = []
        self.clears = []
        self.fail_set = False
        self.fail_clear_at = 0
        self.configured = {}

    def GetSharedBufferPatchGeneration(self):
        return self.generation

    def GetBuffers(self):
        return [types.SimpleNamespace(resourceId=self.resource, length=4096)]

    def SetSharedBufferPatch(self, resource, offset, data):
        if resource is not self.resource:
            raise AssertionError("The setter must receive a real GetBuffers ResourceId")
        if self.fail_set:
            return "unsupported native usage"
        self.sets.append((offset, bytes(data)))
        self.configured[(offset, len(data))] = bytes(data)
        self.generation += 1
        return ""

    def ClearSharedBufferPatch(self, resource, offset, length):
        if resource is not self.resource:
            raise AssertionError("The clear must receive a real GetBuffers ResourceId")
        if self.fail_clear_at and len(self.clears) + 1 == self.fail_clear_at:
            return "injected clear failure"
        self.clears.append((offset, length))
        self.configured.pop((offset, length))
        self.generation += 1
        return ""


class SharedCBufferTests(unittest.TestCase):
    def setUp(self):
        self.ctrl = _Controller()
        self.edits = shared.SharedCBufferEdits(
            lambda value: 202 if value is self.ctrl.resource else 0)
        self.bridge = _Bridge()

        def snapshot(params):
            data = self.bridge.snapshot(params)
            data["bufferLength"] = 4096
            return self.edits.register(data, self.ctrl)

        self.panel = panel.MaterialPanelController(
            snapshot, lambda params: self.edits.apply(params, self.ctrl),
            lambda params: self.edits.rollback(params, self.ctrl), self.bridge.context)

    def expose(self, **changes):
        return self.panel.expose(_selection(**changes))

    def test_python_36_compatible(self):
        ast.parse(MODULE_PATH.read_text(encoding="utf-8"), feature_version=(3, 6))

    def test_global_window_budget_precedes_copy_and_decode_and_is_released(self):
        snapshot = self.bridge.snapshot({})
        size = shared.MAX_SHARED_CBUFFER_BYTES
        raw = b"\x00" * size
        snapshot.update(byteSize=size, validByteSize=size, bufferLength=size + 64,
                        rawBase64=base64.b64encode(raw).decode("ascii"),
                        rawSha256=hashlib.sha256(raw).hexdigest())
        self.edits.register(snapshot, self.ctrl)
        cost = size * 2 + ((size + 2) // 3) * 4
        self.assertEqual(self.edits._record_bytes, cost)
        self.assertLessEqual(cost, shared.MAX_SHARED_CBUFFER_RECORD_BYTES)
        second = dict(snapshot, snapshotId="f" * 32)
        with mock.patch.object(shared.copy, "deepcopy", side_effect=AssertionError("copied")), \
                mock.patch.object(shared.base64, "b64decode", side_effect=AssertionError("decoded")):
            with self.assertRaisesRegex(RuntimeError, "byte budget"):
                self.edits.register(second, self.ctrl)
        self.assertEqual(len(self.edits.records), 1)
        self.assertEqual(self.edits._record_bytes, cost)
        with self.assertRaisesRegex(ValueError, "already registered"):
            self.edits.register(snapshot, self.ctrl)
        self.edits.rollback({"snapshot_id": snapshot["snapshotId"], "override_id": "",
                             "expected_capture_id": snapshot["captureId"],
                             "expected_capture_session_id": snapshot["captureSessionId"]}, self.ctrl)
        self.assertEqual(self.edits._record_bytes, 0)
        self.edits.register(second, self.ctrl)
        self.edits.forget()
        self.assertEqual(self.edits._record_bytes, 0)

    def test_oversized_window_or_encoding_is_rejected_before_copy(self):
        snapshot = self.bridge.snapshot({})
        snapshot["bufferLength"] = 4096
        with mock.patch.object(shared.copy, "deepcopy", side_effect=AssertionError("copied")), \
                mock.patch.object(shared.base64, "b64decode", side_effect=AssertionError("decoded")):
            oversized = dict(snapshot, validByteSize=shared.MAX_SHARED_CBUFFER_BYTES + 1)
            with self.assertRaises(ValueError):
                self.edits.register(oversized, self.ctrl)
            with self.assertRaises(ValueError):
                self.edits.register(dict(snapshot, rawBase64=snapshot["rawBase64"] + "AAAA"), self.ctrl)
        self.assertEqual(self.edits.records, {})
        self.assertEqual(self.edits._record_bytes, 0)

    def test_native_write_is_field_only_and_remains_owned_across_eids(self):
        status = self.expose(fieldName="tint", fieldByteOffset=4, fieldComponents=3)
        pid = status["parameters"][0]["parameterId"]
        self.bridge.current_event = 78
        result = self.panel.update(status["sessionId"], pid, [0.25, 0.5, 0.75])
        self.assertEqual(self.ctrl.sets, [(68, struct.pack("<3f", 0.25, 0.5, 0.75))])
        self.assertEqual(result["generation"], 1)
        self.assertEqual(result["parameters"][0]["bindingByteOffset"], 64)
        self.assertEqual(result["parameters"][0]["byteOffset"], 4)
        self.assertEqual(result["parameters"][0]["byteSize"], 12)
        # Returning via a different shader/stage that shares this window keeps
        # the same ownership; origin EID is evidence, not mutation scope.
        self.bridge.current_event = 41
        same = self.expose(shaderResourceId=303, physicalSlot=5,
                           fieldName="tint", fieldByteOffset=4, fieldComponents=3)
        self.assertEqual(same["sessionId"], status["sessionId"])
        self.panel.rollback(status["sessionId"])
        self.assertEqual(self.ctrl.clears, [(68, 12)])
        self.assertEqual(self.ctrl.configured, {})

    def test_updates_change_one_field_without_resending_other_fields_or_padding(self):
        rough = self.expose()
        both = self.expose(fieldName="flags", fieldByteOffset=20,
                           fieldBaseType="UInt", fieldComponents=1)
        sid = rough["sessionId"]
        self.panel.update(sid, rough["parameters"][0]["parameterId"], 0.125)
        self.panel.update(sid, both["parameters"][1]["parameterId"], 42)
        self.panel.update(sid, rough["parameters"][0]["parameterId"], 0.5)
        self.assertEqual([offset for offset, _ in self.ctrl.sets], [64, 84, 64])
        self.assertEqual([len(data) for _, data in self.ctrl.sets], [4, 4, 4])
        self.assertEqual(self.ctrl.configured[(84, 4)], struct.pack("<I", 42))
        self.panel.rollback(sid)
        self.assertEqual(set(self.ctrl.clears), {(64, 4), (84, 4)})

    def test_native_rejection_preserves_revision_and_can_retry(self):
        status = self.expose()
        pid = status["parameters"][0]["parameterId"]
        self.ctrl.fail_set = True
        with self.assertRaisesRegex(RuntimeError, "unsupported native usage"):
            self.panel.update(status["sessionId"], pid, 1.0)
        self.assertEqual(self.panel.status(status["sessionId"])["revision"], 0)
        self.assertEqual(self.ctrl.generation, 0)
        self.ctrl.fail_set = False
        self.panel.update(status["sessionId"], pid, 1.0)
        self.assertEqual(self.ctrl.generation, 1)

    def test_external_generation_cannot_be_overwritten_or_cleared(self):
        status = self.expose()
        pid = status["parameters"][0]["parameterId"]
        self.panel.update(status["sessionId"], pid, 1.0)
        self.ctrl.generation += 1
        with self.assertRaisesRegex(RuntimeError, "outside this material editor"):
            self.panel.update(status["sessionId"], pid, 0.25)
        with self.assertRaisesRegex(RuntimeError, "expired material-panel session"):
            self.panel.rollback(status["sessionId"])
        self.assertEqual(len(self.ctrl.sets), 1)
        self.assertEqual(self.ctrl.clears, [])
        self.assertEqual(self.panel.list_status(), [])
        self.assertEqual(self.edits.records, {})
        reopened = self.expose()
        self.panel.update(reopened["sessionId"], reopened["parameters"][0]["parameterId"], 0.75)
        self.assertEqual(len(self.ctrl.sets), 2)

    def test_first_selection_after_native_edit_adopts_generation_and_resets_to_current_baseline(self):
        external = struct.pack("<f", 0.625)
        self.ctrl.SetSharedBufferPatch(self.ctrl.resource, 64, external)
        original_snapshot = self.bridge.snapshot

        def snapshot(params):
            value = original_snapshot(params)
            raw = external + base64.b64decode(value["rawBase64"])[4:]
            value["rawBase64"] = base64.b64encode(raw).decode("ascii")
            value["rawSha256"] = hashlib.sha256(raw).hexdigest()
            return value

        self.bridge.snapshot = snapshot
        status = self.expose()
        self.assertEqual(status["parameters"][0]["value"], [0.625])
        self.assertEqual(status["resetMode"], "snapshot-baseline")
        self.panel.update(status["sessionId"], status["parameters"][0]["parameterId"], 0.125)
        result = self.panel.rollback(status["sessionId"])
        self.assertTrue(result["snapshotReleased"])
        self.assertEqual(self.ctrl.configured[(64, 4)], external)
        self.assertEqual(self.ctrl.clears, [])

    def test_external_change_before_reset_never_replaces_other_editors_value(self):
        status = self.expose()
        self.panel.update(status["sessionId"], status["parameters"][0]["parameterId"], 0.25)
        external = struct.pack("<f", 0.875)
        self.ctrl.SetSharedBufferPatch(self.ctrl.resource, 64, external)
        with self.assertRaisesRegex(RuntimeError, "outside this material editor"):
            self.panel.rollback(status["sessionId"])
        self.assertEqual(self.ctrl.configured[(64, 4)], external)
        self.assertEqual(self.ctrl.clears, [])
        self.assertEqual(self.panel.list_status(), [])

    def test_clear_failure_keeps_remaining_ownership_and_retries_exact_ranges(self):
        first = self.expose()
        second = self.expose(fieldName="flags", fieldByteOffset=20, fieldBaseType="UInt")
        sid = first["sessionId"]
        self.panel.update(sid, first["parameters"][0]["parameterId"], 1.0)
        self.panel.update(sid, second["parameters"][1]["parameterId"], 9)
        self.ctrl.fail_clear_at = 2
        with self.assertRaisesRegex(RuntimeError, "injected clear failure"):
            self.panel.rollback(sid)
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            self.panel.update(sid, first["parameters"][0]["parameterId"], 0.5)
        self.ctrl.fail_clear_at = 0
        self.panel.rollback(sid)
        self.assertEqual(self.ctrl.clears, [(64, 4), (84, 4)])

    def test_untrusted_offset_or_unknown_field_never_reaches_native_setter(self):
        snapshot = self.bridge.snapshot({})
        snapshot["bufferLength"] = 4096
        del snapshot["fields"][0]["byteOffset"]
        with self.assertRaises(KeyError):
            self.edits.register(snapshot, self.ctrl)
        self.assertEqual(self.ctrl.sets, [])

    def test_another_snapshot_cannot_claim_an_overlapping_owned_range(self):
        status = self.expose()
        self.panel.update(status["sessionId"], status["parameters"][0]["parameterId"], 1.0)
        other = self.bridge.snapshot({})
        other["snapshotId"] = "f" * 32
        other["bufferLength"] = 4096
        self.edits.register(other, self.ctrl)
        leaf = other["fields"][0]
        raw = bytearray(base64.b64decode(other["rawBase64"]))
        packed = struct.pack("<f", 0.125)
        raw[:4] = packed
        params = {
            "snapshot_id": other["snapshotId"], "expected_capture_id": other["captureId"],
            "expected_capture_session_id": other["captureSessionId"],
            "expected_revision": 0, "new_revision": 1, "expected_override_id": "",
            "expected_layout_sha256": other["layoutSha256"],
            "expected_raw_sha256": other["rawSha256"],
            "data_base64": base64.b64encode(raw).decode("ascii"),
            "data_sha256": hashlib.sha256(raw).hexdigest(),
            "patches": [{"leafId": leaf["leafId"],
                         "packedBase64": base64.b64encode(packed).decode("ascii")}],
        }
        with self.assertRaisesRegex(RuntimeError, "already owns"):
            self.edits.apply(params, self.ctrl)
        self.assertEqual(len(self.ctrl.sets), 1)

    def test_capture_identity_expires_before_mutation(self):
        status = self.expose()
        self.bridge.capture_session_id = "f" * 32
        with self.assertRaisesRegex(panel.MaterialPanelError, "expired capture"):
            self.panel.update(status["sessionId"], status["parameters"][0]["parameterId"], 1.0)
        self.assertEqual(self.ctrl.sets, [])


class SharedCBufferBridgeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = cbuffer_fixture.EventCBufferBridgeTests()
        self.fixture.setUp()
        self.bridge = self.fixture.bridge
        self.ctrl = self.fixture.controller
        self.ctrl.SetFrameEvent(77, True)
        self.ctrl.GetSharedBufferPatchGeneration = lambda: 0

    def snapshot(self, array_index=0):
        return self.bridge._snapshot_shared_cbuffer({
            "expected_capture_id": CAPTURE_ID, "event_id": 77,
            "stage": "fragment", "block_index": 0, "array_index": array_index})

    def test_four_api_remote_snapshot_uses_actual_binding_and_original_metadata(self):
        for api in ("D3D11", "D3D12", "OpenGL", "Vulkan"):
            self.bridge._shared_cbuffer_edits.forget()
            self.ctrl.GetAPIProperties = lambda api=api: _value(
                pipelineType="GraphicsAPI." + api, remoteReplay=True)
            snapshot = self.snapshot()
            self.assertEqual(snapshot["schema"], shared.SNAPSHOT_SCHEMA)
            self.assertEqual(snapshot["bufferResourceId"], int(self.fixture.buffer))
            self.assertEqual(snapshot["byteOffset"], 65536)
            self.assertEqual(snapshot["fields"][0]["byteOffset"], 0)
            self.assertEqual(snapshot["fields"][0]["byteSize"], 16)
            self.assertEqual(self.ctrl.set_calls, [])

    def test_native_zero_valued_api_and_float_enums_remain_valid(self):
        class NativeZero(int):
            def __new__(cls, name):
                value = int.__new__(cls, 0)
                value.name = name
                return value

            def __str__(self):
                return self.name

        self.ctrl.GetAPIProperties = lambda: _value(pipelineType=NativeZero("GraphicsAPI.D3D11"))
        self.fixture.float4.baseType = NativeZero("VarType.Float")
        snapshot = self.snapshot()
        self.assertEqual(snapshot["api"], "d3d11")
        self.assertEqual(snapshot["fields"][0]["baseType"], "Float")
        self.assertEqual(snapshot["fields"][0]["byteSize"], 16)

    def test_array_binding_index_is_passed_to_generic_pipeline(self):
        self.ctrl.GetAPIProperties = lambda: _value(pipelineType="GraphicsAPI.Vulkan")
        self.fixture.block.bindArraySize = 4
        snapshot = self.snapshot(array_index=3)
        self.assertEqual(snapshot["arrayIndex"], 3)
        self.assertEqual(self.fixture.pipeline.constant_block_calls[-1][2], 3)
        with self.assertRaisesRegex(RuntimeError, "fixed CBuffer binding"):
            self.snapshot(array_index=4)

    def test_gl_and_vulkan_use_actual_dynamic_window_without_d3d_alignment(self):
        for api in ("OpenGL", "Vulkan"):
            self.bridge._shared_cbuffer_edits.forget()
            self.ctrl.GetAPIProperties = lambda api=api: _value(pipelineType="GraphicsAPI." + api)
            self.ctrl.event_windows[77] = (65540, bytes(range(20)))
            self.fixture.block.byteSize = 20
            snapshot = self.snapshot()
            self.assertEqual(snapshot["byteOffset"], 65540)
            self.assertEqual(snapshot["validByteSize"], 20)
            self.assertEqual(snapshot["fields"][0]["byteSize"], 16)

    def test_large_shared_window_edits_only_the_issued_field_at_actual_dynamic_offset(self):
        self.ctrl.GetAPIProperties = lambda: _value(pipelineType="GraphicsAPI.Vulkan")
        size, base, offset = 128 * 1024, 65540, 96 * 1024
        raw = bytes(range(256)) * (size // 256)
        self.ctrl.event_windows[77] = (base, raw)
        self.ctrl.buffer_length = base + size + 4096
        self.fixture.block.byteSize = size
        self.fixture.block.variables = [cbuffer_fixture._constant("largeField", offset,
                                                                  self.fixture.float4)]
        snapshot = self.snapshot()
        self.assertEqual(snapshot["validByteSize"], size)
        self.assertEqual(base64.b64decode(snapshot["rawBase64"]), raw)
        self.assertEqual(snapshot["fields"][0]["byteOffset"], offset)
        generation, calls = [0], []
        self.ctrl.GetSharedBufferPatchGeneration = lambda: generation[0]

        def setter(resource, absolute_offset, data):
            self.assertIs(resource, self.fixture.buffer)
            calls.append((absolute_offset, bytes(data)))
            generation[0] += 1
            return ""

        self.ctrl.SetSharedBufferPatch = setter
        leaf = snapshot["fields"][0]
        packed = struct.pack("<4f", 0.1, 0.2, 0.3, 0.4)
        candidate, manifest = self.fixture._patch_payload(snapshot, [(leaf["leafId"], packed)])
        self.bridge._apply_shared_cbuffer({
            "snapshot_id": snapshot["snapshotId"], "expected_capture_id": CAPTURE_ID,
            "expected_capture_session_id": CAPTURE_SESSION_ID,
            "expected_raw_sha256": snapshot["rawSha256"],
            "expected_layout_sha256": snapshot["layoutSha256"],
            "expected_override_id": "", "expected_revision": 0, "new_revision": 1,
            "data_base64": base64.b64encode(candidate).decode("ascii"),
            "data_sha256": hashlib.sha256(candidate).hexdigest(), "patches": manifest})
        self.assertEqual(calls, [(base + offset, packed)])
        self.assertEqual(candidate[:offset], raw[:offset])
        self.assertEqual(candidate[offset + 16:], raw[offset + 16:])

    def test_budget_and_window_limit_precede_native_readback_and_event_limit_is_unchanged(self):
        self.ctrl.GetAPIProperties = lambda: _value(pipelineType="GraphicsAPI.OpenGL")
        size = 128 * 1024
        self.ctrl.event_windows[77] = (65540, bytes(size))
        self.ctrl.buffer_length = 65540 + size
        self.fixture.block.byteSize = size
        edits = self.bridge._shared_cbuffer_edits
        edits._record_bytes = shared.MAX_SHARED_CBUFFER_RECORD_BYTES
        with mock.patch.object(self.ctrl, "GetBufferData", side_effect=AssertionError("readback")):
            with self.assertRaisesRegex(RuntimeError, "byte budget"):
                self.snapshot()
            edits.forget()
            self.ctrl.event_windows[77] = (0, bytes(shared.MAX_SHARED_CBUFFER_BYTES + 1))
            self.ctrl.buffer_length = shared.MAX_SHARED_CBUFFER_BYTES + 1
            with self.assertRaisesRegex(RuntimeError, "window is invalid"):
                self.snapshot()
            self.ctrl.GetAPIProperties = lambda: _value(pipelineType="GraphicsAPI.D3D11")
            self.ctrl.event_windows[77] = (65536, bytes(size))
            self.ctrl.buffer_length = 65536 + size
            with self.assertRaisesRegex(RuntimeError, "window is invalid"):
                self.bridge._event_cbuffer_live_state(
                    self.ctrl, 77, "fragment", self.bridge.rd.ShaderStage.Fragment, 0, shared=False)

    def test_missing_binding_offset_and_missing_leaf_offset_fail_closed(self):
        # The generic legacy descriptor normalizer defaults missing offsets to 0;
        # a shared write capability must never use that fallback.
        original_set = self.ctrl.SetFrameEvent
        self.ctrl.SetFrameEvent = lambda *_args: None
        del self.fixture.pipeline.binding.descriptor.byteOffset
        with self.assertRaisesRegex(RuntimeError, "exact CBuffer binding byte range"):
            self.snapshot()
        self.ctrl.SetFrameEvent = original_set
        del self.fixture.block.variables[0].byteOffset
        snapshot = self.snapshot()
        self.assertTrue(all(leaf["path"] != "颜色" for leaf in snapshot["fields"]))

    def test_shared_apply_does_not_replay_origin_eid_or_use_event_override(self):
        snapshot = self.snapshot()
        calls = []
        generation = [0]
        self.ctrl.GetSharedBufferPatchGeneration = lambda: generation[0]

        def set_shared(resource, offset, data):
            self.assertIs(resource, self.fixture.buffer)
            calls.append((offset, bytes(data)))
            generation[0] += 1
            return ""

        self.ctrl.SetSharedBufferPatch = set_shared
        self.ctrl.SetFrameEvent(78, True)
        self.ctrl.frame_event_calls.clear()
        leaf = snapshot["fields"][0]
        packed = struct.pack("<4f", 0.1, 0.2, 0.3, 0.4)
        candidate, manifest = self.fixture._patch_payload(snapshot, [(leaf["leafId"], packed)])
        response = self.bridge._apply_shared_cbuffer({
            "snapshot_id": snapshot["snapshotId"], "expected_capture_id": CAPTURE_ID,
            "expected_capture_session_id": CAPTURE_SESSION_ID,
            "expected_raw_sha256": snapshot["rawSha256"],
            "expected_layout_sha256": snapshot["layoutSha256"],
            "expected_override_id": "", "expected_revision": 0, "new_revision": 1,
            "data_base64": base64.b64encode(candidate).decode("ascii"),
            "data_sha256": hashlib.sha256(candidate).hexdigest(), "patches": manifest})
        self.assertEqual(response["schema"], shared.EDIT_SCHEMA)
        self.assertEqual(calls, [(65536, packed)])
        self.assertEqual(self.ctrl.frame_event_calls, [])
        self.assertEqual(self.ctrl.set_calls, [])


if __name__ == "__main__":
    unittest.main()
