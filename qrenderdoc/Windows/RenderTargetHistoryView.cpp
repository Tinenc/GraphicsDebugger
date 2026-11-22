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

#include "RenderTargetHistoryView.h"

#include <QApplication>
#include <QCoreApplication>
#include <QDateTime>
#include <QFileInfo>
#include <QFontMetrics>
#include <QHBoxLayout>
#include <QImage>
#include <QLabel>
#include <QMap>
#include <QMouseEvent>
#include <QPainter>
#include <QPointer>
#include <QProgressBar>
#include <QResizeEvent>
#include <QScrollArea>
#include <QScrollBar>
#include <QShortcut>
#include <QStyle>
#include <QTimer>
#include <QToolButton>
#include <QVBoxLayout>
#include <QWheelEvent>
#include <algorithm>
#include <cmath>
#include <limits>
#include <map>
#include <vector>
#include "Code/QRDUtils.h"
#include "Code/Resources.h"
#include "Windows/TextureViewer.h"

RenderTargetHistoryPolicy::TabPresentation RenderTargetHistoryPolicy::ResolveTabPresentation(
    bool currentTab, const TabBinding *binding)
{
  if(currentTab || binding == NULL || !binding->explicitlyOpened)
    return {};

  return {true, binding->selectedEvent};
}

void RenderTargetHistoryPolicy::ClearTabBinding(TabBinding &binding)
{
  binding = {};
}

RenderTargetHistoryPolicy::StaticRevisionCacheAction
RenderTargetHistoryPolicy::ResolveStaticRevisionCacheAction(bool hasCachedResult,
                                                             StaticRevisionChange change)
{
  if(!hasCachedResult || change == StaticRevisionChange::ShaderReplacement)
    return StaticRevisionCacheAction::Capture;

  return StaticRevisionCacheAction::Reuse;
}

namespace
{
// i18n-ignore: low-priority replay-queue tag, never shown to the user.
const rdcstr historyBackgroundTag = "ReplayLab.background.RenderTargetHistory";
constexpr int MaxCachedThumbnails = 24;
constexpr uint64_t MaxHistoryImageBytes = 32ULL * 1024 * 1024;
constexpr int MaxTargetCaches = 8;
constexpr int ThumbnailPitch = 174;
constexpr int ThumbnailPrefetchPixels = 2 * ThumbnailPitch;

class ScopedHistoryUITiming
{
public:
  ScopedHistoryUITiming(QObject *owner, const char *operation) : m_Owner(owner), m_Operation(operation)
  {
    static const bool enabled = qEnvironmentVariableIntValue("RENDERDOC_EID_TIMING") == 1;
    if(enabled)
      m_Timer.start();
  }
  ~ScopedHistoryUITiming()
  {
    if(!m_Timer.isValid() || !m_Owner)
      return;
    const double elapsedMS = double(m_Timer.nsecsElapsed()) / 1000000.0;
    // i18n-ignore: bounded numeric profiling data and stable operation IDs, not UI text.
    QVariantMap spans = m_Owner->property("historyUISpans").toMap();
    const QString key = QString::fromLatin1(m_Operation);
    QVariantMap span = spans.value(key).toMap();
    // i18n-ignore: JSON key for numeric diagnostic duration, never a UI label.
    span[lit("lastMs")] = elapsedMS;
    // i18n-ignore: JSON key for numeric diagnostic maximum, never a UI label.
    span[lit("maximumMs")] = qMax(span.value(lit("maximumMs")).toDouble(), elapsedMS);
    // i18n-ignore: JSON key for numeric diagnostic count, never a UI label.
    span[lit("samples")] = span.value(lit("samples")).toULongLong() + 1;
    spans[key] = span;
    m_Owner->setProperty("historyUISpans", spans);
  }
private:
  QPointer<QObject> m_Owner;
  const char *m_Operation;
  QElapsedTimer m_Timer;
};

QRect ThumbnailRect(int index)
{
  return QRect(4 + index * ThumbnailPitch, 4, 168, 118);
}

int OldestThumbnail(const QVector<uint64_t> &lastUsed, const QVector<int> &protectedIndices)
{
  int oldest = -1;
  for(int i = 0; i < lastUsed.count(); i++)
    if(lastUsed[i] != 0 && !protectedIndices.contains(i) &&
       (oldest < 0 || lastUsed[i] < lastUsed[oldest]))
      oldest = i;
  return oldest;
}
struct HistoryCandidate
{
  uint32_t eventId = 0;
  ResourceUsage usage = ResourceUsage::Unused;
};

QString HistoryTargetKey(const QString &captureIdentity, const QString &resource,
                         const Subresource &subresource, CompType typeCast)
{
  // i18n-ignore: internal process-lifetime cache key, never shown to the user.
  return QFormatStr("%1/%2/%3/%4/%5/%6")
      .arg(captureIdentity)
      .arg(resource)
      .arg(subresource.mip)
      .arg(subresource.slice)
      .arg(subresource.sample)
      .arg(uint32_t(typeCast));
}

QString CaptureIdentity(ICaptureContext &ctx)
{
  const rdcstr captureFilename = ctx.GetCaptureFilename();
  const QString filename =
      QString::fromUtf8(captureFilename.c_str(), int(captureFilename.size()));
  QFileInfo info(filename);
  QString path = info.canonicalFilePath();
  if(path.isEmpty())
    path = info.absoluteFilePath();

  // Keep cached images across child-view and Texture Viewer closure. File metadata prevents a
  // capture overwritten at the same path from accidentally reusing an older process cache.
  // i18n-ignore: internal process-lifetime cache identity, never shown to the user.
  return QFormatStr("%1/%2/%3/%4")
      .arg((qulonglong)(void *)&ctx)
      .arg(path)
      .arg(info.exists() ? info.size() : -1)
      .arg(info.exists() ? info.lastModified().toMSecsSinceEpoch() : 0);
}

bool IsReadWriteUsage(ResourceUsage usage)
{
  return usage >= ResourceUsage::VS_RWResource && usage <= ResourceUsage::All_RWResource;
}

bool IsHistoryWriteUsage(ResourceUsage usage)
{
  if(IsReadWriteUsage(usage))
    return true;

  switch(usage)
  {
    case ResourceUsage::ColorTarget:
    case ResourceUsage::DepthStencilTarget:
    case ResourceUsage::Clear:
    case ResourceUsage::Discard:
    case ResourceUsage::GenMips:
    case ResourceUsage::Resolve:
    case ResourceUsage::ResolveDst:
    case ResourceUsage::Copy:
    case ResourceUsage::CopyDst: return true;
    default: return false;
  }
}

int UsagePriority(ResourceUsage usage)
{
  switch(usage)
  {
    case ResourceUsage::Clear: return 100;
    case ResourceUsage::ResolveDst:
    case ResourceUsage::Resolve: return 90;
    case ResourceUsage::CopyDst:
    case ResourceUsage::Copy: return 80;
    case ResourceUsage::ColorTarget: return 70;
    case ResourceUsage::DepthStencilTarget: return 60;
    case ResourceUsage::GenMips: return 50;
    case ResourceUsage::Discard: return 40;
    default: return IsReadWriteUsage(usage) ? 30 : 0;
  }
}

bool CandidateMayAffectSubresource(const HistoryCandidate &candidate,
                                   const Subresource &subresource)
{
  // GetUsage is resource-wide and ActionDescription only retains the first subresource for some
  // multi-range operations. Keep write candidates conservatively. GenerateMips is the one safe
  // reduction: it reads the base mip and writes the remainder of the chain.
  if(candidate.usage == ResourceUsage::GenMips)
    return subresource.mip > 0;
  return true;
}

QVector<HistoryCandidate> BuildHistoryCandidates(const rdcarray<EventUsage> &usage,
                                                 const Subresource &subresource)
{
  std::map<uint32_t, ResourceUsage> unique;

  for(const EventUsage &event : usage)
  {
    HistoryCandidate candidate = {event.eventId, event.usage};
    if(event.eventId == 0 || !IsHistoryWriteUsage(event.usage) ||
       !CandidateMayAffectSubresource(candidate, subresource))
      continue;

    auto it = unique.find(event.eventId);
    if(it == unique.end() || UsagePriority(event.usage) > UsagePriority(it->second))
      unique[event.eventId] = event.usage;
  }

  QVector<HistoryCandidate> ret;
  ret.reserve((int)unique.size());
  for(const auto &event : unique)
    ret.push_back({event.first, event.second});

  return ret;
}

int NearestRevisionIndex(const QVector<uint32_t> &events, uint32_t anchor)
{
  if(events.isEmpty())
    return -1;

  int nearest = 0;
  for(int i = 0; i < events.count(); i++)
  {
    if(events[i] > anchor)
      break;
    nearest = i;
  }

  return nearest;
}

QSize PreviewSize(const TextureDescription &texture, const Subresource &subresource, int count)
{
  const uint32_t mipWidth = qMax(1U, texture.width >> qMin(subresource.mip, 31U));
  const uint32_t mipHeight = qMax(1U, texture.height >> qMin(subresource.mip, 31U));
  const double aspect = double(mipWidth) / double(mipHeight);

  int width = 256;
  int height = qMax(1, int(std::round(double(width) / aspect)));
  if(height > 144)
  {
    height = 144;
    width = qMax(1, int(std::round(double(height) * aspect)));
  }

  // Resolution is independent of history length: only a bounded neighborhood is decoded.
  Q_UNUSED(count);
  return QSize(width, height);
}

bool IsValidRGBPayload(size_t byteCount, const QSize &size)
{
  if(size.width() <= 0 || size.height() <= 0)
    return false;

  const uint64_t width = uint64_t(size.width());
  const uint64_t height = uint64_t(size.height());
  if(width > std::numeric_limits<uint64_t>::max() / height ||
     width * height > std::numeric_limits<uint64_t>::max() / 3ULL)
    return false;

  return uint64_t(byteCount) == width * height * 3ULL;
}

int DominantWheelDelta(const QPoint &delta)
{
  return qAbs(delta.x()) > qAbs(delta.y()) ? delta.x() : delta.y();
}

bool HorizontalWheelScrollValue(int current, int minimum, int maximum,
                                const QPoint &pixelDelta, const QPoint &angleDelta,
                                int singleStep, int wheelLines, int &result)
{
  if(maximum <= minimum)
    return false;

  int movement = DominantWheelDelta(pixelDelta);
  if(movement == 0)
  {
    const int angle = DominantWheelDelta(angleDelta);
    if(angle == 0)
      return false;

    movement = qRound(double(angle) * double(qMax(1, singleStep)) *
                      double(qMax(1, wheelLines)) / 120.0);
  }

  result = qBound(minimum, current - movement, maximum);
  return result != current;
}

QVector<int> SelectVirtualizedThumbnailIndices(const QVector<QRect> &items,
                                               const QRect &visibleRect, int prefetchPixels,
                                               int maximum)
{
  struct RankedItem
  {
    int index = -1;
    int tier = 0;
    int distance = 0;
  };

  if(maximum <= 0 || visibleRect.isEmpty())
    return {};

  const QRect prefetchRect = visibleRect.adjusted(-prefetchPixels, 0, prefetchPixels, 0);
  QVector<RankedItem> ranked;
  ranked.reserve(items.count());
  for(int i = 0; i < items.count(); i++)
  {
    if(!items[i].intersects(prefetchRect))
      continue;

    RankedItem item;
    item.index = i;
    item.tier = items[i].intersects(visibleRect) ? 0 : 1;
    item.distance = qAbs(items[i].center().x() - visibleRect.center().x());
    ranked.push_back(item);
  }

  std::stable_sort(ranked.begin(), ranked.end(), [](const RankedItem &a, const RankedItem &b) {
    if(a.tier != b.tier)
      return a.tier < b.tier;
    if(a.distance != b.distance)
      return a.distance < b.distance;
    return a.index < b.index;
  });

  QVector<int> selected;
  selected.reserve(qMin(maximum, ranked.count()));
  for(const RankedItem &item : ranked)
  {
    if(selected.count() >= maximum)
      break;
    selected.push_back(item.index);
  }
  return selected;
}
}

