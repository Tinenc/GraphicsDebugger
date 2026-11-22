"""Instant diagnostics must accept Vulkan HLSL without hiding value typos."""
import ast
from pathlib import Path
import re

import pytest


@pytest.fixture(scope="module")
def diagnose():
    path = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge/project_shader_workspace.py"
    tree = ast.parse(path.read_text(encoding="utf8"))
    nodes = [n for n in tree.body if
             isinstance(n, ast.FunctionDef) and (n.name.startswith("_hlsl_") or n.name == "hlsl_local_diagnostics")
             or isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id.startswith("_HLSL_") for t in n.targets)]
    scope = {"re": re}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), scope)
    return scope["hlsl_local_diagnostics"]


def test_vulkan_attributes_in_struct_and_resource_declarations(diagnose):
    source = '''
struct Inputs {
    [[vk::location(0)]] float2 uv : TEXCOORD0;
    [[vk::location(1)]] float3 normal : TEXCOORD1;
};
[[vk::binding(0, 1)]] Texture2D<float4> image;
[[vk::binding(1, 1)]] SamplerState samplerState;
float4 main(Inputs input) : SV_Target0 { return image.Sample(samplerState, input.uv); }
'''
    assert diagnose(source) == []


def test_attribute_arguments_and_value_named_vk_are_still_checked(diagnose):
    source = '''struct Inputs { [[vk::location(missingLocation)]] float2 uv; };
float4 main() : SV_Target0 { return vk + missingColor; }'''
    errors = diagnose(source)
    assert [row["text"] for row in errors] == ["Undefined identifier 'missingLocation'",
        "Undefined identifier 'vk'", "Undefined identifier 'missingColor'"]
    assert [(row["line"], row["column"]) for row in errors] == [(1, 32), (2, 37), (2, 42)]


def test_current_bound_compiler_replaces_provisional_local_errors():
    from types import SimpleNamespace
    path=Path(__file__).resolve().parents[2]/"renderdoc_mcp_bridge/project_shader_workspace.py"
    tree=ast.parse(path.read_text(encoding='utf8'))
    method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_current_problems')
    scope={};exec(compile(ast.Module(body=[method],type_ignores=[]),str(path),'exec'),scope)
    local=[{'text':"Undefined identifier 'COMPILER_DEFINED_MACRO'",'line':1}]
    self=SimpleNamespace(_local_analysis={'a':{'textRevision':7,'diagnostics':local}},
        _live_check={'a':{'textRevision':7,'ok':True,'diagnostics':[]}})
    doc={'dirty':True,'workspaceId':'a','textRevision':7}
    assert scope['_current_problems'](self,doc,{})==[]
    real=[{'text':'Actual compiler diagnostic','line':2}]
    self._live_check['a'].update(ok=False,diagnostics=real)
    assert scope['_current_problems'](self,doc,{})==real
    self._live_check['a']['textRevision']=6
    assert scope['_current_problems'](self,doc,{})==local
