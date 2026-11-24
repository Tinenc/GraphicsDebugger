using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEditor.PackageManager;
using UnityEngine;
using UnityEngine.Experimental.Rendering;
using UnityEngine.Rendering;
using UnityEngine.SceneManagement;

namespace Bread.Editor {
[McpForUnityTool("bread_prepare_fbx_exporter",Description="Install the official FBX exporter on explicit use, or report readiness. Does not restart the Editor or use batch mode.")]
public static class BreadPrepareFbxExporter {
 static UnityEditor.PackageManager.Requests.AddRequest request;
 internal static Type Exporter => AppDomain.CurrentDomain.GetAssemblies().Select(a=>a.GetType("UnityEditor.Formats.Fbx.Exporter.ModelExporter")).FirstOrDefault(t=>t!=null);
 public static object HandleCommand(JObject p){
  if(Exporter!=null)return new {success=true,data=new {status="ready",provider="Unity FBX Exporter",editorMode=Application.isBatchMode?"batch":"interactive"}};
  if(p.Value<bool?>("install")==true&&request==null)request=Client.Add("com.unity.formats.fbx@4.2.1");
  return new {success=true,data=new {status=request==null?"not-installed":request.IsCompleted&&request.Status==StatusCode.Failure?"failed":"pending",
   error=request?.Error?.message,nextAction="After package resolution/domain reload, call again. Raw files and other asset operations remain available."}};
 }
}

[McpForUnityTool("bread_export_portable_assets",Description="Export an actual regular-renderer hierarchy as FBX, TGA/EXR textures, editable materials and a prefab. Does not certify fidelity or convert an opaque procedural buffer without its layout.")]
public static class BreadExportPortableAssets {
 static string Hash(byte[] bytes){using(var sha=SHA256.Create())return String.Concat(sha.ComputeHash(bytes).Select(x=>x.ToString("x2")));}
 static string Safe(string name){return String.Concat((name??"Asset").Select(c=>Char.IsLetterOrDigit(c)||c=='_'||c=='-'?c:'_'));}
 static byte[] Tga(Color[] pixels,int width,int height,bool srgb){
  var bytes=new byte[18+pixels.Length*4];bytes[2]=2;bytes[12]=(byte)width;bytes[13]=(byte)(width>>8);bytes[14]=(byte)height;bytes[15]=(byte)(height>>8);bytes[16]=32;bytes[17]=8;
  for(int i=0;i<pixels.Length;i++){var c=srgb?pixels[i].gamma:pixels[i];int at=18+i*4;bytes[at]=(byte)Mathf.RoundToInt(Mathf.Clamp01(c.b)*255);bytes[at+1]=(byte)Mathf.RoundToInt(Mathf.Clamp01(c.g)*255);bytes[at+2]=(byte)Mathf.RoundToInt(Mathf.Clamp01(c.r)*255);bytes[at+3]=(byte)Mathf.RoundToInt(Mathf.Clamp01(pixels[i].a)*255);}
  return bytes;
 }
 static Texture ExportTexture(Texture source,string folder,JArray facts){
  bool cube=source is Cubemap;
  if(!(source is Texture2D)&&!cube){facts.Add(new JObject{["source"]=source.name,["status"]="requires-engine-adapter",["reason"]="Use native probes/history/screen-space substitutes or an explicitly authored volume/cube adapter; retain the source."});return source;}
  int width=source.width,height=source.height;
  if(width>16384||height>16384||(long)width*height*(cube?6:1)>32L*1024*1024)throw new ArgumentException("Texture exceeds bounded export dimensions.");
  var format=source.graphicsFormat;bool srgb=GraphicsFormatUtility.IsSRGBFormat(format);
  var token=format.ToString();bool hdr=cube||token.Contains("SFloat")||token.Contains("UFloat")||token.Contains("BC6H")||token.Contains("R16")||token.Contains("R32")||token.Contains("10")||token.Contains("11");
  var previous=RenderTexture.active;var rt=RenderTexture.GetTemporary(width,height,0,RenderTextureFormat.ARGBFloat,RenderTextureReadWrite.Linear);
  var image=new Texture2D(width*(cube?6:1),height,TextureFormat.RGBAFloat,false,true);
  Texture2D face=null;
  try{
   for(int slice=0;slice<(cube?6:1);slice++){
    Texture input=source;
    if(cube){face=new Texture2D(width,height,format,TextureCreationFlags.None);Graphics.CopyTexture(source,slice,0,face,0,0);input=face;}
    Graphics.Blit(input,rt);RenderTexture.active=rt;image.ReadPixels(new Rect(0,0,width,height),slice*width,0,false);
    if(face!=null){UnityEngine.Object.DestroyImmediate(face);face=null;}
   }
   image.Apply(false,false);
   byte[] bytes=hdr?image.EncodeToEXR(token.Contains("R32")?Texture2D.EXRFlags.OutputAsFloat:Texture2D.EXRFlags.CompressZIP):Tga(image.GetPixels(),image.width,image.height,srgb);
   var identity=Hash(bytes);var path=folder+"/"+Safe(source.name)+"-"+identity.Substring(0,16)+(hdr?".exr":".tga");
   if(!File.Exists(path))File.WriteAllBytes(path,bytes);
   AssetDatabase.ImportAsset(path,ImportAssetOptions.ForceSynchronousImport);
   var importer=(TextureImporter)AssetImporter.GetAtPath(path);importer.sRGBTexture=srgb&&!hdr;importer.textureType=TextureImporterType.Default;importer.mipmapEnabled=source.mipmapCount>1;
   importer.filterMode=source.filterMode;importer.wrapMode=source.wrapMode;
   if(cube){importer.textureShape=TextureImporterShape.TextureCube;importer.generateCubemap=TextureImporterGenerateCubemap.AutoCubemap;}
   importer.SaveAndReimport();
   facts.Add(new JObject{["source"]=source.name,["path"]=path,["sha256"]=identity,["sourceFormat"]=token,["samplingSpace"]=srgb?"sRGB":"linear",
    ["mipPolicy"]="Unity-generated from exported base level",["layout"]=cube?"six faces; +X,-X,+Y,-Y,+Z,-Z horizontal strip":"2D"});
   return AssetDatabase.LoadAssetAtPath<Texture>(path);
  }finally{RenderTexture.active=previous;RenderTexture.ReleaseTemporary(rt);UnityEngine.Object.DestroyImmediate(image);if(face!=null)UnityEngine.Object.DestroyImmediate(face);}
 }
 public static object HandleCommand(JObject p){GameObject clone=null;try{
  string target=(string)p["target"];var source=String.IsNullOrEmpty(target)?Selection.activeGameObject:GameObject.Find(target)??AssetDatabase.LoadAssetAtPath<GameObject>(target);
  if(source==null)throw new ArgumentException("Select an actual scene/prefab hierarchy or provide target.");
  var renderers=source.GetComponentsInChildren<Renderer>(true).Where(r=>r is MeshRenderer||r is SkinnedMeshRenderer).ToArray();
  if(renderers.Length==0)return new {success=true,data=new {status="regular-mesh-required",nextAction="Decode or read back the actual vertex/skin output once with its layout; create MeshFilter/MeshRenderer or SkinnedMeshRenderer, then export. A procedural draw is not a normal model asset."}};
  string folder=((string)p["output_directory"]??"Assets/BreadPortable/"+Safe(source.name)).Replace('\\','/').TrimEnd('/');
  if(!folder.StartsWith("Assets/",StringComparison.Ordinal)||folder.Split('/').Any(part=>part==".."||part=="."))throw new ArgumentException("Output must be a project-local Assets directory.");
  foreach(var sub in new[]{"Models","Textures","Materials","Prefabs"})Directory.CreateDirectory(folder+"/"+sub);
  var facts=new JArray();var materials=new Dictionary<Material,Material>();var textures=new Dictionary<Texture,Texture>();
  clone=UnityEngine.Object.Instantiate(source);clone.name=source.name;clone.SetActive(false);
  foreach(var renderer in clone.GetComponentsInChildren<Renderer>(true)){
   var assigned=renderer.sharedMaterials;
   for(int i=0;i<assigned.Length;i++){
    var original=assigned[i];if(original==null)continue;
    if(!materials.TryGetValue(original,out var material)){
     material=new Material(original);var path=AssetDatabase.GenerateUniqueAssetPath(folder+"/Materials/"+Safe(original.name)+".mat");
     foreach(var property in original.GetTexturePropertyNames()){
      var texture=original.GetTexture(property);if(texture==null)continue;
      if(!textures.TryGetValue(texture,out var exported)){exported=ExportTexture(texture,folder+"/Textures",facts);textures[texture]=exported;}
      material.SetTexture(property,exported);
     }
     AssetDatabase.CreateAsset(material,path);materials[original]=material;
    }
    assigned[i]=material;
   }
   renderer.sharedMaterials=assigned;
  }
  var fbx=folder+"/Models/"+Safe(source.name)+".fbx";bool exportedFbx=false;string diagnostic=null;
  var exporter=BreadPrepareFbxExporter.Exporter;
  if(exporter!=null){
   if(File.Exists(fbx))diagnostic="Existing FBX retained; choose another output directory for an intentional new revision.";
   else{var method=exporter.GetMethods(BindingFlags.Static|BindingFlags.Public).FirstOrDefault(m=>m.Name=="ExportObject"&&m.GetParameters().Length==2);
    if(method==null)throw new NotSupportedException("Installed FBX exporter has no supported ExportObject API.");
    method.Invoke(null,new object[]{Path.GetFullPath(fbx),clone});AssetDatabase.ImportAsset(fbx,ImportAssetOptions.ForceSynchronousImport);exportedFbx=File.Exists(fbx);
    var meshes=AssetDatabase.LoadAllAssetsAtPath(fbx).OfType<Mesh>().GroupBy(m=>m.name).Where(g=>g.Count()==1).ToDictionary(g=>g.Key,g=>g.Single());
    foreach(var filter in clone.GetComponentsInChildren<MeshFilter>(true))if(filter.sharedMesh!=null&&meshes.TryGetValue(filter.sharedMesh.name,out var mesh))filter.sharedMesh=mesh;
    foreach(var skin in clone.GetComponentsInChildren<SkinnedMeshRenderer>(true))if(skin.sharedMesh!=null&&meshes.TryGetValue(skin.sharedMesh.name,out var mesh))skin.sharedMesh=mesh;
   }
  }else diagnostic="FBX exporter missing: call bread_prepare_fbx_exporter(install=true), then retry; other exported assets are retained.";
  var prefab=AssetDatabase.GenerateUniqueAssetPath(folder+"/Prefabs/"+Safe(source.name)+".prefab");clone.SetActive(source.activeSelf);PrefabUtility.SaveAsPrefabAsset(clone,prefab);AssetDatabase.SaveAssets();
  var remainingMeshes=clone.GetComponentsInChildren<MeshFilter>(true).Where(f=>f.sharedMesh!=null&&!AssetDatabase.GetAssetPath(f.sharedMesh).EndsWith(".fbx",StringComparison.OrdinalIgnoreCase)).Select(f=>f.name)
   .Concat(clone.GetComponentsInChildren<SkinnedMeshRenderer>(true).Where(f=>f.sharedMesh!=null&&!AssetDatabase.GetAssetPath(f.sharedMesh).EndsWith(".fbx",StringComparison.OrdinalIgnoreCase)).Select(f=>f.name)).ToArray();
  bool portableTextures=facts.OfType<JObject>().All(f=>f["path"]!=null);
  return new {success=true,data=new {status=exportedFbx&&remainingMeshes.Length==0&&portableTextures?"exported-awaiting-runtime-review":"partial",fbx=exportedFbx?fbx:null,prefab,materials=materials.Count,textures=facts,remainingNonFbxMeshBindings=remainingMeshes,
   diagnostic,validation="Files exported; inspect normal renderer bindings, material controls, camera/light motion and actual final output before judging fidelity."}};
 }catch(Exception e){return new {success=false,error=e.ToString()};}finally{if(clone!=null)UnityEngine.Object.DestroyImmediate(clone);}}
}

[McpForUnityTool("bread_inspect_editable_delivery",Description="Observe regular model/texture/material/prefab assets, cameras, lights and Volume settings. Returns diagnostics, never semantic acceptance.")]
public static class BreadInspectEditableDelivery {
 public static object HandleCommand(JObject p){try{
  string folder=(string)p["asset_root"]??"Assets";
  if(folder!="Assets"&&!folder.StartsWith("Assets/",StringComparison.Ordinal)||folder.Split('/').Any(x=>x==".."))throw new ArgumentException("Inspect a project-local Assets root.");
  Func<string,string[]> assets=filter=>AssetDatabase.FindAssets(filter,new[]{folder}).Select(AssetDatabase.GUIDToAssetPath).Distinct().ToArray();
  var cameras=new JArray(UnityEngine.Object.FindObjectsOfType<Camera>().Select(camera=>new JObject{["name"]=camera.name,["enabled"]=camera.enabled,["fov"]=camera.fieldOfView,
   ["orthographic"]=camera.orthographic,["target"]=camera.targetTexture==null?"Game/backbuffer":camera.targetTexture.name,["targetFormat"]=camera.targetTexture?.graphicsFormat.ToString(),["cullingMask"]=camera.cullingMask}));
  var lights=new JArray(UnityEngine.Object.FindObjectsOfType<Light>().Select(light=>new JObject{["name"]=light.name,["type"]=light.type.ToString(),["enabled"]=light.enabled,["shadows"]=light.shadows.ToString(),["intensity"]=light.intensity}));
  var normal=UnityEngine.Object.FindObjectsOfType<MeshRenderer>().Length+UnityEngine.Object.FindObjectsOfType<SkinnedMeshRenderer>().Length;
  var volumes=new JArray(UnityEngine.Object.FindObjectsOfType<MonoBehaviour>().Where(c=>c!=null&&c.GetType().Name=="Volume").Select(c=>new JObject{["name"]=c.name,["enabled"]=c.enabled,["type"]=c.GetType().FullName}));
  var indicators=new JArray();foreach(var effect in UnityEngine.Object.FindObjectsOfType<BreadCaptureEffect>())indicators.Add(new JObject{["object"]=effect.name,["type"]="capture-effect-runner",["runInCamera"]=effect.runInCamera,["output"]=effect.output,["lastError"]=effect.lastError});
  return new {success=true,data=new {status="observed",scene=SceneManager.GetActiveScene().path,normalRenderers=normal,
   fbxModels=assets("t:Model").Where(path=>path.EndsWith(".fbx",StringComparison.OrdinalIgnoreCase)).ToArray(),
   textures=assets("t:Texture").Select(path=>new {path,format=Path.GetExtension(path)}).ToArray(),materials=assets("t:Material"),prefabs=assets("t:Prefab"),
   cameras,lights,volumes,captureReplayIndicators=indicators,
   nextAction="Judge the requested delivery: conventional assets, meaningful material controls, live engine interfaces and Volume effects. A replay image or absence of automated evidence is not a semantic verdict."}};
 }catch(Exception e){return new {success=false,error=e.ToString()};}}
}
}
