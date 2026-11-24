using System;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.Experimental.Rendering;

namespace Bread
{
    [Serializable] public sealed class BreadEffectResource
    {
        public string name, format, initializationKind, provenance, historyFrom;
        public int width, height;
        public bool randomWrite;
        public Color initialValue;
        public Texture sourceTexture;
    }
    [Serializable] public sealed class BreadEffectTextureBinding { public string property, resource; }
    [Serializable] public sealed class BreadEffectPass
    {
        public string id, kind, implementationAsset, entryPoint, insertionPoint, kernel, source, output, outputVersion, globalTextureName;
        public string[] reads = Array.Empty<string>(), writes = Array.Empty<string>();
        public int[] groups = Array.Empty<int>();
        public ComputeShader compute;
        public Material material;
        public MonoBehaviour custom;
        public int materialPass;
        public BreadEffectTextureBinding[] textures = Array.Empty<BreadEffectTextureBinding>();
    }

    // Agent-authored CPU/native/custom alternatives can implement this contract.
    // They still execute inside the same measured render and emit real outputs.
    public interface IBreadCaptureEffectPass
    { void Execute(BreadCaptureEffect effect, Camera camera, BreadEffectPass pass); }

    [ExecuteAlways, DisallowMultipleComponent]
    public sealed class BreadCaptureEffect : MonoBehaviour
    {
        public BreadEffectResource[] resources = Array.Empty<BreadEffectResource>();
        public BreadEffectPass[] passes = Array.Empty<BreadEffectPass>();
        public int warmupFrames;
        public string output;
        public Camera targetCamera;
        public bool runInCamera = true;
        public BreadCaptureBindings parameters;
        [NonSerialized] public string lastError;
        [NonSerialized] public bool uploadsVerified;
        private readonly Dictionary<string, RenderTexture> textures = new Dictionary<string, RenderTexture>();

        public RenderTexture Resource(string name)
        { if (!textures.TryGetValue(name, out var value)) throw new ArgumentException("Effect resource is unavailable: " + name); return value; }

        private void OnEnable()
        { Camera.onPostRender += RenderBuiltin; UnityEngine.Rendering.RenderPipelineManager.endCameraRendering += RenderSrp; }
        private void OnDisable()
        { Camera.onPostRender -= RenderBuiltin; UnityEngine.Rendering.RenderPipelineManager.endCameraRendering -= RenderSrp; Release(); }
        private void OnDestroy() { Release(); }
        private void RenderBuiltin(Camera camera)
        { if (UnityEngine.Rendering.GraphicsSettings.currentRenderPipeline == null) RenderCamera(camera); }
        private void RenderSrp(UnityEngine.Rendering.ScriptableRenderContext context, Camera camera) { RenderCamera(camera); }
        private void RenderCamera(Camera camera)
        {
            if (!runInCamera || camera == null || camera != targetCamera || !isActiveAndEnabled) return;
            var previous = RenderTexture.active; bool previousSrgb = GL.sRGBWrite;
            try { var image = Execute(camera); GL.sRGBWrite = QualitySettings.activeColorSpace == ColorSpace.Linear;
                Graphics.Blit(image, camera.targetTexture); }
            catch (Exception error) { lastError = error.Message; }
            finally { RenderTexture.active = previous; GL.sRGBWrite = previousSrgb; }
        }
        public void Release()
        { foreach (var texture in textures.Values) { texture.Release(); DestroyImmediate(texture); } textures.Clear(); }

