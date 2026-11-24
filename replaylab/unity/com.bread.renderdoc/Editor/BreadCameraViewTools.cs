using System;
using System.IO;
using System.Linq;
using System.Collections.Generic;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;
using UnityEngine.Rendering;

namespace Bread.Editor
{
    [McpForUnityTool("bread_inspect_camera_rendering", Description="Observe existing Game/Scene cameras, effective renderer data, Feature definitions, Scene FX/lighting switches and instrumented per-view resources/history. Does not open views or change the scene. Definitions are separate from observed execution.")]
    public static class BreadInspectCameraRendering
    {
        internal static Camera[] Cameras()
        {
            return Resources.FindObjectsOfTypeAll<Camera>().Where(c => c && c.gameObject.scene.IsValid() &&
                (c.cameraType == CameraType.Game || c.cameraType == CameraType.SceneView)).
                Concat(SceneView.sceneViews.Cast<SceneView>().Select(v => v.camera)).Where(c => c).Distinct().ToArray();
        }
        static JArray Matrix(Matrix4x4 m) => new JArray(Enumerable.Range(0,16).Select(i => m[i]));
        internal static JObject CameraInfo(Camera camera)
        {
            var pipeline = GraphicsSettings.currentRenderPipeline;
            var scene = SceneView.sceneViews.Cast<SceneView>().FirstOrDefault(v => v.camera == camera);
            var row = new JObject { ["cameraId"]=camera.GetInstanceID(), ["name"]=camera.name,
                ["cameraType"]=camera.cameraType.ToString(), ["enabled"]=camera.enabled,
                ["width"]=camera.pixelWidth, ["height"]=camera.pixelHeight, ["cullingMask"]=camera.cullingMask,
                ["scaledWidth"]=camera.scaledPixelWidth,["scaledHeight"]=camera.scaledPixelHeight,
                ["aspect"]=camera.aspect,["pixelRect"]=new JArray(camera.pixelRect.x,camera.pixelRect.y,camera.pixelRect.width,camera.pixelRect.height),
                ["position"]=new JArray(camera.transform.position.x,camera.transform.position.y,camera.transform.position.z),
                ["worldToCamera"]=Matrix(camera.worldToCameraMatrix), ["projection"]=Matrix(camera.projectionMatrix),
                ["nonJitteredProjection"]=Matrix(camera.nonJitteredProjectionMatrix),
                ["cullingMatrix"]=Matrix(camera.cullingMatrix),
                ["transparentProjectionJitter"]=camera.useJitteredProjectionMatrixForTransparentRendering,
                ["near"]=camera.nearClipPlane,
                ["far"]=camera.farClipPlane, ["fov"]=camera.fieldOfView, ["orthographic"]=camera.orthographic,
                ["renderSequence"]=BreadCameraEvidence.Sequence(camera),
                ["targetTextureId"]=camera.targetTexture ? camera.targetTexture.GetInstanceID() : 0,
                ["pipelineAsset"]=pipeline ? AssetDatabase.GetAssetPath(pipeline) : "built-in",
                ["features"]=new JArray() };
            var projectionAspect = Mathf.Abs(camera.nonJitteredProjectionMatrix.m11 / camera.nonJitteredProjectionMatrix.m00);
            row["projectionAspect"] = float.IsNaN(projectionAspect) || float.IsInfinity(projectionAspect) ? (JToken)JValue.CreateNull() : new JValue(projectionAspect);
            row["viewportAspect"] = camera.pixelHeight>0 ? (float)camera.pixelWidth/camera.pixelHeight : 0;
            row["projectionMatchesViewport"] = camera.pixelHeight>0 && Mathf.Abs(projectionAspect-(float)camera.pixelWidth/camera.pixelHeight)<=2f/camera.pixelHeight;
            if (scene) { row["sceneLighting"]=scene.sceneLighting; row["imageEffects"]=scene.sceneViewState.imageEffectsEnabled; row["drawMode"]=scene.cameraMode.drawMode.ToString(); }
            var additional = camera.GetComponents<MonoBehaviour>().FirstOrDefault(c => c && c.GetType().FullName == "UnityEngine.Rendering.Universal.UniversalAdditionalCameraData");
            int requested = -1;
            if (additional) { var so=new SerializedObject(additional); requested=so.FindProperty("m_RendererIndex")?.intValue ?? -1;
                row["postProcessingRequested"]=so.FindProperty("m_RenderPostProcessing")?.boolValue; }
            if (pipeline && pipeline.GetType().FullName.Contains("UniversalRenderPipelineAsset"))
            {
                var so=new SerializedObject(pipeline);int fallback=so.FindProperty("m_DefaultRendererIndex")?.intValue ?? 0;
                int index=scene || requested<0 ? fallback : requested;
                var list=so.FindProperty("m_RendererDataList");
                // URP falls back to its default when the requested slot is absent.
                if(list!=null && (index<0 || index>=list.arraySize || !list.GetArrayElementAtIndex(index).objectReferenceValue)) index=fallback;
                row["requestedRendererIndex"]=requested;row["effectiveRendererIndex"]=index;
                var data=list!=null && index>=0 && index<list.arraySize ? list.GetArrayElementAtIndex(index).objectReferenceValue as ScriptableObject : null;
                row["rendererData"]=data ? AssetDatabase.GetAssetPath(data) : "unavailable";
                if (data)
                {
                    var definition=new SerializedObject(data);var items=definition.FindProperty("m_RendererFeatures");
                    if(items!=null) for(int i=0;i<items.arraySize;i++)
                    {
                        var feature=items.GetArrayElementAtIndex(i).objectReferenceValue as ScriptableObject;if(!feature)continue;
                        var fields=new SerializedObject(feature);string source=AssetDatabase.GetAssetPath(MonoScript.FromScriptableObject(feature));
                        string text=File.Exists(source)&&new FileInfo(source).Length<1024*1024 ? File.ReadAllText(source) : "";
                        ((JArray)row["features"]).Add(new JObject { ["name"]=feature.name,["type"]=feature.GetType().FullName,
                            ["active"]=fields.FindProperty("m_Active")?.boolValue ?? true,["sourcePath"]=source,
                            ["gameCameraFilterCandidate"]=text.Contains("CameraType.Game"),
                            ["filterFindingScope"]="source-text hint; inspect conditions and actual per-camera execution" });
                    }
                }
            }
            return row;
        }
        public static object HandleCommand(JObject p)
        {
            try
            {
                BreadCameraEvidence.Observe();
                var cameras=Cameras();var resources=JArray.FromObject(BreadCameraEvidence.Resources());
                var collisions=new JArray();
                foreach(var group in BreadCameraEvidence.Resources().Where(r=>!r.viewIndependent).GroupBy(r=>r.textureId))
                    if(group.Select(r=>r.cameraId).Distinct().Count()>1)
                        collisions.Add(new JObject { ["textureId"]=group.Key,["cameraIds"]=new JArray(group.Select(r=>r.cameraId).Distinct()),["finding"]="shared-view-dependent-allocation" });
                return new {success=true,data=new JObject { ["schema"]="bread-camera-observations/v1",
                    ["cameras"]=new JArray(cameras.Select(CameraInfo)),["resources"]=resources,
                    ["gameViews"]=new JArray(BreadGameViewResize.Observations()),
                    ["executedFeatures"]=JArray.FromObject(BreadCameraEvidence.Features()),["resourceCollisions"]=collisions,
                    ["diagnosticsOnly"]=true,["executionCoverage"]="Only explicitly instrumented Feature/resource records establish execution; empty records are unknown.",
                    ["nextAction"]="Compare effective Feature chains and settings, then capture both actual final views. Inspect Scene FX/lighting, per-camera masks and histories. Different camera poses should produce different images." } };
            }
            catch(Exception e) { return new {success=false,error=e.ToString()}; }
        }
    }

