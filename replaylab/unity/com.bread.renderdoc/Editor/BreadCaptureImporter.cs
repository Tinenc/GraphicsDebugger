using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
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
    // Fixed, project-scoped operations: no generated Editor script is executed.
    [McpForUnityTool("bread_import_capture", Description = "Import a verified Bread draw request or read its receipt.")]
    public static class BreadCaptureImporter
    {
        public class Parameters
        {
            [ToolParameter("request_path", Description = "Absolute request JSON in this project's Library/Bread/Requests", Required = true)]
            public string request_path { get; set; }
            [ToolParameter("action", Description = "import or status", Required = false)]
            public string action { get; set; }
            [ToolParameter("SHA-256 of the exact request file supplied by the Host", Required = false)]
            public string request_file_sha256 { get; set; }
        }

        private static readonly HashSet<string> Pending = new HashSet<string>();
        private static string Project => Path.GetFullPath(Path.Combine(Application.dataPath, ".."));

        public static object HandleCommand(JObject args)
        {
            try
            {
                var path = Path.GetFullPath((string)args["request_path"] ?? "");
                var allowed = Path.Combine(Project, "Library", "Bread", "Requests") + Path.DirectorySeparatorChar;
                if (!path.StartsWith(allowed, StringComparison.OrdinalIgnoreCase) || !File.Exists(path))
                    throw new InvalidOperationException("Request must belong to this Unity project.");
                RejectRedirected(path);
                if (new FileInfo(path).Length > 512L * 1024 * 1024) throw new InvalidOperationException("Request too large.");
                var requestBytes = File.ReadAllBytes(path);
                string requestFileHash;
                using (var hash = SHA256.Create()) requestFileHash = BitConverter.ToString(hash.ComputeHash(requestBytes)).Replace("-", "").ToLowerInvariant();
                var action = (string)args["action"] ?? "import";
                if ((action == "import" || args["request_file_sha256"] != null) &&
                    !String.Equals((string)args["request_file_sha256"], requestFileHash, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("Request file does not match the Host SHA-256.");
                var request = JObject.Parse(Encoding.UTF8.GetString(requestBytes));
                var id = (string)request["requestSha256"];
                if (id == null || id.Length != 64 || id.Any(c => !Uri.IsHexDigit(c)) ||
                    Path.GetFileName(path) != id + ".json" || (string)request["schema"] != "bread-unity-import/v1" ||
                    !String.Equals(Path.GetFullPath((string)request["projectPath"]), Project, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("Request identity does not match this project.");
                var receiptPath = Path.Combine(Path.GetDirectoryName(path), id + ".receipt.json");
                var receipt = File.Exists(receiptPath) ? JObject.Parse(File.ReadAllText(receiptPath)) : null;
                if (action == "status")
                {
                    if (receipt != null && ((string)receipt["status"] == "imported" || (string)receipt["status"] == "partial") &&
                        !SamePlatform(receipt["unityPlatform"] as JObject, UnityPlatform()))
                        return new JObject { ["status"] = "stale_environment", ["requestSha256"] = id,
                            ["reason"] = "Unity's graphics environment changed; repeat import to refresh verified evidence while preserving modified outputs.",
                            ["nextAction"] = "repeat-import", ["receiptPath"] = receiptPath,
                            ["previousUnityPlatform"] = receipt["unityPlatform"]?.DeepClone(), ["unityPlatform"] = UnityPlatform(),
                            ["visualEquivalenceVerified"] = false };
                    if (receipt != null && (string)receipt["status"] == "pending" && !Pending.Contains(id))
                    {
                        receipt["status"] = "interrupted";
                        receipt["reason"] = "Editor reloaded during import; repeat the import request to resume.";
                    }
                    return receipt ?? new JObject { ["status"] = "not_started", ["requestSha256"] = id };
                }
                if (action != "import") throw new InvalidOperationException("Unsupported action.");
                if (receipt != null && ((string)receipt["status"] == "imported" || (string)receipt["status"] == "partial"))
                {
                    if ((string)receipt["requestFileSha256"] != requestFileHash || !OutputsPresent(receipt))
                        return new JObject { ["status"] = "needs_project_decision", ["requestSha256"] = id,
                            ["reason"] = "An existing imported output or request changed. Preserve it; prepare a revised export instead of overwriting user edits.",
                            ["receiptPath"] = receiptPath, ["visualEquivalenceVerified"] = false };
                    // Project color space/API changes can alter Library imports
                    // without changing any source asset hashes. Rebuild evidence
                    // only after the existing output protection check passes.
                    if (SamePlatform(receipt["unityPlatform"] as JObject, UnityPlatform()))
                    {
                        receipt["reused"] = true;
                        return receipt;
                    }
                }
                if (Pending.Contains(id)) return receipt;
                Pending.Add(id);
                var queued = new JObject { ["status"] = "pending", ["requestSha256"] = id, ["receiptPath"] = receiptPath };
                WriteReceipt(receiptPath, queued);
                EditorApplication.delayCall += () =>
                {
                    try
                    {
                        var imported = Import(request, receiptPath);
                        imported["requestFileSha256"] = requestFileHash;
                        WriteReceipt(receiptPath, imported);
                    }
                    catch (Exception error)
                    {
                        WriteReceipt(receiptPath, new JObject { ["status"] = "failed", ["requestSha256"] = id,
                            ["reason"] = error.ToString(), ["visualEquivalenceVerified"] = false });
                    }
                    finally { Pending.Remove(id); }
                };
                return queued;
            }
            catch (Exception error) { return new JObject { ["status"] = "failed", ["reason"] = error.Message }; }
        }

        private static void RejectRedirected(string path)
        {
            for (var item = path; !String.IsNullOrEmpty(item); item = Path.GetDirectoryName(item))
                if ((File.Exists(item) || Directory.Exists(item)) && (File.GetAttributes(item) & FileAttributes.ReparsePoint) != 0)
                    throw new InvalidOperationException("Redirected import paths are unsupported.");
        }

        private static string Hash(string path)
        {
            using (var stream = File.OpenRead(path)) using (var hash = SHA256.Create())
                return BitConverter.ToString(hash.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
        }

        private static string CopyEvidence(JObject evidence, string destination)
        {
            var source = (string)evidence["sourcePath"];
            RejectRedirected(source);
            if (!String.Equals(Hash(source), (string)evidence["sha256"], StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("Source artifact changed after preparation.");
            File.Copy(source, destination, true);
            AssetDatabase.ImportAsset(destination, ImportAssetOptions.ForceSynchronousImport);
            return destination;
        }

        private static void WriteReceipt(string path, JObject receipt)
        {
            var temporary = path + ".tmp";
            File.WriteAllText(temporary, receipt.ToString());
            if (File.Exists(path)) File.Delete(path);
            File.Move(temporary, path);
        }

        private static bool OutputsPresent(JObject receipt)
        {
            var outputs = receipt["outputs"] as JArray;
            return outputs != null && outputs.Count > 0 && outputs.All(x => File.Exists((string)x["path"]) && Hash((string)x["path"]) == (string)x["sha256"]);
        }

        private static Vector4 Vector(JToken row, float w = 0)
        {
            var values = (JArray)row;
            return new Vector4((float)values[0], values.Count > 1 ? (float)values[1] : 0,
                values.Count > 2 ? (float)values[2] : 0, values.Count > 3 ? (float)values[3] : w);
        }

        private static Mesh CreateMesh(JObject source)
        {
            var attributes = (JObject)source["attributes"];
            var positions = ((JArray)attributes["POSITION"]["data"]).Select(x => (Vector3)Vector(x)).ToArray();
            var mesh = new Mesh { name = "Bread captured draw", indexFormat = positions.Length > 65535 ? IndexFormat.UInt32 : IndexFormat.UInt16 };
            var names = new List<string> { "POSITION", "NORMAL", "TANGENT", "COLOR_0" };
            names.AddRange(Enumerable.Range(0, 8).Select(x => "TEXCOORD_" + x));
            var kinds = new List<VertexAttribute> { VertexAttribute.Position, VertexAttribute.Normal,
                VertexAttribute.Tangent, VertexAttribute.Color };
            kinds.AddRange(Enumerable.Range(0, 8).Select(x => (VertexAttribute)((int)VertexAttribute.TexCoord0 + x)));
            var descriptors = new List<VertexAttributeDescriptor>();
            var channels = new List<JObject>();
            for (var index = 0; index < names.Count; index++)
            {
                var attribute = attributes[names[index]] as JObject;
                if (attribute == null) continue;
                var components = (int)attribute["components"];
                if (components < 1 || components > 4 || ((JArray)attribute["data"]).Count != positions.Length)
                    throw new InvalidOperationException("Invalid captured vertex channel dimensions.");
                if (!SystemInfo.SupportsVertexAttributeFormat(VertexAttributeFormat.Float32, components))
                    throw new InvalidOperationException("This graphics API does not support the captured vertex channel dimensions.");
                descriptors.Add(new VertexAttributeDescriptor(kinds[index], VertexAttributeFormat.Float32, components, 0));
                channels.Add(attribute);
            }
            // Low-level layout preserves scalar TEXCOORD and RGB dimensions;
            // SetUVs(Vector2) and Color would silently expand those channels.
            mesh.SetVertexBufferParams(positions.Length, descriptors.ToArray());
            var expectedStride = 0;
            foreach (var descriptor in descriptors)
            {
                if (mesh.GetVertexAttributeOffset(descriptor.attribute) != expectedStride)
                    throw new InvalidOperationException("Unity reordered or padded the captured vertex layout.");
                expectedStride += descriptor.dimension * sizeof(float);
            }
            if (mesh.GetVertexBufferStride(0) != expectedStride)
                throw new InvalidOperationException("Unity vertex stride differs from the captured target layout.");
            using (var bytes = new MemoryStream())
            using (var writer = new BinaryWriter(bytes))
            {
                for (var vertex = 0; vertex < positions.Length; vertex++)
                    foreach (var channel in channels)
                    {
                        var row = (JArray)channel["data"][vertex];
                        if (row.Count != (int)channel["components"])
                            throw new InvalidOperationException("Captured vertex row dimensions disagree.");
                        foreach (var item in row)
                        {
                            var value = (double)item;
                            if (Double.IsNaN(value) || Double.IsInfinity(value) || Math.Abs(value) > Single.MaxValue ||
                                item.Type == JTokenType.Integer && (double)(float)value != value)
                                throw new InvalidOperationException("Captured vertex value cannot be represented as Float32.");
                            writer.Write((float)value);
                        }
                    }
                writer.Flush();
                var data = bytes.ToArray();
                if (data.LongLength != (long)positions.Length * expectedStride)
                    throw new InvalidOperationException("Captured vertex buffer length disagrees with its layout.");
                mesh.SetVertexBufferData(data, 0, 0, data.Length);
            }
            foreach (var descriptor in descriptors)
                if (mesh.GetVertexAttributeDimension(descriptor.attribute) != descriptor.dimension ||
                    mesh.GetVertexAttributeFormat(descriptor.attribute) != descriptor.format)
                    throw new InvalidOperationException("Unity vertex layout differs from the requested captured layout.");
            var mode = (int)source["mode"];
            var topology = mode == 0 ? MeshTopology.Points : mode == 1 ? MeshTopology.Lines : mode == 3 ? MeshTopology.LineStrip : MeshTopology.Triangles;
            mesh.SetIndices(((JArray)source["indices"]).Select(x => (int)x).ToArray(), topology, 0);
            mesh.RecalculateBounds(); // Captured normals and tangents are never regenerated.
            return mesh;
        }

        private static void Gap(JArray gaps, string code, string detail)
        { gaps.Add(new JObject { ["code"] = code, ["detail"] = detail }); }

        private static JObject UnityPlatform()
        {
            // These are diagnostics, not instructions to flip image pixels, mesh
            // UVs or screen/depth coordinates. URP handles the latter in Shader.
            var pipeline = GraphicsSettings.currentRenderPipeline;
            var pipelinePath = pipeline == null ? "" : AssetDatabase.GetAssetPath(pipeline);
            return new JObject {
                ["graphicsDeviceType"] = SystemInfo.graphicsDeviceType.ToString(),
                ["activeColorSpace"] = QualitySettings.activeColorSpace.ToString(),
                ["graphicsUVStartsAtTop"] = SystemInfo.graphicsUVStartsAtTop,
                ["usesReversedZBuffer"] = SystemInfo.usesReversedZBuffer,
                ["unityVersion"] = Application.unityVersion,
                ["renderPipeline"] = BreadCaptureConfiguration.Pipeline(), ["pipelineAssetPath"] = pipelinePath,
                ["pipelineAssetSha256"] = File.Exists(pipelinePath) ? Hash(pipelinePath) : "",
                ["automaticApiFlip"] = false, ["imagePixelsRewritten"] = false,
                ["visualEquivalenceVerified"] = false };
        }

        private static bool SamePlatform(JObject recorded, JObject current)
        {
            return recorded != null && new[] { "graphicsDeviceType", "activeColorSpace", "graphicsUVStartsAtTop",
                "usesReversedZBuffer", "unityVersion", "renderPipeline", "pipelineAssetPath", "pipelineAssetSha256" }.All(key => recorded[key] != null && JToken.DeepEquals(recorded[key], current[key]));
        }

        private static void ApplySampler(TextureImporter importer, JObject sampler, string property, JArray gaps)
        {
            if (sampler == null) return;
            foreach (var setting in sampler.Properties())
            {
                var value = (string)setting.Value;
                if (setting.Name == "filterMode" && Enum.TryParse(value, true, out FilterMode filter) && Enum.IsDefined(typeof(FilterMode), filter))
                    importer.filterMode = filter;
                else if ((setting.Name == "wrapU" || setting.Name == "wrapV") &&
                         Enum.TryParse(value, true, out TextureWrapMode wrap) && Enum.IsDefined(typeof(TextureWrapMode), wrap))
                {
                    if (setting.Name == "wrapU") importer.wrapModeU = wrap;
                    else importer.wrapModeV = wrap;
                }
                else if (setting.Name == "anisoLevel" && setting.Value.Type == JTokenType.Integer &&
                         (int)setting.Value >= 0 && (int)setting.Value <= 16)
                    importer.anisoLevel = (int)setting.Value;
                else Gap(gaps, "sampler_setting_not_applied", property + ": " + setting.Name + "=" + value);
            }
        }

        private static JObject ApplyTextureTransform(Material material, string property, JObject transform,
                                                    string shaderCode, bool native, JArray gaps)
        {
            if (transform == null) return new JObject { ["applied"] = false, ["policy"] = "preserve-exported-uv" };
            var scale = transform["scale"] as JArray;
            var offset = transform["offset"] as JArray;
            if (scale == null || offset == null || scale.Count != 2 || offset.Count != 2 ||
                scale.Concat(offset).Any(x => (x.Type != JTokenType.Float && x.Type != JTokenType.Integer) ||
                                             Double.IsNaN((double)x) || Double.IsInfinity((double)x) || Math.Abs((double)x) > Single.MaxValue))
                throw new InvalidOperationException("Invalid explicit Unity texture transform.");
            var result = new JObject { ["requested"] = transform.DeepClone(), ["applied"] = false };
            if (material == null || !material.HasProperty(property)) return result;
            var targetScale = new Vector2((float)scale[0], (float)scale[1]);
            var targetOffset = new Vector2((float)offset[0], (float)offset[1]);
            material.SetTextureScale(property, targetScale);
            material.SetTextureOffset(property, targetOffset);
            var actualScale = material.GetTextureScale(property);
            var actualOffset = material.GetTextureOffset(property);
            result["applied"] = actualScale == targetScale && actualOffset == targetOffset;
            result["scale"] = new JArray(actualScale.x, actualScale.y);
            result["offset"] = new JArray(actualOffset.x, actualOffset.y);
            // _ST may be a CBUFFER member, so Shader.HasProperty alone cannot
            // establish whether the sampling calculation consumes the transform.
            var token = Regex.Escape(property);
            var consumesTransform = native && property == "_BaseMap" ||
                Regex.IsMatch(shaderCode, @"\bTRANSFORM_TEX\s*\([^,]+,\s*" + token + @"\s*\)") ||
                Regex.IsMatch(shaderCode, token + @"_ST\s*\.\s*xy\s*\+\s*" + token + @"_ST\s*\.\s*zw");
            result["shaderUseEvidence"] = consumesTransform ? "known-transform-expression" : "unverified";
            result["visualUseVerified"] = false;
            if (!consumesTransform && (targetScale != Vector2.one || targetOffset != Vector2.zero))
                Gap(gaps, "shader_uv_transform_unverified", property + ": material scale/offset was stored, but Shader consumption needs validation.");
            return result;
        }

        private static void ApplyProperty(Material material, JObject property, JArray gaps)
        {
            var name = (string)property["name"];
            var value = property["value"];
            var type = (string)property["type"] ?? "auto";
            if (value is JArray matrix && matrix.Count == 16 && type == "matrix")
            {
                // The request uses explicit row-major element order, independent of HLSL storage.
                var result = new Matrix4x4();
                for (var row = 0; row < 4; row++) for (var col = 0; col < 4; col++) result[row, col] = (float)matrix[row * 4 + col];
                material.SetMatrix(name, result);
                Gap(gaps, "matrix_requires_runtime_binding", name + ": Unity Material assets do not serialize matrix properties.");
            }
            else if (!material.HasProperty(name)) Gap(gaps, "shader_property_absent", name);
            else if (value is JArray list)
            {
                if (list.Count > 4) { Gap(gaps, "material_property_shape_unsupported", name); return; }
                if (type == "color") material.SetColor(name, (Color)Vector(list, 1));
                else material.SetVector(name, Vector(list));
            }
            else if (type == "int") material.SetInt(name, (int)value);
            else material.SetFloat(name, (float)value);
        }

        private static void NativeMaterial(Material material, JObject settings)
        {
            if (settings["baseColorFactor"] is JArray color) material.SetColor("_BaseColor", (Color)Vector(color, 1));
            material.SetFloat("_Metallic", (float?)settings["metallicFactor"] ?? 0);
            material.SetFloat("_Smoothness", 1 - ((float?)settings["roughnessFactor"] ?? 1));
            material.SetFloat("_Cull", (bool?)settings["doubleSided"] == true ? 0 : 2);
            var mode = (string)settings["alphaMode"] ?? "OPAQUE";
            if (mode == "MASK")
            {
                material.SetFloat("_AlphaClip", 1); material.SetFloat("_Cutoff", (float?)settings["alphaCutoff"] ?? 0.5f);
                material.EnableKeyword("_ALPHATEST_ON"); material.renderQueue = (int)RenderQueue.AlphaTest;
                material.SetOverrideTag("RenderType", "TransparentCutout");
            }
            else if (mode == "BLEND")
            {
                material.SetFloat("_Surface", 1); material.SetFloat("_SrcBlend", (float)BlendMode.SrcAlpha);
                material.SetFloat("_DstBlend", (float)BlendMode.OneMinusSrcAlpha); material.SetFloat("_ZWrite", 0);
                material.EnableKeyword("_SURFACE_TYPE_TRANSPARENT"); material.renderQueue = (int)RenderQueue.Transparent;
                material.SetOverrideTag("RenderType", "Transparent");
            }
        }

        private static void EnsureUrp(string folder, JArray gaps)
        {
            var current = GraphicsSettings.currentRenderPipeline;
            if (current != null)
            {
                if (!current.GetType().FullName.Contains("UniversalRenderPipelineAsset"))
                    Gap(gaps, "existing_pipeline_requires_decision", current.GetType().FullName);
                return;
            }
            // Reflection keeps this installer package compilable before UPM has
            // resolved URP. An existing non-URP pipeline is never replaced.
            var pipelineType = Type.GetType("UnityEngine.Rendering.Universal.UniversalRenderPipelineAsset, Unity.RenderPipelines.Universal.Runtime");
            var rendererType = Type.GetType("UnityEngine.Rendering.Universal.UniversalRendererData, Unity.RenderPipelines.Universal.Runtime");
            var create = pipelineType?.GetMethods(BindingFlags.Public | BindingFlags.Static)
                .FirstOrDefault(method => method.Name == "Create" && method.GetParameters().Length == 1);
            if (pipelineType == null || rendererType == null || create == null)
            { Gap(gaps, "urp_factory_unavailable", "URP is not loaded or this version lacks the expected asset factory."); return; }
            var pipelinePath = folder + "/BreadPipeline.asset";
            var pipeline = AssetDatabase.LoadAssetAtPath<RenderPipelineAsset>(pipelinePath);
            if (pipeline == null)
            {
                var rendererPath = folder + "/BreadRenderer.asset";
                var renderer = AssetDatabase.LoadAssetAtPath<ScriptableObject>(rendererPath);
                if (renderer == null)
                {
                    renderer = ScriptableObject.CreateInstance(rendererType);
                    AssetDatabase.CreateAsset(renderer, rendererPath);
                }
                pipeline = (RenderPipelineAsset)create.Invoke(null, new object[] { renderer });
                AssetDatabase.CreateAsset(pipeline, pipelinePath);
                EditorUtility.SetDirty(renderer);
            }
            GraphicsSettings.renderPipelineAsset = pipeline;
            AssetDatabase.SaveAssets();
        }

        private static JObject Import(JObject request, string receiptPath)
        {
            var id = (string)request["requestSha256"];
            var folder = "Assets/BreadImports/" + id.Substring(0, 24);
            if ((string)request["destination"] != folder) throw new InvalidOperationException("Invalid destination.");
            var policy = request["pipelinePolicy"] as JObject ?? new JObject();
            var expectedPipeline = (string)policy["targetPipeline"];
            var allowCreateUrp = (bool?)policy["allowCreateUrp"] == true && expectedPipeline == "urp";
            var actualPipeline = BreadCaptureConfiguration.Pipeline();
            if (!String.IsNullOrEmpty(expectedPipeline) && expectedPipeline != actualPipeline &&
                !(allowCreateUrp && actualPipeline == "built-in"))
                throw new InvalidOperationException("The active pipeline differs from the requested target. Adapt the implementation to the existing pipeline; it was preserved.");
            RejectRedirected(Path.Combine(Project, folder));
            Directory.CreateDirectory(folder);
            bool scriptsChanged = false;
            foreach (JObject asset in request["assets"] as JArray ?? new JArray())
            {
                var relative = (string)asset["path"];
                if (String.IsNullOrEmpty(relative) || relative.Contains("\\") || relative.Split('/').Any(part => part == "" || part == "." || part == "..") || Path.IsPathRooted(relative))
                    throw new InvalidOperationException("Implementation asset must use a relative path.");
                var destination = folder + "/" + relative;
                RejectRedirected(Path.GetFullPath(destination));
                if (File.Exists(destination) && Hash(destination) == (string)asset["sha256"]) continue;
                Directory.CreateDirectory(Path.GetDirectoryName(destination));
                var sourcePath = (string)asset["sourcePath"];
                RejectRedirected(sourcePath);
                if (Hash(sourcePath) != (string)asset["sha256"]) throw new InvalidOperationException("Implementation asset changed after preparation.");
                File.Copy(sourcePath, destination, true);
                scriptsChanged |= new[] { ".cs", ".asmdef", ".asmref" }.Contains(Path.GetExtension(destination).ToLowerInvariant());
            }
            AssetDatabase.Refresh(ImportAssetOptions.ForceSynchronousImport);
            if (scriptsChanged || EditorApplication.isCompiling)
                return new JObject { ["status"] = "interrupted", ["code"] = "implementation_compile_pending", ["requestSha256"] = id,
                    ["retrySameRequest"] = true, ["reason"] = "Implementation assets were staged. Resume the same import after Unity compilation/domain reload." };
            if (EditorUtility.scriptCompilationFailed)
                throw new InvalidOperationException("Unity script compilation failed. Read compiler diagnostics, repair the implementation and retry this request.");
            var gaps = (JArray)request["gaps"].DeepClone();
            var effectRoute = (string)request["executionKind"] == "effect";
            var mesh = effectRoute ? null : CreateMesh((JObject)request["mesh"]);
            if (mesh != null && (string)request["shaderStrategy"] == "target-native-pbr" && mesh.normals.Length != mesh.vertexCount)
                Gap(gaps, "native_material_normals_missing", "The captured mesh has no complete normals for URP Lit; vertex data was preserved without recalculation.");
            var meshPath = folder + "/DrawMesh.asset";
            var priorMesh = AssetDatabase.LoadAssetAtPath<Mesh>(meshPath);
            if (mesh != null && priorMesh != null) { EditorUtility.CopySerialized(mesh, priorMesh); UnityEngine.Object.DestroyImmediate(mesh); mesh = priorMesh; }
            else if (mesh != null) AssetDatabase.CreateAsset(mesh, meshPath);

            if (allowCreateUrp) EnsureUrp(folder, gaps);

            Shader shader = null;
            var shaderCode = "";
            var source = request["shader"] as JObject;
            var shaderAsset = (string)request["options"]?["shaderAssetPath"];
            if (!String.IsNullOrEmpty(shaderAsset))
            {
                if (!((JArray)request["assets"]).Any(asset => (string)asset["path"] == shaderAsset && (string)asset["kind"] == "shader"))
                    throw new InvalidOperationException("The primary Shader must be a manifested implementation asset.");
                shader = AssetDatabase.LoadAssetAtPath<Shader>(folder + "/" + shaderAsset);
                shaderCode = Regex.Replace(File.ReadAllText(folder + "/" + shaderAsset), @"/\*[\s\S]*?\*/|//[^\r\n]*", "");
            }
            else if (source != null)
            {
                shader = AssetDatabase.LoadAssetAtPath<Shader>(CopyEvidence(source, folder + "/Draw.shader"));
                shaderCode = Regex.Replace(File.ReadAllText(folder + "/Draw.shader"), @"/\*[\s\S]*?\*/|//[^\r\n]*", "");
            }
            else if ((string)request["shaderStrategy"] == "target-native-pbr" && BreadCaptureConfiguration.Pipeline() == "urp") shader = Shader.Find("Universal Render Pipeline/Lit");
            var knownNativeShader = source == null && shader != null && shader.name == "Universal Render Pipeline/Lit";
            var shaderOkay = effectRoute || shader != null && !ShaderUtil.ShaderHasError(shader);
            var diagnostics = new JArray();
            if (shader != null) foreach (var message in ShaderUtil.GetShaderMessages(shader))
                diagnostics.Add(new JObject { ["message"] = message.message, ["severity"] = message.severity.ToString(), ["line"] = message.line });
            if (!shaderOkay) Gap(gaps, "shader_not_compiled", shader == null ? "No mapped Unity Shader is available." : "Shader has compiler errors.");
            Material material = null;
            var materialPath = folder + "/DrawMaterial.mat";
            if (shader != null)
            {
                material = AssetDatabase.LoadAssetAtPath<Material>(materialPath);
                if (material == null) { material = new Material(shader); AssetDatabase.CreateAsset(material, materialPath); }
                else material.shader = shader;
                if ((string)request["shaderStrategy"] == "target-native-pbr") NativeMaterial(material, (JObject)request["material"]);
                foreach (var property in (JArray)request["properties"]) BreadCaptureConfiguration.Property(material, (JObject)property, gaps);
                BreadCaptureConfiguration.RenderState(material, request["options"]?["renderState"] as JObject, gaps);
                if (!String.IsNullOrEmpty(shaderCode) && !shaderCode.Contains("Bread.Editor.BreadRestoredShaderGUI"))
                    Gap(gaps, "material_inspector_not_connected", "The custom Shader retains its declared inspector. Add Bread.Editor.BreadRestoredShaderGUI to the generated Shader, or integrate the texture-first typed controls with its existing ShaderGUI.");
            }
            var textureCount = 0;
            var textureEvidence = new JArray();
            foreach (JObject texture in (JArray)request["textures"])
            {
                var extension = ((string)texture["extension"]).ToLowerInvariant();
                if (!new[] { ".png", ".tga", ".exr", ".hdr" }.Contains(extension)) { Gap(gaps, "unsupported_image", extension); continue; }
                var texturePath = folder + "/Texture_" + textureCount + extension;
                CopyEvidence(texture, texturePath);
                var importer = (TextureImporter)AssetImporter.GetAtPath(texturePath);
                importer.textureType = (bool?)texture["normalMap"] == true ? TextureImporterType.NormalMap : TextureImporterType.Default;
                importer.sRGBTexture = (string)texture["colorSpace"] == "sRGB";
                var prop = (string)texture["property"];
                if (importer.textureType == TextureImporterType.NormalMap && importer.sRGBTexture)
                {
                    importer.sRGBTexture = false;
                    Gap(gaps, "normal_map_srgb_conflict", prop + ": Unity normal-map decoding requires linear sampling; the conflicting sRGB request was not applied.");
                }
                importer.textureCompression = TextureImporterCompression.Uncompressed;
                importer.mipmapEnabled = false; // A base image is not the captured mip chain.
                importer.npotScale = TextureImporterNPOTScale.None;
                importer.maxTextureSize = 16384;
                var sampler = texture["sampler"] as JObject;
                ApplySampler(importer, sampler, prop, gaps);
                importer.SaveAndReimport();
                var imported = AssetDatabase.LoadAssetAtPath<Texture2D>(texturePath);
                if (imported == null) { Gap(gaps, "texture_import_failed", texturePath); continue; }
                if (material != null && material.HasProperty(prop))
                {
                    material.SetTexture(prop, imported);
                    if ((string)request["shaderStrategy"] == "target-native-pbr" && prop == "_BumpMap") material.EnableKeyword("_NORMALMAP");
                    if ((string)request["shaderStrategy"] == "target-native-pbr" && prop == "_EmissionMap") material.EnableKeyword("_EMISSION");
                }
                else if (!effectRoute) Gap(gaps, "texture_property_not_assigned", prop);
                var transform = ApplyTextureTransform(material, prop, texture["unityUvTransform"] as JObject,
                    shaderCode, knownNativeShader, gaps);
                var srgbFormat = UnityEngine.Experimental.Rendering.GraphicsFormatUtility.IsSRGBFormat(imported.graphicsFormat);
                var sourceSampling = (string)texture["colorSpaceContract"]?["sourceSamplingSpace"] ?? (string)texture["colorSpace"];
                if (sourceSampling == "sRGB" && QualitySettings.activeColorSpace != ColorSpace.Linear)
                    Gap(gaps, "target_gamma_srgb_decode_unverified", prop + ": source sampling expects sRGB decode but the Unity project is Gamma. The project color space was preserved.");
                else if (sourceSampling == "sRGB" && !srgbFormat)
                    Gap(gaps, "texture_srgb_decode_not_applied", prop + ": imported graphics format does not supply the requested sRGB decode.");
                else if (sourceSampling == "linear" && srgbFormat && QualitySettings.activeColorSpace == ColorSpace.Linear)
                    Gap(gaps, "texture_unexpected_srgb_decode", prop + ": the imported format decodes sRGB although the captured view sampled linear data.");
                var samplerEvidence = knownNativeShader ||
                    Regex.IsMatch(shaderCode, @"\bSAMPLE_TEXTURE2D(?:_LOD|_BIAS|_GRAD)?\s*\(\s*" + Regex.Escape(prop) +
                                              @"\s*,\s*sampler" + Regex.Escape(prop) + @"\s*,");
                if (!effectRoute && !samplerEvidence && sampler != null && sampler.HasValues)
                    Gap(gaps, "shader_sampler_binding_unverified", prop + ": importer sampling settings were stored; an inline or shared Shader sampler may override them.");
                textureEvidence.Add(new JObject {
                    ["sourceResourceId"] = texture["sourceResourceId"], ["property"] = prop, ["path"] = texturePath,
                    ["sourceSha256"] = texture["sha256"], ["colorSpaceContract"] = texture["colorSpaceContract"]?.DeepClone(),
                    ["sourceSamplingSpace"] = sourceSampling, ["graphicsFormat"] = imported.graphicsFormat.ToString(),
                    ["graphicsFormatIsSRGB"] = srgbFormat, ["sRGBTexture"] = importer.sRGBTexture,
                    ["textureType"] = importer.textureType.ToString(), ["activeColorSpace"] = QualitySettings.activeColorSpace.ToString(),
                    ["sourceImageFilePreserved"] = Hash(texturePath) == (string)texture["sha256"],
                    ["normalMapRepacking"] = importer.textureType == TextureImporterType.NormalMap,
                    ["width"] = imported.width, ["height"] = imported.height, ["mipmapCount"] = imported.mipmapCount,
                    ["mipmapEnabled"] = importer.mipmapEnabled, ["mipPolicy"] = "base-level-image",
                    ["sampler"] = new JObject { ["filterMode"] = imported.filterMode.ToString(), ["wrapU"] = imported.wrapModeU.ToString(),
                        ["wrapV"] = imported.wrapModeV.ToString(), ["anisoLevel"] = imported.anisoLevel },
                    ["requestedSampler"] = sampler?.DeepClone(), ["capturedSamplerEvidence"] = texture["capturedSamplerEvidence"]?.DeepClone(),
                    ["shaderSamplerBindingEvidence"] = samplerEvidence ? "known-texture-sampler-expression" : "unverified",
                    ["unityUvTransform"] = transform, ["imagePixelsRewritten"] = false, ["orientationVerified"] = false });
                textureCount++;
            }
            if (material != null) EditorUtility.SetDirty(material);
            if (mesh != null) EditorUtility.SetDirty(mesh);
            AssetDatabase.SaveAssets();
            var inspector = request["materialInspector"] as JObject;
            if (material != null && inspector != null && !BreadRestoredShaderGUI.Store(material, inspector))
                Gap(gaps, "material_inspector_metadata_preserved", "Existing non-JSON importer data was preserved; native Shader property controls remain available.");
            foreach (var diagnostic in inspector?["diagnostics"] as JArray ?? new JArray())
                Gap(gaps, "material_inspector_metadata", (string)diagnostic);
            var previousActiveScene = SceneManager.GetActiveScene();
            var prefabPath = folder + "/CapturedDraw.prefab";
            var scenePath = folder + "/Preview.unity";
            var scene = BreadCaptureConfiguration.OpenOwnedScene(scenePath);
            try
            {
                foreach (var previousRoot in scene.GetRootGameObjects()) UnityEngine.Object.DestroyImmediate(previousRoot);
                var root = new GameObject((string)request["assetName"]);
                SceneManager.MoveGameObjectToScene(root, scene);
                if (mesh != null) { root.AddComponent<MeshFilter>().sharedMesh = mesh;
                    var renderer = root.AddComponent<MeshRenderer>(); renderer.sharedMaterial = material; }
                else root.AddComponent<Bread.BreadCaptureEffect>();
                var cameraObject = new GameObject("Bread Preview Camera"); SceneManager.MoveGameObjectToScene(cameraObject, scene);
                var camera = cameraObject.AddComponent<Camera>();
                var center = mesh == null ? Vector3.zero : mesh.bounds.center;
                var radius = mesh == null ? 1 : Mathf.Max(mesh.bounds.extents.magnitude, 0.01f);
                camera.transform.position = center + new Vector3(0, 0, -radius * 3);
                camera.transform.LookAt(center); camera.nearClipPlane = radius / 100; camera.farClipPlane = radius * 20;
                var lightObject = new GameObject("Bread Preview Light"); SceneManager.MoveGameObjectToScene(lightObject, scene);
                lightObject.transform.rotation = Quaternion.Euler(40, -30, 0);
                lightObject.AddComponent<Light>().type = LightType.Directional;
                if (effectRoute) BreadEffectConfiguration.Configure(root, folder, request["options"]?["effect"] as JObject,
                    (JArray)request["textures"], request["runtimeBindings"] as JArray, gaps);
                else BreadCaptureConfiguration.Bind(root, request["runtimeBindings"] as JArray, gaps);
                if (effectRoute) { root.GetComponent<Bread.BreadCaptureEffect>().targetCamera = camera; EditorUtility.SetDirty(root.GetComponent<Bread.BreadCaptureEffect>()); }
                BreadCaptureConfiguration.Scene(root, camera, scene, request["options"]?["scene"] as JObject, gaps);
                BreadCaptureConfiguration.Features(request["options"]?["rendererFeatures"] as JArray, true, gaps);
                PrefabUtility.SaveAsPrefabAsset(root, prefabPath);
                if (!EditorSceneManager.SaveScene(scene, scenePath))
                    Gap(gaps, "preview_scene_not_saved", "Prefab imported; Unity could not serialize the isolated preview scene.");
            }
            finally
            {
                EditorSceneManager.CloseScene(scene, true);
                if (previousActiveScene.IsValid() && previousActiveScene.isLoaded)
                    SceneManager.SetActiveScene(previousActiveScene);
            }
            var pipeline = GraphicsSettings.currentRenderPipeline;
            var urp = pipeline != null && pipeline.GetType().FullName.Contains("UniversalRenderPipelineAsset");
            actualPipeline = BreadCaptureConfiguration.Pipeline();
            var compatiblePipeline = String.IsNullOrEmpty(expectedPipeline) || expectedPipeline == actualPipeline;
            var outputs = new JArray(Directory.GetFiles(folder, "*", SearchOption.AllDirectories)
                .Select(path => new JObject { ["path"] = Path.GetFullPath(path), ["sha256"] = Hash(path) }));
            var checks = new JObject { ["meshImport"] = effectRoute || mesh != null, ["shaderCompile"] = shaderOkay,
                ["materialImport"] = effectRoute || material != null, ["textureImport"] = textureCount == ((JArray)request["textures"]).Count,
                ["sceneIntegrity"] = File.Exists(scenePath), ["pipelineCompatible"] = compatiblePipeline };
            return new JObject { ["status"] = gaps.Count == 0 && checks.Properties().All(x => (bool)x.Value) ? "imported" : "partial",
                ["requestSha256"] = id, ["packageManifestSha256"] = request["packageManifestSha256"],
                ["receiptPath"] = receiptPath, ["destination"] = folder, ["prefabPath"] = prefabPath, ["scenePath"] = scenePath,
                ["checks"] = checks, ["gaps"] = gaps, ["shaderDiagnostics"] = diagnostics, ["outputs"] = outputs,
                ["executionKind"] = effectRoute ? "effect" : "draw", ["geometryApplicable"] = !effectRoute,
                ["platformContract"] = request["platformContract"]?.DeepClone(), ["unityPlatform"] = UnityPlatform(),
                ["textureEvidence"] = textureEvidence,
                ["actualPipeline"] = actualPipeline, ["targetPipeline"] = expectedPipeline, ["urpActive"] = urp,
                ["vertexBindings"] = (request["mesh"] as JObject)?["shaderBindings"]?.DeepClone(),
                ["vertexLayout"] = mesh == null ? new JArray() : new JArray(mesh.GetVertexAttributes().Select(x => new JObject {
                    ["attribute"] = x.attribute.ToString(), ["components"] = x.dimension,
                    ["format"] = x.format.ToString(), ["stream"] = x.stream,
                    ["byteOffset"] = mesh.GetVertexAttributeOffset(x.attribute) })),
                ["visualEquivalenceVerified"] = false };
        }
    }
}
