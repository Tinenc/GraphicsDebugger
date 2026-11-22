// Opt-in, read-only ownership accounting. Never populate lazy structured arrays, read GPU data,
// or run in interactive measurements. Address ranges are reconciled against OS commit by the
// owned-process diagnostic harness; logical resource sizes are not CPU residency estimates.
struct ReplayStructuredMemoryAudit
{
  uint64_t objects = 0, objectBytes = 0, childCapacityBytes = 0, lazyBytes = 0, stringBytes = 0;
  rdcarray<rdcpair<uintptr_t, uintptr_t>> pages;
  void RecordPages(const void *pointer, uint64_t bytes)
  {
    const uintptr_t address = uintptr_t(pointer);
    if(!address || !bytes || address > UINTPTR_MAX - 4095 || bytes > UINTPTR_MAX - address - 4095)
      return;
    pages.push_back({address & ~uintptr_t(4095), (address + bytes + 4095) & ~uintptr_t(4095)});
  }
  rdcstr PageAudit()
  {
    // These are pages touched by known objects, not exclusive byte ownership: allocator
    // headers, fragmentation and unrelated small allocations may share their boundary pages.
    std::sort(pages.begin(), pages.end());
    rdcstr lines;
    uintptr_t begin = 0, end = 0;
    const auto emit = [&]() {
      if(end > begin)
        lines += StringFormat::Fmt(
            "api=D3D12Memory kind=structuredStoragePages id=ResourceId::0 bytes=%llu address=%p\n",
            (unsigned long long)(end - begin), (void *)begin);
    };
    for(const auto &page : pages)
    {
      if(page.first > end)
      {
        emit();
        begin = page.first;
      }
      end = RDCMAX(end, page.second);
    }
    emit();
    return lines;
  }
  void Walk(const SDObject *object)
  {
    if(!object)
      return;
    objects++;
    objectBytes += sizeof(SDObject);
    childCapacityBytes += object->data.children.capacity() * sizeof(SDObject *);
    stringBytes += object->name.size() + object->type.name.size() + object->data.str.size();
    RecordPages(object, sizeof(SDObject));
    RecordPages(object->data.children.data(), object->data.children.capacity() * sizeof(SDObject *));
    RecordPages(object->name.c_str(), object->name.size() + 1);
    RecordPages(object->type.name.c_str(), object->type.name.size() + 1);
    RecordPages(object->data.str.c_str(), object->data.str.size() + 1);
    if(object->m_Lazy)
    {
      lazyBytes += sizeof(LazyArrayData) + object->m_Lazy->elemSize * object->data.children.size();
      RecordPages(object->m_Lazy, sizeof(LazyArrayData));
      RecordPages(object->m_Lazy->data, object->m_Lazy->elemSize * object->data.children.size());
    }
    for(const SDObject *child : object->data.children)
      Walk(child);
  }
};

rdcstr WrappedID3D12Device::GetReplayUploadMemoryAudit()
{
  rdcstr lines;
  for(const auto &entry : m_UploadBuffers)
  {
    ID3D12Resource *resource = entry.second;
    if(!resource)
      continue;
    const D3D12_RESOURCE_DESC desc = resource->GetDesc();
    void *mapped = NULL;
    const D3D12_RANGE noAccess = {0, 0};
    if(SUCCEEDED(Unwrap(resource)->Map(0, &noAccess, &mapped)))
    {
      if(mapped)
        lines += StringFormat::Fmt(
            "api=D3D12Memory kind=fixedWriteUploadMapping id=%s bytes=%llu address=%p\n",
            ToStr(GetResID(resource)).c_str(), (unsigned long long)desc.Width, mapped);
      Unwrap(resource)->Unmap(0, &noAccess);
    }
  }
  return lines;
}

