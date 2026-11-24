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

#include "PipelineStateViewer.h"
#include <QApplication>
#include <QClipboard>
#include <QJsonDocument>
#include <QJsonObject>
#include <QMenu>
#include <QMouseEvent>
#include <QPainter>
#include <QPointer>
#include <QScrollBar>
#include <QStylePainter>
#include <QSvgRenderer>
#include <QToolButton>
#include <QTimer>
#include <QXmlStreamWriter>
#include <memory>
#include "Code/CaptureContext.h"
#include "Code/QRDUtils.h"
#include "Code/ReplayLab/ReplayLabIntegration.h"
#include "Code/ReplayLab/ReplayLabAIResourceAttachment.h"
#include "Code/Resources.h"
#include "Windows/AI/AIAssistant.h"
#include "Windows/RenderTargetHistoryView.h"
#include "Widgets/Extended/RDLabel.h"
#include "Widgets/Extended/RDTreeWidget.h"
#include "toolwindowmanager/ToolWindowManager.h"
#include "D3D11PipelineStateViewer.h"
#include "D3D12PipelineStateViewer.h"
#include "GLPipelineStateViewer.h"
#include "VulkanPipelineStateViewer.h"
#include "ui_PipelineStateViewer.h"

static uint32_t byteSize(const ResourceFormat &fmt)
{
  if(fmt.Special())
  {
    switch(fmt.type)
    {
      default:
      case ResourceFormatType::R9G9B9E5:
      case ResourceFormatType::R5G6B5:
      case ResourceFormatType::R5G5B5A1:
      case ResourceFormatType::R4G4B4A4:
      case ResourceFormatType::R4G4:
      case ResourceFormatType::BC1:
      case ResourceFormatType::BC2:
      case ResourceFormatType::BC3:
      case ResourceFormatType::BC4:
      case ResourceFormatType::BC5:
      case ResourceFormatType::BC6:
      case ResourceFormatType::BC7:
      case ResourceFormatType::ETC2:
      case ResourceFormatType::EAC:
      case ResourceFormatType::ASTC:
      case ResourceFormatType::D16S8:
      case ResourceFormatType::D24S8:
      case ResourceFormatType::D32S8:
      case ResourceFormatType::S8:
      case ResourceFormatType::YUV8:
      case ResourceFormatType::YUV10:
      case ResourceFormatType::YUV12:
      case ResourceFormatType::YUV16:
      case ResourceFormatType::PVRTC: return ~0U;
      case ResourceFormatType::A8: return 1;
      case ResourceFormatType::R10G10B10A2:
      case ResourceFormatType::R11G11B10: return 4;
    }
  }

  return fmt.compByteWidth * fmt.compCount;
}

RDPreviewTooltip::RDPreviewTooltip(PipelineStateViewer *parent, CustomPaintWidget *thumbnail,
                                   ICaptureContext &ctx)
    : QFrame(parent), m_Ctx(ctx)
{
  int margin = style()->pixelMetric(QStyle::PM_ToolTipLabelFrameWidth, NULL, this);
  int opacity = style()->styleHint(QStyle::SH_ToolTipLabel_Opacity, NULL, this);

  pipe = parent;

  setWindowFlags(Qt::ToolTip | Qt::WindowStaysOnTopHint);
  setAttribute(Qt::WA_TransparentForMouseEvents);
  setForegroundRole(QPalette::ToolTipText);
  setBackgroundRole(QPalette::ToolTipBase);
  setFrameStyle(QFrame::NoFrame);
  setWindowOpacity(opacity / 255.0);
  setSizePolicy(QSizePolicy::Minimum, QSizePolicy::Minimum);

  QHBoxLayout *hbox = new QHBoxLayout;
  QVBoxLayout *vbox = new QVBoxLayout;
  hbox->setSpacing(0);
  hbox->setContentsMargins(0, 0, 0, 0);
  vbox->setSpacing(2);
  vbox->setContentsMargins(6, 3, 6, 3);

  label = new QLabel(this);
  label->setAlignment(Qt::AlignLeft);

  title = new QLabel(this);
  title->setAlignment(Qt::AlignLeft);

  setLayout(vbox);
  vbox->addWidget(title);
  vbox->addLayout(hbox);

  hbox->addWidget(thumbnail);
  hbox->addStretch();

  vbox->addWidget(label);
}

void RDPreviewTooltip::hideTip()
{
  hide();
}

QSize RDPreviewTooltip::configureTip(QWidget *widget, QString text, QModelIndex idx)
{
  ResourceId id = pipe->updateThumbnail(widget, idx);
  if(id != ResourceId())
  {
    title->setText(m_Ctx.GetResourceName(id));
    title->show();
  }
  else
  {
    title->hide();
  }
  label->setText(text);
  label->setVisible(!text.isEmpty());
  layout()->update();
  layout()->activate();
  return minimumSizeHint();
}

void RDPreviewTooltip::showTipAtPos(QPoint pos)
{
  move(pos);
  resize(minimumSize());
  show();
}

bool RDPreviewTooltip::forceTip(QWidget *widget, QModelIndex idx)
{
  return pipe->hasThumbnail(widget, idx);
}

void RDPreviewTooltip::paintEvent(QPaintEvent *ev)
{
  QStylePainter p(this);
  QStyleOptionFrame opt;
  opt.init(this);
  p.drawPrimitive(QStyle::PE_PanelTipLabel, opt);
  p.end();

  QWidget::paintEvent(ev);
}

void RDPreviewTooltip::resizeEvent(QResizeEvent *e)
{
  QStyleHintReturnMask frameMask;
  QStyleOption option;
  option.init(this);
  if(style()->styleHint(QStyle::SH_ToolTip_Mask, &option, this, &frameMask))
    setMask(frameMask.region);

  QWidget::resizeEvent(e);
}

PipelineStateViewer::PipelineStateViewer(ICaptureContext &ctx, QWidget *parent)
    : QFrame(parent), ui(new Ui::PipelineStateViewer), m_Ctx(ctx)
{
  ui->setupUi(this);

  m_DetailsTimer = new QTimer(this);
  m_DetailsTimer->setSingleShot(true);
  m_DetailsTimer->setInterval(75);
  m_DetailsNotice = new QLabel(tr("Pipeline details update after replay interaction pauses."), this);
  m_DetailsNotice->setMargin(4);
  m_DetailsNotice->setAutoFillBackground(true);
  m_DetailsNotice->setBackgroundRole(QPalette::ToolTipBase);
  m_DetailsNotice->setForegroundRole(QPalette::ToolTipText);
  m_DetailsNotice->adjustSize();
  m_DetailsNotice->move(4, 4);
  m_DetailsNotice->hide();
  QObject::connect(m_DetailsTimer, &QTimer::timeout, this, [this]() {
    if(!m_Ctx.IsCaptureLoaded() || !isVisible())
      return;
    if(m_Ctx.IsScrubbing())
      m_DetailsTimer->start();
    else if(m_DetailsPending)
      OnEventChanged(m_Ctx.CurEvent());
  });

  ui->thumbnail->SetContext(m_Ctx);
  ui->layout->removeWidget(ui->thumbnail);

  m_Tooltip = new RDPreviewTooltip(this, ui->thumbnail, m_Ctx);

  QColor c = palette().color(QPalette::ToolTipBase).toRgb();

  m_TexDisplay.backgroundColor = FloatVector();
  m_TexDisplay.backgroundColor.w = 1.0f;

  // auto-fit and center scale
  m_TexDisplay.scale = -1.0f;

  m_D3D11 = NULL;
  m_D3D12 = NULL;
  m_GL = NULL;
  m_Vulkan = NULL;

  m_Current = NULL;

  for(size_t i = 0; i < ARRAY_COUNT(editMenus); i++)
    editMenus[i] = new QMenu(this);

  m_Ctx.AddCaptureViewer(this);
}

PipelineStateViewer::~PipelineStateViewer()
{
  reset();

  m_Ctx.BuiltinWindowClosed(this);
  m_Ctx.RemoveCaptureViewer(this);

  delete m_Tooltip;

  delete ui;
}

void PipelineStateViewer::OnCaptureLoaded()
{
  if(m_Ctx.APIProps().pipelineType == GraphicsAPI::D3D11)
    setToD3D11();
  else if(m_Ctx.APIProps().pipelineType == GraphicsAPI::D3D12)
    setToD3D12();
  else if(m_Ctx.APIProps().pipelineType == GraphicsAPI::OpenGL)
    setToGL();
  else if(m_Ctx.APIProps().pipelineType == GraphicsAPI::Vulkan)
    setToVulkan();

  if(m_Current)
    m_Current->OnCaptureLoaded();

  if(!m_Ctx.APIProps().remoteReplay)
  {
    WindowingData thumbData = ui->thumbnail->GetWidgetWindowingData();

    m_Ctx.Replay().BlockInvoke([thumbData, this](IReplayController *r) {
      m_Output = r->CreateOutput(thumbData, ReplayOutputType::Texture);

      ui->thumbnail->SetOutput(m_Output);

      RT_UpdateAndDisplay(r);
    });
  }
  else
  {
    m_Output = NULL;
  }
}

void PipelineStateViewer::RT_UpdateAndDisplay(IReplayController *r)
{
  if(m_Output != NULL)
  {
    m_Output->SetTextureDisplay(m_TexDisplay);

    GUIInvoke::call(this, [this]() { ui->thumbnail->update(); });
  }
}

void PipelineStateViewer::OnCaptureClosed()
{
  m_DetailsTimer->stop();
  if(m_DetailsPending)
    setToolTip(m_PreviousToolTip);
  m_DetailsPending = false;
  setAttribute(Qt::WA_TransparentForMouseEvents, false);
  m_DetailsNotice->hide();
  // i18n-ignore: native publication evidence, never a visible label.
  setProperty("replayDetailsPending", false);
  if(m_Current)
    m_Current->OnCaptureClosed();
}

void PipelineStateViewer::OnEventChanged(uint32_t eventId)
{
  RENDERDOC_PROFILEFUNCTION();

  if(m_Ctx.IsScrubbing() || !isVisible())
  {
    DeferDetails();
    return;
  }
  m_DetailsTimer->stop();
  if(m_DetailsPending)
    setToolTip(m_PreviousToolTip);
  m_DetailsPending = false;
  setAttribute(Qt::WA_TransparentForMouseEvents, false);
  m_DetailsNotice->hide();
  // i18n-ignore: native publication evidence, never a visible label.
  setProperty("replayDetailsPending", false);

  if(m_Ctx.APIProps().pipelineType == GraphicsAPI::D3D11)
    setToD3D11();
  else if(m_Ctx.APIProps().pipelineType == GraphicsAPI::D3D12)
    setToD3D12();
  else if(m_Ctx.APIProps().pipelineType == GraphicsAPI::OpenGL)
    setToGL();
  else if(m_Ctx.APIProps().pipelineType == GraphicsAPI::Vulkan)
    setToVulkan();

  if(m_Current)
    m_Current->OnEventChanged(eventId);
}

