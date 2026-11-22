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

#pragma once

#include <cmath>
#include <mutex>
#include "api/replay/renderdoc_replay.h"
#include "quat.h"
#include "vec.h"

class Matrix4f;

class Camera : public ICamera
{
public:
  Camera(CameraType t) : type(t), dirty(true), pos(), dist(0.0f)
  {
    Camera::ResetArcball();
    SetFPSRotation(0.0f, 0.0f, 0.0f);
  }
  virtual ~Camera() {}
  void Shutdown() { delete this; }
  void SetPosition(float x, float y, float z)
  {
    if(!std::isfinite(x) || !std::isfinite(y) || !std::isfinite(z))
      return;
    std::lock_guard<std::mutex> lock(m_Mutex);
    dirty = true;
    pos = Vec3f(x, y, z);
  }

  void SetNearFar(float n, float f)
  {
    if(!std::isfinite(n) || !std::isfinite(f) || n <= 0 || f <= n)
      return;
    std::lock_guard<std::mutex> lock(m_Mutex);
    m_Near = n;
    m_Far = f;
  }

  void SetFieldOfView(float degrees)
  {
    std::lock_guard<std::mutex> lock(m_Mutex);
    if(std::isfinite(degrees) && degrees > 0 && degrees < 180)
      m_FieldOfView = degrees;
  }
  static Matrix4f MeshProjection(ICamera *camera, float aspect);

  float GetNear() const
  {
    std::lock_guard<std::mutex> lock(m_Mutex);
    return m_Near;
  }
  float GetFar()
  {
    std::lock_guard<std::mutex> lock(m_Mutex);
    return m_Far;
  }

  // Arcball functions
  void ResetArcball();
  void SetArcballDistance(float d)
  {
    if(!std::isfinite(d))
      return;
    std::lock_guard<std::mutex> lock(m_Mutex);
    dirty = true;
    dist = d;
  }
  void RotateArcball(float ax, float ay, float bx, float by);
  void SetTurntableRotation(float yaw, float pitch);
  void RotateMeshPreviewOrbit(float yaw, float pitch, bool inverted);

  // FPS look functions
  void SetFPSRotation(float x, float y, float z)
  {
    if(!std::isfinite(x) || !std::isfinite(y) || !std::isfinite(z))
      return;
    std::lock_guard<std::mutex> lock(m_Mutex);
    dirty = true;
    fpsrot = Quatf::AxisAngle(Vec3f(0, 0, 1), -z) * Quatf::AxisAngle(Vec3f(0, 1, 0), -y) *
             Quatf::AxisAngle(Vec3f(1, 0, 0), -x);
  }

  FloatVector GetPosition();
  FloatVector GetForward();
  FloatVector GetRight();
  FloatVector GetUp();
  const Matrix4f GetMatrix();

private:
  mutable std::mutex m_Mutex;
  void Update();

  CameraType type;

  bool dirty;
  Matrix4f mat, basis;

  float m_FieldOfView = 90.0f;
  float m_Near = 0.1f, m_Far = 100000.0f;

  Vec3f pos;

  // Arcball
  Quatf arcrot;
  float dist;

  // FPS look
  Quatf fpsrot;
};
