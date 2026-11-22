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

#include "TextureViewer.h"
#include <float.h>
#include <math.h>
#include <QClipboard>
#include <QColorDialog>
#include <QFileSystemWatcher>
#include <QFontDatabase>
#include <QImage>
#include <QItemDelegate>
#include <QJsonDocument>
#include <QMenu>
#include <QPainter>
#include <QPointer>
#include <QScopedValueRollback>
#include <QSplitter>
#include <QStackedWidget>
#include <QStyledItemDelegate>
#include <QTimer>
#include <QUuid>
#include <memory>
#include "Code/QRDUtils.h"
#include "Code/ReplayLab/ReplayLabAIResourceAttachment.h"
#include "Code/Resources.h"
#include "Dialogs/TextureSaveDialog.h"
#include "Widgets/Extended/RDHeaderView.h"
#include "Widgets/ResourcePreview.h"
#include "Widgets/TextureGoto.h"
#include "Windows/AI/AITextureAnnotation.h"
#include "Windows/RenderTargetHistoryView.h"
#include "flowlayout/FlowLayout.h"
#include "toolwindowmanager/ToolWindowManagerArea.h"
#include "ui_TextureViewer.h"

float area(const QSizeF &s)
{
  return s.width() * s.height();
}

float aspect(const QSizeF &s)
{
  return s.width() / s.height();
}

static bool CanAverageTextureSamples(const TextureDescription &texture, CompType typeCast)
{
  const CompType effectiveType =
      typeCast == CompType::Typeless ? texture.format.compType : typeCast;
  return effectiveType != CompType::UInt && effectiveType != CompType::SInt &&
         effectiveType != CompType::Depth &&
         !(texture.creationFlags & TextureCategory::DepthTarget);
}

static bool TexturePresentationMatchesCanvas(const rdcpair<int32_t, int32_t> &rendered,
                                             const QSize &canvas)
{
  return canvas.width() > 0 && canvas.height() > 0 && rendered.first == canvas.width() &&
         rendered.second == canvas.height();
}

// i18n-ignore: internal QObject property identifiers, never shown to the user.
static constexpr const char *RenderTargetHistoryEnabledProperty =
    "_renderdoc_render_target_history_enabled";
// i18n-ignore: internal QObject property identifiers, never shown to the user.
static constexpr const char *RenderTargetHistoryEventProperty =
    "_renderdoc_render_target_history_event";

class ScopedTextureHistoryReplay
{
public:
  ScopedTextureHistoryReplay(IReplayController *controller, uint32_t eventId)
      : m_Controller(controller), m_Active(controller && eventId != 0)
  {
    if(m_Active)
      m_Controller->ReplayFrameEventForAnalysis(eventId);
  }

  ~ScopedTextureHistoryReplay()
  {
    if(m_Active)
      m_Controller->RestoreFrameEventAfterAnalysis();
  }

private:
  IReplayController *m_Controller = NULL;
  bool m_Active = false;
};

static bool ResolveTextureHistorySnapshot(IReplayController *controller, uint32_t eventId,
                                          const TextureDisplay &source, TextureDisplay &resolved)
{
  resolved = source;
  if(controller == NULL || eventId == 0 || source.customShaderId != ResourceId() ||
     source.decodeYUV)
    return false;

  if(source.overlay != DebugOverlay::NoOverlay && source.overlay != DebugOverlay::NaN &&
     source.overlay != DebugOverlay::Clipping)
    return false;

  return controller->ResolveEventTextureSnapshot(eventId, source, resolved);
}

// if changing these functions, consider running the 'exhaustive test' at the bottom of this file
// once

static inline uint32_t MipCoordFromBase(const uint32_t coord, const uint32_t mip, const uint32_t dim)
{
  const uint32_t mipDim = (dim >> mip) > 0 ? (dim >> mip) : 1;

  // for mip levels where we more than half (e.g. 15x15 to 7x7) the coord can't be shifted by the
  // mip.
  // e.g. if the top level is 960x540 an x coordinate of 950 would be shifted by 7 down to 7, but
  // mip 7 is 7x4 so the max x co-ordinate is 6. Instead we need to get the float value on the top
  // mip, multiply by the mip dimension, and floor it

  const float coordf = float(coord) / float(dim);

  // we add 1e-6 to account for float errors, where we might not get back coord after rounding down
  // in the coordf calculation even when mipDim == dim. This will not affect the rounding for any
  // realistic texture sizes - even for a dim of 16383 and coord of 16382
  return uint32_t(mipDim * (coordf + 1e-6f));
}

static inline uint32_t BaseCoordFromMip(uint32_t coord, const uint32_t mip, const uint32_t dim)
{
  const uint32_t mipDim = (dim >> mip) > 0 ? (dim >> mip) : 1;

  // reverse of the above conversion

  const float coordf = float(coord) / float(mipDim);

  return uint32_t(dim * (coordf + 1e-6f));
}

static Descriptor MakeDescriptor(ResourceId res, Subresource sub = Subresource())
{
  Descriptor ret;
  ret.type = DescriptorType::ReadWriteImage;
  ret.resource = res;
  ret.firstMip = sub.mip;
  ret.firstSlice = sub.slice;
  return ret;
}

static UsedDescriptor MakeUsedDescriptor(ShaderStage stage, ResourceId res,
                                         Subresource sub = Subresource())
{
  UsedDescriptor ret;
  ret.descriptor = MakeDescriptor(res, sub);
  ret.access.stage = stage;
  ret.access.type = ret.descriptor.type;
  ret.access.index = DescriptorAccess::NoShaderBinding;
  ret.access.byteSize = 1;
  return ret;
}

static QMap<QString, ShaderEncoding> encodingExtensions = {
    {lit("hlsl"), ShaderEncoding::HLSL},
    {lit("glsl"), ShaderEncoding::GLSL},
    {lit("frag"), ShaderEncoding::GLSL},
    {lit("spvasm"), ShaderEncoding::SPIRVAsm},
    {lit("spvasm"), ShaderEncoding::OpenGLSPIRVAsm},
    {lit("slang"), ShaderEncoding::Slang},
};

Q_DECLARE_METATYPE(Following);

Following::Following(const TextureViewer &tex, FollowType type, ShaderStage stage, uint32_t index,
                     uint32_t arrayElement)
    : tex(tex), Type(type), Stage(stage), index(index), arrayEl(arrayElement)
{
}

Following::Following(const Following &other) : tex(other.tex)
{
  Type = other.Type;
  Stage = other.Stage;
  index = other.index;
  arrayEl = other.arrayEl;
}

namespace FollowingInternal
{
TextureViewer *invalid = NULL;
};

Following::Following() : tex(*FollowingInternal::invalid)
{
  // we need a default constructor for QVariant but we don't want it to be valid, we always
  // initialise Following() with a TextureViewer reference.
  Type = FollowType::OutputColor;
  Stage = ShaderStage::Pixel;
  index = 0;
  arrayEl = 0;
}

Following &Following::operator=(const Following &other)
{
  Type = other.Type;
  Stage = other.Stage;
  index = other.index;
  arrayEl = other.arrayEl;
  return *this;
}

bool Following::operator!=(const Following &o) const
{
  return !(*this == o);
}

bool Following::operator==(const Following &o) const
{
  return Type == o.Type && Stage == o.Stage && index == o.index && arrayEl == o.arrayEl;
}

void Following::GetActionContext(ICaptureContext &ctx, bool &copy, bool &clear, bool &compute)
{
  const ActionDescription *curAction = ctx.CurAction();
  copy = curAction != NULL &&
         (curAction->flags & (ActionFlags::Copy | ActionFlags::Resolve | ActionFlags::Present));
  clear = curAction != NULL && (curAction->flags & ActionFlags::Clear);
  compute = curAction != NULL && (curAction->flags & ActionFlags::Dispatch) &&
            ctx.CurPipelineState().GetShader(ShaderStage::Compute) != ResourceId();
}

int Following::GetHighestMip(ICaptureContext &ctx)
{
  return GetDescriptor(ctx, arrayEl).firstMip;
}

int Following::GetFirstArraySlice(ICaptureContext &ctx)
{
  return GetDescriptor(ctx, arrayEl).firstSlice;
}

CompType Following::GetTypeHint(ICaptureContext &ctx)
{
  return GetDescriptor(ctx, arrayEl).format.compType;
}

ResourceId Following::GetResourceId(ICaptureContext &ctx)
{
  return GetDescriptor(ctx, arrayEl).resource;
}

Descriptor Following::GetDescriptor(ICaptureContext &ctx, uint32_t arrayIdx)
{
  Descriptor ret;

  if(Type == FollowType::OutputColor)
  {
    rdcarray<Descriptor> outputs = GetOutputTargets(ctx);

    if(index < outputs.count())
      ret = outputs[index];
  }
  else if(Type == FollowType::OutputDepth)
  {
    ret = GetDepthTarget(ctx);
  }
  else if(Type == FollowType::OutputDepthResolve)
  {
    ret = GetDepthResolveTarget(ctx);
  }
  else if(Type == FollowType::ReadWrite)
  {
    const rdcarray<UsedDescriptor> &rw = tex.m_ReadWriteResources[(int)Stage];

    for(const UsedDescriptor &d : rw)
    {
      if(d.access.index == index && d.access.arrayElement == arrayIdx)
      {
        return d.descriptor;
      }
    }
  }
  else if(Type == FollowType::ReadOnly)
  {
    const rdcarray<UsedDescriptor> &ro = tex.m_ReadOnlyResources[(int)Stage];

    for(const UsedDescriptor &d : ro)
    {
      if(d.access.index == index && d.access.arrayElement == arrayIdx)
      {
        return d.descriptor;
      }
    }
  }

  return ret;
}

rdcarray<Descriptor> Following::GetOutputTargets(ICaptureContext &ctx)
{
  const ActionDescription *curAction = ctx.CurAction();
  // Present can retain an unrelated offscreen RT in the graphics pipeline. The following view
  // must show the actual presented image, matching the frame diagram's final output evidence.
  if(curAction && (curAction->flags & ActionFlags::Present) &&
     curAction->copyDestination != ResourceId())
    return {MakeDescriptor(curAction->copyDestination, curAction->copyDestinationSubresource)};
  bool copy = false, clear = false, compute = false;
  GetActionContext(ctx, copy, clear, compute);

  if(copy || clear)
  {
    return {MakeDescriptor(curAction->copyDestination, curAction->copyDestinationSubresource)};
  }
  else if(compute)
  {
    return {};
  }
  else
  {
    rdcarray<Descriptor> ret = ctx.CurPipelineState().GetOutputTargets();

    if(ret.isEmpty() && curAction != NULL && (curAction->flags & ActionFlags::Present))
    {
      if(curAction->copyDestination != ResourceId())
        return {MakeDescriptor(curAction->copyDestination, curAction->copyDestinationSubresource)};

      for(const TextureDescription &tex : ctx.GetTextures())
      {
        if(tex.creationFlags & TextureCategory::SwapBuffer)
          return {MakeDescriptor(tex.resourceId)};
      }
    }

    return ret;
  }
}

Descriptor Following::GetDepthTarget(ICaptureContext &ctx)
{
  bool copy = false, clear = false, compute = false;
  GetActionContext(ctx, copy, clear, compute);

  if(copy || clear || compute)
    return Descriptor();
  else
    return ctx.CurPipelineState().GetDepthTarget();
}

Descriptor Following::GetDepthResolveTarget(ICaptureContext &ctx)
{
  bool copy = false, clear = false, compute = false;
  GetActionContext(ctx, copy, clear, compute);

  if(copy || clear || compute)
    return Descriptor();
  else
    return ctx.CurPipelineState().GetDepthResolveTarget();
}

rdcarray<UsedDescriptor> Following::GetReadWriteResources(ICaptureContext &ctx, ShaderStage stage,
                                                          bool onlyUsed)
{
  bool copy = false, clear = false, compute = false;
  GetActionContext(ctx, copy, clear, compute);

  if(copy || clear)
  {
    return rdcarray<UsedDescriptor>();
  }
  else if(compute)
  {
    // only return compute resources for one stage
    if(stage != ShaderStage::Pixel && stage != ShaderStage::Compute)
      return rdcarray<UsedDescriptor>();

    return ctx.CurPipelineState().GetReadWriteResources(ShaderStage::Compute, onlyUsed);
  }
  else
  {
    return ctx.CurPipelineState().GetReadWriteResources(stage, onlyUsed);
  }
}

rdcarray<UsedDescriptor> Following::GetReadOnlyResources(ICaptureContext &ctx, ShaderStage stage,
                                                         bool onlyUsed)
{
  const ActionDescription *curAction = ctx.CurAction();
  bool copy = false, clear = false, compute = false;
  GetActionContext(ctx, copy, clear, compute);

  if(copy || clear)
  {
    rdcarray<UsedDescriptor> ret;

    // only return copy source for one stage
    if(copy && stage == ShaderStage::Pixel)
      ret.push_back(
          MakeUsedDescriptor(stage, curAction->copySource, curAction->copySourceSubresource));

    return ret;
  }
  else if(compute)
  {
    // only return compute resources for one stage
    if(stage != ShaderStage::Pixel && stage != ShaderStage::Compute)
      return rdcarray<UsedDescriptor>();

    return ctx.CurPipelineState().GetReadOnlyResources(ShaderStage::Compute, onlyUsed);
  }
  else
  {
    return ctx.CurPipelineState().GetReadOnlyResources(stage, onlyUsed);
  }
}

const ShaderReflection *Following::GetReflection(ICaptureContext &ctx, ShaderStage stage)
{
  bool copy = false, clear = false, compute = false;
  GetActionContext(ctx, copy, clear, compute);

  if(copy || clear)
    return NULL;
  else if(compute)
    return ctx.CurPipelineState().GetShaderReflection(ShaderStage::Compute);
  else
    return ctx.CurPipelineState().GetShaderReflection(stage);
}

const ShaderReflection *Following::GetReflection(ICaptureContext &ctx)
{
  return GetReflection(ctx, Stage);
}

namespace TextureListFilter
{
enum Columns
{
  Column_TexName,
  Column_TexWidth,
  Column_TexHeight,
  Column_TexDepth,
  Column_MipsCount,
  Column_TexFormat,
  Column_Count,
};
}

TextureDescription *TextureViewer::GetCurrentTexture()
{
  return m_CachedTexture;
}

TextureViewer::OutputBindingKey TextureViewer::UI_OutputBindingKey()
{
  OutputBindingKey key;
  auto append = [this, &key](FollowType type, ShaderStage stage, int slot, uint32_t arrayElement,
                             const Descriptor &descriptor) {
    if(m_Ctx.GetTexture(descriptor.resource) != NULL)
      key.emplace_back(type, stage, slot, arrayElement, descriptor.resource, descriptor.firstMip,
                       descriptor.firstSlice);
  };

  const rdcarray<Descriptor> targets = Following::GetOutputTargets(m_Ctx);
  for(uint32_t slot = 0; slot < targets.size(); slot++)
    append(FollowType::OutputColor, ShaderStage::Pixel, slot, 0, targets[slot]);
  append(FollowType::OutputDepth, ShaderStage::Pixel, 0, 0, Following::GetDepthTarget(m_Ctx));
  append(FollowType::OutputDepthResolve, ShaderStage::Pixel, 0, 0,
         Following::GetDepthResolveTarget(m_Ctx));

  // Shader bindings can vary between draws in one attachment group. Only use them to identify
  // the output group when there are no displayable framebuffer attachments (e.g. a dispatch).
  if(key.empty())
  {
    for(uint32_t stage = 0; stage < NumShaderStages; stage++)
      for(const UsedDescriptor &descriptor : m_ReadWriteResources[stage])
        if(!descriptor.access.staticallyUnused)
          append(FollowType::ReadWrite, descriptor.access.stage, descriptor.access.index,
                 descriptor.access.arrayElement, descriptor.descriptor);
  }

  return key;
}

void TextureViewer::UI_RememberOutputFollowing()
{
  if(m_Following.Type == FollowType::ReadOnly)
    return;

  ResourceId resource = m_Following.GetResourceId(m_Ctx);
  if(m_Ctx.GetTexture(resource) == NULL)
    return;

  OutputBindingKey key = UI_OutputBindingKey();
  if(!key.empty())
  {
    // Only an explicit selection writes this preference. Automatic fallback must never replace
    // the remembered choice when a different event temporarily lacks the selected binding.
    m_OutputPreferences.erase(key);
    m_OutputPreferences.emplace(std::move(key), std::make_pair(m_Following, resource));
  }
}

void TextureViewer::UI_ResolveOutputFollowing()
{
  if(currentTextureIsLocked() || m_Following.Type == FollowType::ReadOnly)
    return;

  const auto preference = m_OutputPreferences.find(UI_OutputBindingKey());
  if(preference != m_OutputPreferences.end())
  {
    Following remembered = preference->second.first;
    ResourceId resource = remembered.GetResourceId(m_Ctx);
    if(resource == preference->second.second && m_Ctx.GetTexture(resource) != NULL)
    {
      m_Following = remembered;
      return;
    }
  }

  // New output groups start at RT0, even if the previous group's slot is also bound here.
  // A group without RT0 uses its first available color/depth/UAV texture instead.
  m_Following = Following(*this, FollowType::OutputColor, ShaderStage::Pixel, 0, 0);
  const rdcarray<Descriptor> targets = Following::GetOutputTargets(m_Ctx);
  for(uint32_t slot = 0; slot < targets.size(); slot++)
  {
    if(m_Ctx.GetTexture(targets[slot].resource) != NULL)
    {
      m_Following = Following(*this, FollowType::OutputColor, ShaderStage::Pixel, slot, 0);
      return;
    }
  }

  if(m_Ctx.GetTexture(Following::GetDepthTarget(m_Ctx).resource) != NULL)
  {
    m_Following = Following(*this, FollowType::OutputDepth, ShaderStage::Pixel, 0, 0);
    return;
  }

  if(m_Ctx.GetTexture(Following::GetDepthResolveTarget(m_Ctx).resource) != NULL)
  {
    m_Following = Following(*this, FollowType::OutputDepthResolve, ShaderStage::Pixel, 0, 0);
    return;
  }

  // These arrays contain only this action's active stages. Never select a binding left over
  // from the previous graphics/compute action, and skip buffer UAVs that cannot be displayed.
  for(uint32_t stage = 0; stage < NumShaderStages; stage++)
  {
    for(const UsedDescriptor &descriptor : m_ReadWriteResources[stage])
    {
      if(!descriptor.access.staticallyUnused &&
         m_Ctx.GetTexture(descriptor.descriptor.resource) != NULL)
      {
        Following candidate(*this, FollowType::ReadWrite, descriptor.access.stage,
                            descriptor.access.index, descriptor.access.arrayElement);
        if(candidate.GetResourceId(m_Ctx) == descriptor.descriptor.resource)
        {
          m_Following = candidate;
          return;
        }
      }
    }
  }
}

void TextureViewer::UI_UpdateCachedTexture()
{
  if(!m_Ctx.IsCaptureLoaded())
  {
    m_CachedTexture = NULL;
    return;
  }

  ResourceId id = m_LockedId;
  if(id == ResourceId())
    id = m_Following.GetResourceId(m_Ctx);

  m_CachedTexture = m_Ctx.GetTexture(id);
  if(m_TemporaryOutputFollowToken && m_TemporaryPreviewActive &&
     m_TemporaryPreviewEvent == m_Ctx.CurEvent() && m_LockedId == ResourceId())
    m_CachedTexture = &m_TemporaryPreviewTexture;

  if(m_CachedTexture != NULL)
  {
    if(m_Ctx.APIProps().shaderDebugging)
    {
      const ShaderReflection *shaderDetails =
          m_Ctx.CurPipelineState().GetShaderReflection(ShaderStage::Pixel);

      if(!m_Ctx.CurAction() ||
         !(m_Ctx.CurAction()->flags & (ActionFlags::MeshDispatch | ActionFlags::Drawcall)))
      {
        ui->debugPixelContext->setEnabled(false);
        ui->debugPixelContext->setToolTip(tr("No draw call selected"));
      }
      else if(!shaderDetails)
      {
        ui->debugPixelContext->setEnabled(false);
        ui->debugPixelContext->setToolTip(tr("No pixel shader bound"));
      }
      else if(!shaderDetails->debugInfo.debuggable)
      {
        ui->debugPixelContext->setEnabled(false);
        ui->debugPixelContext->setToolTip(
            tr("The current pixel shader does not support debugging: %1")
                .arg(shaderDetails->debugInfo.debugStatus));
      }
      else
      {
        ui->debugPixelContext->setEnabled(true);
        ui->debugPixelContext->setToolTip(QString());
      }
    }
    else
    {
      ui->debugPixelContext->setEnabled(false);
      ui->debugPixelContext->setToolTip(tr("Shader Debugging not supported on this API"));
    }

    if(m_Ctx.APIProps().pixelHistory)
    {
      ui->pixelHistory->setEnabled(true);
      ui->pixelHistory->setToolTip(QString());
    }
    else
    {
      ui->pixelHistory->setEnabled(false);
      ui->pixelHistory->setToolTip(tr("Pixel History not supported on this API"));
    }
  }
  else
  {
    ui->debugPixelContext->setEnabled(false);
    ui->debugPixelContext->setToolTip(tr("No active texture selected"));
    ui->pixelHistory->setEnabled(false);
    ui->pixelHistory->setToolTip(tr("No active texture selected"));
  }
}

TextureViewer::TextureViewer(ICaptureContext &ctx, QWidget *parent)
    : QFrame(parent),
      ui(new Ui::TextureViewer),
      m_Ctx(ctx),
      m_Following(*this, FollowType::OutputColor, ShaderStage::Pixel, 0, 0),
      m_BeforeTemporaryOutputFollow(*this, FollowType::OutputColor, ShaderStage::Pixel, 0, 0)
{
  ui->setupUi(this);

  m_AnalysisTimer = new QTimer(this);
  m_AnalysisTimer->setSingleShot(true);
  m_AnalysisTimer->setInterval(75);
  QObject::connect(m_AnalysisTimer, &QTimer::timeout, this, &TextureViewer::QueueDeferredAnalysis);

  // Keep the native render window in its own page. Qt painting over a native GPU child is not
  // reliable on Windows; the annotation page displays a pinned replay readback instead.
  ui->renderLayout->removeItem(ui->gridLayout);
  m_AIAnnotationStack = new QStackedWidget(this);
  QWidget *liveTexture = new QWidget(m_AIAnnotationStack);
  liveTexture->setLayout(ui->gridLayout);
  m_AIAnnotationStack->addWidget(liveTexture);
  ui->renderLayout->insertWidget(0, m_AIAnnotationStack, 1);

  ui->render->SetContext(m_Ctx);
  ui->pixelContext->SetContext(m_Ctx);

  // Tab transitions temporarily hide the native surfaces while retaining their layout extent.
  for(CustomPaintWidget *surface : {ui->render, ui->pixelContext})
  {
    QSizePolicy policy = surface->sizePolicy();
    policy.setRetainSizeWhenHidden(true);
    surface->setSizePolicy(policy);
  }

  ui->textureList->setFont(Formatter::PreferredFont());
  ui->textureListFilter->setFont(Formatter::PreferredFont());
  ui->rangeBlack->setFont(Formatter::PreferredFont());
  ui->rangeWhite->setFont(Formatter::PreferredFont());
  ui->hdrMul->setFont(Formatter::PreferredFont());
  ui->channels->setFont(Formatter::PreferredFont());
  ui->mipLevel->setFont(Formatter::PreferredFont());
  ui->sliceFace->setFont(Formatter::PreferredFont());
  ui->zoomOption->setFont(Formatter::PreferredFont());

  Reset();

  on_checkerBack_clicked();

  QObject::connect(ui->zoomOption->lineEdit(), &QLineEdit::returnPressed, this,
                   &TextureViewer::zoomOption_returnPressed);

  QObject::connect(ui->depthDisplay, &QToolButton::toggled, this,
                   &TextureViewer::channelsWidget_toggled);
  QObject::connect(ui->stencilDisplay, &QToolButton::toggled, this,
                   &TextureViewer::channelsWidget_toggled);
  QObject::connect(ui->flip_y, &QToolButton::toggled, this, &TextureViewer::channelsWidget_toggled);
  QObject::connect(ui->gammaDisplay, &QToolButton::toggled, this,
                   &TextureViewer::channelsWidget_toggled);
  QObject::connect(ui->channels, OverloadedSlot<int>::of(&QComboBox::currentIndexChanged), this,
                   &TextureViewer::channelsWidget_selected);
  QObject::connect(ui->hdrMul, OverloadedSlot<int>::of(&QComboBox::currentIndexChanged), this,
                   &TextureViewer::channelsWidget_selected);
  QObject::connect(ui->hdrMul, &QComboBox::currentTextChanged, [this] { UI_UpdateChannels(); });
  QObject::connect(ui->customShader, OverloadedSlot<int>::of(&QComboBox::currentIndexChanged), this,
                   &TextureViewer::channelsWidget_selected);
  QObject::connect(ui->customShader, &QComboBox::currentTextChanged, [this] { UI_UpdateChannels(); });
  QObject::connect(ui->rangeHistogram, &RangeHistogram::rangeUpdated, this,
                   &TextureViewer::range_rangeUpdated);
  QObject::connect(ui->rangeBlack, &RDLineEdit::textChanged, this,
                   &TextureViewer::rangePoint_textChanged);
  QObject::connect(ui->rangeBlack, &RDLineEdit::leave, this, &TextureViewer::rangePoint_leave);
  QObject::connect(ui->rangeBlack, &RDLineEdit::keyPress, this, &TextureViewer::rangePoint_keyPress);
  QObject::connect(ui->rangeWhite, &RDLineEdit::textChanged, this,
                   &TextureViewer::rangePoint_textChanged);
  QObject::connect(ui->rangeWhite, &RDLineEdit::leave, this, &TextureViewer::rangePoint_leave);
  QObject::connect(ui->rangeWhite, &RDLineEdit::keyPress, this, &TextureViewer::rangePoint_keyPress);

  for(RDToolButton *butt : {ui->channelRed, ui->channelGreen, ui->channelBlue, ui->channelAlpha})
  {
    QObject::connect(butt, &RDToolButton::toggled, this, &TextureViewer::channelsWidget_toggled);
    QObject::connect(butt, &RDToolButton::mouseClicked, this,
                     &TextureViewer::channelsWidget_mouseClicked);
    QObject::connect(butt, &RDToolButton::doubleClicked, this,
                     &TextureViewer::channelsWidget_mouseClicked);
  }

  {
    QMenu *extensionsMenu = new QMenu(this);

    ui->extensions->setMenu(extensionsMenu);
    ui->extensions->setPopupMode(QToolButton::InstantPopup);

    QObject::connect(extensionsMenu, &QMenu::aboutToShow, [this, extensionsMenu]() {
      extensionsMenu->clear();
      m_Ctx.Extensions().MenuDisplaying(PanelMenu::TextureViewer, extensionsMenu, ui->extensions, {});
    });
  }

  QObject::connect(ui->textureList, &RDTreeWidget::itemActivated, this,
                   &TextureViewer::texture_itemActivated);

  QWidget *renderContainer = ui->renderContainer;

  ui->dockarea->addToolWindow(ui->renderContainer, ToolWindowManager::EmptySpace);
  ui->dockarea->setToolWindowProperties(
      renderContainer, ToolWindowManager::DisallowUserDocking | ToolWindowManager::HideCloseButton |
                           ToolWindowManager::DisableDraggableTab |
                           ToolWindowManager::AlwaysDisplayFullTabs);

  ui->dockarea->addToolWindow(
      ui->textureListFrame,
      ToolWindowManager::AreaReference(ToolWindowManager::NoArea,
                                       ui->dockarea->areaOf(renderContainer), 0.25f));
  ui->dockarea->setToolWindowProperties(ui->textureListFrame, ToolWindowManager::HideOnClose);

  ui->dockarea->addToolWindow(ui->inputThumbs, ToolWindowManager::AreaReference(
                                                   ToolWindowManager::RightOf,
                                                   ui->dockarea->areaOf(renderContainer), 0.25f));
  ui->dockarea->setToolWindowProperties(ui->inputThumbs, ToolWindowManager::HideCloseButton);

  ui->dockarea->addToolWindow(
      ui->outputThumbs, ToolWindowManager::AreaReference(ToolWindowManager::AddTo,
                                                         ui->dockarea->areaOf(ui->inputThumbs)));
  ui->dockarea->setToolWindowProperties(ui->outputThumbs, ToolWindowManager::HideCloseButton);

  ui->dockarea->addToolWindow(
      ui->pixelContextLayout,
      ToolWindowManager::AreaReference(ToolWindowManager::BottomOf,
                                       ui->dockarea->areaOf(ui->outputThumbs), 0.25f));
  ui->dockarea->setToolWindowProperties(ui->pixelContextLayout, ToolWindowManager::HideCloseButton);

  ui->dockarea->setAllowFloatingWindow(false);

  renderContainer->setWindowTitle(tr("Unbound"));
  ui->pixelContextLayout->setWindowTitle(tr("Pixel Context"));
  ui->outputThumbs->setWindowTitle(tr("Outputs"));
  ui->inputThumbs->setWindowTitle(tr("Inputs"));
  ui->textureListFrame->setWindowTitle(tr("Texture List"));

  m_Goto = new TextureGoto(this, [this](QPoint p) { GotoLocation(p.x(), p.y()); });

  QVBoxLayout *vertical = new QVBoxLayout(this);

  vertical->setSpacing(3);
  vertical->setContentsMargins(3, 3, 3, 3);

  QWidget *flow1widget = new QWidget(this);
  QWidget *flow2widget = new QWidget(this);

  FlowLayout *flow1 = new FlowLayout(flow1widget, 0, 3, 3);
  FlowLayout *flow2 = new FlowLayout(flow2widget, 0, 3, 3);

  flow1widget->setSizePolicy(QSizePolicy::Preferred, QSizePolicy::Minimum);
  flow2widget->setSizePolicy(QSizePolicy::Preferred, QSizePolicy::Minimum);

  flow1->addWidget(ui->channelsToolbar);
  flow1->addWidget(ui->subresourceToolbar);
  flow1->addWidget(ui->actionToolbar);

  flow2->addWidget(ui->zoomToolbar);
  flow2->addWidget(ui->overlayToolbar);
  flow2->addWidget(ui->rangeToolbar);

  vertical->addWidget(flow1widget);
  vertical->addWidget(flow2widget);
  m_MainHistorySplitter = new QSplitter(Qt::Vertical, this);
  m_MainHistorySplitter->setChildrenCollapsible(false);
  m_MainHistorySplitter->addWidget(ui->dockarea);
  m_MainHistorySplitter->setStretchFactor(0, 1);
  vertical->addWidget(m_MainHistorySplitter, 1);

  Ui_TextureViewer *u = ui;
  u->pixelcontextgrid->setAlignment(u->pixelHistory, Qt::AlignCenter);
  u->pixelcontextgrid->setAlignment(u->debugPixelContext, Qt::AlignCenter);

  QWidget *statusflowWidget = new QWidget(this);

  ui->statusbar->removeWidget(ui->texStatusName);
  ui->statusbar->removeWidget(ui->texStatusDim);
  ui->statusbar->removeWidget(ui->texStatusFormat);
  ui->statusbar->removeWidget(ui->pickSwatch);
  ui->statusbar->removeWidget(ui->hoverText);
  ui->statusbar->removeWidget(ui->pickedText);

  FlowLayout *statusflow = new FlowLayout(0, 3, 0);

  statusflowWidget->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Minimum);

  statusflow->addWidget(ui->texStatusName);
  statusflow->addWidget(ui->texStatusDim);
  statusflow->addWidget(ui->texStatusFormat);
  statusflow->addWidget(ui->pickSwatch);
  statusflow->addWidget(ui->hoverText);
  statusflow->addWidget(ui->pickedText);

  ui->texStatusName->setFont(Formatter::FixedFont());
  ui->texStatusDim->setFont(Formatter::FixedFont());
  ui->texStatusFormat->setFont(Formatter::FixedFont());
  ui->hoverText->setFont(Formatter::FixedFont());
  ui->pickedText->setFont(Formatter::FixedFont());

  ui->renderLayout->removeItem(ui->statusbar);
  ui->renderLayout->addItem(statusflow);

  ui->channels->addItems({lit("RGBA"), lit("RGBM"), lit("YUVA decode"), tr("Custom")});

  ui->zoomOption->addItems({lit("10%"), lit("25%"), lit("50%"), lit("75%"), lit("100%"),
                            lit("200%"), lit("400%"), lit("800%")});

  ui->hdrMul->addItems({lit("2"), lit("4"), lit("8"), lit("16"), lit("32"), lit("128")});

  ui->overlay->addItems({tr("None"), tr("Highlight Drawcall"), tr("Wireframe Mesh"),
                         tr("Depth Test"), tr("Stencil Test"), tr("Backface Cull"),
                         tr("Viewport/Scissor Region"), tr("NaN/INF/-ve Display"),
                         tr("Histogram Clipping"), tr("Clear Before Pass"), tr("Clear Before Draw"),
                         tr("Quad Overdraw (Pass)"), tr("Quad Overdraw (Draw)"),
                         tr("Triangle Size (Pass)"), tr("Triangle Size (Draw)")});

  ui->textureListFilter->addItems({QString(), tr("Textures"), tr("Render Targets")});

  ui->textureList->setColumns({tr("Texture Name"), tr("Width"), tr("Height"), tr("Depth/Slices"),
                               tr("Mips Count"), tr("Format"), tr("Go")});
  ui->textureList->setHoverIconColumn(6, Icons::action(), Icons::action_hover());
  ui->textureList->viewport()->setAttribute(Qt::WA_Hover);
  ui->textureList->setMouseTracking(true);
  {
    RDHeaderView *header = new RDHeaderView(Qt::Horizontal, this);
    ui->textureList->setHeader(header);

    header->setColumnStretchHints({1, -1, -1, -1, -1, -1, -1});
  }

  ui->textureList->sortByColumn(TextureListFilter::Column_TexName, Qt::SortOrder::AscendingOrder);

  ui->zoomOption->setCurrentText(QString());
  ui->fitToWindow->toggle();

  m_Ctx.AddCaptureViewer(this);

  SetupTextureTabs();

  QObject::connect(ui->render, &CustomPaintWidget::clicked, this, &TextureViewer::render_mouseClick);
  QObject::connect(ui->render, &CustomPaintWidget::mouseMove, this, &TextureViewer::render_mouseMove);
  QObject::connect(ui->render, &CustomPaintWidget::mouseWheel, this,
                   &TextureViewer::render_mouseWheel);
  QObject::connect(ui->render, &CustomPaintWidget::resize, this, &TextureViewer::render_resize);
  QObject::connect(ui->render, &CustomPaintWidget::keyPress, this, &TextureViewer::render_keyPress);

  QObject::connect(ui->pixelContext, &CustomPaintWidget::keyPress, this,
                   &TextureViewer::render_keyPress);
}

