using System;
using System.Collections.Generic;
using System.Linq;
using UnityEngine;
using UnityEngine.Rendering;

namespace Bread
{
    // Opt-in instrumentation and resource helpers. Installing the SDK alone
    // does not change a scene, camera, pipeline asset or renderer selection.
    public static class BreadCameraEvidence
    {
        [Serializable] public sealed class Resource
        {
            public int cameraId, textureId, width, height, generation, unityFrame, viewIndex;
            public int cameraPixelWidth, cameraPixelHeight, viewportWidth, viewportHeight;
            public long renderSequence;
            public string cameraType, role, kind, producer, historyResetReason;
            public bool viewIndependent, historyValid, historyStateObserved;
        }
        [Serializable] public sealed class Feature
        {
            public int cameraId; public long renderSequence;
            public string name, settingsKey; public bool enabled;
        }
        static readonly Dictionary<int, long> sequences = new Dictionary<int, long>();
        static readonly Dictionary<string, Resource> resources = new Dictionary<string, Resource>();
        static readonly Dictionary<string, Feature> features = new Dictionary<string, Feature>();
        static readonly HashSet<string> readyGlobals = new HashSet<string>();
        static bool hooked;
        public static Camera CurrentCamera { get; private set; }
        public static void Observe()
        {
            if (hooked) return;
            hooked = true;
            RenderPipelineManager.beginCameraRendering += Begin;
            RenderPipelineManager.endCameraRendering += End;
        }
        static void Begin(ScriptableRenderContext context, Camera camera)
        {
            CurrentCamera = camera;
            int id = camera.GetInstanceID();
            if (!sequences.ContainsKey(id) && sequences.Count >= 256) { sequences.Clear(); resources.Clear(); features.Clear(); }
            sequences.TryGetValue(id, out var sequence); sequences[id] = sequence + 1;
            // Only globals explicitly published through this helper are owned.
            // A camera that skips an effect must not inherit another view's mask.
            foreach (string ready in readyGlobals) Shader.SetGlobalFloat(ready, 0);
            Shader.SetGlobalInt("_BreadRenderingCameraId", id);
        }
        static void End(ScriptableRenderContext context, Camera camera)
        {
            foreach (string ready in readyGlobals) Shader.SetGlobalFloat(ready, 0);
            if (CurrentCamera == camera) CurrentCamera = null;
        }
        public static long Sequence(Camera camera)
        { Observe(); return camera != null && sequences.TryGetValue(camera.GetInstanceID(), out var value) ? value : 0; }
        public static void RecordFeature(Camera camera, string name, bool enabled, string settingsKey = "")
        {
            if (!camera) throw new ArgumentNullException(nameof(camera));
            Observe(); int id = camera.GetInstanceID();
            if (features.Count >= 1024) features.Clear();
            features[id + ":" + name] = new Feature { cameraId = id, name = name, enabled = enabled,
                settingsKey = settingsKey, renderSequence = Sequence(camera) };
        }
        public static void RecordResource(Camera camera, string role, Texture texture,
            bool history = false, bool viewIndependent = false, int generation = 0, string producer = "",
            int viewIndex = 0, RectInt? viewport = null, bool? historyValid = null, string historyResetReason = "")
        {
            if (!camera || !texture) throw new ArgumentException("An actual camera and resource are required.");
            if (history && viewIndependent) throw new ArgumentException("History cannot be shared between views.");
            Observe(); int id = camera.GetInstanceID();
            if (resources.Count >= 2048) resources.Clear();
            resources[id + ":" + viewIndex + ":" + role] = new Resource { cameraId = id, cameraType = camera.cameraType.ToString(),
                role = role, kind = history ? "history" : viewIndependent ? "world" : "screen", viewIndependent = viewIndependent,
                textureId = texture.GetInstanceID(), width = texture.width, height = texture.height, generation = generation,
                renderSequence = Sequence(camera), unityFrame = Time.frameCount, producer = producer, viewIndex = viewIndex,
                cameraPixelWidth = camera.pixelWidth, cameraPixelHeight = camera.pixelHeight,
                viewportWidth = viewport?.width ?? texture.width, viewportHeight = viewport?.height ?? texture.height,
                historyValid = historyValid ?? false, historyStateObserved = historyValid.HasValue, historyResetReason = historyResetReason };
        }
        public static void PublishScreen(CommandBuffer commands, Camera camera, string textureGlobal,
            string readyGlobal, Texture texture, string role, int generation = 0, int viewIndex = 0, RectInt? viewport = null)
        {
            if (commands == null || String.IsNullOrEmpty(readyGlobal) || String.IsNullOrEmpty(textureGlobal))
                throw new ArgumentException("Supply the actual command buffer and complete global binding.");
            RecordResource(camera, role, texture, generation: generation, viewIndex: viewIndex, viewport: viewport);
            readyGlobals.Add(readyGlobal);
            commands.SetGlobalTexture(textureGlobal, texture);
            commands.SetGlobalInt(textureGlobal + "_BreadOwner", camera.GetInstanceID());
            commands.SetGlobalFloat(readyGlobal, 1);
        }
        public static Resource[] Resources() => resources.Values.ToArray();
        public static Feature[] Features() => features.Values.ToArray();
    }

