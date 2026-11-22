"""Create an isolated real-Editor smoke project; never opens an existing project.

Run from mcp_host with the repository virtualenv and --editor <Unity executable>.
The synthetic capture is explicitly labelled; this validates infrastructure,
domain reload, persistent binding, pipeline preservation and render evidence,
not reconstruction of a game's rendering algorithm.
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import shutil
import struct
import tempfile

from src.unity_restore import prepare_unity_import
from tests.test_unity_restore import finalized_package, SMOKE_URP_SHADER


BUILTIN_SHADER = '''Shader "Bread/SmokeBuiltin" {
Properties { _BaseColor("Color", Color) = (1,1,1,1) }
SubShader { Pass { Name "Forward" Cull Off
CGPROGRAM
#pragma vertex Vert
#pragma fragment Frag
#include "UnityCG.cginc"
#include "Common.hlsl"
float4 _BaseColor;
float4 Vert(float4 position:POSITION):SV_POSITION { return UnityObjectToClipPos(position); }
float4 Frag():SV_Target { return CapturedColor(_BaseColor); }
ENDCG } } }
'''

FEATURE = '''using Bread;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.Rendering.RenderGraphModule;
using UnityEngine.Rendering.Universal;
public sealed class BreadSmokeFeature : ScriptableRendererFeature {
 sealed class Pass : ScriptableRenderPass {
  sealed class Data { public Camera camera; public RenderTexture target; }
  RTHandle retained;
  public void Release(){if(retained!=null){var texture=retained.rt;retained.Release();Object.DestroyImmediate(texture);retained=null;}}
  public override void RecordRenderGraph(RenderGraph graph, ContextContainer frame) {
   var view=frame.Get<UniversalCameraData>(); var resources=frame.Get<UniversalResourceData>();
   if(retained==null || retained.rt.width!=view.cameraTargetDescriptor.width || retained.rt.height!=view.cameraTargetDescriptor.height){
    Release(); var texture=new RenderTexture(view.cameraTargetDescriptor.width,view.cameraTargetDescriptor.height,0,RenderTextureFormat.ARGBFloat,RenderTextureReadWrite.Linear);
    texture.name="Bread smoke retained output";texture.Create();retained=RTHandles.Alloc(texture);
   }
   var output=graph.ImportTexture(retained);
   using(var builder=graph.AddRasterRenderPass<Data>("Bread smoke feature",out var data)) {
    data.camera=view.camera; data.target=retained.rt;
    builder.SetRenderAttachment(resources.activeColorTexture,0,AccessFlags.Write);
    builder.SetRenderAttachment(output,1,AccessFlags.Write);
    builder.SetGlobalTextureAfterPass(output,Shader.PropertyToID("_BreadSmokeOutput"));
    builder.AllowPassCulling(false);
    builder.SetRenderFunc((Data pass,RasterGraphContext ctx)=> {
     ctx.cmd.ClearRenderTarget(RTClearFlags.Color,new Color(0.125f,0.25f,0.5f,1),1,0);
     BreadCapturePassEvidence.Record("smoke-feature",pass.camera,"Runtime/BreadSmokeFeature.cs",new string[0],new[]{"smoke-global"},pass.target,
       "renderer-feature","BreadSmokeFeature.Pass.RecordRenderGraph","AfterRenderingTransparents","smoke-global","_BreadSmokeOutput");
    });
   }
  }
 }
 Pass pass;
 public override void Create(){pass?.Release();pass=new Pass{renderPassEvent=RenderPassEvent.AfterRenderingTransparents};}
 protected override void Dispose(bool disposing){pass?.Release();}
 public override void AddRenderPasses(ScriptableRenderer renderer,ref RenderingData data){renderer.EnqueuePass(pass);}
}
'''


def prepare(editor: Path) -> Path:
    repository = Path(__file__).resolve().parents[2]
    project = Path(tempfile.mkdtemp(prefix="bread-unity-editor-smoke-"))
    (project / "Assets/Editor").mkdir(parents=True)
    (project / "Assets/MCPStub/Editor").mkdir(parents=True)
    (project / "ProjectSettings").mkdir()
    (project / "Packages").mkdir()
    (project / "Assets/Foreign.unity").write_text("%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n--- !u!104 &1\nRenderSettings:\n  m_ObjectHideFlags: 0\n  serializedVersion: 9\n  m_Fog: 0\n")
    version = "6000.0.55f1"
    (project / "ProjectSettings/ProjectVersion.txt").write_text("m_EditorVersion: " + version + "\n")
    (project / "Packages/manifest.json").write_text(json.dumps({"dependencies": {
        "com.unity.nuget.newtonsoft-json": "3.2.1", "com.unity.render-pipelines.universal": "17.0.4"}}))
    for name in ("com.bread.renderdoc", "com.bread.unity-connection"):
        shutil.copytree(repository / "unity" / name, project / "Packages" / name)
    (project / "Packages/com.coplaydev.unity-mcp").mkdir()
    (project / "Packages/com.coplaydev.unity-mcp/package.json").write_text(json.dumps({
        "name": "com.coplaydev.unity-mcp", "version": "10.3.0", "displayName": "Compile-only MCP metadata stub"}))
    (project / "Assets/MCPStub/Editor/MCPForUnity.Editor.asmdef").write_text(json.dumps({"name": "MCPForUnity.Editor", "includePlatforms": ["Editor"]}))
    (project / "Assets/MCPStub/Editor/McpAttributes.cs").write_text('''using System;
namespace MCPForUnity.Editor { public static class McpCiBoot { public static void StartStdioForCi() {} } }
namespace MCPForUnity.Editor.Tools {
[AttributeUsage(AttributeTargets.Class)] public sealed class McpForUnityToolAttribute:Attribute { public McpForUnityToolAttribute(string name){} public string Description {get;set;} }
[AttributeUsage(AttributeTargets.Property)] public sealed class ToolParameterAttribute:Attribute { public ToolParameterAttribute(string name){} public string Description {get;set;} public bool Required {get;set;} }
}''')
    shutil.copyfile(Path(__file__).parent / "fixtures/unity_capture_editor_smoke.cs", project / "Assets/Editor/BreadCaptureEditorSmoke.cs")
    matrix = [2, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    configuration = {"scene": {"object": {"position": [0, 0, 0], "rotationEuler": [0, 0, 0], "scale": [1, 1, 1]},
        "camera": {"position": [0, 0, -2], "rotationEuler": [0, 0, 0], "projection": "perspective", "near": 0.1,
                   "far": 10, "fieldOfView": 45, "allowMSAA": False, "clearFlags": "SolidColor", "backgroundColor": [0, 0, 0, 1]}, "lights": []},
        "runtimeBindings": [{"name": "_CaptureMatrix", "type": "matrix", "value": matrix},
            {"name": "_CaptureArray", "type": "float-array", "value": [0.25, 0.5, 0.75]},
            {"name": "_CaptureInteger", "type": "int", "value": 123456789},
            {"name": "_CaptureBuffer", "type": "buffer", "stride": 4, "bufferBase64": base64.b64encode(struct.pack("<2I", 0xDEADBEEF, 0x1020304)).decode()}],
        "renderState": {"requiredPasses": ["Forward"], "renderQueue": 2000}}
    requests = []
    for index, pipeline in enumerate(("built-in", "urp")):
        shader = BUILTIN_SHADER if index == 0 else SMOKE_URP_SHADER
        assets = [{"path": "Shaders/Captured.shader", "kind": "shader", "content": shader},
                  {"path": "Shaders/Common.hlsl", "kind": "include", "content": "float4 CapturedColor(float4 value) { return value; }"},
                  {"path": "Runtime/BreadSmokeMarker.cs", "kind": "script", "content": "public sealed class BreadSmokeMarker" + str(index) + " {}"}]
        if index == 1:
            assets.append({"path": "Runtime/BreadSmokeFeature.cs", "kind": "script", "content": FEATURE})
        options = {**configuration, "pipelinePolicy": {"targetPipeline": pipeline, "allowCreateUrp": index == 1},
                   "shaderAssetPath": "Shaders/Captured.shader", "assets": assets}
        package = finalized_package(project / ("Evidence" + str(index)), shader_source=SMOKE_URP_SHADER,
                                    material_color=[0.2, 0.4, 0.6, 1], unity_options=options)
        requests.append(prepare_unity_import(package, project)["requestPath"])
    plan = {"schemaVersion": 2, "target": {"pipeline": "urp", "editorVersion": version, "insertionPoint": "AfterRenderingTransparents"},
        "externalInputs": [], "outputs": ["smoke-global"], "implementationAssets": ["Runtime/BreadSmokeFeature.cs"],
        "resources": [{"version": "smoke-global", "format": "R32G32B32A32_SFloat", "width": 64, "height": 64, "samples": 1, "lifetime": "frame"}],
        "passes": [{"id": "smoke-feature", "reads": [], "writes": ["smoke-global"], "insertionPoint": "AfterRenderingTransparents",
            "implementation": {"asset": "Runtime/BreadSmokeFeature.cs", "kind": "renderer-feature", "entryPoint": "BreadSmokeFeature.Pass.RecordRenderGraph"},
            "attachments": [{"version": "smoke-global", "load": "clear", "store": "store", "clearValue": [0.125, 0.25, 0.5, 1]}]}]}
    (project / "smoke-config.json").write_text(json.dumps({"requests": requests, "configuration": configuration, "urpPlan": plan}, indent=2))
    (project / "run-command.json").write_text(json.dumps([str(editor), "-batchmode", "-projectPath", str(project),
        "-executeMethod", "BreadCaptureEditorSmoke.Run", "-logFile", str(project / "Editor.log")]))
    return project


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--editor", type=Path, required=True)
    arguments = parser.parse_args()
    print(prepare(arguments.editor))
