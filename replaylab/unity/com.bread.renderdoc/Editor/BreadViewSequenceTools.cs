using System;
using System.IO;
using System.Linq;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEditor;

namespace Bread.Editor
{
    [McpForUnityTool("bread_capture_view_sequence", Description="Request/status/cancel bounded consecutive final-view samples from existing Scene/Game cameras. Uses actual render sequences and optional warmup renders; never moves cameras, freezes time, edits a scene or claims stability. Read JSON sidecars with review_unity_temporal_stability. Repaint requests are observations, not a benchmark of normal cadence.")]
    public static class BreadCaptureViewSequence
    {
        static string id, child, scenario, status="idle", diagnostic;
        static int[] cameras;
        static int sampleCount, completed;
        static double deadline;
        static bool running;
        static readonly JArray samples=new JArray();
        static void Stop(string state, string detail=null)
        { running=false;status=state;diagnostic=detail;EditorApplication.update-=Update; }
        static JObject Call(string action,int warmup=0)
        {
            return JObject.FromObject(BreadCaptureViewOutputs.HandleCommand(new JObject {
                ["action"]=action,["camera_ids"]=new JArray(cameras??Array.Empty<int>()),["warmup_renders"]=warmup,
                ["timeout_seconds"]=Math.Min(30,Math.Max(1,deadline-EditorApplication.timeSinceStartup)) }));
        }
        static void Next(int warmup)
        {
            var response=Call("request",warmup);
            if(response.Value<bool?>("success")!=true)throw new InvalidOperationException((string)response["error"]??"View capture request failed.");
            var data=(JObject)response["data"];
            if((string)data["status"]=="existing-request-pending")throw new InvalidOperationException("Another view capture is already pending; reuse or finish that request.");
            child=(string)data["requestId"];
        }
        static void Update()
        {
            if(!running)return;
            try
            {
                if(child!=BreadPassCapture.RequestId){Stop("superseded","Another capture owns the readback queue.");return;}
                var data=(JObject)Call("status")["data"];
                string state=(string)data["status"];
                if(state=="timed-out"||EditorApplication.timeSinceStartup>=deadline)
                {Call("cancel");Stop("timed-out","A selected view did not supply all actual final outputs before the finite deadline.");return;}
                if(state=="pending")return;
                var results=data["results"] as JObject;
                foreach(int camera in cameras)
                {
                    string path=(string)results?[camera+"|Bread.View.Final"];
                    if(String.IsNullOrEmpty(path)||!File.Exists(path)||!File.Exists(path+".json"))
                    {Stop("incomplete","Missing/unsupported final output: "+path);return;}
                    samples.Add(new JObject { ["cameraId"]=camera,["sampleIndex"]=completed,["scenario"]=scenario,
                        ["sidecarPath"]=path+".json" });
                }
                completed++;
                if(completed>=sampleCount){Stop("observed");return;}
                Next(0);
            }
            catch(Exception e){Stop("failed",e.Message);}
        }
        public static object HandleCommand(JObject p)
        {
            try
            {
                string action=(string)p["action"]??"status";
                if(action=="request")
                {
                    if(running)return new {success=true,data=new {status="existing-sequence-pending",requestId=id}};
                    var selected=(p["camera_ids"] as JArray)?.Values<int>().Distinct().ToArray();
                    int count=p.Value<int?>("sample_count")??8,warmup=p.Value<int?>("warmup_renders")??2;
                    double timeout=p.Value<double?>("timeout_seconds")??60;
                    if(selected==null||selected.Length<1||selected.Length>4||count<3||count>16||warmup<0||warmup>120||
                        Double.IsNaN(timeout)||Double.IsInfinity(timeout)||timeout<5||timeout>120)
                        throw new ArgumentException("Use 1..4 existing camera IDs, sample_count 3..16, warmup_renders 0..120, finite timeout_seconds 5..120.");
                    var existing=BreadInspectCameraRendering.Cameras();
                    if(selected.Any(value=>!existing.Any(c=>c.GetInstanceID()==value)))throw new ArgumentException("Inspect current cameras; a selected view is absent.");
                    cameras=selected;sampleCount=count;completed=0;samples.Clear();diagnostic=null;
                    scenario=(string)p["scenario"]??"stationary";
                    if(scenario.Length>128)throw new ArgumentException("Use a short scenario label.");
                    id=Guid.NewGuid().ToString("N");deadline=EditorApplication.timeSinceStartup+timeout;
                    Next(warmup);status="pending";running=true;EditorApplication.update+=Update;
                }
                else if(action=="cancel")
                {if(running&&child==BreadPassCapture.RequestId)Call("cancel");Stop("cancelled");}
                else if(action!="status")throw new ArgumentException("Use request/status/cancel.");
                return new {success=true,data=new {schema="bread-view-sequence/v1",requestId=id,status,diagnostic,
                    cameraIds=cameras,sampleCount,completedSamples=completed,samples=samples.DeepClone(),diagnosticsOnly=true,
                    nextAction="Read the sidecars, compare stationary samples and each return-to-pose group, and record resize/orbit experiments. The sampler does not control animation, lighting, time or window size; do not interpret changing inputs as flicker."}};
            }
            catch(Exception e){return new {success=false,error=e.ToString()};}
        }
    }
}
