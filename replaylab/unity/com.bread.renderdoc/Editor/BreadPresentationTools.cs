using System;
using System.IO;
using System.Linq;
using System.Reflection;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEditor.PackageManager;
using UnityEngine;
using UnityEngine.Rendering;

namespace Bread.Editor {
[McpForUnityTool("bread_prepare_urp_presentation",Description="Prepare an URP 14+ camera-color adapter. Rerun check_only after compilation; this never treats an effect RT as proof of final Game output.")]
public static class BreadPrepareUrpPresentation {
 public static object HandleCommand(JObject p){try{
  var pipeline=GraphicsSettings.currentRenderPipeline;
  if(pipeline==null||!pipeline.GetType().FullName.Contains("UniversalRenderPipelineAsset"))return new {success=true,data=new {status="not-applicable",reason="Use the active pipeline's native color-target/custom-pass adapter; the pipeline is preserved."}};
  var pipelinePackage=UnityEditor.PackageManager.PackageInfo.FindForAssembly(pipeline.GetType().Assembly);
  int major=0;Int32.TryParse((pipelinePackage?.version??"").Split('.')[0],out major);
  if(major<14||major>=17)return new {success=true,data=new {status="version-specific-adapter-required",version=pipelinePackage?.version,nextAction="Author an adapter for this installed URP version (RenderGraph or legacy handles); continue other import work. This template targets URP 14-16."}};
  const string folder="Assets/Bread/Rendering";
  var featureType=AppDomain.CurrentDomain.GetAssemblies().Select(a=>a.GetType("Bread.URP.BreadCaptureUrpFeature")).FirstOrDefault(t=>t!=null);
  if(featureType==null){
   if(p.Value<bool?>("check_only")!=true){
    Directory.CreateDirectory(folder);var package=UnityEditor.PackageManager.PackageInfo.FindForAssembly(typeof(BreadPrepareUrpPresentation).Assembly);
    foreach(var name in new[]{"BreadCaptureUrpFeature.cs","BreadCameraPresentation.shader"}){
     var target=folder+"/"+name;
     if(!File.Exists(target))File.Copy(Path.Combine(package.resolvedPath,"Templates",name+".txt"),target);
    }
    var assembly=folder+"/Bread.Capture.URP.asmdef";
    if(!File.Exists(assembly))File.WriteAllText(assembly,"{\"name\":\"Bread.Capture.URP\",\"references\":[\"Bread.Capture.Runtime\",\"Unity.RenderPipelines.Core.Runtime\",\"Unity.RenderPipelines.Universal.Runtime\"]}");
    AssetDatabase.Refresh();
   }
   return new {success=true,data=new {status="pending-compilation",nextAction="Reinvoke this tool after compilation; retain customized files and inspect compiler errors if it stays pending."}};
  }
  var assetPath=(string)p["renderer_asset_path"];
  UnityEngine.Object renderer=null;
  if(!String.IsNullOrEmpty(assetPath))renderer=AssetDatabase.LoadAssetAtPath<UnityEngine.Object>(assetPath);
  else{var so=new SerializedObject(pipeline);var list=so.FindProperty("m_RendererDataList");var index=so.FindProperty("m_DefaultRendererIndex");if(list!=null&&list.arraySize>0)renderer=list.GetArrayElementAtIndex(Mathf.Clamp(index?.intValue??0,0,list.arraySize-1)).objectReferenceValue;}
  if(renderer==null)throw new ArgumentException("Select the actual active UniversalRendererData asset.");
  var serialized=new SerializedObject(renderer);var features=serialized.FindProperty("m_RendererFeatures");
  if(features==null)throw new ArgumentException("Selected asset has no renderer feature list.");
  ScriptableObject feature=null;
  for(int i=0;i<features.arraySize;i++){var candidate=features.GetArrayElementAtIndex(i).objectReferenceValue;if(candidate!=null&&candidate.GetType()==featureType)feature=(ScriptableObject)candidate;}
  if(p.Value<bool?>("check_only")==true)return new {success=true,data=new {status=feature==null?"available-not-configured":"configured",renderer=AssetDatabase.GetAssetPath(renderer),target="allocated-camera-color-handle"}};
  if(feature==null){feature=ScriptableObject.CreateInstance(featureType);feature.name="Bread camera color presentation";AssetDatabase.AddObjectToAsset(feature,renderer);int index=features.arraySize++;features.GetArrayElementAtIndex(index).objectReferenceValue=feature;serialized.ApplyModifiedProperties();}
  var fields=new SerializedObject(feature);fields.FindProperty("sourceIsDisplayEncoded").boolValue=p.Value<bool?>("source_is_display_encoded")==true;fields.FindProperty("flipSourceY").boolValue=p.Value<bool?>("flip_y")==true;fields.ApplyModifiedProperties();
  EditorUtility.SetDirty(renderer);EditorUtility.SetDirty(feature);AssetDatabase.SaveAssets();
  return new {success=true,data=new {status="configured",renderer=AssetDatabase.GetAssetPath(renderer),insertion="BeforeRenderingPostProcessing",target="allocated-camera-color-handle",nextAction="For cameras using this adapter set BreadCaptureEffect.runInCamera=false to avoid duplicate execution. Render the actual camera, verify Game output, encoding and active Volume overrides."}};
 }catch(Exception e){return new {success=false,error=e.ToString()};}}
}
}