TextureViewer::~TextureViewer()
{
  m_EventRequestSerial.fetch_add(1, std::memory_order_acq_rel);
  m_DisplayRequestSerial.fetch_add(1, std::memory_order_acq_rel);

  if(m_Output)
  {
    m_Ctx.Replay().BlockInvoke([this](IReplayController *r) {
      for(auto &thumbnail : m_NativeThumbnailOutputs)
        if(thumbnail.second)
          thumbnail.second->Shutdown();
      m_NativeThumbnailOutputs.clear();
      m_Output->Shutdown();
    });
  }

  m_Ctx.BuiltinWindowClosed(this);
  m_Ctx.RemoveCaptureViewer(this);
  CompletePresentationWaiters();
  delete ui;
}

void TextureViewer::enterEvent(QEvent *event)
{
  HighlightUsage();
}

void TextureViewer::showEvent(QShowEvent *event)
{
  HighlightUsage();
}

void TextureViewer::HighlightUsage()
{
  TextureDescription *texptr = GetCurrentTexture();

  if(texptr && m_Ctx.HasTimelineBar())
    m_Ctx.GetTimelineBar()->HighlightResourceUsage(texptr->resourceId);
}

void TextureViewer::SelectPreview(ResourcePreview *prev)
{
  if(!m_UpdatingTemporaryOutputFollow)
    m_TemporaryOutputFollowToken = 0;
  Following follow = prev->property("f").value<Following>();

  for(ResourcePreview *p : ui->outputThumbs->thumbs())
    p->setSelected(false);

  for(ResourcePreview *p : ui->inputThumbs->thumbs())
    p->setSelected(false);

  m_Following = Following(follow);
  UI_RememberOutputFollowing();
  prev->setSelected(true);

  UI_UpdateCachedTexture();

  ResourceId id = m_Following.GetResourceId(m_Ctx);

  UI_OnTextureSelectionChanged(false);
  if(id != ResourceId())
    ui->renderContainer->show();
}

TextureViewer::ViewRequest TextureViewer::CurrentViewRequest() const
{
  ViewRequest request;
  request.display = m_TexDisplay;
  if(m_CachedTexture)
    request.texture = *m_CachedTexture;
  request.pickedPoint = m_PickedPoint;
  request.hoverPixel = m_CurHoverPixel;
  request.eventId = m_Ctx.CurEvent();
  request.historyEvent = ActiveRenderTargetHistoryEvent();
  request.serial = m_DisplayRequestSerial.load(std::memory_order_acquire);
  request.eventSerial = m_EventRequestSerial.load(std::memory_order_acquire);
  request.rangeMin = ui->rangeHistogram->rangeMin();
  request.rangeMax = ui->rangeHistogram->rangeMax();
  request.visualise = m_Visualise;
  request.clearBeforeReveal = m_PresentationSuspended;
  return request;
}

bool TextureViewer::IsCurrentViewRequest(const ViewRequest &request) const
{
  return m_DisplayRequestSerial.load(std::memory_order_acquire) == request.serial &&
         m_EventRequestSerial.load(std::memory_order_acquire) == request.eventSerial;
}

void TextureViewer::RT_FetchCurrentPixel(IReplayController *r, const ViewRequest &request,
                                         const TextureDisplay &display, uint32_t x, uint32_t y,
                                         PixelValue &pickValue, PixelValue &realValue)
{
  if(request.texture.resourceId == ResourceId() || m_Output == NULL)
    return;

  if(display.flipY)
    y = (request.texture.height - 1) - y;

  x = ::MipCoordFromBase(x, request.display.subresource.mip, request.texture.width);
  y = ::MipCoordFromBase(y, request.display.subresource.mip, request.texture.height);

  ResourceId id = display.resourceId;
  Subresource sub = display.subresource;
  CompType typeCast = display.typeCast;

  if(display.overlay == DebugOverlay::QuadOverdrawDraw ||
     display.overlay == DebugOverlay::QuadOverdrawPass ||
     display.overlay == DebugOverlay::TriangleSizeDraw ||
     display.overlay == DebugOverlay::TriangleSizePass)
  {
    ResourceId overlayResId = m_Output->GetDebugOverlayTexID();

    if(overlayResId != ResourceId())
    {
      id = overlayResId;
      typeCast = CompType::Typeless;
    }
  }

  realValue = r->PickPixel(id, x, y, sub, typeCast);

  if(display.customShaderId != ResourceId())
  {
    pickValue = r->PickPixel(m_Output->GetCustomShaderTexID(), x, y,
                             {display.subresource.mip, 0, 0}, CompType::Typeless);
  }
  else
  {
    pickValue = realValue;
  }
}

void TextureViewer::RT_PickPixelsAndUpdate(IReplayController *r, const ViewRequest &request,
                                           bool hover)
{
  const QPoint point = hover ? request.hoverPixel : request.pickedPoint;
  PixelValue pickValue = {}, realValue = {};

  if(!IsCurrentViewRequest(request) || r->GetCurrentEventID() != request.eventId ||
     m_Output == NULL || request.texture.resourceId == ResourceId() || point.x() < 0 || point.y() < 0)
    return;

  TextureDisplay analysisDisplay;
  const bool haveSnapshot =
      ResolveTextureHistorySnapshot(r, request.historyEvent, request.display, analysisDisplay);
  ScopedTextureHistoryReplay historyReplay(r, haveSnapshot ? 0 : request.historyEvent);

  uint32_t x = (uint32_t)point.x();
  uint32_t y = (uint32_t)point.y();

  RT_FetchCurrentPixel(r, request, analysisDisplay, x, y, pickValue, realValue);

  if(!hover)
  {
    if(haveSnapshot)
    {
      x = ::MipCoordFromBase(x, request.display.subresource.mip, request.texture.width);
      y = ::MipCoordFromBase(y, request.display.subresource.mip, request.texture.height);
    }
    m_Output->SetPixelContextLocation(x, y);
  }

  QPointer<TextureViewer> self(this);
  GUIInvoke::call(this, [self, request, point, hover, pickValue, realValue]() {
    if(!self || !self->IsCurrentViewRequest(request) ||
       point != (hover ? self->m_CurHoverPixel : self->m_PickedPoint))
      return;
    self->m_CurHoverValue = pickValue;
    if(!hover)
    {
      self->m_CurPixelValue = pickValue;
      self->m_CurRealValue = realValue;
    }
    self->UI_UpdateStatusText();
  });
}

void TextureViewer::RT_UpdateAndDisplay(IReplayController *, const TextureDisplay &display,
                                        uint32_t historyEvent)
{
  if(m_Output != NULL)
  {
    if(historyEvent != 0)
      m_Output->DisplayTextureAtEvent(historyEvent, display);
    else
    {
      m_Output->SetTextureDisplay(display);
      // SetTextureDisplay only changes replay-side presentation state. Draw and present it in the
      // same coalesced callback so a queued QWidget paint cannot add a second replay-thread hop.
      m_Output->Display();
    }
  }
}

void TextureViewer::SuspendTexturePresentation()
{
  // Reparenting the shared native window retains its last presented backbuffer. Invalidate queued
  // work and hide it before moving the layout, so a history image cannot become the current tab's
  // first visible frame. A running replay may finish, but only the matching request can reveal it.
  m_DisplayRequestSerial.fetch_add(1, std::memory_order_acq_rel);
  ui->render->SetOutputEventId(0);
  ui->pixelContext->SetOutputEventId(0);
  // i18n-ignore: internal presentation-evidence properties for UI regression fixtures.
  ui->render->setProperty("eidDisplayedEvent", QVariant());
  // i18n-ignore: internal presentation-evidence properties for UI regression fixtures.
  ui->render->setProperty("eidDisplayedResource", QVariant());
  if(m_Output && m_Ctx.IsCaptureLoaded())
  {
    m_PresentationSuspended = true;
    m_PresentationPending = true;
    // i18n-ignore: internal presentation-evidence property for UI regression fixtures.
    ui->render->setProperty("eidPresentationPending", true);
    ui->render->hide();
    ui->pixelContext->hide();
  }
}

void TextureViewer::CapturePresentationWitness()
{
  // i18n-ignore: opt-in profiling environment and evidence property identifiers, never UI text.
  if(qgetenv("RENDERDOC_EID_TIMING") != "1")
    return;
  QImage image;
  if(m_Ctx.IsCaptureLoaded() && m_Output && !m_PresentationSuspended)
  {
    const ViewRequest request = CurrentViewRequest();
    m_Ctx.Replay().BlockInvoke([this, request, &image](IReplayController *r) {
      if(!IsCurrentViewRequest(request) || r->GetCurrentEventID() != request.eventId)
        return;
      // Read the existing output, without Display(), rebinding, or an alternate output. A test
      // must observe a stale output image if one exists, not repair it by rendering its reference.
      const rdcpair<int32_t, int32_t> dimensions = m_Output->GetDimensions();
      if(dimensions.first <= 0 || dimensions.second <= 0)
        return;
      const uint64_t expected = uint64_t(dimensions.first) * dimensions.second * 3;
      if(expected > 256ULL * 1024 * 1024)
        return;
      const bytebuf pixels = m_Output->ReadbackOutputTexture();
      if(expected != pixels.size() || !IsCurrentViewRequest(request))
        return;
      image = QImage(pixels.data(), dimensions.first, dimensions.second, dimensions.first * 3,
                     QImage::Format_RGB888).copy();
    });
    if(!IsCurrentViewRequest(request))
      image = QImage();
  }
  // i18n-ignore: one explicitly requested diagnostic frame; the fixture clears it after reading.
  setProperty("eidPresentationWitness", image);
}

void TextureViewer::WhenPresentationReady(uint32_t eventId, std::function<void()> completed)
{
  if(!m_Ctx.IsCaptureLoaded() || !m_Output || !isVisible() || m_Ctx.CurEvent() != eventId ||
     (!m_PresentationPending &&
      m_CompletedDisplayRequestSerial == m_DisplayRequestSerial.load(std::memory_order_acquire)))
  {
    completed();
    return;
  }
  m_PresentationWaiters.push_back(std::move(completed));
}

void TextureViewer::CompletePresentationWaiters()
{
  // Detach before callbacks: a completion can navigate, close a view or request another update.
  std::vector<std::function<void()>> waiters;
  waiters.swap(m_PresentationWaiters);
  for(const auto &completed : waiters)
    completed();
}

void TextureViewer::QueueUpdateAndDisplay(bool updateAnalysis)
{
  // Capture-close notifications also repaint widgets. They must not create new native outputs
  // after the close barrier or recreate a preview child whose capture has already been detached.
  if(!m_Ctx.IsCaptureLoaded() || !m_Output)
    return;
  if(m_RetargetingRenderTargetHistory || m_UpdatingTextureState)
    return;

  m_AnalysisPending = m_AnalysisPending || updateAnalysis;

  QPointer<TextureViewer> self(this);
  m_DisplayRequestSerial.fetch_add(1, std::memory_order_acq_rel);
  ViewRequest request = CurrentViewRequest();
  // i18n-ignore: internal presentation identities used by isolated regression fixtures.
  ui->render->setProperty("eidRequestedSerial", qulonglong(request.serial));
  // i18n-ignore: internal presentation-evidence property, never shown to the user.
  ui->render->setProperty("eidPresentationPending", true);
  CaptureNativeThumbnailBindings(request);
  m_Ctx.Replay().AsyncInvoke(
      // i18n-ignore: internal replay-request coalescing tag, never shown to the user.
      lit("TextureViewerHistoryDisplay"), [self, request, updateAnalysis](IReplayController *r) {
        if(!self || !self->IsCurrentViewRequest(request) || r->GetCurrentEventID() != request.eventId)
          return;
        self->RT_ExecuteDisplayRequest(r, request, updateAnalysis);
      });
}

void TextureViewer::CaptureNativeThumbnailBindings(ViewRequest &request)
{
  // Capture only visible preview bindings on the GUI thread. Their cached native outputs draw
  // directly from replay textures; no thumbnail pixels or readback buffers cross to Qt.
  for(ThumbnailStrip *strip : {ui->outputThumbs, ui->inputThumbs})
  {
    for(ResourcePreview *preview : strip->thumbs())
    {
      if(!preview->isActive() || !preview->isVisible() || preview->visibleRegion().isEmpty())
        continue;
      ViewRequest::ThumbnailBinding binding;
      binding.preview = preview;
      binding.size = preview->GetThumbSize() * preview->devicePixelRatioF();
      if(preview->NativePresentationMatches(request.eventSerial, binding.size))
        continue;
      binding.window = preview->GetWidgetWindowingData();
      binding.resource = preview->property("id").value<ResourceId>();
      binding.subresource = {preview->property("mip").toUInt(), preview->property("slice").toUInt(),
                             0};
      binding.typeCast = (CompType)preview->property("cast").toUInt();
      const TextureDescription *texture = m_Ctx.GetTexture(binding.resource);
      binding.depth = texture && ((texture->creationFlags & TextureCategory::DepthTarget) ||
                                  texture->format.compType == CompType::Depth);
      request.thumbnails.push_back(binding);
    }
  }
}

void TextureViewer::QueueNativeThumbnailRefresh()
{
  if(!m_Ctx.IsCaptureLoaded() || !m_Output || m_UpdatingTextureState)
    return;
  ViewRequest request = CurrentViewRequest();
  CaptureNativeThumbnailBindings(request);
  if(request.thumbnails.empty())
    return;
  QPointer<TextureViewer> self(this);
  m_Ctx.Replay().AsyncInvoke(
      // i18n-ignore: coalesced native-preview expose/resize queue; not a new main-image request.
      lit("TextureViewerNativeThumbnails"), [self, request](IReplayController *r) {
        if(self && self->IsCurrentViewRequest(request) && r->GetCurrentEventID() == request.eventId)
          self->RT_UpdateNativeThumbnails(r, request);
      });
}

void TextureViewer::RT_ExecuteDisplayRequest(IReplayController *r, const ViewRequest &request,
                                             bool updateAnalysis)
{
  QPointer<TextureViewer> self(this);
  if(request.clearBeforeReveal)
  {
    // A hidden native child does not receive its final resize until show(). Clear the old
    // surface before that show, then bind the final source only after the visible layout is
    // settled. No history replay or current-event navigation is needed for this clear.
    TextureDisplay blank = request.display;
    blank.resourceId = ResourceId();
    blank.customShaderId = ResourceId();
    blank.overlay = DebugOverlay::NoOverlay;
    blank.decodeYUV = false;
    self->RT_UpdateAndDisplay(r, blank, 0);
  }
  else
  {
    self->RT_UpdateAndDisplay(r, request.display, request.historyEvent);
  }
  if(!self->IsCurrentViewRequest(request))
    return;
  if(self->m_Output && r->GetFatalErrorStatus().code == ResultCode::Succeeded)
  {
    const rdcpair<int32_t, int32_t> renderedDimensions = self->m_Output->GetDimensions();
    GUIInvoke::call(qApp, [self, request, updateAnalysis, renderedDimensions]() {
      if(!self || !self->IsCurrentViewRequest(request) || self->m_Ctx.CurEvent() != request.eventId)
        return;
      if(request.clearBeforeReveal)
      {
        // Keep the paint gate and completion identity closed throughout show()/resize.
        // Those synchronous resize callbacks update fitted scale/scrollbars, but must not
        // race an intermediate request ahead of the final visible layout.
        self->m_PresentationSuspended = false;
        {
          QScopedValueRollback<bool> retargeting(self->m_RetargetingRenderTargetHistory, true);
          self->ui->render->show();
          self->ui->pixelContext->show();
          self->ui->renderLayout->activate();
          self->ui->render->layout()->activate();
          self->ui->pixelContext->layout()->activate();
        }
        GUIInvoke::defer(qApp, [self, request, updateAnalysis]() {
          if(self && self->IsCurrentViewRequest(request) && self->m_Ctx.CurEvent() == request.eventId)
            self->QueueUpdateAndDisplay(updateAnalysis);
        });
        return;
      }
      if(self->m_PresentationPending &&
         !TexturePresentationMatchesCanvas(
             renderedDimensions, QSize(self->realRenderWidth(), self->realRenderHeight())))
      {
        // A later layout/resize may have overtaken the worker without changing EID. It is
        // not a completed presentation of this canvas; request the settled dimensions.
        if(self->ui->render->isVisible())
          self->QueueUpdateAndDisplay(updateAnalysis);
        return;
      }
      // Open native painting only after this request has actually bound and displayed its
      // source. A stale callback must not reopen the gate after a newer tab hid the surface.
      self->ui->render->SetOutputEventId(request.eventId);
      self->ui->pixelContext->SetOutputEventId(request.eventId);
      // i18n-ignore: internal completion-evidence property for the replay benchmark.
      self->ui->render->setProperty("eidDisplayedEvent",
                                    request.historyEvent ? request.historyEvent : request.eventId);
      // i18n-ignore: internal completion-evidence property for the replay benchmark.
      self->ui->render->setProperty("eidDisplayedResource", ToQStr(request.display.resourceId));
      // i18n-ignore: internal completion-evidence properties for UI regression fixtures.
      self->ui->render->setProperty("eidPresentationSerial", qulonglong(request.serial));
      // i18n-ignore: internal completion-evidence properties for UI regression fixtures.
      self->ui->render->setProperty("eidPresentationPending", false);
      self->m_PresentationPending = false;
    });
    // Reveal the main image promptly. The full-update completion still joins the native
    // thumbnail batch and the latest display serial; optional analysis starts afterwards.
    if(!request.clearBeforeReveal && self->IsCurrentViewRequest(request))
    {
      self->RT_UpdateNativeThumbnails(r, request);
      GUIInvoke::call(qApp, [self, request]() {
        if(!self || !self->IsCurrentViewRequest(request) ||
           self->m_Ctx.CurEvent() != request.eventId || self->m_PresentationPending)
          return;
        self->m_CompletedDisplayRequestSerial = request.serial;
        if(self->m_AnalysisPending)
          self->m_AnalysisTimer->start();
        self->CompletePresentationWaiters();
      });
    }
  }
  else
  {
    GUIInvoke::call(qApp, [self]() {
      if(self)
        self->CompletePresentationWaiters();
    });
  }
}

void TextureViewer::CaptureThumbnailWitnesses()
{
  // Explicit diagnostic readback only. Ordinary preview drawing never invokes this slot.
  if(qEnvironmentVariableIntValue("RENDERDOC_EID_TIMING") != 1 || !m_Ctx.IsCaptureLoaded())
    return;
  struct Witness
  {
    ResourcePreview *preview;
    ResourceId resource;
    QImage image;
  };
  std::vector<Witness> witnesses;
  const uint32_t eventId = m_Ctx.CurEvent();
  for(ThumbnailStrip *strip : {ui->outputThumbs, ui->inputThumbs})
    for(ResourcePreview *preview : strip->thumbs())
      if(preview->isActive() && preview->isVisible() && !preview->visibleRegion().isEmpty())
        witnesses.push_back({preview, preview->property("id").value<ResourceId>(), QImage()});
  m_Ctx.Replay().BlockInvoke([this, eventId, &witnesses](IReplayController *r) {
    if(r->GetCurrentEventID() != eventId)
      return;
    for(Witness &witness : witnesses)
    {
      auto it = m_NativeThumbnailOutputs.find(witness.preview);
      if(it == m_NativeThumbnailOutputs.end())
        continue;
      const auto dimensions = it->second->GetDimensions();
      if(dimensions.first <= 0 || dimensions.second <= 0 ||
         uint64_t(dimensions.first) * dimensions.second > 16 * 1024 * 1024)
        continue;
      const bytebuf bytes = it->second->ReadbackOutputTexture();
      if(bytes.size() != size_t(dimensions.first) * dimensions.second * 3)
        continue;
      witness.image = QImage(bytes.data(), dimensions.first, dimensions.second,
                             dimensions.first * 3, QImage::Format_RGB888)
                          .copy();
    }
  });
  QVariantList values;
  for(const Witness &witness : witnesses)
  {
    // i18n-ignore: opt-in pixel evidence fields, never user-facing labels.
    values.push_back(QVariantMap{
        {lit("resource"),    // i18n-ignore: diagnostic resource identity key.
         ToQStr(witness.resource)},
        {lit("event"), eventId},                           // i18n-ignore: diagnostic event ID key.
        {lit("image"), witness.image},                     // i18n-ignore: diagnostic image key.
        {lit("mip"), witness.preview->property("mip")},    // i18n-ignore: diagnostic mip key.
        {lit("slice"), witness.preview->property("slice")},    // i18n-ignore: diagnostic slice key.
        {lit("cast"), witness.preview->property("cast")}});    // i18n-ignore: diagnostic type-cast key.
  }
  // i18n-ignore: explicitly requested native-surface evidence, consumed only by regression fixtures.
  setProperty("replayThumbnailWitnesses", values);
}

void TextureViewer::RT_UpdateNativeThumbnails(IReplayController *r, const ViewRequest &request)
{
  for(const ViewRequest::ThumbnailBinding &binding : request.thumbnails)
  {
    if(!IsCurrentViewRequest(request) || r->GetCurrentEventID() != request.eventId || !binding.preview)
      return;
    IReplayOutput *&output = m_NativeThumbnailOutputs[binding.preview.data()];
    if(!output)
      output = r->CreateOutput(binding.window, ReplayOutputType::Texture);
    if(!output)
      continue;
    TextureDisplay display;
    display.resourceId = binding.resource;
    display.subresource = binding.subresource;
    display.typeCast = binding.typeCast;
    display.red = display.green = display.blue = true;
    display.alpha = false;
    display.hdrMultiplier = -1.0f;
    display.linearDisplayAsGamma = true;
    display.scale = -1.0f;
    display.backgroundColor = FloatVector(0.0f, 0.0f, 0.0f, 1.0f);
    if(binding.typeCast == CompType::SNorm)
      display.rangeMin = -1.0f;
    if(binding.depth)
      display.green = display.blue = false;
    output->SetTextureDisplay(display);
    output->Display();
  }
  QPointer<TextureViewer> self(this);
  GUIInvoke::call(this, [self, request]() {
    if(!self || !self->IsCurrentViewRequest(request))
      return;
    for(const ViewRequest::ThumbnailBinding &binding : request.thumbnails)
      if(binding.preview)
        binding.preview->MarkNativePresentation(request.eventSerial, binding.size);
  });
}

void TextureViewer::QueueDeferredAnalysis()
{
  if(!m_Ctx.IsCaptureLoaded() || !isVisible() || !m_Output)
    return;
  if(m_Ctx.IsScrubbing())
  {
    m_AnalysisTimer->start();
    return;
  }
  const ViewRequest request = CurrentViewRequest();
  QPointer<TextureViewer> self(this);
  m_AnalysisPending = false;
  m_Ctx.Replay().AsyncInvoke(
      // i18n-ignore: internal coalescing tag for optional idle-time texture analysis.
      lit("TextureViewerDeferredAnalysis"), [self, request](IReplayController *r) {
        if(!self || !self->IsCurrentViewRequest(request) || r->GetCurrentEventID() != request.eventId)
          return;
        self->RT_PickPixelsAndUpdate(r, request);
        self->RT_PickPixelsAndUpdate(r, request, true);
        self->RT_UpdateVisualRange(r, request);
      });
}

void TextureViewer::CancelReplayBackgroundWork()
{
  if(m_RenderTargetHistory)
    m_RenderTargetHistory->OnReplayInput();
}

