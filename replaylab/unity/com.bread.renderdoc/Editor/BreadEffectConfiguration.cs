using System;
using System.IO;
using System.Linq;
using Bread;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;

namespace Bread.Editor
{
    internal static class BreadEffectConfiguration
    {
        internal const string CopyImplementation = "Packages/com.bread.renderdoc/Runtime/BreadCaptureEffect.cs";
        internal static string AssetPath(string folder, string asset)
        {
            if (String.IsNullOrWhiteSpace(asset) || asset.Contains("\\") || asset.Contains(":") || asset.StartsWith("/") ||
                asset.Split('/').Any(part => String.IsNullOrEmpty(part) || part == "." || part == ".."))
                throw new ArgumentException("Effect asset must be a canonical project Assets path or imported relative path.");
            var path = asset.StartsWith("Assets/", StringComparison.Ordinal) ? asset : folder + "/" + asset;
            if (!path.StartsWith("Assets/", StringComparison.Ordinal)) throw new ArgumentException("Effect asset must belong to this project.");
            var physical = Path.GetFullPath(path);
            var assetsRoot = Path.GetFullPath(Application.dataPath).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            if (!physical.StartsWith(assetsRoot, Application.platform == RuntimePlatform.WindowsEditor ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal))
                throw new ArgumentException("Effect asset escaped this project.");
            for (var part = physical; !String.IsNullOrEmpty(part); part = Path.GetDirectoryName(part))
                if ((File.Exists(part) || Directory.Exists(part)) && (File.GetAttributes(part) & FileAttributes.ReparsePoint) != 0)
                    throw new ArgumentException("Redirected effect assets are unsupported.");
            return path;
        }

