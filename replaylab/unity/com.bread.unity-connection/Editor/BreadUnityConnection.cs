using System;
using System.IO;
using System.Linq;
using MCPForUnity.Editor;
using MCPForUnity.Editor.Services.Transport.Transports;
using MCPForUnity.Editor.Tools;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEditor.PackageManager;
using UnityEditor.PackageManager.Requests;
using UnityEngine;
using UnityEngine.Rendering;

namespace Bread.UnityConnection
{
    // This package is inert until Bread writes the project-local opt-in file.
    // Session-scoped stdio does not rewrite the user's global transport preferences.
    [InitializeOnLoad]
    public static class BreadUnityConnection
    {
        private static double nextCheck;
        private static string activeNonce;
        private static string activeReconnectRequest;
        public static string ActiveNonce => activeNonce;
        public static string ActiveReconnectRequest => activeReconnectRequest;
        public static string ActivePackageRevision { get; private set; }
        public static string ProjectPath => Path.GetFullPath(Path.Combine(Application.dataPath, "..")).Replace('\\', '/');
        private static string ConfigPath => Path.Combine(ProjectPath, "Library/Bread/unity-connection.json");

        static BreadUnityConnection()
        {
            EditorApplication.update += Update;
        }

        public static JObject Configuration()
        {
            if (!File.Exists(ConfigPath)) return null;
            return JObject.Parse(File.ReadAllText(ConfigPath));
        }

