using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;

namespace Bread.Editor
{
    [McpForUnityTool("bread_resize_game_view", Description="List/request/restore an existing Game window for Free Aspect resize checks without focus. Uses guarded editor-version reflection to verify the actual Free Aspect mode. Window bounds are UI points, not render pixels: inspect actual camera extents and final outputs after settling. Does not create windows, edit scenes or save assets; restore the saved mode/bounds in finally.")]
    public static class BreadGameViewResize
    {
        sealed class Original { public Rect position; public int sizeIndex; }
        static readonly Dictionary<int, Original> original = new Dictionary<int, Original>();
        const BindingFlags Instance = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
        static EditorWindow[] Windows() => Resources.FindObjectsOfTypeAll<EditorWindow>().Where(
            window=>window && window.GetType().FullName=="UnityEditor.GameView").ToArray();
        static PropertyInfo SizeIndex(EditorWindow window)
        {
            var property=window.GetType().GetProperty("selectedSizeIndex",Instance);
            if(property==null || !property.CanRead || !property.CanWrite || property.PropertyType!=typeof(int))
                throw new NotSupportedException("This Unity version does not expose a supported Game size selector. Use an equivalent editor action and observe the actual output.");
            return property;
        }
        internal static bool? FreeAspect(EditorWindow window)
        {
            // Observed properties, not a localized menu-string comparison.
            // Reference: UnityCsReference/Editor/Mono/GameView/{GameView,GameViewSize}.cs
            try {
                var value=window.GetType().GetProperty("currentGameViewSize",Instance)?.GetValue(window);
                var property=value?.GetType().GetProperty("isFreeAspectRatio",Instance);
                return property?.GetValue(value) is bool free ? (bool?)free : null;
            } catch(Exception) { return null; }
        }
        internal static JObject[] Observations() => Windows().Select(window=>new JObject {
            ["windowId"]=window.GetInstanceID(),
            ["windowBounds"]=new JArray(window.position.x,window.position.y,window.position.width,window.position.height),
            ["freeAspectRatio"]=FreeAspect(window).HasValue ? (JToken)new JValue(FreeAspect(window).Value) : JValue.CreateNull(),
            ["scope"]="Editor window mode/bounds; actual rendering camera pixels and final outputs are separate observations"
        }).ToArray();
        static void Restore(EditorWindow window, Original saved)
        { SizeIndex(window).SetValue(window,saved.sizeIndex); window.position=saved.position; window.Repaint(); }
        static BreadGameViewResize()
        {
            AssemblyReloadEvents.beforeAssemblyReload += RestoreAll;
            EditorApplication.quitting += RestoreAll;
        }
        static void RestoreAll()
        {
            foreach(var window in Windows())
                if(original.TryGetValue(window.GetInstanceID(),out var saved))
                    try { Restore(window,saved); } catch(Exception) { /* Caller still receives explicit restore failures. */ }
            original.Clear();
        }
        public static object HandleCommand(JObject p)
        {
            try
            {
                string action=p.Value<string>("action")??"list";
                if(action=="list") return new {success=true,data=Observations()};
                int id=p.Value<int>("window_id");
                var window=Windows().FirstOrDefault(value=>value.GetInstanceID()==id);
                if(!window) throw new ArgumentException("List and select an existing Game window_id.");
                var selector=SizeIndex(window);
                if(action=="restore")
                {
                    if(!original.TryGetValue(id,out var saved)) throw new ArgumentException("No original mode/bounds were retained by this helper.");
                    Restore(window,saved);original.Remove(id);
                }
                else if(action=="request")
                {
                    int width=p.Value<int>("window_width"),height=p.Value<int>("window_height");
                    if(width<128||height<128||width>4096||height>4096) throw new ArgumentException("Use 128..4096 UI points for both window dimensions.");
                    var before=new Original { position=window.position,sizeIndex=(int)selector.GetValue(window) };
                    bool first=!original.ContainsKey(id);
                    if(first) original[id]=before;
                    try
                    {
                        // Free Aspect is the first built-in size in supported
                        // Unity editors; verify the resulting object explicitly.
                        if(FreeAspect(window)!=true) selector.SetValue(window,0);
                        if(FreeAspect(window)!=true) throw new NotSupportedException("Free Aspect could not be observed; the previous window mode will be restored.");
                        var position=window.position;position.width=width;position.height=height;window.position=position;
                    }
                    catch { Restore(window,before);if(first) original.Remove(id);throw; }
                }
                else throw new ArgumentException("Use list, request or restore.");
                window.Repaint();EditorApplication.QueuePlayerLoopUpdate();
                return new {success=true,data=new {windowId=id,action,windows=Observations(),
                    nextAction="Capture this Game camera's actual settled final output at original/wide/tall/return extents. Restore saved mode/bounds in finally. UI bounds, Zoom or Scale changes alone do not prove render resolution changed; never focus the editor or substitute Scene captures."}};
            }
            catch(Exception error) { return new {success=false,error=error.Message}; }
        }
    }
}
