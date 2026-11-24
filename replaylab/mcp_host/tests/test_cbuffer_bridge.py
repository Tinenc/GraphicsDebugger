import base64
import hashlib
import importlib.util
import json
import os
import struct
import sys
import types
import unittest
from unittest import mock
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
CAPTURE_ID = "a" * 64
OTHER_CAPTURE_ID = "f" * 64
CAPTURE_SESSION_ID = "b" * 32


class _ResourceId:
    def __init__(self, value=0):
        self.value = value

    def __int__(self):
        return self.value

    def __bool__(self):
        return bool(self.value)


def _load_bridge():
    fake_renderdoc = types.ModuleType("renderdoc")
    fake_renderdoc.ResourceId = _ResourceId
    fake_renderdoc.ShaderStage = types.SimpleNamespace(
        Vertex="vs",
        Hull="hs",
        Domain="ds",
        Geometry="gs",
        Fragment="ps",
        Compute="cs",
        Task=6,
        Mesh=7,
    )
    previous = sys.modules.get("renderdoc")
    sys.modules["renderdoc"] = fake_renderdoc
    try:
        spec = importlib.util.spec_from_file_location(
            "renderdoc_mcp_bridge_cbuffer_test", BRIDGE_PATH
        )
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"PySide2": None}):
            spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("renderdoc", None)
        else:
            sys.modules["renderdoc"] = previous


def _value(**values):
    return types.SimpleNamespace(**values)


def _constant_type(
    base_type,
    *,
    columns=1,
    rows=1,
    elements=1,
    array_stride=0,
    matrix_stride=0,
    flags=0,
    pointer_type_id=0xFFFFFFFF,
    members=None,
):
    return _value(
        baseType=base_type,
        rows=rows,
        columns=columns,
        elements=elements,
        arrayByteStride=array_stride,
        matrixByteStride=matrix_stride,
        members=list(members or []),
        flags=flags,
        pointerTypeID=pointer_type_id,
        name="",
    )


def _constant(name, offset, shader_type, bit_offset=0, bit_size=0):
    return _value(
        name=name,
        byteOffset=offset,
        bitFieldOffset=bit_offset,
        bitFieldSize=bit_size,
        type=shader_type,
    )


class _Pipeline:
    def __init__(self, shader, binding):
        self.shader = shader
        self.binding = binding
        self.constant_block_calls = []

    def GetShader(self, stage):
        return self.shader

    def GetShaderEntryPoint(self, stage):
        return "main"

    def GetShaderPipelineObject(self):
        return _ResourceId()

    def GetConstantBlock(self, stage, block_index, array_index):
        self.constant_block_calls.append((stage, block_index, array_index))
        return self.binding


class _Controller:
    def __init__(self, pipeline, reflection, shader, buffer, raw, buffer_length):
        self.pipeline = pipeline
        self.reflection = reflection
        self.shader = shader
        self.buffer = buffer
        self.raw = raw
        self.event_windows = {
            77: (65536, raw),
            78: (131072, bytes(range(32))),
        }
        self.buffer_length = buffer_length
        self.event = 9
        self.frame_event_calls = []
        self.set_calls = []
        self.clear_calls = []
        self.clear_all_calls = 0
        self.fail_next_set = False
        self.stage_state = _value(resourceId=shader, classInstances=[])

    def GetAPIProperties(self):
        return _value(pipelineType="GraphicsAPI.D3D11", remoteReplay=False)

    def SetFrameEvent(self, event_id, force):
        self.frame_event_calls.append(event_id)
        self.event = event_id
        window = self.event_windows.get(event_id)
        if window is not None:
            self.pipeline.binding.descriptor.byteOffset = window[0]
            self.pipeline.binding.descriptor.byteSize = len(window[1])

    def GetRootActions(self):
        # The production bridge only accepts a real Draw/Dispatch EID.  Keep
        # this fixture honest by exposing the two events backed by live CBV
        # windows as draw actions instead of relying on a pipeline-only mock.
        return [
            _value(eventId=event_id, flags=0x0002, children=[])
            for event_id in sorted(self.event_windows)
        ]

    def GetPipelineState(self):
        return self.pipeline

    def GetD3D11PipelineState(self):
        return _value(
            vertexShader=None,
            hullShader=None,
            domainShader=None,
            geometryShader=None,
            pixelShader=self.stage_state,
            computeShader=None,
        )

    def GetShaderEntryPoints(self, shader):
        return [_value(name="main")]

    def GetShader(self, pipeline, shader, entry):
        return self.reflection

    def GetBuffers(self):
        return [_value(resourceId=self.buffer, length=self.buffer_length)]

    def GetBufferData(self, buffer, byte_offset, byte_size):
        if buffer is not self.buffer:
            raise AssertionError("bridge fabricated a buffer ResourceId")
        for expected_offset, raw in self.event_windows.values():
            if byte_offset == expected_offset and byte_size == len(raw):
                return raw
        raise AssertionError("bridge did not preserve the ring-buffer window")

    def SetEventCBufferOverride(self, *arguments):
        self.set_calls.append(arguments)
        if self.fail_next_set:
            self.fail_next_set = False
            return "injected setter failure"
        return ""

    def ClearEventCBufferOverride(self, *arguments):
        self.clear_calls.append(arguments)

    def ClearEventCBufferOverrides(self):
        self.clear_all_calls += 1