void PipelineStateViewer::DeferDetails()
{
  if(!m_DetailsPending)
  {
    m_PreviousToolTip = toolTip();
    setToolTip(tr("Pipeline details update after replay interaction pauses."));
  }
  m_DetailsPending = true;
  // Do not recursively enable/disable hundreds of child widgets: that invalidates Qt styles
  // and size hints on every pointer gesture. A local notice guards delayed details instead.
  setAttribute(Qt::WA_TransparentForMouseEvents, true);
  m_DetailsNotice->show();
  m_DetailsNotice->raise();
  // i18n-ignore: native publication evidence, never a visible label.
  setProperty("replayDetailsPending", true);
  if(isVisible())
    m_DetailsTimer->start();
}

void PipelineStateViewer::OnScrubEvent(uint32_t eventId)
{
  // The old details still correspond to the currently displayed frame until a new event is
  // published. Only OnEventChanged may mark those details pending; input itself must stay cheap.
  m_DetailsTimer->stop();
}

QString PipelineStateViewer::GetCurrentAPI()
{
  if(m_Current == m_D3D11)
    return lit("D3D11");
  else if(m_Current == m_D3D12)
    return lit("D3D12");
  else if(m_Current == m_GL)
    return lit("OpenGL");
  else if(m_Current == m_Vulkan)
    return lit("Vulkan");

  return lit("");
}

QVariant PipelineStateViewer::persistData()
{
  QVariantMap state;

  state[lit("type")] = GetCurrentAPI();

  return state;
}

void PipelineStateViewer::setPersistData(const QVariant &persistData)
{
  QString str = persistData.toMap()[lit("type")].toString();

  if(str == lit("D3D11"))
    setToD3D11();
  else if(str == lit("D3D12"))
    setToD3D12();
  else if(str == lit("GL"))
    setToGL();
  else if(str == lit("Vulkan"))
    setToVulkan();
}

void PipelineStateViewer::reset()
{
  delete m_D3D11;
  delete m_D3D12;
  delete m_GL;
  delete m_Vulkan;

  m_D3D11 = NULL;
  m_D3D12 = NULL;
  m_GL = NULL;
  m_Vulkan = NULL;

  m_Current = NULL;
}

void PipelineStateViewer::setToD3D11()
{
  if(m_D3D11)
    return;

  reset();

  m_D3D11 = new D3D11PipelineStateViewer(m_Ctx, *this, this);
  ui->layout->addWidget(m_D3D11);
  m_Current = m_D3D11;
}

void PipelineStateViewer::setToD3D12()
{
  if(m_D3D12)
    return;

  reset();

  m_D3D12 = new D3D12PipelineStateViewer(m_Ctx, *this, this);
  ui->layout->addWidget(m_D3D12);
  m_Current = m_D3D12;
}

void PipelineStateViewer::setToGL()
{
  if(m_GL)
    return;

  reset();

  m_GL = new GLPipelineStateViewer(m_Ctx, *this, this);
  ui->layout->addWidget(m_GL);
  m_Current = m_GL;
}

void PipelineStateViewer::setToVulkan()
{
  if(m_Vulkan)
    return;

  reset();

  m_Vulkan = new VulkanPipelineStateViewer(m_Ctx, *this, this);
  ui->layout->addWidget(m_Vulkan);
  m_Current = m_Vulkan;
}

