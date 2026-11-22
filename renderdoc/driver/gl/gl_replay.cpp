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

#include "gl_replay.h"
#include <set>
#include "common/timing.h"

#include "core/settings.h"
#include "data/glsl_shaders.h"
#include "driver/ihv/amd/amd_counters.h"
#include "driver/ihv/arm/arm_counters.h"
#include "driver/ihv/intel/intel_gl_counters.h"
#include "driver/ihv/nv/nv_gl_counters.h"
#include "maths/matrix.h"
#include "replay/dummy_driver.h"
#include "replay/replay_resource_versions.h"
#include "replay/replay_timing.h"
#include "serialise/rdcfile.h"
#include "strings/string_utils.h"
#include "gl_driver.h"
#include "gl_resources.h"

#define OPENGL 1
#include "data/glsl/glsl_ubos_cpp.h"

RDOC_CONFIG(bool, OpenGL_HardwareCounters, true,
            "Enable support for IHV-specific hardware counters on OpenGL.");

static const char *SPIRVDisassemblyTarget = "SPIR-V (RenderDoc)";

namespace
{
struct GLCheckpointMetadataBudget
{
  explicit GLCheckpointMetadataBudget(uint64_t budget)
      : limit(RDCMIN<uint64_t>(budget, 64ULL * 1024 * 1024))
  {
  }
  uint64_t bytes = 0;
  const uint64_t limit;
  bool Add(uint64_t count, uint64_t stride)
  {
    if(bytes > limit || (stride && count > (limit - bytes) / stride))
    {
      bytes = ~0ULL;
      return false;
    }
    bytes += count * stride;
    return true;
  }
};

bool GLCheckpointUniformMetadata(GLCheckpointMetadataBudget &budget, int32_t block,
                                 int32_t arraySize, int32_t nameLength)
{
  if(block >= 0)
    return true;
  // The existing uniform serializer uses a 1024-byte name buffer. Bound both its
  // unrolled value array and all temporary/writer/final copies before entering it.
  return arraySize >= 0 && nameLength > 0 && nameLength <= 1024 &&
         (arraySize <= 1 || nameLength >= 4) && budget.Add(RDCMAX(1, arraySize), 2048);
}

bool GLCheckpointConstantMetadata(GLCheckpointMetadataBudget &budget, const ShaderConstant &var,
                                  size_t parentLength, uint32_t depth)
{
  if(depth > 32 || var.name.empty() || var.name.size() > 1000 ||
     parentLength > 1000 - var.name.size() || !budget.Add(1, 16384))
    return false;
  const size_t length = parentLength + var.name.size();
  const uint32_t elements = RDCMAX(1U, var.type.elements);
  if(var.type.members.empty())
    return length < 1000 && budget.Add(elements, 2048);
  // The general serializer expands struct arrays and constructs each nested name.
  // Keep this optional preflight finite without allocating that expansion itself.
  if(elements != 1 || length >= 1000)
    return false;
  for(const ShaderConstant &member : var.type.members)
    if(!GLCheckpointConstantMetadata(budget, member, length + 1, depth + 1))
      return false;
  return true;
}

bool GLCheckpointProgramMetadata(GLCheckpointMetadataBudget &budget,
                                 const PerStageReflections &stages, GLuint program)
{
  // Conservative charges cover the serializer's arrays, name strings, growing memory writer,
  // final bytebuf, and SPIR-V unroll scratch simultaneously. No GPU storage is allocated here.
  if(!budget.Add(1, 64 * 1024))
    return false;
  bool spirv = false;
  for(size_t s = 0; s < NumShaderStages; s++)
    spirv |= stages.refls[s] && stages.refls[s]->encoding == ShaderEncoding::OpenGLSPIRV;
  if(spirv)
  {
    for(size_t s = 0; s < NumShaderStages; s++)
      if(stages.refls[s])
        for(const ConstantBlock &block : stages.refls[s]->constantBlocks)
          if(!block.bufferBacked && block.byteSize > 0)
            for(const ShaderConstant &var : block.variables)
              if(!GLCheckpointConstantMetadata(budget, var, 0, 0))
                return false;
    return true;
  }
  GLint uniforms = 0;
  GL.glGetProgramInterfaceiv(program, eGL_UNIFORM, eGL_ACTIVE_RESOURCES, &uniforms);
  if(GL.glGetError() != eGL_NONE || uniforms < 0 || !budget.Add(uint64_t(uniforms), 16384))
    return false;
  const GLenum props[] = {eGL_BLOCK_INDEX, eGL_ARRAY_SIZE, eGL_NAME_LENGTH};
  for(GLint i = 0; i < uniforms; i++)
  {
    GLint values[3] = {-1, -1, -1};
    GL.glGetProgramResourceiv(program, eGL_UNIFORM, i, 3, props, 3, NULL, values);
    if(GL.glGetError() != eGL_NONE ||
       !GLCheckpointUniformMetadata(budget, values[0], values[1], values[2]))
      return false;
  }
  for(GLenum type : {eGL_UNIFORM_BLOCK, eGL_SHADER_STORAGE_BLOCK})
  {
    if(type == eGL_SHADER_STORAGE_BLOCK && !HasExt[ARB_shader_storage_buffer_object])
      continue;
    GLint count = 0;
    GL.glGetProgramInterfaceiv(program, type, eGL_ACTIVE_RESOURCES, &count);
    if(GL.glGetError() != eGL_NONE || count < 0 || !budget.Add(uint64_t(count), 16384))
      return false;
  }
  return true;
}

// GLInitialContents stores 8 colour attachments and 16 VAO attribute/buffer slots.
// Inspect actual captured parameters, never reject a GPU merely for having higher limits.
bool GLCheckpointObjectRangeUnsupported(const rdcstr &name, const SDObject &chunk)
{
  const auto highAttachment = [](const SDObject *value) {
    if(!value || (value->type.basetype != SDBasic::Enum && !value->IsUInt() && !value->IsInt()))
      return false;
    const uint64_t attachment = value->AsUInt64();
    return attachment >= uint64_t(eGL_COLOR_ATTACHMENT0) + 8 &&
           attachment < uint64_t(eGL_COLOR_ATTACHMENT0) + 32;
  };
  if(name.contains("Framebuffer") || name.contains("DrawBuffer") || name.contains("ReadBuffer"))
  {
    if(highAttachment(chunk.FindChild("attachment")) || highAttachment(chunk.FindChild("buf")) ||
       highAttachment(chunk.FindChild("mode")))
      return true;
    for(const char *field : {"attachments", "bufs"})
    {
      const SDObject *array = chunk.FindChild(field);
      if(array && array->IsArray())
        for(size_t i = 0; i < array->NumChildren(); i++)
        {
          const SDObject *value = array->GetChild(i);
          if(highAttachment(value) ||
             (rdcstr(field) == "bufs" && i >= 8 && value->AsUInt64() != uint64_t(eGL_NONE)))
            return true;
        }
    }
  }
  if(name.contains("Vertex") || name.contains("Attrib"))
  {
    for(const char *field : {"index", "attribindex", "bindingindex"})
    {
      const SDObject *value = chunk.FindChild(field);
      if(value && (value->IsUInt() || value->IsInt()) && value->AsUInt64() >= 16)
        return true;
    }
    if(name.contains("VertexBuffers"))
    {
      const SDObject *first = chunk.FindChild("first"), *count = chunk.FindChild("count");
      if(first && count && count->AsInt64() > 0 &&
         (first->AsUInt64() >= 16 || uint64_t(count->AsInt64()) > 16 - first->AsUInt64()))
        return true;
    }
  }
  return false;
}

bool GLCheckpointUnsupportedFrameCall(const rdcstr &name)
{
  if((name.beginsWith("glGen") && !name.beginsWith("glGenerate")) || name.beginsWith("glCreate") ||
     name.beginsWith("glDelete"))
    return true;
  // Object lifetime/storage, linked code, and hidden GPU execution state cannot be reconstructed
  // from the fixed-storage checkpoint below. Data uploads and ordinary state changes remain legal.
  const char *unsupported[] = {
      "BufferData",
      "BufferStorage",
      "TexImage",
      "TextureImage",
      "TexStorage",
      "TextureStorage",
      "CopyTexImage",
      "CopyTextureImage",
      "RenderbufferStorage",
      "TextureView",
      "PageCommitment",
      "BeginQuery",
      "EndQuery",
      "QueryCounter",
      "QueryBufferObject",
      "ConditionalRender",
      "TransformFeedback",
      "WaitSync",
      "FenceSync",
      "LinkProgram",
      "CompileShader",
      "ShaderSource",
      "ShaderBinary",
      "ProgramBinary",
      "AttachShader",
      "DetachShader",
      "SpecializeShader",
      "ProgramParameteri",
      "MakeTextureHandle",
      "MakeImageHandle",
      "UniformHandle",
      "MemoryObject",
      "Semaphore",
      "MultisampleEXT",
      "MultiviewOVR",
      "PerfQuery",
      "wglDX",
      "vrapi_",
      "FramebufferParameteri",
      "ParameterIiv",
      "ParameterIuiv",
      "VertexAttribI1",
      "VertexAttribI2",
      "VertexAttribI3",
      "VertexAttribI4",
      "VertexAttribL",
  };
  for(const char *unsafe : unsupported)
    if(name.contains(unsafe))
      return true;
  return false;
}
}

// Only checkpoint records own these allocations. The current execution lineage holds weak
// references, so eviction cannot leave an unaccounted live GPU copy behind.
struct GLCheckpointBufferVersion
{
  GLInitialContents initial;
  uint64_t bytes = 0;
  ~GLCheckpointBufferVersion()
  {
    if(initial.resource.name)
      GL.glDeleteBuffers(1, &initial.resource.name);
  }
};

struct GLCheckpointTextureVersion
{
  GLInitialContents initial;
  GLResource source;
  uint64_t bytes = 0;
  GLenum target = eGL_NONE, format = eGL_NONE;
  uint32_t width = 0, height = 0, depth = 0, mips = 0, samples = 0;
  ~GLCheckpointTextureVersion()
  {
    if(initial.resource.name)
    {
      if(initial.resource.Namespace == eResRenderbuffer)
        GL.glDeleteRenderbuffers(1, &initial.resource.name);
      else
        GL.glDeleteTextures(1, &initial.resource.name);
    }
  }
};

namespace
{
enum class GLCheckpointBufferEffect
{
  Unknown,
  StateOnly,
  ExplicitBufferWrite,
  ShaderExecution,
};

GLCheckpointBufferEffect GLCheckpointClassifyBufferEffect(GLChunk chunk)
{
  switch(chunk)
  {
    // Every alias dispatches to one of the two serialisers carrying the resolved destination.
    // Their native write hooks invalidate that resource before executing the update/copy.
    case GLChunk::glBufferSubData:
    case GLChunk::glBufferSubDataARB:
    case GLChunk::glNamedBufferSubData:
    case GLChunk::glNamedBufferSubDataEXT:
    case GLChunk::glCopyBufferSubData:
    case GLChunk::glCopyNamedBufferSubData:
    case GLChunk::glNamedCopyBufferSubDataEXT: return GLCheckpointBufferEffect::ExplicitBufferWrite;
    case GLChunk::glDispatchCompute:
    case GLChunk::glDispatchComputeGroupSizeARB:
    case GLChunk::glDispatchComputeIndirect:
    case GLChunk::glDrawArrays:
    case GLChunk::glDrawArraysIndirect:
    case GLChunk::glDrawArraysInstancedARB:
    case GLChunk::glDrawArraysInstancedEXT:
    case GLChunk::glDrawArraysInstanced:
    case GLChunk::glDrawArraysInstancedBaseInstanceEXT:
    case GLChunk::glDrawArraysInstancedBaseInstance:
    case GLChunk::glDrawElements:
    case GLChunk::glDrawElementsIndirect:
    case GLChunk::glDrawRangeElementsEXT:
    case GLChunk::glDrawRangeElements:
    case GLChunk::glDrawRangeElementsBaseVertexEXT:
    case GLChunk::glDrawRangeElementsBaseVertexOES:
    case GLChunk::glDrawRangeElementsBaseVertex:
    case GLChunk::glDrawElementsBaseVertexEXT:
    case GLChunk::glDrawElementsBaseVertexOES:
    case GLChunk::glDrawElementsBaseVertex:
    case GLChunk::glDrawElementsInstancedARB:
    case GLChunk::glDrawElementsInstancedEXT:
    case GLChunk::glDrawElementsInstanced:
    case GLChunk::glDrawElementsInstancedBaseInstanceEXT:
    case GLChunk::glDrawElementsInstancedBaseInstance:
    case GLChunk::glDrawElementsInstancedBaseVertexEXT:
    case GLChunk::glDrawElementsInstancedBaseVertexOES:
    case GLChunk::glDrawElementsInstancedBaseVertex:
    case GLChunk::glDrawElementsInstancedBaseVertexBaseInstanceEXT:
    case GLChunk::glDrawElementsInstancedBaseVertexBaseInstance:
    case GLChunk::glMultiDrawArraysEXT:
    case GLChunk::glMultiDrawArrays:
    case GLChunk::glMultiDrawElements:
    case GLChunk::glMultiDrawElementsBaseVertexEXT:
    case GLChunk::glMultiDrawElementsBaseVertexOES:
    case GLChunk::glMultiDrawElementsBaseVertex:
    case GLChunk::glMultiDrawArraysIndirect:
    case GLChunk::glMultiDrawElementsIndirect:
    case GLChunk::glMultiDrawArraysIndirectCountARB:
    case GLChunk::glMultiDrawArraysIndirectCount:
    case GLChunk::glMultiDrawElementsIndirectCountARB:
    case GLChunk::glMultiDrawElementsIndirectCount:
      return GLCheckpointBufferEffect::ShaderExecution;

    // Explicitly audited buffer-content-preserving calls. In particular, glClearBufferfv is an
    // FBO operation; glClearBufferData/SubData are deliberately NOT in this list.
    case GLChunk::glBindBuffer:
    case GLChunk::glBindBufferARB:
    case GLChunk::glBindBufferBase:
    case GLChunk::glBindBufferBaseEXT:
    case GLChunk::glBindBufferRange:
    case GLChunk::glBindBufferRangeEXT:
    case GLChunk::glBindBuffersBase:
    case GLChunk::glBindBuffersRange:
    case GLChunk::glBindVertexArray:
    case GLChunk::glBindVertexArrayOES:
    case GLChunk::glBindVertexBuffer:
    case GLChunk::glBindVertexBuffers:
    case GLChunk::glBindFramebuffer:
    case GLChunk::glBindFramebufferEXT:
    case GLChunk::glBindTexture:
    case GLChunk::glBindTextures:
    case GLChunk::glBindMultiTextureEXT:
    case GLChunk::glBindTextureUnit:
    case GLChunk::glBindImageTexture:
    case GLChunk::glBindImageTextureEXT:
    case GLChunk::glBindImageTextures:
    case GLChunk::glBindSampler:
    case GLChunk::glBindSamplers:
    case GLChunk::glUseProgram:
    case GLChunk::glUseProgramStages:
    case GLChunk::glUseProgramStagesEXT:
    case GLChunk::glUniformBlockBinding:
    case GLChunk::glUniform1f:
    case GLChunk::glUniform2f:
    case GLChunk::glUniform3f:
    case GLChunk::glUniform4f:
    case GLChunk::glUniform1i:
    case GLChunk::glUniform1fv:
    case GLChunk::glUniform2fv:
    case GLChunk::glUniform3fv:
    case GLChunk::glUniform4fv:
    case GLChunk::glUniformMatrix4fv:
    case GLChunk::glProgramUniform1f:
    case GLChunk::glProgramUniform2f:
    case GLChunk::glProgramUniform3f:
    case GLChunk::glProgramUniform4f:
    case GLChunk::glProgramUniform1i:
    case GLChunk::glProgramUniform1fv:
    case GLChunk::glProgramUniform2fv:
    case GLChunk::glProgramUniform3fv:
    case GLChunk::glProgramUniform4fv:
    case GLChunk::glProgramUniformMatrix4fv:
    case GLChunk::glBindProgramPipeline:
    case GLChunk::glBindProgramPipelineEXT:
    case GLChunk::glViewport:
    case GLChunk::glScissor:
    case GLChunk::glEnable:
    case GLChunk::glDisable:
    case GLChunk::glEnablei:
    case GLChunk::glDisablei:
    case GLChunk::glDepthFunc:
    case GLChunk::glDepthMask:
    case GLChunk::glDepthRange:
    case GLChunk::glDepthRangef:
    case GLChunk::glBlendFunc:
    case GLChunk::glBlendFuncSeparate:
    case GLChunk::glBlendEquation:
    case GLChunk::glBlendEquationSeparate:
    case GLChunk::glBlendColor:
    case GLChunk::glColorMask:
    case GLChunk::glStencilFunc:
    case GLChunk::glStencilFuncSeparate:
    case GLChunk::glStencilMask:
    case GLChunk::glStencilMaskSeparate:
    case GLChunk::glStencilOp:
    case GLChunk::glStencilOpSeparate:
    case GLChunk::glFrontFace:
    case GLChunk::glCullFace:
    case GLChunk::glPolygonMode:
    case GLChunk::glPolygonOffset:
    case GLChunk::glClearColor:
    case GLChunk::glClearDepth:
    case GLChunk::glClearDepthf:
    case GLChunk::glClearStencil:
    case GLChunk::glClear:
    case GLChunk::glClearBufferfv:
    case GLChunk::glClearNamedFramebufferfv:
    case GLChunk::glClearBufferiv:
    case GLChunk::glClearNamedFramebufferiv:
    case GLChunk::glClearBufferuiv:
    case GLChunk::glClearNamedFramebufferuiv:
    case GLChunk::glClearBufferfi:
    case GLChunk::glClearNamedFramebufferfi:
    case GLChunk::glDrawBuffer:
    case GLChunk::glDrawBuffers:
    case GLChunk::glDrawBuffersARB:
    case GLChunk::glDrawBuffersEXT:
    case GLChunk::glReadBuffer:
    case GLChunk::glMemoryBarrier:
    case GLChunk::glMemoryBarrierEXT:
    case GLChunk::glMemoryBarrierByRegion:
    case GLChunk::glTextureBarrier:
    case GLChunk::glPushDebugGroup:
    case GLChunk::glPushDebugGroupKHR:
    case GLChunk::glPushGroupMarkerEXT:
    case GLChunk::glPopDebugGroup:
    case GLChunk::glPopDebugGroupKHR:
    case GLChunk::glPopGroupMarkerEXT:
    case GLChunk::glDebugMessageInsert:
    case GLChunk::glDebugMessageInsertARB:
    case GLChunk::glDebugMessageInsertKHR:
    case GLChunk::glStringMarkerGREMEDY:
    case GLChunk::glInsertEventMarkerEXT:
    case GLChunk::SetCommandAnnotation: return GLCheckpointBufferEffect::StateOnly;
    default: return GLCheckpointBufferEffect::Unknown;
  }
}

enum class GLCheckpointTextureEffect
{
  Unknown,
  StateOnly,
  Draw,
  Dispatch,
  BoundFramebuffer,
  ExplicitDestination,
};

GLCheckpointTextureEffect GLCheckpointClassifyTextureEffect(GLChunk chunk)
{
  // The buffer whitelist contains clears, which are NOT texture-content-preserving calls.
  switch(chunk)
  {
    case GLChunk::glActiveTexture:
    case GLChunk::glActiveTextureARB:
    case GLChunk::glTexParameteri:
    case GLChunk::glTextureParameteri:
    case GLChunk::glTextureParameteriEXT:
    case GLChunk::glMultiTexParameteriEXT:
    case GLChunk::glTexParameteriv:
    case GLChunk::glTextureParameteriv:
    case GLChunk::glTextureParameterivEXT:
    case GLChunk::glMultiTexParameterivEXT:
    case GLChunk::glTexParameterf:
    case GLChunk::glTextureParameterf:
    case GLChunk::glTextureParameterfEXT:
    case GLChunk::glMultiTexParameterfEXT:
    case GLChunk::glTexParameterfv:
    case GLChunk::glTextureParameterfv:
    case GLChunk::glTextureParameterfvEXT:
    case GLChunk::glMultiTexParameterfvEXT: return GLCheckpointTextureEffect::StateOnly;
    case GLChunk::glClear: return GLCheckpointTextureEffect::BoundFramebuffer;
    case GLChunk::glClearBufferfv:
    case GLChunk::glClearNamedFramebufferfv:
    case GLChunk::glClearBufferiv:
    case GLChunk::glClearNamedFramebufferiv:
    case GLChunk::glClearBufferuiv:
    case GLChunk::glClearNamedFramebufferuiv:
    case GLChunk::glClearBufferfi:
    case GLChunk::glClearNamedFramebufferfi:
    case GLChunk::glBlitFramebuffer:
    case GLChunk::glBlitFramebufferEXT:
    case GLChunk::glBlitNamedFramebuffer:
    case GLChunk::glCopyImageSubData:
    case GLChunk::glCopyImageSubDataEXT:
    case GLChunk::glCopyImageSubDataOES:
    case GLChunk::glClearTexImage:
    case GLChunk::glClearTexSubImage: return GLCheckpointTextureEffect::ExplicitDestination;
    case GLChunk::glDispatchCompute:
    case GLChunk::glDispatchComputeIndirect:
    case GLChunk::glDispatchComputeGroupSizeARB: return GLCheckpointTextureEffect::Dispatch;
    default: break;
  }
  const GLCheckpointBufferEffect buffer = GLCheckpointClassifyBufferEffect(chunk);
  if(buffer == GLCheckpointBufferEffect::ShaderExecution)
    return GLCheckpointTextureEffect::Draw;
  // Explicit buffer copies cannot alias ordinary texture storage. Texture-buffer views own no
  // image snapshot; external memory and texture views are excluded by the separate sharing gate.
  if(buffer == GLCheckpointBufferEffect::StateOnly ||
     buffer == GLCheckpointBufferEffect::ExplicitBufferWrite)
    return GLCheckpointTextureEffect::StateOnly;
  return GLCheckpointTextureEffect::Unknown;
}

uint64_t GLRenderbufferByteSize(uint32_t width, uint32_t height, uint32_t samples,
                                GLenum internalFormat)
{
  if(!width || !height || !samples || internalFormat == eGL_NONE ||
     IsCompressedFormat(internalFormat))
    return 0;
  const GLenum format = GetBaseFormat(internalFormat), type = GetDataType(internalFormat);
  if(format == eGL_NONE || type == eGL_NONE)
    return 0;
  // Packed depth/stencil and packed colour formats deliberately have compByteWidth == 0.
  // Use their native pixel representation, then perform all dimensional products in 64 bits.
  uint64_t bytes = uint64_t(GetByteSize(1, 1, 1, format, type));
  for(uint32_t factor : {width, height, samples})
  {
    if(bytes > ~uint64_t(0) / factor)
      return 0;
    bytes *= factor;
  }
  return bytes;
}

bool GLCheckpointTextureShapeMatches(const GLCheckpointTextureVersion &version,
                                     const GLResource &source, uint64_t bytes, GLenum target,
                                     GLenum format, const TextureDescription &texture)
{
  return version.source == source && version.bytes == bytes && version.target == target &&
         version.format == format && version.width == texture.width &&
         version.height == texture.height && version.depth == texture.depth &&
         version.mips == texture.mips && version.samples == texture.msSamp;
}

template <typename Query, typename Resolve>
bool GLCheckpointFramebufferWriteSet(GLint maximum, Query query, Resolve resolve,
                                     rdcarray<ResourceId> &writes)
{
  if(maximum <= 0 || maximum > 256)
    return false;
  for(GLint index = 0; index < maximum + 2; ++index)
  {
    const GLenum attachment = index < maximum    ? GLenum(eGL_COLOR_ATTACHMENT0 + index)
                              : index == maximum ? eGL_DEPTH_ATTACHMENT
                                                 : eGL_STENCIL_ATTACHMENT;
    GLint type = 0, name = 0;
    if(!query(attachment, eGL_FRAMEBUFFER_ATTACHMENT_OBJECT_TYPE, type))
      return false;
    if(type == eGL_NONE)
      continue;
    if(!query(attachment, eGL_FRAMEBUFFER_ATTACHMENT_OBJECT_NAME, name) || !name ||
       (type != eGL_TEXTURE && type != eGL_RENDERBUFFER))
      return false;
    ResourceId id = resolve(GLenum(type), GLuint(name));
    if(id == ResourceId())
      return false;
    if(!writes.contains(id))
      writes.push_back(id);
  }
  return true;
}

void GLCheckpointApplyTextureWrites(
    bool complete, const rdcarray<ResourceId> &writes,
    std::map<ResourceId, std::weak_ptr<GLCheckpointTextureVersion>> &versions)
{
  if(!complete)
    versions.clear();
  else
    for(ResourceId id : writes)
      versions.erase(id);
}

bool GLCheckpointTextureResident(const std::weak_ptr<GLCheckpointTextureVersion> &resident,
                                 const std::shared_ptr<GLCheckpointTextureVersion> &target)
{
  return target && resident.lock() == target;
}

GLInitialContents GLCheckpointTextureRestoreState(const GLInitialContents &state,
                                                  GLResource contents, bool resident)
{
  GLInitialContents result = state;
  result.resource = resident ? GLResource(MakeNullResource) : contents;
  return result;
}

struct GLCheckpointTextureAccounting
{
  std::set<const GLCheckpointTextureVersion *> versions;
  uint64_t allocated = 0, shared = 0;
  void Add(const GLCheckpointTextureVersion *version)
  {
    if(versions.insert(version).second)
      allocated += sizeof(GLCheckpointTextureVersion) + version->bytes;
    else
      shared += version->bytes;
  }
};

// The callback queries the COMPLETE native slot range, not GLRenderState's fixed arrays or a
// reflection's used bindings. Any uncertainty discards sharing, including a bound image that
// could alias a buffer through a texture-buffer view.
template <typename Query, typename Dirty>
bool GLCheckpointEnumerateBufferWrites(bool storage, bool atomic, bool images, Query query,
                                       Dirty dirty)
{
  const struct
  {
    bool supported;
    GLenum maximum, binding;
  } groups[] = {{storage, eGL_MAX_SHADER_STORAGE_BUFFER_BINDINGS, eGL_SHADER_STORAGE_BUFFER_BINDING},
                {atomic, eGL_MAX_ATOMIC_COUNTER_BUFFER_BINDINGS, eGL_ATOMIC_COUNTER_BUFFER_BINDING},
                {images, eGL_MAX_IMAGE_UNITS, eGL_IMAGE_BINDING_NAME}};
  for(const auto &group : groups)
  {
    if(!group.supported)
      continue;
    GLint count = 0;
    if(!query(group.maximum, ~0U, count) || count <= 0 || count > 65536)
      return false;
    for(GLint slot = 0; slot < count; ++slot)
    {
      GLint name = 0;
      if(!query(group.binding, (GLuint)slot, name))
        return false;
      if(name)
      {
        if(group.binding == eGL_IMAGE_BINDING_NAME)
          return false;
        dirty((GLuint)name);
      }
    }
  }
  return true;
}

struct GLCheckpointBufferAccounting
{
  std::set<const GLCheckpointBufferVersion *> versions;
  uint64_t allocated = 0, shared = 0;
  void Add(const GLCheckpointBufferVersion *version)
  {
    if(versions.insert(version).second)
      allocated += sizeof(GLCheckpointBufferVersion) + version->bytes;
    else
      shared += version->bytes;
  }
};

std::shared_ptr<GLCheckpointBufferVersion> GLCheckpointReusableBufferVersion(
    const std::weak_ptr<GLCheckpointBufferVersion> &previous, uint64_t bytes)
{
  std::shared_ptr<GLCheckpointBufferVersion> version = previous.lock();
  if(version && version->bytes != bytes)
    version.reset();
  return version;
}

bool GLCheckpointContentEvidenceComplete(GLChunk chunk)
{
  // Shader execution writes only exposed attachment/storage bindings. All such bindings are
  // excluded below, including unused bindings a replacement shader could start accessing.
  const GLCheckpointBufferEffect effect = GLCheckpointClassifyBufferEffect(chunk);
  return effect == GLCheckpointBufferEffect::StateOnly ||
         effect == GLCheckpointBufferEffect::ShaderExecution ||
         effect == GLCheckpointBufferEffect::ExplicitBufferWrite;
}

bool GLPatchInitialBufferEvidenceComplete(GLChunk chunk)
{
  if(chunk == GLChunk::SwapBuffers || chunk == GLChunk::glFlush || chunk == GLChunk::glFinish)
    return true;
  if(GLCheckpointContentEvidenceComplete(chunk))
    return true;
  // Texture parameter changes and framebuffer/image copies cannot write an
  // ordinary UBO. Buffer/texture aliases are rejected separately by the patch
  // binding proof. Do not weaken texture checkpoint sharing to admit these.
  const GLCheckpointTextureEffect effect = GLCheckpointClassifyTextureEffect(chunk);
  return effect == GLCheckpointTextureEffect::StateOnly ||
         effect == GLCheckpointTextureEffect::BoundFramebuffer ||
         effect == GLCheckpointTextureEffect::ExplicitDestination;
}

bool GLCheckpointShareOriginalContents(bool completeEvidence, bool potentialWrite,
                                       bool aliasUncertain, bool edited)
{
  if(!completeEvidence || potentialWrite || aliasUncertain || edited)
    return false;
  // An original static resource keeps the same native contents through every canonical replay.
  // This identity is per resource; it is not inferred from matching EIDs or texture pixels.
  const ReplayResourceVersion original = {1, 0};
  return PlanReplayResourceRestore(original, original, 0, {}) == ReplayResourceRestoreAction::Keep;
}
}

// A replay checkpoint owns complete live resource contents and object state. The capture's
// immutable shaders and serialised command stream remain shared in the resource manager.
struct GLReplay::ReplayCheckpoint
{
  struct Resource
  {
    ResourceId id;
    GLResource live;
    GLInitialContents initial;
    std::shared_ptr<GLCheckpointBufferVersion> bufferVersion;
    std::shared_ptr<GLCheckpointTextureVersion> textureVersion;
    bytebuf uniforms;
    uint64_t bytes = 0;
    uint32_t width = 0, height = 0;
    bool renderbuffer = false;
    bool sharedContents = false;
    GLint activeProgram = 0;
  };
  ~ReplayCheckpoint()
  {
    // All checkpoint destruction is performed with the replay context current.
    for(Resource &res : resources)
    {
      if(!res.initial.resource.name)
        continue;
      GLuint name = res.initial.resource.name;
      if(res.initial.resource.Namespace == eResBuffer)
        GL.glDeleteBuffers(1, &name);
      else if(res.initial.resource.Namespace == eResTexture)
        GL.glDeleteTextures(1, &name);
      else if(res.initial.resource.Namespace == eResRenderbuffer)
        GL.glDeleteRenderbuffers(1, &name);
    }
  }
  uint32_t eventId = 0;
  uint64_t lastUsed = 0, bytes = 0;
  uint64_t staticBytes = 0;
  uint32_t staticResources = 0;
  GLRenderState state;
  Topology topology = Topology::Unknown;
  uint32_t indexWidth = 0;
  GLuint defaultFBO = 0;
  GLInitialContents defaultFBOState;
  PixelPackState pack;
  GLint renderbufferBinding = 0;
  rdcarray<Resource> resources;
};

GLReplay::~GLReplay()
{
}

void GLReplay::ResetCheckpointBufferLineage()
{
  m_CheckpointBufferVersions.clear();
}

void GLReplay::ResetCheckpointTextureLineage()
{
  m_CheckpointTextureVersions.clear();
  m_CheckpointTexturePendingTarget = 0;
}

void GLReplay::ResetCheckpointTextureTracking()
{
  ResetCheckpointTextureLineage();
  m_CheckpointTextureWrites.clear();
  m_CheckpointTextureWriteResources = 0;
  m_CheckpointTextureWriteOverflow = false;
  m_CheckpointTextureBindingEdited = false;
  m_CheckpointTextureSharingScanned = m_CheckpointTextureSharingSafe = false;
}

bool GLReplay::QueryCheckpointFramebufferWrites(GLuint framebuffer, rdcarray<ResourceId> &writes)
{
  if(!framebuffer || !GL.glGetNamedFramebufferAttachmentParameterivEXT)
    return false;
  GLint maximum = 0;
  GL.glGetIntegerv(eGL_MAX_COLOR_ATTACHMENTS, &maximum);
  return GLCheckpointFramebufferWriteSet(
      maximum,
      [framebuffer](GLenum attachment, GLenum parameter, GLint &value) {
        GL.glGetNamedFramebufferAttachmentParameterivEXT(framebuffer, attachment, parameter, &value);
        return true;
      },
      [this](GLenum type, GLuint name) {
        const GLResource resource = type == eGL_TEXTURE ? TextureRes(m_pDriver->GetCtx(), name)
                                                        : RenderbufferRes(m_pDriver->GetCtx(), name);
        return m_pDriver->GetResourceManager()->GetResID(resource);
      },
      writes);
}

void GLReplay::RecordCheckpointTextureWrite(const GLResource &resource)
{
  auto found = m_CheckpointTextureWrites.find(m_pDriver->m_CurChunkOffset);
  if(found == m_CheckpointTextureWrites.end() || m_CheckpointTextureWriteOverflow)
    return;
  TextureWriteRecord &record = found->second;
  if(GL.glGetError() != eGL_NONE)
    return;
  if(resource.Namespace == eResFramebuffer)
    record.complete = QueryCheckpointFramebufferWrites(resource.name, record.writes);
  else if(resource.Namespace == eResTexture || resource.Namespace == eResRenderbuffer)
  {
    ResourceId id = m_pDriver->GetResourceManager()->GetResID(resource);
    record.complete = id != ResourceId();
    if(record.complete)
      record.writes.push_back(id);
  }
  record.complete &= GL.glGetError() == eGL_NONE;
  m_CheckpointTextureWriteResources += record.writes.size();
  if(m_CheckpointTextureWriteResources > 1024 * 1024)
  {
    m_CheckpointTextureWrites.clear();
    m_CheckpointTextureWriteOverflow = true;
  }
}

void GLReplay::TrackCheckpointTextureWrites(GLChunk chunk)
{
  const GLCheckpointTextureEffect effect = GLCheckpointClassifyTextureEffect(chunk);
  if(IsLoading(m_pDriver->m_State))
  {
    if(effect == GLCheckpointTextureEffect::StateOnly ||
       effect == GLCheckpointTextureEffect::Unknown || m_CheckpointTextureWriteOverflow)
      return;
    if(m_CheckpointTextureWrites.size() >= 262144)
    {
      m_CheckpointTextureWrites.clear();
      m_CheckpointTextureWriteOverflow = true;
      return;
    }
    TextureWriteRecord &record = m_CheckpointTextureWrites[m_pDriver->m_CurChunkOffset];
    record.chunk = uint32_t(chunk);
    // DSA clear/copy serialisers provide their resolved destination after deserialisation. An
    // absent callback leaves this record incomplete, never an empty set of possible writes.
    if(effect == GLCheckpointTextureEffect::ExplicitDestination || GL.glGetError() != eGL_NONE)
      return;
    record.complete = true;
    if(effect != GLCheckpointTextureEffect::Dispatch)
    {
      GLint framebuffer = 0;
      GL.glGetIntegerv(eGL_DRAW_FRAMEBUFFER_BINDING, &framebuffer);
      record.complete = QueryCheckpointFramebufferWrites(GLuint(framebuffer), record.writes);
    }
    if(effect != GLCheckpointTextureEffect::BoundFramebuffer && HasExt[ARB_shader_image_load_store])
    {
      GLint maximum = 0;
      GL.glGetIntegerv(eGL_MAX_IMAGE_UNITS, &maximum);
      if(!GL.glGetIntegeri_v || maximum <= 0 || maximum > 65536)
        record.complete = false;
      else
        for(GLint slot = 0; slot < maximum; ++slot)
        {
          GLint name = 0;
          GL.glGetIntegeri_v(eGL_IMAGE_BINDING_NAME, GLuint(slot), &name);
          if(!name)
            continue;
          ResourceId id = m_pDriver->GetResourceManager()->GetResID(
              TextureRes(m_pDriver->GetCtx(), GLuint(name)));
          record.complete &= id != ResourceId();
          if(!record.writes.contains(id))
            record.writes.push_back(id);
        }
    }
    record.complete &= GL.glGetError() == eGL_NONE;
    m_CheckpointTextureWriteResources += record.writes.size();
    if(m_CheckpointTextureWriteResources > 1024 * 1024)
    {
      m_CheckpointTextureWrites.clear();
      m_CheckpointTextureWriteOverflow = true;
    }
    return;
  }
  if(!IsActiveReplaying(m_pDriver->m_State) || m_CheckpointTextureVersions.empty())
    return;
  if(!m_CheckpointTextureReplayManaged || m_ReplayCheckpointBypass || m_pDriver->m_FetchCounters ||
     m_pDriver->m_ActiveFeedback || m_pDriver->m_ActiveConditional)
  {
    ResetCheckpointTextureLineage();
    return;
  }
  if(effect == GLCheckpointTextureEffect::StateOnly)
    return;
  const auto found = m_CheckpointTextureWrites.find(m_pDriver->m_CurChunkOffset);
  if(effect == GLCheckpointTextureEffect::Unknown || m_CheckpointTextureWriteOverflow ||
     found == m_CheckpointTextureWrites.end() || !found->second.complete ||
     found->second.chunk != uint32_t(chunk))
  {
    GLCheckpointApplyTextureWrites(false, {}, m_CheckpointTextureVersions);
    m_CheckpointTexturePendingTarget = 0;
    return;
  }
  // The index records complete native bindings, including unused image units, all colour
  // attachments, and depth/stencil. Neither captured reflection nor write masks narrow it.
  GLCheckpointApplyTextureWrites(true, found->second.writes, m_CheckpointTextureVersions);
}

bool GLReplay::PrepareCheckpointTextureSharing()
{
  if(m_CheckpointTextureBindingEdited ||
     Process::GetEnvVariable("RENDERDOC_GL_DISABLE_CHECKPOINT_TEXTURE_VERSIONS") == "1")
    return false;
  if(m_CheckpointTextureSharingScanned)
    return m_CheckpointTextureSharingSafe;
  m_CheckpointTextureSharingScanned = true;
  if(m_CheckpointTextureWriteOverflow || !PrepareReplayCheckpoints())
    return false;
  // A view may write its parent under another name. Keep the original full-image path until a
  // complete storage-owner graph exists. Replay-only display/copy resources are not capture assets.
  for(const auto &entry : m_pDriver->m_Textures)
    if((!ResourceIDGen::IsReplayOnlyID(entry.first) ||
        (entry.second.creationFlags & TextureCategory::SwapBuffer)) &&
       entry.second.view)
      return false;
  SDFile *file = GetStructuredFile();
  if(!file)
    return false;
  for(const SDChunk *chunk : file->chunks)
  {
    const rdcstr name = m_pDriver->GetChunkName(chunk->metadata.chunkID);
    // Include capture-begin setup: automatic multisample resolves, bindless and external storage
    // can affect bytes outside the draw's ordinary attachment/image binding set.
    for(const char *unsafe :
        {"TextureHandle", "ImageHandle", "UniformHandle", "PageCommitment", "MemoryObject",
         "Semaphore", "EGLImage", "MultisampleEXT", "MultiviewOVR", "wglDX"})
      if(name.contains(unsafe))
        return false;
  }
  m_CheckpointTextureSharingSafe = true;
  return true;
}

void GLReplay::InvalidateCheckpointBufferVersion(const GLResource &buffer)
{
  m_CheckpointBufferVersions.erase(m_pDriver->GetResourceManager()->GetID(buffer));
}

void GLReplay::TrackCheckpointBufferWrites(GLChunk chunk)
{
  if(m_CheckpointBufferVersions.empty())
    return;
  const GLCheckpointBufferEffect effect = GLCheckpointClassifyBufferEffect(chunk);
  if(effect == GLCheckpointBufferEffect::StateOnly ||
     effect == GLCheckpointBufferEffect::ExplicitBufferWrite)
    return;
  if(effect == GLCheckpointBufferEffect::Unknown || m_ReplayCheckpointBypass ||
     m_pDriver->m_ActiveFeedback || m_pDriver->m_ActiveConditional || m_pDriver->m_FetchCounters ||
     !GL.glGetIntegeri_v || GL.glGetError() != eGL_NONE)
  {
    ResetCheckpointBufferLineage();
    return;
  }
  const bool complete = GLCheckpointEnumerateBufferWrites(
      HasExt[ARB_shader_storage_buffer_object], HasExt[ARB_shader_atomic_counters],
      HasExt[ARB_shader_image_load_store],
      [](GLenum parameter, GLuint index, GLint &value) {
        if(index == ~0U)
          GL.glGetIntegerv(parameter, &value);
        else
          GL.glGetIntegeri_v(parameter, index, &value);
        return true;
      },
      [this](GLuint name) {
        InvalidateCheckpointBufferVersion(BufferRes(m_pDriver->GetCtx(), name));
      });
  // One error check covers the whole enumeration; do not add a driver round trip per slot.
  const bool querySucceeded = GL.glGetError() == eGL_NONE;
  if(!complete || !querySucceeded)
    ResetCheckpointBufferLineage();
}

void GLReplay::ConfigureReplayCheckpoints(const ReplayCheckpointConfig &config,
                                          const rdcarray<ReplayCheckpointCandidate> &candidates)
{
  MakeCurrentReplayContext(&m_ReplayCtx);
  ResetCheckpointBufferLineage();
  if(m_CheckpointStats.enabled && ReplayCheckpointStorageCompatible(m_CheckpointConfig, config))
  {
    for(auto it = m_Checkpoints.begin(); it != m_Checkpoints.end();)
      if(it->first == m_ReplayPatchAnchorEvent ||
         ReplayCheckpointCandidateContains(candidates, it->first))
        ++it;
      else
        it = m_Checkpoints.erase(it);
    m_CheckpointConfig = config;
    m_CheckpointCandidates = candidates;
    std::sort(m_CheckpointCandidates.begin(), m_CheckpointCandidates.end(),
              [](const ReplayCheckpointCandidate &a, const ReplayCheckpointCandidate &b) {
                return a.eventId < b.eventId;
              });
    UpdateCheckpointAccounting();
    return;
  }
  m_Checkpoints.clear();
  m_CheckpointConfig = config;
  m_CheckpointCandidates = candidates;
  std::sort(m_CheckpointCandidates.begin(), m_CheckpointCandidates.end(),
            [](const ReplayCheckpointCandidate &a, const ReplayCheckpointCandidate &b) {
              return a.eventId < b.eventId;
            });
  m_CheckpointStats = ReplayCheckpointStats();
  m_CheckpointStats.supported = true;
  m_CheckpointStats.enabled = config.enabled && config.byteBudget > 0 && config.maxCheckpoints > 0;
  m_CheckpointPrepared = m_CheckpointPreparingFailed = false;
}

void GLReplay::InvalidateReplayCheckpoints(uint32_t firstAffectedEvent)
{
  InvalidateLiveForward();
  MakeCurrentReplayContext(&m_ReplayCtx);
  ResetCheckpointBufferLineage();
  // Value-only edits can retain a pre-consumer prefix, but every subsequent save must classify
  // the new edit table again. Set/clear and shader/resource replacement use this same reset.
  m_CheckpointStaticResources.clear();
  m_CheckpointTextureSharingScanned = m_CheckpointTextureSharingSafe = false;
  for(auto it = m_Checkpoints.begin(); it != m_Checkpoints.end();)
  {
    if(firstAffectedEvent && it->first < firstAffectedEvent)
      ++it;
    else
      it = m_Checkpoints.erase(it);
  }
  m_CheckpointStats.invalidations++;
  m_CheckpointPrepared = m_CheckpointPreparingFailed = false;
  UpdateCheckpointAccounting();
}

void GLReplay::UpdateCheckpointAccounting()
{
  m_CheckpointStats.events.clear();
  m_CheckpointStats.allocatedBytes = CheckpointStorageBytes(NULL, m_CheckpointStats.sharedBytes);
  for(const auto &entry : m_Checkpoints)
  {
    m_CheckpointStats.events.push_back(entry.first);
  }
  m_CheckpointStats.checkpointCount = (uint32_t)m_Checkpoints.size();
}

uint64_t GLReplay::CheckpointStorageBytes(const ReplayCheckpoint *building, uint64_t &sharedBytes) const
{
  GLCheckpointBufferAccounting buffers;
  GLCheckpointTextureAccounting textures;
  uint64_t bytes = 0, staticBytes = 0;
  const auto account = [&](const ReplayCheckpoint &checkpoint) {
    bytes += checkpoint.bytes;
    for(const ReplayCheckpoint::Resource &resource : checkpoint.resources)
    {
      if(resource.bufferVersion)
        buffers.Add(resource.bufferVersion.get());
      if(resource.textureVersion)
        textures.Add(resource.textureVersion.get());
      if(resource.sharedContents)
        staticBytes += resource.bytes;
    }
  };
  for(const auto &entry : m_Checkpoints)
    account(*entry.second);
  if(building)
    account(*building);
  sharedBytes = buffers.shared + staticBytes + textures.shared;
  return bytes + buffers.allocated + textures.allocated;
}

ReplayCheckpointStats GLReplay::GetReplayCheckpointStats()
{
  UpdateCheckpointAccounting();
  return m_CheckpointStats;
}

void GLReplay::EvictOldestCheckpoint()
{
  auto oldest = m_Checkpoints.end();
  for(auto it = m_Checkpoints.begin(); it != m_Checkpoints.end(); ++it)
    if(it->first != m_ReplayPatchAnchorEvent &&
       (oldest == m_Checkpoints.end() || it->second->lastUsed < oldest->second->lastUsed))
      oldest = it;
  if(oldest != m_Checkpoints.end())
    m_Checkpoints.erase(oldest);
  UpdateCheckpointAccounting();
}

bool GLReplay::PrepareReplayCheckpoints()
{
  if(m_CheckpointPrepared || m_CheckpointPreparingFailed)
    return m_CheckpointPrepared;
  auto reject = [this](const rdcstr &reason) {
    m_CheckpointStats.reason = reason;
    m_CheckpointStats.rejected++;
    m_CheckpointPreparingFailed = true;
    return false;
  };
  GLCheckpointMetadataBudget metadata(m_CheckpointConfig.byteBudget);
  const uint64_t recordBytes = 2 * sizeof(ReplayCheckpoint::Resource) + sizeof(GLResource) +
                               sizeof(TextureDescription) + sizeof(GLCheckpointBufferVersion) +
                               sizeof(GLCheckpointTextureVersion) + 512;
  if(!metadata.Add(1, sizeof(ReplayCheckpoint) + 4096) ||
     !metadata.Add(m_Resources.size(), recordBytes) ||
     !metadata.Add(m_pDriver->m_Textures.size(), recordBytes) || !metadata.Add(5, recordBytes))
    return reject("OpenGL checkpoint metadata exceeds its bounded allocation budget");
  if(m_Proxy || !HasExt[ARB_copy_image] || !GL.glCopyImageSubData ||
     VendorCheck[VendorCheck_Qualcomm_avoid_glCopyImageSubData])
    return reject(
        "OpenGL checkpoint resource copies require a reliable local copy-image implementation");
  SDFile *file = GetStructuredFile();
  if(!file)
    return reject("OpenGL checkpoint safety scan requires structured capture chunks");
  bool frame = false;
  bool completeContentEvidence = true;
  bool completeBufferEvidence = true;
  uint32_t contextChanges = 0;
  for(const SDChunk *chunk : file->chunks)
  {
    const rdcstr name = m_pDriver->GetChunkName(chunk->metadata.chunkID);
    if(GLCheckpointObjectRangeUnsupported(name, *chunk))
      return reject("OpenGL capture exceeds checkpoint attachment/VAO state capacity: " + name);
    if(chunk->metadata.chunkID == (uint32_t)SystemChunk::CaptureBegin)
      frame = true;
    if(!frame)
      continue;
    const GLChunk type = GLChunk(chunk->metadata.chunkID);
    if(type == GLChunk::MakeContextCurrent || type == GLChunk::ContextConfiguration)
    {
      if(++contextChanges > 1)
        return reject("OpenGL checkpoints currently require one capture context");
      continue;
    }
    if(type == GLChunk::ImplicitThreadSwitch)
      return reject("OpenGL checkpoint cannot resume an implicit context/thread switch");
    if(GLCheckpointUnsupportedFrameCall(name))
      return reject("OpenGL checkpoint frame contains unsupported mutable/hidden state: " + name);
    if(chunk->metadata.chunkID >= (uint32_t)SystemChunk::FirstDriverChunk &&
       !GLCheckpointContentEvidenceComplete(type))
      completeContentEvidence = false;
    if(chunk->metadata.chunkID >= (uint32_t)SystemChunk::FirstDriverChunk &&
       !GLPatchInitialBufferEvidenceComplete(type))
      completeBufferEvidence = false;
  }
  if(!frame)
    return reject("OpenGL checkpoint safety scan could not locate the frame boundary");
  GLResourceManager *manager = m_pDriver->GetResourceManager();
  for(const ResourceDescription &description : m_Resources)
  {
    const GLResource res = manager->GetResource(description.resourceId, true);
    if(res.Namespace == eResExternalMemory || res.Namespace == eResExternalSemaphore ||
       res.Namespace == eResQuery || res.Namespace == eResSync || res.Namespace == eResFeedback)
      return reject("OpenGL checkpoint contains query, feedback or external object state");
  }
  for(const auto &entry : m_pDriver->m_Textures)
  {
    if(ResourceIDGen::IsReplayOnlyID(entry.first) &&
       !(entry.second.creationFlags & TextureCategory::SwapBuffer))
      continue;
    const WrappedOpenGL::TextureData &tex = entry.second;
    if(tex.internalFormat == eGL_DEPTH32F_STENCIL8 && VendorCheck[VendorCheck_NV_avoid_D32S8_copy])
      return reject("OpenGL checkpoint cannot safely copy this driver's D32F_S8 texture");
    if(IsCompressedFormat(tex.internalFormat) &&
       (IsGLES || VendorCheck[VendorCheck_AMD_copy_compressed_tinymips] ||
        (tex.curType == eGL_TEXTURE_CUBE_MAP && VendorCheck[VendorCheck_AMD_copy_compressed_cubemaps])))
      return reject(
          "OpenGL compressed checkpoint resource needs an unsupported driver copy workaround");
  }
  PrepareCheckpointStaticResources(
      completeBufferEvidence && m_pDriver->m_ReplayCheckpointBufferWrites.complete &&
          Process::GetEnvVariable("RENDERDOC_GL_DISABLE_STATIC_CHECKPOINT_SHARING") != "1",
      completeContentEvidence &&
          Process::GetEnvVariable("RENDERDOC_GL_DISABLE_STATIC_CHECKPOINT_SHARING") != "1");
  m_CheckpointPrepared = true;
  m_CheckpointStats.reason.clear();
  return true;
}

void GLReplay::PrepareCheckpointStaticResources(bool completeBufferEvidence,
                                                bool completeTextureEvidence)
{
  m_CheckpointStaticResources.clear();
  if(!completeBufferEvidence && !completeTextureEvidence)
    return;
  GLResourceManager *manager = m_pDriver->GetResourceManager();
  std::set<ResourceId> attachedTextures, aliasedBuffers, editedBuffers;
  bool textureAliases = false, missingInitialAttachments = false;
  for(const ReplayBufferPatch &patch : m_pDriver->m_ReplayBufferPatches)
    editedBuffers.insert(patch.buffer);
  for(const auto &entry : m_pDriver->m_Textures)
  {
    textureAliases |= entry.second.view;
    if(entry.second.curType == eGL_TEXTURE_BUFFER)
    {
      const GLResource buffer = manager->GetInitialContents(entry.first).tex.texBuffer;
      if(buffer.name)
        aliasedBuffers.insert(manager->GetID(buffer));
    }
  }
  // An FBO may be established before CaptureBegin and only rebound in the captured frame.
  // Its initial attachments must not become static merely because no attachment call appears.
  for(const ResourceDescription &description : m_Resources)
  {
    const GLResource live = manager->GetResource(description.resourceId, true);
    if(live.Namespace == eResFramebuffer)
    {
      const GLInitialContents initial = manager->GetInitialContents(description.resourceId);
      if(initial.fbo.valid)
        for(const FramebufferAttachmentData &attachment : initial.fbo.Attachments)
          if(attachment.obj.name)
            attachedTextures.insert(manager->GetID(attachment.obj));
      if(!initial.fbo.valid)
        missingInitialAttachments = true;
    }
  }
  for(const ResourceDescription &description : m_Resources)
  {
    const GLResource live = manager->GetResource(description.resourceId, true);
    const ResourceId id = manager->GetID(live);
    if(live.Namespace == eResBuffer)
    {
      auto info = m_pDriver->m_Buffers.find(id);
      if(info != m_pDriver->m_Buffers.end() &&
         GLCheckpointShareOriginalContents(
             completeBufferEvidence,
             info->second.replayPatchUnsafe ||
                 bool(info->second.creationFlags & BufferCategory::ReadWrite) ||
                 m_pDriver->m_ReplayCheckpointBufferWrites.buffers.count(id) != 0,
             aliasedBuffers.count(id) != 0, editedBuffers.count(description.resourceId) != 0))
        m_CheckpointStaticResources.insert(id);
    }
    else if(live.Namespace == eResTexture)
    {
      auto info = m_pDriver->m_Textures.find(id);
      if(info != m_pDriver->m_Textures.end() &&
         GLCheckpointShareOriginalContents(
             completeTextureEvidence,
             bool(info->second.creationFlags &
                  (TextureCategory::ColorTarget | TextureCategory::DepthTarget |
                   TextureCategory::ShaderReadWrite | TextureCategory::SwapBuffer)) ||
                 attachedTextures.count(id) != 0,
             textureAliases || missingInitialAttachments || info->second.curType == eGL_TEXTURE_BUFFER,
             false))
        m_CheckpointStaticResources.insert(id);
    }
  }
}

bool GLReplay::SaveReplayCheckpoint(uint32_t eventId)
{
  if(m_ReplayCheckpointPrefixOnly && !m_pDriver->m_ReplayBufferPatches.empty() &&
     (m_ReplayPatchAnchorEvent == 0 || eventId != m_ReplayPatchAnchorEvent))
    return false;
  if(m_ReplayCheckpointBypass || !m_CheckpointStats.enabled ||
     m_Checkpoints.find(eventId) != m_Checkpoints.end() || m_pDriver->m_ActiveConditional ||
     m_pDriver->m_ActiveFeedback || m_pDriver->m_FetchCounters)
    return false;
  for(const auto &query : m_pDriver->m_ActiveQueries)
    for(bool active : query)
      if(active)
        return false;
  const bool checkpointTiming = Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1";
  PerformanceTimer checkpointTimer;
  GLResourceManager *manager = m_pDriver->GetResourceManager();
  GLCheckpointMetadataBudget metadata(m_CheckpointConfig.byteBudget);
  const uint64_t perResourceMetadata =
      2 * sizeof(ReplayCheckpoint::Resource) + sizeof(GLResource) + sizeof(TextureDescription) +
      sizeof(GLCheckpointBufferVersion) + sizeof(GLCheckpointTextureVersion) + 512;
  // Cover vector growth, deduplication/accounting nodes, and GetTextures' temporary array before
  // constructing them. The texture map is an upper bound on replay-created attachments too.
  if(!metadata.Add(1, sizeof(ReplayCheckpoint) + 4096) ||
     !metadata.Add(m_Resources.size(), perResourceMetadata) ||
     !metadata.Add(m_pDriver->m_Textures.size(), perResourceMetadata) ||
     !metadata.Add(5, perResourceMetadata))
  {
    m_CheckpointStats.rejected++;
    m_CheckpointStats.reason = "OpenGL checkpoint metadata exceeds its bounded allocation budget";
    return false;
  }
  if(!PrepareReplayCheckpoints())
    return false;
  const bool textureSharing = m_CheckpointTextureReplayManaged && PrepareCheckpointTextureSharing();
  uint64_t textureCopyBytes = 0, textureSharedBytes = 0;
  std::unique_ptr<ReplayCheckpoint> checkpoint(new ReplayCheckpoint());
  checkpoint->eventId = eventId;
  checkpoint->state.FetchState(m_pDriver);
  checkpoint->topology = m_pDriver->m_LastTopology;
  checkpoint->indexWidth = m_pDriver->m_LastIndexWidth;
  checkpoint->defaultFBO = m_pDriver->m_CurrentDefaultFBO;
  // GLRenderState's default-FBO fields describe real FBO 0. Replay uses an emulated FBO, whose
  // read/draw routing must be retained separately, including holes in glDrawBuffers.
  if(checkpoint->defaultFBO)
    manager->ContextPrepare_InitialState(
        FramebufferRes(m_pDriver->GetCtx(), checkpoint->defaultFBO), &checkpoint->defaultFBOState);
  checkpoint->pack.Fetch(true);
  GL.glGetIntegerv(eGL_RENDERBUFFER_BINDING, &checkpoint->renderbufferBinding);
  checkpoint->bytes = sizeof(ReplayCheckpoint);
  std::set<GLResource> added;
  auto add = [&](ResourceId id, const GLResource &live) {
    if(!live.name || !added.insert(live).second)
      return;
    if(live.Namespace != eResBuffer && live.Namespace != eResTexture &&
       live.Namespace != eResRenderbuffer && live.Namespace != eResProgram &&
       live.Namespace != eResVertexArray && live.Namespace != eResFramebuffer &&
       live.Namespace != eResSampler && live.Namespace != eResProgramPipe)
      return;
    if(live.Namespace == eResTexture)
    {
      auto details = m_pDriver->m_Textures.find(manager->GetID(live));
      if(details != m_pDriver->m_Textures.end() && details->second.internalFormat == eGL_NONE)
        return;
    }
    ReplayCheckpoint::Resource resource;
    resource.id = id;
    resource.live = live;
    resource.sharedContents = m_CheckpointStaticResources.count(manager->GetID(live)) != 0;
    checkpoint->resources.push_back(resource);
  };
  for(const ResourceDescription &description : m_Resources)
    add(description.resourceId, manager->GetResource(description.resourceId, true));
  // Replay-created default framebuffer attachments are live capture data too.
  for(const TextureDescription &tex : GetTextures())
    add(tex.resourceId, manager->GetResource(tex.resourceId, true));
  add(manager->GetID(checkpoint->state.VAO), checkpoint->state.VAO);
  add(manager->GetID(checkpoint->state.DrawFBO), checkpoint->state.DrawFBO);
  add(manager->GetID(checkpoint->state.ReadFBO), checkpoint->state.ReadFBO);
  if(m_pDriver->m_Global_VAO0)
  {
    GLResource vao = VertexArrayRes(m_pDriver->GetCtx(), m_pDriver->m_Global_VAO0);
    add(manager->GetID(vao), vao);
  }
  if(checkpoint->defaultFBO)
  {
    GLResource fbo = FramebufferRes(m_pDriver->GetCtx(), checkpoint->defaultFBO);
    add(manager->GetID(fbo), fbo);
  }
  auto reject = [&](const rdcstr &reason) {
    ResetCheckpointBufferLineage();
    ResetCheckpointTextureLineage();
    ApplyCheckpointBindings(*checkpoint);
    m_CheckpointStats.rejected++;
    m_CheckpointStats.reason = reason;
    return false;
  };
  // Preflight before GPU allocations. Proven static contents need no copy. Mutable buffer
  // versions can be shared only along a successful native restore/save lineage. Texture content
  // versions are separate from per-checkpoint sampler/object state.
  for(ReplayCheckpoint::Resource &res : checkpoint->resources)
  {
    if(res.live.Namespace == eResBuffer)
    {
      GLint size = 0;
      GLint mapped = 0;
      GL.glGetNamedBufferParameterivEXT(res.live.name, eGL_BUFFER_SIZE, &size);
      GL.glGetNamedBufferParameterivEXT(res.live.name, eGL_BUFFER_MAPPED, &mapped);
      if(mapped || size < 0 || uint64_t(size) > 0xffffffffULL)
        return reject("OpenGL checkpoint contains a mapped or oversized buffer");
      res.bytes = RDCMAX(uint64_t(4), uint64_t(size));
      if(!res.sharedContents)
      {
        auto version = m_CheckpointBufferVersions.find(manager->GetID(res.live));
        if(version != m_CheckpointBufferVersions.end())
          res.bufferVersion = GLCheckpointReusableBufferVersion(version->second, res.bytes);
        if(!res.bufferVersion)
        {
          res.bufferVersion = std::make_shared<GLCheckpointBufferVersion>();
          res.bufferVersion->bytes = res.bytes;
        }
      }
    }
    else if(res.live.Namespace == eResTexture || res.live.Namespace == eResRenderbuffer)
    {
      TextureDescription tex = GetTexture(manager->GetID(res.live));
      res.bytes = tex.byteSize;
      res.width = tex.width;
      res.height = tex.height;
      res.renderbuffer = res.live.Namespace == eResRenderbuffer;
      auto details = m_pDriver->m_Textures.find(manager->GetID(res.live));
      if(details == m_pDriver->m_Textures.end())
        return reject("OpenGL checkpoint texture metadata is unavailable");
      if(details->second.view || details->second.curType == eGL_TEXTURE_BUFFER)
        res.bytes =
            0;    // Contents are owned by another buffer/texture, object state is saved below.
      if(details->second.internalFormat != eGL_NONE && !details->second.view &&
         details->second.curType != eGL_TEXTURE_BUFFER && !res.bytes)
        return reject("OpenGL checkpoint texture byte size is unavailable");
      if(textureSharing && !res.sharedContents && res.bytes)
      {
        auto previous = m_CheckpointTextureVersions.find(manager->GetID(res.live));
        if(previous != m_CheckpointTextureVersions.end())
          res.textureVersion = previous->second.lock();
        if(res.textureVersion && !GLCheckpointTextureShapeMatches(
                                     *res.textureVersion, res.live, res.bytes,
                                     details->second.curType, details->second.internalFormat, tex))
          res.textureVersion.reset();
        if(res.textureVersion)
          textureSharedBytes += res.bytes;
        else
        {
          res.textureVersion = std::make_shared<GLCheckpointTextureVersion>();
          res.textureVersion->source = res.live;
          res.textureVersion->bytes = res.bytes;
          res.textureVersion->target = details->second.curType;
          res.textureVersion->format = details->second.internalFormat;
          res.textureVersion->width = tex.width;
          res.textureVersion->height = tex.height;
          res.textureVersion->depth = tex.depth;
          res.textureVersion->mips = tex.mips;
          res.textureVersion->samples = tex.msSamp;
          textureCopyBytes += res.bytes;
        }
      }
      else if(!res.sharedContents)
        textureCopyBytes += res.bytes;
    }
    else if(res.live.Namespace == eResProgram)
    {
      GLint linked = 0;
      GL.glGetProgramiv(res.live.name, eGL_LINK_STATUS, &linked);
      if(linked)
      {
        PerStageReflections stages;
        m_pDriver->FillReflectionArray(manager->GetID(res.live), stages);
        if(!GLCheckpointProgramMetadata(metadata, stages, res.live.name))
          return reject("OpenGL checkpoint uniform metadata exceeds its bounded allocation budget");
        WriteSerialiser ser(new StreamWriter(4096), Ownership::Stream);
        SerialiseProgramUniforms(ser, CaptureState::ActiveCapturing, stages, res.live.name, NULL);
        if(ser.IsErrored() || ser.GetWriter()->GetOffset() > metadata.limit)
          return reject(
              "OpenGL checkpoint uniform serialization exceeded its bounded allocation budget");
        res.uniforms.assign(ser.GetWriter()->GetData(), (size_t)ser.GetWriter()->GetOffset());
        res.bytes = res.uniforms.size();
      }
    }
    else if(res.live.Namespace == eResProgramPipe)
    {
      GL.glGetProgramPipelineiv(res.live.name, eGL_ACTIVE_PROGRAM, &res.activeProgram);
    }
    else if(res.live.Namespace == eResFramebuffer)
    {
      GLint maxAttachments = 0;
      GL.glGetIntegerv(eGL_MAX_COLOR_ATTACHMENTS, &maxAttachments);
      for(GLint attachment = 8; attachment < maxAttachments; attachment++)
      {
        GLint object = 0;
        GL.glGetNamedFramebufferAttachmentParameterivEXT(
            res.live.name, GLenum(eGL_COLOR_ATTACHMENT0 + attachment),
            eGL_FRAMEBUFFER_ATTACHMENT_OBJECT_NAME, &object);
        if(object)
          return reject("OpenGL checkpoint FBO binds a colour attachment beyond slot 7");
      }
      GLint maxDraws = 0;
      GL.glGetIntegerv(eGL_MAX_DRAW_BUFFERS, &maxDraws);
      if(maxAttachments > 8 || maxDraws > 8)
      {
        GL.glBindFramebuffer(eGL_DRAW_FRAMEBUFFER, res.live.name);
        GL.glBindFramebuffer(eGL_READ_FRAMEBUFFER, res.live.name);
        for(GLint draw = 0; draw < maxDraws; draw++)
        {
          GLint route = 0;
          GL.glGetIntegerv(GLenum(eGL_DRAW_BUFFER0 + draw), &route);
          if((draw >= 8 && route != GLint(eGL_NONE)) ||
             (route >= GLint(eGL_COLOR_ATTACHMENT0) + 8 && route < GLint(eGL_COLOR_ATTACHMENT0) + 32))
            return reject("OpenGL checkpoint FBO routes a colour output beyond saved slots");
        }
        GLint route = 0;
        GL.glGetIntegerv(eGL_READ_BUFFER, &route);
        if(route >= GLint(eGL_COLOR_ATTACHMENT0) + 8 && route < GLint(eGL_COLOR_ATTACHMENT0) + 32)
          return reject("OpenGL checkpoint FBO reads a colour attachment beyond slot 7");
      }
    }
    else if(res.live.Namespace == eResVertexArray)
    {
      GL.glBindVertexArray(res.live.name);
      GLint maxAttribs = 0;
      GL.glGetIntegerv(eGL_MAX_VERTEX_ATTRIBS, &maxAttribs);
      for(GLuint attr = 0; attr < GLuint(maxAttribs); attr++)
      {
        if(attr >= 16)
        {
          GLint enabled = 0, buffer = 0;
          GL.glGetVertexAttribiv(attr, eGL_VERTEX_ATTRIB_ARRAY_ENABLED, &enabled);
          GL.glGetVertexAttribiv(attr, eGL_VERTEX_ATTRIB_ARRAY_BUFFER_BINDING, &buffer);
          if(enabled || buffer)
            return reject("OpenGL checkpoint VAO uses a vertex attribute beyond slot 15");
        }
        if(attr < 16 && HasExt[ARB_vertex_attrib_64bit])
        {
          GLint longFormat = 0;
          GL.glGetVertexAttribiv(attr, eGL_VERTEX_ATTRIB_ARRAY_LONG, &longFormat);
          if(longFormat)
            return reject("OpenGL checkpoint VAO contains unsupported 64-bit vertex attributes");
        }
        if(attr < 16 && HasExt[ARB_vertex_attrib_binding])
        {
          GLint binding = 0;
          GL.glGetVertexAttribiv(attr, eGL_VERTEX_ATTRIB_BINDING, &binding);
          if(binding >= 16)
            return reject("OpenGL checkpoint VAO routes an attribute beyond vertex binding 15");
        }
      }
      if(HasExt[ARB_vertex_attrib_binding])
      {
        GLint maxBindings = 0;
        GL.glGetIntegerv(eGL_MAX_VERTEX_ATTRIB_BINDINGS, &maxBindings);
        for(GLuint binding = 16; binding < GLuint(maxBindings); binding++)
        {
          GLint buffer = 0;
          GL.glGetIntegeri_v(eGL_VERTEX_BINDING_BUFFER, binding, &buffer);
          if(buffer)
            return reject("OpenGL checkpoint VAO binds a vertex buffer beyond slot 15");
        }
      }
    }
    if(res.sharedContents)
    {
      ++checkpoint->staticResources;
      checkpoint->staticBytes += res.bytes;
    }
    const uint64_t contentBytes =
        res.bufferVersion || res.textureVersion || res.sharedContents ? 0 : res.bytes;
    if(checkpoint->bytes > m_CheckpointConfig.byteBudget ||
       sizeof(ReplayCheckpoint::Resource) > m_CheckpointConfig.byteBudget - checkpoint->bytes ||
       contentBytes >
           m_CheckpointConfig.byteBudget - checkpoint->bytes - sizeof(ReplayCheckpoint::Resource))
      return reject("OpenGL checkpoint working set exceeds its byte budget");
    checkpoint->bytes += sizeof(ReplayCheckpoint::Resource) + contentBytes;
  }
  ApplyCheckpointBindings(*checkpoint);
  // Include the candidate's strong references while evicting. A reused version can survive its
  // old checkpoint being evicted, so subtracting that checkpoint's logical bytes is incorrect.
  uint64_t sharedBytes = 0;
  while(!m_Checkpoints.empty() &&
        (m_Checkpoints.size() >= m_CheckpointConfig.maxCheckpoints ||
         CheckpointStorageBytes(checkpoint.get(), sharedBytes) > m_CheckpointConfig.byteBudget))
  {
    const size_t count = m_Checkpoints.size();
    EvictOldestCheckpoint();
    if(count == m_Checkpoints.size())
      return reject("OpenGL checkpoint budget is reserved for the shared-buffer prefix anchor");
  }
  if(CheckpointStorageBytes(checkpoint.get(), sharedBytes) > m_CheckpointConfig.byteBudget)
    return reject("OpenGL checkpoint working set exceeds its byte budget");
  for(uint32_t error = 0; error < 8 && GL.glGetError() != eGL_NONE; error++)
  {
  }
  if(HasExt[ARB_shader_image_load_store])
    GL.glMemoryBarrier(GLbitfield(eGL_ALL_BARRIER_BITS));
  const double checkpointPrepareMS = checkpointTiming ? checkpointTimer.GetMilliseconds() : 0.0;
  for(ReplayCheckpoint::Resource &res : checkpoint->resources)
  {
    if(res.sharedContents && res.live.Namespace == eResBuffer)
      continue;
    if(res.bufferVersion)
    {
      if(!res.bufferVersion->initial.resource.name)
        manager->ContextPrepare_InitialState(res.live, &res.bufferVersion->initial);
      if(!res.bufferVersion->initial.resource.name)
        return reject("OpenGL checkpoint buffer copy allocation failed");
    }
    else if(res.renderbuffer && !(res.textureVersion && res.textureVersion->initial.resource.name))
    {
      GLint width = 0, height = 0, format = 0, samples = 0, old = 0;
      GL.glGetIntegerv(eGL_RENDERBUFFER_BINDING, &old);
      GL.glBindRenderbuffer(eGL_RENDERBUFFER, res.live.name);
      GL.glGetRenderbufferParameteriv(eGL_RENDERBUFFER, eGL_RENDERBUFFER_WIDTH, &width);
      GL.glGetRenderbufferParameteriv(eGL_RENDERBUFFER, eGL_RENDERBUFFER_HEIGHT, &height);
      GL.glGetRenderbufferParameteriv(eGL_RENDERBUFFER, eGL_RENDERBUFFER_INTERNAL_FORMAT, &format);
      GL.glGetRenderbufferParameteriv(eGL_RENDERBUFFER, eGL_RENDERBUFFER_SAMPLES, &samples);
      GLuint copy = 0;
      GL.glGenRenderbuffers(1, &copy);
      GL.glBindRenderbuffer(eGL_RENDERBUFFER, copy);
      GL.glRenderbufferStorageMultisample(eGL_RENDERBUFFER, samples, GLenum(format), width, height);
      GL.glBindRenderbuffer(eGL_RENDERBUFFER, old);
      res.initial.resource = GLResource(res.live.ContextShareGroup, eResRenderbuffer, copy);
      GL.glCopyImageSubData(res.live.name, eGL_RENDERBUFFER, 0, 0, 0, 0, copy, eGL_RENDERBUFFER, 0,
                            0, 0, 0, width, height, 1);
    }
    else if(res.live.Namespace != eResProgram && !res.renderbuffer)
    {
      manager->ContextPrepare_InitialState(
          res.live, &res.initial,
          !res.sharedContents && !(res.textureVersion && res.textureVersion->initial.resource.name));
    }
    if(res.textureVersion && !res.textureVersion->initial.resource.name)
    {
      res.textureVersion->initial = res.initial;
      res.initial.resource = GLResource(MakeNullResource);
      if(!res.textureVersion->initial.resource.name)
        return reject("OpenGL checkpoint texture copy allocation failed");
    }
    if(GL.glGetError() != eGL_NONE)
      return reject("OpenGL checkpoint resource copy or state capture failed");
  }
  const double checkpointCopyMS = checkpointTiming ? checkpointTimer.GetMilliseconds() : 0.0;
  ApplyCheckpointBindings(*checkpoint);
  if(GL.glGetError() != eGL_NONE)
    return reject("OpenGL checkpoint state reapplication failed");
  ResetCheckpointBufferLineage();
  ResetCheckpointTextureLineage();
  for(const ReplayCheckpoint::Resource &res : checkpoint->resources)
  {
    if(res.bufferVersion)
      m_CheckpointBufferVersions[manager->GetID(res.live)] = res.bufferVersion;
    if(res.textureVersion)
      m_CheckpointTextureVersions[manager->GetID(res.live)] = res.textureVersion;
  }
  checkpoint->lastUsed = ++m_CheckpointUseSerial;
  const uint32_t staticResources = checkpoint->staticResources;
  const uint64_t staticBytes = checkpoint->staticBytes;
  m_Checkpoints[eventId] = std::move(checkpoint);
  m_CheckpointStats.builds++;
  m_CheckpointStats.lastSaveEvent = eventId;
  m_CheckpointStats.reason.clear();
  UpdateCheckpointAccounting();
  if(checkpointTiming)
  {
    const double totalMS = checkpointTimer.GetMilliseconds();
    const rdcstr line = StringFormat::Fmt(
        "api=OpenGLCheckpoint op=save event=%u attempt=successful prepare_ms=%.3f "
        "resource_copy_ms=%.3f "
        "bind_finalize_ms=%.3f total_ms=%.3f allocatedBytes=%llu sharedBytes=%llu "
        "sharedStaticResources=%u sharedStaticBytes=%llu textureVersions=%u "
        "textureCopyBytes=%llu textureSharedBytes=%llu",
        eventId, checkpointPrepareMS, checkpointCopyMS - checkpointPrepareMS,
        totalMS - checkpointCopyMS, totalMS, (unsigned long long)m_CheckpointStats.allocatedBytes,
        (unsigned long long)m_CheckpointStats.sharedBytes, staticResources,
        (unsigned long long)staticBytes, textureSharing ? 1U : 0U,
        (unsigned long long)textureCopyBytes, (unsigned long long)textureSharedBytes);
    RDCLOG("[EIDTiming] %s", line.c_str());
    AppendReplayTimingLog(line);
  }
  return true;
}

void GLReplay::ApplyCheckpointBindings(ReplayCheckpoint &checkpoint)
{
  m_pDriver->m_CurrentDefaultFBO = checkpoint.defaultFBO;
  checkpoint.state.ApplyState(m_pDriver);
  if(checkpoint.defaultFBO)
    m_pDriver->GetResourceManager()->Apply_InitialState(
        FramebufferRes(m_pDriver->GetCtx(), checkpoint.defaultFBO), checkpoint.defaultFBOState);
  checkpoint.pack.Apply(true);
  GL.glBindRenderbuffer(eGL_RENDERBUFFER, checkpoint.renderbufferBinding);
}

bool GLReplay::RestoreReplayCheckpoint(ReplayCheckpoint &checkpoint)
{
  ResetCheckpointBufferLineage();
  bool completed = false;
  struct TextureRestoreFailure
  {
    bool &completed;
    std::map<ResourceId, std::weak_ptr<GLCheckpointTextureVersion>> &versions;
    ~TextureRestoreFailure()
    {
      if(!completed)
        versions.clear();
    }
  } textureFailure = {completed, m_CheckpointTextureVersions};
  if(m_ReplayCheckpointBypass || m_pDriver->m_FetchCounters || m_pDriver->m_ActiveFeedback ||
     m_pDriver->m_ActiveConditional)
    return false;
  const bool checkpointTiming = Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1";
  PerformanceTimer checkpointTimer;
  GLResourceManager *manager = m_pDriver->GetResourceManager();
  uint64_t textureCopyBytes = 0, textureResidentBytes = 0;
  for(const ReplayCheckpoint::Resource &res : checkpoint.resources)
    if(res.id != ResourceId() && manager->GetResource(res.id, true) != res.live)
      return false;
  const double checkpointValidateMS = checkpointTiming ? checkpointTimer.GetMilliseconds() : 0.0;
  for(uint32_t error = 0; error < 8 && GL.glGetError() != eGL_NONE; error++)
  {
  }
  if(HasExt[ARB_shader_image_load_store])
    GL.glMemoryBarrier(GLbitfield(eGL_ALL_BARRIER_BITS));
  for(ReplayCheckpoint::Resource &res : checkpoint.resources)
  {
    if(res.sharedContents && res.live.Namespace == eResBuffer)
      continue;
    bool residentTexture = false;
    if(res.textureVersion && m_CheckpointTextureReplayManaged && PrepareCheckpointTextureSharing())
    {
      const auto current = m_CheckpointTextureVersions.find(manager->GetID(res.live));
      residentTexture = current != m_CheckpointTextureVersions.end() &&
                        GLCheckpointTextureResident(current->second, res.textureVersion);
    }
    if(residentTexture)
      textureResidentBytes += res.bytes;
    else if((res.live.Namespace == eResTexture || res.renderbuffer) && !res.sharedContents)
      textureCopyBytes += res.bytes;
    if(res.textureVersion && res.renderbuffer)
    {
      if(!residentTexture)
        GL.glCopyImageSubData(res.textureVersion->initial.resource.name, eGL_RENDERBUFFER, 0, 0, 0, 0,
                              res.live.name, eGL_RENDERBUFFER, 0, 0, 0, 0, res.width, res.height, 1);
    }
    else if(res.textureVersion)
    {
      // Restore the checkpoint's own object/sampler state even when its pixels already reside
      // in the working texture. The shared version owns only content, never another CP's state.
      GLInitialContents apply = GLCheckpointTextureRestoreState(
          res.initial, res.textureVersion->initial.resource, residentTexture);
      manager->Apply_InitialState(res.live, apply);
    }
    else if(res.renderbuffer)
      GL.glCopyImageSubData(res.initial.resource.name, eGL_RENDERBUFFER, 0, 0, 0, 0, res.live.name,
                            eGL_RENDERBUFFER, 0, 0, 0, 0, res.width, res.height, 1);
    else if(res.live.Namespace == eResProgram)
    {
      if(!res.uniforms.empty())
      {
        PerStageReflections stages;
        m_pDriver->FillReflectionArray(manager->GetID(res.live), stages);
        ReadSerialiser ser(new StreamReader(res.uniforms), Ownership::Stream);
        SerialiseProgramUniforms(ser, CaptureState::ActiveReplaying, stages, res.live.name, NULL);
        if(ser.IsErrored())
          return false;
      }
    }
    else
    {
      // The capture initial-state helper intentionally leaves unbound pipeline stages alone;
      // a checkpoint must restore null stages as well, because a later replay may bind them.
      if(res.live.Namespace == eResProgramPipe)
        m_pDriver->glUseProgramStages(res.live.name, GLbitfield(eGL_ALL_SHADER_BITS), 0);
      manager->Apply_InitialState(res.live,
                                  res.bufferVersion ? res.bufferVersion->initial : res.initial);
      if(res.live.Namespace == eResProgramPipe)
        GL.glActiveShaderProgram(res.live.name, res.activeProgram);
    }
    if(GL.glGetError() != eGL_NONE)
      return false;
  }
  const double checkpointCopyMS = checkpointTiming ? checkpointTimer.GetMilliseconds() : 0.0;
  ApplyCheckpointBindings(checkpoint);
  m_pDriver->m_LastTopology = checkpoint.topology;
  m_pDriver->m_LastIndexWidth = checkpoint.indexWidth;
  m_pDriver->m_CurEventID = checkpoint.eventId;
  m_pDriver->m_CurEvents.clear();
  m_pDriver->m_WasActiveFeedback = false;
  checkpoint.lastUsed = ++m_CheckpointUseSerial;
  const bool success = GL.glGetError() == eGL_NONE;
  if(success)
    for(const ReplayCheckpoint::Resource &res : checkpoint.resources)
    {
      if(res.bufferVersion)
        m_CheckpointBufferVersions[manager->GetID(res.live)] = res.bufferVersion;
      if(res.textureVersion && m_CheckpointTextureReplayManaged)
        m_CheckpointTextureVersions[manager->GetID(res.live)] = res.textureVersion;
      else
        m_CheckpointTextureVersions.erase(manager->GetID(res.live));
    }
  completed = success;
  if(checkpointTiming)
  {
    const double totalMS = checkpointTimer.GetMilliseconds();
    const rdcstr line = StringFormat::Fmt(
        "api=OpenGLCheckpoint op=restore event=%u success=%u validate_ms=%.3f "
        "resource_restore_ms=%.3f "
        "bind_finalize_ms=%.3f total_ms=%.3f sharedStaticResources=%u sharedStaticBytes=%llu "
        "textureCopyBytes=%llu textureResidentBytes=%llu",
        checkpoint.eventId, success ? 1U : 0U, checkpointValidateMS,
        checkpointCopyMS - checkpointValidateMS, totalMS - checkpointCopyMS, totalMS,
        checkpoint.staticResources, (unsigned long long)checkpoint.staticBytes,
        (unsigned long long)textureCopyBytes, (unsigned long long)textureResidentBytes);
    RDCLOG("[EIDTiming] %s", line.c_str());
    AppendReplayTimingLog(line);
  }
  return success;
}

GLReplay::GLReplay(WrappedOpenGL *d)
{
  m_pDriver = d;

  RenderDoc::Inst().RegisterMemoryRegion(this, sizeof(GLReplay));

  m_Proxy = false;

  m_Degraded = false;

  RDCEraseEl(m_ReplayCtx);
  m_DebugCtx = NULL;

  m_DebugID = 0;

  m_OutputWindowID = 1;

  RDCEraseEl(m_GetTexturePrevData);
  RDCEraseEl(m_DriverInfo);
}

void GLReplay::Shutdown()
{
  ClearEventShaderOverrides();
  ReleaseEventShaderPrograms();
  ReleaseEventShaderFloatPreviews();
  InvalidateLiveForward();
  MakeCurrentReplayContext(&m_ReplayCtx);
  ResetCheckpointBufferLineage();
  if(m_pDriver->m_ReplayPatchUpload)
    GL.glDeleteBuffers(1, &m_pDriver->m_ReplayPatchUpload);
  m_pDriver->m_ReplayPatchUpload = 0;
  m_pDriver->m_ReplayBufferPatches.clear();
  m_Checkpoints.clear();
  SAFE_DELETE(m_pAMDCounters);
  SAFE_DELETE(m_pIntelCounters);
  SAFE_DELETE(m_pARMCounters);
#if DISABLED(RDOC_ANDROID) && DISABLED(RDOC_APPLE)
  SAFE_DELETE(m_pNVCounters);
#endif

  rdcarray<ResourceId> textureSnapshots = m_TextureSnapshots;
  FreeTextureSnapshots(textureSnapshots);

  DeleteDebugData();

  DestroyOutputWindow(m_DebugID);

  CloseReplayContext();

  // clean up cached GetTextureData allocations
  for(size_t i = 0; i < ARRAY_COUNT(m_GetTexturePrevData); i++)
  {
    delete[] m_GetTexturePrevData[i];
    m_GetTexturePrevData[i] = NULL;
  }

  delete m_pDriver;
}

RDResult GLReplay::FatalErrorCheck()
{
  return m_pDriver->FatalErrorCheck();
}

IReplayDriver *GLReplay::MakeDummyDriver()
{
  // gather up the shaders we've allocated to pass to the dummy driver
  rdcarray<const ShaderReflection *> shaders;
  for(auto it = m_pDriver->m_Shaders.begin(); it != m_pDriver->m_Shaders.end(); it++)
  {
    shaders.push_back(it->second.StealReflection());
  }

  IReplayDriver *dummy = new DummyDriver(this, shaders, m_pDriver->DetachStructuredFile(),
                                         m_pDriver->DetachAnnotations());

  return dummy;
}

RDResult GLReplay::ReadLogInitialisation(RDCFile *rdc, bool storeStructuredBuffers)
{
  InvalidateLiveForward();
  m_LiveForwardScanned = m_LiveForwardCaptureSafe = false;
  m_LiveForwardReason.clear();
  MakeCurrentReplayContext(&m_ReplayCtx);
  return m_pDriver->ReadLogInitialisation(rdc, storeStructuredBuffers);
}

namespace
{
bool GLLiveForwardOrdinaryRoot(ReplayLogType replayType, bool bypass, bool naturalReference)
{
  return replayType == eReplay_WithoutDraw && !bypass && !naturalReference;
}

bool GLLiveForwardCallSupported(GLChunk type)
{
  // The indexed call must leave all execution state resident. Query/conditional/transform
  // feedback scopes are specifically forbidden: ContextReplayLog closes them at every stop.
  // Object creation, storage replacement, context changes and future commands fail closed.
  if(GLCheckpointClassifyBufferEffect(type) != GLCheckpointBufferEffect::Unknown ||
     type == GLChunk::glIndirectSubCommand)
    return true;
  // These calls update fixed-storage contents or native object/binding state. They replay in
  // full against resident resources. Keep this list separate from checkpoint static sharing:
  // a texture subimage or framebuffer copy is emphatically NOT immutable-content evidence.
  switch(type)
  {
    case GLChunk::glVertexAttribPointer:
    case GLChunk::glVertexAttribPointerARB:
    case GLChunk::glVertexArrayVertexAttribOffsetEXT:
    case GLChunk::glVertexAttribIPointer:
    case GLChunk::glVertexAttribIPointerEXT:
    case GLChunk::glVertexArrayVertexAttribIOffsetEXT:
    case GLChunk::glVertexAttribLPointer:
    case GLChunk::glVertexAttribLPointerEXT:
    case GLChunk::glVertexArrayVertexAttribLOffsetEXT:
    case GLChunk::glVertexAttribBinding:
    case GLChunk::glVertexArrayAttribBinding:
    case GLChunk::glVertexArrayVertexAttribBindingEXT:
    case GLChunk::glVertexAttribFormat:
    case GLChunk::glVertexArrayAttribFormat:
    case GLChunk::glVertexArrayVertexAttribFormatEXT:
    case GLChunk::glVertexAttribIFormat:
    case GLChunk::glVertexArrayAttribIFormat:
    case GLChunk::glVertexArrayVertexAttribIFormatEXT:
    case GLChunk::glVertexAttribLFormat:
    case GLChunk::glVertexArrayAttribLFormat:
    case GLChunk::glVertexArrayVertexAttribLFormatEXT:
    case GLChunk::glVertexAttribDivisor:
    case GLChunk::glVertexAttribDivisorARB:
    case GLChunk::glVertexArrayVertexAttribDivisorEXT:
    case GLChunk::glEnableVertexAttribArray:
    case GLChunk::glEnableVertexAttribArrayARB:
    case GLChunk::glEnableVertexArrayAttrib:
    case GLChunk::glEnableVertexArrayAttribEXT:
    case GLChunk::glDisableVertexAttribArray:
    case GLChunk::glDisableVertexAttribArrayARB:
    case GLChunk::glDisableVertexArrayAttrib:
    case GLChunk::glDisableVertexArrayAttribEXT:
    case GLChunk::glVertexBindingDivisor:
    case GLChunk::glVertexArrayBindingDivisor:
    case GLChunk::glVertexArrayVertexBindingDivisorEXT:
    case GLChunk::glVertexAttrib1d:
    case GLChunk::glVertexAttrib1dARB:
    case GLChunk::glVertexAttrib1dv:
    case GLChunk::glVertexAttrib1dvARB:
    case GLChunk::glVertexAttrib1f:
    case GLChunk::glVertexAttrib1fARB:
    case GLChunk::glVertexAttrib1fv:
    case GLChunk::glVertexAttrib1fvARB:
    case GLChunk::glVertexAttrib1s:
    case GLChunk::glVertexAttrib1sARB:
    case GLChunk::glVertexAttrib1sv:
    case GLChunk::glVertexAttrib1svARB:
    case GLChunk::glVertexAttrib2d:
    case GLChunk::glVertexAttrib2dARB:
    case GLChunk::glVertexAttrib2dv:
    case GLChunk::glVertexAttrib2dvARB:
    case GLChunk::glVertexAttrib2f:
    case GLChunk::glVertexAttrib2fARB:
    case GLChunk::glVertexAttrib2fv:
    case GLChunk::glVertexAttrib2fvARB:
    case GLChunk::glVertexAttrib2s:
    case GLChunk::glVertexAttrib2sARB:
    case GLChunk::glVertexAttrib2sv:
    case GLChunk::glVertexAttrib2svARB:
    case GLChunk::glVertexAttrib3d:
    case GLChunk::glVertexAttrib3dARB:
    case GLChunk::glVertexAttrib3dv:
    case GLChunk::glVertexAttrib3dvARB:
    case GLChunk::glVertexAttrib3f:
    case GLChunk::glVertexAttrib3fARB:
    case GLChunk::glVertexAttrib3fv:
    case GLChunk::glVertexAttrib3fvARB:
    case GLChunk::glVertexAttrib3s:
    case GLChunk::glVertexAttrib3sARB:
    case GLChunk::glVertexAttrib3sv:
    case GLChunk::glVertexAttrib3svARB:
    case GLChunk::glVertexAttrib4d:
    case GLChunk::glVertexAttrib4dARB:
    case GLChunk::glVertexAttrib4dv:
    case GLChunk::glVertexAttrib4dvARB:
    case GLChunk::glVertexAttrib4f:
    case GLChunk::glVertexAttrib4fARB:
    case GLChunk::glVertexAttrib4fv:
    case GLChunk::glVertexAttrib4fvARB:
    case GLChunk::glVertexAttrib4s:
    case GLChunk::glVertexAttrib4sARB:
    case GLChunk::glVertexAttrib4sv:
    case GLChunk::glVertexAttrib4svARB:
    case GLChunk::glVertexAttribI1i:
    case GLChunk::glVertexAttribI1iEXT:
    case GLChunk::glVertexAttribI1iv:
    case GLChunk::glVertexAttribI1ivEXT:
    case GLChunk::glVertexAttribI1ui:
    case GLChunk::glVertexAttribI1uiEXT:
    case GLChunk::glVertexAttribI1uiv:
    case GLChunk::glVertexAttribI1uivEXT:
    case GLChunk::glVertexAttribI2i:
    case GLChunk::glVertexAttribI2iEXT:
    case GLChunk::glVertexAttribI2iv:
    case GLChunk::glVertexAttribI2ivEXT:
    case GLChunk::glVertexAttribI2ui:
    case GLChunk::glVertexAttribI2uiEXT:
    case GLChunk::glVertexAttribI2uiv:
    case GLChunk::glVertexAttribI2uivEXT:
    case GLChunk::glVertexAttribI3i:
    case GLChunk::glVertexAttribI3iEXT:
    case GLChunk::glVertexAttribI3iv:
    case GLChunk::glVertexAttribI3ivEXT:
    case GLChunk::glVertexAttribI3ui:
    case GLChunk::glVertexAttribI3uiEXT:
    case GLChunk::glVertexAttribI3uiv:
    case GLChunk::glVertexAttribI3uivEXT:
    case GLChunk::glVertexAttribI4i:
    case GLChunk::glVertexAttribI4iEXT:
    case GLChunk::glVertexAttribI4iv:
    case GLChunk::glVertexAttribI4ivEXT:
    case GLChunk::glVertexAttribI4ui:
    case GLChunk::glVertexAttribI4uiEXT:
    case GLChunk::glVertexAttribI4uiv:
    case GLChunk::glVertexAttribI4uivEXT:
    case GLChunk::glVertexAttribL1d:
    case GLChunk::glVertexAttribL1dEXT:
    case GLChunk::glVertexAttribL1dv:
    case GLChunk::glVertexAttribL1dvEXT:
    case GLChunk::glVertexAttribL2d:
    case GLChunk::glVertexAttribL2dEXT:
    case GLChunk::glVertexAttribL2dv:
    case GLChunk::glVertexAttribL2dvEXT:
    case GLChunk::glVertexAttribL3d:
    case GLChunk::glVertexAttribL3dEXT:
    case GLChunk::glVertexAttribL3dv:
    case GLChunk::glVertexAttribL3dvEXT:
    case GLChunk::glVertexAttribL4d:
    case GLChunk::glVertexAttribL4dEXT:
    case GLChunk::glVertexAttribL4dv:
    case GLChunk::glVertexAttribL4dvEXT:
    case GLChunk::glClearBufferData:
    case GLChunk::glClearNamedBufferData:
    case GLChunk::glClearNamedBufferDataEXT:
    case GLChunk::glClearBufferSubData:
    case GLChunk::glClearNamedBufferSubData:
    case GLChunk::glClearNamedBufferSubDataEXT:
    case GLChunk::glClearTexImage:
    case GLChunk::glClearTexSubImage:
    case GLChunk::glFramebufferTexture:
    case GLChunk::glFramebufferTextureOES:
    case GLChunk::glFramebufferTextureARB:
    case GLChunk::glFramebufferTextureEXT:
    case GLChunk::glNamedFramebufferTexture:
    case GLChunk::glNamedFramebufferTextureEXT:
    case GLChunk::glFramebufferTexture1D:
    case GLChunk::glFramebufferTexture1DEXT:
    case GLChunk::glNamedFramebufferTexture1DEXT:
    case GLChunk::glFramebufferTexture2D:
    case GLChunk::glFramebufferTexture2DEXT:
    case GLChunk::glNamedFramebufferTexture2DEXT:
    case GLChunk::glFramebufferTexture3D:
    case GLChunk::glFramebufferTexture3DEXT:
    case GLChunk::glFramebufferTexture3DOES:
    case GLChunk::glNamedFramebufferTexture3DEXT:
    case GLChunk::glFramebufferRenderbuffer:
    case GLChunk::glFramebufferRenderbufferEXT:
    case GLChunk::glNamedFramebufferRenderbuffer:
    case GLChunk::glNamedFramebufferRenderbufferEXT:
    case GLChunk::glFramebufferTextureLayer:
    case GLChunk::glFramebufferTextureLayerARB:
    case GLChunk::glFramebufferTextureLayerEXT:
    case GLChunk::glNamedFramebufferTextureLayer:
    case GLChunk::glNamedFramebufferTextureLayerEXT:
    case GLChunk::glBlitFramebuffer:
    case GLChunk::glBlitFramebufferEXT:
    case GLChunk::glDepthRangeIndexed:
    case GLChunk::glDepthRangeArrayv:
    case GLChunk::glSampleMaski:
    case GLChunk::glViewportIndexedf:
    case GLChunk::glViewportIndexedfv:
    case GLChunk::glViewportArrayv:
    case GLChunk::glScissorIndexed:
    case GLChunk::glScissorIndexedv:
    case GLChunk::glScissorArrayv:
    case GLChunk::glCopyImageSubDataEXT:
    case GLChunk::glCopyImageSubDataOES:
    case GLChunk::glCopyImageSubData:
    case GLChunk::glCopyTexSubImage1D:
    case GLChunk::glCopyTextureSubImage1D:
    case GLChunk::glCopyTextureSubImage1DEXT:
    case GLChunk::glCopyTexSubImage2D:
    case GLChunk::glCopyTextureSubImage2D:
    case GLChunk::glCopyTextureSubImage2DEXT:
    case GLChunk::glCopyTexSubImage3D:
    case GLChunk::glCopyTexSubImage3DOES:
    case GLChunk::glCopyTextureSubImage3D:
    case GLChunk::glCopyTextureSubImage3DEXT:
    case GLChunk::glPixelStoref:
    case GLChunk::glPixelStorei:
    case GLChunk::glTexSubImage1D:
    case GLChunk::glTextureSubImage1D:
    case GLChunk::glTextureSubImage1DEXT:
    case GLChunk::glTexSubImage2D:
    case GLChunk::glTextureSubImage2D:
    case GLChunk::glTextureSubImage2DEXT:
    case GLChunk::glTexSubImage3D:
    case GLChunk::glTexSubImage3DOES:
    case GLChunk::glTextureSubImage3D:
    case GLChunk::glTextureSubImage3DEXT:
    case GLChunk::glCompressedTexSubImage1D:
    case GLChunk::glCompressedTexSubImage1DARB:
    case GLChunk::glCompressedTextureSubImage1D:
    case GLChunk::glCompressedTextureSubImage1DEXT:
    case GLChunk::glCompressedTexSubImage2D:
    case GLChunk::glCompressedTexSubImage2DARB:
    case GLChunk::glCompressedTextureSubImage2D:
    case GLChunk::glCompressedTextureSubImage2DEXT:
    case GLChunk::glCompressedTexSubImage3D:
    case GLChunk::glCompressedTexSubImage3DARB:
    case GLChunk::glCompressedTexSubImage3DOES:
    case GLChunk::glCompressedTextureSubImage3D:
    case GLChunk::glCompressedTextureSubImage3DEXT: return true;
    default: return false;
  }
}

bool GLLiveForwardTerminalPresent(const SDFile &file, size_t index)
{
  // A present is excluded only when it is the final driver chunk immediately before the
  // terminating CaptureEnd. The live cursor cannot select a Present action, so this suffix
  // is never part of the continued interval. A mid-frame swap still rejects the capture.
  if(index >= file.chunks.size() || file.chunks.size() - index != 2 ||
     file.chunks[index + 1]->metadata.chunkID != uint32_t(SystemChunk::CaptureEnd))
    return false;
  switch(GLChunk(file.chunks[index]->metadata.chunkID))
  {
    case GLChunk::SwapBuffers:
    case GLChunk::wglSwapBuffers:
    case GLChunk::glXSwapBuffers:
    case GLChunk::CGLFlushDrawable:
    case GLChunk::eglSwapBuffers:
    case GLChunk::eglPostSubBufferNV:
    case GLChunk::eglSwapBuffersWithDamageEXT:
    case GLChunk::eglSwapBuffersWithDamageKHR: return true;
    default: return false;
  }
}

bool GLLiveForwardEventBoundary(const rdcarray<APIEvent> &events, uint32_t eventId)
{
  if(eventId == 0 || uint64_t(eventId) + 1 >= events.size() || events[eventId].eventId != eventId ||
     events[eventId + 1].eventId != eventId + 1 ||
     events[eventId + 1].fileOffset <= events[eventId].fileOffset)
    return false;
  if(eventId > 1 && (events[eventId - 1].eventId != eventId - 1 ||
                     events[eventId - 1].fileOffset >= events[eventId].fileOffset))
    return false;
  return true;
}
}

bool GLReplay::PrepareLiveForward()
{
  if(m_LiveForwardScanned)
    return m_LiveForwardCaptureSafe;
  m_LiveForwardScanned = true;
  m_LiveForwardCaptureSafe = false;
  const auto reject = [this](const rdcstr &reason) {
    m_LiveForwardReason = reason;
    return false;
  };
  if(m_Proxy || m_pDriver->m_ReplayOptions.apiValidation)
    return reject("proxyOrValidation");
  SDFile *file = GetStructuredFile();
  if(!file)
    return reject("missingStructuredFrame");
  bool frame = false, executedCall = false;
  uint32_t setups = 0;
  for(size_t index = 0; index < file->chunks.size(); ++index)
  {
    const SDChunk *chunk = file->chunks[index];
    if(chunk->metadata.chunkID == uint32_t(SystemChunk::CaptureBegin))
    {
      frame = true;
      continue;
    }
    if(!frame || chunk->metadata.chunkID < uint32_t(SystemChunk::FirstDriverChunk))
      continue;
    const GLChunk type = GLChunk(chunk->metadata.chunkID);
    if(GLLiveForwardTerminalPresent(*file, index))
      continue;
    if(type == GLChunk::MakeContextCurrent || type == GLChunk::ContextConfiguration)
    {
      if(executedCall || ++setups > 1)
        return reject("captureContextSwitch");
      continue;
    }
    if(!GLLiveForwardCallSupported(type))
      return reject("unsupportedFrameCall:" +
                    rdcstr(m_pDriver->GetChunkName(chunk->metadata.chunkID)));
    executedCall = true;
  }
  if(!frame || !executedCall)
    return reject("emptyOrMissingFrame");
  // These objects can carry a scope already active in the capture-begin state; checking only
  // calls after CaptureBegin would miss that state even when the replay tail already closed it.
  GLResourceManager *manager = m_pDriver->GetResourceManager();
  for(const ResourceDescription &description : m_Resources)
  {
    const GLResource resource = manager->GetResource(description.resourceId, true);
    if(resource.Namespace == eResQuery || resource.Namespace == eResFeedback ||
       resource.Namespace == eResSync || resource.Namespace == eResExternalMemory ||
       resource.Namespace == eResExternalSemaphore)
      return reject("queryFeedbackOrExternalObject");
  }
  m_LiveForwardCaptureSafe = true;
  m_LiveForwardReason.clear();
  return true;
}

bool GLReplay::LiveForwardEndpoint(uint32_t eventId) const
{
  if(!GLLiveForwardEventBoundary(m_pDriver->m_Events, eventId))
    return false;
  const ActionDescription *action = m_pDriver->GetAction(eventId);
  return action &&
         bool(action->flags & (ActionFlags::Drawcall | ActionFlags::Dispatch | ActionFlags::Clear)) &&
         !(action->flags & ActionFlags::MultiAction) && action->children.empty();
}

void GLReplay::ReplayLog(uint32_t endEventID, ReplayLogType replayType)
{
  MakeCurrentReplayContext(&m_ReplayCtx);
  PerformanceTimer liveTimer;
  const bool liveEnabled = Process::GetEnvVariable("RENDERDOC_GL_ENABLE_LIVE_FORWARD") != "0";
  const uint32_t previousEvent = m_LiveForward.event;
  const bool ordinaryRoot = GLLiveForwardOrdinaryRoot(replayType, m_ReplayCheckpointBypass,
                                                      m_ReplayCheckpointNaturalReference) &&
                            !m_pDriver->m_FetchCounters && !m_pDriver->m_ActiveFeedback &&
                            !m_pDriver->m_WasActiveFeedback && !m_pDriver->m_ActiveConditional;
  const bool pendingTarget = replayType == eReplay_OnlyDraw &&
                             m_LiveForward.pendingTarget == endEventID &&
                             !m_pDriver->m_FetchCounters;
  const bool safeEndpoint = liveEnabled && (ordinaryRoot || pendingTarget) &&
                            PrepareLiveForward() && LiveForwardEndpoint(endEventID);
  const bool liveForward = ordinaryRoot && safeEndpoint && m_LiveForward.CanAdvance(endEventID);
  const bool textureTarget =
      replayType == eReplay_OnlyDraw && m_CheckpointTexturePendingTarget == endEventID;
  const bool trackTextures = m_CheckpointStats.enabled && !m_ReplayCheckpointBypass &&
                             !m_CheckpointTextureAnalysis && !m_ReplayCheckpointNaturalReference &&
                             (ordinaryRoot || textureTarget) && PrepareCheckpointTextureSharing();
  bool keepTextureVersions = false;
  struct TextureReplayScope
  {
    bool &managed, &keep;
    bool previous;
    std::map<ResourceId, std::weak_ptr<GLCheckpointTextureVersion>> &versions;
    ~TextureReplayScope()
    {
      managed = previous;
      if(!keep)
        versions.clear();
    }
  } textureScope = {m_CheckpointTextureReplayManaged, keepTextureVersions,
                    m_CheckpointTextureReplayManaged, m_CheckpointTextureVersions};
  m_CheckpointTextureReplayManaged = trackTextures;
  if(!trackTextures)
    ResetCheckpointTextureLineage();
  m_CheckpointTexturePendingTarget = 0;
  // Any other entry (including force/analysis) consumes the old provenance. A successful
  // ordinary root arms a new target only after its complete prefix has actually executed.
  m_LiveForward.Invalidate();
  // No buffer version proof survives a separate replay/analysis/OnlyDraw call. A successful checkpoint
  // restore below establishes a fresh lineage; scoped reset also covers errors and early returns.
  ResetCheckpointBufferLineage();
  struct ResetLineage
  {
    std::map<ResourceId, std::weak_ptr<GLCheckpointBufferVersion>> &versions;
    ~ResetLineage() { versions.clear(); }
  } resetLineage = {m_CheckpointBufferVersions};
  uint32_t startEvent = liveForward ? previousEvent + 1 : 0;
  const uint32_t finalEvent =
      replayType == eReplay_WithoutDraw ? RDCMAX(1U, endEventID) - 1 : endEventID;
  if(!liveForward && !m_ReplayCheckpointBypass && m_CheckpointStats.enabled && finalEvent > 0 &&
     (replayType == eReplay_Full || replayType == eReplay_WithoutDraw) &&
     !m_pDriver->m_FetchCounters && PrepareReplayCheckpoints())
  {
    auto best = m_Checkpoints.upper_bound(finalEvent);
    if(best != m_Checkpoints.begin())
    {
      --best;
      if(RestoreReplayCheckpoint(*best->second))
      {
        // Retained prefixes contain the previous value generation. Overlay the accepted ranges
        // even if the requested EID is exactly this checkpoint and no suffix will execute.
        if(!m_pDriver->ApplyReplayBufferPatches())
          return;
        startEvent = best->first + 1;
        m_CheckpointStats.hits++;
        m_CheckpointStats.lastResumeEvent = best->first;
      }
      else
        InvalidateReplayCheckpoints();
    }
    if(startEvent == 0)
      m_CheckpointStats.misses++;
    uint32_t saveEvent = 0;
    const bool needsPatchAnchor =
        m_ReplayPatchAnchorEvent && m_ReplayPatchAnchorEvent >= startEvent &&
        m_ReplayPatchAnchorEvent <= finalEvent &&
        m_Checkpoints.find(m_ReplayPatchAnchorEvent) == m_Checkpoints.end();
    for(const ReplayCheckpointCandidate &candidate : m_CheckpointCandidates)
    {
      if(candidate.eventId == 0 || candidate.eventId > finalEvent || candidate.eventId < startEvent ||
         m_Checkpoints.find(candidate.eventId) != m_Checkpoints.end())
        continue;
      if(startEvent != 0 && candidate.eventId - (startEvent - 1) < m_CheckpointConfig.minEventGap)
        continue;
      // Multi-draw child EIDs may share a chunk: only save after the entire chunk has finished.
      if(m_pDriver->GetEvent(candidate.eventId).fileOffset ==
         m_pDriver->GetEvent(candidate.eventId + 1).fileOffset)
        continue;
      saveEvent = candidate.eventId;
    }
    if(needsPatchAnchor)
      saveEvent = m_ReplayPatchAnchorEvent;
    if(m_ReplayCheckpointPrefixOnly && !m_pDriver->m_ReplayBufferPatches.empty() &&
       saveEvent != m_ReplayPatchAnchorEvent)
      saveEvent = 0;
    if(saveEvent)
    {
      m_pDriver->ReplayLog(startEvent, saveEvent, eReplay_Full, true);
      if(!m_pDriver->m_LastReplaySucceeded || FatalErrorCheck() != ResultCode::Succeeded)
        return;
      SaveReplayCheckpoint(saveEvent);
      startEvent = saveEvent + 1;
    }
  }
  bool replaySucceeded = true;
  if(startEvent == 0 || startEvent <= finalEvent || replayType == eReplay_OnlyDraw)
  {
    m_pDriver->ReplayLog(startEvent, endEventID, replayType, true);
    replaySucceeded = m_pDriver->m_LastReplaySucceeded;
  }
  replaySucceeded &= FatalErrorCheck() == ResultCode::Succeeded;
  keepTextureVersions = trackTextures && replaySucceeded;
  if(keepTextureVersions && ordinaryRoot)
    m_CheckpointTexturePendingTarget = endEventID;
  if(ordinaryRoot && safeEndpoint && replaySucceeded)
    m_LiveForward.Arm(endEventID);
  else if(pendingTarget && safeEndpoint)
  {
    m_LiveForward.Arm(endEventID);
    m_LiveForward.Complete(endEventID, replaySucceeded);
  }
  if(replayType == eReplay_WithoutDraw && Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
  {
    const rdcstr reason = !liveEnabled                  ? "disabled"
                          : !ordinaryRoot               ? "forceOrAnalysis"
                          : !m_LiveForwardCaptureSafe   ? m_LiveForwardReason
                          : !safeEndpoint               ? "sharedChunkOrUnsupportedEndpoint"
                          : !previousEvent              ? "noPublishedCursor"
                          : endEventID <= previousEvent ? "notForward"
                                                        : "residentPrefix";
    const rdcstr line = StringFormat::Fmt(
        "api=GLLiveForward from=%u target=%u enabled=%u hit=%u initialRestoreSkipped=%u "
        "replayedFirst=%u replayedLast=%u succeeded=%u total_ms=%.3f reason=%s",
        previousEvent, endEventID, liveEnabled ? 1U : 0U, liveForward ? 1U : 0U,
        liveForward ? 1U : 0U, startEvent, finalEvent, replaySucceeded ? 1U : 0U,
        liveTimer.GetMilliseconds(), reason.c_str());
    RDCLOG("[EIDTiming] %s", line.c_str());
    AppendReplayTimingLog(line);
  }

  // clear array cache
  for(size_t i = 0; i < ARRAY_COUNT(m_GetTexturePrevData); i++)
  {
    delete[] m_GetTexturePrevData[i];
    m_GetTexturePrevData[i] = NULL;
  }
}

SDFile *GLReplay::GetStructuredFile()
{
  return m_pDriver->GetStructuredFile();
}

rdcarray<uint32_t> GLReplay::GetPassEvents(uint32_t eventId)
{
  rdcarray<uint32_t> passEvents;

  const ActionDescription *action = m_pDriver->GetAction(eventId);

  const ActionDescription *start = action;
  while(start && start->previousAction && !(start->previousAction->flags & ActionFlags::Clear))
  {
    const ActionDescription *prev = start->previousAction;

    if(start->outputs != prev->outputs || start->depthOut != prev->depthOut)
      break;

    start = prev;
  }

  while(start)
  {
    if(start == action)
      break;

    if(start->flags & ActionFlags::Drawcall)
      passEvents.push_back(start->eventId);

    start = start->nextAction;
  }

  return passEvents;
}

rdcarray<WindowingSystem> GLReplay::GetSupportedWindowSystems()
{
  rdcarray<WindowingSystem> ret;

#if ENABLED(RDOC_LINUX)

#if ENABLED(RDOC_WAYLAND)
  // if wayland is supported and a display is configured, we *must* get wayland surfaces to render
  // on
  if(RenderDoc::Inst().GetGlobalEnvironment().waylandDisplay)
  {
    ret.push_back(WindowingSystem::Wayland);
  }
  else
#endif
  {
    // only Xlib supported for GLX. We can't report XCB here since we need the Display, and that
    // can't be obtained from XCB. The application is free to use XCB internally but it would have
    // to create a hybrid and initialise XCB out of Xlib, to be able to provide the display and
    // drawable to us.
    ret.push_back(WindowingSystem::Xlib);
  }

#elif ENABLED(RDOC_ANDROID)
  ret.push_back(WindowingSystem::Android);
#elif ENABLED(RDOC_APPLE)
  ret.push_back(WindowingSystem::MacOS);
#elif ENABLED(RDOC_WIN32)
  ret.push_back(WindowingSystem::Win32);
#endif

  return ret;
}

rdcarray<GPUDevice> GLReplay::GetAvailableGPUs()
{
  // GL doesn't support multiple GPUs, return an empty list
  return {};
}

APIProperties GLReplay::GetAPIProperties()
{
  APIProperties ret = m_pDriver->APIProps;

  ret.pipelineType = GraphicsAPI::OpenGL;
  ret.localRenderer = GraphicsAPI::OpenGL;
  ret.degraded = m_Degraded;
  ret.vendor = m_DriverInfo.vendor;
  ret.pixelHistory = true;
  // we require storage buffers and compute shaders
  ret.shaderDebugging = HasExt[ARB_shader_storage_buffer_object] && HasExt[ARB_compute_shader];

  return ret;
}

ResourceDescription &GLReplay::GetResourceDesc(ResourceId id)
{
  auto it = m_ResourceIdx.find(id);
  if(it == m_ResourceIdx.end())
  {
    m_ResourceIdx[id] = m_Resources.size();
    m_Resources.push_back(ResourceDescription());
    m_Resources.back().resourceId = id;
    return m_Resources.back();
  }

  return m_Resources[it->second];
}

rdcarray<ResourceDescription> GLReplay::GetResources()
{
  return m_Resources;
}

void GLReplay::SetReplayData(GLWindowingData data)
{
  m_ReplayCtx = data;
  m_pDriver->RegisterReplayContext(m_ReplayCtx, NULL, true, true);

  m_pDriver->RegisterDebugCallback();

  InitDebugData();

  if(!HasDebugContext())
    return;

  if(!m_Proxy && OpenGL_HardwareCounters())
  {
    AMDCounters *countersAMD = NULL;
    IntelGlCounters *countersIntel = NULL;
    ARMCounters *countersARM = NULL;
#if DISABLED(RDOC_ANDROID) && DISABLED(RDOC_APPLE)
    NVGLCounters *countersNV = NULL;
#endif

    bool isMesa = false;

    // try to identify mesa - don't enable any IHV counters when running mesa.
    {
      WrappedOpenGL &drv = *m_pDriver;

      const char *version = (const char *)drv.glGetString(eGL_VERSION);
      const char *vendor = (const char *)drv.glGetString(eGL_VENDOR);
      const char *renderer = (const char *)drv.glGetString(eGL_RENDERER);

      for(rdcstr haystack : {strlower(version), strlower(vendor), strlower(renderer)})
      {
        haystack = " " + haystack + " ";

        // the version should always contain 'mesa', but it's also commonly present in either vendor
        // or renderer - except for nouveau which we look for separately
        for(const char *needle : {" mesa ", "nouveau"})
        {
          if(haystack.contains(needle))
          {
            isMesa = true;
            break;
          }
        }

        if(isMesa)
          break;
      }
    }

    if(isMesa)
    {
      if(m_DriverInfo.vendor == GPUVendor::Intel)
      {
        RDCLOG("Intel GPU detected - trying to initialise Intel GL counters");
        countersIntel = new IntelGlCounters();
      }
      else
        RDCLOG("Non Intel Mesa driver detected - skipping IHV counter initialisation");
    }
    else
    {
      if(m_DriverInfo.vendor == GPUVendor::Intel)
      {
        RDCLOG("Intel GPU detected - trying to initialise Intel GL counters");
        countersIntel = new IntelGlCounters();
      }
      else if(m_DriverInfo.vendor == GPUVendor::AMD || m_DriverInfo.vendor == GPUVendor::Samsung)
      {
        RDCLOG("AMD or Samsung GPU detected - trying to initialise AMD counters");
        countersAMD = new AMDCounters();
      }
      else if(m_DriverInfo.vendor == GPUVendor::ARM)
      {
        RDCLOG("ARM Mali GPU detected - trying to initialise ARM counters");
        countersARM = new ARMCounters();
      }
#if DISABLED(RDOC_ANDROID) && DISABLED(RDOC_APPLE)
      else if(m_DriverInfo.vendor == GPUVendor::nVidia)
      {
        RDCLOG("NVIDIA GPU detected - trying to initialise NVIDIA counters");
        countersNV = new NVGLCounters();
      }
#endif
      else
      {
        RDCLOG("%s GPU detected - no counters available", ToStr(m_DriverInfo.vendor).c_str());
      }
    }

    if(countersAMD && countersAMD->Init(AMDCounters::ApiType::Ogl, m_ReplayCtx.ctx))
    {
      m_pAMDCounters = countersAMD;
    }
    else
    {
      delete countersAMD;
      m_pAMDCounters = NULL;
    }

    if(countersIntel && countersIntel->Init())
    {
      m_pIntelCounters = countersIntel;
    }
    else
    {
      delete countersIntel;
      m_pIntelCounters = NULL;
    }

    if(countersARM && countersARM->Init())
    {
      m_pARMCounters = countersARM;
    }
    else
    {
      delete countersARM;
      m_pARMCounters = NULL;
    }

#if DISABLED(RDOC_ANDROID) && DISABLED(RDOC_APPLE)
    if(countersNV && countersNV->Init(m_pDriver))
    {
      m_pNVCounters = countersNV;
    }
    else
    {
      delete countersNV;
      m_pNVCounters = NULL;
    }
#endif
  }
}

void GLReplay::GetBufferData(ResourceId buff, uint64_t offset, uint64_t len, bytebuf &ret)
{
  // Texture display/readback can leave an output/debug context current. Buffer
  // reads must observe the replay context's command stream and binding namespace,
  // just like FillCBufferVariables and the other resource read paths.
  MakeCurrentReplayContext(&m_ReplayCtx);

  if(m_pDriver->m_Buffers.find(buff) == m_pDriver->m_Buffers.end())
  {
    RDCWARN("Requesting data for non-existant buffer %s", ToStr(buff).c_str());
    ret.clear();
    return;
  }

  WrappedOpenGL::BufferData &buf = m_pDriver->m_Buffers[buff];

  uint64_t bufsize = buf.size;

  if(offset >= bufsize)
  {
    // can't read past the end of the buffer, return empty
    return;
  }

  if(len == 0 || len > bufsize)
  {
    len = bufsize;
  }

  if(offset + len > bufsize)
  {
    RDCWARN("Attempting to read off the end of the buffer (%llu %llu). Will be clamped (%llu)",
            offset, len, bufsize);
    len = RDCMIN(len, bufsize - offset);
  }

  ret.resize((size_t)len);

  WrappedOpenGL &drv = *m_pDriver;

  GLuint oldbuf = 0;
  drv.glGetIntegerv(eGL_COPY_READ_BUFFER_BINDING, (GLint *)&oldbuf);

  drv.glBindBuffer(eGL_COPY_READ_BUFFER, buf.resource.name);

  drv.glGetBufferSubData(eGL_COPY_READ_BUFFER, (GLintptr)offset, (GLsizeiptr)len, &ret[0]);

  drv.glBindBuffer(eGL_COPY_READ_BUFFER, oldbuf);
}

void GLReplay::CacheTexture(ResourceId id)
{
  if(m_CachedTextures.find(id) != m_CachedTextures.end())
    return;

  TextureDescription tex = {};

  MakeCurrentReplayContext(&m_ReplayCtx);

  WrappedOpenGL::TextureData &res = m_pDriver->m_Textures[id];
  WrappedOpenGL &drv = *m_pDriver;

  tex.resourceId = id;

  if(res.resource.Namespace == eResUnknown || res.curType == eGL_NONE)
  {
    if(res.resource.Namespace == eResUnknown)
      RDCERR("Details for invalid texture id %s requested", ToStr(id).c_str());

    tex.format = ResourceFormat();
    tex.dimension = 1;
    tex.type = TextureType::Unknown;
    tex.width = tex.height = tex.depth = 1;
    tex.cubemap = false;
    tex.mips = 1;
    tex.arraysize = 1;
    tex.creationFlags = TextureCategory::NoFlags;
    tex.msQual = 0;
    tex.msSamp = 1;
    tex.byteSize = 1;

    m_CachedTextures[id] = tex;
    return;
  }

  if(res.resource.Namespace == eResRenderbuffer || res.curType == eGL_RENDERBUFFER)
  {
    tex.dimension = 2;
    tex.type = TextureType::Texture2D;
    tex.width = res.width;
    tex.height = res.height;
    tex.depth = 1;
    tex.cubemap = false;
    tex.mips = 1;
    tex.arraysize = 1;
    tex.creationFlags = TextureCategory::ColorTarget;
    tex.msQual = 0;
    tex.msSamp = RDCMAX(1, res.samples);

    if(res.internalFormat == eGL_NONE)
    {
      tex.format = ResourceFormat();
    }
    else
    {
      tex.format = MakeResourceFormat(eGL_TEXTURE_2D, res.internalFormat);

      if(IsDepthStencilFormat(res.internalFormat))
        tex.creationFlags |= TextureCategory::DepthTarget;
    }

    tex.byteSize = GLRenderbufferByteSize(tex.width, tex.height, tex.msSamp, res.internalFormat);

    m_CachedTextures[id] = tex;
    return;
  }

  GLenum target = TextureTarget(res.curType);

  GLenum levelQueryType = target;
  if(levelQueryType == eGL_TEXTURE_CUBE_MAP)
    levelQueryType = eGL_TEXTURE_CUBE_MAP_POSITIVE_X;

  GLint width = 1, height = 1, depth = 1, samples = 1;
  drv.glGetTextureLevelParameterivEXT(res.resource.name, levelQueryType, 0, eGL_TEXTURE_WIDTH,
                                      &width);
  drv.glGetTextureLevelParameterivEXT(res.resource.name, levelQueryType, 0, eGL_TEXTURE_HEIGHT,
                                      &height);
  drv.glGetTextureLevelParameterivEXT(res.resource.name, levelQueryType, 0, eGL_TEXTURE_DEPTH,
                                      &depth);
  drv.glGetTextureLevelParameterivEXT(res.resource.name, levelQueryType, 0, eGL_TEXTURE_SAMPLES,
                                      &samples);

  // the above queries sometimes come back 0, if we have dimensions from creation functions, use
  // those
  if(width == 0 && res.width > 0)
    width = res.width;
  if(height == 0 && res.height > 0)
    height = res.height;
  if(depth == 0 && res.depth > 0)
    depth = res.depth;

  if(res.width == 0 && width > 0)
  {
    RDCWARN("TextureData::width didn't get filled out, setting at last minute");
    res.width = width;
  }
  if(res.height == 0 && height > 0)
  {
    RDCWARN("TextureData::height didn't get filled out, setting at last minute");
    res.height = height;
  }
  if(res.depth == 0 && depth > 0)
  {
    RDCWARN("TextureData::depth didn't get filled out, setting at last minute");
    res.depth = depth;
  }

  // reasonably common defaults
  tex.msQual = 0;
  tex.msSamp = 1;
  tex.width = tex.height = tex.depth = tex.arraysize = 1;
  tex.cubemap = false;

  switch(target)
  {
    case eGL_TEXTURE_BUFFER: tex.type = TextureType::Buffer; break;
    case eGL_TEXTURE_1D: tex.type = TextureType::Texture1D; break;
    case eGL_TEXTURE_2D: tex.type = TextureType::Texture2D; break;
    case eGL_TEXTURE_3D: tex.type = TextureType::Texture3D; break;
    case eGL_TEXTURE_1D_ARRAY: tex.type = TextureType::Texture1DArray; break;
    case eGL_TEXTURE_2D_ARRAY: tex.type = TextureType::Texture2DArray; break;
    case eGL_TEXTURE_RECTANGLE: tex.type = TextureType::TextureRect; break;
    case eGL_TEXTURE_2D_MULTISAMPLE: tex.type = TextureType::Texture2DMS; break;
    case eGL_TEXTURE_2D_MULTISAMPLE_ARRAY: tex.type = TextureType::Texture2DMSArray; break;
    case eGL_TEXTURE_CUBE_MAP: tex.type = TextureType::TextureCube; break;
    case eGL_TEXTURE_CUBE_MAP_ARRAY: tex.type = TextureType::TextureCubeArray; break;

    default:
      tex.type = TextureType::Unknown;
      RDCERR("Unexpected texture enum %s", ToStr(target).c_str());
  }

  switch(target)
  {
    case eGL_TEXTURE_1D:
    case eGL_TEXTURE_BUFFER:
      tex.dimension = 1;
      tex.width = (uint32_t)width;
      break;
    case eGL_TEXTURE_1D_ARRAY:
      tex.dimension = 1;
      tex.width = (uint32_t)width;
      tex.arraysize = height;
      break;
    case eGL_TEXTURE_2D:
    case eGL_TEXTURE_RECTANGLE:
    case eGL_TEXTURE_2D_MULTISAMPLE:
    case eGL_TEXTURE_CUBE_MAP:
      tex.dimension = 2;
      tex.width = (uint32_t)width;
      tex.height = (uint32_t)height;
      tex.depth = 1;
      tex.arraysize = (target == eGL_TEXTURE_CUBE_MAP ? 6 : 1);
      tex.cubemap = (target == eGL_TEXTURE_CUBE_MAP);
      tex.msSamp = RDCMAX(1, target == eGL_TEXTURE_2D_MULTISAMPLE ? samples : 1);
      break;
    case eGL_TEXTURE_2D_ARRAY:
    case eGL_TEXTURE_2D_MULTISAMPLE_ARRAY:
    case eGL_TEXTURE_CUBE_MAP_ARRAY:
      tex.dimension = 2;
      tex.width = (uint32_t)width;
      tex.height = (uint32_t)height;
      tex.depth = 1;
      tex.arraysize = depth;
      tex.cubemap = (target == eGL_TEXTURE_CUBE_MAP_ARRAY);
      tex.msSamp = RDCMAX(1, target == eGL_TEXTURE_2D_MULTISAMPLE_ARRAY ? samples : 1);
      break;
    case eGL_TEXTURE_3D:
      tex.dimension = 3;
      tex.width = (uint32_t)width;
      tex.height = (uint32_t)height;
      tex.depth = (uint32_t)depth;
      break;

    default: tex.dimension = 2; RDCERR("Unexpected texture enum %s", ToStr(target).c_str());
  }

  tex.creationFlags = res.creationFlags;

  // surely this will be the same for each level... right? that would be insane if it wasn't
  GLint fmt = 0;
  drv.glGetTextureLevelParameterivEXT(res.resource.name, levelQueryType, 0,
                                      eGL_TEXTURE_INTERNAL_FORMAT, &fmt);

  tex.format = MakeResourceFormat(target, (GLenum)fmt);

  if(tex.format.compType == CompType::Depth)
    tex.creationFlags |= TextureCategory::DepthTarget;

  if(target == eGL_TEXTURE_BUFFER)
  {
    tex.dimension = 1;
    tex.height = tex.depth = 1;
    tex.cubemap = false;
    tex.mips = 1;
    tex.arraysize = 1;
    tex.creationFlags = TextureCategory::ShaderRead;
    tex.msQual = 0;
    tex.msSamp = 1;
    tex.byteSize = 0;

    if(HasExt[ARB_texture_buffer_range])
    {
      drv.glGetTextureLevelParameterivEXT(res.resource.name, levelQueryType, 0,
                                          eGL_TEXTURE_BUFFER_SIZE, (GLint *)&tex.byteSize);
      tex.width = uint32_t(tex.byteSize / RDCMAX(1, tex.format.compByteWidth * tex.format.compCount));
    }

    m_CachedTextures[id] = tex;
    return;
  }

  if(res.view)
  {
    tex.mips = Log2Floor(res.mipsValid + 1);
  }
  else
  {
    tex.mips = GetNumMips(target, res.resource.name, tex.width, tex.height, tex.depth);
  }

  GLint compressed = 0;
  drv.glGetTextureLevelParameterivEXT(res.resource.name, levelQueryType, 0, eGL_TEXTURE_COMPRESSED,
                                      &compressed);
  tex.byteSize = 0;
  for(uint32_t m = 0; m < tex.mips; m++)
  {
    if(fmt == eGL_NONE)
    {
    }
    else if(compressed)
    {
      tex.byteSize += (uint64_t)GetCompressedByteSize(RDCMAX(1U, tex.width >> m),
                                                      RDCMAX(1U, tex.height >> m), 1, (GLenum)fmt);
    }
    else if(tex.format.Special())
    {
      tex.byteSize += GetByteSize(RDCMAX(1U, tex.width >> m), RDCMAX(1U, tex.height >> m),
                                  RDCMAX(1U, tex.depth >> m), GetBaseFormat((GLenum)fmt),
                                  GetDataType((GLenum)fmt));
    }
    else
    {
      tex.byteSize += RDCMAX(1U, tex.width >> m) * RDCMAX(1U, tex.height >> m) *
                      RDCMAX(1U, tex.depth >> m) * tex.format.compByteWidth * tex.format.compCount;
    }
  }

  tex.byteSize *= tex.arraysize;
  tex.byteSize *= tex.msSamp;

  m_CachedTextures[id] = tex;
}

BufferDescription GLReplay::GetBuffer(ResourceId id)
{
  BufferDescription ret = {};

  MakeCurrentReplayContext(&m_ReplayCtx);

  WrappedOpenGL::BufferData &res = m_pDriver->m_Buffers[id];

  if(res.resource.Namespace == eResUnknown)
  {
    RDCERR("Details for invalid buffer id %s requested", ToStr(id).c_str());
    RDCEraseEl(ret);
    return ret;
  }

  WrappedOpenGL &drv = *m_pDriver;

  ret.resourceId = id;

  GLint prevBind = 0;
  if(res.curType != eGL_NONE)
  {
    drv.glGetIntegerv(BufferBinding(res.curType), &prevBind);

    drv.glBindBuffer(res.curType, res.resource.name);
  }

  ret.creationFlags = res.creationFlags;

  ret.length = res.size;

  if(res.curType != eGL_NONE)
    drv.glBindBuffer(res.curType, prevBind);

  return ret;
}

TextureDescription GLReplay::GetTexture(ResourceId id)
{
  auto it = m_CachedTextures.find(id);
  if(it == m_CachedTextures.end())
  {
    CacheTexture(id);
    return m_CachedTextures[id];
  }

  return it->second;
}

rdcarray<BufferDescription> GLReplay::GetBuffers()
{
  rdcarray<BufferDescription> ret;

  for(auto it = m_pDriver->m_Buffers.begin(); it != m_pDriver->m_Buffers.end(); ++it)
  {
    // skip buffers that aren't from the log
    if(ResourceIDGen::IsReplayOnlyID(it->first))
      continue;

    ret.push_back(GetBuffer(it->first));
  }

  return ret;
}

rdcarray<TextureDescription> GLReplay::GetTextures()
{
  rdcarray<TextureDescription> ret;
  ret.reserve(m_pDriver->m_Textures.size());

  for(auto it = m_pDriver->m_Textures.begin(); it != m_pDriver->m_Textures.end(); ++it)
  {
    WrappedOpenGL::TextureData &res = m_pDriver->m_Textures[it->first];

    // skip textures that aren't from the log (except the 'default backbuffer' textures)
    if(!(res.creationFlags & TextureCategory::SwapBuffer) && ResourceIDGen::IsReplayOnlyID(it->first))
      continue;

    CacheTexture(it->first);
    ret.push_back(m_CachedTextures[it->first]);
  }

  return ret;
}

rdcarray<DebugMessage> GLReplay::GetDebugMessages()
{
  return m_pDriver->GetDebugMessages();
}

rdcarray<ShaderEntryPoint> GLReplay::GetShaderEntryPoints(ResourceId shader)
{
  if(!m_pDriver->HasShader(shader))
    return {};

  const WrappedOpenGL::ShaderData &shaderDetails = m_pDriver->GetShader(shader);

  if(shaderDetails.GetReflection()->resourceId == ResourceId())
  {
    RDCERR("Can't get shader details without successful reflect");
    return {};
  }

  return {{shaderDetails.GetReflection()->entryPoint, shaderDetails.GetReflection()->stage}};
}

const ShaderReflection *GLReplay::GetShader(ResourceId pipeline, ResourceId shader,
                                            ShaderEntryPoint entry)
{
  const WrappedOpenGL::ShaderData &shaderDetails = m_pDriver->GetShader(shader);

  if(shaderDetails.GetReflection()->resourceId == ResourceId())
  {
    RDCERR("Can't get shader details without successful reflect");
    return NULL;
  }

  return shaderDetails.GetReflection();
}

rdcarray<rdcstr> GLReplay::GetDisassemblyTargets(bool withPipeline)
{
  return {SPIRVDisassemblyTarget};
}

rdcstr GLReplay::DisassembleShader(ResourceId pipeline, const ShaderReflection *refl,
                                   const rdcstr &target)
{
  const WrappedOpenGL::ShaderData &shaderDetails = m_pDriver->GetShader(refl->resourceId);

  if(shaderDetails.sources.empty() && shaderDetails.spirvWords.empty() &&
     shaderDetails.convertedSpirvWords.empty())
    return "; Invalid Shader Specified";

  if(target == SPIRVDisassemblyTarget || target.empty())
  {
    m_pDriver->GetWriteableShader(refl->resourceId).Disassemble(refl->entryPoint);

    return shaderDetails.disassembly;
  }

  return StringFormat::Fmt("; Invalid disassembly target %s", target.c_str());
}

void GLReplay::SavePipelineState(uint32_t eventId)
{
  if(!m_GLPipelineState)
    return;

  GLPipe::State &pipe = *m_GLPipelineState;
  WrappedOpenGL &drv = *m_pDriver;
  GLResourceManager *rm = m_pDriver->GetResourceManager();

  MakeCurrentReplayContext(&m_ReplayCtx);

  GLRenderState rs;
  rs.FetchState(&drv);

  // Index buffer

  ContextPair &ctx = drv.GetCtx();

  pipe.descriptorStore = m_pDriver->m_DescriptorsID;
  pipe.descriptorCount = EncodeGLDescriptorIndex({GLDescriptorMapping::Count, 0});
  pipe.descriptorByteSize = 1;

  m_Access.clear();

  GLuint vao = 0;
  drv.glGetIntegerv(eGL_VERTEX_ARRAY_BINDING, (GLint *)&vao);
  pipe.vertexInput.vertexArrayObject = rm->GetResID(VertexArrayRes(ctx, vao));

  GLuint ibuffer = 0;
  drv.glGetIntegerv(eGL_ELEMENT_ARRAY_BUFFER_BINDING, (GLint *)&ibuffer);
  pipe.vertexInput.indexBuffer = rm->GetResID(BufferRes(ctx, ibuffer));

  pipe.vertexInput.primitiveRestart = rs.Enabled[GLRenderState::eEnabled_PrimitiveRestart] ||
                                      rs.Enabled[GLRenderState::eEnabled_PrimitiveRestartFixedIndex];
  pipe.vertexInput.restartIndex = rs.Enabled[GLRenderState::eEnabled_PrimitiveRestartFixedIndex]
                                      ? ~0U
                                      : rs.PrimitiveRestartIndex;

  const GLDrawParams &drawParams = m_pDriver->GetDrawParameters(eventId);

  pipe.vertexInput.indexByteStride = drawParams.indexWidth;
  pipe.vertexInput.topology = drawParams.topo;

  // Vertex buffers and attributes
  GLint numVBufferBindings = 16;
  drv.glGetIntegerv(eGL_MAX_VERTEX_ATTRIB_BINDINGS, &numVBufferBindings);

  GLint numVAttribBindings = 16;
  drv.glGetIntegerv(eGL_MAX_VERTEX_ATTRIBS, &numVAttribBindings);

  pipe.vertexInput.vertexBuffers.resize(numVBufferBindings);
  pipe.vertexInput.attributes.resize(numVAttribBindings);

  for(GLuint i = 0; i < (GLuint)numVBufferBindings; i++)
  {
    GLuint buffer = GetBoundVertexBuffer(i);

    pipe.vertexInput.vertexBuffers[i].resourceId = rm->GetResID(BufferRes(ctx, buffer));

    drv.glGetIntegeri_v(eGL_VERTEX_BINDING_STRIDE, i,
                        (GLint *)&pipe.vertexInput.vertexBuffers[i].byteStride);
    drv.glGetIntegeri_v(eGL_VERTEX_BINDING_OFFSET, i,
                        (GLint *)&pipe.vertexInput.vertexBuffers[i].byteOffset);
    drv.glGetIntegeri_v(eGL_VERTEX_BINDING_DIVISOR, i,
                        (GLint *)&pipe.vertexInput.vertexBuffers[i].instanceDivisor);
  }

  for(GLuint i = 0; i < (GLuint)numVAttribBindings; i++)
  {
    drv.glGetVertexAttribiv(i, eGL_VERTEX_ATTRIB_ARRAY_ENABLED,
                            (GLint *)&pipe.vertexInput.attributes[i].enabled);
    drv.glGetVertexAttribiv(i, eGL_VERTEX_ATTRIB_BINDING,
                            (GLint *)&pipe.vertexInput.attributes[i].vertexBufferSlot);
    drv.glGetVertexAttribiv(i, eGL_VERTEX_ATTRIB_RELATIVE_OFFSET,
                            (GLint *)&pipe.vertexInput.attributes[i].byteOffset);

    GLenum type = eGL_FLOAT;
    GLint normalized = 0;

    drv.glGetVertexAttribiv(i, eGL_VERTEX_ATTRIB_ARRAY_TYPE, (GLint *)&type);
    drv.glGetVertexAttribiv(i, eGL_VERTEX_ATTRIB_ARRAY_NORMALIZED, &normalized);

    GLint integer = 0;
    drv.glGetVertexAttribiv(i, eGL_VERTEX_ATTRIB_ARRAY_INTEGER, &integer);

    RDCEraseEl(pipe.vertexInput.attributes[i].genericValue);
    drv.glGetVertexAttribfv(i, eGL_CURRENT_VERTEX_ATTRIB,
                            (GLfloat *)pipe.vertexInput.attributes[i].genericValue.floatValue.data());

    ResourceFormat fmt;

    fmt.type = ResourceFormatType::Regular;
    GLint compCount;
    drv.glGetVertexAttribiv(i, eGL_VERTEX_ATTRIB_ARRAY_SIZE, (GLint *)&compCount);

    fmt.compCount = (uint8_t)compCount;

    switch(type)
    {
      default:
      case eGL_BYTE:
        fmt.compByteWidth = 1;
        fmt.compType = CompType::SInt;
        break;
      case eGL_UNSIGNED_BYTE:
        fmt.compByteWidth = 1;
        fmt.compType = CompType::UInt;
        break;
      case eGL_SHORT:
        fmt.compByteWidth = 2;
        fmt.compType = CompType::SInt;
        break;
      case eGL_UNSIGNED_SHORT:
        fmt.compByteWidth = 2;
        fmt.compType = CompType::UInt;
        break;
      case eGL_INT:
        fmt.compByteWidth = 4;
        fmt.compType = CompType::SInt;
        break;
      case eGL_UNSIGNED_INT:
        fmt.compByteWidth = 4;
        fmt.compType = CompType::UInt;
        break;
      case eGL_FLOAT:
        fmt.compByteWidth = 4;
        fmt.compType = CompType::Float;
        break;
      case eGL_DOUBLE:
        fmt.compByteWidth = 8;
        fmt.compType = CompType::Float;
        break;
      case eGL_HALF_FLOAT:
        fmt.compByteWidth = 2;
        fmt.compType = CompType::Float;
        break;
      case eGL_INT_2_10_10_10_REV:
        fmt.type = ResourceFormatType::R10G10B10A2;
        fmt.compCount = 4;
        fmt.compType = CompType::SInt;
        break;
      case eGL_UNSIGNED_INT_2_10_10_10_REV:
        fmt.type = ResourceFormatType::R10G10B10A2;
        fmt.compCount = 4;
        fmt.compType = CompType::UInt;
        break;
      case eGL_UNSIGNED_INT_10F_11F_11F_REV:
        fmt.type = ResourceFormatType::R11G11B10;
        fmt.compCount = 3;
        fmt.compType = CompType::Float;
        // spec says this format is never normalized regardless.
        normalized = 0;
        break;
    }

    if(compCount == eGL_BGRA)
    {
      fmt.compByteWidth = 1;
      fmt.compCount = 4;
      fmt.SetBGRAOrder(true);
      fmt.compType = CompType::UNorm;

      // spec says BGRA inputs are ALWAYS normalised
      normalized = 1;

      if(type == eGL_UNSIGNED_INT_2_10_10_10_REV || type == eGL_INT_2_10_10_10_REV)
      {
        fmt.type = ResourceFormatType::R10G10B10A2;
        fmt.compType = type == eGL_UNSIGNED_INT_2_10_10_10_REV ? CompType::UInt : CompType::SInt;
      }
      else if(type != eGL_UNSIGNED_BYTE)
      {
        // haven't checked the other cases work properly
        RDCERR("Unexpected BGRA type");
      }
    }

    // normalized/floatCast flags are irrelevant for float formats
    if(fmt.compType == CompType::SInt || fmt.compType == CompType::UInt)
    {
      // if it wasn't an integer, it's cast to float
      pipe.vertexInput.attributes[i].floatCast = !integer;

      // if we're casting, change the component type as appropriate
      if(!integer)
      {
        if(normalized != 0)
          fmt.compType = (fmt.compType == CompType::SInt) ? CompType::SNorm : CompType::UNorm;
      }
    }
    else
    {
      pipe.vertexInput.attributes[i].floatCast = false;
    }

    pipe.vertexInput.attributes[i].format = fmt;
  }

  pipe.vertexInput.provokingVertexLast = (rs.ProvokingVertex != eGL_FIRST_VERTEX_CONVENTION);

  pipe.vertexProcessing.defaultInnerLevel = rs.PatchParams.defaultInnerLevel;
  pipe.vertexProcessing.defaultOuterLevel = rs.PatchParams.defaultOuterLevel;

  pipe.vertexProcessing.discard = rs.Enabled[GLRenderState::eEnabled_RasterizerDiscard];
  pipe.vertexProcessing.clipOriginLowerLeft = (rs.ClipOrigin != eGL_UPPER_LEFT);
  pipe.vertexProcessing.clipNegativeOneToOne = (rs.ClipDepth != eGL_ZERO_TO_ONE);
  for(int i = 0; i < 8; i++)
    pipe.vertexProcessing.clipPlanes[i] = rs.Enabled[GLRenderState::eEnabled_ClipDistance0 + i];

  // Shader stages & Textures

  GLint numTexUnits = 8;
  drv.glGetIntegerv(eGL_MAX_COMBINED_TEXTURE_IMAGE_UNITS, &numTexUnits);

  GLenum activeTexture = eGL_TEXTURE0;
  drv.glGetIntegerv(eGL_ACTIVE_TEXTURE, (GLint *)&activeTexture);

  pipe.vertexShader.stage = ShaderStage::Vertex;
  pipe.tessControlShader.stage = ShaderStage::Tess_Control;
  pipe.tessEvalShader.stage = ShaderStage::Tess_Eval;
  pipe.geometryShader.stage = ShaderStage::Geometry;
  pipe.fragmentShader.stage = ShaderStage::Fragment;
  pipe.computeShader.stage = ShaderStage::Compute;

  GLPipe::Shader *stages[NumShaderStages] = {
      &pipe.vertexShader,   &pipe.tessControlShader, &pipe.tessEvalShader,
      &pipe.geometryShader, &pipe.fragmentShader,    &pipe.computeShader,
  };
  const ShaderReflection *refls[NumShaderStages] = {NULL};
  ResourceId progIds[NumShaderStages];
  ResourceId shadIds[NumShaderStages];
  GLuint progForStage[NumShaderStages] = {};
  bool spirv[NumShaderStages] = {false};

  for(size_t i = 0; i < NumShaderStages; i++)
  {
    if(!stages[i])
      continue;

    stages[i]->programResourceId = stages[i]->shaderResourceId = ResourceId();
    stages[i]->reflection = NULL;
  }

  rdcarray<int32_t> vertexAttrBindings;

  {
    GLuint curProg = 0;
    drv.glGetIntegerv(eGL_CURRENT_PROGRAM, (GLint *)&curProg);

    if(curProg == 0)
    {
      GLuint curPipe = 0;
      drv.glGetIntegerv(eGL_PROGRAM_PIPELINE_BINDING, (GLint *)&curPipe);

      if(curPipe != 0)
      {
        ResourceId id = rm->GetResID(ProgramPipeRes(ctx, curPipe));
        const WrappedOpenGL::PipelineData &pipeDetails = m_pDriver->GetPipeline(id);

        pipe.pipelineResourceId = rm->GetUnreplacedID(id);

        for(size_t i = 0; i < ARRAY_COUNT(pipeDetails.stageShaders); i++)
        {
          if(!stages[i])
            continue;

          if(pipeDetails.stageShaders[i] != ResourceId())
          {
            progIds[i] = pipeDetails.stagePrograms[i];
            shadIds[i] = pipeDetails.stageShaders[i];

            progForStage[i] = rm->GetResource(pipeDetails.stagePrograms[i]).name;
          }
        }
      }
    }
    else
    {
      ResourceId id = rm->GetResID(ProgramRes(ctx, curProg));
      const WrappedOpenGL::ProgramData &progDetails = m_pDriver->GetProgram(id);

      pipe.pipelineResourceId = ResourceId();

      for(size_t i = 0; i < ARRAY_COUNT(progDetails.stageShaders); i++)
      {
        if(!stages[i])
          continue;

        if(progDetails.stageShaders[i] != ResourceId())
        {
          progIds[i] = id;
          shadIds[i] = progDetails.stageShaders[i];

          progForStage[i] = curProg;
        }
      }
    }
  }

  for(size_t i = 0; i < NumShaderStages; i++)
  {
    if(progForStage[i])
    {
      progForStage[i] = rm->GetResource(progIds[i]).name;
      stages[i]->programResourceId = rm->GetUnreplacedID(progIds[i]);
      stages[i]->shaderResourceId = rm->GetUnreplacedID(shadIds[i]);

      const WrappedOpenGL::ShaderData &shaderDetails = m_pDriver->GetShader(shadIds[i]);

      if(shaderDetails.GetReflection()->resourceId == ResourceId())
        stages[i]->reflection = refls[i] = NULL;
      else
        stages[i]->reflection = refls[i] = shaderDetails.GetReflection();

      if(!shaderDetails.spirvWords.empty())
        spirv[i] = true;

      if(i == 0)
        EvaluateVertexAttributeBinds(progForStage[i], refls[i], spirv[i], vertexAttrBindings);
    }
    else if(stages[i])
    {
      stages[i]->programResourceId = stages[i]->shaderResourceId = ResourceId();
      stages[i]->reflection = NULL;
    }
  }

  for(size_t i = 0; i < pipe.vertexInput.attributes.size(); i++)
  {
    if(i < vertexAttrBindings.size())
      pipe.vertexInput.attributes[i].boundShaderInput = vertexAttrBindings[i];
    else
      pipe.vertexInput.attributes[i].boundShaderInput = -1;
  }

  pipe.textureCompleteness.clear();

  for(size_t s = 0; s < NumShaderStages; s++)
  {
    const ShaderReflection *refl = refls[s];

    if(!refl)
      continue;

    GLuint prog = progForStage[s];

    DescriptorAccess access;
    access.descriptorStore = m_pDriver->m_DescriptorsID;
    access.stage = refl->stage;
    access.byteSize = 1;

    m_Access.reserve(m_Access.size() + refl->constantBlocks.size() +
                     refl->readOnlyResources.size() + refl->readWriteResources.size());

    RDCASSERT(refl->constantBlocks.size() < 0xffff, refl->constantBlocks.size());
    for(uint16_t i = 0; i < refl->constantBlocks.size(); i++)
    {
      uint32_t slot = 0;
      bool used = false;
      GetCurrentBinding(prog, refl, refl->constantBlocks[i], slot, used);

      access.staticallyUnused = !used;
      access.type = DescriptorType::ConstantBuffer;
      access.index = i;
      if(!refl->constantBlocks[i].bufferBacked)
        access.byteOffset = EncodeGLDescriptorIndex({GLDescriptorMapping::BareUniforms, (uint32_t)s});
      else
        access.byteOffset =
            EncodeGLDescriptorIndex({GLDescriptorMapping::UniformBinding, (uint32_t)slot});
      m_Access.push_back(access);
    }

    RDCASSERT(refl->readOnlyResources.size() < 0xffff, refl->readOnlyResources.size());
    for(uint16_t i = 0; i < refl->readOnlyResources.size(); i++)
    {
      uint32_t slot = 0;
      bool used = false;
      GetCurrentBinding(prog, refl, refl->readOnlyResources[i], slot, used);

      access.staticallyUnused = !used;

      GLDescriptorMapping descType = GLDescriptorMapping::Tex2D;

      switch(refl->readOnlyResources[i].textureType)
      {
        case TextureType::Buffer: descType = GLDescriptorMapping::TexBuffer; break;
        case TextureType::Texture1D: descType = GLDescriptorMapping::Tex1D; break;
        case TextureType::Texture1DArray: descType = GLDescriptorMapping::Tex1DArray; break;
        case TextureType::Texture2D: descType = GLDescriptorMapping::Tex2D; break;
        case TextureType::TextureRect: descType = GLDescriptorMapping::TexRect; break;
        case TextureType::Texture2DArray: descType = GLDescriptorMapping::Tex2DArray; break;
        case TextureType::Texture2DMS: descType = GLDescriptorMapping::Tex2DMS; break;
        case TextureType::Texture2DMSArray: descType = GLDescriptorMapping::Tex2DMSArray; break;
        case TextureType::Texture3D: descType = GLDescriptorMapping::Tex3D; break;
        case TextureType::TextureCube: descType = GLDescriptorMapping::TexCube; break;
        case TextureType::TextureCubeArray: descType = GLDescriptorMapping::TexCubeArray; break;
        case TextureType::Unknown:
        case TextureType::Count:
          RDCERR("Invalid resource type on binding %s", refl->readOnlyResources[i].name.c_str());
          break;
      }

      access.type = DescriptorType::ImageSampler;
      if(descType == GLDescriptorMapping::TexBuffer)
        access.type = DescriptorType::TypedBuffer;
      access.index = i;
      access.byteOffset = EncodeGLDescriptorIndex({descType, slot});
      m_Access.push_back(access);

      // checking texture completeness is a pretty expensive operation since it requires a lot of
      // queries against the driver's texture properties.
      // We assume that if a texture and sampler are complete at any point, even if their
      // properties change mid-frame they will stay complete. Similarly if they are _incomplete_
      // they will stay incomplete. Thus we can cache the results for a given pair, which if
      // samplers don't change (or are only ever used consistently with the same texture) amounts
      // to one entry per texture.
      // Note that textures can't change target, so we don't need to icnlude the target in the key
      drv.glActiveTexture(GLenum(eGL_TEXTURE0 + slot));

      GLenum binding = eGL_NONE;
      switch(refl->readOnlyResources[i].textureType)
      {
        case TextureType::Unknown: binding = eGL_NONE; break;
        case TextureType::Buffer: binding = eGL_TEXTURE_BINDING_BUFFER; break;
        case TextureType::Texture1D: binding = eGL_TEXTURE_BINDING_1D; break;
        case TextureType::Texture1DArray: binding = eGL_TEXTURE_BINDING_1D_ARRAY; break;
        case TextureType::Texture2D: binding = eGL_TEXTURE_BINDING_2D; break;
        case TextureType::TextureRect: binding = eGL_TEXTURE_BINDING_RECTANGLE; break;
        case TextureType::Texture2DArray: binding = eGL_TEXTURE_BINDING_2D_ARRAY; break;
        case TextureType::Texture2DMS: binding = eGL_TEXTURE_BINDING_2D_MULTISAMPLE; break;
        case TextureType::Texture2DMSArray:
          binding = eGL_TEXTURE_BINDING_2D_MULTISAMPLE_ARRAY;
          break;
        case TextureType::Texture3D: binding = eGL_TEXTURE_BINDING_3D; break;
        case TextureType::TextureCube: binding = eGL_TEXTURE_BINDING_CUBE_MAP; break;
        case TextureType::TextureCubeArray: binding = eGL_TEXTURE_BINDING_CUBE_MAP_ARRAY; break;
        case TextureType::Count: RDCERR("Invalid shader resource type"); break;
      }

      GLuint tex = 0;

      if(descType == GLDescriptorMapping::TexCubeArray && !HasExt[ARB_texture_cube_map_array])
        tex = 0;
      else
        drv.glGetIntegerv(binding, (GLint *)&tex);

      GLuint samp = 0;
      if(HasExt[ARB_sampler_objects])
        drv.glGetIntegerv(eGL_SAMPLER_BINDING, (GLint *)&samp);

      CompleteCacheKey complete = {tex, samp};

      auto it = m_CompleteCache.find(complete);
      if(it == m_CompleteCache.end())
        it = m_CompleteCache.insert(
            it,
            std::make_pair(complete, GetTextureCompleteStatus(TextureTarget(binding), tex, samp)));
      if(!it->second.empty())
      {
        GLPipe::TextureCompleteness completeness;
        completeness.descriptorByteOffset = access.byteOffset;
        completeness.completeStatus = it->second;
        pipe.textureCompleteness.push_back(completeness);
      }
    }

    RDCASSERT(refl->readWriteResources.size() < 0xffff, refl->readWriteResources.size());
    for(uint16_t i = 0; i < refl->readWriteResources.size(); i++)
    {
      uint32_t slot = 0;
      bool used = false;
      GetCurrentBinding(prog, refl, refl->readWriteResources[i], slot, used);

      access.staticallyUnused = !used;

      GLDescriptorMapping descType = GLDescriptorMapping::Images;

      if(refl->readWriteResources[i].isTexture)
      {
        access.type = DescriptorType::ReadWriteImage;
        if(refl->readWriteResources[i].textureType == TextureType::Buffer)
          access.type = DescriptorType::ReadWriteTypedBuffer;
      }
      else
      {
        access.type = DescriptorType::ReadWriteBuffer;
        descType = GLDescriptorMapping::ShaderStorage;

        if(refl->readWriteResources[i].variableType.rows == 1 &&
           refl->readWriteResources[i].variableType.columns == 1 &&
           refl->readWriteResources[i].variableType.baseType == VarType::UInt)
        {
          descType = GLDescriptorMapping::AtomicCounter;
        }
      }

      access.index = i;
      access.byteOffset = EncodeGLDescriptorIndex({descType, slot});
      m_Access.push_back(access);
    }
  }

  // GL is ass-backwards in its handling of texture units. When a shader is active
  // the types in the glsl samplers inform which targets are used from which texture units
  //
  // So texture unit 5 can have a 2D bound (texture 52) and a Cube bound (texture 77).
  // * if a uniform sampler2D has value 5 then the 2D texture is used, and we sample from 52
  // * if a uniform samplerCube has value 5 then the Cube texture is used, and we sample from 77
  // It's illegal for both a sampler2D and samplerCube to both have the same value (or any two
  // different types). It makes it all rather pointless and needlessly complex.
  //
  // What we have to do then, is consider the program, look at the values of the uniforms, and
  // then check if two uniforms with different types point to the same binding
  for(uint32_t unit = 0; unit < (uint32_t)numTexUnits; unit++)
  {
    rdcstr typeConflict;
    GLenum binding = eGL_NONE;
    GLenum target = eGL_NONE;
    TextureType resType = TextureType::Unknown;
    rdcstr firstBindName;

    rdcarray<uint32_t> descriptorsReferenced;

    for(const DescriptorAccess &access : m_Access)
    {
      // only look at read-only descriptors, these are the texture units that can clash
      if(!IsReadOnlyDescriptor(access.type))
        continue;

      const ShaderReflection *refl = refls[(uint32_t)access.stage];
      if(refl == NULL)
      {
        RDCERR("Unexpected NULL reflection on %s shader with a descriptor access",
               ToStr(access.stage).c_str());
        continue;
      }

      uint32_t accessedUnit = DecodeGLDescriptorIndex(access.byteOffset).idx;

      // accessed the same unit, check its binding
      if(accessedUnit == unit)
      {
        if(!descriptorsReferenced.contains(access.byteOffset))
          descriptorsReferenced.push_back(access.byteOffset);

        const ShaderResource &res = refl->readOnlyResources[access.index];
        GLenum t = eGL_NONE;

        switch(res.textureType)
        {
          case TextureType::Unknown: target = eGL_NONE; break;
          case TextureType::Buffer: target = eGL_TEXTURE_BUFFER; break;
          case TextureType::Texture1D: target = eGL_TEXTURE_1D; break;
          case TextureType::Texture1DArray: target = eGL_TEXTURE_1D_ARRAY; break;
          case TextureType::Texture2D: target = eGL_TEXTURE_2D; break;
          case TextureType::TextureRect: target = eGL_TEXTURE_RECTANGLE; break;
          case TextureType::Texture2DArray: target = eGL_TEXTURE_2D_ARRAY; break;
          case TextureType::Texture2DMS: target = eGL_TEXTURE_2D_MULTISAMPLE; break;
          case TextureType::Texture2DMSArray: target = eGL_TEXTURE_2D_MULTISAMPLE_ARRAY; break;
          case TextureType::Texture3D: target = eGL_TEXTURE_3D; break;
          case TextureType::TextureCube: target = eGL_TEXTURE_CUBE_MAP; break;
          case TextureType::TextureCubeArray: target = eGL_TEXTURE_CUBE_MAP_ARRAY; break;
          case TextureType::Count: RDCERR("Invalid shader resource type"); break;
        }

        if(target != eGL_NONE)
          t = TextureBinding(target);

        if(binding == eGL_NONE)
        {
          binding = t;
          firstBindName = res.name;
          resType = res.textureType;
        }
        else if(binding == t)
        {
          // two uniforms with the same type pointing to the same slot is fine
          binding = t;
        }
        else if(binding != t)
        {
          RDCERR("Two uniforms pointing to texture unit %d with types %s and %s", unit,
                 ToStr(binding).c_str(), ToStr(t).c_str());

          if(typeConflict.empty())
          {
            typeConflict = StringFormat::Fmt("First binding found '%s' is %s",
                                             firstBindName.c_str(), ToStr(resType).c_str());
          }

          typeConflict +=
              StringFormat::Fmt(", '%s' is %s", res.name.c_str(), ToStr(res.textureType).c_str());
        }
      }
    }

    // if we found a type conflict, add an entry for all descriptors
    if(!typeConflict.empty())
    {
      for(uint32_t descriptor : descriptorsReferenced)
      {
        bool found = false;
        for(GLPipe::TextureCompleteness &completeness : pipe.textureCompleteness)
        {
          if(completeness.descriptorByteOffset == descriptor)
          {
            // don't worry about overwriting, the descriptor byte offset is unique to the unit so
            // we should only set this at most once
            completeness.typeConflict = typeConflict;
            found = true;
          }
        }

        if(!found)
        {
          GLPipe::TextureCompleteness completeness;
          completeness.descriptorByteOffset = descriptor;
          completeness.typeConflict = typeConflict;
          pipe.textureCompleteness.push_back(completeness);
        }
      }
    }
  }

  RDCEraseEl(pipe.transformFeedback);

  if(HasExt[ARB_transform_feedback2])
  {
    GLuint feedback = 0;
    drv.glGetIntegerv(eGL_TRANSFORM_FEEDBACK_BINDING, (GLint *)&feedback);

    if(feedback != 0)
      pipe.transformFeedback.feedbackResourceId = rm->GetResID(FeedbackRes(ctx, feedback));
    else
      pipe.transformFeedback.feedbackResourceId = ResourceId();

    GLint maxCount = 0;
    drv.glGetIntegerv(eGL_MAX_TRANSFORM_FEEDBACK_SEPARATE_ATTRIBS, &maxCount);

    for(int i = 0; i < (int)ARRAY_COUNT(pipe.transformFeedback.bufferResourceId) && i < maxCount; i++)
    {
      GLuint buffer = 0;
      drv.glGetIntegeri_v(eGL_TRANSFORM_FEEDBACK_BUFFER_BINDING, i, (GLint *)&buffer);
      pipe.transformFeedback.bufferResourceId[i] = rm->GetResID(BufferRes(ctx, buffer));
      drv.glGetInteger64i_v(eGL_TRANSFORM_FEEDBACK_BUFFER_START, i,
                            (GLint64 *)&pipe.transformFeedback.byteOffset[i]);
      drv.glGetInteger64i_v(eGL_TRANSFORM_FEEDBACK_BUFFER_SIZE, i,
                            (GLint64 *)&pipe.transformFeedback.byteSize[i]);
    }

    GLint p = 0;
    drv.glGetIntegerv(eGL_TRANSFORM_FEEDBACK_BUFFER_PAUSED, &p);
    pipe.transformFeedback.paused = (p != 0);

    drv.glGetIntegerv(eGL_TRANSFORM_FEEDBACK_BUFFER_ACTIVE, &p);
    pipe.transformFeedback.active = (p != 0) || m_pDriver->m_WasActiveFeedback;
  }

  for(size_t i = 0; i < ARRAY_COUNT(rs.Subroutines); i++)
  {
    size_t num = RDCMIN(128, rs.Subroutines[i].numSubroutines);
    if(num == 0)
    {
      RDCEraseEl(stages[i]->subroutines);
    }
    else
    {
      stages[i]->subroutines.resize(num);
      memcpy(stages[i]->subroutines.data(), rs.Subroutines[i].Values, num * sizeof(uint32_t));
    }
  }

  drv.glActiveTexture(activeTexture);

  // Vertex post processing and rasterization

  RDCCOMPILE_ASSERT(ARRAY_COUNT(rs.Viewports) == ARRAY_COUNT(rs.DepthRanges),
                    "GL Viewport count does not match depth ranges count");
  pipe.rasterizer.viewports.resize(ARRAY_COUNT(rs.Viewports));
  for(size_t v = 0; v < pipe.rasterizer.viewports.size(); ++v)
  {
    pipe.rasterizer.viewports[v].x = rs.Viewports[v].x;
    pipe.rasterizer.viewports[v].y = rs.Viewports[v].y;
    pipe.rasterizer.viewports[v].width = rs.Viewports[v].width;
    pipe.rasterizer.viewports[v].height = rs.Viewports[v].height;
    pipe.rasterizer.viewports[v].minDepth = (float)rs.DepthRanges[v].nearZ;
    pipe.rasterizer.viewports[v].maxDepth = (float)rs.DepthRanges[v].farZ;
  }

  pipe.rasterizer.scissors.resize(ARRAY_COUNT(rs.Scissors));
  for(size_t s = 0; s < pipe.rasterizer.scissors.size(); ++s)
  {
    pipe.rasterizer.scissors[s].x = rs.Scissors[s].x;
    pipe.rasterizer.scissors[s].y = rs.Scissors[s].y;
    pipe.rasterizer.scissors[s].width = rs.Scissors[s].width;
    pipe.rasterizer.scissors[s].height = rs.Scissors[s].height;
    pipe.rasterizer.scissors[s].enabled = rs.Scissors[s].enabled;
  }

  int polygonOffsetEnableEnum;
  switch(rs.PolygonMode)
  {
    default:
      RDCWARN("Unexpected value for POLYGON_MODE %x", rs.PolygonMode);
      DELIBERATE_FALLTHROUGH();
    case eGL_FILL:
      pipe.rasterizer.state.fillMode = FillMode::Solid;
      polygonOffsetEnableEnum = GLRenderState::eEnabled_PolyOffsetFill;
      break;
    case eGL_LINE:
      pipe.rasterizer.state.fillMode = FillMode::Wireframe;
      polygonOffsetEnableEnum = GLRenderState::eEnabled_PolyOffsetLine;
      break;
    case eGL_POINT:
      pipe.rasterizer.state.fillMode = FillMode::Point;
      polygonOffsetEnableEnum = GLRenderState::eEnabled_PolyOffsetPoint;
      break;
  }
  if(rs.Enabled[polygonOffsetEnableEnum])
  {
    pipe.rasterizer.state.depthBias = rs.PolygonOffset[1];
    pipe.rasterizer.state.slopeScaledDepthBias = rs.PolygonOffset[0];
    pipe.rasterizer.state.offsetClamp = rs.PolygonOffset[2];
  }
  else
  {
    pipe.rasterizer.state.depthBias = 0.0f;
    pipe.rasterizer.state.slopeScaledDepthBias = 0.0f;
    pipe.rasterizer.state.offsetClamp = 0.0f;
  }

  if(rs.Enabled[GLRenderState::eEnabled_CullFace])
  {
    switch(rs.CullFace)
    {
      default: RDCWARN("Unexpected value for CULL_FACE %x", rs.CullFace); DELIBERATE_FALLTHROUGH();
      case eGL_BACK: pipe.rasterizer.state.cullMode = CullMode::Back; break;
      case eGL_FRONT: pipe.rasterizer.state.cullMode = CullMode::Front; break;
      case eGL_FRONT_AND_BACK: pipe.rasterizer.state.cullMode = CullMode::FrontAndBack; break;
    }
  }
  else
  {
    pipe.rasterizer.state.cullMode = CullMode::NoCull;
  }

  RDCASSERT(rs.FrontFace == eGL_CCW || rs.FrontFace == eGL_CW);
  pipe.rasterizer.state.frontCCW = rs.FrontFace == eGL_CCW;
  pipe.rasterizer.state.depthClamp = rs.Enabled[GLRenderState::eEnabled_DepthClamp];

  pipe.rasterizer.state.multisampleEnable = rs.Enabled[GLRenderState::eEnabled_Multisample];
  pipe.rasterizer.state.sampleShading = rs.Enabled[GLRenderState::eEnabled_SampleShading];
  pipe.rasterizer.state.sampleMask = rs.Enabled[GLRenderState::eEnabled_SampleMask];
  pipe.rasterizer.state.sampleMaskValue =
      rs.SampleMask[0];    // assume number of samples is less than 32
  pipe.rasterizer.state.sampleCoverage = rs.Enabled[GLRenderState::eEnabled_SampleCoverage];
  pipe.rasterizer.state.sampleCoverageInvert = rs.SampleCoverageInvert;
  pipe.rasterizer.state.sampleCoverageValue = rs.SampleCoverage;
  pipe.rasterizer.state.alphaToCoverage = rs.Enabled[GLRenderState::eEnabled_SampleAlphaToCoverage];
  pipe.rasterizer.state.alphaToOne = rs.Enabled[GLRenderState::eEnabled_SampleAlphaToOne];
  pipe.rasterizer.state.minSampleShadingRate = rs.MinSampleShading;

  pipe.rasterizer.state.programmablePointSize = rs.Enabled[rs.eEnabled_ProgramPointSize];
  pipe.rasterizer.state.pointSize = rs.PointSize;
  pipe.rasterizer.state.lineWidth = rs.LineWidth;
  pipe.rasterizer.state.pointFadeThreshold = rs.PointFadeThresholdSize;
  pipe.rasterizer.state.pointOriginUpperLeft = (rs.PointSpriteOrigin != eGL_LOWER_LEFT);

  // depth and stencil states

  pipe.depthState.depthEnable = rs.Enabled[GLRenderState::eEnabled_DepthTest];
  pipe.depthState.depthWrites = rs.DepthWriteMask != 0;
  pipe.depthState.depthFunction = MakeCompareFunc(rs.DepthFunc);

  pipe.depthState.depthBounds = rs.Enabled[GLRenderState::eEnabled_DepthBoundsEXT];
  pipe.depthState.nearBound = rs.DepthBounds.nearZ;
  pipe.depthState.farBound = rs.DepthBounds.farZ;

  pipe.stencilState.stencilEnable = rs.Enabled[GLRenderState::eEnabled_StencilTest];
  pipe.stencilState.frontFace.compareMask = rs.StencilFront.valuemask;
  pipe.stencilState.frontFace.writeMask = rs.StencilFront.writemask;
  pipe.stencilState.frontFace.reference = uint8_t(rs.StencilFront.ref & 0xff);
  pipe.stencilState.frontFace.function = MakeCompareFunc(rs.StencilFront.func);
  pipe.stencilState.frontFace.passOperation = MakeStencilOp(rs.StencilFront.pass);
  pipe.stencilState.frontFace.failOperation = MakeStencilOp(rs.StencilFront.stencilFail);
  pipe.stencilState.frontFace.depthFailOperation = MakeStencilOp(rs.StencilFront.depthFail);
  pipe.stencilState.backFace.compareMask = rs.StencilBack.valuemask;
  pipe.stencilState.backFace.writeMask = rs.StencilBack.writemask;
  pipe.stencilState.backFace.reference = uint8_t(rs.StencilBack.ref & 0xff);
  pipe.stencilState.backFace.function = MakeCompareFunc(rs.StencilBack.func);
  pipe.stencilState.backFace.passOperation = MakeStencilOp(rs.StencilBack.pass);
  pipe.stencilState.backFace.failOperation = MakeStencilOp(rs.StencilBack.stencilFail);
  pipe.stencilState.backFace.depthFailOperation = MakeStencilOp(rs.StencilBack.depthFail);

  // Frame buffer

  GLuint curDrawFBO = 0;
  drv.glGetIntegerv(eGL_DRAW_FRAMEBUFFER_BINDING, (GLint *)&curDrawFBO);
  GLuint curReadFBO = 0;
  drv.glGetIntegerv(eGL_READ_FRAMEBUFFER_BINDING, (GLint *)&curReadFBO);

  GLint numCols = 8;
  drv.glGetIntegerv(eGL_MAX_COLOR_ATTACHMENTS, &numCols);

  bool rbCol[32] = {false};
  bool rbDepth = false;
  bool rbStencil = false;
  GLuint curCol[32] = {0};
  GLuint curDepth = 0;
  GLuint curStencil = 0;

  RDCASSERT(numCols <= 32);

  // we should never bind the true default framebuffer - if the app did, we will have our fake bound
  RDCASSERT(curDrawFBO != 0);
  RDCASSERT(curReadFBO != 0);

  {
    GLenum type = eGL_TEXTURE;
    for(GLint i = 0; i < numCols; i++)
    {
      drv.glGetFramebufferAttachmentParameteriv(
          eGL_DRAW_FRAMEBUFFER, GLenum(eGL_COLOR_ATTACHMENT0 + i),
          eGL_FRAMEBUFFER_ATTACHMENT_OBJECT_NAME, (GLint *)&curCol[i]);
      drv.glGetFramebufferAttachmentParameteriv(
          eGL_DRAW_FRAMEBUFFER, GLenum(eGL_COLOR_ATTACHMENT0 + i),
          eGL_FRAMEBUFFER_ATTACHMENT_OBJECT_TYPE, (GLint *)&type);
      if(type == eGL_RENDERBUFFER)
        rbCol[i] = true;
    }

    drv.glGetFramebufferAttachmentParameteriv(eGL_DRAW_FRAMEBUFFER, eGL_DEPTH_ATTACHMENT,
                                              eGL_FRAMEBUFFER_ATTACHMENT_OBJECT_NAME,
                                              (GLint *)&curDepth);
    drv.glGetFramebufferAttachmentParameteriv(eGL_DRAW_FRAMEBUFFER, eGL_DEPTH_ATTACHMENT,
                                              eGL_FRAMEBUFFER_ATTACHMENT_OBJECT_TYPE, (GLint *)&type);
    if(type == eGL_RENDERBUFFER)
      rbDepth = true;
    drv.glGetFramebufferAttachmentParameteriv(eGL_DRAW_FRAMEBUFFER, eGL_STENCIL_ATTACHMENT,
                                              eGL_FRAMEBUFFER_ATTACHMENT_OBJECT_NAME,
                                              (GLint *)&curStencil);
    drv.glGetFramebufferAttachmentParameteriv(eGL_DRAW_FRAMEBUFFER, eGL_STENCIL_ATTACHMENT,
                                              eGL_FRAMEBUFFER_ATTACHMENT_OBJECT_TYPE, (GLint *)&type);
    if(type == eGL_RENDERBUFFER)
      rbStencil = true;

    pipe.framebuffer.drawFBO.resourceId = rm->GetResID(FramebufferRes(ctx, curDrawFBO));
    pipe.framebuffer.drawFBO.colorAttachments.resize(numCols);
    for(GLint i = 0; i < numCols; i++)
    {
      ResourceId id =
          rm->GetResID(rbCol[i] ? RenderbufferRes(ctx, curCol[i]) : TextureRes(ctx, curCol[i]));

      pipe.framebuffer.drawFBO.colorAttachments[i].resource = id;
      // A GL framebuffer attachment selects exactly one mip. Initialise the descriptor range on
      // every event, including when a texture view is replaced by a renderbuffer in this slot.
      pipe.framebuffer.drawFBO.colorAttachments[i].firstMip = 0;
      pipe.framebuffer.drawFBO.colorAttachments[i].firstSlice = 0;
      pipe.framebuffer.drawFBO.colorAttachments[i].numMips = id != ResourceId() ? 1 : 0;

      if(id != ResourceId())
        pipe.framebuffer.drawFBO.colorAttachments[i].format = GetTexture(id).format;

      GLenum attachment = GLenum(eGL_COLOR_ATTACHMENT0 + i);

      if(pipe.framebuffer.drawFBO.colorAttachments[i].resource != ResourceId() && !rbCol[i])
        GetFramebufferMipAndLayer(curDrawFBO, attachment,
                                  &pipe.framebuffer.drawFBO.colorAttachments[i].firstMip,
                                  &pipe.framebuffer.drawFBO.colorAttachments[i].firstSlice);

      pipe.framebuffer.drawFBO.colorAttachments[i].numSlices = 1;

      if(!rbCol[i] && id != ResourceId())
      {
        // desktop GL allows layered attachments which attach all slices from 0 to N
        if(!IsGLES)
        {
          GLint layered = 0;
          GL.glGetNamedFramebufferAttachmentParameterivEXT(
              curDrawFBO, attachment, eGL_FRAMEBUFFER_ATTACHMENT_LAYERED, &layered);

          if(layered)
          {
            pipe.framebuffer.drawFBO.colorAttachments[i].numSlices =
                m_pDriver->m_Textures[id].depth & 0xffff;
          }
        }
        else
        {
          // on GLES there's an OVR extension that allows attaching multiple layers
          if(HasExt[OVR_multiview])
          {
            GLint numViews = 0, startView = 0;
            GL.glGetNamedFramebufferAttachmentParameterivEXT(
                curDrawFBO, attachment, eGL_FRAMEBUFFER_ATTACHMENT_TEXTURE_NUM_VIEWS_OVR, &numViews);
            GL.glGetNamedFramebufferAttachmentParameterivEXT(
                curDrawFBO, attachment, eGL_FRAMEBUFFER_ATTACHMENT_TEXTURE_BASE_VIEW_INDEX_OVR,
                &startView);

            if(numViews > 1)
            {
              pipe.framebuffer.drawFBO.colorAttachments[i].numSlices = numViews & 0xffff;
              pipe.framebuffer.drawFBO.colorAttachments[i].firstSlice = startView & 0xffff;
            }
          }
        }
      }

      GLenum swizzles[4] = {eGL_RED, eGL_GREEN, eGL_BLUE, eGL_ALPHA};
      if(!rbCol[i] && id != ResourceId() &&
         (HasExt[ARB_texture_swizzle] || HasExt[EXT_texture_swizzle]))
      {
        GLenum target = m_pDriver->m_Textures[id].curType;
        GetTextureSwizzle(curCol[i], target, swizzles);
      }

      pipe.framebuffer.drawFBO.colorAttachments[i].swizzle.red = MakeSwizzle(swizzles[0]);
      pipe.framebuffer.drawFBO.colorAttachments[i].swizzle.green = MakeSwizzle(swizzles[1]);
      pipe.framebuffer.drawFBO.colorAttachments[i].swizzle.blue = MakeSwizzle(swizzles[2]);
      pipe.framebuffer.drawFBO.colorAttachments[i].swizzle.alpha = MakeSwizzle(swizzles[3]);
    }

    ResourceId id =
        rm->GetResID(rbDepth ? RenderbufferRes(ctx, curDepth) : TextureRes(ctx, curDepth));
    pipe.framebuffer.drawFBO.depthAttachment.resource = id;
    pipe.framebuffer.drawFBO.stencilAttachment.resource =
        rm->GetResID(rbStencil ? RenderbufferRes(ctx, curStencil) : TextureRes(ctx, curStencil));
    pipe.framebuffer.drawFBO.depthAttachment.firstMip = 0;
    pipe.framebuffer.drawFBO.depthAttachment.firstSlice = 0;
    pipe.framebuffer.drawFBO.depthAttachment.numMips = id != ResourceId() ? 1 : 0;
    pipe.framebuffer.drawFBO.stencilAttachment.firstMip = 0;
    pipe.framebuffer.drawFBO.stencilAttachment.firstSlice = 0;
    pipe.framebuffer.drawFBO.stencilAttachment.numMips =
        pipe.framebuffer.drawFBO.stencilAttachment.resource != ResourceId() ? 1 : 0;

    if(pipe.framebuffer.drawFBO.depthAttachment.resource != ResourceId() && !rbDepth)
      GetFramebufferMipAndLayer(curDrawFBO, eGL_DEPTH_ATTACHMENT,
                                &pipe.framebuffer.drawFBO.depthAttachment.firstMip,
                                &pipe.framebuffer.drawFBO.depthAttachment.firstSlice);

    if(pipe.framebuffer.drawFBO.stencilAttachment.resource != ResourceId() && !rbStencil)
      GetFramebufferMipAndLayer(curDrawFBO, eGL_STENCIL_ATTACHMENT,
                                &pipe.framebuffer.drawFBO.stencilAttachment.firstMip,
                                &pipe.framebuffer.drawFBO.stencilAttachment.firstSlice);

    pipe.framebuffer.drawFBO.depthAttachment.numSlices = 1;
    pipe.framebuffer.drawFBO.stencilAttachment.numSlices = 1;

    if(!rbDepth && id != ResourceId())
    {
      // desktop GL allows layered attachments which attach all slices from 0 to N
      if(!IsGLES)
      {
        GLint layered = 0;
        GL.glGetNamedFramebufferAttachmentParameterivEXT(
            curDrawFBO, eGL_DEPTH_ATTACHMENT, eGL_FRAMEBUFFER_ATTACHMENT_LAYERED, &layered);

        if(layered)
        {
          pipe.framebuffer.drawFBO.depthAttachment.numSlices =
              m_pDriver->m_Textures[id].depth & 0xffff;
        }
      }
      else
      {
        // on GLES there's an OVR extension that allows attaching multiple layers
        if(HasExt[OVR_multiview])
        {
          GLint numViews = 0, startView = 0;
          GL.glGetNamedFramebufferAttachmentParameterivEXT(
              curDrawFBO, eGL_DEPTH_ATTACHMENT, eGL_FRAMEBUFFER_ATTACHMENT_TEXTURE_NUM_VIEWS_OVR,
              &numViews);
          GL.glGetNamedFramebufferAttachmentParameterivEXT(
              curDrawFBO, eGL_DEPTH_ATTACHMENT,
              eGL_FRAMEBUFFER_ATTACHMENT_TEXTURE_BASE_VIEW_INDEX_OVR, &startView);

          if(numViews > 1)
          {
            pipe.framebuffer.drawFBO.depthAttachment.numSlices = numViews & 0xffff;
            pipe.framebuffer.drawFBO.depthAttachment.firstSlice = startView & 0xffff;
          }
        }
      }

      if(pipe.framebuffer.drawFBO.stencilAttachment.resource ==
         pipe.framebuffer.drawFBO.depthAttachment.resource)
      {
        pipe.framebuffer.drawFBO.stencilAttachment.firstSlice =
            pipe.framebuffer.drawFBO.depthAttachment.firstSlice;
        pipe.framebuffer.drawFBO.stencilAttachment.numSlices =
            pipe.framebuffer.drawFBO.depthAttachment.numSlices;
      }
    }

    pipe.framebuffer.drawFBO.drawBuffers.resize(numCols);
    for(GLint i = 0; i < numCols; i++)
    {
      GLenum b = eGL_NONE;
      drv.glGetIntegerv(GLenum(eGL_DRAW_BUFFER0 + i), (GLint *)&b);
      if(b >= eGL_COLOR_ATTACHMENT0 && b <= GLenum(eGL_COLOR_ATTACHMENT0 + numCols))
        pipe.framebuffer.drawFBO.drawBuffers[i] = b - eGL_COLOR_ATTACHMENT0;
      else
        pipe.framebuffer.drawFBO.drawBuffers[i] = -1;
    }

    pipe.framebuffer.drawFBO.readBuffer = -1;
  }

  {
    GLenum type = eGL_TEXTURE;
    for(GLint i = 0; i < numCols; i++)
    {
      drv.glGetFramebufferAttachmentParameteriv(
          eGL_READ_FRAMEBUFFER, GLenum(eGL_COLOR_ATTACHMENT0 + i),
          eGL_FRAMEBUFFER_ATTACHMENT_OBJECT_NAME, (GLint *)&curCol[i]);
      drv.glGetFramebufferAttachmentParameteriv(
          eGL_READ_FRAMEBUFFER, GLenum(eGL_COLOR_ATTACHMENT0 + i),
          eGL_FRAMEBUFFER_ATTACHMENT_OBJECT_TYPE, (GLint *)&type);
      if(type == eGL_RENDERBUFFER)
        rbCol[i] = true;
    }

    drv.glGetFramebufferAttachmentParameteriv(eGL_READ_FRAMEBUFFER, eGL_DEPTH_ATTACHMENT,
                                              eGL_FRAMEBUFFER_ATTACHMENT_OBJECT_NAME,
                                              (GLint *)&curDepth);
    drv.glGetFramebufferAttachmentParameteriv(eGL_READ_FRAMEBUFFER, eGL_DEPTH_ATTACHMENT,
                                              eGL_FRAMEBUFFER_ATTACHMENT_OBJECT_TYPE, (GLint *)&type);
    if(type == eGL_RENDERBUFFER)
      rbDepth = true;
    drv.glGetFramebufferAttachmentParameteriv(eGL_READ_FRAMEBUFFER, eGL_STENCIL_ATTACHMENT,
                                              eGL_FRAMEBUFFER_ATTACHMENT_OBJECT_NAME,
                                              (GLint *)&curStencil);
    drv.glGetFramebufferAttachmentParameteriv(eGL_READ_FRAMEBUFFER, eGL_STENCIL_ATTACHMENT,
                                              eGL_FRAMEBUFFER_ATTACHMENT_OBJECT_TYPE, (GLint *)&type);
    if(type == eGL_RENDERBUFFER)
      rbStencil = true;

    pipe.framebuffer.readFBO.resourceId = rm->GetResID(FramebufferRes(ctx, curReadFBO));
    pipe.framebuffer.readFBO.colorAttachments.resize(numCols);
    for(GLint i = 0; i < numCols; i++)
    {
      pipe.framebuffer.readFBO.colorAttachments[i].resource =
          rm->GetResID(rbCol[i] ? RenderbufferRes(ctx, curCol[i]) : TextureRes(ctx, curCol[i]));

      if(pipe.framebuffer.readFBO.colorAttachments[i].resource != ResourceId() && !rbCol[i])
        GetFramebufferMipAndLayer(curReadFBO, GLenum(eGL_COLOR_ATTACHMENT0 + i),
                                  &pipe.framebuffer.readFBO.colorAttachments[i].firstMip,
                                  &pipe.framebuffer.readFBO.colorAttachments[i].firstSlice);
    }

    pipe.framebuffer.readFBO.depthAttachment.resource =
        rm->GetResID(rbDepth ? RenderbufferRes(ctx, curDepth) : TextureRes(ctx, curDepth));
    pipe.framebuffer.readFBO.stencilAttachment.resource =
        rm->GetResID(rbStencil ? RenderbufferRes(ctx, curStencil) : TextureRes(ctx, curStencil));

    if(pipe.framebuffer.readFBO.depthAttachment.resource != ResourceId() && !rbDepth)
      GetFramebufferMipAndLayer(curReadFBO, eGL_DEPTH_ATTACHMENT,
                                &pipe.framebuffer.readFBO.depthAttachment.firstMip,
                                &pipe.framebuffer.readFBO.depthAttachment.firstSlice);

    if(pipe.framebuffer.readFBO.stencilAttachment.resource != ResourceId() && !rbStencil)
      GetFramebufferMipAndLayer(curReadFBO, eGL_STENCIL_ATTACHMENT,
                                &pipe.framebuffer.readFBO.stencilAttachment.firstMip,
                                &pipe.framebuffer.readFBO.stencilAttachment.firstSlice);

    pipe.framebuffer.readFBO.drawBuffers.resize(numCols);
    for(GLint i = 0; i < numCols; i++)
      pipe.framebuffer.readFBO.drawBuffers[i] = -1;

    GLenum b = eGL_NONE;
    drv.glGetIntegerv(eGL_READ_BUFFER, (GLint *)&b);
    if(b >= eGL_COLOR_ATTACHMENT0 && b <= GLenum(eGL_COLOR_ATTACHMENT0 + numCols))
      pipe.framebuffer.drawFBO.readBuffer = b - eGL_COLOR_ATTACHMENT0;
    else
      pipe.framebuffer.drawFBO.readBuffer = -1;
  }

  pipe.framebuffer.blendState.blendFactor = rs.BlendColor;

  pipe.framebuffer.framebufferSRGB = rs.Enabled[GLRenderState::eEnabled_FramebufferSRGB];
  pipe.framebuffer.dither = rs.Enabled[GLRenderState::eEnabled_Dither];

  RDCCOMPILE_ASSERT(ARRAY_COUNT(rs.Blends) == ARRAY_COUNT(rs.ColorMasks),
                    "Color masks and blends mismatched");
  pipe.framebuffer.blendState.blends.resize(ARRAY_COUNT(rs.Blends));
  for(size_t i = 0; i < ARRAY_COUNT(rs.Blends); i++)
  {
    pipe.framebuffer.blendState.blends[i].enabled = rs.Blends[i].Enabled;
    pipe.framebuffer.blendState.blends[i].logicOperation = LogicOperation::NoOp;

    if(rs.LogicOp != eGL_NONE && rs.LogicOp != eGL_COPY)
      pipe.framebuffer.blendState.blends[i].logicOperation = MakeLogicOp(rs.LogicOp);

    pipe.framebuffer.blendState.blends[i].logicOperationEnabled =
        rs.Enabled[GLRenderState::eEnabled_ColorLogicOp];

    pipe.framebuffer.blendState.blends[i].colorBlend.source =
        MakeBlendMultiplier(rs.Blends[i].SourceRGB);
    pipe.framebuffer.blendState.blends[i].colorBlend.destination =
        MakeBlendMultiplier(rs.Blends[i].DestinationRGB);
    pipe.framebuffer.blendState.blends[i].colorBlend.operation =
        MakeBlendOp(rs.Blends[i].EquationRGB);

    pipe.framebuffer.blendState.blends[i].alphaBlend.source =
        MakeBlendMultiplier(rs.Blends[i].SourceAlpha);
    pipe.framebuffer.blendState.blends[i].alphaBlend.destination =
        MakeBlendMultiplier(rs.Blends[i].DestinationAlpha);
    pipe.framebuffer.blendState.blends[i].alphaBlend.operation =
        MakeBlendOp(rs.Blends[i].EquationAlpha);

    pipe.framebuffer.blendState.blends[i].writeMask = 0;
    if(rs.ColorMasks[i].red)
      pipe.framebuffer.blendState.blends[i].writeMask |= 1;
    if(rs.ColorMasks[i].green)
      pipe.framebuffer.blendState.blends[i].writeMask |= 2;
    if(rs.ColorMasks[i].blue)
      pipe.framebuffer.blendState.blends[i].writeMask |= 4;
    if(rs.ColorMasks[i].alpha)
      pipe.framebuffer.blendState.blends[i].writeMask |= 8;
  }

  switch(rs.Hints.Derivatives)
  {
    default:
    case eGL_DONT_CARE: pipe.hints.derivatives = QualityHint::DontCare; break;
    case eGL_NICEST: pipe.hints.derivatives = QualityHint::Nicest; break;
    case eGL_FASTEST: pipe.hints.derivatives = QualityHint::Fastest; break;
  }

  switch(rs.Hints.LineSmooth)
  {
    default:
    case eGL_DONT_CARE: pipe.hints.lineSmoothing = QualityHint::DontCare; break;
    case eGL_NICEST: pipe.hints.lineSmoothing = QualityHint::Nicest; break;
    case eGL_FASTEST: pipe.hints.lineSmoothing = QualityHint::Fastest; break;
  }

  switch(rs.Hints.PolySmooth)
  {
    default:
    case eGL_DONT_CARE: pipe.hints.polySmoothing = QualityHint::DontCare; break;
    case eGL_NICEST: pipe.hints.polySmoothing = QualityHint::Nicest; break;
    case eGL_FASTEST: pipe.hints.polySmoothing = QualityHint::Fastest; break;
  }

  switch(rs.Hints.TexCompression)
  {
    default:
    case eGL_DONT_CARE: pipe.hints.textureCompression = QualityHint::DontCare; break;
    case eGL_NICEST: pipe.hints.textureCompression = QualityHint::Nicest; break;
    case eGL_FASTEST: pipe.hints.textureCompression = QualityHint::Fastest; break;
  }

  pipe.hints.lineSmoothingEnabled = rs.Enabled[GLRenderState::eEnabled_LineSmooth];
  pipe.hints.polySmoothingEnabled = rs.Enabled[GLRenderState::eEnabled_PolySmooth];
}

rdcarray<Descriptor> GLReplay::GetDescriptors(ResourceId descriptorStore,
                                              const rdcarray<DescriptorRange> &ranges)
{
  rdcarray<Descriptor> ret;

  if(descriptorStore != m_pDriver->m_DescriptorsID)
  {
    RDCERR("Descriptors query for invalid descriptor store on fixed bindings API (OpenGL)");
    return ret;
  }

  MakeCurrentReplayContext(&m_ReplayCtx);
  WrappedOpenGL &drv = *m_pDriver;
  GLResourceManager *rm = m_pDriver->GetResourceManager();

  ContextPair &ctx = drv.GetCtx();

  GLRenderState rs;
  rs.FetchState(&drv);

  size_t count = 0;
  for(const DescriptorRange &r : ranges)
    count += r.count;
  ret.resize(count);

  size_t dst = 0;
  for(const DescriptorRange &r : ranges)
  {
    uint32_t descriptorId = r.offset;

    for(uint32_t i = 0; i < r.count; i++, dst++, descriptorId++)
    {
      GLDescriptorLocation idx = DecodeGLDescriptorIndex(descriptorId);

      if(idx.type == GLDescriptorMapping::Invalid)
      {
      }
      else if(idx.type == GLDescriptorMapping::BareUniforms)
      {
        // not feasible to emulate backing store for bare uniforms. We could synthesise our own byte
        // layout and query uniform values into a buffer but... let's not.
        ret[dst].type = DescriptorType::ConstantBuffer;
        ret[dst].byteSize = 16 * 1024;
      }
      else if(idx.type == GLDescriptorMapping::UniformBinding)
      {
        ret[dst].type = DescriptorType::ConstantBuffer;
        if(rs.UniformBinding[idx.idx].res.name != 0)
        {
          ResourceId id = rm->GetResID(rs.UniformBinding[idx.idx].res);
          ret[dst].resource = id;
          ret[dst].byteOffset = rs.UniformBinding[idx.idx].start;
          ret[dst].byteSize = rs.UniformBinding[idx.idx].size;

          if(ret[dst].byteSize == 0)
            ret[dst].byteSize = m_pDriver->m_Buffers[id].size;
        }
      }
      else if(idx.type == GLDescriptorMapping::AtomicCounter)
      {
        ret[dst].type = DescriptorType::ReadWriteBuffer;
        if(rs.AtomicCounter[idx.idx].res.name != 0)
        {
          ResourceId id = rm->GetResID(rs.AtomicCounter[idx.idx].res);
          ret[dst].resource = id;
          ret[dst].byteOffset = rs.AtomicCounter[idx.idx].start;
          ret[dst].byteSize = rs.AtomicCounter[idx.idx].size;

          if(ret[dst].byteSize == 0)
            ret[dst].byteSize = m_pDriver->m_Buffers[id].size;
        }
      }
      else if(idx.type == GLDescriptorMapping::ShaderStorage)
      {
        ret[dst].type = DescriptorType::ReadWriteBuffer;
        if(rs.ShaderStorage[idx.idx].res.name != 0)
        {
          ResourceId id = rm->GetResID(rs.ShaderStorage[idx.idx].res);
          ret[dst].resource = id;
          ret[dst].byteOffset = rs.ShaderStorage[idx.idx].start;
          ret[dst].byteSize = rs.ShaderStorage[idx.idx].size;

          if(ret[dst].byteSize == 0)
            ret[dst].byteSize = m_pDriver->m_Buffers[id].size;
        }
      }
      else if(idx.type == GLDescriptorMapping::Images)
      {
        ret[dst].type = DescriptorType::ReadWriteImage;
        if(rs.Images[idx.idx].res.name != 0)
        {
          ResourceId id = rm->GetResID(rs.Images[idx.idx].res);
          ret[dst].resource = id;
          ret[dst].firstMip = rs.Images[idx.idx].level & 0xff;
          ret[dst].numMips = 1;
          ret[dst].firstSlice = rs.Images[idx.idx].layer & 0xffff;
          if(rs.Images[idx.idx].access == eGL_READ_ONLY)
          {
            ret[dst].flags = DescriptorFlags::ReadOnlyAccess;
          }
          else if(rs.Images[idx.idx].access == eGL_WRITE_ONLY)
          {
            ret[dst].flags = DescriptorFlags::WriteOnlyAccess;
          }
          ret[dst].format = MakeResourceFormat(eGL_TEXTURE_2D, rs.Images[idx.idx].format);

          CacheTexture(id);

          ret[dst].textureType = m_CachedTextures[id].type;
          if(ret[dst].textureType == TextureType::Texture3D)
            ret[dst].numSlices = rs.Images[idx.idx].layered
                                     ? (m_CachedTextures[id].depth >> ret[dst].firstMip) & 0xffff
                                     : 1;
          else
            ret[dst].numSlices =
                rs.Images[idx.idx].layered ? m_CachedTextures[id].arraysize & 0xffff : 1;

          if(ret[dst].textureType == TextureType::Buffer)
            ret[dst].type = DescriptorType::ReadWriteTypedBuffer;
        }
      }
      else
      {
        ret[dst].type = DescriptorType::ImageSampler;
        if(idx.type == GLDescriptorMapping::TexBuffer)
          ret[dst].type = DescriptorType::TypedBuffer;

        drv.glActiveTexture(GLenum(eGL_TEXTURE0 + idx.idx));

        GLuint tex = 0;
        GLenum target = eGL_NONE;

        if(idx.type == GLDescriptorMapping::TexCubeArray && !HasExt[ARB_texture_cube_map_array])
        {
          tex = 0;
          target = eGL_TEXTURE_CUBE_MAP_ARRAY;
        }
        else
        {
          switch(idx.type)
          {
            case GLDescriptorMapping::Tex1D:
              target = eGL_TEXTURE_1D;
              ret[dst].textureType = TextureType::Texture1D;

              if(IsGLES)
                continue;
              break;
            case GLDescriptorMapping::Tex2D:
              target = eGL_TEXTURE_2D;
              ret[dst].textureType = TextureType::Texture2D;
              break;
            case GLDescriptorMapping::Tex3D:
              target = eGL_TEXTURE_3D;
              ret[dst].textureType = TextureType::Texture3D;
              break;
            case GLDescriptorMapping::Tex1DArray:
              target = eGL_TEXTURE_1D_ARRAY;
              ret[dst].textureType = TextureType::Texture1DArray;

              if(IsGLES)
                continue;
              break;
            case GLDescriptorMapping::Tex2DArray:
              target = eGL_TEXTURE_2D_ARRAY;
              ret[dst].textureType = TextureType::Texture2DArray;
              break;
            case GLDescriptorMapping::TexCubeArray:
              target = eGL_TEXTURE_CUBE_MAP_ARRAY;
              ret[dst].textureType = TextureType::TextureCubeArray;

              if(!HasExt[ARB_texture_cube_map_array])
                continue;
              break;
            case GLDescriptorMapping::TexRect:
              target = eGL_TEXTURE_RECTANGLE;
              ret[dst].textureType = TextureType::TextureRect;

              if(IsGLES)
                continue;
              break;
            case GLDescriptorMapping::TexBuffer:
              target = eGL_TEXTURE_BUFFER;
              ret[dst].textureType = TextureType::Buffer;

              if(!HasExt[ARB_texture_buffer_object])
                continue;
              break;
            case GLDescriptorMapping::TexCube:
              target = eGL_TEXTURE_CUBE_MAP;
              ret[dst].textureType = TextureType::TextureCube;
              break;
            case GLDescriptorMapping::Tex2DMS:
              target = eGL_TEXTURE_2D_MULTISAMPLE;
              ret[dst].textureType = TextureType::Texture2DMS;

              if(!HasExt[ARB_texture_multisample_no_array] && !HasExt[ARB_texture_multisample])
                continue;
              break;
            case GLDescriptorMapping::Tex2DMSArray:
              target = eGL_TEXTURE_2D_MULTISAMPLE_ARRAY;
              ret[dst].textureType = TextureType::Texture2DMSArray;

              if(!HasExt[ARB_texture_multisample])
                continue;
              break;
            case GLDescriptorMapping::AtomicCounter:
            case GLDescriptorMapping::ShaderStorage:
            case GLDescriptorMapping::BareUniforms:
            case GLDescriptorMapping::UniformBinding:
            case GLDescriptorMapping::Images:
            case GLDescriptorMapping::Invalid:
            case GLDescriptorMapping::Count: target = eGL_NONE; break;
          }
        }

        if(target == eGL_NONE)
          continue;

        GLenum binding = TextureBinding(target);

        drv.glGetIntegerv(binding, (GLint *)&tex);

        if(tex == 0)
          continue;

        GLint firstMip = 0, numMips = 1;

        if(target != eGL_TEXTURE_BUFFER)
        {
          drv.glGetTextureParameterivEXT(tex, target, eGL_TEXTURE_BASE_LEVEL, &firstMip);
          drv.glGetTextureParameterivEXT(tex, target, eGL_TEXTURE_MAX_LEVEL, &numMips);

          numMips = numMips - firstMip + 1;
        }

        ResourceId id = rm->GetResID(TextureRes(ctx, tex));
        ret[dst].resource = id;
        ret[dst].firstMip = firstMip & 0xff;
        ret[dst].numMips = numMips & 0xff;

        GLenum levelQueryType =
            target == eGL_TEXTURE_CUBE_MAP ? eGL_TEXTURE_CUBE_MAP_POSITIVE_X : target;
        GLenum fmt = eGL_NONE;
        drv.glGetTexLevelParameteriv(levelQueryType, 0, eGL_TEXTURE_INTERNAL_FORMAT, (GLint *)&fmt);
        ret[dst].format = MakeResourceFormat(target, fmt);
        if(IsDepthStencilFormat(fmt))
        {
          GLint depthMode = eGL_DEPTH_COMPONENT;

          if(HasExt[ARB_stencil_texturing])
            drv.glGetTextureParameterivEXT(tex, target, eGL_DEPTH_STENCIL_TEXTURE_MODE, &depthMode);

          ret[dst].format = ResourceFormat();
          ret[dst].format.type = ResourceFormatType::Regular;
          ret[dst].format.compByteWidth = 1;
          ret[dst].format.compCount = 1;
          if(depthMode == eGL_DEPTH_COMPONENT)
            ret[dst].format.compType = CompType::Depth;
          else if(depthMode == eGL_STENCIL_INDEX)
            ret[dst].format.compType = CompType::UInt;
        }

        GLenum swizzles[4] = {eGL_RED, eGL_GREEN, eGL_BLUE, eGL_ALPHA};
        if(target != eGL_TEXTURE_BUFFER &&
           (HasExt[ARB_texture_swizzle] || HasExt[EXT_texture_swizzle]))
          GetTextureSwizzle(tex, target, swizzles);

        ret[dst].swizzle.red = MakeSwizzle(swizzles[0]);
        ret[dst].swizzle.green = MakeSwizzle(swizzles[1]);
        ret[dst].swizzle.blue = MakeSwizzle(swizzles[2]);
        ret[dst].swizzle.alpha = MakeSwizzle(swizzles[3]);

        GLuint samp = 0;
        if(HasExt[ARB_sampler_objects])
          drv.glGetIntegerv(eGL_SAMPLER_BINDING, (GLint *)&samp);

        ret[dst].secondary = rm->GetResID(SamplerRes(ctx, samp));
      }
    }
  }

  return ret;
}

rdcarray<SamplerDescriptor> GLReplay::GetSamplerDescriptors(ResourceId descriptorStore,
                                                            const rdcarray<DescriptorRange> &ranges)
{
  rdcarray<SamplerDescriptor> ret;

  if(descriptorStore != m_pDriver->m_DescriptorsID)
  {
    RDCERR("Descriptors query for invalid descriptor store on fixed bindings API (OpenGL)");
    return ret;
  }

  MakeCurrentReplayContext(&m_ReplayCtx);
  WrappedOpenGL &drv = *m_pDriver;
  GLResourceManager *rm = m_pDriver->GetResourceManager();

  ContextPair &ctx = drv.GetCtx();

  size_t count = 0;
  for(const DescriptorRange &r : ranges)
    count += r.count;
  ret.resize(count);

  size_t dst = 0;
  for(const DescriptorRange &r : ranges)
  {
    uint32_t descriptorId = r.offset;

    for(uint32_t i = 0; i < r.count; i++, dst++, descriptorId++)
    {
      GLDescriptorLocation idx = DecodeGLDescriptorIndex(descriptorId);

      if(idx.type == GLDescriptorMapping::Invalid || idx.type == GLDescriptorMapping::Images ||
         idx.type == GLDescriptorMapping::AtomicCounter ||
         idx.type == GLDescriptorMapping::ShaderStorage ||
         idx.type == GLDescriptorMapping::UniformBinding)
        continue;

      drv.glActiveTexture(GLenum(eGL_TEXTURE0 + idx.idx));

      GLuint tex = 0;
      GLenum target = eGL_NONE;

      if(idx.type == GLDescriptorMapping::TexCubeArray && !HasExt[ARB_texture_cube_map_array])
      {
        tex = 0;
        target = eGL_TEXTURE_CUBE_MAP_ARRAY;
      }
      else
      {
        switch(idx.type)
        {
          case GLDescriptorMapping::Tex1D:
            target = eGL_TEXTURE_1D;

            if(IsGLES)
              continue;
            break;
          case GLDescriptorMapping::Tex2D: target = eGL_TEXTURE_2D; break;
          case GLDescriptorMapping::Tex3D: target = eGL_TEXTURE_3D; break;
          case GLDescriptorMapping::Tex1DArray:
            target = eGL_TEXTURE_1D_ARRAY;

            if(IsGLES)
              continue;
            break;
          case GLDescriptorMapping::Tex2DArray: target = eGL_TEXTURE_2D_ARRAY; break;
          case GLDescriptorMapping::TexCubeArray:
            target = eGL_TEXTURE_CUBE_MAP_ARRAY;

            if(!HasExt[ARB_texture_cube_map_array])
              continue;
            break;
          case GLDescriptorMapping::TexRect:
            target = eGL_TEXTURE_RECTANGLE;

            if(IsGLES)
              continue;
            break;
          case GLDescriptorMapping::TexBuffer:
            target = eGL_TEXTURE_BUFFER;

            if(!HasExt[ARB_texture_buffer_object])
              continue;
            break;
          case GLDescriptorMapping::TexCube: target = eGL_TEXTURE_CUBE_MAP; break;
          case GLDescriptorMapping::Tex2DMS:
            target = eGL_TEXTURE_2D_MULTISAMPLE;

            if(!HasExt[ARB_texture_multisample_no_array] && !HasExt[ARB_texture_multisample])
              continue;
            break;
          case GLDescriptorMapping::Tex2DMSArray:
            target = eGL_TEXTURE_2D_MULTISAMPLE_ARRAY;

            if(!HasExt[ARB_texture_multisample])
              continue;
            break;
          case GLDescriptorMapping::AtomicCounter:
          case GLDescriptorMapping::ShaderStorage:
          case GLDescriptorMapping::BareUniforms:
          case GLDescriptorMapping::UniformBinding:
          case GLDescriptorMapping::Images:
          case GLDescriptorMapping::Invalid:
          case GLDescriptorMapping::Count: target = eGL_NONE; break;
        }
      }

      if(target == eGL_NONE)
        continue;

      GLenum binding = TextureBinding(target);

      GLuint samp = 0;
      if(HasExt[ARB_sampler_objects])
        drv.glGetIntegerv(eGL_SAMPLER_BINDING, (GLint *)&samp);

      drv.glGetIntegerv(binding, (GLint *)&tex);

      if(samp == 0 && tex == 0)
        continue;

      ret[dst].object = rm->GetResID(SamplerRes(ctx, samp));

      // GL has separate sampler objects but they don't exist as separate sampler descriptors
      ret[dst].type = DescriptorType::ImageSampler;

      if(target != eGL_TEXTURE_2D_MULTISAMPLE && target != eGL_TEXTURE_2D_MULTISAMPLE_ARRAY)
      {
        if(samp != 0)
          drv.glGetSamplerParameterfv(samp, eGL_TEXTURE_BORDER_COLOR,
                                      ret[dst].borderColorValue.floatValue.data());
        else
          drv.glGetTextureParameterfvEXT(tex, target, eGL_TEXTURE_BORDER_COLOR,
                                         ret[dst].borderColorValue.floatValue.data());

        ret[dst].borderColorType = CompType::Float;

        GLint v;
        v = 0;
        if(samp != 0)
          drv.glGetSamplerParameteriv(samp, eGL_TEXTURE_WRAP_S, &v);
        else
          drv.glGetTextureParameterivEXT(tex, target, eGL_TEXTURE_WRAP_S, &v);
        ret[dst].addressU = MakeAddressMode((GLenum)v);

        v = 0;
        if(samp != 0)
          drv.glGetSamplerParameteriv(samp, eGL_TEXTURE_WRAP_T, &v);
        else
          drv.glGetTextureParameterivEXT(tex, target, eGL_TEXTURE_WRAP_T, &v);
        ret[dst].addressV = MakeAddressMode((GLenum)v);

        v = 0;
        if(samp != 0)
          drv.glGetSamplerParameteriv(samp, eGL_TEXTURE_WRAP_R, &v);
        else
          drv.glGetTextureParameterivEXT(tex, target, eGL_TEXTURE_WRAP_R, &v);
        ret[dst].addressW = MakeAddressMode((GLenum)v);

        // GLES 3 is always seamless
        if(IsGLES && GLCoreVersion > 30)
        {
          ret[dst].seamlessCubemaps = true;
        }
        else if(!IsGLES)
        {
          // on GLES 2 this is always going to be false, GL has a toggle
          ret[dst].seamlessCubemaps = drv.glIsEnabled(eGL_TEXTURE_CUBE_MAP_SEAMLESS) != GL_FALSE;
        }

        v = 0;
        if(samp != 0)
          drv.glGetSamplerParameteriv(samp, eGL_TEXTURE_COMPARE_FUNC, &v);
        else
          drv.glGetTextureParameterivEXT(tex, target, eGL_TEXTURE_COMPARE_FUNC, &v);
        ret[dst].compareFunction = MakeCompareFunc((GLenum)v);

        GLint minf = 0;
        GLint magf = 0;
        if(samp != 0)
          drv.glGetSamplerParameteriv(samp, eGL_TEXTURE_MIN_FILTER, &minf);
        else
          drv.glGetTextureParameterivEXT(tex, target, eGL_TEXTURE_MIN_FILTER, &minf);

        if(samp != 0)
          drv.glGetSamplerParameteriv(samp, eGL_TEXTURE_MAG_FILTER, &magf);
        else
          drv.glGetTextureParameterivEXT(tex, target, eGL_TEXTURE_MAG_FILTER, &magf);

        if(HasExt[ARB_texture_filter_anisotropic])
        {
          if(samp != 0)
            drv.glGetSamplerParameterfv(samp, eGL_TEXTURE_MAX_ANISOTROPY, &ret[dst].maxAnisotropy);
          else
            drv.glGetTextureParameterfvEXT(tex, target, eGL_TEXTURE_MAX_ANISOTROPY,
                                           &ret[dst].maxAnisotropy);
        }
        else
        {
          ret[dst].maxAnisotropy = 0.0f;
        }

        ret[dst].filter = MakeFilter((GLenum)minf, (GLenum)magf, ret[dst].maxAnisotropy);

        v = 0;
        if(samp != 0)
          drv.glGetSamplerParameteriv(samp, eGL_TEXTURE_COMPARE_MODE, &v);
        else
          drv.glGetTextureParameterivEXT(tex, target, eGL_TEXTURE_COMPARE_MODE, &v);
        ret[dst].filter.filter = (GLenum)v == eGL_COMPARE_REF_TO_TEXTURE ? FilterFunction::Comparison
                                                                         : FilterFunction::Normal;

        if(samp != 0)
          drv.glGetSamplerParameterfv(samp, eGL_TEXTURE_MAX_LOD, &ret[dst].maxLOD);
        else
          drv.glGetTextureParameterfvEXT(tex, target, eGL_TEXTURE_MAX_LOD, &ret[dst].maxLOD);

        if(samp != 0)
          drv.glGetSamplerParameterfv(samp, eGL_TEXTURE_MIN_LOD, &ret[dst].minLOD);
        else
          drv.glGetTextureParameterfvEXT(tex, target, eGL_TEXTURE_MIN_LOD, &ret[dst].minLOD);

        if(!IsGLES)
        {
          if(samp != 0)
            drv.glGetSamplerParameterfv(samp, eGL_TEXTURE_LOD_BIAS, &ret[dst].mipBias);
          else
            drv.glGetTextureParameterfvEXT(tex, target, eGL_TEXTURE_LOD_BIAS, &ret[dst].mipBias);
        }
      }
    }
  }

  return ret;
}

rdcarray<DescriptorAccess> GLReplay::GetDescriptorAccess(uint32_t eventId)
{
  return m_Access;
}

rdcarray<DescriptorLogicalLocation> GLReplay::GetDescriptorLocations(
    ResourceId descriptorStore, const rdcarray<DescriptorRange> &ranges)
{
  rdcarray<DescriptorLogicalLocation> ret;

  if(descriptorStore != m_pDriver->m_DescriptorsID)
  {
    RDCERR("Descriptors query for invalid descriptor store on fixed bindings API (OpenGL)");
    return ret;
  }

  size_t count = 0;
  for(const DescriptorRange &r : ranges)
    count += r.count;
  ret.resize(count);

  size_t dst = 0;
  for(const DescriptorRange &r : ranges)
  {
    uint32_t descriptorByteOffset = r.offset;

    for(uint32_t i = 0; i < r.count; i++, dst++, descriptorByteOffset++)
    {
      DescriptorLogicalLocation &dstLoc = ret[dst];
      GLDescriptorLocation srcLoc = DecodeGLDescriptorIndex(descriptorByteOffset);

      const char *prefix = "Unknown";
      dstLoc.stageMask = ShaderStageMask::All;
      switch(srcLoc.type)
      {
        case GLDescriptorMapping::BareUniforms:
          prefix = "Uniforms";
          dstLoc.category = DescriptorCategory::ConstantBlock;
          break;
        case GLDescriptorMapping::UniformBinding:
          prefix = "UBO";
          dstLoc.category = DescriptorCategory::ConstantBlock;
          break;
        case GLDescriptorMapping::Tex1D:
          prefix = "Tex1D";
          dstLoc.category = DescriptorCategory::ReadOnlyResource;
          break;
        case GLDescriptorMapping::Tex2D:
          prefix = "Tex2D";
          dstLoc.category = DescriptorCategory::ReadOnlyResource;
          break;
        case GLDescriptorMapping::Tex3D:
          prefix = "Tex3D";
          dstLoc.category = DescriptorCategory::ReadOnlyResource;
          break;
        case GLDescriptorMapping::Tex1DArray:
          prefix = "Tex1DArray";
          dstLoc.category = DescriptorCategory::ReadOnlyResource;
          break;
        case GLDescriptorMapping::Tex2DArray:
          prefix = "Tex2DArray";
          dstLoc.category = DescriptorCategory::ReadOnlyResource;
          break;
        case GLDescriptorMapping::TexCubeArray:
          prefix = "TexCubeArray";
          dstLoc.category = DescriptorCategory::ReadOnlyResource;
          break;
        case GLDescriptorMapping::TexRect:
          prefix = "TexRect";
          dstLoc.category = DescriptorCategory::ReadOnlyResource;
          break;
        case GLDescriptorMapping::TexBuffer:
          prefix = "TexBuffer";
          dstLoc.category = DescriptorCategory::ReadOnlyResource;
          break;
        case GLDescriptorMapping::TexCube:
          prefix = "TexCube";
          dstLoc.category = DescriptorCategory::ReadOnlyResource;
          break;
        case GLDescriptorMapping::Tex2DMS:
          prefix = "Tex2DMS";
          dstLoc.category = DescriptorCategory::ReadOnlyResource;
          break;
        case GLDescriptorMapping::Tex2DMSArray:
          prefix = "Tex2DMSArray";
          dstLoc.category = DescriptorCategory::ReadOnlyResource;
          break;
        case GLDescriptorMapping::Images:
          prefix = "Image";
          dstLoc.category = DescriptorCategory::ReadWriteResource;
          break;
        case GLDescriptorMapping::AtomicCounter:
          prefix = "Atomic";
          dstLoc.category = DescriptorCategory::ReadWriteResource;
          break;
        case GLDescriptorMapping::ShaderStorage:
          prefix = "SSBO";
          dstLoc.category = DescriptorCategory::ReadWriteResource;
          break;
        case GLDescriptorMapping::Count:
        case GLDescriptorMapping::Invalid: dstLoc.category = DescriptorCategory::Unknown; break;
      }
      dstLoc.fixedBindNumber = srcLoc.idx;

      if(srcLoc.type == GLDescriptorMapping::BareUniforms)
        dstLoc.logicalBindName =
            StringFormat::Fmt("%s %s", prefix, ToStr(ShaderStage(srcLoc.idx)).c_str());
      else
        dstLoc.logicalBindName = StringFormat::Fmt("%s %u", prefix, srcLoc.idx);
    }
  }

  return ret;
}

void GLReplay::OpenGLFillCBufferVariables(ResourceId shader, GLuint prog, bool bufferBacked,
                                          rdcstr prefix, const rdcarray<ShaderConstant> &variables,
                                          rdcarray<ShaderVariable> &outvars,
                                          const bytebuf &bufferData)
{
  bytebuf uniformData;
  const bytebuf &data = bufferBacked ? bufferData : uniformData;

  if(!bufferBacked)
    uniformData.resize(128);

  for(int32_t i = 0; i < variables.count(); i++)
  {
    const ShaderConstantType &desc = variables[i].type;

    // remove implicit '.' for recursing through "structs" if it's actually a multi-dimensional
    // array.
    if(!prefix.empty() && prefix.back() == '.' && variables[i].name[0] == '[')
      prefix.pop_back();

    ShaderVariable var;
    var.name = variables[i].name;
    var.rows = desc.rows;
    var.columns = desc.columns;
    var.type = desc.baseType;
    var.flags = desc.flags;

    const uint32_t matStride = desc.matrixByteStride;

    if(!variables[i].type.members.empty())
    {
      if(desc.elements <= 1)
      {
        OpenGLFillCBufferVariables(shader, prog, bufferBacked, prefix + var.name.c_str() + ".",
                                   variables[i].type.members, var.members, data);
        var.type = VarType::Struct;
      }
      else
      {
        var.members.resize(desc.elements);
        for(uint32_t a = 0; a < desc.elements; a++)
        {
          ShaderVariable &arrEl = var.members[a];
          arrEl.rows = var.rows;
          arrEl.columns = var.columns;
          arrEl.name = StringFormat::Fmt("%s[%u]", var.name.c_str(), a);
          arrEl.type = VarType::Struct;
          arrEl.flags = var.flags;

          OpenGLFillCBufferVariables(shader, prog, bufferBacked, prefix + arrEl.name.c_str() + ".",
                                     variables[i].type.members, arrEl.members, data);
        }
        var.rows = var.columns = 0;
      }
    }
    else
    {
      RDCEraseEl(var.value);

      // need to query offset and strides as there's no way to know what layout was used
      // (and if it's not an std layout it's implementation defined :( )
      rdcstr fullname = prefix + var.name;

      GLuint idx = GL.glGetProgramResourceIndex(prog, eGL_UNIFORM, fullname.c_str());

      // if this is an array of size 1 try looking for <array_variable_name>[0].<member_name>
      if((idx == GL_INVALID_INDEX) && (desc.elements == 1))
      {
        rdcstr arrayZeroName = prefix;
        if(!arrayZeroName.empty() && arrayZeroName.back() == '.')
          arrayZeroName.pop_back();
        arrayZeroName += "[0]." + var.name;

        idx = GL.glGetProgramResourceIndex(prog, eGL_UNIFORM, arrayZeroName.c_str());
      }
      if(idx == GL_INVALID_INDEX)
      {
        // this might not be an error, this might be the corresponding member in an array-of-structs
        // that doesn't exist because it's not in a UBO.
        // e.g.:
        // struct foo { float a; float b; }
        // uniform foo bar[2];
        //
        // If the program only references bar[0].a and bar[1].b then we'd reflect the full structure
        // but only bar[0].a and bar[1].b  would have indices - bar[0].b and bar[1].a would not.
        RDCWARN("Can't find program resource index for %s", fullname.c_str());

        if(bufferBacked)
          RDCERR("Uniform is buffer backed - index expected");

        // if this is an array, generate empty members
        if(desc.elements > 1)
        {
          rdcarray<ShaderVariable> elems;
          for(uint32_t a = 0; a < desc.elements; a++)
          {
            ShaderVariable el = var;

            // if this is the last part of a multidimensional array, don't include the variable name
            if(var.name[0] != '[')
              el.name = StringFormat::Fmt("%s[%u]", var.name.c_str(), a);
            else
              el.name = StringFormat::Fmt("[%u]", a);

            elems.push_back(el);
          }

          var.members = elems;
          var.rows = var.columns = 0;
        }
      }
      else
      {
        GLenum props[] = {bufferBacked ? eGL_OFFSET : eGL_LOCATION};
        GLint values[] = {0};

        GL.glGetProgramResourceiv(prog, eGL_UNIFORM, idx, ARRAY_COUNT(props), props,
                                  ARRAY_COUNT(props), NULL, values);

        GLint location = values[0];
        GLint offset = values[0];

        if(!bufferBacked)
          offset = 0;

        if(desc.elements <= 1)
        {
          if(!bufferBacked)
          {
            switch(var.type)
            {
              case VarType::Unknown:
              case VarType::GPUPointer:
              case VarType::ConstantBlock:
              case VarType::ReadOnlyResource:
              case VarType::ReadWriteResource:
              case VarType::Sampler:
              case VarType::SLong:
              case VarType::ULong:
              case VarType::SShort:
              case VarType::UShort:
              case VarType::SByte:
              case VarType::UByte:
              case VarType::Half:
              case VarType::Struct:
              case VarType::Enum:
                RDCERR("Unexpected base variable type %s, treating as float",
                       ToStr(var.type).c_str());
                DELIBERATE_FALLTHROUGH();
              case VarType::Float:
                GL.glGetUniformfv(prog, location, (float *)uniformData.data());
                break;
              case VarType::SInt:
                GL.glGetUniformiv(prog, location, (int32_t *)uniformData.data());
                break;
              case VarType::Bool:
              case VarType::UInt:
                GL.glGetUniformuiv(prog, location, (uint32_t *)uniformData.data());
                break;
              case VarType::Double:
                GL.glGetUniformdv(prog, location, (double *)uniformData.data());
                break;
            }
          }

          StandardFillCBufferVariable(shader, desc, offset, data, var, matStride);
        }
        else
        {
          rdcarray<ShaderVariable> elems;
          for(uint32_t a = 0; a < desc.elements; a++)
          {
            ShaderVariable el = var;

            // if this is the last part of a multidimensional array, don't include the variable name
            if(var.name[0] != '[')
              el.name = StringFormat::Fmt("%s[%u]", var.name.c_str(), a);
            else
              el.name = StringFormat::Fmt("[%u]", a);

            if(!bufferBacked)
            {
              switch(var.type)
              {
                case VarType::Unknown:
                case VarType::GPUPointer:
                case VarType::ConstantBlock:
                case VarType::ReadOnlyResource:
                case VarType::ReadWriteResource:
                case VarType::Sampler:
                case VarType::SLong:
                case VarType::ULong:
                case VarType::SShort:
                case VarType::UShort:
                case VarType::SByte:
                case VarType::UByte:
                case VarType::Half:
                case VarType::Struct:
                case VarType::Enum:
                  RDCERR("Unexpected base variable type %s, treating as float",
                         ToStr(var.type).c_str());
                  DELIBERATE_FALLTHROUGH();
                case VarType::Float:
                  GL.glGetUniformfv(prog, location + a, (float *)uniformData.data());
                  break;
                case VarType::SInt:
                  GL.glGetUniformiv(prog, location + a, (int32_t *)uniformData.data());
                  break;
                case VarType::Bool:
                case VarType::UInt:
                  GL.glGetUniformuiv(prog, location + a, (uint32_t *)uniformData.data());
                  break;
                case VarType::Double:
                  GL.glGetUniformdv(prog, location + a, (double *)uniformData.data());
                  break;
              }
            }

            StandardFillCBufferVariable(shader, desc, offset, data, el, matStride);

            if(bufferBacked)
              offset += desc.arrayByteStride;

            elems.push_back(el);
          }

          var.members = elems;
          var.rows = var.columns = 0;
        }
      }
    }

    outvars.push_back(var);
  }
}

void GLReplay::FillCBufferVariables(ResourceId pipeline, ResourceId shader, ShaderStage stage,
                                    rdcstr entryPoint, uint32_t cbufSlot,
                                    rdcarray<ShaderVariable> &outvars, const bytebuf &data)
{
  WrappedOpenGL &drv = *m_pDriver;

  MakeCurrentReplayContext(&m_ReplayCtx);

  const WrappedOpenGL::ShaderData &shaderDetails = m_pDriver->GetShader(shader);

  if((int32_t)cbufSlot >= shaderDetails.GetReflection()->constantBlocks.count())
  {
    RDCERR("Requesting invalid constant block");
    return;
  }

  GLuint curProg = 0;
  drv.glGetIntegerv(eGL_CURRENT_PROGRAM, (GLint *)&curProg);

  if(curProg == 0)
  {
    drv.glGetIntegerv(eGL_PROGRAM_PIPELINE_BINDING, (GLint *)&curProg);

    if(curProg == 0)
    {
      RDCERR("No program or pipeline bound");
      return;
    }
    else
    {
      ResourceId id =
          m_pDriver->GetResourceManager()->GetResID(ProgramPipeRes(m_pDriver->GetCtx(), curProg));
      const WrappedOpenGL::PipelineData &pipeDetails = m_pDriver->GetPipeline(id);

      size_t s = ShaderIdx(shaderDetails.type);

      curProg = m_pDriver->GetResourceManager()->GetResource(pipeDetails.stagePrograms[s]).name;
    }
  }

  const ConstantBlock &cblock = shaderDetails.GetReflection()->constantBlocks[cbufSlot];

  if(shaderDetails.spirvWords.empty())
  {
    OpenGLFillCBufferVariables(shaderDetails.GetReflection()->resourceId, curProg,
                               cblock.bufferBacked ? true : false, "", cblock.variables, outvars,
                               data);
  }
  else
  {
    if(cblock.compileConstants)
    {
      rdcarray<SpecConstant> specconsts;

      for(size_t i = 0; i < shaderDetails.specIDs.size(); i++)
      {
        SpecConstant spec;
        spec.specID = shaderDetails.specIDs[i];
        spec.value = shaderDetails.specValues[i];
        spec.dataSize = 4;
        specconsts.push_back(spec);
      }

      FillSpecConstantVariables(shaderDetails.GetReflection()->resourceId, shaderDetails.patchData,
                                cblock.variables, outvars, specconsts);
    }
    else if(!cblock.bufferBacked)
    {
      OpenGLFillCBufferVariables(shaderDetails.GetReflection()->resourceId, curProg, false, "",
                                 cblock.variables, outvars, data);
    }
    else
    {
      StandardFillCBufferVariables(shaderDetails.GetReflection()->resourceId, cblock.variables,
                                   outvars, data);
    }
  }
}

void GLReplay::GetTextureData(ResourceId tex, const Subresource &sub,
                              const GetTextureDataParams &params, bytebuf &data)
{
  WrappedOpenGL &drv = *m_pDriver;

  if(m_pDriver->m_Textures.find(tex) == m_pDriver->m_Textures.end())
  {
    data.clear();
    RDCWARN("Requesting data for non-existant texture %s", ToStr(tex).c_str());
    return;
  }

  WrappedOpenGL::TextureData &texDetails = m_pDriver->m_Textures[tex];

  GLuint tempTex = 0;

  Subresource s = sub;

  GLenum texType = texDetails.curType;
  GLuint texname = texDetails.resource.name;

  int numMips = GetNumMips(texType, texname, texDetails.width, texDetails.height, texDetails.depth);

  s.mip = RDCMIN(uint32_t(numMips - 1), s.mip);

  GLenum intFormat = texDetails.internalFormat;
  GLsizei width = RDCMAX(1, texDetails.width >> s.mip);
  GLsizei height = RDCMAX(1, texDetails.height >> s.mip);
  GLsizei depth = RDCMAX(1, texDetails.depth >> s.mip);
  GLsizei arraysize = 1;
  GLint samples = texDetails.samples;

  if(texType == eGL_NONE)
  {
    RDCERR("Trying to get texture data for unknown ID %s!", ToStr(tex).c_str());
    return;
  }

  if(texType == eGL_TEXTURE_BUFFER)
  {
    GLuint bufName = 0;
    drv.glGetTextureLevelParameterivEXT(texname, texType, 0, eGL_TEXTURE_BUFFER_DATA_STORE_BINDING,
                                        (GLint *)&bufName);
    ResourceId id =
        m_pDriver->GetResourceManager()->GetResID(BufferRes(m_pDriver->GetCtx(), bufName));

    GLuint offs = 0, size = 0;
    drv.glGetTextureLevelParameterivEXT(texname, texType, 0, eGL_TEXTURE_BUFFER_OFFSET,
                                        (GLint *)&offs);
    drv.glGetTextureLevelParameterivEXT(texname, texType, 0, eGL_TEXTURE_BUFFER_SIZE, (GLint *)&size);

    GetBufferData(id, offs, size, data);
    return;
  }

  if(texType == eGL_TEXTURE_2D_ARRAY || texType == eGL_TEXTURE_2D_MULTISAMPLE_ARRAY ||
     texType == eGL_TEXTURE_1D_ARRAY || texType == eGL_TEXTURE_CUBE_MAP ||
     texType == eGL_TEXTURE_CUBE_MAP_ARRAY)
  {
    // array size doesn't get mip'd down
    depth = 1;
    arraysize = texDetails.depth;
    if(texType == eGL_TEXTURE_1D_ARRAY)
    {
      height = 1;
      arraysize = texDetails.height;
    }
    if(texType == eGL_TEXTURE_CUBE_MAP)
    {
      arraysize = 6;
    }
  }

  s.sample = RDCMIN(uint32_t(texDetails.samples - 1), s.sample);
  s.slice = RDCMIN(uint32_t(arraysize - 1), s.slice);

  if(params.remap != RemapTexture::NoRemap)
  {
    GLenum remapFormat = eGL_RGBA8;
    if(params.remap == RemapTexture::RGBA8)
      remapFormat = eGL_RGBA8;
    else if(params.remap == RemapTexture::RGBA16)
      remapFormat = eGL_RGBA16F;
    else if(params.remap == RemapTexture::RGBA32)
      remapFormat = eGL_RGBA32F;

    CompType typeCast = BaseRemapType(params);
    if(typeCast == CompType::Typeless && IsSRGBFormat(intFormat))
      typeCast = CompType::UNormSRGB;

    remapFormat = GetViewCastedFormat(remapFormat, typeCast);

    GLenum newtarget = (texType == eGL_TEXTURE_3D ? eGL_TEXTURE_3D : eGL_TEXTURE_2D);

    if(intFormat != remapFormat || newtarget != texType)
    {
      MakeCurrentReplayContext(m_DebugCtx);

      GLenum finalFormat = remapFormat;

      // create temporary texture of width/height in the new format to render to
      drv.glGenTextures(1, &tempTex);
      drv.glBindTexture(newtarget, tempTex);
      if(newtarget == eGL_TEXTURE_3D)
        drv.glTextureImage3DEXT(tempTex, newtarget, 0, finalFormat, width, height, depth, 0,
                                GetBaseFormat(finalFormat), GetDataType(finalFormat), NULL);
      else
        drv.glTextureImage2DEXT(tempTex, newtarget, 0, finalFormat, width, height, 0,
                                GetBaseFormat(finalFormat), GetDataType(finalFormat), NULL);
      drv.glTextureParameteriEXT(tempTex, newtarget, eGL_TEXTURE_MAX_LEVEL, 0);

      // create temp framebuffer
      GLuint fbo = 0;
      drv.glGenFramebuffers(1, &fbo);
      drv.glBindFramebuffer(eGL_FRAMEBUFFER, fbo);

      drv.glTextureParameteriEXT(tempTex, newtarget, eGL_TEXTURE_MIN_FILTER, eGL_NEAREST);
      drv.glTextureParameteriEXT(tempTex, newtarget, eGL_TEXTURE_MAG_FILTER, eGL_NEAREST);
      drv.glTextureParameteriEXT(tempTex, newtarget, eGL_TEXTURE_WRAP_S, eGL_CLAMP_TO_EDGE);
      drv.glTextureParameteriEXT(tempTex, newtarget, eGL_TEXTURE_WRAP_T, eGL_CLAMP_TO_EDGE);
      drv.glTextureParameteriEXT(tempTex, newtarget, eGL_TEXTURE_WRAP_R, eGL_CLAMP_TO_EDGE);
      if(newtarget == eGL_TEXTURE_3D)
        drv.glFramebufferTexture3D(eGL_FRAMEBUFFER, eGL_COLOR_ATTACHMENT0, eGL_TEXTURE_3D, tempTex,
                                   0, 0);
      else if(newtarget == eGL_TEXTURE_2D)
        drv.glFramebufferTexture2D(eGL_FRAMEBUFFER, eGL_COLOR_ATTACHMENT0, newtarget, tempTex, 0);

      float col[] = {0.0f, 0.0f, 0.0f, 1.0f};
      drv.glClearBufferfv(eGL_COLOR, 0, col);

      // render to the temp texture to do the downcast
      float oldW = DebugData.outWidth;
      float oldH = DebugData.outHeight;

      DebugData.outWidth = float(width);
      DebugData.outHeight = float(height);

      GLenum baseFormat = !IsCompressedFormat(intFormat) ? GetBaseFormat(intFormat) : eGL_RGBA;

      TexDisplayFlags flags = eTexDisplay_None;

      if(IsUIntFormat(intFormat))
        flags = eTexDisplay_RemapUInt;
      else if(IsSIntFormat(intFormat))
        flags = eTexDisplay_RemapSInt;
      else
        flags = eTexDisplay_RemapFloat;

      for(GLsizei d = 0; d < (newtarget == eGL_TEXTURE_3D ? depth : 1); d++)
      {
        TextureDisplay texDisplay;

        texDisplay.red = texDisplay.green = texDisplay.blue = texDisplay.alpha = true;
        texDisplay.hdrMultiplier = -1.0f;
        texDisplay.linearDisplayAsGamma = false;
        texDisplay.overlay = DebugOverlay::NoOverlay;
        texDisplay.flipY = false;
        texDisplay.subresource.mip = s.mip;
        texDisplay.subresource.sample = params.resolve ? ~0U : s.sample;
        texDisplay.subresource.slice = s.slice;
        texDisplay.customShaderId = ResourceId();
        texDisplay.rangeMin = params.blackPoint;
        texDisplay.rangeMax = params.whitePoint;
        texDisplay.scale = 1.0f;
        texDisplay.resourceId = tex;
        texDisplay.typeCast = params.typeCast;
        texDisplay.rawOutput = false;
        texDisplay.xOffset = 0;
        texDisplay.yOffset = 0;

        if(newtarget == eGL_TEXTURE_3D)
        {
          drv.glFramebufferTexture3D(eGL_FRAMEBUFFER, eGL_COLOR_ATTACHMENT0, eGL_TEXTURE_3D,
                                     tempTex, 0, (GLint)d);
          texDisplay.subresource.slice = (uint32_t)d;
        }

        drv.glViewport(0, 0, width, height);

        GLboolean color_mask[4];
        drv.glGetBooleanv(eGL_COLOR_WRITEMASK, color_mask);

        // for depth, ensure we only write to the red channel, don't write into 'stencil' in green
        // with depth data
        if(baseFormat == eGL_DEPTH_COMPONENT || baseFormat == eGL_DEPTH_STENCIL)
        {
          drv.glColorMask(GL_TRUE, GL_FALSE, GL_FALSE, GL_FALSE);
        }

        RenderTextureInternal(texDisplay, flags);

        drv.glColorMask(color_mask[0], color_mask[1], color_mask[2], color_mask[3]);
      }

      // do one more time for the stencil
      if(baseFormat == eGL_DEPTH_STENCIL || baseFormat == eGL_STENCIL_INDEX)
      {
        TextureDisplay texDisplay;

        texDisplay.green = true;
        texDisplay.red = texDisplay.blue = texDisplay.alpha = false;
        texDisplay.hdrMultiplier = -1.0f;
        texDisplay.linearDisplayAsGamma = false;
        texDisplay.overlay = DebugOverlay::NoOverlay;
        texDisplay.flipY = false;
        texDisplay.subresource.mip = s.mip;
        texDisplay.subresource.sample = params.resolve ? ~0U : s.sample;
        texDisplay.subresource.slice = s.slice;
        texDisplay.customShaderId = ResourceId();
        texDisplay.rangeMin = params.blackPoint;
        texDisplay.rangeMax = params.whitePoint;
        texDisplay.scale = 1.0f;
        texDisplay.resourceId = tex;
        texDisplay.typeCast = CompType::Typeless;
        texDisplay.rawOutput = false;
        texDisplay.xOffset = 0;
        texDisplay.yOffset = 0;

        drv.glViewport(0, 0, width, height);

        GLboolean color_mask[4];
        drv.glGetBooleanv(eGL_COLOR_WRITEMASK, color_mask);
        drv.glColorMask(GL_FALSE, GL_TRUE, GL_FALSE, GL_FALSE);
        if(baseFormat == eGL_STENCIL_INDEX)
          drv.glColorMask(GL_TRUE, GL_TRUE, GL_FALSE, GL_FALSE);

        flags = TexDisplayFlags(
            flags & ~(eTexDisplay_RemapFloat | eTexDisplay_RemapUInt | eTexDisplay_RemapSInt));

        RenderTextureInternal(texDisplay, flags);

        drv.glColorMask(color_mask[0], color_mask[1], color_mask[2], color_mask[3]);
      }

      DebugData.outWidth = oldW;
      DebugData.outHeight = oldH;

      // rewrite the variables to temporary texture
      texType = newtarget;
      texname = tempTex;
      intFormat = finalFormat;
      if(newtarget != eGL_TEXTURE_3D)
        depth = 1;
      arraysize = 1;
      samples = 1;
      s.mip = 0;
      s.slice = 0;

      drv.glDeleteFramebuffers(1, &fbo);
    }
  }
  else if(params.resolve && samples > 1)
  {
    MakeCurrentReplayContext(m_DebugCtx);

    GLuint curDrawFBO = 0;
    GLuint curReadFBO = 0;
    drv.glGetIntegerv(eGL_DRAW_FRAMEBUFFER_BINDING, (GLint *)&curDrawFBO);
    drv.glGetIntegerv(eGL_READ_FRAMEBUFFER_BINDING, (GLint *)&curReadFBO);

    // create temporary texture of width/height in same format to render to
    drv.glGenTextures(1, &tempTex);
    drv.glBindTexture(eGL_TEXTURE_2D, tempTex);
    drv.glTextureImage2DEXT(tempTex, eGL_TEXTURE_2D, 0, intFormat, width, height, 0,
                            GetBaseFormat(intFormat), GetDataType(intFormat), NULL);
    drv.glTextureParameteriEXT(tempTex, eGL_TEXTURE_2D, eGL_TEXTURE_MAX_LEVEL, 0);

    // create temp framebuffers
    GLuint fbos[2] = {0};
    drv.glGenFramebuffers(2, fbos);

    drv.glBindFramebuffer(eGL_FRAMEBUFFER, fbos[0]);
    drv.glFramebufferTexture2D(eGL_FRAMEBUFFER, eGL_COLOR_ATTACHMENT0, eGL_TEXTURE_2D, tempTex, 0);

    drv.glBindFramebuffer(eGL_FRAMEBUFFER, fbos[1]);
    if(texType == eGL_TEXTURE_2D_MULTISAMPLE_ARRAY)
      drv.glFramebufferTextureLayer(eGL_FRAMEBUFFER, eGL_COLOR_ATTACHMENT0, texname, 0, s.slice);
    else
      drv.glFramebufferTexture2D(eGL_FRAMEBUFFER, eGL_COLOR_ATTACHMENT0, texType, texname, 0);

    // do default resolve (framebuffer blit)
    drv.glBindFramebuffer(eGL_DRAW_FRAMEBUFFER, fbos[0]);
    drv.glBindFramebuffer(eGL_READ_FRAMEBUFFER, fbos[1]);

    float col[] = {0.3f, 0.4f, 0.5f, 1.0f};
    drv.glClearBufferfv(eGL_COLOR, 0, col);

    SafeBlitFramebuffer(0, 0, width, height, 0, 0, width, height, GL_COLOR_BUFFER_BIT, eGL_NEAREST);

    // rewrite the variables to temporary texture
    texType = eGL_TEXTURE_2D;
    texname = tempTex;
    depth = 1;
    s.mip = 0;
    s.slice = 0;
    s.sample = 0;
    arraysize = 1;
    samples = 1;

    drv.glDeleteFramebuffers(2, fbos);

    drv.glBindFramebuffer(eGL_DRAW_FRAMEBUFFER, curDrawFBO);
    drv.glBindFramebuffer(eGL_READ_FRAMEBUFFER, curReadFBO);
  }
  else if(samples > 1)
  {
    MakeCurrentReplayContext(m_DebugCtx);

    if(texType == eGL_RENDERBUFFER)
    {
      // do blit from renderbuffer to texture
      MakeCurrentReplayContext(&m_ReplayCtx);

      GLuint curDrawFBO = 0;
      GLuint curReadFBO = 0;
      drv.glGetIntegerv(eGL_DRAW_FRAMEBUFFER_BINDING, (GLint *)&curDrawFBO);
      drv.glGetIntegerv(eGL_READ_FRAMEBUFFER_BINDING, (GLint *)&curReadFBO);

      drv.glBindFramebuffer(eGL_DRAW_FRAMEBUFFER, texDetails.renderbufferFBOs[1]);
      drv.glBindFramebuffer(eGL_READ_FRAMEBUFFER, texDetails.renderbufferFBOs[0]);

      GLenum b = GetBaseFormat(texDetails.internalFormat);

      GLbitfield mask = GL_COLOR_BUFFER_BIT;

      if(b == eGL_DEPTH_COMPONENT)
        mask = GL_DEPTH_BUFFER_BIT;
      else if(b == eGL_STENCIL)
        mask = GL_STENCIL_BUFFER_BIT;
      else if(b == eGL_DEPTH_STENCIL)
        mask = GL_DEPTH_BUFFER_BIT | GL_STENCIL_BUFFER_BIT;

      SafeBlitFramebuffer(0, 0, texDetails.width, texDetails.height, 0, 0, texDetails.width,
                          texDetails.height, mask, eGL_NEAREST);

      drv.glBindFramebuffer(eGL_DRAW_FRAMEBUFFER, curDrawFBO);
      drv.glBindFramebuffer(eGL_READ_FRAMEBUFFER, curReadFBO);

      // then proceed to read from the texture
      texname = texDetails.renderbufferReadTex;
      texType = texDetails.samples > 1 ? eGL_TEXTURE_2D_MULTISAMPLE : eGL_TEXTURE_2D;

      MakeCurrentReplayContext(m_DebugCtx);
    }

    // copy multisampled texture to an array. This creates tempTex and returns it in that variable,
    // for us to own
    tempTex = 0;
    m_pDriver->CopyTex2DMSToArray(tempTex, texname, width, height, arraysize, samples, intFormat);

    // CopyTex2DMSToArray is unwrapped, so register the resource here now
    m_pDriver->GetResourceManager()->RegisterResource(ResourceId(),
                                                      TextureRes(m_pDriver->GetCtx(), tempTex));

    // rewrite the variables to temporary texture
    texType = eGL_TEXTURE_2D_ARRAY;
    texname = tempTex;
    depth = 1;
    arraysize = arraysize * samples;
    // remap from slice & sample to just slice, given that each slice is expanded to N slices - one
    // for each sample.
    s.slice = s.slice * samples + s.sample;
    s.sample = 0;
    samples = 1;
  }

  // fetch and return data now
  {
    MakeCurrentReplayContext(m_DebugCtx);

    PixelPackState pack;
    pack.Fetch(true);

    ResetPixelPackState(true, 1);

    if(texType == eGL_RENDERBUFFER)
    {
      // do blit from renderbuffer to texture
      MakeCurrentReplayContext(&m_ReplayCtx);

      GLuint curDrawFBO = 0;
      GLuint curReadFBO = 0;
      drv.glGetIntegerv(eGL_DRAW_FRAMEBUFFER_BINDING, (GLint *)&curDrawFBO);
      drv.glGetIntegerv(eGL_READ_FRAMEBUFFER_BINDING, (GLint *)&curReadFBO);

      drv.glBindFramebuffer(eGL_DRAW_FRAMEBUFFER, texDetails.renderbufferFBOs[1]);
      drv.glBindFramebuffer(eGL_READ_FRAMEBUFFER, texDetails.renderbufferFBOs[0]);

      GLenum b = GetBaseFormat(texDetails.internalFormat);

      GLbitfield mask = GL_COLOR_BUFFER_BIT;

      if(b == eGL_DEPTH_COMPONENT)
        mask = GL_DEPTH_BUFFER_BIT;
      else if(b == eGL_STENCIL)
        mask = GL_STENCIL_BUFFER_BIT;
      else if(b == eGL_DEPTH_STENCIL)
        mask = GL_DEPTH_BUFFER_BIT | GL_STENCIL_BUFFER_BIT;

      SafeBlitFramebuffer(0, 0, texDetails.width, texDetails.height, 0, 0, texDetails.width,
                          texDetails.height, mask, eGL_NEAREST);

      drv.glBindFramebuffer(eGL_DRAW_FRAMEBUFFER, curDrawFBO);
      drv.glBindFramebuffer(eGL_READ_FRAMEBUFFER, curReadFBO);

      // then proceed to read from the texture
      texname = texDetails.renderbufferReadTex;
      texType = texDetails.samples > 1 ? eGL_TEXTURE_2D_MULTISAMPLE : eGL_TEXTURE_2D;

      MakeCurrentReplayContext(m_DebugCtx);
    }

    GLenum binding = TextureBinding(texType);

    GLuint prevtex = 0;
    drv.glGetIntegerv(binding, (GLint *)&prevtex);

    drv.glBindTexture(texType, texname);

    GLenum target = texType;
    if(texType == eGL_TEXTURE_CUBE_MAP)
    {
      GLenum targets[] = {
          eGL_TEXTURE_CUBE_MAP_POSITIVE_X, eGL_TEXTURE_CUBE_MAP_NEGATIVE_X,
          eGL_TEXTURE_CUBE_MAP_POSITIVE_Y, eGL_TEXTURE_CUBE_MAP_NEGATIVE_Y,
          eGL_TEXTURE_CUBE_MAP_POSITIVE_Z, eGL_TEXTURE_CUBE_MAP_NEGATIVE_Z,
      };

      RDCASSERT(s.slice < ARRAY_COUNT(targets));
      target = targets[s.slice];

      // we've "used" the slice, it's not actually a real slice anymore...
      s.slice = 0;
      arraysize = 1;
    }

    size_t dataSize = 0;

    if(IsCompressedFormat(intFormat))
    {
      dataSize = (size_t)GetCompressedByteSize(width, height, depth, intFormat);

      // contains a single slice
      data.resize(dataSize);

      // Note that for array textures we fetch the whole mip level (all slices at that mip). Since
      // GL returns all slices together, we cache it and keep the data around. This is because in
      // many cases we don't just want one slice we want all of them, but to preserve the API
      // querying slice-at-a-time we must cache the results of calling glGetTexImage to avoid
      // allocating the whole N layers N times.

      // check arraysize, since if we remapped or otherwise picked out a slice above, this will now
      // be 1 and we don't have to worry about anything
      if(arraysize > 1)
      {
        // if we don't have this texture cached, delete the previous data
        // we don't have to use anything else as the cache key, because if we still have an array at
        // this point then none of the GetTextureDataParams are relevant - only mip/arrayIdx
        if(m_GetTexturePrevID != tex)
        {
          for(size_t i = 0; i < ARRAY_COUNT(m_GetTexturePrevData); i++)
          {
            delete[] m_GetTexturePrevData[i];
            m_GetTexturePrevData[i] = NULL;
          }
        }

        m_GetTexturePrevID = tex;

        RDCASSERT(s.mip < ARRAY_COUNT(m_GetTexturePrevData));

        // if we don't have this mip cached, fetch it now
        if(m_GetTexturePrevData[s.mip] == NULL)
        {
          m_GetTexturePrevData[s.mip] = new byte[dataSize * arraysize];
          if(IsGLES)
            texDetails.GetCompressedImageDataGLES(s.mip, target, dataSize * arraysize,
                                                  m_GetTexturePrevData[s.mip]);
          else
            drv.glGetCompressedTexImage(target, s.mip, m_GetTexturePrevData[s.mip]);
        }

        // now copy the slice from the cache into ret
        byte *src = m_GetTexturePrevData[s.mip];
        src += dataSize * s.slice;

        memcpy(data.data(), src, dataSize);
      }
      else
      {
        // for non-arrays we can just readback without caching
        if(IsGLES)
          texDetails.GetCompressedImageDataGLES(s.mip, target, dataSize, data.data());
        else
          drv.glGetCompressedTexImage(target, s.mip, data.data());
      }
    }
    else
    {
      GLenum fmt = GetBaseFormat(intFormat);
      GLenum type = GetDataType(intFormat);

      size_t rowSize = GetByteSize(width, 1, 1, fmt, type);
      dataSize = GetByteSize(width, height, depth, fmt, type);
      data.resize(dataSize);

      // see above for the logic of handling arrays
      if(arraysize > 1)
      {
        if(m_GetTexturePrevID != tex)
        {
          for(size_t i = 0; i < ARRAY_COUNT(m_GetTexturePrevData); i++)
          {
            delete[] m_GetTexturePrevData[i];
            m_GetTexturePrevData[i] = NULL;
          }
        }

        m_GetTexturePrevID = tex;

        RDCASSERT(s.mip < ARRAY_COUNT(m_GetTexturePrevData));

        // if we don't have this mip cached, fetch it now
        if(m_GetTexturePrevData[s.mip] == NULL)
        {
          m_GetTexturePrevData[s.mip] = new byte[dataSize * arraysize];
          drv.glGetTexImage(target, (GLint)s.mip, fmt, type, m_GetTexturePrevData[s.mip]);
        }

        // now copy the slice from the cache into ret
        byte *src = m_GetTexturePrevData[s.mip];
        src += dataSize * s.slice;

        memcpy(data.data(), src, dataSize);
      }
      else
      {
        drv.glGetTexImage(target, (GLint)s.mip, fmt, type, data.data());
      }

      if(params.standardLayout)
      {
        // GL puts D24 in the top bits (whether or not there's stencil). We choose to standardise it
        // to be in the low bits, so swizzle here. for D24 with no stencil, the stencil bits are
        // undefined so we can move them around and it means nothing.
        if(intFormat == eGL_DEPTH24_STENCIL8 || intFormat == eGL_DEPTH_COMPONENT24)
        {
          uint32_t *ptr = (uint32_t *)data.data();

          for(GLsizei z = 0; z < depth; z++)
          {
            for(GLsizei y = 0; y < height; y++)
            {
              for(GLsizei x = 0; x < width; x++)
              {
                const uint32_t val = *ptr;
                *ptr = (val >> 8) | ((val & 0xff) << 24);
                ptr++;
              }
            }
          }
        }

        // GL's RGBA4/RGB5A1 is BGRA order, but it puts alpha in the bottom bits where we expect it
        // in the top
        if(intFormat == eGL_RGBA4)
        {
          uint16_t *ptr = (uint16_t *)data.data();

          for(size_t i = 0; i < data.size(); i += sizeof(uint16_t))
          {
            const uint16_t val = *ptr;
            *ptr = (val >> 4) | ((val & 0xf) << 12);
            ptr++;
          }
        }
        else if(intFormat == eGL_RGB5_A1)
        {
          uint16_t *ptr = (uint16_t *)data.data();

          for(size_t i = 0; i < data.size(); i += sizeof(uint16_t))
          {
            const uint16_t val = *ptr;
            *ptr = (val >> 1) | ((val & 0x1) << 15);
            ptr++;
          }
        }
      }

      // if we're saving to disk we make the decision to vertically flip any non-compressed
      // images. This is a bit arbitrary, but really origin top-left is common for all disk
      // formats so we do this flip from bottom-left origin. We only do this for saving to
      // disk so that if we're transferring over the network etc for remote replay, the image
      // order is consistent (and we just need to take care to apply an extra vertical flip
      // for display when proxying).

      if(params.forDiskSave)
      {
        // need to vertically flip the image now to get conventional row ordering
        // we either do this when copying out the slice of interest, or just
        // on its own
        byte *src, *dst;

        byte *row = new byte[rowSize];

        size_t sliceSize = GetByteSize(width, height, 1, fmt, type);

        // invert all slices in a 3D texture
        for(GLsizei d = 0; d < depth; d++)
        {
          dst = data.data() + d * sliceSize;
          src = dst + (height - 1) * rowSize;

          for(GLsizei i = 0; i < height >> 1; i++)
          {
            memcpy(row, src, rowSize);
            memcpy(src, dst, rowSize);
            memcpy(dst, row, rowSize);

            dst += rowSize;
            src -= rowSize;
          }
        }

        delete[] row;
      }
    }

    pack.Apply(true);

    drv.glBindTexture(texType, prevtex);
  }

  if(tempTex)
    drv.glDeleteTextures(1, &tempTex);
}

void GLReplay::SetCustomShaderIncludes(const rdcarray<rdcstr> &directories)
{
}

void GLReplay::BuildCustomShader(ShaderEncoding sourceEncoding, const bytebuf &source,
                                 const rdcstr &entry, const ShaderCompileFlags &compileFlags,
                                 ShaderStage type, ResourceId &id, rdcstr &errors)
{
  if(sourceEncoding == ShaderEncoding::GLSL)
  {
    rdcstr sourceText = InsertSnippetAfterVersion(ShaderType::GLSL, (const char *)source.data(),
                                                  source.count(), GLSL_CUSTOM_PREFIX);

    bytebuf patchedSource;
    patchedSource.assign((byte *)sourceText.begin(), sourceText.size());

    return BuildTargetShader(sourceEncoding, patchedSource, entry, compileFlags, type, id, errors);
  }

  BuildTargetShader(sourceEncoding, source, entry, compileFlags, type, id, errors);
}

ResourceId GLReplay::ApplyCustomShader(TextureDisplay &display)
{
  TextureAnalysisScope textureAnalysis(*this);
  if(display.customShaderId == ResourceId() || display.resourceId == ResourceId())
    return ResourceId();

  WrappedOpenGL::TextureData &texDetails = m_pDriver->m_Textures[display.resourceId];

  MakeCurrentReplayContext(m_DebugCtx);

  CreateCustomShaderTex(texDetails.width, texDetails.height);

  m_pDriver->glBindFramebuffer(eGL_FRAMEBUFFER, DebugData.customFBO);
  m_pDriver->glFramebufferTexture2D(eGL_FRAMEBUFFER, eGL_COLOR_ATTACHMENT0, eGL_TEXTURE_2D,
                                    DebugData.customTex, display.subresource.mip);

  m_pDriver->glViewport(0, 0, RDCMAX(1, texDetails.width >> display.subresource.mip),
                        RDCMAX(1, texDetails.height >> display.subresource.mip));

  DebugData.outWidth = float(RDCMAX(1, texDetails.width));
  DebugData.outHeight = float(RDCMAX(1, texDetails.height));

  float clr[] = {0.0f, 0.8f, 0.0f, 0.0f};
  m_pDriver->glClearBufferfv(eGL_COLOR, 0, clr);

  TextureDisplay disp;
  disp.red = disp.green = disp.blue = disp.alpha = true;
  disp.flipY = false;
  disp.xOffset = 0.0f;
  disp.yOffset = 0.0f;
  disp.customShaderId = display.customShaderId;
  disp.resourceId = display.resourceId;
  disp.typeCast = display.typeCast;
  disp.hdrMultiplier = -1.0f;
  disp.linearDisplayAsGamma = false;
  disp.subresource = display.subresource;
  disp.overlay = DebugOverlay::NoOverlay;
  disp.rangeMin = 0.0f;
  disp.rangeMax = 1.0f;
  disp.rawOutput = false;
  disp.scale = 1.0f;

  RenderTextureInternal(disp, eTexDisplay_MipShift);

  return DebugData.CustomShaderTexID;
}

void GLReplay::CreateCustomShaderTex(uint32_t w, uint32_t h)
{
  if(DebugData.customTex)
  {
    uint32_t oldw = 0, oldh = 0;
    m_pDriver->glGetTextureLevelParameterivEXT(DebugData.customTex, eGL_TEXTURE_2D, 0,
                                               eGL_TEXTURE_WIDTH, (GLint *)&oldw);
    m_pDriver->glGetTextureLevelParameterivEXT(DebugData.customTex, eGL_TEXTURE_2D, 0,
                                               eGL_TEXTURE_HEIGHT, (GLint *)&oldh);

    if(oldw == w && oldh == h)
      return;

    m_pDriver->glDeleteTextures(1, &DebugData.customTex);
    DebugData.customTex = 0;
  }

  uint32_t mips = CalcNumMips((int)w, (int)h, 1);

  m_pDriver->glGenTextures(1, &DebugData.customTex);
  m_pDriver->glBindTexture(eGL_TEXTURE_2D, DebugData.customTex);
  for(uint32_t i = 0; i < mips; i++)
  {
    m_pDriver->glTextureImage2DEXT(DebugData.customTex, eGL_TEXTURE_2D, i, eGL_RGBA16F,
                                   (GLsizei)RDCMAX(1U, w >> i), (GLsizei)RDCMAX(1U, h >> i), 0,
                                   eGL_RGBA, eGL_FLOAT, NULL);
  }
  m_pDriver->glTextureParameteriEXT(DebugData.customTex, eGL_TEXTURE_2D, eGL_TEXTURE_MIN_FILTER,
                                    eGL_NEAREST);
  m_pDriver->glTextureParameteriEXT(DebugData.customTex, eGL_TEXTURE_2D, eGL_TEXTURE_MAG_FILTER,
                                    eGL_NEAREST);
  m_pDriver->glTextureParameteriEXT(DebugData.customTex, eGL_TEXTURE_2D, eGL_TEXTURE_BASE_LEVEL, 0);
  m_pDriver->glTextureParameteriEXT(DebugData.customTex, eGL_TEXTURE_2D, eGL_TEXTURE_MAX_LEVEL,
                                    mips - 1);
  m_pDriver->glTextureParameteriEXT(DebugData.customTex, eGL_TEXTURE_2D, eGL_TEXTURE_WRAP_S,
                                    eGL_CLAMP_TO_EDGE);
  m_pDriver->glTextureParameteriEXT(DebugData.customTex, eGL_TEXTURE_2D, eGL_TEXTURE_WRAP_T,
                                    eGL_CLAMP_TO_EDGE);

  DebugData.CustomShaderTexID =
      m_pDriver->GetResourceManager()->GetResID(TextureRes(m_pDriver->GetCtx(), DebugData.customTex));
}

void GLReplay::FreeCustomShader(ResourceId id)
{
  if(id == ResourceId())
    return;

  m_pDriver->glDeleteShader(m_pDriver->GetResourceManager()->GetResource(id).name);
}

void GLReplay::BuildTargetShader(ShaderEncoding sourceEncoding, const bytebuf &source,
                                 const rdcstr &entry, const ShaderCompileFlags &compileFlags,
                                 ShaderStage type, ResourceId &id, rdcstr &errors)
{
  WrappedOpenGL &drv = *m_pDriver;

  MakeCurrentReplayContext(m_DebugCtx);

  GLenum shtype = eGL_VERTEX_SHADER;
  switch(type)
  {
    case ShaderStage::Vertex: shtype = eGL_VERTEX_SHADER; break;
    case ShaderStage::Tess_Control: shtype = eGL_TESS_CONTROL_SHADER; break;
    case ShaderStage::Tess_Eval: shtype = eGL_TESS_EVALUATION_SHADER; break;
    case ShaderStage::Geometry: shtype = eGL_GEOMETRY_SHADER; break;
    case ShaderStage::Fragment: shtype = eGL_FRAGMENT_SHADER; break;
    case ShaderStage::Compute: shtype = eGL_COMPUTE_SHADER; break;
    default:
    {
      RDCERR("Unknown shader type %u", type);
      id = ResourceId();
      return;
    }
  }

  GLint len = source.count();
  GLuint shader = drv.glCreateShader(shtype);
  if(sourceEncoding == ShaderEncoding::GLSL)
  {
    const char *src = (const char *)source.data();
    drv.glShaderSource(shader, 1, &src, &len);
    drv.glCompileShader(shader);
  }
  else if(sourceEncoding == ShaderEncoding::OpenGLSPIRV)
  {
    if(!HasExt[ARB_gl_spirv])
    {
      errors = "OpenGL SPIR-V target shaders require GL_ARB_gl_spirv";
      drv.glDeleteShader(shader);
      id = ResourceId();
      return;
    }
    if(source.size() < 5 * sizeof(uint32_t) || (source.size() % sizeof(uint32_t)) != 0)
    {
      errors = "OpenGL SPIR-V target shader is truncated or not DWORD-aligned";
      drv.glDeleteShader(shader);
      id = ResourceId();
      return;
    }
    drv.glShaderBinary(1, &shader, eGL_SHADER_BINARY_FORMAT_SPIR_V, source.data(), len);
    drv.glSpecializeShader(shader, entry.c_str(), 0, NULL, NULL);
  }
  else
  {
    errors = StringFormat::Fmt("Unsupported OpenGL target ShaderEncoding %s",
                               ToStr(sourceEncoding).c_str());
    drv.glDeleteShader(shader);
    id = ResourceId();
    return;
  }

  GLint status = 0;
  drv.glGetShaderiv(shader, eGL_COMPILE_STATUS, &status);

  {
    len = 1024;
    drv.glGetShaderiv(shader, eGL_INFO_LOG_LENGTH, &len);
    char *buffer = new char[len + 1];
    drv.glGetShaderInfoLog(shader, len, NULL, buffer);
    buffer[len] = 0;
    errors = buffer;
    delete[] buffer;
  }

  if(status == 0)
    id = ResourceId();
  else
    id = m_pDriver->GetResourceManager()->GetResID(ShaderRes(m_pDriver->GetCtx(), shader));
}

void GLReplay::ReplaceResource(ResourceId from, ResourceId to)
{
  InvalidateLiveForward();
  MakeCurrentReplayContext(&m_ReplayCtx);
  ResetCheckpointBufferLineage();
  if(m_pDriver->GetResourceManager()->HasResource(from))
  {
    const GLNamespace type = m_pDriver->GetResourceManager()->GetResource(from).Namespace;
    // Generic image/FBO substitutions can redirect the loading-time physical destination set.
    // Keep ordinary full copies for the remainder of this capture after such a substitution.
    // Shader/program substitutions retain all physical bindings and invalidate CP generations.
    if(type != eResShader && type != eResProgram && type != eResProgramPipe)
      m_CheckpointTextureBindingEdited = true;
  }
  m_pDriver->ReplaceResource(from, to);
  ClearPostVSCache();
}

void GLReplay::RemoveReplacement(ResourceId id)
{
  InvalidateLiveForward();
  MakeCurrentReplayContext(&m_ReplayCtx);
  ResetCheckpointBufferLineage();
  m_pDriver->RemoveReplacement(id);
  ClearPostVSCache();
}

rdcstr GLReplay::SetEventShaderOverride(uint32_t eventId, ShaderStage stage,
                                       ResourceId expectedOriginal, ResourceId replacement,
                                       uint32_t outputTarget, bool independentFloatOutput)
{
  RestoreEventShaderOverrides();
  const ActionDescription *action = m_pDriver->GetAction(eventId);
  if(!action || !(action->flags & (ActionFlags::Drawcall | ActionFlags::Dispatch)))
    return "The event is not a graphics draw or compute dispatch";
  auto rm = m_pDriver->GetResourceManager();
  if(uint32_t(stage) >= 6 || expectedOriginal == ResourceId() ||
     rm->GetUnreplacedID(expectedOriginal) != expectedOriginal ||
     rm->GetUnreplacedID(replacement) != replacement || !rm->HasResource(replacement))
    return "Event Shader override requires a captured original and an unowned target Shader";
  const GLResource target = rm->GetResource(replacement, true);
  const ShaderReflection *reflection = m_pDriver->GetShader(replacement).GetReflection();
  if(target.Namespace != eResShader || !reflection || reflection->stage != stage)
    return "The target Shader stage does not match the event override";
  if(outputTarget != ~0U && (stage != ShaderStage::Pixel || outputTarget >= 8))
    return "A blend bypass requires a valid fragment color target";
  if(independentFloatOutput && (stage != ShaderStage::Pixel || outputTarget == ~0U))
    return "Independent float preview requires an explicit fragment color target";
  m_EventShaderOverrides[{eventId, stage}] = {expectedOriginal, replacement, outputTarget,
                                             independentFloatOutput};
  ClearPostVSCache();
  return {};
}

void GLReplay::ReleaseEventShaderPrograms(ResourceId target)
{
  MakeCurrentReplayContext(&m_ReplayCtx);
  for(auto it = m_EventShaderPipelines.begin(); it != m_EventShaderPipelines.end();)
    if(target == ResourceId() || it->first.Uses(target))
    {
      m_pDriver->glDeleteProgramPipelines(1, &it->second);
      it = m_EventShaderPipelines.erase(it);
    }
    else
      ++it;
  for(auto it = m_EventShaderPrograms.begin(); it != m_EventShaderPrograms.end();)
    if(target == ResourceId() || it->first.Uses(target))
    {
      m_pDriver->glDeleteProgram(it->second);
      it = m_EventShaderPrograms.erase(it);
    }
    else
      ++it;
}

void GLReplay::ClearEventShaderOverride(uint32_t eventId, ShaderStage stage)
{
  RestoreEventShaderOverrides();
  m_EventShaderOverrides.erase({eventId, stage});
  ClearPostVSCache();
}

void GLReplay::ClearEventShaderOverrides()
{
  RestoreEventShaderOverrides();
  m_EventShaderOverrides.clear();
  ClearPostVSCache();
}

void GLReplay::ClearEventShaderOverridesForResource(ResourceId id)
{
  RestoreEventShaderOverrides();
  for(auto it = m_EventShaderOverrides.begin(); it != m_EventShaderOverrides.end();)
    if(it->second.replacement == id)
      it = m_EventShaderOverrides.erase(it);
    else
      ++it;
  ReleaseEventShaderPrograms(id);
}

void GLReplay::DestroyEventShaderFloatPreview(EventShaderFloatPreview &preview)
{
  if(preview.framebuffer)
    m_pDriver->glDeleteFramebuffers(1, &preview.framebuffer);
  for(auto &attachment : preview.attachments)
    if(attachment.texture)
    {
      ResourceId id = m_pDriver->GetResourceManager()->GetResID(
          TextureRes(m_pDriver->GetCtx(), attachment.texture));
      m_CachedTextures.erase(id);
      m_pDriver->glDeleteTextures(1, &attachment.texture);
    }
  preview = EventShaderFloatPreview();
}

void GLReplay::ReleaseEventShaderFloatPreviews()
{
  if(m_EventShaderFloatPreviews.empty()) return;
  MakeCurrentReplayContext(&m_ReplayCtx);
  GL.glFinish();
  for(auto &item : m_EventShaderFloatPreviews)
    DestroyEventShaderFloatPreview(item.second);
  m_EventShaderFloatPreviews.clear();
}

TextureDescription GLReplay::GetEventShaderPreviewTexture(uint32_t eventId, ShaderStage stage)
{
  auto binding = m_EventShaderOverrides.find({eventId, stage});
  if(binding == m_EventShaderOverrides.end() || !binding->second.independentFloatOutput)
    return {};
  auto found = m_EventShaderFloatPreviews.find({eventId, binding->second.outputTarget});
  if(found == m_EventShaderFloatPreviews.end()) return {};
  MakeCurrentReplayContext(&m_ReplayCtx);
  return GetTexture(m_pDriver->GetResourceManager()->GetResID(TextureRes(m_pDriver->GetCtx(),
      found->second.attachments[found->second.selected].texture)));
}

rdcstr GLReplay::PrepareEventShaderFloatPreview(uint32_t eventId, uint32_t outputTarget,
                                                GLRenderState &original,
                                                EventShaderFloatPreview *&preview)
{
  preview = NULL;
  WrappedOpenGL &drv = *m_pDriver;
  auto rm = drv.GetResourceManager();
  ContextPair &ctx = drv.GetCtx();
  if(IsGLES && !HasExt[EXT_color_buffer_float])
    return "Independent RGBA32F preview requires EXT_color_buffer_float on this OpenGL ES device";
  GLint sourceFBO = 0, maximum = 0, drawCount = 0, fboSamples = 0;
  GL.glGetIntegerv(eGL_DRAW_FRAMEBUFFER_BINDING, &sourceFBO);
  GL.glGetIntegerv(eGL_MAX_COLOR_ATTACHMENTS, &maximum);
  GL.glGetIntegerv(eGL_MAX_DRAW_BUFFERS, &drawCount);
  GL.glGetIntegerv(eGL_SAMPLES, &fboSamples);
  if(sourceFBO == 0 || maximum <= 0 || maximum > 32 || drawCount <= 0 || outputTarget >= uint32_t(drawCount))
    return "Independent float preview requires a complete replay framebuffer";
  EventShaderFloatPreview requested;
  drawCount = RDCMIN(drawCount, 8);
  requested.drawBuffers.resize(drawCount);
  for(GLint i = 0; i < drawCount; i++)
    GL.glGetIntegerv(GLenum(eGL_DRAW_BUFFER0 + i), (GLint *)&requested.drawBuffers[i]);
  const GLenum selectedAttachment = requested.drawBuffers[outputTarget];
  if(selectedAttachment < eGL_COLOR_ATTACHMENT0 || selectedAttachment >= GLenum(eGL_COLOR_ATTACHMENT0 + maximum))
    return "The independent float preview output slot has no color attachment";
  // Every path, including allocation/format failures, restores all mutable GL state and the
  // source FBO's own read-buffer selection (which GLRenderState intentionally does not own).
  GL.glBindFramebuffer(eGL_READ_FRAMEBUFFER, sourceFBO);
  GLint sourceRead = 0;
  GL.glGetIntegerv(eGL_READ_BUFFER, &sourceRead);
  struct Restore
  {
    WrappedOpenGL &driver; GLRenderState &state; GLuint fbo; GLenum read;
    ~Restore()
    {
      GL.glBindFramebuffer(eGL_READ_FRAMEBUFFER, fbo);
      GL.glReadBuffer(read);
      state.ApplyState(&driver);
    }
  } restore = {drv, original, (GLuint)sourceFBO, (GLenum)sourceRead};
  const bool copyImage = HasExt[ARB_copy_image] && GL.glCopyImageSubData &&
                         !VendorCheck[VendorCheck_Qualcomm_avoid_glCopyImageSubData];
  GLint depthName = 0, depthType = 0;
  GL.glGetFramebufferAttachmentParameteriv(eGL_DRAW_FRAMEBUFFER, eGL_DEPTH_ATTACHMENT,
      eGL_FRAMEBUFFER_ATTACHMENT_OBJECT_TYPE, &depthType);
  if(depthType != eGL_NONE)
    GL.glGetFramebufferAttachmentParameteriv(eGL_DRAW_FRAMEBUFFER, eGL_DEPTH_ATTACHMENT,
        eGL_FRAMEBUFFER_ATTACHMENT_OBJECT_NAME, &depthName);
  for(GLint index = 0; index < maximum + 2; index++)
  {
    const GLenum slot = index < maximum ? GLenum(eGL_COLOR_ATTACHMENT0 + index) :
                         index == maximum ? eGL_DEPTH_ATTACHMENT : eGL_STENCIL_ATTACHMENT;
    GLint type = 0, name = 0;
    GL.glGetFramebufferAttachmentParameteriv(eGL_DRAW_FRAMEBUFFER, slot,
        eGL_FRAMEBUFFER_ATTACHMENT_OBJECT_TYPE, &type);
    if(type == eGL_NONE) continue;
    GL.glGetFramebufferAttachmentParameteriv(eGL_DRAW_FRAMEBUFFER, slot,
        eGL_FRAMEBUFFER_ATTACHMENT_OBJECT_NAME, &name);
    if(!name || (type != eGL_TEXTURE && type != eGL_RENDERBUFFER))
      return "Independent float preview encountered an unsupported OpenGL attachment";
    // A combined depth/stencil image must remain one shared attachment.
    if(slot == eGL_STENCIL_ATTACHMENT && name == depthName && type == depthType)
    {
      if(requested.attachments.empty())
        return "Independent float preview depth/stencil metadata is incomplete";
      requested.attachments.back().attachment = eGL_DEPTH_STENCIL_ATTACHMENT;
      continue;
    }
    EventShaderFloatAttachment a;
    a.attachment = slot; a.sourceName = (GLuint)name;
    a.source = rm->GetResID(type == eGL_RENDERBUFFER ? RenderbufferRes(ctx, name) : TextureRes(ctx, name));
    auto metadata = drv.m_Textures.find(a.source);
    if(metadata == drv.m_Textures.end())
      return "Independent float preview attachment metadata is unavailable";
    const auto &texture = metadata->second;
    a.sourceTarget = type == eGL_RENDERBUFFER ? eGL_RENDERBUFFER : TextureTarget(texture.curType);
    a.format = slot == selectedAttachment ? eGL_RGBA32F : texture.internalFormat;
    a.samples = RDCMAX(1, texture.samples);
    if(a.samples != RDCMAX(1, fboSamples))
      return "Independent float preview does not support implicit multisample render-to-texture attachments";
    if(type == eGL_TEXTURE)
    {
      GetFramebufferMipAndLayer(sourceFBO, slot, &a.mip, &a.layer);
      if(!IsGLES)
      {
        GLint layered = 0;
        GL.glGetFramebufferAttachmentParameteriv(eGL_DRAW_FRAMEBUFFER, slot,
            eGL_FRAMEBUFFER_ATTACHMENT_LAYERED, &layered);
        a.layered = layered != 0;
        if(a.layered)
        {
          a.layer = 0;
          a.layers = texture.depth & 0xffff;
          if(a.sourceTarget == eGL_TEXTURE_CUBE_MAP) a.layers = 6;
          if(a.sourceTarget == eGL_TEXTURE_3D) a.layers = RDCMAX(1, a.layers >> a.mip);
        }
      }
      else if(HasExt[OVR_multiview])
      {
        GLint views = 0;
        GL.glGetFramebufferAttachmentParameteriv(eGL_DRAW_FRAMEBUFFER, slot,
            eGL_FRAMEBUFFER_ATTACHMENT_TEXTURE_NUM_VIEWS_OVR, &views);
        // Ordinary 2D attachments report the extension's default of one view.
        // They must not be rebound with the multiview entry point, which requires an array texture.
        if(views > 1)
        {
          a.multiview = true; a.layers = views;
          GL.glGetFramebufferAttachmentParameteriv(eGL_DRAW_FRAMEBUFFER, slot,
              eGL_FRAMEBUFFER_ATTACHMENT_TEXTURE_BASE_VIEW_INDEX_OVR, &a.layer);
        }
      }
      if(a.samples > 1)
      {
        GLint fixed = 1;
        GL.glGetTextureLevelParameterivEXT(name, a.sourceTarget, 0, eGL_TEXTURE_FIXED_SAMPLE_LOCATIONS, &fixed);
        a.fixedSamples = fixed != 0;
      }
    }
    if(a.layers < 1 || a.mip < 0 || a.mip >= 32 || a.layer < 0 ||
       (a.layers > 1 && !copyImage) ||
       (a.samples > 1 && !(a.layers > 1 ? HasExt[ARB_texture_multisample] :
                                         HasExt[ARB_texture_multisample_no_array])))
      return "Independent float preview cannot preserve this layered or multisample attachment";
    a.width = RDCMAX(1, texture.width >> a.mip);
    a.height = RDCMAX(1, texture.height >> a.mip);
    uint64_t bytes = GLRenderbufferByteSize(a.width, a.height, a.samples, a.format);
    if(!bytes || bytes > 512ULL * 1024 * 1024 / uint64_t(a.layers))
      return "Independent float preview attachment exceeds the 512 MiB scratch budget";
    requested.bytes += bytes * uint64_t(a.layers);
    if(slot == selectedAttachment) requested.selected = requested.attachments.size();
    requested.signature += StringFormat::Fmt("%u:%s:%u:%u:%d:%d:%d:%d:%d:%d:%d:%d;", slot,
      ToStr(a.source).c_str(), a.sourceTarget, a.format, a.mip, a.layer, a.layers,
      a.width, a.height, a.samples, a.layered || a.multiview, a.fixedSamples);
    requested.attachments.push_back(a);
  }
  for(GLenum draw : requested.drawBuffers)
    requested.signature += StringFormat::Fmt("%u,", draw);
  for(const auto &attachment : requested.attachments)
    requested.signature += StringFormat::Fmt("attachment%u;", attachment.attachment);
  if(requested.attachments.empty() || requested.attachments[requested.selected].attachment != selectedAttachment)
    return "The independent float preview attachment is unavailable";
  if(requested.bytes > 512ULL * 1024 * 1024)
    return "Independent float preview attachments exceed the 512 MiB scratch budget";
  auto key = std::make_pair(eventId, outputTarget);
  auto found = m_EventShaderFloatPreviews.find(key);
  if(found != m_EventShaderFloatPreviews.end() && found->second.signature != requested.signature)
  {
    GL.glFinish();
    DestroyEventShaderFloatPreview(found->second);
    m_EventShaderFloatPreviews.erase(found);
    found = m_EventShaderFloatPreviews.end();
  }
  if(found == m_EventShaderFloatPreviews.end())
  {
    uint64_t total = requested.bytes;
    for(const auto &item : m_EventShaderFloatPreviews) total += item.second.bytes;
    while(m_EventShaderFloatPreviews.size() >= 8 || total > 512ULL * 1024 * 1024)
    {
      auto oldest = m_EventShaderFloatPreviews.end();
      for(auto it = m_EventShaderFloatPreviews.begin(); it != m_EventShaderFloatPreviews.end(); ++it)
      {
        auto active = m_EventShaderOverrides.find({it->first.first, ShaderStage::Pixel});
        if(active != m_EventShaderOverrides.end() && active->second.independentFloatOutput &&
           active->second.outputTarget == it->first.second) continue;
        if(oldest == m_EventShaderFloatPreviews.end() || it->second.used < oldest->second.used) oldest = it;
      }
      if(oldest == m_EventShaderFloatPreviews.end())
        return "Independent float preview cache is full of active previews";
      GL.glFinish();
      total -= oldest->second.bytes;
      DestroyEventShaderFloatPreview(oldest->second);
      m_EventShaderFloatPreviews.erase(oldest);
    }
    GL.glBindBuffer(eGL_PIXEL_UNPACK_BUFFER, 0);
    for(int errors = 0; errors < 16 && GL.glGetError() != eGL_NONE; errors++) {}
    drv.glGenFramebuffers(1, &requested.framebuffer);
    drv.glBindFramebuffer(eGL_DRAW_FRAMEBUFFER, requested.framebuffer);
    rdcstr failure;
    for(auto &a : requested.attachments)
    {
      const GLenum target = a.samples > 1 ? (a.layers > 1 ? eGL_TEXTURE_2D_MULTISAMPLE_ARRAY : eGL_TEXTURE_2D_MULTISAMPLE) :
                                            (a.layers > 1 ? eGL_TEXTURE_2D_ARRAY : eGL_TEXTURE_2D);
      drv.glGenTextures(1, &a.texture);
      drv.glBindTexture(target, a.texture);
      if(a.samples > 1)
      {
        if(a.layers > 1)
          drv.glTextureStorage3DMultisampleEXT(a.texture, target, a.samples, a.format, a.width, a.height, a.layers, a.fixedSamples);
        else
          drv.glTextureStorage2DMultisampleEXT(a.texture, target, a.samples, a.format, a.width, a.height, a.fixedSamples);
      }
      else
      {
        if(a.layers > 1)
          drv.glTextureImage3DEXT(a.texture, target, 0, a.format, a.width, a.height, a.layers, 0,
                                  GetBaseFormat(a.format), GetDataType(a.format), NULL);
        else
          drv.glTextureImage2DEXT(a.texture, target, 0, a.format, a.width, a.height, 0,
                                  GetBaseFormat(a.format), GetDataType(a.format), NULL);
        drv.glTextureParameteriEXT(a.texture, target, eGL_TEXTURE_MAX_LEVEL, 0);
        drv.glTextureParameteriEXT(a.texture, target, eGL_TEXTURE_MIN_FILTER, eGL_NEAREST);
        drv.glTextureParameteriEXT(a.texture, target, eGL_TEXTURE_MAG_FILTER, eGL_NEAREST);
      }
      GLenum allocationError = GL.glGetError();
      if(allocationError != eGL_NONE)
      {
        failure = StringFormat::Fmt("Allocating independent OpenGL preview %s/%s failed: %s",
          ToStr(a.format).c_str(), ToStr(target).c_str(), ToStr(allocationError).c_str());
        break;
      }
      if(a.multiview)
        drv.glFramebufferTextureMultiviewOVR(eGL_DRAW_FRAMEBUFFER, a.attachment, a.texture, 0, 0, a.layers);
      else if(a.layered)
        drv.glFramebufferTexture(eGL_DRAW_FRAMEBUFFER, a.attachment, a.texture, 0);
      else
        drv.glFramebufferTexture2D(eGL_DRAW_FRAMEBUFFER, a.attachment, target, a.texture, 0);
      GLenum attachmentError = GL.glGetError();
      if(attachmentError != eGL_NONE)
      {
        failure = StringFormat::Fmt("Attaching independent OpenGL preview %s at %s failed: %s",
          ToStr(a.format).c_str(), ToStr(a.attachment).c_str(), ToStr(attachmentError).c_str());
        break;
      }
    }
    if(failure.empty())
    {
      drv.glDrawBuffers((GLsizei)requested.drawBuffers.size(), requested.drawBuffers.data());
      GL.glBindFramebuffer(eGL_READ_FRAMEBUFFER, requested.framebuffer);
      GL.glReadBuffer(selectedAttachment);
      if(GL.glCheckFramebufferStatus(eGL_DRAW_FRAMEBUFFER) != eGL_FRAMEBUFFER_COMPLETE)
        failure = "Independent RGBA32F preview framebuffer is incomplete on this device";
    }
    if(!failure.empty())
    {
      DestroyEventShaderFloatPreview(requested);
      return failure;
    }
    found = m_EventShaderFloatPreviews.insert({key, std::move(requested)}).first;
  }
  preview = &found->second;
  GL.glBindFramebuffer(eGL_READ_FRAMEBUFFER, sourceFBO);
  GL.glBindFramebuffer(eGL_DRAW_FRAMEBUFFER, preview->framebuffer);
  GL.glDisable(eGL_SCISSOR_TEST);
  if(HasExt[EXT_framebuffer_sRGB]) GL.glDisable(eGL_FRAMEBUFFER_SRGB);
  for(int errors = 0; errors < 16 && GL.glGetError() != eGL_NONE; errors++) {}
  for(size_t i = 0; i < preview->attachments.size(); i++)
  {
    if(i == preview->selected) continue;
    const auto &a = preview->attachments[i];
    GLenum target = a.samples > 1 ? (a.layers > 1 ? eGL_TEXTURE_2D_MULTISAMPLE_ARRAY : eGL_TEXTURE_2D_MULTISAMPLE) :
                                    (a.layers > 1 ? eGL_TEXTURE_2D_ARRAY : eGL_TEXTURE_2D);
    if(copyImage)
      GL.glCopyImageSubData(a.sourceName, a.sourceTarget, a.mip, 0, 0, a.layer,
        a.texture, target, 0, 0, 0, 0, a.width, a.height, a.layers);
    else
    {
      GLbitfield mask = a.attachment == eGL_DEPTH_ATTACHMENT ? eGL_DEPTH_BUFFER_BIT :
        a.attachment == eGL_STENCIL_ATTACHMENT ? eGL_STENCIL_BUFFER_BIT :
        a.attachment == eGL_DEPTH_STENCIL_ATTACHMENT ? eGL_DEPTH_BUFFER_BIT | eGL_STENCIL_BUFFER_BIT : eGL_COLOR_BUFFER_BIT;
      if(mask == eGL_COLOR_BUFFER_BIT)
      {
        GL.glReadBuffer(a.attachment);
        GL.glDrawBuffers(1, &a.attachment);
      }
      GL.glBlitFramebuffer(0, 0, a.width, a.height, 0, 0, a.width, a.height, mask, eGL_NEAREST);
    }
    if(GL.glGetError() != eGL_NONE)
      return "Independent OpenGL preview could not copy an attachment exactly";
  }
  GL.glDrawBuffers((GLsizei)preview->drawBuffers.size(), preview->drawBuffers.data());
  if(HasExt[EXT_draw_buffers2] || HasExt[ARB_draw_buffers_blend])
    GL.glColorMaski(outputTarget, GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
  else
    GL.glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);
  const GLfloat clear[] = {0, 0, 0, 0};
  GL.glClearBufferfv(eGL_COLOR, outputTarget, clear);
  if(GL.glGetError() != eGL_NONE)
    return "Clearing the independent OpenGL float preview failed";
  preview->used = ++m_EventShaderFloatClock;
  return {};
}

rdcstr GLReplay::ApplyEventShaderOverrides(uint32_t eventId)
{
  if(m_EventShaderPreviousState)
    return "A previous OpenGL event Shader override was not restored";
  rdcarray<std::pair<ShaderStage, EventShaderOverrideBinding>> pending;
  for(const auto &item : m_EventShaderOverrides)
    if(item.first.first == eventId)
      pending.push_back({item.first.second, item.second});
  if(pending.empty())
    return {};
  MakeCurrentReplayContext(&m_ReplayCtx);
  WrappedOpenGL &drv = *m_pDriver;
  auto rm = drv.GetResourceManager();
  ContextPair &ctx = drv.GetCtx();
  GLRenderState original;
  original.FetchState(&drv);
  // Subroutine uniform locations and implementation indices are link-dependent.
  // GLRenderState currently stores only numeric bindings, so relinking and then
  // restoring them would silently select a different function (or be invalid).
  for(const GLRenderState::Subroutine &subroutine : original.Subroutines)
    if(subroutine.numSubroutines > 0)
      return "OpenGL event Shader overrides cannot preserve active Shader subroutine bindings";
  GLuint programs[6] = {};
  ResourceId shaders[6] = {};
  ResourceId programIds[6] = {};
  if(original.Program.name)
  {
    ResourceId id = rm->GetResID(original.Program);
    const auto &program = drv.GetProgram(id);
    for(size_t i = 0; i < ARRAY_COUNT(programs); i++)
      if(program.stageShaders[i] != ResourceId())
      {
        programs[i] = original.Program.name;
        programIds[i] = id;
        shaders[i] = program.stageShaders[i];
      }
  }
  else if(original.Pipeline.name)
  {
    const auto &pipeline = drv.GetPipeline(rm->GetResID(original.Pipeline));
    for(size_t i = 0; i < ARRAY_COUNT(programs); i++)
      if(pipeline.stagePrograms[i] != ResourceId())
      {
        GLResource program = rm->GetResource(pipeline.stagePrograms[i], true);
        programs[i] = program.name;
        programIds[i] = rm->GetResID(program);
        shaders[i] = drv.GetProgram(programIds[i]).stageShaders[i];
      }
  }
  else
    return "The selected event has no live OpenGL program or program pipeline";
  EventShaderPipelineKey pipelineKey;
  pipelineKey.pipeline = rm->GetResID(original.Program.name ? original.Program : original.Pipeline);
  pipelineKey.eventId = eventId;
  std::map<ResourceId, EventShaderPipelineKey> programKeys;
  for(const auto &item : pending)
  {
    const uint32_t stage = uint32_t(item.first);
    if(!programs[stage] || rm->GetUnreplacedID(shaders[stage]) != item.second.original)
      return "The selected OpenGL event no longer binds the expected Shader";
    if(!rm->HasResource(item.second.replacement) ||
       rm->GetResource(item.second.replacement, true).Namespace != eResShader)
      return "The event Shader target is no longer available";
    EventShaderPipelineKey &programKey = programKeys[programIds[stage]];
    programKey.pipeline = programIds[stage];
    programKey.eventId = eventId;
    programKey.replacements[stage] = item.second.replacement;
    pipelineKey.replacements[stage] = item.second.replacement;
    if(item.second.outputTarget != ~0U)
      pipelineKey.outputTarget = item.second.outputTarget;
    pipelineKey.independentFloatOutput |= item.second.independentFloatOutput;
  }
  if(pipelineKey.outputTarget != ~0U)
  {
    GLint maxTargets = 0;
    GL.glGetIntegerv(eGL_MAX_DRAW_BUFFERS, &maxTargets);
    if(pipelineKey.outputTarget >= uint32_t(maxTargets))
      return "The requested debug color target exceeds the OpenGL draw-buffer limit";
  }
  // Bound the live cache without evicting any program while it is installed.
  if(m_EventShaderPrograms.size() >= 64 || m_EventShaderPipelines.size() >= 64)
    ReleaseEventShaderPrograms();
  std::map<ResourceId, GLuint> variants;
  for(const auto &item : programKeys)
  {
    const EventShaderPipelineKey &key = item.second;
    auto cached = m_EventShaderPrograms.find(key);
    const GLuint source = rm->GetResource(key.pipeline, true).name;
    if(cached == m_EventShaderPrograms.end())
    {
      const auto &program = drv.GetProgram(key.pipeline);
      GLuint variant = drv.glCreateProgram();
      for(size_t i = 0; i < ARRAY_COUNT(programs); i++)
      {
        ResourceId shader = key.replacements[i] != ResourceId() ? key.replacements[i] : program.stageShaders[i];
        if(shader == ResourceId())
          continue;
        GLResource resource = rm->GetResource(shader, true);
        if(resource.Namespace != eResShader)
        {
          drv.glDeleteProgram(variant);
          return "A linked program stage cannot be reconstructed as a Shader object";
        }
        drv.glAttachShader(variant, resource.name);
      }
      if(HasExt[ARB_separate_shader_objects])
      {
        GLint separable = 0;
        GL.glGetProgramiv(source, eGL_PROGRAM_SEPARABLE, &separable);
        if(separable)
          drv.glProgramParameteri(variant, eGL_PROGRAM_SEPARABLE, GL_TRUE);
      }
      if(program.stageShaders[0] != ResourceId())
        CopyProgramAttribBindings(source, variant, drv.GetShader(program.stageShaders[0]).GetReflection());
      if(program.stageShaders[4] != ResourceId())
        CopyProgramFragDataBindings(source, variant, drv.GetShader(program.stageShaders[4]).GetReflection());
      // Transform-feedback varyings and their buffer mode are program link state,
      // not uniforms. Preserve them even for a fragment-only replacement.
      GLint varyingCount = 0;
      GL.glGetProgramiv(source, eGL_TRANSFORM_FEEDBACK_VARYINGS, &varyingCount);
      if(varyingCount > 0)
      {
        GLint nameLength = 0, bufferMode = 0;
        GL.glGetProgramiv(source, eGL_TRANSFORM_FEEDBACK_VARYING_MAX_LENGTH, &nameLength);
        GL.glGetProgramiv(source, eGL_TRANSFORM_FEEDBACK_BUFFER_MODE, &bufferMode);
        rdcarray<rdcstr> varyingNames;
        rdcarray<const GLchar *> varyingPointers;
        varyingNames.resize(varyingCount);
        for(GLint v = 0; v < varyingCount; v++)
        {
          varyingNames[v].resize(RDCMAX(1, nameLength));
          GLsizei written = 0, count = 0;
          GLenum type = eGL_NONE;
          GL.glGetTransformFeedbackVarying(source, v, (GLsizei)varyingNames[v].size(),
                                          &written, &count, &type, varyingNames[v].data());
          varyingNames[v].resize(written);
          varyingPointers.push_back(varyingNames[v].c_str());
        }
        drv.glTransformFeedbackVaryings(variant, varyingCount, varyingPointers.data(),
                                       (GLenum)bufferMode);
      }
      drv.glLinkProgram(variant);
      GLint linked = 0;
      GL.glGetProgramiv(variant, eGL_LINK_STATUS, &linked);
      if(!linked)
      {
        GLint length = 0;
        GL.glGetProgramiv(variant, eGL_INFO_LOG_LENGTH, &length);
        rdcstr log;
        log.resize(RDCMAX(1, length));
        GL.glGetProgramInfoLog(variant, length, NULL, log.data());
        drv.glDeleteProgram(variant);
        return "Linking the event Shader program failed: " + log;
      }
      cached = m_EventShaderPrograms.insert({key, variant}).first;
    }
    // Uniform values are mutable capture state; compiled program caches must never cache them.
    PerStageReflections sourceStages, targetStages;
    drv.FillReflectionArray(key.pipeline, sourceStages);
    drv.FillReflectionArray(ProgramRes(ctx, cached->second), targetStages);
    CopyProgramUniforms(sourceStages, source, targetStages, cached->second);
    variants[key.pipeline] = cached->second;
  }
  GLRenderState changed = original;
  if(original.Program.name)
    changed.Program = ProgramRes(ctx, variants.begin()->second);
  else
  {
    auto cached = m_EventShaderPipelines.find(pipelineKey);
    if(cached == m_EventShaderPipelines.end())
    {
      GLuint pipeline = 0;
      drv.glGenProgramPipelines(1, &pipeline);
      const GLbitfield bits[] = {eGL_VERTEX_SHADER_BIT, eGL_TESS_CONTROL_SHADER_BIT,
          eGL_TESS_EVALUATION_SHADER_BIT, eGL_GEOMETRY_SHADER_BIT,
          eGL_FRAGMENT_SHADER_BIT, eGL_COMPUTE_SHADER_BIT};
      for(size_t i = 0; i < ARRAY_COUNT(programs); i++)
        if(programs[i])
        {
          auto variant = variants.find(programIds[i]);
          drv.glUseProgramStages(pipeline, bits[i], variant == variants.end() ? programs[i] : variant->second);
        }
      drv.glValidateProgramPipeline(pipeline);
      GLint valid = 0;
      GL.glGetProgramPipelineiv(pipeline, eGL_VALIDATE_STATUS, &valid);
      if(!valid)
      {
        drv.glDeleteProgramPipelines(1, &pipeline);
        return "The event Shader program pipeline failed interface validation";
      }
      cached = m_EventShaderPipelines.insert({pipelineKey, pipeline}).first;
    }
    changed.Pipeline = ProgramPipeRes(ctx, cached->second);
  }
  if(pipelineKey.outputTarget != ~0U)
  {
    uint32_t blendIndex = HasExt[ARB_draw_buffers_blend] ? pipelineKey.outputTarget : 0;
    uint32_t maskIndex = (HasExt[EXT_draw_buffers2] || HasExt[ARB_draw_buffers_blend]) ? pipelineKey.outputTarget : 0;
    changed.Blends[blendIndex].Enabled = false;
    changed.ColorMasks[maskIndex] = {GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE};
    changed.Enabled[GLRenderState::eEnabled_SampleAlphaToCoverage] = false;
    changed.Enabled[GLRenderState::eEnabled_ColorLogicOp] = false;
  }
  if(pipelineKey.independentFloatOutput)
  {
    for(const rdcstr &source : drv.GetShader(shaders[uint32_t(ShaderStage::Pixel)]).sources)
      if(source.contains("framebuffer_fetch"))
        return "Independent float preview cannot preserve OpenGL framebuffer-fetch semantics";
    EventShaderFloatPreview *preview = NULL;
    rdcstr error = PrepareEventShaderFloatPreview(eventId, pipelineKey.outputTarget, original, preview);
    if(!error.empty()) return error;
    changed.DrawFBO = FramebufferRes(ctx, preview->framebuffer);
  }
  InvalidateLiveForward();
  m_EventShaderPreviousState.reset(new GLRenderState(original));
  changed.ApplyState(&drv);
  return {};
}

void GLReplay::RestoreEventShaderOverrides()
{
  if(!m_EventShaderPreviousState)
    return;
  MakeCurrentReplayContext(&m_ReplayCtx);
  m_EventShaderPreviousState->ApplyState(m_pDriver);
  m_EventShaderPreviousState.reset();
}

rdcstr GLReplay::ReplaceTargetShaderChecked(ResourceId from, ResourceId to)
{
  InvalidateLiveForward();
  MakeCurrentReplayContext(&m_ReplayCtx);
  ResetCheckpointBufferLineage();
  rdcstr error = m_pDriver->ReplaceTargetShaderChecked(from, to);
  if(error.empty())
    ClearPostVSCache();
  return error;
}

rdcstr GLReplay::RemoveTargetShaderReplacementChecked(ResourceId id)
{
  ResetCheckpointTextureLineage();
  MakeCurrentReplayContext(&m_ReplayCtx);
  rdcstr error = m_pDriver->RemoveTargetShaderReplacementChecked(id);
  if(error.empty())
    ClearPostVSCache();
  return error;
}

void GLReplay::FreeTargetResource(ResourceId id)
{
  MakeCurrentReplayContext(&m_ReplayCtx);
  m_pDriver->FreeTargetResource(id);
}

void GLReplay::ClearReplayCache()
{
  ClearPostVSCache();
}

void GLReplay::ReloadShaderDebugInformation()
{
  // Nop : GL does not have seperate debug information
}

ResourceId GLReplay::CreateProxyTexture(const TextureDescription &templateTex)
{
  WrappedOpenGL &drv = *m_pDriver;

  MakeCurrentReplayContext(m_DebugCtx);

  GLenum intFormat = MakeGLFormat(templateTex.format);
  bool isCompressed = IsCompressedFormat(intFormat);

  GLenum baseFormat = eGL_RGBA;
  GLenum dataType = eGL_UNSIGNED_BYTE;
  if(!isCompressed)
  {
    baseFormat = GetBaseFormat(intFormat);
    dataType = GetDataType(intFormat);
  }

  if(baseFormat == eGL_NONE || dataType == eGL_NONE)
    return ResourceId();

  GLuint tex = 0;
  drv.glGenTextures(1, &tex);

  GLenum target = eGL_NONE;

  switch(templateTex.type)
  {
    case TextureType::Unknown: break;
    case TextureType::Buffer:
    case TextureType::Texture1D: target = eGL_TEXTURE_1D; break;
    case TextureType::Texture1DArray: target = eGL_TEXTURE_1D_ARRAY; break;
    case TextureType::TextureRect:
    case TextureType::Texture2D: target = eGL_TEXTURE_2D; break;
    case TextureType::Texture2DArray: target = eGL_TEXTURE_2D_ARRAY; break;
    case TextureType::Texture2DMS: target = eGL_TEXTURE_2D_MULTISAMPLE; break;
    case TextureType::Texture2DMSArray: target = eGL_TEXTURE_2D_MULTISAMPLE_ARRAY; break;
    case TextureType::Texture3D: target = eGL_TEXTURE_3D; break;
    case TextureType::TextureCube: target = eGL_TEXTURE_CUBE_MAP; break;
    case TextureType::TextureCubeArray: target = eGL_TEXTURE_CUBE_MAP_ARRAY; break;
    case TextureType::Count: RDCERR("Invalid texture dimension"); break;
  }

  if(target != eGL_NONE)
  {
    drv.glBindTexture(target, tex);

    if(target == eGL_TEXTURE_2D_MULTISAMPLE)
    {
      drv.glTextureStorage2DMultisampleEXT(tex, target, templateTex.msSamp, intFormat,
                                           templateTex.width, templateTex.height, GL_TRUE);
    }
    else if(target == eGL_TEXTURE_2D_MULTISAMPLE_ARRAY)
    {
      drv.glTextureStorage3DMultisampleEXT(tex, target, templateTex.msSamp, intFormat,
                                           templateTex.width, templateTex.height,
                                           templateTex.arraysize, GL_TRUE);
    }
    else
    {
      GLsizei w = (GLsizei)templateTex.width;
      GLsizei h = (GLsizei)templateTex.height;
      GLsizei d = (GLsizei)templateTex.depth;
      int dim = (int)templateTex.dimension;

      if(target == eGL_TEXTURE_1D_ARRAY)
      {
        h = templateTex.arraysize;
        dim = 2;
      }
      else if(target == eGL_TEXTURE_2D_ARRAY || target == eGL_TEXTURE_CUBE_MAP_ARRAY)
      {
        d = templateTex.arraysize;
        dim = 3;
      }

      GLenum targets[] = {
          eGL_TEXTURE_CUBE_MAP_POSITIVE_X, eGL_TEXTURE_CUBE_MAP_NEGATIVE_X,
          eGL_TEXTURE_CUBE_MAP_POSITIVE_Y, eGL_TEXTURE_CUBE_MAP_NEGATIVE_Y,
          eGL_TEXTURE_CUBE_MAP_POSITIVE_Z, eGL_TEXTURE_CUBE_MAP_NEGATIVE_Z,
      };

      int count = ARRAY_COUNT(targets);

      if(target != eGL_TEXTURE_CUBE_MAP)
      {
        targets[0] = target;
        count = 1;
      }

      for(int m = 0; m < (int)templateTex.mips; m++)
      {
        for(int t = 0; t < count; t++)
        {
          if(isCompressed)
          {
            GLsizei compSize = (GLsizei)GetCompressedByteSize(w, h, d, intFormat);

            bytebuf dummy;
            dummy.resize(compSize);

            if(dim == 1)
              drv.glCompressedTextureImage1DEXT(tex, targets[t], m, intFormat, w, 0, compSize,
                                                &dummy[0]);
            else if(dim == 2)
              drv.glCompressedTextureImage2DEXT(tex, targets[t], m, intFormat, w, h, 0, compSize,
                                                &dummy[0]);
            else if(dim == 3)
              drv.glCompressedTextureImage3DEXT(tex, targets[t], m, intFormat, w, h, d, 0, compSize,
                                                &dummy[0]);
          }
          else
          {
            if(dim == 1)
              drv.glTextureImage1DEXT(tex, targets[t], m, intFormat, w, 0, baseFormat, dataType,
                                      NULL);
            else if(dim == 2)
              drv.glTextureImage2DEXT(tex, targets[t], m, intFormat, w, h, 0, baseFormat, dataType,
                                      NULL);
            else if(dim == 3)
              drv.glTextureImage3DEXT(tex, targets[t], m, intFormat, w, h, d, 0, baseFormat,
                                      dataType, NULL);
          }
        }

        w = RDCMAX(1, w >> 1);
        if(target != eGL_TEXTURE_1D_ARRAY)
          h = RDCMAX(1, h >> 1);
        if(target != eGL_TEXTURE_2D_ARRAY && target != eGL_TEXTURE_CUBE_MAP_ARRAY)
          d = RDCMAX(1, d >> 1);
      }
    }

    drv.glTextureParameteriEXT(tex, target, eGL_TEXTURE_MAX_LEVEL, templateTex.mips - 1);
  }

  // Swizzle R/B channels only for non BGRA textures
  if(templateTex.format.BGRAOrder() && target != eGL_NONE && baseFormat != eGL_BGRA)
  {
    if(HasExt[ARB_texture_swizzle] || HasExt[EXT_texture_swizzle])
    {
      GLint bgraSwizzle[] = {eGL_BLUE, eGL_GREEN, eGL_RED, eGL_ALPHA};
      GLint bgrSwizzle[] = {eGL_BLUE, eGL_GREEN, eGL_RED, eGL_ONE};

      if(templateTex.format.compCount == 4)
        SetTextureSwizzle(tex, target, (GLenum *)bgraSwizzle);
      else if(templateTex.format.compCount == 3)
        SetTextureSwizzle(tex, target, (GLenum *)bgrSwizzle);
      else
        RDCERR("Unexpected component count %d for BGRA order format", templateTex.format.compCount);
    }
    else
    {
      RDCERR("Can't create a BGRA proxy texture without texture swizzle extension");
    }
  }

  ResourceId id = m_pDriver->GetResourceManager()->GetResID(TextureRes(m_pDriver->GetCtx(), tex));

  return id;
}

ResourceId GLReplay::CreateProxyTextureSnapshot(const TextureDescription &templateTex)
{
  ResourceId snapshot = CreateProxyTexture(templateTex);
  if(snapshot != ResourceId())
    m_TextureSnapshots.push_back(snapshot);
  return snapshot;
}

ResourceId GLReplay::CreateTextureSnapshot(ResourceId source, const Subresource &sourceSub,
                                           CompType typeCast, Subresource &snapshotSub)
{
  (void)typeCast;

  auto sourceIt = m_pDriver->m_Textures.find(source);
  if(sourceIt == m_pDriver->m_Textures.end())
    return ResourceId();

  const WrappedOpenGL::TextureData &sourceDetails = sourceIt->second;
  if(sourceDetails.curType == eGL_NONE || sourceDetails.samples > 1 || sourceDetails.width <= 0 ||
     sourceDetails.height <= 0 || !HasExt[ARB_copy_image] || GL.glFenceSync == NULL ||
     GL.glWaitSync == NULL || GL.glDeleteSync == NULL || GL.glFlush == NULL ||
     VendorCheck[VendorCheck_Qualcomm_avoid_glCopyImageSubData] ||
     (sourceDetails.internalFormat == eGL_DEPTH32F_STENCIL8 &&
      VendorCheck[VendorCheck_NV_avoid_D32S8_copy]))
    return ResourceId();

  TextureDescription snapshotDesc = GetTexture(source);
  if(snapshotDesc.resourceId == ResourceId() || snapshotDesc.mips == 0)
    return ResourceId();

  const uint32_t mip = RDCMIN(sourceSub.mip, snapshotDesc.mips - 1);
  const uint32_t width = RDCMAX(1U, snapshotDesc.width >> mip);
  const uint32_t height = RDCMAX(1U, snapshotDesc.height >> mip);
  const bool source3D = sourceDetails.curType == eGL_TEXTURE_3D;
  const bool compressed = IsCompressedFormat(sourceDetails.internalFormat);
  if(compressed &&
     (IsGLES || (VendorCheck[VendorCheck_AMD_copy_compressed_tinymips] && (width < 4 || height < 4)) ||
      (VendorCheck[VendorCheck_AMD_copy_compressed_cubemaps] &&
       sourceDetails.curType == eGL_TEXTURE_CUBE_MAP)))
    return ResourceId();

  snapshotDesc.width = width;
  snapshotDesc.height = height;
  snapshotDesc.mips = 1;
  snapshotDesc.msSamp = 1;
  snapshotDesc.msQual = 0;
  snapshotDesc.cubemap = false;

  GLint sourceY = 0;
  GLint sourceZ = 0;
  GLsizei copyDepth = 1;
  if(source3D)
  {
    snapshotDesc.depth = RDCMAX(1U, snapshotDesc.depth >> mip);
    snapshotDesc.arraysize = 1;
    snapshotDesc.type = TextureType::Texture3D;
    copyDepth = (GLsizei)snapshotDesc.depth;
  }
  else
  {
    snapshotDesc.depth = 1;
    snapshotDesc.arraysize = 1;
    snapshotDesc.dimension =
        sourceDetails.curType == eGL_TEXTURE_1D || sourceDetails.curType == eGL_TEXTURE_1D_ARRAY ? 1
                                                                                                 : 2;
    snapshotDesc.type = snapshotDesc.dimension == 1 ? TextureType::Texture1D : TextureType::Texture2D;

    if(sourceDetails.curType == eGL_TEXTURE_1D_ARRAY)
      sourceY = (GLint)RDCMIN(sourceSub.slice, RDCMAX(1U, GetTexture(source).arraysize) - 1);
    else if(sourceDetails.curType == eGL_TEXTURE_2D_ARRAY ||
            sourceDetails.curType == eGL_TEXTURE_CUBE_MAP ||
            sourceDetails.curType == eGL_TEXTURE_CUBE_MAP_ARRAY)
      sourceZ = (GLint)RDCMIN(sourceSub.slice, RDCMAX(1U, GetTexture(source).arraysize) - 1);
  }

  ResourceId snapshot = CreateProxyTextureSnapshot(snapshotDesc);
  if(snapshot == ResourceId())
    return ResourceId();

  auto snapshotIt = m_pDriver->m_Textures.find(snapshot);
  if(snapshotIt == m_pDriver->m_Textures.end() || snapshotIt->second.curType == eGL_NONE ||
     snapshotIt->second.internalFormat != sourceDetails.internalFormat)
  {
    FreeTextureSnapshot(snapshot);
    return ResourceId();
  }

  // Replay and debug use shared objects but distinct GL command streams. Order the copy after the
  // replay entirely on the GPU, then put a reciprocal wait in the replay stream so a subsequent
  // ReplayLog cannot mutate the source before the copy has consumed it.
  MakeCurrentReplayContext(&m_ReplayCtx);
  GLsync replayReady = GL.glFenceSync(eGL_SYNC_GPU_COMMANDS_COMPLETE, 0);
  if(replayReady == NULL)
  {
    FreeTextureSnapshot(snapshot);
    MakeCurrentReplayContext(&m_ReplayCtx);
    return ResourceId();
  }
  GL.glFlush();

  MakeCurrentReplayContext(m_DebugCtx);
  GL.glWaitSync(replayReady, 0, GL_TIMEOUT_IGNORED);
  GL.glDeleteSync(replayReady);
  GL.glCopyImageSubData(
      m_pDriver->GetResourceManager()->GetResource(source).name, sourceDetails.curType, (GLint)mip,
      0, sourceY, sourceZ, m_pDriver->GetResourceManager()->GetResource(snapshot).name,
      snapshotIt->second.curType, 0, 0, 0, 0, (GLsizei)width, (GLsizei)height, copyDepth);

  GLsync copyReady = GL.glFenceSync(eGL_SYNC_GPU_COMMANDS_COMPLETE, 0);
  if(copyReady == NULL)
  {
    // Fence allocation failure is exceptional. Complete the already-issued copy before deleting
    // its destination, then return null so the caller can use the portable fallback.
    GL.glFinish();
    FreeTextureSnapshot(snapshot);
    MakeCurrentReplayContext(&m_ReplayCtx);
    return ResourceId();
  }
  GL.glFlush();

  MakeCurrentReplayContext(&m_ReplayCtx);
  GL.glWaitSync(copyReady, 0, GL_TIMEOUT_IGNORED);
  GL.glDeleteSync(copyReady);

  snapshotSub = Subresource();
  if(source3D)
    snapshotSub.slice = RDCMIN(sourceSub.slice, snapshotDesc.depth - 1);
  return snapshot;
}

void GLReplay::FreeTextureSnapshot(ResourceId snapshot)
{
  rdcarray<ResourceId> snapshots;
  snapshots.push_back(snapshot);
  FreeTextureSnapshots(snapshots);
}

void GLReplay::FreeTextureSnapshots(const rdcarray<ResourceId> &snapshots)
{
  bool ownedSnapshot = false;
  for(ResourceId snapshot : snapshots)
  {
    if(m_TextureSnapshots.contains(snapshot))
    {
      ownedSnapshot = true;
      break;
    }
  }

  if(!ownedSnapshot)
    return;

  MakeCurrentReplayContext(m_DebugCtx);
  GLResourceManager *resourceManager = m_pDriver->GetResourceManager();
  for(ResourceId snapshot : snapshots)
  {
    for(size_t snapshotIndex = 0; snapshotIndex < m_TextureSnapshots.size(); snapshotIndex++)
    {
      if(m_TextureSnapshots[snapshotIndex] != snapshot)
        continue;

      if(resourceManager->HasResource(snapshot))
      {
        GLResource resource = resourceManager->GetResource(snapshot);
        if(resource.Namespace == eResTexture)
          m_pDriver->glDeleteTextures(1, &resource.name);
      }

      m_pDriver->m_Textures.erase(snapshot);
      m_CachedTextures.erase(snapshot);
      m_TextureSnapshots.erase(snapshotIndex);
      break;
    }
  }
}

void GLReplay::SetProxyTextureData(ResourceId texid, const Subresource &sub, byte *data,
                                   size_t dataSize)
{
  InvalidateLiveForward();
  WrappedOpenGL &drv = *m_pDriver;

  GLuint tex = m_pDriver->GetResourceManager()->GetResource(texid).name;

  WrappedOpenGL::TextureData &texdetails = m_pDriver->m_Textures[texid];

  if(texdetails.curType == eGL_NONE)
    return;

  GLenum fmt = texdetails.internalFormat;
  GLenum target = texdetails.curType;

  GLint depth = 1;
  if(target == eGL_TEXTURE_3D)
    depth = RDCMAX(1, texdetails.depth >> sub.mip);

  GLint width = RDCMAX(1, texdetails.width >> sub.mip);
  GLint height = RDCMAX(1, texdetails.height >> sub.mip);

  GLint mip =
      RDCMIN((GLint)sub.mip,
             GetNumMips(target, tex, texdetails.width, texdetails.height, texdetails.depth) - 1);
  GLint slice = (GLint)sub.slice;
  GLint sample = RDCMIN((GLint)sub.sample, texdetails.samples - 1);

  if(target == eGL_TEXTURE_1D_ARRAY)
    slice = RDCMIN(slice, texdetails.height);
  if(target == eGL_TEXTURE_2D_ARRAY || target == eGL_TEXTURE_2D_MULTISAMPLE_ARRAY ||
     target == eGL_TEXTURE_CUBE_MAP_ARRAY)
    slice = RDCMIN(slice, texdetails.depth);

  if(target == eGL_TEXTURE_1D_ARRAY)
    height = 1;

  if(IsCompressedFormat(fmt))
  {
    PixelUnpackState unpack;
    unpack.Fetch(true);

    ResetPixelUnpackState(true, 1);

    if(target == eGL_TEXTURE_1D)
    {
      drv.glCompressedTextureSubImage1DEXT(tex, target, mip, 0, width, fmt, (GLsizei)dataSize, data);
    }
    else if(target == eGL_TEXTURE_1D_ARRAY)
    {
      drv.glCompressedTextureSubImage2DEXT(tex, target, mip, 0, slice, width, 1, fmt,
                                           (GLsizei)dataSize, data);
    }
    else if(target == eGL_TEXTURE_2D)
    {
      drv.glCompressedTextureSubImage2DEXT(tex, target, mip, 0, 0, width, height, fmt,
                                           (GLsizei)dataSize, data);
    }
    else if(target == eGL_TEXTURE_2D_ARRAY || target == eGL_TEXTURE_CUBE_MAP_ARRAY)
    {
      drv.glCompressedTextureSubImage3DEXT(tex, target, mip, 0, 0, slice, width, height, 1, fmt,
                                           (GLsizei)dataSize, data);
    }
    else if(target == eGL_TEXTURE_3D)
    {
      drv.glCompressedTextureSubImage3DEXT(tex, target, mip, 0, 0, 0, width, height, depth, fmt,
                                           (GLsizei)dataSize, data);
    }
    else if(target == eGL_TEXTURE_CUBE_MAP)
    {
      GLenum targets[] = {
          eGL_TEXTURE_CUBE_MAP_POSITIVE_X, eGL_TEXTURE_CUBE_MAP_NEGATIVE_X,
          eGL_TEXTURE_CUBE_MAP_POSITIVE_Y, eGL_TEXTURE_CUBE_MAP_NEGATIVE_Y,
          eGL_TEXTURE_CUBE_MAP_POSITIVE_Z, eGL_TEXTURE_CUBE_MAP_NEGATIVE_Z,
      };

      target = targets[RDCMIN(slice, GLint(ARRAY_COUNT(targets) - 1))];

      drv.glCompressedTextureSubImage2DEXT(tex, target, mip, 0, 0, width, height, fmt,
                                           (GLsizei)dataSize, data);
    }
    else if(target == eGL_TEXTURE_2D_MULTISAMPLE || target == eGL_TEXTURE_2D_MULTISAMPLE_ARRAY)
    {
      RDCERR("Unexpected compressed MSAA texture!");
    }

    unpack.Apply(true);
  }
  else
  {
    GLenum baseformat = GetBaseFormat(fmt);
    GLenum datatype = GetDataType(fmt);

    if(dataSize < GetByteSize(width, height, depth, baseformat, datatype))
    {
      RDCERR("Insufficient data provided to SetProxyTextureData");
      return;
    }

    PixelUnpackState unpack;
    unpack.Fetch(false);

    ResetPixelUnpackState(false, 1);

    bytebuf swizzled;

    // GL puts D24 in the top bits (whether or not there's stencil). We choose to standardise it
    // to be in the low bits, so swizzle here. for D24 with no stencil, the stencil bits are
    // undefined so we can move them around and it means nothing.
    if(texdetails.internalFormat == eGL_DEPTH24_STENCIL8 ||
       texdetails.internalFormat == eGL_DEPTH_COMPONENT24)
    {
      const uint32_t *srcptr = (const uint32_t *)data;
      swizzled.resize(dataSize);
      uint32_t *dstptr = (uint32_t *)swizzled.data();

      for(size_t i = 0; i < dataSize; i += 4)
      {
        const uint32_t val = *srcptr;
        *dstptr = (val << 8) | ((val & 0xff000000) >> 24);
        srcptr++;
        dstptr++;
      }

      data = swizzled.data();
    }
    // GL's RGBA4/RGB5A1 is BGRA order, but it puts alpha in the bottom bits where we expect it
    // in the top
    else if(texdetails.internalFormat == eGL_RGBA4)
    {
      const uint16_t *srcptr = (const uint16_t *)data;
      swizzled.resize(dataSize);
      uint16_t *dstptr = (uint16_t *)swizzled.data();

      for(size_t i = 0; i < dataSize; i += 2)
      {
        const uint16_t val = *srcptr;
        *dstptr = ((val & 0x0fff) << 4) | ((val & 0xf000) >> 12);
        srcptr++;
        dstptr++;
      }

      data = swizzled.data();
    }
    else if(texdetails.internalFormat == eGL_RGB5_A1)
    {
      const uint16_t *srcptr = (const uint16_t *)data;
      swizzled.resize(dataSize);
      uint16_t *dstptr = (uint16_t *)swizzled.data();

      for(size_t i = 0; i < dataSize; i += 2)
      {
        const uint16_t val = *srcptr;
        *dstptr = ((val & 0x7fff) << 1) | ((val & 0x8000) >> 12);
        srcptr++;
        dstptr++;
      }

      data = swizzled.data();
    }

    if(target == eGL_TEXTURE_1D)
    {
      drv.glTextureSubImage1DEXT(tex, target, mip, 0, width, baseformat, datatype, data);
    }
    else if(target == eGL_TEXTURE_1D_ARRAY)
    {
      drv.glTextureSubImage2DEXT(tex, target, mip, 0, slice, width, 1, baseformat, datatype, data);
    }
    else if(target == eGL_TEXTURE_2D)
    {
      drv.glTextureSubImage2DEXT(tex, target, mip, 0, 0, width, height, baseformat, datatype, data);
    }
    else if(target == eGL_TEXTURE_2D_ARRAY || target == eGL_TEXTURE_CUBE_MAP_ARRAY)
    {
      drv.glTextureSubImage3DEXT(tex, target, mip, 0, 0, slice, width, height, 1, baseformat,
                                 datatype, data);
    }
    else if(target == eGL_TEXTURE_3D)
    {
      drv.glTextureSubImage3DEXT(tex, target, mip, 0, 0, 0, width, height, depth, baseformat,
                                 datatype, data);
    }
    else if(target == eGL_TEXTURE_CUBE_MAP)
    {
      GLenum targets[] = {
          eGL_TEXTURE_CUBE_MAP_POSITIVE_X, eGL_TEXTURE_CUBE_MAP_NEGATIVE_X,
          eGL_TEXTURE_CUBE_MAP_POSITIVE_Y, eGL_TEXTURE_CUBE_MAP_NEGATIVE_Y,
          eGL_TEXTURE_CUBE_MAP_POSITIVE_Z, eGL_TEXTURE_CUBE_MAP_NEGATIVE_Z,
      };

      target = targets[RDCMIN(slice, GLint(ARRAY_COUNT(targets) - 1))];

      drv.glTextureSubImage2DEXT(tex, target, mip, 0, 0, width, height, baseformat, datatype, data);
    }
    else if(target == eGL_TEXTURE_2D_MULTISAMPLE || target == eGL_TEXTURE_2D_MULTISAMPLE_ARRAY)
    {
      // create a temporary 2D array texture to upload the data to. Must use TexStorage to allow
      // texture views inside CopyArrayToTex2DMS
      GLuint uploadTex = 0;
      drv.glGenTextures(1, &uploadTex);
      drv.glBindTexture(eGL_TEXTURE_2D_ARRAY, uploadTex);
      drv.glTextureStorage3DEXT(uploadTex, eGL_TEXTURE_2D_ARRAY, 1, texdetails.internalFormat,
                                width, height, texdetails.samples * RDCMAX(1, texdetails.depth));
      drv.glTextureParameteriEXT(uploadTex, eGL_TEXTURE_2D_ARRAY, eGL_TEXTURE_MAX_LEVEL, 0);

      GLint unpackedSlice = slice * texdetails.samples + sample;

      // upload the data to the given slice
      drv.glTextureSubImage3DEXT(uploadTex, eGL_TEXTURE_2D_ARRAY, 0, 0, 0, unpackedSlice, width,
                                 height, 1, baseformat, datatype, data);

      // copy this slice into the 2D MSAA texture
      m_pDriver->CopyArrayToTex2DMS(tex, uploadTex, width, height, texdetails.depth,
                                    texdetails.samples, texdetails.internalFormat, unpackedSlice);

      // delete the temporary texture
      drv.glDeleteTextures(1, &uploadTex);
    }

    unpack.Apply(false);
  }
}

bool GLReplay::IsTextureSupported(const TextureDescription &tex)
{
  // GL can't decide if these formats are BGRA or RGBA order.
  // The bit order in memory for e.g. R4G4B4A4 is:
  // 15 .. ..  0
  //   R G B A
  //
  // but if you upload bits in that order with GL_RGBA it gets flipped.
  // It's more reliable to report no support and force a remap
  switch(tex.format.type)
  {
    case ResourceFormatType::R4G4:
    case ResourceFormatType::R4G4B4A4:
    case ResourceFormatType::R5G6B5:
    case ResourceFormatType::R5G5B5A1: return false;
    default: break;
  }

  // We couldn't create proxy textures for ASTC textures (see MakeGLFormat). So we give back false
  // and let RemapProxyTextureIfNeeded to set remap type for them.
  if(tex.format.type == ResourceFormatType::ASTC)
    return false;

  // we don't try to replay alpha8 textures, as we stick strictly to core profile GL
  if(tex.format.type == ResourceFormatType::A8)
    return false;

  // don't support 1D/3D block compressed textures
  if(tex.dimension != 2 &&
     (tex.format.type == ResourceFormatType::BC1 || tex.format.type == ResourceFormatType::BC2 ||
      tex.format.type == ResourceFormatType::BC3 || tex.format.type == ResourceFormatType::BC4 ||
      tex.format.type == ResourceFormatType::BC5 || tex.format.type == ResourceFormatType::BC6 ||
      tex.format.type == ResourceFormatType::BC7 || tex.format.type == ResourceFormatType::ASTC ||
      tex.format.type == ResourceFormatType::ETC2 || tex.format.type == ResourceFormatType::EAC))
    return false;

  // don't support 3D depth textures
  if(tex.dimension == 3 &&
     (tex.format.compType == CompType::Depth || tex.format.type == ResourceFormatType::D16S8 ||
      tex.format.type == ResourceFormatType::D24S8 || tex.format.type == ResourceFormatType::D32S8))
    return false;

  GLenum fmt = MakeGLFormat(tex.format);

  if(fmt == eGL_NONE)
    return false;

  // BGRA is not accepted as an internal format in case of GL
  // EXT_texture_format_BGRA8888 is required for creating BGRA proxy textures in case of GLES
  if(fmt == eGL_BGRA8_EXT && (!IsGLES || !HasExt[EXT_texture_format_BGRA8888]))
    return false;

  GLenum target = eGL_TEXTURE_2D;

  switch(tex.type)
  {
    case TextureType::Unknown: break;
    case TextureType::Buffer:
    case TextureType::Texture1D: target = eGL_TEXTURE_1D; break;
    case TextureType::Texture1DArray: target = eGL_TEXTURE_1D_ARRAY; break;
    case TextureType::TextureRect:
    case TextureType::Texture2D: target = eGL_TEXTURE_2D; break;
    case TextureType::Texture2DArray: target = eGL_TEXTURE_2D_ARRAY; break;
    case TextureType::Texture2DMS: target = eGL_TEXTURE_2D_MULTISAMPLE; break;
    case TextureType::Texture2DMSArray: target = eGL_TEXTURE_2D_MULTISAMPLE_ARRAY; break;
    case TextureType::Texture3D: target = eGL_TEXTURE_3D; break;
    case TextureType::TextureCube: target = eGL_TEXTURE_CUBE_MAP; break;
    case TextureType::TextureCubeArray: target = eGL_TEXTURE_CUBE_MAP_ARRAY; break;
    case TextureType::Count: RDCERR("Invalid texture dimension"); break;
  }

  GLint supported = 0, fragment = 0;
  m_pDriver->glGetInternalformativ(target, fmt, eGL_INTERNALFORMAT_SUPPORTED, 1, &supported);
  m_pDriver->glGetInternalformativ(target, fmt, eGL_FRAGMENT_TEXTURE, 1, &fragment);

  // check the texture is supported
  if(supported == 0 || fragment == 0)
    return false;

  // for multisampled textures it must be in a view compatibility class, to let us copy to/from the
  // MSAA texture.
  if(tex.msSamp > 1 && !IsDepthStencilFormat(fmt))
  {
    GLenum viewClass = eGL_NONE;
    m_pDriver->glGetInternalformativ(eGL_TEXTURE_2D_ARRAY, fmt, eGL_VIEW_COMPATIBILITY_CLASS, 1,
                                     (GLint *)&viewClass);

    if(viewClass == eGL_NONE)
      return false;
  }

  return true;
}

bool GLReplay::NeedRemapForFetch(const ResourceFormat &format)
{
  if(format.compType == CompType::Depth || format.type == ResourceFormatType::D16S8 ||
     format.type == ResourceFormatType::D24S8 || format.type == ResourceFormatType::D32S8)
    return IsGLES && !HasExt[NV_read_depth];
  return false;
}

ResourceId GLReplay::CreateProxyBuffer(const BufferDescription &templateBuf)
{
  WrappedOpenGL &drv = *m_pDriver;

  MakeCurrentReplayContext(m_DebugCtx);

  GLenum target = eGL_ARRAY_BUFFER;

  if(templateBuf.creationFlags & BufferCategory::Indirect)
    target = eGL_DRAW_INDIRECT_BUFFER;
  if(templateBuf.creationFlags & BufferCategory::Index)
    target = eGL_ELEMENT_ARRAY_BUFFER;
  if(templateBuf.creationFlags & BufferCategory::Constants)
    target = eGL_UNIFORM_BUFFER;
  if(templateBuf.creationFlags & BufferCategory::ReadWrite)
    target = eGL_SHADER_STORAGE_BUFFER;

  GLuint buf = 0;
  drv.glGenBuffers(1, &buf);
  drv.glBindBuffer(target, buf);
  drv.glNamedBufferDataEXT(buf, (GLsizeiptr)templateBuf.length, NULL, eGL_DYNAMIC_DRAW);

  ResourceId id = m_pDriver->GetResourceManager()->GetResID(BufferRes(m_pDriver->GetCtx(), buf));

  return id;
}

void GLReplay::SetProxyBufferData(ResourceId bufid, byte *data, size_t dataSize)
{
  InvalidateLiveForward();
  GLuint buf = m_pDriver->GetResourceManager()->GetResource(bufid).name;

  m_pDriver->glNamedBufferSubDataEXT(buf, 0, dataSize, data);
}

static uint32_t GLReplayPatchValueCutoff(const rdcarray<ReplayBufferPatch> &previous,
                                         const rdcarray<ReplayBufferPatch> &next,
                                         const std::map<ResourceId, uint32_t> &firstReads,
                                         rdcstr &reason,
                                         const rdcarray<ReplayBufferPatchRange> *changes = NULL)
{
  if(!SameReplayBufferPatchLayout(previous, next))
  {
    reason = "patchLayoutChanged";
    return 0;
  }
  uint32_t first = ~0U;
  for(size_t i = 0; i < next.size(); ++i)
  {
    auto use = firstReads.find(next[i].buffer);
    if(use == firstReads.end() || use->second == 0)
    {
      reason = "missingPotentialUniformOrCopyRead";
      return 0;
    }
    first = RDCMIN(first, use->second);
  }
  reason = "sameRangesPotentialUniformAndCopyReads";
  // Validate every original proof above before using a value delta. This fallback map describes
  // whole buffers; the separate native range index can refine it only with complete evidence.
  // A shader may read any bound UBO slot, independent of original reflection.
  if(changes && !changes->empty())
  {
    uint32_t changedFirst = ~0U;
    for(const ReplayBufferPatchRange &change : *changes)
    {
      auto use = firstReads.find(change.buffer);
      if(use == firstReads.end() || use->second == 0)
      {
        reason = "missingChangedBufferConsumer";
        return 0;
      }
      changedFirst = RDCMIN(changedFirst, use->second);
    }
    reason = "changedBuffersPotentialUniformAndCopyReads";
    return changedFirst;
  }
  return first;
}

static uint32_t GLReplayPatchRangeValueCutoff(const rdcarray<ReplayBufferPatch> &previous,
                                              const rdcarray<ReplayBufferPatch> &next,
                                              const std::map<ResourceId, uint32_t> &firstReads,
                                              const ReplayResourceAccessIndex &index,
                                              uint32_t lastEvent, rdcstr &reason,
                                              bool &rangeIndexKnown,
                                              const rdcarray<ReplayBufferPatchRange> *changes)
{
  rangeIndexKnown = false;
  const uint32_t fallback = GLReplayPatchValueCutoff(previous, next, firstReads, reason, changes);
  if(!fallback || !changes || changes->empty())
    return fallback;
  uint32_t first = ~0U;
  for(const ReplayBufferPatchRange &change : *changes)
  {
    const ReplayResourceReadQuery read =
        index.FirstRead({change.buffer, 0, change.byteOffset, change.byteLength}, 1, lastEvent);
    if(!read.complete || !read.found)
    {
      // A missing range is not an unused resource proof. Unknown events, storage lifetime,
      // replacement invalidation and work budgets all retain the old whole-buffer cutoff.
      reason = "rangeReadIndexIncomplete";
      return fallback;
    }
    first = RDCMIN(first, read.eventId);
  }
  rangeIndexKnown = true;
  reason = "changedRangesNativeUniformAndCopyReads";
  return first;
}

namespace
{
struct GLReplayPatchUploadRange
{
  size_t patch = 0;
  uint64_t patchOffset = 0, uploadOffset = 0, bytes = 0;
};

bool GLReplayPatchUploadRanges(const rdcarray<ReplayBufferPatch> &previous,
                               const rdcarray<ReplayBufferPatch> &next,
                               const rdcarray<ReplayBufferPatchRange> &changes,
                               rdcarray<GLReplayPatchUploadRange> &ranges)
{
  ranges.clear();
  if(!SameReplayBufferPatchLayout(previous, next) || changes.size() > 4096)
    return false;
  rdcarray<uint64_t> offsets;
  uint64_t total = 0;
  for(const ReplayBufferPatch &patch : next)
  {
    offsets.push_back(total);
    total += patch.data.size();
  }
  const ReplayBufferPatchRange *last = NULL;
  for(const ReplayBufferPatchRange &change : changes)
  {
    if(!change.byteLength || change.byteOffset > UINT64_MAX - change.byteLength ||
       (last &&
        (change.buffer < last->buffer ||
         (change.buffer == last->buffer && change.byteOffset < last->byteOffset + last->byteLength))))
    {
      ranges.clear();
      return false;
    }
    uint64_t position = change.byteOffset;
    const uint64_t end = position + change.byteLength;
    for(size_t i = 0; i < next.size() && position < end; ++i)
    {
      const ReplayBufferPatch &patch = next[i];
      if(patch.buffer != change.buffer || position < patch.byteOffset ||
         position >= patch.byteOffset + patch.data.size())
        continue;
      GLReplayPatchUploadRange range;
      range.patch = i;
      range.patchOffset = position - patch.byteOffset;
      range.uploadOffset = offsets[i] + range.patchOffset;
      range.bytes = RDCMIN(end - position, uint64_t(patch.data.size()) - range.patchOffset);
      ranges.push_back(range);
      position += range.bytes;
    }
    // A delta may cross adjacent patches, but never an unpatched gap or another buffer.
    if(position != end)
    {
      ranges.clear();
      return false;
    }
    last = &change;
  }
  return true;
}

enum class GLReplayPatchUploadResult
{
  Succeeded,
  Restored,
  RollbackFailed,
};

template <typename Generate, typename Bind, typename Allocate>
bool GLReplayPatchAllocateUpload(Generate generate, Bind bind, Allocate allocate)
{
  // Failed binding leaves the previous object current. Never issue a storage write until
  // generation and binding have both been confirmed independently.
  return generate() && bind() && allocate();
}

template <typename Write>
GLReplayPatchUploadResult GLReplayPatchUpdateUpload(const rdcarray<ReplayBufferPatch> &previous,
                                                    const rdcarray<ReplayBufferPatch> &next,
                                                    const rdcarray<GLReplayPatchUploadRange> &ranges,
                                                    Write write, uint64_t &writtenBytes,
                                                    uint64_t &rollbackBytes)
{
  writtenBytes = rollbackBytes = 0;
  for(const GLReplayPatchUploadRange &range : ranges)
  {
    if(write(range.uploadOffset, next[range.patch].data.data() + size_t(range.patchOffset),
             range.bytes, false))
    {
      writtenBytes += range.bytes;
      continue;
    }
    // A failed native transfer may follow earlier successful transfers. Restore the complete
    // accepted payload, including the failing range, before returning an ordinary edit error.
    bool restored = true;
    uint64_t offset = 0;
    for(const ReplayBufferPatch &patch : previous)
    {
      const bool success = write(offset, patch.data.data(), patch.data.size(), true);
      restored &= success;
      if(success)
        rollbackBytes += patch.data.size();
      offset += patch.data.size();
    }
    return restored ? GLReplayPatchUploadResult::Restored : GLReplayPatchUploadResult::RollbackFailed;
  }
  return GLReplayPatchUploadResult::Succeeded;
}
}

rdcstr GLReplay::SetReplayBufferPatches(const rdcarray<ReplayBufferPatch> &patches)
{
  // Changing overlay bytes does not mutate the already resident upstream texture bytes. Their
  // immutable checkpoint owner survives only when the existing first-consumer proof retains it.
  m_LiveForward.Invalidate();
  m_CheckpointTexturePendingTarget = 0;
  rdcstr error = ValidateReplayBufferPatches(patches);
  if(!error.empty())
    return error;
  if(patches.empty() && m_pDriver->m_ReplayBufferPatches.empty())
    return rdcstr();

  MakeCurrentReplayContext(&m_ReplayCtx);
  GLResourceManager *rm = m_pDriver->GetResourceManager();
  if(!patches.empty() && !m_pDriver->m_ReplayPatchInitialBindingsChecked)
  {
    m_pDriver->m_ReplayPatchInitialBindingsChecked = true;
    for(const auto &texture : m_pDriver->m_Textures)
      if(texture.second.curType == eGL_TEXTURE_BUFFER)
        m_pDriver->TrackReplayPatchBinding(rm->GetInitialContents(texture.first).tex.texBuffer,
                                           eGL_TEXTURE_BUFFER);
    for(const ResourceDescription &resource : m_Resources)
    {
      GLResource object = rm->GetResource(resource.resourceId, true);
      if(object.Namespace == eResFeedback)
        for(const GLResource &bound : rm->GetInitialContents(resource.resourceId).xfb.Buffer)
          m_pDriver->TrackReplayPatchBinding(bound, eGL_TRANSFORM_FEEDBACK_BUFFER);
    }
    SDFile *file = GetStructuredFile();
    if(!file)
      m_pDriver->m_ReplayPatchCaptureError =
          "OpenGL buffer patches require structured context evidence";
    else
    {
      bool frame = false;
      bool completeContentEvidence = true;
      rdcstr incompleteContentChunk;
      uint32_t contexts = 0;
      for(const SDChunk *chunk : file->chunks)
      {
        if(chunk->metadata.chunkID == (uint32_t)SystemChunk::CaptureBegin)
          frame = true;
        if(!frame)
          continue;
        const GLChunk type = GLChunk(chunk->metadata.chunkID);
        if(type == GLChunk::MakeContextCurrent || type == GLChunk::ContextConfiguration)
        {
          if(++contexts > 1)
            m_pDriver->m_ReplayPatchCaptureError =
                "OpenGL buffer patches currently require one capture context";
          continue;
        }
        if(chunk->metadata.chunkID >= (uint32_t)SystemChunk::FirstDriverChunk &&
           !GLPatchInitialBufferEvidenceComplete(type))
        {
          completeContentEvidence = false;
          if(incompleteContentChunk.empty())
            incompleteContentChunk = chunk->name;
        }
        if(type == GLChunk::ImplicitThreadSwitch)
          m_pDriver->m_ReplayPatchCaptureError =
              "OpenGL buffer patches currently require one capture context";
      }
      m_ReplayPatchReadOnlyEvidence =
          frame && completeContentEvidence && m_pDriver->m_ReplayCheckpointBufferWrites.complete;
      if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
        AppendReplayTimingLog(StringFormat::Fmt(
            "api=GLPatchInitialProof frame=%u complete=%u writesComplete=%u firstUnknown=%s",
            frame ? 1U : 0U, completeContentEvidence ? 1U : 0U,
            m_pDriver->m_ReplayCheckpointBufferWrites.complete ? 1U : 0U,
            incompleteContentChunk.c_str()));
    }
  }
  if(!patches.empty() && !m_pDriver->m_ReplayPatchCaptureError.empty())
    return m_pDriver->m_ReplayPatchCaptureError;
  uint64_t total = 0;
  for(const ReplayBufferPatch &patch : patches)
  {
    if(!rm->HasResource(patch.buffer) || rm->HasReplacement(patch.buffer) ||
       rm->GetUnreplacedID(patch.buffer) != patch.buffer)
      return "OpenGL buffer patches require an unreplaced captured buffer";
    GLResource live = rm->GetResource(patch.buffer);
    auto it = m_pDriver->m_Buffers.find(rm->GetResID(live));
    if(live.Namespace != eResBuffer || it == m_pDriver->m_Buffers.end())
      return "OpenGL patch resource is not a buffer";
    const WrappedOpenGL::BufferData &info = it->second;
    if(info.replayPatchUnsafe || (info.creationFlags & BufferCategory::ReadWrite) ||
       !(info.creationFlags & BufferCategory::Constants))
      return "OpenGL persistent patches currently require uniform buffers without storage, atomic, "
             "transform-feedback, texture-buffer, sparse or external use";
    const uint64_t size = RDCMIN(info.size, info.replayPatchMinSize);
    if(patch.byteOffset > size || patch.data.size() > size - patch.byteOffset)
      return "OpenGL patch is outside the buffer storage used by the capture";
    const uint64_t nativeLimit = sizeof(GLintptr) == 4 ? 0x7fffffffULL : 0x7fffffffffffffffULL;
    if(patch.byteOffset > nativeLimit || patch.data.size() > nativeLimit - patch.byteOffset)
      return "OpenGL patch range exceeds the replay process native buffer offset range";
    GLInitialContents initial = rm->GetInitialContents(patch.buffer);
    const uint64_t initialCharge = (info.size + 65535) & ~65535ULL;
    if(initial.resource.name == 0 && m_ReplayPatchReadOnlyEvidence &&
       m_pDriver->m_ReplayCheckpointBufferWrites.buffers.count(patch.buffer) == 0 && info.size > 0 &&
       info.size <= 16ULL * 1024 * 1024 && m_ReplayPatchImmutableInitialCount < 256 &&
       initialCharge <= 64ULL * 1024 * 1024 - m_ReplayPatchImmutableInitialBytes &&
       GL.glCopyBufferSubData && GL.glGenBuffers && GL.glBufferData && GL.glGetBufferParameteriv)
    {
      // A read-only UBO's current bytes are its immutable frame-start contents.
      // Keep a bounded GPU copy before editing; ordinary initial restoration then
      // also handles undo and navigation without any CPU data readback.
      for(uint32_t attempt = 0; attempt < 8 && GL.glGetError() != eGL_NONE; ++attempt)
      {
      }
      if(GL.glGetError() == eGL_NONE)
      {
        GLint read = 0, write = 0;
        GL.glGetIntegerv(eGL_COPY_READ_BUFFER_BINDING, &read);
        GL.glGetIntegerv(eGL_COPY_WRITE_BUFFER_BINDING, &write);
        GLuint source = 0;
        GL.glGenBuffers(1, &source);
        GL.glBindBuffer(eGL_COPY_WRITE_BUFFER, source);
        GL.glBufferData(eGL_COPY_WRITE_BUFFER, GLsizeiptr(info.size), NULL, eGL_STATIC_COPY);
        GLint allocated = 0;
        GL.glGetBufferParameteriv(eGL_COPY_WRITE_BUFFER, eGL_BUFFER_SIZE, &allocated);
        bool valid = source != 0 && uint64_t(allocated) == info.size && GL.glGetError() == eGL_NONE;
        if(valid)
        {
          GL.glBindBuffer(eGL_COPY_READ_BUFFER, live.name);
          GL.glCopyBufferSubData(eGL_COPY_READ_BUFFER, eGL_COPY_WRITE_BUFFER, 0, 0,
                                 GLsizeiptr(info.size));
          valid = GL.glGetError() == eGL_NONE;
        }
        GL.glBindBuffer(eGL_COPY_READ_BUFFER, GLuint(read));
        GL.glBindBuffer(eGL_COPY_WRITE_BUFFER, GLuint(write));
        valid &= GL.glGetError() == eGL_NONE;
        if(valid)
        {
          initial = GLInitialContents(BufferRes(m_pDriver->GetCtx(), source), uint32_t(info.size));
          rm->SetInitialContents(patch.buffer, initial);
          m_ReplayPatchImmutableInitialBytes += initialCharge;
          ++m_ReplayPatchImmutableInitialCount;
        }
        else if(source)
          GL.glDeleteBuffers(1, &source);
      }
    }
    if(initial.resource.name == 0 || initial.bufferLength < patch.byteOffset + patch.data.size())
      return "OpenGL patch requires immutable captured initial buffer contents for clear";
    total += patch.data.size();
    if(total > 128ULL * 1024 * 1024)
      return "OpenGL persistent buffer patch upload exceeds 128 MiB";
  }

  rdcarray<ReplayBufferPatchRange> changes;
  const bool deltaKnown =
      GetReplayBufferPatchChanges(m_pDriver->m_ReplayBufferPatches, patches, changes).empty();
  const bool deltaEnabled =
      Process::GetEnvVariable("RENDERDOC_REPLAY_DISABLE_BUFFER_PATCH_DELTA") != "1";
  uint64_t changedBytes = 0;
  std::set<ResourceId> changedBuffers;
  if(deltaKnown)
    for(const ReplayBufferPatchRange &change : changes)
    {
      changedBytes += change.byteLength;
      changedBuffers.insert(change.buffer);
    }

  GLuint upload = 0;
  bool uploadReused = false;
  uint64_t uploadWrittenBytes = 0;
  if(!patches.empty())
  {
    // These are core in GLES 3.0 too. Do not rely on desktop DSA or on immutable storage having
    // DYNAMIC_STORAGE_BIT: a server-side copy can update that storage without changing its flags.
    if(!GL.glCopyBufferSubData || !GL.glGenBuffers || !GL.glBufferData || !GL.glBufferSubData ||
       !GL.glBindBuffer || !GL.glGetBufferParameteriv)
      return "OpenGL/GLES replay context does not support buffer-copy patch uploads";
    rdcarray<GLReplayPatchUploadRange> uploadRanges;
    bool reuseUpload =
        m_pDriver->m_ReplayPatchUpload && deltaKnown &&
        Process::GetEnvVariable("RENDERDOC_GL_DISABLE_BUFFER_PATCH_UPLOAD_REUSE") != "1" &&
        GLReplayPatchUploadRanges(m_pDriver->m_ReplayBufferPatches, patches, changes, uploadRanges);
    // Consume only bounded stale errors before the transaction. Native errors below belong to
    // this update and must never be ignored merely because the allocation size looks correct.
    for(uint32_t i = 0; i < 8 && GL.glGetError() != eGL_NONE; ++i)
    {
    }
    if(GL.glGetError() != eGL_NONE)
      return "OpenGL persistent buffer patch upload has an unresolved context error";
    GLint previous = 0;
    GL.glGetIntegerv(eGL_COPY_READ_BUFFER_BINDING, &previous);
    if(GL.glGetError() != eGL_NONE)
      return "OpenGL persistent buffer patch upload binding is unavailable";
    if(reuseUpload)
    {
      GL.glBindBuffer(eGL_COPY_READ_BUFFER, m_pDriver->m_ReplayPatchUpload);
      GLint allocated = 0;
      GL.glGetBufferParameteriv(eGL_COPY_READ_BUFFER, eGL_BUFFER_SIZE, &allocated);
      reuseUpload = GL.glGetError() == eGL_NONE && allocated == (GLint)total;
    }
    if(reuseUpload)
    {
      uint64_t rollbackBytes = 0;
      const GLReplayPatchUploadResult result = GLReplayPatchUpdateUpload(
          m_pDriver->m_ReplayBufferPatches, patches, uploadRanges,
          [](uint64_t offset, const byte *data, uint64_t size, bool) {
            // BufferSubData preserves GL command ordering against earlier GPU copies reading
            // this upload object. Do not substitute an unsynchronised map or orphan its storage.
            GL.glBufferSubData(eGL_COPY_READ_BUFFER, (GLintptr)offset, (GLsizeiptr)size, data);
            return GL.glGetError() == eGL_NONE;
          },
          uploadWrittenBytes, rollbackBytes);
      GL.glBindBuffer(eGL_COPY_READ_BUFFER, previous);
      const bool bindingRestored = GL.glGetError() == eGL_NONE;
      if(result != GLReplayPatchUploadResult::Succeeded || !bindingRestored)
      {
        if(result == GLReplayPatchUploadResult::RollbackFailed || !bindingRestored)
        {
          // The accepted CPU table is still unchanged. If native rollback cannot reproduce it,
          // prevent another replay from consuming potentially mixed parameter bytes.
          SET_ERROR_RESULT(m_pDriver->m_FatalError, ResultCode::APIReplayFailed,
                           "OpenGL persistent buffer patch upload rollback could not be confirmed");
          InvalidateReplayCheckpoints();
        }
        if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
          AppendReplayTimingLog(StringFormat::Fmt(
              "api=OpenGLBufferPatch op=uploadError uploadReused=1 uploadWrittenBytes=%llu "
              "rollbackBytes=%llu rollbackSucceeded=%u bindingRestored=%u",
              (unsigned long long)uploadWrittenBytes, (unsigned long long)rollbackBytes,
              result == GLReplayPatchUploadResult::Restored ? 1U : 0U, bindingRestored ? 1U : 0U));
        return "OpenGL persistent buffer patch upload failed; the new edit was not accepted";
      }
      upload = m_pDriver->m_ReplayPatchUpload;
      uploadReused = true;
    }
    else
    {
      bytebuf data;
      data.reserve((size_t)total);
      for(const ReplayBufferPatch &patch : patches)
        data.append(patch.data);
      const bool allocatedSuccessfully = GLReplayPatchAllocateUpload(
          [&]() {
            GL.glGenBuffers(1, &upload);
            return GL.glGetError() == eGL_NONE && upload != 0;
          },
          [&]() {
            GL.glBindBuffer(eGL_COPY_READ_BUFFER, upload);
            return GL.glGetError() == eGL_NONE;
          },
          [&]() {
            GL.glBufferData(eGL_COPY_READ_BUFFER, (GLsizeiptr)total, data.data(), eGL_STATIC_DRAW);
            GLint allocated = 0;
            GL.glGetBufferParameteriv(eGL_COPY_READ_BUFFER, eGL_BUFFER_SIZE, &allocated);
            return GL.glGetError() == eGL_NONE && allocated == (GLint)total;
          });
      GL.glBindBuffer(eGL_COPY_READ_BUFFER, previous);
      const bool bindingRestored = GL.glGetError() == eGL_NONE;
      if(!allocatedSuccessfully || !bindingRestored)
      {
        if(upload)
          GL.glDeleteBuffers(1, &upload);
        if(!bindingRestored)
          SET_ERROR_RESULT(m_pDriver->m_FatalError, ResultCode::APIReplayFailed,
                           "OpenGL persistent buffer patch upload binding could not be restored");
        return "OpenGL persistent buffer patch upload allocation failed";
      }
      uploadWrittenBytes = total;
    }
  }

  rdcstr cutoffReason;
  bool rangeIndexKnown = false;
  uint32_t cutoff = GLReplayPatchRangeValueCutoff(
      m_pDriver->m_ReplayBufferPatches, patches, m_pDriver->m_ReplayPatchFirstReads,
      m_pDriver->m_ReplayPatchReadIndex, m_pDriver->m_ReplayPatchReadLastEvent, cutoffReason,
      rangeIndexKnown, deltaKnown && deltaEnabled ? &changes : NULL);
  rdcstr anchorReason;
  uint32_t firstRead =
      GLReplayPatchValueCutoff(patches, patches, m_pDriver->m_ReplayPatchFirstReads, anchorReason);
  // PBO uploads and non-UBO inputs need their own consumer evidence. Do not silently infer that
  // absence from the reflected usage list means they were never read.
  if(!m_pDriver->m_ReplayPatchReadEvidenceError.empty())
  {
    cutoffReason = m_pDriver->m_ReplayPatchReadEvidenceError;
    cutoff = firstRead = 0;
    rangeIndexKnown = false;
  }
  for(const ReplayBufferPatch &patch : patches)
  {
    const WrappedOpenGL::BufferData &info = m_pDriver->m_Buffers[patch.buffer];
    if(info.replayPatchNonUniformInput || info.creationFlags != BufferCategory::Constants)
    {
      cutoffReason = "additionalBufferInputWithoutCompleteFirstReadEvidence";
      cutoff = firstRead = 0;
      rangeIndexKnown = false;
    }
  }
  // Commit only after the complete new upload and every target have been validated. Initial
  // contents are never edited, so removing the table and replaying restores the captured bytes.
  m_ReplayPatchFirstAffectedEvent = cutoff;
  m_ReplayPatchAnchorEvent = firstRead > 1 ? firstRead - 1 : 0;
  // Synthetic multi-draw children share one native chunk and cannot be checkpoint boundaries.
  while(m_ReplayPatchAnchorEvent && m_pDriver->GetEvent(m_ReplayPatchAnchorEvent).fileOffset ==
                                        m_pDriver->GetEvent(m_ReplayPatchAnchorEvent + 1).fileOffset)
    --m_ReplayPatchAnchorEvent;
  // Only the overlay bytes changed: the immutable capture stream and potential native write
  // bindings have not. Keep the completed safety scan/static classification for other assets.
  // Removed patches stay conservatively non-static until a later general classification pass.
  const rdcarray<ReplayBufferPatch> *editTables[] = {&m_pDriver->m_ReplayBufferPatches, &patches};
  for(const rdcarray<ReplayBufferPatch> *table : editTables)
    for(const ReplayBufferPatch &patch : *table)
    {
      m_CheckpointStaticResources.erase(patch.buffer);
      m_CheckpointBufferVersions.erase(patch.buffer);
    }
  for(auto checkpoint = m_Checkpoints.begin(); checkpoint != m_Checkpoints.end();)
    if(cutoff && checkpoint->first < cutoff)
      ++checkpoint;
    else
      checkpoint = m_Checkpoints.erase(checkpoint);
  m_CheckpointStats.invalidations++;
  UpdateCheckpointAccounting();
  if(m_pDriver->m_ReplayPatchUpload && !uploadReused)
    GL.glDeleteBuffers(1, &m_pDriver->m_ReplayPatchUpload);
  m_pDriver->m_ReplayPatchUpload = upload;
  m_pDriver->m_ReplayBufferPatches = patches;
  if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
  {
    const rdcstr line = StringFormat::Fmt(
        "api=OpenGLBufferPatch op=set ranges=%zu firstAffected=%u anchor=%u reason=%s "
        "deltaKnown=%u deltaEnabled=%u changedRanges=%zu changedBytes=%llu changedBuffers=%zu "
        "rangeIndexKnown=%u uploadReused=%u uploadWrittenBytes=%llu",
        patches.size(), cutoff, m_ReplayPatchAnchorEvent, cutoffReason.c_str(),
        deltaKnown ? 1U : 0U, deltaEnabled ? 1U : 0U, deltaKnown ? changes.size() : 0,
        (unsigned long long)changedBytes, changedBuffers.size(), rangeIndexKnown ? 1U : 0U,
        uploadReused ? 1U : 0U, (unsigned long long)uploadWrittenBytes);
    RDCLOG("[EIDTiming] %s", line.c_str());
    AppendReplayTimingLog(line);
  }
  return rdcstr();
}

rdcarray<EventUsage> GLReplay::GetUsage(ResourceId id)
{
  if((m_pDriver->m_Textures.find(id) == m_pDriver->m_Textures.end() ||
      m_pDriver->m_Textures[id].resource.Namespace == eResUnknown) &&
     (m_pDriver->m_Buffers.find(id) == m_pDriver->m_Buffers.end() ||
      m_pDriver->m_Buffers[id].resource.Namespace == eResUnknown) &&
     !m_pDriver->HasShader(id))
  {
    return {EventUsage(0, ResourceUsage::Unused)};
  }

  return m_pDriver->GetUsage(id);
}

void GLReplay::MakeCurrentReplayContext(GLWindowingData *ctx)
{
  static GLWindowingData *prev = NULL;

  if(ctx && ctx != prev)
  {
#if ENABLED(RDOC_APPLE)
    GL.glFinish();
#endif

    m_pDriver->m_Platform.MakeContextCurrent(*ctx);
    prev = ctx;
    m_pDriver->ActivateContext(*ctx);
  }
}

void GLReplay::SwapBuffers(GLWindowingData *ctx)
{
  m_pDriver->m_Platform.SwapBuffers(*ctx);
}

void GLReplay::CloseReplayContext()
{
  m_pDriver->UnregisterReplayContext(m_ReplayCtx);
}

RDResult CreateReplayDevice(RDCDriver rdcdriver, RDCFile *rdc, const ReplayOptions &opts,
                            GLPlatform &platform, IReplayDriver **&driver)
{
  GLInitParams initParams;
  uint64_t ver = GLInitParams::CurrentVersion;

  // if we have an RDCFile, open the frame capture section and serialise the init params.
  // if not, we're creating a proxy-capable device so use default-initialised init params.
  if(rdc)
  {
    int sectionIdx = rdc->SectionIndex(SectionType::FrameCapture);

    if(sectionIdx < 0)
      RETURN_ERROR_RESULT(ResultCode::FileCorrupted, "File does not contain captured API data");

    ver = rdc->GetSectionProperties(sectionIdx).version;

    if(!GLInitParams::IsSupportedVersion(ver))
    {
      RETURN_ERROR_RESULT(
          ResultCode::APIIncompatibleVersion,
          "Incompatible OpenGL serialise version %llu, newest version supported is %llu", ver,
          GLInitParams::CurrentVersion);
    }

    StreamReader *reader = rdc->ReadSection(sectionIdx);

    ReadSerialiser ser(reader, Ownership::Stream);

    ser.SetVersion(ver);

    SystemChunk chunk = ser.ReadChunk<SystemChunk>();

    if(chunk != SystemChunk::DriverInit)
    {
      RETURN_ERROR_RESULT(ResultCode::FileCorrupted,
                          "Expected to get a DriverInit chunk, instead got %u", chunk);
    }

    SERIALISE_ELEMENT(initParams);

    if(ser.IsErrored())
      return ser.GetError();

    if(!initParams.renderer.empty())
      RDCLOG("Capture was created on %s / %s", initParams.renderer.c_str(),
             initParams.version.c_str());
  }

  GLWindowingData data = {};

  RDResult status = platform.InitialiseAPI(data, rdcdriver, opts.apiValidation);

  // any errors will be already printed, just pass the error up
  if(status != ResultCode::Succeeded)
    return status;

  bool current = platform.MakeContextCurrent(data);
  if(!current)
  {
    platform.DeleteReplayContext(data);
    RETURN_ERROR_RESULT(ResultCode::APIInitFailed, "Couldn't activate the created replay context");
  }

  // we use the platform's function which tries GL's GetProcAddress first, then falls back to
  // regular function lookup
  GL.PopulateWithCallback([&platform](const char *func) { return platform.GetReplayFunction(func); });

  FetchEnabledExtensions();

  // see gl_emulated.cpp
  GL.EmulateUnsupportedFunctions();
  GL.EmulateRequiredExtensions();

  RDResult extensionsValidated = CheckReplayContext();
  if(extensionsValidated != ResultCode::Succeeded)
  {
    platform.DeleteReplayContext(data);
    return extensionsValidated;
  }

  RDResult functionsValidated = ValidateFunctionPointers();
  if(functionsValidated != ResultCode::Succeeded)
  {
    platform.DeleteReplayContext(data);
    return functionsValidated;
  }

  WrappedOpenGL *gldriver = new WrappedOpenGL(platform);
  gldriver->SetDriverType(rdcdriver);

  GL.DriverForEmulation(gldriver);

  RDCLOG("Created %s replay device.", ToStr(rdcdriver).c_str());

  GLReplay *replay = gldriver->GetReplay();
  replay->SetProxy(rdc == NULL);
  replay->SetReplayData(data);

  if(!replay->HasDebugContext())
  {
    delete gldriver;
    platform.DeleteReplayContext(data);
    RETURN_ERROR_RESULT(ResultCode::APIHardwareUnsupported, "Failed to create analysis context");
  }

  gldriver->Initialise(initParams, ver, opts);

  *driver = (IReplayDriver *)replay;
  return ResultCode::Succeeded;
}

RDResult GL_ProcessStructured(RDCFile *rdc, SDFile &output)
{
  GLDummyPlatform dummy;
  WrappedOpenGL device(dummy);

  int sectionIdx = rdc->SectionIndex(SectionType::FrameCapture);

  if(sectionIdx < 0)
    RETURN_ERROR_RESULT(ResultCode::FileCorrupted, "File does not contain captured API data");

  device.SetStructuredExport(rdc->GetSectionProperties(sectionIdx).version);
  RDResult status = device.ReadLogInitialisation(rdc, true);

  if(status == ResultCode::Succeeded)
    device.GetStructuredFile()->Swap(output);

  return status;
}

static StructuredProcessRegistration GLProcessRegistration(RDCDriver::OpenGL, &GL_ProcessStructured);
static StructuredProcessRegistration GLESProcessRegistration(RDCDriver::OpenGLES,
                                                             &GL_ProcessStructured);

rdcarray<GLVersion> GetReplayVersions(RDCDriver api)
{
  // try to create all versions from highest down to lowest in order to get the highest versioned
  // context we can
  if(api == RDCDriver::OpenGLES)
  {
    return {
        {3, 2},
        {3, 1},
        {3, 0},
    };
  }
  else
  {
    return {
        {4, 6}, {4, 5}, {4, 4}, {4, 3}, {4, 2}, {4, 1}, {4, 0}, {3, 3}, {3, 2},
    };
  }
}

#if defined(RENDERDOC_SUPPORT_GLES)

RDResult GLES_CreateReplayDevice(RDCFile *rdc, const ReplayOptions &opts, IReplayDriver **driver)
{
  RDCLOG("Creating an OpenGL ES replay device");

  // for GLES replay, we try to use EGL if it's available. If it's not available, we look to see if
  // we can create an OpenGL ES context via the platform GL functions
  if(GetEGLPlatform().CanCreateGLESContext())
  {
    bool load_ok = GetEGLPlatform().PopulateForReplay();

    if(!load_ok)
    {
      RETURN_ERROR_RESULT(ResultCode::APIInitFailed,
                          "Couldn't find required EGL function addresses");
    }

    RDCLOG("Initialising GLES replay via libEGL");

    return CreateReplayDevice(rdc ? rdc->GetDriver() : RDCDriver::OpenGLES, rdc, opts,
                              GetEGLPlatform(), driver);
  }
#if defined(RENDERDOC_SUPPORT_GL)
  else if(GetGLPlatform().CanCreateGLESContext())
  {
    RDCLOG("libEGL is not available, falling back to EXT_create_context_es2_profile");

    bool load_ok = GetGLPlatform().PopulateForReplay();

    if(!load_ok)
    {
      RETURN_ERROR_RESULT(ResultCode::APIInitFailed,
                          "Couldn't find required GL function addresses");
    }

    return CreateReplayDevice(rdc ? rdc->GetDriver() : RDCDriver::OpenGLES, rdc, opts,
                              GetGLPlatform(), driver);
  }

  RETURN_ERROR_RESULT(ResultCode::APIInitFailed,
                      "libEGL not available, and GL cannot initialise or doesn't support "
                      "EXT_create_context_es2_profile");
#else
  // no GL support, no fallback apart from EGL

  RETURN_ERROR_RESULT(ResultCode::APIInitFailed, "libEGL is not available");
#endif
}

static DriverRegistration GLESDriverRegistration(RDCDriver::OpenGLES, &GLES_CreateReplayDevice);

#endif

#if defined(RENDERDOC_SUPPORT_GL)

RDResult GL_CreateReplayDevice(RDCFile *rdc, const ReplayOptions &opts, IReplayDriver **driver)
{
  GLPlatform *gl_platform = &GetGLPlatform();

  if(RenderDoc::Inst().GetGlobalEnvironment().waylandDisplay)
  {
#if defined(RENDERDOC_SUPPORT_EGL)
    RDCLOG("Forcing EGL device creation for wayland");
    gl_platform = &GetEGLPlatform();
#else
    RETURN_ERROR_RESULT(ResultCode::InternalError,
                        "EGL support must be enabled at build time when using Wayland");
#endif
  }

  bool can_create_gl_context = gl_platform->CanCreateGLContext();

#if defined(RENDERDOC_SUPPORT_EGL)
  if(!can_create_gl_context && gl_platform == &GetGLPlatform())
  {
    RDCLOG("Cannot create GL context with GL platform, falling back to EGL");
    gl_platform = &GetEGLPlatform();
    can_create_gl_context = gl_platform->CanCreateGLContext();
  }
#endif

  if(!can_create_gl_context)
  {
    RETURN_ERROR_RESULT(ResultCode::APIInitFailed,
                        "Current platform doesn't support OpenGL contexts");
  }

  RDCDEBUG("Creating an OpenGL replay device");

  bool load_ok = gl_platform->PopulateForReplay();

  if(!load_ok)
  {
    RETURN_ERROR_RESULT(ResultCode::APIInitFailed,
                        "Couldn't find required platform %s function addresses",
                        gl_platform == &GetGLPlatform() ? "GL" : "EGL");
  }

  return CreateReplayDevice(rdc ? rdc->GetDriver() : RDCDriver::OpenGL, rdc, opts, *gl_platform,
                            driver);
}

static DriverRegistration GLDriverRegistration(RDCDriver::OpenGL, &GL_CreateReplayDevice);

#endif

#if ENABLED(ENABLE_UNIT_TESTS)
#include "catch/catch.hpp"

TEST_CASE("OpenGL live forward publishes only a successful matching ordinary target",
          "[gl][replay][live-forward]")
{
  // Reference sampling must replay from its requested root even if a caller supplies the
  // reference flag independently of bypass. OnlyDraw publication needs an ordinary root seed.
  CHECK(GLLiveForwardOrdinaryRoot(eReplay_WithoutDraw, false, false));
  CHECK_FALSE(GLLiveForwardOrdinaryRoot(eReplay_WithoutDraw, true, false));
  CHECK_FALSE(GLLiveForwardOrdinaryRoot(eReplay_WithoutDraw, false, true));
  CHECK_FALSE(GLLiveForwardOrdinaryRoot(eReplay_WithoutDraw, true, true));
  CHECK_FALSE(GLLiveForwardOrdinaryRoot(eReplay_OnlyDraw, false, false));
  CHECK_FALSE(GLLiveForwardOrdinaryRoot(eReplay_Full, false, false));
  GLLiveForwardCursor cursor;
  CHECK_FALSE(cursor.CanAdvance(20));
  CHECK_FALSE(cursor.Complete(10, true));
  cursor.Arm(10);
  CHECK_FALSE(cursor.CanAdvance(20));
  CHECK_FALSE(cursor.Complete(11, true));
  CHECK(cursor.event == 0);
  cursor.Arm(10);
  CHECK_FALSE(cursor.Complete(10, false));
  CHECK_FALSE(cursor.CanAdvance(20));
  cursor.Arm(10);
  CHECK(cursor.Complete(10, true));
  CHECK(cursor.CanAdvance(11));
  CHECK(cursor.CanAdvance(20));
  CHECK_FALSE(cursor.CanAdvance(10));
  CHECK_FALSE(cursor.CanAdvance(9));

  // Force and direct driver analysis consume pending provenance before their OnlyDraw. An
  // identical numerical target must not republish a cursor after that reset or an edit.
  cursor.Arm(20);
  cursor.Invalidate();
  CHECK_FALSE(cursor.Complete(20, true));
  CHECK_FALSE(cursor.CanAdvance(30));
  cursor.Arm(30);
  CHECK(cursor.Complete(30, true));
  cursor.Invalidate();
  CHECK_FALSE(cursor.CanAdvance(31));
  CHECK_FALSE(cursor.Complete(30, true));
}

TEST_CASE("OpenGL and GLES live forward exclude only a proven terminal present suffix",
          "[gl][replay][live-forward]")
{
  SDFile file;
  auto append = [&file](uint32_t type) {
    SDChunk *chunk = new SDChunk("live-present"_lit);
    chunk->metadata.chunkID = type;
    file.chunks.push_back(chunk);
  };
  append(uint32_t(SystemChunk::CaptureBegin));
  append(uint32_t(GLChunk::glDrawArrays));
  append(uint32_t(GLChunk::SwapBuffers));
  append(uint32_t(SystemChunk::CaptureEnd));
  for(GLChunk present :
      {GLChunk::SwapBuffers, GLChunk::wglSwapBuffers, GLChunk::glXSwapBuffers,
       GLChunk::CGLFlushDrawable, GLChunk::eglSwapBuffers, GLChunk::eglPostSubBufferNV,
       GLChunk::eglSwapBuffersWithDamageEXT, GLChunk::eglSwapBuffersWithDamageKHR})
  {
    file.chunks[2]->metadata.chunkID = uint32_t(present);
    CHECK(GLLiveForwardTerminalPresent(file, 2));
    CHECK_FALSE(GLLiveForwardCallSupported(present));
  }
  CHECK_FALSE(GLLiveForwardTerminalPresent(file, 1));
  CHECK_FALSE(GLLiveForwardTerminalPresent(file, 3));
  CHECK_FALSE(GLLiveForwardTerminalPresent(file, 4));
  // A swap followed by more drawing, context changes or unknown commands remains unsupported.
  for(GLChunk after : {GLChunk::glDrawArrays, GLChunk::MakeContextCurrent,
                       GLChunk::ImplicitThreadSwitch, GLChunk(~0U)})
  {
    file.chunks[3]->metadata.chunkID = uint32_t(after);
    CHECK_FALSE(GLLiveForwardTerminalPresent(file, 2));
  }
  file.chunks[3]->metadata.chunkID = uint32_t(SystemChunk::CaptureEnd);
  append(uint32_t(GLChunk::glDrawArrays));
  CHECK_FALSE(GLLiveForwardTerminalPresent(file, 2));
}

TEST_CASE("OpenGL live forward cannot start or finish within a shared multi-draw chunk",
          "[gl][replay][live-forward]")
{
  rdcarray<APIEvent> events;
  events.resize(8);
  for(uint32_t i = 1; i < events.size(); i++)
  {
    events[i].eventId = i;
    events[i].fileOffset = i * 10;
  }
  CHECK(GLLiveForwardEventBoundary(events, 1));
  CHECK(GLLiveForwardEventBoundary(events, 4));
  CHECK_FALSE(GLLiveForwardEventBoundary(events, 0));
  CHECK_FALSE(GLLiveForwardEventBoundary(events, 7));
  events[3].fileOffset = events[4].fileOffset = events[5].fileOffset;
  CHECK_FALSE(GLLiveForwardEventBoundary(events, 3));
  CHECK_FALSE(GLLiveForwardEventBoundary(events, 4));
  CHECK_FALSE(GLLiveForwardEventBoundary(events, 5));
  CHECK(GLLiveForwardEventBoundary(events, 6));
  events[7].eventId = 0;
  CHECK_FALSE(GLLiveForwardEventBoundary(events, 6));
}

TEST_CASE("OpenGL and GLES live forward reject hidden execution context and future commands",
          "[gl][replay][live-forward]")
{
  for(GLChunk chunk : {GLChunk::glDrawArrays, GLChunk::glDrawElements, GLChunk::glDispatchCompute,
                       GLChunk::glMultiDrawArrays, GLChunk::glUniform1f, GLChunk::glBindBufferBase,
                       GLChunk::glClear, GLChunk::glMemoryBarrier, GLChunk::glNamedBufferSubData,
                       GLChunk::glTexSubImage2D, GLChunk::glCopyImageSubData,
                       GLChunk::glClearBufferSubData, GLChunk::glFramebufferTextureLayer,
                       GLChunk::glVertexAttribPointer, GLChunk::glVertexAttribIFormat})
    CHECK(GLLiveForwardCallSupported(chunk));
  for(GLChunk chunk :
      {GLChunk::glBeginQuery, GLChunk::glEndQuery, GLChunk::glBeginConditionalRender,
       GLChunk::glEndConditionalRender, GLChunk::glBeginTransformFeedback,
       GLChunk::glEndTransformFeedback, GLChunk::glFenceSync, GLChunk::glWaitSync,
       GLChunk::MakeContextCurrent, GLChunk::ImplicitThreadSwitch, GLChunk::ContextConfiguration,
       GLChunk::glNamedBufferStorage, GLChunk::glTextureView, GLChunk(~0U)})
    CHECK_FALSE(GLLiveForwardCallSupported(chunk));
}

TEST_CASE("OpenGL checkpoint buffer sharing fails closed for unknown and hidden writes",
          "[gl][checkpoint][sharing]")
{
  for(GLChunk chunk :
      {GLChunk::glBufferStorage, GLChunk::glBufferData, GLChunk::glUnmapNamedBufferEXT,
       GLChunk::CoherentMapWrite, GLChunk::glFlushMappedBufferRange, GLChunk::glClearBufferData,
       GLChunk::glClearNamedBufferSubData, GLChunk::glClearTexImage, GLChunk::glReadPixels,
       GLChunk::glGetQueryBufferObjectuiv, GLChunk::glBeginTransformFeedback,
       GLChunk::MakeContextCurrent, GLChunk(~0U)})
    CHECK((GLCheckpointClassifyBufferEffect(chunk) == GLCheckpointBufferEffect::Unknown));
  for(GLChunk chunk :
      {GLChunk::glBindBufferRange, GLChunk::glBindImageTextures, GLChunk::glClearBufferfv,
       GLChunk::glClearNamedFramebufferfi, GLChunk::glProgramUniform4fv, GLChunk::glMemoryBarrier})
    CHECK((GLCheckpointClassifyBufferEffect(chunk) == GLCheckpointBufferEffect::StateOnly));
  for(GLChunk chunk : {GLChunk::glNamedBufferSubDataEXT, GLChunk::glBufferSubDataARB,
                       GLChunk::glCopyBufferSubData, GLChunk::glCopyNamedBufferSubData})
    CHECK((GLCheckpointClassifyBufferEffect(chunk) == GLCheckpointBufferEffect::ExplicitBufferWrite));
  for(GLChunk chunk : {GLChunk::glDrawArrays, GLChunk::glDrawElementsInstancedBaseVertexOES,
                       GLChunk::glMultiDrawElementsIndirectCount, GLChunk::glDispatchComputeIndirect})
    CHECK((GLCheckpointClassifyBufferEffect(chunk) == GLCheckpointBufferEffect::ShaderExecution));
}

TEST_CASE("OpenGL checkpoint potential writes enumerate all native SSBO and atomic slots",
          "[gl][checkpoint][sharing]")
{
  std::set<GLuint> dirty;
  uint32_t bindingQueries = 0;
  auto query = [&](GLenum parameter, GLuint index, GLint &value) {
    if(index == ~0U)
      value = parameter == eGL_MAX_SHADER_STORAGE_BUFFER_BINDINGS ? 64 : 48;
    else
    {
      ++bindingQueries;
      value = (parameter == eGL_SHADER_STORAGE_BUFFER_BINDING && index == 63)   ? 101
              : (parameter == eGL_ATOMIC_COUNTER_BUFFER_BINDING && index == 47) ? 202
                                                                                : 0;
    }
    return true;
  };
  CHECK(GLCheckpointEnumerateBufferWrites(true, true, false, query,
                                          [&](GLuint name) { dirty.insert(name); }));
  CHECK(bindingQueries == 112);
  CHECK(dirty.size() == 2);
  CHECK(dirty.count(101) == 1);
  CHECK(dirty.count(202) == 1);
}

TEST_CASE("OpenGL checkpoint image aliases and incomplete binding evidence forbid buffer sharing",
          "[gl][checkpoint][sharing]")
{
  auto ignore = [](GLuint) {};
  CHECK_FALSE(GLCheckpointEnumerateBufferWrites(
      false, false, true,
      [](GLenum, GLuint index, GLint &value) {
        value = index == ~0U ? 128 : (index == 127 ? 77 : 0);
        return true;
      },
      ignore));
  for(GLint maximum : {0, -1, 65537})
    CHECK_FALSE(GLCheckpointEnumerateBufferWrites(
        true, false, false,
        [maximum](GLenum, GLuint, GLint &value) {
          value = maximum;
          return true;
        },
        ignore));
  CHECK_FALSE(GLCheckpointEnumerateBufferWrites(
      false, true, false,
      [](GLenum, GLuint index, GLint &value) {
        value = index == ~0U ? 48 : 0;
        return index != 47;
      },
      ignore));
  // GLES contexts without these features have no such write slots. Do not issue invalid queries.
  uint32_t queries = 0;
  CHECK(GLCheckpointEnumerateBufferWrites(
      false, false, false,
      [&](GLenum, GLuint, GLint &) {
        ++queries;
        return false;
      },
      ignore));
  CHECK(queries == 0);
}

TEST_CASE("OpenGL checkpoint buffer accounting counts physical versions through eviction",
          "[gl][checkpoint][sharing]")
{
  auto first = std::make_shared<GLCheckpointBufferVersion>();
  first->bytes = 4096;
  auto written = std::make_shared<GLCheckpointBufferVersion>();
  written->bytes = 4096;
  GLCheckpointBufferAccounting budget;
  budget.Add(first.get());
  budget.Add(first.get());
  CHECK(budget.allocated == 4096 + sizeof(GLCheckpointBufferVersion));
  CHECK(budget.shared == 4096);
  // Equal size / write EID does not identify equal GPU contents: a new version costs new storage.
  budget.Add(written.get());
  CHECK(budget.allocated == 2 * (4096 + sizeof(GLCheckpointBufferVersion)));
  auto candidate = first;
  first.reset();
  GLCheckpointBufferAccounting afterEviction;
  afterEviction.Add(candidate.get());
  afterEviction.Add(written.get());
  CHECK(afterEviction.allocated == budget.allocated);
  CHECK(afterEviction.shared == 0);
}

TEST_CASE("OpenGL checkpoint current lineage cannot retain evicted or resized buffer versions",
          "[gl][checkpoint][sharing]")
{
  auto checkpoint = std::make_shared<GLCheckpointBufferVersion>();
  checkpoint->bytes = 1024;
  std::weak_ptr<GLCheckpointBufferVersion> current = checkpoint;
  CHECK((GLCheckpointReusableBufferVersion(current, 1024) == checkpoint));
  CHECK_FALSE(bool(GLCheckpointReusableBufferVersion(current, 2048)));
  auto nextCheckpoint = GLCheckpointReusableBufferVersion(current, 1024);
  checkpoint.reset();
  CHECK_FALSE(current.expired());
  nextCheckpoint.reset();
  CHECK(current.expired());
  CHECK_FALSE(bool(GLCheckpointReusableBufferVersion(current, 1024)));
  // Resetting provenance prevents reuse even when the old immutable checkpoint remains alive.
  checkpoint = std::make_shared<GLCheckpointBufferVersion>();
  checkpoint->bytes = 1024;
  current = checkpoint;
  current.reset();
  CHECK_FALSE(bool(GLCheckpointReusableBufferVersion(current, 1024)));
}

TEST_CASE("OpenGL texture writes distinguish clears from buffer-only state calls",
          "[gl][checkpoint][texture-sharing]")
{
  // Parenthesise the whole comparison: this private enum has no reflected string formatter.
  // Catch should diagnose the boolean predicate without instantiating DoStringise for the enum.
  CHECK((GLCheckpointClassifyTextureEffect(GLChunk::glClear) ==
         GLCheckpointTextureEffect::BoundFramebuffer));
  CHECK((GLCheckpointClassifyTextureEffect(GLChunk::glClearNamedFramebufferfi) ==
         GLCheckpointTextureEffect::ExplicitDestination));
  CHECK((GLCheckpointClassifyTextureEffect(GLChunk::glCopyImageSubData) ==
         GLCheckpointTextureEffect::ExplicitDestination));
  CHECK((GLCheckpointClassifyTextureEffect(GLChunk::glClearTexImage) ==
         GLCheckpointTextureEffect::ExplicitDestination));
  CHECK((GLCheckpointClassifyTextureEffect(GLChunk::glDrawArrays) == GLCheckpointTextureEffect::Draw));
  CHECK((GLCheckpointClassifyTextureEffect(GLChunk::glDispatchComputeIndirect) ==
         GLCheckpointTextureEffect::Dispatch));
  CHECK((GLCheckpointClassifyTextureEffect(GLChunk::glClearColor) ==
         GLCheckpointTextureEffect::StateOnly));
  CHECK((GLCheckpointClassifyTextureEffect(GLChunk::glActiveTexture) ==
         GLCheckpointTextureEffect::StateOnly));
  CHECK((GLCheckpointClassifyTextureEffect(GLChunk::glActiveTextureARB) ==
         GLCheckpointTextureEffect::StateOnly));
  CHECK((GLCheckpointClassifyTextureEffect(GLChunk::glTexParameteri) ==
         GLCheckpointTextureEffect::StateOnly));
  CHECK((GLCheckpointClassifyTextureEffect(GLChunk::glBufferSubData) ==
         GLCheckpointTextureEffect::StateOnly));
  CHECK((GLCheckpointClassifyTextureEffect(GLChunk::glTexSubImage2D) ==
         GLCheckpointTextureEffect::Unknown));
  CHECK((GLCheckpointClassifyTextureEffect(GLChunk::glGenerateMipmap) ==
         GLCheckpointTextureEffect::Unknown));
  CHECK((GLCheckpointClassifyTextureEffect(GLChunk(0xffffffff)) ==
         GLCheckpointTextureEffect::Unknown));
}

TEST_CASE("OpenGL checkpoint renderbuffer sizing includes packed depth and sample storage",
          "[gl][checkpoint][texture-sharing]")
{
  CHECK(GLRenderbufferByteSize(640, 480, 1, eGL_DEPTH24_STENCIL8) == uint64_t(640) * 480 * 4);
  CHECK(GLRenderbufferByteSize(640, 480, 4, eGL_DEPTH32F_STENCIL8) == uint64_t(640) * 480 * 4 * 8);
  CHECK(GLRenderbufferByteSize(640, 480, 4, eGL_RGB10_A2) == uint64_t(640) * 480 * 4 * 4);
  CHECK(GLRenderbufferByteSize(640, 480, 1, eGL_RGBA8) == uint64_t(640) * 480 * 4);
  CHECK(GLRenderbufferByteSize(640, 480, 1, eGL_DEPTH_COMPONENT16) == uint64_t(640) * 480 * 2);
  CHECK(GLRenderbufferByteSize(640, 480, 1, eGL_STENCIL_INDEX8) == uint64_t(640) * 480);
  CHECK(GLRenderbufferByteSize(65536, 65536, 1, eGL_RGBA8) == (uint64_t(1) << 34));
  CHECK(GLRenderbufferByteSize(~0U, ~0U, 2, eGL_RGBA8) == 0);
  CHECK(GLRenderbufferByteSize(0, 480, 1, eGL_RGBA8) == 0);
  CHECK(GLRenderbufferByteSize(640, 480, 0, eGL_RGBA8) == 0);
  CHECK(GLRenderbufferByteSize(640, 480, 1, eGL_NONE) == 0);
}

TEST_CASE("OpenGL texture write evidence includes every FBO slot and fails closed",
          "[gl][checkpoint][texture-sharing]")
{
  const ResourceId color = ResourceIDGen::GetNewUniqueID();
  const ResourceId hiddenColor = ResourceIDGen::GetNewUniqueID();
  const ResourceId depthStencil = ResourceIDGen::GetNewUniqueID();
  const auto resolve = [&](GLenum type, GLuint name) {
    return name == 10 && type == eGL_TEXTURE        ? color
           : name == 11 && type == eGL_TEXTURE      ? hiddenColor
           : name == 12 && type == eGL_RENDERBUFFER ? depthStencil
                                                    : ResourceId();
  };
  bool failQuery = false, missingObject = false;
  const auto query = [&](GLenum attachment, GLenum parameter, GLint &value) {
    if(failQuery && attachment == eGL_COLOR_ATTACHMENT7)
      return false;
    const bool depth = attachment == eGL_DEPTH_ATTACHMENT || attachment == eGL_STENCIL_ATTACHMENT;
    const bool present =
        depth || attachment == eGL_COLOR_ATTACHMENT0 || attachment == eGL_COLOR_ATTACHMENT7;
    if(parameter == eGL_FRAMEBUFFER_ATTACHMENT_OBJECT_TYPE)
      value = present ? (depth ? eGL_RENDERBUFFER : eGL_TEXTURE) : eGL_NONE;
    else
      value = missingObject ? 999 : depth ? 12 : attachment == eGL_COLOR_ATTACHMENT0 ? 10 : 11;
    return true;
  };
  rdcarray<ResourceId> writes;
  REQUIRE(GLCheckpointFramebufferWriteSet(8, query, resolve, writes));
  CHECK(writes == rdcarray<ResourceId>({color, hiddenColor, depthStencil}));
  // No colour write mask, draw route, shader reflection or depth-enable flag filters these IDs.
  failQuery = true;
  CHECK_FALSE(GLCheckpointFramebufferWriteSet(8, query, resolve, writes));
  failQuery = false;
  missingObject = true;
  CHECK_FALSE(GLCheckpointFramebufferWriteSet(8, query, resolve, writes));
  CHECK_FALSE(GLCheckpointFramebufferWriteSet(0, query, resolve, writes));
  CHECK_FALSE(GLCheckpointFramebufferWriteSet(257, query, resolve, writes));
}

TEST_CASE("OpenGL texture versions preserve upstream bytes but never unknown or edited descendants",
          "[gl][checkpoint][texture-sharing]")
{
  const ResourceId upstream = ResourceIDGen::GetNewUniqueID();
  const ResourceId downstream = ResourceIDGen::GetNewUniqueID();
  auto prefix = std::make_shared<GLCheckpointTextureVersion>();
  auto suffix = std::make_shared<GLCheckpointTextureVersion>();
  auto rewritten = std::make_shared<GLCheckpointTextureVersion>();
  std::map<ResourceId, std::weak_ptr<GLCheckpointTextureVersion>> resident = {{upstream, prefix},
                                                                              {downstream, suffix}};
  GLCheckpointApplyTextureWrites(true, {downstream}, resident);
  CHECK(GLCheckpointTextureResident(resident[upstream], prefix));
  CHECK_FALSE(GLCheckpointTextureResident(resident[downstream], suffix));
  CHECK_FALSE(GLCheckpointTextureResident(resident[upstream], rewritten));
  resident[downstream] = suffix;
  // Dropping all invalidated downstream CP owners expires its identity, while retained prefix
  // owners keep upstream content usable across a same-layout CBuffer edit.
  suffix.reset();
  CHECK(resident[downstream].expired());
  CHECK(GLCheckpointTextureResident(resident[upstream], prefix));
  GLCheckpointApplyTextureWrites(false, {}, resident);
  CHECK(resident.empty());
  CHECK_FALSE(GLCheckpointTextureResident({}, prefix));
}

TEST_CASE("OpenGL shared texture content keeps each checkpoint's sampler state",
          "[gl][checkpoint][texture-sharing]")
{
  GLInitialContents first, second;
  first.tex.minFilter = eGL_NEAREST;
  first.tex.wrap[0] = eGL_REPEAT;
  second.tex.minFilter = eGL_LINEAR;
  second.tex.wrap[0] = eGL_CLAMP_TO_EDGE;
  const GLResource copy(NULL, eResTexture, 123);
  const GLInitialContents restored = GLCheckpointTextureRestoreState(first, copy, false);
  CHECK((restored.resource == copy));
  CHECK(restored.tex.minFilter == eGL_NEAREST);
  CHECK(restored.tex.wrap[0] == eGL_REPEAT);
  const GLInitialContents alreadyResident = GLCheckpointTextureRestoreState(second, copy, true);
  CHECK(alreadyResident.resource.name == 0);
  CHECK(alreadyResident.tex.minFilter == eGL_LINEAR);
  CHECK(alreadyResident.tex.wrap[0] == eGL_CLAMP_TO_EDGE);
}

TEST_CASE("OpenGL texture storage counts immutable allocations and validates native shape",
          "[gl][checkpoint][texture-sharing]")
{
  auto prefix = std::make_shared<GLCheckpointTextureVersion>();
  auto changed = std::make_shared<GLCheckpointTextureVersion>();
  prefix->bytes = changed->bytes = 4096;
  GLCheckpointTextureAccounting budget;
  budget.Add(prefix.get());
  budget.Add(prefix.get());
  CHECK(budget.allocated == 4096 + sizeof(GLCheckpointTextureVersion));
  CHECK(budget.shared == 4096);
  budget.Add(changed.get());
  CHECK(budget.allocated == 2 * (4096 + sizeof(GLCheckpointTextureVersion)));
  auto candidate = prefix;
  prefix.reset();
  GLCheckpointTextureAccounting afterEviction;
  afterEviction.Add(candidate.get());
  afterEviction.Add(changed.get());
  CHECK(afterEviction.allocated == budget.allocated);
  CHECK(afterEviction.shared == 0);
  TextureDescription desc;
  desc.width = candidate->width = 32;
  desc.height = candidate->height = 32;
  desc.depth = candidate->depth = 1;
  desc.mips = candidate->mips = 1;
  desc.msSamp = candidate->samples = 1;
  candidate->target = eGL_TEXTURE_2D;
  candidate->format = eGL_RGBA8;
  candidate->source = GLResource(NULL, eResTexture, 321);
  CHECK(GLCheckpointTextureShapeMatches(*candidate, candidate->source, 4096, eGL_TEXTURE_2D,
                                        eGL_RGBA8, desc));
  desc.msSamp = 4;
  CHECK_FALSE(GLCheckpointTextureShapeMatches(*candidate, candidate->source, 4096, eGL_TEXTURE_2D,
                                              eGL_RGBA8, desc));
  desc.msSamp = 1;
  CHECK_FALSE(GLCheckpointTextureShapeMatches(*candidate, candidate->source, 8192, eGL_TEXTURE_2D,
                                              eGL_RGBA8, desc));
  CHECK_FALSE(GLCheckpointTextureShapeMatches(*candidate, GLResource(NULL, eResTexture, 322), 4096,
                                              eGL_TEXTURE_2D, eGL_RGBA8, desc));
}

TEST_CASE("OpenGL patch deltas map original addresses into a compact upload",
          "[gl][bufferpatch][upload]")
{
  ReplayBufferPatch first;
  first.buffer = ResourceIDGen::GetNewUniqueID();
  first.byteOffset = 256;
  first.data = {1, 2, 3};
  ReplayBufferPatch adjacent = first;
  adjacent.byteOffset = 259;
  adjacent.data = {4, 5};
  ReplayBufferPatch other = first;
  other.buffer = ResourceIDGen::GetNewUniqueID();
  other.byteOffset = 4096;
  other.data = {6, 7, 8};
  rdcarray<ReplayBufferPatch> previous = {first, adjacent, other}, next = previous;
  next[0].data[2] = 30;
  next[1].data = {40, 50};
  next[2].data[1] = 70;
  rdcarray<ReplayBufferPatchRange> changes;
  REQUIRE(GetReplayBufferPatchChanges(previous, next, changes).empty());
  REQUIRE(changes.size() == 2);
  rdcarray<GLReplayPatchUploadRange> ranges;
  REQUIRE(GLReplayPatchUploadRanges(previous, next, changes, ranges));
  REQUIRE(ranges.size() == 3);
  CHECK(ranges[0].patch == 0);
  CHECK(ranges[0].patchOffset == 2);
  CHECK(ranges[0].uploadOffset == 2);
  CHECK(ranges[0].bytes == 1);
  CHECK(ranges[1].patch == 1);
  CHECK(ranges[1].patchOffset == 0);
  CHECK(ranges[1].uploadOffset == 3);
  CHECK(ranges[1].bytes == 2);
  CHECK(ranges[2].patch == 2);
  CHECK(ranges[2].patchOffset == 1);
  CHECK(ranges[2].uploadOffset == 6);
  CHECK(ranges[2].bytes == 1);

  bytebuf native = {1, 2, 3, 4, 5, 6, 7, 8};
  const bytebuf expected = {1, 2, 30, 40, 50, 6, 70, 8};
  uint64_t written = 0, rollback = 0;
  uint32_t calls = 0;
  const auto result = GLReplayPatchUpdateUpload(
      previous, next, ranges,
      [&](uint64_t offset, const byte *data, uint64_t size, bool undo) {
        CHECK_FALSE(undo);
        memcpy(native.data() + size_t(offset), data, size_t(size));
        ++calls;
        return true;
      },
      written, rollback);
  CHECK((result == GLReplayPatchUploadResult::Succeeded));
  CHECK((native == expected));
  CHECK(written == 4);
  CHECK(rollback == 0);
  CHECK(calls == 3);

  // Equal values keep the object but issue no native transfer at all.
  REQUIRE(GLReplayPatchUploadRanges(next, next, {}, ranges));
  const auto unchanged = GLReplayPatchUpdateUpload(
      next, next, ranges,
      [&](uint64_t, const byte *, uint64_t, bool) {
        ++calls;
        return false;
      },
      written, rollback);
  CHECK((unchanged == GLReplayPatchUploadResult::Succeeded));
  CHECK(calls == 3);
  CHECK(written == 0);
}

TEST_CASE("OpenGL upload reuse rejects changed layouts and unrepresentable deltas",
          "[gl][bufferpatch][upload]")
{
  ReplayBufferPatch first;
  first.buffer = ResourceIDGen::GetNewUniqueID();
  first.byteOffset = 64;
  first.data = {1, 2};
  ReplayBufferPatch second = first;
  second.byteOffset = 128;
  rdcarray<ReplayBufferPatch> previous = {first, second}, next = previous;
  rdcarray<GLReplayPatchUploadRange> ranges;
  ReplayBufferPatchRange valid = {first.buffer, 64, 1};
  REQUIRE(GLReplayPatchUploadRanges(previous, next, {valid}, ranges));
  CHECK_FALSE(GLReplayPatchUploadRanges({}, next, {valid}, ranges));
  CHECK(ranges.empty());
  CHECK_FALSE(GLReplayPatchUploadRanges(previous, {}, {valid}, ranges));
  next[1].byteOffset++;
  CHECK_FALSE(GLReplayPatchUploadRanges(previous, next, {valid}, ranges));
  next = previous;
  next[1].data.push_back(3);
  CHECK_FALSE(GLReplayPatchUploadRanges(previous, next, {valid}, ranges));
  next = previous;
  for(ReplayBufferPatchRange invalid :
      {ReplayBufferPatchRange{first.buffer, 65, 64}, ReplayBufferPatchRange{first.buffer, 128, 0},
       ReplayBufferPatchRange{first.buffer, UINT64_MAX, 2},
       ReplayBufferPatchRange{ResourceId(), 128, 1}, ReplayBufferPatchRange{first.buffer, 64, 1}})
  {
    CHECK_FALSE(GLReplayPatchUploadRanges(previous, next, {valid, invalid}, ranges));
    CHECK(ranges.empty());
  }
}

TEST_CASE("OpenGL failed upload allocation never overwrites a previously bound payload",
          "[gl][bufferpatch][upload]")
{
  for(uint32_t failure = 0; failure <= 3; ++failure)
  {
    const bytebuf original = {1, 2, 3, 4};
    const bytebuf replacement = {5, 6, 7, 8};
    bytebuf accepted = original, candidate;
    bytebuf *bound = &accepted;
    uint32_t bindings = 0, allocations = 0;
    const bool success = GLReplayPatchAllocateUpload([&]() { return failure != 1; },
                                                     [&]() {
                                                       ++bindings;
                                                       if(failure == 2)
                                                         return false;
                                                       bound = &candidate;
                                                       return true;
                                                     },
                                                     [&]() {
                                                       ++allocations;
                                                       *bound = replacement;
                                                       return failure != 3;
                                                     });
    CHECK(success == (failure == 0));
    CHECK((accepted == original));
    CHECK(bindings == (failure == 1 ? 0U : 1U));
    CHECK(allocations == (failure == 1 || failure == 2 ? 0U : 1U));
    if(failure == 1 || failure == 2)
      CHECK(candidate.empty());
    else
      CHECK((candidate == replacement));
  }
}

TEST_CASE("OpenGL partial upload failure restores all accepted bytes before returning",
          "[gl][bufferpatch][upload]")
{
  ReplayBufferPatch first;
  first.buffer = ResourceIDGen::GetNewUniqueID();
  first.byteOffset = 256;
  first.data = {1, 2, 3, 4};
  ReplayBufferPatch second = first;
  second.byteOffset = 512;
  second.data = {5, 6, 7, 8};
  rdcarray<ReplayBufferPatch> previous = {first, second}, next = previous;
  next[0].data[1] = 20;
  next[1].data[2] = 70;
  rdcarray<ReplayBufferPatchRange> changes;
  REQUIRE(GetReplayBufferPatchChanges(previous, next, changes).empty());
  rdcarray<GLReplayPatchUploadRange> ranges;
  REQUIRE(GLReplayPatchUploadRanges(previous, next, changes, ranges));
  const bytebuf original = {1, 2, 3, 4, 5, 6, 7, 8};
  for(bool rollbackFails : {false, true})
  {
    bytebuf native = original;
    uint32_t writes = 0, undos = 0;
    uint64_t writtenBytes = 0, rollbackBytes = 0;
    const auto result = GLReplayPatchUpdateUpload(
        previous, next, ranges,
        [&](uint64_t offset, const byte *data, uint64_t size, bool undo) {
          if(undo)
          {
            ++undos;
            if(rollbackFails && undos == 1)
              return false;
          }
          else
            ++writes;
          // Simulate a driver writing the failing transfer too: rollback must cover it.
          memcpy(native.data() + size_t(offset), data, size_t(size));
          return undo || writes != 2;
        },
        writtenBytes, rollbackBytes);
    CHECK(writes == 2);
    CHECK(undos == 2);
    CHECK(writtenBytes == 1);
    if(rollbackFails)
    {
      CHECK((result == GLReplayPatchUploadResult::RollbackFailed));
      CHECK((native != original));
      CHECK(rollbackBytes == 4);
    }
    else
    {
      CHECK((result == GLReplayPatchUploadResult::Restored));
      CHECK((native == original));
      CHECK(rollbackBytes == 8);
    }
    CHECK(previous[0].data[1] == 2);
    CHECK(previous[1].data[2] == 7);
  }
}

TEST_CASE("OpenGL persistent uniform patches reject writable binding aliases", "[gl][bufferpatch]")
{
  // Copy writes have a native post-write hook. Shader/TF/image writes are not assumed absent just
  // because an original shader or a particular draw did not use a bound resource.
  CHECK_FALSE(GLReplayPatchBindingCanWrite(eGL_UNIFORM_BUFFER));
  CHECK_FALSE(GLReplayPatchBindingCanWrite(eGL_COPY_WRITE_BUFFER));
  CHECK_FALSE(GLReplayPatchBindingCanWrite(eGL_COPY_READ_BUFFER));
  CHECK(GLReplayPatchBindingCanWrite(eGL_SHADER_STORAGE_BUFFER));
  CHECK(GLReplayPatchBindingCanWrite(eGL_ATOMIC_COUNTER_BUFFER));
  CHECK(GLReplayPatchBindingCanWrite(eGL_TRANSFORM_FEEDBACK_BUFFER));
  CHECK(GLReplayPatchBindingCanWrite(eGL_TEXTURE_BUFFER));
  CHECK(GLReplayPatchBindingCanWrite(eGL_PIXEL_PACK_BUFFER));
}

TEST_CASE("OpenGL shared value edits preserve only a proven pre-consumer prefix",
          "[gl][bufferpatch]")
{
  ReplayBufferPatch patch;
  patch.buffer = ResourceIDGen::GetNewUniqueID();
  patch.byteOffset = 16;
  patch.data = {1, 2, 3, 4};
  ReplayBufferPatch changed = patch;
  changed.data = {5, 6, 7, 8};
  std::map<ResourceId, uint32_t> reads = {{patch.buffer, 18}};
  rdcstr reason;
  CHECK(GLReplayPatchValueCutoff({patch}, {changed}, reads, reason) == 18);
  // A copy at 7 consumes the patch before its first shader read at 18.
  reads[patch.buffer] = 7;
  CHECK(GLReplayPatchValueCutoff({patch}, {changed}, reads, reason) == 7);
  CHECK(GLReplayPatchValueCutoff({}, {changed}, reads, reason) == 0);
  CHECK(GLReplayPatchValueCutoff({patch}, {}, reads, reason) == 0);
  changed.data.pop_back();
  CHECK(GLReplayPatchValueCutoff({patch}, {changed}, reads, reason) == 0);
  changed = patch;
  ++changed.byteOffset;
  CHECK(GLReplayPatchValueCutoff({patch}, {changed}, reads, reason) == 0);
  changed = patch;
  changed.buffer = ResourceIDGen::GetNewUniqueID();
  CHECK(GLReplayPatchValueCutoff({patch}, {changed}, reads, reason) == 0);
  CHECK(GLReplayPatchValueCutoff({patch}, {patch}, {}, reason) == 0);
}

TEST_CASE("OpenGL value deltas retain prefixes before unchanged parameter consumers",
          "[gl][bufferpatch]")
{
  ReplayBufferPatch early;
  early.buffer = ResourceIDGen::GetNewUniqueID();
  early.byteOffset = 16;
  early.data = {1, 2, 3, 4};
  ReplayBufferPatch late = early;
  late.buffer = ResourceIDGen::GetNewUniqueID();
  ReplayBufferPatch changed = late;
  changed.data[2] = 9;
  const rdcarray<ReplayBufferPatch> previous = {early, late};
  const rdcarray<ReplayBufferPatch> next = {early, changed};
  std::map<ResourceId, uint32_t> reads = {{early.buffer, 7}, {late.buffer, 43}};
  rdcarray<ReplayBufferPatchRange> changes;
  REQUIRE(GetReplayBufferPatchChanges(previous, next, changes).empty());
  REQUIRE(changes.size() == 1);
  CHECK(changes[0].byteLength == 1);
  rdcstr reason;
  CHECK(GLReplayPatchValueCutoff(previous, next, reads, reason, &changes) == 43);
  CHECK(reason == "changedBuffersPotentialUniformAndCopyReads");
  // The A/B control omits the delta, and the full-table edit anchor always stays at 7.
  CHECK(GLReplayPatchValueCutoff(previous, next, reads, reason) == 7);
  CHECK(GLReplayPatchValueCutoff(next, next, reads, reason) == 7);
  // Unknown native binding evidence cannot be repaired by a value delta. In particular a shader
  // may consume a physically bound slot which the original reflected GetUsage omitted.
  CHECK(GLReplayPatchValueCutoff(previous, next, {{late.buffer, 43}}, reason, &changes) == 0);
  CHECK(GLReplayPatchValueCutoff(previous, next, {{early.buffer, 7}}, reason, &changes) == 0);
  CHECK(GLReplayPatchValueCutoff(previous, {}, reads, reason, &changes) == 0);
  changed.byteOffset++;
  CHECK(GLReplayPatchValueCutoff(previous, {early, changed}, reads, reason, &changes) == 0);
  // Exceeding the work budget still accepts the edit through the old conservative cutoff.
  CHECK_FALSE(GetReplayBufferPatchChanges(previous, next, changes, 0).empty());
  CHECK(changes.empty());
  CHECK(GLReplayPatchValueCutoff(previous, next, reads, reason) == 7);

  late = early;
  late.byteOffset = 32;
  changed = late;
  changed.data[0] = 9;
  REQUIRE(GetReplayBufferPatchChanges({early, late}, {early, changed}, changes).empty());
  CHECK(GLReplayPatchValueCutoff({early, late}, {early, changed}, reads, reason, &changes) == 7);
}

TEST_CASE("OpenGL checkpoint boundary permits uploads but rejects hidden execution and lifetime",
          "[gl][checkpoint]")
{
  CHECK_FALSE(GLCheckpointUnsupportedFrameCall("glBufferSubData"));
  CHECK_FALSE(GLCheckpointUnsupportedFrameCall("glNamedBufferSubDataEXT"));
  CHECK_FALSE(GLCheckpointUnsupportedFrameCall("glTextureSubImage2DEXT"));
  CHECK_FALSE(GLCheckpointUnsupportedFrameCall("glGenerateMipmap"));
  CHECK_FALSE(GLCheckpointUnsupportedFrameCall("glProgramUniform4fv"));
  CHECK_FALSE(GLCheckpointUnsupportedFrameCall("glNamedFramebufferTexture"));
  CHECK(GLCheckpointUnsupportedFrameCall("glBufferData"));
  CHECK(GLCheckpointUnsupportedFrameCall("glGenBuffers"));
  CHECK(GLCheckpointUnsupportedFrameCall("glCreateTextures"));
  CHECK(GLCheckpointUnsupportedFrameCall("glDeleteVertexArrays"));
  CHECK(GLCheckpointUnsupportedFrameCall("glCopyTexImage2D"));
  CHECK(GLCheckpointUnsupportedFrameCall("glBeginTransformFeedback"));
  CHECK(GLCheckpointUnsupportedFrameCall("glGetQueryBufferObjectuiv"));
  CHECK(GLCheckpointUnsupportedFrameCall("glLinkProgram"));
  CHECK(GLCheckpointUnsupportedFrameCall("glFramebufferTexture2DMultisampleEXT"));
}

TEST_CASE("OpenGL indexed UBO reads use native windows with whole-buffer fallbacks",
          "[gl][bufferpatch]")
{
  const ResourceId buffer = ResourceIDGen::GetNewUniqueID();
  ReplayResourceRange range = GLReplayPatchUniformReadRange(buffer, 1024, 256, 16, true);
  CHECK(range.offset == 256);
  CHECK(range.size == 16);
  for(const rdcpair<int64_t, int64_t> &values :
      {rdcpair<int64_t, int64_t>(0, 0), {-1, 16}, {256, -1}, {1024, 16}, {1008, 32}})
  {
    range = GLReplayPatchUniformReadRange(buffer, 1024, values.first, values.second, true);
    CHECK(range.offset == 0);
    CHECK(range.size == 1024);
  }
  range = GLReplayPatchUniformReadRange(buffer, 1024, 256, 16, false);
  CHECK(range.offset == 0);
  CHECK(range.size == 1024);
  // No 32-bit truncation of desktop native binding offsets.
  range = GLReplayPatchUniformReadRange(buffer, 0x200000000ULL, 0x100000000LL, 16, true);
  CHECK(range.offset == 0x100000000ULL);
  CHECK(range.size == 16);
}

TEST_CASE("OpenGL same-buffer scalar deltas keep prefixes before a later native UBO window",
          "[gl][bufferpatch]")
{
  ReplayBufferPatch early;
  early.buffer = ResourceIDGen::GetNewUniqueID();
  early.byteOffset = 0;
  early.data = {1, 2, 3, 4};
  ReplayBufferPatch late = early;
  late.byteOffset = 256;
  ReplayBufferPatch changed = late;
  changed.data[2] = 9;
  std::map<ResourceId, uint32_t> firstReads = {{early.buffer, 8}};
  rdcarray<ReplayBufferPatchRange> changes;
  REQUIRE(GetReplayBufferPatchChanges({early, late}, {early, changed}, changes).empty());
  ReplayResourceAccessIndex index;
  REQUIRE(index.Register(early.buffer, 0, 512, false));
  REQUIRE(index.RecordRead(8, GLReplayPatchUniformReadRange(early.buffer, 512, 0, 16, true), true));
  REQUIRE(index.RecordRead(44, GLReplayPatchUniformReadRange(early.buffer, 512, 256, 16, true), true));
  REQUIRE(index.FinalizeReads(1, 64, true));
  rdcstr reason;
  bool known = false;
  CHECK(GLReplayPatchRangeValueCutoff({early, late}, {early, changed}, firstReads, index, 64,
                                      reason, known, &changes) == 44);
  CHECK(known);
  // The whole-table prefix anchor and A/B disabled path still start before the earliest UBO.
  CHECK(GLReplayPatchValueCutoff({early, changed}, {early, changed}, firstReads, reason) == 8);
  CHECK(GLReplayPatchRangeValueCutoff({early, late}, {early, changed}, firstReads, index, 64,
                                      reason, known, NULL) == 8);
  CHECK_FALSE(known);
  CHECK(GLReplayPatchRangeValueCutoff({early, late}, {}, firstReads, index, 64, reason, known,
                                      &changes) == 0);
  ReplayBufferPatch moved = changed;
  moved.byteOffset++;
  CHECK(GLReplayPatchRangeValueCutoff({early, late}, {early, moved}, firstReads, index, 64, reason,
                                      known, &changes) == 0);
  index.InvalidateForEdit();
  CHECK(GLReplayPatchRangeValueCutoff({early, late}, {early, changed}, firstReads, index, 64,
                                      reason, known, &changes) == 8);
  CHECK_FALSE(known);
}

TEST_CASE("OpenGL native read proof retains copy and unknown-consumer boundaries",
          "[gl][bufferpatch]")
{
  ReplayBufferPatch old;
  old.buffer = ResourceIDGen::GetNewUniqueID();
  old.byteOffset = 256;
  old.data = {1, 2, 3, 4};
  ReplayBufferPatch next = old;
  next.data[0] = 5;
  rdcarray<ReplayBufferPatchRange> changes;
  REQUIRE(GetReplayBufferPatchChanges({old}, {next}, changes).empty());
  rdcstr reason;
  bool known = false;
  auto cutoff = [&](const ReplayResourceAccessIndex &index, uint32_t first) {
    return GLReplayPatchRangeValueCutoff({old}, {next}, {{old.buffer, first}}, index, 64, reason,
                                         known, &changes);
  };
  ReplayResourceAccessIndex copy;
  REQUIRE(copy.Register(old.buffer, 0, 512, false));
  // Copy sources and non-UBO inputs cover the whole original storage, not the shader member.
  REQUIRE(copy.RecordRead(5, {old.buffer, 0, 0, 512}, true));
  REQUIRE(copy.RecordRead(44, {old.buffer, 0, 256, 16}, true));
  REQUIRE(copy.FinalizeReads(1, 64, true));
  CHECK(cutoff(copy, 5) == 5);
  CHECK(known);
  for(uint32_t mode = 0; mode < 4; mode++)
  {
    ReplayResourceAccessIndex index;
    REQUIRE(index.Register(old.buffer, 0, 512, mode == 1));
    REQUIRE(index.RecordRead(8, {old.buffer, 0, 0, 16}, true));
    if(mode != 2)
      REQUIRE(index.RecordRead(44, {old.buffer, 0, 256, 16}, true));
    if(mode == 0)
      index.RecordUnknown(35);
    index.FinalizeReads(1, 64, mode != 3);
    // Unknown event, uncertain alias, no found read and an incomplete scan all keep fallback.
    CHECK(cutoff(index, 8) == 8);
    CHECK_FALSE(known);
  }
  ReplayResourceAccessIndex budget(16, 16, 1);
  REQUIRE(budget.Register(old.buffer, 0, 512, false));
  REQUIRE(budget.RecordRead(44, {old.buffer, 0, 256, 16}, true));
  REQUIRE(budget.FinalizeReads(1, 64, true));
  CHECK(cutoff(budget, 8) == 8);
  CHECK_FALSE(known);
}

TEST_CASE("OpenGL explicit buffer uploads do not make unrelated static assets writable",
          "[gl][checkpoint]")
{
  const ResourceId uniformBuffer = ResourceIDGen::GetNewUniqueID();
  const ResourceId mesh = ResourceIDGen::GetNewUniqueID();
  GLReplayCheckpointBufferWrites writes(1);
  writes.Record(uniformBuffer);
  writes.Record(uniformBuffer);
  REQUIRE(writes.complete);
  CHECK(writes.buffers.size() == 1);
  CHECK_FALSE(GLCheckpointShareOriginalContents(
      writes.complete, writes.buffers.count(uniformBuffer) != 0, false, false));
  CHECK(GLCheckpointShareOriginalContents(writes.complete, writes.buffers.count(mesh) != 0, false,
                                          false));
  CHECK(GLCheckpointShareOriginalContents(writes.complete, false, false, false));
  writes.Record(mesh);
  CHECK_FALSE(writes.complete);
  CHECK_FALSE(GLCheckpointShareOriginalContents(writes.complete, false, false, false));
  writes.Clear();
  CHECK(writes.complete);
  CHECK(writes.buffers.empty());
  writes.Record(ResourceId());
  CHECK_FALSE(writes.complete);
}

TEST_CASE("OpenGL static checkpoint sharing requires complete frame content evidence",
          "[gl][checkpoint][resourceversions]")
{
  CHECK(GLCheckpointContentEvidenceComplete(GLChunk::glDrawArrays));
  CHECK(GLCheckpointContentEvidenceComplete(GLChunk::glDispatchCompute));
  CHECK(GLCheckpointContentEvidenceComplete(GLChunk::glBindFramebuffer));
  CHECK(GLCheckpointContentEvidenceComplete(GLChunk::glClear));
  // Buffer SubData/copy destinations have a separate complete native write set. Other legal
  // commands still invalidate the static-content proof until their destinations are tracked too.
  CHECK(GLCheckpointContentEvidenceComplete(GLChunk::glNamedBufferSubDataEXT));
  CHECK(GLCheckpointContentEvidenceComplete(GLChunk::glCopyBufferSubData));
  CHECK_FALSE(GLCheckpointContentEvidenceComplete(GLChunk::glTextureSubImage2DEXT));
  CHECK_FALSE(GLCheckpointContentEvidenceComplete(GLChunk::glGenerateMipmap));
  CHECK_FALSE(GLCheckpointContentEvidenceComplete(GLChunk(0xffffffffU)));
  CHECK(GLPatchInitialBufferEvidenceComplete(GLChunk::glDrawArrays));
  CHECK(GLPatchInitialBufferEvidenceComplete(GLChunk::glCopyBufferSubData));
  CHECK(GLPatchInitialBufferEvidenceComplete(GLChunk::glTexParameteri));
  CHECK(GLPatchInitialBufferEvidenceComplete(GLChunk::glBlitFramebuffer));
  CHECK(GLPatchInitialBufferEvidenceComplete(GLChunk::SwapBuffers));
  CHECK(GLPatchInitialBufferEvidenceComplete(GLChunk::glFlush));
  CHECK(GLPatchInitialBufferEvidenceComplete(GLChunk::glFinish));
  CHECK_FALSE(GLPatchInitialBufferEvidenceComplete(GLChunk::glReadPixels));
  CHECK_FALSE(GLPatchInitialBufferEvidenceComplete(GLChunk(0xffffffffU)));

  CHECK(GLCheckpointShareOriginalContents(true, false, false, false));
  CHECK_FALSE(GLCheckpointShareOriginalContents(false, false, false, false));
  CHECK_FALSE(GLCheckpointShareOriginalContents(true, true, false, false));
  CHECK_FALSE(GLCheckpointShareOriginalContents(true, false, true, false));
  CHECK_FALSE(GLCheckpointShareOriginalContents(true, false, false, true));
  CHECK_FALSE(GLCheckpointShareOriginalContents(true, true, true, true));
}
TEST_CASE("OpenGL checkpoint metadata is bounded before uniform expansion",
          "[gl][replay][checkpoint]")
{
  GLCheckpointMetadataBudget exact(64);
  CHECK(exact.Add(4, 16));
  CHECK_FALSE(exact.Add(1, 1));
  CHECK_FALSE(exact.Add(0, 1));
  GLCheckpointMetadataBudget overflow(~0ULL);
  CHECK(overflow.limit == 64ULL * 1024 * 1024);
  CHECK_FALSE(overflow.Add(~0ULL, 2048));
  GLCheckpointMetadataBudget arrays(4096);
  CHECK(GLCheckpointUniformMetadata(arrays, -1, 2, 8));
  CHECK_FALSE(GLCheckpointUniformMetadata(arrays, -1, 1, 8));
  GLCheckpointMetadataBudget invalid(65536);
  CHECK_FALSE(GLCheckpointUniformMetadata(invalid, -1, 1, 1025));
  CHECK_FALSE(GLCheckpointUniformMetadata(invalid, -1, -1, 8));
  // Buffer-backed uniforms never allocate value arrays in the loose-uniform serializer.
  CHECK(GLCheckpointUniformMetadata(invalid, 0, INT_MAX, INT_MAX));
  ShaderConstant scalar;
  scalar.name = "value";
  scalar.type.elements = 2;
  CHECK(GLCheckpointConstantMetadata(invalid, scalar, 0, 0));
  scalar.type.elements = ~0U;
  CHECK_FALSE(GLCheckpointConstantMetadata(invalid, scalar, 0, 0));
  GLCheckpointMetadataBudget nested(65536);
  ShaderConstant structure;
  structure.name = "structure";
  structure.type.elements = 2;
  scalar.type.elements = 1;
  structure.type.members.push_back(scalar);
  CHECK_FALSE(GLCheckpointConstantMetadata(nested, structure, 0, 0));
  CHECK_FALSE(GLCheckpointConstantMetadata(nested, scalar, 0, 33));
}

TEST_CASE("OpenGL checkpoints reject actual object slots beyond their saved state",
          "[gl][checkpoint]")
{
  SDChunk attachment("glNamedFramebufferTexture"_lit);
  SDObject *slot = attachment.AddAndOwnChild(makeSDEnum("attachment"_lit, eGL_COLOR_ATTACHMENT7));
  CHECK_FALSE(GLCheckpointObjectRangeUnsupported("glNamedFramebufferTexture", attachment));
  slot->UInt64() = uint64_t(eGL_COLOR_ATTACHMENT0) + 8;
  CHECK(GLCheckpointObjectRangeUnsupported("glNamedFramebufferTexture", attachment));
  slot->UInt64() = eGL_DEPTH_ATTACHMENT;
  CHECK_FALSE(GLCheckpointObjectRangeUnsupported("glNamedFramebufferTexture", attachment));
  SDChunk read("glReadBuffer"_lit);
  read.AddAndOwnChild(makeSDEnum("mode"_lit, eGL_COLOR_ATTACHMENT8));
  CHECK(GLCheckpointObjectRangeUnsupported("glReadBuffer", read));

  SDChunk vao("glVertexAttribBinding"_lit);
  vao.AddAndOwnChild(makeSDUInt32("attribindex"_lit, 1));
  SDObject *binding = vao.AddAndOwnChild(makeSDUInt32("bindingindex"_lit, 15));
  CHECK_FALSE(GLCheckpointObjectRangeUnsupported("glVertexAttribBinding", vao));
  binding->UInt64() = 16;
  CHECK(GLCheckpointObjectRangeUnsupported("glVertexAttribBinding", vao));

  SDChunk buffers("glBindVertexBuffers"_lit);
  buffers.AddAndOwnChild(makeSDUInt32("first"_lit, 15));
  SDObject *count = buffers.AddAndOwnChild(makeSDInt32("count"_lit, 1));
  CHECK_FALSE(GLCheckpointObjectRangeUnsupported("glBindVertexBuffers", buffers));
  count->Int64() = 2;
  CHECK(GLCheckpointObjectRangeUnsupported("glBindVertexBuffers", buffers));
  count->Int64() = 0;
  CHECK_FALSE(GLCheckpointObjectRangeUnsupported("glBindVertexBuffers", buffers));

  SDChunk draw("glDrawBuffers"_lit);
  SDObject *drawBuffers = draw.AddAndOwnChild(makeSDArray("bufs"_lit));
  drawBuffers->AddAndOwnChild(makeSDEnum("[]"_lit, eGL_COLOR_ATTACHMENT0));
  drawBuffers->AddAndOwnChild(makeSDEnum("[]"_lit, eGL_NONE));
  CHECK_FALSE(GLCheckpointObjectRangeUnsupported("glDrawBuffers", draw));
  drawBuffers->AddAndOwnChild(makeSDEnum("[]"_lit, eGL_COLOR_ATTACHMENT8));
  CHECK(GLCheckpointObjectRangeUnsupported("glDrawBuffers", draw));
}

#endif