void TextureViewer::QueuePickPixelsAndUpdate(bool hover)
{
  if(m_RetargetingRenderTargetHistory || m_UpdatingTextureState)
    return;
  QPointer<TextureViewer> self(this);
  const ViewRequest request = CurrentViewRequest();
  // i18n-ignore: internal replay-request coalescing tags, never shown to the user.
  m_Ctx.Replay().AsyncInvoke(hover ? lit("PickPixelHover") : lit("PickPixelClick"),
                             [self, request, hover](IReplayController *r) {
                               if(self)
                                 self->RT_PickPixelsAndUpdate(r, request, hover);
                             });
}

void TextureViewer::QueueUpdateVisualRange()
{
  if(m_RetargetingRenderTargetHistory || m_UpdatingTextureState)
    return;
  QPointer<TextureViewer> self(this);
  const ViewRequest request = CurrentViewRequest();
  // i18n-ignore: internal replay-request coalescing tag, never shown to the user.
  m_Ctx.Replay().AsyncInvoke(lit("TextureViewerHistogram"), [self, request](IReplayController *r) {
    if(self)
      self->RT_UpdateVisualRange(r, request);
  });
}

uint32_t TextureViewer::ActiveRenderTargetHistoryEvent() const
{
  // This accessor is used on the replay thread. Visibility is encoded by storing 0 when the
  // embedded panel is hidden, so no QWidget is ever touched outside the GUI thread.
  return m_RenderTargetHistoryEvent.load(std::memory_order_relaxed);
}

void TextureViewer::CloseAIAnnotation()
{
  ++m_AIAnnotationSerial;
  m_AIAnnotationStack->setCurrentIndex(0);
  if(m_AIAnnotation)
  {
    m_AIAnnotationStack->removeWidget(m_AIAnnotation);
    m_AIAnnotation->deleteLater();
    m_AIAnnotation.clear();
  }
}

bool TextureViewer::MatchesAIAnnotation(const QJsonObject &context) const
{
  const ViewRequest request = CurrentViewRequest();
  const QJsonObject capture = context.value(QLatin1String("capture")).toObject();
  const QJsonObject resource = context.value(QLatin1String("resource")).toObject();
  const QJsonObject annotation = context.value(QLatin1String("annotation")).toObject();
  return capture.value(QLatin1String("eventId")).toDouble() ==
             (request.historyEvent ? request.historyEvent : request.eventId) &&
         resource.value(QLatin1String("resourceId")).toString() ==
             ToQStr(request.display.resourceId) &&
         resource.value(QLatin1String("mip")).toDouble() == request.display.subresource.mip &&
         resource.value(QLatin1String("slice")).toDouble() == request.display.subresource.slice &&
         resource.value(QLatin1String("sample")).toDouble() == request.display.subresource.sample &&
         annotation.value(QLatin1String("flipY")).toBool() == request.display.flipY &&
         annotation.value(QLatin1String("rangeMin")).toDouble() == request.display.rangeMin &&
         annotation.value(QLatin1String("rangeMax")).toDouble() == request.display.rangeMax &&
         annotation.value(QLatin1String("channels")).toArray() ==
             QJsonArray({request.display.red, request.display.green, request.display.blue,
                         request.display.alpha}) &&
         annotation.value(QLatin1String("typeCast")).toString() == ToQStr(request.display.typeCast) &&
         annotation.value(QLatin1String("overlay")).toString() == ToQStr(request.display.overlay) &&
         annotation.value(QLatin1String("customShaderId")).toString() ==
             ToQStr(request.display.customShaderId) &&
         annotation.value(QLatin1String("decodeYUV")).toBool() == request.display.decodeYUV &&
         annotation.value(QLatin1String("linearDisplayAsGamma")).toBool() ==
             request.display.linearDisplayAsGamma;
}

void TextureViewer::EditAIAnnotation(
    const QImage &original, const QString &title, const QJsonObject &context,
    std::function<bool(const QImage &, const QImage &, const QJsonObject &)> accept)
{
  // An in-progress editor owns its unsaved strokes. Bring it forward instead of replacing it.
  if(m_AIAnnotation)
  {
    m_AIAnnotationStack->setCurrentWidget(m_AIAnnotation);
    m_AIAnnotation->setFocus();
    return;
  }
  ++m_AIAnnotationSerial;
  QRectF imageViewRect;
  if(MatchesAIAnnotation(context))
  {
    // TextureDisplay uses physical pixels and the full base-level extent, even for a mip.
    // The immutable AI snapshot can be downsampled; map its whole image onto the same
    // logical rectangle instead of fitting it to the smaller annotation canvas.
    const ViewRequest request = CurrentViewRequest();
    const qreal dpr = ui->render->devicePixelRatioF();
    imageViewRect = QRectF(
        QPointF(ui->render->mapToGlobal(QPoint())) +
            QPointF(request.display.xOffset / dpr, request.display.yOffset / dpr),
        QSizeF(request.texture.width, request.texture.height) * (request.display.scale / dpr));
  }
  QPointer<TextureViewer> self(this);
  m_AIAnnotation = new AITextureAnnotation(
      original, title, context, accept,
      [self]() {
        if(self)
          self->CloseAIAnnotation();
      },
      m_AIAnnotationStack);
  m_AIAnnotationStack->addWidget(m_AIAnnotation);
  m_AIAnnotationStack->setCurrentWidget(m_AIAnnotation);
  if(imageViewRect.isValid())
    m_AIAnnotation->SetImageViewRect(imageViewRect);
}

void TextureViewer::BeginAIAnnotation(
    const QJsonObject &captureBinding,
    std::function<bool(const QImage &, const QImage &, const QJsonObject &)> accept,
    std::function<void(const QString &)> error)
{
  if(m_AIAnnotation)
  {
    m_AIAnnotationStack->setCurrentWidget(m_AIAnnotation);
    m_AIAnnotation->setFocus();
    return;
  }
  const ViewRequest request = CurrentViewRequest();
  const uint32_t eventId = request.historyEvent ? request.historyEvent : request.eventId;
  if(!m_Ctx.IsCaptureLoaded() || request.texture.resourceId == ResourceId() ||
     request.display.resourceId == ResourceId() || eventId == 0)
  {
    error(tr("Select a render target in the Texture Viewer before drawing."));
    return;
  }
  const uint64_t serial = ++m_AIAnnotationSerial;
  const int mip = qMin(31U, request.display.subresource.mip);
  const QSize textureSize(qMax(1U, request.texture.width >> mip),
                          qMax(1U, request.texture.height >> mip));
  // The editor inherits the live viewport transform. Capture its source at one
  // texel per pixel so opening the brush never adds a second resampling step.
  const QSize imageSize = textureSize;
  if(qint64(imageSize.width()) * imageSize.height() > 64LL * 1024 * 1024)
  {
    error(tr("The render target image could not be read for annotation."));
    return;
  }
  QJsonObject context =
      ReplayLabAIResourceContext(m_Ctx, request.display.resourceId, "texture-viewer");
  QJsonObject capture = captureBinding;
  capture[QLatin1String("eventId")] = double(eventId);
  capture[QLatin1String("selectedEventId")] = double(m_Ctx.CurSelectedEvent());
  context[QLatin1String("capture")] = capture;
  QJsonObject origin = context.value(QLatin1String("origin")).toObject();
  origin[QLatin1String("kind")] = QLatin1String("rt-annotation");
  context[QLatin1String("origin")] = origin;
  QJsonObject resource = context.value(QLatin1String("resource")).toObject();
  resource[QLatin1String("mip")] = double(request.display.subresource.mip);
  resource[QLatin1String("slice")] = double(request.display.subresource.slice);
  resource[QLatin1String("sample")] = double(request.display.subresource.sample);
  context[QLatin1String("resource")] = resource;
  QJsonObject annotation;
  annotation[QLatin1String("schema")] = QLatin1String("replaylab-rt-annotation/v1");
  annotation[QLatin1String("pairId")] = QUuid::createUuid().toString().mid(1, 36);
  annotation[QLatin1String("coordinateSpace")] = QLatin1String("display-normalized-top-left");
  annotation[QLatin1String("imageWidth")] = imageSize.width();
  annotation[QLatin1String("imageHeight")] = imageSize.height();
  annotation[QLatin1String("textureWidth")] = textureSize.width();
  annotation[QLatin1String("textureHeight")] = textureSize.height();
  annotation[QLatin1String("flipY")] = request.display.flipY;
  annotation[QLatin1String("rangeMin")] = request.display.rangeMin;
  annotation[QLatin1String("rangeMax")] = request.display.rangeMax;
  annotation[QLatin1String("typeCast")] = ToQStr(request.display.typeCast);
  annotation[QLatin1String("channels")] = QJsonArray(
      {request.display.red, request.display.green, request.display.blue, request.display.alpha});
  annotation[QLatin1String("overlay")] = ToQStr(request.display.overlay);
  annotation[QLatin1String("customShaderId")] = ToQStr(request.display.customShaderId);
  annotation[QLatin1String("decodeYUV")] = request.display.decodeYUV;
  annotation[QLatin1String("linearDisplayAsGamma")] = request.display.linearDisplayAsGamma;
  context[QLatin1String("annotation")] = annotation;
  const QString title = tr("Annotate EID %1 | %2 | MIP %3, slice %4")
                            .arg(eventId)
                            .arg(m_Ctx.GetResourceName(request.display.resourceId))
                            .arg(request.display.subresource.mip)
                            .arg(request.display.subresource.slice);
  QPointer<TextureViewer> self(this);
  m_Ctx.Replay().AsyncInvoke([self, serial, request, eventId, imageSize, context, title, accept,
                              error](IReplayController *r) {
    QImage image;
    QString diagnostic;
    if(!self || !self->IsCurrentViewRequest(request) || r->GetCurrentEventID() != request.eventId)
      diagnostic = tr("The texture changed while preparing the annotation. Click the brush again.");
    else
    {
      TextureDisplay display = request.display;
      display.scale = 1.0f;
      display.xOffset = display.yOffset = 0;
      const uint64_t generation = r->GetShaderReplacementGeneration(0);
      IReplayOutput *output =
          r->CreateOutput(CreateHeadlessWindowingData(imageSize.width(), imageSize.height()),
                          ReplayOutputType::Texture);
      if(output)
      {
        output->DisplayTextureAtEvent(eventId, display);
        const bytebuf pixels = output->ReadbackOutputTexture();
        if(pixels.size() == uint64_t(imageSize.width()) * imageSize.height() * 3 &&
           r->GetShaderReplacementGeneration(0) == generation)
          image = QImage(pixels.data(), imageSize.width(), imageSize.height(),
                         imageSize.width() * 3, QImage::Format_RGB888)
                      .copy();
        output->Shutdown();
      }
      if(image.isNull())
        diagnostic = tr("The render target image could not be read for annotation.");
    }
    GUIInvoke::call(qApp, [self, serial, request, image, context, title, accept, error, diagnostic]() {
      if(!self || self->m_AIAnnotationSerial != serial)
        return;
      if(!diagnostic.isEmpty() || !self->IsCurrentViewRequest(request))
      {
        error(diagnostic.isEmpty()
                  ? tr("The texture changed while preparing the annotation. Click the brush again.")
                  : diagnostic);
        return;
      }
      self->EditAIAnnotation(image, title, context, accept);
    });
  });
}

void TextureViewer::RT_UpdateVisualRange(IReplayController *r, const ViewRequest &request)
{
  if(!IsCurrentViewRequest(request) || r->GetCurrentEventID() != request.eventId ||
     !request.visualise || request.texture.resourceId == ResourceId() || m_Output == NULL)
    return;

  TextureDisplay analysisDisplay;
  const bool haveSnapshot =
      ResolveTextureHistorySnapshot(r, request.historyEvent, request.display, analysisDisplay);
  ScopedTextureHistoryReplay historyReplay(r, haveSnapshot ? 0 : request.historyEvent);

  const TextureDescription &tex = request.texture;
  const TextureDisplay &display = request.display;

  ResourceFormat fmt = tex.format;

  bool uintTex = (tex.format.compType == CompType::UInt);
  bool sintTex = (tex.format.compType == CompType::SInt);

  if(tex.format.compType == CompType::Typeless && display.typeCast == CompType::UInt)
    uintTex = true;

  if(tex.format.compType == CompType::Typeless && display.typeCast == CompType::SInt)
    sintTex = true;

  if(display.customShaderId != ResourceId())
    fmt.compCount = 4;

  rdcfixedarray<bool, 4> channels = {
      display.red ? true : false,
      display.green && fmt.compCount > 1,
      display.blue && fmt.compCount > 2,
      display.alpha && fmt.compCount > 3,
  };

  ResourceId textureId = analysisDisplay.resourceId;
  Subresource sub = analysisDisplay.subresource;
  CompType typeCast = analysisDisplay.typeCast;

  if(analysisDisplay.customShaderId != ResourceId() &&
     m_Output->GetCustomShaderTexID() != ResourceId())
  {
    textureId = m_Output->GetCustomShaderTexID();
    sub.slice = sub.sample = 0;
    typeCast = CompType::Typeless;
  }

  PixelValue min, max;
  rdctie(min, max) = r->GetMinMax(textureId, sub, typeCast);

  // exclude any channels where the min == max, as this destroys the histogram's utility.
  // When we do this, after we have the histogram we set the appropriate bucket to max - to still
  // show that there was data there but it's "clamped".
  float excludedBucket[4] = {-1.0f, -1.0f, -1.0f, -1.0f};
  const float rangeSize = request.rangeMax - request.rangeMin;
  for(int i = 0; i < 4; i++)
  {
    if(min.uintValue[i] == max.uintValue[i])
    {
      channels[i] = false;

      if(uintTex)
        excludedBucket[i] = (float(min.uintValue[i]) - request.rangeMin) / rangeSize;
      else if(sintTex)
        excludedBucket[i] = (float(min.intValue[i]) - request.rangeMin) / rangeSize;
      else
        excludedBucket[i] = (min.floatValue[i] - request.rangeMin) / rangeSize;
    }
  }

  rdcarray<uint32_t> histogram =
      r->GetHistogram(textureId, sub, typeCast, request.rangeMin, request.rangeMax, channels);

  if(!histogram.empty())
  {
    QVector<uint32_t> histogramVec(histogram.count());
    if(!histogram.isEmpty())
      memcpy(histogramVec.data(), histogram.data(), histogram.byteSize());

    // if the histogram is completely empty we still want to set 1 value in there.
    uint32_t maxval = 1;
    for(const uint32_t &v : histogramVec)
      maxval = qMax(v, maxval);

    if(!histogramVec.isEmpty())
    {
      for(int i = 0; i < 4; i++)
      {
        int bucket = excludedBucket[i] * (histogramVec.size() - 1);
        if(bucket >= 0 && bucket < histogramVec.size())
          histogramVec[bucket] = maxval;
      }
    }

    QPointer<TextureViewer> self(this);
    GUIInvoke::call(this, [self, request, histogramVec]() {
      if(!self || !self->IsCurrentViewRequest(request) || !self->m_Visualise)
        return;
      self->ui->rangeHistogram->setHistogramRange(request.rangeMin, request.rangeMax);
      self->ui->rangeHistogram->setHistogramData(histogramVec);
    });
  }
}

void TextureViewer::UI_UpdateStatusText()
{
  TextureDescription *texptr = GetCurrentTexture();
  if(texptr == NULL)
    return;

  TextureDescription &tex = *texptr;

  CompType compType = tex.format.compType;

  const bool yuv =
      (tex.format.type == ResourceFormatType::YUV8 || tex.format.type == ResourceFormatType::YUV10 ||
       tex.format.type == ResourceFormatType::YUV12 || tex.format.type == ResourceFormatType::YUV16);

  if(tex.format.compType != m_TexDisplay.typeCast && m_TexDisplay.typeCast != CompType::Typeless &&
     !yuv)
    compType = m_TexDisplay.typeCast;

  bool dsv = (tex.creationFlags & TextureCategory::DepthTarget) || (compType == CompType::Depth) ||
             (tex.format.type == ResourceFormatType::S8);
  bool uintTex = (compType == CompType::UInt);
  bool sintTex = (compType == CompType::SInt);

  if(m_TexDisplay.overlay == DebugOverlay::QuadOverdrawPass ||
     m_TexDisplay.overlay == DebugOverlay::QuadOverdrawDraw ||
     m_TexDisplay.overlay == DebugOverlay::TriangleSizePass ||
     m_TexDisplay.overlay == DebugOverlay::TriangleSizeDraw)
  {
    dsv = false;
    uintTex = false;
    sintTex = false;
  }

  QColor swatchColor;

  if(dsv || uintTex || sintTex)
  {
    swatchColor = QColor(0, 0, 0);
  }
  else
  {
    float r = qBound(0.0f, m_CurHoverValue.floatValue[0], 1.0f);
    float g = qBound(0.0f, m_CurHoverValue.floatValue[1], 1.0f);
    float b = qBound(0.0f, m_CurHoverValue.floatValue[2], 1.0f);

    swatchColor = QColor(int(255.0f * r), int(255.0f * g), int(255.0f * b));
  }

  {
    QPalette Pal(palette());

    Pal.setColor(QPalette::Background, swatchColor);

    ui->pickSwatch->setAutoFillBackground(true);
    ui->pickSwatch->setPalette(Pal);
  }

  uint32_t mipWidth = qMax(1U, tex.width >> (int)m_TexDisplay.subresource.mip);
  uint32_t mipHeight = qMax(1U, tex.height >> (int)m_TexDisplay.subresource.mip);

  int x = MipCoordFromBase(m_CurHoverPixel.x(), tex.width);
  int y = MipCoordFromBase(m_CurHoverPixel.y(), tex.height);

  if(ShouldFlipForGL())
    y = (int)(mipHeight - 1) - y;
  if(m_TexDisplay.flipY)
    y = (int)(mipHeight - 1) - y;

  y = qMax(0, y);

  float invWidth = 1.0f / mipWidth;
  float invHeight = 1.0f / mipHeight;

  QString hoverCoords = QFormatStr("%1, %2 (%3, %4)")
                            .arg(x, 4)
                            .arg(y, 4)
                            .arg((x * invWidth), 5, 'f', 4)
                            .arg((y * invHeight), 5, 'f', 4);

  QString hoverText;

  uint32_t hoverX = (uint32_t)m_CurHoverPixel.x();
  uint32_t hoverY = (uint32_t)m_CurHoverPixel.y();

  if(hoverX > tex.width || hoverY > tex.height)
    hoverText = tr("Hover - [%1] - ").arg(hoverCoords);
  else
    hoverText = tr("Hover -  %1  - ").arg(hoverCoords);

  ui->hoverText->setText(hoverText);

  QString pickedText;
  QString pickedTooltip;

  if(m_PickedPoint.x() >= 0)
  {
    x = MipCoordFromBase(m_PickedPoint.x(), tex.width);
    y = MipCoordFromBase(m_PickedPoint.y(), tex.height);
    if(ShouldFlipForGL())
      y = (int)(mipHeight - 1) - y;
    if(m_TexDisplay.flipY)
      y = (int)(mipHeight - 1) - y;

    y = qMax(0, y);

    pickedText = tr("Right click - %1, %2: ").arg(x, 4).arg(y, 4);

    PixelValue val = m_CurPixelValue;

    if(m_TexDisplay.customShaderId != ResourceId())
    {
      pickedText += QFormatStr("%1, %2, %3, %4")
                        .arg(Formatter::Format(val.floatValue[0]))
                        .arg(Formatter::Format(val.floatValue[1]))
                        .arg(Formatter::Format(val.floatValue[2]))
                        .arg(Formatter::Format(val.floatValue[3]));

      val = m_CurRealValue;

      pickedText += tr(" (Real: ");
    }

    if(tex.format.type == ResourceFormatType::A8)
      val.floatValue[0] = val.floatValue[3];

    if(dsv)
    {
      pickedText += tr("Depth ");
      if(uintTex)
      {
        pickedText += Formatter::Format(val.uintValue[0]);
      }
      else
      {
        pickedText += Formatter::Format(val.floatValue[0]);
      }

      if(tex.format.type == ResourceFormatType::D16S8 ||
         tex.format.type == ResourceFormatType::D24S8 ||
         tex.format.type == ResourceFormatType::D32S8 || tex.format.type == ResourceFormatType::S8)
      {
        int stencil = (int)(255.0f * val.floatValue[1]);

        if(tex.format.type == ResourceFormatType::S8)
        {
          pickedText.clear();
          stencil = val.uintValue[0];
        }
        else
        {
          pickedText += lit(", ");
        }

        pickedText += tr("Stencil 0x%1").arg(Formatter::Format(uint8_t(stencil & 0xff), true));

        pickedTooltip = tr("Stencil: %1 / 0x%2 / 0b%3")
                            .arg(stencil, 3, 10, QLatin1Char(' '))
                            .arg(Formatter::Format(uint8_t(stencil & 0xff), true))
                            .arg(stencil, 8, 2, QLatin1Char('0'));
      }
    }
    else
    {
      // Restrict the number of components displayed to the component count of the resource format
      if(uintTex)
      {
        pickedText += QFormatStr("%1").arg(Formatter::Format(val.uintValue[0]));
        if(tex.format.compCount > 1)
          pickedText += QFormatStr(", %1").arg(Formatter::Format(val.uintValue[1]));
        if(tex.format.compCount > 2)
          pickedText += QFormatStr(", %1").arg(Formatter::Format(val.uintValue[2]));
        if(tex.format.compCount > 3)
          pickedText += QFormatStr(", %1").arg(Formatter::Format(val.uintValue[3]));
      }
      else if(sintTex)
      {
        pickedText += QFormatStr("%1").arg(Formatter::Format(val.intValue[0]));
        if(tex.format.compCount > 1)
          pickedText += QFormatStr(", %1").arg(Formatter::Format(val.intValue[1]));
        if(tex.format.compCount > 2)
          pickedText += QFormatStr(", %1").arg(Formatter::Format(val.intValue[2]));
        if(tex.format.compCount > 3)
          pickedText += QFormatStr(", %1").arg(Formatter::Format(val.intValue[3]));
      }
      else
      {
        pickedText += QFormatStr("%1").arg(Formatter::Format(val.floatValue[0]));
        if(tex.format.compCount > 1)
          pickedText += QFormatStr(", %1").arg(Formatter::Format(val.floatValue[1]));
        if(tex.format.compCount > 2)
          pickedText += QFormatStr(", %1").arg(Formatter::Format(val.floatValue[2]));
        if(tex.format.compCount > 3)
          pickedText += QFormatStr(", %1").arg(Formatter::Format(val.floatValue[3]));
      }
    }

    if(m_TexDisplay.customShaderId != ResourceId())
      pickedText += lit(")");
  }
  else
  {
    pickedText += tr("Right click to pick a pixel");
  }

  // try and keep status text consistent by sticking to the high water mark
  // of length (prevents nasty oscillation when the length of the string is
  // just popping over/under enough to overflow onto the next line).

  if(pickedText.length() > m_HighWaterStatusLength)
    m_HighWaterStatusLength = pickedText.length();

  if(pickedText.length() < m_HighWaterStatusLength)
    pickedText += QString(m_HighWaterStatusLength - pickedText.length(), QLatin1Char(' '));

  ui->pickedText->setText(pickedText);
  ui->pickedText->setToolTip(pickedTooltip);
}

void TextureViewer::UI_UpdateTextureDetails()
{
  QString status;

  TextureDescription *texptr = GetCurrentTexture();
  if(texptr == NULL)
  {
    ui->texStatusName->setText(status);
    ui->texStatusDim->setText(status);
    ui->texStatusFormat->setText(status);

    ui->renderContainer->setWindowTitle(tr("Unbound"));
    return;
  }

  TextureDescription &current = *texptr;

  ResourceId followID = m_Following.GetResourceId(m_Ctx);

  {
    TextureDescription *followtex = m_Ctx.GetTexture(followID);
    BufferDescription *followbuf = m_Ctx.GetBuffer(followID);

    QString title;

    if(followID == ResourceId())
    {
      title = tr("Unbound");
    }
    else if(followtex || followbuf)
    {
      QString name = m_Ctx.GetResourceName(followID);

      switch(m_Following.Type)
      {
        case FollowType::OutputColor:
          title = QString(tr("Cur Output %1 - %2")).arg(m_Following.index).arg(name);
          break;
        case FollowType::OutputDepth: title = QString(tr("Cur Depth Output - %1")).arg(name); break;
        case FollowType::ReadWrite:
          title = QString(tr("Cur RW Output %1 - %2")).arg(m_Following.index).arg(name);
          break;
        case FollowType::ReadOnly:
          title = QString(tr("Cur Input %1 - %2")).arg(m_Following.index).arg(name);
          break;
        case FollowType::OutputDepthResolve:
          title = QString(tr("Cur Depth Resolve Output - %1")).arg(name);
          break;
      }
    }
    else
    {
      switch(m_Following.Type)
      {
        case FollowType::OutputColor:
          title = QString(tr("Cur Output %1")).arg(m_Following.index);
          break;
        case FollowType::OutputDepth: title = QString(tr("Cur Depth Output")); break;
        case FollowType::ReadWrite:
          title = QString(tr("Cur RW Output %1")).arg(m_Following.index);
          break;
        case FollowType::ReadOnly:
          title = QString(tr("Cur Input %1")).arg(m_Following.index);
          break;
        case FollowType::OutputDepthResolve: title = QString(tr("Cur Depth Resolve Output")); break;
      }
    }

    if(m_CachedTexture == &m_TemporaryPreviewTexture && m_TemporaryPreviewActive)
      title = tr("Visual debug preview - RGBA32F");
    ui->renderContainer->setWindowTitle(title);
  }

  ui->texStatusName->setText((m_CachedTexture == &m_TemporaryPreviewTexture && m_TemporaryPreviewActive
                                ? tr("Visual debug preview - RGBA32F")
                                : QString(m_Ctx.GetResourceName(current.resourceId))) + lit(" - "));

  status = QString();

  if(current.dimension >= 1)
    status += QString::number(current.width);
  if(current.dimension >= 2)
    status += lit("x") + QString::number(current.height);
  if(current.dimension >= 3)
    status += lit("x") + QString::number(current.depth);

  if(current.arraysize > 1)
    status += QFormatStr("[%1]").arg(QString::number(current.arraysize));

  if(current.msSamp > 1)
  {
    // quality is only used by D3D, specify these here for simplicity
    if(current.msQual == 0xffffffff)
      status += QFormatStr(" MS %1x Std Pattern").arg(current.msSamp);
    else if(current.msQual == 0xfffffffe)
      status += QFormatStr(" MS %1x Cent Pattern").arg(current.msSamp);
    else if(current.msQual > 0)
      status += QFormatStr(" MS %1x %2 Quality").arg(current.msSamp).arg(current.msQual);
    else
      status += QFormatStr(" MS %1x").arg(current.msSamp);
  }

  status += QFormatStr(" %1 mips").arg(current.mips);

  status += lit(" - ");

  ui->texStatusDim->setText(status);

  status = current.format.Name();

  const bool yuv = (current.format.type == ResourceFormatType::YUV8 ||
                    current.format.type == ResourceFormatType::YUV10 ||
                    current.format.type == ResourceFormatType::YUV12 ||
                    current.format.type == ResourceFormatType::YUV16);

  CompType viewCast = CompType::Typeless;

  if(current.format.compType != m_TexDisplay.typeCast &&
     m_TexDisplay.typeCast != CompType::Typeless && !yuv)
  {
    viewCast = m_TexDisplay.typeCast;
  }
  else if(current.format.compType == CompType::Typeless &&
          m_TexDisplay.typeCast == CompType::Typeless && !yuv)
  {
    // if it's a typeless texture and we don't have a hint, ensure the user knows it's being viewed
    // as unorm as a fallback
    viewCast = CompType::UNorm;
  }

  if(viewCast != CompType::Typeless)
    status += tr(" Viewed as %1").arg(ToQStr(viewCast));

  ui->texStatusFormat->setText(status);
}

