using System;
using System.Collections.Generic;
using System.IO;
using UnityEngine;
using UnityEngine.Rendering;

namespace Bread {
// Append after the actual producer commands. Copies and readbacks preserve the
// caller's render targets, viewport and matrices; diagnostic Blit calls did not.
public static class BreadPassCapture {
 static readonly HashSet<string> requested=new HashSet<string>();
 static readonly Dictionary<string,string> results=new Dictionary<string,string>();
 static string folder; static int generation, inFlight; static long inFlightBytes;
 static readonly Dictionary<int,long> minimumSequences=new Dictionary<int,long>();
 public static string RequestId {get;private set;}
 public static void Request(string[] passIds,string outputFolder,int[] cameraIds=null,Dictionary<int,long> minRenderSequences=null){
  generation++;RequestId=Guid.NewGuid().ToString("N");requested.Clear();results.Clear();folder=Path.GetFullPath(outputFolder);Directory.CreateDirectory(folder);
  BreadCameraEvidence.Observe();
  minimumSequences.Clear();if(minRenderSequences!=null)foreach(var row in minRenderSequences)minimumSequences[row.Key]=row.Value;
  foreach(var id in passIds){if(cameraIds==null||cameraIds.Length==0)requested.Add(id);else foreach(int camera in cameraIds)requested.Add(camera+"|"+id);}
 }
 public static Dictionary<string,string> Status()=>new Dictionary<string,string>(results);
 public static string[] Pending(){var items=new string[requested.Count];requested.CopyTo(items);return items;}
 public static void Cancel(){generation++;requested.Clear();}
 [Serializable] sealed class Metadata {
  public string path,encoding="float32le",colorSpace="source-sampling-format",rowOrigin="native-gpu-readback",gpuApi,passId,sourceFormat,fixtureId,cameraName,cameraType;
  public int width,height,channels=4,unityFrame,cameraId,sourceSamples,readbackSamples=1;public long renderSequence;
  public int cameraPixelWidth,cameraPixelHeight,scaledPixelWidth,scaledPixelHeight;
  public Rect pixelRect;
  public BreadCameraEvidence.Resource[] resources;
  public Matrix4x4 worldToCamera,projection,nonJitteredProjection;public Vector3 cameraPosition;
  public bool finalCameraOutput,graphicsUVStartsAtTop;
 }
 public static void Record(CommandBuffer commands,RenderTexture source,string passId,string fixtureId="",Camera camera=null,bool finalCameraOutput=false){
  camera=camera?camera:BreadCameraEvidence.CurrentCamera?BreadCameraEvidence.CurrentCamera:Camera.current;
  string selected=(camera?camera.GetInstanceID():0)+"|"+passId;
  string key=requested.Contains(selected)?selected:passId;
  if(source==null||commands==null||!requested.Contains(key))return;
  if(camera && minimumSequences.TryGetValue(camera.GetInstanceID(),out var minimum) && BreadCameraEvidence.Sequence(camera)<minimum)return;
  long bytes=(long)source.width*source.height*32;
  if(bytes>256L*1024*1024){requested.Remove(key);results[key]="readback-too-large; capture a smaller ROI or tile the output";return;}
  if(inFlight>=2||inFlightBytes+bytes>256L*1024*1024){results[key]="waiting-readback-budget; freeze inputs across capture frames";return;}
  requested.Remove(key);
  if(!SystemInfo.supportsAsyncGPUReadback||SystemInfo.copyTextureSupport==CopyTextureSupport.None){results[key]="unsupported-copy-or-readback; use a persistent output capture adapter";return;}
  if(source.dimension!=TextureDimension.Tex2D||source.graphicsFormat==UnityEngine.Experimental.Rendering.GraphicsFormat.None){results[key]="explicit-slice-or-depth-decoder-required";return;}
  var descriptor=source.descriptor;descriptor.depthBufferBits=0;descriptor.depthStencilFormat=UnityEngine.Experimental.Rendering.GraphicsFormat.None;
  descriptor.msaaSamples=1;descriptor.bindMS=false;descriptor.useMipMap=false;descriptor.autoGenerateMips=false;descriptor.enableRandomWrite=false;
  var copy=new RenderTexture(descriptor);copy.Create();int expectedGeneration=generation;
  inFlight++;inFlightBytes+=bytes;
  var path=Path.Combine(folder,"Module_"+Guid.NewGuid().ToString("N")+".rgba32f");
  var metadata=new Metadata{path=path,passId=passId,fixtureId=fixtureId,sourceFormat=source.graphicsFormat.ToString(),width=copy.width,height=copy.height,
   unityFrame=Time.frameCount,cameraId=camera?camera.GetInstanceID():0,cameraName=camera?camera.name:"",cameraType=camera?camera.cameraType.ToString():"unobserved",
   renderSequence=BreadCameraEvidence.Sequence(camera),sourceSamples=source.antiAliasing,finalCameraOutput=finalCameraOutput,
   worldToCamera=camera?camera.worldToCameraMatrix:Matrix4x4.identity,projection=camera?camera.projectionMatrix:Matrix4x4.identity,
   nonJitteredProjection=camera?camera.nonJitteredProjectionMatrix:Matrix4x4.identity,cameraPosition=camera?camera.transform.position:Vector3.zero,
   gpuApi=SystemInfo.graphicsDeviceType.ToString(),graphicsUVStartsAtTop=SystemInfo.graphicsUVStartsAtTop};
  metadata.cameraPixelWidth=camera?camera.pixelWidth:0;metadata.cameraPixelHeight=camera?camera.pixelHeight:0;
  metadata.scaledPixelWidth=camera?camera.scaledPixelWidth:0;metadata.scaledPixelHeight=camera?camera.scaledPixelHeight:0;
  metadata.pixelRect=camera?camera.pixelRect:new Rect();
  metadata.resources=Array.FindAll(BreadCameraEvidence.Resources(),r=>r.cameraId==metadata.cameraId && r.renderSequence==metadata.renderSequence);
  results[key]="pending-submission";
  if(source.antiAliasing>1)commands.ResolveAntiAliasedSurface(source,copy);else commands.CopyTexture(source,copy);
  commands.RequestAsyncReadback(copy,0,TextureFormat.RGBAFloat,request=>{
   try{if(expectedGeneration!=generation)return;if(request.hasError){results[key]="GPU-readback-failed; inspect actual source format support";return;}
    File.WriteAllBytes(path,request.GetData<byte>().ToArray());File.WriteAllText(path+".json",JsonUtility.ToJson(metadata,true),new System.Text.UTF8Encoding(true));results[key]=path;}
   catch(Exception error){if(expectedGeneration==generation)results[key]=error.Message;}
   finally{inFlight--;inFlightBytes-=bytes;copy.Release();if(Application.isPlaying)UnityEngine.Object.Destroy(copy);else UnityEngine.Object.DestroyImmediate(copy);}
  });
 }
}
}
