using System;
using UnityEngine;
using UnityEngine.Rendering;

namespace Bread
{
    // Render-only projection overrides. Never write a SceneView Camera's
    // projectionMatrix, worldToCameraMatrix, cullingMatrix or Transform for TAA.
    // Editor handles/gizmos must keep the editor-owned, non-jittered camera.
    public static class BreadRenderProjection
    {
        public static Matrix4x4 WithPixelJitter(Matrix4x4 projection, Vector2 jitter,
            int viewportWidth, int viewportHeight)
        {
            if (viewportWidth < 1 || viewportHeight < 1 ||
                float.IsNaN(jitter.x) || float.IsNaN(jitter.y) ||
                float.IsInfinity(jitter.x) || float.IsInfinity(jitter.y))
                throw new ArgumentException("Use finite pixel jitter and the current physical viewport.");
            // A homogeneous clip-space translation supports perspective,
            // orthographic and off-axis cameras without changing their aspect.
            var translation = Matrix4x4.identity;
            translation.m03 = 2f * jitter.x / viewportWidth;
            translation.m13 = 2f * jitter.y / viewportHeight;
            return translation * projection;
        }

        public static void Apply(CommandBuffer commands, Matrix4x4 view, Matrix4x4 renderProjection)
        {
            if (commands == null) throw new ArgumentNullException(nameof(commands));
            commands.SetViewProjectionMatrices(view, renderProjection);
        }

        public static void Restore(CommandBuffer commands, Matrix4x4 view, Matrix4x4 pipelineProjection)
        {
            if (commands == null) throw new ArgumentNullException(nameof(commands));
            // Use the actual matrices provided by this render pipeline, with
            // matching GPU/Y-flip conventions. Restore every overridden shader
            // inverse/previous matrix in the same producer's finally path too.
            commands.SetViewProjectionMatrices(view, pipelineProjection);
        }
    }
}