void TextureViewer::UI_OnTextureSelectionChanged(bool newAction)
{
  TextureDescription *texptr = GetCurrentTexture();

  // reset high-water mark
  m_HighWaterStatusLength = 0;

  if(texptr == NULL)
  {
    m_TexDisplay.resourceId = ResourceId();
    UI_UpdateTextureDetails();
    QueueUpdateAndDisplay();
    return;
  }

  const bool wasUpdating = m_UpdatingTextureState;
  QScopedValueRollback<bool> updating(m_UpdatingTextureState, true);

  TextureDescription &tex = *texptr;

  bool newtex = (m_TexDisplay.resourceId != tex.resourceId);

  // save settings for this current texture
  if(m_Ctx.Config().TextureViewer_PerTexSettings)
  {
    m_TextureSettings[m_TexDisplay.resourceId].r = ui->channelRed->isChecked();
    m_TextureSettings[m_TexDisplay.resourceId].g = ui->channelGreen->isChecked();
    m_TextureSettings[m_TexDisplay.resourceId].b = ui->channelBlue->isChecked();
    m_TextureSettings[m_TexDisplay.resourceId].a = ui->channelAlpha->isChecked();

    // save state regardless, we just don't apply it without the setting
    m_TextureSettings[m_TexDisplay.resourceId].flip_y = ui->flip_y->isChecked();

    m_TextureSettings[m_TexDisplay.resourceId].displayType = qMax(0, ui->channels->currentIndex());
    m_TextureSettings[m_TexDisplay.resourceId].customShader = ui->customShader->currentText();

    m_TextureSettings[m_TexDisplay.resourceId].depth = ui->depthDisplay->isChecked();
    m_TextureSettings[m_TexDisplay.resourceId].stencil = ui->stencilDisplay->isChecked();

    m_TextureSettings[m_TexDisplay.resourceId].mip = qMax(0, ui->mipLevel->currentIndex());
    m_TextureSettings[m_TexDisplay.resourceId].slice = qMax(0, ui->sliceFace->currentIndex());

    m_TextureSettings[m_TexDisplay.resourceId].minrange = ui->rangeHistogram->blackPoint();
    m_TextureSettings[m_TexDisplay.resourceId].maxrange = ui->rangeHistogram->whitePoint();

    if(m_TexDisplay.typeCast != CompType::Typeless)
      m_TextureSettings[m_TexDisplay.resourceId].typeCast = m_TexDisplay.typeCast;
  }

  m_TexDisplay.resourceId = tex.resourceId;

  // interpret the texture according to the currently following type.
  if(m_CachedTexture == &m_TemporaryPreviewTexture && m_TemporaryPreviewActive)
    m_TexDisplay.typeCast = CompType::Float;
  else if(!currentTextureIsLocked())
    m_TexDisplay.typeCast = m_Following.GetTypeHint(m_Ctx);
  else
    m_TexDisplay.typeCast = CompType::Typeless;

  // if there is no such type or it isn't being followed, use the last seen interpretation
  if(m_TexDisplay.typeCast == CompType::Typeless &&
     m_TextureSettings.contains(m_TexDisplay.resourceId))
    m_TexDisplay.typeCast = m_TextureSettings[m_TexDisplay.resourceId].typeCast;

  // try to maintain the pan in the new texture. If the new texture
  // is approx an integer multiple of the old texture, just changing
  // the scale will keep everything the same. This is useful for
  // downsample chains and things where you're flipping back and forth
  // between overlapping textures, but even in the non-integer case
  // pan will be kept approximately the same.
  QSizeF curSize((float)tex.width, (float)tex.height);
  float curArea = area(curSize);
  float prevArea = area(m_PrevSize);

  if(prevArea > 0.0f && m_PrevSize.width() > 0.0f)
  {
    float prevX = m_TexDisplay.xOffset;
    float prevY = m_TexDisplay.yOffset;

    // allow slight difference in aspect ratio for rounding errors
    // in downscales (e.g. 1680x1050 -> 840x525 -> 420x262 in the
    // last downscale the ratios are 1.6 and 1.603053435).
    if(qAbs(aspect(curSize) - aspect(m_PrevSize)) < 0.01f)
    {
      m_TexDisplay.scale *= m_PrevSize.width() / curSize.width();
      setCurrentZoomValue(m_TexDisplay.scale);
    }
    else
    {
      // this scale factor is arbitrary really, only intention is to have
      // integer scales come out precisely, other 'similar' sizes will be
      // similar ish
      float scaleFactor = (float)(sqrt(curArea) / sqrt(prevArea));

      m_TexDisplay.xOffset = prevX * scaleFactor;
      m_TexDisplay.yOffset = prevY * scaleFactor;
    }
  }

  m_PrevSize = curSize;

  // refresh scroll position
  UI_CalcScrollbars();
  setScrollPosition(getScrollPosition());

  UI_UpdateStatusText();

  // block signals for mipLevel and sliceFace comboboxes while editing them
  ui->mipLevel->blockSignals(true);
  ui->sliceFace->blockSignals(true);

  ui->mipLevel->clear();

  m_TexDisplay.subresource.mip = 0;
  m_TexDisplay.subresource.slice = 0;
  m_TexDisplay.subresource.sample = 0;

  bool usemipsettings = true;
  bool useslicesettings = true;

  if(tex.msSamp > 1)
  {
    for(uint32_t i = 0; i < tex.msSamp; i++)
      ui->mipLevel->addItem(tr("Sample %1").arg(i));

    // add an option to display unweighted average resolved value,
    // to get an idea of how the samples average
    if(CanAverageTextureSamples(tex, m_TexDisplay.typeCast))
      ui->mipLevel->addItem(tr("Average val"));

    ui->mipLabel->setText(tr("Sample"));

    ui->mipLevel->setCurrentIndex(0);
  }
  else
  {
    for(uint32_t i = 0; i < tex.mips; i++)
      ui->mipLevel->addItem(
          QFormatStr("%1 - %2x%3").arg(i).arg(qMax(1U, tex.width >> i)).arg(qMax(1U, tex.height >> i)));

    ui->mipLabel->setText(tr("Mip"));
  }

  if(tex.mips == 1 && tex.msSamp <= 1)
    ui->mipLevel->setEnabled(false);
  else
    ui->mipLevel->setEnabled(true);

  ui->sliceFace->clear();

  uint32_t numSlices = 1;

  if(tex.arraysize == 1 && tex.depth <= 1)
  {
    ui->sliceFace->setEnabled(false);
  }
  else
  {
    ui->sliceFace->setEnabled(true);

    QString cubeFaces[] = {lit("X+"), lit("X-"), lit("Y+"), lit("Y-"), lit("Z+"), lit("Z-")};

    numSlices = tex.arraysize;

    // for 3D textures, display the number of slices at this mip
    if(tex.depth > 1)
      numSlices = qMax(1u, tex.depth >> (int)ui->mipLevel->currentIndex());

    for(uint32_t i = 0; i < numSlices; i++)
    {
      if(tex.cubemap)
      {
        QString name = cubeFaces[i % 6];
        if(numSlices > 6)
          name = QFormatStr("[%1] %2").arg(i / 6).arg(
              cubeFaces[i % 6]);    // Front 1, Back 2, 3, 4 etc for cube arrays
        ui->sliceFace->addItem(name);
      }
      else
      {
        ui->sliceFace->addItem(tr("Slice %1").arg(i));
      }
    }
  }

  // enable signals for mipLevel and sliceFace
  ui->mipLevel->blockSignals(false);
  ui->sliceFace->blockSignals(false);

  {
    int highestMip = -1;

    // only switch to the selected mip for outputs, and when changing action
    if(!currentTextureIsLocked() && m_Following.Type != FollowType::ReadOnly && (newAction || newtex))
      highestMip = m_Following.GetHighestMip(m_Ctx);

    // assuming we get a valid mip for the highest mip, only switch to it
    // if we've selected a new texture, or if it's different than the last mip.
    // This prevents the case where the user has clicked on another mip and
    // we don't want to snap their view back when stepping between events with the
    // same mip used. But it does mean that if they are stepping between
    // events with different mips used, then we will update in that case.
    if(highestMip >= 0 && (newtex || highestMip != m_PrevHighestMip))
    {
      usemipsettings = false;
      ui->mipLevel->setCurrentIndex(qBound(0, highestMip, (int)tex.mips - 1));
    }

    if(ui->mipLevel->currentIndex() == -1)
      ui->mipLevel->setCurrentIndex(qBound(0, m_PrevHighestMip, (int)tex.mips - 1));

    m_PrevHighestMip = highestMip;
  }

  {
    int firstArraySlice = -1;
    // only switch to the selected mip for outputs, and when changing action
    if(!currentTextureIsLocked() && m_Following.Type != FollowType::ReadOnly && (newAction || newtex))
      firstArraySlice = m_Following.GetFirstArraySlice(m_Ctx);

    // see above with highestMip and prevHighestMip for the logic behind this
    if(firstArraySlice >= 0 && (newtex || firstArraySlice != m_PrevFirstArraySlice))
    {
      useslicesettings = false;
      ui->sliceFace->setCurrentIndex(qBound(0, firstArraySlice, (int)numSlices - 1));
    }

    if(ui->sliceFace->currentIndex() == -1)
      ui->sliceFace->setCurrentIndex(qBound(0, m_PrevFirstArraySlice, (int)numSlices - 1));

    m_PrevFirstArraySlice = firstArraySlice;
  }

  // because slice and mip are specially set above, we restore any per-tex settings to apply
  // even if we don't switch to a new texture.
  // Note that if the slice or mip was changed because that slice or mip is the selected one
  // at the API level, we leave this alone.
  if(m_Ctx.Config().TextureViewer_PerTexSettings && m_TextureSettings.contains(tex.resourceId))
  {
    if(usemipsettings)
      ui->mipLevel->setCurrentIndex(m_TextureSettings[tex.resourceId].mip);

    if(useslicesettings)
      ui->sliceFace->setCurrentIndex(m_TextureSettings[tex.resourceId].slice);

    if(m_Ctx.Config().TextureViewer_PerTexYFlip)
      ui->flip_y->setChecked(m_TextureSettings[tex.resourceId].flip_y);
  }

  // handling for if we've switched to a new texture
  if(newtex)
  {
    // if we save certain settings per-texture, restore them (if we have any)
    if(m_Ctx.Config().TextureViewer_PerTexSettings && m_TextureSettings.contains(tex.resourceId))
    {
      ui->channels->setCurrentIndex(m_TextureSettings[tex.resourceId].displayType);

      ui->customShader->setCurrentText(m_TextureSettings[tex.resourceId].customShader);

      ui->channelRed->setChecked(m_TextureSettings[tex.resourceId].r);
      ui->channelGreen->setChecked(m_TextureSettings[tex.resourceId].g);
      ui->channelBlue->setChecked(m_TextureSettings[tex.resourceId].b);
      ui->channelAlpha->setChecked(m_TextureSettings[tex.resourceId].a);

      ui->depthDisplay->setChecked(m_TextureSettings[tex.resourceId].depth);
      ui->stencilDisplay->setChecked(m_TextureSettings[tex.resourceId].stencil);

      if(m_Ctx.Config().TextureViewer_PerTexYFlip)
        ui->flip_y->setChecked(m_TextureSettings[tex.resourceId].flip_y);

      m_NoRangePaint = true;
      ui->rangeHistogram->setRange(m_TextureSettings[m_TexDisplay.resourceId].minrange,
                                   m_TextureSettings[m_TexDisplay.resourceId].maxrange);
      m_NoRangePaint = false;
    }
    else if(m_Ctx.Config().TextureViewer_PerTexSettings)
    {
      // if we are using per-tex settings, reset back to RGB
      ui->channels->setCurrentIndex(0);

      ui->customShader->setCurrentText(QString());

      ui->channelRed->setChecked(true);
      ui->channelGreen->setChecked(true);
      ui->channelBlue->setChecked(true);
      ui->channelAlpha->setChecked(false);

      // for alpha textures, only show the alpha channel
      if(texptr->format.type == ResourceFormatType::A8)
      {
        ui->channelRed->setChecked(false);
        ui->channelGreen->setChecked(false);
        ui->channelBlue->setChecked(false);
        ui->channelAlpha->setChecked(true);
      }

      ui->depthDisplay->setChecked(true);
      ui->stencilDisplay->setChecked(false);

      if(m_Ctx.Config().TextureViewer_PerTexYFlip)
        ui->flip_y->setChecked(false);

      m_NoRangePaint = true;
      UI_SetHistogramRange(texptr, m_TexDisplay.typeCast);
      m_NoRangePaint = false;
    }

    ui->depthDisplay->setEnabled(true);

    if(tex.format.type == ResourceFormatType::S8)
    {
      ui->depthDisplay->setEnabled(false);
      ui->depthDisplay->setChecked(false);
      ui->stencilDisplay->setChecked(true);
    }

    // reset the range if desired
    if(m_Ctx.Config().TextureViewer_ResetRange)
    {
      UI_SetHistogramRange(texptr, m_TexDisplay.typeCast);
    }
  }

  UI_UpdateFittedScale();
  UI_UpdateTextureDetails();
  UI_UpdateChannels();

  m_UpdatingTextureState = wasUpdating;
  QueueUpdateAndDisplay(true);
  if(ui->autoFit->isChecked())
    AutoFitRange();

  HighlightUsage();
}

void TextureViewer::UI_SetHistogramRange(const TextureDescription *tex, CompType typeCast)
{
  if(tex != NULL && (tex->format.compType == CompType::SNorm || typeCast == CompType::SNorm))
    ui->rangeHistogram->setRange(-1.0f, 1.0f);
  else
    ui->rangeHistogram->setRange(0.0f, 1.0f);
}

void TextureViewer::UI_UpdateChannels()
{
  TextureDescription *tex = GetCurrentTexture();

#define SHOW(widget) widget->setVisible(true)
#define HIDE(widget) widget->setVisible(false)
#define ENABLE(widget) widget->setEnabled(true)
#define DISABLE(widget) widget->setEnabled(false)

  if(tex != NULL && (tex->creationFlags & TextureCategory::SwapBuffer))
  {
    // swapbuffer is always srgb for 8-bit types, linear for 16-bit types
    DISABLE(ui->gammaDisplay);

    if(tex->format.compByteWidth == 2 && tex->format.type == ResourceFormatType::Regular)
      m_TexDisplay.linearDisplayAsGamma = false;
    else
      m_TexDisplay.linearDisplayAsGamma = true;
  }
  else
  {
    if(tex != NULL && !tex->format.SRGBCorrected())
      ENABLE(ui->gammaDisplay);
    else
      DISABLE(ui->gammaDisplay);

    m_TexDisplay.linearDisplayAsGamma =
        !ui->gammaDisplay->isEnabled() || ui->gammaDisplay->isChecked();
  }

  if(tex != NULL && tex->format.SRGBCorrected())
    m_TexDisplay.linearDisplayAsGamma = false;

  bool dsv = false;
  if(tex != NULL)
    dsv = (tex->creationFlags & TextureCategory::DepthTarget) ||
          (tex->format.compType == CompType::Depth);

  bool yuv = false;
  if(tex != NULL)
    yuv = (tex->format.type == ResourceFormatType::YUV8 ||
           tex->format.type == ResourceFormatType::YUV10 ||
           tex->format.type == ResourceFormatType::YUV12 ||
           tex->format.type == ResourceFormatType::YUV16);

  const bool defaultDisplay = ui->channels->currentIndex() == 0;
  const bool rgbmDisplay = ui->channels->currentIndex() == 1;
  const bool yuvDecodeDisplay = ui->channels->currentIndex() == 2;
  const bool customDisplay = ui->channels->currentIndex() == 3;

  if(customDisplay && m_NeedCustomReload)
  {
    m_NeedCustomReload = false;

    reloadCustomShaders(QString());
  }

  ui->channels->setItemText(0, yuv ? lit("YUVA") : lit("RGBA"));
  ui->channelRed->setText(lit("R"));
  ui->channelGreen->setText(lit("G"));
  ui->channelBlue->setText(lit("B"));

  if(dsv && !customDisplay)
  {
    // Depth display (when not using custom)

    HIDE(ui->channelRed);
    HIDE(ui->channelGreen);
    HIDE(ui->channelBlue);
    HIDE(ui->channelAlpha);
    HIDE(ui->mulSep);
    HIDE(ui->mulLabel);
    HIDE(ui->hdrMul);
    HIDE(ui->customShader);
    HIDE(ui->customCreate);
    HIDE(ui->customEdit);
    HIDE(ui->customDelete);
    SHOW(ui->depthDisplay);
    SHOW(ui->stencilDisplay);

    if(tex != NULL && tex->format.type == ResourceFormatType::S8)
      HIDE(ui->depthDisplay);

    m_TexDisplay.red = ui->depthDisplay->isChecked();
    m_TexDisplay.green = ui->stencilDisplay->isChecked();
    m_TexDisplay.blue = false;
    m_TexDisplay.alpha = false;

    if(m_TexDisplay.red == m_TexDisplay.green && !m_TexDisplay.red)
    {
      m_TexDisplay.red = true;
      ui->depthDisplay->setChecked(true);
    }

    m_TexDisplay.decodeYUV = false;
    m_TexDisplay.hdrMultiplier = -1.0f;
    if(m_TexDisplay.customShaderId != ResourceId())
    {
      m_CurPixelValue.floatValue = {0.0f, 0.0f, 0.0f, 0.0f};
      m_CurRealValue.floatValue = {0.0f, 0.0f, 0.0f, 0.0f};
      UI_UpdateStatusText();
    }
    m_TexDisplay.customShaderId = ResourceId();
  }
  else if(defaultDisplay || yuvDecodeDisplay || !m_Ctx.IsCaptureLoaded())
  {
    // RGBA. YUV Decode is almost identical but we set decodeYUV
    SHOW(ui->channelRed);
    SHOW(ui->channelGreen);
    SHOW(ui->channelBlue);
    SHOW(ui->channelAlpha);
    HIDE(ui->mulSep);
    HIDE(ui->mulLabel);
    HIDE(ui->hdrMul);
    HIDE(ui->customShader);
    HIDE(ui->customCreate);
    HIDE(ui->customEdit);
    HIDE(ui->customDelete);
    HIDE(ui->depthDisplay);
    HIDE(ui->stencilDisplay);

    m_TexDisplay.red = ui->channelRed->isChecked();
    m_TexDisplay.green = ui->channelGreen->isChecked();
    m_TexDisplay.blue = ui->channelBlue->isChecked();
    m_TexDisplay.alpha = ui->channelAlpha->isChecked();

    if(!yuvDecodeDisplay && yuv)
    {
      ui->channelRed->setText(lit("V"));
      ui->channelGreen->setText(lit("Y"));
      ui->channelBlue->setText(lit("U"));
    }

    m_TexDisplay.decodeYUV = yuvDecodeDisplay;
    m_TexDisplay.hdrMultiplier = -1.0f;
    if(m_TexDisplay.customShaderId != ResourceId())
    {
      m_CurPixelValue.floatValue = {0.0f, 0.0f, 0.0f, 0.0f};
      m_CurRealValue.floatValue = {0.0f, 0.0f, 0.0f, 0.0f};
      UI_UpdateStatusText();
    }
    m_TexDisplay.customShaderId = ResourceId();
  }
  else if(rgbmDisplay)
  {
    // RGBM
    SHOW(ui->channelRed);
    SHOW(ui->channelGreen);
    SHOW(ui->channelBlue);
    HIDE(ui->channelAlpha);
    SHOW(ui->mulSep);
    SHOW(ui->mulLabel);
    SHOW(ui->hdrMul);
    HIDE(ui->customShader);
    HIDE(ui->customCreate);
    HIDE(ui->customEdit);
    HIDE(ui->customDelete);
    HIDE(ui->depthDisplay);
    HIDE(ui->stencilDisplay);

    m_TexDisplay.red = ui->channelRed->isChecked();
    m_TexDisplay.green = ui->channelGreen->isChecked();
    m_TexDisplay.blue = ui->channelBlue->isChecked();
    m_TexDisplay.alpha = false;

    bool ok = false;
    float mul = ui->hdrMul->currentText().toFloat(&ok);

    if(!ok)
    {
      mul = 32.0f;
      ui->hdrMul->setCurrentText(lit("32"));
    }

    m_TexDisplay.decodeYUV = false;
    m_TexDisplay.hdrMultiplier = mul;
    if(m_TexDisplay.customShaderId != ResourceId())
    {
      m_CurPixelValue.floatValue = {0.0f, 0.0f, 0.0f, 0.0f};
      m_CurRealValue.floatValue = {0.0f, 0.0f, 0.0f, 0.0f};
      UI_UpdateStatusText();
    }
    m_TexDisplay.customShaderId = ResourceId();
  }
  else if(customDisplay)
  {
    // custom shaders
    SHOW(ui->channelRed);
    SHOW(ui->channelGreen);
    SHOW(ui->channelBlue);
    SHOW(ui->channelAlpha);
    HIDE(ui->mulSep);
    HIDE(ui->mulLabel);
    HIDE(ui->hdrMul);
    SHOW(ui->customShader);
    SHOW(ui->customCreate);
    SHOW(ui->customEdit);
    SHOW(ui->customDelete);
    HIDE(ui->depthDisplay);
    HIDE(ui->stencilDisplay);

    m_TexDisplay.red = ui->channelRed->isChecked();
    m_TexDisplay.green = ui->channelGreen->isChecked();
    m_TexDisplay.blue = ui->channelBlue->isChecked();
    m_TexDisplay.alpha = ui->channelAlpha->isChecked();

    m_TexDisplay.decodeYUV = false;
    m_TexDisplay.hdrMultiplier = -1.0f;

    m_TexDisplay.customShaderId = ResourceId();

    QString shaderName = ui->customShader->currentText().toUpper();

    if(m_CustomShaders.contains(shaderName))
    {
      if(m_TexDisplay.customShaderId == ResourceId())
      {
        m_CurPixelValue.floatValue = {0.0f, 0.0f, 0.0f, 0.0f};
        m_CurRealValue.floatValue = {0.0f, 0.0f, 0.0f, 0.0f};
        UI_UpdateStatusText();
      }
      m_TexDisplay.customShaderId = m_CustomShaders[shaderName];
      ui->customDelete->setEnabled(true);
      ui->customEdit->setEnabled(true);
    }
    else
    {
      ui->customDelete->setEnabled(false);
      ui->customEdit->setEnabled(false);
    }
  }

#undef HIDE
#undef SHOW
#undef ENABLE
#undef DISABLE

  m_TexDisplay.flipY = ui->flip_y->isChecked();
  setProperty("textureDisplayFlipY", m_TexDisplay.flipY);

  QueueUpdateAndDisplay();
  QueueUpdateVisualRange();
  UI_UpdateStatusText();
}

void TextureViewer::SetupTextureTabs()
{
  ToolWindowManagerArea *textureTabs = ui->dockarea->areaOf(ui->renderContainer);

  if(!textureTabs)
    return;

  QIcon tabIcon;
  tabIcon.addFile(QStringLiteral(":/logo.svg"), QSize(), QIcon::Normal, QIcon::Off);

  textureTabs->setTabIcon(0, tabIcon);

  textureTabs->setElideMode(Qt::ElideRight);

  QObject::connect(textureTabs, &QTabWidget::currentChanged, this,
                   &TextureViewer::textureTab_Changed);
  QObject::connect(textureTabs, &QTabWidget::tabCloseRequested, this,
                   &TextureViewer::textureTab_Closing);

  textureTabs->disableUserDrop();

  textureTabs->tabBar()->setContextMenuPolicy(Qt::CustomContextMenu);

  QObject::connect(textureTabs->tabBar(), &QTabBar::customContextMenuRequested, this,
                   &TextureViewer::textureTab_Menu);

  // show any fixed panels that got closed by previous bugs and saved as closed

  if(ui->dockarea->areaOf(ui->inputThumbs) == NULL)
    ui->dockarea->moveToolWindow(
        ui->inputThumbs,
        ToolWindowManager::AreaReference(ToolWindowManager::RightOf,
                                         ui->dockarea->areaOf(ui->renderContainer), 0.25f));

  if(ui->dockarea->areaOf(ui->outputThumbs) == NULL)
    ui->dockarea->moveToolWindow(
        ui->outputThumbs, ToolWindowManager::AreaReference(ToolWindowManager::AddTo,
                                                           ui->dockarea->areaOf(ui->inputThumbs)));

  QWidget *resourcePanels[] = {ui->inputThumbs, ui->outputThumbs};
  for(QWidget *panel : resourcePanels)
  {
    ToolWindowManagerArea *area = ui->dockarea->areaOf(panel);
    if(area)
    {
      // i18n-ignore: internal QObject property identifier, never shown to the user.
      area->tabBar()->setProperty("_renderdoc_ultra_bright_selected_tabs", true);
      area->tabBar()->update();
    }
  }

  if(ui->dockarea->areaOf(ui->pixelContextLayout) == NULL)
    ui->dockarea->moveToolWindow(
        ui->pixelContextLayout,
        ToolWindowManager::AreaReference(ToolWindowManager::BottomOf,
                                         ui->dockarea->areaOf(ui->outputThumbs), 0.25f));

  ui->renderContainer->setLayout(ui->renderLayout);
}

QWidget *TextureViewer::CurrentTextureTab() const
{
  ToolWindowManagerArea *textureTabs = ui->dockarea->areaOf(ui->renderContainer);
  return textureTabs ? textureTabs->currentWidget() : NULL;
}

bool TextureViewer::HasRenderTargetHistoryBinding(QWidget *tab) const
{
  if(!tab)
    return false;

  const RenderTargetHistoryPolicy::TabBinding binding = {
      tab->property(RenderTargetHistoryEnabledProperty).toBool(),
      tab->property(RenderTargetHistoryEventProperty).toUInt(),
  };
  return RenderTargetHistoryPolicy::ResolveTabPresentation(tab == ui->renderContainer, &binding)
      .visible;
}

void TextureViewer::ActivateRenderTargetHistoryForTab(QWidget *tab, uint32_t fallbackEvent)
{
  m_RenderTargetHistoryEvent = 0;

  if(!m_RenderTargetHistory)
    return;

  const RenderTargetHistoryPolicy::TabBinding binding = {
      tab && tab->property(RenderTargetHistoryEnabledProperty).toBool(),
      tab ? tab->property(RenderTargetHistoryEventProperty).toUInt() : 0,
  };
  const RenderTargetHistoryPolicy::TabPresentation presentation =
      RenderTargetHistoryPolicy::ResolveTabPresentation(tab == ui->renderContainer, &binding);

  if(!presentation.visible || !tab)
  {
    m_RenderTargetHistory->Deactivate();
    return;
  }

  const ResourceId resource = tab->property("id").value<ResourceId>();
  if(resource == ResourceId() || resource != m_TexDisplay.resourceId)
  {
    m_RenderTargetHistory->Deactivate();
    return;
  }

  uint32_t anchorEvent = presentation.activeEvent;
  if(anchorEvent == 0)
    anchorEvent = fallbackEvent;

  const bool wasRetargetingHistory = m_RetargetingRenderTargetHistory;
  m_RetargetingRenderTargetHistory = true;
  m_RenderTargetHistory->show();
  m_RenderTargetHistory->SetTarget(resource, m_TexDisplay.subresource, m_TexDisplay.typeCast,
                                   anchorEvent);

  const uint32_t selectedEvent = m_RenderTargetHistory->SelectedEvent();
  if(selectedEvent != 0)
    tab->setProperty(RenderTargetHistoryEventProperty, selectedEvent);
  m_RenderTargetHistoryEvent = selectedEvent;
  m_RetargetingRenderTargetHistory = wasRetargetingHistory;

  if(!wasRetargetingHistory && selectedEvent != 0)
    QueueUpdateAndDisplay();
}

void TextureViewer::CloseTextureTab(QWidget *tab)
{
  if(!tab || tab == ui->renderContainer)
    return;

  const ResourceId id = tab->property("id").value<ResourceId>();
  if(id != ResourceId())
    m_LockedTabs.remove(id);

  tab->setProperty(RenderTargetHistoryEnabledProperty, false);
  tab->setProperty(RenderTargetHistoryEventProperty, QVariant());
  ui->dockarea->removeToolWindow(tab);
}

void TextureViewer::RemoveTextureTabs(int firstIndex)
{
  ToolWindowManagerArea *textureTabs = ui->dockarea->areaOf(ui->renderContainer);

  // remove all tabs from firstIndex, except unclosable tabs
  for(int i = firstIndex; i < textureTabs->count();)
  {
    if(ui->dockarea->toolWindowProperties(textureTabs->widget(i)) & ToolWindowManager::HideCloseButton)
    {
      i++;
      continue;
    }
    CloseTextureTab(textureTabs->widget(i));
  }
}

void TextureViewer::textureTab_Menu(const QPoint &pos)
{
  ToolWindowManagerArea *textureTabs = ui->dockarea->areaOf(ui->renderContainer);

  int tabIndex = textureTabs->tabBar()->tabAt(pos);

  if(tabIndex == -1)
    return;

  QAction closeTab(tr("Close tab"), this);
  QAction closeOtherTabs(tr("Close other tabs"), this);
  QAction closeRightTabs(tr("Close tabs to the right"), this);

  if(ui->dockarea->toolWindowProperties(textureTabs->widget(tabIndex)) &
     ToolWindowManager::HideCloseButton)
    closeTab.setEnabled(false);

  QMenu contextMenu(this);

  contextMenu.addAction(&closeTab);
  contextMenu.addAction(&closeOtherTabs);
  contextMenu.addAction(&closeRightTabs);

  QWidget *selectedTab = textureTabs->widget(tabIndex);
  QObject::connect(&closeTab, &QAction::triggered,
                   [this, selectedTab]() { CloseTextureTab(selectedTab); });

  QObject::connect(&closeRightTabs, &QAction::triggered,
                   [this, tabIndex]() { RemoveTextureTabs(tabIndex + 1); });

  QObject::connect(&closeOtherTabs, &QAction::triggered, [this]() { RemoveTextureTabs(0); });

  RDDialog::show(&contextMenu, QCursor::pos());
}

