/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2015-2026 Baldur Karlsson
 * Copyright (c) 2014 Crytek
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 ******************************************************************************/

#include "camera.h"
#include <math.h>
#include <string.h>
#include "common/common.h"
#include "matrix.h"

Matrix4f Camera::MeshProjection(ICamera *camera, float aspect)
{
  const Camera *cam = static_cast<Camera *>(camera);
  if(!cam)
    return Matrix4f::Perspective(90.0f, 0.1f, 100000.0f, aspect);
  std::lock_guard<std::mutex> lock(cam->m_Mutex);
  return Matrix4f::Perspective(cam ? cam->m_FieldOfView : 90.0f, cam ? cam->m_Near : 0.1f,
                               cam ? cam->m_Far : 100000.0f, aspect);
}

void Camera::ResetArcball()
{
  std::lock_guard<std::mutex> lock(m_Mutex);
  dirty = true;

  arcrot = Quatf::AxisAngle(Vec3f(1, 0, 0), 0.0f);
}

// https://en.wikibooks.org/wiki/OpenGL_Programming/Modern_OpenGL_Tutorial_Arcball
void Camera::RotateArcball(float ax, float ay, float bx, float by)
{
  std::lock_guard<std::mutex> lock(m_Mutex);
  Vec3f a, b;

  Vec2f from(ax, ay);
  Vec2f to(bx, by);

  float az = from.x * from.x + from.y * from.y;
  float bz = to.x * to.x + to.y * to.y;
  if(!std::isfinite(az) || !std::isfinite(bz))
    return;

  // keep the controls stable by rejecting very small movements.
  if(fabsf(az - bz) < 1e-5f)
    return;

  if(az < 1.0f)
  {
    a = Vec3f(from.x, from.y, sqrtf(1.0f - az));
  }
  else
  {
    a = Vec3f(from.x, from.y, 0.0f);
    a.Normalise();
  }

  if(bz < 1.0f)
  {
    b = Vec3f(to.x, to.y, sqrtf(1.0f - bz));
  }
  else
  {
    b = Vec3f(to.x, to.y, 0.0f);
    b.Normalise();
  }

  const float dot = RDCMAX(-1.0f, RDCMIN(1.0f, a.Dot(b)));
  float angle = acosf(dot);

  Vec3f axis = a.Cross(b);
  if(axis.Length() < 1e-10f)
  {
    if(dot > 0)
      return;
    axis = a.Cross(fabsf(a.x) < .9f ? Vec3f(1, 0, 0) : Vec3f(0, 1, 0));
  }
  axis.Normalise();

  dirty = true;

  Quatf delta = Quatf::AxisAngle(axis, angle);
  arcrot = arcrot * delta;
}

void Camera::SetTurntableRotation(float yaw, float pitch)
{
  if(!std::isfinite(yaw) || !std::isfinite(pitch))
    return;
  std::lock_guard<std::mutex> lock(m_Mutex);
  dirty = true;
  arcrot = Quatf::AxisAngle(Vec3f(0, 1, 0), yaw) * Quatf::AxisAngle(Vec3f(1, 0, 0), pitch);
}

void Camera::RotateMeshPreviewOrbit(float yaw, float pitch, bool inverted)
{
  if(!std::isfinite(yaw) || !std::isfinite(pitch) || (yaw == 0.0f && pitch == 0.0f))
    return;

  std::lock_guard<std::mutex> lock(m_Mutex);
  Quatf &orientation = type == CameraType::FPSLook ? fpsrot : arcrot;
  const Matrix4f axes = orientation.GetMatrix().Transpose();
  const Vec3f worldUp(0, 1, 0);
  Vec3f pitchAxis = axes.GetRight();

  // When the view is rolled onto its side, screen-right and world-up can
  // coincide. Blend toward the horizon so vertical motion can leave that pose.
  // Like Blender's turntable, keep the side of the horizon chosen for this drag.
  const float weight = RDCMIN(1.0f, pitchAxis.y * pitchAxis.y);
  if(weight > 1e-6f)
  {
    const Vec3f horizon = worldUp.Cross(axes.GetForward());
    const float length = horizon.Length();
    if(length > 1e-6f)
    {
      pitchAxis = pitchAxis * (1.0f - weight) + horizon * ((inverted ? -weight : weight) / length);
      pitchAxis.Normalise();
    }
  }

  orientation = Quatf::AxisAngle(worldUp, inverted ? -yaw : yaw) *
                Quatf::AxisAngle(pitchAxis, pitch) * orientation;
  const float inverseLength =
      1.0f / sqrtf(orientation.w * orientation.w + orientation.v.Dot(orientation.v));
  orientation.w *= inverseLength;
  orientation.v = orientation.v * inverseLength;
  dirty = true;
}

