using UnityEngine;
using UnityEngine.Rendering;

namespace Bread
{
    // An explicitly requested off-axis/jittered projection must survive scene
    // reload. Ordinary cameras continue to use Unity's native projection.
    [ExecuteAlways, RequireComponent(typeof(Camera)), DisallowMultipleComponent]
    public sealed class BreadCaptureCameraState : MonoBehaviour
    {
        public bool useProjectionOverride;
        public bool adaptToViewport = true;
        public float[] projectionRowMajor = new float[16];
        private Camera view;
        private void OnEnable() { view = GetComponent<Camera>(); RenderPipelineManager.beginCameraRendering += BeforeCamera; Apply(); }
        private void OnDisable()
        { RenderPipelineManager.beginCameraRendering -= BeforeCamera;
          if (view != null && view.cameraType != CameraType.SceneView) {
              if (adaptToViewport) view.ResetAspect(); view.ResetProjectionMatrix(); } }
        private void OnPreCull() { Apply(); }
        private void LateUpdate() { Apply(); }
        private void BeforeCamera(ScriptableRenderContext context, Camera camera) { if (camera == view) Apply(); }
        public void Apply()
        {
            if (view == null) view = GetComponent<Camera>();
            // Unity's SceneView owns its projection, lens and window aspect.
            // Never apply a Game/capture reference projection to that camera.
            if (view.cameraType == CameraType.SceneView) return;
            // Also release a stale capture aspect when using Unity's native
            // projection. Free Aspect and render-scale changes are per-view;
            // never pin this to the capture extent or to another editor view.
            if (adaptToViewport) view.ResetAspect();
            if (useProjectionOverride && projectionRowMajor != null && projectionRowMajor.Length == 16)
            {
                var projection = BreadCaptureBindings.Matrix(projectionRowMajor);
                if (adaptToViewport && view.pixelWidth > 0 && view.pixelHeight > 0)
                {
                    projection = BreadProjectionPolicy.MatchViewportAspect(projection, view.pixelWidth, view.pixelHeight);
                }
                view.projectionMatrix = projection;
            }
            else view.ResetProjectionMatrix();
        }
    }
}