static void AuditD3D12Memory(WrappedID3D12Device *device)
{
  if(Process::GetEnvVariable("RENDERDOC_D3D12_MEMORY_AUDIT") != "1")
    return;
  rdcstr lines;
  auto record = [&](const char *kind, ResourceId id, uint64_t bytes, const void *address) {
    lines += StringFormat::Fmt("api=D3D12Memory kind=%s id=%s bytes=%llu address=%p\n", kind,
                               ToStr(id).c_str(), (unsigned long long)bytes, address);
  };
  D3D12ResourceManager *manager = device->GetResourceManager();
  lines += device->GetReplayUploadMemoryAudit();
  if(const StreamReader *reader = device->GetQueue()->GetFrameReaderForMemoryAudit())
    record("retainedFrameStream", ResourceId(), reader->GetBufferSizeForMemoryAudit(),
           reader->GetBufferDataForMemoryAudit());
  for(ID3D12Resource *buffer : device->GetQueue()->GetCommandData()->m_IndirectBuffers)
  {
    if(!buffer)
      continue;
    const D3D12_RANGE noAccess = {0, 0};
    void *mapped = NULL;
    if(SUCCEEDED(Unwrap(buffer)->Map(0, &noAccess, &mapped)))
    {
      if(mapped)
        record("indirectArgumentReadback", GetResID(buffer), buffer->GetDesc().Width, mapped);
      Unwrap(buffer)->Unmap(0, &noAccess);
    }
  }
  for(ResourceId id : manager->GetInitialContentResourceIDs())
  {
    const D3D12InitialContents initial = manager->GetInitialContents(id);
    if(initial.srcData)
      record("initialCPUBytes", id, initial.dataSize, initial.srcData);
    if(initial.descriptors)
      record("initialDescriptorCPU", id, uint64_t(initial.numDescriptors) * sizeof(D3D12Descriptor),
             initial.descriptors);
    if(initial.resourceType == Resource_DescriptorHeap)
    {
      auto *live = (WrappedID3D12DescriptorHeap *)manager->GetResource(id);
      if(live)
        record("liveDescriptorMirror", id,
               uint64_t(live->GetNumDescriptors()) * sizeof(D3D12Descriptor), live->GetDescriptors());
      auto *saved = (WrappedID3D12DescriptorHeap *)initial.resource;
      if(saved)
        record("initialDescriptorMirror", id,
               uint64_t(saved->GetNumDescriptors()) * sizeof(D3D12Descriptor),
               saved->GetDescriptors());
    }
    if(initial.resource &&
       (initial.resourceType == Resource_Resource || initial.resourceType == Resource_Heap) &&
       (initial.tag == D3D12InitialContents::Copy || initial.tag == D3D12InitialContents::ForceCopy))
    {
      ID3D12Resource *source = Unwrap((ID3D12Resource *)initial.resource);
      const D3D12_RESOURCE_DESC desc = source->GetDesc();
      D3D12_HEAP_PROPERTIES heap = {};
      if(desc.Dimension == D3D12_RESOURCE_DIMENSION_BUFFER &&
         SUCCEEDED(source->GetHeapProperties(&heap, NULL)))
      {
        void *mapped = NULL;
        const D3D12_RANGE none = {0, 0};
        if(heap.Type == D3D12_HEAP_TYPE_UPLOAD && SUCCEEDED(source->Map(0, &none, &mapped)))
        {
          record("initialUploadMapping", id, desc.Width, mapped);
          auto captured = device->GetResourceList().find(id);
          if(captured != device->GetResourceList().end())
          {
            const D3D12_RESOURCE_DESC live = captured->second->GetReal()->GetDesc();
            const bool immutableCandidate =
                device->IsReadOnlyResource(id) && live.Dimension != D3D12_RESOURCE_DIMENSION_BUFFER &&
                !captured->second->GetHeap() && !initial.sparseBinds && !initial.sparseTable &&
                !(live.Flags & (D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS |
                                D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET |
                                D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL));
            lines += StringFormat::Fmt(
                "api=D3D12MemoryClass id=%s sourceBytes=%llu dimension=%u flags=%u "
                "readOnly=%u placed=%u immutableTextureCandidate=%u\n",
                ToStr(id).c_str(), (unsigned long long)desc.Width, (uint32_t)live.Dimension,
                (uint32_t)live.Flags, device->IsReadOnlyResource(id) ? 1U : 0U,
                captured->second->GetHeap() ? 1U : 0U, immutableCandidate ? 1U : 0U);
          }
          source->Unmap(0, &none);
        }
      }
    }
  }
  std::set<WrappedID3D12Heap *> observedHeaps;
  for(const auto &entry : device->GetResourceList())
  {
    WrappedID3D12Heap *placed = entry.second->GetHeap();
    if(placed && observedHeaps.insert(placed).second)
    {
      const D3D12_HEAP_DESC desc = placed->GetDesc();
      lines += StringFormat::Fmt(
          "api=D3D12MemoryHeap id=%s bytes=%llu type=%u flags=%u creationNode=%u visibleNode=%u\n",
          ToStr(GetResID(placed)).c_str(), (unsigned long long)desc.SizeInBytes,
          uint32_t(desc.Properties.Type), uint32_t(desc.Flags), desc.Properties.CreationNodeMask,
          desc.Properties.VisibleNodeMask);
    }
    ID3D12Resource *resource = entry.second->GetReal();
    const D3D12_RESOURCE_DESC desc = resource->GetDesc();
    D3D12_HEAP_PROPERTIES heap = {};
    if(FAILED(resource->GetHeapProperties(&heap, NULL)))
      continue;
    const auto allocation = device->GetReal()->GetResourceAllocationInfo(0, 1, &desc);
    lines += StringFormat::Fmt(
        "api=D3D12MemoryResource id=%s heap=%u dimension=%u width=%llu height=%u "
        "depthOrArray=%u mips=%u flags=%u format=%u allocationBytes=%llu placedHeap=%s "
        "heapOffset=%llu\n",
        ToStr(entry.first).c_str(), uint32_t(heap.Type), uint32_t(desc.Dimension),
        (unsigned long long)desc.Width, desc.Height, uint32_t(desc.DepthOrArraySize),
        uint32_t(desc.MipLevels), uint32_t(desc.Flags), uint32_t(desc.Format),
        (unsigned long long)allocation.SizeInBytes, ToStr(GetResID(entry.second->GetHeap())).c_str(),
        (unsigned long long)entry.second->GetHeapOffset());
    if(desc.Dimension != D3D12_RESOURCE_DIMENSION_BUFFER)
      continue;
    if(heap.Type == D3D12_HEAP_TYPE_UPLOAD || heap.Type == D3D12_HEAP_TYPE_READBACK ||
       (heap.Type == D3D12_HEAP_TYPE_CUSTOM && heap.MemoryPoolPreference == D3D12_MEMORY_POOL_L0))
    {
      void *mapped = NULL;
      const D3D12_RANGE none = {0, 0};
      if(SUCCEEDED(resource->Map(0, &none, &mapped)))
      {
        record(heap.Type == D3D12_HEAP_TYPE_READBACK ? "liveReadbackMapping" : "liveCPUBufferMapping",
               entry.first, desc.Width, mapped);
        resource->Unmap(0, &none);
      }
    }
  }
  const SDFile &file = *device->GetStructuredFile();
  ReplayStructuredMemoryAudit structured;
  for(const SDChunk *chunk : file.chunks)
    structured.Walk(chunk);
  lines += structured.PageAudit();
  uint64_t bufferBytes = 0, bufferCapacity = 0;
  for(const bytebuf *buffer : file.buffers)
    if(buffer)
    {
      bufferBytes += buffer->size();
      bufferCapacity += buffer->capacity();
      record("structuredByteBuffer", ResourceId(), buffer->capacity(), buffer->data());
    }
  lines += StringFormat::Fmt(
      "api=D3D12Memory kind=structuredSummary chunks=%zu objects=%llu objectBytes=%llu "
      "childCapacityBytes=%llu lazyBytes=%llu logicalStringBytes=%llu buffers=%zu "
      "bufferBytes=%llu bufferCapacityBytes=%llu\n",
      file.chunks.size(), (unsigned long long)structured.objects,
      (unsigned long long)structured.objectBytes, (unsigned long long)structured.childCapacityBytes,
      (unsigned long long)structured.lazyBytes, (unsigned long long)structured.stringBytes,
      file.buffers.size(), (unsigned long long)bufferBytes, (unsigned long long)bufferCapacity);
  AppendReplayTimingLog(lines);
  AppendReplayTimingLog(WrappedID3D12Shader::GetMemoryAudit());
}
