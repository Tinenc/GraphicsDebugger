"""Raster and dispatch A/B/A must observe the action's actual output domain."""
import importlib.util
from pathlib import Path
import struct
from types import SimpleNamespace as NS

import pytest

from tests.test_shader_numeric_validation import FakeBridge, native_blob, validation, packed
from tests.test_shader_storage_validation import bridge as storage_bridge
from tests.test_bridge_resource_export import load_resource_export


@pytest.mark.parametrize("api", ["d3d11", "d3d12", "vulkan", "opengl"])
@pytest.mark.parametrize("stage", ["hull", "domain", "geometry", "task", "mesh"])
def test_raster_stages_use_full_replay_and_stability(api, stage):
    bridge = FakeBridge(existing=True)
    bridge.params["stage"] = stage
    bridge._available_shader_stages = lambda names: [(name, name) for name in names]
    bridge._shader_api_token = lambda _: api
    pipeline = bridge.ctrl.GetPipelineState()
    pipeline.GetShaderReflection = lambda _: None
    bridge.ctrl.GetPipelineState = lambda: pipeline
    bridge.ctrl.ComparePixelShaderPair = lambda *args: pytest.fail("PS pair cannot validate another stage")
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"] and result["stage"] == stage
    assert result["baselineStability"]["checked"]
    assert bridge.ctrl.observe_count == 3
    assert bridge.ctrl.mapping == 999 and bridge.ctrl.event_id == 17


@pytest.mark.parametrize("api", ["d3d11", "d3d12", "vulkan", "opengl"])
def test_compute_compares_uav_bytes_and_ignores_old_render_targets(storage_bridge, api):
    bridge = storage_bridge
    bridge.params["stage"] = "compute"
    bridge._available_shader_stages = lambda names: [("compute", "compute")]
    bridge._shader_api_token = lambda _: api
    bridge.ctrl.GetTextureData = lambda *args: pytest.fail("A dispatch cannot validate stale RTV data")
    result = validation.compare_candidates(bridge, bridge.params)
    assert result["accepted"] and result["observationScope"] == "dispatch-storage"
    assert result["baselineStability"]["checked"] and result["sampleCount"] == 4
    bridge.ctrl.GetBufferData = lambda *args: b"abce" if bridge.ctrl.targets.get(bridge.ctrl.mapping) == native_blob("candidate") else b"abcd"
    result = validation.compare_candidates(bridge, bridge.params)
    assert not result["accepted"] and result["replayRestored"]


def test_compute_without_observable_outputs_cannot_pass():
    bridge = FakeBridge()
    bridge.params["stage"] = "compute"
    bridge._available_shader_stages = lambda _: [("compute", "compute")]
    result = validation.compare_candidates(bridge, bridge.params)
    assert not result["accepted"] and "no observable output" in result["reason"]


@pytest.mark.parametrize("flag,expected", [(1, ["vertex", "fragment"]), (2, ["compute"]), (4, ["task", "mesh", "fragment"]), (8, ["raygen", "miss"])])
def test_export_excludes_idle_shader_pipelines(flag, expected):
    module = load_resource_export()
    rd = NS(ActionFlags=NS(Drawcall=1, Dispatch=2, MeshDispatch=4, DispatchRay=8))
    stages = [(name, name) for name in ["vertex", "task", "mesh", "fragment", "compute", "raygen", "miss"]]
    assert [name for name, _ in module._action_stages(NS(flags=flag), rd, stages)] == expected


def load_vertex():
    path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge/shader_vertex_validation.py"
    spec = importlib.util.spec_from_file_location("bread_vertex_validation_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("stage,model", [("vertex", 0), ("compute", 5), ("task", 5267), ("task", 5364),
    ("mesh", 5268), ("mesh", 5365), ("raygen", 5313), ("intersection", 5314), ("anyhit", 5315),
    ("closesthit", 5316), ("miss", 5317), ("callable", 5318)])
def test_bound_compiler_resolves_all_spirv_execution_models(stage, model):
    path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge/shader_bound_compile.py"
    spec = importlib.util.spec_from_file_location("bread_bound_compile_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    words = [0x07230203, 0x10600, 0, 20, 0, (5 << 16) | 15, model, 1,
             int.from_bytes(b"main", "little"), 0]
    raw = struct.pack("<%dI" % len(words), *words)
    assert module._spirv_entry(raw, "main", stage) == "main"
    with pytest.raises(RuntimeError, match="missing or ambiguous"):
        module._spirv_entry(raw, "main", "fragment")


