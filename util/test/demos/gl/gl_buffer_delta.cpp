// The MIT License (MIT). Copyright (c) 2026 RenderDoc contributors.
// See the repository LICENSE.md for the full licence.
#include "gl_test.h"

struct GLBufferDeltaFixture : OpenGLGraphicsTest
{
  int Run(bool sharedRanges, bool requestES)
  {
    if(requestES)
    {
      gles = true;
      glMajor = 3;
      glMinor = 2;
      GLDefaultVertex.replace(0, GLDefaultVertex.find('\n'), "#version 320 es");
      GLDefaultPixel.replace(0, GLDefaultPixel.find('\n'), "#version 320 es");
    }
    if(!Init())
      return 3;
    const std::string prefix =
        gles ? "#version 320 es\nprecision highp float;\n" : "#version 410 core\n";
    GLuint program = MakeProgram(
        prefix + "layout(location=0) in vec3 pos; void main(){gl_Position=vec4(pos,1.0);}",
        prefix +
            "layout(std140) uniform Delta {vec4 tint;};"
            "layout(location=0) out vec4 color; void main(){color=tint;}");
    glUniformBlockBinding(program, glGetUniformBlockIndex(program, "Delta"), 0);
    GLint alignment = 0;
    glGetIntegerv(GL_UNIFORM_BUFFER_OFFSET_ALIGNMENT, &alignment);
    if(alignment <= 0)
      return 4;
    const size_t offset =
        ((size_t(256) + size_t(alignment) - 1) / size_t(alignment)) * size_t(alignment);
    std::vector<float> earlyData((offset + 256) / sizeof(float), 0.0f);
    std::vector<float> lateData = earlyData;
    for(size_t i = 0; i + 3 < earlyData.size(); i += 4)
    {
      earlyData[i] = 0.125f;
      earlyData[i + 1] = 0.25f;
      earlyData[i + 2] = 0.5f;
      earlyData[i + 3] = 1.0f;
      lateData[i] = 0.625f;
      lateData[i + 1] = 0.5f;
      lateData[i + 2] = 0.25f;
      lateData[i + 3] = 1.0f;
    }
    if(sharedRanges)
      memcpy(earlyData.data() + offset / sizeof(float), lateData.data(), 256);
    GLuint early = MakeBuffer(), late = MakeBuffer();
    glBindBuffer(GL_UNIFORM_BUFFER, early);
    glBufferData(GL_UNIFORM_BUFFER, GLsizeiptr(earlyData.size() * sizeof(float)), earlyData.data(),
                 GL_DYNAMIC_DRAW);
    glBindBuffer(GL_UNIFORM_BUFFER, late);
    glBufferData(GL_UNIFORM_BUFFER, GLsizeiptr(lateData.size() * sizeof(float)), lateData.data(),
                 GL_DYNAMIC_DRAW);
    if(GLAD_GL_KHR_debug)
    {
      glObjectLabel(GL_BUFFER, early, -1, "Delta_Early_Or_Shared");
      glObjectLabel(GL_BUFFER, late, -1, "Delta_Late");
    }
    while(Running())
    {
      // Captured CPU updates overwrite the overlays on every full replay. The replay backend
      // must reapply shared edits in native command order, while static mesh/texture assets stay
      // eligible for content sharing because only these two resolved destinations are written.
      glBindBuffer(GL_UNIFORM_BUFFER, early);
      glBufferSubData(GL_UNIFORM_BUFFER, 0, GLsizeiptr(earlyData.size() * sizeof(float)),
                      earlyData.data());
      glBindBuffer(GL_UNIFORM_BUFFER, late);
      glBufferSubData(GL_UNIFORM_BUFFER, 0, GLsizeiptr(lateData.size() * sizeof(float)),
                      lateData.data());
      const float clear[] = {0.03125f, 0.0625f, 0.09375f, 1.0f};
      glClearBufferfv(GL_COLOR, 0, clear);
      glBindVertexArray(DefaultTriVAO);
      glUseProgram(program);
      for(uint32_t region = 0; region < 4; region++)
      {
        const GLuint buffer = sharedRanges || region < 2 ? early : late;
        const GLintptr base = sharedRanges && region >= 2 ? GLintptr(offset) : 0;
        glBindBufferRange(GL_UNIFORM_BUFFER, 0, buffer, base, 16);
        const GLint width = GLint(screenWidth / 4);
        glViewport(GLint(region) * width, 0, width, GLsizei(screenHeight));
        setMarker("Delta parameter region " + std::to_string(region));
        glDrawArrays(GL_TRIANGLES, 0, 3);
      }
      Present();
    }
    return 0;
  }
};

namespace DesktopBuffers
{
RD_TEST(GL_Buffer_Delta, GLBufferDeltaFixture)
{
  static constexpr const char *Description = "Two GL UBOs with distinct native first consumers";
  int main()
  {
    return Run(false, false);
  }
};
REGISTER_TEST();
}

namespace DesktopRanges
{
RD_TEST(GL_Buffer_Delta_Ranges, GLBufferDeltaFixture)
{
  static constexpr const char *Description = "Two native GL UBO windows in one shared buffer";
  int main()
  {
    return Run(true, false);
  }
};
REGISTER_TEST();
}

namespace ESBuffers
{
RD_TEST(GLES_Buffer_Delta, GLBufferDeltaFixture)
{
  static constexpr const char *Description = "Two ES 3.2 UBOs with distinct native first consumers";
  int main()
  {
    return Run(false, true);
  }
};
REGISTER_TEST();
}

namespace ESRanges
{
RD_TEST(GLES_Buffer_Delta_Ranges, GLBufferDeltaFixture)
{
  static constexpr const char *Description = "Two native ES 3.2 UBO windows in one shared buffer";
  int main()
  {
    return Run(true, true);
  }
};
REGISTER_TEST();
}