        public void PrepareUploads(bool resetInputs = false)
        {
            uploadsVerified = false;
            try
            {
                parameters = parameters != null ? parameters : GetComponent<BreadCaptureBindings>();
                if (parameters == null) throw new InvalidOperationException("Effect parameter store is missing.");
                if (resetInputs) parameters.ResetInputs(); else parameters.Apply();
                if (parameters.lastError != null) throw new InvalidOperationException(parameters.lastError);
                foreach (var pass in passes)
                {
                    if (pass.insertionPoint != "AfterRendering")
                        throw new NotSupportedException("The effect runner executes at AfterRendering. Use an actual RendererFeature or CustomPass adapter for another insertion point.");
                    if (pass.kind == "compute")
                    {
                        if (!SystemInfo.supportsComputeShaders) throw new NotSupportedException("Compute is unavailable on this device. Supply the equivalent fullscreen or custom CPU pass.");
                        if (pass.compute == null) throw new InvalidOperationException("Compute pass asset is missing.");
                        int kernel = pass.compute.FindKernel(pass.kernel);
                        foreach (var value in parameters.bindings) Upload(pass.compute, kernel, null, value);
                    }
                    else if (pass.kind == "fullscreen")
                    {
                        if (pass.material == null || pass.material.shader == null || !pass.material.shader.isSupported)
                            throw new InvalidOperationException("Fullscreen pass Shader is unavailable on this device.");
                        foreach (var value in parameters.bindings) Upload(null, 0, pass.material, value);
                    }
                    else if (pass.kind == "custom" && !(pass.custom is IBreadCaptureEffectPass))
                        throw new InvalidOperationException("Custom pass does not implement IBreadCaptureEffectPass.");
                }
                uploadsVerified = true; lastError = null;
            }
            catch (Exception error) { lastError = error.Message; }
        }

        private void Upload(ComputeShader compute, int kernel, Material material, BreadParameterBinding binding)
        {
            var block = parameters.ParameterBlock;
            switch (binding.type)
            {
                case "float": if (compute != null) compute.SetFloat(binding.name, block.GetFloat(binding.name)); else material.SetFloat(binding.name, block.GetFloat(binding.name)); break;
                case "int": if (compute != null) compute.SetInt(binding.name, block.GetInteger(binding.name)); else material.SetInteger(binding.name, block.GetInteger(binding.name)); break;
                case "vector": case "color": if (compute != null) compute.SetVector(binding.name, block.GetVector(binding.name)); else material.SetVector(binding.name, block.GetVector(binding.name)); break;
                case "matrix": if (compute != null) compute.SetMatrix(binding.name, block.GetMatrix(binding.name)); else material.SetMatrix(binding.name, block.GetMatrix(binding.name)); break;
                case "float-array": if (compute != null) compute.SetFloats(binding.name, block.GetFloatArray(binding.name)); else material.SetFloatArray(binding.name, block.GetFloatArray(binding.name)); break;
                case "vector-array": if (compute != null) compute.SetVectorArray(binding.name, block.GetVectorArray(binding.name)); else material.SetVectorArray(binding.name, block.GetVectorArray(binding.name)); break;
                case "matrix-array": if (compute != null) compute.SetMatrixArray(binding.name, block.GetMatrixArray(binding.name)); else material.SetMatrixArray(binding.name, block.GetMatrixArray(binding.name)); break;
                case "buffer": if (compute != null) compute.SetBuffer(kernel, binding.name, parameters.GetBuffer(binding.name)); else material.SetBuffer(binding.name, parameters.GetBuffer(binding.name)); break;
                default: throw new NotSupportedException("Effect parameter storage is unsupported: " + binding.type);
            }
        }

