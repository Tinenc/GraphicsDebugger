// Native read bounds for shared CBuffer edits. Included after the command-state whitelist.
// Capture metadata is built once per set of edited buffers. Descriptor history is a union of
// every captured slot version, so recording-time heap contents cannot hide a later consumer.

void D3D12CommandData::ObserveBufferPatchReads(BakedCmdListInfo &info, D3D12Chunk chunk,
                                               uint32_t relativeEvent)
{
  if(chunk == D3D12Chunk::List_Reset)
  {
    info.bufferPatchReadFailure = "";
    info.bufferPatchReadFailureChunk = info.bufferPatchReadFailureEvent = 0;
  }
  if(!info.bufferPatchReadsComplete)
    return;
  const auto fail = [&](const char *reason) {
    if(info.bufferPatchReadsComplete)
    {
      info.bufferPatchReadFailure = reason;
      info.bufferPatchReadFailureChunk = uint32_t(chunk);
      info.bufferPatchReadFailureEvent = relativeEvent;
    }
    info.bufferPatchReadsComplete = false;
  };
  bool graphics = chunk == D3D12Chunk::List_DrawInstanced ||
                  chunk == D3D12Chunk::List_DrawIndexedInstanced ||
                  chunk == D3D12Chunk::List_DispatchMesh;
  bool compute = chunk == D3D12Chunk::List_Dispatch;
  const bool indirect = chunk == D3D12Chunk::List_ExecuteIndirect;
  BakedCmdListInfo::BufferPatchReadOp read;
  read.relativeEvent = relativeEvent;
  if(indirect)
  {
    const uint32_t node = info.replayLastIndirectEvent;
    if(node >= info.eventNodes.size() || !info.eventNodes[node].hasExecuteData ||
       !info.eventNodes[node].executeData.sig)
    {
      fail("missingIndirectSignature");
      return;
    }
    const auto &execute = info.eventNodes[node].executeData;
    const auto &signature = execute.sig->sig;
    if(!ReplayUAVDeclarationBindingsKnown(chunk, &signature) || node != relativeEvent)
    {
      fail("unsupportedIndirectSignature");
      return;
    }
    const auto stages = ReplayWriteStages(chunk, &signature);
    graphics = stages.graphics;
    compute = stages.compute;
    for(const auto &argument : signature.arguments)
      if(argument.Type == D3D12_INDIRECT_ARGUMENT_TYPE_CONSTANT_BUFFER_VIEW ||
         argument.Type == D3D12_INDIRECT_ARGUMENT_TYPE_SHADER_RESOURCE_VIEW ||
         argument.Type == D3D12_INDIRECT_ARGUMENT_TYPE_VERTEX_BUFFER_VIEW ||
         argument.Type == D3D12_INDIRECT_ARGUMENT_TYPE_INDEX_BUFFER_VIEW)
        read.generatedAddresses = true;
    const uint64_t count = execute.maxCount;
    const uint64_t stride = signature.ByteStride, packed = signature.PackedByteSize;
    if(count && (execute.sourceArgs == ResourceId() || !stride || packed > stride ||
                 count - 1 > (~0ULL - packed) / stride))
    {
      fail("invalidIndirectArgumentExtent");
      return;
    }
    if(count)
      read.direct.push_back(
          {execute.sourceArgs, 0, execute.sourceArgsOffset, (count - 1) * stride + packed});
    if(execute.sourceCount != ResourceId())
      read.direct.push_back({execute.sourceCount, 0, execute.sourceCountOffset, 4});
  }
  const bool copy = chunk == D3D12Chunk::List_CopyResource ||
                    chunk == D3D12Chunk::List_CopyBufferRegion ||
                    chunk == D3D12Chunk::List_CopyTextureRegion;
  if(IsReplayClearPrefixState(chunk) || chunk == D3D12Chunk::List_ResourceBarrier)
    return;
  // Clears, query resolves and discards don't read parameter-buffer bytes, but their predicate
  // may do so. Unknown/generated accesses retain the original whole-prefix invalidation.
  if(!graphics && !compute && !copy && chunk != D3D12Chunk::List_ClearRenderTargetView &&
     chunk != D3D12Chunk::List_ClearDepthStencilView &&
     chunk != D3D12Chunk::List_ClearUnorderedAccessViewUint &&
     chunk != D3D12Chunk::List_ClearUnorderedAccessViewFloat &&
     chunk != D3D12Chunk::List_ResolveQueryData && chunk != D3D12Chunk::List_DiscardResource)
  {
    fail("unsupportedNativeChunk");
    return;
  }
  auto direct = [&](ResourceId id, uint64_t offset, uint64_t size) {
    if(id != ResourceId())
      read.direct.push_back({id, 0, offset, size});
  };
  const D3D12RenderState &state = info.state;
  const ReplayPSOUAVProof *declarations =
      (graphics != compute) ? &GetReplayPSOUAVProof(state.pipe) : NULL;
  direct(state.predication.buffer, state.predication.offset, 8);
  if(graphics)
  {
    direct(state.ibuffer.buf, state.ibuffer.offs, state.ibuffer.size);
    for(const auto &vertex : state.vbuffers)
      direct(vertex.buf, vertex.offs, vertex.size);
    for(const auto &stream : state.streamouts)
      direct(stream.countbuf, stream.countoffs, 8);
  }
  for(const auto *root : {&state.graphics, &state.compute})
  {
    if((root == &state.graphics && !graphics) || (root == &state.compute && !compute))
      continue;
    auto *rm = m_pDevice ? m_pDevice->GetResourceManager() : NULL;
    auto *signature = rm && rm->HasResource(root->rootsig) && !rm->HasReplacement(root->rootsig)
                          ? rm->GetResAs<WrappedID3D12RootSignature>(root->rootsig)
                          : NULL;
    if(!signature ||
       (signature->sig.Flags & D3D12_ROOT_SIGNATURE_FLAG_CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED))
    {
      fail(!signature ? "missingRootSignature" : "directlyIndexedRootSignature");
      break;
    }
    for(size_t i = 0; i < signature->sig.Parameters.size(); i++)
    {
      const auto &parameter = signature->sig.Parameters[i];
      if(parameter.ParameterType == D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS)
        continue;
      if(i >= root->sigelems.size())
      {
        fail("missingRootArgument");
        break;
      }
      const auto &element = root->sigelems[i];
      if(parameter.ParameterType != D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE)
      {
        const bool correctType =
            (parameter.ParameterType == D3D12_ROOT_PARAMETER_TYPE_CBV && element.type == eRootCBV) ||
            (parameter.ParameterType == D3D12_ROOT_PARAMETER_TYPE_SRV && element.type == eRootSRV) ||
            (parameter.ParameterType == D3D12_ROOT_PARAMETER_TYPE_UAV && element.type == eRootUAV);
        if(!correctType)
          fail("rootDescriptorTypeMismatch");
        else if(parameter.ParameterType == D3D12_ROOT_PARAMETER_TYPE_CBV && declarations &&
                declarations->complete && declarations->constantBuffersComplete)
        {
          uint64_t bytes = 0;
          for(const auto &cb : declarations->constantBuffers)
            if(cb.space == parameter.Descriptor.RegisterSpace &&
               cb.first <= parameter.Descriptor.ShaderRegister &&
               parameter.Descriptor.ShaderRegister - cb.first < cb.count &&
               ReplayUAVVisibilityMatches(parameter.ShaderVisibility, cb.stage))
              bytes = RDCMAX<uint64_t>(bytes, cb.byteSize);
          // Bounds come from the original executable declaration, not an external debug blob.
          // A root argument unused by every executable stage is not a buffer consumer.
          if(bytes)
            direct(element.id, element.offset, bytes);
        }
        else
          // Unknown declarations and root SRV/UAVs retain the whole remaining allocation.
          direct(element.id, element.offset, ~0ULL);
        continue;
      }
      auto *heap = rm->HasResource(element.id) && !rm->HasReplacement(element.id)
                       ? rm->GetResAs<WrappedID3D12DescriptorHeap>(element.id)
                       : NULL;
      if(element.type != eRootTable || !heap || !state.heaps.contains(element.id))
      {
        fail("invalidDescriptorTableBinding");
        break;
      }
      uint64_t previousEnd = 0;
      for(const D3D12_DESCRIPTOR_RANGE1 &range : parameter.ranges)
      {
        uint64_t offset = range.OffsetInDescriptorsFromTableStart;
        if(offset == D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND)
          offset = previousEnd;
        const uint64_t capacity = heap->GetNumDescriptors();
        if(element.offset > capacity || offset > capacity - element.offset)
        {
          fail("tableBaseOutOfRange");
          break;
        }
        const uint64_t remaining = capacity - element.offset - offset;
        const uint64_t declared = range.NumDescriptors == UINT_MAX ? remaining : range.NumDescriptors;
        // A declared array can overhang the bound heap when only an in-heap subset is used.
        // Every legal read is within this intersection. Keep APPEND based on the declaration,
        // not the clipped count; a later invalid table base still fails closed above.
        const uint64_t count = RDCMIN(declared, remaining);
        previousEnd = offset + declared;
        if(range.RangeType == D3D12_DESCRIPTOR_RANGE_TYPE_SAMPLER)
          continue;
        if(heap->GetDesc().Type != D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV ||
           (range.RangeType != D3D12_DESCRIPTOR_RANGE_TYPE_CBV &&
            range.RangeType != D3D12_DESCRIPTOR_RANGE_TYPE_SRV &&
            range.RangeType != D3D12_DESCRIPTOR_RANGE_TYPE_UAV))
          fail("descriptorRangeTypeMismatch");
        else if(count)
          read.tables.push_back({element.id, uint32_t(element.offset + offset), uint32_t(count)});
      }
    }
  }
  const size_t entries = 1 + read.direct.size() + read.tables.size();
  if(entries > 1000000 || m_BufferPatchReadEntries > 1000000 - entries)
    fail("nativeReadMetadataBudget");
  if(info.bufferPatchReadsComplete)
  {
    m_BufferPatchReadEntries += entries;
    info.bufferPatchReads.push_back(std::move(read));
  }
}

