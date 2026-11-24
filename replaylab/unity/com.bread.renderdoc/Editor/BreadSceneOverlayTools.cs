using System;
using System.Collections.Generic;
using System.Linq;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;

namespace Bread.Editor
{
    [McpForUnityTool("bread_observe_scene_overlays", Description="Bounded request/status/cancel observations of existing SceneView camera matrices and actual HandleUtility projections of light positions over multiple repaints. Does not move cameras/lights, change render settings or focus Unity. Diagnostics complement final rendered-image comparison; projected positions are not screenshots of gizmo icons.")]
    public static class BreadObserveSceneOverlays
    {
        static readonly List<JObject> samples = new List<JObject>();
        static SceneView view;
        static int wanted;
        static double deadline, nextRepaint;
        static bool listening;
        static string state = "idle", requestId;
        static void Stop(string reason)
        {
            if(listening) { SceneView.duringSceneGui -= Observe; EditorApplication.update -= Update; }
            listening = false; state = reason;
        }
        static void Update()
        {
            if(!view || !view.camera) { Stop("view-closed"); return; }
            if(EditorApplication.timeSinceStartup >= deadline) { Stop("timed-out"); return; }
            if(EditorApplication.timeSinceStartup >= nextRepaint)
            { nextRepaint = EditorApplication.timeSinceStartup + .1; view.Repaint(); }
        }
        static void Observe(SceneView current)
        {
            if(!listening || current != view || Event.current.type != EventType.Repaint) return;
            var lights = Resources.FindObjectsOfTypeAll<Light>().Where(light => light &&
                light.gameObject.scene.IsValid() && light.gameObject.activeInHierarchy).OrderBy(light=>light.GetInstanceID()).Take(32);
            var points = new JArray();
            foreach(var light in lights)
            {
                var world = light.transform.position;
                var gui = HandleUtility.WorldToGUIPoint(world);
                points.Add(new JObject { ["lightId"]=light.GetInstanceID(),
                    ["world"]=new JArray(world.x,world.y,world.z), ["gui"]=new JArray(gui.x,gui.y) });
            }
            samples.Add(new JObject { ["time"]=EditorApplication.timeSinceStartup,
                ["camera"]=BreadInspectCameraRendering.CameraInfo(current.camera), ["lightHandleProjections"]=points,
                ["pointsPerPixel"]=1f/EditorGUIUtility.pixelsPerPoint });
            if(samples.Count >= wanted) Stop("observed");
        }
        public static object HandleCommand(JObject parameters)
        {
            try
            {
                string action = (string)parameters["action"] ?? "status";
                if(action == "request")
                {
                    if(listening) throw new InvalidOperationException("An overlay observation is already pending; inspect or cancel it first.");
                    int cameraId = parameters.Value<int?>("camera_id") ?? 0;
                    view = SceneView.sceneViews.Cast<SceneView>().FirstOrDefault(v=>v.camera && v.camera.GetInstanceID()==cameraId);
                    wanted = parameters.Value<int?>("samples") ?? 16;
                    double seconds = parameters.Value<double?>("timeout_seconds") ?? 15;
                    if(!view || wanted < 16 || wanted > 120 || Double.IsNaN(seconds) || Double.IsInfinity(seconds) || seconds < 2 || seconds > 60)
                        throw new ArgumentException("Choose an existing Scene camera, 16..120 samples and a finite 2..60 second timeout.");
                    samples.Clear(); requestId = Guid.NewGuid().ToString("N");
                    deadline = EditorApplication.timeSinceStartup + seconds; nextRepaint = 0;
                    state = "pending"; listening = true;
                    SceneView.duringSceneGui += Observe; EditorApplication.update += Update;
                }
                else if(action == "cancel") Stop("cancelled");
                else if(action != "status") throw new ArgumentException("Use request/status/cancel.");
                return new { success=true, data=new { requestId, status=state, wanted,
                    observations=new JArray(samples), diagnosticsOnly=true,
                    nextAction="Compare projections only when pose, viewport and light world positions match. Test wide/tall/wide and moving-camera/light runs separately. Diagnose leaked temporal projection, changed transforms or depth fighting; also compare final Scene and Game images. No observed render is inferred from source checks." } };
            }
            catch(Exception error) { return new {success=false,error=error.ToString()}; }
        }
    }
}
