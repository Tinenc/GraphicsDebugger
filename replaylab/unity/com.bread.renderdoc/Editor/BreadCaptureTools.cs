using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.SceneManagement;

namespace Bread.Editor
{
    [McpForUnityTool("bread_refresh_assets", Description = "Import Agent-authored project assets and report actual compiler/update state; retry check_only after domain reload.")]
    public static class BreadRefreshAssets
    {
        public static object HandleCommand(JObject parameters)
        {
            if (!EditorApplication.isCompiling && !EditorApplication.isUpdating && parameters.Value<bool?>("check_only") != true)
                AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);
            bool busy = EditorApplication.isCompiling || EditorApplication.isUpdating;
            return new { success = true, data = new { status = busy ? "pending" : EditorUtility.scriptCompilationFailed ? "needs-repair" : "ready",
                projectPath = Path.GetFullPath(Path.Combine(Application.dataPath, "..")),
                isCompiling = EditorApplication.isCompiling, isUpdating = EditorApplication.isUpdating,
                compilationFailed = EditorUtility.scriptCompilationFailed, retrySameRequest = busy } };
        }
    }

    internal static class BreadCaptureTools
    {
        internal static string Project => Path.GetFullPath(Path.Combine(Application.dataPath, ".."));

        internal static JObject Request(JObject args, out string folder, out string requestPath)
        {
            requestPath = Path.GetFullPath((string)args["request_path"] ?? "");
            var allowed = Path.Combine(Project, "Library", "Bread", "Requests") + Path.DirectorySeparatorChar;
            if (!requestPath.StartsWith(allowed, StringComparison.OrdinalIgnoreCase) || !File.Exists(requestPath))
                throw new ArgumentException("Request must belong to this Unity project.");
            var request = JObject.Parse(File.ReadAllText(requestPath));
            var id = (string)request["requestSha256"];
            if (id == null || id.Length != 64 || id.Any(c => !Uri.IsHexDigit(c)) || Path.GetFileName(requestPath) != id + ".json" ||
                !String.Equals(Path.GetFullPath((string)request["projectPath"]), Project, StringComparison.OrdinalIgnoreCase))
                throw new ArgumentException("Request identity changed.");
            folder = "Assets/BreadImports/" + id.Substring(0, 24);
            if ((string)request["destination"] != folder) throw new ArgumentException("Request destination changed.");
            foreach (var item in new[] { requestPath, Path.GetFullPath(folder) })
                for (var part = item; !String.IsNullOrEmpty(part); part = Path.GetDirectoryName(part))
                    if ((File.Exists(part) || Directory.Exists(part)) && (File.GetAttributes(part) & FileAttributes.ReparsePoint) != 0)
                        throw new ArgumentException("Redirected capture paths are unsupported.");
            var receiptPath = Path.ChangeExtension(requestPath, ".receipt.json");
            if (!File.Exists(receiptPath) || (string)JObject.Parse(File.ReadAllText(receiptPath))["requestFileSha256"] != BreadCaptureConfiguration.Hash(requestPath))
                throw new ArgumentException("The immutable request has no matching successful import receipt.");
            if (EditorApplication.isCompiling || EditorApplication.isUpdating)
                throw new InvalidOperationException("Unity is compiling/importing. Retry the same request after compilation finishes.");
            return request;
        }

        internal static JObject Configuration(JObject request, string path)
        {
            var sidecar = Path.ChangeExtension(path, ".configuration.json");
            if (!File.Exists(sidecar))
            {
                var initial = (JObject)(request["options"]?.DeepClone() ?? new JObject());
                // Host-resolved physical bytes and automatic matrix/array
                // mappings are authoritative over the undecoded transfer spec.
                initial["runtimeBindings"] = request["runtimeBindings"]?.DeepClone() ?? new JArray();
                return initial;
            }
            var saved = JObject.Parse(File.ReadAllText(sidecar));
            if ((string)saved["requestSha256"] != (string)request["requestSha256"]) throw new InvalidOperationException("Configuration identity changed.");
            return (JObject)saved["configuration"];
        }

        internal static JObject Observe(JObject request, string folder, Scene scene, JObject configuration)
        {
            var gaps = new JArray();
            var roots = scene.GetRootGameObjects();
            var effectRoute = (string)request["executionKind"] == "effect";
            var root = roots.FirstOrDefault(x => effectRoute ? x.GetComponent<Bread.BreadCaptureEffect>() != null : x.GetComponent<MeshRenderer>() != null);
            var effect = root?.GetComponent<Bread.BreadCaptureEffect>();
            var camera = roots.SelectMany(x => x.GetComponentsInChildren<Camera>(true)).FirstOrDefault();
            var renderer = root == null ? null : root.GetComponent<MeshRenderer>();
            var material = renderer == null ? null : renderer.sharedMaterial;
            var shader = material?.shader;
            var bindings = BreadCaptureConfiguration.BindingSnapshot(root?.GetComponent<Bread.BreadCaptureBindings>());
            if (((JArray)bindings["errors"]).Count != 0) BreadCaptureConfiguration.Gap(gaps, "runtime_binding_failed", bindings["errors"].ToString());
            var requested = (string)request["pipelinePolicy"]?["targetPipeline"];
            var actual = BreadCaptureConfiguration.Pipeline();
            var compatible = String.IsNullOrEmpty(requested) || requested == actual;
            if (!compatible) BreadCaptureConfiguration.Gap(gaps, "pipeline_mismatch", requested + " != " + actual);
            var diagnostics = new JArray();
            if (shader != null)
                foreach (var message in ShaderUtil.GetShaderMessages(shader)) diagnostics.Add(new JObject { ["message"] = message.message, ["severity"] = message.severity.ToString(), ["line"] = message.line });
            bool implementationCompiled = !EditorUtility.scriptCompilationFailed;
            var implementationAssets = effectRoute && effect != null
                ? new JArray(effect.passes.Where(pass => pass.kind != "copy").Select(pass => new JObject {
                    ["path"] = pass.implementationAsset, ["kind"] = pass.kind == "fullscreen" ? "shader" : pass.kind }))
                : request["assets"] as JArray ?? new JArray();
            foreach (JObject asset in implementationAssets)
            {
                var assetPath = BreadEffectConfiguration.AssetPath(folder, (string)asset["path"]);
                if ((string)asset["kind"] == "shader")
                {
                    var implementation = AssetDatabase.LoadAssetAtPath<Shader>(assetPath);
                    implementationCompiled &= implementation != null && !ShaderUtil.ShaderHasError(implementation);
                    if (implementation != null) foreach (var message in ShaderUtil.GetShaderMessages(implementation))
                        diagnostics.Add(new JObject { ["assetPath"] = assetPath, ["message"] = message.message, ["severity"] = message.severity.ToString(), ["line"] = message.line });
                }
                if ((string)asset["kind"] == "compute")
                {
                    var compute = AssetDatabase.LoadAssetAtPath<ComputeShader>(assetPath);
                    implementationCompiled &= compute != null;
                    if (compute != null) foreach (var message in ShaderUtil.GetComputeShaderMessages(compute))
                    {
                        implementationCompiled &= message.severity.ToString() != "Error";
                        diagnostics.Add(new JObject { ["assetPath"] = assetPath, ["message"] = message.message, ["severity"] = message.severity.ToString(), ["line"] = message.line });
                    }
                }
            }
            var required = configuration["renderState"]?["requiredPasses"] as JArray;
            if (required != null && material != null) foreach (var pass in required.Select(x => (string)x))
                if (material.FindPass(pass) < 0) BreadCaptureConfiguration.Gap(gaps, "required_shader_pass_missing", pass);
                else if (!material.GetShaderPassEnabled(pass)) BreadCaptureConfiguration.Gap(gaps, "required_shader_pass_disabled", pass);
            var features = BreadCaptureConfiguration.Features(configuration["rendererFeatures"] as JArray, false, gaps, camera);
            if (configuration["scene"]?["camera"] == null) BreadCaptureConfiguration.Gap(gaps, "capture_camera_not_configured", "Configure a matching captured view before comparison.");
            if (!effectRoute && configuration["renderState"] == null) BreadCaptureConfiguration.Gap(gaps, "draw_state_not_reviewed", "Provide the capture-to-Shader state mapping.");
            if (effectRoute && (effect == null || effect.passes.Length == 0)) BreadCaptureConfiguration.Gap(gaps, "effect_implementation_required", "Add executable compute, fullscreen or custom passes.");
            if (effect?.lastError != null) BreadCaptureConfiguration.Gap(gaps, "effect_input_upload_failed", effect.lastError);
            var textureChecks = new JArray();
            foreach (JObject texture in request["textures"] as JArray ?? new JArray())
            {
                var property = (string)texture["property"];
                var propertyIndex = shader == null ? -1 : shader.FindPropertyIndex(property);
                var assigned = material != null && propertyIndex >= 0 && shader.GetPropertyType(propertyIndex) == ShaderPropertyType.Texture ? material.GetTexture(property) : null;
                if (effectRoute && effect != null)
                {
                    var expectedIndex = ((JArray)request["textures"]).IndexOf(texture);
                    var expected = AssetDatabase.LoadAssetAtPath<Texture>(folder + "/Texture_" + expectedIndex + (string)texture["extension"]);
                    assigned = effect.resources.Any(resource => resource.sourceTexture == expected) ? expected : null;
                }
                var texturePath = assigned == null ? "" : AssetDatabase.GetAssetPath(assigned);
                var importer = AssetImporter.GetAtPath(texturePath) as TextureImporter;
                var digest = File.Exists(texturePath) ? BreadCaptureConfiguration.Hash(texturePath) : null;
                var valid = assigned != null && !String.IsNullOrEmpty(texturePath) && importer != null && digest == (string)texture["sha256"];
                if (importer != null)
                {
                    valid &= importer.sRGBTexture == ((string)texture["colorSpace"] == "sRGB") &&
                        (importer.textureType == TextureImporterType.NormalMap) == ((bool?)texture["normalMap"] == true);
                    if (texture["sampler"] is JObject sampler) foreach (var setting in sampler.Properties())
                    {
                        if (setting.Name == "filterMode") valid &= importer.filterMode.ToString() == (string)setting.Value;
                        if (setting.Name == "wrapU") valid &= importer.wrapModeU.ToString() == (string)setting.Value;
                        if (setting.Name == "wrapV") valid &= importer.wrapModeV.ToString() == (string)setting.Value;
                        if (setting.Name == "anisoLevel") valid &= importer.anisoLevel == (int)setting.Value;
                    }
                }
                if (!valid) BreadCaptureConfiguration.Gap(gaps, "texture_binding_missing", property);
                textureChecks.Add(new JObject { ["property"] = property, ["path"] = texturePath, ["bound"] = valid, ["sha256"] = digest,
                    ["expectedSha256"] = texture["sha256"], ["sRGBTexture"] = importer?.sRGBTexture,
                    ["textureType"] = importer?.textureType.ToString() });
            }
            var checks = new JObject { ["meshImport"] = effectRoute && effect != null || root?.GetComponent<MeshFilter>()?.sharedMesh != null,
                ["shaderCompile"] = (effectRoute ? effect != null && effect.passes.Length > 0 : shader != null && !ShaderUtil.ShaderHasError(shader)) && implementationCompiled,
                ["materialImport"] = effectRoute && effect != null || material != null,
                ["implementationCompiled"] = implementationCompiled,
                ["textureImport"] = textureChecks.All(texture => (bool)texture["bound"]),
                ["sceneIntegrity"] = scene.IsValid() && scene.isLoaded && File.Exists(folder + "/Preview.unity") && root != null && camera != null,
                ["cameraPresent"] = camera != null, ["pipelineCompatible"] = compatible,
                ["runtimeBindingsApplied"] = ((JArray)bindings["errors"]).Count == 0 };
            var dependencies = Dependencies(folder, configuration, gaps);
            return new JObject { ["schema"] = "bread-unity-capture-observation/v1", ["status"] = gaps.Count == 0 && checks.Properties().All(x => (bool)x.Value) ? "inspected" : "needs-repair",
                ["projectPath"] = Project,
                ["requestSha256"] = request["requestSha256"], ["packageManifestSha256"] = request["packageManifestSha256"],
                ["scenePath"] = folder + "/Preview.unity", ["renderPipeline"] = actual,
                ["targetGraphicsAPI"] = SystemInfo.graphicsDeviceType.ToString(), ["colorSpace"] = QualitySettings.activeColorSpace.ToString(),
                ["unityVersion"] = Application.unityVersion, ["operatingSystem"] = SystemInfo.operatingSystem,
                ["files"] = dependencies["files"], ["externalDependencies"] = dependencies["externalDependencies"], ["virtualDependencies"] = dependencies["virtualDependencies"],
                ["material"] = BreadCaptureConfiguration.MaterialSnapshot(material), ["runtimeBindings"] = bindings,
                ["executionKind"] = effectRoute ? "effect" : "draw", ["geometryApplicable"] = !effectRoute,
                ["effect"] = BreadEffectConfiguration.Snapshot(effect),
                ["textureBindings"] = textureChecks,
                ["systemInterfaces"] = new JArray((request["runtimeBindings"] as JArray ?? new JArray()).OfType<JObject>().Where(entry =>
                    (string)entry["kind"] == "system" || (string)entry["classification"] == "system").Select(entry => entry.DeepClone())),
                ["camera"] = camera == null ? null : BreadCaptureConfiguration.CameraSnapshot(camera),
                ["object"] = root == null ? null : new JObject { ["position"] = new JArray(root.transform.position.x, root.transform.position.y, root.transform.position.z),
                    ["rotation"] = new JArray(root.transform.rotation.x, root.transform.rotation.y, root.transform.rotation.z, root.transform.rotation.w),
                    ["scale"] = new JArray(root.transform.localScale.x, root.transform.localScale.y, root.transform.localScale.z) },
                ["lights"] = new JArray(roots.SelectMany(x => x.GetComponentsInChildren<Light>(true)).Select(light => new JObject { ["name"] = light.name, ["type"] = light.type.ToString(),
                    ["intensity"] = light.intensity, ["range"] = light.range, ["spotAngle"] = light.spotAngle, ["shadows"] = light.shadows.ToString(),
                    ["color"] = new JArray(light.color.r, light.color.g, light.color.b, light.color.a) })),
                ["components"] = new JArray(roots.SelectMany(x => x.GetComponentsInChildren<Component>(true)).Where(component => component != null).Select(component => new JObject {
                    ["gameObject"] = component.gameObject.name, ["type"] = component.GetType().FullName,
                    ["serialized"] = BreadCaptureConfiguration.StableSerialization(component) })),
                ["rendererFeatures"] = features, ["renderPlan"] = configuration["renderPlan"]?.DeepClone(),
                ["requestedRenderState"] = configuration["renderState"]?.DeepClone(), ["gpuDrawStateVerified"] = false,
                ["checks"] = checks, ["gaps"] = gaps, ["shaderDiagnostics"] = diagnostics, ["visualEquivalenceVerified"] = false };
        }

        private static JObject Dependencies(string folder, JObject configuration, JArray gaps)
        {
            var files = new SortedSet<string>(Directory.GetFiles(folder, "*", SearchOption.AllDirectories).Select(Path.GetFullPath), StringComparer.Ordinal);
            var seeds = new List<string> { folder + "/Preview.unity" };
            var active = GraphicsSettings.currentRenderPipeline;
            if (active != null) seeds.Add(AssetDatabase.GetAssetPath(active));
            if (configuration["rendererFeatures"] is JArray features)
                seeds.AddRange(features.OfType<JObject>().Select(item => (string)item["rendererDataAssetPath"]).Where(path => !String.IsNullOrEmpty(path)));
            if (configuration["effect"]?["passes"] is JArray effectPasses)
                seeds.AddRange(effectPasses.OfType<JObject>()
                    .Select(pass => (string)pass["kind"] == "copy" ? BreadEffectConfiguration.CopyImplementation
                        : BreadEffectConfiguration.AssetPath(folder, (string)pass["asset"])));
            var external = new JArray(); var virtualFiles = new JArray();
            var assetPaths = new Dictionary<string, string>(StringComparer.Ordinal);
            var projectPrefix = Project.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar) + Path.DirectorySeparatorChar;
            foreach (var dependency in AssetDatabase.GetDependencies(seeds.Distinct().ToArray(), true).OrderBy(path => path, StringComparer.Ordinal))
            {
                var physical = Path.GetFullPath(dependency);
                var package = UnityEditor.PackageManager.PackageInfo.FindForAssetPath(dependency);
                if (!File.Exists(physical) && package != null && dependency.StartsWith("Packages/" + package.name + "/", StringComparison.Ordinal))
                    physical = Path.GetFullPath(Path.Combine(package.resolvedPath, dependency.Substring(("Packages/" + package.name + "/").Length)));
                if (!File.Exists(physical))
                {
                    if (dependency.StartsWith("Resources/unity_builtin", StringComparison.Ordinal) || dependency.StartsWith("Library/unity", StringComparison.Ordinal))
                        virtualFiles.Add(new JObject { ["assetPath"] = dependency, ["unityVersion"] = Application.unityVersion });
                    else BreadCaptureConfiguration.Gap(gaps, "asset_dependency_unavailable", dependency);
                    continue;
                }
                if (physical.StartsWith(projectPrefix, StringComparison.OrdinalIgnoreCase))
                {
                    files.Add(physical); assetPaths[physical] = dependency;
                    if (File.Exists(physical + ".meta")) { files.Add(physical + ".meta"); assetPaths[physical + ".meta"] = dependency + ".meta"; }
                }
                else if (package != null && physical.StartsWith(Path.GetFullPath(package.resolvedPath).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar) + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
                    foreach (var path in new[] { physical, physical + ".meta" }.Where(File.Exists))
                        external.Add(new JObject { ["path"] = path, ["sha256"] = BreadCaptureConfiguration.Hash(path),
                            ["assetPath"] = dependency + (path == physical + ".meta" ? ".meta" : ""),
                            ["package"] = new JObject { ["name"] = package.name, ["version"] = package.version,
                                ["source"] = package.source.ToString(), ["resolvedPath"] = Path.GetFullPath(package.resolvedPath) },
                            ["authority"] = "Unity-PackageInfo-GetDependencies" });
                else BreadCaptureConfiguration.Gap(gaps, "external_package_dependency_unbound", dependency);
            }
            foreach (var path in new[] { "ProjectSettings/GraphicsSettings.asset", "ProjectSettings/QualitySettings.asset", "ProjectSettings/ProjectSettings.asset",
                "ProjectSettings/ProjectVersion.txt", "Packages/manifest.json", "Packages/packages-lock.json" })
                if (File.Exists(path)) files.Add(Path.GetFullPath(path));
            return new JObject { ["files"] = new JArray(files.Select(path => {
                    var row = new JObject { ["path"] = path, ["sha256"] = BreadCaptureConfiguration.Hash(path) };
                    if (assetPaths.TryGetValue(path, out var assetPath)) row["assetPath"] = assetPath;
                    return row;
                })),
                ["externalDependencies"] = external, ["virtualDependencies"] = virtualFiles };
        }

        internal static object Run(JObject args, string action)
        {
            Scene scene = default;
            var previous = SceneManager.GetActiveScene();
            var openPreview = typeof(EditorSceneManager).GetMethod("OpenPreviewScene", BindingFlags.Public | BindingFlags.Static,
                null, new[] { typeof(string) }, null);
            bool preview = action != "configure" && openPreview != null;
            bool opened = false;
            try
            {
                var request = Request(args, out var folder, out var requestPath);
                var path = folder + "/Preview.unity";
                if (!File.Exists(path)) throw new InvalidOperationException("Import or create the isolated Preview.unity scene first.");
                var configuration = Configuration(request, requestPath);
                if (preview) { scene = (Scene)openPreview.Invoke(null, new object[] { path }); opened = true; }
                else
                {
                    scene = SceneManager.GetSceneByPath(path);
                    if (action == "configure" && scene.IsValid() && scene.isLoaded && scene.isDirty) throw new InvalidOperationException("The imported scene has unsaved edits; preserve them before applying a repair.");
                    if (!scene.IsValid() || !scene.isLoaded) { scene = EditorSceneManager.OpenScene(path, OpenSceneMode.Additive); opened = true; }
                }
                if (action == "configure")
                {
                    var patch = args["configuration"] as JObject ?? new JObject();
                    foreach (var property in patch.Properties())
                    {
                        if (!new[] { "scene", "materialProperties", "runtimeBindings", "renderState", "rendererFeatures", "renderPlan", "effect" }.Contains(property.Name))
                            throw new ArgumentException("Unsupported configuration field: " + property.Name);
                        configuration[property.Name] = property.Value.DeepClone();
                    }
                    var roots = scene.GetRootGameObjects();
                    var effectRoute = (string)request["executionKind"] == "effect";
                    var root = roots.FirstOrDefault(x => effectRoute ? x.GetComponent<Bread.BreadCaptureEffect>() != null : x.GetComponent<MeshRenderer>() != null);
                    var camera = roots.SelectMany(x => x.GetComponentsInChildren<Camera>(true)).FirstOrDefault();
                    if (root == null || camera == null) throw new InvalidOperationException("Imported mesh/camera is missing.");
                    var renderer = root.GetComponent<MeshRenderer>();
                    var material = renderer == null ? null : renderer.sharedMaterial;
                    var repairGaps = new JArray();
                    BreadCaptureConfiguration.Scene(root, camera, scene, configuration["scene"] as JObject, repairGaps);
                    if (material != null) foreach (JObject item in request["properties"] as JArray ?? new JArray()) BreadCaptureConfiguration.Property(material, item, repairGaps);
                    if (material != null && configuration["materialProperties"] is JArray properties) foreach (JObject item in properties) BreadCaptureConfiguration.Property(material, item, repairGaps);
                    var runtime = new JObject();
                    foreach (JObject item in request["runtimeBindings"] as JArray ?? new JArray()) runtime[(string)item["name"] ?? (string)item["target"]] = item.DeepClone();
                    foreach (JObject item in configuration["runtimeBindings"] as JArray ?? new JArray()) runtime[(string)item["name"] ?? (string)item["target"]] = item.DeepClone();
                    configuration["runtimeBindings"] = new JArray(runtime.Properties().Select(item => item.Value));
                    if (effectRoute) BreadEffectConfiguration.Configure(root, folder, configuration["effect"] as JObject,
                        (JArray)request["textures"], (JArray)configuration["runtimeBindings"], repairGaps);
                    else BreadCaptureConfiguration.Bind(root, (JArray)configuration["runtimeBindings"], repairGaps);
                    if (effectRoute) { root.GetComponent<Bread.BreadCaptureEffect>().targetCamera = camera; EditorUtility.SetDirty(root.GetComponent<Bread.BreadCaptureEffect>()); }
                    if (material != null) BreadCaptureConfiguration.RenderState(material, configuration["renderState"] as JObject, repairGaps);
                    BreadCaptureConfiguration.Features(configuration["rendererFeatures"] as JArray, true, repairGaps, camera);
                    if (material != null) EditorUtility.SetDirty(material); AssetDatabase.SaveAssets();
                    PrefabUtility.SaveAsPrefabAsset(root, folder + "/CapturedDraw.prefab");
                    EditorSceneManager.MarkSceneDirty(scene);
                    if (!EditorSceneManager.SaveScene(scene, path)) throw new IOException("Unity did not save the repaired scene.");
                    File.WriteAllText(Path.ChangeExtension(requestPath, ".configuration.json"), new JObject { ["requestSha256"] = request["requestSha256"], ["configuration"] = configuration }.ToString());
                    var result = Observe(request, folder, scene, configuration);
                    ((JArray)result["gaps"]).Merge(repairGaps);
                    if (repairGaps.Count > 0) result["status"] = "needs-repair";
                    return new { success = true, data = result };
                }
                var observation = Observe(request, folder, scene, configuration);
                if (action == "render")
                {
                    Render(args, scene, folder, observation);
                    var after = Observe(request, folder, scene, configuration);
                    foreach (var field in new[] { "image", "passExecution", "displayMapping", "targetOutput", "targetOutputEvidence", "renderExecutionId", "renderCamera", "effectExecution", "cameraOutputObserved", "readbackSource", "cameraTarget" })
                        if (observation[field] != null) after[field] = observation[field];
                    foreach (var gap in ((JArray)observation["gaps"]).OfType<JObject>().Where(gap => (string)gap["code"] == "render_plan_pass_not_observed"))
                        ((JArray)after["gaps"]).Add(gap);
                    if (((JArray)after["gaps"]).Count > 0) after["status"] = "needs-repair";
                    observation = after;
                }
                return new { success = true, data = observation };
            }
            catch (Exception error)
            { return new { success = false, data = new { status = "needs-repair", code = "capture_" + action + "_failed", detail = error.GetBaseException().Message, diagnostic = error.ToString(), retrySameRequest = true } }; }
            finally
            {
                if (opened && scene.IsValid())
                { if (preview) EditorSceneManager.ClosePreviewScene(scene); else EditorSceneManager.CloseScene(scene, true); }
                if (previous.IsValid() && previous.isLoaded) SceneManager.SetActiveScene(previous);
            }
        }

        private static void Render(JObject args, Scene scene, string folder, JObject result)
        {
            var mapping = args["display_mapping"] as JObject ?? new JObject { ["channel"] = "rgba", ["range_mode"] = "native", ["flip_y"] = false, ["value_decode"] = "native" };
            var outputSelection = args["target_output"] as JObject ?? new JObject { ["kind"] = "camera-color" };
            var channel = (string)mapping["channel"] ?? "rgba";
            var range = (string)mapping["range_mode"] ?? "native";
            var decode = (string)mapping["value_decode"] ?? "native";
            if (!new[] { "rgba", "rgb", "r", "g", "b", "a" }.Contains(channel) || decode != "native")
                throw new NotSupportedException("unsupported_display_mapping: depth and bit reinterpretation require an explicit GPU adapter; no comparison image was generated.");
            if (range != "native" && range != "explicit") throw new NotSupportedException("unsupported_display_mapping: use a shared native or explicit fixed range; independent normalization is forbidden.");
            var black = range == "explicit" ? (double?)mapping["black_point"] ?? Double.NaN : 0;
            var white = range == "explicit" ? (double?)mapping["white_point"] ?? Double.NaN : 1;
            if (Double.IsNaN(black) || Double.IsNaN(white) || Double.IsInfinity(black) || Double.IsInfinity(white) || white <= black)
                throw new ArgumentException("Explicit display range must be finite and increasing.");
            var flip = (bool?)mapping["flip_y"] == true;
            var outputKind = (string)outputSelection["kind"] ?? "camera-color";
            if (outputKind != "camera-color" && outputKind != "global-texture") throw new NotSupportedException("unsupported_target_output: select camera-color or a named global RenderTexture.");
            var width = (int?)args["width"] ?? 1920; var height = (int?)args["height"] ?? 1080;
            if (width < 1 || height < 1 || width > 16384 || height > 16384 || (long)width * height > 32 * 1024 * 1024)
                throw new ArgumentException("Render dimensions exceed the supported range.");
            var cameraName = (string)args["camera_name"];
            var camera = scene.GetRootGameObjects().SelectMany(x => x.GetComponentsInChildren<Camera>(true)).FirstOrDefault(x => cameraName == null || x.name == cameraName);
            if (camera == null) throw new ArgumentException("Requested camera does not exist in the imported scene.");
            var output = Path.GetFullPath((string)args["output_path"] ?? Path.Combine(Project, "Library/Bread/Renders", Guid.NewGuid().ToString("N") + ".png"));
            var allowed = Path.GetFullPath(Path.Combine(Project, "Library/Bread/Renders")) + Path.DirectorySeparatorChar;
            if (!output.StartsWith(allowed, StringComparison.OrdinalIgnoreCase) || Path.GetExtension(output).ToLowerInvariant() != ".png")
                throw new ArgumentException("Render output must be a PNG under this project's Library/Bread/Renders.");
            if (File.Exists(output)) throw new IOException("Preserve the existing evidence image; choose a new output path.");
            Directory.CreateDirectory(Path.GetDirectoryName(output));
            bool cameraFloat = range == "explicit" && outputKind == "camera-color";
            var target = new RenderTexture(width, height, 24, cameraFloat ? RenderTextureFormat.ARGBFloat : RenderTextureFormat.ARGB32,
                cameraFloat ? RenderTextureReadWrite.Linear : RenderTextureReadWrite.sRGB);
            var previousActive = RenderTexture.active;
            var previousTarget = camera.targetTexture;
            var previousCameraScene = camera.scene;
            Texture2D pixels = null;
            try
            {
                target.Create(); camera.scene = scene; camera.targetTexture = target;
                Bread.BreadCapturePassEvidence.Begin(camera);
                result["renderExecutionId"] = Bread.BreadCapturePassEvidence.CurrentRenderId;
                var route = "built-in-camera-render";
                var effect = scene.GetRootGameObjects().Select(root => root.GetComponent<Bread.BreadCaptureEffect>()).FirstOrDefault(value => value != null);
                RenderTexture effectOutput = null;
                if (effect != null && outputKind != "camera-color")
                {
                    effectOutput = effect.Execute(camera); route = "bread-effect-graph";
                    result["effectExecution"] = BreadEffectConfiguration.Snapshot(effect);
                    result["effectExecution"]["renderExecutionId"] = result["renderExecutionId"];
                    result["effectExecution"]["executedIterations"] = effect.warmupFrames + 1;
                }
                else if (GraphicsSettings.currentRenderPipeline == null) camera.Render();
                else
                {
                    var requestType = typeof(RenderPipeline).GetNestedType("StandardRequest", BindingFlags.Public);
                    if (requestType == null) throw new NotSupportedException("This pipeline version has no StandardRequest. Use the connected provider's camera screenshot route.");
                    var request = Activator.CreateInstance(requestType);
                    requestType.GetField("destination").SetValue(request, target);
                    var supports = typeof(RenderPipeline).GetMethods(BindingFlags.Static | BindingFlags.Public).First(x => x.Name == "SupportsRenderRequest" && x.IsGenericMethodDefinition).MakeGenericMethod(requestType);
                    if (!(bool)supports.Invoke(null, new[] { (object)camera, request }))
                        throw new NotSupportedException("The active pipeline does not support this camera render request; use its supported screenshot/custom render route.");
                    var submit = typeof(RenderPipeline).GetMethods(BindingFlags.Static | BindingFlags.Public).First(x => x.Name == "SubmitRenderRequest" && x.IsGenericMethodDefinition).MakeGenericMethod(requestType);
                    submit.Invoke(null, new[] { (object)camera, request }); route = "srp-standard-request";
                }
                var executed = Bread.BreadCapturePassEvidence.End();
                if (effect != null && outputKind == "camera-color")
                    result["effectExecution"] = BreadEffectConfiguration.Snapshot(effect);
                var readTarget = effectOutput ?? target;
                result["cameraOutputObserved"] = outputKind == "camera-color" && readTarget == target;
                result["readbackSource"] = outputKind == "camera-color" ? "actual-camera-color-target" : "explicit-pass-output";
                result["cameraTarget"] = new JObject { ["width"] = target.width, ["height"] = target.height,
                    ["format"] = target.graphicsFormat.ToString(), ["route"] = route };
                if (outputKind == "global-texture")
                {
                    var name = (string)outputSelection["name"];
                    if (String.IsNullOrEmpty(name)) throw new ArgumentException("Global texture output requires its exact Shader property name.");
                    // CommandBuffer/RenderGraph global bindings need not be
                    // mirrored by Shader.GetGlobalTexture's managed lookup.
                    // Prefer the actual object recorded during this render.
                    readTarget = Bread.BreadCapturePassEvidence.RecordedOutput(name) ?? Shader.GetGlobalTexture(name) as RenderTexture;
                    if (readTarget == null) throw new NotSupportedException("unsupported_target_output: the named output is absent or is not a persistent RenderTexture. Retain the pass output until readback.");
                }
                var producer = executed.LastOrDefault(item => item.outputInstanceId == readTarget.GetInstanceID() && item.writes.Length > 0);
                var renderPlan = result["renderPlan"] as JObject;
                if (outputKind == "global-texture" && (producer == null || !(renderPlan?["passes"] is JArray planned) || !planned.Any(pass => (string)pass["id"] == producer.passId)))
                    throw new InvalidOperationException("target_output_not_produced_this_render: the selected texture must be the exact output recorded by a planned pass in this camera render.");
                if (readTarget.width != width || readTarget.height != height)
                    throw new ArgumentException("The selected output dimensions differ from the requested reference; readback never resizes it.");
                var format = readTarget.graphicsFormat;
                if (readTarget.dimension != TextureDimension.Tex2D || readTarget.volumeDepth != 1 || readTarget.antiAliasing != 1 ||
                    UnityEngine.Experimental.Rendering.GraphicsFormatUtility.IsDepthFormat(format) ||
                    UnityEngine.Experimental.Rendering.GraphicsFormatUtility.IsIntegerFormat(format))
                    throw new NotSupportedException("unsupported_target_output: depth, integer, multisample and array outputs need an explicit matching readback adapter.");
                var srgb = UnityEngine.Experimental.Rendering.GraphicsFormatUtility.IsSRGBFormat(format);
                bool floatReadback = range == "explicit" || !srgb;
                RenderTexture.active = readTarget;
                pixels = new Texture2D(width, height, floatReadback ? TextureFormat.RGBAFloat : TextureFormat.RGBA32, false, floatReadback);
                pixels.ReadPixels(new Rect(0, 0, width, height), 0, 0); pixels.Apply(false, false);
                var sourcePixels = pixels.GetPixels();
                var encoded = new Color32[sourcePixels.Length];
                int nonFinite = 0;
                Func<float, byte> quantize = value => {
                    if (Single.IsNaN(value) || Single.IsInfinity(value)) { nonFinite++; return 0; }
                    return (byte)Mathf.RoundToInt((float)Math.Max(0, Math.Min(1, (value - black) / (white - black))) * 255);
                };
                for (int y = 0; y < height; y++) for (int x = 0; x < width; x++)
                {
                    var value = sourcePixels[y * width + x];
                    byte red, green, blue;
                    if (channel == "rgba" || channel == "rgb") { red = quantize(value.r); green = quantize(value.g); blue = quantize(value.b); }
                    else { var scalar = channel == "r" ? value.r : channel == "g" ? value.g : channel == "b" ? value.b : value.a; red = green = blue = quantize(scalar); }
                    encoded[(flip ? height - 1 - y : y) * width + x] = new Color32(red, green, blue, 255);
                }
                UnityEngine.Object.DestroyImmediate(pixels);
                pixels = new Texture2D(width, height, TextureFormat.RGBA32, false, true);
                pixels.SetPixels32(encoded); pixels.Apply(false, false);
                File.WriteAllBytes(output, pixels.EncodeToPNG());
                result["passExecution"] = JArray.FromObject(executed);
                if (renderPlan?["passes"] is JArray passes)
                    foreach (JObject pass in passes)
                        if (!executed.Any(item => item.passId == (string)pass["id"]))
                        { BreadCaptureConfiguration.Gap((JArray)result["gaps"], "render_plan_pass_not_observed", (string)pass["id"]); result["status"] = "needs-repair"; }
                result["image"] = new JObject { ["path"] = output, ["sha256"] = BreadCaptureConfiguration.Hash(output), ["width"] = width, ["height"] = height,
                    ["format"] = "RGBA8-PNG", ["route"] = route, ["sceneIsolated"] = true, ["uiOverlayIncluded"] = false,
                    ["displayTransform"] = "Fixed component window to opaque RGB8; no additional gamma, exposure or independent normalization",
                    ["sourceGraphicsFormat"] = format.ToString(), ["sourceIsSRGB"] = srgb, ["floatReadback"] = floatReadback,
                    ["nonFiniteComponents"] = nonFinite, ["alphaMode"] = "opaque-rgb", ["capturedAtUtc"] = DateTime.UtcNow.ToString("O") };
                result["displayMapping"] = mapping.DeepClone(); result["targetOutput"] = outputSelection.DeepClone();
                result["targetOutputEvidence"] = new JObject { ["currentRender"] = true, ["producerPassId"] = producer?.passId,
                    ["outputInstanceId"] = readTarget.GetInstanceID(), ["outputVersion"] = producer?.outputVersion,
                    ["renderId"] = result["renderExecutionId"], ["cameraName"] = camera.name,
                    ["graphicsFormat"] = format.ToString(), ["width"] = readTarget.width, ["height"] = readTarget.height };
                result["renderCamera"] = BreadCaptureConfiguration.CameraSnapshot(camera);
            }
            finally
            {
                camera.targetTexture = previousTarget; camera.scene = previousCameraScene; RenderTexture.active = previousActive;
                Bread.BreadCapturePassEvidence.End();
                if (pixels != null) UnityEngine.Object.DestroyImmediate(pixels);
                target.Release(); UnityEngine.Object.DestroyImmediate(target);
            }
        }
    }

    [McpForUnityTool("bread_inspect_capture", Description = "Read current imported assets, material/runtime values, camera, pipeline and compiler diagnostics for an immutable Bread request.")]
    public static class BreadInspectCapture
    {
        public class Parameters { [ToolParameter("request_path", Required = true)] public string request_path { get; set; } }
        public static object HandleCommand(JObject parameters) => BreadCaptureTools.Run(parameters, "inspect");
    }

    [McpForUnityTool("bread_apply_capture_configuration", Description = "Apply and persist a scoped Bread scene/material/runtime/renderer configuration, then return actual readback.")]
    public static class BreadApplyCaptureConfiguration
    {
        public class Parameters
        {
            [ToolParameter("request_path", Required = true)] public string request_path { get; set; }
            [ToolParameter("configuration")] public JObject configuration { get; set; }
        }
        public static object HandleCommand(JObject parameters) => BreadCaptureTools.Run(parameters, "configure");
    }

    [McpForUnityTool("bread_render_capture", Description = "Render the imported camera into a fresh project-local PNG and return its hash plus the current scene/asset evidence.")]
    public static class BreadRenderCapture
    {
        public class Parameters
        {
            [ToolParameter("request_path", Required = true)] public string request_path { get; set; }
            [ToolParameter("width")] public int width { get; set; } = 1920;
            [ToolParameter("height")] public int height { get; set; } = 1080;
            [ToolParameter("output_path")] public string output_path { get; set; }
            [ToolParameter("camera_name")] public string camera_name { get; set; }
            [ToolParameter("display_mapping")] public JObject display_mapping { get; set; }
            [ToolParameter("target_output")] public JObject target_output { get; set; }
        }
        public static object HandleCommand(JObject parameters) => BreadCaptureTools.Run(parameters, "render");
    }
}
