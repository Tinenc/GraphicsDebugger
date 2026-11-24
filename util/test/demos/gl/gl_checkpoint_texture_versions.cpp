// The MIT License (MIT). Copyright (c) 2026 RenderDoc contributors.
// See the repository LICENSE.md for the full licence.
#include "gl_test.h"
#if defined(__linux__) && !defined(ANDROID)
#include "3rdparty/glad/glad_glx.h"
#endif

struct GLCheckpointTextureVersionsFixture : OpenGLGraphicsTest
{
  int Run(bool requestES)
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
    // Desktop test setup uses the desktop GL loader even for a native ES context. ES 3.2 core
    // includes APIs such as TexStorage2D that desktop GL only loads at 4.2 or via an ARB extension.
    // Load the actual ES core entry points without changing the context or texture storage model.
    if(gles)
    {
      bool loaded = true;
#if defined(WIN32)
      loaded = gladLoadGLES2Loader([](const char *name) -> void * {
                 HMODULE module = GetModuleHandleA("opengl32.dll");
                 if(!module)
                   return NULL;
                 typedef PROC(WINAPI * GetProc)(LPCSTR);
                 GetProc getProc = (GetProc)GetProcAddress(module, "wglGetProcAddress");
                 PROC address = getProc ? getProc(name) : NULL;
                 const uintptr_t value = (uintptr_t)address;
                 if(value <= 3 || value == ~uintptr_t(0))
                   address = GetProcAddress(module, name);
                 return (void *)address;
               }) != 0;
#elif defined(__linux__) && !defined(ANDROID)
      loaded = gladLoadGLES2Loader([](const char *name) -> void * {
                 if(glXGetProcAddressARB)
                   return (void *)glXGetProcAddressARB((const GLubyte *)name);
                 return glXGetProcAddress ? (void *)glXGetProcAddress((const GLubyte *)name) : NULL;
               }) != 0;
#endif
      // Android's EGL setup already loads ES core directly.
      if(!loaded || !GLAD_GL_ES_VERSION_3_2)
      {
        TEST_ERROR("Could not load native OpenGL ES 3.2 fixture entry points");
        return 4;
      }
    }
    if(!glTexStorage2D || !glClearBufferfi || !glBlitFramebuffer || !glFramebufferRenderbuffer)
    {
      TEST_ERROR("Required texture/depth fixture entry points are unavailable");
      return 4;
    }
    const std::string prefix =
        gles ? "#version 320 es\nprecision highp float;\n" : "#version 410 core\n";
    const std::string vertex =
        prefix + "layout(location=0) in vec3 pos; void main(){gl_Position=vec4(pos,1.0);}";
    const GLuint upstreamProgram =
        MakeProgram(vertex, prefix +
                                "layout(std140) uniform Parameters {vec4 tint;};"
                                "layout(location=0) out vec4 color; void main(){color=tint;}");
    const GLuint downstreamProgram =
        MakeProgram(vertex, prefix +
                                "layout(std140) uniform Parameters {vec4 tint;};"
                                "uniform sampler2D upstream; layout(location=0) out vec4 color;"
                                "void main(){vec2 uv=gl_FragCoord.xy/vec2(textureSize(upstream,0));"
                                "color=vec4(texture(upstream,uv).rgb*0.5+tint.rgb*0.5,1.0);}");
    glUniformBlockBinding(upstreamProgram, glGetUniformBlockIndex(upstreamProgram, "Parameters"), 0);
    glUniformBlockBinding(downstreamProgram,
                          glGetUniformBlockIndex(downstreamProgram, "Parameters"), 0);
    glUseProgram(downstreamProgram);
    glUniform1i(glGetUniformLocation(downstreamProgram, "upstream"), 0);

