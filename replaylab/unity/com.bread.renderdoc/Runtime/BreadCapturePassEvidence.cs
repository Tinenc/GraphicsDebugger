using System;
using System.Collections.Generic;
using UnityEngine;

namespace Bread
{
    public sealed class BreadPassObservation
    {
        public string passId;
        public string implementationAsset;
        public string[] reads;
        public string[] writes;
        public int width;
        public int height;
        public string format;
        public int samples;
        public int outputInstanceId;
        public string outputVersion;
        public string kind;
        public string entryPoint;
        public string insertionPoint;
        public string renderId;
        public string cameraName;
        public string globalTextureName;
        public string observedAtUtc;
    }

    // The derived RendererFeature/CustomPass/command-buffer implementation calls
    // Record inside its actual execution callback. Registration alone never
    // counts as execution. This is scheduling evidence, not Shader equivalence.
    public static class BreadCapturePassEvidence
    {
        private static Camera camera;
        public static string CurrentRenderId { get; private set; }
        private static readonly List<BreadPassObservation> observations = new List<BreadPassObservation>();
        private static readonly Dictionary<string, RenderTexture> outputs = new Dictionary<string, RenderTexture>();
        public static void Begin(Camera target) { camera = target; CurrentRenderId = Guid.NewGuid().ToString("N"); observations.Clear(); outputs.Clear(); }
        public static void Record(string passId, Camera target, string implementationAsset,
                                  string[] reads, string[] writes, RenderTexture output,
                                  string kind = "", string entryPoint = "", string insertionPoint = "", string outputVersion = "", string globalTextureName = "")
        {
            if (camera == null || target != camera) return;
            if (!String.IsNullOrEmpty(globalTextureName) && output != null) outputs[globalTextureName] = output;
            observations.Add(new BreadPassObservation { passId = passId, implementationAsset = implementationAsset,
                reads = reads ?? Array.Empty<string>(), writes = writes ?? Array.Empty<string>(),
                width = output == null ? 0 : output.width, height = output == null ? 0 : output.height,
                format = output == null ? "unobserved" : output.graphicsFormat.ToString(), samples = output == null ? 0 : output.antiAliasing,
                outputInstanceId = output == null ? 0 : output.GetInstanceID(),
                outputVersion = String.IsNullOrEmpty(outputVersion) && writes != null && writes.Length == 1 ? writes[0] : outputVersion,
                kind = kind, entryPoint = entryPoint, insertionPoint = insertionPoint,
                renderId = CurrentRenderId, cameraName = camera.name,
                globalTextureName = globalTextureName,
                observedAtUtc = DateTime.UtcNow.ToString("O") });
        }
        public static BreadPassObservation[] End()
        { camera = null; return observations.ToArray(); }
        public static RenderTexture RecordedOutput(string name)
        { return outputs.TryGetValue(name, out var output) ? output : null; }
    }
}
