using System;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;
using UnityEngine.Experimental.Rendering;
using UnityEngine.Rendering;

namespace Bread.Editor {
[McpForUnityTool("bread_capture_pass_outputs",Description="Request/status/cancel float snapshots from instrumented pass or Shader module outputs. Call BreadPassCapture.Record on the actual command buffer after that stage; pending does not mean executed.")]
public static class BreadCapturePassOutputs {
 public static object HandleCommand(JObject p){try{
  string action=(string)p["action"]??"status";
  if(action=="request"){
   var names=(p["pass_ids"] as JArray)?.Values<string>().Where(x=>!String.IsNullOrEmpty(x)).Distinct().ToArray();if(names==null||names.Length==0)throw new ArgumentException("Provide actual pass/module identifiers.");
   BreadPassCapture.Request(names,Path.GetFullPath("Library/Bread/Renders/"+Guid.NewGuid().ToString("N")),(p["camera_ids"] as JArray)?.Values<int>().Distinct().ToArray());
  }else if(action=="cancel")BreadPassCapture.Cancel();else if(action!="status")throw new ArgumentException("Action must be request/status/cancel.");
  var results=BreadPassCapture.Status();var pending=BreadPassCapture.Pending();
  return new {success=true,data=new {results,pending,readbacksComplete=results.Count>0&&pending.Length==0&&results.Values.All(File.Exists),nextAction="Render the instrumented stage. GPU readbacks are bounded to two/256 MiB in flight; freeze relevant inputs and record fixture IDs across capture frames. Compare actual float outputs against identical source inputs and reuse unchanged snapshots."}};
 }catch(Exception e){return new {success=false,error=e.ToString()};}}
}

internal static class FidelityReadback {
 internal static object Save(Texture source,string name,JObject provenance){
  if(source==null)throw new ArgumentException("An actual persistent texture is required.");
  if(source.dimension!=TextureDimension.Tex2D)throw new ArgumentException("Read back the required slice/volume explicitly; this tool captures 2D pass outputs.");
  if((long)source.width*source.height>32L*1024*1024)throw new ArgumentException("Readback exceeds bounded dimensions.");
  var folder=Path.GetFullPath("Library/Bread/Renders");Directory.CreateDirectory(folder);
  var path=Path.Combine(folder,"Pass_"+Guid.NewGuid().ToString("N")+".rgba32f");
  var previous=RenderTexture.active;var rt=RenderTexture.GetTemporary(source.width,source.height,0,RenderTextureFormat.ARGBFloat,RenderTextureReadWrite.Linear);
  var pixels=new Texture2D(source.width,source.height,TextureFormat.RGBAFloat,false,true);
  try{Graphics.Blit(source,rt);RenderTexture.active=rt;pixels.ReadPixels(new Rect(0,0,rt.width,rt.height),0,0,false);pixels.Apply(false,false);
   var bytes=pixels.GetRawTextureData<byte>().ToArray();File.WriteAllBytes(path,bytes);
   string sha;using(var hash=SHA256.Create())sha=String.Concat(hash.ComputeHash(bytes).Select(b=>b.ToString("x2")));
   var metadata=new JObject{["path"]=path,["name"]=name,["width"]=rt.width,["height"]=rt.height,["channels"]=4,["encoding"]="float32le",
    ["colorSpace"]="linear",["sourceFormat"]=source.graphicsFormat.ToString(),["sha256"]=sha,["unityFrame"]=Time.frameCount,["provenance"]=provenance};
   metadata["sourceSamples"]=source is RenderTexture multi?multi.antiAliasing:1;metadata["readbackSamples"]=1;
   File.WriteAllText(path+".json",metadata.ToString(),new System.Text.UTF8Encoding(true));return metadata;
  }finally{RenderTexture.active=previous;RenderTexture.ReleaseTemporary(rt);UnityEngine.Object.DestroyImmediate(pixels);}
 }
}

[McpForUnityTool("bread_capture_texture",Description="Read an actual persistent global/named/asset 2D pass texture as linear float32 plus a JSON sidecar, preserving HDR. Does not claim it is final camera output.")]
public static class BreadCaptureTexture {
 public static object HandleCommand(JObject p){try{
  var global=(string)p["global_name"];var asset=(string)p["asset_path"];var name=(string)p["texture_name"];
  Texture source=!String.IsNullOrEmpty(global)?Shader.GetGlobalTexture(global):!String.IsNullOrEmpty(asset)?AssetDatabase.LoadAssetAtPath<Texture>(asset):null;
  if(source==null&&!String.IsNullOrEmpty(name)){
   var matches=Resources.FindObjectsOfTypeAll<Texture>().Where(t=>t.name==name).ToArray();
   if(matches.Length==1)source=matches[0];else return new {success=true,data=new {status="selection-required",matches=matches.Select(t=>new {name=t.name,id=t.GetInstanceID(),path=AssetDatabase.GetAssetPath(t)}).ToArray()}};
  }
  return new {success=true,data=FidelityReadback.Save(source,name??global??asset,new JObject{["kind"]="actual-persistent-pass-texture",["global"]=global,["asset"]=asset})};
 }catch(Exception e){return new {success=false,error=e.ToString()};}}
}

[McpForUnityTool("bread_frame_debugger",Description="Inspect/capture Unity Frame Debugger draw/dispatch outputs. Actions: inspect, begin, select(index), capture(index), end. Call end to restore previous debugger state. Unsupported internals return diagnostics; other pass readback routes remain usable.")]
public static class BreadFrameDebugger {
 static Type Find(string name)=>AppDomain.CurrentDomain.GetAssemblies().Select(a=>a.GetType(name)).FirstOrDefault(t=>t!=null);
 static Type Utility=>Find("UnityEditorInternal.FrameDebuggerInternal.FrameDebuggerUtility");
 static object Get(object value,string name)=>value?.GetType().GetField(name,BindingFlags.Public|BindingFlags.NonPublic|BindingFlags.Instance)?.GetValue(value);
 static object Call(string name,params object[] args)=>Utility.GetMethod(name,BindingFlags.Public|BindingFlags.NonPublic|BindingFlags.Static)?.Invoke(null,args);
 static int Property(string name)=>Convert.ToInt32(Utility.GetProperty(name,BindingFlags.Public|BindingFlags.NonPublic|BindingFlags.Static).GetValue(null));
 static bool Enabled(){var type=Find("UnityEngine.Rendering.FrameDebugger");return type?.GetProperty("enabled",BindingFlags.Public|BindingFlags.Static)?.GetValue(null) as bool? ?? false;}
 public static object HandleCommand(JObject p){try{
  if(Utility==null)return new {success=true,data=new {status="unsupported-version",nextAction="Use bread_capture_texture or instrument actual pass outputs with BreadPassCapture. Do not accept a final-only screenshot as a pass comparison."}};
  var action=(string)p["action"]??"inspect";
  if(action=="begin"){
   if(!SessionState.GetBool("Bread.FrameDebugger.Owned",false)){SessionState.SetBool("Bread.FrameDebugger.PreviousEnabled",Enabled());SessionState.SetInt("Bread.FrameDebugger.PreviousLimit",Property("limit"));SessionState.SetBool("Bread.FrameDebugger.Owned",true);}
   Call("SetEnabled",true,0);EditorApplication.QueuePlayerLoopUpdate();UnityEditorInternal.InternalEditorUtility.RepaintAllViews();
   return new {success=true,data=new {status="capture-requested",nextAction="Inspect after an Editor render; this receipt is not an observed pass."}};
  }
  if(action=="end"){
   if(SessionState.GetBool("Bread.FrameDebugger.Owned",false)){Utility.GetProperty("limit").SetValue(null,SessionState.GetInt("Bread.FrameDebugger.PreviousLimit",0));Call("SetEnabled",SessionState.GetBool("Bread.FrameDebugger.PreviousEnabled",false),0);SessionState.SetBool("Bread.FrameDebugger.Owned",false);}
   return new {success=true,data=new {status="restored"}};
  }
  int count=Property("count");
  if(action=="inspect"){
   var events=Call("GetFrameEvents") as Array;var rows=new JArray();int offset=Math.Max(0,p.Value<int?>("offset")??0),limit=Math.Min(256,Math.Max(1,p.Value<int?>("limit")??128));
   if(events!=null)for(int i=offset;i<Math.Min(events.Length,offset+limit);i++){var evt=events.GetValue(i);var obj=Get(evt,"m_Obj") as UnityEngine.Object;rows.Add(new JObject{["index"]=i,["type"]=Convert.ToString(Get(evt,"m_Type")),["name"]=Convert.ToString(Call("GetFrameEventInfoName",i)),["object"]=obj==null?null:obj.name});}
   return new {success=true,data=new {status="observed",enabled=Enabled(),count,events=rows,nextOffset=offset+limit<count?(int?)(offset+limit):null,unityVersion=Application.unityVersion}};
  }
  int index=p.Value<int?>("index")??-1;if(index<0||index>=count)throw new ArgumentException("Choose an observed zero-based event index.");
  Utility.GetProperty("limit").SetValue(null,index+1);EditorApplication.QueuePlayerLoopUpdate();UnityEditorInternal.InternalEditorUtility.RepaintAllViews();
  var typeData=Find("UnityEditorInternal.FrameDebuggerInternal.FrameDebuggerEventData");if(typeData==null)throw new NotSupportedException("Frame event data is unavailable; use pass instrumentation.");
  var data=Activator.CreateInstance(typeData,true);bool ready=Call("GetFrameEventData",index,data) as bool? ?? false;
  if(!ready)return new {success=true,data=new {status="pending-render",index,nextAction="Reinvoke capture after the requested render; stale event data was not exported."}};
  var info=new JObject{["kind"]="frame-debugger-inspection-texture",["eventIndex"]=index,["eventDataHash"]=Convert.ToString(Utility.GetProperty("eventDataHash")?.GetValue(null))};
  foreach(var field in new[]{"m_OriginalShaderName","m_RealShaderName","m_PassName","m_PassLightMode","m_ShaderPassIndex","m_RenderTargetName","m_RenderTargetWidth","m_RenderTargetHeight","m_RenderTargetFormat","m_RenderTargetIsBackBuffer","m_ComputeShaderName","m_ComputeShaderKernelName","m_ComputeShaderThreadGroupsX","m_ComputeShaderThreadGroupsY","m_ComputeShaderThreadGroupsZ"}){var value=Get(data,field);if(value!=null)info[field]=JToken.FromObject(value);}
  if(action=="select")return new {success=true,data=info};
  if(action!="capture")throw new ArgumentException("Unknown debugger action.");
  var target=Get(data,"m_RenderTargetRenderTexture") as RenderTexture;
  if(target==null)return new {success=true,data=new {status="target-not-readable",info,nextAction="Capture the actual persistent pass texture or instrument its command buffer; no image was fabricated."}};
  info["mappingNote"]="Frame Debugger inspection texture; compare identical display mapping. For algorithm precision use the actual source RT via bread_capture_texture/BreadPassCapture.";
  return new {success=true,data=FidelityReadback.Save(target,"FrameDebugger_"+index,info)};
 }catch(Exception e){return new {success=false,error=e.GetBaseException().ToString(),nextAction="Use the actual pass texture/instrumentation route if this Unity version's debugger interface differs."};}}
}

[McpForUnityTool("bread_shadow_diagnostics",Description="Observe shadow settings and compute a texel-snapped, padded light-space AABB fit for real casters. Returns a plan, does not change lights or certify shadow fidelity.")]
public static class BreadShadowDiagnostics {
 public static object HandleCommand(JObject p){try{
  string lightName=(string)p["light"];var light=UnityEngine.Object.FindObjectsOfType<Light>().FirstOrDefault(l=>l.type==LightType.Directional&&(lightName==null||l.name==lightName));if(light==null)throw new ArgumentException("Select a real directional light.");
  var names=(p["renderers"] as JArray)?.Values<string>().ToArray();var renderers=UnityEngine.Object.FindObjectsOfType<Renderer>().Where(r=>r.enabled&&r.gameObject.activeInHierarchy&&(names==null||names.Contains(r.name))).ToArray();if(renderers.Length==0)throw new ArgumentException("Select actual shadow caster renderers.");
  var bounds=renderers[0].bounds;foreach(var r in renderers.Skip(1))bounds.Encapsulate(r.bounds);
  var rotation=Matrix4x4.Rotate(Quaternion.Inverse(light.transform.rotation));var min=new Vector3(float.PositiveInfinity,float.PositiveInfinity,float.PositiveInfinity);var max=-min;
  for(int i=0;i<8;i++){var point=bounds.center+Vector3.Scale(bounds.extents,new Vector3((i&1)==0?-1:1,(i&2)==0?-1:1,(i&4)==0?-1:1));point=rotation.MultiplyPoint3x4(point);min=Vector3.Min(min,point);max=Vector3.Max(max,point);}
  int resolution=Mathf.Clamp(p.Value<int?>("resolution")??2048,64,16384);float padding=Mathf.Max(0,p.Value<float?>("padding")??0.1f);min-=Vector3.one*padding;max+=Vector3.one*padding;
  float span=Mathf.Max(max.x-min.x,max.y-min.y),texel=Mathf.Max(span/resolution,1e-6f);var center=(min+max)*0.5f;center.x=Mathf.Round(center.x/texel)*texel;center.y=Mathf.Round(center.y/texel)*texel;
  float extent=span*0.5f+texel;float near=0.01f,far=max.z-min.z+2*padding+near;var position=Quaternion.Euler(light.transform.eulerAngles)*new Vector3(center.x,center.y,min.z-padding);
  var view=Matrix4x4.Scale(new Vector3(1,1,-1))*Matrix4x4.TRS(position,light.transform.rotation,Vector3.one).inverse;var projection=Matrix4x4.Ortho(-extent,extent,-extent,extent,near,far);
  Func<Vector3,JArray> vector=v=>new JArray(v.x,v.y,v.z);Func<Matrix4x4,JArray> matrix=m=>new JArray(Enumerable.Range(0,16).Select(i=>m[i]));
  var pipeline=GraphicsSettings.currentRenderPipeline;var settings=new JObject();if(pipeline!=null){var so=new SerializedObject(pipeline);foreach(var field in new[]{"m_MainLightShadowmapResolution","m_AdditionalLightsShadowmapResolution","m_ShadowDistance","m_ShadowCascadeCount","m_ShadowDepthBias","m_ShadowNormalBias","m_SoftShadowsSupported"}){var item=so.FindProperty(field);if(item!=null)settings[field]=item.propertyType==SerializedPropertyType.Boolean?JToken.FromObject(item.boolValue):item.propertyType==SerializedPropertyType.Float?JToken.FromObject(item.floatValue):JToken.FromObject(item.intValue);}}
  return new {success=true,data=new {status="observed-plan",casters=renderers.Select(r=>r.name).ToArray(),worldBounds=new {center=vector(bounds.center),size=vector(bounds.size)},lightSpaceBounds=new {min=vector(min),max=vector(max)},resolution,worldUnitsPerTexel=texel,atlasUtilization=(max.x-min.x)*(max.y-min.y)/(4*extent*extent),view=matrix(view),projection=matrix(projection),gpuProjection=matrix(GL.GetGPUProjectionMatrix(projection,true)),pipelineSettings=settings,
   lightSettings=new {light.shadowBias,light.shadowNormalBias,light.shadowNearPlane,light.shadowStrength,resolution=light.shadowResolution.ToString()},
   nextAction="Compare the source RDC shadow producer, caster alpha/skin/culling, atlas regions, filter taps, bias and receiver transform. Fit must include receivers and filter margin; add cascades for large scenes. Port the source shadow algorithm before accepting a native approximation."}};
 }catch(Exception e){return new {success=false,error=e.ToString()};}}
}
}