void TextureViewer::textureTab_Changed(int index)
{
  if(!m_UpdatingTemporaryOutputFollow)
    m_TemporaryOutputFollowToken = 0;
  ToolWindowManagerArea *textureTabs = ui->dockarea->areaOf(ui->renderContainer);

  if(!textureTabs)
    return;

  QWidget *w = textureTabs->widget(index);

  SuspendTexturePresentation();
  const bool wasRetargetingHistory = m_RetargetingRenderTargetHistory;
  QScopedValueRollback<bool> retargeting(m_RetargetingRenderTargetHistory, true);

  // History presentation belongs to an explicitly linked locked tab. Clear the old transient
  // event before rebuilding the shared texture controls for the newly selected tab.
  m_RenderTargetHistoryEvent = 0;
  if(m_RenderTargetHistory)
    m_RenderTargetHistory->Deactivate();

  if(w)
  {
    w->setLayout(ui->renderLayout);

    if(w == ui->renderContainer)
      m_LockedId = ResourceId();
    else
      m_LockedId = w->property("id").value<ResourceId>();

    UI_UpdateCachedTexture();
  }

  UI_OnTextureSelectionChanged(false);

  // OpenRenderTargetHistory performs one explicit activation after it has attached the binding to
  // a newly-created tab. Ordinary user tab switches restore that tab's own selected EID here.
  if(!wasRetargetingHistory)
    ActivateRenderTargetHistoryForTab(w, m_Ctx.CurEvent());

  // Queue the final tab identity once. In particular, entering a history tab must not race an
  // intermediate current-event display scheduled before its historical binding was restored.
  m_RetargetingRenderTargetHistory = wasRetargetingHistory;
  QueueUpdateAndDisplay(true);
  if(ui->autoFit->isChecked())
    AutoFitRange();
}

void TextureViewer::textureTab_Closing(int index)
{
  ToolWindowManagerArea *textureTabs = ui->dockarea->areaOf(ui->renderContainer);
  if(index > 0)
  {
    // this callback happens AFTER the widget has already been removed unfortunately, so
    // we need to search through the locked tab list to see which one was removed to be
    // able to delete it properly.
    QList<ResourceId> ids = m_LockedTabs.keys();
    for(int i = 0; i < textureTabs->count(); i++)
    {
      QWidget *w = textureTabs->widget(i);
      ResourceId id = w->property("id").value<ResourceId>();
      ids.removeOne(id);
    }

    if(ids.count() != 1)
      qWarning() << "Expected only one removed tab, got " << ids.count();

    for(ResourceId id : ids)
      m_LockedTabs.remove(id);

    return;
  }

  // should never get here - tab 0 is the dynamic tab which is uncloseable.
  qCritical() << "Somehow closing dynamic tab?";
  if(textureTabs->count() > 1)
  {
    textureTabs->setCurrentIndex(0);
    textureTabs->widget(0)->show();
  }
}

ResourcePreview *TextureViewer::UI_CreateThumbnail(ThumbnailStrip *strip)
{
  ResourcePreview *prev = new ResourcePreview(m_Ctx, m_Output);

  QObject::connect(prev, &ResourcePreview::clicked, this, &TextureViewer::thumb_clicked);
  QObject::connect(prev, &ResourcePreview::doubleClicked, this, &TextureViewer::thumb_doubleClicked);
  QObject::connect(prev, &ResourcePreview::resourceLinkActivated, this,
                   &TextureViewer::thumb_resourceLinkActivated);
  QObject::connect(prev, &ResourcePreview::resized, this, &TextureViewer::UI_PreviewResized);

  prev->setActive(false);
  strip->addThumb(prev);
  return prev;
}

void TextureViewer::UI_CreateThumbnails()
{
  if(!ui->outputThumbs->thumbs().isEmpty())
    return;

  // these will expand, but we make sure that there is a good set reserved
  for(int i = 0; i < 9; i++)
  {
    ResourcePreview *prev = UI_CreateThumbnail(ui->outputThumbs);

    if(i == 0)
      prev->setSelected(true);
  }

  for(int i = 0; i < 128; i++)
    UI_CreateThumbnail(ui->inputThumbs);
}

void TextureViewer::ViewTexture(ResourceId ID, CompType typeCast, bool focus)
{
  if(QThread::currentThread() != QCoreApplication::instance()->thread())
  {
    GUIInvoke::call(this, [this, ID, typeCast, focus] { this->ViewTexture(ID, typeCast, focus); });
    return;
  }

  if(!m_UpdatingTemporaryOutputFollow)
    m_TemporaryOutputFollowToken = 0;

  if(typeCast != CompType::Typeless)
    m_TextureSettings[ID].typeCast = typeCast;

  if(m_LockedTabs.contains(ID))
  {
    QWidget *w = m_LockedTabs[ID];
    ToolWindowManagerArea *textureTabs = ui->dockarea->areaOf(ui->renderContainer);
    const int idx = textureTabs ? textureTabs->indexOf(w) : -1;

    if(idx >= 0)
    {
      if(focus)
        ToolWindowManager::raiseToolWindow(this);
      textureTabs->setCurrentIndex(idx);

      QueueUpdateAndDisplay();
      return;
    }

    // Context-menu tab removal in older layouts could leave a detached widget in this map. Drop
    // the stale entry and create a fresh locked tab below.
    m_LockedTabs.remove(ID);
  }

  TextureDescription *tex = m_Ctx.GetTexture(ID);
  if(tex)
  {
    QWidget *lockedContainer = new QWidget(this);
    lockedContainer->setWindowTitle(m_Ctx.GetResourceName(ID));
    lockedContainer->setProperty("id", QVariant::fromValue(ID));

    ToolWindowManagerArea *textureTabs = ui->dockarea->areaOf(ui->renderContainer);

    ToolWindowManager::AreaReference ref(ToolWindowManager::AddTo, textureTabs);

    ui->dockarea->addToolWindow(lockedContainer, ref);
    ui->dockarea->setToolWindowProperties(
        lockedContainer,
        ToolWindowManager::DisallowUserDocking | ToolWindowManager::AlwaysDisplayFullTabs);

    lockedContainer->setLayout(ui->renderLayout);

    int idx = textureTabs->indexOf(lockedContainer);

    if(idx >= 0)
      textureTabs->setTabIcon(idx, Icons::page_go());
    else
      qCritical() << "Couldn't get tab index of new tab to set icon";

    // newPanel.DockHandler.TabPageContextMenuStrip = tabContextMenu;

    if(focus)
      ToolWindowManager::raiseToolWindow(this);

    m_LockedTabs[ID] = lockedContainer;

    QueueUpdateAndDisplay();
    return;
  }

  BufferDescription *buf = m_Ctx.GetBuffer(ID);
  if(buf)
  {
    IBufferViewer *viewer = m_Ctx.ViewBuffer(0, ~0ULL, ID);

    m_Ctx.AddDockWindow(viewer->Widget(), DockReference::AddTo, this);
  }
}

void TextureViewer::ViewFollowedResource(FollowType followType, ShaderStage stage, int32_t index,
                                         int32_t arrayElement)
{
  Following f;
  f.Type = followType;
  f.Stage = stage;
  f.index = index;
  f.arrayEl = arrayElement;

  if(f.Type == FollowType::OutputColor || f.Type == FollowType::OutputDepth)
  {
    f.Stage = ShaderStage::Pixel;
    f.arrayEl = 0;
  }

  if(f.Type == FollowType::OutputDepth || f.Type == FollowType::OutputDepthResolve)
    f.index = 0;

  for(ResourcePreview *p :
      (f.Type == FollowType::ReadOnly ? ui->inputThumbs->thumbs() : ui->outputThumbs->thumbs()))
  {
    Following follow = p->property("f").value<Following>();

    if(follow == f)
    {
      SelectPreview(p);

      ToolWindowManagerArea *textureTabs = ui->dockarea->areaOf(ui->renderContainer);

      // following tab is always tab 0.
      textureTabs->setCurrentIndex(0);

      return;
    }
  }

  qWarning() << "Couldn't find matching bound resource";
}

bool TextureViewer::FollowBoundTexture(ResourceId id, const QString &attachmentKind)
{
  static const QMap<QString, FollowType> kinds = {
      {QLatin1String("srv"), FollowType::ReadOnly},
      {QLatin1String("uav"), FollowType::ReadWrite},
      {QLatin1String("dsv"), FollowType::OutputDepth},
      {QLatin1String("dsr"), FollowType::OutputDepthResolve},
      {QLatin1String("rtv"), FollowType::OutputColor},
      {QLatin1String("present"), FollowType::OutputColor},
  };
  const auto kind = kinds.constFind(attachmentKind);
  if(!attachmentKind.isEmpty() && kind == kinds.constEnd())
    return false;
  ResourcePreview *selection = nullptr;
  for(const auto &thumbs : {ui->outputThumbs->thumbs(), ui->inputThumbs->thumbs()})
  {
    for(ResourcePreview *preview : thumbs)
    {
      if(!preview->isActive() || preview->property("id").value<ResourceId>() != id)
        continue;
      const Following follow = preview->property("f").value<Following>();
      if(kind != kinds.constEnd() && follow.Type != *kind)
        continue;
      if(!selection || follow == m_Following)
        selection = preview;
    }
  }
  ToolWindowManagerArea *tabs = ui->dockarea->areaOf(ui->renderContainer);
  if(!selection || !tabs)
    return false;
  SelectPreview(selection);
  tabs->setCurrentWidget(ui->renderContainer);
  return GetCurrentResource() == id;
}

uint64_t TextureViewer::BeginTemporaryOutputFollow(uint32_t index)
{
  if(m_TemporaryOutputFollowToken != 0 || !m_Ctx.IsCaptureLoaded())
    return 0;

  m_BeforeTemporaryOutputFollow = m_Following;
  m_BeforeTemporaryOutputTab = CurrentTextureTab();
  m_BeforeTemporaryOutputResource = m_TexDisplay.resourceId;
  m_BeforeTemporaryOutputType = m_TexDisplay.typeCast;
  m_BeforeTemporaryOutputSubresource = m_TexDisplay.subresource;
  const uint64_t token = ++m_NextTemporaryOutputFollowToken;
  m_TemporaryOutputFollowToken = token;
  if(!UpdateTemporaryOutputFollow(token, index))
  {
    m_TemporaryOutputFollowToken = 0;
    return 0;
  }
  ToolWindowManager::raiseToolWindow(this);
  return token;
}

bool TextureViewer::UpdateTemporaryOutputFollow(uint64_t token, uint32_t index)
{
  if(token == 0 || token != m_TemporaryOutputFollowToken || !m_Ctx.IsCaptureLoaded())
    return false;
  QScopedValueRollback<bool> changing(m_UpdatingTemporaryOutputFollow, true);
  m_TemporaryPreviewActive = false;
  ViewFollowedResource(FollowType::OutputColor, ShaderStage::Pixel, index, 0);
  return CurrentTextureTab() == ui->renderContainer && m_Following.Type == FollowType::OutputColor &&
         m_Following.index >= 0 && uint32_t(m_Following.index) == index;
}

bool TextureViewer::ShowTemporaryTexturePreview(uint64_t token, const TextureDescription &texture)
{
  if(token == 0 || token != m_TemporaryOutputFollowToken || !m_Ctx.IsCaptureLoaded() ||
     texture.resourceId == ResourceId() || texture.width == 0 || texture.height == 0 ||
     texture.format.compCount != 4 || texture.format.compByteWidth != 4 ||
     texture.format.compType != CompType::Float || CurrentTextureTab() != ui->renderContainer)
    return false;
  QScopedValueRollback<bool> changing(m_UpdatingTemporaryOutputFollow, true);
  m_TemporaryPreviewTexture = texture;
  m_TemporaryPreviewActive = true;
  m_TemporaryPreviewEvent = m_Ctx.CurEvent();
  UI_UpdateCachedTexture();
  UI_OnTextureSelectionChanged(false);
  m_TexDisplay.typeCast = CompType::Float;
  SetSelectedSubresource(Subresource());
  UI_UpdateTextureDetails();
  QueueUpdateAndDisplay();
  return true;
}

bool TextureViewer::EndTemporaryOutputFollow(uint64_t token)
{
  if(token == 0 || token != m_TemporaryOutputFollowToken || !m_Ctx.IsCaptureLoaded())
    return false;
  m_TemporaryOutputFollowToken = 0;
  m_TemporaryPreviewActive = false;
  QScopedValueRollback<bool> changing(m_UpdatingTemporaryOutputFollow, true);
  ToolWindowManagerArea *textureTabs = ui->dockarea->areaOf(ui->renderContainer);
  if(!textureTabs || !m_BeforeTemporaryOutputTab ||
     textureTabs->indexOf(m_BeforeTemporaryOutputTab) < 0)
  {
    // The user may close the saved tab while the temporary dynamic view is
    // active. Detach its descriptor even though that tab cannot be restored.
    UI_UpdateCachedTexture();
    UI_OnTextureSelectionChanged(false);
    return false;
  }

  m_Following = m_BeforeTemporaryOutputFollow;
  UI_RememberOutputFollowing();
  for(ResourcePreview *preview : ui->outputThumbs->thumbs())
    preview->setSelected(preview->property("f").value<Following>() == m_Following);
  for(ResourcePreview *preview : ui->inputThumbs->thumbs())
    preview->setSelected(preview->property("f").value<Following>() == m_Following);
  textureTabs->setCurrentWidget(m_BeforeTemporaryOutputTab);
  UI_UpdateCachedTexture();
  UI_OnTextureSelectionChanged(false);
  if(m_TexDisplay.resourceId == m_BeforeTemporaryOutputResource)
  {
    m_TexDisplay.typeCast = m_BeforeTemporaryOutputType;
    m_TextureSettings[m_TexDisplay.resourceId].typeCast = m_BeforeTemporaryOutputType;
    SetSelectedSubresource(m_BeforeTemporaryOutputSubresource);
    UI_UpdateTextureDetails();
    QueueUpdateAndDisplay();
  }
  return true;
}

ResourceId TextureViewer::GetCurrentResource()
{
  TextureDescription *tex = GetCurrentTexture();
  if(tex)
    return tex->resourceId;
  return ResourceId();
}

Subresource TextureViewer::GetSelectedSubresource()
{
  return m_TexDisplay.subresource;
}

void TextureViewer::SetSelectedSubresource(Subresource sub)
{
  TextureDescription *tex = GetCurrentTexture();
  if(!tex)
    return;

  if(tex->mips > 1)
  {
    m_TexDisplay.subresource.mip = qMin(sub.mip, tex->mips - 1);
    m_TexDisplay.subresource.sample = 0;
    ui->mipLevel->setCurrentIndex((int)m_TexDisplay.subresource.mip);
  }
  else if(tex->msSamp > 1)
  {
    if(sub.sample == TextureDisplay::ResolveSamples && ui->mipLevel->count() > (int)tex->msSamp)
    {
      m_TexDisplay.subresource.sample = TextureDisplay::ResolveSamples;
      ui->mipLevel->setCurrentIndex((int)tex->msSamp);
    }
    else if(sub.sample == TextureDisplay::ResolveSamples)
    {
      // Integer and depth multisample textures have no average/resolve entry. Sample 0 is the
      // stable default; clamping ~0U would incorrectly select the last physical sample.
      m_TexDisplay.subresource.sample = 0;
      ui->mipLevel->setCurrentIndex(0);
    }
    else
    {
      m_TexDisplay.subresource.sample = qMin(sub.sample, tex->msSamp - 1);
      ui->mipLevel->setCurrentIndex((int)m_TexDisplay.subresource.sample);
    }
    m_TexDisplay.subresource.mip = 0;
  }
  else
  {
    m_TexDisplay.subresource.mip = 0;
    m_TexDisplay.subresource.sample = 0;
  }

  const uint32_t slices = tex->depth > 1
                              ? qMax(1U, tex->depth >> m_TexDisplay.subresource.mip)
                              : qMax(1U, tex->arraysize);
  m_TexDisplay.subresource.slice = qMin(sub.slice, slices - 1);
  if(tex->depth > 1)
    ui->sliceFace->setCurrentIndex((int)m_TexDisplay.subresource.slice);
  else
    ui->sliceFace->setCurrentIndex((int)m_TexDisplay.subresource.slice);
}

void TextureViewer::GotoLocation(uint32_t x, uint32_t y)
{
  if(!m_Ctx.IsCaptureLoaded())
    return;

  TextureDescription *tex = GetCurrentTexture();

  if(tex == NULL)
    return;

  x = qMin(BaseCoordFromMip(x, tex->width), uint32_t(tex->width - 1));
  y = qMin(BaseCoordFromMip(y, tex->height), uint32_t(tex->height - 1));

  m_PickedPoint = QPoint(x, y);

  if(ShouldFlipForGL())
    m_PickedPoint.setY((int)(tex->height - 1) - m_PickedPoint.y());
  if(m_TexDisplay.flipY)
    m_PickedPoint.setY((int)(tex->height - 1) - m_PickedPoint.y());

  // centre the picked point.
  QPoint scrollPos;
  scrollPos.setX(-m_PickedPoint.x() * m_TexDisplay.scale + realRenderWidth() / 2);
  scrollPos.setY(-m_PickedPoint.y() * m_TexDisplay.scale + realRenderHeight() / 2);

  setScrollPosition(scrollPos);

  if(m_Output != NULL)
    QueuePickPixelsAndUpdate();
  QueueUpdateAndDisplay();

  UI_UpdateStatusText();
}

DebugOverlay TextureViewer::GetTextureOverlay()
{
  return m_TexDisplay.overlay;
}

void TextureViewer::SetTextureOverlay(DebugOverlay overlay)
{
  ui->overlay->setCurrentIndex((int)overlay);
}

bool TextureViewer::IsZoomAutoFit()
{
  return ui->fitToWindow->isChecked();
}

float TextureViewer::GetZoomLevel()
{
  if(ui->fitToWindow->isChecked())
    return m_TexDisplay.scale;

  QString zoomText = ui->zoomOption->currentText().replace(QLatin1Char('%'), QLatin1Char(' '));

  bool ok = false;
  int zoom = zoomText.toInt(&ok);

  if(!ok)
    zoom = 100;

  return (float)(zoom) / 100.0f;
}

void TextureViewer::SetZoomLevel(bool autofit, float zoom)
{
  ui->fitToWindow->setChecked(autofit);
  if(!autofit)
    UI_SetScale(zoom);
}

rdcpair<float, float> TextureViewer::GetHistogramRange()
{
  return {m_TexDisplay.rangeMin, m_TexDisplay.rangeMax};
}

void TextureViewer::SetHistogramRange(float blackpoint, float whitepoint)
{
  ui->rangeHistogram->setRange(blackpoint, whitepoint);
}

uint32_t TextureViewer::GetChannelVisibilityBits()
{
  uint32_t ret = 0;
  if(m_TexDisplay.red)
    ret |= 0x1;
  if(m_TexDisplay.green)
    ret |= 0x2;
  if(m_TexDisplay.blue)
    ret |= 0x4;
  if(m_TexDisplay.alpha)
    ret |= 0x8;
  return ret;
}

void TextureViewer::SetFlipY(bool flipped)
{
  ui->flip_y->setChecked(flipped);
}

rdcfixedarray<bool, 4> TextureViewer::GetChannelVisibility()
{
  return {m_TexDisplay.red, m_TexDisplay.green, m_TexDisplay.blue, m_TexDisplay.alpha};
}

void TextureViewer::SetChannelVisibility(bool red, bool green, bool blue, bool alpha)
{
  ui->channelRed->setChecked(red);
  ui->channelGreen->setChecked(green);
  ui->channelBlue->setChecked(blue);
  ui->channelAlpha->setChecked(alpha);
}

RenderTargetHistoryView *TextureViewer::OpenRenderTargetHistory(
    ResourceId resource, const Subresource &subresource, CompType typeCast, uint32_t anchorEID)
{
  if(resource == ResourceId() || !m_Ctx.GetTexture(resource))
    return NULL;

  SuspendTexturePresentation();
  const bool wasRetargetingHistory = m_RetargetingRenderTargetHistory;
  m_RetargetingRenderTargetHistory = true;

  // Stop transient event display before ViewTexture can synchronously refresh an existing tab.
  m_RenderTargetHistoryEvent = 0;
  ViewTexture(resource, typeCast, true);

  ToolWindowManagerArea *textureTabs = ui->dockarea->areaOf(ui->renderContainer);
  QWidget *historyTab = m_LockedTabs.value(resource, NULL);
  if(!textureTabs || !historyTab || textureTabs->indexOf(historyTab) < 0)
  {
    if(m_RenderTargetHistory)
      m_RenderTargetHistory->Deactivate();
    m_RetargetingRenderTargetHistory = wasRetargetingHistory;
    // The shared surface was suspended above. Complete an ordinary display even if docking could
    // not produce the requested history tab; never reveal the previous historical backbuffer.
    QueueUpdateAndDisplay(true);
    return NULL;
  }

  historyTab->setProperty(RenderTargetHistoryEnabledProperty, true);
  historyTab->setProperty(RenderTargetHistoryEventProperty, anchorEID);

  // Selecting the already-current locked tab does not emit currentChanged. Apply a new descriptor
  // interpretation explicitly so the main image and MSAA sample controls match the clicked link.
  if(typeCast != CompType::Typeless && m_TexDisplay.typeCast != typeCast)
  {
    m_TextureSettings[resource].typeCast = typeCast;
    m_TexDisplay.typeCast = typeCast;
    UI_OnTextureSelectionChanged(false);
  }
  SetSelectedSubresource(subresource);
  m_RetargetingRenderTargetHistory = wasRetargetingHistory;

  if(!m_RenderTargetHistory)
  {
    m_RenderTargetHistory = new RenderTargetHistoryView(*this, m_Ctx, m_MainHistorySplitter);
    m_MainHistorySplitter->addWidget(m_RenderTargetHistory);
    m_MainHistorySplitter->setStretchFactor(0, 1);
    m_MainHistorySplitter->setStretchFactor(1, 0);
  }

  ActivateRenderTargetHistoryForTab(historyTab, anchorEID);
  if(ActiveRenderTargetHistoryEvent() == 0 && !wasRetargetingHistory)
    QueueUpdateAndDisplay();
  if(ui->autoFit->isChecked())
    AutoFitRange();

  const int totalHeight = qMax(height(), 400);
  m_MainHistorySplitter->setSizes({qMax(180, totalHeight - 230), 230});
  ToolWindowManager::raiseToolWindow(this);
  return m_RenderTargetHistory;
}

void TextureViewer::CloseRenderTargetHistory()
{
  if(!m_RenderTargetHistory)
    return;

  SuspendTexturePresentation();
  QWidget *historyTab = CurrentTextureTab();
  if(HasRenderTargetHistoryBinding(historyTab))
  {
    historyTab->setProperty(RenderTargetHistoryEnabledProperty, false);
    historyTab->setProperty(RenderTargetHistoryEventProperty, QVariant());
  }

  m_RenderTargetHistoryEvent = 0;
  m_RenderTargetHistory->Deactivate();
  QueueUpdateAndDisplay();
}

void TextureViewer::RenderTargetHistorySelectionChanged(uint32_t eventId)
{
  QWidget *historyTab = CurrentTextureTab();
  if(!m_RenderTargetHistory || !m_RenderTargetHistory->isVisible() ||
     !HasRenderTargetHistoryBinding(historyTab))
    return;

  const ResourceId resource = historyTab->property("id").value<ResourceId>();
  if(resource == ResourceId() || m_RenderTargetHistory->Resource() != resource)
    return;

  historyTab->setProperty(RenderTargetHistoryEventProperty, eventId);
  const uint32_t previous = m_RenderTargetHistoryEvent.exchange(eventId);
  if(previous != eventId && !m_RetargetingRenderTargetHistory)
    QueueUpdateAndDisplay();
}

void TextureViewer::UpdateRenderTargetHistorySubresource()
{
  QWidget *historyTab = CurrentTextureTab();
  if(m_RetargetingRenderTargetHistory || !m_RenderTargetHistory ||
     !m_RenderTargetHistory->isVisible() || !HasRenderTargetHistoryBinding(historyTab) ||
     m_TexDisplay.resourceId == ResourceId())
    return;

  const ResourceId resource = historyTab->property("id").value<ResourceId>();
  if(resource != m_TexDisplay.resourceId)
    return;

  uint32_t anchor = historyTab->property(RenderTargetHistoryEventProperty).toUInt();
  if(anchor == 0)
    anchor = m_Ctx.CurEvent();

  const bool wasRetargetingHistory = m_RetargetingRenderTargetHistory;
  m_RetargetingRenderTargetHistory = true;
  m_RenderTargetHistoryEvent = 0;
  m_RenderTargetHistory->SetTarget(m_TexDisplay.resourceId, m_TexDisplay.subresource,
                                   m_TexDisplay.typeCast, anchor);
  const uint32_t selectedEvent = m_RenderTargetHistory->SelectedEvent();
  if(selectedEvent != 0)
    historyTab->setProperty(RenderTargetHistoryEventProperty, selectedEvent);
  m_RenderTargetHistoryEvent = selectedEvent;
  m_RetargetingRenderTargetHistory = wasRetargetingHistory;
}

void TextureViewer::texContextItem_triggered()
{
  QAction *act = qobject_cast<QAction *>(QObject::sender());

  QVariant eid = act->property("eid");
  if(eid.isValid())
  {
    // Resource-usage menu entries only navigate the static capture. Use the snapshot-aware path;
    // a viewer which later asks for mutable resource contents will materialise the event lazily.
    m_Ctx.ScrubToEvent(eid.toUInt());
    m_Ctx.EndScrub();
    return;
  }

  QVariant idvar = act->property("id");
  if(idvar.isValid())
  {
    ResourceId id = idvar.value<ResourceId>();
    CompType typeCast = CompType::Typeless;
    if(m_TextureSettings.contains(id))
      typeCast = m_TextureSettings[id].typeCast;

    ViewTexture(id, typeCast, false);
    return;
  }
}

void TextureViewer::AddResourceUsageEntry(QMenu &menu, uint32_t start, uint32_t end,
                                          ResourceUsage usage)
{
  QAction *item = NULL;

  if(start == end)
    item = new QAction(
        QFormatStr("EID %1: %2").arg(start).arg(ToQStr(usage, m_Ctx.APIProps().pipelineType)), this);
  else
    item = new QAction(
        QFormatStr("EID %1-%2: %3").arg(start).arg(end).arg(ToQStr(usage, m_Ctx.APIProps().pipelineType)),
        this);

  QObject::connect(item, &QAction::triggered, this, &TextureViewer::texContextItem_triggered);
  item->setProperty("eid", QVariant(end));

  if(start <= m_Ctx.CurEvent() && m_Ctx.CurEvent() <= end)
    item->setIcon(Icons::flag_green());

  menu.addAction(item);
}

void TextureViewer::OpenResourceContextMenu(ResourceId id, bool input,
                                            const rdcarray<EventUsage> &usage)
{
  QMenu contextMenu(this);

  QAction openLockedTab(tr("Open new Locked Tab"), this);
  QAction openResourceInspector(tr("Open in Resource Inspector"), this);
  QAction usageTitle(tr("Used:"), this);
  QAction imageLayout(this);

  openLockedTab.setIcon(Icons::action_hover());
  openResourceInspector.setIcon(Icons::link());

  if(m_Ctx.CurPipelineState().SupportsBarriers())
  {
    imageLayout.setText(tr("Image is in layout ") + m_Ctx.CurPipelineState().GetResourceLayout(id));
    contextMenu.addAction(&imageLayout);
    contextMenu.addSeparator();
  }

  if(id != ResourceId())
  {
    contextMenu.addAction(&openLockedTab);
    contextMenu.addAction(&openResourceInspector);

    QJsonObject attachment = ReplayLabAIResourceContext(m_Ctx, id, "texture-viewer");
    if(id == m_TexDisplay.resourceId)
    {
      QJsonObject resource = attachment[QLatin1String("resource")].toObject();
      resource[QLatin1String("mip")] = int(m_TexDisplay.subresource.mip);
      resource[QLatin1String("slice")] = int(m_TexDisplay.subresource.slice);
      if(m_TexDisplay.subresource.sample != ~0U)
        resource[QLatin1String("sample")] = int(m_TexDisplay.subresource.sample);
      attachment[QLatin1String("resource")] = resource;
    }
    const QString title = tr("Texture: %1 (EID %2)").arg(m_Ctx.GetResourceName(id)).arg(m_Ctx.CurEvent());
    const QString json = ReplayLabAIResourceJSON(attachment);
    const QString text = ReplayLabAIResourceJSON(ReplayLabAIResourceDetails(m_Ctx, id, attachment));
    QAction *addToChat = contextMenu.addAction(tr("Add texture to AI conversation"));
    addToChat->setObjectName(QStringLiteral("addTextureToAI"));
    QObject::connect(addToChat, &QAction::triggered, [this, title, text, json]() {
      m_Ctx.AddTextToAIAssistant(title, text, title, json);
    });

    contextMenu.addSeparator();
    m_Ctx.Extensions().MenuDisplaying(input ? ContextMenu::TextureViewer_InputThumbnail
                                            : ContextMenu::TextureViewer_OutputThumbnail,
                                      &contextMenu, {{"resourceId", id}});

    contextMenu.addSeparator();
    contextMenu.addAction(&usageTitle);

    openLockedTab.setProperty("id", QVariant::fromValue(id));

    QObject::connect(&openLockedTab, &QAction::triggered, this,
                     &TextureViewer::texContextItem_triggered);

    QObject::connect(&openResourceInspector, &QAction::triggered, [this, id]() {
      m_Ctx.ShowResourceInspector();

      m_Ctx.GetResourceInspector()->Inspect(id);
    });

    CombineUsageEvents(m_Ctx, usage, false,
                       [this, &contextMenu](uint32_t start, uint32_t end, ResourceUsage use) {
                         AddResourceUsageEntry(contextMenu, start, end, use);
                       });

    RDDialog::show(&contextMenu, QCursor::pos());
  }
  else
  {
    m_Ctx.Extensions().MenuDisplaying(input ? ContextMenu::TextureViewer_InputThumbnail
                                            : ContextMenu::TextureViewer_OutputThumbnail,
                                      &contextMenu, {});

    RDDialog::show(&contextMenu, QCursor::pos());
  }
}