void Camera::Update()
{
  if(!dirty)
    return;

  if(type == CameraType::FPSLook)
  {
    Matrix4f p = Matrix4f::Translation(-pos);
    Matrix4f r = fpsrot.GetMatrix();

    mat = r.Mul(p);
    basis = mat.Transpose();
  }
  else
  {
    Matrix4f p = Matrix4f::Translation(-pos);
    Matrix4f r = arcrot.GetMatrix();
    Matrix4f d = Matrix4f::Translation(Vec3f(0.0f, 0.0f, dist));

    mat = d.Mul(r.Mul(p));
    basis = mat.Transpose();
  }
  dirty = false;
}

const Matrix4f Camera::GetMatrix()
{
  std::lock_guard<std::mutex> lock(m_Mutex);
  Update();
  return mat;
}

FloatVector Camera::GetPosition()
{
  std::lock_guard<std::mutex> lock(m_Mutex);
  return FloatVector(pos.x, pos.y, pos.z, 1.0f);
}

FloatVector Camera::GetForward()
{
  std::lock_guard<std::mutex> lock(m_Mutex);
  Update();
  Vec3f fwd = basis.GetForward();
  return FloatVector(fwd.x, fwd.y, fwd.z, 1.0f);
}

FloatVector Camera::GetRight()
{
  std::lock_guard<std::mutex> lock(m_Mutex);
  Update();
  Vec3f right = basis.GetRight();
  return FloatVector(right.x, right.y, right.z, 1.0f);
}

FloatVector Camera::GetUp()
{
  std::lock_guard<std::mutex> lock(m_Mutex);
  Update();
  Vec3f up = basis.GetUp();
  return FloatVector(up.x, up.y, up.z, 1.0f);
}

#if ENABLED(ENABLE_UNIT_TESTS)
#include "3rdparty/catch/catch.hpp"

TEST_CASE("Mesh turntable orbit keeps its horizon", "[mesh][camera]")
{
  Camera camera(CameraType::Arcball);
  camera.SetPosition(1.0f, 2.0f, 3.0f);
  camera.SetArcballDistance(4.0f);
  for(float yaw : {-3.0f, -1.0f, 0.0f, 1.0f, 3.0f})
    for(float pitch : {-1.56f, -0.7f, 0.0f, 0.7f, 1.56f})
    {
      camera.SetTurntableRotation(yaw, pitch);
      const FloatVector right = camera.GetRight();
      const FloatVector forward = camera.GetForward();
      const FloatVector up = camera.GetUp();
      CHECK(right.y == Approx(0.0f).margin(1e-6));
      CHECK(up.y > 0.0f);
      CHECK(right.x * forward.x + right.y * forward.y + right.z * forward.z ==
            Approx(0.0f).margin(1e-6));
    }
}

TEST_CASE("Mesh turntable crosses both poles without snapping", "[mesh][camera]")
{
  Camera camera(CameraType::Arcball);
  camera.SetArcballDistance(2.0f);
  Vec3f previous;
  bool first = true;
  for(int step = -330; step <= 330; ++step)
  {
    const float pitch = float(step) * 0.01f;
    camera.SetTurntableRotation(0.4f, pitch);
    const FloatVector v = camera.GetForward();
    const Vec3f forward(v.x, v.y, v.z);
    CHECK(forward.Length() == Approx(1.0f).margin(1e-5f));
    if(!first)
      CHECK((forward - previous).Length() < 0.011f);
    previous = forward;
    first = false;
  }
}
#endif
