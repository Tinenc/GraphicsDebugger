"""Saved, unsaved and instrumented source share the bound compiler recipe."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
from unittest import mock

import pytest

from src.shader_workspace import workspace as ws
from src.shader_workspace.compiler_contract import SCHEMA
from tests.test_native_shader_compile import native_workspace

BRIDGE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge"


def load(name):
    spec = importlib.util.spec_from_file_location("contract_test_"+name, BRIDGE/(name+".py"))
    module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


portable = load("shader_visual_compile")
bound = load("shader_bound_compile")


def seal(recipe):
    recipe.pop("identitySha256", None)
    recipe["identitySha256"] = hashlib.sha256(json.dumps(recipe,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf8")).hexdigest()
    return recipe


def test_saved_check_and_preview_use_the_same_legacy_binding(native_workspace):
    service,root=native_workspace
    source=(root/"source/shader.hlsl").read_text()
    manifest=json.loads((root/"manifest.json").read_text())
    original=(root/"manifest.json").read_bytes()
    recipe=service.compiler_contract(root,manifest["workspaceId"],manifest["baseline"]["rawSha256"])
    assert (root/"manifest.json").read_bytes()==original
    assert recipe["backend"]=="bound-route"
    runner=portable.Compiler(str(root))
    preview=bound.BoundCompiler(runner).compile(recipe,source,recipe["entryPoint"])
    assert service.compile_check(root,source)=={"ok":True,"diagnostics":[]}
    assert (root/"manifest.json").read_bytes()==original
    service._native_compile_provider=mock.Mock(side_effect=AssertionError("independent native rule"))
    saved=service.compile_for_live_edit(root)
    assert saved["rawSha256"]==hashlib.sha256(preview["bytes"]).hexdigest()
    assert not service._native_compile_provider.called
    again=service.compile_for_live_edit(root)
    assert again["compilationCacheHit"] is True
    assert (root/"source/shader.hlsl").read_text()==source


def test_contract_binds_original_and_current_tool_files(native_workspace):
    service,root=native_workspace
    manifest=json.loads((root/"manifest.json").read_text())
    args=(root,manifest["workspaceId"],manifest["baseline"]["rawSha256"])
    recipe=service.compiler_contract(*args)
    compiler=bound.BoundCompiler(portable.Compiler(str(root)))
    first=compiler.identity(recipe)
    changed=copy.deepcopy(recipe);changed['steps'][0]['arguments'].append('-DCHANGED=1');seal(changed)
    assert compiler.identity(changed)!=first
    dependency=Path(recipe["steps"][0]["files"][-1][0])
    dependency.write_bytes(dependency.read_bytes()+b"\n# changed\n")
    with pytest.raises(RuntimeError,match="compiler changed"):
        compiler.identity(recipe)
    with pytest.raises(ws.ManifestValidationError,match="identity changed"):
        service.compiler_contract(root,"0"*32,manifest["baseline"]["rawSha256"])


def test_legacy_compiler_refresh_preserves_source_and_rebinds_only_once(native_workspace):
    service, root = native_workspace
    service.compile_for_live_edit(root)
    before = (root / "manifest.json").read_bytes()
    manifest = json.loads(before)
    source = (root / "source/shader.hlsl").read_bytes()
    old_blob = root / manifest["latestCompiled"]["blobPath"]
    old_bytes = old_blob.read_bytes()
    registry_path = service.registry.path
    registry = json.loads(registry_path.read_text())
    registry["tools"][manifest["tools"]["compiler"]["toolId"]]["compile"]["timeoutSeconds"] += 1
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    current = ws.ShaderWorkspaceService(registry_path)
    args = (root, manifest["workspaceId"], manifest["baseline"]["rawSha256"])
    current.compiler_contract(*args)
    repaired = json.loads((root / "manifest.json").read_bytes())
    assert repaired["workspaceId"] == manifest["workspaceId"]
    assert repaired["revision"] == manifest["revision"]
    assert repaired["tools"]["decompiler"] == manifest["tools"]["decompiler"]
    assert repaired["latestCompiled"] is None
    assert (root / "source/shader.hlsl").read_bytes() == source
    assert old_blob.read_bytes() == old_bytes
    archive = next((root / "toolchain-history").iterdir())
    assert (archive / "manifest.json").read_bytes() == before
    current.compiler_contract(*args)
    assert len(list((root / "toolchain-history").iterdir())) == 1
    assert current.compile_for_live_edit(root)["manualFastCompile"]


def test_preview_identity_tracks_loaded_compiler_library(tmp_path,monkeypatch):
    program=tmp_path/('dxc.exe' if os.name=='nt' else 'dxc')
    library=tmp_path/'compiler-library.bin'
    program.write_bytes(b'compiler');library.write_bytes(b'library-v1')
    runner=portable.Compiler(str(tmp_path))
    monkeypatch.setattr(runner,'_cached_dependencies',lambda *args:[str(program),str(library)])
    recipe=seal(dict(schema=SCHEMA,backend='bound-route',steps=[dict(program=str(program),
        files=[[str(program),hashlib.sha256(program.read_bytes()).hexdigest()]])]))
    compiler=bound.BoundCompiler(runner);first=compiler.identity(recipe)
    library.write_bytes(b'updated-library-version')
    assert compiler.identity(recipe)!=first


def test_multi_hop_uses_physical_spirv_entry():
    name=b'Exported\0';name+=b'\0'*(-len(name)%4)
    words=[0x07230203,0x10000,0,2,0,(3+len(name)//4)<<16|15,4,1]
    raw=struct.pack('<%dI'%len(words),*words)+name
    assert bound._spirv_entry(raw,'SourcePS','fragment')=='Exported'


@pytest.mark.parametrize("api,output",[("d3d11","dxbc"),("d3d12","dxil"),("vulkan","spirv"),("opengl","opengl-spirv")])
def test_contract_keeps_target_parameters_and_legacy_paths(native_workspace,api,output):
    service,root=native_workspace
    manifest=json.loads((root/"manifest.json").read_text())
    # Test projection independently of manifest provenance admission, which is
    # already exercised by compiler_contract and native/route workspace suites.
    from src.shader_workspace.compiler_contract import project
    manifest['capture']['api']=api;manifest['targetProfile']['profile']='ps_6_0' if api=='d3d12' else 'ps_5_0'
    recipe=project(service,root,manifest)
    assert recipe['values']['target_api']==api
    assert recipe['values']['workspace_dir']==str(root)
    assert recipe['values']['target_profile']==manifest['targetProfile']['profile']
    assert recipe['originalRawSha256']==manifest['baseline']['rawSha256']
    assert recipe['steps'][0]['arguments']==list(service._bound_operation(manifest,'compiler','compile').arguments)


def dxc_recipe(tmp_path,monkeypatch):
    executable=os.environ.get('BREAD_VISUAL_DXC') or shutil.which('dxc')
    if not executable:pytest.skip('No native DXC installed')
    monkeypatch.setenv('PATH',str(Path(executable).parent)+os.pathsep+os.environ.get('PATH',''))
    runner=portable.Compiler(str(tmp_path/'app'))
    identity=runner.identity('hlsl','vulkan','ps_6_0')
    recipe=dict(schema=SCHEMA,backend='bound-route',workspaceId='a'*32,originalRawSha256='b'*64,
        stage='fragment',entryPoint='main',language='hlsl',values={},flags=[],outputFormat='spirv',environment={},
        steps=[dict(program=str(Path(executable).resolve()),files=identity['files'],timeoutSeconds=90,
            inputFormat='hlsl',outputFormat='spirv',toolId='test-dxc',arguments=['-spirv','-HV','2018','-T','ps_6_0',
                '-E','{entry_point}','-DVALUE=0.25','-Zpr','-Fo','{output}','{input}'])])
    if os.name=='nt':recipe['environment']['SystemRoot']=os.environ['SystemRoot']
    return runner,seal(recipe)


def test_real_preprocess_compile_share_macro_version_and_layout(tmp_path,monkeypatch):
    runner,recipe=dxc_recipe(tmp_path,monkeypatch)
    compiler=bound.BoundCompiler(runner)
    source='float4 main():SV_Target0 { bool2 v=bool2(true,false); float2 a=v?VALUE.xx:0.0.xx; return float4(a,0,1); }'
    assert compiler.needs_preprocess(recipe)
    preprocessed=compiler.preprocess(recipe,source,'main',None,portable._mirror_source)
    assert 'VALUE' not in preprocessed
    assert compiler.compile(recipe,preprocessed,'main')['encoding']=='SPIRV'
    include_source='#include "nested/shared.hlsli"\n'+source.replace('return float4(a,0,1);','return Color(a);')
    snapshot={'rootName':'main.hlsl','editedName':'main.hlsl','files':{'main.hlsl':include_source,
        'nested/shared.hlsli':'float4 Color(float2 a){return float4(a,VALUE,1);}'},'blockedIncludes':{}}
    expanded=compiler.preprocess(recipe,include_source,'main',snapshot,portable._mirror_source)
    assert '#include' not in expanded and 'VALUE' not in expanded
    assert compiler.compile(recipe,expanded,'main')['encoding']=='SPIRV'
    modern=copy.deepcopy(recipe);modern['steps'][0]['arguments'][2]='2021';seal(modern)
    with pytest.raises(RuntimeError,match='must be scalar'):
        compiler.compile(modern,source,'main')


def test_real_glslang_preprocess_and_compile_keep_hlsl_mode(tmp_path):
    executable=os.environ.get('BREAD_VISUAL_GLSLANG') or shutil.which('glslangValidator')
    if not executable:pytest.skip('No native glslang installed')
    executable=str(Path(executable).resolve())
    recipe=seal(dict(schema=SCHEMA,backend='bound-route',steps=[dict(program=executable,
        arguments=['-D','-V','--target-env','vulkan1.2','-S','frag','-e','{entry_point}','-DVALUE=0.25',
            '--hlsl-iomap','--hlsl-offsets','--hlsl-dx-position-w','-o','{output}','{input}'],
        files=[[executable,hashlib.sha256(Path(executable).read_bytes()).hexdigest()]],
        timeoutSeconds=90,inputFormat='hlsl',outputFormat='spirv',toolId='glslang-hlsl')],
        values={},environment={},outputFormat='spirv'))
    compiler=bound.BoundCompiler(portable.Compiler(str(tmp_path)))
    source='float4 main():SV_Target0 { bool2 v=bool2(true,false); return float4(v?VALUE.xx:0.0.xx,0,1); }'
    preprocessed=compiler.preprocess(recipe,source,'main',None,portable._mirror_source)
    assert 'VALUE' not in preprocessed
    assert compiler.compile(recipe,preprocessed,'main')['encoding']=='SPIRV'


@pytest.mark.skipif(os.name!='nt',reason='D3DCompiler runs on a Windows replay host')
def test_native_dxbc_preprocess_uses_the_pinned_dll():
    dll=BRIDGE.parents[1]/'x64/Development/d3dcompiler_47.dll'
    if not dll.is_file():dll=BRIDGE.parents[1]/'x64/Development/replaylab/tools/d3dcompiler_47.dll'
    if not dll.is_file():pytest.skip('D3DCompiler DLL is not installed')
    source='#define Value(x) ((x)*0.25)\nfloat4 main():SV_Target0{return Value(2);}'
    text=bound._d3d_preprocess(str(dll),source,'input.hlsl')
    assert 'Value(' not in text and '0.25' in text