        internal static BreadCaptureEffect Configure(GameObject root, string folder, JObject settings, JArray importedTextures, JArray bindings, JArray gaps)
        {
            var effect = root.GetComponent<BreadCaptureEffect>() ?? root.AddComponent<BreadCaptureEffect>();
            if (settings == null) { BreadCaptureConfiguration.Gap(gaps, "effect_implementation_required", "Create the captured algorithm using compute, fullscreen or custom passes."); return effect; }
            var resources = settings["resources"] as JArray ?? new JArray();
            var passes = settings["passes"] as JArray ?? new JArray();
            if (resources.Count < 1 || resources.Count > 128 || passes.Count < 1 || passes.Count > 128)
                throw new ArgumentException("Effect requires bounded resource and executable pass lists.");
            effect.output = (string)settings["output"];
            effect.warmupFrames = (int?)settings["warmupFrames"] ?? 0;
            effect.resources = resources.OfType<JObject>().Select(row => {
                var initial = row["initialization"] as JObject;
                if (initial == null || initial["provenance"] == null) throw new ArgumentException("Effect input initialization needs explicit provenance.");
                var result = new BreadEffectResource { name = (string)row["name"], width = (int)row["width"], height = (int)row["height"],
                    format = (string)row["format"], historyFrom = (string)row["historyFrom"], initializationKind = (string)initial["kind"],
                    provenance = initial["provenance"].ToString(Newtonsoft.Json.Formatting.None),
                    randomWrite = (bool?)row["randomWrite"] == true || passes.Any(pass => (string)pass["kind"] == "compute" && (string)pass["output"] == (string)row["name"]) };
                if (result.width < 1 || result.width > 16384 || result.height < 1 || result.height > 16384) throw new ArgumentException("Effect dimensions are invalid.");
                if (initial["value"] is JArray color && color.Count == 4) result.initialValue = new Color((float)color[0], (float)color[1], (float)color[2], (float)color[3]);
                if (result.initializationKind == "texture")
                {
                    var property = (string)initial["textureProperty"];
                    var asset = (string)initial["textureAsset"];
                    if (String.IsNullOrEmpty(property) == String.IsNullOrEmpty(asset)) throw new ArgumentException("Texture initialization must choose exactly one imported property or project asset.");
                    if (asset != null) result.sourceTexture = AssetDatabase.LoadAssetAtPath<Texture>(AssetPath(folder, asset));
                    else for (int index = 0; index < importedTextures.Count; index++) if ((string)importedTextures[index]["property"] == property)
                        result.sourceTexture = AssetDatabase.LoadAssetAtPath<Texture>(folder + "/Texture_" + index + (string)importedTextures[index]["extension"]);
                    if (result.sourceTexture == null) throw new ArgumentException("Effect texture initializer is unavailable: " + (property ?? asset));
                }
                return result;
            }).ToArray();
            if (effect.resources.Sum(resource => (long)resource.width * resource.height) > 64L * 1024 * 1024)
                throw new ArgumentException("Effect resource allocation exceeds its budget.");
            effect.passes = passes.OfType<JObject>().Select(row => {
                var asset = (string)row["asset"];
                var kind = (string)row["kind"];
                if ((string)row["insertionPoint"] != "AfterRendering")
                    throw new ArgumentException("The effect runner executes at AfterRendering. Use an actual RendererFeature or CustomPass adapter for another insertion point.");
                if (kind == "copy" && asset != null && asset != CopyImplementation)
                    throw new ArgumentException("Copy implementation must identify the actual BreadCaptureEffect.cs package asset.");
                var assetPath = kind == "copy" ? CopyImplementation : asset == null ? null : AssetPath(folder, asset);
                var pass = new BreadEffectPass { id = (string)row["id"], kind = kind, implementationAsset = kind == "copy" ? CopyImplementation : asset,
                    entryPoint = (string)row["entryPoint"], insertionPoint = (string)row["insertionPoint"], kernel = (string)row["kernel"],
                    source = (string)row["source"], output = (string)row["output"], outputVersion = (string)row["outputVersion"],
                    globalTextureName = (string)row["globalTextureName"], materialPass = (int?)row["materialPass"] ?? 0,
                    reads = (row["reads"] as JArray ?? new JArray()).Select(value => (string)value).ToArray(),
                    writes = (row["writes"] as JArray ?? new JArray()).Select(value => (string)value).ToArray(),
                    groups = (row["groups"] as JArray ?? new JArray()).Select(value => (int)value).ToArray(),
                    textures = (row["textures"] as JObject ?? new JObject()).Properties().Select(pair => new BreadEffectTextureBinding { property = pair.Name, resource = (string)pair.Value }).ToArray() };
                if (pass.kind == "compute") pass.compute = AssetDatabase.LoadAssetAtPath<ComputeShader>(assetPath);
                else if (pass.kind == "fullscreen")
                {
                    var shader = AssetDatabase.LoadAssetAtPath<Shader>(assetPath);
                    if (shader == null) throw new ArgumentException("Fullscreen effect Shader is missing.");
                    var materialPath = folder + "/EffectMaterial_" + Array.IndexOf(passes.ToArray(), row) + ".mat";
                    var material = AssetDatabase.LoadAssetAtPath<Material>(materialPath);
                    if (material == null) { material = new Material(shader); AssetDatabase.CreateAsset(material, materialPath); } else material.shader = shader;
                    pass.material = material;
                }
                else if (pass.kind == "custom")
                {
                    var name = (string)row["componentType"];
                    var type = AppDomain.CurrentDomain.GetAssemblies().Select(assembly => assembly.GetType(name, false)).FirstOrDefault(candidate => candidate != null);
                    if (type == null || !typeof(MonoBehaviour).IsAssignableFrom(type) || !typeof(IBreadCaptureEffectPass).IsAssignableFrom(type))
                        throw new ArgumentException("Custom effect implementation is unavailable: " + name);
                    var script = AssetDatabase.LoadAssetAtPath<MonoScript>(assetPath);
                    if (script == null || script.GetClass() != type) throw new ArgumentException("Custom effect asset does not declare the selected implementation type.");
                    pass.custom = (MonoBehaviour)(root.GetComponent(type) ?? root.AddComponent(type));
                    if (row["settings"] is JObject values) EditorJsonUtility.FromJsonOverwrite(values.ToString(), pass.custom);
                    EditorUtility.SetDirty(pass.custom);
                }
                return pass;
            }).ToArray();
            BreadCaptureConfiguration.Bind(root, bindings ?? new JArray(), gaps);
            effect.parameters = root.GetComponent<BreadCaptureBindings>();
            effect.PrepareUploads(true);
            if (effect.lastError != null) BreadCaptureConfiguration.Gap(gaps, "effect_input_upload_failed", effect.lastError);
            foreach (var pass in effect.passes) if (pass.material != null) EditorUtility.SetDirty(pass.material);
            EditorUtility.SetDirty(effect); return effect;
        }

        internal static JObject Snapshot(BreadCaptureEffect effect)
        {
            if (effect == null) return null;
            return new JObject { ["kind"] = "executable-effect", ["warmupFrames"] = effect.warmupFrames, ["output"] = effect.output,
                ["uploadsVerified"] = effect.uploadsVerified, ["error"] = effect.lastError,
                ["resources"] = new JArray(effect.resources.Select(resource => new JObject { ["name"] = resource.name,
                    ["width"] = resource.width, ["height"] = resource.height, ["format"] = resource.format,
                    ["initializationKind"] = resource.initializationKind, ["provenance"] = resource.provenance,
                    ["historyFrom"] = resource.historyFrom, ["sourceTexture"] = AssetDatabase.GetAssetPath(resource.sourceTexture) })),
                ["passes"] = new JArray(effect.passes.Select(pass => new JObject { ["id"] = pass.id, ["kind"] = pass.kind,
                    ["asset"] = pass.implementationAsset, ["entryPoint"] = pass.entryPoint, ["output"] = pass.output })) };
        }
    }
}