        private static void Update()
        {
            if (EditorApplication.timeSinceStartup < nextCheck || EditorApplication.isCompiling || EditorApplication.isUpdating) return;
            nextCheck = EditorApplication.timeSinceStartup + 2;
            try
            {
                var config = Configuration();
                if (config == null || config.Value<bool?>("enabled") != true) return;
                var nonce = config.Value<string>("nonce");
                if (string.IsNullOrEmpty(nonce)) return;
                var reconnectRequest = config.Value<string>("reconnectRequest") ?? "";
                if (nonce == activeNonce && reconnectRequest == activeReconnectRequest && StdioBridgeHost.IsRunning) return;
                var revision = config.Value<string>("packageRevision") ?? "";
                if (SessionState.GetString("Bread.UnityConnection.PackageRevision", "") != revision)
                {
                    SessionState.SetString("Bread.UnityConnection.PackageRevision", revision);
                    activeNonce = null;
                    AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);
                    return;
                }
                McpCiBoot.StartStdioForCi();
                activeNonce = nonce;
                activeReconnectRequest = reconnectRequest;
                ActivePackageRevision = revision;
            }
            catch (Exception error)
            {
                Debug.LogWarning("Bread Unity connection: " + error.Message);
            }
        }
    }

    [McpForUnityTool("bread_connection_identity", Description = "Verify the exact project and Bread connection nonce.")]
    public static class BreadConnectionIdentity
    {
        private static string[] Capabilities()
        {
            var names = new System.Collections.Generic.List<string> {
                "bread_connection_identity", "bread_inspect_rendering", "bread_prepare_urp" };
            var optional = new[] {
                new[] { "BreadCaptureImporter", "bread_import_capture" },
                new[] { "BreadInspectCapture", "bread_inspect_capture" },
                new[] { "BreadApplyCaptureConfiguration", "bread_apply_capture_configuration" },
                new[] { "BreadRenderCapture", "bread_render_capture" },
                new[] { "BreadRefreshAssets", "bread_refresh_assets" },
                new[] { "BreadPrepareFbxExporter", "bread_prepare_fbx_exporter" },
                new[] { "BreadExportPortableAssets", "bread_export_portable_assets" },
                new[] { "BreadInspectEditableDelivery", "bread_inspect_editable_delivery" },
                new[] { "BreadFrameDebugger", "bread_frame_debugger" },
                new[] { "BreadCaptureTexture", "bread_capture_texture" },
                new[] { "BreadCapturePassOutputs", "bread_capture_pass_outputs" },
                new[] { "BreadShadowDiagnostics", "bread_shadow_diagnostics" },
                new[] { "BreadImportVolumeTexture", "bread_import_volume_texture" },
                new[] { "BreadApplyAssetLayout", "bread_apply_asset_layout" },
                new[] { "BreadInspectCameraRendering", "bread_inspect_camera_rendering" },
                new[] { "BreadCaptureViewOutputs", "bread_capture_view_outputs" },
                new[] { "BreadSceneViewResize", "bread_resize_scene_view" },
                new[] { "BreadPrepareUrpPresentation", "bread_prepare_urp_presentation" } };
            foreach (var item in optional)
                if (Type.GetType("Bread.Editor." + item[0] + ", Bread.Capture.Editor") != null) names.Add(item[1]);
            return names.ToArray();
        }

        public static object HandleCommand(JObject parameters)
        {
            var config = BreadUnityConnection.Configuration();
            return new
            {
                success = config != null && config.Value<bool?>("enabled") == true,
                data = new
                {
                    projectPath = BreadUnityConnection.ProjectPath,
                    unityVersion = Application.unityVersion,
                    isBatchMode = Application.isBatchMode,
                    editorMode = Application.isBatchMode ? "batch" : "interactive",
                    nonce = BreadUnityConnection.ActiveNonce,
                    packageRevision = BreadUnityConnection.ActivePackageRevision,
                    reconnectRequest = BreadUnityConnection.ActiveReconnectRequest,
                    isCompiling = EditorApplication.isCompiling,
                    isUpdating = EditorApplication.isUpdating,
                    processId = System.Diagnostics.Process.GetCurrentProcess().Id,
                    capabilities = Capabilities(),
                }
            };
        }
    }

    [McpForUnityTool("bread_inspect_rendering", Description = "Read the active rendering pipeline, graphics conventions and installed package versions without changing the project.")]
    public static class BreadInspectRendering
    {
        private static JObject BuildTargetSnapshot(BuildTarget target, bool includeGraphicsApis)
        {
            var result = new JObject { ["target"] = target.ToString() };
            var errors = new JArray();
            try
            {
                var group = BuildPipeline.GetBuildTargetGroup(target);
                result["group"] = group.ToString();
                result["supportedInEditor"] = BuildPipeline.IsBuildTargetSupported(group, target);
            }
            catch (Exception error)
            {
                result["supportedInEditor"] = null;
                errors.Add(new JObject { ["operation"] = "IsBuildTargetSupported", ["detail"] = error.GetBaseException().Message });
            }
            if (includeGraphicsApis)
            {
                try
                {
                    result["usesDefaultGraphicsAPIs"] = PlayerSettings.GetUseDefaultGraphicsAPIs(target);
                    result["configuredGraphicsAPIs"] = new JArray(PlayerSettings.GetGraphicsAPIs(target).Select(api => api.ToString()));
                    result["graphicsApiConfigurationStatus"] = "observed";
                }
                catch (Exception error)
                {
                    result["graphicsApiConfigurationStatus"] = "unavailable";
                    errors.Add(new JObject { ["operation"] = "PlayerSettings.GetGraphicsAPIs", ["detail"] = error.GetBaseException().Message });
                }
            }
            result["errors"] = errors;
            return result;
        }

        public static object HandleCommand(JObject parameters)
        {
            var active = GraphicsSettings.currentRenderPipeline;
            var type = active == null ? "" : active.GetType().FullName;
            var pipeline = active == null ? "built-in" :
                type.Contains("UniversalRenderPipelineAsset") ? "urp" :
                type.Contains("HDRenderPipelineAsset") ? "hdrp" : "custom";
            var packages = new JObject();
            foreach (var package in UnityEditor.PackageManager.PackageInfo.GetAllRegisteredPackages())
                if (package.name.StartsWith("com.unity.render-pipelines.", StringComparison.Ordinal))
                    packages[package.name] = package.version;
            var buildTarget = EditorUserBuildSettings.activeBuildTarget;
            var targetInspection = BuildTargetSnapshot(buildTarget, true);
            return new { success = true, data = new JObject {
                ["projectPath"] = BreadUnityConnection.ProjectPath,
                ["unityVersion"] = Application.unityVersion,
                ["isBatchMode"] = Application.isBatchMode,
                ["editorMode"] = Application.isBatchMode ? "batch" : "interactive",
                ["renderPipeline"] = pipeline, ["pipelineType"] = type,
                ["pipelineAsset"] = active == null ? "" : AssetDatabase.GetAssetPath(active),
                ["qualityLevel"] = QualitySettings.GetQualityLevel(),
                ["qualityPipelineOverride"] = QualitySettings.renderPipeline == null ? "" : AssetDatabase.GetAssetPath(QualitySettings.renderPipeline),
                ["packages"] = packages,
                ["graphicsDeviceType"] = SystemInfo.graphicsDeviceType.ToString(),
                ["activeColorSpace"] = QualitySettings.activeColorSpace.ToString(),
                ["graphicsUVStartsAtTop"] = SystemInfo.graphicsUVStartsAtTop,
                ["usesReversedZBuffer"] = SystemInfo.usesReversedZBuffer,
                ["supportsComputeShaders"] = SystemInfo.supportsComputeShaders,
                ["supportsGeometryShaders"] = SystemInfo.supportsGeometryShaders,
                ["supportsTessellationShaders"] = SystemInfo.supportsTessellationShaders,
                ["graphicsShaderLevel"] = SystemInfo.graphicsShaderLevel,
                ["supportedRenderTargetCount"] = SystemInfo.supportedRenderTargetCount,
                ["graphicsCapabilityScope"] = "active-editor-device; target-player-hardware-not-executed",
                ["activeBuildTarget"] = buildTarget.ToString(),
                ["activeBuildTargetInspection"] = targetInspection,
                ["buildTargetAvailability"] = new JArray(Enum.GetValues(typeof(BuildTarget)).Cast<BuildTarget>().Distinct()
                    .Where(target => target != BuildTarget.NoTarget)
                    .Select(target => target == buildTarget ? targetInspection.DeepClone() : BuildTargetSnapshot(target, false))),
                ["renderGraphApiAvailable"] = Type.GetType("UnityEngine.Rendering.RenderGraphModule.RenderGraph, Unity.RenderPipelines.Core.Runtime") != null,
                ["renderGraphMode"] = "inspect-active-pipeline-settings",
                ["cameras"] = new JArray(UnityEngine.Object.FindObjectsOfType<Camera>()
                    .Select(camera => new JObject { ["name"] = camera.name, ["instanceId"] = camera.GetInstanceID(),
                        ["enabled"] = camera.enabled, ["width"] = camera.pixelWidth, ["height"] = camera.pixelHeight,
                        ["allowHDR"] = camera.allowHDR, ["allowMSAA"] = camera.allowMSAA })),
                ["managedImportPipeline"] = "preserve-existing", ["managedImportScope"] = "manifested-draw-and-pass-implementation",
                ["customEffectRoute"] = "project-tools",
            }};
        }
    }

    [McpForUnityTool("bread_prepare_urp", Description = "Install the Unity-compatible URP package when no conflicting pipeline is declared.")]
    public static class BreadPrepareUrp
    {
        private static AddRequest pending;
        private static string error;

        public static object HandleCommand(JObject parameters)
        {
            var active = GraphicsSettings.currentRenderPipeline;
            if (active != null && !active.GetType().FullName.Contains("UniversalRenderPipelineAsset"))
                return new { success = false, data = new { status = "needs_project_decision",
                    code = "existing-non-urp-pipeline", pipeline = active.GetType().FullName,
                    alternative = "Keep the current pipeline and supply a Shader adapted to that pipeline." } };
            if (active == null && parameters.Value<bool?>("allow_pipeline_creation") != true)
                return new { success = false, data = new { status = "needs_project_decision",
                    code = "existing-built-in-pipeline", pipeline = "built-in",
                    alternative = "Preserve the Built-in pipeline, or explicitly request creation of a URP target with allow_pipeline_creation." } };
            var manifest = JObject.Parse(File.ReadAllText(Path.Combine(BreadUnityConnection.ProjectPath, "Packages/manifest.json")));
            var dependencies = manifest["dependencies"] as JObject;
            if (dependencies?["com.unity.render-pipelines.universal"] != null)
            {
                var runtime = Type.GetType("UnityEngine.Rendering.Universal.UniversalRenderPipelineAsset, Unity.RenderPipelines.Universal.Runtime");
                bool ready = runtime != null && !EditorApplication.isCompiling && !EditorApplication.isUpdating;
                return new { success = true, data = new { status = ready ? "ready" : "pending", version = dependencies["com.unity.render-pipelines.universal"].ToString() } };
            }
            if (dependencies?["com.unity.render-pipelines.high-definition"] != null)
                return new { success = false, data = new { status = "needs_project_decision", code = "existing-hdrp-package" } };
            if (pending != null && pending.IsCompleted)
            {
                if (pending.Status == StatusCode.Failure) error = pending.Error.message;
                pending = null;
            }
            // A repaired network/package condition must not remain blocked by a
            // static error until the user restarts the Editor. Retry is explicit
            // so an unchanged poll cannot repeatedly run the package installer.
            if (error != null && parameters.Value<bool?>("retry") == true) error = null;
            if (error != null) return new { success = false, data = new { status = "failed", detail = error,
                retryAvailable = true, retryTool = "bread_prepare_urp", retryArgument = "retry" } };
            if (pending == null)
            {
                // The Unity package resolver chooses the version compatible with the
                // installed Editor. Never rewrite versions in an existing URP project.
                string package = "com.unity.render-pipelines.universal";
                string builtIn = Path.Combine(EditorApplication.applicationContentsPath,
                    "Resources/PackageManager/BuiltInPackages/com.unity.render-pipelines.universal/package.json");
                if (File.Exists(builtIn))
                {
                    var version = JObject.Parse(File.ReadAllText(builtIn)).Value<string>("version");
                    if (!string.IsNullOrEmpty(version)) package += "@" + version;
                }
                pending = Client.Add(package);
            }
            return new { success = true, data = new { status = "pending" } };
        }
    }
}