    [McpForUnityTool("bread_capture_view_outputs", Description="Request/status/cancel final outputs for explicit existing camera IDs. Captures actual SRP end targets; backbuffers need the opt-in BreadViewEvidenceUrpFeature template. Repaints existing views without moving or copying cameras. Reports pending/timeout/supersession, never substitutes one view for another.")]
    public static class BreadCaptureViewOutputs
    {
        static string requestId;
        static double nextRepaint;
        static int[] ids=Array.Empty<int>();static double deadline;static bool listening,timedOut;static string[] timedOutPending=Array.Empty<string>();
        static readonly Dictionary<int,string> unavailable=new Dictionary<int,string>();
        static void Stop() { if(!listening)return;RenderPipelineManager.endCameraRendering-=End;EditorApplication.update-=Update;listening=false; }
        static void Update()
        {
            if(requestId!=BreadPassCapture.RequestId){Stop();return;}
            if(EditorApplication.timeSinceStartup>=deadline){timedOut=true;timedOutPending=BreadPassCapture.Pending();BreadPassCapture.Cancel();Stop();}
            else if(BreadPassCapture.Pending().Length==0 && BreadPassCapture.Status().Values.All(value=>value!="pending-submission"))Stop();
            else if(EditorApplication.timeSinceStartup>=nextRepaint)
            {nextRepaint=EditorApplication.timeSinceStartup+0.1;SceneView.RepaintAll();EditorApplication.QueuePlayerLoopUpdate();UnityEditorInternal.InternalEditorUtility.RepaintAllViews();}
        }
        static void End(ScriptableRenderContext context,Camera camera)
        {
            if(!listening||!ids.Contains(camera.GetInstanceID())||requestId!=BreadPassCapture.RequestId)return;
            string key=camera.GetInstanceID()+"|Bread.View.Final";
            if(!BreadPassCapture.Pending().Contains(key))return;
            var target=camera.targetTexture;
            if(!target){unavailable[camera.GetInstanceID()]="backbuffer: install the explicit final-camera-target probe adapter; no intermediate or other camera was substituted";return;}
            var cmd=new CommandBuffer { name="Bread actual view output observation" };
            try { BreadPassCapture.Record(cmd,target,"Bread.View.Final","SRP-endCameraRendering; camera target before editor handles",camera,true);context.ExecuteCommandBuffer(cmd); }
            finally { cmd.Release(); }
        }
        public static object HandleCommand(JObject p)
        {
            try
            {
                string action=(string)p["action"]??"status";
                if(action=="request")
                {
                    if(listening)return new {success=true,data=new {status="existing-request-pending",requestId}};
                    var selected=(p["camera_ids"] as JArray)?.Values<int>().Distinct().ToArray();
                    if(selected==null||selected.Length==0||selected.Length>8)throw new ArgumentException("Select one to eight observed camera IDs, including distinct Scene and Game cameras for dual-view acceptance.");
                    var cameras=BreadInspectCameraRendering.Cameras();if(selected.Any(id=>!cameras.Any(c=>c.GetInstanceID()==id)))throw new ArgumentException("A selected view no longer exists; inspect cameras again.");
                    Stop();unavailable.Clear();ids=selected;timedOut=false;timedOutPending=Array.Empty<string>();
                    int warmup=p.Value<int?>("warmup_renders")??0;
                    double timeout=p.Value<double?>("timeout_seconds")??15;
                    if(warmup<0||warmup>120||Double.IsNaN(timeout)||Double.IsInfinity(timeout)||timeout<1||timeout>60)
                        throw new ArgumentException("Use warmup_renders 0..120 and finite timeout_seconds 1..60.");
                    var minimum=ids.ToDictionary(id=>id,id=>BreadCameraEvidence.Sequence(cameras.First(c=>c.GetInstanceID()==id))+warmup+1);
                    BreadPassCapture.Request(new[]{"Bread.View.Final"},Path.GetFullPath("Library/Bread/ViewCaptures/"+Guid.NewGuid().ToString("N")),ids,minimum);
                    requestId=BreadPassCapture.RequestId;deadline=EditorApplication.timeSinceStartup+timeout;
                    listening=true;RenderPipelineManager.endCameraRendering+=End;EditorApplication.update+=Update;
                    SceneView.RepaintAll();EditorApplication.QueuePlayerLoopUpdate();UnityEditorInternal.InternalEditorUtility.RepaintAllViews();
                }
                else if(action=="cancel"){Stop();if(requestId==BreadPassCapture.RequestId)BreadPassCapture.Cancel();}
                else if(action!="status")throw new ArgumentException("Use request/status/cancel.");
                bool own=requestId==BreadPassCapture.RequestId;
                return new {success=true,data=new {requestId,status=!own?"superseded":timedOut?"timed-out":listening?"pending":"observed-or-cancelled",
                    cameraIds=ids,results=own?BreadPassCapture.Status():new Dictionary<string,string>(),pending=own?BreadPassCapture.Pending():Array.Empty<string>(),timedOutPending,unavailable,
                    diagnosticsOnly=true,nextAction="Read matching final-view sidecars and compare original RDC output under aligned pose/extent/color/history conditions. Local Shader comparisons do not establish whole-scene or dual-view completion. Reuse unchanged proof; do not copy Game history/masks into Scene."}};
            }
            catch(Exception e){return new {success=false,error=e.ToString()};}
        }
    }
}
