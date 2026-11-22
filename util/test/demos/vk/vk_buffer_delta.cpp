// The MIT License (MIT). Copyright (c) 2026 RenderDoc contributors.
// See the repository LICENSE.md for the full licence.
#include "vk_test.h"
#include <memory>

RD_TEST(VK_Buffer_Delta, VulkanGraphicsTest)
{
  static constexpr const char *Description =
      "Four independent output regions read early and late uniform-buffer byte windows. "
      "Supports static descriptor views, dynamic offsets and separate original buffers.";

  int main()
  {
    if(!Init())
      return 3;

    const bool dynamic = GetEnvVar("RENDERDOC_VK_BUFFER_DELTA_DYNAMIC") == "1";
    const bool separate = GetEnvVar("RENDERDOC_VK_BUFFER_DELTA_SEPARATE") == "1";
    const bool stages = GetEnvVar("RENDERDOC_VK_BUFFER_DELTA_STAGES") == "1";
    const VkDeviceSize stride = std::max(VkDeviceSize(256),
                                        physProperties.limits.minUniformBufferOffsetAlignment);
    // Dynamic offsets are uint32_t; retain room for the complete view and its sentinels.
    if(stride > 1024 * 1024)
      return 4;
    const VkDescriptorType type = dynamic ? VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER_DYNAMIC
                                         : VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
    auto upload = [](AllocatedBuffer &buffer, const void *data, size_t size) {
      void *mapped = NULL;
      CHECK_VKR(vmaMapMemory(buffer.allocator, buffer.alloc, &mapped));
      memcpy(mapped, data, size);
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
        {0, type, 1, VK_SHADER_STAGE_FRAGMENT_BIT},
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

    const float early[12] = {0.125f, 0.25f, 0.5f, 1.0f, 0.2f, 0.45f,
                             0.7f, 1.0f, 2.0f, 3.0f, 4.0f, 5.0f};
    const float late[12] = {0.375f, 0.5f, 0.75f, 1.0f, 0.6f, 0.3f,
                            0.15f, 1.0f, 6.0f, 7.0f, 8.0f, 9.0f};
    std::vector<byte> initial(size_t(stride + sizeof(late)), byte(0x5a));
    memcpy(initial.data(), early, sizeof(early));
    memcpy(initial.data() + size_t(stride), late, sizeof(late));
    const VkBufferUsageFlags usage = VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT |
                                     VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    // Dedicated linear allocations make this a positive range-index fixture on every allocator;
    // unrelated VMA image pooling or aliases must not decide the expected acceptance.
    VmaAllocationCreateInfo allocation = {};
    allocation.flags = VMA_ALLOCATION_CREATE_DEDICATED_MEMORY_BIT;
    allocation.usage = VMA_MEMORY_USAGE_CPU_TO_GPU;
    AllocatedBuffer shared(this, vkh::BufferCreateInfo(initial.size(), usage), allocation);
    upload(shared, initial.data(), initial.size());
    setName(shared.buffer, "DeltaSharedParameterBuffer stride=" + std::to_string(stride));
    AllocatedBuffer other(this, vkh::BufferCreateInfo(sizeof(late), usage), allocation);
    upload(other, late, sizeof(late));
    setName(other.buffer, "DeltaLateParameterBuffer");
    AllocatedBuffer vb(
        this, vkh::BufferCreateInfo(sizeof(DefaultTri), VK_BUFFER_USAGE_VERTEX_BUFFER_BIT),
        VmaAllocationCreateInfo({0, VMA_MEMORY_USAGE_CPU_TO_GPU}));
    upload(vb, DefaultTri, sizeof(DefaultTri));

    VkDescriptorSet earlySet = allocateDescriptorSet(setLayout);
    VkDescriptorSet lateSet = allocateDescriptorSet(setLayout);
    const VkDeviceSize lateOffset = separate ? 0 : stride;
    const VkBuffer lateBuffer = separate ? other.buffer : shared.buffer;
    vkh::updateDescriptorSets(device, {
        vkh::WriteDescriptorSet(earlySet, 0, type,
                               {vkh::DescriptorBufferInfo(shared.buffer, 0, sizeof(early))}),
        vkh::WriteDescriptorSet(lateSet, 0, type,
                               {vkh::DescriptorBufferInfo(lateBuffer,
                                                          dynamic ? 0 : lateOffset, sizeof(late))}),
    });

    // Optional completed submissions produce two RTs that later stages leave unchanged. Their
    // image contents can share one immutable GPU version across subsequent checkpoints; all
    // attachment/layout state still belongs to each checkpoint independently.
    std::unique_ptr<AllocatedImage> upstream[2];
    VkFramebuffer upstreamFB[2] = {};
    if(stages)
      for(uint32_t stage = 0; stage < 2; ++stage)
      {
        VmaAllocationCreateInfo imageAllocation = {};
        imageAllocation.flags = VMA_ALLOCATION_CREATE_DEDICATED_MEMORY_BIT;
        imageAllocation.usage = VMA_MEMORY_USAGE_GPU_ONLY;
        upstream[stage].reset(new AllocatedImage(this,
            vkh::ImageCreateInfo(mainWindow->scissor.extent.width, mainWindow->scissor.extent.height,
                                0, mainWindow->format, VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT |
                                   VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT),
            imageAllocation));
        setName(upstream[stage]->image, "DeltaPersistentUpstreamRT" + std::to_string(stage));
        VkImageView view = createImageView(vkh::ImageViewCreateInfo(
            upstream[stage]->image, VK_IMAGE_VIEW_TYPE_2D, mainWindow->format));
        upstreamFB[stage] = createFramebuffer(vkh::FramebufferCreateInfo(
            mainWindow->rp, {view}, mainWindow->scissor.extent));
      }

    while(Running())
    {
      if(stages)
        for(uint32_t stage = 0; stage < 2; ++stage)
        {
          VkCommandBuffer prefix = GetCommandBuffer();
          CHECK_VKR(vkBeginCommandBuffer(prefix, vkh::CommandBufferBeginInfo()));
          if(stage == 0)
            for(uint32_t initialStage = 0; initialStage < 2; ++initialStage)
            {
              // Both full-resource witnesses must have defined contents at the first draw.
              // Deferring RT1's UNDEFINED transition/clear until its own stage would let an
              // earlier readback compare arbitrary previous-frame bytes against a later reset.
              vkh::cmdPipelineBarrier(prefix, {vkh::ImageMemoryBarrier(0, VK_ACCESS_TRANSFER_WRITE_BIT,
                  VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_GENERAL, upstream[initialStage]->image)});
              vkCmdClearColorImage(prefix, upstream[initialStage]->image, VK_IMAGE_LAYOUT_GENERAL,
                                  vkh::ClearColorValue(0.07f, 0.09f, 0.11f, 1.0f), 1,
                                  vkh::ImageSubresourceRange());
            }
          vkh::cmdPipelineBarrier(prefix, {vkh::ImageMemoryBarrier(VK_ACCESS_TRANSFER_WRITE_BIT,
              VK_ACCESS_COLOR_ATTACHMENT_READ_BIT | VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT,
              VK_IMAGE_LAYOUT_GENERAL, VK_IMAGE_LAYOUT_GENERAL, upstream[stage]->image)});
          vkCmdBeginRenderPass(prefix,
              vkh::RenderPassBeginInfo(mainWindow->rp, upstreamFB[stage], mainWindow->scissor),
              VK_SUBPASS_CONTENTS_INLINE);
          vkCmdBindPipeline(prefix, VK_PIPELINE_BIND_POINT_GRAPHICS, pipe);
          vkh::cmdBindVertexBuffers(prefix, 0, {vb.buffer}, {0});
          vkCmdSetScissor(prefix, 0, 1, &mainWindow->scissor);
          vkCmdSetViewport(prefix, 0, 1, &mainWindow->viewport);
          const VkDescriptorSet set = stage == 0 ? earlySet : lateSet;
          const uint32_t offset = uint32_t(stage == 0 ? 0 : lateOffset);
          vkCmdBindDescriptorSets(prefix, VK_PIPELINE_BIND_POINT_GRAPHICS, layout, 0, 1, &set,
                                  dynamic ? 1 : 0, dynamic ? &offset : NULL);
          setMarker(prefix, "Delta upstream stage " + std::to_string(stage));
          vkCmdDraw(prefix, 3, 1, 0, 0);
          vkCmdEndRenderPass(prefix);
          CHECK_VKR(vkEndCommandBuffer(prefix));
          Submit(stage, 3, {prefix});
        }
      VkCommandBuffer cmd = GetCommandBuffer();
      CHECK_VKR(vkBeginCommandBuffer(cmd, vkh::CommandBufferBeginInfo()));
      VkImage image = StartUsingBackbuffer(cmd, VK_ACCESS_TRANSFER_WRITE_BIT, VK_IMAGE_LAYOUT_GENERAL);
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
      vkh::cmdBindVertexBuffers(cmd, 0, {vb.buffer}, {0});
      vkCmdSetScissor(cmd, 0, 1, &mainWindow->scissor);
      for(uint32_t region = 0; region < 4; region++)
      {
        const bool earlyConsumer = region < 2;
        const VkDescriptorSet set = earlyConsumer ? earlySet : lateSet;
        const uint32_t dynamicOffset = uint32_t(earlyConsumer ? 0 : lateOffset);
        vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_GRAPHICS, layout, 0, 1, &set,
                                dynamic ? 1 : 0, dynamic ? &dynamicOffset : NULL);
        VkViewport viewport = mainWindow->viewport;
        viewport.width /= 4.0f;
        viewport.x += region * viewport.width;
        vkCmdSetViewport(cmd, 0, 1, &viewport);
        setMarker(cmd, std::string(earlyConsumer ? "Delta early region " : "Delta late region ") +
                           std::to_string(region) + " parameterOffset=" +
                           std::to_string((earlyConsumer ? 0 : lateOffset) + 16));
        vkCmdDraw(cmd, 3, 1, 0, 0);
      }
      vkCmdEndRenderPass(cmd);
      FinishUsingBackbuffer(cmd, VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT, VK_IMAGE_LAYOUT_GENERAL);
      CHECK_VKR(vkEndCommandBuffer(cmd));
      Submit(stages ? 2 : 0, stages ? 3 : 1, {cmd});
      CHECK_VKR(vkQueueWaitIdle(queue));
      Present();
    }
    return 0;
  }
};

REGISTER_TEST();