bool D3D12CommandData::GetBufferPatchRangeConsumer(const std::map<ResourceId, uint64_t> &buffers,
                                                   const rdcarray<ReplayBufferPatchRange> &changes,
                                                   uint32_t &first)
{
  first = ~0U;
  if(!m_BufferPatchReadIndexPrepared || m_BufferPatchReadResources != buffers)
  {
    m_BufferPatchReadIndex.Clear();
    m_BufferPatchReadResources = buffers;
    m_BufferPatchReadIndexPrepared = true;
    bool complete = !buffers.empty() && m_Events.size() > 1 && m_Events.size() <= UINT_MAX;
    const char *failure = complete ? "" : "invalidReadScope";
    ResourceId failedList, failedResource, failedHeap;
    uint32_t failedEvent = 0, failedChunk = 0;
    const auto fail = [&](const char *reason) {
      if(!failure[0])
        failure = reason;
      complete = false;
    };
    for(const auto &buffer : buffers)
      if(!m_BufferPatchReadIndex.Register(buffer.first, 0, buffer.second, false))
        fail("resourceRegistrationFailed");
    size_t work = 1000000;
    auto add = [&](uint32_t event, ResourceId id, uint64_t offset, uint64_t size) {
      const auto buffer = buffers.find(id);
      if(buffer == buffers.end())
        return;
      if(!work || offset >= buffer->second || !size)
      {
        if(complete)
        {
          failedResource = id;
          failedEvent = event;
        }
        fail(!work ? "readIndexWorkBudget" : "invalidBufferReadExtent");
        return;
      }
      work--;
      if(size == ~0ULL)
        size = buffer->second - offset;
      if(size > buffer->second - offset ||
         !m_BufferPatchReadIndex.RecordRead(event, {id, 0, offset, size}, true))
      {
        if(complete)
        {
          failedResource = id;
          failedEvent = event;
        }
        fail("bufferReadRecordFailed");
      }
    };
    // Secondary/generated commands need a separate access proof; do not infer it from their
    // reflected resources or from a parent's bindings.
    // Serialise_Reset queries both execution maps with operator[], creating empty secondary
    // entries during ordinary primary replay. Only an actual submitted bundle needs its own proof.
    for(const auto &secondary : m_Partial[Secondary].cmdListExecs)
      if(!secondary.second.empty())
      {
        failedList = secondary.first;
        fail("submittedSecondaryCommands");
        break;
      }
    // Reject known incomplete command metadata before building potentially large descriptor
    // histories. Previously the first edit paid hundreds of milliseconds before discovering a
    // later list had already failed during loading. This removes work instead of deferring it.
    for(const auto &submission : m_Partial[Primary].cmdListExecs)
    {
      if(!complete)
        break;
      if(submission.second.empty())
        continue;
      const auto found = m_BakedCmdListInfo.find(submission.first);
      if(found == m_BakedCmdListInfo.end() || !found->second.bufferPatchReadsComplete)
      {
        failedList = submission.first;
        if(found != m_BakedCmdListInfo.end())
        {
          failedEvent = found->second.bufferPatchReadFailureEvent;
          failedChunk = found->second.bufferPatchReadFailureChunk;
        }
        fail(found == m_BakedCmdListInfo.end()         ? "missingSubmittedCommandList"
             : found->second.bufferPatchReadFailure[0] ? found->second.bufferPatchReadFailure
                                                       : "incompleteSubmittedCommandList");
      }
    }
    for(const auto &submission : m_Partial[Primary].cmdListExecs)
    {
      if(!complete)
        break;
      if(submission.second.empty())
        continue;
      const auto found = m_BakedCmdListInfo.find(submission.first);
      if(found == m_BakedCmdListInfo.end() || !found->second.bufferPatchReadsComplete)
      {
        if(complete)
        {
          failedList = submission.first;
          if(found != m_BakedCmdListInfo.end())
          {
            failedEvent = found->second.bufferPatchReadFailureEvent;
            failedChunk = found->second.bufferPatchReadFailureChunk;
          }
        }
        fail(found == m_BakedCmdListInfo.end()         ? "missingSubmittedCommandList"
             : found->second.bufferPatchReadFailure[0] ? found->second.bufferPatchReadFailure
                                                       : "incompleteSubmittedCommandList");
        break;
      }
      const auto &info = found->second;
      const auto &copies = info.bufferPatchCopyReads;
      for(uint32_t base : submission.second)
      {
        if(!base || !info.eventCount || uint64_t(base) + info.eventCount > m_Events.size())
        {
          if(complete)
          {
            failedList = submission.first;
            failedEvent = base;
          }
          fail("invalidSubmittedEventExtent");
          break;
        }
        for(const auto &chunk : info.checkpointChunks)
        {
          if(!work)
          {
            fail("commandReadWorkBudget");
            break;
          }
          work--;
          if(chunk.type != D3D12Chunk::List_CopyResource &&
             chunk.type != D3D12Chunk::List_CopyBufferRegion &&
             chunk.type != D3D12Chunk::List_CopyTextureRegion)
            continue;
          if(!chunk.eventAfter || copies.find(chunk.eventAfter - 1) == copies.end())
          {
            if(complete)
            {
              failedList = submission.first;
              failedChunk = uint32_t(chunk.type);
              failedEvent = base + (chunk.eventAfter ? chunk.eventAfter - 1 : 0);
            }
            fail("missingCopySourceEvidence");
            break;
          }
          for(ResourceId source : copies.at(chunk.eventAfter - 1))
            add(base + chunk.eventAfter - 1, source, 0, ~0ULL);
        }
        for(const auto &read : info.bufferPatchReads)
        {
          if(!work || read.relativeEvent >= info.eventCount)
          {
            if(complete)
            {
              failedList = submission.first;
              failedEvent = base;
            }
            fail(!work ? "commandReadWorkBudget" : "invalidRelativeReadEvent");
            break;
          }
          work--;
          const uint32_t event = base + read.relativeEvent;
          // Generated GPU addresses may differ after a parameter edit. Cover every byte of
          // each queried original buffer at this command, never just captured argument values.
          // This conservative union remains useful for later reads while failing closed on
          // unsupported root UAV/ray signatures above. It does not authorize indirect CBV edits.
          if(read.generatedAddresses)
            for(const auto &buffer : buffers)
              add(event, buffer.first, 0, buffer.second);
          for(const auto &range : read.direct)
            add(event, range.resource, range.offset, range.size);
          for(const auto &table : read.tables)
          {
            auto *rm = m_pDevice ? m_pDevice->GetResourceManager() : NULL;
            auto *heap = rm && rm->HasResource(table.heap) && !rm->HasReplacement(table.heap)
                             ? rm->GetResAs<WrappedID3D12DescriptorHeap>(table.heap)
                             : NULL;
            for(const auto &buffer : buffers)
            {
              rdcarray<rdcpair<uint64_t, uint64_t>> ranges;
              if(!heap || !heap->GatherReplayBufferReadRanges(buffer.first, table.first,
                                                              table.count, ranges, work))
              {
                if(complete)
                {
                  failedList = submission.first;
                  failedResource = buffer.first;
                  failedHeap = table.heap;
                  failedEvent = event;
                }
                fail(!heap ? "missingDescriptorHeap" : "descriptorHistoryIncompleteOrBudget");
              }
              for(const auto &range : ranges)
                add(event, buffer.first, range.first, range.second);
            }
          }
        }
        if(!complete)
          break;
      }
      if(!complete)
        break;
    }
    m_BufferPatchReadIndex.FinalizeReads(1, uint32_t(m_Events.size() - 1), complete);
    // One receipt per rebuilt index, not per command/query. Preserve the first failure so an
    // incomplete dependency proof has a concrete native cause without an unbounded logging loop.
    if(m_pDevice && Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
      AppendReplayTimingLog(StringFormat::Fmt(
          "api=D3D12BufferReadIndex op=build complete=%u reason=%s buffers=%zu events=%zu "
          "remainingWork=%zu list=%s resource=%s heap=%s event=%u chunk=%u",
          complete ? 1U : 0U, failure[0] ? failure : "none", buffers.size(), m_Events.size(), work,
          ToStr(failedList).c_str(), ToStr(failedResource).c_str(), ToStr(failedHeap).c_str(),
          failedEvent, failedChunk));
  }
  for(const ReplayBufferPatchRange &change : changes)
  {
    const auto query = m_BufferPatchReadIndex.FirstRead(
        {change.buffer, 0, change.byteOffset, change.byteLength}, 1, uint32_t(m_Events.size() - 1));
    if(!query.complete)
      return false;
    if(query.found)
      first = RDCMIN(first, query.eventId);
  }
  return !changes.empty();
}

#if ENABLED(ENABLE_UNIT_TESTS)
TEST_CASE("D3D12 native read diagnostics preserve the first unresolved command",
          "[d3d12][replay][bufferpatch]")
{
  D3D12CommandData cmd;
  BakedCmdListInfo info;
  cmd.ObserveBufferPatchReads(info, D3D12Chunk::List_ExecuteIndirect, 7);
  CHECK_FALSE(info.bufferPatchReadsComplete);
  CHECK(rdcstr(info.bufferPatchReadFailure) == "missingIndirectSignature");
  CHECK(info.bufferPatchReadFailureChunk == uint32_t(D3D12Chunk::List_ExecuteIndirect));
  CHECK(info.bufferPatchReadFailureEvent == 7);
  cmd.ObserveBufferPatchReads(info, D3D12Chunk::List_DrawInstanced, 9);
  CHECK(info.bufferPatchReadFailureEvent == 7);
  CHECK(info.bufferPatchReads.empty());
  // ObserveCheckpointChunk reinitialises completeness before forwarding the captured Reset.
  info.bufferPatchReadsComplete = true;
  cmd.ObserveBufferPatchReads(info, D3D12Chunk::List_Reset, 0);
  CHECK(rdcstr(info.bufferPatchReadFailure).empty());
  CHECK(info.bufferPatchReadFailureChunk == 0);
  CHECK(info.bufferPatchReadFailureEvent == 0);
  // A subsequent unresolved native root signature retains a distinct, actionable cause.
  cmd.ObserveBufferPatchReads(info, D3D12Chunk::List_DrawInstanced, 3);
  CHECK_FALSE(info.bufferPatchReadsComplete);
  CHECK(rdcstr(info.bufferPatchReadFailure) == "missingRootSignature");
  CHECK(info.bufferPatchReadFailureEvent == 3);
}

TEST_CASE("D3D12 byte consumers use submitted EIDs and preserve independent parameter ranges",
          "[d3d12][replay][bufferpatch]")
{
  D3D12CommandData cmd;
  const ResourceId buffer = ResourceIDGen::GetNewUniqueID();
  const ResourceId firstList = ResourceIDGen::GetNewUniqueID();
  const ResourceId secondList = ResourceIDGen::GetNewUniqueID();
  auto &early = cmd.m_BakedCmdListInfo[firstList];
  auto &late = cmd.m_BakedCmdListInfo[secondList];
  early.eventCount = late.eventCount = 10;
  BakedCmdListInfo::BufferPatchReadOp a, b;
  a.relativeEvent = 4;
  a.direct = {{buffer, 0, 256, 256}};
  b.relativeEvent = 6;
  b.direct = {{buffer, 0, 512, 256}};
  early.bufferPatchReads = {a};
  late.bufferPatchReads = {b};
  cmd.m_Events.resize(101);
  cmd.m_Partial[D3D12CommandData::Primary].cmdListExecs[firstList] = {10, 60};
  cmd.m_Partial[D3D12CommandData::Primary].cmdListExecs[secondList] = {40};
  uint32_t first = 0;
  REQUIRE(cmd.GetBufferPatchRangeConsumer({{buffer, 1024}}, {{buffer, 512, 4}}, first));
  CHECK(first == 46);
  REQUIRE(cmd.GetBufferPatchRangeConsumer({{buffer, 1024}}, {{buffer, 256, 4}}, first));
  CHECK(first == 14);
  REQUIRE(cmd.GetBufferPatchRangeConsumer({{buffer, 1024}}, {{buffer, 508, 8}}, first));
  CHECK(first == 14);
  REQUIRE(cmd.GetBufferPatchRangeConsumer({{buffer, 1024}}, {{buffer, 0, 4}}, first));
  CHECK(first == ~0U);
  // An indirect GPU address is not frozen to the originally captured resource/offset. It
  // conservatively consumes all queried bytes at that command, including a previously unused
  // lane. Earlier independent direct reads still retain their own first-use boundary.
  late.bufferPatchReads[0].generatedAddresses = true;
  cmd.m_BufferPatchReadIndexPrepared = false;
  REQUIRE(cmd.GetBufferPatchRangeConsumer({{buffer, 1024}}, {{buffer, 0, 4}}, first));
  CHECK(first == 46);
  REQUIRE(cmd.GetBufferPatchRangeConsumer({{buffer, 1024}}, {{buffer, 256, 4}}, first));
  CHECK(first == 14);
  late.bufferPatchReads[0].generatedAddresses = false;
  cmd.m_BufferPatchReadIndexPrepared = false;
  // Invalid extents, unresolved commands, secondary executions and loading changes lose proof.
  CHECK_FALSE(cmd.GetBufferPatchRangeConsumer({{buffer, 600}}, {{buffer, 512, 4}}, first));
  late.bufferPatchReadsComplete = false;
  CHECK_FALSE(cmd.GetBufferPatchRangeConsumer({{buffer, 1024}}, {{buffer, 512, 4}}, first));
  late.bufferPatchReadsComplete = true;
  cmd.m_BufferPatchReadIndexPrepared = false;
  cmd.m_Partial[D3D12CommandData::Secondary].cmdListExecs[secondList] = {40};
  CHECK_FALSE(cmd.GetBufferPatchRangeConsumer({{buffer, 1024}}, {{buffer, 512, 4}}, first));
  cmd.m_Partial[D3D12CommandData::Secondary].cmdListExecs.clear();
  cmd.m_BufferPatchReadIndexPrepared = false;
  // Ordinary primary Reset creates these empty map entries while checking rerecord ranges.
  // They must not be confused with a submitted secondary list, which was rejected above.
  cmd.m_Partial[D3D12CommandData::Secondary].cmdListExecs[firstList];
  cmd.m_Partial[D3D12CommandData::Secondary].cmdListExecs[secondList];
  REQUIRE(cmd.GetBufferPatchRangeConsumer({{buffer, 1024}}, {{buffer, 512, 4}}, first));
  CHECK(first == 46);
  cmd.m_BufferPatchReadIndexPrepared = false;
  cmd.m_Partial[D3D12CommandData::Primary].cmdListExecs[secondList] = {5};
  REQUIRE(cmd.GetBufferPatchRangeConsumer({{buffer, 1024}}, {{buffer, 512, 4}}, first));
  CHECK(first == 11);
}
#endif
