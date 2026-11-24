using System;
using System.Collections.Generic;
using System.Linq;
using Newtonsoft.Json.Linq;
using UnityEditor;
using UnityEngine;
using UnityEngine.Experimental.Rendering;

namespace Bread.Editor
{
    // Opt-in only: CustomEditor "Bread.Editor.BreadRestoredShaderGUI".
    // Does not replace URP/HDRP or third-party Shader inspectors globally.
    public sealed class BreadRestoredShaderGUI : ShaderGUI
    {
        const string Key = "breadMaterialInspector";
        readonly Dictionary<string, Tuple<string, JObject>> cache = new Dictionary<string, Tuple<string, JObject>>();
        static string Text(string english, string chinese) => Application.systemLanguage == SystemLanguage.ChineseSimplified ? chinese : english;
        static bool Flag(JObject descriptor, string name) => descriptor?[name]?.Type == JTokenType.Boolean && (bool)descriptor[name];

        public static bool Store(Material material, JObject presentation)
        {
            if (presentation == null || (string)presentation["schema"] != "bread-material-inspector/v1") return false;
            var path = AssetDatabase.GetAssetPath(material);
            var importer = AssetImporter.GetAtPath(path);
            if (importer == null) return false;
            JObject data;
            try { data = String.IsNullOrWhiteSpace(importer.userData) ? new JObject() : JObject.Parse(importer.userData); }
            catch { return false; } // Preserve another plugin's opaque data unchanged.
            data[Key] = presentation.DeepClone();
            importer.userData = data.ToString(Newtonsoft.Json.Formatting.None);
            AssetDatabase.WriteImportSettingsIfDirty(path);
            return true;
        }

        JObject Metadata(Material material)
        {
            var path = AssetDatabase.GetAssetPath(material);
            var importer = AssetImporter.GetAtPath(path);
            var text = importer != null ? importer.userData : "";
            if (String.IsNullOrEmpty(text) || text.Length > 2 * 1024 * 1024) return null;
            Tuple<string, JObject> entry;
            if (cache.TryGetValue(path, out entry) && entry.Item1 == text) return entry.Item2;
            JObject result = null;
            try
            {
                var value = JObject.Parse(text)[Key] as JObject;
                if ((string)value?["schema"] == "bread-material-inspector/v1") result = value;
            }
            catch { /* Optional labels never prevent material editing. */ }
            if (cache.Count >= 128) cache.Clear();
            cache[path] = Tuple.Create(text, result);
            return result;
        }

        JObject Descriptor(MaterialEditor editor, string name)
        {
            JObject common = null;
            foreach (var target in editor.targets.OfType<Material>())
            {
                var list = Metadata(target)?["properties"] as JArray;
                var item = list?.OfType<JObject>().FirstOrDefault(value => value["name"]?.Type == JTokenType.String && (string)value["name"] == name);
                if (item != null && new[] { "label", "group", "tooltip", "control" }.Any(key =>
                    item[key] != null && item[key].Type != JTokenType.String)) return null;
                if (item != null && new[] { "min", "max" }.Any(key => item[key] != null &&
                    item[key].Type != JTokenType.Float && item[key].Type != JTokenType.Integer)) return null;
                if (item == null || (common != null && !JToken.DeepEquals(common, item))) return null;
                common = item;
            }
            return common;
        }

        static bool HasDrawer(Material material, MaterialProperty property)
        {
            var index = material.shader.FindPropertyIndex(property.name);
            return index >= 0 && material.shader.GetPropertyAttributes(index).Length > 0;
        }

