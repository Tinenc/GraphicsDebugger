/******************************************************************************
 * The MIT License (MIT)
 *
 * Copyright (c) 2015-2026 Baldur Karlsson
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

#include <QDir>
#include <QFrame>
#include <QImage>
#include <QJsonObject>
#include <QMenu>
#include <QMouseEvent>
#include <QPointer>
#include <QTime>
#include <atomic>
#include <functional>
#include <map>
#include <tuple>
#include <vector>
#include "Code/Interface/QRDInterface.h"

namespace Ui
{
class TextureViewer;
}

class RDTreeWidgetItem;
class ResourcePreview;
class ThumbnailStrip;
class TextureGoto;
class QFileSystemWatcher;
class TextureViewer;
class QSplitter;
class RenderTargetHistoryView;
class QStackedWidget;
class AITextureAnnotation;
class QTimer;

struct Following
{
  FollowType Type;
  ShaderStage Stage;
  int index;
  uint32_t arrayEl;

  // this is only for QVariant compatibility and will generate an invalid Following instance! do not
  // use!
  Following();

  Following(const TextureViewer &tex, FollowType type, ShaderStage stage, uint32_t index,
            uint32_t arrayElement);
  Following(const Following &other);
  Following &operator=(const Following &other);

  bool operator==(const Following &o) const;
  bool operator!=(const Following &o) const;
  static void GetActionContext(ICaptureContext &ctx, bool &copy, bool &clear, bool &compute);

  int GetHighestMip(ICaptureContext &ctx);
  int GetFirstArraySlice(ICaptureContext &ctx);
  CompType GetTypeHint(ICaptureContext &ctx);

  ResourceId GetResourceId(ICaptureContext &ctx);
  Descriptor GetDescriptor(ICaptureContext &ctx, uint32_t arrayIdx);

  static rdcarray<Descriptor> GetOutputTargets(ICaptureContext &ctx);

  static Descriptor GetDepthTarget(ICaptureContext &ctx);
  static Descriptor GetDepthResolveTarget(ICaptureContext &ctx);
  static rdcarray<UsedDescriptor> GetReadWriteResources(ICaptureContext &ctx, ShaderStage stage,
                                                        bool onlyUsed);
  static rdcarray<UsedDescriptor> GetReadOnlyResources(ICaptureContext &ctx, ShaderStage stage,
                                                       bool onlyUsed);

  const ShaderReflection *GetReflection(ICaptureContext &ctx);
  static const ShaderReflection *GetReflection(ICaptureContext &ctx, ShaderStage stage);

private:
  const TextureViewer &tex;
};

struct TexSettings
{
  TexSettings()
  {
    displayType = 0;
    r = g = b = true;
    a = false;
    flip_y = false;
    depth = true;
    stencil = false;
    mip = 0;
    slice = 0;
    minrange = 0.0f;
    maxrange = 1.0f;
    typeCast = CompType::Typeless;
  }

  int displayType;    // RGBA, RGBM, YUV Decode, Custom
  QString customShader;
  bool r, g, b, a;
  bool flip_y;
  bool depth, stencil;
  int mip, slice;
  float minrange, maxrange;
  CompType typeCast;
};

class TextureViewer : public QFrame, public ITextureViewer, public ICaptureViewer
{
private:
  Q_OBJECT

  Q_PROPERTY(QVariant persistData READ persistData WRITE setPersistData DESIGNABLE false SCRIPTABLE false)

  // Texture List
  enum class FilterType
  {
    None,
    Textures,
    RenderTargets,
    String
  };

public:
  // Internal GUI-thread completion: includes a replacement display queued by a late resize.
  void WhenPresentationReady(uint32_t eventId, std::function<void()> completed);
  explicit TextureViewer(ICaptureContext &ctx, QWidget *parent = 0);
  ~TextureViewer();

  // ITextureViewer
  QWidget *Widget() override { return this; }
  void ViewTexture(ResourceId ID, CompType typeCast, bool focus) override;
  void ViewFollowedResource(FollowType followType, ShaderStage stage, int32_t index,
                            int32_t arrayElement) override;
  bool FollowBoundTexture(ResourceId id, const QString &attachmentKind);
  uint64_t BeginTemporaryOutputFollow(uint32_t index) override;
  bool UpdateTemporaryOutputFollow(uint64_t token, uint32_t index) override;
  bool ShowTemporaryTexturePreview(uint64_t token, const TextureDescription &texture) override;
  bool EndTemporaryOutputFollow(uint64_t token) override;
  ResourceId GetCurrentResource() override;

  Subresource GetSelectedSubresource() override;
  void SetSelectedSubresource(Subresource sub) override;
  rdcpair<int32_t, int32_t> GetPickedLocation() override;
  void GotoLocation(uint32_t x, uint32_t y) override;
  DebugOverlay GetTextureOverlay() override;
  void SetTextureOverlay(DebugOverlay overlay) override;

  TextureDisplay GetTextureDisplay() override { return m_TexDisplay; }
  bool IsZoomAutoFit() override;
  float GetZoomLevel() override;
  void SetZoomLevel(bool autofit, float zoom) override;

  rdcpair<float, float> GetHistogramRange() override;
  void SetHistogramRange(float blackpoint, float whitepoint) override;

  uint32_t GetChannelVisibilityBits() override;
  rdcfixedarray<bool, 4> GetChannelVisibility() override;
  void SetChannelVisibility(bool red, bool green, bool blue, bool alpha) override;
  void SetFlipY(bool flipped) override;

  RenderTargetHistoryView *OpenRenderTargetHistory(ResourceId resource,
                                                   const Subresource &subresource,
                                                   CompType typeCast, uint32_t anchorEID);
  void CloseRenderTargetHistory();
  void RenderTargetHistorySelectionChanged(uint32_t eventId);
  void BeginAIAnnotation(const QJsonObject &captureBinding,
                         std::function<bool(const QImage &, const QImage &, const QJsonObject &)> accept,
                         std::function<void(const QString &)> error);
  bool MatchesAIAnnotation(const QJsonObject &context) const;
  void EditAIAnnotation(const QImage &original, const QString &title, const QJsonObject &context,
                        std::function<bool(const QImage &, const QImage &, const QJsonObject &)> accept);

  // ICaptureViewer
  void OnCaptureLoaded() override;
  void OnCaptureClosed() override;
  void OnSelectedEventChanged(uint32_t eventId) override {}
  void OnEventChanged(uint32_t eventId) override;
  void OnScrubEvent(uint32_t eventId) override;

  QVariant persistData();
  void setPersistData(const QVariant &persistData);

private slots:
  // automatic slots
  void on_renderHScroll_valueChanged(int position);
  void on_renderVScroll_valueChanged(int position);

  void on_fitToWindow_toggled(bool checked);
  void on_zoomExactSize_clicked();
  void on_zoomOption_currentIndexChanged(int index);

  void on_mipLevel_currentIndexChanged(int index);
  void on_sliceFace_currentIndexChanged(int index);
  void on_overlay_currentIndexChanged(int index);

  void on_zoomRange_clicked();
  void on_autoFit_clicked();
  void on_autoFit_mouseClicked(QMouseEvent *e);
  void on_reset01_clicked();
  void on_visualiseRange_clicked();
  void on_backcolorPick_clicked();
  void on_checkerBack_clicked();

  void on_locationGoto_clicked();
  void on_viewTexBuffer_clicked();
  void on_resourceDetails_clicked();
  void on_texListShow_clicked();
  void on_saveTex_clicked();
  void on_debugPixelContext_clicked();
  void on_pixelHistory_clicked();

  void on_customCreate_clicked();
  void on_customEdit_clicked();
  void on_customDelete_clicked();

  void on_cancelTextureListFilter_clicked();
  void on_textureListFilter_editTextChanged(const QString &text);
  void on_textureListFilter_currentIndexChanged(int index);
  void on_colSelect_clicked();
  void texture_itemActivated(RDTreeWidgetItem *item, int column);

  // manual slots
  void render_mouseClick(QMouseEvent *e);
  void render_mouseMove(QMouseEvent *e);
  void render_mouseWheel(QWheelEvent *e);
  void render_resize(QResizeEvent *e);
  void render_keyPress(QKeyEvent *e);

  void textureTab_Menu(const QPoint &pos);
  void textureTab_Changed(int index);
  void textureTab_Closing(int index);

  void thumb_clicked(QMouseEvent *);
  void thumb_doubleClicked(QMouseEvent *);
  void thumb_resourceLinkActivated();
  void texContextItem_triggered();

  void zoomOption_returnPressed();

  void range_rangeUpdated();
  void rangePoint_textChanged(QString text);
  void rangePoint_leave();
  void rangePoint_keyPress(QKeyEvent *e);

  void customShaderModified(const QString &path);

  void channelsWidget_mouseClicked(QMouseEvent *event);
  void channelsWidget_toggled(bool checked) { UI_UpdateChannels(); }
  void channelsWidget_selected(int index) { UI_UpdateChannels(); }
protected:
  void enterEvent(QEvent *event) override;
  void showEvent(QShowEvent *event) override;

private:
  QStackedWidget *m_AIAnnotationStack = NULL;
  QPointer<AITextureAnnotation> m_AIAnnotation;
  uint64_t m_AIAnnotationSerial = 0;
  void CloseAIAnnotation();
  struct ViewRequest
  {
    struct ThumbnailBinding
    {
      WindowingData window;
      QPointer<ResourcePreview> preview;
      QSize size;
      ResourceId resource;
      Subresource subresource;
      CompType typeCast = CompType::Typeless;
      bool depth = false;
    };
    rdcarray<ThumbnailBinding> thumbnails;
    TextureDisplay display;
    TextureDescription texture;
    QPoint pickedPoint, hoverPixel;
    uint32_t eventId = 0, historyEvent = 0;
    uint64_t serial = 0, eventSerial = 0;
    float rangeMin = 0.0f, rangeMax = 1.0f;
    bool visualise = false;
    bool clearBeforeReveal = false;
  };
  ViewRequest CurrentViewRequest() const;
  bool IsCurrentViewRequest(const ViewRequest &request) const;
  void RT_FetchCurrentPixel(IReplayController *r, const ViewRequest &request,
                            const TextureDisplay &display, uint32_t x, uint32_t y,
                            PixelValue &pickValue, PixelValue &realValue);
  void RT_PickPixelsAndUpdate(IReplayController *, const ViewRequest &request, bool hover = false);
  void RT_UpdateAndDisplay(IReplayController *, const TextureDisplay &display, uint32_t historyEvent);
  void RT_UpdateVisualRange(IReplayController *, const ViewRequest &request);
  void SuspendTexturePresentation();
  Q_SLOT void CapturePresentationWitness();
  Q_SLOT void CaptureThumbnailWitnesses();
  void QueueUpdateAndDisplay(bool updateAnalysis = false);
  void RT_ExecuteDisplayRequest(IReplayController *r, const ViewRequest &request,
                                bool updateAnalysis);
  void CaptureNativeThumbnailBindings(ViewRequest &request);
  void QueueNativeThumbnailRefresh();
  void QueueDeferredAnalysis();
public:
  void CancelReplayBackgroundWork();
private:
  QTimer *m_AnalysisTimer = NULL;
  bool m_AnalysisPending = false;
  // Replay-thread owned outputs, reused by widget identity until capture close.
  std::map<ResourcePreview *, IReplayOutput *> m_NativeThumbnailOutputs;
  void RT_UpdateNativeThumbnails(IReplayController *r, const ViewRequest &request);
  void QueuePickPixelsAndUpdate(bool hover = false);
  void QueueUpdateVisualRange();
  uint32_t ActiveRenderTargetHistoryEvent() const;
  void UpdateRenderTargetHistorySubresource();

  void UI_UpdateStatusText();
  void UI_UpdateTextureDetails();
  void UI_OnTextureSelectionChanged(bool newAction);

  void UI_SetHistogramRange(const TextureDescription *tex, CompType typeCast);

  void UI_UpdateChannels();

  void HighlightUsage();

  void SelectPreview(ResourcePreview *prev);

  void ShowPixelHistory(bool failedDebug);

  void SetupTextureTabs();
  void RemoveTextureTabs(int firstIndex);

  void Reset();

  void refreshTextureList();
  void refreshTextureList(FilterType filterType, const QString &filterStr);

  ResourcePreview *UI_CreateThumbnail(ThumbnailStrip *strip);
  void UI_CreateThumbnails();
  void InitResourcePreview(ResourcePreview *prev, Descriptor res, bool force, Following &follow,
                           const QString &bindName, const QString &slotName);

  void InitStageResourcePreviews(ShaderStage stage, const rdcarray<ShaderResource> &shaderInterface,
                                 const rdcarray<UsedDescriptor> &descriptors, ThumbnailStrip *prevs,
                                 int &prevIndex, bool copy, bool rw);

  void UI_PreviewResized(ResourcePreview *prev);

  void AddResourceUsageEntry(QMenu &menu, uint32_t start, uint32_t end, ResourceUsage usage);
  void OpenResourceContextMenu(ResourceId id, bool input, const rdcarray<EventUsage> &usage);

  void AutoFitRange();
  void rangePoint_Update();

  void updateBackgroundColors();

  bool currentTextureIsLocked() { return m_LockedId != ResourceId(); }
  QWidget *CurrentTextureTab() const;
  bool HasRenderTargetHistoryBinding(QWidget *tab) const;
  void ActivateRenderTargetHistoryForTab(QWidget *tab, uint32_t fallbackEvent);
  void CloseTextureTab(QWidget *tab);
  void setFitToWindow(bool checked);

  void setCurrentZoomValue(float zoom);

  bool ScrollUpdateScrollbars = true;

  float CurMaxScrollX();
  float CurMaxScrollY();

  float GetFitScale();

  int realRenderWidth() const;
  int realRenderHeight() const;

  QPoint getScrollPosition();
  void setScrollPosition(const QPoint &pos);

  TextureDescription *GetCurrentTexture();
  void UI_UpdateCachedTexture();
  void UI_ResolveOutputFollowing();
  // Capture-local attachment identities, not event IDs or shader names. A pass can contain many
  // draws and shaders while presenting the same output choices.
  using OutputBindingKey =
      std::vector<std::tuple<FollowType, ShaderStage, int, uint32_t, ResourceId, int, int>>;
  OutputBindingKey UI_OutputBindingKey();
  void UI_RememberOutputFollowing();

  void ShowGotoPopup();

  bool ShouldFlipForGL();
  uint32_t MipCoordFromBase(int coord, const uint32_t dim);
  uint32_t BaseCoordFromMip(int coord, const uint32_t dim);

  void UI_UpdateFittedScale();
  void UI_SetScale(float s);
  void UI_SetScale(float s, int x, int y);
  void UI_CalcScrollbars();

  QPoint m_DragStartScroll;
  QPoint m_DragStartPos;

  QPoint m_CurHoverPixel;
  QPoint m_PickedPoint;

  QSizeF m_PrevSize;

  PixelValue m_CurRealValue = {};
  PixelValue m_CurPixelValue = {};
  PixelValue m_CurHoverValue = {};

  QColor backCol;

  int m_HighWaterStatusLength = 0;
  int m_PrevFirstArraySlice = -1;
  int m_PrevHighestMip = -1;

  bool m_Visualise = false;
  bool m_NoRangePaint = false;
  bool m_RangePoint_Dirty = false;

  ResourceId m_LockedId;
  QMap<ResourceId, QWidget *> m_LockedTabs;
  int m_ResourceCacheID = -1;

  TextureGoto *m_Goto;

  Ui::TextureViewer *ui;
  ICaptureContext &m_Ctx;
  IReplayOutput *m_Output = NULL;
  QSplitter *m_MainHistorySplitter = NULL;
  RenderTargetHistoryView *m_RenderTargetHistory = NULL;
  std::atomic<uint32_t> m_RenderTargetHistoryEvent = {0};
  // Invalidate immutable presentation and analysis requests when their UI state is superseded.
  std::atomic<uint64_t> m_DisplayRequestSerial = {0};
  std::atomic<uint64_t> m_EventRequestSerial = {0};
  bool m_PresentationSuspended = false;
  bool m_PresentationPending = false;
  uint64_t m_CompletedDisplayRequestSerial = 0;
  std::vector<std::function<void()>> m_PresentationWaiters;
  void CompletePresentationWaiters();
  bool m_UpdatingTextureState = false;
  bool m_RetargetingRenderTargetHistory = false;

  TextureSave m_SaveConfig;

  bool m_NeedCustomReload = false;

  TextureDescription *m_CachedTexture;
  Following m_Following;
  uint64_t m_TemporaryOutputFollowToken = 0;
  uint64_t m_NextTemporaryOutputFollowToken = 0;
  bool m_UpdatingTemporaryOutputFollow = false;
  Following m_BeforeTemporaryOutputFollow;
  QPointer<QWidget> m_BeforeTemporaryOutputTab;
  ResourceId m_BeforeTemporaryOutputResource;
  CompType m_BeforeTemporaryOutputType = CompType::Typeless;
  Subresource m_BeforeTemporaryOutputSubresource;
  TextureDescription m_TemporaryPreviewTexture;
  bool m_TemporaryPreviewActive = false;
  uint32_t m_TemporaryPreviewEvent = 0;
  std::map<OutputBindingKey, std::pair<Following, ResourceId>> m_OutputPreferences;
  QMap<ResourceId, TexSettings> m_TextureSettings;

  friend struct Following;

  rdcarray<UsedDescriptor> m_ReadOnlyResources[NumShaderStages];
  rdcarray<UsedDescriptor> m_ReadWriteResources[NumShaderStages];

  struct DescriptorThumbUpdate
  {
    DescriptorAccess access;
    QPointer<ResourcePreview> preview;
    QString slotName;
  };

  rdcarray<DescriptorThumbUpdate> m_DescriptorThumbUpdates;

  QTime m_CustomShaderTimer;
  int m_CustomShaderWriteTime = 0;

  QFileSystemWatcher *m_Watcher = NULL;
  QStringList m_CustomShadersBusy;
  QMap<QString, ResourceId> m_CustomShaders;
  QMap<QString, IShaderViewer *> m_CustomShaderEditor;

  bool canCompileCustomShader(ShaderEncoding encoding);
  void reloadCustomShaders(const QString &filter);
  QList<QDir> getShaderDirectories() const;
  QString getShaderPath(const QString &filename) const;

  TextureDisplay m_TexDisplay;
};
