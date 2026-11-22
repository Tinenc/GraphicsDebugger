/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2016-2026 Baldur Karlsson
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

#include <algorithm>
#include "core/settings.h"
#include "driver/dxgi/dxgi_common.h"
#include "driver/ihv/amd/official/DXExt/AmdExtD3DCommandListMarkerApi.h"
#include "driver/shaders/dxbc/dxbc_bytecode.h"
#include "driver/shaders/dxil/dxil_bytecode.h"
#include "driver/shaders/dxil/dxil_common.h"
#include "d3d12_command_list.h"
#include "d3d12_replay_clear.h"
#include "d3d12_command_queue.h"
#include "replay/replay_timing.h"
#include "replay/resource_usage.h"

RDOC_EXTERN_CONFIG(bool, D3D12_Debug_RT_Auditing);

WRAPPED_POOL_INST(WrappedID3D12CommandQueue);
WRAPPED_POOL_INST(WrappedID3D12GraphicsCommandList);

template <>
ID3D12GraphicsCommandList *Unwrap(ID3D12GraphicsCommandList *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

ID3D12GraphicsCommandList *Unwrap(ID3D12GraphicsCommandList1 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

ID3D12GraphicsCommandList *Unwrap(ID3D12GraphicsCommandList2 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

ID3D12GraphicsCommandList *Unwrap(ID3D12GraphicsCommandList3 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

ID3D12GraphicsCommandList *Unwrap(ID3D12GraphicsCommandList4 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

ID3D12GraphicsCommandList *Unwrap(ID3D12GraphicsCommandList5 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

ID3D12GraphicsCommandList *Unwrap(ID3D12GraphicsCommandList6 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

ID3D12GraphicsCommandList *Unwrap(ID3D12GraphicsCommandList7 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

ID3D12GraphicsCommandList *Unwrap(ID3D12GraphicsCommandList8 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

ID3D12GraphicsCommandList *Unwrap(ID3D12GraphicsCommandList9 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

ID3D12GraphicsCommandList *Unwrap(ID3D12GraphicsCommandList10 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

ID3D12GraphicsCommandList1 *Unwrap1(ID3D12GraphicsCommandList1 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal1();
}

ID3D12GraphicsCommandList2 *Unwrap2(ID3D12GraphicsCommandList2 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal2();
}

ID3D12GraphicsCommandList3 *Unwrap3(ID3D12GraphicsCommandList3 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal3();
}

ID3D12GraphicsCommandList4 *Unwrap4(ID3D12GraphicsCommandList4 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal4();
}

ID3D12GraphicsCommandList5 *Unwrap5(ID3D12GraphicsCommandList5 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal5();
}

ID3D12GraphicsCommandList6 *Unwrap6(ID3D12GraphicsCommandList6 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal6();
}

ID3D12GraphicsCommandList7 *Unwrap7(ID3D12GraphicsCommandList7 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal7();
}

ID3D12GraphicsCommandList8 *Unwrap8(ID3D12GraphicsCommandList8 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal8();
}

ID3D12GraphicsCommandList9 *Unwrap9(ID3D12GraphicsCommandList9 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal9();
}

ID3D12GraphicsCommandList10 *Unwrap10(ID3D12GraphicsCommandList10 *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal10();
}

template <>
ID3D12CommandList *Unwrap(ID3D12CommandList *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetReal();
}

template <>
ResourceId GetResID(ID3D12GraphicsCommandList *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
ResourceId GetResID(ID3D12GraphicsCommandList1 *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
ResourceId GetResID(ID3D12GraphicsCommandList2 *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
ResourceId GetResID(ID3D12GraphicsCommandList3 *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
ResourceId GetResID(ID3D12GraphicsCommandList4 *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
ResourceId GetResID(ID3D12GraphicsCommandList5 *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
ResourceId GetResID(ID3D12GraphicsCommandList6 *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
ResourceId GetResID(ID3D12GraphicsCommandList7 *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
ResourceId GetResID(ID3D12GraphicsCommandList8 *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
ResourceId GetResID(ID3D12GraphicsCommandList9 *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
ResourceId GetResID(ID3D12GraphicsCommandList10 *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
ResourceId GetResID(ID3D12CommandList *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceID();
}

template <>
D3D12ResourceRecord *GetRecord(ID3D12GraphicsCommandList *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceRecord();
}

template <>
D3D12ResourceRecord *GetRecord(ID3D12CommandList *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12GraphicsCommandList *)obj)->GetResourceRecord();
}

template <>
ID3D12CommandQueue *Unwrap(ID3D12CommandQueue *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12CommandQueue *)obj)->GetReal();
}

template <>
ResourceId GetResID(ID3D12CommandQueue *obj)
{
  if(obj == NULL)
    return ResourceId();

  return ((WrappedID3D12CommandQueue *)obj)->GetResourceID();
}

template <>
D3D12ResourceRecord *GetRecord(ID3D12CommandQueue *obj)
{
  if(obj == NULL)
    return NULL;

  return ((WrappedID3D12CommandQueue *)obj)->GetResourceRecord();
}

WrappedID3D12GraphicsCommandList *GetWrapped(ID3D12GraphicsCommandList1 *obj)
{
  return ((WrappedID3D12GraphicsCommandList *)obj);
}

WrappedID3D12GraphicsCommandList *GetWrapped(ID3D12GraphicsCommandList2 *obj)
{
  return ((WrappedID3D12GraphicsCommandList *)obj);
}

WrappedID3D12GraphicsCommandList *GetWrapped(ID3D12GraphicsCommandList3 *obj)
{
  return ((WrappedID3D12GraphicsCommandList *)obj);
}

WrappedID3D12GraphicsCommandList *GetWrapped(ID3D12GraphicsCommandList4 *obj)
{
  return ((WrappedID3D12GraphicsCommandList *)obj);
}

WrappedID3D12GraphicsCommandList *GetWrapped(ID3D12GraphicsCommandList5 *obj)
{
  return ((WrappedID3D12GraphicsCommandList *)obj);
}

WrappedID3D12GraphicsCommandList *GetWrapped(ID3D12GraphicsCommandList6 *obj)
{
  return ((WrappedID3D12GraphicsCommandList *)obj);
}

WrappedID3D12GraphicsCommandList *GetWrapped(ID3D12GraphicsCommandList7 *obj)
{
  return ((WrappedID3D12GraphicsCommandList *)obj);
}

WrappedID3D12GraphicsCommandList *GetWrapped(ID3D12GraphicsCommandList8 *obj)
{
  return ((WrappedID3D12GraphicsCommandList *)obj);
}

WrappedID3D12GraphicsCommandList *GetWrapped(ID3D12GraphicsCommandList9 *obj)
{
  return ((WrappedID3D12GraphicsCommandList *)obj);
}

WrappedID3D12GraphicsCommandList *GetWrapped(ID3D12GraphicsCommandList10 *obj)
{
  return ((WrappedID3D12GraphicsCommandList *)obj);
}

ULONG STDMETHODCALLTYPE WrappedID3D12DebugCommandQueue::AddRef()
{
  if(m_pQueue)
    m_pQueue->AddRef();
  return 1;
}

ULONG STDMETHODCALLTYPE WrappedID3D12DebugCommandQueue::Release()
{
  if(m_pQueue)
    m_pQueue->Release();
  return 1;
}

ULONG STDMETHODCALLTYPE WrappedID3D12DebugCommandList::AddRef()
{
  if(m_pList)
    m_pList->AddRef();
  return 1;
}

ULONG STDMETHODCALLTYPE WrappedID3D12DebugCommandList::Release()
{
  if(m_pList)
    m_pList->Release();
  return 1;
}

HRESULT STDMETHODCALLTYPE WrappedID3D12CompatibilityQueue::QueryInterface(REFIID riid,
                                                                          void **ppvObject)
{
  return m_pQueue.QueryInterface(riid, ppvObject);
}

ULONG STDMETHODCALLTYPE WrappedID3D12CompatibilityQueue::AddRef()
{
  return m_pQueue.AddRef();
}

ULONG STDMETHODCALLTYPE WrappedID3D12CompatibilityQueue::Release()
{
  return m_pQueue.Release();
}

HRESULT STDMETHODCALLTYPE WrappedID3D12CompatibilityQueue::AcquireKeyedMutex(
    _In_ ID3D12Object *pHeapOrResourceWithKeyedMutex, UINT64 Key, DWORD dwTimeout,
    _Reserved_ void *pReserved, _In_range_(0, 0) UINT Reserved)
{
  return m_pReal->AcquireKeyedMutex(Unwrap(pHeapOrResourceWithKeyedMutex), Key, dwTimeout,
                                    pReserved, Reserved);
}

HRESULT STDMETHODCALLTYPE WrappedID3D12CompatibilityQueue::ReleaseKeyedMutex(
    _In_ ID3D12Object *pHeapOrResourceWithKeyedMutex, UINT64 Key, _Reserved_ void *pReserved,
    _In_range_(0, 0) UINT Reserved)
{
  return m_pReal->ReleaseKeyedMutex(Unwrap(pHeapOrResourceWithKeyedMutex), Key, pReserved, Reserved);
}

HRESULT STDMETHODCALLTYPE WrappedDownlevelQueue::QueryInterface(REFIID riid, void **ppvObject)
{
  return m_pQueue.QueryInterface(riid, ppvObject);
}

ULONG STDMETHODCALLTYPE WrappedDownlevelQueue::AddRef()
{
  return m_pQueue.AddRef();
}

ULONG STDMETHODCALLTYPE WrappedDownlevelQueue::Release()
{
  return m_pQueue.Release();
}

HRESULT STDMETHODCALLTYPE WrappedDownlevelQueue::Present(ID3D12GraphicsCommandList *pOpenCommandList,
                                                         ID3D12Resource *pSourceTex2D, HWND hWindow,
                                                         D3D12_DOWNLEVEL_PRESENT_FLAGS Flags)
{
  return m_pQueue.Present(pOpenCommandList, pSourceTex2D, hWindow, Flags);
}

WrappedID3D12CommandQueue::WrappedID3D12CommandQueue(ResourceId id, ID3D12CommandQueue *real,
                                                     WrappedID3D12Device *device, CaptureState &state)
    : RefCounter12(real),
      m_pDevice(device),
      m_State(state),
      m_WrappedDownlevel(*this),
      m_WrappedCompat(*this),
      m_SharingContract(*m_pDevice)
{
  RenderDoc::Inst().RegisterMemoryRegion(this, sizeof(WrappedID3D12CommandQueue));

  m_WrappedDebug.m_pQueue = this;
  m_pDownlevel = NULL;
  m_pReal1 = NULL;
  if(m_pReal)
  {
    m_pReal->QueryInterface(__uuidof(ID3D12DebugCommandQueue), (void **)&m_WrappedDebug.m_pReal);
    m_pReal->QueryInterface(__uuidof(ID3D12DebugCommandQueue1), (void **)&m_WrappedDebug.m_pReal1);
    m_pReal->QueryInterface(__uuidof(ID3D12CommandQueueDownlevel), (void **)&m_pDownlevel);
    m_pReal->QueryInterface(__uuidof(ID3D12CommandQueue1), (void **)&m_pReal1);
    m_pReal->QueryInterface(__uuidof(ID3D12CompatibilityQueue), (void **)&m_WrappedCompat.m_pReal);
    m_pReal->QueryInterface(__uuidof(ID3D12SharingContract), (void **)&m_SharingContract.m_pReal);
  }

  if(RenderDoc::Inst().IsReplayApp())
  {
    m_ReplayList = new WrappedID3D12GraphicsCommandList(ResourceId(), NULL, m_pDevice, state);

    m_ReplayList->SetCommandData(&m_Cmd);
  }

  if(id == ResourceId())
    id = ResourceIDGen::GetNewUniqueID();
  m_ResourceID = id;

  m_QueueRecord = NULL;
  m_CreationRecord = NULL;

  m_Cmd.m_pDevice = m_pDevice;

  if(!RenderDoc::Inst().IsReplayApp())
  {
    m_QueueRecord = m_pDevice->GetResourceManager()->AddResourceRecord(m_ResourceID);
    m_QueueRecord->type = Resource_CommandQueue;
    m_QueueRecord->DataInSerialiser = false;
    m_QueueRecord->InternalResource = true;
    m_QueueRecord->Length = 0;

    // a bit of a hack, we make a parallel resource record with the same lifetime as the command
    // queue. It will hold onto our create chunk and not get thrown away as we clear and re-fill
    // submissions into the queue record itself. We'll pull it into the capture by marking the
    // queues as referenced.
    m_CreationRecord =
        m_pDevice->GetResourceManager()->AddResourceRecord(ResourceIDGen::GetNewUniqueID());
    m_CreationRecord->type = Resource_CommandQueue;
    m_CreationRecord->InternalResource = true;
  }

  m_pDevice->GetResourceManager()->AddResource(GetResourceID(), this);

  m_pDevice->SoftRef();
}

WrappedID3D12CommandQueue::~WrappedID3D12CommandQueue()
{
  SAFE_DELETE(m_FrameReader);

  // The prepared allocators are deliberately never reset while their reusable lists are alive.
  // Queue teardown happens after replay work has been drained by device shutdown.
  m_Cmd.ClearPreparedReplayCache();
  m_Cmd.ClearFullReplayCompanions();

  SAFE_RELEASE(m_CallbackFence);

  for(SDObject *o : m_Cmd.m_EventAnnotations)
    delete o;

  delete m_Cmd.m_RootAnnotation;

  if(m_CreationRecord)
    m_CreationRecord->Delete(m_pDevice->GetResourceManager());

  if(m_QueueRecord)
    m_QueueRecord->Delete(m_pDevice->GetResourceManager());
  m_pDevice->GetResourceManager()->ReleaseResource(GetResourceID());
  m_pDevice->RemoveQueue(this);

  SAFE_RELEASE(m_pDownlevel);
  SAFE_RELEASE(m_pReal1);

  SAFE_RELEASE(m_WrappedCompat.m_pReal);
  SAFE_RELEASE(m_WrappedDebug.m_pReal);
  SAFE_RELEASE(m_WrappedDebug.m_pReal1);
  SAFE_RELEASE(m_SharingContract.m_pReal);
  SAFE_RELEASE(m_pReal);
}

HRESULT STDMETHODCALLTYPE WrappedID3D12CommandQueue::QueryInterface(REFIID riid, void **ppvObject)
{
  if(riid == __uuidof(IUnknown))
  {
    *ppvObject = (IUnknown *)(ID3D12CommandQueue *)this;
    AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12CommandQueue))
  {
    *ppvObject = (ID3D12CommandQueue *)this;
    AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12DebugCommandQueue))
  {
    if(m_WrappedDebug.m_pReal)
    {
      AddRef();
      *ppvObject = (ID3D12DebugCommandQueue *)&m_WrappedDebug;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12DebugCommandQueue1))
  {
    if(m_WrappedDebug.m_pReal1)
    {
      AddRef();
      *ppvObject = (ID3D12DebugCommandQueue1 *)&m_WrappedDebug;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12CompatibilityQueue))
  {
    if(m_WrappedCompat.m_pReal)
    {
      AddRef();
      *ppvObject = (ID3D12CompatibilityQueue *)&m_WrappedCompat;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12SharingContract))
  {
    *ppvObject = (ID3D12SharingContract *)&m_SharingContract;
    AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12Pageable))
  {
    *ppvObject = (ID3D12Pageable *)this;
    AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12DeviceChild))
  {
    *ppvObject = (ID3D12DeviceChild *)this;
    AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12Object))
  {
    *ppvObject = (ID3D12DeviceChild *)this;
    AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12CommandQueueDownlevel))
  {
    if(m_pDownlevel)
    {
      *ppvObject = &m_WrappedDownlevel;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }

  return RefCounter12::QueryInterface("ID3D12CommandQueue", riid, ppvObject);
}

void WrappedID3D12CommandQueue::CheckAndFreeRayDispatches()
{
  UINT64 signalled = 0;
  if(m_CallbackFence)
    signalled = m_CallbackFence->GetCompletedValue();

  for(PatchedRayDispatch::Resources &ray : m_RayDispatchesPending)
  {
    if(signalled >= ray.fenceValue)
    {
      GetResourceManager()->GetRTManager()->AddDispatchTimer(ray.query);
      ray.Release();
    }
  }

  m_RayDispatchesPending.removeIf(
      [](const PatchedRayDispatch::Resources &ray) { return ray.lookupBuffer == NULL; });
}

void WrappedID3D12CommandQueue::ClearAfterCapture()
{
  // delete cmd buffers now - had to keep them alive until after serialiser flush.
  for(size_t i = 0; i < m_CmdListRecords.size(); i++)
    m_CmdListRecords[i]->Delete(GetResourceManager());

  for(size_t i = 0; i < m_CmdListAllocators.size(); i++)
    m_CmdListAllocators[i]->Delete(GetResourceManager());

  m_CmdListRecords.clear();
  m_CmdListAllocators.clear();

  m_QueueRecord->DeleteChunks();
}

WriteSerialiser &WrappedID3D12CommandQueue::GetThreadSerialiser()
{
  return m_pDevice->GetThreadSerialiser();
}

rdcstr WrappedID3D12CommandQueue::GetChunkName(uint32_t idx)
{
  if((SystemChunk)idx < SystemChunk::FirstDriverChunk)
    return ToStr((SystemChunk)idx);

  return ToStr((D3D12Chunk)idx);
}

const APIEvent &WrappedID3D12CommandQueue::GetEvent(uint32_t eventId)
{
  // start at where the requested eventId would be
  size_t idx = eventId;

  // find the next valid event (some may be skipped)
  while(idx < m_Cmd.m_Events.size() - 1 && m_Cmd.m_Events[idx].eventId == 0)
    idx++;

  return m_Cmd.m_Events[RDCMIN(idx, m_Cmd.m_Events.size() - 1)];
}

bool WrappedID3D12CommandQueue::ProcessChunk(ReadSerialiser &ser, D3D12Chunk chunk)
{
  // Sparse mapping handlers validate the captured source/heap relation after deserialisation;
  // an incomplete alias closure still invalidates the complete dirty proof there.
  if(IsLoading(m_State))
  {
    m_Cmd.m_AddedEventNode = false;
    m_Cmd.m_LoadingEventNode = D3D12EventNode();
  }

  bool ret = false;

  switch(chunk)
  {
    case D3D12Chunk::Device_CreateConstantBufferView:
    case D3D12Chunk::Device_CreateShaderResourceView:
    case D3D12Chunk::Device_CreateUnorderedAccessView:
    case D3D12Chunk::Device_CreateRenderTargetView:
    case D3D12Chunk::Device_CreateDepthStencilView:
    case D3D12Chunk::Device_CreateSampler:
    case D3D12Chunk::Device_CreateSampler2:
    case D3D12Chunk::Device_TryCreateShaderResourceView:
    case D3D12Chunk::Device_TryCreateUnorderedAccessView:
    case D3D12Chunk::Device_TryCreateConstantBufferView:
    case D3D12Chunk::Device_TryCreateSampler2:
    case D3D12Chunk::Device_TryCreateRenderTargetView:
    case D3D12Chunk::Device_TryCreateDepthStencilView:
      ret = m_pDevice->Serialise_DynamicDescriptorWrite(ser, NULL);
      break;
    case D3D12Chunk::Device_CopyDescriptors:
    case D3D12Chunk::Device_CopyDescriptorsSimple:
      ret = m_pDevice->Serialise_DynamicDescriptorCopies(ser, rdcarray<DynamicDescriptorCopy>());
      break;

    case D3D12Chunk::Queue_ExecuteCommandLists:
      ret = Serialise_ExecuteCommandLists(ser, 0, NULL);
      break;
    case D3D12Chunk::Queue_Signal: ret = Serialise_Signal(ser, NULL, 0); break;
    case D3D12Chunk::Queue_Wait: ret = Serialise_Wait(ser, NULL, 0); break;
    case D3D12Chunk::Queue_UpdateTileMappings:
      ret = Serialise_UpdateTileMappings(ser, NULL, 0, NULL, NULL, NULL, 0, NULL, NULL, NULL,
                                         D3D12_TILE_MAPPING_FLAGS(0));
      break;
    case D3D12Chunk::Queue_CopyTileMappings:
      ret =
          Serialise_CopyTileMappings(ser, NULL, NULL, NULL, NULL, NULL, D3D12_TILE_MAPPING_FLAGS(0));
      break;
    case D3D12Chunk::Queue_BeginEvent: ret = Serialise_BeginEvent(ser, 0, NULL, 0); break;
    case D3D12Chunk::Queue_SetMarker: ret = Serialise_SetMarker(ser, 0, NULL, 0); break;
    case D3D12Chunk::Queue_EndEvent: ret = Serialise_EndEvent(ser); break;

    case D3D12Chunk::List_Close: ret = m_ReplayList->Serialise_Close(ser); break;
    case D3D12Chunk::List_Reset: ret = m_ReplayList->Serialise_Reset(ser, NULL, NULL); break;
    case D3D12Chunk::List_ResourceBarrier:
      ret = m_ReplayList->Serialise_ResourceBarrier(ser, 0, NULL);
      break;
    case D3D12Chunk::List_BeginQuery:
      ret = m_ReplayList->Serialise_BeginQuery(ser, NULL, D3D12_QUERY_TYPE(0), 0);
      break;
    case D3D12Chunk::List_EndQuery:
      ret = m_ReplayList->Serialise_EndQuery(ser, NULL, D3D12_QUERY_TYPE(0), 0);
      break;
    case D3D12Chunk::List_ResolveQueryData:
      ret = m_ReplayList->Serialise_ResolveQueryData(ser, NULL, D3D12_QUERY_TYPE(0), 0, 0, NULL, 0);
      break;
    case D3D12Chunk::List_SetPredication:
      ret = m_ReplayList->Serialise_SetPredication(ser, NULL, 0, D3D12_PREDICATION_OP(0));
      break;
    case D3D12Chunk::List_DrawIndexedInstanced:
      ret = m_ReplayList->Serialise_DrawIndexedInstanced(ser, 0, 0, 0, 0, 0);
      break;
    case D3D12Chunk::List_DrawInstanced:
      ret = m_ReplayList->Serialise_DrawInstanced(ser, 0, 0, 0, 0);
      break;
    case D3D12Chunk::List_Dispatch: ret = m_ReplayList->Serialise_Dispatch(ser, 0, 0, 0); break;
    case D3D12Chunk::List_ExecuteIndirect:
      ret = m_ReplayList->Serialise_ExecuteIndirect(ser, NULL, 0, NULL, 0, NULL, 0);
      break;
    case D3D12Chunk::List_ExecuteBundle:
      ret = m_ReplayList->Serialise_ExecuteBundle(ser, NULL);
      break;
    case D3D12Chunk::List_CopyBufferRegion:
      ret = m_ReplayList->Serialise_CopyBufferRegion(ser, NULL, 0, NULL, 0, 0);
      break;
    case D3D12Chunk::List_CopyTextureRegion:
      ret = m_ReplayList->Serialise_CopyTextureRegion(ser, NULL, 0, 0, 0, NULL, NULL);
      break;
    case D3D12Chunk::List_CopyResource:
      ret = m_ReplayList->Serialise_CopyResource(ser, NULL, NULL);
      break;
    case D3D12Chunk::List_ResolveSubresource:
      ret = m_ReplayList->Serialise_ResolveSubresource(ser, NULL, 0, NULL, 0, DXGI_FORMAT_UNKNOWN);
      break;
    case D3D12Chunk::List_ClearRenderTargetView:
      ret = m_ReplayList->Serialise_ClearRenderTargetView(ser, D3D12_CPU_DESCRIPTOR_HANDLE(),
                                                          (FLOAT *)NULL, 0, NULL);
      break;
    case D3D12Chunk::List_ClearDepthStencilView:
      ret = m_ReplayList->Serialise_ClearDepthStencilView(ser, D3D12_CPU_DESCRIPTOR_HANDLE(),
                                                          D3D12_CLEAR_FLAGS(0), 0.0f, 0, 0, NULL);
      break;
    case D3D12Chunk::List_ClearUnorderedAccessViewUint:
      ret = m_ReplayList->Serialise_ClearUnorderedAccessViewUint(
          ser, D3D12_GPU_DESCRIPTOR_HANDLE(), D3D12_CPU_DESCRIPTOR_HANDLE(), NULL, NULL, 0, NULL);
      break;
    case D3D12Chunk::List_ClearUnorderedAccessViewFloat:
      ret = m_ReplayList->Serialise_ClearUnorderedAccessViewFloat(
          ser, D3D12_GPU_DESCRIPTOR_HANDLE(), D3D12_CPU_DESCRIPTOR_HANDLE(), NULL, NULL, 0, NULL);
      break;
    case D3D12Chunk::List_DiscardResource:
      ret = m_ReplayList->Serialise_DiscardResource(ser, NULL, NULL);
      break;
    case D3D12Chunk::List_IASetPrimitiveTopology:
      ret = m_ReplayList->Serialise_IASetPrimitiveTopology(ser, D3D_PRIMITIVE_TOPOLOGY_UNDEFINED);
      break;
    case D3D12Chunk::List_IASetIndexBuffer:
      ret = m_ReplayList->Serialise_IASetIndexBuffer(ser, NULL);
      break;
    case D3D12Chunk::List_IASetVertexBuffers:
      ret = m_ReplayList->Serialise_IASetVertexBuffers(ser, 0, 0, NULL);
      break;
    case D3D12Chunk::List_SOSetTargets:
      ret = m_ReplayList->Serialise_SOSetTargets(ser, 0, 0, NULL);
      break;
    case D3D12Chunk::List_RSSetViewports:
      ret = m_ReplayList->Serialise_RSSetViewports(ser, 0, NULL);
      break;
    case D3D12Chunk::List_RSSetScissorRects:
      ret = m_ReplayList->Serialise_RSSetScissorRects(ser, 0, NULL);
      break;
    case D3D12Chunk::List_SetPipelineState:
      ret = m_ReplayList->Serialise_SetPipelineState(ser, NULL);
      break;
    case D3D12Chunk::List_SetDescriptorHeaps:
      ret = m_ReplayList->Serialise_SetDescriptorHeaps(ser, 0, NULL);
      break;
    case D3D12Chunk::List_OMSetRenderTargets:
      ret = m_ReplayList->Serialise_OMSetRenderTargets(ser, 0, NULL, FALSE, NULL);
      break;
    case D3D12Chunk::List_OMSetStencilRef:
      ret = m_ReplayList->Serialise_OMSetStencilRef(ser, 0);
      break;
    case D3D12Chunk::List_OMSetBlendFactor:
      ret = m_ReplayList->Serialise_OMSetBlendFactor(ser, NULL);
      break;
    case D3D12Chunk::List_SetGraphicsRootDescriptorTable:
      ret = m_ReplayList->Serialise_SetGraphicsRootDescriptorTable(ser, 0,
                                                                   D3D12_GPU_DESCRIPTOR_HANDLE());
      break;
    case D3D12Chunk::List_SetGraphicsRootSignature:
      ret = m_ReplayList->Serialise_SetGraphicsRootSignature(ser, NULL);
      break;
    case D3D12Chunk::List_SetGraphicsRoot32BitConstant:
      ret = m_ReplayList->Serialise_SetGraphicsRoot32BitConstant(ser, 0, 0, 0);
      break;
    case D3D12Chunk::List_SetGraphicsRoot32BitConstants:
      ret = m_ReplayList->Serialise_SetGraphicsRoot32BitConstants(ser, 0, 0, NULL, 0);
      break;
    case D3D12Chunk::List_SetGraphicsRootConstantBufferView:
      ret = m_ReplayList->Serialise_SetGraphicsRootConstantBufferView(ser, 0,
                                                                      D3D12_GPU_VIRTUAL_ADDRESS());
      break;
    case D3D12Chunk::List_SetGraphicsRootShaderResourceView:
      ret = m_ReplayList->Serialise_SetGraphicsRootShaderResourceView(ser, 0,
                                                                      D3D12_GPU_VIRTUAL_ADDRESS());
      break;
    case D3D12Chunk::List_SetGraphicsRootUnorderedAccessView:
      ret = m_ReplayList->Serialise_SetGraphicsRootUnorderedAccessView(ser, 0,
                                                                       D3D12_GPU_VIRTUAL_ADDRESS());
      break;
    case D3D12Chunk::List_SetComputeRootDescriptorTable:
      ret = m_ReplayList->Serialise_SetComputeRootDescriptorTable(ser, 0,
                                                                  D3D12_GPU_DESCRIPTOR_HANDLE());
      break;
    case D3D12Chunk::List_SetComputeRootSignature:
      ret = m_ReplayList->Serialise_SetComputeRootSignature(ser, NULL);
      break;
    case D3D12Chunk::List_SetComputeRoot32BitConstant:
      ret = m_ReplayList->Serialise_SetComputeRoot32BitConstant(ser, 0, 0, 0);
      break;
    case D3D12Chunk::List_SetComputeRoot32BitConstants:
      ret = m_ReplayList->Serialise_SetComputeRoot32BitConstants(ser, 0, 0, NULL, 0);
      break;
    case D3D12Chunk::List_SetComputeRootConstantBufferView:
      ret = m_ReplayList->Serialise_SetComputeRootConstantBufferView(ser, 0,
                                                                     D3D12_GPU_VIRTUAL_ADDRESS());
      break;
    case D3D12Chunk::List_SetComputeRootShaderResourceView:
      ret = m_ReplayList->Serialise_SetComputeRootShaderResourceView(ser, 0,
                                                                     D3D12_GPU_VIRTUAL_ADDRESS());
      break;
    case D3D12Chunk::List_SetComputeRootUnorderedAccessView:
      ret = m_ReplayList->Serialise_SetComputeRootUnorderedAccessView(ser, 0,
                                                                      D3D12_GPU_VIRTUAL_ADDRESS());
      break;
    case D3D12Chunk::List_CopyTiles:
      ret = m_ReplayList->Serialise_CopyTiles(ser, NULL, NULL, NULL, NULL, 0,
                                              D3D12_TILE_COPY_FLAGS(0));
      break;
    case D3D12Chunk::List_AtomicCopyBufferUINT:
      ret = m_ReplayList->Serialise_AtomicCopyBufferUINT(ser, NULL, 0, NULL, 0, 0, NULL, NULL);
      break;
    case D3D12Chunk::List_AtomicCopyBufferUINT64:
      ret = m_ReplayList->Serialise_AtomicCopyBufferUINT64(ser, NULL, 0, NULL, 0, 0, NULL, NULL);
      break;
    case D3D12Chunk::List_OMSetDepthBounds:
      ret = m_ReplayList->Serialise_OMSetDepthBounds(ser, 0.0f, 0.0f);
      break;
    case D3D12Chunk::List_ResolveSubresourceRegion:
      ret = m_ReplayList->Serialise_ResolveSubresourceRegion(
          ser, NULL, 0, 0, 0, NULL, 0, NULL, DXGI_FORMAT_UNKNOWN, D3D12_RESOLVE_MODE_DECOMPRESS);
      break;
    case D3D12Chunk::List_SetSamplePositions:
      ret = m_ReplayList->Serialise_SetSamplePositions(ser, 0, NULL, NULL);
      break;
    case D3D12Chunk::List_SetViewInstanceMask:
      ret = m_ReplayList->Serialise_SetViewInstanceMask(ser, 0);
      break;
    case D3D12Chunk::List_WriteBufferImmediate:
      ret = m_ReplayList->Serialise_WriteBufferImmediate(ser, 0, NULL, NULL);
      break;
    case D3D12Chunk::List_BeginRenderPass:
      ret = m_ReplayList->Serialise_BeginRenderPass(ser, 0, NULL, NULL, D3D12_RENDER_PASS_FLAG_NONE);
      break;
    case D3D12Chunk::List_EndRenderPass: ret = m_ReplayList->Serialise_EndRenderPass(ser); break;
    case D3D12Chunk::List_RSSetShadingRate:
      ret = m_ReplayList->Serialise_RSSetShadingRate(ser, D3D12_SHADING_RATE_1X1, NULL);
      break;
    case D3D12Chunk::List_RSSetShadingRateImage:
      ret = m_ReplayList->Serialise_RSSetShadingRateImage(ser, NULL);
      break;
    case D3D12Chunk::List_OMSetFrontAndBackStencilRef:
      ret = m_ReplayList->Serialise_OMSetFrontAndBackStencilRef(ser, 0, 0);
      break;
    case D3D12Chunk::List_RSSetDepthBias:
      ret = m_ReplayList->Serialise_RSSetDepthBias(ser, 0.0f, 0.0f, 0.0f);
      break;
    case D3D12Chunk::List_IASetIndexBufferStripCutValue:
      ret = m_ReplayList->Serialise_IASetIndexBufferStripCutValue(
          ser, D3D12_INDEX_BUFFER_STRIP_CUT_VALUE_DISABLED);
      break;
    case D3D12Chunk::List_Barrier: ret = m_ReplayList->Serialise_Barrier(ser, 0, NULL); break;
    case D3D12Chunk::List_DispatchMesh:
      ret = m_ReplayList->Serialise_DispatchMesh(ser, 0, 0, 0);
      break;

    case D3D12Chunk::PushMarker: ret = m_ReplayList->Serialise_BeginEvent(ser, 0, NULL, 0); break;
    case D3D12Chunk::PopMarker: ret = m_ReplayList->Serialise_EndEvent(ser); break;
    case D3D12Chunk::SetMarker: ret = m_ReplayList->Serialise_SetMarker(ser, 0, NULL, 0); break;

    case D3D12Chunk::CoherentMapWrite:
    case D3D12Chunk::Resource_Unmap:
      ret = m_pDevice->Serialise_MapDataWrite(ser, NULL, 0, NULL, D3D12_RANGE(), false);
      break;
    case D3D12Chunk::Resource_WriteToSubresource:
      ret = m_pDevice->Serialise_WriteToSubresource(ser, NULL, 0, NULL, NULL, 0, 0);
      break;
    case D3D12Chunk::List_IndirectSubCommand:
      // this is a fake chunk generated at runtime as part of indirect draws.
      // Just in case it gets exported and imported, completely ignore it.
      return true;

    case D3D12Chunk::Swapchain_Present: ret = m_pDevice->Serialise_Present(ser, NULL, 0, 0); break;

    case D3D12Chunk::List_ClearState: ret = m_ReplayList->Serialise_ClearState(ser, NULL); break;

    case D3D12Chunk::List_BuildRaytracingAccelerationStructure:
      ret = m_ReplayList->Serialise_BuildRaytracingAccelerationStructure(ser, NULL, 0, NULL);
      break;
    case D3D12Chunk::List_CopyRaytracingAccelerationStructure:
      ret = m_ReplayList->Serialise_CopyRaytracingAccelerationStructure(
          ser, D3D12_GPU_VIRTUAL_ADDRESS(), D3D12_GPU_VIRTUAL_ADDRESS(),
          D3D12_RAYTRACING_ACCELERATION_STRUCTURE_COPY_MODE_CLONE);
      break;
    case D3D12Chunk::List_EmitRaytracingAccelerationStructurePostbuildInfo:
      ret = m_ReplayList->Serialise_EmitRaytracingAccelerationStructurePostbuildInfo(ser, NULL, 0,
                                                                                     NULL);
      break;
    case D3D12Chunk::List_DispatchRays:
      ret = m_ReplayList->Serialise_DispatchRays(ser, NULL);
      break;
    case D3D12Chunk::List_SetPipelineState1:
      ret = m_ReplayList->Serialise_SetPipelineState1(ser, NULL);
      break;

    case D3D12Chunk::SetCommandAnnotation:
      ret = m_ReplayList->Serialise_SetCommandAnnotation(ser, rdcstr(), eRENDERDOC_AnnotationMax, 0,
                                                         RENDERDOC_AnnotationValue());
      break;
    case D3D12Chunk::SetQueueAnnotation:
      ret = Serialise_SetQueueAnnotation(ser, rdcstr(), eRENDERDOC_AnnotationMax, 0,
                                         RENDERDOC_AnnotationValue());
      break;

    // in order to get a warning if we miss a case, we explicitly handle the device creation chunks
    // here. If we actually encounter one it's an error (we shouldn't see these inside the captured
    // frame itself)
    case D3D12Chunk::Device_CreateCommandQueue:
    case D3D12Chunk::Device_CreateCommandAllocator:
    case D3D12Chunk::Device_CreateCommandList:
    case D3D12Chunk::Device_CreateGraphicsPipeline:
    case D3D12Chunk::Device_CreateComputePipeline:
    case D3D12Chunk::Device_CreateDescriptorHeap:
    case D3D12Chunk::Device_CreateRootSignature:
    case D3D12Chunk::Device_CreateCommandSignature:
    case D3D12Chunk::Device_CreateHeap:
    case D3D12Chunk::Device_CreateCommittedResource:
    case D3D12Chunk::Device_CreatePlacedResource:
    case D3D12Chunk::Device_CreateReservedResource:
    case D3D12Chunk::Device_CreateQueryHeap:
    case D3D12Chunk::Device_CreateFence:
    case D3D12Chunk::SetName:
    case D3D12Chunk::SetShaderDebugPath:
    case D3D12Chunk::CreateSwapBuffer:
    case D3D12Chunk::Device_CreatePipelineState:
    case D3D12Chunk::Device_CreateHeapFromAddress:
    case D3D12Chunk::Device_CreateHeapFromFileMapping:
    case D3D12Chunk::Device_OpenSharedHandle:
    case D3D12Chunk::Device_CreateCommandList1:
    case D3D12Chunk::Device_CreateCommittedResource1:
    case D3D12Chunk::Device_CreateHeap1:
    case D3D12Chunk::Device_ExternalDXGIResource:
    case D3D12Chunk::CompatDevice_CreateSharedResource:
    case D3D12Chunk::CompatDevice_CreateSharedHeap:
    case D3D12Chunk::SetShaderExtUAV:
    case D3D12Chunk::Device_CreateCommittedResource2:
    case D3D12Chunk::Device_CreatePlacedResource1:
    case D3D12Chunk::Device_CreateCommandQueue1:
    case D3D12Chunk::Device_CreateCommittedResource3:
    case D3D12Chunk::Device_CreatePlacedResource2:
    case D3D12Chunk::Device_CreateReservedResource1:
    case D3D12Chunk::Device_CreateReservedResource2:
    case D3D12Chunk::Device_CreateStateObject:
    case D3D12Chunk::Device_AddToStateObject:
    case D3D12Chunk::CreateAS:
    case D3D12Chunk::StateObject_SetPipelineStackSize:
    case D3D12Chunk::Device_CreateHeapFromAddress1:
    case D3D12Chunk::Device_CreateRootSignatureFromSubobjectInLibrary:
    case D3D12Chunk::List_SetProgram:
    case D3D12Chunk::List_DispatchGraph:
    case D3D12Chunk::Device_CreateQueryHeap1:
      RDCERR("Unexpected chunk while processing frame: %s", ToStr(chunk).c_str());
      return false;

    // no explicit default so that we have compiler warnings if a chunk isn't explicitly handled.
    case D3D12Chunk::Max: break;
  }

  {
    SystemChunk system = (SystemChunk)chunk;

    if(system == SystemChunk::CaptureEnd)
    {
      SERIALISE_ELEMENT_LOCAL(PresentedImage, ResourceId()).TypedAs("ID3D12Resource *"_lit);

      SERIALISE_CHECK_READ_ERRORS();

      if(PresentedImage != ResourceId())
        m_Cmd.m_LastPresentedImage = PresentedImage;

      if(IsLoading(m_State) && m_Cmd.m_LastChunk != D3D12Chunk::Swapchain_Present)
      {
        m_Cmd.AddEvent();

        ActionDescription action;
        action.customName = "End of Capture";
        action.flags |= ActionFlags::Present;

        action.copyDestination = m_Cmd.m_LastPresentedImage;

        m_Cmd.AddAction(action);
      }

      ret = true;
    }
    else if(!ret)
    {
      RDCERR("Chunk failed to serialise: %s", ToStr(chunk).c_str());
      return false;
    }
  }

  if(IsLoading(m_State))
  {
    if(chunk == D3D12Chunk::List_Reset || chunk == D3D12Chunk::List_Close)
    {
      // don't add these events - they will be handled when inserted in-line into queue submit
    }
    else if(chunk == D3D12Chunk::Queue_EndEvent)
    {
      // also ignore, this just pops the action stack
    }
    else if(chunk == D3D12Chunk::SetCommandAnnotation || chunk == D3D12Chunk::SetQueueAnnotation)
    {
    }
    else
    {
      if(!m_Cmd.m_AddedEventNode)
        m_Cmd.AddEvent();
    }
  }

  return ret;
}

static const char *PreparedReplayRejectReasonName(PreparedReplayRejectReason reason)
{
  switch(reason)
  {
    case PreparedReplayRejectReason::None: return "none";
    case PreparedReplayRejectReason::UnsupportedChunk: return "unsupportedChunk";
    case PreparedReplayRejectReason::InterleavedChunk: return "interleavedChunk";
    case PreparedReplayRejectReason::InterleavedReset: return "interleavedReset";
    case PreparedReplayRejectReason::APICommand: return "apiCommand";
    case PreparedReplayRejectReason::IncompleteRange: return "incompleteRange";
    case PreparedReplayRejectReason::NativeQueries: return "nativeQueries";
  }
  return "unknown";
}

RDResult WrappedID3D12CommandQueue::ReplayLog(CaptureState readType, uint32_t startEventID,
                                              uint32_t endEventID, bool partial,
                                              bool allowPreparedReplay, bool allowCheckpoints)
{
  ScopedReplayTimingSection setupTiming(ReplayTimingSection::ReplaySetup);
  m_State = readType;

  m_Cmd.m_PreparedReplayEnabled = false;
  m_Cmd.m_PreparedSkipEnd = 0;
  m_Cmd.m_PreparedCacheHits = 0;
  m_Cmd.m_PreparedCacheBuilds = 0;
  m_Cmd.m_PreparedSkippedChunks = 0;
  m_Cmd.m_PreparedSkippedBytes = 0;
  m_Cmd.m_PreparedAttempts = 0;
  m_Cmd.m_PreparedRejectedType = 0;
  m_Cmd.m_PreparedRejectedUnsafe = 0;
  m_Cmd.m_PreparedRejectedExisting = 0;
  m_Cmd.m_PreparedRejectedBudget = 0;
  m_Cmd.m_PreparedRejectSummary.clear();
  m_Cmd.m_PreparedTimingEnabled =
      IsActiveReplaying(m_State) && Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1";
  m_Cmd.m_ReplayWriteCutoffEnabled =
      Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_DIRTY_WRITE_CUTOFF") != "1";

  if(!partial)
  {
    // The device drained the previous replay before entering this non-partial recording, as
    // required for the existing rerecorded wrappers below. Never reset a submitted allocator.
    m_Cmd.ClearFullReplayCompanions();
    for(size_t i = 0; i < m_Cmd.m_RerecordCmdList.size(); i++)
      SAFE_RELEASE(m_Cmd.m_RerecordCmdList[i]);

    m_Cmd.m_RerecordCmdList.clear();
  }

  if(!m_FrameReader)
  {
    RETURN_ERROR_RESULT(ResultCode::InvalidParameter,
                        "Can't replay context capture without frame reader");
  }

  m_FrameReader->SetOffset(0);

  ReadSerialiser ser(m_FrameReader, Ownership::Nothing);
  if(IsReplayTimingActive())
  {
    ser.SetParameterReadCallback(&NotifyReplayTimingParametersDecoded);
    SetReplayTimingChunkNameLookup(&GetChunkName);
  }

  ser.SetStringDatabase(&m_StringDB);
  ser.SetUserData(GetResourceManager());
  ser.SetVersion(m_pDevice->GetCaptureVersion());

  if(IsLoading(m_State) || IsStructuredExporting(m_State))
  {
    ser.ConfigureStructuredExport(&GetChunkName, IsStructuredExporting(m_State),
                                  m_pDevice->GetTimeBase(), m_pDevice->GetTimeFrequency());

    ser.GetStructuredFile().Swap(*m_pDevice->GetStructuredFile());

    m_StructuredFile = &ser.GetStructuredFile();
  }
  else
  {
    m_StructuredFile = m_pDevice->GetStructuredFile();
  }

  m_Cmd.m_StructuredFile = m_StructuredFile;

  SystemChunk header = ser.ReadChunk<SystemChunk>();
  RDCASSERTEQUAL(header, SystemChunk::CaptureBegin);

  if(partial)
    ser.SkipCurrentChunk();
  else
    m_pDevice->Serialise_BeginCaptureFrame(ser);

  ser.EndChunk();

  if(IsLoading(m_State))
  {
    m_Cmd.m_ReplaySubmissionQueues.clear();
    m_Cmd.m_ReplayTypedRootChunks.clear();
    m_Cmd.m_ReplayTypedFrameBegin = m_Cmd.m_ReplayTypedFrameEnd = ser.GetReader()->GetOffset();
    m_Cmd.m_ReplayTypedRootComplete = true;
    m_pDevice->ApplyInitialContents();

    m_pDevice->ExecuteLists();
    m_pDevice->FlushLists();

    if(m_pDevice->HasFatalError())
      return m_pDevice->FatalErrorCheck();
  }

  if(IsActiveReplaying(m_State))
  {
    APIEvent ev = GetEvent(startEventID);
    m_Cmd.m_RootEventID = ev.eventId;

    // if not partial, we need to be sure to replay
    // past the command list records, so can't
    // skip to the file offset of the first event
    if(partial)
    {
      ser.GetReader()->SetOffset(ev.fileOffset);

      D3D12CommandData::ActionUse use(ev.fileOffset, 0);
      auto it = std::lower_bound(m_Cmd.m_ActionUses.begin(), m_Cmd.m_ActionUses.end(), use);

      if(it != m_Cmd.m_ActionUses.end())
      {
        BakedCmdListInfo &cmdInfo = m_Cmd.m_BakedCmdListInfo[it->cmdList];
        cmdInfo.curEventID = it->relativeEID;
      }
    }

    m_Cmd.m_FirstEventID = startEventID;
    m_Cmd.m_LastEventID = endEventID;

    // Only a normal full-frame prefix ending inside one primary command list can use prepared
    // nodes. This preserves the existing partial-list state reconstruction for the selected EID.
    bool targetInsideCommandList = false;
    for(auto it = m_Cmd.m_Partial[D3D12CommandData::Primary].cmdListExecs.begin();
        it != m_Cmd.m_Partial[D3D12CommandData::Primary].cmdListExecs.end(); ++it)
    {
      const uint32_t eventCount = m_Cmd.m_BakedCmdListInfo[it->first].eventCount;
      for(uint32_t baseEvent : it->second)
      {
        if(baseEvent <= endEventID && endEventID < baseEvent + eventCount)
        {
          targetInsideCommandList = true;
          break;
        }
      }
      if(targetInsideCommandList)
        break;
    }

    m_Cmd.m_PreparedReplayEnabled =
        allowPreparedReplay && !partial && startEventID == 1 && targetInsideCommandList &&
        m_Cmd.m_ActionCallback == NULL &&
        // Prepared hits are experimental until native command-list lifetime and query replay have
        // passed capture validation. Never enable this path implicitly for normal EID scrubbing.
        Process::GetEnvVariable("RENDERDOC_D3D12_ENABLE_PREPARED_REPLAY") == "1" &&
        Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_PREPARED_REPLAY") != "1";
  }
  else
  {
    m_Cmd.m_RootEventID = 1;
    m_Cmd.m_FirstEventID = 0;
    m_Cmd.m_LastEventID = ~0U;
  }

  if(IsReplayMode(m_State))
  {
    for(size_t i = 0; i < m_Cmd.m_IndirectBuffers.size(); i++)
      SAFE_RELEASE(m_Cmd.m_IndirectBuffers[i]);
    m_Cmd.m_IndirectBuffers.clear();
  }

  uint64_t startOffset = ser.GetReader()->GetOffset();
  const auto recordFullCompanionAtClose = [&](ResourceId baked, uint64_t closeEnd) -> RDResult {
    const RDResult companion = m_Cmd.RecordFullReplayCompanion(
        baked, closeEnd,
        [&](const BakedCmdListInfo::CheckpointRecordingChunk &chunk) -> RDResult {
          ser.GetReader()->SetOffset(chunk.offset);
          m_Cmd.m_CurChunkOffset = chunk.offset;
          D3D12Chunk type = ser.ReadChunk<D3D12Chunk>();
          if(ser.GetReader()->IsErrored() || type != chunk.type)
            return RDResult(ResultCode::APIDataCorrupted,
                            "Repeated-list companion source chunk changed");
          m_Cmd.m_ChunkMetadata = ser.ChunkMetadata();
          m_Cmd.m_LastCmdListID = ResourceId();
          bool success;
          {
            ScopedReplayTimingChunk timing((uint32_t)type);
            success = ProcessChunk(ser, type);
          }
          ser.EndChunk();
          AddReplayTimingCounter(ReplayTimingCounter::ChunkBytes, chunk.endOffset - chunk.offset);
          if(ser.GetReader()->IsErrored())
            return RDResult(ResultCode::APIDataCorrupted, ser.GetError().message);
          if(!success)
            return m_Cmd.m_FailedReplayResult;
          if(m_pDevice->HasFatalError())
            return m_pDevice->FatalErrorCheck();
          if(m_Cmd.m_LastCmdListID != chunk.baked ||
             ser.GetReader()->GetOffset() != chunk.endOffset)
            return RDResult(ResultCode::APIDataCorrupted,
                            "Repeated-list companion command identity changed");
          return ResultCode::Succeeded;
        });
    // No outer stream event/chunk is replayed twice. The helper only records list commands.
    ser.GetReader()->SetOffset(closeEnd);
    return companion;
  };
  if(allowCheckpoints && IsActiveReplaying(m_State) && !partial && startEventID == 1)
  {
    uint64_t resumeOffset = 0;
    uint32_t nextEvent = 0, nextAction = 0;
    if(m_pDevice->RestoreReplayCheckpoint(endEventID, resumeOffset, nextEvent, nextAction))
    {
      m_pDevice->MarkCheckpointReplayWrites();
      if(!m_pDevice->ApplyCheckpointReplayBufferPatches())
        return RDResult(ResultCode::APIReplayFailed,
                        "Could not apply current shared buffer values after checkpoint restore");
      m_Cmd.m_RootEventID = nextEvent;
      // Resume is still a root command stream, not the existing single-list partial path.
      // Keep FirstEventID == 1 so queries, Reset..Close and submission event accounting work.
      m_Cmd.m_PreparedReplayEnabled = false;
      rdcarray<BakedCmdListInfo::CheckpointRecordingChunk> continuation;
      rdcstr continuationError;
      const bool targetContinuation =
          Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_TARGET_CONTINUATION") != "1";
      if(!m_Cmd.GetCheckpointContinuation(resumeOffset, nextEvent, continuation, continuationError,
                                         NULL, targetContinuation ? endEventID : ~0U))
        return RDResult(ResultCode::APIReplayFailed, continuationError);
      PerformanceTimer continuationTimer;
      uint64_t continuationBytes = 0, metadataBytes = 0;
      uint32_t decodedChunks = 0, metadataChunks = 0;
      std::set<ResourceId> decodedLists, metadataLists;
      uint32_t indirectChunks = 0, indirectRecorded = 0;
      // Reconstruct only outstanding command recordings. These whitelisted chunks record native
      // commands and CPU pipeline state; they never execute a queue or read back GPU data. The
      // resource checkpoint remains untouched until the normal suffix reaches their submission.
      for(const auto &chunk : continuation)
      {
        // Lists whose first relevant submission is after this request never get a native
        // rerecord mapping. Keep their Reset/Close identities and interleaved ordering, but use
        // loading's final (including indirect-count compression) cursor instead of decoding
        // command payloads. If Reset disagrees with the plan, use the original handler.
        if(m_Cmd.ApplyCheckpointContinuationMetadata(chunk))
        {
          metadataChunks++;
          metadataBytes += chunk.endOffset - chunk.offset;
          metadataLists.insert(chunk.baked);
          continue;
        }
        decodedChunks++;
        decodedLists.insert(chunk.baked);
        ser.GetReader()->SetOffset(chunk.offset);
        m_Cmd.m_CurChunkOffset = chunk.offset;
        D3D12Chunk context;
        {
          ScopedReplayTimingSection timing(ReplayTimingSection::ChunkFraming);
          context = ser.ReadChunk<D3D12Chunk>();
        }
        if(ser.GetReader()->IsErrored() || context != chunk.type)
          return RDResult(ResultCode::APIDataCorrupted,
                          "Checkpoint continuation command does not match its source chunk");
        m_Cmd.m_ChunkMetadata = ser.ChunkMetadata();
        m_Cmd.m_LastCmdListID = ResourceId();
        if(context == D3D12Chunk::List_ExecuteIndirect)
        {
          indirectChunks++;
          if(m_Cmd.InRerecordRange(chunk.baked))
            indirectRecorded++;
        }
        bool success;
        {
          ScopedReplayTimingChunk timing((uint32_t)context);
          success = ProcessChunk(ser, context);
        }
        {
          ScopedReplayTimingSection timing(ReplayTimingSection::ChunkFraming);
          ser.EndChunk();
        }
        if(ser.GetReader()->IsErrored())
          return RDResult(ResultCode::APIDataCorrupted, ser.GetError().message);
        if(!success)
          return m_Cmd.m_FailedReplayResult;
        if(m_pDevice->HasFatalError())
          return m_pDevice->FatalErrorCheck();
        if(m_Cmd.m_LastCmdListID != chunk.baked || ser.GetReader()->GetOffset() != chunk.endOffset)
          return RDResult(ResultCode::APIDataCorrupted,
                          "Checkpoint continuation command-list identity changed");
        m_Cmd.m_BakedCmdListInfo[chunk.baked].curEventID = chunk.eventAfter;
        if(context == D3D12Chunk::List_Close)
        {
          const RDResult companion = recordFullCompanionAtClose(chunk.baked, chunk.endOffset);
          if(companion != ResultCode::Succeeded)
            return companion;
        }
        m_Cmd.m_LastChunk = context;
        continuationBytes += chunk.endOffset - chunk.offset;
        AddReplayTimingCounter(ReplayTimingCounter::ChunkBytes, chunk.endOffset - chunk.offset);
      }
      if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
      {
        const rdcstr line = StringFormat::Fmt(
            "api=D3D12Checkpoint op=continuation target=%u chunks=%zu bytes=%llu "
            "targetAware=%u decodedChunks=%u metadataChunks=%u metadataBytes=%llu "
            "decodedLists=%zu metadataLists=%zu indirectChunks=%u indirectRecorded=%u total_ms=%.3f",
            endEventID, continuation.size(), (unsigned long long)continuationBytes,
            targetContinuation ? 1U : 0U, decodedChunks, metadataChunks,
            (unsigned long long)metadataBytes, decodedLists.size(), metadataLists.size(),
            indirectChunks, indirectRecorded, continuationTimer.GetMilliseconds());
        RDCLOG("[EIDTiming] %s", line.c_str());
        AppendReplayTimingLog(line);
      }
      ser.GetReader()->SetOffset(resumeOffset);
      m_Cmd.m_LastCmdListID = ResourceId();
      m_Cmd.m_LastChunk = D3D12Chunk::Queue_ExecuteCommandLists;
    }
  }
  setupTiming.End();

  for(;;)
  {
    if(IsActiveReplaying(m_State) && m_Cmd.m_RootEventID > endEventID)
    {
      // we can just break out if we've done all the events desired.
      // note that the command list events aren't 'real' and we just blaze through them
      break;
    }

    m_Cmd.m_CurChunkOffset = ser.GetReader()->GetOffset();

    D3D12Chunk context;
    {
      ScopedReplayTimingSection timing(ReplayTimingSection::ChunkFraming);
      context = ser.ReadChunk<D3D12Chunk>();
    }

    if(ser.GetReader()->IsErrored())
      return RDResult(ResultCode::APIDataCorrupted, ser.GetError().message);

    m_Cmd.m_ChunkMetadata = ser.ChunkMetadata();

    m_Cmd.m_LastCmdListID = ResourceId();

    bool success;
    {
      ScopedReplayTimingChunk timing((uint32_t)context);
      success = ProcessChunk(ser, context);
    }

    {
      ScopedReplayTimingSection timing(ReplayTimingSection::ChunkFraming);
      ser.EndChunk();
    }

    ScopedReplayTimingSection bookkeepingTiming(ReplayTimingSection::ReplayBookkeeping);

    const uint64_t chunkEndOffset = ser.GetReader()->GetOffset();
    AddReplayTimingCounter(ReplayTimingCounter::ChunkBytes, chunkEndOffset - m_Cmd.m_CurChunkOffset);

    if(ser.GetReader()->IsErrored())
      return RDResult(ResultCode::APIDataCorrupted, ser.GetError().message);

    // if there wasn't a serialisation error, but the chunk didn't succeed, then it's an API replay
    // failure.
    if(!success)
    {
      rdcstr extra;

      if(m_pDevice->GetInfoQueue())
      {
        extra += "\n";

        for(UINT64 i = 0;
            i < m_pDevice->GetInfoQueue()->GetNumStoredMessagesAllowedByRetrievalFilter(); i++)
        {
          SIZE_T len = 0;
          m_pDevice->GetInfoQueue()->GetMessage(i, NULL, &len);

          char *msgbuf = new char[len];
          D3D12_MESSAGE *message = (D3D12_MESSAGE *)msgbuf;

          m_pDevice->GetInfoQueue()->GetMessage(i, message, &len);

          extra += "\n";
          extra += message->pDescription;

          delete[] msgbuf;
        }
      }
      else
      {
        extra +=
            "\n\nMore debugging information may be available by enabling API validation on "
            "replay via `File` -> `Open Capture with Options`";
      }

      if(m_pDevice->HasFatalError())
      {
        RDResult result = m_pDevice->FatalErrorCheck();
        result.message = rdcstr(result.message) + extra;
        return result;
      }

      m_Cmd.m_FailedReplayResult.message = rdcstr(m_Cmd.m_FailedReplayResult.message) + extra;
      return m_Cmd.m_FailedReplayResult;
    }

    if(m_pDevice->HasFatalError())
      return ResultCode::Succeeded;

    if(IsLoading(m_State))
      m_Cmd.ObservePreparedReplayChunk(context, chunkEndOffset);
    else if(IsActiveReplaying(m_State))
    {
      if(context == D3D12Chunk::List_Close)
      {
        m_Cmd.FinishPreparedCommandList(m_Cmd.m_LastCmdListID, chunkEndOffset);
        const RDResult companion = recordFullCompanionAtClose(m_Cmd.m_LastCmdListID, chunkEndOffset);
        if(companion != ResultCode::Succeeded)
          return companion;
      }

      if(context == D3D12Chunk::List_Reset && m_Cmd.m_PreparedSkipEnd > chunkEndOffset)
      {
        const ResourceId baked = m_Cmd.m_LastCmdListID;
        BakedCmdListInfo &info = m_Cmd.m_BakedCmdListInfo[baked];
        m_Cmd.m_PreparedSkippedBytes += m_Cmd.m_PreparedSkipEnd - chunkEndOffset;
        if(info.endChunk > info.beginChunk)
          m_Cmd.m_PreparedSkippedChunks += info.endChunk - info.beginChunk;
        info.curEventID = 0;
        ser.GetReader()->SetOffset(m_Cmd.m_PreparedSkipEnd);
        m_Cmd.m_PreparedSkipEnd = 0;
        m_Cmd.m_LastChunk = D3D12Chunk::List_Close;
        continue;
      }
    }

    if(IsLoading(m_State) || IsStructuredExporting(m_State))
      RenderDoc::Inst().SetProgress(
          LoadProgress::FrameEventsRead,
          float(m_Cmd.m_CurChunkOffset - startOffset) / float(ser.GetReader()->GetSize()));

    if((SystemChunk)context == SystemChunk::CaptureEnd || ser.GetReader()->AtEnd())
      break;

    // break out if we were only executing one event
    if(IsActiveReplaying(m_State) && startEventID == endEventID)
      break;

    m_Cmd.m_LastChunk = context;

    // increment root event ID either if we didn't just replay a cmd
    // buffer event, OR if we are doing a frame sub-section replay,
    // in which case it's up to the calling code to make sure we only
    // replay inside a command list (if we crossed command list
    // boundaries, the event IDs would no longer match up).
    if(IsLoading(m_State))
    {
      m_Cmd.m_RootEventID = (uint32_t)m_Cmd.m_EventNodes.size() + 1;
      if(m_Cmd.m_LastCmdListID != ResourceId())
      {
        BakedCmdListInfo &info = m_Cmd.m_BakedCmdListInfo[m_Cmd.m_LastCmdListID];
        info.curEventID = (uint32_t)info.eventNodes.size();
      }
    }
    else if(m_Cmd.m_LastCmdListID == ResourceId() || startEventID > 1)
    {
      if(context != D3D12Chunk::SetQueueAnnotation)
        m_Cmd.m_RootEventID++;

      if(startEventID > 1)
        ser.GetReader()->SetOffset(GetEvent(m_Cmd.m_RootEventID).fileOffset);
    }
    else
    {
      // these events are completely omitted, so don't increment the curEventID
      if(context != D3D12Chunk::List_Reset && context != D3D12Chunk::List_Close &&
         context != D3D12Chunk::SetCommandAnnotation)
        m_Cmd.m_BakedCmdListInfo[m_Cmd.m_LastCmdListID].curEventID++;
    }

    bookkeepingTiming.End();
    if(IsLoading(m_State))
      m_Cmd.ObserveCheckpointChunk(context, chunkEndOffset);
    else if(allowCheckpoints && IsActiveReplaying(m_State) && !partial && startEventID == 1 &&
            context == D3D12Chunk::Queue_ExecuteCommandLists &&
            m_Cmd.m_RootEventID <= endEventID)
      m_pDevice->SaveReplayCheckpoint(m_Cmd.m_RootEventID - 1, chunkEndOffset, m_Cmd.m_RootEventID,
                                      0);
  }

  // swap the structure back now that we've accumulated the frame as well.
  if(IsLoading(m_State) || IsStructuredExporting(m_State))
    ser.GetStructuredFile().Swap(*m_pDevice->GetStructuredFile());

  m_StructuredFile = NULL;

  m_Cmd.m_RerecordCmds.clear();

  if(m_Cmd.m_PreparedTimingEnabled)
  {
    const rdcstr preparedLine = StringFormat::Fmt(
        "api=D3D12Prepared enabled=%u attempts=%u hits=%u builds=%u rejectedType=%u "
        "rejectedUnsafe=%u rejectedExisting=%u rejectedBudget=%u skippedChunks=%u "
        "skippedBytes=%llu cacheLists=%zu cacheSourceBytes=%llu",
        m_Cmd.m_PreparedReplayEnabled ? 1U : 0U, m_Cmd.m_PreparedAttempts,
        m_Cmd.m_PreparedCacheHits, m_Cmd.m_PreparedCacheBuilds, m_Cmd.m_PreparedRejectedType,
        m_Cmd.m_PreparedRejectedUnsafe, m_Cmd.m_PreparedRejectedExisting,
        m_Cmd.m_PreparedRejectedBudget, m_Cmd.m_PreparedSkippedChunks,
        (unsigned long long)m_Cmd.m_PreparedSkippedBytes, m_Cmd.m_PreparedCmdLists.size(),
        (unsigned long long)m_Cmd.m_PreparedCacheBytes);
    RDCLOG("[EIDTiming] %s", preparedLine.c_str());

    // Opening/flushing the timing file (and the main diagnostic logger) for every blocker adds
    // measurable latency to the replay being measured. Keep the regular log compact and append
    // the complete diagnostic block to the timing file in one operation.
    rdcstr preparedLog = preparedLine;

    for(const auto &entry : m_Cmd.m_PreparedRejectSummary)
    {
      const D3D12CommandData::PreparedReplayRejectSummary &summary = entry.second;
      const rdcstr rejectedLine = StringFormat::Fmt(
          "api=D3D12PreparedReject start=%u end=%u reason=%s chunk=%s chunkId=%u attempts=%u "
          "firstBaked=%s firstParent=%s observedList=%s observedParent=%s "
          "firstBeginOffset=%llu firstOffset=%llu scope=firstBlocker",
          startEventID, endEventID, PreparedReplayRejectReasonName(summary.reason),
          ToStr(summary.chunk).c_str(), (uint32_t)summary.chunk, summary.attempts,
          ToStr(summary.firstBaked).c_str(), ToStr(summary.firstParent).c_str(),
          ToStr(summary.observedList).c_str(), ToStr(summary.observedParent).c_str(),
          (unsigned long long)summary.firstBeginOffset, (unsigned long long)summary.firstOffset);
      preparedLog += "\n";
      preparedLog += rejectedLine;
    }
    AppendReplayTimingLog(preparedLog);
  }

  return ResultCode::Succeeded;
}

WrappedID3D12GraphicsCommandList::WrappedID3D12GraphicsCommandList(ResourceId id,
                                                                   ID3D12GraphicsCommandList *real,
                                                                   WrappedID3D12Device *device,
                                                                   CaptureState &state)
    : m_RefCounter(real, false), m_pList(real), m_pDevice(device), m_State(state)
{
  RenderDoc::Inst().RegisterMemoryRegion(this, sizeof(WrappedID3D12GraphicsCommandList));

  m_pList1 = NULL;
  m_pList2 = NULL;
  m_pList3 = NULL;
  m_pList4 = NULL;
  m_pList5 = NULL;

  m_WrappedDebug.m_pList = this;
  if(m_pList)
  {
    m_pList->QueryInterface(__uuidof(ID3D12DebugCommandList), (void **)&m_WrappedDebug.m_pReal);
    m_pList->QueryInterface(__uuidof(ID3D12DebugCommandList1), (void **)&m_WrappedDebug.m_pReal1);
    m_pList->QueryInterface(__uuidof(ID3D12DebugCommandList2), (void **)&m_WrappedDebug.m_pReal2);
    m_pList->QueryInterface(__uuidof(ID3D12DebugCommandList3), (void **)&m_WrappedDebug.m_pReal3);

    m_pList->QueryInterface(__uuidof(ID3D12GraphicsCommandList1), (void **)&m_pList1);
    m_pList->QueryInterface(__uuidof(ID3D12GraphicsCommandList2), (void **)&m_pList2);
    m_pList->QueryInterface(__uuidof(ID3D12GraphicsCommandList3), (void **)&m_pList3);
    m_pList->QueryInterface(__uuidof(ID3D12GraphicsCommandList4), (void **)&m_pList4);
    m_pList->QueryInterface(__uuidof(ID3D12GraphicsCommandList5), (void **)&m_pList5);
    m_pList->QueryInterface(__uuidof(ID3D12GraphicsCommandList6), (void **)&m_pList6);
    m_pList->QueryInterface(__uuidof(ID3D12GraphicsCommandList7), (void **)&m_pList7);
    m_pList->QueryInterface(__uuidof(ID3D12GraphicsCommandList8), (void **)&m_pList8);
    m_pList->QueryInterface(__uuidof(ID3D12GraphicsCommandList9), (void **)&m_pList9);
    m_pList->QueryInterface(__uuidof(ID3D12GraphicsCommandList10), (void **)&m_pList10);
  }

  m_ResourceID = id;
  if(id == ResourceId())
    m_ResourceID = ResourceIDGen::GetNewUniqueID();

  RDCEraseEl(m_Init);

  m_ListRecord = NULL;
  m_CreationRecord = NULL;
  m_Cmd = NULL;

  m_CurGfxRootSig = NULL;
  m_CurCompRootSig = NULL;

  if(!RenderDoc::Inst().IsReplayApp())
  {
    m_ListRecord = m_pDevice->GetResourceManager()->AddResourceRecord(m_ResourceID);
    m_ListRecord->type = Resource_GraphicsCommandList;
    m_ListRecord->DataInSerialiser = false;
    m_ListRecord->InternalResource = true;
    m_ListRecord->Length = 0;

    m_ListRecord->cmdInfo = new CmdListRecordingInfo();

    // this is set up in the implicit Reset() right after creation
    m_ListRecord->bakedCommands = NULL;

    // a bit of a hack, we make a parallel resource record with the same lifetime as the command
    // list and make it a parent, so it will hold onto our create chunk and not try to
    // record it (and throw it away with baked commands that are unused), then it'll be pulled
    // into the capture.
    m_CreationRecord =
        m_pDevice->GetResourceManager()->AddResourceRecord(ResourceIDGen::GetNewUniqueID());
    m_CreationRecord->type = Resource_GraphicsCommandList;
    m_CreationRecord->InternalResource = true;

    m_ListRecord->AddParent(m_CreationRecord);
  }
  else
  {
    m_Cmd = m_pDevice->GetQueue()->GetCommandData();
  }

  if(m_pList)
  {
    bool ret = m_pDevice->GetResourceManager()->AddWrapper(this, m_pList);
    if(!ret)
      RDCERR("Error adding wrapper for ID3D12GraphicsCommandList");
  }

  m_pDevice->GetResourceManager()->AddResource(GetResourceID(), this);

  m_pDevice->SoftRef();
}

WrappedID3D12GraphicsCommandList::~WrappedID3D12GraphicsCommandList()
{
  SAFE_RELEASE(m_AMDMarkers);

  for(std::function<void()> &func : m_UnusedCleanupCallbacks)
    func();
  m_UnusedCleanupCallbacks.clear();

  if(m_pList)
    m_pDevice->GetResourceManager()->RemoveWrapper(this, m_pList);

  if(m_CreationRecord)
    m_CreationRecord->Delete(m_pDevice->GetResourceManager());

  if(m_ListRecord && m_ListRecord->bakedCommands)
    m_ListRecord->bakedCommands->Delete(m_pDevice->GetResourceManager());

  if(m_ListRecord)
    m_ListRecord->Delete(m_pDevice->GetResourceManager());

  m_pDevice->GetResourceManager()->ReleaseResource(GetResourceID());

  SAFE_RELEASE(m_WrappedDebug.m_pReal);
  SAFE_RELEASE(m_WrappedDebug.m_pReal1);
  SAFE_RELEASE(m_WrappedDebug.m_pReal2);
  SAFE_RELEASE(m_WrappedDebug.m_pReal3);
  SAFE_RELEASE(m_pList10);
  SAFE_RELEASE(m_pList9);
  SAFE_RELEASE(m_pList8);
  SAFE_RELEASE(m_pList7);
  SAFE_RELEASE(m_pList6);
  SAFE_RELEASE(m_pList5);
  SAFE_RELEASE(m_pList4);
  SAFE_RELEASE(m_pList3);
  SAFE_RELEASE(m_pList2);
  SAFE_RELEASE(m_pList1);
  SAFE_RELEASE(m_pList);
}

bool WrappedID3D12GraphicsCommandList::ValidateRootGPUVA(D3D12_GPU_VIRTUAL_ADDRESS buffer)
{
  if(buffer == 0)
  {
    // silently drop binds of 0 since this sometimes crashes drivers, and is not legal if the
    // address is ever actually accessed (if it is accessed, whatever was bound or never bound here
    // is just as illegal)

    return true;
  }

  return false;
}

WriteSerialiser &WrappedID3D12GraphicsCommandList::GetThreadSerialiser()
{
  return m_pDevice->GetThreadSerialiser();
}

void WrappedID3D12GraphicsCommandList::AddRayDispatches(rdcarray<PatchedRayDispatch::Resources> &dispatches)
{
  dispatches.reserve(dispatches.size() + m_RayDispatches.size());
  for(const PatchedRayDispatch::Resources &r : m_RayDispatches)
  {
    dispatches.push_back(r);
    r.AddRef();
  }
}

rdcstr WrappedID3D12GraphicsCommandList::GetChunkName(uint32_t idx)
{
  if((SystemChunk)idx < SystemChunk::FirstDriverChunk)
    return ToStr((SystemChunk)idx);

  return ToStr((D3D12Chunk)idx);
}

HRESULT STDMETHODCALLTYPE WrappedID3D12GraphicsCommandList::QueryInterface(REFIID riid,
                                                                           void **ppvObject)
{
  if(riid == __uuidof(IUnknown))
  {
    *ppvObject = (IUnknown *)(ID3D12GraphicsCommandList *)this;
    AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12GraphicsCommandList))
  {
    *ppvObject = (ID3D12GraphicsCommandList *)this;
    AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12DebugCommandList))
  {
    if(m_WrappedDebug.m_pReal)
    {
      AddRef();
      *ppvObject = (ID3D12DebugCommandList *)&m_WrappedDebug;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12DebugCommandList1))
  {
    if(m_WrappedDebug.m_pReal1)
    {
      AddRef();
      *ppvObject = (ID3D12DebugCommandList1 *)&m_WrappedDebug;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12DebugCommandList2))
  {
    if(m_WrappedDebug.m_pReal2)
    {
      AddRef();
      *ppvObject = (ID3D12DebugCommandList2 *)&m_WrappedDebug;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12DebugCommandList3))
  {
    if(m_WrappedDebug.m_pReal3)
    {
      AddRef();
      *ppvObject = (ID3D12DebugCommandList3 *)&m_WrappedDebug;
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12GraphicsCommandList1))
  {
    if(m_pList1)
    {
      *ppvObject = (ID3D12GraphicsCommandList1 *)this;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12GraphicsCommandList2))
  {
    if(m_pList2)
    {
      *ppvObject = (ID3D12GraphicsCommandList2 *)this;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12GraphicsCommandList3))
  {
    if(m_pList3)
    {
      *ppvObject = (ID3D12GraphicsCommandList3 *)this;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12GraphicsCommandList4))
  {
    if(m_pList4)
    {
      *ppvObject = (ID3D12GraphicsCommandList4 *)this;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12GraphicsCommandList5))
  {
    if(m_pList5)
    {
      *ppvObject = (ID3D12GraphicsCommandList5 *)this;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12GraphicsCommandList6))
  {
    if(m_pList6)
    {
      *ppvObject = (ID3D12GraphicsCommandList6 *)this;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12GraphicsCommandList7))
  {
    if(m_pList7)
    {
      *ppvObject = (ID3D12GraphicsCommandList7 *)this;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12GraphicsCommandList8))
  {
    if(m_pList8)
    {
      *ppvObject = (ID3D12GraphicsCommandList8 *)this;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12GraphicsCommandList9))
  {
    if(m_pList9)
    {
      *ppvObject = (ID3D12GraphicsCommandList9 *)this;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12GraphicsCommandList10))
  {
    if(m_pList10)
    {
      *ppvObject = (ID3D12GraphicsCommandList10 *)this;
      AddRef();
      return S_OK;
    }
    else
    {
      return E_NOINTERFACE;
    }
  }
  else if(riid == __uuidof(ID3D12CommandList))
  {
    *ppvObject = (ID3D12CommandList *)this;
    AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12Pageable))
  {
    *ppvObject = (ID3D12Pageable *)this;
    AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12DeviceChild))
  {
    *ppvObject = (ID3D12DeviceChild *)this;
    AddRef();
    return S_OK;
  }
  else if(riid == __uuidof(ID3D12Object))
  {
    *ppvObject = (ID3D12DeviceChild *)this;
    AddRef();
    return S_OK;
  }

  return m_RefCounter.QueryInterface("ID3D12GraphicsCommandList", riid, ppvObject);
}

void BakedCmdListInfo::ShiftReplayMetadata(uint32_t firstFollowingEvent, int64_t delta)
{
  // ExecuteIndirect resolves its real count at submission. Keep all command-list-relative proof
  // records in the same coordinates as the final event stream.
  for(CheckpointRecordingChunk &chunk : checkpointChunks)
    if(chunk.eventAfter >= firstFollowingEvent)
      chunk.eventAfter = uint32_t(int64_t(chunk.eventAfter) + delta);
  for(CheckpointQueryOp &query : checkpointQueries)
    if(query.relativeEvent >= firstFollowingEvent)
      query.relativeEvent = uint32_t(int64_t(query.relativeEvent) + delta);
  for(CheckpointBarrierOp &barrier : checkpointBarriers)
    if(barrier.relativeEvent >= firstFollowingEvent)
      barrier.relativeEvent = uint32_t(int64_t(barrier.relativeEvent) + delta);
  for(CheckpointBarrierOp &barrier : replayTypedBarriers)
    if(barrier.relativeEvent >= firstFollowingEvent)
      barrier.relativeEvent = uint32_t(int64_t(barrier.relativeEvent) + delta);
  for(uint32_t &event : replayClearUnsafeBarriers)
    if(event >= firstFollowingEvent)
      event = uint32_t(int64_t(event) + delta);
  for(ReplayClearOp &clear : replayClearOps)
    if(clear.relativeEvent >= firstFollowingEvent)
      clear.relativeEvent = uint32_t(int64_t(clear.relativeEvent) + delta);
  for(ReplayTextureCopyOp &copy : replayTextureCopies)
    if(copy.relativeEvent >= firstFollowingEvent)
      copy.relativeEvent = uint32_t(int64_t(copy.relativeEvent) + delta);
  for(ReplayTextureReadOp &read : replayTextureReadOps)
    if(read.relativeEvent >= firstFollowingEvent)
      read.relativeEvent = uint32_t(int64_t(read.relativeEvent) + delta);
  for(BufferPatchReadOp &read : bufferPatchReads)
    if(read.relativeEvent >= firstFollowingEvent)
      read.relativeEvent = uint32_t(int64_t(read.relativeEvent) + delta);
  for(auto &write : replayWriteFirstEvents)
    if(write.second >= firstFollowingEvent)
      write.second = uint32_t(int64_t(write.second) + delta);
  for(auto &write : replayRootUAVFirstEvents)
    if(write.second >= firstFollowingEvent)
      write.second = uint32_t(int64_t(write.second) + delta);
  for(auto &write : replayUAVFirstEvents)
    if(write.second >= firstFollowingEvent)
      write.second = uint32_t(int64_t(write.second) + delta);
  for(auto &write : replayDeclaredUAVFirstEvents)
    if(write.second >= firstFollowingEvent)
      write.second = uint32_t(int64_t(write.second) + delta);
  if(replayFirstUnknownWriteEvent != ~0U && replayFirstUnknownWriteEvent >= firstFollowingEvent)
    replayFirstUnknownWriteEvent = uint32_t(int64_t(replayFirstUnknownWriteEvent) + delta);
  if(replayLastIndirectEvent >= firstFollowingEvent)
    replayLastIndirectEvent = uint32_t(int64_t(replayLastIndirectEvent) + delta);
}

SubresourceStateVector BakedCmdListInfo::GetState(WrappedID3D12Device *device, ResourceId id)
{
  std::map<ResourceId, SubresourceStateVector> data;

  data[id] = device->GetSubresourceStates(id);

  device->GetResourceManager()->ApplyBarriers(barriers, data);

  SubresourceStateVector ret = data[id];

  if(type == D3D12_COMMAND_LIST_TYPE_COMPUTE)
  {
    for(D3D12ResourceLayout &layout : ret)
    {
      if(layout.IsStates())
      {
        D3D12_RESOURCE_STATES states = layout.ToStates();
        states &= ~(D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE | D3D12_RESOURCE_STATE_STREAM_OUT |
                    D3D12_RESOURCE_STATE_INDEX_BUFFER | D3D12_RESOURCE_STATE_RENDER_TARGET |
                    D3D12_RESOURCE_STATE_DEPTH_WRITE | D3D12_RESOURCE_STATE_DEPTH_READ);
        layout = D3D12ResourceLayout::FromStates(states);
      }
    }
  }

  return ret;
}

D3D12CommandData::D3D12CommandData()
{
  m_CurChunkOffset = 0;

  m_IndirectOffset = 0;

  m_RootEventID = 1;
  m_FirstEventID = 0;
  m_LastEventID = ~0U;

  m_StructuredFile = NULL;

  m_pDevice = NULL;

  m_ActionCallback = NULL;

  m_AddedEventNode = false;
}

void D3D12CommandData::GetIndirectBuffer(size_t size, ID3D12Resource **buf, uint64_t *offs)
{
  // check if we need to allocate a new buffer
  if(m_IndirectBuffers.empty() || m_IndirectOffset + size > m_IndirectSize)
  {
    D3D12_RESOURCE_DESC indirectDesc;
    indirectDesc.Alignment = 0;
    indirectDesc.DepthOrArraySize = 1;
    indirectDesc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    indirectDesc.Flags = D3D12_RESOURCE_FLAG_NONE;
    indirectDesc.Format = DXGI_FORMAT_UNKNOWN;
    indirectDesc.Height = 1;
    indirectDesc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    indirectDesc.MipLevels = 1;
    indirectDesc.SampleDesc.Count = 1;
    indirectDesc.SampleDesc.Quality = 0;
    indirectDesc.Width = RDCMAX(AlignUp((uint64_t)size, 64ULL), m_IndirectSize);

    D3D12_HEAP_PROPERTIES heapProps;
    heapProps.Type = D3D12_HEAP_TYPE_READBACK;
    heapProps.CPUPageProperty = D3D12_CPU_PAGE_PROPERTY_UNKNOWN;
    heapProps.MemoryPoolPreference = D3D12_MEMORY_POOL_UNKNOWN;
    heapProps.CreationNodeMask = 1;
    heapProps.VisibleNodeMask = 1;

    ID3D12Resource *argbuf = NULL;

    HRESULT hr = m_pDevice->CreateCommittedResource(&heapProps, D3D12_HEAP_FLAG_NONE, &indirectDesc,
                                                    D3D12_RESOURCE_STATE_COPY_DEST, NULL,
                                                    __uuidof(ID3D12Resource), (void **)&argbuf);
    m_pDevice->RemoveReplayResource(GetResID(argbuf));

    SetObjName(argbuf, StringFormat::Fmt("Indirect Readback Buf (%llu bytes)", (uint64_t)size));

    if(FAILED(hr))
      RDCERR("Failed to create indirect buffer, HRESULT: %s", ToStr(hr).c_str());

    m_IndirectBuffers.push_back(argbuf);
    m_IndirectOffset = 0;
  }

  *buf = m_IndirectBuffers.back();
  *offs = m_IndirectOffset;

  m_IndirectOffset = AlignUp16(m_IndirectOffset + size);
}

uint32_t D3D12CommandData::HandlePreCallback(ID3D12GraphicsCommandListX *list, ActionFlags type,
                                             uint32_t multiDrawOffset)
{
  if(!m_ActionCallback)
    return 0;

  // look up the EID this action came from
  ActionUse use(m_CurChunkOffset, 0);
  auto it = std::lower_bound(m_ActionUses.begin(), m_ActionUses.end(), use);

  if(it == m_ActionUses.end())
  {
    RDCERR("Couldn't find action use entry for %llu", m_CurChunkOffset);
    return 0;
  }

  uint32_t eventId = it->eventId;

  RDCASSERT(eventId != 0);

  // handle all aliases of this action as long as it's not a multidraw
  const ActionDescription *action = m_pDevice->GetAction(eventId);

  if(action == NULL || !(action->flags & ActionFlags::MultiAction))
  {
    ++it;
    while(it != m_ActionUses.end() && it->fileOffset == m_CurChunkOffset)
    {
      m_ActionCallback->AliasEvent(eventId, it->eventId);
      ++it;
    }
  }

  eventId += multiDrawOffset;

  switch(type)
  {
    case ActionFlags::MeshDispatch:
    case ActionFlags::Drawcall:
    {
      m_ActionCallback->PreDraw(eventId, list);
      break;
    }
    case ActionFlags::Dispatch:
    case ActionFlags::DispatchRay:
    {
      m_ActionCallback->PreDispatch(eventId, list);
      break;
    }
    default:
    {
      m_ActionCallback->PreMisc(eventId, type, list);
      break;
    }
  }

  return eventId;
}

bool D3D12CommandData::InRerecordRange(ResourceId cmdid)
{
  // if we have an outside command list, assume the range is valid and we're replaying all events
  // onto it.
  if(m_OutsideCmdList != NULL)
    return true;

  // if not, check if we're one of the actual partial command buffers and check to see if we're in
  // the range for their partial replay.
  for(int p = 0; p < ePartialNum; p++)
  {
    if(cmdid == m_Partial[p].partialParent)
    {
      return m_BakedCmdListInfo[m_Partial[p].partialParent].curEventID <=
             m_LastEventID - m_Partial[p].baseEvent;
    }
  }

  // otherwise just check if we have a re-record command list for this, as then we're doing a full
  // re-record and replay
  return m_RerecordCmds.find(cmdid) != m_RerecordCmds.end();
}

bool D3D12CommandData::HasRerecordCmdList(ResourceId cmdid)
{
  if(m_OutsideCmdList != NULL)
    return true;

  return m_RerecordCmds.find(cmdid) != m_RerecordCmds.end();
}

bool D3D12CommandData::IsPartialCmdList(ResourceId cmdid)
{
  if(m_OutsideCmdList != NULL)
    return true;

  for(int p = 0; p < ePartialNum; p++)
    if(cmdid == m_Partial[p].partialParent)
      return true;

  return false;
}

ID3D12GraphicsCommandListX *D3D12CommandData::RerecordCmdList(ResourceId cmdid,
                                                              PartialReplayIndex partialType)
{
  if(m_OutsideCmdList != NULL)
    return m_OutsideCmdList;

  auto it = m_RerecordCmds.find(cmdid);

  if(it == m_RerecordCmds.end())
  {
    RDCERR("Didn't generate re-record command for %s", ToStr(cmdid).c_str());
    return NULL;
  }

  return it->second;
}

bool D3D12PreparedQueryTracker::Begin(ResourceId heap, D3D12_QUERY_TYPE type, UINT index)
{
  if(heap == ResourceId() || type == D3D12_QUERY_TYPE_TIMESTAMP)
    return false;

  for(const Query &query : pending)
    if(query.heap == heap && query.index == index)
      return false;

  pending.push_back({heap, type, index});
  return true;
}

bool D3D12PreparedQueryTracker::End(ResourceId heap, D3D12_QUERY_TYPE type, UINT index)
{
  if(heap == ResourceId())
    return false;

  // Timestamps have only an EndQuery and deliberately do not open a query scope.
  if(type == D3D12_QUERY_TYPE_TIMESTAMP)
    return true;

  for(size_t i = 0; i < pending.size(); i++)
  {
    const Query &query = pending[i];
    if(query.heap == heap && query.type == type && query.index == index)
    {
      pending.erase(i);
      return true;
    }
  }
  return false;
}

static bool IsPreparedReplaySafeChunk(D3D12Chunk chunk)
{
  // Whitelist only commands which record entirely into a native command list and whose referenced
  // objects remain live for the capture. Anything with replay-time readback, generated argument
  // buffers, nested command-list lifetime, or queue/CPU side effects fails closed.
  switch(chunk)
  {
    case D3D12Chunk::List_Close:
    case D3D12Chunk::List_Reset:
    case D3D12Chunk::List_ResourceBarrier:
    // Query validity and saved fallback results are fixed after capture loading. Active replay
    // records native queries/resolves/copies without changing that metadata or allocating buffers.
    // Paired non-timestamp scopes must also pass the per-list loading check below.
    case D3D12Chunk::List_BeginQuery:
    case D3D12Chunk::List_EndQuery:
    case D3D12Chunk::List_ResolveQueryData:
    case D3D12Chunk::List_DrawIndexedInstanced:
    case D3D12Chunk::List_DrawInstanced:
    case D3D12Chunk::List_Dispatch:
    case D3D12Chunk::List_CopyBufferRegion:
    case D3D12Chunk::List_CopyTextureRegion:
    case D3D12Chunk::List_CopyResource:
    case D3D12Chunk::List_ResolveSubresource:
    case D3D12Chunk::List_ClearRenderTargetView:
    case D3D12Chunk::List_ClearDepthStencilView:
    case D3D12Chunk::List_IASetPrimitiveTopology:
    case D3D12Chunk::List_IASetIndexBuffer:
    case D3D12Chunk::List_IASetVertexBuffers:
    case D3D12Chunk::List_SOSetTargets:
    case D3D12Chunk::List_RSSetViewports:
    case D3D12Chunk::List_RSSetScissorRects:
    case D3D12Chunk::List_SetPipelineState:
    case D3D12Chunk::List_SetDescriptorHeaps:
    case D3D12Chunk::List_OMSetRenderTargets:
    case D3D12Chunk::List_OMSetStencilRef:
    case D3D12Chunk::List_OMSetBlendFactor:
    case D3D12Chunk::List_SetGraphicsRootDescriptorTable:
    case D3D12Chunk::List_SetGraphicsRootSignature:
    case D3D12Chunk::List_SetGraphicsRoot32BitConstant:
    case D3D12Chunk::List_SetGraphicsRoot32BitConstants:
    case D3D12Chunk::List_SetGraphicsRootConstantBufferView:
    case D3D12Chunk::List_SetGraphicsRootShaderResourceView:
    case D3D12Chunk::List_SetGraphicsRootUnorderedAccessView:
    case D3D12Chunk::List_SetComputeRootDescriptorTable:
    case D3D12Chunk::List_SetComputeRootSignature:
    case D3D12Chunk::List_SetComputeRoot32BitConstant:
    case D3D12Chunk::List_SetComputeRoot32BitConstants:
    case D3D12Chunk::List_SetComputeRootConstantBufferView:
    case D3D12Chunk::List_SetComputeRootShaderResourceView:
    case D3D12Chunk::List_SetComputeRootUnorderedAccessView:
    case D3D12Chunk::List_OMSetDepthBounds:
    case D3D12Chunk::List_ResolveSubresourceRegion:
    case D3D12Chunk::List_SetSamplePositions:
    case D3D12Chunk::List_SetViewInstanceMask:
    case D3D12Chunk::List_BeginRenderPass:
    case D3D12Chunk::List_EndRenderPass:
    case D3D12Chunk::List_RSSetShadingRate:
    case D3D12Chunk::List_RSSetShadingRateImage:
    case D3D12Chunk::List_OMSetFrontAndBackStencilRef:
    case D3D12Chunk::List_RSSetDepthBias:
    case D3D12Chunk::List_IASetIndexBufferStripCutValue:
    case D3D12Chunk::List_DispatchMesh:
    case D3D12Chunk::List_ClearState:
    case D3D12Chunk::PushMarker:
    case D3D12Chunk::SetMarker:
    case D3D12Chunk::PopMarker:
    case D3D12Chunk::SetCommandAnnotation: return true;
    default: break;
  }

  return false;
}

static bool IsCheckpointContinuationSafeChunk(D3D12Chunk chunk)
{
  if(IsPreparedReplaySafeChunk(chunk))
    return true;

  // Continuation re-runs the original handler and consumes temporary CPU descriptors now; it
  // does not retain a generated native list across replays. Keep this separate from PreparedReplay.
  switch(chunk)
  {
    case D3D12Chunk::List_SetPredication:
    case D3D12Chunk::List_WriteBufferImmediate:
    case D3D12Chunk::List_ClearUnorderedAccessViewUint:
    case D3D12Chunk::List_ClearUnorderedAccessViewFloat:
    case D3D12Chunk::List_AtomicCopyBufferUINT:
    case D3D12Chunk::List_AtomicCopyBufferUINT64:
    // CopyTiles records a copy, not tile mappings. Written sparse storage and queue mapping
    // changes are still independently rejected by resource and stream checkpoint validation.
    case D3D12Chunk::List_CopyTiles:
    // Optional discard-pattern setup fills only debug-owned UPLOAD buffers and records work into
    // this list. The captured destination is not changed until the original suffix submission.
    case D3D12Chunk::List_DiscardResource: return true;
    default: break;
  }
  return false;
}

static bool IsCompletedReplayOccurrence(uint32_t base, uint32_t length, uint32_t partialBase,
                                        uint32_t target)
{
  return length > 0 && base < partialBase && uint64_t(base) + length <= target;
}

enum class DescriptorSlotUse
{
  Unused,
  Used,
  Unknown,
};

static DescriptorSlotUse ReplayDescriptorSlotUse(const BakedCmdListInfo &info, ResourceId heap,
                                                 uint32_t index)
{
  if(!info.replayTextureReadsComplete || info.checkpointChunks.size() < 2 ||
     info.checkpointChunks.front().type != D3D12Chunk::List_Reset ||
     info.checkpointChunks.back().type != D3D12Chunk::List_Close)
    return DescriptorSlotUse::Unknown;
  size_t shaderActions = 0;
  for(const auto &chunk : info.checkpointChunks)
  {
    switch(chunk.type)
    {
      case D3D12Chunk::List_DrawIndexedInstanced:
      case D3D12Chunk::List_DrawInstanced:
      case D3D12Chunk::List_Dispatch:
      case D3D12Chunk::List_DispatchMesh:
      case D3D12Chunk::List_ExecuteIndirect: shaderActions++; break;
      // These operations have explicit GPU addresses/resources, not shader descriptor handles.
      case D3D12Chunk::List_DiscardResource:
      case D3D12Chunk::List_SetPredication:
      case D3D12Chunk::List_WriteBufferImmediate:
      case D3D12Chunk::List_CopyTiles:
      case D3D12Chunk::List_AtomicCopyBufferUINT:
      case D3D12Chunk::List_AtomicCopyBufferUINT64: break;
      default:
        // In particular, UAV clears and bundles require separate descriptor-use metadata.
        if(!IsPreparedReplaySafeChunk(chunk.type))
          return DescriptorSlotUse::Unknown;
        break;
    }
  }
  if(info.replayTextureReadOps.size() < shaderActions)
    return DescriptorSlotUse::Unknown;
  for(const auto &read : info.replayTextureReadOps)
  {
    if(read.unknown || read.unknownDescriptorUses)
      return DescriptorSlotUse::Unknown;
    for(const auto *ranges : {&read.descriptorRanges, &read.otherDescriptorRanges})
      for(const auto &range : *ranges)
      {
        if(uint64_t(range.first) + range.count > uint64_t(UINT_MAX) + 1)
          return DescriptorSlotUse::Unknown;
        if(range.heap == heap && index >= range.first && index - range.first < range.count)
          return DescriptorSlotUse::Used;
      }
  }
  return DescriptorSlotUse::Unused;
}

bool D3D12CommandData::DescriptorSlotUnusedBefore(ResourceId heap, uint32_t index,
                                                  uint32_t beforeEvent, const char *&reason) const
{
  size_t work = 0;
  for(const auto &entry : m_Partial[Primary].cmdListExecs)
  {
    for(uint32_t base : entry.second)
    {
      if(base >= beforeEvent)
        continue;
      const auto found = m_BakedCmdListInfo.find(entry.first);
      if(found == m_BakedCmdListInfo.end() || uint64_t(base) + found->second.eventCount >= beforeEvent)
      {
        reason = "incompleteSubmission";
        return false;
      }
      const BakedCmdListInfo &info = found->second;
      work += info.checkpointChunks.size() + info.replayTextureReadOps.size();
      if(work > 262144)
      {
        reason = "proofBudget";
        return false;
      }
      const DescriptorSlotUse use = ReplayDescriptorSlotUse(info, heap, index);
      if(use != DescriptorSlotUse::Unused)
      {
        reason = use == DescriptorSlotUse::Used ? "overlappingReadRange" : "unknownReadMetadata";
        return false;
      }
    }
  }
  reason = "provedUnusedSlot";
  return true;
}

static bool SameLiveForwardOccurrence(const rdcarray<uint32_t> &submissions, uint32_t base,
                                     uint32_t length, uint32_t current, uint32_t target)
{
  return length > 0 && submissions.contains(base) && base <= current && current < target &&
         uint64_t(target) < uint64_t(base) + length;
}

static bool CompletedLiveForwardIndirect(const BakedCmdListInfo::CheckpointRecordingChunk &chunk,
                                         uint64_t currentOffset, uint32_t currentRelative,
                                         rdcstr &reason)
{
  if(chunk.offset >= currentOffset)
  {
    reason = "indirectAtEndpointOrForwardRange";
    return false;
  }
  // eventAfter is the first following relative EID after loading has removed unused indirect
  // children. It includes the group's closing marker; never infer completion from a child index.
  if(chunk.eventAfter == 0 || chunk.eventAfter > currentRelative)
  {
    reason = "indirectPrefixNotCompleted";
    return false;
  }
  // Loading's signature proof excludes GPU addresses, ray dispatch, and unknown argument types.
  // This prefix has already executed; no old native list or patched argument source is reused.
  if(!chunk.directIndirect)
  {
    reason = "indirectPrefixAddressRayOrUnknown";
    return false;
  }
  return true;
}

static bool LiveForwardCommandsSafe(const BakedCmdListInfo &info, uint64_t currentOffset,
                                   uint64_t targetOffset, uint32_t currentRelative,
                                   uint32_t targetRelative, rdcstr &reason)
{
  if(info.type != D3D12_COMMAND_LIST_TYPE_DIRECT || !info.checkpointBegin ||
     info.checkpointBegin >= currentOffset || currentOffset >= targetOffset ||
     info.checkpointEnd <= targetOffset || info.checkpointChunks.empty() ||
     info.checkpointChunks.front().type != D3D12Chunk::List_Reset)
  {
    reason = "missingDirectRecording";
    return false;
  }
  for(const auto &chunk : info.checkpointChunks)
  {
    if(chunk.offset > targetOffset)
      break;
    // ApplyState cannot reopen these native scopes. Reject them even before the current draw:
    // ordinary partial Close may have repaired an outstanding query, hiding its original scope.
    switch(chunk.type)
    {
      case D3D12Chunk::List_ExecuteIndirect:
        if(!CompletedLiveForwardIndirect(chunk, currentOffset, currentRelative, reason))
          return false;
        break;
      case D3D12Chunk::List_BeginQuery:
      case D3D12Chunk::List_BeginRenderPass:
      case D3D12Chunk::List_EndRenderPass:
      case D3D12Chunk::List_DispatchMesh:
      case D3D12Chunk::List_ClearState:
        reason = "nativeScopeOrUnsupportedState";
        return false;
      case D3D12Chunk::List_EndQuery:
      case D3D12Chunk::List_ResolveQueryData:
        // Completed timestamp values and resolves before current remain physically resident.
        // Do not run the old partial handler over new queries: it deliberately skips EndQuery.
        if(chunk.offset > currentOffset)
        {
          reason = "queryInForwardRange";
          return false;
        }
        break;
      default:
        if(!IsCheckpointContinuationSafeChunk(chunk.type))
        {
          reason = StringFormat::Fmt("unsupportedChunk:%s", ToStr(chunk.type).c_str());
          return false;
        }
        break;
    }
  }
  for(const auto &query : info.checkpointQueries)
    if(query.relativeEvent <= currentRelative && !query.resolve &&
       (!query.completed || query.type != D3D12_QUERY_TYPE_TIMESTAMP))
    {
      reason = "nonTimestampPrefixQuery";
      return false;
    }
  for(const auto &barrier : info.checkpointBarriers)
    if(barrier.relativeEvent <= targetRelative &&
       barrier.type == D3D12_RESOURCE_BARRIER_TYPE_TRANSITION &&
       barrier.flags != D3D12_RESOURCE_BARRIER_FLAG_NONE)
    {
      reason = "splitBarrierPrefix";
      return false;
    }
  return true;
}

static bool LiveForwardEventsMatch(const BakedCmdListInfo &info, const rdcarray<APIEvent> &events,
                                  ResourceId baked, uint32_t current, uint32_t target,
                                  rdcstr &reason)
{
  if(target >= events.size())
  {
    reason = "missingEventIndex";
    return false;
  }
  uint64_t previous = 0;
  for(uint64_t eid = current; eid <= target; eid++)
  {
    const APIEvent &event = events[(size_t)eid];
    if(event.eventId != eid || (eid != current && event.fileOffset <= previous))
    {
      reason = "nonLinearEventRange";
      return false;
    }
    const auto found = std::lower_bound(
        info.checkpointChunks.begin(), info.checkpointChunks.end(), event.fileOffset,
        [](const BakedCmdListInfo::CheckpointRecordingChunk &chunk, uint64_t offset) {
          return chunk.offset < offset;
        });
    if(found == info.checkpointChunks.end() || found->offset != event.fileOffset ||
       found->baked != baked || found->type == D3D12Chunk::List_Reset ||
       found->type == D3D12Chunk::List_Close)
    {
      reason = "eventOutsideRecording";
      return false;
    }
    previous = event.fileOffset;
  }
  return true;
}

bool D3D12CommandData::HasResolvedLiveForwardState() const
{
  const auto &pending = m_RenderState.indirectState;
  return pending.argsBuf == NULL && pending.argsOffs == 0 && pending.comSig == NULL &&
         pending.argsToProcess == 0 && m_IndirectData.commandSig == NULL &&
         m_IndirectData.argsBuffer == NULL && m_IndirectData.argsOffset == 0;
}

bool D3D12CommandData::ValidateLiveForwardRange(ResourceId baked, uint32_t baseEvent,
                                               uint32_t currentEvent, uint32_t targetEvent,
                                               rdcstr &reason) const
{
  reason.clear();
  if(!HasResolvedLiveForwardState())
  {
    reason = "unresolvedIndirectState";
    return false;
  }
  const auto info = m_BakedCmdListInfo.find(baked);
  const auto submissions = m_Partial[Primary].cmdListExecs.find(baked);
  if(info == m_BakedCmdListInfo.end() || submissions == m_Partial[Primary].cmdListExecs.end() ||
     !SameLiveForwardOccurrence(submissions->second, baseEvent, info->second.eventCount,
                                currentEvent, targetEvent))
  {
    reason = "differentSubmissionOrDirection";
    return false;
  }
  if(!m_pDevice)
  {
    reason = "missingDevice";
    return false;
  }
  for(uint32_t event : {currentEvent, targetEvent})
  {
    const ActionDescription *action = m_pDevice->GetAction(event);
    if(!action || action->drawIndex != 0 ||
       !(action->flags & (ActionFlags::Drawcall | ActionFlags::Dispatch)) ||
       (action->flags & (ActionFlags::MultiAction | ActionFlags::DispatchRay | ActionFlags::MeshDispatch)))
    {
      reason = "notOrdinaryDrawOrDispatch";
      return false;
    }
  }
  return LiveForwardEventsMatch(info->second, m_Events, baked, currentEvent, targetEvent, reason) &&
         LiveForwardCommandsSafe(info->second, m_Events[currentEvent].fileOffset,
                                  m_Events[targetEvent].fileOffset, currentEvent - baseEvent,
                                  targetEvent - baseEvent, reason);
}

static bool IsFullReplayCompanionSafe(const BakedCmdListInfo &info)
{
  // Recording the companion at the original Close must observe the same CPU descriptor values
  // as the first recording. Contiguity excludes intervening descriptor writes and other lists.
  if(info.type != D3D12_COMMAND_LIST_TYPE_DIRECT || !info.preparedReplaySafe ||
     info.preparedReplayEnd <= info.preparedReplayBegin || !info.checkpointBarriers.empty() ||
     info.checkpointChunks.size() < 2 ||
     info.checkpointChunks.front().type != D3D12Chunk::List_Reset ||
     info.checkpointChunks.back().type != D3D12Chunk::List_Close)
    return false;

  uint64_t previous = info.preparedReplayBegin;
  for(const auto &chunk : info.checkpointChunks)
  {
    if(chunk.offset != previous || chunk.endOffset <= chunk.offset ||
       !IsPreparedReplaySafeChunk(chunk.type))
      return false;
    previous = chunk.endOffset;
    switch(chunk.type)
    {
      // Keep the first implementation free of nested recording contexts, render-pass repair,
      // generated GPU addresses, and callback/annotation state outside the local state below.
      case D3D12Chunk::List_BeginRenderPass:
      case D3D12Chunk::List_EndRenderPass:
      case D3D12Chunk::SetCommandAnnotation: return false;
      default: break;
    }
  }
  return previous == info.preparedReplayEnd;
}

static bool PreparedQueriesAreTimestampWrites(const BakedCmdListInfo &info)
{
  // End(timestamp) has no paired scope or CPU-side resolve decision. Native execution writes
  // the same heap slot every replay. Resolves, paired queries and incomplete metadata stay on
  // the original recorder; no query result is cached here.
  for(const auto &query : info.checkpointQueries)
    if(query.heap == ResourceId() || query.resolve || !query.completed || query.count != 1 ||
       query.type != D3D12_QUERY_TYPE_TIMESTAMP)
      return false;
  return true;
}

// Copy only mutable recording fields. BakedCmdListInfo owns an action pointer and also contains
// immutable loading indexes: copying or replacing that whole object would double-free/drop them.
struct ReplayCompanionMutableState
{
  ReplayCompanionMutableState(const BakedCmdListInfo &info)
      : state(info.state), barriers(info.barriers), queries(info.m_OutstandingQueries),
        markerCount(info.markerCount), event(info.curEventID)
  {
  }
  void Restore(BakedCmdListInfo &info) const
  {
    info.state = state;
    info.barriers = barriers;
    info.m_OutstandingQueries = queries;
    info.markerCount = markerCount;
    info.curEventID = event;
  }
  D3D12RenderState state;
  BarrierSet barriers;
  rdcarray<BakedCmdListInfo::OutstandingQuery> queries;
  int markerCount;
  uint32_t event;
};

static bool IsCheckpointDirectIndirectSignature(const D3D12CommandSignature &signature)
{
  if(signature.raytraced || signature.arguments.empty())
    return false;
  for(const D3D12_INDIRECT_ARGUMENT_DESC &argument : signature.arguments)
  {
    switch(argument.Type)
    {
      case D3D12_INDIRECT_ARGUMENT_TYPE_DRAW:
      case D3D12_INDIRECT_ARGUMENT_TYPE_DRAW_INDEXED:
      case D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH:
      case D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH_MESH:
      case D3D12_INDIRECT_ARGUMENT_TYPE_CONSTANT: break;
      // Address-bearing VB/IB/CBV/SRV/UAV and ray arguments depend on generated GPU patch buffers
      // and a shared upload ring. Unknown future arguments are not implicitly continuation-safe.
      default: return false;
    }
  }
  return true;
}

#if ENABLED(ENABLE_UNIT_TESTS)

#include "catch/catch.hpp"

TEST_CASE("Descriptor overwrite proof retains unknown and overlapping GPU consumers",
          "[d3d12][replay][descriptor-hazard]")
{
  BakedCmdListInfo info;
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  info.checkpointChunks.push_back({heap, 1, 2, 0, D3D12Chunk::List_Reset});
  info.checkpointChunks.push_back({heap, 3, 4, 0, D3D12Chunk::List_Close});
  CHECK(bool(ReplayDescriptorSlotUse(info, heap, 10) == DescriptorSlotUse::Unused));
  info.checkpointChunks.insert(1, {heap, 2, 3, 1, D3D12Chunk::List_Dispatch});
  CHECK(bool(ReplayDescriptorSlotUse(info, heap, 10) == DescriptorSlotUse::Unknown));
  BakedCmdListInfo::ReplayTextureReadOp read;
  read.descriptorRanges.push_back({heap, 0, 4});
  info.replayTextureReadOps.push_back(read);
  CHECK(bool(ReplayDescriptorSlotUse(info, heap, 10) == DescriptorSlotUse::Unused));
  CHECK(bool(ReplayDescriptorSlotUse(info, heap, 2) == DescriptorSlotUse::Used));
  CHECK(bool(ReplayDescriptorSlotUse(info, heap, 4) == DescriptorSlotUse::Unused));
  info.replayTextureReadOps[0].otherDescriptorRanges.push_back({heap, 10, 2});
  CHECK(bool(ReplayDescriptorSlotUse(info, heap, 10) == DescriptorSlotUse::Used));
  CHECK(bool(ReplayDescriptorSlotUse(info, heap, 12) == DescriptorSlotUse::Unused));
  info.replayTextureReadOps[0].otherDescriptorRanges.clear();
  info.replayTextureReadOps[0].unknownDescriptorUses = true;
  CHECK(bool(ReplayDescriptorSlotUse(info, heap, 10) == DescriptorSlotUse::Unknown));
  info.replayTextureReadOps[0].unknownDescriptorUses = false;
  info.replayTextureReadOps[0].unknown = true;
  CHECK(bool(ReplayDescriptorSlotUse(info, heap, 10) == DescriptorSlotUse::Unknown));
  info.replayTextureReadOps[0].unknown = false;
  info.replayTextureReadsComplete = false;
  CHECK(bool(ReplayDescriptorSlotUse(info, heap, 10) == DescriptorSlotUse::Unknown));
  info.replayTextureReadsComplete = true;
  info.replayTextureReadOps[0].descriptorRanges[0] = {heap, UINT_MAX, 2};
  CHECK(bool(ReplayDescriptorSlotUse(info, heap, 10) == DescriptorSlotUse::Unknown));
  info.replayTextureReadOps[0].descriptorRanges[0] = {heap, 0, 1048576};
  CHECK(bool(ReplayDescriptorSlotUse(info, heap, 426647) == DescriptorSlotUse::Used));
  info.checkpointChunks[1].type = D3D12Chunk::List_ClearUnorderedAccessViewUint;
  CHECK(bool(ReplayDescriptorSlotUse(info, heap, 426647) == DescriptorSlotUse::Unknown));
  info.checkpointChunks.back().type = D3D12Chunk::List_SetPipelineState;
  CHECK(bool(ReplayDescriptorSlotUse(info, heap, 426647) == DescriptorSlotUse::Unknown));
}

TEST_CASE("D3D12 live forward stays inside the actual primary submission instance",
          "[d3d12][replay][live-forward]")
{
  const rdcarray<uint32_t> submissions = {44, 68};
  CHECK(SameLiveForwardOccurrence(submissions, 44, 19, 50, 58));
  CHECK(SameLiveForwardOccurrence(submissions, 68, 19, 74, 82));
  CHECK_FALSE(SameLiveForwardOccurrence(submissions, 44, 19, 58, 82));
  CHECK_FALSE(SameLiveForwardOccurrence(submissions, 68, 19, 58, 82));
  CHECK_FALSE(SameLiveForwardOccurrence(submissions, 68, 19, 82, 82));
  CHECK_FALSE(SameLiveForwardOccurrence(submissions, 68, 19, 82, 74));
  CHECK_FALSE(SameLiveForwardOccurrence(submissions, 68, 19, 82, 87));
  CHECK_FALSE(SameLiveForwardOccurrence(submissions, 69, 19, 74, 82));
  CHECK_FALSE(SameLiveForwardOccurrence(submissions, 68, 0, 74, 82));
  const rdcarray<uint32_t> high = {~0U - 10};
  CHECK(SameLiveForwardOccurrence(high, ~0U - 10, 20, ~0U - 2, ~0U - 1));
  CHECK_FALSE(SameLiveForwardOccurrence(high, ~0U - 10, 20, ~0U - 2, 1));
}

TEST_CASE("D3D12 live forward requires a dense unambiguous API event interval",
          "[d3d12][replay][live-forward]")
{
  BakedCmdListInfo info;
  const ResourceId baked = ResourceIDGen::GetNewUniqueID();
  const ResourceId other = ResourceIDGen::GetNewUniqueID();
  info.checkpointChunks = {{baked, 10, 20, 0, D3D12Chunk::List_Reset},
                           {baked, 20, 30, 1, D3D12Chunk::List_DrawInstanced},
                           {baked, 30, 40, 2, D3D12Chunk::List_SetPipelineState},
                           {baked, 40, 50, 3, D3D12Chunk::List_DrawInstanced},
                           {baked, 50, 60, 0, D3D12Chunk::List_Close}};
  rdcarray<APIEvent> events;
  events.resize(8);
  for(uint32_t eid = 5; eid <= 7; eid++)
  {
    events[eid].eventId = eid;
    events[eid].fileOffset = 20 + 10 * (eid - 5);
  }
  rdcstr reason;
  CHECK(LiveForwardEventsMatch(info, events, baked, 5, 7, reason));
  events[6].eventId = 0;
  CHECK_FALSE(LiveForwardEventsMatch(info, events, baked, 5, 7, reason));
  events[6].eventId = 6;
  events[6].fileOffset = 20; // indirect children may share one original chunk
  CHECK_FALSE(LiveForwardEventsMatch(info, events, baked, 5, 7, reason));
  events[6].fileOffset = 31;
  CHECK_FALSE(LiveForwardEventsMatch(info, events, baked, 5, 7, reason));
  events[6].fileOffset = 30;
  info.checkpointChunks[2].baked = other;
  CHECK_FALSE(LiveForwardEventsMatch(info, events, baked, 5, 7, reason));
  info.checkpointChunks[2].baked = baked;
  info.checkpointChunks[2].type = D3D12Chunk::List_Close;
  CHECK_FALSE(LiveForwardEventsMatch(info, events, baked, 5, 7, reason));
  info.checkpointChunks[2].type = D3D12Chunk::List_SetPipelineState;
  CHECK_FALSE(LiveForwardEventsMatch(info, events, baked, 5, 8, reason));
}

TEST_CASE("D3D12 live forward excludes native scopes and preserves resident prefix queries",
          "[d3d12][replay][live-forward]")
{
  BakedCmdListInfo info;
  const ResourceId baked = ResourceIDGen::GetNewUniqueID();
  info.type = D3D12_COMMAND_LIST_TYPE_DIRECT;
  info.checkpointBegin = 10;
  info.checkpointEnd = 80;
  info.checkpointChunks = {{baked, 10, 20, 0, D3D12Chunk::List_Reset},
                           {baked, 20, 30, 1, D3D12Chunk::List_EndQuery},
                           {baked, 30, 40, 2, D3D12Chunk::List_DrawInstanced},
                           {baked, 40, 50, 3, D3D12Chunk::List_ResourceBarrier},
                           {baked, 50, 60, 4, D3D12Chunk::List_DrawInstanced},
                           {baked, 60, 70, 5, D3D12Chunk::List_ExecuteIndirect},
                           {baked, 70, 80, 0, D3D12Chunk::List_Close}};
  BakedCmdListInfo::CheckpointQueryOp query;
  query.relativeEvent = 1;
  query.type = D3D12_QUERY_TYPE_TIMESTAMP;
  query.completed = true;
  info.checkpointQueries.push_back(query);
  rdcstr reason;
  CHECK(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  // Even a query scope which partial Close repaired cannot be reconstructed by ApplyState.
  info.checkpointChunks[1].type = D3D12Chunk::List_BeginQuery;
  CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  info.checkpointChunks[1].type = D3D12Chunk::List_EndQuery;
  info.checkpointQueries[0].type = D3D12_QUERY_TYPE_OCCLUSION;
  CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  info.checkpointQueries[0].type = D3D12_QUERY_TYPE_TIMESTAMP;
  info.checkpointQueries[0].completed = false;
  CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  info.checkpointQueries[0].completed = true;
  for(D3D12Chunk unsupported : {D3D12Chunk::List_EndQuery, D3D12Chunk::List_ResolveQueryData,
                                D3D12Chunk::List_BeginRenderPass, D3D12Chunk::List_EndRenderPass,
                                D3D12Chunk::List_ExecuteIndirect, D3D12Chunk::List_ExecuteBundle,
                                D3D12Chunk::List_DispatchRays, D3D12Chunk::List_ClearState})
  {
    info.checkpointChunks[3].type = unsupported;
    CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  }
  info.checkpointChunks[3].type = D3D12Chunk::List_ResourceBarrier;
  BakedCmdListInfo::CheckpointBarrierOp barrier;
  barrier.relativeEvent = 3;
  barrier.type = D3D12_RESOURCE_BARRIER_TYPE_ALIASING;
  info.checkpointBarriers.push_back(barrier);
  CHECK(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  info.checkpointBarriers[0].type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
  info.checkpointBarriers[0].flags = D3D12_RESOURCE_BARRIER_FLAG_BEGIN_ONLY;
  CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  info.checkpointBarriers[0].flags = D3D12_RESOURCE_BARRIER_FLAG_END_ONLY;
  CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  info.checkpointBarriers[0].relativeEvent = 5;
  CHECK(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  info.type = D3D12_COMMAND_LIST_TYPE_COMPUTE;
  CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
}

TEST_CASE("D3D12 live forward accepts only completed direct indirect prefixes with resolved state",
          "[d3d12][replay][live-forward]")
{
  BakedCmdListInfo info;
  const ResourceId baked = ResourceIDGen::GetNewUniqueID();
  info.type = D3D12_COMMAND_LIST_TYPE_DIRECT;
  info.checkpointBegin = 10;
  info.checkpointEnd = 80;
  info.checkpointChunks = {{baked, 10, 20, 0, D3D12Chunk::List_Reset},
                           {baked, 20, 30, 2, D3D12Chunk::List_ExecuteIndirect, true},
                           {baked, 30, 40, 3, D3D12Chunk::List_DrawInstanced},
                           {baked, 40, 50, 4, D3D12Chunk::List_SetPipelineState},
                           {baked, 50, 60, 5, D3D12Chunk::List_DrawInstanced},
                           {baked, 60, 70, 6, D3D12Chunk::List_ExecuteIndirect},
                           {baked, 70, 80, 0, D3D12Chunk::List_Close}};
  rdcstr reason;
  CHECK(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  // The compacted group's first following event may be the current ordinary draw, but not later.
  info.checkpointChunks[1].eventAfter = 3;
  CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  CHECK(reason == "indirectPrefixNotCompleted");
  info.checkpointChunks[1].eventAfter = 0;
  CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  info.checkpointChunks[1].eventAfter = 2;
  info.checkpointChunks[1].directIndirect = false; // GPU VA, ray, or unproved signature
  CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  CHECK(reason == "indirectPrefixAddressRayOrUnknown");
  info.checkpointChunks[1].directIndirect = true;
  // Even the first indirect child can have drawIndex zero: the native chunk must precede current.
  CHECK_FALSE(LiveForwardCommandsSafe(info, 20, 50, 2, 4, reason));
  CHECK(reason == "indirectAtEndpointOrForwardRange");
  info.checkpointChunks[3].type = D3D12Chunk::List_ExecuteIndirect;
  info.checkpointChunks[3].directIndirect = true;
  CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  CHECK(reason == "indirectAtEndpointOrForwardRange");
  info.checkpointChunks[3].type = D3D12Chunk::List_SetPipelineState;
  info.checkpointChunks[4].type = D3D12Chunk::List_ExecuteIndirect;
  info.checkpointChunks[4].directIndirect = true;
  CHECK_FALSE(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));
  info.checkpointChunks[4].type = D3D12Chunk::List_DrawInstanced;
  CHECK(LiveForwardCommandsSafe(info, 30, 50, 2, 4, reason));

  D3D12CommandData command;
  CHECK(command.HasResolvedLiveForwardState());
  command.m_RenderState.indirectState.argsToProcess = 1;
  CHECK_FALSE(command.HasResolvedLiveForwardState());
  command.m_RenderState.indirectState = {};
  command.m_RenderState.indirectState.argsOffs = 16;
  CHECK_FALSE(command.HasResolvedLiveForwardState());
  command.m_RenderState.indirectState = {};
  // These borrowed addresses are never dereferenced or owned; only pending-pointer presence is tested.
  command.m_RenderState.indirectState.argsBuf = reinterpret_cast<ID3D12Resource *>(&command);
  CHECK_FALSE(command.HasResolvedLiveForwardState());
  command.m_RenderState.indirectState = {};
  command.m_RenderState.indirectState.comSig = reinterpret_cast<ID3D12CommandSignature *>(&command);
  CHECK_FALSE(command.HasResolvedLiveForwardState());
  command.m_RenderState.indirectState = {};
  command.m_IndirectData.argsOffset = 16;
  CHECK_FALSE(command.HasResolvedLiveForwardState());
  command.m_IndirectData = {};
  command.m_IndirectData.argsBuffer = reinterpret_cast<ID3D12Resource *>(&command);
  CHECK_FALSE(command.HasResolvedLiveForwardState());
  command.m_IndirectData = {};
  command.m_IndirectData.commandSig = reinterpret_cast<ID3D12CommandSignature *>(&command);
  CHECK_FALSE(command.HasResolvedLiveForwardState());
  command.m_IndirectData = {};
  CHECK(command.HasResolvedLiveForwardState());
}

TEST_CASE("D3D12 repeated submissions choose full versions only for completed occurrences",
          "[d3d12][replay][repeated-list]")
{
  // In the query fixture, target82's WithoutDraw ends at81. The first occurrence must retain
  // its trailing EndQuery at local17; the second occurrence is truncated at local13.
  CHECK(IsCompletedReplayOccurrence(44, 19, 68, 81));
  CHECK_FALSE(IsCompletedReplayOccurrence(68, 19, 68, 81));
  CHECK_FALSE(IsCompletedReplayOccurrence(44, 19, 44, 57));
  CHECK_FALSE(IsCompletedReplayOccurrence(92, 19, 68, 81));
  CHECK_FALSE(IsCompletedReplayOccurrence(44, 0, 68, 81));
  CHECK_FALSE(IsCompletedReplayOccurrence(~0U - 1, 19, ~0U, ~0U));
}

TEST_CASE("D3D12 full companions reject interleaving and state outside their recording context",
          "[d3d12][replay][repeated-list]")
{
  BakedCmdListInfo info;
  const ResourceId baked = ResourceIDGen::GetNewUniqueID();
  info.type = D3D12_COMMAND_LIST_TYPE_DIRECT;
  info.preparedReplaySafe = true;
  info.preparedReplayBegin = 10;
  info.preparedReplayEnd = 50;
  info.checkpointChunks = {{baked, 10, 20, 0, D3D12Chunk::List_Reset},
                           {baked, 20, 30, 1, D3D12Chunk::List_ResolveQueryData},
                           {baked, 30, 40, 2, D3D12Chunk::List_CopyBufferRegion},
                           {baked, 40, 50, 0, D3D12Chunk::List_Close}};
  CHECK(IsFullReplayCompanionSafe(info));
  info.checkpointChunks[2].offset++;
  CHECK_FALSE(IsFullReplayCompanionSafe(info));
  info.checkpointChunks[2].offset--;
  for(D3D12Chunk chunk : {D3D12Chunk::List_ExecuteBundle, D3D12Chunk::List_ExecuteIndirect,
                         D3D12Chunk::List_BeginRenderPass, D3D12Chunk::List_EndRenderPass,
                         D3D12Chunk::SetCommandAnnotation, D3D12Chunk::Device_CreateRenderTargetView})
  {
    info.checkpointChunks[2].type = chunk;
    CHECK_FALSE(IsFullReplayCompanionSafe(info));
  }
  info.checkpointChunks[2].type = D3D12Chunk::List_CopyBufferRegion;
  info.checkpointBarriers.push_back({});
  CHECK_FALSE(IsFullReplayCompanionSafe(info));
  info.checkpointBarriers.clear();
  info.type = D3D12_COMMAND_LIST_TYPE_BUNDLE;
  CHECK_FALSE(IsFullReplayCompanionSafe(info));
  info.type = D3D12_COMMAND_LIST_TYPE_DIRECT;
  info.preparedReplaySafe = false;
  CHECK_FALSE(IsFullReplayCompanionSafe(info));
}

TEST_CASE("D3D12 companion state restoration preserves partial outputs and loading ownership",
          "[d3d12][replay][repeated-list]")
{
  BakedCmdListInfo info;
  info.eventNodes.resize(1);
  D3D12EventNode *const ownedEvent = info.eventNodes.data();
  info.markerCount = 2;
  info.curEventID = 13;
  info.state.pipe = ResourceIDGen::GetNewUniqueID();
  const ResourceId partialPipeline = info.state.pipe;
  info.state.views.push_back({1.0f, 2.0f, 3.0f, 4.0f, 0.0f, 1.0f});
  info.state.graphics.sigelems.resize(1);
  info.state.graphics.sigelems[0].SetConstant(0, 7);
  info.m_OutstandingQueries.push_back({NULL, D3D12_QUERY_TYPE_BINARY_OCCLUSION, 1});
  info.barriers.barriers.push_back({});
  info.bufferPatchConsumers.insert(partialPipeline);
  ReplayCompanionMutableState saved(info);
  info.state.pipe = ResourceId();
  info.state.views[0].Width = 999.0f;
  info.state.graphics.sigelems[0].SetConstant(0, 99);
  info.markerCount = 0;
  info.curEventID = 0;
  info.m_OutstandingQueries.clear();
  info.barriers.clear();
  saved.Restore(info);
  CHECK(info.state.pipe == partialPipeline);
  CHECK(info.state.views[0].Width == 3.0f);
  CHECK(info.state.graphics.sigelems[0].constants[0] == 7);
  CHECK(info.markerCount == 2);
  CHECK(info.curEventID == 13);
  REQUIRE(info.m_OutstandingQueries.size() == 1);
  CHECK(info.m_OutstandingQueries[0].Index == 1);
  CHECK(info.barriers.barriers.size() == 1);
  CHECK(info.eventNodes.data() == ownedEvent);
  CHECK(info.bufferPatchConsumers.count(partialPipeline) == 1);
}

TEST_CASE("D3D12 checkpoint indirect signatures exclude GPU address patching",
          "[d3d12][replay][checkpoint]")
{
  D3D12CommandSignature signature;
  CHECK_FALSE(IsCheckpointDirectIndirectSignature(signature));
  D3D12_INDIRECT_ARGUMENT_DESC argument = {};
  for(D3D12_INDIRECT_ARGUMENT_TYPE type :
      {D3D12_INDIRECT_ARGUMENT_TYPE_DRAW, D3D12_INDIRECT_ARGUMENT_TYPE_DRAW_INDEXED,
       D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH, D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH_MESH,
       D3D12_INDIRECT_ARGUMENT_TYPE_CONSTANT})
  {
    argument.Type = type;
    signature.arguments = {argument};
    CHECK(IsCheckpointDirectIndirectSignature(signature));
  }
  for(D3D12_INDIRECT_ARGUMENT_TYPE type :
      {D3D12_INDIRECT_ARGUMENT_TYPE_VERTEX_BUFFER_VIEW, D3D12_INDIRECT_ARGUMENT_TYPE_INDEX_BUFFER_VIEW,
       D3D12_INDIRECT_ARGUMENT_TYPE_CONSTANT_BUFFER_VIEW,
       D3D12_INDIRECT_ARGUMENT_TYPE_SHADER_RESOURCE_VIEW,
       D3D12_INDIRECT_ARGUMENT_TYPE_UNORDERED_ACCESS_VIEW, D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH_RAYS,
       D3D12_INDIRECT_ARGUMENT_TYPE(0xffff)})
  {
    argument.Type = type;
    signature.arguments.push_back(argument);
    CHECK_FALSE(IsCheckpointDirectIndirectSignature(signature));
    signature.arguments.pop_back();
  }
  signature.raytraced = true;
  CHECK_FALSE(IsCheckpointDirectIndirectSignature(signature));
  CHECK_FALSE(IsPreparedReplaySafeChunk(D3D12Chunk::List_ExecuteIndirect));
}

TEST_CASE("D3D12 checkpoint metadata follows final indirect event counts",
          "[d3d12][replay][checkpoint]")
{
  BakedCmdListInfo info;
  const ResourceId baked = ResourceIDGen::GetNewUniqueID();
  info.checkpointChunks.push_back({baked, 1, 2, 0, D3D12Chunk::List_Reset});
  info.checkpointChunks.push_back({baked, 3, 4, 1, D3D12Chunk::List_SetPipelineState});
  info.checkpointChunks.push_back({baked, 5, 6, 7, D3D12Chunk::List_ExecuteIndirect});
  info.checkpointChunks.push_back({baked, 7, 8, 11, D3D12Chunk::List_ClearRenderTargetView});
  info.checkpointChunks.push_back({baked, 9, 10, 0, D3D12Chunk::List_Close});
  info.checkpointQueries.push_back({baked, 0, 1, 1, false});
  info.checkpointQueries.push_back({baked, 0, 1, 7, true});
  BakedCmdListInfo::CheckpointBarrierOp barrier;
  barrier.relativeEvent = 8;
  info.checkpointBarriers.push_back(barrier);
  info.replayTypedBarriers.push_back(barrier);
  info.RecordReplayTextureCopy({baked, baked, 10, true, true});
  info.replayClearUnsafeBarriers = {2, 8};
  info.replayClearOps.push_back({baked, 10, true, true});
  BakedCmdListInfo::ReplayTextureReadOp beforeIndirect, afterIndirect;
  beforeIndirect.relativeEvent = 2;
  afterIndirect.relativeEvent = 10;
  afterIndirect.directResources.push_back(baked);
  afterIndirect.descriptorRanges.push_back({baked, 4, 8});
  info.replayTextureReadOps = {beforeIndirect, afterIndirect};
  info.ShiftReplayMetadata(6, -3);
  CHECK(info.checkpointChunks[0].eventAfter == 0);
  CHECK(info.checkpointChunks[1].eventAfter == 1);
  CHECK(info.checkpointChunks[2].eventAfter == 4);
  CHECK(info.checkpointChunks[3].eventAfter == 8);
  CHECK(info.checkpointChunks[4].eventAfter == 0);
  CHECK(info.checkpointQueries[0].relativeEvent == 1);
  CHECK(info.checkpointQueries[1].relativeEvent == 4);
  CHECK(info.checkpointBarriers[0].relativeEvent == 5);
  CHECK(info.replayTypedBarriers[0].relativeEvent == 5);
  CHECK(info.replayTextureCopies[0].relativeEvent == 7);
  REQUIRE(info.replayClearUnsafeBarriers.size() == 2);
  CHECK(info.replayClearUnsafeBarriers[0] == 2);
  CHECK(info.replayClearUnsafeBarriers[1] == 5);
  CHECK(info.replayClearOps[0].relativeEvent == 7);
  REQUIRE(info.replayTextureReadOps.size() == 2);
  CHECK(info.replayTextureReadOps[0].relativeEvent == 2);
  CHECK(info.replayTextureReadOps[1].relativeEvent == 7);
  CHECK(info.replayTextureReadOps[1].directResources[0] == baked);
  CHECK(info.replayTextureReadOps[1].descriptorRanges[0].first == 4);
}

TEST_CASE("D3D12 prepared replay queries require matching command-list-local scopes",
          "[d3d12][replay][prepared-query]")
{
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  const ResourceId otherHeap = ResourceIDGen::GetNewUniqueID();
  D3D12PreparedQueryTracker firstList, secondList;

  CHECK(firstList.Complete());
  REQUIRE(firstList.Begin(heap, D3D12_QUERY_TYPE_OCCLUSION, 7));
  CHECK_FALSE(firstList.Complete());
  CHECK_FALSE(firstList.Begin(heap, D3D12_QUERY_TYPE_BINARY_OCCLUSION, 7));
  CHECK_FALSE(firstList.End(otherHeap, D3D12_QUERY_TYPE_OCCLUSION, 7));
  CHECK_FALSE(firstList.End(heap, D3D12_QUERY_TYPE_BINARY_OCCLUSION, 7));
  CHECK_FALSE(firstList.End(heap, D3D12_QUERY_TYPE_OCCLUSION, 8));
  CHECK_FALSE(secondList.End(heap, D3D12_QUERY_TYPE_OCCLUSION, 7));
  CHECK_FALSE(firstList.Complete());
  CHECK(firstList.End(heap, D3D12_QUERY_TYPE_OCCLUSION, 7));
  CHECK(firstList.Complete());
  CHECK_FALSE(firstList.End(heap, D3D12_QUERY_TYPE_OCCLUSION, 7));
}

TEST_CASE("D3D12 prepared timestamps have no begin scope and do not consume paired queries",
          "[d3d12][replay][prepared-query]")
{
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  D3D12PreparedQueryTracker tracker;

  CHECK_FALSE(tracker.Begin(heap, D3D12_QUERY_TYPE_TIMESTAMP, 1));
  CHECK(tracker.End(heap, D3D12_QUERY_TYPE_TIMESTAMP, 1));
  CHECK(tracker.Complete());
  REQUIRE(tracker.Begin(heap, D3D12_QUERY_TYPE_PIPELINE_STATISTICS, 2));
  CHECK(tracker.End(heap, D3D12_QUERY_TYPE_TIMESTAMP, 1));
  CHECK_FALSE(tracker.Complete());
  CHECK_FALSE(tracker.End(ResourceId(), D3D12_QUERY_TYPE_TIMESTAMP, 1));
  CHECK(tracker.End(heap, D3D12_QUERY_TYPE_PIPELINE_STATISTICS, 2));
  CHECK(tracker.Complete());
}

TEST_CASE("Prepared native queries retain only proved timestamp writes",
          "[d3d12][replay][prepared-query]")
{
  BakedCmdListInfo info;
  CHECK(PreparedQueriesAreTimestampWrites(info));
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  info.checkpointQueries.push_back({heap, 1, 1, 2, false, D3D12_QUERY_TYPE_TIMESTAMP, true});
  REQUIRE(PreparedQueriesAreTimestampWrites(info));
  info.checkpointQueries.back().resolve = true;
  CHECK_FALSE(PreparedQueriesAreTimestampWrites(info));
  info.checkpointQueries.back().resolve = false;
  info.checkpointQueries.back().completed = false;
  CHECK_FALSE(PreparedQueriesAreTimestampWrites(info));
  info.checkpointQueries.back().completed = true;
  info.checkpointQueries.back().type = D3D12_QUERY_TYPE_OCCLUSION;
  CHECK_FALSE(PreparedQueriesAreTimestampWrites(info));
  info.checkpointQueries.back().type = D3D12_QUERY_TYPE_TIMESTAMP;
  info.checkpointQueries.back().count = 2;
  CHECK_FALSE(PreparedQueriesAreTimestampWrites(info));
  info.checkpointQueries.back().count = 1;
  info.checkpointQueries.back().heap = ResourceId();
  CHECK_FALSE(PreparedQueriesAreTimestampWrites(info));
}

TEST_CASE("D3D12 checkpoints require closed root-stream boundaries",
          "[d3d12][replay][checkpoint]")
{
  D3D12CommandData command;
  const ResourceId prefix = ResourceIDGen::GetNewUniqueID();
  const ResourceId suffix = ResourceIDGen::GetNewUniqueID();
  auto &first = command.m_BakedCmdListInfo[prefix];
  first.checkpointBegin = 10;
  first.checkpointEnd = 100;
  first.eventCount = 20;
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[prefix].push_back(2);
  auto &second = command.m_BakedCmdListInfo[suffix];
  second.checkpointBegin = 120;
  second.checkpointEnd = 200;
  second.eventCount = 30;
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[suffix].push_back(25);
  D3D12CommandData::CheckpointBoundary boundary = {23, 24, 8, 110};
  rdcstr reason;
  CHECK(command.IsCheckpointBoundarySafe(boundary, reason));
  const ResourceId queryHeap = ResourceIDGen::GetNewUniqueID();
  second.checkpointQueries.push_back({queryHeap, 4, 1, 1, false});
  second.checkpointQueries.push_back({queryHeap, 4, 1, 2, true});
  CHECK(command.IsCheckpointBoundarySafe(boundary, reason));
  second.checkpointQueries.erase(0);
  CHECK(command.IsCheckpointBoundarySafe(boundary, reason));
  CHECK(boundary.firstNativeResolveEID == 27);
  second.checkpointQueries.clear();

  BakedCmdListInfo::CheckpointBarrierOp beginSplit;
  beginSplit.before = ResourceIDGen::GetNewUniqueID();
  beginSplit.relativeEvent = 1;
  beginSplit.subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
  beginSplit.flags = D3D12_RESOURCE_BARRIER_FLAG_BEGIN_ONLY;
  beginSplit.stateBefore = D3D12_RESOURCE_STATE_COPY_DEST;
  beginSplit.stateAfter = D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE;
  first.checkpointBarriers.push_back(beginSplit);
  CHECK_FALSE(command.IsCheckpointBoundarySafe(boundary, reason));
  auto endSplit = beginSplit;
  endSplit.relativeEvent = 2;
  endSplit.flags = D3D12_RESOURCE_BARRIER_FLAG_END_ONLY;
  first.checkpointBarriers.push_back(endSplit);
  CHECK(command.IsCheckpointBoundarySafe(boundary, reason));
  first.checkpointBarriers.back().stateAfter = D3D12_RESOURCE_STATE_NON_PIXEL_SHADER_RESOURCE;
  CHECK_FALSE(command.IsCheckpointBoundarySafe(boundary, reason));
  first.checkpointBarriers.clear();
  first.checkpointBarriers.push_back(endSplit);
  CHECK_FALSE(command.IsCheckpointBoundarySafe(boundary, reason));
  first.checkpointBarriers.clear();
  first.checkpointBarriers.push_back(beginSplit);
  auto individualSplit = beginSplit;
  individualSplit.subresource = 0;
  first.checkpointBarriers.push_back(individualSplit);
  CHECK_FALSE(command.IsCheckpointBoundarySafe(boundary, reason));
  first.checkpointBarriers.clear();
  first.checkpointBarriers.push_back(beginSplit);
  second.checkpointBarriers.push_back(endSplit);
  CHECK_FALSE(command.IsCheckpointBoundarySafe(boundary, reason));
  D3D12CommandData::CheckpointBoundary afterBothSubmissions = {56, 57, 10, 210};
  CHECK(command.IsCheckpointBoundarySafe(afterBothSubmissions, reason));
  first.checkpointBarriers.clear();
  second.checkpointBarriers.clear();

  // An outstanding recording needs its indexed Reset and pure recording prefix reconstructed.
  second.checkpointBegin = 90;
  CHECK_FALSE(command.IsCheckpointBoundarySafe(boundary, reason));
  second.checkpointChunks.push_back({suffix, 90, 95, 0, D3D12Chunk::List_Reset});
  second.checkpointChunks.push_back({suffix, 96, 99, 1, D3D12Chunk::List_DrawInstanced});
  CHECK(command.IsCheckpointBoundarySafe(boundary, reason));
  rdcarray<BakedCmdListInfo::CheckpointRecordingChunk> continuation;
  CHECK(command.GetCheckpointContinuation(boundary.offset, boundary.nextEvent, continuation, reason));
  REQUIRE(continuation.size() == 2);
  CHECK(continuation[0].type == D3D12Chunk::List_Reset);
  CHECK(continuation[1].eventAfter == 1);
  second.checkpointChunks.back().type = D3D12Chunk::List_ExecuteIndirect;
  CHECK_FALSE(command.IsCheckpointBoundarySafe(boundary, reason));
  second.checkpointChunks.back().directIndirect = true;
  CHECK(command.IsCheckpointBoundarySafe(boundary, reason));
  second.checkpointChunks.back().directIndirect = false;
  second.checkpointChunks.back().type = D3D12Chunk::List_DiscardResource;
  CHECK_FALSE(IsPreparedReplaySafeChunk(D3D12Chunk::List_DiscardResource));
  CHECK(command.IsCheckpointBoundarySafe(boundary, reason));
  CHECK(command.GetCheckpointContinuation(boundary.offset, boundary.nextEvent, continuation, reason));
  REQUIRE(continuation.size() == 2);
  CHECK(continuation.back().type == D3D12Chunk::List_DiscardResource);
  for(D3D12Chunk chunk : {D3D12Chunk::List_ClearUnorderedAccessViewUint,
                          D3D12Chunk::List_ClearUnorderedAccessViewFloat,
                          D3D12Chunk::List_AtomicCopyBufferUINT,
                          D3D12Chunk::List_AtomicCopyBufferUINT64, D3D12Chunk::List_CopyTiles})
  {
    second.checkpointChunks.back().type = chunk;
    CHECK_FALSE(IsPreparedReplaySafeChunk(chunk));
    CHECK(command.IsCheckpointBoundarySafe(boundary, reason));
  }
  // Collect all rejected types in one pass, deduplicating source offsets across candidate checks.
  second.checkpointChunks.back().type = D3D12Chunk::List_ExecuteIndirect;
  second.checkpointChunks.push_back({suffix, 100, 103, 2, D3D12Chunk::List_ExecuteBundle});
  D3D12CommandData::CheckpointUnsupportedChunks unsupported;
  CHECK_FALSE(command.GetCheckpointContinuation(boundary.offset, boundary.nextEvent, continuation,
                                                 reason, &unsupported));
  CHECK(continuation.empty());
  REQUIRE(unsupported.size() == 2);
  CHECK(unsupported[D3D12Chunk::List_ExecuteIndirect].size() == 1);
  CHECK(unsupported[D3D12Chunk::List_ExecuteBundle].size() == 1);
  CHECK(reason.contains(ToStr(D3D12Chunk::List_ExecuteIndirect)));
  CHECK(reason.contains(ToStr(D3D12Chunk::List_ExecuteBundle)));
  CHECK_FALSE(command.GetCheckpointContinuation(boundary.offset, boundary.nextEvent, continuation,
                                                 reason, &unsupported));
  CHECK(unsupported[D3D12Chunk::List_ExecuteIndirect].size() == 1);
  CHECK(unsupported[D3D12Chunk::List_ExecuteBundle].size() == 1);
  second.checkpointChunks.pop_back();
  second.checkpointChunks.back().type = D3D12Chunk::List_DrawInstanced;
  // A closed but not-yet-submitted list must be reconstructed too.
  second.checkpointEnd = 108;
  second.checkpointChunks.push_back({suffix, 105, 108, 0, D3D12Chunk::List_Close});
  CHECK(command.IsCheckpointBoundarySafe(boundary, reason));
  CHECK(command.GetCheckpointContinuation(boundary.offset, boundary.nextEvent, continuation, reason));
  REQUIRE(continuation.size() == 3);
  CHECK(continuation.back().type == D3D12Chunk::List_Close);
  second.checkpointEnd = 200;
  second.checkpointChunks.pop_back();
  // Never-submitted open lists still need their wrapper/CPU state for later chunks and Close.
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[suffix].clear();
  // Without any execution, even an address-bearing indirect only advances the event counter;
  // the active replay handler cannot enter GPU patching or allocate indirect readback state.
  second.checkpointChunks.back().type = D3D12Chunk::List_ExecuteIndirect;
  CHECK(command.GetCheckpointContinuation(boundary.offset, boundary.nextEvent, continuation, reason));
  CHECK(continuation.size() == 2);
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[suffix].push_back(25);
  second.checkpointChunks.clear();
  second.checkpointBegin = 120;
  // A second submission of a previously recorded list also crosses the boundary.
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[prefix].push_back(60);
  CHECK_FALSE(command.IsCheckpointBoundarySafe(boundary, reason));
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[prefix].pop_back();
  first.checkpointEnd = 0;
  CHECK_FALSE(command.IsCheckpointBoundarySafe(boundary, reason));
  first.checkpointEnd = 100;
  command.m_CheckpointUnsupported = "split barrier";
  CHECK_FALSE(command.IsCheckpointBoundarySafe(boundary, reason));
}

TEST_CASE("D3D12 checkpoints retain the interval before the first dependent native query resolve",
          "[d3d12][replay][checkpoint-query]")
{
  D3D12CommandData command;
  const ResourceId prefix = ResourceIDGen::GetNewUniqueID();
  const ResourceId suffix = ResourceIDGen::GetNewUniqueID();
  const ResourceId heapA = ResourceIDGen::GetNewUniqueID();
  const ResourceId heapB = ResourceIDGen::GetNewUniqueID();
  auto &first = command.m_BakedCmdListInfo[prefix];
  first.checkpointBegin = 10;
  first.checkpointEnd = 100;
  first.eventCount = 20;
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[prefix].push_back(2);
  auto &second = command.m_BakedCmdListInfo[suffix];
  second.checkpointBegin = 120;
  second.checkpointEnd = 200;
  second.eventCount = 30;
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[suffix].push_back(25);
  D3D12CommandData::CheckpointBoundary boundary = {23, 24, 8, 110};
  rdcstr reason;
  REQUIRE(command.IsCheckpointBoundarySafe(boundary, reason));
  CHECK(boundary.firstNativeResolveEID == ~0U);

  // Array/map insertion order must not select a later resolve. A prefix EndQuery is not saved
  // with resource bytes; only a new suffix EndQuery can make its native result self-contained.
  first.checkpointQueries.push_back({heapA, 4, 1, 1, false, D3D12_QUERY_TYPE_TIMESTAMP});
  second.checkpointQueries.push_back({heapA, 4, 1, 9, true, D3D12_QUERY_TYPE_TIMESTAMP});
  second.checkpointQueries.push_back({heapB, 7, 1, 3, true, D3D12_QUERY_TYPE_PIPELINE_STATISTICS});
  REQUIRE(command.IsCheckpointBoundarySafe(boundary, reason));
  CHECK(boundary.firstNativeResolveEID == 28);
  CHECK(boundary.firstNativeResolveType == D3D12_QUERY_TYPE_PIPELINE_STATISTICS);
  CHECK(IsCheckpointQueryTargetSafe(27, boundary.firstNativeResolveEID));
  CHECK_FALSE(IsCheckpointQueryTargetSafe(28, boundary.firstNativeResolveEID));
  CHECK_FALSE(IsCheckpointQueryTargetSafe(29, boundary.firstNativeResolveEID));
  // WithoutDraw may internally end at 27, but a requested EID 28 includes its subsequent
  // OnlyDraw resolve and must use the canonical prefix instead of this checkpoint.
  const uint32_t requestedEvent = 28;
  CHECK(IsCheckpointQueryTargetSafe(requestedEvent - 1, boundary.firstNativeResolveEID));
  CHECK_FALSE(IsCheckpointQueryTargetSafe(requestedEvent, boundary.firstNativeResolveEID));

  second.checkpointQueries.push_back({heapB, 7, 1, 2, false, D3D12_QUERY_TYPE_PIPELINE_STATISTICS});
  REQUIRE(command.IsCheckpointBoundarySafe(boundary, reason));
  CHECK(boundary.firstNativeResolveEID == 34);
  CHECK(boundary.firstNativeResolveType == D3D12_QUERY_TYPE_TIMESTAMP);
  second.checkpointQueries.push_back({heapA, 4, 1, 10, false, D3D12_QUERY_TYPE_TIMESTAMP});
  REQUIRE(command.IsCheckpointBoundarySafe(boundary, reason));
  CHECK(boundary.firstNativeResolveEID == 34);    // an EndQuery after resolve is too late
  second.checkpointQueries.back().relativeEvent = 8;
  REQUIRE(command.IsCheckpointBoundarySafe(boundary, reason));
  CHECK(boundary.firstNativeResolveEID == ~0U);
  CHECK(boundary.firstNativeResolveType == D3D12_QUERY_TYPE_OCCLUSION);
  CHECK(IsCheckpointQueryTargetSafe(~0U, boundary.firstNativeResolveEID));

  second.checkpointQueries[0].count = 2;
  REQUIRE(command.IsCheckpointBoundarySafe(boundary, reason));
  CHECK(boundary.firstNativeResolveEID == 34);    // neighbouring query 5 still depends on prefix
  second.checkpointQueries.push_back({heapA, 5, 1, 8, false, D3D12_QUERY_TYPE_TIMESTAMP});
  REQUIRE(command.IsCheckpointBoundarySafe(boundary, reason));
  CHECK(boundary.firstNativeResolveEID == ~0U);
  // Once both submissions are in the prefix, no future resolve can bound this later checkpoint.
  D3D12CommandData::CheckpointBoundary afterBoth = {56, 57, 10, 210};
  REQUIRE(command.IsCheckpointBoundarySafe(afterBoth, reason));
  CHECK(afterBoth.firstNativeResolveEID == ~0U);
}

TEST_CASE("D3D12 checkpoint continuation decodes only recordings submitted by the target",
          "[d3d12][replay][checkpoint-continuation]")
{
  D3D12CommandData command;
  const ResourceId first = ResourceIDGen::GetNewUniqueID();
  const ResourceId later = ResourceIDGen::GetNewUniqueID();
  const ResourceId repeated = ResourceIDGen::GetNewUniqueID();
  const ResourceId never = ResourceIDGen::GetNewUniqueID();
  const auto addList = [&](ResourceId id, uint64_t begin, bool closed) {
    BakedCmdListInfo &info = command.m_BakedCmdListInfo[id];
    info.checkpointBegin = begin;
    info.checkpointEnd = closed ? begin + 63 : 200;
    info.eventCount = 20;
    info.checkpointChunks.push_back({id, begin, begin + 1, 0, D3D12Chunk::List_Reset});
    info.checkpointChunks.push_back(
        {id, begin + 10, begin + 11, 1, D3D12Chunk::List_SetPipelineState});
    info.checkpointChunks.push_back(
        {id, begin + 20, begin + 21, 7, D3D12Chunk::List_ExecuteIndirect, true});
    if(closed)
      info.checkpointChunks.push_back(
          {id, begin + 60, begin + 63, 0, D3D12Chunk::List_Close});
  };
  // Interleaved source ranges contain two already closed but unsubmitted lists and two open
  // recordings. A repeated list is needed if any of its submissions falls in the target range.
  addList(first, 10, true);
  addList(later, 12, false);
  addList(repeated, 14, true);
  addList(never, 16, false);
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[first] = {100};
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[later] = {300};
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[repeated] = {80, 400};
  const uint64_t offset = 100;
  const uint32_t nextEvent = 50;
  rdcarray<BakedCmdListInfo::CheckpointRecordingChunk> plan;
  rdcstr reason;
  const auto checkPlan = [&](uint32_t target, bool firstFull, bool laterFull, bool repeatedFull,
                             bool neverFull) {
    REQUIRE(command.GetCheckpointContinuation(offset, nextEvent, plan, reason, NULL, target));
    REQUIRE(plan.size() == 14);
    uint64_t previous = 0;
    for(const auto &chunk : plan)
    {
      CHECK(chunk.offset > previous);
      previous = chunk.offset;
      const bool identity =
          chunk.type == D3D12Chunk::List_Reset || chunk.type == D3D12Chunk::List_Close;
      const bool full = chunk.baked == first      ? firstFull
                        : chunk.baked == later    ? laterFull
                        : chunk.baked == repeated ? repeatedFull
                                                  : neverFull;
      CHECK(chunk.metadataOnly == (!identity && !full));
      if(chunk.type == D3D12Chunk::List_ExecuteIndirect)
        CHECK(chunk.eventAfter == 7);
    }
  };
  checkPlan(79, false, false, false, false);   // before the first suffix submission
  checkPlan(80, false, false, true, false);    // exact start, still a partial native recording
  checkPlan(105, true, false, true, false);    // inside the first list
  checkPlan(121, true, false, true, false);    // after the first list
  checkPlan(305, true, true, true, false);     // inside the later open list
  checkPlan(405, true, true, true, false);     // second submission of the same baked list
  checkPlan(~0U, true, true, true, true);      // conservative A/B and boundary safety validation
  // Planning must not modify the loading index or the native query/barrier continuation guards.
  for(const auto &entry : command.m_BakedCmdListInfo)
    for(const auto &chunk : entry.second.checkpointChunks)
      CHECK_FALSE(chunk.metadataOnly);
  command.m_Partial[D3D12CommandData::Secondary].cmdListExecs[never] = {60};
  checkPlan(79, false, false, false, true);    // match Reset's secondary-list safety decision
  command.m_Partial[D3D12CommandData::Secondary].cmdListExecs[never].clear();
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[repeated].push_back(40);
  CHECK_FALSE(command.GetCheckpointContinuation(offset, nextEvent, plan, reason, NULL, 79));
  CHECK(reason.contains("across the checkpoint"));
}

TEST_CASE("D3D12 checkpoint metadata continuation retains identity and final indirect cursors",
          "[d3d12][replay][checkpoint-continuation]")
{
  D3D12CommandData command;
  const ResourceId baked = ResourceIDGen::GetNewUniqueID();
  BakedCmdListInfo::CheckpointRecordingChunk chunk = {
      baked, 10, 20, 7, D3D12Chunk::List_ExecuteIndirect, true, true};
  command.m_BakedCmdListInfo[baked].curEventID = 1;
  command.m_RootEventID = 100;
  REQUIRE(command.ApplyCheckpointContinuationMetadata(chunk));
  CHECK(command.m_BakedCmdListInfo[baked].curEventID == 7);
  CHECK(command.m_LastCmdListID == baked);
  CHECK(command.m_CurChunkOffset == 10);
  CHECK(command.m_LastChunk == D3D12Chunk::List_ExecuteIndirect);
  CHECK(command.m_RootEventID == 100);
  for(D3D12Chunk identity : {D3D12Chunk::List_Reset, D3D12Chunk::List_Close})
  {
    chunk.type = identity;
    CHECK_FALSE(command.ApplyCheckpointContinuationMetadata(chunk));
  }
  chunk.type = D3D12Chunk::List_ExecuteIndirect;
  chunk.eventAfter = 8;
  command.m_RerecordCmds[baked] = NULL;    // existence, not pointer value, drives the real handler
  CHECK_FALSE(command.ApplyCheckpointContinuationMetadata(chunk));
  CHECK(command.m_BakedCmdListInfo[baked].curEventID == 7);
  command.m_RerecordCmds.clear();
  command.m_Partial[D3D12CommandData::Primary].partialParent = baked;
  CHECK_FALSE(command.ApplyCheckpointContinuationMetadata(chunk));
  command.m_Partial[D3D12CommandData::Primary].partialParent = ResourceId();
  chunk.metadataOnly = false;
  CHECK_FALSE(command.ApplyCheckpointContinuationMetadata(chunk));
}

TEST_CASE("D3D12 shared buffer consumers include native roots and conservatively unknown bindings",
          "[d3d12][replay][checkpoint-buffer-patch]")
{
  D3D12CommandData command;
  const ResourceId buffer = ResourceIDGen::GetNewUniqueID();
  const ResourceId other = ResourceIDGen::GetNewUniqueID();
  const ResourceId directList = ResourceIDGen::GetNewUniqueID();
  const ResourceId tableList = ResourceIDGen::GetNewUniqueID();
  auto &direct = command.m_BakedCmdListInfo[directList];
  direct.curEventID = 1;
  command.m_LastCmdListID = directList;
  command.m_CurChunkOffset = 10;
  direct.state.graphics.sigelems.push_back(
      D3D12RenderState::SignatureElement(eRootCBV, buffer, 256));
  command.ObserveCheckpointChunk(D3D12Chunk::List_SetGraphicsRootConstantBufferView, 20);
  CHECK(direct.bufferPatchConsumers.count(buffer) == 1);
  CHECK_FALSE(direct.bufferPatchUnknownConsumers);
  CHECK(command.GetBufferPatchFirstConsumer(buffer) == ~0U);    // recording is not execution
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[directList] = {100, 50};
  CHECK(command.GetBufferPatchFirstConsumer(buffer) == 50);
  CHECK(command.GetBufferPatchFirstConsumer(other) == ~0U);
  direct.state.compute.sigelems.push_back(
      D3D12RenderState::SignatureElement(eRootSRV, other, 0));
  command.ObserveCheckpointChunk(D3D12Chunk::List_SetComputeRootShaderResourceView, 30);
  CHECK(command.GetBufferPatchFirstConsumer(other) == 50);

  auto &table = command.m_BakedCmdListInfo[tableList];
  table.curEventID = 1;
  command.m_LastCmdListID = tableList;
  command.ObserveCheckpointChunk(D3D12Chunk::List_SetGraphicsRootDescriptorTable, 40);
  CHECK(table.bufferPatchUnknownConsumers);
  CHECK(command.GetBufferPatchFirstConsumer(buffer) == 50);    // unsubmitted table does not bind
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[tableList] = {25};
  CHECK(command.GetBufferPatchFirstConsumer(buffer) == 25);
  CHECK(command.GetBufferPatchFirstConsumer(other) == 25);
  command.m_Partial[D3D12CommandData::Secondary].cmdListExecs[directList] = {10};
  CHECK(command.GetBufferPatchFirstConsumer(buffer) == 10);
  command.m_Partial[D3D12CommandData::Secondary].cmdListExecs[directList].clear();
  table.bufferPatchUnknownConsumers = false;
  command.ObserveCheckpointChunk(D3D12Chunk::List_ExecuteIndirect, 50);
  CHECK(command.GetBufferPatchFirstConsumer(buffer) == 25);
  // An unresolved signature may expose directly indexed descriptor storage. It is not evidence
  // that only the explicit root buffers can be consumed.
  table.bufferPatchUnknownConsumers = false;
  table.state.graphics.rootsig = ResourceIDGen::GetNewUniqueID();
  command.ObserveCheckpointChunk(D3D12Chunk::List_SetGraphicsRootSignature, 60);
  CHECK(table.bufferPatchUnknownConsumers);
  command.m_BakedCmdListInfo.erase(tableList);
  CHECK(command.GetBufferPatchFirstConsumer(buffer) == 0);    // missing execution metadata fails closed
}

#endif    // ENABLED(ENABLE_UNIT_TESTS)

template <typename Key>
static void RecordReplayWriteStart(std::set<Key> &writes, std::map<Key, uint32_t> &firstEvents,
                                   const Key &key, uint32_t relativeEvent)
{
  writes.insert(key);
  auto inserted = firstEvents.insert({key, relativeEvent});
  inserted.first->second = RDCMIN(inserted.first->second, relativeEvent);
}

static void RecordUnknownReplayWrite(BakedCmdListInfo &info, uint32_t relativeEvent,
                                     D3D12Chunk chunk)
{
  info.replayUnknownWrites = true;
  if(relativeEvent < info.replayFirstUnknownWriteEvent)
  {
    info.replayFirstUnknownWriteEvent = relativeEvent;
    info.replayUnknownWriteChunk = chunk;
  }
}

static bool AddReplayUAVRange(BakedCmdListInfo &info, ResourceId heap, uint32_t heapCount,
                             uint64_t base, uint64_t offset, uint32_t requestedCount,
                             uint32_t relativeEvent = 0, bool includeDeclared = true)
{
  if(base > heapCount || offset > heapCount - base)
    return false;
  const uint64_t first = base + offset;
  const uint64_t remaining = heapCount - first;
  const uint64_t count = requestedCount == UINT_MAX ? remaining : requestedCount;
  if(count > remaining)
    return false;
  if(count)
  {
    const BakedCmdListInfo::ReplayUAVRange range = {heap, uint32_t(first), uint32_t(count)};
    RecordReplayWriteStart(info.replayUAVRanges, info.replayUAVFirstEvents,
                           range, relativeEvent);
    if(includeDeclared)
      RecordReplayWriteStart(info.replayDeclaredUAVRanges, info.replayDeclaredUAVFirstEvents,
                             range, relativeEvent);
  }
  return true;
}

static bool HasKnownReplayIndirectWrites(const D3D12CommandSignature &signature)
{
  if(signature.raytraced || signature.arguments.empty())
    return false;
  for(const D3D12_INDIRECT_ARGUMENT_DESC &argument : signature.arguments)
  {
    switch(argument.Type)
    {
      case D3D12_INDIRECT_ARGUMENT_TYPE_DRAW:
      case D3D12_INDIRECT_ARGUMENT_TYPE_DRAW_INDEXED:
      case D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH:
      case D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH_MESH:
      case D3D12_INDIRECT_ARGUMENT_TYPE_CONSTANT:
      case D3D12_INDIRECT_ARGUMENT_TYPE_INCREMENTING_CONSTANT:
      case D3D12_INDIRECT_ARGUMENT_TYPE_VERTEX_BUFFER_VIEW:
      case D3D12_INDIRECT_ARGUMENT_TYPE_INDEX_BUFFER_VIEW:
      case D3D12_INDIRECT_ARGUMENT_TYPE_CONSTANT_BUFFER_VIEW:
      case D3D12_INDIRECT_ARGUMENT_TYPE_SHADER_RESOURCE_VIEW: break;
      // Generated read addresses can change branches but cannot introduce new write bindings.
      // Generated root UAV addresses, rays and future argument kinds require a complete reset.
      default: return false;
    }
  }
  return true;
}

struct ReplayWriteBindingStages
{
  bool graphics, compute;
};

static bool UseReplayActiveWriteStages()
{
  // The loading index is fixed for this native process. A/B runs set the opt-out before opening
  // the capture; do not allocate/read environment strings for every draw or dispatch.
  static const bool enabled =
      Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_ACTIVE_WRITE_STAGES") != "1";
  return enabled;
}

static ReplayWriteBindingStages ReplayWriteStages(D3D12Chunk chunk,
                                                   const D3D12CommandSignature *signature = NULL)
{
  if(chunk == D3D12Chunk::List_DrawInstanced || chunk == D3D12Chunk::List_DrawIndexedInstanced ||
     chunk == D3D12Chunk::List_DispatchMesh)
    return {true, false};
  if(chunk == D3D12Chunk::List_Dispatch)
    return {false, true};
  if(chunk != D3D12Chunk::List_ExecuteIndirect || !signature || signature->raytraced ||
     signature->arguments.empty())
    return {true, true};
  ReplayWriteBindingStages stages = {false, false};
  for(const D3D12_INDIRECT_ARGUMENT_DESC &argument : signature->arguments)
  {
    switch(argument.Type)
    {
      case D3D12_INDIRECT_ARGUMENT_TYPE_DRAW:
      case D3D12_INDIRECT_ARGUMENT_TYPE_DRAW_INDEXED:
      case D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH_MESH: stages.graphics = true; break;
      case D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH: stages.compute = true; break;
      case D3D12_INDIRECT_ARGUMENT_TYPE_CONSTANT:
      case D3D12_INDIRECT_ARGUMENT_TYPE_INCREMENTING_CONSTANT:
      case D3D12_INDIRECT_ARGUMENT_TYPE_VERTEX_BUFFER_VIEW:
      case D3D12_INDIRECT_ARGUMENT_TYPE_INDEX_BUFFER_VIEW:
      case D3D12_INDIRECT_ARGUMENT_TYPE_CONSTANT_BUFFER_VIEW:
      case D3D12_INDIRECT_ARGUMENT_TYPE_SHADER_RESOURCE_VIEW:
      case D3D12_INDIRECT_ARGUMENT_TYPE_UNORDERED_ACCESS_VIEW: break;
      default: return {true, true};
    }
  }
  return stages.graphics || stages.compute ? stages : ReplayWriteBindingStages{true, true};
}

static bool ReplayUAVDeclarationBindingsKnown(D3D12Chunk chunk,
                                               const D3D12CommandSignature *signature = NULL)
{
  if(chunk == D3D12Chunk::List_DrawInstanced || chunk == D3D12Chunk::List_DrawIndexedInstanced ||
     chunk == D3D12Chunk::List_Dispatch || chunk == D3D12Chunk::List_DispatchMesh)
    return true;
  if(chunk != D3D12Chunk::List_ExecuteIndirect || !signature ||
     !HasKnownReplayIndirectWrites(*signature))
    return false;
  uint32_t terminals = 0;
  bool graphics = false;
  for(size_t i = 0; i < signature->arguments.size(); i++)
  {
    const D3D12_INDIRECT_ARGUMENT_TYPE type = signature->arguments[i].Type;
    const bool terminal = type == D3D12_INDIRECT_ARGUMENT_TYPE_DRAW ||
                          type == D3D12_INDIRECT_ARGUMENT_TYPE_DRAW_INDEXED ||
                          type == D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH ||
                          type == D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH_MESH;
    if(terminal)
    {
      terminals++;
      graphics = type != D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH;
      if(i + 1 != signature->arguments.size())
        return false;
    }
  }
  // A legal indirect signature has one final operation. Its arguments cannot change the PSO,
  // root-signature object or descriptor tables. CBV/SRV/VB/IB addresses and root constants may
  // change reads/branches/array indices; retaining every declared UAV array element covers them.
  // Generated root UAV addresses, rays and unknown argument kinds were rejected above.
  return terminals == 1 && graphics == signature->graphics;
}

static const uint32_t ReplayTextureReadMetadataLimit = 1024 * 1024;

static bool AddReplayTextureReadRange(BakedCmdListInfo::ReplayTextureReadOp &read, ResourceId heap,
                                      uint32_t heapCount, uint64_t base, uint64_t offset,
                                      uint32_t requestedCount, bool otherDescriptor = false)
{
  auto &ranges = otherDescriptor ? read.otherDescriptorRanges : read.descriptorRanges;
  if(heap == ResourceId() || base > heapCount || offset > heapCount - base)
    return false;
  const uint64_t first = base + offset;
  const uint64_t remaining = heapCount - first;
  const uint64_t count = requestedCount == UINT_MAX ? remaining : requestedCount;
  if(count > remaining || ranges.size() >= ReplayTextureReadMetadataLimit)
    return false;
  if(count)
    ranges.push_back({heap, uint32_t(first), uint32_t(count)});
  return true;
}

static void StoreReplayTextureRead(BakedCmdListInfo &info,
                                   BakedCmdListInfo::ReplayTextureReadOp &&read)
{
  if(!info.replayTextureReadsComplete)
    return;
  const uint64_t entries = 1 + read.directResources.size() + read.descriptorRanges.size() +
                           read.otherDescriptorRanges.size();
  if(info.replayTextureReadEntries > ReplayTextureReadMetadataLimit ||
     entries > ReplayTextureReadMetadataLimit - info.replayTextureReadEntries)
  {
    info.replayTextureReadsComplete = false;
    info.replayTextureReadOps.clear();
    info.replayTextureReadEntries = 0;
    return;
  }
  info.replayTextureReadEntries += uint32_t(entries);
  info.replayTextureReadOps.push_back(std::move(read));
}

template <typename DescriptorQuery>
static bool ReplayTextureReadMayReference(const BakedCmdListInfo::ReplayTextureReadOp &read,
                                          ResourceId target, DescriptorQuery descriptorQuery)
{
  if(read.unknown || target == ResourceId() || read.directResources.contains(target))
    return true;
  for(const auto &range : read.descriptorRanges)
    if(descriptorQuery(range, target))
      return true;
  return false;
}

static bool UseReplayUAVDeclarations()
{
  static const bool enabled =
      Process::GetEnvVariable("RENDERDOC_D3D12_ENABLE_UAV_DECLARATION_RANGES") == "1";
  return enabled;
}

using ReplayShaderUAVBinding = D3D12CommandData::ReplayShaderUAVBinding;
using ReplayPSOUAVProof = D3D12CommandData::ReplayPSOUAVProof;

static bool ReplayFiniteUAVBinding(uint32_t first, uint32_t count)
{
  return count != 0 && count != UINT_MAX && uint64_t(first) + count <= uint64_t(UINT_MAX) + 1;
}

// Validate the complete executable token walk before accepting declaration absence. An empty
// reflection object alone also represents a failed parser, and is never a negative write proof.
static bool ReplayDXBCTokenDeclarations(const rdcarray<uint32_t> &tokens, uint32_t &uavCount)
{
  uavCount = 0;
  if(tokens.size() < 3 || tokens[1] != tokens.size())
    return false;
  for(size_t offset = 2; offset < tokens.size();)
  {
    const uint32_t opcode = tokens[offset] & 0x7ff;
    if(opcode >= DXBCBytecode::NUM_REAL_OPCODES || opcode == DXBCBytecode::OPCODE_RESERVED0 ||
       opcode == DXBCBytecode::OPCODE_RESERVED1 || opcode == DXBCBytecode::OPCODE_RESERVED2 ||
       opcode == DXBCBytecode::OPCODE_RESERVED3 ||
       opcode == DXBCBytecode::OPCODE_DCL_INTERFACE ||
       opcode == DXBCBytecode::OPCODE_DCL_FUNCTION_BODY ||
       opcode == DXBCBytecode::OPCODE_DCL_FUNCTION_TABLE)
      return false;
    uint32_t length = (tokens[offset] >> 24) & 0x7f;
    if(opcode == DXBCBytecode::OPCODE_CUSTOMDATA)
    {
      if(offset + 1 >= tokens.size())
        return false;
      length = tokens[offset + 1];
      if(length < 2)
        return false;
    }
    if(!length || length > tokens.size() - offset)
      return false;
    if(opcode == DXBCBytecode::OPCODE_DCL_UNORDERED_ACCESS_VIEW_RAW ||
       opcode == DXBCBytecode::OPCODE_DCL_UNORDERED_ACCESS_VIEW_STRUCTURED ||
       opcode == DXBCBytecode::OPCODE_DCL_UNORDERED_ACCESS_VIEW_TYPED)
      uavCount++;
    offset += length;
  }
  return true;
}

static bool ReplayDXILMetadataUint(const DXIL::Metadata *metadata, uint32_t &value)
{
  uint64_t wide = 0;
  if(!metadata || !metadata->isConstant || !metadata->value ||
     !DXIL::getival(metadata->value, wide) || wide > UINT_MAX)
    return false;
  value = uint32_t(wide);
  return true;
}

struct ReplayOriginalExecutable
{
  const byte *bytes = NULL;
  size_t length = 0;
  uint32_t fourcc = 0;
  uint64_t featureFlags = 0;
  bool featureInfoPresent = false;
};

static bool ReplaySM66FeatureInfoKnown(const ReplayOriginalExecutable &original)
{
  const uint64_t heapFlags = uint64_t(DXBC::GlobalShaderFlags::ResourceDescriptorHeapIndexing) |
                             uint64_t(DXBC::GlobalShaderFlags::SamplerDescriptorHeapIndexing) |
                             uint64_t(DXBC::GlobalShaderFlags::AtomicInt64OnHeapResource);
  return original.featureInfoPresent && !(original.featureFlags & heapFlags);
}

static uint32_t ReplayShaderWord(const byte *bytes, size_t offset)
{
  uint32_t value;
  memcpy(&value, bytes + offset, sizeof(value));
  return value;
}

// This takes GetDesc's original PSO input only. DXBCContainer is intentionally not involved:
// its debug search/ILDB selection may substitute a different program, even without a HASH.
static bool ReplayFindOriginalExecutable(const byte *bytes, size_t length,
                                         ReplayOriginalExecutable &executable)
{
  executable = {};
  if(!bytes || length < 32 || length > UINT_MAX ||
     ReplayShaderWord(bytes, 0) != DXBC::FOURCC_DXBC || ReplayShaderWord(bytes, 20) != 1 ||
     ReplayShaderWord(bytes, 24) != length)
    return false;
  const uint32_t count = ReplayShaderWord(bytes, 28);
  if(!count || count > (length - 32) / 4)
    return false;
  const size_t tableEnd = 32 + size_t(count) * 4;
  rdcarray<rdcpair<size_t, size_t>> occupied;
  for(uint32_t i = 0; i < count; i++)
  {
    const size_t offset = ReplayShaderWord(bytes, 32 + size_t(i) * 4);
    if(offset < tableEnd || (offset & 3) || offset > length || length - offset < 8)
      return false;
    const uint32_t fourcc = ReplayShaderWord(bytes, offset);
    const size_t payloadLength = ReplayShaderWord(bytes, offset + 4);
    if(payloadLength > length - offset - 8)
      return false;
    const size_t end = offset + 8 + payloadLength;
    for(const auto &previous : occupied)
      if(offset < previous.second && previous.first < end)
        return false;
    occupied.push_back({offset, end});
    if(fourcc == DXBC::FOURCC_SHEX || fourcc == DXBC::FOURCC_SHDR ||
       fourcc == DXBC::FOURCC_DXIL)
    {
      // Reject ambiguous duplicates rather than duplicating the runtime's precedence rules.
      if(executable.bytes || !payloadLength)
        return false;
      executable.bytes = bytes + offset + 8;
      executable.length = payloadLength;
      executable.fourcc = fourcc;
    }
    if(fourcc == DXBC::FOURCC_SFI0)
    {
      if(executable.featureInfoPresent || payloadLength != sizeof(uint64_t))
        return false;
      executable.featureInfoPresent = true;
      executable.featureFlags = uint64_t(ReplayShaderWord(bytes, offset + 8)) |
                                (uint64_t(ReplayShaderWord(bytes, offset + 12)) << 32);
    }
  }
  return executable.bytes != NULL;
}

static const char *ReplayOriginalUAVDeclarations(const byte *bytes, size_t length, ShaderStage stage,
                                                 rdcarray<ReplayShaderUAVBinding> &bindings,
                                                 D3D12CommandData::ReplayPSOUAVProof *cbProof = NULL)
{
  ReplayOriginalExecutable original;
  if(!ReplayFindOriginalExecutable(bytes, length, original))
    return "missingOrAmbiguousOriginalExecutable";
  rdcarray<ReplayShaderUAVBinding> declared;
  if(original.fourcc == DXBC::FOURCC_SHDR || original.fourcc == DXBC::FOURCC_SHEX)
  {
    if(original.length % sizeof(uint32_t) != 0)
      return "unalignedOriginalDXBCTokens";
    DXBCBytecode::Program program(original.bytes, original.length);
    uint32_t declarations = 0;
    if(program.GetMajorVersion() != 5 || program.GetMinorVersion() > 1 ||
       uint32_t(program.GetShaderType()) > uint32_t(DXBC::ShaderType::Compute) ||
       DXBC::GetShaderStage(program.GetShaderType()) != stage ||
       !ReplayDXBCTokenDeclarations(program.GetTokens(), declarations))
      return "unsupportedDXBCTokens";
    DXBC::Reflection *executable = program.GuessReflection();
    if(!executable)
      return "missingDXBCDeclarations";
    const bool complete = executable->UAVs.size() == declarations;
    if(cbProof)
    {
      uint32_t cbDeclarations = 0;
      for(size_t i = 0; i < program.GetNumDeclarations(); i++)
        cbDeclarations +=
            program.GetDeclaration(i).declaration == DXBCBytecode::OPCODE_DCL_CONSTANT_BUFFER;
      cbProof->constantBuffersComplete = cbDeclarations == executable->CBuffers.size();
      for(const auto &cb : executable->CBuffers)
      {
        if(!cb.descriptor.byteSize || cb.descriptor.byteSize > 65536 ||
           !ReplayFiniteUAVBinding(cb.reg, cb.bindCount))
          cbProof->constantBuffersComplete = false;
        cbProof->constantBuffers.push_back(
            {stage, cb.space, cb.reg, cb.bindCount, cb.descriptor.byteSize});
      }
    }
    for(const DXBC::ShaderInputBind &binding : executable->UAVs)
      declared.push_back({stage, binding.space, binding.reg, binding.bindCount});
    delete executable;
    if(!complete)
      return "incompleteDXBCDeclarations";
  }
  else if(original.fourcc == DXBC::FOURCC_DXIL)
  {
    if(!DXIL::Program::Valid(original.bytes, original.length))
      return "invalidOriginalDXILProgram";
    DXIL::Program dxilProgram(original.bytes, original.length);
    if(dxilProgram.GetMajorVersion() == 6 && dxilProgram.GetMinorVersion() == 6)
    {
      // Flags are from the same original container as the executable, never an external debug
      // blob. They are an additional veto; the complete call/handle-origin proof is still required.
      if(!ReplaySM66FeatureInfoKnown(original))
        return "sm66MissingOrHeapFeatureInfo";
      rdcarray<DXIL::ReplayRootBinding> rootBindings;
      const char *reason = dxilProgram.GetReplayFiniteRootBindings(rootBindings);
      if(reason)
        return reason;
      for(const DXIL::ReplayRootBinding &binding : rootBindings)
        if(binding.resourceClass == DXIL::ResourceClass::UAV)
          declared.push_back({stage, binding.space, binding.first, binding.count});
      if(DXBC::GetShaderStage(dxilProgram.GetShaderType()) != stage)
        return "sm66PipelineStageMismatch";
    }
    else
    {
      // Preserve the existing SM6.0-6.5 eligibility and metadata checks. SM6.7+ remains unsupported.
      if(dxilProgram.GetMajorVersion() != 6 || dxilProgram.GetMinorVersion() > 5 ||
         uint32_t(dxilProgram.GetShaderType()) > uint32_t(DXBC::ShaderType::Compute) ||
         DXBC::GetShaderStage(dxilProgram.GetShaderType()) != stage ||
         !dxilProgram.GetDebugStatus().empty() || dxilProgram.GetDirectHeapAcessCount() != 0)
        return "unsupportedDXILModelOrOperation";
      const DXIL::Metadata *entries = dxilProgram.GetMetadataByName("dx.entryPoints");
      if(!entries || entries->children.size() != 1 || !entries->children[0] ||
         entries->children[0]->children.size() < 5 || !entries->children[0]->children[0])
        return "incompleteDXILEntryPoint";
      const DXIL::Metadata *resources = entries->children[0]->children[3];
      const DXIL::Metadata *global = dxilProgram.GetMetadataByName("dx.resources");
      if((resources && (!global || global->children.size() != 1 ||
                        global->children[0] != resources || resources->children.size() != 4)) ||
         (!resources && global && (global->children.size() != 1 || global->children[0])))
        return "incompleteDXILResources";
      const DXIL::Metadata *uavs = resources ? resources->children[1] : NULL;
      if(uavs)
        for(const DXIL::Metadata *uav : uavs->children)
        {
          ReplayShaderUAVBinding binding;
          binding.stage = stage;
          if(!uav || uav->children.size() <= size_t(DXIL::ResField::UAVTags) ||
             !ReplayDXILMetadataUint(uav->children[size_t(DXIL::ResField::Space)], binding.space) ||
             !ReplayDXILMetadataUint(uav->children[size_t(DXIL::ResField::RegBase)], binding.first) ||
             !ReplayDXILMetadataUint(uav->children[size_t(DXIL::ResField::RegCount)], binding.count))
            return "incompleteDXILUAVDeclaration";
          declared.push_back(binding);
        }
    }
    if(cbProof)
    {
      const auto *entry = dxilProgram.GetEntryPointInterface();
      cbProof->constantBuffersComplete = entry != NULL;
      if(entry)
        for(const auto &cb : entry->cbuffers)
        {
          if(!cb.cbufferData.sizeInBytes || cb.cbufferData.sizeInBytes > 65536 ||
             !ReplayFiniteUAVBinding(cb.regBase, cb.regCount))
            cbProof->constantBuffersComplete = false;
          cbProof->constantBuffers.push_back(
              {stage, cb.space, cb.regBase, cb.regCount, cb.cbufferData.sizeInBytes});
        }
    }
  }
  else
  {
    return "missingExecutableProgram";
  }

  for(const ReplayShaderUAVBinding &binding : declared)
    if(!ReplayFiniteUAVBinding(binding.first, binding.count))
      return "unboundedOrInvalidDeclaration";
  bindings.append(declared);
  return NULL;
}

static bool ReplayUAVReflectionMatches(const ShaderReflection &reflection, ShaderStage stage,
                                        const rdcarray<ReplayShaderUAVBinding> &declared)
{
  if(reflection.stage != stage || reflection.readWriteResources.size() != declared.size())
    return false;
  for(const ShaderResource &resource : reflection.readWriteResources)
  {
    bool matched = false;
    for(const ReplayShaderUAVBinding &binding : declared)
      matched |= binding.space == resource.fixedBindSetOrSpace &&
                 binding.first == resource.fixedBindNumber && binding.count == resource.bindArraySize;
    if(!matched)
      return false;
  }
  return true;
}

static const char *ReplayShaderUAVDeclarations(WrappedID3D12Shader *shader, ShaderStage stage,
                                               rdcarray<ReplayShaderUAVBinding> &bindings,
                                               D3D12CommandData::ReplayPSOUAVProof *cbProof = NULL)
{
  uint32_t vendorSlot = ~0U, vendorSpace = ~0U;
  shader->GetShaderExtSlot(vendorSlot, vendorSpace);
  if(vendorSlot != ~0U || vendorSpace != ~0U)
    return "vendorExtension";
  const D3D12_SHADER_BYTECODE original = shader->GetDesc();
  rdcarray<ReplayShaderUAVBinding> declared;
  const char *reason = ReplayOriginalUAVDeclarations(
      (const byte *)original.pShaderBytecode, original.BytecodeLength, stage, declared, cbProof);
  if(reason)
    return reason;
  // Reflection is an additional veto only, never the source of a negative write proof. A debug
  // blob with missing/different UAVs cannot shrink the original executable's declaration set.
  if(!ReplayUAVReflectionMatches(shader->GetDetails(), stage, declared))
    return "reflectionDeclarationMismatch";
  bindings.append(declared);
  return NULL;
}

const D3D12CommandData::ReplayPSOUAVProof &D3D12CommandData::GetReplayPSOUAVProof(ResourceId pipeline)
{
  auto found = m_ReplayPSOUAVProofs.find(pipeline);
  if(found != m_ReplayPSOUAVProofs.end())
    return found->second;
  ReplayPSOUAVProof &proof = m_ReplayPSOUAVProofs[pipeline];
  D3D12ResourceManager *rm = m_pDevice ? m_pDevice->GetResourceManager() : NULL;
  WrappedID3D12PipelineState *pipe =
      rm && pipeline != ResourceId() ? rm->GetResAs<WrappedID3D12PipelineState>(pipeline) : NULL;
  if(!pipe || rm->HasReplacement(pipeline))
    proof.reason = "missingOrReplacedPipeline";
  else
  {
    WrappedID3D12Shader *shaders[NumShaderStages] = {};
    proof.graphics = pipe->IsGraphics();
    if(pipe->IsCompute())
      shaders[uint32_t(ShaderStage::Compute)] = pipe->CS();
    else if(pipe->IsGraphics())
    {
      shaders[uint32_t(ShaderStage::Vertex)] = pipe->VS();
      shaders[uint32_t(ShaderStage::Hull)] = pipe->HS();
      shaders[uint32_t(ShaderStage::Domain)] = pipe->DS();
      shaders[uint32_t(ShaderStage::Geometry)] = pipe->GS();
      shaders[uint32_t(ShaderStage::Pixel)] = pipe->PS();
      shaders[uint32_t(ShaderStage::Amplification)] = pipe->AS();
      shaders[uint32_t(ShaderStage::Mesh)] = pipe->MS();
    }
    proof.reason = "missingShader";
    proof.constantBuffersComplete = true;
    bool any = false;
    for(uint32_t stage = 0; stage < NumShaderStages; stage++)
    {
      if(!shaders[stage])
        continue;
      any = true;
      const ResourceId shaderID = shaders[stage]->GetResourceID();
      if(rm->HasReplacement(shaderID))
      {
        proof.reason = "replacedShader";
        break;
      }
      // Raw executable parsing is deliberately independent of the debug container cache. Reuse
      // this immutable result across PSOs sharing one shader instead of parsing it per PSO.
      const rdcpair<ResourceId, ShaderStage> key = {shaderID, ShaderStage(stage)};
      auto cached = m_ReplayShaderUAVProofs.find(key);
      if(cached == m_ReplayShaderUAVProofs.end())
      {
        ReplayPSOUAVProof &shaderProof = m_ReplayShaderUAVProofs[key];
        shaderProof.reason = ReplayShaderUAVDeclarations(shaders[stage], ShaderStage(stage),
                                                         shaderProof.bindings, &shaderProof);
        shaderProof.complete = shaderProof.reason == NULL;
        cached = m_ReplayShaderUAVProofs.find(key);
      }
      proof.reason = cached->second.reason;
      if(proof.reason)
        break;
      proof.bindings.append(cached->second.bindings);
      proof.constantBuffersComplete &= cached->second.constantBuffersComplete;
      proof.constantBuffers.append(cached->second.constantBuffers);
    }
    proof.complete = any && proof.reason == NULL;
    if(proof.complete)
      proof.reason = "completeExecutableDeclarations";
  }
  // Proofs are built while loading, before prepared-replay timing is enabled. The cache lookup
  // above still makes this exactly one diagnostic per PSO, including loading-time fallbacks.
  if(m_PreparedTimingEnabled || Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
  {
    const rdcstr line = StringFormat::Fmt(
        "api=D3D12UAVDeclarationPSO pipeline=%s complete=%u graphics=%u ranges=%zu reason=%s",
        ToStr(pipeline).c_str(), proof.complete ? 1U : 0U, proof.graphics ? 1U : 0U,
        proof.bindings.size(), proof.reason);
    RDCLOG("%s", line.c_str());
    AppendReplayTimingLog(line);
  }
  return proof;
}

static bool ReplayUAVVisibilityMatches(D3D12_SHADER_VISIBILITY visibility, ShaderStage stage)
{
  if(visibility == D3D12_SHADER_VISIBILITY_ALL)
    return true;
  switch(stage)
  {
    case ShaderStage::Vertex: return visibility == D3D12_SHADER_VISIBILITY_VERTEX;
    case ShaderStage::Hull: return visibility == D3D12_SHADER_VISIBILITY_HULL;
    case ShaderStage::Domain: return visibility == D3D12_SHADER_VISIBILITY_DOMAIN;
    case ShaderStage::Geometry: return visibility == D3D12_SHADER_VISIBILITY_GEOMETRY;
    case ShaderStage::Pixel: return visibility == D3D12_SHADER_VISIBILITY_PIXEL;
    case ShaderStage::Amplification: return visibility == D3D12_SHADER_VISIBILITY_AMPLIFICATION;
    case ShaderStage::Mesh: return visibility == D3D12_SHADER_VISIBILITY_MESH;
    default: return false;
  }
}

// Output offsets are relative to this one UAV range, not the whole descriptor table. Keep every
// element of a declared array: constants, branches and dynamic array indices do not narrow it.
static bool ReplayUAVTableDeclarationRanges(const ReplayPSOUAVProof *proof, bool graphics,
                                            bool bindingsKnown, D3D12_ROOT_SIGNATURE_FLAGS flags,
                                            D3D12_SHADER_VISIBILITY visibility,
                                            const D3D12_DESCRIPTOR_RANGE1 &range,
                                            rdcarray<rdcpair<uint32_t, uint32_t>> &selected)
{
  selected.clear();
  if(!proof || !proof->complete || proof->graphics != graphics || !bindingsKnown ||
     (flags & (D3D12_ROOT_SIGNATURE_FLAG_CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED |
               D3D12_ROOT_SIGNATURE_FLAG_SAMPLER_HEAP_DIRECTLY_INDEXED)) ||
     uint32_t(visibility) > uint32_t(D3D12_SHADER_VISIBILITY_MESH) ||
     range.RangeType != D3D12_DESCRIPTOR_RANGE_TYPE_UAV ||
     !ReplayFiniteUAVBinding(range.BaseShaderRegister, range.NumDescriptors))
    return false;
  const uint64_t end = uint64_t(range.BaseShaderRegister) + range.NumDescriptors;
  for(const ReplayShaderUAVBinding &binding : proof->bindings)
  {
    if(!ReplayFiniteUAVBinding(binding.first, binding.count))
      return false;
    if(binding.space != range.RegisterSpace || !ReplayUAVVisibilityMatches(visibility, binding.stage))
      continue;
    const uint64_t first = RDCMAX(uint64_t(range.BaseShaderRegister), uint64_t(binding.first));
    const uint64_t last = RDCMIN(end, uint64_t(binding.first) + binding.count);
    if(first < last)
      selected.push_back({uint32_t(first - range.BaseShaderRegister), uint32_t(last - first)});
  }
  std::sort(selected.begin(), selected.end());
  for(size_t i = 1; i < selected.size();)
  {
    const uint64_t previousEnd = uint64_t(selected[i - 1].first) + selected[i - 1].second;
    if(selected[i].first <= previousEnd)
    {
      selected[i - 1].second = uint32_t(
          RDCMAX(previousEnd, uint64_t(selected[i].first) + selected[i].second) - selected[i - 1].first);
      selected.erase(i);
    }
    else
      i++;
  }
  return true;
}

void D3D12CommandData::ObserveReplayWriteBindings(BakedCmdListInfo &info,
                                                const D3D12RenderState &state,
                                                uint32_t relativeEvent, bool graphics, bool compute,
                                                bool declarationBindingsKnown)
{
  BakedCmdListInfo::ReplayTextureReadOp textureRead;
  textureRead.relativeEvent = relativeEvent;
  textureRead.unknown = !graphics && !compute;
  const bool declarationsEnabled = UseReplayUAVDeclarations();
  const ReplayPSOUAVProof *declarations =
      declarationsEnabled && graphics != compute && declarationBindingsKnown
          ? &GetReplayPSOUAVProof(state.pipe) : NULL;
  // Draw and dispatch bindings coexist in D3D12 state. Only the selected pipeline executes;
  // stale graphics outputs on a dispatch, or stale compute UAVs on a draw, do not write bytes.
  if(graphics)
  {
    for(ResourceId target : state.GetRTVIDs())
      if(target != ResourceId())
      {
        textureRead.directResources.push_back(target);
        RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, target, relativeEvent);
      }
    if(state.GetDSVID() != ResourceId())
    {
      textureRead.directResources.push_back(state.GetDSVID());
      RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, state.GetDSVID(),
                             relativeEvent);
    }
    if(state.shadingRateImage != ResourceId())
      textureRead.directResources.push_back(state.shadingRateImage);
    for(const auto &output : state.streamouts)
    {
      if(output.buf != ResourceId())
        RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, output.buf, relativeEvent);
      if(output.countbuf != ResourceId())
        RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, output.countbuf,
                               relativeEvent);
    }
  }

  for(const auto *root : {&state.graphics, &state.compute})
  {
    if((root == &state.graphics && !graphics) || (root == &state.compute && !compute))
      continue;
    if(root->rootsig == ResourceId())
    {
      textureRead.unknown = true;
      continue;
    }
    WrappedID3D12RootSignature *signature =
        m_pDevice ? m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12RootSignature>(
                        root->rootsig) : NULL;
    if(!signature)
    {
      textureRead.unknown = true;
      info.replayUnknownWrites = true;
      continue;
    }
    if(signature->sig.Flags & D3D12_ROOT_SIGNATURE_FLAG_CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED)
    {
      bool resourceHeap = false;
      for(ResourceId heapID : state.heaps)
      {
        WrappedID3D12DescriptorHeap *heap =
            m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12DescriptorHeap>(heapID);
        if(!heap)
        {
          textureRead.unknown = true;
          info.replayUnknownWrites = true;
        }
        else if(heap->GetDesc().Type == D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV)
        {
          resourceHeap = true;
          textureRead.unknown |= !AddReplayTextureReadRange(
              textureRead, heapID, heap->GetNumDescriptors(), 0, 0, UINT_MAX);
          AddReplayUAVRange(info, heapID, heap->GetNumDescriptors(), 0, 0, UINT_MAX,
                            relativeEvent, declarationsEnabled);
          if(declarationsEnabled)
          {
            info.replayUAVDeclarationFallbacks++;
            info.replayUAVSlotsBefore += heap->GetNumDescriptors();
            info.replayUAVSlotsAfter += heap->GetNumDescriptors();
          }
        }
      }
      textureRead.unknown |= !resourceHeap;
    }
    if(signature->sig.Flags & D3D12_ROOT_SIGNATURE_FLAG_SAMPLER_HEAP_DIRECTLY_INDEXED)
    {
      bool samplerHeap = false;
      for(ResourceId heapID : state.heaps)
      {
        WrappedID3D12DescriptorHeap *heap =
            m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12DescriptorHeap>(heapID);
        if(!heap)
          textureRead.unknownDescriptorUses = true;
        else if(heap->GetDesc().Type == D3D12_DESCRIPTOR_HEAP_TYPE_SAMPLER)
        {
          samplerHeap = true;
          textureRead.unknownDescriptorUses |= !AddReplayTextureReadRange(
              textureRead, heapID, heap->GetNumDescriptors(), 0, 0, UINT_MAX, true);
        }
      }
      textureRead.unknownDescriptorUses |= !samplerHeap;
    }
    for(size_t i = 0; i < signature->sig.Parameters.size(); i++)
    {
      const auto &parameter = signature->sig.Parameters[i];
      bool textureTable = false;
      if(parameter.ParameterType == D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE)
        for(const D3D12_DESCRIPTOR_RANGE1 &range : parameter.ranges)
          textureTable |= range.RangeType == D3D12_DESCRIPTOR_RANGE_TYPE_SRV ||
                          range.RangeType == D3D12_DESCRIPTOR_RANGE_TYPE_UAV;
      if(i >= root->sigelems.size())
      {
        textureRead.unknown |= textureTable;
        textureRead.unknownDescriptorUses |=
            parameter.ParameterType == D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
        continue;
      }
      const auto &element = root->sigelems[i];
      if(parameter.ParameterType == D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE &&
         (element.type != eRootTable || !state.heaps.contains(element.id)))
      {
        textureRead.unknown |= textureTable;
        textureRead.unknownDescriptorUses = true;
      }
      if(parameter.ParameterType == D3D12_ROOT_PARAMETER_TYPE_UAV && element.type == eRootUAV)
      {
        if(element.id != ResourceId())
          RecordReplayWriteStart(info.replayRootUAVWrites, info.replayRootUAVFirstEvents, element.id,
                                 relativeEvent);
      }
      else if(parameter.ParameterType == D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE &&
              element.type == eRootTable)
      {
        WrappedID3D12DescriptorHeap *heap =
            m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12DescriptorHeap>(element.id);
        if(!heap)
        {
          textureRead.unknown |= textureTable;
          textureRead.unknownDescriptorUses = true;
          info.replayUnknownWrites = true;
          continue;
        }
        if(textureTable && heap->GetDesc().Type != D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV)
          textureRead.unknown = true;
        uint64_t previousEnd = 0;
        for(const D3D12_DESCRIPTOR_RANGE1 &range : parameter.ranges)
        {
          uint64_t offset = range.OffsetInDescriptorsFromTableStart;
          if(offset == D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND)
            offset = previousEnd;
          const uint64_t remaining = element.offset <= heap->GetNumDescriptors() &&
                                            offset <= heap->GetNumDescriptors() - element.offset
                                         ? heap->GetNumDescriptors() - element.offset - offset : 0;
          const uint64_t count = range.NumDescriptors == UINT_MAX ? remaining : range.NumDescriptors;
          previousEnd = offset + count;
          if(range.RangeType == D3D12_DESCRIPTOR_RANGE_TYPE_SRV ||
             range.RangeType == D3D12_DESCRIPTOR_RANGE_TYPE_UAV)
            textureRead.unknown |= !AddReplayTextureReadRange(
                textureRead, element.id, heap->GetNumDescriptors(), element.offset, offset,
                range.NumDescriptors);
          else
          {
            const bool sampler = range.RangeType == D3D12_DESCRIPTOR_RANGE_TYPE_SAMPLER;
            const bool cbv = range.RangeType == D3D12_DESCRIPTOR_RANGE_TYPE_CBV;
            const bool validHeap =
                heap->GetDesc().Type == (sampler ? D3D12_DESCRIPTOR_HEAP_TYPE_SAMPLER
                                                 : D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
            textureRead.unknownDescriptorUses |=
                !(sampler || cbv) || !validHeap ||
                !AddReplayTextureReadRange(textureRead, element.id, heap->GetNumDescriptors(),
                                           element.offset, offset, range.NumDescriptors, true);
          }
          if(range.RangeType != D3D12_DESCRIPTOR_RANGE_TYPE_UAV)
            continue;
          rdcarray<rdcpair<uint32_t, uint32_t>> selected;
          const bool narrowed =
              ReplayUAVTableDeclarationRanges(declarations, root == &state.graphics,
                                               declarationBindingsKnown,
                                               signature->sig.Flags, parameter.ShaderVisibility,
                                               range, selected) &&
              heap->GetDesc().Type == D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV &&
              state.heaps.contains(element.id);
          if(!AddReplayUAVRange(info, element.id, heap->GetNumDescriptors(), element.offset,
                                offset, range.NumDescriptors, relativeEvent,
                                declarationsEnabled && !narrowed))
          {
            info.replayUnknownWrites = true;
            continue;
          }
          if(declarationsEnabled)
          {
            info.replayUAVSlotsBefore += count;
            if(narrowed)
            {
              info.replayUAVDeclarationHits++;
              for(const auto &subrange : selected)
              {
                // The full table range passed native heap bounds checks above. Register
                // intersection offsets are bounded by that range, including explicit/APPEND.
                const BakedCmdListInfo::ReplayUAVRange declared = {
                    element.id, uint32_t(element.offset + offset + subrange.first), subrange.second};
                RecordReplayWriteStart(info.replayDeclaredUAVRanges,
                                       info.replayDeclaredUAVFirstEvents, declared, relativeEvent);
                info.replayUAVSlotsAfter += subrange.second;
              }
            }
            else
            {
              info.replayUAVDeclarationFallbacks++;
              info.replayUAVSlotsAfter += count;
            }
          }
        }
      }
    }
  }
  StoreReplayTextureRead(info, std::move(textureRead));
}

static void GatherReplayEventUsage(const D3D12EventNode &node, uint32_t event, BakedCmdListInfo &info)
{
  for(const auto &use : node.resourceUsage)
  {
    const ResourceUsage usage = use.second;
    if(usage == ResourceUsage::CopySrc || usage == ResourceUsage::Copy)
      info.bufferPatchCopyReads[event].insert(use.first);
    if((usage >= ResourceUsage::VS_RWResource && usage <= ResourceUsage::All_RWResource) ||
       usage == ResourceUsage::CopyDst || usage == ResourceUsage::Copy ||
       usage == ResourceUsage::ResolveDst || usage == ResourceUsage::ResolveSrc ||
       usage == ResourceUsage::Resolve ||
       usage == ResourceUsage::Clear || usage == ResourceUsage::Discard ||
       usage == ResourceUsage::ColorTarget || usage == ResourceUsage::DepthStencilTarget ||
       usage == ResourceUsage::StreamOut)
      RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, use.first, event);
  }
}

void D3D12CommandData::BuildReplayWriteTracking()
{
  for(auto &entry : m_BakedCmdListInfo)
  {
    BakedCmdListInfo &info = entry.second;
    if(!info.checkpointChunks.empty() && info.checkpointEnd)
    {
      // Explicit handlers append Copy/Clear/Resolve usages after AddAction. Read the final
      // command-list-local events before the loading nodes are discarded.
      for(uint32_t event = 0; event < info.eventNodes.size(); ++event)
        GatherReplayEventUsage(info.eventNodes[event], event, info);
      for(const auto &barrier : info.checkpointBarriers)
      {
        if(barrier.type != D3D12_RESOURCE_BARRIER_TYPE_ALIASING)
          continue;
        if(barrier.before == ResourceId() && barrier.after == ResourceId())
        {
          RecordUnknownReplayWrite(info, barrier.relativeEvent, D3D12Chunk::List_ResourceBarrier);
        }
        else
        {
          if(barrier.before != ResourceId())
            RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, barrier.before,
                                   barrier.relativeEvent);
          if(barrier.after != ResourceId())
            RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, barrier.after,
                                   barrier.relativeEvent);
        }
      }
      info.replayWritesKnown = true;
      // Missing legacy/index metadata cannot turn a previously unknown list into a known prefix.
      info.replayWriteEventsKnown =
          info.replayWriteFirstEvents.size() == info.replayWrites.size() &&
          info.replayRootUAVFirstEvents.size() == info.replayRootUAVWrites.size() &&
          info.replayUAVFirstEvents.size() == info.replayUAVRanges.size() &&
          info.replayDeclaredUAVFirstEvents.size() == info.replayDeclaredUAVRanges.size() &&
          (!info.replayUnknownWrites || info.replayFirstUnknownWriteEvent != ~0U);
    }
  }
}

struct ReplayWriteCutoff
{
  uint32_t last = ~0U;
  bool indexed = false;
  bool unknown = false;
  const char *fallback = "none";
};

static ReplayWriteCutoff SelectReplayWriteCutoff(const BakedCmdListInfo &info, bool enabled,
                                                bool knownOccurrence, uint32_t baseEvent,
                                                uint32_t lastEvent)
{
  ReplayWriteCutoff cutoff;
  cutoff.unknown = info.replayUnknownWrites;
  if(!enabled)
    cutoff.fallback = "disabled";
  else if(!info.replayWriteEventsKnown)
    cutoff.fallback = "missingWriteIndex";
  else if(!knownOccurrence || !info.eventCount || baseEvent == ~0U || baseEvent > lastEvent)
    cutoff.fallback = "unknownOccurrence";
  else
  {
    // Use the base of this actual submission, never the base of another partial occurrence of
    // the same baked list. Subtraction is checked above and cannot overflow like base + length.
    cutoff.last = RDCMIN(lastEvent - baseEvent, info.eventCount - 1);
    cutoff.indexed = true;
    cutoff.unknown = info.replayUnknownWrites && info.replayFirstUnknownWriteEvent <= cutoff.last;
  }
  return cutoff;
}

template <typename Key>
static bool IsReplayWriteInPrefix(const std::map<Key, uint32_t> &firstEvents, const Key &key,
                                  const ReplayWriteCutoff &cutoff)
{
  if(!cutoff.indexed)
    return true;
  const auto first = firstEvents.find(key);
  // BuildReplayWriteTracking checks index completeness. If a later path ever adds an unindexed
  // union member, retaining it is the safe direction and keeps the old dirty upper bound.
  return first == firstEvents.end() || first->second <= cutoff.last;
}

static bool ReplayUseDeclaredUAVIndex(bool enabled, bool edited, bool completeIndex)
{
  return enabled && !edited && completeIndex;
}

void D3D12CommandData::MarkReplaySubmittedWrites(ResourceId baked, uint32_t baseEvent,
                                                uint32_t lastEvent)
{
  if(!m_pDevice->IsReplayDirtyTrackingValid())
    return;
  auto found = m_BakedCmdListInfo.find(baked);
  bool knownOccurrence = false;
  for(int p = 0; p < ePartialNum; p++)
  {
    const auto executions = m_Partial[p].cmdListExecs.find(baked);
    if(executions != m_Partial[p].cmdListExecs.end())
      for(uint32_t base : executions->second)
        knownOccurrence |= base == baseEvent;
  }
  ReplayWriteCutoff cutoff;
  if(found != m_BakedCmdListInfo.end())
    cutoff = SelectReplayWriteCutoff(found->second, m_ReplayWriteCutoffEnabled, knownOccurrence,
                                    baseEvent, lastEvent);
  if(found == m_BakedCmdListInfo.end() || !found->second.replayWritesKnown || cutoff.unknown)
  {
    const rdcstr reason = StringFormat::Fmt("unknownSubmittedList:%s:%s", ToStr(baked).c_str(),
        found == m_BakedCmdListInfo.end() ? "missing" :
            ToStr(found->second.replayUnknownWriteChunk).c_str());
    m_pDevice->InvalidateReplayDirtyTracking(reason.c_str());
    if(m_PreparedTimingEnabled)
    {
      const rdcstr line = StringFormat::Fmt(
          "api=D3D12DirtyWriteCutoff baked=%s base=%u requestedEnd=%u localLast=%u enabled=%u "
          "indexed=%u firstUnknown=%u proofValid=0 fallback=%s reason=%s",
          ToStr(baked).c_str(), baseEvent, lastEvent, cutoff.last,
          m_ReplayWriteCutoffEnabled ? 1U : 0U, cutoff.indexed ? 1U : 0U,
          found == m_BakedCmdListInfo.end() ? ~0U : found->second.replayFirstUnknownWriteEvent,
          cutoff.fallback, reason.c_str());
      RDCLOG("%s", line.c_str());
      AppendReplayTimingLog(line);
    }
    return;
  }
  const BakedCmdListInfo &info = found->second;
  uint32_t skippedResources = 0, skippedRootUAVs = 0, skippedRanges = 0;
  for(ResourceId id : info.replayWrites)
    if(IsReplayWriteInPrefix(info.replayWriteFirstEvents, id, cutoff))
      m_pDevice->MarkReplayResourceDirty(id);
    else
      skippedResources++;
  for(ResourceId id : info.replayRootUAVWrites)
    if(IsReplayWriteInPrefix(info.replayRootUAVFirstEvents, id, cutoff))
      m_pDevice->MarkReplayResourceDirty(id, true);
    else
      skippedRootUAVs++;
  std::set<ResourceId> descriptors;
  const bool declaredUAVs = ReplayUseDeclaredUAVIndex(
      UseReplayUAVDeclarations(), m_pDevice->HasReplayCheckpointDependencyEdits(),
      info.replayWriteEventsKnown);
  const auto &uavRanges = declaredUAVs ? info.replayDeclaredUAVRanges : info.replayUAVRanges;
  const auto &uavFirstEvents =
      declaredUAVs ? info.replayDeclaredUAVFirstEvents : info.replayUAVFirstEvents;
  for(const auto &range : uavRanges)
  {
    if(!IsReplayWriteInPrefix(uavFirstEvents, range, cutoff))
    {
      skippedRanges++;
      continue;
    }
    WrappedID3D12DescriptorHeap *heap =
        m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12DescriptorHeap>(range.heap);
    if(!heap || m_pDevice->GetResourceManager()->HasReplacement(range.heap) ||
       !heap->GatherReplayUAVResources(range.first, range.count, descriptors))
    {
      m_pDevice->InvalidateReplayDirtyTracking("unknownSubmittedDescriptors");
      return;
    }
  }
  for(ResourceId id : descriptors)
    m_pDevice->MarkReplayResourceDirty(id);
  // Log the target occurrence/OnlyDraw and the first submission that loses a valid dirty proof.
  // Other complete occurrences remain unlogged; never write one line for every submitted list.
  if(m_PreparedTimingEnabled &&
     (!m_pDevice->IsReplayDirtyTrackingValid() ||
      (baseEvent != ~0U && lastEvent >= baseEvent &&
       uint64_t(lastEvent) - baseEvent < info.eventCount)))
  {
    const rdcstr line = StringFormat::Fmt(
        "api=D3D12DirtyWriteCutoff baked=%s base=%u requestedEnd=%u localLast=%u enabled=%u "
        "indexed=%u activeWriteStages=%u skippedResources=%u skippedRootUAVs=%u skippedRanges=%u "
        "declaredUAVs=%u declarationHits=%u declarationFallbacks=%u uavSlotsBefore=%llu "
        "uavSlotsAfter=%llu "
        "firstUnknown=%u futureUnknown=%u proofValid=%u fallback=%s",
        ToStr(baked).c_str(), baseEvent, lastEvent, cutoff.last,
        m_ReplayWriteCutoffEnabled ? 1U : 0U, cutoff.indexed ? 1U : 0U,
        UseReplayActiveWriteStages() ? 1U : 0U, skippedResources, skippedRootUAVs, skippedRanges,
        declaredUAVs ? 1U : 0U, info.replayUAVDeclarationHits, info.replayUAVDeclarationFallbacks,
        (unsigned long long)info.replayUAVSlotsBefore, (unsigned long long)info.replayUAVSlotsAfter,
        info.replayFirstUnknownWriteEvent,
        info.replayUnknownWrites && !cutoff.unknown ? 1U : 0U,
        m_pDevice->IsReplayDirtyTrackingValid() ? 1U : 0U, cutoff.fallback);
    RDCLOG("%s", line.c_str());
    AppendReplayTimingLog(line);
  }
}

static uint32_t ReplayChunkFirstWriteEvent(const BakedCmdListInfo &info, D3D12Chunk chunk)
{
  if(chunk == D3D12Chunk::List_ExecuteIndirect)
  {
    // ObserveCheckpointChunk runs after the handler and event increment. ExecuteIndirect has
    // already advanced past every reserved child; use its saved pre-expansion group start.
    // An unresolved group gets the whole-recording lower bound, never a guessed later event.
    return info.replayLastIndirectEvent;
  }
  return info.curEventID ? info.curEventID - 1 : 0;
}

void D3D12CommandData::MarkReplaySubmittedOverwrites(ResourceId baked, uint32_t baseEvent,
                                                     uint32_t lastEvent)
{
  const auto found = m_BakedCmdListInfo.find(baked);
  const auto executions = m_Partial[Primary].cmdListExecs.find(baked);
  if(!m_pDevice || found == m_BakedCmdListInfo.end() ||
     executions == m_Partial[Primary].cmdListExecs.end() ||
     !executions->second.contains(baseEvent) || lastEvent < baseEvent)
    return;
  const BakedCmdListInfo &info = found->second;
  for(const auto &clear : info.replayClearOps)
    if(clear.unconditional && clear.fullResource && clear.relativeEvent < info.eventCount &&
       uint64_t(baseEvent) + clear.relativeEvent >= m_FirstEventID &&
       uint64_t(baseEvent) + clear.relativeEvent <= lastEvent)
      m_pDevice->MarkReplayFullOverwriteSubmitted(clear.resource);
}

void D3D12CommandData::ObserveCheckpointChunk(D3D12Chunk chunk, uint64_t chunkEndOffset)
{
  m_ReplayTypedFrameEnd = chunkEndOffset;
  if(m_ReplayTypedRootComplete && m_LastCmdListID == ResourceId())
  {
    if(m_ReplayTypedRootChunks.size() >= 1000000 || !m_StructuredFile ||
       m_StructuredFile->chunks.empty() || m_CurChunkOffset >= chunkEndOffset)
    {
      m_ReplayTypedRootComplete = false;
      m_ReplayTypedRootChunks.clear();
    }
    else
      m_ReplayTypedRootChunks.push_back({m_CurChunkOffset, chunkEndOffset, m_RootEventID,
          uint32_t(m_StructuredFile->chunks.size() - 1), chunk});
  }
  m_BufferPatchReadIndexPrepared = false;
  if(m_LastCmdListID != ResourceId())
  {
    BakedCmdListInfo &info = m_BakedCmdListInfo[m_LastCmdListID];
    if(chunk == D3D12Chunk::List_Reset)
    {
      info.bufferPatchReads.clear();
      info.bufferPatchReadsComplete = true;
      info.replayClearOps.clear();
      info.replayTextureCopies.clear();
      info.replayTextureCopiesComplete = true;
      info.replayTypedBarriers.clear();
      info.replayTypedBarriersComplete = true;
      info.replayTextureReadOps.clear();
      info.replayTextureReadEntries = 0;
      info.replayTextureReadsComplete = true;
      info.replayWrites.clear();
      info.replayRootUAVWrites.clear();
      info.replayUAVRanges.clear();
      info.replayWriteFirstEvents.clear();
      info.replayRootUAVFirstEvents.clear();
      info.replayUAVFirstEvents.clear();
      info.replayDeclaredUAVRanges.clear();
      info.replayDeclaredUAVFirstEvents.clear();
      info.replayUAVSlotsBefore = info.replayUAVSlotsAfter = 0;
      info.replayUAVDeclarationHits = info.replayUAVDeclarationFallbacks = 0;
      info.replayFirstUnknownWriteEvent = ~0U;
      info.replayWritesKnown = info.replayWriteEventsKnown = info.replayUnknownWrites = false;
    }
    const uint32_t firstWriteEvent = ReplayChunkFirstWriteEvent(info, chunk);
    ObserveBufferPatchReads(info, chunk, firstWriteEvent);
    const bool previouslyKnown = !info.replayUnknownWrites;
    if(!IsPreparedReplaySafeChunk(chunk) && chunk != D3D12Chunk::List_ExecuteIndirect &&
       chunk != D3D12Chunk::List_SetPredication && chunk != D3D12Chunk::List_DiscardResource &&
       chunk != D3D12Chunk::List_ClearUnorderedAccessViewUint &&
       chunk != D3D12Chunk::List_ClearUnorderedAccessViewFloat)
      info.replayUnknownWrites = true;
    switch(chunk)
    {
      case D3D12Chunk::List_DrawInstanced:
      case D3D12Chunk::List_DrawIndexedInstanced:
      case D3D12Chunk::List_Dispatch:
      case D3D12Chunk::List_DispatchMesh:
      case D3D12Chunk::List_ExecuteIndirect:
      {
        const D3D12CommandSignature *signature = NULL;
        if(chunk == D3D12Chunk::List_ExecuteIndirect && info.replayLastIndirectSignature)
          signature = &info.replayLastIndirectSignature->sig;
        const ReplayWriteBindingStages stages = UseReplayActiveWriteStages()
                                                     ? ReplayWriteStages(chunk, signature)
                                                     : ReplayWriteBindingStages{true, true};
        ObserveReplayWriteBindings(info, info.state, firstWriteEvent, stages.graphics,
                                    stages.compute, ReplayUAVDeclarationBindingsKnown(chunk, signature));
        break;
      }
      case D3D12Chunk::List_BeginRenderPass:
      case D3D12Chunk::List_EndRenderPass:
      case D3D12Chunk::List_WriteBufferImmediate:
      case D3D12Chunk::List_AtomicCopyBufferUINT:
      case D3D12Chunk::List_AtomicCopyBufferUINT64:
      case D3D12Chunk::List_CopyTiles:
      case D3D12Chunk::List_ExecuteBundle:
      case D3D12Chunk::List_Barrier:
      case D3D12Chunk::List_BuildRaytracingAccelerationStructure:
      case D3D12Chunk::List_CopyRaytracingAccelerationStructure:
      case D3D12Chunk::List_EmitRaytracingAccelerationStructurePostbuildInfo:
      case D3D12Chunk::List_DispatchRays:
      case D3D12Chunk::List_SetProgram:
      case D3D12Chunk::List_DispatchGraph: info.replayUnknownWrites = true; break;
      default: break;
    }
    switch(chunk)
    {
      case D3D12Chunk::List_SetGraphicsRootConstantBufferView:
      case D3D12Chunk::List_SetComputeRootConstantBufferView:
      case D3D12Chunk::List_SetGraphicsRootShaderResourceView:
      case D3D12Chunk::List_SetComputeRootShaderResourceView:
      case D3D12Chunk::List_SetGraphicsRootUnorderedAccessView:
      case D3D12Chunk::List_SetComputeRootUnorderedAccessView:
        for(const auto *root : {&info.state.graphics, &info.state.compute})
          for(const auto &element : root->sigelems)
            if((element.type == eRootCBV || element.type == eRootSRV || element.type == eRootUAV) &&
               element.id != ResourceId())
              info.bufferPatchConsumers.insert(element.id);
        break;
      case D3D12Chunk::List_SetGraphicsRootDescriptorTable:
      case D3D12Chunk::List_SetComputeRootDescriptorTable:
      case D3D12Chunk::List_ExecuteIndirect:
        info.bufferPatchUnknownConsumers = true;
        break;
      case D3D12Chunk::List_SetGraphicsRootSignature:
      case D3D12Chunk::List_SetComputeRootSignature:
        for(const auto *root : {&info.state.graphics, &info.state.compute})
        {
          if(root->rootsig == ResourceId())
            continue;
          WrappedID3D12RootSignature *signature =
              m_pDevice ? m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12RootSignature>(
                              root->rootsig)
                        : NULL;
          if(!signature || (signature->sig.Flags &
                            D3D12_ROOT_SIGNATURE_FLAG_CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED))
            info.bufferPatchUnknownConsumers = true;
        }
        break;
      default: break;
    }
    info.checkpointChunks.push_back(
        {m_LastCmdListID, m_CurChunkOffset, chunkEndOffset, info.curEventID, chunk});
    if(chunk == D3D12Chunk::List_ExecuteIndirect && info.replayLastIndirectSignature)
      info.checkpointChunks.back().directIndirect =
          IsCheckpointDirectIndirectSignature(info.replayLastIndirectSignature->sig);
    if(chunk == D3D12Chunk::List_ExecuteIndirect)
      if(!info.replayLastIndirectSignature ||
         !HasKnownReplayIndirectWrites(info.replayLastIndirectSignature->sig))
        info.replayUnknownWrites = true;
    if(info.replayUnknownWrites)
    {
      if(previouslyKnown)
        RecordUnknownReplayWrite(info, firstWriteEvent, chunk);
      else if(info.replayFirstUnknownWriteEvent == ~0U)
        RecordUnknownReplayWrite(info, 0, chunk);    // unknown origin keeps the original full fallback
    }
  }
  if(chunk == D3D12Chunk::List_Reset)
    m_BakedCmdListInfo[m_LastCmdListID].checkpointBegin = m_CurChunkOffset;
  else if(chunk == D3D12Chunk::List_Close)
    m_BakedCmdListInfo[m_LastCmdListID].checkpointEnd = chunkEndOffset;
  else if(chunk == D3D12Chunk::Queue_ExecuteCommandLists)
  {
    m_CheckpointBoundaries.push_back({m_RootEventID - 1, m_RootEventID, 0, chunkEndOffset});
    if(m_pDevice)
      m_pDevice->RecordCheckpointDescriptorBytes(m_CheckpointBoundaries.back().descriptorBytes,
                                                 m_CheckpointBoundaries.back().fullDescriptorBytes);
  }

  // These commands carry state outside normal resource bytes and closed primary lists. A future
  // backend extension must explicitly save their state before permitting a seek across them.
  switch(chunk)
  {
    case D3D12Chunk::Queue_UpdateTileMappings:
    case D3D12Chunk::Queue_CopyTileMappings:
    case D3D12Chunk::List_ExecuteBundle:
    case D3D12Chunk::List_Barrier:
    case D3D12Chunk::List_BuildRaytracingAccelerationStructure:
    case D3D12Chunk::List_CopyRaytracingAccelerationStructure:
    case D3D12Chunk::List_DispatchRays:
    case D3D12Chunk::List_DispatchGraph:
      if(m_CheckpointUnsupported.empty())
        m_CheckpointUnsupported = "Checkpoint state is not implemented for " + ToStr(chunk);
      break;
    default: break;
  }
}

// Deliberately a texture-read-free whitelist, not the replay/continuation whitelist. Copy, query
// resolves, render-pass operations and future API commands stop discovery even if replay supports
// them. No shader reflection or absent GetUsage entry can establish that an operation is harmless.
static bool IsReplayClearPrefixState(D3D12Chunk chunk)
{
  switch(chunk)
  {
    case D3D12Chunk::List_Close:
    case D3D12Chunk::List_Reset:
    case D3D12Chunk::List_ClearState:
    case D3D12Chunk::List_IASetPrimitiveTopology:
    case D3D12Chunk::List_IASetIndexBuffer:
    case D3D12Chunk::List_IASetVertexBuffers:
    case D3D12Chunk::List_SOSetTargets:
    case D3D12Chunk::List_RSSetViewports:
    case D3D12Chunk::List_RSSetScissorRects:
    case D3D12Chunk::List_SetPipelineState:
    case D3D12Chunk::List_SetDescriptorHeaps:
    case D3D12Chunk::List_OMSetRenderTargets:
    case D3D12Chunk::List_OMSetStencilRef:
    case D3D12Chunk::List_OMSetBlendFactor:
    case D3D12Chunk::List_SetGraphicsRootDescriptorTable:
    case D3D12Chunk::List_SetGraphicsRootSignature:
    case D3D12Chunk::List_SetGraphicsRoot32BitConstant:
    case D3D12Chunk::List_SetGraphicsRoot32BitConstants:
    case D3D12Chunk::List_SetGraphicsRootConstantBufferView:
    case D3D12Chunk::List_SetGraphicsRootShaderResourceView:
    case D3D12Chunk::List_SetGraphicsRootUnorderedAccessView:
    case D3D12Chunk::List_SetComputeRootDescriptorTable:
    case D3D12Chunk::List_SetComputeRootSignature:
    case D3D12Chunk::List_SetComputeRoot32BitConstant:
    case D3D12Chunk::List_SetComputeRoot32BitConstants:
    case D3D12Chunk::List_SetComputeRootConstantBufferView:
    case D3D12Chunk::List_SetComputeRootShaderResourceView:
    case D3D12Chunk::List_SetComputeRootUnorderedAccessView:
    case D3D12Chunk::List_SetPredication:
    case D3D12Chunk::List_OMSetDepthBounds:
    case D3D12Chunk::List_SetSamplePositions:
    case D3D12Chunk::List_SetViewInstanceMask:
    case D3D12Chunk::List_RSSetShadingRate:
    case D3D12Chunk::List_RSSetShadingRateImage:
    case D3D12Chunk::List_OMSetFrontAndBackStencilRef:
    case D3D12Chunk::List_RSSetDepthBias:
    case D3D12Chunk::List_IASetIndexBufferStripCutValue:
    // These change hidden query-heap state, not texture contents. They still execute normally;
    // draws remain subject to the complete per-resource read proof, and ResolveQueryData stops
    // discovery. This does not make any query/checkpoint continuation newly eligible.
    case D3D12Chunk::List_BeginQuery:
    case D3D12Chunk::List_EndQuery:
    case D3D12Chunk::PushMarker:
    case D3D12Chunk::SetMarker:
    case D3D12Chunk::PopMarker:
    case D3D12Chunk::SetCommandAnnotation: return true;
    default: return false;
  }
}

bool BakedCmdListInfo::IsReplayClearBarrierReadFree(D3D12_RESOURCE_BARRIER_TYPE type,
                                                  D3D12_RESOURCE_BARRIER_FLAGS flags)
{
  // This classifies logical texture-content reads only. The original barrier still executes.
  // An alias barrier concerns placed/reserved storage, excluded by the caller's committed set.
  // Do not broaden split-transition, unknown-type, or unknown-flag handling along with it.
  return flags == D3D12_RESOURCE_BARRIER_FLAG_NONE &&
         (type == D3D12_RESOURCE_BARRIER_TYPE_TRANSITION ||
          type == D3D12_RESOURCE_BARRIER_TYPE_ALIASING ||
          type == D3D12_RESOURCE_BARRIER_TYPE_UAV);
}

static bool ObserveReplayClearPrefix(D3D12Chunk chunk, bool unsafeBarrier,
                                     const BakedCmdListInfo::ReplayClearOp *clear,
                                     const std::set<ResourceId> &eligibleTextures,
                                     std::set<ResourceId> &overwritten)
{
  if(chunk == D3D12Chunk::List_ResourceBarrier)
    return !unsafeBarrier;
  if(chunk == D3D12Chunk::List_ClearRenderTargetView ||
     chunk == D3D12Chunk::List_ClearDepthStencilView || chunk == D3D12Chunk::List_DiscardResource)
  {
    // A predicate itself may consume a resource. Partial unconditional clears do not read old
    // content and can precede a later full clear, but cannot authorize a skipped resource copy.
    if(!clear || !clear->unconditional || clear->resource == ResourceId())
      return false;
    if(clear->fullResource && eligibleTextures.count(clear->resource))
      overwritten.insert(clear->resource);
    return true;
  }
  return IsReplayClearPrefixState(chunk);
}

// The simple whole-resource model is used only for non-inheriting UNKNOWN-layout textures.
// A frame can start with an unknown active owner. Later explicit activation and initialization
// may establish a valid state; preceding unknown contents must never be copied as a checkpoint.
struct ReplayTypedAliasOwners
{
  const std::map<ResourceId, std::set<ResourceId>> &conflicts;
  std::set<ResourceId> unknown, active, valid;
  ReplayTypedAliasOwners(const std::map<ResourceId, std::set<ResourceId>> &graph) : conflicts(graph)
  {
    for(const auto &entry : graph)
      unknown.insert(entry.first);
  }
  void Activate(ResourceId id)
  {
    auto found = conflicts.find(id);
    if(found == conflicts.end())
      return;
    unknown.erase(id);
    valid.erase(id);
    active.insert(id);
    for(ResourceId other : found->second)
    {
      unknown.erase(other);
      active.erase(other);
      valid.erase(other);
    }
  }
  void Access(ResourceId id)
  {
    auto found = conflicts.find(id);
    if(found == conflicts.end() || valid.count(id))
      return;
    // No positive owner proof for this possible access. Do not guess data inheritance or
    // activation from GetUsage; a later explicit full initialization can recover the proof.
    for(ResourceId other : found->second)
    {
      // Descriptor ranges are upper bounds: this access might not actually execute. Therefore
      // it cannot prove another possible owner's contents dead. Preserve that uncertainty until
      // an unconditional explicit activation invalidates its actual physical overlap.
      if(active.count(other) || valid.count(other))
        unknown.insert(other);
      active.erase(other);
      valid.erase(other);
    }
    active.erase(id);
    unknown.insert(id);
  }
  void Initialize(ResourceId id, bool full)
  {
    if(!conflicts.count(id))
      return;
    if(full && active.count(id))
      valid.insert(id);
    else
      Access(id);
  }
  void Transition(ResourceId id)
  {
    // Transitioning an explicitly active owner is necessary before its initial Clear/Copy.
    // An inactive/unknown alias can change layout metadata and destroys an overlapping proof.
    if(!active.count(id))
      Access(id);
  }
  bool Complete() const { return unknown.empty() && active == valid; }
};

bool D3D12CommandData::GetReplayTextureAliasOwners(
    uint32_t throughEvent, const std::map<ResourceId, std::set<ResourceId>> &conflicts,
    std::set<ResourceId> &owners, rdcstr &reason) const
{
  owners.clear();
  const auto fail = [&](uint32_t event, const char *why) {
    reason = StringFormat::Fmt("Texture alias owner proof: %s at EID %u", why, event);
    return false;
  };
  if(conflicts.empty())
    return true;
  // Loading swaps the serialiser's structured file back to the device before returning. The
  // command-data pointer can still name that temporary serialiser until the next replay starts.
  const SDFile *structured = m_pDevice ? m_pDevice->GetStructuredFile() : m_StructuredFile;
  if(!throughEvent || !structured || throughEvent >= m_Events.size() ||
     throughEvent > 1000000 || conflicts.size() > 4096 || !m_CheckpointUnsupported.empty())
    return fail(throughEvent, "invalid range, incomplete capture, or metadata budget");
  uint64_t prefixEnd = 0;
  for(const CheckpointBoundary &boundary : m_CheckpointBoundaries)
    if(boundary.eventId == throughEvent && boundary.nextEvent == throughEvent + 1)
      prefixEnd = boundary.offset;
  if(!prefixEnd || !m_ReplayTypedRootComplete || !m_ReplayTypedFrameBegin ||
     prefixEnd <= m_ReplayTypedFrameBegin || prefixEnd > m_ReplayTypedFrameEnd)
    return fail(throughEvent, "missing complete native root prefix");
  uint64_t work = 0, previousRootEnd = m_ReplayTypedFrameBegin;
  bool submittedBoundary = false;
  for(const ReplayTypedRootChunk &root : m_ReplayTypedRootChunks)
  {
    if(root.offset >= prefixEnd)
      break;
    if(++work > 1000000 || root.offset < previousRootEnd || root.offset >= root.endOffset ||
       root.endOffset > prefixEnd || root.chunkIndex >= structured->chunks.size() ||
       structured->chunks[root.chunkIndex]->metadata.chunkID != uint32_t(root.type) ||
       !root.nextEvent || root.nextEvent > throughEvent + 1)
      return fail(throughEvent, "invalid native root identity or prefix coverage");
    previousRootEnd = root.endOffset;
    submittedBoundary |= root.endOffset == prefixEnd && root.nextEvent == throughEvent + 1 &&
                         root.type == D3D12Chunk::Queue_ExecuteCommandLists;
    switch(root.type)
    {
      case D3D12Chunk::Queue_ExecuteCommandLists:
      case D3D12Chunk::Queue_Signal:
      case D3D12Chunk::Queue_Wait:
      case D3D12Chunk::Queue_BeginEvent:
      case D3D12Chunk::Queue_EndEvent:
      case D3D12Chunk::Queue_SetMarker:
      case D3D12Chunk::SetCommandAnnotation:
      case D3D12Chunk::SetQueueAnnotation:
      // Descriptor creation/copy changes descriptor storage, not texture contents or activation.
      // Consumers still use complete captured descriptor history and exact RTV/DSV identities.
      case D3D12Chunk::Device_CreateConstantBufferView:
      case D3D12Chunk::Device_CreateShaderResourceView:
      case D3D12Chunk::Device_CreateUnorderedAccessView:
      case D3D12Chunk::Device_CreateRenderTargetView:
      case D3D12Chunk::Device_CreateDepthStencilView:
      case D3D12Chunk::Device_CreateSampler:
      case D3D12Chunk::Device_CreateSampler2:
      case D3D12Chunk::Device_CopyDescriptors:
      case D3D12Chunk::Device_CopyDescriptorsSimple: break;
      default:
      {
        reason = StringFormat::Fmt("Texture alias owner proof: unsupported root %s before EID %u",
                                   ToStr(root.type).c_str(), root.nextEvent);
        return false;
      }
    }
  }
  if(!submittedBoundary)
    return fail(throughEvent, "native submission boundary is missing");
  uint64_t graphEdges = 0;
  for(const auto &entry : conflicts)
  {
    graphEdges += entry.second.size();
    if(graphEdges > 65536)
      return fail(0, "physical overlap graph budget");
    if(entry.first == ResourceId() || entry.second.empty() || entry.second.count(entry.first))
      return fail(0, "invalid physical overlap graph");
    for(ResourceId other : entry.second)
    {
      auto opposite = conflicts.find(other);
      if(opposite == conflicts.end() || !opposite->second.count(entry.first))
        return fail(0, "incomplete physical overlap closure");
    }
  }
  struct Occurrence
  {
    uint32_t base, end;
    const BakedCmdListInfo *info;
  };
  rdcarray<Occurrence> occurrences;
  std::set<ResourceId> queues;
  std::map<uint32_t, const BakedCmdListInfo::ReplayTextureReadOp *> reads;
  std::map<uint32_t, const BakedCmdListInfo::ReplayTextureCopyOp *> copies;
  std::map<uint32_t, const BakedCmdListInfo::ReplayClearOp *> clears;
  std::map<uint32_t, rdcarray<const BakedCmdListInfo::CheckpointBarrierOp *>> aliases;
  std::set<uint32_t> unsafeBarriers;
  for(const auto &entry : m_Partial[Primary].cmdListExecs)
  {
    const auto baked = m_BakedCmdListInfo.find(entry.first);
    if(baked == m_BakedCmdListInfo.end())
      return fail(0, "missing submitted command-list metadata");
    const auto &info = baked->second;
    for(uint32_t base : entry.second)
    {
      if(base > throughEvent)
        continue;
      const uint64_t end = uint64_t(base) + info.eventCount;
      const auto queue = m_ReplaySubmissionQueues.find(base);
      if(!info.eventCount || end > throughEvent || info.type != D3D12_COMMAND_LIST_TYPE_DIRECT ||
         !info.checkpointBegin || !info.checkpointEnd || info.checkpointChunks.empty() ||
         !info.replayTextureReadsComplete || !info.replayTextureCopiesComplete ||
         !info.replayTypedBarriersComplete || queue == m_ReplaySubmissionQueues.end() ||
         queue->second == ResourceId())
        return fail(base, "incomplete primary submission or texture access evidence");
      queues.insert(queue->second);
      if(queues.size() != 1)
        return fail(base, "multiple participating queues");
      occurrences.push_back({base, uint32_t(end), &info});
      for(const auto &read : info.replayTextureReadOps)
        if(++work > 1000000 || read.relativeEvent >= info.eventCount ||
           !reads.insert({base + read.relativeEvent, &read}).second)
          return fail(base, "invalid read index or metadata budget");
      for(const auto &copy : info.replayTextureCopies)
        if(++work > 1000000 || copy.relativeEvent >= info.eventCount ||
           !copies.insert({base + copy.relativeEvent, &copy}).second)
          return fail(base, "invalid copy index or metadata budget");
      for(const auto &clear : info.replayClearOps)
        if(++work > 1000000 || clear.relativeEvent >= info.eventCount ||
           !clears.insert({base + clear.relativeEvent, &clear}).second)
          return fail(base, "invalid clear index or metadata budget");
      for(const auto &barrier : info.replayTypedBarriers)
      {
        if(++work > 1000000 || barrier.relativeEvent >= info.eventCount)
          return fail(base, "invalid barrier index or metadata budget");
        aliases[base + barrier.relativeEvent].push_back(&barrier);
      }
      for(uint32_t unsafe : info.replayClearUnsafeBarriers)
      {
        if(++work > 1000000 || unsafe >= info.eventCount)
          return fail(base, "invalid unsafe barrier index or metadata budget");
        unsafeBarriers.insert(base + unsafe);
      }
    }
  }
  for(const auto &entry : m_Partial[Secondary].cmdListExecs)
    for(uint32_t base : entry.second)
      if(base <= throughEvent)
        return fail(base, "secondary execution");
  std::sort(occurrences.begin(), occurrences.end(),
            [](const Occurrence &a, const Occurrence &b) { return a.base < b.base; });
  for(size_t i = 1; i < occurrences.size(); i++)
    if(occurrences[i].base < occurrences[i - 1].end)
      return fail(occurrences[i].base, "overlapping primary submissions");

  ReplayTypedAliasOwners state(conflicts);
  std::map<BakedCmdListInfo::ReplayUAVRange, std::set<ResourceId>> descriptorAliases;
  std::set<ResourceId> touchedMembers;
  const auto chargeAccess = [&](ResourceId id) {
    auto found = conflicts.find(id);
    // Touching any member makes its entire physical overlap component relevant. Only wholly
    // untouched components may retain their canonical initial contents without an owner copy.
    if(found != conflicts.end() && touchedMembers.insert(id).second)
    {
      rdcarray<ResourceId> pending = {id};
      while(!pending.empty())
      {
        const ResourceId member = pending.back();
        pending.pop_back();
        if(++work > 1000000)
          return false;
        for(ResourceId other : conflicts.at(member))
        {
          if(++work > 1000000)
            return false;
          if(touchedMembers.insert(other).second)
            pending.push_back(other);
        }
      }
    }
    work += found == conflicts.end() ? 1 : 1 + found->second.size();
    return work <= 1000000;
  };
  size_t occurrence = 0;
  for(uint32_t eid = 1; eid <= throughEvent; eid++)
  {
    if(++work > 1000000)
      return fail(eid, "query work budget");
    while(occurrence < occurrences.size() && eid >= occurrences[occurrence].end)
      occurrence++;
    if(occurrence == occurrences.size() || eid < occurrences[occurrence].base)
      continue;    // the native root index proved these calls, gaps, and synthetic boundaries
    const APIEvent &event = m_Events[eid];
    if(event.eventId != eid || event.chunkIndex >= structured->chunks.size())
      return fail(eid, "missing exact submitted API event");
    const auto chunk = D3D12Chunk(structured->chunks[event.chunkIndex]->metadata.chunkID);
    const auto &item = occurrences[occurrence];
    const auto &info = *item.info;
    auto recorded = std::lower_bound(info.checkpointChunks.begin(), info.checkpointChunks.end(),
                                     event.fileOffset,
                                     [](const BakedCmdListInfo::CheckpointRecordingChunk &c,
                                        uint64_t offset) { return c.offset < offset; });
    if(recorded == info.checkpointChunks.end() || recorded->offset != event.fileOffset ||
       recorded->type != chunk)
      return fail(eid, "recording identity mismatch");
    if(chunk == D3D12Chunk::List_ResourceBarrier)
    {
      if(unsafeBarriers.count(eid))
        return fail(eid, "split or unknown barrier");
      auto barriers = aliases.find(eid);
      if(barriers != aliases.end())
        for(const auto *barrier : barriers->second)
        {
          if(++work > 1000000)
            return fail(eid, "barrier work budget");
          if(barrier->flags != D3D12_RESOURCE_BARRIER_FLAG_NONE ||
             (barrier->type != D3D12_RESOURCE_BARRIER_TYPE_ALIASING &&
              barrier->type != D3D12_RESOURCE_BARRIER_TYPE_TRANSITION))
            return fail(eid, "split or unknown typed barrier");
          const ResourceId target = barrier->type == D3D12_RESOURCE_BARRIER_TYPE_ALIASING
                                        ? barrier->after
                                        : barrier->before;
          if(target == ResourceId())
            return fail(eid, "wildcard alias activation or null transition");
          if(!chargeAccess(target))
            return fail(eid, "alias activation work budget");
          if(barrier->type == D3D12_RESOURCE_BARRIER_TYPE_ALIASING)
            state.Activate(target);
          else
            state.Transition(target);
        }
      continue;
    }
    if(chunk == D3D12Chunk::List_DrawInstanced || chunk == D3D12Chunk::List_DrawIndexedInstanced ||
       chunk == D3D12Chunk::List_Dispatch || chunk == D3D12Chunk::List_DispatchMesh ||
       chunk == D3D12Chunk::List_ExecuteIndirect)
    {
      const auto found = reads.find(eid);
      const auto *access = found == reads.end() ? NULL : found->second;
      if(!access || access->unknown)
        return fail(eid, "missing or unknown texture bindings");
      std::set<ResourceId> mayAccess;
      for(ResourceId direct : access->directResources)
      {
        if(++work > 1000000)
          return fail(eid, "overlap access work budget");
        if(conflicts.count(direct))
          mayAccess.insert(direct);
      }
      for(const auto &range : access->descriptorRanges)
      {
        if(++work > 1000000)
          return fail(eid, "descriptor access work budget");
        auto known = descriptorAliases.find(range);
        if(known == descriptorAliases.end())
        {
          auto *rm = m_pDevice ? m_pDevice->GetResourceManager() : NULL;
          ID3D12DeviceChild *object = rm && rm->HasResource(range.heap) &&
                                              !rm->HasReplacement(range.heap) &&
                                              rm->GetUnreplacedID(range.heap) == range.heap
                                          ? rm->GetResourceWithoutReplacement(range.heap, true)
                                          : NULL;
          if(!object || TryIdentifyTypeByPtr(object) != Resource_DescriptorHeap)
            return fail(eid, "descriptor heap identity changed");
          auto *heap = (WrappedID3D12DescriptorHeap *)object;
          std::set<ResourceId> referenced;
          for(const auto &candidate : conflicts)
          {
            if(++work > 1000000)
              return fail(eid, "descriptor history work budget");
            if(heap->ReplayRangeMayReference(candidate.first, range.first, range.count))
              referenced.insert(candidate.first);
          }
          known = descriptorAliases.insert({range, std::move(referenced)}).first;
        }
        for(ResourceId id : known->second)
        {
          if(++work > 1000000)
            return fail(eid, "descriptor closure work budget");
          mayAccess.insert(id);
        }
      }
      for(ResourceId id : mayAccess)
      {
        if(!chargeAccess(id))
          return fail(eid, "owner update work budget");
        state.Access(id);
      }
      continue;
    }
    if(chunk == D3D12Chunk::List_CopyResource || chunk == D3D12Chunk::List_CopyTextureRegion)
    {
      const auto found = copies.find(eid);
      const auto *copy = found == copies.end() ? NULL : found->second;
      if(!copy || copy->source == ResourceId() || copy->destination == ResourceId())
        return fail(eid, "missing copy identities");
      if(!chargeAccess(copy->source) || !chargeAccess(copy->destination))
        return fail(eid, "copy owner work budget");
      state.Access(copy->source);
      state.Initialize(copy->destination, copy->unconditional && copy->fullResource);
      continue;
    }
    if(chunk == D3D12Chunk::List_ClearRenderTargetView ||
       chunk == D3D12Chunk::List_ClearDepthStencilView || chunk == D3D12Chunk::List_DiscardResource)
    {
      const auto found = clears.find(eid);
      const auto *clear = found == clears.end() ? NULL : found->second;
      if(!clear || clear->resource == ResourceId())
        return fail(eid, "missing clear identity");
      if(!chargeAccess(clear->resource))
        return fail(eid, "clear owner work budget");
      state.Initialize(clear->resource, clear->unconditional && clear->fullResource);
      continue;
    }
    if(IsReplayClearPrefixState(chunk) || chunk == D3D12Chunk::List_CopyBufferRegion ||
       chunk == D3D12Chunk::List_ResolveQueryData)
      continue;
    return fail(eid, "unclassified texture operation");
  }
  // A wholly untouched overlap component keeps its canonical initial contents. Initial heap
  // restoration is not skipped by this path. Every component which was touched needs complete
  // owner/initialization evidence, including unknown nonoverlapping owners in the same closure.
  bool complete = state.active == state.valid;
  for(ResourceId id : state.unknown)
    complete &= touchedMembers.count(id) == 0;
  if(!complete)
    return fail(throughEvent, "active owner or full initialization remains unknown");
  owners = state.valid;
  reason.clear();
  return true;
}

std::set<ResourceId> D3D12CommandData::GetReplayFullClearPrefix(
    uint32_t checkpointEvent, uint32_t throughEvent,
    const std::set<ResourceId> &eligibleTextures) const
{
  std::set<ResourceId> overwritten;
  struct Diagnostic
  {
    bool enabled;
    uint32_t checkpoint, target;
    size_t queues;
    size_t candidates = 0, approved = 0, readOps = 0, unknownReads = 0, blockedCandidates = 0;
    size_t eligibleResources = 0, aliasBarrierEventsPassed = 0, aliasBarriersPassed = 0;
    size_t barrierStops = 0;
    uint32_t firstBlock = 0, stopEvent = 0;
    uint32_t stopChunkID = 0;
    rdcstr stopChunkName = "none";
    const char *firstReason = "none", *stopReason = "complete";
    void Block(uint32_t event, const char *reason)
    {
      if(firstBlock == 0)
      {
        firstBlock = event;
        firstReason = reason;
      }
    }
    void Stop(uint32_t event, const char *reason)
    {
      Block(event, reason);
      stopEvent = event;
      stopReason = reason;
    }
    ~Diagnostic()
    {
      if(enabled)
      {
        const rdcstr line = StringFormat::Fmt(
            "api=D3D12ClearProof checkpoint=%u target=%u queues=%zu candidates=%zu approved=%zu "
            "readOps=%zu unknownReads=%zu blockedCandidates=%zu firstBlockingEID=%u "
            "firstReason=%s stopEID=%u stopReason=%s stopChunkID=%u stopChunk=%s "
            "eligibleResources=%zu aliasBarrierEventsPassed=%zu aliasBarriersPassed=%zu "
            "barrierStops=%zu",
            checkpoint, target, queues, candidates, approved, readOps, unknownReads,
            blockedCandidates, firstBlock, firstReason, stopEvent, stopReason,
            stopChunkID, stopChunkName.c_str(), eligibleResources, aliasBarrierEventsPassed,
            aliasBarriersPassed, barrierStops);
        RDCLOG("%s", line.c_str());
        AppendReplayTimingLog(line);
      }
    }
  } diagnostic = {Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1", checkpointEvent,
                   throughEvent, 0};
  diagnostic.eligibleResources = eligibleTextures.size();
  if(eligibleTextures.empty())
  {
    diagnostic.Stop(checkpointEvent, "noEligibleCommittedTexture");
    return overwritten;
  }
  if(!checkpointEvent || checkpointEvent >= throughEvent || !m_CheckpointUnsupported.empty() ||
     !m_StructuredFile || throughEvent >= m_Events.size())
  {
    diagnostic.Stop(checkpointEvent, "invalidRangeOrUnsupportedCapture");
    return overwritten;
  }
  bool boundaryFound = false;
  for(const CheckpointBoundary &boundary : m_CheckpointBoundaries)
    if(boundary.eventId == checkpointEvent && boundary.nextEvent == checkpointEvent + 1)
      boundaryFound = true;
  if(!boundaryFound)
  {
    diagnostic.Stop(checkpointEvent, "checkpointBoundaryMissing");
    return overwritten;
  }

  struct Occurrence
  {
    uint32_t base, end;
    const BakedCmdListInfo *info;
  };
  rdcarray<Occurrence> occurrences;
  std::map<uint32_t, const BakedCmdListInfo::ReplayClearOp *> clears;
  std::map<uint32_t, const BakedCmdListInfo::ReplayTextureReadOp *> reads;
  std::set<ResourceId> candidates, readBeforeClear;
  std::set<ResourceId> participatingQueues;
  std::set<uint32_t> unsafeBarriers;
  std::map<uint32_t, size_t> aliasBarriers;
  for(const auto &entry : m_Partial[Primary].cmdListExecs)
  {
    const auto found = m_BakedCmdListInfo.find(entry.first);
    if(found == m_BakedCmdListInfo.end())
    {
      diagnostic.Stop(checkpointEvent, "submittedListMetadataMissing");
      return {};
    }
    const BakedCmdListInfo &info = found->second;
    for(uint32_t base : entry.second)
    {
      const uint64_t end = uint64_t(base) + info.eventCount;
      if(base > throughEvent || end <= checkpointEvent)
        continue;
      if(base <= checkpointEvent || end > ~0U || !info.eventCount ||
         info.type != D3D12_COMMAND_LIST_TYPE_DIRECT || !info.checkpointBegin ||
         !info.checkpointEnd || info.checkpointChunks.empty())
      {
        diagnostic.Stop(base, "overlappingOrUnsupportedPrimary");
        return {};
      }
      const auto queue = m_ReplaySubmissionQueues.find(base);
      if(queue == m_ReplaySubmissionQueues.end() || queue->second == ResourceId())
      {
        diagnostic.Stop(base, "submittedQueueIdentityMissing");
        return {};
      }
      participatingQueues.insert(queue->second);
      diagnostic.queues = participatingQueues.size();
      // Restore drains the preceding work before publishing checkpoint contents. A single
      // participating queue orders this suffix; unrelated queues created elsewhere in the
      // frame do not invalidate that proof. Multi-queue suffixes still need a finer proof.
      if(participatingQueues.size() != 1)
      {
        diagnostic.Stop(base, "multipleParticipatingQueues");
        return {};
      }
      occurrences.push_back({base, uint32_t(end), &info});
      for(const auto &clear : info.replayClearOps)
      {
        if(clear.relativeEvent >= info.eventCount)
        {
          diagnostic.Stop(base, "clearOutsideList");
          return {};
        }
        const uint32_t event = base + clear.relativeEvent;
        if(!clears.insert({event, &clear}).second)
        {
          diagnostic.Stop(event, "duplicateClearEID");
          return {};
        }
        if(event > checkpointEvent && event <= throughEvent && clear.unconditional &&
           clear.fullResource && eligibleTextures.count(clear.resource))
          candidates.insert(clear.resource);
      }
      if(info.replayTextureReadsComplete)
        for(const auto &read : info.replayTextureReadOps)
        {
          if(read.relativeEvent >= info.eventCount ||
             !reads.insert({base + read.relativeEvent, &read}).second)
          {
            diagnostic.Stop(base, "invalidOrDuplicateReadEID");
            return {};
          }
        }
      for(const auto &barrier : info.checkpointBarriers)
      {
        if(barrier.relativeEvent >= info.eventCount)
        {
          diagnostic.Stop(base, "barrierOutsideList");
          return {};
        }
        if(!BakedCmdListInfo::IsReplayClearBarrierReadFree(barrier.type, barrier.flags))
          unsafeBarriers.insert(base + barrier.relativeEvent);
        else if(barrier.type == D3D12_RESOURCE_BARRIER_TYPE_ALIASING)
          aliasBarriers[base + barrier.relativeEvent]++;
      }
      for(uint32_t event : info.replayClearUnsafeBarriers)
      {
        if(event >= info.eventCount)
        {
          diagnostic.Stop(base, "unsafeBarrierOutsideList");
          return {};
        }
        unsafeBarriers.insert(base + event);
      }
    }
  }
  diagnostic.candidates = candidates.size();
  if(candidates.empty())
  {
    diagnostic.Stop(checkpointEvent, "noFullClearCandidate");
    return {};
  }
  std::sort(occurrences.begin(), occurrences.end(),
            [](const Occurrence &a, const Occurrence &b) { return a.base < b.base; });
  for(size_t i = 1; i < occurrences.size(); i++)
    if(occurrences[i].base < occurrences[i - 1].end)
    {
      diagnostic.Stop(occurrences[i].base, "overlappingPrimaryOccurrences");
      return {};
    }
  // Nested execution cannot be represented by these primary intervals. It is already rejected
  // for normal checkpoint continuation; repeat that invariant for this standalone proof query.
  for(const auto &entry : m_Partial[Secondary].cmdListExecs)
    for(uint32_t base : entry.second)
      if(base > checkpointEvent && base <= throughEvent)
      {
        diagnostic.Stop(base, "secondaryExecution");
        return {};
      }

  size_t occurrence = 0;
  for(uint32_t eid = checkpointEvent + 1; eid <= throughEvent; eid++)
  {
    const APIEvent &event = m_Events[eid];
    if(event.eventId != eid || event.chunkIndex >= m_StructuredFile->chunks.size())
    {
      diagnostic.Stop(eid, "missingExactEvent");
      break;
    }
    const D3D12Chunk chunk =
        D3D12Chunk(m_StructuredFile->chunks[event.chunkIndex]->metadata.chunkID);
    while(occurrence < occurrences.size() && eid >= occurrences[occurrence].end)
      occurrence++;
    const bool inPrimary = occurrence < occurrences.size() &&
                           eid >= occurrences[occurrence].base;
    if(!inPrimary)
    {
      // The loading index inserts synthetic Reset/Close boundaries for submitted lists. Queue
      // waits/signals carry no resource bytes; all other root work fails closed.
      if(chunk == D3D12Chunk::Queue_ExecuteCommandLists || chunk == D3D12Chunk::Queue_Wait ||
         chunk == D3D12Chunk::Queue_Signal || chunk == D3D12Chunk::List_Reset ||
         chunk == D3D12Chunk::List_Close || chunk == D3D12Chunk::PushMarker ||
         chunk == D3D12Chunk::PopMarker || chunk == D3D12Chunk::SetMarker)
        continue;
      diagnostic.Stop(eid, "rootCommandDependency");
      break;
    }
    const BakedCmdListInfo &info = *occurrences[occurrence].info;
    auto recorded = std::lower_bound(
        info.checkpointChunks.begin(), info.checkpointChunks.end(), event.fileOffset,
        [](const BakedCmdListInfo::CheckpointRecordingChunk &item, uint64_t offset) {
          return item.offset < offset;
        });
    if(recorded == info.checkpointChunks.end() || recorded->offset != event.fileOffset ||
       recorded->type != chunk)
    {
      diagnostic.Stop(eid, "recordingIdentityMismatch");
      break;
    }
    if(chunk == D3D12Chunk::List_DrawInstanced || chunk == D3D12Chunk::List_DrawIndexedInstanced ||
       chunk == D3D12Chunk::List_Dispatch || chunk == D3D12Chunk::List_DispatchMesh)
    {
      diagnostic.readOps++;
      const auto read = reads.find(eid);
      // Metadata omission never means an empty binding set. Keep any already-proven clears,
      // but stop before considering a later clear when this executed action is unaccounted for.
      if(read == reads.end() || read->second->unknown)
      {
        diagnostic.unknownReads++;
        diagnostic.Stop(eid, read == reads.end() ? "missingReadBindings" : "unknownReadBindings");
        break;
      }
      for(ResourceId candidate : candidates)
      {
        if(overwritten.count(candidate) || readBeforeClear.count(candidate))
          continue;
        const bool mayRead = ReplayTextureReadMayReference(
            *read->second, candidate,
            [this](const BakedCmdListInfo::ReplayUAVRange &range, ResourceId resource) {
              WrappedID3D12DescriptorHeap *heap = m_pDevice
                  ? m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12DescriptorHeap>(range.heap)
                  : NULL;
              return !heap || m_pDevice->GetResourceManager()->HasReplacement(range.heap) ||
                     heap->ReplayRangeMayReference(resource, range.first, range.count);
            });
        if(mayRead)
        {
          readBeforeClear.insert(candidate);
          diagnostic.blockedCandidates++;
          diagnostic.Block(eid, "candidateMayBeReadBeforeClear");
        }
      }
      continue;
    }
    const auto clear = clears.find(eid);
    if(!ObserveReplayClearPrefix(chunk, unsafeBarriers.count(eid) != 0,
                                 clear == clears.end() ? NULL : clear->second,
                                 eligibleTextures, overwritten))
    {
      if(chunk == D3D12Chunk::List_ResourceBarrier)
        diagnostic.barrierStops++;
      diagnostic.Stop(eid, unsafeBarriers.count(eid) ? "splitOrUnknownBarrier" :
          clear != clears.end() ? "predicatedOrUnknownClear" : "globalCommandDependency");
      diagnostic.stopChunkID = uint32_t(chunk);
      diagnostic.stopChunkName = ToStr(chunk);
      break;
    }
    if(chunk == D3D12Chunk::List_ResourceBarrier)
    {
      const auto aliases = aliasBarriers.find(eid);
      if(aliases != aliasBarriers.end())
      {
        diagnostic.aliasBarrierEventsPassed++;
        diagnostic.aliasBarriersPassed += aliases->second;
      }
    }
    if(clear != clears.end() && readBeforeClear.count(clear->second->resource))
      overwritten.erase(clear->second->resource);
  }
  diagnostic.approved = overwritten.size();
  return overwritten;
}

#if ENABLED(ENABLE_UNIT_TESTS)
TEST_CASE("D3D12 typed alias owners require activation initialization and nonoverlap",
          "[d3d12][checkpoint][typed-alias]")
{
  const ResourceId a = ResourceIDGen::GetNewUniqueID(), b = ResourceIDGen::GetNewUniqueID();
  const ResourceId c = ResourceIDGen::GetNewUniqueID(), unrelated = ResourceIDGen::GetNewUniqueID();
  // A spans B and C; B and C are adjacent nonoverlapping owners in the same closure.
  std::map<ResourceId, std::set<ResourceId>> graph = {{a, {b, c}}, {b, {a}}, {c, {a}}};
  ReplayTypedAliasOwners state(graph);
  CHECK_FALSE(state.Complete());
  state.Access(a);    // initially active capture state is not automatically a known snapshot
  CHECK_FALSE(state.Complete());
  state.Activate(a);
  CHECK_FALSE(state.Complete());
  state.Initialize(a, true);
  REQUIRE(state.Complete());
  CHECK(state.valid == std::set<ResourceId>{a});
  state.Access(unrelated);
  state.Initialize(a, false);    // partial update preserves an already fully initialized owner
  CHECK(state.Complete());
  state.Activate(b);
  CHECK_FALSE(state.Complete());
  state.Initialize(b, true);
  REQUIRE(state.Complete());
  CHECK(state.valid == std::set<ResourceId>{b});
  state.Activate(c);
  state.Initialize(c, true);
  REQUIRE(state.Complete());
  CHECK(state.valid == std::set<ResourceId>({b, c}));
  state.Access(a);    // an unproved overlapping access invalidates both owners
  CHECK_FALSE(state.Complete());
  CHECK(state.valid.empty());
  state.Initialize(a, true);    // full copy alone cannot infer an omitted explicit activation
  CHECK_FALSE(state.Complete());
  state.Activate(a);
  state.Initialize(a, true);
  CHECK(state.Complete());

  // A and C do not overlap; B overlaps both. A *possible* B access cannot prove A dead when a
  // subsequent explicit C activation only invalidates B. Otherwise A's live contents are lost.
  std::map<ResourceId, std::set<ResourceId>> chain = {{a, {b}}, {b, {a, c}}, {c, {b}}};
  ReplayTypedAliasOwners uncertain(chain);
  uncertain.Activate(a);
  uncertain.Initialize(a, true);
  uncertain.Activate(c);
  uncertain.Initialize(c, true);
  REQUIRE(uncertain.Complete());
  uncertain.Access(b);
  uncertain.Activate(c);
  uncertain.Initialize(c, true);
  CHECK_FALSE(uncertain.Complete());
  CHECK(uncertain.unknown.count(a) == 1);
  ReplayTypedAliasOwners transitions(graph);
  transitions.Activate(a);
  transitions.Transition(a);    // owner transition before full initialization is necessary
  transitions.Initialize(a, true);
  CHECK(transitions.Complete());
  transitions.Transition(b);    // another alias's transition may touch hidden metadata
  CHECK_FALSE(transitions.Complete());
  transitions.Activate(a);
  transitions.Initialize(a, true);
  CHECK(transitions.Complete());
}

TEST_CASE("D3D12 typed owner proof rebases actual submissions and rejects hidden work",
          "[d3d12][checkpoint][typed-alias]")
{
  SDFile file;
  D3D12CommandData command;
  command.m_StructuredFile = &file;
  const ResourceId a = ResourceIDGen::GetNewUniqueID(), b = ResourceIDGen::GetNewUniqueID();
  const ResourceId source = ResourceIDGen::GetNewUniqueID(), queue = ResourceIDGen::GetNewUniqueID();
  const ResourceId first = ResourceIDGen::GetNewUniqueID(), second = ResourceIDGen::GetNewUniqueID();
  std::map<ResourceId, std::set<ResourceId>> graph = {{a, {b}}, {b, {a}}};
  command.m_Events.resize(13);
  const D3D12Chunk types[] = {D3D12Chunk::List_Reset,
                              D3D12Chunk::List_ResourceBarrier,
                              D3D12Chunk::List_ClearRenderTargetView,
                              D3D12Chunk::List_DrawInstanced,
                              D3D12Chunk::List_Close,
                              D3D12Chunk::Queue_Signal,
                              D3D12Chunk::List_Reset,
                              D3D12Chunk::List_ResourceBarrier,
                              D3D12Chunk::List_CopyResource,
                              D3D12Chunk::List_DrawInstanced,
                              D3D12Chunk::List_Close,
                              D3D12Chunk::Queue_Signal};
  for(uint32_t i = 0; i < ARRAY_COUNT(types); i++)
  {
    SDChunk *chunk = new SDChunk("typed-alias-owner-proof"_lit);
    chunk->metadata.chunkID = uint32_t(types[i]);
    file.chunks.push_back(chunk);
    command.m_Events[i + 1] = APIEvent();
    command.m_Events[i + 1].eventId = i + 1;
    command.m_Events[i + 1].chunkIndex = i;
    command.m_Events[i + 1].fileOffset = 100 + 10 * i;
  }
  command.m_ReplayTypedFrameBegin = 90;
  command.m_ReplayTypedFrameEnd = 2000;
  command.m_ReplayTypedRootComplete = true;
  for(const auto root : {D3D12CommandData::ReplayTypedRootChunk{
                            90, 100, 2, 12, D3D12Chunk::Queue_EndEvent},
                        D3D12CommandData::ReplayTypedRootChunk{
                            900, 1000, 7, 13, D3D12Chunk::Queue_ExecuteCommandLists},
                        D3D12CommandData::ReplayTypedRootChunk{
                            1900, 2000, 13, 14, D3D12Chunk::Queue_ExecuteCommandLists}})
  {
    SDChunk *chunk = new SDChunk("typed-alias-root-proof"_lit);
    chunk->metadata.chunkID = uint32_t(root.type);
    REQUIRE(file.chunks.size() == root.chunkIndex);
    file.chunks.push_back(chunk);
    command.m_ReplayTypedRootChunks.push_back(root);
  }
  command.m_CheckpointBoundaries = {{6, 7, 0, 1000}, {12, 13, 0, 2000}};
  for(uint32_t base : {2U, 8U})
  {
    const ResourceId baked = base == 2 ? first : second;
    BakedCmdListInfo &info = command.m_BakedCmdListInfo[baked];
    info.type = D3D12_COMMAND_LIST_TYPE_DIRECT;
    info.eventCount = 3;
    info.checkpointBegin = 100 + base * 10;
    info.checkpointEnd = info.checkpointBegin + 30;
    command.m_Partial[D3D12CommandData::Primary].cmdListExecs[baked] = {base};
    command.m_ReplaySubmissionQueues[base] = queue;
    for(uint32_t eid = base; eid < base + 3; eid++)
    {
      const auto &event = command.m_Events[eid];
      info.checkpointChunks.push_back(
          {baked, event.fileOffset, event.fileOffset + 10, eid - base, types[eid - 1]});
    }
    BakedCmdListInfo::CheckpointBarrierOp barrier;
    barrier.type = D3D12_RESOURCE_BARRIER_TYPE_ALIASING;
    barrier.after = base == 2 ? a : b;
    info.checkpointBarriers.push_back(barrier);
    info.replayTypedBarriers.push_back(barrier);
    BakedCmdListInfo::ReplayTextureReadOp draw;
    draw.relativeEvent = 2;
    draw.directResources = {barrier.after};
    info.replayTextureReadOps.push_back(draw);
  }
  auto &firstInfo = command.m_BakedCmdListInfo[first];
  auto &secondInfo = command.m_BakedCmdListInfo[second];
  firstInfo.replayClearOps.push_back({a, 1, true, true});
  secondInfo.RecordReplayTextureCopy({source, b, 1, true, true});
  std::set<ResourceId> owners;
  rdcstr reason;
  REQUIRE(command.GetReplayTextureAliasOwners(6, graph, owners, reason));
  CHECK(owners == std::set<ResourceId>{a});
  REQUIRE(command.GetReplayTextureAliasOwners(12, graph, owners, reason));
  CHECK(owners == std::set<ResourceId>{b});
  CHECK_FALSE(command.GetReplayTextureAliasOwners(3, graph, owners, reason));
  CHECK(owners.empty());
  secondInfo.replayTextureCopies[0].unconditional = false;
  CHECK_FALSE(command.GetReplayTextureAliasOwners(12, graph, owners, reason));
  secondInfo.replayTextureCopies[0].unconditional = true;
  secondInfo.replayTextureCopies[0].fullResource = false;
  CHECK_FALSE(command.GetReplayTextureAliasOwners(12, graph, owners, reason));
  secondInfo.replayTextureCopies[0].fullResource = true;
  secondInfo.replayTextureCopies[0].source =
      a;    // cannot inherit from the now-inactive overlapping A
  CHECK_FALSE(command.GetReplayTextureAliasOwners(12, graph, owners, reason));
  secondInfo.replayTextureCopies[0].source = source;
  secondInfo.replayTypedBarriers[0].after = ResourceId();
  CHECK_FALSE(command.GetReplayTextureAliasOwners(12, graph, owners, reason));
  secondInfo.replayTypedBarriers[0].after = b;
  secondInfo.replayClearUnsafeBarriers = {0};
  CHECK_FALSE(command.GetReplayTextureAliasOwners(12, graph, owners, reason));
  secondInfo.replayClearUnsafeBarriers.clear();
  secondInfo.replayTextureReadOps[0].directResources = {a, b};
  CHECK_FALSE(command.GetReplayTextureAliasOwners(12, graph, owners, reason));
  secondInfo.replayTextureReadOps[0].directResources = {b};
  secondInfo.replayTextureReadOps[0].unknown = true;
  CHECK_FALSE(command.GetReplayTextureAliasOwners(12, graph, owners, reason));
  secondInfo.replayTextureReadOps[0].unknown = false;
  command.m_ReplaySubmissionQueues[8] = ResourceIDGen::GetNewUniqueID();
  CHECK_FALSE(command.GetReplayTextureAliasOwners(12, graph, owners, reason));
  command.m_ReplaySubmissionQueues[8] = queue;
  command.m_Partial[D3D12CommandData::Secondary].cmdListExecs[first] = {4};
  CHECK_FALSE(command.GetReplayTextureAliasOwners(12, graph, owners, reason));
  command.m_Partial[D3D12CommandData::Secondary].cmdListExecs.clear();
  file.chunks[12]->metadata.chunkID = uint32_t(D3D12Chunk::List_DispatchRays);
  command.m_ReplayTypedRootChunks[0].type = D3D12Chunk::List_DispatchRays;
  CHECK_FALSE(command.GetReplayTextureAliasOwners(12, graph, owners, reason));
  file.chunks[12]->metadata.chunkID = uint32_t(D3D12Chunk::Queue_EndEvent);
  command.m_ReplayTypedRootChunks[0].type = D3D12Chunk::Queue_EndEvent;
  file.chunks[12]->metadata.chunkID = uint32_t(D3D12Chunk::Device_CreateRenderTargetView);
  command.m_ReplayTypedRootChunks[0].type = D3D12Chunk::Device_CreateRenderTargetView;
  REQUIRE(command.GetReplayTextureAliasOwners(12, graph, owners, reason));
  // A descriptor write is safe; a missing/mismatched root record is not silently skipped.
  file.chunks[12]->metadata.chunkID = uint32_t(D3D12Chunk::Resource_WriteToSubresource);
  CHECK_FALSE(command.GetReplayTextureAliasOwners(12, graph, owners, reason));
  command.m_ReplayTypedRootChunks[0].type = D3D12Chunk::Resource_WriteToSubresource;
  CHECK_FALSE(command.GetReplayTextureAliasOwners(12, graph, owners, reason));
  file.chunks[12]->metadata.chunkID = uint32_t(D3D12Chunk::Queue_EndEvent);
  command.m_ReplayTypedRootChunks[0].type = D3D12Chunk::Queue_EndEvent;
  const auto firstRootEvent = command.m_Events[1];
  command.m_Events[1] = APIEvent();    // Queue_EndEvent has no public APIEvent entry
  REQUIRE(command.GetReplayTextureAliasOwners(12, graph, owners, reason));
  command.m_Events[1] = firstRootEvent;
  const auto firstSubmittedEvent = command.m_Events[2];
  command.m_Events[2] = APIEvent();
  CHECK_FALSE(command.GetReplayTextureAliasOwners(12, graph, owners, reason));
  command.m_Events[2] = firstSubmittedEvent;
  command.m_ReplayTypedRootComplete = false;
  CHECK_FALSE(command.GetReplayTextureAliasOwners(12, graph, owners, reason));
  command.m_ReplayTypedRootComplete = true;
  command.m_ReplayTypedFrameEnd = 1999;
  CHECK_FALSE(command.GetReplayTextureAliasOwners(12, graph, owners, reason));
  command.m_ReplayTypedFrameEnd = 2000;
  secondInfo.replayTextureCopiesComplete = false;
  CHECK_FALSE(command.GetReplayTextureAliasOwners(12, graph, owners, reason));
  secondInfo.replayTextureCopiesComplete = true;
  REQUIRE(command.GetReplayTextureAliasOwners(12, graph, owners, reason));
  // ResourceBarrier arrays preserve their native order even when all entries share one EID.
  // Transitioning the previous valid owner before activation is safe; touching it afterward
  // may alter the newly selected owner's metadata and cannot be repaired by guessing a copy.
  BakedCmdListInfo::CheckpointBarrierOp transition;
  transition.type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
  transition.before = a;
  const auto activation = secondInfo.replayTypedBarriers[0];
  secondInfo.replayTypedBarriers = {transition, activation};
  REQUIRE(command.GetReplayTextureAliasOwners(12, graph, owners, reason));
  secondInfo.replayTypedBarriers = {activation, transition};
  CHECK_FALSE(command.GetReplayTextureAliasOwners(12, graph, owners, reason));
  secondInfo.replayTypedBarriers = {activation};
  // Earlier checkpoints remain possible before any alias component is touched at all.
  auto untouchedGraph = graph;
  const ResourceId untouchedA = ResourceIDGen::GetNewUniqueID();
  const ResourceId untouchedB = ResourceIDGen::GetNewUniqueID();
  untouchedGraph = {{untouchedA, {untouchedB}}, {untouchedB, {untouchedA}}};
  REQUIRE(command.GetReplayTextureAliasOwners(6, untouchedGraph, owners, reason));
  CHECK(owners.empty());
  untouchedGraph.insert(graph.begin(), graph.end());
  REQUIRE(command.GetReplayTextureAliasOwners(6, untouchedGraph, owners, reason));
  CHECK(owners == std::set<ResourceId>{a});
  graph[b].clear();
  CHECK_FALSE(command.GetReplayTextureAliasOwners(12, graph, owners, reason));
  command.m_StructuredFile = NULL;
}

TEST_CASE("D3D12 typed copy evidence has a sticky recording budget",
          "[d3d12][checkpoint][typed-alias]")
{
  BakedCmdListInfo info;
  BakedCmdListInfo::ReplayTextureCopyOp copy;
  for(uint32_t i = 0; i < 65536; i++)
    info.RecordReplayTextureCopy(copy);
  REQUIRE(info.replayTextureCopiesComplete);
  REQUIRE(info.replayTextureCopies.size() == 65536);
  info.RecordReplayTextureCopy(copy);
  CHECK_FALSE(info.replayTextureCopiesComplete);
  CHECK(info.replayTextureCopies.empty());
  info.RecordReplayTextureCopy(copy);
  CHECK_FALSE(info.replayTextureCopiesComplete);
  CHECK(info.replayTextureCopies.empty());
}

TEST_CASE("D3D12 discard coverage comes from an executed full pattern fill",
          "[d3d12][replay][clear-prefix]")
{
  D3D12_RESOURCE_DESC desc = {};
  desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE3D;
  desc.Width = 640;
  desc.Height = 360;
  desc.DepthOrArraySize = 128;
  desc.MipLevels = 2;
  desc.SampleDesc.Count = 1;
  desc.Format = DXGI_FORMAT_R16G16B16A16_FLOAT;
  CHECK(D3D12ReplayFullDiscardPattern(desc, NULL, true, 2));
  CHECK_FALSE(D3D12ReplayFullDiscardPattern(desc, NULL, false, 2));
  CHECK_FALSE(D3D12ReplayFullDiscardPattern(desc, NULL, true, 0));
  D3D12_DISCARD_REGION region = {};
  region.NumSubresources = 2;
  CHECK(D3D12ReplayFullDiscardPattern(desc, &region, true, 2));
  region.NumSubresources = 1;
  CHECK_FALSE(D3D12ReplayFullDiscardPattern(desc, &region, true, 2));
  region.NumSubresources = 2;
  region.FirstSubresource = 1;
  CHECK_FALSE(D3D12ReplayFullDiscardPattern(desc, &region, true, 2));
  region.FirstSubresource = 0;
  region.NumRects = 1;
  CHECK_FALSE(D3D12ReplayFullDiscardPattern(desc, &region, true, 2));
  desc.SampleDesc.Count = 4;
  CHECK_FALSE(D3D12ReplayFullDiscardPattern(desc, NULL, true, 2));
  desc.SampleDesc.Count = 1;
  desc.Format = DXGI_FORMAT_BC7_UNORM;
  CHECK_FALSE(D3D12ReplayFullDiscardPattern(desc, NULL, true, 2));
  desc.Format = DXGI_FORMAT_R32_FLOAT;
  desc.Flags = D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL;
  CHECK_FALSE(D3D12ReplayFullDiscardPattern(desc, NULL, true, 2));
}

TEST_CASE("D3D12 exact clear coverage requires every resource slice mip and plane",
          "[d3d12][replay][clear-prefix]")
{
  D3D12_RESOURCE_DESC desc = {};
  desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
  desc.Width = desc.Height = 64;
  desc.DepthOrArraySize = desc.MipLevels = 1;
  desc.SampleDesc.Count = 1;
  desc.Flags = D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;
  D3D12_RENDER_TARGET_VIEW_DESC rtv = {};
  rtv.ViewDimension = D3D12_RTV_DIMENSION_TEXTURE2D;
  CHECK(D3D12ReplayFullRTVClear(desc, rtv, 0, 1));
  CHECK_FALSE(D3D12ReplayFullRTVClear(desc, rtv, 1, 1));
  CHECK_FALSE(D3D12ReplayFullRTVClear(desc, rtv, 0, 2));
  desc.MipLevels = 2;
  CHECK_FALSE(D3D12ReplayFullRTVClear(desc, rtv, 0, 1));
  desc.MipLevels = 1;
  desc.DepthOrArraySize = 4;
  CHECK_FALSE(D3D12ReplayFullRTVClear(desc, rtv, 0, 1));
  rtv.ViewDimension = D3D12_RTV_DIMENSION_TEXTURE2DARRAY;
  rtv.Texture2DArray.ArraySize = 4;
  CHECK(D3D12ReplayFullRTVClear(desc, rtv, 0, 1));
  rtv.Texture2DArray.FirstArraySlice = 1;
  CHECK_FALSE(D3D12ReplayFullRTVClear(desc, rtv, 0, 1));
  rtv = {};
  desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE3D;
  desc.DepthOrArraySize = 128;
  rtv.ViewDimension = D3D12_RTV_DIMENSION_TEXTURE3D;
  rtv.Texture3D.WSize = 128;
  CHECK(D3D12ReplayFullRTVClear(desc, rtv, 0, 1));
  rtv.Texture3D.WSize = 1;
  CHECK_FALSE(D3D12ReplayFullRTVClear(desc, rtv, 0, 1));
  rtv.Texture3D.WSize = ~0U;
  CHECK(D3D12ReplayFullRTVClear(desc, rtv, 0, 1));

  desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
  desc.DepthOrArraySize = 1;
  desc.Flags = D3D12_RESOURCE_FLAG_ALLOW_DEPTH_STENCIL;
  D3D12_DEPTH_STENCIL_VIEW_DESC dsv = {};
  dsv.ViewDimension = D3D12_DSV_DIMENSION_TEXTURE2D;
  const D3D12_CLEAR_FLAGS both = D3D12_CLEAR_FLAG_DEPTH | D3D12_CLEAR_FLAG_STENCIL;
  CHECK(D3D12ReplayFullDSVClear(desc, dsv, D3D12_CLEAR_FLAG_DEPTH, 0, 1));
  CHECK_FALSE(D3D12ReplayFullDSVClear(desc, dsv, D3D12_CLEAR_FLAG_DEPTH, 0, 2));
  CHECK_FALSE(D3D12ReplayFullDSVClear(desc, dsv, D3D12_CLEAR_FLAG_STENCIL, 0, 2));
  CHECK(D3D12ReplayFullDSVClear(desc, dsv, both, 0, 2));
  CHECK_FALSE(D3D12ReplayFullDSVClear(desc, dsv, both, 1, 2));
  dsv.Flags = D3D12_DSV_FLAG_READ_ONLY_STENCIL;
  CHECK_FALSE(D3D12ReplayFullDSVClear(desc, dsv, both, 0, 2));
  dsv.Flags = D3D12_DSV_FLAG_NONE;
  desc.SampleDesc.Count = 4;
  CHECK_FALSE(D3D12ReplayFullDSVClear(desc, dsv, both, 0, 2));
  dsv.ViewDimension = D3D12_DSV_DIMENSION_TEXTURE2DMS;
  CHECK(D3D12ReplayFullDSVClear(desc, dsv, both, 0, 2));
}

TEST_CASE("D3D12 content barrier classification accepts only known nonsplit barriers",
          "[d3d12][replay][clear-prefix]")
{
  for(D3D12_RESOURCE_BARRIER_TYPE type : {D3D12_RESOURCE_BARRIER_TYPE_TRANSITION,
                                         D3D12_RESOURCE_BARRIER_TYPE_ALIASING,
                                         D3D12_RESOURCE_BARRIER_TYPE_UAV})
  {
    CHECK(BakedCmdListInfo::IsReplayClearBarrierReadFree(type, D3D12_RESOURCE_BARRIER_FLAG_NONE));
    for(D3D12_RESOURCE_BARRIER_FLAGS flags : {
            D3D12_RESOURCE_BARRIER_FLAG_BEGIN_ONLY, D3D12_RESOURCE_BARRIER_FLAG_END_ONLY,
            D3D12_RESOURCE_BARRIER_FLAGS(D3D12_RESOURCE_BARRIER_FLAG_BEGIN_ONLY |
                                          D3D12_RESOURCE_BARRIER_FLAG_END_ONLY),
            D3D12_RESOURCE_BARRIER_FLAGS(~0U)})
      CHECK_FALSE(BakedCmdListInfo::IsReplayClearBarrierReadFree(type, flags));
  }
  CHECK_FALSE(BakedCmdListInfo::IsReplayClearBarrierReadFree(
      D3D12_RESOURCE_BARRIER_TYPE(~0U), D3D12_RESOURCE_BARRIER_FLAG_NONE));
}

TEST_CASE("D3D12 clear prefix ends before reads unknown commands and unsafe barriers",
          "[d3d12][replay][clear-prefix]")
{
  const ResourceId resource = ResourceIDGen::GetNewUniqueID();
  BakedCmdListInfo::ReplayClearOp clear = {resource, 5, true, true};
  std::set<ResourceId> result;
  CHECK(ObserveReplayClearPrefix(D3D12Chunk::List_SetComputeRootConstantBufferView,
                                 false, NULL, {resource}, result));
  CHECK(ObserveReplayClearPrefix(D3D12Chunk::List_ResourceBarrier, false, NULL, {resource}, result));
  CHECK(ObserveReplayClearPrefix(D3D12Chunk::List_ClearRenderTargetView, false, &clear,
                                 {resource}, result));
  CHECK(result.count(resource) == 1);
  for(D3D12Chunk chunk : {D3D12Chunk::List_DrawInstanced, D3D12Chunk::List_Dispatch,
                          D3D12Chunk::List_CopyResource, D3D12Chunk::List_ResolveSubresource,
                          D3D12Chunk::List_ExecuteIndirect, D3D12Chunk::List_ExecuteBundle,
                          D3D12Chunk::List_DiscardResource, D3D12Chunk::List_BeginRenderPass,
                          D3D12Chunk::List_BuildRaytracingAccelerationStructure,
                          D3D12Chunk(~0U)})
    CHECK_FALSE(ObserveReplayClearPrefix(chunk, false, NULL, {resource}, result));
  CHECK_FALSE(ObserveReplayClearPrefix(D3D12Chunk::List_ResourceBarrier, true, NULL,
                                       {resource}, result));
  result.clear();
  clear.unconditional = false;
  CHECK_FALSE(ObserveReplayClearPrefix(D3D12Chunk::List_ClearRenderTargetView, false, &clear,
                                 {resource}, result));
  clear.unconditional = true;
  clear.fullResource = false;
  CHECK(ObserveReplayClearPrefix(D3D12Chunk::List_ClearDepthStencilView, false, &clear,
                                 {resource}, result));
  CHECK(result.empty());
  clear.fullResource = true;
  CHECK(ObserveReplayClearPrefix(D3D12Chunk::List_ClearRenderTargetView, false, &clear, {}, result));
  CHECK(result.empty());
}

TEST_CASE("D3D12 full clears are rebased to actual submissions and exclude OnlyDraw target",
          "[d3d12][replay][clear-prefix]")
{
  SDFile file;
  D3D12CommandData command;
  command.m_StructuredFile = &file;
  const ResourceId baked = ResourceIDGen::GetNewUniqueID();
  const ResourceId resource = ResourceIDGen::GetNewUniqueID();
  BakedCmdListInfo &info = command.m_BakedCmdListInfo[baked];
  info.type = D3D12_COMMAND_LIST_TYPE_DIRECT;
  info.eventCount = 5;
  info.checkpointBegin = 100;
  info.checkpointEnd = 160;
  info.replayClearOps.push_back({resource, 2, true, true});
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[baked] = {12, 32};
  const ResourceId queue = ResourceIDGen::GetNewUniqueID();
  command.m_ReplaySubmissionQueues = {{12, queue}, {32, queue}};
  command.m_CheckpointBoundaries.push_back({10, 11, 0, 50});
  command.m_CheckpointBoundaries.push_back({30, 31, 0, 60});
  command.m_Events.resize(37);
  const D3D12Chunk chunks[] = {D3D12Chunk::List_Reset, D3D12Chunk::List_SetPipelineState,
                              D3D12Chunk::List_ResourceBarrier,
                              D3D12Chunk::List_ClearRenderTargetView,
                              D3D12Chunk::List_DrawInstanced, D3D12Chunk::List_Close};
  for(uint32_t i = 0; i < 6; i++)
  {
    SDChunk *chunk = new SDChunk("clear-proof"_lit);
    chunk->metadata.chunkID = uint32_t(chunks[i]);
    file.chunks.push_back(chunk);
    for(uint32_t first : {11U, 31U})
    {
      APIEvent event;
      event.eventId = first + i;
      event.chunkIndex = i;
      event.fileOffset = 100 + i * 10;
      command.m_Events[event.eventId] = event;
    }
    info.checkpointChunks.push_back({baked, 100 + i * 10, 110 + i * 10, i, chunks[i]});
  }
  // A real submission checkpoint names its synthetic Close at base + eventCount. The
  // preceding list must be excluded before requiring a suffix occurrence to start after it.
  const ResourceId previous = ResourceIDGen::GetNewUniqueID();
  BakedCmdListInfo &prefix = command.m_BakedCmdListInfo[previous];
  prefix.type = D3D12_COMMAND_LIST_TYPE_DIRECT;
  prefix.eventCount = 7;
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[previous] = {3};
  CHECK(command.GetReplayFullClearPrefix(10, 14, {resource}).count(resource) == 1);
  CHECK(command.GetReplayFullClearPrefix(10, 13, {resource}).empty());
  CHECK(command.GetReplayFullClearPrefix(10, 14, {resource}).count(resource) == 1);
  CHECK(command.GetReplayFullClearPrefix(10, 15, {resource}).count(resource) == 1);
  CHECK(command.GetReplayFullClearPrefix(30, 34, {resource}).count(resource) == 1);
  CHECK(command.GetReplayFullClearPrefix(29, 34, {resource}).empty());
  command.m_ReplaySubmissionQueues[32] = ResourceIDGen::GetNewUniqueID();
  CHECK(command.GetReplayFullClearPrefix(10, 34, {resource}).empty());
  CHECK(command.GetReplayFullClearPrefix(30, 34, {resource}).count(resource) == 1);
  command.m_ReplaySubmissionQueues[32] = queue;
  command.m_ReplaySubmissionQueues.erase(12);
  CHECK(command.GetReplayFullClearPrefix(10, 14, {resource}).empty());
  command.m_ReplaySubmissionQueues[12] = ResourceId();
  CHECK(command.GetReplayFullClearPrefix(10, 14, {resource}).empty());
  command.m_ReplaySubmissionQueues[12] = queue;
  BakedCmdListInfo::CheckpointBarrierOp alias;
  alias.relativeEvent = 1;
  alias.type = D3D12_RESOURCE_BARRIER_TYPE_ALIASING;
  info.checkpointBarriers.push_back(alias);
  // NULL/NULL is a wildcard over placed/reserved storage. The caller's independent committed
  // texture is unaffected, while an omitted or different eligible set authorizes nothing.
  CHECK(command.GetReplayFullClearPrefix(10, 15, {resource}).count(resource) == 1);
  CHECK(command.GetReplayFullClearPrefix(10, 15, {}).empty());
  CHECK(command.GetReplayFullClearPrefix(10, 15, {baked}).empty());
  info.checkpointBarriers[0].before = baked;
  info.checkpointBarriers[0].after = previous;
  CHECK(command.GetReplayFullClearPrefix(10, 15, {resource}).count(resource) == 1);
  // Every member of the same ResourceBarrier call contributes. An alias does not erase a
  // split/unsupported operation, regardless of array order or which resource it names.
  BakedCmdListInfo::CheckpointBarrierOp split;
  split.relativeEvent = alias.relativeEvent;
  split.before = baked;
  split.flags = D3D12_RESOURCE_BARRIER_FLAG_BEGIN_ONLY;
  info.checkpointBarriers.push_back(split);
  CHECK(command.GetReplayFullClearPrefix(10, 15, {resource}).empty());
  std::swap(info.checkpointBarriers[0], info.checkpointBarriers[1]);
  CHECK(command.GetReplayFullClearPrefix(10, 15, {resource}).empty());
  info.checkpointBarriers = {alias};
  info.checkpointBarriers[0].flags = D3D12_RESOURCE_BARRIER_FLAG_END_ONLY;
  CHECK(command.GetReplayFullClearPrefix(10, 15, {resource}).empty());
  info.checkpointBarriers = {alias};
  // Loading separately records unsafe native union types/flags that the legacy checkpoint
  // barrier index does not describe, including a filtered NULL split transition.
  info.replayClearUnsafeBarriers = {alias.relativeEvent};
  CHECK(command.GetReplayFullClearPrefix(10, 15, {resource}).empty());
  info.replayClearUnsafeBarriers = {info.eventCount};
  CHECK(command.GetReplayFullClearPrefix(10, 15, {resource}).empty());
  info.replayClearUnsafeBarriers.clear();
  CHECK(command.GetReplayFullClearPrefix(10, 15, {resource}).count(resource) == 1);
  info.checkpointBarriers.clear();
  info.replayClearOps[0].unconditional = false;
  CHECK(command.GetReplayFullClearPrefix(30, 34, {resource}).empty());
  info.replayClearOps[0].unconditional = true;
  command.m_Events[33].fileOffset++;
  CHECK(command.GetReplayFullClearPrefix(30, 34, {resource}).empty());
  command.m_StructuredFile = NULL;
}

TEST_CASE("D3D12 texture read proof includes complete descriptor ranges and unknown bindings",
          "[d3d12][replay][clear-prefix]")
{
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  const ResourceId direct = ResourceIDGen::GetNewUniqueID();
  const ResourceId historical = ResourceIDGen::GetNewUniqueID();
  const ResourceId unrelated = ResourceIDGen::GetNewUniqueID();
  BakedCmdListInfo::ReplayTextureReadOp read;
  read.directResources.push_back(direct);
  REQUIRE(AddReplayTextureReadRange(read, heap, 20, 4, 2, 3));
  REQUIRE(read.descriptorRanges.size() == 1);
  CHECK(read.descriptorRanges[0].first == 6);
  CHECK(read.descriptorRanges[0].count == 3);
  uint32_t queried = 0;
  const auto history = [&](const BakedCmdListInfo::ReplayUAVRange &range, ResourceId target) {
    queried++;
    return range.heap == heap && range.first <= 7 && range.first + range.count > 7 &&
           target == historical;
  };
  CHECK(ReplayTextureReadMayReference(read, direct, history));
  CHECK(queried == 0);
  CHECK(ReplayTextureReadMayReference(read, historical, history));
  CHECK_FALSE(ReplayTextureReadMayReference(read, unrelated, history));
  read.unknown = true;
  CHECK(ReplayTextureReadMayReference(read, unrelated, history));
  read.unknown = false;
  CHECK(ReplayTextureReadMayReference(read, unrelated,
        [](const BakedCmdListInfo::ReplayUAVRange &, ResourceId) { return true; }));

  BakedCmdListInfo::ReplayTextureReadOp unbounded;
  REQUIRE(AddReplayTextureReadRange(unbounded, heap, 20, 4, 2, UINT_MAX));
  CHECK(unbounded.descriptorRanges[0].first == 6);
  CHECK(unbounded.descriptorRanges[0].count == 14);
  CHECK_FALSE(AddReplayTextureReadRange(unbounded, heap, 20, UINT64_MAX, 0, 1));
  CHECK_FALSE(AddReplayTextureReadRange(unbounded, heap, 20, 4, UINT64_MAX, 1));
  CHECK_FALSE(AddReplayTextureReadRange(unbounded, heap, 20, 4, 2, 15));
  CHECK_FALSE(AddReplayTextureReadRange(unbounded, ResourceId(), 20, 0, 0, 1));

  BakedCmdListInfo bounded;
  bounded.replayTextureReadEntries = ReplayTextureReadMetadataLimit - 1;
  StoreReplayTextureRead(bounded, {});
  CHECK(bounded.replayTextureReadsComplete);
  REQUIRE(bounded.replayTextureReadOps.size() == 1);
  StoreReplayTextureRead(bounded, {});
  CHECK_FALSE(bounded.replayTextureReadsComplete);
  CHECK(bounded.replayTextureReadOps.empty());
  StoreReplayTextureRead(bounded, {});
  CHECK(bounded.replayTextureReadOps.empty());
}

TEST_CASE("D3D12 full clears can follow unrelated shader work but never an earlier resource read",
          "[d3d12][replay][clear-prefix]")
{
  SDFile file;
  D3D12CommandData command;
  command.m_StructuredFile = &file;
  const ResourceId baked = ResourceIDGen::GetNewUniqueID();
  const ResourceId a = ResourceIDGen::GetNewUniqueID();
  const ResourceId b = ResourceIDGen::GetNewUniqueID();
  const ResourceId c = ResourceIDGen::GetNewUniqueID();
  BakedCmdListInfo &info = command.m_BakedCmdListInfo[baked];
  info.type = D3D12_COMMAND_LIST_TYPE_DIRECT;
  info.eventCount = 7;
  info.checkpointBegin = 100;
  info.checkpointEnd = 190;
  info.replayClearOps = {{a, 1, true, true}, {c, 3, true, true}, {b, 4, true, true},
                         {a, 6, true, true}};
  BakedCmdListInfo::ReplayTextureReadOp draw, dispatch, laterDraw;
  draw.relativeEvent = 0;
  draw.directResources = {a};
  dispatch.relativeEvent = 2;
  dispatch.directResources = {b};
  laterDraw.relativeEvent = 5;
  laterDraw.directResources = {c};
  info.replayTextureReadOps = {draw, dispatch, laterDraw};
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[baked] = {12};
  command.m_ReplaySubmissionQueues[12] = ResourceIDGen::GetNewUniqueID();
  command.m_CheckpointBoundaries.push_back({10, 11, 0, 50});
  const D3D12Chunk chunks[] = {D3D12Chunk::List_Reset, D3D12Chunk::List_DrawInstanced,
                              D3D12Chunk::List_ClearRenderTargetView, D3D12Chunk::List_Dispatch,
                              D3D12Chunk::List_ClearDepthStencilView,
                              D3D12Chunk::List_ClearRenderTargetView,
                              D3D12Chunk::List_DrawIndexedInstanced,
                              D3D12Chunk::List_ClearRenderTargetView, D3D12Chunk::List_Close};
  command.m_Events.resize(20);
  for(uint32_t i = 0; i < ARRAY_COUNT(chunks); i++)
  {
    SDChunk *chunk = new SDChunk("per-resource-clear-proof"_lit);
    chunk->metadata.chunkID = uint32_t(chunks[i]);
    file.chunks.push_back(chunk);
    APIEvent event;
    event.eventId = 11 + i;
    event.chunkIndex = i;
    event.fileOffset = 100 + i * 10;
    command.m_Events[event.eventId] = event;
    info.checkpointChunks.push_back({baked, event.fileOffset, event.fileOffset + 10, i, chunks[i]});
  }
  CHECK(command.GetReplayFullClearPrefix(10, 14, {a, b, c}).empty());
  CHECK((command.GetReplayFullClearPrefix(10, 15, {a, b, c}) == std::set<ResourceId>({c})));
  CHECK((command.GetReplayFullClearPrefix(10, 18, {a, b, c}) == std::set<ResourceId>({c})));
  // Binding C before its clear removes only C; an unrelated A clear is still eligible if A was
  // not consumed. Reading C after its clear does not revoke the already-established proof.
  info.replayTextureReadOps[0].directResources = {c};
  CHECK((command.GetReplayFullClearPrefix(10, 18, {a, b, c}) == std::set<ResourceId>({a})));
  info.replayTextureReadOps[0].directResources = {a};
  info.replayTextureReadOps[1].unknown = true;
  CHECK(command.GetReplayFullClearPrefix(10, 18, {a, b, c}).empty());
  info.replayTextureReadOps[1].unknown = false;
  info.replayTextureReadsComplete = false;
  CHECK(command.GetReplayFullClearPrefix(10, 18, {a, b, c}).empty());
  info.replayTextureReadsComplete = true;
  info.replayTextureReadOps[1].relativeEvent++;
  CHECK(command.GetReplayFullClearPrefix(10, 18, {a, b, c}).empty());
  info.replayTextureReadOps[1].relativeEvent--;
  // A copy's source and destination dependency is not represented by shader bindings.
  file.chunks[3]->metadata.chunkID = uint32_t(D3D12Chunk::List_CopyResource);
  info.checkpointChunks[3].type = D3D12Chunk::List_CopyResource;
  CHECK(command.GetReplayFullClearPrefix(10, 18, {a, b, c}).empty());
  command.m_StructuredFile = NULL;
}

TEST_CASE("D3D12 texture overwrite proof crosses query boundaries but preserves draw reads",
          "[d3d12][replay][clear-prefix]")
{
  SDFile file;
  D3D12CommandData command;
  command.m_StructuredFile = &file;
  const ResourceId baked = ResourceIDGen::GetNewUniqueID();
  const ResourceId texture = ResourceIDGen::GetNewUniqueID();
  const ResourceId unrelated = ResourceIDGen::GetNewUniqueID();
  BakedCmdListInfo &info = command.m_BakedCmdListInfo[baked];
  info.type = D3D12_COMMAND_LIST_TYPE_DIRECT;
  info.eventCount = 4;
  info.checkpointBegin = 100;
  info.checkpointEnd = 160;
  info.replayClearOps.push_back({texture, 3, true, true});
  command.m_Partial[D3D12CommandData::Primary].cmdListExecs[baked] = {12};
  command.m_ReplaySubmissionQueues[12] = ResourceIDGen::GetNewUniqueID();
  command.m_CheckpointBoundaries.push_back({10, 11, 0, 50});
  const D3D12Chunk chunks[] = {D3D12Chunk::List_Reset, D3D12Chunk::List_BeginQuery,
                              D3D12Chunk::List_SetPipelineState, D3D12Chunk::List_EndQuery,
                              D3D12Chunk::List_ClearRenderTargetView, D3D12Chunk::List_Close};
  command.m_Events.resize(17);
  for(uint32_t i = 0; i < ARRAY_COUNT(chunks); i++)
  {
    SDChunk *chunk = new SDChunk("query-clear-proof"_lit);
    chunk->metadata.chunkID = uint32_t(chunks[i]);
    file.chunks.push_back(chunk);
    APIEvent event;
    event.eventId = 11 + i;
    event.chunkIndex = i;
    event.fileOffset = 100 + i * 10;
    command.m_Events[event.eventId] = event;
    info.checkpointChunks.push_back({baked, event.fileOffset, event.fileOffset + 10, i, chunks[i]});
  }
  CHECK(command.GetReplayFullClearPrefix(10, 15, {texture}).count(texture) == 1);
  CHECK(command.GetReplayFullClearPrefix(10, 14, {texture}).empty());
  BakedCmdListInfo::ReplayTextureReadOp draw;
  draw.relativeEvent = 1;
  draw.directResources = {texture};
  info.replayTextureReadOps = {draw};
  file.chunks[2]->metadata.chunkID = uint32_t(D3D12Chunk::List_DrawInstanced);
  info.checkpointChunks[2].type = D3D12Chunk::List_DrawInstanced;
  CHECK(command.GetReplayFullClearPrefix(10, 15, {texture}).empty());
  info.replayTextureReadOps[0].directResources = {unrelated};
  CHECK(command.GetReplayFullClearPrefix(10, 15, {texture}).count(texture) == 1);
  info.replayTextureReadOps[0].unknown = true;
  CHECK(command.GetReplayFullClearPrefix(10, 15, {texture}).empty());
  info.replayTextureReadOps[0].unknown = false;
  for(D3D12Chunk blocker : {D3D12Chunk::List_ResolveQueryData, D3D12Chunk::List_CopyResource,
                             D3D12Chunk::List_SetProgram})
  {
    file.chunks[3]->metadata.chunkID = uint32_t(blocker);
    info.checkpointChunks[3].type = blocker;
    CHECK(command.GetReplayFullClearPrefix(10, 15, {texture}).empty());
  }
  file.chunks[3]->metadata.chunkID = uint32_t(D3D12Chunk::List_ResourceBarrier);
  info.checkpointChunks[3].type = D3D12Chunk::List_ResourceBarrier;
  BakedCmdListInfo::CheckpointBarrierOp barrier;
  barrier.relativeEvent = 2;
  barrier.type = D3D12_RESOURCE_BARRIER_TYPE_ALIASING;
  info.checkpointBarriers.push_back(barrier);
  CHECK(command.GetReplayFullClearPrefix(10, 15, {texture}).count(texture) == 1);
  // The alias does not reset prior reads: this earlier draw observes the old texture, so its
  // later full clear cannot justify dropping the checkpoint/initial content copy.
  info.replayTextureReadOps[0].directResources = {texture};
  CHECK(command.GetReplayFullClearPrefix(10, 15, {texture}).empty());
  info.replayTextureReadOps[0].directResources = {unrelated};
  CHECK(command.GetReplayFullClearPrefix(10, 15, {texture}).count(texture) == 1);
  command.m_StructuredFile = NULL;
}

TEST_CASE("D3D12 executable UAV bounds preserve spaces visibility and entire arrays",
          "[d3d12][replay][uav-declarations]")
{
  ReplayPSOUAVProof proof;
  proof.complete = proof.graphics = true;
  proof.bindings = {{ShaderStage::Pixel, 7, 4, 3}, {ShaderStage::Pixel, 7, 6, 4},
                    {ShaderStage::Vertex, 7, 20, 2}, {ShaderStage::Pixel, 8, 12, 1}};
  D3D12_DESCRIPTOR_RANGE1 range = {};
  range.RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_UAV;
  range.BaseShaderRegister = 2;
  range.RegisterSpace = 7;
  range.NumDescriptors = 32;
  rdcarray<rdcpair<uint32_t, uint32_t>> selected;
  REQUIRE(ReplayUAVTableDeclarationRanges(&proof, true, true, D3D12_ROOT_SIGNATURE_FLAG_NONE,
                                          D3D12_SHADER_VISIBILITY_PIXEL, range, selected));
  REQUIRE(selected.size() == 1);
  CHECK(selected[0].first == 2);
  CHECK(selected[0].second == 6);
  REQUIRE(ReplayUAVTableDeclarationRanges(&proof, true, true, D3D12_ROOT_SIGNATURE_FLAG_NONE,
                                          D3D12_SHADER_VISIBILITY_ALL, range, selected));
  REQUIRE(selected.size() == 2);
  CHECK(selected[1].first == 18);
  CHECK(selected[1].second == 2);
  range.RegisterSpace = 8;
  REQUIRE(ReplayUAVTableDeclarationRanges(&proof, true, true, D3D12_ROOT_SIGNATURE_FLAG_NONE,
                                          D3D12_SHADER_VISIBILITY_PIXEL, range, selected));
  REQUIRE(selected.size() == 1);
  CHECK(selected[0].first == 10);
  CHECK(selected[0].second == 1);
  range.RegisterSpace = 99;
  CHECK(ReplayUAVTableDeclarationRanges(&proof, true, true, D3D12_ROOT_SIGNATURE_FLAG_NONE,
                                        D3D12_SHADER_VISIBILITY_ALL, range, selected));
  CHECK(selected.empty());
  proof.graphics = false;
  proof.bindings = {{ShaderStage::Compute, 7, 4, 8}};
  range.RegisterSpace = 7;
  range.BaseShaderRegister = 8;
  range.NumDescriptors = 16;
  REQUIRE(ReplayUAVTableDeclarationRanges(&proof, false, true, D3D12_ROOT_SIGNATURE_FLAG_NONE,
                                          D3D12_SHADER_VISIBILITY_ALL, range, selected));
  REQUIRE(selected.size() == 1);
  CHECK(selected[0].first == 0);
  CHECK(selected[0].second == 4);
  CHECK_FALSE(ReplayUAVVisibilityMatches(D3D12_SHADER_VISIBILITY_PIXEL, ShaderStage::Compute));
  CHECK(ReplayUAVVisibilityMatches(D3D12_SHADER_VISIBILITY_MESH, ShaderStage::Mesh));
}

TEST_CASE("D3D12 unproved UAV access always keeps the original range",
          "[d3d12][replay][uav-declarations]")
{
  ReplayPSOUAVProof proof;
  proof.complete = true;
  proof.bindings = {{ShaderStage::Compute, 0, 0, 1}};
  D3D12_DESCRIPTOR_RANGE1 range = {};
  range.RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_UAV;
  range.NumDescriptors = 64;
  rdcarray<rdcpair<uint32_t, uint32_t>> selected;
  const auto select = [&](const ReplayPSOUAVProof *p, bool graphics, bool direct,
                          D3D12_ROOT_SIGNATURE_FLAGS flags) {
    return ReplayUAVTableDeclarationRanges(p, graphics, direct, flags,
                                            D3D12_SHADER_VISIBILITY_ALL, range, selected);
  };
  CHECK_FALSE(select(NULL, false, true, D3D12_ROOT_SIGNATURE_FLAG_NONE));
  proof.complete = false;
  CHECK_FALSE(select(&proof, false, true, D3D12_ROOT_SIGNATURE_FLAG_NONE));
  proof.complete = true;
  CHECK_FALSE(select(&proof, true, true, D3D12_ROOT_SIGNATURE_FLAG_NONE));
  CHECK_FALSE(select(&proof, false, false, D3D12_ROOT_SIGNATURE_FLAG_NONE));
  CHECK_FALSE(select(&proof, false, true,
                     D3D12_ROOT_SIGNATURE_FLAG_CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED));
  CHECK_FALSE(select(&proof, false, true,
                     D3D12_ROOT_SIGNATURE_FLAG_SAMPLER_HEAP_DIRECTLY_INDEXED));
  range.NumDescriptors = UINT_MAX;
  CHECK_FALSE(select(&proof, false, true, D3D12_ROOT_SIGNATURE_FLAG_NONE));
  range.NumDescriptors = 64;
  proof.bindings[0].count = UINT_MAX;
  CHECK_FALSE(select(&proof, false, true, D3D12_ROOT_SIGNATURE_FLAG_NONE));
  proof.bindings[0].count = 1;
  range.BaseShaderRegister = UINT_MAX - 2;
  CHECK_FALSE(select(&proof, false, true, D3D12_ROOT_SIGNATURE_FLAG_NONE));
  range.BaseShaderRegister = 0;
  range.RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_SRV;
  CHECK_FALSE(select(&proof, false, true, D3D12_ROOT_SIGNATURE_FLAG_NONE));
  CHECK_FALSE(ReplayFiniteUAVBinding(0, 0));
  CHECK_FALSE(ReplayFiniteUAVBinding(0, UINT_MAX));
  CHECK(ReplayFiniteUAVBinding(UINT_MAX, 1));
  CHECK_FALSE(ReplayFiniteUAVBinding(UINT_MAX, 2));

  CHECK(ReplayUseDeclaredUAVIndex(true, false, true));
  CHECK_FALSE(ReplayUseDeclaredUAVIndex(false, false, true));
  CHECK_FALSE(ReplayUseDeclaredUAVIndex(true, true, true));
  CHECK_FALSE(ReplayUseDeclaredUAVIndex(true, false, false));
}

TEST_CASE("D3D12 indirect UAV declaration proof requires one known final operation",
          "[d3d12][replay][uav-declarations]")
{
  CHECK(ReplayUAVDeclarationBindingsKnown(D3D12Chunk::List_DrawInstanced));
  CHECK(ReplayUAVDeclarationBindingsKnown(D3D12Chunk::List_DrawIndexedInstanced));
  CHECK(ReplayUAVDeclarationBindingsKnown(D3D12Chunk::List_Dispatch));
  CHECK(ReplayUAVDeclarationBindingsKnown(D3D12Chunk::List_DispatchMesh));
  CHECK_FALSE(ReplayUAVDeclarationBindingsKnown(D3D12Chunk::List_ExecuteIndirect));
  CHECK_FALSE(ReplayUAVDeclarationBindingsKnown(D3D12Chunk::List_SetPipelineState));
  for(D3D12_INDIRECT_ARGUMENT_TYPE terminal : {D3D12_INDIRECT_ARGUMENT_TYPE_DRAW,
                                               D3D12_INDIRECT_ARGUMENT_TYPE_DRAW_INDEXED,
                                               D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH,
                                               D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH_MESH})
  {
    D3D12CommandSignature signature;
    signature.graphics = terminal != D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH;
    D3D12_INDIRECT_ARGUMENT_DESC argument = {};
    // Generated CBV/SRV addresses and values can influence every array element, but cannot
    // introduce an undeclared UAV register or change its descriptor-table base.
    for(D3D12_INDIRECT_ARGUMENT_TYPE state : {D3D12_INDIRECT_ARGUMENT_TYPE_CONSTANT,
                                              D3D12_INDIRECT_ARGUMENT_TYPE_CONSTANT_BUFFER_VIEW,
                                              D3D12_INDIRECT_ARGUMENT_TYPE_SHADER_RESOURCE_VIEW})
    {
      argument.Type = state;
      signature.arguments.push_back(argument);
    }
    argument.Type = terminal;
    signature.arguments.push_back(argument);
    REQUIRE(ReplayUAVDeclarationBindingsKnown(D3D12Chunk::List_ExecuteIndirect, &signature));
    ReplayPSOUAVProof proof;
    proof.complete = true;
    proof.graphics = signature.graphics;
    proof.bindings = {{signature.graphics ? ShaderStage::Pixel : ShaderStage::Compute, 7, 4, 8}};
    D3D12_DESCRIPTOR_RANGE1 range = {};
    range.RangeType = D3D12_DESCRIPTOR_RANGE_TYPE_UAV;
    range.BaseShaderRegister = 2;
    range.RegisterSpace = 7;
    range.NumDescriptors = 64;
    rdcarray<rdcpair<uint32_t, uint32_t>> selected;
    REQUIRE(ReplayUAVTableDeclarationRanges(
        &proof, signature.graphics,
        ReplayUAVDeclarationBindingsKnown(D3D12Chunk::List_ExecuteIndirect, &signature),
        D3D12_ROOT_SIGNATURE_FLAG_NONE, D3D12_SHADER_VISIBILITY_ALL, range, selected));
    REQUIRE(selected.size() == 1);
    CHECK(selected[0].first == 2);
    CHECK(selected[0].second == 8);
    signature.raytraced = true;
    CHECK_FALSE(ReplayUAVDeclarationBindingsKnown(D3D12Chunk::List_ExecuteIndirect, &signature));
    signature.raytraced = false;
    signature.graphics = !signature.graphics;
    CHECK_FALSE(ReplayUAVDeclarationBindingsKnown(D3D12Chunk::List_ExecuteIndirect, &signature));
    signature.graphics = !signature.graphics;
    signature.arguments[0].Type = D3D12_INDIRECT_ARGUMENT_TYPE_UNORDERED_ACCESS_VIEW;
    CHECK_FALSE(ReplayUAVDeclarationBindingsKnown(D3D12Chunk::List_ExecuteIndirect, &signature));
    signature.arguments[0].Type = D3D12_INDIRECT_ARGUMENT_TYPE(~0U);
    CHECK_FALSE(ReplayUAVDeclarationBindingsKnown(D3D12Chunk::List_ExecuteIndirect, &signature));
    signature.arguments[0].Type = D3D12_INDIRECT_ARGUMENT_TYPE_CONSTANT;
    signature.arguments.push_back(argument);
    CHECK_FALSE(ReplayUAVDeclarationBindingsKnown(D3D12Chunk::List_ExecuteIndirect, &signature));
    signature.arguments.back().Type = terminal == D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH
                                         ? D3D12_INDIRECT_ARGUMENT_TYPE_DRAW
                                         : D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH;
    CHECK_FALSE(ReplayUAVDeclarationBindingsKnown(D3D12Chunk::List_ExecuteIndirect, &signature));
    signature.arguments.back().Type = D3D12_INDIRECT_ARGUMENT_TYPE_CONSTANT;
    CHECK_FALSE(ReplayUAVDeclarationBindingsKnown(D3D12Chunk::List_ExecuteIndirect, &signature));
    signature.arguments.clear();
    CHECK_FALSE(ReplayUAVDeclarationBindingsKnown(D3D12Chunk::List_ExecuteIndirect, &signature));
    argument.Type = D3D12_INDIRECT_ARGUMENT_TYPE_CONSTANT_BUFFER_VIEW;
    signature.arguments.push_back(argument);
    CHECK_FALSE(ReplayUAVDeclarationBindingsKnown(D3D12Chunk::List_ExecuteIndirect, &signature));
  }
  D3D12CommandSignature indexed;
  for(D3D12_INDIRECT_ARGUMENT_TYPE type : {D3D12_INDIRECT_ARGUMENT_TYPE_VERTEX_BUFFER_VIEW,
                                           D3D12_INDIRECT_ARGUMENT_TYPE_INDEX_BUFFER_VIEW,
                                           D3D12_INDIRECT_ARGUMENT_TYPE_DRAW_INDEXED})
  {
    D3D12_INDIRECT_ARGUMENT_DESC argument = {};
    argument.Type = type;
    indexed.arguments.push_back(argument);
  }
  CHECK(ReplayUAVDeclarationBindingsKnown(D3D12Chunk::List_ExecuteIndirect, &indexed));
}

TEST_CASE("D3D12 declaration proof requires a complete executable token stream",
          "[d3d12][replay][uav-declarations]")
{
  uint32_t declarations = ~0U;
  rdcarray<uint32_t> tokens = {0x50050, 3, (1U << 24) | DXBCBytecode::OPCODE_RET};
  CHECK(ReplayDXBCTokenDeclarations(tokens, declarations));
  CHECK(declarations == 0);
  tokens[1] = 4;
  CHECK_FALSE(ReplayDXBCTokenDeclarations(tokens, declarations));
  tokens[1] = 3;
  tokens[2] = DXBCBytecode::OPCODE_RET;
  CHECK_FALSE(ReplayDXBCTokenDeclarations(tokens, declarations));
  tokens[2] = (2U << 24) | DXBCBytecode::OPCODE_RET;
  CHECK_FALSE(ReplayDXBCTokenDeclarations(tokens, declarations));
  for(uint32_t opcode : {uint32_t(DXBCBytecode::OPCODE_RESERVED0),
                         uint32_t(DXBCBytecode::OPCODE_DCL_INTERFACE),
                         uint32_t(DXBCBytecode::NUM_REAL_OPCODES), 0x7ffU})
  {
    tokens[2] = (1U << 24) | opcode;
    CHECK_FALSE(ReplayDXBCTokenDeclarations(tokens, declarations));
  }
  // This token walker verifies framing/count, not operand contents. The native caller then
  // requires the actual decoder's UAV declaration count and exact reflected binding ranges.
  tokens = {0x50050, 6, (2U << 24) | DXBCBytecode::OPCODE_DCL_UNORDERED_ACCESS_VIEW_RAW,
             0, (1U << 24) | DXBCBytecode::OPCODE_RET,
             (1U << 24) | DXBCBytecode::OPCODE_RET};
  CHECK(ReplayDXBCTokenDeclarations(tokens, declarations));
  CHECK(declarations == 1);
  tokens = {0x50050, 5, DXBCBytecode::OPCODE_CUSTOMDATA, 2,
             (1U << 24) | DXBCBytecode::OPCODE_RET};
  CHECK(ReplayDXBCTokenDeclarations(tokens, declarations));
  tokens[3] = 0;
  CHECK_FALSE(ReplayDXBCTokenDeclarations(tokens, declarations));
  tokens[3] = 20;
  CHECK_FALSE(ReplayDXBCTokenDeclarations(tokens, declarations));
  uint32_t value = 0;
  CHECK_FALSE(ReplayDXILMetadataUint(NULL, value));
  DXIL::Metadata metadata;
  CHECK_FALSE(ReplayDXILMetadataUint(&metadata, value));
}

static bytebuf ReplayUAVTestContainer(
    const rdcarray<rdcpair<uint32_t, rdcarray<uint32_t>>> &chunks)
{
  rdcarray<uint32_t> words;
  words.resize(8 + chunks.size());
  words[0] = DXBC::FOURCC_DXBC;
  words[5] = 1;
  words[7] = uint32_t(chunks.size());
  for(size_t i = 0; i < chunks.size(); i++)
  {
    words[8 + i] = uint32_t(words.byteSize());
    words.push_back(chunks[i].first);
    words.push_back(uint32_t(chunks[i].second.byteSize()));
    words.append(chunks[i].second);
  }
  words[6] = uint32_t(words.byteSize());
  bytebuf bytes;
  bytes.assign((const byte *)words.data(), words.byteSize());
  return bytes;
}

TEST_CASE("D3D12 UAV proofs read original executable bytes despite conflicting debug programs",
          "[d3d12][replay][uav-declarations]")
{
  // Actual CS 5.0 dcl_uav_raw u3; ret. The unrelated debug program declares no UAVs. This
  // container intentionally has no HASH: neither external debug matching nor ILDB is trusted.
  const rdcarray<uint32_t> executable = {
      0x50050, 6, (3U << 24) | DXBCBytecode::OPCODE_DCL_UNORDERED_ACCESS_VIEW_RAW,
      (1U << 20) | (uint32_t(DXBCBytecode::TYPE_UNORDERED_ACCESS_VIEW) << 12), 3,
      (1U << 24) | DXBCBytecode::OPCODE_RET};
  const rdcarray<uint32_t> debug = {0x50050, 3, (1U << 24) | DXBCBytecode::OPCODE_RET};
  for(bool debugFirst : {false, true})
  {
    rdcarray<rdcpair<uint32_t, rdcarray<uint32_t>>> chunks = {
        {DXBC::FOURCC_SHDR, executable}, {DXBC::FOURCC_ILDB, debug}};
    if(debugFirst)
      std::swap(chunks[0], chunks[1]);
    bytebuf bytes = ReplayUAVTestContainer(chunks);
    ReplayOriginalExecutable selected;
    REQUIRE(ReplayFindOriginalExecutable(bytes.data(), bytes.size(), selected));
    CHECK(selected.fourcc == DXBC::FOURCC_SHDR);
    REQUIRE(selected.length == executable.byteSize());
    CHECK(memcmp(selected.bytes, executable.data(), executable.byteSize()) == 0);
    rdcarray<ReplayShaderUAVBinding> declared;
    REQUIRE(ReplayOriginalUAVDeclarations(bytes.data(), bytes.size(), ShaderStage::Compute,
                                           declared) == NULL);
    REQUIRE(declared.size() == 1);
    CHECK(declared[0].first == 3);
    CHECK(declared[0].count == 1);
    CHECK(declared[0].space == 0);
    ShaderReflection debugReflection;
    debugReflection.stage = ShaderStage::Compute;
    CHECK_FALSE(ReplayUAVReflectionMatches(debugReflection, ShaderStage::Compute, declared));
    debugReflection.readWriteResources.resize(1);
    debugReflection.readWriteResources[0].fixedBindNumber = 3;
    debugReflection.readWriteResources[0].fixedBindSetOrSpace = 0;
    debugReflection.readWriteResources[0].bindArraySize = 1;
    CHECK(ReplayUAVReflectionMatches(debugReflection, ShaderStage::Compute, declared));
    debugReflection.readWriteResources[0].fixedBindNumber = 2;
    CHECK_FALSE(ReplayUAVReflectionMatches(debugReflection, ShaderStage::Compute, declared));
  }
  ReplayOriginalExecutable selected;
  bytebuf bytes = ReplayUAVTestContainer({{DXBC::FOURCC_ILDB, executable}});
  CHECK_FALSE(ReplayFindOriginalExecutable(bytes.data(), bytes.size(), selected));
  bytes = ReplayUAVTestContainer({{DXBC::FOURCC_SHDR, executable}, {DXBC::FOURCC_SHEX, debug}});
  CHECK_FALSE(ReplayFindOriginalExecutable(bytes.data(), bytes.size(), selected));
  bytes = ReplayUAVTestContainer({{DXBC::FOURCC_DXIL, executable}, {DXBC::FOURCC_SHDR, debug}});
  CHECK_FALSE(ReplayFindOriginalExecutable(bytes.data(), bytes.size(), selected));
  bytes = ReplayUAVTestContainer({{DXBC::FOURCC_SHDR, executable}, {DXBC::FOURCC_SHDR, executable}});
  CHECK_FALSE(ReplayFindOriginalExecutable(bytes.data(), bytes.size(), selected));
  bytes = ReplayUAVTestContainer({{DXBC::FOURCC_SHDR, executable}});
  CHECK_FALSE(ReplayFindOriginalExecutable(bytes.data(), bytes.size() - 1, selected));
  CHECK_FALSE(ReplayFindOriginalExecutable(NULL, 0, selected));
  const uint32_t insideHeader = 4;
  memcpy(bytes.data() + 32, &insideHeader, sizeof(insideHeader));
  CHECK_FALSE(ReplayFindOriginalExecutable(bytes.data(), bytes.size(), selected));
}

TEST_CASE("D3D12 SM6.6 proof requires unique original feature flags without heap addressing",
          "[d3d12][replay][uav-declarations]")
{
  // This exercises the original-container selector and production veto, independently of the
  // IR proof. In particular an adjacent debug program cannot supply missing feature flags.
  const rdcarray<uint32_t> executable = {0x60066};
  ReplayOriginalExecutable selected;
  for(bool flagsFirst : {false, true})
  {
    rdcarray<rdcpair<uint32_t, rdcarray<uint32_t>>> chunks = {
        {DXBC::FOURCC_DXIL, executable}, {DXBC::FOURCC_SFI0, {0, 0}}};
    if(flagsFirst)
      std::swap(chunks[0], chunks[1]);
    bytebuf bytes = ReplayUAVTestContainer(chunks);
    REQUIRE(ReplayFindOriginalExecutable(bytes.data(), bytes.size(), selected));
    CHECK(ReplaySM66FeatureInfoKnown(selected));
    CHECK(selected.featureFlags == 0);
    CHECK(selected.fourcc == DXBC::FOURCC_DXIL);
  }
  for(DXBC::GlobalShaderFlags flag : {DXBC::GlobalShaderFlags::ResourceDescriptorHeapIndexing,
                                     DXBC::GlobalShaderFlags::SamplerDescriptorHeapIndexing,
                                     DXBC::GlobalShaderFlags::AtomicInt64OnHeapResource})
  {
    const uint64_t bits = uint64_t(flag);
    bytebuf bytes = ReplayUAVTestContainer(
        {{DXBC::FOURCC_DXIL, executable},
         {DXBC::FOURCC_SFI0, {uint32_t(bits), uint32_t(bits >> 32)}}});
    REQUIRE(ReplayFindOriginalExecutable(bytes.data(), bytes.size(), selected));
    CHECK(selected.featureFlags == bits);
    CHECK_FALSE(ReplaySM66FeatureInfoKnown(selected));
  }
  bytebuf bytes = ReplayUAVTestContainer(
      {{DXBC::FOURCC_DXIL, executable}, {DXBC::FOURCC_ILDB, {0, 0}}});
  REQUIRE(ReplayFindOriginalExecutable(bytes.data(), bytes.size(), selected));
  CHECK_FALSE(ReplaySM66FeatureInfoKnown(selected));
  bytes = ReplayUAVTestContainer({{DXBC::FOURCC_DXIL, executable}, {DXBC::FOURCC_SFI0, {0}}});
  CHECK_FALSE(ReplayFindOriginalExecutable(bytes.data(), bytes.size(), selected));
  bytes = ReplayUAVTestContainer({{DXBC::FOURCC_DXIL, executable},
                                  {DXBC::FOURCC_SFI0, {0, 0}},
                                  {DXBC::FOURCC_SFI0, {0, 0}}});
  CHECK_FALSE(ReplayFindOriginalExecutable(bytes.data(), bytes.size(), selected));
}

TEST_CASE("D3D12 original shader model 5.1 declarations preserve register arrays and spaces",
          "[d3d12][replay][uav-declarations]")
{
  // CS 5.1 dcl_uav_raw U0[4:6], space7; ret. Identifier 0 differs from register base 4.
  rdcarray<uint32_t> tokens = {
      0x50051, 9, (6U << 24) | DXBCBytecode::OPCODE_DCL_UNORDERED_ACCESS_VIEW_RAW,
      (3U << 20) | (uint32_t(DXBCBytecode::TYPE_UNORDERED_ACCESS_VIEW) << 12), 0, 4, 6, 7,
      (1U << 24) | DXBCBytecode::OPCODE_RET};
  bytebuf bytes = ReplayUAVTestContainer({{DXBC::FOURCC_SHEX, tokens}});
  rdcarray<ReplayShaderUAVBinding> declared;
  REQUIRE(ReplayOriginalUAVDeclarations(bytes.data(), bytes.size(), ShaderStage::Compute,
                                         declared) == NULL);
  REQUIRE(declared.size() == 1);
  CHECK(declared[0].first == 4);
  CHECK(declared[0].count == 3);
  CHECK(declared[0].space == 7);
  declared.clear();
  CHECK(ReplayOriginalUAVDeclarations(bytes.data(), bytes.size(), ShaderStage::Pixel,
                                       declared) != NULL);
  CHECK(declared.empty());
  tokens[6] = UINT_MAX;
  bytes = ReplayUAVTestContainer({{DXBC::FOURCC_SHEX, tokens}});
  CHECK(ReplayOriginalUAVDeclarations(bytes.data(), bytes.size(), ShaderStage::Compute,
                                       declared) != NULL);
  CHECK(declared.empty());
  // A DXIL chunk must itself contain a valid executable; an adjacent ILDB cannot repair it.
  bytes = ReplayUAVTestContainer({{DXBC::FOURCC_DXIL, {0, 0, 0}},
                                   {DXBC::FOURCC_ILDB, tokens}});
  CHECK(ReplayOriginalUAVDeclarations(bytes.data(), bytes.size(), ShaderStage::Compute,
                                       declared) != NULL);
}

TEST_CASE("D3D12 root CBV bounds use original executable sizes", "[d3d12][replay][bufferpatch]")
{
  // CS 5.0: dcl_constantbuffer CB3[4], immediateIndexed; ret. The executable promises 64 bytes,
  // independently of debug/reflection chunks that could describe a different source layout.
  rdcarray<uint32_t> tokens = {0x50050,
                               7,
                               (4U << 24) | DXBCBytecode::OPCODE_DCL_CONSTANT_BUFFER,
                               (2U << 20) | (uint32_t(DXBCBytecode::TYPE_CONSTANT_BUFFER) << 12),
                               3,
                               4,
                               (1U << 24) | DXBCBytecode::OPCODE_RET};
  bytebuf bytes = ReplayUAVTestContainer({{DXBC::FOURCC_SHDR, tokens}, {DXBC::FOURCC_RDEF, {0, 0}}});
  rdcarray<ReplayShaderUAVBinding> uavs;
  D3D12CommandData::ReplayPSOUAVProof proof;
  REQUIRE(ReplayOriginalUAVDeclarations(bytes.data(), bytes.size(), ShaderStage::Compute, uavs,
                                        &proof) == NULL);
  REQUIRE(proof.constantBuffersComplete);
  REQUIRE(proof.constantBuffers.size() == 1);
  CHECK(proof.constantBuffers[0].first == 3);
  CHECK(proof.constantBuffers[0].count == 1);
  CHECK(proof.constantBuffers[0].byteSize == 64);
}

TEST_CASE("D3D12 original UAV range remains available after edits and EID rebasing",
          "[d3d12][replay][uav-declarations]")
{
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  BakedCmdListInfo info;
  REQUIRE(AddReplayUAVRange(info, heap, 512, 100, 32, 64, 20, false));
  CHECK(info.replayDeclaredUAVRanges.empty());
  const BakedCmdListInfo::ReplayUAVRange declared = {heap, 134, 3};
  RecordReplayWriteStart(info.replayDeclaredUAVRanges, info.replayDeclaredUAVFirstEvents,
                         declared, 20);
  REQUIRE(info.replayUAVRanges.size() == 1);
  CHECK(info.replayUAVRanges.begin()->first == 132);
  CHECK(info.replayUAVRanges.begin()->count == 64);
  info.ShiftReplayMetadata(10, -3);
  CHECK(info.replayUAVFirstEvents.begin()->second == 17);
  CHECK(info.replayDeclaredUAVFirstEvents.at(declared) == 17);
  ReplayWriteCutoff cutoff;
  cutoff.indexed = true;
  cutoff.last = 16;
  CHECK_FALSE(IsReplayWriteInPrefix(info.replayDeclaredUAVFirstEvents, declared, cutoff));
  cutoff.last = 17;
  CHECK(IsReplayWriteInPrefix(info.replayDeclaredUAVFirstEvents, declared, cutoff));
  // A later unproved action contributes its full range to the alternate union too.
  REQUIRE(AddReplayUAVRange(info, heap, 512, 100, 32, 64, 30));
  CHECK(info.replayDeclaredUAVRanges.size() == 2);
  CHECK(info.replayUAVFirstEvents.begin()->second == 17);
}

TEST_CASE("D3D12 writes use only the executed graphics or compute pipeline bindings",
          "[d3d12][replay][write-stages]")
{
  for(D3D12Chunk chunk : {D3D12Chunk::List_DrawInstanced, D3D12Chunk::List_DrawIndexedInstanced,
                          D3D12Chunk::List_DispatchMesh})
  {
    CHECK(ReplayWriteStages(chunk).graphics);
    CHECK_FALSE(ReplayWriteStages(chunk).compute);
  }
  CHECK_FALSE(ReplayWriteStages(D3D12Chunk::List_Dispatch).graphics);
  CHECK(ReplayWriteStages(D3D12Chunk::List_Dispatch).compute);
  CHECK(ReplayWriteStages(D3D12Chunk::List_ExecuteIndirect).graphics);
  CHECK(ReplayWriteStages(D3D12Chunk::List_ExecuteIndirect).compute);
  D3D12CommandSignature signature;
  D3D12_INDIRECT_ARGUMENT_DESC argument = {};
  argument.Type = D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH;
  signature.arguments.push_back(argument);
  CHECK_FALSE(ReplayWriteStages(D3D12Chunk::List_ExecuteIndirect, &signature).graphics);
  CHECK(ReplayWriteStages(D3D12Chunk::List_ExecuteIndirect, &signature).compute);
  argument.Type = D3D12_INDIRECT_ARGUMENT_TYPE_DRAW_INDEXED;
  signature.arguments.push_back(argument);
  CHECK(ReplayWriteStages(D3D12Chunk::List_ExecuteIndirect, &signature).graphics);
  CHECK(ReplayWriteStages(D3D12Chunk::List_ExecuteIndirect, &signature).compute);
  signature.arguments.erase(0);
  CHECK(ReplayWriteStages(D3D12Chunk::List_ExecuteIndirect, &signature).graphics);
  CHECK_FALSE(ReplayWriteStages(D3D12Chunk::List_ExecuteIndirect, &signature).compute);
  signature.raytraced = true;
  CHECK(ReplayWriteStages(D3D12Chunk::List_ExecuteIndirect, &signature).compute);
  signature.raytraced = false;
  signature.arguments[0].Type = D3D12_INDIRECT_ARGUMENT_TYPE(~0U);
  CHECK(ReplayWriteStages(D3D12Chunk::List_ExecuteIndirect, &signature).graphics);
  CHECK(ReplayWriteStages(D3D12Chunk::List_ExecuteIndirect, &signature).compute);

  // A dispatch must not pick up still-bound stream-output buffers from an earlier draw.
  D3D12CommandData command;
  D3D12RenderState state;
  state.streamouts.resize(1);
  state.streamouts[0].buf = ResourceIDGen::GetNewUniqueID();
  state.streamouts[0].countbuf = ResourceIDGen::GetNewUniqueID();
  state.shadingRateImage = ResourceIDGen::GetNewUniqueID();
  BakedCmdListInfo compute, graphics;
  command.ObserveReplayWriteBindings(compute, state, 5, false, true);
  CHECK(compute.replayWrites.empty());
  command.ObserveReplayWriteBindings(graphics, state, 6, true, false);
  CHECK(graphics.replayWrites.count(state.streamouts[0].buf) == 1);
  CHECK(graphics.replayWrites.count(state.streamouts[0].countbuf) == 1);
  REQUIRE(compute.replayTextureReadOps.size() == 1);
  REQUIRE(graphics.replayTextureReadOps.size() == 1);
  CHECK(compute.replayTextureReadOps[0].directResources.empty());
  CHECK(graphics.replayTextureReadOps[0].directResources.contains(state.shadingRateImage));
  CHECK(compute.replayTextureReadOps[0].unknown);
  CHECK(graphics.replayTextureReadOps[0].unknown);
}
#endif

#include "d3d12_buffer_read_index.inl"

uint32_t D3D12CommandData::GetBufferPatchFirstConsumer(ResourceId buffer) const
{
  uint32_t first = ~0U;
  for(int p = 0; p < ePartialNum; p++)
    for(const auto &submission : m_Partial[p].cmdListExecs)
    {
      const auto found = m_BakedCmdListInfo.find(submission.first);
      if(found == m_BakedCmdListInfo.end())
        return 0;
      const auto &info = found->second;
      if(info.bufferPatchUnknownConsumers || info.bufferPatchConsumers.count(buffer))
        for(uint32_t event : submission.second)
          first = RDCMIN(first, event);
    }
  return first;
}

bool D3D12CommandData::GetCheckpointContinuation(
    uint64_t offset, uint32_t nextEvent,
    rdcarray<BakedCmdListInfo::CheckpointRecordingChunk> &chunks, rdcstr &reason,
    CheckpointUnsupportedChunks *unsupported, uint32_t targetEvent) const
{
  chunks.clear();
  reason.clear();
  std::map<D3D12Chunk, uint64_t> rejectedChunks;
  for(const auto &entry : m_BakedCmdListInfo)
  {
    const BakedCmdListInfo &info = entry.second;
    if(!info.checkpointBegin || info.checkpointBegin >= offset)
      continue;
    const auto executions = m_Partial[Primary].cmdListExecs.find(entry.first);
    bool prefixSubmit = false, suffixSubmit = false;
    if(executions != m_Partial[Primary].cmdListExecs.end())
      for(uint32_t base : executions->second)
      {
        prefixSubmit |= base < nextEvent;
        suffixSubmit |= base >= nextEvent;
      }
    const bool open = !info.checkpointEnd || info.checkpointEnd > offset;
    if(!open && !suffixSubmit)
      continue;
    // Match Serialise_Reset's rerecord decision, including a list containing the target and any
    // repeated submission in the interval. Check secondary executions too: even though bundles
    // are currently rejected by checkpoint validation, never prune a list Reset could record.
    bool targetSubmit = targetEvent == ~0U;
    for(int p = 0; p < ePartialNum && !targetSubmit; p++)
    {
      const auto submits = m_Partial[p].cmdListExecs.find(entry.first);
      if(submits != m_Partial[p].cmdListExecs.end())
        for(uint32_t base : submits->second)
          targetSubmit |= base <= targetEvent;
    }
    if(prefixSubmit)
    {
      if(reason.empty())
        reason = "A submitted command list is recorded or submitted again across the checkpoint";
    }
    if(info.checkpointChunks.empty() ||
       info.checkpointChunks.front().type != D3D12Chunk::List_Reset)
    {
      if(reason.empty())
        reason = "Missing Reset for an outstanding command-list recording";
    }
    for(const auto &chunk : info.checkpointChunks)
    {
      if(chunk.offset >= offset)
        break;
      if(chunk.endOffset > offset && reason.empty())
        reason = "A command chunk crosses the checkpoint stream position";
      const bool directIndirect = chunk.type == D3D12Chunk::List_ExecuteIndirect &&
                                  (chunk.directIndirect || (!prefixSubmit && !suffixSubmit));
      if(!IsCheckpointContinuationSafeChunk(chunk.type) && !directIndirect)
      {
        rejectedChunks[chunk.type]++;
        if(unsupported)
          (*unsupported)[chunk.type].insert(chunk.offset);
      }
      chunks.push_back(chunk);
      chunks.back().metadataOnly = !targetSubmit && chunk.type != D3D12Chunk::List_Reset &&
                                   chunk.type != D3D12Chunk::List_Close;
    }
  }
  if(!rejectedChunks.empty())
  {
    if(!reason.empty())
      reason += "; ";
    reason += "Outstanding command-list prefix needs unsupported replay work: ";
    bool first = true;
    for(const auto &rejected : rejectedChunks)
    {
      reason += StringFormat::Fmt("%s%s(%llu)", first ? "" : ",", ToStr(rejected.first).c_str(),
                                  (unsigned long long)rejected.second);
      first = false;
    }
  }
  if(!reason.empty())
  {
    chunks.clear();
    return false;
  }
  if(!chunks.empty() && m_pDevice && m_pDevice->GetCaptureVersion() < 0x5)
  {
    reason = "Old capture lacks consumed CPU descriptor values for command-list continuation";
    return false;
  }
  // Preserve interleaved Reset/recording order, including parent->baked wrapper replacements.
  std::sort(chunks.begin(), chunks.end(),
              [](const BakedCmdListInfo::CheckpointRecordingChunk &a,
                 const BakedCmdListInfo::CheckpointRecordingChunk &b) { return a.offset < b.offset; });
  return true;
}

bool D3D12CommandData::ApplyCheckpointContinuationMetadata(
    const BakedCmdListInfo::CheckpointRecordingChunk &chunk)
{
  if(!chunk.metadataOnly || chunk.type == D3D12Chunk::List_Reset ||
     chunk.type == D3D12Chunk::List_Close || HasRerecordCmdList(chunk.baked) ||
     IsPartialCmdList(chunk.baked))
    return false;

  // Reset established the captured baked wrapper and parent replacement. No native list can
  // consume the skipped state in this request; suffix chunks/Close still see its exact cursor.
  // In particular do not derive this from MaxCommandCount: loading may have removed unused
  // ExecuteIndirect child events, and ShiftReplayMetadata already fixed eventAfter in this index.
  m_CurChunkOffset = chunk.offset;
  m_LastCmdListID = chunk.baked;
  m_BakedCmdListInfo[chunk.baked].curEventID = chunk.eventAfter;
  m_LastChunk = chunk.type;
  return true;
}

struct CheckpointQueryUse
{
  uint32_t eventId;
  ResourceId heap;
  uint32_t index;
  bool resolve;
  D3D12_QUERY_TYPE type;
  ResourceId baked;
  uint32_t relativeEvent;
  bool completed;
  bool restorable;
  bool nativeValid;
};

static D3D12CheckpointQueryPlan PlanCheckpointQueries(uint32_t nextEvent,
                                                     rdcarray<CheckpointQueryUse> queries)
{
  D3D12CheckpointQueryPlan plan;
  std::stable_sort(queries.begin(), queries.end(),
                   [](const CheckpointQueryUse &a, const CheckpointQueryUse &b) {
                     return a.eventId < b.eventId;
                   });
  std::map<D3D12CheckpointQuerySlot, D3D12CheckpointQueryVersion> prefix;
  std::set<D3D12CheckpointQuerySlot> suffixWrites;
  std::set<D3D12CheckpointQuerySlot> incompleteSuffixWrites;
  // A baked list can be submitted repeatedly. Its one recorded resolve cannot switch between
  // a saved prefix value and a newly produced native value on different submissions.
  std::map<D3D12CheckpointQueryResolve, bool> modes;
  for(const CheckpointQueryUse &query : queries)
  {
    const D3D12CheckpointQuerySlot slot = {query.heap, query.index};
    if(query.eventId < nextEvent)
    {
      if(!query.resolve)
      {
        prefix.erase(slot);
        if(query.completed && query.restorable)
          prefix[slot] = {query.type, query.eventId};
      }
      continue;
    }
    if(!query.resolve)
    {
      if(query.completed)
      {
        suffixWrites.insert(slot);
        incompleteSuffixWrites.erase(slot);
      }
      else
        incompleteSuffixWrites.insert(slot);
      continue;
    }
    if(!query.nativeValid)
      continue;    // immutable pre-capture query results already have an independent source
    const bool needsPrefix = suffixWrites.find(slot) == suffixWrites.end();
    const auto value = prefix.find(slot);
    const bool saved = needsPrefix && value != prefix.end() && query.restorable &&
                       value->second.type == query.type;
    const D3D12CheckpointQueryResolve key = {{query.baked, query.relativeEvent}, slot};
    const auto mode = modes.find(key);
    if(incompleteSuffixWrites.find(slot) != incompleteSuffixWrites.end() ||
       (needsPrefix && !saved) || (mode != modes.end() && mode->second != saved))
    {
      plan.firstUnsupportedResolve = query.eventId;
      plan.firstUnsupportedType = query.type;
      plan.unsupportedSlot = slot;
      plan.unsupportedReason = incompleteSuffixWrites.find(slot) != incompleteSuffixWrites.end()
                                   ? "Query has an unmatched suffix End"
                               : !needsPrefix || saved
                                   ? "Repeated command-list submission changes query resolve source"
                                   : "Query has no compatible completed prefix slot";
      break;
    }
    modes[key] = saved;
    if(saved)
    {
      plan.slots[slot] = value->second;
      plan.resolves.insert(key);
    }
  }
  return plan;
}

#if ENABLED(ENABLE_UNIT_TESTS)
TEST_CASE("D3D12 dirty plans use final explicit writes and preserve alias storage closure",
          "[d3d12][replay][dirty-restore]")
{
  D3D12CommandData command;
  const ResourceId baked = ResourceIDGen::GetNewUniqueID();
  const ResourceId texture = ResourceIDGen::GetNewUniqueID();
  const ResourceId copied = ResourceIDGen::GetNewUniqueID();
  const ResourceId input = ResourceIDGen::GetNewUniqueID();
  const ResourceId alias = ResourceIDGen::GetNewUniqueID();
  auto &info = command.m_BakedCmdListInfo[baked];
  info.eventNodes.resize(3);
  info.eventNodes[1].resourceUsage.push_back({copied, ResourceUsage::CopyDst});
  info.eventNodes[1].resourceUsage.push_back({input, ResourceUsage::CopySrc});
  info.eventNodes[2].resourceUsage.push_back({texture, ResourceUsage::ColorTarget});
  info.checkpointChunks.push_back({baked, 0, 20, 0, D3D12Chunk::List_Reset});
  info.checkpointEnd = 100;
  BakedCmdListInfo::CheckpointBarrierOp barrier;
  barrier.type = D3D12_RESOURCE_BARRIER_TYPE_ALIASING;
  barrier.after = alias;
  info.checkpointBarriers.push_back(barrier);
  command.BuildReplayWriteTracking();
  CHECK(info.replayWritesKnown);
  CHECK_FALSE(info.replayUnknownWrites);
  CHECK(info.replayWrites.count(copied) == 1);
  CHECK(info.replayWrites.count(texture) == 1);
  CHECK(info.replayWrites.count(input) == 0);
  CHECK(info.replayWrites.count(alias) == 1);
  // Building metadata neither invents a submission nor grants completeness to an open recording.
  CHECK(command.m_Partial[D3D12CommandData::Primary].cmdListExecs.empty());
  const ResourceId open = ResourceIDGen::GetNewUniqueID();
  command.m_BakedCmdListInfo[open].eventNodes.resize(1);
  command.BuildReplayWriteTracking();
  CHECK_FALSE(command.m_BakedCmdListInfo[open].replayWritesKnown);
  info.checkpointBarriers.back().after = ResourceId();
  command.BuildReplayWriteTracking();
  CHECK(info.replayUnknownWrites);
}

TEST_CASE("D3D12 dirty UAV ranges have no reflected-usage cutoff or overflowing bounds",
          "[d3d12][replay][dirty-restore]")
{
  BakedCmdListInfo info;
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  CHECK(AddReplayUAVRange(info, heap, 8000000, 64, 128, 5000000));
  REQUIRE(info.replayUAVRanges.size() == 1);
  CHECK(info.replayUAVRanges.begin()->first == 192);
  CHECK(info.replayUAVRanges.begin()->count == 5000000);
  CHECK(AddReplayUAVRange(info, heap, 8000000, 7900000, 100, UINT_MAX));
  CHECK_FALSE(AddReplayUAVRange(info, heap, 8000000, ~0ULL, 1, 1));
  CHECK_FALSE(AddReplayUAVRange(info, heap, 8000000, 7900000, 100001, 1));
  CHECK_FALSE(AddReplayUAVRange(info, heap, 8000000, 7900000, 0, 100001));
  CHECK(AddReplayUAVRange(info, heap, 8000000, 8000000, 0, UINT_MAX));
  CHECK(info.replayUAVRanges.size() == 2);

  D3D12CommandSignature signature;
  CHECK_FALSE(HasKnownReplayIndirectWrites(signature));
  for(D3D12_INDIRECT_ARGUMENT_TYPE type : {
          D3D12_INDIRECT_ARGUMENT_TYPE_DRAW, D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH,
          D3D12_INDIRECT_ARGUMENT_TYPE_CONSTANT_BUFFER_VIEW,
          D3D12_INDIRECT_ARGUMENT_TYPE_SHADER_RESOURCE_VIEW,
          D3D12_INDIRECT_ARGUMENT_TYPE_VERTEX_BUFFER_VIEW,
          D3D12_INDIRECT_ARGUMENT_TYPE_INDEX_BUFFER_VIEW})
  {
    D3D12_INDIRECT_ARGUMENT_DESC argument = {};
    argument.Type = type;
    signature.arguments.push_back(argument);
    CHECK(HasKnownReplayIndirectWrites(signature));
  }
  signature.arguments.back().Type = D3D12_INDIRECT_ARGUMENT_TYPE_UNORDERED_ACCESS_VIEW;
  CHECK_FALSE(HasKnownReplayIndirectWrites(signature));
  signature.arguments.back().Type = D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH_RAYS;
  CHECK_FALSE(HasKnownReplayIndirectWrites(signature));
  signature.arguments.back().Type = D3D12_INDIRECT_ARGUMENT_TYPE_DRAW;
  signature.raytraced = true;
  CHECK_FALSE(HasKnownReplayIndirectWrites(signature));
}

TEST_CASE("D3D12 dirty write cutoffs exclude future resources without forgetting earlier writes",
          "[d3d12][replay][dirty-restore]")
{
  BakedCmdListInfo info;
  info.eventCount = 30;
  info.replayWritesKnown = info.replayWriteEventsKnown = true;
  const ResourceId output = ResourceIDGen::GetNewUniqueID();
  const ResourceId futureSparse = ResourceIDGen::GetNewUniqueID();
  const ResourceId rootUAV = ResourceIDGen::GetNewUniqueID();
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, output, 9);
  RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, output, 21);
  RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, futureSparse, 20);
  RecordReplayWriteStart(info.replayRootUAVWrites, info.replayRootUAVFirstEvents, rootUAV, 11);
  REQUIRE(AddReplayUAVRange(info, heap, 8000000, 64, 128, 5000000, 12));
  const auto range = *info.replayUAVRanges.begin();
  const auto before = SelectReplayWriteCutoff(info, true, true, 100, 108);
  CHECK_FALSE(IsReplayWriteInPrefix(info.replayWriteFirstEvents, output, before));
  const auto at = SelectReplayWriteCutoff(info, true, true, 100, 109);
  CHECK(at.indexed);
  CHECK(at.last == 9);
  CHECK(IsReplayWriteInPrefix(info.replayWriteFirstEvents, output, at));
  // This is the production filter before MarkReplayResourceDirty can invalidate on a sparse RID.
  CHECK_FALSE(IsReplayWriteInPrefix(info.replayWriteFirstEvents, futureSparse, at));
  CHECK_FALSE(IsReplayWriteInPrefix(info.replayRootUAVFirstEvents, rootUAV, at));
  CHECK_FALSE(IsReplayWriteInPrefix(info.replayUAVFirstEvents, range, at));
  const auto uav = SelectReplayWriteCutoff(info, true, true, 100, 112);
  CHECK(IsReplayWriteInPrefix(info.replayRootUAVFirstEvents, rootUAV, uav));
  CHECK(IsReplayWriteInPrefix(info.replayUAVFirstEvents, range, uav));
  CHECK(info.replayUAVFirstEvents.begin()->second == 12);
  CHECK(info.replayWriteFirstEvents[output] == 9);
  CHECK(info.replayWrites.size() == 2);
  const auto full = SelectReplayWriteCutoff(info, true, true, 100, 500);
  CHECK(full.last == 29);
  CHECK(IsReplayWriteInPrefix(info.replayWriteFirstEvents, futureSparse, full));
}

TEST_CASE("D3D12 dirty cutoffs use each repeated submission base and fail closed on unknown origins",
          "[d3d12][replay][dirty-restore]")
{
  BakedCmdListInfo info;
  info.eventCount = 19;
  info.replayWritesKnown = info.replayWriteEventsKnown = true;
  const ResourceId trailingWrite = ResourceIDGen::GetNewUniqueID();
  RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, trailingWrite, 17);
  // The repeated-list fixture's target82 has WithoutDraw81: occurrence44 is full, occurrence68
  // is partial. Using the latter's base for both would incorrectly forget the earlier write.
  const auto earlier = SelectReplayWriteCutoff(info, true, true, 44, 81);
  const auto partial = SelectReplayWriteCutoff(info, true, true, 68, 81);
  const auto onlyDraw = SelectReplayWriteCutoff(info, true, true, 68, 82);
  CHECK(earlier.last == 18);
  CHECK(partial.last == 13);
  CHECK(onlyDraw.last == 14);
  CHECK(IsReplayWriteInPrefix(info.replayWriteFirstEvents, trailingWrite, earlier));
  CHECK_FALSE(IsReplayWriteInPrefix(info.replayWriteFirstEvents, trailingWrite, partial));
  CHECK_FALSE(IsReplayWriteInPrefix(info.replayWriteFirstEvents, trailingWrite, onlyDraw));
  RecordUnknownReplayWrite(info, 17, D3D12Chunk::List_ExecuteBundle);
  CHECK_FALSE(SelectReplayWriteCutoff(info, true, true, 68, 84).unknown);
  CHECK(SelectReplayWriteCutoff(info, true, true, 68, 85).unknown);
  CHECK(SelectReplayWriteCutoff(info, true, true, 68, 86).unknown);
  CHECK(SelectReplayWriteCutoff(info, true, true, 44, 81).unknown);
  const auto disabled = SelectReplayWriteCutoff(info, false, true, 68, 81);
  CHECK_FALSE(disabled.indexed);
  CHECK(disabled.unknown);
  CHECK(IsReplayWriteInPrefix(info.replayWriteFirstEvents, trailingWrite, disabled));
  CHECK_FALSE(SelectReplayWriteCutoff(info, true, false, 68, 81).indexed);
  CHECK_FALSE(SelectReplayWriteCutoff(info, true, true, 90, 81).indexed);
  CHECK_FALSE(SelectReplayWriteCutoff(info, true, true, ~0U, ~0U).indexed);
  info.replayWriteEventsKnown = false;
  CHECK(SelectReplayWriteCutoff(info, true, true, 68, 81).unknown);
  CHECK_FALSE(SelectReplayWriteCutoff(info, true, true, 68, 81).indexed);
  info.replayWriteEventsKnown = true;
  RecordUnknownReplayWrite(info, 2, D3D12Chunk::List_DispatchRays);
  CHECK(info.replayFirstUnknownWriteEvent == 2);
  CHECK(info.replayUnknownWriteChunk == D3D12Chunk::List_DispatchRays);
  CHECK(SelectReplayWriteCutoff(info, true, true, 68, 81).unknown);
  info.replayUnknownWrites = false;
  const auto largeBase = SelectReplayWriteCutoff(info, true, true, ~0U - 10, ~0U - 1);
  CHECK(largeBase.indexed);
  CHECK(largeBase.last == 9);
}

TEST_CASE("D3D12 dirty indirect write starts precede expanded child events and follow compaction",
          "[d3d12][replay][dirty-restore]")
{
  BakedCmdListInfo info;
  info.eventCount = 24;
  info.curEventID = 21;
  CHECK(ReplayChunkFirstWriteEvent(info, D3D12Chunk::List_DrawInstanced) == 20);
  CHECK(ReplayChunkFirstWriteEvent(info, D3D12Chunk::List_ExecuteIndirect) == 0);
  info.replayLastIndirectEvent = 2;
  CHECK(ReplayChunkFirstWriteEvent(info, D3D12Chunk::List_ExecuteIndirect) == 2);
  const ResourceId indirectWrite = ResourceIDGen::GetNewUniqueID();
  const ResourceId laterWrite = ResourceIDGen::GetNewUniqueID();
  const ResourceId rootUAV = ResourceIDGen::GetNewUniqueID();
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, indirectWrite, 2);
  RecordReplayWriteStart(info.replayWrites, info.replayWriteFirstEvents, laterWrite, 22);
  RecordReplayWriteStart(info.replayRootUAVWrites, info.replayRootUAVFirstEvents, rootUAV, 22);
  REQUIRE(AddReplayUAVRange(info, heap, 64, 0, 0, 64, 22));
  RecordUnknownReplayWrite(info, 23, D3D12Chunk::List_ExecuteBundle);
  info.ShiftReplayMetadata(21, -3);
  CHECK(info.replayWriteFirstEvents[indirectWrite] == 2);
  CHECK(info.replayWriteFirstEvents[laterWrite] == 19);
  CHECK(info.replayRootUAVFirstEvents[rootUAV] == 19);
  CHECK(info.replayUAVFirstEvents.begin()->second == 19);
  CHECK(info.replayFirstUnknownWriteEvent == 20);
  CHECK(info.replayLastIndirectEvent == 2);
  info.replayWritesKnown = info.replayWriteEventsKnown = true;
  const auto insideIndirect = SelectReplayWriteCutoff(info, true, true, 100, 103);
  CHECK(IsReplayWriteInPrefix(info.replayWriteFirstEvents, indirectWrite, insideIndirect));
  CHECK_FALSE(IsReplayWriteInPrefix(info.replayWriteFirstEvents, laterWrite, insideIndirect));
  // Missing group metadata is assigned event0, which can only over-mark and cannot prove clean.
  info.replayLastIndirectEvent = 0;
  CHECK(ReplayChunkFirstWriteEvent(info, D3D12Chunk::List_ExecuteIndirect) == 0);
}

TEST_CASE("D3D12 checkpoints preserve completed query values by submission and recording identity",
          "[d3d12][replay][checkpoint-query]")
{
  const ResourceId heap = ResourceIDGen::GetNewUniqueID();
  const ResourceId first = ResourceIDGen::GetNewUniqueID();
  const ResourceId second = ResourceIDGen::GetNewUniqueID();
  rdcarray<CheckpointQueryUse> queries = {
      {5, heap, 4, false, D3D12_QUERY_TYPE_TIMESTAMP, first, 2, true, true, true},
      {25, heap, 4, true, D3D12_QUERY_TYPE_TIMESTAMP, second, 1, false, true, true}};
  auto plan = PlanCheckpointQueries(20, queries);
  REQUIRE(plan.slots.size() == 1);
  CHECK(plan.slots.begin()->second.endEvent == 5);
  CHECK(plan.resolves.size() == 1);
  CHECK(plan.firstUnsupportedResolve == ~0U);

  // No completed prefix End may be inferred from the whole-frame native-valid bit.
  queries[0].completed = false;
  CHECK(PlanCheckpointQueries(20, queries).firstUnsupportedResolve == 25);
  queries[0].completed = true;
  queries[0].restorable = false;
  CHECK(PlanCheckpointQueries(20, queries).firstUnsupportedResolve == 25);
  queries[0].restorable = true;
  queries[0].type = D3D12_QUERY_TYPE_OCCLUSION;
  CHECK(PlanCheckpointQueries(20, queries).firstUnsupportedResolve == 25);
  queries[0].type = D3D12_QUERY_TYPE_TIMESTAMP;

  // Re-recording an End before the boundary's file position is irrelevant: submission EID wins.
  queries.push_back({24, heap, 4, false, D3D12_QUERY_TYPE_TIMESTAMP, second, 0, true, true, true});
  plan = PlanCheckpointQueries(20, queries);
  CHECK(plan.slots.empty());
  CHECK(plan.firstUnsupportedResolve == ~0U);
  queries.back().eventId = 26;
  queries.push_back({45, heap, 4, true, D3D12_QUERY_TYPE_TIMESTAMP, second, 1, false, true, true});
  plan = PlanCheckpointQueries(20, queries);
  CHECK(plan.firstUnsupportedResolve == 45);
  CHECK(IsCheckpointQueryTargetSafe(44, plan.firstUnsupportedResolve));
  CHECK_FALSE(IsCheckpointQueryTargetSafe(45, plan.firstUnsupportedResolve));
  CHECK(plan.resolves.size() == 1);

  // A distinct later recording can resolve the new native value without affecting the old one.
  queries.back().baked = first;
  CHECK(PlanCheckpointQueries(20, queries).firstUnsupportedResolve == ~0U);
  queries[2].completed = false;
  CHECK(PlanCheckpointQueries(20, queries).firstUnsupportedResolve == 45);
  queries[2].completed = true;
  queries[1].index = 5;
  CHECK(PlanCheckpointQueries(20, queries).firstUnsupportedResolve == 25);
  queries[1].nativeValid = false;
  CHECK(PlanCheckpointQueries(20, queries).firstUnsupportedResolve == ~0U);
}
#endif

bool D3D12CommandData::IsCheckpointBoundarySafe(CheckpointBoundary &boundary,
                                               rdcstr &reason,
                                               CheckpointUnsupportedChunks *unsupported) const
{
  boundary.firstNativeResolveEID = ~0U;
  boundary.firstNativeResolveType = D3D12_QUERY_TYPE_OCCLUSION;
  boundary.queries = {};
  if(!m_CheckpointUnsupported.empty())
  {
    reason = m_CheckpointUnsupported;
    return false;
  }

  rdcarray<CheckpointQueryUse> queries;
  rdcarray<BakedCmdListInfo::CheckpointBarrierOp> barriers;
  rdcarray<BakedCmdListInfo::CheckpointRecordingChunk> continuation;
  if(!GetCheckpointContinuation(boundary.offset, boundary.nextEvent, continuation, reason, unsupported))
    return false;
  for(const auto &entry : m_Partial[Primary].cmdListExecs)
  {
    auto found = m_BakedCmdListInfo.find(entry.first);
    if(found == m_BakedCmdListInfo.end())
    {
      reason = "Missing baked command-list metadata";
      return false;
    }
    const BakedCmdListInfo &info = found->second;
    for(uint32_t base : entry.second)
    {
      // Unsubmitted recordings are reconstructed from their own original prefix chunks after
      // resource restore. Submitted lists cannot cross the point (checked above).
      if(base < boundary.nextEvent &&
         (info.checkpointEnd == 0 || info.checkpointEnd > boundary.offset ||
          base + info.eventCount >= boundary.nextEvent))
      {
        reason = "Checkpoint is inside an incomplete command-list submission";
        return false;
      }
      if(base < boundary.nextEvent)
        for(const auto &barrier : info.checkpointBarriers)
        {
          barriers.push_back(barrier);
          barriers.back().relativeEvent += base;
        }
      for(const auto &query : info.checkpointQueries)
      {
        if(query.count > 1024 * 1024 || query.index > ~0U - query.count)
        {
          reason = "Query range exceeds checkpoint metadata limits";
          return false;
        }
        WrappedID3D12QueryHeap *heap =
            m_pDevice ? m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12QueryHeap>(query.heap)
                      : NULL;
        for(uint32_t index = 0; index < query.count; index++)
        {
          const uint32_t slot = query.index + index;
          const bool valid = !heap || heap->IsReplayQueryValid(slot);
          const D3D12_QUERY_TYPE type =
              heap && query.resolve ? heap->GetReplayQueryType(slot) : query.type;
          queries.push_back(
              {base + query.relativeEvent, query.heap, slot, query.resolve, type, entry.first,
               query.relativeEvent, query.completed,
               heap && heap->CanCheckpointQuery(slot, type), valid});
        }
      }
    }
  }
  std::stable_sort(barriers.begin(), barriers.end(),
                   [](const BakedCmdListInfo::CheckpointBarrierOp &a,
                      const BakedCmdListInfo::CheckpointBarrierOp &b) {
                     return a.relativeEvent < b.relativeEvent;
                   });
  using SplitKey = std::pair<ResourceId, uint32_t>;
  std::map<SplitKey, std::pair<D3D12_RESOURCE_STATES, D3D12_RESOURCE_STATES>> pendingSplits;
  for(const auto &barrier : barriers)
  {
    if(barrier.type == D3D12_RESOURCE_BARRIER_TYPE_ALIASING)
    {
      if(!m_pDevice || !m_pDevice->IsReplayCheckpointAliasingSafe(
                           barrier.before, barrier.after, boundary.eventId))
      {
        reason = StringFormat::Fmt(
            "Prefix aliasing at EID %u (%s -> %s) lacks proved restorable storage",
            barrier.relativeEvent, ToStr(barrier.before).c_str(), ToStr(barrier.after).c_str());
        return false;
      }
      continue;
    }
    const SplitKey key = std::make_pair(barrier.before, barrier.subresource);
    const auto states = std::make_pair(barrier.stateBefore, barrier.stateAfter);
    if(barrier.flags == D3D12_RESOURCE_BARRIER_FLAG_BEGIN_ONLY)
    {
      // ALL_SUBRESOURCES overlaps each individual subresource too. Do not accept an ambiguous
      // split state merely because its key differs from another pending transition.
      for(const auto &pending : pendingSplits)
        if(pending.first.first == key.first &&
           (pending.first.second == key.second || key.second == D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES ||
            pending.first.second == D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES))
        {
          reason = "Overlapping split resource transitions require additional checkpoint state";
          return false;
        }
      pendingSplits[key] = states;
    }
    else if(barrier.flags == D3D12_RESOURCE_BARRIER_FLAG_END_ONLY)
    {
      auto pending = pendingSplits.find(key);
      if(pending == pendingSplits.end() || pending->second != states)
      {
        reason = "A split resource transition has no matching prefix begin";
        return false;
      }
      pendingSplits.erase(pending);
    }
    else
    {
      reason = "Unsupported split resource barrier flags";
      return false;
    }
  }
  if(!pendingSplits.empty())
  {
    reason = "A split resource transition is still pending at this submission boundary";
    return false;
  }
  std::stable_sort(queries.begin(), queries.end(),
                   [](const CheckpointQueryUse &a, const CheckpointQueryUse &b) {
                     return a.eventId < b.eventId;
                   });
  std::set<std::pair<ResourceId, uint32_t>> suffixWrites;
  for(const CheckpointQueryUse &query : queries)
  {
    if(query.eventId < boundary.nextEvent)
      continue;
    auto key = std::make_pair(query.heap, query.index);
    if(!query.resolve)
      suffixWrites.insert(key);
    else if(suffixWrites.find(key) == suffixWrites.end())
    {
      WrappedID3D12QueryHeap *heap =
          m_pDevice ? m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12QueryHeap>(query.heap)
                    : NULL;
      // Queries never issued in this frame already resolve from the immutable captured result
      // buffer. A live query must be re-issued after the checkpoint before its next resolve.
      if(!heap || heap->IsReplayQueryValid(query.index))
      {
        // Native query heap contents cannot be restored from ordinary resource bytes. Earlier
        // targets remain valid: this resolve is not recorded before its EID enters replay range.
        boundary.firstNativeResolveEID = query.eventId;
        boundary.firstNativeResolveType = heap ? heap->GetReplayQueryType(query.index) : query.type;
        break;
      }
    }
  }
  if(m_pDevice && Process::GetEnvVariable("RENDERDOC_D3D12_DISABLE_CHECKPOINT_QUERIES") != "1")
  {
    boundary.queries = PlanCheckpointQueries(boundary.nextEvent, queries);
    boundary.firstNativeResolveEID = boundary.queries.firstUnsupportedResolve;
    boundary.firstNativeResolveType = boundary.queries.firstUnsupportedType;
  }
  return true;
}

void D3D12CommandData::ObservePreparedReplayChunk(D3D12Chunk chunk, uint64_t chunkEndOffset)
{
  const auto reject = [this, chunk](BakedCmdListInfo &info, PreparedReplayRejectReason reason) {
    info.preparedReplaySafe = false;
    if(info.preparedReplayRejectReason == PreparedReplayRejectReason::None)
    {
      info.preparedReplayRejectReason = reason;
      info.preparedReplayRejectChunk = chunk;
      info.preparedReplayRejectOffset = m_CurChunkOffset;
      info.preparedReplayRejectList = m_LastCmdListID;
    }
  };

  if(chunk == D3D12Chunk::List_Reset)
  {
    if(m_PreparedScanList != ResourceId())
      reject(m_BakedCmdListInfo[m_PreparedScanList], PreparedReplayRejectReason::InterleavedReset);

    m_PreparedScanList = m_LastCmdListID;
    if(m_PreparedScanList != ResourceId())
    {
      BakedCmdListInfo &info = m_BakedCmdListInfo[m_PreparedScanList];
      info.preparedReplayBegin = m_CurChunkOffset;
      info.preparedReplayEnd = 0;
      info.preparedReplayRejectReason = PreparedReplayRejectReason::None;
      info.preparedReplayRejectChunk = chunk;
      info.preparedReplayRejectOffset = m_CurChunkOffset;
      info.preparedReplayRejectList = ResourceId();
      info.preparedReplayQueries.Reset();
    }
    return;
  }

  if(m_PreparedScanList == ResourceId())
    return;

  BakedCmdListInfo &info = m_BakedCmdListInfo[m_PreparedScanList];

  if(chunk == D3D12Chunk::List_Close && m_LastCmdListID == m_PreparedScanList &&
     !info.preparedReplayQueries.Complete())
    reject(info, PreparedReplayRejectReason::APICommand);

  // Some handlers reject particular arguments, e.g. ResourceBarrier permits only transitions.
  // Observe after ProcessChunk so this records that rule separately from the chunk whitelist.
  if(!info.preparedReplaySafe &&
     info.preparedReplayRejectReason == PreparedReplayRejectReason::None)
    reject(info, PreparedReplayRejectReason::APICommand);

  // A non-command chunk, interleaved recording from another list, or a command with replay-owned
  // transient data makes Reset..Close unsafe to seek across as one unit.
  if(m_LastCmdListID != m_PreparedScanList)
    reject(info, PreparedReplayRejectReason::InterleavedChunk);
  else if(!IsPreparedReplaySafeChunk(chunk))
    reject(info, PreparedReplayRejectReason::UnsupportedChunk);

  if(chunk == D3D12Chunk::List_Close && m_LastCmdListID == m_PreparedScanList)
  {
    if(info.preparedReplaySafe)
      info.preparedReplayEnd = chunkEndOffset;
    m_PreparedScanList = ResourceId();
  }
}

void D3D12CommandData::PrepareFullReplayCompanion(ResourceId baked, ResourceId parent,
                                                ID3D12PipelineState *initialPipeline)
{
  if(m_OutsideCmdList != NULL || m_FirstEventID != 1 ||
     m_Partial[Primary].partialParent != baked)
    return;

  const BakedCmdListInfo &info = m_BakedCmdListInfo[baked];
  const uint32_t partialBase = m_Partial[Primary].baseEvent;
  bool needsFull = false;
  for(uint32_t base : m_Partial[Primary].cmdListExecs[baked])
    needsFull |= IsCompletedReplayOccurrence(base, info.eventCount, partialBase, m_LastEventID);
  if(!needsFull)
    return;

  if(m_ActionCallback != NULL || D3D12_Debug_RT_Auditing() || !IsFullReplayCompanionSafe(info))
  {
    if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
    {
      const rdcstr line = StringFormat::Fmt(
          "api=D3D12RepeatedList op=unsupported baked=%s target=%u partialBase=%u "
          "reason=nonContiguousOrUnsupportedRecordingContext",
          ToStr(baked).c_str(), m_LastEventID, partialBase);
      RDCLOG("[EIDTiming] %s", line.c_str());
      AppendReplayTimingLog(line);
    }
    return;
  }

  FullReplayCompanion &companion = m_FullReplayCompanions[baked];
  companion.parent = parent;
  companion.initialPipeline = GetResID(initialPipeline);
  companion.resetState = info.state;
  companion.partialBase = partialBase;
}

RDResult D3D12CommandData::RecordFullReplayCompanion(
    ResourceId baked, uint64_t endOffset,
    const std::function<RDResult(const BakedCmdListInfo::CheckpointRecordingChunk &)> &record)
{
  auto found = m_FullReplayCompanions.find(baked);
  if(found == m_FullReplayCompanions.end() || found->second.ready)
    return ResultCode::Succeeded;

  FullReplayCompanion &companion = found->second;
  BakedCmdListInfo &info = m_BakedCmdListInfo[baked];
  if(endOffset != info.preparedReplayEnd || !IsFullReplayCompanionSafe(info) ||
     m_ActionCallback != NULL || m_OutsideCmdList != NULL || !HasRerecordCmdList(baked))
    return RDResult(ResultCode::APIReplayFailed, "Repeated-list companion recording guard changed");

  PerformanceTimer timer;
  HRESULT hr = m_pDevice->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT,
                                                __uuidof(ID3D12CommandAllocator),
                                                (void **)&companion.allocator);
  if(FAILED(hr) || companion.allocator == NULL)
    return RDResult(ResultCode::APIReplayFailed, "Could not allocate repeated-list companion");
  ID3D12PipelineState *initialPipeline = NULL;
  if(companion.initialPipeline != ResourceId())
  {
    initialPipeline = m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12PipelineState>(
        companion.initialPipeline);
    if(initialPipeline == NULL)
      return RDResult(ResultCode::APIReplayFailed, "Repeated-list initial pipeline is no longer live");
  }
  ID3D12GraphicsCommandList *baseList = NULL;
  hr = m_pDevice->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, companion.allocator,
                                    initialPipeline, __uuidof(ID3D12GraphicsCommandList),
                                    (void **)&baseList);
  companion.list = (ID3D12GraphicsCommandListX *)baseList;
  if(FAILED(hr) || companion.list == NULL)
    return RDResult(ResultCode::APIReplayFailed, "Could not create repeated-list companion");
  m_pDevice->GetResourceManager()->SetInternalResource(companion.allocator);
  m_pDevice->GetResourceManager()->SetInternalResource(companion.list);

  // Keep the existing captured-ID wrapper and replacement mapping intact. Only handler routing
  // points at the fresh internal list while the same serialized body is decoded a second time.
  BakedCmdListInfo &parent = m_BakedCmdListInfo[companion.parent];
  ReplayCompanionMutableState partialState(info);
  const uint32_t parentEvent = parent.curEventID;
  const D3D12RenderState renderState = m_RenderState;
  const auto previousRouting = m_RerecordCmds;
  const ResourceId lastList = m_LastCmdListID;
  const uint64_t lastOffset = m_CurChunkOffset;
  const SDChunkMetaData metadata = m_ChunkMetadata;
  const D3D12Chunk lastChunk = m_LastChunk;
  const uint32_t rootEvent = m_RootEventID;
  const bool addedEventNode = m_AddedEventNode;
  ResourceId partialParents[ePartialNum];
  uint32_t partialBases[ePartialNum];
  bool renderPasses[ePartialNum];
  for(int p = 0; p < ePartialNum; p++)
  {
    partialParents[p] = m_Partial[p].partialParent;
    partialBases[p] = m_Partial[p].baseEvent;
    renderPasses[p] = m_Partial[p].renderPassActive;
    if(m_Partial[p].partialParent == baked)
      m_Partial[p].Reset();
  }
  m_RerecordCmds[baked] = companion.list;
  m_RerecordCmds[companion.parent] = companion.list;
  info.state = companion.resetState;
  info.barriers.clear();
  info.m_OutstandingQueries.clear();
  info.curEventID = 0;
  info.markerCount = 0;

  RDResult result = ResultCode::Succeeded;
  uint64_t sourceBytes = 0;
  // Reset is represented by the fresh list's creation and captured reset-state seed. Running
  // Serialise_Reset again would attempt to create a second wrapper with the same captured ID.
  for(size_t i = 1; i < info.checkpointChunks.size(); i++)
  {
    const auto &chunk = info.checkpointChunks[i];
    result = record(chunk);
    if(result != ResultCode::Succeeded)
      break;
    info.curEventID = chunk.eventAfter;
    m_LastChunk = chunk.type;
    sourceBytes += chunk.endOffset - chunk.offset;
  }

  // Full companion barriers remain under its fresh native wrapper ID. Restore the target's
  // state/barriers so OnlyDraw and pipeline publication continue to describe the selected EID.
  partialState.Restore(info);
  parent.curEventID = parentEvent;
  m_RenderState = renderState;
  m_RerecordCmds = previousRouting;
  m_LastCmdListID = lastList;
  m_CurChunkOffset = lastOffset;
  m_ChunkMetadata = metadata;
  m_LastChunk = lastChunk;
  m_RootEventID = rootEvent;
  m_AddedEventNode = addedEventNode;
  for(int p = 0; p < ePartialNum; p++)
  {
    m_Partial[p].partialParent = partialParents[p];
    m_Partial[p].baseEvent = partialBases[p];
    m_Partial[p].renderPassActive = renderPasses[p];
  }
  companion.ready = result == ResultCode::Succeeded;
  if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
  {
    const rdcstr line = StringFormat::Fmt(
        "api=D3D12RepeatedList op=recordFull baked=%s target=%u partialBase=%u ready=%u "
        "chunks=%zu sourceBytes=%llu total_ms=%.3f",
        ToStr(baked).c_str(), m_LastEventID, companion.partialBase, companion.ready ? 1U : 0U,
        info.checkpointChunks.size() - 1, (unsigned long long)sourceBytes, timer.GetMilliseconds());
    RDCLOG("[EIDTiming] %s", line.c_str());
    AppendReplayTimingLog(line);
  }
  return result;
}

ID3D12GraphicsCommandListX *D3D12CommandData::RerecordCmdListForSubmission(ResourceId baked,
                                                                         uint32_t baseEvent)
{
  auto found = m_FullReplayCompanions.find(baked);
  if(found != m_FullReplayCompanions.end() && found->second.ready)
  {
    const bool completed = IsCompletedReplayOccurrence(baseEvent,
        m_BakedCmdListInfo[baked].eventCount, found->second.partialBase, m_LastEventID);
    if(Process::GetEnvVariable("RENDERDOC_EID_TIMING") == "1")
    {
      const rdcstr line = StringFormat::Fmt(
          "api=D3D12RepeatedList op=%s baked=%s base=%u target=%u",
          completed ? "submitFull" : "submitPartial", ToStr(baked).c_str(), baseEvent, m_LastEventID);
      RDCLOG("[EIDTiming] %s", line.c_str());
      AppendReplayTimingLog(line);
    }
    if(completed)
      return found->second.list;
  }
  return RerecordCmdList(baked);
}

void D3D12CommandData::ClearFullReplayCompanions()
{
  // Lists and their independent allocators stay alive until the next drained prefix replay or
  // queue teardown, including across the subsequent OnlyDraw of this same selected event.
  for(auto &entry : m_FullReplayCompanions)
  {
    if(entry.second.list)
      m_BakedCmdListInfo.erase(GetResID(entry.second.list));
    SAFE_RELEASE(entry.second.list);
    SAFE_RELEASE(entry.second.allocator);
  }
  m_FullReplayCompanions.clear();
}

ID3D12GraphicsCommandListX *D3D12CommandData::BeginPreparedCommandList(
    ResourceId baked, ResourceId parent, D3D12_COMMAND_LIST_TYPE type,
    ID3D12PipelineState *initialState, bool &reused)
{
  reused = false;

  if(!m_PreparedReplayEnabled)
    return NULL;

  m_PreparedAttempts++;

  if(type != D3D12_COMMAND_LIST_TYPE_DIRECT)
  {
    m_PreparedRejectedType++;
    return NULL;
  }

  BakedCmdListInfo &info = m_BakedCmdListInfo[baked];
  // Report the stream's actual first blocker before the additional query-lifetime guard. The
  // old early query rejection hid interleaving and unsupported replay-side mutations.
  const bool unsafeStream =
      !info.preparedReplaySafe || info.preparedReplayEnd <= info.preparedReplayBegin;
  const bool retainTimestamps =
      Process::GetEnvVariable("RENDERDOC_D3D12_ENABLE_PREPARED_TIMESTAMPS") == "1" &&
      PreparedQueriesAreTimestampWrites(info);
  if(unsafeStream || (!info.checkpointQueries.empty() && !retainTimestamps))
  {
    m_PreparedRejectedUnsafe++;
    if(m_PreparedTimingEnabled)
    {
      const PreparedReplayRejectReason reason =
          !unsafeStream ? PreparedReplayRejectReason::NativeQueries
          : info.preparedReplayRejectReason == PreparedReplayRejectReason::None
              ? PreparedReplayRejectReason::IncompleteRange
              : info.preparedReplayRejectReason;
      const uint64_t key = ((uint64_t)reason << 32) | (uint32_t)info.preparedReplayRejectChunk;
      PreparedReplayRejectSummary &summary = m_PreparedRejectSummary[key];
      if(summary.attempts == 0)
      {
        summary.reason = reason;
        summary.chunk = info.preparedReplayRejectChunk;
        summary.firstBaked = baked;
        summary.firstParent = info.parentList;
        summary.observedList = info.preparedReplayRejectList;
        auto observedIt = m_BakedCmdListInfo.find(summary.observedList);
        if(observedIt != m_BakedCmdListInfo.end())
          summary.observedParent = observedIt->second.parentList;
        summary.firstBeginOffset = info.preparedReplayBegin;
        summary.firstOffset = info.preparedReplayRejectOffset;
      }
      summary.attempts++;
    }
    return NULL;
  }

  auto preparedIt = m_PreparedCmdLists.find(baked);
  if(preparedIt != m_PreparedCmdLists.end())
  {
    PreparedCommandList &prepared = preparedIt->second;
    if(prepared.ready && prepared.beginOffset == info.preparedReplayBegin &&
       prepared.endOffset == info.preparedReplayEnd && prepared.list != NULL)
    {
      m_RerecordCmds[baked] = prepared.list;
      m_RerecordCmds[parent] = prepared.list;
      m_PreparedSkipEnd = prepared.endOffset;
      m_PreparedCacheHits++;
      reused = true;
      return prepared.list;
    }

    m_PreparedRejectedExisting++;
    return NULL;
  }

  const uint64_t sourceBytes = info.preparedReplayEnd - info.preparedReplayBegin;
  static const size_t maxPreparedLists = 128;
  static const uint64_t maxPreparedSourceBytes = 256ULL * 1024ULL * 1024ULL;
  if(m_PreparedCmdLists.size() >= maxPreparedLists ||
     sourceBytes > maxPreparedSourceBytes - RDCMIN(maxPreparedSourceBytes, m_PreparedCacheBytes))
  {
    m_PreparedRejectedBudget++;
    return NULL;
  }

  ID3D12CommandAllocator *allocator = NULL;
  HRESULT hr = m_pDevice->CreateCommandAllocator(type, __uuidof(ID3D12CommandAllocator),
                                                  (void **)&allocator);
  if(FAILED(hr) || allocator == NULL)
  {
    SAFE_RELEASE(allocator);
    return NULL;
  }

  ID3D12GraphicsCommandList *baseList = NULL;
  hr = m_pDevice->CreateCommandList(0, type, allocator, initialState,
                                    __uuidof(ID3D12GraphicsCommandList), (void **)&baseList);
  if(FAILED(hr) || baseList == NULL)
  {
    SAFE_RELEASE(baseList);
    SAFE_RELEASE(allocator);
    return NULL;
  }

  m_pDevice->GetResourceManager()->SetInternalResource(allocator);
  m_pDevice->GetResourceManager()->SetInternalResource(baseList);

  PreparedCommandList &prepared = m_PreparedCmdLists[baked];
  prepared.allocator = allocator;
  prepared.list = (ID3D12GraphicsCommandListX *)baseList;
  prepared.parentList = parent;
  prepared.beginOffset = info.preparedReplayBegin;
  prepared.endOffset = info.preparedReplayEnd;
  prepared.sourceBytes = sourceBytes;
  prepared.ready = false;

  m_PreparedCacheBytes += sourceBytes;
  m_PreparedCacheBuilds++;
  m_PreparedBuildingList = baked;
  m_RerecordCmds[baked] = prepared.list;
  m_RerecordCmds[parent] = prepared.list;
  return prepared.list;
}

void D3D12CommandData::FinishPreparedCommandList(ResourceId baked, uint64_t chunkEndOffset)
{
  if(m_PreparedBuildingList != baked)
    return;

  auto it = m_PreparedCmdLists.find(baked);
  if(it != m_PreparedCmdLists.end())
  {
    PreparedCommandList &prepared = it->second;
    prepared.ready = (prepared.list != NULL && prepared.endOffset == chunkEndOffset);
  }

  m_PreparedBuildingList = ResourceId();
}

void D3D12CommandData::ClearPreparedReplayCache()
{
  m_RerecordCmds.clear();
  m_PreparedBuildingList = ResourceId();
  m_PreparedSkipEnd = 0;

  for(auto it = m_PreparedCmdLists.begin(); it != m_PreparedCmdLists.end(); ++it)
  {
    ResourceId listId = it->second.list ? GetResID(it->second.list) : ResourceId();
    if(listId != ResourceId())
      m_BakedCmdListInfo.erase(listId);
    SAFE_RELEASE(it->second.list);
    SAFE_RELEASE(it->second.allocator);
  }

  m_PreparedCmdLists.clear();
  m_PreparedCacheBytes = 0;
}

void D3D12CommandData::AddEvent()
{
  rdcarray<D3D12EventNode> &eventNodes =
      (m_LastCmdListID != ResourceId() ? m_BakedCmdListInfo[m_LastCmdListID].eventNodes
                                       : m_EventNodes);
  eventNodes.emplace_back(m_LoadingEventNode);
  m_LoadingEventNode = D3D12EventNode();

  D3D12EventNode &node = eventNodes.back();

  // if we're using replay-time debug messages, fetch them now since we can do better to correlate to events on replay
  if(m_pDevice->GetReplayOptions().apiValidation)
    node.debugMessages = m_pDevice->GetDebugMessages();

  APIEvent &apievent = node.event;
  apievent.fileOffset = m_CurChunkOffset;
  apievent.chunkIndex = uint32_t(m_StructuredFile->chunks.size() - 1);
  // event IDs start from one not zero
  apievent.eventId = (uint32_t)eventNodes.count();
  if(m_LastCmdListID != ResourceId())
  {
    node.annotations.swap(m_BakedCmdListInfo[m_LastCmdListID].pendingAnnotations);
  }
  else
  {
    if(m_RootAnnotation)
      apievent.annotations = m_RootAnnotation->Duplicate();
  }

  m_AddedEventNode = true;
}

void D3D12CommandData::AddResourceUsage(D3D12EventNode &eventNode, ResourceId id, ResourceUsage usage)
{
  if(id == ResourceId())
    return;

  eventNode.resourceUsage.push_back(make_rdcpair(id, usage));
}

void D3D12CommandData::AddCPUUsage(ResourceId id, ResourceUsage usage)
{
  m_LoadingEventNode.resourceUsage.push_back(make_rdcpair<ResourceId, ResourceUsage>(id, usage));
}

void D3D12CommandData::AddUsageForBindInRootSig(const D3D12RenderState &state,
                                                D3D12EventNode &eventNode,
                                                const D3D12RenderState::RootSignature *rootsig,
                                                D3D12_DESCRIPTOR_RANGE_TYPE type, uint32_t space,
                                                uint32_t bind, uint32_t rangeSize)
{
  static bool hugeRangeWarned = false;

  // use a 'clamped' range size to avoid annoying overflow issues
  rangeSize = RDCMIN(rangeSize, 0x10000000U);

  D3D12ResourceManager *rm = m_pDevice->GetResourceManager();

  WrappedID3D12RootSignature *sig = rm->GetResAs<WrappedID3D12RootSignature>(rootsig->rootsig);

  for(size_t rootEl = 0; rootEl < sig->sig.Parameters.size(); rootEl++)
  {
    if(rootEl >= rootsig->sigelems.size())
      break;

    const D3D12RootSignatureParameter &p = sig->sig.Parameters[rootEl];
    const D3D12RenderState::SignatureElement &el = rootsig->sigelems[rootEl];

    if(p.ParameterType == D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS)
      continue;

    ResourceUsage cb = ResourceUsage::CS_Constants;
    ResourceUsage ro = ResourceUsage::CS_Resource;
    ResourceUsage rw = ResourceUsage::CS_RWResource;

    if(rootsig == &state.graphics)
    {
      if(p.ShaderVisibility == D3D12_SHADER_VISIBILITY_ALL)
      {
        cb = ResourceUsage::All_Constants;
        ro = ResourceUsage::All_Resource;
        rw = ResourceUsage::All_RWResource;
      }
      else
      {
        cb = CBUsage(p.ShaderVisibility - D3D12_SHADER_VISIBILITY_VERTEX);
        ro = ResUsage(p.ShaderVisibility - D3D12_SHADER_VISIBILITY_VERTEX);
        rw = RWResUsage(p.ShaderVisibility - D3D12_SHADER_VISIBILITY_VERTEX);
      }
    }

    if(p.ParameterType == D3D12_ROOT_PARAMETER_TYPE_CBV && el.type == eRootCBV &&
       type == D3D12_DESCRIPTOR_RANGE_TYPE_CBV && p.Descriptor.RegisterSpace == space &&
       p.Descriptor.ShaderRegister >= bind && p.Descriptor.ShaderRegister < bind + rangeSize)
    {
      AddResourceUsage(eventNode, el.id, cb);

      // common case - root element matches 1:1 with a non-array shader bind, if so we can exit. If
      // not we might have to continue since other parts of it might be mapped to a table, or
      // another root element
      if(rangeSize == 1)
        return;
    }
    else if(p.ParameterType == D3D12_ROOT_PARAMETER_TYPE_SRV && el.type == eRootSRV &&
            type == D3D12_DESCRIPTOR_RANGE_TYPE_SRV && p.Descriptor.RegisterSpace == space &&
            p.Descriptor.ShaderRegister >= bind && p.Descriptor.ShaderRegister < bind + rangeSize)
    {
      AddResourceUsage(eventNode, el.id, ro);

      // common case - root element matches 1:1 with a non-array shader bind, if so we can exit. If
      // not we might have to continue since other parts of it might be mapped to a table, or
      // another root element
      if(rangeSize == 1)
        return;
    }
    else if(p.ParameterType == D3D12_ROOT_PARAMETER_TYPE_UAV && el.type == eRootUAV &&
            type == D3D12_DESCRIPTOR_RANGE_TYPE_UAV && p.Descriptor.RegisterSpace == space &&
            p.Descriptor.ShaderRegister >= bind && p.Descriptor.ShaderRegister < bind + rangeSize)
    {
      AddResourceUsage(eventNode, el.id, rw);

      // common case - root element matches 1:1 with a non-array shader bind, if so we can exit. If
      // not we might have to continue since other parts of it might be mapped to a table, or
      // another root element
      if(rangeSize == 1)
        return;
    }
    else if(p.ParameterType == D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE && el.type == eRootTable)
    {
      WrappedID3D12DescriptorHeap *heap =
          m_pDevice->GetResourceManager()->GetResAs<WrappedID3D12DescriptorHeap>(el.id);

      if(heap == NULL)
        continue;

      UINT prevTableOffset = 0;

      for(size_t r = 0; r < p.ranges.size(); r++)
      {
        const D3D12_DESCRIPTOR_RANGE1 &range = p.ranges[r];

        UINT offset = range.OffsetInDescriptorsFromTableStart;

        if(range.OffsetInDescriptorsFromTableStart == D3D12_DESCRIPTOR_RANGE_OFFSET_APPEND)
          offset = prevTableOffset;

        D3D12Descriptor *desc = (D3D12Descriptor *)heap->GetCPUDescriptorHandleForHeapStart().ptr;
        desc += el.offset;
        desc += offset;

        UINT num = range.NumDescriptors;

        if(num == UINT_MAX)
        {
          // find out how many descriptors are left after
          num = heap->GetNumDescriptors() - offset - UINT(el.offset);
        }

        prevTableOffset = offset + num;

        // skip ranges that aren't the type or register space we want
        if(range.RangeType != type || range.RegisterSpace != space)
          continue;

        // skip ranges that don't overlap with the registers we are looking for at all
        if(range.BaseShaderRegister + num <= bind || range.BaseShaderRegister >= bind + rangeSize)
          continue;

        if(num > 1000)
        {
          if(!hugeRangeWarned)
            RDCWARN("Skipping large, most likely 'bindless', descriptor range");
          hugeRangeWarned = true;

          continue;
        }

        bool allInRange = (bind >= range.BaseShaderRegister && rangeSize <= range.NumDescriptors);

        // move to the first descriptor in the range which is in the binding we want, if the binding
        // is later on in the range.
        //
        // It's also possible that the range is later on in the binding (e.g. if the binding is at
        // base register 5 and is 1000000 in length, the range could start at register 10. In that
        // case we just consume as much of the range as still fits in the bind
        if(bind > range.BaseShaderRegister)
          desc += (bind - range.BaseShaderRegister);
        if(range.BaseShaderRegister > bind)
          rangeSize -= (range.BaseShaderRegister - bind);

        if(range.RangeType == D3D12_DESCRIPTOR_RANGE_TYPE_CBV)
        {
          for(UINT i = 0; i < num && i < rangeSize; i++)
          {
            ResourceId id = WrappedID3D12Resource::GetResIDFromAddr(desc->GetCBV().BufferLocation);

            AddResourceUsage(eventNode, id, cb);

            desc++;
          }
        }
        else if(range.RangeType == D3D12_DESCRIPTOR_RANGE_TYPE_SRV ||
                range.RangeType == D3D12_DESCRIPTOR_RANGE_TYPE_UAV)
        {
          ResourceUsage usage = range.RangeType == D3D12_DESCRIPTOR_RANGE_TYPE_SRV ? ro : rw;

          for(UINT i = 0; i < num && i < rangeSize; i++)
          {
            AddResourceUsage(eventNode, desc->GetResResourceId(), usage);
            if(range.RangeType == D3D12_DESCRIPTOR_RANGE_TYPE_UAV &&
               desc->GetCounterResourceId() != ResourceId())
              AddResourceUsage(eventNode, desc->GetCounterResourceId(), usage);

            desc++;
          }
        }

        // if this descriptor range fully covered the binding (which may be quite common) we can
        // return now, other ranges/root elements won't overlap so don't bother looking at them
        if(allInRange)
          return;
      }
    }
  }
}

void D3D12CommandData::AddUsage(const D3D12RenderState &state, D3D12EventNode &eventNode)
{
  ActionDescription &a = eventNode.action;

  ActionFlags DrawMask = ActionFlags::Drawcall | ActionFlags::MeshDispatch | ActionFlags::Dispatch;
  if(!(a.flags & DrawMask))
    return;

  D3D12ResourceManager *rm = m_pDevice->GetResourceManager();

  const D3D12RenderState::RootSignature *rootsig = NULL;

  WrappedID3D12PipelineState *pipe = NULL;

  if(state.pipe != ResourceId())
    pipe = rm->GetResAs<WrappedID3D12PipelineState>(state.pipe);

  WrappedID3D12Shader *shaders[NumShaderStages] = {};
  const ShaderReflection *refls[NumShaderStages] = {};

  if((a.flags & ActionFlags::Dispatch) && pipe && pipe->IsCompute())
  {
    shaders[uint32_t(ShaderStage::Compute)] =
        (WrappedID3D12Shader *)pipe->compute->CS.pShaderBytecode;
  }
  else if(!(a.flags & ActionFlags::Dispatch) && pipe && pipe->IsGraphics())
  {
    D3D12_SHADER_BYTECODE *srcArr[] = {
        &pipe->graphics->VS,
        &pipe->graphics->HS,
        &pipe->graphics->DS,
        &pipe->graphics->GS,
        &pipe->graphics->PS,
        // compute
        NULL,
        &pipe->graphics->AS,
        &pipe->graphics->MS,
    };
    for(size_t stage = 0; stage < ARRAY_COUNT(srcArr); stage++)
    {
      if(srcArr[stage])
        shaders[stage] = (WrappedID3D12Shader *)srcArr[stage]->pShaderBytecode;
    }
  }

  // Index every shader action on the existing draw/dispatch action-building path. Submission
  // merging keeps each rebased event, so GetUsage(shaderId) needs no replay or EID traversal.
  for(size_t stage = 0; stage < ARRAY_COUNT(shaders); stage++)
  {
    if(shaders[stage])
      AddResourceUsage(eventNode, shaders[stage]->GetResourceID(), ResourceUsage::Shader);
  }

  if((a.flags & ActionFlags::Dispatch) && state.compute.rootsig != ResourceId())
  {
    rootsig = &state.compute;

    if(shaders[uint32_t(ShaderStage::Compute)])
      refls[uint32_t(ShaderStage::Compute)] =
          &shaders[uint32_t(ShaderStage::Compute)]->GetDetails();
  }
  else if(state.graphics.rootsig != ResourceId())
  {
    rootsig = &state.graphics;

    for(size_t stage = 0; stage < ARRAY_COUNT(shaders); stage++)
    {
      if(shaders[stage])
        refls[stage] = &shaders[stage]->GetDetails();
    }

    if(a.flags & ActionFlags::Indexed && state.ibuffer.buf != ResourceId())
      eventNode.resourceUsage.push_back(make_rdcpair(state.ibuffer.buf, ResourceUsage::IndexBuffer));

    if(a.flags & ActionFlags::Drawcall)
    {
      for(size_t i = 0; i < state.vbuffers.size(); i++)
      {
        if(state.vbuffers[i].buf != ResourceId())
          eventNode.resourceUsage.push_back(
              make_rdcpair(state.vbuffers[i].buf, ResourceUsage::VertexBuffer));
      }

      for(size_t i = 0; i < state.streamouts.size(); i++)
      {
        if(state.streamouts[i].buf != ResourceId())
          eventNode.resourceUsage.push_back(
              make_rdcpair(state.streamouts[i].buf, ResourceUsage::StreamOut));
        if(state.streamouts[i].countbuf != ResourceId())
          eventNode.resourceUsage.push_back(
              make_rdcpair(state.streamouts[i].countbuf, ResourceUsage::StreamOut));
      }
    }

    rdcarray<ResourceId> rts = state.GetRTVIDs();

    for(size_t i = 0; i < rts.size(); i++)
    {
      if(rts[i] != ResourceId())
        eventNode.resourceUsage.push_back(make_rdcpair(rts[i], ResourceUsage::ColorTarget));
    }

    ResourceId id = state.GetDSVID();
    if(id != ResourceId())
      eventNode.resourceUsage.push_back(make_rdcpair(id, ResourceUsage::DepthStencilTarget));
  }

  if(rootsig)
  {
    // iterate over each stage, looking at its used binds, then for each bind find it in the root
    // signature. We have to do this kind of N:N lookup because of D3D12's bad design, but this
    // should be a better way around to do it than iterating over the root signature and finding a
    // bind for each element
    for(size_t sh = 0; sh < ARRAY_COUNT(refls); sh++)
    {
      if(!refls[sh])
        continue;

      for(const ConstantBlock &b : refls[sh]->constantBlocks)
      {
        AddUsageForBindInRootSig(state, eventNode, rootsig, D3D12_DESCRIPTOR_RANGE_TYPE_CBV,
                                 b.fixedBindSetOrSpace, b.fixedBindNumber, b.bindArraySize);
      }

      for(const ShaderResource &r : refls[sh]->readOnlyResources)
      {
        AddUsageForBindInRootSig(state, eventNode, rootsig, D3D12_DESCRIPTOR_RANGE_TYPE_SRV,
                                 r.fixedBindSetOrSpace, r.fixedBindNumber, r.bindArraySize);
      }

      for(const ShaderResource &r : refls[sh]->readWriteResources)
      {
        AddUsageForBindInRootSig(state, eventNode, rootsig, D3D12_DESCRIPTOR_RANGE_TYPE_UAV,
                                 r.fixedBindSetOrSpace, r.fixedBindNumber, r.bindArraySize);
      }
    }
  }
}

void D3D12CommandData::BakeEventNodes(ActionDescription &rootAction)
{
  // EIDs start from 1
  m_Events.resize(1);
  uint32_t eid = 1;
  uint32_t actionId = 1;
  rdcarray<APIEvent> actionEvents;

  rdcarray<ActionDescription *> actionStack;
  actionStack.push_back(&rootAction);

  ActionDescription *parentAction = actionStack.back();

  std::map<ResourceId, uint32_t> cmdListStarts;

  for(const D3D12EventNode &node : m_EventNodes)
  {
    m_Events.push_back(node.event);
    APIEvent &apievent = m_Events.back();
    apievent.eventId = eid;
    actionEvents.push_back(apievent);

    for(const DebugMessage &msg : node.debugMessages)
    {
      DebugMessage m(msg);
      m.eventId = eid;
      m_pDevice->AddDebugMessage(m);
    }

    if(apievent.annotations)
      m_EventAnnotations.push_back(apievent.annotations);

    for(auto it = node.resourceUsage.begin(); it != node.resourceUsage.end(); ++it)
    {
      ResourceUsage usage = it->second;
      AppendFrameResourceUsage(m_ResourceUses[it->first], {eid, usage});
    }

    if(node.action.actionId == UINT32_MAX)
    {
      parentAction->children.push_back(node.action);
      ActionDescription &action = parentAction->children.back();

      RDCASSERT(action.events.isEmpty());
      action.events.swap(actionEvents);
      action.eventId = eid;
      action.actionId = actionId;

      ActionFlags flags = action.flags;
      if(flags & ActionFlags::PushMarker)
      {
        actionStack.push_back(parentAction);
        parentAction = &action;
      }

      if(!action.events.empty() && node.addActionUse)
      {
        auto it = cmdListStarts.find(node.cmdListID);
        uint32_t startEID = 0;
        if(it != cmdListStarts.end())
          startEID = it->second;

        ActionUse use(action.events.back().fileOffset, eid, node.cmdListID, eid - startEID);

        // insert in sorted location
        auto drawit = std::lower_bound(m_ActionUses.begin(), m_ActionUses.end(), use);
        m_ActionUses.insert(drawit - m_ActionUses.begin(), use);
      }

      if(flags & ActionFlags::PopMarker)
      {
        if(actionStack.count() == 0)
        {
          RDCWARN("Pop but the action stack is empty");
          parentAction = &rootAction;
        }
        else
        {
          parentAction = actionStack.back();
          RDCASSERT(parentAction);
          if(!parentAction)
          {
            RDCERR("NULL parent on the action stack");
            parentAction = &rootAction;
          }
          actionStack.pop_back();
        }
      }

      // markers don't increment action ID
      ActionFlags MarkerMask = ActionFlags::SetMarker | ActionFlags::PushMarker |
                               ActionFlags::PopMarker | ActionFlags::PassBoundary;
      bool isMarker = (flags & MarkerMask) ? true : false;
      if(!isMarker)
        ++actionId;
    }

    if(node.addPrimaryExecute)
    {
      RDCASSERT(node.primaryCmdId != ResourceId());
      cmdListStarts[node.primaryCmdId] = eid + 1;
      m_Partial[D3D12CommandData::Primary].cmdListExecs[node.primaryCmdId].push_back(eid + 1);
    }
    ++eid;
  }

  BuildReplayWriteTracking();

  // release the EventNodes now they are baked into actions and events
  m_EventNodes.clear();
  for(auto it = m_BakedCmdListInfo.begin(); it != m_BakedCmdListInfo.end(); ++it)
    it->second.eventNodes.clear();
}

void D3D12CommandData::AddAction(const ActionDescription &a)
{
  RDCASSERT(m_AddedEventNode);

  D3D12EventNode &node = GetLastEventNode();
  ActionDescription &action = node.action;
  action = a;

  for(int i = 0; i < 8; i++)
    action.outputs[i] = ResourceId();

  action.depthOut = ResourceId();

  if(m_LastCmdListID != ResourceId())
  {
    node.addActionUse = true;
    node.cmdListID = m_LastCmdListID;

    rdcarray<ResourceId> rts = m_BakedCmdListInfo[m_LastCmdListID].state.GetRTVIDs();

    for(size_t i = 0; i < ARRAY_COUNT(action.outputs); i++)
    {
      if(i < rts.size())
        action.outputs[i] = rts[i];
      else
        action.outputs[i] = ResourceId();
    }

    action.depthOut = m_BakedCmdListInfo[m_LastCmdListID].state.GetDSVID();
  }

  // Mark the action as active
  action.actionId = UINT32_MAX;

  if(m_LastCmdListID != ResourceId())
    AddUsage(m_BakedCmdListInfo[m_LastCmdListID].state, node);
}

SDObject *D3D12CommandData::InsertEventNodes(WrappedID3D12GraphicsCommandList *replayList,
                                             ResourceId cmd, BakedCmdListInfo &cmdListInfo)
{
  // Start with the Queue annotation (RootAnnotations)
  SDObject *localAnnotations = NULL;
  if(m_RootAnnotation)
    localAnnotations = m_RootAnnotation->Duplicate();

  rdcarray<D3D12EventNode> &eventNodes = cmdListInfo.eventNodes;
  for(size_t i = 0; i < eventNodes.size(); i++)
  {
    if(eventNodes[i].hasExecuteData)
    {
      // Readback the patch buffer and update the event nodes
      // The reserved execute indirect event nodes might get duplicated or deleted
      // This modifies eventNodes including the current node
      replayList->FinaliseExecuteIndirectEvents(cmdListInfo, i);
      if(m_pDevice->HasFatalError())
        return localAnnotations;
    }

    D3D12EventNode n = eventNodes[i];

    if(localAnnotations)
    {
      // Modify using the annotations stored in the event node
      for(const PendingAnnotation &annot : n.annotations)
      {
        if(annot.valueType == eRENDERDOC_Empty)
          localAnnotations->EraseChildByKeyPath(annot.key);
        else
          WriteAnnotation(localAnnotations->CreateChildByKeyPath(annot.key), annot.valueType,
                          annot.valueVectorWidth, annot.value);
      }
      n.event.annotations = localAnnotations->Duplicate();
    }
    m_EventNodes.push_back(n);
  }
  cmdListInfo.eventCount = (uint32_t)eventNodes.size();

  return localAnnotations;
}
