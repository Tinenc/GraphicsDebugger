using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;
using Bread;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.SceneManagement;

namespace Bread.Editor
{
    internal static class BreadCaptureConfiguration
    {
        internal static UnityEngine.SceneManagement.Scene OpenOwnedScene(string path)
        {
            // Opening a saved scene additively preserves an unrelated untitled
            // scene; the Editor NewScene command refuses that case.
            if (!File.Exists(path))
            {
                File.WriteAllText(path, "%YAML 1.1\n%TAG !u! tag:unity3d.com,2011:\n--- !u!104 &1\nRenderSettings:\n  m_ObjectHideFlags: 0\n  serializedVersion: 9\n  m_Fog: 0\n");
                AssetDatabase.ImportAsset(path, ImportAssetOptions.ForceSynchronousImport);
            }
            return UnityEditor.SceneManagement.EditorSceneManager.OpenScene(path, UnityEditor.SceneManagement.OpenSceneMode.Additive);
        }
        internal static string Pipeline()
        {
            var active = GraphicsSettings.currentRenderPipeline;
            if (active == null) return "built-in";
            var name = active.GetType().FullName;
            return name.Contains("UniversalRenderPipelineAsset") ? "urp" : name.Contains("HDRenderPipelineAsset") ? "hdrp" : "custom";
        }

