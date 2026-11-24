import ast
import base64
import importlib.util
import hashlib
import json
from pathlib import Path
import struct
import sys
import types

import pytest

from src.unity_fidelity_tools import asset_layout, trace_dependencies, write_document
from src.unity_layered_validation import compare_layers

BRIDGE = Path(__file__).resolve().parents[2] / "renderdoc_mcp_bridge"


def document_model():
    package = types.ModuleType("bread_document_test_package"); package.__path__ = [str(BRIDGE)]
    sys.modules[package.__name__] = package
    spec = importlib.util.spec_from_file_location(package.__name__ + ".document_content", BRIDGE / "document_content.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_unicode_writer_and_inert_markdown_table_diagram_reader(tmp_path):
    path = tmp_path / "说明.md"
    content = "# 渲染还原\n\n| 模块 | 状态 |\n| --- | --- |\n| 阴影 | 待修复 |\n\n```mermaid\nflowchart LR\nEID1267[原始光照] --> Unity[引擎输出]\n```\n"
    receipt = write_document(str(path), content)
    assert receipt["unicodeRoundTrip"] and receipt["questionMarkRuns"] == 0
    model = document_model().load_document(path)
    assert "渲染还原" in model["html"] and "<table>" in model["html"]
    assert model["diagrams"][0]["graph"]["edges"] == [{"source": "EID1267", "target": "Unity"}]
    assert not model["previewTruncated"]
    assert model["text"] == content


def test_html_reader_does_not_execute_scripts_or_load_remote_images_and_reports_lost_text(tmp_path):
    path = tmp_path / "report.html"
    path.write_text('<h1>???</h1><script>write_secret()</script><img src="https://host/image"><a href="javascript:run()">bad</a><a href="https://docs.example">docs</a>',encoding="utf-8")
    model = document_model().load_document(path)
    assert "write_secret" not in model["html"] and "javascript:" not in model["html"]
    assert "https://host/image" not in model["html"]
    assert 'href="https://docs.example"' in model["html"]
    assert model["diagnostics"]["questionMarkRuns"] == 1


def test_native_reader_and_vendored_parser_remain_python36_compatible():
    for path in [BRIDGE/"document_content.py",BRIDGE/"project_document_viewer.py",*(BRIDGE/"vendor").rglob("*.py")]:
        ast.parse(path.read_text(encoding="utf-8"),feature_version=(3,6))
    assert "Redistribution and use" in (BRIDGE/"vendor/mistune/LICENSE.py").read_text()


def test_dependency_trace_keeps_partial_writers_history_and_does_not_classify_3d_as_static():
    inspected=[]; usage_calls=[]
    def inspect(eid):
        inspected.append(eid)
        return {"textureInputs": [{"resourceId": 7, "kind":"srv"}] if eid==30 else []}
    def usage(rid):
        usage_calls.append(rid)
        return {"candidates":[{"eventId":10,"usage":"ColorTarget"},{"eventId":20,"usage":"CS_RWResource"},{"eventId":29,"usage":"PS_Resource"}]}
    graph=trace_dependencies([30,30],inspect,usage,lambda rid:{"d":16})
    assert set(inspected)=={10,20,30} and usage_calls==[7]
    assert [row["eventId"] for row in graph["resources"][0]["producerCandidates"]]==[20,10]
    assert "3D does not imply static" in graph["resources"][0]["staticness"]
    history=trace_dependencies([10],lambda eid:{"inputs":[{"resourceId":8}]} if eid==10 else {},
        lambda rid:{"candidates":[{"eventId":20,"usage":"CS_RWResource"}]},lambda rid:{"d":1})
    assert history["resources"][0]["historyUpdateCandidates"][0]["eventId"]==20
    assert set(history["events"])=={10,20}


def test_asset_layout_is_flat_semantic_shared_and_sampler_aware():
    base=dict(kind="texture",role="EID1267_BaseColor-12abcdef12345678",contentSha256="same",samplingIdentity={"srgb":True,"wrap":"Repeat"})
    rows=[dict(base,path="Assets/Old/EID1267/a.tga",eventIds=[1267]),dict(base,path="Assets/Old/EID1272/b.tga",eventIds=[1272]),
          dict(base,path="Assets/Old/EID1281/c.tga",eventIds=[1281],samplingIdentity={"srgb":False,"wrap":"Repeat"})]
    result=asset_layout(rows,"Assets/New")
    assert {r["target"] for r in result["moves"]}=={"Assets/New/Textures/BaseColor.tga","Assets/New/Textures/EID1281_BaseColor.tga"}
    assert len(result["textureAliases"])==1 and not result["collisions"]
    assert rows[0]["path"]=="Assets/Old/EID1267/a.tga"


def test_asset_layout_handles_saved_scene_and_required_code_without_extra_directories():
    rows = [dict(kind=kind, path="Assets/Staging/" + name, role=role)
        for kind, name, role in [("scene", "Effect.unity", "Effect"),
            ("shader", "Effect.shader", "Effect"), ("runtime", "Effect.cs", "EffectRenderer"),
            ("editor", "Inspector.cs", "EffectInspector")]]
    plan = asset_layout(rows, "Assets/Effect")
    assert {row["target"] for row in plan["moves"]} == {
        "Assets/Effect/Scenes/Effect.unity", "Assets/Effect/Shaders/Effect.shader",
        "Assets/Effect/Runtime/EffectRenderer.cs", "Assets/Effect/Editor/EffectInspector.cs"}
    assert not plan["textureAliases"] and not plan["collisions"]


def test_raw_float_extension_sidecar_roi_and_channel_errors_find_shared_module(tmp_path):
    left=tmp_path/"left.rgba32f";right=tmp_path/"right.rgba32f"
    left.write_bytes(struct.pack("<8f",0,0,0,1,2,4,6,1));right.write_bytes(struct.pack("<8f",0,0,0,1,3,4,6,1))
    for path in [left,right]:
        Path(str(path)+".json").write_text(json.dumps(dict(width=2,height=1,channels=4,encoding="float32le")))
    result=compare_layers([dict(name="indirect",module="SampleIndirect",category="native-engine",roi=[1,0,1,1],reference={"path":str(left)},rendered={"path":str(right)})])
    metrics=result["layers"][0]["metrics"]
    assert metrics["comparedComponents"]==4 and metrics["meanAbsoluteError"]==0.25
    assert metrics["componentsOverTolerance"]==1 and metrics["channelMeanSignedError"]==[1,0,0,0]
    assert result["firstDivergentLayer"]=="indirect" and "SampleIndirect" in result["sharedModuleGroups"]
    assert result["diagnosticsOnly"]


def test_event_texture_chunks_reuse_one_read_and_invalidate_for_cbuffer_change(monkeypatch):
    from test_bridge_composite_readonly import load_bridge
    bridge=load_bridge();monkeypatch.setitem(sys.modules,bridge.__name__,bridge)
    spec=importlib.util.spec_from_file_location("fidelity_channels",BRIDGE/"frame_flow_channels.py")
    channels=importlib.util.module_from_spec(spec);spec.loader.exec_module(channels)
    monkeypatch.setitem(sys.modules,bridge.__name__+".frame_flow_channels",channels)
    bridge._capture_session_id="session";bridge._request_capture_loaded=lambda:True
    bridge._request_visible_event_id=lambda default:2111;bridge._event_texture_raw_cache={}
    bridge._shared_shader_mutation_epoch=1;bridge._event_cbuffer_release_serial=0;bridge._event_cbuffer_snapshots={}
    bridge._find_action_by_event=lambda roots,eid:types.SimpleNamespace(eventId=eid)
    events=[];reads=[];bridge._set_frame_event=lambda ctrl,eid,force:events.append(eid)
    bridge._fmt_str=lambda fmt:"R8G8B8A8_UNorm";bridge.rd.Subresource=lambda *args:args
    texture=types.SimpleNamespace(resourceId=7,byteSize=32,mips=1,width=2,height=2,depth=2,type="Texture3D",format=None)
    ctrl=types.SimpleNamespace(GetRootActions=lambda:[],GetTextures=lambda:[texture],GetTextureData=lambda *args:(reads.append(True),bytes(range(32)))[1])
    bridge._replay=lambda callback:callback(ctrl)
    params=dict(event_id=30,resource_id="7",mip=0,length=4,expected_capture_session_id="session")
    assert base64.b64decode(bridge._get_event_texture_bytes(params)["base64"])==bytes(range(4))
    assert base64.b64decode(bridge._get_event_texture_bytes(dict(params,offset=4))["base64"])==bytes(range(4,8))
    assert len(reads)==1 and events==[30,2111]
    bridge._event_cbuffer_snapshots["buffer"]={"revision":2,"currentRawSha256":"changed"}
    bridge._get_event_texture_bytes(params)
    assert len(reads)==2 and events==[30,2111,30,2111]


@pytest.mark.parametrize("volume", [False, True])
def test_registered_texture_export_retains_all_mips_and_float_sidecars(tmp_path, monkeypatch, volume):
    from src.unity_restoration_tools import register_tools
    monkeypatch.setenv("BREAD_UNITY_RESTORATION_ROOT", str(tmp_path/"store"))
    calls=[]
    def call(name, parameters, **unused):
        assert name=="get_event_texture_bytes"
        calls.append(parameters)
        mip=parameters["mip"];size=max(1,2>>mip);depth=size if volume else 1
        raw=struct.pack("<"+"f"*(size*size*depth*4),*range(size*size*depth*4))
        offset=parameters["offset"];part=raw[offset:offset+17]
        return json.dumps(dict(base64=base64.b64encode(part).decode(),width=size,height=size,depth=depth,mips=2,
            dimension="Texture3D" if volume else "Texture2D",captureSessionId="session",mutationEpoch="unchanged",
            format="R32G32B32A32_FLOAT",length=len(raw),sha256=hashlib.sha256(raw).hexdigest()))
    host=types.SimpleNamespace(_active_live_context=lambda **kw:{"capture":{"captureSessionId":"session"}},
        _call=call,_ok_compact=json.dumps,_err=lambda error:json.dumps({"error":str(error)}))
    mcp=types.SimpleNamespace(tool=lambda:lambda function:function)
    functions=register_tools(mcp,host)
    name="export_unity_volume_texture" if volume else "export_unity_texture_data"
    result=json.loads(functions[name](30,"7",str(tmp_path/"evidence")))
    assert "error" not in result,result
    manifest=json.loads(Path(result["manifestPath"]).read_text())
    assert len(manifest["mips"])==2 and {row["mip"] for row in calls}=={0,1}
    assert all(row["expected_capture_session_id"]=="session" for row in calls)
    first=manifest["mips"][0]
    assert first["depth"]==(2 if volume else 1)
    assert first["encoding"]=="float32le" and first["channels"]==4
    if not volume:
        assert json.loads(Path(first["path"]+".json").read_text())["width"]==2
    # A repeated byte-identical export reuses evidence; it does not need a new ledger.
    assert "error" not in json.loads(functions[name](30,"7",str(tmp_path/"evidence")))


def test_dependency_inspection_failure_keeps_other_event_evidence():
    def inspect(eid):
        if eid==10:raise RuntimeError("unsupported replay stage")
        return {"eventId":eid,"textureInputs":[]}
    result=trace_dependencies([10,20],inspect,lambda rid:{},lambda rid:{})
    assert result["events"][20]["eventId"]==20
    assert result["events"][10]["inspectionError"]=="unsupported replay stage"
    assert result["limits"][0]["reason"]=="inspection-unavailable"


def test_dependency_trace_follows_utility_texture_copy_source():
    snapshots={30:{"textureInputs":[{"resourceId":7}]},20:{"textureInputs":[],"details":{"inputs":[{"resourceId":8,"role":"copy-source"}]}},10:{}}
    def usage(rid):
        return {"candidates":[{"eventId":20 if rid==7 else 10,"usage":"CopyDst" if rid==7 else "ColorTarget"}]}
    result=trace_dependencies([30],lambda eid:snapshots[eid],usage,lambda rid:{"d":1})
    assert set(result["events"])=={10,20,30}
    assert [row["resourceId"] for row in result["resources"]]==["7","8"]
