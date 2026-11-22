from __future__ import annotations

import ast
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


REPOSITORY = Path(__file__).resolve().parents[2]
BRIDGE_PATH = REPOSITORY / "renderdoc_mcp_bridge" / "__init__.py"
SERVER_PATH = REPOSITORY / "mcp_host" / "src" / "server.py"


class _ResourceId:
    def __init__(self, value: int):
        self._value = value

    def __int__(self):
        return self._value


class _Subresource:
    def __init__(self, mip=0, slice=0, sample=0):
        self.mip = mip
        self.slice = slice
        self.sample = sample


class _DebugPixelInputs:
    def __init__(self):
        self.sample = 0xFFFFFFFF
        self.primitive = 0xFFFFFFFF
        self.view = 0xFFFFFFFF


class _Colour:
    def __init__(self, values):
        self.floatValue = list(values)
        self.intValue = [int(value) for value in values]
        self.uintValue = [int(value) for value in values]


class _ModificationValue:
    def __init__(self, values, depth=0.5, stencil=3, valid=True):
        self.col = _Colour(values)
        self.depth = depth
        self.stencil = stencil
        self._valid = valid

    def IsValid(self):
        return self._valid


class _Modification:
    _FAILURES = (
        "sampleMasked", "backfaceCulled", "depthClipped", "depthBoundsFailed",
        "viewClipped", "scissorClipped", "shaderDiscarded", "depthTestFailed",
        "stencilTestFailed", "predicationSkipped",
    )

    def __init__(self, event_id, fragment, primitive, passed=True, direct=False):
        self.eventId = event_id
        self.fragIndex = fragment
        self.primitiveID = primitive
        self.preMod = _ModificationValue([0.1, 0.2, 0.3, 1.0])
        self.shaderOut = _ModificationValue([0.4, 0.5, 0.6, 1.0], depth=0.4)
        self.postMod = _ModificationValue([0.7, 0.8, 0.9, 1.0], depth=0.4)
        self.directShaderWrite = direct
        self.unboundPS = False
        for name in self._FAILURES:
            setattr(self, name, False)
        if not passed:
            self.depthTestFailed = True

    def Passed(self):
        return not any(getattr(self, name) for name in self._FAILURES)


class _Action:
    def __init__(self, event_id, flags, name):
        self.eventId = event_id
        self.actionId = event_id
        self.flags = flags
        self.customName = name
        self.children = []


class _Replay:
    def __init__(self, controller):
        self.controller = controller

    def BlockInvoke(self, callback):
        callback(self.controller)


class _Context:
    def __init__(self, controller, event_id=99):
        self.controller = controller
        self.event_id = event_id

    def Replay(self):
        return _Replay(self.controller)

    def CurEvent(self):
        return self.event_id

    def IsCaptureLoaded(self):
        return True