QXmlStreamWriter *PipelineStateViewer::beginHTMLExport()
{
  if(!m_Ctx.IsCaptureLoaded())
    return NULL;

  QString filename = RDDialog::getSaveFileName(this, tr("Export pipeline state as HTML"), QString(),
                                               tr("HTML files (*.html)"));

  if(!filename.isEmpty())
  {
    ANALYTIC_SET(Export.PipelineState, true);

    QDir dirinfo = QFileInfo(filename).dir();
    if(dirinfo.exists())
    {
      QFile *f = new QFile(filename, this);
      if(f->open(QIODevice::WriteOnly | QIODevice::Truncate))
      {
        QXmlStreamWriter *xmlptr = new QXmlStreamWriter(f);

        QXmlStreamWriter &xml = *xmlptr;

        xml.setAutoFormatting(true);
        xml.setAutoFormattingIndent(4);
        xml.writeStartDocument();
        xml.writeDTD(lit("<!DOCTYPE html>"));

        xml.writeStartElement(lit("html"));
        xml.writeAttribute(lit("lang"), lit("en"));

        QString title = tr("%1 EID %2 - %3 Pipeline export")
                            .arg(QFileInfo(m_Ctx.GetCaptureFilename()).fileName())
                            .arg(m_Ctx.CurEvent())
                            .arg(GetCurrentAPI());

        {
          xml.writeStartElement(lit("head"));

          xml.writeStartElement(lit("meta"));
          xml.writeAttribute(lit("charset"), lit("utf-8"));
          xml.writeEndElement();

          xml.writeStartElement(lit("meta"));
          xml.writeAttribute(lit("http-equiv"), lit("X-UA-Compatible"));
          xml.writeAttribute(lit("content"), lit("IE=edge"));
          xml.writeEndElement();

          xml.writeStartElement(lit("meta"));
          xml.writeAttribute(lit("name"), lit("viewport"));
          xml.writeAttribute(lit("content"), lit("width=device-width, initial-scale=1"));
          xml.writeEndElement();

          xml.writeStartElement(lit("meta"));
          xml.writeAttribute(lit("name"), lit("description"));
          xml.writeAttribute(lit("content"), lit(""));
          xml.writeEndElement();

          xml.writeStartElement(lit("meta"));
          xml.writeAttribute(lit("name"), lit("author"));
          xml.writeAttribute(lit("content"), lit(""));
          xml.writeEndElement();

          xml.writeStartElement(lit("meta"));
          xml.writeAttribute(lit("http-equiv"), lit("Content-Type"));
          xml.writeAttribute(lit("content"), lit("text/html;charset=utf-8"));
          xml.writeEndElement();

          xml.writeStartElement(lit("title"));
          xml.writeCharacters(title);
          xml.writeEndElement();

          xml.writeStartElement(lit("style"));
          xml.writeComment(lit(R"(

/* If you think this css is ugly/bad, open a pull request! */
body { margin: 20px; }
div.stage { border: 1px solid #BBBBBB; border-radius: 5px; padding: 16px; margin-bottom: 32px; }
div.stage h1 { text-decoration: underline; margin-top: 0px; }
div.stage table { border: 1px solid #AAAAAA; border-collapse: collapse; }
div.stage table thead tr { border-bottom: 1px solid #AAAAAA; background-color: #EEEEFF; }
div.stage table tr th { border-right: 1px solid #AAAAAA; padding: 6px; }
div.stage table tr td { border-right: 1px solid #AAAAAA; background-color: #EEEEEE; padding: 3px; }

)"));
          xml.writeEndElement();    // </style>

          xml.writeEndElement();    // </head>
        }

        {
          xml.writeStartElement(lit("body"));

          xml.writeStartElement(lit("h1"));
          xml.writeCharacters(title);
          xml.writeEndElement();

          xml.writeStartElement(lit("h3"));
          {
            uint32_t frameNumber = m_Ctx.FrameInfo().frameNumber;

            QString context = frameNumber == ~0U ? tr("Capture") : tr("Frame %1").arg(frameNumber);

            const ActionDescription *action = m_Ctx.CurAction();

            QList<const ActionDescription *> actionstack;
            const ActionDescription *parent = action ? action->parent : NULL;
            while(parent)
            {
              actionstack.push_front(parent);
              parent = parent->parent;
            }

            for(const ActionDescription *d : actionstack)
            {
              context += QFormatStr(" > %1").arg(d->customName);
            }

            if(action)
              context +=
                  QFormatStr(" => %1").arg(m_Ctx.GetEventBrowser()->GetEventName(action->eventId));
            else
              context += tr(" => Capture Start");

            xml.writeCharacters(context);
          }
          xml.writeEndElement();    // </h3>
        }

        // body is open

        return xmlptr;
      }

      RDDialog::critical(
          this, tr("Error exporting pipeline state"),
          tr("Couldn't open path %1 for write.\n%2").arg(filename).arg(f->errorString()));

      delete f;

      return NULL;
    }
    else
    {
      RDDialog::critical(this, tr("Invalid directory"),
                         tr("Cannot find target directory to save to"));
      return NULL;
    }
  }

  return NULL;
}

void PipelineStateViewer::exportHTMLTable(QXmlStreamWriter &xml, const QStringList &cols,
                                          const QList<QVariantList> &rows)
{
  xml.writeStartElement(lit("table"));

  {
    xml.writeStartElement(lit("thead"));
    xml.writeStartElement(lit("tr"));

    for(const QString &col : cols)
    {
      xml.writeStartElement(lit("th"));
      xml.writeCharacters(col);
      xml.writeEndElement();
    }

    xml.writeEndElement();
    xml.writeEndElement();
  }

  {
    xml.writeStartElement(lit("tbody"));

    if(rows.isEmpty())
    {
      xml.writeStartElement(lit("tr"));

      for(int i = 0; i < cols.count(); i++)
      {
        xml.writeStartElement(lit("td"));
        xml.writeCharacters(lit("-"));
        xml.writeEndElement();
      }

      xml.writeEndElement();
    }
    else
    {
      for(const QVariantList &row : rows)
      {
        xml.writeStartElement(lit("tr"));

        for(const QVariant &el : row)
        {
          xml.writeStartElement(lit("td"));

          if(el.type() == QVariant::Bool)
            xml.writeCharacters(el.toBool() ? tr("True") : tr("False"));
          else
            xml.writeCharacters(el.toString());

          xml.writeEndElement();
        }

        xml.writeEndElement();
      }
    }

    xml.writeEndElement();
  }

  xml.writeEndElement();
}

void PipelineStateViewer::exportHTMLTable(QXmlStreamWriter &xml, const QStringList &cols,
                                          const QVariantList &row)
{
  exportHTMLTable(xml, cols, QList<QVariantList>({row}));
}

void PipelineStateViewer::endHTMLExport(QXmlStreamWriter *xml)
{
  xml->writeEndElement();    // </body>

  xml->writeEndElement();    // </html>

  xml->writeEndDocument();

  // delete the file the writer was writing to
  QFile *f = qobject_cast<QFile *>(xml->device());
  delete f;

  delete xml;
}

void PipelineStateViewer::setTopologyDiagram(QLabel *diagram, Topology topo)
{
  int idx = qMin((int)topo, (int)Topology::PatchList);

  if(m_TopoPixmaps[idx].isNull())
  {
    QSvgRenderer svg;
    switch(topo)
    {
      case Topology::PointList: svg.load(lit(":/topologies/topo_pointlist.svg")); break;
      case Topology::LineList: svg.load(lit(":/topologies/topo_linelist.svg")); break;
      case Topology::LineStrip: svg.load(lit(":/topologies/topo_linestrip.svg")); break;
      case Topology::TriangleList: svg.load(lit(":/topologies/topo_trilist.svg")); break;
      case Topology::TriangleStrip: svg.load(lit(":/topologies/topo_tristrip.svg")); break;
      case Topology::LineList_Adj: svg.load(lit(":/topologies/topo_linelist_adj.svg")); break;
      case Topology::LineStrip_Adj: svg.load(lit(":/topologies/topo_linestrip_adj.svg")); break;
      case Topology::TriangleList_Adj: svg.load(lit(":/topologies/topo_trilist_adj.svg")); break;
      case Topology::TriangleStrip_Adj: svg.load(lit(":/topologies/topo_tristrip_adj.svg")); break;
      default: svg.load(lit(":/topologies/topo_patch.svg")); break;
    }

    QRect rect = svg.viewBox();

    QImage im(rect.size() * diagram->devicePixelRatio(), QImage::Format_ARGB32);

    im.fill(QColor(0, 0, 0, 0));

    QPainter p(&im);

    svg.render(&p);

    // convert the colors - black maps to Text (foreground) and white maps to Base (background)
    QColor white = diagram->palette().color(QPalette::Active, QPalette::Base);
    QColor black = diagram->palette().color(QPalette::Active, QPalette::Text);

    const float br = black.redF();
    const float bg = black.greenF();
    const float bb = black.blueF();

    const float wr = white.redF();
    const float wg = white.greenF();
    const float wb = white.blueF();

    for(int y = 0; y < im.height(); y++)
    {
      QRgb *line = (QRgb *)im.scanLine(y);

      for(int x = 0; x < im.width(); x++)
      {
        // delta of 0 is black, delta of 255 is white
        const float delta = float(qRed(*line));
        const float bd = 255.0f - delta;
        const float wd = delta;

        const int r = int(br * bd + wr * wd);
        const int g = int(bg * bd + wg * wd);
        const int b = int(bb * bd + wb * wd);

        *line = qRgba(r, g, b, qAlpha(*line));

        line++;
      }
    }

    m_TopoPixmaps[idx] = QPixmap::fromImage(im);
    m_TopoPixmaps[idx].setDevicePixelRatio(diagram->devicePixelRatioF());
  }

  diagram->setPixmap(m_TopoPixmaps[idx]);
}

void PipelineStateViewer::setMeshViewPixmap(RDLabel *meshView)
{
  QImage meshIcon = Pixmaps::wireframe_mesh(meshView->devicePixelRatio()).toImage();
  QImage colSwapped(meshIcon.size(), QImage::Format_ARGB32);
  colSwapped.fill(meshView->palette().color(QPalette::WindowText));

  for(int y = 0; y < meshIcon.height(); y++)
  {
    const QRgb *in = (const QRgb *)meshIcon.constScanLine(y);
    QRgb *out = (QRgb *)colSwapped.scanLine(y);

    for(int x = 0; x < meshIcon.width(); x++)
    {
      *out = qRgba(qRed(*out), qGreen(*out), qBlue(*out), qAlpha(*in));

      in++;
      out++;
    }
  }

  QPixmap p = QPixmap::fromImage(colSwapped);
  p.setDevicePixelRatio(meshView->devicePixelRatioF());

  meshView->setPixmap(p);
  meshView->setPreserveAspectRatio(true);

  QPalette pal = meshView->palette();
  pal.setColor(QPalette::Shadow, pal.color(QPalette::Window).darker(120));
  meshView->setPalette(pal);
  meshView->setBackgroundRole(QPalette::Window);
  meshView->setMouseTracking(true);

  QObject::connect(meshView, &RDLabel::mouseMoved, [meshView](QMouseEvent *) {
    meshView->setBackgroundRole(QPalette::Shadow);
    meshView->setAutoFillBackground(true);
  });
  QObject::connect(meshView, &RDLabel::leave, [meshView]() {
    meshView->setBackgroundRole(QPalette::Window);
    meshView->setAutoFillBackground(false);
  });
}

namespace
{
struct HLSLAbiSkeleton
{
  QString source;
  QStringList diagnostics;
};

static void AddSkeletonDiagnostic(QStringList &diagnostics, const QString &diagnostic)
{
  if(!diagnostic.isEmpty() && !diagnostics.contains(diagnostic))
    diagnostics.push_back(diagnostic);
}

static bool IsHLSLIdentifierChar(QChar c, bool first)
{
  const ushort ch = c.unicode();
  const bool alpha = (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z');
  return alpha || ch == '_' || (!first && ch >= '0' && ch <= '9');
}

static bool IsHLSLKeyword(const QString &identifier)
{
  static const QSet<QString> keywords = {
      lit("asm"),       lit("bool"),      lit("break"),      lit("buffer"),
      lit("byteaddressbuffer"),            lit("case"),       lit("cbuffer"),
      lit("class"),     lit("column_major"),                 lit("compile"),
      lit("const"),     lit("continue"),  lit("default"),    lit("discard"),
      lit("do"),        lit("double"),    lit("else"),       lit("false"),
      lit("float"),     lit("for"),       lit("half"),       lit("if"),
      lit("in"),        lit("inline"),    lit("inout"),      lit("int"),
      lit("interface"), lit("matrix"),    lit("namespace"),  lit("out"),
      lit("register"),  lit("return"),    lit("row_major"),  lit("sampler"),
      lit("samplerstate"),                 lit("shared"),     lit("static"),
      lit("struct"),    lit("switch"),    lit("texture"),    lit("true"),
      lit("typedef"),   lit("uint"),      lit("uniform"),    lit("vector"),
      lit("void"),      lit("volatile"),  lit("while"),
  };

  return keywords.contains(identifier.toLower());
}

static QString SanitiseHLSLIdentifier(const QString &candidate, const QString &fallback)
{
  QString ret;
  ret.reserve(candidate.size() + 1);

  // Preserve a leading digit by prefixing '_' instead of replacing the digit. This keeps captured
  // names recognisable while still producing a legal HLSL identifier.
  if(!candidate.isEmpty() && candidate[0] >= QLatin1Char('0') &&
     candidate[0] <= QLatin1Char('9'))
    ret = lit("_");

  for(int i = 0; i < candidate.size(); i++)
    ret += IsHLSLIdentifierChar(candidate[i], ret.isEmpty()) ? candidate[i] : QLatin1Char('_');

  if(ret.isEmpty())
    ret = fallback;

  if(!ret.isEmpty() && !IsHLSLIdentifierChar(ret[0], true))
    ret.prepend(QLatin1Char('_'));

  if(IsHLSLKeyword(ret))
    ret.prepend(QLatin1Char('_'));

  return ret;
}

static QString UniqueHLSLIdentifier(const QString &candidate, const QString &fallback,
                                    QSet<QString> &used)
{
  const QString base = SanitiseHLSLIdentifier(candidate, fallback);
  QString ret = base;
  int suffix = 2;

  while(used.contains(ret.toLower()))
    ret = base + lit("_%1").arg(suffix++);

  used.insert(ret.toLower());
  return ret;
}

static QString HLSLScalarType(VarType type)
{
  switch(type)
  {
    case VarType::Float: return lit("float");
    case VarType::Double: return lit("double");
    case VarType::Half: return lit("min16float");
    case VarType::SInt:
    case VarType::Enum: return lit("int");
    case VarType::UInt: return lit("uint");
    case VarType::SShort: return lit("min16int");
    case VarType::UShort: return lit("min16uint");
    case VarType::Bool: return lit("bool");
    default: break;
  }

  return QString();
}

static int AppendHLSLVariables(bool cbufferContents, const rdcarray<ShaderConstant> &vars,
                               QString &contents, QString &structDefs, QStringList &diagnostics,
                               const QString &scope, QSet<QString> &typeNames);

static QString HLSLConstantType(const ShaderConstantType &type, QString &structDefs,
                                QStringList &diagnostics, const QString &suggestedStructName,
                                QSet<QString> &typeNames)
{
  if(type.baseType == VarType::Struct)
  {
    if(type.members.isEmpty())
    {
      AddSkeletonDiagnostic(
          diagnostics,
          QObject::tr("%1 is reflected as a struct without any member layout.")
              .arg(suggestedStructName));
      return QString();
    }

    const QString structName =
        UniqueHLSLIdentifier(type.name, suggestedStructName + lit("_type"), typeNames);
    QString members;
    if(AppendHLSLVariables(false, type.members, members, structDefs, diagnostics, structName,
                           typeNames) == 0)
    {
      AddSkeletonDiagnostic(
          diagnostics,
          QObject::tr("%1 has no members with a representable HLSL type.").arg(structName));
      return QString();
    }

    structDefs += lit("struct %1 {\n%2};\n\n").arg(structName).arg(members);
    return structName;
  }

  QString base = HLSLScalarType(type.baseType);
  if(base.isEmpty())
  {
    AddSkeletonDiagnostic(
        diagnostics,
        QObject::tr("%1 has unrepresentable reflected type '%2'.")
            .arg(suggestedStructName)
            .arg(ToQStr(type.baseType)));
    return QString();
  }

  if(type.rows == 0 || type.columns == 0 || type.rows > 4 || type.columns > 4)
  {
    AddSkeletonDiagnostic(
        diagnostics,
        QObject::tr("%1 has invalid HLSL dimensions %2x%3.")
            .arg(suggestedStructName)
            .arg(type.rows)
            .arg(type.columns));
    return QString();
  }

  if(type.rows > 1 && type.columns > 1)
  {
    const QString major = type.RowMajor() ? lit("row_major ") : lit("column_major ");
    return major + base + QString::number(type.rows) + QLatin1Char('x') +
           QString::number(type.columns);
  }

  const uint32_t components = qMax(type.rows, type.columns);
  if(components > 1)
    base += QString::number(components);

  return base;
}

static int AppendHLSLVariables(bool cbufferContents, const rdcarray<ShaderConstant> &vars,
                               QString &contents, QString &structDefs, QStringList &diagnostics,
                               const QString &scope, QSet<QString> &typeNames)
{
  QSet<QString> fieldNames;
  int emitted = 0;

  for(int i = 0; i < vars.count(); i++)
  {
    const ShaderConstant &v = vars[i];
    const QString fallback = lit("field_%1_%2").arg(v.byteOffset, 0, 16).arg(i);
    const QString fieldName = UniqueHLSLIdentifier(v.name, fallback, fieldNames);
    const QString typeName = HLSLConstantType(v.type, structDefs, diagnostics,
                                              scope + QLatin1Char('_') + fieldName, typeNames);

    if(typeName.isEmpty())
    {
      contents += lit("\t// unresolved: %1 at byte offset 0x%2\n")
                      .arg(fieldName)
                      .arg(v.byteOffset, 0, 16);
      continue;
    }

    contents += lit("\t%1 %2").arg(typeName).arg(fieldName);

    if(v.type.elements > 1)
      contents += lit("[%1]").arg(v.type.elements);
    else if(v.type.elements == 0)
      AddSkeletonDiagnostic(
          diagnostics,
          QObject::tr("%1.%2 has an unknown/unbounded array size.").arg(scope).arg(fieldName));

    if(cbufferContents)
    {
      if((v.byteOffset % 4) != 0)
      {
        AddSkeletonDiagnostic(
            diagnostics,
            QObject::tr("%1.%2 starts at non-DWORD byte offset %3.")
                .arg(scope)
                .arg(fieldName)
                .arg(v.byteOffset));
      }

      const uint32_t dwordOffset = v.byteOffset / 4;
      const uint32_t vectorIndex = dwordOffset / 4;
      const uint32_t vectorComponent = dwordOffset % 4;
      const char components[] = {'x', 'y', 'z', 'w'};
      contents += lit(" : packoffset(c%1.%2)")
                      .arg(vectorIndex)
                      .arg(QLatin1Char(components[vectorComponent]));
    }

    contents += lit(";\n");
    emitted++;
  }

  return emitted;
}

static QString HLSLRegisterClause(char registerClass, uint32_t bindNumber, uint32_t space)
{
  if(space == 0)
    return lit("register(%1%2)").arg(QLatin1Char(registerClass)).arg(bindNumber);

  return lit("register(%1%2, space%3)")
      .arg(QLatin1Char(registerClass))
      .arg(bindNumber)
      .arg(space);
}

static QString HLSLResourceType(const ShaderResource &res, QString &structDefs,
                                QStringList &diagnostics, const QString &resourceName,
                                QSet<QString> &typeNames)
{
  if(res.descriptorType == DescriptorType::AccelerationStructure)
    return lit("RaytracingAccelerationStructure");

  const bool readWrite = IsReadWriteDescriptor(res.descriptorType) || !res.isReadOnly;

  if(!res.isTexture)
  {
    if(res.variableType.baseType == VarType::UByte && res.variableType.name == "byte")
      return readWrite ? lit("RWByteAddressBuffer") : lit("ByteAddressBuffer");

    const QString element = HLSLConstantType(res.variableType, structDefs, diagnostics,
                                             resourceName + lit("_element"), typeNames);
    if(element.isEmpty())
      return QString();

    if(res.descriptorType == DescriptorType::TypedBuffer ||
       res.descriptorType == DescriptorType::ReadWriteTypedBuffer)
      return (readWrite ? lit("RWBuffer<") : lit("Buffer<")) + element + QLatin1Char('>');

    return (readWrite ? lit("RWStructuredBuffer<") : lit("StructuredBuffer<")) + element +
           QLatin1Char('>');
  }

  const QString dimensions[arraydim<TextureType>()] = {
      QString(),            lit("Buffer"),      lit("Texture1D"),      lit("Texture1DArray"),
      lit("Texture2D"),    QString(),           lit("Texture2DArray"), lit("Texture2DMS"),
      lit("Texture2DMSArray"), lit("Texture3D"), lit("TextureCube"),   lit("TextureCubeArray"),
  };

  const QString dimension = dimensions[(size_t)res.textureType];
  if(dimension.isEmpty() || dimension == lit("Buffer"))
  {
    AddSkeletonDiagnostic(
        diagnostics,
        QObject::tr("%1 has an unsupported/unknown texture dimension.").arg(resourceName));
    return QString();
  }

  const QString element = HLSLConstantType(res.variableType, structDefs, diagnostics,
                                           resourceName + lit("_texel"), typeNames);
  if(element.isEmpty())
    return QString();

  return (readWrite ? lit("RW") : QString()) + dimension + QLatin1Char('<') + element +
         QLatin1Char('>');
}

static bool IsHLSLSemantic(const QString &semantic)
{
  if(semantic.isEmpty())
    return false;

  for(int i = 0; i < semantic.size(); i++)
    if(!IsHLSLIdentifierChar(semantic[i], i == 0))
      return false;

  return true;
}

static QString HLSLSignatureType(const SigParameter &sig)
{
  QString type = HLSLScalarType(sig.varType);
  if(type.isEmpty() || sig.compCount == 0 || sig.compCount > 4)
    return QString();

  if(sig.compCount > 1)
    type += QString::number(sig.compCount);
  return type;
}

static int AppendHLSLSignature(const rdcarray<SigParameter> &signature, const QString &prefix,
                               QString &contents, QStringList &diagnostics)
{
  QSet<QString> fieldNames;
  QSet<QString> semantics;
  int emitted = 0;

  for(int i = 0; i < signature.count(); i++)
  {
    const SigParameter &sig = signature[i];
    const QString semantic = D3DSemanticString(sig);
    const QString type = HLSLSignatureType(sig);

    if(type.isEmpty())
    {
      AddSkeletonDiagnostic(
          diagnostics,
          QObject::tr("%1 signature element %2 has no representable HLSL scalar/vector type.")
              .arg(prefix)
              .arg(i));
      continue;
    }

    if(!IsHLSLSemantic(semantic))
    {
      AddSkeletonDiagnostic(
          diagnostics,
          QObject::tr("%1 signature element %2 has unsupported semantic '%3'.")
              .arg(prefix)
              .arg(i)
              .arg(semantic));
      continue;
    }

    if(semantics.contains(semantic.toLower()))
    {
      AddSkeletonDiagnostic(
          diagnostics,
          QObject::tr("%1 signature contains duplicate semantic '%2'.")
              .arg(prefix)
              .arg(semantic));
      continue;
    }
    semantics.insert(semantic.toLower());

    QString candidate = sig.varName;
    if(candidate.isEmpty())
      candidate = prefix.toLower() + QLatin1Char('_') + semantic;
    const QString fieldName = UniqueHLSLIdentifier(candidate, lit("%1_%2").arg(prefix).arg(i),
                                                   fieldNames);

    contents += lit("\t%1 %2 : %3;\n").arg(type).arg(fieldName).arg(semantic);
    emitted++;
  }

  return emitted;
}

static HLSLAbiSkeleton GenerateHLSLAbiSkeleton(const ShaderReflection *shaderDetails,
                                               const QString &entryFunc)
{
  HLSLAbiSkeleton result;
  // i18n-ignore: generated HLSL source comments remain canonical English code text.
  result.source =
      lit("// ABI SKELETON ONLY - THIS IS NOT DECOMPILED SHADER SOURCE.\n"
          "// It contains only declarations recovered from reflection and a zero-behaviour entry.\n"
          "// This explanatory document is deliberately detached from Apply/ReplaceResource.\n"
          "// Use a configured decompiler or Organize and Apply Shader with Bread AI for an\n"
          "// executable candidate where the format/API supports one. Compilation, ABI validation,\n"
          "// shared apply, and rollback are separate gates. D3D12 DXBC/DXIL candidates additionally\n"
          "// require a checked rebuild of every affected graphics/compute PSO before commit.\n\n");

  if(!shaderDetails)
  {
    AddSkeletonDiagnostic(result.diagnostics, QObject::tr("No shader reflection is available."));
    return result;
  }

  QString structDefs;
  QString bindings;
  QSet<QString> globalNames;
  QSet<QString> typeNames;

  for(int i = 0; i < shaderDetails->samplers.count(); i++)
  {
    const ShaderSampler &samp = shaderDetails->samplers[i];
    const QString name = UniqueHLSLIdentifier(
        samp.name, lit("sampler_s%1_%2").arg(samp.fixedBindNumber).arg(i), globalNames);
    bindings += lit("SamplerState %1").arg(name);
    if(samp.bindArraySize > 1 && samp.bindArraySize != ~0U)
      bindings += lit("[%1]").arg(samp.bindArraySize);
    bindings += lit(" : %1; // comparison state cannot be inferred from reflection\n")
                    .arg(HLSLRegisterClause('s', samp.fixedBindNumber,
                                            samp.fixedBindSetOrSpace));
  }

  for(int category = 0; category < 2; category++)
  {
    const rdcarray<ShaderResource> &resources =
        category == 0 ? shaderDetails->readOnlyResources : shaderDetails->readWriteResources;
    const char registerClass = category == 0 ? 't' : 'u';

    for(int i = 0; i < resources.count(); i++)
    {
      const ShaderResource &res = resources[i];
      const QString name = UniqueHLSLIdentifier(
          res.name, lit("resource_%1%2_%3").arg(QLatin1Char(registerClass)).arg(res.fixedBindNumber).arg(i),
          globalNames);
      const QString type = HLSLResourceType(res, structDefs, result.diagnostics, name, typeNames);
      if(type.isEmpty())
      {
        bindings += lit("// unresolved resource %1 at %2\n")
                        .arg(name)
                        .arg(HLSLRegisterClause(registerClass, res.fixedBindNumber,
                                                res.fixedBindSetOrSpace));
        continue;
      }

      bindings += lit("%1 %2").arg(type).arg(name);
      if(res.bindArraySize > 1 && res.bindArraySize != ~0U)
        bindings += lit("[%1]").arg(res.bindArraySize);
      else if(res.bindArraySize == ~0U)
      {
        bindings += lit("[]");
        AddSkeletonDiagnostic(
            result.diagnostics,
            QObject::tr("%1 is an unbounded resource array; target-profile support is unknown.")
                .arg(name));
      }
      bindings += lit(" : %1;\n")
                      .arg(HLSLRegisterClause(registerClass, res.fixedBindNumber,
                                              res.fixedBindSetOrSpace));
    }
  }

  for(int i = 0; i < shaderDetails->constantBlocks.count(); i++)
  {
    const ConstantBlock &cbuf = shaderDetails->constantBlocks[i];
    QString preferredName = cbuf.name;
    if(preferredName == lit("$Globals"))
      preferredName = lit("_Globals");
    const QString name = UniqueHLSLIdentifier(
        preferredName, lit("cbuffer_b%1_%2").arg(cbuf.fixedBindNumber).arg(i), globalNames);

    if(!cbuf.bufferBacked || cbuf.inlineDataBytes || cbuf.compileConstants)
    {
      AddSkeletonDiagnostic(
          result.diagnostics,
          QObject::tr("%1 is not an ordinary buffer-backed HLSL cbuffer.").arg(name));
      bindings += lit("// unresolved non-buffer constant block %1\n").arg(name);
      continue;
    }

    if(cbuf.bindArraySize != 1)
      AddSkeletonDiagnostic(
          result.diagnostics,
          QObject::tr("%1 has descriptor array size %2, which cbuffer syntax cannot preserve.")
              .arg(name)
              .arg(cbuf.bindArraySize));

    QString contents;
    const int variables = AppendHLSLVariables(true, cbuf.variables, contents, structDefs,
                                              result.diagnostics, name, typeNames);
    if(variables == 0)
    {
      AddSkeletonDiagnostic(
          result.diagnostics,
          QObject::tr("%1 has no complete reflected variable layout (reported byte size %2).")
              .arg(name)
              .arg(cbuf.byteSize));
      bindings += lit("// unresolved cbuffer %1 : %2, byteSize=%3\n")
                      .arg(name)
                      .arg(HLSLRegisterClause('b', cbuf.fixedBindNumber,
                                              cbuf.fixedBindSetOrSpace))
                      .arg(cbuf.byteSize);
      continue;
    }

    bindings += lit("cbuffer %1 : %2 {\n%3};\n\n")
                    .arg(name)
                    .arg(HLSLRegisterClause('b', cbuf.fixedBindNumber,
                                            cbuf.fixedBindSetOrSpace))
                    .arg(contents);
  }

  result.source += structDefs + bindings + lit("\n");

  QString inputContents;
  QString outputContents;
  const int inputCount = AppendHLSLSignature(shaderDetails->inputSignature, lit("Input"),
                                             inputContents, result.diagnostics);
  const int outputCount = AppendHLSLSignature(shaderDetails->outputSignature, lit("Output"),
                                              outputContents, result.diagnostics);

  if(inputCount > 0)
    result.source += lit("struct InputStruct {\n%1};\n\n").arg(inputContents);
  if(outputCount > 0)
    result.source += lit("struct OutputStruct {\n%1};\n\n").arg(outputContents);

  if(shaderDetails->stage == ShaderStage::Compute)
  {
    const rdcfixedarray<uint32_t, 3> &threads = shaderDetails->dispatchThreadsDimension;
    if(threads[0] > 0 && threads[1] > 0 && threads[2] > 0)
      result.source += lit("[numthreads(%1, %2, %3)]\n")
                           .arg(threads[0])
                           .arg(threads[1])
                           .arg(threads[2]);
    else
      AddSkeletonDiagnostic(result.diagnostics,
                            QObject::tr("Compute thread-group dimensions are unavailable."));
  }
  else if(shaderDetails->stage != ShaderStage::Vertex && shaderDetails->stage != ShaderStage::Pixel)
  {
    AddSkeletonDiagnostic(
        result.diagnostics,
        QObject::tr("%1 entry-point attributes/topology cannot be reconstructed by the ABI skeleton.")
            .arg(ToQStr(shaderDetails->stage)));
  }

  result.source += outputCount > 0 ? lit("OutputStruct ") : lit("void ");
  result.source += SanitiseHLSLIdentifier(entryFunc, lit("EditedShader"));
  result.source += inputCount > 0 ? lit("(in InputStruct IN)\n{\n") : lit("()\n{\n");

  if(outputCount > 0)
    result.source += lit("\tOutputStruct OUT = (OutputStruct)0;\n\n"
                         "\t// Shader calculations were not recovered.\n\n"
                         "\treturn OUT;\n");
  else
    result.source += lit("\t// Shader calculations were not recovered.\n");

  result.source += lit("}\n");

  return result;
}

static QString ShaderWorkspaceStageName(ShaderStage stage)
{
  switch(stage)
  {
    case ShaderStage::Vertex: return lit("vertex");
    case ShaderStage::Hull: return lit("hull");
    case ShaderStage::Domain: return lit("domain");
    case ShaderStage::Geometry: return lit("geometry");
    case ShaderStage::Pixel: return lit("fragment");
    case ShaderStage::Compute: return lit("compute");
    case ShaderStage::Task: return lit("task");
    case ShaderStage::Mesh: return lit("mesh");
    case ShaderStage::RayGen: return lit("raygen");
    case ShaderStage::Intersection: return lit("intersection");
    case ShaderStage::AnyHit: return lit("anyhit");
    case ShaderStage::ClosestHit: return lit("closesthit");
    case ShaderStage::Miss: return lit("miss");
    case ShaderStage::Callable: return lit("callable");
    default: break;
  }

  return QString();
}

static bool HasShaderWorkspaceIntent(ICaptureContext &ctx, ShaderStage stage)
{
  const QString stageName = ShaderWorkspaceStageName(stage);
  if(stageName.isEmpty())
    return false;

  CaptureContext &capture = static_cast<CaptureContext &>(ctx);
  return capture.ReplayLab().AvailableShaderStages().contains(stageName);
}
}    // namespace

void PipelineStateViewer::showEvent(QShowEvent *event)
{
  if(m_DetailsPending && m_Ctx.IsCaptureLoaded())
    m_DetailsTimer->start(0);
  // we didn't set any default pipeline state in case it would be overridden by the persist data.
  // But if we don't have any persist data and we're about to show, default to D3D11.
  if(m_Current == NULL)
  {
    setToD3D11();
  }
}

void PipelineStateViewer::SetStencilLabelValue(QLabel *label, uint8_t value)
{
  label->setText(Formatter::Format(value, true));
  label->setToolTip(tr("%1 / 0x%2 / 0b%3")
                        .arg(value, 3, 10, QLatin1Char(' '))
                        .arg(Formatter::Format(value, true))
                        .arg(value, 8, 2, QLatin1Char('0')));
}

void PipelineStateViewer::SetStencilTreeItemValue(RDTreeWidgetItem *item, int column, uint8_t value)
{
  item->setText(column, Formatter::Format(value, true));
  item->setToolTip(column, tr("%1 / 0x%2 / 0b%3")
                               .arg(value, 3, 10, QLatin1Char(' '))
                               .arg(Formatter::Format(value, true))
                               .arg(value, 8, 2, QLatin1Char('0')));
}

void PipelineStateViewer::shaderEdit_clicked()
{
  QToolButton *sender = qobject_cast<QToolButton *>(QObject::sender());
  if(!sender)
    return;

  // activate the first item in the menu, if there are any items, as the default action.
  QMenu *menu = sender->menu();
  if(menu && !menu->actions().isEmpty())
    menu->actions()[0]->trigger();
}

IShaderViewer *PipelineStateViewer::EditShader(ResourceId id, ShaderStage shaderType,
                                               const rdcstr &entry, ShaderCompileFlags compileFlags,
                                               KnownShaderTool knownTool,
                                               ShaderEncoding shaderEncoding,
                                               const rdcstrpairs &files)
{
  IShaderViewer *sv = m_Ctx.EditShader(id, shaderType, entry, files, knownTool, shaderEncoding,
                                       compileFlags, NULL, NULL);

  m_Ctx.AddDockWindow(sv->Widget(), DockReference::AddTo, this);

  return sv;
}

IShaderViewer *PipelineStateViewer::ViewShaderExplanation(ShaderStage shaderType,
                                                          const rdcstr &entry,
                                                          const rdcstrpairs &files,
                                                          const rdcstr &diagnostics)
{
  // A null ResourceId is intentional. Explanatory IR/ABI skeletons are editable as notes, but they
  // are never attached to the captured shader's ReplaceResource path.
  IShaderViewer *sv = m_Ctx.EditShader(ResourceId(), shaderType, entry, files,
                                       KnownShaderTool::Unknown, ShaderEncoding::Unknown, {}, NULL,
                                       NULL);

  if(!sv || !sv->Widget())
    return NULL;

  QWidget *widget = sv->Widget();
  widget->setProperty("renderdoc_shader_representation", lit("explanatory-ir"));
  widget->setWindowTitle(tr("Shader ABI Skeleton (Explanatory Only)"));
  sv->SetExplanatoryMode(diagnostics);
  m_Ctx.AddDockWindow(widget, DockReference::AddTo, this);
  return sv;
}

static int FindShaderEditSourceFile(const ShaderDebugInfo &debugInfo)
{
  int entryFile = debugInfo.editBaseFile;

  if(entryFile < 0 || entryFile >= debugInfo.files.count())
    entryFile = debugInfo.entryLocation.fileIndex;

  if(entryFile >= 0 && entryFile < debugInfo.files.count() &&
     !debugInfo.files[entryFile].contents.isEmpty())
    return entryFile;

  for(int i = 0; i < debugInfo.files.count(); i++)
    if(!debugInfo.files[i].contents.isEmpty())
      return i;

  return -1;
}

IShaderViewer *PipelineStateViewer::EditOriginalShaderSource(ResourceId id,
                                                             const ShaderReflection *shaderDetails)
{
  QSet<uint> uniqueFiles;
  rdcstrpairs files;

  // add the entry point file first, if we have one
  const int entryFile = FindShaderEditSourceFile(shaderDetails->debugInfo);

  if(entryFile < 0)
    return NULL;

  for(int i = -1; i < shaderDetails->debugInfo.files.count(); i++)
  {
    int idx = i;
    if(idx < 0)
      idx = entryFile;
    else if(idx == entryFile)
      continue;

    if(idx < 0 || idx >= shaderDetails->debugInfo.files.count())
      continue;

    const ShaderSourceFile &s = shaderDetails->debugInfo.files[idx];

    if(s.contents.isEmpty())
      continue;

    QString filename = s.filename;

    uint filenameHash = qHash(filename.toLower());

    if(uniqueFiles.contains(filenameHash))
    {
      qWarning() << lit("Duplicate full filename") << filename;
      continue;
    }
    uniqueFiles.insert(filenameHash);

    files.push_back(make_rdcpair(s.filename, s.contents));
  }

  return EditShader(id, shaderDetails->stage, shaderDetails->debugInfo.entrySourceName,
                    shaderDetails->debugInfo.compileFlags, shaderDetails->debugInfo.compiler,
                    shaderDetails->debugInfo.encoding, files);
}

IShaderViewer *PipelineStateViewer::EditDecompiledSource(const ShaderProcessingTool &tool,
                                                         ResourceId id,
                                                         const ShaderReflection *shaderDetails)
{
  ShaderToolOutput out = tool.DisassembleShader(this, shaderDetails, "");

  if(out.result.isEmpty())
  {
    QString log = QString::fromUtf8(out.log.c_str(), int(out.log.size()));
    if(log.isEmpty())
      log = tr("<No diagnostic log was produced.>");

    RDDialog::critical(
        this, tr("Shader Decompilation Failed"),
        tr("The shader processing tool '%1' produced no shader source.\n\nLog:\n%2")
            .arg(tool.name)
            .arg(log));
    return NULL;
  }

  rdcstr source;
  source.assign((const char *)out.result.data(), out.result.size());

  rdcstrpairs files;
  files.push_back(rdcpair<rdcstr, rdcstr>("decompiled", source));

  ShaderCompileFlags flags;

  for(const ShaderCompileFlag &flag : shaderDetails->debugInfo.compileFlags.flags)
    if(flag.name == "@spirver")
      flags.flags.push_back(flag);

  IShaderViewer *sv = EditShader(id, shaderDetails->stage, shaderDetails->entryPoint, flags,
                                 KnownShaderTool::Unknown, tool.output, files);

  sv->ShowErrors(out.log);

  return sv;
}

void PipelineStateViewer::SetupShaderEditButton(QToolButton *button, ResourceId pipelineId,
                                                ResourceId shaderId,
                                                const ShaderReflection *shaderDetails)
{
  if(!shaderDetails || !button->isEnabled() || button->popupMode() != QToolButton::MenuButtonPopup)
    return;

  // Menu actions outlive the currently published pipeline state. Keep one owned
  // reflection for all actions, including both hops of the SPIR-V explanation.
  const std::shared_ptr<const ShaderReflection> ownedShaderDetails =
      std::make_shared<ShaderReflection>(*shaderDetails);
  shaderDetails = ownedShaderDetails.get();

  QMenu *menu = editMenus[(int)shaderDetails->stage];

  menu->clear();

  // The primary Edit action always goes to the capture-owned Bread workspace. Stock editors
  // remain available in the dropdown. Reuse capture-owned bindings before any refresh.
  QAction *workspace = new QAction(tr("Open in Shader Workspace"), menu);
  workspace->setIcon(Icons::page_white_edit());
  const QString stageName = ShaderWorkspaceStageName(shaderDetails->stage);
  connect(workspace, &QAction::triggered, this, [this, stageName, shaderId, ownedShaderDetails]() {
    uint64_t resourceValue = 0;
    static_assert(sizeof(resourceValue) == sizeof(shaderId), "ResourceId wire size changed");
    memcpy(&resourceValue, &shaderId, sizeof(resourceValue));
    CaptureContext &capture = static_cast<CaptureContext &>(m_Ctx);
    capture.ReplayLab().OpenShaderWorkspace(m_Ctx.CurEvent(), stageName,
        QString::number(resourceValue), QString(ownedShaderDetails->entryPoint));
  });
  menu->addAction(workspace);

  rdcarray<ShaderEncoding> accepted = m_Ctx.TargetShaderEncodings();

  const ShaderDebugInfo &dbg = shaderDetails->debugInfo;

  // if we have original source and it's in a known format, display it as the first most preferred
  // option
  const int entryFile = FindShaderEditSourceFile(dbg);
  if(entryFile >= 0 && dbg.encoding != ShaderEncoding::Unknown)
  {
    QAction *action = new QAction(tr("Edit Source - %1").arg(dbg.files[entryFile].filename), menu);
    action->setIcon(Icons::page_white_edit());

    QObject::connect(action, &QAction::triggered, [this, shaderId, ownedShaderDetails]() {
      const auto reflection = ownedShaderDetails;
      EditOriginalShaderSource(shaderId, reflection.get());
    });

    menu->addAction(action);
  }

  // Prefer capture-bound Bread organization whenever the selected stage is bound. Capability
  // The Agent decides whether a configured/hash-pinned editable round trip is
  // available; evidence-only reconstruction must not be presented here as compile/apply support.
  // This action is deliberately ahead of stock processing tools unless embedded source was
  // available above.
  if(HasShaderWorkspaceIntent(m_Ctx, shaderDetails->stage))
  {
    QAction *action = new QAction(tr("Organize and Apply Shader with Bread AI"), menu);
    action->setIcon(Icons::page_white_edit());
    action->setToolTip(
        tr("Ask Bread AI to organize this capture-bound Shader and apply the finished result. "
           "It chooses the relevant evidence and editing tools. You can inspect its draft in "
           "Shader Workspace; applying a replacement checks the exact target, compile result, "
           "ABI, and current replay state."));

    const ShaderStage shaderStage = shaderDetails->stage;
    QObject::connect(action, &QAction::triggered, [this, shaderStage]() {
      CaptureContext &capture = static_cast<CaptureContext &>(m_Ctx);
      const QString stageName = ShaderWorkspaceStageName(shaderStage);
      AIAssistant *assistant = capture.GetAIAssistant();
      assistant->StartCaptureIntent(tr("Organize and apply selected Shader with Bread AI"),
                                    stageName,
                                    lit("reverse-edit-selected-shader"));    // i18n-ignore: stable UI intent identifier
      capture.ShowAIAssistant();
    });

    menu->addAction(action);
  }

  // next up, try the shader processing tools in order - all the ones that will decompile from our
  // native representation. We don't check here yet if we have a valid compiler to compile from the
  // output to what we want.
  for(const ShaderProcessingTool &tool : m_Ctx.Config().ShaderProcessors)
  {
    // skip tools that can't decode our shader, or doesn't produce a textual output
    if(tool.input != shaderDetails->encoding || !IsTextRepresentation(tool.output))
      continue;

    QAction *action = new QAction(tr("Decompile with %1").arg(tool.name), menu);
    action->setIcon(Icons::page_white_edit());

    QObject::connect(action, &QAction::triggered, [this, tool, shaderId, ownedShaderDetails]() {
      // Tool execution may run a nested event loop and rebuild/delete this menu.
      const auto reflection = ownedShaderDetails;
      EditDecompiledSource(tool, shaderId, reflection.get());
    });

    menu->addAction(action);
  }

  // Last-resort representations are explanatory only. In particular, reflection is allowed to
  // omit source types/names and output declarations, so a generated ABI skeleton must never enter
  // the captured shader's compile/ReplaceResource path.
  if(shaderDetails->encoding == ShaderEncoding::DXBC ||
     shaderDetails->encoding == ShaderEncoding::DXIL ||
     shaderDetails->encoding == ShaderEncoding::SPIRV ||
     shaderDetails->encoding == ShaderEncoding::OpenGLSPIRV)
  {
    QString label = tr("Review ABI Skeleton (Not Decompiled)");

    if(shaderDetails->encoding == ShaderEncoding::SPIRV ||
       shaderDetails->encoding == ShaderEncoding::OpenGLSPIRV)
      label = tr("Review Native SPIR-V (Explanatory Only)");

    QAction *action = new QAction(label, menu);
    action->setIcon(Icons::page_white_edit());

    QObject::connect(action, &QAction::triggered, [this, pipelineId, ownedShaderDetails]() {
      const auto reflection = ownedShaderDetails;
      QString entry;

      if(reflection->encoding == ShaderEncoding::SPIRV ||
         reflection->encoding == ShaderEncoding::OpenGLSPIRV)
      {
        m_Ctx.Replay().AsyncInvoke([this, pipelineId, reflection](IReplayController *r) {
          rdcstr disasm = r->DisassembleShader(pipelineId, reflection.get(), "");

          QString editeddisasm =
              tr("; NATIVE SPIR-V DISASSEMBLY - EXPLANATORY LANE ONLY\n") +
              tr("; Use a validated SPIR-V assembler/decompiler route to create executable source.\n\n");

          editeddisasm += disasm;

          GUIInvoke::call(this, [this, reflection, editeddisasm]() {
            rdcstrpairs files;
            files.push_back(rdcpair<rdcstr, rdcstr>("native_spirv_explanation.txt", editeddisasm));

            ViewShaderExplanation(
                reflection->stage, reflection->entryPoint, files,
                tr("Representation lane: explanatory-ir\n"
                   "The text is native disassembly, not HLSL/GLSL source. Apply is disabled.\n"
                   "Use the external workspace for assemble/validate/ABI-gated replacement."));
          });
        });
      }
      else if(reflection->encoding == ShaderEncoding::DXBC ||
              reflection->encoding == ShaderEncoding::DXIL)
      {
        // i18n-ignore: generated HLSL entry-point identifier, not user-visible prose.
        entry = lit("EditedShader%1S").arg(ToQStr(reflection->stage, GraphicsAPI::D3D11)[0]);

        HLSLAbiSkeleton skeleton = GenerateHLSLAbiSkeleton(reflection.get(), entry);

        QString diagnostics =
            tr("Representation lane: explanatory-ir\n"
               "This ABI skeleton is generated from reflection; it is not recovered shader "
               "logic. Apply is disabled.\n");
        if(reflection->encoding == ShaderEncoding::DXIL)
          diagnostics +=
              tr("No trusted DXIL-to-executable-HLSL round-trip is implied by this view. A "
                 "reconstructed cross-IR result remains experimental until baseline compilation "
                 "and strict ABI validation pass.\n");

        if(!skeleton.diagnostics.isEmpty())
          diagnostics += tr("\nIncomplete reflection / non-representable ABI fields:\n- ") +
                         skeleton.diagnostics.join(lit("\n- "));

        rdcstrpairs files;
        files.push_back(rdcpair<rdcstr, rdcstr>("abi_skeleton_explanatory.hlsl", skeleton.source));

        ViewShaderExplanation(reflection->stage, entry, files, diagnostics);
      }
    });

    menu->addAction(action);
  }

  button->setMenu(menu);
}

void PipelineStateViewer::AddResourceUsageEntry(QMenu &menu, uint32_t start, uint32_t end,
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

  QObject::connect(item, &QAction::triggered, [this, end]() { m_Ctx.SetEventID({}, end, end); });

  menu.addAction(item);
}

void PipelineStateViewer::ShowResourceContextMenu(RDTreeWidget *widget, const QPoint &pos,
                                                  ResourceId id, const rdcarray<EventUsage> &usage)
{
  RDTreeWidgetItem *item = widget->itemAt(pos);

  QMenu contextMenu(this);

  QModelIndexList selectedRows = widget->selectionModel()->selectedRows();
  bool multiSelected = selectedRows.count() > 1;

  if(multiSelected)
  {
    // build CSV now while selection is still valid
    QString csvData = widget->buildSelectionCSV();

    QAction copyCSV(tr("&Copy as CSV"), this);
    copyCSV.setIcon(Icons::copy());

    contextMenu.addAction(&copyCSV);

    QObject::connect(&copyCSV, &QAction::triggered,
                     [csvData]() {
                       QClipboard *clipboard = QApplication::clipboard();
                       clipboard->setText(csvData);
                     });

    RDDialog::show(&contextMenu, widget->viewport()->mapToGlobal(pos));
    return;
  }

  QAction copy(tr("&Copy"), this);

  contextMenu.addAction(&copy);

  copy.setIcon(Icons::copy());

  QObject::connect(&copy, &QAction::triggered,
                   [widget, pos, item]() { widget->copyItem(pos, item); });

  QAction usageTitle(tr("Used:"), this);
  QAction openRenderTargetHistory(tr("Open Render Target History"), this);
  QAction openResourceInspector(tr("Open in Resource Inspector"), this);

  openRenderTargetHistory.setIcon(Icons::link());
  openResourceInspector.setIcon(Icons::link());

  if(id != ResourceId())
  {
    contextMenu.addSeparator();

    Subresource historySubresource;
    CompType historyTypeCast = CompType::Typeless;
    if(GetTextureHistoryDetails(item, id, historySubresource, historyTypeCast))
    {
      contextMenu.addAction(&openRenderTargetHistory);
      QObject::connect(&openRenderTargetHistory, &QAction::triggered,
                       [this, id, historySubresource, historyTypeCast]() {
                         OpenRenderTargetHistory(id, historySubresource, historyTypeCast);
                       });
    }

    contextMenu.addAction(&openResourceInspector);
    const QJsonObject attachment = ReplayLabAIResourceContext(m_Ctx, id, "pipeline-state-viewer");
    QJsonObject details = ReplayLabAIResourceDetails(m_Ctx, id, attachment);
    if(item)
    {
      QJsonArray row;
      for(int column = 0; column < widget->model()->columnCount(); ++column)
        row.append(item->text(column));
      details[QLatin1String("selectedRow")] = row;
    }
    const QString title = tr("Resource: %1 (EID %2)").arg(m_Ctx.GetResourceName(id)).arg(m_Ctx.CurEvent());
    const QString json = ReplayLabAIResourceJSON(attachment);
    const QString text = ReplayLabAIResourceJSON(details);
    QAction *addToChat = contextMenu.addAction(tr("Add resource to AI conversation"));
    addToChat->setObjectName(QStringLiteral("addResourceToAI"));
    QObject::connect(addToChat, &QAction::triggered, [this, title, text, json]() {
      m_Ctx.AddTextToAIAssistant(title, text, title, json);
    });
    contextMenu.addAction(&usageTitle);

    QObject::connect(&openResourceInspector, &QAction::triggered, [this, id]() {
      m_Ctx.ShowResourceInspector();

      m_Ctx.GetResourceInspector()->Inspect(id);
    });

    CombineUsageEvents(m_Ctx, usage, false,
                       [this, &contextMenu](uint32_t start, uint32_t end, ResourceUsage use) {
                         AddResourceUsageEntry(contextMenu, start, end, use);
                       });
  }

  RDDialog::show(&contextMenu, widget->viewport()->mapToGlobal(pos));
}

ResourceId PipelineStateViewer::updateThumbnail(QWidget *widget, QModelIndex idx)
{
  ResourceId id;

  if(!m_Output)
    return id;

  RDTreeWidget *treeWidget = qobject_cast<RDTreeWidget *>(widget);
  if(treeWidget)
  {
    RDTreeWidgetItem *item = treeWidget->itemForIndex(idx);

    if(item)
    {
      if(m_D3D11)
        id = m_D3D11->GetResource(item);
      else if(m_D3D12)
        id = m_D3D12->GetResource(item);
      else if(m_GL)
        id = m_GL->GetResource(item);
      else if(m_Vulkan)
        id = m_Vulkan->GetResource(item);
    }

    TextureDescription *tex = m_Ctx.GetTexture(id);

    if(tex)
    {
      m_TexDisplay.resourceId = id;
      INVOKE_MEMFN(RT_UpdateAndDisplay);

      float aspect = (float)tex->width / (float)qMax(1U, tex->height);

      // keep height fixed at 100, and make width match the aspect ratio of the texture - up to 21:9
      // ratio
      ui->thumbnail->setFixedSize((int)qBound(100.0f, aspect * 100.0f, (21.0f / 9.0f) * 100.0f), 100);
      ui->thumbnail->show();
    }
    else
    {
      ui->thumbnail->hide();
    }
  }

  return id;
}

bool PipelineStateViewer::hasThumbnail(QWidget *widget, QModelIndex idx)
{
  if(!m_Output)
    return false;

  RDTreeWidget *treeWidget = qobject_cast<RDTreeWidget *>(widget);
  if(treeWidget)
  {
    ResourceId id;

    RDTreeWidgetItem *item = treeWidget->itemForIndex(idx);

    if(item)
    {
      if(m_D3D11)
        id = m_D3D11->GetResource(item);
      else if(m_D3D12)
        id = m_D3D12->GetResource(item);
      else if(m_GL)
        id = m_GL->GetResource(item);
      else if(m_Vulkan)
        id = m_Vulkan->GetResource(item);
    }

    if(id != ResourceId() && m_Ctx.GetTexture(id))
      return true;
  }

  return false;
}

void PipelineStateViewer::SetupResourceView(RDTreeWidget *widget)
{
  auto handler = [this, widget](const QPoint &pos) {
    RDTreeWidgetItem *item = widget->itemAt(pos);

    ResourceId id;

    if(m_D3D11)
      id = m_D3D11->GetResource(item);
    else if(m_D3D12)
      id = m_D3D12->GetResource(item);
    else if(m_GL)
      id = m_GL->GetResource(item);
    else if(m_Vulkan)
      id = m_Vulkan->GetResource(item);

    if(id != ResourceId())
    {
      QPointer<PipelineStateViewer> owner(this);
      QPointer<RDTreeWidget> tree(widget);
      const uint32_t eventId = m_Ctx.CurEvent();
      const rdcstr capture = m_Ctx.GetCaptureFilename();
      m_Ctx.Replay().AsyncInvoke([owner, tree, pos, id, eventId, capture](IReplayController *r) {
        rdcarray<EventUsage> usage = r->GetUsage(id);
        if(!owner)
          return;
        GUIInvoke::call(owner, [owner, tree, pos, id, usage, eventId, capture]() {
          if(owner && tree && owner->m_Ctx.IsCaptureLoaded() &&
             owner->m_Ctx.CurEvent() == eventId && owner->m_Ctx.GetCaptureFilename() == capture)
            owner->ShowResourceContextMenu(tree, pos, id, usage);
        });
      });
    }
    else
    {
      ShowResourceContextMenu(widget, pos, id, {});
    }
  };

  widget->setContextMenuPolicy(Qt::CustomContextMenu);
  widget->setSelectionMode(QAbstractItemView::ExtendedSelection);
  QObject::connect(widget, &RDTreeWidget::customContextMenuRequested, handler);

  widget->setResourceLinkHandler(
      [this](ResourceId id, RDTreeWidgetItem *item, int) {
        Subresource subresource;
        CompType typeCast = CompType::Typeless;
        if(!GetTextureHistoryDetails(item, id, subresource, typeCast))
          return false;

        OpenRenderTargetHistory(id, subresource, typeCast);
        return true;
      });

  widget->setCustomTooltip(m_Tooltip);
}

bool PipelineStateViewer::GetTextureHistoryDetails(RDTreeWidgetItem *item, ResourceId id,
                                                   Subresource &subresource, CompType &typeCast)
{
  TextureDescription *texture = m_Ctx.GetTexture(id);
  if(!item || !texture)
    return false;

  ResourceId taggedResource;
  bool detailsFound = false;
  if(m_D3D11)
  {
    taggedResource = m_D3D11->GetResource(item);
    detailsFound = m_D3D11->GetTextureHistoryDetails(item, subresource, typeCast);
  }
  else if(m_D3D12)
  {
    taggedResource = m_D3D12->GetResource(item);
    detailsFound = m_D3D12->GetTextureHistoryDetails(item, subresource, typeCast);
  }
  else if(m_GL)
  {
    taggedResource = m_GL->GetResource(item);
    detailsFound = m_GL->GetTextureHistoryDetails(item, subresource, typeCast);
  }
  else if(m_Vulkan)
  {
    taggedResource = m_Vulkan->GetResource(item);
    detailsFound = m_Vulkan->GetTextureHistoryDetails(item, subresource, typeCast);
  }

  if(!detailsFound || taggedResource != id)
    return false;

  subresource.mip = qMin(subresource.mip, qMax(1U, texture->mips) - 1U);
  const uint32_t sliceCount = texture->depth > 1
                                  ? qMax(1U, texture->depth >> qMin(subresource.mip, 31U))
                                  : qMax(1U, texture->arraysize);
  subresource.slice = qMin(subresource.slice, sliceCount - 1U);
  if(typeCast == CompType::Typeless)
    typeCast = texture->format.compType;
  if(texture->msSamp > 1 && subresource.sample == TextureDisplay::ResolveSamples &&
     (typeCast == CompType::UInt || typeCast == CompType::SInt || typeCast == CompType::Depth ||
      (texture->creationFlags & TextureCategory::DepthTarget)))
    subresource.sample = 0;
  return true;
}

void PipelineStateViewer::OpenRenderTargetHistory(ResourceId id,
                                                  const Subresource &subresource,
                                                  CompType typeCast)
{
  RenderTargetHistoryView::Open(m_Ctx, id, subresource, typeCast, m_Ctx.CurEvent(), this);
}

QString PipelineStateViewer::GetVBufferFormatString(uint32_t slot)
{
  rdcarray<BoundVBuffer> vbs = m_Ctx.CurPipelineState().GetVBuffers();
  rdcarray<VertexInputAttribute> attrs = m_Ctx.CurPipelineState().GetVertexInputs();

  if(slot >= vbs.size())
    return tr("// Unbound vertex buffer slot %1").arg(slot);

  uint32_t stride = vbs[slot].byteStride;

  // filter attributes to only the ones enabled and using this slot
  attrs.removeIf([slot](const VertexInputAttribute &attr) {
    return (!attr.used || attr.vertexBuffer != (int)slot);
  });

  // we now have all attributes in this buffer. Sort by offset
  std::sort(attrs.begin(), attrs.end(),
            [](const VertexInputAttribute &a, const VertexInputAttribute &b) {
              return a.byteOffset < b.byteOffset;
            });

  // ensure we don't have any overlap between attributes or with the stride
  for(size_t i = 0; i < attrs.size(); i++)
  {
    uint32_t cursz = byteSize(attrs[i].format);
    if(cursz == 0)
      return tr("// Unhandled vertex attribute type '%1'").arg(ToQStr(attrs[i].format.type));

    // for all but the first attribute, ensure no overlaps with previous. We allow identical
    // elements
    if(i > 0)
    {
      uint32_t prevsz = byteSize(attrs[i - 1].format);

      if((attrs[i - 1].byteOffset != attrs[i].byteOffset || cursz != prevsz) &&
         attrs[i - 1].byteOffset + prevsz > attrs[i].byteOffset)
        return tr("// vertex attributes overlapping, no automatic format available");
    }

    if(i + 1 == attrs.size())
    {
      // for the last attribute, ensure the total size doesn't overlap stride
      if(attrs[i].byteOffset + cursz > stride && stride > 0)
        return tr("// vertex stride %1 less than total data fetched %2")
            .arg(stride)
            .arg(attrs[i].byteOffset + cursz);
    }
  }

  QString format;

  uint32_t offset = 0;

  format = lit("struct vbuffer {\n");

  for(size_t i = 0; i < attrs.size(); i++)
  {
    // we disallowed overlaps above, but we do allow *duplicates*. So if our offset has already
    // passed, silently skip this element.
    if(attrs[i].byteOffset < offset)
      continue;

    // declare an explicit offset if there's a gap from previous element to this one
    if(attrs[i].byteOffset > offset)
      format += lit("  [[offset(%1)]]\n").arg(attrs[i].byteOffset);

    format += lit("  ");

    const ResourceFormat &fmt = attrs[i].format;

    offset = attrs[i].byteOffset + byteSize(fmt);

    if(fmt.Special())
    {
      switch(fmt.type)
      {
        case ResourceFormatType::R10G10B10A2:
          if(fmt.compType == CompType::UNorm)
            format += lit("[[packed(r10g10b10a2)]] [[unorm]] uint4");
          else
            format += lit("[[packed(r10g10b10a2)]] uint4");
          break;
        case ResourceFormatType::R11G11B10: format += lit("[[packed(r11g11b10)]] float3"); break;
        default: format += tr("// unknown type "); break;
      }
    }
    else
    {
      QChar widthchar[] = {
          QLatin1Char('?'), QLatin1Char('b'), QLatin1Char('h'), QLatin1Char('?'), QLatin1Char('f'),
      };

      if(fmt.compType == CompType::UNorm || fmt.compType == CompType::UNormSRGB)
      {
        format += lit("[[unorm]]");
      }
      else if(fmt.compType == CompType::SNorm)
      {
        format += lit("[[snorm]]");
      }

      if(fmt.compType == CompType::UInt || fmt.compType == CompType::UNorm ||
         fmt.compType == CompType::UNormSRGB)
        format += lit("u");

      if(fmt.compByteWidth == 1)
      {
        format += lit("byte");
      }
      else if(fmt.compByteWidth == 2)
      {
        if(fmt.compType == CompType::Float)
          format += lit("half");
        else
          format += lit("short");
      }
      else if(fmt.compByteWidth == 4)
      {
        if(fmt.compType == CompType::Float)
          format += lit("float");
        else
          format += lit("int");
      }
      else if(fmt.compByteWidth == 8)
      {
        if(fmt.compType == CompType::Float)
          format += lit("double");
        else
          format += lit("long");
      }

      format += QString::number(fmt.compCount);
    }

    QString real_name = QString(attrs[i].name);
    QString sanitised_name = real_name;

    sanitised_name.replace(QLatin1Char('.'), QLatin1Char('_'))
        .replace(QLatin1Char(':'), QLatin1Char('_'))
        .replace(QLatin1Char('['), QLatin1Char('_'))
        .replace(QLatin1Char(']'), QLatin1Char('_'));

    if(real_name == sanitised_name)
      format += QFormatStr(" %1;\n").arg(sanitised_name);
    else
      format += QFormatStr(" %1; // %2\n").arg(sanitised_name).arg(real_name);
  }

  format += lit("}\n\nvbuffer vertex[];");

  if(stride > offset)
    format = lit("[[size(%1)]]\n").arg(stride) + format;

  format = lit("#pack(scalar) // vertex buffers can be tightly packed\n"
               "\n") +
           format;

  return format;
}

QColor PipelineStateViewer::GetViewDetailsColor()
{
  return QColor::fromHslF(0.45f, 1.0f,
                          qBound(0.25, palette().color(QPalette::Base).lightnessF(), 0.75));
}

bool PipelineStateViewer::SaveShaderFile(const ShaderReflection *shader)
{
  if(!shader)
    return false;

  // The save dialog pumps events, so a replacement may retire this reflection
  // before it returns. Export the bytes selected when the action was invoked.
  const bytebuf rawBytes = shader->rawBytes;
  QString filter;

  switch(shader->encoding)
  {
    case ShaderEncoding::DXBC: filter = tr("DXBC Shader files (*.dxbc)"); break;
    case ShaderEncoding::HLSL: filter = tr("HLSL files (*.hlsl)"); break;
    case ShaderEncoding::GLSL: filter = tr("GLSL files (*.glsl)"); break;
    case ShaderEncoding::SPIRV:
    case ShaderEncoding::OpenGLSPIRV: filter = tr("SPIR-V files (*.spv)"); break;
    case ShaderEncoding::SPIRVAsm:
    case ShaderEncoding::OpenGLSPIRVAsm: filter = tr("SPIR-V assembly files (*.spvasm)"); break;
    case ShaderEncoding::DXIL: filter = tr("DXIL Shader files (*.dxbc)"); break;
    case ShaderEncoding::Slang: filter = tr("Slang Shader files (*.slang)"); break;
    case ShaderEncoding::Unknown:
    case ShaderEncoding::Count: filter = tr("All files (*.*)"); break;
  }

  QString filename = RDDialog::getSaveFileName(this, tr("Save Shader As"), QString(), filter);

  if(!filename.isEmpty())
  {
    ANALYTIC_SET(Export.Shader, true);

    QDir dirinfo = QFileInfo(filename).dir();
    if(dirinfo.exists())
    {
      QFile f(filename);
      if(f.open(QIODevice::WriteOnly | QIODevice::Truncate))
      {
        f.write((const char *)rawBytes.data(), (qint64)rawBytes.size());
      }
      else
      {
        RDDialog::critical(
            this, tr("Error saving shader"),
            tr("Couldn't open path %1 for write.\n%2").arg(filename).arg(f.errorString()));
        return false;
      }
    }
    else
    {
      RDDialog::critical(this, tr("Invalid directory"),
                         tr("Cannot find target directory to save to"));
      return false;
    }
  }

  return true;
}

void PipelineStateViewer::SelectPipelineStage(PipelineStage stage)
{
  if(m_D3D11)
    m_D3D11->SelectPipelineStage(stage);
  else if(m_D3D12)
    m_D3D12->SelectPipelineStage(stage);
  else if(m_GL)
    m_GL->SelectPipelineStage(stage);
  else if(m_Vulkan)
    m_Vulkan->SelectPipelineStage(stage);
}

ScopedTreeUpdater::ScopedTreeUpdater(RDTreeWidget *widget) : m_Widget(widget)
{
  vs = m_Widget->verticalScrollBar()->value();
  m_Widget->beginUpdate();
  m_Widget->clear();
}

ScopedTreeUpdater::~ScopedTreeUpdater()
{
  m_Widget->clearSelection();
  m_Widget->endUpdate();
  m_Widget->verticalScrollBar()->setValue(vs);
}

#if ENABLE_UNIT_TESTS

#include "3rdparty/catch/catch.hpp"

TEST_CASE("Generated shader ABI skeleton fails closed on incomplete DXIL reflection",
          "[qrenderdoc][shader][abi-skeleton]")
{
  ShaderReflection reflection;
  reflection.encoding = ShaderEncoding::DXIL;
  reflection.stage = ShaderStage::Pixel;

  ConstantBlock cb;
  cb.name = "cbuffer0";
  cb.fixedBindNumber = 0;
  cb.byteSize = 160;

  ShaderConstant unresolved;
  unresolved.type.baseType = VarType::Unknown;
  unresolved.type.rows = 1;
  unresolved.type.columns = 1;
  unresolved.type.elements = 10;
  cb.variables.push_back(unresolved);
  reflection.constantBlocks.push_back(cb);

  SigParameter position;
  position.systemValue = ShaderBuiltin::Position;
  position.varType = VarType::Float;
  position.compCount = 4;
  position.regIndex = 0;
  reflection.inputSignature.push_back(position);

  ShaderSampler samplerArray;
  samplerArray.name = "linear-sampler";
  samplerArray.fixedBindNumber = 3;
  samplerArray.bindArraySize = 4;
  reflection.samplers.push_back(samplerArray);

  for(uint16_t semanticIndex : {uint16_t(6), uint16_t(9), uint16_t(8)})
  {
    SigParameter texcoord;
    texcoord.semanticName = "TEXCOORD";
    texcoord.semanticIndex = semanticIndex;
    texcoord.semanticIdxName = "TEXCOORD" + ToStr(semanticIndex);
    texcoord.varType = VarType::Float;
    texcoord.compCount = semanticIndex == 9 ? 3 : (semanticIndex == 8 ? 1 : 4);
    texcoord.regIndex = 0;    // reproduces the duplicate param0 bug in the old generator
    reflection.inputSignature.push_back(texcoord);
  }

  HLSLAbiSkeleton skeleton = GenerateHLSLAbiSkeleton(&reflection, lit("EditedShaderPS"));

  CHECK(skeleton.source.contains(lit("ABI SKELETON ONLY")));
  CHECK_FALSE(skeleton.source.contains(lit("unknown[10]"), Qt::CaseInsensitive));
  CHECK_FALSE(skeleton.source.contains(lit("struct OutputStruct")));
  CHECK(skeleton.source.contains(lit("void EditedShaderPS")));
  CHECK(skeleton.source.contains(lit("SamplerState linear_sampler[4] : register(s3);")));
  CHECK_FALSE(skeleton.source.contains(lit("register(s3)[4]")));
  CHECK(skeleton.source.contains(lit("input_TEXCOORD6")));
  CHECK(skeleton.source.contains(lit("input_TEXCOORD9")));
  CHECK(skeleton.source.contains(lit("input_TEXCOORD8")));
  CHECK(skeleton.diagnostics.join(lit("\n")).contains(lit("unrepresentable reflected type")));
}

TEST_CASE("HLSL ABI skeleton identifiers are legal and unique",
          "[qrenderdoc][shader][abi-skeleton]")
{
  QSet<QString> used;
  CHECK(bool(UniqueHLSLIdentifier(lit("param0"), lit("field"), used) == lit("param0")));
  CHECK(bool(UniqueHLSLIdentifier(lit("param0"), lit("field"), used) == lit("param0_2")));
  CHECK(bool(UniqueHLSLIdentifier(lit("9 invalid-name"), lit("field"), used) ==
             lit("_9_invalid_name")));
  CHECK(bool(UniqueHLSLIdentifier(lit("float"), lit("field"), used) == lit("_float")));
}

#endif