        internal static string Hash(string path)
        {
            using (var stream = File.OpenRead(path)) using (var hash = SHA256.Create())
                return BitConverter.ToString(hash.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
        }

        internal static void Gap(JArray gaps, string code, string detail)
        { gaps.Add(new JObject { ["code"] = code, ["detail"] = detail }); }

        private static float Number(JToken token)
        {
            if (token == null || (token.Type != JTokenType.Float && token.Type != JTokenType.Integer))
                throw new ArgumentException("Expected a finite number.");
            var value = (double)token;
            if (Double.IsNaN(value) || Double.IsInfinity(value) || Math.Abs(value) > Single.MaxValue)
                throw new ArgumentException("Value cannot be represented by Unity Float32.");
            return (float)value;
        }

        private static float[] Numbers(JToken token, int count = -1)
        {
            if (!(token is JArray array)) throw new ArgumentException("Expected a numeric array.");
            var values = array.SelectMany(x => x is JArray row ? row.AsEnumerable() : new[] { x }).Select(Number).ToArray();
            if (count >= 0 && values.Length != count) throw new ArgumentException("Unexpected numeric array dimensions.");
            return values;
        }

        private static Vector3 V3(JToken token)
        { var v = Numbers(token, 3); return new Vector3(v[0], v[1], v[2]); }

        internal static BreadParameterBinding Binding(JObject item)
        {
            var name = (string)item["name"] ?? (string)item["target"];
            if (String.IsNullOrEmpty(name)) throw new ArgumentException("A runtime binding needs a target name.");
            var type = (string)item["type"] ?? "float";
            var kind = (string)item["kind"];
            var rows = (int?)item["rows"] ?? 1;
            var columns = (int?)item["columns"] ?? 1;
            if (kind == "array") type = rows > 1 ? "matrix-array" : columns > 1 ? "vector-array" : "float-array";
            var value = item["value"] ?? item["values"];
            var binding = new BreadParameterBinding { name = name, type = type,
                scalarType = (string)item["scalarType"],
                sourceIdentity = (item["sourceIdentity"] ?? item["source"])?.ToString(Newtonsoft.Json.Formatting.None),
                components = (int?)item["components"] ?? columns, stride = (int?)item["stride"] ?? 4,
                bufferType = (string)item["bufferType"] ?? "structured",
                initialCounter = (long?)item["initialCounter"] ?? -1,
                counterProvenance = item["counterProvenance"]?.ToString(Newtonsoft.Json.Formatting.None),
                bufferBase64 = (string)item["bufferBase64"] ?? (string)item["dataBase64"] };
            if (type == "buffer") {
                if (binding.bufferType == "append" || binding.bufferType == "counter") {
                    var provenance = item["counterProvenance"] as JObject;
                    if (provenance == null || !new[] { "captured", "inferred", "unity-native" }.Contains((string)provenance["kind"]) || String.IsNullOrWhiteSpace((string)provenance["reason"]))
                        throw new ArgumentException("Initial buffer counter requires explicit evidence or reconstruction provenance.");
                }
                return binding;
            }
            if ((type == "vector" || type == "color") && new[] { "int", "uint", "bool" }.Contains(binding.scalarType))
                throw new ArgumentException("Integer vectors require an explicit typed buffer binding; SetVector would change their storage representation.");
            if (type == "int")
            {
                if (value is JArray integers && integers.Count == 1) value = integers[0];
                var integer = value.Type == JTokenType.Boolean ? ((bool)value ? 1L : 0L) : (long)value;
                if (integer < Int32.MinValue || integer > UInt32.MaxValue) throw new ArgumentException("Integer binding is outside 32-bit storage.");
                binding.integer = unchecked((int)integer);
                return binding;
            }
            binding.values = value is JArray ? Numbers(value) : new[] { Number(value) };
            if ((type == "float-array" || type == "vector-array") && new[] { "int", "uint", "bool" }.Contains((string)item["scalarType"]))
                throw new ArgumentException("Integer arrays require an explicit typed buffer binding and matching Shader access; float arrays would change their bit representation.");
            if (type == "matrix-array" && rows != 4 && item["rows"] != null || type == "matrix-array" && columns != 4 && item["columns"] != null)
                throw new ArgumentException("Non-4x4 matrix arrays require an explicit target layout adapter.");
            if (type == "array")
                binding.type = (string)item["elementType"] == "matrix" ? "matrix-array" :
                    binding.components > 1 ? "vector-array" : "float-array";
            if (type == "auto") binding.type = binding.values.Length == 1 ? "float" : "vector";
            if (binding.type == "matrix" && binding.values.Length != 16) throw new ArgumentException("Matrix binding needs 16 row-major values.");
            if ((binding.type == "vector" || binding.type == "color") && (binding.values.Length < 1 || binding.values.Length > 4))
                throw new ArgumentException("Vector binding has more than four components.");
            if (binding.values.Length > 1_048_576) throw new ArgumentException("Runtime binding is too large.");
            return binding;
        }

        internal static void Bind(GameObject root, JArray entries, JArray gaps)
        {
            if (entries == null) entries = new JArray();
            if (entries.Count == 0 && root.GetComponent<BreadCaptureEffect>() == null) return;
            var parsed = new List<BreadParameterBinding>();
            foreach (JObject entry in entries)
            {
                if ((string)entry["kind"] == "system" || (string)entry["classification"] == "system")
                {
                    // The Host requires a source-bound unity-native decision
                    // for each system interface. Captured camera/frame values
                    // must never become persistent material overrides.
                    continue;
                }
                try { parsed.Add(Binding(entry)); }
                catch (Exception error) { Gap(gaps, "runtime_binding_layout_requires_adapter", ((string)entry["name"] ?? (string)entry["target"]) + ": " + error.Message); }
            }
            if (parsed.GroupBy(x => x.name).Any(group => group.Count() > 1)) throw new ArgumentException("Duplicate runtime property binding.");
            var binder = root.GetComponent<BreadCaptureBindings>() ?? root.AddComponent<BreadCaptureBindings>();
            binder.parameterOnly = root.GetComponent<BreadCaptureEffect>() != null;
            binder.target = root.GetComponent<Renderer>();
            binder.bindings = parsed.ToArray();
            binder.Apply();
            if (binder.lastError != null) Gap(gaps, "runtime_binding_failed", binder.lastError);
            EditorUtility.SetDirty(binder);
        }

        internal static void Property(Material material, JObject item, JArray gaps)
        {
            BreadParameterBinding binding;
            try { binding = Binding(item); }
            catch (Exception error) { Gap(gaps, "material_property_layout_requires_adapter", ((string)item["name"] ?? (string)item["target"]) + ": " + error.Message); return; }
            if (!material.HasProperty(binding.name)) { Gap(gaps, "shader_property_absent", binding.name); return; }
            var v = binding.values;
            switch (binding.type)
            {
                case "float": material.SetFloat(binding.name, v[0]); break;
                case "int": material.SetInteger(binding.name, binding.integer); break;
                case "color": material.SetColor(binding.name, new Color(v[0], v.Length > 1 ? v[1] : 0, v.Length > 2 ? v[2] : 0, v.Length > 3 ? v[3] : 1)); break;
                case "vector": material.SetVector(binding.name, new Vector4(v[0], v.Length > 1 ? v[1] : 0, v.Length > 2 ? v[2] : 0, v.Length > 3 ? v[3] : 0)); break;
                default: Gap(gaps, "runtime_property_required", binding.name + ": " + binding.type); break;
            }
        }

        private static void Transform(Transform target, JObject settings)
        {
            if (settings == null) return;
            if (settings["position"] != null) target.position = V3(settings["position"]);
            if (settings["rotationEuler"] != null) target.rotation = Quaternion.Euler(V3(settings["rotationEuler"]));
            if (settings["rotation"] != null)
            { var q = Numbers(settings["rotation"], 4); target.rotation = new Quaternion(q[0], q[1], q[2], q[3]); }
            if (settings["scale"] != null) target.localScale = V3(settings["scale"]);
        }

        internal static void Scene(GameObject root, Camera camera, UnityEngine.SceneManagement.Scene scene, JObject settings, JArray gaps)
        {
            if (settings == null) { Gap(gaps, "capture_scene_not_configured", "Supply the observed object, camera and lighting configuration."); return; }
            Transform(root.transform, settings["object"] as JObject);
            var view = settings["camera"] as JObject;
            if (view == null) Gap(gaps, "capture_camera_not_configured", "The preview camera is a framing aid.");
            else
            {
                Transform(camera.transform, view);
                if (view["projection"] != null) camera.orthographic = (string)view["projection"] == "orthographic";
                if (view["near"] != null) camera.nearClipPlane = Number(view["near"]);
                if (view["far"] != null) camera.farClipPlane = Number(view["far"]);
                if (view["fieldOfView"] != null) camera.fieldOfView = Number(view["fieldOfView"]);
                if (view["orthographicSize"] != null) camera.orthographicSize = Number(view["orthographicSize"]);
                // A capture aspect is a calibration reference. Pinning the
                // camera's aspect would freeze it when its viewport resizes.
                bool fixedReference = view["fixedReferenceProjection"]?.Value<bool>() == true;
                if (fixedReference && camera.cameraType != CameraType.SceneView && view["aspect"] != null)
                    camera.aspect = Number(view["aspect"]);
                else if (camera.cameraType != CameraType.SceneView) camera.ResetAspect();
                if (view["allowHDR"] != null) camera.allowHDR = (bool)view["allowHDR"];
                if (view["allowMSAA"] != null) camera.allowMSAA = (bool)view["allowMSAA"];
                if (view["cullingMask"] != null) camera.cullingMask = (int)view["cullingMask"];
                if (view["clearFlags"] != null) camera.clearFlags = (CameraClearFlags)Enum.Parse(typeof(CameraClearFlags), (string)view["clearFlags"], true);
                if (view["backgroundColor"] != null) { var color = Numbers(view["backgroundColor"], 4); camera.backgroundColor = new Color(color[0], color[1], color[2], color[3]); }
                if (view["rect"] != null) { var rect = Numbers(view["rect"], 4); camera.rect = new Rect(rect[0], rect[1], rect[2], rect[3]); }
                if (view["projectionMatrix"] != null && camera.cameraType != CameraType.SceneView)
                {
                    var projection = camera.GetComponent<BreadCaptureCameraState>() ?? camera.gameObject.AddComponent<BreadCaptureCameraState>();
                    projection.projectionRowMajor = Numbers(view["projectionMatrix"], 16);
                    projection.useProjectionOverride = true;
                    projection.adaptToViewport = !fixedReference;
                    projection.Apply(); EditorUtility.SetDirty(projection);
                }
                else if (view["useNativeProjection"]?.Value<bool>() == true)
                {
                    var projection = camera.GetComponent<BreadCaptureCameraState>();
                    if (projection != null) { projection.useProjectionOverride = false; projection.Apply(); EditorUtility.SetDirty(projection); }
                }
                if (camera.nearClipPlane <= 0 || camera.farClipPlane <= camera.nearClipPlane)
                    throw new ArgumentException("Camera near/far planes are invalid.");
            }
            if (settings["lights"] is JArray lights)
            {
                foreach (var existing in scene.GetRootGameObjects().Where(x => x.name.StartsWith("Bread Preview Light", StringComparison.Ordinal)))
                    UnityEngine.Object.DestroyImmediate(existing);
                foreach (JObject item in lights)
                {
                    var name = (string)item["name"] ?? "Bread Restored Light";
                    var obj = scene.GetRootGameObjects().FirstOrDefault(x => x.name == name && x.GetComponent<Light>() != null);
                    if (obj == null) { obj = new GameObject(name); SceneManager.MoveGameObjectToScene(obj, scene); }
                    var light = obj.GetComponent<Light>() ?? obj.AddComponent<Light>();
                    Transform(obj.transform, item);
                    if (item["type"] != null) light.type = (LightType)Enum.Parse(typeof(LightType), (string)item["type"], true);
                    if (item["intensity"] != null) light.intensity = Number(item["intensity"]);
                    if (item["range"] != null) light.range = Number(item["range"]);
                    if (item["spotAngle"] != null) light.spotAngle = Number(item["spotAngle"]);
                    if (item["shadows"] != null) light.shadows = (LightShadows)Enum.Parse(typeof(LightShadows), (string)item["shadows"], true);
                    if (item["shadowStrength"] != null) light.shadowStrength = Number(item["shadowStrength"]);
                    if (item["shadowBias"] != null) light.shadowBias = Number(item["shadowBias"]);
                    if (item["shadowNormalBias"] != null) light.shadowNormalBias = Number(item["shadowNormalBias"]);
                    if (item["color"] != null) { var c = Numbers(item["color"], 4); light.color = new Color(c[0], c[1], c[2], c[3]); }
                }
            }
            else Gap(gaps, "capture_lighting_not_configured", "Explicitly choose restored lights or an empty list for an unlit/frozen-light effect.");
            if (settings["components"] is JArray components)
                foreach (JObject item in components)
                {
                    var targetName = (string)item["target"];
                    var target = targetName == "camera" ? camera.gameObject : targetName == "object" ? root : null;
                    if (target == null) throw new ArgumentException("Capture components must target camera or object.");
                    var name = (string)item["type"];
                    var type = AppDomain.CurrentDomain.GetAssemblies().Select(assembly => assembly.GetType(name, false)).FirstOrDefault(candidate => candidate != null);
                    if (type == null || !typeof(Component).IsAssignableFrom(type))
                    { Gap(gaps, "scene_component_type_unavailable", name); continue; }
                    var component = target.GetComponent(type) ?? target.AddComponent(type);
                    if (item["settings"] is JObject values) EditorJsonUtility.FromJsonOverwrite(values.ToString(), component);
                    if (item["assetReferences"] is JObject references)
                    {
                        var serialized = new SerializedObject(component);
                        foreach (var reference in references.Properties())
                        {
                            var field = serialized.FindProperty(reference.Name);
                            if (field == null || field.propertyType != SerializedPropertyType.ObjectReference)
                                throw new ArgumentException("Component asset reference is not an object field: " + reference.Name);
                            var asset = AssetDatabase.LoadAssetAtPath<UnityEngine.Object>((string)reference.Value);
                            if (asset == null) throw new ArgumentException("Component asset reference is unavailable: " + reference.Value);
                            field.objectReferenceValue = asset;
                        }
                        serialized.ApplyModifiedPropertiesWithoutUndo();
                    }
                    EditorUtility.SetDirty(component);
                }
        }

        internal static void RenderState(Material material, JObject state, JArray gaps)
        {
            if (state == null) { Gap(gaps, "draw_state_not_reviewed", "Supply required passes and the captured state-to-Shader mapping."); return; }
            if (state["renderQueue"] != null) material.renderQueue = (int)state["renderQueue"];
            if (state["properties"] is JObject properties)
                foreach (var property in properties.Properties()) Property(material, new JObject { ["name"] = property.Name, ["value"] = property.Value, ["type"] = "float" }, gaps);
            if (state["keywords"] is JArray keywords) material.shaderKeywords = keywords.Select(x => (string)x).ToArray();
            if (state["requiredPasses"] is JArray passes)
                foreach (var name in passes.Select(x => (string)x))
                    if (material.FindPass(name) < 0) Gap(gaps, "required_shader_pass_missing", name);
            if (state["passEnabled"] is JObject passEnabled)
                foreach (var pass in passEnabled.Properties()) material.SetShaderPassEnabled(pass.Name, (bool)pass.Value);
            if (state["requiredPasses"] is JArray required)
                foreach (var name in required.Select(value => (string)value))
                    if (material.FindPass(name) >= 0 && !material.GetShaderPassEnabled(name)) Gap(gaps, "required_shader_pass_disabled", name);
            EditorUtility.SetDirty(material);
        }

        internal static JObject CameraSnapshot(Camera camera)
        {
            return new JObject { ["name"] = camera.name, ["position"] = new JArray(camera.transform.position.x, camera.transform.position.y, camera.transform.position.z),
                ["rotation"] = new JArray(camera.transform.rotation.x, camera.transform.rotation.y, camera.transform.rotation.z, camera.transform.rotation.w),
                ["projection"] = camera.orthographic ? "orthographic" : "perspective", ["near"] = camera.nearClipPlane, ["far"] = camera.farClipPlane,
                ["fieldOfView"] = camera.fieldOfView, ["orthographicSize"] = camera.orthographicSize, ["aspect"] = camera.aspect,
                ["allowHDR"] = camera.allowHDR, ["allowMSAA"] = camera.allowMSAA, ["cullingMask"] = camera.cullingMask,
                ["clearFlags"] = camera.clearFlags.ToString(), ["backgroundColor"] = new JArray(camera.backgroundColor.r, camera.backgroundColor.g, camera.backgroundColor.b, camera.backgroundColor.a),
                ["rect"] = new JArray(camera.rect.x, camera.rect.y, camera.rect.width, camera.rect.height),
                ["projectionMatrix"] = new JArray(Enumerable.Range(0, 16).Select(i => camera.projectionMatrix[i / 4, i % 4])),
                ["worldToCameraMatrix"] = new JArray(Enumerable.Range(0, 16).Select(i => camera.worldToCameraMatrix[i / 4, i % 4])) };
        }

        internal static JObject MaterialSnapshot(Material material)
        {
            if (material == null) return null;
            var properties = new JObject();
            var propertyTypes = new JObject();
            var shader = material.shader;
            for (int i = 0; shader != null && i < shader.GetPropertyCount(); i++)
            {
                var name = shader.GetPropertyName(i);
                propertyTypes[name] = shader.GetPropertyType(i).ToString();
                switch (shader.GetPropertyType(i))
                {
                    case ShaderPropertyType.Color: var color = material.GetColor(name); properties[name] = new JArray(color.r, color.g, color.b, color.a); break;
                    case ShaderPropertyType.Vector: var vector = material.GetVector(name); properties[name] = new JArray(vector.x, vector.y, vector.z, vector.w); break;
                    case ShaderPropertyType.Texture:
                        var texture = material.GetTexture(name); properties[name] = texture == null ? null : AssetDatabase.GetAssetPath(texture); break;
                    case ShaderPropertyType.Int: properties[name] = material.GetInteger(name); break;
                    default: properties[name] = material.GetFloat(name); break;
                }
            }
            return new JObject { ["path"] = AssetDatabase.GetAssetPath(material), ["shader"] = shader == null ? "" : shader.name,
                ["shaderPath"] = shader == null ? "" : AssetDatabase.GetAssetPath(shader), ["properties"] = properties, ["propertyTypes"] = propertyTypes,
                ["renderQueue"] = material.renderQueue, ["keywords"] = new JArray(material.shaderKeywords),
                ["passes"] = new JArray(Enumerable.Range(0, material.passCount).Select(i => new JObject { ["name"] = material.GetPassName(i), ["enabled"] = material.GetShaderPassEnabled(material.GetPassName(i)) })),
                ["serialized"] = StableSerialization(material) };
        }

        internal static JToken StableSerialization(UnityEngine.Object value)
        {
            var serialized = JToken.Parse(EditorJsonUtility.ToJson(value));
            foreach (var reference in ((JContainer)serialized).DescendantsAndSelf().OfType<JObject>().Where(item => item["instanceID"] != null).ToArray())
            {
                var target = EditorUtility.InstanceIDToObject((int)reference["instanceID"]);
                reference.RemoveAll();
                if (target == null) { reference["null"] = true; continue; }
                if (AssetDatabase.TryGetGUIDAndLocalFileIdentifier(target, out string guid, out long localId) && !String.IsNullOrEmpty(guid))
                { reference["assetGuid"] = guid; reference["localId"] = localId; reference["assetPath"] = AssetDatabase.GetAssetPath(target); }
                else
                {
                    var transform = target is Component component ? component.transform : (target as GameObject)?.transform;
                    var hierarchy = target.name;
                    if (transform != null) { hierarchy = transform.name; for (var parent = transform.parent; parent != null; parent = parent.parent) hierarchy = parent.name + "/" + hierarchy; }
                    reference["objectPath"] = hierarchy; reference["type"] = target.GetType().FullName;
                }
            }
            return serialized;
        }

        internal static JObject BindingSnapshot(BreadCaptureBindings binder)
        {
            var entries = new JArray();
            var errors = new JArray();
            if (binder == null) return new JObject { ["readback"] = entries, ["errors"] = errors };
            binder.Apply();
            if (binder.lastError != null) errors.Add(binder.lastError);
            var effect = binder.GetComponent<BreadCaptureEffect>();
            if (effect != null) effect.PrepareUploads(true);
            var block = binder.ParameterBlock ?? new MaterialPropertyBlock();
            if (binder.target != null) binder.target.GetPropertyBlock(block);
            foreach (var item in binder.bindings)
            {
                JToken value;
                switch (item.type)
                {
                    case "float": value = block.GetFloat(item.name); break;
                    case "int": value = item.scalarType == "uint" ? (JToken)new JValue(unchecked((uint)block.GetInteger(item.name))) : new JValue(block.GetInteger(item.name)); break;
                    case "vector": case "color": var v = block.GetVector(item.name); value = new JArray(v.x, v.y, v.z, v.w); break;
                    case "matrix": var m = block.GetMatrix(item.name); value = new JArray(Enumerable.Range(0, 16).Select(i => m[i / 4, i % 4])); break;
                    case "float-array": value = new JArray(block.GetFloatArray(item.name)); break;
                    case "vector-array": value = new JArray(block.GetVectorArray(item.name).Select(x => new JArray(x.x, x.y, x.z, x.w))); break;
                    case "matrix-array": value = new JArray(block.GetMatrixArray(item.name).Select(x => new JArray(Enumerable.Range(0, 16).Select(i => x[i / 4, i % 4])))); break;
                    case "buffer":
                        var words = binder.ReadBuffer(item.name);
                        if (words == null) value = null;
                        else
                        {
                            var bytes = new byte[words.Length * 4]; Buffer.BlockCopy(words, 0, bytes, 0, bytes.Length);
                            string digest; using (var hash = SHA256.Create()) digest = BitConverter.ToString(hash.ComputeHash(bytes)).Replace("-", "").ToLowerInvariant();
                            value = new JObject { ["byteLength"] = bytes.Length, ["sha256"] = digest, ["previewWords"] = new JArray(words.Take(16)), ["hashCoversAllBytes"] = true };
                            if (item.bufferType == "append" || item.bufferType == "counter") {
                                value["counter"] = binder.ReadCounter(item.name); value["counterProvenance"] = item.counterProvenance; }
                        }
                        break;
                    default: errors.Add("Unsupported binding readback: " + item.type); value = null; break;
                }
                entries.Add(new JObject { ["name"] = item.name, ["type"] = item.type, ["value"] = value,
                    ["scalarType"] = item.scalarType,
                    ["sourceIdentity"] = item.sourceIdentity,
                    ["scope"] = item.type == "buffer" ? "gpu-buffer-data" : effect != null ? "compute-upload" : "renderer-property-block",
                    ["uploadVerified"] = effect != null && effect.uploadsVerified });
            }
            return new JObject { ["readback"] = entries, ["errors"] = errors };
        }

        internal static JArray Features(JArray requested, bool install, JArray gaps, Camera camera = null)
        {
            var result = new JArray();
            if (requested == null) return result;
            foreach (JObject item in requested)
            {
                var path = (string)item["rendererDataAssetPath"];
                if (String.IsNullOrEmpty(path) || !path.StartsWith("Assets/", StringComparison.Ordinal) || path.Contains(".."))
                    throw new ArgumentException("Renderer data must be an existing project Assets path.");
                var renderer = AssetDatabase.LoadAssetAtPath<ScriptableObject>(path);
                var list = renderer?.GetType().GetProperty("rendererFeatures")?.GetValue(renderer) as IList;
                var name = (string)item["type"];
                var type = AppDomain.CurrentDomain.GetAssemblies().Select(assembly => assembly.GetType(name, false)).FirstOrDefault(candidate => candidate != null);
                ScriptableObject feature = list?.Cast<object>().FirstOrDefault(x => x != null && x.GetType() == type) as ScriptableObject;
                if (install && feature == null && list != null && type != null && typeof(ScriptableObject).IsAssignableFrom(type))
                {
                    var element = list.GetType().GetGenericArguments().FirstOrDefault();
                    if (element == null || !element.IsAssignableFrom(type)) throw new ArgumentException("Feature type is incompatible with the active renderer.");
                    feature = ScriptableObject.CreateInstance(type); feature.name = type.Name;
                    if (item["settings"] is JObject settings) EditorJsonUtility.FromJsonOverwrite(settings.ToString(), feature);
                    AssetDatabase.AddObjectToAsset(feature, renderer); list.Add(feature);
                    EditorUtility.SetDirty(feature); EditorUtility.SetDirty(renderer); AssetDatabase.SaveAssets();
                }
                if (install && feature != null)
                {
                    if (item["settings"] is JObject settings) EditorJsonUtility.FromJsonOverwrite(settings.ToString(), feature);
                    if (item["active"] != null) feature.GetType().GetMethod("SetActive")?.Invoke(feature, new object[] { (bool)item["active"] });
                    if (item["assetReferences"] is JObject references)
                    {
                        var serialized = new SerializedObject(feature);
                        foreach (var reference in references.Properties())
                        {
                            var field = serialized.FindProperty(reference.Name);
                            var asset = AssetDatabase.LoadAssetAtPath<UnityEngine.Object>((string)reference.Value);
                            if (field == null || field.propertyType != SerializedPropertyType.ObjectReference || asset == null)
                                throw new ArgumentException("Invalid feature asset reference: " + reference.Name);
                            field.objectReferenceValue = asset;
                        }
                        serialized.ApplyModifiedPropertiesWithoutUndo();
                    }
                    EditorUtility.SetDirty(feature); EditorUtility.SetDirty(renderer); AssetDatabase.SaveAssets();
                }
                if (feature == null) Gap(gaps, "renderer_feature_not_installed", name + " at " + path);
                bool assigned = false;
                var active = GraphicsSettings.currentRenderPipeline;
                if (active != null && camera != null)
                {
                    var pipeline = new SerializedObject(active);
                    var renderers = pipeline.FindProperty("m_RendererDataList");
                    var index = pipeline.FindProperty("m_DefaultRendererIndex")?.intValue ?? 0;
                    var additional = camera.GetComponents<Component>().FirstOrDefault(component => component != null && component.GetType().FullName == "UnityEngine.Rendering.Universal.UniversalAdditionalCameraData");
                    if (additional != null)
                    { var choice = new SerializedObject(additional).FindProperty("m_RendererIndex")?.intValue ?? -1; if (choice >= 0) index = choice; }
                    assigned = renderers != null && index >= 0 && index < renderers.arraySize && renderers.GetArrayElementAtIndex(index).objectReferenceValue == renderer;
                }
                if (camera != null && !assigned) Gap(gaps, "renderer_feature_not_on_camera_renderer", name + " at " + path);
                result.Add(new JObject { ["rendererDataAssetPath"] = path, ["type"] = name, ["installed"] = feature != null,
                    ["active"] = feature == null ? (JToken)null : new JValue(feature.GetType().GetProperty("isActive")?.GetValue(feature)),
                    ["settings"] = feature == null ? null : StableSerialization(feature),
                    ["activeRendererAssignmentVerified"] = assigned,
                    ["rendererAssetSha256"] = File.Exists(path) ? Hash(path) : null });
            }
            return result;
        }
    }
}
