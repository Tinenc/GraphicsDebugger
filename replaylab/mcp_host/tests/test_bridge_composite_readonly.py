from __future__ import annotations

import importlib.util
import math
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
            "renderdoc_mcp_bridge_composite_readonly_contract", BRIDGE_PATH
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("renderdoc", None)
        else:
            sys.modules["renderdoc"] = previous


class CompositeReadonlyBridgeTests(unittest.TestCase):
    def _collect_flow_pair(
        self,
        bridge,
        *,
        fragment=True,
        rasterizer_discard=False,
        depth_test=True,
        stencil_test=False,
        include_uav=False,
        include_stream_output=False,
        indexed=False,
    ):
        bridge.rd.ShaderStage = types.SimpleNamespace(Vertex=1, Fragment=2)
        bridge.rd.ActionFlags = types.SimpleNamespace(
            Drawcall=0x0002,
            Dispatch=0x0004,
            MeshDispatch=0x0008,
            DispatchRay=0x4000,
            Indexed=0x010000,
            Indirect=0x080000,
        )
        bridge.rd.DescriptorFlags = types.SimpleNamespace(
            ReadOnlyAccess=0x8,
            WriteOnlyAccess=0x10,
        )
        bridge.rd.ResourceId = lambda: 0
        flags = 0x0002 | (0x010000 if indexed else 0)
        action = types.SimpleNamespace(
            eventId=9,
            actionId=9,
            customName="Draw",
            flags=flags,
            children=[],
            outputs=[10],
            depthOut=20,
            numIndices=3,
            numInstances=1,
            drawIndex=7,
            dispatchBase=(1, 2, 3),
        )
        color_equation = types.SimpleNamespace(
            source="BlendMultiplier.SrcAlpha",
            destination="BlendMultiplier.InvSrcAlpha",
            operation="BlendOperation.Add",
        )
        alpha_equation = types.SimpleNamespace(
            source="BlendMultiplier.One",
            destination="BlendMultiplier.Zero",
            operation="BlendOperation.Max",
        )
        blend = types.SimpleNamespace(
            enabled=True,
            logicOperationEnabled=False,
            colorBlend=color_equation,
            alphaBlend=alpha_equation,
            logicOperation="LogicOperation.NoOp",
            writeMask=15,
        )

        class Pipeline:
            def GetShader(self, stage):
                if stage == 1:
                    return 101
                if stage == 2 and fragment:
                    return 102
                return 0

            def GetSamplers(self, stage, only_used=True):
                return []

            def GetReadOnlyResources(self, stage, only_used=True):
                return []

            def GetConstantBlocks(self, stage, only_used=True):
                return []

            def GetReadWriteResources(self, stage, only_used=True):
                if include_uav and stage == 1:
                    return [types.SimpleNamespace(
                        descriptor=types.SimpleNamespace(
                            resource=30, flags=0x10))]
                return []

            def GetVBuffers(self):
                return [types.SimpleNamespace(
                    resourceId=50, byteOffset=4, byteSize=256,
                    byteStride=32)]

            def GetVertexInputs(self):
                return [types.SimpleNamespace(
                    name="POSITION", vertexBuffer=0, byteOffset=0,
                    format=types.SimpleNamespace(name="R32G32B32_FLOAT"),
                    perInstance=False, instanceRate=0,
                    genericEnabled=False, used=True)]

            def GetIBuffer(self):
                return types.SimpleNamespace(
                    resourceId=60, byteOffset=2, byteSize=128,
                    byteStride=2)

            def GetColorBlends(self):
                return [blend]

            def GetOutputTargets(self):
                return [types.SimpleNamespace(resource=10)]

            def GetDepthTarget(self):
                return types.SimpleNamespace(resource=20)

            def GetPrimitiveTopology(self):
                return "TriangleList"

            def GetViewport(self, unused_index):
                return None

            def GetScissor(self, unused_index):
                return None

            def GetStencilFaces(self):
                face = types.SimpleNamespace(
                    function="CompareFunction.AlwaysTrue",
                    failOperation="StencilOperation.Keep",
                    depthFailOperation="StencilOperation.Keep",
                    passOperation="StencilOperation.Keep",
                    reference=0,
                    compareMask=255,
                    writeMask=0,
                )
                return face, face

            def GetRasterizedStream(self):
                if rasterizer_discard is None:
                    raise RuntimeError("rasterized stream is not exposed")
                return 0

        pipeline = Pipeline()

        class Controller:
            def GetResources(self):
                return [
                    types.SimpleNamespace(resourceId=value, name="R%d" % value)
                    for value in (10, 20, 30, 40, 41, 50, 60)
                ]

            def GetTextures(self):
                return [
                    types.SimpleNamespace(
                        resourceId=value,
                        format=types.SimpleNamespace(name="RGBA8"),
                        msSamp=4,
                    )
                    for value in (10, 20)
                ]

            def GetRootActions(self):
                return [action]

            def SetFrameEvent(self, event_id, force):
                self.event = (event_id, force)

            def GetPipelineState(self):
                return pipeline

            def GetShaderEntryPoints(self, shader_id):
                return []

            def GetAPIProperties(self):
                return types.SimpleNamespace(pipelineType="GraphicsAPI.D3D12")

            def GetVulkanPipelineState(self):
                raise RuntimeError("inactive")

            def GetD3D12PipelineState(self):
                depth = types.SimpleNamespace(
                    depthEnable=depth_test,
                    depthWrites=depth_test,
                    depthFunction="CompareFunction.Less",
                    stencilEnable=stencil_test,
                )
                outputs = []
                if include_stream_output:
                    outputs.append(types.SimpleNamespace(
                        resourceId=40,
                        byteOffset=8,
                        byteSize=64,
                        writtenCountResourceId=41,
                        writtenCountByteOffset=0,
                    ))
                return types.SimpleNamespace(
                    outputMerger=types.SimpleNamespace(
                        depthStencilState=depth,
                        depthReadOnly=False,
                        stencilReadOnly=True,
                    ),
                    rasterizer=types.SimpleNamespace(
                        state=types.SimpleNamespace(
                            rasterizerDiscardEnable=rasterizer_discard,
                            cullMode="CullMode.Back",
                            fillMode="FillMode.Solid",
                            frontCCW=False,
                        )
                    ),
                    streamOut=types.SimpleNamespace(outputs=outputs),
                )

        controller = Controller()
        session_id = "b" * 32
        bridge._capture_session_id = session_id
        bridge._ctx = types.SimpleNamespace(CurEvent=lambda: 7)
        bridge._replay = lambda callback: callback(controller)
        frame = bridge._get_current_frame_flow_inputs({
            "expected_capture_session_id": session_id,
            "max_actions": 10,
        })
        single = bridge._draw_call_details_on_replay(controller, 9)
        return frame, frame["pipelineStates"]["9"], single

    def test_composite_methods_are_registered(self) -> None:
        bridge = load_bridge()

        for name in (
            "inspect_event",
            "analyze_pixel",
            "inspect_shader",
            "get_current_frame_flow_inputs",
        ):
            self.assertIn(name, bridge.METHODS)

    def test_depth_state_uses_only_the_active_backend_and_honours_readonly(self):
        bridge = load_bridge()

        class Controller:
            def GetAPIProperties(self):
                return types.SimpleNamespace(pipelineType="GraphicsAPI.D3D12")

            def GetD3D11PipelineState(self):
                raise AssertionError("inactive backend must not be queried")

            def GetD3D12PipelineState(self):
                depth = types.SimpleNamespace(
                    depthEnable=True,
                    depthWrites=True,
                    depthFunction="CompareFunction.LessEqual",
                    stencilEnable=True,
                )
                return types.SimpleNamespace(
                    outputMerger=types.SimpleNamespace(
                        depthStencilState=depth,
                        depthReadOnly=True,
                        stencilReadOnly=False,
                    ),
                    rasterizer=types.SimpleNamespace(
                        state=types.SimpleNamespace(
                            cullMode="CullMode.Back",
                            fillMode="FillMode.Solid",
                            frontCCW=True,
                        )
                    ),
                )

            def GetVulkanPipelineState(self):
                raise AssertionError("inactive backend must not be queried")

            def GetOpenGLPipelineState(self):
                raise AssertionError("inactive backend must not be queried")

        state = bridge._pass_depth_state(Controller())
        self.assertTrue(state["depthTestEnabled"])
        self.assertFalse(state["depthWriteEnabled"])
        self.assertTrue(state["depthReadOnly"])
        self.assertEqual("CompareFunction.LessEqual", state["depthFunction"])
        self.assertTrue(state["stencilTestEnabled"])
        self.assertFalse(state["stencilReadOnly"])
        self.assertTrue(state["rasterStateResolved"])
        self.assertEqual("CullMode.Back", state["cullMode"])
        self.assertEqual("FillMode.Solid", state["fillMode"])
        self.assertTrue(state["frontCCW"])

    def test_stencil_faces_and_sampler_bindings_remain_candidate_evidence(self):
        bridge = load_bridge()

        class Pipeline:
            def GetPrimitiveTopology(self):
                return "TriangleList"

            def GetViewport(self, unused_index):
                return None

            def GetScissor(self, unused_index):
                return None

            def GetStencilFaces(self):
                front = types.SimpleNamespace(
                    function="CompareFunction.Equal",
                    failOperation="StencilOperation.Keep",
                    depthFailOperation="StencilOperation.Keep",
                    passOperation="StencilOperation.Replace",
                    reference=3,
                    compareMask=255,
                    writeMask=15,
                )
                back = types.SimpleNamespace(
                    function="CompareFunction.AlwaysTrue",
                    failOperation="StencilOperation.Keep",
                    depthFailOperation="StencilOperation.Keep",
                    passOperation="StencilOperation.Keep",
                    reference=0,
                    compareMask=255,
                    writeMask=0,
                )
                return front, back

        state = bridge._pass_draw_state(Pipeline())
        self.assertTrue(state["stencilStateResolved"])
        self.assertTrue(state["stencilWriteOperationsCanWrite"])
        self.assertEqual(15, state["stencilFront"]["writeMask"])

        binding = bridge._pass_sampler_binding(
            types.SimpleNamespace(
                access=types.SimpleNamespace(
                    index=0xffff,
                    arrayElement=2,
                    descriptorStore=91,
                    byteOffset=64,
                    byteSize=32,
                    staticallyUnused=False,
                ),
                sampler=types.SimpleNamespace(
                    object=0,
                    creationTimeConstant=True,
                    type="DescriptorType.Sampler",
                    addressU="AddressMode.Wrap",
                    addressV="AddressMode.ClampEdge",
                    addressW="AddressMode.Wrap",
                    compareFunction="CompareFunction.AlwaysTrue",
                    filter="TextureFilter.Linear",
                    maxAnisotropy=8.0,
                    minLOD=0.0,
                    maxLOD=12.0,
                    mipBias=0.0,
                ),
            )
        )
        self.assertTrue(binding["directAccess"])
        self.assertNotIn("bindingIndex", binding)
        self.assertTrue(binding["creationTimeConstant"])
        self.assertFalse(binding["runtimeAccessProven"])
        self.assertEqual(
            "not-statically-unused-candidate", binding["accessEvidence"]
        )
        unused = bridge._pass_sampler_binding(types.SimpleNamespace(
            access=types.SimpleNamespace(staticallyUnused=True),
            sampler=types.SimpleNamespace(),
        ))
        self.assertTrue(unused["staticallyUnused"])
        self.assertEqual("statically-unused", unused["accessEvidence"])

    def test_stencil_only_depth_attachment_read_matches_single_event(self):
        bridge = load_bridge()
        unused_frame, state, single = self._collect_flow_pair(
            bridge, depth_test=False, stencil_test=True)

        frame_accesses = [
            value["access"] for value in state["readResources"]
            if value["resourceId"] == 20
        ]
        single_accesses = [
            value["access"] for value in single["inputs"]
            if value["resourceId"] == 20
        ]
        self.assertIn("stencil-test", frame_accesses)
        self.assertIn("stencil-test", single_accesses)
        self.assertNotIn("depth-test", frame_accesses)
        self.assertNotIn("depth-test", single_accesses)

    def test_known_discard_strips_all_raster_io_in_both_paths(self):
        bridge = load_bridge()
        frame, state, single = self._collect_flow_pair(
            bridge,
            rasterizer_discard=True,
            include_uav=True,
            include_stream_output=True,
        )
        self.assertFalse(state["rasterOutputApplicable"])
        self.assertEqual(
            "rasterizer-discard", state["rasterOutputSuppressionReason"])
        self.assertFalse(single["drawState"]["rasterOutputApplicable"])
        self.assertEqual(
            "rasterizer-discard",
            single["drawState"]["rasterOutputSuppressionReason"],
        )
        raster_accesses = bridge._RASTER_IO_ACCESSES
        self.assertTrue(all(
            value["access"] not in raster_accesses
            for value in state["readResources"] +
            state["writeResources"] + state.get("possibleOutputs", [])
        ))
        self.assertTrue(all(
            value["access"] not in raster_accesses
            for value in single["inputs"] + single["outputs"] +
            single["possibleOutputs"]
        ))
        for values in (state["writeResources"], single["outputs"]):
            accesses = {value["access"] for value in values}
            self.assertIn("shader-write", accesses)
            self.assertIn("stream-output", accesses)
            self.assertIn("stream-output-counter", accesses)
        self.assertEqual([], state["renderTargets"])
        self.assertIsNone(state["depthTarget"])
        self.assertEqual([], single["renderTargets"])
        self.assertIsNone(single["depthTarget"])
        self.assertNotIn("outputs", frame["actions"][0])
        self.assertNotIn("depthOutput", frame["actions"][0])

    def test_missing_fragment_suppresses_color_but_retains_depth_stencil_io(self):
        bridge = load_bridge()
        frame, state, single = self._collect_flow_pair(
            bridge,
            fragment=False,
            rasterizer_discard=False,
            depth_test=True,
            stencil_test=True,
        )
        for draw_state in (state, single["drawState"]):
            self.assertTrue(draw_state["rasterOutputApplicable"])
            self.assertEqual(
                "no-fragment-shader",
                draw_state["rasterOutputSuppressionReason"],
            )
        for values in (
            state["readResources"] + state["writeResources"] +
            state.get("possibleOutputs", []),
            single["inputs"] + single["outputs"] + single["possibleOutputs"],
        ):
            accesses = {value["access"] for value in values}
            self.assertTrue(accesses.isdisjoint(
                bridge._COLOR_RASTER_IO_ACCESSES))
            self.assertIn("depth-test", accesses)
            self.assertIn("stencil-test", accesses)
            self.assertIn("depth-target", accesses)
        self.assertEqual([], state["renderTargets"])
        self.assertEqual(20, state["depthTarget"]["resourceId"])
        self.assertEqual([], single["renderTargets"])
        self.assertEqual(20, single["depthTarget"])
        self.assertNotIn("outputs", frame["actions"][0])
        self.assertEqual(20, frame["actions"][0]["depthOutput"]["resourceId"])

    def test_unknown_discard_downgrades_raster_writes_in_both_paths(self):
        bridge = load_bridge()
        frame, state, single = self._collect_flow_pair(
            bridge, rasterizer_discard=None, depth_test=True)
        for draw_state in (state, single["drawState"]):
            self.assertIsNone(draw_state["rasterOutputApplicable"])
            self.assertEqual(
                "rasterizer-discard-unknown",
                draw_state["rasterOutputSuppressionReason"],
            )
        for definite, possible in (
            (state["writeResources"], state["possibleOutputs"]),
            (single["outputs"], single["possibleOutputs"]),
        ):
            definite_accesses = {value["access"] for value in definite}
            possible_accesses = {value["access"] for value in possible}
            self.assertNotIn("color-target", definite_accesses)
            self.assertNotIn("depth-target", definite_accesses)
            self.assertIn("color-target-possible", possible_accesses)
            self.assertIn("depth-target-possible", possible_accesses)
            self.assertTrue(all(
                value.get("certainty") == "unknown-rasterizer-discard"
                for value in possible
                if value["access"] in (
                    "color-target-possible", "depth-target-possible")
            ))
        self.assertEqual([10], [value["resourceId"]
                               for value in state["renderTargets"]])
        self.assertEqual(20, state["depthTarget"]["resourceId"])
        self.assertNotIn("outputs", frame["actions"][0])
        self.assertNotIn("depthOutput", frame["actions"][0])

    def test_per_draw_pipeline_details_preserve_only_observed_values(self):
        bridge = load_bridge()
        unused_frame, state, single = self._collect_flow_pair(
            bridge, indexed=True)

        for draw_state in (state, single["drawState"]):
            self.assertEqual(4, draw_state["sampleCount"])
            self.assertTrue(draw_state["sampleCountResolved"])
            self.assertEqual(2, draw_state["indexStride"])
            self.assertEqual("POSITION", draw_state["vertexLayout"][0]["name"])
            self.assertEqual(
                "R32G32B32_FLOAT", draw_state["vertexLayout"][0]["format"])
            blend = draw_state["colorTargetStates"][0]
            self.assertEqual(
                "BlendMultiplier.SrcAlpha",
                blend["colorBlend"]["sourceFactor"],
            )
            self.assertEqual(
                "BlendMultiplier.InvSrcAlpha",
                blend["colorBlend"]["destinationFactor"],
            )
            self.assertEqual(
                "BlendOperation.Max", blend["alphaBlend"]["operation"])
            self.assertEqual("LogicOperation.NoOp", blend["logicOperation"])
        for values in (state["readResources"], single["inputs"]):
            vertex = next(
                value for value in values
                if value["access"] == "vertex-buffer")
            index = next(
                value for value in values
                if value["access"] == "index-buffer")
            self.assertEqual(32, vertex["byteStride"])
            self.assertEqual(2, index["byteStride"])
        for workload in (
                unused_frame["actions"][0]["workload"], single["workload"]):
            self.assertEqual(7, workload["drawIndex"])
            self.assertEqual([1, 2, 3], workload["dispatchBase"])
        self.assertEqual("graphics", single["actionKind"])

        unknown = bridge._action_workload(types.SimpleNamespace(
            numIndices=1, numInstances=1))
        self.assertIsNone(unknown["drawIndex"])
        self.assertIsNone(unknown["dispatchBase"])

    def test_single_event_present_ignores_stale_pipeline_outputs(self) -> None:
        bridge = load_bridge()
        bridge.rd.ShaderStage = types.SimpleNamespace()
        bridge.rd.ResourceId = lambda: 0

        present = types.SimpleNamespace(
            eventId=2,
            actionId=2,
            customName="Present",
            flags=0x0100,
            outputs=[55],
            children=[],
            numIndices=0,
            numInstances=0,
        )

        class Pipeline:
            def GetVBuffers(self):
                return []

            def GetVertexInputs(self):
                return []

            def GetIBuffer(self):
                return None

            def GetColorBlends(self):
                return [
                    types.SimpleNamespace(
                        enabled=False,
                        logicOperationEnabled=False,
                        writeMask=15,
                    )
                ]

            def GetOutputTargets(self):
                return [types.SimpleNamespace(resource=99)]

            def GetDepthTarget(self):
                return None

        class Controller:
            def GetPipelineState(self):
                return Pipeline()

            def GetRootActions(self):
                return [present]

            def GetResources(self):
                return [
                    types.SimpleNamespace(resourceId=55, name="Backbuffer"),
                    types.SimpleNamespace(resourceId=99, name="Stale RT"),
                ]

            def GetTextures(self):
                return []

            def GetAPIProperties(self):
                return types.SimpleNamespace(pipelineType="GraphicsAPI.Unknown")

        result = bridge._draw_call_details_on_replay(Controller(), 2)
        self.assertEqual([], result["outputs"])
        self.assertEqual([], result["possibleOutputs"])
        self.assertEqual([55], [value["resourceId"] for value in result["inputs"]])
        self.assertEqual("present-source", result["inputs"][0]["access"])
        self.assertFalse(result["participation"]["outputAffectingCandidate"])
        self.assertFalse(result["drawState"]["pipelineStateApplicable"])
        self.assertEqual("utility", result["actionKind"])

    def test_single_event_copy_resolve_coalesces_destination_roles(self) -> None:
        bridge = load_bridge()
        bridge.rd.ShaderStage = types.SimpleNamespace()
        bridge.rd.ResourceId = lambda: 0

        class Pipeline:
            def GetVBuffers(self):
                return []

            def GetVertexInputs(self):
                return []

            def GetIBuffer(self):
                return None

            def GetColorBlends(self):
                return []

            def GetOutputTargets(self):
                return []

            def GetDepthTarget(self):
                return None

        class Controller:
            def __init__(self, action):
                self.action = action

            def GetPipelineState(self):
                return Pipeline()

            def GetRootActions(self):
                return [self.action]

            def GetResources(self):
                return [types.SimpleNamespace(
                    resourceId=55, name="Destination")]

            def GetTextures(self):
                return []

            def GetAPIProperties(self):
                return types.SimpleNamespace(
                    pipelineType="GraphicsAPI.Unknown")

        for flag in (0x0400, 0x0800):
            with self.subTest(flag=flag):
                action = types.SimpleNamespace(
                    eventId=2,
                    actionId=2,
                    customName="Transfer",
                    flags=flag,
                    copyDestination=55,
                    copyDestinationSubresource=types.SimpleNamespace(
                        mip=2, slice=0, sample=0, plane=0),
                    outputs=[55],
                    children=[],
                    numIndices=0,
                    numInstances=0,
                )
                result = bridge._draw_call_details_on_replay(
                    Controller(action), 2)
                self.assertEqual("utility", result["actionKind"])
                self.assertEqual(1, len(result["outputs"]))
                output = result["outputs"][0]
                self.assertEqual("copy-destination", output["access"])
                self.assertEqual(
                    ["copy-destination", "action-output"], output["roles"])
                self.assertEqual(2, output["subresource"]["mip"])

    def test_utility_destination_excludes_inherited_color_and_depth_targets(self) -> None:
        bridge = load_bridge()
        bridge.rd.ShaderStage = types.SimpleNamespace()
        bridge.rd.ResourceId = lambda: 0
        pipeline = types.SimpleNamespace(
            GetVBuffers=lambda: [], GetVertexInputs=lambda: [], GetIBuffer=lambda: None,
            GetColorBlends=lambda: [], GetOutputTargets=lambda: [], GetDepthTarget=lambda: None)
        for flag in (0x0001, 0x100001, 0x0400, 0x0800):
            with self.subTest(flag=flag):
                action = types.SimpleNamespace(
                    eventId=2, actionId=2, customName="Utility", flags=flag,
                    copyDestination=55, outputs=[66], depthOut=77, children=[],
                    numIndices=0, numInstances=0)
                controller = types.SimpleNamespace(
                    GetPipelineState=lambda: pipeline, GetRootActions=lambda: [action],
                    GetResources=lambda: [], GetTextures=lambda: [],
                    GetAPIProperties=lambda: types.SimpleNamespace(pipelineType="GraphicsAPI.Unknown"))
                result = bridge._draw_call_details_on_replay(controller, 2)
                self.assertEqual([55], [row["resourceId"] for row in result["outputs"]])
                self.assertEqual([], result["possibleOutputs"])
                self.assertEqual("utility", result["actionKind"])
                self.assertFalse(result["drawState"]["pipelineStateApplicable"])
                # If this API's native action lacks an exact destination, its
                # declared targets remain candidates rather than disappearing.
                action.copyDestination = 0
                conservative = bridge._draw_call_details_on_replay(controller, 2)
                resources = {row["resourceId"] for row in
                             conservative["outputs"] + conservative["possibleOutputs"]}
                self.assertEqual({66, 77}, resources)

    def test_frame_flow_input_receipt_is_self_measured_and_limit_truncated(self):
        bridge = load_bridge()
        session_id = "a" * 32
        bridge._capture_session_id = session_id
        bridge._ctx = types.SimpleNamespace(CurEvent=lambda: 7)
        replay_calls = []

        class Controller:
            def GetResources(self):
                return []

            def GetTextures(self):
                return []

            def GetRootActions(self):
                return [
                    types.SimpleNamespace(
                        eventId=1,
                        actionId=1,
                        customName="Marker A",
                        flags=0,
                        children=[],
                    ),
                    types.SimpleNamespace(
                        eventId=2,
                        actionId=2,
                        customName="Marker B",
                        flags=0,
                        children=[],
                    ),
                ]

            def SetFrameEvent(self, event_id, force):
                self.restored = (event_id, force)

        controller = Controller()

        def replay(callback):
            replay_calls.append(callback)
            return callback(controller)

        bridge._replay = replay
        result = bridge._get_current_frame_flow_inputs(
            {
                "expected_capture_session_id": session_id,
                "max_actions": 1,
            }
        )
        self.assertEqual(1, len(replay_calls))
        self.assertEqual(1, len(result["actions"]))
        self.assertTrue(result["summary"]["truncated"])
        self.assertIn("action-limit", result["summary"]["truncationReasons"])
        self.assertEqual(
            bridge._frame_flow_response_json_bytes(result),
            result["summary"]["serializedBytes"],
        )

    def test_ipc_bytes_hard_limit_and_resource_metadata_budget_are_explicit(self):
        bridge = load_bridge()
        value = {"id": "x", "result": {"message": "路径"}}
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "response.json"
            bridge._atomic_write_json(str(target), value)
            self.assertEqual(bridge._ipc_json_bytes(value), target.read_bytes())

            oversized = {
                "id": "x",
                "result": {
                    "schema": "replaylab-current-frame-flow-inputs/v1",
                    "blob": "x" * 512,
                },
            }
            original_limit = bridge.MAX_FRAME_FLOW_SERIALIZED_BYTES
            bridge.MAX_FRAME_FLOW_SERIALIZED_BYTES = 128
            try:
                with self.assertRaisesRegex(ValueError, "hard limit"):
                    bridge._atomic_write_json(str(target), oversized)
            finally:
                bridge.MAX_FRAME_FLOW_SERIALIZED_BYTES = original_limit

            with self.assertRaises(ValueError):
                bridge._ipc_json_bytes({"result": math.nan})

        class Controller:
            def GetResources(self):
                return [
                    types.SimpleNamespace(resourceId=index, name="N" * 100)
                    for index in range(1, 6)
                ]

            def GetTextures(self):
                return [
                    types.SimpleNamespace(
                        resourceId=index, format="RGBA8", msSamp=1)
                    for index in range(1, 6)
                ]

        old_records = bridge.MAX_FRAME_FLOW_RESOURCE_METADATA_RECORDS
        old_bytes = bridge.MAX_FRAME_FLOW_RESOURCE_METADATA_BYTES
        bridge.MAX_FRAME_FLOW_RESOURCE_METADATA_RECORDS = 2
        bridge.MAX_FRAME_FLOW_RESOURCE_METADATA_BYTES = 2048
        try:
            metadata, receipt = bridge._bounded_frame_flow_resource_metadata(
                Controller())
        finally:
            bridge.MAX_FRAME_FLOW_RESOURCE_METADATA_RECORDS = old_records
            bridge.MAX_FRAME_FLOW_RESOURCE_METADATA_BYTES = old_bytes
        self.assertEqual(2, len(metadata))
        self.assertEqual(10, receipt["sourceRecordCount"])
        self.assertEqual(6, receipt["omittedSourceRecordCount"])
        self.assertLessEqual(receipt["budgetedBytes"], receipt["byteBudget"])
        self.assertTrue(receipt["truncated"])

    def test_shader_variable_non_finite_floats_remain_strict_json(self):
        bridge = load_bridge()
        variable = types.SimpleNamespace(
            name="capturedValue",
            value=types.SimpleNamespace(
                f32v=[1.0, math.nan, math.inf, -math.inf, 0.0],
                u32v=[0, 0, 0, 0],
                s32v=[0, 0, 0, 0],
            ),
            members=[],
        )

        result = bridge._var_dict(variable)

        self.assertEqual([1.0, None, None, None], result["float"])
        self.assertEqual(
            [
                {"index": 1, "value": "nan"},
                {"index": 2, "value": "+inf"},
                {"index": 3, "value": "-inf"},
            ],
            result["floatNonFinite"],
        )
        bridge._ipc_json_bytes(result)

    def test_inspect_event_restores_the_visible_event_through_request_tracking(self) -> None:
        bridge = load_bridge()

        class Controller:
            def __init__(self):
                self.events = []

            def SetFrameEvent(self, event_id, force):
                self.events.append((event_id, force))

        controller = Controller()

        class Replay:
            def BlockInvoke(self, callback):
                callback(controller)

        bridge._ctx = types.SimpleNamespace(
            IsCaptureLoaded=lambda: True,
            CurEvent=lambda: 7,
            Replay=lambda: Replay(),
        )
        bridge._synchronise_capture_lifecycle = lambda: None
        bridge._publish_console_event = lambda *args, **kwargs: None
        bridge._draw_call_details_on_replay = lambda ctrl, event_id: {
            "eventId": event_id,
        }
        bridge._pipeline_state_on_replay = lambda ctrl: {"pipeline": True}

        response = bridge.handle_request(
            {"id": 3, "method": "inspect_event", "params": {"event_id": 41}}
        )

        self.assertNotIn("error", response)
        self.assertEqual([(41, True), (7, True)], controller.events)

    def test_inspect_event_collects_draw_and_pipeline_in_one_replay(self) -> None:
        bridge = load_bridge()
        replay_calls = []

        class Controller:
            def __init__(self):
                self.events = []

            def SetFrameEvent(self, event_id, force):
                self.events.append((event_id, force))

        controller = Controller()

        def replay(callback):
            replay_calls.append(callback)
            return callback(controller)

        bridge._replay = replay
        bridge._draw_call_details_on_replay = lambda ctrl, event_id: {
            "eventId": event_id,
            "draw": True,
        }
        bridge._pipeline_state_on_replay = lambda ctrl: {"pipeline": True}

        result = bridge._inspect_event({"event_id": 41})

        self.assertEqual(1, len(replay_calls))
        self.assertEqual([(41, True)], controller.events)
        self.assertEqual("renderdoc-internal-inspect-event/v1", result["schema"])
        self.assertEqual({"eventId": 41, "draw": True}, result["drawCall"])
        self.assertEqual({"pipeline": True}, result["pipeline"])

    def test_analyze_pixel_collects_all_evidence_and_restores_in_one_replay(self) -> None:
        bridge = load_bridge()
        replay_calls = []
        helper_calls = []

        class Controller:
            def __init__(self):
                self.events = []

            def SetFrameEvent(self, event_id, force):
                self.events.append((event_id, force))

        controller = Controller()

        def replay(callback):
            replay_calls.append(callback)
            return callback(controller)

        bridge._ctx = types.SimpleNamespace(
            IsCaptureLoaded=lambda: True,
            CurEvent=lambda: 7,
        )
        bridge._replay = replay
        bridge._synchronise_capture_lifecycle = lambda: None
        bridge._publish_console_event = lambda *args, **kwargs: None

        def texture_info(ctrl, resource_id):
            helper_calls.append(("texture", ctrl, resource_id))
            return {"id": resource_id}

        def texture_minmax(ctrl, resource_id):
            helper_calls.append(("minmax", ctrl, resource_id))
            return {"min": 0, "max": 1}

        def inspect(ctrl, texture_id, x, y, mip, array_slice, sample,
                    prefer_geometry, cutoff_event_id):
            helper_calls.append(
                (
                    "inspection", ctrl, texture_id, x, y, mip, array_slice,
                    sample, prefer_geometry, cutoff_event_id,
                )
            )
            return {"pixel": [x, y]}

        bridge._texture_info_on_replay = texture_info
        bridge._texture_minmax_on_replay = texture_minmax
        bridge._inspect_pixel_object_on_replay = inspect

        response = bridge.handle_request(
            {
                "id": 5,
                "method": "analyze_pixel",
                "params": {
                    "texture_id": 99,
                    "x": 12,
                    "y": 34,
                    "event_id": 41,
                    "mip": 1,
                    "slice": 2,
                    "sample": 3,
                    "prefer_geometry": False,
                },
            }
        )
        result = response["result"]

        self.assertEqual(1, len(replay_calls))
        self.assertEqual([(41, True), (7, True)], controller.events)
        self.assertEqual(["texture", "minmax", "inspection"], [c[0] for c in helper_calls])
        self.assertEqual("renderdoc-internal-analyze-pixel/v1", result["schema"])
        self.assertEqual({"id": 99}, result["texture"])
        self.assertEqual({"min": 0, "max": 1}, result["minMax"])
        self.assertEqual(
            {"originalEventId": 7, "frozenEventId": 41, "restored": True},
            result["inspection"]["eventRestore"],
        )

    def test_inspect_shader_runs_shader_info_once_without_reverse_duplication(self) -> None:
        bridge = load_bridge()
        calls = []

        def shader_info(params):
            calls.append(params)
            return {"disassembly": {"DXIL": "shader"}}

        bridge._get_shader_info = shader_info
        bridge._reverse_shader = lambda params: self.fail(
            "inspect_shader must not repeat reconstruction through reverse_shader"
        )

        result = bridge._inspect_shader({"event_id": 41, "stage": "pixel"})

        self.assertEqual([{"event_id": 41, "stage": "fragment"}], calls)
        self.assertEqual("renderdoc-internal-inspect-shader/v2", result["schema"])
        self.assertEqual("fragment", result["stage"])
        self.assertEqual({"disassembly": {"DXIL": "shader"}}, result["shader"])
        self.assertNotIn("reconstructionEvidence", result)

    def test_readonly_shader_prefers_source_or_one_portable_ir_without_external_tools(self) -> None:
        bridge = load_bridge()
        entry = types.SimpleNamespace(name="MainPS")
        reflection = types.SimpleNamespace(entryPoint="MainPS", encoding="DXIL", debugInfo=None,
                                            inputSignature=[], outputSignature=[])
        calls = []
        pipeline = types.SimpleNamespace(GetShader=lambda stage: 99)
        controller = types.SimpleNamespace(
            GetPipelineState=lambda: pipeline,
            GetShaderEntryPoints=lambda sid: [entry],
            GetShader=lambda *args: reflection,
            GetAPIProperties=lambda: types.SimpleNamespace(pipelineType="D3D12"),
            GetDisassemblyTargets=lambda full: ["AMD GCN ISA", "DXBC / DXIL", "DXC DXIL"],
            DisassembleShader=lambda pid, refl, target: calls.append(target) or "define void @MainPS() { ret void }",
            GetResources=lambda: [], GetTextures=lambda: [],
        )
        bridge._replay = lambda callback: callback(controller)
        bridge._available_shader_stages = lambda names: [("fragment", "PS")]
        bridge._set_frame_event = lambda *args: None
        bridge._get_pipeline_obj = lambda *args: 17
        bridge._pipeline_shader_entry_selection = lambda *args: {"entry": entry, "evidence": {"entryPoints": []}}
        bridge._shader_reflection_identity_failure = lambda *args: None
        bridge._shader_raw_bytes = lambda *args: b"captured shader"
        bridge._compiled_shader_interface = lambda *args: {}
        bridge._captured_shader_identity_for_context = lambda *args: None
        bridge._matching_shader_aliases = lambda *args: []
        result = bridge._reverse_shader({"event_id": 41, "stage": "fragment", "text_mode": "preferred"})
        self.assertEqual(calls, ["DXBC / DXIL"])
        self.assertEqual(list(result["disassembly"]), ["DXBC / DXIL"])
        calls.clear()
        reflection.debugInfo = types.SimpleNamespace(files=[types.SimpleNamespace(filename="main.hlsl", contents="float4 MainPS() { return 1; }")])
        result = bridge._reverse_shader({"event_id": 41, "stage": "fragment", "text_mode": "preferred"})
        self.assertEqual(calls, [])
        self.assertTrue(result["textSelection"]["embeddedSourcePreferred"])
        bridge._reverse_shader({"event_id": 41, "stage": "fragment"})
        self.assertEqual(calls, ["AMD GCN ISA", "DXBC / DXIL", "DXC DXIL"])
        calls.clear()
        reflection.debugInfo = None
        controller.DisassembleShader = lambda pid, refl, target: calls.append(target) or (
            "error: target unavailable" if target == "DXBC / DXIL" else "valid instructions")
        result = bridge._reverse_shader({"event_id": 41, "stage": "fragment", "text_mode": "preferred"})
        self.assertEqual(calls, ["DXBC / DXIL", "DXC DXIL"])
        self.assertEqual(result["disassembly"]["DXC DXIL"], "valid instructions")

    def test_shader_info_fails_closed_when_multi_entry_pipeline_selection_is_unknown(self) -> None:
        bridge = load_bridge()
        stage_token = object()
        bridge._available_shader_stages = lambda names: [("fragment", stage_token)]
        bridge._set_frame_event = lambda ctrl, event_id, force: None
        bridge._get_pipeline_obj = lambda pipeline, ctrl: 17

        class Pipeline:
            def GetShader(self, stage):
                self.requested_stage = stage
                return 99

            def GetShaderEntryPoint(self, stage):
                self.entry_stage = stage
                return "not-one-of-the-reflected-entries"

        pipeline = Pipeline()

        class Controller:
            def GetPipelineState(self):
                return pipeline

            def GetShaderEntryPoints(self, shader):
                self.requested_shader = shader
                return [
                    types.SimpleNamespace(name="RayA", stage="RayGen"),
                    types.SimpleNamespace(name="RayB", stage="RayGen"),
                ]

        controller = Controller()
        bridge._replay = lambda callback: callback(controller)

        result = bridge._get_shader_info({"event_id": 41, "stage": "fragment"})

        self.assertEqual("shader-entry-point-ambiguous", result["errorId"])
        self.assertEqual(["RayA", "RayB"], [item["name"] for item in result["entryPoints"]])
        self.assertNotIn("disassembly", result)
        self.assertIn("arbitrary entry point", result["unknowns"][0])

    def test_pipeline_state_fails_closed_without_multi_entry_reflection_facts(self) -> None:
        bridge = load_bridge()
        stage_token = object()
        bridge._available_shader_stages = lambda names=None: [
            ("fragment", stage_token)
        ]
        bridge._get_pipeline_obj = lambda pipeline, ctrl: 17

        class Pipeline:
            def GetShader(self, stage):
                return 99

            def GetShaderEntryPoint(self, stage):
                return "MainA"

            def GetOutputTargets(self):
                return []

            def GetDepthTarget(self):
                return None

        pipeline = Pipeline()

        class Controller:
            def GetPipelineState(self):
                return pipeline

            def GetShaderEntryPoints(self, shader):
                return [
                    types.SimpleNamespace(name="MainA", stage="fragment"),
                    types.SimpleNamespace(name="MainA", stage="fragment"),
                ]

            def GetShader(self, *unused_arguments):
                raise AssertionError("must not reflect an arbitrary entry")

        result = bridge._pipeline_state_on_replay(Controller())
        shader = result["shaders"]["fragment"]

        self.assertEqual("shader-entry-point-ambiguous", shader["errorId"])
        self.assertEqual("ambiguous", shader["entryPointSelection"]["status"])
        for key in ("entryPoint", "textureCount", "cbufferCount", "samplerCount"):
            self.assertNotIn(key, shader)

    def test_pipeline_state_treats_unrepresentable_entry_stage_as_unknown(self) -> None:
        bridge = load_bridge()
        stage_token = object()
        bridge._available_shader_stages = lambda names=None: [
            ("fragment", stage_token)
        ]
        bridge._get_pipeline_obj = lambda pipeline, ctrl: 17

        class Entry:
            name = "ScreenProbeAdaptivePlacementMarkCS"

            @property
            def stage(self):
                raise ValueError("232 is not a valid ShaderStage")

        entry = Entry()
        reflection = types.SimpleNamespace(
            entryPoint="ScreenProbeAdaptivePlacementMarkCS", stage="fragment",
            readOnlyResources=[],
            constantBlocks=[], samplers=[])

        class Pipeline:
            def GetShader(self, stage):
                return 99

            def GetShaderEntryPoint(self, stage):
                return "main"

            def GetOutputTargets(self):
                return []

            def GetDepthTarget(self):
                return None

        class Controller:
            def GetPipelineState(self):
                return Pipeline()

            def GetShaderEntryPoints(self, shader):
                return [entry]

            def GetShader(self, pipeline_id, shader, selected):
                self.selected = selected
                return reflection

        controller = Controller()
        result = bridge._pipeline_state_on_replay(controller)
        shader = result["shaders"]["fragment"]

        self.assertIs(controller.selected, entry)
        self.assertEqual("selected", shader["entryPointSelection"]["status"])
        self.assertEqual(
            "unique-bound-resource-entry",
            shader["entryPointSelection"]["source"])
        self.assertIsNone(shader["entryPointSelection"]["entryPoints"][0]["stage"])
        self.assertEqual("ScreenProbeAdaptivePlacementMarkCS", shader["entryPoint"])

    def test_bound_textures_fails_closed_for_multi_entry_shader(self) -> None:
        bridge = load_bridge()
        stage_token = object()
        bridge._available_shader_stages = lambda names: [
            ("fragment", stage_token)
        ]
        bridge._set_frame_event = lambda ctrl, event_id, force: None
        bridge._get_pipeline_obj = lambda pipeline, ctrl: 17

        class Pipeline:
            def GetShader(self, stage):
                return 99

            def GetShaderEntryPoint(self, stage):
                return "not-reflected"

        pipeline = Pipeline()

        class Controller:
            def GetPipelineState(self):
                return pipeline

            def GetShaderEntryPoints(self, shader):
                return [
                    types.SimpleNamespace(name="MainA", stage="fragment"),
                    types.SimpleNamespace(name="MainB", stage="fragment"),
                ]

            def GetShader(self, *unused_arguments):
                raise AssertionError("must not reflect an arbitrary entry")

        bridge._replay = lambda callback: callback(Controller())
        result = bridge._get_bound_textures(
            {"event_id": 41, "stage": "fragment"})

        self.assertEqual("shader-entry-point-ambiguous", result["errorId"])
        self.assertEqual("ambiguous", result["entryPointSelection"]["status"])
        self.assertNotIn("role", result)

    def test_bound_texture_role_is_explicitly_low_confidence_heuristic(self) -> None:
        bridge = load_bridge()
        stage_token = object()
        bridge._available_shader_stages = lambda names: [
            ("fragment", stage_token)
        ]
        bridge._set_frame_event = lambda ctrl, event_id, force: None
        bridge._get_pipeline_obj = lambda pipeline, ctrl: 17

        entry = types.SimpleNamespace(name="MainPS", stage="fragment")
        reflection = types.SimpleNamespace(
            entryPoint="MainPS",
            stage="fragment",
            readOnlyResources=[types.SimpleNamespace(
                isTexture=True, name="AlbedoTexture", fixedBindNumber=0)],
        )

        class Pipeline:
            def GetShader(self, stage):
                return 99

            def GetShaderEntryPoint(self, stage):
                return "MainPS"

            def GetReadOnlyResources(self, stage):
                return [types.SimpleNamespace(resourceId=5)]

        pipeline = Pipeline()

        class Controller:
            def GetPipelineState(self):
                return pipeline

            def GetShaderEntryPoints(self, shader):
                return [entry]

            def GetShader(self, pipeline_id, shader, selected):
                self.selected = selected
                return reflection

            def GetResources(self):
                return [types.SimpleNamespace(resourceId=5, name="Albedo")]

            def GetTextures(self):
                return [types.SimpleNamespace(
                    resourceId=5, width=64, height=64, format="RGBA8")]

        bridge._replay = lambda callback: callback(Controller())
        result = bridge._get_bound_textures(
            {"event_id": 41, "stage": "fragment"})

        self.assertEqual(1, len(result))
        self.assertEqual(result[0]["role"], result[0]["roleInference"]["value"])
        self.assertEqual("low", result[0]["roleInference"]["confidence"])
        self.assertFalse(result[0]["roleInference"]["observed"])

    def test_analyze_pixel_restores_event_when_inspection_fails(self) -> None:
        bridge = load_bridge()

        class Controller:
            def __init__(self):
                self.events = []

            def SetFrameEvent(self, event_id, force):
                self.events.append((event_id, force))

        controller = Controller()
        replay_calls = []

        def replay(callback):
            replay_calls.append(callback)
            return callback(controller)

        bridge._ctx = types.SimpleNamespace(
            IsCaptureLoaded=lambda: True,
            CurEvent=lambda: 7,
        )
        bridge._replay = replay
        bridge._texture_info_on_replay = lambda ctrl, resource_id: {"id": resource_id}
        bridge._texture_minmax_on_replay = lambda ctrl, resource_id: {}

        def fail_inspection(*args, **kwargs):
            del args, kwargs
            raise RuntimeError("pixel history failed")

        bridge._inspect_pixel_object_on_replay = fail_inspection

        with self.assertRaisesRegex(RuntimeError, "pixel history failed"):
            bridge._analyze_pixel(
                {"texture_id": 99, "x": 1, "y": 2, "event_id": 41}
            )

        self.assertEqual(1, len(replay_calls))
        self.assertEqual([(41, True), (7, True)], controller.events)

    def test_composite_method_keeps_capture_session_guard(self) -> None:
        bridge = load_bridge()
        calls = []
        bridge._capture_session_id = "a" * 32
        bridge._synchronise_capture_lifecycle = lambda: None
        bridge._publish_console_event = lambda *args, **kwargs: None
        bridge.METHODS["inspect_event"] = lambda params: calls.append(params)

        response = bridge.handle_request(
            {
                "id": 17,
                "method": "inspect_event",
                "params": {
                    "event_id": 41,
                    "__replaylab_expected_capture_session_id": "b" * 32,
                },
            }
        )

        self.assertEqual([], calls)
        self.assertEqual(17, response["id"])
        self.assertIn("error", response)
        self.assertIn("capture session changed", response["error"]["message"])


if __name__ == "__main__":
    unittest.main()
