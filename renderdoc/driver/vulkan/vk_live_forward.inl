/******************************************************************************
 * Ordinary Vulkan live forward replay. Included by vk_core.cpp; independent of
 * checkpoint storage budgets, but uses the same conservative command proof.
 ******************************************************************************/

static bool VulkanLiveForwardOrdinaryRoot(uint32_t start, ReplayLogType type, bool ordinary,
                                          bool bypass, bool naturalReference, bool callback)
{
  return start == 0 && type == eReplay_WithoutDraw && ordinary && !bypass && !naturalReference &&
         !callback;
}

static bool VulkanLiveForwardCanPublish(uint32_t event, uint32_t pending, uint64_t generation,
                                        uint64_t pendingGeneration, bool succeeded)
{
  return succeeded && event != 0 && event == pending && generation == pendingGeneration;
}

static bool VulkanLiveForwardSameCursor(ResourceId previous, uint32_t previousBegin,
                                        ResourceId current, uint32_t currentBegin, uint32_t from,
                                        uint32_t target)
{
  return previous != ResourceId() && previous == current && previousBegin == currentBegin &&
         from != 0 && target > from;
}

static bool VulkanLiveForwardPatchGenerationSafe(uint64_t completed, uint64_t current,
                                                 bool patchesActive, bool patchCaptureValidated)
{
  return completed == current && (!patchesActive || patchCaptureValidated);
}

void WrappedVulkan::InvalidateLiveForward(const char *reason)
{
  m_LiveForward.valid = false;
  m_LiveForward.pendingTarget = 0;
  ++m_LiveForward.generation;
  m_LiveForward.reason = reason;
}

bool WrappedVulkan::CanReplayLiveForward(uint32_t target, rdcstr &reason)
{
  reason = m_LiveForward.reason;
  if(!m_LiveForward.valid)
    return false;
  if(!m_CheckpointCaptureSafe || !m_CheckpointSubmissionCaptureSafe || m_ReplayOptions.apiValidation ||
     Vulkan_Debug_SingleSubmitFlushing())
  {
    reason = "captureProofOrValidation";
    return false;
  }
  // A commit invalidates this cursor before replacing the upload buffers. Once an ordinary
  // target has completed for the new generation, forward replay can use its actual contents.
  // The partial prelude uploads the same values outside the resumed LOAD pass, and captured
  // buffer transfers reapply them through ContextProcessChunk's existing post-write hooks.
  if(!VulkanLiveForwardPatchGenerationSafe(
         m_LiveForward.completedGeneration, m_LiveForward.generation,
         !m_ReplayBufferPatches.empty(),
         m_ReplayPatchCaptureChecked && m_ReplayPatchCaptureError.empty()))
  {
    reason = "staleEditGenerationOrUnvalidatedPatches";
    return false;
  }
  if(m_Partial.partialStack.size() != 1 || m_Partial.partialStack[0].renderPassSuspended)
  {
    reason = "notSingleUnsuspendedPrimary";
    return false;
  }
  const CommandBufferNode &node = m_Partial.partialStack[0];
  if(!VulkanLiveForwardSameCursor(m_LiveForward.commandBuffer, m_LiveForward.beginEvent, node.cmdId,
                                  node.beginEvent, m_LiveForward.eventId, target) ||
     m_PushCommandBuffer != m_LiveForward.pushCommandBuffer ||
     m_RenderState.GetRenderPass() != m_LiveForward.renderPass ||
     m_RenderState.subpass != m_LiveForward.subpass ||
     node.renderPassActive != m_LiveForward.renderPassActive)
  {
    reason = "cursorOrPassChangedOrNotForward";
    return false;
  }
  if(!CheckReplayCheckpointResourceReplacements())
  {
    reason = "bufferOrImageReplacement";
    return false;
  }
  ReplayCheckpoint cursor;
  cursor.eventId = m_LiveForward.eventId;
  cursor.partialStack = m_Partial.partialStack;
  cursor.pushCommandBuffer = m_PushCommandBuffer;
  if(!CheckReplayCheckpointInterval(cursor, target, &reason))
    return false;
  reason = "samePrimarySubmissionAndPass";
  return true;
}

void WrappedVulkan::CompleteLiveForwardTarget(uint32_t eventId)
{
  if(!VulkanLiveForwardCanPublish(eventId, m_LiveForward.pendingTarget, m_LiveForward.generation,
                                  m_LiveForward.pendingGeneration, !HasFatalError()) ||
     m_Partial.partialStack.size() != 1 || m_Partial.partialStack[0].renderPassSuspended ||
     !m_Partial.partialStack[0].childCmdNodes.empty())
  {
    InvalidateLiveForward("targetWithoutCompletedOrdinaryRoot");
    return;
  }
  const CommandBufferNode &node = m_Partial.partialStack[0];
  const auto baked = m_BakedCmdBufferInfo.find(node.cmdId);
  const ActionDescription *action = GetAction(eventId);
  if(!action || action->eventId != eventId ||
     !(action->flags & (ActionFlags::Drawcall | ActionFlags::Dispatch | ActionFlags::MeshDispatch)) ||
     (action->flags & ActionFlags::Indirect) || baked == m_BakedCmdBufferInfo.end() ||
     baked->second.level != VK_COMMAND_BUFFER_LEVEL_PRIMARY ||
     !IsEventInCommandBuffer(&node, eventId, baked->second.eventCount) ||
     !GetResourceManager()->HasResource(node.cmdId) || m_PushCommandBuffer == ResourceId())
  {
    InvalidateLiveForward("targetNotNativePrimaryAction");
    return;
  }
  m_LiveForward.eventId = eventId;
  m_LiveForward.completedGeneration = m_LiveForward.generation;
  m_LiveForward.commandBuffer = node.cmdId;
  m_LiveForward.beginEvent = node.beginEvent;
  m_LiveForward.pushCommandBuffer = m_PushCommandBuffer;
  m_LiveForward.renderPass = m_RenderState.GetRenderPass();
  m_LiveForward.subpass = m_RenderState.subpass;
  m_LiveForward.renderPassActive = node.renderPassActive;
  m_LiveForward.pendingTarget = 0;
  m_LiveForward.valid = true;
  m_LiveForward.reason = "ordinaryTarget";
}

void WrappedVulkan::FinishLiveForwardRequest(uint32_t eventId, bool ordinaryRoot,
                                             bool ordinaryTarget, bool succeeded)
{
  if(!succeeded || HasFatalError())
  {
    InvalidateLiveForward("incompleteOrFailedReplay");
    return;
  }
  if(ordinaryRoot)
  {
    m_LiveForward.pendingTarget = eventId;
    m_LiveForward.pendingGeneration = m_LiveForward.generation;
  }
  else if(ordinaryTarget)
  {
    CompleteLiveForwardTarget(eventId);
  }
}
