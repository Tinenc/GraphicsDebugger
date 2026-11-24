/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2026 Baldur Karlsson
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

#pragma once

#include <QFrame>
#include <QElapsedTimer>
#include <QVector>
#include <atomic>
#include <memory>
#include "Code/Interface/QRDInterface.h"

class QLabel;
class QProgressBar;
class QResizeEvent;
class QScrollArea;
class QToolButton;
class QTimer;
class RevisionThumbnail;
class TextureViewer;

namespace RenderTargetHistoryPolicy
{
struct TabBinding
{
  bool explicitlyOpened = false;
  uint32_t selectedEvent = 0;
};

struct TabPresentation
{
  bool visible = false;
  uint32_t activeEvent = 0;
};

enum class StaticRevisionChange
{
  DisplayUnchanged,
  Zoom,
  Pan,
  Channels,
  Range,
  Background,
  ShaderReplacement,
};

enum class StaticRevisionCacheAction
{
  Reuse,
  Capture,
};

TabPresentation ResolveTabPresentation(bool currentTab, const TabBinding *binding);
void ClearTabBinding(TabBinding &binding);
StaticRevisionCacheAction ResolveStaticRevisionCacheAction(bool hasCachedResult,
                                                            StaticRevisionChange change);
}

class RenderTargetHistoryView : public QFrame, public ICaptureViewer
{
public:
  static RenderTargetHistoryView *Open(ICaptureContext &ctx, ResourceId resource,
                                       const Subresource &subresource, CompType typeCast,
                                       uint32_t anchorEID, QWidget *referenceWindow);

  explicit RenderTargetHistoryView(TextureViewer &textureViewer, ICaptureContext &ctx,
                                   QWidget *parent = 0);
  ~RenderTargetHistoryView();

  void SetTarget(ResourceId resource, const Subresource &subresource, CompType typeCast,
                 uint32_t anchorEID);
  void Deactivate();
  void SelectNearestEvent(uint32_t eventId);
  bool StepSelection(int delta);

  ResourceId Resource() const { return m_Resource; }
  Subresource SelectedSubresource() const { return m_Subresource; }
  CompType TypeCast() const { return m_TypeCast; }
  uint32_t SelectedEvent() const;

  // ICaptureViewer
  void OnCaptureLoaded() override;
  void OnCaptureClosed() override;
  void OnSelectedEventChanged(uint32_t eventId) override {}
  void OnEventChanged(uint32_t eventId) override;
  void OnScrubEvent(uint32_t eventId) override;
  void OnReplayInput();

protected:
  void resizeEvent(QResizeEvent *event) override;

private:
  struct Revision
  {
    int cacheIndex = -1;
    uint32_t eventId = 0;
    ResourceUsage usage = ResourceUsage::Unused;
    QString actionName;
    QString usageName;
    RevisionThumbnail *thumbnail = NULL;
  };

  struct CacheEntry;
  struct ReplayJob;
  struct ThumbnailResult;

  static std::shared_ptr<CacheEntry> AcquireCache(ICaptureContext &ctx, ResourceId resource,
                                                  const Subresource &subresource,
                                                  CompType typeCast);
  static QMap<QString, std::shared_ptr<CacheEntry>> &CacheRegistry();

  void StartLoad();
  void ResumeAfterScrub();
  void BeginValidation();
  void QueueThumbnail(int index);
  void ApplyThumbnail(const std::shared_ptr<ThumbnailResult> &result);
  void CancelWork(bool block = false);

  void ClearThumbnailWidgets();
  void RebuildThumbnails();
  void RefreshThumbnailImages();
  void FinishLoad(uint64_t mutationGeneration);
  void ScheduleThumbnailRefresh();
  void RefreshVisibleThumbnails();
  void QueueNextThumbnail();
  void PruneThumbnails();
  void UpdateDiagnostics();

  void SelectIndex(int index, bool ensureVisible = true);
  void SelectEvent(uint32_t eventId);
  void JumpToSelectedEvent();
  void UpdateCurrentEventMarkers();
  void UpdateSelectionText();
  void UpdateHeader();

  TextureViewer &m_TextureViewer;
  ICaptureContext &m_Ctx;
  ResourceId m_Resource;
  Subresource m_Subresource;
  CompType m_TypeCast = CompType::Typeless;
  uint32_t m_AnchorEID = 0;
  uint32_t m_CurrentEID = 0;

  QLabel *m_ResourceLabel = NULL;
  QLabel *m_SubresourceLabel = NULL;
  QLabel *m_SelectionText = NULL;
  QLabel *m_StatusText = NULL;
  QProgressBar *m_Progress = NULL;
  QScrollArea *m_ScrollArea = NULL;
  QWidget *m_ThumbnailContainer = NULL;
  QToolButton *m_Previous = NULL;
  QToolButton *m_Next = NULL;
  QToolButton *m_Close = NULL;

  QVector<Revision> m_Revisions;
  int m_SelectedIndex = -1;
  std::shared_ptr<CacheEntry> m_Cache;
  std::shared_ptr<ReplayJob> m_Job;
  QVector<int> m_VisibleIndices;
  uint64_t m_ViewGeneration = 1;
  uint64_t m_ImageUseSerial = 0;
  uint64_t m_RequestSerial = 0;
  bool m_RefreshScheduled = false;
  bool m_ApplyingBatch = false;
  bool m_ScrollPending = false;
  bool m_ReplayThreadAvailable = false;
  QTimer *m_ScrubResumeTimer = NULL;
  QElapsedTimer m_UIHeartbeatClock;
  uint64_t m_UIHeartbeatSamples = 0;
  double m_MaxLoadingHeartbeatMS = 0.0;
  bool m_PreviousHeartbeatLoading = false;
};