void TextureViewer::InitResourcePreview(ResourcePreview *prev, Descriptor res, bool force,
                                        Following &follow, const QString &bindName,
                                        const QString &slotName)
{
  Subresource sub = {0, 0, ~0U};
  if(res.resource != ResourceId() || force)
  {
    QString fullname = bindName;
    if(!m_Ctx.IsAutogeneratedName(res.resource))
    {
      if(!fullname.isEmpty())
        fullname += lit(" = ");
      fullname += m_Ctx.GetResourceName(res.resource);
    }
    if(fullname.isEmpty())
      fullname = m_Ctx.GetResourceName(res.resource);

    prev->setResourceName(fullname);

    prev->setProperty("f", QVariant::fromValue(follow));
    prev->setSlotName(slotName);
    prev->setActive(true);
    prev->setSelected(m_Following == follow);

    if(m_Ctx.GetTexture(res.resource))
    {
      prev->setResourceLinkEnabled(true, tr("Open Render Target History"));
      if(res.firstMip >= 0)
        sub.mip = res.firstMip;
      if(res.firstSlice >= 0)
        sub.slice = res.firstSlice;
    }
    else
    {
      prev->setResourceLinkEnabled(false);
      res.resource = ResourceId();
      res.format = ResourceFormat();
    }
  }
  else if(m_Following == follow)
  {
    prev->setResourceName(tr("Unused"));
    prev->setResourceLinkEnabled(false);
    prev->setActive(true);
    prev->setSelected(true);

    res.resource = ResourceId();
    res.format = ResourceFormat();
  }
  else
  {
    prev->setResourceName(QString());
    prev->setResourceLinkEnabled(false);
    prev->setActive(false);
    prev->setSelected(false);

    return;
  }

  if(prev->property("id").value<ResourceId>() != res.resource ||
     prev->property("mip").toUInt() != sub.mip || prev->property("slice").toUInt() != sub.slice ||
     prev->property("cast").toUInt() != uint32_t(res.format.compType))
    prev->ClearThumb();

  prev->setProperty("id", QVariant::fromValue(res.resource));
  prev->setProperty("mip", sub.mip);
  prev->setProperty("slice", sub.slice);
  prev->setProperty("cast", uint32_t(res.format.compType));

  UI_PreviewResized(prev);
}

void TextureViewer::UI_PreviewResized(ResourcePreview *prev)
{
  if(!prev->isActive() || !prev->isVisible() || m_UpdatingTextureState || !m_Output)
    return;
  if(prev->NativePresentationMatches(m_EventRequestSerial.load(std::memory_order_acquire),
                                     prev->GetThumbSize() * prev->devicePixelRatioF()))
    return;
  QueueNativeThumbnailRefresh();
}

void TextureViewer::InitStageResourcePreviews(ShaderStage stage,
                                              const rdcarray<ShaderResource> &shaderInterface,
                                              const rdcarray<UsedDescriptor> &descriptors,
                                              ThumbnailStrip *prevs, int &prevIndex, bool copy,
                                              bool rw)
{
  for(const UsedDescriptor &desc : descriptors)
  {
    Following follow(*this, rw ? FollowType::ReadWrite : FollowType::ReadOnly, desc.access.stage,
                     desc.access.index, desc.access.arrayElement);

    // show if it's referenced by the shader - regardless of empty or not
    bool show = !desc.access.staticallyUnused || copy;

    // omit buffers even if the shader uses them, unless this is a copy
    if(desc.descriptor.resource != ResourceId() && m_Ctx.GetTexture(desc.descriptor.resource) == NULL)
      show = copy;

    // it's the one we're following
    show = show || (follow == m_Following);

    ResourcePreview *prev = NULL;

    if(prevIndex < prevs->thumbs().size())
    {
      prev = prevs->thumbs()[prevIndex];

      // don't use it if we're not actually going to show it
      if(!show && !prev->isActive())
        continue;
    }
    else
    {
      // don't create it if we're not actually going to show it
      if(!show)
        continue;

      prev = UI_CreateThumbnail(prevs);
    }

    prevIndex++;

    QString slotName;
    QString bindName;

    if(copy)
    {
      slotName = tr("SRC");
      bindName = tr("Source");
    }
    else if(desc.access.index == DescriptorAccess::NoShaderBinding)
    {
      // this is a sensible name but we should query the API for name it prefers
      slotName = QFormatStr("Descriptor[%1]").arg(desc.access.arrayElement);

      // batch up these updates for now and send them off after previews are done
      if(show)
        m_DescriptorThumbUpdates.push_back({desc.access, prev});
    }
    else
    {
      slotName = QFormatStr("%1 %2%3")
                     .arg(m_Ctx.CurPipelineState().Abbrev(stage))
                     .arg(rw ? lit("RW ") : lit(""))
                     .arg(desc.access.index);
      if(desc.access.index < shaderInterface.size())
      {
        const ShaderResource &bind = shaderInterface[desc.access.index];
        bindName = bind.name;

        if(bind.bindArraySize > 1)
        {
          slotName += QFormatStr("[%1]").arg(desc.access.arrayElement);
          bindName += QFormatStr("[%1]").arg(desc.access.arrayElement);
        }
      }
    }

    InitResourcePreview(prev, show ? desc.descriptor : Descriptor(), show, follow, bindName,
                        slotName);
  }
}

void TextureViewer::thumb_doubleClicked(QMouseEvent *e)
{
  if(e->buttons() & Qt::LeftButton)
  {
    ResourceId id = m_Following.GetResourceId(m_Ctx);

    if(id != ResourceId())
    {
      ViewTexture(id, m_Following.GetTypeHint(m_Ctx), false);
    }
  }
}

void TextureViewer::thumb_resourceLinkActivated()
{
  ResourcePreview *preview = qobject_cast<ResourcePreview *>(QObject::sender());
  if(!preview)
    return;

  ResourceId resource = preview->property("id").value<ResourceId>();
  TextureDescription *texture = m_Ctx.GetTexture(resource);
  if(!texture)
    return;

  Subresource subresource;
  subresource.mip = preview->property("mip").toUInt();
  subresource.slice = preview->property("slice").toUInt();

  CompType typeCast = (CompType)preview->property("cast").toUInt();
  if(typeCast == CompType::Typeless)
    typeCast = texture->format.compType;
  subresource.sample = texture->msSamp > 1 && CanAverageTextureSamples(*texture, typeCast)
                           ? TextureDisplay::ResolveSamples
                           : 0;

  RenderTargetHistoryView::Open(m_Ctx, resource, subresource, typeCast, m_Ctx.CurEvent(), this);
}

void TextureViewer::thumb_clicked(QMouseEvent *e)
{
  if(e->buttons() & Qt::LeftButton)
  {
    ResourcePreview *prev = qobject_cast<ResourcePreview *>(QObject::sender());

    SelectPreview(prev);
  }

  if(e->buttons() & Qt::RightButton)
  {
    ResourcePreview *prev = qobject_cast<ResourcePreview *>(QObject::sender());

    Following follow = prev->property("f").value<Following>();

    ResourceId id = follow.GetResourceId(m_Ctx);

    if(id == ResourceId() && follow == m_Following)
      id = m_TexDisplay.resourceId;

    rdcarray<EventUsage> empty;

    bool input = follow.Type == FollowType::ReadOnly;

    if(id == ResourceId())
    {
      OpenResourceContextMenu(id, input, empty);
    }
    else
    {
      QPointer<TextureViewer> owner(this);
      const uint64_t serial = m_EventRequestSerial.load(std::memory_order_acquire);
      m_Ctx.Replay().AsyncInvoke([owner, id, input, serial](IReplayController *r) {
        rdcarray<EventUsage> usage = r->GetUsage(id);
        if(!owner)
          return;
        GUIInvoke::call(owner, [owner, id, input, usage, serial]() {
          if(owner && owner->m_EventRequestSerial.load(std::memory_order_acquire) == serial)
            owner->OpenResourceContextMenu(id, input, usage);
        });
      });
    }
  }
}

void TextureViewer::render_mouseWheel(QWheelEvent *e)
{
  QPoint cursorPos = e->pos();

  setFitToWindow(false);

  // scroll in logarithmic scale
  double logScale = logf(m_TexDisplay.scale);
  logScale += e->delta() / 2500.0;
  UI_SetScale((float)expf(logScale), cursorPos.x() * ui->render->devicePixelRatioF(),
              cursorPos.y() * ui->render->devicePixelRatioF());

  e->accept();
}

void TextureViewer::render_mouseMove(QMouseEvent *e)
{
  if(m_Output == NULL)
    return;

  m_CurHoverPixel.setX(int((float(e->x() * ui->render->devicePixelRatioF()) - m_TexDisplay.xOffset) /
                           m_TexDisplay.scale));
  m_CurHoverPixel.setY(int((float(e->y() * ui->render->devicePixelRatioF()) - m_TexDisplay.yOffset) /
                           m_TexDisplay.scale));

  if(m_TexDisplay.resourceId != ResourceId())
  {
    TextureDescription *texptr = GetCurrentTexture();

    if(texptr != NULL)
    {
      if(e->buttons() & Qt::RightButton)
      {
        ui->render->setCursor(QCursor(Qt::CrossCursor));

        m_PickedPoint = m_CurHoverPixel;

        m_PickedPoint.setX(qBound(0, m_PickedPoint.x(), (int)texptr->width - 1));
        m_PickedPoint.setY(qBound(0, m_PickedPoint.y(), (int)texptr->height - 1));

        QueuePickPixelsAndUpdate();
      }
      else if(e->buttons() == Qt::NoButton)
      {
        QueuePickPixelsAndUpdate(true);
      }
    }
  }

  QPoint curpos = QCursor::pos();

  if(e->buttons() & Qt::LeftButton)
  {
    if(qAbs(m_DragStartPos.x() - curpos.x()) > ui->renderHScroll->singleStep() ||
       qAbs(m_DragStartPos.y() - curpos.y()) > ui->renderVScroll->singleStep())
    {
      setScrollPosition(QPoint(m_DragStartScroll.x() + (curpos.x() - m_DragStartPos.x()),
                               m_DragStartScroll.y() + (curpos.y() - m_DragStartPos.y())));
    }

    ui->render->setCursor(QCursor(Qt::SizeAllCursor));
  }

  if(e->buttons() == Qt::NoButton)
  {
    ui->render->unsetCursor();
  }

  UI_UpdateStatusText();
}

void TextureViewer::render_mouseClick(QMouseEvent *e)
{
  ui->render->setFocus();

  if(e->buttons() & Qt::RightButton)
    render_mouseMove(e);

  if(e->buttons() & Qt::LeftButton)
  {
    m_DragStartPos = QCursor::pos();
    m_DragStartScroll = getScrollPosition();

    ui->render->setCursor(QCursor(Qt::SizeAllCursor));
  }
}

void TextureViewer::render_resize(QResizeEvent *e)
{
  UI_UpdateFittedScale();
  UI_CalcScrollbars();

  QueueUpdateAndDisplay();
}

void TextureViewer::render_keyPress(QKeyEvent *e)
{
  TextureDescription *texptr = GetCurrentTexture();

  if(texptr == NULL)
    return;

  if(ActiveRenderTargetHistoryEvent() != 0 &&
     !(e->modifiers() & (Qt::ControlModifier | Qt::AltModifier | Qt::MetaModifier)))
  {
    int delta = 0;
    if(e->key() == Qt::Key_Left || e->key() == Qt::Key_Up)
      delta = -1;
    else if(e->key() == Qt::Key_Right || e->key() == Qt::Key_Down)
      delta = 1;

    if(delta != 0 && m_RenderTargetHistory->StepSelection(delta))
    {
      e->accept();
      return;
    }
  }

  if(e->matches(QKeySequence::Copy))
  {
    QClipboard *clipboard = QApplication::clipboard();
    clipboard->setText(ui->texStatusName->text() + ui->texStatusDim->text() +
                       ui->texStatusFormat->text() + lit(" | ") + ui->hoverText->text() +
                       ui->pickedText->text());
  }

  if(!m_Ctx.IsCaptureLoaded())
    return;

  if((e->modifiers() & Qt::ControlModifier) && e->key() == Qt::Key_G)
  {
    ShowGotoPopup();
  }

  bool nudged = false;

  uint32_t mipx = MipCoordFromBase(m_PickedPoint.x(), texptr->width);
  uint32_t mipy = MipCoordFromBase(m_PickedPoint.y(), texptr->height);

  if(e->key() == Qt::Key_Up && m_PickedPoint.y() > 0)
  {
    m_PickedPoint.setY(BaseCoordFromMip(mipy - 1, texptr->height));
    nudged = true;
  }
  else if(e->key() == Qt::Key_Down && m_PickedPoint.y() < (int)texptr->height - 1)
  {
    m_PickedPoint.setY(BaseCoordFromMip(mipy + 1, texptr->height));
    nudged = true;
  }
  else if(e->key() == Qt::Key_Left && m_PickedPoint.x() > 0)
  {
    m_PickedPoint.setX(BaseCoordFromMip(mipx - 1, texptr->width));
    nudged = true;
  }
  else if(e->key() == Qt::Key_Right && m_PickedPoint.x() < (int)texptr->width - 1)
  {
    m_PickedPoint.setX(BaseCoordFromMip(mipx + 1, texptr->width));
    nudged = true;
  }

  if(nudged)
  {
    m_PickedPoint = QPoint(qBound(0, m_PickedPoint.x(), (int)texptr->width - 1),
                           qBound(0, m_PickedPoint.y(), (int)texptr->height - 1));
    e->accept();

    QueueUpdateAndDisplay(true);

    UI_UpdateStatusText();
  }
}

float TextureViewer::CurMaxScrollX()
{
  TextureDescription *texptr = GetCurrentTexture();

  QSizeF size(1.0f, 1.0f);

  if(texptr != NULL)
    size = QSizeF(texptr->width, texptr->height);

  return realRenderWidth() - size.width() * m_TexDisplay.scale;
}

float TextureViewer::CurMaxScrollY()
{
  TextureDescription *texptr = GetCurrentTexture();

  QSizeF size(1.0f, 1.0f);

  if(texptr != NULL)
    size = QSizeF(texptr->width, texptr->height);

  return realRenderHeight() - size.height() * m_TexDisplay.scale;
}

QPoint TextureViewer::getScrollPosition()
{
  return QPoint((int)m_TexDisplay.xOffset, m_TexDisplay.yOffset);
}

void TextureViewer::setScrollPosition(const QPoint &pos)
{
  m_TexDisplay.xOffset = qMax(CurMaxScrollX(), (float)pos.x());
  m_TexDisplay.yOffset = qMax(CurMaxScrollY(), (float)pos.y());

  m_TexDisplay.xOffset = qMin(0.0f, m_TexDisplay.xOffset);
  m_TexDisplay.yOffset = qMin(0.0f, m_TexDisplay.yOffset);

  if(ScrollUpdateScrollbars)
  {
    ScrollUpdateScrollbars = false;

    if(ui->renderHScroll->isEnabled())
      ui->renderHScroll->setValue(qBound(0, -int(m_TexDisplay.xOffset), ui->renderHScroll->maximum()));

    if(ui->renderVScroll->isEnabled())
      ui->renderVScroll->setValue(qBound(0, -int(m_TexDisplay.yOffset), ui->renderVScroll->maximum()));

    ScrollUpdateScrollbars = true;
  }

  QueueUpdateAndDisplay();
}

void TextureViewer::UI_CalcScrollbars()
{
  TextureDescription *texptr = GetCurrentTexture();

  QSizeF size(1.0f, 1.0f);

  if(texptr != NULL)
  {
    size = QSizeF(texptr->width, texptr->height);
  }

  if((int)floor(size.width() * m_TexDisplay.scale) <= realRenderWidth())
  {
    ui->renderHScroll->setEnabled(false);
  }
  else
  {
    ui->renderHScroll->setEnabled(true);

    ui->renderHScroll->setMaximum((int)ceil(size.width() * m_TexDisplay.scale - realRenderWidth()));
    ui->renderHScroll->setPageStep(qMax(1, ui->renderHScroll->maximum() / 6));
    ui->renderHScroll->setSingleStep(int(m_TexDisplay.scale));
  }

  if((int)floor(size.height() * m_TexDisplay.scale) <= realRenderHeight())
  {
    ui->renderVScroll->setEnabled(false);
  }
  else
  {
    ui->renderVScroll->setEnabled(true);

    ui->renderVScroll->setMaximum((int)ceil(size.height() * m_TexDisplay.scale - realRenderHeight()));
    ui->renderVScroll->setPageStep(qMax(1, ui->renderVScroll->maximum() / 6));
    ui->renderVScroll->setSingleStep(int(m_TexDisplay.scale));
  }
}

void TextureViewer::on_renderHScroll_valueChanged(int position)
{
  if(!ScrollUpdateScrollbars)
    return;

  ScrollUpdateScrollbars = false;

  {
    float delta = (float)position / (float)ui->renderHScroll->maximum();
    setScrollPosition(QPoint((int)(CurMaxScrollX() * delta), getScrollPosition().y()));
  }

  ScrollUpdateScrollbars = true;
}

void TextureViewer::on_renderVScroll_valueChanged(int position)
{
  if(!ScrollUpdateScrollbars)
    return;

  ScrollUpdateScrollbars = false;

  {
    float delta = (float)position / (float)ui->renderVScroll->maximum();
    setScrollPosition(QPoint(getScrollPosition().x(), (int)(CurMaxScrollY() * delta)));
  }

  ScrollUpdateScrollbars = true;
}

void TextureViewer::updateBackgroundColors()
{
  ui->render->SetBackCol(backCol);
  ui->pixelContext->SetBackCol(backCol);
}

void TextureViewer::OnCaptureLoaded()
{
  Reset();

  WindowingData renderData = ui->render->GetWidgetWindowingData();
  WindowingData contextData = ui->pixelContext->GetWidgetWindowingData();

  ui->saveTex->setEnabled(true);
  ui->locationGoto->setEnabled(true);
  ui->viewTexBuffer->setEnabled(true);

  ui->pixelHistory->setEnabled(false);
  ui->pixelHistory->setToolTip(QString());

  ui->debugPixelContext->setEnabled(false);
  ui->pixelHistory->setToolTip(QString());

  m_TexDisplay.backgroundColor =
      backCol.isValid() ? FloatVector(backCol.redF(), backCol.greenF(), backCol.blueF(), 1.0f)
                        : FloatVector();

  const TextureDisplay display = m_TexDisplay;
  const uint32_t historyEvent = ActiveRenderTargetHistoryEvent();
  m_Ctx.Replay().BlockInvoke(
      [renderData, contextData, display, historyEvent, this](IReplayController *r) {
        m_Output = r->CreateOutput(renderData, ReplayOutputType::Texture);

        m_Output->SetPixelContext(contextData);

        ui->render->SetOutput(m_Output);
        ui->pixelContext->SetOutput(m_Output);
        ui->render->SetOutputEventId(0);
        ui->pixelContext->SetOutputEventId(0);

        RT_UpdateAndDisplay(r, display, historyEvent);

        GUIInvoke::call(this, [this]() { OnEventChanged(m_Ctx.CurEvent()); });
      });

  m_Watcher = new QFileSystemWatcher({ConfigFilePath(QString())}, this);

  QObject::connect(m_Watcher, &QFileSystemWatcher::fileChanged, this,
                   &TextureViewer::customShaderModified);
  QObject::connect(m_Watcher, &QFileSystemWatcher::directoryChanged, this,
                   &TextureViewer::customShaderModified);

  m_NeedCustomReload = true;
}

void TextureViewer::Reset()
{
  m_AnalysisTimer->stop();
  m_AnalysisPending = false;
  // Capture shutdown owns these IReplayOutputs. Reset follows the replay-queue close barrier.
  m_NativeThumbnailOutputs.clear();
  CloseAIAnnotation();
  m_TemporaryOutputFollowToken = 0;
  m_BeforeTemporaryOutputTab.clear();
  m_PresentationSuspended = false;
  m_PresentationPending = false;
  // i18n-ignore: internal completion-evidence properties for UI regression fixtures.
  ui->render->setProperty("eidPresentationPending", QVariant());
  // i18n-ignore: internal completion-evidence properties for UI regression fixtures.
  ui->render->setProperty("eidPresentationSerial", QVariant());
  ui->render->show();
  ui->pixelContext->show();
  m_CachedTexture = NULL;
  m_OutputPreferences.clear();
  m_Following = Following(*this, FollowType::OutputColor, ShaderStage::Pixel, 0, 0);

  m_PickedPoint.setX(-1);
  m_PickedPoint.setY(-1);

  memset(&m_TexDisplay, 0, sizeof(m_TexDisplay));
  m_TexDisplay.backgroundColor =
      backCol.isValid() ? FloatVector(backCol.redF(), backCol.greenF(), backCol.blueF(), 1.0f)
                        : FloatVector();

  m_Output = NULL;

  m_TextureSettings.clear();

  m_PrevSize = QSizeF();
  m_HighWaterStatusLength = 0;

  ui->rangeHistogram->setRange(0.0f, 1.0f);

  ui->textureListFilter->setCurrentIndex(0);

  ui->renderHScroll->setEnabled(false);
  ui->renderVScroll->setEnabled(false);

  ui->pixelHistory->setEnabled(false);
  ui->pixelHistory->setToolTip(QString());
  ui->debugPixelContext->setEnabled(false);
  ui->debugPixelContext->setToolTip(QString());

  ui->texStatusName->setText(QString());
  ui->texStatusDim->setText(QString());
  ui->texStatusFormat->setText(QString());
  ui->hoverText->setText(QString());
  ui->pickedText->setText(QString());
  ui->renderContainer->setWindowTitle(tr("Current"));
  ui->mipLevel->clear();
  ui->sliceFace->clear();

  UI_SetScale(1.0f);

  ui->channels->setCurrentIndex(0);
  ui->overlay->setCurrentIndex(0);

  {
    QPalette Pal(palette());

    Pal.setColor(QPalette::Background, Qt::black);

    ui->pickSwatch->setAutoFillBackground(true);
    ui->pickSwatch->setPalette(Pal);
  }

  ui->customShader->clear();

  updateBackgroundColors();

  ui->inputThumbs->clearThumbs();
  ui->outputThumbs->clearThumbs();

  UI_UpdateTextureDetails();
  UI_UpdateChannels();
}

void TextureViewer::refreshTextureList()
{
  on_textureListFilter_currentIndexChanged(ui->textureListFilter->currentIndex());
}

void addToRoot(RDTreeWidgetItem *root, const TextureDescription &t)
{
  const QVariant &res = QVariant::fromValue(t.resourceId);
  RDTreeWidgetItem *child =
      new RDTreeWidgetItem({res, t.width, t.height, (3 == t.dimension ? t.depth : t.arraysize),
                            t.mips, t.format.Name(), QString()});

  child->setTag(res);
  root->addChild(child);
}

void TextureViewer::refreshTextureList(FilterType filterType, const QString &filterStr)
{
  ui->textureList->beginUpdate();
  ui->textureList->clearSelection();

  ui->textureList->clear();
  RDTreeWidgetItem *root = ui->textureList->invisibleRootItem();

  TextureCategory rtFlags = TextureCategory::ColorTarget | TextureCategory::DepthTarget;

  for(const TextureDescription &t : m_Ctx.GetTextures())
  {
    if(filterType == FilterType::Textures)
    {
      if(!(t.creationFlags & rtFlags))
        addToRoot(root, t);
    }
    else if(filterType == FilterType::RenderTargets)
    {
      if((t.creationFlags & rtFlags))
        addToRoot(root, t);
    }
    else
    {
      if(filterStr.isEmpty())
      {
        addToRoot(root, t);
      }
      else
      {
        if(QString(m_Ctx.GetResourceName(t.resourceId)).contains(filterStr, Qt::CaseInsensitive) ||
           QString::number(t.width).contains(filterStr, Qt::CaseInsensitive) ||
           QString::number(t.height).contains(filterStr, Qt::CaseInsensitive) ||
           QString::number((3 == t.dimension ? t.depth : t.arraysize))
               .contains(filterStr, Qt::CaseInsensitive) ||
           QString::number(t.mips).contains(filterStr, Qt::CaseInsensitive) ||
           QString(t.format.Name()).contains(filterStr, Qt::CaseInsensitive))
          addToRoot(root, t);
      }
    }
  }

  ui->textureList->setSelectedItem(root);

  ui->textureList->sortByColumn(ui->textureList->header()->sortIndicatorSection(),
                                ui->textureList->header()->sortIndicatorOrder());

  ui->textureList->setUpdatesEnabled(true);

  ui->textureList->endUpdate();
}

void TextureViewer::OnCaptureClosed()
{
  // i18n-ignore: internal completion-evidence property for the replay benchmark.
  ui->render->setProperty("eidDisplayedEvent", QVariant());
  // i18n-ignore: internal completion-evidence property for the replay benchmark.
  ui->render->setProperty("eidDisplayedResource", QVariant());
  m_EventRequestSerial.fetch_add(1, std::memory_order_acq_rel);
  // Invalidate queued callbacks before touching either the output pointer or thumbnail widgets.
  // The empty blocking invoke is a replay-thread barrier: a callback which had already passed its
  // serial check finishes before Reset clears m_Output, while stale queued callbacks return at
  // their first instruction.
  m_DisplayRequestSerial.fetch_add(1, std::memory_order_acq_rel);

  m_RenderTargetHistoryEvent = 0;
  if(m_RenderTargetHistory)
    m_RenderTargetHistory->Deactivate();

  m_Ctx.Replay().BlockInvoke([this](IReplayController *) {
    // Retire native surfaces while their QWidget handles still exist. The controller owns the
    // remaining main output until replay shutdown; preview widgets are deleted by Reset below.
    for(auto &thumbnail : m_NativeThumbnailOutputs)
      if(thumbnail.second)
        thumbnail.second->Shutdown();
    m_NativeThumbnailOutputs.clear();
  });

  Reset();

  refreshTextureList();

  delete m_Watcher;
  m_Watcher = NULL;

  RemoveTextureTabs(0);

  m_LockedTabs.clear();

  ui->customShader->clear();
  m_CustomShaders.clear();

  ui->saveTex->setEnabled(false);
  ui->locationGoto->setEnabled(false);
  ui->viewTexBuffer->setEnabled(false);

  UI_UpdateChannels();
  CompletePresentationWaiters();
}

void TextureViewer::OnScrubEvent(uint32_t eventId)
{
  m_AnalysisTimer->stop();
  m_AnalysisPending = false;
  m_EventRequestSerial.fetch_add(1, std::memory_order_acq_rel);
  m_DisplayRequestSerial.fetch_add(1, std::memory_order_acq_rel);
  // A new gesture cancels the old display. Let its navigation cycle advance to the new target;
  // waiting for a cancelled request here would block the very replay that can replace it.
  CompletePresentationWaiters();

  // Gesture input only cancels obsolete work. The completed event is published through
  // OnEventChanged, which selects its current binding before the output can be presented.
  (void)eventId;
}

