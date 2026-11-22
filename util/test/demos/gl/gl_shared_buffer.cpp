// The MIT License (MIT). Copyright (c) 2026 RenderDoc contributors.
// See the repository LICENSE.md for the full licence.
#include "gl_test.h"

RD_TEST(GL_Shared_Buffer, OpenGLGraphicsTest)
{
  static constexpr const char *Description =
      "Four draws share one UBO range, with BufferSubData and CopyBufferSubData writes between "
      "consumers. Set RENDERDOC_SHARED_BUFFER_GLES=1 to request an ES 3.2 context.";

  int main()
  {
    const char *es = getenv("RENDERDOC_SHARED_BUFFER_GLES");
    if(es && es[0] == '1')
    {
      gles = true;
      glMajor = 3;
      glMinor = 2;
      GLDefaultVertex.replace(0, GLDefaultVertex.find('\n'), "#version 320 es");
      GLDefaultPixel.replace(0, GLDefaultPixel.find('\n'), "#version 320 es");
    }
    if(!Init())
      return 3;
    const std::string prefix = gles ? "#version 320 es\nprecision highp float;\n" : "#version 410 core\n";
    GLuint program = MakeProgram(
        prefix + "layout(location=0) in vec3 pos; void main(){gl_Position=vec4(pos,1.0);}",
        prefix + "layout(std140) uniform Shared {vec4 tint;};"
                 "layout(location=0) out vec4 color; void main(){color=tint;}");
    glUniformBlockBinding(program, glGetUniformBlockIndex(program, "Shared"), 0);
    GLint alignment = 0;
    glGetIntegerv(GL_UNIFORM_BUFFER_OFFSET_ALIGNMENT, &alignment);
    const size_t offset = size_t(alignment > 256 ? alignment : 256);
    std::vector<float> data((offset + 256) / sizeof(float), 0.0f);
    data[offset / 4 + 0] = 0.2f; data[offset / 4 + 1] = 0.45f;
    data[offset / 4 + 2] = 0.7f; data[offset / 4 + 3] = 1.0f;
    GLuint cb = MakeBuffer(), source = MakeBuffer();
    glBindBuffer(GL_UNIFORM_BUFFER, cb);
    glBufferData(GL_UNIFORM_BUFFER, GLsizeiptr(data.size() * 4), data.data(), GL_DYNAMIC_DRAW);
    glBindBuffer(GL_COPY_READ_BUFFER, source);
    glBufferData(GL_COPY_READ_BUFFER, GLsizeiptr(data.size() * 4), data.data(), GL_STATIC_DRAW);
    if(GLAD_GL_KHR_debug)
      glObjectLabel(GL_BUFFER, cb, -1, "SharedParameterBuffer");
    while(Running())
    {
      const float clear[] = {0.03f, 0.04f, 0.05f, 1.0f};
      glClearBufferfv(GL_COLOR, 0, clear);
      glBindVertexArray(DefaultTriVAO);
      glUseProgram(program);
      glBindBufferRange(GL_UNIFORM_BUFFER, 0, cb, GLintptr(offset), 16);
      for(uint32_t region = 0; region < 4; region++)
      {
        glBindBuffer(GL_UNIFORM_BUFFER, cb);
        if((region % 2) == 0)
          glBufferSubData(GL_UNIFORM_BUFFER, 0, GLsizeiptr(data.size() * 4), data.data());
        else
        {
          glBindBuffer(GL_COPY_READ_BUFFER, source);
          glBindBuffer(GL_COPY_WRITE_BUFFER, cb);
          glCopyBufferSubData(GL_COPY_READ_BUFFER, GL_COPY_WRITE_BUFFER, 0, 0, GLsizeiptr(data.size() * 4));
        }
        const GLint width = GLint(screenWidth / 4);
        glViewport(GLint(region) * width, 0, width, GLsizei(screenHeight));
        setMarker("Shared parameter region " + std::to_string(region));
        glDrawArrays(GL_TRIANGLES, 0, 3);
      }
      Present();
    }
    return 0;
  }
};

REGISTER_TEST();
