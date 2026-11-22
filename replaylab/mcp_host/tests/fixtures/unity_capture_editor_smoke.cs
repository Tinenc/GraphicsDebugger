using System;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using Bread.Editor;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;

[InitializeOnLoad]
public static class BreadCaptureEditorSmoke
{
    private static double next;
    private static string Root => Path.GetFullPath(Path.Combine(Application.dataPath, ".."));
    private static string StatePath => Path.Combine(Root, "smoke-state.json");
    static BreadCaptureEditorSmoke() { EditorApplication.update += Tick; }
    public static void Run()
    {
        if (!File.Exists(StatePath)) Save(new JObject { ["phase"] = "import", ["index"] = 0, ["reloads"] = 0 });
    }
    private static void Save(JObject state) { File.WriteAllText(StatePath, state.ToString()); }
    private static string Hash(string path)
    { using(var hash = SHA256.Create()) return BitConverter.ToString(hash.ComputeHash(File.ReadAllBytes(path))).Replace("-", "").ToLowerInvariant(); }
    private static JObject Object(object value) => value as JObject ?? JObject.FromObject(value);
    private static void Require(bool value, string message) { if (!value) throw new Exception(message); }
    private static void Tick()
    {
        if (!File.Exists(StatePath) || EditorApplication.isCompiling || EditorApplication.isUpdating || EditorApplication.timeSinceStartup < next) return;
        next = EditorApplication.timeSinceStartup + 1;
        var state = JObject.Parse(File.ReadAllText(StatePath));
        if ((string)state["phase"] == "done" || (string)state["phase"] == "failed") return;
        try
        {
            var config = JObject.Parse(File.ReadAllText(Path.Combine(Root, "smoke-config.json")));
            var index = (int)state["index"];
            var request = (string)config["requests"][index];
            var args = new JObject { ["request_path"] = request, ["request_file_sha256"] = Hash(request) };
            if ((string)state["phase"] == "import")
            {
                var receipt = Object(BreadCaptureImporter.HandleCommand(args));
                File.WriteAllText(Path.Combine(Root, "import-start-" + index + ".json"), receipt.ToString());
                Require((string)receipt["status"] != "failed", receipt.ToString());
                state["phase"] = "poll"; Save(state); return;
            }
            if ((string)state["phase"] == "poll")
            {
                args["action"] = "status";
                var receipt = Object(BreadCaptureImporter.HandleCommand(args));
                var status = (string)receipt["status"];
                if (status == "pending") return;
                if (status == "interrupted") { state["phase"] = "import"; state["reloads"] = (int)state["reloads"] + 1; Save(state); return; }
                Require(status == "partial" || status == "imported", receipt.ToString());
                File.WriteAllText(Path.Combine(Root, "import-" + index + ".json"), receipt.ToString());
                state["phase"] = "configure"; Save(state); return;
            }
            if ((string)state["phase"] == "configure")
            {
                var requestJson = JObject.Parse(File.ReadAllText(request));
                var patch = (JObject)config["configuration"].DeepClone();
                if (index == 1)
                {
                    patch["rendererFeatures"] = new JArray(new JObject { ["rendererDataAssetPath"] = (string)requestJson["destination"] + "/BreadRenderer.asset", ["type"] = "BreadSmokeFeature" });
                    patch["renderPlan"] = config["urpPlan"].DeepClone();
                }
                var applied = Object(BreadApplyCaptureConfiguration.HandleCommand(new JObject { ["request_path"] = request, ["configuration"] = patch }));
                Require(applied.Value<bool>("success"), applied.ToString());
                File.WriteAllText(Path.Combine(Root, "configured-" + index + ".json"), applied.ToString());
                state["phase"] = "after-reload"; Save(state); EditorUtility.RequestScriptReload(); return;
            }
            if ((string)state["phase"] == "after-reload")
            {
                var observed = Object(BreadInspectCapture.HandleCommand(new JObject { ["request_path"] = request }));
                Require(observed.Value<bool>("success"), observed.ToString());
                var data = (JObject)observed["data"];
                Require((string)data["renderPipeline"] == (index == 0 ? "built-in" : "urp"), "Pipeline was not preserved/created as requested.");
                Require(data["checks"].Value<bool>("shaderCompile"), data.ToString());
                var bindings = (JArray)data["runtimeBindings"]["readback"];
                Require(bindings.Count >= 4, "Persistent bindings disappeared after reload.");
                Require((float)bindings.First(x => (string)x["name"] == "_CaptureMatrix")["value"][0] == 2, "Matrix readback changed.");
                Require((float)bindings.First(x => (string)x["name"] == "_CaptureArray")["value"][2] == 0.75f, "Array readback changed.");
                Require((uint)bindings.First(x => (string)x["name"] == "_CaptureBuffer")["value"]["previewWords"][0] == 0xDEADBEEFu, "Buffer GPU bytes changed.");
                File.WriteAllText(Path.Combine(Root, "reloaded-" + index + ".json"), observed.ToString());
                // A foreign loaded scene must not enter the isolated capture.
                var foreign = EditorSceneManager.OpenScene("Assets/Foreign.unity", OpenSceneMode.Additive);
                var blocker = GameObject.CreatePrimitive(PrimitiveType.Cube); SceneManager.MoveGameObjectToScene(blocker, foreign);
                blocker.transform.position = new Vector3(0, 0, -1); blocker.transform.localScale = Vector3.one * 100;
                var rendered = Object(BreadRenderCapture.HandleCommand(new JObject { ["request_path"] = request, ["width"] = 64, ["height"] = 64 }));
                EditorSceneManager.CloseScene(foreign, true);
                Require(rendered.Value<bool>("success"), rendered.ToString());
                File.WriteAllText(Path.Combine(Root, "rendered-" + index + ".json"), rendered.ToString());
                var image = new Texture2D(2, 2); ImageConversion.LoadImage(image, File.ReadAllBytes((string)rendered["data"]["image"]["path"]));
                var center = image.GetPixel(32, 32); var corner = image.GetPixel(0, 0); UnityEngine.Object.DestroyImmediate(image);
                Require(index == 0 ? Mathf.Abs(center.r - 0.2f) < 0.01f && corner.r < 0.01f : Mathf.Abs(center.r - 0.125f) < 0.01f && Mathf.Abs(corner.r - 0.125f) < 0.01f,
                    "The captured pixels do not show the actual draw/feature, or a foreign scene contaminated the image.");
                var repeated = Object(BreadInspectCapture.HandleCommand(new JObject { ["request_path"] = request }));
                Require(repeated.Value<bool>("success"), repeated.ToString());
                Require(JToken.DeepEquals(data["material"], repeated["data"]["material"]), "Material snapshot changed across preview reopen.");
                Require(JToken.DeepEquals(data["camera"], repeated["data"]["camera"]), "Camera snapshot changed after rendering into a temporary target.");
                File.WriteAllText(Path.Combine(Root, "after-render-inspect-" + index + ".json"), repeated.ToString());
                var reapplied = Object(BreadApplyCaptureConfiguration.HandleCommand(new JObject { ["request_path"] = request }));
                Require(reapplied.Value<bool>("success"), "Default repair must reapply the persisted configuration: " + reapplied);
                File.WriteAllText(Path.Combine(Root, "reapplied-" + index + ".json"), reapplied.ToString());
                if (index == 1)
                {
                    Require(((JArray)rendered["data"]["passExecution"]).Any(pass => (string)pass["passId"] == "smoke-feature"), "RendererFeature never executed.");
                    Require(rendered["data"]["rendererFeatures"][0].Value<bool>("activeRendererAssignmentVerified"), "Feature was not assigned to the active camera renderer.");
                    var selection = new JObject { ["kind"] = "global-texture", ["name"] = "_BreadSmokeOutput" };
                    var mapping = new JObject { ["channel"] = "r", ["range_mode"] = "explicit", ["black_point"] = 0, ["white_point"] = 0.5, ["flip_y"] = true, ["value_decode"] = "native" };
                    var global = Object(BreadRenderCapture.HandleCommand(new JObject { ["request_path"] = request, ["width"] = 64, ["height"] = 64,
                        ["target_output"] = selection, ["display_mapping"] = mapping }));
                    Require(global.Value<bool>("success"), global.ToString());
                    Require(JToken.DeepEquals(global["data"]["displayMapping"], mapping) && JToken.DeepEquals(global["data"]["targetOutput"], selection), "Display/output binding changed.");
                    Require((string)global["data"]["targetOutputEvidence"]["producerPassId"] == "smoke-feature", "Global output has no current producer.");
                    var globalImage = new Texture2D(2, 2); ImageConversion.LoadImage(globalImage, File.ReadAllBytes((string)global["data"]["image"]["path"]));
                    var gray = globalImage.GetPixel(32, 32); UnityEngine.Object.DestroyImmediate(globalImage);
                    Require(Mathf.Abs(gray.r - 0.25f) < 0.01f && gray.r == gray.g && gray.g == gray.b, "Global explicit channel/range mapping is incorrect.");
                    File.WriteAllText(Path.Combine(Root, "global-output.json"), global.ToString());
                    var rendererPath = (string)rendered["data"]["rendererFeatures"][0]["rendererDataAssetPath"];
                    var feature = AssetDatabase.LoadAllAssetsAtPath(rendererPath).First(asset => asset != null && asset.GetType().FullName == "BreadSmokeFeature");
                    feature.GetType().GetMethod("SetActive").Invoke(feature, new object[] { false }); EditorUtility.SetDirty(feature); AssetDatabase.SaveAssets();
                    var stale = Object(BreadRenderCapture.HandleCommand(new JObject { ["request_path"] = request, ["width"] = 64, ["height"] = 64,
                        ["target_output"] = selection, ["display_mapping"] = mapping }));
                    Require(!stale.Value<bool>("success"), "A previous render's global texture must not be accepted after its producer stops.");
                    File.WriteAllText(Path.Combine(Root, "stale-output-rejected.json"), stale.ToString());
                    feature.GetType().GetMethod("SetActive").Invoke(feature, new object[] { true }); EditorUtility.SetDirty(feature); AssetDatabase.SaveAssets();
                    var unsupported = Object(BreadRenderCapture.HandleCommand(new JObject { ["request_path"] = request, ["width"] = 64, ["height"] = 64,
                        ["display_mapping"] = new JObject { ["channel"] = "depth", ["value_decode"] = "native" } }));
                    Require(!unsupported.Value<bool>("success"), "Unsupported depth capture must not produce equivalent evidence.");
                    File.WriteAllText(Path.Combine(Root, "unsupported-depth.json"), unsupported.ToString());
                    var fresh = Object(BreadRenderCapture.HandleCommand(new JObject { ["request_path"] = request, ["width"] = 64, ["height"] = 64,
                        ["target_output"] = selection, ["display_mapping"] = mapping }));
                    Require(fresh.Value<bool>("success"), fresh.ToString());
                    File.WriteAllText(Path.Combine(Root, "fresh-render.json"), fresh.ToString());
                    var finalInspection = Object(BreadInspectCapture.HandleCommand(new JObject { ["request_path"] = request }));
                    Require(finalInspection.Value<bool>("success"), finalInspection.ToString());
                    File.WriteAllText(Path.Combine(Root, "fresh-inspect.json"), finalInspection.ToString());
                    state["phase"] = "done"; Save(state); EditorApplication.Exit(0);
                }
                else { state["index"] = 1; state["phase"] = "import"; Save(state); }
            }
        }
        catch (Exception error)
        { state["phase"] = "failed"; state["error"] = error.ToString(); Save(state); Debug.LogException(error); EditorApplication.Exit(1); }
    }
}