void TextureViewer::OnEventChanged(uint32_t eventId)
{
  // A display callback from the previous event can be sitting behind thumbnail work on the replay
  // queue. Invalidate it before rebuilding this event's UI so it cannot bind stale resources.
  m_EventRequestSerial.fetch_add(1, std::memory_order_acq_rel);
  m_DisplayRequestSerial.fetch_add(1, std::memory_order_acq_rel);
  QScopedValueRollback<bool> updating(m_UpdatingTextureState, true);

  bool copy = false, clear = false, compute = false;
  Following::GetActionContext(m_Ctx, copy, clear, compute);

  ShaderStage stages[] = {
      ShaderStage::Vertex, ShaderStage::Hull, ShaderStage::Domain, ShaderStage::Geometry,
      ShaderStage::Pixel,  ShaderStage::Task, ShaderStage::Mesh,
  };

  QFont font = ui->overlay->font();
  if(m_Ctx.APIProps().pipelineType == GraphicsAPI::Vulkan &&
     m_Ctx.CurVulkanPipelineState()->multisample.rasterSamples > 1)
  {
    font.setItalic(true);
    ui->overlay->setItemText((int)DebugOverlay::QuadOverdrawDraw, tr("Overdraw (N/A on MSAA)"));
    ui->overlay->setItemText((int)DebugOverlay::QuadOverdrawPass, tr("Overdraw (N/A on MSAA)"));
    ui->overlay->setItemData((int)DebugOverlay::QuadOverdrawDraw, font, Qt::FontRole);
    ui->overlay->setItemData((int)DebugOverlay::QuadOverdrawPass, font, Qt::FontRole);
  }
  else
  {
    ui->overlay->setItemText((int)DebugOverlay::QuadOverdrawDraw, tr("Quad Overdraw (Draw)"));
    ui->overlay->setItemText((int)DebugOverlay::QuadOverdrawPass, tr("Quad Overdraw (Pass)"));
    ui->overlay->setItemData((int)DebugOverlay::QuadOverdrawDraw, font, Qt::FontRole);
    ui->overlay->setItemData((int)DebugOverlay::QuadOverdrawPass, font, Qt::FontRole);
  }

  int count = 7;

  if(compute)
  {
    stages[0] = ShaderStage::Compute;
    count = 1;
  }

  for(uint32_t stage = 0; stage < NumShaderStages; stage++)
  {
    m_ReadOnlyResources[stage].clear();
    m_ReadWriteResources[stage].clear();
  }

  for(int i = 0; i < count; i++)
  {
    ShaderStage stage = stages[i];

    m_ReadOnlyResources[(uint32_t)stage] = Following::GetReadOnlyResources(m_Ctx, stage, true);
    m_ReadWriteResources[(uint32_t)stage] = Following::GetReadWriteResources(m_Ctx, stage, true);
  }

  UI_ResolveOutputFollowing();
  UI_UpdateCachedTexture();

  TextureDescription *CurrentTexture = GetCurrentTexture();

  if(!currentTextureIsLocked() ||
     (CurrentTexture != NULL && m_TexDisplay.resourceId != CurrentTexture->resourceId))
    UI_OnTextureSelectionChanged(true);

  // No displayable binding is unbound for this event. Output fallback above uses only the current
  // action's bindings; explicit texture tabs retain their ID.
  if(CurrentTexture == NULL)
    m_TexDisplay.resourceId = ResourceId();

  if(m_Output == NULL)
    return;

  // Rebuilding combo boxes emits display signals. Only queue their final coherent state, before
  // the resource-preview batch, while the controller still owns this published event.
  UI_CreateThumbnails();

  UI_UpdateTextureDetails();

  if(m_ResourceCacheID != m_Ctx.ResourceNameCacheID())
  {
    m_ResourceCacheID = m_Ctx.ResourceNameCacheID();
    refreshTextureList();
  }

  // iterate over locked tabs, and update the name if it's changed
  for(QWidget *w : m_LockedTabs.values())
  {
    ResourceId id = w->property("id").value<ResourceId>();
    w->setWindowTitle(m_Ctx.GetResourceName(id));
  }

  rdcarray<Descriptor> RTs = Following::GetOutputTargets(m_Ctx);
  Descriptor Depth = Following::GetDepthTarget(m_Ctx);
  Descriptor DepthResolve = Following::GetDepthResolveTarget(m_Ctx);

  int outIndex = 0;
  int inIndex = 0;

  ui->outputThumbs->setUpdatesEnabled(false);
  ui->inputThumbs->setUpdatesEnabled(false);

  for(uint32_t rt = 0; rt < RTs.size(); rt++)
  {
    ResourcePreview *prev;

    if(outIndex < ui->outputThumbs->thumbs().size())
      prev = ui->outputThumbs->thumbs()[outIndex];
    else
      prev = UI_CreateThumbnail(ui->outputThumbs);

    outIndex++;

    Following follow(*this, FollowType::OutputColor, ShaderStage::Pixel, rt, 0);
    QString bindName = (copy || clear) ? tr("Destination") : QString();
    QString slotName = (copy || clear)
                           ? tr("DST")
                           : QString(m_Ctx.CurPipelineState().OutputAbbrev() + QString::number(rt));

    InitResourcePreview(prev, RTs[rt], false, follow, bindName, slotName);
  }

  // depth
  {
    ResourcePreview *prev;

    if(outIndex < ui->outputThumbs->thumbs().size())
      prev = ui->outputThumbs->thumbs()[outIndex];
    else
      prev = UI_CreateThumbnail(ui->outputThumbs);

    outIndex++;

    Following follow(*this, FollowType::OutputDepth, ShaderStage::Pixel, 0, 0);

    InitResourcePreview(prev, Depth, false, follow, QString(), tr("DS"));
  }
  // depth resolve
  {
    ResourcePreview *prev;

    if(outIndex < ui->outputThumbs->thumbs().size())
      prev = ui->outputThumbs->thumbs()[outIndex];
    else
      prev = UI_CreateThumbnail(ui->outputThumbs);

    outIndex++;

    Following follow(*this, FollowType::OutputDepthResolve, ShaderStage::Pixel, 0, 0);

    InitResourcePreview(prev, DepthResolve, false, follow, QString(), tr("DSR"));
  }

  const rdcarray<ShaderResource> empty;

  m_DescriptorThumbUpdates.clear();

  // display resources used for all stages
  for(int i = 0; i < count; i++)
  {
    ShaderStage stage = stages[i];

    const ShaderReflection *details = Following::GetReflection(m_Ctx, stage);

    InitStageResourcePreviews(stage, details != NULL ? details->readWriteResources : empty,
                              m_ReadWriteResources[(uint32_t)stage], ui->outputThumbs, outIndex,
                              copy, true);

    InitStageResourcePreviews(stage, details != NULL ? details->readOnlyResources : empty,
                              m_ReadOnlyResources[(uint32_t)stage], ui->inputThumbs, inIndex, copy,
                              false);
  }

  if(!m_DescriptorThumbUpdates.empty())
  {
    QPointer<TextureViewer> self(this);
    const uint64_t eventSerial = m_EventRequestSerial.load(std::memory_order_acquire);
    rdcarray<DescriptorThumbUpdate> updates = m_DescriptorThumbUpdates;
    m_Ctx.Replay().AsyncInvoke([self, eventId, eventSerial, updates](IReplayController *r) mutable {
      if(!self || self->m_EventRequestSerial.load(std::memory_order_acquire) != eventSerial ||
         r->GetCurrentEventID() != eventId)
        return;
      // we could collate ranges by descriptor store, but in practice we don't expect descriptors to be
      // scattered across multiple stores. So to keep the code simple for now we do a linear sweep
      ResourceId store;
      rdcarray<DescriptorRange> ranges;
      rdcarray<DescriptorLogicalLocation> locations;

      for(const DescriptorThumbUpdate &update : updates)
      {
        if(update.access.descriptorStore != store)
        {
          if(store != ResourceId())
            locations.append(r->GetDescriptorLocations(store, ranges));

          store = update.access.descriptorStore;
          ranges.clear();
        }

        // if the last range is contiguous with this access, append this access as a new range to query
        if(!ranges.empty() && ranges.back().descriptorSize == update.access.byteSize &&
           ranges.back().offset + ranges.back().descriptorSize == update.access.byteOffset &&
           ranges.back().type == update.access.type)
        {
          ranges.back().count++;
          continue;
        }

        DescriptorRange range = update.access;
        ranges.push_back(range);
      }

      if(store != ResourceId())
        locations.append(r->GetDescriptorLocations(store, ranges));

      for(size_t i = 0; i < updates.size(); i++)
      {
        if(i < locations.size())
          updates[i].slotName = locations[i].logicalBindName;
      }

      GUIInvoke::call(qApp, [self, eventSerial, updates]() {
        if(!self || self->m_EventRequestSerial.load(std::memory_order_acquire) != eventSerial)
          return;
        for(const DescriptorThumbUpdate &update : updates)
          if(update.preview && !update.slotName.isEmpty())
            update.preview->setSlotName(update.slotName);
      });
    });
  }

  // hide others
  const QVector<ResourcePreview *> &outThumbs = ui->outputThumbs->thumbs();

  for(; outIndex < outThumbs.size(); outIndex++)
  {
    ResourcePreview *prev = outThumbs[outIndex];
    prev->setResourceName(QString());
    prev->setActive(false);
    prev->setSelected(false);
  }

  ui->outputThumbs->refreshLayout();

  const QVector<ResourcePreview *> &inThumbs = ui->inputThumbs->thumbs();

  for(; inIndex < inThumbs.size(); inIndex++)
  {
    ResourcePreview *prev = inThumbs[inIndex];
    prev->setResourceName(QString());
    prev->setActive(false);
    prev->setSelected(false);
  }

  ui->inputThumbs->refreshLayout();

  if(ui->autoFit->isChecked())
    AutoFitRange();
  // Publish one coherent visible preview batch after all bindings and final layout are known.
  m_UpdatingTextureState = false;
  QueueUpdateAndDisplay(true);
}

QVariant TextureViewer::persistData()
{
  QVariantMap state = ui->dockarea->saveState();

  QVariantList columns;
  for(int i = 0; i < TextureListFilter::Column_Count; i++)
  {
    QVariantMap col;

    bool hidden = ui->textureList->header()->isSectionHidden(i);

    col[lit("hidden")] = hidden;
    columns.push_back(col);
  }

  state[lit("columns")] = columns;
  state[lit("backCol")] = backCol;
  state[lit("checker")] = !backCol.isValid();

  return state;
}

void TextureViewer::setPersistData(const QVariant &persistData)
{
  QVariantMap state = persistData.toMap();

  QVariantList columns = state[lit("columns")].toList();
  for(int i = 0; i < columns.count() && i < TextureListFilter::Column_Count; i++)
  {
    QVariantMap col = columns[i].toMap();

    bool hidden = col[lit("hidden")].toBool();

    if(hidden)
      ui->textureList->header()->hideSection(i);
    else
      ui->textureList->header()->showSection(i);
  }

  backCol = state[lit("backCol")].value<QColor>();
  bool checker = state[lit("checker")].value<bool>();

  if(checker)
    backCol = QColor();

  ui->backcolorPick->setChecked(!checker);
  ui->checkerBack->setChecked(checker);

  m_TexDisplay.backgroundColor =
      checker ? FloatVector() : FloatVector(backCol.redF(), backCol.greenF(), backCol.blueF(), 1.0f);

  RemoveTextureTabs(0);

  m_LockedTabs.clear();

  updateBackgroundColors();

  ui->dockarea->restoreState(state);

  SetupTextureTabs();
}

float TextureViewer::GetFitScale()
{
  TextureDescription *texptr = GetCurrentTexture();

  if(texptr == NULL)
    return 1.0f;

  float xscale = (float)realRenderWidth() / (float)texptr->width;
  float yscale = (float)realRenderHeight() / (float)texptr->height;

  return qMin(xscale, yscale);
}

int TextureViewer::realRenderWidth() const
{
  return ui->render->width() * ui->render->devicePixelRatioF();
}

int TextureViewer::realRenderHeight() const
{
  return ui->render->height() * ui->render->devicePixelRatioF();
}

void TextureViewer::UI_UpdateFittedScale()
{
  if(ui->fitToWindow->isChecked())
    UI_SetScale(1.0f);
}

void TextureViewer::UI_SetScale(float s)
{
  UI_SetScale(s, ui->render->width() / 2, ui->render->height() / 2);
}

void TextureViewer::UI_SetScale(float s, int x, int y)
{
  if(ui->fitToWindow->isChecked())
    s = GetFitScale();

  float prevScale = m_TexDisplay.scale;

  m_TexDisplay.scale = qBound(0.1f, s, 256.0f);

  QueueUpdateAndDisplay();

  float scaleDelta = (m_TexDisplay.scale / prevScale);

  QPoint newPos = getScrollPosition();

  newPos -= QPoint(x, y);
  newPos = QPoint((int)(newPos.x() * scaleDelta), (int)(newPos.y() * scaleDelta));
  newPos += QPoint(x, y);

  setCurrentZoomValue(m_TexDisplay.scale);

  UI_CalcScrollbars();

  setScrollPosition(newPos);
}

void TextureViewer::setCurrentZoomValue(float zoom)
{
  ui->zoomOption->setCurrentText(QString::number(ceil(zoom * 100)) + lit("%"));
}

void TextureViewer::setFitToWindow(bool checked)
{
  if(checked)
  {
    UI_UpdateFittedScale();
    ui->fitToWindow->setChecked(true);
  }
  else if(!checked)
  {
    ui->fitToWindow->setChecked(false);
    float curScale = m_TexDisplay.scale;
    ui->zoomOption->setCurrentText(QString());
    setCurrentZoomValue(curScale);
  }
}

void TextureViewer::on_fitToWindow_toggled(bool checked)
{
  UI_UpdateFittedScale();
}

void TextureViewer::on_zoomExactSize_clicked()
{
  ui->fitToWindow->setChecked(false);
  UI_SetScale(1.0f);
}

void TextureViewer::on_zoomOption_currentIndexChanged(int index)
{
  if(index >= 0)
  {
    setFitToWindow(false);
    ui->zoomOption->setCurrentText(ui->zoomOption->itemText(index));
    UI_SetScale(GetZoomLevel());
  }
}

void TextureViewer::zoomOption_returnPressed()
{
  ui->fitToWindow->setChecked(false);
  float zoom = GetZoomLevel();
  UI_SetScale(zoom);
}

void TextureViewer::on_overlay_currentIndexChanged(int index)
{
  m_TexDisplay.overlay = DebugOverlay::NoOverlay;

  if(ui->overlay->currentIndex() > 0)
    m_TexDisplay.overlay = (DebugOverlay)ui->overlay->currentIndex();

#define ANALYTICS_OVERLAY(name) \
  case DebugOverlay::name: ANALYTIC_SET(TextureOverlays.name, true); break;

  switch(m_TexDisplay.overlay)
  {
    ANALYTICS_OVERLAY(Drawcall);
    ANALYTICS_OVERLAY(Wireframe);
    ANALYTICS_OVERLAY(Depth);
    ANALYTICS_OVERLAY(Stencil);
    ANALYTICS_OVERLAY(BackfaceCull);
    ANALYTICS_OVERLAY(ViewportScissor);
    ANALYTICS_OVERLAY(NaN);
    ANALYTICS_OVERLAY(Clipping);
    ANALYTICS_OVERLAY(ClearBeforePass);
    ANALYTICS_OVERLAY(ClearBeforeDraw);
    ANALYTICS_OVERLAY(QuadOverdrawPass);
    ANALYTICS_OVERLAY(QuadOverdrawDraw);
    ANALYTICS_OVERLAY(TriangleSizePass);
    ANALYTICS_OVERLAY(TriangleSizeDraw);
    default: break;
  }

#undef ANALYTICS_OVERLAY

  QueueUpdateAndDisplay();
  if(m_Output != NULL && m_PickedPoint.x() >= 0 && m_PickedPoint.y() >= 0)
  {
    QueuePickPixelsAndUpdate();
  }
}

void TextureViewer::channelsWidget_mouseClicked(QMouseEvent *event)
{
  RDToolButton *s = qobject_cast<RDToolButton *>(QObject::sender());

  if(event->button() == Qt::RightButton && s)
  {
    bool checkd = false;

    RDToolButton *butts[] = {ui->channelRed, ui->channelGreen, ui->channelBlue, ui->channelAlpha};

    for(RDToolButton *b : butts)
    {
      if(b->isChecked() && b != s)
        checkd = true;
      if(!b->isChecked() && b == s)
        checkd = true;
    }

    ui->channelRed->setChecked(!checkd);
    ui->channelGreen->setChecked(!checkd);
    ui->channelBlue->setChecked(!checkd);
    ui->channelAlpha->setChecked(!checkd);
    s->setChecked(checkd);
  }
}

void TextureViewer::range_rangeUpdated()
{
  m_TexDisplay.rangeMin = ui->rangeHistogram->blackPoint();
  m_TexDisplay.rangeMax = ui->rangeHistogram->whitePoint();

  ui->rangeBlack->setText(Formatter::Format(m_TexDisplay.rangeMin));
  ui->rangeWhite->setText(Formatter::Format(m_TexDisplay.rangeMax));

  if(m_NoRangePaint)
    return;

  QueueUpdateAndDisplay();

  if(m_Output == NULL)
  {
    ui->render->update();
    ui->pixelContext->update();
  }
}

void TextureViewer::rangePoint_textChanged(QString text)
{
  m_RangePoint_Dirty = true;
}

void TextureViewer::rangePoint_Update()
{
  float black = ui->rangeHistogram->blackPoint();
  float white = ui->rangeHistogram->whitePoint();

  bool ok = false;
  double d = ui->rangeBlack->text().toDouble(&ok);

  if(ok)
    black = d;

  d = ui->rangeWhite->text().toDouble(&ok);

  if(ok)
    white = d;

  ui->rangeHistogram->setRange(black, white);

  QueueUpdateVisualRange();
}

void TextureViewer::rangePoint_leave()
{
  if(!m_RangePoint_Dirty)
    return;

  rangePoint_Update();

  m_RangePoint_Dirty = false;
}

void TextureViewer::rangePoint_keyPress(QKeyEvent *e)
{
  // escape key
  if(e->key() == Qt::Key_Escape)
  {
    m_RangePoint_Dirty = false;
    ui->rangeHistogram->setRange(ui->rangeHistogram->blackPoint(), ui->rangeHistogram->whitePoint());
  }
  if(e->key() == Qt::Key_Return || e->key() == Qt::Key_Enter)
  {
    rangePoint_Update();
  }
}

void TextureViewer::on_zoomRange_clicked()
{
  float black = ui->rangeHistogram->blackPoint();
  float white = ui->rangeHistogram->whitePoint();

  ui->autoFit->setChecked(false);

  ui->rangeHistogram->setRange(black, white);

  QueueUpdateVisualRange();
}

void TextureViewer::on_autoFit_clicked()
{
  AutoFitRange();
  ui->autoFit->setChecked(false);
}

void TextureViewer::on_autoFit_mouseClicked(QMouseEvent *e)
{
  if(e->buttons() & Qt::RightButton)
    ui->autoFit->setChecked(!ui->autoFit->isChecked());
}

void TextureViewer::on_reset01_clicked()
{
  UI_SetHistogramRange(GetCurrentTexture(), m_TexDisplay.typeCast);

  ui->autoFit->setChecked(false);

  QueueUpdateVisualRange();
}

void TextureViewer::on_visualiseRange_clicked()
{
  if(ui->visualiseRange->isChecked())
  {
    ui->rangeHistogram->setMinimumSize(QSize(300, 90));

    m_Visualise = true;
    QueueUpdateVisualRange();
  }
  else
  {
    m_Visualise = false;
    ui->rangeHistogram->setMinimumSize(QSize(200, 0));

    ui->rangeHistogram->setHistogramData({});
  }
}

void TextureViewer::AutoFitRange()
{
  // no capture loaded or buffer/empty texture currently being viewed - don't autofit
  if(m_RetargetingRenderTargetHistory || m_UpdatingTextureState || !m_Ctx.IsCaptureLoaded() ||
     GetCurrentTexture() == NULL || m_Output == NULL)
    return;

  QPointer<TextureViewer> self(this);
  const ViewRequest request = CurrentViewRequest();
  // i18n-ignore: internal replay-request coalescing tag, never shown to the user.
  m_Ctx.Replay().AsyncInvoke(lit("TextureViewerAutoFit"), [self, request](IReplayController *r) {
    if(!self || !self->IsCurrentViewRequest(request) || r->GetCurrentEventID() != request.eventId ||
       self->m_Output == NULL)
      return;
    const uint32_t historyEvent = request.historyEvent;
    TextureDisplay analysisDisplay;
    const bool haveSnapshot =
        ResolveTextureHistorySnapshot(r, historyEvent, request.display, analysisDisplay);
    ScopedTextureHistoryReplay historyReplay(r, haveSnapshot ? 0 : historyEvent);

    ResourceId textureId = analysisDisplay.resourceId;
    Subresource sub = analysisDisplay.subresource;
    CompType typeCast = analysisDisplay.typeCast;

    if(analysisDisplay.customShaderId != ResourceId() &&
       self->m_Output->GetCustomShaderTexID() != ResourceId())
    {
      textureId = self->m_Output->GetCustomShaderTexID();
      sub.slice = sub.sample = 0;
      typeCast = CompType::Typeless;
    }

    PixelValue min, max;
    rdctie(min, max) = r->GetMinMax(textureId, sub, typeCast);

    {
      float minval = FLT_MAX;
      float maxval = -FLT_MAX;

      bool changeRange = false;

      ResourceFormat fmt = request.texture.format;

      if(request.display.customShaderId != ResourceId())
      {
        fmt.compType = CompType::Float;
      }
      if(fmt.compType == CompType::Typeless && request.display.typeCast == CompType::UInt)
        fmt.compType = CompType::UInt;
      if(fmt.compType == CompType::Typeless && request.display.typeCast == CompType::SInt)
        fmt.compType = CompType::SInt;

      for(int i = 0; i < 4; i++)
      {
        if(fmt.compType == CompType::UInt)
        {
          min.floatValue[i] = min.uintValue[i];
          max.floatValue[i] = max.uintValue[i];
        }
        else if(fmt.compType == CompType::SInt)
        {
          min.floatValue[i] = min.intValue[i];
          max.floatValue[i] = max.intValue[i];
        }
      }

      if(request.display.red)
      {
        minval = qMin(minval, min.floatValue[0]);
        maxval = qMax(maxval, max.floatValue[0]);
        changeRange = true;
      }
      if(request.display.green && fmt.compCount > 1)
      {
        minval = qMin(minval, min.floatValue[1]);
        maxval = qMax(maxval, max.floatValue[1]);
        changeRange = true;
      }
      if(request.display.blue && fmt.compCount > 2)
      {
        minval = qMin(minval, min.floatValue[2]);
        maxval = qMax(maxval, max.floatValue[2]);
        changeRange = true;
      }
      if(request.display.alpha && fmt.compCount > 3)
      {
        minval = qMin(minval, min.floatValue[3]);
        maxval = qMax(maxval, max.floatValue[3]);
        changeRange = true;
      }

      if(changeRange)
      {
        GUIInvoke::call(qApp, [self, request, minval, maxval]() {
          if(!self || !self->IsCurrentViewRequest(request))
            return;
          self->ui->rangeHistogram->setRange(minval, maxval);
          self->QueueUpdateVisualRange();
        });
      }
    }
  });
}

void TextureViewer::on_backcolorPick_clicked()
{
  QColor col = QColorDialog::getColor(Qt::black, this, tr("Choose background colour"));

  if(!col.isValid())
  {
    ui->backcolorPick->setChecked(!ui->checkerBack->isChecked());
    return;
  }

  col = col.toRgb();
  m_TexDisplay.backgroundColor = FloatVector(col.redF(), col.greenF(), col.blueF(), 1.0f);

  backCol = col;

  updateBackgroundColors();

  ui->backcolorPick->setChecked(true);
  ui->checkerBack->setChecked(false);

  QueueUpdateAndDisplay();

  if(m_Output == NULL)
  {
    ui->render->update();
    ui->pixelContext->update();
  }
}

void TextureViewer::on_checkerBack_clicked()
{
  ui->checkerBack->setChecked(true);
  ui->backcolorPick->setChecked(false);

  backCol = QColor();

  m_TexDisplay.backgroundColor = FloatVector();

  updateBackgroundColors();

  QueueUpdateAndDisplay();

  if(m_Output == NULL)
  {
    ui->render->update();
    ui->pixelContext->update();
  }
}

void TextureViewer::on_mipLevel_currentIndexChanged(int index)
{
  TextureDescription *texptr = GetCurrentTexture();
  if(texptr == NULL)
    return;

  TextureDescription &tex = *texptr;

  uint32_t prevSlice = m_TexDisplay.subresource.slice << (int)m_TexDisplay.subresource.mip;

  if(tex.mips > 1)
  {
    m_TexDisplay.subresource.mip = (uint32_t)qMax(0, index);
    m_TexDisplay.subresource.sample = 0;
  }
  else
  {
    m_TexDisplay.subresource.mip = 0;
    m_TexDisplay.subresource.sample = (uint32_t)qMax(0, index);
    if(m_TexDisplay.subresource.sample == tex.msSamp)
      m_TexDisplay.subresource.sample = ~0U;
  }

  // For 3D textures, update the slice list for this mip
  if(tex.depth > 1)
  {
    uint32_t newSlice = prevSlice >> (int)m_TexDisplay.subresource.mip;

    uint32_t numSlices = qMax(1U, tex.depth >> (int)m_TexDisplay.subresource.mip);

    ui->sliceFace->clear();

    for(uint32_t i = 0; i < numSlices; i++)
      ui->sliceFace->addItem(tr("Slice %1").arg(i));

    // changing sliceFace index will handle updating range & re-picking
    ui->sliceFace->setCurrentIndex((int)qBound(0U, newSlice, numSlices - 1));

    return;
  }

  UpdateRenderTargetHistorySubresource();
  QueueUpdateAndDisplay();

  if(m_Output != NULL && m_PickedPoint.x() >= 0 && m_PickedPoint.y() >= 0)
  {
    QueuePickPixelsAndUpdate();
  }

  QueueUpdateVisualRange();
}

void TextureViewer::on_sliceFace_currentIndexChanged(int index)
{
  TextureDescription *texptr = GetCurrentTexture();
  if(texptr == NULL)
    return;

  TextureDescription &tex = *texptr;
  m_TexDisplay.subresource.slice = (uint32_t)qMax(0, index);

  UpdateRenderTargetHistorySubresource();
  QueueUpdateAndDisplay();

  QueueUpdateVisualRange();

  if(m_Output != NULL && m_PickedPoint.x() >= 0 && m_PickedPoint.y() >= 0)
  {
    QueuePickPixelsAndUpdate();
  }
}

void TextureViewer::on_locationGoto_clicked()
{
  ShowGotoPopup();
}

rdcpair<int32_t, int32_t> TextureViewer::GetPickedLocation()
{
  TextureDescription *texptr = GetCurrentTexture();

  if(texptr)
  {
    QPoint p = m_PickedPoint;

    p.setX(MipCoordFromBase(p.x(), texptr->width));
    p.setY(MipCoordFromBase(p.y(), texptr->height));

    uint32_t mipHeight = qMax(1U, texptr->height >> (int)m_TexDisplay.subresource.mip);

    if(ShouldFlipForGL())
      p.setY((int)(mipHeight - 1) - p.y());
    if(m_TexDisplay.flipY)
      p.setY((int)(mipHeight - 1) - p.y());

    return {p.x(), p.y()};
  }

  return {-1, -1};
}

void TextureViewer::ShowGotoPopup()
{
  TextureDescription *texptr = GetCurrentTexture();

  if(texptr)
  {
    QPoint p = m_PickedPoint;

    p.setX(MipCoordFromBase(p.x(), texptr->width));
    p.setY(MipCoordFromBase(p.y(), texptr->height));

    uint32_t mipHeight = qMax(1U, texptr->height >> (int)m_TexDisplay.subresource.mip);

    if(ShouldFlipForGL())
      p.setY((int)(mipHeight - 1) - p.y());
    if(m_TexDisplay.flipY)
      p.setY((int)(mipHeight - 1) - p.y());

    m_Goto->show(ui->render, p);
  }
}

bool TextureViewer::ShouldFlipForGL()
{
  if(m_Ctx.APIProps().pipelineType == GraphicsAPI::OpenGL)
  {
    // lower left is the default clip origin, which needs the Y flip
    return m_Ctx.CurGLPipelineState()->vertexProcessing.clipOriginLowerLeft;
  }

  return false;
}

void TextureViewer::on_viewTexBuffer_clicked()
{
  TextureDescription *texptr = GetCurrentTexture();

  if(texptr)
  {
    IBufferViewer *viewer =
        m_Ctx.ViewTextureAsBuffer(texptr->resourceId, m_TexDisplay.subresource,
                                  BufferFormatter::GetTextureFormatString(*texptr));

    viewer->ScrollToRow(m_PickedPoint.y());
    viewer->ScrollToColumn(m_PickedPoint.x() * texptr->format.compCount + 1);

    m_Ctx.AddDockWindow(viewer->Widget(), DockReference::AddTo, this);
  }
}

void TextureViewer::on_resourceDetails_clicked()
{
  TextureDescription *texptr = GetCurrentTexture();

  if(texptr)
  {
    if(!m_Ctx.HasResourceInspector())
      m_Ctx.ShowResourceInspector();

    m_Ctx.GetResourceInspector()->Inspect(texptr->resourceId);

    ToolWindowManager::raiseToolWindow(m_Ctx.GetResourceInspector()->Widget());
  }
}