class _Controller:
    def __init__(self):
        self.texture_id = _ResourceId(101)
        self.buffer_id = _ResourceId(202)
        self.texture = types.SimpleNamespace(
            resourceId=self.texture_id,
            byteSize=8000,
            width=64,
            height=32,
            depth=1,
            dimension=2,
            mips=3,
            arraysize=1,
            msSamp=1,
            format=types.SimpleNamespace(name="R16G16B16A16_FLOAT"),
        )
        self.buffer = types.SimpleNamespace(resourceId=self.buffer_id, length=5000)
        self.actions = [
            _Action(10, 0x0002, "Opaque mesh"),
            _Action(20, 0x0004, "Compute write"),
            _Action(30, 0x0002, "Occluded mesh"),
        ]
        self.modifications = [
            _Modification(10, 0, 7, passed=True),
            _Modification(30, 0, 9, passed=False),
            _Modification(20, 0, 0xFFFFFFFF, passed=True, direct=True),
        ]
        self.set_events = []
        self.resource_arguments = []
        self.buffer_reads = []
        self.last_subresource = None
        self.last_debug_inputs = None

    def GetTextures(self):
        return [self.texture]

    def GetBuffers(self):
        return [self.buffer]

    def GetResources(self):
        return [self.texture, self.buffer]

    def GetPipelineState(self):
        output = types.SimpleNamespace(
            resource=self.texture_id, firstMip=0, firstSlice=0
        )
        depth = types.SimpleNamespace(resource=_ResourceId(0))
        return types.SimpleNamespace(
            GetOutputTargets=lambda: [output],
            GetDepthTarget=lambda: depth,
        )

    def GetRootActions(self):
        return self.actions

    def PixelHistory(self, resource_id, x, y, subresource, component_type):
        self.resource_arguments.append(resource_id)
        self.last_subresource = subresource
        return self.modifications

    def SetFrameEvent(self, event_id, force):
        self.set_events.append((event_id, force))

    def GetTextureData(self, resource_id, subresource):
        self.resource_arguments.append(resource_id)
        return b"T" * 8000

    def GetBufferData(self, resource_id, offset, length):
        self.resource_arguments.append(resource_id)
        self.buffer_reads.append((resource_id, offset, length))
        return b"B" * 5000

    def PickPixel(self, resource_id, x, y, subresource, component_type):
        self.resource_arguments.append(resource_id)
        return types.SimpleNamespace(floatValue=[0.1, 0.2, 0.3, 1.0])

    def GetMinMax(self, resource_id, subresource, component_type):
        self.resource_arguments.append(resource_id)
        return (
            types.SimpleNamespace(floatValue=[0.0, 0.0, 0.0, 0.0]),
            types.SimpleNamespace(floatValue=[1.0, 1.0, 1.0, 1.0]),
        )

    def DebugPixel(self, x, y, inputs):
        self.last_debug_inputs = inputs
        return None