class HorizontalWheelScrollArea : public QScrollArea
{
public:
  using QScrollArea::QScrollArea;

protected:
  void wheelEvent(QWheelEvent *event) override
  {
    QScrollBar *scrollBar = horizontalScrollBar();
    int value = scrollBar->value();
    if(HorizontalWheelScrollValue(value, scrollBar->minimum(), scrollBar->maximum(),
                                  event->pixelDelta(), event->angleDelta(),
                                  scrollBar->singleStep(), QApplication::wheelScrollLines(), value))
    {
      scrollBar->setValue(value);
      event->accept();
      return;
    }

    QScrollArea::wheelEvent(event);
  }
};

class RevisionThumbnail : public QFrame
{
public:
  RevisionThumbnail(uint32_t eventId, const QString &usage, QWidget *parent = 0)
      : QFrame(parent), m_EventId(eventId), m_Usage(usage)
  {
    setFixedSize(168, 118);
    setCursor(Qt::PointingHandCursor);
    setFocusPolicy(Qt::ClickFocus);
    setFrameStyle(QFrame::NoFrame);

    // i18n-ignore: internal Qt object/property names for bounded history diagnostics.
    setObjectName(lit("historyRevision"));
    setProperty("historyEventId", eventId);
  }

  void SetImage(const QImage &image, bool unavailable)
  {
    m_Image = image;
    m_Unavailable = unavailable;
    // i18n-ignore: read-only thumbnail completion diagnostic, not a UI label.
    setProperty("historyThumbnailReady", !image.isNull());
    setProperty("historyThumbnailUnavailable", unavailable);
    update();
  }
  void SetSelected(bool selected)
  {
    m_Selected = selected;
    update();
  }
  void SetCurrent(bool current)
  {
    m_Current = current;
    update();
  }

  std::function<void()> clicked;
  std::function<void()> doubleClicked;

protected:
  void paintEvent(QPaintEvent *) override
  {
    QPainter painter(this);
    painter.setRenderHint(QPainter::SmoothPixmapTransform, true);

    QColor border = palette().color(QPalette::Mid);
    if(m_Selected)
      border = palette().color(QPalette::Highlight);
    painter.setPen(QPen(border, m_Selected ? 3 : 1));
    painter.setBrush(palette().brush(QPalette::Base));
    painter.drawRect(rect().adjusted(1, 1, -2, -2));

    const QRect imageRect = ImageRect();
    painter.fillRect(imageRect, palette().brush(QPalette::Dark));
    if(!m_Image.isNull())
    {
      QSize target = m_Image.size();
      target.scale(imageRect.size(), Qt::KeepAspectRatio);
      QRect targetRect(QPoint(), target);
      targetRect.moveCenter(imageRect.center());
      painter.drawImage(targetRect, m_Image);
    }
    else
    {
      painter.setPen(palette().color(QPalette::Disabled, QPalette::Text));
      painter.drawText(imageRect, Qt::AlignCenter,
                       m_Unavailable
                           ? QCoreApplication::translate("RenderTargetHistoryView", "Unavailable")
                           : QCoreApplication::translate("RenderTargetHistoryView", "Loading..."));
    }

    QFont labelFont = font();
    labelFont.setBold(true);
    painter.setFont(labelFont);
    painter.setPen(palette().color(QPalette::Text));
    painter.drawText(QRect(7, height() - 36, width() - 14, 16), Qt::AlignLeft | Qt::AlignVCenter,
                     QCoreApplication::translate("RenderTargetHistoryView", "EID %1")
                         .arg(m_EventId));

    labelFont.setBold(false);
    painter.setFont(labelFont);
    painter.drawText(QRect(7, height() - 20, width() - 14, 14), Qt::AlignLeft | Qt::AlignVCenter,
                     fontMetrics().elidedText(m_Usage, Qt::ElideRight, width() - 14));

    if(m_Current)
    {
      painter.setPen(QPen(palette().color(QPalette::Link), 3));
      painter.drawLine(5, height() - 4, width() - 5, height() - 4);
    }
  }

  void mousePressEvent(QMouseEvent *event) override
  {
    if(event->button() == Qt::LeftButton && clicked)
    {
      event->accept();
      setFocus(Qt::MouseFocusReason);
      clicked();
      return;
    }
    QFrame::mousePressEvent(event);
  }

  void mouseDoubleClickEvent(QMouseEvent *event) override
  {
    if(event->button() == Qt::LeftButton && doubleClicked)
    {
      event->accept();
      doubleClicked();
      return;
    }
    QFrame::mouseDoubleClickEvent(event);
  }

private:
  QRect ImageRect() const { return rect().adjusted(7, 7, -7, -39); }

  uint32_t m_EventId = 0;
  QString m_Usage;
  QImage m_Image;
  bool m_Unavailable = false;
  bool m_Selected = false;
  bool m_Current = false;
};

struct RenderTargetHistoryView::CacheEntry
{
  struct CachedRevision
  {
    HistoryCandidate candidate;
    QImage image;
    uint64_t lastUsed = 0;
    bool unavailable = false;
  };

  QVector<HistoryCandidate> candidates;
  QVector<CachedRevision> revisions;
  QSize previewSize;
  bool discoveryComplete = false;
  bool loadComplete = false;
  uint64_t mutationGeneration = 0;
  uint64_t lastTargetUse = 0;
};

struct RenderTargetHistoryView::ReplayJob
{
  std::atomic<bool> cancelled = {false};
  int index = -1;    // Negative for discovery or mutation validation.
};

struct RenderTargetHistoryView::ThumbnailResult
{
  int index = -1;
  QSize size;
  uint64_t mutationGeneration = 0;
  bool stale = false;
  bytebuf pixels;
};