        public RenderTexture Execute(Camera camera)
        {
            Release(); PrepareUploads(true);
            if (lastError != null) throw new InvalidOperationException(lastError);
            if (resources.Length < 1 || resources.Length > 128 || passes.Length < 1 || passes.Length > 128 || warmupFrames < 0 || warmupFrames > 64)
                throw new ArgumentException("Effect execution exceeds its resource/pass/warmup bounds.");
            var previous = RenderTexture.active;
            bool previousSrgb = GL.sRGBWrite;
            try
            {
                foreach (var descriptor in resources)
                {
                    var format = (GraphicsFormat)Enum.Parse(typeof(GraphicsFormat), descriptor.format);
                    if (!SystemInfo.IsFormatSupported(format, FormatUsage.Render) || descriptor.randomWrite && !SystemInfo.IsFormatSupported(format, FormatUsage.LoadStore))
                        throw new NotSupportedException("Resource format needs a device-compatible adaptation: " + descriptor.format);
                    var texture = new RenderTexture(new RenderTextureDescriptor(descriptor.width, descriptor.height, format, 0) {
                        enableRandomWrite = descriptor.randomWrite, msaaSamples = 1, volumeDepth = 1, dimension = UnityEngine.Rendering.TextureDimension.Tex2D });
                    texture.name = "Bread Effect " + descriptor.name;
                    if (!texture.Create()) throw new InvalidOperationException("Could not create effect resource: " + descriptor.name);
                    textures.Add(descriptor.name, texture);
                    GL.sRGBWrite = GraphicsFormatUtility.IsSRGBFormat(format);
                    if (descriptor.initializationKind == "texture")
                    { if (descriptor.sourceTexture == null) throw new InvalidOperationException("History/source texture is unavailable."); Graphics.Blit(descriptor.sourceTexture, texture); }
                    else if (descriptor.initializationKind == "clear") { RenderTexture.active = texture; GL.Clear(false, true, descriptor.initialValue); }
                    else throw new ArgumentException("Effect initialization must be explicit.");
                }
                for (int frame = 0; frame <= warmupFrames; frame++)
                {
                    foreach (var pass in passes)
                    {
                        var destination = Resource(pass.output);
                        GL.sRGBWrite = GraphicsFormatUtility.IsSRGBFormat(destination.graphicsFormat);
                        if (pass.kind == "compute")
                        {
                            int kernel = pass.compute.FindKernel(pass.kernel);
                            foreach (var binding in pass.textures) pass.compute.SetTexture(kernel, binding.property, Resource(binding.resource));
                            pass.compute.GetKernelThreadGroupSizes(kernel, out uint x, out uint y, out uint z);
                            int gx = pass.groups.Length == 3 ? pass.groups[0] : (destination.width + (int)x - 1) / (int)x;
                            int gy = pass.groups.Length == 3 ? pass.groups[1] : (destination.height + (int)y - 1) / (int)y;
                            int gz = pass.groups.Length == 3 ? pass.groups[2] : 1;
                            pass.compute.Dispatch(kernel, gx, gy, gz);
                        }
                        else if (pass.kind == "fullscreen")
                        {
                            foreach (var binding in pass.textures) pass.material.SetTexture(binding.property, Resource(binding.resource));
                            if (pass.source == pass.output) throw new ArgumentException("Fullscreen output cannot alias its sampled source.");
                            Graphics.Blit(String.IsNullOrEmpty(pass.source) ? null : Resource(pass.source), destination, pass.material, pass.materialPass);
                        }
                        else if (pass.kind == "copy")
                        { if (pass.source == pass.output) throw new ArgumentException("Copy output cannot alias source."); Graphics.CopyTexture(Resource(pass.source), destination); }
                        else if (pass.kind == "custom") ((IBreadCaptureEffectPass)pass.custom).Execute(this, camera, pass);
                        else throw new NotSupportedException("Unknown effect pass kind.");
                        if (!String.IsNullOrEmpty(pass.globalTextureName)) Shader.SetGlobalTexture(pass.globalTextureName, destination);
                        // Warmup belongs to temporal reconstruction; final-frame
                        // evidence has one ordered execution per planned pass.
                        if (frame == warmupFrames) BreadCapturePassEvidence.Record(pass.id, camera, pass.implementationAsset, pass.reads, pass.writes,
                            destination, pass.kind == "fullscreen" ? "draw" : pass.kind == "copy" ? "custom" : pass.kind,
                            pass.entryPoint, "AfterRendering", pass.outputVersion, pass.globalTextureName);
                    }
                    if (frame < warmupFrames) foreach (var descriptor in resources)
                        if (!String.IsNullOrEmpty(descriptor.historyFrom)) Graphics.CopyTexture(Resource(descriptor.historyFrom), Resource(descriptor.name));
                }
                return Resource(output);
            }
            finally { RenderTexture.active = previous; GL.sRGBWrite = previousSrgb; }
        }
    }
}
