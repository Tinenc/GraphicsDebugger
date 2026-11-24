"""Exercise native zero-valued API enums at the Bridge's actual identity boundaries."""
import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


BRIDGE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "__init__.py"
_spec = importlib.util.spec_from_file_location('native_enum_dxbc_layout', BRIDGE.parent/'dxbc_cbuffer_layout.py')
_layout = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_layout)


class NativeEnum(int):
    def __new__(cls, value, spelling):
        instance = int.__new__(cls, value)
        instance.spelling = spelling
        return instance

    def __str__(self):
        return self.spelling


def namespace(api, renderer=None):
    names = {"_native_enum_text", "_physical_enum", "_shader_api_token",
             "_project_shader_index_api", "_shader_encoding_token",
             "_capture_identity_on_ui", "_compiled_shader_interface"}
    nodes = [node for node in ast.parse(BRIDGE.read_text(encoding="utf8")).body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    props = SimpleNamespace(pipelineType=api, localRenderer=renderer)
    values = {"_dxbc_cbuffer_layout": _layout,
              "_shader_raw_bytes": lambda reflection: bytes(getattr(reflection, 'rawBytes', b'')),
              "_ctx": SimpleNamespace(IsCaptureLoaded=lambda: True, APIProps=lambda: props,
                IsCaptureLocal=lambda: True, GetCaptureFilename=lambda: "C:/capture/triangle.rdc"),
              "_capture_session_id": "a" * 32, "_symbol_capture_snapshot": {},
              "_known_stage_name": lambda reflection: "fragment",
              "_thread_group_size": lambda reflection: []}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(BRIDGE), "exec"), values)
    return values, SimpleNamespace(GetAPIProperties=lambda: props)


@pytest.mark.parametrize("value,name,token", [(0, "D3D11", "d3d11"), (1, "D3D12", "d3d12"),
    (2, "OpenGL", "opengl"), (3, "Vulkan", "vulkan")])
def test_native_api_survives_context_capture_index_and_compile_route(value, name, token):
    api = NativeEnum(value, "GraphicsAPI." + name)
    ns, controller = namespace(api, api)
    identity = ns["_capture_identity_on_ui"]()
    assert identity["api"] == "GraphicsAPI." + name
    assert identity["renderer"] == "GraphicsAPI." + name
    assert ns["_shader_api_token"](controller) == token
    assert ns["_project_shader_index_api"](api) == token
    assert ns["_project_shader_index_api"](identity["api"]) == token


def test_absent_api_never_falls_back_to_d3d11():
    ns, controller = namespace(None)
    assert ns["_capture_identity_on_ui"]()["api"] == ""
    assert ns["_shader_api_token"](controller) == ""
    assert ns["_project_shader_index_api"](None) == ""


@pytest.mark.parametrize("value,name", [(0, "Unknown"), (1, "DXBC"), (2, "GLSL"), (6, "DXIL")])
def test_native_shader_encoding_preserves_zero_unknown_without_inventing_a_format(value, name):
    ns, _ = namespace(None)
    encoding = NativeEnum(value, "ShaderEncoding." + name)
    reflection = SimpleNamespace(encoding=encoding, entryPoint="main",
                                 outputTopology=NativeEnum(0, "Topology.Unknown"))
    interface = ns["_compiled_shader_interface"](reflection)
    assert interface["encoding"] == "ShaderEncoding." + name
    assert interface["outputTopology"] == "Topology.Unknown"
    assert ns["_shader_encoding_token"](encoding) == name.lower()


def test_catalog_and_candidate_agree_on_native_zero_output_topology():
    # Exercise the catalog's actual expression: GLSL has no container-derived
    # baseline that could mask a lost zero-valued Topology.Unknown.
    tree = ast.parse(BRIDGE.read_text(encoding="utf8"))
    details = next(node.value for node in ast.walk(tree)
                   if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                   and any(isinstance(target, ast.Name) and target.id == "shader_details"
                           for target in node.targets))
    topology = next(value for key, value in zip(details.keys, details.values)
                    if isinstance(key, ast.Constant) and key.value == "outputTopology")
    ns, _ = namespace(None)
    reflection = SimpleNamespace(encoding=NativeEnum(2, "ShaderEncoding.GLSL"),
                                 entryPoint="main", outputTopology=NativeEnum(0, "Topology.Unknown"))
    ns["reflection"] = reflection
    baseline = eval(compile(ast.Expression(topology), str(BRIDGE), "eval"), ns)
    candidate = ns["_compiled_shader_interface"](reflection)["outputTopology"]
    assert baseline == candidate == "Topology.Unknown"
