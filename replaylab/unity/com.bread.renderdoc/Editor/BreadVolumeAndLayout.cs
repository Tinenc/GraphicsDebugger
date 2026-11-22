using System;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;
using UnityEngine.Experimental.Rendering;
using UnityEngine.Rendering;

namespace Bread.Editor {
[McpForUnityTool("bread_import_volume_texture",Description="Import an exact raw mip-chain volume manifest as a native Texture3D asset with explicit format and U/V/W sampling. Does not flatten 3D into a screen image or regenerate source mips.")]
public static class BreadImportVolumeTexture {
 public static object HandleCommand(JObject p){Texture3D texture=null;try{
  var manifestPath=Path.GetFullPath((string)p["manifest_path"]??"");var data=JObject.Parse(File.ReadAllText(manifestPath));
  if((string)data["schema"]!="bread-volume-texture/v1")throw new ArgumentException("Use a captured volume mip-chain manifest.");
  string output=((string)p["asset_path"]??"Assets/BreadRestoration/Textures/IndirectVolume.asset").Replace('\\','/');
  if(!output.StartsWith("Assets/",StringComparison.Ordinal)||output.Split('/').Contains("..")||!output.EndsWith(".asset",StringComparison.OrdinalIgnoreCase))throw new ArgumentException("Select a native Texture3D asset path under Assets.");
  if(File.Exists(output))throw new IOException("Retain the existing texture/GUID; select a new asset path for a changed volume revision.");
  string formatName=(string)p["graphics_format"]??(string)data["sourceFormat"];
  if(!Enum.TryParse(formatName,true,out GraphicsFormat format)||format==GraphicsFormat.None||!SystemInfo.IsFormatSupported(format,FormatUsage.Sample))return new {success=true,data=new {status="decode-or-format-adapter-required",sourceFormat=(string)data["sourceFormat"],graphicsFormat=formatName,nextAction="Use the exact format mapping or decode the source blocks/precision explicitly; keep captured bytes and continue other restoration work."}};
  var mips=data["mips"] as JArray;if(mips==null||mips.Count==0)throw new ArgumentException("All source mip levels are required.");
  int w=data.Value<int>("width"),h=data.Value<int>("height"),d=data.Value<int>("depth");if(w<1||h<1||d<1||(long)w*h*d>32L*1024*1024)throw new ArgumentException("Volume exceeds bounded dimensions.");
  texture=new Texture3D(w,h,d,format,mips.Count>1?TextureCreationFlags.MipChain:TextureCreationFlags.None,mips.Count);texture.name=(string)data["name"]??Path.GetFileNameWithoutExtension(output);
  for(int i=0;i<mips.Count;i++){
   var row=(JObject)mips[i];var raw=File.ReadAllBytes((string)row["path"]);if(raw.LongLength>256L*1024*1024)throw new ArgumentException("Mip exceeds bounded data size.");
   long expected=((Math.Max(1,w>>i)+(long)GraphicsFormatUtility.GetBlockWidth(format)-1)/GraphicsFormatUtility.GetBlockWidth(format))*
    ((Math.Max(1,h>>i)+(long)GraphicsFormatUtility.GetBlockHeight(format)-1)/GraphicsFormatUtility.GetBlockHeight(format))*Math.Max(1,d>>i)*GraphicsFormatUtility.GetBlockSize(format);
   if(raw.LongLength!=expected)throw new ArgumentException("Raw mip size does not match the selected graphicsFormat; use the actual format or an explicit decoder.");
   using(var sha=SHA256.Create()){var actual=String.Concat(sha.ComputeHash(raw).Select(b=>b.ToString("x2")));if(actual!=(string)row["sha256"])throw new IOException("Mip bytes changed after export.");}
   texture.SetPixelData(raw,i,0);
  }
  var sample=p["sampling"] as JObject??data["sampling"] as JObject??new JObject();
  if(Enum.TryParse((string)sample["filterMode"]??"Trilinear",true,out FilterMode filter))texture.filterMode=filter;
  if(Enum.TryParse((string)sample["wrapU"]??"Clamp",true,out TextureWrapMode wrapU))texture.wrapModeU=wrapU;
  if(Enum.TryParse((string)sample["wrapV"]??"Clamp",true,out TextureWrapMode wrapV))texture.wrapModeV=wrapV;
  if(Enum.TryParse((string)sample["wrapW"]??"Clamp",true,out TextureWrapMode wrapW))texture.wrapModeW=wrapW;
  texture.anisoLevel=Mathf.Clamp(sample.Value<int?>("anisoLevel")??1,0,16);texture.mipMapBias=sample.Value<float?>("mipMapBias")??0;
  texture.Apply(false,false);Directory.CreateDirectory(Path.GetDirectoryName(output));AssetDatabase.CreateAsset(texture,output);texture=null;AssetDatabase.SaveAssets();
  return new {success=true,data=new {status="imported-awaiting-sampling-review",asset=output,dimensions=new[]{w,h,d},mipCount=mips.Count,graphicsFormat=format.ToString(),sampling=sample,samplingComplete=new[]{"filterMode","wrapU","wrapV","wrapW"}.All(k=>sample[k]!=null),
   nextAction="Bind the original world-to-volume coordinates, SH/probe decode and LOD rules. Compare sampled coefficients/modules; dimension alone does not prove a volume is static or light-independent."}};
 }catch(Exception e){return new {success=false,error=e.ToString()};}finally{if(texture!=null)UnityEngine.Object.DestroyImmediate(texture);}}
}

[McpForUnityTool("bread_apply_asset_layout",Description="Apply an explicit flat asset layout plan through AssetDatabase.MoveAsset, preserving GUIDs. Rebind duplicated texture references while retaining originals; returns collisions individually instead of overwriting assets.")]
public static class BreadApplyAssetLayout {
 static bool Valid(string path)=>!String.IsNullOrEmpty(path)&&path.StartsWith("Assets/",StringComparison.Ordinal)&&!path.Split('/').Any(x=>x==".."||x==".");
 static void Folder(string path){path=path.Replace('\\','/');if(AssetDatabase.IsValidFolder(path))return;var parent=Path.GetDirectoryName(path).Replace('\\','/');Folder(parent);AssetDatabase.CreateFolder(parent,Path.GetFileName(path));}
 public static object HandleCommand(JObject p){try{
  var plan=p["plan"] as JObject??p;bool dry=p.Value<bool?>("dry_run")??true;var results=new JArray();var validTargets=new System.Collections.Generic.HashSet<string>();
  string scope=(string)plan["assetRoot"];if(!dry&&(plan["textureAliases"] as JArray)?.Count>0&&!Valid(scope+"/scope"))throw new ArgumentException("Provide the owned assetRoot for reference updates.");
  if(!dry)foreach(JObject item in plan["moves"] as JArray??new JArray()){string target=((string)item["target"]??"").Replace('\\','/');if(Valid(target))Folder(Path.GetDirectoryName(target));}
  if(!dry)AssetDatabase.StartAssetEditing();
  try{
  foreach(JObject item in plan["moves"] as JArray??new JArray()){
   string source=((string)item["source"]??"").Replace('\\','/'),target=((string)item["target"]??"").Replace('\\','/');
   if(!Valid(source)||!Valid(target)){results.Add(new JObject{["source"]=source,["status"]="invalid-path"});continue;}
   string guid=AssetDatabase.AssetPathToGUID(source);
   if(String.IsNullOrEmpty(guid)){results.Add(new JObject{["source"]=source,["status"]="missing-source"});continue;}
   if(source==target){validTargets.Add(target);results.Add(new JObject{["source"]=source,["target"]=target,["status"]="current",["guid"]=guid});continue;}
   if(File.Exists(target)){results.Add(new JObject{["source"]=source,["target"]=target,["status"]="collision-preserved"});continue;}
   string error=null;if(!dry)error=AssetDatabase.MoveAsset(source,target);
   if(!dry&&String.IsNullOrEmpty(error))validTargets.Add(target);
   results.Add(new JObject{["source"]=source,["target"]=target,["status"]=dry?"planned":String.IsNullOrEmpty(error)?"moved":"failed",["guid"]=guid,["error"]=error});
  }
  }finally{if(!dry)AssetDatabase.StopAssetEditing();}
  int rebound=0;var unresolved=new JArray();
  if(!dry)foreach(JObject alias in plan["textureAliases"] as JArray??new JArray()){
   string oldPath=(string)alias["source"],newPath=(string)alias["canonical"];if(!Valid(oldPath)||!Valid(newPath))continue;
   if(!validTargets.Contains(newPath)){unresolved.Add(alias.DeepClone());continue;}
   var old=AssetDatabase.LoadAssetAtPath<Texture>(oldPath);var canonical=AssetDatabase.LoadAssetAtPath<Texture>(newPath);
   if(old==null||canonical==null){unresolved.Add(alias.DeepClone());continue;}
   foreach(var id in AssetDatabase.FindAssets("t:Material",new[]{scope})){var material=AssetDatabase.LoadAssetAtPath<Material>(AssetDatabase.GUIDToAssetPath(id));if(material==null)continue;bool changed=false;
    foreach(var key in material.GetTexturePropertyNames())if(material.GetTexture(key)==old){material.SetTexture(key,canonical);changed=true;rebound++;}
    if(changed)EditorUtility.SetDirty(material);
   }
  }
  if(!dry)AssetDatabase.SaveAssets();
  return new {success=true,data=new {dryRun=dry,assets=results,reboundTextureProperties=rebound,unresolvedTextureAliases=unresolved,originalDuplicateFilesPreserved=true,
   nextAction="Inspect shared references and scene/prefab bindings. Archive retained duplicates only after consumers are rebound; no .meta GUIDs were regenerated."}};
 }catch(Exception e){return new {success=false,error=e.ToString()};}}
}
}
