using System;
using UnityEngine;

namespace Bread
{
    // Allocation extent and active view extent are different for scaled RTHandles.
    // Construct for the current camera/eye inside its render, never from Screen.
    public readonly struct BreadViewLayout
    {
        public readonly int AllocationWidth, AllocationHeight;
        public readonly RectInt Viewport;
        public BreadViewLayout(int width, int height, RectInt viewport)
        {
            if (width < 1 || height < 1 || viewport.x < 0 || viewport.y < 0 ||
                viewport.width < 1 || viewport.height < 1 || viewport.xMax > width || viewport.yMax > height)
                throw new ArgumentException("Supply the active pixel viewport inside the current view's allocation.");
            AllocationWidth = width; AllocationHeight = height; Viewport = viewport;
        }
        // Match Unity's _Texture_TexelSize convention for allocation UV sampling.
        public Vector4 TexelSize => new Vector4(1f/AllocationWidth, 1f/AllocationHeight, AllocationWidth, AllocationHeight);
        public Vector4 ViewSize => new Vector4(Viewport.width, Viewport.height, 1f/Viewport.width, 1f/Viewport.height);
        public Vector4 UvScaleBias => new Vector4((float)Viewport.width/AllocationWidth,
            (float)Viewport.height/AllocationHeight, (float)Viewport.x/AllocationWidth, (float)Viewport.y/AllocationHeight);
        public Vector2 MapViewUv(Vector2 uv)
        { var mapping = UvScaleBias; return new Vector2(uv.x*mapping.x+mapping.z, uv.y*mapping.y+mapping.w); }
    }
}
