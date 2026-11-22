using System;
using System.Collections.Generic;
using System.Linq;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;

namespace Bread.Editor
{
    [McpForUnityTool("bread_resize_scene_view", Description="Resize an explicit existing Scene window without focusing it or overriding its camera. Use request with camera_id/window_width/window_height, capture its actual settled output, then restore in finally. Window bounds are UI points; query actual camera pixel dimensions after repaint. Does not save assets or move cameras.")]
    public static class BreadSceneViewResize
    {
        static readonly Dictionary<int,Rect> original = new Dictionary<int,Rect>();
        public static object HandleCommand(JObject p)
        {
            try
            {
                int id=p.Value<int>("camera_id");
                var window=SceneView.sceneViews.Cast<SceneView>().FirstOrDefault(v=>v.camera && v.camera.GetInstanceID()==id);
                if (!window) throw new ArgumentException("Inspect cameras and select an existing Scene camera.");
                string action=p.Value<string>("action")??"request";
                if (action=="restore")
                {
                    if (!original.TryGetValue(id,out var position)) throw new ArgumentException("No original window bounds were saved by this helper.");
                    window.position=position; original.Remove(id);
                }
                else if (action=="request")
                {
                    int width=p.Value<int>("window_width"),height=p.Value<int>("window_height");
                    if (width<128 || height<128 || width>4096 || height>4096) throw new ArgumentException("Use bounded window dimensions from 128 to 4096 UI points.");
                    if (!original.ContainsKey(id)) original[id]=window.position;
                    var position=window.position; position.width=width;position.height=height;window.position=position;
                }
                else throw new ArgumentException("Use request or restore.");
                window.Repaint();
                return new {success=true,data=new {cameraId=id,action,windowBounds=window.position,
                    cameraPixelWidth=window.camera.pixelWidth,cameraPixelHeight=window.camera.pixelHeight,
                    observation="Repaint requested; inspect/capture actual settled dimensions. Docking may constrain bounds; requested dimensions alone are not evidence."}};
            }
            catch(Exception error){return new {success=false,error=error.Message};}
        }
    }
}