QMap<QString, std::shared_ptr<RenderTargetHistoryView::CacheEntry>>
    &RenderTargetHistoryView::CacheRegistry()
{
  static QMap<QString, std::shared_ptr<CacheEntry>> caches;
  return caches;
}

std::shared_ptr<RenderTargetHistoryView::CacheEntry> RenderTargetHistoryView::AcquireCache(
    ICaptureContext &ctx, ResourceId resource, const Subresource &subresource, CompType typeCast)
{
  // GUI-thread-only metadata LRU, with a separate global image-byte budget. The virtualized
  // widget neighborhood is not an image-cache limit: ordinary 74-event histories fit entirely.
  auto &caches = CacheRegistry();
  static uint64_t useSerial = 0;
  const QString key = HistoryTargetKey(CaptureIdentity(ctx), ToQStr(resource), subresource, typeCast);
  std::shared_ptr<CacheEntry> entry = caches.value(key);
  if(!entry)
  {
    entry = std::make_shared<CacheEntry>();
    caches.insert(key, entry);
  }
  entry->lastTargetUse = ++useSerial;
  while(caches.count() > MaxTargetCaches)
  {
    auto oldest = caches.end();
    for(auto it = caches.begin(); it != caches.end(); ++it)
      if(it.key() != key &&
         (oldest == caches.end() || it.value()->lastTargetUse < oldest.value()->lastTargetUse))
        oldest = it;
    if(oldest == caches.end())
      break;
    caches.erase(oldest);
  }
  return entry;
}

RenderTargetHistoryView *RenderTargetHistoryView::Open(ICaptureContext &ctx, ResourceId resource,
                                                       const Subresource &subresource,
                                                       CompType typeCast, uint32_t anchorEID,
                                                       QWidget *referenceWindow)
{
  Q_UNUSED(referenceWindow);

  if(!ctx.HasTextureViewer())
    ctx.ShowTextureViewer();

  ITextureViewer *textureViewer = ctx.GetTextureViewer();
  TextureViewer *viewer =
      textureViewer ? qobject_cast<TextureViewer *>(textureViewer->Widget()) : NULL;
  if(!viewer)
    return NULL;

  return viewer->OpenRenderTargetHistory(resource, subresource, typeCast, anchorEID);
}

RenderTargetHistoryView::RenderTargetHistoryView(TextureViewer &textureViewer,
                                                  ICaptureContext &ctx, QWidget *parent)
    : QFrame(parent), m_TextureViewer(textureViewer), m_Ctx(ctx), m_CurrentEID(ctx.CurEvent())
{
  m_ReplayThreadAvailable = ctx.IsCaptureLoaded();
  if(qEnvironmentVariableIntValue("RENDERDOC_EID_TIMING") == 1)
  {
    // Measure the native GUI event loop independently of Python fixture callbacks/GIL work.
    // Count a loading interval only if it was active at its beginning, so an unrelated long
    // callback before opening this view cannot be attributed to thumbnail loading.
    QTimer *heartbeat = new QTimer(this);
    heartbeat->setTimerType(Qt::PreciseTimer);
    heartbeat->setInterval(10);
    m_UIHeartbeatClock.start();
    QObject::connect(heartbeat, &QTimer::timeout, this, [this]() {
      const double elapsedMS = double(m_UIHeartbeatClock.nsecsElapsed()) / 1000000.0;
      m_UIHeartbeatClock.restart();
      if(m_PreviousHeartbeatLoading)
      {
        ++m_UIHeartbeatSamples;
        m_MaxLoadingHeartbeatMS = qMax(m_MaxLoadingHeartbeatMS, elapsedMS);
        // i18n-ignore: numeric opt-in event-loop diagnostics, not user-visible labels.
        setProperty("historyNativeHeartbeatSamples", qulonglong(m_UIHeartbeatSamples));
        setProperty("historyNativeMaximumLoadingHeartbeatMs", m_MaxLoadingHeartbeatMS);
      }
      m_PreviousHeartbeatLoading = isVisible() && m_Ctx.IsCaptureLoaded() &&
          !m_Ctx.IsScrubbing() && m_Job && !m_Job->cancelled.load();
    });
    heartbeat->start();
  }
  m_ScrubResumeTimer = new QTimer(this);
  m_ScrubResumeTimer->setSingleShot(true);
  m_ScrubResumeTimer->setInterval(60);
  QObject::connect(m_ScrubResumeTimer, &QTimer::timeout, this, [this]() {
    if(!isVisible() || !m_Cache || !m_Ctx.IsCaptureLoaded())
      return;
    if(m_Ctx.IsScrubbing())
      ResumeAfterScrub();
    else
      StartLoad();
  });
  setObjectName(lit("renderTargetHistoryView"));    // i18n-ignore: internal Qt object name
  setFocusPolicy(Qt::StrongFocus);
  setFrameStyle(QFrame::StyledPanel | QFrame::Sunken);
  setMinimumHeight(196);

  QVBoxLayout *root = new QVBoxLayout(this);
  root->setContentsMargins(6, 4, 6, 4);
  root->setSpacing(4);

  QHBoxLayout *heading = new QHBoxLayout();
  QLabel *title = new QLabel(
      QCoreApplication::translate("RenderTargetHistoryView", "Render Target History"), this);
  QFont titleFont = title->font();
  titleFont.setBold(true);
  title->setFont(titleFont);
  heading->addWidget(title);

  m_ResourceLabel = new QLabel(this);
  m_ResourceLabel->setTextInteractionFlags(Qt::TextSelectableByMouse);
  heading->addWidget(m_ResourceLabel, 1);

  m_SubresourceLabel = new QLabel(this);
  heading->addWidget(m_SubresourceLabel);

  m_Close = new QToolButton(this);
  m_Close->setIcon(Icons::cross());
  m_Close->setAutoRaise(true);
  m_Close->setToolTip(
      QCoreApplication::translate("RenderTargetHistoryView", "Close render target history"));
  heading->addWidget(m_Close);
  root->addLayout(heading);

  QHBoxLayout *controls = new QHBoxLayout();
  m_Previous = new QToolButton(this);
  m_Previous->setArrowType(Qt::LeftArrow);
  m_Previous->setToolTip(
      QCoreApplication::translate("RenderTargetHistoryView", "Previous revision"));
  m_Next = new QToolButton(this);
  m_Next->setArrowType(Qt::RightArrow);
  m_Next->setToolTip(
      QCoreApplication::translate("RenderTargetHistoryView", "Next revision"));
  controls->addWidget(m_Previous);
  controls->addWidget(m_Next);

  m_SelectionText = new QLabel(this);
  m_SelectionText->setTextInteractionFlags(Qt::TextSelectableByMouse);
  controls->addWidget(m_SelectionText, 1);

  m_StatusText = new QLabel(this);
  m_StatusText->setToolTip(QCoreApplication::translate(
      "RenderTargetHistoryView",
      "Output events are resource-wide write candidates; a binding does not prove that every "
      "pixel or the selected subresource changed."));
  controls->addWidget(m_StatusText);

  m_Progress = new QProgressBar(this);
  m_Progress->setTextVisible(true);
  m_Progress->setRange(0, 0);
  m_Progress->setMaximumWidth(190);
  controls->addWidget(m_Progress);
  root->addLayout(controls);

  m_ScrollArea = new HorizontalWheelScrollArea(this);
  m_ScrollArea->setWidgetResizable(true);
  m_ScrollArea->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOn);
  m_ScrollArea->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
  m_ScrollArea->setMinimumHeight(146);
  m_ThumbnailContainer = new QWidget(m_ScrollArea);
  // i18n-ignore: internal Qt object name for scroll/input regression tests.
  m_ScrollArea->setObjectName(lit("renderTargetHistoryScroll"));
  m_ThumbnailContainer->setMinimumHeight(126);
  m_ScrollArea->setWidget(m_ThumbnailContainer);
  root->addWidget(m_ScrollArea);

  QObject::connect(m_Previous, &QToolButton::clicked, [this]() { StepSelection(-1); });
  QObject::connect(m_Next, &QToolButton::clicked, [this]() { StepSelection(1); });
  QObject::connect(m_Close, &QToolButton::clicked,
                   [this]() { m_TextureViewer.CloseRenderTargetHistory(); });
  QObject::connect(m_ScrollArea->horizontalScrollBar(), &QScrollBar::valueChanged, this,
                   [this]() { ScheduleThumbnailRefresh(); });
  QObject::connect(m_ScrollArea->horizontalScrollBar(), &QScrollBar::rangeChanged, this,
                   [this](int, int) { ScheduleThumbnailRefresh(); });

  auto addShortcut = [this](int key, const std::function<void()> &callback) {
    QShortcut *shortcut = new QShortcut(QKeySequence(key), this);
    shortcut->setContext(Qt::WidgetWithChildrenShortcut);
    QObject::connect(shortcut, &QShortcut::activated, callback);
  };
  addShortcut(Qt::Key_Left, [this]() { StepSelection(-1); });
  addShortcut(Qt::Key_Up, [this]() { StepSelection(-1); });
  addShortcut(Qt::Key_Right, [this]() { StepSelection(1); });
  addShortcut(Qt::Key_Down, [this]() { StepSelection(1); });
  addShortcut(Qt::Key_Home, [this]() { SelectIndex(0); });
  addShortcut(Qt::Key_End, [this]() { SelectIndex(m_Revisions.count() - 1); });
  addShortcut(Qt::Key_Return, [this]() { JumpToSelectedEvent(); });
  addShortcut(Qt::Key_Enter, [this]() { JumpToSelectedEvent(); });

  UpdateSelectionText();
  m_Ctx.AddCaptureViewer(this);
}

