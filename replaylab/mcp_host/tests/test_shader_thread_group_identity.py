"""Only workgroup stages carry initialized native dispatch dimensions."""
import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.shader_workspace.abi import AbiValidationError, parse_shader_abi


BRIDGE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge" / "__init__.py"


def namespace():
    functions = {"_native_enum_text", "_thread_group_size", "_compiled_shader_interface",
                 "_normalise_stage_name", "_known_stage_name"}
    tree = ast.parse(BRIDGE.read_text(encoding="utf8"))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in functions]
    spec=importlib.util.spec_from_file_location('thread_group_dxbc_layout',BRIDGE.parent/'dxbc_cbuffer_layout.py')
    layout=importlib.util.module_from_spec(spec);spec.loader.exec_module(layout)
    values = {"_dxbc_cbuffer_layout":layout,
              "_shader_raw_bytes":lambda reflection:bytes(getattr(reflection,'rawBytes',b'')),
              "_STAGE_ALIASES": {"pixel": "fragment"},
              "_SHADER_STAGE_NAMES": {"vertex", "hull", "domain", "geometry", "fragment", "compute", "task", "mesh"},
              "_safe_int": lambda value, fallback: int(value)}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(BRIDGE), "exec"), values)
    return values


def reflection(stage, dimensions):
    return SimpleNamespace(stage="ShaderStage." + stage, dispatchThreadsDimension=dimensions,
                           entryPoint="main", encoding="ShaderEncoding.DXBC", outputTopology="Topology.Unknown")


@pytest.mark.parametrize("stage", ["Vertex", "Hull", "Domain", "Geometry", "Pixel"])
def test_graphics_catalog_interface_does_not_publish_uninitialized_compute_dimensions(stage):
    ns = namespace()
    # The native DX11 capture exposed these nonzero values for a pixel Shader.
    result = ns["_compiled_shader_interface"](reflection(stage, [7200, 614, 227643840]))
    assert result["threadGroupSize"] == []
    assert parse_shader_abi(result).thread_group_size == ()


def test_noncompute_dimensions_are_not_read_at_all():
    class GraphicsReflection:
        stage = "ShaderStage.Vertex"

        @property
        def dispatchThreadsDimension(self):
            raise AssertionError("compute-only native field was read")

    assert namespace()["_thread_group_size"](GraphicsReflection()) == []


def test_compute_dimensions_remain_exact_and_go_through_existing_abi_validation():
    ns = namespace()
    result = ns["_compiled_shader_interface"](reflection("Compute", [8, 4, 1]))
    assert result["threadGroupSize"] == [8, 4, 1]
    assert parse_shader_abi(result).thread_group_size == (8, 4, 1)
    invalid = ns["_compiled_shader_interface"](reflection("Compute", [8, 0, 1]))
    assert invalid["threadGroupSize"] == [8, 0, 1]
    with pytest.raises(AbiValidationError, match="threadGroupSize"):
        parse_shader_abi(invalid)


@pytest.mark.parametrize("stage", ["Task", "Mesh"])
def test_other_workgroup_dimensions_are_preserved_without_expanding_abi_support(stage):
    result = namespace()["_compiled_shader_interface"](reflection(stage, [32, 1, 1]))
    assert result["threadGroupSize"] == [32, 1, 1]
    # Keep the existing Host capability gate; serialization must not hide facts
    # to make a currently unsupported interface appear compatible.
    with pytest.raises(AbiValidationError, match="threadGroupSize"):
        parse_shader_abi(result)