        void Draw(MaterialEditor editor, MaterialProperty property, JObject descriptor)
        {
            var label = (string)descriptor?["label"];
            if (String.IsNullOrWhiteSpace(label)) label = ObjectNames.NicifyVariableName(property.displayName);
            var tooltip = property.name + (String.IsNullOrWhiteSpace((string)descriptor?["tooltip"]) ? "" : "\n" + (string)descriptor["tooltip"]);
            var content = new GUIContent(label, tooltip);
            var material = editor.target as Material;
            var control = (string)descriptor?["control"];
            // Native ShaderLab drawers retain their keyword/enum semantics.
            if (material != null && !HasDrawer(material, property) && control == "color" && property.type == MaterialProperty.PropType.Vector)
            {
                var vector = property.vectorValue;
                var oldMixed = EditorGUI.showMixedValue;
                EditorGUI.showMixedValue = property.hasMixedValue;
                try
                {
                    EditorGUI.BeginChangeCheck();
                    var value = EditorGUILayout.ColorField(content, new Color(vector.x, vector.y, vector.z, vector.w), true,
                        Flag(descriptor, "alpha"), Flag(descriptor, "hdr"));
                    if (EditorGUI.EndChangeCheck())
                    {
                        editor.RegisterPropertyChangeUndo(label);
                        property.vectorValue = new Vector4(value.r, value.g, value.b, Flag(descriptor, "alpha") ? value.a : vector.w);
                    }
                }
                finally { EditorGUI.showMixedValue = oldMixed; }
            }
            else if (material != null && !HasDrawer(material, property) && control == "slider" && property.type == MaterialProperty.PropType.Float)
                DrawSlider(editor, property, descriptor, content);
            else editor.ShaderProperty(property, content);

            if (property.type == MaterialProperty.PropType.Texture && !property.hasMixedValue && property.textureValue != null)
            {
                var texture = property.textureValue;
                var depth = texture is Texture3D ? ((Texture3D)texture).depth : texture is Texture2DArray ? ((Texture2DArray)texture).depth : 1;
                var info = texture.width + " × " + texture.height + (depth > 1 ? " × " + depth : "") + " · " + texture.dimension
                    + " · " + texture.graphicsFormat + " · " + (GraphicsFormatUtility.IsSRGBFormat(texture.graphicsFormat) ? "sRGB" : "Linear")
                    + " · mip " + texture.mipmapCount + " · " + texture.filterMode + " · " + texture.wrapMode;
                EditorGUILayout.SelectableLabel(info, EditorStyles.miniLabel, GUILayout.Height(EditorGUIUtility.singleLineHeight));
            }
        }

        static void DrawSlider(MaterialEditor editor, MaterialProperty property, JObject descriptor, GUIContent label)
        {
            float low, high;
            if (!Single.TryParse((string)descriptor["min"], System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out low)
                || !Single.TryParse((string)descriptor["max"], System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out high)
                || Single.IsInfinity(low) || Single.IsInfinity(high) || Single.IsNaN(low) || Single.IsNaN(high) || low >= high)
            { editor.ShaderProperty(property, label); return; }
            var oldMixed = EditorGUI.showMixedValue;
            EditorGUI.showMixedValue = property.hasMixedValue;
            try
            {
                // Do not let merely repainting a slider clamp an HDR/outlier
                // captured value. Numeric entry always remains unbounded.
                EditorGUI.BeginChangeCheck();
                var numeric = EditorGUILayout.FloatField(label, property.floatValue);
                if (EditorGUI.EndChangeCheck()) { editor.RegisterPropertyChangeUndo(label.text); property.floatValue = numeric; }
                EditorGUI.BeginChangeCheck();
                var slider = GUILayout.HorizontalSlider(Mathf.Clamp(property.floatValue, low, high), low, high);
                if (EditorGUI.EndChangeCheck()) { editor.RegisterPropertyChangeUndo(label.text); property.floatValue = slider; }
            }
            finally { EditorGUI.showMixedValue = oldMixed; }
        }

        public override void OnGUI(MaterialEditor editor, MaterialProperty[] properties)
        {
            var visible = properties.Where(p => (p.flags & MaterialProperty.PropFlags.HideInInspector) == 0).ToArray();
            var descriptors = visible.ToDictionary(p => p.name, p => Descriptor(editor, p.name));
            var textures = visible.Where(p => p.type == MaterialProperty.PropType.Texture).ToArray();
            if (textures.Length > 0)
            {
                EditorGUILayout.LabelField(Text("Textures", "贴图"), EditorStyles.boldLabel);
                foreach (var property in textures) Draw(editor, property, descriptors[property.name]);
            }
            foreach (var group in visible.Where(p => p.type != MaterialProperty.PropType.Texture).GroupBy(p =>
                (string)descriptors[p.name]?["group"] ?? (p.type == MaterialProperty.PropType.Color || (string)descriptors[p.name]?["control"] == "color"
                    ? Text("Colors", "颜色") : Text("Parameters", "参数"))))
            {
                EditorGUILayout.Space();
                EditorGUILayout.LabelField(group.Key, EditorStyles.boldLabel);
                foreach (var property in group) Draw(editor, property, descriptors[property.name]);
            }
            EditorGUILayout.Space();
            editor.RenderQueueField();
            editor.EnableInstancingField();
            editor.DoubleSidedGIField();
        }
    }
}