RenderTargetHistoryView::~RenderTargetHistoryView()
{
  CancelWork(true);
  m_Ctx.RemoveCaptureViewer(this);
  ClearThumbnailWidgets();
}

void RenderTargetHistoryView::Deactivate()
{
  m_ScrubResumeTimer->stop();
  CancelWork();
  hide();
}

void RenderTargetHistoryView::SetTarget(ResourceId resource, const Subresource &subresource,
                                        CompType typeCast, uint32_t anchorEID)
{
  const std::shared_ptr<CacheEntry> targetCache =
      AcquireCache(m_Ctx, resource, subresource, typeCast);
  const bool sameTarget = m_Resource == resource && m_Subresource == subresource &&
                          m_TypeCast == typeCast && m_Cache == targetCache;

  m_AnchorEID = anchorEID;
  m_CurrentEID = m_Ctx.CurEvent();

  if(sameTarget)
  {
    SelectNearestEvent(anchorEID);
    BeginValidation();
    return;
  }

  CancelWork();
  ClearThumbnailWidgets();

  m_Resource = resource;
  m_Subresource = subresource;
  m_TypeCast = typeCast;
  m_Cache = targetCache;
  UpdateHeader();

  if(m_Cache->discoveryComplete)
    RebuildThumbnails();

  StartLoad();
}

void RenderTargetHistoryView::OnCaptureLoaded()
{
  m_ReplayThreadAvailable = true;
  if(isVisible() && m_Resource != ResourceId())
    StartLoad();
}

void RenderTargetHistoryView::OnCaptureClosed()
{
  // Local headless outputs are destroyed inside their worker before capture shutdown proceeds.
  Deactivate();
  CancelWork(true);
  ClearThumbnailWidgets();
  m_Cache.reset();
  m_Resource = ResourceId();
  m_ReplayThreadAvailable = false;
}

void RenderTargetHistoryView::OnEventChanged(uint32_t eventId)
{
  m_CurrentEID = eventId;
  UpdateCurrentEventMarkers();

  // Live scrubbing publishes every completed event to all panels. History follows the marker,
  // but its independent revision replays must wait until interaction has ended.
  if(m_Ctx.IsScrubbing())
  {
    CancelWork();
    ResumeAfterScrub();
    return;
  }

  if(isVisible())
  {
    CancelWork();
    if(!m_ScrubResumeTimer->isActive())
      BeginValidation();
  }
}

void RenderTargetHistoryView::OnScrubEvent(uint32_t eventId)
{
  // During an interactive scrub move the marker and cancel revision work immediately. Cached
  // images remain available, but no history replay/readback is allowed to compete with the
  // interactive lane. The guarded timer resumes the explicitly opened history after release.
  m_CurrentEID = m_Ctx.CurEvent();
  UpdateCurrentEventMarkers();
  CancelWork();
  ResumeAfterScrub();
}

void RenderTargetHistoryView::OnReplayInput()
{
  CancelWork();
  if(isVisible() && m_Cache && m_Ctx.IsCaptureLoaded())
    m_ScrubResumeTimer->start(750);
}

void RenderTargetHistoryView::ResumeAfterScrub()
{
  // One timer per visible history view avoids a backlog of delayed jobs during a long drag.
  // Deactivation stops it; the timeout rechecks the current capture, target and interaction state.
  if(isVisible() && m_Cache && m_Ctx.IsCaptureLoaded() && !m_ScrubResumeTimer->isActive())
    m_ScrubResumeTimer->start();
}

void RenderTargetHistoryView::StartLoad()
{
  if(m_Ctx.IsScrubbing())
  {
    CancelWork();
    ResumeAfterScrub();
    return;
  }

  m_ScrubResumeTimer->stop();
  if(!m_Cache || !m_Ctx.IsCaptureLoaded() || m_Job)
    return;

  if(m_Cache->discoveryComplete)
  {
    BeginValidation();
    return;
  }

  m_StatusText->setText(
      QCoreApplication::translate("RenderTargetHistoryView", "Finding output events..."));
  m_Progress->show();
  m_Progress->setRange(0, 0);

  const ResourceId resource = m_Resource;
  const Subresource subresource = m_Subresource;
  const std::shared_ptr<CacheEntry> cache = m_Cache;
  const std::shared_ptr<ReplayJob> job = std::make_shared<ReplayJob>();
  m_Job = job;
  QPointer<RenderTargetHistoryView> self(this);
  m_Ctx.Replay().AsyncInvoke(
      historyBackgroundTag,
      [resource, subresource, cache, job, self](IReplayController *controller) {
        rdcarray<EventUsage> usage;
        if(!job->cancelled.load())
          usage = controller->GetUsage(resource);

        std::shared_ptr<rdcarray<EventUsage>> result =
            std::make_shared<rdcarray<EventUsage>>(std::move(usage));
        GUIInvoke::call(qApp, [self, cache, job, subresource, result]() {
          if(!self || job->cancelled.load() || self->m_Job != job || self->m_Cache != cache)
            return;

          // i18n-ignore: internal profiling operation ID.
          ScopedHistoryUITiming timing(self.data(), "discovery");

          cache->candidates = BuildHistoryCandidates(*result, subresource);
          cache->revisions.clear();
          cache->revisions.reserve(cache->candidates.count());
          for(const HistoryCandidate &candidate : cache->candidates)
          {
            CacheEntry::CachedRevision revision;
            revision.candidate = candidate;
            cache->revisions.push_back(revision);
          }

          TextureDescription *texture = self->m_Ctx.GetTexture(self->m_Resource);
          if(texture)
            cache->previewSize = PreviewSize(*texture, self->m_Subresource,
                                             cache->candidates.count());
          cache->discoveryComplete = true;
          cache->loadComplete = false;

          self->m_Job.reset();
          self->RebuildThumbnails();
          self->BeginValidation();
        });
      });
}

void RenderTargetHistoryView::BeginValidation()
{
  if(m_Ctx.IsScrubbing())
  {
    CancelWork();
    ResumeAfterScrub();
    return;
  }
  m_ScrubResumeTimer->stop();
  if(!m_Cache || !m_Cache->discoveryComplete || !m_Ctx.IsCaptureLoaded() || m_Job)
    return;
  if(m_Cache->candidates.isEmpty())
  {
    m_Progress->hide();
    m_StatusText->setText(QCoreApplication::translate(
        "RenderTargetHistoryView", "No render-target write candidates were recorded."));
    UpdateDiagnostics();
    return;
  }

  const std::shared_ptr<CacheEntry> cache = m_Cache;
  const std::shared_ptr<ReplayJob> job = std::make_shared<ReplayJob>();
  m_Job = job;
  QPointer<RenderTargetHistoryView> self(this);
  m_Ctx.Replay().AsyncInvoke(historyBackgroundTag, [self, cache, job](IReplayController *controller) {
    if(job->cancelled.load())
      return;
    const uint64_t generation = controller->GetShaderReplacementGeneration(0);
    GUIInvoke::call(qApp, [self, cache, job, generation]() {
      if(!self || job->cancelled.load() || self->m_Job != job || self->m_Cache != cache)
        return;
      // Earlier event replacements can change every later revision. This serial includes CBuffer
      // overrides and shader replacements; display-only controls do not invalidate small images.
      if(cache->mutationGeneration != generation || !cache->loadComplete)
      {
        for(CacheEntry::CachedRevision &revision : cache->revisions)
        {
          revision.image = QImage();
          revision.lastUsed = 0;
          revision.unavailable = revision.candidate.usage == ResourceUsage::Discard;
        }
      }
      cache->mutationGeneration = generation;
      cache->loadComplete = true;
      self->m_Job.reset();
      self->RefreshThumbnailImages();
      self->FinishLoad(generation);
      self->ScheduleThumbnailRefresh();
    });
  });
  // OnEventChanged does not carry a mutation generation. Expose pending validation explicitly;
  // do not treat every ordinary EID publication as proof that valid cached images became stale.
  UpdateDiagnostics();
}

