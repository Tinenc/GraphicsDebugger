"""Prepare a fresh real-Unity compute, history and alternative-route smoke project."""
import argparse
import json
from pathlib import Path
import shutil

from src.unity_restore import prepare_unity_import
from src.unity_recovery import write_recovery_assets
from tests.unity_capture_editor_smoke import prepare
from tests.test_unity_restore import finalized_package
from tests.test_unity_effects import COMPUTE, effect_options

FULLSCREEN = '''Shader "Bread/TemporalFallback" { SubShader { Pass { ZTest Always ZWrite Off Cull Off
CGPROGRAM
#pragma vertex vert_img
#pragma fragment frag
#include "UnityCG.cginc"
sampler2D History; float4 _BaseColor;
float4 frag(v2f_img input):SV_Target { return lerp(tex2D(History,input.uv),_BaseColor,0.5); }
ENDCG
} } }
'''
CPU = '''using Bread;
using UnityEngine;
public sealed class BreadCpuTemporal:MonoBehaviour,IBreadCaptureEffectPass {
 public void Execute(BreadCaptureEffect effect,Camera camera,BreadEffectPass pass) {
  var source=effect.Resource("history"); var target=effect.Resource(pass.output);
  var previous=RenderTexture.active; var image=new Texture2D(source.width,source.height,TextureFormat.RGBAFloat,false,true);
  try { RenderTexture.active=source; image.ReadPixels(new Rect(0,0,source.width,source.height),0,0);
   var values=image.GetPixels(); Color color=effect.parameters.ParameterBlock.GetVector("_BaseColor");
   for(int index=0;index<values.Length;index++) values[index]=Color.Lerp(values[index],color,0.5f);
   image.SetPixels(values); image.Apply(false,false); Graphics.Blit(image,target);
  } finally { RenderTexture.active=previous; Object.DestroyImmediate(image); }
 }
}
'''

def create(editor):
    project = prepare(editor)
    # Keep the infrastructure preparation but replace its runner with the effect
    # suite; old smoke-state does not exist in this newly-created project.
    (project / "Assets/Editor/BreadCaptureEditorSmoke.cs").unlink()
    shutil.copyfile(Path(__file__).parent / "fixtures/unity_effect_editor_smoke.cs", project / "Assets/Editor/BreadEffectEditorSmoke.cs")
    options = effect_options()
    package = finalized_package(project / "DispatchEvidence", dispatch=True, shader_source=COMPUTE,
        material_color=[0.2,0.4,0.6,1], unity_options=options)
    request = prepare_unity_import(package, project)
    authored = write_recovery_assets(str(project), "a" * 32, [
        {"path": "Temporal.compute", "content": COMPUTE},
        {"path": "Temporal.shader", "content": FULLSCREEN},
        {"path": "BreadCpuTemporal.cs", "content": CPU}], "Synthetic verification of executable platform alternatives")
    (project / "effect-config.json").write_text(json.dumps({"request": request["requestPath"], "effect": options["effect"],
        "renderPlan": options["renderPlan"], "recoveryAssets": authored}, indent=2))
    (project / "run-command.json").write_text(json.dumps([str(editor), "-batchmode", "-projectPath", str(project),
        "-executeMethod", "BreadEffectEditorSmoke.Run", "-logFile", str(project / "Editor.log")]))
    return project

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--editor", required=True, type=Path)
    print(create(parser.parse_args().editor))
