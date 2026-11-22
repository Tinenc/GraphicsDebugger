// The MIT License (MIT). Copyright (c) 2026 RenderDoc contributors.
// See the repository LICENSE.md for the full licence.
#include "vk_test.h"

RD_TEST(VK_Shared_Buffer, VulkanGraphicsTest)
{
  static constexpr const char *Description =
      "Four draws share one named UBO with tint at absolute byte offset 16. Captured "
      "UpdateBuffer and CopyBuffer overwrite the buffer between consumers.";

  int main()
  {
    if(!Init())
      return 3;

    auto upload = [](AllocatedBuffer &buffer, const void *data, size_t size) {
      void *mapped = NULL;
      CHECK_VKR(vmaMapMemory(buffer.allocator, buffer.alloc, &mapped));
      memcpy(mapped, data, size);
      // CPU_TO_GPU does not require coherent memory on every Vulkan implementation.
      CHECK_VKR(vmaFlushAllocation(buffer.allocator, buffer.alloc, 0, size));
      vmaUnmapMemory(buffer.allocator, buffer.alloc);
    };

    const std::string pixel = R"EOSHADER(
#version 450 core
layout(set = 0, binding = 0, std140) uniform SharedParameters
{
  vec4 beforeTint;
  vec4 tint;
  vec4 afterTint;
};
layout(location = 0) out vec4 color;
void main() { color = tint; }
)EOSHADER";

    VkDescriptorSetLayout setLayout = createDescriptorSetLayout(vkh::DescriptorSetLayoutCreateInfo({
        {0, VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER, 1, VK_SHADER_STAGE_FRAGMENT_BIT},
    }));
    VkPipelineLayout layout = createPipelineLayout(vkh::PipelineLayoutCreateInfo({setLayout}));
    vkh::GraphicsPipelineCreateInfo pipeInfo;
    pipeInfo.layout = layout;
    pipeInfo.renderPass = mainWindow->rp;
    pipeInfo.vertexInputState.vertexBindingDescriptions = {vkh::vertexBind(0, DefaultA2V)};
    pipeInfo.vertexInputState.vertexAttributeDescriptions = {
        vkh::vertexAttr(0, 0, DefaultA2V, pos),
        vkh::vertexAttr(1, 0, DefaultA2V, col),
        vkh::vertexAttr(2, 0, DefaultA2V, uv),
    };
    pipeInfo.stages = {
        CompileShaderModule(VKDefaultVertex, ShaderLang::glsl, ShaderStage::vert, "main"),
        CompileShaderModule(pixel, ShaderLang::glsl, ShaderStage::frag, "main"),
    };
    VkPipeline pipe = createGraphicsPipeline(pipeInfo);

    // The descriptor starts at byte zero. std140 places tint at byte 16, independently of the
    // device's minUniformBufferOffsetAlignment. The adjacent vectors are readback sentinels.
    static_assert(sizeof(float) == 4, "Shared buffer fixture requires 32-bit floats");
    const float parameters[12] = {0.125f, 0.25f, 0.5f, 1.0f, 0.2f, 0.45f,
                                  0.7f,   1.0f,  2.0f, 3.0f, 4.0f, 5.0f};
    AllocatedBuffer shared(
        this,
        vkh::BufferCreateInfo(sizeof(parameters),
                             VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT),
        VmaAllocationCreateInfo({0, VMA_MEMORY_USAGE_CPU_TO_GPU}));
    upload(shared, parameters, sizeof(parameters));
    setName(shared.buffer, "SharedParameterBuffer");

    AllocatedBuffer source(this,
                           vkh::BufferCreateInfo(sizeof(parameters), VK_BUFFER_USAGE_TRANSFER_SRC_BIT),
                           VmaAllocationCreateInfo({0, VMA_MEMORY_USAGE_CPU_TO_GPU}));
    upload(source, parameters, sizeof(parameters));
    setName(source.buffer, "SharedParameterCopySource");

    AllocatedBuffer vb(
        this,
        vkh::BufferCreateInfo(sizeof(DefaultTri),
                             VK_BUFFER_USAGE_VERTEX_BUFFER_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT),
        VmaAllocationCreateInfo({0, VMA_MEMORY_USAGE_CPU_TO_GPU}));
    upload(vb, DefaultTri, sizeof(DefaultTri));
    VkDescriptorSet set = allocateDescriptorSet(setLayout);
    vkh::updateDescriptorSets(device, {
                                         vkh::WriteDescriptorSet(
                                             set, 0, VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER,
                                             {vkh::DescriptorBufferInfo(shared.buffer, 0,
                                                                        sizeof(parameters))}),
                                     });

    while(Running())
    {
      // A separately requested proof fixture keeps all four draws in one native pass. Each draw
      // changes a distinct screen region, so omitting an intermediate draw changes final pixels.
      // The normal fixture below retains its captured uploads and separate submissions.
      if(GetEnvVar("RENDERDOC_PORTABLE_FORWARD_FIXTURE") == "1")
      {
        VkCommandBuffer cmd = GetCommandBuffer();
        CHECK_VKR(vkBeginCommandBuffer(cmd, vkh::CommandBufferBeginInfo()));
        VkImage image = StartUsingBackbuffer(cmd, VK_ACCESS_TRANSFER_WRITE_BIT,
                                            VK_IMAGE_LAYOUT_GENERAL);
        vkCmdClearColorImage(cmd, image, VK_IMAGE_LAYOUT_GENERAL,
                            vkh::ClearColorValue(0.03f, 0.04f, 0.05f, 1.0f), 1,
                            vkh::ImageSubresourceRange());
        vkh::cmdPipelineBarrier(cmd,
            {vkh::ImageMemoryBarrier(VK_ACCESS_TRANSFER_WRITE_BIT,
                VK_ACCESS_COLOR_ATTACHMENT_READ_BIT | VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT,
                VK_IMAGE_LAYOUT_GENERAL, VK_IMAGE_LAYOUT_GENERAL, image)}, {}, {},
            VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT);
        vkCmdBeginRenderPass(cmd,
            vkh::RenderPassBeginInfo(mainWindow->rp, mainWindow->GetFB(), mainWindow->scissor),
            VK_SUBPASS_CONTENTS_INLINE);
        vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, pipe);
        vkh::cmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, layout, 0, {set}, {});
        vkh::cmdBindVertexBuffers(cmd, 0, {vb.buffer}, {0});
        vkCmdSetScissor(cmd, 0, 1, &mainWindow->scissor);
        for(uint32_t region = 0; region < 4; region++)
        {
          VkViewport viewport = mainWindow->viewport;
          viewport.width /= 4.0f;
          viewport.x += region * viewport.width;
          vkCmdSetViewport(cmd, 0, 1, &viewport);
          setMarker(cmd, "Live forward region " + std::to_string(region));
          vkCmdDraw(cmd, 3, 1, 0, 0);
        }
        vkCmdEndRenderPass(cmd);
        FinishUsingBackbuffer(cmd, VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT, VK_IMAGE_LAYOUT_GENERAL);
        CHECK_VKR(vkEndCommandBuffer(cmd));
        Submit(0, 1, {cmd});
        CHECK_VKR(vkQueueWaitIdle(queue));
        Present();
        continue;
      }
      VkImage swapimg = VK_NULL_HANDLE;
      for(uint32_t region = 0; region < 4; region++)
      {
        VkCommandBuffer cmd = GetCommandBuffer();
        CHECK_VKR(vkBeginCommandBuffer(cmd, vkh::CommandBufferBeginInfo()));
        if(region == 0)
        {
          swapimg = StartUsingBackbuffer(cmd, VK_ACCESS_TRANSFER_WRITE_BIT, VK_IMAGE_LAYOUT_GENERAL);
          vkCmdClearColorImage(cmd, swapimg, VK_IMAGE_LAYOUT_GENERAL,
                              vkh::ClearColorValue(0.03f, 0.04f, 0.05f, 1.0f), 1,
                              vkh::ImageSubresourceRange());
        }

        if(region == 1 || region == 2)
        {
          // Complete the previous fragment reads before overwriting the same UBO. These are
          // real captured writes of the original value: an active edit must survive both paths.
          vkh::cmdPipelineBarrier(
              cmd, {},
              {vkh::BufferMemoryBarrier(VK_ACCESS_UNIFORM_READ_BIT, VK_ACCESS_TRANSFER_WRITE_BIT,
                                        shared.buffer)},
              {}, VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT);
          if(region == 1)
          {
            setMarker(cmd, "Shared parameter overwrite: vkCmdUpdateBuffer");
            vkCmdUpdateBuffer(cmd, shared.buffer, 0, sizeof(parameters), parameters);
          }
          else
          {
            setMarker(cmd, "Shared parameter overwrite: vkCmdCopyBuffer");
            VkBufferCopy copy = {};
            copy.size = sizeof(parameters);
            vkCmdCopyBuffer(cmd, source.buffer, shared.buffer, 1, &copy);
          }
          vkh::cmdPipelineBarrier(
              cmd, {},
              {vkh::BufferMemoryBarrier(VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_UNIFORM_READ_BIT,
                                        shared.buffer)},
              {}, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT);
        }

        // The default window render pass uses LOAD/STORE and GENERAL. Only the first region
        // transitions from UNDEFINED and clears; later submissions preserve the earlier draws.
        vkh::cmdPipelineBarrier(
            cmd,
            {vkh::ImageMemoryBarrier(
                region == 0 ? VK_ACCESS_TRANSFER_WRITE_BIT : VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT,
                VK_ACCESS_COLOR_ATTACHMENT_READ_BIT | VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT,
                VK_IMAGE_LAYOUT_GENERAL, VK_IMAGE_LAYOUT_GENERAL, swapimg)},
            {}, {},
            region == 0 ? VK_PIPELINE_STAGE_TRANSFER_BIT : VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,
            VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT);

        vkCmdBeginRenderPass(
            cmd, vkh::RenderPassBeginInfo(mainWindow->rp, mainWindow->GetFB(), mainWindow->scissor),
            VK_SUBPASS_CONTENTS_INLINE);
        vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, pipe);
        vkh::cmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, layout, 0, {set}, {});
        VkViewport viewport = mainWindow->viewport;
        viewport.width /= 4.0f;
        viewport.x += region * viewport.width;
        vkCmdSetViewport(cmd, 0, 1, &viewport);
        vkCmdSetScissor(cmd, 0, 1, &mainWindow->scissor);
        vkh::cmdBindVertexBuffers(cmd, 0, {vb.buffer}, {0});
        setMarker(cmd, "Shared parameter region " + std::to_string(region));
        vkCmdDraw(cmd, 3, 1, 0, 0);
        vkCmdEndRenderPass(cmd);

        if(region == 3)
          FinishUsingBackbuffer(cmd, VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT, VK_IMAGE_LAYOUT_GENERAL);
        CHECK_VKR(vkEndCommandBuffer(cmd));
        Submit(region, 4, {cmd});
        CHECK_VKR(vkQueueWaitIdle(queue));
      }
      Present();
    }
    return 0;
  }
};

REGISTER_TEST();