void RenderTargetHistoryView::QueueThumbnail(int index)
{
  if(m_Job || !m_Cache || !m_Cache->loadComplete || !m_Ctx.IsCaptureLoaded() ||
     !isVisible() || m_Ctx.IsScrubbing() || index < 0 || index >= m_Revisions.count())
    return;

  const ResourceId resource = m_Resource;
  const Subresource subresource = m_Subresource;
  const CompType typeCast = m_TypeCast;
  const uint32_t eventId = m_Revisions[index].eventId;
  const QSize previewSize = m_Cache->previewSize;
  const uint64_t generation = m_Cache->mutationGeneration;
  const uint64_t viewGeneration = m_ViewGeneration;
  TextureDescription *texture = m_Ctx.GetTexture(resource);
  const bool isDepth = texture && ((texture->creationFlags & TextureCategory::DepthTarget) ||
                                   texture->format.compType == CompType::Depth);
  const std::shared_ptr<CacheEntry> cache = m_Cache;
  const std::shared_ptr<ReplayJob> job = std::make_shared<ReplayJob>();
  job->index = index;
  m_Job = job;
  m_RequestSerial++;
  // i18n-ignore: bounded read-only replay request diagnostics for regression fixtures.
  QVariantList requested = property("historyRequestedEvents").toList();
  requested.push_back(eventId);
  while(requested.count() > 64)
    requested.removeFirst();
  setProperty("historyRequestedEvents", requested);
  setProperty("historyLastRequestedEvent", eventId);
  UpdateDiagnostics();
  QPointer<RenderTargetHistoryView> self(this);
  QVector<int> indices = {index};
  // Selected revision first, then bounded ascending groups. Short histories can retain all
  // their tiny images without retaining full-resolution GPU snapshots.
  if(index != m_SelectedIndex)
  {
    QVector<int> candidates = m_VisibleIndices;
    bool visibleMissing = false;
    for(int i : m_VisibleIndices)
      visibleMissing |= cache->revisions[i].image.isNull() && !cache->revisions[i].unavailable;
    const uint64_t imageBytes = uint64_t((previewSize.width() * 3 + 3) & ~3) * previewSize.height();
    if(!visibleMissing && imageBytes &&
       uint64_t(m_Revisions.size()) <= MaxHistoryImageBytes / imageBytes)
      for(int i = 0; i < m_Revisions.size(); i++)
        if(!candidates.contains(i))
          candidates.push_back(i);
    for(int i : candidates)
      if(indices.size() < 24 && !indices.contains(i) && cache->revisions[i].image.isNull() &&
         !cache->revisions[i].unavailable)
        indices.push_back(i);
  }
  std::sort(indices.begin(), indices.end(),
            [this](int a, int b) { return m_Revisions[a].eventId < m_Revisions[b].eventId; });
  rdcarray<uint32_t> events;
  for(int i : indices)
    events.push_back(m_Revisions[i].eventId);

  // One low-priority job and one local output. It cannot retain a native window or an output
  // awaiting a GUI cleanup handle. Selection/scroll cancellation discards an obsolete result.
  m_Ctx.Replay().AsyncInvoke(
      historyBackgroundTag,
      [self, cache, job, indices, events, index, eventId, previewSize, generation, viewGeneration,
       resource, subresource, typeCast, isDepth](IReplayController *controller) {
        if(job->cancelled.load())
          return;
        QVector<std::shared_ptr<ThumbnailResult>> results;
        rdcarray<bytebuf> pixels;
        const bool stale = controller->GetShaderReplacementGeneration(0) != generation;
        if(!stale)
        {
          TextureDisplay display;
          display.resourceId = resource;
          display.subresource = subresource;
          display.typeCast = typeCast;
          display.scale = -1.0f;
          display.rangeMin = typeCast == CompType::SNorm ? -1.0f : 0.0f;
          display.rangeMax = 1.0f;
          if(isDepth)
            display.green = display.blue = false;
          IReplayOutput *output = controller->CreateOutput(
              CreateHeadlessWindowingData(previewSize.width(), previewSize.height()),
              ReplayOutputType::Texture);
          if(output)
          {
            // Headless history renders directly into this small output without a full-resolution
            // event snapshot. DisplayTextureAtEvent restores the live event before returning.
            if(!job->cancelled.load())
            {
              pixels = output->ReadbackTextureAtEvents(events, display,
                                                       [job]() { return job->cancelled.load(); });
            }
            output->Shutdown();
          }
        }
        const size_t completed = pixels.empty() ? 1 : pixels.size();
        for(int n = 0; n < indices.size() && size_t(n) < completed; n++)
        {
          auto result = std::make_shared<ThumbnailResult>();
          result->index = indices[n];
          result->size = previewSize;
          result->mutationGeneration = controller->GetShaderReplacementGeneration(0);
          result->stale = stale || result->mutationGeneration != generation;
          if(size_t(n) < pixels.size())
            result->pixels = std::move(pixels[n]);
          results.push_back(result);
        }
        GUIInvoke::call(qApp, [self, cache, job, results, viewGeneration]() {
          if(!self || job->cancelled.load() || self->m_Job != job || self->m_Cache != cache ||
             self->m_ViewGeneration != viewGeneration)
            return;
          self->m_Job.reset();
          self->m_ApplyingBatch = true;
          for(const auto &result : results)
            self->ApplyThumbnail(result);
          self->m_ApplyingBatch = false;
          self->ScheduleThumbnailRefresh();
        });
      });
}

void RenderTargetHistoryView::ApplyThumbnail(const std::shared_ptr<ThumbnailResult> &result)
{
  // i18n-ignore: internal profiling operation ID.
  ScopedHistoryUITiming timing(this, "applyThumbnail");
  if(!m_Cache)
    return;
  if(result->stale || result->mutationGeneration != m_Cache->mutationGeneration)
  {
    m_Cache->loadComplete = false;
    BeginValidation();
    return;
  }
  if(result->index >= 0 && result->index < m_Cache->revisions.count())
  {
    CacheEntry::CachedRevision &cached = m_Cache->revisions[result->index];
    if(IsValidRGBPayload(result->pixels.size(), result->size))
    {
      QImage borrowed(result->pixels.data(), result->size.width(), result->size.height(),
                      result->size.width() * 3, QImage::Format_RGB888);
      cached.image = borrowed.copy();
      cached.lastUsed = ++m_ImageUseSerial;
    }
    else
    {
      cached.unavailable = true;
    }
  }
  PruneThumbnails();
  RefreshThumbnailImages();
  FinishLoad(m_Cache->mutationGeneration);
  ScheduleThumbnailRefresh();
}

void RenderTargetHistoryView::FinishLoad(uint64_t mutationGeneration)
{
  Q_UNUSED(mutationGeneration);
  m_Progress->hide();
  int available = 0;
  if(m_Cache)
    for(const CacheEntry::CachedRevision &revision : m_Cache->revisions)
      available += !revision.image.isNull() ? 1 : 0;
  m_StatusText->setText(
      QCoreApplication::translate("RenderTargetHistoryView", "Cached %1 of %2 revisions.")
          .arg(available)
          .arg(m_Revisions.count()));
  UpdateSelectionText();
  UpdateDiagnostics();
}

void RenderTargetHistoryView::CancelWork(bool block)
{
  ++m_ViewGeneration;
  if(m_Job)
  {
    m_Job->cancelled.store(true);
    m_Job.reset();
  }
  // Only destruction/capture closure drains the queue. Normal tab switches and mouse navigation
  // are nonblocking; every output is owned and shut down by its worker, even after cancellation.
  if(block && m_ReplayThreadAvailable)
    m_Ctx.Replay().BlockInvoke([](IReplayController *) {});
  UpdateDiagnostics();
}

void RenderTargetHistoryView::ClearThumbnailWidgets()
{
  for(Revision &revision : m_Revisions)
    delete revision.thumbnail;
  m_Revisions.clear();
  m_VisibleIndices.clear();
  m_SelectedIndex = -1;
  m_ScrollPending = false;
  m_ThumbnailContainer->setMinimumWidth(0);
  // i18n-ignore: bounded internal history request diagnostics reset on target/capture changes.
  setProperty("historyRequestedEvents", QVariantList());
  UpdateDiagnostics();
}

void RenderTargetHistoryView::RebuildThumbnails()
{
  ClearThumbnailWidgets();
  if(!m_Cache)
    return;
  m_Revisions.reserve(m_Cache->revisions.count());
  for(int cacheIndex = 0; cacheIndex < m_Cache->revisions.count(); cacheIndex++)
  {
    const CacheEntry::CachedRevision &cached = m_Cache->revisions[cacheIndex];
    Revision revision;
    revision.cacheIndex = cacheIndex;
    revision.eventId = cached.candidate.eventId;
    revision.usage = cached.candidate.usage;
    // Metadata is cheap; QWidget/native-window construction is strictly neighborhood-only.
    revision.usageName = ToQDisplayStr(revision.usage, m_Ctx.APIProps().pipelineType);
    m_Revisions.push_back(std::move(revision));
    m_ImageUseSerial = qMax(m_ImageUseSerial, cached.lastUsed);
  }
  m_ThumbnailContainer->setMinimumWidth(8 + m_Revisions.count() * ThumbnailPitch);
  m_ThumbnailContainer->resize(m_ThumbnailContainer->minimumWidth(), 126);
  SelectNearestEvent(m_AnchorEID);
  ScheduleThumbnailRefresh();
}