class _Replay:
    def __init__(self, controller):
        self.controller = controller

    def BlockInvoke(self, function):
        function(self.controller)


class _Context:
    def __init__(self, controller):
        self.controller = controller

    def Replay(self):
        return _Replay(self.controller)

    def IsCaptureLoaded(self):
        return True

    def GetCaptureFilename(self):
        return "D:/captures/material.rdc"

    def CurEvent(self):
        return self.controller.event


class EventCBufferBridgeTests(unittest.TestCase):
    def test_replay_action_mask_fallback_is_not_overridden_by_event_filter(self):
        # The module also has an Event Browser-specific resolver. It must not
        # shadow the replay helper whose fallback preserves Draw/Dispatch
        # validation on older/minimal RenderDoc Python bindings.
        self.assertEqual(
            self.bridge._action_flags_mask(("Drawcall",), 0x0002),
            0x0002,
        )
        self.assertTrue(callable(self.bridge._event_browser_action_flags_mask))

    def setUp(self):
        self.bridge = _load_bridge()
        self.shader = _ResourceId(101)
        self.buffer = _ResourceId(202)
        self.raw = bytes(range(64))
        # Real DXBC reflection reports register strides even for ordinary
        # scalar/vector leaves; these values must not make the material empty.
        self.float4 = _constant_type(
            "VarType.Float", columns=4, array_stride=16, matrix_stride=16
        )
        self.uint_array = _constant_type(
            "VarType.UInt", elements=2, array_stride=16, matrix_stride=16
        )
        variables = [
            _constant("颜色", 0, self.float4),
            _constant("Weights", 16, self.uint_array),
        ]
        self.block = _value(
            name="CapturedMaterial",
            fixedBindNumber=3,
            fixedBindSetOrSpace=0,
            bindArraySize=1,
            byteSize=48,
            bufferBacked=True,
            inlineDataBytes=False,
            compileConstants=False,
            variables=variables,
        )
        self.reflection = _value(
            entryPoint="main",
            rawBytes=b"DXBC exact shader bytes",
            interfaces=[],
            constantBlocks=[self.block],
            stage="ShaderStage.Fragment",
        )
        binding = _value(
            descriptor=_value(
                resource=self.buffer,
                byteOffset=65536,
                byteSize=64,
            )
        )
        self.pipeline = _Pipeline(self.shader, binding)
        self.controller = _Controller(
            self.pipeline,
            self.reflection,
            self.shader,
            self.buffer,
            self.raw,
            131072 + 32,
        )
        self.context = _Context(self.controller)
        self.bridge._ctx = self.context
        capture_token = self.bridge._current_capture_token()
        self.bridge._active_capture_token = capture_token
        self.bridge._capture_session_id = CAPTURE_SESSION_ID
        self.bridge._known_catalog_capture_ids = {
            CAPTURE_ID: capture_token[1],
            OTHER_CAPTURE_ID: capture_token[1],
        }
        shader_sha256 = hashlib.sha256(self.reflection.rawBytes).hexdigest()
        common_alias = {
            "verified": True,
            "metadata": {"programVerified": True},
            "provenance": "program",
            "captureId": CAPTURE_ID,
            "shaderResourceId": int(self.shader),
            "stage": "fragment",
            "entryPoint": "main",
            "rawSha256": shader_sha256,
            "bindNumber": 3,
            "registerSpace": 0,
        }
        block_alias = dict(common_alias)
        block_alias.update(
            {"bindingKind": "constantBuffer", "displayName": "真实材质参数"}
        )
        field_alias = dict(common_alias)
        field_alias.update(
            {
                "bindingKind": "constantField",
                "byteOffset": 0,
                "displayName": "真实颜色",
            }
        )
        self.bridge._symbol_alias_overlay = {"aliases": [block_alias, field_alias]}

    def _snapshot(self, event_id=77, block_index=0):
        return self.bridge._snapshot_event_cbuffer(
            {
                "expected_capture_id": CAPTURE_ID,
                "event_id": event_id,
                "stage": "fragment",
                "block_index": block_index,
            }
        )

    @staticmethod
    def _patch_payload(snapshot, patches):
        baseline = base64.b64decode(snapshot["rawBase64"])
        candidate = bytearray(baseline)
        fields = {field["leafId"]: field for field in snapshot["fields"]}
        manifest = []
        for leaf_id, packed in patches:
            field = fields[leaf_id]
            start = field["byteOffset"]
            candidate[start : start + field["byteSize"]] = packed
            manifest.append(
                {
                    "leafId": leaf_id,
                    "packedBase64": base64.b64encode(packed).decode("ascii"),
                }
            )
        return bytes(candidate), manifest

    def _apply(self, snapshot, patches, revision, current_sha256, override_id=""):
        candidate, manifest = self._patch_payload(snapshot, patches)
        result = self.bridge._apply_event_cbuffer_override(
            {
                "snapshot_id": snapshot["snapshotId"],
                "expected_capture_id": CAPTURE_ID,
                "expected_capture_session_id": CAPTURE_SESSION_ID,
                "expected_raw_sha256": current_sha256,
                "expected_layout_sha256": snapshot["layoutSha256"],
                "expected_override_id": override_id,
                "expected_revision": revision,
                "new_revision": revision + 1,
                "data_base64": base64.b64encode(candidate).decode("ascii"),
                "data_sha256": hashlib.sha256(candidate).hexdigest(),
                "patches": manifest,
            }
        )
        return result, candidate

    def test_snapshot_apply_status_restore_and_rollback_use_exact_physical_state(self):
        snapshot = self._snapshot()
        self.assertEqual(self.controller.event, 9)
        self.assertEqual(self.pipeline.constant_block_calls[-1], ("ps", 0, 0))
        self.assertEqual(snapshot["blockIndex"], 0)
        self.assertEqual(snapshot["api"], "d3d11")
        self.assertEqual(snapshot["slot"], 3)
        self.assertEqual(snapshot["byteOffset"], 65536)
        self.assertEqual(snapshot["byteSize"], 64)
        self.assertEqual(snapshot["validByteSize"], 64)
        self.assertEqual(snapshot["constantBlockPath"], "CapturedMaterial")
        self.assertEqual(snapshot["constantBlockDisplayName"], "真实材质参数")

        color = next(field for field in snapshot["fields"] if field["path"] == "颜色")
        self.assertEqual(color["displayName"], "真实颜色")
        identity = {
            "arrayIndex": color["arrayIndex"],
            "baseType": color["baseType"],
            "byteOffset": color["byteOffset"],
            "byteSize": color["byteSize"],
            "components": color["components"],
            "path": color["path"],
        }
        expected_leaf_id = hashlib.sha256(
            json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:32]
        self.assertEqual(color["leafId"], expected_leaf_id)

        forged = bytearray(self.raw)
        forged[-1] ^= 0xFF
        with self.assertRaisesRegex(ValueError, "immutable baseline"):
            self.bridge._apply_event_cbuffer_override(
                {
                    "snapshot_id": snapshot["snapshotId"],
                    "expected_capture_id": CAPTURE_ID,
                    "expected_capture_session_id": CAPTURE_SESSION_ID,
                    "expected_raw_sha256": snapshot["rawSha256"],
                    "expected_layout_sha256": snapshot["layoutSha256"],
                    "expected_override_id": "",
                    "expected_revision": 0,
                    "new_revision": 1,
                    "data_base64": base64.b64encode(forged).decode("ascii"),
                    "data_sha256": hashlib.sha256(forged).hexdigest(),
                    "patches": [],
                }
            )
        self.assertEqual(self.controller.set_calls, [])

        first_patches = [(color["leafId"], struct.pack("<4f", 1.0, 0.5, 0.25, 1.0))]
        first, first_bytes = self._apply(
            snapshot, first_patches, 0, snapshot["rawSha256"]
        )
        first_set = self.controller.set_calls[-1]
        self.assertIs(first_set[3], self.shader)
        self.assertIs(first_set[4], self.buffer)
        self.assertEqual(first_set[2], 3)
        self.assertEqual(first_set[5:7], (65536, 64))
        self.assertEqual(first_set[8], 0)

        weight = next(
            field for field in snapshot["fields"] if field["path"] == "Weights[1]"
        )
        cumulative = first_patches + [(weight["leafId"], struct.pack("<I", 17))]
        second, second_bytes = self._apply(
            snapshot,
            cumulative,
            1,
            hashlib.sha256(first_bytes).hexdigest(),
            first["overrideId"],
        )
        self.assertEqual(second["overrideId"], first["overrideId"])
        self.assertEqual(second["revision"], 2)
        status = self.bridge._get_event_cbuffer_override_status(
            {
                "snapshot_id": snapshot["snapshotId"],
                "expected_capture_id": CAPTURE_ID,
                "expected_capture_session_id": CAPTURE_SESSION_ID,
            }
        )
        self.assertEqual(status["overrideId"], first["overrideId"])
        self.assertEqual(status["currentRawSha256"], hashlib.sha256(second_bytes).hexdigest())

        self.controller.fail_next_set = True
        replacement = [(color["leafId"], struct.pack("<4f", 0.0, 0.0, 0.0, 1.0))]
        before_failure = len(self.controller.set_calls)
        with self.assertRaisesRegex(RuntimeError, "injected setter failure"):
            self._apply(
                snapshot,
                replacement,
                2,
                hashlib.sha256(second_bytes).hexdigest(),
                first["overrideId"],
            )
        self.assertEqual(len(self.controller.set_calls), before_failure + 2)
        self.assertEqual(self.controller.set_calls[-1][-2], second_bytes)
        self.assertEqual(self.controller.set_calls[-1][-1], 0)
        preserved = self.bridge._event_cbuffer_snapshots[snapshot["snapshotId"]]
        self.assertEqual(preserved["revision"], 2)
        self.assertEqual(preserved["currentBytes"], second_bytes)

        rolled_back = self.bridge._rollback_event_cbuffer_override(
            {
                "snapshot_id": snapshot["snapshotId"],
                "override_id": "",
                "expected_capture_id": CAPTURE_ID,
                "expected_capture_session_id": CAPTURE_SESSION_ID,
            }
        )
        self.assertTrue(rolled_back["snapshotReleased"])
        self.assertTrue(rolled_back["cleared"])
        self.assertEqual(self.controller.clear_calls[-1], (77, "ps", 3, 0))
        retried = self.bridge._rollback_event_cbuffer_override(
            {
                "snapshot_id": snapshot["snapshotId"],
                "override_id": "",
                "expected_capture_id": CAPTURE_ID,
                "expected_capture_session_id": CAPTURE_SESSION_ID,
            }
        )
        self.assertTrue(retried["alreadyReleased"])
        self.assertEqual(len(self.controller.clear_calls), 1)

    def test_committed_cbuffer_mutations_refresh_snapshot_consumers_once(self):
        notifications = []
        self.bridge._notify_shader_replay_changed = lambda: notifications.append(True)

        snapshot = self._snapshot()
        color = next(field for field in snapshot["fields"] if field["path"] == "颜色")
        applied, _ = self._apply(
            snapshot,
            [(color["leafId"], struct.pack("<4f", 0.2, 0.4, 0.6, 1.0))],
            0,
            snapshot["rawSha256"],
        )
        self.assertEqual(len(notifications), 1)

        self.bridge._rollback_event_cbuffer_override(
            {
                "snapshot_id": snapshot["snapshotId"],
                "override_id": applied["overrideId"],
                "expected_capture_id": CAPTURE_ID,
                "expected_capture_session_id": CAPTURE_SESSION_ID,
            }
        )
        self.assertEqual(len(notifications), 2)

        # An idempotent retry performs no controller mutation and must not wake viewers again.
        self.bridge._rollback_event_cbuffer_override(
            {
                "snapshot_id": snapshot["snapshotId"],
                "override_id": applied["overrideId"],
                "expected_capture_id": CAPTURE_ID,
                "expected_capture_session_id": CAPTURE_SESSION_ID,
            }
        )
        self.assertEqual(len(notifications), 2)

    def test_existing_physical_key_rejects_new_owner_without_mutating_original(self):
        snapshot = self._snapshot()
        original = self.bridge._event_cbuffer_snapshots[snapshot["snapshotId"]]
        with self.assertRaisesRegex(RuntimeError, "already has") as inactive_error:
            self._snapshot()
        self.assertNotIn(snapshot["snapshotId"], str(inactive_error.exception))
        self.assertIs(
            self.bridge._event_cbuffer_snapshots[snapshot["snapshotId"]], original
        )

        second_block = _value(**dict(vars(self.block)))
        second_block.name = "SameSlotDifferentBlock"
        self.reflection.constantBlocks.append(second_block)
        with self.assertRaisesRegex(RuntimeError, "already has"):
            self._snapshot(block_index=1)
        self.assertEqual(len(self.bridge._event_cbuffer_snapshots), 1)

        color = next(field for field in snapshot["fields"] if field["path"] == "颜色")
        applied, _ = self._apply(
            snapshot,
            [(color["leafId"], struct.pack("<4f", 1.0, 1.0, 1.0, 1.0))],
            0,
            snapshot["rawSha256"],
        )
        with self.assertRaisesRegex(RuntimeError, "already has"):
            self._snapshot()
        self.assertEqual(original["overrideId"], applied["overrideId"])
        self.assertEqual(len(self.bridge._event_cbuffer_snapshots), 1)

    def test_same_event_stage_slot_can_coexist_across_register_spaces(self):
        original_live_state = self.bridge._event_cbuffer_live_state

        def live_state_with_space(ctrl, event_id, stage_name, stage, block_index):
            state = original_live_state(ctrl, event_id, stage_name, stage, 0)
            state["api"] = "d3d12"
            state["registerSpace"] = block_index
            state["constantBlockPath"] = "CapturedMaterialSpace%d" % block_index
            state["constantBlockName"] = state["constantBlockPath"]
            return state

        self.bridge._event_cbuffer_live_state = live_state_with_space
        space0 = self._snapshot(block_index=0)
        space7 = self._snapshot(block_index=7)

        self.assertEqual(space0["slot"], space7["slot"])
        self.assertEqual(space0["registerSpace"], 0)
        self.assertEqual(space7["registerSpace"], 7)
        self.assertEqual(len(self.bridge._event_cbuffer_snapshots), 2)
        self.assertEqual(len(self.bridge._event_cbuffer_snapshot_keys), 2)
        with self.assertRaisesRegex(RuntimeError, "already has"):
            self._snapshot(block_index=7)

    def test_capture_gates_and_explicit_clear_all_preserve_then_release_state(self):
        first = self._snapshot(77)
        original = self.bridge._event_cbuffer_snapshots[first["snapshotId"]]
        with self.assertRaisesRegex(RuntimeError, "session changed"):
            self.bridge._get_event_cbuffer_override_status(
                {
                    "snapshot_id": first["snapshotId"],
                    "expected_capture_id": CAPTURE_ID,
                    "expected_capture_session_id": "1" * 32,
                }
            )
        self.assertIs(self.bridge._event_cbuffer_snapshots[first["snapshotId"]], original)
        with self.assertRaisesRegex(RuntimeError, "catalog identity"):
            self.bridge._get_event_cbuffer_override_status(
                {
                    "snapshot_id": first["snapshotId"],
                    "expected_capture_id": OTHER_CAPTURE_ID,
                    "expected_capture_session_id": CAPTURE_SESSION_ID,
                }
            )
        self.assertIs(self.bridge._event_cbuffer_snapshots[first["snapshotId"]], original)

        color = next(field for field in first["fields"] if field["path"] == "颜色")
        self._apply(
            first,
            [(color["leafId"], struct.pack("<4f", 0.1, 0.2, 0.3, 0.4))],
            0,
            first["rawSha256"],
        )
        second = self._snapshot(78)
        self.assertEqual(second["bufferResourceId"], first["bufferResourceId"])
        self.assertEqual(second["byteOffset"], 131072)
        self.assertEqual(second["byteSize"], 32)
        self.assertEqual(second["validByteSize"], 32)
        with self.assertRaisesRegex(RuntimeError, "session changed"):
            self.bridge._clear_all_event_cbuffer_snapshots(
                {
                    "expected_capture_id": CAPTURE_ID,
                    "expected_capture_session_id": "2" * 32,
                }
            )
        self.assertEqual(self.controller.clear_all_calls, 0)
        cleared = self.bridge._clear_all_event_cbuffer_snapshots(
            {
                "expected_capture_id": CAPTURE_ID,
                "expected_capture_session_id": CAPTURE_SESSION_ID,
            }
        )
        self.assertEqual(cleared["clearedOverrideCount"], 1)
        self.assertEqual(cleared["releasedSnapshotCount"], 2)
        self.assertTrue(cleared["cleared"])
        self.assertEqual(self.controller.clear_all_calls, 1)
        self.assertEqual(self.bridge._event_cbuffer_snapshots, {})
        self.assertEqual(self.bridge._event_cbuffer_snapshot_keys, {})
        self.assertEqual(self.bridge._event_cbuffer_released_snapshots, {})
        replacement = self._snapshot(77)
        self.assertNotEqual(replacement["snapshotId"], first["snapshotId"])
        self.assertNotEqual(replacement["snapshotId"], second["snapshotId"])

    def test_editable_leaf_gate_rejects_ambiguous_physical_types(self):
        values = [
            _constant("ok", 0, _constant_type("VarType.Float")),
            _constant(
                "matrix", 16, _constant_type("VarType.Float", rows=4, columns=4)
            ),
            _constant(
                "pointer", 32, _constant_type("VarType.UInt", pointer_type_id=3)
            ),
            _constant(
                "truncated", 36, _constant_type("VarType.SInt", flags=0x0100)
            ),
            _constant(
                "badStride",
                40,
                _constant_type("VarType.UInt", elements=2, array_stride=2),
            ),
            _constant(
                "bitfield",
                48,
                _constant_type("VarType.UInt"),
                bit_size=3,
            ),
        ]
        leaves = self.bridge._event_cbuffer_editable_leaves(values, 64, 64)
        self.assertEqual([leaf["path"] for leaf in leaves], ["ok"])

    def test_snapshot_never_replays_invalid_event_zero_for_restore(self):
        self.controller.event = 0
        snapshot = self._snapshot()
        self.assertEqual(self.controller.event, 77)
        self.assertNotIn(0, self.controller.frame_event_calls)

    def test_present_is_rejected_but_older_bindings_without_actions_remain_compatible(self):
        self.controller.GetRootActions = lambda: [
            _value(eventId=77, flags=0x0100, children=[])
        ]
        with self.assertRaisesRegex(
                RuntimeError,
                "Select an actual Draw, Dispatch, or MeshDispatch EID"):
            self._snapshot()
        self.assertEqual(self.bridge._event_cbuffer_snapshots, {})

        # Older/minimal Python replay bindings may not expose GetRootActions.
        # The bridge must preserve compatibility and defer the authoritative
        # action check to the native transactional setter.
        self.controller.GetRootActions = None
        snapshot = self._snapshot()
        self.assertEqual(snapshot["eventId"], 77)
        self.assertEqual(snapshot["slot"], 3)

    def test_unique_d3d12_entry_point_case_normalisation_is_accepted(self):
        self.controller.GetShaderEntryPoints = lambda unused_shader: [
            _value(name="Main")
        ]
        self.reflection.entryPoint = "Main"
        snapshot = self._snapshot()
        record = self.bridge._event_cbuffer_snapshots[snapshot["snapshotId"]]
        self.assertEqual(record["entryPoint"], "Main")

        self.bridge._rollback_event_cbuffer_override({
            "snapshot_id": snapshot["snapshotId"],
            "override_id": "",
            "expected_capture_id": CAPTURE_ID,
            "expected_capture_session_id": CAPTURE_SESSION_ID,
        })
        self.controller.GetShaderEntryPoints = lambda unused_shader: [
            _value(name="Main"), _value(name="MAIN")
        ]
        with self.assertRaisesRegex(
                RuntimeError, "exact shader entry point is unavailable"):
            self._snapshot()

    def test_d3d12_root_cbv_uses_reflected_window_not_upload_ring_remainder(self):
        self.bridge.rd.ShaderStage.Fragment = 4
        self.block.fixedBindSetOrSpace = 7
        self.block.byteSize = 48
        raw = bytes(range(48))

        class D3D12Controller(_Controller):
            stage_index = 4

            def GetAPIProperties(inner_self):
                return _value(pipelineType="GraphicsAPI.D3D12", remoteReplay=False)

            def SetFrameEvent(inner_self, event_id, force):
                inner_self.frame_event_calls.append(event_id)
                inner_self.event = event_id
                if event_id == 77:
                    inner_self.pipeline.binding.descriptor.byteOffset = 65536
                    # This is how RenderDoc's generic root-descriptor view is
                    # represented for an upload ring: the entire remainder.
                    inner_self.pipeline.binding.descriptor.byteSize = 1024 * 1024

            def GetD3D12PipelineState(inner_self):
                root_descriptor = _value(
                    type="DescriptorType.ConstantBuffer",
                    resource=inner_self.buffer,
                    byteOffset=65536,
                )
                root_param = _value(
                    visibility=1 << inner_self.stage_index,
                    reg=3,
                    space=7,
                    descriptor=root_descriptor,
                )
                return _value(
                    pipelineResourceId=_ResourceId(),
                    rootSignature=_value(parameters=[root_param]),
                )

            def GetBufferData(inner_self, buffer, byte_offset, byte_size):
                if buffer is not inner_self.buffer:
                    raise AssertionError("bridge fabricated a buffer ResourceId")
                if byte_offset != 65536 or byte_size != len(raw):
                    raise AssertionError("root-CBV snapshot did not use reflected size")
                return raw

        controller = D3D12Controller(
            self.pipeline, self.reflection, self.shader, self.buffer, raw,
            65536 + 1024 * 1024)
        controller.event_windows = {77: (65536, raw)}
        self.context.controller = controller
        self.bridge._ctx = self.context

        snapshot = self._snapshot()
        self.assertEqual(snapshot["api"], "d3d12")
        self.assertEqual(snapshot["registerSpace"], 7)
        self.assertEqual(snapshot["byteOffset"], 65536)
        self.assertEqual(snapshot["byteSize"], 48)
        self.assertEqual(snapshot["validByteSize"], 48)
        self.assertEqual(base64.b64decode(snapshot["rawBase64"]), raw)
        cleared = self.bridge._clear_all_event_cbuffer_snapshots({
            "expected_capture_id": CAPTURE_ID,
            "expected_capture_session_id": CAPTURE_SESSION_ID,
        })
        self.assertTrue(cleared["cleared"])
        self.assertEqual(cleared["releasedSnapshotCount"], 1)
        self.assertEqual(controller.clear_all_calls, 1)

        controller.GetRootActions = lambda: [
            _value(eventId=77, flags=0x0002 | 0x080000, children=[])
        ]
        with self.assertRaisesRegex(
                RuntimeError, "ExecuteIndirect root-CBV editing is excluded"):
            self.bridge._event_cbuffer_live_state(
                controller, 77, "fragment", 4, 0)
        controller.GetRootActions = lambda: [
            _value(eventId=77, flags=0x0008, children=[])
        ]

        for stage_name, stage_index in (("task", 6), ("mesh", 7)):
            controller.stage_index = stage_index
            self.reflection.stage = stage_name
            state = self.bridge._event_cbuffer_live_state(
                controller, 77, stage_name, stage_index, 0)
            self.assertEqual(state["stageName"], stage_name)
            self.assertEqual(state["registerSpace"], 7)
            self.assertEqual(state["byteSize"], 48)


if __name__ == "__main__":
    unittest.main()