    GLuint targets[2] = {MakeTexture(), MakeTexture()};
    GLuint framebuffers[2] = {MakeFBO(), MakeFBO()};
    for(int index = 0; index < 2; ++index)
    {
      glBindTexture(GL_TEXTURE_2D, targets[index]);
      glTexStorage2D(GL_TEXTURE_2D, 1, GL_RGBA8, GLsizei(screenWidth), GLsizei(screenHeight));
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
      glBindFramebuffer(GL_FRAMEBUFFER, framebuffers[index]);
      glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, targets[index], 0);
    }
    GLuint depth = 0;
    glGenRenderbuffers(1, &depth);
    glBindRenderbuffer(GL_RENDERBUFFER, depth);
    glRenderbufferStorage(GL_RENDERBUFFER, GL_DEPTH24_STENCIL8, GLsizei(screenWidth),
                          GLsizei(screenHeight));
    glBindFramebuffer(GL_FRAMEBUFFER, framebuffers[0]);
    glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_DEPTH_STENCIL_ATTACHMENT, GL_RENDERBUFFER, depth);
    for(GLuint framebuffer : framebuffers)
    {
      glBindFramebuffer(GL_FRAMEBUFFER, framebuffer);
      if(glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE)
        return 4;
    }
    GLuint parameters[2] = {MakeBuffer(), MakeBuffer()};
    const float values[2][4] = {{0.75f, 0.25f, 0.125f, 1.0f}, {0.125f, 0.5f, 0.75f, 1.0f}};
    for(int index = 0; index < 2; ++index)
    {
      glBindBuffer(GL_UNIFORM_BUFFER, parameters[index]);
      glBufferData(GL_UNIFORM_BUFFER, sizeof(values[index]), values[index], GL_DYNAMIC_DRAW);
    }
    if(glObjectLabel)
    {
      glObjectLabel(GL_TEXTURE, targets[0], -1, "TextureVersion_Upstream_RT");
      glObjectLabel(GL_TEXTURE, targets[1], -1, "TextureVersion_Downstream_RT");
      glObjectLabel(GL_RENDERBUFFER, depth, -1, "TextureVersion_Upstream_DepthStencil");
      glObjectLabel(GL_BUFFER, parameters[0], -1, "TextureVersion_Upstream_Parameters");
      glObjectLabel(GL_BUFFER, parameters[1], -1, "TextureVersion_Downstream_Parameters");
    }
    while(Running())
    {
      glBindVertexArray(DefaultTriVAO);
      glBindFramebuffer(GL_FRAMEBUFFER, framebuffers[0]);
      glViewport(0, 0, GLsizei(screenWidth), GLsizei(screenHeight));
      glEnable(GL_DEPTH_TEST);
      glDepthFunc(GL_LESS);
      glDepthMask(GL_TRUE);
      const float upstreamClear[4] = {0.03125f, 0.0625f, 0.09375f, 1.0f};
      glClearBufferfv(GL_COLOR, 0, upstreamClear);
      glClearBufferfi(GL_DEPTH_STENCIL, 0, 1.0f, 0);
      glUseProgram(upstreamProgram);
      glBindBufferRange(GL_UNIFORM_BUFFER, 0, parameters[0], 0, 16);
      setMarker("Texture version upstream draw");
      glDrawArrays(GL_TRIANGLES, 0, 3);

      // Each later stage changes only B. A and the packed depth/stencil remain identical GPU
      // content across these checkpoints, while their independent object state is still saved.
      glDisable(GL_DEPTH_TEST);
      glBindFramebuffer(GL_FRAMEBUFFER, framebuffers[1]);
      const float downstreamClear[4] = {0.015625f, 0.03125f, 0.046875f, 1.0f};
      glClearBufferfv(GL_COLOR, 0, downstreamClear);
      glActiveTexture(GL_TEXTURE0);
      glBindTexture(GL_TEXTURE_2D, targets[0]);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
      glUseProgram(downstreamProgram);
      glBindBufferRange(GL_UNIFORM_BUFFER, 0, parameters[1], 0, 16);
      for(uint32_t region = 0; region < 4; ++region)
      {
        if(region == 2)
          glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_REPEAT);
        const GLint width = GLint(screenWidth / 4);
        glViewport(GLint(region) * width, 0, width, GLsizei(screenHeight));
        setMarker("Texture version downstream draw " + std::to_string(region));
        glDrawArrays(GL_TRIANGLES, 0, 3);
      }
      glBindFramebuffer(GL_READ_FRAMEBUFFER, framebuffers[1]);
      glBindFramebuffer(GL_DRAW_FRAMEBUFFER, 0);
      glBlitFramebuffer(0, 0, GLint(screenWidth), GLint(screenHeight), 0, 0, GLint(screenWidth),
                        GLint(screenHeight), GL_COLOR_BUFFER_BIT, GL_NEAREST);
      Present();
    }
    glDeleteRenderbuffers(1, &depth);
    return 0;
  }
};

namespace DesktopTextureVersions
{
RD_TEST(GL_Checkpoint_Texture_Versions, GLCheckpointTextureVersionsFixture)
{
  static constexpr const char *Description =
      "GL checkpoints share unchanged upstream RT and depth content";
  int main()
  {
    return Run(false);
  }
};
REGISTER_TEST();
}

namespace ESTextureVersions
{
RD_TEST(GLES_Checkpoint_Texture_Versions, GLCheckpointTextureVersionsFixture)
{
  static constexpr const char *Description =
      "ES checkpoints share unchanged upstream RT and depth content";
  int main()
  {
    return Run(true);
  }
};
REGISTER_TEST();
}