def _load_bridge():
    fake_renderdoc = types.ModuleType("renderdoc")
    fake_renderdoc.Subresource = _Subresource
    fake_renderdoc.DebugPixelInputs = _DebugPixelInputs
    fake_renderdoc.CompType = types.SimpleNamespace(Typeless=0)
    fake_renderdoc.ActionFlags = types.SimpleNamespace(
        Clear=0x0001,
        Drawcall=0x0002,
        Dispatch=0x0004,
        MeshDispatch=0x0008,
        Present=0x0100,
        Copy=0x0400,
        Resolve=0x0800,
        GenMips=0x1000,
        DispatchRay=0x4000,
        BuildAccStruct=0x8000,
    )
    fake_renderdoc.GPUCounter = types.SimpleNamespace(EventGPUDuration=1)
    previous = sys.modules.get("renderdoc")
    sys.modules["renderdoc"] = fake_renderdoc
    try:
        spec = importlib.util.spec_from_file_location(
            "renderdoc_mcp_bridge_pixel_contract", BRIDGE_PATH
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("renderdoc", None)
        else:
            sys.modules["renderdoc"] = previous


class PixelObjectInspectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = _load_bridge()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        patch = mock.patch.object(self.bridge, "_replaylab_local_root", temporary.name)
        patch.start()
        self.addCleanup(patch.stop)

    def test_inspector_preserves_history_and_classifies_without_object_guess(self):
        controller = _Controller()
        self.bridge._ctx = _Context(controller)

        result = self.bridge._inspect_pixel_object({
            "texture_id": 101,
            "x": 12,
            "y": 8,
            "event_id": 42,
            "mip": 0,
            "slice": 0,
            "sample": 0,
            "prefer_geometry": True,
        })

        self.assertEqual(
            result["schema"], "renderdoc-ultra-pixel-object-inspector/v1"
        )
        self.assertIs(controller.resource_arguments[0], controller.texture_id)
        self.assertEqual(controller.set_events, [(42, True), (99, True)])
        self.assertEqual(result["eventRestore"], {
            "originalEventId": 99, "frozenEventId": 42, "restored": True,
        })
        self.assertEqual(len(result["modifications"]), 3)
        first = result["modifications"][0]
        self.assertEqual(first["fragIndex"], 0)
        self.assertEqual(first["primitiveId"], 7)
        self.assertEqual(first["pre"]["floatValue"], [0.1, 0.2, 0.3, 1.0])
        self.assertEqual(first["shaderOut"]["depth"], 0.4)
        self.assertEqual(first["post"]["stencil"], 3)
        self.assertIn("predicationSkipped", first["flags"])
        self.assertTrue(result["modifications"][1]["flags"]["depthTestFailed"])
        self.assertIsNone(first.get("contributed"))
        self.assertFalse(first["contributionConfirmed"])
        self.assertEqual(
            result["modifications"][2]["writeClassification"],
            "potential-direct-shader-write",
        )

        categories = result["categories"]
        self.assertEqual(categories["lastRecordedModification"]["eventId"], 20)
        self.assertEqual(categories["lastPassedWriteCandidate"]["eventId"], 20)
        self.assertEqual(categories["lastConfirmedNonDirectWrite"]["eventId"], 10)
        self.assertEqual(categories["lastPassedRasterGeometry"]["eventId"], 10)
        self.assertEqual(
            [item["eventId"] for item in categories["allPassedWriteCandidates"]],
            [10, 20],
        )
        self.assertEqual(
            [item["eventId"] for item in categories["potentialDirectShaderWrites"]],
            [20],
        )
        self.assertEqual(result["preferredSelection"]["kind"],
                         "LastPassedRasterGeometry")
        self.assertIsNone(result["preferredSelection"]["objectIdentity"])
        self.assertEqual(result["confidence"]["objectIdentity"], "none")
        self.assertEqual(result["confidence"]["lastPassedWriteCandidate"], "low")
        self.assertEqual(result["historyScope"]["cutoffEventId"], 42)
        self.assertTrue(result["historyScope"]["coversThroughLastListedAction"])
        self.assertNotIn("instanceId", json.dumps(result))
        self.assertFalse(result["invocationPolicy"]["hoverSupported"])

    def test_legacy_resource_tools_use_resolved_ids_and_report_truncation(self):
        controller = _Controller()
        self.bridge._ctx = _Context(controller)

        texture_data = self.bridge._get_texture_data({
            "resource_id": 101, "mip": 0, "slice": 0,
        })
        self.assertEqual(texture_data["length"], 8000)
        self.assertEqual(texture_data["returnedBytes"], 7500)
        self.assertTrue(texture_data["truncated"])
        self.assertEqual(len(base64.b64decode(texture_data["base64"])), 7500)

        buffer_data = self.bridge._get_buffer_data({
            "resource_id": 202, "offset": 0, "length": 5000,
        })
        self.assertEqual(controller.buffer_reads[-1], (
            controller.buffer_id, 0, self.bridge.MAX_BUFFER_DATA_BYTES,
        ))
        self.assertEqual(buffer_data["returnedBytes"], 4096)
        self.assertTrue(buffer_data["truncated"])
        self.assertEqual(buffer_data["hexReturnedBytes"], 512)

        read_count = len(controller.buffer_reads)
        with self.assertRaisesRegex(ValueError, "length must be positive"):
            self.bridge._get_buffer_data({
                "resource_id": 202, "offset": 0, "length": 0,
            })
        with self.assertRaisesRegex(ValueError, "length must be positive"):
            self.bridge._get_buffer_data({
                "resource_id": 202, "offset": 0, "length": -1,
            })
        self.assertEqual(len(controller.buffer_reads), read_count)

        self.bridge._pick_pixel({"resource_id": 101, "x": 0, "y": 0})
        self.bridge._get_texture_minmax({"resource_id": 101})
        history = self.bridge._pixel_history({
            "resource_id": 101, "x": 0, "y": 0, "event_id": 99,
        })
        self.assertEqual(len(history), 3)
        self.assertTrue(all(
            argument is controller.texture_id
            for argument in controller.resource_arguments
            if argument is not controller.buffer_id
        ))
        self.assertIn(controller.buffer_id, controller.resource_arguments)

        with self.assertRaisesRegex(ValueError, "Unknown texture resource ID"):
            self.bridge._pick_pixel({"resource_id": 999, "x": 0, "y": 0})

    def test_ipc_request_reader_rejects_oversize_symlink_and_non_regular(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid = root / "valid.json"
            valid.write_bytes(
                b'{"id":"request","method":"ping","params":{}}')
            self.assertEqual(
                self.bridge._read_ipc_request_json(str(valid))["id"],
                "request",
            )

            oversize = root / "oversize.json"
            with oversize.open("wb") as stream:
                stream.truncate(self.bridge.MAX_IPC_REQUEST_BYTES + 1)
            with self.assertRaisesRegex(ValueError, "maximum size"):
                self.bridge._read_ipc_request_json(str(oversize))

            directory = root / "directory.json"
            directory.mkdir()
            with self.assertRaisesRegex(ValueError, "regular file"):
                self.bridge._read_ipc_request_json(str(directory))

            symlink = root / "symlink.json"
            try:
                symlink.symlink_to(valid)
            except (NotImplementedError, OSError):
                symlink = None
            if symlink is not None:
                with self.assertRaisesRegex(ValueError, "regular file"):
                    self.bridge._read_ipc_request_json(str(symlink))

    def test_lighting_cbuffer_reads_only_the_bound_window(self):
        class _LightingController:
            def __init__(self):
                self.calls = []

            def GetBufferData(self, resource_id, offset, length):
                self.calls.append((resource_id, offset, length))
                return b"L" * (length + 32)

        controller = _LightingController()
        resource_id = _ResourceId(303)
        binding = types.SimpleNamespace(byteOffset=128, byteSize=8192)
        data, offset, length = self.bridge._read_lighting_cbuffer_data(
            controller, resource_id, binding)
        self.assertEqual((offset, length), (
            128, self.bridge.MAX_LIGHTING_CBUFFER_BYTES,
        ))
        self.assertEqual(controller.calls, [(
            resource_id, 128, self.bridge.MAX_LIGHTING_CBUFFER_BYTES,
        )])
        self.assertEqual(len(data), self.bridge.MAX_LIGHTING_CBUFFER_BYTES)

        empty_binding = types.SimpleNamespace(byteOffset=64, byteSize=0)
        empty, offset, length = self.bridge._read_lighting_cbuffer_data(
            controller, resource_id, empty_binding)
        self.assertIsNone(empty)
        self.assertEqual((offset, length), (64, 0))
        self.assertEqual(len(controller.calls), 1)

    def test_event_output_fingerprint_hashes_full_bytes_and_restores_event(self):
        controller = _Controller()
        self.bridge._ctx = _Context(controller, event_id=99)
        self.bridge._capture_session_id = "a" * 32
        result = self.bridge._fingerprint_event_outputs({
            "event_id": 10,
            "grid_size": 2,
            "expected_capture_session_id": "a" * 32,
        })
        self.assertEqual(result["eventId"], 10)
        self.assertEqual(result["captureSessionId"], "a" * 32)
        self.assertEqual(len(result["outputs"]), 1)
        output = result["outputs"][0]
        self.assertEqual(output["rawHashStatus"], "hashed")
        self.assertEqual(output["rawByteSize"], 8000)
        self.assertEqual(output["rawSha256"], hashlib.sha256(b"T" * 8000).hexdigest())
        self.assertEqual(len(output["samples"]), 4)
        self.assertEqual(controller.set_events, [(10, True), (99, True)])
        self.assertFalse(result["semanticEquivalenceVerified"])

    def test_event_output_fingerprint_skips_large_full_read(self):
        controller = _Controller()
        controller.texture.byteSize = 33 * 1024 * 1024

        def unexpected_full_read(*_args):
            raise AssertionError("large output must not be materialised")

        controller.GetTextureData = unexpected_full_read
        self.bridge._ctx = _Context(controller, event_id=99)
        self.bridge._capture_session_id = "a" * 32
        result = self.bridge._fingerprint_event_outputs({
            "event_id": 10,
            "grid_size": 2,
            "expected_capture_session_id": "a" * 32,
        })
        output = result["outputs"][0]
        self.assertEqual(output["rawHashStatus"], "skipped-texture-too-large")
        self.assertIsNone(output["rawByteSize"])
        self.assertIsNone(output["rawSha256"])
        self.assertEqual(len(output["samples"]), 4)

    def test_inspector_validates_subresources_and_restores_after_failure(self):
        controller = _Controller()
        self.bridge._ctx = _Context(controller)
        with self.assertRaisesRegex(ValueError, "Sample 1"):
            self.bridge._inspect_pixel_object({
                "texture_id": 101, "x": 0, "y": 0, "event_id": 42, "sample": 1,
            })
        self.assertEqual(controller.set_events, [(42, True), (99, True)])

    def test_debug_pixel_can_select_event_and_restores_it(self):
        controller = _Controller()
        self.bridge._ctx = _Context(controller)
        result = self.bridge._debug_pixel({
            "x": 3,
            "y": 4,
            "event_id": 10,
            "sample": 0,
            "primitive_id": 7,
            "view": 0,
        })
        self.assertIn("error", result)
        self.assertEqual(controller.set_events, [(10, True), (99, True)])
        self.assertEqual(controller.last_debug_inputs.sample, 0)
        self.assertEqual(controller.last_debug_inputs.primitive, 7)
        self.assertEqual(controller.last_debug_inputs.view, 0)

    def test_debug_pixel_serializes_current_state_changes_and_preserves_zero_components(self):
        controller = _Controller()
        self.bridge._ctx = _Context(controller)
        trace = types.SimpleNamespace(debugger=object())
        before = types.SimpleNamespace(name="r0", type="VarType.Float", rows=1, columns=3,
                                       members=[], value=types.SimpleNamespace(f32v=[1.0, 0.0, 0.0]))
        after = types.SimpleNamespace(name="r0", type="VarType.Float", rows=1, columns=3,
                                      members=[], value=types.SimpleNamespace(f32v=[2.0, float("inf"), 0.0]))
        # ShaderDebugState has changes, not sourceVars. No source mapping is fabricated.
        state = types.SimpleNamespace(stepIndex=7, nextInstruction=12, flags="NoEvent",
                                      changes=[types.SimpleNamespace(before=before, after=after)],
                                      callstack=["MainPS", "Lighting"])
        batches = iter([[state], []])
        freed = []
        controller.DebugPixel = lambda *_args: trace
        controller.ContinueDebug = lambda _debugger: next(batches)
        controller.FreeTrace = freed.append
        result = self.bridge._debug_pixel({"x": 3, "y": 4, "event_id": 10})
        step = result["trace"][0]
        self.assertEqual(step["changes"][0]["before"]["value"], [1.0, 0.0, 0.0])
        self.assertEqual(step["vars"][0]["value"], [2.0, None, 0.0])
        self.assertEqual(step["vars"][0]["nonFinite"], [{"index": 1, "value": "+inf"}])
        self.assertEqual(step["nextInstruction"], 12)
        self.assertEqual(step["callstack"], ["MainPS", "Lighting"])
        self.assertEqual(result["variableRepresentation"], "debug-variable-changes")
        self.assertFalse(result["traceTruncated"])
        self.assertEqual(freed, [trace])
        self.assertEqual(controller.set_events, [(10, True), (99, True)])
        json.dumps(result, allow_nan=False)

    def test_debug_vertex_bounds_retained_trace_but_reports_full_step_count(self):
        controller = _Controller()
        self.bridge._ctx = _Context(controller)
        trace = types.SimpleNamespace(debugger=object())
        states = [types.SimpleNamespace(stepIndex=i, nextInstruction=i, changes=[], callstack=[])
                  for i in range(65)]
        batches = iter([states[:40], states[40:], []])
        freed = []
        controller.DebugVertex = lambda *_args: trace
        controller.ContinueDebug = lambda _debugger: next(batches)
        controller.FreeTrace = freed.append
        result = self.bridge._debug_vertex({"vertex_id": 7})
        self.assertEqual(result["steps"], 65)
        self.assertEqual(len(result["trace"]), 50)
        self.assertTrue(result["traceTruncated"])
        self.assertEqual(result["trace"][-1]["step"], 49)
        self.assertEqual(freed, [trace])
        artifact = result["traceArtifact"]
        records = [json.loads(line) for line in Path(artifact["path"]).read_text().splitlines()]
        self.assertEqual(records[-2]["step"], 64)
        self.assertTrue(artifact["complete"])
        self.assertTrue(artifact["executionComplete"])
        self.assertEqual(records[-1]["steps"], 65)
        self.assertEqual(hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest(), artifact["sha256"])
        self.assertEqual(controller.set_events, [(99, True)])

    def test_debug_trace_late_actual_values_have_reusable_byte_offsets(self):
        controller = _Controller()
        self.bridge._ctx = _Context(controller)
        trace = types.SimpleNamespace(debugger=object())
        states = []
        for number in range(3483):
            variable = types.SimpleNamespace(
                name="result", type="Float", rows=1, columns=2, members=[],
                value=types.SimpleNamespace(f32v=[float(number), 0.0]))
            states.append(types.SimpleNamespace(
                stepIndex=number, nextInstruction=number, callstack=["MainPS"],
                changes=[types.SimpleNamespace(before=variable, after=variable)]))
        batches = iter([states, []])
        controller.DebugPixel = lambda *_args: trace
        controller.ContinueDebug = lambda _debugger: next(batches)
        freed = []
        controller.FreeTrace = freed.append
        result = self.bridge._debug_pixel({"x": 3, "y": 4, "event_id": 10})
        artifact = result["traceArtifact"]
        self.assertEqual(result["steps"], 3483)
        self.assertEqual(len(result["trace"]), 50)
        self.assertTrue(artifact["complete"])
        with Path(artifact["path"]).open("rb") as stream:
            stream.seek(artifact["stepIndex"][-1]["offset"])
            tail = [json.loads(line) for line in stream]
        self.assertEqual(tail[-2]["step"], 3482)
        self.assertEqual(tail[-2]["changes"][0]["after"]["value"], [3482.0, 0.0])
        self.assertEqual(freed, [trace])
        self.assertEqual(controller.set_events, [(10, True), (99, True)])

    def test_debug_artifact_limits_are_explicit_and_stop_unbounded_continuation(self):
        for limit_name, limit_value, expected in (
                ("_SHADER_DEBUG_TRACE_MAX_STEPS", 60, "step-limit"),
                ("_SHADER_DEBUG_TRACE_MAX_BYTES", 8192, "byte-limit")):
            with self.subTest(limit=limit_name), mock.patch.object(self.bridge, limit_name, limit_value):
                controller = _Controller()
                self.bridge._ctx = _Context(controller)
                trace = types.SimpleNamespace(debugger=object())
                states = [types.SimpleNamespace(
                    stepIndex=i, nextInstruction=i, changes=[], callstack=[]) for i in range(80)]
                controller.DebugVertex = lambda *_args: trace
                calls = []
                controller.ContinueDebug = lambda _debugger: calls.append(True) or states
                freed = []
                controller.FreeTrace = freed.append
                result = self.bridge._debug_vertex({"vertex_id": 3})
                artifact = result["traceArtifact"]
                self.assertFalse(artifact["complete"])
                self.assertFalse(artifact["executionComplete"])
                self.assertIn(expected, artifact["truncationReasons"])
                self.assertEqual(len(calls), 1)
                self.assertLessEqual(artifact["byteSize"], self.bridge._SHADER_DEBUG_TRACE_MAX_BYTES)
                self.assertEqual(freed, [trace])
                self.assertEqual(controller.set_events, [(99, True)])

    def test_debug_artifact_prunes_only_owned_finished_traces(self):
        root = Path(self.bridge._shader_debug_trace_root())
        unrelated = root / "user-document.jsonl"
        unrelated.write_text("keep")
        old = root / ("1-" + "a" * 32 + "-" + "b" * 32 + ".jsonl")
        old.write_text("old")
        self.bridge.os.utime(str(old), (1, 1))
        pending = root / ("1-" + "c" * 32 + "-" + "d" * 32 + ".jsonl.part")
        pending.write_text("active")
        self.bridge._shader_debug_trace_root()
        self.assertFalse(old.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(pending.exists())

    def test_debug_trace_is_freed_when_continuation_fails(self):
        for operation in ("pixel", "vertex"):
            with self.subTest(operation=operation):
                controller = _Controller()
                self.bridge._ctx = _Context(controller)
                trace = types.SimpleNamespace(debugger=object())
                freed = []
                controller.DebugPixel = lambda *_args: trace
                controller.DebugVertex = lambda *_args: trace
                controller.FreeTrace = freed.append

                def fail(_debugger):
                    raise RuntimeError("debug continuation failed")

                controller.ContinueDebug = fail
                with self.assertRaisesRegex(RuntimeError, "debug continuation failed"):
                    if operation == "pixel":
                        self.bridge._debug_pixel({"event_id": 10})
                    else:
                        self.bridge._debug_vertex({"vertex_id": 0})
                self.assertEqual(freed, [trace])
                if operation == "pixel":
                    self.assertEqual(controller.set_events, [(10, True), (99, True)])
                else:
                    self.assertEqual(controller.set_events, [(99, True)])
                self.assertEqual(list(Path(self.bridge._shader_debug_trace_root()).glob("*.part")), [])

    def test_vertex_event_restored_when_debug_creation_or_trace_release_fails(self):
        for phase in ("create", "release"):
            with self.subTest(phase=phase):
                controller = _Controller()
                self.bridge._ctx = _Context(controller)

                def fail(*_args):
                    raise RuntimeError("debug failed")

                controller.DebugVertex = fail if phase == "create" else (
                    lambda *_args: types.SimpleNamespace(debugger=object()))
                controller.FreeTrace = fail
                controller.ContinueDebug = lambda *_args: []
                with self.assertRaisesRegex(RuntimeError, "debug failed"):
                    self.bridge._debug_vertex({"vertex_id": 7})
                self.assertEqual(controller.set_events, [(99, True)])

    def test_debug_values_use_declared_integer_and_half_storage(self):
        signed = types.SimpleNamespace(name="index", type="SInt", rows=1, columns=2,
                                       members=[], value=types.SimpleNamespace(s32v=[-3, 0]))
        half = types.SimpleNamespace(name="weight", type="Half", rows=1, columns=2,
                                     members=[], value=types.SimpleNamespace(u16v=[0x3c00, 0]))
        unavailable = types.SimpleNamespace(name="gone", type="Unknown", rows=0, columns=0,
                                            members=[], value=object())
        self.assertEqual(self.bridge._debug_variable_snapshot(signed)["value"], [-3, 0])
        self.assertEqual(self.bridge._debug_variable_snapshot(half)["value"], [1.0, 0.0])
        self.assertFalse(self.bridge._debug_variable_snapshot(unavailable)["valueAvailable"])

    def test_action_timings_use_real_duration_counter_or_report_unsupported(self):
        controller = _Controller()
        description = types.SimpleNamespace(
            name="Event GPU Duration",
            description="Time taken for this event on the GPU",
            resultType="CompType.Float",
            resultByteWidth=8,
            unit="CounterUnit.Seconds",
        )
        controller.EnumerateCounters = lambda: [1]
        controller.DescribeCounter = lambda counter: description
        controller.FetchCounters = lambda counters: [
            types.SimpleNamespace(
                eventId=10,
                counter=1,
                value=types.SimpleNamespace(d=0.001),
            ),
            types.SimpleNamespace(
                eventId=20,
                counter=1,
                value=types.SimpleNamespace(d=0.002),
            ),
        ]
        self.bridge._ctx = _Context(controller)
        result = self.bridge._get_action_timings({"event_ids": [10]})
        self.assertTrue(result["supported"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["timings"][0]["eventId"], 10)
        self.assertAlmostEqual(result["timings"][0]["milliseconds"], 1.0)
        self.assertEqual(controller.set_events, [(99, True)])

        unsupported = _Controller()
        unsupported.EnumerateCounters = lambda: []
        self.bridge._ctx = _Context(unsupported)
        result = self.bridge._get_action_timings({})
        self.assertFalse(result["supported"])
        self.assertIn("not available", result["reason"])
        self.assertEqual(unsupported.set_events, [(99, True)])

    def test_server_and_bridge_contracts_are_registered(self):
        server_tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))
        functions = {
            node.name: node
            for node in server_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertIn("inspect_pixel_object", functions)
        inspect_args = [
            argument.arg for argument in functions["inspect_pixel_object"].args.args
        ]
        self.assertEqual(inspect_args, [
            "texture_id", "x", "y", "event_id", "mip", "slice", "sample",
            "prefer_geometry",
        ])
        debug_args = [argument.arg for argument in functions["debug_pixel"].args.args]
        self.assertEqual(debug_args, [
            "x", "y", "event_id", "sample", "primitive_id", "view",
        ])

        bridge_source = BRIDGE_PATH.read_text(encoding="utf-8")
        ast.parse(bridge_source, feature_version=(3, 6))
        self.assertIn('"inspect_pixel_object": _inspect_pixel_object', bridge_source)
        self.assertIn('"get_action_timings": _get_action_timings', bridge_source)


if __name__ == "__main__":
    unittest.main()