void RenderTargetHistoryView::RefreshThumbnailImages()
{
  if(!m_Cache)
    return;
  for(Revision &revision : m_Revisions)
    if(revision.thumbnail && revision.cacheIndex >= 0 &&
       revision.cacheIndex < m_Cache->revisions.count())
      revision.thumbnail->SetImage(m_Cache->revisions[revision.cacheIndex].image,
                                   m_Cache->revisions[revision.cacheIndex].unavailable);
}

void RenderTargetHistoryView::ScheduleThumbnailRefresh()
{
  if(m_RefreshScheduled)
    return;
  m_RefreshScheduled = true;
  QTimer::singleShot(0, this, [this]() {
    m_RefreshScheduled = false;
    RefreshVisibleThumbnails();
  });
}

void RenderTargetHistoryView::RefreshVisibleThumbnails()
{
  // i18n-ignore: internal profiling operation ID.
  ScopedHistoryUITiming timing(this, "visibleThumbnails");
  if(!isVisible() || !m_Cache || !m_Cache->discoveryComplete || m_ScrollPending)
    return;
  QVector<QRect> bounds;
  bounds.reserve(m_Revisions.count());
  for(int i = 0; i < m_Revisions.count(); i++)
    bounds.push_back(ThumbnailRect(i));
  const QRect viewport(m_ScrollArea->horizontalScrollBar()->value(), 0,
                       m_ScrollArea->viewport()->width(), 126);
  m_VisibleIndices = SelectVirtualizedThumbnailIndices(bounds, viewport, ThumbnailPrefetchPixels,
                                                       MaxCachedThumbnails);
  for(int i = 0; i < m_Revisions.count(); i++)
  {
    Revision &revision = m_Revisions[i];
    if(!m_VisibleIndices.contains(i))
    {
      delete revision.thumbnail;
      revision.thumbnail = NULL;
      continue;
    }
    if(!revision.thumbnail)
    {
      const ActionDescription *action = m_Ctx.GetAction(revision.eventId);
      if(action)
        revision.actionName = action->GetName(m_Ctx.GetStructuredFile());
      RevisionThumbnail *thumbnail = new RevisionThumbnail(
          revision.eventId, revision.usageName, m_ThumbnailContainer);
      thumbnail->setGeometry(ThumbnailRect(i));
      // i18n-ignore: internal revision index for virtualized input regression fixtures.
      thumbnail->setProperty("historyRevisionIndex", i);
      thumbnail->setAccessibleName(
          QCoreApplication::translate("RenderTargetHistoryView", "Output revision at EID %1")
              .arg(revision.eventId));
      thumbnail->setToolTip(
          QCoreApplication::translate(
              "RenderTargetHistoryView",
              "EID %1\n%2\n%3\n\nSingle-click to preview in Texture Viewer. Double-click to "
              "jump to this EID.")
              .arg(revision.eventId).arg(revision.usageName).arg(revision.actionName));
      // A mouse selection must not scroll/recreate the object between click and double-click.
      thumbnail->clicked = [this, i]() { SelectIndex(i, false); };
      thumbnail->doubleClicked = [this, i]() {
        SelectIndex(i, false);
        JumpToSelectedEvent();
      };
      revision.thumbnail = thumbnail;
      thumbnail->show();
    }
    revision.thumbnail->SetSelected(i == m_SelectedIndex);
    revision.thumbnail->SetCurrent(revision.eventId == m_CurrentEID);
    CacheEntry::CachedRevision &cached = m_Cache->revisions[i];
    revision.thumbnail->SetImage(cached.image, cached.unavailable);
    if(!cached.image.isNull())
      cached.lastUsed = ++m_ImageUseSerial;
  }
  if(m_Job && m_Job->index >= 0 && !m_VisibleIndices.contains(m_Job->index))
    for(int index : m_VisibleIndices)
      if(m_Cache->revisions[index].image.isNull() && !m_Cache->revisions[index].unavailable)
      {
        // Cancel offscreen work only when new visible pixels need the lane. A background prefill
        // is intentionally offscreen; repeated layout/range notifications must not cancel it.
        CancelWork();
        break;
      }
  PruneThumbnails();
  UpdateDiagnostics();
  QueueNextThumbnail();
}

void RenderTargetHistoryView::QueueNextThumbnail()
{
  if(m_ApplyingBatch)
    return;
  if(m_Ctx.IsScrubbing())
  {
    CancelWork();
    ResumeAfterScrub();
    return;
  }
  if(m_Job || !m_Cache || !m_Cache->loadComplete || !isVisible() || m_ScrollPending)
    return;
  // Selected revision first; remaining candidates are already ranked visible-before-prefetch.
  QVector<int> priority = m_VisibleIndices;
  if(priority.removeOne(m_SelectedIndex))
    priority.prepend(m_SelectedIndex);
  for(int index : priority)
  {
    const CacheEntry::CachedRevision &revision = m_Cache->revisions[index];
    if(revision.image.isNull() && !revision.unavailable)
    {
      QueueThumbnail(index);
      return;
    }
  }
  const uint64_t imageBytes =
      uint64_t((m_Cache->previewSize.width() * 3 + 3) & ~3) * m_Cache->previewSize.height();
  if(!m_VisibleIndices.isEmpty() && imageBytes &&
     uint64_t(m_Revisions.size()) <= MaxHistoryImageBytes / imageBytes)
    for(int i = 0; i < m_Cache->revisions.size(); i++)
      if(m_Cache->revisions[i].image.isNull() && !m_Cache->revisions[i].unavailable)
      {
        QueueThumbnail(i);
        return;
      }
}

void RenderTargetHistoryView::PruneThumbnails()
{
  if(!m_Cache)
    return;
  QVector<uint64_t> used;
  uint64_t bytes = 0;
  for(const CacheEntry::CachedRevision &revision : m_Cache->revisions)
  {
    used.push_back(revision.image.isNull() ? 0 : revision.lastUsed);
    bytes += uint64_t(revision.image.bytesPerLine()) * revision.image.height();
  }
  auto &registry = CacheRegistry();
  for(const auto &entry : registry)
    if(entry != m_Cache)
      for(const auto &revision : entry->revisions)
        bytes += uint64_t(revision.image.bytesPerLine()) * revision.image.height();
  // Reclaim images belonging to older targets first, retaining their cheap discovery metadata.
  for(const auto &entry : registry)
    if(bytes > MaxHistoryImageBytes && entry != m_Cache)
      for(auto &revision : entry->revisions)
      {
        bytes -= uint64_t(revision.image.bytesPerLine()) * revision.image.height();
        revision.image = QImage();
        revision.lastUsed = 0;
      }
  while(bytes > MaxHistoryImageBytes)
  {
    const int oldest = OldestThumbnail(used, m_VisibleIndices);
    if(oldest < 0)
      break;
    const QImage &image = m_Cache->revisions[oldest].image;
    bytes -= uint64_t(image.bytesPerLine()) * image.height();
    m_Cache->revisions[oldest].image = QImage();
    m_Cache->revisions[oldest].lastUsed = 0;
    used[oldest] = 0;
  }
}

void RenderTargetHistoryView::UpdateDiagnostics()
{
  int cached = 0, unavailable = 0, widgets = 0;
  uint64_t imageBytes = 0;
  for(const auto &entry : CacheRegistry())
    for(const auto &revision : entry->revisions)
      imageBytes += uint64_t(revision.image.bytesPerLine()) * revision.image.height();
  if(m_Cache)
    for(const CacheEntry::CachedRevision &revision : m_Cache->revisions)
    {
      cached += !revision.image.isNull() ? 1 : 0;
      unavailable += revision.unavailable ? 1 : 0;
    }
  for(const Revision &revision : m_Revisions)
    widgets += revision.thumbnail != NULL ? 1 : 0;
  // i18n-ignore: read-only Qt properties for native mouse, cache-bound and scheduling regression tests.
  setProperty("historyRevisionCount", m_Revisions.count());
  setProperty("historyCachedCount", cached);
  // i18n-ignore: unavailable preview count for completion/error diagnostics.
  setProperty("historyUnavailableCount", unavailable);
  // i18n-ignore: byte-budget diagnostics for bounded, process-local preview images.
  setProperty("historyImageBytes", qulonglong(imageBytes));
  setProperty("historyImageBudgetBytes", qulonglong(MaxHistoryImageBytes));
  setProperty("historyLiveWidgets", widgets);
  setProperty("historySelectedEvent", SelectedEvent());
  setProperty("historySelectedIndex", m_SelectedIndex);
  setProperty("historyRequestSerial", qulonglong(m_RequestSerial));
  // i18n-ignore: scheduler diagnostics; these are not user-visible labels.
  setProperty("historyWorkerActive", bool(m_Job));
  setProperty("historyScrollPending", m_ScrollPending);
  setProperty("historyRefreshScheduled", m_RefreshScheduled);
  setProperty("historyApplyingBatch", m_ApplyingBatch);
  setProperty("historyVisible", isVisible());
  setProperty("historyScrubbing", m_Ctx.IsScrubbing());
  // i18n-ignore: read-only generation/validation evidence, not a promise of current GPU state.
  setProperty("historyValidationPending",
              m_Cache && m_Cache->discoveryComplete && m_Job && m_Job->index < 0);
  // i18n-ignore: last successfully validated cache generation; absent before validation.
  setProperty("historyCacheMutationGeneration",
              m_Cache && m_Cache->loadComplete ? QVariant(qulonglong(m_Cache->mutationGeneration))
                                               : QVariant());
  setProperty("historyPendingEvent", m_Job && m_Job->index >= 0 ?
      m_Revisions[m_Job->index].eventId : 0U);
}