def test_vertex_readback_ignores_padding_and_preserves_integer_channels():
    module = load_vertex()
    signature = [NS(stream=0, systemValue="Undefined", varType="UInt", compCount=1),
                 NS(stream=0, systemValue="Position", varType="Float", compCount=3)]
    pipeline = NS(GetShaderReflection=lambda _: NS(outputSignature=signature), MultiviewBroadcastCount=lambda: 1,
        HasAlignedPostVSData=lambda _: True, IsRestartEnabled=lambda: False)
    mesh = NS(vertexResourceId=100, indexResourceId=0, vertexByteOffset=0, vertexByteStride=16,
        numIndices=2, topology="TriangleList")
    raw = struct.pack("<3fI3fI", 1, 2, 3, 0xffffffff, 4, 5, 6, 5)
    ctrl = NS(GetPipelineState=lambda: pipeline,
        GetRootActions=lambda: [NS(eventId=50, flags=2, numInstances=1, children=[])],
        GetPostVSData=lambda *args: mesh, GetBufferData=lambda *args: raw)
    bridge = NS(rd=NS(ShaderStage=NS(Vertex="vertex"), MeshDataStage=NS(VSOut="vs")), _resource_id_int=int)
    outputs = module.outputs(bridge, ctrl, 50, 1024, 1024)
    assert outputs[0][1] == packed("f", 1, 2, 3, 4, 5, 6)
    assert outputs[1][0]["componentType"] == "UInt"
    assert outputs[1][1] == packed("I", 0xffffffff, 5)


@pytest.mark.parametrize("flags,instances,expected", [
    (2, 0, 1), (2 | 0x020000, 0, 0), (2 | 0x020000, 2, 2),
    (2 | 0x080000, 0, 0), (2 | 0x020000 | 0x080000, 2, 2),
])
def test_vertex_numeric_readback_never_invents_instanced_work(flags, instances, expected):
    module = load_vertex()
    calls = []
    signature = [NS(stream=0, systemValue="Position", varType="Float", compCount=4)]
    pipeline = NS(GetShaderReflection=lambda _: NS(outputSignature=signature), MultiviewBroadcastCount=lambda: 1,
        HasAlignedPostVSData=lambda _: True, IsRestartEnabled=lambda: False)
    mesh = NS(vertexResourceId=100, indexResourceId=0, vertexByteOffset=0, vertexByteStride=16,
        numIndices=1, topology="TriangleList")
    def post_vs(instance, view, stage):
        calls.append(instance)
        return mesh
    ctrl = NS(GetPipelineState=lambda: pipeline,
        GetRootActions=lambda: [NS(eventId=50, flags=flags, numInstances=instances, children=[])],
        GetPostVSData=post_vs, GetBufferData=lambda *args: packed("f", 1, 2, 3, 1))
    bridge = NS(rd=NS(ShaderStage=NS(Vertex="vertex"), MeshDataStage=NS(VSOut="vs")), _resource_id_int=int)
    if expected:
        assert len(module.outputs(bridge, ctrl, 50, 1024, 1024)) == expected
    else:
        with pytest.raises(ValueError, match="zero or unresolved"):
            module.outputs(bridge, ctrl, 50, 1024, 1024)
    assert calls == list(range(expected))


def test_vertex_value_difference_cannot_hide_behind_identical_render_targets(monkeypatch):
    bridge = FakeBridge(existing=True)
    bridge.params["stage"] = "vertex"
    bridge._available_shader_stages = lambda names: [(name, name) for name in names]
    pipeline = bridge.ctrl.GetPipelineState()
    pipeline.GetShaderReflection = lambda _: None
    bridge.ctrl.GetPipelineState = lambda: pipeline
    bridge.ctrl.output_values = {}
    def outputs(*args):
        desc = {"kind": "vertex-channel", "componentType": "Float", "componentByteWidth": 4,
            "componentCount": 1, "formatType": "Regular", "width": 1, "height": 1}
        return [(desc, packed("f", 2 if bridge.ctrl.mapping == 202 else 1))]
    monkeypatch.setattr(validation, "_vertex_module", lambda: NS(outputs=outputs))
    result = validation.compare_candidates(bridge, bridge.params)
    assert not result["accepted"] and result["replayRestored"]
    assert result["metrics"]["outputs"][-1]["output"]["kind"] == "vertex-channel"