void TextureViewer::on_saveTex_clicked()
{
  TextureDescription *texptr = GetCurrentTexture();

  if(!texptr || !m_Output)
    return;

  // overwrite save params with current texture display settings
  m_SaveConfig.resourceId = m_TexDisplay.resourceId;
  m_SaveConfig.typeCast = m_TexDisplay.typeCast;
  m_SaveConfig.slice.sliceIndex = (int)m_TexDisplay.subresource.slice;
  m_SaveConfig.mip = (int)m_TexDisplay.subresource.mip;

  m_SaveConfig.channelExtract = -1;
  if(m_TexDisplay.red && !m_TexDisplay.green && !m_TexDisplay.blue && !m_TexDisplay.alpha)
    m_SaveConfig.channelExtract = 0;
  if(!m_TexDisplay.red && m_TexDisplay.green && !m_TexDisplay.blue && !m_TexDisplay.alpha)
    m_SaveConfig.channelExtract = 1;
  if(!m_TexDisplay.red && !m_TexDisplay.green && m_TexDisplay.blue && !m_TexDisplay.alpha)
    m_SaveConfig.channelExtract = 2;
  if(!m_TexDisplay.red && !m_TexDisplay.green && !m_TexDisplay.blue && m_TexDisplay.alpha)
    m_SaveConfig.channelExtract = 3;

  m_SaveConfig.comp.blackPoint = m_TexDisplay.rangeMin;
  m_SaveConfig.comp.whitePoint = m_TexDisplay.rangeMax;
  m_SaveConfig.alphaCol = m_TexDisplay.backgroundColor;

  if(m_TexDisplay.customShaderId != ResourceId())
  {
    ResourceId id;
    m_Ctx.Replay().BlockInvoke(
        [this, &id](IReplayController *r) { id = m_Output->GetCustomShaderTexID(); });

    if(id != ResourceId())
    {
      m_SaveConfig.resourceId = id;
      m_SaveConfig.typeCast = CompType::Typeless;
    }
  }

  ResourceId overlayTexID;
  if(m_TexDisplay.overlay != DebugOverlay::NoOverlay)
  {
    m_Ctx.Replay().BlockInvoke([this, &overlayTexID](IReplayController *r) {
      overlayTexID = m_Output->GetDebugOverlayTexID();
    });
  }
  const bool hasSelectedOverlay = (m_TexDisplay.overlay != DebugOverlay::NoOverlay);
  const bool hasOverlay = (hasSelectedOverlay && overlayTexID != ResourceId());
  TextureSaveDialog saveDialog(*texptr, hasOverlay, m_SaveConfig, this);
  int res = RDDialog::show(&saveDialog);

  m_SaveConfig = saveDialog.config();

  if(saveDialog.saveOverlayInstead())
  {
    m_SaveConfig.resourceId = overlayTexID;
    m_SaveConfig.typeCast = CompType::Typeless;

    if(m_TexDisplay.overlay == DebugOverlay::QuadOverdrawDraw ||
       m_TexDisplay.overlay == DebugOverlay::QuadOverdrawPass ||
       m_TexDisplay.overlay == DebugOverlay::TriangleSizeDraw ||
       m_TexDisplay.overlay == DebugOverlay::TriangleSizePass)
    {
      m_SaveConfig.comp.blackPoint = 0.0f;
      m_SaveConfig.comp.whitePoint = 255.0f;
    }
  }

  if(res)
  {
    ANALYTIC_SET(Export.Texture, true);

    ResultDetails result = {ResultCode::Succeeded};
    QString fn = saveDialog.filename();

    m_Ctx.Replay().BlockInvoke([this, &result, fn](IReplayController *r) {
      ScopedTextureHistoryReplay historyReplay(r, ActiveRenderTargetHistoryEvent());
      result = r->SaveTexture(m_SaveConfig, fn);
    });

    if(!result.OK())
    {
      RDDialog::critical(NULL, tr("Error saving texture"),
                         tr("Error saving texture %1:\n\n%2").arg(fn).arg(result.Message()));
    }
  }
}

void TextureViewer::on_debugPixelContext_clicked()
{
  if(m_PickedPoint.x() < 0 || m_PickedPoint.y() < 0)
    return;

  TextureDescription *texptr = GetCurrentTexture();
  if(!texptr)
    return;
  m_Ctx.CancelReplayBackgroundWork();

  int x = MipCoordFromBase(m_PickedPoint.x(), texptr->width);
  int y = MipCoordFromBase(m_PickedPoint.y(), texptr->height);

  uint32_t mipHeight = qMax(1U, texptr->height >> (int)m_TexDisplay.subresource.mip);

  if(m_TexDisplay.flipY)
    y = (int)(mipHeight - 1) - y;

  bool done = false;
  ShaderDebugTrace *trace = NULL;

  uint32_t view = m_TexDisplay.subresource.slice - m_Following.GetFirstArraySlice(m_Ctx);
  m_Ctx.Replay().AsyncInvoke([this, &trace, &done, x, y, view](IReplayController *r) {
    DebugPixelInputs inputs;
    inputs.sample = m_TexDisplay.subresource.sample;
    inputs.view = view;
    trace = r->DebugPixel((uint32_t)x, (uint32_t)y, inputs);

    if(trace->debugger == NULL)
    {
      r->FreeTrace(trace);
      trace = NULL;
    }

    done = true;
  });

  QString debugContext = tr("Pixel %1,%2").arg(x).arg(y);

  // wait a short while before displaying the progress dialog (which won't show if we're already
  // done by the time we reach it)
  for(int i = 0; !done && i < 100; i++)
    QThread::msleep(5);

  ShowProgressDialog(this, tr("Debugging %1").arg(debugContext), [&done]() { return done; });

  // if we couldn't debug the pixel on this event, open up a pixel history
  if(!trace)
  {
    if(m_Ctx.APIProps().pixelHistory)
      ShowPixelHistory(true);
    else
      RDDialog::critical(this, tr("Debug Error"), tr("Error debugging pixel."));
    return;
  }

  const ShaderReflection *shaderDetails =
      m_Ctx.CurPipelineState().GetShaderReflection(ShaderStage::Pixel);
  ResourceId pipeline = m_Ctx.CurPipelineState().GetGraphicsPipelineObject();

  // viewer takes ownership of the trace
  IShaderViewer *s = m_Ctx.DebugShader(shaderDetails, pipeline, trace, debugContext);

  m_Ctx.AddDockWindow(s->Widget(), DockReference::AddTo, this);
}

void TextureViewer::on_pixelHistory_clicked()
{
  ShowPixelHistory(false);
}

void TextureViewer::ShowPixelHistory(bool failedDebug)
{
  m_Ctx.CancelReplayBackgroundWork();
  TextureDescription *texptr = GetCurrentTexture();

  if(!texptr || !m_Output)
    return;

  ANALYTIC_SET(UIFeatures.PixelHistory, true);

  int x = MipCoordFromBase(m_PickedPoint.x(), texptr->width);
  int y = MipCoordFromBase(m_PickedPoint.y(), texptr->height);

  uint32_t mipHeight = qMax(1U, texptr->height >> (int)m_TexDisplay.subresource.mip);

  if(m_TexDisplay.flipY)
    y = (int)(mipHeight - 1) - y;

  uint32_t view = m_TexDisplay.subresource.slice - m_Following.GetFirstArraySlice(m_Ctx);
  IPixelHistoryView *hist = m_Ctx.ViewPixelHistory(texptr->resourceId, x, y, view, m_TexDisplay);

  if(failedDebug)
    hist->SetFailedDebug();

  m_Ctx.AddDockWindow(hist->Widget(), DockReference::TransientPopupArea, this, 0.3f);

  // we use this pointer to ensure that the history viewer is still visible (and hasn't been closed)
  // by the time we want to set the results.
  QPointer<QWidget> histWidget = hist->Widget();

  // add a short delay so that controls repainting after a new panel appears can get at the
  // render thread before we insert the long blocking pixel history task
  LambdaThread *thread = new LambdaThread([this, texptr, x, y, hist, histWidget]() {
    QThread::msleep(150);
    m_Ctx.Replay().AsyncInvoke([this, texptr, x, y, hist, histWidget](IReplayController *r) {
      rdcarray<PixelModification> history =
          r->PixelHistory(texptr->resourceId, (uint32_t)x, (int32_t)y, m_TexDisplay.subresource,
                          m_TexDisplay.typeCast);

      GUIInvoke::call(this, [hist, histWidget, history] {
        if(histWidget)
          hist->SetHistory(history);
      });
    });
  });
  thread->selfDelete(true);
  thread->start();
}

void TextureViewer::on_texListShow_clicked()
{
  if(ui->textureListFrame->isVisible())
  {
    ui->dockarea->moveToolWindow(ui->textureListFrame, ToolWindowManager::NoArea);
  }
  else
  {
    ui->textureListFilter->setCurrentText(QString());
    ui->dockarea->moveToolWindow(
        ui->textureListFrame,
        ToolWindowManager::AreaReference(ToolWindowManager::BottomOf,
                                         ui->dockarea->areaOf(ui->renderContainer), 0.2f));
    ui->dockarea->setToolWindowProperties(ui->textureListFrame, ToolWindowManager::HideOnClose);
  }
}

void TextureViewer::on_cancelTextureListFilter_clicked()
{
  ui->textureListFilter->setCurrentText(QString());
}

void TextureViewer::on_colSelect_clicked()
{
  QStringList headers;
  for(int i = 0; i < TextureListFilter::Column_Count; ++i)
  {
    headers.push_back(
        ui->textureList->model()->headerData(i, Qt::Horizontal, Qt::DisplayRole).toString());
  }

  UpdateVisibleColumns(tr("Select Texture List Columns"), TextureListFilter::Column_Count,
                       ui->textureList->header(), headers);
}

void TextureViewer::on_textureListFilter_editTextChanged(const QString &text)
{
  refreshTextureList(FilterType::String, text);
}

void TextureViewer::on_textureListFilter_currentIndexChanged(int index)
{
  if(ui->textureListFilter->currentIndex() == 1)
    refreshTextureList(FilterType::Textures, QString());
  else if(ui->textureListFilter->currentIndex() == 2)
    refreshTextureList(FilterType::RenderTargets, QString());
  else
    refreshTextureList(FilterType::String, ui->textureListFilter->currentText());
}

void TextureViewer::texture_itemActivated(RDTreeWidgetItem *item, int column)
{
  QVariant tag = item->tag();
  if(!tag.canConvert<ResourceId>())
    return;

  TextureDescription *tex = m_Ctx.GetTexture(tag.value<ResourceId>());
  if(!tex)
    return;

  if(tex->type == TextureType::Buffer)
  {
    IBufferViewer *viewer = m_Ctx.ViewTextureAsBuffer(
        tex->resourceId, Subresource(), BufferFormatter::GetTextureFormatString(*tex));

    m_Ctx.AddDockWindow(viewer->Widget(), DockReference::AddTo, this);
  }
  else
  {
    CompType typeCast = CompType::Typeless;
    if(m_TextureSettings.contains(tex->resourceId))
      typeCast = m_TextureSettings[tex->resourceId].typeCast;
    ViewTexture(tex->resourceId, typeCast, true);
  }
}

bool TextureViewer::canCompileCustomShader(ShaderEncoding encoding)
{
  rdcarray<ShaderEncoding> supported = m_Ctx.CustomShaderEncodings();

  // if it's directly supported, we can trivially compile it
  if(supported.contains(encoding))
    return true;

  // otherwise see if we have a tool that can compile it for us

  for(const ShaderProcessingTool &tool : m_Ctx.Config().ShaderProcessors)
  {
    // if this tool transforms from the encoding to one we support, we can compile a shader of this
    // encoding
    if(tool.input == encoding && supported.contains(tool.output))
      return true;
  }

  // all options exhausted, we can't compile this
  return false;
}

void TextureViewer::reloadCustomShaders(const QString &filter)
{
  if(!m_Ctx.IsCaptureLoaded())
    return;

  if(filter.isEmpty())
  {
    QString prevtext = ui->customShader->currentText();

    QList<ResourceId> shaders = m_CustomShaders.values();

    m_Ctx.Replay().AsyncInvoke([shaders](IReplayController *r) {
      for(ResourceId s : shaders)
        r->FreeCustomShader(s);
    });

    ui->customShader->clear();
    m_CustomShaders.clear();

    ui->customShader->setCurrentText(prevtext);
  }
  else
  {
    QString fn = QFileInfo(filter).fileName();
    QString key = fn.toUpper();

    if(m_CustomShaders.contains(key))
    {
      if(m_CustomShadersBusy.contains(key))
        return;

      ResourceId freed = m_CustomShaders[key];
      m_Ctx.Replay().AsyncInvoke([freed](IReplayController *r) { r->FreeCustomShader(freed); });

      m_CustomShaders.remove(key);

      QString text = ui->customShader->currentText();

      for(int i = 0; i < ui->customShader->count(); i++)
      {
        if(ui->customShader->itemText(i).compare(fn, Qt::CaseInsensitive) == 0)
        {
          ui->customShader->removeItem(i);
          break;
        }
      }

      ui->customShader->setCurrentText(text);
    }
  }

  QStringList filters;
  for(auto it = encodingExtensions.begin(); it != encodingExtensions.end(); ++it)
  {
    if(!canCompileCustomShader(it.value()))
      continue;

    filters.push_back(lit("*.") + it.key());
  }

  QStringList files;
  QList<QDir> shaderDirectories = getShaderDirectories();
  for(const QDir &dir : shaderDirectories)
  {
    QStringList currentDirFiles =
        dir.entryList(filters, QDir::Files | QDir::NoDotAndDotDot, QDir::Name | QDir::IgnoreCase);
    for(const QString &f : currentDirFiles)
    {
      files.append(QDir::cleanPath(dir.absoluteFilePath(f)));
    }
  }

  QStringList watchedFiles = m_Watcher->files();
  if(!watchedFiles.isEmpty())
    m_Watcher->removePaths(watchedFiles);

  for(const QString &f : files)
  {
    QFileInfo fileInfo(f);
    QString fn = fileInfo.fileName();
    QString key = fn.toUpper();
    ShaderEncoding encoding = encodingExtensions[fileInfo.completeSuffix()];

    if(!filter.isEmpty() && filter.toUpper() != key)
      continue;

    m_Watcher->addPath(f);

    if(!m_CustomShaders.contains(key) && !m_CustomShadersBusy.contains(key))
    {
      QFile fileHandle(f);
      if(fileHandle.open(QFile::ReadOnly | QFile::Text))
      {
        QTextStream stream(&fileHandle);
        QString source = stream.readAll();

        bytebuf shaderBytes;

        rdcarray<ShaderEncoding> supported = m_Ctx.CustomShaderEncodings();
        rdcarray<ShaderSourcePrefix> prefixes = m_Ctx.CustomShaderSourcePrefixes();

        rdcstr errors;

        if(supported.contains(encoding))
        {
          // apply any prefix needed
          for(const ShaderSourcePrefix &prefix : prefixes)
          {
            if(prefix.encoding == encoding)
            {
              source = QString(prefix.prefix) + source;
              break;
            }
          }

          shaderBytes = bytebuf(source.toUtf8());
        }
        else
        {
          // we don't accept this encoding directly, need to compile
          for(const ShaderProcessingTool &tool : m_Ctx.Config().ShaderProcessors)
          {
            // pick the first tool that can convert to an accepted format
            if(tool.input == encoding && supported.contains(tool.output))
            {
              // apply any prefix needed
              for(const ShaderSourcePrefix &prefix : prefixes)
              {
                if(prefix.encoding == encoding)
                {
                  source = QString(prefix.prefix) + source;
                  break;
                }
              }

              ShaderToolOutput out =
                  tool.CompileShader(this, source, "main", ShaderStage::Pixel, "", "");

              errors = out.log;

              if(m_CustomShaderEditor.contains(key))
                m_CustomShaderEditor[key]->ShowErrors(errors);

              if(out.result.isEmpty())
                break;

              encoding = tool.output;
              shaderBytes = out.result;
              break;
            }
          }
        }

        // if the encoding still isn't supported, all tools failed. Bail out now
        if(!supported.contains(encoding))
        {
          if(m_CustomShaderEditor.contains(key))
            m_CustomShaderEditor[key]->ShowErrors(errors);

          m_CustomShaders[key] = ResourceId();

          QString prevtext = ui->customShader->currentText();
          ui->customShader->addItem(fn);
          ui->customShader->setCurrentText(prevtext);
          continue;
        }

        ANALYTIC_SET(UIFeatures.CustomTextureVisualise, true);

        fileHandle.close();

        rdcarray<rdcstr> dirs;

        for(QDir d : getShaderDirectories())
        {
          if(d.exists())
            dirs.push_back(d.absolutePath());
        }

        m_CustomShaders[key] = ResourceId();
        m_CustomShadersBusy.push_back(key);
        m_Ctx.Replay().AsyncInvoke(
            [this, fn, dirs, key, shaderBytes, encoding, errors](IReplayController *r) {
              rdcstr buildErrors;

              r->SetCustomShaderIncludes(dirs);

              ResourceId id;
              rdctie(id, buildErrors) = r->BuildCustomShader(
                  "main", encoding, shaderBytes, ShaderCompileFlags(), ShaderStage::Pixel);

              if(!errors.empty())
                buildErrors = errors + "\n\n" + buildErrors;

              if(m_CustomShaderEditor.contains(key))
              {
                IShaderViewer *editor = m_CustomShaderEditor[key];
                GUIInvoke::call(editor->Widget(),
                                [editor, buildErrors]() { editor->ShowErrors(buildErrors); });
              }

              GUIInvoke::call(this, [this, fn, key, id]() {
                QString prevtext = ui->customShader->currentText();
                ui->customShader->addItem(fn);
                ui->customShader->setCurrentText(prevtext);

                m_CustomShaders[key] = id;
                m_CustomShadersBusy.removeOne(key);

                UI_UpdateChannels();
              });
            });
      }
    }
  }
}

QList<QDir> TextureViewer::getShaderDirectories() const
{
  QList<QDir> dirs;
  dirs.reserve(int(m_Ctx.Config().TextureViewer_ShaderDirs.size() + 1u));
  dirs.append(QDir(ConfigFilePath(QString())));
  for(const rdcstr &dir : m_Ctx.Config().TextureViewer_ShaderDirs)
  {
    dirs.append(QDir(dir));
  }

  return dirs;
}

QString TextureViewer::getShaderPath(const QString &filename) const
{
  QString path;
  QList<QDir> directories = getShaderDirectories();
  for(const QDir &dir : directories)
  {
    QStringList currentDirFiles =
        dir.entryList(QDir::Files | QDir::NoDotAndDotDot, QDir::Name | QDir::IgnoreCase);

    if(currentDirFiles.contains(filename, Qt::CaseInsensitive))
    {
      path = QDir::cleanPath(dir.absoluteFilePath(filename));
      break;
    }
  }

  return path;
}

uint32_t TextureViewer::MipCoordFromBase(int coord, const uint32_t dim)
{
  return ::MipCoordFromBase(coord, m_TexDisplay.subresource.mip, dim);
}

uint32_t TextureViewer::BaseCoordFromMip(int coord, const uint32_t dim)
{
  return ::BaseCoordFromMip(coord, m_TexDisplay.subresource.mip, dim);
}

void TextureViewer::on_customCreate_clicked()
{
  QString filename = ui->customShader->currentText();

  if(filename.isEmpty())
  {
    RDDialog::critical(this, tr("Error Creating Shader"),
                       tr("No shader name specified.\nEnter a new name in the textbox"));
    return;
  }

  if(m_CustomShaders.contains(filename.toUpper()))
  {
    RDDialog::critical(this, tr("Error Creating Shader"),
                       tr("Selected shader already exists.\nEnter a new name in the textbox."));
    ui->customShader->setCurrentText(QString());
    UI_UpdateChannels();
    return;
  }

  ShaderEncoding enc = encodingExtensions[QFileInfo(filename).completeSuffix()];

  if(enc == ShaderEncoding::Unknown)
  {
    QString extString;

    for(auto it = encodingExtensions.begin(); it != encodingExtensions.end(); ++it)
    {
      if(!canCompileCustomShader(it.value()))
        continue;

      if(!extString.isEmpty())
        extString += lit(", ");
      extString += it.key();
    }

    RDDialog::critical(this, tr("Error Creating Shader"),
                       tr("No file extension specified, unknown shading language.\n"
                          "Filename must contain one of: %1")
                           .arg(extString));
    return;
  }

  QString src;

  if(enc == ShaderEncoding::HLSL || enc == ShaderEncoding::Slang)
  {
    src =
        lit("float4 main(float4 pos : SV_Position, float4 uv : TEXCOORD0) : SV_Target0\n"
            "{\n"
            "    return float4(0,0,0,1);\n"
            "}\n");
  }
  else if(enc == ShaderEncoding::GLSL)
  {
    src =
        lit("#version 420 core\n\n"
            "layout (location = 0) in vec2 uv;\n\n"
            "layout (location = 0) out vec4 color_out;\n\n"
            "void main()\n"
            "{\n"
            "    color_out = vec4(0,0,0,1);\n"
            "}\n");
  }
  else if(enc == ShaderEncoding::SPIRVAsm || enc == ShaderEncoding::OpenGLSPIRVAsm)
  {
    src = lit("; SPIR-V");
  }
  else
  {
    src = tr("Unknown format - no template available");
  }

  QString path = QDir::cleanPath(QDir(ConfigFilePath(QString())).absoluteFilePath(filename));
  QFile fileHandle(path);
  if(fileHandle.open(QFile::WriteOnly | QIODevice::Truncate | QIODevice::Text))
  {
    fileHandle.write(src.toUtf8());
    fileHandle.close();
  }
  else
  {
    RDDialog::critical(
        this, tr("Cannot create shader"),
        tr("Couldn't create file for shader %1\n%2").arg(filename).arg(fileHandle.errorString()));
  }

  // auto-open edit window
  on_customEdit_clicked();

  reloadCustomShaders(filename);
}

void TextureViewer::on_customEdit_clicked()
{
  QString filename = ui->customShader->currentText();
  QString key = filename.toUpper();

  if(filename.isEmpty())
  {
    RDDialog::critical(this, tr("Error Editing Shader"),
                       tr("No shader selected.\nSelect a custom shader from the drop-down"));
    return;
  }

  QString path = getShaderPath(filename);
  QString src;

  QFile fileHandle(path);
  if(fileHandle.open(QFile::ReadOnly | QFile::Text))
  {
    QTextStream stream(&fileHandle);
    src = stream.readAll();
    fileHandle.close();
  }
  else
  {
    RDDialog::critical(
        this, tr("Cannot open shader"),
        tr("Couldn't open file for shader %1\n%2").arg(filename).arg(fileHandle.errorString()));
    return;
  }

  rdcstrpairs files;
  files.push_back({filename, src});

  QPointer<TextureViewer> thisPointer(this);

  IShaderViewer *s = m_Ctx.EditShader(
      ResourceId(), ShaderStage::Fragment, lit("main"), files, KnownShaderTool::Unknown,
      encodingExtensions[QFileInfo(filename).completeSuffix()], ShaderCompileFlags(),
      // Save Callback
      [thisPointer, key, filename, path](ICaptureContext *ctx, IShaderViewer *viewer, ResourceId,
                                         ShaderStage, ShaderEncoding, ShaderCompileFlags, rdcstr,
                                         bytebuf) {
        {
          // don't trigger a full refresh
          if(thisPointer)
            thisPointer->m_CustomShaderWriteTime = thisPointer->m_CustomShaderTimer.elapsed();

          rdcstrpairs files = viewer->GetCurrentFileContents();

          if(files.size() != 1)
            qCritical() << "Unexpected number of files in custom shader viewer" << files.count();

          if(files.empty())
            return;

          QFile fileHandle(path);
          if(fileHandle.open(QFile::WriteOnly | QIODevice::Truncate | QIODevice::Text))
          {
            fileHandle.write(files[0].second.c_str(), files[0].second.size());
            fileHandle.close();

            // watcher doesn't trigger on internal modifications
            if(thisPointer)
              thisPointer->reloadCustomShaders(filename);
          }
          else
          {
            if(thisPointer)
            {
              RDDialog::critical(
                  thisPointer, tr("Cannot save shader"),
                  tr("Couldn't save file for shader %1\n%2").arg(filename).arg(fileHandle.errorString()));
            }
          }
        }
      },

      [thisPointer, key](ICaptureContext *, IShaderViewer *, ResourceId) {
        if(thisPointer)
          thisPointer->m_CustomShaderEditor.remove(key);
      });

  m_CustomShaderEditor[key] = s;

  m_Ctx.AddDockWindow(s->Widget(), DockReference::AddTo, this);
}

void TextureViewer::on_customDelete_clicked()
{
  QString shaderName = ui->customShader->currentText();

  if(shaderName.isEmpty())
  {
    RDDialog::critical(this, tr("Error Deleting Shader"),
                       tr("No shader selected.\nSelect a custom shader from the drop-down"));
    return;
  }

  if(!m_CustomShaders.contains(shaderName.toUpper()))
  {
    RDDialog::critical(
        this, tr("Error Deleting Shader"),
        tr("Selected shader doesn't exist.\nSelect a custom shader from the drop-down"));
    return;
  }

  QMessageBox::StandardButton res =
      RDDialog::question(this, tr("Deleting Custom Shader"),
                         tr("Really delete %1?").arg(shaderName), RDDialog::YesNoCancel);

  if(res == QMessageBox::Yes)
  {
    QString path = getShaderPath(shaderName);

    if(!QFileInfo::exists(path))
    {
      RDDialog::critical(
          this, tr("Error Deleting Shader"),
          tr("Shader file %1 can't be found.\nSelect a custom shader from the drop-down")
              .arg(shaderName));
      return;
    }

    if(!QFile::remove(path))
    {
      RDDialog::critical(this, tr("Error Deleting Shader"),
                         tr("Error deleting shader %1 from disk").arg(shaderName));
      return;
    }

    ui->customShader->setCurrentText(QString());
    UI_UpdateChannels();
    reloadCustomShaders(QString());
  }
}

void TextureViewer::customShaderModified(const QString &path)
{
  static bool recurse = false;

  if(recurse)
    return;

  // if we just wrote a shader less than 100ms ago, don't refresh - this will have been handled
  // internally at a finer granularity than 'all shaders'
  if(m_CustomShaderWriteTime > m_CustomShaderTimer.elapsed() - 100)
    return;

  recurse = true;

  // allow time for modifications to finish
  QThread::msleep(15);

  reloadCustomShaders(QString());

  recurse = false;
}

#if ENABLE_UNIT_TESTS

#include "3rdparty/catch/catch.hpp"

TEST_CASE("Texture presentation completion requires the rendered canvas dimensions",
          "[texture][publication]")
{
  // The real tab race rendered while the native child was still 674x722, then show() delivered
  // the current tab's deferred resize to 674x957. EID/ResourceId alone cannot certify that frame.
  const rdcpair<int32_t, int32_t> historyCanvas = {674, 722};
  const QSize currentCanvas(674, 957);
  CHECK_FALSE(TexturePresentationMatchesCanvas(historyCanvas, currentCanvas));
  CHECK(TexturePresentationMatchesCanvas({674, 957}, currentCanvas));
  CHECK_FALSE(TexturePresentationMatchesCanvas({674, 957}, QSize(675, 957)));
  CHECK_FALSE(TexturePresentationMatchesCanvas({0, 0}, QSize(0, 0)));
  CHECK_FALSE(TexturePresentationMatchesCanvas({-1, 957}, QSize(-1, 957)));
}

// helper to avoid needing to test every possibility, which being O(n^2) up to with n=65536 can
// still be a bit slow.
// This can be disabled to exhaustively test when changing the function

TEST_CASE("mip co-ordinate helpers", "[helpers]")
{
  for(uint32_t dim = 0; dim < 65536;)
  {
    const uint32_t numMips = (uint32_t)floor(log2(double(dim)));

    // last mip coord seen
    uint32_t lastCoord[16] = {};

    for(uint32_t coord = 0; coord < dim; coord++)
    {
      // do manual checks so that this is fast. If we use a CHECK() for these this is orders of
      // magnitude slower
      if(BaseCoordFromMip(coord, 0, dim) != coord)
      {
        INFO(coord);
        INFO(dim);
        FAIL("BaseCoordFromMip isn't identity on mip 0");
      }
      if(MipCoordFromBase(coord, 0, dim) != coord)
      {
        INFO(coord);
        INFO(dim);
        FAIL("MipCoordFromBase isn't identity on mip 0");
      }

      for(uint32_t mip = 1; mip < numMips; mip++)
      {
        uint32_t mc = MipCoordFromBase(coord, mip, dim);

        if(mc != lastCoord[mip] && mc != lastCoord[mip] + 1)
        {
          INFO(coord);
          INFO(dim);
          INFO(mip);
          FAIL("MipCoordFromBase isn't continuous");
        }

        lastCoord[mip] = mc;
      }

      // for power of two textures we can do some extra tests
      if((dim & (dim - 1)) == 0)
      {
        // any co-ordinate that's divisible by a power of two up to that mip level should be
        // reflexive on that mip. E.g. on a 16x16 texture 0,0 at mip 0 should map 1:1 with 0,0 on
        // mip 1
        // and similarly 2,2 on mip 0 should map to 1,1 on mip 1 and back again
        for(uint32_t mip = 1; mip < numMips; mip++)
        {
          uint32_t pow2 = 1U << mip;
          if((coord % pow2) == 0)
          {
            if(BaseCoordFromMip(MipCoordFromBase(coord, mip, dim), mip, dim) != coord)
            {
              INFO(coord);
              INFO(dim);
              FAIL("MipCoordFromBase isn't reflexive on mip when lined up");
            }
          }
        }
      }
    }

    for(uint32_t mip = 1; mip < numMips; mip++)
    {
      uint32_t mipDim = qMax(1U, dim >> mip);

      if(lastCoord[mip] != mipDim - 1)
      {
        INFO(lastCoord[mip]);
        INFO(dim);
        INFO(mip);
        FAIL("not all mip co-ords are mapped to");
      }
    }

// exhaustive test
#if 0
    dim++;
#else
    if(dim < 8192)
    {
      dim++;
    }
    else if(dim < 16384)
    {
      if((dim % 2) == 0)
        dim++;
      else
        dim += 2;
    }
    else if(dim < 65536)
    {
      if((dim % 2) == 0)
        dim++;
      else
        dim += 8;
    }
#endif
  }
};

#endif