void RenderTargetHistoryView::resizeEvent(QResizeEvent *event)
{
  QFrame::resizeEvent(event);
  ScheduleThumbnailRefresh();
}

void RenderTargetHistoryView::SelectNearestEvent(uint32_t eventId)
{
  m_AnchorEID = eventId;
  QVector<uint32_t> events;
  events.reserve(m_Revisions.count());
  for(const Revision &revision : m_Revisions)
    events.push_back(revision.eventId);
  SelectIndex(NearestRevisionIndex(events, eventId));
}

void RenderTargetHistoryView::SelectEvent(uint32_t eventId)
{
  for(int i = 0; i < m_Revisions.count(); i++)
  {
    if(m_Revisions[i].eventId == eventId)
    {
      SelectIndex(i);
      return;
    }
  }
}

void RenderTargetHistoryView::SelectIndex(int index, bool ensureVisible)
{
  if(index < 0 || index >= m_Revisions.count())
  {
    if(m_Revisions.isEmpty())
      m_SelectedIndex = -1;
    UpdateSelectionText();
    return;
  }

  const bool changed = m_SelectedIndex != index;
  if(m_SelectedIndex >= 0 && m_SelectedIndex < m_Revisions.count() &&
     m_Revisions[m_SelectedIndex].thumbnail)
    m_Revisions[m_SelectedIndex].thumbnail->SetSelected(false);
  m_SelectedIndex = index;
  if(m_Revisions[index].thumbnail)
    m_Revisions[index].thumbnail->SetSelected(true);
  if(!ensureVisible)
    m_ScrollPending = false;
  if(ensureVisible)
  {
    m_ScrollPending = true;
    const std::shared_ptr<CacheEntry> cache = m_Cache;
    // Give QScrollArea one GUI turn to update its range before deriving a neighborhood. This
    // prevents an initial index-zero batch when the requested anchor is near the end of a frame.
    QTimer::singleShot(0, this, [this, cache, index]() {
      if(m_Cache != cache || m_SelectedIndex != index)
        return;
      m_ScrollArea->ensureVisible(ThumbnailRect(index).center().x(), 60, 100, 8);
      m_ScrollPending = false;
      ScheduleThumbnailRefresh();
    });
  }
  if(changed && m_Job && m_Job->index >= 0)
    CancelWork();
  UpdateSelectionText();
  UpdateDiagnostics();
  // Full-size selected output uses the existing foreground Texture Viewer lane, before the next
  // background thumbnail is enqueued. The remaining history never loads full-size images.
  if(changed)
    m_TextureViewer.RenderTargetHistorySelectionChanged(SelectedEvent());
  ScheduleThumbnailRefresh();
}

bool RenderTargetHistoryView::StepSelection(int delta)
{
  if(m_Revisions.isEmpty())
    return false;

  const int current = m_SelectedIndex < 0 ? 0 : m_SelectedIndex;
  SelectIndex(qBound(0, current + delta, m_Revisions.count() - 1));
  return true;
}

uint32_t RenderTargetHistoryView::SelectedEvent() const
{
  if(m_SelectedIndex < 0 || m_SelectedIndex >= m_Revisions.count())
    return 0;
  return m_Revisions[m_SelectedIndex].eventId;
}

void RenderTargetHistoryView::JumpToSelectedEvent()
{
  const uint32_t eventId = SelectedEvent();
  if(eventId == 0)
    return;

  m_CurrentEID = eventId;
  UpdateCurrentEventMarkers();
  m_Ctx.SetEventID({this}, eventId, eventId);
}

void RenderTargetHistoryView::UpdateCurrentEventMarkers()
{
  for(Revision &revision : m_Revisions)
    if(revision.thumbnail)
      revision.thumbnail->SetCurrent(revision.eventId == m_CurrentEID);
}

void RenderTargetHistoryView::UpdateSelectionText()
{
  const bool hasSelection = m_SelectedIndex >= 0 && m_SelectedIndex < m_Revisions.count();
  m_Previous->setEnabled(hasSelection && m_SelectedIndex > 0);
  m_Next->setEnabled(hasSelection && m_SelectedIndex + 1 < m_Revisions.count());

  if(!hasSelection)
  {
    m_SelectionText->setText(QCoreApplication::translate(
        "RenderTargetHistoryView", "Select a revision to inspect its full texture output."));
    return;
  }

  const Revision &revision = m_Revisions[m_SelectedIndex];
  m_SelectionText->setText(
      QCoreApplication::translate("RenderTargetHistoryView", "EID %1 | Revision %2 of %3 | %4")
          .arg(revision.eventId)
          .arg(m_SelectedIndex + 1)
          .arg(m_Revisions.count())
          .arg(revision.usageName));
}

void RenderTargetHistoryView::UpdateHeader()
{
  m_ResourceLabel->setText(m_Ctx.GetResourceName(m_Resource));

  if(m_Subresource.sample == TextureDisplay::ResolveSamples)
    m_SubresourceLabel->setText(
        QCoreApplication::translate("RenderTargetHistoryView", "Mip %1 | Slice %2 | Resolved")
            .arg(m_Subresource.mip)
            .arg(m_Subresource.slice));
  else
    m_SubresourceLabel->setText(
        QCoreApplication::translate("RenderTargetHistoryView", "Mip %1 | Slice %2 | Sample %3")
            .arg(m_Subresource.mip)
            .arg(m_Subresource.slice)
            .arg(m_Subresource.sample));
}

#if ENABLE_UNIT_TESTS

#include "3rdparty/catch/catch.hpp"

TEST_CASE("Render target history filters and deduplicates write candidates", "[rthistory]")
{
  rdcarray<EventUsage> usage = {
      {9, ResourceUsage::PS_Resource},       {7, ResourceUsage::ColorTarget},
      {7, ResourceUsage::Clear},             {3, ResourceUsage::CopyDst},
      {0, ResourceUsage::ColorTarget},       {11, ResourceUsage::PS_RWResource},
  };

  QVector<HistoryCandidate> candidates = BuildHistoryCandidates(usage, Subresource());
  REQUIRE(candidates.count() == 3);
  CHECK(candidates[0].eventId == 3);
  CHECK(candidates[1].eventId == 7);
  CHECK(bool(candidates[1].usage == ResourceUsage::Clear));
  CHECK(candidates[2].eventId == 11);
}

TEST_CASE("Render target history selects the nearest preceding revision", "[rthistory]")
{
  QVector<uint32_t> events = {4, 10, 18};
  CHECK(NearestRevisionIndex(events, 1) == 0);
  CHECK(NearestRevisionIndex(events, 4) == 0);
  CHECK(NearestRevisionIndex(events, 17) == 1);
  CHECK(NearestRevisionIndex(events, 99) == 2);
  CHECK(NearestRevisionIndex({}, 99) == -1);
}

TEST_CASE("Render target history keeps resource-wide candidates conservatively", "[rthistory]")
{
  CHECK(CandidateMayAffectSubresource({5, ResourceUsage::Clear}, Subresource(4, 7, 0)));
  CHECK(CandidateMayAffectSubresource({6, ResourceUsage::ColorTarget}, Subresource(3, 9, 0)));
  CHECK_FALSE(CandidateMayAffectSubresource({7, ResourceUsage::GenMips}, Subresource(0, 2, 0)));
  CHECK(CandidateMayAffectSubresource({7, ResourceUsage::GenMips}, Subresource(1, 2, 0)));
}

