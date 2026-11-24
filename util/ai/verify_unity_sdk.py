"""Compile SDK sources against an installed Unity reference set, without opening/editing a project."""
from pathlib import Path
import json
import re
import subprocess
import tempfile
import argparse
import os

root = Path(__file__).resolve().parents[2]
parser=argparse.ArgumentParser()
parser.add_argument("--output-directory",type=Path,default=root/"output/bread-view-stability-20261101")
cli_options=parser.parse_args()
project = Path("D:/Study/Project/UnityTest")
editor = Path("D:/Program Files/Unity/2022.3.21f1/Editor/Data")
compiler = editor / "MonoBleedingEdge/lib/mono/4.5/csc.exe"
mono = editor / "MonoBleedingEdge/bin/mono.exe"
package = root / "replaylab/unity/com.bread.renderdoc"
results = []
with tempfile.TemporaryDirectory(prefix="bread-sdk-compile-") as temporary:
    temporary = Path(temporary)
    for assembly, source_root in [("Bread.Capture.Runtime", package / "Runtime"), ("Bread.Capture.Editor", package / "Editor"),
            ("Bread.UnityConnection.Editor", root / "replaylab/unity/com.bread.unity-connection/Editor")]:
        matches = list((project / "Library/Bee").rglob(assembly + ".rsp"))
        if not matches:
            raise RuntimeError("Missing existing compiler reference snapshot: " + assembly)
        lines = matches[0].read_text(encoding="utf-8-sig").splitlines()
        options = [line for line in lines if line.startswith(("-r:", "/r:", "-reference:", "/reference:", "-define:", "-unsafe", "-nostdlib", "-langversion:"))]
        options = [('-r:"' + str(temporary / 'Bread.Capture.Runtime.dll') + '"') if 'Bread.Capture.Runtime.' in line and '.dll' in line else line for line in options]
        sources = sorted(source_root.glob("*.cs"))
        response = temporary / (assembly + ".rsp")
        response.write_text("\n".join(["-nologo", "-target:library", '-out:"' + str(temporary / (assembly + ".dll")) + '"',
            *options, *['"' + str(path) + '"' for path in sources]]), encoding="utf-8")
        completed = subprocess.run([str(mono), str(compiler), "@" + str(response)], cwd=project,
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        row = {"assembly": assembly, "sources": len(sources), "exitCode": completed.returncode,
            "diagnostics": (completed.stdout + completed.stderr).strip()}
        results.append(row)
        if completed.returncode:
            print(json.dumps(row, ensure_ascii=False));raise SystemExit(completed.returncode)
    # Exercise the managed history state without constructing an engine Camera,
    # loading a scene or starting Unity. Compilation alone misses double advance.
    harness=temporary/"HistoryState.cs"
    harness.write_text('''using Bread;
using UnityEngine;
class HistoryStateCheck {
 static int Main() {
  var s=new BreadCameraResources<object>.State();
  if(s.CommitHistory())return 1;
  s.RenderSequence=10;if(!s.CommitHistory()||!s.HistoryValid)return 2;
  if(s.CommitHistory())return 3;
  s.RenderSequence=9;if(s.CommitHistory())return 4;
  s.ResetHistory("resize");if(s.HistoryValid||s.HistoryResetReason!="resize")return 5;
  s.RenderSequence=11;if(!s.CommitHistory()||!s.HistoryValid)return 6;
  var wide=new BreadViewLayout(2048,2048,new RectInt(32,16,1600,900));
  var mapped=wide.MapViewUv(new Vector2(1,1));
  if(System.Math.Abs(mapped.x-1632f/2048)>0.000001 || System.Math.Abs(mapped.y-916f/2048)>0.000001)return 7;
  if(wide.ViewSize.x!=1600 || wide.ViewSize.y!=900 || wide.TexelSize.z!=2048)return 8;
  var tall=new BreadViewLayout(1024,2048,new RectInt(0,0,600,1200));
  if(tall.ViewSize.x/tall.ViewSize.y!=.5f || wide.ViewSize.x/wide.ViewSize.y<1)return 9;
  try { new BreadViewLayout(100,100,new RectInt(0,0,101,100)); return 10; }
  catch(System.ArgumentException) {}
  var projection=new Matrix4x4();projection.m00=1.125f;projection.m11=2f;projection.m02=.125f;projection.m32=-1;
  foreach(var extent in new[]{new Vector2Int(1600,900),new Vector2Int(900,1600),new Vector2Int(1600,900)}) {
   var adapted=BreadProjectionPolicy.MatchViewportAspect(projection,extent.x,extent.y);
   if(System.Math.Abs(System.Math.Abs(adapted.m11/adapted.m00)-(float)extent.x/extent.y)>1e-6)return 11;
   if(adapted.m11!=projection.m11||adapted.m02!=projection.m02||adapted.m32!=projection.m32)return 12;
  }
  var jittered=BreadRenderProjection.WithPixelJitter(projection,new Vector2(.5f,-.25f),1600,900);
  var point=new Vector4(1,2,3,1);var before=projection*point;var after=jittered*point;
  if(System.Math.Abs(after.x/after.w-before.x/before.w-1f/1600)>1e-6)return 14;
  if(System.Math.Abs(after.y/after.w-before.y/before.w+.5f/900)>1e-6)return 15;
  var ortho=Matrix4x4.identity;var moved=BreadRenderProjection.WithPixelJitter(ortho,new Vector2(1,1),600,1200);
  if(System.Math.Abs(moved.m03-2f/600)>1e-6 || System.Math.Abs(moved.m13-2f/1200)>1e-6)return 16;
  if(projection.m00!=1.125f)return 13;
  return 0;
 }
}''',encoding="utf-8")
    engine=editor/"Managed/UnityEngine"
    completed=subprocess.run([str(mono),str(compiler),"-nologo","-nostdlib+","-target:exe",'-out:'+str(temporary/"HistoryState.exe"),
        '-r:'+str(temporary/"Bread.Capture.Runtime.dll"),'-r:'+str(engine/"UnityEngine.CoreModule.dll"),
        '-r:'+str(editor/"NetStandard/ref/2.1.0/netstandard.dll"),str(harness)],
        cwd=temporary,capture_output=True,text=True,encoding="utf-8",errors="replace")
    if completed.returncode:raise RuntimeError(completed.stdout+completed.stderr)
    environment=dict(os.environ,MONO_PATH=str(engine)+os.pathsep+str(temporary))
    completed=subprocess.run([str(mono),str(temporary/"HistoryState.exe")],cwd=temporary,env=environment,
        capture_output=True,text=True,encoding="utf-8",errors="replace")
    results.append(dict(assembly="Bread managed history state behavior",sources=1,exitCode=completed.returncode,
                        diagnostics=(completed.stdout+completed.stderr).strip()))
    if completed.returncode:raise RuntimeError("Managed history-state behavior failed: "+str(results[-1]))
    references = [path for path in (project / "Library/ScriptAssemblies").glob("*.dll")
        if path.name in {"Unity.RenderPipelines.Core.Runtime.dll", "Unity.RenderPipelines.Universal.Runtime.dll"}]
    templates = []
    for name in ("BreadCaptureUrpFeature", "BreadViewEvidenceUrpFeature", "BreadScreenEffectUrpFeature"):
        source = temporary / (name + ".cs")
        source.write_bytes((package / "Templates" / (name + ".cs.txt")).read_bytes())
        templates.append(source)
    runtime_lines = next((project / "Library/Bee").rglob("Bread.Capture.Runtime.rsp")).read_text(encoding="utf-8-sig").splitlines()
    options = [line for line in runtime_lines if line.startswith(("-r:", "/r:", "-reference:", "-nostdlib", "-define:", "-langversion:"))]
    response = temporary / "urp.rsp"
    response.write_text("\n".join(["-nologo", "-target:library", '-out:"' + str(temporary / "URP.dll") + '"', *options,
        '-r:"' + str(temporary / "Bread.Capture.Runtime.dll") + '"', *['-r:"' + str(path) + '"' for path in references], *['"' + str(path) + '"' for path in templates]]), encoding="utf-8")
    completed = subprocess.run([str(mono), str(compiler), "@" + str(response)], cwd=project, capture_output=True, text=True, encoding="utf-8", errors="replace")
    results.append({"assembly":"Bread.Capture.URP templates", "sources":len(templates), "exitCode":completed.returncode,"diagnostics":(completed.stdout+completed.stderr).strip()})
    output = cli_options.output_directory / "unity-sdk-compile.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"projectReadOnly":True,"results":results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(results, ensure_ascii=True))
    raise SystemExit(completed.returncode)
