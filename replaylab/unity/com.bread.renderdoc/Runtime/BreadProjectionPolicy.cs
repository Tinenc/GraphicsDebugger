using System;
using UnityEngine;

namespace Bread
{
    public static class BreadProjectionPolicy
    {
        // Keep vertical framing and lens shift; adapt horizontal focal scale to
        // the current view. A capture matrix is not a window-size invariant.
        public static Matrix4x4 MatchViewportAspect(Matrix4x4 captured, int width, int height)
        {
            if (width < 1 || height < 1 || !Finite(captured.m00) || !Finite(captured.m11) ||
                Mathf.Abs(captured.m00) < 1e-8f || Mathf.Abs(captured.m11) < 1e-8f)
                throw new ArgumentException("Supply a finite projection and actual viewport dimensions.");
            float factor = Mathf.Abs(captured.m11 / captured.m00) / ((float)width / height);
            captured.m00 *= factor;
            captured.m01 *= factor;
            return captured;
        }
        static bool Finite(float value) => !float.IsNaN(value) && !float.IsInfinity(value);
    }
}