TEST_CASE("Render target history cache keys include every display subresource field", "[rthistory]")
{
  // i18n-ignore: internal cache-key unit-test fixture identifiers, never shown to the user.
  const QString base = HistoryTargetKey(lit("capture"), lit("resource7"), Subresource(1, 2, 3),
                                        CompType::Float);
  // i18n-ignore: internal cache-key unit-test fixture identifiers, never shown to the user.
  CHECK(bool(base == HistoryTargetKey(lit("capture"), lit("resource7"), Subresource(1, 2, 3),
                                      CompType::Float)));
  // i18n-ignore: internal cache-key unit-test fixture identifiers, never shown to the user.
  CHECK(bool(base != HistoryTargetKey(lit("capture2"), lit("resource7"), Subresource(1, 2, 3),
                                      CompType::Float)));
  // i18n-ignore: internal cache-key unit-test fixture identifiers, never shown to the user.
  CHECK(bool(base != HistoryTargetKey(lit("capture"), lit("resource8"), Subresource(1, 2, 3),
                                      CompType::Float)));
  // i18n-ignore: internal cache-key unit-test fixture identifiers, never shown to the user.
  CHECK(bool(base != HistoryTargetKey(lit("capture"), lit("resource7"), Subresource(0, 2, 3),
                                      CompType::Float)));
  // i18n-ignore: internal cache-key unit-test fixture identifiers, never shown to the user.
  CHECK(bool(base != HistoryTargetKey(lit("capture"), lit("resource7"), Subresource(1, 3, 3),
                                      CompType::Float)));
  // i18n-ignore: internal cache-key unit-test fixture identifiers, never shown to the user.
  CHECK(bool(base != HistoryTargetKey(lit("capture"), lit("resource7"), Subresource(1, 2, 4),
                                      CompType::Float)));
  // i18n-ignore: internal cache-key unit-test fixture identifiers, never shown to the user.
  CHECK(bool(base != HistoryTargetKey(lit("capture"), lit("resource7"), Subresource(1, 2, 3),
                                      CompType::UInt)));
}

TEST_CASE("Render target history validates RGB thumbnail payloads without overflow", "[rthistory]")
{
  CHECK(IsValidRGBPayload(12, QSize(2, 2)));
  CHECK_FALSE(IsValidRGBPayload(11, QSize(2, 2)));
  CHECK_FALSE(IsValidRGBPayload(13, QSize(2, 2)));
  CHECK_FALSE(IsValidRGBPayload(0, QSize(0, 2)));
  CHECK_FALSE(IsValidRGBPayload(0, QSize(2, 0)));
}

TEST_CASE("Render target history maps wheel input to horizontal scrolling", "[rthistory]")
{
  int value = 50;
  CHECK(HorizontalWheelScrollValue(50, 0, 200, QPoint(), QPoint(0, -120), 10, 3, value));
  CHECK(value == 80);

  CHECK(HorizontalWheelScrollValue(50, 0, 200, QPoint(), QPoint(120, 0), 10, 3, value));
  CHECK(value == 20);

  CHECK(HorizontalWheelScrollValue(50, 0, 200, QPoint(3, -9), QPoint(0, 120), 10, 3, value));
  CHECK(value == 59);

  value = 77;
  CHECK_FALSE(HorizontalWheelScrollValue(77, 0, 200, QPoint(), QPoint(), 10, 3, value));
  CHECK(value == 77);

  value = 200;
  CHECK_FALSE(HorizontalWheelScrollValue(200, 0, 200, QPoint(0, -8), QPoint(), 10, 3, value));
  CHECK(value == 200);
}

TEST_CASE("Render target history bounds thumbnail visibility and prefetch", "[rthistory]")
{
  QVector<QRect> items;
  for(int i = 0; i < 12; i++)
    items.push_back(QRect(i * 174, 0, 168, 118));

  const QRect viewport(2 * 174, 0, 2 * 174, 118);
  QVector<int> selected = SelectVirtualizedThumbnailIndices(items, viewport, 174, 3);
  REQUIRE(selected.count() == 3);
  CHECK(selected.contains(2));
  CHECK(selected.contains(3));
  CHECK_FALSE(selected.contains(0));
  CHECK_FALSE(selected.contains(5));

  selected = SelectVirtualizedThumbnailIndices(items, viewport, 10000, 2);
  CHECK(selected.count() == 2);
  CHECK(selected.contains(2));
  CHECK(selected.contains(3));

  CHECK(SelectVirtualizedThumbnailIndices(items, QRect(), 174, 3).isEmpty());
  CHECK(SelectVirtualizedThumbnailIndices(items, viewport, 174, 0).isEmpty());
}

TEST_CASE("Render target history presentation belongs only to explicitly opened tabs",
          "[rthistory][tabs]")
{
  using namespace RenderTargetHistoryPolicy;

  const TabBinding linked = {true, 17};
  TabPresentation presentation = ResolveTabPresentation(true, &linked);
  CHECK_FALSE(presentation.visible);
  CHECK(presentation.activeEvent == 0);

  const TabBinding ordinaryLocked;
  presentation = ResolveTabPresentation(false, &ordinaryLocked);
  CHECK_FALSE(presentation.visible);
  CHECK(presentation.activeEvent == 0);

  presentation = ResolveTabPresentation(false, NULL);
  CHECK_FALSE(presentation.visible);
  CHECK(presentation.activeEvent == 0);
}

TEST_CASE("Render target history restores independent tab selections and clears closed bindings",
          "[rthistory][tabs]")
{
  using namespace RenderTargetHistoryPolicy;

  TabBinding targetA = {true, 17};
  TabBinding targetB = {true, 42};

  TabPresentation presentation = ResolveTabPresentation(false, &targetA);
  REQUIRE(presentation.visible);
  CHECK(presentation.activeEvent == 17);

  presentation = ResolveTabPresentation(false, &targetB);
  REQUIRE(presentation.visible);
  CHECK(presentation.activeEvent == 42);

  targetB.selectedEvent = 43;
  presentation = ResolveTabPresentation(false, &targetA);
  REQUIRE(presentation.visible);
  CHECK(presentation.activeEvent == 17);

  ClearTabBinding(targetA);
  presentation = ResolveTabPresentation(false, &targetA);
  CHECK_FALSE(presentation.visible);
  CHECK(presentation.activeEvent == 0);

  // Closing a tab removes its presentation binding, not the process-lifetime revision cache.
  CHECK(bool(ResolveStaticRevisionCacheAction(true, StaticRevisionChange::DisplayUnchanged) ==
             StaticRevisionCacheAction::Reuse));
}

TEST_CASE("Render target history recaptures static revisions only when missing or shader-dirty",
          "[rthistory][cache]")
{
  using namespace RenderTargetHistoryPolicy;

  CHECK(bool(ResolveStaticRevisionCacheAction(false, StaticRevisionChange::DisplayUnchanged) ==
             StaticRevisionCacheAction::Capture));

  const StaticRevisionChange displayOnlyChanges[] = {
      StaticRevisionChange::DisplayUnchanged, StaticRevisionChange::Zoom,
      StaticRevisionChange::Pan,              StaticRevisionChange::Channels,
      StaticRevisionChange::Range,            StaticRevisionChange::Background,
  };
  for(StaticRevisionChange change : displayOnlyChanges)
  {
    INFO("Display-only change " << int(change));
    CHECK(bool(ResolveStaticRevisionCacheAction(true, change) ==
               StaticRevisionCacheAction::Reuse));
  }

  CHECK(bool(ResolveStaticRevisionCacheAction(true, StaticRevisionChange::ShaderReplacement) ==
             StaticRevisionCacheAction::Capture));
  CHECK(bool(ResolveStaticRevisionCacheAction(false, StaticRevisionChange::ShaderReplacement) ==
             StaticRevisionCacheAction::Capture));

  // Once the dirty revision has been captured, subsequent UI changes reuse it again.
  CHECK(bool(ResolveStaticRevisionCacheAction(true, StaticRevisionChange::DisplayUnchanged) ==
             StaticRevisionCacheAction::Reuse));
}

TEST_CASE("Long render target history starts near its anchor with bounded cards", "[rthistory]")
{
  QVector<QRect> items;
  for(int i = 0; i < 1504; i++)
    items.push_back(ThumbnailRect(i));
  const QRect viewport(1400 * ThumbnailPitch, 0, 5 * ThumbnailPitch, 126);
  const QVector<int> selected = SelectVirtualizedThumbnailIndices(
      items, viewport, ThumbnailPrefetchPixels, MaxCachedThumbnails);
  REQUIRE(!selected.isEmpty());
  CHECK(selected.count() <= MaxCachedThumbnails);
  CHECK(selected.contains(1402));
  CHECK_FALSE(selected.contains(0));
  for(int index : selected)
    CHECK(index >= 1398);
}

TEST_CASE("History thumbnail resolution is independent of candidate count", "[rthistory]")
{
  TextureDescription texture;
  texture.width = 1920;
  texture.height = 1080;
  const QSize single = PreviewSize(texture, Subresource(), 1);
  const QSize many = PreviewSize(texture, Subresource(), 1504);
  CHECK(single.width() == 256);
  CHECK(single.height() == 144);
  CHECK(bool(single == many));
  CHECK(uint64_t(MaxTargetCaches) * MaxCachedThumbnails * 256 * 144 * 3 < 32ULL * 1024 * 1024);
}

TEST_CASE("History LRU evicts old images without invalidating visible cards", "[rthistory]")
{
  CHECK(OldestThumbnail({0, 1, 8, 4}, {1, 2}) == 3);
  CHECK(OldestThumbnail({0, 1, 8, 4}, {2}) == 1);
  CHECK(OldestThumbnail({0, 1, 8, 4}, {1, 2, 3}) == -1);
  CHECK(OldestThumbnail({}, {}) == -1);
}

#endif