    // Own a separate screen/history allocation for each camera and XR view.
    // Caller clears/reseeds textures when HistoryValid is false, then marks it
    // valid after submitting that view's actual producer commands.
    public sealed class BreadCameraResources<T> : IDisposable where T : class
    {
        public sealed class State
        {
            public Camera Camera; public T Data;
            public int Width, Height, Generation; public bool HistoryValid;
            public long RenderSequence;
            public long LastCommittedSequence { get; private set; }
            public string HistoryResetReason { get; private set; } = "new-allocation";
            public RectInt Viewport { get; internal set; }
            public BreadViewLayout Layout => new BreadViewLayout(Width, Height, Viewport);
            public void ResetHistory(string reason) { HistoryValid = false; HistoryResetReason = reason; }
            // Call only after submitting this camera's real producer. Multiple
            // passes in the same render cannot advance ping-pong/jitter twice.
            public bool CommitHistory()
            {
                if (RenderSequence <= LastCommittedSequence) return false;
                LastCommittedSequence = RenderSequence; HistoryValid = true; return true;
            }
            internal string descriptor; internal Matrix4x4 projection;
        }
        readonly Dictionary<string, State> states = new Dictionary<string, State>();
        readonly Action<T> release;
        public BreadCameraResources(Action<T> release) { this.release = release ?? throw new ArgumentNullException(nameof(release)); BreadCameraEvidence.Observe(); }
        public State Acquire(Camera camera, int width, int height, string descriptorKey, Func<T> create,
            bool cameraCut = false, int viewIndex = 0)
        {
            if (!camera || width < 1 || height < 1 || create == null) throw new ArgumentException("Supply an actual camera, extent and allocator.");
            foreach (var stale in states.Where(row => !row.Value.Camera).Select(row => row.Key).ToArray())
            { release(states[stale].Data); states.Remove(stale); }
            string key = camera.GetInstanceID() + ":" + viewIndex;
            states.TryGetValue(key, out var state);
            bool resized = state == null || state.Camera != camera || state.Width != width || state.Height != height || state.descriptor != descriptorKey;
            if (resized)
            {
                int generation = (state?.Generation ?? 0) + 1;
                state?.ResetHistory("extent-or-descriptor-changed");
                // Keep the previous allocation alive if allocation fails.
                var replacement = create();
                if (replacement == null || (state != null && ReferenceEquals(replacement, state.Data)))
                    throw new InvalidOperationException("Allocate a distinct per-view resource.");
                if (state != null) release(state.Data);
                state = new State { Camera = camera, Width = width, Height = height, Data = replacement,
                    Generation = generation, descriptor = descriptorKey, HistoryValid = false };
                states[key] = state;
            }
            if (cameraCut) state.ResetHistory("camera-cut");
            else if (!resized && !SameProjection(state.projection, camera.nonJitteredProjectionMatrix)) state.ResetHistory("projection-changed");
            state.projection = camera.nonJitteredProjectionMatrix;
            state.Viewport = new RectInt(0, 0, width, height);
            state.RenderSequence = BreadCameraEvidence.Sequence(camera);
            return state;
        }
        static bool SameProjection(Matrix4x4 a, Matrix4x4 b)
        { for (int i=0;i<16;i++) if (float.IsNaN(a[i]) || float.IsInfinity(a[i]) || float.IsNaN(b[i]) || float.IsInfinity(b[i]) || Mathf.Abs(a[i]-b[i]) > 0.00001f) return false; return true; }
        public State Acquire(Camera camera, RenderTextureDescriptor descriptor, Func<RenderTextureDescriptor,T> create,
            bool cameraCut = false, int viewIndex = 0, string settingsKey = "", RectInt? activeViewport = null)
        {
            var viewport = activeViewport ?? new RectInt(0,0,descriptor.width,descriptor.height);
            if (viewport.x < 0 || viewport.y < 0 || viewport.width < 1 || viewport.height < 1 ||
                viewport.xMax > descriptor.width || viewport.yMax > descriptor.height)
                throw new ArgumentException("Use the actual active pixel viewport inside this view's allocation.");
            string identity = descriptor.graphicsFormat+":"+descriptor.depthStencilFormat+":"+descriptor.msaaSamples+":"+
                descriptor.dimension+":"+descriptor.volumeDepth+":"+descriptor.vrUsage+":"+descriptor.useDynamicScale+":"+
                descriptor.enableRandomWrite+":"+descriptor.bindMS+":"+descriptor.useMipMap+":"+descriptor.mipCount+":"+
                descriptor.memoryless+":"+descriptor.shadowSamplingMode+":"+viewport+":"+settingsKey;
            var state = Acquire(camera,descriptor.width,descriptor.height,identity,()=>create(descriptor),cameraCut,viewIndex);
            state.Viewport = viewport; return state;
        }
        public void Invalidate(Camera camera)
        { foreach (var state in states.Values) if (state.Camera == camera) state.ResetHistory("explicit-invalidation"); }
        public void Dispose() { foreach (var state in states.Values) release(state.Data); states.Clear(); }
    }
}
